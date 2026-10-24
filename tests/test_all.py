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
import nedarim  # noqa: E402
import store  # noqa: E402

DAY = datetime.date(2026, 10, 23)
PBX = '0765674200'


def make_cfg(db):
    return {'api_key': 'k', 'ivr_token': 'ivrtok', 'event_token': 'evtok', 'public_url': 'https://x.example',
            'secret_key': 's' * 64, 'db_path': db, 'pbx_number': PBX, 'auto_trial_days': 7, 'trial_days': 7,
            'admin_password_hash': admin.hash_password('secretpw1', rounds=1000), 'niqqud': False}


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
        self.assertTrue(store.authorization(mk('active', 'monthly', '2026-10-20'), DAY, True)[0])  # grace
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
        self.assertEqual(self.ask_name(r), 'aconf')  # activate / change / add another / add fallback
        r = self.send(aconf='1')
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
        self.send(apick='2')
        r = self.send(aconf='1')
        self.assertEqual(r[-1]['type'], 'noiseMonitor')

    def test_listen(self):
        sid = self.sub_with()
        with self.con:
            store.add_verified(self.con, sid, '0501112222', 'admin')
        self.assertEqual(self.call('0501112222', mode='listen', call='L0')[-1]['type'], 'hangup')  # nothing active
        self.call('0521234567', call='M1')
        self.send(apick='1')
        self.send(aconf='1')
        r = self.call('0501112222', mode='listen', call='L1')
        self.assertEqual(r[-1], {'type': 'noiseMonitorJoin', 'name': 'join', 'roomId': f's{sid}-0521234567'})
        r = self.send(join='LEFT')  # left right away: the caller is told it did not work
        self.assertIn('לא הצלחנו', r[0]['files'][0]['text'])
        self.assertEqual(r[-1], {'type': 'hangup'})
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
        self.assertIn('7', r[0]['enabledKeys'].split(','))
        r = self.send(menu='3')
        self.assertEqual((r[0]['type'], r[-1]['type'], r[-1]['name']), ('simpleMessage', 'simpleMenu', 'menu'))
        r = self.send(menu='ERROR')  # no key after the menu repeated: the call really ends
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
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'apick': '1', 'aconf': '1'}, DAY)
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

        def fake(cfg, kind, phone, text=None, **kw):
            calls.append((kind, phone))
            return 'sent', {'campaignId': '1'}
        self.assertTrue(core.process_one(self.cfg, self.db, fake, log=lambda *a, **k: None))
        self.assertFalse(core.process_one(self.cfg, self.db, fake, log=lambda *a, **k: None))
        self.assertEqual(calls, [('noise', '0501112222')])

        def boom(cfg, kind, phone, text=None, **kw):
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


