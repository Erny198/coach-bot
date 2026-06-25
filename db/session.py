"""Async-движок SQLAlchemy + SQLite в режиме WAL.

WAL даёт параллельные чтения и сериализованную запись — для одного бота-сервера
этого достаточно на тысячи пользователей. Переезд на Postgres = смена DATABASE_URL.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Включаем WAL и разумные PRAGMA на каждом соединении SQLite.
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()
    except Exception:
        # Не SQLite (например, Postgres) — PRAGMA не нужны.
        pass


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal
