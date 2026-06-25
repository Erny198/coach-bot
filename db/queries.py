"""Доступ к данным: пользователи, подписки, лимиты, аналитика.

Все функции принимают AsyncSession. Бизнес-логику доступа (что значит «есть
подписка / исчерпан лимит») держим здесь, чтобы bot.py только вызывал хелперы.
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import pricing, settings
from db.models import LlmCall, Payment, ScheduledMessage, User, UserEvent, WebMessage


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite может вернуть naive datetime — приводим к UTC-aware для сравнений."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


# ── Пользователи ──────────────────────────────────────────────────────────
async def get_or_create_user(
    session: AsyncSession, tg_id: int, username: str | None = None, name: str | None = None,
) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(tg_id=tg_id, username=username, name=name)
        session.add(user)
    else:
        if username and user.username != username:
            user.username = username
        if name and user.name != name:
            user.name = name
        user.last_seen = _utcnow()
    # Унаследованный полный доступ для текущего whitelist. Применяем каждый раз,
    # когда видим grandfathered-username и ещё нет тарифа (в т.ч. при миграции,
    # где username мог появиться не сразу). Реальную покупку не перетираем.
    if (user.username in settings.GRANDFATHERED_USERNAMES
            and user.subscription_tier is None):
        user.subscription_tier = "all"
        user.access_until = _utcnow() + dt.timedelta(days=3650)
    await session.flush()
    return user


async def set_email_if_absent(session: AsyncSession, user: User) -> str:
    """Служебный email для чека ЮKassa (54-ФЗ), если ещё не задан."""
    if not user.email:
        domain = settings.DOMAIN or "coachbot.local"
        user.email = f"tg{user.tg_id}@{domain}"
        await session.flush()
    return user.email


# ── Подписки ────────────────────────────────────────────────────────────--
async def grant_subscription(
    session: AsyncSession, tg_id: int, tier: str, period: str, *, is_trial: bool = False,
) -> User:
    """Выдать/продлить подписку. Идемпотентно по сроку: если доступ ещё активен,
    продлеваем от текущего access_until, иначе — от now."""
    user = await get_or_create_user(session, tg_id)
    days = pricing.PERIODS.get(period, 30)
    now = _utcnow()
    base = _aware(user.access_until) if (user.access_until and _aware(user.access_until) > now) else now
    user.subscription_tier = tier
    user.access_until = base + dt.timedelta(days=days)
    user.is_trial = is_trial
    await session.flush()
    return user


def has_active_subscription(user: User) -> bool:
    return bool(user.access_until and _aware(user.access_until) > _utcnow())


def can_use_mode(user: User, bot_mode: str) -> tuple[bool, str]:
    """Главная проверка доступа к режиму.

    Возвращает (allowed, reason):
      reason ∈ {"subscription", "free", "limit", "paywall"}.
      allowed=True при активной подписке, покрывающей режим, ИЛИ если ещё есть
      бесплатные действия. allowed=False → нужно показать paywall.
    """
    flag = pricing.BOT_MODE_TO_FLAG.get(bot_mode)
    if flag is None:
        return False, "paywall"

    # 1) Активная подписка, покрывающая режим
    if has_active_subscription(user) and flag in pricing.tier_modes(user.subscription_tier or ""):
        return True, "subscription"

    # 2) Бесплатные лимиты
    counter_key = pricing.BOT_MODE_TO_FREE_COUNTER.get(bot_mode)
    if counter_key is None:
        return False, "paywall"  # напр. supervisor — без бесплатных действий
    used = _free_counter_value(user, counter_key)
    limit = pricing.FREE_LIMITS.get(counter_key, 0)
    if used < limit:
        return True, "free"
    return False, "limit"


_COUNTER_ATTR = {
    "coach": "free_coach_used",
    "coachee": "free_coachee_used",
    "trainer_topics": "free_trainer_topics",
    "instrument_cards": "free_instr_cards",
}


def _free_counter_value(user: User, counter_key: str) -> int:
    return getattr(user, _COUNTER_ATTR[counter_key], 0)


async def consume_free_action(session: AsyncSession, user: User, bot_mode: str) -> None:
    """Увеличить счётчик бесплатных действий для режима (вызывать только когда
    действие реально было бесплатным, т.е. reason == 'free')."""
    counter_key = pricing.BOT_MODE_TO_FREE_COUNTER.get(bot_mode)
    if counter_key is None:
        return
    attr = _COUNTER_ATTR[counter_key]
    setattr(user, attr, getattr(user, attr, 0) + 1)
    await session.flush()


# ── Аналитика и затраты ─────────────────────────────────────────────────--
async def log_event(session: AsyncSession, tg_id: int, event: str, props: dict | None = None) -> None:
    session.add(UserEvent(tg_id=tg_id, event=event,
                          props_json=json.dumps(props, ensure_ascii=False) if props else None))
    await session.flush()


async def log_llm_call(
    session: AsyncSession, tg_id: int, mode: str | None, tokens_in: int, tokens_out: int,
    cost_usd: float = 0.0,
) -> None:
    session.add(LlmCall(tg_id=tg_id, mode=mode, tokens_in=tokens_in,
                        tokens_out=tokens_out, cost_usd=cost_usd))
    user = await session.get(User, tg_id)
    if user:
        user.total_input_tokens += tokens_in
        user.total_output_tokens += tokens_out
    await session.flush()


# ── Платежи ───────────────────────────────────────────────────────────────
async def create_payment_row(
    session: AsyncSession, tg_id: int, tier: str, period: str, amount: float,
    currency: str = "RUB", yk_payment_id: str | None = None,
) -> Payment:
    p = Payment(tg_id=tg_id, tier=tier, period=period, amount=amount,
                currency=currency, yk_payment_id=yk_payment_id, status="pending")
    session.add(p)
    await session.flush()
    return p


async def get_payment_by_yk(session: AsyncSession, yk_payment_id: str) -> Payment | None:
    return (await session.execute(
        select(Payment).where(Payment.yk_payment_id == yk_payment_id)
    )).scalar_one_or_none()


async def mark_payment_succeeded(session: AsyncSession, yk_payment_id: str) -> Payment | None:
    """Идемпотентно: повторный вебхук не выдаст доступ дважды."""
    p = (await session.execute(
        select(Payment).where(Payment.yk_payment_id == yk_payment_id)
    )).scalar_one_or_none()
    if p is None or p.status == "succeeded":
        return p
    p.status = "succeeded"
    p.confirmed_at = _utcnow()
    await session.flush()
    return p


# ── Расписание сообщений ────────────────────────────────────────────────--
async def schedule_message(
    session: AsyncSession, tg_id: int, kind: str, send_at: dt.datetime, payload: dict | None = None,
) -> None:
    session.add(ScheduledMessage(
        tg_id=tg_id, kind=kind, send_at=send_at,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    ))
    await session.flush()


async def add_web_message(session: AsyncSession, tg_id: int, thread: str, role: str, content: str) -> None:
    session.add(WebMessage(tg_id=tg_id, thread=thread, role=role, content=content))
    await session.flush()


async def get_web_history(session: AsyncSession, tg_id: int, thread: str, limit: int = 20) -> list[dict]:
    """Последние `limit` сообщений треда в хронологическом порядке (для LLM)."""
    rows = (await session.execute(
        select(WebMessage).where(WebMessage.tg_id == tg_id, WebMessage.thread == thread)
        .order_by(WebMessage.id.desc()).limit(limit)
    )).scalars().all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


async def web_thread_len(session: AsyncSession, tg_id: int, thread: str) -> int:
    from sqlalchemy import func
    return (await session.execute(
        select(func.count()).select_from(WebMessage)
        .where(WebMessage.tg_id == tg_id, WebMessage.thread == thread)
    )).scalar() or 0


async def reset_web_thread(session: AsyncSession, tg_id: int, thread: str) -> None:
    rows = (await session.execute(
        select(WebMessage).where(WebMessage.tg_id == tg_id, WebMessage.thread == thread)
    )).scalars().all()
    for r in rows:
        await session.delete(r)
    await session.flush()


async def clear_unsent(session: AsyncSession, tg_id: int, kind: str) -> None:
    """Снять (пометить отправленными) неотправленные сообщения данного типа —
    чтобы при продлении/повторе не копились дубли."""
    rows = (await session.execute(
        select(ScheduledMessage).where(
            ScheduledMessage.tg_id == tg_id,
            ScheduledMessage.kind == kind,
            ScheduledMessage.sent == False,  # noqa: E712
        )
    )).scalars().all()
    for r in rows:
        r.sent = True
    await session.flush()


async def mark_sent(session: AsyncSession, msg_id: int) -> None:
    msg = await session.get(ScheduledMessage, msg_id)
    if msg:
        msg.sent = True
        await session.flush()


async def due_messages(session: AsyncSession, now: dt.datetime | None = None) -> list[ScheduledMessage]:
    now = now or _utcnow()
    rows = (await session.execute(
        select(ScheduledMessage).where(
            ScheduledMessage.sent == False,  # noqa: E712
            ScheduledMessage.send_at <= now,
        )
    )).scalars().all()
    return list(rows)
