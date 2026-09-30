# ============================================
# server.py — ОСНОВНАЯ ЧАСТЬ (HTTP-сервер)
# ============================================
# Здесь только веб-слой: FastAPI, роуты, админка, выдача страниц и запуск.
# Вся логика и инструменты лежат в tools.py — импортируются ниже.
# Запуск:  python server.py
# ============================================

import asyncio
import base64
import binascii
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse

import tools
import telegram_runner       # фоновая нить с Telegram-ботом (см. lifespan)
# Раньше здесь стоял звёздочный импорт `from tools import *`: он тянул в
# глобальную область видимости server.py всё содержимое tools.py — сотни имён,
# включая служебные (WEATHER_CODES, SPA_ROOT_IDS и т.п.). Из-за этого было
# непонятно, откуда взялась та или иная функция, и линтер приходилось глушить
# комментарием noqa. Теперь импортируем ровно то, что server.py использует.
import re
import urllib.parse
import uuid

import requests
from bs4 import BeautifulSoup

# Транспортные модели запросов живут в отдельном модуле schemas.py.
from schemas import (
    AdminRequest, ApiKeyRequest, AskRequest, BrowseRequest, CheatSheetRequest,
    CompleteTaskRequest, DeleteCredentialsRequest, DownloadRequest,
    FetchUrlRequest, HistoryRequest, NewsRequest, RegisterRequest,
    RememberRequest, SaveCredentialsRequest, UploadRequest,
)
from schemas import MAX_UPLOAD_BYTES
# Функции и константы движка.
from tools import (
    add_task, ai_endpoint, ai_summarize, ask_ai, browser_headers,
    chat_log_page, cheat_sheet, clean_old_messages, clear_history, cloud_ready,
    collect_news, complete_task, connect_services, create_user, delete_credentials,
    dequeue_messages, detect_command, enqueue_message, execute_command,
    format_news, get_tasks, get_user, list_credentials,
    load_settings_from_cloud, log_chat, logger, normalize_site, open_as_user,
    plain_page_text, probe_cloud_tables, public_settings, rate_limit,
    save_credentials, take_scheduled_messages, update_settings,
)

# Клиент Supabase пересоздаётся в connect_services() уже после импорта, поэтому
# держим модульную ссылку: _sync_from_tools() обновит её на старте.
supabase = tools.supabase
from tools import BASE_DIR, WEB_DIR, DOWNLOADS_DIR, SETTINGS, UPLOADS_DIR
# Проверка адреса живёт в tools.py: тот же страж используется и внутри самих
# инструментов (http_get_text, plain_page_text), поэтому одной копии достаточно.
from tools import _safe_public_url

# Папка uploads/ теперь объявлена в tools.py: из неё же читает инструмент
# read_uploaded_file, а ручка /upload_file сюда только пишет. Держать путь в
# двух местах нельзя — это ровно тот случай, когда копии расходятся.


def _sync_from_tools():
    """Забирает изменяемое состояние из tools.

    tools.connect_services() пересоздаёт клиент Supabase уже после импорта,
    а SETTINGS может подгрузиться из облака — поэтому перед работой
    подтягиваем актуальные ссылки в этот модуль.
    """
    global supabase, SETTINGS
    supabase = tools.supabase
    SETTINGS = tools.SETTINGS


# Слова-команды для новостей. Раньше хватало подстроки «новост» в любом месте
# сообщения, и вопрос «объясни, как делают новости» уходил в ленту RSS вместо
# нейросети. Теперь команда срабатывает, только если запрос целиком про новости.
NEWS_COMMANDS = ("/news", "новости", "новость")
NEWS_VERBS = ("покажи", "дай", "расскажи", "какие", "свежие", "последние",
              "показывай", "давай")


