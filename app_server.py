"""Private Telegram Mini App; reuse the existing Telegram client and owner binding."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from bot import Telegram, APIError, Store, Bot, load_env

ROOT = Path(__file__).resolve().parent
CATEGORIES = ('bed','bath','hall','kitchen','balcony','sale','home','finance','shopping','documents')
DEFAULT_NAMES = ('Bedroom','Bathroom','Entryway','Kitchen','Balcony','Selling','Utilities','Finances','Shopping','Documents')
DEFAULT_ICONS = ('bed-double','bath','coat','cooking-pot','panels-top-left','coins','house','credit-card','shopping-cart','book-user')
PAIRING_LIFETIME = 15 * 60
SESSION_LIFETIME = 180 * 24 * 60 * 60
SESSION_COOKIE = '__Host-pearly_session'
UNLOCK_LIFETIME = 12 * 60 * 60
PIN_LOCK_TIME = 5 * 60
PIN_HASH_ITERATIONS = 600_000

def ensure_categories(db):
    db.execute('CREATE TABLE IF NOT EXISTS categories(id TEXT PRIMARY KEY,name TEXT NOT NULL,icon TEXT NOT NULL,position INTEGER NOT NULL)')
    for pos, (key, name, icon) in enumerate(zip(CATEGORIES, DEFAULT_NAMES, DEFAULT_ICONS)):
        db.execute('INSERT OR IGNORE INTO categories VALUES(?,?,?,?)',(key,name,icon,pos))

def category_state(db):
    return [dict(zip(('id','name','icon','position'), row)) for row in db.execute('SELECT id,name,icon,position FROM categories ORDER BY position,id')]

def initialize(path):
    # Same database and owner as previous releases. Reset only once, atomically.
    old = Store(path)
    with old.db:
        old.db.execute('CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, category TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)')
        old.db.execute('CREATE TABLE IF NOT EXISTS app_requests(id TEXT PRIMARY KEY)')
        old.db.execute('CREATE TABLE IF NOT EXISTS web_pairings(code_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at INTEGER NOT NULL)')
        old.db.execute('CREATE TABLE IF NOT EXISTS web_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at INTEGER NOT NULL)')
        session_columns = {column[1] for column in old.db.execute('PRAGMA table_info(web_sessions)')}
        for name, definition in (
            ('pin_salt', 'TEXT'),
            ('pin_hash', 'TEXT'),
            ('pin_failures', 'INTEGER NOT NULL DEFAULT 0'),
            ('pin_locked_until', 'INTEGER NOT NULL DEFAULT 0'),
            ('unlock_hash', 'TEXT'),
            ('unlock_expires_at', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if name not in session_columns:
                old.db.execute(f'ALTER TABLE web_sessions ADD COLUMN {name} {definition}')
        ensure_categories(old.db)
    old.db.close()

def digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()

def issue_pairing_code(db, user, now=None):
    now = int(time.time() if now is None else now)
    code = secrets.token_hex(12)
    with db:
        db.execute('DELETE FROM web_pairings WHERE user_id=? OR expires_at<=?', (user, now))
        db.execute('INSERT INTO web_pairings VALUES(?,?,?)', (digest(code), user, now + PAIRING_LIFETIME))
    return '-'.join(code.upper()[i:i+4] for i in range(0, len(code), 4))

def redeem_pairing_code(db, supplied, allowed, now=None):
    now = int(time.time() if now is None else now)
    code = supplied.replace('-', '').replace(' ', '').lower() if isinstance(supplied, str) else ''
    if len(code) != 24 or any(c not in '0123456789abcdef' for c in code):
        raise ValueError('Invalid code. Send /install to your Telegram bot for a new one.')
    row = db.execute('SELECT user_id,expires_at FROM web_pairings WHERE code_hash=?', (digest(code),)).fetchone()
    if not row or row[1] <= now or str(row[0]) not in {str(user) for user in allowed if user}:
        raise ValueError('Code expired. Send /install to your Telegram bot for a new one.')
    db.execute('DELETE FROM web_pairings WHERE code_hash=?', (digest(code),))
    db.execute('DELETE FROM web_sessions WHERE expires_at<=?', (now,))
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO web_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)', (digest(token), row[0], now + SESSION_LIFETIME))
    return token

def browser_session(db, cookie_header, allowed, now=None):
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header or '')
    except CookieError:
        return None
    item = cookies.get(SESSION_COOKIE)
    token = item.value if item else ''
    if not 20 <= len(token) <= 128 or not token.isascii():
        return None
    columns = ('token_hash','user_id','expires_at','pin_salt','pin_hash','pin_failures',
               'pin_locked_until','unlock_hash','unlock_expires_at')
    row = db.execute(f'SELECT {",".join(columns)} FROM web_sessions WHERE token_hash=?', (digest(token),)).fetchone()
    now = int(time.time() if now is None else now)
    if not row or row[2] <= now or str(row[1]) not in {str(user) for user in allowed if user}:
        return None
    return dict(zip(columns, row))

def session_user(db, cookie_header, allowed, now=None):
    session = browser_session(db, cookie_header, allowed, now)
    return session['user_id'] if session else None

def valid_pin(pin):
    return isinstance(pin, str) and len(pin) == 4 and all('0' <= char <= '9' for char in pin)

def pin_digest(pin, salt):
    return hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), bytes.fromhex(salt), PIN_HASH_ITERATIONS).hex()

def new_unlock(db, session, now):
    token = secrets.token_urlsafe(32)
    db.execute('UPDATE web_sessions SET unlock_hash=?,unlock_expires_at=? WHERE token_hash=?',
               (digest(token), now + UNLOCK_LIFETIME, session['token_hash']))
    return token

def setup_pin(db, session, pin, now=None):
    if not valid_pin(pin):
        raise ValueError('Choose exactly four digits (0–9)')
    if session['pin_hash']:
        raise ValueError('PIN is already set')
    now = int(time.time() if now is None else now)
    salt = secrets.token_hex(16)
    result = db.execute('UPDATE web_sessions SET pin_salt=?,pin_hash=?,pin_failures=0,pin_locked_until=0 '
                        'WHERE token_hash=? AND pin_hash IS NULL',
                        (salt, pin_digest(pin, salt), session['token_hash']))
    if result.rowcount != 1:
        raise ValueError('PIN is already set')
    return new_unlock(db, session, now)

def unlock_pin(db, session, pin, now=None):
    """Return (status, token, retry_after, remaining_attempts). Caller commits failures."""
    now = int(time.time() if now is None else now)
    if session['pin_locked_until'] > now:
        return 'locked', None, session['pin_locked_until'] - now, 0
    failures = 0 if session['pin_locked_until'] else session['pin_failures']
    if not valid_pin(pin) or not hmac.compare_digest(pin_digest(pin, session['pin_salt']), session['pin_hash']):
        failures += 1
        locked_until = now + PIN_LOCK_TIME if failures >= 5 else 0
        db.execute('UPDATE web_sessions SET pin_failures=?,pin_locked_until=? WHERE token_hash=?',
                   (failures, locked_until, session['token_hash']))
        return ('locked' if locked_until else 'wrong'), None, PIN_LOCK_TIME if locked_until else 0, max(0, 5 - failures)
    db.execute('UPDATE web_sessions SET pin_failures=0,pin_locked_until=0 WHERE token_hash=?', (session['token_hash'],))
    return 'ok', new_unlock(db, session, now), 0, 5

def unlocked_session(session, supplied, now=None):
    now = int(time.time() if now is None else now)
    return (isinstance(supplied, str) and 20 <= len(supplied) <= 128 and supplied.isascii()
            and session['unlock_expires_at'] > now and session['unlock_hash'] is not None
            and hmac.compare_digest(digest(supplied), session['unlock_hash']))

def authenticate(raw, token, owner, now=None):
    pairs = parse_qsl(raw, keep_blank_values=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('Invalid authentication')
    supplied = values.pop('hash', '')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{key}={value}' for key, value in sorted(values.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError('Open the app using its Telegram button')
    age = (time.time() if now is None else now) - int(values.get('auth_date', '0'))
    if not -30 <= age <= 86400:
        raise ValueError('Close and reopen the app in Telegram')
    user = json.loads(values.get('user', '{}')).get('id')
    if not isinstance(user, int) or str(user) not in {str(x) for x in (owner if isinstance(owner, (set, list, tuple)) else [owner]) if x}:
        raise ValueError('Access is restricted')
    return user

def mutate(db, data):
    ensure_categories(db)
    op = data.get('op')
    if op == 'state':
        return
    request_id = data.get('request_id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80:
        raise ValueError('Invalid request')
    if db.execute('SELECT 1 FROM app_requests WHERE id=?', (request_id,)).fetchone():
        return
    if op in ('create', 'edit'):
        title = data.get('title', '')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise ValueError('Task must contain 1 to 80 characters')
        if not db.execute('SELECT 1 FROM categories WHERE id=?',(data.get('category'),)).fetchone():
            raise ValueError('Choose a category')
        if op == 'create':
            db.execute('INSERT INTO steps(title,category) VALUES(?,?)', (title.strip(), data['category']))
        else:
            cur = db.execute('UPDATE steps SET title=?,category=? WHERE id=?', (title.strip(), data['category'], data.get('id')))
            if not cur.rowcount:
                raise ValueError('Task was deleted. Reload your list')
    elif op in ('complete','restore'):
        cur = db.execute('UPDATE steps SET done=? WHERE id=?', (int(op=='complete'), data.get('id')))
        if not cur.rowcount:
            raise ValueError('Task was deleted')
    elif op == 'delete':
        db.execute('DELETE FROM steps WHERE id=?',(data.get('id'),))
    elif op == 'category_create':
        name = data.get('name','')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError('Category must contain 1 to 40 characters')
        name = name.strip()
        if any(row[0].casefold()==name.casefold() for row in db.execute('SELECT name FROM categories')):
            raise ValueError('This category already exists')
        position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM categories').fetchone()[0]
        db.execute('INSERT INTO categories VALUES(?,?,?,?)',('custom_'+uuid.uuid4().hex,name,'tag',position))
    elif op == 'category_order':
        order = data.get('order')
        ids = {r[0] for r in db.execute('SELECT id FROM categories')}
        if not isinstance(order,list) or any(not isinstance(x,str) for x in order) or len(order)!=len(ids) or set(order)!=ids:
            raise ValueError('Categories changed. Reopen the order editor')
        for pos,key in enumerate(order):
            db.execute('UPDATE categories SET position=? WHERE id=?',(pos,key))
    elif op == 'clear':
        db.execute('DELETE FROM steps WHERE done=1')
    else:
        raise ValueError('Unknown action')
    db.execute('INSERT INTO app_requests VALUES(?)', (request_id,))

def user_database(path, user, owner):
    if str(user) == str(owner):
        return path
    guest_path = Path(path).with_name(f'pearly-user-{int(user)}.sqlite3')
    with sqlite3.connect(guest_path, timeout=15) as db:
        db.execute('CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, category TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS app_requests(id TEXT PRIMARY KEY)')
    return guest_path

def make_handler(path, token, allowed, extra=(), app_url=''):
    parsed_url = urlsplit(app_url)
    app_origin = f'{parsed_url.scheme}://{parsed_url.netloc}'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log auth data, token or task titles.

        def reply(self, status, body, kind='application/json; charset=utf-8', headers=None):
            body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json(self):
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                raise ValueError('Expected JSON')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('Request too large')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Invalid request')
            return data

        def same_origin(self):
            return self.headers.get('Origin') == app_origin

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, {'ok':True})
            if self.path in ('/', '/app'):
                return self.reply(200, (ROOT/'app.html').read_bytes(), 'text/html; charset=utf-8')
            for route, filename, kind in (
                ('/manifest.webmanifest', 'manifest.webmanifest', 'application/manifest+json'),
                ('/icon-192.png', 'icon-192.png', 'image/png'),
                ('/icon-512.png', 'icon-512.png', 'image/png'),
                ('/apple-touch-icon.png', 'apple-touch-icon.png', 'image/png'),
                ('/lucide.min.js', 'lucide.min.js', 'application/javascript; charset=utf-8'),
            ):
                if self.path == route:
                    return self.reply(200, (ROOT/filename).read_bytes(), kind)
            self.reply(404, {'error':'Not found'})

        def do_POST(self):
            if self.path not in ('/api', '/auth/redeem', '/auth/setup-pin', '/auth/unlock'):
                return self.reply(404, {'error':'Not found'})
            try:
                data = self.read_json()
                with sqlite3.connect(path, timeout=15) as db:
                    owner = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
                    owner_id = owner[0] if owner else allowed
                    if self.path == '/auth/redeem':
                        if not self.same_origin():
                            return self.reply(403, {'error':'Invalid origin'})
                        db.execute('BEGIN IMMEDIATE')
                        browser_token = redeem_pairing_code(db, data.get('code'), [owner_id])
                        db.commit()
                        cookie = f'{SESSION_COOKIE}={browser_token}; Path=/; Max-Age={SESSION_LIFETIME}; Secure; HttpOnly; SameSite=Strict'
                        return self.reply(200, {'ok':True,'needs_pin':True}, headers={'Set-Cookie':cookie})
                    if self.path in ('/auth/setup-pin', '/auth/unlock'):
                        if not self.same_origin():
                            return self.reply(403, {'error':'Invalid origin'})
                        db.execute('BEGIN IMMEDIATE')
                        session = browser_session(db, self.headers.get('Cookie'), [owner_id])
                        if not session:
                            return self.reply(401, {'auth':'pairing','error':'Enter your code from /install in Telegram'})
                        if self.path == '/auth/setup-pin':
                            if session['pin_hash']:
                                return self.reply(409, {'auth':'pin','error':'PIN is already set'})
                            unlock_token = setup_pin(db, session, data.get('pin'))
                            db.commit()
                            return self.reply(200, {'ok':True,'unlock_token':unlock_token})
                        if not session['pin_hash']:
                            return self.reply(428, {'auth':'setup','error':'Choose your four-digit PIN'})
                        status, unlock_token, retry_after, remaining = unlock_pin(db, session, data.get('pin'))
                        db.commit()
                        if status == 'locked':
                            return self.reply(429, {'auth':'pin','error':'Too many attempts. Try again in five minutes.',
                                                    'retry_after':retry_after}, headers={'Retry-After':str(retry_after)})
                        if status == 'wrong':
                            return self.reply(403, {'auth':'pin','error':'Incorrect PIN','remaining_attempts':remaining})
                        return self.reply(200, {'ok':True,'unlock_token':unlock_token})
                    init_data = self.headers.get('X-Telegram-Init-Data', '')
                    if init_data:
                        user = authenticate(init_data, token, [owner_id, *extra])
                    else:
                        if not self.same_origin():
                            return self.reply(403, {'error':'Invalid origin'})
                        session = browser_session(db, self.headers.get('Cookie'), [owner_id])
                        if not session:
                            return self.reply(401, {'auth':'pairing','error':'Enter your code from /install in Telegram'})
                        if not session['pin_hash']:
                            return self.reply(428, {'auth':'setup','error':'Choose your four-digit PIN'})
                        if not unlocked_session(session, self.headers.get('X-Pearly-Unlock')):
                            return self.reply(423, {'auth':'pin','error':'Enter your four-digit PIN'})
                        user = session['user_id']
                with sqlite3.connect(user_database(path, user, owner_id), timeout=15) as db:
                    db.row_factory = sqlite3.Row
                    db.execute('BEGIN IMMEDIATE')
                    mutate(db, data)
                    result = [dict(row) for row in db.execute('SELECT * FROM steps ORDER BY id')]
                    categories = category_state(db)
                self.reply(200, {'tasks':result,'categories':categories})
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {'error':str(exc)})
            except Exception:
                self.reply(500, {'error':'Could not save. Try again.'})
    return Handler

def main():
    load_env()
    token = os.environ.get('BOT_TOKEN', '').strip()
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0') or '0')
    extra = {int(x.strip()) for x in os.environ.get('ADDITIONAL_USER_IDS', '').split(',') if x.strip()}
    url = os.environ.get('APP_URL', '').strip().rstrip('/')
    if not url:
        domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip()
        url = 'https://' + domain if domain else ''
    if not token or not url.startswith('https://'):
        raise SystemExit('Set BOT_TOKEN and APP_URL=https://your-public-domain')
    path = Path(os.environ.get('DATABASE_PATH', str(ROOT/'data'/'domik.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate configuration before the one-time destructive migration.
    api = Telegram(token)
    api.call('getMe')
    if api.call('getWebhookInfo').get('url'):
        raise SystemExit('Existing webhook: disable it before polling')
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        owner = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone() if 'meta' in tables else None
    if not owner and not allowed and not os.environ.get('PAIRING_CODE'):
        raise SystemExit('Set ALLOWED_USER_ID or PAIRING_CODE')
    initialize(path)
    store = Store(path)
    gate = Bot(api, store, os.environ.get('PAIRING_CODE',''), allowed)
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), make_handler(path, token, allowed, extra, url))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api.call('setMyCommands', commands=[{'command':'start','description':'Open Pearly'},{'command':'install','description':'Connect iPhone home screen app'},{'command':'id','description':'Show my Telegram ID'}])
    api.call('setChatMenuButton', menu_button={'type':'web_app','text':'Open','web_app':{'url':url}})
    print('Pearly v3 ready', flush=True)
    while True:
        try:
            updates = api.call('getUpdates', offset=int(store.get('offset','0')), timeout=30, allowed_updates=['message','callback_query'])
            for update in updates:
                callback = update.get('callback_query')
                message = update.get('message') or (dict(callback.get('message',{}), **{'from':callback['from']}) if callback else {})
                if callback:
                    api.call('answerCallbackQuery', callback_query_id=callback['id'], text='Open the updated app')
                if message.get('chat',{}).get('type') == 'private' and message.get('text','').split('@')[0] == '/id':
                    api.call('sendMessage', chat_id=message['chat']['id'], text=f"Your Telegram ID: {message['from']['id']}")
                elif (message.get('chat',{}).get('type') == 'private' and message.get('text','').split('@')[0] == '/install'
                      and gate.authorized(message) and store.get('owner') == str(message['from']['id'])):
                    code = issue_pairing_code(store.db, message['from']['id'])
                    api.call('sendMessage', chat_id=message['chat']['id'], text=(
                        'Pearly для iPhone\n\n'
                        f'1. Откройте в Safari: {url}\n'
                        '2. Поделиться → На экран «Домой» → Добавить.\n'
                        '3. Откройте новую иконку и вставьте код:\n\n'
                        f'{code}\n\n'
                        '4. Придумайте свой PIN из четырёх цифр. Далее приложение будет открываться по нему.\n\n'
                        'Код из Telegram действует 15 минут и только один раз. Если срок истёк, отправьте /install ещё раз.'
                    ))
                elif (message.get('chat',{}).get('type') == 'private' and message.get('from',{}).get('id') in extra) or gate.authorized(message):
                    api.call('sendMessage', chat_id=message['chat']['id'], text='Pearly • More space. You at the center.', reply_markup={'inline_keyboard':[[{'text':'Open','web_app':{'url':url}}]]})
                with store.db:
                    store.put('offset', update['update_id']+1)
        except APIError as exc:
            if exc.code in (401,409):
                raise SystemExit('Check token / only one running bot instance')
            time.sleep(min(max(exc.retry_after,2),30))

if __name__ == '__main__':
    main()
