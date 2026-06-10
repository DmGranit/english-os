"""Программа дня: критерии слотов, карта дня, лог сессий, слотовые напоминания."""
import asyncio

import bot, db, llm
from conftest import UID


# ---------- program: настройка ----------

def test_program_default_free(fresh_db):
    assert fresh_db.get_program(UID) == "free"            # нет строки users — дефолт
    fresh_db.set_program(UID, "cycle")
    assert fresh_db.get_program(UID) == "cycle"


# ---------- day_map: три критерия выполнения слотов ----------

def test_day_map_new_slot_by_promoted_today(fresh_db):
    assert fresh_db.day_map(UID)["new"] is False
    fresh_db.start_learning([1], UID)                     # promoted_at = сегодня
    assert fresh_db.day_map(UID)["new"] is True


def test_day_map_review_slot_by_review_today(fresh_db):
    fresh_db.start_learning([1], UID)
    assert fresh_db.day_map(UID)["review"] is False
    fresh_db.review(1, True, UID)                         # запись в reviews за сегодня
    assert fresh_db.day_map(UID)["review"] is True


def test_day_map_scenario_slot_by_session_log(fresh_db):
    fresh_db.log_session(UID, "flow")                     # flow слот НЕ закрывает
    assert fresh_db.day_map(UID)["scenario"] is False
    fresh_db.log_session(UID, "scenario")
    assert fresh_db.day_map(UID)["scenario"] is True


# ---------- карта дня в боте: free / cycle ----------

def test_day_line_free_is_none(fresh_db):
    assert bot._day_line(UID) is None                     # free — карты нет


def test_day_line_cycle_shows_slots_and_hint(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.start_learning([1], UID)                     # утро закрыто
    line = bot._day_line(UID)
    assert "🌅 ✅" in line and "☀️ —" in line and "🎭 —" in line
    assert "Повторить" in line                            # следующий невыполненный слот


def test_next_slot_hint_free_is_none(fresh_db):
    assert bot._next_slot_hint(UID) is None


# ---------- лог сессий пишется при ИТОГе (ручной и авто — одна точка) ----------

def test_finish_session_logs_mode(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Отчёт.")
    monkeypatch.setattr(db, "backup", lambda: None)

    async def send(text, reply_markup=None):
        pass

    ud = {"mode": "scenario", "history": [{"role": "user", "content": "hi"}]}
    asyncio.run(bot._finish_session(ud, UID, send))       # «ручной» путь (кнопка/слово)
    ud2 = {"mode": "flow", "history": [{"role": "user", "content": "hi"}]}
    asyncio.run(bot._finish_session(ud2, UID, send))      # «авто» путь — та же точка
    with fresh_db._conn() as c:
        rows = [r["mode"] for r in c.execute(
            "SELECT mode FROM sessions WHERE user_id=? ORDER BY id", (UID,))]
    assert rows == ["scenario", "flow"]


def test_finish_session_empty_history_not_logged(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "backup", lambda: None)

    async def send(text, reply_markup=None):
        pass

    asyncio.run(bot._finish_session({"mode": "flow", "history": []}, UID, send))
    with fresh_db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (UID,)).fetchone()[0]
    assert n == 0                                         # пустая сессия — не сессия


# ---------- слотовые напоминания: молчат по закрытому, приходят по открытому ----------

def test_slot_reminder_fires_when_pending(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.start_learning([1], UID)                     # due сегодня, повторений ещё нет
    text = bot._slot_reminder_text(UID, "review")
    assert text and "1" in text                           # «N слов ждут»


def test_slot_reminder_silent_when_done(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID)                         # слот закрыт заранее
    assert bot._slot_reminder_text(UID, "review") is None


def test_slot_reminder_scenario(fresh_db):
    fresh_db.set_program(UID, "cycle")
    assert "🎭" in bot._slot_reminder_text(UID, "scenario")
    fresh_db.log_session(UID, "scenario")
    assert bot._slot_reminder_text(UID, "scenario") is None


# ---------- free-режим напоминаний не затронут; cycle уходит в слоты ----------

def test_free_reminders_untouched_and_exclude_cycle(fresh_db):
    fresh_db.set_reminder(UID, 10)
    assert UID in fresh_db.reminder_users(10)             # free — как раньше
    fresh_db.set_program(UID, "cycle")
    assert UID not in fresh_db.reminder_users(10)         # cycle — только слотовые


def test_slot_users_by_minute(fresh_db):
    fresh_db.set_program(UID, "cycle")                    # дефолты 9:00/14:00/19:00 в минутах
    assert (UID, "new") in fresh_db.slot_users(540)
    assert (UID, "review") in fresh_db.slot_users(840)
    assert (UID, "scenario") in fresh_db.slot_users(1140)
    assert fresh_db.slot_users(480) == []
    fresh_db.set_slot_time(UID, "morning", 480)           # 8:00
    assert (UID, "new") in fresh_db.slot_users(480)
