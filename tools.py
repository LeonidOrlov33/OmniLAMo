# ============================================
# tools.py — ИНСТРУМЕНТЫ (движок агента)
# ============================================
# Вторая часть распиленного server.py: настройки, шифрование, Supabase,
# новости, задания, учётки сайтов, вход по логину/паролю, вызов нейросети
# и разбор команд. Пользовательский интерфейс и HTTP-роуты — в server.py.
# Этот файл НЕ запускается сам по себе: импортируется из server.py.
# ============================================

# ============================================
# ДВИЖОК АГЕНТА (OmniLAMo, бесплатная бета)
# ============================================

import os
import uuid
import random
import datetime
import hashlib
import ipaddress
import logging
import re
import json
import socket
import time
import threading
from logging.handlers import RotatingFileHandler
# FastAPI-приложение, middleware и транспортные Pydantic-модели живут в
# server.py / schemas.py — это их слой. В самом движке pydantic больше не
# нужен: модели импортируются из schemas.py ниже, ради обратной совместимости.
from supabase import create_client, Client

# Groq SDK не нужен: нейросеть вызывается напрямую по HTTP, поэтому подойдёт
# любой совместимый с OpenAI путь (Groq, OpenAI, OpenRouter, локальный сервер).

# Все пути считаются от папки самого server.py, а не от текущего каталога:
# раньше запуск из другого каталога ломал поиск settings.json и downloads/.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = BASE_DIR
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

# Потолок на скачиваемый файл. Без него ссылка на многогигабайтный образ
# забивала диск бесплатного Render (там всего 512 МБ) и роняла сервис.
# 50 МБ — с запасом хватает документам, картинкам и архивам с сайтов.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_MB = MAX_DOWNLOAD_BYTES // (1024 * 1024)

# Парсинг
import requests
from bs4 import BeautifulSoup
from html import unescape
import urllib.parse

# Шифрование паролей
from cryptography.fernet import Fernet
import base64 as _b64

# -------------------------------------------------
# ЛОГИРОВАНИЕ
# -------------------------------------------------
# Логи пишутся с ротацией: server.log не растёт бесконечно (раньше за пару
# недель набегало 70 КБ ошибок и хвост истории терялся).
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
logger = logging.getLogger("OmniLAMo")
logger.setLevel(logging.INFO)
# Обработчики ставим только один раз: тест и повторный импорт модуля иначе
# добавляли их снова, и каждая строка лога печаталась по нескольку раз.
if not logger.handlers:
    _log_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    _log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(_log_handler)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)
    logger.propagate = False

# -------------------------------------------------
# КОНФИГУРАЦИЯ
# -------------------------------------------------
# Секретов в коде больше нет: адрес и ключ Supabase, ключ нейросети приходят
# только из переменных окружения. На Render это раздел Environment, локально —
# файл .env рядом с server.py или переменные оболочки. Раньше ключи были
# прописаны прямо здесь: любой, кто увидел исходник (или случайно выложил его
# на GitHub), получал полный доступ к базе и жёг лимит нейросети.

def _load_env_file() -> None:
    """Читает .env рядом с server.py и заполняет os.environ.

    Своего разбора достаточно: python-dotenv тянет лишнюю зависимость, а нужен
    ровно один формат KEY=VALUE. Уже заданные переменные не перезаписываются —
    значит, окружение Render всегда важнее локального файла.
    """
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        logger.info(".env прочитан — переменные окружения заполнены")
    except Exception as e:
        logger.error(f".env не прочитан: {e}")


_load_env_file()

# Секретов в коде нет: адрес и ключ Supabase приходят только из переменных
# окружения (на Render — раздел Environment, локально — файл .env рядом с
# server.py). Раньше здесь были прописаны настоящие адрес и ключ проекта:
# любой, кто увидел исходник или выложил его на GitHub, получал доступ к базе.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# -------------------------------------------------
# НАСТРОЙКИ (меняются из админки: путь, API-ключ, модель)
# -------------------------------------------------
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_SETTINGS = {
    # Пароля по умолчанию больше нет. Раньше здесь стоял общеизвестный пароль,
    # прописанный прямо в коде: если переменная ADMIN_PASSWORD не задана,
    # админка открывалась этим паролем — то есть фактически без пароля, и
    # любой, кто читал исходник, входил в неё. Теперь значение по умолчанию
    # пустое, а is_admin() отвергает пустой пароль: неверная настройка
    # окружения запирает вход, а не открывает его.
    "admin_password": os.getenv("ADMIN_PASSWORD", ""),
    "ai_base_url": os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1"),
    "ai_api_key": os.getenv("AI_API_KEY", GROQ_API_KEY),
    "ai_model": os.getenv("AI_MODEL", "llama-3.3-70b-versatile"),
    "temperature": 0.7,
    "max_tokens": 500,
    "system_prompt": "Ты — OmniLAMo, личный ИИ-агент. Отвечай кратко, дружелюбно и по-русски.",
}

# Замки модуля. Все — RLock, а не Lock: обычный threading.Lock не пускает
# тот же поток второй раз, и случайное «взял замок внутри функции, которую
# вызвал уже под этим замком» намертво вешает обработчик (а с ним и воркер).
# RLock такую ошибку прощает. Порядок захвата, если когда-нибудь понадобится
# взять два замка сразу: _settings_lock -> _rate_lock -> _tasks_lock ->
# _sched_lock -> _running_lock (обратный порядок запрещён).
_settings_lock = threading.RLock()
SETTINGS = dict(DEFAULT_SETTINGS)


def load_settings():
    """Читает settings.json, добавляя недостающие ключи из DEFAULT_SETTINGS."""
    data = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                # Пустое значение из файла НЕ должно затирать то, что пришло из
                # переменных окружения. Иначе settings.json с "admin_password": ""
                # отменял бы ADMIN_PASSWORD из .env, и админка запиралась бы
                # навсегда — ровно так же ведёт себя загрузка из облака ниже.
                data.update({k: v for k, v in saved.items()
                             if k in DEFAULT_SETTINGS and v not in (None, "")})
                logger.info("Настройки загружены из settings.json")
    except Exception as e:
        logger.error(f"load_settings: {e}")
    SETTINGS.clear()
    SETTINGS.update(data)
    return SETTINGS


# Настройки админки живут в двух местах: файл рядом с server.py (локальный запуск)
# и таблица app_settings в Supabase (Render). На сервере файл стирается при
# каждом деплое, поэтому облако — основной источник, файл — резерв.
SETTINGS_CLOUD_ROW = "main"


def save_settings():
    """Пишет SETTINGS в файл и, если доступно, в Supabase. Вызывать под _settings_lock."""
    ok = False
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, ensure_ascii=False, indent=2)
        ok = True
    except Exception as e:
        # На Render папка приложения может быть доступна только для чтения —
        # это не ошибка, если настройки уехали в облако.
        logger.warning(f"save_settings в файл: {e}")
    if cloud_ready("app_settings"):
        try:
            supabase.table("app_settings").upsert({
                "id": SETTINGS_CLOUD_ROW,
                "data": dict(SETTINGS),
                "updated_at": datetime.datetime.now().isoformat(),
            }).execute()
            ok = True
        except Exception as e:
            logger.error(f"save_settings в Supabase: {e}")
    return ok


def load_settings_from_cloud():
    """Подтягивает настройки из Supabase. Вызывать после connect_services().

    Без этого сохранённый в админке ключ ИИ терялся после каждого перезапуска
    на Render, и сервер снова отвечал «Нейросеть не настроена».
    """
    if not cloud_ready("app_settings"):
        return SETTINGS
    try:
        res = supabase.table("app_settings").select("data").eq(
            "id", SETTINGS_CLOUD_ROW).limit(1).execute()
        rows = res.data or []
        data = rows[0].get("data") if rows else None
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict) and data:
            with _settings_lock:
                for key, value in data.items():
                    if key in DEFAULT_SETTINGS and value not in (None, ""):
                        SETTINGS[key] = value
            logger.info("Настройки загружены из Supabase")
    except Exception as e:
        logger.error(f"load_settings_from_cloud: {e}")
    return SETTINGS


def update_settings(patch: dict):
    """Меняет часть настроек и сохраняет их.

    Возвращает (SETTINGS, saved, rejected):

    * saved — удалось ли записать настройки хоть куда-то (файл или облако).
      Раньше результат save_settings() молча выбрасывался, и админка писала
      «Сохранено», даже если не сохранилось ничего: на Render папка бывает
      только для чтения, а Supabase недоступен. Теперь это видно вызывающему.
    * rejected — какие ключи не приняты и почему. Раньше кривое значение
      («0,7» вместо «0.7») молча пропускалось, и пользователь не понимал,
      почему настройка не применилась.
    """
    with _settings_lock:
        rejected = []
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS:
                rejected.append(f"{key}: неизвестная настройка")
                continue
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if value == "":
                    continue
            if key in ("temperature", "max_tokens"):
                try:
                    # Запятая как десятичный разделитель: «0,7» — частая опечатка,
                    # и раньше она просто проглатывалась.
                    value = float(str(value).replace(",", ".")) if key == "temperature" else int(value)
                except (TypeError, ValueError):
                    expected = "число (например 0.7)" if key == "temperature" else "целое число"
                    rejected.append(f"{key}: ожидается {expected}, получено {value!r}")
                    continue
            SETTINGS[key] = value
        saved = save_settings()
        # Кэш ответов привязан к модели — при смене настроек он устаревает
        ai_cache.clear()
    return SETTINGS, saved, rejected


def public_settings():
    """Настройки для админки: ключ маскируется, пароль не отдаём."""
    key = SETTINGS.get("ai_api_key", "")
    masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("*" * len(key))
    return {
        "ai_base_url": SETTINGS.get("ai_base_url", ""),
        "ai_api_key_masked": masked,
        "ai_api_key_set": bool(key),
        "ai_model": SETTINGS.get("ai_model", ""),
        "temperature": SETTINGS.get("temperature", 0.7),
        "max_tokens": SETTINGS.get("max_tokens", 500),
        "system_prompt": SETTINGS.get("system_prompt", ""),
        "settings_file": SETTINGS_FILE,
    }


load_settings()

# -------------------------------------------------
# ШИФРОВАНИЕ ПАРОЛЕЙ КЛИЕНТОВ
# -------------------------------------------------
FERNET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fernet.key")