class HistoryTests(Base):
    def test_noise_events_recorded_and_history_flow(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'c1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'apick': '1'}, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'apick': '1', 'aconf': '1'}, DAY)
        room = f's{sid}-0521234567'
        now = time.time()
        base = {'time': now, 'roomId': room, 'callId': 'c1', 'num': PBX, 'eventId': 'ev1'}
        with self.con:
            core.handle_event(self.cfg, self.con, {**base, 'event': 'noise', 'level': 70, 'startedAt': now - 300}, 'noise')
            core.handle_event(self.cfg, self.con, {**base, 'event': 'noiseEnd', 'noiseSec': 7.4, 'peakLevel': 81,
                                                   'fileId': 'F1', 'startedAt': now - 300}, 'noiseEnd')
            core.handle_event(self.cfg, self.con, {**base, 'eventId': 'ev2', 'event': 'noiseEnd', 'noiseSec': 2,
                                                   'fileError': True, 'startedAt': now - 60}, 'noiseEnd')
        rows = self.con.execute('SELECT event_id,noise_sec,peak,file_id FROM noise_events ORDER BY started').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('ev1', 7.4, 81, 'F1'), ('ev2', 2.0, None, None)])
        # the verified phone (not a subscriber phone) hears the recordings straight away, newest first
        q = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'h1', 'PBXcallStatus': 'CALL'}

        def go(**k):
            with self.con:
                return ivr.handle(self.cfg, self.con, {**q, **k}, DAY, 'history')[0]
        r = go()
        self.assertEqual(r[0]['type'], 'simpleMenu')
        texts = ' '.join(f.get('text', '') for f in r[0]['files'])
        self.assertIn('נרשמו 2 אירועי רעש', texts)
        self.assertIn('אין הקלטה', texts)  # newest (ev2) has no recording
        self.assertNotIn('בשעה', texts)  # details only on request
        r = go(hnav='NEXT')  # no key -> next (older) = ev1
        self.assertEqual(r[0]['files'], [{'fileId': 'F1'}])
        r = go(hnav='5')  # details of ev1
        det = ' '.join(f.get('text', '') for f in r[0]['files'])
        self.assertIn('בשעה', det)
        self.assertIn('7 שניות', det)
        self.assertIn('81', det)
        r = go(hnav='2')  # replay
        self.assertIn({'fileId': 'F1'}, r[0]['files'])
        r = go(hnav='1')  # back to newer
        self.assertNotIn({'fileId': 'F1'}, r[0]['files'])
        r = go(hnav='1')  # already the newest
        self.assertIn('האחרון שנרשם', ' '.join(f.get('text', '') for f in r[0]['files']))
        r = go(hnav='3')
        r = go(hnav='3')  # past the end
        self.assertEqual(r[-1]['type'], 'hangup')
        with self.con:
            r, _ = ivr.handle(self.cfg, self.con, {**q, 'PBXphone': '0599999999', 'PBXcallId': 'h2'}, DAY, 'history')
        self.assertEqual(r[-1]['type'], 'hangup')  # stranger gets nothing


class VoiceTests(Base):
    def test_voice_only_on_fixed_prompts(self):
        self.cfg['voice'] = 'Charon'
        p = {'PBXphone': '0599999999', 'PBXnum': PBX, 'PBXcallId': 'v1', 'PBXcallStatus': 'CALL'}
        with self.con:
            r, _ = ivr.handle(self.cfg, self.con, p, DAY, 'menu')
        self.assertEqual(r[0]['files'][0].get('voice'), 'Charon')
        with self.con:  # auto-trial welcome contains a number -> stays on the free voice
            r, _ = ivr.handle(self.cfg, self.con, {**p, 'PBXcallId': 'v2'}, DAY, 'activate')
        files = r[0]['files']
        self.assertNotIn('voice', files[0])  # 'welcome_trial' has {days}
        self.assertEqual(files[-1].get('voice'), 'Charon')  # fixed prompt


class PaymentTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg['payments'] = {'monthly': 8.9, 'annual': 82, 'category': 'שמרטפון'}
        self.p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'p1', 'PBXcallStatus': 'CALL'}

    def go(self, mode='activate', **k):
        self.p.update(k)
        with self.con:
            return ivr.handle(self.cfg, self.con, dict(self.p), DAY, mode)[0]

    def sub(self):
        return store.find_by_phone(self.con, '0521234567')

    def test_expired_trial_offered_monthly_keva(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567', plan='trial', status='trial',
                                          expires='2026-10-01')
        r = self.go()
        self.assertEqual(r[0]['name'], 'pplan')
        self.assertIn('הסתיימה', r[0]['files'][0]['text'])
        r = self.go(pplan='1')
        cc = r[-1]
        self.assertEqual((cc['type'], cc['sum'], cc['paymentsMode'], cc['category']),
                         ('creditCard', 8.9, 'kevaMonthly', f'שמרטפון {sid}'))
        r = self.go(payment='OK', CONFIRM_payment='555')
        self.assertEqual(r[-1]['type'], 'hangup')
        s = self.sub()
        self.assertEqual((s['status'], s['plan'], s['billing'], s['expires']), ('active', 'monthly', 'nedarim', '2026-11-23'))
        # Nedarim sync sees the same first charge (same confirmation): no double extension
        rows = [{'TransactionId': '100', 'Amount': '8.9', 'Confirmation': '555', 'Groupe': f'שמרטפון {sid}',
                 'KevaId': '777', 'TransactionTime': '23/10/2026 12:00'}]
        with self.con:
            nedarim.apply_transactions(self.cfg, self.con, rows, log=lambda *a, **k: None)
        self.assertEqual((self.sub()['expires'], self.sub()['keva_id']), ('2026-11-23', '777'))
        # next month's charge matched by KevaId (no category) extends by a month
        rows = [{'TransactionId': '150', 'Amount': '8.9', 'Confirmation': '600', 'Groupe': '', 'KevaId': '777',
                 'TransactionTime': '23/11/2026 08:00'}]
        with self.con:
            last, n = nedarim.apply_transactions(self.cfg, self.con, rows, log=lambda *a, **k: None)
        self.assertEqual((last, n, self.sub()['expires']), (150, 1, '2026-12-23'))
        with self.con:  # same transaction again: ignored
            self.assertEqual(nedarim.apply_transactions(self.cfg, self.con, rows, log=lambda *a, **k: None)[1], 0)

    def test_annual_and_failure_and_enforcement(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567', status='disabled')
        self.go()
        r = self.go(pplan='2')
        self.assertEqual((r[-1]['sum'], r[-1]['paymentsMode']), (82, 'regular'))
        r = self.go(payment='DECLINE')
        self.assertEqual(self.sub()['status'], 'disabled')
        self.p['PBXcallId'] = 'p2'
        for k in ('pplan', 'payment', 'CONFIRM_payment'):
            self.p.pop(k, None)
        self.go()
        self.go(pplan='2')
        self.go(payment='OK', CONFIRM_payment='9')
        s = self.sub()
        self.assertEqual((s['plan'], s['expires']), ('annual', '2027-10-23'))
        # paid through the system: blocked only after paid-until + grace
        later = datetime.date(2027, 10, 27)
        self.assertTrue(store.authorization(s, later)[0])
        self.assertFalse(store.authorization(s, later + datetime.timedelta(days=5))[0])
        # imported (legacy) subscribers are not enforced
        with self.con:
            lid = store.create_subscriber(self.con, main_phone='0539999999', expires='2020-01-01', legacy_id='5')
        self.assertTrue(store.authorization(store.find_by_phone(self.con, '0539999999'), DAY)[0])

    def test_account_menu_offers_payment_and_sync_ignores_others(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
        r = self.go('account')
        self.assertIn('להצטרפות', ' '.join(f.get('text', '') for f in r[0]['files']))
        r = self.go('account', acm='5')
        self.assertEqual(r[0]['name'], 'pplan')
        rows = [{'TransactionId': '7', 'Amount': '100', 'Confirmation': '1', 'Groupe': 'בניין'},
                {'TransactionId': '8', 'Amount': '0', 'Confirmation': '2', 'Groupe': f'שמרטפון {sid}'},
                {'TransactionId': '9', 'Amount': '82', 'Confirmation': '', 'Groupe': f'שמרטפון {sid}'}]
        with self.con:
            self.assertEqual(nedarim.apply_transactions(self.cfg, self.con, rows, log=lambda *a, **k: None), (9, 0))

    def test_sync_pages_and_state(self):
        self.cfg['nedarim'] = {'mosad': '1', 'api_password': 'x', 'start_id': '50'}
        calls = []

        def fake(cfg, last):
            calls.append(last)
            return [] if calls[1:] else [{'TransactionId': '60', 'Amount': '1', 'Confirmation': '1'}]
        self.assertTrue(nedarim.sync(self.cfg, self.db, fetch=fake, log=lambda *a, **k: None))
        nedarim.sync(self.cfg, self.db, fetch=fake, log=lambda *a, **k: None)
        self.assertEqual(calls, ['50', '60'])


class ReconnectTests(Base):
    def test_parent_requests_reconnect_and_room_auto_resumes(self):
        self.cfg.update({'reconnect_extension': '4321', 'reconnect_charge': False})
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        room = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'r1', 'PBXcallStatus': 'CALL'}

        def call(p, mode='activate'):
            with self.con:
                return ivr.handle(self.cfg, self.con, p, DAY, mode)[0]
        call(room)
        call({**room, 'apick': '1'})
        call({**room, 'apick': '1', 'aconf': '1'})
        with self.con:  # the provider cuts the room call
            core.handle_event(self.cfg, self.con, {'event': 'disconnected', 'time': time.time(), 'callId': 'r1',
                                                   'roomId': f's{sid}-0521234567', 'num': PBX, 'reason': 'hangup'},
                              'disconnected')
        self.assertIsNone(self.con.execute("SELECT 1 FROM alerts WHERE kind='reconnect'").fetchone())  # not automatic
        parent = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'L1', 'PBXcallStatus': 'CALL'}
        r = call(parent, 'listen')  # nothing to listen to -> reconnect is offered
        self.assertEqual(r[0]['name'], 'rcon')
        with self.con:
            r, woke = ivr.handle(self.cfg, self.con, {**parent, 'rcon': '1'}, DAY, 'listen')
        self.assertTrue(woke)
        al = self.con.execute("SELECT * FROM alerts WHERE kind='reconnect'").fetchone()
        self.assertEqual((al['alert_phone'], al['call_id']), ('0521234567', 'r1'))
        sent = {}

        class Resp:
            def __init__(self, body):
                self.body = body
            def read(self, n):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            sent.update(json.loads(req.data))
            return Resp(b'{"status":"OK","errorCode":0,"campaignId":"5"}')
        import urllib.request
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = fake_open
        try:
            self.assertEqual(core.send_alert(self.cfg, 'reconnect', '0521234567')[0], 'sent')
        finally:
            urllib.request.OpenerDirector.open = orig
        self.assertEqual((sent['messagesType'], sent['extensionActivation'], sent['phones']),
                         ('extensionActivation', '4321', ['0521234567']))
        self.assertNotIn('audioText', sent)
        with self.con:
            self.con.execute("UPDATE alerts SET state='sent' WHERE id=?", (al['id'],))
        # the room phone answers the outbound call: monitoring resumes with no prompts
        r = call({**room, 'PBXcallId': 'r2', 'PBXcallType': 'out'})
        self.assertEqual(r[-1]['type'], 'noiseMonitor')
        self.assertEqual(r[-1]['roomId'], f's{sid}-0521234567')
        s2 = self.con.execute("SELECT * FROM sessions WHERE call_id='r2'").fetchone()
        self.assertEqual((s2['alert_phone'], s2['subscriber_id']), ('0501112222', sid))
        # an unexpected outbound call is just hung up
        self.assertEqual(call({**room, 'PBXcallId': 'r3', 'PBXcallType': 'out'}), [{'type': 'hangup'}])
        # stranger gets nothing; dedicated extension says it is active now
        self.assertEqual(call({**parent, 'PBXphone': '0599999999', 'PBXcallId': 'x'}, 'reconnect')[-1]['type'], 'hangup')
        r = call({**parent, 'PBXcallId': 'L2'}, 'reconnect')
        self.assertIn('פעיל כרגע', r[0]['files'][0]['text'])


