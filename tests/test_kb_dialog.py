"""Диалоговые режимы убирают клавиатуру бота с экрана (вернётся с носителем после ИТОГа)."""
import asyncio, types

from telegram import ReplyKeyboardRemove

import bot
from conftest import UID


def _enter(fresh_db, mode):
    captured = {}

    async def out(text, markup=None):
        captured["markup"] = markup
        return None

    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._enter_mode(out, ctx, UID, mode))
    return captured.get("markup")


def test_dialog_modes_remove_keyboard(fresh_db):
    for mode in ("scenario", "flow"):
        assert isinstance(_enter(fresh_db, mode), ReplyKeyboardRemove)


def test_review_empty_keeps_keyboard_untouched(fresh_db):
    # нет слов к повторению — обычное сообщение, клавиатуру не трогаем
    assert _enter(fresh_db, "review") is None


# ---------- раскладка панели: Темы в панели, Итог во всю ширину ----------

def test_main_kb_layout(fresh_db):
    rows = [[b["text"] for b in row] for row in bot.MAIN_KB.to_dict()["keyboard"]]
    assert [bot.BTN_TOPICS, bot.BTN_PROG] in rows         # Темы — в панели, не в /меню
    assert [bot.BTN_END] in rows                          # Итог — во всю ширину
    assert bot.BTN_TOPICS in bot.MAIN_BUTTONS


def test_topics_button_routes_to_axis_menu(fresh_db):
    sent = []

    async def reply_text(text, reply_markup=None):
        sent.append(text)

    update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=reply_text),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._route_button(update, ctx, UID, bot.BTN_TOPICS))
    assert sent and "срез тем" in sent[0]