def _is_news_request(text: str) -> bool:
    """Просит ли пользователь новости, а не спрашивает о них.

    «новости», «/news», «покажи новости», «свежие новости» — да.
    «объясни, как делают новости» — нет, это вопрос к нейросети.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if t in NEWS_COMMANDS:
        return True
    words = t.split()
    if not any(w in NEWS_COMMANDS[1:] for w in words):
        return False
    # Новости — предмет просьбы, только если запрос короткий и начинается
    # с глагола-просьбы («покажи свежие новости за сегодня»).
    if len(words) > 5:
        return False
    return words[0] in NEWS_VERBS


# Служебные команды в ленте истории листать незачем.
SERVICE_COMMANDS = ("/help", "/status", "/clear", "/reset")


def _answer(api_key: str, answer: str, **extra):
    """Ответ чата: реплика бота уходит в журнал, остальное — как было.

    Все успешные ответы /ask проходят через эту обёртку, поэтому история для
    экрана теперь двусторонняя: вопрос и ответ, а не только вопрос.
    """
    _log_turn(api_key, "bot", answer)
    payload = {"success": True, "answer": answer}
    payload.update(extra)
    return payload


def _log_turn(api_key: str, role: str, text: str):
    """Пишет реплику в журнал истории для экрана.

    Раньше в журнал попадали только сообщения пользователя: /history отдавал
    одни вопросы, а ответы агента при перезагрузке страницы исчезали, хотя
    нейросеть их помнила. Теперь сохраняем обе стороны диалога.
    """
    if not text:
        return
    stripped = text.strip().lower()
    if role == "user" and (stripped in SERVICE_COMMANDS or
                           (stripped.startswith("/") and stripped not in ("/news", "/tasks"))):
        return
    if role == "bot" and stripped in SERVICE_COMMANDS:
        return
    log_chat(api_key, role, text)


# -------------------------------------------------
# FASTAPI
# -------------------------------------------------
def startup():
    """Готовит сервисы к работе. Вызывается один раз при старте приложения."""
    connect_services()
    _sync_from_tools()
    # Сначала выясняем, какие таблицы есть в облаке, потом подтягиваем из них
    # настройки: на Render файл settings.json после деплоя пустой.
    probe_cloud_tables()
    load_settings_from_cloud()
    clean_old_messages()
    # Фоновый планировщик: следит за метками расписания в заданиях и сам
    # выполняет наступившие (например, «Один раз 16.09.2026 в 20:04:17»).
    tools.start_task_scheduler()
    # Telegram-бот: поднимается фоновой нитью этого же процесса, если задана
    # переменная TELEGRAM_BOT_TOKEN. На бесплатном Render отдельный worker
    # недоступен, поэтому оба входа обслуживает один сервис. Без токена
    # вызов просто пишет строку в лог и ничего не делает.
    telegram_runner.start_telegram_bot()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Старт и остановка приложения.

    Раньше здесь был @app.on_event("startup"). FastAPI 0.109 помечает его как
    устаревший и печатает DeprecationWarning при каждом запуске — на Render это
    лишний шум в логе. Штатная замена — lifespan: код старта остался тот же,
    изменился только способ его подписки.
    """
    # Все эндпоинты объявлены через def (синхронные) и внутри делают
    # блокирующие сетевые вызовы (requests к ИИ и к сайтам). FastAPI выполняет
    # такие функции в пуле потоков anyio, а его потолок по умолчанию — 40.
    # Долгий запрос к ИИ (до 60 секунд) занимает поток целиком, поэтому при
    # наплыве пользователей очередь доходила бы и до лёгких ручек вроде /health.
    # Поднимаем потолок: запас на пиковую нагрузку без переписывания всего
    # сетевого слоя на httpx/async.
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = 100
    except Exception as e:   # pragma: no cover — среда без anyio
        logger.warning(f"Не удалось поднять лимит потоков: {e}")
    startup()
    yield


# -------------------------------------------------
# CORS: КОМУ РАЗРЕШЕНО ОБРАЩАТЬСЯ К API ИЗ БРАУЗЕРА
# -------------------------------------------------
# Раньше здесь стояло allow_origins=["*"] вместе с allow_credentials=True —
# и это была самая опасная строка во всём файле. Любой сайт (например,
# evil.com) мог из браузера пользователя сделать запрос к нашему API, потому
# что сервер отвечал «разрешено всем». Ключ пользователя лежит в теле запроса,
# не привязан к домену и не истекает, поэтому чужой странице достаточно было
# выполнить запрос от имени зашедшего пользователя, чтобы прочитать его
# историю, задания и сохранённые учётки.
#
# Теперь источники перечисляются вручную в переменной CORS_ALLOW_ORIGINS
# через запятую, например:
#   CORS_ALLOW_ORIGINS=https://мой-сервис.onrender.com,http://127.0.0.1:8000
# По умолчанию список пуст. Свои страницы (index.html, tasks.html, админку)
# сервер отдаёт с того же origin, поэтому им CORS вообще не нужен — пустой
# список ничего не ломает, зато закрывает доступ чужим сайтам.
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]

