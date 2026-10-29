"""
Discord-бот «Катя». Вся платформенная логика здесь,
общая (OpenRouter/промпт/роли) — в kate_common.py.
"""

import logging
import os

import httpx
import discord
from discord.ext import commands

import kate_common

kate_common.setup_logging()
logger = logging.getLogger("kate.discord")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан")

kate_common.init("discord")

http_client: httpx.AsyncClient | None = None

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    global http_client
    http_client = httpx.AsyncClient(timeout=kate_common.REQUEST_TIMEOUT)
    logger.info("Катя (Discord) запущена! (%s). Модель: %s", bot.user, kate_common.MODEL)
    logger.info("Владелец: %s | Друзья: %s | VIP: %s",
                kate_common.OWNER_ID, kate_common.FRIEND_IDS, kate_common.VIP_IDS)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return
    if not bot.user.mentioned_in(message):
        return

    author_name = message.author.display_name
    author_id = message.author.id

    prompt = (message.content
              .replace(f"<@!{bot.user.id}>", "")
              .replace(f"<@{bot.user.id}>", "")
              .strip())

    if not prompt:
        await message.channel.send("Слушаю, госпожа. Что нужно сделать?")
        return

    messages = kate_common.build_messages(author_id, author_name, prompt)

    async with message.channel.typing():
        try:
            answer = await kate_common.ask_openrouter(messages, http_client)
        except Exception:
            logger.exception("Ошибка при запросе к OpenRouter")
            await message.channel.send(kate_common.error_message_for(author_id))
            return

    for chunk in kate_common.split_message(answer, kate_common.DISCORD_LIMIT):
        await message.channel.send(chunk)


@bot.command(name="log")
async def log_cmd(ctx: commands.Context):
    author_name = ctx.author.display_name
    author_id = ctx.author.id
    status = kate_common.get_user_status(author_id)
    role_text = kate_common.role_ins(author_name, author_id)
    await ctx.send(
        f"**Автор:** {author_name} (`{author_id}`)\n"
        f"**Роль:** `{status}`\n"
        f"**Инструкция роли:** {role_text}"
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)