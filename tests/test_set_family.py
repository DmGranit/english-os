"""Фаза 2: db.set_family — правка family + переиндекс word_family, без клоббера коллокаций."""
import json
import db


def test_set_family_updates_content_and_index(fresh_db):
    assert db.set_family(1, ["investment", "reinvest"]) is True   # word 1 = invest
    w = db.get_word(1)
    assert w["family"] == ["investment", "reinvest"]
    with db._conn() as c:
        members = {r["member"] for r in
                   c.execute("SELECT member FROM word_family WHERE word_id=1")}
    assert members == {"investment", "reinvest"}


def test_set_family_missing_word_returns_false(fresh_db):
    assert db.set_family(99999, ["x"]) is False


def test_set_family_does_not_touch_collocations(fresh_db):
    with db._conn() as c:
        c.execute("INSERT INTO word_collocation (word_id, text) VALUES (1, 'invest heavily')")
    db.set_family(1, ["investor"])
    with db._conn() as c:
        coll = {r["text"] for r in
                c.execute("SELECT text FROM word_collocation WHERE word_id=1")}
    assert coll == {"invest heavily"}            # семья переписана, коллокация цела