if not CORS_ALLOW_ORIGINS:
    logger.info(
        "CORS_ALLOW_ORIGINS не задан — кросс-доменные запросы из браузера "
        "запрещены (свои страницы работают: они с того же origin)"
    )

app = FastAPI(title="OmniLAMo Free Beta", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=bool(CORS_ALLOW_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# ЭНДПОИНТЫ
# -------------------------------------------------

@app.get("/")
def home():
    return {"status": "ok", "version": "free-beta"}


# Отдельный пул потоков только для проверки живости. Синхронные обработчики
# FastAPI выполняются в общем пуле anyio (его потолок поднят до 100 в lifespan),
# и туда же уходят долгие запросы к ИИ — до 60 секунд. Пока такой запрос держит
# поток, /health ждал бы освобождения места, балансировщик Render не получал бы
# ответ вовремя и перезапускал контейнер. Свой пул гарантирует, что опрос базы
# не встанет в очередь за работой пользователей.
_HEALTH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health")


def _probe_users_table() -> None:
    """Один лёгкий запрос к Supabase: жив ли он и пускает ли нас. Блокирующий."""
    supabase.table("users").select("api_key").limit(1).execute()


@app.get("/health")
async def health():
    """Быстрая проверка живости: видно, отвечает ли база и какая модель настроена.

    Нужно, чтобы отличить «сервер не запущен» от «сервер жив, но Supabase молчит»,
    не заглядывая в server.log.

    Объявлена через async def намеренно. У синхронного обработчика (def) и
    запросов к ИИ один и тот же пул потоков anyio: под нагрузкой /health вставал
    в очередь за 60-секундными ответами модели, балансировщик Render не
    дожидался ответа и перезапускал контейнер. Здесь событийный цикл отдаёт
    ответ сразу, а опрос базы уходит в отдельный пул и ограничен по времени.

    Раньше здесь запрос к базе шёл напрямую, без проверки cloud_ready. Если
    Supabase не настроен, в ответе появлялось database_error вида «'NoneType'
    object has no attribute 'table'»: выглядело как поломка базы, хотя ключи
    просто не заданы. Теперь сначала спрашиваем cloud_ready, а database_mode
    прямо говорит, облако это или локальные файлы.
    """
    if cloud_ready("users"):
        db_ok = False
        db_error = ""
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _HEALTH_EXECUTOR, _probe_users_table
                ),
                timeout=2.0,
            )
            db_ok = True
        except Exception as e:
            db_error = str(e)[:200]
        return {
            "status": "ok",
            "version": "free-beta",
            "database": db_ok,
            "database_mode": "supabase",
            "database_error": db_error,
            "model": SETTINGS.get("ai_model", ""),
        }
    return {
        "status": "ok",
        "version": "free-beta",
        "database": False,
        "database_mode": "local-files",
        "database_error": "",
        "model": SETTINGS.get("ai_model", ""),
    }

@app.post("/register")
def register(data: RegisterRequest):
    if not rate_limit(f"reg_{data.name}", max_requests=3, window_seconds=300):
        return {"success": False, "error": "Слишком часто"}
    api_key = uuid.uuid4().hex + uuid.uuid4().hex
    if create_user(data.name, api_key):
        logger.info(f"Новый пользователь: {data.name}")
        return {"success": True, "api_key": api_key}
    return {"success": False, "error": "Ошибка БД"}

@app.post("/history")
def history(data: HistoryRequest):
    """Страница истории чата для листания вверх.

    Отдаём постранично, а не всё сразу: за месяцы переписки в журнале могут
    быть сотни реплик, и грузить их при каждом открытии страницы незачем.
    """
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    page = chat_log_page(data.api_key, before_seq=data.before_seq, limit=data.limit)
    page["success"] = True
    return page


