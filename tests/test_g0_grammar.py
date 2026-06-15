"""G0: grammar_ref оживлён в deep_view (долг C3).
Матч по целому слову (\b), не подстроке — R36.
"""
import db


def _seed_grammar(conn):
    rows = [
        ("Modal verbs",   "modal + V (без to)",           "You should go, not You should to go", "You should to go → You should go"),
        ("Future simple", "will + V / am going to + V",   "I will go, she will come",             "I will to go → I will go"),
        ("Present Perfect", "have/has + V3",              "I have finished it",                   "I finished it (when result matters)"),
    ]
    for topic, formula, example, ru_mistake in rows:
        conn.execute("""INSERT OR IGNORE INTO grammar_ref (topic, formula, example, ru_mistake)
                        VALUES (?,?,?,?)""", (topic, formula, example, ru_mistake))


def test_grammar_for_word_modal(fresh_db):
    """Служебное слово 'should' → тема Modal verbs."""
    with db._conn() as c:
        _seed_grammar(c)
    gr = db.grammar_for_word("should")
    assert gr is not None
    assert "modal" in gr["topic"].lower()


def test_grammar_for_word_no_match(fresh_db):
    """Контент-слово без грамм-привязки → None."""
    with db._conn() as c:
        _seed_grammar(c)
    gr = db.grammar_for_word("decision")
    assert gr is None


def test_grammar_for_word_none_input(fresh_db):
    assert db.grammar_for_word(None) is None
    assert db.grammar_for_word("") is None


def test_deep_view_includes_grammar(fresh_db, monkeypatch):
    """deep_view для слова с grammar-хитом содержит 📐."""
    monkeypatch.setattr(db, "grammar_for_word",
                        lambda w: {"topic": "Modal verbs",
                                   "formula": "modal + V",
                                   "ru_mistake": "You should to go"})
    text = db.deep_view(1)
    assert "📐" in text
    assert "Modal verbs" in text


def test_deep_view_no_grammar_for_unknown(fresh_db, monkeypatch):
    """deep_view без grammar-хита — не содержит 📐."""
    monkeypatch.setattr(db, "grammar_for_word", lambda w: None)
    text = db.deep_view(1)
    assert "📐" not in text


def test_grammar_no_substring_false_match(fresh_db):
    """'skill' не даёт хит на 'will'; 'scan' не даёт хит на 'can'. R36-поправка."""
    with db._conn() as c:
        _seed_grammar(c)
    assert db.grammar_for_word("will") is not None   # will → Future — должен матчить
    assert db.grammar_for_word("skill") is None      # skill содержит 'will' как подстроку — не матчит
    assert db.grammar_for_word("scan") is None       # scan содержит 'can' как подстроку — не матчит
