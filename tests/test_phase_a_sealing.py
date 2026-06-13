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

def test_english_stop_on_typed_card_goes_to_typed(fresh_db, monkeypatch):
    """Английское «stop» во время typed-карточки → ответ на карточку, не конец сессии.
    Бот ждёт английское слово; «stop» может быть правильным ответом."""
    calls = []
    async def fake_typed(update, ctx, uid, text): calls.append(("typed", text))
    async def fake_finish(ud, uid, send): calls.append(("finish", None))
    monkeypatch.setattr(bot, "_handle_typed_answer", fake_typed)
    monkeypatch.setattr(bot, "_finish_session", fake_finish)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "card_shown_at": time.time(),
        "typed_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, "Stop"))

    assert ("typed", "Stop") in calls
    assert ("finish", None) not in calls


def test_russian_stop_on_typed_card_ends_session(fresh_db, monkeypatch):
    """Русское «Итог»/«Стоп» во время typed-карточки → завершает сессию.
    Кириллица — точно не ответ на английскую typed-карточку."""
    calls = []
    async def fake_typed(update, ctx, uid, text): calls.append(("typed", text))
    async def fake_finish(ud, uid, send): calls.append(("finish", None))
    monkeypatch.setattr(bot, "_handle_typed_answer", fake_typed)
    monkeypatch.setattr(bot, "_finish_session", fake_finish)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "card_shown_at": time.time(),
        "typed_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, "Стоп"))

    assert ("finish", None) in calls
    assert ("typed", "Стоп") not in calls


def test_english_stop_on_mcq_card_ends_session(fresh_db, monkeypatch):
    """«Stop» во время mcq/self-rate карточки (typed_wid не установлен) → завершает сессию."""
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

    assert ("finish", None) in calls
    assert ("card", "Stop") not in calls


def test_explicit_itog_button_still_ends_during_typed(fresh_db, monkeypatch):
    """Явная кнопка «🏁 Итог» завершает сессию даже если typed_wid установлен."""
    calls = []
    async def fake_typed(update, ctx, uid, text): calls.append(("typed", text))
    async def fake_finish(ud, uid, send): calls.append(("finish", None))
    monkeypatch.setattr(bot, "_handle_typed_answer", fake_typed)
    monkeypatch.setattr(bot, "_finish_session", fake_finish)

    sent = []
    update = types.SimpleNamespace(message=_msg(sent),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(user_data={
        "review_queue": [1], "review_pos": 0, "card_shown_at": time.time(),
        "typed_wid": 1})
    asyncio.run(bot._process_user_text(update, ctx, UID, bot.BTN_END))

    assert ("finish", None) in calls
    assert all(c[0] != "typed" for c in calls)


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