@app.post("/ask")
def ask(data: AskRequest):
    if not rate_limit(f"ask_{data.api_key}", max_requests=10, window_seconds=60):
        return {"success": False, "error": "Слишком часто"}
    user = get_user(data.api_key)
    if not user:
        return {"success": False, "error": "Неверный ключ"}
    # Реплика пользователя попадает в журнал истории. Служебные команды
    # (/help, /clear, /status) не записываем: листать их в ленте незачем.
    _log_turn(data.api_key, "user", data.text)
    if data.text == "/help":
        return _answer(data.api_key, "🤖 Команды:\n💬 Просто спроси\n📰 'Новости' или /news\n🌤 'Какая погода в Москве?'\n🔎 'Найди в интернете ...'\n⏰ 'Напомни в 19:30 позвонить маме'\n📅 'Проверь дневник'\n🔗 'Открой ссылку https://...'\n📥 'Скачай https://...'\n📋 /tasks\n🧹 /clear — забыть историю диалога")
    if data.text == "/status":
        return _answer(data.api_key, "📊 Тариф: Бесплатный (без ограничений)")
    if data.text.strip().lower() in ("/clear", "/reset", "забудь историю"):
        clear_history(data.api_key)
        return _answer(data.api_key, "🧹 История диалога очищена — начинаем с чистого листа")
    # Новости обрабатываем прямо здесь, чтобы работало и без веб-страницы
    # (curl, Telegram-бот и т.п.), а не только через index.html.
    # Проверка была «"новост" in text», и ЛЮБАЯ фраза со словом «новост»
    # («объясни, как делают новости», «что нового в мире новостей») уходила
    # в ленту RSS вместо нейросети. Теперь команда срабатывает, только когда
    # новости — это и есть весь запрос, а не упоминание внутри вопроса.
    if _is_news_request(data.text):
        source, titles, errors = collect_news(limit=8)
        if not titles:
            detail = "; ".join(errors) if errors else "ленты не ответили"
            return {"success": False, "error": f"Не удалось получить новости: {detail}"}
        return _answer(data.api_key, format_news(source, titles, ai_summarize(titles, source)))
    command = detect_command(data.text)
    # Если регулярка не смогла довести команду до конца (например, «открой
    # ссылку» без самой ссылки) — не отдаём заглушку, а пускаем в нейросеть:
    # у неё теперь есть инструменты, и она спросит ссылку сама или вызовет нужный.
    if command and command.get("action") == "browse" and not (command.get("params") or {}).get("url"):
        command = None
    enqueue_message(data.api_key, data.text)
    if command:
        # Команда выполняется сразу и возвращается готовым текстом: раньше
        # клиент получал только {"command": ...} и молча показывал «Готово».
        answer = execute_command(data.api_key, command)
        try:
            supabase.table("message_queue").update({"answer": answer, "status": "done"}).eq(
                "api_key", data.api_key).eq("status", "new").execute()
        except Exception as e:
            logger.error(f"Queue update: {e}")
        return _answer(data.api_key, answer, command=command)
    answer = ask_ai(user["name"], data.text, data.api_key)
    try:
        supabase.table("message_queue").update({"answer": answer, "status": "done"}).eq("api_key", data.api_key).eq("status", "new").execute()
    except Exception as e:
        logger.error(f"Queue update: {e}")
    return _answer(data.api_key, answer)

@app.post("/get_updates")
def get_updates(data: ApiKeyRequest):
    # Раньше здесь был AskRequest с обязательным полем text: клиент, который
    # просто опрашивает очередь (без текста), получал 422 Unprocessable Entity.
    # Для опроса достаточно одного ключа.
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    return {"success": True, "updates": dequeue_messages(data.api_key)}

@app.post("/get_scheduled")
def get_scheduled(data: ApiKeyRequest):
    """Сообщения от планировщика: задания, наступившие по расписанию.

    Планировщик работает в фоне и складывает готовые ответы в очередь.
    Страница чата раз в 15 секунд забирает их отсюда и показывает в ленте —
    иначе «напиши привет в 20:04:17» выполнялось бы, но никто этого не видел.
    """
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    return {"success": True, "messages": take_scheduled_messages(data.api_key)}

@app.post("/fetch_url")
def fetch_url(data: FetchUrlRequest):
    user = get_user(data.api_key)
    if not user:
        return {"success": False, "error": "Неверный ключ"}
    # Проверяем адрес до запроса: раньше сюда можно было прислать
    # http://127.0.0.1:8000/... или http://169.254.169.254/ и заставить
    # сервер сходить внутрь своей же сети (SSRF) — прочитать админку,
    # метаданные облака или чужие внутренние сервисы.
    ok, reason = _safe_public_url(data.url)
    if not ok:
        return {"success": False, "error": reason}
    try:
        response = requests.get(
            data.url, timeout=10, headers=browser_headers(), allow_redirects=True,
        )
        # Перенаправление могло увести на внутренний адрес — проверяем, куда
        # нас в итоге привели, и не читаем такую страницу.
        ok, reason = _safe_public_url(response.url)
        if not ok:
            return {"success": False, "error": reason}
        soup = BeautifulSoup(response.text, "html.parser")
        return {"success": True, "content": soup.get_text()[:2000]}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/remember")
