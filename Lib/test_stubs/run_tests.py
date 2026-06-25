"""Логические тесты бота без сети и реального Telegram/Claude."""
import asyncio
import sys, os

STUBS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(STUBS, "..", ".."))
sys.path.insert(0, STUBS)
sys.path.insert(0, ROOT)
os.chdir(STUBS)  # чтобы json-файлы не засоряли репо

import bot

PASS, FAIL = 0, []

def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name}  {detail}")

# ---------- извлечение имени ----------
check("имя: «меня зовут Эрнест»", bot._extract_name_mention("меня зовут Эрнест") == "Эрнест")
check("имя: «Зови меня Аня»", bot._extract_name_mention("Зови меня Аня") == "Аня")
check("имя: «моё имя Виктор»", bot._extract_name_mention("моё имя Виктор") == "Виктор")
check("имя: обычный текст → None", bot._extract_name_mention("хочу обсудить работу") is None)
check("имя: «зовут как-то» без имени", bot._extract_name_mention("как меня зовут?") in (None, "Как", "?"))

# ---------- кризис / тяжёлые темы ----------
check("кризис: суицидальный сигнал", bot._is_crisis("не хочу жить больше"))
check("кризис: обычный текст → False", not bot._is_crisis("надо поменять работу"))
check("тяжёлая тема обнаруживается", bot._is_heavy_topic("у меня умерла мама"))
check("тяжёлая тема: «рак» как слово", bot._is_heavy_topic("у отца рак"))
check("лёгкая тема → False", not bot._is_heavy_topic("хочу повышение"))
check("нет ложного: «характер»", not bot._is_heavy_topic("у меня такой характер"))
check("нет ложного: «умеренный»", not bot._is_heavy_topic("веду умеренный образ жизни"))
check("нет ложного: «автомобиль»", not bot._is_heavy_topic("купил автомобиль"))
check("нет ложного: «ракурс»", not bot._is_heavy_topic("посмотрим с другого ракурса"))

# ---------- валидация имени ----------
check("имя ок: «Эрнест»", bot._looks_like_name("Эрнест"))
check("имя ок: «Анна Мария»", bot._looks_like_name("Анна Мария"))
check("не имя: вопрос", not bot._looks_like_name("как найти вторую работу?"))
check("не имя: длинная фраза", not bot._looks_like_name("хочу поговорить про смену карьеры"))
check("не имя: цифры", not bot._looks_like_name("user123"))
check("не имя: пусто", not bot._looks_like_name("  "))

# ---------- сессии и фазы ----------
s = bot.get_session(111)
check("новая сессия: фаза ask_name (имя неизвестно)", s["coach_phase"] == bot.COACH_PHASE_ASK_NAME)
bot._store_name(222, "Тест")
s2 = bot.get_session(222)
check("сессия с известным именем: фаза contracting", s2["coach_phase"] == bot.COACH_PHASE_CONTRACTING)
check("имя подставлено в сессию", s2["user_name"] == "Тест")

# ---------- переходы фаз ----------
s3 = {"coach_phase": bot.COACH_PHASE_CONTRACTING, "coach_exchanges": 0}
r = bot._coach_phase_transition(s3, "Отлично, начнём!\n[КОНТРАКТ_УСТАНОВЛЕН]", 1)
check("маркер контракта → WORKING", s3["coach_phase"] == bot.COACH_PHASE_WORKING)
check("маркер удалён из ответа", "[КОНТРАКТ_УСТАНОВЛЕН]" not in r)

s4 = {"coach_phase": bot.COACH_PHASE_CONTRACTING, "coach_exchanges": 0}
bot._coach_phase_transition(s4, "без маркера", bot.COACH_CONTRACTING_MAX_TURNS)
check("страховка по числу обменов → WORKING", s4["coach_phase"] == bot.COACH_PHASE_WORKING)

s5 = {"coach_phase": bot.COACH_PHASE_WORKING, "coach_exchanges": bot.COACH_WORKING_MAX_TURNS - 1,
      "coach_working_limit": bot.COACH_WORKING_MAX_TURNS, "coach_awaiting_close_confirm": False}
bot._coach_phase_transition(s5, "ответ", 99)
check("лимит WORKING → запрос подтверждения закрытия", s5["coach_awaiting_close_confirm"] is True)

