"""Stateless-сессии на подписанных токенах (HMAC).

Личность пользователя = подпись токена `tg_id.exp.sig`. Никаких файлов/таблиц
сессий: проверяем подпись и срок. Один общий секрет в .env (HMAC_SECRET).
"""
from __future__ import annotations

import hashlib
import hmac
import time

from config import settings

_TTL_DEFAULT = 30 * 86400  # 30 дней


def _sig(msg: str) -> str:
    return hmac.new(settings.HMAC_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def sign(tg_id: int, ttl_seconds: int = _TTL_DEFAULT) -> str:
    exp = int(time.time()) + ttl_seconds
    msg = f"{tg_id}.{exp}"
    return f"{msg}.{_sig(msg)}"


def verify(token: str | None) -> int | None:
    """Вернуть tg_id, если токен валиден и не истёк, иначе None."""
    if not token:
        return None
    try:
        tg_id_s, exp_s, sig = token.split(".")
        msg = f"{tg_id_s}.{exp_s}"
        if not hmac.compare_digest(sig, _sig(msg)):
            return None
        if int(exp_s) < int(time.time()):
            return None
        return int(tg_id_s)
    except (ValueError, AttributeError):
        return None
