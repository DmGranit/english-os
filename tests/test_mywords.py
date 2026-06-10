"""Видимость: постоянная клавиатура + 📒 Мои слова (список того, что учу)."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- постоянная клавиатура (не прячется после нажатия) ----------

def test_main_kb_persistent_not_one_time():
    kb = bot.MAIN_KB.to_dict()
    assert kb.get("is_persistent") is True
    assert not kb.get("one_time_keyboard")          # кнопки видны всегда


# ---------- db: список слов в работе ----------

def test_my_words_lists_learning(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    fresh_db.review(1, True, UID)
    fresh_db.review(1, True, UID)
    fresh_db.review(1, True, UID)                    # invest -> box 3 (зреет/освоено)
    data = db.my_words(UID)
    learning = {w["word"] for w in data["learning"]}
    assert "deadline" in learning                    # ещё в работе
    assert data["mastered_n"] == 1                   # invest освоен
    assert data["new_n"] >= 0


def test_my_words_empty_user(fresh_db):
    data = db.my_words(UID)
    assert data["learning"] == [] and data["mastered_n"] == 0


# ---------- команда /mywords ----------

def test_mywords_cmd_shows_list(fresh_db):
    fresh_db.start_learning([1, 2], UID)
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)
        return types.SimpleNamespace()

    update = types.SimpleNamespace(
        message=types.SimpleNamespace(reply_text=reply_text),
        effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot.mywords_cmd(update, ctx))
    body = " ".join(sent)
    assert "invest" in body and "deadline" in body   # слова показаны
    assert "учишь" in body.lower() or "в работе" in body.lower()