s6 = dict(s5); s6["coach_awaiting_close_confirm"] = "shown"; s6["coach_exchanges"] = 99
bot._coach_phase_transition(s6, "ответ", 100)
check("подтверждение не показывается повторно", s6["coach_awaiting_close_confirm"] == "shown")

check("фазы CLOSING больше нет", not hasattr(bot, "COACH_PHASE_CLOSING"))
check("маркера завершения больше нет", not hasattr(bot, "COACH_CLOSED_MARKER"))

# ---------- системный промпт коуча ----------
sp = bot._get_coach_system_prompt({"coach_phase": bot.COACH_PHASE_WORKING, "coach_exchanges": 0, "user_name": "Аня"})
check("имя инжектится в промпт", "Аня" in sp)
sp_heavy = bot._get_coach_system_prompt({"coach_phase": bot.COACH_PHASE_WORKING, "coach_exchanges": 0,
                                         "user_name": None}, heavy_topic=True)
check("тяжёлая тема инжектится", len(sp_heavy) > len(bot.COACH_SYSTEM_PROMPT))
sp_near = bot._get_coach_system_prompt({"coach_phase": bot.COACH_PHASE_WORKING,
                                        "coach_exchanges": bot.COACH_WORKING_NEAR_END, "user_name": None})
check("инъекция «завершай» при близком конце", "приближается к завершению" in sp_near)

# ---------- шапка ----------
h = bot._mode_header({"mode": "coach", "coach_phase": bot.COACH_PHASE_WORKING, "coach_exchanges": 3})
check("шапка коуча с прогрессом", "3/" in h and "█" in h)
check("шапка тренера", "Тренер" in bot._mode_header({"mode": "trainer", "trainer_topic": None, "trainer_exchanges": 0}))
check("нет режима → пустая шапка", bot._mode_header({"mode": None}) == "")

# ---------- клавиатуры ----------
kb = bot._trainer_topics_keyboard()
n_buttons = sum(len(row) for row in kb.inline_keyboard)
check(f"тренер: все 27 тем на клавиатуре ({n_buttons})", n_buttons == len(bot.TRAINER_TOPICS))
check("тренер: максимум 2 в ряд", all(len(row) <= 2 for row in kb.inline_keyboard))

# ---------- целостность данных ----------
import knowledge_base as kbm
missing_details = [k for k in kbm.TRAINER_TOPICS if k not in kbm.TOPIC_DETAILS]
check(f"у всех тем тренера есть TOPIC_DETAILS (нет: {missing_details})", not missing_details)
missing_q = [k for k in kbm.INSTRUMENTS if k not in kbm.INSTRUMENT_QUESTIONS]
check(f"у всех инструментов есть вопросы (нет: {missing_q})", not missing_q)
no_short = [k for k, v in kbm.TRAINER_TOPICS.items() if not v.get("short_name")]
check(f"у всех тем есть short_name (нет: {no_short})", not no_short)
long_btn = [k for k, v in kbm.TRAINER_TOPICS.items()
            if len(f"{v['emoji']} {v.get('short_name') or v['name']}") > 26]
check(f"кнопки тем не длиннее ~26 симв. (длинные: {long_btn})", not long_btn)
long_instr = [k for k, v in kbm.INSTRUMENTS.items() if len(f"{v['emoji']} {v['name']}") > 26]
check(f"кнопки инструментов не длиннее ~26 симв. (длинные: {long_instr})", not long_instr)

# callback_data лимит Telegram = 64 байта
bad_cb = [k for k in list(kbm.TRAINER_TOPICS) + list(kbm.INSTRUMENTS)
          if len(f"topic_{k}".encode()) > 64]
check(f"callback_data ≤ 64 байт (плохие: {bad_cb})", not bad_cb)

# маркер не должен встречаться в текстах вопросов
marker_leak = [k for k, v in kbm.INSTRUMENT_QUESTIONS.items() if "###" in v or "**" in v]
check(f"инструменты: без markdown-мусора (плохие: {marker_leak})", not marker_leak)

# ---------- отчёт / ключевые слова ----------
check("отчёт: «дай отчёт»", bot._is_report_request("дай отчёт по сессии"))
check("отчёт: обычная фраза → False", not bot._is_report_request("давай поговорим о работе"))

