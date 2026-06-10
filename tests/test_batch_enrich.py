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


def _qa_aware(generate):
    """Фейк llm.chat: QA-проходу отвечает «ok», генерации — переданной функцией."""
    def chat(system, messages, **k):
        if "рецензент" in system:
            return '{"ok": true, "drop_root": false, "reason": "ok"}'
        return generate(system, messages)
    return chat


def test_run_overrides_scenario(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", _qa_aware(
        lambda system, messages: _fake_payload(messages[-1]["content"].split()[-1])))
    res = enrich.run(["menu", "waiter"], user_id=UID, scenario="Restaurant")
    assert res["added"] == 2
    items = fresh_db.list_pending(UID)
    assert {i["word"] for i in items} == {"menu", "waiter"}
    for i in items:                            # тег сценария принудительный, не от модели
        assert json.loads(i["payload"])["scenario"] == "Restaurant"


def test_run_without_override_keeps_old_behaviour(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", _qa_aware(lambda system, messages: _fake_payload("menu")))
    enrich.run(["menu"], user_id=UID)
    payload = json.loads(fresh_db.list_pending(UID)[0]["payload"])
    assert payload["scenario"] in ("Universal", "Pitching", "Status update", "Negotiating")


# ---------- QA-шаг («стерео»): второй ИИ-проход перед очередью ----------

def _chat_seq(replies):
    """llm.chat-мок, отдающий ответы по очереди (1-й вызов — генерация, 2-й — QA)."""
    it = iter(replies)

    def chat(*a, **k):
        return next(it)

    return chat


def test_qa_ok_passes_to_pending(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", _chat_seq([
        _fake_payload("traction"),
        '{"ok": true, "drop_root": false, "reason": "всё верно"}',
    ]))
    res = enrich.run(["traction"], user_id=UID)
    assert res["added"] == 1 and res["failed"] == 0
    assert fresh_db.list_pending(UID)[0]["word"] == "traction"


def test_qa_reject_blocks_word(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", _chat_seq([
        _fake_payload("traction"),
        '{"ok": false, "reason": "перевод неверен"}',
    ]))
    res = enrich.run(["traction"], user_id=UID)
    assert res["added"] == 0 and res["failed"] == 1
    assert fresh_db.list_pending(UID) == []


def test_qa_drop_root_strips_invented_latin(fresh_db, monkeypatch):
    payload = json.loads(_fake_payload("traction"))
    payload["root"] = "tract (выдумка)"
    monkeypatch.setattr(llm, "chat", _chat_seq([
        json.dumps(payload),
        '{"ok": true, "drop_root": true, "reason": "корень сомнителен"}',
    ]))
    enrich.run(["traction"], user_id=UID)
    saved = json.loads(fresh_db.list_pending(UID)[0]["payload"])
    assert saved["root"] is None                     # латынь вычищена, слово сохранено


def test_qa_garbage_verdict_blocks(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", _chat_seq([
        _fake_payload("traction"),
        "я не настроен сегодня проверять",
    ]))
    res = enrich.run(["traction"], user_id=UID)
    assert res["failed"] == 1                        # нет вердикта — не пускаем


# ---------- уведомление владельцу после пакетного наполнения ----------

def test_owner_notified_when_words_added(fresh_db, monkeypatch):
    sent = []
    monkeypatch.setattr(enrich, "_notify_owner", lambda text: sent.append(text))
    monkeypatch.setattr(llm, "chat", _qa_aware(lambda s, m: _fake_payload("menu")))
    enrich.run(["menu"], user_id=UID)
    assert sent and "+1" in sent[0] and "/pending" in sent[0]


def test_owner_not_notified_when_nothing_added(fresh_db, monkeypatch):
    sent = []
    monkeypatch.setattr(enrich, "_notify_owner", lambda text: sent.append(text))
    enrich.run(["invest"], user_id=UID)              # дубль — added=0
    assert sent == []


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