def _load_fernet_key() -> str:
    """Возвращает ключ шифрования и делает его постоянным.

    Раньше ключ молча выводился из SUPABASE_KEY, и смена этого ключа делала
    сохранённые пароли нечитаемыми. Теперь ключ один раз сохраняется в
    fernet.key и живёт отдельно от настроек Supabase.
    """
    env_key = os.getenv("FERNET_KEY")
    if env_key:
        return env_key
    try:
        if os.path.exists(FERNET_KEY_FILE):
            with open(FERNET_KEY_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
    except Exception as e:
        logger.error(f"fernet key read: {e}")
    # Основу берём из SUPABASE_KEY: уже сохранённые пароли остаются рабочими.
    # Если переменная не задана (локальный запуск без облака), берём случайную
    # строку — ключ всё равно один раз ложится в fernet.key и больше не меняется.
    seed = SUPABASE_KEY or uuid.uuid4().hex
    derived = _b64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()).decode()
    try:
        with open(FERNET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(derived)
        logger.info("Ключ шифрования паролей сохранён в fernet.key")
    except Exception as e:
        logger.error(f"fernet key write: {e}")
    return derived


FERNET_KEY = _load_fernet_key()
try:
    _cipher = Fernet(FERNET_KEY.encode())
except Exception as e:
    logger.error(f"fernet key invalid: {e}")
    _backup = f"{FERNET_KEY_FILE}.corrupt-{int(time.time())}"
    try:
        if os.path.exists(FERNET_KEY_FILE):
            os.replace(FERNET_KEY_FILE, _backup)
    except OSError:
        pass
    FERNET_KEY = _b64.urlsafe_b64encode(
        hashlib.sha256((SUPABASE_KEY or uuid.uuid4().hex).encode()).digest()).decode()
    _cipher = Fernet(FERNET_KEY.encode())

def encrypt_secret(plain: str) -> str:
    return _cipher.encrypt(plain.encode()).decode()

def decrypt_secret(token: str) -> str:
    try:
        return _cipher.decrypt(token.encode()).decode()
    except Exception:
        return ""


def login_from_row(row) -> str:
    """Логин из строки учётки: сначала пробуем расшифровать, иначе открытый текст.

    Логины начали шифровать позже паролей, поэтому в одной таблице лежат
    записи обоих видов. Fernet-токен всегда начинается с «gAAAAA» и
    расшифровывается ключом; обычный логин такую строку не образует, так что
    ошибочно «расшифровать» открытый текст не получится.
    """
    raw = str((row or {}).get("login") or "")
    if not raw:
        return ""
    return decrypt_secret(raw) or raw


def login_to_store(login: str) -> str:
    """Логин в том виде, в котором его пишем в базу: зашифрованным."""
    login = str(login or "")
    if not login:
        return ""
    # Уже зашифрованный логин (повторное сохранение, перенос из файла) не
    # шифруем второй раз: иначе после миграции логин перестал бы читаться.
    return login if decrypt_secret(login) else encrypt_secret(login)

supabase: Client = None
ai_cache = {}

# Таблицы, которые реально есть в облаке. Проверяются один раз на старте:
# если таблицы нет, работаем с локальным файлом и не ждём таймаут на каждом запросе.
_CLOUD_TABLES = set()

# Таблицы, которые создаёт schema.sql. Без них облачный режим не имеет смысла.
# telegram_users — таблица бота: связка «chat_id → api_key». Её может не быть
# (старая схема) — тогда бот работает с локальным файлом.
CLOUD_TABLE_NAMES = ("users", "message_queue", "tasks", "credentials",
                     "app_settings", "telegram_users")


def cloud_ready(table: str) -> bool:
    """True, если клиент Supabase подключён и таблица доступна."""
    return supabase is not None and table in _CLOUD_TABLES


# -------------------------------------------------
# ПОДКЛЮЧЕНИЕ С ПОВТОРАМИ
# -------------------------------------------------
def connect_services():
    """Создаёт клиент Supabase. Без ключей сразу уходит в локальный режим.

    Раньше при пустых SUPABASE_URL/SUPABASE_KEY цикл трижды вызывал
    create_client и трижды печатал «supabase_url is required» с паузой в две
    секунды. Выглядело как сбой сети, хотя переменные просто не заданы: ни
    .env рядом с server.py, ни переменных окружения Render.
    """
    global supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning(
            "SUPABASE_URL/SUPABASE_KEY не заданы — работаем с локальными файлами"
        )
        logger.info(f"AI: {SETTINGS.get('ai_base_url')} / {SETTINGS.get('ai_model')}")
        return
    for attempt in range(3):
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            logger.info("Supabase connected")
            break
        except Exception as e:
            logger.warning(f"Supabase attempt {attempt+1}: {e}")
            time.sleep(2)
    logger.info(f"AI: {SETTINGS.get('ai_base_url')} / {SETTINGS.get('ai_model')}")


def probe_cloud_tables():
    """Проверяет, какие таблицы есть в Supabase.

    Раньше сервер писал задания и учётки только в локальные файлы: на Render
    диск не сохраняется между перезапусками, и данные пропадали после каждого
    деплоя. Теперь при наличии таблиц всё уходит в облако, а файлы остаются
    запасным вариантом для локального запуска.
    """
    _CLOUD_TABLES.clear()
    if supabase is None:
        logger.warning("Supabase недоступен — работаем с локальными файлами")
        return
    for table in CLOUD_TABLE_NAMES:
        try:
            supabase.table(table).select("*").limit(1).execute()
            _CLOUD_TABLES.add(table)
        except Exception as e:
            logger.warning(f"Таблица {table} в Supabase недоступна: {str(e)[:200]}")
    logger.info(f"Облачные таблицы: {sorted(_CLOUD_TABLES) or 'нет'}")

# -------------------------------------------------
# УТИЛИТЫ
# -------------------------------------------------
def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

# is_valid_name() переехала в schemas.py вместе с моделями, которые её
# используют; ниже она импортируется ради обратной совместимости.

request_log = {}
# Счётчик запросов трогают сразу несколько нитей: FastAPI выполняет
# синхронные обработчики в пуле потоков. Без замка две нити могли
# перезаписать список меток друг друга, и часть запросов не считалась.
_rate_lock = threading.RLock()


def rate_limit(key: str, max_requests: int = 10, window_seconds: int = 60) -> bool:
    """Простой счётчик запросов в скользящем окне.

    Раньше здесь было (now - t).seconds: у datetime.timedelta это остаток от
    суток, а не общее число секунд, поэтому окно лома лось на границе суток —
    лимит либо не срабатывал, либо резал запросы слишком надолго.
    """
    now = datetime.datetime.now()
    with _rate_lock:
        stamps = [t for t in request_log.get(key, [])
                  if (now - t).total_seconds() < window_seconds]
        if len(stamps) >= max_requests:
            request_log[key] = stamps
            return False
        stamps.append(now)
        request_log[key] = stamps
        # Ключей становится много (по одному на api_key) — подчищаем устаревшие.
        # Прежняя уборка «if not v» не удаляла ничего: список меток пустым не бывает
        # (свежая метка дописывается строкой выше), поэтому словарь рос бесконечно.
        # Теперь выбрасываем ключи, у которых ВСЕ метки вышли за окно.
        if len(request_log) > 5000:
            for k in [k for k, v in request_log.items()
                      if not any((now - t).total_seconds() < window_seconds for t in v)]:
                request_log.pop(k, None)
    return True

# -------------------------------------------------
# МОДЕЛИ (вынесены в schemas.py)
# -------------------------------------------------
# Транспортные Pydantic-модели запросов объявлены в отдельном модуле
# schemas.py: это слой валидации HTTP-запроса, а не бизнес-логика движка.
# Импортируем их сюда ради обратной совместимости — весь существующий код
# (и `from tools import *` в server.py) продолжает видеть те же имена.
from schemas import (  # noqa: F401
    API_KEY_FIELD,
    RegisterRequest,
    AskRequest,
    ApiKeyRequest,
    HistoryRequest,
    FetchUrlRequest,
    RememberRequest,
    CompleteTaskRequest,
    SaveCredentialsRequest,
    DeleteCredentialsRequest,
    BrowseRequest,
    DownloadRequest,
    NewsRequest,
    is_valid_name,
)

# -------------------------------------------------
# ИНТЕРНЕТ: СТРАНИЦЫ И НОВОСТИ
# -------------------------------------------------
# Ленты новостей без ключей и регистрации. Если одна не ответит — берём следующую.
NEWS_FEEDS = [
    ("Лента.ру", "https://lenta.ru/rss/news"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml"),
    ("РИА Новости", "https://ria.ru/export/rss2/archive/index.xml"),
]

# Многие сайты (в частности Википедия) отдают 403, если у запроса нет
# заголовков Accept / Accept-Language — одного User-Agent им мало. Заголовки
# собирает browser_headers() ниже: она подставляет случайный UA на каждый запрос,
# поэтому общих констант USER_AGENT / BROWSER_HEADERS больше нет — один и тот же
# UA во всех запросах сразу выдавал робота.

# Википедия и другие сайты отклоняют "браузерный" UA без контакта (HTTP 403) и
# требуют описательный UA по своей робот-политике. Второй заход делаем с ним.
HONEST_HEADERS = {
    # Контакт нужен по робот-политике Википедии и подобных сайтов: с заглушкой
    # user@example.com они отвечают 403. Адрес берём из окружения, чтобы его
    # не приходилось править в коде.
    "User-Agent": "DesktopAgent/1.0 (personal assistant; contact: "
                  + os.getenv("CONTACT_EMAIL", "user@example.com") + ") requests/2.32",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# -------------------------------------------------
# РОТАЦИЯ USER-AGENT
# -------------------------------------------------
# Один и тот же User-Agent на все запросы — заметный признак робота: сайты
# (Wildberries, Ozon, маркетплейсы, школьные дневники) быстро начинают отдавать
# капчу. Поэтому на каждый исходящий запрос берём случайный РЕАЛЬНЫЙ UA из
# набора: iPhone/Safari, Windows/Chrome, Android/Chrome. Все строки —
# настоящие связки «браузер + платформа», а не выдуманные: подделка, которой
# не существует в природе, выдаёт бота даже вернее, чем один статичный UA.
USER_AGENTS = (
    # Windows + Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # macOS + Chrome / Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # iPhone / iPad + Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1",
    # Android + Chrome
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
)


def random_user_agent() -> str:
    """Случайный реалистичный User-Agent из набора выше."""
    return random.choice(USER_AGENTS)


def browser_headers(user_agent: str = "") -> dict:
    """Заголовки запроса с случайным (или заданным) User-Agent.

    Возвращает НОВЫЙ словарь на каждый вызов: если отдавать общий
    BROWSER_HEADERS, случайный UA одного потока затрёт UA другого.
    """
    return {
        "User-Agent": user_agent or random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }


# Сети, куда серверу ходить нельзя: локальная петля, приватные диапазоны и
# служебные адреса облаков. Проверка нужна, потому что ссылку даёт
# пользователь, а запрос делает сервер — без неё это готовый SSRF
# (чтение админки, метаданных облака, чужих внутренних сервисов).
_LOOPBACK_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),      # link-local + метаданные облака
    ipaddress.ip_network("fc00::/7"),            # unique local IPv6
    ipaddress.ip_network("fe80::/10"),           # link-local IPv6
    ipaddress.ip_network("0.0.0.0/8"),
)
# Имена, заведомо указывающие внутрь: их даже не резолвим.
_BLOCKED_HOSTNAMES = ("localhost", "localhost.localdomain", "metadata.google.internal")


def _safe_public_url(url: str):
    """Пускать ли серверный запрос по этому адресу.

    Возвращает (True, "") или (False, причина). Отсекаем не-http схемы, локальные
    имена и адреса, которые резолвятся в приватную/петлевую сеть.
    """
    raw = (url or "").strip()
    if not raw:
        return False, "Пустая ссылка"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return False, "Нужна ссылка http:// или https://"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "В ссылке нет адреса сайта"
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return False, "Внутренние адреса недоступны"
    candidates = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False, f"Не удалось разрешить адрес: {host}"
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not candidates:
        return False, f"Не удалось разрешить адрес: {host}"
    for ip in candidates:
        for net in _LOOPBACK_NETS + _PRIVATE_NETS:
            if ip.version == net.version and ip in net:
                return False, "Внутренние адреса недоступны"
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, "Недопустимый адрес"
    return True, ""


def _download(url: str, timeout: int):
    """GET страницы: случайный браузерный UA, при 403 — повтор с честным UA."""
    ok, reason = _safe_public_url(url)
    if not ok:
        # Запрос идёт от имени сервера: без этой проверки чужая ссылка
        # могла заставить его прочитать внутренний сервис (SSRF).
        raise ValueError(reason)
    # Случайный UA на каждый запрос: один и тот же выдаёт робота и ускоряет
    # получение капчи (см. USER_AGENTS).
    res = requests.get(url, timeout=timeout, headers=browser_headers())
    if res.status_code == 403:
        res = requests.get(url, timeout=timeout, headers=HONEST_HEADERS)
    res.raise_for_status()
    return res


def http_get_text(url: str, timeout: int = 15) -> str:
    """Скачивает страницу и возвращает текст. Определяет кодировку по ответу."""
    res = _download(url, timeout)
    res.raise_for_status()
    # requests угадывает кодировку по HTTP-заголовку и часто ошибается на русских сайтах,
    # поэтому сначала смотрим на <meta charset>, и только потом на догадку requests.
    if not res.encoding or res.encoding.lower() in ("iso-8859-1", "ascii"):
        res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def page_to_text(html: str, limit: int = 4000) -> str:
    """Вытаскивает читаемый текст из HTML, выбрасывая скрипты и стили."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:limit]


def parse_rss_titles(xml_text: str, limit: int = 20):
    """Достаёт заголовки из RSS/Atom. Работает и без строгого парсера."""
    soup = BeautifulSoup(xml_text, "xml")
    items = soup.find_all("item") or soup.find_all("entry")
    titles = []
    for it in items[:limit]:
        t = it.find("title")
        if not t:
            continue
        title = t.get_text(" ", strip=True)
        if title:
            titles.append(unescape(title))
    if titles:
        return titles
    # Запасной путь: если XML разобрать не удалось, вытаскиваем <title> регуляркой.
    raw = re.findall(r"<title[^>]*>(.*?)</title>", xml_text, re.S | re.I)
    for t in raw:
        t = unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip()
        if t:
            titles.append(t)
        if len(titles) >= limit:
            break
    return titles


def collect_news(limit: int = 8):
    """Собирает заголовки из NEWS_FEEDS. Возвращает (источник, заголовки, ошибки)."""
    errors = []
    for name, feed in NEWS_FEEDS:
        try:
            titles = parse_rss_titles(http_get_text(feed, timeout=12), limit=limit)
            if titles:
                return name, titles, errors
        except Exception as e:
            errors.append(f"{name}: {e}")
    return None, [], errors


def ai_summarize(titles, source: str) -> str:
    """Просит нейросеть пересказать заголовки. Если не вышло — вернёт пустую строку."""
    if not titles:
        return ""
    url = ai_endpoint()
    if not url:
        return ""
    try:
        res = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {SETTINGS.get('ai_api_key','')}",
                "Content-Type": "application/json",
            },
            json={
                "model": SETTINGS.get("ai_model", ""),
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты пересказываешь новости. Кратко, по-русски, без выдумок. "
                                   "Не добавляй фактов, которых нет в заголовках.",
                    },
                    {
                        "role": "user",
                        "content": f"Источник: {source}. Заголовки:\n"
                                   + "\n".join(f"- {t}" for t in titles)
                                   + "\n\nСделай короткую сводку (5-7 строк) по этим заголовкам.",
                    },
                ],
                "temperature": 0.3,
                "max_tokens": 400,
            },
            timeout=60,
        )
        if res.status_code == 200:
            data = res.json()
            return ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    except Exception as e:
        logger.error(f"ai_summarize: {e}")
    return ""


def format_news(source: str, titles, summary: str = "") -> str:
    """Собирает текст ответа для чата."""
    lines = [f"📰 Новости — {source}", ""]
    for i, t in enumerate(titles, 1):
        lines.append(f"{i}. {t}")
    if summary:
        lines += ["", "Кратко:", summary.strip()]
    return "\n".join(lines)


# -------------------------------------------------
# БАЗА ДАННЫХ
# -------------------------------------------------
def create_user(name: str, api_key: str):
    try:
        supabase.table("users").insert({
            "name": name,
            "api_key": api_key,
            "tariff": "free",
            "created_at": datetime.datetime.now().isoformat()
        }).execute()
        return True
    except Exception as e:
        logger.error(f"create_user: {e}")
        return False

def get_user(api_key: str):
    try:
        res = supabase.table("users").select("*").eq("api_key", api_key).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"get_user: {e}")
        return None


# -------------------------------------------------
# TELEGRAM-ПОЛЬЗОВАТЕЛИ В ОБЛАКЕ
# -------------------------------------------------
# telegram_users.json жил только на диске. На Render диск не сохраняется между
# перезапусками, и после каждого деплоя бот выдавал старым chat_id новые
# api_key: задания, история и сохранённые сайты «терялись» у пользователя.
# Теперь связка chat_id → api_key лежит в Supabase, файл — резерв.
def get_telegram_user(chat_id: int):
    """Строка telegram_users по chat_id или None (в том числе при сбое)."""
    if not cloud_ready("telegram_users"):
        return None
    try:
        res = supabase.table("telegram_users").select(
            "chat_id,api_key,name").eq("chat_id", str(chat_id)).limit(1).execute()
        return (res.data or [None])[0]
    except Exception as e:
        logger.error(f"telegram_users чтение: {e}")
        return None


def save_telegram_user(chat_id: int, api_key: str, name: str) -> bool:
    """Пишет связку chat_id → api_key в Supabase. False — облака нет."""
    if not cloud_ready("telegram_users"):
        return False
    try:
        supabase.table("telegram_users").upsert({
            "chat_id": str(chat_id),
            "api_key": api_key,
            "name": name,
            "updated_at": datetime.datetime.now().isoformat(),
        }, on_conflict="chat_id").execute()
        return True
    except Exception as e:
        logger.error(f"telegram_users запись: {e}")
        return False

# -------------------------------------------------
# ПАМЯТЬ ЗАДАНИЙ
# -------------------------------------------------
# Задания лежат в локальном файле tasks.json. Раньше они писались в таблицу
# tasks в Supabase, но такой таблицы в проекте нет (PGRST205), поэтому
# добавление задания всегда падало с ошибкой сохранения.
TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.json")
_tasks_lock = threading.RLock()


def _load_tasks() -> dict:
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        # encoding="utf-8-sig" — файл мог быть сохранён с BOM (например,
        # PowerShell Set-Content -Encoding UTF8 или внешний редактор).
        # С обычным "utf-8" json.load падал с "Unexpected UTF-8 BOM", функция
        # возвращала {}, и список заданий выглядел пустым; следующая же запись
        # затирала файл и задания пропадали навсегда.
        with open(TASKS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"tasks read: {e}")
        # НЕ возвращаем {} молча: помечаем сбой, чтобы add_task/complete_task
        # не перезаписали повреждённый файл пустым списком.
        raise


def _write_tasks(data: dict) -> bool:
    try:
        tmp = TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TASKS_FILE)
        return True
    except Exception as e:
        logger.error(f"tasks write: {e}")
        return False


def add_task(api_key: str, task: str) -> bool:
    task = (task or "").strip()
    if not task:
        return False
    entry = {
        "id": uuid.uuid4().hex,
        "api_key": api_key,
        "task": task,
        "status": "active",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if cloud_ready("tasks"):
        try:
            supabase.table("tasks").insert(entry).execute()
            logger.info(f"Задание добавлено в облако: {task[:60]}")
            return True
        except Exception as e:
            logger.error(f"add_task облако: {e}")
    with _tasks_lock:
        try:
            data = _load_tasks()
        except Exception:
            # Файл повреждён/недочитан: не перезаписываем его пустым списком,
            # иначе все прежние задания исчезнут безвозвратно.
            return False
        data.setdefault(api_key, []).append(entry)
        ok = _write_tasks(data)
    if ok:
        logger.info(f"Задание добавлено: {task[:60]}")
    return ok


def get_tasks(api_key: str):
    """Активные задания пользователя, новые сверху."""
    if cloud_ready("tasks"):
        try:
            res = supabase.table("tasks").select(
                "id,task,status,created_at").eq("api_key", api_key).eq(
                "status", "active").order("created_at", desc=True).execute()
            items = res.data or []
            if items:
                return items
            # В облаке пусто — возможно, задания остались в файле с прошлой версии.
            # Показываем их и переносим наверх, чтобы данные не потерялись.
            with _tasks_lock:
                try:
                    local = [t for t in _load_tasks().get(api_key, [])
                             if t.get("status") == "active"]
                except Exception:
                    local = []
            for t in local:
                try:
                    supabase.table("tasks").insert({
                        "id": t.get("id") or uuid.uuid4().hex,
                        "api_key": api_key,
                        "task": t.get("task", ""),
                        "status": "active",
                        "created_at": t.get("created_at") or datetime.datetime.now().isoformat(),
                    }).execute()
                except Exception as e:
                    logger.error(f"Перенос задания в облако: {e}")
            if local:
                logger.info(f"Перенесено заданий в облако: {len(local)}")
                local.sort(key=lambda t: t.get("created_at", ""), reverse=True)
            return local
        except Exception as e:
            logger.error(f"get_tasks облако: {e}")
    with _tasks_lock:
        try:
            items = list(_load_tasks().get(api_key, []))
        except Exception:
            # Битый файл не должен ронять страницу заданий: показываем пусто,
            # но НЕ перезаписываем файл (запись делает только add_task).
            items = []
    active = [t for t in items if t.get("status") == "active"]
    active.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return active


def complete_task(api_key: str, task_id: str) -> bool:
    """Отмечает задание выполненным (оно пропадает из списка активных)."""
    done_at = datetime.datetime.now().isoformat(timespec="seconds")
    if cloud_ready("tasks"):
        try:
            res = supabase.table("tasks").update(
                {"status": "done", "done_at": done_at}).eq(
                "api_key", api_key).eq("id", task_id).execute()
            if res.data:
                logger.info(f"Задание выполнено в облаке: {task_id}")
                return True
        except Exception as e:
            logger.error(f"complete_task облако: {e}")
    with _tasks_lock:
        try:
            data = _load_tasks()
        except Exception:
            # Повреждённый файл не перезаписываем — иначе потеряем все задания.
            return False
        for t in data.get(api_key, []):
            if t.get("id") == task_id:
                t["status"] = "done"
                t["done_at"] = done_at
                ok = _write_tasks(data)
                if ok:
                    logger.info(f"Задание выполнено: {t.get('task', '')[:60]}")
                return ok
    return False

# -------------------------------------------------
# ПЛАНИРОВЩИК ЗАДАНИЙ
# -------------------------------------------------
# Задание хранится строкой «[<метка расписания>] текст», например
# «[1️⃣ Один раз 16.09.2026 в 20:04:17] напиши привет в чате». Раньше метка
# была только украшением: никто не следил за временем, и задание молча
# лежало в списке. Теперь фоновая нить раз в 20 секунд проверяет активные
# задания, выполняет наступившие и складывает ответ в очередь — страница
# чата забирает её через /get_scheduled.
SCHEDULED_FILE = os.path.join(BASE_DIR, "scheduled.json")
_sched_lock = threading.RLock()
_scheduler_started = False

TASK_LABEL_RE = re.compile(r"^\[(.+?)\]\s*(.*)$", re.DOTALL)
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# Названия дней недели в тех языках, которые есть в tasks.html (0 — понедельник).
WEEKDAY_WORDS = {
    "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3,
    "пятница": 4, "суббота": 5, "воскресенье": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
    "lunes": 0, "martes": 1, "miércoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "domingo": 6,
    "segunda": 0, "terça": 1, "quarta": 2, "quinta": 3, "sexta": 4,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3,
    "星期五": 4, "星期六": 5, "星期日": 6,
    "月曜日": 0, "火曜日": 1, "水曜日": 2, "木曜日": 3,
    "金曜日": 4, "土曜日": 5, "日曜日": 6,
    "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2, "गुरुवार": 3,
    "शुक्रवार": 4, "शनिवार": 5, "रविवार": 6,
    "الاثنين": 0, "الثلاثاء": 1, "الأربعاء": 2, "الخميس": 3,
    "الجمعة": 4, "السبت": 5, "الأحد": 6,
}


def _load_scheduled() -> dict:
    if not os.path.exists(SCHEDULED_FILE):
        return {}
    try:
        with open(SCHEDULED_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"scheduled read: {e}")
        return {}


def _write_scheduled(data: dict) -> bool:
    try:
        tmp = SCHEDULED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCHEDULED_FILE)
        return True
    except Exception as e:
        logger.error(f"scheduled write: {e}")
        return False


def push_scheduled_message(api_key: str, text: str, answer: str) -> bool:
    """Кладёт готовое сообщение от планировщика в очередь для страницы чата."""
    with _sched_lock:
        data = _load_scheduled()
        data.setdefault(api_key, []).append({
            "id": uuid.uuid4().hex,
            "text": text,
            "answer": answer,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        return _write_scheduled(data)


def take_scheduled_messages(api_key: str):
    """Забирает и очищает очередь сообщений планировщика для пользователя."""
    with _sched_lock:
        data = _load_scheduled()
        items = data.pop(api_key, []) or []
        if items:
            _write_scheduled(data)
    return items


def _split_task_label(raw: str):
    m = TASK_LABEL_RE.match(raw or "")
    if not m:
        return None, ""
    return m.group(1), (m.group(2) or "").strip()


def _parse_schedule(label: str):
    """Разбирает метку расписания в словарь или возвращает None.

    «Один раз 16.09.2026 в 20:04:17» → дата и время;
    «Еженедельно (Понедельник) в 09:00:00» → день недели и время;
    «Ежедневно в 07:30:00» → только время.
    """
    if not label:
        return None
    tm = TIME_RE.search(label)
    if not tm:
        return None
    hh, mm = int(tm.group(1)), int(tm.group(2))
    ss = int(tm.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    dm = DATE_RE.search(label)
    if dm:
        try:
            date = datetime.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            return None
        return {"kind": "once", "date": date, "hh": hh, "mm": mm, "ss": ss}
    low = label.lower()
    for word, idx in WEEKDAY_WORDS.items():
        if word in low:
            return {"kind": "weekly", "weekday": idx, "hh": hh, "mm": mm, "ss": ss}
    return {"kind": "daily", "hh": hh, "mm": mm, "ss": ss}


def _last_occurrence(sched: dict, now: datetime.datetime) -> datetime.datetime:
    """Ближайший прошедший момент по расписанию (не позже now)."""
    t = datetime.time(sched["hh"], sched["mm"], sched["ss"])
    if sched["kind"] == "daily":
        cand = datetime.datetime.combine(now.date(), t)
        if cand > now:
            cand -= datetime.timedelta(days=1)
        return cand
    days = (now.weekday() - sched["weekday"]) % 7
    cand = datetime.datetime.combine((now - datetime.timedelta(days=days)).date(), t)
    if cand > now:
        cand -= datetime.timedelta(days=7)
    return cand


def _set_task_last_run(api_key: str, task_id: str, when_iso: str) -> bool:
    """Отмечает время последнего запуска (для ежедневных и еженедельных)."""
    if cloud_ready("tasks"):
        try:
            supabase.table("tasks").update({"last_run": when_iso}).eq(
                "api_key", api_key).eq("id", task_id).execute()
            return True
        except Exception as e:
            logger.error(f"last_run облако: {e}")
    with _tasks_lock:
        try:
            data = _load_tasks()
        except Exception:
            return False
        for t in data.get(api_key, []):
            if t.get("id") == task_id:
                t["last_run"] = when_iso
                return _write_tasks(data)
    return False


def _run_scheduled_task(api_key: str, task_id: str, text: str, label: str):
    """Выполняет наступившее задание и кладёт ответ в очередь чата.

    Нейросети задание уходит как прямое поручение: «сделай задание <текст>»,
    чтобы модель не отвечала на него как на обычное сообщение в чате, а
    действительно выполняла его (открыть сайт, проверить дневник и т.п.).
    """
    user = get_user(api_key) or {}
    name = user.get("name") or "пользователь"
    ai_prompt = f"сделай задание {text}".strip()
    # Задание с расписанием тоже попадает в историю чата: иначе в ленте
    # появлялось бы «🔔 …» без строки, к чему это относилось.
    log_chat(api_key, "user", f"⏰ Задание по расписанию: {text}")
    answer = ""
    if ai_ready():
        try:
            # as_task=True: не кэшируем и подставляем инструкцию для поручений.
            answer = ask_ai(name, ai_prompt, api_key, as_task=True) or ""
        except Exception as e:
            logger.error(f"Планировщик, ИИ: {e}")
    if not answer or answer.startswith(("Ошибка ИИ", "Нейросеть не настроена")):
        answer = f"🔔 Напоминание: {text}"
    push_scheduled_message(api_key, text, answer)
    log_chat(api_key, "bot", answer)
    logger.info(f"Задание выполнено по расписанию [{label}]: {text[:60]}")


# Задания выполняются в отдельных нитях: раньше _check_due_tasks вызывала ИИ
# прямо внутри цикла планировщика, и одно долгое поручение (браузер, разбор
# страницы) сдвигало проверку всех остальных заданий. Теперь планировщик
# только «будит» задачу и сразу идёт дальше.
_running_tasks = set()
_running_lock = threading.RLock()


def _spawn_scheduled_task(api_key: str, task_id: str, text: str, label: str):
    """Запускает задание в отдельной нити, не задерживая планировщик.

    Повторный запуск того же задания, пока предыдущий ещё работает,
    пропускается: иначе медленное поручение запускалось бы каждые 20 секунд.
    """
    key = (api_key, task_id)
    with _running_lock:
        if key in _running_tasks:
            logger.info(f"Задание уже выполняется, пропускаю повтор: {task_id}")
            return
        _running_tasks.add(key)

    def _worker():
        try:
            _run_scheduled_task(api_key, task_id, text, label)
        except Exception as e:
            logger.error(f"Задание {task_id} упало: {e}")
        finally:
            with _running_lock:
                _running_tasks.discard(key)

    threading.Thread(target=_worker, name=f"task-{task_id}", daemon=True).start()


def _due_candidates():
    """Все активные задания: из облака, если таблица есть, иначе из файла."""
    items = []
    if cloud_ready("tasks"):
        try:
            rows = supabase.table("tasks").select(
                "id,api_key,task,last_run").eq("status", "active").execute().data or []
            for r in rows:
                items.append((r.get("api_key", ""), r.get("id", ""),
                              r.get("task", ""), r.get("last_run")))
            return items
        except Exception as e:
            logger.error(f"Планировщик, облако: {e}")
    with _tasks_lock:
        try:
            data = _load_tasks()
        except Exception:
            data = {}
    for api_key, tasks in (data or {}).items():
        for t in tasks or []:
            if t.get("status") != "active":
                continue
            items.append((api_key, t.get("id", ""), t.get("task", ""), t.get("last_run")))
    return items


# Задания, про которые уже написали в лог «метка не разобрана»: проверка идёт
# каждые 20 секунд, и без этого пометки сыпались бы бесконечно.
# Множество ограничено по размеру: id выполненных и удалённых заданий из него
# не исчезают, поэтому без подрезки оно росло бы всю жизнь процесса.
_bad_schedule_logged = set()
_BAD_SCHEDULE_LOGGED_LIMIT = 2000


def _mark_bad_schedule(task_id: str) -> bool:
    """True, если про это задание ещё не писали (и запоминает его id).

    Храним только ограниченное число id: если заданий с битой меткой окажется
    очень много, лишняя строка в логе безвредна, а память важнее.
    """
    if not task_id or task_id in _bad_schedule_logged:
        return False
    _bad_schedule_logged.add(task_id)
    if len(_bad_schedule_logged) > _BAD_SCHEDULE_LOGGED_LIMIT:
        for old in list(_bad_schedule_logged)[:_BAD_SCHEDULE_LOGGED_LIMIT // 2]:
            _bad_schedule_logged.discard(old)
    return True


def _check_due_tasks():
    now = datetime.datetime.now()
    for api_key, task_id, raw, last_run in _due_candidates():
        label, text = _split_task_label(raw)
        if not label or not text:
            if _mark_bad_schedule(task_id):
                logger.warning(
                    f"Задание без метки расписания пропущено: {str(raw)[:80]!r}")
            continue
        sched = _parse_schedule(label)
        if not sched:
            # Метка есть, но время в ней не разобрать («[когда-нибудь] …»).
            # Раньше такой случай молча проглатывался: задание не выполнялось,
            # а в логе не было ни строчки — причину приходилось искать вслепую.
            if _mark_bad_schedule(task_id):
                logger.warning(
                    f"Метка расписания не разобрана, задание не выполнится: "
                    f"{label!r} (id {task_id})")
            continue
        if sched["kind"] == "once":
            due = datetime.datetime.combine(
                sched["date"], datetime.time(sched["hh"], sched["mm"], sched["ss"]))
        else:
            due = _last_occurrence(sched, now)
        if due > now:
            continue
        last_dt = None
        if last_run:
            try:
                last_dt = datetime.datetime.fromisoformat(last_run)
            except (TypeError, ValueError):
                last_dt = None
        if last_dt and last_dt >= due:
            continue
        # Отмечаем запуск ДО выполнения: если complete_task/_set_task_last_run
        # не сработают (сбой записи, рестарт сервера посреди работы), задание
        # не будет выполняться заново каждые 20 секунд.
        if sched["kind"] == "once":
            complete_task(api_key, task_id)
        else:
            _set_task_last_run(api_key, task_id, now.isoformat(timespec="seconds"))
        _spawn_scheduled_task(api_key, task_id, text, label)


def _scheduler_loop():
    logger.info("Планировщик заданий запущен (проверка каждые 20 секунд)")
    while True:
        try:
            _check_due_tasks()
        except Exception as e:
            logger.error(f"Планировщик: {e}")
        time.sleep(20)


def start_task_scheduler():
    """Запускает фоновую нить планировщика (вызывается при старте сервера)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, name="task-scheduler", daemon=True).start()


