"""Анти-старвейшн продукции в колоде (v2.6 пост-смоук): продуктивная (box>=
PRODUCTIVE_FROM_BOX) due-карточка не должна выпадать из колоды из-за капа, даже
когда она наименее просрочена среди due-слов. Без этого `prod=0` держится
операционно: единственная зрелая карточка погребена за беклогом узнавания."""
import datetime

import db
from conftest import UID


def _set(db_, wid, box, next_review):
    with db_._conn() as c:
        c.execute("""UPDATE state SET status='learning', box=?, next_review=?
                     WHERE user_id=? AND word_id=?""", (box, next_review, UID, wid))


def test_due_today_keeps_productive_within_cap(fresh_db):
    today = datetime.date.today().isoformat()
    older = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _set(fresh_db, 1, db.PRODUCTIVE_FROM_BOX, today)      # продуктивная, наименее просрочена
    for wid in (2, 3, 4):                                 # узнавание, просрочено сильнее
        _set(fresh_db, wid, 2, older)
    due, _ = fresh_db.due_today(UID, limit=2)             # кап=2: при «самые просроченные первыми» box3 выпадает
    ids = [w["word_id"] for w in due]
    assert 1 in ids                                       # продуктивная гарантированно в колоде
    assert len(due) <= 2                                  # кап соблюдён


def test_due_today_unchanged_when_no_productive(fresh_db):
    """Деградация: нет продуктивных due → поведение прежнее (самые просроченные первыми)."""
    today = datetime.date.today().isoformat()
    older = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _set(fresh_db, 1, 2, today)
    _set(fresh_db, 2, 2, older)
    due, _ = fresh_db.due_today(UID, limit=1)
    assert [w["word_id"] for w in due] == [2]             # самый просроченный
