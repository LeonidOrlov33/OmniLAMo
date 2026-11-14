# ============================================
# hedgehog_brain.py — МОЗГ ВИРТУАЛЬНОГО ЁЖИКА
# ============================================

import random
from collections import deque


class HedgehogBrain:
    def __init__(self):
        # Отдельная короткая память для каждого пользователя.
        self.memories = {}

        # Текущее состояние ёжика для каждого пользователя.
        self.states = {}

        # Короткие самостоятельные реплики.
        self.random_phrases = [
            "Так, а что тут происходит?",
            "Я вообще-то был занят важными ёжиковыми делами.",
            "Не понял. Повтори для особо колючих.",
            "Секунду. Я думаю. Это опасно.",
            "Я ничего не трогал. Почти.",
            "Вы кто такие? Я вас не звал...",
            "А, это ты. Ладно.",
            "Мне кажется, я заслужил корешок.",
            "В шкафу опять ничего интересного. Несправедливо.",
        ]

        # idle встречается чаще, чтобы ёжик не был слишком активным.
        self.idle_actions = [
            "idle",
            "idle",
            "idle",
            "sleeping",
            "searching",
            "eating",
        ]

    def get_memory(self, api_key):
        if api_key not in self.memories:
            self.memories[api_key] = deque(maxlen=10)
        return self.memories[api_key]

    def add_memory(self, api_key, role, text):
        self.get_memory(api_key).append({
            "role": role,
            "text": str(text).strip(),
        })

    def get_memory_text(self, api_key):
        memory = self.get_memory(api_key)

        if not memory:
            return "Это начало разговора."

        lines = []

        for item in memory:
            role = "Пользователь" if item["role"] == "user" else "Ёжик"
            lines.append(f"{role}: {item['text']}")

        return "\n".join(lines)

    def get_state(self, api_key):
        return self.states.get(api_key, "idle")

    def set_state(self, api_key, state):
        allowed_states = {
            "idle",
            "sleeping",
            "searching",
            "eating",
            "thinking",
            "talking",
        }

        if state in allowed_states:
            self.states[api_key] = state

    def build_prompt(self, api_key, user_message):
        memory = self.get_memory_text(api_key)
        state = self.get_state(api_key)

        return f"""
Ты — маленький виртуальный ёжик.

Ты живёшь один в своей уютной комнате внутри OmniLAMo.
В комнате есть шкаф. Иногда ты сидишь, спишь, ищешь что-нибудь в шкафу,
находишь корешок и грызёшь его.

ТВОЙ ХАРАКТЕР:
- Ты смешной, добрый и немного странный.
- Иногда ведёшь себя как маленький.
- Иногда подшучиваешь.
- Иногда делаешь вид, что занят очень важными ёжиковыми делами.
- Ты разговариваешь естественно и по-доброму.

ВАЖНЫЕ ПРАВИЛА:
- Обычно отвечай коротко: одно или два предложения.
- Твой ответ будет озвучен голосом, поэтому пиши естественно для речи.
- Не используй markdown.
- Не пиши длинные списки без необходимости.
- Не используй много эмодзи.
- Лёгкие русские мемные фразы используй редко и только по ситуации.
- Не повторяй одну и ту же шутку постоянно.
- Не говори, что ты языковая модель, нейросеть или чат-бот.
- Не говори, что пользователь переписывается с тобой: он разговаривает с тобой голосом.
- Не придумывай семью в обычном разговоре без причины. Семья — часть историй
  персонажа для видео, а в OmniLAMo ты находишься в своей комнате.

ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ:
{state}

ПОСЛЕДНИЕ РЕПЛИКИ:
{memory}

ПОЛЬЗОВАТЕЛЬ ТОЛЬКО ЧТО СКАЗАЛ:
{user_message}

Ответь как живой смешной маленький ёжик.
""".strip()

    def process_message(self, api_key, user_name, user_message, ask_ai_function):
        self.set_state(api_key, "thinking")

        self.add_memory(api_key, "user", user_message)

        prompt = self.build_prompt(api_key, user_message)

        answer = ask_ai_function(
            user_name,
            prompt,
            api_key,
        )

        if not answer:
            answer = "Я так сильно задумался, что забыл, о чём думал."

        answer = str(answer).strip()

        self.add_memory(api_key, "hedgehog", answer)
        self.set_state(api_key, "talking")

        return answer

    def random_activity(self, api_key):
        action = random.choice(self.idle_actions)
        self.set_state(api_key, action)
        return action

    def random_phrase(self):
        return random.choice(self.random_phrases)

    def reset_user(self, api_key):
        self.memories.pop(api_key, None)
        self.states[api_key] = "idle"


hedgehog_brain = HedgehogBrain()