def remember(data: RememberRequest):
    user = get_user(data.api_key)
    if not user:
        return {"success": False, "error": "Неверный ключ"}
    if add_task(data.api_key, data.task):
        return {"success": True, "message": "📝 Запомнил задание"}
    return {"success": False, "error": "Ошибка сохранения"}

@app.get("/get_tasks")
def get_tasks_endpoint(api_key: str):
    if not get_user(api_key):
        return {"success": False, "error": "Неверный ключ"}
    return {"success": True, "tasks": get_tasks(api_key)}

@app.post("/complete_task")
def complete_task_endpoint(data: CompleteTaskRequest):
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    if complete_task(data.api_key, data.task_id):
        return {"success": True, "message": "✅ Задание выполнено"}
    return {"success": False, "error": "Не удалось выполнить"}

@app.post("/save_credentials")
def save_credentials_endpoint(data: SaveCredentialsRequest):
    """Сохраняет учётку сайта: ссылка + название + логин + пароль."""
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    site = normalize_site(data.site, data.url)
    if not site:
        return {"success": False, "error": "Укажи ссылку на сайт, например https://vk.com"}
    if not data.login.strip():
        return {"success": False, "error": "Укажи логин"}
    if not data.password:
        return {"success": False, "error": "Укажи пароль"}
    if save_credentials(data.api_key, data.site, data.login, data.password,
                        url=data.url, title=data.title):
        logger.info(f"Учётка сохранена: {site}")
        return {
            "success": True,
            "message": f"🔐 {data.title or site}: учётка сохранена",
            "site": site,
        }
    return {"success": False, "error": "Не удалось сохранить (проверь папку с server.py на запись)"}

@app.post("/list_credentials")
def list_credentials_endpoint(data: ApiKeyRequest):
    """Список сохранённых сайтов. Пароли наружу не отдаются."""
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    return {"success": True, "sites": list_credentials(data.api_key)}

@app.post("/delete_credentials")
def delete_credentials_endpoint(data: DeleteCredentialsRequest):
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    if delete_credentials(data.api_key, data.site):
        return {"success": True, "message": f"🗑 {data.site}: учётка удалена"}
    return {"success": False, "error": "Такой сайт не найден"}

@app.post("/browse")
def browse(data: BrowseRequest):
    """Открывает ссылку и возвращает текст страницы.

    Раньше здесь был Playwright (полноценный браузер). В этом окружении он не
    запускается: asyncio не может создать pipe для дочернего процесса
    (PermissionError: WinError 5), поэтому Chromium падал всегда. Обычный
    requests работает, поэтому страницу читаем им — для текстовых сайтов
    (новости, дневник, справки) этого достаточно.
    """
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    try:
        # Если для сайта сохранена учётка — сначала входим: иначе закрытая
        # страница отдаст только форму логина, и агент покажет её вместо данных.
        content, logged_in = open_as_user(data.api_key, data.url)
        if not content:
            # Фолбэк без входа: распознаёт капчу/ЕСИА/SPA и честно объясняет.
            content = plain_page_text(data.url, limit=4000)
        if not content:
            return {"success": False, "error": "Страница пустая или не текстовая"}
        return {"success": True, "content": content, "url": data.url, "logged_in": logged_in}
    except Exception as e:
        logger.error(f"browse {data.url}: {e}")
        return {"success": False, "error": f"Не удалось открыть ссылку: {e}"}


@app.post("/news")
def news(data: NewsRequest):
    """Свежие заголовки с RSS-лент + краткий пересказ от нейросети."""
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    limit = max(3, min(int(data.limit or 8), 15))
    source, titles, errors = collect_news(limit=limit)
    if not titles:
        detail = "; ".join(errors) if errors else "ленты не ответили"
        return {"success": False, "error": f"Не удалось получить новости: {detail}"}
    summary = ai_summarize(titles, source)
    answer = format_news(source, titles, summary)
    logger.info(f"Новости: {source}, {len(titles)} заголовков")
    return {"success": True, "answer": answer, "source": source,
            "titles": titles, "summary": summary}


