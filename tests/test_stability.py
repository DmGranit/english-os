"""Батч «устойчивость»: персистентность сессии, лимит Telegram, гигиена истории."""
import asyncio

import bot


# ---------- (1) персистентность: user_data переживает «рестарт» ----------

def test_user_data_survives_restart(tmp_path):
    path = str(tmp_path / "state.pickle")
    ud = {"mode": "scenario",
          "history": [{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "hello!"}],
          "last_seen": 123.456,
          "review_queue": [1, 2]}

    async def roundtrip():
        p1 = bot._persistence(path)
        await p1.update_user_data(42, ud)
        await p1.flush()                       # «выключили бота»
        p2 = bot._persistence(path)            # «включили заново»
        return await p2.get_user_data()

    data = asyncio.run(roundtrip())
    assert data[42] == ud


# ---------- (2) лимит Telegram 4096: резка длинных сообщений ----------

def test_chunks_short_text_untouched():
    assert bot._chunks("короткий ответ") == ["короткий ответ"]


def test_chunks_long_text_splits_without_loss():
    text = "\n".join(f"строка {i} " + "x" * 90 for i in range(500))   # ~50К символов
    parts = bot._chunks(text)
    assert len(parts) > 1
    assert all(len(p) <= bot.TG_LIMIT for p in parts)
    assert "\n".join(parts) == text            # ничего не потеряли и не переставили


def test_chunks_hard_cuts_single_huge_line():
    text = "x" * 10_000                        # одна строка длиннее лимита
    parts = bot._chunks(text)
    assert all(len(p) <= bot.TG_LIMIT for p in parts)
    assert "".join(parts) == text


# ---------- (3) история не начинается с assistant ----------

def test_remember_trims_and_never_starts_with_assistant():
    hist = []
    bot._remember(hist, "user", "u0")
    for i in range(bot.MAX_HISTORY):           # чередование, переполняющее окно
        bot._remember(hist, "assistant", f"a{i}")
        bot._remember(hist, "user", f"u{i + 1}")
    assert len(hist) <= bot.MAX_HISTORY
    assert hist[0]["role"] == "user"           # контракт: первый ход всегда user
    assert hist[-1] == {"role": "user", "content": f"u{bot.MAX_HISTORY}"}
