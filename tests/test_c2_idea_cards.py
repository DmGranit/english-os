"""C2: DNA-идеи как концепт-карточки — idea_ref + idea_info."""
import db


def test_idea_ref_seeded_on_init(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM idea_ref").fetchone()[0]
    assert n == len(db._IDEA_SEED)


def test_idea_info_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    info = db.idea_info("Communication")
    assert info is not None
    assert info["ru"] == "Коммуникация"
    assert info["description"]
    assert info["thinking_pattern"]


def test_idea_info_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    assert db.idea_info("NonExistent") is None
    assert db.idea_info(None) is None


def test_idea_ref_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    db.init_db()  # повторный вызов не дублирует
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM idea_ref").fetchone()[0]
    assert n == len(db._IDEA_SEED)


def test_all_ideas_have_thinking_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db._conn() as c:
        rows = c.execute(
            "SELECT idea FROM idea_ref WHERE thinking_pattern IS NULL OR thinking_pattern=''"
        ).fetchall()
    assert rows == [], f"Идеи без thinking_pattern: {[r['idea'] for r in rows]}"