class AlertReconnectTests(Base):
    def setup_dropped(self):
        self.cfg.update({'reconnect_extension': '4321', 'alert_extension': '4322', 'reconnect_charge': False})
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        self.room = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'r1', 'PBXcallStatus': 'CALL'}
        self.call(self.room)
        self.call({**self.room, 'apick': '1'})
        self.call({**self.room, 'apick': '1', 'aconf': '1'})
        with self.con:
            core.handle_event(self.cfg, self.con, {'event': 'disconnected', 'time': time.time(), 'callId': 'r1',
                                                   'roomId': f's{sid}-0521234567', 'num': PBX, 'reason': 'hangup'},
                              'disconnected')
            self.con.execute("UPDATE alerts SET state='sent' WHERE kind='disconnected'")
        self.parent = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'A1', 'PBXcallStatus': 'CALL',
                       'PBXcallType': 'out'}
        return sid

    def call(self, p, mode='activate', now=None):
        with self.con:
            return ivr.handle(self.cfg, self.con, p, DAY, mode, now=now)[0]

    def test_payload_goes_to_alert_extension(self):
        p = {}
        self.cfg['alert_extension'] = '4322'
        c = core.campaign_payload(self.cfg, 'disconnected', '0501112222')
        self.assertIn('audioText', c)  # payload builder unchanged; send_alert switches the type
        import urllib.request
        sent = {}

        class R:
            def read(self, n): return b'{"status":"OK","errorCode":0}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = lambda s, req, timeout=None: (sent.update(json.loads(req.data)), R())[1]
        try:
            core.send_alert(self.cfg, 'disconnected', '0501112222')
        finally:
            urllib.request.OpenerDirector.open = orig
        self.assertEqual((sent['messagesType'], sent['extensionActivation']), ('extensionActivation', '4322'))

    def test_success(self):
        sid = self.setup_dropped()
        r = self.call(self.parent, 'alert')
        self.assertEqual(r[0]['name'], 'alm')
        r = self.call({**self.parent, 'alm': '1'}, 'alert')
        self.assertEqual(r[0]['name'], 'rcw')
        self.assertEqual(self.con.execute("SELECT alert_phone FROM alerts WHERE kind='reconnect'").fetchone()[0],
                         '0521234567')
        r = self.call({**self.parent, 'rcw': 'WAIT'}, 'alert')  # still waiting
        self.assertEqual(r[0]['name'], 'rcw')
        with self.con:
            self.con.execute("UPDATE alerts SET state='sent' WHERE kind='reconnect'")
        r = self.call({**self.room, 'PBXcallId': 'r2', 'PBXcallType': 'out'})  # room auto-answers
        self.assertEqual(r[-1]['type'], 'noiseMonitor')
        r = self.call({**self.parent, 'rcw': 'WAIT'}, 'alert')
        self.assertIn('חודש בהצלחה', r[0]['files'][0]['text'])
        r = self.call({**self.parent, 'rcd': '2'}, 'alert')
        self.assertEqual(r[-1], {'type': 'noiseMonitorJoin', 'name': 'join', 'roomId': f's{sid}-0521234567'})

    def test_failure_after_timeout(self):
        self.setup_dropped()
        self.call(self.parent, 'alert')
        self.call({**self.parent, 'alm': '1'}, 'alert')
        r = self.call({**self.parent, 'rcw': 'WAIT'}, 'alert', now=time.time() + 200)
        self.assertEqual(r[-1]['type'], 'hangup')
        self.assertIn('לא הצלחנו', r[0]['files'][0]['text'])


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


