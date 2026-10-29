"""
Telegram-бот «Катя» на aiogram 3.x.
Общая логика — в kate_common.py.
В личке — на любое текстовое сообщение; в группе — на @mention или реплай боту.
"""

import asyncio
import logging
import os

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

import kate_common

kate_common.setup_logging()
logger = logging.getLogger("kate.telegram")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан")

kate_common.init("telegram")

dp = Dispatcher()
http_client: httpx.AsyncClient | None = None
_bot_username: str | None = None


def _clean_mention(text: str) -> str:
    if not text:
        return ""
    if _bot_username and f"@{_bot_username}" in text:
        text = text.replace(f"@{_bot_username}", "")
    return text.strip()


def _is_addressed_to_bot(message: Message, bot_id: int) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True
    if message.reply_to_message and message.reply_to_message.from_user \
            and message.reply_to_message.from_user.id == bot_id:
        return True
    text = message.text or ""
    if _bot_username and f"@{_bot_username}" in text:
        return True
    return False


@dp.message(Command("log"))
async def log_cmd(message: Message):
    user = message.from_user
    if not user:
        return
    author_name = user.full_name or "?"
    status = kate_common.get_user_status(user.id)
    role_text = kate_common.role_ins(author_name, user.id)
    await message.reply(
        f"<b>Автор:</b> {author_name} (<code>{user.id}</code>)\n"
        f"<b>Роль:</b> <code>{status}</code>\n"
        f"<b>Инструкция роли:</b> {role_text}",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def on_message(message: Message, bot: Bot):
    user = message.from_user
    if not user or user.is_bot:
        return
    if not _is_addressed_to_bot(message, bot.id):
        return

    prompt = _clean_mention(message.text)
    if not prompt:
        await message.reply("Слушаю, госпожа. Что нужно сделать?")
        return

    author_name = user.full_name or "Пользователь"
    messages = kate_common.build_messages(user.id, author_name, prompt)

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            answer = await kate_common.ask_openrouter(messages, http_client)
        except Exception:
            logger.exception("Ошибка при запросе к OpenRouter")
            await message.reply(kate_common.error_message_for(user.id))
            return

    for chunk in kate_common.split_message(answer, kate_common.TELEGRAM_LIMIT):
        await message.reply(chunk)


async def _run():
    global http_client, _bot_username
    http_client = httpx.AsyncClient(timeout=kate_common.REQUEST_TIMEOUT)
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        me = await bot.me()
        _bot_username = me.username
        logger.info("Катя (Telegram) запущена! (@%s). Модель: %s",
                    _bot_username, kate_common.MODEL)
        logger.info("Владелец: %s | Друзья: %s | VIP: %s",
                    kate_common.OWNER_ID, kate_common.FRIEND_IDS, kate_common.VIP_IDS)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await http_client.aclose()


if __name__ == "__main__":
    asyncio.run(_run())