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
import report  # noqa: E402
import store  # noqa: E402

DAY = datetime.date(2026, 10, 23)


def spoken(cmd):
    return ' '.join(f.get('text') or ' '.join(f.get('digits', '')) for f in cmd.get('files', []))


def sp(phone):
    return ' '.join(phone)
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

    def test_inactive_phone_can_become_extra_phone(self):
        with self.con:
            parent = store.create_subscriber(self.con, main_phone='0521111111')
            other = store.create_subscriber(self.con, main_phone='0529999999')  # active: stays its own
            trial = store.create_subscriber(self.con, main_phone='0533333333', plan='trial', status='trial',
                                            expires='2099-01-01')
            old = store.create_subscriber(self.con, main_phone='0544444444', status='cancelled')
            store.add_phone(self.con, old, '0545555555')
            with self.assertRaises(ValueError):
                store.add_phone(self.con, parent, '0529999999')
            store.add_phone(self.con, parent, '0533333333')
            store.add_phone(self.con, parent, '0545555555')
        for p in ('0533333333', '0545555555'):
            self.assertEqual(store.find_by_phone(self.con, p)['id'], parent)
        t = self.con.execute('SELECT * FROM subscribers WHERE id=?', (trial,)).fetchone()
        self.assertEqual((t['main_phone'], t['status']), (f'0533333333-old{trial}', 'cancelled'))
        self.assertEqual(store.find_by_phone(self.con, '0544444444')['id'], old)  # its main phone is untouched
        self.assertEqual(store.find_by_phone(self.con, '0529999999')['id'], other)

    def test_renew_trials(self):
        with self.con:
            ids = {s: store.create_subscriber(self.con, main_phone=p, status=st, plan=pl, expires=ex)
                   for s, p, st, pl, ex in (('cancelled', '0521000001', 'cancelled', 'monthly', '2026-01-01'),
                                            ('disabled', '0521000002', 'disabled', 'annual', None),
                                            ('expired', '0521000003', 'trial', 'trial', '2026-10-01'),
                                            ('paid', '0521000004', 'active', 'monthly', '2099-01-01'),
                                            ('free', '0521000005', 'active', 'free', None),
                                            ('trial', '0521000006', 'trial', 'trial', '2099-01-01'))}
            self.assertEqual(store.renewable_count(self.con, DAY), 3)
            self.assertEqual(store.renew_trials(self.con, DAY), 3)
        rows = {r['id']: r for r in self.con.execute('SELECT * FROM subscribers')}
        for k in ('cancelled', 'disabled', 'expired'):
            self.assertEqual((rows[ids[k]]['status'], rows[ids[k]]['expires']), ('trial', None))
        self.assertEqual(rows[ids['paid']]['status'], 'active')
        self.assertEqual(rows[ids['trial']]['expires'], '2099-01-01')
        self.assertIn('ניסיון חדש לכל', admin.list_page(self.cfg, self.con, {}, DAY))

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
        self.assertEqual(self.ask_name(r), 'anum')
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
        self.assertEqual(self.ask_name(r), 'anum')
        r = self.send(anum='0501112222')  # not verified yet: a code is sent first
        self.assertEqual(self.ask_name(r), 'vcode')
        self.assertTrue(self.woke)
        al = self.con.execute("SELECT * FROM alerts WHERE kind='verify'").fetchone()
        code = self.con.execute('SELECT code FROM verifications').fetchone()[0]
        self.assertEqual(al['alert_phone'], '0501112222')
        self.assertIn(', '.join(code), json.loads(al['payload'])['text'])
        r = self.send(vcode='0000' if code != '0000' else '1111')
        self.assertEqual(self.ask_name(r), 'vcode')
        r = self.send(vcode=code)  # correct -> straight to activate / change / add
        self.assertEqual(store.verified_phones(self.con, sid), ['0501112222'])
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
        self.assertNotIn(sp('0501111111'), spoken(r[0]))  # numbers are not read out
        r = self.send(anum='0539999999')  # an unverified number -> verification code
        self.assertEqual(self.ask_name(r), 'vcode')
        self.call('0521234567', call='c2')
        r = self.send(anum='0502222222')
        self.assertEqual(self.ask_name(r), 'aconf')
        r = self.send(aconf='1')
        self.assertEqual(r[-1]['type'], 'noiseMonitor')

    def test_listen(self):
        sid = self.sub_with()
        with self.con:
            store.add_verified(self.con, sid, '0501112222', 'admin')
        self.assertEqual(self.call('0501112222', mode='listen', call='L0')[-1]['type'], 'hangup')  # nothing active
        self.call('0521234567', call='M1')
        self.send(anum='0501112222')
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
        r = self.send(acm='4')  # no verification here any more (only during activation)
        self.assertEqual(r[-1]['type'], 'hangup')

    def test_menu_info_and_message(self):
        r = self.call('0599999999', mode='menu')
        self.assertEqual(r[0]['type'], 'simpleMenu')
        self.assertNotIn('7', r[0]['enabledKeys'].split(','))  # 7 and 8 moved under 2
        r = self.send(menu='3')
        self.assertEqual((r[0]['type'], r[-1]['type'], r[-1]['name']), ('simpleMessage', 'simpleMenu', 'menu'))
        r = self.send(menu='ERROR')  # no key after the menu repeated: the call really ends
        self.assertEqual(r[-1]['type'], 'hangup')
        self.call('0599999999', mode='message', call='m2')
        r = self.send()
        self.assertEqual(r[0]['type'], 'record')
        self.assertEqual(r[0]['confirm'], 'no')  # saved on '#': no confirm step to hang up in
        r = self.send(rec='file1', FILEID_rec='777', FILE_rec='file1', DURATION_rec='12')
        self.assertEqual(r[-1]['type'], 'hangup')
        m = self.con.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((m['file_id'], m['state'], m['duration']), ('777', 'stored', 12.0))

    def test_message_saved_on_hangup(self):
        self.call('0599999999', mode='message')
        self.send(PBXcallStatus='HANGUP', FILEID_rec='888', DURATION_rec='5')
        self.assertEqual(self.con.execute('SELECT file_id FROM messages').fetchone()[0], '888')
        self.assertIsNone(self.con.execute("SELECT 1 FROM alerts WHERE kind='message'").fetchone())  # not configured

    def test_new_message_pings_the_owner(self):
        self.cfg.update({'message_notify_phone': '0533155059', 'alert_mode': 'voice'})
        self.call('0599999999', mode='message', call='m3')
        self.send()
        self.send(rec='file1', FILEID_rec='779', FILE_rec='file1', DURATION_rec='9')
        a = self.con.execute("SELECT * FROM alerts WHERE kind='message'").fetchone()
        self.assertEqual((a['alert_phone'], a['state']), ('0533155059', 'pending'))
        sent = []
        orig = core.send_ping
        core.send_ping = lambda cfg, kind, phone: (sent.append((cfg['alert_mode'], kind, phone)) or ('sent', {}))
        try:
            self.assertTrue(core.process_one(self.cfg, self.db, log=lambda *a, **k: None))
        finally:
            core.send_ping = orig
        self.assertEqual(sent, [('ping', 'message', '0533155059')])  # a ring-and-drop even when alerts are 'voice'
        import urllib.request
        body = {}

        class R:
            def read(self, n): return b'{"status":"OK","errorCode":0}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = lambda s, req, timeout=None: (
            body.update(urllib.parse.parse_qsl(req.data.decode())), R())[1]
        try:
            core.send_ping({**self.cfg, 'alert_mode': 'ping'}, 'message', '0533155059')
        finally:
            urllib.request.OpenerDirector.open = orig
        self.assertEqual((body['cid'], body['apiKey']), ('4444', 'k'))  # the API key is required (HTTP 401 without)
        self.assertEqual(self.con.execute("SELECT state FROM alerts WHERE kind='message'").fetchone()[0], 'sent')