class PingAndDepositTests(Base):
    """Ring-and-drop alerts, call-back notices, stopping alerts, prepaid deposit."""

    def setUp(self):
        super().setUp()
        self.cfg.update({'alert_mode': 'ping', 'reconnect_extension': '12079',
                         'payments': {'monthly': 8.9, 'annual': 82, 'per_minute_agorot': 1}})
        with self.con:
            self.sid = store.create_subscriber(self.con, main_phone='0521234567', plan='trial', status='trial',
                                               expires='2099-01-01')
            store.add_verified(self.con, self.sid, '0501112222', 'admin')
        self.room = f's{self.sid}-0521234567'

    def ivr(self, phone, call, mode='activate', **extra):
        p = {'PBXphone': phone, 'PBXnum': PBX, 'PBXcallId': call, 'PBXcallStatus': 'CALL', **extra}
        with self.con:
            return ivr.handle(self.cfg, self.con, p, DAY, mode)[0]

    def ev(self, kind, now=None, **kw):
        e = {'event': kind, 'time': now or time.time(), 'roomId': self.room, 'callId': 'c1', 'num': PBX, **kw}
        with self.con:
            return core.handle_event(self.cfg, self.con, e, kind, now=now)

    def start_room(self):
        self.ivr('0521234567', 'c1')
        self.ivr('0521234567', 'c1', apick='1')
        return self.ivr('0521234567', 'c1', apick='1', aconf='1')

    def test_start_says_alert_phone(self):
        r = self.start_room()
        self.assertIn({'digits': '0501112222'}, r[0]['files'])
        self.assertNotIn('maxDuration', r[-1])

    def test_ping_payload_and_fallback(self):
        sent = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self, n=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            sent.append((req.full_url, req.data))
            if b'makeCall' in (req.data or b''):
                return Resp(b'{"status":"OK","errorCode":"0","callId":"0510001111"}')
            return Resp(b'{"status":"OK","errorCode":"0","billing":"0.15"}')
        import urllib.request
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = fake_open
        try:
            state, res = core.send_alert(self.cfg, 'noise', '0501112222')
            self.assertEqual(state, 'sent')
            self.assertIn(b'cid=1111', sent[-1][1])
            state, res = core.send_alert(self.cfg, 'noise', '0501112222', mode='message')
            self.assertIn('campaignApi', sent[-1][0])
        finally:
            urllib.request.OpenerDirector.open = orig

    def test_callback_notice_then_menu(self):
        self.start_room()
        now = time.time()
        self.ev('monitorStart', now=now)
        self.ev('noise', now=now + 10, eventId='e1')
        with self.con:
            self.con.execute("UPDATE alerts SET state='sent'")
        r = self.ivr('0501112222', 'p1', 'menu')
        texts = ' '.join(f.get('text', '') for f in r[0]['files'])
        self.assertIn('זוהה רעש', texts)
        self.assertEqual(r[0]['name'], 'menu')
        r = self.ivr('0501112222', 'p2', 'menu')  # heard once only
        self.assertNotIn('זוהה רעש', ' '.join(f.get('text', '') for f in r[0]['files']))

    def test_callback_after_disconnect_offers_reconnect(self):
        self.start_room()
        self.ev('monitorStart')
        self.ev('disconnected', reason='hangup')
        with self.con:
            self.con.execute("UPDATE alerts SET state='sent'")
        r = self.ivr('0501112222', 'p1', 'menu')
        self.assertEqual(r[0]['name'], 'alm')
        r = self.ivr('0501112222', 'p1', 'menu', alm='0')  # back to the main menu
        self.assertEqual(r[0]['name'], 'menu')

    def test_stop_alerts(self):
        self.start_room()
        self.ev('monitorStart')
        r = self.ivr('0501112222', 'p1', 'menu')
        r = self.ivr('0501112222', 'p1', 'menu', menu='8')
        self.assertIn('הופסקה', r[0]['files'][0]['text'])
        self.assertEqual(self.ev('noise', eventId='e9'), (200, 'muted'))

    def test_prepaid_limits_and_charges(self):
        with self.con:
            store.add_credit(self.con, self.sid, 120, source='admin', amount=1.2)  # 1.20 = 120 minutes
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (self.sid,)).fetchone()
        self.assertEqual((sub['plan'], store.authorization(sub, DAY)[0]), ('prepaid', True))
        r = self.start_room()
        self.assertEqual(r[-1]['maxDuration'], 120 * 60 - 10)
        now = time.time()
        self.ev('monitorStart', now=now)
        self.ev('disconnected', now=now + 61, reason='hangup')  # 2 started minutes
        bal = self.con.execute('SELECT balance FROM subscribers WHERE id=?', (self.sid,)).fetchone()[0]
        self.assertEqual(bal, 118)
        with self.con:  # settling twice charges once
            core.settle(self.cfg, self.con, 'c1', now + 500)
        self.assertEqual(self.con.execute('SELECT balance FROM subscribers WHERE id=?', (self.sid,)).fetchone()[0], 118)

    def test_prepaid_runs_out(self):
        with self.con:
            store.add_credit(self.con, self.sid, 2, source='admin', amount=0.02)
        self.start_room()
        now = time.time()
        self.ev('monitorStart', now=now)
        self.ev('disconnected', now=now + 115, reason='hangup')
        kinds = [r[0] for r in self.con.execute('SELECT kind FROM alerts')]
        self.assertEqual(kinds, ['balance'])
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (self.sid,)).fetchone()
        self.assertFalse(store.authorization(sub, DAY)[0])

    def test_paid_message_alert_charges_cost(self):
        with self.con:
            store.add_credit(self.con, self.sid, 500, source='admin', amount=5)
            self.con.execute("UPDATE subscribers SET alert_mode='message' WHERE id=?", (self.sid,))
        self.start_room()
        self.ev('monitorStart')
        self.ev('noise', eventId='e1')
        got = []

        def fake(cfg, kind, phone, text=None, mode=None):
            got.append(mode)
            return 'sent', {'billing': '0.15'}
        core.process_one(self.cfg, self.db, fake, log=lambda *a, **k: None)
        self.assertEqual(got, ['message'])
        self.assertEqual(self.con.execute('SELECT balance FROM subscribers WHERE id=?', (self.sid,)).fetchone()[0], 485)

    def test_deposit_by_phone(self):
        self.ivr('0521234567', 'd1', 'pay')
        r = self.ivr('0521234567', 'd1', 'pay', pplan='3')
        self.assertEqual(r[0]['name'], 'pdep')
        r = self.ivr('0521234567', 'd1', 'pay', pplan='3', pdep='2')
        cc = r[-1]
        self.assertEqual((cc['type'], cc['sum'], cc['paymentsMode']), ('creditCard', 20.0, 'regular'))
        self.assertIn('פיקדון', cc['category'])
        self.ivr('0521234567', 'd1', 'pay', pplan='3', pdep='2', payment='OK', CONFIRM_payment='777')
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (self.sid,)).fetchone()
        self.assertEqual((sub['balance'], sub['plan']), (2000, 'prepaid'))
        with self.con:  # the same charge from the Nedarim sync is not added twice
            nedarim.apply_transactions(self.cfg, self.con, [{'TransactionId': '5', 'Amount': '20', 'Confirmation': '777',
                                                              'Groupe': f'שמרטפון פיקדון {self.sid}'}], log=lambda *a, **k: None)
        self.assertEqual(self.con.execute('SELECT balance FROM subscribers WHERE id=?', (self.sid,)).fetchone()[0], 2000)


