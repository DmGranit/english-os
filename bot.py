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
import html as _htmlmod
import os, json, random, re, shutil, time, asyncio, datetime, logging, types, urllib.parse, zlib
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove,
                      BotCommand, BotCommandScopeChat)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, ApplicationHandlerStop,
                          ContextTypes, PicklePersistence, filters)
import db, prompts, llm, enrich, selfcheck

log = logging.getLogger("english_os")
_STARTED = time.time()    # для аптайма в /health

# лимиты трат (настраиваются env): защита кошелька при тест-эксплуатации
LLM_HOURLY_CAP = int(os.environ.get("LLM_HOURLY_CAP", "40"))   # LLM-вызовов в час на человека
VOICE_MAX_SEC = int(os.environ.get("VOICE_MAX_SEC", "60"))     # секунд на ОДНО голосовое
TEXT_MAX_LEN = int(os.environ.get("TEXT_MAX_LEN", "2000"))     # символов на одно сообщение
COOLDOWN_MSG = ("☕ Передохнём пару минут — за последний час было много запросов. "
                "Карточки ☀️ Повторить работают без ограничений!")

# ежедневный бэкап: локально + копия на офсайт (Google Drive синхронизирует в облако)
OFFSITE_DIR = os.environ.get("ENGLISH_OS_OFFSITE", r"G:\Мой диск\English_OS\backups")
BACKUP_MINUTE = 3 * 60 + 30          # 03:30 — тихий час
STOCK_ALERT_DAYS = 14                # запас новых слов меньше — карточка владельцу

def _daily_backup():
    """Бэкап базы + копия на офсайт. Возвращает путь копии или None (офсайт недоступен)."""
    src = db.backup()
    try:
        os.makedirs(OFFSITE_DIR, exist_ok=True)
        dst = os.path.join(OFFSITE_DIR, os.path.basename(src))
        shutil.copy(src, dst)
        return dst
    except Exception:
        log.exception("offsite backup failed (локальный бэкап есть: %s)", src)
        return None

def _rate_ok(ud, now=None):
    """Лимитер: не больше LLM_HOURLY_CAP вызовов модели в час на пользователя.
    Скользящее окно в user_data (переживает рестарт через персистентность)."""
    now = time.time() if now is None else now
    calls = [t for t in ud.get("llm_calls", []) if now - t < 3600]
    allowed = len(calls) < LLM_HOURLY_CAP
    if allowed:
        calls.append(now)
    ud["llm_calls"] = calls
    return allowed

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

# модель пишет markdown (**жирный**, `код`, ### заголовки) — Telegram понимает только HTML.
# Конвертируем на бэкенде: экранируем спецсимволы, размечаем в пределах одной строки
# (через перенос не матчим — чанкование по строкам не порвёт теги).
def _to_html(text):
    s = _htmlmod.escape(text or "", quote=False)
    s = re.sub(r"^#{1,4}\s*(.+)$", r"<b>\1</b>", s, flags=re.M)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return s

async def _reply_render(msg, text, markup=None):
    """Ответ с HTML-разметкой; битые сущности — фолбэк в плоский текст (не падаем)."""
    try:
        return await msg.reply_text(_to_html(text), reply_markup=markup, parse_mode="HTML")
    except Exception:
        return await msg.reply_text(text, reply_markup=markup)

async def _swap_in(ph, reply, markup=None):
    """Заменить плейсхолдер «⏳…» готовым ответом. НИКОГДА не оставляет плейсхолдер
    висеть: HTML -> плоский edit -> новое сообщение -> кнопка «не работает». Сбои логируются."""
    parts = _chunks(reply)
    first_markup = markup if len(parts) == 1 else None
    try:                                          # 1) штатно: правим плейсхолдер с HTML
        await ph.edit_text(_to_html(parts[0]), parse_mode="HTML", reply_markup=first_markup)
    except Exception:
        try:                                      # 2) HTML кривой -> плоский edit
            await ph.edit_text(parts[0], reply_markup=first_markup)
        except Exception:
            log.exception("swap_in edit failed — fallback to new message")
            try:                                  # 3) edit не идёт -> новое сообщение
                await ph.reply_text(parts[0], reply_markup=first_markup)
            except Exception:                     # 4) совсем глухо -> кнопка ретрая
                log.exception("swap_in reply failed")
                try:
                    await ph.reply_text(
                        "⚠️ Не получил ответ — что-то пошло не так. Попробуем ещё раз?",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("🔄 Повторить", callback_data="retry:scn")]]))
                except Exception:
                    pass
                return ph
    m = ph
    for k, p in enumerate(parts[1:], start=2):
        m = await _reply_render(m, p, markup if k == len(parts) else None)
    return m

async def _say(msg, text, markup=None):
    """reply_text с разрезанием под лимит и HTML-отрисовкой; клавиатура — на последней части."""
    parts = _chunks(text)
    for p in parts[:-1]:
        await _reply_render(msg, p)
    return await _reply_render(msg, parts[-1], markup)

async def _deliver(out, text, markup=None):
    """Показать длинный текст: первая часть через out (edit или reply), хвост — реплаями.
    out сам решает про HTML (см. замыкания в on_mode/_route_button)."""
    parts = _chunks(text)
    m = await out(parts[0], markup if len(parts) == 1 else None)
    for k, p in enumerate(parts[1:], start=2):
        m = await _reply_render(m, p, markup if k == len(parts) else None)
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
# фразы-завершения: совпадают с ВСЕЙ репликой целиком (с возможным «!»/«.»),
# а не с её началом — иначе «stop the train please» ложно сработает
_END_PHRASES = re.compile(
    r"(итог|стоп( игра)?|закончил[аи]?|закончим|закончить|отч[её]т|хватит|финиш|"
    r"all done|we'?re done|that'?s all|that'?s it|the end|"
    r"let'?s (finish|stop)( for today| up| now)?|"
    r"finish for today|finish up|finish|i'?m done|done for today|done|stop)"
    r"[\s.!]*$", re.I)
_END_PHRASES = re.compile(r"^\s*" + _END_PHRASES.pattern, re.I)

def _is_end_trigger(text):
    """Реплика завершает сессию? Точные слова ИЛИ короткая фраза-завершение ЦЕЛИКОМ
    (не длинное предложение, где finish/stop — обычные слова)."""
    t = (text or "").strip().lower()
    if t in END_WORDS:
        return True
    return bool(_END_PHRASES.match(t)) and len(t.split()) <= 4
IDLE_SUMMARY_SEC = 25 * 60   # тишина, после которой сессия сводится автоматически

def _end_prompt():
    """Команда завершения с РЕАЛЬНОЙ датой (A1.2): без неё модель выдумывала дату в шапке."""
    return (f"<КОНЕЦ СЕССИИ> Сегодня {datetime.date.today().isoformat()} — в шапке ИТОГ "
            "используй ЭТУ дату. Заверши занятие: дай человеческий отчёт ИТОГ, а в самом конце — "
            "машинный JSON-блок (reviewed/add/errors). Не переводи это сообщение и не проси повторить.")

async def _finish_session(ud, uid, send):
    """Свести сессию: ИТОГ от модели -> запись в базу -> отчёт через send(text).
    ud — user_data пользователя (история/режим). История после сведения очищается."""
    if not ud.get("history"):                      # пустая сессия — LLM не дёргаем
        await send("Пока нечего подводить — мы ещё не общались в этой сессии 🙂 "
                   "Напиши пару фраз или нажми ☀️ Повторить.")
        return
    ctx = types.SimpleNamespace(user_data=ud)      # _call работает только с user_data
    reply = await _call(ctx, ud.get("mode", "flow"), uid, _end_prompt(), with_summary=True)
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
    ud.pop("scn_words", None)                      # целевые слова сессии больше не грунтуются
    ud.pop("typed_wid", None)                      # незакрытая typed-карточка снята
    ud.pop("ctx_checked", None)                    # следующая сессия снова ловит цель ученика
    for p in _chunks(reply):                       # отчёт может не влезть в одно сообщение
        try:
            await send(_to_html(p), parse_mode="HTML")
        except Exception:                          # старый контракт send / битая разметка
            await send(p)
    # носитель клавиатуры: подсказка слота (cycle) или нейтральный текст (free)
    await send(_next_action_text(uid), reply_markup=MAIN_KB)

