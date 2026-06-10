"""Этап 4: сценарий-сессия — пресеты, подбор слов под полосу, оркестратор, грунтовка."""
import asyncio, types

import bot, db, llm
from conftest import UID


# ---------- theme_words с полосой комфорта ----------

def _add_word(db_, wid, word, level, priority, scenario):
    with db_._conn() as c:
        c.execute("""INSERT INTO content (word_id, word, ru, priority, level, scenario,
                     family, collocations, phrasal) VALUES (?,?,?,?,?,?, '[]','[]','[]')""",
                  (wid, word, "перевод", priority, level, scenario))
    db_.ensure_user_state(UID)


def test_theme_words_respects_band(fresh_db):
    _add_word(fresh_db, 99, "scale", "B2", 99, "Pitching")   # ценное, но выше полосы
    fresh_db.ensure_user_state(UID)
    words_b1 = fresh_db.theme_words("scn", "Pitching", UID, n=2, band="B1")
    assert words_b1[0]["word"] == "invest"                   # B1 при полосе B1 — первым
    words_b2 = fresh_db.theme_words("scn", "Pitching", UID, n=2, band="B2")
    assert words_b2[0]["word"] == "scale"                    # полоса выросла — слово доступно


# ---------- клавиатура пресетов ----------

def test_scenario_kb_built_from_alive_topics(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "scenario_list",
                        lambda min_n=15: [("Pitching", 27), ("Small talk", 15)])
    kb = bot._scenario_kb().to_dict()["inline_keyboard"]
    flat = [b for row in kb for b in row]
    assert [b["callback_data"] for b in flat] == ["scn:Pitching", "scn:Small talk"]


# ---------- оркестратор сценарий-сессии ----------

class _Ph:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        return self


def test_begin_scenario_orchestrates(fresh_db, monkeypatch):
    seen = {}

    def fake_chat(system, messages, **kw):
        seen["system"] = system
        seen["seed"] = messages[0]["content"]
        return "Intro + sample + role line."

    monkeypatch.setattr(llm, "chat", fake_chat)
    ph = _Ph()
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append((text, reply_markup))
        return ph

    msg = types.SimpleNamespace(reply_text=reply_text)
    ctx = types.SimpleNamespace(user_data={"awaiting_slot_hours": True})
    asyncio.run(bot._begin_scenario(msg, ctx, UID, "Pitching"))

    assert sent and "⏳" in sent[0][0]                        # мгновенный отклик
    assert ctx.user_data["mode"] == "scenario"
    assert ctx.user_data["scn_words"]                        # целевые слова сессии заданы
    assert "Pitching" in seen["seed"] and "invest" in seen["seed"]
    assert ph.edits and "role line" in ph.edits[0]           # плейсхолдер заменён ответом
    with fresh_db._conn() as c:                              # слова вошли в SRS
        st = c.execute("SELECT status FROM state WHERE user_id=? AND word_id=1",
                       (UID,)).fetchone()["status"]
    assert st == "learning"


# ---------- грунтовка диалога целевыми словами сессии ----------

def test_call_injects_session_words(fresh_db, monkeypatch):
    seen = {}

    def fake_chat(system, messages, **kw):
        seen["system"] = system
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    ctx = types.SimpleNamespace(user_data={"mode": "scenario", "history": [],
                                           "scn_words": [1]})
    asyncio.run(bot._call(ctx, "scenario", UID, "Let's negotiate the deal"))
    assert "ЦЕЛЕВЫЕ СЛОВА СЦЕНАРИЯ" in seen["system"]
    assert "invest" in seen["system"]


def test_finish_session_clears_session_words(fresh_db, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Отчёт.")
    monkeypatch.setattr(db, "backup", lambda: None)

    async def send(text, reply_markup=None, parse_mode=None):
        pass

    ud = {"mode": "scenario", "scn_words": [1],
          "history": [{"role": "user", "content": "hi"}]}
    asyncio.run(bot._finish_session(ud, UID, send))
    assert "scn_words" not in ud                             # сессия закрыта — цель снята
