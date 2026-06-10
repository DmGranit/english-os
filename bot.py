"""
English OS — каркас телеграм-бота.
Связывает: db (контент+SRS+интейк) · prompts (ЯДРО+режим) · llm (вызов модели).

Запуск:
    pip install -r requirements.txt
    python seed.py                       # один раз: залить контент из JSON
    export TELEGRAM_TOKEN=...  LLM_API_KEY=...
    python bot.py

Готово: режим повторения карточками (RU->EN) с кнопками «вспомнил/забыл» -> db.review.
Готово: разбор машинного JSON-блока ИТОГ -> db.apply_session_summary -> reviews/pending.
Готово: очередь подтверждения /pending (confirm/reject add-слов, защита от дубликатов).

Что осознанно оставлено как TODO (помечено ниже):
    - парсинг свободного ввода «Учим X, Y» в режим+слова (или только кнопки)
"""
import os, json, re, time, asyncio, datetime, logging, types, urllib.parse, zlib
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove,
                      BotCommand, BotCommandScopeChat)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, ApplicationHandlerStop,
                          ContextTypes, PicklePersistence, filters)
import db, prompts, llm, enrich

log = logging.getLogger("english_os")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CONTENT_USER = db.DEFAULT_USER   # общий пул контента и очереди pending (его наполняют seed.py/enrich.py)
# allowlist: кому вообще можно пользоваться ботом. Пусто -> пускаем всех (на время настройки).
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(";", ",").split(",")
                 if x.strip().isdigit()}
# админ контента (кто разбирает /pending). Пусто -> /pending доступен любому из allowlist.
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID", "").strip().isdigit() else None
# умная модель для ЖИВОГО диалога (роль в сценарии держит лучше); остальное — на дешёвой по умолчанию
SMART_MODEL = os.environ.get("SMART_MODEL", "gpt-4o")
SMART_MODES = {"scenario", "flow"}
_REMINDER_TASK = None   # держим ссылку на фоновый цикл напоминаний
_IDLE_TASK = None       # фоновый цикл авто-ИТОГа по неактивности

def _learner(update):
    """Telegram-id пользователя: у каждого свой прогресс (общий только контент)."""
    return update.effective_user.id

# режим и история диалога живут в context.user_data; PicklePersistence переживает рестарт
MAX_HISTORY = 20    # держим последние 20 сообщений (~10 обменов) — чтобы контекст не рос без предела

def _persistence(path=None):
    """Хранилище сессий: режим, история и last_seen переживают рестарт бота."""
    return PicklePersistence(
        filepath=path or os.environ.get("ENGLISH_OS_STATE", "bot_state.pickle"))

def _mode(ctx):     return ctx.user_data.get("mode", "flow")
def _history(ctx):  return ctx.user_data.setdefault("history", [])

def _remember(hist, role, content):
    """Добавить сообщение и оставить только последние MAX_HISTORY (свежий хвост)."""
    hist.append({"role": role, "content": content})
    hist[:] = hist[-MAX_HISTORY:]     # [-MAX_HISTORY:] = последние N; старые отбрасываются
    while hist and hist[0]["role"] == "assistant":   # Anthropic требует первым ход user
        hist.pop(0)

# ---------- лимит Telegram: одно сообщение <= 4096 символов ----------
TG_LIMIT = 4096

