"""B5: качество enrich — целевой смысл + hardening validate; set_derivation→bool."""
import json
import db, enrich, llm
from conftest import UID


def test_set_derivation_returns_bool(fresh_db):
    assert fresh_db.set_derivation(1, base="vest", affix="in-") is True     # word_id 1 = invest есть
    assert fresh_db.set_derivation(99999, base="x", affix="-y") is False    # нет такого слова


# ---------- sense: целевой смысл ----------

def _qa_ok(generate):
    def chat(system, messages, **k):
        if "рецензент" in system:
            return '{"ok": true, "drop_root": false, "reason": "ok"}'
        return generate(system, messages)
    return chat


def test_sense_passed_into_generation_prompt(fresh_db, monkeypatch):
    captured = {}
    def gen(system, messages):
        captured["user"] = messages[-1]["content"]
        return json.dumps({"word": "patient", "ru": "пациент", "scenario": "Universal", "level": "A2"})
    monkeypatch.setattr(llm, "chat", _qa_ok(gen))
    enrich.run(["patient"], user_id=UID, senses={"patient": "пациент (роль в клинике)"})
    assert "пациент (роль в клинике)" in captured["user"]      # смысл дошёл до промпта
    assert json.loads(fresh_db.list_pending(UID)[0]["payload"])["ru"] == "пациент"


def test_no_sense_keeps_old_behaviour(fresh_db, monkeypatch):
    captured = {}
    def gen(system, messages):
        captured["user"] = messages[-1]["content"]
        return json.dumps({"word": "menu", "ru": "меню", "scenario": "Universal", "level": "A2"})
    monkeypatch.setattr(llm, "chat", _qa_ok(gen))
    enrich.run(["menu"], user_id=UID)
    assert "Целевой смысл" not in captured["user"]              # без sense — старый промпт


def test_validate_strips_null_root_and_bogus_idea_frame(fresh_db, monkeypatch):
    bogus = json.dumps({"word": "water", "ru": "вода", "root": "null",
                        "dna_idea": "НесуществующаяИдея", "thinking_frame": "НесуществующийФрейм",
                        "scenario": "Universal", "level": "A1"})
    monkeypatch.setattr(llm, "chat", _qa_ok(lambda s, m: bogus))
    enrich.run(["water"], user_id=UID)
    p = json.loads(fresh_db.list_pending(UID)[0]["payload"])
    assert p["root"] is None                       # «null»-строка → None
    assert p["dna_idea"] is None                   # идея не из таксономии → None (не форсим)
    assert p["thinking_frame"] is None             # фрейм не из таксономии → None
