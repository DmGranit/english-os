"""B3: упражнение-микс — exercise_for_word (сборка/продукция) + грейд + поурочный loop."""
import asyncio, types
import db, bot
from conftest import UID


# ─── test RED: mix lesson finish calls log_session + backup (regression B3) ──

def test_mix_lesson_finish_calls_log_session_and_backup(fresh_db, monkeypatch):
    """Completing the lesson through _lesson_advance must call db.log_session(uid,'new')
    and db.backup() — the side-effects previously skipped by the bare 'Урок завершён' reply."""
    log_calls = []
    backup_calls = []

    monkeypatch.setattr(db, "log_session", lambda uid, slot: log_calls.append((uid, slot)))
    monkeypatch.setattr(db, "backup", lambda: backup_calls.append(True))
    monkeypatch.setattr(db, "recognized_today", lambda uid, limit=1: [])
    # _next_action_text needs fresh_db to work
    monkeypatch.setattr(bot, "_next_action_text", lambda uid: "Дальше →")

    replies = []

    async def fake_reply(text, reply_markup=None, **k):
        replies.append(text)

    # Queue exhausted: pos will advance past last item → lesson end
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "mode": "lesson",
        "review_ok": 1, "review_fail": 0,
    })
    msg = types.SimpleNamespace(reply_text=fake_reply)

    asyncio.run(bot._lesson_advance(ctx, UID, msg))

    assert (UID, "new") in log_calls, "log_session(uid,'new') must be called on lesson finish"
    assert backup_calls, "db.backup() must be called on lesson finish"


def test_exercise_assembly_when_family_decomposable(fresh_db):
    # invest (word_id 1) с гнездом, где investment = invest + -ment (уверенно)
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment", "investor"]',))
    ex = fresh_db.exercise_for_word(1)
    assert ex["kind"] == "assembly"
    assert ex["expected"] == "investment"
    assert "invest" in ex["prompt"] and "-ment" in ex["prompt"]

def test_exercise_production_when_no_decomposable_family(fresh_db):
    # deadline (word_id 2): нет гнезда → продукция RU→EN
    ex = fresh_db.exercise_for_word(2)
    assert ex["kind"] == "production"
    assert ex["expected"] == "deadline"
    assert "крайний срок" in ex["prompt"]


# ─── helpers ─────────────────────────────────────────────────────────────────

def _async_none():
    f = asyncio.get_event_loop().create_future() if False else asyncio.Future()
    # Use simple approach: run a coroutine that returns None
    async def _none(): return None
    return asyncio.ensure_future(_none()) if False else _make_future(None)

def _make_future(val):
    """Return an already-resolved Future with value val."""
    import concurrent.futures as cf
    loop = asyncio.new_event_loop()
    f = loop.create_future()
    f.set_result(val)
    return f

def _make_coro(val=None):
    """Return a coroutine that resolves to val."""
    async def _inner(*a, **k): return val
    return _inner


# ─── test: correct assembly answer records and advances ──────────────────────

def test_mix_answer_assembly_correct_records_and_advances(fresh_db, monkeypatch):
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment"]',))
    recorded = {}

    async def rec(ctx, uid, wid, ok, ms, card_type=None):
        recorded.update(wid=wid, ok=ok, ct=card_type)

    advanced = {}

    async def adv(ctx, uid, message):
        advanced["yes"] = True

    monkeypatch.setattr(bot, "_record_review", rec)
    monkeypatch.setattr(bot, "_lesson_advance", adv)

    ctx = types.SimpleNamespace(user_data={
        "mix": {"wid": 1, "expected": "investment", "kind": "assembly"},
        "review_queue": [1], "review_pos": 0,
    })

    replies = []

    async def fake_reply(text, **k):
        replies.append(text)

    msg = types.SimpleNamespace(reply_text=fake_reply)
    update = types.SimpleNamespace(
        message=msg,
        effective_user=types.SimpleNamespace(id=UID),
    )
    asyncio.run(bot._handle_mix_answer(update, ctx, UID, "investment"))
    assert recorded["ok"] is True
    assert recorded["ct"] == "assembly"
    assert advanced.get("yes")
    assert "mix" not in ctx.user_data


# ─── test: wrong answer records fail ─────────────────────────────────────────

def test_mix_answer_wrong_records_fail(fresh_db, monkeypatch):
    recorded = {}

    async def rec(ctx, uid, wid, ok, ms, card_type=None):
        recorded.update(wid=wid, ok=ok, ct=card_type)

    advanced = {}

    async def adv(ctx, uid, message):
        advanced["yes"] = True

    monkeypatch.setattr(bot, "_record_review", rec)
    monkeypatch.setattr(bot, "_lesson_advance", adv)

    ctx = types.SimpleNamespace(user_data={
        "mix": {"wid": 2, "expected": "deadline", "kind": "production"},
        "review_queue": [2], "review_pos": 0,
    })

    replies = []

    async def fake_reply(text, **k):
        replies.append(text)

    msg = types.SimpleNamespace(reply_text=fake_reply)
    update = types.SimpleNamespace(
        message=msg,
        effective_user=types.SimpleNamespace(id=UID),
    )
    asyncio.run(bot._handle_mix_answer(update, ctx, UID, "wrong_answer"))
    assert recorded["ok"] is False
    assert recorded["ct"] == "production"
    assert advanced.get("yes")
    assert any("deadline" in r for r in replies)


# ─── test: lesson_advance at end sets mode=flow ──────────────────────────────

def test_lesson_advance_at_end_sets_flow(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "encoding_view", lambda wid: f"view:{wid}")
    monkeypatch.setattr(db, "backup", lambda: None)

    replies = []

    async def fake_reply(text, reply_markup=None, **k):
        replies.append(text)

    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "mode": "lesson",
    })
    msg = types.SimpleNamespace(reply_text=fake_reply)

    asyncio.run(bot._lesson_advance(ctx, UID, msg))
    assert ctx.user_data.get("mode") == "flow"
    assert any("завершён" in r for r in replies)


# ─── test: lesson_advance mid-deck sets enc_pending ──────────────────────────

def test_lesson_advance_mid_deck_sets_enc_pending(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "encoding_view", lambda wid: f"view:{wid}")
    monkeypatch.setattr(db, "backup", lambda: None)

    replies = []

    async def fake_reply(text, reply_markup=None, **k):
        replies.append(text)

    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1, 2], "review_pos": 0, "mode": "lesson",
    })
    msg = types.SimpleNamespace(reply_text=fake_reply)

    asyncio.run(bot._lesson_advance(ctx, UID, msg))
    assert ctx.user_data.get("enc_pending") is True
    assert ctx.user_data["review_pos"] == 1
    assert replies and "view:2" in replies[0]
