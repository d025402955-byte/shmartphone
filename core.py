"""Call flow (Technoline Module API), monitor events and alert calls (Campaign API)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import store

DEFAULT_TEXTS = {
    'alert_noise': 'שמרטפון. זוהה רעש בחדר התינוק.',
    'alert_disconnected': 'שמרטפון. הקו בחדר התינוק התנתק.',
    'alert_reconnect': '',  # not spoken: the answered call goes into the monitoring extension
    'alert_balance': 'שמרטפון. יתרת הפיקדון נגמרה והניטור הסתיים.',
}


def text(cfg, key, **kw):
    t = cfg.get('texts', {}).get(key) or DEFAULT_TEXTS[key]
    return t.format(**kw)


# ---------- monitor events ----------

ALERT_KINDS = ('noise', 'disconnected', 'balance')


def _num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def record_noise(con, sess, ev, now):
    """Keep a history row per noise event (start + end/recording)."""
    eid = str(ev['eventId'])[:64]
    con.execute('INSERT OR IGNORE INTO noise_events(subscriber_id,call_id,event_id,started,peak,created) '
                'VALUES(?,?,?,?,?,?)', (sess['subscriber_id'], sess['call_id'], eid,
                                       _num(ev.get('startedAt')) or now, _num(ev.get('level'), int), now))
    if ev.get('event') == 'noiseEnd':
        file_id = None if ev.get('fileError') else (str(ev.get('fileId') or '')[:64] or None)
        con.execute('UPDATE noise_events SET ended=?, noise_sec=?, peak=COALESCE(?,peak), file_id=COALESCE(?,file_id), '
                    'started=COALESCE(?,started) WHERE event_id=?',
                    (now, _num(ev.get('noiseSec')), _num(ev.get('peakLevel'), int), file_id,
                     _num(ev.get('startedAt')), eid))


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
        con.execute("UPDATE sessions SET status='monitoring', mon_start=COALESCE(mon_start,?) WHERE call_id=?",
                    (now, call_id))
    if kind in ('noise', 'noiseEnd') and ev.get('eventId'):
        record_noise(con, sess, ev, now)
    delay = None
    if kind == 'disconnected':
        con.execute('UPDATE sessions SET ended=COALESCE(ended,?), status=? WHERE call_id=?',
                    (now, 'disconnected ' + str(ev.get('reason', '')), call_id))
        if settle(cfg, con, call_id, now) == 'empty':
            kind = 'balance'  # the deposit ran out: say so instead of 'disconnected'
        if not cfg.get('alert_on_disconnect', True) and kind == 'disconnected':
            return 200, 'ok'
    if kind not in ALERT_KINDS:
        return 200, 'ok'
    if not sess['alert_phone']:
        return 200, 'muted'  # the parent stopped alerts from this room
    key = f"{kind}:{ev.get('eventId') or call_id}"
    if con.execute('SELECT 1 FROM alerts WHERE unique_key=?', (key,)).fetchone():
        return 200, 'duplicate'
    cooldown = float(cfg.get('alert_cooldown', 120))
    recent = con.execute(
        "SELECT 1 FROM alerts WHERE subscriber_id=? AND kind=? AND created>? "
        "AND state IN ('pending','sending','sent','demo','unknown')",
        (sess['subscriber_id'], kind, now - cooldown)).fetchone()
    state = 'skipped' if recent else 'pending'
    con.execute('INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state,result,not_before) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (key, sess['subscriber_id'], call_id, kind, sess['alert_phone'], now, state,
                 json.dumps({'note': 'cooldown'}) if recent else None, delay))
    return 200, 'queued' if state == 'pending' else 'cooldown'


def settle(cfg, con, call_id, now=None):
    """Charge a finished prepaid session; when the deposit is used up, queue one 'balance' alert.
    Returns 'empty' when the balance is exhausted, else ''."""
    per_minute = max(1, int((cfg.get('payments') or {}).get('per_minute_agorot', 1)))
    store.settle_session(con, call_id, per_minute, now)
    s = con.execute('SELECT * FROM sessions WHERE call_id=?', (call_id,)).fetchone()
    if not s or not s['subscriber_id'] or not s['mon_start']:
        return ''
    sub = con.execute('SELECT * FROM subscribers WHERE id=?', (s['subscriber_id'],)).fetchone()
    if not sub or sub['plan'] != 'prepaid' or store.balance(sub) >= per_minute:
        return ''
    if s['alert_phone']:
        con.execute('INSERT OR IGNORE INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state) '
                    'VALUES(?,?,?,?,?,?,?)', (f'balance:{call_id}', sub['id'], call_id, 'balance', s['alert_phone'],
                                              now or time.time(), 'pending'))
    return 'empty'


def queue_reconnect(cfg, con, sub_id, room_phone, prev_call_id, requested_by, now=None):
    """Ask Technoline to call the room phone into the monitoring extension (it must auto-answer).

    Returns (ok, reason)."""
    now = now or time.time()
    if not cfg.get('reconnect_extension'):
        return False, 'not_configured'
    recent = con.execute("SELECT COUNT(*) FROM alerts WHERE subscriber_id=? AND kind='reconnect' AND created>?",
                         (sub_id, now - 7200)).fetchone()[0]
    if recent >= int(cfg.get('reconnect_max_per_2h', 4)):
        return False, 'too_many'
    con.execute('INSERT INTO alerts(unique_key,subscriber_id,call_id,kind,alert_phone,created,state,payload) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (f'reconnect:{prev_call_id}:{int(now)}', sub_id, prev_call_id, 'reconnect', room_phone, now, 'pending',
                 json.dumps({'by': requested_by})))
    store.audit(con, 'reconnect', {'id': sub_id, 'room_phone': room_phone, 'by': requested_by})
    return True, ''


# ---------- alert calls ----------

def campaign_payload(cfg, kind, phone, override_text=None):
    payload = {
        'action': 'campaignRun', 'apiKey': cfg['api_key'], 'phones': [phone],
        'audioText': override_text or text(cfg, 'alert_' + kind) or None, 'title': 'shmartphone ' + kind,
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


PING_CODES = {'noise': '1111', 'disconnected': '2222', 'balance': '3333'}
VOICE_TEXTS = {'noise': 'שמרטפון. זוהה רעש בחדר התינוק. בשעה {code}.',
               'disconnected': 'שמרטפון. הקו בחדר התינוק התנתק. בשעה {code}.',
               'balance': 'שמרטפון. יתרת הפיקדון נגמרה והניטור הסתיים. בשעה {code}.'}


def send_ping(cfg, kind, phone):
    """Alert by makeCall: 'ping' rings and drops (caller ID 0510 + code tells the kind); 'voice' reads a short
    free message. The parent calls the line back to hear details. Needs the server IP approved at Technoline."""
    mode = cfg.get('alert_mode', 'ping')
    code = str((cfg.get('ping_codes') or {}).get(kind) or PING_CODES.get(kind, '9999'))
    params = {'action': 'makeCall', 'phone': phone, 'cid': code}
    if cfg.get('ping_caller_id'):
        params['callerId'] = str(cfg['ping_caller_id'])
    if mode == 'voice':  # the required 4-digit code is read out: make it the time (HHMM)
        import datetime
        import zoneinfo
        params['cid'] = datetime.datetime.now(zoneinfo.ZoneInfo(cfg.get('timezone', 'Asia/Jerusalem'))).strftime('%H%M')
        params.update({'mode': 'voice', 'apiKey': cfg['api_key'],
                       'voiceText': ((cfg.get('voice_texts') or {}).get(kind) or VOICE_TEXTS.get(kind, ''))[:200]})
    url = cfg.get('files_url', 'https://api.tlivr.com/ivrFilesApi.php')
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                                 headers={'User-Agent': 'curl/8.5.0'}, method='POST')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as resp:
            result = json.loads(resp.read(65536) or b'{}')
    except urllib.error.HTTPError as e:
        return 'failed', {'note': f'makeCall HTTP {e.code}' + (' (server IP not approved)' if e.code == 403 else '')}
    if str(result.get('status', '')).upper() != 'OK' or str(result.get('errorCode')) != '0':
        return 'failed', {'errorCode': result.get('errorCode'), 'note': str(result.get('note') or '')[:300]}
    return 'sent', {'via': mode, 'callId': result.get('callId'), 'campaignId': result.get('campaignId')}


def send_alert(cfg, kind, phone, override_text=None, mode=None):
    """Deliver one alert. mode: ping / voice (free makeCall) or message (paid campaign call).
    Returns (state, result_dict). Raises on transport errors."""
    mode = mode or cfg.get('alert_mode') or 'message'
    if kind in PING_CODES and mode in ('ping', 'voice') and not cfg.get('demo'):
        state, result = send_ping({**cfg, 'alert_mode': mode}, kind, phone)
        if state == 'sent':
            return state, result
        fallback = send_campaign(cfg, kind, phone, override_text)  # never lose an alert
        return fallback[0], {**fallback[1], 'ping_failed': result}
    return send_campaign(cfg, kind, phone, override_text)


def send_campaign(cfg, kind, phone, override_text=None):
    """Call the Campaign API. Returns (state, result_dict). Raises on transport errors."""
    url = cfg.get('campaign_url', 'https://api.tlivr.com/campaignApi.php')
    payload = campaign_payload(cfg, kind, phone, override_text)
    if kind == 'verify':
        payload['demo'] = 'no'  # a verification code must always really be delivered
    if kind == 'disconnected' and cfg.get('alert_extension'):
        # interactive alert: the answered call goes into the 'alert' extension, which offers to reconnect
        payload.pop('audioText', None)
        payload.update({'messagesType': 'extensionActivation', 'extensionActivation': str(cfg['alert_extension'])})
    if kind == 'reconnect':  # call the room phone back into the activate extension
        payload.pop('audioText', None)
        payload.update({'messagesType': 'extensionActivation',
                        'extensionActivation': str(cfg['reconnect_extension']),
                        'callLength': int(cfg.get('reconnect_ring_seconds', 30))})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'curl/8.5.0'}, method='POST')
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as resp:
        result = json.loads(resp.read(65536))
    if str(result.get('status', '')).upper() != 'OK' or str(result.get('errorCode')) != '0':
        return 'failed', {'errorCode': result.get('errorCode'),
                          'note': str(result.get('note') or result.get('messige') or '')[:300]}
    keep = {k: result.get(k) for k in ('campaignId', 'phones', 'billing', 'accountSum')}
    return ('demo' if cfg.get('demo') and kind != 'verify' else 'sent'), keep


def process_one(cfg, db_path, sender=send_alert, log=print):
    con = store.connect(db_path)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM alerts WHERE state='pending' AND (not_before IS NULL OR not_before<=?) "
                          "ORDER BY id LIMIT 1", (time.time(),)).fetchone()
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
        payload = json.loads(row['payload']) if row['payload'] else {}
        mode = alert_mode_for(cfg, db_path, row['subscriber_id'])
        try:
            if payload.get('text') and row['kind'] != 'reconnect':
                state, result = sender(cfg, row['kind'], row['alert_phone'], payload['text'])
            elif mode:
                state, result = sender(cfg, row['kind'], row['alert_phone'], mode=mode)
            else:
                state, result = sender(cfg, row['kind'], row['alert_phone'])
        except Exception as e:  # the provider may have accepted it; never retry blindly
            log('SEND_ERROR', repr(e), flush=True)
            state, result = 'unknown', {'note': 'Delivery uncertain: ' + repr(e)[:200]}
    log('ALERT', row['id'], row['kind'], state,
        json.dumps(result, ensure_ascii=False) if row['kind'] != 'verify' else '', flush=True)
    con = store.connect(db_path)
    with con:
        con.execute('UPDATE alerts SET state=?, result=? WHERE id=?',
                    (state, json.dumps(result, ensure_ascii=False), row['id']))
        if mode == 'message' and state == 'sent' and row['kind'] in PING_CODES:
            store.charge_alert(con, row['subscriber_id'], result.get('billing'), row['id'])
    con.close()
    return True


def alert_mode_for(cfg, db_path, sid):
    """ping / voice / message for this subscriber. Paid messages need a positive deposit balance."""
    if not sid:
        return None
    con = store.connect(db_path)
    try:
        sub = con.execute('SELECT * FROM subscribers WHERE id=?', (sid,)).fetchone()
    finally:
        con.close()
    if not sub:
        return None
    mode = sub['alert_mode'] or cfg.get('alert_mode') or 'message'
    if mode == 'message' and sub['alert_mode'] == 'message' and store.balance(sub) <= 0:
        return cfg.get('free_alert_mode', 'ping') if cfg.get('alert_mode') in ('ping', 'voice') else 'message'
    return mode


# ---------- customer-service messages -> email ----------

def fetch_recording(cfg, file_id):
    url = cfg.get('files_url', 'https://api.tlivr.com/ivrFilesApi.php')
    data = urllib.parse.urlencode({'action': 'fileDownload', 'audio': file_id, 'apiKey': cfg['api_key']}).encode()
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'curl/8.5.0'}, method='POST')
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=60) as resp:
        body = resp.read(30 * 1024 * 1024)
        ctype = resp.headers.get('Content-Type', '')
    if 'json' in ctype or body[:1] == b'{':
        raise RuntimeError('download failed: ' + body[:200].decode('utf-8', 'replace'))
    return body


def send_email(cfg, msg_row, audio):
    import smtplib
    from email.message import EmailMessage
    s = cfg['smtp']
    m = EmailMessage()
    m['Subject'] = f"שמרטפון: הודעה חדשה מ-{msg_row['phone']}"
    m['From'] = s.get('from') or s['user']
    m['To'] = s['to']
    sub = msg_row['subscriber_id']
    m.set_content(f"הודעה קולית לשירות הלקוחות\nמטלפון: {msg_row['phone']}\n"
                  f"מנוי: {('#' + str(sub)) if sub else 'לא רשום'}\n"
                  f"אורך: {int(msg_row['duration'] or 0)} שניות\n"
                  f"זמן: {time.strftime('%d/%m/%Y %H:%M', time.localtime(msg_row['created']))}\n")
    if audio:
        m.add_attachment(audio, maintype='audio', subtype='mpeg', filename=f"message-{msg_row['id']}.mp3")
    port = int(s.get('port', 587))
    if port == 465:
        srv = smtplib.SMTP_SSL(s['host'], port, timeout=30)
    else:
        srv = smtplib.SMTP(s['host'], port, timeout=30)
        srv.starttls()
    with srv:
        srv.login(s['user'], s['password'])
        srv.send_message(m)


def process_message(cfg, db_path, fetch=fetch_recording, mail=send_email, log=print):
    if not cfg.get('smtp'):
        return False
    con = store.connect(db_path)
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM messages WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            con.rollback()
            return False
        con.execute("UPDATE messages SET state='sending' WHERE id=?", (row['id'],))
        con.commit()
    finally:
        con.close()
    try:
        audio = fetch(cfg, row['file_id']) if row['file_id'] else b''
        mail(cfg, row, audio)
        state, result = 'emailed', ''
    except Exception as e:
        state, result = 'failed', repr(e)[:300]
    log('MESSAGE', row['id'], state, result, flush=True)
    con = store.connect(db_path)
    with con:
        con.execute('UPDATE messages SET state=?, result=? WHERE id=?', (state, result, row['id']))
    con.close()
    return True
