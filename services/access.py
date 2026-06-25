"""Сервис доступа: единая точка для bot.py.

Каждый вызов открывает короткую async-сессию БД, гарантирует наличие юзера,
принимает решение о доступе к режиму и (при бесплатном действии) списывает лимит.
bot.py не работает с БД напрямую — только через эти функции.
"""
from __future__ import annotations

import datetime as dt

from db import queries as q
from db.session import AsyncSessionLocal


async def touch_user(tg_id: int, username: str | None = None, name: str | None = None) -> dict:
    """Зарегистрировать/обновить юзера. Возвращает снимок для лога/онбординга."""
    async with AsyncSessionLocal() as s:
        u = await q.get_or_create_user(s, tg_id, username, name)
        snap = {
            "tg_id": u.tg_id,
            "tier": u.subscription_tier,
            "active": q.has_active_subscription(u),
            "onboarding_done": u.onboarding_done,
            "role": u.role,
            "name": u.name,
        }
        await s.commit()
        return snap


async def check_mode(
    tg_id: int, username: str | None, name: str | None, bot_mode: str, *, consume: bool = True,
) -> tuple[bool, str]:
    """Проверить доступ к режиму. Если действие бесплатное и consume=True —
    сразу списать лимит. Возвращает (allowed, reason)."""
    async with AsyncSessionLocal() as s:
        u = await q.get_or_create_user(s, tg_id, username, name)
        allowed, reason = q.can_use_mode(u, bot_mode)
        if allowed and reason == "free" and consume:
            await q.consume_free_action(s, u, bot_mode)
        await q.log_event(s, tg_id, "mode_gate",
                          {"mode": bot_mode, "allowed": allowed, "reason": reason})
        await s.commit()
        return allowed, reason


async def set_onboarding(tg_id: int, role: str | None = None, done: bool = True) -> None:
    async with AsyncSessionLocal() as s:
        u = await q.get_or_create_user(s, tg_id)
        u.onboarding_done = done
        if role:
            u.role = role
        await q.log_event(s, tg_id, "onboarding_done", {"role": role})
        await s.commit()


async def log_event(tg_id: int, event: str, props: dict | None = None) -> None:
    async with AsyncSessionLocal() as s:
        await q.log_event(s, tg_id, event, props)
        await s.commit()


async def log_llm(tg_id: int, mode: str | None, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    async with AsyncSessionLocal() as s:
        await q.log_llm_call(s, tg_id, mode, tokens_in, tokens_out, cost_usd)
        await s.commit()


# ── Подписка + автонапоминания ────────────────────────────────────────────
async def apply_subscription(
    tg_id: int, tier: str, period: str, *, is_trial: bool = False,
) -> dict:
    """Выдать подписку и поставить напоминание об истечении за 3 дня до конца.
    Старые неотправленные «expiring» снимаем, чтобы не было дублей. Этим
    пользуется вебхук оплаты (этап 4)."""
    async with AsyncSessionLocal() as s:
        user = await q.grant_subscription(s, tg_id, tier, period, is_trial=is_trial)
        await q.clear_unsent(s, tg_id, "expiring")
        until = user.access_until
        if until is not None:
            if until.tzinfo is None:
                until = until.replace(tzinfo=dt.timezone.utc)
            send_at = until - dt.timedelta(days=3)
            if send_at > dt.datetime.now(dt.timezone.utc):
                await q.schedule_message(s, tg_id, "expiring", send_at)
        await q.log_event(s, tg_id, "purchase", {"tier": tier, "period": period, "trial": is_trial})
        snap = {"tier": user.subscription_tier, "until": user.access_until.isoformat() if user.access_until else None}
        await s.commit()
        return snap


async def schedule_winback(tg_id: int, after_days: int = 3) -> None:
    async with AsyncSessionLocal() as s:
        await q.clear_unsent(s, tg_id, "winback")
        await q.schedule_message(
            s, tg_id, "winback",
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=after_days),
        )
        await s.commit()


async def schedule_nudge(tg_id: int, after_days: int = 1) -> None:
    async with AsyncSessionLocal() as s:
        await q.clear_unsent(s, tg_id, "nudge")
        await q.schedule_message(
            s, tg_id, "nudge",
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=after_days),
        )
        await s.commit()
