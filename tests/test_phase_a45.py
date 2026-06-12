"""Фаза A, батчи A4-A5 (v2.4): тёплое превью продукции (A4.1), честный STT (A4.2),
интерливинг колоды (A4.3), правило 98% с проверкой (A5.1), след чтения (A5.2)."""
import asyncio, datetime, types

import bot, db, llm
from conftest import UID


def _set_due(db_, wid, next_review, scenario=None):
    with db_._conn() as c:
        c.execute("""UPDATE state SET status='learning', box=2, next_review=?
                     WHERE user_id=? AND word_id=?""", (next_review, UID, wid))
        if scenario:
            c.execute("UPDATE content SET scenario=? WHERE word_id=?", (scenario, wid))


def _add_word(db_, wid, word, scenario):
    with db_._conn() as c:
        c.execute("""INSERT INTO content (word_id, word, ru, priority, level, scenario,
                     family, collocations, phrasal) VALUES (?,?,?,?,?,?, '[]','[]','[]')""",
                  (wid, word, word + "_ru", 30 - wid, "B1", scenario))
    db_.ensure_user_state(UID)


# ---------- A4.3: интерливинг — темы внутри дня чередуются ----------

def test_interleave_no_theme_stacks(fresh_db):
    today = datetime.date.today().isoformat()
    _add_word(fresh_db, 5, "alpha", "Pitching")
    _add_word(fresh_db, 6, "beta", "Status update")
    # 3 слова Pitching (1,3,5) и 3 Status update (2,5->6...) на один день
    for wid, scn in ((1, "Pitching"), (3, "Pitching"), (5, "Pitching"),
                     (2, "Status update"), (4, "Status update"), (6, "Status update")):
        _set_due(fresh_db, wid, today, scn)
    due, _ = fresh_db.due_today(UID)
    themes = [w["scenario"] for w in due]
    runs = max(len(list(g)) for _, g in __import__("itertools").groupby(themes))
    assert runs <= 2                                  # нет 3 подряд одной темы
    assert sorted(w["word_id"] for w in due) == [1, 2, 3, 4, 5, 6]   # никто не потерян


def test_interleave_keeps_overdue_days_order(fresh_db):
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    _set_due(fresh_db, 2, today, "Pitching")          # сегодня
    _set_due(fresh_db, 1, yesterday, "Pitching")      # просрочено сильнее
    due, _ = fresh_db.due_today(UID)
    assert [w["word_id"] for w in due] == [1, 2]      # самые просроченные первыми


# ---------- A4.1: тёплое превью продукции (вне SRS) ----------

def test_fresh_today_picks_only_todays_box1(fresh_db):
    fresh_db.start_learning([1], UID)                 # сегодня, box 1
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    fresh_db.start_learning([2], UID)
    with fresh_db._conn() as c:                       # слово 2 — «вчерашнее»
        c.execute("UPDATE state SET promoted_at=? WHERE user_id=? AND word_id=2",
                  (yesterday, UID))
    fresh = fresh_db.fresh_today(UID, limit=5)
    assert [w["word_id"] for w in fresh] == [1]


class _Q:
    def __init__(self):
        self.edits, self.replies = [], []
        self.message = types.SimpleNamespace(reply_text=self._reply)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)

    async def _reply(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))


