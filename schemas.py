# ============================================
# schemas.py — ТРАНСПОРТНЫЕ МОДЕЛИ ЗАПРОСОВ (HTTP-слой)
# ============================================
# Раньше эти Pydantic-модели лежали внутри tools.py — движка с бизнес-
# логикой. Из-за этого выходило, что транспортный слой (валидация тела
# HTTP-запроса) и логика приложения физически в одном файле: правка формы
# запроса требовала трогать движок, а сам движок оказывался «зацеплен» за
# FastAPI-контракт.
#
# Теперь модели живут отдельно. tools.py импортирует их отсюда (обратная
# совместимость: старый код, который делал `from tools import AskRequest`,
# продолжает работать), а server.py берёт их напрямую.
# ============================================

import re

from pydantic import BaseModel, Field, validator


# -------------------------------------------------
# ОБЩИЕ ПРОВЕРКИ
# -------------------------------------------------
def is_valid_name(name: str) -> bool:
    """Имя пользователя: 2–20 символов, буквы/цифры/пробел/дефис."""
    return bool(re.match(r"^[а-яА-Яa-zA-Z0-9\s\-]{2,20}$", name or ""))


# Длина ключа у всех запросов одна и та же. Раньше ограничение стояло только
# у /ask (30..100), а в остальных моделях api_key был вообще без границ: можно
# было прислать строку на мегабайт, и сервер честно тащил её в базу и в логи.
# 64 символа — это два uuid4.hex, как в /register и в telegram_agent/bot.py;
# запас до 128 оставляем на случай смены формата ключей.
API_KEY_FIELD = Field(..., min_length=30, max_length=128)


# -------------------------------------------------
# МОДЕЛИ ЗАПРОСОВ
# -------------------------------------------------
class RegisterRequest(BaseModel):
    name: str

    @validator("name")
    def validate_name(cls, v):
        if not is_valid_name(v):
            raise ValueError("Имя от 2 до 20 символов")
        return v.strip()


class AskRequest(BaseModel):
    # Тот же предел, что и у API_KEY_FIELD ниже: ключ — это два uuid4.hex (64),
    # и держать здесь 100, пока остальные модели принимают 128, смысла нет.
    api_key: str = Field(..., min_length=30, max_length=128)
    text: str = Field(..., min_length=1, max_length=2000)

    @validator("text")
    def validate_text(cls, v):
        if "<script" in v.lower() or "javascript:" in v.lower():
            raise ValueError("Вредоносный код")
        return v.strip()


class ApiKeyRequest(BaseModel):
    api_key: str = API_KEY_FIELD


class HistoryRequest(BaseModel):
    """Страница истории чата: before_seq — листать реплики старее этого номера."""
    api_key: str = API_KEY_FIELD
    before_seq: int = 0
    limit: int = 20


class FetchUrlRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    url: str


class RememberRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    task: str = Field(..., min_length=1, max_length=2000)


class CompleteTaskRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    task_id: str


class SaveCredentialsRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    site: str = ""      # домен; если пусто — вычислим из url
    login: str
    password: str
    url: str = ""        # ссылка на сайт
    title: str = ""      # человекочитаемое название ("Мой банк")


class DeleteCredentialsRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    site: str


class BrowseRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    url: str


class DownloadRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    url: str


class NewsRequest(BaseModel):
    api_key: str = API_KEY_FIELD
    topic: str = ""
    limit: int = 8


# Админские ручки. Раньше эта модель была объявлена прямо в server.py —
# единственная из тринадцати, оставшаяся в веб-слое: остальные уже переехали
# сюда. Из-за этого server.py продолжал импортировать BaseModel из pydantic
# и держал у себя кусок транспортного контракта. Теперь все модели запросов
# лежат в одном месте, а server.py только импортирует их.
class AdminRequest(BaseModel):
    admin_password: str = ""
    api_key: str = ""
    days: int = 30
    patch: dict = {}
