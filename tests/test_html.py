"""HTML-оформление: конвертация markdown модели в Telegram-HTML + безопасный фолбэк."""
import asyncio, types

import bot
from conftest import UID


# ---------- конвертер ----------

def test_bold_and_code_converted():
    assert bot._to_html("Focus on **learning** and `invest`.") == \
        "Focus on <b>learning</b> and <code>invest</code>."


def test_html_chars_escaped():
    # «учим <слово>» и & не должны ломать разметку
    assert bot._to_html("учим <слово> & ещё") == "учим &lt;слово&gt; &amp; ещё"


def test_headers_become_bold():
    assert bot._to_html("### Now, create a sentence") == "<b>Now, create a sentence</b>"


def test_bold_not_matched_across_lines():
    text = "a **строка1\nстрока2** b"            # перенос строки — не жирный
    assert "<b>" not in bot._to_html(text)


def test_plain_text_untouched():
    assert bot._to_html("Обычный текст 🎭 без разметки.") == "Обычный текст 🎭 без разметки."


# ---------- отправка: HTML с фолбэком в плоский текст ----------

def test_say_sends_html(fresh_db):
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append((text, parse_mode))

    msg = types.SimpleNamespace(reply_text=reply_text)
    asyncio.run(bot._say(msg, "Focus on **learning**."))
    assert sent == [("Focus on <b>learning</b>.", "HTML")]


def test_say_falls_back_to_plain_on_bad_html(fresh_db):
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        if parse_mode == "HTML":
            raise RuntimeError("Bad Request: can't parse entities")
        sent.append((text, parse_mode))

    msg = types.SimpleNamespace(reply_text=reply_text)
    asyncio.run(bot._say(msg, "битая **разметка"))
    assert sent == [("битая **разметка", None)]    # пользователь получил текст, не ошибку
