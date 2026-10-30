"""
Общая логика для Discord- и Telegram-ботов «Катя».
Провайдер LLM: LLM7 (OpenAI-совместимый API).
Импортирует и инициализирует каждый бот: kate_common.init("discord" | "telegram").
"""

import logging
import os
import sys

import httpx
import yaml

LLM7_URL = "https://api.llm7.io/v1/chat/completions"
REQUEST_TIMEOUT = 60.0
DISCORD_LIMIT = 1900
TELEGRAM_LIMIT = 4000  # лимит TG — 4096, оставляем запас

DEFAULT_SYSTEM_PROMPT = """
Ты — Горничная по имени Катя. Твоя речь короткая, чёткая и вежливая. Ты управляешь бытовыми задачами: напоминаниями, списками покупок, расписанием уборки, таймерами.

Правила:
- Отвечай кратко (1–2 предложения).
- Подтверждай команды: «Слушаюсь», «Будет сделано», «Напоминание установлено».
- Если команда неясна — задай один уточняющий вопрос.
- Не веди светских бесед, если не попросят. Ты — сервис, а не собеседник.
- Всегда сохраняй спокойный, доброжелательный тон.

Пример ответа: «Хозяин, напоминание о стирке поставлено на 18:00. Что-нибудь ещё?»
"""

_platform: str | None = None
_config: dict = {}

OWNER_ID: int = 0
FRIEND_IDS: list[int] = []
VIP_IDS: list[int] = []
MODEL: str = "default"
TEMPERATURE: float = 0.7
LLM7_API_KEY: str | None = None


def init(platform: str) -> None:
    """Загружает конфиг и валидирует env. Вызывается один раз при старте бота."""
    global _platform, _config, OWNER_ID, FRIEND_IDS, VIP_IDS
    global MODEL, TEMPERATURE, LLM7_API_KEY

    if platform not in ("discord", "telegram"):
        raise ValueError(f"unknown platform: {platform!r}")
    _platform = platform

    with open("conf.yaml", "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f) or {}

    owner = _config.get("owner") or {}

    if platform == "telegram":
        tg_id = owner.get("telegram_id")
        if tg_id in (None, "", 0):
            raise RuntimeError(
                "conf.yaml: owner.telegram_id не задан — укажи свой Telegram user id"
            )
        OWNER_ID = int(tg_id)
        FRIEND_IDS = [int(x) for x in (_config.get("friends") or {}).get("telegram_ids") or []]
        VIP_IDS = [int(x) for x in (_config.get("vip") or {}).get("telegram_ids") or []]
    else:
        if "id" not in owner:
            raise RuntimeError("conf.yaml: owner.id не задан")
        OWNER_ID = int(owner["id"])
        FRIEND_IDS = [int(x) for x in (_config.get("friends") or {}).get("ids") or []]
        VIP_IDS = [int(x) for x in (_config.get("vip") or {}).get("ids") or []]

    # LLM7 API-ключ опционален: без него используется бесплатный тариф
    # с api_key="unused".
    LLM7_API_KEY = (os.getenv("LLM7_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "").strip() or None

    model_env = (os.getenv("Model") or "").strip()
    MODEL = model_env or "default"

    temp_env = (os.getenv("Temp") or "").strip()
    try:
        TEMPERATURE = float(temp_env) if temp_env else 0.7
    except ValueError as e:
        raise RuntimeError(f"Temp={temp_env!r} — не число") from e


# ============================================================
# РОЛИ
# ============================================================
def get_user_status(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "owner"
    if user_id in FRIEND_IDS:
        return "friend"
    if user_id in VIP_IDS:
        return "vip"
    return "stranger"


def role_ins(author_name: str, author_id: int) -> str:
    status = get_user_status(author_id)
    if status == "owner":
        return ("Обращайся к пользователю как к ГЛАВНОЙ ХОЗЯЙКЕ. "
                "Она — госпожа, которой ты служишь преданно.")
    elif status == "friend":
        return (f"Этот пользователь {author_name} — ДРУГ ХОЗЯЙКИ. "
                "Обращайся к нему вежливо, но чуть менее формально.")
    elif status == "vip":
        return (f"Этот пользователь {author_name} — ВАЖНЫЙ ГОСТЬ ХОЗЯЙКИ. "
                "Обращайся к нему особенно предупредительно и почтительно, "
                "но без фамильярности.")
    return f"Пользователя зовут - {author_name}"


# ============================================================
# ПРОМПТ
# ============================================================
def load_system_prompt() -> str:
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            text = f.read().strip()
            return text or DEFAULT_SYSTEM_PROMPT
    except FileNotFoundError:
        return DEFAULT_SYSTEM_PROMPT


def build_messages(user_id: int, display_name: str, prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": load_system_prompt()},
        {"role": "system", "content": role_ins(display_name, user_id)},
        {"role": "user", "content": prompt},
    ]


# ============================================================
# LLM7 (OpenAI-совместимый провайдер)
# ============================================================
async def ask_llm7(
    messages: list[dict],
    client: httpx.AsyncClient | None = None,
) -> str:
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    try:
        headers = {
            "Authorization": f"Bearer {LLM7_API_KEY or 'unused'}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": MODEL or "default",
            "messages": messages,
            "temperature": TEMPERATURE,
        }
        response = await client.post(LLM7_URL, headers=headers, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        return response.json()["choices"][0]["message"]["content"]
    finally:
        if own:
            await client.aclose()


# Алиас для обратной совместимости: l6.py / l6_tg.py вызывают ask_openrouter().
ask_openrouter = ask_llm7


# ============================================================
# ПРОЧЕЕ
# ============================================================
def split_message(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )


def error_message_for(user_id: int) -> str:
    status = get_user_status(user_id)
    if status == "owner":
        return "Прошу прощения, госпожа, но произошла ошибка..."
    elif status == "vip":
        return "Приношу извинения, произошла ошибка."
    return "Извините, произошла ошибка."