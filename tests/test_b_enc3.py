"""B-enc3: мини-очередь доработки (rework) — повторный показ без SRS-записи.

Ключевой инвариант (план стр.217): ответ на rework-карточку НЕ вызывает db.review.
"""
import asyncio, types
import bot, db
from conftest import UID


# ---------- helpers ----------

def _ctx(queue, pos=0, boxes=None, rework_ids=None, rework_queue=None):
    ud = {
        "review_queue": list(queue),
        "review_pos": pos,
        "review_box": boxes or {wid: 1 for wid in queue},
        "mode": "review",
    }
    if rework_ids is not None:
        ud["rework_ids"] = set(rework_ids)
    if rework_queue is not None:
        ud["rework_queue"] = list(rework_queue)
    return types.SimpleNamespace(user_data=ud)


# ---------- rework_queue накапливается при провале (db-путь) ----------

def test_fail_adds_to_rework_queue(fresh_db, monkeypatch):
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1])
    asyncio.run(bot._record_review(ctx, UID, 1, False, None))
    assert ctx.user_data.get("rework_queue") == [1]


def test_ok_no_rework_queue(fresh_db, monkeypatch):
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1])
    asyncio.run(bot._record_review(ctx, UID, 1, True, None))
    assert ctx.user_data.get("rework_queue", []) == []


def test_rework_no_duplicate(fresh_db, monkeypatch):
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1])
    asyncio.run(bot._record_review(ctx, UID, 1, False, None))
    asyncio.run(bot._record_review(ctx, UID, 1, False, None))
    # второй провал не дублирует запись
    assert ctx.user_data.get("rework_queue") == [1]


# ---------- главный инвариант: rework-карточка НЕ пишет db.review ----------

def test_rework_card_skips_db_review(fresh_db, monkeypatch):
    """Ответ на rework-карточку (wid in rework_ids) не создаёт запись в reviews."""
    fresh_db.start_learning([1], UID)
    call_count = {"n": 0}
    orig_review = db.review

    def counting_review(*a, **kw):
        call_count["n"] += 1
        return orig_review(*a, **kw)

    monkeypatch.setattr(db, "review", counting_review)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1], rework_ids=[1])
    asyncio.run(bot._record_review(ctx, UID, 1, True, None))
    assert call_count["n"] == 0             # db.review не вызывался


def test_rework_card_no_second_rework(fresh_db, monkeypatch):
    """Провал на rework-карточке не добавляет в rework_queue (кап по построению)."""
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1], rework_ids=[1])
    asyncio.run(bot._record_review(ctx, UID, 1, False, None))
    assert ctx.user_data.get("rework_queue", []) == []


def test_rework_summary_counters_not_touched(fresh_db, monkeypatch):
    """Rework-ответ не инкрементит review_ok/review_fail (R24: summary врать не должен)."""
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = _ctx([1], rework_ids=[1])
    ctx.user_data["review_ok"] = 2
    ctx.user_data["review_fail"] = 1
    asyncio.run(bot._record_review(ctx, UID, 1, True, None))
    assert ctx.user_data["review_ok"] == 2   # не изменился
    assert ctx.user_data["review_fail"] == 1


# ---------- _next_card вливает rework в конец очереди ----------

def test_next_card_flushes_rework(fresh_db, monkeypatch):
    """Когда pos >= len(queue) и rework_queue не пуст: queue расширяется, rework_ids установлен."""
    fresh_db.start_learning([1, 2], UID)
    edits = []

    class _Q:
        message = types.SimpleNamespace(reply_text=lambda *a, **k: None)
        async def edit_message_text(self, text, reply_markup=None, **kw):
            edits.append(text)
        async def answer(self, *a, **k): pass

    monkeypatch.setattr(db, "get_word",
                        lambda wid: {"word_id": wid, "word": "w", "ru": "r",
                                     "ipa_uk": None, "example": None, "dna_idea": "X"})
    monkeypatch.setattr(db, "mcq_options", lambda wid, k=4: [])
    ctx = _ctx([1, 2], pos=1, rework_queue=[2])   # pos=1 = последняя карточка; _next_card инкрементит до 2 = конец
    asyncio.run(bot._next_card(_Q(), ctx, UID))
    assert 2 in ctx.user_data.get("review_queue", [])     # wid 2 влит
    assert 2 in ctx.user_data.get("rework_ids", set())    # помечен
    assert ctx.user_data.get("rework_queue", []) == []    # очищен