class EventTests(Base):
    def start(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'c1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222'}, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222', 'aconf': '1'}, DAY)
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
        self.assertEqual((st['created'], st['updated'], st['unchanged']), (0, 0, 4))  # nothing changed in the file
        with self.con:
            st = importer.import_phones(self.con, importer.read_table(self.PHONES_CSV.encode()))
        self.assertEqual(st['created'], 1)
        self.assertEqual(len(st['errors']), 1)
        self.assertEqual(store.find_by_phone(self.con, '0556713102')['main_phone'], '0533187195')

    def test_reimport_keeps_payers_here_and_never_blocks_trials(self):
        with self.con:
            importer.import_subscribers(self.con, importer.read_table(self.SUBS_CSV.encode('utf-8-sig')))
        trial = store.find_by_phone(self.con, '0533157319')
        self.assertEqual((trial['status'], trial['expires']), ('trial', None))  # starts on the next call
        paid = store.find_by_phone(self.con, '0533187195')
        with self.con:
            store.record_payment(self.con, paid['id'], source='phone', months=1, amount=8.9, day=DAY)
            other = store.create_subscriber(self.con, main_phone='0521110000', plan='trial', status='trial',
                                            legacy_id='11')
            self.con.execute("UPDATE subscribers SET expires='2025-01-01' WHERE id=?", (other,))  # expired here
        expires_paid = store.find_by_phone(self.con, '0533187195')['expires']
        newer = ('ID,טלפון לקוח ראשי,סטטוס מנוי,סכום לחיוב,סוג חיוב,תאריך תפוגת המנוי\n'
                 '7,0533157319,מושבת,8.9,חודשי,2024-03-01\n'      # open trial here: stays a trial
                 '8,0533187195,מושבת,8.9,חודשי,2026-11-20\n'      # pays through this system: kept
                 '9,0556758504,פעיל,8.9,חודשי,2026-12-01\n'       # renewed in the old system: updated
                 '11,0521110000,תקופת נסיון,,חינמי,2025-01-01\n'  # expired trial here, trial in the file: renewed
                 '12,0521110001,תקופת נסיון,,חינמי,2024-01-01\n')  # new trial
        with self.con:
            st = importer.import_subscribers(self.con, importer.read_table(newer.encode('utf-8-sig')), today=DAY)
        self.assertEqual((st['kept_trial'], st['kept_paid'], st['created']), (1, 1, 1))
        get = lambda p: store.find_by_phone(self.con, p)
        self.assertEqual((get('0533157319')['status'], get('0533157319')['expires']), ('trial', None))
        self.assertEqual((get('0533187195')['status'], get('0533187195')['expires']), ('active', expires_paid))
        self.assertEqual((get('0556758504')['status'], get('0556758504')['expires']), ('active', '2026-12-01'))
        self.assertEqual((get('0521110000')['status'], get('0521110000')['expires']), ('trial', None))
        self.assertEqual((get('0521110001')['status'], get('0521110001')['expires']), ('trial', None))

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
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222'}, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222', 'aconf': '1'}, DAY)
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
        self.assertIn('נרשמו 2 רעשים', texts)
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

    def test_phone_payment_without_confirm_is_not_extended_twice_by_sync(self):
        today = datetime.date.today()
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0556788891', plan='monthly', status='active',
                                          expires=(today + datetime.timedelta(days=14)).isoformat())
            first = store.record_payment(self.con, sid, source='phone', months=1, amount=8.9, confirm=None, day=today)
        tx = [{'TransactionId': '81639082', 'Amount': '8.9', 'Confirmation': '2743258', 'KevaId': '2577272',
               'Groupe': f'שמרטפון {sid}', 'TransactionTime': today.strftime('%d/%m/%Y 20:16')}]
        with self.con:  # the same charge, seen by the Nedarim sync: linked, not a second month
            self.assertEqual(nedarim.apply_transactions(self.cfg, self.con, tx, log=lambda *a, **k: None),
                             (81639082, 0))
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
        self.assertEqual((sub['expires'], sub['keva_id']), (first.isoformat(), '2577272'))
        rows = self.con.execute('SELECT confirm, transaction_id FROM payments').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('2743258', '81639082')])
        nxt = [{**tx[0], 'TransactionId': '81700000', 'Confirmation': '2799999',
                'TransactionTime': (today + datetime.timedelta(days=31)).strftime('%d/%m/%Y 06:00')}]
        with self.con:  # next month's standing-order charge does extend
            self.assertEqual(nedarim.apply_transactions(self.cfg, self.con, nxt, log=lambda *a, **k: None)[1], 1)

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
        call({**room, 'anum': '0501112222'})
        call({**room, 'anum': '0501112222', 'aconf': '1'})
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
        self.assertEqual([c['type'] for c in r], ['noiseMonitor'])  # silent: nothing is played in the room
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
        self.call({**self.room, 'anum': '0501112222'})
        self.call({**self.room, 'anum': '0501112222', 'aconf': '1'})
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

    def test_remote_room_phone_from_parent_menu(self):
        self.cfg.update({'reconnect_extension': '4321', 'reconnect_charge': False})
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0501112222')
            store.add_verified(self.con, sid, '0509998888', 'admin')
        parent = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'P1', 'PBXcallStatus': 'CALL'}
        self.assertIn('מרחוק', self.call(parent, 'parent')[0]['files'][0]['text'])
        self.assertEqual(self.call({**parent, 'pmenu': '4'}, 'parent')[-1]['name'], 'rnum')
        self.assertEqual(self.call({**parent, 'rnum': '0501112222'})[-1]['name'], 'rnum')  # own phone: not the room
        r = self.call({**parent, 'rnum': '0521234567'})  # then exactly as in 1, plus '2#' = this phone
        self.assertEqual(r[-1]['name'], 'anum')
        self.assertIn('שממנו התקשרתם', spoken(r[-1]))
        self.assertEqual(self.call({**parent, 'anum': '0521234567'})[-1]['name'], 'anum')  # room phone can't get alerts
        self.assertEqual(self.call({**parent, 'anum': '0509998888'})[-1]['name'], 'aconf')
        r = self.call({**parent, 'aconf': '1'})
        self.assertEqual(r[-1]['name'], 'rcw')
        self.assertNotIn('noiseMonitor', [c['type'] for c in r])  # the parent's own call is not monitored
        row = self.con.execute("SELECT * FROM alerts WHERE kind='reconnect'").fetchone()
        self.assertEqual(row['alert_phone'], '0521234567')
        self.assertEqual(json.loads(row['payload'])['settings']['ap'], '0509998888')
        with self.con:
            self.con.execute("UPDATE alerts SET state='sent' WHERE kind='reconnect'")
        room = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'R1', 'PBXcallStatus': 'CALL',
                'PBXcallType': 'out'}
        r = self.call(room)  # the room phone auto-answers: silent monitoring with the parent's choices
        self.assertEqual([c['type'] for c in r], ['noiseMonitor'])
        self.assertEqual((r[0]['roomId'], r[0]['joinBeep']), (f's{sid}-0521234567', 'no'))  # silent: no prompt in the room
        self.assertEqual(self.con.execute("SELECT alert_phone FROM sessions WHERE call_id='R1'").fetchone()[0],
                         '0509998888')
        r = self.call({**parent, 'rcw': 'WAIT'})
        self.assertIn('הופעל בהצלחה', r[0]['files'][0]['text'])
        r = self.call({**parent, 'rcd': '2'})
        self.assertEqual(r[-1], {'type': 'noiseMonitorJoin', 'name': 'join', 'roomId': f's{sid}-0521234567'})

    def test_parent_menu_offers_joining_when_payments_on(self):
        new = {'PBXphone': '0541234567', 'PBXnum': PBX, 'PBXcallId': 'J1', 'PBXcallStatus': 'CALL'}
        r = self.call(new, 'parent')
        self.assertNotIn('5', r[0]['enabledKeys'].split(','))  # no payments configured: no 'join' option
        self.cfg['payments'] = {'monthly': 8.9, 'annual': 82}
        r = self.call({**new, 'PBXcallId': 'J2'}, 'parent')
        self.assertIn('5', r[0]['enabledKeys'].split(','))
        self.assertIn('להצטרפות כמנוי', spoken(r[0]))
        self.assertEqual(self.call({**new, 'PBXcallId': 'J2', 'pmenu': '5'}, 'parent')[-1]['name'], 'pplan')

    def test_renewed_trial_starts_on_the_next_call(self):
        with self.con:  # an old trial, renewed: no end date until the subscriber calls again
            sid = store.create_subscriber(self.con, main_phone='0551234560', plan='trial', status='trial')
            self.con.execute('UPDATE subscribers SET expires=NULL WHERE id=?', (sid,))
        caller = {'PBXphone': '0551234560', 'PBXnum': PBX, 'PBXcallId': 'T1', 'PBXcallStatus': 'CALL'}
        r = self.call(caller)
        self.assertIn('תקופת ניסיון של 7 ימים', spoken(r[-1]))
        self.assertEqual(r[-1]['name'], 'anum')
        exp = self.con.execute('SELECT expires FROM subscribers WHERE id=?', (sid,)).fetchone()[0]
        self.assertEqual(exp, (DAY + datetime.timedelta(days=7)).isoformat())
        r = self.call({**caller, 'PBXcallId': 'T2'})  # the next call does not restart it
        self.assertNotIn('תקופת ניסיון', spoken(r[-1]))

    def test_pay_menu_tells_active_subscribers_they_need_not_join(self):
        self.cfg['payments'] = {'monthly': 8.9, 'annual': 82}
        with self.con:
            store.create_subscriber(self.con, main_phone='0551234567', plan='monthly', status='active',
                                    expires='2026-11-10')
        caller = {'PBXphone': '0551234567', 'PBXnum': PBX, 'PBXcallId': 'K1', 'PBXcallStatus': 'CALL'}
        r = self.call(caller, 'pay')
        self.assertIn('כבר פעיל, בתוקף עד 10 בנובמבר 2026', spoken(r[-1]))
        new = {'PBXphone': '0541234568', 'PBXnum': PBX, 'PBXcallId': 'K2', 'PBXcallStatus': 'CALL'}
        self.assertNotIn('כבר פעיל', spoken(self.call(new, 'pay')[-1]))  # a new trial is not 'already a subscriber'

    def test_remote_room_phone_busy_and_failed(self):
        self.cfg.update({'reconnect_extension': '4321', 'reconnect_charge': False})
        sid = self.setup_dropped()
        with self.con:
            store.add_phone(self.con, sid, '0501112222')  # the parent may use the service from their phone
        parent = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'P2', 'PBXcallStatus': 'CALL'}
        self.call(parent, 'remote')
        self.call({**parent, 'rnum': '0531234567'})
        self.call({**parent, 'anum': '2'})  # '2#' = alerts to the phone calling now
        self.call({**parent, 'aconf': '1'})
        payload = self.con.execute("SELECT payload FROM alerts WHERE kind='reconnect'").fetchone()[0]
        self.assertEqual(json.loads(payload)['settings']['ap'], '0501112222')
        r = self.call({**parent, 'rcw': 'WAIT'}, now=time.time() + 200)  # the room phone never answered
        self.assertIn('מענה אוטומטי', r[0]['files'][0]['text'])
        with self.con:  # a room phone that is already monitoring is not dialled again
            self.con.execute("UPDATE sessions SET status='monitoring', ended=NULL, started=? WHERE call_id='r1'",
                             (time.time(),))
        parent['PBXcallId'] = 'P3'
        self.call(parent, 'remote')
        self.assertIn('כבר מחובר', self.call({**parent, 'rnum': '0521234567'})[0]['files'][0]['text'])

    def test_call_alerts_play_what_happened_then_the_menu(self):
        self.cfg.update({'alert_mode': 'call', 'alert_extension': '4322'})
        import urllib.request
        sent = {}

        class R:
            def read(self, n): return b'{"status":"OK","errorCode":0}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = lambda s, req, timeout=None: (sent.update(json.loads(req.data)), R())[1]
        try:
            core.send_alert(self.cfg, 'noise', '0501112222')  # no spoken text: a call into the alert extension
        finally:
            urllib.request.OpenerDirector.open = orig
        self.assertEqual((sent['messagesType'], sent['extensionActivation']), ('extensionActivation', '4322'))
        self.assertNotIn('audioText', sent)
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            self.con.execute("INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state) "
                             "VALUES('n1',?,'r1','noise','0501112222',?,'sent')", (sid, time.time() - 60))
        parent = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'A9', 'PBXcallStatus': 'CALL',
                  'PBXcallType': 'out'}
        r = self.call(parent, 'alert')  # the parent answers: the notices, then the main menu
        self.assertEqual(r[-1]['name'], 'menu')
        self.assertIn('זוהה רעש בחדר התינוק', spoken(r[-1]))
        r = self.call({**parent, 'menu': '3'}, 'alert')  # the menu works as in a call to the system
        self.assertIn('שְׁמַרְטַפוֹן הוא שירות', spoken(r[0]))

    def test_failure_after_timeout(self):
        self.setup_dropped()
        self.call(self.parent, 'alert')
        self.call({**self.parent, 'alm': '1'}, 'alert')
        r = self.call({**self.parent, 'rcw': 'WAIT'}, 'alert', now=time.time() + 200)
        self.assertEqual(r[-1]['type'], 'hangup')
        self.assertIn('לא הצלחנו', r[0]['files'][0]['text'])