# ---------- программа дня: слоты, карта, подсказки ----------
_SLOTS = [("new", "🌅", "Новые"), ("review", "☀️", "Повторить"), ("scenario", "🎭", "Сценарий")]

# времена слотов: парсинг пользовательского ввода и формат показа (минуты от полуночи)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3])(?::([0-5]\d))?$")

def _fmt_time(minutes):
    if minutes is None:                       # слот выключен явно (/remind off)
        return "выкл"
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
BTN_MYWORDS, BTN_END = "📒 Слова", "🏁 Итог"
MAIN_KB = ReplyKeyboardMarkup(
    # учиться · учиться · смотреть · завершить
    [[BTN_NEW, BTN_REVIEW], [BTN_SCEN, BTN_READ],
     [BTN_TOPICS, BTN_MYWORDS], [BTN_PROG, BTN_END]],
    resize_keyboard=True, one_time_keyboard=True,   # сворачивается в значок ⌨ (is_persistent
    input_field_placeholder="Кнопки — значок ⌨ справа")  # застревал открытым поверх меню команд)
MAIN_BUTTONS = {BTN_NEW, BTN_REVIEW, BTN_SCEN, BTN_READ,
                BTN_TOPICS, BTN_MYWORDS, BTN_PROG}   # BTN_END — в END_WORDS

async def _route_button(update, ctx, uid, label):
    """Кнопка постоянной клавиатуры -> то же действие, что inline/команда."""
    m = update.message
    async def out(text, markup=None):
        return await _reply_render(m, text, markup)
    if label == BTN_PROG:
        await progress_cmd(update, ctx)
    elif label == BTN_READ:
        await read_cmd(update, ctx)
    elif label == BTN_TOPICS:
        await topics_cmd(update, ctx)
    elif label == BTN_MYWORDS:
        await mywords_cmd(update, ctx)
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
    badge = _owner_badge(uid)                 # владельцу — хвост очереди на подтверждение
    if badge:
        lead += "\n\n" + badge
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
        "Пиши «учим <слово>», говори голосом или жми 📖 Читать. Сменить темп — /pace.\n"
        "Буду напоминать о повторении в 10:00 (поменять: /remind 9 · выключить: /remind off).")
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

_BOX_TAG = {1: "🟡", 2: "🟡", 3: "🟢", 4: "🟢", 5: "🌳"}

async def mywords_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """📒 Мои слова — что ученик сейчас учит (из базы, без LLM)."""
    d = db.my_words(_learner(update))
    if not d["learning"] and not d["mastered_n"]:
        await update.message.reply_text(
            "Пока ничего не учишь 🌱 Жми 🌅 Новые — начнём!")
        return
    lines = [f"📒 Сейчас в работе ({len(d['learning'])}):"]
    for w in d["learning"][:25]:
        lines.append(f"{_BOX_TAG.get(w['box'], '🟡')} {w['word']} — {w['ru']}")
    if len(d["learning"]) > 25:
        lines.append(f"…и ещё {len(d['learning']) - 25}")
    lines.append(f"\n🌳 Освоено: {d['mastered_n']} · впереди в базе: {d['new_n']}")
    lines.append("🟡 знакомлюсь · 🟢 закрепляю · 🌳 освоено")
    await _say(update.message, "\n".join(lines))

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
        "• голосовые понимаю 🎤 — говори по-английски;\n"
        "• «учим invest, traction» — разберу эти слова;\n"
        "• замолчишь на 25 минут — сам подведу итог сессии;\n"
        "• кнопки прячутся после нажатия — вернуть их можно значком ⌨ в строке ввода.\n\n"
        "Команды: /topics темы · /mistakes мои ошибки · /pace темп · /program программа дня · "
        "/remind напоминания · /add слова в очередь",
        reply_markup=MAIN_KB)

# ---------- Слой Б: ручной руль сложности (полоса скрыта, ярлык не показываем) ----------
def _difficulty_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Проще", callback_data="diff:down"),
        InlineKeyboardButton("⬆️ Сложнее", callback_data="diff:up"),
    ]])

async def _apply_difficulty(uid, direction):
    band, moved = db.nudge_band(uid, direction)
    if not moved:
        return ("Ты уже на максимуме 💪 Дальше — только новые слова посложнее."
                if direction > 0 else "Это самый мягкий темп 🌱 Не переживай, всё получится.")
    return ("💪 Добавил сложности: слова будут потруднее и раньше спрошу на продукцию."
            if direction > 0 else "🌱 Сделал помягче: слова попроще, без спешки.")

async def on_difficulty(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg = await _apply_difficulty(_learner(update), +1 if q.data == "diff:up" else -1)
    await q.edit_message_text(msg)

async def harder_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _apply_difficulty(_learner(update), +1))

async def easier_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _apply_difficulty(_learner(update), -1))

