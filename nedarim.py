"""Renewal sync with Nedarim Plus (read-only).

Monthly standing orders are charged by Nedarim Plus every month. This module reads the
transaction history (GetHistoryJson) and extends the matching subscriber for every new
successful charge. Matching: the category we send with every phone payment
("<category> <subscriber id>"), or a standing-order id we already know.

Nothing is written to Nedarim Plus, and no webhook is configured there, so an existing
webhook (e.g. of the old registration system) is never overwritten.

Limits: GetHistoryJson allows 20 requests/hour per institution + IP. We use at most
`MAX_PAGES` requests per run and run every 20 minutes.
"""
import json
import re
import time
import urllib.parse
import urllib.request

import store

URL = 'https://matara.pro/nedarimplus/Reports/Manage3.aspx'
MAX_PAGES = 3
PAGE = 2000


def kv_get(con, key, default=None):
    row = con.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def kv_set(con, key, value):
    con.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, str(value)))


def fetch_history(cfg, last_id):
    n = cfg['nedarim']
    data = urllib.parse.urlencode({'Action': 'GetHistoryJson', 'MosadId': n['mosad'], 'ApiPassword': n['api_password'],
                                   'LastId': last_id or '', 'MaxId': PAGE}).encode()
    req = urllib.request.Request(URL, data=data, method='POST', headers={'User-Agent': 'curl/8.5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(50 * 1024 * 1024).decode('utf-8', 'replace')
    try:
        rows = json.loads(body)
    except ValueError:
        raise RuntimeError('nedarim: ' + body[:200])
    if isinstance(rows, dict):  # error envelope
        raise RuntimeError('nedarim: ' + str(rows.get('Message') or rows)[:200])
    return rows


def parse_date(s):
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(s or ''))
    if not m:
        return None
    import datetime
    d, mo, y = map(int, m.groups())
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def match_subscriber(cfg, con, tx):
    cat = re.escape((cfg.get('payments') or {}).get('category', 'שמרטפון'))
    for field in ('Groupe', 'Comments'):
        m = re.search(cat + r'\s*(\d+)', str(tx.get(field) or ''))
        if m:
            row = con.execute('SELECT id FROM subscribers WHERE id=?', (int(m.group(1)),)).fetchone()
            if row:
                return row['id']
    keva = str(tx.get('KevaId') or '').strip()
    if keva:
        row = con.execute('SELECT id FROM subscribers WHERE keva_id=?', (keva,)).fetchone()
        if row:
            return row['id']
    return None


def apply_transactions(cfg, con, rows, log=print):
    """Returns (new_last_id, applied)."""
    monthly = float((cfg.get('payments') or {}).get('monthly', 8.9))
    annual = float((cfg.get('payments') or {}).get('annual', 82))
    last, applied = None, 0
    for tx in rows:
        try:
            tid = int(tx.get('TransactionId'))
        except (TypeError, ValueError):
            continue
        last = max(last or 0, tid)
        try:
            amount = float(tx.get('Amount') or 0)
        except ValueError:
            continue
        if amount <= 0 or not tx.get('Confirmation'):
            continue  # cancelled, refund or temporary transaction
        sid = match_subscriber(cfg, con, tx)
        if not sid:
            continue
        months = 12 if amount >= (monthly + annual) / 2 else 1
        day = parse_date(tx.get('TransactionTime'))
        keva = str(tx.get('KevaId') or '').strip() or None
        new = store.record_payment(con, sid, source='nedarim', months=months, amount=amount,
                                   confirm=str(tx.get('Confirmation')), transaction_id=str(tid), keva_id=keva,
                                   day=day)
        if new:
            applied += 1
            log('PAYMENT', sid, months, amount, new.isoformat(), flush=True)
    return last, applied


def sync(cfg, db_path, fetch=fetch_history, log=print):
    if not (cfg.get('nedarim') or {}).get('api_password'):
        return False
    con = store.connect(db_path)
    try:
        last = kv_get(con, 'nedarim_last_id') or (cfg['nedarim'].get('start_id') or '')
        for _ in range(MAX_PAGES):
            rows = fetch(cfg, last)
            with con:
                new_last, _ = apply_transactions(cfg, con, rows, log)
                if new_last:
                    last = str(new_last)
                    kv_set(con, 'nedarim_last_id', last)
                kv_set(con, 'nedarim_last_sync', int(time.time()))
            if len(rows) < PAGE:
                break
    finally:
        con.close()
    return True
