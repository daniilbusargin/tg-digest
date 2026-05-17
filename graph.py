"""LangGraph StateGraph: fetch → (cond) → summarize → assemble.

Это сердце проекта и учебная цель — пайплайн собран именно через StateGraph
с TypedDict-состоянием, условным ребром после fetch и map-этапом через
asyncio.Semaphore в summarize_node. Чекпоинтер — SqliteSaver.
"""
from __future__ import annotations

import asyncio
import logging
import operator
import os
from typing import Annotated, Any, TypedDict

import aiohttp
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from db import list_channels, save_posts, update_last_seen
from parser import fetch_channel_posts


log = logging.getLogger(__name__)


# ---------- Pydantic-схема, в которую LLM должна уложить саммари ----------


class PostSummary(BaseModel):
    title: str = Field(..., description="Короткий заголовок поста (до 80 символов)")
    key_points: list[str] = Field(..., description="2–5 ключевых тезисов поста")
    category: str = Field(..., description="Короткая категория: новости/туториал/мнение/анонс/др.")


# ---------- состояние графа ----------


class DigestState(TypedDict, total=False):
    # Список постов, накопленных fetch-узлом. Каждый элемент:
    # {channel_id, channel_username, msg_id, text, url}
    new_posts: list[dict]
    # Аккумулируется map-этапом; благодаря reducer'у safe для конкурентного append
    summaries: Annotated[list, operator.add]
    # Итоговый markdown
    digest: str
    errors: list[str]


# ---------- настройка LLM ----------


def _build_llm() -> ChatOllama:
    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        temperature=0.2,
    )


_PARSER = PydanticOutputParser(pydantic_object=PostSummary)

_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "Ты — редактор технического дайджеста. По одному посту из публичного "
        "Telegram-канала верни краткое структурированное саммари СТРОГО на русском. "
        "Не выдумывай факты, которых нет в исходном тексте.\n"
        "{format_instructions}",
    ),
    (
        "human",
        "Канал: @{channel}\nСсылка: {url}\n\nТекст поста:\n{text}",
    ),
]).partial(format_instructions=_PARSER.get_format_instructions())


# ---------- узлы ----------


async def fetch_node(state: DigestState) -> DigestState:
    """Парсит активные каналы, сохраняет новые посты, обновляет last_seen_msg_id."""
    channels = await list_channels(active_only=True)
    new_posts: list[dict] = []
    errors: list[str] = []

    async with aiohttp.ClientSession() as session:
        for ch in channels:
            try:
                posts = await fetch_channel_posts(
                    ch.username, session, since_msg_id=ch.last_seen_msg_id
                )
            except Exception as e:
                log.exception("fetch failed for %s", ch.username)
                errors.append(f"@{ch.username}: {e}")
                await asyncio.sleep(1.5)
                continue

            if posts:
                saved = await save_posts(ch.id, posts)
                max_id = max(p["msg_id"] for p in posts)
                await update_last_seen(ch.id, max_id)
                for row in saved:
                    new_posts.append({
                        "channel_id": ch.id,
                        "channel_username": ch.username,
                        "msg_id": row.msg_id,
                        "text": row.text,
                        "url": row.url,
                    })
                log.info("fetched %s: %d new", ch.username, len(saved))
            else:
                log.info("fetched %s: 0 new", ch.username)

            # Вежливая пауза между каналами
            await asyncio.sleep(1.5)

    return {"new_posts": new_posts, "errors": errors, "summaries": []}


def _route_after_fetch(state: DigestState) -> str:
    return "summarize" if state.get("new_posts") else END


async def summarize_node(state: DigestState) -> DigestState:
    """Map-этап: каждое сообщение → PostSummary через LLM (Semaphore=2)."""
    posts: list[dict] = state.get("new_posts", [])
    if not posts:
        return {"summaries": []}

    llm = _build_llm()
    chain = _PROMPT | llm | _PARSER
    sem = asyncio.Semaphore(2)
    errors: list[str] = list(state.get("errors", []))

    async def _one(post: dict) -> dict | None:
        async with sem:
            try:
                summary: PostSummary = await chain.ainvoke({
                    "channel": post["channel_username"],
                    "url": post["url"],
                    "text": post["text"][:4000],  # на всякий случай ограничим
                })
                return {
                    "channel_username": post["channel_username"],
                    "msg_id": post["msg_id"],
                    "url": post["url"],
                    "title": summary.title,
                    "key_points": summary.key_points,
                    "category": summary.category,
                }
            except Exception as e:
                log.exception("summarize failed: %s", post.get("url"))
                errors.append(f"{post.get('url')}: {e}")
                return None

    results = await asyncio.gather(*(_one(p) for p in posts))
    summaries = [r for r in results if r is not None]
    return {"summaries": summaries, "errors": errors}


def _assemble_markdown(summaries: list[dict]) -> str:
    if not summaries:
        return "_Сегодня ничего нового._"

    by_channel: dict[str, list[dict]] = {}
    for s in summaries:
        by_channel.setdefault(s["channel_username"], []).append(s)

    lines: list[str] = ["# Дайджест", ""]
    for username in sorted(by_channel):
        items = sorted(by_channel[username], key=lambda x: x["msg_id"])
        lines.append(f"## @{username}")
        lines.append("")
        for s in items:
            lines.append(f"### [{s['title']}]({s['url']})  _·_ {s['category']}")
            for kp in s["key_points"]:
                lines.append(f"- {kp}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def assemble_node(state: DigestState) -> DigestState:
    digest = _assemble_markdown(state.get("summaries", []))
    return {"digest": digest}


# ---------- сборка графа ----------


def build_graph(checkpointer: Any | None = None):
    g = StateGraph(DigestState)
    g.add_node("fetch", fetch_node)
    g.add_node("summarize", summarize_node)
    g.add_node("assemble", assemble_node)

    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", _route_after_fetch, {"summarize": "summarize", END: END})
    g.add_edge("summarize", "assemble")
    g.add_edge("assemble", END)

    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()


def open_checkpointer() -> SqliteSaver:
    """SqliteSaver на отдельной БД, чтобы не смешивать с прикладными таблицами."""
    db_path = os.getenv("LANGGRAPH_DB_PATH", "langgraph_checkpoints.db")
    # SqliteSaver — синхронный контекст-менеджер, используем .from_conn_string
    return SqliteSaver.from_conn_string(db_path)


async def run_digest() -> DigestState:
    """Запускает пайплайн один раз и возвращает финальное состояние."""
    with open_checkpointer() as saver:
        app = build_graph(checkpointer=saver)
        config = {"configurable": {"thread_id": "daily-digest"}}
        # Стартовое состояние пустое — узлы сами наполнят
        result = await app.ainvoke({"new_posts": [], "summaries": [], "errors": []}, config=config)
        return result  # type: ignore[return-value]
