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
import os, json, re, time, asyncio, datetime
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      BotCommand, BotCommandScopeChat)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, TypeHandler, ApplicationHandlerStop,
                          ContextTypes, filters)
import db, prompts, llm, enrich

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

def _learner(update):
    """Telegram-id пользователя: у каждого свой прогресс (общий только контент)."""
    return update.effective_user.id

# режим и история диалога живут в context.user_data (на сессию)
MAX_HISTORY = 20    # держим последние 20 сообщений (~10 обменов) — чтобы контекст не рос без предела
PRODUCTIVE_FROM_BOX = 3   # box 1–2: спрашиваем EN→RU (узнавание); box 3+: RU→EN (продукция)

def _mode(ctx):     return ctx.user_data.get("mode", "flow")
def _history(ctx):  return ctx.user_data.setdefault("history", [])

def _remember(hist, role, content):
    """Добавить сообщение и оставить только последние MAX_HISTORY (свежий хвост)."""
    hist.append({"role": role, "content": content})
    hist[:] = hist[-MAX_HISTORY:]     # [-MAX_HISTORY:] = последние N; старые отбрасываются

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

# ---------- /start ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = _learner(update)
    db.ensure_user_state(uid)
    if not db.is_onboarded(uid):                  # новый пользователь -> онбординг
        await _begin_onboarding(update, ctx)
        return
    # вернувшийся: один ведущий CTA по самому нужному действию + полное меню ниже
    due, _ = db.due_today(uid)
    n_due = len(due)
    rows = []
    if n_due:
        rows.append([InlineKeyboardButton(f"☀️ Повторить ({n_due})", callback_data="mode:review")])
        lead = f"С возвращением! 👋\nСегодня {n_due} слов(а) ждут повторения — закрепим?"
    elif db.new_remaining(uid):
        rows.append([InlineKeyboardButton("🌅 Учить новые", callback_data="mode:new")])
        lead = "С возвращением! 👋\nПовторять нечего ✅ — берём новые слова?"
    else:
        lead = "С возвращением! 👋\nВсё повторено, новых пока нет — поболтаем, тема или /read?"
    rows += [
        [InlineKeyboardButton("🌅 Новые", callback_data="mode:new"),
         InlineKeyboardButton("☀️ Повторение", callback_data="mode:review")],
        [InlineKeyboardButton("🎭 Сценарий", callback_data="mode:scenario"),
         InlineKeyboardButton("🗣️ Поток", callback_data="mode:flow")],
        [InlineKeyboardButton("📚 Темы (учить по теме)", callback_data="topics")],
    ]
    await update.message.reply_text(lead, reply_markup=InlineKeyboardMarkup(rows))

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
        "Жми /start (меню), пиши «учим <слово>», говори голосом или /read для чтения. "
        "Сменить темп — /pace.")

