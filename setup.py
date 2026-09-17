"""Interactive local setup. Secret token is never printed."""
import getpass
import os
import secrets
from bot import ROOT, Telegram, APIError


def main():
    target = ROOT / '.env'
    if target.exists():
        raise SystemExit('.env уже существует. Настройки сохранены. Для запуска: python bot.py')
    print('Zero Space · Clean. Create. Continue the story.')
    print('1. Открой https://t.me/BotFather и создай бота командой /newbot.')
    print('2. Скопируй выданный токен. Ввод ниже будет скрыт.')
    token = getpass.getpass('Токен Telegram-бота: ').strip()
    if not token or any(c.isspace() for c in token):
        raise SystemExit('Токен не должен быть пустым или содержать пробелы.')
    try:
        me = Telegram(token).call('getMe')
    except APIError as exc:
        raise SystemExit(f'Не удалось проверить токен (код {exc.code}). Настройки не записаны.') from None
    pairing = secrets.token_urlsafe(24)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(f'BOT_TOKEN={token}\nPAIRING_CODE={pairing}\n')
    print('\nНастройки сохранены. Теперь запусти: python bot.py')
    print('После запуска открой эту личную ссылку и нажми «Запустить»:')
    print(f'https://t.me/{me["username"]}?start={pairing}')
    print('Ссылка привяжет бота к твоему аккаунту один раз. Не пересылай её другим.')


if __name__ == '__main__':
    main()