async def pace_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pace — руль сложности в любой момент (двигает скрытую полосу, ярлык не показываем)."""
    await update.message.reply_text(
        "Как тебе сложность? Можешь подкрутить в любой момент — подстроюсь.",
        reply_markup=_difficulty_kb())

# ---------- переключение режима (inline-кнопка и постоянная клавиатура) ----------
async def on_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data.split(":")[1]
    uid = _learner(update)

    async def out(text, markup=None):                 # inline-путь правит то же сообщение
        if isinstance(markup, ReplyKeyboardRemove):   # edit не умеет reply-маркапы
            return await q.edit_message_text(text)
        try:
            return await q.edit_message_text(_to_html(text), reply_markup=markup,
                                             parse_mode="HTML")
        except Exception:                             # битая разметка — плоским текстом
            return await q.edit_message_text(text, reply_markup=markup)

    await _enter_mode(out, ctx, uid, mode, tmsg=q.message)

async def _enter_mode(out, ctx, uid, mode, tmsg=None):
    """Вход в режим. out(text, markup) — способ показа: edit (inline) или reply (клавиатура)."""
    ctx.user_data["mode"] = mode
    ctx.user_data["history"] = []
    ctx.user_data.pop("awaiting_slot_hours", None)   # ушёл учиться — настройку часов отменяем
    ctx.user_data.pop("ctx_checked", None)           # новая сессия — снова можно поймать цель
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
        due, st = db.due_today(uid, limit=db.DECK_CAP)   # кап колоды: не марафон на 90 карт
        if not due:
            await out("На сегодня повторять нечего ✅")
            return
        total = db.due_count(uid)
        if total > len(due):                             # остальное — мягко, без вины
            ctx.user_data["deck_more"] = total - len(due)
        ctx.user_data["review_queue"] = [w["word_id"] for w in due]  # очередь слов
        ctx.user_data["review_box"]   = {wid: st[wid]["box"] for wid in st}  # box -> направление
        ctx.user_data["review_pos"]   = 0      # на какой карточке стоим
        ctx.user_data["review_ok"]    = 0      # счётчик «вспомнил»
        ctx.user_data["review_fail"]  = 0      # счётчик «забыл»
        ctx.user_data["card_shown_at"] = time.time()   # старт замера времени recall
        text, kb = _card_payload(ctx, uid)     # показать первую карточку
        await out(text, kb)

    elif mode == "scenario":   # пресеты живых тем + свой вариант текстом (канон 3.4)
        await out("🎭 РОЛЕВАЯ ИГРА. Я войду в роль (инвестор, клиент…), а ты отвечаешь "
                  "по-английски — можно голосом 🎤. Ошибки разберу в конце (жми 🏁 Итог).\n\n"
                  "Выбери ситуацию — или напиши свою («питч инвестору»):",
                  _scenario_kb())

    else:  # flow — диалог: клавиатура с экрана убирается целиком,
           # вернётся сама с носителем после ИТОГа (one_time её прятал не до конца)
        await out("🗣️ Поток. Просто общаемся на английском — пиши или говори голосом.",
                  ReplyKeyboardRemove())

# ---------- обычное сообщение (текст и голос идут одним путём) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _process_user_text(update, ctx, _learner(update), update.message.text)

def _deck_card(ctx):
    """Активная карточка колоды: (word, box) или (None, None). Свежесть < 10 мин."""
    ud = ctx.user_data
    queue = ud.get("review_queue") or []
    pos = ud.get("review_pos", 0)
    if pos < len(queue) and time.time() - ud.get("card_shown_at", 0) < 600:
        wid = queue[pos]
        return db.get_word(wid), ud.get("review_box", {}).get(wid, 1)
    return None, None

def _stt_hints(ctx, uid):
    """Подсказки Whisper (A7). Во время колоды язык ответа известен ТОЧНО:
    ожидаемое слово уходит в prompt (на 1-секундных клипах без подсказки Whisper
    галлюцинирует — валлийский кейс). Вне колоды: en, prompt — слова в работе.
    ⚠️ A4.2: на ПРОВЕРОЧНЫХ карточках (typed box 3, сборка box 4, тёплое превью)
    слово-подсказку НЕ передаём — Whisper «дослышивал» ожидаемый ответ, и проверка
    продукции подыгрывала сама себе. language='en' остаётся всегда."""
    ud = ctx.user_data
    if ud.get("typed_wid") or ud.get("asm_wid") or ud.get("warm_wid"):
        return "en", None                       # честная продукция: без подсказки ответа
    word, _ = _deck_card(ctx)
    if word:
        return "en", word["word"]               # самооценочные карточки: подсказка остаётся
    # вне колоды: en ПО УМОЛЧАНИЮ везде — без подсказки короткие клипы галлюцинируют
    # (болгарский/валлийский/грузинский кейсы); русские команды лучше текстом
    focus = ", ".join(w["word"] for w in db.learning_words(uid)) or None
    return "en", focus

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Голосовое: скачать -> распознать речь -> дальше как обычный текст."""
    uid = _learner(update)
    dur = getattr(update.message.voice, "duration", 0) or 0
    if dur > VOICE_MAX_SEC:                      # лимит на ОДНО сообщение (Whisper поминутный)
        await update.message.reply_text(
            f"🎤 Голосовое длинновато ({dur} сек, максимум {VOICE_MAX_SEC}). "
            "Скажи покороче — диалог живее репликами 🙂")
        return
    if not _rate_ok(ctx.user_data):
        await update.message.reply_text(COOLDOWN_MSG)
        return
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
    card, _ = _deck_card(ctx)
    if card and not ctx.user_data.get("act_wid") and len(text.split()) > 5:
        await update.message.reply_text(      # карточка ждёт слово, пришла тирада — галлюцинация
            "🎤 Кажется, не расслышал. Скажи одно слово-ответ ещё раз — или напиши текстом.")
        return
    await update.message.reply_text(f"🗣 You said: {text}")
    await _process_user_text(update, ctx, uid, text)

async def _process_user_text(update, ctx, uid, text):
    confirm = _handle_slot_input(ctx.user_data, uid, text)   # ждали часы слотов?
    if confirm:
        await update.message.reply_text(confirm)
        return

    # завершение сессии: слово-триггер, естественная фраза или кнопка «🏁 Итог»
    if _is_end_trigger(text):
        await _typing(update.message)
        await _finish_session(ctx.user_data, uid, update.message.reply_text)
        return

    if text in MAIN_BUTTONS:                     # постоянная клавиатура
        await _route_button(update, ctx, uid, text)
        return

    if ctx.user_data.get("await_feedback"):      # ждём фидбек (текстом или голосом)
        await _save_feedback(update, ctx, uid, text)
        return

    if ctx.user_data.get("act_wid"):             # ждём фразу для активации нового слова
        await _handle_activation_phrase(update, ctx, uid, text)
        return

    if ctx.user_data.get("warm_wid"):            # тёплое превью продукции (A4.1, вне SRS)
        await _handle_warm_answer(update, ctx, uid, text)
        return

    if ctx.user_data.get("typed_wid"):           # ждём напечатанный ответ карточки box 3
        await _handle_typed_answer(update, ctx, uid, text)
        return

    queue = ctx.user_data.get("review_queue") or []
    pos = ctx.user_data.get("review_pos", 0)
    fresh = time.time() - ctx.user_data.get("card_shown_at", 0) < 600
    if pos < len(queue) and fresh:               # идёт колода: текст = попытка ответа,
        await _text_attempt_on_card(update, ctx, uid, text)   # а не повод для болтовни
        return

    if len(text) > TEXT_MAX_LEN:                 # кап входа: токены модели не резиновые
        await update.message.reply_text(
            f"✂️ Слишком длинное сообщение ({len(text)} символов, максимум {TEXT_MAX_LEN}). "
            "Сократи или разбей на части.")
        return
    if not _rate_ok(ctx.user_data):
        await update.message.reply_text(COOLDOWN_MSG)
        return

    await _typing(update.message)                 # «печатает…», пока ИИ думает
    targets = _parse_learn_intent(text)          # «учим X, Y» -> разбор по графу
    if targets:
        await _teach_words(update, ctx, uid, targets)
        return

    if _mode(ctx) == "review":                   # A2.3: протухшая колода — разговор это flow,
        ctx.user_data["mode"] = "flow"           # а не «назови, что повторяем» без DUE-списка
    reply = await _call(ctx, _mode(ctx), uid, text)
    n = ctx.user_data.get("msg_n", 0) + 1
    ctx.user_data["msg_n"] = n
    # 👍/👎 изредка (каждый 4-й ответ диалога) — сигнал качества без визуального шума
    rate = _rate_kb(str(n)) if _mode(ctx) in ("flow", "scenario") and n % 4 == 0 else None
    await _say(update.message, reply, rate)
    if _mode(ctx) in ("flow", "scenario"):       # из живой речи тихо узнаём, кто наш ученик
        await _maybe_capture_context(ctx, uid, text)

# ---------- роутинг намерения «учим X» ----------
# «new words?» убран из триггеров: «New words are hard for me» — обычная фраза, не намерение (A6)
_LEARN_RE = re.compile(
    r"^\s*(?:давай\s+)?(?:учим|учить|выучим|поучим|разбер[её]м|разбери|хочу выучить|хочу учить|"
    r"teach me|i'?d like to learn|i want to learn|let'?s learn)\b[:\-\s]*(.+)$",
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

# ---------- активация: закрепить новое слово своей фразой сразу (generation effect) ----------
def _activation_kb(word_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎤 Скажи фразу", callback_data=f"act:say:{word_id}"),
        InlineKeyboardButton("✍️ Напиши фразу", callback_data=f"act:write:{word_id}"),
    ], [InlineKeyboardButton("⏭ Потом", callback_data=f"act:skip:{word_id}")]])

