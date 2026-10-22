"""Password-protected admin pages (Hebrew, RTL, server-rendered)."""
import base64
import csv
import datetime
import email.parser
import email.policy
import hashlib
import hmac
import html
import io
import json
import time
import urllib.parse

import importer
import store

E = html.escape
PAGE_SIZE = 50
_fails = {}  # ip -> [timestamps]


def hash_password(pw, salt=None, rounds=200_000):
    salt = salt or base64.b64encode(hashlib.sha256(str(time.time_ns()).encode()).digest()[:12]).decode()
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), rounds)
    return f'pbkdf2${rounds}${salt}${base64.b64encode(dk).decode()}'


def check_password(pw, stored):
    try:
        _, rounds, salt, _ = stored.split('$', 3)
        return hmac.compare_digest(hash_password(pw, salt, int(rounds)), stored)
    except Exception:
        return False


def csrf_token(cfg):
    return hmac.new(cfg['secret_key'].encode(), b'csrf', 'sha256').hexdigest()[:32]


def authorized(cfg, headers, ip):
    now = time.time()
    fails = [t for t in _fails.get(ip, []) if now - t < 600]
    _fails[ip] = fails
    if len(fails) >= 10:
        return False
    auth = headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        user, _, pw = base64.b64decode(auth[6:]).decode().partition(':')
    except Exception:
        user, pw = '', ''
    if user == 'admin' and check_password(pw, cfg.get('admin_password_hash', '')):
        return True
    fails.append(now)
    return False


# ---------- helpers ----------

STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2433;--muted:#6b7385;--line:#e3e6ec;--brand:#2f6fde;--ok:#1f9d55;--warn:#c27c0e;--bad:#c53030}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:15px}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header b{font-size:18px}header a{color:var(--ink);text-decoration:none}header a:hover{color:var(--brand)}
main{max-width:1100px;margin:16px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;background:var(--card)}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:13px}tr:hover td{background:#fafbfd}
.wrap{overflow-x:auto}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:#fff}
button,.btn{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--brand);background:var(--brand);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
.btn2{background:#fff;color:var(--brand)}.danger{background:var(--bad);border-color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
label{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:13px}
.pill{padding:2px 8px;border-radius:99px;font-size:12px;color:#fff}.active{background:var(--ok)}.trial{background:var(--warn)}.disabled{background:#8a93a6}
.exp{color:var(--bad);font-weight:600}.msg{padding:10px 14px;border-radius:8px;margin-bottom:12px;background:#e8f5ee;color:#14532d}.err{background:#fdecec;color:#7f1d1d}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.muted{color:var(--muted)}
"""


def page(title, body, msg=None, err=None):
    m = ''
    if msg:
        m = f'<div class="msg">{E(msg)}</div>'
    if err:
        m += f'<div class="msg err">{E(err)}</div>'
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} · שמרטפון</title>
<style>{STYLE}</style></head><body><header><b>שמרטפון · ניהול</b>
<a href="/admin">מנויים</a><a href="/admin/new">מנוי חדש</a><a href="/admin/activity">פעילות</a>
<a href="/admin/import">ייבוא</a><a href="/admin/export.csv">ייצוא CSV</a></header>
<main>{m}{body}</main></body></html>"""


def hidden(cfg):
    return f'<input type="hidden" name="csrf" value="{csrf_token(cfg)}">'


def fmt_ts(ts):
    if not ts:
        return ''
    return datetime.datetime.fromtimestamp(ts).strftime('%d/%m/%Y %H:%M')


def fmt_date(d):
    if not d:
        return ''
    try:
        return datetime.date.fromisoformat(d).strftime('%d/%m/%Y')
    except ValueError:
        return d


def options(mapping, current):
    return ''.join(f'<option value="{k}"{" selected" if k == current else ""}>{E(v)}</option>'
                   for k, v in mapping.items())


def sub_form(cfg, s=None):
    s = s or {}
    g = lambda k: E(str(s[k])) if s and s[k] is not None else ''
    return f"""<div class="grid">
<label>שם<input name="name" value="{g('name')}"></label>
<label>טלפון ראשי<input name="main_phone" value="{g('main_phone')}" required inputmode="tel"></label>
<label>סוג מנוי<select name="plan">{options(store.PLANS, s['plan'] if s else 'monthly')}</select></label>
<label>סטטוס<select name="status">{options(store.STATUSES, s['status'] if s else 'active')}</select></label>
<label>סכום לחיוב<input name="amount" value="{g('amount')}" inputmode="decimal"></label>
<label>תוקף עד<input type="date" name="expires" value="{g('expires')}"></label>
<label>חיוב הבא<input type="date" name="next_charge" value="{g('next_charge')}"></label>
<label>טלפון להתראות<input name="alert_phone" value="{g('alert_phone')}" inputmode="tel"></label>
</div><label style="margin-top:12px">הערות<textarea name="notes" rows="2">{g('notes')}</textarea></label>"""


# ---------- pages ----------

def list_page(cfg, con, q, day):
    term = q.get('q', [''])[0].strip()
    status = q.get('status', [''])[0]
    pg = max(1, int(q.get('p', ['1'])[0] or 1))
    where, args = [], []
    if term:
        digits = store.norm_phone(term)
        where.append('(s.name LIKE ? OR s.main_phone LIKE ? OR s.id IN '
                     '(SELECT subscriber_id FROM phones WHERE phone LIKE ?) OR s.legacy_id=?)')
        args += [f'%{term}%', f'%{digits or term}%', f'%{digits or term}%', term]
    if status == 'expired':
        where.append("s.status IN ('active','trial') AND s.plan!='free' AND s.expires IS NOT NULL AND s.expires<?")
        args.append(day.isoformat())
    elif status in store.STATUSES:
        where.append('s.status=?')
        args.append(status)
    w = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = con.execute(f'SELECT COUNT(*) FROM subscribers s {w}', args).fetchone()[0]
    rows = con.execute(f'SELECT s.*, (SELECT COUNT(*) FROM phones p WHERE p.subscriber_id=s.id) AS nph '
                       f'FROM subscribers s {w} ORDER BY s.id DESC LIMIT ? OFFSET ?',
                       args + [PAGE_SIZE, (pg - 1) * PAGE_SIZE]).fetchall()
    counts = dict(con.execute('SELECT status, COUNT(*) FROM subscribers GROUP BY status').fetchall())
    trs = []
    for r in rows:
        expired = r['expires'] and r['expires'] < day.isoformat() and r['plan'] != 'free'
        trs.append(
            f"<tr><td><a href='/admin/sub/{r['id']}'>{r['id']}</a></td><td>{E(r['name'])}</td>"
            f"<td><a href='/admin/sub/{r['id']}'>{E(r['main_phone'])}</a></td><td>{r['nph'] or ''}</td>"
            f"<td><span class='pill {r['status']}'>{E(store.STATUSES.get(r['status'], r['status']))}</span></td>"
            f"<td>{E(store.PLANS.get(r['plan'], r['plan']))}</td>"
            f"<td class='{'exp' if expired else ''}'>{fmt_date(r['expires'])}</td>"
            f"<td>{E(r['alert_phone'] or '')}</td></tr>")
    stat_opts = {'': 'הכל', **store.STATUSES, 'expired': 'פג תוקף'}
    nav = ''
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        base = urllib.parse.urlencode({'q': term, 'status': status})
        nav = ' '.join(
            f"<a class='btn{'' if i == pg else ' btn2'}' href='/admin?{base}&p={i}'>{i}</a>"
            for i in range(max(1, pg - 5), min(pages, pg + 5) + 1))
    summary = ' · '.join(f'{E(v)}: {counts.get(k, 0)}' for k, v in store.STATUSES.items())
    return page('מנויים', f"""<div class="card"><form class="row" method="get" action="/admin">
<label>חיפוש (שם / טלפון)<input name="q" value="{E(term)}"></label>
<label>סטטוס<select name="status">{options(stat_opts, status)}</select></label>
<button>חיפוש</button><a class="btn btn2" href="/admin/new">+ מנוי חדש</a></form>
<p class="muted">{total} תוצאות · {summary}</p></div>
<div class="wrap"><table><tr><th>#</th><th>שם</th><th>טלפון ראשי</th><th>נוספים</th><th>סטטוס</th><th>סוג</th>
<th>תוקף</th><th>התראות ל</th></tr>{''.join(trs)}</table></div><p>{nav}</p>""")


def new_page(cfg, msg=None, err=None, values=None):
    return page('מנוי חדש', f"""<div class="card"><h2>מנוי חדש</h2>
<form method="post" action="/admin/new">{hidden(cfg)}{sub_form(cfg, values)}
<p class="muted">אם לא מילאת תוקף: ניסיון = {int(cfg.get('trial_days', 7))} ימים, חודשי = חודש, שנתי = שנה, חינם = ללא תוקף.</p>
<button>שמירה</button></form></div>""", msg, err)


def sub_page(cfg, con, sid, msg=None, err=None):
    s = con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
    if not s:
        return None
    phones = con.execute('SELECT * FROM phones WHERE subscriber_id=? ORDER BY id', (sid,)).fetchall()
    ph = ''.join(
        f"<tr><td>{E(p['phone'])}</td><td>{E(p['label'])}</td><td><form method='post' "
        f"action='/admin/sub/{sid}/action'>{hidden(cfg)}<input type='hidden' name='phone_id' value='{p['id']}'>"
        f"<button class='btn2' name='op' value='del_phone'>הסרה</button></form></td></tr>" for p in phones)
    sessions = con.execute('SELECT * FROM sessions WHERE subscriber_id=? ORDER BY started DESC LIMIT 10',
                           (sid,)).fetchall()
    alerts = con.execute('SELECT * FROM alerts WHERE subscriber_id=? ORDER BY id DESC LIMIT 10',
                         (sid,)).fetchall()
    ss = ''.join(f"<tr><td>{fmt_ts(r['started'])}</td><td>{fmt_ts(r['ended'])}</td><td>{E(r['phone'] or '')}</td>"
                 f"<td>{E(r['alert_phone'] or '')}</td><td>{E(r['status'] or '')}</td></tr>" for r in sessions)
    al = ''.join(f"<tr><td>{fmt_ts(r['created'])}</td><td>{E(r['kind'])}</td><td>{E(r['alert_phone'] or '')}</td>"
                 f"<td>{E(r['state'])}</td></tr>" for r in alerts)
    ver = con.execute('SELECT * FROM alert_phones WHERE subscriber_id=? ORDER BY verified DESC', (sid,)).fetchall()
    vr = ''.join(
        f"<tr><td>{E(v['phone'])}</td><td class='muted'>{fmt_ts(v['verified'])} · {E(v['method'])}</td><td><form method='post' "
        f"action='/admin/sub/{sid}/action'>{hidden(cfg)}<input type='hidden' name='phone' value='{E(v['phone'])}'>"
        f"<button class='btn2' name='op' value='del_alert'>הסרה</button></form></td></tr>" for v in ver)
    noise = con.execute('SELECT * FROM noise_events WHERE subscriber_id=? ORDER BY started DESC LIMIT 15', (sid,)).fetchall()
    nz = ''.join(f"<tr><td>{fmt_ts(n['started'])}</td><td>{int(round(n['noise_sec'] or 0))} שנ'</td>"
                 f"<td>{n['peak'] or ''}</td><td>{'יש הקלטה' if n['file_id'] else ''}</td></tr>" for n in noise)
    act = lambda op, label, cls='': (f"<button class='{cls}' name='op' value='{op}'>{label}</button>")
    return page(f"מנוי {sid}", f"""<div class="card"><h2>מנוי #{sid} {E(s['name'])}</h2>
<form method="post" action="/admin/sub/{sid}">{hidden(cfg)}{sub_form(cfg, s)}<p><button>שמירה</button></p></form></div>
<div class="card"><h3>פעולות</h3><form class="row" method="post" action="/admin/sub/{sid}/action">{hidden(cfg)}
{act('pay_month', 'שולם · הארכה בחודש')}{act('pay_year', 'שולם · הארכה בשנה')}
{act('trial', 'תקופת ניסיון')}{act('enable', 'הפעלה', 'btn2')}{act('disable', 'השבתה', 'btn2')}
{act('delete', 'מחיקה', 'danger')}</form>
<p class="muted">נוצר {fmt_ts(s['created'])} · עודכן {fmt_ts(s['updated'])}{' · מזהה רישומון ' + E(s['legacy_id']) if s['legacy_id'] else ''}</p></div>
<div class="card"><h3>טלפונים מורשים נוספים</h3><table>{ph or '<tr><td class=muted>אין</td></tr>'}</table>
<form class="row" method="post" action="/admin/sub/{sid}/action" style="margin-top:10px">{hidden(cfg)}
<label>טלפון<input name="phone" inputmode="tel"></label><label>תיאור<input name="label"></label>
<button name="op" value="add_phone">הוספה</button></form></div>
<div class="card"><h3>מספרים מאומתים לקבלת התראות</h3><table>{vr or '<tr><td class=muted>אין</td></tr>'}</table>
<form class="row" method="post" action="/admin/sub/{sid}/action" style="margin-top:10px">{hidden(cfg)}
<label>טלפון (אימות ידני על ידי המנהל)<input name="phone" inputmode="tel"></label>
<button name="op" value="add_alert">אימות והוספה</button></form></div>
<div class="card"><h3>אירועי רעש אחרונים</h3><div class="wrap"><table><tr><th>זמן</th><th>משך</th><th>עוצמה</th><th></th></tr>{nz}</table></div></div>
<div class="card"><h3>שיחות אחרונות</h3><div class="wrap"><table><tr><th>התחלה</th><th>סיום</th><th>מטלפון</th><th>התראות ל</th><th>מצב</th></tr>{ss}</table></div></div>
<div class="card"><h3>התראות אחרונות</h3><div class="wrap"><table><tr><th>זמן</th><th>סוג</th><th>אל</th><th>מצב</th></tr>{al}</table></div></div>""",
                msg, err)


def activity_page(cfg, con):
    live = con.execute("SELECT x.*, s.name, s.main_phone FROM sessions x LEFT JOIN subscribers s ON s.id=x.subscriber_id "
                       "WHERE x.ended IS NULL ORDER BY x.started DESC LIMIT 100").fetchall()
    alerts = con.execute("SELECT a.*, s.main_phone FROM alerts a LEFT JOIN subscribers s ON s.id=a.subscriber_id "
                         "ORDER BY a.id DESC LIMIT 100").fetchall()
    msgs = con.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50").fetchall()
    ms = ''.join(f"<tr><td>{fmt_ts(m['created'])}</td><td>{E(m['phone'] or '')}</td>"
                 f"<td>{('<a href=/admin/sub/' + str(m['subscriber_id']) + '>#' + str(m['subscriber_id']) + '</a>') if m['subscriber_id'] else ''}</td>"
                 f"<td>{int(m['duration'] or 0)} שנ'</td><td>{E(m['state'])}</td>"
                 f"<td>{('<a href=/admin/msg/' + str(m['id']) + '.mp3>האזנה</a>') if m['file_id'] else ''}</td></tr>" for m in msgs)
    lv = ''.join(f"<tr><td><a href='/admin/sub/{r['subscriber_id']}'>{E(r['main_phone'] or '')}</a></td>"
                 f"<td>{E(r['phone'] or '')}</td><td>{fmt_ts(r['started'])}</td><td>{E(r['alert_phone'] or '')}</td>"
                 f"<td>{E(r['status'] or '')}</td></tr>" for r in live)
    al = ''.join(f"<tr><td>{fmt_ts(r['created'])}</td><td><a href='/admin/sub/{r['subscriber_id']}'>{E(r['main_phone'] or '')}</a></td>"
                 f"<td>{E(r['kind'])}</td><td>{E(r['alert_phone'] or '')}</td><td>{E(r['state'])}</td>"
                 f"<td class='muted'>{E((r['result'] or '')[:120])}</td></tr>" for r in alerts)
    return page('פעילות', f"""<div class="card"><h3>ניטורים פתוחים</h3><div class="wrap"><table><tr><th>מנוי</th><th>מטלפון</th>
<th>התחלה</th><th>התראות ל</th><th>מצב</th></tr>{lv}</table></div></div>
<div class="card"><h3>הודעות לשירות הלקוחות</h3><div class="wrap"><table><tr><th>זמן</th><th>מטלפון</th><th>מנוי</th>
<th>אורך</th><th>מצב</th><th></th></tr>{ms}</table></div></div>
<div class="card"><h3>התראות אחרונות</h3><div class="wrap"><table><tr><th>זמן</th><th>מנוי</th><th>סוג</th><th>אל</th>
<th>מצב</th><th>תשובה</th></tr>{al}</table></div></div>""")


def import_page(cfg, msg=None, err=None):
    return page('ייבוא', f"""<div class="card"><h2>ייבוא מרישומון</h2>
<p>ייצא מרישומון לאקסל וטען כאן. קודם טבלת <b>מנויים</b>, אחר כך טבלת <b>מספרי משנה</b>.
עמודות של כרטיסי אשראי לא נקראות ולא נשמרות. ייבוא חוזר מעדכן מנויים קיימים ולא יוצר כפילויות.</p>
<form method="post" action="/admin/import" enctype="multipart/form-data" class="row">{hidden(cfg)}
<label>סוג הקובץ<select name="kind"><option value="subs">מנויים</option><option value="phones">מספרי משנה</option></select></label>
<label>קובץ (xlsx / xls / csv)<input type="file" name="file" required></label><button>ייבוא</button></form></div>""",
                msg, err)


def export_csv(con):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['id', 'name', 'main_phone', 'extra_phones', 'plan', 'status', 'amount', 'expires',
                'next_charge', 'alert_phone', 'notes', 'legacy_id'])
    for r in con.execute('SELECT * FROM subscribers ORDER BY id'):
        extra = ' '.join(p[0] for p in con.execute('SELECT phone FROM phones WHERE subscriber_id=?', (r['id'],)))
        w.writerow([r['id'], r['name'], r['main_phone'], extra, r['plan'], r['status'], r['amount'] or '',
                    r['expires'] or '', r['next_charge'] or '', r['alert_phone'] or '', r['notes'], r['legacy_id'] or ''])
    return ('﻿' + out.getvalue()).encode('utf-8')


# ---------- form handling ----------

def parse_form(headers, body):
    ctype = headers.get('Content-Type', '')
    if ctype.startswith('multipart/form-data'):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b'Content-Type: ' + ctype.encode() + b'\r\n\r\n' + body)
        fields, files = {}, {}
        for part in msg.iter_parts():
            name = part.get_param('name', header='content-disposition')
            fname = part.get_param('filename', header='content-disposition')
            payload = part.get_payload(decode=True) or b''
            if fname is not None:
                files[name] = (fname, payload)
            else:
                fields[name] = payload.decode('utf-8', 'replace')
        return fields, files
    return {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode('utf-8', 'replace'),
                                                        keep_blank_values=True).items()}, {}


def form_fields(f):
    amount = (f.get('amount') or '').strip()
    try:
        amount = float(amount) if amount else None
    except ValueError:
        raise ValueError('סכום לא תקין')
    plan = f.get('plan') if f.get('plan') in store.PLANS else 'monthly'
    status = f.get('status') if f.get('status') in store.STATUSES else 'active'
    return dict(name=(f.get('name') or '').strip(), main_phone=f.get('main_phone', ''), plan=plan, status=status,
                amount=amount, expires=store.parse_date(f.get('expires')),
                next_charge=store.parse_date(f.get('next_charge')),
                alert_phone=(f.get('alert_phone') or '').strip() or None, notes=(f.get('notes') or '').strip())


def default_expiry(cfg, plan, day):
    if plan == 'trial':
        return (day + datetime.timedelta(days=int(cfg.get('trial_days', 7)))).isoformat()
    if plan == 'monthly':
        return store.add_months(day, 1).isoformat()
    if plan == 'annual':
        return store.add_months(day, 12).isoformat()
    return None


def handle(cfg, con, method, path, query, headers, body, day):
    """Return (status, content_type, bytes, extra_headers)."""
    html_ok = lambda s: (200, 'text/html; charset=utf-8', s.encode(), {})
    redirect = lambda loc: (303, 'text/plain', b'', {'Location': loc})
    q = urllib.parse.parse_qs(query)
    parts = [p for p in path.split('/') if p][1:]  # strip 'admin'

    if method == 'POST':
        f, files = parse_form(headers, body)
        if not hmac.compare_digest(f.get('csrf', ''), csrf_token(cfg)):
            return 403, 'text/plain', b'bad csrf', {}
    if not parts:
        return html_ok(list_page(cfg, con, q, day))
    if parts == ['new']:
        if method == 'GET':
            return html_ok(new_page(cfg))
        try:
            vals = form_fields(f)
            if not vals['expires']:
                vals['expires'] = default_expiry(cfg, vals['plan'], day)
            if vals['plan'] == 'trial':
                vals['status'] = 'trial'
            with con:
                sid = store.create_subscriber(con, **vals)
        except ValueError as e:
            return html_ok(new_page(cfg, err=str(e)))
        return redirect(f'/admin/sub/{sid}?m=created')
    if parts == ['activity']:
        return html_ok(activity_page(cfg, con))
    if len(parts) == 2 and parts[0] == 'msg' and parts[1].endswith('.mp3') and parts[1][:-4].isdigit():
        m = con.execute('SELECT * FROM messages WHERE id=?', (int(parts[1][:-4]),)).fetchone()
        if not m or not m['file_id']:
            return 404, 'text/plain', b'not found', {}
        import core
        try:
            audio = core.fetch_recording(cfg, m['file_id'])
        except Exception as e:
            return 502, 'text/plain; charset=utf-8', f'ההורדה נכשלה: {e}'.encode(), {}
        return 200, 'audio/mpeg', audio, {}
    if parts == ['export.csv']:
        return 200, 'text/csv; charset=utf-8', export_csv(con), {
            'Content-Disposition': f'attachment; filename="shmartphone-{day.isoformat()}.csv"'}
    if parts == ['import']:
        if method == 'GET':
            return html_ok(import_page(cfg))
        fname, data = files.get('file', ('', b''))
        if not data:
            return html_ok(import_page(cfg, err='לא נבחר קובץ'))
        try:
            rows = importer.read_table(data, fname)
            with con:
                if f.get('kind') == 'phones':
                    st = importer.import_phones(con, rows, int(cfg.get('max_extra_phones', 5)))
                    msg = f"נוספו {st['created']} טלפונים, {st['existing']} כבר קיימים, {st['skipped']} דולגו"
                else:
                    st = importer.import_subscribers(con, rows)
                    msg = f"נוצרו {st['created']} מנויים, עודכנו {st['updated']}, דולגו {st['skipped']}"
                store.audit(con, 'import', {'kind': f.get('kind'), 'file': fname, **{k: v for k, v in st.items() if k != 'errors'}})
        except Exception as e:
            return html_ok(import_page(cfg, err=f'הייבוא נכשל: {e}'))
        errs = st['errors']
        return html_ok(import_page(cfg, msg=msg, err=(f'{len(errs)} שגיאות: ' + ' | '.join(errs[:30])) if errs else None))
    if parts[0] == 'sub' and len(parts) >= 2 and parts[1].isdigit():
        sid = int(parts[1])
        if not con.execute('SELECT 1 FROM subscribers WHERE id=?', (sid,)).fetchone():
            return 404, 'text/plain', 'not found'.encode(), {}
        if method == 'GET':
            m = {'created': 'המנוי נוצר', 'saved': 'נשמר', 'done': 'בוצע'}.get(q.get('m', [''])[0])
            return html_ok(sub_page(cfg, con, sid, msg=m))
        try:
            with con:
                if len(parts) == 2:
                    store.update_subscriber(con, sid, **form_fields(f))
                    return redirect(f'/admin/sub/{sid}?m=saved')
                op = f.get('op')
                if op == 'pay_month':
                    store.extend(con, sid, 1, day)
                elif op == 'pay_year':
                    store.extend(con, sid, 12, day)
                elif op == 'trial':
                    store.update_subscriber(con, sid, status='trial', plan='trial',
                                            expires=default_expiry(cfg, 'trial', day))
                elif op == 'enable':
                    store.update_subscriber(con, sid, status='active')
                elif op == 'disable':
                    store.update_subscriber(con, sid, status='disabled')
                elif op == 'delete':
                    store.delete_subscriber(con, sid)
                    return redirect('/admin')
                elif op == 'add_phone':
                    store.add_phone(con, sid, f.get('phone', ''), f.get('label', ''),
                                    int(cfg.get('max_extra_phones', 5)))
                elif op == 'del_phone':
                    store.remove_phone(con, sid, int(f.get('phone_id', 0)))
                elif op == 'add_alert':
                    p = store.norm_phone(f.get('phone', ''))
                    if not store.valid_phone(p):
                        raise ValueError('מספר טלפון לא תקין')
                    store.add_verified(con, sid, p, 'admin')
                elif op == 'del_alert':
                    store.remove_verified(con, sid, store.norm_phone(f.get('phone', '')))
                else:
                    return 400, 'text/plain', b'bad op', {}
        except ValueError as e:
            return html_ok(sub_page(cfg, con, sid, err=str(e)))
        return redirect(f'/admin/sub/{sid}?m=done')
    return 404, 'text/plain', b'not found', {}
