"""Daily summary email: what happened in the system over the last 24 hours."""
import datetime
import time

import store

ALERT_NAMES = {'noise': 'רעש', 'disconnected': 'ניתוק', 'balance': 'פיקדון נגמר', 'reconnect': 'חיוג חוזר',
               'verify': 'אימות מספר', 'message': 'צינתוק על הודעה'}
STATE_NAMES = {'sent': 'נשלחו', 'emailed': 'נשלחו למייל','answered': 'נענו', 'heard': 'נשמעו', 'skipped': 'דולגו', 'failed': 'נכשלו',
               'unknown': 'לא ידוע', 'pending': 'ממתינות', 'sending': 'בשליחה'}
MODE_NAMES = {'activate': 'הפעלה', 'listen': 'האזנה', 'menu': 'תפריט', 'account': 'מנוי', 'alert': 'שיחת התראה',
              'history': 'רעשים שזוהו', 'info': 'הסבר', 'message': 'הודעה', 'voicemail': 'תא קולי',
              'remote': 'שליטה מרחוק', 'stop': 'הפסקת התראות', 'verify': 'אימות', 'phones': 'מספרים',
              'parent': 'הורה'}
NOISY_ROOM = 10  # alerts in a day from one subscriber that are worth pointing out


def _fmt_time(ts):
    return time.strftime('%d/%m %H:%M', time.localtime(ts))


