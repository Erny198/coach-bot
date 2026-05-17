# Бот персонального коуча

Telegram-бот коучинга глубинной трансформации на базе методологии Next Level Transformation Academy.

## Переменные окружения

Задай в Railway (Settings → Variables):

| Переменная | Описание |
|---|---|
| `TELEGRAM_TOKEN` | Токен бота от @BotFather |
| `ANTHROPIC_API_KEY` | Ключ Anthropic Claude API |
| `OPENAI_API_KEY` | Ключ OpenAI (для голосовых сообщений / Whisper) |
| `CLAUDE_MODEL` | Модель Claude (по умолчанию `claude-sonnet-4-5-20251001`) |

## Деплой на Railway

1. Загрузи репозиторий на GitHub
2. Создай новый проект на Railway → Deploy from GitHub repo
3. Добавь переменные окружения
4. Railway автоматически соберёт Docker-образ и запустит бота

## Команды бота

- `/start` — начать новую сессию
- `/reset` — сбросить текущую сессию
- `/help` — справка

Для получения отчёта по сессии напиши «отчёт» или «подведи итог».