async def on_activation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор канала активации: голосом/текстом сказать свою фразу со словом, либо «потом»."""
    q = update.callback_query
    await q.answer()
    _, action, wid = q.data.split(":")
    if action == "skip":
        ctx.user_data.pop("act_wid", None)
        await q.edit_message_text("Ок! Закрепим на повторении. 👍")
        return
    word = db.get_word(int(wid))
    if not word:
        return
    ctx.user_data["act_wid"] = int(wid)
    how = "Скажи голосом" if action == "say" else "Напиши"
    goal = db.get_goal(_learner(update))
    about = " про свой проект/работу" if goal else ""
    await q.edit_message_text(
        f"⚡ {how} короткую фразу со словом «{word['word']}» ({word['ru']}){about} — "
        f"проверю и подскажу, как звучит у носителя.")

async def _handle_activation_phrase(update, ctx, uid, text):
    """Фраза ученика с целевым словом: мгновенный фидбек + смысловая награда (generation effect)."""
    wid = ctx.user_data.get("act_wid")
    word = db.get_word(wid)
    if word and not re.search(re.escape(word["word"]), text, re.I):  # слова нет — не тратим LLM
        await update.message.reply_text(
            f"Попробуй ещё раз — вставь слово «{word['word']}» в свою фразу 🙂")
        return
    ctx.user_data.pop("act_wid", None)
    system = ("Ты — тёплый тренер английского. Ученик составил свою фразу с целевым словом. "
              "Кратко: верно ли и естественно ли. Если есть ошибка — «❌→✅» с причиной. "
              "Дай строку «💬 Natural: <как сказал бы носитель>». Без воды, по-доброму.")
    seed = f"Целевое слово: {word['word']} ({word['ru']}). Фраза ученика: {text}"
    reply = await asyncio.to_thread(llm.chat, system, [{"role": "user", "content": seed}])
    db.log_tech(uid, "activation", f"{word['word']}: {text[:120]}")
    await _say(update.message,
               reply + "\n\n🌱 Ты использовал слово в живой фразе — это закрепляет сильнее "
                       "карточек. Так растут нейронные связи!")

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
    if wd:                                       # активация: закрепить первое слово фразой сразу
        await msg.reply_text("⚡ Закрепим прямо сейчас?", reply_markup=_activation_kb(wd[0]["word_id"]))

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

# ---------- сценарий-сессия: пресеты + мини-цикл «слова → образец → роль» (канон 3.4) ----------
def _scenario_kb():
    """Пресеты ролевых ситуаций — живые темы базы (есть чем грунтовать роль)."""
    pairs = db.scenario_list(min_n=15)[:8]
    btns = [InlineKeyboardButton(name, callback_data=f"scn:{name}") for name, _ in pairs]
    return InlineKeyboardMarkup([btns[i:i + 2] for i in range(0, len(btns), 2)])

async def _begin_scenario(msg, ctx, uid, scenario):
    """Старт сценарий-сессии: слова сценария под полосу (в SRS) → i+1-образец →
    бот в роли, грунтованный целевыми словами. Полоса скрыта, ИТОГ закрывает как обычно."""
    ph = await msg.reply_text(
        "⏳ Готовлю сценарий…\n(чтобы закончить и разобрать ошибки — напиши «Итог»)",
        reply_markup=ReplyKeyboardRemove())
    db.ensure_user_state(uid)
    wd = db.theme_words("scn", scenario, uid, n=4, band=db.get_band(uid))
    ids = [w["word_id"] for w in wd]
    db.start_learning(ids, uid, via="scenario")   # A1.3: не закрывает слот NEW в карте дня
    ctx.user_data.pop("awaiting_slot_hours", None)
    ctx.user_data.update(mode="scenario", history=[], scn_words=ids)
    seed = (f"СЦЕНАРИЙ-СЕССИЯ: «{scenario}». Один ответ, три шага:\n"
            "1) ЦЕЛЕВЫЕ СЛОВА — по строке на каждое: слово — перевод — короткая коллокация.\n"
            "2) ОБРАЗЕЦ — мини-диалог 4–6 реплик по сценарию: ~98% простой лексики + целевые "
            "слова, порядок слов SVOMPT.\n"
            "3) РОЛЬ — войди в собеседника этого сценария и начни сцену первой репликой. "
            "Дальше держи роль по правилам режима (ошибки копи молча до ИТОГа).\n"
            + db.format_for_agent(wd))
    system = prompts.assemble("scenario") + "\n\n" + db.learner_profile(uid)
    hist = ctx.user_data["history"]
    _remember(hist, "user", seed)
    reply = await asyncio.to_thread(llm.chat, system, hist, model=SMART_MODEL)
    _remember(hist, "assistant", reply)
    await _swap_in(ph, reply)

async def on_scenario(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scenario = q.data.split(":", 1)[1]
    ctx.user_data["last_scenario"] = scenario     # для кнопки «🔄 Повторить»
    await _begin_scenario(q.message, ctx, _learner(update), scenario)

async def on_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка «🔄 Повторить» после сбоя: перезапустить последний сценарий."""
    q = update.callback_query
    await q.answer()
    scn = ctx.user_data.get("last_scenario")
    if scn:
        await _begin_scenario(q.message, ctx, _learner(update), scn)
    else:
        await q.message.reply_text("Выбери сценарий заново: 🎭 Сценарий")

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
_CTX_SYS = (
    "Из реплики ученика извлеки рабочий контекст (профессия/сфера/цель изучения английского), "
    "если он там есть. Верни СТРОГО JSON: "
    '{"capture": true, "summary": "<кратко: роль + сфера + цель>"} либо {"capture": false}. '
    "capture=true только если в реплике РЕАЛЬНО сказано о работе/цели, не домысливай.")

async def _maybe_capture_context(ctx, uid, text):
    """Тихо вытащить контекст ученика из свободной речи (раз за сессию, если цель не задана)
    -> users.goal -> подмешивается в каждый промпт. Анкеты нет — учим и узнаём заодно."""
    if ctx.user_data.get("ctx_checked") or db.get_goal(uid):
        return
    ctx.user_data["ctx_checked"] = True
    try:
        raw = await asyncio.to_thread(llm.chat, _CTX_SYS, [{"role": "user", "content": text}],
                                      max_tokens=120)
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        if data.get("capture") and data.get("summary"):
            db.set_goal(uid, data["summary"].strip()[:300])
    except Exception:
        pass

async def _call(ctx, mode, uid, user_text, with_summary=False):
    system = prompts.assemble(mode, with_summary=with_summary) + "\n\n" + db.learner_profile(uid)
    matched = db.match_words(user_text)        # грунтовка: слова из базы в реплике
    if matched:
        system += ("\n\nСЛОВА ИЗ БАЗЫ в реплике (для «💬 Natural» бери ИХ коллокации/сеть, не выдумывай):\n"
                   + db.format_for_agent(matched))
    scn_ids = ctx.user_data.get("scn_words")   # сценарий-сессия: целевые слова в каждой реплике
    if scn_ids:
        scn_wd = [w for w in (db.get_word(i) for i in scn_ids) if w]
        system += ("\n\nЦЕЛЕВЫЕ СЛОВА СЦЕНАРИЯ (вплетай в свои реплики и строй сцену так, "
                   "чтобы ученик ими пользовался):\n" + db.format_for_agent(scn_wd))
    if mode == "scenario":                     # A2.1: роль живёт в system, а не в первом
        scn_name = ctx.user_data.get("last_scenario")   # сообщении — обрезка истории её не съест
        if scn_name:
            system += (f"\n\nТЫ В РОЛИ: собеседник сценария «{scn_name}». Держи роль как живой "
                       "человек до самого ИТОГа; ошибки ученика копи молча — разбор только в ИТОГе.")
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

