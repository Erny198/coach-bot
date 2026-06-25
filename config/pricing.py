"""Единый источник тарифов, доступов и бесплатных лимитов.

Цены и состав режимов держим ТОЛЬКО здесь — и бот, и Mini App читают отсюда,
чтобы нигде не было рассинхрона.
"""
from __future__ import annotations

# ── Режимы доступа (флаги) ────────────────────────────────────────────────
# Три коммерческих режима. «trainer» открывает все три подраздела:
# Коучи (клиент), Тренер (вопросы), Инструменты.
MODE_COACH = "coach"
MODE_TRAINER = "trainer"
MODE_SUPERVISOR = "supervisor"

ALL_MODES = (MODE_COACH, MODE_TRAINER, MODE_SUPERVISOR)

# Соответствие технических режимов бота → требуемый флаг доступа.
# Ключи совпадают с callback_data / внутренними mode-кодами бота.
BOT_MODE_TO_FLAG = {
    "coach":       MODE_COACH,       # 🧭 Коуч — бот коучит пользователя
    "coachee":     MODE_TRAINER,     # 🎓 Коучи — бот = клиент
    "trainer":     MODE_TRAINER,     # 🥊 Тренер — тренировка вопросов
    "instruments": MODE_TRAINER,     # 📋 Инструменты — справочник
    "supervisor":  MODE_SUPERVISOR,  # 🔬 Супервизор — разбор сессии
}

# ── Тарифы ────────────────────────────────────────────────────────────────
# modes — какие флаги открывает тариф.
# Цены: рубли (RU) и доллары (intl). Год = цена 10 месяцев (≈17% выгоды).
TIERS: dict[str, dict] = {
    "coach": {
        "title": "Коуч",
        "modes": {MODE_COACH},
        "rub_month": 15000, "rub_year": 150000,
        "usd_month": 150,   "usd_year": 1500,
    },
    "trainer": {
        "title": "Тренер",
        "modes": {MODE_TRAINER},
        "rub_month": 35000, "rub_year": 350000,
        "usd_month": 350,   "usd_year": 3500,
    },
    "supervisor": {
        "title": "Супервизор",
        "modes": {MODE_SUPERVISOR},
        "rub_month": 35000, "rub_year": 350000,
        "usd_month": 350,   "usd_year": 3500,
    },
    "coach_trainer": {
        "title": "Коуч + Тренер",
        "modes": {MODE_COACH, MODE_TRAINER},
        "rub_month": 45000, "rub_year": 450000,
        "usd_month": 450,   "usd_year": 4500,
    },
    "trainer_supervisor": {
        "title": "Тренер + Супервизор",
        "modes": {MODE_TRAINER, MODE_SUPERVISOR},
        "rub_month": 55000, "rub_year": 550000,
        "usd_month": 550,   "usd_year": 5500,
    },
    "coach_supervisor": {
        "title": "Коуч + Супервизор",
        "modes": {MODE_COACH, MODE_SUPERVISOR},
        "rub_month": 45000, "rub_year": 450000,
        "usd_month": 450,   "usd_year": 4500,
    },
    "all": {
        "title": "Всё включено",
        "modes": {MODE_COACH, MODE_TRAINER, MODE_SUPERVISOR},
        "rub_month": 60000, "rub_year": 600000,
        "usd_month": 600,   "usd_year": 6000,
    },
}

PERIODS = {"month": 30, "year": 365}  # дней доступа


def tiers_public(order: list[str] | None = None) -> list[dict]:
    """Список тарифов для Mini App (единый источник цен)."""
    order = order or [
        "coach", "trainer", "supervisor",
        "coach_trainer", "trainer_supervisor", "coach_supervisor", "all",
    ]
    out = []
    for key in order:
        t = TIERS[key]
        out.append({
            "key": key, "title": t["title"],
            "rub_month": t["rub_month"], "rub_year": t["rub_year"],
            "usd_month": t["usd_month"], "usd_year": t["usd_year"],
            "best": key == "all",
        })
    return out


def tier_modes(tier: str) -> set[str]:
    """Набор флагов, которые открывает тариф (пустой для неизвестного)."""
    info = TIERS.get(tier)
    return set(info["modes"]) if info else set()


def price(tier: str, period: str, currency: str = "rub") -> float | int | None:
    info = TIERS.get(tier)
    if not info or period not in PERIODS:
        return None
    return info.get(f"{currency}_{period}")


# ── Бесплатные лимиты (по согласованию) ───────────────────────────────────
# Считаем по штучным действиям в каждом режиме; после исчерпания — paywall.
FREE_LIMITS = {
    "coach": 1,             # 🧭 Коуч: 1 сессия
    "coachee": 1,           # 🎓 Коучи: 1 сессия с клиентом
    "trainer_topics": 2,    # 🥊 Тренер: тест 2 тем из 13
    "instrument_cards": 2,  # 📋 Инструменты: просмотр 2 карточек
}

# Соответствие технического режима → ключ счётчика бесплатных действий.
BOT_MODE_TO_FREE_COUNTER = {
    "coach": "coach",
    "coachee": "coachee",
    "trainer": "trainer_topics",
    "instruments": "instrument_cards",
    # supervisor: бесплатных действий нет — сразу paywall (премиум-режим)
}
