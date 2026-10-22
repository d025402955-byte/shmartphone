import base64
import datetime
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import admin  # noqa: E402
import core  # noqa: E402
import importer  # noqa: E402
import ivr  # noqa: E402
import store  # noqa: E402

DAY = datetime.date(2026, 10, 23)
PBX = '0765674200'


def make_cfg(db):
    return {'api_key': 'k', 'ivr_token': 'ivrtok', 'event_token': 'evtok', 'public_url': 'https://x.example',
            'secret_key': 's' * 64, 'db_path': db, 'pbx_number': PBX, 'auto_trial_days': 7, 'trial_days': 7,
            'admin_password_hash': admin.hash_password('secretpw1', rounds=1000)}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, 'a.db')
        store.init(self.db)
        self.cfg = make_cfg(self.db)
        self.con = store.connect(self.db)

    def tearDown(self):
        self.con.close()



class StoreTests(Base):
    def test_norm_phone(self):
        self.assertEqual(store.norm_phone('972-52-1234567'), '0521234567')
        self.assertEqual(store.norm_phone('521234567.0'), '0521234567')
        self.assertEqual(store.norm_phone('025611562'), '025611562')

    def test_authorization(self):
        mk = lambda st, plan, exp: {'status': st, 'plan': plan, 'expires': exp}
        self.assertTrue(store.authorization(mk('active', 'monthly', '2020-01-01'), DAY)[0])
        self.assertFalse(store.authorization(mk('active', 'monthly', '2020-01-01'), DAY, True)[0])
        self.assertTrue(store.authorization(mk('trial', 'trial', '2026-10-23'), DAY)[0])
        self.assertFalse(store.authorization(mk('trial', 'trial', '2026-10-22'), DAY)[0])
        self.assertFalse(store.authorization(mk('disabled', 'monthly', None), DAY)[0])
        self.assertFalse(store.authorization(mk('cancelled', 'monthly', None), DAY)[0])

    def test_extend(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521111111', expires='2026-12-31')
            self.assertEqual(store.extend(self.con, sid, 1, DAY).isoformat(), '2027-01-31')
            sid2 = store.create_subscriber(self.con, main_phone='0521111112', expires='2026-01-31')
            self.assertEqual(store.extend(self.con, sid2, 12, DAY).isoformat(), '2027-10-23')

    def test_unique_phones(self):
        with self.con:
            a = store.create_subscriber(self.con, main_phone='0521111111')
            store.add_phone(self.con, a, '0532222222')
            with self.assertRaises(ValueError):
                store.create_subscriber(self.con, main_phone='0532222222')
            self.assertEqual(store.find_by_phone(self.con, '0532222222')['id'], a)


class IvrTests(Base):
    """Simulates Technoline: parameters accumulate during a call; each answer adds/overwrites one."""

    def call(self, phone, mode='activate', call='c1'):
        self.params = {'PBXphone': phone, 'PBXnum': PBX, 'PBXcallId': call, 'PBXcallStatus': 'CALL'}
        self.mode = mode
        return self.send()

    def send(self, **answer):
        self.params.update(answer)
        with self.con:
            resp, self.woke = ivr.handle(self.cfg, self.con, dict(self.params), DAY, self.mode)
        return resp

    def types(self, r):
        return [c['type'] for c in r]

    def ask_name(self, r):
        return [c for c in r if c['type'] == 'getDTMF'][-1]['name']

    def sub_with(self, phone='0521234567', **kw):
        with self.con:
            return store.create_subscriber(self.con, main_phone=phone, **kw)

    def test_wrong_system_rejected(self):
        p = {'PBXphone': '0521234567', 'PBXnum': '0999', 'PBXcallId': 'x', 'PBXcallStatus': 'CALL'}
        self.assertEqual(ivr.handle(self.cfg, self.con, p, DAY)[0], [{'type': 'hangup'}])

    def test_unknown_gets_trial_and_must_verify_first(self):
        r = self.call('0521234567')
        self.assertEqual(self.ask_name(r), 'vnum')
        sub = store.find_by_phone(self.con, '0521234567')
        self.assertEqual((sub['status'], sub['expires']), ('trial', '2026-10-30'))

    def test_disabled_rejected_and_no_auto_trial(self):
        self.sub_with(status='disabled')
        self.assertEqual(self.call('0521234567')[-1]['type'], 'hangup')
        self.cfg['auto_trial_days'] = 0
        self.assertEqual(self.call('0539999999', call='c2')[-1]['type'], 'hangup')
        self.assertIsNone(store.find_by_phone(self.con, '0539999999'))

    def test_activate_requires_verified_then_code_flow(self):
        sid = self.sub_with()
        r = self.call('0521234567')
        self.assertEqual(self.ask_name(r), 'vnum')
        r = self.send(vnum='0501112222')
        self.assertEqual(self.ask_name(r), 'vcode')
        self.assertTrue(self.woke)
        al = self.con.execute("SELECT * FROM alerts WHERE kind='verify'").fetchone()
        code = self.con.execute('SELECT code FROM verifications').fetchone()[0]
        self.assertEqual(al['alert_phone'], '0501112222')
        self.assertIn(', '.join(code), json.loads(al['payload'])['text'])
        r = self.send(vcode='0000' if code != '0000' else '1111')
        self.assertEqual(self.ask_name(r), 'vcode')
        r = self.send(vcode=code)  # correct -> continues to activate with one choice
        self.assertEqual(self.ask_name(r), 'apick')
        self.assertEqual(store.verified_phones(self.con, sid), ['0501112222'])
        r = self.send(apick='1')
        self.assertEqual(self.types(r), ['simpleMessage', 'noiseMonitor'])
        self.assertEqual(r[-1]['roomId'], f's{sid}-0521234567')
        self.assertEqual(r[-1]['alertUrl'], 'https://x.example/events/evtok')
        r = self.send(nm='BUSY')
        self.assertEqual(r[-1]['type'], 'hangup')

    def test_verify_by_calling_from_that_phone(self):
        sid = self.sub_with()
        with self.con:
            store.add_phone(self.con, sid, '0533333333')
        self.call('0533333333', mode='verify')
        r = self.send(vnum='0533333333')
        self.assertEqual(r[-1]['type'], 'hangup')
        self.assertEqual(store.verified_phones(self.con, sid), ['0533333333'])
        self.assertIsNone(self.con.execute("SELECT 1 FROM alerts WHERE kind='verify'").fetchone())

    def test_pending_code_on_later_call_and_rate_limit(self):
        sid = self.sub_with()
        self.call('0521234567', mode='verify')
        self.send(vnum='0501112222')
        code = self.con.execute('SELECT code FROM verifications').fetchone()[0]
        r = self.send(vcode='SKIP')
        self.assertEqual(r[-1]['type'], 'hangup')  # "enter later"
        r = self.call('0521234567', mode='verify', call='c2')
        self.assertEqual(self.ask_name(r), 'vcode')
        r = self.send(vcode=code)
        self.assertEqual(store.verified_phones(self.con, sid), ['0501112222'])
        for i in range(2):  # limit is 3 code calls per hour, the first one included
            r = self.call('0521234567', mode='verify', call=f'r{i}')
            if self.ask_name(r) == 'vcode':  # a pending code is offered first; '#' = verify another number
                r = self.send(vcode='SKIP')
            self.assertEqual(self.ask_name(r), 'vnum')
            self.send(vnum=f'05011100{i}0')
        r = self.call('0521234567', mode='verify', call='r9')
        self.send(vcode='SKIP')
        self.assertEqual(self.send(vnum='0501119999')[-1]['type'], 'hangup')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM verifications').fetchone()[0], 3)

    def test_pick_among_several(self):
        sid = self.sub_with()
        with self.con:
            store.add_verified(self.con, sid, '0501111111', 'admin')
            store.add_verified(self.con, sid, '0502222222', 'admin')
            store.add_verified(self.con, sid, '0521234567', 'admin')  # caller itself is excluded
        r = self.call('0521234567')
        self.assertEqual(sum(1 for f in r[0]['files'] if 'digits' in f), 2)
        r = self.send(apick='9')
        self.assertEqual(self.ask_name(r), 'vnum')
        self.call('0521234567', call='c2')
        r = self.send(apick='2')
        self.assertEqual(r[-1]['type'], 'noiseMonitor')

    def test_listen(self):
        sid = self.sub_with()
        with self.con:
            store.add_verified(self.con, sid, '0501112222', 'admin')
        self.assertEqual(self.call('0501112222', mode='listen', call='L0')[-1]['type'], 'hangup')  # nothing active
        self.call('0521234567', call='M1')
        self.send(apick='1')
        r = self.call('0501112222', mode='listen', call='L1')
        self.assertEqual(r[-1], {'type': 'noiseMonitorJoin', 'name': 'join', 'roomId': f's{sid}-0521234567'})
        self.assertEqual(self.send(join='LEFT'), [{'type': 'hangup'}])
        self.assertEqual(self.call('0599999999', mode='listen', call='L2')[-1]['type'], 'hangup')  # stranger

    def test_phones_menu(self):
        sid = self.sub_with()
        self.call('0521234567', mode='phones')
        r = self.send(phm='1')
        self.assertEqual(self.ask_name(r), 'phn')
        r = self.send(phn='0537777777')
        self.assertEqual(self.ask_name(r), 'phm')
        self.assertEqual(store.find_by_phone(self.con, '0537777777')['id'], sid)
        r = self.send(phm='3')
        self.assertEqual(self.ask_name(r), 'phr')
        r = self.send(phr='1')
        self.assertIsNone(store.find_by_phone(self.con, '0537777777'))
        self.assertEqual(self.send(phm='0')[-1]['type'], 'hangup')

    def test_account(self):
        sid = self.sub_with(status='disabled', expires='2026-12-01')
        with self.con:
            store.add_verified(self.con, sid, '0501112222', 'admin')
        self.call('0521234567', mode='account')
        r = self.send(acm='1')
        spoken = ' '.join(f.get('text', '') for c in r for f in c.get('files', []))
        self.assertIn('מושבת', spoken)
        self.assertIn('1 בדצמבר 2026', spoken)
        r = self.send(acm='2')
        self.assertEqual(self.ask_name(r), 'acr')
        self.send(acr='1')
        self.assertEqual(store.verified_phones(self.con, sid), [])
        r = self.send(acm='4')  # disabled -> cannot verify
        self.assertEqual(self.ask_name(r), 'acm')

    def test_menu_info_and_message(self):
        r = self.call('0599999999', mode='menu')
        self.assertEqual(r[0]['type'], 'simpleMenu')
        r = self.send(menu='3')
        self.assertEqual(r[-1]['type'], 'hangup')
        self.call('0599999999', mode='message', call='m2')
        r = self.send()
        self.assertEqual(r[0]['type'], 'record')
        r = self.send(rec='file1', FILEID_rec='777', FILE_rec='file1', DURATION_rec='12')
        self.assertEqual(r[-1]['type'], 'hangup')
        m = self.con.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((m['file_id'], m['state'], m['duration']), ('777', 'stored', 12.0))

    def test_message_saved_on_hangup(self):
        self.call('0599999999', mode='message')
        self.send(PBXcallStatus='HANGUP', FILEID_rec='888', DURATION_rec='5')
        self.assertEqual(self.con.execute('SELECT file_id FROM messages').fetchone()[0], '888')


class EventTests(Base):
    def start(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'c1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'apick': '1'}, DAY)
        return sid

    def ev(self, kind, **kw):
        e = {'event': kind, 'time': time.time(), 'roomId': self.room, 'callId': 'c1', 'num': PBX, **kw}
        with self.con:
            return core.handle_event(self.cfg, self.con, e, kind)

    def test_noise_queues_alert_with_cooldown_and_dedupe(self):
        sid = self.start()
        self.room = f's{sid}-0521234567'
        self.assertEqual(self.ev('noise', eventId='e1'), (200, 'queued'))
        self.assertEqual(self.ev('noise', eventId='e1'), (200, 'duplicate'))
        self.assertEqual(self.ev('noise', eventId='e2'), (200, 'cooldown'))
        self.assertEqual(self.ev('disconnected', reason='hangup'), (200, 'queued'))
        rows = self.con.execute('SELECT kind,state,alert_phone FROM alerts ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('noise', 'pending', '0501112222'),
                                                    ('noise', 'skipped', '0501112222'),
                                                    ('disconnected', 'pending', '0501112222')])

    def test_rejects_bad_events(self):
        sid = self.start()
        self.room = f's{sid}-0521234567'
        e = {'event': 'noise', 'time': time.time(), 'roomId': self.room, 'callId': 'c1', 'num': PBX}
        self.assertEqual(core.handle_event(self.cfg, self.con, e, 'level')[0], 400)
        self.assertEqual(core.handle_event(self.cfg, self.con, {**e, 'num': '1'}, 'noise')[0], 403)
        self.assertEqual(core.handle_event(self.cfg, self.con, {**e, 'time': 1}, 'noise')[0], 400)
        self.assertEqual(core.handle_event(self.cfg, self.con, {**e, 'roomId': 'other'}, 'noise')[0], 404)

    def test_worker_sends(self):
        sid = self.start()
        self.room = f's{sid}-0521234567'
        self.ev('noise', eventId='e1')
        self.con.commit()
        calls = []

        def fake(cfg, kind, phone, text=None):
            calls.append((kind, phone))
            return 'sent', {'campaignId': '1'}
        self.assertTrue(core.process_one(self.cfg, self.db, fake, log=lambda *a, **k: None))
        self.assertFalse(core.process_one(self.cfg, self.db, fake, log=lambda *a, **k: None))
        self.assertEqual(calls, [('noise', '0501112222')])

        def boom(cfg, kind, phone, text=None):
            raise TimeoutError()
        self.ev('disconnected')
        self.con.commit()
        core.process_one(self.cfg, self.db, boom, log=lambda *a, **k: None)
        st = self.con.execute("SELECT state FROM alerts WHERE kind='disconnected'").fetchone()[0]
        self.assertEqual(st, 'unknown')

    def test_payload(self):
        p = core.campaign_payload({**self.cfg, 'demo': True}, 'noise', '0501112222')
        self.assertEqual(p['phones'], ['0501112222'])
        self.assertEqual(p['demo'], 'yes')
        self.assertEqual(p['audioText'], core.DEFAULT_TEXTS['alert_noise'])


class ImportTests(Base):
    SUBS_CSV = ('ID,מזהה שירות,טלפון לקוח ראשי,סטטוס מנוי,סכום לחיוב,סוג חיוב,תאריך החיוב הבא,תאריך תפוגת המנוי,טוקן,Cvv\n'
                '7,2,0533157319,תקופת נסיון,(לא הוגדר),חינמי,,2024-02-25,SECRETTOKEN,123\n'
                '8,2,533187195,פעיל,8.9,חודשי,2026-11-20,2026-11-20,SECRETTOKEN,456\n'
                '9,2,0556758504,מבוטל,8.9,חודשי,,2024-09-03,,\n'
                '10,2,025777932,חינמי,,חינמי,,2030-01-01,,\n')
    PHONES_CSV = ('ID,שירות,מנוי,טלפון,שם,תאריך ושעת יצירה\n'
                  '1,2 - שמרטפון,8 - 0533187195,0556713102,recordPhonesNames/001.wav,2024-02-21\n'
                  '2,2 - שמרטפון,99 - 0599999999,0556713103,x,2024-02-21\n')

    def test_import_csv(self):
        with self.con:
            st = importer.import_subscribers(self.con, importer.read_table(self.SUBS_CSV.encode('utf-8-sig')))
        self.assertEqual((st['created'], st['updated']), (4, 0))
        a = store.find_by_phone(self.con, '0533187195')
        self.assertEqual((a['status'], a['plan'], a['amount'], a['expires'], a['legacy_id']),
                         ('active', 'monthly', 8.9, '2026-11-20', '8'))
        self.assertEqual(store.find_by_phone(self.con, '0556758504')['status'], 'cancelled')
        free = store.find_by_phone(self.con, '025777932')
        self.assertEqual((free['status'], free['plan']), ('active', 'free'))
        dump = json.dumps([dict(r) for r in self.con.execute('SELECT * FROM subscribers')], ensure_ascii=False)
        self.assertNotIn('SECRETTOKEN', dump)
        with self.con:
            st = importer.import_subscribers(self.con, importer.read_table(self.SUBS_CSV.encode('utf-8-sig')))
        self.assertEqual((st['created'], st['updated']), (0, 4))
        with self.con:
            st = importer.import_phones(self.con, importer.read_table(self.PHONES_CSV.encode()))
        self.assertEqual(st['created'], 1)
        self.assertEqual(len(st['errors']), 1)
        self.assertEqual(store.find_by_phone(self.con, '0556713102')['main_phone'], '0533187195')

    def test_html_table(self):
        html = ('<table><tr><th>ID</th><th>טלפון לקוח ראשי</th><th>סטטוס מנוי</th></tr>'
                '<tr><td>5</td><td>0521234567</td><td>פעיל</td></tr></table>')
        rows = importer.read_table(html.encode())
        self.assertEqual(rows[0]['טלפון לקוח ראשי'], '0521234567')

    def test_real_xls_if_present(self):
        d = '/mnt/user-data/attachments/67adef3d-f472-5016-9907-cac62bc97454'
        if not os.path.isdir(d):
            self.skipTest('no sample files')
        files = sorted(os.listdir(d))
        subs = [f for f in files if f.startswith('3c644479')]
        phones = [f for f in files if f.startswith('7826520e')]
        if not subs or not phones:
            self.skipTest('no sample files')
        with self.con:
            s1 = importer.import_subscribers(self.con, importer.read_table(open(os.path.join(d, subs[0]), 'rb').read()))
            s2 = importer.import_phones(self.con, importer.read_table(open(os.path.join(d, phones[0]), 'rb').read()), 50)
        total = self.con.execute('SELECT COUNT(*) FROM subscribers').fetchone()[0]
        print('\nreal import:', {k: v for k, v in s1.items() if k != 'errors'}, len(s1['errors']), 'errors;',
              {k: v for k, v in s2.items() if k != 'errors'}, len(s2['errors']), 'errors; total', total)
        for e in (s1['errors'] + s2['errors'])[:10]:
            print('  ', e.split('(')[0], e.split(':')[-1])
        cols = [r[1] for r in self.con.execute('PRAGMA table_info(subscribers)')]
        self.assertNotIn('token', ' '.join(cols).lower())
        self.assertGreater(total, 1200)


class AdminTests(Base):
    def auth(self, pw='secretpw1'):
        return {'Authorization': 'Basic ' + base64.b64encode(f'admin:{pw}'.encode()).decode()}

    def test_auth(self):
        self.assertTrue(admin.authorized(self.cfg, self.auth(), '1.1.1.1'))
        self.assertFalse(admin.authorized(self.cfg, self.auth('wrong'), '1.1.1.2'))
        self.assertFalse(admin.authorized(self.cfg, {}, '1.1.1.3'))
        for _ in range(10):
            admin.authorized(self.cfg, self.auth('wrong'), '9.9.9.9')
        self.assertFalse(admin.authorized(self.cfg, self.auth(), '9.9.9.9'))  # locked out

    def post(self, path, **fields):
        fields.setdefault('csrf', admin.csrf_token(self.cfg))
        body = '&'.join(f'{k}={v}' for k, v in fields.items()).encode()
        return admin.handle(self.cfg, self.con, 'POST', path, '', {'Content-Type': 'application/x-www-form-urlencoded'},
                            body, DAY)

    def test_crud(self):
        st, _, out, hdr = self.post('/admin/new', name='abc', main_phone='0521234567', plan='monthly', status='active')
        self.assertEqual(st, 303)
        sid = int(hdr['Location'].split('/')[3].split('?')[0])
        self.assertEqual(store.find_by_phone(self.con, '0521234567')['expires'], '2026-11-23')
        st, *_ = self.post(f'/admin/sub/{sid}/action', op='pay_year')
        self.assertEqual(store.find_by_phone(self.con, '0521234567')['expires'], '2027-11-23')
        self.post(f'/admin/sub/{sid}/action', op='add_phone', phone='0539999999')
        self.assertEqual(store.find_by_phone(self.con, '0539999999')['id'], sid)
        self.post(f'/admin/sub/{sid}/action', op='disable')
        self.assertEqual(store.find_by_phone(self.con, '0521234567')['status'], 'disabled')
        st, _, out, _ = admin.handle(self.cfg, self.con, 'GET', '/admin', 'q=0521234567', {}, b'', DAY)
        self.assertIn('0521234567', out.decode())
        st, _, out, _ = admin.handle(self.cfg, self.con, 'GET', f'/admin/sub/{sid}', '', {}, b'', DAY)
        self.assertEqual(st, 200)
        self.assertEqual(admin.handle(self.cfg, self.con, 'GET', '/admin/activity', '', {}, b'', DAY)[0], 200)
        self.assertIn(b'0521234567', admin.handle(self.cfg, self.con, 'GET', '/admin/export.csv', '', {}, b'', DAY)[2])
        st, *_ = self.post(f'/admin/sub/{sid}/action', op='delete')
        self.assertIsNone(store.find_by_phone(self.con, '0521234567'))

    def test_csrf_and_escaping(self):
        st, *_ = self.post('/admin/new', csrf='bad', main_phone='0521234567')
        self.assertEqual(st, 403)
        self.post('/admin/new', name='<script>x</script>', main_phone='0521234567')
        out = admin.handle(self.cfg, self.con, 'GET', '/admin', '', {}, b'', DAY)[2].decode()
        self.assertNotIn('<script>x', out)

    def test_multipart_import(self):
        b = 'XyZb'
        body = (f'--{b}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{admin.csrf_token(self.cfg)}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nsubs\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="s.csv"\r\n'
                f'Content-Type: text/csv\r\n\r\n').encode() + ImportTests.SUBS_CSV.encode() + f'\r\n--{b}--\r\n'.encode()
        st, _, out, _ = admin.handle(self.cfg, self.con, 'POST', '/admin/import', '',
                                     {'Content-Type': f'multipart/form-data; boundary={b}'}, body, DAY)
        self.assertEqual(st, 200)
        self.assertIn('נוצרו 4', out.decode())


if __name__ == '__main__':
    unittest.main()


class EmailTests(Base):
    def test_message_email_worker(self):
        self.cfg['smtp'] = {'host': 'h', 'user': 'u', 'password': 'p', 'to': 't@x'}
        with self.con:
            self.con.execute("INSERT INTO messages(phone,file_id,duration,created,state) VALUES('0521',?,3,?,'pending')",
                             ('9', time.time()))
        sent = []
        ok = core.process_message(self.cfg, self.db, fetch=lambda c, f: b'MP3', mail=lambda c, r, a: sent.append(a),
                                  log=lambda *a, **k: None)
        self.assertTrue(ok)
        self.assertEqual(sent, [b'MP3'])
        self.assertEqual(self.con.execute('SELECT state FROM messages').fetchone()[0], 'emailed')
        del self.cfg['smtp']
        self.assertFalse(core.process_message(self.cfg, self.db))
