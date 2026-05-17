"""Парсер публичного веб-превью Telegram-канала: https://t.me/s/{username}.

Возвращает список словарей {msg_id, text, url, posted_at}. Авторизация не требуется,
никаких приватных API не используется — только HTML с открытой страницы канала.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import aiohttp
from selectolax.parser import HTMLParser, Node


log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE_URL = "https://t.me/s/{username}"
REQUEST_TIMEOUT = 20


def _extract_msg_id(post_node: Node) -> Optional[int]:
    data_post = post_node.attributes.get("data-post")
    if not data_post:
        return None
    parts = data_post.rsplit("/", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _extract_text(post_node: Node) -> str:
    text_node = post_node.css_first(".tgme_widget_message_text")
    if not text_node:
        return ""
    # selectolax выдаёт текст с учётом <br/>
    return text_node.text(separator="\n", strip=True)


def _extract_datetime(post_node: Node) -> Optional[datetime]:
    time_node = post_node.css_first(".tgme_widget_message_date time")
    if not time_node:
        return None
    dt_attr = time_node.attributes.get("datetime")
    if not dt_attr:
        return None
    try:
        # формат: 2024-05-14T18:30:00+00:00
        return datetime.fromisoformat(dt_attr)
    except ValueError:
        return None


def _parse_html(html: str, username: str) -> list[dict]:
    tree = HTMLParser(html)
    posts: list[dict] = []
    for node in tree.css(".tgme_widget_message_wrap .tgme_widget_message"):
        msg_id = _extract_msg_id(node)
        if msg_id is None:
            continue
        text = _extract_text(node)
        if not text:
            # пропускаем чисто-медиа посты без текста — нечего саммаризировать
            continue
        posts.append({
            "msg_id": msg_id,
            "text": text,
            "url": f"https://t.me/{username}/{msg_id}",
            "posted_at": _extract_datetime(node),
        })
    posts.sort(key=lambda p: p["msg_id"])
    return posts


async def fetch_channel_posts(
    username: str,
    session: aiohttp.ClientSession,
    since_msg_id: int = 0,
) -> list[dict]:
    """Скачивает HTML страницы канала и возвращает посты с msg_id > since_msg_id."""
    url = BASE_URL.format(username=username)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("t.me/s/%s вернул HTTP %s", username, resp.status)
                return []
            html = await resp.text()
    except Exception as e:
        log.warning("Ошибка загрузки t.me/s/%s: %s", username, e)
        return []

    posts = _parse_html(html, username)
    if since_msg_id:
        posts = [p for p in posts if p["msg_id"] > since_msg_id]
    return posts


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def is_valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))
