"""B1: обезвреживание лавины — единый дневной бюджет ввода + кап колоды."""
import types

import db
from conftest import UID, WORDS


def _more_words(db_, n):
    """Добавить n new-слов сверх фикстуры (для тестов бюджета/капа)."""
    with db_._conn() as c:
        base = c.execute("SELECT COALESCE(MAX(word_id),0) FROM content").fetchone()[0]
        for i in range(1, n + 1):
            c.execute("""INSERT INTO content (word_id, word, ru, priority, level, scenario,
                         family, collocations, phrasal)
                         VALUES (?,?,?,?,?,?, '[]','[]','[]')""",
                      (base + i, f"w{i}", f"п{i}", 5, "B1", "Universal"))
    db_.ensure_user_state(UID)


# ---------- единый дневной бюджет: start_learning тоже считается ----------

def _all_new_ids(db_):
    with db_._conn() as c:
        return [r["word_id"] for r in c.execute(
            "SELECT word_id FROM content ORDER BY word_id")]


def test_start_learning_respects_daily_budget(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "DAILY_NEW_CAP", 3)
    _more_words(fresh_db, 10)
    added = fresh_db.start_learning(_all_new_ids(fresh_db)[:8], UID)
    assert fresh_db.promoted_today(UID) <= db.DAILY_NEW_CAP * db.DIRECT_BUDGET_MULT


def test_intake_budget_left_decreases(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "DAILY_NEW_CAP", 5)
    assert db.intake_budget_left(UID) == 5 * db.DIRECT_BUDGET_MULT
    fresh_db.start_learning([1, 2], UID)
    assert db.intake_budget_left(UID) == 5 * db.DIRECT_BUDGET_MULT - 2


def test_budget_exhausted_blocks_further_intake(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "DAILY_NEW_CAP", 1)
    _more_words(fresh_db, 10)
    added = fresh_db.start_learning(_all_new_ids(fresh_db), UID)   # бюджет 1*mult
    assert len(added) == db.DAILY_NEW_CAP * db.DIRECT_BUDGET_MULT


def test_start_learning_returns_actually_added(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "DAILY_NEW_CAP", 10)
    added = fresh_db.start_learning([1, 2, 3], UID)
    assert set(added) == {1, 2, 3}                          # вернул реально введённые


# ---------- кап колоды ----------

def test_due_today_capped(fresh_db, monkeypatch):
    import datetime
    _more_words(fresh_db, 40)
    today = datetime.date.today().isoformat()
    with fresh_db._conn() as c:                             # сделать 40 слов due
        c.execute("""UPDATE state SET status='learning', box=1, next_review=?
                     WHERE user_id=?""", (today, UID))
    due, st = fresh_db.due_today(UID, limit=20)
    assert len(due) == 20                                   # колода ограничена
    total = fresh_db.due_count(UID)
    assert total >= 40                                      # но общий долг виден


def test_due_today_no_limit_by_default(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    due, _ = fresh_db.due_today(UID)                        # без limit — как раньше
    assert len(due) == 2
