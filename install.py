#!/usr/bin/env python3
"""Install / update / roll back the Shmartphone app on the server.

  python3 /opt/shmartphone-app/install.py            install or update (keeps existing settings)
  python3 /opt/shmartphone-app/install.py password   set a new admin password
  python3 /opt/shmartphone-app/install.py rollback   go back to the previous app
  python3 /opt/shmartphone-app/install.py status     show service state

Reads the old /etc/shmartphone/config.json once to reuse the API key, tokens,
system number and public URL, so the Technoline extension URL stays the same.
Never prints secret values.
"""
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OLD_CFG = '/etc/shmartphone/config.json'
NEW_CFG = '/etc/shmartphone/app.json'
DB_PATH = '/var/lib/shmartphone/app.db'
SERVICE = 'shmartphone'
DROPIN_DIR = f'/etc/systemd/system/{SERVICE}.service.d'
DROPIN = os.path.join(DROPIN_DIR, 'zz-app.conf')

sys.path.insert(0, APP_DIR)


def run(*cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True).stdout.strip()


def choose(label, keys):
    print(f'\n{label}:')
    for i, k in enumerate(keys, 1):
        print(f'  {i}. {k}')
    while True:
        a = input('number: ').strip()
        if a.isdigit() and 1 <= int(a) <= len(keys):
            return keys[int(a) - 1]


def find_key(old, must, exclude=()):
    keys = [k for k in old if isinstance(old[k], str) and all(m in k.lower() for m in must)
            and not any(x in k.lower() for x in exclude)]
    return keys


def ask_password():
    while True:
        a = getpass.getpass('new admin password (min 8 chars): ')
        if len(a) < 8:
            print('too short')
            continue
        if getpass.getpass('again: ') != a:
            print('not the same, try again')
            continue
        return a


def build_config():
    import admin
    old = {}
    if os.path.exists(OLD_CFG):
        with open(OLD_CFG, encoding='utf-8') as f:
            old = json.load(f)
        print('old config keys:', ', '.join(sorted(old)))
    cfg = {'api_key': old.get('api_key', ''), 'pbx_number': old.get('pbx_number', ''),
           'demo': bool(old.get('demo', False))}

    urls = [k for k, v in old.items() if isinstance(v, str) and v.startswith('https://')]
    url_keys = [k for k in urls if 'url' in k.lower() and 'campaign' not in k.lower() and 'api' not in k.lower()]
    if len(url_keys) == 1:
        cfg['public_url'] = old[url_keys[0]].rstrip('/')
    else:
        cfg['public_url'] = input('public url (example https://212-80-206-147.sslip.io): ').strip().rstrip('/')

    candidates = {
        'ivr_token': find_key(old, ['ivr'], ['url']),
        'event_token': find_key(old, ['event'], ['url']) or find_key(old, ['alert', 'token'], ['url']),
    }
    for name, keys in candidates.items():
        if len(keys) == 1:
            cfg[name] = old[keys[0]]
            print(f'{name}: taken from "{keys[0]}"')
        elif keys:
            cfg[name] = old[choose(f'which key is the {name}?', keys)]
        else:
            cfg[name] = secrets.token_hex(16)
            print(f'{name}: NEW token generated (update the Technoline extension URL)')

    for k in ('listen_host', 'host'):
        if old.get(k):
            cfg['listen_host'] = old[k]
    for k in ('listen_port', 'port'):
        if old.get(k):
            cfg['listen_port'] = int(old[k])
    if old.get('threshold'):
        cfg['monitor'] = {'threshold': int(old['threshold'])}

    cfg.update({
        'db_path': DB_PATH, 'secret_key': secrets.token_hex(32), 'timezone': 'Asia/Jerusalem',
        'auto_trial_days': 7, 'trial_days': 7, 'max_extra_phones': 5, 'alert_cooldown': 120,
        'alert_on_disconnect': True, 'enforce_active_expiry': False, 'call_length': 25, 'dial_retries': 1,
        'campaign_url': 'https://api.tlivr.com/campaignApi.php',
    })
    if not cfg['api_key']:
        cfg['api_key'] = getpass.getpass('Technoline API key: ').strip()
    cfg['admin_password_hash'] = admin.hash_password(ask_password())
    return cfg


def write_config(cfg, user):
    tmp = NEW_CFG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    if user and user != 'root':
        shutil.chown(tmp, 'root', user)
    os.replace(tmp, NEW_CFG)


def service_user():
    u = run('systemctl', 'show', '-p', 'User', '--value', SERVICE, check=False)
    return u or 'root'


def install():
    import store
    user = service_user()
    print('service user:', user)
    if os.path.exists(NEW_CFG):
        with open(NEW_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
        print('keeping existing', NEW_CFG)
    else:
        cfg = build_config()
        write_config(cfg, user)
        print('wrote', NEW_CFG)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    store.init(cfg['db_path'])
    if user != 'root':
        for p in (os.path.dirname(DB_PATH), cfg['db_path'], cfg['db_path'] + '-wal', cfg['db_path'] + '-shm'):
            if os.path.exists(p):
                shutil.chown(p, user, user)
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(DROPIN, 'w') as f:
        f.write('[Service]\nExecStart=\n'
                f'ExecStart=/usr/bin/python3 {APP_DIR}/app.py\n'
                f'Environment=SHMARTPHONE_CONFIG={NEW_CFG}\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', SERVICE)
    time.sleep(2)
    print('service:', run('systemctl', 'is-active', SERVICE, check=False))
    port = cfg.get('listen_port', 8088)
    for url in (f'http://127.0.0.1:{port}/health', cfg['public_url'] + '/health'):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                print('health', url.split('/')[2], r.status, r.read().decode())
        except Exception as e:
            print('health', url.split('/')[2], 'FAILED', repr(e)[:120])
    try:
        urllib.request.urlopen(cfg['public_url'] + '/admin', timeout=10)
    except urllib.error.HTTPError as e:
        print('admin page:', 'OK (asks for password)' if e.code == 401 else f'HTTP {e.code}')
    except Exception as e:
        print('admin page: FAILED', repr(e)[:120])
    print('\nadmin:', cfg['public_url'] + '/admin   (user: admin)')
    print('demo mode:', cfg.get('demo'))


def set_password():
    import admin
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['admin_password_hash'] = admin.hash_password(ask_password())
    write_config(cfg, service_user())
    print('password updated (no restart needed after next restart; restarting now)')
    run('systemctl', 'restart', SERVICE)


def rollback():
    if os.path.exists(DROPIN):
        os.remove(DROPIN)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', SERVICE)
    print('rolled back to the previous app; service:', run('systemctl', 'is-active', SERVICE, check=False))


if __name__ == '__main__':
    if os.geteuid() != 0:
        sys.exit('run as root')
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'install'
    {'install': install, 'password': set_password, 'rollback': rollback,
     'status': lambda: print(run('systemctl', 'status', SERVICE, '--no-pager', check=False))}[cmd]()
