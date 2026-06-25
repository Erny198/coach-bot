"""
Telegram-бот коучинга глубинной трансформации.
4 режима: Коуч, Коучи, Тренер, Супервизор.
Стек: python-telegram-bot + Anthropic Claude API.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import re
import time as time_module
from typing import Any, Dict, List

import anthropic
import openai
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from knowledge_base import (
    BLOCKS,
    INSTRUMENT_BLOCKS,
    TOPIC_BLOCKS,
    COACH_TOOLBOX,
    block_name,
    COACHEE_MODE_START,
    COACHEE_SYSTEM_PROMPT,
    COACH_ASK_NAME_MESSAGE,
    COACH_CONTRACTING_PROMPT,
    COACH_CRISIS_KEYWORDS,
    COACH_CRISIS_RESPONSE,
    COACH_HEAVY_KEYWORDS,
    COACH_HEAVY_TOPIC_NOTE,
    COACH_MODE_START,
    COACH_MODE_START_RETURNING,
    COACH_NAME_PROMPT_NOTE,
    COACH_REPORT_KEYWORDS,
    COACH_REPORT_PROMPT,
    COACH_SYSTEM_PROMPT,
    INSTRUMENT_QUESTIONS,
    INSTRUMENTS,
    INSTRUMENTS_MODE_START,
    SUPERVISOR_MODE_START,
    SUPERVISOR_SYSTEM_PROMPT,
    TOPIC_DETAILS,
    TRAINER_MODE_START,
    TRAINER_TOPICS,
    WELCOME_MESSAGE,
    get_trainer_prompt,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Коммерческий слой (БД, доступ, тексты paywall)
from db.migrations import init_db, migrate_legacy_json  # noqa: E402
from config import settings as cfg  # noqa: E402
# sessions импортируем под алиасом: имя `sessions` уже занято словарём
# диалоговых сессий бота ниже.
from services import access, texts, scheduler, bridge  # noqa: E402
from services import sessions as tokens  # noqa: E402


def _miniapp_url(tg_id: int) -> str | None:
    """URL входа в Mini App со свежим подписанным токеном (None, если домен не задан)."""
    if not cfg.DOMAIN:
        return None
    return f"https://{cfg.DOMAIN}/pay/enter?t={tokens.sign(tg_id)}"


def _web_url(tg_id: int) -> str | None:
    """Персональная ссылка на веб-кабинет, действует 10 минут."""
    if not cfg.DOMAIN:
        return None
    return f"https://{cfg.DOMAIN}/app/enter?t={tokens.sign(tg_id, ttl_seconds=600)}"


async def _send_web_link(message, tg_id: int) -> None:
    url = _web_url(tg_id)
    if not url:
        await message.reply_text(texts.WEB_UNAVAILABLE)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👉 Открыть веб-версию", url=url)]])
    await message.reply_text(texts.WEB_INTRO, reply_markup=kb)


def _pay_button(tg_id: int, label: str = "💳 Оформить подписку") -> InlineKeyboardButton:
    """web_app-кнопка, если задан домен; иначе fallback на текстовый список тарифов."""
    url = _miniapp_url(tg_id)
    if url:
        return InlineKeyboardButton(label, web_app=WebAppInfo(url))
    return InlineKeyboardButton("💳 Тарифы", callback_data="show_pricing")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# claude-sonnet-4-20250514 отключается 15.06.2026 — не откатывать дефолт
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_HISTORY = 40

# Фазы режима «Коуч»
COACH_PHASE_ASK_NAME     = "ask_name"
COACH_PHASE_CONFIRM_NAME = "confirm_name"
COACH_PHASE_CONTRACTING  = "contracting"
COACH_PHASE_WORKING      = "working"
COACH_CONTRACTING_MARKER    = "[КОНТРАКТ_УСТАНОВЛЕН]"
COACH_CONTRACTING_MAX_TURNS = 2   # страховка: переключить если затянулся
COACH_WORKING_NEAR_END      = 20  # инъекция «завершай» в промпт
COACH_WORKING_MAX_TURNS     = 25  # автопереключение в closing

# Тренер: длина практики и финальный разбор
TRAINER_MAX_EXCHANGES = 10
TRAINER_FINALE_NOTE = (
    "\n\nВАЖНО: это ФИНАЛЬНЫЙ обмен практики. Вместо новой ситуации дай итоговый разбор:\n"
    "1. Что получалось хорошо у тренирующегося — конкретные примеры его вопросов.\n"
    "2. Главная зона роста — одна, самая важная.\n"
    "3. Общая оценка практики из 10 с коротким обоснованием.\n"
    "4. Одна рекомендация, что потренировать в следующий раз.\n"
    "Тепло поблагодари за практику. Никаких новых ситуаций и вопросов."
)

ALLOWED_USERS = {
    "ErnestKh8",
    "Marina_Mescheriakova",
    "katerina_gulyaeva",
    "nosokvik",
    "NikitaMelkumov777",
    "Uliana_Gazarova",
    "mamikonian",
    "rimskaya_ann",
    "majorkina67",
    "KondakovSL",
    "ArturGrigoryan359",
}

sessions: Dict[int, Dict[str, Any]] = {}
client: anthropic.Anthropic | None = None
openai_client: openai.OpenAI | None = None

# Каталог для персистентных данных. На Railway подключи Volume и задай
# DATA_DIR=/data — иначе файлы стираются при каждом деплое.
DATA_DIR = os.getenv("DATA_DIR", ".")

# Персистентное хранение имён пользователей (user_id → имя)
NAMES_FILE = os.path.join(DATA_DIR, "user_names.json")
_user_names: Dict[int, str] = {}

# Персистентное хранение активных сессий — переживают редеплой
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

# Динамический список доступа — управляется через /adduser и /removeuser
ALLOWED_FILE = os.path.join(DATA_DIR, "allowed_users.json")
ADMIN_USERNAME = "ErnestKh8"
_allowed_users: set[str] = set()


def _load_sessions() -> None:
    global sessions
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            sessions = {int(k): v for k, v in raw.items()}
        logger.info("Loaded %d active sessions from %s", len(sessions), SESSIONS_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        sessions = {}


def _save_sessions() -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sessions.items()}, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("Could not save sessions: %s", e)


def _load_allowed_users() -> None:
    global _allowed_users
    try:
        with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
            _allowed_users = set(json.load(f))
        logger.info("Loaded %d allowed users from %s", len(_allowed_users), ALLOWED_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        _allowed_users = set(ALLOWED_USERS)
        _save_allowed_users()


def _save_allowed_users() -> None:
    try:
        with open(ALLOWED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_allowed_users), f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Could not save allowed users: %s", e)


def _load_user_names() -> None:
    global _user_names
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            _user_names = {int(k): v for k, v in raw.items()}
        logger.info("Loaded %d user names from %s", len(_user_names), NAMES_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        _user_names = {}


def _save_user_names() -> None:
    try:
        with open(NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _user_names.items()}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Could not save user names: %s", e)


def _get_stored_name(user_id: int) -> str | None:
    return _user_names.get(user_id)


def _store_name(user_id: int, name: str) -> None:
    _user_names[user_id] = name
    _save_user_names()


# Цены Claude API ($ за 1 токен)
CLAUDE_PRICE_INPUT  = 3.00  / 1_000_000   # $3.00 per 1M input tokens
CLAUDE_PRICE_OUTPUT = 15.00 / 1_000_000   # $15.00 per 1M output tokens

# Активность пользователей и токены
ACTIVITY_FILE = os.path.join(DATA_DIR, "user_activity.json")
_user_activity: Dict[str, Any] = {}


def _load_activity() -> None:
    global _user_activity
    try:
        with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
            _user_activity = json.load(f)
        logger.info("Loaded activity for %d users", len(_user_activity))
    except (FileNotFoundError, json.JSONDecodeError):
        _user_activity = {}


def _save_activity() -> None:
    try:
        with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(_user_activity, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Could not save activity: %s", e)


def _track_usage(user_id: int, username: str | None, inp: int, out: int) -> None:
    """Записать использование токенов после вызова Claude."""
    if inp == 0 and out == 0:
        return
    today = datetime.date.today().isoformat()
    uid = str(user_id)
    now = time_module.time()
    if uid not in _user_activity:
        _user_activity[uid] = {
            "username": username or f"id:{user_id}",
            "first_seen": now,
            "last_seen": now,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "daily_tokens": {},
        }
    entry = _user_activity[uid]
    entry["last_seen"] = now
    if username:
        entry["username"] = username
    entry["total_input_tokens"] = entry.get("total_input_tokens", 0) + inp
    entry["total_output_tokens"] = entry.get("total_output_tokens", 0) + out
    day = entry.setdefault("daily_tokens", {}).setdefault(today, {"input": 0, "output": 0})
    day["input"]  += inp
    day["output"] += out
    _save_activity()


# Паттерны для обнаружения имени в тексте сообщения
_NAME_PATTERNS = re.compile(
    r"(?:меня зовут|зови меня|называй меня|обращайся ко мне|мо[её] имя)[^\w]*([А-ЯЁа-яёA-Za-z][а-яёa-z]*)",
    re.IGNORECASE,
)


# Chat ID админа — чтобы слать уведомления о чужих. Заполняется когда админ
# пишет боту; переживает редеплой через файл.
ADMIN_CHAT_FILE = os.path.join(DATA_DIR, "admin_chat.json")
_admin_chat_id: int | None = None
_denied_notified: set[int] = set()  # кому уже отправили уведомление (на процесс)


def _load_admin_chat() -> None:
    global _admin_chat_id
    try:
        with open(ADMIN_CHAT_FILE, "r", encoding="utf-8") as f:
            _admin_chat_id = json.load(f).get("chat_id")
    except (FileNotFoundError, json.JSONDecodeError):
        _admin_chat_id = None


def _store_admin_chat(chat_id: int) -> None:
    global _admin_chat_id
    if _admin_chat_id == chat_id:
        return
    _admin_chat_id = chat_id
    try:
        with open(ADMIN_CHAT_FILE, "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id}, f)
    except OSError as e:
        logger.warning("Could not save admin chat id: %s", e)


async def _notify_admin_denied(context, user) -> None:
    """Сообщить админу, что кто-то без доступа стучится в бот."""
    if _admin_chat_id is None or user.id in _denied_notified:
        return
    _denied_notified.add(user.id)
    label = f"@{user.username}" if user.username else (user.full_name or f"id:{user.id}")
    text = f"🔔 В бот стучится новый человек: {label} (id {user.id})."
    if user.username:
        text += f"\nОткрыть доступ: /adduser {user.username}"
    try:
        await context.bot.send_message(chat_id=_admin_chat_id, text=text)
    except Exception:
        logger.warning("Could not notify admin about denied user %s", user.id)


def _is_allowed(user) -> bool:
    """Проверить, есть ли username пользователя в списке доступа."""
    if not user or not user.username:
        return False
    return user.username in _allowed_users


def _is_admin(user) -> bool:
    return bool(user and user.username == ADMIN_USERNAME)


def get_session(user_id: int) -> Dict[str, Any]:
    if user_id not in sessions:
        known_name = _get_stored_name(user_id)
        sessions[user_id] = {
            "mode": None,
            "history": [],
            "trainer_topic": None,
            "trainer_exchanges": 0,
            "coach_phase": COACH_PHASE_CONTRACTING if known_name else COACH_PHASE_ASK_NAME,
            "coach_exchanges": 0,
            "coach_working_limit": COACH_WORKING_MAX_TURNS,
            "coach_awaiting_close_confirm": False,
            "user_name": known_name,
            "pending_name": None,
        }
    return sessions[user_id]


def init_claude() -> None:
    global client
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан")
        client = None
        return
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    logger.info("Anthropic client initialized")


def init_openai() -> None:
    global openai_client
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY не задан — распознавание голоса недоступно")
        openai_client = None
        return
    openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client initialized")


async def ask_claude(
    system_prompt: str,
    history: List[dict],
    user_message: str,
    user_id: int | None = None,
    username: str | None = None,
) -> str:
    """Вызвать Claude не блокируя event loop; записать токены если передан user_id."""
    if client is None:
        return (
            "⚠️ API ключ Anthropic не настроен. "
            "Добавьте переменную окружения ANTHROPIC_API_KEY в Railway."
        )

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = await asyncio.to_thread(
            client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=2048,
            # Кэширование префикса: системный промпт помечен явно,
            # top-level cache_control докэширует историю диалога.
            # Повторные чтения из кэша стоят ~0.1x от обычной цены.
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            cache_control={"type": "ephemeral"},
            messages=messages,
        )
        blocks = response.content or []
        text_parts = [block.text for block in blocks if getattr(block, "type", "") == "text"]
        reply = "\n".join(part.strip() for part in text_parts if part and part.strip())
        if user_id is not None:
            # Кэш-токены пересчитываем в эквивалент обычных входных:
            # запись в кэш стоит 1.25x, чтение — 0.1x
            inp = getattr(response.usage, "input_tokens", 0)
            cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            equiv_input = int(inp + cache_write * 1.25 + cache_read * 0.1)
            out_tokens = getattr(response.usage, "output_tokens", 0)
            _track_usage(user_id, username, equiv_input, out_tokens)
            # Дублируем в БД для единого учёта затрат (llm_calls).
            try:
                cost = equiv_input * CLAUDE_PRICE_INPUT + out_tokens * CLAUDE_PRICE_OUTPUT
                await access.log_llm(user_id, None, equiv_input, out_tokens, cost)
            except Exception:
                logger.debug("llm_calls DB log skipped", exc_info=True)
        return reply or "⚠️ Claude вернул пустой ответ."
    except anthropic.APIError:
        logger.exception("Claude API error")
        return "⚠️ Ошибка API Anthropic. Проверьте ключ, модель и лимиты аккаунта."
    except Exception:
        logger.exception("Unexpected Claude error")
        return "⚠️ Произошла ошибка при обращении к Claude."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if _is_admin(update.effective_user) and update.effective_chat:
        _store_admin_chat(update.effective_chat.id)

    # Freemium: доступ открыт всем. Регистрируем юзера в БД (grandfathered-список
    # получает полный доступ автоматически внутри get_or_create_user).
    snap = await access.touch_user(
        update.effective_user.id, update.effective_user.username,
        _get_stored_name(update.effective_user.id),
    )
    await access.log_event(update.effective_user.id, "start")

    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    _save_sessions()

    # Персональная menu-кнопка Mini App со свежим токеном (если задан домен).
    mini_url = _miniapp_url(user_id)
    if mini_url and update.effective_chat:
        try:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonWebApp(text="Подписка", web_app=WebAppInfo(mini_url)),
            )
        except Exception:
            logger.debug("set_chat_menu_button skipped", exc_info=True)

    if not snap.get("onboarding_done"):
        # Новый пользователь — продающий онбординг: ценность + выбор роли.
        await update.message.reply_text(texts.ONBOARDING_WELCOME, reply_markup=_role_kb())
    else:
        await update.message.reply_text(texts.MENU_TITLE, reply_markup=_main_menu_kb(user_id))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return
    sessions.pop(update.effective_user.id, None)
    _save_sessions()
    await update.message.reply_text("🔄 Сессия сброшена. Нажми /start, чтобы начать заново.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    text = (
        "Команды бота:\n\n"
        "/start — выбрать режим\n"
        "/reset — сбросить сессию\n"
        "/topics — список тем для тренировки\n"
        "/report — отчёт по текущей коуч-сессии\n"
        "/help — справка\n\n"
        "Режимы:\n"
        "🧑‍💼 Коуч — бот проводит сессию\n"
        "🎓 Коучи — бот играет клиента\n"
        "📚 Тренер — тренировка вопросов\n"
        "🔬 Супервизор — разбор сессии"
    )
    if _is_admin(update.effective_user):
        text += (
            "\n\nАдмин:\n"
            "/stats — статистика и затраты\n"
            "/adduser ник — открыть доступ\n"
            "/removeuser ник — закрыть доступ"
        )
    await update.message.reply_text(text)


async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    await _show_trainer_topics(update.message)


async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user:
        return
    await _send_web_link(update.message, update.effective_user.id)


def _main_menu_kb(tg_id: int | None = None) -> InlineKeyboardMarkup:
    """Коммерческое меню из 3 режимов (Тренер раскрывается в подменю)."""
    pay_row = [_pay_button(tg_id, "💳 Подписка")] if tg_id is not None else \
        [InlineKeyboardButton("💳 Тарифы", callback_data="show_pricing")]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 Коуч — бот коучит меня", callback_data="mode_coach")],
        [InlineKeyboardButton("🥊 Тренировка — ежедневная практика", callback_data="submenu_trainer")],
        [InlineKeyboardButton("🔬 Супервизор — разбор сессии", callback_data="mode_supervisor")],
        [InlineKeyboardButton("🌐 Веб-версия (в браузере)", callback_data="web_link")],
        pay_row,
    ])


def _trainer_submenu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎓 Коучи — тренируюсь на клиенте", callback_data="mode_coachee")],
        [InlineKeyboardButton("🥊 Тренер — вопросы по темам", callback_data="mode_trainer")],
        [InlineKeyboardButton("📋 Инструменты — справочник", callback_data="mode_instruments")],
        [InlineKeyboardButton("↩️ Меню", callback_data="main_menu")],
    ])


def _role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎓 Учусь на коуча / выпускник", callback_data="role_student")],
        [InlineKeyboardButton("🧑‍💼 Практикую, нужна форма", callback_data="role_practicing")],
        [InlineKeyboardButton("🏫 Веду учеников / школу", callback_data="role_mentor")],
        [InlineKeyboardButton("🔎 Просто смотрю", callback_data="role_curious")],
    ])


async def _show_paywall(query, bot_mode: str, reason: str) -> None:
    """Показать paywall с краткой ценностью и кнопкой тарифов."""
    kb = InlineKeyboardMarkup([
        [_pay_button(query.from_user.id)],
        [InlineKeyboardButton("↩️ Меню", callback_data="main_menu")],
    ])
    await query.message.reply_text(texts.paywall_text(bot_mode, reason), reply_markup=kb)


async def _gate(query, bot_mode: str) -> bool:
    """Проверка доступа к режиму. Если нет — показывает paywall и возвращает False.
    Бесплатное действие списывается внутри check_mode."""
    allowed, reason = await access.check_mode(
        query.from_user.id, query.from_user.username,
        _get_stored_name(query.from_user.id), bot_mode,
    )
    if not allowed:
        await _show_paywall(query, bot_mode, reason)
    return allowed


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    session = get_session(user_id)
    data = query.data or ""

    if data == "show_pricing":
        await query.message.reply_text(texts.pricing_overview())
        return

    if data == "web_link":
        await _send_web_link(query.message, query.from_user.id)
        return

    if data.startswith("instblock_"):
        bid = data.removeprefix("instblock_")
        await query.message.reply_text(
            f"{block_name(bid)} — инструменты:",
            reply_markup=_instruments_block_kb(bid),
        )
        return

    if data.startswith("topicblock_"):
        bid = data.removeprefix("topicblock_")
        await query.message.reply_text(
            f"{block_name(bid)} — темы тренировки:",
            reply_markup=_topics_block_kb(bid),
        )
        return

    if data == "submenu_trainer":
        await query.edit_message_text(texts.TRAINER_SUBMENU_TITLE, reply_markup=_trainer_submenu_kb())
        return

    if data.startswith("role_"):
        role = data.removeprefix("role_")
        await access.set_onboarding(query.from_user.id, role=role)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🪑 Провести пробную сессию", callback_data="mode_coachee")],
            [InlineKeyboardButton("📋 Открыть меню", callback_data="main_menu")],
        ])
        await query.edit_message_text(texts.role_reply(role), reply_markup=kb)
        return

    if data == "mode_coach":
        if not await _gate(query, "coach"):
            return
        known_name = _get_stored_name(query.from_user.id)
        session["mode"] = "coach"
        session["history"] = []
        session["coach_exchanges"] = 0
        session["coach_working_limit"] = COACH_WORKING_MAX_TURNS
        session["coach_awaiting_close_confirm"] = False
        session["pending_name"] = None
        session["user_name"] = known_name
        if known_name:
            # Имя уже известно — короткое приветствие и сразу к контрактингу
            await query.edit_message_text(COACH_MODE_START_RETURNING.format(name=known_name))
            session["coach_phase"] = COACH_PHASE_CONTRACTING
            await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
            system = _get_coach_system_prompt(session)
            first_msg = await ask_claude(
                system, [], "Клиент готов, начинаем сессию.",
                query.from_user.id, query.from_user.username,
            )
            session["history"].append({"role": "user", "content": "Клиент готов, начинаем сессию."})
            session["history"].append({"role": "assistant", "content": first_msg})
            await _send_long_message(query.message, _mode_header(session) + first_msg)
        else:
            # Первый раз — полное приветствие и вопрос об имени
            await query.edit_message_text(COACH_MODE_START)
            session["coach_phase"] = COACH_PHASE_ASK_NAME
            await query.message.reply_text(COACH_ASK_NAME_MESSAGE)
        _save_sessions()
        return

    if data == "mode_coachee":
        if not await _gate(query, "coachee"):
            return
        session["mode"] = "coachee"
        session["history"] = []
        session["coachee_profile"] = None  # новый случайный профиль на сессию
        await query.edit_message_text(COACHEE_MODE_START, parse_mode="MarkdownV2")
        _save_sessions()
        return

    if data == "mode_trainer":
        session["mode"] = "trainer"
        session["history"] = []
        session["trainer_topic"] = None
        await query.edit_message_text(TRAINER_MODE_START, parse_mode="MarkdownV2")
        await _show_trainer_topics_after_edit(query)
        _save_sessions()
        return

    if data == "mode_supervisor":
        if not await _gate(query, "supervisor"):
            return
        session["mode"] = "supervisor"
        session["history"] = []
        await query.edit_message_text(SUPERVISOR_MODE_START, parse_mode="MarkdownV2")
        _save_sessions()
        return

    if data == "mode_instruments":
        session["mode"] = "instruments"
        session["history"] = []
        await query.edit_message_text(INSTRUMENTS_MODE_START, parse_mode="MarkdownV2")
        await _show_instruments_keyboard(query.message)
        _save_sessions()
        return

    if data == "main_menu":
        sessions.pop(query.from_user.id, None)
        _save_sessions()
        await query.message.reply_text(texts.MENU_TITLE, reply_markup=_main_menu_kb(query.from_user.id))
        return

    if data == "coach_name_yes":
        name = session.get("pending_name") or "друг"
        session["user_name"] = name
        session["pending_name"] = None
        session["coach_phase"] = COACH_PHASE_CONTRACTING
        _store_name(query.from_user.id, name)   # сохраняем навсегда
        await query.edit_message_text(f"Отлично, {name}! Начинаем сессию.")
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        system = _get_coach_system_prompt(session)
        first_msg = await ask_claude(
            system, [], "Клиент готов, начинаем сессию.",
            query.from_user.id, query.from_user.username,
        )
        session["history"].append({"role": "user", "content": "Клиент готов, начинаем сессию."})
        session["history"].append({"role": "assistant", "content": first_msg})
        await _send_long_message(query.message, _mode_header(session) + first_msg)
        _save_sessions()
        return

    if data == "coach_name_no":
        session["pending_name"] = None
        session["coach_phase"] = COACH_PHASE_ASK_NAME
        await query.edit_message_text("Хорошо! Как тебя зовут?")
        _save_sessions()
        return

    if data == "coach_close_yes":
        # WORKING сам подвёл к естественному финалу — никакой отдельной
        # фазы CLOSING, никаких повторных вопросов. Сразу выбор:
        # отчёт, новая сессия или меню.
        session["coach_awaiting_close_confirm"] = False
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Отчёт по сессии", callback_data="coach_final_report")],
            [
                InlineKeyboardButton("🔄 Новая сессия", callback_data="mode_coach"),
                InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
            ],
        ])
        await query.edit_message_text(
            "Спасибо за сессию 🙏 Хочешь, я подготовлю отчёт?",
            reply_markup=keyboard,
        )
        _save_sessions()
        return

    if data == "coach_final_report":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        report = await ask_claude(
            COACH_REPORT_PROMPT, session["history"],
            "Составь отчёт по нашей сессии.",
            query.from_user.id, query.from_user.username,
        )
        await _send_long_message(query.message, report)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Новая сессия", callback_data="mode_coach"),
            InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
        ]])
        await query.message.reply_text("Что дальше?", reply_markup=keyboard)
        _save_sessions()
        return

    if data == "coach_close_no":
        limit = session.get("coach_working_limit", COACH_WORKING_MAX_TURNS)
        session["coach_working_limit"] = limit + 5
        session["coach_awaiting_close_confirm"] = False
        await query.edit_message_text("Хорошо, продолжаем! Ещё несколько обменов.")
        _save_sessions()
        return

    if data.startswith("instr_"):
        if not await _gate(query, "instruments"):
            return
        instr_key = data.removeprefix("instr_")
        questions = INSTRUMENT_QUESTIONS.get(instr_key)
        if questions is None:
            await query.message.reply_text("Инструмент не найден.")
            return
        session["mode"] = "instruments"
        nav_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 К инструментам", callback_data="mode_instruments"),
                InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
            ]
        ])
        await query.message.reply_text(questions, reply_markup=nav_keyboard)
        return

    if data.startswith("topic_"):
        if not await _gate(query, "trainer"):
            return
        topic_key = data.removeprefix("topic_")
        topic_info = TRAINER_TOPICS.get(topic_key)
        if topic_info is None:
            await query.message.reply_text("Не удалось найти тему. Выбери её ещё раз через /topics.")
            return

        session["mode"] = "trainer"
        session["trainer_topic"] = topic_key
        session["history"] = []
        session["trainer_exchanges"] = 0
        # Курируемый кейс из банка, не повторяющийся у этого пользователя.
        session["trainer_case"] = await access.next_trainer_case(query.from_user.id, topic_key)

        topic_name = topic_info["name"]
        emoji = topic_info["emoji"]
        await query.edit_message_text(
            f"{emoji} *Тема:* {_escape_md(topic_name)}\n\n"
            "Сейчас я дам краткую теорию и первую ситуацию для практики\\.\n"
            "Ты будешь задавать вопросы, а я — давать обратную связь\\.\n\n"
            "Напиши *Начинаем* или любое сообщение, чтобы стартовать\\!",
            parse_mode="MarkdownV2",
        )
        _save_sessions()
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return

    # Freemium: текст обрабатываем для всех; доступ к платным режимам
    # ограничивается гейтами при входе в режим, а не здесь.
    user_text = (update.message.text or "").strip()

    if not user_text:
        await update.message.reply_text("Пришли текстовое сообщение или нажми /start.")
        return

    await _process_user_text(update, context, user_text)


async def _send_session_report(update: Update, context, session: dict,
                               uid: int, uname: str | None) -> None:
    """Сгенерировать и отправить отчёт по текущей коуч-сессии."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    report = await ask_claude(
        COACH_REPORT_PROMPT, session["history"],
        "Составь отчёт по нашей сессии.", uid, uname,
    )
    session["history"].append({"role": "user", "content": "Составь отчёт по нашей сессии."})
    session["history"].append({"role": "assistant", "content": report})
    await _send_long_message(update.message, report)


