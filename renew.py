"""Automatic renewal from the card saved in the subscriber's (frozen) standing order.

Nedarim Plus can only repeat a standing order monthly, and charges on its own date, so the order is kept frozen
and the server charges it itself (TashlumBodedNew), monthly or yearly, on the subscriber's paid-until date. The day
before, the subscriber gets a reminder call saying how much will be charged and how to cancel the renewal.
"""
import datetime
import json
import time

import nedarim
import store

REMINDER = ('שלום, כאן שְׁמַרְטַפוֹן. מחר יחודש המנוי שלכם ב-{amount} שקלים, מהכרטיס שמסתיים בספרות {last}. '
            'אם אינכם רוצים להמשיך, אפשר לבטל את החידוש עד מחר: התקשרו לשְׁמַרְטַפוֹן, '
            'הקישו 4 לניהול המנוי, ואז 8. תודה.')
LATE_DAYS = 3  # a renewal missed by more than this (e.g. the server was down) is not charged: the subscriber pays


def _due(con, day):
    """Subscribers whose renewal is on: a paid plan, a frozen standing order with a known card, not cancelled."""
    return con.execute("SELECT * FROM subscribers WHERE status='active' AND plan IN ('monthly','annual') "
                       "AND keva_id IS NOT NULL AND keva_frozen>0 AND card_last IS NOT NULL AND renew_off IS NULL "
                       "AND expires IS NOT NULL AND expires>=? AND expires<=?",
                       ((day - datetime.timedelta(days=LATE_DAYS)).isoformat(),
                        (day + datetime.timedelta(days=1)).isoformat())).fetchall()


def amount_for(cfg, sub):
    p = cfg.get('payments') or {}
    return float(p.get('annual', 82)) if sub['plan'] == 'annual' else float(p.get('monthly', 8.9))


def money(x):
    return ('%.2f' % x).rstrip('0').rstrip('.')


def run(cfg, db_path, charge=nedarim.charge_keva, now=None, log=print):
    """Queue tomorrow's reminders and charge today's renewals. Safe to run often: each step happens once."""
    if not (cfg.get('payments') or {}).get('auto_renew'):
        return
    now = now or time.time()
    day = datetime.date.fromtimestamp(now)
    if time.localtime(now).tm_hour < int((cfg.get('payments') or {}).get('renew_hour', 10)):
        return  # reminders and charges only in daytime
    con = store.connect(db_path)
    try:
        for sub in _due(con, day):
            key = f"renew:{sub['id']}:{sub['expires']}"
            amount = amount_for(cfg, sub)
            reminder = con.execute('SELECT created FROM alerts WHERE unique_key=?', (key,)).fetchone()
            if not reminder:  # the day before (or now, if that day was missed): the charge waits a day after it
                with con:
                    con.execute('INSERT INTO alerts(unique_key,subscriber_id,kind,alert_phone,created,state,payload) '
                                'VALUES(?,?,?,?,?,?,?)',
                                (key, sub['id'], 'renew_notice', sub['main_phone'], now, 'pending',
                                 json.dumps({'text': REMINDER.format(amount=money(amount),
                                                                     last=' '.join(sub['card_last']))},
                                            ensure_ascii=False)))
                    store.audit(con, 'renew_notice', {'id': sub['id'], 'expires': sub['expires'], 'amount': amount})
                continue
            if sub['expires'] > day.isoformat() or now - reminder[0] < 20 * 3600:
                continue  # not the renewal day yet, or the reminder was too recent to give a day to cancel
            if con.execute('SELECT 1 FROM kv WHERE key=?', (key,)).fetchone():
                continue  # already tried for this period (charged, declined or no answer): never twice
            with con:
                con.execute('INSERT INTO kv(key,value) VALUES(?,?)', (key, 'trying'))
            category = (cfg.get('payments') or {}).get('category', 'שמרטפון')
            try:
                r = charge(cfg, sub['keva_id'], amount, f"{category} {sub['id']}", f"auto-{sub['id']}-{sub['expires']}")
            except Exception as e:  # no answer: maybe charged; the Nedarim sync extends the subscriber if so
                r = {'Status': 'Unknown', 'Message': repr(e)[:150]}
            with con:
                con.execute('UPDATE kv SET value=? WHERE key=?', (str(r.get('Status')), key))
                if r.get('Status') == 'OK':
                    new = store.record_payment(con, sub['id'], source='keva', months=12 if sub['plan'] == 'annual' else 1,
                                               amount=amount, confirm=str(r.get('Confirmation') or '') or None,
                                               transaction_id=str(r.get('TransactionId') or r.get('ID') or '') or None,
                                               day=day)
                    store.audit(con, 'renew_charged', {'id': sub['id'], 'amount': amount,
                                                       'expires': new.isoformat() if new else None})
                else:
                    store.audit(con, 'renew_failed', {'id': sub['id'], 'amount': amount,
                                                      'status': r.get('Status'), 'message': str(r.get('Message'))[:150]})
            log('RENEW', sub['id'], amount, r.get('Status'), str(r.get('Message') or '')[:80], flush=True)
    finally:
        con.close()
