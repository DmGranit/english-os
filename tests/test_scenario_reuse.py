"""B4: перенос сегодняшних слов в сценарий — scenario_target_words + _begin_scenario."""
import datetime
import db
from conftest import UID


def _learned_today(db_, wid):
    today = datetime.date.today().isoformat()
    with db_._conn() as c:
        c.execute("""UPDATE state SET status='learning', box=2, promoted_at=?, next_review=?
                     WHERE user_id=? AND word_id=?""", (today, today, UID, wid))


def test_today_words_come_first(fresh_db):
    _learned_today(fresh_db, 2)                          # deadline выучен сегодня (тема Status update)
    ids = fresh_db.scenario_target_words(UID, "Pitching", n=4)
    assert ids[0] == 2                                   # сегодняшнее — первым, даже из другой темы
    assert any(i in ids for i in (1, 3))                 # добор словами Pitching (invest/revenue)
    assert len(ids) <= 4 and len(set(ids)) == len(ids)   # кап + без дублей


def test_graceful_when_no_today_words(fresh_db):
    ids = fresh_db.scenario_target_words(UID, "Pitching", n=4)
    assert all(isinstance(i, int) for i in ids)          # только тема, без падений
    assert 2 not in ids                                  # deadline не сегодняшний → не подмешан
