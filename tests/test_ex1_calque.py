"""EX1 (план v2.6): упражнение «найди кальку» на mistakes_ref.

Стендалон-упражнение (R44, вариант А) — /calque, ВНЕ SRS (Лейтнер/reviews не трогаются).
Формат «найди кальку»: 3 фразы, одна — русская калька (`wrong`), две — корректные `right`
из ДРУГИХ категорий (R44). Верный тап — тихий зачёт; промах → log_error('...','ex1_calque_miss').
"""
import asyncio, types

import bot, db
from conftest import UID

ns = types.SimpleNamespace

# (category, wrong, right, why, context)
_MISTAKES = [
    ("collocation", "do a decision", "make a decision", "калька с русского", "принять решение"),
    ("collocation", "make homework", "do homework", "калька", "делать домашку"),
    ("preposition", "depend from", "depend on", "калька", "зависеть от"),
    ("article", "go to home", "go home", "калька", "идти домой"),
]
_RIGHT2CAT = {r: cat for cat, w, r, why, ctx in _MISTAKES}


def _seed_mistakes(db_):
    with db_._conn() as c:
        for cat, w, r, why, ctx in _MISTAKES:
            c.execute("""INSERT INTO mistakes_ref (category, wrong, right, why, context)
                         VALUES (?,?,?,?,?)""", (cat, w, r, why, ctx))


# ---------- db.calque_card ----------

def test_calque_card_shape(fresh_db):
    _seed_mistakes(fresh_db)
    card = fresh_db.calque_card()
    assert card["wrong"] and card["right"] and card["why"]
    assert len(card["distractors"]) == 2


def test_calque_distractors_other_category(fresh_db):
    _seed_mistakes(fresh_db)
    for _ in range(30):                                 # рандомная строка — инвариант на выборке
        card = fresh_db.calque_card()
        for d in card["distractors"]:
            assert _RIGHT2CAT[d] != card["category"]   # дистрактор из ДРУГОЙ категории (R44)
            assert d != card["right"]                  # и не правильная форма самой строки


def test_calque_distractors_unique(fresh_db):
    _seed_mistakes(fresh_db)
    for _ in range(30):
        card = fresh_db.calque_card()
        assert len(set(card["distractors"])) == 2      # без дублей


def test_calque_card_none_when_empty(fresh_db):
    assert fresh_db.calque_card() is None              # нет данных → None (не падение)


# ---------- поведение: /calque + on_calque ----------

class _FakeQ:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.message = ns(reply_text=self._reply)
        self.replies = []

    async def answer(self, *a, **k): pass
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)
    async def _reply(self, text, reply_markup=None, parse_mode=None):
        self.replies.append(text)


def _msg():
    sent = []
    async def reply_text(text, reply_markup=None, parse_mode=None):
        sent.append(text)
    return ns(reply_text=reply_text), sent


def _start(fresh_db):
    _seed_mistakes(fresh_db)
    fresh_db.ensure_user_state(UID)
    ctx = ns(user_data={})
    msg, sent = _msg()
    update = ns(message=msg, effective_user=ns(id=UID))
    asyncio.run(bot.cmd_calque(update, ctx))
    return ctx, sent


def test_calque_correct_no_error(fresh_db):
    ctx, _ = _start(fresh_db)
    ci = ctx.user_data["ex1_card"]["correct_idx"]
    q = _FakeQ(f"ex1:pick:{ci}")
    asyncio.run(bot.on_calque(ns(callback_query=q, effective_user=ns(id=UID)), ctx))
    assert ctx.user_data["ex1"]["ok"] == 1
    with fresh_db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 0   # успех — журнал тих


def test_calque_wrong_logs_error(fresh_db):
    ctx, _ = _start(fresh_db)
    card = ctx.user_data["ex1_card"]
    wrong_pick = (card["correct_idx"] + 1) % 3                # любой не-калька
    q = _FakeQ(f"ex1:pick:{wrong_pick}")
    asyncio.run(bot.on_calque(ns(callback_query=q, effective_user=ns(id=UID)), ctx))
    with fresh_db._conn() as c:
        rows = c.execute("SELECT category, note FROM errors").fetchall()
    assert len(rows) == 1
    assert rows[0]["note"] == "ex1_calque_miss"
    assert rows[0]["category"] == card["category"]


def test_calque_reveal_shows_right_and_why(fresh_db):
    ctx, _ = _start(fresh_db)
    ci = ctx.user_data["ex1_card"]["correct_idx"]
    right = ctx.user_data["ex1_card"]["right"]
    q = _FakeQ(f"ex1:pick:{ci}")
    asyncio.run(bot.on_calque(ns(callback_query=q, effective_user=ns(id=UID)), ctx))
    assert any(right in t for t in q.edits)                   # правильная форма показана на раскрытии


def test_calque_exercise_completes_outside_srs(fresh_db):
    ctx, _ = _start(fresh_db)
    for _ in range(5):                                        # ответить все 5 карточек
        ci = ctx.user_data["ex1_card"]["correct_idx"]
        asyncio.run(bot.on_calque(ns(callback_query=_FakeQ(f"ex1:pick:{ci}"),
                                     effective_user=ns(id=UID)), ctx))
        asyncio.run(bot.on_calque(ns(callback_query=_FakeQ("ex1:next"),
                                     effective_user=ns(id=UID)), ctx))
    assert "ex1" not in ctx.user_data                         # сессия завершена и подчищена
    with fresh_db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0   # вне SRS