class ReconnectChargeTests(Base):
    def test_needs_deposit_and_charges_cost(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
        cfg = {**self.cfg, 'reconnect_extension': '12079'}
        with self.con:
            self.assertEqual(core.queue_reconnect(cfg, self.con, sid, '0521234567', 'c1', '0501112222'),
                             (False, 'no_balance'))
            store.add_credit(self.con, sid, 300, source='admin', amount=3)
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
        self.assertEqual(sub['plan'], 'monthly')  # an active plan is kept; the deposit pays extras
        with self.con:
            self.assertEqual(core.queue_reconnect(cfg, self.con, sid, '0521234567', 'c1', '0501112222'), (True, ''))
        core.process_one(cfg, self.db, lambda *a, **k: ('sent', {'billing': '1.00'}), log=lambda *a, **k: None)
        self.assertEqual(self.con.execute('SELECT balance FROM subscribers WHERE id=?', (sid,)).fetchone()[0], 200)


class BalanceTests(Base):
    def test_hear_balance(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567', plan='free')
            store.add_credit(self.con, sid, 5000, source='admin', amount=50)
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'b1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY, 'account')
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'acm': '7'}, DAY, 'account')[0]
        texts = ' '.join(f.get('text', '') for f in r[0]['files'])
        self.assertIn('50 שקלים', texts)
        self.assertIn('50 חיוגים', texts)


