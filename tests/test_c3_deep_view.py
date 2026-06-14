"""C3: deep_view += IPA / particle_logic / BrE_AmE / confuse."""
import db


def _seed(conn):
    conn.execute("""INSERT OR IGNORE INTO phrasal_ref (phrasal, meaning, example, logic, category)
                    VALUES (?,?,?,?,?)""",
                 ("bring up", "поднять тему", "He brought it up.", "up = на поверхность", "communication"))
    conn.execute("""INSERT OR IGNORE INTO bre_ame_ref (category, bre, ame, ru)
                    VALUES (?,?,?,?)""",
                 ("vocabulary", "flat", "apartment", "квартира"))
    conn.execute("""INSERT OR IGNORE INTO confuse_ref (root, trap, group_a, group_b, how_to)
                    VALUES (?,?,?,?,?)""",
                 ("affect/effect", "глагол vs существительное",
                  "affect (v) — влиять", "effect (n) — результат",
                  "A=Action (глагол), E=End result (существительное)"))


def _add_word(conn, word_id, word, ipa_us=None, ipa_uk=None):
    conn.execute("""INSERT OR IGNORE INTO content
                    (word_id, word, ru, priority, level, scenario, family, collocations,
                     phrasal, ipa_us, ipa_uk)
                    VALUES (?,?,?,10,'B1','Test','[]','[]','[]',?,?)""",
                 (word_id, word, "перевод", ipa_us, ipa_uk))


# ---------- IPA ----------

def test_deep_view_shows_ipa(fresh_db):
    with db._conn() as c:
        _add_word(c, 50, "invest", ipa_us="ɪnˈvest", ipa_uk="ɪnˈvest")
    text = db.deep_view(50)
    assert "ɪnˈvest" in text
    assert "🔊" in text


def test_deep_view_ipa_both_variants(fresh_db):
    with db._conn() as c:
        _add_word(c, 51, "schedule", ipa_us="ˈskɛdʒuːl", ipa_uk="ˈʃɛdjuːl")
    text = db.deep_view(51)
    assert "ˈskɛdʒuːl" in text
    assert "ˈʃɛdjuːl" in text


def test_deep_view_no_ipa_silent(fresh_db):
    with db._conn() as c:
        _add_word(c, 52, "bring", ipa_us=None, ipa_uk=None)
    text = db.deep_view(52)
    assert "🔊" not in text


# ---------- particle logic (C3.Δa) ----------

def test_phrasal_logic_found(fresh_db):
    with db._conn() as c:
        _seed(c)
        _add_word(c, 53, "bring")
    hits = db.phrasal_logic_for_word("bring")
    assert any("up = на поверхность" in logic for _, logic in hits)


def test_phrasal_logic_not_found(fresh_db):
    with db._conn() as c:
        _seed(c)
    assert db.phrasal_logic_for_word("invest") == []
    assert db.phrasal_logic_for_word(None) == []


def test_deep_view_shows_particle_logic(fresh_db):
    with db._conn() as c:
        _seed(c)
        _add_word(c, 53, "bring")
    text = db.deep_view(53)
    assert "↗️" in text
    assert "bring up" in text


# ---------- BrE/AmE (C3.Δb) ----------

def test_bre_ame_for_word_found(fresh_db):
    with db._conn() as c:
        _seed(c)
    r = db.bre_ame_for_word("flat")
    assert r is not None
    assert r["ame"] == "apartment"


def test_bre_ame_for_word_ame_side(fresh_db):
    with db._conn() as c:
        _seed(c)
    r = db.bre_ame_for_word("apartment")
    assert r is not None
    assert r["bre"] == "flat"


def test_bre_ame_for_word_not_found(fresh_db):
    assert db.bre_ame_for_word("invest") is None
    assert db.bre_ame_for_word(None) is None


# ---------- confuse_ref (C3) ----------

def test_confuse_for_word_group_a(fresh_db):
    with db._conn() as c:
        _seed(c)
    r = db.confuse_for_word("affect")
    assert r is not None
    assert "A=Action" in r["how_to"]


def test_confuse_for_word_group_b(fresh_db):
    with db._conn() as c:
        _seed(c)
    r = db.confuse_for_word("effect")
    assert r is not None


def test_confuse_for_word_not_found(fresh_db):
    assert db.confuse_for_word("invest") is None
    assert db.confuse_for_word(None) is None


def test_deep_view_shows_confuse(fresh_db):
    with db._conn() as c:
        _seed(c)
        _add_word(c, 54, "affect")
    text = db.deep_view(54)
    assert "⚠️" in text
    assert "не путай" in text
