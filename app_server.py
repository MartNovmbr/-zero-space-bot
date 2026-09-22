"""Private Telegram Mini App; reuse the existing Telegram client and owner binding."""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

from bot import Telegram, APIError, Store, Bot, load_env

ROOT = Path(__file__).resolve().parent
CATEGORIES = ('bed','bath','hall','kitchen','balcony','sale','home','finance','shopping','documents')
DEFAULT_NAMES = ('Bedroom','Bathroom','Entryway','Kitchen','Balcony','Selling','Utilities','Finances','Shopping','Documents')
DEFAULT_ICONS = ('bed-double','bath','coat','cooking-pot','panels-top-left','coins','house','credit-card','shopping-cart','book-user')

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
        ensure_categories(old.db)
    old.db.close()

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

def make_handler(path, token, allowed, extra=()):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log auth data, token or task titles.

        def reply(self, status, body, kind='application/json; charset=utf-8'):
            body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/health':
                return self.reply(200, {'ok':True})
            if self.path in ('/', '/app'):
                return self.reply(200, (ROOT/'app.html').read_bytes(), 'text/html; charset=utf-8')
            self.reply(404, {'error':'Not found'})

        def do_POST(self):
            if self.path != '/api':
                return self.reply(404, {'error':'Not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Request too large')
                data = json.loads(self.rfile.read(length))
                with sqlite3.connect(path, timeout=15) as db:
                    owner = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
                    owner_id = owner[0] if owner else allowed
                    user = authenticate(self.headers.get('X-Telegram-Init-Data',''), token, [owner_id, *extra])
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
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), make_handler(path, token, allowed, extra))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api.call('setMyCommands', commands=[{'command':'start','description':'Open Pearly'},{'command':'id','description':'Show my Telegram ID'}])
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