async def _process_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    """Общая обработка текста пользователя — из сообщения или транскрипции голоса."""
    user_id = update.effective_user.id
    session = get_session(user_id)

    if session["mode"] is None:
        await update.message.reply_text("Сначала выбери режим через /start.")
        return

    if session["mode"] == "instruments":
        await _show_instruments_keyboard(update.message)
        return

    # Сбор имени — фазы ASK_NAME и CONFIRM_NAME
    if session["mode"] == "coach" and session.get("coach_phase") in (
        COACH_PHASE_ASK_NAME, COACH_PHASE_CONFIRM_NAME
    ):
        await _handle_name_collection(update, session, user_text)
        _save_sessions()
        return

    # Тихое обновление имени в режиме Коуч — «меня зовут X»
    if session["mode"] == "coach":
        mentioned_name = _extract_name_mention(user_text)
        if mentioned_name:
            session["user_name"] = mentioned_name
            _store_name(user_id, mentioned_name)
            logger.info("Name updated via mention: %s (user_id=%s)", mentioned_name, user_id)

    _uid = user_id
    _uname = update.effective_user.username

    # Запрос отчёта в режиме Коуч — работает в любой фазе
    if session["mode"] == "coach" and _is_report_request(user_text) and session["history"]:
        await _send_session_report(update, context, session, _uid, _uname)
        _save_sessions()
        return

    # Кризисный сигнал в режиме Коуч — фиксированный ответ с ресурсами помощи
    if session["mode"] == "coach" and _is_crisis(user_text):
        session["history"].append({"role": "user", "content": user_text})
        session["history"].append({"role": "assistant", "content": COACH_CRISIS_RESPONSE})
        await _send_long_message(update.message, COACH_CRISIS_RESPONSE)
        _save_sessions()
        return

    if session["mode"] == "coach":
        is_heavy = _is_heavy_topic(user_text)
        system_prompt = _get_coach_system_prompt(session, heavy_topic=is_heavy)
    elif session["mode"] == "coachee":
        system_prompt = COACHEE_SYSTEM_PROMPT + _coachee_profile_note(session)
    elif session["mode"] == "trainer":
        topic_key = session.get("trainer_topic")
        if not topic_key:
            await _show_trainer_topics(update.message)
            return
        # Кейс из банка подставляем только в первую ситуацию практики.
        case_text = session.get("trainer_case") if session.get("trainer_exchanges", 0) == 0 else None
        system_prompt = get_trainer_prompt(topic_key, case_text)
        if session.get("trainer_exchanges", 0) + 1 >= TRAINER_MAX_EXCHANGES:
            system_prompt += TRAINER_FINALE_NOTE
    elif session["mode"] == "supervisor":
        system_prompt = SUPERVISOR_SYSTEM_PROMPT
    else:
        await update.message.reply_text("Нажми /start для начала.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if session["mode"] == "trainer":
        session["trainer_exchanges"] = session.get("trainer_exchanges", 0) + 1

    reply = await ask_claude(system_prompt, session["history"], user_text, _uid, _uname)

    if session["mode"] == "coach":
        user_turns = sum(1 for m in session["history"] if m["role"] == "user") + 1
        reply = _coach_phase_transition(session, reply, user_turns)

    session["history"].append({"role": "user", "content": user_text})
    session["history"].append({"role": "assistant", "content": reply})

    if len(session["history"]) > MAX_HISTORY * 2:
        session["history"] = session["history"][-(MAX_HISTORY * 2):]

    await _send_long_message(update.message, _mode_header(session) + reply)

    # Финал практики в Тренере — разбор выдан, предложить продолжение
    if session["mode"] == "trainer" and session.get("trainer_exchanges", 0) >= TRAINER_MAX_EXCHANGES:
        session["trainer_topic"] = None
        session["trainer_exchanges"] = 0
        session["history"] = []
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📚 Другая тема", callback_data="mode_trainer"),
            InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
        ]])
        await update.message.reply_text(
            "🎉 Практика завершена! Что дальше?",
            reply_markup=keyboard,
        )

    # Запрос подтверждения закрытия сессии
    if session.get("coach_awaiting_close_confirm") is True:
        session["coach_awaiting_close_confirm"] = "shown"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, закрываем", callback_data="coach_close_yes"),
            InlineKeyboardButton("🔄 Нет, продолжаем", callback_data="coach_close_no"),
        ]])
        await update.message.reply_text(
            "Закрываем сессию или хочешь продолжить?",
            reply_markup=keyboard,
        )

    _save_sessions()


