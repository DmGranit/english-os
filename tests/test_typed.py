"""Этап 5.3: typed recall (box 3) — ответ печатается, проверка объективная."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- «одна правка» — допуск на опечатку ----------

def test_one_edit_away():
    assert bot._one_edit_away("traction", "traction")
    assert bot._one_edit_away("tracton", "traction")     # пропуск буквы
    assert bot._one_edit_away("tracktion", "traction")   # лишняя буква
    assert bot._one_edit_away("trastion", "traction")    # замена
    assert bot._one_edit_away("invets", "invest")        # перестановка соседних (Дамерау)
    assert not bot._one_edit_away("trakshen", "traction")
    assert not bot._one_edit_away("deal", "deadline")


# ---------- карточка box 3 — ввод текста ----------

def _ctx(queue, boxes, pos=0):
    return types.SimpleNamespace(user_data={
        "review_queue": queue, "review_box": boxes, "review_pos": pos})


def test_box3_card_asks_typed_answer(fresh_db):
    ctx = _ctx([1], {1: 3})
    text, kb = bot._card_payload(ctx, UID)
    assert "✍️" in text and "Напиши" in text
    assert "инвестировать" in text                       # вопрос — перевод
    assert "invest" not in text                          # ответ не подсвечен
    btns = [b["callback_data"] for row in kb.to_dict()["inline_keyboard"] for b in row]
    assert btns == ["rev:fail"]                          # только «Не помню»
    assert ctx.user_data["typed_wid"] == 1


def test_box5_keeps_self_assessment(fresh_db):
    ctx = _ctx([1], {1: 5})
    text, kb = bot._card_payload(ctx, UID)
    assert "✍️" not in text                              # maintenance — как раньше
    assert ctx.user_data.get("typed_wid") is None


# ---------- обработка напечатанного ответа ----------

def _env():
    sent = []

    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)
        return types.SimpleNamespace()

    chat = types.SimpleNamespace(send_action=_noop)
    message = types.SimpleNamespace(reply_text=reply_text, chat=chat, voice=None)
    update = types.SimpleNamespace(message=message,
                                   effective_user=types.SimpleNamespace(id=UID))
    return update, sent


async def _noop(*a, **k):
    pass


def _run_typed(fresh_db, monkeypatch, answer, queue=None, boxes=None):
    recorded = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None:
                        recorded.update(wid=wid, ok=ok) or
                        {"word_id": wid, "box": 4, "status": "learning", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    monkeypatch.setattr(db, "backup", lambda: None)
    ctx = _ctx(queue or [1], boxes or {1: 3})
    bot._card_payload(ctx, UID)                          # построить typed-карточку
    update, sent = _env()
    asyncio.run(bot._process_user_text(update, ctx, UID, answer))
    return recorded, sent, ctx


def test_typed_exact_answer_scores(fresh_db, monkeypatch):
    recorded, sent, ctx = _run_typed(fresh_db, monkeypatch, "invest")
    assert recorded == {"wid": 1, "ok": True}
    assert any("✅" in s for s in sent)
    assert "typed_wid" not in ctx.user_data              # карточка закрыта


def test_typed_typo_forgiven(fresh_db, monkeypatch):
    recorded, sent, _ = _run_typed(fresh_db, monkeypatch, "invets")
    assert recorded["ok"] is True
    assert any("Почти" in s for s in sent)


def test_typed_wrong_answer_fails_and_shows_truth(fresh_db, monkeypatch):
    recorded, sent, _ = _run_typed(fresh_db, monkeypatch, "money")
    assert recorded["ok"] is False
    assert any("invest" in s for s in sent)              # правильный ответ показан


def test_typed_advances_to_next_card(fresh_db, monkeypatch):
    recorded, sent, ctx = _run_typed(fresh_db, monkeypatch, "invest",
                                     queue=[1, 2], boxes={1: 3, 2: 1})
    assert ctx.user_data["review_pos"] == 1              # колода сдвинулась
    assert any("deadline" in s for s in sent)            # следующая карточка пришла


def test_itog_interrupts_typed_card(fresh_db, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Отчёт.")
    monkeypatch.setattr(db, "backup", lambda: None)
    ctx = _ctx([1], {1: 3})
    ctx.user_data["history"] = [{"role": "user", "content": "hi"}]
    bot._card_payload(ctx, UID)
    update, sent = _env()
    asyncio.run(bot._process_user_text(update, ctx, UID, "Итог"))
    assert "typed_wid" not in ctx.user_data              # сессия закрыта, карточка снята
    assert any("Отчёт" in s for s in sent)