class MessageExtensionTests(Base):
    def test_key9_goes_to_technoline_voicemail(self):
        self.cfg['message_extension'] = '12101'
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'm1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY, 'menu')
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'menu': '9'}, DAY, 'menu')[0]
        self.assertEqual(r, [{'type': 'goTo', 'goTo': '12101'}])


class VoicemailTests(Base):
    def test_record_then_owner_listens_with_code(self):
        self.cfg['voicemail_pin_hash'] = admin.hash_password('12345', rounds=1000)
        caller = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'v1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, caller, DAY, 'menu')
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**caller, 'menu': '9'}, DAY, 'menu')[0]
        self.assertEqual(r[0]['type'], 'record')
        with self.con:
            ivr.handle(self.cfg, self.con, {**caller, 'menu': '9', 'rec': 'f.wav', 'FILEID_rec': 'F1',
                                            'DURATION_rec': '7'}, DAY, 'menu')
        owner = {'PBXphone': '0533333333', 'PBXnum': PBX, 'PBXcallId': 'o1', 'PBXcallStatus': 'CALL'}
        call = lambda **a: ivr.handle(self.cfg, self.con, {**owner, **a}, DAY, 'menu')[0]
        with self.con:
            call()
        with self.con:
            r = call(menu='0')
        self.assertEqual(r[0]['name'], 'vpin')
        with self.con:
            r = call(menu='0', vpin='999')
        self.assertIn('שגוי', ' '.join(f.get('text', '') for f in r[0]['files']))
        with self.con:
            r = call(menu='0', vpin='12345')
        self.assertIn({'fileName': 'f.wav'}, r[0]['files'])
        self.assertIn({'digits': '0521234567'}, r[0]['files'])
        with self.con:
            r = call(menu='0', vpin='12345', vnav='7')
        self.assertEqual(self.con.execute('SELECT state FROM messages').fetchone()[0], 'deleted')

    def test_key0_without_code_configured_just_shows_menu(self):
        p = {'PBXphone': '0533333333', 'PBXnum': PBX, 'PBXcallId': 'o2', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY, 'menu')
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'menu': '0'}, DAY, 'menu')[0]
        self.assertEqual(r[0]['name'], 'menu')


