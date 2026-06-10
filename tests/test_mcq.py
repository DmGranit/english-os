"""Учебный процесс v2, часть 1: box 1 — выбор из 4 (дистракторы из базы)."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- дистракторы из базы ----------

def test_distractors_from_base(fresh_db):
    opts = db.mcq_options(1, k=4)                      # invest
    rus = [o["ru"] for o in opts]
    assert "инвестировать" in rus                      # верный среди вариантов
    assert len(opts) == 4 and len(set(rus)) == 4       # 4 разных
    assert all(o["ru"] for o in opts)                  # без пустых


def test_distractors_degrade_gracefully(fresh_db):
    # даже если близких мало — не падаем, добираем любыми
    opts = db.mcq_options(2, k=4)
    assert 2 <= len(opts) <= 4
    assert any(o["word_id"] == 2 for o in opts)        # верный всегда внутри


# ---------- карточка box 1 = MCQ, без ввода/самооценки ----------

def _ctx(box, wid=1):
    return types.SimpleNamespace(user_data={
        "review_queue": [wid], "review_box": {wid: box}, "review_pos": 0})


def test_box1_is_mcq(fresh_db, monkeypatch):
    monkeypatch.setattr(bot.random, "sample", lambda seq, k: list(seq))
    ctx = _ctx(1)
    text, kb = bot._card_payload(ctx, UID)
    assert "value" not in text                         # ответ не подсвечен
    assert "invest" in text                            # вопрос — слово
    rows = kb.to_dict()["inline_keyboard"]
    cbs = [b["callback_data"] for row in rows for b in row]
    assert all(c.startswith("mcq:") for c in cbs)
    assert len(cbs) == 4
    assert "mcq:1" in cbs                              # верный вариант среди кнопок


def test_box2_still_cloze_not_mcq(fresh_db):
    ctx = _ctx(2)
    _, kb = bot._card_payload(ctx, UID)
    cbs = [b["callback_data"] for row in kb.to_dict()["inline_keyboard"] for b in row]
    assert any(c == "rev:show" for c in cbs)           # box 2 не тронут


# ---------- обработка ответа MCQ: объективный зачёт ----------

class _Q:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.message = types.SimpleNamespace(reply_text=self._r)

    async def answer(self, *a, **k): pass
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)
    async def _r(self, *a, **k): return self.message


def _run_mcq(fresh_db, monkeypatch, pick_wid, queue=None, boxes=None):
    rec = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None, card_type=None:
                        rec.update(wid=wid, ok=ok) or
                        {"word_id": wid, "box": 2, "status": "learning", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    monkeypatch.setattr(db, "backup", lambda: None)
    monkeypatch.setattr(bot.random, "sample", lambda seq, k: list(seq))
    ctx = _ctx(1) if not queue else types.SimpleNamespace(
        user_data={"review_queue": queue, "review_box": boxes, "review_pos": 0})
    bot._card_payload(ctx, UID)                        # построить MCQ (кладёт mcq_answer)
    q = _Q(f"mcq:{pick_wid}")
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_mcq(update, ctx))
    return rec, q, ctx


def test_mcq_correct_scores(fresh_db, monkeypatch):
    rec, q, ctx = _run_mcq(fresh_db, monkeypatch, pick_wid=1)
    assert rec == {"wid": 1, "ok": True}
    assert any("✅" in e for e in q.edits)


def test_mcq_wrong_fails_and_shows_truth(fresh_db, monkeypatch):
    rec, q, ctx = _run_mcq(fresh_db, monkeypatch, pick_wid=3)   # revenue вместо invest
    assert rec["ok"] is False
    assert any("инвестировать" in e for e in q.edits)