@app.post("/cheatsheet")
def cheatsheet(data: CheatSheetRequest):
    """Шпаргалка по теме: формулы и ключевые тезисы, без «воды».

    Отдельная ручка, а не /ask: тема шпаргалки — не реплика диалога, поэтому
    в историю чата она не пишется и контекст предыдущей переписки на неё не
    влияет (см. комментарий у cheat_sheet в tools.py). Лимит тот же, что у
    /ask: запрос всё равно уходит к нейросети и стоит денег.
    """
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    if not rate_limit(f"cheat_{data.api_key}", max_requests=10, window_seconds=60):
        return {"success": False, "error": "Слишком много запросов — подожди минуту"}
    topic = (data.topic or "").strip()
    if not topic:
        return {"success": False, "error": "Напиши тему"}
    answer = cheat_sheet(topic)
    logger.info(f"Шпаргалка: {topic[:60]}")
    return {"success": True, "answer": answer, "topic": topic}


@app.post("/download_file")
def download_file(data: DownloadRequest):
    """Скачивает файл по ссылке в папку downloads рядом с сервером."""
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}

    parsed = urllib.parse.urlparse(data.url)
    if parsed.scheme not in ("http", "https"):
        return {"success": False, "error": "Нужна ссылка http:// или https://"}
    # Ссылку даёт пользователь, а запрос делает сервер — без этой проверки
    # сюда можно было прислать http://127.0.0.1:8000/... или
    # http://169.254.169.254/ и выкачать внутренний сервис или метаданные
    # облака в файл (та же SSRF-дыра, что уже закрыта в /fetch_url и _download).
    ok, reason = _safe_public_url(data.url)
    if not ok:
        return {"success": False, "error": reason}

    name = os.path.basename(urllib.parse.unquote(parsed.path)) or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]  # безопасное имя для Windows
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    try:
        os.makedirs(folder, exist_ok=True)
        res = requests.get(data.url, timeout=60, headers=browser_headers(), stream=True)
        res.raise_for_status()
        # Перенаправление могло увести на внутренний адрес — проверяем, куда
        # нас в итоге привели, и не сохраняем такую страницу.
        ok, reason = _safe_public_url(res.url)
        if not ok:
            return {"success": False, "error": reason}
        path = os.path.join(folder, name)
        # Не перезаписываем одноимённые файлы — добавляем номер.
        base, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path = f"{base}_{n}{ext}"
            n += 1
        with open(path, "wb") as f:
            for chunk in res.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        size = os.path.getsize(path)
        logger.info(f"Скачан файл: {path} ({size} байт)")
        return {"success": True, "message": f"✅ Скачал «{os.path.basename(path)}» ({size} байт)",
                "path": path, "size": size}
    except Exception as e:
        logger.error(f"download_file {data.url}: {e}")
        return {"success": False, "error": f"Не удалось скачать: {e}"}


@app.post("/upload_file")
def upload_file(data: UploadRequest):
    """Принимает файл с устройства (компьютера или телефона) и кладёт в uploads/.

    Тело — JSON с base64, а не multipart: FastAPI требует для File() пакет
    python-multipart, которого нет в requirements.txt, и без него сервер падал
    бы при старте целиком, а не только эта ручка. Цена решения — base64
    раздувает тело на треть, поэтому размер ограничен (см. MAX_UPLOAD_BYTES).
    """
    if not get_user(data.api_key):
        return {"success": False, "error": "Неверный ключ"}
    if not rate_limit(f"upload_{data.api_key}", max_requests=20, window_seconds=60):
        return {"success": False, "error": "Слишком много загрузок — подожди минуту"}

    # Имя чистим тем же правилом, что и у download_file: пользователь присылает
    # его из браузера, а на выходе получается путь на диске. Без чистки сюда
    # пролезает "../../server.py" и запись уходит выше папки загрузок.
    name = os.path.basename(data.filename or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120]
    if not name or name in (".", ".."):
        name = "file"

    try:
        raw = base64.b64decode(data.content_b64, validate=False)
    except (binascii.Error, ValueError):
        return {"success": False, "error": "Файл повреждён при передаче"}
    # Pydantic уже ограничил длину строки, но проверяем и сами байты: base64
    # декодируется в объёме 3/4 от длины, и без этой проверки лимит по строке
    # не равен лимиту по файлу.
    if len(raw) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return {"success": False, "error": f"Файл больше {limit_mb} МБ"}
    if not raw:
        return {"success": False, "error": "Пустой файл"}

    try:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        path = os.path.join(UPLOADS_DIR, name)
        # Одноимённые не перезаписываем — иначе вторая загрузка молча съест первую.
        base, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path = f"{base}_{n}{ext}"
            n += 1
        with open(path, "wb") as f:
            f.write(raw)
        size = os.path.getsize(path)
        logger.info(f"Загружен файл: {path} ({size} байт)")
        return {"success": True,
                "message": f"✅ Загрузил «{os.path.basename(path)}» ({size} байт)",
                "path": path, "size": size}
    except Exception as e:
        logger.error(f"upload_file {name}: {e}")
        return {"success": False, "error": f"Не удалось сохранить файл: {e}"}

