"""ORM-модели (SQLAlchemy 2.x). Подписки, платежи, аналитика, расписание.

Диалоговые сессии (история, фазы) здесь НЕ хранятся — они остаются в текущем
механизме бота. В БД — только то, что должно переживать рестарты и нужно для
монетизации и метрик.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Служебный email для чека ЮKassa (54-ФЗ). Подставляется автоматически.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Подписка
    subscription_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    access_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)

    # Счётчики бесплатных действий (см. config/pricing.FREE_LIMITS)
    free_coach_used: Mapped[int] = mapped_column(Integer, default=0)
    free_coachee_used: Mapped[int] = mapped_column(Integer, default=0)
    free_trainer_topics: Mapped[int] = mapped_column(Integer, default=0)
    free_instr_cards: Mapped[int] = mapped_column(Integer, default=0)

    # Учёт затрат (агрегаты; детальный лог — в llm_calls)
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)

    onboarding_done: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)  # сегмент из онбординга

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    payments: Mapped[list["Payment"]] = relationship(back_populates="user")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    yk_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    tier: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(8))   # month / year
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending/succeeded/canceled
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")


class UserEvent(Base):
    """Продуктовая аналитика (воронка): start, onboarding_done, aha, paywall_shown,
    purchase, ... Свойства события — в props_json (строка JSON)."""
    __tablename__ = "user_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(String(48), index=True)
    props_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LlmCall(Base):
    """Детальный лог обращений к LLM для учёта затрат (видно в будущей CRM)."""
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WebMessage(Base):
    """История диалогов веб-кабинета. thread = режим (+тема для тренера),
    напр. 'coach', 'coachee', 'supervisor', 'trainer:score'. Прогресс общий с
    ботом через tg_id, но история веб-сессий хранится отдельно от Telegram-сессий."""
    __tablename__ = "web_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    thread: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))   # user / assistant
    content: Mapped[str] = mapped_column(Text)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SeenCase(Base):
    """Какие курируемые кейсы тренировки пользователь уже получал — чтобы не
    повторять. Когда все кейсы темы просмотрены, движок очищает их и цикл идёт заново."""
    __tablename__ = "seen_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic: Mapped[str] = mapped_column(String(48), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ScheduledMessage(Base):
    """Очередь отложенных сообщений: окончание триала, истечение подписки,
    win-back, nudge. Тик scheduler читает send_at и шлёт неотправленные."""
    __tablename__ = "scheduled_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # trial_end/expiring/winback/nudge
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
