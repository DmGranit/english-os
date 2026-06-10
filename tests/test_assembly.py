"""Этап 5.2: сборка SVOMPT (box 4) — предложение из перемешанных слов кнопками."""
import asyncio, types

import bot, db
from conftest import UID

EXAMPLE_TOKENS = ["we", "need", "to", "meet", "the", "deadline"]   # первое слово нормализовано


# ---------- пригодность примера ----------

def test_asm_tokens_splits_suitable_example():
    assert bot._asm_tokens("We need to meet the deadline") == EXAMPLE_TOKENS


def test_asm_tokens_rejects_too_short_or_long():
    assert bot._asm_tokens("Too short") is None
    assert bot._asm_tokens(" ".join(["w"] * 12)) is None
    assert bot._asm_tokens(None) is None


# ---------- карточка box 4 — конструктор ----------

def _ctx_for(wid, box):
    return types.SimpleNamespace(user_data={
        "review_queue": [wid], "review_box": {wid: box}, "review_pos": 0})


def test_box4_card_is_assembly(fresh_db, monkeypatch):
    monkeypatch.setattr(bot.random, "sample", lambda seq, k: list(reversed(seq)))
    with fresh_db._conn() as c:                       # пример деловой фразы у deadline
        c.execute("UPDATE content SET example='We need to meet the deadline' WHERE word_id=2")
    ctx = _ctx_for(2, 4)
    text, kb = bot._card_payload(ctx, UID)
    assert "🧩" in text and "deadline" in text         # конструктор с подсказкой слова
    labels = [b["text"] for row in kb.to_dict()["inline_keyboard"] for b in row]
    assert sorted(labels) == sorted(EXAMPLE_TOKENS)    # все кусочки на кнопках
    assert ctx.user_data["asm_target"] == EXAMPLE_TOKENS
    assert ctx.user_data["asm_order"] == list(reversed(EXAMPLE_TOKENS))


def test_box4_without_example_falls_back_to_production(fresh_db):
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET example=NULL WHERE word_id=2")
    ctx = _ctx_for(2, 4)
    text, _ = bot._card_payload(ctx, UID)
    assert "🧩" not in text                            # обычная продукция RU→EN
    assert "Как сказать по-английски" in text


# ---------- сборка: верные/неверные тапы, запись результата ----------

class _Q:
    def __init__(self, data):
        self.data = data
        self.edits, self.toasts = [], []
        self.message = types.SimpleNamespace(
            reply_text=self._reply, edit_text=self._edit)

    async def answer(self, text=None, show_alert=False):
        self.toasts.append(text)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))

    async def _reply(self, *a, **k):
        return self.message

    async def _edit(self, *a, **k):
        pass


def _tap(ctx, idx_or_next, recorded):
    q = _Q(f"asm:{idx_or_next}")
    update = types.SimpleNamespace(callback_query=q,
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_assembly(update, ctx))
    return q


def test_assembly_full_flow_records_objective_result(fresh_db, monkeypatch):
    monkeypatch.setattr(bot.random, "sample", lambda seq, k: list(reversed(seq)))
    monkeypatch.setattr(db, "backup", lambda: None)
    recorded = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None, card_type=None:
                        recorded.update(wid=wid, ok=ok) or
                        {"word_id": wid, "box": 5, "status": "known", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET example='We need to meet the deadline' WHERE word_id=2")
    ctx = _ctx_for(2, 4)
    bot._card_payload(ctx, UID)                        # построить конструктор

    order = ctx.user_data["asm_order"]                 # тапаем в ПРАВИЛЬНОМ порядке цели
    q = None
    for token in EXAMPLE_TOKENS:
        q = _tap(ctx, order.index(token), recorded)
    assert recorded == {"wid": 2, "ok": True}          # собрал без ошибок -> объективный зачёт
    assert q.edits and "✅" in q.edits[-1][0]           # экран результата
    assert "We need to meet the deadline" in q.edits[-1][0]   # в показе заглавная восстановлена

    _tap(ctx, "next", recorded)                        # «Дальше» -> колода закончилась
    # финал колоды редактирует сообщение и шлёт носитель — достаточно, что не упало


def test_assembly_wrong_tap_counts_error(fresh_db, monkeypatch):
    monkeypatch.setattr(bot.random, "sample", lambda seq, k: list(reversed(seq)))
    recorded = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None, card_type=None:
                        recorded.update(ok=ok) or
                        {"word_id": wid, "box": 1, "status": "forgot", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    monkeypatch.setattr(db, "backup", lambda: None)
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET example='We need to meet the deadline' WHERE word_id=2")
    ctx = _ctx_for(2, 4)
    bot._card_payload(ctx, UID)
    order = ctx.user_data["asm_order"]

    q = _tap(ctx, order.index("deadline"), recorded)   # неверное первое слово
    assert ctx.user_data["asm_errors"] == 1
    assert q.toasts and q.toasts[0]                    # мягкий тост, не падение
    for token in EXAMPLE_TOKENS:                       # дособираем правильно
        _tap(ctx, order.index(token), recorded)
    assert recorded["ok"] is False                     # с ошибкой -> честный незачёт
