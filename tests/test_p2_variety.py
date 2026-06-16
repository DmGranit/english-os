"""P2 (план v2.6): вариативность формата карточки в коробке (анти-«одна кнопка»).

Один и тот же box даёт разные лица в разные дни — детерминированно по hash(wid+date+box),
но стабильно в течение дня. Вариант А (R42): варьируются ТОЛЬКО box1/box2; продуктивная
лестница box3/4/5 не трогается. Ключ честности: записываемый reviews.card_type отражает
ФАКТ показанного формата (через ctx.user_data['card_format']), а не выводится из box.
"""
import asyncio, types

import bot, db
from conftest import UID

DAYS = [f"2026-06-{d:02d}" for d in range(1, 29)]


# ---------- селектор формата ----------

def test_card_format_deterministic_within_day():
    a = bot._card_format(1, 1, opts_n=4, date="2026-06-16")
    b = bot._card_format(1, 1, opts_n=4, date="2026-06-16")
    assert a == b                                          # стабильно в течение дня


def test_card_format_box1_varies_across_days():
    seen = {bot._card_format(1, 1, opts_n=4, date=d) for d in DAYS}
    assert seen == {"mcq", "self"}                         # оба лица встречаются по дням


def test_card_format_box1_only_compatible():
    assert all(bot._card_format(1, 1, opts_n=4, date=d) in {"mcq", "self"} for d in DAYS)


def test_card_format_box2_cloze_or_self():
    seen = {bot._card_format(1, 2, has_cloze=True, date=d) for d in DAYS}
    assert seen == {"cloze", "self"}


def test_card_format_ladder_sacred():
    for box in (3, 4, 5):                                  # продуктивная лестница не варьируется
        assert bot._card_format(1, box, opts_n=4, has_cloze=True, date="2026-06-16") is None


def test_card_format_fallback_no_cloze():                 # нет коллокации → recall, не пустой cloze
    assert all(bot._card_format(1, 2, has_cloze=False, date=d) == "self" for d in DAYS)


def test_card_format_fallback_few_options():              # <2 дистрактора → recall, не MCQ-огрызок
    assert all(bot._card_format(1, 1, opts_n=1, date=d) == "self" for d in DAYS)


# ---------- честность card_type: пишется факт формата, не вывод из box ----------

class _FakeQ:
    def __init__(self, data):
        self.data = data
        self.message = types.SimpleNamespace(reply_text=self._a, edit_text=self._a)

    async def answer(self, *a, **k): pass
    async def edit_message_text(self, *a, **k): pass
    async def _a(self, *a, **k): pass


def _capture_card_type(monkeypatch):
    rec = {}
    monkeypatch.setattr(db, "review",
                        lambda wid, ok, uid, variant=None, ms=None, card_type=None:
                        rec.update(card_type=card_type) or
                        {"word_id": wid, "box": 2, "status": "learning", "next_review": ""})
    monkeypatch.setattr(db, "adapt_band", lambda uid: None)
    monkeypatch.setattr(db, "backup", lambda: None)
    return rec


def _answer_box2(monkeypatch, card_format=None):
    rec = _capture_card_type(monkeypatch)
    ud = {"review_queue": [1], "review_box": {1: 2}, "review_pos": 0}
    if card_format is not None:
        ud["card_format"] = card_format
    ctx = types.SimpleNamespace(user_data=ud)
    update = types.SimpleNamespace(callback_query=_FakeQ("rev:ok"),
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot.on_review(update, ctx))
    return rec


def test_box2_self_records_self_not_cloze(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)                        # recall-вариант на box2 → 'self', НЕ 'cloze'
    assert _answer_box2(monkeypatch, card_format="self")["card_type"] == "self"


def test_box2_cloze_records_cloze(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)
    assert _answer_box2(monkeypatch, card_format="cloze")["card_type"] == "cloze"


def test_box2_default_cloze_when_no_format(fresh_db, monkeypatch):
    fresh_db.ensure_user_state(UID)                        # backward-compat: без card_format → старый вывод из box
    assert _answer_box2(monkeypatch, card_format=None)["card_type"] == "cloze"