# -------------------------------------------------
# АДМИНКА
# -------------------------------------------------
# Модель AdminRequest приходит из schemas.py — как и все остальные
# транспортные модели. Раньше она одна объявлялась здесь, и из-за этого
# server.py держал у себя кусок HTTP-контракта и импортировал BaseModel.


def is_admin(password: str) -> bool:
    return bool(password) and password == SETTINGS.get("admin_password")


# Админские ручки отвечают и на GET, и на POST: admin_panel.html дергает их
# через fetch() без метода (GET), а тесты и curl иногда шлют POST — раньше
# POST давал 405 Method Not Allowed.
@app.api_route("/admin_stats_data", methods=["GET", "POST"])
def admin_stats_data(admin_password: str = ""):
    """Сводка по базе: пользователи, тарифы, сообщения, доход.

    Пароль обязателен. Раньше ручка была открыта всем: любой, кто знал адрес
    сервера, мог прочитать число пользователей и сообщений.
    """
    if not is_admin(admin_password):
        return {"success": False, "error": "Неверный пароль"}
    stats = {"total_users": 0, "paid_users": 0, "revenue_rub": 0, "total_messages": 0}
    try:
        users = supabase.table("users").select("tariff").execute().data or []
        stats["total_users"] = len(users)
        stats["paid_users"] = sum(1 for u in users if u.get("tariff") not in (None, "free"))
    except Exception as e:
        logger.error(f"stats users: {e}")
    try:
        stats["total_messages"] = len(supabase.table("message_queue").select("id").execute().data or [])
    except Exception as e:
        logger.error(f"stats messages: {e}")
    stats["revenue_rub"] = stats["paid_users"] * 299
    return {"success": True, "stats": stats}


@app.api_route("/admin_users_data", methods=["GET", "POST"])
def admin_users_data(admin_password: str = ""):
    """Список пользователей с ключами. Пароль обязателен.

    Раньше ручка была открыта: любой мог получить api_key всех пользователей
    и пользоваться сервисом от их имени.
    """
    if not is_admin(admin_password):
        return {"success": False, "error": "Неверный пароль"}
    try:
        rows = supabase.table("users").select("name,api_key,tariff,created_at").execute().data or []
        return {"success": True, "users": rows}
    except Exception as e:
        logger.error(f"admin_users_data: {e}")
        return {"success": False, "error": str(e)}


@app.post("/admin_give_sub_data")
def admin_give_sub_data(data: AdminRequest):
    if not is_admin(data.admin_password):
        return {"success": False, "error": "Неверный пароль"}
    try:
        user = get_user(data.api_key)
        if not user:
            return {"success": False, "error": "Ключ не найден"}
        supabase.table("users").update({"tariff": "paid"}).eq("api_key", data.api_key).execute()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.api_route("/admin_settings_data", methods=["GET", "POST"])
def admin_settings_data(admin_password: str = ""):
    """Текущие настройки нейросети. Пароль обязателен, API-ключ отдаётся маской."""
    if not is_admin(admin_password):
        return {"success": False, "error": "Неверный пароль"}
    return {"success": True, "settings": public_settings()}


@app.post("/admin_settings_save")
def admin_settings_save(data: AdminRequest):
    """Меняет путь, API-ключ, модель и параметры генерации. Пустые поля не затирают значения."""
    if not is_admin(data.admin_password):
        return {"success": False, "error": "Неверный пароль"}
    patch = data.patch or {}
    # Пустая строка ключа означает "не менять": в public_settings он показан маской
    if patch.get("ai_api_key") == "":
        patch.pop("ai_api_key", None)
    _settings, saved, rejected = update_settings(patch)
    logger.info(f"Настройки обновлены: {sorted(patch.keys())}")
    # Раньше здесь всегда возвращалось success: True, и админка писала
    # «Сохранено», даже когда на Render папка только для чтения, а Supabase
    # недоступен — то есть когда не сохранилось вообще ничего. Теперь об этом
    # сообщаем честно, вместе с причиной отказа по каждому полю.
    if not saved:
        return {
            "success": False,
            "error": "Настройки применены в памяти, но не сохранились: "
                     "файл недоступен для записи и облако не настроено. "
                     "После перезапуска сервиса значения сбросятся.",
            "rejected": rejected,
            "settings": public_settings(),
        }
    if rejected:
        return {
            "success": True,
            "warning": "Часть значений не принята: " + "; ".join(rejected),
            "rejected": rejected,
            "settings": public_settings(),
        }
    return {"success": True, "settings": public_settings()}