# -------------------------------------------------
# УЧЁТКИ КЛИЕНТОВ (зашифрованные пароли)
# -------------------------------------------------
# Пароли лежат в локальном файле credentials.json в зашифрованном виде.
# Таблицы credentials в Supabase нет (PGRST205), поэтому облако — только
# необязательное зеркало: если запись туда не прошла, сохранение всё равно успешно.
CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
_cred_lock = threading.RLock()


def _load_credentials() -> dict:
    if not os.path.exists(CREDENTIALS_FILE):
        return {}
    try:
        # encoding="utf-8-sig": файл мог быть сохранён с BOM (PowerShell,
        # внешний редактор) — с обычным "utf-8" json.load падал, и сохранённые
        # пароли «пропадали» из интерфейса.
        with open(CREDENTIALS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # Битый файл НЕ затираем: откладываем копию рядом и начинаем с пустого
        # словаря. Иначе следующая же запись (save_credentials) уничтожила бы
        # все сохранённые пароли безвозвратно.
        backup = f"{CREDENTIALS_FILE}.corrupt-{int(time.time())}"
        try:
            os.replace(CREDENTIALS_FILE, backup)
            logger.error(f"credentials read: {e}; битый файл отложен в {backup}")
        except OSError:
            logger.error(f"credentials read: {e}")
        return {}


def _write_credentials(data: dict) -> bool:
    try:
        tmp = CREDENTIALS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CREDENTIALS_FILE)
        return True
    except Exception as e:
        logger.error(f"credentials write: {e}")
        return False


