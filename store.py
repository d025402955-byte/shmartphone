"""Subscriber and event storage (SQLite, stdlib only)."""
import calendar
import datetime
import json
import re
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers(
  id INTEGER PRIMARY KEY,
  legacy_id TEXT,
  name TEXT NOT NULL DEFAULT '',
  main_phone TEXT NOT NULL UNIQUE,
  plan TEXT NOT NULL DEFAULT 'monthly',
  status TEXT NOT NULL DEFAULT 'active',
  amount REAL,
  expires TEXT,
  next_charge TEXT,
  alert_phone TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phones(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  phone TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  call_id TEXT PRIMARY KEY,
  subscriber_id INTEGER,
  phone TEXT,
  room_id TEXT,
  alert_phone TEXT,
  tries INTEGER NOT NULL DEFAULT 0,
  started REAL,
  ended REAL,
  status TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  unique_key TEXT UNIQUE,
  subscriber_id INTEGER,
  call_id TEXT,
  kind TEXT,
  alert_phone TEXT,
  created REAL,
  state TEXT NOT NULL,
  result TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  ts REAL,
  action TEXT,
  details TEXT
);
CREATE TABLE IF NOT EXISTS alert_phones(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  phone TEXT NOT NULL,
  verified REAL NOT NULL,
  method TEXT NOT NULL DEFAULT '',
  UNIQUE(subscriber_id, phone)
);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  phone TEXT NOT NULL,
  code TEXT NOT NULL,
  created REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  done REAL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER,
  phone TEXT,
  call_id TEXT,
  file_id TEXT,
  file_name TEXT,
  duration REAL,
  created REAL NOT NULL,
  state TEXT NOT NULL,
  result TEXT
);
CREATE TABLE IF NOT EXISTS noise_events(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  call_id TEXT,
  event_id TEXT UNIQUE,
  started REAL,
  ended REAL,
  noise_sec REAL,
  peak INTEGER,
  file_id TEXT,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS noise_sub ON noise_events(subscriber_id, started);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY,
  subscriber_id INTEGER REFERENCES subscribers(id) ON DELETE SET NULL,
  source TEXT NOT NULL,
  plan TEXT,
  amount REAL,
  confirm TEXT,
  transaction_id TEXT UNIQUE,
  keva_id TEXT,
  paid_until TEXT,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_confirm ON payments(confirm);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS alerts_state ON alerts(state);
CREATE INDEX IF NOT EXISTS subs_legacy ON subscribers(legacy_id);
CREATE INDEX IF NOT EXISTS alert_phones_phone ON alert_phones(phone);
CREATE INDEX IF NOT EXISTS messages_state ON messages(state);
"""

MIGRATIONS = {
    'sessions': [('step', 'TEXT'), ('mode', 'TEXT'), ('data', 'TEXT'), ('mon_start', 'REAL'), ('charged', 'INTEGER')],
    'alerts': [('payload', 'TEXT'), ('not_before', 'REAL'), ('heard', 'REAL'), ('charged', 'INTEGER')],
    'noise_events': [('file_name', 'TEXT'), ('folder', 'TEXT'), ('file_url', 'TEXT')],
    'subscribers': [('billing', 'TEXT'), ('keva_id', 'TEXT'), ('balance', 'INTEGER'), ('alert_mode', 'TEXT'),
                    ('alert_extra', 'TEXT'), ('alert_backup', 'TEXT')],
}

GRACE_DAYS = 5  # a paid subscription keeps working this many days after its paid-until date

PLANS = {'monthly': 'חודשי', 'annual': 'שנתי', 'prepaid': 'פיקדון', 'free': 'חינם', 'trial': 'ניסיון'}
STATUSES = {'active': 'פעיל', 'trial': 'תקופת ניסיון', 'disabled': 'מושבת', 'cancelled': 'מבוטל'}


def connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=15000')
    return con


def init(path):
    con = connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        for name, typ in cols:
            if name not in have:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {typ}')
    con.commit()
    con.close()


# ---------- verified alert phones ----------

def verified_phones(con, sid):
    return [r['phone'] for r in con.execute(
        'SELECT phone FROM alert_phones WHERE subscriber_id=? ORDER BY verified DESC', (sid,))]


def add_verified(con, sid, phone, method):
    con.execute('INSERT INTO alert_phones(subscriber_id,phone,verified,method) VALUES(?,?,?,?) '
                'ON CONFLICT(subscriber_id,phone) DO UPDATE SET verified=excluded.verified, method=excluded.method',
                (sid, phone, time.time(), method))
    audit(con, 'verify_phone', {'id': sid, 'phone': phone, 'method': method})


def remove_verified(con, sid, phone):
    con.execute('DELETE FROM alert_phones WHERE subscriber_id=? AND phone=?', (sid, phone))
    audit(con, 'unverify_phone', {'id': sid, 'phone': phone})


# ---------- phones and dates ----------

def norm_phone(value):
    """Normalize an Israeli phone number to local digits (05xxxxxxxx / 0xxxxxxxx)."""
    s = str(value or '').strip()
    if re.fullmatch(r'\d+\.0+', s):  # Excel numeric cell like 533157319.0
        s = s.split('.')[0]
    d = ''.join(c for c in s if c.isdigit())
    if d.startswith('972'):
        d = '0' + d[3:]
    if d and not d.startswith('0') and len(d) in (8, 9):
        d = '0' + d  # leading zero lost in a spreadsheet
    return d


def valid_phone(d):
    return bool(d) and d.startswith('0') and len(d) in (9, 10)


def today(tz=None):
    return datetime.datetime.now(tz).date()


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def parse_date(value):
    """Accept YYYY-MM-DD[ hh:mm:ss], DD/MM/YYYY, or an Excel serial number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if 20000 < float(value) < 80000:
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(value)))).isoformat()
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r'\d{5}(\.\d+)?', s):
        return parse_date(float(s))
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        y, mo, dd = map(int, m.groups())
    else:
        m = re.match(r'(\d{1,2})[/.](\d{1,2})[/.](\d{4})', s)
        if not m:
            return None
        dd, mo, y = map(int, m.groups())
    try:
        return datetime.date(y, mo, dd).isoformat()
    except ValueError:
        return None


