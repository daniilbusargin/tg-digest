"""Aiogram 3 — приватный бот владельца с командами /add /list /remove /digest."""
from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from db import add_channel, list_channels, remove_channel
from parser import is_valid_username


log = logging.getLogger(__name__)

OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

# Колбэк ручного запуска дайджеста проставляется из main.py
DigestRunner = Callable[[Message], Awaitable[None]]
_digest_runner: Optional[DigestRunner] = None


def set_digest_runner(runner: DigestRunner) -> None:
    global _digest_runner
    _digest_runner = runner


router = Router(name="owner")


@router.message(F.chat.id != OWNER_CHAT_ID)
async def reject_strangers(message: Message) -> None:
    # Молча игнорируем всех, кроме владельца.
    log.info("Ignored message from chat_id=%s", message.chat.id)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я собираю дайджест публичных Telegram-каналов.\n\n"
        "Команды:\n"
        "/add @channel — добавить канал\n"
        "/remove @channel — удалить канал\n"
        "/list — список каналов\n"
        "/digest — собрать дайджест прямо сейчас"
    )


def _parse_username_arg(command: CommandObject) -> Optional[str]:
    if not command.args:
        return None
    return command.args.strip().split()[0]


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    arg = _parse_username_arg(command)
    if not arg:
        await message.answer("Использование: <code>/add @channel</code>")
        return
    try:
        ch = await add_channel(arg)
    except ValueError as e:
        await message.answer(f"Ошибка: {e}")
        return
    if not is_valid_username(ch.username):
        await message.answer(
            f"⚠️ Канал @{ch.username} сохранён, но имя выглядит нестандартно — проверьте."
        )
        return
    await message.answer(f"✅ Канал @{ch.username} добавлен.")


@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject) -> None:
    arg = _parse_username_arg(command)
    if not arg:
        await message.answer("Использование: <code>/remove @channel</code>")
        return
    ok = await remove_channel(arg)
    if ok:
        await message.answer(f"🗑 Канал {arg} удалён.")
    else:
        await message.answer(f"Канал {arg} не найден.")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    channels = await list_channels()
    if not channels:
        await message.answer("Список каналов пуст. Добавьте: <code>/add @channel</code>")
        return
    lines = ["<b>Каналы:</b>"]
    for ch in channels:
        mark = "✅" if ch.is_active else "⏸"
        last = f" (last_seen={ch.last_seen_msg_id})" if ch.last_seen_msg_id else ""
        lines.append(f"{mark} @{ch.username}{last}")
    await message.answer("\n".join(lines))


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    if _digest_runner is None:
        await message.answer("Сборщик дайджеста ещё не инициализирован.")
        return
    await message.answer("⏳ Собираю дайджест, это займёт минуту…")
    try:
        await _digest_runner(message)
    except Exception as e:
        log.exception("digest failed")
        await message.answer(f"❌ Ошибка сборки дайджеста: {e}")


def build_bot() -> tuple[Bot, Dispatcher]:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not OWNER_CHAT_ID:
        raise RuntimeError("OWNER_CHAT_ID не задан в .env")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