class LastConnectionTests(Base):
    def test_key0_uses_last_alert_phone(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567', alert_phone='0502222222')
            store.add_verified(self.con, sid, '0501111111', 'admin')
            store.add_verified(self.con, sid, '0502222222', 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'l1', 'PBXcallStatus': 'CALL'}
        with self.con:
            r = ivr.handle(self.cfg, self.con, p, DAY)[0]
        self.assertIn('הקישו 0', ' '.join(f.get('text', '') for f in r[-1]['files']))
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'apick': '0'}, DAY)[0]
        self.assertEqual(r[-1]['type'], 'noiseMonitor')
        self.assertEqual(self.con.execute("SELECT alert_phone FROM sessions WHERE call_id='l1'").fetchone()[0],
                         '0502222222')


class MoreNumbersTests(Base):
    def test_extra_and_backup_numbers(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            for ph in ('0501111111', '0502222222', '0503333333'):
                store.add_verified(self.con, sid, ph, 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'c1', 'PBXcallStatus': 'CALL'}
        call = lambda **a: ivr.handle(self.cfg, self.con, {**p, **a}, DAY)[0]
        with self.con:
            call()
        with self.con:
            r = call(apick='1')
        self.assertEqual(r[0]['name'], 'aconf')
        with self.con:
            r = call(apick='1', aconf='3')  # add another number
        self.assertEqual(r[0]['name'], 'amore')
        with self.con:
            r = call(apick='1', aconf='3', amore='1')
        self.assertEqual(r[0]['name'], 'aconf')
        with self.con:
            call(apick='1', aconf='4')
        with self.con:
            r = call(apick='1', aconf='4', amore='1')  # fallback = the remaining number
        texts = [f.get('digits') for f in r[0]['files'] if f.get('digits')]
        self.assertEqual(len(texts), 3)
        with self.con:
            r = call(apick='1', aconf='1')
        self.assertEqual(r[-1]['type'], 'noiseMonitor')
        room = r[-1]['roomId']
        e = {'event': 'monitorStart', 'time': time.time(), 'roomId': room, 'callId': 'c1', 'num': PBX}
        with self.con:
            core.handle_event(self.cfg, self.con, e, 'monitorStart')
            core.handle_event(self.cfg, self.con, {**e, 'event': 'noise', 'eventId': 'n1'}, 'noise')
        phones = sorted(r[0] for r in self.con.execute("SELECT alert_phone FROM alerts WHERE kind='noise'"))
        self.assertEqual(len(phones), 2)  # main + additional
        sends = []

        def fail(cfg, kind, phone, text=None, mode=None):
            sends.append(phone)
            return 'failed', {}
        while core.process_one(self.cfg, self.db, fail, log=lambda *a, **k: None):
            pass
        self.assertEqual(len(sends), 3)  # main failed -> the fallback number got it too


class NiqqudTests(Base):
    def test_spoken_texts_are_vowel_marked(self):
        cfg = {**self.cfg, 'niqqud': True}
        self.assertIn('הַקִּישׁוּ', ivr.t(cfg, 'menu'))
        self.assertEqual(ivr.t(cfg, 'notice_noise_one', hh='23:40'), 'בְּשָׁעָה 23:40 זֻהָה רַעַשׁ בְּחֶדֶר הַתִּינוֹק.')
        self.assertIn('שְׁמַרְטַפוֹן', core.text(cfg, 'alert_noise'))
        self.assertEqual(ivr.t(cfg, 'alerts_menu', mode='x')[:3], 'סוג')  # missing key: plain text