def test_finish_review_offers_warm_preview(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    fresh_db.start_learning([1], UID)                 # есть свежее слово дня
    ctx = types.SimpleNamespace(user_data={"review_ok": 1, "review_fail": 0})
    q = _Q()
    asyncio.run(bot._finish_review(q, ctx, UID))
    texts = [t for t, _ in q.replies]
    assert any("⚡" in t for t in texts)               # превью предложено
    assert any("warm:go:1" in str(kb.to_dict()) for _, kb in q.replies if kb)  # кнопка на слово 1


def test_finish_review_no_fresh_no_preview(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    ctx = types.SimpleNamespace(user_data={"review_ok": 1, "review_fail": 0})
    q = _Q()
    asyncio.run(bot._finish_review(q, ctx, UID))
    assert not any("⚡" in t for t, _ in q.replies)    # нечего показывать — не предлагаем


def _msg():
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)

    return types.SimpleNamespace(reply_text=reply_text), sent


def test_warm_answer_correct_not_recorded(fresh_db):
    fresh_db.start_learning([1], UID)
    ctx = types.SimpleNamespace(user_data={"warm_wid": 1})
    msg, sent = _msg()
    update = types.SimpleNamespace(message=msg)
    asyncio.run(bot._handle_warm_answer(update, ctx, UID, "invest"))
    assert "🔥" in sent[0]                             # успех отпразднован
    assert "warm_wid" not in ctx.user_data            # ожидание снято
    with fresh_db._conn() as c:                       # SRS не тронут — превью вне учёта
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_warm_answer_wrong_not_recorded(fresh_db):
    fresh_db.start_learning([1], UID)
    ctx = types.SimpleNamespace(user_data={"warm_wid": 1})
    msg, sent = _msg()
    update = types.SimpleNamespace(message=msg)
    asyncio.run(bot._handle_warm_answer(update, ctx, UID, "wrongword"))
    assert "invest" in sent[0]                        # правильный ответ показан
    with fresh_db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


# ---------- A4.2: честный STT на проверочных карточках ----------

def test_stt_no_hint_on_assembly_and_warm(fresh_db):
    for key in ("asm_wid", "warm_wid"):
        ctx = types.SimpleNamespace(user_data={key: 1})
        lang, prompt = bot._stt_hints(ctx, UID)
        assert lang == "en" and prompt is None


def test_stt_hint_stays_on_selfassess_card(fresh_db):
    import time as _t
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_box": {1: 2}, "review_pos": 0,
        "card_shown_at": _t.time()})
    lang, prompt = bot._stt_hints(ctx, UID)           # box 2 — самооценка, подсказка живёт
    assert lang == "en" and "invest" in prompt


# ---------- A5.1: правило 98% — проверка и регенерат ----------

def test_unknown_share_counts_and_morphology():
    known = {"invest", "deadline"}
    ratio, unk = bot._unknown_share("We invest today", known)
    assert ratio == 0.0 and unk == []                 # we/today — простое ядро
    ratio, unk = bot._unknown_share("Deadlines invested investing", known)
    assert ratio == 0.0                               # морфология: -s/-ed/-ing от известных
    ratio, unk = bot._unknown_share("Quantum chromodynamics rules", known)
    assert ratio > 0.5 and "quantum" in unk


def test_unknown_share_ignores_keywords_line():
    known = set()
    text = "We work today.\n🔑 words: paradigm — a typical example"
    ratio, unk = bot._unknown_share(text, known)
    assert "paradigm" not in unk                      # строка 🔑 — пояснения, не текст


def _read_update():
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)

    async def send_action(*a, **k):
        pass

    msg = types.SimpleNamespace(reply_text=reply_text,
                                chat=types.SimpleNamespace(send_action=send_action))
    return types.SimpleNamespace(message=msg,
                                 effective_user=types.SimpleNamespace(id=UID)), sent


def test_read_regenerates_once_on_violation(fresh_db, monkeypatch):
    fresh_db.start_learning([1, 2], UID)              # целевые есть
    calls = []

    def fake_chat(system, messages, max_tokens=600, model=None):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return "Ubiquitous paradigms juxtapose ontological frameworks."   # нарушение
        return "We invest today. The deadline is near."                       # норм

    monkeypatch.setattr(llm, "chat", fake_chat)
    update, sent = _read_update()
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot.read_cmd(update, ctx))
    assert len(calls) == 2                            # ровно один регенерат
    assert "ПЕРЕДЕЛКА" in calls[1]                    # модель получила список нарушителей
    assert any("invest" in t for t in sent)           # ученику ушла вторая версия


def test_read_good_text_no_regenerate(fresh_db, monkeypatch):
    fresh_db.start_learning([1, 2], UID)
    calls = []

    def fake_chat(system, messages, max_tokens=600, model=None):
        calls.append(1)
        return "We invest today. The deadline is near."

    monkeypatch.setattr(llm, "chat", fake_chat)
    update, sent = _read_update()
    asyncio.run(bot.read_cmd(update, types.SimpleNamespace(user_data={})))
    assert len(calls) == 1                            # чистый текст — без переделки


# ---------- A5.2: чтение оставляет след ----------

def test_read_logs_input_session(fresh_db, monkeypatch):
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: "We invest today.")
    update, _ = _read_update()
    asyncio.run(bot.read_cmd(update, types.SimpleNamespace(user_data={})))
    with fresh_db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=? AND mode='input'",
                      (UID,)).fetchone()[0]
    assert n == 1                                     # режим больше не невидим


def test_progress_summary_counts_inputs(fresh_db):
    fresh_db.log_session(UID, "input")
    fresh_db.log_session(UID, "input")
    fresh_db.log_session(UID, "scenario")
    s = fresh_db.progress_summary(UID)
    assert s["inputs"] == 2
    assert s["sessions"] == 3                         # общий счёт занятий не сломан
