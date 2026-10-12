import hashlib
import hmac
import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from app_server import (
    SESSION_COOKIE,
    browser_session,
    initialize,
    issue_pairing_code,
    make_handler,
    redeem_pairing_code,
    session_user,
)
from bot import Store


class PwaAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tasks.sqlite3'
        store = Store(self.path)
        store.bind(42)
        store.db.close()
        initialize(self.path)
        self.db = sqlite3.connect(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_pairing_is_one_time_and_session_expires(self):
        code = issue_pairing_code(self.db, 42, now=1000)
        with self.db:
            token = redeem_pairing_code(self.db, code, [42], now=1001)
        cookie = f'{SESSION_COOKIE}={token}'
        self.assertEqual(session_user(self.db, cookie, [42], now=1002), 42)
        self.assertIsNone(session_user(self.db, cookie, [43], now=1002))
        self.assertIsNone(session_user(self.db, cookie, [42], now=1001 + 180 * 24 * 60 * 60))
        with self.assertRaises(ValueError):
            with self.db:
                redeem_pairing_code(self.db, code, [42], now=1002)

    def test_existing_sessions_gain_pin_columns_without_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'older.sqlite3'
            store = Store(path)
            store.bind(42)
            with store.db:
                store.db.execute('CREATE TABLE web_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at INTEGER NOT NULL)')
                store.db.execute('INSERT INTO web_sessions VALUES(?,?,?)', ('prior-session', 42, 9_999_999_999))
            store.db.close()
            initialize(path)
            with sqlite3.connect(path) as db:
                row = db.execute('SELECT token_hash,pin_hash,pin_failures FROM web_sessions').fetchone()
                self.assertEqual(row, ('prior-session', None, 0))
            initialize(path)

    def test_browser_login_and_origin_protection(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(self.path, 'test-token', 42, extra=(43,), app_url=origin))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(route, data=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                body = None if data is None else json.dumps(data)
                connection.request('GET' if data is None else 'POST', route, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        try:
            status, _, body = request('/manifest.webmanifest')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['display'], 'standalone')

            json_headers = {'Content-Type': 'application/json', 'Origin': origin}
            status, _, _ = request('/api', {'op': 'state'}, json_headers)
            self.assertEqual(status, 401)

            telegram_data = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': 42})}
            secret = hmac.new(b'WebAppData', b'test-token', hashlib.sha256).digest()
            check = '\n'.join(f'{key}={value}' for key, value in sorted(telegram_data.items()))
            telegram_data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            status, _, _ = request('/api', {'op': 'state'}, {
                'Content-Type': 'application/json', 'X-Telegram-Init-Data': urlencode(telegram_data),
            })
            self.assertEqual(status, 200)
            telegram_data['user'] = json.dumps({'id': 43})
            check = '\n'.join(f'{key}={value}' for key, value in sorted(telegram_data.items()) if key != 'hash')
            telegram_data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            status, _, _ = request('/api', {'op': 'state'}, {
                'Content-Type': 'application/json', 'X-Telegram-Init-Data': urlencode(telegram_data),
            })
            self.assertEqual(status, 200)

            code = issue_pairing_code(self.db, 42)
            status, _, _ = request('/auth/redeem', {'code': code}, {'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
            self.assertEqual(status, 403)

            status, headers, body = request('/auth/redeem', {'code': code}, json_headers)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['needs_pin'])
            cookie = headers['Set-Cookie']
            self.assertIn('Secure', cookie)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.assertTrue(cookie.startswith(SESSION_COOKIE + '='))

            with_cookie = dict(json_headers, Cookie=cookie.split(';', 1)[0])
            status, _, body = request('/api', {'op': 'state'}, with_cookie)
            self.assertEqual(status, 428)
            self.assertEqual(json.loads(body)['auth'], 'setup')
            status, _, _ = request('/auth/setup-pin', {'pin': '0042'}, dict(with_cookie, Origin='https://evil.example'))
            self.assertEqual(status, 403)
            status, _, _ = request('/auth/setup-pin', {'pin': '１２３４'}, with_cookie)
            self.assertEqual(status, 400)
            status, _, body = request('/auth/setup-pin', {'pin': '0042'}, with_cookie)
            self.assertEqual(status, 200)
            unlock_token = json.loads(body)['unlock_token']
            self.assertGreaterEqual(len(unlock_token), 40)
            session = browser_session(self.db, with_cookie['Cookie'], [42])
            self.assertNotEqual(session['pin_hash'], '0042')
            self.assertEqual(len(session['pin_salt']), 32)
            status, _, _ = request('/auth/setup-pin', {'pin': '0042'}, with_cookie)
            self.assertEqual(status, 409)
            status, _, body = request('/api', {'op': 'state'}, with_cookie)
            self.assertEqual(status, 423)
            self.assertEqual(json.loads(body)['auth'], 'pin')
            with_unlock = dict(with_cookie, **{'X-Pearly-Unlock':unlock_token})
            status, _, body = request('/api', {'op': 'create', 'title': 'PWA task', 'category': 'bed', 'request_id': 'one'}, with_unlock)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['tasks'][0]['title'], 'PWA task')
            status, _, _ = request('/api', {'op': 'state'}, dict(with_unlock, Origin='https://evil.example'))
            self.assertEqual(status, 403)
            for attempt in range(4):
                status, _, body = request('/auth/unlock', {'pin': '9999'}, with_cookie)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)['remaining_attempts'], 4 - attempt)
            status, headers, body = request('/auth/unlock', {'pin': '9999'}, with_cookie)
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)['auth'], 'pin')
            self.assertEqual(headers['Retry-After'], '300')
            status, _, _ = request('/auth/unlock', {'pin': '0042'}, with_cookie)
            self.assertEqual(status, 429)
            with self.db:
                self.db.execute('UPDATE web_sessions SET pin_locked_until=?', (int(time.time()) - 1,))
            status, _, body = request('/auth/unlock', {'pin': '0042'}, with_cookie)
            self.assertEqual(status, 200)
            next_unlock_token = json.loads(body)['unlock_token']
            self.assertNotEqual(unlock_token, next_unlock_token)
            status, _, _ = request('/api', {'op': 'state'}, with_unlock)
            self.assertEqual(status, 423)
            status, _, body = request('/api', {'op': 'state'}, dict(with_cookie, **{'X-Pearly-Unlock':next_unlock_token}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['tasks'][0]['title'], 'PWA task')
            with self.db:
                self.db.execute('UPDATE web_sessions SET unlock_expires_at=0')
            status, _, _ = request('/api', {'op': 'state'}, dict(with_cookie, **{'X-Pearly-Unlock':next_unlock_token}))
            self.assertEqual(status, 423)
            status, _, _ = request('/auth/redeem', {'code': code}, json_headers)
            self.assertEqual(status, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
