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
import urllib.error
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url, fields, hops=3, timeout=60):
    """POST form fields; follow redirects by re-POSTing (urllib would turn them into a GET and drop the body)."""
    data = urllib.parse.urlencode(fields).encode()
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(hops):
        req = urllib.request.Request(url, data=data, method='POST',
                                     headers={'User-Agent': 'curl/8.5.0',
                                              'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read(50 * 1024 * 1024).decode('utf-8', 'replace'), url
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
                url = urllib.parse.urljoin(url, e.headers['Location'])
                continue
            raise
    raise RuntimeError('too many redirects')


def fetch_history(cfg, last_id, debug=None):
    n = cfg['nedarim']
    body, final = _post(URL, {'Action': 'GetHistoryJson', 'MosadId': str(n['mosad']).strip(),
                              'ApiPassword': n['api_password'], 'LastId': last_id or '', 'MaxId': PAGE})
    if debug is not None:
        debug['url'] = final
    try:
        rows = json.loads(body)
    except ValueError:
        raise RuntimeError('nedarim: ' + body[:200])
    if isinstance(rows, dict):  # error envelope
        raise RuntimeError('nedarim: ' + str(rows.get('Message') or rows)[:200])
    return rows


# ---------- standing orders kept frozen: the saved card is charged only when the subscriber presses 1 ----------

def _call(cfg, action, timeout=60, **fields):
    n = cfg['nedarim']
    body, _ = _post(URL, {'Action': action, 'MosadId': str(n['mosad']).strip(), 'ApiPassword': n['api_password'],
                          **{k: str(v) for k, v in fields.items()}}, timeout=timeout)
    try:
        return json.loads(body)
    except ValueError:
        return body.strip()  # some actions answer plain text: 'OK' or the error


def keva_details(cfg, keva_id):
    r = _call(cfg, 'GetKevaId', KevaId=keva_id)
    if not isinstance(r, dict) or not r.get('KevaId'):
        raise RuntimeError('nedarim GetKevaId: ' + str(r.get('Message') if isinstance(r, dict) else r)[:200])
    return r


def freeze_keva(cfg, keva_id):
    r = _call(cfg, 'DisableKeva', KevaId=keva_id)
    if r != 'OK':
        raise RuntimeError('nedarim DisableKeva: ' + str(r)[:200])


def charge_keva(cfg, keva_id, amount, groupe, ajax_id):
    """One charge of the card saved in a (frozen) standing order. Returns the response; Status == 'OK' = charged."""
    r = _call(cfg, 'TashlumBodedNew', timeout=25, KevaId=keva_id, Amount=amount, Currency=1, Tashlumim=1, Groupe=groupe,
              JoinToKevaId='Join', AjaxId=ajax_id)
    return r if isinstance(r, dict) else {'Status': 'Error', 'Message': str(r)[:200]}


def freeze_new_kevas(cfg, con, details=keva_details, freeze=freeze_keva, log=print):
    """Freeze every standing order we know of that is still active, and keep its card's last digits for the menu."""
    rows = con.execute("SELECT id, keva_id FROM subscribers WHERE keva_id IS NOT NULL AND keva_id!='' "
                       "AND keva_frozen IS NULL").fetchall()
    for sid, keva in rows:
        try:
            d = details(cfg, keva)
            if str(d.get('KevaStatus')) == '1':
                freeze(cfg, keva)
            # usable for charges: a card is saved and the order was not deleted (a one-payment order that already
            # ended may be reported as active, frozen or ended - its card is still there)
            frozen = str(d.get('KevaStatus')) != '3' and bool(d.get('KevaLastNum'))
            with con:
                con.execute('UPDATE subscribers SET keva_frozen=?, card_last=? WHERE id=?',
                            (time.time() if frozen else -1, str(d.get('KevaLastNum') or '')[-4:] or None, sid))
                store.audit(con, 'keva_freeze', {'id': sid, 'keva': keva, 'was': d.get('KevaStatus')})
            log('KEVA_FROZEN', sid, keva, 'was', d.get('KevaStatus'), flush=True)
        except Exception as e:
            log('KEVA_FREEZE_ERROR', sid, keva, repr(e)[:200], flush=True)


def name_keva(cfg, keva_id, name, phone, groupe):
    """Put the subscriber's name and phone on the standing order and its charges, so it can be found in Nedarim
    (the phone payment module sends only the card and the category)."""
    r = _call(cfg, 'UpdateKevaNew', KevaId=keva_id, ClientName=name, Phone=phone, Groupe=groupe, UpdateHistory='true')
    if not isinstance(r, dict) or r.get('Result') != 'OK':
        raise RuntimeError('nedarim UpdateKevaNew: ' + str(r.get('Message') if isinstance(r, dict) else r)[:200])


def name_kevas(cfg, con, update=name_keva, log=print):
    rows = con.execute("SELECT * FROM subscribers WHERE keva_id IS NOT NULL AND keva_id!='' "
                       "AND COALESCE(keva_named,'')!=keva_id").fetchall()
    category = (cfg.get('payments') or {}).get('category', 'שמרטפון')
    for sub in rows:
        name = (sub['name'] or '').strip() or f"מנוי שמרטפון {sub['id']}"
        try:
            update(cfg, sub['keva_id'], name.replace('<', '').replace('>', '')[:100], sub['main_phone'],
                   f"{category} {sub['id']}")
            with con:
                con.execute('UPDATE subscribers SET keva_named=? WHERE id=?', (sub['keva_id'], sub['id']))
            log('KEVA_NAMED', sub['id'], sub['keva_id'], flush=True)
        except Exception as e:
            log('KEVA_NAME_ERROR', sub['id'], sub['keva_id'], repr(e)[:200], flush=True)


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


def match_deposit(cfg, con, tx):
    cat = re.escape((cfg.get('payments') or {}).get('category', 'שמרטפון'))
    for field in ('Groupe', 'Comments'):
        m = re.search(cat + r'\s*פיקדון\s*(\d+)', str(tx.get(field) or ''))
        if m:
            row = con.execute('SELECT id FROM subscribers WHERE id=?', (int(m.group(1)),)).fetchone()
            if row:
                return row['id']
    return None


def match_subscriber(cfg, con, tx):
    cat = re.escape((cfg.get('payments') or {}).get('category', 'שמרטפון'))
    for field in ('Groupe', 'Comments'):
        m = re.search(cat + r'\s*(\d+)', str(tx.get(field) or ''))
        if m:
            row = con.execute('SELECT id FROM subscribers WHERE id=?', (int(m.group(1)),)).fetchone()
            if row:
                return row['id']
    keva = str(tx.get('KevaId') or '').strip().lstrip('-')  # '-' = a single charge joined to the order
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
        dep = match_deposit(cfg, con, tx)
        if dep:
            if store.add_credit(con, dep, round(amount * 100), source='nedarim', amount=amount,
                                confirm=str(tx.get('Confirmation')), transaction_id=str(tid)) is not None:
                applied += 1
                log('DEPOSIT', dep, amount, flush=True)
            continue
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
        if (cfg.get('payments') or {}).get('manual_keva'):
            freeze_new_kevas(cfg, con, log=log)
            name_kevas(cfg, con, log=log)
    finally:
        con.close()
    return True
