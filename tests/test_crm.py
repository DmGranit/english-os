"""Батч CRM + обратная связь: имена, карточки людей, голосовой фидбек, оценки."""
import asyncio, types, datetime

import bot, db, llm
from conftest import UID


# ---------- имена сохраняются при каждом заходе ----------

def test_touch_user_saves_name(fresh_db):
    fresh_db.ensure_user_state(UID)
    db.touch_user(UID, "Дмитрий Г (@DmGranit, id 6634)")
    assert "DmGranit" in db.get_name(UID)
    db.touch_user(UID, "Дмитрий Гранит (@DmGranit)")     # обновляется, last_seen двигается
    assert "Гранит" in db.get_name(UID)


def test_touch_does_not_clobber_role(fresh_db):
    db.set_role(UID, "owner")
    db.touch_user(UID, "Имя")
    assert db.get_role(UID) == "owner"                   # имя не сбивает роль


# ---------- CRM-строки ----------

def test_crm_rows_have_name_activity_context(fresh_db):
    db.set_role(UID, "approved")
    db.touch_user(UID, "Жена (@wife)")
    db.set_goal(UID, "маркетолог")
    db.log_session(UID, "flow")
    rows = {r["user_id"]: r for r in db.crm_rows()}
    r = rows[UID]
    assert "wife" in r["name"] and r["sessions"] == 1
    assert r["goal"] == "маркетолог" and r["last_seen"]


# ---------- голосовой /feedback ----------

def test_voice_feedback_mode_captures_text(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_ID", 999)
    sent, notified = [], []

    async def reply_text(t, reply_markup=None, parse_mode=None):
        sent.append(t)

    async def send_message(cid, t, **k):
        notified.append((cid, t))

    msg = types.SimpleNamespace(reply_text=reply_text, voice=None,
                                chat=types.SimpleNamespace(send_action=_noop))
    update = types.SimpleNamespace(message=msg,
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={"await_feedback": True},
                                bot=types.SimpleNamespace(send_message=send_message))
    asyncio.run(bot._process_user_text(update, ctx, UID, "карточки мелковаты"))
    assert db.recent_tech(kind="feedback")[0]["summary"] == "карточки мелковаты"
    assert notified and notified[0][0] == 999            # владельцу ушло
    assert "await_feedback" not in ctx.user_data


async def _noop(*a, **k): pass


# ---------- оценка ответа 👍/👎 ----------

def test_rate_kb_and_logging(fresh_db):
    kb = bot._rate_kb("msgkey").to_dict()["inline_keyboard"]
    cbs = [b["callback_data"] for row in kb for b in row]
    assert cbs == ["rate:up:msgkey", "rate:down:msgkey"]


def test_on_rate_logs_signal(fresh_db):
    q = types.SimpleNamespace(data="rate:down:x", _ans=[])

    async def answer(t=None, **k):
        q._ans.append(t)

    async def edit_reply_markup(reply_markup=None):
        pass

    q.answer = answer
    q.edit_message_reply_markup = edit_reply_markup
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot.on_rate(update, ctx))
    rows = db.recent_tech(kind="rating")
    assert rows and "down" in rows[0]["summary"]
