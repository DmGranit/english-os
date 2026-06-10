"""Слой Б: ручной руль сложности — ⬆️ Сложнее / ⬇️ Проще двигают скрытую полосу."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- db.nudge_band ----------

def test_nudge_up_and_down(fresh_db):
    fresh_db.set_band(UID, "A2")
    band, moved = fresh_db.nudge_band(UID, +1)
    assert band == "B1" and moved
    band, moved = fresh_db.nudge_band(UID, +1)
    assert band == "B2" and moved
    band, moved = fresh_db.nudge_band(UID, +1)
    assert band == "B2" and not moved          # потолок — не падаем


def test_nudge_down_floor(fresh_db):
    fresh_db.set_band(UID, "A2")
    band, moved = fresh_db.nudge_band(UID, -1)
    assert band == "A2" and not moved          # пол — не ниже


# ---------- кнопки/команды ----------

def test_difficulty_kb_has_both():
    cbs = [b["callback_data"] for row in bot._difficulty_kb().to_dict()["inline_keyboard"]
           for b in row]
    assert "diff:up" in cbs and "diff:down" in cbs


def test_on_difficulty_moves_band(fresh_db):
    fresh_db.set_band(UID, "A2")
    edits = []

    async def edit_message_text(text, reply_markup=None, parse_mode=None):
        edits.append(text)

    async def answer(*a, **k):
        pass

    q = types.SimpleNamespace(data="diff:up", edit_message_text=edit_message_text, answer=answer)
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot.on_difficulty(update, ctx))
    assert fresh_db.get_band(UID) == "B1"      # полоса поднялась
    assert edits and ("сложнее" in edits[0].lower() or "💪" in edits[0])


def test_on_difficulty_at_ceiling_message(fresh_db):
    fresh_db.set_band(UID, "B2")
    edits = []

    async def edit_message_text(text, reply_markup=None, parse_mode=None):
        edits.append(text)

    async def answer(*a, **k):
        pass

    q = types.SimpleNamespace(data="diff:up", edit_message_text=edit_message_text, answer=answer)
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_difficulty(update, types.SimpleNamespace(user_data={})))
    assert edits and "максим" in edits[0].lower()   # уже на максимуме
