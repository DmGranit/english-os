"""Учебный v2, часть 3: активация — закрепить новое слово фразой сразу (выбор канала)."""
import asyncio, types

import bot, db, llm
from conftest import UID


# ---------- кнопки активации после урока ----------

def test_activation_kb_offers_channels(fresh_db):
    kb = bot._activation_kb(1).to_dict()["inline_keyboard"]
    cbs = [b["callback_data"] for row in kb for b in row]
    assert "act:say:1" in cbs and "act:write:1" in cbs   # голос и текст
    assert any(c.startswith("act:skip") for c in cbs)    # «потом» без вины


# ---------- старт активации: бот ждёт фразу ----------

class _Q:
    def __init__(self, data):
        self.data = data
        self.msgs = []
        self.message = types.SimpleNamespace(reply_text=self._r)

    async def answer(self, *a, **k): pass
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.msgs.append(text)
    async def _r(self, text, reply_markup=None, parse_mode=None):
        self.msgs.append(text); return self.message


def test_act_say_sets_awaiting_with_word(fresh_db):
    ctx = types.SimpleNamespace(user_data={})
    q = _Q("act:say:1")
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_activation(update, ctx))
    assert ctx.user_data["act_wid"] == 1                 # ждём фразу с этим словом
    assert any("invest" in m for m in q.msgs)


def test_act_skip_clears(fresh_db):
    ctx = types.SimpleNamespace(user_data={"act_wid": 1})
    q = _Q("act:skip:1")
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_activation(update, ctx))
    assert "act_wid" not in ctx.user_data


# ---------- проверка фразы: мгновенный фидбек + награда ----------

def _env():
    sent = []
    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text); return types.SimpleNamespace()
    chat = types.SimpleNamespace(send_action=_noop)
    message = types.SimpleNamespace(reply_text=reply_text, chat=chat, voice=None)
    update = types.SimpleNamespace(message=message,
                                   effective_user=types.SimpleNamespace(id=UID))
    return update, sent


async def _noop(*a, **k): pass


def test_phrase_with_word_gets_feedback_and_reward(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: "✅ Звучит естественно!\n💬 Natural: We should invest more.")
    update, sent = _env()
    ctx = types.SimpleNamespace(user_data={"act_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, "We need invest in ads"))
    assert any("invest" in s for s in sent)
    assert any("🌱" in s or "закрепляет" in s for s in sent)   # смысловая награда
    assert "act_wid" not in ctx.user_data                      # активация завершена


def test_phrase_without_word_asks_again(fresh_db, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: called.append(1) or "x")
    update, sent = _env()
    ctx = types.SimpleNamespace(user_data={"act_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, "The weather is fine"))
    assert called == []                                  # слова нет — LLM не тратим
    assert ctx.user_data.get("act_wid") == 1             # ждём фразу ещё раз
    assert any("invest" in s for s in sent)


def test_activation_logged_as_format(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    fresh_db.start_learning([1], UID)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "✅ ok")
    update, sent = _env()
    ctx = types.SimpleNamespace(user_data={"act_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, "I invest now"))
    rows = fresh_db.recent_tech(kind="activation")
    assert rows and "invest" in rows[0]["summary"]       # активация попала в журнал
