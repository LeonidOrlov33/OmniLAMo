# ============================================
# telegram_runner.py — Telegram-бот внутри веб-сервиса
# ============================================
# Зачем это нужно: на бесплатном Render доступен только один веб-инстанс,
# отдельный background worker там не даётся. Поэтому бот поднимается фоновой
# нитью внутри того же процесса, что и server.py — тогда и сайт, и Telegram
# работают, а платить (точнее, не платить) нужно за один сервис.
#
# Бот НЕ включается сам по себе: нужна переменная TELEGRAM_BOT_TOKEN. Пока её
# нет, этот модуль молча ничего не делает и сайт работает как обычно.
#
# Локально ничего не меняется: `python bot.py` в папке telegram_agent
# по-прежнему запускает только бота, без сайта.
# ============================================

import os
import sys
import threading

from tools import logger

# Никакой нити не запускаем дважды: uvicorn может импортировать модуль
# повторно, а лишний long polling только жёг бы лимиты Telegram.
_started = False
# RLock — как и все замки в tools.py: повторный захват тем же потоком не
# вешает процесс намертво.
_start_lock = threading.RLock()

# Сколько ждать после сбоя бота, прежде чем пробовать снова (секунды).
# Без паузы упавший бот крутился бы в tight loop и забивал лог.
RETRY_DELAY = 30


def telegram_token() -> str:
    """Токен бота из переменных окружения. Пусто — бот не нужен."""
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _bot_dir() -> str:
    """Путь к папке telegram_agent (она лежит рядом с free_agent)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "telegram_agent")


def _run_bot_forever() -> None:
    """Запускает long polling и перезапускает его при сбое.

    run_polling() рассчитан на вечный цикл, но падает, если Telegram не
    ответил на первый getMe (например, неверный токен или сеть моргнула).
    Внутри веб-сервиса такое падение не должно ничего ронять: ловим,
    пишем в лог и пробуем снова через паузу.
    """
    import time

    bot_dir = _bot_dir()
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)

    try:
        import bot  # noqa: WPS433 — импорт внутри функции: без токена он не нужен
    except Exception as e:
        logger.error(f"Telegram-бот не подключён: {e}")
        return

    token = telegram_token()
    logger.info("Telegram-бот: запускаю long polling внутри веб-сервиса")

    while True:
        try:
            bot.run_polling(token)
        except Exception as e:
            logger.error(f"Telegram-бот остановился: {e} — повтор через {RETRY_DELAY} с")
            time.sleep(RETRY_DELAY)


def start_telegram_bot() -> bool:
    """Поднимает бота фоновой нитью. True — запущен, False — не потребовался."""
    global _started

    if not telegram_token():
        logger.info("TELEGRAM_BOT_TOKEN не задан — Telegram-бот выключен")
        return False

    with _start_lock:
        if _started:
            logger.info("Telegram-бот уже запущен — второй раз не поднимаю")
            return False
        _started = True

    thread = threading.Thread(
        target=_run_bot_forever,
        name="telegram-bot",
        # daemon=True: при остановке сервиса Render гасит процесс, и нить
        # не должна держать его «в живых» до бесконечности.
        daemon=True,
    )
    thread.start()
    logger.info("Telegram-бот запущен фоновой нитью")
    return True