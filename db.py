"""SQLAlchemy 2.0 async-модели и хелперы поверх aiosqlite."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, Boolean, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


DB_PATH = os.getenv("DB_PATH", "digest.db")
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_msg_id: Mapped[int] = mapped_column(BigInteger, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    posts: Mapped[list["Post"]] = relationship(back_populates="channel", cascade="all, delete-orphan")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("channel_id", "msg_id", name="uq_channel_msg"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    msg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    channel: Mapped[Channel] = relationship(back_populates="posts")


engine = create_async_engine(DB_URL, echo=False, future=True)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _normalize_username(raw: str) -> str:
    u = raw.strip()
    if u.startswith("https://t.me/"):
        u = u[len("https://t.me/"):]
    if u.startswith("t.me/"):
        u = u[len("t.me/"):]
    if u.startswith("s/"):
        u = u[2:]
    u = u.lstrip("@").rstrip("/")
    return u.lower()


async def add_channel(raw_username: str) -> Channel:
    username = _normalize_username(raw_username)
    if not username:
        raise ValueError("Пустой username канала")

    async with SessionLocal() as session:
        existing = await session.scalar(select(Channel).where(Channel.username == username))
        if existing:
            if not existing.is_active:
                existing.is_active = True
                await session.commit()
            return existing
        channel = Channel(username=username, is_active=True, last_seen_msg_id=0)
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


async def remove_channel(raw_username: str) -> bool:
    username = _normalize_username(raw_username)
    async with SessionLocal() as session:
        ch = await session.scalar(select(Channel).where(Channel.username == username))
        if not ch:
            return False
        await session.delete(ch)
        await session.commit()
        return True


async def list_channels(active_only: bool = False) -> list[Channel]:
    async with SessionLocal() as session:
        stmt = select(Channel).order_by(Channel.username)
        if active_only:
            stmt = stmt.where(Channel.is_active.is_(True))
        result = await session.scalars(stmt)
        return list(result.all())


async def get_channel(raw_username: str) -> Optional[Channel]:
    username = _normalize_username(raw_username)
    async with SessionLocal() as session:
        return await session.scalar(select(Channel).where(Channel.username == username))


async def save_posts(channel_id: int, posts: list[dict]) -> list[Post]:
    """Сохраняет новые посты, возвращает реально вставленные."""
    if not posts:
        return []
    saved: list[Post] = []
    async with SessionLocal() as session:
        existing_ids = set(
            (await session.scalars(
                select(Post.msg_id).where(Post.channel_id == channel_id)
            )).all()
        )
        for p in posts:
            if p["msg_id"] in existing_ids:
                continue
            row = Post(
                channel_id=channel_id,
                msg_id=p["msg_id"],
                text=p.get("text", ""),
                url=p.get("url", ""),
                posted_at=p.get("posted_at"),
            )
            session.add(row)
            saved.append(row)
        await session.commit()
        for row in saved:
            await session.refresh(row)
    return saved


async def update_last_seen(channel_id: int, msg_id: int) -> None:
    async with SessionLocal() as session:
        ch = await session.get(Channel, channel_id)
        if ch and msg_id > ch.last_seen_msg_id:
            ch.last_seen_msg_id = msg_id
            await session.commit()
