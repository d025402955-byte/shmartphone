"""Import subscriber exports (xlsx / csv / html-table .xls) from Rishumon.

Only the columns listed in SUB_COLUMNS / PHONE_COLUMNS are read. Card columns
(token, CVV, card expiry, last digits, holder id/name) are never read or stored.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import store
import xls

SUB_COLUMNS = {
    'legacy_id': ['ID', 'מזהה', 'מזהה מנוי'],
    'phone': ['טלפון לקוח ראשי', 'טלפון ראשי', 'טלפון'],
    'name': ['שם לקוח', 'שם המנוי', 'שם'],
    'status': ['סטטוס מנוי', 'סטטוס'],
    'amount': ['סכום לחיוב', 'סכום'],
    'billing': ['סוג חיוב'],
    'next_charge': ['תאריך החיוב הבא'],
    'expires': ['תאריך תפוגת המנוי', 'תאריך תפוגה'],
    'office_note': ['הערת משרד'],
    'status_note': ['הערת סטטוס'],
}
PHONE_COLUMNS = {
    'phone': ['טלפון'],
    'subscription': ['מנוי'],
}
STATUS_MAP = {'פעיל': 'active', 'מושבת': 'disabled', 'תקופת נסיון': 'trial', 'תקופת ניסיון': 'trial',
              'מבוטל': 'cancelled', 'חינמי': 'active'}
BILLING_MAP = {'חודשי': 'monthly', 'שנתי': 'annual', 'חינמי': 'free', 'חינם': 'free'}
EMPTY = {'', '(לא הוגדר)', 'לא הוגדר', 'None', 'null'}


def _clean(v):
    s = '' if v is None else str(v).strip()
    return '' if s in EMPTY else s


def _norm_header(h):
    return re.sub(r'\s+', ' ', str(h or '').replace('‏', '').replace('‎', '')).strip()


# ---------- readers ----------

def _read_xlsx(data):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % ns['m'])))
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
    root = ET.fromstring(z.read(sheets[0]))
    rows = []
    for r in root.iter('{%s}row' % ns['m']):
        cells = {}
        for c in r.findall('m:c', ns):
            ref = c.get('r', '')
            col = 0
            for ch in re.match(r'[A-Z]*', ref).group():
                col = col * 26 + ord(ch) - 64
            t = c.get('t')
            v = c.find('m:v', ns)
            if t == 's' and v is not None:
                val = shared[int(v.text)]
            elif t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter('{%s}t' % ns['m']))
            else:
                val = v.text if v is not None else ''
            cells[col - 1 if col else len(cells)] = val
        if cells:
            rows.append([cells.get(i, '') for i in range(max(cells) + 1)])
    return rows


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.in_cell, self.cell = True, []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.row is not None:
            self.row.append(''.join(self.cell).strip())
            self.in_cell = False
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, d):
        if self.in_cell:
            self.cell.append(d)


def _decode(data):
    for enc in ('utf-8-sig', 'cp1255'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode('utf-8', 'replace')


def read_table(data, filename=''):
    """Return a list of dict rows keyed by normalized header."""
    if data[:2] == b'PK':
        rows = _read_xlsx(data)
    elif data[:8] == xls.OLE_MAGIC:
        rows = xls.read_xls(data)
    else:
        txt = _decode(data)
        if '<table' in txt.lower() or '<tr' in txt.lower():
            p = _TableParser()
            p.feed(txt)
            rows = p.rows
        else:
            dialect = csv.Sniffer().sniff(txt[:4096], delimiters=',;\t') if txt.strip() else csv.excel
            rows = list(csv.reader(io.StringIO(txt), dialect))
    # header = first row that contains a phone-like header
    for i, r in enumerate(rows):
        hs = [_norm_header(h) for h in r]
        if any('טלפון' in h for h in hs):
            out = []
            for r2 in rows[i + 1:]:
                if not any(_clean(c) for c in r2):
                    continue
                out.append({hs[j]: r2[j] if j < len(r2) else '' for j in range(len(hs)) if hs[j]})
            return out
    raise ValueError('לא נמצאה שורת כותרות עם עמודת טלפון')


def _pick(row, names):
    for n in names:
        if n in row:
            return _clean(row[n])
    return ''


# ---------- importers ----------

def _paid_here(con, sub):
    """Billed by this system (Nedarim through our phone payments): the old system no longer decides."""
    return sub['billing'] == 'nedarim' or con.execute('SELECT 1 FROM payments WHERE subscriber_id=? LIMIT 1',
                                                      (sub['id'],)).fetchone() is not None


def _trial_open(sub, today):
    return sub['status'] == 'trial' and (not sub['expires'] or sub['expires'] >= today)


def import_subscribers(con, rows, today=None):
    """Create / refresh subscribers from a Rishumon export. Re-import safe:
    - subscribers paying through this system keep plan, status and dates;
    - trials are never blocked: a trial from the file starts its days on the next call (no end date),
      and an open trial here stays open unless the file says the subscriber is now active;
    - local data (verified numbers, alert numbers, history) is never touched."""
    import datetime
    today = (today or datetime.date.today()).isoformat()
    stats = {'created': 0, 'updated': 0, 'kept_paid': 0, 'kept_trial': 0, 'skipped': 0, 'errors': []}
    for i, row in enumerate(rows, start=2):
        phone = store.norm_phone(_pick(row, SUB_COLUMNS['phone']))
        if not store.valid_phone(phone):
            stats['skipped'] += 1
            continue
        legacy = _pick(row, SUB_COLUMNS['legacy_id']) or None
        if legacy and re.fullmatch(r'\d+\.0+', legacy):
            legacy = legacy.split('.')[0]
        raw_status = _pick(row, SUB_COLUMNS['status'])
        status = STATUS_MAP.get(raw_status, 'disabled')
        plan = BILLING_MAP.get(_pick(row, SUB_COLUMNS['billing']), 'monthly')
        if status == 'trial':
            plan = 'trial'
        if raw_status == 'חינמי':
            plan = 'free'
        notes = ' · '.join(x for x in (_pick(row, SUB_COLUMNS['office_note']),
                                        _pick(row, SUB_COLUMNS['status_note'])) if x)
        amount = _pick(row, SUB_COLUMNS['amount'])
        try:
            amount = float(amount) if amount else None
        except ValueError:
            amount = None
        fields = dict(name=_pick(row, SUB_COLUMNS['name']), plan=plan, status=status, amount=amount,
                      expires=store.parse_date(_pick(row, SUB_COLUMNS['expires'])),
                      next_charge=store.parse_date(_pick(row, SUB_COLUMNS['next_charge'])))
        existing = None
        if legacy:
            existing = con.execute('SELECT * FROM subscribers WHERE legacy_id=?', (legacy,)).fetchone()
        if existing is None:
            existing = con.execute('SELECT * FROM subscribers WHERE main_phone=?', (phone,)).fetchone()
        if status == 'trial':
            fields.update(expires=None, next_charge=None)  # the trial days start on the next call
        try:
            if existing and _paid_here(con, existing):
                fields = {'name': fields['name']} if fields['name'] else {}
                stats['kept_paid'] += 1
            elif existing and _trial_open(existing, today) and status != 'active':
                fields = {'name': fields['name']} if fields['name'] else {}
                stats['kept_trial'] += 1
            if existing:
                changed = {k: v for k, v in {'main_phone': phone, **fields}.items() if existing[k] != v}
                if changed:
                    store.update_subscriber(con, existing['id'], **changed)
                    stats['updated'] += 1
                else:
                    stats['unchanged'] = stats.get('unchanged', 0) + 1
                if legacy and not existing['legacy_id']:
                    con.execute('UPDATE subscribers SET legacy_id=? WHERE id=?', (legacy, existing['id']))
            else:
                store.create_subscriber(con, main_phone=phone, legacy_id=legacy,
                                        notes=('יובא מרישומון. ' + notes).strip(), **fields)
                stats['created'] += 1
        except ValueError as e:
            stats['errors'].append(f'שורה {i} ({phone}): {e}')
    return stats


def import_phones(con, rows, max_phones=50):
    stats = {'created': 0, 'existing': 0, 'skipped': 0, 'errors': []}
    for i, row in enumerate(rows, start=2):
        phone = store.norm_phone(_pick(row, PHONE_COLUMNS['phone']))
        ref = _pick(row, PHONE_COLUMNS['subscription'])  # e.g. "103 - 0527173229"
        if not store.valid_phone(phone) or not ref:
            stats['skipped'] += 1
            continue
        tokens = re.findall(r'\d+', ref)
        legacy = next((t for t in tokens if len(t) <= 6), None)
        main = next((t for t in tokens if len(t) >= 8), '')
        sub = None
        if legacy:
            sub = con.execute('SELECT * FROM subscribers WHERE legacy_id=?', (legacy,)).fetchone()
        if sub is None:
            sub = con.execute('SELECT * FROM subscribers WHERE main_phone=?',
                              (store.norm_phone(main),)).fetchone()
        if sub is None:
            stats['errors'].append(f'שורה {i} ({phone}): לא נמצא מנוי {ref}')
            continue
        owner = store.phone_owner(con, phone)
        if owner == sub['id']:
            stats['existing'] += 1
            continue
        try:
            store.add_phone(con, sub['id'], phone, max_phones=max_phones)
            stats['created'] += 1
        except ValueError as e:
            stats['errors'].append(f'שורה {i} ({phone}): {e}')
    return stats
