"""Этап 2: A/B-редизайн — рандомизация, честный тайминг, атрибуция к прошлому показу."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- назначение варианта: пользователь×слово, не чётность word_id ----------

def test_variant_stable_and_user_dependent():
    assert bot._variant(UID, 7) == bot._variant(UID, 7)          # детерминировано
    pool = {bot._variant(UID, w) for w in range(1, 40)}
    assert pool == {"layered", "flat"}                           # оба варианта встречаются
    # назначение зависит от пользователя: у двух людей раскладка по словам разная
    diff = [w for w in range(1, 60) if bot._variant(1, w) != bot._variant(2, w)]
    assert diff                                                  # хотя бы одно слово отличается


# ---------- тайминг: время recall = до «Показать ответ», не до оценки ----------

class _FakeQ:
    def __init__(self, data):
        self.data = data
        self.message = types.SimpleNamespace(
            reply_text=self._areply, edit_text=self._aedit)

    async def answer(self, *a, **k): pass
    async def edit_message_text(self, *a, **k): pass
    async def _areply(self, *a, **k): pass
    async def _aedit(self, *a, **k): pass


def test_recall_ms_measured_until_reveal(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    recorded = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None, card_type=None:
                        recorded.update(wid=wid, ms=ms) or
                        {"word_id": wid, "box": 2, "status": "learning", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_box": {1: 1}, "review_pos": 0,
        "card_shown_at": 100.0})
    update = types.SimpleNamespace(callback_query=_FakeQ("rev:show"),
                                   effective_user=types.SimpleNamespace(id=UID))
    monkeypatch.setattr(bot.time, "time", lambda: 103.5)         # вспоминал 3.5 секунды
    asyncio.run(bot.on_review(update, ctx))                      # «Показать ответ»

    monkeypatch.setattr(bot.time, "time", lambda: 200.0)         # долго читал ответ/сеть
    update = types.SimpleNamespace(callback_query=_FakeQ("rev:ok"),
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_review(update, ctx))                      # «Вспомнил»
    assert recorded["ms"] == 3500                                # чтение ответа НЕ вошло


# ---------- атрибуция: результат относится к варианту ПРЕДЫДУЩЕГО показа ----------

def _add_review(db_, wid, remembered, variant, ms=None):
    with db_._conn() as c:
        c.execute("""INSERT INTO reviews (user_id, word_id, ts, remembered, variant, ms)
                     VALUES (?,?,?,?,?,?)""",
                  (UID, wid, "2026-06-10T00:00:00", int(remembered), variant, ms))


def test_variant_stats_attributes_to_previous_encoding(fresh_db):
    # слово 1: показ layered -> следующий раз ВСПОМНИЛ (зачёт layered)
    _add_review(fresh_db, 1, True, "layered")
    _add_review(fresh_db, 1, True, "layered", ms=2000)
    # слово 2: показ flat -> следующий раз ЗАБЫЛ (зачёт flat)
    _add_review(fresh_db, 2, True, "flat")
    _add_review(fresh_db, 2, False, "flat", ms=4000)
    stats = {s["variant"]: s for s in fresh_db.variant_stats(UID)}
    assert stats["layered"]["n"] == 1 and stats["layered"]["accuracy"] == 1.0
    assert stats["flat"]["n"] == 1 and stats["flat"]["accuracy"] == 0.0


def test_variant_stats_first_review_excluded(fresh_db):
    _add_review(fresh_db, 1, True, "layered")                    # первого показа недостаточно
    assert fresh_db.variant_stats(UID) == []