def build(con, now=None, day=None):
    """Return (subject, body) for the 24 hours that end at `now`."""
    now = now or time.time()
    since = now - 86400
    day = day or datetime.date.fromtimestamp(now)
    out = []

    def section(title):
        out.append('')
        out.append(title)
        out.append('-' * len(title))

    calls = con.execute('SELECT COUNT(*), COUNT(DISTINCT phone) FROM sessions WHERE started>=?', (since,)).fetchone()
    by_mode = con.execute("SELECT COALESCE(mode,''), COUNT(*) FROM sessions WHERE started>=? GROUP BY 1 "
                          "ORDER BY 2 DESC", (since,)).fetchall()
    mon = con.execute('SELECT COUNT(*), COUNT(DISTINCT subscriber_id), '
                      'COALESCE(SUM(MIN(COALESCE(ended,?),?)-MAX(mon_start,?)),0) FROM sessions '
                      'WHERE mon_start IS NOT NULL AND mon_start<? AND COALESCE(ended,?)>=?',
                      (now, now, since, now, now, since)).fetchone()
    live = con.execute("SELECT COUNT(*) FROM sessions WHERE status='monitoring' AND ended IS NULL").fetchone()[0]
    section('שיחות')
    out.append(f'שיחות נכנסות: {calls[0]} (מ-{calls[1]} מספרים שונים)')
    if by_mode:
        out.append('לפי שלוחה: ' + ', '.join(f'{MODE_NAMES.get(m, m or "ללא")} {n}' for m, n in by_mode))
    out.append(f'ניטורים: {mon[0]} של {mon[1]} מנויים, סה"כ {int(mon[2] // 60)} דקות')
    out.append(f'ניטורים פעילים עכשיו: {live}')

    rows = con.execute('SELECT kind, state, COUNT(*) FROM alerts WHERE created>=? GROUP BY 1, 2 ORDER BY 1, 3 DESC',
                       (since,)).fetchall()
    section('שיחות יוצאות והתראות')
    if rows:
        kinds = {}
        for kind, state, n in rows:
            kinds.setdefault(kind, []).append(f'{STATE_NAMES.get(state, state)} {n}')
        for kind, parts in kinds.items():
            out.append(f'{ALERT_NAMES.get(kind, kind)}: ' + ', '.join(parts))
        placed = sum(n for _, state, n in rows if state in ('sent', 'answered', 'heard', 'unknown'))
        out.append(f'סה"כ שיחות שיצאו: {placed} (כ-1 ₪ כל אחת בטכנוליין)')
    else:
        out.append('לא יצאו שיחות')
    charged = con.execute('SELECT COALESCE(SUM(charged),0) FROM alerts WHERE created>=?', (since,)).fetchone()[0]
    if charged:
        out.append(f'חויב מפיקדונות המנויים: {charged / 100:.2f} ₪')
    noisy = con.execute("SELECT a.subscriber_id, s.main_phone, COUNT(*) n FROM alerts a "
                        "LEFT JOIN subscribers s ON s.id=a.subscriber_id WHERE a.created>=? AND a.kind='noise' "
                        "AND a.state!='skipped' GROUP BY 1 HAVING n>=? ORDER BY n DESC", (since, NOISY_ROOM)).fetchall()
    for sid, phone, n in noisy:
        out.append(f'שימו לב: מנוי #{sid} ({phone or "?"}) קיבל {n} התראות רעש')

    pays = con.execute('SELECT p.created, p.subscriber_id, s.main_phone, p.plan, p.amount, p.source FROM payments p '
                       'LEFT JOIN subscribers s ON s.id=p.subscriber_id WHERE p.created>=? ORDER BY p.created',
                       (since,)).fetchall()
    section('תשלומים')
    if pays:
        for ts, sid, phone, plan, amount, source in pays:
            out.append(f'{_fmt_time(ts)}  #{sid} {phone or ""}  {store.PLANS.get(plan, "פיקדון" if plan == "deposit" else plan or "")}'
                       f'  {amount or 0:g} ₪  ({source})')
        out.append(f'סה"כ: {sum(p[4] or 0 for p in pays):g} ₪ ב-{len(pays)} תשלומים')
    else:
        out.append('לא היו תשלומים')

    section('מנויים')
    new = con.execute('SELECT status, COUNT(*) FROM subscribers WHERE created>=? GROUP BY 1', (since,)).fetchall()
    out.append('הצטרפו היום: ' + (', '.join(f'{store.STATUSES.get(s, s)} {n}' for s, n in new) if new else '0'))
    totals = con.execute('SELECT status, COUNT(*) FROM subscribers GROUP BY 1 ORDER BY 2 DESC').fetchall()
    out.append('סה"כ במערכת: ' + ', '.join(f'{store.STATUSES.get(s, s)} {n}' for s, n in totals))
    soon = (day + datetime.timedelta(days=3)).isoformat()
    ending = con.execute("SELECT COUNT(*) FROM subscribers WHERE status IN ('active','trial') AND plan!='free' "
                         "AND expires IS NOT NULL AND expires>=? AND expires<=?", (day.isoformat(), soon)).fetchone()[0]
    out.append(f'תוקף שנגמר ב-3 הימים הקרובים: {ending}')
    low = con.execute("SELECT COUNT(*) FROM subscribers WHERE plan='prepaid' AND status='active' "
                      "AND COALESCE(balance,0)<=0").fetchone()[0]
    if low:
        out.append(f'מנויי פיקדון פעילים בלי יתרה: {low}')

    msgs = con.execute('SELECT state, COUNT(*) FROM messages WHERE created>=? GROUP BY 1', (since,)).fetchall()
    section('הודעות בשלוחה 9')
    out.append(', '.join(f'{STATE_NAMES.get(s, s)} {n}' for s, n in msgs) if msgs else 'לא היו הודעות')

    problems = []
    failed = con.execute("SELECT kind, COUNT(*) FROM alerts WHERE created>=? AND state='failed' GROUP BY 1",
                         (since,)).fetchall()
    for kind, n in failed:
        problems.append(f'{n} שיחות {ALERT_NAMES.get(kind, kind)} נכשלו')
    stuck = con.execute("SELECT COUNT(*) FROM alerts WHERE state IN ('pending','sending') AND created<?",
                        (now - 900,)).fetchone()[0]
    if stuck:
        problems.append(f'{stuck} שיחות יוצאות תקועות יותר מרבע שעה')
    mfail = con.execute("SELECT COUNT(*) FROM messages WHERE created>=? AND state='failed'", (since,)).fetchone()[0]
    if mfail:
        problems.append(f'{mfail} הודעות לא נשלחו למייל')
    long_calls = con.execute('SELECT COUNT(*) FROM sessions WHERE mon_start>=? AND ended-mon_start>=7200',
                             (since,)).fetchone()[0]
    if long_calls:
        problems.append(f'{long_calls} ניטורים נמשכו שעתיים ויותר (ייתכן שנחתכו)')
    section('תקלות')
    out.extend(problems or ['לא נמצאו תקלות'])

    subject = f'שמרטפון: סיכום יומי {day.strftime("%d/%m/%Y")}'
    head = f'סיכום פעילות מ-{_fmt_time(since)} עד {_fmt_time(now)}'
    return subject, head + '\n' + '\n'.join(out) + '\n'


def due(cfg, con, now=None):
    """True once a day, after the configured hour (local time), if no report went out today."""
    if not cfg.get('smtp') or cfg.get('daily_report', True) is False:
        return False
    now = now or time.time()
    t = time.localtime(now)
    if t.tm_hour < int(cfg.get('daily_report_hour', 22)):
        return False
    row = con.execute("SELECT value FROM kv WHERE key='daily_report_sent'").fetchone()
    return not row or row[0] != time.strftime('%Y-%m-%d', t)


def mark_sent(con, now=None):
    with con:
        con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('daily_report_sent',?)",
                    (time.strftime('%Y-%m-%d', time.localtime(now or time.time())),))


def run(cfg, db_path, mail, now=None, force=False, log=print):
    """Send the daily report if it is due (or `force`). Returns True when one was sent."""
    con = store.connect(db_path)
    try:
        if not force and not due(cfg, con, now):
            return False
        subject, body = build(con, now)
        mail(cfg, subject, body)
        mark_sent(con, now)
        log('DAILY_REPORT sent', flush=True)
        return True
    finally:
        con.close()