# ---------- typed recall (box 3): ответ печатается, опечатка в одну правку прощается ----------
def _one_edit_away(a, b):
    """Равны или отличаются одной правкой (вставка/удаление/замена/перестановка
    соседних букв — Дамерау) — допуск на опечатку."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):                       # перестановка соседних: invets ↔ invest
        diffs = [k for k in range(len(a)) if a[k] != b[k]]
        if (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]):
            return True
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return True

async def _text_attempt_on_card(update, ctx, uid, text):
    """Кнопочная карточка + напечатанный ответ (естественный инстинкт): показываем
    правильный ответ, отмечаем попытку, даём кнопки самооценки. Колода не бросается."""
    queue, pos = ctx.user_data["review_queue"], ctx.user_data["review_pos"]
    wid = queue[pos]
    word = db.get_word(wid)
    box = ctx.user_data.get("review_box", {}).get(wid, 1)
    productive = box >= db.PRODUCTIVE_FROM_BOX
    ctx.user_data["review_reveal"] = True
    body = _review_card_text(word, pos + 1, len(queue), True, productive,
                             _variant(uid, wid), box=box)
    await update.message.reply_text(f"✍️ Ты написал: «{text.strip()}»\n\n{body}",
                                    reply_markup=_review_kb(True))

async def _handle_typed_answer(update, ctx, uid, text):
    """Проверить напечатанный ответ карточки box 3: объективный зачёт вместо самооценки."""
    wid = ctx.user_data.pop("typed_wid")
    word = db.get_word(wid)
    given = (text or "").strip()
    exact = given.lower() == (word["word"] or "").lower()
    near = not exact and _one_edit_away(given, word["word"])
    ok = exact or near
    shown = ctx.user_data.get("card_shown_at")
    ms = int((time.time() - shown) * 1000) if shown else None
    await _record_review(ctx, uid, wid, ok, ms, card_type="typed")
    if exact:
        head = f"✅ Точно! {word['word']} — {word['ru']}"
    elif near:
        head = f"✅ Почти — засчитано! Правильно: {word['word']} (у тебя: {given})"
    else:
        head = f"❌ Это было «{word['word']}» — {word['ru']}"
    ex = f"\n{word['example']}" if word.get("example") else ""
    block = ("\n" + _network_block(word)) if _variant(uid, wid) == "layered" else ""
    await update.message.reply_text(head + ex + block)
    shim = types.SimpleNamespace(edit_message_text=update.message.reply_text,  # текстовый путь:
                                 message=update.message)                       # «edit» = новое сообщение
    await _next_card(shim, ctx, uid)

# ---------- сборка SVOMPT (box 4): предложение из перемешанных слов ----------
ASM_MIN, ASM_MAX = 4, 10    # пригодная длина примера в словах

def _asm_tokens(example):
    """Кусочки для конструктора. None — пример отсутствует или неудобной длины.
    Регистр нормализуем: заглавная первого слова и точка в конце «палят» порядок —
    половина задачи решалась бы без знания SVOMPT."""
    toks = (example or "").rstrip(".!?").split()
    if not (ASM_MIN <= len(toks) <= ASM_MAX):
        return None
    if toks and toks[0][:1].isupper() and toks[0].lower() != "i":
        toks[0] = toks[0][0].lower() + toks[0][1:]   # снять заглавную-подсказку начала
    return toks

def _asm_kb(order, used):
    btns = [InlineKeyboardButton(t, callback_data=f"asm:{i}")
            for i, t in enumerate(order) if i not in used]
    return InlineKeyboardMarkup([btns[i:i + 3] for i in range(0, len(btns), 3)])

def _asm_question(word, pos, total, picked):
    head = f"🔁 Повторение · карточка {pos}/{total}"
    built = f"\n👉 {' '.join(picked)}" if picked else ""
    return (f"{head}\n\n🧩 Собери предложение со словом «{word['word']}» ({word['ru']}) — "
            f"тапай слова по порядку. Помни шаблон SVOMPT!{built}")

def _cloze_for(word):
    """Коллокация с пропуском целевого слова — для cloze-карточки box 2.
    Слово ищется ЦЕЛИКОМ (\\b): «investor» не матчит «investors», «please» не матчит
    «pleased» — иначе ученик видит огрызок «attract ___s». None — нет чистого вхождения."""
    pat = re.compile(r"\b" + re.escape(word["word"]) + r"\b", re.I)
    for c in word.get("collocations") or []:
        if pat.search(c):
            return pat.sub("___", c)
    return None

def _review_card_text(word, pos, total, reveal, productive, variant="layered", box=1):
    """Карточка. Лестница трудности: box 1 — узнавание (EN→RU), box 2 — cloze
    (чанк с пропуском: тренируем коллокацию как единицу), box 3+ — продукция (RU→EN).
    Вариант: layered показывает «сеть» при раскрытии ответа, flat — только ответ (A/B)."""
    head = f"🔁 Повторение · карточка {pos}/{total}"
    ipa = f"  🔊 {word['ipa_uk']}" if word.get("ipa_uk") else ""
    ex  = f"\nПример: {word['example']}" if word.get("example") else ""
    cloze = _cloze_for(word) if (not productive and box == 2) else None
    hint = "Нажми «Показать ответ» — или просто напиши свой вариант сообщением."
    if productive:                                   # RU -> EN
        if not reveal:
            return (f"{head}\n\nКак сказать по-английски:\n«{word['ru']}»  ({word['dna_idea']})\n\n"
                    f"{hint}")
        answer = f"«{word['ru']}»\n→ {word['word']}{ipa}{ex}"
    elif cloze:                                      # cloze: вставь слово в чанк
        if not reveal:
            return (f"{head}\n\nВставь слово в коллокацию:\n«{cloze}»  "
                    f"(подсказка: {word['ru']})\n\n{hint}")
        answer = (f"«{cloze.replace('___', word['word'])}»\n"
                  f"→ {word['word']}{ipa} — {word['ru']}{ex}")
    else:                                            # EN -> RU (узнавание)
        if not reveal:
            return (f"{head}\n\nЧто это значит:\n«{word['word']}»{ipa}\n\n{hint}")
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
    ctx.user_data.pop("typed_wid", None)        # карточка сменилась — ожидание ввода снято
    if not reveal and box == 1:                 # box 1: выбор из 4 (объективно, без печати русского)
        opts = db.mcq_options(wid, k=4)
        if len(opts) >= 2:
            order = random.sample(opts, len(opts))
            ctx.user_data["mcq_answer"] = wid
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(o["ru"], callback_data=f"mcq:{o['word_id']}")]
                 for o in order])
            return (f"🔁 Повторение · карточка {pos + 1}/{len(queue)}\n\n"
                    f"Что значит «{word['word']}»"
                    + (f"  🔊 {word['ipa_uk']}" if word.get("ipa_uk") else "") + "?", kb)
    if not reveal and box == 3:                 # box 3: продукция вводом текста (объективно)
        ctx.user_data["typed_wid"] = wid
        return ((f"🔁 Повторение · карточка {pos + 1}/{len(queue)}\n\n"
                 f"✍️ Как сказать по-английски:\n«{word['ru']}»  ({word['dna_idea']})\n\n"
                 f"Напиши ответ сообщением."),
                InlineKeyboardMarkup([[InlineKeyboardButton("🤷 Не помню",
                                                            callback_data="rev:fail")]]))
    if not reveal and box == 4:                 # box 4: сборка SVOMPT (объективная проверка)
        toks = _asm_tokens(word.get("example"))
        if toks:
            order = random.sample(toks, len(toks))
            ctx.user_data.update(asm_target=toks, asm_order=order, asm_used=[],
                                 asm_picked=[], asm_errors=0, asm_wid=wid)
            return (_asm_question(word, pos + 1, len(queue), []), _asm_kb(order, []))
    return (_review_card_text(word, pos + 1, len(queue), reveal, productive,
                              _variant(uid, wid), box=box),
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
    more = ctx.user_data.pop("deck_more", 0)
    tail = (f"\n\n📚 Ещё {more} слов(а) ждут — но это на потом, без спешки. "
            f"Лучше короткими подходами!") if more else ""
    await q.edit_message_text(
        f"🔁 Повторение завершено!\n\n"
        f"Карточек: {ok + fail}\n✅ Вспомнил: {ok}\n❌ Забыл: {fail}\n\n"
        f"Прогресс сохранён.{tail}"
    )
    fresh = db.fresh_today(uid, limit=1)       # A4.1: продукция видна в день 1, не через неделю
    if fresh:
        await q.message.reply_text(
            "⚡ Бонус: одно из сегодняшних слов — попробуешь сказать сам? "
            "Это превью продукции, в статистику не идёт.",
            reply_markup=_warm_kb(fresh[0]["word_id"]))
    # носитель клавиатуры (reply-клавиатуру нельзя приложить к edit — только новым сообщением)
    await q.message.reply_text(_next_action_text(uid), reply_markup=MAIN_KB)

# ---------- тёплое превью продукции (A4.1): typed-карточка ВНЕ SRS-учёта ----------
def _warm_kb(wid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Попробую", callback_data=f"warm:go:{wid}"),
        InlineKeyboardButton("Потом", callback_data="warm:skip"),
    ]])

async def on_warm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Превью продукции в день 1: ядро продукта (скажи сам) показывается сразу,
    но НЕ пишется в reviews — SRS-математика не искажается."""
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "skip":
        ctx.user_data.pop("warm_wid", None)
        await q.edit_message_text("Ок! Продукция начнёт приходить сама — по мере созревания слов 🌱")
        return
    word = db.get_word(int(parts[2]))
    if not word:
        return
    ctx.user_data["warm_wid"] = word["word_id"]
    await q.edit_message_text(
        f"⚡ Как сказать по-английски:\n«{word['ru']}»?\n\n"
        "Напиши или скажи голосом 🎤 (это превью — в статистику не идёт).")