def _chunks(text, limit=TG_LIMIT):
    """Разрезать текст под лимит: по строкам/абзацам; жёсткий срез — только если
    одна строка сама длиннее лимита. «\\n».join(parts) восстанавливает текст."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = ""
        while len(line) > limit:                  # строка-монстр — режем жёстко
            if cur:
                parts.append(cur); cur = ""
            parts.append(line[:limit]); line = line[limit:]
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts

async def _say(msg, text, markup=None):
    """reply_text с разрезанием под лимит; клавиатура — на последней части."""
    parts = _chunks(text)
    for p in parts[:-1]:
        await msg.reply_text(p)
    return await msg.reply_text(parts[-1], reply_markup=markup)

async def _deliver(out, text, markup=None):
    """Показать длинный текст: первая часть через out (edit или reply), хвост — реплаями."""
    parts = _chunks(text)
    m = await out(parts[0], markup if len(parts) == 1 else None)
    for k, p in enumerate(parts[1:], start=2):
        m = await m.reply_text(p, reply_markup=markup if k == len(parts) else None)
    return m

# машинный блок ```json {...}``` в конце ответа модели (контракт ИТОГ)
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
# ИТОГ = человеческий отчёт + машинный JSON: дефолтных 600 токенов не хватает, JSON обрезается
SUMMARY_MAX_TOKENS = 1600

def _extract_summary(text):
    """Вырезать машинный JSON-блок из ответа модели.
    Возвращает (данные|None, текст_без_json для показа пользователю)."""
    matches = list(_JSON_BLOCK.finditer(text))
    if not matches:
        return None, text
    m = matches[-1]      # по контракту машинный блок — ПОСЛЕДНИЙ; раньше могут быть примеры
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, text             # битый JSON — показать текст как есть, ничего не писать
    clean = (text[:m.start()] + text[m.end():]).strip()
    return data, clean

# ---------- завершение сессии (общая точка: «Закончили», кнопка «🏁 Итог», авто-ИТОГ) ----------
END_WORDS = {"закончили", "отчёт", "отчет", "стоп", "итог", "🏁 итог"}
IDLE_SUMMARY_SEC = 25 * 60   # тишина, после которой сессия сводится автоматически

END_PROMPT = ("<КОНЕЦ СЕССИИ> Заверши занятие: дай человеческий отчёт ИТОГ, а в самом конце — "
              "машинный JSON-блок (reviewed/add/errors). Не переводи это сообщение и не проси повторить.")

async def _finish_session(ud, uid, send):
    """Свести сессию: ИТОГ от модели -> запись в базу -> отчёт через send(text).
    ud — user_data пользователя (история/режим). История после сведения очищается."""
    if not ud.get("history"):                      # пустая сессия — LLM не дёргаем
        await send("Пока нечего подводить — мы ещё не общались в этой сессии 🙂 "
                   "Напиши пару фраз или нажми ☀️ Повторить.")
        return
    ctx = types.SimpleNamespace(user_data=ud)      # _call работает только с user_data
    reply = await _call(ctx, ud.get("mode", "flow"), uid, END_PROMPT, with_summary=True)
    data, reply = _extract_summary(reply)
    reply = _strip_fences(reply)                   # человеческий отчёт — без ```-заборов
    if data:
        r = db.apply_session_summary(data, uid)
        reply += (f"\n\n— записано в базу: повторений {r['ok'] + r['fail']} "
                  f"(✅ {r['ok']} / ❌ {r['fail']}), новых слов в очередь: {r['added']}"
                  f", структурных ошибок: {r.get('errors', 0)}")
    db.backup()
    db.log_session(uid, ud.get("mode", "flow"))    # след сессии — для карты дня
    ud["history"] = []                             # сессия закрыта — авто-ИТОГ не повторится
    ud["mode"] = "flow"                            # роль снята: следующий текст — просто разговор
    for p in _chunks(reply):                       # отчёт может не влезть в одно сообщение
        await send(p)
    # носитель клавиатуры: подсказка слота (cycle) или нейтральный текст (free)
    await send(_next_action_text(uid), reply_markup=MAIN_KB)

# ---------- программа дня: слоты, карта, подсказки ----------
_SLOTS = [("new", "🌅", "Новые"), ("review", "☀️", "Повторить"), ("scenario", "🎭", "Сценарий")]

# времена слотов: парсинг пользовательского ввода и формат показа (минуты от полуночи)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3])(?::([0-5]\d))?$")

def _fmt_time(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"

def _parse_time(tok):
    """«8» / «8:30» -> минуты от полуночи; None, если не время."""
    m = _TIME_RE.match(tok or "")
    return int(m.group(1)) * 60 + int(m.group(2) or 0) if m else None

def _norm_tokens(text):
    """Терпимость к форматам: запятые — разделители, точка между числами = двоеточие."""
    return [t.replace(".", ":") for t in (text or "").replace(",", " ").split()]

def _parse_slot_times(text):
    """«9 14 19» / «8:30 13 19:15» / «9, 14, 19» -> [утро, день, вечер] в минутах; иначе None."""
    toks = _norm_tokens(text)
    if len(toks) != 3:
        return None
    times = [_parse_time(t) for t in toks]
    return times if all(t is not None for t in times) else None

def _handle_slot_input(ud, uid, text):
    """Если ждали ввода часов слотов — попытаться применить. Возвращает текст ответа или None
    (None = обработать сообщение обычным путём). Не застреваем: посторонний ввод снимает флаг;
    похожая на время опечатка — подсказка, ожидание сохраняется."""
    if not ud.pop("awaiting_slot_hours", False):
        return None
    times = _parse_slot_times(text)
    if times:
        for slot, t in zip(("morning", "day", "evening"), times):
            db.set_slot_time(uid, slot, t)
        return (f"Готово: 🌅 {_fmt_time(times[0])} · ☀️ {_fmt_time(times[1])} "
                f"· 🎭 {_fmt_time(times[2])} ✅")
    toks = _norm_tokens(text)
    if len(toks) == 3 and any(_parse_time(t) is not None for t in toks):  # похоже на попытку
        ud["awaiting_slot_hours"] = True
        return ("Не понял время 🤔 Напиши три времени через пробел — утро, день, вечер.\n"
                "Например: 9 14 19 или 8:30 13 19:15")
    return None                               # кнопка/обычный текст — дальше обычным путём

def _day_line(uid):
    """Строка карты дня + подсказка следующего слота. None для program='free'."""
    if db.get_program(uid) != "cycle":
        return None
    m = db.day_map(uid)
    marks = " · ".join(f"{icon} {'✅' if m[key] else '—'}" for key, icon, _ in _SLOTS)
    nxt = next((s for s in _SLOTS if not m[s[0]]), None)
    hint = f"Следующий слот: {nxt[1]} {nxt[2]}" if nxt else "Все слоты на сегодня закрыты 🎉"
    return f"Сегодня: {marks}\n{hint}"

def _next_slot_hint(uid):
    """Одна строка после завершения занятия: какой слот следующий. None для free."""
    if db.get_program(uid) != "cycle":
        return None
    m = db.day_map(uid)
    nxt = next((s for s in _SLOTS if not m[s[0]]), None)
    return f"Дальше по программе: {nxt[1]} {nxt[2]}" if nxt else "Все слоты дня закрыты 🎉"

def _next_action_text(uid):
    """Текст сообщения-носителя клавиатуры после завершения занятия (гибрид:
    кнопки всплывают ровно в момент выбора следующего действия)."""
    return _next_slot_hint(uid) or "Что дальше? 👇"

def _slot_reminder_text(uid, slot):
    """Текст слотового напоминания. None — молчим: слот уже закрыт или делать нечего."""
    if db.day_map(uid).get(slot):
        return None
    if slot == "new":
        if db.new_remaining(uid) == 0:
            return None
        return "🌅 Утренний слот: Новые слова — пара минут на разбор."
    if slot == "review":
        due, _ = db.due_today(uid)
        if not due:
            return None
        return f"☀️ Дневной слот: Повторить — {len(due)} слов ждут."
    return "🎭 Вечерний слот: Сценарий — разыграем рабочую ситуацию?"

# модель иногда оборачивает человеческий отчёт в ```-заборы (копирует шаблон буквально) —
# Telegram не знает markdown, и заборы выходят мусором на экране
_FENCE_LINE = re.compile(r"^\s*```\w*\s*$", re.M)

def _strip_fences(text):
    return _FENCE_LINE.sub("", text or "").strip()

def _idle_users(user_data, now=None, idle_sec=IDLE_SUMMARY_SEC):
    """Кому пора авто-ИТОГ: есть несведённый диалог и тишина >= idle_sec."""
    now = time.time() if now is None else now
    return [uid for uid, ud in user_data.items()
            if ud.get("history") and now - ud.get("last_seen", now) >= idle_sec]

# ---------- постоянная клавиатура управления ----------
BTN_NEW, BTN_REVIEW = "🌅 Новые", "☀️ Повторить"
BTN_SCEN, BTN_READ = "🎭 Сценарий", "📖 Читать"
BTN_TOPICS, BTN_PROG = "📚 Темы", "📈 Прогресс"
BTN_END = "🏁 Итог"
MAIN_KB = ReplyKeyboardMarkup(
    # учиться / выбирать и смотреть / завершить (Итог — во всю ширину, главное действие)
    [[BTN_NEW, BTN_REVIEW], [BTN_SCEN, BTN_READ], [BTN_TOPICS, BTN_PROG], [BTN_END]],
    resize_keyboard=True, one_time_keyboard=True,   # всплывающая: после нажатия сворачивается
    input_field_placeholder="Пиши по-английски · кнопки: ⌨")  # подсказка у значка возврата
MAIN_BUTTONS = {BTN_NEW, BTN_REVIEW, BTN_SCEN, BTN_READ, BTN_TOPICS, BTN_PROG}  # BTN_END — в END_WORDS

async def _route_button(update, ctx, uid, label):
    """Кнопка постоянной клавиатуры -> то же действие, что inline/команда."""
    m = update.message
    async def out(text, markup=None):
        return await m.reply_text(text, reply_markup=markup)
    if label == BTN_PROG:
        await progress_cmd(update, ctx)
    elif label == BTN_READ:
        await read_cmd(update, ctx)
    elif label == BTN_TOPICS:
        await topics_cmd(update, ctx)
    else:
        mode = {BTN_NEW: "new", BTN_REVIEW: "review", BTN_SCEN: "scenario"}[label]
        await _enter_mode(out, ctx, uid, mode, tmsg=m)

# ---------- /start ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = _learner(update)
    db.ensure_user_state(uid)
    if not db.is_onboarded(uid):                  # новый пользователь -> онбординг
        await _begin_onboarding(update, ctx)
        return
    # вернувшийся: ведущий CTA по самому нужному действию; управление — постоянной клавиатурой
    due, _ = db.due_today(uid)
    n_due = len(due)
    if n_due:
        lead = f"С возвращением! 👋\nСегодня {n_due} слов(а) ждут повторения — жми ☀️ Повторить."
    elif db.new_remaining(uid):
        lead = "С возвращением! 👋\nПовторять нечего ✅ — бери 🌅 Новые."
    else:
        lead = ("С возвращением! 👋\nВсё повторено, новых пока нет — поболтаем? "
                "Просто пиши на английском, темы — /topics, чтение — 📖 Читать.")
    day_line = _day_line(uid)                 # карта дня — только для программы 'cycle'
    if day_line:
        lead += "\n\n" + day_line
    await update.message.reply_text(
        lead + "\n\nКнопки управления — снизу 👇 Справка: /help", reply_markup=MAIN_KB)

# ---------- онбординг: органичный само-выбор темпа (БЕЗ присвоения уровня) ----------
_PACE = {"A2": "🌱 С самых основ", "B1": "🚶 Уже кое-что знаю", "B2": "🏃 Уверенно общаюсь"}

def _pace_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"pace:{band}")]
                                 for band, label in _PACE.items()])

async def _begin_onboarding(update, ctx):
    await update.message.reply_text(
        "Привет! Я English OS — тренер делового английского. 👋\n\n"
        "Как это работает, коротко:\n"
        "• учим слова не списком, а сетью: смысл → семья слов → как говорят носители;\n"
        "• повторяем по науке (интервалы) — чтобы не забывать;\n"
        "• можно болтать, разыгрывать сценарии, читать (/read) и говорить голосом;\n"
        "• я помню твой прогресс и подстраиваюсь под тебя.\n\n"
        "С чего комфортно начать? Это не оценка — подстроюсь по ходу.",
        reply_markup=_pace_kb())

async def on_pace(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    band = q.data.split(":")[1]
    uid = _learner(update)
    db.ensure_user_state(uid)
    db.mark_onboarded(uid, band)            # band хранится ВНУТРЕННЕ, пользователю не показываем
    ids = db.seed_starter_words(uid, band, n=5)
    starter = ", ".join(db.get_word(i)["word"] for i in ids) if ids else "—"
    await q.edit_message_text(
        "Отлично, начнём отсюда. 🎯\n"
        f"Первые слова: {starter}.\n\n"
        "Дальше я сам подстроюсь под тебя: пойдёт легко — добавлю посложнее; будет трудно — притормозим.\n"
        "Пиши «учим <слово>», говори голосом или жми 📖 Читать. Сменить темп — /pace.")
    await q.message.reply_text("Кнопки управления — снизу 👇 Справка: /help", reply_markup=MAIN_KB)
    await q.message.reply_text(
        "Как заниматься?\n"
        "🗓 Программа дня — веду по циклу: утром Новые, днём Повторение, вечером Сценарий, "
        "напоминаю только о незакрытых слотах.\n"
        "🆓 Свободный режим — сам выбираешь, что и когда.\n"
        "Любая кнопка работает всегда — программа лишь подсказывает порядок. Сменить: /program.",
        reply_markup=_program_kb())

# ---------- программа занятий: free | cycle ----------
def _program_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓 Программа дня: утро–день–вечер", callback_data="prog:cycle")],
        [InlineKeyboardButton("🆓 Свободный режим", callback_data="prog:free")],
    ])

async def program_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/program — выбрать режим занятий в любой момент."""
    cur = db.get_program(_learner(update))
    label = "🗓 программа дня" if cur == "cycle" else "🆓 свободный"
    await update.message.reply_text(
        f"Сейчас: {label}. Как заниматься дальше?", reply_markup=_program_kb())

