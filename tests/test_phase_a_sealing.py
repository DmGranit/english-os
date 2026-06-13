"""Запечатывание Фазы A (2026-06-12): дефекты, вскрытые живым тестом.
B6 — залипший await_feedback теряет слово /add; B1 — ответ-стоп-слово на карточке
выбивает из колоды; B2 — сырой markdown в фолбэке _swap_in; B8 — детерминизм /read.
"""
import asyncio, time, types

import bot, db, enrich
from conftest import UID


async def _noop(*a, **k):
    pass


def _msg(sent):
    async def reply_text(t, reply_markup=None, parse_mode=None):
        sent.append(t)
        return types.SimpleNamespace()
    return types.SimpleNamespace(reply_text=reply_text, voice=None,
                                 chat=types.SimpleNamespace(send_action=_noop))


# ---------- B6: /add больше не теряет слово в залипшем await_feedback ----------

def test_add_cmd_no_args_clears_feedback_and_arms_add(fresh_db):
    """/add без аргументов снимает залипший await_feedback и ставит ожидание слова."""
    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(args=[], user_data={"await_feedback": True})
    asyncio.run(bot.add_cmd(update, ctx))
    assert "await_feedback" not in ctx.user_data       # стоп залипанию (корень B6)
    assert ctx.user_data.get("await_add") is True       # follow-up слово будет поймано


def test_word_after_add_goes_to_enrich_not_feedback(fresh_db, monkeypatch):
    """Слово, присланное после /add, уходит в enrich/очередь, а не в feedback."""
    seen = {}
    def fake_run(words, *a, **k):
        seen["words"] = list(words)
        return {"added": 1, "skipped": 0, "failed": 0}
    monkeypatch.setattr(enrich, "run", fake_run)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={"await_add": True},
                                bot=types.SimpleNamespace(send_message=_noop))
    asyncio.run(bot._process_user_text(update, ctx, UID, "Congratulate"))

    assert seen.get("words") == ["Congratulate"]        # ушло в enrich
    assert not db.recent_tech(kind="feedback")          # НЕ в feedback (нет потери данных)
    assert "await_add" not in ctx.user_data             # ожидание снято


# ---------- B1: стоп-слово как ОТВЕТ на карточку не завершает сессию ----------

def _deck_ctx(text_attempt, finish):
    return text_attempt, finish


def test_stop_word_on_active_card_goes_to_card(fresh_db, monkeypatch):
    """«Stop» во время свежей карточки = попытка ответа, а не конец сессии (B1)."""
    calls = []
    async def fake_card(update, ctx, uid, text): calls.append(("card", text))
    async def fake_finish(ud, uid, send): calls.append(("finish", None))
    monkeypatch.setattr(bot, "_text_attempt_on_card", fake_card)
    monkeypatch.setattr(bot, "_finish_session", fake_finish)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "card_shown_at": time.time()})
    asyncio.run(bot._process_user_text(update, ctx, UID, "Stop"))

    assert ("card", "Stop") in calls                    # ответ ушёл в карточку
    assert ("finish", None) not in calls                # сессия НЕ завершена


def test_explicit_itog_button_still_ends_during_deck(fresh_db, monkeypatch):
    """Явная кнопка «🏁 Итог» во время колоды по-прежнему завершает сессию."""
    calls = []
    async def fake_card(update, ctx, uid, text): calls.append(("card", text))
    async def fake_finish(ud, uid, send): calls.append(("finish", None))
    monkeypatch.setattr(bot, "_text_attempt_on_card", fake_card)
    monkeypatch.setattr(bot, "_finish_session", fake_finish)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "card_shown_at": time.time()})
    asyncio.run(bot._process_user_text(update, ctx, UID, bot.BTN_END))

    assert ("finish", None) in calls                    # кнопка завершает
    assert all(c[0] != "card" for c in calls)           # в карточку не ушло


# ---------- B2: фолбэк _swap_in рендерит HTML, а не сырой markdown ----------

def test_swap_in_fallback_renders_html_not_raw():
    """Сбой edit → новое сообщение приходит как HTML (bold), а не сырой **."""
    captured = []
    class _Ph:
        async def edit_text(self, *a, **k):
            raise RuntimeError("Bad Request: can't parse entities")
        async def reply_text(self, text, reply_markup=None, parse_mode=None):
            captured.append((text, parse_mode))
            return self
    asyncio.run(bot._swap_in(_Ph(), "Вот **incredible** результат"))
    text, pm = captured[-1]
    assert "**" not in text                              # сырых звёздочек нет
    assert "<b>incredible</b>" in text and pm == "HTML"  # отрендерено как HTML


# ---------- B8: /read не крутит одни и те же целевые слова ----------

def _seed_learning(n):
    with db._conn() as c:
        for wid in range(10, 10 + n):
            c.execute("""INSERT INTO content (word_id, word, ru, priority, level,
                         scenario, family, collocations, phrasal)
                         VALUES (?,?,?,?, 'B1','Pitching','[]','[]','[]')""",
                      (wid, f"w{wid}", f"ру{wid}", 50 - wid))
            c.execute("""INSERT INTO state (user_id, word_id, status, box)
                         VALUES (?,?, 'learning', 1)""", (UID, wid))


def test_target_words_varies_across_calls(fresh_db):
    """8 кандидатов, лимит 4 → за серию вызовов покрывается БОЛЬШЕ 4 слов (B8)."""
    _seed_learning(8)
    seen = set()
    for _ in range(30):
        seen.update(w["word_id"] for w in db.target_words(UID, limit=4))
    assert len(seen) > 4                                 # не залипает на топ-4 по priority
