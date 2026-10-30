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
        with urllib.request.urlopen(cfg['public_url'] + '/admin', timeout=10) as r:
            print('admin page:', 'OK (asks for password)' if '/admin/login' in r.url else f'OPEN? {r.url}')
    except urllib.error.HTTPError as e:
        print('admin page:', f'HTTP {e.code}')
    except Exception as e:
        print('admin page: FAILED', repr(e)[:120])
    print('\nadmin:', cfg['public_url'] + '/admin')
    print('demo mode:', cfg.get('demo'))


def set_password():
    import admin
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['admin_password_hash'] = admin.hash_password(ask_password())
    write_config(cfg, service_user())
    print('password updated (no restart needed after next restart; restarting now)')
    run('systemctl', 'restart', SERVICE)


def set_smtp():
    """Email settings for customer-service messages (option 9)."""
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    print('Gmail: host smtp.gmail.com, port 587, user = the gmail address, password = an App Password')
    host = input('smtp host [smtp.gmail.com]: ').strip() or 'smtp.gmail.com'
    port = int(input('smtp port [587]: ').strip() or 587)
    user = input('smtp user (sending email address): ').strip()
    password = getpass.getpass('smtp password (not shown): ').strip()
    to = input('send messages to (email address): ').strip()
    cfg['smtp'] = {'host': host, 'port': port, 'user': user, 'password': password, 'from': user, 'to': to}
    import smtplib
    try:
        srv = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
        with srv:
            if port != 465:
                srv.starttls()
            srv.login(user, password)
        print('login OK')
    except Exception as e:
        print('login FAILED:', repr(e)[:200])
        if input('save anyway? (y/n): ').strip() != 'y':
            return
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('saved; service restarted')


GEMINI_VOICES = ['Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe',
                 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi',
                 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird',
                 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat']


def set_voice():
    """install.py voice NAME  (case does not matter; 'none' = free default voice)"""
    name = (sys.argv[2] if len(sys.argv) > 2 else '').strip().lower()
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    if name in ('', 'none', 'default'):
        cfg.pop('voice', None)
        print('voice: free default')
    else:
        match = [v for v in GEMINI_VOICES if v.lower() == name]
        if not match:
            print('unknown voice. choose one of:', ' '.join(v.lower() for v in GEMINI_VOICES))
            return
        cfg['voice'] = match[0]
        print('voice:', match[0])
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)


def set_alerts():
    """install.py alerts ping|voice|message|call [offer-voice]  default alert type for subscribers who did not choose.
    call = the system calls the parent into the alert extension (needs alert_extension), who hears what happened."""
    mode = (sys.argv[2] if len(sys.argv) > 2 else '').strip().lower()
    if mode not in ('ping', 'voice', 'message', 'call'):
        print('usage: install.py alerts ping|voice|message|call')
        return
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['alert_mode'] = mode
    cfg['voice_alerts_offer'] = 'offer-voice' in sys.argv[3:]
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('alerts:', mode, '(voice offered)' if cfg['voice_alerts_offer'] else '')


def set_price():
    """install.py price AGOROT  deposit price per monitoring minute"""
    try:
        agorot = int(sys.argv[2])
        assert 1 <= agorot <= 100
    except (IndexError, ValueError, AssertionError):
        print('usage: install.py price AGOROT   (1-100)')
        return
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg.setdefault('payments', {})['per_minute_agorot'] = agorot
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('price per minute (agorot):', agorot)


def set_recharge():
    """install.py recharge on|off  charge reconnect calls to the subscriber's deposit"""
    v = (sys.argv[2] if len(sys.argv) > 2 else '').lower()
    if v not in ('on', 'off'):
        print('usage: install.py recharge on|off')
        return
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['reconnect_charge'] = v == 'on'
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('reconnect charged from deposit:', v)


def set_message_extension():
    """install.py msgext EXTENSION_ID  key 9 goes to this Technoline extension (0 = record here)"""
    v = (sys.argv[2] if len(sys.argv) > 2 else '').strip()
    if not v.isdigit():
        print('usage: install.py msgext EXTENSION_ID   (0 = off)')
        return
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    if v == '0':
        cfg.pop('message_extension', None)
    else:
        cfg['message_extension'] = v
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('key 9 ->', cfg.get('message_extension', 'record here'))


def set_voicemail_pin():
    """install.py vmpin  set the code for listening to messages (menu key 0); typed at the prompt, stored hashed"""
    import getpass
    import admin
    pin = getpass.getpass('code (digits): ').strip()
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        print('the code must be 4-8 digits')
        return
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['voicemail_pin_hash'] = admin.hash_password(pin)
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('voicemail code saved')


