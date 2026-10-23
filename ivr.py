"""Call flows for the Technoline Module API.

Each menu option is its own Technoline API extension pointing to /ivr/<token>/<mode>:
  menu      full main menu (optional - use if you prefer one extension)
  activate  1  start monitoring (only to verified alert numbers)
  listen    2  babysitter line: join a room you are allowed to hear
  info      3  explanation
  phones    4  add / hear / remove authorized phones
  verify    5  verify a phone number for receiving alerts (code call)
  account   6  account status, alert numbers, phones
  message   9  record a message for customer service (emailed)
  history      noise history: recent noise events, with their recordings
  pay          join / renew a paid subscription (card by phone through Technoline -> Nedarim Plus)

Per-call state is kept in the sessions table (step + data), so stale request
parameters can never be mistaken for new answers.
"""
import datetime
import json
import secrets
import time

import store

MODES = ('menu', 'activate', 'listen', 'info', 'phones', 'verify', 'account', 'message', 'history', 'pay')
MENU_KEYS = {'1': 'activate', '2': 'listen', '3': 'info', '4': 'phones', '5': 'verify', '6': 'account',
             '9': 'message'}
HEB_MONTHS = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר',
              'נובמבר', 'דצמבר']
VERIFY_TTL = 30 * 60

TEXTS = {
    'menu': 'ברוכים הבאים לשמרטפון, המערכת החכמה שתסייע בשמירה על ילדיכם ותעניק לכם שקט נפשי כאשר אתם יוצאים מהבית. '
            'להפעלת השמרטפון, הקישו 1. למעבר לשלוחת שמרטף, הקישו 2. '
            'להסבר ולהוראות הפעלה, הקישו 3. להוספת טלפונים מורשים, הקישו 4. '
            'לאימות מספר טלפון לקבלת התראות, הקישו 5. לניהול חשבון המנוי, הקישו 6. '
            'להשארת הודעה לשירות הלקוחות, הקישו 9.',
    'info': 'שמרטפון הוא שירות שמאפשר לכם לצאת מהבית בשקט, כשהילדים ישנים. '
            'משאירים בחדר טלפון, מתקשרים ממנו למערכת, ומקישים 1 להפעלת השמרטפון. '
            'המערכת מקשיבה לחדר כל הזמן. כשנשמע בכי או רעש, אנחנו מתקשרים מיד למספר שבחרתם, '
            'כדי שתוכלו לחזור או לבדוק. '
            'לפני השימוש הראשון יש לאמת את המספר שיקבל את ההתראות, בשלוחה 5. '
            'אפשר לאמת כמה מספרים, למשל של שני ההורים או של שמרטף. '
            'כדי להקשיב לחדר בכל רגע, מתקשרים מטלפון מאומת ומקישים 2. '
            'טלפונים נוספים שמותר להפעיל מהם את השירות מוסיפים בשלוחה 4. '
            'חשוב: השאירו את הטלפון שבחדר מחובר למטען, בקרבת מקום השינה, ואל תנתקו את השיחה. '
            'ספקים מסוימים מנתקים שיחות ארוכות אחרי כשעה. אם זה קורה, נתקשר אליכם להודיע שהקו התנתק. '
            'השירות הוא עזר בלבד, ואינו מחליף השגחה של מבוגר. תודה שבחרתם בשמרטפון.',
    'welcome_trial': 'ברוכים הבאים לשמרטפון. הופעלה עבורכם תקופת ניסיון של {days} ימים.',
    'not_registered': 'מספר הטלפון שממנו התקשרתם אינו רשום בשירות שמרטפון.',
    'disabled': 'המנוי שלכם אינו פעיל.',
    'trial_expired': 'תקופת הניסיון שלכם הסתיימה.',
    'expired': 'תוקף המנוי שלכם הסתיים.',
    'renew': 'לחידוש המנוי השאירו הודעה בשלוחה 9 בתפריט הראשי. תודה.',
    'no_verified': 'עדיין לא אומת מספר לקבלת התראות. נאמת מספר עכשיו.',
    'confirm_one': 'ההתראות יישלחו למספר',
    'confirm_one_keys': 'לאישור הקישו 1. לאימות מספר אחר הקישו 2.',
    'pick_prefix': 'להתראות למספר',
    'pick_press': 'הקישו',
    'pick_new': 'לאימות מספר חדש הקישו 9.',
    'starting': 'הניטור מתחיל. אפשר להניח את הטלפון בחדר.',
    'busy': 'כבר פועל ניטור מהטלפון הזה.',
    'error': 'אירעה תקלה. נסו שוב מאוחר יותר.',
    'bad_key': 'הבחירה אינה תקינה.',
    'ask_verify_number': 'הקישו את מספר הטלפון שיקבל את ההתראות, ובסיומו הקישו סולמית.',
    'bad_number': 'המספר שהוקש אינו תקין.',
    'verified_ok': 'המספר אומת ויקבל התראות.',
    'code_sent': 'התקשרנו עכשיו למספר שהקשתם, והשמענו בו קוד אימות בן ארבע ספרות. הקישו כאן את הקוד.',
    'ask_pending_code': 'יש לכם אימות שממתין לקוד. הקישו את הקוד שקיבלתם, או סולמית לאימות מספר אחר.',
    'code_wrong': 'הקוד שגוי.',
    'code_later': 'תוכלו לחזור לשלוחה 5 ולהקיש את הקוד במהלך חצי השעה הקרובה.',
    'code_failed': 'האימות נכשל. נסו שוב מאוחר יותר.',
    'too_many_verify': 'שלחנו כבר כמה קודי אימות בשעה האחרונה. נסו שוב מאוחר יותר.',
    'verify_call': 'שלום, זוהי שיחה ממערכת שמרטפון. קוד האימות הוא: {code}. אני חוזר: {code}. '
                   'אם לא ביקשתם לקבל התראות משמרטפון, אפשר להתעלם משיחה זו.',
    'listen_none': 'אין כרגע ניטור פעיל שאתם מורשים להאזין לו.',
    'listen_pick': 'להאזנה לחדר של המנוי',
    'listen_connect': 'מתחברים לחדר.',
    'listen_ended': 'הניטור בחדר הסתיים.',
    'listen_full': 'כבר יש מאזין בחדר. נסו שוב בעוד כמה דקות.',
    'phones_menu': 'להוספת טלפון מורשה הקישו 1. לשמיעת הטלפונים המורשים הקישו 2. להסרת טלפון מורשה הקישו 3. לסיום הקישו 0.',
    'phones_ask': 'הקישו את מספר הטלפון שתרצו להוסיף, ובסיומו הקישו סולמית.',
    'phones_added': 'הטלפון נוסף כטלפון מורשה.',
    'phones_main': 'הטלפון הראשי',
    'phones_extra': 'טלפונים נוספים',
    'phones_none': 'אין טלפונים נוספים.',
    'phones_remove': 'להסרת המספר',
    'phones_removed': 'הטלפון הוסר.',
    'account_menu': 'לשמיעת מצב המנוי הקישו 1. לשמיעת והסרת המספרים המאומתים לקבלת התראות הקישו 2. '
                    'לניהול הטלפונים המורשים הקישו 3. לאימות מספר חדש לקבלת התראות הקישו 4. לסיום הקישו 0.',
    'account_status': 'המנוי שלכם {status}. סוג המנוי: {plan}.',
    'account_until': 'בתוקף עד {date}.',
    'account_verified': 'המספרים המאומתים לקבלת התראות:',
    'account_no_verified': 'אין מספרים מאומתים לקבלת התראות.',
    'account_remove': 'להסרת המספר',
    'back_zero': 'לחזרה הקישו 0.',
    'removed': 'המספר הוסר.',
    'message_prompt': 'השאירו הודעה לשירות הלקוחות, ובסיומה הקישו סולמית.',
    'message_thanks': 'ההודעה התקבלה. נחזור אליכם בהקדם. תודה.',
    'goodbye': 'להתראות.',
    'account_pay': 'להצטרפות או לחידוש מנוי בתשלום הקישו 5.',
    'pay_menu': 'למנוי חודשי ב-{monthly} שקלים לחודש, בהוראת קבע באשראי, הקישו 1. '
                'למנוי שנתי ב-{annual} שקלים, בתשלום אחד, הקישו 2. לחזרה הקישו 0.',
    'pay_card_intro': 'נעבור עכשיו להקשת פרטי כרטיס האשראי.',
    'pay_failed': 'התשלום לא הושלם. אפשר לנסות שוב בכל עת.',
    'pay_ok_monthly': 'תודה. המנוי החודשי הופעל, והוא בתוקף עד {date}. החיוב יתחדש מדי חודש.',
    'pay_ok_annual': 'תודה. המנוי השנתי הופעל, והוא בתוקף עד {date}.',
    'pay_next': 'להפעלת השמרטפון התקשרו שוב ובחרו 1. להתראות.',
    'history_none': 'לא נרשמו אירועי רעש ביממה האחרונה.',
    'history_intro': 'ביממה האחרונה נרשמו {n} אירועי רעש. מתחילים מהאחרון. '
                     'בכל רגע: לבא הקישו 3, לקודם 1, לשמיעה חוזרת 2, לזמן ופרטי ההקלטה 5, לסיום 0.',
    'history_item': 'אירוע {i}, בשעה {hh}, לפני {ago}, {sec} שניות.',
    'history_peak': 'עוצמה מרבית {peak} מתוך 100.',
    'history_no_file': 'לאירוע הזה אין הקלטה.',
    'history_end': 'אלה כל אירועי הרעש מהיממה האחרונה. להתראות.',
    'history_first': 'זה האירוע האחרון שנרשם.',
    'history_pick': 'להיסטוריה של המנוי',
}


