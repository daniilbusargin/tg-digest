"""Entrypoint: инициализация БД, регистрация бота, запуск polling."""
from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from aiogram.types import Message  # noqa: E402

from bot import build_bot, set_digest_runner  # noqa: E402
from db import init_db  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("main")


async def _digest_stub(message: Message) -> None:
    # Будет заменено реализацией LangGraph в следующей фазе.
    await message.answer("⚠️ Дайджест ещё не подключён (фаза 4).")


async def amain() -> None:
    await init_db()
    bot, dp = build_bot()
    set_digest_runner(_digest_stub)
    log.info("Bot is starting (polling)…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(amain())
