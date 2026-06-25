"""Настройки приложения. Берём из переменных окружения (.env).

Используем pydantic-settings, если установлен; иначе — лёгкий фолбэк на os.environ,
чтобы модуль импортировался даже без зависимости (удобно для тестов БД-слоя).
"""
from __future__ import annotations

import os

# Каталог персистентных данных. На сервере подключи Volume и задай DATA_DIR=/data.
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))

# Путь к файлу БД (SQLite). Для async-движка используем aiosqlite.
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")

# Секреты (заполняются на этапах 4–7; здесь — чтобы был единый список).
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YK_SHOP_ID = os.getenv("YK_SHOP_ID", "")
YK_SECRET_KEY = os.getenv("YK_SECRET_KEY", "")
HMAC_SECRET = os.getenv("HMAC_SECRET", "dev-insecure-secret-change-me")
DOMAIN = os.getenv("DOMAIN", "")

# Включены ли «Чеки от ЮKassa» (54-ФЗ). Если да — передаём receipt.email.
YK_RECEIPTS_ENABLED = os.getenv("YK_RECEIPTS_ENABLED", "false").lower() == "true"

# Мост (aiohttp). Наружу через nginx проксируются только /pay/*.
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8080"))
# Включать мост вместе с ботом. Выключи (false), если мост запускается отдельным сервисом.
RUN_BRIDGE = os.getenv("RUN_BRIDGE", "true").lower() == "true"

# Имена пользователей с «унаследованным» полным доступом (текущий whitelist).
# Им при первом контакте выдаётся полный доступ, чтобы не потерять ранних юзеров.
GRANDFATHERED_USERNAMES: set[str] = {
    "ErnestKh8", "Marina_Mescheriakova", "katerina_gulyaeva", "nosokvik",
    "NikitaMelkumov777", "Uliana_Gazarova", "mamikonian", "rimskaya_ann",
    "majorkina67", "KondakovSL", "ArturGrigoryan359",
}

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ErnestKh8")


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
