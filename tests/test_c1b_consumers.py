"""C1.b: быстрые потребители — scenario_ref_get + colloc_anti_matches."""
import db


def _seed(conn):
    conn.execute("""INSERT OR REPLACE INTO scenario_ref
                    (scenario, opener, key_phrases, closer, context)
                    VALUES (?,?,?,?,?)""",
                 ("Pitching", "I'd like to present…", "Our solution…", "Any questions?", "B2"))
    conn.execute("""INSERT OR IGNORE INTO colloc_ref (core, verbs, adjs, ru, anti)
                    VALUES (?,?,?,?,?)""",
                 ("deadline", '["meet","miss"]', '["tight"]', "крайний срок", "do a deadline"))
    conn.execute("""INSERT OR IGNORE INTO colloc_ref (core, verbs, adjs, ru, anti)
                    VALUES (?,?,?,?,?)""",
                 ("meeting", '["hold","attend"]', '["brief"]', "встреча", None))


# ---------- scenario_ref_get ----------

def test_scenario_ref_get_found(fresh_db):
    with db._conn() as c:
        _seed(c)
    ref = db.scenario_ref_get("Pitching")
    assert ref is not None
    assert ref["opener"] == "I'd like to present…"
    assert ref["key_phrases"] == "Our solution…"
    assert ref["closer"] == "Any questions?"


def test_scenario_ref_get_not_found(fresh_db):
    assert db.scenario_ref_get("NonExistent") is None
    assert db.scenario_ref_get(None) is None


# ---------- colloc_anti_matches ----------

def test_anti_match_found(fresh_db):
    with db._conn() as c:
        _seed(c)
    words = [{"word": "deadline"}]
    hits = db.colloc_anti_matches(words, "I always do a deadline")
    assert hits == [("deadline", "do a deadline")]


def test_anti_no_match(fresh_db):
    with db._conn() as c:
        _seed(c)
    words = [{"word": "deadline"}]
    hits = db.colloc_anti_matches(words, "I always meet the deadline")
    assert hits == []


def test_anti_null_anti_skipped(fresh_db):
    with db._conn() as c:
        _seed(c)
    # "meeting" has anti=NULL — should never match
    words = [{"word": "meeting"}]
    hits = db.colloc_anti_matches(words, "hold a meeting")
    assert hits == []


def test_anti_empty_inputs(fresh_db):
    assert db.colloc_anti_matches([], "some text") == []
    assert db.colloc_anti_matches([{"word": "deadline"}], "") == []
    assert db.colloc_anti_matches([{"word": "deadline"}], None) == []


def test_anti_emoji_prefix_stripped(fresh_db):
    """C1.b bug-fix: anti в БД хранится с префиксом ❌, в речи ученика его нет → должен матчить."""
    with db._conn() as c:
        c.execute("""INSERT OR IGNORE INTO colloc_ref (core, verbs, adjs, ru, anti)
                     VALUES (?,?,?,?,?)""",
                  ("decision", '["make"]', '[]', "решение", "❌ do a decision"))
    words = [{"word": "decision"}]
    hits = db.colloc_anti_matches(words, "i will do a decision")
    assert hits == [("decision", "❌ do a decision")]
