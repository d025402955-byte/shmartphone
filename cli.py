#!/usr/bin/env python3
"""Command-line tools for the server (no browser needed).

  python3 /opt/shmartphone-app/cli.py import DIR    import DIR/subs.csv then DIR/phones.csv, then delete them
  python3 /opt/shmartphone-app/cli.py stats         subscriber counts
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


def stats():
    con = store.connect(db_path())
    print('subscribers:', dict(con.execute('SELECT status, COUNT(*) FROM subscribers GROUP BY status').fetchall()))
    print('extra phones:', con.execute('SELECT COUNT(*) FROM phones').fetchone()[0])
    con.close()


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'
    if cmd == 'import':
        do_import(sys.argv[2])
    elif cmd == 'extensions':
        extensions()
    elif cmd == 'ext':
        for e in sys.argv[2:]:
            ext_settings(e)
    else:
        stats()