class VerifyConfirmTests(Base):
    """The verification call goes into the alert extension: the other phone presses 1, no code to carry over."""

    def setUp(self):
        super().setUp()
        self.cfg.update({'alert_extension': '4322'})
        with self.con:
            self.sid = store.create_subscriber(self.con, main_phone='0521234567')
        self.room = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'r1', 'PBXcallStatus': 'CALL'}
        self.other = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'V1', 'PBXcallStatus': 'CALL',
                      'PBXcallType': 'out'}

    def call(self, p, mode='activate', now=None):
        with self.con:
            return ivr.handle(self.cfg, self.con, p, DAY, mode, now=now)[0]

    def ask_number(self):
        self.call(self.room)
        r = self.call({**self.room, 'anum': '0501112222'})
        self.assertEqual(r[-1]['name'], 'vw')
        self.assertIn('הקישו 1 לאישור', spoken(r[-1]))
        return r

    def test_confirm_with_1_on_the_other_phone(self):
        self.ask_number()
        self.assertEqual(self.call({**self.room, 'vw': 'WAIT'})[-1]['name'], 'vw')  # still waiting
        r = self.call(self.other, 'alert')  # the other phone answers the verification call
        self.assertEqual(r[-1]['name'], 'vcm')
        code = self.con.execute('SELECT code FROM verifications').fetchone()[0]
        self.assertIn(', '.join(code), spoken(r[-1]))  # the code is read out too, as a fallback
        r = self.call({**self.other, 'vcm': '1'}, 'alert')
        self.assertIn('אושר', spoken(r[0]))
        self.assertIn('0501112222', store.verified_phones(self.con, self.sid))
        r = self.call({**self.room, 'vw': 'WAIT'})
        self.assertEqual(r[-1]['name'], 'aconf')  # back in activation: 1 = start monitoring
        r = self.call({**self.room, 'vw': 'WAIT', 'aconf': '1'})
        self.assertEqual(r[-1]['type'], 'noiseMonitor')
        self.assertIn('אל תנתקו את השיחה', spoken(r[0]))
        self.assertEqual(r[0]['files'][-1], {'fileLink': 'https://x.example/beep.wav',
                                             'fileName': 'shmartphone_beep_v1'})  # a beep marks the start
        wav = ivr.beep_wav()
        self.assertEqual((wav[:4], wav[8:12]), (b'RIFF', b'WAVE'))
        self.assertGreater(len(wav), 9000)  # 0.6 s at 8 kHz, 16-bit

    def test_typing_the_code_still_works(self):
        self.ask_number()
        r = self.call({**self.room, 'vw': '9'})
        self.assertEqual(r[-1]['name'], 'vcode')
        code = self.con.execute('SELECT code FROM verifications').fetchone()[0]
        self.assertEqual(self.call({**self.room, 'vw': '9', 'vcode': code})[-1]['name'], 'aconf')

    def test_gives_up_after_the_wait(self):
        self.ask_number()
        r = self.call({**self.room, 'vw': 'WAIT'}, now=time.time() + 120)
        self.assertEqual(r[-1]['type'], 'hangup')
        self.assertIn('לא הושלם', spoken(r[0]))

    def test_disconnect_alert_still_works_on_the_same_extension(self):
        self.ask_number()
        with self.con:  # a newer 'room disconnected' alert to that phone wins over the verification
            self.con.execute("INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state) "
                             "VALUES('d1',?,'r0','disconnected','0501112222',?,'sent')", (self.sid, time.time() + 1))
        self.assertNotEqual(self.call(self.other, 'alert')[-1].get('name'), 'vcm')

    def test_verify_call_goes_to_the_alert_extension(self):
        import urllib.request
        sent = {}

        class R:
            def read(self, n): return b'{"status":"OK","errorCode":0}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = urllib.request.OpenerDirector.open
        urllib.request.OpenerDirector.open = lambda s, req, timeout=None: (sent.update(json.loads(req.data)), R())[1]
        try:
            core.send_alert(self.cfg, 'verify', '0501112222', 'קוד')
        finally:
            urllib.request.OpenerDirector.open = orig
        self.assertEqual((sent['messagesType'], sent['extensionActivation']), ('extensionActivation', '4322'))
        self.assertNotIn('audioText', sent)


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

    def test_message_copied_to_voicemail_box(self):
        self.cfg.update({'messages_folder': '12116', 'messages_copy_folder': '12101'})
        calls = []

        def api(cfg, action, **kw):
            calls.append((action, kw))
            if action == 'filesList':
                return [{'id': 7, 'name': 'msg_x', 'ivrName': 'abc'}, {'id': 8, 'name': 'other', 'ivrName': 'zzz'}]
            return {'status': 'OK'}
        row = {'file_id': 'abc', 'file_name': 'msg_x'}
        self.assertEqual(core.copy_message_file(self.cfg, row, api), '')
        self.assertEqual(calls[-1], ('fileCopy', {'fileId': '7', 'target': '12101'}))
        self.assertEqual(core.copy_message_file(self.cfg, {'file_id': None, 'file_name': 'msg_x'}, api), '')
        self.assertEqual(core.copy_message_file(self.cfg, {'file_id': 'nope', 'file_name': 'nope'}, api),
                         'copy: file not found')

    def test_stale_sessions(self):
        now = time.time()
        with self.con:
            for cid, started, mon in (('old', now - 7 * 3600, None), ('new', now - 600, None),
                                      ('oldmon', now - 7 * 3600, now - 7 * 3600)):
                self.con.execute("INSERT INTO sessions(call_id,started,mon_start,status) VALUES(?,?,?,'menu')",
                                 (cid, started, mon))
            self.assertEqual(store.close_stale_sessions(self.con, now), 1)
        open_ = [r[0] for r in self.con.execute('SELECT call_id FROM sessions WHERE ended IS NULL ORDER BY call_id')]
        self.assertEqual(open_, ['new', 'oldmon'])  # a monitoring call is never guessed closed
        page = admin.activity_page(self.cfg, self.con)
        self.assertNotIn('>old<', page)

    def test_daily_report(self):
        now = time.mktime((2026, 10, 28, 22, 0, 5, 0, 0, -1))
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567', plan='prepaid', status='active')
            self.con.execute("INSERT INTO sessions(call_id,subscriber_id,phone,started,mon_start,ended,status,mode) "
                             "VALUES('c1',?,'0521234567',?,?,?,'disconnected hangup','activate')",
                             (sid, now - 4000, now - 3900, now - 300))
            for i in range(12):
                self.con.execute("INSERT INTO alerts(subscriber_id,kind,created,state) VALUES(?,'noise',?,'sent')",
                                 (sid, now - 3000 + i))
            self.con.execute("INSERT INTO alerts(subscriber_id,kind,created,state) VALUES(?,'verify',?,'failed')",
                             (sid, now - 100))
            self.con.execute("INSERT INTO alerts(subscriber_id,kind,created,state) VALUES(?,'noise',?,'sent')",
                             (sid, now - 90000))  # yesterday: not counted
            self.con.execute("INSERT INTO payments(subscriber_id,source,plan,amount,created) "
                             "VALUES(?,'nedarim','monthly',8.9,?)", (sid, now - 50))
        subject, body = report.build(self.con, now)
        self.assertIn('28/10/2026', subject)
        self.assertIn('ניטורים: 1 של 1 מנויים, סה"כ 60 דקות', body)
        self.assertIn('רעש: נשלחו 12', body)
        self.assertIn('סה"כ שיחות שיצאו: 12', body)
        self.assertIn('קיבל 12 התראות רעש', body)
        self.assertIn('8.9 ₪', body)
        self.assertIn('1 שיחות אימות מספר נכשלו', body)

        sent = []
        mail = lambda c, s, b: sent.append(s)  # noqa: E731
        quiet = lambda *a, **k: None  # noqa: E731
        self.assertFalse(report.run(self.cfg, self.db, mail, now=now, log=quiet))  # no smtp configured
        self.cfg['smtp'] = {'host': 'h', 'user': 'u', 'password': 'p', 'to': 't@x'}
        self.assertFalse(report.run(self.cfg, self.db, mail, now=now - 3600, log=quiet))  # 21:00: too early
        self.assertTrue(report.run(self.cfg, self.db, mail, now=now, log=quiet))
        self.assertFalse(report.run(self.cfg, self.db, mail, now=now + 600, log=quiet))  # once a day
        self.assertTrue(report.run(self.cfg, self.db, mail, now=now + 86400, log=quiet))
        self.assertEqual(len(sent), 2)


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
        self.ivr('0521234567', 'c1', anum='0501112222')
        return self.ivr('0521234567', 'c1', anum='0501112222', aconf='1')

    def test_start_says_alert_phone(self):
        r = self.start_room()
        self.assertNotIn(sp('0501112222'), spoken(r[0]))  # the number is not read out
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
        r = self.ivr('0501112222', 'p1', 'menu', menu='2')
        self.assertEqual(r[-1]['name'], 'pmenu')
        r = self.ivr('0501112222', 'p1', 'menu', menu='2', pmenu='3')
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
        self.assertIn(sp('0521234567'), spoken(r[0]))
        with self.con:
            r = call(menu='0', vpin='12345', vnav='8')  # call the sender back
        self.assertEqual((r[-1]['type'], r[-1]['dialPhone']), ('simpleRouting', '0521234567'))
        with self.con:
            r = call(menu='0', vpin='12345', vnav='8', vroute='ANSWERED')  # back to the list, then the menu
        with self.con:
            call(menu='0')
        with self.con:
            call(menu='0', vpin='12345')
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
        self.assertIn('הקישו 1 וסולמית', ' '.join(f.get('text', '') for f in r[-1]['files']))
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'anum': '1'}, DAY)[0]
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
            r = call(anum='0501111111')
        self.assertEqual(r[0]['name'], 'aconf')
        with self.con:
            r = call(anum='0501111111', aconf='3')  # add another number
        self.assertEqual(r[0]['name'], 'amore')
        with self.con:
            r = call(anum='0501111111', aconf='3', amore='0502222222')
        self.assertEqual(r[0]['name'], 'aconf')
        with self.con:
            call(anum='0501111111', aconf='4')
        with self.con:
            r = call(anum='0501111111', aconf='4', amore='0503333333')  # fallback
        self.assertEqual(r[0]['name'], 'aconf')
        with self.con:
            r = call(anum='0501111111', aconf='1')
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


