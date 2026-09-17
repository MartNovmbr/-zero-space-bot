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

from media import CHAOS_TOTAL, compose_room

ROOT = Path(__file__).resolve().parent
LEVELS = {1: 'Совсем легко · 1 ✨', 3: 'Легко · 3 ✨',
          7: 'Средне · 7 ✨', 12: 'Сложно · 12 ✨'}
LEVELS_EN = {1: 'Very easy · 1 ✨', 3: 'Easy · 3 ✨',
             7: 'Medium · 7 ✨', 12: 'Hard · 12 ✨'}
UPGRADES = [
    ('Первый уют', 'Ковёр и растение', 18),
    ('Больше света', 'Окно и льняные шторы', 53),
    ('Кабинет писателя', 'Обои, красивая дверь, кресло и письменный стол с книгами', 95),
]
UPGRADES_EN = [
    ('First comfort', 'A rug and a plant', 18),
    ('More light', 'A window and linen curtains', 53),
    ("The writer's study", 'Wallpaper, a beautiful door, an armchair, a writing desk and books', 95),
]
IMAGES = ['00-empty.png', '01-cozy.png', '02-window.png', '03-finished.png']
CLEAR_AT = [18, 71, 166]
AFTER_MESSAGES = [
    'Chaos fades.', 'A little more light.', 'The room can breathe again.',
    'The chaos thins. The story continues.', 'A cleaner room, a brighter chapter.'
]
AFTER_MESSAGES_RU = [
    'Хаос отступает.', 'Ещё немного света.', 'Комната снова может дышать.',
    'Хаос редеет. История продолжается.', 'Чище комната — светлее новая глава.'
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
            INSERT OR IGNORE INTO meta VALUES('undo_debt','0');
            INSERT OR IGNORE INTO meta VALUES('language','ru');
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
            reward = row['difficulty']
            debt = int(self.get('undo_debt', '0'))
            debt_paid = min(debt, reward)
            self.put('undo_debt', debt-debt_paid)
            self.put('balance', int(self.get('balance')) + reward-debt_paid)
            self.put('total_earned', int(self.get('total_earned')) + row['difficulty'])
            self.put('chaos_remaining', max(0, int(self.get('chaos_remaining')) - row['difficulty']))
            return row['difficulty']

    def undo(self, task_id):
        """Return a completed task without rolling back purchased room stages."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.task(task_id)
            if not row or row['status'] != 'done':
                return 0
            reward = row['difficulty']
            balance = int(self.get('balance'))
            charged = min(balance, reward)
            self.db.execute("UPDATE tasks SET status='active',completed=NULL WHERE id=?", (task_id,))
            self.put('balance', balance-charged)
            self.put('undo_debt', int(self.get('undo_debt', '0')) + reward-charged)
            self.put('total_earned', max(0, int(self.get('total_earned'))-reward))
            self.put('chaos_remaining', min(CHAOS_TOTAL, int(self.get('chaos_remaining'))+reward))
            return reward

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


class Bot:
    def __init__(self, api, store, pairing_code='', allowed_user=0):
        self.api, self.store = api, store
        self.pairing_code, self.allowed_user = pairing_code, allowed_user

    @property
    def lang(self):
        return self.store.get('language', 'ru') if self.store.get('language', 'ru') in ('ru', 'en') else 'ru'

    def t(self, ru, en):
        return en if self.lang == 'en' else ru

    def level_label(self, difficulty):
        return (LEVELS_EN if self.lang == 'en' else LEVELS)[difficulty]

    def menu(self):
        rows = [[btn(self.t('🦝 Комната', '🦝 Room'), 'room'),
                 btn(self.t('✍️ Добавить дело', '✍️ Add task'), 'add')],
                [btn(self.t('☑️ Мои дела', '☑️ My tasks'), 'list:0'),
                 btn(self.t('🪴 Обустроить', '🪴 Furnish'), 'shop')],
                [btn(self.t('📖 О дивный чистый мир', '📖 O Brave Clean World'), 'history:0')]]
        return self.with_language(rows)

    def update_commands(self):
        descriptions = [
            ('start', self.t('Моя комната', 'My room')),
            ('add', self.t('Добавить дело', 'Add a task')),
            ('tasks', self.t('Мои дела', 'My tasks')),
            ('shop', self.t('Обустроить комнату', 'Furnish the room')),
            ('language', self.t('Русский / English', 'Русский / English')),
            ('cancel', self.t('Отменить ввод', 'Cancel input')),
            ('help', self.t('Как всё работает', 'How it works')),
        ]
        self.api.call('setMyCommands', commands=[{'command': command, 'description': description}
                                                  for command, description in descriptions])

    def with_language(self, keyboard):
        rows = [list(row) for row in (keyboard or [])]
        target, label = ('en', '🌐 English') if self.lang == 'ru' else ('ru', '🌐 Русский')
        if not any(button.get('callback_data', '').startswith('lang:') for row in rows for button in row):
            rows.append([btn(label, f'lang:{target}')])
        return rows

    def send(self, chat, text, keyboard=None):
        return self.api.call('sendMessage', chat_id=chat, text=text,
            reply_markup={'inline_keyboard': self.menu() if keyboard is None else self.with_language(keyboard)})

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
        if self.lang == 'en':
            text = (f'ZERO SPACE\nClean. Create. Continue the story.\n\n🦝 Red the raccoon writer’s room\n\n'
                    f'✨ Sparks: {balance}\nRewards in your story: {count}\nFurnishing: {stage}/3\n\n'
                    f'🌫 Chaos cleared: {round(cleared/CHAOS_TOTAL*100)}% · {chaos}/{CHAOS_TOTAL} remains\n\n')
            text += ("Red now has his own writing corner. A new chapter begins with you 🤍" if stage == 3 else
                     'Even one tiny task is a new line in our story. Red is happy to see you, even after a break.')
        else:
            text = (f'ZERO SPACE\nClean. Create. Continue the story.\n\n🦝 Комната енота-писателя Реда\n\n'
                    f'✨ Искры: {balance}\nНаград в твоей истории: {count}\nОбустройство: {stage}/3\n\n'
                    f'🌫 Хаос рассеян на {round(cleared/CHAOS_TOTAL*100)}% · осталось {chaos}/{CHAOS_TOTAL}\n\n')
            text += ('Теперь у Реда есть свой писательский уголок. Новая глава начинается с тебя 🤍' if stage == 3 else
                     'Даже одно маленькое дело — новая строчка в нашей истории. Ред рад тебе и после перерыва.')
        self.send_room_photo(chat, text, stage, chaos)

    def send_room_photo(self, chat, caption, stage, chaos, keyboard=None):
        cache = Path(os.environ.get('MEDIA_CACHE', str(ROOT/'data'/'media_cache')))
        image_path = cache/f'room-v2-{stage}-{chaos}.jpg'
        if not image_path.exists():
            compose_room(stage, chaos, image_path)
        file_key = f'photo:v2:{stage}:{chaos}'
        file_id = self.store.get(file_key)
        result = None
        markup = self.menu() if keyboard is None else self.with_language(keyboard)
        if file_id:
            try:
                result = self.api.call('sendPhoto', chat_id=chat, photo=file_id,
                    caption=caption, reply_markup={'inline_keyboard': markup})
            except APIError as exc:
                if exc.code != 400:
                    raise
        if result is None:
            result = self.api.photo(chat, image_path, caption, markup)
        if result.get('photo'):
            with self.store.db:
                self.store.put(file_key, result['photo'][-1]['file_id'])

    def show_tasks(self, chat, page=0, history=False):
        page = max(0, page)
        status = 'done' if history else 'active'
        total = self.store.db.execute('SELECT count(*) FROM tasks WHERE status=?', (status,)).fetchone()[0]
        page = min(page, max(0, (total-1)//8))
        order = 'completed DESC, id DESC' if history else 'difficulty ASC, id ASC'
        rows = self.store.db.execute(f'SELECT * FROM tasks WHERE status=? ORDER BY {order} LIMIT 8 OFFSET ?', (status,page*8)).fetchall()
        if not rows:
            empty_history = self.t(
                '📖 О дивный чистый мир\n\nЗдесь Ред запишет твои награды: сколько искр ты получила и за какое дело. Первая запись появится после выполненной задачи 🤍',
                '📖 O Brave Clean World\n\nRed will record your rewards here: how many sparks you earned and for which task. Your first entry will appear after a completed task 🤍')
            self.send(chat, empty_history if history else self.t(
                'Пока нет дел. Можно записать первое — даже очень маленькое.',
                'No tasks yet. Add the first one — even something tiny.'))
            return
        keyboard = [[btn((f'✨ +{r["difficulty"]} · {r["title"][:40]}' if history else f'{r["title"][:40]} · {r["difficulty"]} ✨'), f'task:{r["id"]}')] for r in rows]
        navigation = []
        prefix = 'history' if history else 'list'
        if page:
            navigation.append(btn(self.t('← Назад', '← Back'), f'{prefix}:{page-1}'))
        if (page+1)*8 < total:
            navigation.append(btn(self.t('Далее →', 'Next →'), f'{prefix}:{page+1}'))
        if navigation:
            keyboard.append(navigation)
        keyboard += [[btn(self.t('✍️ Добавить дело', '✍️ Add task'), 'add'),
                      btn(self.t('🦝 Комната', '🦝 Room'), 'room')]]
        if history:
            earned = self.store.db.execute("SELECT COALESCE(sum(difficulty),0) FROM tasks WHERE status='done'").fetchone()[0]
            text = self.t(
                f'📖 О дивный чистый мир\n\nТвоя история наград · {total} записей\nВсего заработано: {earned} ✨\n\nВыбери награду, чтобы вспомнить, за какое дело она получена. Потраченные искры остаются в истории.',
                f'📖 O Brave Clean World\n\nYour reward story · {total} entries\nTotal earned: {earned} ✨\n\nChoose a reward to remember the task behind it. Spent sparks remain in the story.')
        else:
            text = self.t(f'☑️ Мои дела · {total}\nЗадачи отсортированы от меньшего количества искр к большему.',
                          f'☑️ My tasks · {total}\nTasks are sorted from the fewest sparks to the most.')
        self.send(chat, text, keyboard)

    def show_task(self, chat, task_id):
        row = self.store.task(task_id)
        if not row or row['status'] == 'archived':
            self.send(chat, self.t('Задача уже убрана из списка.', 'This task has already been removed from the list.'))
            return
        if row['status'] == 'done':
            text = self.t(
                f'📖 О дивный чистый мир\n\nНаграда: +{row["difficulty"]} ✨\nЗа дело: {row["title"]}\n\nРед бережно сохранил эту маленькую победу.',
                f'📖 O Brave Clean World\n\nReward: +{row["difficulty"]} ✨\nFor: {row["title"]}\n\nRed carefully saved this small victory.')
            self.send(chat, text, [
                [btn(self.t('↩️ Вернуть задачу', '↩️ Return task'), f'undoask:{task_id}')],
                [btn(self.t('← История наград', '← Reward story'), 'history:0'),
                 btn(self.t('🦝 Комната', '🦝 Room'), 'room')]])
            return
        self.send(chat, self.t(f'{row["title"]}\n\nСложность: {self.level_label(row["difficulty"])}',
                               f'{row["title"]}\n\nDifficulty: {self.level_label(row["difficulty"])}'), [
            [btn(self.t('Готово ✓', 'Done ✓'), f'done:{task_id}')],
            [btn(self.t('Изменить сложность', 'Change difficulty'), f'level:{task_id}'),
             btn(self.t('Переименовать', 'Rename'), f'rename:{task_id}')],
            [btn(self.t('Убрать из списка', 'Remove from list'), f'archiveask:{task_id}'),
             btn(self.t('← Мои дела', '← My tasks'), 'list:0')]])

    def difficulty(self, chat, prefix, text):
        levels = LEVELS_EN if self.lang == 'en' else LEVELS
        self.send(chat, text, [[btn(label, f'{prefix}:{value}')] for value,label in levels.items()] +
                  [[btn(self.t('Отмена', 'Cancel'), 'cancel')]])

    def shop(self, chat):
        stage, balance = int(self.store.get('stage')), int(self.store.get('balance'))
        earned = int(self.store.get('total_earned'))
        if stage == 3:
            self.send(chat, self.t('Комната полностью обустроена 🤍\nВсе покупки останутся с тобой. Искры за новые дела продолжат копиться.',
                                   'The room is fully furnished 🤍\nAll purchases stay with you. Sparks from new tasks will keep accumulating.'))
            return
        title, contents, price = (UPGRADES_EN if self.lang == 'en' else UPGRADES)[stage]
        unlocked = earned >= CLEAR_AT[stage]
        if self.lang == 'en':
            text = f'🪴 {title}\n{contents}\n\nPrice: {price} ✨\nYou have: {balance} ✨\n'
            text += ('This area is clear — the space is ready.\n\n' if unlocked else
                     f'Clear the chaos first: {earned}/{CLEAR_AT[stage]} sparks earned from real tasks.\nRemaining: {CLEAR_AT[stage]-earned} ✨\n\n')
            text += 'The room image will update after purchase.'
        else:
            text = f'🪴 {title}\n{contents}\n\nЦена: {price} ✨\nУ тебя: {balance} ✨\n'
            text += ('Участок комнаты очищен — место свободно.\n\n' if unlocked else
                     f'Сначала рассей хаос: заработано {earned}/{CLEAR_AT[stage]} искр за реальные дела.\nОсталось: {CLEAR_AT[stage]-earned} ✨\n\n')
            text += 'После покупки картинка комнаты обновится.'
        keyboard = [[btn(self.t(f'Купить · {price} ✨', f'Buy · {price} ✨'), f'buy:{stage}')]] if unlocked else []
        keyboard += [[btn(self.t('☑️ Мои дела', '☑️ My tasks'),'list:0'),
                      btn(self.t('🦝 Комната', '🦝 Room'),'room')]]
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
                self.api.call('answerCallbackQuery', callback_query_id=callback['id'], text=self.t('Это личный бот.', 'This is a private bot.'))
            elif message.get('chat', {}).get('type') == 'private':
                self.send(message['chat']['id'], self.t('Это личный бот. Для первого входа используй свою ссылку привязки.',
                                                        'This is a private bot. Use your personal pairing link for the first login.'), [])
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
            self.send(chat, self.t('Пока я принимаю задачи текстом. Напиши, что хочешь сделать.',
                                   'For now I accept tasks as text. Tell me what you want to do.'))
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
            self.send(chat, self.t('Отменено. Можно начать заново в любое время.', 'Cancelled. You can start again at any time.'))
        elif command == '/language':
            self.send(chat, self.t('Выбери язык интерфейса.', 'Choose the interface language.'),
                      [[btn('Русский', 'lang:ru'), btn('English', 'lang:en')]])
        elif command == '/help':
            self.send(chat, self.t(
                'Напиши задачу → выбери сложность → отметь выполнение → получи искры.\n\n1 / 3 / 7 / 12 искр — по твоим ощущениям. Время не измеряется.\nВ активной задаче можно изменить название и сложность. Выполненную задачу можно вернуть: хаос и награда откатятся, а мебель останется.\nНет штрафов и обязательных ежедневных серий.\n\n/room — комната\n/add — новое дело\n/tasks — мои дела\n/shop — покупки\n/language — язык\n/cancel — отменить ввод',
                'Write a task → choose difficulty → mark it done → earn sparks.\n\n1 / 3 / 7 / 12 sparks — based on how it feels to you. Time is not measured.\nYou can rename an active task or change its difficulty. A completed task can be returned: chaos and its reward roll back, while furniture stays.\nNo penalties or mandatory daily streaks.\n\n/room — room\n/add — new task\n/tasks — my tasks\n/shop — purchases\n/language — language\n/cancel — cancel input'))
        elif command.startswith('/'):
            self.send(chat, self.t('Не знаю эту команду. Нажми кнопку ниже или /help.',
                                   "I don't know this command. Use a button below or /help."))
        else:
            if len(text) > 250:
                self.send(chat, self.t('Сократи название до 250 символов — так оно поместится в карточку.',
                                       'Shorten the name to 250 characters so it fits on the card.'))
                return
            draft = self.store.draft()
            if draft and draft['kind'].startswith('rename:'):
                changed = self.store.rename(int(draft['kind'].split(':')[1]), text)
                self.send(chat, self.t('Название обновлено.' if changed else 'Эта задача уже закрыта.',
                                       'Name updated.' if changed else 'This task is already closed.'))
            else:
                nonce = self.store.begin_draft(text, 'difficulty')
                self.difficulty(chat, f'new:{nonce}', self.t(f'«{text}»\n\nНасколько это сложно для тебя?',
                                                            f'“{text}”\n\nHow difficult does this feel?'))

    def add(self, chat):
        self.store.begin_draft()
        self.send(chat, self.t('Какое дело хочешь записать?\nНапиши одним сообщением. Например: «Разобрать одну полку».',
                               'What task do you want to add?\nSend it in one message. For example: “Clear one shelf.”'),
                  [[btn(self.t('Отмена', 'Cancel'),'cancel')]])

    def on_callback(self, chat, data):
        parts = data.split(':')
        action = parts[0]
        if data == 'room':
            self.room(chat)
        elif data in ('lang:ru', 'lang:en'):
            with self.store.db:
                self.store.put('language', data.split(':')[1])
            self.update_commands()
            self.room(chat)
        elif data == 'add':
            self.add(chat)
        elif data == 'cancel':
            self.store.cancel()
            self.send(chat, self.t('Отменено. Ред никуда не торопится 🤍', 'Cancelled. Red is in no hurry 🤍'))
        elif data == 'shop':
            self.shop(chat)
        elif len(parts) == 3 and action == 'new' and parts[2].isdigit():
            task_id = self.store.create_task(parts[1], int(parts[2]))
            if task_id:
                self.show_task(chat, task_id)
            else:
                self.send(chat, self.t('Эта карточка уже обработана или заменена. Открой «Мои дела».',
                                       'This card was already handled or replaced. Open “My tasks”.'))
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
                    phrase = (AFTER_MESSAGES if self.lang == 'en' else AFTER_MESSAGES_RU)[number % len(AFTER_MESSAGES)]
                    cleared_before, cleared_after = CHAOS_TOTAL-before, CHAOS_TOTAL-after
                    unlocked = [i for i,t in enumerate(CLEAR_AT) if cleared_before < t <= cleared_after]
                    extra = ''
                    if unlocked:
                        title = (UPGRADES_EN if self.lang == 'en' else UPGRADES)[unlocked[-1]][0]
                        extra = self.t(f'\n\n🌟 Участок хаоса исчез полностью. Открыто улучшение «{title}».',
                                       f'\n\n🌟 A section of chaos vanished completely. “{title}” is now unlocked.')
                    completion = self.t(
                        f'{phrase}\n\n✨ +{reward} искр! Хаос уменьшился на {reward}.\nРед записал награду в «О дивный чистый мир».\nБаланс: {self.store.get("balance")} ✨{extra}\n\nМожно закончить на сегодня.',
                        f'{phrase}\n\n✨ +{reward} sparks! Chaos decreased by {reward}.\nRed recorded the reward in “O Brave Clean World”.\nBalance: {self.store.get("balance")} ✨{extra}\n\nYou can stop for today.')
                    self.send_room_photo(chat, completion, int(self.store.get('stage')), after,
                                         [[btn(self.t('☑️ Мои дела', '☑️ My tasks'), 'list:0'),
                                           btn(self.t('🦝 Комната', '🦝 Room'), 'room')]])
                else:
                    self.send(chat, self.t('Эта задача уже закрыта. Награда повторно не списывается и не начисляется.',
                                           'This task is already closed. Its reward cannot be added twice.'))
            elif action == 'undoask':
                row = self.store.task(number)
                if row and row['status'] == 'done':
                    self.send(chat, self.t(
                        f'Вернуть задачу «{row["title"]}»?\n\n{row["difficulty"]} искр будут отменены, хаос увеличится обратно. Уже купленная мебель и этап комнаты останутся.',
                        f'Return “{row["title"]}”?\n\n{row["difficulty"]} sparks will be reversed and the chaos will grow back. Purchased furniture and the room stage will stay.'),
                        [[btn(self.t('Да, вернуть', 'Yes, return it'), f'undo:{number}'),
                          btn(self.t('Оставить выполненной', 'Keep completed'), f'task:{number}')]])
                else:
                    self.send(chat, self.t('Эта задача уже возвращена.', 'This task has already been returned.'))
            elif action == 'undo':
                reward = self.store.undo(number)
                if reward:
                    chaos = int(self.store.get('chaos_remaining'))
                    debt = int(self.store.get('undo_debt', '0'))
                    debt_note = self.t(
                        f'\nСледующие {debt} ✨ сначала восстановят отменённую награду.' if debt else '',
                        f'\nThe next {debt} ✨ will first restore the reversed reward.' if debt else '')
                    caption = self.t(
                        f'↩️ Задача возвращена в активные.\nХаос увеличился на {reward}. Мебель и обустройство не изменились.{debt_note}',
                        f'↩️ The task is active again.\nChaos increased by {reward}. Furniture and furnishing did not change.{debt_note}')
                    self.send_room_photo(chat, caption, int(self.store.get('stage')), chaos,
                                         [[btn(self.t('Открыть задачу', 'Open task'), f'task:{number}'),
                                           btn(self.t('☑️ Мои дела', '☑️ My tasks'), 'list:0')]])
                else:
                    self.send(chat, self.t('Эту задачу уже нельзя вернуть повторно.', 'This task cannot be returned again.'))
            elif action == 'level':
                row = self.store.task(number)
                if row and row['status'] == 'active':
                    self.difficulty(chat, f'edit:{number}', self.t('Выбери новую сложность. Награда изменится до выполнения.',
                                                                  'Choose a new difficulty. The reward changes before completion.'))
                else:
                    self.send(chat, self.t('Сложность закрытой задачи уже не меняется.',
                                           'The difficulty of a closed task cannot be changed.'))
            elif action == 'edit' and len(parts) == 3 and parts[2].isdigit():
                self.store.change_difficulty(number, int(parts[2]))
                self.show_task(chat, number)
            elif action == 'rename':
                row = self.store.task(number)
                if row and row['status'] == 'active':
                    self.store.begin_draft(kind=f'rename:{number}')
                    self.send(chat, self.t('Напиши новое название задачи.', 'Send the new task name.'),
                              [[btn(self.t('Отмена', 'Cancel'),'cancel')]])
                else:
                    self.send(chat, self.t('Эта задача уже закрыта.', 'This task is already closed.'))
            elif action == 'archiveask':
                self.send(chat, self.t('Убрать задачу из активных? Искры не изменятся.',
                                       'Remove this task from the active list? Sparks will not change.'),
                          [[btn(self.t('Да, убрать', 'Yes, remove'),f'archive:{number}'),
                            btn(self.t('Оставить', 'Keep it'),f'task:{number}')]])
            elif action == 'archive':
                self.store.archive(number)
                self.show_tasks(chat)
            elif action == 'buy':
                result = self.store.buy(number)
                if result == 'ok':
                    self.room(chat)
                elif result == 'poor':
                    self.send(chat, self.t('Пока не хватает искр. Ничего не списано — можно вернуться позже.',
                                           'Not enough sparks yet. Nothing was spent — you can return later.'))
                elif result == 'locked':
                    self.send(chat, self.t('Это место ещё скрыто хаосом. Выполни несколько реальных дел — искры постепенно освободят его.',
                                           'This space is still hidden by chaos. Complete a few real tasks and sparks will gradually clear it.'))
                else:
                    self.send(chat, self.t('Этот этап уже куплен или ещё не открыт.',
                                           'This stage is already purchased or not yet available.'))
            else:
                self.send(chat, self.t('Открой актуальное меню ниже.', 'Use the current menu below.'))
        else:
            self.send(chat, self.t('Открой актуальное меню ниже.', 'Use the current menu below.'))


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
        bot.update_commands()
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