# ---------- subscribers ----------

def audit(con, action, details):
    con.execute('INSERT INTO audit(ts,action,details) VALUES(?,?,?)',
                (time.time(), action, json.dumps(details, ensure_ascii=False)))


def find_by_phone(con, phone):
    """Return the subscriber row that owns this phone (main or extra), or None."""
    phone = norm_phone(phone)
    if not phone:
        return None
    row = con.execute('SELECT * FROM subscribers WHERE main_phone=?', (phone,)).fetchone()
    if row:
        return row
    return con.execute('SELECT s.* FROM subscribers s JOIN phones p ON p.subscriber_id=s.id '
                       'WHERE p.phone=?', (phone,)).fetchone()


def phone_owner(con, phone):
    """Subscriber id already using this phone anywhere, or None."""
    row = find_by_phone(con, phone)
    return row['id'] if row else None


def authorization(sub, day, enforce_active_expiry=False):
    """(allowed, reason). Active = allowed; trial = allowed until expiry; disabled = not."""
    if sub is None:
        return False, 'unknown'
    status, exp = sub['status'], sub['expires']
    if status in ('disabled', 'cancelled'):
        return False, 'disabled'
    if status == 'trial':
        if exp and exp >= day.isoformat():
            return True, ''
        return False, 'trial_expired'
    if status == 'active' and sub['plan'] == 'prepaid':
        return balance(sub) > 0, 'no_balance'
    if status == 'active':
        if sub['plan'] == 'free' or not exp:
            return True, ''
        billed_here = 'billing' in sub.keys() and sub['billing'] == 'nedarim'
        if not billed_here and not enforce_active_expiry:
            return True, ''  # imported subscribers, still billed by the old system
        limit = (day - datetime.timedelta(days=GRACE_DAYS)).isoformat()
        return (exp >= limit), 'expired'
    return False, 'unknown'