async def _handle_warm_answer(update, ctx, uid, text):
    """Ответ на тёплое превью: фидбек без записи в SRS — витрина продукции, не проверка."""
    wid = ctx.user_data.pop("warm_wid")
    word = db.get_word(wid)
    given = (text or "").strip()
    ok = (given.lower() == (word["word"] or "").lower()) or _one_edit_away(given, word["word"])
    if ok:
        msg = (f"🔥 Да! {word['word']} — {word['ru']}.\n"
               "Это и есть продукция — сказать самому. Дальше такие карточки будут "
               "приходить сами, по мере созревания слов.")
    else:
        msg = (f"Это было «{word['word']}» — {word['ru']}. Ничего страшного: слово свежее, "
               "повторения сделают своё 🌱 (превью в статистику не идёт)")
    await update.message.reply_text(msg)

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
    box = ctx.user_data.get("review_box", {}).get(word_id, 1)
    ctype = "cloze" if (box == 2 and not productive_box(box)) else "self"
    await _record_review(ctx, uid, word_id, remembered, ms, card_type=ctype)
    await _next_card(q, ctx, uid)

def productive_box(box):
    return box >= db.PRODUCTIVE_FROM_BOX

async def _record_review(ctx, uid, word_id, remembered, ms, card_type="self"):
    """Записать результат карточки (SRS + A/B + полоса + счётчики сессии)."""
    ctx.user_data.pop("typed_wid", None)        # «Не помню» и любой исход снимают ожидание
    db.review(word_id, remembered, uid, variant=_variant(uid, word_id), ms=ms,
              card_type=card_type)
    db.adapt_band(uid)                          # тихо подстраиваем полосу
    key = "review_ok" if remembered else "review_fail"
    ctx.user_data[key] = ctx.user_data.get(key, 0) + 1

async def _next_card(q, ctx, uid):
    """Сдвинуть колоду: следующая карточка или финал."""
    ctx.user_data["review_pos"] = ctx.user_data.get("review_pos", 0) + 1
    if ctx.user_data["review_pos"] >= len(ctx.user_data.get("review_queue", [])):
        await _finish_review(q, ctx, uid)
    else:
        await _show_card(q, ctx, uid, reveal=False)

async def on_mcq(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Карточка-выбор box 1: тап по варианту — объективный зачёт, без самооценки."""
    q = update.callback_query
    await q.answer()
    uid = _learner(update)
    target = ctx.user_data.pop("mcq_answer", None)
    if target is None:
        await q.answer("Карточка устарела — нажми ☀️ Повторить", show_alert=True)
        return
    picked = int(q.data.split(":")[1])
    word = db.get_word(target)
    ok = picked == target
    shown = ctx.user_data.get("card_shown_at")
    ms = int((time.time() - shown) * 1000) if shown else None
    await _record_review(ctx, uid, target, ok, ms, card_type="mcq")
    head = (f"✅ Верно! {word['word']} — {word['ru']}" if ok
            else f"❌ «{word['word']}» — {word['ru']}, а не «{db.get_word(picked)['ru']}»")
    ex = f"\n{word['example']}" if word.get("example") else ""
    block = ("\n" + _network_block(word)) if _variant(uid, target) == "layered" else ""
    await q.edit_message_text(head + ex + block)
    shim = types.SimpleNamespace(edit_message_text=q.message.reply_text, message=q.message)
    await _next_card(shim, ctx, uid)

async def on_assembly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Конструктор SVOMPT (box 4): тапы по словам, результат — объективный (по ошибкам)."""
    q = update.callback_query
    uid = _learner(update)
    ud = ctx.user_data
    arg = q.data.split(":", 1)[1]
    if arg == "next":                          # с экрана результата — дальше по колоде
        await q.answer()
        await _next_card(q, ctx, uid)
        return
    order, target = ud.get("asm_order"), ud.get("asm_target")
    if not order or not target:
        await q.answer("Карточка устарела — нажми ☀️ Повторить", show_alert=True)
        return
    i = int(arg)
    if i in ud.get("asm_used", []) or i >= len(order):
        await q.answer()
        return
    if order[i] != target[len(ud["asm_picked"])]:
        ud["asm_errors"] = ud.get("asm_errors", 0) + 1
        await q.answer("Не это слово 🙂 Вспомни порядок SVOMPT")
        return
    await q.answer()
    ud["asm_used"].append(i)
    ud["asm_picked"].append(order[i])
    word = db.get_word(ud["asm_wid"])
    pos, total = ud.get("review_pos", 0), len(ud.get("review_queue", []))
    if len(ud["asm_picked"]) < len(target):    # собираем дальше
        await q.edit_message_text(_asm_question(word, pos + 1, total, ud["asm_picked"]),
                                  reply_markup=_asm_kb(order, ud["asm_used"]))
        return
    # собрано: объективный зачёт (1 мисклик прощается — A3.2), экран результата, «Дальше»
    errs = ud.get("asm_errors", 0)
    ok = errs <= 1
    shown = ud.get("card_shown_at")
    ms = int((time.time() - shown) * 1000) if shown else None
    await _record_review(ctx, uid, ud["asm_wid"], ok, ms, card_type="assembly")
    verdict = ("✅ Собрано без ошибок!" if errs == 0
               else "✅ Собрано! (один промах — прощён)" if ok
               else f"⚠️ Собрано, но ошибок: {errs}")
    block = ("\n" + _network_block(word)) if _variant(uid, ud["asm_wid"]) == "layered" else ""
    sentence = " ".join(target)
    sentence = sentence[:1].upper() + sentence[1:]   # вернуть заглавную в показе (в задаче снята)
    for k in ("asm_target", "asm_order", "asm_used", "asm_picked", "asm_errors", "asm_wid"):
        ud.pop(k, None)
    await q.edit_message_text(
        f"🧩 {sentence}\n{verdict}{block}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶ Дальше", callback_data="asm:next")]]))

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
# A5.1: правило 98% обеспечивается ПРОВЕРКОЙ, а не просьбой. Известные = словарь ученика
# (зрелые+целевые+их семьи) + простое высокочастотное ядро. Порог и ядро — ниже.
READ_UNKNOWN_MAX = 0.08          # доля незнакомых токенов, после которой текст переделывается