async def pace_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pace — выбрать темп заново (без оценки уровня)."""
    await update.message.reply_text(
        "С каким темпом продолжить? Это не оценка — подстроюсь.", reply_markup=_pace_kb())

# ---------- переключение режима кнопкой ----------
async def on_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = q.data.split(":")[1]
    ctx.user_data["mode"] = mode
    ctx.user_data["history"] = []
    uid = _learner(update)
    db.ensure_user_state(uid)            # новый ученик / новые слова получают state

    if mode == "new":
        words = db.promote_new(uid)          # лимит держит сам db
        if not words:
            if db.new_remaining(uid) == 0:
                msg = ("🎉 Ты прошёл все новые слова базы! Пока добавить нечего — "
                       "закрепляем: жми ☀️ Повторение или 📖 /read.")
            else:
                msg = (f"На сегодня дневная норма новых слов уже взята "
                       f"(по {db.DAILY_NEW_CAP}/день — так мозг усваивает лучше, без лавины повторений). "
                       f"Давай закрепим: ☀️ Повторение или 📖 /read. Новые — завтра 🌙")
            await q.edit_message_text(msg)
            return
        seed = ("Познакомь КРАТКО с новыми словами на сегодня: слово — перевод, 1 живой пример, "
                "1 коллокация. Без разбора корней/слоёв (это под кнопкой 🔍). "
                "В конце предложи составить свою фразу.\n" + db.format_for_agent(words))
        await _ask(q, ctx, mode, seed, uid, markup=_deep_kb(words))

    elif mode == "review":
        due, st = db.due_today(uid)
        if not due:
            await q.edit_message_text("На сегодня повторять нечего ✅")
            return
        ctx.user_data["review_queue"] = [w["word_id"] for w in due]  # очередь слов
        ctx.user_data["review_box"]   = {wid: st[wid]["box"] for wid in st}  # box -> направление
        ctx.user_data["review_pos"]   = 0      # на какой карточке стоим
        ctx.user_data["review_ok"]    = 0      # счётчик «вспомнил»
        ctx.user_data["review_fail"]  = 0      # счётчик «забыл»
        await _show_card(q, ctx)               # показать первую карточку

    else:  # scenario / flow
        await q.edit_message_text({
            "scenario": "🎭 Сценарий. Назови ситуацию (питч, переговоры, статус) — войду в роль.",
            "flow": "🗣️ Поток. Просто общаемся на английском — пиши или говори голосом.",
        }[mode])

# ---------- обычное сообщение (текст и голос идут одним путём) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _process_user_text(update, ctx, _learner(update), update.message.text)

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Голосовое: скачать -> распознать речь -> дальше как обычный текст."""
    uid = _learner(update)
    try:
        f = await update.message.voice.get_file()
        buf = await f.download_as_bytearray()
    except Exception:
        await update.message.reply_text("Не смог скачать голосовое. Попробуй ещё раз.")
        return
    text = llm.transcribe(bytes(buf))
    if not text:
        await update.message.reply_text("🎤 Не расслышал. Повтори голосом или напиши текстом.")
        return
    await update.message.reply_text(f"🗣 You said: {text}")
    await _process_user_text(update, ctx, uid, text)

async def _process_user_text(update, ctx, uid, text):
    await _typing(update.message)                 # «печатает…», пока ИИ думает
    # команды завершения сессии -> ИТОГ + бэкап
    if text.strip().lower() in ("закончили", "отчёт", "отчет", "стоп"):
        end = ("<КОНЕЦ СЕССИИ> Заверши занятие: дай человеческий отчёт ИТОГ, а в самом конце — "
               "машинный JSON-блок (reviewed/add/errors). Не переводи это сообщение и не проси повторить.")
        reply = await _call(ctx, _mode(ctx), uid, end, with_summary=True)
        data, reply = _extract_summary(reply)         # отделить машинный блок от текста
        if data:
            r = db.apply_session_summary(data, uid)   # записать в reviews / pending / errors
            reply += (f"\n\n— записано в базу: повторений {r['ok'] + r['fail']} "
                      f"(✅ {r['ok']} / ❌ {r['fail']}), новых слов в очередь: {r['added']}"
                      f", структурных ошибок: {r.get('errors', 0)}")
        db.backup()
        await update.message.reply_text(reply)
        return

    targets = _parse_learn_intent(text)          # «учим X, Y» -> разбор по графу
    if targets:
        await _teach_words(update, ctx, uid, targets)
        return

    reply = await _call(ctx, _mode(ctx), uid, text)
    await update.message.reply_text(reply)

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
    await msg.reply_text(reply, reply_markup=_deep_kb(wd))

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

async def _ask(q, ctx, mode, seed_text, uid, markup=None):
    """Первый ход режима: положить данные слов как user-сообщение и получить разбор."""
    system = prompts.assemble(mode) + "\n\n" + db.learner_profile(uid)
    hist = ctx.user_data["history"]
    _remember(hist, "user", seed_text)
    await _typing(q.message)
    reply = await asyncio.to_thread(llm.chat, system, hist)
    _remember(hist, "assistant", reply)
    await q.edit_message_text(reply, reply_markup=markup)

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

def _variant(word_id):
    """A/B-назначение карточки (детерминированно по слову): чётные — layered, нечётные — flat.
    Инструментовка для будущей проверки «сеть vs плоско», не само доказательство."""
    return "layered" if word_id % 2 == 0 else "flat"

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

