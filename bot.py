"""
Telegram-бот персонального коуча глубинной трансформации.
Стек: python-telegram-bot + Anthropic Claude API + OpenAI Whisper.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Dict, List

import anthropic
import openai
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from knowledge_base import (
    COACH_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    WELCOME_MESSAGE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20251001")
MAX_HISTORY = 40

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

# Ключевые слова для запроса отчёта
REPORT_KEYWORDS = {
    "отчёт", "отчет", "подведи итог", "подведи итоги",
    "что замечаешь", "что заметил", "что заметила",
    "резюме сессии", "итог сессии", "что было", "краткое резюме",
}

sessions: Dict[int, Dict[str, Any]] = {}
claude_client: anthropic.Anthropic | None = None
openai_client: openai.OpenAI | None = None


def _is_allowed(user) -> bool:
    if not user or not user.username:
        return False
    return user.username in ALLOWED_USERS


def get_session(user_id: int) -> Dict[str, Any]:
    if user_id not in sessions:
        sessions[user_id] = {"history": []}
    return sessions[user_id]


def init_clients() -> None:
    global claude_client, openai_client

    if ANTHROPIC_API_KEY:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("Anthropic client initialized")
    else:
        logger.warning("ANTHROPIC_API_KEY не задан")

    if OPENAI_API_KEY:
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI client initialized")
    else:
        logger.warning("OPENAI_API_KEY не задан — голосовые сообщения недоступны")


def ask_claude(system_prompt: str, history: List[dict], user_message: str) -> str:
    if claude_client is None:
        return (
            "К сожалению, API ключ не настроен. "
            "Добавь переменную ANTHROPIC_API_KEY в настройках Railway."
        )

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=messages,
        )
        blocks = response.content or []
        text_parts = [
            block.text
            for block in blocks
            if getattr(block, "type", "") == "text"
        ]
        reply = "\n".join(part.strip() for part in text_parts if part and part.strip())
        return reply or "Произошла ошибка — пустой ответ. Попробуй ещё раз."
    except anthropic.APIError:
        logger.exception("Claude API error")
        return "Произошла ошибка при обращении к Claude. Проверь API ключ и лимиты."
    except Exception:
        logger.exception("Unexpected error")
        return "Что-то пошло не так. Попробуй ещё раз."


def _is_report_request(text: str) -> bool:
    lower = text.lower().strip()
    return any(keyword in lower for keyword in REPORT_KEYWORDS)


async def _process_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    """Общая логика обработки текста (из сообщения или голоса)."""
    user_id = update.effective_user.id
    session = get_session(user_id)
    history = session["history"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    if _is_report_request(user_text) and history:
        reply = ask_claude(REPORT_SYSTEM_PROMPT, history, "Составь отчёт по нашей сессии.")
    else:
        reply = ask_claude(COACH_SYSTEM_PROMPT, history, user_text)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply})

        if len(history) > MAX_HISTORY * 2:
            history[:] = history[-(MAX_HISTORY * 2):]

    await _send_long_message(update.message, reply)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    if not _is_allowed(update.effective_user):
        await update.message.reply_text("Доступ ограничен. Обратись к администратору.")
        return

    sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(WELCOME_MESSAGE)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return
    sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "Сессия сброшена. Начнём заново — с чем ты пришёл(а) сегодня?"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return
    text = (
        "Команды бота:\n\n"
        "/start — начать новую сессию\n"
        "/reset — сбросить текущую сессию\n"
        "/help — эта справка\n\n"
        "Просто пиши или отправляй голосовые сообщения.\n\n"
        "Чтобы получить отчёт по сессии — напиши «отчёт» или «подведи итог»."
    )
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return

    if not _is_allowed(update.effective_user):
        await update.message.reply_text("Доступ ограничен. Обратись к администратору.")
        return

    user_text = (update.message.text or "").strip()
    if not user_text:
        await update.message.reply_text("Напиши что-нибудь или отправь голосовое сообщение.")
        return

    await _process_text(update, context, user_text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return

    if not _is_allowed(update.effective_user):
        await update.message.reply_text("Доступ ограничен. Обратись к администратору.")
        return

    if openai_client is None:
        await update.message.reply_text(
            "Голосовые сообщения пока недоступны — не настроен OPENAI_API_KEY."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    text = None
    error_info = ""

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)
        size = os.path.getsize(tmp_path)
        if size == 0:
            error_info = "файл пустой"
        else:
            with open(tmp_path, "rb") as audio_file:
                result = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("voice.ogg", audio_file, "audio/ogg"),
                    language="ru",
                )
            text = (result.text or "").strip() or None
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
        await update.message.reply_text(
            f"Не удалось распознать голосовое сообщение{detail}. Попробуй ещё раз."
        )
        return

    await _process_text(update, context, text)


async def _send_long_message(message, text: str, chunk_size: int = 4000) -> None:
    if len(text) <= 4096:
        await message.reply_text(text)
        return
    for i in range(0, len(text), chunk_size):
        await message.reply_text(text[i: i + chunk_size])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error", exc_info=context.error)


def main() -> None:
    print("=== BOT STARTING ===")
    print("TELEGRAM_TOKEN set:", bool(TELEGRAM_TOKEN))
    print("ANTHROPIC_API_KEY set:", bool(ANTHROPIC_API_KEY))
    print("OPENAI_API_KEY set:", bool(OPENAI_API_KEY))

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")

    init_clients()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_error_handler(error_handler)

    print("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
