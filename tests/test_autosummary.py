"""Авто-ИТОГ по неактивности: выбор «заснувших» сессий и завершение сессии."""
import asyncio

import bot, db, llm
from conftest import UID

REPORT = ('Итог: повторили invest.\n'
          '```json\n{"reviewed": [{"word": "invest", "ok": true}], '
          '"add": [], "errors": []}\n```')


# ---------- _idle_users: кого пора сводить ----------

def test_idle_users_picks_silent_with_history():
    data = {1: {"history": [{"role": "user", "content": "hi"}], "last_seen": 1000.0}}
    assert bot._idle_users(data, now=1000.0 + bot.IDLE_SUMMARY_SEC) == [1]


def test_idle_users_skips_recent():
    data = {1: {"history": [{"role": "user", "content": "hi"}], "last_seen": 1000.0}}
    assert bot._idle_users(data, now=1000.0 + bot.IDLE_SUMMARY_SEC - 1) == []


def test_idle_users_skips_empty_history():
    data = {1: {"history": [], "last_seen": 0.0},
            2: {"last_seen": 0.0}}
    assert bot._idle_users(data, now=10_000_000.0) == []


def test_idle_users_skips_without_last_seen():
    """Нет отметки активности — не считаем заснувшим (now - now = 0)."""
    data = {1: {"history": [{"role": "user", "content": "hi"}]}}
    assert bot._idle_users(data, now=10_000_000.0) == []


# ---------- _finish_session: единая точка завершения ----------

def _run_finish(ud, monkeypatch, reply):
    calls = {"n": 0}

    def fake_chat(system, messages, max_tokens=600, model=None):
        calls["n"] += 1
        return reply

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(db, "backup", lambda: "skipped")   # не плодить файлы в backups/
    sent = []

    async def send(text, reply_markup=None):
        sent.append(text)

    asyncio.run(bot._finish_session(ud, UID, send))
    return sent, calls["n"]


def test_finish_session_applies_and_clears(fresh_db, monkeypatch):
    fresh_db.start_learning([1], UID)                       # invest в box 1
    ud = {"mode": "scenario", "history": [{"role": "user", "content": "invest is great"}]}
    sent, n_llm = _run_finish(ud, monkeypatch, REPORT)
    assert n_llm == 1
    assert ud["history"] == []                              # сессия закрыта
    assert ud["mode"] == "flow"                             # роль снята: дальше — просто разговор
    assert len(sent) == 2                                   # отчёт + носитель клавиатуры
    assert "записано в базу" in sent[0]
    with fresh_db._conn() as c:                             # ИТОГ дошёл до SRS
        box = c.execute("SELECT box FROM state WHERE user_id=? AND word_id=1",
                        (UID,)).fetchone()["box"]
    assert box == 2


def test_finish_session_empty_history_skips_llm(fresh_db, monkeypatch):
    ud = {"mode": "flow", "history": []}
    sent, n_llm = _run_finish(ud, monkeypatch, REPORT)
    assert n_llm == 0                                       # LLM не дёргали впустую
    assert len(sent) == 1 and "нечего" in sent[0].lower()


def test_finish_session_without_json_still_reports(fresh_db, monkeypatch):
    """Модель не отдала машинный блок — пользователь всё равно получает текст, без падения."""
    ud = {"mode": "flow", "history": [{"role": "user", "content": "hello"}]}
    sent, _ = _run_finish(ud, monkeypatch, "Просто текст отчёта без JSON.")
    assert len(sent) == 2                                   # отчёт + носитель клавиатуры
    assert "Просто текст отчёта" in sent[0]
    assert ud["history"] == []
