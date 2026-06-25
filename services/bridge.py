"""HTTP-мост (aiohttp, 127.0.0.1) между ботом, Mini App и ЮKassa.

Наружу через nginx проксируются только /pay/*. Маршруты:
  GET  /pay/enter?t=<token>   — проверка токена → cookie coach_sess → 302 на /
  GET  /pay/landing           — отдаёт Mini App только при валидной cookie
  POST /pay/create            — создать платёж ЮKassa (tg_id из cookie)
  POST /pay/webhook/<token>   — вебхук ЮKassa: перезапрос статуса → выдача доступа

Бизнес-логика вынесена в чистые функции (begin_checkout / process_succeeded),
чтобы её можно было тестировать без поднятия HTTP.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

from aiohttp import web

from config import pricing, settings
from db import queries as q
from db.session import AsyncSessionLocal
from services import access, payments, sessions, texts

logger = logging.getLogger(__name__)

_LANDING = os.path.join(os.path.dirname(os.path.dirname(__file__)), "landing", "index.html")
_STUB = "<!doctype html><meta charset=utf-8><title>CoachDojo</title><h2>Откройте страницу из бота.</h2>"
COOKIE = "coach_sess"


def webhook_token() -> str:
    """Стабильный токен в пути вебхука (секрет не светим в URL открытым текстом)."""
    return hmac.new(settings.HMAC_SECRET.encode(), b"yookassa-webhook", hashlib.sha256).hexdigest()[:24]


def _return_url() -> str:
    base = f"https://{settings.DOMAIN}" if settings.DOMAIN else "https://example.com"
    return f"{base}/pay/done"


# ── Чистая бизнес-логика (тестируемая без HTTP) ───────────────────────────
async def begin_checkout(tg_id: int, tier: str, period: str) -> str:
    """Создать платёж ЮKassa и вернуть confirmation_url. Кидает ValueError при
    некорректном тарифе."""
    if tier not in pricing.TIERS or period not in pricing.PERIODS:
        raise ValueError("bad tier/period")
    async with AsyncSessionLocal() as s:
        user = await q.get_or_create_user(s, tg_id)
        email = await q.set_email_if_absent(s, user) if settings.YK_RECEIPTS_ENABLED else None
        await s.commit()

    data = await payments.create_payment(tg_id, tier, period, _return_url(), email)
    yk_id = data.get("id")
    confirmation = (data.get("confirmation") or {}).get("confirmation_url", "")

    async with AsyncSessionLocal() as s:
        await q.create_payment_row(
            s, tg_id, tier, period, float(pricing.price(tier, period, "rub")),
            yk_payment_id=yk_id,
        )
        await q.log_event(s, tg_id, "checkout_started", {"tier": tier, "period": period})
        await s.commit()
    return confirmation


async def process_succeeded(bot, payment_id: str) -> bool:
    """Перезапросить статус и идемпотентно выдать доступ. Возвращает True, если
    платёж успешен (в т.ч. если уже был обработан ранее)."""
    data = await payments.fetch_status(payment_id)
    if data.get("status") != "succeeded":
        return False
    meta = data.get("metadata") or {}
    tg_id = int(meta.get("tg_id", 0))
    tier = meta.get("tier")
    period = meta.get("period")
    if not tg_id or tier not in pricing.TIERS or period not in pricing.PERIODS:
        logger.warning("webhook: bad metadata %s", meta)
        return False

    async with AsyncSessionLocal() as s:
        existing = await q.get_payment_by_yk(s, payment_id)
        already = bool(existing and existing.status == "succeeded")
        if existing:
            await q.mark_payment_succeeded(s, payment_id)
        await s.commit()
    if already:
        return True  # идемпотентность: доступ уже выдан, не дублируем

    await access.apply_subscription(tg_id, tier, period)
    if bot is not None:
        title = pricing.TIERS[tier]["title"]
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=f"✅ Оплата прошла. Подписка «{title}» активна. Приятной практики! Нажми /start.",
            )
        except Exception as e:
            logger.warning("notify after payment failed tg=%s: %s", tg_id, e)
    return True


# ── HTTP-обработчики ──────────────────────────────────────────────────────
async def _enter(request: web.Request) -> web.StreamResponse:
    tg_id = sessions.verify(request.query.get("t"))
    if tg_id is None:
        return web.Response(text=_STUB, content_type="text/html", status=403)
    resp = web.HTTPFound("/")
    resp.set_cookie(COOKIE, sessions.sign(tg_id), max_age=30 * 86400,
                    httponly=True, secure=True, samesite="Lax")
    return resp


async def _landing(request: web.Request) -> web.Response:
    tg_id = sessions.verify(request.cookies.get(COOKIE))
    if tg_id is None:
        return web.Response(text=_STUB, content_type="text/html", status=403)
    try:
        with open(_LANDING, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        html = _STUB
    return web.Response(text=html, content_type="text/html",
                        headers={"X-Robots-Tag": "noindex"})


async def _create(request: web.Request) -> web.Response:
    tg_id = sessions.verify(request.cookies.get(COOKIE))
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    try:
        url = await begin_checkout(tg_id, body.get("tier", ""), body.get("period", ""))
    except ValueError:
        return web.json_response({"error": "bad tier/period"}, status=400)
    except Exception as e:
        logger.exception("checkout failed")
        return web.json_response({"error": "payment_error", "detail": str(e)}, status=502)
    return web.json_response({"confirmation_url": url})


async def _tariffs(request: web.Request) -> web.Response:
    """Тарифы для Mini App — из единого источника цен."""
    return web.json_response({"tiers": pricing.tiers_public()})


async def _webhook(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.match_info.get("token", ""), webhook_token()):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    obj = body.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return web.json_response({"ok": True})  # игнорируем нерелевантные события
    try:
        await process_succeeded(request.app["bot"], payment_id)
    except Exception:
        logger.exception("webhook processing failed")
        # 200, чтобы ЮKassa не retry-флудила; статус всё равно перезапросим.
    return web.json_response({"ok": True})


def build_app(bot=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.get("/pay/enter", _enter),
        web.get("/pay/landing", _landing),
        web.get("/pay/tariffs", _tariffs),
        web.post("/pay/create", _create),
        web.post("/pay/webhook/{token}", _webhook),
    ])
    # Веб-кабинет /app/* на том же мосте.
    from services import webapp
    webapp.add_routes(app)
    return app


async def start_bridge(bot, host: str = "127.0.0.1", port: int = 8080) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Bridge started on %s:%s (webhook token=%s)", host, port, webhook_token())
    return runner
