#!/usr/bin/env python3
"""Shmartphone server: Technoline call flow, noise-monitor events, alert calls and admin pages.

Config: /etc/shmartphone/app.json (see install.py). Stdlib only.
"""
import datetime
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin  # noqa: E402
import core  # noqa: E402
import ivr  # noqa: E402
import nedarim  # noqa: E402
import store  # noqa: E402

CONFIG_PATH = os.environ.get('SHMARTPHONE_CONFIG', '/etc/shmartphone/app.json')
MAX_BODY = 6 * 1024 * 1024
HOME_PAGE = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>שמרטפון</title>
<style>body{font-family:system-ui,Arial,sans-serif;max-width:640px;margin:60px auto;padding:0 16px;color:#1d2433;line-height:1.6}
h1{font-size:28px}a{color:#2f6fde}</style></head><body>
<h1>שמרטפון</h1>
<p>שירות טלפוני לניטור חדר תינוק: הטלפון שבחדר מתקשר לקו השירות, המערכת מזהה רעש
ומתקשרת להורים להתריע.</p>
<p>זהו שרת המערכת. הוא משמש את מרכזיית הטלפון ואת מנהל השירות לניהול המנויים,
ואינו מכיל תוכן לגלישה.</p>
<p><a href="/admin">כניסת מנהל</a></p>
</body></html>"""


def load_config(path=CONFIG_PATH):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    for k in ('api_key', 'ivr_token', 'event_token', 'public_url', 'secret_key', 'db_path'):
        if not cfg.get(k):
            raise SystemExit(f'config missing {k}')
    return cfg


def make_handler(cfg):
    tz = zoneinfo.ZoneInfo(cfg.get('timezone', 'Asia/Jerusalem'))
    wake = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'shmartphone'
        sys_version = ''

        def log_message(self, fmt, *args):
            path = self.path.split('?')[0]
            for tok in (cfg['ivr_token'], cfg['event_token']):
                path = path.replace(tok, '***')
            print('HTTP', self.command, path, args[1] if len(args) > 1 else '', flush=True)

        def client_ip(self):
            ip = self.client_address[0]
            if ip in ('127.0.0.1', '::1') and self.headers.get('X-Forwarded-For'):
                ip = self.headers['X-Forwarded-For'].split(',')[-1].strip()
            return ip

        def send(self, status, ctype, body, extra=None):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def json(self, status, obj):
            self.send(status, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False).encode())

        def body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise ValueError('body too large')
            return self.rfile.read(n) if n else b''

        def do_HEAD(self):
            self.route('GET')

        def do_GET(self):
            self.route('GET')

        def do_POST(self):
            self.route('POST')

        def route(self, method):
            try:
                self._route(method)
            except Exception as e:  # keep the server alive; never leak details
                print('ERROR', method, repr(e)[:300], flush=True)
                try:
                    self.send(500, 'text/plain', b'error')
                except Exception:
                    pass

        def _route(self, method):
            url = urllib.parse.urlsplit(self.path)
            parts = [p for p in url.path.split('/') if p]
            day = datetime.datetime.now(tz).date()

            if url.path == '/health':
                return self.send(200, 'text/plain', b'ok')

            if url.path == '/':
                return self.send(200, 'text/html; charset=utf-8', HOME_PAGE.encode())

            if len(parts) in (2, 3) and parts[0] == 'ivr' and hmac.compare_digest(parts[1], cfg['ivr_token']):
                mode = parts[2] if len(parts) == 3 else 'activate'
                params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
                if method == 'POST':
                    params.update({k: v[-1] for k, v in urllib.parse.parse_qs(
                        self.body().decode('utf-8', 'replace'), keep_blank_values=True).items()})
                con = store.connect(cfg['db_path'])
                try:
                    with con:
                        resp, wake_now = ivr.handle(cfg, con, params, day, mode)
                finally:
                    con.close()
                if wake_now:
                    wake.set()
                print('IVR', mode, params.get('PBXcallStatus'),
                      'types=' + ','.join(c.get('type', '') for c in resp), flush=True)
                return self.json(200, resp)

            if len(parts) == 2 and parts[0] == 'events' and method == 'POST' \
                    and hmac.compare_digest(parts[1], cfg['event_token']):
                raw = self.body()
                if len(raw) > 16384:
                    return self.send(413, 'text/plain', b'too large')
                ev = json.loads(raw or b'{}')
                con = store.connect(cfg['db_path'])
                try:
                    with con:
                        status, msg = core.handle_event(cfg, con, ev, self.headers.get('X-IPPBX-Event', ''))
                finally:
                    con.close()
                print('EVENT', ev.get('event'), ev.get('roomId'), msg,
                      'level=' + str(ev.get('level') or ev.get('peakLevel') or ''), flush=True)
                if msg == 'queued':
                    wake.set()
                return self.send(status, 'text/plain', msg.encode())

            if parts and parts[0] == 'admin':
                if not admin.authorized(cfg, self.headers, self.client_ip()):
                    return self.send(401, 'text/plain; charset=utf-8', 'נדרשת התחברות'.encode(),
                                     {'WWW-Authenticate': 'Basic realm="shmartphone", charset="UTF-8"'})
                body = self.body() if method == 'POST' else b''
                con = store.connect(cfg['db_path'])
                try:
                    status, ctype, out, extra = admin.handle(cfg, con, method, url.path, url.query,
                                                             self.headers, body, day)
                finally:
                    con.close()
                return self.send(status, ctype, out, extra)

            return self.send(404, 'text/plain', b'not found')

    return Handler, wake


def alert_worker(cfg, wake):
    last_sync = 0
    while True:
        try:
            while core.process_one(cfg, cfg['db_path']):
                pass
            while core.process_message(cfg, cfg['db_path']):
                pass
        except Exception as e:
            print('WORKER_ERROR', repr(e)[:300], flush=True)
        if time.time() - last_sync > int(cfg.get('nedarim_sync_seconds', 1200)):
            last_sync = time.time()
            try:
                nedarim.sync(cfg, cfg['db_path'])
            except Exception as e:
                print('NEDARIM_SYNC_ERROR', repr(e)[:300], flush=True)
        wake.wait(5)
        wake.clear()


def main():
    cfg = load_config()
    store.init(cfg['db_path'])
    # alerts left in 'sending' by a crash may or may not have been placed: mark them unknown
    con = store.connect(cfg['db_path'])
    with con:
        con.execute("UPDATE alerts SET state='unknown', result=? WHERE state='sending'",
                    (json.dumps({'note': 'interrupted by restart'}),))
    con.close()
    handler, wake = make_handler(cfg)
    threading.Thread(target=alert_worker, args=(cfg, wake), daemon=True).start()
    host, port = cfg.get('listen_host', '127.0.0.1'), int(cfg.get('listen_port', 8088))
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    print(f'shmartphone listening on {host}:{port} demo={bool(cfg.get("demo"))}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