async def on_program(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prog = q.data.split(":")[1]
    uid = _learner(update)
    db.set_program(uid, prog)
    if prog == "cycle":
        t = db.get_slot_times(uid)
        ctx.user_data["awaiting_slot_hours"] = True      # следующий текст — попытка задать часы
        await q.edit_message_text(
            "🗓 Программа дня включена.\n"
            "Буду напоминать трижды в день: 🌅 утром Новые слова · ☀️ днём Повторение · "
            "🎭 вечером Сценарий. Только если слот ещё не закрыт — позанимался раньше, промолчу.\n\n"
            "Зададим удобные часы. Напиши три времени через пробел — утро, день, вечер. "
            "Например: 9 14 19 (можно с минутами: 8:30 13 19:15).\n"
            f"Сейчас стоят {_fmt_time(t['morning'])} / {_fmt_time(t['day'])} / {_fmt_time(t['evening'])}.\n\n"
            "📅 А ещё можешь поставить ежедневное напоминание в свой календарь — кнопка ниже.",
            reply_markup=_cal_kb())
    else:
        await q.edit_message_text(
            "🆓 Свободный режим: занимайся, как удобно. Вернуть программу — /program.")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help — как пользоваться ботом (все ритуалы наглядно)."""
    await update.message.reply_text(
        "🧭 КАК ПОЛЬЗОВАТЬСЯ\n\n"
        "Кнопки снизу:\n"
        f"🌅 Новые — слова дня (до {db.DAILY_NEW_CAP} в день)\n"
        "☀️ Повторить — карточки, когда пора повторять\n"
        "🎭 Сценарий — ролевая игра: питч, переговоры, собеседование\n"
        "📖 Читать — короткий текст под твой уровень\n"
        "📚 Темы — учить слова по смыслу или сценарию\n"
        "📈 Прогресс — что ты уже можешь\n"
        "🏁 Итог — закончить занятие и записать прогресс\n\n"
        "А ещё:\n"
        "• 🗣️ Поток — без кнопки: просто пиши на английском, отвечу и мягко поправлю\n"
        "  (после Итога ты всегда возвращаешься сюда);\n"
        "• голосовые понимаю 🎤;\n"
        "• «учим invest, traction» — разберу эти слова;\n"
        "• замолчишь на 25 минут — сам подведу итог сессии;\n"
        "• кнопки прячутся после нажатия — вернуть их можно значком ⌨ в строке ввода.\n\n"
        "Команды: /topics темы · /mistakes мои ошибки · /pace темп · /program программа дня · "
        "/remind напоминания · /add слова в очередь",
        reply_markup=MAIN_KB)

async def pace_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pace — выбрать темп заново (без оценки уровня)."""
    await update.message.reply_text(
        "С каким темпом продолжить? Это не оценка — подстроюсь.", reply_markup=_pace_kb())

# ---------- переключение режима (inline-кнопка и постоянная клавиатура) ----------
async def on_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data.split(":")[1]
    uid = _learner(update)

    async def out(text, markup=None):                 # inline-путь правит то же сообщение
        if isinstance(markup, ReplyKeyboardRemove):   # edit не умеет reply-маркапы
            return await q.edit_message_text(text)
        return await q.edit_message_text(text, reply_markup=markup)

    await _enter_mode(out, ctx, uid, mode, tmsg=q.message)

async def _enter_mode(out, ctx, uid, mode, tmsg=None):
    """Вход в режим. out(text, markup) — способ показа: edit (inline) или reply (клавиатура)."""
    ctx.user_data["mode"] = mode
    ctx.user_data["history"] = []
    ctx.user_data.pop("awaiting_slot_hours", None)   # ушёл учиться — настройку часов отменяем
    db.ensure_user_state(uid)            # новый ученик / новые слова получают state

    if mode == "new":
        words = db.promote_new(uid)          # лимит держит сам db
        if not words:
            if db.new_remaining(uid) == 0:
                msg = ("🎉 Ты прошёл все новые слова базы! Пока добавить нечего — "
                       "закрепляем: жми ☀️ Повторить или 📖 Читать.")
            else:
                msg = (f"На сегодня дневная норма новых слов уже взята "
                       f"(по {db.DAILY_NEW_CAP}/день — так мозг усваивает лучше, без лавины повторений). "
                       f"Давай закрепим: ☀️ Повторить или 📖 Читать. Новые — завтра 🌙")
            await out(msg)
            return
        seed = ("Познакомь КРАТКО с новыми словами на сегодня: слово — перевод, 1 живой пример, "
                "1 коллокация. Без разбора корней/слоёв (это под кнопкой 🔍). "
                "В конце предложи составить свою фразу.\n" + db.format_for_agent(words))
        await _ask(out, ctx, mode, seed, uid, markup=_deep_kb(words), tmsg=tmsg)

    elif mode == "review":
        due, st = db.due_today(uid)
        if not due:
            await out("На сегодня повторять нечего ✅")
            return
        ctx.user_data["review_queue"] = [w["word_id"] for w in due]  # очередь слов
        ctx.user_data["review_box"]   = {wid: st[wid]["box"] for wid in st}  # box -> направление
        ctx.user_data["review_pos"]   = 0      # на какой карточке стоим
        ctx.user_data["review_ok"]    = 0      # счётчик «вспомнил»
        ctx.user_data["review_fail"]  = 0      # счётчик «забыл»
        ctx.user_data["card_shown_at"] = time.time()   # старт замера времени recall
        text, kb = _card_payload(ctx, uid)     # показать первую карточку
        await out(text, kb)

    else:  # scenario / flow — диалог: клавиатура с экрана убирается целиком,
           # вернётся сама с носителем после ИТОГа (one_time её прятал не до конца:
           # Telegram показывал кнопки снова при закрытии системной клавиатуры)
        await out({
            "scenario": "🎭 Сценарий. Назови ситуацию (питч, переговоры, статус) — войду в роль.",
            "flow": "🗣️ Поток. Просто общаемся на английском — пиши или говори голосом.",
        }[mode], ReplyKeyboardRemove())

# ---------- обычное сообщение (текст и голос идут одним путём) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _process_user_text(update, ctx, _learner(update), update.message.text)

def _stt_hints(ctx, uid):
    """Подсказки Whisper (A7): в разговорных режимах человек говорит по-английски —
    жёсткий language=en (без него акцент уводит автоопределение в чужой язык);
    prompt — слова в работе, чтобы именно их Whisper слышал правильно."""
    lang = "en" if _mode(ctx) in ("scenario", "flow") else None
    focus = ", ".join(w["word"] for w in db.learning_words(uid)) or None
    return lang, focus

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Голосовое: скачать -> распознать речь -> дальше как обычный текст."""
    uid = _learner(update)
    try:
        f = await update.message.voice.get_file()
        buf = await f.download_as_bytearray()
    except Exception:
        await update.message.reply_text("Не смог скачать голосовое. Попробуй ещё раз.")
        return
    lang, focus = _stt_hints(ctx, uid)
    text = await asyncio.to_thread(llm.transcribe, bytes(buf), language=lang, prompt=focus)
    if not text:
        await update.message.reply_text("🎤 Не расслышал. Повтори голосом или напиши текстом.")
        return
    await update.message.reply_text(f"🗣 You said: {text}")
    await _process_user_text(update, ctx, uid, text)

async def _process_user_text(update, ctx, uid, text):
    confirm = _handle_slot_input(ctx.user_data, uid, text)   # ждали часы слотов?
    if confirm:
        await update.message.reply_text(confirm)
        return

    # завершение сессии: слово-триггер или кнопка «🏁 Итог»
    if text.strip().lower() in END_WORDS:
        await _typing(update.message)
        await _finish_session(ctx.user_data, uid, update.message.reply_text)
        return

    if text in MAIN_BUTTONS:                     # постоянная клавиатура
        await _route_button(update, ctx, uid, text)
        return

    await _typing(update.message)                 # «печатает…», пока ИИ думает
    targets = _parse_learn_intent(text)          # «учим X, Y» -> разбор по графу
    if targets:
        await _teach_words(update, ctx, uid, targets)
        return

    reply = await _call(ctx, _mode(ctx), uid, text)
    await _say(update.message, reply)

# ---------- роутинг намерения «учим X» ----------
_LEARN_RE = re.compile(
    r"^\s*(?:давай\s+)?(?:учим|учить|выучим|поучим|разбер[её]м|разбери|хочу выучить|хочу учить|"
    r"teach me|i'?d like to learn|i want to learn|let'?s learn|new words?)\b[:\-\s]*(.+)$",
    re.I)

_LEARN_STOP = {"and", "or", "the", "a", "an", "to", "with", "и", "или",
               "some", "new", "more", "few", "word", "words", "vocabulary", "vocab",
               "business", "english", "phrase", "phrases"}

def _parse_learn_intent(text):
    """Если реплика вида «учим X, Y» — вернуть список английских слов, иначе None."""
    m = _LEARN_RE.search((text or "").strip())
    if not m:
        return None
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", m.group(1))
             if len(w) >= 2 and w.lower() not in _LEARN_STOP][:6]
    return words or None

async def _typing(msg):
    """Показать «печатает…», чтобы пауза на генерацию ИИ не выглядела зависанием."""
    try:
        await msg.chat.send_action("typing")
    except Exception:
        pass

def _deep_kb(wd):
    """Кнопки разбора: лупа + слово + перевод, и снизу — продолжения (не терять импульс)."""
    rows = [[InlineKeyboardButton(f"🔍 {x['word']} — {x['ru']}", callback_data=f"deep:{x['word_id']}")]
            for x in wd[:5]]
    rows.append([InlineKeyboardButton("➡️ Ещё новые", callback_data="mode:new"),
                 InlineKeyboardButton("☀️ Повторить", callback_data="mode:review")])
    return InlineKeyboardMarkup(rows)

async def _deliver_lesson(msg, ctx, uid, wd):
    """Лёгкое знакомство со словами + ввод в SRS. Глубина (корень/семья/слои) — под кнопкой 🔍."""
    db.start_learning([x["word_id"] for x in wd], uid)
    seed = ("Познакомь КРАТКО и по-дружески с этими словами, плотно. Для каждого: слово — перевод, "
            "1 живой пример, 1 коллокация. НЕ разбирай корни/семьи/слои (это под кнопкой 🔍). "
            "В конце предложи составить одну свою фразу с любым из них.\n" + db.format_for_agent(wd))
    system = prompts.assemble("new") + "\n\n" + db.learner_profile(uid)
    await _typing(msg)
    reply = await asyncio.to_thread(llm.chat, system, [{"role": "user", "content": seed}])
    await _say(msg, reply, _deep_kb(wd))

async def _teach_words(update, ctx, uid, words):
    """Слова из базы — разбор NEW + ввод в SRS + кнопки «Глубже»; новых — в очередь enrich/pending."""
    in_base = [w for w in words if db.find_word_id(w)]
    new = [w for w in words if not db.find_word_id(w)]
    if in_base:
        wd = [db.get_word(db.find_word_id(w)) for w in in_base]
        await _deliver_lesson(update.message, ctx, uid, wd)
    if new:
        await update.message.reply_text("⏳ Этих слов нет в базе — добавляю в очередь: " + ", ".join(new))
        res = await asyncio.to_thread(enrich.run, new)
        await update.message.reply_text(
            f"Добавил {res['added']} в очередь /pending (подтвердит админ).")

# ---------- «📚 Темы»: навигация по DNA-идеям и сценариям ----------
def _axis_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧬 По смыслу (DNA)", callback_data="topax:idea")],
        [InlineKeyboardButton("🎭 По сценарию (сфера)", callback_data="topax:scn")],
    ])

async def topics_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 Выбери срез тем:", reply_markup=_axis_kb())

async def on_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("📚 Выбери срез тем:", reply_markup=_axis_kb())

async def on_topax(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    axis = q.data.split(":")[1]
    pairs = db.idea_list() if axis == "idea" else db.scenario_list()
    head = ("🧬 ДНК-идея — это смысл-«магнит», вокруг которого живёт куст близких слов "
            "(напр. Investment: invest, fund, revenue). Выбери идею:") if axis == "idea" else \
           "🎭 Сфера/ситуация — где будешь применять язык. Выбери:"
    btns = [InlineKeyboardButton(f"{name} · {n}", callback_data=f"topic:{axis}:{name}") for name, n in pairs]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    await q.edit_message_text(head, reply_markup=InlineKeyboardMarkup(rows))

async def on_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":", 2)                  # topic : axis : value
    if len(parts) < 3:
        return
    _, axis, value = parts
    uid = _learner(update)
    wd = db.theme_words(axis, value, uid, n=5)
    if not wd:
        await q.message.reply_text("В этой теме пока нет слов.")
        return
    await q.message.reply_text(f"📚 Тема «{value}» — разбираем {len(wd)} слов(а).")
    await _deliver_lesson(q.message, ctx, uid, wd)

# ---------- вспомогательные ----------
async def _call(ctx, mode, uid, user_text, with_summary=False):
    system = prompts.assemble(mode, with_summary=with_summary) + "\n\n" + db.learner_profile(uid)
    matched = db.match_words(user_text)        # грунтовка: слова из базы в реплике
    if matched:
        system += ("\n\nСЛОВА ИЗ БАЗЫ в реплике (для «💬 Natural» бери ИХ коллокации/сеть, не выдумывай):\n"
                   + db.format_for_agent(matched))
    hist = _history(ctx)
    _remember(hist, "user", user_text)
    model = SMART_MODEL if mode in SMART_MODES else None   # диалог -> умная модель; структура -> дешёвая
    if with_summary:
        reply = await asyncio.to_thread(llm.chat, system, hist,
                                        model=model, max_tokens=SUMMARY_MAX_TOKENS)
    else:
        reply = await asyncio.to_thread(llm.chat, system, hist, model=model)
    _remember(hist, "assistant", reply)
    return reply

async def _ask(out, ctx, mode, seed_text, uid, markup=None, tmsg=None):
    """Первый ход режима: положить данные слов как user-сообщение и получить разбор.
    out(text, markup) — способ показа; tmsg — сообщение чата для индикатора «печатает…»."""
    system = prompts.assemble(mode) + "\n\n" + db.learner_profile(uid)
    hist = ctx.user_data["history"]
    _remember(hist, "user", seed_text)
    if tmsg:
        await _typing(tmsg)
    reply = await asyncio.to_thread(llm.chat, system, hist)
    _remember(hist, "assistant", reply)
    await _deliver(out, reply, markup)

# ---------- режим повторения: карточки ----------
def _review_kb(reveal):
    """Кнопки карточки. До показа ответа — одна кнопка; после — две оценки."""
    if not reveal:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("👁 Показать ответ", callback_data="rev:show")]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Вспомнил", callback_data="rev:ok"),
        InlineKeyboardButton("❌ Забыл", callback_data="rev:fail"),
    ]])

def _variant(user_id, word_id):
    """A/B-назначение карточки: детерминированная рандомизация ПОЛЬЗОВАТЕЛЬ×СЛОВО
    (crc32, не чётность id — она смешивала сложность конкретных слов с вариантом).
    У каждого пользователя своя раскладка слов по layered/flat."""
    return "layered" if zlib.crc32(f"{user_id}:{word_id}".encode()) & 1 == 0 else "flat"

def _network_block(word):
    """Блок «сети» для layered-карточки: связи слова (проверенные данные из базы)."""
    lines = []
    ri = db.root_info(word.get("root"))
    if ri:
        lines.append(f"🌳 корень: {word['root']} ({ri['idea']}; {ri['origin']})")
    if word.get("family"):
        lines.append("🌱 семья: " + ", ".join(word["family"]))
    if word.get("collocations"):
        lines.append("🔗 коллокации: " + ", ".join(word["collocations"]))
    if word.get("thinking_frame"):
        fi = db.frame_info(word["thinking_frame"])
        extra = f" — {fi['ru']}" if fi and fi.get("ru") else ""
        lines.append(f"🧠 фрейм: {word['thinking_frame']}{extra}")
    return "\n".join(lines)

def _review_card_text(word, pos, total, reveal, productive, variant="layered"):
    """Карточка. Направление: productive=True -> RU→EN, иначе EN→RU.
    Вариант: layered показывает «сеть» при раскрытии ответа, flat — только ответ (A/B)."""
    head = f"🔁 Повторение · карточка {pos}/{total}"
    ipa = f"  🔊 {word['ipa_uk']}" if word.get("ipa_uk") else ""
    ex  = f"\nПример: {word['example']}" if word.get("example") else ""
    if productive:                                   # RU -> EN
        if not reveal:
            return (f"{head}\n\nКак сказать по-английски:\n«{word['ru']}»  ({word['dna_idea']})\n\n"
                    f"Вспомни — потом нажми «Показать ответ».")
        answer = f"«{word['ru']}»\n→ {word['word']}{ipa}{ex}"
    else:                                            # EN -> RU (узнавание)
        if not reveal:
            return (f"{head}\n\nЧто это значит:\n«{word['word']}»{ipa}\n\n"
                    f"Вспомни перевод — потом нажми «Показать ответ».")
        answer = f"«{word['word']}»\n→ {word['ru']}{ex}"
    block = ("\n" + _network_block(word)) if variant == "layered" else ""
    return f"{head}\n\n{answer}{block}\n\nТы вспомнил?"

def _card_payload(ctx, uid, reveal=False):
    """Текст и клавиатура текущей карточки очереди."""
    queue = ctx.user_data.get("review_queue", [])
    pos = ctx.user_data.get("review_pos", 0)
    wid = queue[pos]
    word = db.get_word(wid)
    box = ctx.user_data.get("review_box", {}).get(wid, 1)
    productive = box >= db.PRODUCTIVE_FROM_BOX  # зрелое слово -> RU→EN; иначе EN→RU
    ctx.user_data["review_reveal"] = reveal
    return (_review_card_text(word, pos + 1, len(queue), reveal, productive, _variant(uid, wid)),
            _review_kb(reveal))

async def _show_card(q, ctx, uid, reveal=False):
    """Показать текущую карточку очереди (правит то же сообщение)."""
    if not reveal:
        ctx.user_data["card_shown_at"] = time.time()   # старт замера времени recall
    text, kb = _card_payload(ctx, uid, reveal)
    await q.edit_message_text(text, reply_markup=kb)

async def _finish_review(q, ctx, uid):
    """Колода кончилась: показать итог и сделать бэкап базы."""
    ok   = ctx.user_data.get("review_ok", 0)
    fail = ctx.user_data.get("review_fail", 0)
    db.backup()
    await q.edit_message_text(
        f"🔁 Повторение завершено!\n\n"
        f"Карточек: {ok + fail}\n✅ Вспомнил: {ok}\n❌ Забыл: {fail}\n\n"
        f"Прогресс сохранён."
    )
    # носитель клавиатуры (reply-клавиатуру нельзя приложить к edit — только новым сообщением)
    await q.message.reply_text(_next_action_text(uid), reply_markup=MAIN_KB)

# ---------- нажатия кнопок в карточках повторения ----------
async def on_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]          # show | ok | fail
    uid = _learner(update)
    queue = ctx.user_data.get("review_queue", [])
    pos = ctx.user_data.get("review_pos", 0)

    if not queue or pos >= len(queue):
        await q.edit_message_text("Сессия повторения завершена. /start — меню.")
        return

    if action == "show":                   # раскрыть ответ на той же карточке
        shown = ctx.user_data.get("card_shown_at")
        if shown:                          # время recall: от вопроса ДО запроса ответа —
            ctx.user_data["card_ms"] = int((time.time() - shown) * 1000)
        await _show_card(q, ctx, uid, reveal=True)   # чтение ответа/сети в замер не входит
        return

    # action == ok | fail -> записать результат текущего слова
    word_id = queue[pos]
    remembered = (action == "ok")
    ms = ctx.user_data.pop("card_ms", None)
    db.review(word_id, remembered, uid, variant=_variant(uid, word_id), ms=ms)  # ← цикл + A/B
    db.adapt_band(uid)                          # тихо подстраиваем полосу по последним повторениям
    key = "review_ok" if remembered else "review_fail"
    ctx.user_data[key] = ctx.user_data.get(key, 0) + 1

    ctx.user_data["review_pos"] = pos + 1   # перейти к следующей карточке
    if ctx.user_data["review_pos"] >= len(queue):
        await _finish_review(q, ctx, uid)
    else:
        await _show_card(q, ctx, uid, reveal=False)

# ---------- очередь новых слов на подтверждение (pending) ----------
def _pending_card_text(item):
    """Карточка слова из очереди: показать, что предлагает добавить ИИ."""
    p = json.loads(item.get("payload") or "{}")
    lines = [f"🆕 Слово в очереди: {item['word']}"]
    if p.get("ru"):           lines.append(f"перевод: {p['ru']}")
    if p.get("dna_idea"):     lines.append(f"идея: {p['dna_idea']}")
    if p.get("collocations"): lines.append("коллокации: " + ", ".join(p["collocations"]))
    if p.get("example"):      lines.append(f"пример: {p['example']}")
    lines.append("\nДобавить в базу?")
    return "\n".join(lines)

def _pending_kb(pending_id, total=1):
    rows = [[
        InlineKeyboardButton("✅ Добавить", callback_data=f"pend:ok:{pending_id}"),
        InlineKeyboardButton("🗑 Отклонить", callback_data=f"pend:no:{pending_id}"),
    ]]
    if total > 1:
        rows.append([InlineKeyboardButton(f"✅ Подтвердить все ({total})",
                                          callback_data="pend:all")])
    return InlineKeyboardMarkup(rows)

async def pending_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pending — показать первое слово из очереди на подтверждение."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Добавление слов в базу — только у администратора.")
        return
    items = db.list_pending(CONTENT_USER)
    if not items:
        await update.message.reply_text("Очередь новых слов пуста ✅")
        return
    await update.message.reply_text(_pending_card_text(items[0]),
                                    reply_markup=_pending_kb(items[0]["id"], len(items)))

async def on_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")                    # pend:ok:<id> | pend:no:<id> | pend:all
    action = parts[1]
    if OWNER_ID and _learner(update) != OWNER_ID:
        await q.edit_message_text("Добавление слов в базу — только у администратора.")
        return
    uid = CONTENT_USER
    if action == "all":                          # подтвердить всю очередь разом
        n = db.confirm_all_pending(uid)
        await q.edit_message_text(f"✅ Подтверждено разом: {n}. Очередь пуста.")
        return
    pid = parts[2]
    if action == "ok":
        msg = "✅ Добавлено в базу." if db.confirm_pending(int(pid), uid) else "⚠️ Уже обработано."
    else:
        msg = "🗑 Отклонено." if db.reject_pending(int(pid), uid) else "⚠️ Уже обработано."
    items = db.list_pending(uid)                  # показать следующее или конец
    if items:
        await q.edit_message_text(msg + "\n\n" + _pending_card_text(items[0]),
                                  reply_markup=_pending_kb(items[0]["id"], len(items)))
    else:
        await q.edit_message_text(msg + "\n\nОчередь пуста ✅")

# ---------- /fill: батч-наполнение сценария (админ) ----------
async def fill_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/fill <сценарий> [N] — подбор N слов из частотных бизнес-списков ->
    enrich (7 слоёв) -> очередь /pending. Дубли отсеиваются на обоих шагах."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Наполнение базы — только у администратора.")
        return
    args = list(ctx.args or [])
    n = 10
    if args and args[-1].isdigit():
        n = max(1, min(int(args.pop()), 25))
    scenario = " ".join(args).strip()
    if not scenario:
        topics = ", ".join(f"{s} ({k})" for s, k in db.scenario_list(min_n=1))
        await update.message.reply_text(
            "Формат: /fill <сценарий> [сколько слов, до 25]\n"
            "Например: /fill Restaurant 15\n\n"
            f"Сценарии сейчас: {topics}")
        return
    await update.message.reply_text(f"⏳ Подбираю до {n} слов для «{scenario}» по частотным спискам…")
    words = await asyncio.to_thread(enrich.suggest_words, scenario, n)
    if not words:
        await update.message.reply_text(
            "Не вышло подобрать слова (модель не дала список или всё уже в базе). Попробуй ещё раз.")
        return
    await update.message.reply_text(
        "Кандидаты: " + ", ".join(words) + f"\n⏳ Разбираю по 7 слоям ({len(words)} слов)…")
    res = await asyncio.to_thread(enrich.run, words, CONTENT_USER, scenario)
    await update.message.reply_text(
        f"Готово: в очередь {res['added']}, дубли {res['skipped']}, не вышло {res['failed']}.\n"
        f"Подтверждение — /pending (есть «Подтвердить все»).")

# ---------- режим чтения: понятный вход i+1 ----------
async def read_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/read — понятный текст по правилу 98%: ~98% известных слов + ~2% целевых новых."""
    uid = _learner(update)
    targets = db.target_words(uid) or db.learning_words(uid)   # новые ~2%
    if not targets:
        await update.message.reply_text(
            "Сначала добавь слова в режиме «🌅 Новые слова» — потом соберу текст для чтения.")
        return
    base = db.mature_words(uid)                                # известное ~98%
    base_list = ", ".join(w["word"] for w in base) or \
        "(освоенных слов пока мало — держи остальную лексику простой и высокочастотной)"
    seed = ("Целевые НОВЫЕ слова — вплети их (это ~2% текста):\n"
            f"{db.format_for_agent(targets)}\n\n"
            f"ИЗВЕСТНЫЕ ученику слова — опирайся на них (~98% текста):\n{base_list}")
    await _typing(update.message)
    text = await asyncio.to_thread(llm.chat, prompts.assemble("input"), [{"role": "user", "content": seed}])
    await _say(update.message, text)

# ---------- движок структурных ошибок: /mistakes ----------
_CAT_RU = {"word_order": "порядок слов", "article": "артикли", "preposition": "предлоги",
           "tense_aspect": "время/вид", "agreement": "согласование",
           "word_choice": "выбор слова", "other": "прочее"}

async def mistakes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/mistakes — повторяющиеся структурные паттерны ошибок + примеры."""
    uid = _learner(update)
    pats = db.error_patterns(uid)
    if not pats:
        await update.message.reply_text("Пока структурных ошибок не накоплено ✅")
        return
    lines = ["📐 Твои повторяющиеся структурные паттерны:\n"]
    for p in pats:
        lines.append(f"• {_CAT_RU.get(p['category'], p['category'])}: {p['n']}×")
        for e in db.recent_errors(p["category"], uid, limit=1):
            if e["wrong"] and e["correct"]:
                lines.append(f"   ❌ {e['wrong']} → ✅ {e['correct']}")
    await update.message.reply_text("\n".join(lines))

# ---------- измеримость: /progress (CEFR can-do) ----------
async def progress_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/progress — прогресс по деловым can-do (CEFR-стиль). Прокси по словарю, не сертификат."""
    uid = _learner(update)
    rows = db.cando_progress(uid)
    lines = ["📈 Что ты уже можешь (по освоенным словам сценария):\n"]
    for r in rows:
        mark = "✅" if r["ready"] else ("▶" if r["mastered"] else "·")
        lines.append(f"{mark} [{r['level']}] {r['ru']} — {r['mastered']}/{r['total']}")
    lines.append("\n⚠️ Это ориентир по словарю, а не официальная оценка CEFR.")
    s = db.progress_summary(uid)            # сводка пути — вторична к can-do (канон Ч.5)
    path = (f"\n🛤 Путь: освоено {s['mastered']} узлов · в работе {s['learning']} · "
            f"впереди в базе {s['new']}\n"
            f"Ориентир покрытия — ~{s['nation_target']} семей слов (Nation). "
            f"Числа — дорога, мера умения — список выше.")
    if s["sessions"]:
        path += f"\nЗанятий: {s['sessions']}" + (f" · с {s['since']}" if s["since"] else "")
    lines.append(path)
    await update.message.reply_text("\n".join(lines))

# ---------- A/B-инструментовка: /abstats ----------
async def abstats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/abstats — сводка A/B (сеть vs плоско): точность и время ответа. Сигнал, НЕ доказательство."""
    stats = db.variant_stats(_learner(update))
    if not stats:
        await update.message.reply_text("Пока нет данных A/B — поучись через «☀️ Повторение».")
        return
    lines = ["🧪 A/B (сеть vs плоско) — точность СЛЕДУЮЩЕГО повторения\n"
             "после сетевого/плоского показа (эффект кодирования):\n"]
    for s in stats:
        acc = f"{round(s['accuracy']*100)}%" if s["accuracy"] is not None else "—"
        ms = f"{s['avg_ms']} мс" if s["avg_ms"] is not None else "—"
        lines.append(f"• после {s['variant']}: n={s['n']}, точность {acc}, recall {ms}")
    lines.append("\n⚠️ Это направленный сигнал. Для вывода нужна когорта, не n=1.")
    await update.message.reply_text("\n".join(lines))

# ---------- неподдерживаемые типы (голос/фото и т.п.) ----------
async def on_unsupported(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Бот понимает только текст. На голос/фото — вежливая подсказка вместо тишины."""
    if update.message:
        await update.message.reply_text(
            "Я понимаю текст и голосовые 🙂 Пришли сообщение текстом или голосом. "
            "Команды: /start, /pending, /read, /progress, /mistakes, /add")

# ---------- progressive disclosure: «🔍 Глубже» (разбор слова из графа по тапу) ----------
async def on_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        wid = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    branch = db.branch_words(wid, _learner(update))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🌳 Открыть ветку ({len(branch)})", callback_data=f"branch:{wid}")]]) if branch else None
    await q.message.reply_text(db.deep_view(wid) or "Нет данных по слову.", reply_markup=kb)

async def on_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """«Открыть ветку»: выучить однокоренные/семью слова одним заходом (наша сеть как фишка)."""
    q = update.callback_query
    await q.answer()
    try:
        wid = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    uid = _learner(update)
    wd = db.branch_words(wid, uid, n=5)
    if not wd:
        await q.message.reply_text("У этого слова пока нет ветки в базе.")
        return
    head = db.get_word(wid)
    await q.message.reply_text(f"🌳 Ветка слова «{head['word']}» — {len(wd)} родственных:")
    await _deliver_lesson(q.message, ctx, uid, wd)

# ---------- контроль доступа: заявки + роли (env ALLOWED_USERS — аварийный замок поверх) ----------
def _access_decision(uid):
    """allowed | pending_new (первая заявка, создана) | pending | blocked."""
    if (OWNER_ID and uid == OWNER_ID) or uid in ALLOWED_USERS:
        return "allowed"
    role = db.get_role(uid)
    if role in ("owner", "approved"):
        return "allowed"
    if role == "blocked":
        return "blocked"
    if role == "pending":
        return "pending"
    return "pending_new"        # незнакомец — заявка создаётся в _guard (нужно имя)

def _knock_kb(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Впустить", callback_data=f"acc:ok:{uid}"),
        InlineKeyboardButton("🚫 Отклонить", callback_data=f"acc:no:{uid}"),
    ]])