async def transcribe_voice(file_path: str) -> str | None:
    """Транскрибировать голосовое сообщение через OpenAI Whisper."""
    if openai_client is None:
        return None
    with open(file_path, "rb") as audio_file:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", audio_file, "audio/ogg"),
            language="ru",
        )
    text = result.text.strip() if result.text else ""
    logger.info("Whisper transcription success: %r", text)
    return text or None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return

    if not _is_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ ограничен. Обратитесь к администратору.")
        await _notify_admin_denied(context, update.effective_user)
        return

    if openai_client is None:
        await update.message.reply_text("⚠️ Распознавание голоса не настроено. Добавьте OPENAI_API_KEY.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    text = None
    error_info = ""
    try:
        await tg_file.download_to_drive(tmp_path)
        size = os.path.getsize(tmp_path)
        logger.info("Voice file downloaded: %s (%d bytes)", tmp_path, size)
        if size == 0:
            error_info = "файл пустой"
        else:
            text = await transcribe_voice(tmp_path)
            logger.info("Whisper result: %r", text)
    except Exception as e:
        logger.exception("Voice handling error")
        error_info = str(e)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not text:
        detail = f" ({error_info})" if error_info else ""
        await update.message.reply_text(f"⚠️ Не удалось распознать голосовое сообщение{detail}. Попробуй ещё раз.")
        return

    # Эхо транскрипции — чтобы человек видел, как его поняли
    echo = text if len(text) <= 300 else text[:300].rstrip() + "…"
    await update.message.reply_text(f"🎤 Ты сказал(а): «{echo}»")

    # Распознанный текст идёт в общий обработчик
    await _process_user_text(update, context, text)


COACHEE_PROFILE_KEYS = ["А", "Б", "В", "Г", "Д", "Е"]


def _coachee_profile_note(session: dict) -> str:
    """Профиль клиента для режима Коучи выбирает код, а не модель —
    иначе модель почти всегда играет первый профиль из списка."""
    profile = session.get("coachee_profile")
    if not profile:
        profile = random.choice(COACHEE_PROFILE_KEYS)
        session["coachee_profile"] = profile
        logger.info("Coachee profile selected: %s", profile)
    return (
        f"\n\nВ ЭТОЙ СЕССИИ играй строго профиль {profile}. "
        "Не сообщай пользователю, какой профиль выбран."
    )


def _is_report_request(text: str) -> bool:
    """Проверить, просит ли пользователь отчёт по сессии."""
    lower = text.lower().strip()
    return any(kw in lower for kw in COACH_REPORT_KEYWORDS)


def _looks_like_name(text: str) -> bool:
    """Проверить, похож ли текст на имя, а не на вопрос или тему для разговора."""
    t = text.strip()
    if not t or len(t) > 30 or "?" in t:
        return False
    if len(t.split()) > 2:
        return False
    return bool(re.fullmatch(r"[А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z\- ]*", t))


async def _handle_name_collection(update: Update, session: dict, text: str) -> None:
    """Обработать ответ на вопрос «как тебя зовут?» — с проверкой что это имя."""
    name = _extract_name_mention(text) or text.strip()
    if not _looks_like_name(name):
        await update.message.reply_text(
            "Кажется, это не имя 🙂 Давай сначала познакомимся — "
            "напиши, как мне к тебе обращаться. А потом перейдём к твоему запросу."
        )
        return
    name = name[0].upper() + name[1:]
    session["pending_name"] = name
    session["coach_phase"] = COACH_PHASE_CONFIRM_NAME
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, всё верно", callback_data="coach_name_yes"),
        InlineKeyboardButton("✏️ Нет, исправить", callback_data="coach_name_no"),
    ]])
    await update.message.reply_text(
        f"{name}, я правильно понял?",
        reply_markup=keyboard,
    )