SETTABLE = {'messages_folder', 'messages_copy_folder', 'niqqud', 'message_notify_phone'}


def set_value():
    """install.py set KEY VALUE  (KEY in SETTABLE; VALUE 0 = remove)"""
    key = sys.argv[2].replace('-', '_') if len(sys.argv) > 2 else ''  # '-' is easier to type in the console
    if len(sys.argv) < 4 or key not in SETTABLE:
        print('usage: install.py set KEY VALUE   keys:', ' '.join(sorted(SETTABLE)))
        return
    val = sys.argv[3].strip()
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    if key == 'niqqud':
        cfg[key] = val not in ('0', 'off', 'no')
    elif val == '0':
        cfg.pop(key, None)
    else:
        cfg[key] = val
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print(key, '=', cfg.get(key))


def set_payments():
    """Turn on payments by phone (Technoline creditCard -> Nedarim Plus) and the renewal sync."""
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    p = cfg.get('payments') or {}
    print('prices are in shekels. press Enter to keep the value in [brackets].')
    p['monthly'] = float(input(f"monthly price [{p.get('monthly', 8.9)}]: ").strip() or p.get('monthly', 8.9))
    p['annual'] = float(input(f"annual price [{p.get('annual', 82)}]: ").strip() or p.get('annual', 82))
    p.setdefault('category', '\u05e9\u05de\u05e8\u05d8\u05e4\u05d5\u05df')
    term = input(f"technoline terminal (usually empty) [{p.get('terminal', '')}]: ").strip()
    if term:
        p['terminal'] = term
    cfg['payments'] = p
    n = cfg.get('nedarim') or {}
    mosad = input(f"nedarim plus institution number [{n.get('mosad', '')}]: ").strip() or n.get('mosad', '')
    key = getpass.getpass('nedarim plus API key npk_... (not shown; Enter = keep): ')
    key = ''.join(ch for ch in key if ch.isprintable() and not ch.isspace()).strip('"\'') or n.get('api_password', '')
    if key:
        print(f"key check: length {len(key)}, starts with npk_: {key.startswith('npk_')}, "
              f"only letters/digits/_-: {all(c.isalnum() or c in '_-' for c in key)}")
    if mosad and key:
        cfg['nedarim'] = {'mosad': mosad, 'api_password': key}
        import nedarim
        try:
            dbg = {}
            try:
                rows = nedarim.fetch_history(cfg, '', dbg)
            finally:
                print('nedarim url used:', dbg.get('url', nedarim.URL))
            print('nedarim connection OK,', len(rows), 'transactions read')
            if rows and len(rows) < nedarim.PAGE:
                cfg['nedarim']['start_id'] = str(max(int(r.get('TransactionId') or 0) for r in rows))
                print('renewal sync will start after transaction', cfg['nedarim']['start_id'])
        except Exception as e:
            print('nedarim connection FAILED:', repr(e)[:200])
            if input('save anyway? (y/n): ').strip() != 'y':
                return
    else:
        print('no nedarim key: payments by phone work, monthly renewals will NOT be synced')
    write_config(cfg, service_user())
    run('systemctl', 'restart', SERVICE)
    print('saved; service restarted')


def set_reconnect():
    """install.py reconnect ACTIVATE_EXT_ID ALERT_EXT_ID  (Technoline extension IDs; 0 0 = off)"""
    args = [a.strip() for a in sys.argv[2:4]]
    with open(NEW_CFG, encoding='utf-8') as f:
        cfg = json.load(f)
    if len(args) != 2 or not all(a.isdigit() for a in args):
        print('usage: install.py reconnect ACTIVATE_EXTENSION_ID ALERT_EXTENSION_ID   (0 0 = off)')
        return
    for key, val in zip(('reconnect_extension', 'alert_extension'), args):
        if val == '0':
            cfg.pop(key, None)
        else:
            cfg[key] = val
    print('room call-back into extension:', cfg.get('reconnect_extension', 'off'),
          '| interactive disconnect alert via extension:', cfg.get('alert_extension', 'off'))
    write_config(cfg, service_user())
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
    {'install': install, 'password': set_password, 'rollback': rollback, 'smtp': set_smtp, 'voice': set_voice, 'alerts': set_alerts, 'price': set_price, 'recharge': set_recharge, 'msgext': set_message_extension, 'vmpin': set_voicemail_pin, 'set': set_value, 'payments': set_payments, 'reconnect': set_reconnect,
     'status': lambda: print(run('systemctl', 'status', SERVICE, '--no-pager', check=False))}[cmd]()
