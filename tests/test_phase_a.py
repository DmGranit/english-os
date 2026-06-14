"""Фаза A (v2.4): ИТОГ не разгоняет SRS (A1.1), дата сессии (A1.2),
сценарий не закрывает слот NEW (A1.3), роль в system (A2.1),
review-фолбэк в flow (A2.3), мягкий Лейтнер (A3.1)."""
import asyncio, datetime, types

import bot, db, prompts
from conftest import UID


def _today_plus(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _state(db_, wid):
    with db_._conn() as c:
        return dict(c.execute("SELECT * FROM state WHERE user_id=? AND word_id=?",
                              (UID, wid)).fetchone())


def _age_promotion(db_, wid, days_ago=3):
    """Сдвинуть promoted_at в прошлое — слово перестаёт быть «введённым сегодня»."""
    past = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    with db_._conn() as c:
        c.execute("UPDATE state SET promoted_at=? WHERE user_id=? AND word_id=?",
                  (past, UID, wid))


# ---------- A1.1: reviewed[] из ИТОГа двигает SRS осторожно ----------

def test_itog_skips_word_promoted_today(fresh_db):
    fresh_db.start_learning([1], UID)                       # введено сегодня, box 1
    r = fresh_db.apply_session_summary(
        {"reviewed": [{"word": "invest", "ok": True}]}, UID)
    assert r["skipped"] == 1 and r["ok"] == 0               # не двигали
    assert _state(fresh_db, 1)["box"] == 1                  # box не прыгнул без интервала


def test_itog_moves_older_word_and_logs_itog_type(fresh_db):
    fresh_db.start_learning([1], UID)
    _age_promotion(fresh_db, 1)                             # введено не сегодня
    r = fresh_db.apply_session_summary(
        {"reviewed": [{"word": "invest", "ok": True}]}, UID)
    assert r["ok"] == 1
    assert _state(fresh_db, 1)["box"] == 2                  # ровно +1
    with fresh_db._conn() as c:
        row = c.execute("""SELECT card_type FROM reviews WHERE user_id=? AND word_id=1
                           ORDER BY id DESC LIMIT 1""", (UID,)).fetchone()
    assert row["card_type"] == "itog"                       # отделимо в A/B


def test_itog_moves_word_at_most_once_per_day(fresh_db):
    fresh_db.start_learning([1], UID)
    _age_promotion(fresh_db, 1)
    fresh_db.apply_session_summary({"reviewed": [{"word": "invest", "ok": True}]}, UID)
    r2 = fresh_db.apply_session_summary(                    # второй ИТОГ в тот же день
        {"reviewed": [{"word": "invest", "ok": True}]}, UID)
    assert r2["skipped"] == 1 and r2["ok"] == 0
    assert _state(fresh_db, 1)["box"] == 2                  # не разогнался до 3


def test_itog_deck_review_today_does_not_block(fresh_db):
    """Блокируется повтор ИТОГ→ИТОГ, а не колода→ИТОГ: card_type='itog' — отдельный счёт.
    (Колода и разговор — разные проверки; защита — от двойного ИТОГ-учёта.)"""
    fresh_db.start_learning([1], UID)
    _age_promotion(fresh_db, 1)
    fresh_db.review(1, True, UID, card_type="self")         # колода сегодня: box 2
    r = fresh_db.apply_session_summary(
        {"reviewed": [{"word": "invest", "ok": True}]}, UID)
    assert r["ok"] == 1                                     # ИТОГ ещё не двигал — можно
    assert _state(fresh_db, 1)["box"] == 3


# ---------- A1.2: реальная дата сессии в ИТОГ ----------

def test_end_prompt_contains_today():
    assert datetime.date.today().isoformat() in bot._end_prompt()
    assert "<КОНЕЦ СЕССИИ>" in bot._end_prompt()


# ---------- A1.3: сценарные слова не закрывают слот NEW ----------

def test_scenario_words_do_not_close_new_slot(fresh_db):
    added = fresh_db.start_learning([1], UID, via="scenario")
    assert added == [1]                                     # слово введено в SRS
    assert fresh_db.day_map(UID)["new"] is False            # но слот NEW не закрыт


def test_direct_words_close_new_slot(fresh_db):
    fresh_db.start_learning([2], UID)                       # дефолт via='direct'
    assert fresh_db.day_map(UID)["new"] is True


def test_promote_new_alone_does_not_close_new_slot(fresh_db):
    # B-enc1: promote_new вводит слова, но слот NEW закрывается урок-завершением
    fresh_db.promote_new(UID, n=1)
    assert fresh_db.day_map(UID)["new"] is False            # ещё нет log_session('new')
    fresh_db.log_session(UID, "new")                        # _finish_lesson
    assert fresh_db.day_map(UID)["new"] is True


# ---------- A2.1: роль сценария в system на каждый ход ----------

def _capture_call(monkeypatch):
    captured = {}

    def fake_chat(system, messages, max_tokens=600, model=None):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    return captured


def test_call_injects_scenario_role(fresh_db, monkeypatch):
    captured = _capture_call(monkeypatch)
    ctx = types.SimpleNamespace(user_data={"last_scenario": "Pitching", "history": []})
    asyncio.run(bot._call(ctx, "scenario", UID, "hello"))
    assert "ТЫ В РОЛИ" in captured["system"]                # роль не зависит от истории
    assert "Pitching" in captured["system"]


def test_call_flow_has_no_role_injection(fresh_db, monkeypatch):
    captured = _capture_call(monkeypatch)
    ctx = types.SimpleNamespace(user_data={"last_scenario": "Pitching", "history": []})
    asyncio.run(bot._call(ctx, "flow", UID, "hello"))
    assert "ТЫ В РОЛИ" not in captured["system"]            # вне сценария роли нет


# ---------- A2.3: review без колоды — это flow, не «назови, что повторяем» ----------

def test_assemble_review_falls_back_to_flow():
    assert "REVIEW" not in prompts.BLOCKS                   # мёртвый модуль выпилен
    out = prompts.assemble("review")                        # не падает
    assert out == prompts.assemble("flow")                  # собирается как FLOW


# ---------- A3.1: мягкий Лейтнер ----------

def test_fail_on_mature_word_drops_one_box(fresh_db):
    fresh_db.start_learning([1], UID)
    for _ in range(3):
        fresh_db.review(1, True, UID)                       # box 4
    r = fresh_db.review(1, False, UID)
    assert r["box"] == 3 and r["status"] == "forgot"        # −1, не в начало
    assert r["next_review"] == _today_plus(fresh_db.INTERVALS[3])


def test_fail_on_young_word_resets_to_box1(fresh_db):
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID)                           # box 2
    r = fresh_db.review(1, False, UID)
    assert r["box"] == 1                                    # незрелое — в начало, как было
    assert r["next_review"] == _today_plus(fresh_db.INTERVALS[1])