def normalize_site(site: str, url: str = "") -> str:
    """Приводит сайт к домену: из 'https://vk.com/login' получается 'vk.com'."""
    raw = (site or "").strip()
    if not raw and url:
        raw = urllib.parse.urlparse(url).netloc
    if not raw:
        return ""
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).netloc
    raw = raw.split("/")[0].split("?")[0].strip().lower()
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def _cloud_credential_rows(api_key: str):
    """Строки учёток пользователя из Supabase (пустой список при сбое)."""
    try:
        res = supabase.table("credentials").select(
            "site,login,password_enc,url,title,updated_at").eq(
            "api_key", api_key).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"credentials облако: {e}")
        return []


def _migrate_local_credentials(api_key: str) -> None:
    """Переносит учётки из credentials.json в облако (один раз, при первом чтении).

    Нужно, чтобы данные, сохранённые до перехода на Supabase, не потерялись
    при деплое на Render.
    """
    with _cred_lock:
        items = dict(_load_credentials().get(api_key, {}))
    for site, row in items.items():
        try:
            supabase.table("credentials").upsert({
                "api_key": api_key,
                "site": site,
                # login_to_store оставляет готовый шифр как есть и шифрует
                # открытый логин — иначе старые записи уходили бы в облако
                # открытым текстом.
                "login": login_to_store(row.get("login", "")),
                "password_enc": row.get("password_enc", ""),
                "url": row.get("url", ""),
                "title": row.get("title", site),
                "updated_at": row.get("updated_at", ""),
            }, on_conflict="api_key,site").execute()
        except Exception as e:
            logger.error(f"Перенос учётки {site} в облако: {e}")
    if items:
        logger.info(f"Перенесено учёток в облако: {len(items)}")


def save_credentials(api_key: str, site: str, login: str, password: str,
                     url: str = "", title: str = "") -> bool:
    site = normalize_site(site, url)
    if not site or not login:
        return False
    entry = {
        # Логин шифруем так же, как пароль: в базе не должно оставаться
        # открытых учётных данных. Старые записи с открытым логином
        # читаются через login_from_row — перезапись переведёт их в шифр.
        "login": login_to_store(login),
        "password_enc": encrypt_secret(password),
        "url": url or ("https://" + site),
        "title": title or site,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    ok = False
    if cloud_ready("credentials"):
        try:
            supabase.table("credentials").upsert(
                {"api_key": api_key, "site": site, **entry},
                on_conflict="api_key,site",
            ).execute()
            ok = True
            logger.info(f"Учётка сохранена в облако: {site}")
        except Exception as e:
            logger.error(f"save_credentials облако: {e}")
    if not ok:
        with _cred_lock:
            data = _load_credentials()
            data.setdefault(api_key, {})[site] = entry
            ok = _write_credentials(data)
    # Учётку пересохранили — старую cookie входа выбрасываем: иначе агент
    # «входил» по ней, даже если новый пароль неверный.
    drop_site_session(api_key, site)
    return ok


def get_credentials(api_key: str, site: str):
    site = normalize_site(site)
    row = None
    if cloud_ready("credentials"):
        try:
            res = supabase.table("credentials").select(
                "login,password_enc,url,title,updated_at").eq(
                "api_key", api_key).eq("site", site).limit(1).execute()
            rows = res.data or []
            row = rows[0] if rows else None
        except Exception as e:
            logger.error(f"get_credentials облако: {e}")
    if not row:
        with _cred_lock:
            row = _load_credentials().get(api_key, {}).get(site)
    if not row:
        return None
    result = dict(row)
    # Пароль наружу НЕ отдаём: в словаре остаётся только шифртекст
    # (password_enc). Раньше здесь лежала готовая расшифровка
    # result["password"] = decrypt_secret(...), и открытая строка потом
    # путешествовала по коду — через login_to_site, payload формы и любой
    # logger.info(f"...{cred}"), который случайно печатал весь словарь.
    # Теперь расшифровка происходит ровно в одной точке — credential_password().
    result.pop("password", None)
    result["has_password"] = bool(row.get("password_enc"))
    # В базе логин зашифрован (или остался открытым с прошлых версий) —
    # наружу отдаём читаемый вариант.
    result["login"] = login_from_row(row)
    result["site"] = site
    return result


def credential_password(row) -> str:
    """Расшифровывает пароль учётки в последний момент.

    Единственная точка во всём модуле, где шифртекст превращается в открытую
    строку. Живёт отдельно от get_credentials(), чтобы пароль не оседал в
    словарях, которые логируются или уходят в ответ API.
    """
    if not row:
        return ""
    return decrypt_secret(row.get("password_enc", ""))


def list_credentials(api_key: str):
    """Список сохранённых сайтов. Пароли НЕ отдаём — только логин и метаданные."""
    if cloud_ready("credentials"):
        rows = _cloud_credential_rows(api_key)
        if rows:
            return sorted(
                ({
                    "site": r.get("site", ""),
                    "login": login_from_row(r),
                    "url": r.get("url", ""),
                    "title": r.get("title") or r.get("site", ""),
                    "updated_at": r.get("updated_at", ""),
                } for r in rows),
                key=lambda x: x["site"],
            )
        # В облаке пусто — поднимаем локальные учётки, иначе после деплоя на
        # Render они бы просто исчезли из интерфейса.
        _migrate_local_credentials(api_key)
        rows = _cloud_credential_rows(api_key)
        if rows:
            return sorted(
                ({
                    "site": r.get("site", ""),
                    "login": login_from_row(r),
                    "url": r.get("url", ""),
                    "title": r.get("title") or r.get("site", ""),
                    "updated_at": r.get("updated_at", ""),
                } for r in rows),
                key=lambda x: x["site"],
            )
    with _cred_lock:
        items = _load_credentials().get(api_key, {})
    out = []
    for site, row in sorted(items.items()):
        out.append({
            "site": site,
            "login": login_from_row(row),
            "url": row.get("url", ""),
            "title": row.get("title", site),
            "updated_at": row.get("updated_at", ""),
        })
    return out


def delete_credentials(api_key: str, site: str) -> bool:
    site = normalize_site(site)
    ok = False
    if cloud_ready("credentials"):
        try:
            res = supabase.table("credentials").delete().eq(
                "api_key", api_key).eq("site", site).execute()
            ok = bool(res.data)
        except Exception as e:
            logger.error(f"delete_credentials облако: {e}")
    with _cred_lock:
        data = _load_credentials()
        if site in data.get(api_key, {}):
            del data[api_key][site]
            ok = _write_credentials(data) or ok
    drop_site_session(api_key, site)
    return ok

# -------------------------------------------------
# ВХОД НА САЙТЫ ПО СОХРАНЁННЫМ УЧЁТКАМ
# -------------------------------------------------
# Раньше агент честно отвечал «логины и пароли я не ввожу» — то есть сохранённые
# учётки лежали мёртвым грузом. Теперь он умеет: найти нужный сайт по названию,
# открыть страницу входа, разобрать форму, отправить логин с паролем, сохранить
# cookie сессии и прочитать уже закрытую страницу (дневник, оценки и т.п.).
_site_sessions = {}      # (api_key, site) -> requests.Session с cookie входа
_site_lock = threading.RLock()
# Сколько сессий входа держим в памяти. Каждая — это requests.Session со своими
# cookie; без предела словарь рос бы с каждой новой парой «пользователь + сайт»
# и не освобождался бы никогда. Вытесненные сессии просто создадутся заново
# при следующем входе — данные учётки при этом не теряются.
_SITE_SESSIONS_LIMIT = 200

LOGIN_FIELD_HINTS = ("user", "login", "email", "mail", "nick", "phone", "tel",
                     "логин", "почта", "телефон", "имя")
PASS_FIELD_HINTS = ("pass", "pwd", "пароль")
# Признаки самой формы (не полей). На странице входа рядом почти всегда есть
# ещё формы — поиск по сайту, подписка на рассылку, регистрация — и пароль
# бывает не только у формы входа. Раньше брали первую попавшуюся с паролем,
# и логин уходил в «подписаться на новости». Эти подсказки позволяют выбрать.
LOGIN_FORM_HINTS = ("login", "log-in", "log_in", "signin", "sign-in", "sign_in",
                    "logon", "log-on", "authform", "auth-form", "authorization",
                    "вход", "войти", "логин", "авториз")
NON_LOGIN_FORM_HINTS = ("search", "поиск", "subscribe", "subscription", "newsletter",
                        "подпис", "register", "registration", "signup", "sign-up",
                        "sign_up", "регистрац")
LOGIN_VERBS = ("войди", "войти", "зайди", "зайти", "залогинься", "залогиниться",
               "авторизуйся", "авторизоваться", "вход ", "входить")


def _response_text(res) -> str:
    """Текст ответа с правильной кодировкой (как в http_get_text)."""
    if not res.encoding or res.encoding.lower() in ("iso-8859-1", "ascii"):
        res.encoding = res.apparent_encoding or "utf-8"
    return res.text


# -------------------------------------------------
# БРАУЗЕР И ЗАЩИТА ОТ РОБОТОВ
# -------------------------------------------------
# requests скачивает только «голый» HTML. Школьные дневники (МЭШ, Дневник.ру),
# банки и крупные сайты закрыты Cloudflare / SmartCaptcha, написаны на React и
# уводят вход на Госуслуги (ЕСИА). Простой GET в таких случаях возвращает не
# данные, а страницу проверки — и агент раньше молча показывал её пользователю
# как «дневник». Здесь мы это распознаём и либо рендерим страницу настоящим
# браузером (если он доступен), либо честно объясняем, что нужен ручной вход.

BROWSER_TIMEOUT_MS = 20000

# Признаки страниц-заглушек: ищем и по разметке, и по тексту.
BOT_WALL_MARKERS = (
    "cf-challenge", "cf_chl_", "cloudflare", "checking your browser",
    "подтвердите, что вы человек", "подтвердите что вы человек",
    "я не робот", "i'm not a robot", "i am not a robot",
    "smartcaptcha", "smart-captcha", "recaptcha", "g-recaptcha",
    "hcaptcha", "turnstile",
)
SMS_WALL_MARKERS = (
    "код из смс", "код подтверждения", "введите код", "введите код из",
    "подтвердите вход", "двухфакторн", "одноразовый код",
)
ESIA_MARKERS = (
    "esia.gosuslugi.ru", "госуслуги", "есиа",
    "единая система идентификации",
)
SPA_ROOT_IDS = ("root", "app", "__next", "___gatsby", "q-app")

_browser_state = {"checked": False, "ok": False, "error": ""}
_browser_lock = threading.RLock()


def _browser_error_text(exc) -> str:
    """Понятное объяснение, почему браузер не запустился."""
    if isinstance(exc, PermissionError) or "WinError 5" in str(exc):
        return ("браузер Chromium не может запуститься: системе запрещено "
                "создавать дочерние процессы (WinError 5 — отказ в доступе)")
    if isinstance(exc, ImportError):
        return "модуль playwright не установлен"
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
        return "браузер Chromium не скачан: выполни «playwright install chromium»"
    return f"{type(exc).__name__}: {exc}"


def browser_available() -> bool:
    """Проверяет один раз, можно ли поднять настоящий браузер.

    Раньше Playwright вызывался прямо в обработчике и падал с PermissionError,
    который никто не ловил: в лог уходило «Future exception was never
    retrieved», а пользователь получал пустую страницу вместо ответа. Теперь
    доступность проверяется заранее, результат кэшируется, а причина сбоя
    показывается пользователю.
    """
    with _browser_lock:
        if _browser_state["checked"]:
            return _browser_state["ok"]
        _browser_state["checked"] = True
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            _browser_state["error"] = _browser_error_text(e)
            logger.warning(f"Playwright недоступен: {_browser_state['error']}")
            return False
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            _browser_state["ok"] = True
            logger.info("Playwright: браузер доступен")
        except Exception as e:
            _browser_state["error"] = _browser_error_text(e)
            logger.warning(f"Playwright не запускается: {_browser_state['error']}")
        return _browser_state["ok"]


def browser_status() -> str:
    """Текстовая причина, по которой браузер недоступен (пусто — доступен)."""
    return _browser_state["error"]


def cookies_for_playwright(cookies):
    """Приводит куки к тому виду, который принимает Playwright.

    requests держит куки в RequestsCookieJar (объекты http.cookiejar.Cookie),
    а Playwright понимает только список словарей: {"name", "value", "url"}
    либо {"name", "value", "domain", "path"}. Если отдать ему CookieJar,
    add_cookies падает в warning и сессия входа теряется — поэтому формат
    приводим явно, а не надеемся на «похоже на список».
    """
    if not cookies:
        return []

    # Уже список словарей — проверяем, что у каждого есть имя, значение и адрес.
    if isinstance(cookies, (list, tuple)):
        out = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name, value = c.get("name"), c.get("value")
            if not name or value is None:
                continue
            item = {"name": str(name), "value": str(value)}
            if c.get("url"):
                item["url"] = str(c["url"])
            elif c.get("domain"):
                item["domain"] = str(c["domain"])
                item["path"] = str(c.get("path") or "/")
            else:
                # Без url и без domain Playwright такую куку не примет.
                continue
            out.append(item)
        return out

    # requests.CookieJar / http.cookiejar.CookieJar: у куки есть domain/path.
    out = []
    try:
        jar = list(cookies)
    except TypeError:
        logger.warning(f"Куки неизвестного формата: {type(cookies).__name__}")
        return []
    for c in jar:
        name = getattr(c, "name", None)
        value = getattr(c, "value", None)
        domain = (getattr(c, "domain", "") or "").strip()
        if not name or value is None or not domain:
            continue
        out.append({
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(getattr(c, "path", "") or "/"),
        })
    return out


def render_page(url, cookies=None, wait_ms=4000, timeout_ms=BROWSER_TIMEOUT_MS):
    """Открывает страницу настоящим браузером и отдаёт готовый HTML.

    Возвращает (html, итоговый_url) или (None, причина). Нужен для SPA
    (React/Vue/Angular), где формы входа и оценки дорисовывает JavaScript, и
    для сайтов, которые отдают данные только «живому» браузеру.
    """
    if not browser_available():
        return None, _browser_state["error"]
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                locale="ru-RU",
                # Случайный UA и на каждый запуск браузера: постоянный
                # «Chrome/124» на всех запросах — готовый отпечаток робота.
                user_agent=random_user_agent(),
                viewport={"width": 1366, "height": 900},
            )
            jar = cookies_for_playwright(cookies)
            if jar:
                try:
                    context.add_cookies(jar)
                except Exception as e:
                    logger.warning(f"Куки не приняты браузером: {e}")
            page = context.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=wait_ms)
            except Exception:
                # Долгие фоновые опросы на странице — не ошибка.
                pass
            html = page.content()
            final_url = page.url
            browser.close()
        return html, final_url
    except Exception as e:
        reason = _browser_error_text(e)
        logger.error(f"render_page {url}: {reason}")
        return None, reason


