"""Батч надёжности: журнал ошибок, фидбек, селфчек, лимиты трат."""
import asyncio, types

import bot, db, llm, selfcheck
from conftest import UID


# ---------- журнал tech_errors ----------

def test_log_and_recent_tech(fresh_db):
    fresh_db.log_tech(UID, "error", "boom", "Traceback ...")
    fresh_db.log_tech(UID, "feedback", "карточки повторяются")
    rows = fresh_db.recent_tech()
    assert len(rows) == 2
    assert rows[0]["kind"] == "feedback"            # свежие первыми
    assert fresh_db.tech_count_24h() == 2


# ---------- лимитер LLM-вызовов ----------

def test_rate_ok_allows_under_cap_and_blocks_at_cap():
    ud = {}
    for _ in range(bot.LLM_HOURLY_CAP):
        assert bot._rate_ok(ud, now=1000.0) is True
    assert bot._rate_ok(ud, now=1000.0) is False    # лимит исчерпан


def test_rate_ok_window_expires():
    ud = {}
    for _ in range(bot.LLM_HOURLY_CAP):
        bot._rate_ok(ud, now=1000.0)
    assert bot._rate_ok(ud, now=1000.0 + 3601) is True   # час прошёл — снова можно


# ---------- селфчек ----------

def test_selfcheck_all_green(fresh_db):
    results = selfcheck.checks()
    assert selfcheck.ok(results) is True
    names = {r[0] for r in results}
    assert {"db", "prompt", "pending", "errors24h"} <= names


# ---------- ограничения входа ----------

def _msg_env(text=""):
    sent = []

    async def reply_text(t, reply_markup=None):
        sent.append(t)

    chat = types.SimpleNamespace(send_action=_noop)
    message = types.SimpleNamespace(reply_text=reply_text, text=text, chat=chat, voice=None)
    update = types.SimpleNamespace(message=message,
                                   effective_user=types.SimpleNamespace(id=UID),
                                   effective_message=message)
    return update, sent


async def _noop(*a, **k):
    pass


def test_overlong_text_rejected_before_llm(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: called.append(1) or "x")
    update, sent = _msg_env()
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._process_user_text(update, ctx, UID, "x" * 3000))
    assert called == []                              # до модели не дошло
    assert sent and "длинн" in sent[0].lower()


def test_overlong_voice_rejected(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "transcribe", lambda *a, **k: called.append(1) or "")
    update, sent = _msg_env()
    update.message.voice = types.SimpleNamespace(duration=bot.VOICE_MAX_SEC + 10)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot.on_voice(update, ctx))
    assert called == []                              # Whisper не дёргался
    assert sent and "голосов" in sent[0].lower()


# ---------- /feedback ----------

def test_feedback_saved_and_owner_notified(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_ID", 424242)     # владелец — другой человек
    notified = []

    async def send_message(chat_id, text, **kw):
        notified.append((chat_id, text))

    update, sent = _msg_env()
    ctx = types.SimpleNamespace(args=["карточки", "повторяются"],
                                user_data={},
                                bot=types.SimpleNamespace(send_message=send_message))
    asyncio.run(bot.feedback_cmd(update, ctx))
    assert fresh_db.recent_tech(kind="feedback")[0]["summary"] == "карточки повторяются"
    assert notified and notified[0][0] == 424242
    assert sent and "Спасибо" in sent[0]


# ---------- глобальный обработчик ошибок ----------

def test_on_error_logs_notifies_and_softens(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_ID", 424242)
    notified = []

    async def send_message(chat_id, text, **kw):
        notified.append((chat_id, text))

    update, sent = _msg_env()
    try:
        raise ValueError("kaboom")
    except ValueError as e:
        err = e
    ctx = types.SimpleNamespace(error=err,
                                bot=types.SimpleNamespace(send_message=send_message))
    asyncio.run(bot.on_error(update, ctx))
    rows = fresh_db.recent_tech(kind="error")
    assert rows and "kaboom" in rows[0]["summary"]   # в журнал записано
    assert notified and "kaboom" in notified[0][1]   # владельцу — карточка
    assert sent and "пошло не так" in sent[0]        # пользователю — мягко
