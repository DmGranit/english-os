"""Контракт ИТОГ: парсинг машинного JSON-блока и токен-лимит финального вызова."""
import asyncio, types

import bot, llm
from conftest import UID

LAST = '{"reviewed": [{"word": "invest", "ok": true}], "add": [], "errors": []}'


def test_extract_summary_single_block():
    text = f"Отчёт сессии.\n```json\n{LAST}\n```"
    data, clean = bot._extract_summary(text)
    assert data["reviewed"][0]["word"] == "invest"
    assert clean == "Отчёт сессии."


def test_extract_summary_takes_last_json_block():
    """Модель может привести JSON-пример раньше по тексту — машинный блок всегда последний."""
    text = ("Формат, как договаривались:\n"
            '```json\n{"reviewed": [{"word": "example", "ok": false}]}\n```\n'
            "А вот итог занятия.\n"
            f"```json\n{LAST}\n```")
    data, clean = bot._extract_summary(text)
    assert data["reviewed"][0]["word"] == "invest"          # последний блок, не первый
    assert "example" in clean                                # пример остался в тексте
    assert "invest" not in clean                             # машинный блок вырезан


def test_extract_summary_no_block():
    data, clean = bot._extract_summary("Просто текст без JSON.")
    assert data is None
    assert clean == "Просто текст без JSON."


def test_extract_summary_broken_json():
    data, clean = bot._extract_summary("Отчёт.\n```json\n{сломано\n```")
    assert data is None
    assert clean == "Отчёт.\n```json\n{сломано\n```"


def test_call_with_summary_raises_token_limit(fresh_db, monkeypatch):
    """ИТОГ = человеческий отчёт + JSON: дефолтных 600 токенов мало, JSON обрезается."""
    captured = {}

    def fake_chat(system, messages, max_tokens=600, model=None):
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._call(ctx, "flow", UID, "Закончили", with_summary=True))
    assert captured["max_tokens"] >= 1500


def test_call_without_summary_keeps_default(fresh_db, monkeypatch):
    captured = {}

    def fake_chat(system, messages, max_tokens=600, model=None):
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._call(ctx, "flow", UID, "hello"))
    assert captured["max_tokens"] == 600
