#!/usr/bin/env python3
"""Command-line tools for the server (no browser needed).

  python3 /opt/shmartphone-app/cli.py import DIR    import DIR/subs.csv then DIR/phones.csv, then delete them
  python3 /opt/shmartphone-app/cli.py stats         subscriber counts
  python3 /opt/shmartphone-app/cli.py report [send] print the daily summary (send: also email it now)
"""
import json
import os
import pwd
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importer  # noqa: E402
import store  # noqa: E402

CFG = os.environ.get('SHMARTPHONE_CONFIG', '/etc/shmartphone/app.json')


def db_path():
    with open(CFG, encoding='utf-8') as f:
        return json.load(f)['db_path']


def fix_owner(path):
    """Keep DB files owned by the service user even when this runs as root."""
    st = os.stat(os.path.dirname(path))
    for p in (path, path + '-wal', path + '-shm'):
        if os.path.exists(p):
            os.chown(p, st.st_uid, st.st_gid)


def do_import(folder):
    path = db_path()
    con = store.connect(path)
    for name, fn in (('subs.csv', importer.import_subscribers), ('phones.csv', importer.import_phones)):
        f = os.path.join(folder, name)
        if not os.path.exists(f):
            print('missing', name)
            continue
        with open(f, 'rb') as fh:
            rows = importer.read_table(fh.read(), name)
        with con:
            st = fn(con, rows) if name == 'subs.csv' else fn(con, rows, 50)
            store.audit(con, 'import', {'file': name, **{k: v for k, v in st.items() if k != 'errors'}})
        print(name, {k: v for k, v in st.items() if k != 'errors'}, 'errors:', len(st['errors']))
        for e in st['errors'][:10]:
            print('  ', e)
        os.remove(f)
    con.close()
    fix_owner(path)
    stats()


