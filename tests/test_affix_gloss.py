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
