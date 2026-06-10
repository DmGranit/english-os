"""Батч-наполнение: подбор слов под сценарий, принудительный тег сценария, «Подтвердить все»."""
import json

import db, enrich, llm
from conftest import UID


# ---------- suggest_words: подбор кандидатов ----------

def test_suggest_words_filters_existing_and_dups(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k:
                        '["invest", "traction", "Traction", "pivot"]')
    words = enrich.suggest_words("Restaurant", n=10)
    assert words == ["traction", "pivot"]      # invest уже в базе; дубль по регистру отсеян


def test_suggest_words_respects_n(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: '["aa", "bb", "cc", "dd"]')
    assert enrich.suggest_words("Restaurant", n=2) == ["aa", "bb"]


def test_suggest_words_garbage_reply(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "не могу, я заглушка")
    assert enrich.suggest_words("Restaurant", n=5) == []


# ---------- run(scenario=...): принудительный тег сценария ----------

def _fake_payload(word):
    return json.dumps({"word": word, "ru": "перевод", "dna_idea": "Exchange",
                       "scenario": "Universal", "level": "B1",
                       "collocations": ["some collocation"]})


def test_run_overrides_scenario(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda system, messages, **k:
                        _fake_payload(messages[-1]["content"].split()[-1]))
    res = enrich.run(["menu", "waiter"], user_id=UID, scenario="Restaurant")
    assert res["added"] == 2
    items = fresh_db.list_pending(UID)
    assert {i["word"] for i in items} == {"menu", "waiter"}
    for i in items:                            # тег сценария принудительный, не от модели
        assert json.loads(i["payload"])["scenario"] == "Restaurant"


def test_run_without_override_keeps_old_behaviour(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda system, messages, **k: _fake_payload("menu"))
    enrich.run(["menu"], user_id=UID)
    payload = json.loads(fresh_db.list_pending(UID)[0]["payload"])
    assert payload["scenario"] in ("Universal", "Pitching", "Status update", "Negotiating")


# ---------- confirm_all_pending: кнопка «Подтвердить все» ----------

def test_confirm_all_pending(fresh_db):
    fresh_db.add_pending("traction", {"ru": "импульс роста"}, UID)
    fresh_db.add_pending("pivot", {"ru": "разворот"}, UID)
    fresh_db.add_pending("invest", {"ru": "дубль"}, UID)     # уже в content — не дублируется
    before = fresh_db.count_content()
    assert fresh_db.confirm_all_pending(UID) == 3            # обработаны все
    assert fresh_db.count_content() == before + 2            # добавлены только новые
    assert fresh_db.list_pending(UID) == []                  # очередь пуста


def test_confirm_all_pending_empty_queue(fresh_db):
    assert fresh_db.confirm_all_pending(UID) == 0
