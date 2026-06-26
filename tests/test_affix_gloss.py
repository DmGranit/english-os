"""affix_gloss: пер-слово смысл аффикса (override affix_ref в строке разбора) — 2026-06-26."""
import json
import db


def test_set_affix_gloss_writes_without_clobber(fresh_db):
    db.set_derivation(1, base="form", affix="in-", gloss="придать form — сообщать")
    assert db.set_affix_gloss(1, "в-/внутрь") is True
    d = db.decompose(1)
    assert d["affix_gloss"] == "в-/внутрь"
    assert d["base"] == "form" and d["affix"] == "in-"
    assert d["gloss"] == "придать form — сообщать"      # не затёрт


def test_set_affix_gloss_no_derivation_returns_false(fresh_db):
    # word 2 = deadline, без derivation
    assert db.set_affix_gloss(2, "что-то") is False


def test_set_affix_gloss_malformed_json_returns_false(fresh_db):
    with db._conn() as c:
        c.execute("UPDATE content SET derivation='{битый' WHERE word_id=1")
    assert db.set_affix_gloss(1, "в-/внутрь") is False


def test_set_affix_gloss_empty_removes_key(fresh_db):
    db.set_derivation(1, base="form", affix="in-")
    db.set_affix_gloss(1, "в-/внутрь")
    assert "affix_gloss" in db.decompose(1)
    assert db.set_affix_gloss(1, "") is True
    assert "affix_gloss" not in db.decompose(1)


# ---------- рендер: affix_gloss перебивает дефолт affix_ref ----------

def test_render_affix_gloss_overrides_default(fresh_db):
    with db._conn() as c:
        c.execute("INSERT OR REPLACE INTO affix_ref (affix, kind, meaning_ru) "
                  "VALUES ('in-','prefix','отрицание (не-)')")
    db.set_derivation(1, base="form", affix="in-")        # word 1 = invest (переиспользуем строку)
    db.set_affix_gloss(1, "в-/внутрь")
    line = next(l for l in db.morpho_lines(db.get_word(1)) if l.startswith("🧩 разбор"))
    assert "в-/внутрь" in line
    assert "отрицание" not in line


def test_render_default_when_no_affix_gloss(fresh_db):
    with db._conn() as c:
        c.execute("INSERT OR REPLACE INTO affix_ref (affix, kind, meaning_ru) "
                  "VALUES ('in-','prefix','отрицание (не-)')")
    db.set_derivation(1, base="form", affix="in-")
    line = next(l for l in db.morpho_lines(db.get_word(1)) if l.startswith("🧩 разбор"))
    assert "отрицание (не-)" in line


# ---------- гнездо: encoding_view / exercise_for_word предпочитают affix_gloss члена ----------

def _seed_nest_member(fresh_db):
    """word 1 (invest) с гнездом из revenue (word 3); revenue — in-дериватив с affix_gloss."""
    with db._conn() as c:
        c.execute("INSERT OR REPLACE INTO affix_ref (affix, kind, meaning_ru) "
                  "VALUES ('in-','prefix','отрицание (не-)')")
        c.execute("UPDATE content SET family='[\"revenue\"]' WHERE word_id=1")
    db.set_derivation(3, base="form", affix="in-")    # revenue = тест-член с разбором
    db.set_affix_gloss(3, "в-/внутрь")


def test_encoding_view_member_prefers_affix_gloss(fresh_db):
    _seed_nest_member(fresh_db)
    text = db.encoding_view(1)
    assert "в-/внутрь" in text
    assert "отрицание" not in text


def test_encoding_view_member_default_when_no_affix_gloss(fresh_db):
    with db._conn() as c:
        c.execute("INSERT OR REPLACE INTO affix_ref (affix, kind, meaning_ru) "
                  "VALUES ('in-','prefix','отрицание (не-)')")
        c.execute("UPDATE content SET family='[\"revenue\"]' WHERE word_id=1")
    db.set_derivation(3, base="form", affix="in-")    # без affix_gloss
    text = db.encoding_view(1)
    assert "отрицание (не-)" in text


def test_exercise_hint_prefers_affix_gloss(fresh_db):
    _seed_nest_member(fresh_db)
    ex = db.exercise_for_word(1)
    assert ex["kind"] == "assembly"
    assert "в-/внутрь" in ex["prompt"]
    assert "отрицание" not in ex["prompt"]
