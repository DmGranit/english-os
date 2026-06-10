"""Учебный v2, часть 2: ленивый сбор контекста ученика -> users.goal -> в промпты."""
import asyncio, types

import bot, db, llm
from conftest import UID


# ---------- хранение цели/контекста ----------

def test_goal_set_and_get(fresh_db):
    fresh_db.ensure_user_state(UID)
    assert fresh_db.get_goal(UID) is None
    fresh_db.set_goal(UID, "маркетолог B2B SaaS, готовится к переговорам с US-клиентом")
    assert "маркетолог" in fresh_db.get_goal(UID)


def test_goal_in_learner_profile(fresh_db):
    fresh_db.ensure_user_state(UID)
    fresh_db.set_goal(UID, "врач, общение с иностранными пациентами")
    prof = fresh_db.learner_profile(UID)
    assert "врач" in prof                              # контекст уходит в каждый промпт


def test_no_goal_no_noise(fresh_db):
    fresh_db.ensure_user_state(UID)
    prof = fresh_db.learner_profile(UID)
    assert "контекст" not in prof.lower()              # пустая цель не мусорит промпт


# ---------- извлечение контекста из свободного разговора ----------

def test_maybe_capture_context_extracts_and_saves(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: '{"capture": true, "summary": "продакт-менеджер в финтехе"}')
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._maybe_capture_context(ctx, UID, "I work as a product manager in fintech"))
    assert "продакт-менеджер" in fresh_db.get_goal(UID)
    assert ctx.user_data.get("ctx_checked") is True    # один раз за сессию


def test_capture_skips_when_nothing_personal(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: '{"capture": false}')
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._maybe_capture_context(ctx, UID, "The weather is nice today"))
    assert fresh_db.get_goal(UID) is None


def test_capture_runs_once_per_session(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    calls = []
    monkeypatch.setattr(llm, "chat",
                        lambda *a, **k: calls.append(1) or '{"capture": false}')
    ctx = types.SimpleNamespace(user_data={"ctx_checked": True})   # уже проверяли
    asyncio.run(bot._maybe_capture_context(ctx, UID, "I am a designer"))
    assert calls == []                                 # повторно LLM не дёргаем


def test_capture_skips_if_goal_already_known(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    fresh_db.set_goal(UID, "учитель")
    calls = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: calls.append(1) or "{}")
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._maybe_capture_context(ctx, UID, "I am an engineer"))
    assert calls == []                                 # цель есть — не переспрашиваем
