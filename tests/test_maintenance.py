"""Этап 1: maintenance-повторения для box 5 и сводка пути (Nation-рамка)."""
import datetime

from conftest import UID


def _set_state(db_, wid, box, status, next_review):
    with db_._conn() as c:
        c.execute("""UPDATE state SET box=?, status=?, next_review=?
                     WHERE user_id=? AND word_id=?""",
                  (box, status, next_review, UID, wid))


def _today_plus(db_, days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


# ---------- known-слова возвращаются на проверку ----------

def test_known_word_comes_due_for_maintenance(fresh_db):
    fresh_db.ensure_user_state(UID)
    _set_state(fresh_db, 1, 5, "known", datetime.date.today().isoformat())
    due, st = fresh_db.due_today(UID)
    assert [w["word_id"] for w in due] == [1]            # «выученное» больше не вечно
    assert st[1]["box"] == 5                             # направление будет prod


def test_known_word_not_due_before_time(fresh_db):
    fresh_db.ensure_user_state(UID)
    _set_state(fresh_db, 1, 5, "known", _today_plus(fresh_db, 30))
    due, _ = fresh_db.due_today(UID)
    assert due == []


def test_maintenance_success_extends_interval(fresh_db):
    fresh_db.ensure_user_state(UID)
    _set_state(fresh_db, 1, 5, "known", datetime.date.today().isoformat())
    r = fresh_db.review(1, True, UID)
    assert r["box"] == 5 and r["status"] == "known"
    assert r["next_review"] == _today_plus(fresh_db, fresh_db.MAINTENANCE_DAYS)


def test_maintenance_failure_returns_to_circulation(fresh_db):
    fresh_db.ensure_user_state(UID)
    _set_state(fresh_db, 1, 5, "known", datetime.date.today().isoformat())
    r = fresh_db.review(1, False, UID)
    # 4.1 (вариант c, канон Ч.3.8): провал ПРОВЕРКИ ВЫЖИВАНИЯ (box 5) -> box 1 (в оборот).
    # Провал выживания = реальное угасание: слово перестаёт числиться освоенным,
    # can-do-прокси не врёт. (Мягкий Лейтнер A3.1 — только для box 3-4, см. ниже.)
    assert r["box"] == 1 and r["status"] == "forgot"
    assert r["next_review"] == _today_plus(fresh_db, fresh_db.INTERVALS[1])


def test_soft_leitner_box4_drops_one(fresh_db):
    """A3.1 остаётся для box 3-4: провал не обнуляет, а опускает на одну коробку."""
    fresh_db.ensure_user_state(UID)
    _set_state(fresh_db, 1, 4, "learning", datetime.date.today().isoformat())
    r = fresh_db.review(1, False, UID)
    assert r["box"] == 3 and r["status"] == "forgot"     # box4 -> box3, не в начало


def test_first_arrival_to_box5_keeps_leitner_interval(fresh_db):
    fresh_db.start_learning([1], UID)
    for _ in range(3):
        fresh_db.review(1, True, UID)                    # box 4
    r = fresh_db.review(1, True, UID)                    # первый раз box 5
    assert r["next_review"] == _today_plus(fresh_db, fresh_db.INTERVALS[5])


# ---------- сводка пути (Nation-рамка, вторична к can-do) ----------

def test_progress_summary_counts(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    for _ in range(3):
        fresh_db.review(1, True, UID)                    # invest -> box 3 (освоено)
    fresh_db.log_session(UID, "flow")
    fresh_db.log_session(UID, "scenario")
    s = fresh_db.progress_summary(UID)
    assert s["mastered"] == 1
    assert s["learning"] == 1
    assert s["new"] == 2                                 # revenue, stakeholder
    assert s["sessions"] == 2
    assert s["since"] == datetime.date.today().isoformat()
    assert s["nation_target"] == 3000


def test_progress_summary_empty_user(fresh_db):
    s = fresh_db.progress_summary(UID)
    assert s["mastered"] == 0 and s["sessions"] == 0 and s["since"] is None


def test_progress_counts_effort_and_tiers(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    fresh_db.review(1, True, UID)                       # box 2 — «знакомо»
    fresh_db.review(2, True, UID)
    fresh_db.review(2, True, UID)                       # box 3 — «освоено»
    s = fresh_db.progress_summary(UID)
    assert s["reviews"] == 3                            # усилие видно: всего повторений
    assert s["familiar"] == 1                           # знакомо (box 1-2)
    assert s["mastered"] == 1                           # освоено (box>=3)
    assert "accuracy" in s                              # точность ответов
