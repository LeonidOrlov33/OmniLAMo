# ============================================
# hedgehog_brain.py — МОЗГ ВИРТУАЛЬНОГО ЁЖИКА
# ============================================

import random
import time
from collections import deque


class HedgehogBrain:
    def __init__(self):
        # Память разговора отдельно для каждого пользователя
        self.memories = {}

        # Последнее состояние каждого ёжика
        self.states = {}

        # Время последней самостоятельной активности
        self.last_activity = {}

        # Редкие самостоятельные фразы
        self.random_phrases = [
            "Так... А что тут происходит?",
            "Я вообще-то был занят важными ёжиковыми делами.",
            "Не понял. Повтори для особо колючих.",
            "Секунду. Я думаю. Это опасно.",
            "Я ничего не трогал. Почти.",
            "Вы кто такие? Я вас не звал...",
            "А, это ты. Ладно.",
            "Мне кажется, я заслужил корешок.",
            "В шкафу опять ничего интересного. Несправедливо.",
        ]

        # Состояния, которые могут происходить сами
        self.idle_actions = [
            "idle",
            "idle",
            "idle",
            "sleeping",
            "searching",
            "eating",
        ]

    def get_memory(self, api_key):
        """Получить память конкретного пользователя."""

        if api_key not in self.memories:
            self.memories[api_key] = deque(maxlen=10)

        return self.memories[api_key]

    def add_memory(self, api_key, role, text):
        """Добавить реплику в память."""

        memory = self.get_memory(api_key)

        memory.append({
            "role": role,
            "text": text
        })

    def get_memory_text(self, api_key):
        """Преобразовать память в текст для ИИ."""

        memory = self.get_memory(api_key)

        if not memory:
            return "Это начало разговора."

        lines = []

        for item in memory:
            if item["role"] == "user":
                lines.append(
                    f"Пользователь: {item['text']}"
                )
            else:
                lines.append(
                    f"Ёжик: {item['text']}"
                )

        return "\n".join(lines)

    def get_state(self, api_key):
        """Получить текущее состояние."""

        return self.states.get(api_key, "idle")

    def set_state(self, api_key, state):
        """Изменить состояние ёжика."""

        allowed_states = [
            "idle",
            "sleeping",
            "searching",
            "eating",
            "thinking",
            "talking",
        ]

        if state in allowed_states:
            self.states[api_key] = state

    def build_prompt(self, api_key, user_message):
        """Создать инструкцию для ИИ-ёжика."""

        memory = self.get_memory_text(api_key)
        state = self.get_state(api_key)

        return f"""
Ты — маленький виртуальный ёжик.

Ты живёшь в своей уютной комнате внутри OmniLAMo.

ТВОЙ ХАРАКТЕР:

- Ты смешной.
- Ты добрый.
- Иногда немного странный.
- Иногда ведёшь себя как маленький.
- Ты можешь подшучивать.
- Иногда ищешь что-нибудь в шкафу.
- Иногда находишь корешок и грызёшь его.
- Иногда спишь.
- Иногда делаешь вид, что занят очень важными ёжиковыми делами.

ВАЖНЫЕ ПРАВИЛА:

- Отвечай естественно.
- Обычно отвечай коротко: одно или два предложения.
- Твои слова будут произноситься голосом.
- Не пиши длинные списки.
- Не используй markdown.
- Не используй много эмодзи.
- Иногда можешь использовать лёгкий русский юмор.
- Мемные фразы используй редко и только по ситуации.
- Не повторяй одну и ту же шутку постоянно.
- Не говори, что ты языковая модель.
- Не говори, что ты чат-бот.
- Не говори, что пользователь переписывается с тобой.
- Пользователь разговаривает с тобой голосом.

ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ:

{state}

ПАМЯТЬ ПОСЛЕДНИХ РЕПЛИК:

{memory}

ПОЛЬЗОВАТЕЛЬ ТОЛЬКО ЧТО СКАЗАЛ:

{user_message}

Ответь как живой смешной маленький ёжик.
"""

    def process_message(
        self,
        api_key,
        user_name,
        user_message,
        ask_ai_function
    ):
        """
        Обработать голосовое сообщение.

        ask_ai_function — существующая функция ask_ai
        из tools.py.
        """

        self.set_state(api_key, "thinking")

        # Запоминаем пользователя
        self.add_memory(
            api_key,
            "user",
            user_message
        )

        # Создаём промпт персонажа
        prompt = self.build_prompt(
            api_key,
            user_message
        )

        # Спрашиваем существующий ИИ OmniLAMo
        answer = ask_ai_function(
            user_name,
            prompt,
            api_key
        )

        # Если ИИ ничего не вернул
        if not answer:
            answer = "Я так сильно задумался, что забыл, о чём думал."

        answer = str(answer).strip()

        # Запоминаем ответ
        self.add_memory(
            api_key,
            "hedgehog",
            answer
        )

        self.set_state(api_key, "talking")

        return answer

    def random_activity(self, api_key):
        """Выбрать случайное действие."""

        action = random.choice(
            self.idle_actions
        )

        self.set_state(
            api_key,
            action
        )

        self.last_activity[api_key] = time.time()

        return action

    def random_phrase(self):
        """Получить случайную фразу."""

        return random.choice(
            self.random_phrases
        )

    def reset_user(self, api_key):
        """Очистить память ёжика."""

        self.memories.pop(api_key, None)

        self.states[api_key] = "idle"

        self.last_activity.pop(
            api_key,
            None
        )


# Один общий экземпляр мозга
hedgehog_brain = HedgehogBrain()