def _extract_name_mention(text: str) -> str | None:
    """Извлечь имя из фраз типа «меня зовут Эрнест». Вернуть None если не найдено."""
    m = _NAME_PATTERNS.search(text)
    if m:
        return m.group(1).strip().capitalize()
    return None


def _keywords_regex(keywords: set[str]) -> re.Pattern[str]:
    """Скомпилировать набор фраз в regex с границами слов.

    Поиск по подстроке давал ложные срабатывания: «рак» внутри «характер»,
    «умер» внутри «умеренный», «бил» внутри «автомобиль».
    """
    alternatives = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"(?<![а-яёa-z])(?:{alternatives})(?![а-яёa-z])", re.IGNORECASE)


_CRISIS_RE = _keywords_regex(COACH_CRISIS_KEYWORDS)
_HEAVY_RE  = _keywords_regex(COACH_HEAVY_KEYWORDS)


def _is_crisis(text: str) -> bool:
    """Обнаружить кризисные сигналы (суицид / самоповреждение)."""
    return bool(_CRISIS_RE.search(text))


def _is_heavy_topic(text: str) -> bool:
    """Обнаружить тяжёлые темы, требующие замедления и особой чуткости."""
    return bool(_HEAVY_RE.search(text))


def _get_coach_system_prompt(session: dict, heavy_topic: bool = False) -> str:
    """Выбрать системный промпт коуча в зависимости от текущей фазы."""
    phase = session.get("coach_phase", COACH_PHASE_CONTRACTING)
    if phase == COACH_PHASE_CONTRACTING:
        base = COACH_CONTRACTING_PROMPT
    else:
        # WORKING — основной промпт + инъекция если близко к концу
        extra = ""
        if session.get("coach_exchanges", 0) >= COACH_WORKING_NEAR_END:
            extra = (
                "\n\nВАЖНО: сессия приближается к завершению, осталось несколько обменов. "
                "Мягко завершай работу с инструментом — переходи к интеграции, "
                "ресурсам и рефлексии."
            )
        base = COACH_SYSTEM_PROMPT + COACH_TOOLBOX + extra

    if heavy_topic:
        base += COACH_HEAVY_TOPIC_NOTE

    name = session.get("user_name")
    if name:
        base += COACH_NAME_PROMPT_NOTE.format(name=name)

    return base


