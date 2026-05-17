"""Entrypoint: инициализация БД, регистрация бота, запуск polling."""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot  # noqa: E402
from aiogram.types import Message  # noqa: E402
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from bot import OWNER_CHAT_ID, build_bot, set_digest_runner  # noqa: E402
from db import init_db  # noqa: E402
from graph import run_digest  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("main")


TG_MAX = 3900  # запас под HTML-разметку (лимит Telegram ~4096)


def _split_for_telegram(text: str, limit: int = TG_MAX) -> list[str]:
    """Разбивает markdown по абзацам, не превышая лимит сообщения."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = (buf + "\n\n" + para).strip() if buf else para
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            if len(para) <= limit:
                buf = para
            else:
                # Слишком длинный абзац — режем грубо
                for i in range(0, len(para), limit):
                    chunks.append(para[i : i + limit])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


async def send_digest_to_owner(bot: Bot, digest_md: str) -> None:
    for chunk in _split_for_telegram(digest_md):
        await bot.send_message(
            OWNER_CHAT_ID,
            chunk,
            disable_web_page_preview=True,
            parse_mode=None,  # markdown отдаём как plain — Telegram MD капризен к ссылкам
        )


async def execute_digest(bot: Bot, reply_to: Message | None = None) -> None:
    state = await run_digest()
    digest_md: str = state.get("digest") or "_Сегодня ничего нового._"
    errors: list[str] = state.get("errors") or []

    await send_digest_to_owner(bot, digest_md)
    if errors:
        err_text = "⚠️ Во время сборки были ошибки:\n" + "\n".join(f"• {e}" for e in errors[:20])
        await bot.send_message(OWNER_CHAT_ID, err_text)


def _build_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = os.getenv("SCHEDULE_TZ", "Europe/Moscow")
    hour = int(os.getenv("SCHEDULE_HOUR", "9"))
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))

    scheduler = AsyncIOScheduler(timezone=tz)

    async def _job() -> None:
        log.info("scheduled digest started")
        try:
            await execute_digest(bot)
        except Exception:
            log.exception("scheduled digest failed")

    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily-digest",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    log.info("Scheduler: daily-digest at %02d:%02d %s", hour, minute, tz)
    return scheduler


async def amain() -> None:
    await init_db()
    bot, dp = build_bot()

    async def _runner(message: Message) -> None:
        await execute_digest(bot, reply_to=message)

    set_digest_runner(_runner)

    scheduler = _build_scheduler(bot)
    scheduler.start()

    log.info("Bot is starting (polling)…")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(amain())