_SIMPLE_WORDS = frozenset("""
a an the and or but so because if when while as than then else not no nor of in on at to for
from with without by about into over under between through during before after up down out off
above near behind across around i you he she it we they me him her us them my your his its our
their mine yours this that these those who what which where why how whose someone anyone
everyone something anything everything nothing somewhere be am is are was were been being have
has had having do does did done doing will would can could may might must shall should need
dare let go goes went gone going get gets got getting make makes made making take takes took
taken taking come comes came coming see sees saw seen seeing know knows knew known knowing
want wants wanted think thinks thought say says said saying tell tells told talk talks talked
speak speaks spoke work works worked working find finds found give gives gave given use uses
used try tries tried call calls called ask asks asked feel feels felt leave leaves left put
puts keep keeps kept start starts started stop stops stopped play played run runs ran live
lives lived mean means meant read reads write writes wrote written meet meets met help helps
helped look looks looked like likes liked love loves loved show shows showed open opens opened
close closes closed buy buys bought pay pays paid eat eats ate drink drinks drank sleep slept
walk walks walked learn learns learned teach teaches taught study studies studied send sends
sent wait waits waited watch watches watched listen listens listened turn turns turned change
changes changed happen happens happened seem seems seemed become becomes became bring brings
brought hold holds held stand stands stood sit sits sat grow grows grew win wins won lose loses
lost plan plans planned hope hopes hoped remember remembers remembered understand understood
thank thanks thanked one two three four five six seven eight nine ten first second third next
last time times day days week weeks month months year years today tomorrow yesterday morning
evening night hour hours minute minutes now then here there soon often always never sometimes
usually very really just also too only again still already yet maybe please well together
people person man woman friend family home house work job office team meeting email phone
money business boss city country word words name question answer problem idea plan project
task report food water coffee tea lunch dinner thing things way ways life world good bad big
small new old long short high low right wrong easy hard early late happy sad busy free nice
great important real full empty best better worse hot cold young same different more most less
least much many few little some any all every each both other another lot bit
""".split())

def _known_token(t, known):
    """Токен знаком? Прямое совпадение + дешёвая морфология (deadlines/invested/investing)."""
    if t in known or t in _SIMPLE_WORDS:
        return True
    for suf in ("'s", "s", "es", "ed", "ing", "ly", "er", "est"):
        if t.endswith(suf) and len(t) > len(suf) + 2 and t[:-len(suf)] in known:
            return True
    if t.endswith("ing") and t[:-3] + "e" in known:    # making -> make
        return True
    return False

def _unknown_share(text, known):
    """(доля, [незнакомые]) по токенам текста. Строка «🔑 words: …» не считается —
    там пояснения целевых слов, им можно быть новыми."""
    body = "\n".join(l for l in (text or "").split("\n") if not l.strip().startswith("🔑"))
    toks = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z'\-]+", body) if len(t) > 1]
    if not toks:
        return 0.0, []
    unk = sorted({t for t in toks if not _known_token(t, known)})
    return sum(1 for t in toks if not _known_token(t, known)) / len(toks), unk

async def read_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/read — понятный текст по правилу 98%: ~98% известных слов + ~2% целевых новых.
    A5.1: выход модели ПРОВЕРЯЕТСЯ (доля незнакомых токенов), при провале — один
    регенерат с конкретным списком нарушителей. A5.2: чтение оставляет след в sessions."""
    uid = _learner(update)
    targets = db.target_words(uid) or db.learning_words(uid)   # новые ~2%
    if not targets:
        await update.message.reply_text(
            "Сначала добавь слова в режиме «🌅 Новые слова» — потом соберу текст для чтения.")
        return
    base = db.mature_words(uid)                                # известное ~98% (до 60 слов)
    base_list = ", ".join(w["word"] for w in base) or \
        "(освоенных слов пока мало — держи остальную лексику простой и высокочастотной)"
    known = {w["word"].lower() for w in base + targets}
    for w in base + targets:                                   # семьи тоже знакомы ученику
        known.update(str(m).lower() for m in (w.get("family") or []))
    seed = ("Целевые НОВЫЕ слова — вплети их (это ~2% текста):\n"
            f"{db.format_for_agent(targets)}\n\n"
            f"ИЗВЕСТНЫЕ ученику слова — опирайся на них (~98% текста):\n{base_list}")
    await _typing(update.message)
    text = await asyncio.to_thread(llm.chat, prompts.assemble("input"),
                                   [{"role": "user", "content": seed}])
    ratio, unk = _unknown_share(text, known)
    if ratio > READ_UNKNOWN_MAX:                               # правило 98% нарушено — переделка
        retry = (seed + "\n\nПЕРЕДЕЛКА: в прошлой версии слишком много слов вне словаря "
                 "ученика: " + ", ".join(unk[:12]) + ". Перепиши текст, заменив их на слова "
                 "из списка известных или на простейшую лексику (A1). Целевые слова сохрани.")
        text = await asyncio.to_thread(llm.chat, prompts.assemble("input"),
                                       [{"role": "user", "content": retry}])
    db.log_session(uid, "input")                               # A5.2: режим больше не невидим
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
    acc = f" · точность {s['accuracy']}%" if s["accuracy"] is not None else ""
    inputs = f" · 📖 текстов {s['inputs']}" if s.get("inputs") else ""   # A5.2: чтение видно
    path = (f"\n💪 Усилие: {s['reviews']} повторений{acc} · {s['sessions']} занятий{inputs}"
            + (f" · с {s['since']}" if s["since"] else "") + "\n"
            f"🌱 Зреют: 🟡 знакомо {s['familiar']} → 🟢 освоено {s['mastered']} "
            f"(освоенным слово становится после нескольких повторений в разные дни) → "
            f"впереди {s['new']}\n"
            f"Ориентир — ~{s['nation_target']} семей слов (Nation). Числа — дорога, "
            f"мера умения — список выше.")
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

# ---------- надёжность: фидбек, селфчек, журнал ошибок ----------
async def _save_feedback(update, ctx, uid, text):
    """Записать фидбек + карточка владельцу (общий путь для текста и голоса)."""
    db.log_tech(uid, "feedback", text)
    if OWNER_ID and uid != OWNER_ID:
        try:
            await ctx.bot.send_message(OWNER_ID, f"🐞 Фидбек от {db.get_name(uid) or uid}: {text}")
        except Exception:
            log.exception("feedback notify failed")
    ctx.user_data.pop("await_feedback", None)
    await update.message.reply_text("Спасибо! Записал и передал 🙏")

async def feedback_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/feedback <текст> — проблема/идея в журнал + владельцу. Без текста — приём голосом."""
    text = " ".join(ctx.args or []).strip()
    if not text:                                  # ждём следующее сообщение (текст ИЛИ голос)
        ctx.user_data["await_feedback"] = True
        await update.message.reply_text(
            "Что не так или чего не хватает? Напиши или наговори голосом — передам разработчику. "
            "(или ещё раз: /feedback карточки мелковаты)")
        return
    await _save_feedback(update, ctx, _learner(update), text)

def _rate_kb(key):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍", callback_data=f"rate:up:{key}"),
        InlineKeyboardButton("👎", callback_data=f"rate:down:{key}"),
    ]])

async def on_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Оценка ответа бота 👍/👎 — лёгкий сигнал качества в журнал (датасет «что работает»)."""
    q = update.callback_query
    _, vote, key = q.data.split(":", 2)
    db.log_tech(_learner(update), "rating", f"{vote}:{key}")
    await q.answer("Спасибо! 🙏")
    try:
        await q.edit_message_reply_markup(reply_markup=None)   # убрать кнопки после оценки
    except Exception:
        pass

async def health_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/health — селфчек системы (владелец)."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Селфчек — только у администратора.")
        return
    lines = ["🩺 Селфчек:"]
    for name, ok_, detail in selfcheck.checks():
        lines.append(f"{'✅' if ok_ else '❌'} {name}: {detail}")
    try:
        me = await ctx.bot.get_me()
        lines.append(f"✅ telegram: @{me.username}")
    except Exception as e:
        lines.append(f"❌ telegram: {e}")
    up = int(time.time() - _STARTED)
    lines.append(f"⏱ аптайм: {up // 3600}ч {(up % 3600) // 60}м")
    await update.message.reply_text("\n".join(lines))

async def issues_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/issues — последние ошибки и фидбек (владелец): готовый список для правок."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Журнал — только у администратора.")
        return
    rows = db.recent_tech(limit=10)
    if not rows:
        await update.message.reply_text("Журнал пуст ✅ — ни ошибок, ни жалоб.")
        return
    lines = ["🛠 Последние ошибки и фидбек:\n"]
    for r in rows:
        mark = "🐞" if r["kind"] == "feedback" else "💥"
        lines.append(f"{mark} [{r['ts'][:16]}] u{r['user_id'] or '?'}: {r['summary']}")
    await _say(update.message, "\n".join(lines))

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик: пользователю — мягко, владельцу — карточка, в журнал — трейс."""
    import traceback
    err = ctx.error
    trace = "".join(traceback.format_exception(None, err, getattr(err, "__traceback__", None)))
    user = getattr(update, "effective_user", None)
    uid = getattr(user, "id", None)
    log.error("handler error (user=%s): %s\n%s", uid, err, trace[-1500:])
    try:
        db.log_tech(uid, "error", repr(err), trace[-3000:])
    except Exception:
        log.exception("log_tech failed")
    if OWNER_ID:
        try:
            await ctx.bot.send_message(
                OWNER_ID, f"💥 Ошибка у u{uid or '?'}: {repr(err)[:200]}\n…{trace[-500:]}")
        except Exception:
            pass
    msg = getattr(update, "effective_message", None)
    if msg is not None:
        try:
            await msg.reply_text("⚙️ Что-то пошло не так — я записал и разберусь. Попробуй ещё раз.")
        except Exception:
            pass

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
        try:
            db.touch_user(u.id, _user_label(u))    # CRM: живое имя/username, не голый id
        except Exception:
            log.exception("touch_user failed")
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