def _user_label(u):
    """Имя (@username, id) для карточки заявки и /users."""
    name = " ".join(filter(None, [u.first_name, u.last_name])) or "Без имени"
    return f"{name} (@{u.username}, id {u.id})" if u.username else f"{name} (id {u.id})"

async def _guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Вахтёр: пускает владельца/одобренных; незнакомцу — заявка, владельцу — уведомление.
    Всё ДО любых вызовов LLM."""
    u = update.effective_user
    if update.callback_query:
        log.info("update: user=%s callback=%s", u.id if u else "?", update.callback_query.data)
    elif update.message:
        kind = "voice" if update.message.voice else "text"
        log.info("update: user=%s %s=%r", u.id if u else "?", kind,
                 (update.message.text or "")[:80])
    if u is None:
        raise ApplicationHandlerStop
    decision = _access_decision(u.id)
    if decision == "allowed":
        ctx.user_data["last_seen"] = time.time()   # пульс активности — для авто-ИТОГа
        return
    if decision == "pending_new" and db.request_access(u.id, _user_label(u)):
        if OWNER_ID:                               # карточка владельцу — один раз на заявку
            try:
                await ctx.bot.send_message(
                    OWNER_ID, f"🔔 Стучится {_user_label(u)} — впустить?",
                    reply_markup=_knock_kb(u.id))
            except Exception:
                log.exception("knock notify failed")
        decision = "pending"
        reply = "Это ранний доступ English OS 🔑 Заявка отправлена — напишу, как только откроют."
    elif decision == "pending":
        reply = "Заявка уже у владельца — ждём решения 🙌"
    else:
        reply = "⛔ Доступ закрыт."
    if update.callback_query:
        await update.callback_query.answer(reply, show_alert=True)
    elif update.message:
        await update.message.reply_text(reply)
    raise ApplicationHandlerStop

# ---------- решения по заявкам и список пользователей (владелец) ----------
async def on_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if OWNER_ID and _learner(update) != OWNER_ID:
        return
    _, action, uid = q.data.split(":")
    uid = int(uid)
    if action == "ok":
        db.set_role(uid, "approved")
        try:
            await ctx.bot.send_message(
                uid, "Доступ открыт — добро пожаловать в English OS! 🎉 Жми /start.")
        except Exception:
            pass
        await q.edit_message_text(f"✅ Впущен: id {uid}. Список: /users")
    else:
        db.set_role(uid, "blocked")
        await q.edit_message_text(f"🚫 Отклонён: id {uid}. Список: /users")

_ROLE_MARK = {"owner": "👑", "approved": "✅", "pending": "🔔", "blocked": "🚫"}

async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/users — кто есть в боте: роль, имя, прогресс; кнопки решений по заявкам."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Список пользователей — только у администратора.")
        return
    rows = db.list_users()
    if not rows:
        await update.message.reply_text("Пока никого нет.")
        return
    lines = ["👥 Пользователи:\n"]
    pending = []
    for r in rows:
        mark = _ROLE_MARK.get(r["role"], "·")
        lines.append(f"{mark} {r['name'] or r['user_id']} — с {r['created_at']}, "
                     f"освоено {r['mastered']}")
        if r["role"] == "pending":
            pending.append(r)
    msg = "\n".join(lines)
    if pending:                                   # решения по первой заявке прямо отсюда
        p = pending[0]
        await update.message.reply_text(
            msg + f"\n\n🔔 Ждёт решения: {p['name'] or p['user_id']}",
            reply_markup=_knock_kb(p["user_id"]))
    else:
        await update.message.reply_text(msg)

# ---------- /add: добавить встреченные слова в очередь на изучение ----------
async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/add word1 word2 — обогатить слова через ИИ и положить в очередь /pending."""
    words = [w.strip() for w in (ctx.args or []) if w.strip()][:8]
    if not words:
        await update.message.reply_text(
            "Напиши слова после команды, например: /add deadline stakeholder — добавлю их в очередь на изучение.")
        return
    await update.message.reply_text("⏳ Разбираю слова и кладу в очередь…")
    res = await asyncio.to_thread(enrich.run, words)     # не блокируем бота на время запросов к ИИ
    await update.message.reply_text(
        f"Готово: добавил {res['added']}, уже было {res['skipped']}, не вышло {res['failed']}.\n"
        f"Появятся в учёбе после подтверждения админом (/pending).")

