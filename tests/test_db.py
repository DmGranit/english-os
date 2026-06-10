"""Регрессионные тесты ядра db: SRS-переходы, дневной лимит новых, применение ИТОГ."""
import datetime

import pytest

from conftest import UID


def _today_plus(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _state(db, wid):
    with db._conn() as c:
        return dict(c.execute("SELECT * FROM state WHERE user_id=? AND word_id=?",
                              (UID, wid)).fetchone())


# ---------- review: переходы Лейтнера ----------

def test_review_remembered_moves_box_up(fresh_db):
    fresh_db.start_learning([1], UID)                       # box 1
    r = fresh_db.review(1, True, UID)
    assert r["box"] == 2
    assert r["status"] == "learning"
    assert r["next_review"] == _today_plus(fresh_db.INTERVALS[2])


def test_review_forgot_resets_to_box1(fresh_db):
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID)                           # box 2
    r = fresh_db.review(1, False, UID)
    assert r["box"] == 1
    assert r["status"] == "forgot"
    assert r["next_review"] == _today_plus(fresh_db.INTERVALS[1])


def test_review_box5_becomes_known(fresh_db):
    fresh_db.start_learning([1], UID)
    for _ in range(4):                                      # 1 -> 2 -> 3 -> 4 -> 5
        r = fresh_db.review(1, True, UID)
    assert r["box"] == 5
    assert r["status"] == "known"
    r = fresh_db.review(1, True, UID)                       # выше 5 не растёт
    assert r["box"] == 5


def test_review_writes_journal(fresh_db):
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID, variant="layered", ms=1234)
    fresh_db.review(1, False, UID)
    with fresh_db._conn() as c:
        rows = c.execute("SELECT word_id, remembered, variant, ms FROM reviews "
                         "WHERE user_id=? ORDER BY id", (UID,)).fetchall()
    assert [(r["remembered"], r["variant"], r["ms"]) for r in rows] == \
           [(1, "layered", 1234), (0, None, None)]


def test_review_without_state_raises(fresh_db):
    with pytest.raises(ValueError):
        fresh_db.review(999, True, UID)


# ---------- promote_new: дневной лимит и приоритет ----------

def test_promote_new_respects_daily_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(fresh_db, "DAILY_NEW_CAP", 2)
    words = fresh_db.promote_new(UID, n=10)
    assert len(words) == 2
    assert fresh_db.promote_new(UID, n=10) == []            # лимит дня исчерпан


def test_promote_new_orders_by_priority(fresh_db, monkeypatch):
    monkeypatch.setattr(fresh_db, "DAILY_NEW_CAP", 2)
    words = fresh_db.promote_new(UID, n=2)
    assert [w["word"] for w in words] == ["invest", "deadline"]   # priority 25, 20


def test_promote_new_marks_state(fresh_db):
    wid = fresh_db.promote_new(UID, n=1)[0]["word_id"]
    st = _state(fresh_db, wid)
    assert st["status"] == "learning"
    assert st["box"] == 1
    assert st["promoted_at"] == datetime.date.today().isoformat()
    assert st["next_review"] == datetime.date.today().isoformat()


# ---------- apply_session_summary: ИТОГ -> SRS / pending / errors ----------

def test_summary_reviewed_updates_srs(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    res = fresh_db.apply_session_summary(
        {"reviewed": [{"word": "invest", "ok": True},
                      {"word": "deadline", "ok": False}]}, UID)
    assert res["ok"] == 1 and res["fail"] == 1
    assert _state(fresh_db, 1)["box"] == 2                  # вспомнил
    assert _state(fresh_db, 2)["status"] == "forgot"        # забыл


def test_summary_unknown_word_skipped(fresh_db):
    res = fresh_db.apply_session_summary(
        {"reviewed": [{"word": "zzz_not_in_base", "ok": True}]}, UID)
    assert res == {"ok": 0, "fail": 0, "added": 0, "skipped": 1, "errors": 0}


def test_summary_add_goes_to_pending(fresh_db):
    res = fresh_db.apply_session_summary(
        {"add": [{"word": "traction", "ru": "импульс роста"}]}, UID)
    assert res["added"] == 1
    items = fresh_db.list_pending(UID)
    assert [i["word"] for i in items] == ["traction"]


def test_summary_add_existing_word_skipped(fresh_db):
    res = fresh_db.apply_session_summary(
        {"add": [{"word": "invest", "ru": "инвестировать"},
                 {"word": "", "ru": "пусто"}]}, UID)
    assert res["added"] == 0
    assert res["skipped"] == 2
    assert fresh_db.list_pending(UID) == []


def test_summary_errors_logged_and_category_sanitized(fresh_db):
    res = fresh_db.apply_session_summary(
        {"errors": [{"category": "word_order", "wrong": "I yesterday went",
                     "correct": "I went yesterday", "cause": "SVOMPT"},
                    {"category": "выдумка", "wrong": "a", "correct": "b"}]}, UID)
    assert res["errors"] == 2
    pats = {p["category"]: p["n"] for p in fresh_db.error_patterns(UID)}
    assert pats == {"word_order": 1, "other": 1}            # мусорная категория -> other


def test_summary_empty_payload_is_noop(fresh_db):
    res = fresh_db.apply_session_summary({}, UID)
    assert res == {"ok": 0, "fail": 0, "added": 0, "skipped": 0, "errors": 0}