def record_payment(con, sid, *, source, months, amount, confirm=None, transaction_id=None, keva_id=None,
                   day=None):
    """Extend a subscription after a successful charge. Idempotent per transaction / confirmation."""
    day = day or datetime.date.today()
    if transaction_id and con.execute('SELECT 1 FROM payments WHERE transaction_id=?', (transaction_id,)).fetchone():
        return None
    if confirm:
        row = con.execute('SELECT * FROM payments WHERE confirm=? AND subscriber_id=?', (confirm, sid)).fetchone()
        if row:  # the same charge seen twice (phone result + Nedarim sync)
            con.execute('UPDATE payments SET transaction_id=COALESCE(transaction_id,?), keva_id=COALESCE(keva_id,?) '
                        'WHERE id=?', (transaction_id, keva_id, row['id']))
            if keva_id:
                con.execute('UPDATE subscribers SET keva_id=? WHERE id=?', (keva_id, sid))
            return None
    if source == 'nedarim':
        # The phone result sometimes comes without CONFIRM_payment (seen with standing orders): the Nedarim
        # charge is then matched to that phone payment by subscriber, amount and date, never extended twice.
        day_ts = time.mktime(day.timetuple())
        row = con.execute("SELECT * FROM payments WHERE subscriber_id=? AND source='phone' AND transaction_id IS NULL "
                          "AND COALESCE(confirm,'')='' AND ABS(amount-?)<0.01 AND created BETWEEN ? AND ? "
                          "ORDER BY id LIMIT 1", (sid, amount, day_ts - 2 * 86400, day_ts + 3 * 86400)).fetchone()
        if row:
            con.execute('UPDATE payments SET confirm=?, transaction_id=?, keva_id=COALESCE(keva_id,?) WHERE id=?',
                        (confirm, transaction_id, keva_id, row['id']))
            if keva_id:
                con.execute('UPDATE subscribers SET keva_id=? WHERE id=?', (keva_id, sid))
            audit(con, 'payment_linked', {'id': sid, 'payment': row['id'], 'transaction': transaction_id})
            return None
    new = extend(con, sid, months, day)
    plan = 'annual' if months >= 12 else 'monthly'
    con.execute('UPDATE subscribers SET billing=?, amount=?, keva_id=COALESCE(?,keva_id) WHERE id=?',
                ('nedarim', amount, keva_id, sid))
    con.execute('INSERT INTO payments(subscriber_id,source,plan,amount,confirm,transaction_id,keva_id,paid_until,created) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (sid, source, plan, amount, confirm, transaction_id, keva_id, new.isoformat(), time.time()))
    return new


# ---------- prepaid deposit (balance in agorot) ----------

def balance(sub):
    return int(sub['balance'] or 0) if 'balance' in sub.keys() else 0


def add_credit(con, sid, agorot, *, source, amount, confirm=None, transaction_id=None, note=''):
    """Add to a subscriber's deposit and switch them to the prepaid plan.

    Idempotent per transaction / confirmation (phone result + Nedarim sync). Returns the new balance or None."""
    agorot = int(agorot)
    if transaction_id and con.execute('SELECT 1 FROM payments WHERE transaction_id=?', (transaction_id,)).fetchone():
        return None
    if confirm:
        row = con.execute('SELECT * FROM payments WHERE confirm=? AND subscriber_id=?', (confirm, sid)).fetchone()
        if row:
            con.execute('UPDATE payments SET transaction_id=COALESCE(transaction_id,?) WHERE id=?',
                        (transaction_id, row['id']))
            return None
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
    keep_plan = sub['plan'] in ('monthly', 'annual', 'free') and authorization(sub, datetime.date.today())[0]
    if keep_plan:  # an active subscription keeps its plan; the deposit pays for extras (reconnect calls)
        con.execute('UPDATE subscribers SET balance=COALESCE(balance,0)+?, updated=? WHERE id=?',
                    (agorot, time.time(), sid))
    else:
        con.execute("UPDATE subscribers SET balance=COALESCE(balance,0)+?, plan='prepaid', status='active', "
                    "billing=COALESCE(billing,?), updated=? WHERE id=?",
                    (agorot, 'nedarim' if source != 'admin' else 'admin', time.time(), sid))
    con.execute('INSERT INTO payments(subscriber_id,source,plan,amount,confirm,transaction_id,created) '
                'VALUES(?,?,?,?,?,?,?)', (sid, source, 'deposit', amount, confirm, transaction_id, time.time()))
    new = con.execute('SELECT balance FROM subscribers WHERE id=?', (sid,)).fetchone()[0]
    audit(con, 'deposit', {'id': sid, 'agorot': agorot, 'source': source, 'balance': new, 'note': note})
    return new


ALERT_MODES = {'ping': 'צינתוק חינם', 'voice': 'הודעה קצרה חינם', 'message': 'הודעה קולית בתשלום',
               'call': 'שיחה מהמערכת'}


def charge_alert(con, sid, billing, alert_id):
    """Pass Technoline's cost of a paid alert call on to the subscriber's deposit (once per alert)."""
    try:
        agorot = int(round(float(billing) * 100))
    except (TypeError, ValueError):
        return 0
    if agorot <= 0 or not sid:
        return 0
    cur = con.execute('UPDATE alerts SET charged=? WHERE id=? AND charged IS NULL', (agorot, alert_id))
    if not cur.rowcount:
        return 0
    con.execute('UPDATE subscribers SET balance=COALESCE(balance,0)-? WHERE id=?', (agorot, sid))
    audit(con, 'alert_charge', {'id': sid, 'alert': alert_id, 'agorot': agorot})
    return agorot


STALE_SESSION_HOURS = 6


def close_stale_sessions(con, now=None):
    """Close calls whose hangup never arrived. Only calls that never started monitoring: those are
    closed by the monitor's 'disconnected' event, and closing them here would guess the deposit charge."""
    now = now or time.time()
    cur = con.execute('UPDATE sessions SET ended=started WHERE ended IS NULL AND mon_start IS NULL AND started<?',
                      (now - STALE_SESSION_HOURS * 3600,))
    return cur.rowcount


def settle_session(con, call_id, per_minute, now=None):
    """Charge a finished monitoring session to a prepaid deposit, once. Returns agorot charged."""
    now = now or time.time()
    s = con.execute('SELECT * FROM sessions WHERE call_id=?', (call_id,)).fetchone()
    if not s or s['charged'] is not None or not s['mon_start'] or not s['subscriber_id']:
        return 0
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (s['subscriber_id'],)).fetchone()
    if not sub or sub['plan'] != 'prepaid':
        con.execute('UPDATE sessions SET charged=0 WHERE call_id=?', (call_id,))
        return 0
    seconds = max(0.0, (s['ended'] or now) - s['mon_start'])
    minutes = int(-(-seconds // 60))  # every started minute
    cost = minutes * int(per_minute)
    con.execute('UPDATE sessions SET charged=? WHERE call_id=?', (cost, call_id))
    con.execute('UPDATE subscribers SET balance=COALESCE(balance,0)-? WHERE id=?', (cost, sub['id']))
    audit(con, 'usage', {'id': sub['id'], 'call': call_id, 'minutes': minutes, 'agorot': cost,
                         'balance': balance(sub) - cost})
    return cost


def create_subscriber(con, *, main_phone, name='', plan='monthly', status='active', amount=None,
                      expires=None, next_charge=None, alert_phone=None, notes='', legacy_id=None):
    main_phone = norm_phone(main_phone)
    if not valid_phone(main_phone):
        raise ValueError('מספר טלפון ראשי לא תקין')
    if phone_owner(con, main_phone):
        raise ValueError('המספר כבר רשום אצל מנוי אחר')
    if alert_phone:
        alert_phone = norm_phone(alert_phone)
        if not valid_phone(alert_phone):
            raise ValueError('מספר להתראות לא תקין')
    now = time.time()
    cur = con.execute(
        'INSERT INTO subscribers(legacy_id,name,main_phone,plan,status,amount,expires,next_charge,'
        'alert_phone,notes,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (legacy_id, name or '', main_phone, plan, status, amount, expires, next_charge,
         alert_phone or None, notes or '', now, now))
    audit(con, 'create', {'id': cur.lastrowid, 'phone': main_phone, 'plan': plan, 'status': status})
    return cur.lastrowid


def update_subscriber(con, sid, **fields):
    allowed = {'name', 'main_phone', 'plan', 'status', 'amount', 'expires', 'next_charge',
               'alert_phone', 'notes'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if 'main_phone' in fields:
        p = norm_phone(fields['main_phone'])
        if not valid_phone(p):
            raise ValueError('מספר טלפון ראשי לא תקין')
        owner = phone_owner(con, p)
        if owner and owner != sid:
            raise ValueError('המספר כבר רשום אצל מנוי אחר')
        fields['main_phone'] = p
    if fields.get('alert_phone'):
        p = norm_phone(fields['alert_phone'])
        if not valid_phone(p):
            raise ValueError('מספר להתראות לא תקין')
        fields['alert_phone'] = p
    elif 'alert_phone' in fields:
        fields['alert_phone'] = None
    if not fields:
        return
    fields['updated'] = time.time()
    cols = ','.join(f'{k}=?' for k in fields)
    con.execute(f'UPDATE subscribers SET {cols} WHERE id=?', (*fields.values(), sid))
    audit(con, 'update', {'id': sid, **{k: v for k, v in fields.items() if k != 'updated'}})


def extend(con, sid, months, day):
    """Record a payment: extend expiry by N months from max(today, current expiry); set active."""
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
    base = day
    if sub['expires'] and sub['expires'] > day.isoformat():
        base = datetime.date.fromisoformat(sub['expires'])
    new = add_months(base, months)
    plan = 'annual' if months >= 12 else 'monthly'
    con.execute('UPDATE subscribers SET expires=?, next_charge=?, status=?, plan=?, updated=? WHERE id=?',
                (new.isoformat(), new.isoformat(), 'active', plan, time.time(), sid))
    audit(con, 'payment', {'id': sid, 'months': months, 'expires': new.isoformat()})
    return new


def add_phone(con, sid, phone, label='', max_phones=5):
    p = norm_phone(phone)
    if not valid_phone(p):
        raise ValueError('מספר טלפון לא תקין')
    if phone_owner(con, p):
        raise ValueError('המספר כבר רשום')
    n = con.execute('SELECT COUNT(*) FROM phones WHERE subscriber_id=?', (sid,)).fetchone()[0]
    if n >= max_phones:
        raise ValueError(f'הגעת למקסימום {max_phones} טלפונים נוספים למנוי')
    con.execute('INSERT INTO phones(subscriber_id,phone,label,created) VALUES(?,?,?,?)',
                (sid, p, label or '', time.time()))
    audit(con, 'add_phone', {'id': sid, 'phone': p})


def remove_phone(con, sid, phone_id):
    con.execute('DELETE FROM phones WHERE id=? AND subscriber_id=?', (phone_id, sid))
    audit(con, 'remove_phone', {'id': sid, 'phone_id': phone_id})


def delete_subscriber(con, sid):
    con.execute('DELETE FROM subscribers WHERE id=?', (sid,))
    audit(con, 'delete', {'id': sid})
