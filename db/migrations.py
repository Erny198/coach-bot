"""Инициализация схемы и перенос данных из старых JSON-файлов в БД.

init_db() — идемпотентно создаёт таблицы (CREATE TABLE IF NOT EXISTS через
checkfirst). Будущие изменения схемы добавлять сюда отдельными идемпотентными
ALTER (без create_all в проде на существующих таблицах).

migrate_legacy_json() — однократно переносит user_names.json и user_activity.json
(имена и накопленные токены) в таблицу users. Whitelist переносить не нужно:
он работает через GRANDFATHERED_USERNAMES при первом контакте.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from config import settings
from db.models import Base
from db.queries import get_or_create_user
from db.session import AsyncSessionLocal, engine


async def init_db() -> None:
    settings.ensure_data_dir()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


async def migrate_legacy_json(data_dir: str | None = None) -> dict:
    """Перенос имён и активности. Возвращает счётчики для лога. Идемпотентно:
    повторный запуск просто обновит существующие записи."""
    data_dir = data_dir or settings.DATA_DIR
    names = _read_json(os.path.join(data_dir, "user_names.json")) or {}
    activity = _read_json(os.path.join(data_dir, "user_activity.json")) or {}

    migrated_names = migrated_activity = 0
    async with AsyncSessionLocal() as session:
        for tg_id_str, name in names.items():
            user = await get_or_create_user(session, int(tg_id_str), name=name)
            user.name = name
            migrated_names += 1
        for tg_id_str, entry in activity.items():
            user = await get_or_create_user(
                session, int(tg_id_str), username=entry.get("username"),
            )
            user.total_input_tokens = int(entry.get("total_input_tokens", 0))
            user.total_output_tokens = int(entry.get("total_output_tokens", 0))
            fs = entry.get("first_seen")
            if fs:
                user.created_at = dt.datetime.fromtimestamp(fs, dt.timezone.utc)
            migrated_activity += 1
        await session.commit()

    return {"names": migrated_names, "activity": migrated_activity}