def _looks_like_spa_shell(low_html: str) -> bool:
    """Похоже ли, что это пустой каркас SPA, а не готовая страница."""
    if "<form" in low_html or "password" in low_html:
        return False
    squeezed = low_html.replace(" ", "")
    quote = chr(34)
    for root_id in SPA_ROOT_IDS:
        if ("id=" + quote + root_id + quote + "></div>") in squeezed:
            return True
    body = re.sub(r"<script.*?</script>", " ", low_html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    words = [w for w in body.split() if len(w) > 2]
    return len(words) < 15 and low_html.count("<script") >= 1


def page_block_reason(html: str, url: str = "") -> str:
    """Почему страницу нельзя считать данными ("" — всё в порядке).

    Готовая фраза для пользователя: вместо страницы проверки Cloudflare или
    пустого шаблона React агент объясняет, что именно произошло.
    """
    low = (html or "").lower()
    if not low.strip():
        return "страница пришла пустой"
    if any(m in low for m in BOT_WALL_MARKERS):
        return ("сайт отдал страницу проверки «вы не робот» (Cloudflare / капча). "
                "Автоматически такую защиту не пройти — нужен вход из обычного браузера.")
    if "esia.gosuslugi.ru" in low or any(m in low for m in ESIA_MARKERS):
        return ("вход уводит на Госуслуги (ЕСИА): нужны подтверждение в приложении "
                "и код. Автоматически войти нельзя — войди вручную в браузере.")
    if any(m in low for m in SMS_WALL_MARKERS):
        return ("сайт просит код из СМС или подтверждение входа. Этот шаг проходит "
                "только вручную — автоматически его не выполнить.")
    if url and _looks_like_spa_shell(low):
        return ("страница пустая, потому что её дорисовывает JavaScript "
                "(React/Vue/Angular). Обычный запрос видит только шаблон.")
    return ""


def plain_page_text(url: str, limit: int = 3000) -> str:
    """Читает страницу без входа: requests, при защите/SPA — настоящий браузер.

    Возвращает готовый текст или понятное объяснение, почему данных нет.
    """
    try:
        html = http_get_text(url, timeout=20)
    except Exception as e:
        logger.error(f"plain_page_text {url}: {e}")
        return f"❌ Не удалось открыть {url}: {e}"
    reason = page_block_reason(html, url)
    if reason:
        rendered, final_url = render_page(url)
        if rendered:
            html = rendered
            url = final_url or url
            reason = page_block_reason(html, url)
        if reason:
            return f"🚧 {url}: {reason}"
    text = page_to_text(html, limit=limit)
    if not text:
        return (f"🚧 {url}: на странице нет текста — вероятно, всё рисует "
                "JavaScript. Нужен настоящий браузер (playwright).")
    return text


# -------------------------------------------------
# СОХРАНЕНИЕ СЕССИЙ ВХОДА В ОБЛАКЕ
# -------------------------------------------------
# Cookie входа жили только в RAM. Контейнер на Render пересоздаётся при каждом
# деплое и засыпает после 15 минут простоя, поэтому после каждого такого
# перезапуска агент заново и медленно логинился на все сайты, а пользователь
# ждал. Кладём cookie в ту же таблицу app_settings, что и настройки, но
# обязательно зашифрованными: cookie — это учётные данные, и в открытом виде
# им в базе не место. Ключ Fernet лежит отдельно от Supabase (fernet.key или
# переменная FERNET_KEY), так что одной утечки базы для входа не хватит.
_SITE_SESSION_ROW_PREFIX = "site_session:"


def _site_session_row_id(api_key: str, site: str) -> str:
    """id строки в app_settings для пары «пользователь + сайт»."""
    return f"{_SITE_SESSION_ROW_PREFIX}{api_key}:{site}"


def _cookies_to_list(jar) -> list:
    """Cookie из requests в список словарей — только то, что нужно для входа.

    Берём name/value/domain/path. Без домена cookie бессмысленна: requests не
    подставит её ни к одному запросу, поэтому такие пропускаем.
    """
    out = []
    try:
        items = list(jar)
    except TypeError:
        return out
    for c in items:
        name = getattr(c, "name", None)
        value = getattr(c, "value", None)
        domain = (getattr(c, "domain", "") or "").strip()
        if not name or value is None or not domain:
            continue
        out.append({
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(getattr(c, "path", "") or "/"),
        })
    return out


def save_site_session(api_key: str, site: str, session) -> bool:
    """Кладёт cookie входа в облако. Best-effort: сбой не должен ломать вход.

    Если базы нет или cookie не сохранились — просто возвращаем False, вход уже
    состоялся и работает как раньше, только до перезапуска контейнера.
    """
    if not (api_key and site and session is not None):
        return False
    if not cloud_ready("app_settings"):
        return False
    cookies = _cookies_to_list(getattr(session, "cookies", None))
    if not cookies:
        return False
    try:
        token = encrypt_secret(json.dumps(cookies, ensure_ascii=False))
        supabase.table("app_settings").upsert({
            "id": _site_session_row_id(api_key, site),
            "data": {"cookies": token},
            "updated_at": datetime.datetime.now().isoformat(),
        }).execute()
        return True
    except Exception as e:
        logger.warning(f"save_site_session {site}: {e}")
        return False


def forget_site_session(api_key: str, site: str = "") -> None:
    """Убирает сохранённые cookie из облака. Best-effort.

    Нужно при смене пароля и удалении учётки: иначе агент поднял бы из облака
    старую сессию и «успешно вошёл» с уже неверным паролем.
    """
    if not cloud_ready("app_settings"):
        return
    try:
        if site:
            supabase.table("app_settings").delete().eq(
                "id", _site_session_row_id(api_key, site)).execute()
            return
        # Все сайты пользователя: сначала находим строки по префиксу, потом
        # удаляем поштучно — так не зависим от поддержки фильтров в delete().
        res = supabase.table("app_settings").select("id").like(
            "id", f"{_SITE_SESSION_ROW_PREFIX}{api_key}:%").execute()
        for row in (res.data or []):
            row_id = row.get("id")
            if row_id:
                supabase.table("app_settings").delete().eq("id", row_id).execute()
    except Exception as e:
        logger.warning(f"forget_site_session {site or api_key}: {e}")


def _restore_site_cookies(api_key: str, site: str, session) -> int:
    """Достаёт cookie из облака и подставляет в сессию. Возвращает их число.

    Любая ошибка (нет базы, ключ не тот, мусор в строке) означает просто «не
    восстановили» — вызывающий код дальше войдёт обычным путём.
    """
    if not cloud_ready("app_settings"):
        return 0
    try:
        res = supabase.table("app_settings").select("data").eq(
            "id", _site_session_row_id(api_key, site)).limit(1).execute()
        rows = res.data or []
        data = rows[0].get("data") if rows else None
        if isinstance(data, str):
            data = json.loads(data)
        token = data.get("cookies") if isinstance(data, dict) else None
        if not token:
            return 0
        raw = decrypt_secret(token)
        if not raw:
            return 0
        cookies = json.loads(raw)
    except Exception as e:
        logger.warning(f"restore_site_session {site}: {e}")
        return 0
    if not isinstance(cookies, list):
        return 0
    restored = 0
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name, value = c.get("name"), c.get("value")
        domain = (c.get("domain") or "").strip()
        if not name or value is None or not domain:
            continue
        try:
            session.cookies.set(str(name), str(value), domain=domain,
                                path=str(c.get("path") or "/"))
            restored += 1
        except Exception:
            continue
    return restored


def get_site_session(api_key: str, site: str):
    """Сессия с cookie входа для пары «пользователь + сайт».

    При промахе кэша пробуем поднять cookie из облака. Запрос к базе делаем ВНЕ
    _site_lock: внутри него сетевой вызов остановил бы всех, кто ждёт сессию.
    """
    key = (api_key, site)
    with _site_lock:
        session = _site_sessions.get(key)
    if session is not None:
        return session
    # Случайный UA и на сессию входа: постоянный «Chrome/124» на всех
    # запросах — заметный признак робота (см. USER_AGENTS).
    fresh = requests.Session()
    fresh.headers.update(browser_headers())
    restored = _restore_site_cookies(api_key, site, fresh)
    if restored:
        logger.info(f"Сессия {site}: восстановлено cookie из облака — {restored}")
    with _site_lock:
        session = _site_sessions.get(key)
        if session is None:
            session = fresh
            _site_sessions[key] = session
            if len(_site_sessions) > _SITE_SESSIONS_LIMIT:
                # Вытесняем самые старые входы: cookie можно получить заново.
                for old in list(_site_sessions)[:-_SITE_SESSIONS_LIMIT]:
                    _site_sessions.pop(old, None)
    return session


def drop_site_session(api_key: str, site: str = "") -> None:
    """Забывает сохранённую cookie входа — и в памяти, и в облаке.

    Нужно при пересохранении учётки: иначе после смены пароля агент продолжал
    ходить по старой сессии и «успешно входил» с неверным паролем. В облаке
    cookie надо убрать тоже: иначе после перезапуска агент поднял бы оттуда
    ту самую старую сессию и обошел бы сброс.
    """
    with _site_lock:
        if site:
            _site_sessions.pop((api_key, site), None)
        else:
            for key in [k for k in _site_sessions if k[0] == api_key]:
                _site_sessions.pop(key, None)
    # Сетевой вызов — намеренно вне _site_lock.
    forget_site_session(api_key, site)


def find_saved_site(api_key: str, query: str):
    """Ищет сохранённую учётку по названию, домену или ссылке.

    «Войди в дневник» → сюда попадает вся фраза; слова-команды («войди»)
    в названиях сайтов не встречаются, поэтому оценка по совпавшим словам
    сама выбирает нужную учётку.
    """
    sites = list_credentials(api_key)
    if not sites:
        return None
    words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) >= 3]
    best, best_score = None, 0
    for item in sites:
        haystack = " ".join([
            str(item.get("title", "")), str(item.get("site", "")),
            str(item.get("login", "")), str(item.get("url", "")),
        ]).lower()
        score = sum(1 for w in words if w in haystack)
        if score > best_score:
            best, best_score = item, score
    if best:
        return best
    # Ничего не совпало, но учётка всего одна — берём её: чаще всего имели в виду её.
    return sites[0] if len(sites) == 1 else None


def find_credential_for_url(api_key: str, url: str):
    """Сохранённая учётка для домена ссылки (или None).

    Домен сравниваем по границе метки: раньше хватало простого endswith, и
    учётка от «notexample.com» подходила к «example.com» — агент отправлял
    чужой логин с паролем на посторонний сайт.
    """
    site = normalize_site("", url)
    if not site:
        return None
    for item in list_credentials(api_key):
        saved = (item.get("site") or "").strip().lower()
        if not saved:
            continue
        if site == saved or site.endswith("." + saved):
            return item
    return None


def parse_login_forms(html: str, base_url: str):
    """Достаёт из страницы формы входа.

    Возвращает список словарей: адрес отправки, метод, имена полей логина и
    пароля, а также скрытые поля (csrf-токены), которые надо отправить вместе
    с логином, иначе форма не примет пароль.
    """
    soup = BeautifulSoup(html, "html.parser")
    forms = []
    for form in soup.find_all("form"):
        inputs = form.find_all("input")
        pass_field = ""
        pass_autocomplete = ""
        login_field = ""
        for inp in inputs:
            itype = (inp.get("type") or "text").lower()
            if itype in ("hidden", "submit", "button", "checkbox", "radio", "image", "file", "reset"):
                continue
            name = (inp.get("name") or inp.get("id") or "").strip()
            if not name:
                continue
            low = " ".join([
                name, str(inp.get("placeholder") or ""), str(inp.get("autocomplete") or ""),
            ]).lower()
            if not pass_field and (itype == "password" or any(h in low for h in PASS_FIELD_HINTS)):
                pass_field = name
                pass_autocomplete = str(inp.get("autocomplete") or "").lower()
            elif not login_field and any(h in low for h in LOGIN_FIELD_HINTS):
                login_field = name
        if not pass_field:
            continue
        if not login_field:
            for inp in inputs:
                itype = (inp.get("type") or "text").lower()
                if itype in ("text", "email", "tel"):
                    login_field = (inp.get("name") or inp.get("id") or "").strip()
                    if login_field:
                        break
        if not login_field:
            continue
        # Поля, которые браузер отправил бы вместе с формой. Раньше сюда
        # попадало ВСЁ, кроме кнопок: галочка «запомнить меня» уезжала на сервер
        # как отмеченная, даже когда пользователь её не ставил, а некоторые
        # формы из-за лишнего поля отказывались принимать пароль.
        hidden = {}
        for inp in inputs:
            name = (inp.get("name") or "").strip()
            itype = (inp.get("type") or "text").lower()
            if not name or name in (login_field, pass_field):
                continue
            if itype in ("submit", "button", "image", "file", "reset"):
                # Браузер отправляет только нажатую кнопку, а её имя в payload
                # ломает часть форм (сайт видит две отправки).
                continue
            if itype in ("checkbox", "radio"):
                # Отмеченные — отправляем, не отмеченные — нет: так делает
                # браузер. unchecked-галочка в payload превращалась в «on».
                if inp.has_attr("checked"):
                    hidden[name] = inp.get("value") or "on"
                continue
            hidden[name] = inp.get("value") or ""
        action = form.get("action") or base_url
        # Какую из форм отправлять, если на странице их несколько (а так бывает
        # почти всегда: поиск, подписка, регистрация). Раньше брали первую
        # попавшуюся с паролем — и логин уходил в форму «регистрация» или
        # «подписаться на рассылку». Теперь форма с признаками входа получает
        # плюс, а заведомо не-входовая — минус. При равенстве баллов max()
        # возвращает первую, то есть прежний порядок сохраняется.
        attrs_low = " ".join([
            str(form.get("name") or ""), str(form.get("id") or ""),
            str(form.get("class") or ""), str(form.get("action") or ""),
            str(form.get("aria-label") or ""), str(form.get("title") or ""),
        ]).lower()
        score = 0
        if any(h in attrs_low for h in LOGIN_FORM_HINTS):
            score += 3
        if any(h in attrs_low for h in NON_LOGIN_FORM_HINTS):
            score -= 2
        if pass_autocomplete == "current-password":
            score += 2
        elif pass_autocomplete == "new-password":
            score -= 2
        forms.append({
            "action": urllib.parse.urljoin(base_url, action),
            "method": (form.get("method") or "post").upper(),
            "login_field": login_field,
            "pass_field": pass_field,
            "extra": hidden,
            "score": score,
        })
    return forms


