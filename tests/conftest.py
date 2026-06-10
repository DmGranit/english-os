"""Общие фикстуры: временная SQLite-база с минимальным контентом."""
import os, sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

UID = 1

# word_id, word, ru, priority, level, scenario
WORDS = [
    (1, "invest",      "инвестировать",            25, "B1", "Pitching"),
    (2, "deadline",    "крайний срок",             20, "B1", "Status update"),
    (3, "revenue",     "выручка",                  15, "B2", "Pitching"),
    (4, "stakeholder", "заинтересованная сторона", 10, "B2", "Negotiating"),
]


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Чистая база во временном файле + контент из WORDS + state для UID."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db._conn() as c:
        for wid, word, ru, pr, lvl, scn in WORDS:
            c.execute("""INSERT INTO content
                         (word_id, word, ru, priority, level, scenario,
                          family, collocations, phrasal)
                         VALUES (?,?,?,?,?,?, '[]','[]','[]')""",
                      (wid, word, ru, pr, lvl, scn))
    db.ensure_user_state(UID)
    return db