@app.post("/admin_settings_test")
def admin_settings_test(data: AdminRequest):
    """Пробный запрос к нейросети, чтобы проверить путь и ключ до сохранения."""
    if not is_admin(data.admin_password):
        return {"success": False, "error": "Неверный пароль"}
    url = ai_endpoint()
    if not url:
        return {"success": False, "error": "Не задан путь (AI base URL)"}
    try:
        res = requests.post(
            url,
            headers={"Authorization": f"Bearer {SETTINGS.get('ai_api_key','')}", "Content-Type": "application/json"},
            json={
                "model": SETTINGS.get("ai_model", ""),
                "messages": [{"role": "user", "content": "Ответь одним словом: работает"}],
                "max_tokens": 20,
            },
            timeout=30,
        )
        if res.status_code != 200:
            return {"success": False, "error": f"HTTP {res.status_code}: {res.text[:300]}"}
        answer = (res.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"success": True, "answer": answer}
    except Exception as e:
        return {"success": False, "error": str(e)}


# -------------------------------------------------
# СТАТИЧЕСКИЕ СТРАНИЦЫ
# -------------------------------------------------
# Страницы лежат рядом с server.py и раньше отдавались внешним веб-сервером.
# Теперь их отдаёт сам FastAPI, иначе по /index.html и /tasks.html был 404,
# а ссылки на главной вели в никуда. Путь WEB_DIR объявлен в начале файла.


def serve_page(filename: str):
    """Отдаёт HTML-файл из папки сервера; 404, если файла нет."""
    path = os.path.join(WEB_DIR, filename)
    if not os.path.isfile(path):
        return HTMLResponse(f"<h1>{filename} не найден</h1>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/index.html", response_class=HTMLResponse)
def page_index():
    return serve_page("index.html")


@app.get("/tasks.html", response_class=HTMLResponse)
def page_tasks():
    return serve_page("tasks.html")


@app.get("/admin_panel.html", response_class=HTMLResponse)
def page_admin_panel():
    return serve_page("admin_panel.html")


@app.get("/значок.png")
def page_icon():
    """Иконка сайта: отдаём как файл, чтобы браузер не пытался её распарсить."""
    path = os.path.join(WEB_DIR, "значок.png")
    if not os.path.isfile(path):
        return HTMLResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Встроенная админка: статистика пользователей и настройки нейросети."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_panel.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception as e:
        return HTMLResponse(f"<h1>admin_panel.html не найден</h1><p>{e}</p>", status_code=500)


# -------------------------------------------------
# ЗАПУСК
# -------------------------------------------------
if __name__ == "__main__":
    import socket
    import uvicorn

    # Порт берётся из переменной окружения PORT, по умолчанию 8000.
    # Раньше здесь было жёстко 8000, и при живой предыдущей копии сервер
    # падал с WinError 10048 («обычно разрешается только одно использование
    # адреса сокета») без внятного объяснения.
    try:
        PORT = int(os.getenv("PORT", "8000"))
    except ValueError:
        PORT = 8000

    def _port_is_busy(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                return True
        return False

    if _port_is_busy(PORT):
        print()
        print(f"!!! Порт {PORT} уже занят — скорее всего предыдущая копия сервера ещё работает.")
        print("    Что сделать:")
        print(f"      1) закрыть старую копию: Ctrl+C в её окне, либо `taskkill /PID <PID> /F`,")
        print(f"         PID можно найти так:  netstat -ano | findstr :{PORT}")
        print(f"      2) либо запустить на другом порту:  set PORT=8010 && python server.py")
        print("    Готовый запуск с авто-освобождением порта: start_server.cmd")
        print()
        raise SystemExit(1)

    print(f"Запускаю сервер: http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
