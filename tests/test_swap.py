"""Плейсхолдер «Готовлю…» никогда не зависает: сбой edit -> видимый фолбэк + лог."""
import asyncio, types

import bot


class _Ph:
    def __init__(self, fail_edit=False):
        self.fail_edit = fail_edit
        self.edits, self.replies = [], []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        if self.fail_edit:
            raise RuntimeError("Bad Request: can't parse entities")
        self.edits.append((text, parse_mode))
        return self

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))
        return self


def test_swap_in_normal_edits_placeholder():
    ph = _Ph()
    asyncio.run(bot._swap_in(ph, "Готовый ответ."))
    assert ph.edits and "Готовый ответ" in ph.edits[0][0]


def test_swap_in_edit_fails_sends_visible_fallback():
    ph = _Ph(fail_edit=True)
    asyncio.run(bot._swap_in(ph, "Ответ модели"))
    # плейсхолдер не завис: пользователь получил текст новым сообщением
    assert ph.replies and "Ответ модели" in ph.replies[-1][0]


def test_swap_in_total_failure_offers_retry():
    """Даже если и edit, и plain-reply падают — отдаём кнопку «не работает»."""
    class _Dead:
        async def edit_text(self, *a, **k):
            raise RuntimeError("x")

    calls = {"n": 0}

    async def reply_text(text, reply_markup=None, parse_mode=None):
        calls["n"] += 1
        if calls["n"] <= 2:                      # оба контент-reply (HTML, затем плоский) падают
            raise RuntimeError("y")
        calls["last"] = (text, reply_markup)     # третий (аварийный, кнопка ретрая) проходит
        return None

    dead = _Dead()
    dead.reply_text = reply_text
    asyncio.run(bot._swap_in(dead, "что-то"))
    assert "не получил" in calls["last"][0].lower() or "🔄" in str(calls["last"][1])
