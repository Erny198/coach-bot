"""Интеграция с ЮKassa поверх REST API (через httpx — уже в зависимостях).

create_payment — создать платёж и получить confirmation_url.
fetch_status  — перезапросить статус (источник правды для вебхука).
Ключи берутся из .env (YK_SHOP_ID / YK_SECRET_KEY) — подставим позже.
"""
from __future__ import annotations

import uuid

import httpx

from config import pricing, settings

API = "https://api.yookassa.ru/v3/payments"


def _auth() -> tuple[str, str]:
    return (settings.YK_SHOP_ID, settings.YK_SECRET_KEY)


def _amount(tier: str, period: str) -> dict:
    value = pricing.price(tier, period, "rub")
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


def _receipt(email: str | None, tier: str, period: str) -> dict | None:
    """Чек 54-ФЗ. ЮKassa требует customer.email, если включены «Чеки»."""
    if not settings.YK_RECEIPTS_ENABLED or not email:
        return None
    title = pricing.TIERS.get(tier, {}).get("title", "Подписка")
    amount = _amount(tier, period)
    return {
        "customer": {"email": email},
        "items": [{
            "description": f"Подписка «{title}» ({'год' if period == 'year' else 'месяц'})",
            "quantity": "1.00",
            "amount": amount,
            "vat_code": 1,                 # без НДС — уточнить под юрлицо
            "payment_subject": "service",
            "payment_mode": "full_payment",
        }],
    }


async def create_payment(
    tg_id: int, tier: str, period: str, return_url: str, email: str | None = None,
) -> dict:
    """Создать платёж. Возвращает JSON ЮKassa (id, confirmation.confirmation_url, status)."""
    body: dict = {
        "amount": _amount(tier, period),
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Подписка {tier}/{period}, tg {tg_id}",
        "metadata": {"tg_id": str(tg_id), "tier": tier, "period": period},
    }
    receipt = _receipt(email, tier, period)
    if receipt:
        body["receipt"] = receipt

    headers = {"Idempotence-Key": str(uuid.uuid4())}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(API, json=body, headers=headers, auth=_auth())
        r.raise_for_status()
        return r.json()


async def fetch_status(payment_id: str) -> dict:
    """Перезапросить платёж — доверяем этому, а не телу вебхука."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/{payment_id}", auth=_auth())
        r.raise_for_status()
        return r.json()
