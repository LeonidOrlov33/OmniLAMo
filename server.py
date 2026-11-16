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
    FetchUrlRequest, HistoryRequest, LoginRequest, NewsRequest,
    RegisterRequest, RememberRequest, SaveCredentialsRequest, UploadRequest,
    HedgehogRequest, HedgehogActivityRequest,
)
from schemas import MAX_UPLOAD_BYTES
from hedgehog_brain import hedgehog_brain
# Функции и константы движка.
from tools import (
    add_task, ai_endpoint, ai_summarize, ask_ai, browser_headers,
    chat_log_page, cheat_sheet, check_user_password, clean_old_messages,
    clear_history, cloud_ready,
    collect_news, complete_task, connect_services, create_user, delete_credentials,
    delete_user_account, dequeue_messages, detect_command, enqueue_message,
    execute_command,
    format_news, get_tasks, get_user, get_user_by_name, list_credentials,
    load_settings_from_cloud, log_chat, logger, mark_queue_done,
    normalize_site, open_as_user,
    plain_page_text, probe_cloud_tables, public_settings, rate_limit,
    save_credentials, save_user_auth, set_user_password,
    take_scheduled_messages, update_settings,
    user_account_stats,
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