async def _show_card(q, ctx, reveal=False):
    """Показать текущую карточку очереди (правит то же сообщение)."""
    queue = ctx.user_data.get("review_queue", [])
    pos = ctx.user_data.get("review_pos", 0)
    wid = queue[pos]
    word = db.get_word(wid)
    box = ctx.user_data.get("review_box", {}).get(wid, 1)
    productive = box >= PRODUCTIVE_FROM_BOX     # зрелое слово -> RU→EN; иначе EN→RU
    if not reveal:
        ctx.user_data["card_shown_at"] = time.time()   # старт замера time-on-task
    ctx.user_data["review_reveal"] = reveal
    await q.edit_message_text(
        _review_card_text(word, pos + 1, len(queue), reveal, productive, _variant(wid)),
        reply_markup=_review_kb(reveal),
    )

async def _finish_review(q, ctx, uid):
    """Колода кончилась: показать итог и сделать бэкап базы."""
    ok   = ctx.user_data.get("review_ok", 0)
    fail = ctx.user_data.get("review_fail", 0)
    db.backup()
    await q.edit_message_text(
        f"🔁 Повторение завершено!\n\n"
        f"Карточек: {ok + fail}\n✅ Вспомнил: {ok}\n❌ Забыл: {fail}\n\n"
        f"Прогресс сохранён. /start — вернуться в меню."
    )

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
        await _show_card(q, ctx, reveal=True)
        return

    # action == ok | fail -> записать результат текущего слова
    word_id = queue[pos]
    remembered = (action == "ok")
    shown = ctx.user_data.get("card_shown_at")
    ms = int((time.time() - shown) * 1000) if shown else None     # time-on-task
    db.review(word_id, remembered, uid, variant=_variant(word_id), ms=ms)  # ← цикл + инструментовка
    db.adapt_band(uid)                          # тихо подстраиваем полосу по последним повторениям
    key = "review_ok" if remembered else "review_fail"
    ctx.user_data[key] = ctx.user_data.get(key, 0) + 1

    ctx.user_data["review_pos"] = pos + 1   # перейти к следующей карточке
    if ctx.user_data["review_pos"] >= len(queue):
        await _finish_review(q, ctx, uid)
    else:
        await _show_card(q, ctx, reveal=False)

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

def _pending_kb(pending_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Добавить", callback_data=f"pend:ok:{pending_id}"),
        InlineKeyboardButton("🗑 Отклонить", callback_data=f"pend:no:{pending_id}"),
    ]])

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
                                    reply_markup=_pending_kb(items[0]["id"]))

async def on_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, action, pid = q.data.split(":")           # pend : ok|no : <id>
    if OWNER_ID and _learner(update) != OWNER_ID:
        await q.edit_message_text("Добавление слов в базу — только у администратора.")
        return
    uid = CONTENT_USER
    if action == "ok":
        msg = "✅ Добавлено в базу." if db.confirm_pending(int(pid), uid) else "⚠️ Уже обработано."
    else:
        msg = "🗑 Отклонено." if db.reject_pending(int(pid), uid) else "⚠️ Уже обработано."
    items = db.list_pending(uid)                  # показать следующее или конец
    if items:
        await q.edit_message_text(msg + "\n\n" + _pending_card_text(items[0]),
                                  reply_markup=_pending_kb(items[0]["id"]))
    else:
        await q.edit_message_text(msg + "\n\nОчередь пуста ✅")

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
    await update.message.reply_text(text)

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
    rows = db.cando_progress(_learner(update))
    lines = ["📈 Что ты уже можешь (по освоенным словам сценария):\n"]
    for r in rows:
        mark = "✅" if r["ready"] else ("▶" if r["mastered"] else "·")
        lines.append(f"{mark} [{r['level']}] {r['ru']} — {r['mastered']}/{r['total']}")
    lines.append("\n⚠️ Это ориентир по словарю, а не официальная оценка CEFR.")
    await update.message.reply_text("\n".join(lines))

