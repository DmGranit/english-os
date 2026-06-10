"""Гибрид клавиатуры: placeholder в строке ввода + кнопки в момент завершения занятия."""
import asyncio, types

import bot, db, llm
from conftest import UID

REPORT = 'Отчёт.\n```json\n{"reviewed": [], "add": [], "errors": []}\n```'


def test_main_kb_has_placeholder():
    kb = bot.MAIN_KB.to_dict()
    assert kb.get("one_time_keyboard") is True       # сворачивается в ⌨ (persistent застревал)
    assert kb.get("input_field_placeholder")


def test_next_action_text_free_and_cycle(fresh_db):
    assert bot._next_action_text(UID) == "Что дальше? 👇"       # free — нейтральный носитель
    fresh_db.set_program(UID, "cycle")
    assert "Дальше по программе" in bot._next_action_text(UID)  # cycle — подсказка слота


def _run_finish(ud, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: REPORT)
    monkeypatch.setattr(db, "backup", lambda: None)
    sent = []

    async def send(text, reply_markup=None):
        sent.append((text, reply_markup))

    asyncio.run(bot._finish_session(ud, UID, send))
    return sent


def test_finish_session_sends_keyboard_carrier_free(fresh_db, monkeypatch):
    ud = {"mode": "flow", "history": [{"role": "user", "content": "hi"}]}
    sent = _run_finish(ud, monkeypatch)
    assert len(sent) == 2                                       # отчёт + носитель клавиатуры
    assert sent[-1][0] == "Что дальше? 👇"
    assert sent[-1][1] is bot.MAIN_KB
    assert sent[0][1] is None                                   # отчёт — без клавиатуры


def test_finish_session_sends_slot_hint_carrier_cycle(fresh_db, monkeypatch):
    fresh_db.set_program(UID, "cycle")
    ud = {"mode": "scenario", "history": [{"role": "user", "content": "hi"}]}
    sent = _run_finish(ud, monkeypatch)
    assert "Дальше по программе" in sent[-1][0]                 # подсказка — носитель кнопок
    assert sent[-1][1] is bot.MAIN_KB
    assert "Дальше по программе" not in sent[0][0]              # из хвоста отчёта ушла


def test_finish_review_sends_keyboard_carrier(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    edits, replies = [], []

    async def edit(text, reply_markup=None):
        edits.append(text)

    async def reply(text, reply_markup=None):
        replies.append((text, reply_markup))

    q = types.SimpleNamespace(edit_message_text=edit,
                              message=types.SimpleNamespace(reply_text=reply))
    ctx = types.SimpleNamespace(user_data={"review_ok": 2, "review_fail": 1})
    asyncio.run(bot._finish_review(q, ctx, UID))
    assert edits and "Повторение завершено" in edits[0]
    assert replies and replies[0][1] is bot.MAIN_KB             # носитель с клавиатурой


def test_finish_session_empty_history_no_carrier(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)
    sent = []

    async def send(text, reply_markup=None):
        sent.append(text)

    asyncio.run(bot._finish_session({"mode": "flow", "history": []}, UID, send))
    assert len(sent) == 1                                       # подсказка без носителя