# ---------- календарь: ссылка-шаблон Google + .ics (без OAuth) ----------
_CAL_TITLE = "English OS — 10 минут английского"
_CAL_DETAILS = "Ежедневная тренировка с ботом English OS: повторение и новые слова."

def _cal_times(hour, day):
    start = f"{day:%Y%m%d}T{hour:02d}0000"
    end = f"{day:%Y%m%d}T{hour:02d}1000"          # событие на 10 минут
    return start, end

def _gcal_link(hour, day):
    """Ссылка-шаблон Google Calendar: открывает форму с готовым ежедневным событием."""
    start, end = _cal_times(hour, day)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode({
        "action": "TEMPLATE",
        "text": _CAL_TITLE,
        "details": _CAL_DETAILS,
        "dates": f"{start}/{end}",
        "recur": "RRULE:FREQ=DAILY",
    })

def _ics_event(hour, day):
    """То же событие в iCalendar (.ics) — для Apple/Outlook/любого календаря. CRLF по стандарту."""
    start, end = _cal_times(hour, day)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//English OS//RU",
        "BEGIN:VEVENT",
        f"UID:english-os-daily-{hour}@english-os",
        f"DTSTAMP:{start}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        "RRULE:FREQ=DAILY",
        f"SUMMARY:{_CAL_TITLE}",
        f"DESCRIPTION:{_CAL_DETAILS}",
        "END:VEVENT", "END:VCALENDAR", ""])

