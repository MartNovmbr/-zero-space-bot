"""Zero Space: a private Telegram bot for cleaning tasks and a cozy story."""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import time
import urllib.error
import urllib.request

from media import CHAOS_TOTAL, compose_room, render_transition

ROOT = Path(__file__).resolve().parent
LEVELS = {1: 'Совсем легко / Easy · 1 ✨', 3: 'Легко / Easy · 3 ✨',
          7: 'Средне / Medium · 7 ✨', 12: 'Сложно / Hard · 12 ✨'}
UPGRADES = [
    ('Первый уют', 'Ковёр и растение', 18),
    ('Больше света', 'Окно и льняные шторы', 53),
    ('Кабинет писателя', 'Обои, красивая дверь, кресло и письменный стол с книгами', 95),
]
IMAGES = ['00-empty.png', '01-cozy.png', '02-window.png', '03-finished.png']
CLEAR_AT = [18, 71, 166]
AFTER_MESSAGES = [
    'Chaos fades.', 'A little more light.', 'The room can breathe again.',
    'The chaos thins. The story continues.', 'A cleaner room, a brighter chapter.'
]


def load_env(path=ROOT / '.env'):
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class APIError(Exception):
    def __init__(self, code=0, retry_after=5):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram API error {code}')


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **params):
        req = urllib.request.Request(self.base + method,
            data=json.dumps(params, ensure_ascii=False).encode(),
            headers={'Content-Type': 'application/json'})
        return self._send(req)

    def _send(self, req):
        try:
            with urllib.request.urlopen(req, timeout=75) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read())
                retry = detail.get('parameters', {}).get('retry_after', 5)
            except (ValueError, OSError):
                retry = 5
            raise APIError(exc.code, retry) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Never include the request URL: it contains the bot's secret token.
            raise APIError(0) from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0))
        return result['result']

    def _media(self, method, field, chat, path, caption='', keyboard=None, mime='application/octet-stream'):
        boundary = 'Domik' + secrets.token_hex(12)
        chunks = []
        fields = {'chat_id': str(chat)}
        if caption:
            fields['caption'] = caption
        if keyboard is not None:
            fields['reply_markup'] = json.dumps({'inline_keyboard': keyboard}, ensure_ascii=False)
        for key, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        chunks.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
        req = urllib.request.Request(self.base + method, data=b''.join(chunks),
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        return self._send(req)

    def photo(self, chat, path, caption, keyboard):
        return self._media('sendPhoto', 'photo', chat, path, caption, keyboard, 'image/jpeg')

    def animation(self, chat, path):
        return self._media('sendAnimation', 'animation', chat, path, mime='image/gif')


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
              difficulty INTEGER NOT NULL CHECK(difficulty IN(1,3,7,12)),
              status TEXT NOT NULL DEFAULT 'active', source TEXT UNIQUE NOT NULL,
              created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed TEXT);
            CREATE TABLE IF NOT EXISTS draft(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              nonce TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('balance','0');
            INSERT OR IGNORE INTO meta VALUES('stage','0');
            INSERT OR IGNORE INTO meta VALUES('offset','0');
        ''')
        earned = self.db.execute("SELECT COALESCE(sum(difficulty),0) FROM tasks WHERE status='done'").fetchone()[0]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('total_earned',?)", (str(earned),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('chaos_remaining',?)", (str(max(0, CHAOS_TOTAL-earned)),))

    def get(self, key, default=''):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def put(self, key, value):
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def bind(self, user):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('owner', str(user)))
        return self.get('owner') == str(user)

    def begin_draft(self, title='', kind='title'):
        nonce = secrets.token_hex(5)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO draft VALUES(1,?,?,?)', (nonce, title, kind))
        return nonce

    def draft(self):
        return self.db.execute('SELECT * FROM draft WHERE singleton=1').fetchone()

    def cancel(self):
        with self.db:
            self.db.execute('DELETE FROM draft')

    def create_task(self, nonce, difficulty):
        if difficulty not in LEVELS:
            return None
        with self.db:
            row = self.db.execute("SELECT * FROM draft WHERE nonce=? AND kind='difficulty'", (nonce,)).fetchone()
            if not row:
                return None
            cur = self.db.execute('INSERT OR IGNORE INTO tasks(title,difficulty,source) VALUES(?,?,?)', (row['title'], difficulty, nonce))
            self.db.execute('DELETE FROM draft')
            return cur.lastrowid

    def task(self, task_id):
        return self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()

    def finish(self, task_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.task(task_id)
            if not row or row['status'] != 'active':
                return 0
            self.db.execute("UPDATE tasks SET status='done',completed=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            self.put('balance', int(self.get('balance')) + row['difficulty'])
            self.put('total_earned', int(self.get('total_earned')) + row['difficulty'])
            self.put('chaos_remaining', max(0, int(self.get('chaos_remaining')) - row['difficulty']))
            return row['difficulty']

    def change_difficulty(self, task_id, difficulty):
        if difficulty not in LEVELS:
            return False
        with self.db:
            return self.db.execute("UPDATE tasks SET difficulty=? WHERE id=? AND status='active'", (difficulty, task_id)).rowcount == 1

    def rename(self, task_id, title):
        with self.db:
            changed = self.db.execute("UPDATE tasks SET title=? WHERE id=? AND status='active'", (title, task_id)).rowcount
            self.db.execute('DELETE FROM draft')
            return bool(changed)

    def archive(self, task_id):
        with self.db:
            return self.db.execute("UPDATE tasks SET status='archived' WHERE id=? AND status='active'", (task_id,)).rowcount == 1

    def buy(self, stage):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if stage != int(self.get('stage')) or stage not in range(len(UPGRADES)):
                return 'stale'
            if int(self.get('total_earned')) < CLEAR_AT[stage]:
                return 'locked'
            price = UPGRADES[stage][2]
            balance = int(self.get('balance'))
            if balance < price:
                return 'poor'
            self.put('balance', balance-price)
            self.put('stage', stage+1)
            return 'ok'


def btn(text, data):
    return {'text': text, 'callback_data': data}


MENU = [[btn('🦝 Комната', 'room'), btn('✍️ Добавить дело', 'add')],
        [btn('☑️ Мои дела', 'list:0'), btn('🪴 Обустроить', 'shop')],
        [btn('📖 О дивный чистый мир', 'history:0')]]


class Bot:
    def __init__(self, api, store, pairing_code='', allowed_user=0):
        self.api, self.store = api, store
        self.pairing_code, self.allowed_user = pairing_code, allowed_user

    def send(self, chat, text, keyboard=None):
        return self.api.call('sendMessage', chat_id=chat, text=text,
            reply_markup={'inline_keyboard': MENU if keyboard is None else keyboard})

    def authorized(self, message):
        if message.get('chat', {}).get('type') != 'private':
            return False
        user = message.get('from', {}).get('id')
        if not user or message.get('chat', {}).get('id') != user:
            return False
        owner = self.store.get('owner')
        if owner:
            return owner == str(user)
        if self.allowed_user:
            return user == self.allowed_user and self.store.bind(user)
        pieces = message.get('text', '').split(maxsplit=1)
        provided = pieces[1] if len(pieces) == 2 and pieces[0].split('@')[0] == '/start' else ''
        if self.pairing_code and provided and hmac.compare_digest(provided, self.pairing_code):
            return self.store.bind(user)
        return False

    def room(self, chat):
        stage, balance = int(self.store.get('stage')), int(self.store.get('balance'))
        chaos = int(self.store.get('chaos_remaining'))
        cleared = CHAOS_TOTAL-chaos
        count = self.store.db.execute("SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0]
        text = (f'ZERO SPACE\nClean. Create. Continue the story.\n\n🦝 Комната енота-писателя Реда\n\n✨ Искры: {balance}\n'
                f'Наград в твоей истории: {count}\nОбустройство: {stage}/3\n\n')
        text += f'🌫 Хаос рассеян на {round(cleared/CHAOS_TOTAL*100)}% · осталось {chaos}/{CHAOS_TOTAL}\n\n'
        text += ('Теперь у Реда есть свой писательский уголок. Новая глава начинается с тебя 🤍' if stage == 3 else
                 'Даже одно маленькое дело — новая строчка в нашей истории. Ред рад тебе и после перерыва.')
        cache = Path(os.environ.get('MEDIA_CACHE', str(ROOT/'data'/'media_cache')))
        image_path = cache/f'room-{stage}-{chaos}.jpg'
        if not image_path.exists():
            compose_room(stage, chaos, image_path)
        file_key = f'photo:{stage}:{chaos}'
        file_id = self.store.get(file_key)
        result = None
        if file_id:
            try:
                result = self.api.call('sendPhoto', chat_id=chat, photo=file_id,
                    caption=text, reply_markup={'inline_keyboard': MENU})
            except APIError as exc:
                if exc.code != 400:
                    raise
        if result is None:
            result = self.api.photo(chat, image_path, text, MENU)
        if result.get('photo'):
            with self.store.db:
                self.store.put(file_key, result['photo'][-1]['file_id'])

    def show_tasks(self, chat, page=0, history=False):
        page = max(0, page)
        status = 'done' if history else 'active'
        total = self.store.db.execute('SELECT count(*) FROM tasks WHERE status=?', (status,)).fetchone()[0]
        page = min(page, max(0, (total-1)//8))
        order = 'completed DESC, id DESC' if history else 'id DESC'
        rows = self.store.db.execute(f'SELECT * FROM tasks WHERE status=? ORDER BY {order} LIMIT 8 OFFSET ?', (status,page*8)).fetchall()
        if not rows:
            self.send(chat, '📖 О дивный чистый мир\n\nЗдесь Ред запишет твои награды: сколько искр ты получила и за какое дело. Первая запись появится после выполненной задачи 🤍' if history else 'Пока нет дел. Можно записать первое — даже очень маленькое.')
            return
        keyboard = [[btn((f'✨ +{r["difficulty"]} · {r["title"][:40]}' if history else f'{r["title"][:40]} · {r["difficulty"]} ✨'), f'task:{r["id"]}')] for r in rows]
        navigation = []
        prefix = 'history' if history else 'list'
        if page:
            navigation.append(btn('← Назад', f'{prefix}:{page-1}'))
        if (page+1)*8 < total:
            navigation.append(btn('Далее →', f'{prefix}:{page+1}'))
        if navigation:
            keyboard.append(navigation)
        keyboard += [[btn('✍️ Добавить дело', 'add'), btn('🦝 Комната', 'room')]]
        if history:
            earned = self.store.db.execute("SELECT COALESCE(sum(difficulty),0) FROM tasks WHERE status='done'").fetchone()[0]
            text = f'📖 О дивный чистый мир\n\nТвоя история наград · {total} записей\nВсего заработано: {earned} ✨\n\nВыбери награду, чтобы вспомнить, за какое дело она получена. Потраченные искры остаются в истории.'
        else:
            text = f'☑️ Мои дела · {total}\nВыбери задачу. Сложность — по твоим ощущениям.'
        self.send(chat, text, keyboard)

    def show_task(self, chat, task_id):
        row = self.store.task(task_id)
        if not row or row['status'] == 'archived':
            self.send(chat, 'Задача уже убрана из списка.')
            return
        if row['status'] == 'done':
            self.send(chat, f'📖 О дивный чистый мир\n\nНаграда: +{row["difficulty"]} ✨\nЗа дело: {row["title"]}\n\nРед бережно сохранил эту маленькую победу.', [[btn('← История наград', 'history:0'), btn('🦝 Комната', 'room')]])
            return
        self.send(chat, f'{row["title"]}\n\nСложность: {LEVELS[row["difficulty"]]}', [
            [btn('Готово ✓', f'done:{task_id}')],
            [btn('Изменить сложность', f'level:{task_id}'), btn('Переименовать', f'rename:{task_id}')],
            [btn('Убрать из списка', f'archiveask:{task_id}'), btn('← Мои дела', 'list:0')]])

    def difficulty(self, chat, prefix, text):
        self.send(chat, text, [[btn(label, f'{prefix}:{value}')] for value,label in LEVELS.items()] + [[btn('Отмена', 'cancel')]])

    def shop(self, chat):
        stage, balance = int(self.store.get('stage')), int(self.store.get('balance'))
        earned = int(self.store.get('total_earned'))
        if stage == 3:
            self.send(chat, 'Комната полностью обустроена 🤍\nВсе покупки останутся с тобой. Искры за новые дела продолжат копиться.')
            return
        title, contents, price = UPGRADES[stage]
        unlocked = earned >= CLEAR_AT[stage]
        text = f'🪴 {title}\n{contents}\n\nЦена: {price} ✨\nУ тебя: {balance} ✨\n'
        text += (f'Участок комнаты очищен — место свободно.\n\n' if unlocked else
                 f'Сначала рассей хаос: заработано {earned}/{CLEAR_AT[stage]} искр за реальные дела.\nОсталось: {CLEAR_AT[stage]-earned} ✨\n\n')
        text += 'После покупки картинка комнаты обновится.'
        keyboard = [[btn(f'Купить · {price} ✨', f'buy:{stage}')]] if unlocked else []
        keyboard += [[btn('☑️ Мои дела','list:0'),btn('🦝 Комната','room')]]
        self.send(chat, text, keyboard)

    def handle(self, update):
        callback = update.get('callback_query')
        if callback:
            message = dict(callback.get('message') or {})
            message['from'] = callback.get('from', {})
        else:
            message = update.get('message') or {}
        if not message or not self.authorized(message):
            if callback:
                self.api.call('answerCallbackQuery', callback_query_id=callback['id'], text='Это личный бот.')
            elif message.get('chat', {}).get('type') == 'private':
                self.send(message['chat']['id'], 'Это личный бот. Для первого входа используй свою ссылку привязки.', [])
            return
        chat = message['chat']['id']
        if callback:
            try:
                self.api.call('answerCallbackQuery', callback_query_id=callback['id'])
            except APIError as exc:
                if exc.code != 400:
                    raise
            return self.on_callback(chat, callback.get('data', ''))
        text = message.get('text', '').strip()
        if not text:
            self.send(chat, 'Пока я принимаю задачи текстом. Напиши, что хочешь сделать.')
            return
        command = text.split()[0].split('@')[0]
        if command in ('/start','/room'):
            self.store.cancel()
            self.room(chat)
        elif command == '/add':
            self.add(chat)
        elif command == '/tasks':
            self.show_tasks(chat)
        elif command == '/shop':
            self.shop(chat)
        elif command == '/cancel':
            self.store.cancel()
            self.send(chat, 'Отменено. Можно начать заново в любое время.')
        elif command == '/help':
            self.send(chat, 'Напиши задачу → выбери сложность → отметь выполнение → получи искры.\n\n'
                '1 / 3 / 7 / 12 искр — по твоим ощущениям. Время не измеряется.\n'
                'В активной задаче можно изменить название и сложность.\n'
                'Повторное нажатие «Готово» не начисляет искры ещё раз.\n'
                'Нет штрафов и обязательных ежедневных серий.\n\n'
                '/room — комната\n/add — новое дело\n/tasks — мои дела\n/shop — покупки\n/cancel — отменить ввод')
        elif command.startswith('/'):
            self.send(chat, 'Не знаю эту команду. Нажми кнопку ниже или /help.')
        else:
            if len(text) > 250:
                self.send(chat, 'Сократи название до 250 символов — так оно поместится в карточку.')
                return
            draft = self.store.draft()
            if draft and draft['kind'].startswith('rename:'):
                changed = self.store.rename(int(draft['kind'].split(':')[1]), text)
                self.send(chat, 'Название обновлено.' if changed else 'Эта задача уже закрыта.')
            else:
                nonce = self.store.begin_draft(text, 'difficulty')
                self.difficulty(chat, f'new:{nonce}', f'«{text}»\n\nНасколько это сложно для тебя?')

    def add(self, chat):
        self.store.begin_draft()
        self.send(chat, 'Какое дело хочешь записать?\nНапиши одним сообщением. Например: «Разобрать одну полку».', [[btn('Отмена','cancel')]])

    def on_callback(self, chat, data):
        parts = data.split(':')
        action = parts[0]
        if data == 'room':
            self.room(chat)
        elif data == 'add':
            self.add(chat)
        elif data == 'cancel':
            self.store.cancel()
            self.send(chat, 'Отменено. Ред никуда не торопится 🤍')
        elif data == 'shop':
            self.shop(chat)
        elif len(parts) == 3 and action == 'new' and parts[2].isdigit():
            task_id = self.store.create_task(parts[1], int(parts[2]))
            if task_id:
                self.show_task(chat, task_id)
            else:
                self.send(chat, 'Эта карточка уже обработана или заменена. Открой «Мои дела».')
        elif len(parts) >= 2 and parts[1].isdigit():
            number = int(parts[1])
            if action in ('list','history'):
                self.show_tasks(chat, number, action == 'history')
            elif action == 'task':
                self.show_task(chat, number)
            elif action == 'done':
                before = int(self.store.get('chaos_remaining'))
                reward = self.store.finish(number)
                if reward:
                    after = int(self.store.get('chaos_remaining'))
                    cache = Path(os.environ.get('MEDIA_CACHE', str(ROOT/'data'/'media_cache')))
                    animation = cache/f'chaos-{int(self.store.get("stage"))}-{before}-{after}.gif'
                    try:
                        if not animation.exists():
                            render_transition(int(self.store.get('stage')), before, after, reward, animation)
                        self.api.animation(chat, animation)
                    except (APIError, OSError, ValueError) as exc:
                        logging.warning('Не удалось отправить анимацию хаоса: %s', type(exc).__name__)
                    phrase = AFTER_MESSAGES[number % len(AFTER_MESSAGES)]
                    cleared_before, cleared_after = CHAOS_TOTAL-before, CHAOS_TOTAL-after
                    unlocked = [i for i,t in enumerate(CLEAR_AT) if cleared_before < t <= cleared_after]
                    extra = ''
                    if unlocked:
                        title = UPGRADES[unlocked[-1]][0]
                        extra = f'\n\n🌟 Участок хаоса исчез полностью. Открыто улучшение «{title}».'
                    self.send(chat, f'{phrase}\n\n✨ +{reward} искр! Хаос уменьшился на {reward}.\nРед записал награду в «О дивный чистый мир» / “O Brave Clean World”.\nБаланс: {self.store.get("balance")} ✨{extra}\n\nМожно закончить на сегодня.')
                else:
                    self.send(chat, 'Эта задача уже закрыта. Награда повторно не списывается и не начисляется.')
            elif action == 'level':
                row = self.store.task(number)
                if row and row['status'] == 'active':
                    self.difficulty(chat, f'edit:{number}', 'Выбери новую сложность. Награда изменится до выполнения.')
                else:
                    self.send(chat, 'Сложность закрытой задачи уже не меняется.')
            elif action == 'edit' and len(parts) == 3 and parts[2].isdigit():
                self.store.change_difficulty(number, int(parts[2]))
                self.show_task(chat, number)
            elif action == 'rename':
                row = self.store.task(number)
                if row and row['status'] == 'active':
                    self.store.begin_draft(kind=f'rename:{number}')
                    self.send(chat, 'Напиши новое название задачи.', [[btn('Отмена','cancel')]])
                else:
                    self.send(chat, 'Эта задача уже закрыта.')
            elif action == 'archiveask':
                self.send(chat, 'Убрать задачу из активных? Искры не изменятся.', [[btn('Да, убрать',f'archive:{number}'),btn('Оставить',f'task:{number}')]])
            elif action == 'archive':
                self.store.archive(number)
                self.show_tasks(chat)
            elif action == 'buy':
                result = self.store.buy(number)
                if result == 'ok':
                    self.room(chat)
                elif result == 'poor':
                    self.send(chat, 'Пока не хватает искр. Ничего не списано — можно вернуться позже.')
                elif result == 'locked':
                    self.send(chat, 'Это место ещё скрыто хаосом. Выполни несколько реальных дел — искры постепенно освободят его.')
                else:
                    self.send(chat, 'Этот этап уже куплен или ещё не открыт.')
            else:
                self.send(chat, 'Открой актуальное меню ниже.')
        else:
            self.send(chat, 'Открой актуальное меню ниже.')


def main():
    load_env()
    token = os.environ.get('BOT_TOKEN', '').strip()
    code = os.environ.get('PAIRING_CODE', '').strip()
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0') or '0')
    db_path = Path(os.environ.get('DATABASE_PATH', str(ROOT/'data'/'domik.sqlite3')))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    if not token or (not code and not allowed and not store.get('owner')):
        raise SystemExit('Сначала запусти: python setup.py')
    api = Telegram(token)
    bot = Bot(api, store, code, allowed)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        api.call('getMe')
        webhook = api.call('getWebhookInfo')
        if webhook.get('url'):
            raise SystemExit('У бота уже настроен webhook. Используй нового бота или отключи webhook осознанно перед запуском.')
        api.call('setMyCommands', commands=[{'command':c,'description':d} for c,d in [
            ('start','Моя комната'),('add','Добавить дело'),('tasks','Мои дела'),
            ('shop','Обустроить комнату'),('cancel','Отменить ввод'),('help','Как всё работает')]])
        logging.info('Zero Space запущен. Для остановки нажми Ctrl+C.')
        while running:
            try:
                updates = api.call('getUpdates', offset=int(store.get('offset')),
                                   timeout=30, allowed_updates=['message','callback_query'])
                for update in updates:
                    bot.handle(update)
                    with store.db:
                        store.put('offset', update['update_id']+1)
            except APIError as exc:
                logging.warning('Сбой связи с Telegram (код %s). Повтор подключения.', exc.code)
                if exc.code in (401,409):
                    raise SystemExit('Проверь токен и убедись, что работает только один экземпляр бота.')
                time.sleep(min(max(exc.retry_after, 2), 60))
    except APIError as exc:
        raise SystemExit(f'Не удалось подключиться к Telegram (код {exc.code}). Проверь интернет и токен.') from None
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
