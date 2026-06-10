"""A7: подсказки Whisper (язык по режиму + слова в работе) и чистка ```-заборов в ИТОГе."""
import types

import bot, llm
from conftest import UID


# ---------- llm: поля multipart-запроса к Whisper ----------

def test_stt_body_includes_language_and_prompt():
    body = llm._stt_body(b"AUDIO", "voice.ogg", "BOUND", language="en", prompt="invest, traction")
    text = body.decode("utf-8", errors="ignore")
    assert 'name="language"\r\n\r\nen' in text
    assert 'name="prompt"\r\n\r\ninvest, traction' in text
    assert b"AUDIO" in body
    assert text.rstrip().endswith("--BOUND--")


def test_stt_body_omits_optional_fields():
    body = llm._stt_body(b"AUDIO", "voice.ogg", "BOUND")
    text = body.decode("utf-8", errors="ignore")
    assert 'name="language"' not in text
    assert 'name="prompt"' not in text
    assert 'name="model"' in text and 'name="file"' in text


# ---------- bot: выбор подсказок по режиму ----------

def test_stt_hints_english_in_conversational_modes(fresh_db):
    fresh_db.start_learning([1, 2], UID)                  # invest, deadline в работе
    for mode in ("scenario", "flow"):
        ctx = types.SimpleNamespace(user_data={"mode": mode})
        lang, focus = bot._stt_hints(ctx, UID)
        assert lang == "en"                               # разговорные режимы — английский
        assert "invest" in focus                          # слова в работе — в подсказку


def test_stt_hints_no_language_outside_conversation(fresh_db):
    ctx = types.SimpleNamespace(user_data={"mode": "new"})
    lang, _ = bot._stt_hints(ctx, UID)
    assert lang is None                                   # «учим …» могут сказать по-русски


def test_stt_hints_empty_focus_is_none(fresh_db):
    ctx = types.SimpleNamespace(user_data={"mode": "flow"})
    _, focus = bot._stt_hints(ctx, UID)
    assert focus is None                                  # нет слов в работе — без prompt


# ---------- чистка ```-заборов в человеческом отчёте ИТОГ ----------

def test_strip_fences_removes_wrapper_lines():
    text = "```\n═══════\nИТОГ СЕССИИ\nОБСУДИЛИ: pay\n═══════\n```"
    clean = bot._strip_fences(text)
    assert "```" not in clean
    assert "ИТОГ СЕССИИ" in clean and "ОБСУДИЛИ: pay" in clean


def test_strip_fences_keeps_normal_text():
    text = "Обычный отчёт без заборов.\nВторая строка."
    assert bot._strip_fences(text) == text
