"""Call flow (Technoline Module API), monitor events and alert calls (Campaign API)."""
import json
import time
import urllib.request

import store

DEFAULT_TEXTS = {
    'welcome_trial': 'ברוכים הבאים לשמרטפון. הופעלה עבורכם תקופת ניסיון של {days} ימים.',
    'not_registered': 'מספר הטלפון שממנו התקשרתם אינו רשום בשירות שמרטפון.',
    'disabled': 'המנוי שלכם אינו פעיל.',
    'trial_expired': 'תקופת הניסיון שלכם הסתיימה.',
    'expired': 'תוקף המנוי שלכם הסתיים.',
    'renew': 'לחידוש המנוי פנו למשרד. תודה.',
    'confirm_before': 'ההתראות יישלחו למספר',
    'confirm_after': 'לאישור הקישו 1. למספר אחר הקישו 2.',
    'ask_number': 'הקישו את מספר הטלפון שאליו יישלחו ההתראות, ובסיומו הקישו סולמית.',
    'bad_number': 'המספר שהוקש אינו תקין.',
    'same_number': 'לא ניתן לשלוח התראות לטלפון שנמצא בחדר. הקישו מספר אחר.',
    'too_many': 'לא התקבל מספר תקין. להתראות.',
    'starting': 'הניטור מתחיל. אפשר להניח את הטלפון בחדר.',
    'busy': 'כבר פועל ניטור מהטלפון הזה.',
    'error': 'אירעה תקלה בהפעלת הניטור. נסו שוב מאוחר יותר.',
    'alert_noise': 'שמרטפון. זוהה רעש בחדר התינוק.',
    'alert_disconnected': 'שמרטפון. הקו בחדר התינוק התנתק.',
}

MONITOR_DEFAULTS = {
    'threshold': 55, 'minDuration': 2, 'maxGap': 0.5, 'silenceEnd': 3, 'preRoll': 2,
    'maxClip': 120, 'cooldown': 0, 'maxListeners': 1, 'joinBeep': 'yes',
}


def text(cfg, key, **kw):
    t = cfg.get('texts', {}).get(key) or DEFAULT_TEXTS[key]
    return t.format(**kw)


def say(*parts):
    return {'type': 'simpleMessage', 'files': [{'text': p} for p in parts if p]}


def goodbye(cfg, *parts):
    cmds = [say(*parts)]
    if cfg.get('unauthorized_goto'):
        cmds.append({'type': 'goTo', 'goTo': cfg['unauthorized_goto']})
    else:
        cmds.append({'type': 'hangup'})
    return cmds


def room_id(sub_id, phone):
    return f's{sub_id}-{phone}'[:40]


def monitor_cmd(cfg, room):
    cmd = {'type': 'noiseMonitor', 'name': 'nm', 'roomId': room,
           'alertUrl': cfg['public_url'].rstrip('/') + '/events/' + cfg['event_token']}
    params = dict(MONITOR_DEFAULTS)
    params.update(cfg.get('monitor', {}))
    cmd.update(params)
    return cmd


def ask_number(cfg, prefix=None):
    files = []
    if prefix:
        files.append({'text': prefix})
    files.append({'text': text(cfg, 'ask_number')})
    return {'type': 'getDTMF', 'name': 'alertnum', 'min': 9, 'max': 10, 'timeout': 6,
            'skipKey': '#', 'skipValue': 'SKIP', 'confirmType': 'digits', 'files': files}