def extensions():
    """List Technoline extensions (ID, key path, type, name) using the stored API key. Read-only."""
    import urllib.parse
    import urllib.request
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    url = cfg.get('files_url', 'https://api.tlivr.com/ivrFilesApi.php')
    data = urllib.parse.urlencode({'action': 'foldersList', 'apiKey': cfg['api_key']}).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers={'User-Agent': 'curl/8.5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        tree = json.loads(r.read())
    if isinstance(tree, dict) and tree.get('status') == 'ERROR':
        print('error:', tree.get('note'))
        return

    def walk(nodes, path):
        for n in (nodes.values() if isinstance(nodes, dict) else nodes):
            if not isinstance(n, dict) or 'id' not in n:
                continue
            p = path + ('/' + str(n.get('extension')) if n.get('extension') not in (None, '') else '')
            print(f"ID {n['id']:>8}   key {p or '/':<12} type {n.get('type', ''):<16} {n.get('name', '')}")
            walk(n.get('children') or {}, p)
    walk(tree, '')


def ext_settings(ext_id):
    """Show one extension's type and settings (the API URL with its token masked). Read-only."""
    import urllib.parse
    import urllib.request
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    url = cfg.get('files_url', 'https://api.tlivr.com/ivrFilesApi.php')
    data = urllib.parse.urlencode({'action': 'folderSettings', 'extensionId': ext_id, 'apiKey': cfg['api_key']}).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers={'User-Agent': 'curl/8.5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        info = json.loads(r.read())
    text = json.dumps(info.get('settings', info), ensure_ascii=True)
    for tok in (cfg['ivr_token'], cfg['event_token']):
        text = text.replace(tok, '***')
    print('ID', ext_id, 'type', info.get('type'), 'key', info.get('extension'))
    print(text[:1500])


def log_window(day, hhmm, minutes=3):
    """Print service log lines of day (YYYYMMDD) from hhmm for N minutes (no special characters needed)."""
    import datetime
    import subprocess
    start = datetime.datetime.strptime(day + hhmm, '%Y%m%d%H%M')
    end = start + datetime.timedelta(minutes=int(minutes))
    out = subprocess.run(['journalctl', '-u', 'shmartphone', '--no-pager', '-o', 'short-iso',
                          '-S', start.strftime('%Y-%m-%d %H:%M:%S'), '-U', end.strftime('%Y-%m-%d %H:%M:%S')],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(' ', 3)
        print(parts[0][11:19], parts[3] if len(parts) > 3 else line)


def doc(word, context=40, name='PBX_DOCUMENTATION_CENTER.md'):
    """Search Technoline's documentation for word; prints matching lines with context. Read-only."""
    import urllib.request
    path = '/tmp/pbx_' + os.path.basename(name)
    if not os.path.exists(path):
        req = urllib.request.Request('https://app.ipsales.co.il/' + os.path.basename(name),
                                     headers={'User-Agent': 'curl/8.5.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if data[:2] == b'\x1f\x8b':
            import gzip
            data = gzip.decompress(data)
        with open(path, 'wb') as f:
            f.write(data)
    lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    print('doc lines:', len(lines))
    hits = [i for i, l in enumerate(lines) if word.lower() in l.lower()]
    print('hits:', len(hits), hits[:40])
    shown = -1
    for i in hits:
        if i <= shown:
            continue
        for j in range(max(0, i - 3), min(len(lines), i + int(context))):
            print(j, lines[j][:220])
        shown = i + int(context) - 1
        print('-----')


def test_alert(sid, kind='noise', mode='voice'):
    """Send one real alert of KIND by MODE (ping/voice/message) to subscriber SID's alert phone."""
    import core
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    con = store.connect(cfg['db_path'])
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (int(sid),)).fetchone()
    con.close()
    phone = sub and sub['alert_phone']
    if not phone:
        print('no alert phone for subscriber', sid)
        return
    print('to', phone[:3] + '****' + phone[-3:], kind, mode)
    if mode in ('ping', 'voice'):
        print(core.send_ping({**cfg, 'alert_mode': mode}, kind, phone))
    else:
        print(core.send_alert(cfg, kind, phone, mode='message'))


def credit(phone, shekels):
    """Add SHEKELS to the deposit of the subscriber that owns PHONE (main or extra phone)."""
    path = db_path()
    con = store.connect(path)
    sub = store.find_by_phone(con, phone)
    if not sub:
        rows = con.execute('SELECT subscriber_id FROM alert_phones WHERE phone=?', (store.norm_phone(phone),)).fetchall()
        print('no subscriber owns this phone; it is a verified alert number of subscriber(s):',
              [r[0] for r in rows] or 'none')
        con.close()
        return
    amount = float(shekels)
    with con:
        new = store.add_credit(con, sub['id'], round(amount * 100), source='admin', amount=amount, note='cli')
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (sub['id'],)).fetchone()
    print('subscriber', sub['id'], 'plan', sub['plan'], 'status', sub['status'], 'balance', '%.2f' % (new / 100))
    con.close()
    fix_owner(path)


def messages():
    con = store.connect(db_path())
    for r in con.execute('SELECT id, phone, file_id, file_name, duration, state, created FROM messages ORDER BY id DESC LIMIT 10'):
        print(dict(r))
    con.close()


def simulate(phone, *keys):
    """Replay a menu call for PHONE pressing KEYS (menu, then answers) on a COPY of the database; prints the JSON."""
    import datetime
    import shutil
    import tempfile
    import ivr
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    tmp = os.path.join(tempfile.mkdtemp(), 'sim.db')
    con0 = store.connect(cfg['db_path'])
    con0.execute('PRAGMA wal_checkpoint')
    con0.close()
    shutil.copy(cfg['db_path'], tmp)
    cfg['db_path'] = tmp
    con = store.connect(tmp)
    p = {'PBXphone': phone, 'PBXnum': cfg.get('pbx_number', ''), 'PBXcallId': 'sim1', 'PBXcallStatus': 'NEW'}
    names = ['menu']
    with con:
        r, _ = ivr.handle(cfg, con, p, datetime.date.today(), 'menu')
    for k in keys:
        name = next((c.get('name') for c in reversed(r) if c.get('name')), 'menu')
        p[name] = k
        with con:
            r, _ = ivr.handle(cfg, con, dict(p), datetime.date.today(), 'menu')
    print(json.dumps(r, ensure_ascii=False)[:3000])


def hostkey():
    """Print the server's SSH host key fingerprints (to compare with what ssh shows on first connect)."""
    import glob
    import subprocess
    for f in sorted(glob.glob('/etc/ssh/ssh_host_*_key.pub')):
        print(subprocess.run(['ssh-keygen', '-lf', f], capture_output=True, text=True).stdout.strip())


def addkey(kind, key, comment='admin-desktop'):
    """Append an SSH public key to root's authorized_keys (no duplicates, safe permissions)."""
    import base64
    if kind not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
        print('unsupported key type')
        return
    base64.b64decode(key, validate=True)  # refuse anything that is not a key
    d = '/root/.ssh'
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, 'authorized_keys')
    old = open(path).read() if os.path.exists(path) else ''
    if key in old:
        print('key already present')
        return
    with open(path, 'a') as f:
        if old and not old.endswith('\n'):
            f.write('\n')
        f.write(f'{kind} {key} {comment}\n')
    os.chmod(path, 0o600)
    os.chmod(d, 0o700)
    print('key added; keys now:', sum(1 for line in open(path) if line.strip()))


def stats():
    con = store.connect(db_path())
    print('subscribers:', dict(con.execute('SELECT status, COUNT(*) FROM subscribers GROUP BY status').fetchall()))
    print('extra phones:', con.execute('SELECT COUNT(*) FROM phones').fetchone()[0])
    con.close()


def daily_report(send=False):
    """Print the daily summary; with `send`, also email it now."""
    import core
    import report
    with open(CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    con = store.connect(cfg['db_path'])
    subject, body = report.build(con)
    con.close()
    print(subject + '\n\n' + body)
    if send:
        core.send_text_email(cfg, subject, body)
        print('sent to', cfg['smtp'].get('report_to') or cfg['smtp']['to'])


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'
    if cmd == 'import':
        do_import(sys.argv[2])
    elif cmd == 'extensions':
        extensions()
    elif cmd == 'log':
        log_window(*sys.argv[2:5])
    elif cmd == 'addkey':
        addkey(*sys.argv[2:5])
    elif cmd == 'hostkey':
        hostkey()
    elif cmd == 'simulate':
        simulate(*sys.argv[2:])
    elif cmd == 'messages':
        messages()
    elif cmd == 'credit':
        credit(*sys.argv[2:4])
    elif cmd == 'testalert':
        test_alert(*sys.argv[2:5])
    elif cmd == 'doc':
        doc(*sys.argv[2:5])
    elif cmd == 'report':
        daily_report(len(sys.argv) > 2 and sys.argv[2] == 'send')
    elif cmd == 'ext':
        for e in sys.argv[2:]:
            ext_settings(e)
    else:
        stats()
