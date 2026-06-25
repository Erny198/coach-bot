"""Веб-кабинет Е-Коуч (браузер). Вход по персональной ссылке из бота,
гейт по подписанной cookie, прогресс/подписка/лимиты — общие с ботом (та же БД).

Маршруты (проксируются nginx как /app/*):
  GET  /app/enter?t=<token>   — короткоживущий токен → cookie → 302 /app
  GET  /app                   — кабинет (только при валидной cookie)
  GET  /app/api/state         — подписка, доступные режимы, прогресс
  GET  /app/api/topics        — темы тренера
  GET  /app/api/instrument/{k}— список вопросов инструмента
  POST /app/api/chat          — сообщение в режим (доступ/лимиты как в боте)
  POST /app/api/reset         — очистить тред режима
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

from db import queries as q
from db.session import AsyncSessionLocal
from config import pricing
from services import access, llm, sessions
from knowledge_base import (
    COACH_SYSTEM_PROMPT, COACH_TOOLBOX, COACHEE_SYSTEM_PROMPT, SUPERVISOR_SYSTEM_PROMPT,
    get_trainer_prompt, TRAINER_TOPICS, INSTRUMENTS, INSTRUMENT_QUESTIONS,
    BLOCKS, TOPIC_BLOCKS,
)

logger = logging.getLogger(__name__)

APP_COOKIE = "coach_app"
_APP_HTML = os.path.join(os.path.dirname(os.path.dirname(__file__)), "landing", "app.html")
_STUB = "<!doctype html><meta charset=utf-8><title>Е-Коуч</title><h2>Откройте веб-версию из бота — по персональной ссылке.</h2>"

SYSTEM_BY_MODE = {
    "coach": lambda topic: COACH_SYSTEM_PROMPT + COACH_TOOLBOX,
    "coachee": lambda topic: COACHEE_SYSTEM_PROMPT,
    "supervisor": lambda topic: SUPERVISOR_SYSTEM_PROMPT,
    "trainer": lambda topic: get_trainer_prompt(topic) if topic else None,
}


def _thread(mode: str, topic: str | None) -> str:
    return f"{mode}:{topic}" if (mode == "trainer" and topic) else mode


def _auth(request: web.Request) -> int | None:
    return sessions.verify(request.cookies.get(APP_COOKIE))


# ── Страницы ──────────────────────────────────────────────────────────────
async def _enter(request: web.Request) -> web.StreamResponse:
    tg_id = sessions.verify(request.query.get("t"))
    if tg_id is None:
        return web.Response(text=_STUB, content_type="text/html", status=403)
    await access.log_event(tg_id, "web_enter")
    resp = web.HTTPFound("/app")
    resp.set_cookie(APP_COOKIE, sessions.sign(tg_id, ttl_seconds=86400),
                    max_age=86400, httponly=True, secure=True, samesite="Lax")
    return resp


async def _app(request: web.Request) -> web.Response:
    if _auth(request) is None:
        return web.Response(text=_STUB, content_type="text/html", status=403)
    try:
        with open(_APP_HTML, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        html = _STUB
    return web.Response(text=html, content_type="text/html", headers={"X-Robots-Tag": "noindex"})


# ── API ────────────────────────────────────────────────────────────────────
async def _state(request: web.Request) -> web.Response:
    tg_id = _auth(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    async with AsyncSessionLocal() as s:
        u = await q.get_or_create_user(s, tg_id)
        modes = sorted(pricing.tier_modes(u.subscription_tier or "")) if q.has_active_subscription(u) else []
        free = {
            "coach": (u.free_coach_used, pricing.FREE_LIMITS["coach"]),
            "coachee": (u.free_coachee_used, pricing.FREE_LIMITS["coachee"]),
            "trainer_topics": (u.free_trainer_topics, pricing.FREE_LIMITS["trainer_topics"]),
            "instrument_cards": (u.free_instr_cards, pricing.FREE_LIMITS["instrument_cards"]),
        }
        state = {
            "name": u.name, "tier": u.subscription_tier,
            "active": q.has_active_subscription(u),
            "until": u.access_until.isoformat() if u.access_until else None,
            "modes": modes, "free": free,
        }
        await s.commit()
    return web.json_response(state)


async def _topics(request: web.Request) -> web.Response:
    if _auth(request) is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    blocks = []
    for bid, emoji, name in BLOCKS:
        items = []
        for k in TOPIC_BLOCKS.get(bid, []):
            v = TRAINER_TOPICS.get(k)
            if v:
                items.append({"key": k, "name": v.get("short_name") or v["name"], "emoji": v.get("emoji", "")})
        if items:
            blocks.append({"id": bid, "emoji": emoji, "name": name, "topics": items})
    return web.json_response({"blocks": blocks})


async def _instrument(request: web.Request) -> web.Response:
    if _auth(request) is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    key = request.match_info["k"]
    text = INSTRUMENT_QUESTIONS.get(key)
    if text is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"key": key, "text": text})


async def _chat(request: web.Request) -> web.Response:
    tg_id = _auth(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    mode = body.get("mode", "")
    topic = body.get("topic")
    message = (body.get("message") or "").strip()
    if mode not in SYSTEM_BY_MODE or not message:
        return web.json_response({"error": "bad_request"}, status=400)
    if mode == "trainer" and not topic:
        return web.json_response({"error": "topic_required"}, status=400)

    thread = _thread(mode, topic)
    async with AsyncSessionLocal() as s:
        is_new = (await q.web_thread_len(s, tg_id, thread)) == 0
        await s.commit()

    # Для тренера в начале треда подставляем невиданный кейс из банка.
    case_text = None
    if mode == "trainer" and is_new:
        case_text = await access.next_trainer_case(tg_id, topic)
    system = get_trainer_prompt(topic, case_text) if mode == "trainer" else SYSTEM_BY_MODE[mode](topic)
    if system is None:
        return web.json_response({"error": "topic_required"}, status=400)

    # Доступ проверяем на старте треда (как в боте: новый = одно действие).
    if is_new:
        allowed, reason = await access.check_mode(tg_id, None, None, mode, consume=True)
        if not allowed:
            return web.json_response({"error": "paywall", "reason": reason,
                                      "paywall": _paywall(mode, reason)}, status=402)

    async with AsyncSessionLocal() as s:
        history = await q.get_web_history(s, tg_id, thread, limit=20)
        await s.commit()

    reply = await llm.ask(system, history, message, tg_id=tg_id, mode=mode)

    async with AsyncSessionLocal() as s:
        await q.add_web_message(s, tg_id, thread, "user", message)
        await q.add_web_message(s, tg_id, thread, "assistant", reply)
        await s.commit()
    return web.json_response({"reply": reply})


async def _reset(request: web.Request) -> web.Response:
    tg_id = _auth(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    thread = _thread(body.get("mode", ""), body.get("topic"))
    async with AsyncSessionLocal() as s:
        await q.reset_web_thread(s, tg_id, thread)
        await s.commit()
    return web.json_response({"ok": True})


def _paywall(mode: str, reason: str) -> str:
    from services import texts
    return texts.paywall_text(mode, reason)


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/app/enter", _enter),
        web.get("/app", _app),
        web.get("/app/api/state", _state),
        web.get("/app/api/topics", _topics),
        web.get("/app/api/instrument/{k}", _instrument),
        web.post("/app/api/chat", _chat),
        web.post("/app/api/reset", _reset),
    ])
