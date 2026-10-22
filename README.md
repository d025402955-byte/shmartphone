# שמרטפון

מערכת ניטור תינוק טלפונית על מרכזיית טכנוליין, עם ניהול מנויים.

- **שיחה נכנסת** (מודול API): בודקת שהמספר שייך למנוי מורשה (טלפון ראשי או נוסף), שואלת לאיזה מספר לשלוח התראות (נשמר למנוי), ומפעילה `noiseMonitor`.
- **אירועי ניטור**: רעש או ניתוק → שיחת התראה דרך API הקמפיינים, עם מניעת כפילויות ומרווח בין התראות.
- **דף ניהול** (`/admin`, משתמש `admin`): חיפוש, מנוי חדש, עריכה, "שולם" (הארכת חודש/שנה), ניסיון, השבתה, מחיקה, טלפונים נוספים, פעילות, ייבוא מרישומון וייצוא CSV.

ללא תלות חיצונית: Python 3.11+ בלבד, SQLite.

## התקנה בשרת

```
git clone https://github.com/d025402955-byte/shmartphone /opt/shmartphone-app
python3 /opt/shmartphone-app/install.py
```

עדכון: `cd /opt/shmartphone-app && git pull && python3 install.py`
חזרה לגרסה הקודמת: `python3 /opt/shmartphone-app/install.py rollback`
סיסמת ניהול חדשה: `python3 /opt/shmartphone-app/install.py password`

ההגדרות נשמרות ב-`/etc/shmartphone/app.json` (לא במאגר). הנתונים ב-`/var/lib/shmartphone/app.db`.

## כללי הרשאה

- `פעיל` → מורשה (חינם תמיד מורשה). עם `enforce_active_expiry: true` גם מנוי פעיל נחסם אחרי תאריך התוקף.
- `תקופת ניסיון` → מורשה עד תאריך התוקף.
- `מושבת` / `מבוטל` → לא מורשה.
- מספר לא מוכר מקבל ניסיון אוטומטי של `auto_trial_days` ימים (0 = כבוי).

## בדיקות

```
python3 -m unittest tests.test_all
```
