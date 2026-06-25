"""Планировщик отложенных сообщений: тик раз в минуту.

Читает таблицу scheduled_messages, отправляет наступившие сообщения через бот
и помечает их отправленными. Для «expiring» дополнительно проверяет, что
подписка всё ещё близка к концу (а не была продлена) — иначе тихо снимает.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from db import queries as q
from db.session import AsyncSessionLocal
from services import texts

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


async def tick(bot) -> int:
    """Один проход. Возвращает число отправленных сообщений."""
    sent = 0
    async with AsyncSessionLocal() as s:
        due = await q.due_messages(s)
        for msg in due:
            send_it = True
            if msg.kind == "expiring":
                user = await q.get_or_create_user(s, msg.tg_id)
                # Не слать, если подписки уже нет или её продлили далеко вперёд.
                if not q.has_active_subscription(user):
                    send_it = False
                else:
                    until = user.access_until
                    if until and until.tzinfo is None:
                        until = until.replace(tzinfo=dt.timezone.utc)
                    if until and (until - dt.datetime.now(dt.timezone.utc)) > dt.timedelta(days=5):
                        send_it = False  # продлили — напоминание неактуально
            if send_it:
                try:
                    await bot.send_message(chat_id=msg.tg_id, text=texts.reminder_text(msg.kind))
                    await q.log_event(s, msg.tg_id, "reminder_sent", {"kind": msg.kind})
                    sent += 1
                except Exception as e:  # юзер заблокировал бота и т.п.
                    logger.warning("reminder send failed tg=%s kind=%s: %s", msg.tg_id, msg.kind, e)
            await q.mark_sent(s, msg.id)
        await s.commit()
    return sent


async def run_loop(bot, interval: int = TICK_SECONDS) -> None:
    """Фоновый цикл. Запускается из post_init через asyncio.create_task."""
    logger.info("Scheduler loop started (interval=%ss)", interval)
    while True:
        try:
            n = await tick(bot)
            if n:
                logger.info("Scheduler sent %d reminder(s)", n)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