# Явные сообщения об ошибке входа. Нужны потому, что часть сайтов (особенно
# SPA) после неверного пароля НЕ рисует форму заново, а показывает текст
# «неверный пароль». Без этой проверки такая страница выглядела как успешный
# вход: поля пароля на ней нет, значит «вошли» — и пользователь получал
# страницу с ошибкой под видом данных дневника.
LOGIN_ERROR_MARKERS = (
    "неверный пароль", "неправильный пароль", "неверный логин",
    "неправильный логин", "неверное имя пользователя", "ошибка входа",
    "incorrect password", "invalid password", "wrong password",
    "invalid credentials", "incorrect username", "invalid login",
    "login failed", "authentication failed", "не удалось войти",
)


def _looks_logged_in(html: str) -> bool:
    """Вход удался, если формы с паролем больше нет и нет текста об ошибке."""
    if any(f["pass_field"] for f in parse_login_forms(html, "")):
        return False
    low = (html or "").lower()
    return not any(marker in low for marker in LOGIN_ERROR_MARKERS)


def login_to_site(api_key: str, query: str = "", url: str = "", follow: str = ""):
    """Входит на сайт по сохранённой учётке.

    Возвращает (текст_страницы, название_сайта, ошибка). Если ошибка пустая —
    вход выполнен и текст страницы уже доступен.
    """
    item = None
    if url:
        item = find_credential_for_url(api_key, url)
    if not item:
        item = find_saved_site(api_key, query)
    if not item:
        sites = list_credentials(api_key)
        if not sites:
            return "", "", ("🔐 Учётка для этого сайта не сохранена. Открой страницу «Сайты» "
                            "в чате и добавь ссылку, логин и пароль — тогда я смогу войти сам.")
        names = ", ".join(s.get("title") or s.get("site") for s in sites)
        return "", "", f"🤔 Не понял, на какой сайт входить. Сохранены: {names}"

    site = item.get("site", "")
    cred = get_credentials(api_key, site)
    # Пароль больше не лежит в словаре: get_credentials отдаёт только шифртекст
    # и признак has_password. Открытое значение рождается ниже — прямо в payload.
    if not cred or not cred.get("has_password"):
        return "", site, (f"🔐 Для «{item.get('title') or site}» сохранён только логин — "
                          "пароль не читается. Пересохрани учётку.")

    entry_url = cred.get("url") or ("https://" + site)
    session = get_site_session(api_key, site)
    try:
        res = session.get(entry_url, timeout=25)
        html = _response_text(res)
        current_url = res.url
    except Exception as e:
        logger.error(f"login get {entry_url}: {e}")
        return "", site, f"❌ Не удалось открыть {entry_url}: {e}"

    forms = parse_login_forms(html, current_url)
    if not forms:
        # Формы нет — возможно, cookie ещё жива с прошлого раза. Но это же
        # бывает и когда сайт вместо страницы отдал заглушку: капчу Cloudflare,
        # запрос кода из СМС или пустой каркас SPA. Раньше здесь стоял return
        # без проверки, и такая заглушка уходила пользователю как «данные
        # дневника» — ровно то, от чего защищает page_block_reason().
        reason = page_block_reason(html, current_url)
        if reason:
            logger.warning(f"Страница закрыта ещё до входа: {site} — {reason}")
            return "", site, f"🚧 «{item.get('title') or site}»: {reason}"
        page = page_to_text(html, limit=3000)
        return page, site, ""

    # Не обязательно первая форма с паролем: на странице их бывает несколько,
    # и «первая» часто оказывается поиском или подпиской. Берём самую похожую
    # на вход по атрибутам формы (см. parse_login_forms). При равенстве баллов
    # max() возвращает первую — поведение как раньше.
    form = max(forms, key=lambda f: f.get("score", 0))
    payload = dict(form["extra"])
    payload[form["login_field"]] = cred["login"]
    # Единственное место, где шифртекст превращается в пароль: сразу уходит
    # в payload и нигде дальше в открытом виде не хранится.
    payload[form["pass_field"]] = credential_password(cred)
    try:
        if form["method"] == "GET":
            res = session.get(form["action"], params=payload, timeout=25)
        else:
            res = session.post(form["action"], data=payload, timeout=25,
                               headers={"Referer": current_url,
                                        "Content-Type": "application/x-www-form-urlencoded"})
        html = _response_text(res)
    except Exception as e:
        logger.error(f"login post {form['action']}: {e}")
        return "", site, f"❌ Ошибка при отправке логина: {e}"

    if not _looks_logged_in(html):
        logger.warning(f"Вход не удался: {site}")
        return "", site, (f"❌ «{item.get('title') or site}»: логин или пароль не подошли "
                          "(сайт снова показал форму входа). Проверь учётку в «Сайтах».")

    # Если попросили конкретную страницу (например /diary) — идём на неё.
    if follow:
        try:
            target = urllib.parse.urljoin(res.url, follow)
            res = session.get(target, timeout=25)
            html = _response_text(res)
        except Exception as e:
            logger.error(f"login follow {follow}: {e}")

    # Даже при удачном входе сайт может показать капчу/СМС или пустой шаблон SPA —
    # тогда честно объясняем, а не отдаём заглушку как «данные дневника».
    reason = page_block_reason(html, res.url if hasattr(res, "url") else entry_url)
    if reason:
        logger.warning(f"После входа страница закрыта: {site} — {reason}")
        return "", site, f"🚧 «{item.get('title') or site}»: {reason}"

    # Логин в журнал не пишем: строка лога — не место для учётных данных.
    logger.info(f"Вход выполнен: {site}")
    # Cookie входа — в облако, чтобы следующий перезапуск контейнера не заставлял
    # пользователя ждать повторного входа. Best-effort: сбой сохранения не влияет
    # на только что выполненный вход.
    save_site_session(api_key, site, session)
    return page_to_text(html, limit=3000), site, ""


def open_as_user(api_key: str, url: str):
    """Открывает ссылку. Если для домена есть учётка — сначала входит.

    Возвращает (текст, признак_входа). Признак нужен, чтобы в ответе честно
    написать «вошёл как …» вместо «вот страница входа».
    """
    item = find_credential_for_url(api_key, url)
    if not item:
        # Без учётки читаем как обычную страницу — с проверкой на капчу и SPA.
        return plain_page_text(url, limit=3000), False
    text, _site, err = login_to_site(api_key, url=url)
    if err or not text:
        return "", False
    # После входа читаем именно запрошенную страницу, а не корень сайта.
    try:
        session = get_site_session(api_key, item["site"])
        res = session.get(url, timeout=25)
        text = page_to_text(_response_text(res), limit=3000)
    except Exception as e:
        logger.error(f"open_as_user {url}: {e}")
    return text, True


def saved_sites_hint(api_key: str) -> str:
    """Строка со сохранёнными сайтами — её подмешиваем в системный промпт.

    Без этого ИИ не знал адресов и отвечал «пришли ссылку», хотя ссылка
    уже была сохранена в «Сайтах».
    """
    sites = list_credentials(api_key)
    if not sites:
        return ""
    lines = [f"- {s.get('title') or s.get('site')}: {s.get('url')} (логин {s.get('login')})"
             for s in sites]
    return ("Сохранённые сайты пользователя (вход по ним я выполняю сам, логин и пароль "
            "пользователю вводить не нужно):\n" + "\n".join(lines))

# -------------------------------------------------
# ОЧЕРЕДЬ
# -------------------------------------------------
def enqueue_message(api_key: str, text: str):
    # Без таблицы в облаке каждый запрос писал ошибку в лог. Проверяем заранее.
    if not cloud_ready("message_queue"):
        return False
    try:
        supabase.table("message_queue").insert({
            "api_key": api_key,
            "text": text,
            "status": "new",
            "created_at": datetime.datetime.now().isoformat()
        }).execute()
        return True
    except Exception as e:
        logger.error(f"enqueue: {e}")
        return False

def dequeue_messages(api_key: str):
    try:
        res = supabase.table("message_queue").select("*").eq("api_key", api_key).eq("status", "done").execute()
        for item in res.data or []:
            supabase.table("message_queue").update({"status": "read"}).eq("id", item["id"]).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"dequeue: {e}")
        return []

def clean_old_messages():
    """Удаляет из очереди реплики старше недели.

    Раньше здесь не было проверки cloud_ready, и при выключенном Supabase
    (нет ключей или нет таблицы) функция падала с «'NoneType' object has no
    attribute 'table'». Это единственная облачная функция без такой защиты,
    поэтому именно она и сыпала ошибкой в лог при каждом запуске.
    """
    if not cloud_ready("message_queue"):
        logger.info("Очередь сообщений не в облаке — чистить нечего")
        return
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        supabase.table("message_queue").delete().lt("created_at", cutoff).execute()
        logger.info("Очищены старые сообщения")
    except Exception as e:
        logger.error(f"clean_old: {e}")

# -------------------------------------------------
# МОЗГИ
# -------------------------------------------------
def ai_endpoint():
    """Собирает полный URL /chat/completions из настроенного пути."""
    base = (SETTINGS.get("ai_base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


# -------------------------------------------------
# ИНСТРУМЕНТЫ ДЛЯ ИИ (function calling)
# -------------------------------------------------
# Раньше ИИ получал только текст: ссылку открывала регулярка detect_command,
# разбирая сообщение пользователя. Теперь модель сама решает, какой инструмент
# вызвать, и получает результат обратно.
AI_TOOL_PROMPT = (
    "У тебя есть инструменты. Если пользователь просит открыть ссылку, скачать файл, "
    "войти на сайт, показать новости, узнать погоду, найти что-то в интернете, "
    "поставить напоминание, добавить или закрыть задание — ВЫЗЫВАЙ инструмент, "
    "а не отвечай словами «сейчас посмотрю». После ответа инструмента перескажи суть по-русски. "
    "В ответе пиши ТОЛЬКО готовый ответ пользователю: без рассуждений, без планов "
    "и без пересказа своих мыслей вроде «user is asking…», «I should…», «just answer»."
)

# Отдельная инструкция для заданий по расписанию. Задание приходит не как
# сообщение в чате, а как поручение «сделай задание <текст>»: модель должна
# именно выполнить его (открыть сайт, проверить дневник), а не просто ответить.
AI_TASK_PROMPT = (
    "Это не сообщение в чате, а задание по расписанию: «сделай задание …». "
    "Выполни его прямо сейчас — сам вызови нужные инструменты (открой сайт, "
    "войди по сохранённой учётке, посмотри новости, закрой задание) и коротко "
    "отчитайся по-русски, что сделал и что получилось. Не спрашивай разрешения "
    "и не проси прислать ссылку, если адрес сайта есть в сохранённых сайтах."
)

# -------------------------------------------------
# ПОГОДА, ПОИСК, ПАМЯТЬ ДИАЛОГА И НАПОМИНАНИЯ
# -------------------------------------------------
# Погода берётся из бесплатного open-meteo, поиск — из HTML-выдачи DuckDuckGo.
# Оба сервиса работают без ключей и регистрации, поэтому одинаково годятся и
# для запуска на своём компьютере, и для Render.

WEATHER_CODES = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось",
    55: "сильная морось", 61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливни", 81: "ливни", 82: "сильные ливни", 85: "снегопад",
    86: "сильный снегопад", 95: "гроза", 96: "гроза с градом",
    99: "сильная гроза с градом",
}


def _geocode_city(city: str):
    """Название города → (широта, долгота, подпись). Ищет через open-meteo."""
    res = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "ru", "format": "json"},
        timeout=15,
    )
    res.raise_for_status()
    rows = (res.json() or {}).get("results") or []
    if not rows:
        return None
    row = rows[0]
    label = ", ".join(x for x in (row.get("name"), row.get("country")) if x)
    return float(row["latitude"]), float(row["longitude"]), label


def get_weather(city: str) -> str:
    """Погода сейчас и прогноз на сегодня с завтрашним днём."""
    city = (city or "").strip() or "Москва"
    try:
        place = _geocode_city(city)
        if not place:
            return f"Не нашёл город «{city}»"
        lat, lon, label = place
        res = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": ("temperature_2m_max,temperature_2m_min,"
                          "weather_code,precipitation_probability_max"),
                "timezone": "auto", "forecast_days": 2,
            },
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        days = daily.get("time") or []
        lines = [f"Погода: {label}"]
        if cur:
            lines.append(
                f"Сейчас {cur.get('temperature_2m')}°C, "
                f"{WEATHER_CODES.get(cur.get('weather_code'), 'без осадков')}, "
                f"ветер {cur.get('wind_speed_10m')} м/с"
            )
        names = ("Сегодня", "Завтра")
        for i, day in enumerate(days[:2]):
            code = (daily.get("weather_code") or [None])[i]
            tmax = (daily.get("temperature_2m_max") or [None])[i]
            tmin = (daily.get("temperature_2m_min") or [None])[i]
            rain = (daily.get("precipitation_probability_max") or [None])[i]
            title = names[i] if i < len(names) else day
            line = f"{title} ({day}): {tmin}…{tmax}°C, {WEATHER_CODES.get(code, 'без осадков')}"
            if rain is not None:
                line += f", дождь {rain}%"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_weather {city}: {e}")
        return f"Не удалось узнать погоду: {e}"


