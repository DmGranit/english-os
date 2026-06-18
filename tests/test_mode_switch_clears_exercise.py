"""Баг, найденный живым смоуком 19.06: брошенное упражнение (gr1_card/gr2_card) и
прочие pending-ответы переживают рестарт (PicklePersistence) и в _process_user_text
проверяются РАНЬШЕ typed_wid — поэтому при REVIEW они перехватывают продуктивный
typed-ответ box3, и `prod` не логируется. Вход в режим обучения должен бросать чужие
pending-состояния."""
import asyncio, types

import bot, db
from conftest import UID


async def _noop(*a, **k):
    pass


def test_enter_review_clears_stale_exercise_state(fresh_db):
    ctx = types.SimpleNamespace(user_data={
        "gr1_card": "Past of go?", "gr2_card": "transform", "warm_wid": 7,
        "act_wid": 9, "awaiting_slot_hours": True,
    })
    asyncio.run(bot._enter_mode(_noop, ctx, UID, "review"))
    for stale in ("gr1_card", "gr2_card", "warm_wid", "act_wid"):
        assert stale not in ctx.user_data, f"{stale} не сброшен при входе в REVIEW"


def test_enter_new_clears_stale_exercise_state(fresh_db):
    ctx = types.SimpleNamespace(user_data={"gr1_card": "x", "gr2_card": "y"})
    asyncio.run(bot._enter_mode(_noop, ctx, UID, "new"))
    assert "gr1_card" not in ctx.user_data and "gr2_card" not in ctx.user_data