def _cal_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📅 В календарь", callback_data="cal:add")]])

async def on_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """«📅 В календарь»: кнопка-ссылка Google Calendar + .ics-файл для остальных."""
    q = update.callback_query
    await q.answer()
    hour = db.get_reminder(_learner(update))
    if hour is None:
        hour = 10
    day = datetime.date.today()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "Открыть Google Calendar", url=_gcal_link(hour, day))]])
    await q.message.reply_text(
        f"Ежедневное событие «{_CAL_TITLE}» в {hour}:00.\n"
        "Google Calendar — по кнопке. Файл ниже — для любого другого календаря "
        "(Apple, Outlook): просто открой его на телефоне.", reply_markup=kb)
    await q.message.reply_document(
        document=_ics_event(hour, day).encode("utf-8"),
        filename="english_os_daily.ics")

# ---------- ежедневные напоминания (лёгкий asyncio-цикл, без apscheduler) ----------
_SLOT_WORDS = {"утро": "morning", "morning": "morning", "день": "day", "day": "day",
               "вечер": "evening", "evening": "evening"}
_SLOT_LABEL = {"morning": "🌅 утро", "day": "☀️ день", "evening": "🎭 вечер"}

async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/remind 9 — напоминать в 9:00 · /remind утро 8 — час слота (программа дня) ·
    /remind off — выключить · без аргумента — статус."""
    uid = _learner(update)
    args = ctx.args or []
    if not args:
        if db.get_program(uid) == "cycle":
            t = db.get_slot_times(uid)
            ctx.user_data["awaiting_slot_hours"] = True   # экран настроек — ждём три времени
            await update.message.reply_text(
                f"Слоты программы: 🌅 {_fmt_time(t['morning'])} · ☀️ {_fmt_time(t['day'])} "
                f"· 🎭 {_fmt_time(t['evening'])}\n"
                "Напоминаю только о незакрытых слотах.\n"
                "Поменять — напиши три времени через пробел (утро, день, вечер): "
                "9 14 19 или 8:30 13 19:15.",
                reply_markup=_cal_kb())
            return
        h = db.get_reminder(uid)
        status = f"в {h}:00" if h is not None else "выключены"
        await update.message.reply_text(
            f"Напоминания: {status}. Поменять: /remind 9 · выключить: /remind off",
            reply_markup=_cal_kb())
        return
    if args[0].lower() in _SLOT_WORDS:                  # /remind утро 8[:30] — путь продвинутых
        slot = _SLOT_WORDS[args[0].lower()]
        t = _parse_time(args[1]) if len(args) > 1 else None
        if t is None:
            await update.message.reply_text("Формат: /remind утро 8 или /remind утро 8:30")
            return
        db.set_slot_time(uid, slot, t)
        await update.message.reply_text(
            f"Готово — слот {_SLOT_LABEL[slot]} теперь в {_fmt_time(t)}.", reply_markup=_cal_kb())
        return
    a = args[0].lower()
    if db.get_program(uid) == "cycle" and a not in ("off", "выкл", "stop", "0"):
        # одиночное число при программе дня — не пишем мёртвый reminder_hour, ведём к слотам
        ctx.user_data["awaiting_slot_hours"] = True
        await update.message.reply_text(
            "У тебя программа дня — напоминания идут по трём слотам.\n"
            "Напиши три времени через пробел (утро, день, вечер): 9 14 19 или 8:30 13 19:15.")
        return
    if a in ("off", "выкл", "stop", "0"):
        db.set_reminder(uid, None)
        await update.message.reply_text("Напоминания выключены.")
        return
    try:
        h = int(a.split(":")[0])
        if not 0 <= h <= 23:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Формат: /remind 9 (час 0–23) или /remind off")
        return
    db.set_reminder(uid, h)
    await update.message.reply_text(
        f"Готово — напомню в {h}:00, если будут слова к повторению. ☀️",
        reply_markup=_cal_kb())

async def _reminder_tick(app, minute_of_day):
    """Один тик напоминаний. free — на круглом часе (как было); cycle — на точной минуте слота,
    и только если слот ещё не закрыт (_slot_reminder_text)."""
    if minute_of_day % 60 == 0:
        for uid in db.reminder_users(minute_of_day // 60):   # free — как раньше
            due, _ = db.due_today(uid)
            if due:
                try:
                    await app.bot.send_message(
                        uid, f"☀️ {len(due)} слов ждут повторения сегодня. /start → Повторение.")
                except Exception:
                    pass
    for uid, slot in db.slot_users(minute_of_day):           # cycle — по слотам, поминутно
        text = _slot_reminder_text(uid, slot)                # None — слот закрыт, молчим
        if text:
            try:
                await app.bot.send_message(uid, text)
            except Exception:
                pass

async def _reminder_loop(app):
    """Минутный цикл: слоты программы дня задаются с точностью до минуты (8:30)."""
    while True:
        now = datetime.datetime.now()
        await asyncio.sleep(max(1, 60 - now.second))
        now = datetime.datetime.now()
        await _reminder_tick(app, now.hour * 60 + now.minute)

async def _idle_loop(app):
    """Раз в минуту: сессии с диалогом, молчащие IDLE_SUMMARY_SEC, сводим авто-ИТОГом."""
    while True:
        await asyncio.sleep(60)
        for uid in _idle_users(app.user_data):
            ud = app.user_data[uid]

            async def send(text, _uid=uid, **kw):
                await app.bot.send_message(_uid, text, **kw)

            try:
                await app.bot.send_message(uid, "🕐 Тишина 25 минут — подвожу итог сессии.")
                await _finish_session(ud, uid, send)
                app.mark_data_for_update_persistence(user_ids=uid)   # правка вне хэндлера
            except Exception:
                log.exception("auto-summary failed for user=%s", uid)

async def _post_init(app):
    """Меню команд (учебное — всем; админское — только владельцу) + цикл напоминаний."""
    learner = [
        BotCommand("start", "меню и начало"),
        BotCommand("help", "как пользоваться"),
        BotCommand("read", "чтение под мой уровень"),
        BotCommand("progress", "мой прогресс"),
        BotCommand("mistakes", "мои частые ошибки"),
        BotCommand("add", "добавить слова в учёбу"),
        BotCommand("topics", "учить слова по теме"),
        BotCommand("pace", "сменить темп / сложность"),
        BotCommand("program", "программа занятий"),
        BotCommand("remind", "напоминания о повторении"),
    ]
    await app.bot.set_my_commands(learner)                       # по умолчанию — всем
    if OWNER_ID:                                                 # владельцу — плюс админское
        await app.bot.set_my_commands(
            learner + [BotCommand("pending", "очередь новых слов (админ)"),
                       BotCommand("fill", "наполнить сценарий словами (админ)"),
                       BotCommand("users", "пользователи и заявки (админ)")],
            scope=BotCommandScopeChat(chat_id=OWNER_ID))
    global _REMINDER_TASK, _IDLE_TASK
    _REMINDER_TASK = asyncio.create_task(_reminder_loop(app))   # ссылка в глобале -> не соберётся GC
    _IDLE_TASK = asyncio.create_task(_idle_loop(app))           # авто-ИТОГ по неактивности

def main():
    if not TOKEN:
        raise SystemExit("Задай TELEGRAM_TOKEN")
    db.init_db()    # схема + миграции при каждом старте (идемпотентно) — не руками
    db.ensure_roles(OWNER_ID, ALLOWED_USERS)   # env-список -> роли (бесшовный переход)
    app = (Application.builder().token(TOKEN)
           .persistence(_persistence())          # сессии переживают рестарт
           .post_init(_post_init).build())
    app.add_handler(TypeHandler(Update, _guard), group=-1)   # вахтёр: проверяется первым
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("fill", fill_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CallbackQueryHandler(on_access, pattern=r"^acc:"))
    app.add_handler(CommandHandler("mistakes", mistakes_cmd))
    app.add_handler(CommandHandler("read", read_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("abstats", abstats_cmd))
    app.add_handler(CommandHandler("progress", progress_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("pace", pace_cmd))
    app.add_handler(CommandHandler("program", program_cmd))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^rev:"))
    app.add_handler(CallbackQueryHandler(on_pending, pattern=r"^pend:"))
    app.add_handler(CallbackQueryHandler(on_deep, pattern=r"^deep:"))
    app.add_handler(CallbackQueryHandler(on_branch, pattern=r"^branch:"))
    app.add_handler(CallbackQueryHandler(on_pace, pattern=r"^pace:"))
    app.add_handler(CallbackQueryHandler(on_calendar, pattern=r"^cal:"))
    app.add_handler(CallbackQueryHandler(on_program, pattern=r"^prog:"))
    app.add_handler(CallbackQueryHandler(on_topics, pattern=r"^topics$"))
    app.add_handler(CallbackQueryHandler(on_topax, pattern=r"^topax:"))
    app.add_handler(CallbackQueryHandler(on_topic, pattern=r"^topic:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))                          # голос -> распознать
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND & ~filters.VOICE,
                                   on_unsupported))                                   # фото и прочее
    app.run_polling()

if __name__ == "__main__":
    main()
