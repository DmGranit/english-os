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
