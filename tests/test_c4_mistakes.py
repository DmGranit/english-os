"""C4: sample_mistakes — выборка калёк для ИТОГа."""
import db


def _seed(conn, n=5):
    for i in range(n):
        conn.execute(
            "INSERT INTO mistakes_ref (category, wrong, right, why, context) VALUES (?,?,?,?,?)",
            (f"cat_{i}", f"wrong_{i}", f"right_{i}", f"why_{i}", f"ctx_{i}")
        )


def test_sample_mistakes_returns_list(fresh_db):
    with db._conn() as c:
        _seed(c, 5)
    result = db.sample_mistakes(3)
    assert isinstance(result, list)
    assert len(result) == 3
    assert "wrong" in result[0]
    assert "right" in result[0]
    assert "why" in result[0]


def test_sample_mistakes_empty_db(fresh_db):
    assert db.sample_mistakes(8) == []


def test_sample_mistakes_respects_n(fresh_db):
    with db._conn() as c:
        _seed(c, 10)
    assert len(db.sample_mistakes(5)) == 5
    assert len(db.sample_mistakes(10)) == 10


def test_sample_mistakes_n_exceeds_table(fresh_db):
    with db._conn() as c:
        _seed(c, 3)
    result = db.sample_mistakes(10)
    assert len(result) == 3  # не больше чем в таблице
