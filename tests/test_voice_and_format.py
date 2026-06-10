"""A7: подсказки Whisper (язык по режиму + слова в работе) и чистка ```-заборов в ИТОГе."""
import asyncio, types

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


def test_stt_hints_english_is_global_default(fresh_db):
    """Грузинский кейс: вне колоды и вне разговорных режимов короткий клип
    галлюцинировал — теперь en по умолчанию ВЕЗДЕ (продукт про английскую речь)."""
    for mode in ("new", "review"):
        ctx = types.SimpleNamespace(user_data={"mode": mode})
        lang, _ = bot._stt_hints(ctx, UID)
        assert lang == "en"


def test_stt_hints_empty_focus_is_none(fresh_db):
    ctx = types.SimpleNamespace(user_data={"mode": "flow"})
    _, focus = bot._stt_hints(ctx, UID)
    assert focus is None                                  # нет слов в работе — без prompt


# ---------- голос во время колоды: язык карточки + страховка от галлюцинаций ----------

def _deck_ctx(fresh_db, box):
    import time as _t
    return types.SimpleNamespace(user_data={
        "review_queue": [1], "review_box": {1: box}, "review_pos": 0,
        "card_shown_at": _t.time()})


def test_stt_hints_deck_box1_expects_russian(fresh_db):
    ctx = _deck_ctx(fresh_db, 1)
    lang, prompt = bot._stt_hints(ctx, UID)
    assert lang == "ru"                                  # ответ box1 — перевод по-русски
    assert "инвестировать" in prompt                     # ожидаемое слово — в подсказку


def test_stt_hints_deck_box3_expects_english(fresh_db):
    ctx = _deck_ctx(fresh_db, 3)
    lang, prompt = bot._stt_hints(ctx, UID)
    assert lang == "en"
    assert "invest" in prompt


def test_voice_hallucination_guard_on_deck(fresh_db, monkeypatch):
    """Карточка ждёт слово, Whisper принёс тираду (валлийский кейс) — «не расслышал»."""
    monkeypatch.setattr(llm, "transcribe",
                        lambda *a, **k: "Diolch yn fawr iawn am wylio'r fideo diolch")
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)

    async def noop(*a, **k):
        pass

    voice = types.SimpleNamespace(duration=1, get_file=noop)

    class _File:
        async def download_as_bytearray(self):
            return bytearray(b"x")

    async def get_file():
        return _File()

    voice.get_file = get_file
    message = types.SimpleNamespace(reply_text=reply_text, voice=voice,
                                    chat=types.SimpleNamespace(send_action=noop))
    update = types.SimpleNamespace(message=message,
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = _deck_ctx(fresh_db, 1)
    asyncio.run(bot.on_voice(update, ctx))
    assert any("не расслышал" in s.lower() for s in sent)
    assert not any("Ты написал" in s for s in sent)      # мусор не стал «попыткой»


# ---------- чистка ```-заборов в человеческом отчёте ИТОГ ----------

def test_strip_fences_removes_wrapper_lines():
    text = "```\n═══════\nИТОГ СЕССИИ\nОБСУДИЛИ: pay\n═══════\n```"
    clean = bot._strip_fences(text)
    assert "```" not in clean
    assert "ИТОГ СЕССИИ" in clean and "ОБСУДИЛИ: pay" in clean


def test_strip_fences_keeps_normal_text():
    text = "Обычный отчёт без заборов.\nВторая строка."
    assert bot._strip_fences(text) == text
