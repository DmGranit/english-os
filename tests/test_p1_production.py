"""P1 (план v2.6): продукция-в-день-1 на УРОКЕ NEW.

Превью продукции (RU→EN, вне SRS) должно цепляться за слово, которое ученик СЕГОДНЯ
УЗНАЛ (promoted_at=today AND box>=2), а не за то, что провалил (осталось в box1).
`fresh_today` (box1) для урока задом наперёд — поэтому отдельный селектор `recognized_today`.
Машинерия тёплого превью (_warm_kb/on_warm/_handle_warm_answer) переиспользуется как есть.
"""
import asyncio, types

import bot, db
from conftest import UID


def _recognize(db_, wid, box=2):
    """Слово введено сегодня (promoted_at=today через start_learning) и продвинуто в box>=2."""
    db_.start_learning([wid], UID)
    with db_._conn() as c:
        c.execute("UPDATE state SET box=?, status='learning' WHERE user_id=? AND word_id=?",
                  (box, UID, wid))


# ---------- селектор recognized_today ----------

def test_recognized_today_returns_promoted_box2(fresh_db):
    _recognize(fresh_db, 1, box=2)                     # узнано сегодня
    got = fresh_db.recognized_today(UID, limit=5)
    assert [w["word_id"] for w in got] == [1]


def test_recognized_today_excludes_box1_failed(fresh_db):
    fresh_db.start_learning([1], UID)                  # введено сегодня, но осталось в box1 = провалено
    got = fresh_db.recognized_today(UID, limit=5)
    assert got == []                                   # проваленное слово не предлагаем «скажи сам»


def test_recognized_today_empty_when_all_box1(fresh_db):
    fresh_db.start_learning([1, 2, 3], UID)            # весь урок провален (всё в box1)
    assert fresh_db.recognized_today(UID, limit=5) == []


def test_recognized_today_respects_priority_limit(fresh_db):
    _recognize(fresh_db, 2, box=2)                     # priority 20
    _recognize(fresh_db, 1, box=3)                     # priority 25 — выше
    got = fresh_db.recognized_today(UID, limit=1)
    assert [w["word_id"] for w in got] == [1]          # топ-1 по priority


def test_recognized_today_excludes_yesterday(fresh_db):
    import datetime
    _recognize(fresh_db, 1, box=2)
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    with fresh_db._conn() as c:                         # узнано, но ВЧЕРА
        c.execute("UPDATE state SET promoted_at=? WHERE user_id=? AND word_id=1",
                  (yesterday, UID))
    assert fresh_db.recognized_today(UID, limit=5) == []


# ---------- поведение: урок NEW предлагает превью по узнанному ----------

class _Q:
    def __init__(self):
        self.edits, self.replies = [], []
        self.message = types.SimpleNamespace(reply_text=self._reply)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)

    async def _reply(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))


def test_finish_lesson_offers_warm_preview_for_recognized(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    _recognize(fresh_db, 1, box=2)                      # одно слово узнано сегодня
    ctx = types.SimpleNamespace(user_data={"mode": "lesson", "review_ok": 1, "review_fail": 0})
    q = _Q()
    asyncio.run(bot._finish_lesson(q, ctx, UID))
    texts = [t for t, _ in q.replies]
    assert any("⚡" in t for t in texts)                # превью предложено
    assert any("warm:go:1" in str(kb.to_dict()) for _, kb in q.replies if kb)  # кнопка на слово 1
    with fresh_db._conn() as c:                         # превью вне SRS — reviews пуст
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_finish_lesson_no_preview_when_all_failed(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    fresh_db.start_learning([1], UID)                  # слово осталось в box1 — провалено
    ctx = types.SimpleNamespace(user_data={"mode": "lesson", "review_ok": 0, "review_fail": 1})
    q = _Q()
    asyncio.run(bot._finish_lesson(q, ctx, UID))
    assert not any("⚡" in t for t, _ in q.replies)     # нечего продуцировать — не предлагаем