def t(cfg, key, **kw):
    return (cfg.get('texts', {}).get(key) or TEXTS[key]).format(**kw)


def say(*parts):
    files = []
    for p in parts:
        if isinstance(p, dict):
            files.append(p)
        elif p:
            files.append({'text': p})
    return {'type': 'simpleMessage', 'files': files}


def ask(name, files, mx=1, mn=1, timeout=7, confirm='no', skip=False):
    cmd = {'type': 'getDTMF', 'name': name, 'min': mn, 'max': mx, 'timeout': timeout, 'confirmType': confirm,
           'files': [f if isinstance(f, dict) else {'text': f} for f in files if f]}
    if skip:
        cmd.update({'skipKey': '#', 'skipValue': 'SKIP'})
    return cmd


def hangup(cfg, *parts):
    return [say(*parts), {'type': 'hangup'}] if any(parts) else [{'type': 'hangup'}]


def say_date(d):
    try:
        x = datetime.date.fromisoformat(d)
    except (TypeError, ValueError):
        return ''
    return f'{x.day} ב{HEB_MONTHS[x.month - 1]} {x.year}'


def spaced(code):
    return ', '.join(code)


class Call:
    def __init__(self, cfg, con, p, day, now):
        self.cfg, self.con, self.p, self.day, self.now = cfg, con, p, day, now
        self.call_id = p.get('PBXcallId', '')
        self.phone = store.norm_phone(p.get('PBXphone', ''))
        self.sess = con.execute('SELECT * FROM sessions WHERE call_id=?', (self.call_id,)).fetchone()
        self.data = json.loads(self.sess['data']) if self.sess and self.sess['data'] else {}
        self.wake = False

    # ----- session helpers -----
    def save(self, step=None, **fields):
        fields['step'] = step
        fields['data'] = json.dumps(self.data, ensure_ascii=False)
        if self.sess is None:
            self.con.execute('INSERT INTO sessions(call_id,phone,started,status) VALUES(?,?,?,?)',
                             (self.call_id, self.phone, self.now, 'menu'))
        cols = ','.join(f'{k}=?' for k in fields)
        self.con.execute(f'UPDATE sessions SET {cols} WHERE call_id=?', (*fields.values(), self.call_id))
        self.sess = self.con.execute('SELECT * FROM sessions WHERE call_id=?', (self.call_id,)).fetchone()

    def retry(self, limit=3):
        n = self.data.get('tries', 0) + 1
        self.data['tries'] = n
        return n < limit

    def reset_tries(self):
        self.data['tries'] = 0

    # ----- subscriber -----
    def subscriber(self, auto_trial=False, allow_inactive=False):
        """Return (sub, intro, None) or (None, None, commands)."""
        sub = store.find_by_phone(self.con, self.phone)
        intro = None
        if sub is None:
            days = int(self.cfg.get('auto_trial_days', 0) or 0)
            if not auto_trial or days <= 0 or not store.valid_phone(self.phone):
                return None, None, hangup(self.cfg, t(self.cfg, 'not_registered'), t(self.cfg, 'renew'))
            exp = (self.day + datetime.timedelta(days=days)).isoformat()
            sid = store.create_subscriber(self.con, main_phone=self.phone, plan='trial', status='trial',
                                          expires=exp, notes='נרשם אוטומטית בשיחה ראשונה')
            sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
            intro = t(self.cfg, 'welcome_trial', days=days)
        if not allow_inactive:
            ok, reason = store.authorization(sub, self.day, self.cfg.get('enforce_active_expiry', False))
            if not ok:
                key = reason if reason in TEXTS else 'disabled'
                if self.payments_on():
                    self.bind(sub)
                    return None, None, self.pay_menu(sub, t(self.cfg, key))
                return None, None, hangup(self.cfg, t(self.cfg, key), t(self.cfg, 'renew'))
        self.bind(sub)
        return sub, intro, None

    def bind(self, sub):
        if self.sess is None or self.sess['subscriber_id'] != sub['id']:
            self.save(self.sess['step'] if self.sess else None, subscriber_id=sub['id'])

    # ----- dispatch -----
    def run(self, mode):
        p = self.p
        if p.get('PBXcallStatus') == 'HANGUP':
            return self.on_hangup()
        if self.cfg.get('pbx_number') and p.get('PBXnum') != self.cfg['pbx_number']:
            return [{'type': 'hangup'}]
        step = self.sess['step'] if self.sess else None
        handlers = {
            'menu': ('menu', self.on_menu), 'act_pick': ('apick', self.on_act_pick),
            'ver_num': ('vnum', self.on_ver_num), 'ver_code': ('vcode', self.on_ver_code),
            'monitoring': ('nm', self.on_monitor_result), 'lis_pick': ('lpick', self.on_listen_pick),
            'listening': ('join', self.on_join_result), 'ph_menu': ('phm', self.on_phones_menu),
            'ph_add': ('phn', self.on_phones_add), 'ph_rm': ('phr', self.on_phones_rm),
            'acc_menu': ('acm', self.on_account_menu), 'acc_rm': ('acr', self.on_account_rm),
            'recording': ('rec', self.on_record), 'his_nav': ('hnav', self.on_history_nav),
            'his_sub': ('hsub', self.on_history_sub), 'pay_plan': ('pplan', self.on_pay_plan),
            'paying': ('payment', self.on_payment),
        }
        if step in handlers and handlers[step][0] in p:
            return handlers[step][1](p[handlers[step][0]])
        if self.sess is None or step is None:
            self.save(None, mode=mode)
        return self.entry(mode)

    def entry(self, mode):
        return {'menu': self.menu, 'activate': self.activate, 'listen': self.listen, 'info': self.info,
                'phones': self.phones, 'verify': self.verify, 'account': self.account,
                'message': self.message, 'history': self.history,
                'pay': self.pay_menu}.get(mode, self.activate)()

    def on_hangup(self):
        if self.sess:
            if self.sess['step'] == 'recording' and self.p.get('FILEID_rec') and not self.data.get('saved'):
                self.store_message()
            self.con.execute("UPDATE sessions SET ended=COALESCE(ended,?) WHERE call_id=?", (self.now, self.call_id))
        return []

    # ----- menu -----
    def menu(self):
        self.save('menu')
        return [{'type': 'simpleMenu', 'name': 'menu', 'enabledKeys': ','.join(MENU_KEYS), 'times': 3,
                 'timeout': 7, 'errorReturn': 'ERROR', 'files': [{'text': t(self.cfg, 'menu')}]}]

    def on_menu(self, key):
        mode = MENU_KEYS.get(key)
        if not mode:
            return hangup(self.cfg, t(self.cfg, 'goodbye'))
        self.save(None, mode=mode)
        return self.entry(mode)

    # ----- 1: activate -----
    def alert_choices(self, sub):
        phones = [x for x in store.verified_phones(self.con, sub['id']) if x != self.phone]
        if sub['alert_phone'] in phones:
            phones.remove(sub['alert_phone'])
            phones.insert(0, sub['alert_phone'])
        return phones[:8]

    def activate(self, intro_override=None):
        sub, intro, err = self.subscriber(auto_trial=True)
        if err:
            return err
        intro = intro_override or intro
        choices = self.alert_choices(sub)
        if not choices:
            self.data['next'] = 'activate'
            return self.ask_verify_number(sub, [intro, t(self.cfg, 'no_verified')])
        self.data['choices'] = choices
        self.reset_tries()
        self.save('act_pick')
        return [self.pick_prompt(choices, intro)]

    def pick_prompt(self, choices, intro=None):
        if len(choices) == 1:
            files = [intro, t(self.cfg, 'confirm_one'), {'digits': choices[0]}, t(self.cfg, 'confirm_one_keys')]
        else:
            files = [intro]
            for i, ph in enumerate(choices, 1):
                files += [t(self.cfg, 'pick_prefix'), {'digits': ph}, t(self.cfg, 'pick_press'), {'number': str(i)}]
            files.append(t(self.cfg, 'pick_new'))
        return ask('apick', files)

    def on_act_pick(self, key):
        sub, _, err = self.subscriber()
        if err:
            return err
        choices = self.data.get('choices', [])
        if key.isdigit() and 1 <= int(key) <= len(choices) and key != '9':
            return self.start_monitor(sub, choices[int(key) - 1])
        if key == '9' or (len(choices) == 1 and key == '2'):
            self.data['next'] = 'activate'
            return self.ask_verify_number(sub, [])
        if self.retry():
            self.save('act_pick')
            return [self.pick_prompt(choices, t(self.cfg, 'bad_key'))]
        return hangup(self.cfg, t(self.cfg, 'goodbye'))

    def start_monitor(self, sub, alert_phone):
        room = f"s{sub['id']}-{self.phone}"[:40]
        store.update_subscriber(self.con, sub['id'], alert_phone=alert_phone)
        self.save('monitoring', room_id=room, alert_phone=alert_phone, status='monitoring', mode='activate')
        cmd = {'type': 'noiseMonitor', 'name': 'nm', 'roomId': room,
               'alertUrl': self.cfg['public_url'].rstrip('/') + '/events/' + self.cfg['event_token']}
        params = {'threshold': 55, 'minDuration': 2, 'maxGap': 0.5, 'silenceEnd': 3, 'preRoll': 2,
                  'maxClip': 120, 'cooldown': 0, 'maxListeners': 1, 'joinBeep': 'yes'}
        params.update(self.cfg.get('monitor', {}))
        cmd.update(params)
        return [say(t(self.cfg, 'starting')), cmd]

    def on_monitor_result(self, status):
        self.con.execute('UPDATE sessions SET ended=?, status=?, step=NULL WHERE call_id=?',
                         (self.now, status, self.call_id))
        if status == 'BUSY':
            return hangup(self.cfg, t(self.cfg, 'busy'))
        if status in ('TIMEOUT', 'HANGUP'):
            return [{'type': 'hangup'}]
        return hangup(self.cfg, t(self.cfg, 'error'))

    # ----- 5: verify -----
    def pending(self, sub):
        return self.con.execute(
            'SELECT * FROM verifications WHERE subscriber_id=? AND done IS NULL AND attempts<3 AND created>? '
            'ORDER BY id DESC LIMIT 1', (sub['id'], self.now - VERIFY_TTL)).fetchone()

    def verify(self):
        sub, intro, err = self.subscriber(auto_trial=True)
        if err:
            return err
        pend = self.pending(sub)
        if pend:
            self.data['vid'] = pend['id']
            self.data['pending_prompt'] = True
            self.reset_tries()
            self.save('ver_code')
            return [ask('vcode', [intro, t(self.cfg, 'ask_pending_code')], mx=4, mn=4, timeout=10, skip=True)]
        return self.ask_verify_number(sub, [intro])

    def ask_verify_number(self, sub, prefix):
        self.reset_tries()
        self.save('ver_num')
        return [ask('vnum', prefix + [t(self.cfg, 'ask_verify_number')], mx=10, mn=9, timeout=6,
                    confirm='digits', skip=True)]

    def on_ver_num(self, value):
        sub, _, err = self.subscriber()
        if err:
            return err
        n = store.norm_phone(value)
        if not store.valid_phone(n):
            if self.retry():
                self.save('ver_num')
                return [ask('vnum', [t(self.cfg, 'bad_number'), t(self.cfg, 'ask_verify_number')], mx=10, mn=9,
                            timeout=6, confirm='digits', skip=True)]
            return hangup(self.cfg, t(self.cfg, 'goodbye'))
        if n == self.phone:  # calling from that phone proves it
            store.add_verified(self.con, sub['id'], n, 'caller')
            return self.verified_continue(sub)
        recent = self.con.execute('SELECT COUNT(*) FROM verifications WHERE subscriber_id=? AND created>?',
                                  (sub['id'], self.now - 3600)).fetchone()[0]
        target = self.con.execute('SELECT COUNT(*) FROM verifications WHERE phone=? AND created>?',
                                  (n, self.now - 86400)).fetchone()[0]
        if recent >= 3 or target >= 5:
            return hangup(self.cfg, t(self.cfg, 'too_many_verify'))
        code = ''.join(secrets.choice('123456789') for _ in range(4))
        cur = self.con.execute('INSERT INTO verifications(subscriber_id,phone,code,created) VALUES(?,?,?,?)',
                               (sub['id'], n, code, self.now))
        self.con.execute('INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state,payload) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (f'verify:{cur.lastrowid}', sub['id'], self.call_id, 'verify', n, self.now, 'pending',
                          json.dumps({'text': t(self.cfg, 'verify_call', code=spaced(code))}, ensure_ascii=False)))
        self.data['vid'] = cur.lastrowid
        self.wake = True
        self.reset_tries()
        self.save('ver_code')
        return [ask('vcode', [t(self.cfg, 'code_sent')], mx=4, mn=4, timeout=25, skip=True)]

    def on_ver_code(self, value):
        sub, _, err = self.subscriber()
        if err:
            return err
        v = self.con.execute('SELECT * FROM verifications WHERE id=? AND subscriber_id=?',
                             (self.data.get('vid'), sub['id'])).fetchone()
        if value == 'SKIP' and self.data.pop('pending_prompt', False):
            return self.ask_verify_number(sub, [])  # '#' on the pending-code prompt: verify another number
        self.data.pop('pending_prompt', None)
        if not v or v['done'] or v['created'] < self.now - VERIFY_TTL or v['attempts'] >= 3:
            return hangup(self.cfg, t(self.cfg, 'code_failed'))
        if value in ('', 'SKIP', 'ERROR'):
            return hangup(self.cfg, t(self.cfg, 'code_later'))
        self.con.execute('UPDATE verifications SET attempts=attempts+1 WHERE id=?', (v['id'],))
        if secrets.compare_digest(value, v['code']):
            self.con.execute('UPDATE verifications SET done=? WHERE id=?', (self.now, v['id']))
            store.add_verified(self.con, sub['id'], v['phone'], 'code')
            return self.verified_continue(sub)
        if v['attempts'] + 1 < 3:
            self.save('ver_code')
            return [ask('vcode', [t(self.cfg, 'code_wrong'), t(self.cfg, 'code_sent')], mx=4, mn=4, timeout=25,
                        skip=True)]
        return hangup(self.cfg, t(self.cfg, 'code_failed'))

    def verified_continue(self, sub):
        nxt = self.data.pop('next', None)
        if nxt == 'activate':
            self.save(None)
            return self.activate(intro_override=t(self.cfg, 'verified_ok'))
        self.save(None)
        return hangup(self.cfg, t(self.cfg, 'verified_ok'))

    # ----- 2: listen -----
    def listen(self):
        subs = set()
        own = store.find_by_phone(self.con, self.phone)
        if own:
            subs.add(own['id'])
        for r in self.con.execute('SELECT subscriber_id FROM alert_phones WHERE phone=?', (self.phone,)):
            subs.add(r[0])
        rooms = []
        for sid in subs:
            sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
            if not store.authorization(sub, self.day, self.cfg.get('enforce_active_expiry', False))[0]:
                continue
            r = self.con.execute(
                "SELECT room_id FROM sessions WHERE subscriber_id=? AND status='monitoring' AND ended IS NULL "
                "AND room_id IS NOT NULL AND started>? AND phone!=? ORDER BY started DESC LIMIT 1",
                (sid, self.now - 12 * 3600, self.phone)).fetchone()
            if r:
                rooms.append((sub['main_phone'], r['room_id'], sid))
        if not rooms:
            return hangup(self.cfg, t(self.cfg, 'listen_none'))
        if len(rooms) == 1:
            return self.join(*rooms[0])
        self.data['rooms'] = rooms[:8]
        self.save('lis_pick')
        files = []
        for i, (main, _, _) in enumerate(rooms[:8], 1):
            files += [t(self.cfg, 'listen_pick'), {'digits': main}, t(self.cfg, 'pick_press'), {'number': str(i)}]
        return [ask('lpick', files)]

    def on_listen_pick(self, key):
        rooms = self.data.get('rooms', [])
        if key.isdigit() and 1 <= int(key) <= len(rooms):
            return self.join(*rooms[int(key) - 1])
        return hangup(self.cfg, t(self.cfg, 'bad_key'))

    def join(self, main, room, sid):
        self.save('listening', room_id=None, subscriber_id=sid, status='listening', mode='listen')
        self.data['room'] = room
        self.save('listening')
        return [say(t(self.cfg, 'listen_connect')), {'type': 'noiseMonitorJoin', 'name': 'join', 'roomId': room}]

    def on_join_result(self, status):
        self.con.execute('UPDATE sessions SET ended=?, status=?, step=NULL WHERE call_id=?',
                         (self.now, 'listen ' + status, self.call_id))
        msgs = {'MONITOR_ENDED': 'listen_ended', 'NOT_ACTIVE': 'listen_none', 'FULL': 'listen_full',
                'REJECTED': 'listen_full'}
        if status == 'LEFT':
            return [{'type': 'hangup'}]
        return hangup(self.cfg, t(self.cfg, msgs.get(status, 'error')))

    # ----- noise history -----
    def allowed_subs(self):
        """Subscribers this caller may see: own subscription + those that verified this phone."""
        out = []
        own = store.find_by_phone(self.con, self.phone)
        if own:
            out.append(own)
        for r in self.con.execute('SELECT s.* FROM subscribers s JOIN alert_phones a ON a.subscriber_id=s.id '
                                  'WHERE a.phone=?', (self.phone,)):
            if not any(o['id'] == r['id'] for o in out):
                out.append(r)
        return [s for s in out
                if store.authorization(s, self.day, self.cfg.get('enforce_active_expiry', False))[0]]

    def history(self):
        subs = [s for s in self.allowed_subs()
                if self.con.execute('SELECT 1 FROM noise_events WHERE subscriber_id=? AND started>?',
                                    (s['id'], self.now - 86400)).fetchone()]
        if not subs:
            if not self.allowed_subs():
                return hangup(self.cfg, t(self.cfg, 'not_registered'))
            return hangup(self.cfg, t(self.cfg, 'history_none'))
        if len(subs) == 1:
            return self.history_for(subs[0]['id'])
        self.data['hsubs'] = [[s['id'], s['main_phone']] for s in subs[:8]]
        self.save('his_sub')
        files = []
        for i, (_, main) in enumerate(self.data['hsubs'], 1):
            files += [t(self.cfg, 'history_pick'), {'digits': main}, t(self.cfg, 'pick_press'), {'number': str(i)}]
        return [ask('hsub', files)]

    def on_history_sub(self, key):
        subs = self.data.get('hsubs', [])
        if key.isdigit() and 1 <= int(key) <= len(subs):
            return self.history_for(subs[int(key) - 1][0])
        return hangup(self.cfg, t(self.cfg, 'goodbye'))

    def history_for(self, sid):
        rows = self.con.execute('SELECT * FROM noise_events WHERE subscriber_id=? AND started>? '
                                'ORDER BY started DESC LIMIT 30', (sid, self.now - 86400)).fetchall()
        self.data['hsid'] = sid
        self.data['hev'] = [[r['started'], r['file_id'], int(round(r['noise_sec'] or 0)), r['peak']] for r in rows]
        self.data['hidx'] = 0
        intro = t(self.cfg, 'history_intro', n=len(rows))
        return self.history_play(0, intro)

    def history_details(self, idx):
        import zoneinfo
        tz = zoneinfo.ZoneInfo(self.cfg.get('timezone', 'Asia/Jerusalem'))
        started, _, sec, peak = self.data['hev'][idx]
        mins = max(0, int((self.now - started) // 60))
        ago = f'{mins} דקות' if mins < 120 else f'{mins // 60} שעות'
        when = datetime.datetime.fromtimestamp(started, tz).strftime('%H:%M')
        text = t(self.cfg, 'history_item', i=idx + 1, hh=when, ago=ago, sec=sec)
        if peak:
            text += ' ' + t(self.cfg, 'history_peak', peak=peak)
        return text

    def history_menu(self, files):
        self.save('his_nav')
        return [{'type': 'simpleMenu', 'name': 'hnav', 'enabledKeys': '0,1,2,3,5', 'times': 1,
                 'timeout': int(self.cfg.get('history_gap', 2)), 'errorReturn': 'NEXT',
                 'files': [f for f in files if f]}]

    def history_play(self, idx, prefix=None):
        events = self.data.get('hev', [])
        if idx >= len(events):
            self.save(None)
            return hangup(self.cfg, prefix, t(self.cfg, 'history_end'))
        idx = max(0, idx)
        self.data['hidx'] = idx
        fid = events[idx][1]
        return self.history_menu([{'text': prefix} if prefix else None,
                                  {'fileId': fid} if fid else {'text': t(self.cfg, 'history_no_file')}])

    def on_history_nav(self, key):
        idx = self.data.get('hidx', 0)
        if key == '0':
            self.save(None)
            return hangup(self.cfg, t(self.cfg, 'goodbye'))
        if key == '1':
            return self.history_play(max(0, idx - 1), t(self.cfg, 'history_first') if idx == 0 else None)
        if key == '2':
            return self.history_play(idx)
        if key == '5':  # time and details of the current recording, then continue
            return self.history_menu([{'text': self.history_details(idx)}])
        return self.history_play(idx + 1)  # '3' or no key: next (older)

    # ----- 3: info -----
    def info(self):
        self.save(None)
        return hangup(self.cfg, t(self.cfg, 'info'))

    # ----- 4: phones -----
    def phones(self, prefix=None):
        sub, _, err = self.subscriber()
        if err:
            return err
        self.reset_tries()
        self.save('ph_menu')
        return [ask('phm', [prefix, t(self.cfg, 'phones_menu')])]

    def on_phones_menu(self, key):
        sub, _, err = self.subscriber()
        if err:
            return err
        if key == '1':
            self.save('ph_add')
            return [ask('phn', [t(self.cfg, 'phones_ask')], mx=10, mn=9, timeout=6, confirm='digits', skip=True)]
        extras = self.con.execute('SELECT * FROM phones WHERE subscriber_id=? ORDER BY id', (sub['id'],)).fetchall()
        if key == '2':
            files = [t(self.cfg, 'phones_main'), {'digits': sub['main_phone']}]
            if extras:
                files.append(t(self.cfg, 'phones_extra'))
                files += [{'digits': e['phone']} for e in extras]
            else:
                files.append(t(self.cfg, 'phones_none'))
            return [say(*files)] + self.phones()
        if key == '3':
            if not extras:
                return self.phones(t(self.cfg, 'phones_none'))
            self.data['extras'] = [[e['id'], e['phone']] for e in extras[:8]]
            self.save('ph_rm')
            files = []
            for i, e in enumerate(extras[:8], 1):
                files += [t(self.cfg, 'phones_remove'), {'digits': e['phone']}, t(self.cfg, 'pick_press'),
                          {'number': str(i)}]
            files.append(t(self.cfg, 'back_zero'))
            return [ask('phr', files)]
        if key == '0':
            return hangup(self.cfg, t(self.cfg, 'goodbye'))
        if self.retry():
            return self.phones(t(self.cfg, 'bad_key'))
        return hangup(self.cfg, t(self.cfg, 'goodbye'))

    def on_phones_add(self, value):
        sub, _, err = self.subscriber()
        if err:
            return err
        if value in ('SKIP', '', 'ERROR'):
            return self.phones()
        try:
            store.add_phone(self.con, sub['id'], value, 'נוסף בטלפון', int(self.cfg.get('max_extra_phones', 5)))
        except ValueError as e:
            return self.phones(str(e))
        return self.phones(t(self.cfg, 'phones_added'))

    def on_phones_rm(self, key):
        sub, _, err = self.subscriber()
        if err:
            return err
        extras = self.data.get('extras', [])
        if key.isdigit() and 1 <= int(key) <= len(extras):
            store.remove_phone(self.con, sub['id'], extras[int(key) - 1][0])
            return self.phones(t(self.cfg, 'phones_removed'))
        return self.phones()

    # ----- 6: account -----
    def account(self, prefix=None):
        sub, _, err = self.subscriber(allow_inactive=True)
        if err:
            return err
        self.save('acc_menu')
        return [ask('acm', [prefix, t(self.cfg, 'account_menu'),
                            t(self.cfg, 'account_pay') if self.payments_on() else None])]

    # ----- payments (Technoline creditCard module -> Nedarim Plus) -----
    def payments_on(self):
        return bool(self.cfg.get('payments'))

    def prices(self):
        p = self.cfg.get('payments') or {}
        return float(p.get('monthly', 8.9)), float(p.get('annual', 82))

    @staticmethod
    def money(x):
        return ('%.2f' % x).rstrip('0').rstrip('.')

    def pay_menu(self, sub=None, prefix=None):
        if sub is None:
            sub, _, err = self.subscriber(auto_trial=True, allow_inactive=True)
            if err:
                return err
        monthly, annual = self.prices()
        self.reset_tries()
        self.save('pay_plan')
        return [ask('pplan', [prefix, t(self.cfg, 'pay_menu', monthly=self.money(monthly),
                                         annual=self.money(annual))], timeout=8)]

    def on_pay_plan(self, key):
        sub, _, err = self.subscriber(auto_trial=True, allow_inactive=True)
        if err:
            return err
        if key not in ('1', '2'):
            if key != '0' and self.retry():
                return self.pay_menu(sub, t(self.cfg, 'bad_key'))
            return hangup(self.cfg, t(self.cfg, 'goodbye'))
        monthly, annual = self.prices()
        p = self.cfg.get('payments') or {}
        cmd = {'type': 'creditCard', 'name': 'payment', 'cvv': 'yes', 'tz': 'yes', 'payments': 1,
               'category': f"{p.get('category', 'שמרטפון')} {sub['id']}",
               'files': []}
        if key == '1':
            cmd.update({'sum': monthly, 'paymentsMode': 'kevaMonthly', 'kevaMonths': ''})
            self.data['pay'] = ['monthly', monthly]
        else:
            cmd.update({'sum': annual, 'paymentsMode': 'regular'})
            self.data['pay'] = ['annual', annual]
        if p.get('terminal'):
            cmd['terminal'] = str(p['terminal'])
        cmd.pop('files')
        self.save('paying')
        return [say(t(self.cfg, 'pay_card_intro')), cmd]

    def on_payment(self, status):
        sub, _, err = self.subscriber(allow_inactive=True)
        if err:
            return err
        plan, amount = self.data.get('pay', ['monthly', 0])
        confirm = self.p.get('CONFIRM_payment') or None
        self.save(None)
        if status != 'OK':
            store.audit(self.con, 'payment_failed', {'id': sub['id'], 'plan': plan, 'status': status[:40]})
            return hangup(self.cfg, t(self.cfg, 'pay_failed'))
        months = 12 if plan == 'annual' else 1
        new = store.record_payment(self.con, sub['id'], source='phone', months=months, amount=amount,
                                   confirm=confirm, day=self.day)
        sub = self.con.execute('SELECT * FROM subscribers WHERE id=?', (sub['id'],)).fetchone()
        until = say_date(new.isoformat() if new else sub['expires'])
        return hangup(self.cfg, t(self.cfg, 'pay_ok_' + plan, date=until), t(self.cfg, 'pay_next'))

    def on_account_menu(self, key):
        sub, _, err = self.subscriber(allow_inactive=True)
        if err:
            return err
        if key == '1':
            parts = [t(self.cfg, 'account_status', status=store.STATUSES.get(sub['status'], ''),
                       plan=store.PLANS.get(sub['plan'], ''))]
            if sub['expires'] and sub['plan'] != 'free':
                parts.append(t(self.cfg, 'account_until', date=say_date(sub['expires'])))
            return [say(*parts)] + self.account()
        if key == '2':
            ver = store.verified_phones(self.con, sub['id'])
            if not ver:
                return self.account(t(self.cfg, 'account_no_verified'))
            self.data['verified'] = ver[:8]
            self.save('acc_rm')
            files = [t(self.cfg, 'account_verified')] + [{'digits': v} for v in ver[:8]]
            for i, v in enumerate(ver[:8], 1):
                files += [t(self.cfg, 'account_remove'), {'digits': v}, t(self.cfg, 'pick_press'), {'number': str(i)}]
            files.append(t(self.cfg, 'back_zero'))
            return [ask('acr', files)]
        if key == '3':
            ok, _ = store.authorization(sub, self.day, self.cfg.get('enforce_active_expiry', False))
            return self.phones() if ok else self.account(t(self.cfg, 'disabled'))
        if key == '4':
            ok, _ = store.authorization(sub, self.day, self.cfg.get('enforce_active_expiry', False))
            if not ok:
                return self.account(t(self.cfg, 'disabled'))
            self.data.pop('next', None)
            return self.ask_verify_number(sub, [])
        if key == '5' and self.payments_on():
            return self.pay_menu(sub)
        return hangup(self.cfg, t(self.cfg, 'goodbye'))

    def on_account_rm(self, key):
        sub, _, err = self.subscriber(allow_inactive=True)
        if err:
            return err
        ver = self.data.get('verified', [])
        if key.isdigit() and 1 <= int(key) <= len(ver):
            store.remove_verified(self.con, sub['id'], ver[int(key) - 1])
            if sub['alert_phone'] == ver[int(key) - 1]:
                store.update_subscriber(self.con, sub['id'], alert_phone=None)
            return self.account(t(self.cfg, 'removed'))
        return self.account()

    # ----- 9: message -----
    def message(self):
        sub = store.find_by_phone(self.con, self.phone)
        self.save('recording', subscriber_id=sub['id'] if sub else None, mode='message')
        return [{'type': 'record', 'name': 'rec', 'max': 180, 'min': 2, 'confirm': 'confirmOnly',
                 'hangupSave': 'yes', 'files': [{'text': t(self.cfg, 'message_prompt')}]}]

    def store_message(self):
        p = self.p
        try:
            dur = float(p.get('DURATION_rec') or 0)
        except ValueError:
            dur = 0
        self.con.execute('INSERT INTO messages(subscriber_id,phone,call_id,file_id,file_name,duration,created,state) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (self.sess['subscriber_id'] if self.sess else None, self.phone, self.call_id,
                          p.get('FILEID_rec'), p.get('FILE_rec') or p.get('rec'), dur, self.now,
                          'pending' if self.cfg.get('smtp') else 'stored'))
        self.data['saved'] = True
        self.wake = True

    def on_record(self, _value):
        if self.p.get('FILEID_rec') and not self.data.get('saved'):
            self.store_message()
        self.save(None)
        return hangup(self.cfg, t(self.cfg, 'message_thanks'))


def apply_voice(cfg, resp):
    """Give fixed prompts the configured voice (e.g. a Gemini voice).

    Technoline caches every unique text, so fixed prompts are paid for once. Texts that change
    per call (times, dates, counts, codes) keep the free default voice so they are not billed
    on every call.
    """
    voice = cfg.get('voice')
    if not voice:
        return resp
    fixed = {t(cfg, k) for k, v in TEXTS.items() if '{' not in v}
    for cmd in resp:
        for f in cmd.get('files', []) or []:
            if isinstance(f, dict) and f.get('text') in fixed:
                f['voice'] = voice
    return resp


def handle(cfg, con, p, day, mode='activate', now=None):
    """Returns (commands, wake_worker)."""
    c = Call(cfg, con, p, day, now or time.time())
    resp = c.run(mode if mode in MODES else 'activate')
    return apply_voice(cfg, resp), c.wake