def web_search(query: str, limit: int = 5) -> str:
    """Ищет в интернете через HTML-выдачу DuckDuckGo — без ключа и регистрации."""
    query = (query or "").strip()
    if not query:
        return "Пустой поисковый запрос"
    try:
        res = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "ru-ru"},
            headers=browser_headers(),
            timeout=20,
        )
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        out = []
        for block in soup.select(".result"):
            link = block.select_one(".result__a")
            if not link:
                continue
            title = link.get_text(" ", strip=True)
            href = link.get("href") or ""
            if href.startswith("//duckduckgo.com/l/"):
                parsed = urllib.parse.urlparse("https:" + href)
                href = urllib.parse.parse_qs(parsed.query).get("uddg", [href])[0]
            snippet_el = block.select_one(".result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            out.append(f"{len(out) + 1}. {title}\n{href}\n{snippet}")
            if len(out) >= max(1, min(int(limit or 5), 10)):
                break
        if not out:
            return f"По запросу «{query}» ничего не нашлось"
        return f"Результаты поиска «{query}»:\n\n" + "\n\n".join(out)
    except Exception as e:
        logger.error(f"web_search {query}: {e}")
        return f"Поиск не удался: {e}"


# Память диалога: короткая история последних реплик по каждому пользователю.
# Раньше каждый вопрос уходил отдельным запросом, и ИИ не помнил, о чём только
# что шла речь — «а подробнее?» оставалось без контекста.
CHAT_HISTORY = {}
CHAT_HISTORY_LIMIT = 6  # сколько пар «вопрос-ответ» помним для контекста ИИ
# Предел числа пользователей, для которых держим историю в памяти. Без него
# словари росли бы с каждым новым гостем и память процесса не освобождалась бы.
CHAT_HISTORY_USERS_LIMIT = 500
CHAT_LOG_USERS_LIMIT = 500
_chat_lock = threading.RLock()

# Журнал чата — для листания истории в интерфейсе. CHAT_HISTORY (выше)
# отвечает только за контекст нейросети и намеренно короткий; если хранить
# для показа тот же список, старые реплики исчезали бы навсегда и листать
# было бы нечего. Поэтому история для экрана ведётся отдельно и длиннее:
# её отдают страницами через /history.
CHAT_LOG = {}
CHAT_LOG_LIMIT = 400      # сколько реплик храним (по 2 на каждую пару)
CHAT_LOG_MAX_PAGE = 100   # предел страницы, чтобы клиент не выкачал всё сразу
_chat_seq = 0             # сквозной номер реплики: по нему листают вверх

# Порядок ключей в словаре — это порядок ПЕРВОГО появления, а не последней
# активности: запись, которую обновили, остаётся на старом месте. Поэтому
# подрезка ниже опирается на явные метки «когда ключ трогали в последний раз»,
# иначе вылетал бы как раз активный пользователь, а оставался давно молчащий.
_chat_touch = {}          # api_key -> номер последнего обращения
_chat_touch_seq = 0


def _touch_chat(api_key: str):
    """Отмечает, что историю этого ключа только что использовали.

    Вызывается под _chat_lock.
    """
    global _chat_touch_seq
    _chat_touch_seq += 1
    _chat_touch[api_key] = _chat_touch_seq


def log_chat(api_key: str, role: str, text: str):
    """Добавляет реплику в журнал чата: role — 'user' или 'bot'."""
    global _chat_seq
    if not api_key or not text:
        return
    with _chat_lock:
        _chat_seq += 1
        entry = {
            "seq": _chat_seq,
            "role": "bot" if role == "bot" else "user",
            "text": str(text)[:2000],
            "ts": datetime.datetime.now().strftime("%H:%M"),
        }
        log = CHAT_LOG.setdefault(api_key, [])
        log.append(entry)
        del log[:-CHAT_LOG_LIMIT]
        # Отмечаем свежесть до подрезки: она смотрит на _chat_touch, а не на
        # порядок первого появления ключа.
        _touch_chat(api_key)
        # Журнал ведётся по всем пользователям сразу: без подрезки словарь рос бы
        # с каждым новым гостем, и память процесса не освобождалась бы никогда.
        _trim_dict(CHAT_LOG, CHAT_LOG_USERS_LIMIT)


def chat_log_page(api_key: str, before_seq: int = 0, limit: int = 20):
    """Страница журнала: реплики с seq < before_seq, самые свежие из них.

    Нумерация вместо смещения от конца: пока листаешь историю, в чат приходят
    новые реплики, и «offset от конца» съезжал бы — одна и та же страница
    показывалась бы дважды или пропускалась. С seq этого не происходит.

    Отдаём в хронологическом порядке (старые → новые) и сообщаем, осталось
    ли что-то выше (has_more), чтобы страница знала, показывать ли кнопку.
    """
    with _chat_lock:
        log = list(CHAT_LOG.get(api_key, []))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    try:
        before_seq = int(before_seq or 0)
    except (TypeError, ValueError):
        before_seq = 0
    limit = max(1, min(limit, CHAT_LOG_MAX_PAGE))
    if before_seq > 0:
        candidates = [e for e in log if e.get("seq", 0) < before_seq]
    else:
        candidates = log
    page = candidates[-limit:]
    oldest = page[0]["seq"] if page else 0
    has_more = any(e.get("seq", 0) < oldest for e in log) if page else False
    return {"messages": page, "oldest_seq": oldest, "has_more": has_more}


def remember_turn(api_key: str, question: str, answer: str):
    """Запоминает последнюю пару реплик пользователя."""
    if not api_key:
        return
    with _chat_lock:
        history = CHAT_HISTORY.setdefault(api_key, [])
        history.append({"q": (question or "")[:1000], "a": (answer or "")[:1000]})
        del history[:-CHAT_HISTORY_LIMIT]
        # Отмечаем свежесть до подрезки: она смотрит на _chat_touch, а не на
        # порядок первого появления ключа.
        _touch_chat(api_key)
        _trim_dict(CHAT_HISTORY, CHAT_HISTORY_USERS_LIMIT)


def get_history(api_key: str):
    with _chat_lock:
        return list(CHAT_HISTORY.get(api_key, []))


def _trim_dict(mapping: dict, limit: int):
    """Не даёт словарю расти бесконечно: оставляет только свежие ключи.

    Ключ здесь — api_key, а пользователей может быть сколько угодно: без
    подрезки CHAT_HISTORY/CHAT_LOG удерживали бы историю всех, кто когда-либо
    писал, и процесс на сервере рос бы вместе с числом гостей.

    Свежесть берём из _chat_touch, а не из порядка ключей: порядок отражает
    момент ПЕРВОГО появления пользователя, поэтому повторно активный ключ
    считался бы старым и вылетал первым. Вызывается под _chat_lock.
    """
    if len(mapping) <= limit:
        return
    order = sorted(mapping, key=lambda k: _chat_touch.get(k, 0))
    for k in order[:len(mapping) - limit]:
        mapping.pop(k, None)
        # Метку свежести снимаем только когда ключ ушёл из ОБОИХ словарей:
        # CHAT_HISTORY и CHAT_LOG подрезаются отдельными вызовами, и если
        # стереть метку на первом из них, уцелевший ключ получил бы вес 0 и
        # вылетел бы следующим — хотя его только что использовали.
        if k not in CHAT_HISTORY and k not in CHAT_LOG:
            _chat_touch.pop(k, None)


def clear_history(api_key: str):
    """Забывает и контекст ИИ, и журнал для листания: /clear очищает чат."""
    with _chat_lock:
        CHAT_HISTORY.pop(api_key, None)
        CHAT_LOG.pop(api_key, None)


def set_reminder(api_key: str, text: str, when: str) -> str:
    """Ставит напоминание: задание с меткой «Один раз <дата> в <время>».

    Дальше его подхватывает штатный планировщик задач — отдельный механизм
    будильников не нужен, а напоминание видно в общем списке заданий.
    """
    text = (text or "").strip()
    if not text:
        return "Пустое напоминание не ставлю"
    now = datetime.datetime.now()
    when = (when or "").strip()
    tm = TIME_RE.search(when)
    if not tm:
        return "Не понял время. Скажи так: «напомни в 19:30 позвонить маме»"
    hh, mm = int(tm.group(1)), int(tm.group(2))
    ss = int(tm.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return "Некорректное время напоминания"
    dm = DATE_RE.search(when)
    if dm:
        try:
            date = datetime.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        except ValueError:
            return "Некорректная дата напоминания"
    else:
        date = now.date()
    moment = datetime.datetime.combine(date, datetime.time(hh, mm, ss))
    if moment <= now:
        # Время уже прошло — значит, речь о завтрашнем дне.
        moment += datetime.timedelta(days=1)
    label = f"Один раз {moment.strftime('%d.%m.%Y')} в {moment.strftime('%H:%M:%S')}"
    ok = add_task(api_key, f"[{label}] {text}")
    if not ok:
        return "Не удалось сохранить напоминание"
    return f"Напомню {moment.strftime('%d.%m.%Y в %H:%M')}: {text}"


AI_TOOLS = [
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Открыть ссылку и вернуть текст страницы. Если для сайта сохранена учётка, вход выполняется автоматически.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Полная ссылка, начиная с http:// или https://"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "download_file",
        "description": "Скачать файл по ссылке в папку downloads.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Ссылка на файл"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "login_site",
        "description": "Войти на сайт по сохранённой учётке и вернуть текст страницы.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Название сайта или часть ссылки"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_news",
        "description": "Показать свежие новости.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Сколько новостей, по умолчанию 8"}}}}},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "Добавить задание в список дел пользователя.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Текст задания"}},
            "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "Показать активные задания пользователя.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "Закрыть задание по его id.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "id задания из list_tasks"}},
            "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Узнать погоду сейчас и прогноз на сегодня-завтра для города.",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "Название города, например Москва"}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Найти что-нибудь в интернете и вернуть список ссылок с описаниями.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "limit": {"type": "integer", "description": "Сколько результатов, по умолчанию 5"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Поставить напоминание на конкретное время. Время в формате ЧЧ:ММ, при необходимости с датой ДД.ММ.ГГГГ.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "О чём напомнить"},
            "when": {"type": "string", "description": "Когда, например «19:30» или «16.09.2026 08:00»"}},
            "required": ["text", "when"]}}},
]