def _coach_phase_transition(session: dict, reply: str, user_turns: int) -> str:
    """Переключить фазу коуч-сессии если нужно. Вернуть очищенный reply."""
    phase = session.get("coach_phase", COACH_PHASE_CONTRACTING)

    if phase == COACH_PHASE_CONTRACTING:
        if COACH_CONTRACTING_MARKER in reply or user_turns >= COACH_CONTRACTING_MAX_TURNS:
            reply = reply.replace(COACH_CONTRACTING_MARKER, "").strip()
            session["coach_phase"] = COACH_PHASE_WORKING
            session["coach_exchanges"] = 0
            logger.info("Coach phase → WORKING")

    elif phase == COACH_PHASE_WORKING:
        session["coach_exchanges"] = session.get("coach_exchanges", 0) + 1
        limit = session.get("coach_working_limit", COACH_WORKING_MAX_TURNS)
        confirm = session.get("coach_awaiting_close_confirm", False)
        if session["coach_exchanges"] >= limit and confirm is False:
            session["coach_awaiting_close_confirm"] = True
            logger.info("Coach: запрос подтверждения закрытия сессии")

    return reply


def _progress_bar(count: int, total: int = 10) -> str:
    """Прогресс-бар: █ заполненные, ░ пустые, максимум total блоков."""
    filled = min(count, total)
    return "█" * filled + "░" * (total - filled)