def handle_ivr(cfg, con, p, day, now=None):
    """Return the JSON-able response for one Module API request."""
    now = now or time.time()
    call_id = p.get('PBXcallId', '')
    phone = store.norm_phone(p.get('PBXphone', ''))

    if p.get('PBXcallStatus') == 'HANGUP':
        con.execute("UPDATE sessions SET ended=?, status=COALESCE(status,'') || ' hangup' "
                    "WHERE call_id=? AND ended IS NULL", (now, call_id))
        return []

    if cfg.get('pbx_number') and p.get('PBXnum') != cfg['pbx_number']:
        return [{'type': 'hangup'}]

    # Monitoring already ran and returned (TIMEOUT / BUSY / errors).
    if 'nm' in p:
        con.execute('UPDATE sessions SET ended=?, status=? WHERE call_id=?', (now, p['nm'], call_id))
        if p['nm'] == 'BUSY':
            return goodbye(cfg, text(cfg, 'busy'))
        if p['nm'] in ('TIMEOUT', 'HANGUP'):
            return [{'type': 'hangup'}]
        return goodbye(cfg, text(cfg, 'error'))

    sub = store.find_by_phone(con, phone)
    intro = None
    if sub is None:
        days = int(cfg.get('auto_trial_days', 0) or 0)
        if days <= 0 or not store.valid_phone(phone):
            return goodbye(cfg, text(cfg, 'not_registered'), text(cfg, 'renew'))
        import datetime
        exp = (day + datetime.timedelta(days=days)).isoformat()
        sid = store.create_subscriber(con, main_phone=phone, plan='trial', status='trial',
                                      expires=exp, notes='נרשם אוטומטית בשיחה ראשונה')
        sub = con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
        intro = text(cfg, 'welcome_trial', days=days)

    ok, reason = store.authorization(sub, day, cfg.get('enforce_active_expiry', False))
    if not ok:
        return goodbye(cfg, text(cfg, reason if reason in DEFAULT_TEXTS else 'disabled'), text(cfg, 'renew'))

    con.execute('INSERT OR IGNORE INTO sessions(call_id,subscriber_id,phone,started,status) '
                'VALUES(?,?,?,?,?)', (call_id, sub['id'], phone, now, 'auth'))

    def start(alert_phone):
        room = room_id(sub['id'], phone)
        con.execute('UPDATE sessions SET room_id=?, alert_phone=?, status=? WHERE call_id=?',
                    (room, alert_phone, 'monitoring', call_id))
        return [say(intro, text(cfg, 'starting')), monitor_cmd(cfg, room)]

    if 'alertnum' in p:
        entered = store.norm_phone(p['alertnum'])
        problem = None
        if not store.valid_phone(entered):
            problem = text(cfg, 'bad_number')
        elif entered == phone:
            problem = text(cfg, 'same_number')
        if problem:
            tries = con.execute('UPDATE sessions SET tries=tries+1 WHERE call_id=? RETURNING tries',
                                (call_id,)).fetchone()
            if tries and tries[0] >= 3:
                return goodbye(cfg, text(cfg, 'too_many'))
            return [ask_number(cfg, problem)]
        store.update_subscriber(con, sub['id'], alert_phone=entered)
        return start(entered)

    if p.get('conf') == '1' and sub['alert_phone']:
        return start(sub['alert_phone'])
    if p.get('conf') == '2':
        return [ask_number(cfg)]

    if sub['alert_phone'] and sub['alert_phone'] != phone:
        files = [{'text': intro}] if intro else []
        files += [{'text': text(cfg, 'confirm_before')}, {'digits': sub['alert_phone']},
                  {'text': text(cfg, 'confirm_after')}]
        return [{'type': 'getDTMF', 'name': 'conf', 'min': 1, 'max': 1, 'timeout': 7,
                 'confirmType': 'no', 'files': files}]
    return [ask_number(cfg, intro)]


# ---------- monitor events ----------

ALERT_KINDS = ('noise', 'disconnected')