def _tool_download(url: str) -> str:
    """Скачивает файл в downloads и возвращает отчёт."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Нужна ссылка http:// или https://"
    name = os.path.basename(urllib.parse.unquote(parsed.path)) or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]
    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        res = requests.get(url, timeout=60, headers=browser_headers(), stream=True)
        res.raise_for_status()
        # Отсекаем гигантов ещё по заголовку: тело даже не начинаем качать.
        declared = res.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            return (f"Файл больше {MAX_DOWNLOAD_MB} МБ "
                    f"({int(declared) // (1024 * 1024)} МБ) — не скачиваю.")
        path = os.path.join(DOWNLOADS_DIR, name)
        base, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path = f"{base}_{n}{ext}"
            n += 1
        written = 0
        try:
            with open(path, "wb") as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    # Размер мог быть не указан в заголовке — считаем на лету,
                    # иначе диск всё равно забьётся.
                    if written > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"файл больше {MAX_DOWNLOAD_MB} МБ")
                    f.write(chunk)
        except Exception:
            # Недокачанный огрызок на диске не оставляем.
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        size = os.path.getsize(path)
        logger.info(f"Скачан файл: {path} ({size} байт)")
        return f"Скачал «{os.path.basename(path)}» ({size} байт)"
    except Exception as e:
        logger.error(f"download {url}: {e}")
        return f"Не удалось скачать: {e}"


def run_ai_tool(name: str, args: dict, api_key: str = "") -> str:
    """Выполняет инструмент, который запросила нейросеть, и возвращает результат текстом."""
    args = args or {}
    try:
        if name == "open_url":
            url = str(args.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return "Нужна полная ссылка http:// или https://"
            content, logged_in = open_as_user(api_key, url)
            if not content:
                # Фолбэк без входа: проверяет капчу/SPA и рендерит браузером.
                content = plain_page_text(url, limit=3000)
            if not content:
                return f"{url}: страница пустая или не текстовая"
            note = "Вход выполнен по сохранённой учётке.\n" if logged_in else ""
            return f"{note}{content}"

        if name == "download_file":
            return _tool_download(str(args.get("url") or "").strip())

        if name == "login_site":
            text, site, err = login_to_site(api_key, query=str(args.get("query") or ""))
            if err:
                return err
            return f"Вошёл на «{site}».\n\n{text}"

        if name == "get_news":
            try:
                limit = int(args.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            source, titles, errors = collect_news(limit=max(1, min(limit, 20)))
            if not titles:
                return "Новости недоступны: " + ("; ".join(errors) or "ленты не ответили")
            return format_news(source, titles)

        if name == "add_task":
            task = str(args.get("task") or "").strip()
            if not task:
                return "Пустое задание не добавляю"
            return "Задание добавлено" if add_task(api_key, task) else "Не удалось добавить задание"

        if name == "list_tasks":
            items = get_tasks(api_key)
            if not items:
                return "Активных заданий нет"
            return "\n".join(f"- {t.get('task')} (id: {t.get('id')})" for t in items)

        if name == "complete_task":
            tid = str(args.get("task_id") or "").strip()
            if not tid:
                return "Не указан id задания"
            return "Задание закрыто" if complete_task(api_key, tid) else "Задание не найдено"

        if name == "get_weather":
            return get_weather(str(args.get("city") or ""))

        if name == "web_search":
            try:
                limit = int(args.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            return web_search(str(args.get("query") or ""), limit=limit)

        if name == "set_reminder":
            return set_reminder(api_key, str(args.get("text") or ""),
                                str(args.get("when") or ""))
    except Exception as e:
        logger.error(f"Инструмент {name}: {e}")
        return f"Ошибка инструмента {name}: {e}"
    return f"Неизвестный инструмент: {name}"


def ai_ready() -> bool:
    """Настроена ли нейросеть: есть путь, ключ и модель."""
    return bool(ai_endpoint() and SETTINGS.get("ai_api_key") and SETTINGS.get("ai_model"))


def strip_reasoning(answer):
    """Убирает из ответа модели «размышления вслух».

    Локальная модель иногда возвращает не готовый ответ, а план: «user is
    asking …», «I should …», «just answer. На странице …». Показывать это в
    чате нельзя — гость видел вместо ответа служебные мысли модели.
    """
    if not answer:
        return ""
    text = str(answer).strip()
    markers = (
        "user is asking", "the user is asking", "i should", "i need to",
        "just answer", "no tool needed", "we need answer", "need answer",
        "final only", "wait,", "let's craft", "potential final", "we must",
    )
    low = text.lower()
    if low.startswith(markers) or "\nuser is asking" in low:
        # План идёт одним куском, а готовый ответ — после пустой строки.
        parts = [p.strip() for p in text.split("\n\n") if p.strip()]
        tail = parts[-1] if parts else ""
        if tail and not tail.lower().startswith(markers):
            text = tail
        else:
            return ""
    # Отрезаем служебные хвосты, которые модель дописывает после ответа.
    for tail_marker in ("\nWe must", "\nLet's", "\nPotential final",
                        "\nUser is asking", "\nJust answer"):
        idx = text.find(tail_marker)
        if idx > 0:
            text = text[:idx].strip()
    return text


def ask_ai(user_name: str, text: str, api_key: str = "", as_task: bool = False):
    """Спрашивает нейросеть. as_task=True — это задание по расписанию.

    Задания кэшировать нельзя: повторяющееся дело («проверь дневник») должно
    каждый раз реально ходить на сайт, а не отдавать вчерашний ответ из кэша.
    """
    url = ai_endpoint()
    key = SETTINGS.get("ai_api_key", "")
    model = SETTINGS.get("ai_model", "")
    if not ai_ready():
        return "Нейросеть не настроена: открой админку и укажи путь, ключ и модель"

    # Кэш учитывает модель И пользователя: без api_key ответ одного клиента
    # отдавался другому (в кэше лежали чужие данные, включая результаты
    # инструментов — открытые страницы и прочитанные задания).
    cache_key = hash_text(f"{model}|{api_key}|{text}")
    if not as_task and cache_key in ai_cache:
        return ai_cache[cache_key]

    # Адреса сохранённых сайтов подмешиваем в системный промпт: без этого ИИ
    # отвечал «пришли ссылку», хотя ссылка уже лежит в «Сайтах».
    system_prompt = SETTINGS.get("system_prompt", "")
    if api_key:
        hint = saved_sites_hint(api_key)
        if hint:
            system_prompt = f"{system_prompt}\n\n{hint}\nЕсли пользователь просит войти на такой сайт, ответь, что можешь это сделать, и попроси написать «войди в <название>»."

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # Для задания берём другую инструкцию: модель должна выполнить поручение,
    # а не отвечать на него как на обычную реплику в чате.
    tool_prompt = AI_TASK_PROMPT if as_task else AI_TOOL_PROMPT
    if as_task:
        # Задание уходит ровно как поручение «сделай задание <текст>»,
        # без обёртки «пользователь пишет» — модель должна его выполнить.
        user_message = text
    else:
        user_message = f"Пользователь {user_name} пишет: {text}"
    messages = [
        {"role": "system", "content": f"{system_prompt}\n\n{tool_prompt}"},
    ]
    # Память диалога: дописываем предыдущие реплики, чтобы ИИ понимал
    # уточнения вроде «а подробнее?». Задания по расписанию идут без истории.
    if not as_task:
        for turn in get_history(api_key):
            messages.append({"role": "user",
                             "content": f"Пользователь {user_name} пишет: {turn['q']}"})
            messages.append({"role": "assistant", "content": turn["a"]})
    messages.append({"role": "user", "content": user_message})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": SETTINGS.get("temperature", 0.7),
        "max_tokens": SETTINGS.get("max_tokens", 500),
        "tools": AI_TOOLS,
        "tool_choice": "auto",
    }

    def _post(body):
        return requests.post(url, headers=headers, json=body, timeout=60)

    try:
        res = _post(payload)

        # Часть локальных моделей не умеет tools и отвечает 400. Тогда просто
        # повторяем без инструментов — обычный чат, как было раньше.
        if res.status_code == 400 and "tool" in res.text.lower():
            logger.warning("Модель не поддерживает tools — работаю без инструментов")
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            res = _post(payload)

        # Счётчик одинаковых вызовов: локальная модель умеет зацикливаться на
        # одном инструменте («открой …» → тот же ответ → снова «открой …»),
        # и без этого запрос крутился до пяти шагов, каждый раз дергая сеть.
        seen_calls = {}
        for _step in range(5):
            if res.status_code != 200:
                logger.error(f"AI {res.status_code}: {res.text[:300]}")
                return f"Ошибка ИИ ({res.status_code}): {res.text[:200]}"

            msg = (res.json().get("choices") or [{}])[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = msg.get("content")
                if not answer:
                    # Некоторые локальные модели кладут весь текст в
                    # reasoning_content, а content оставляют пустым.
                    answer = msg.get("reasoning_content")
                answer = strip_reasoning(answer)
                if not answer:
                    return "Не понял вопрос"
                if len(ai_cache) < 100:
                    ai_cache[cache_key] = answer
                # Запоминаем пару реплик, чтобы следующий вопрос понимал контекст.
                if not as_task:
                    remember_turn(api_key, text, answer)
                return answer

            # Модель попросила инструменты — выполняем и возвращаем результат ей.
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                # Один и тот же инструмент с теми же аргументами в третий раз —
                # это цикл, а не работа. Останавливаемся и отдаём то, что уже
                # успели получить, вместо повторного похода на тот же сайт.
                signature = f"{name}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                if seen_calls[signature] > 2:
                    logger.warning(f"ИИ зациклился на инструменте {name} — остановка")
                    # Формулировка без технических деталей: пользователю важно
                    # понять, что дело не в его запросе, а во внешнем ресурсе.
                    return ("Сайт временно недоступен или выдает ошибку, "
                            "попробуйте позже")
                logger.info(f"ИИ вызвал инструмент: {name} {args}")
                result = run_ai_tool(name, args, api_key)
                messages.append({"role": "tool",
                                 "tool_call_id": call.get("id") or name,
                                 "content": result[:8000]})
            res = _post(payload)

        return "Слишком много шагов с инструментами — остановился"
    except Exception as e:
        logger.error(f"AI request: {e}")
        return f"Ошибка ИИ: {e}"


# -------------------------------------------------
# ШПАРГАЛКА
# -------------------------------------------------
# Отдельный запрос к модели, а не ask_ai. Причины:
#  * шпаргалке не нужны инструменты — она ничего не открывает и не ищет,
#    а лишние tools в теле запроса только провоцируют модель «сходить на сайт»;
#  * историю диалога подмешивать нельзя: тема «химия» после переписки про
#    дневник тянула бы ответ в сторону;
#  * нужен свой лимит длины: тезисы не влезают в 500 токенов чата.
CHEAT_SHEET_PROMPT = (
    "Ты — генератор компактных шпаргалок. По теме пользователя сделай максимально "
    "плотную выжимку: только формулы, определения, даты, правила, исключения и "
    "ключевые тезисы, которые реально спрашивают на контрольной. "
    "Никаких вступлений, обращений, пояснений и «воды» — сразу по делу. "
    "Пиши короткими строками, при необходимости списком. "
    "Отвечай на языке темы. Объём — до 350 слов."
)

# Кэш шпаргалок: одна и та же тема не должна каждый раз стоить запроса к модели.
# Ключ — хэш модели и темы (без api_key: шпаргалка не зависит от пользователя,
# в ней нет ни истории диалога, ни его сайтов).
cheat_cache = {}


def cheat_sheet(topic: str) -> str:
    """Краткая шпаргалка по теме: формулы и ключевые тезисы без «воды»."""
    url = ai_endpoint()
    key = SETTINGS.get("ai_api_key", "")
    model = SETTINGS.get("ai_model", "")
    if not ai_ready():
        return "Нейросеть не настроена: открой админку и укажи путь, ключ и модель"

    topic = (topic or "").strip()
    if not topic:
        return "Напиши тему — например «тригонометрия» или «Великая Отечественная война»"

    cache_key = hash_text(f"cheat|{model}|{topic.lower()}")
    if cache_key in cheat_cache:
        return cheat_cache[cache_key]

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CHEAT_SHEET_PROMPT},
            {"role": "user", "content": topic},
        ],
        # Ниже температуры чата: у шпаргалки важна точность формул, а не
        # разнообразие формулировок.
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=60)
        if res.status_code != 200:
            logger.error(f"AI cheatsheet {res.status_code}: {res.text[:300]}")
            return f"Ошибка ИИ ({res.status_code}): {res.text[:200]}"
        msg = (res.json().get("choices") or [{}])[0].get("message") or {}
        answer = msg.get("content") or msg.get("reasoning_content")
        answer = strip_reasoning(answer)
        if not answer:
            return "Не понял тему — попробуй сформулировать иначе"
        if len(cheat_cache) < 200:
            cheat_cache[cache_key] = answer
        return answer
    except Exception as e:
        logger.error(f"AI cheatsheet request: {e}")
        return f"Ошибка ИИ: {e}"


# -------------------------------------------------
# КОМАНДЫ
# -------------------------------------------------
URL_RE = re.compile(r"https?://[^\s]+")

# Символы, которые не могут стоять в URL как есть: кириллица, кавычки,
# угловые скобки. Всё с этого места — уже текст фразы, а не адрес.
_URL_STOP_RE = re.compile(r"[\u0400-\u04FF\"'<>«»]")


def _clean_url(raw: str) -> str:
    """Обрезает хвост фразы, который regex захватил вместе со ссылкой.

    ``https?://[^\s]+`` останавливается только на пробеле, поэтому в
    «открой https://example.com,пожалуйста» (без пробела после запятой) в
    адрес попадало слово «пожалуйста». Дальше urlparse считал его частью
    хоста, getaddrinfo не мог разрешить такое имя, и валидная ссылка
    заканчивалась ошибкой «Не удалось разрешить адрес».
    """
    if not raw:
        return ""
    stop = _URL_STOP_RE.search(raw)
    if stop:
        raw = raw[:stop.start()]
    return raw.rstrip(".,;:!?»)\"'…")


def detect_command(text: str):
    """Определяет команду и заодно вытаскивает из текста ссылку, если она нужна.

    Команда распознаётся только при явном приказе («открой …», «скачай …»).
    Раньше хватало одного слова, и любой вопрос вроде «расскажи что такое
    дневник наблюдений» или «как очистить кэш в браузере» попадал в заглушку
    вместо нейросети.
    """
    t = text.lower()
    url_match = URL_RE.search(text)
    url = _clean_url(url_match.group(0)) if url_match else ""

    # «Войди в дневник», «залогинься на сайте» — это вход по сохранённой учётке.
    if any(k in t for k in LOGIN_VERBS):
        return {"action": "login_site", "params": {"query": text, "url": url}}

    # «Проверь дневник» — если учётка сайта сохранена, агент входит и читает его.
    # Эту проверку делаем ДО разбора «открой»: иначе короткая фраза «открой дневник»
    # (2 слова, ссылки нет) уходила в browse и агент отвечал «пришли ссылку целиком»
    # вместо того, чтобы войти по сохранённой учётке и прочитать дневник.
    if "дневник" in t and any(k in t for k in ("проверь", "посмотри", "зайди", "покажи", "открой")):
        return {"action": "check_diary", "params": {"query": text}}

    open_words = ("зайди на", "открой", "перейди", "сходи на", "fetch", "browse")
    if url and any(k in t for k in open_words):
        return {"action": "browse", "params": {"url": url}}
    # «открой» без ссылки — короткая просьба, на неё отвечаем подсказкой.
    if any(k in t for k in open_words) and len(t.split()) <= 4:
        return {"action": "browse", "params": {"url": url}}

    if "будильник" in t and any(k in t for k in ("поставь", "поставить", "заведи", "установи", "включи")):
        return {"action": "set_alarm", "params": {"time": "07:00"}}

    if url and any(k in t for k in ("скачай", "скачать", "загрузи", "загрузить")):
        return {"action": "download_file", "params": {"url": url}}

    if any(k in t for k in ("очисти", "почисти")) and any(k in t for k in ("downloads", "папку", "загрузк")):
        return {"action": "clean_system", "params": {}}

    return None


def execute_command(api_key: str, command: dict):
    """Выполняет команду и возвращает текст ответа для чата.

    Раньше /ask просто отдавал {"command": ...} клиенту, а клиент показывал
    «Готово» и ничего не делал — команда терялась. Теперь действие выполняется
    на сервере, и в ответе всегда есть готовый текст `answer`.
    """
    action = command.get("action")
    params = command.get("params") or {}

    if action == "browse":
        url = params.get("url")
        if not url:
            return "🔗 Пришли ссылку целиком, например: открой https://example.com"
        try:
            # Если для этого сайта сохранена учётка — сначала входим, иначе
            # закрытая страница отдаст только форму логина.
            content, logged_in = open_as_user(api_key, url)
            if not content:
                # Фолбэк без входа: проверяет капчу/SPA и рендерит браузером.
                content = plain_page_text(url, limit=3000)
            if not content:
                return f"🔗 {url}: страница пустая или не текстовая"
            note = "🔓 Вошёл по сохранённой учётке.\n\n" if logged_in else ""
            logger.info(f"Команда browse выполнена: {url} (вход: {logged_in})")
            return f"🔗 {url}\n\n{note}{content}"
        except Exception as e:
            logger.error(f"browse {url}: {e}")
            return f"❌ Не удалось открыть ссылку: {e}"

    if action == "download_file":
        url = params.get("url")
        if not url:
            return "📥 Пришли ссылку на файл, например: скачай https://example.com/file.pdf"
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "📥 Нужна ссылка http:// или https://"
        name = os.path.basename(urllib.parse.unquote(parsed.path)) or "file"
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]
        folder = DOWNLOADS_DIR
        try:
            os.makedirs(folder, exist_ok=True)
            res = requests.get(url, timeout=60, headers=browser_headers(), stream=True)
            res.raise_for_status()
            # Тот же потолок, что и в инструменте download_file: одна ссылка
            # не должна забивать диск бесплатного Render на сотни мегабайт.
            declared = res.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                return (f"❌ Файл больше {MAX_DOWNLOAD_MB} МБ "
                        f"({int(declared) // (1024 * 1024)} МБ) — не скачиваю.")
            path = os.path.join(folder, name)
            base, ext = os.path.splitext(path)
            n = 1
            while os.path.exists(path):
                path = f"{base}_{n}{ext}"
                n += 1
            written = 0
            try:
                with open(path, "wb") as f:
                    for chunk in res.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"файл больше {MAX_DOWNLOAD_MB} МБ")
                        f.write(chunk)
            except Exception:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
                raise
            size = os.path.getsize(path)
            logger.info(f"Скачан файл: {path} ({size} байт)")
            return f"✅ Скачал «{os.path.basename(path)}» ({size} байт)"
        except Exception as e:
            logger.error(f"download_file {url}: {e}")
            return f"❌ Не удалось скачать: {e}"

    if action == "login_site":
        text, site, err = login_to_site(api_key, query=params.get("query", ""),
                                        url=params.get("url", ""))
        if err:
            return err
        return f"🔓 Вошёл на «{site}».\n\n{text}"

    if action == "set_alarm":
        return ("⏰ Будильник на этом компьютере я ставить не умею — для этого нужен "
                "доступ к системе. Напомню иначе: добавь задание «запомни ...» на странице заданий.")

    if action == "check_diary":
        # Учётка дневника сохранена? Тогда входим и читаем его сами.
        text, site, err = login_to_site(api_key, query=params.get("query", "дневник"))
        if not err and text:
            return f"📅 {site} — вошёл по сохранённой учётке:\n\n{text}"
        if err and "не сохранена" not in err:
            return err
        return ("📅 Дневник я не вижу: школьные дневники требуют входа по логину. "
                "Сохрани учётку сайта (кнопка «Сайты») — и я войду сам.")

    if action == "clean_system":
        return ("🧹 Чистку системы я не выполняю: у меня есть доступ только к своим файлам. "
                "Очистить можно папку downloads рядом с server.py.")

    return "🤔 Не понял, что нужно сделать."