def _owner_badge(uid):
    """Бейдж в /start для владельца: сколько слов ждут подтверждения. None — нечего/не владелец."""
    if not OWNER_ID or uid != OWNER_ID:
        return None
    n = len(db.list_pending(CONTENT_USER))
    return f"📥 В очереди на подтверждение: {n} слов — /pending" if n else None

_ROLE_MARK = {"owner": "👑", "approved": "✅", "pending": "🔔", "blocked": "🚫"}

def _ago(iso):
    """«сегодня / вчера / N дн назад» из ISO-времени (для CRM)."""
    if not iso:
        return "—"
    d = iso[:10]
    today = datetime.date.today()
    days = (today - datetime.date.fromisoformat(d)).days
    return "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн назад"

async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/users — мини-CRM: роль, имя, контекст, активность; решения по заявкам."""
    if OWNER_ID and _learner(update) != OWNER_ID:
        await update.message.reply_text("Список пользователей — только у администратора.")
        return
    rows = db.crm_rows()
    if not rows:
        await update.message.reply_text("Пока никого нет.")
        return
    lines = ["👥 Пользователи:\n"]
    pending = []
    for r in rows:
        mark = _ROLE_MARK.get(r["role"], "·")
        line = (f"{mark} {r['name'] or r['user_id']} · {r['sessions']} сесс. · "
                f"{r['mastered']} слов · {_ago(r['last_seen'])}")
        if r["goal"]:
            line += f"\n    🎯 {r['goal']}"
        lines.append(line)
        if r["role"] == "pending":
            pending.append(r)
    msg = "\n".join(lines)
    if pending:                                   # решения по первой заявке прямо отсюда
        p = pending[0]
        await _say(update.message, msg + f"\n\n🔔 Ждёт решения: {p['name'] or p['user_id']}",
                   _knock_kb(p["user_id"]))
    else:
        await _say(update.message, msg)

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
        if db.get_program(uid) == "cycle":               # выключение слотовых напоминаний
            for slot in ("morning", "day", "evening"):
                db.set_slot_time(uid, slot, None)
            await update.message.reply_text(
                "Слотовые напоминания выключены. Вернуть: /remind и задай три времени.")
            return
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
    if minute_of_day == BACKUP_MINUTE:                       # ночной бэкап + сигнал о запасе
        log.info("daily backup -> %s", _daily_backup())
        days = db.stock_days()
        if days is not None and days < STOCK_ALERT_DAYS and OWNER_ID:
            try:
                await app.bot.send_message(
                    OWNER_ID, f"📉 Запас новых слов: ~{days} дн. у самого активного ученика. "
                              f"Пора пополнить: /fill <сценарий> 15")
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
        BotCommand("mywords", "мои слова в работе"),
        BotCommand("mistakes", "мои частые ошибки"),
        BotCommand("add", "добавить слова в учёбу"),
        BotCommand("topics", "учить слова по теме"),
        BotCommand("pace", "сложность: проще / сложнее"),
        BotCommand("program", "программа занятий"),
        BotCommand("remind", "напоминания о повторении"),
        BotCommand("feedback", "сообщить о проблеме"),
    ]
    await app.bot.set_my_commands(learner)                       # по умолчанию — всем
    if OWNER_ID:                                                 # владельцу — плюс админское
        await app.bot.set_my_commands(
            learner + [BotCommand("pending", "очередь новых слов (админ)"),
                       BotCommand("fill", "наполнить сценарий словами (админ)"),
                       BotCommand("users", "пользователи и заявки (админ)"),
                       BotCommand("health", "селфчек системы (админ)"),
                       BotCommand("issues", "ошибки и фидбек (админ)")],
            scope=BotCommandScopeChat(chat_id=OWNER_ID))
    global _REMINDER_TASK, _IDLE_TASK
    _REMINDER_TASK = asyncio.create_task(_reminder_loop(app))   # ссылка в глобале -> не соберётся GC
    _IDLE_TASK = asyncio.create_task(_idle_loop(app))           # авто-ИТОГ по неактивности

def main():
    if not TOKEN:
        raise SystemExit("Задай TELEGRAM_TOKEN")
    db.init_db()    # схема + миграции при каждом старте (идемпотентно) — не руками
    db.ensure_roles(OWNER_ID, ALLOWED_USERS)   # env-список -> роли (бесшовный переход)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)         # история ошибок переживает рестарты
    for name, ok_, detail in selfcheck.checks():   # селфчек на старте: проблемы — громко
        (log.info if ok_ else log.error)("selfcheck %s: %s", name, detail)
    app = (Application.builder().token(TOKEN)
           .persistence(_persistence())          # сессии переживают рестарт
           .post_init(_post_init).build())
    app.add_handler(TypeHandler(Update, _guard), group=-1)   # вахтёр: проверяется первым
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("fill", fill_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("issues", issues_cmd))
    app.add_error_handler(on_error)            # ни одно падение не уходит молча
    app.add_handler(CallbackQueryHandler(on_access, pattern=r"^acc:"))
    app.add_handler(CommandHandler("mistakes", mistakes_cmd))
    app.add_handler(CommandHandler("read", read_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("abstats", abstats_cmd))
    app.add_handler(CommandHandler("progress", progress_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("pace", pace_cmd))
    app.add_handler(CommandHandler("mywords", mywords_cmd))
    app.add_handler(CommandHandler("harder", harder_cmd))
    app.add_handler(CommandHandler("easier", easier_cmd))
    app.add_handler(CallbackQueryHandler(on_difficulty, pattern=r"^diff:"))
    app.add_handler(CommandHandler("program", program_cmd))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^rev:"))
    app.add_handler(CallbackQueryHandler(on_assembly, pattern=r"^asm:"))
    app.add_handler(CallbackQueryHandler(on_warm, pattern=r"^warm:"))   # превью продукции
    app.add_handler(CallbackQueryHandler(on_mcq, pattern=r"^mcq:"))
    app.add_handler(CallbackQueryHandler(on_activation, pattern=r"^act:"))
    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_pending, pattern=r"^pend:"))
    app.add_handler(CallbackQueryHandler(on_deep, pattern=r"^deep:"))
    app.add_handler(CallbackQueryHandler(on_branch, pattern=r"^branch:"))
    app.add_handler(CallbackQueryHandler(on_pace, pattern=r"^pace:"))
    app.add_handler(CallbackQueryHandler(on_calendar, pattern=r"^cal:"))
    app.add_handler(CallbackQueryHandler(on_program, pattern=r"^prog:"))
    app.add_handler(CallbackQueryHandler(on_scenario, pattern=r"^scn:"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry:"))
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