# ---------- статистика ----------
bot._user_activity.clear()
import time
now = time.time()
bot._user_activity["1"] = {"username": "alice", "first_seen": now - 3 * 86400, "last_seen": now - 100,
                           "total_input_tokens": 1_000_000, "total_output_tokens": 100_000,
                           "daily_tokens": {}}
# cost = 1M*3/1M + 0.1M*15/1M = 3 + 1.5 = 4.5
cost = (bot._user_activity["1"]["total_input_tokens"] * bot.CLAUDE_PRICE_INPUT
        + bot._user_activity["1"]["total_output_tokens"] * bot.CLAUDE_PRICE_OUTPUT)
check("расчёт стоимости верный (4.5)", abs(cost - 4.5) < 1e-9)

# ---------- персистентность сессий ----------
bot.sessions.clear()
s_test = bot.get_session(333)
s_test["mode"] = "coach"
s_test["coach_phase"] = bot.COACH_PHASE_WORKING
s_test["history"] = [{"role": "user", "content": "привет"}]
bot._save_sessions()
bot.sessions.clear()
bot._load_sessions()
restored = bot.sessions.get(333)
check("сессия восстановлена после перезапуска", restored is not None)
check("ключи сессий — int", all(isinstance(k, int) for k in bot.sessions))
check("история восстановлена", restored and restored["history"][0]["content"] == "привет")
check("фаза восстановлена", restored and restored["coach_phase"] == bot.COACH_PHASE_WORKING)

# ---------- список доступа ----------
bot._allowed_users = set(bot.ALLOWED_USERS)
class _U:
    def __init__(self, username): self.username = username
check("допущенный пользователь проходит", bot._is_allowed(_U("ErnestKh8")))
check("чужой не проходит", not bot._is_allowed(_U("hacker")))
bot._allowed_users.add("newcoach")
check("динамически добавленный проходит", bot._is_allowed(_U("newcoach")))
check("админ определяется", bot._is_admin(_U("ErnestKh8")))
check("не-админ определяется", not bot._is_admin(_U("nosokvik")))

# ---------- профиль коучи ----------
s_prof = {"coachee_profile": None}
note = bot._coachee_profile_note(s_prof)
check("профиль выбран кодом", s_prof["coachee_profile"] in bot.COACHEE_PROFILE_KEYS)
check("профиль в инъекции", s_prof["coachee_profile"] in note)
note2 = bot._coachee_profile_note(s_prof)
check("профиль стабилен в рамках сессии", note == note2)
profiles = {bot._coachee_profile_note({"coachee_profile": None}) for _ in range(60)}
check(f"профили разнообразны ({len(profiles)} из 6)", len(profiles) >= 4)

# ---------- приветствие возвращающегося ----------
check("короткое приветствие содержит имя",
      "Тест" in kbm.COACH_MODE_START_RETURNING.format(name="Тест"))
check("короткое приветствие реально короче",
      len(kbm.COACH_MODE_START_RETURNING) < len(kbm.COACH_MODE_START) / 2)

# ---------- модель и тренер-финал ----------
check("дефолтная модель — claude-sonnet-4-6 (не устаревшая)",
      bot.CLAUDE_MODEL == "claude-sonnet-4-6" or "20250514" not in bot.CLAUDE_MODEL)
check("лимит практики тренера = 10", bot.TRAINER_MAX_EXCHANGES == 10)
check("финальная нота требует разбор", "итоговый разбор" in bot.TRAINER_FINALE_NOTE)

# ---------- админ-чат и уведомления ----------
bot._admin_chat_id = None
bot._store_admin_chat(12345)
check("admin chat id сохранён", bot._admin_chat_id == 12345)
bot._load_admin_chat()
check("admin chat id восстановлен из файла", bot._admin_chat_id == 12345)

# ---------- ask_claude без ключа ----------
bot.client = None
reply = asyncio.run(bot.ask_claude("sys", [], "msg"))
check("ask_claude без ключа → понятная ошибка", "ANTHROPIC_API_KEY" in reply)

print()
print(f"PASSED: {PASS}, FAILED: {len(FAIL)}")
if FAIL:
    print("Провалены:", *FAIL, sep="\n  - ")
    sys.exit(1)
