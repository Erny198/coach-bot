"""Чистый вызов LLM без привязки к Telegram — для веб-кабинета.

Переиспользует тот же Anthropic-клиент и модель, что и бот, но не тянет
telegram-слой. Логирует затраты в БД (llm_calls). В тестах подменяется моком.
"""
from __future__ import annotations

import asyncio
import logging
import os

import anthropic

from services import access

logger = logging.getLogger(__name__)

_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_PRICE_IN = 3.00 / 1_000_000
_PRICE_OUT = 15.00 / 1_000_000

_client: anthropic.Anthropic | None = None


def init() -> None:
    global _client
    key = os.getenv("ANTHROPIC_API_KEY", "")
    _client = anthropic.Anthropic(api_key=key) if key else None


async def ask(system: str, history: list[dict], user_message: str,
              tg_id: int | None = None, mode: str | None = None) -> str:
    if _client is None:
        return "⚠️ ИИ не настроен (нет ANTHROPIC_API_KEY)."
    messages = list(history) + [{"role": "user", "content": user_message}]
    try:
        resp = await asyncio.to_thread(
            _client.messages.create,
            model=_MODEL, max_tokens=2048,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        parts = [b.text for b in (resp.content or []) if getattr(b, "type", "") == "text"]
        reply = "\n".join(p.strip() for p in parts if p and p.strip())
        if tg_id is not None:
            ti = getattr(resp.usage, "input_tokens", 0)
            to = getattr(resp.usage, "output_tokens", 0)
            try:
                await access.log_llm(tg_id, mode, ti, to, ti * _PRICE_IN + to * _PRICE_OUT)
            except Exception:
                logger.debug("llm log skipped", exc_info=True)
        return reply or "⚠️ Пустой ответ."
    except Exception:
        logger.exception("LLM error")
        return "⚠️ Ошибка обращения к ИИ. Попробуй ещё раз."