# ---------- A/B-инструментовка: /abstats ----------
async def abstats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/abstats — сводка A/B (сеть vs плоско): точность и время ответа. Сигнал, НЕ доказательство."""
    stats = db.variant_stats(_learner(update))
    if not stats:
        await update.message.reply_text("Пока нет данных A/B — поучись через «☀️ Повторение».")
        return
    lines = ["🧪 A/B карточек (сеть vs плоско):\n"]
    for s in stats:
        acc = f"{round(s['accuracy']*100)}%" if s["accuracy"] is not None else "—"
        ms = f"{s['avg_ms']} мс" if s["avg_ms"] is not None else "—"
        lines.append(f"• {s['variant']}: n={s['n']}, точность {acc}, ср. время {ms}")
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

# ---------- контроль доступа (allowlist) ----------
async def _guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пускаем только разрешённые Telegram-id (если ALLOWED_USERS задан). Иначе — стоп."""
    u = update.effective_user
    if ALLOWED_USERS and (u is None or u.id not in ALLOWED_USERS):
        if update.callback_query:
            await update.callback_query.answer("⛔ Доступ закрыт", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Этот бот персональный. Доступ закрыт.")
        raise ApplicationHandlerStop

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

# ---------- ежедневные напоминания (лёгкий asyncio-цикл, без apscheduler) ----------
async def remind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/remind 9 — напоминать в 9:00 · /remind off — выключить · без аргумента — статус."""
    uid = _learner(update)
    args = ctx.args or []
    if not args:
        h = db.get_reminder(uid)
        status = f"в {h}:00" if h is not None else "выключены"
        await update.message.reply_text(
            f"Напоминания: {status}. Поменять: /remind 9 · выключить: /remind off")
        return
    a = args[0].lower()
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
    await update.message.reply_text(f"Готово — напомню в {h}:00, если будут слова к повторению. ☀️")

async def _reminder_loop(app):
    """Просыпается на каждом часе; шлёт «N к повторению» тем, у кого этот час задан и есть due."""
    while True:
        now = datetime.datetime.now()
        await asyncio.sleep(max(60, 3600 - (now.minute * 60 + now.second)))
        hour = datetime.datetime.now().hour
        for uid in db.reminder_users(hour):
            due, _ = db.due_today(uid)
            if due:
                try:
                    await app.bot.send_message(
                        uid, f"☀️ {len(due)} слов ждут повторения сегодня. /start → Повторение.")
                except Exception:
                    pass

async def _post_init(app):
    """Меню команд (учебное — всем; админское — только владельцу) + цикл напоминаний."""
    learner = [
        BotCommand("start", "меню и начало"),
        BotCommand("read", "чтение под мой уровень"),
        BotCommand("progress", "мой прогресс"),
        BotCommand("mistakes", "мои частые ошибки"),
        BotCommand("add", "добавить слова в учёбу"),
        BotCommand("topics", "учить слова по теме"),
        BotCommand("pace", "сменить темп / сложность"),
        BotCommand("remind", "напоминания о повторении"),
    ]
    await app.bot.set_my_commands(learner)                       # по умолчанию — всем
    if OWNER_ID:                                                 # владельцу — плюс админское
        await app.bot.set_my_commands(
            learner + [BotCommand("pending", "очередь новых слов (админ)")],
            scope=BotCommandScopeChat(chat_id=OWNER_ID))
    global _REMINDER_TASK
    _REMINDER_TASK = asyncio.create_task(_reminder_loop(app))   # ссылка в глобале -> не соберётся GC

def main():
    if not TOKEN:
        raise SystemExit("Задай TELEGRAM_TOKEN")
    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(TypeHandler(Update, _guard), group=-1)   # вахтёр: проверяется первым
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("mistakes", mistakes_cmd))
    app.add_handler(CommandHandler("read", read_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("abstats", abstats_cmd))
    app.add_handler(CommandHandler("progress", progress_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("pace", pace_cmd))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^rev:"))
    app.add_handler(CallbackQueryHandler(on_pending, pattern=r"^pend:"))
    app.add_handler(CallbackQueryHandler(on_deep, pattern=r"^deep:"))
    app.add_handler(CallbackQueryHandler(on_branch, pattern=r"^branch:"))
    app.add_handler(CallbackQueryHandler(on_pace, pattern=r"^pace:"))
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
