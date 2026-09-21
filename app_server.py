"""Private Telegram Mini App; reuse the existing Telegram client and owner binding."""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

from bot import Telegram, APIError, Store, Bot, load_env

ROOT = Path(__file__).resolve().parent
CATEGORIES = ('bed','bath','hall','kitchen','balcony','sale','home','finance','shopping','documents')

def initialize(path):
    # Same database and owner as previous releases. Reset only once, atomically.
    old = Store(path)
    with old.db:
        old.db.execute('CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, category TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)')
        old.db.execute('CREATE TABLE IF NOT EXISTS app_requests(id TEXT PRIMARY KEY)')
        if old.get('pearl_v1_reset') != '1':
            old.db.execute('DELETE FROM tasks')
            old.db.execute('DELETE FROM draft')
            old.db.execute('DELETE FROM steps')
            old.db.execute("DELETE FROM meta WHERE key NOT IN ('owner','language','offset')")
            old.put('pearl_v1_reset', '1')
    old.db.close()

def authenticate(raw, token, owner, now=None):
    pairs = parse_qsl(raw, keep_blank_values=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('Некорректная авторизация')
    supplied = values.pop('hash', '')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{key}={value}' for key, value in sorted(values.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError('Открой приложение через кнопку в Telegram')
    age = (time.time() if now is None else now) - int(values.get('auth_date', '0'))
    if not -30 <= age <= 86400:
        raise ValueError('Закрой и снова открой приложение в Telegram')
    user = json.loads(values.get('user', '{}')).get('id')
    if not owner or str(user) != str(owner):
        raise ValueError('Это личный бот')
    return user

def mutate(db, data):
    op = data.get('op')
    if op == 'state':
        return
    request_id = data.get('request_id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 80:
        raise ValueError('Некорректный запрос')
    if db.execute('SELECT 1 FROM app_requests WHERE id=?', (request_id,)).fetchone():
        return
    if op in ('create', 'edit'):
        title = data.get('title', '')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 40:
            raise ValueError('Название должно содержать от 1 до 40 символов')
        if data.get('category') not in CATEGORIES:
            raise ValueError('Выбери категорию')
        if op == 'create':
            db.execute('INSERT INTO steps(title,category) VALUES(?,?)', (title.strip(), data['category']))
        else:
            cur = db.execute('UPDATE steps SET title=?,category=? WHERE id=?', (title.strip(), data['category'], data.get('id')))
            if not cur.rowcount:
                raise ValueError('Задача уже удалена. Открой список заново')
    elif op in ('complete','restore'):
        cur = db.execute('UPDATE steps SET done=? WHERE id=?', (int(op=='complete'), data.get('id')))
        if not cur.rowcount:
            raise ValueError('Задача уже удалена')
    elif op == 'clear':
        db.execute('DELETE FROM steps WHERE done=1')
    else:
        raise ValueError('Неизвестное действие')
    db.execute('INSERT INTO app_requests VALUES(?)', (request_id,))

def make_handler(path, token, allowed):
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
                    raise ValueError('Слишком большой запрос')
                data = json.loads(self.rfile.read(length))
                with sqlite3.connect(path, timeout=15) as db:
                    db.row_factory = sqlite3.Row
                    owner = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
                    authenticate(self.headers.get('X-Telegram-Init-Data',''), token, owner[0] if owner else allowed)
                    db.execute('BEGIN IMMEDIATE')
                    mutate(db, data)
                    result = [dict(row) for row in db.execute('SELECT * FROM steps ORDER BY id')]
                self.reply(200, {'tasks':result})
            except (ValueError, TypeError, KeyError) as exc:
                self.reply(400, {'error':str(exc)})
            except Exception:
                self.reply(500, {'error':'Не удалось сохранить. Попробуй ещё раз.'})
    return Handler

def main():
    load_env()
    token = os.environ.get('BOT_TOKEN', '').strip()
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0') or '0')
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
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), make_handler(path, token, allowed))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api.call('setMyCommands', commands=[{'command':'start','description':'Открыть Zero Space'}])
    api.call('setChatMenuButton', menu_button={'type':'web_app','text':'Открыть','web_app':{'url':url}})
    print('Zero Space v1 ready', flush=True)
    while True:
        try:
            updates = api.call('getUpdates', offset=int(store.get('offset','0')), timeout=30, allowed_updates=['message','callback_query'])
            for update in updates:
                callback = update.get('callback_query')
                message = update.get('message') or (dict(callback.get('message',{}), **{'from':callback['from']}) if callback else {})
                if callback:
                    api.call('answerCallbackQuery', callback_query_id=callback['id'], text='Открой обновлённое приложение')
                if gate.authorized(message):
                    api.call('sendMessage', chat_id=message['chat']['id'], text='Zero Space · Маленькие шаги. Больше спокойствия.', reply_markup={'inline_keyboard':[[{'text':'Открыть','web_app':{'url':url}}]]})
                with store.db:
                    store.put('offset', update['update_id']+1)
        except APIError as exc:
            if exc.code in (401,409):
                raise SystemExit('Check token / only one running bot instance')
            time.sleep(min(max(exc.retry_after,2),30))

if __name__ == '__main__':
    main()