class EarlyHangupMessageTests(Base):
    def test_message_kept_when_caller_hangs_up_before_hash(self):
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'h1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY, 'menu')
        with self.con:
            r = ivr.handle(self.cfg, self.con, {**p, 'menu': '9'}, DAY, 'menu')[0]
        self.assertEqual(r[0]['fileName'], 'msg_h1')
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'menu': '9', 'PBXcallStatus': 'HANGUP'}, DAY, 'menu')
        self.assertEqual(self.con.execute('SELECT file_name FROM messages').fetchone()[0], 'msg_h1')


class FreshNoiseTests(Base):
    def test_fresh_recording_plays_by_direct_link(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_verified(self.con, sid, '0501112222', 'admin')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'c1', 'PBXcallStatus': 'CALL'}
        with self.con:
            ivr.handle(self.cfg, self.con, p, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222'}, DAY)
        with self.con:
            ivr.handle(self.cfg, self.con, {**p, 'anum': '0501112222', 'aconf': '1'}, DAY)
        base = {'time': time.time(), 'roomId': f's{sid}-0521234567', 'callId': 'c1', 'num': PBX, 'eventId': 'e1'}
        with self.con:
            core.handle_event(self.cfg, self.con, {**base, 'event': 'noise'}, 'noise')
            core.handle_event(self.cfg, self.con, {**base, 'event': 'noiseEnd', 'noiseSec': 4, 'fileId': 'F9',
                                                   'fileName': 'noise_x', 'saveFolder': '12116',
                                                   'fileUrl': 'https://files.example/noise_x.opus'}, 'noiseEnd')
        q = {'PBXphone': '0501112222', 'PBXnum': PBX, 'PBXcallId': 'h1', 'PBXcallStatus': 'CALL'}
        with self.con:
            r = ivr.handle(self.cfg, self.con, q, DAY, 'history')[0]
        self.assertIn({'fileLink': 'https://files.example/noise_x.opus', 'fileName': 'noise_x'}, r[0]['files'])


class OneAtATimeTests(Base):
    def test_phones_listed_one_by_one(self):
        with self.con:
            sid = store.create_subscriber(self.con, main_phone='0521234567')
            store.add_phone(self.con, sid, '0531111111')
            store.add_phone(self.con, sid, '0532222222')
        p = {'PBXphone': '0521234567', 'PBXnum': PBX, 'PBXcallId': 'o1', 'PBXcallStatus': 'CALL'}
        call = lambda **a: ivr.handle(self.cfg, self.con, {**p, **a}, DAY, 'phones')[0]
        with self.con:
            call()
        with self.con:
            r = call(phm='2')
        self.assertIn(sp('0531111111'), spoken(r[0]))
        self.assertNotIn(sp('0532222222'), spoken(r[0]))  # only the first number
        with self.con:
            r = call(phm='2', phr='2')  # next
        self.assertIn(sp('0532222222'), spoken(r[0]))
        with self.con:
            call(phm='2', phr='1')  # remove the current (second) one
        self.assertEqual([x[0] for x in self.con.execute('SELECT phone FROM phones')], ['0531111111'])