def _mode_header(session: dict) -> str:
    """Собрать контекстную шапку для ответа бота: режим + тема + прогресс."""
    mode = session.get("mode")
    sep = "―" * 18 + "\n"
    if mode == "coach":
        phase = session.get("coach_phase", COACH_PHASE_CONTRACTING)
        phase_labels = {
            COACH_PHASE_ASK_NAME:     "Знакомство",
            COACH_PHASE_CONFIRM_NAME: "Знакомство",
            COACH_PHASE_CONTRACTING:  "Контракт",
            COACH_PHASE_WORKING:      "Сессия",
        }
        phase_label = phase_labels.get(phase, "")
        if phase == COACH_PHASE_WORKING:
            count = session.get("coach_exchanges", 0)
            bar = _progress_bar(count, total=COACH_WORKING_MAX_TURNS)
            return f"🧑‍💼 Коуч • {phase_label}  {count}/{COACH_WORKING_MAX_TURNS}  {bar}\n{sep}"
        return f"🧑‍💼 Коуч • {phase_label}\n{sep}"
    if mode == "coachee":
        return f"🎓 Коучи\n{sep}"
    if mode == "supervisor":
        return f"🔬 Супервизор\n{sep}"
    if mode == "trainer":
        topic_key = session.get("trainer_topic") or ""
        topic_info = TRAINER_TOPICS.get(topic_key, {})
        emoji = topic_info.get("emoji", "📚")
        name = topic_info.get("short_name") or topic_info.get("name", "")
        label = f"{emoji} {name}" if name else "Тренер"
        count = session.get("trainer_exchanges", 0)
        bar = _progress_bar(count)
        return f"📚 Тренер • {label}\nОбмен {count}  {bar}\n{sep}"
    return ""


