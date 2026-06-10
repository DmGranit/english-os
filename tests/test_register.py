"""Регистр-батч (A2): пакетная разметка formal/neutral/informal с прямой записью."""
import annotate_register as ar

from conftest import UID


def test_parse_map_valid_and_filtered():
    raw = ('Вот разметка:\n{"invest": "neutral", "deadline": "informal", '
           '"revenue": "formal", "stakeholder": "выдумка", "чужое": "formal"}')
    m = ar.parse_map(raw, expected={"invest", "deadline", "revenue", "stakeholder"})
    assert m == {"invest": "neutral", "deadline": "informal", "revenue": "formal"}
    # «stakeholder» с мусорным значением отброшен, «чужое» слово — не из батча


def test_parse_map_garbage_returns_empty():
    assert ar.parse_map("не буду размечать", expected={"invest"}) == {}


def test_apply_map_updates_register(fresh_db):
    n = ar.apply_map({"invest": "formal", "deadline": "informal"})
    assert n == 2
    with fresh_db._conn() as c:
        rows = {r["word"]: r["register"] for r in
                c.execute("SELECT word, register FROM content")}
    assert rows["invest"] == "formal"
    assert rows["deadline"] == "informal"
    assert rows["revenue"] is None                   # не размеченное не тронуто


def test_targets_and_batches(fresh_db):
    words = ar.fetch_targets()                       # в фикстуре register пуст у всех
    assert {w for w in words} == {"invest", "deadline", "revenue", "stakeholder"}
    batches = list(ar.batches(["a", "b", "c", "d", "e"], size=2))
    assert batches == [["a", "b"], ["c", "d"], ["e"]]