def handle_event(cfg, con, ev, header_event, now=None):
    """Validate and record one noise-monitor event. Returns (http_status, message)."""
    now = now or time.time()
    kind = ev.get('event')
    if not kind or header_event != kind:
        return 400, 'event mismatch'
    if cfg.get('pbx_number') and str(ev.get('num')) != cfg['pbx_number']:
        return 403, 'wrong system'
    try:
        if abs(now - float(ev.get('time', 0))) > 900:
            return 400, 'stale'
    except (TypeError, ValueError):
        return 400, 'bad time'
    call_id = str(ev.get('callId', ''))
    sess = con.execute('SELECT * FROM sessions WHERE call_id=?', (call_id,)).fetchone()
    if not sess or sess['room_id'] != ev.get('roomId'):
        return 404, 'unknown session'
    if kind == 'monitorStart':
        con.execute("UPDATE sessions SET status='monitoring' WHERE call_id=?", (call_id,))
    if kind == 'disconnected':
        con.execute('UPDATE sessions SET ended=COALESCE(ended,?), status=? WHERE call_id=?',
                    (now, 'disconnected ' + str(ev.get('reason', '')), call_id))
        if not cfg.get('alert_on_disconnect', True):
            return 200, 'ok'
    if kind not in ALERT_KINDS:
        return 200, 'ok'
    key = f"{kind}:{ev.get('eventId') or call_id}"
    if con.execute('SELECT 1 FROM alerts WHERE unique_key=?', (key,)).fetchone():
        return 200, 'duplicate'
    cooldown = float(cfg.get('alert_cooldown', 120))
    recent = con.execute(
        "SELECT 1 FROM alerts WHERE subscriber_id=? AND kind=? AND created>? "
        "AND state IN ('pending','sending','sent','demo','unknown')",
        (sess['subscriber_id'], kind, now - cooldown)).fetchone()
    state = 'skipped' if recent else 'pending'
    con.execute('INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state,result) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (key, sess['subscriber_id'], call_id, kind, sess['alert_phone'], now, state,
                 json.dumps({'note': 'cooldown'}) if recent else None))
    return 200, 'queued' if state == 'pending' else 'cooldown'


# ---------- alert calls ----------

def campaign_payload(cfg, kind, phone):
    payload = {
        'action': 'campaignRun', 'apiKey': cfg['api_key'], 'phones': [phone],
        'audioText': text(cfg, 'alert_' + kind), 'title': 'shmartphone ' + kind,
        'callLength': int(cfg.get('call_length', 25)), 'betweenRetries': 5,
        'dialRetries': int(cfg.get('dial_retries', 1)), 'maxRing': 1, 'reasonableHours': 'no',
        'demo': 'yes' if cfg.get('demo') else 'no',
    }
    if cfg.get('call_id'):
        payload['callId'] = cfg['call_id']
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send_alert(cfg, kind, phone):
    """Call the Campaign API. Returns (state, result_dict). Raises on transport errors."""
    url = cfg.get('campaign_url', 'https://api.tlivr.com/campaignApi.php')
    req = urllib.request.Request(url, data=json.dumps(campaign_payload(cfg, kind, phone)).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'curl/8.5.0'}, method='POST')
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as resp:
        result = json.loads(resp.read(65536))
    if str(result.get('status', '')).upper() != 'OK' or str(result.get('errorCode')) != '0':
        return 'failed', {'errorCode': result.get('errorCode'),
                          'note': str(result.get('note') or result.get('messige') or '')[:300]}
    keep = {k: result.get(k) for k in ('campaignId', 'phones', 'billing', 'accountSum')}
    return ('demo' if cfg.get('demo') else 'sent'), keep


def process_one(cfg, db_path, sender=send_alert, log=print):
    con = store.connect(db_path)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM alerts WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            con.rollback()
            return False
        con.execute("UPDATE alerts SET state='sending' WHERE id=?", (row['id'],))
        con.commit()
    finally:
        con.close()
    if not row['alert_phone']:
        state, result = 'failed', {'note': 'no alert phone'}
    else:
        try:
            state, result = sender(cfg, row['kind'], row['alert_phone'])
        except Exception as e:  # the provider may have accepted it; never retry blindly
            log('SEND_ERROR', repr(e), flush=True)
            state, result = 'unknown', {'note': 'Delivery uncertain: ' + repr(e)[:200]}
    log('ALERT', row['id'], row['kind'], state, json.dumps(result, ensure_ascii=False), flush=True)
    con = store.connect(db_path)
    with con:
        con.execute('UPDATE alerts SET state=?, result=? WHERE id=?',
                    (state, json.dumps(result, ensure_ascii=False), row['id']))
    con.close()
    return True