def _block_menu_kb(prefix: str) -> InlineKeyboardMarkup:
    """Меню из 6 блоков. prefix: 'instblock' или 'topicblock'."""
    rows = [[InlineKeyboardButton(f"{emoji} {name}", callback_data=f"{prefix}_{bid}")]
            for bid, emoji, name in BLOCKS]
    rows.append([InlineKeyboardButton("↩️ Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _instruments_block_kb(block_id: str) -> InlineKeyboardMarkup:
    """Инструменты внутри блока (2 в ряду) + возврат к блокам."""
    rows = []
    keys = INSTRUMENT_BLOCKS.get(block_id, [])
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i : i + 2]:
            info = INSTRUMENTS.get(key)
            if not info:
                continue
            row.append(InlineKeyboardButton(f"{info['emoji']} {info['name']}", callback_data=f"instr_{key}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("⬅️ Блоки", callback_data="mode_instruments"),
        InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(rows)


async def _show_instruments_keyboard(message) -> None:
    """Показать выбор блока инструментов (двухуровневая навигация)."""
    await message.reply_text(
        "📋 Инструменты — выбери блок:",
        reply_markup=_block_menu_kb("instblock"),
    )


def _trainer_topics_keyboard() -> InlineKeyboardMarkup:
    """Первый уровень тренера — выбор блока."""
    return _block_menu_kb("topicblock")


def _topics_block_kb(block_id: str) -> InlineKeyboardMarkup:
    """Темы внутри блока (2 в ряду) + возврат к блокам."""
    rows = []
    keys = TOPIC_BLOCKS.get(block_id, [])
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i : i + 2]:
            info = TRAINER_TOPICS.get(key)
            if not info:
                continue
            short = info.get("short_name") or info["name"]
            row.append(InlineKeyboardButton(f"{info['emoji']} {short}", callback_data=f"topic_{key}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("⬅️ Блоки", callback_data="mode_trainer"),
        InlineKeyboardButton("↩️ Меню", callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(rows)


async def _show_trainer_topics(message) -> None:
    await message.reply_text(
        "📚 Выбери тему для тренировки вопросов:",
        reply_markup=_trainer_topics_keyboard(),
    )


async def _show_trainer_topics_after_edit(query) -> None:
    await query.message.reply_text(
        "👇 Выбери тему:",
        reply_markup=_trainer_topics_keyboard(),
    )


async def _send_long_message(message, text: str, chunk_size: int = 4000) -> None:
    if len(text) <= 4096:
        await message.reply_text(text)
        return
    for i in range(0, len(text), chunk_size):
        await message.reply_text(text[i : i + chunk_size])


def _escape_md(text: str) -> str:
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отчёт по текущей коуч-сессии по команде."""
    if not update.effective_user or not update.message:
        return
    if not _is_allowed(update.effective_user):
        await update.message.reply_text("⛔ Доступ ограничен. Обратитесь к администратору.")
        await _notify_admin_denied(context, update.effective_user)
        return
    session = get_session(update.effective_user.id)
    if session["mode"] != "coach" or not session["history"]:
        await update.message.reply_text(
            "Отчёт доступен в режиме Коуч после начала сессии. Нажми /start."
        )
        return
    await _send_session_report(
        update, context, session,
        update.effective_user.id, update.effective_user.username,
    )
    _save_sessions()


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открыть доступ пользователю: /adduser ник (только админ)."""
    if not update.effective_user or not update.message:
        return
    if not _is_admin(update.effective_user):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /adduser ник (можно с @)")
        return
    username = args[0].lstrip("@")
    if username in _allowed_users:
        await update.message.reply_text(f"@{username} уже в списке доступа.")
        return
    _allowed_users.add(username)
    _save_allowed_users()
    await update.message.reply_text(f"✅ Доступ открыт: @{username}")


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закрыть доступ пользователю: /removeuser ник (только админ)."""
    if not update.effective_user or not update.message:
        return
    if not _is_admin(update.effective_user):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /removeuser ник (можно с @)")
        return
    username = args[0].lstrip("@")
    if username == ADMIN_USERNAME:
        await update.message.reply_text("Нельзя закрыть доступ администратору.")
        return
    if username not in _allowed_users:
        await update.message.reply_text(f"@{username} нет в списке доступа.")
        return
    _allowed_users.discard(username)
    _save_allowed_users()
    await update.message.reply_text(f"✅ Доступ закрыт: @{username}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика бота — только для администратора."""
    del context
    if not update.effective_user or not update.message:
        return
    if not _is_admin(update.effective_user):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return

    now = time_module.time()
    day_secs  = 86400
    week_secs = 7 * day_secs

    total_users   = len(_user_activity)
    active_24h    = []
    active_7d     = []
    new_24h       = []
    total_cost_all = 0.0
    daily_cost_all = 0.0
    today_iso = datetime.date.today().isoformat()

    user_costs: list[tuple[str, float]] = []

    for uid, entry in _user_activity.items():
        inp   = entry.get("total_input_tokens", 0)
        out   = entry.get("total_output_tokens", 0)
        cost  = inp * CLAUDE_PRICE_INPUT + out * CLAUDE_PRICE_OUTPUT
        total_cost_all += cost

        uname = entry.get("username") or f"id:{uid}"
        label = f"@{uname}" if not uname.startswith("id:") else uname
        user_costs.append((label, cost))

        last  = entry.get("last_seen", 0)
        first = entry.get("first_seen", 0)
        if now - last <= day_secs:
            active_24h.append(label)
        if now - last <= week_secs:
            active_7d.append(label)
        if now - first <= day_secs:
            new_24h.append(label)

        # дневные затраты
        day_tokens = entry.get("daily_tokens", {}).get(today_iso, {})
        daily_cost_all += (
            day_tokens.get("input", 0) * CLAUDE_PRICE_INPUT
            + day_tokens.get("output", 0) * CLAUDE_PRICE_OUTPUT
        )

    # Сортировка по затратам
    user_costs.sort(key=lambda x: x[1], reverse=True)

    lines = ["📊 Статистика бота\n"]

    lines.append(f"👥 Юзеры — Всего: {total_users}")
    for label, cost in user_costs:
        lines.append(f"  {label}  ${cost:.4f}")

    lines.append(f"\n⚡ Активных за 24ч: {len(active_24h)}")
    if active_24h:
        lines.append(", ".join(active_24h))

    lines.append(f"\n📅 За 7 дней: {len(active_7d)}")
    if active_7d:
        lines.append(", ".join(active_7d))

    lines.append(f"\n🆕 Новых за сутки: {len(new_24h)}")
    if new_24h:
        lines.append(", ".join(new_24h))

    lines.append(f"\n💰 Итоговый Cost API")
    lines.append(f"За сутки: ${daily_cost_all:.4f}")
    lines.append(f"Всего: ${total_cost_all:.4f}")

    await update.message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error", exc_info=context.error)


async def _post_init(application) -> None:
    """Инициализация БД и однократный перенос старых JSON при запуске."""
    await init_db()
    try:
        res = await migrate_legacy_json()
        logger.info("Legacy migrate: %s", res)
    except Exception as e:  # миграция не должна ронять бот
        logger.warning("Legacy migrate skipped: %s", e)
    # Фоновый планировщик напоминаний.
    asyncio.create_task(scheduler.run_loop(application.bot))
    # HTTP-мост для Mini App и вебхука ЮKassa.
    if cfg.RUN_BRIDGE:
        try:
            await bridge.start_bridge(application.bot, cfg.BRIDGE_HOST, cfg.BRIDGE_PORT)
        except Exception:
            logger.exception("Bridge failed to start")


def main() -> None:
    print("=== BOT STARTING ===")
    print("TELEGRAM_TOKEN set:", bool(TELEGRAM_TOKEN))
    print("ANTHROPIC_API_KEY set:", bool(ANTHROPIC_API_KEY))
    print("Topics loaded:", len(TRAINER_TOPICS))
    print("Topic details loaded:", len(TOPIC_DETAILS))

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")

    os.makedirs(DATA_DIR, exist_ok=True)
    _load_user_names()
    _load_activity()
    _load_sessions()
    _load_allowed_users()
    _load_admin_chat()
    init_claude()
    init_openai()
    from services import llm
    llm.init()

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("topics", topics_cmd))
    application.add_handler(CommandHandler("web", web_cmd))
    application.add_handler(CommandHandler("report", report_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("adduser", adduser_cmd))
    application.add_handler(CommandHandler("removeuser", removeuser_cmd))
    application.add_handler(CallbackQueryHandler(mode_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_error_handler(error_handler)

    print("✅ Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
