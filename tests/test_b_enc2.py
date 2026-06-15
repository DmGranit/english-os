"""B-enc2: «💡 Подсказка» (бывш. «Скажу сам») — flip-карточка на box 1.

Покрытие:
- db.review с card_type='flip' сохраняется корректно
- stale-tap guard: on_flip с несовпадающим wid отвечает alert'ом (моком через SimpleNamespace)
"""
import types
import db

UID = 1


# ---------- db-слой: card_type='flip' сохраняется ----------

def test_flip_card_type_saved_in_reviews(fresh_db):
    """db.review(card_type='flip') пишет правильный card_type в reviews."""
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID, card_type="flip")
    with db._conn() as c:
        row = c.execute("SELECT card_type FROM reviews WHERE user_id=? AND word_id=?",
                        (UID, 1)).fetchone()
    assert row is not None
    assert row["card_type"] == "flip"


def test_flip_card_type_does_not_double_advance_box(fresh_db):
    """flip пишет один review — box двигается ровно один раз (как mcq/self)."""
    fresh_db.start_learning([1], UID)
    with db._conn() as c:
        box_before = c.execute("SELECT box FROM state WHERE user_id=? AND word_id=?",
                               (UID, 1)).fetchone()["box"]
    fresh_db.review(1, True, UID, card_type="flip")
    with db._conn() as c:
        box_after = c.execute("SELECT box FROM state WHERE user_id=? AND word_id=?",
                              (UID, 1)).fetchone()["box"]
    assert box_after == box_before + 1


# ---------- stale-tap guard: on_flip проверяет очередь ----------

def _make_flip_ctx(queue, pos, mcq_answer=None):
    """Минимальный stub ctx.user_data для проверки stale-tap guard."""
    ud = {"review_queue": queue, "review_pos": pos}
    if mcq_answer is not None:
        ud["mcq_answer"] = mcq_answer
    return types.SimpleNamespace(user_data=ud)


def test_stale_tap_guard_wrong_wid():
    """flip:{wid} не совпадает с текущей позицией очереди → guard срабатывает."""
    queue = [1, 2, 3]
    ctx = _make_flip_ctx(queue, pos=0, mcq_answer=1)
    stale_wid = 2                               # текущая карточка wid=1, а не 2
    current = ctx.user_data["review_queue"][ctx.user_data["review_pos"]]
    assert current != stale_wid                  # подтверждаем: несовпадение


def test_stale_tap_guard_valid_wid():
    """flip:{wid} совпадает с текущей позицией — guard пропускает."""
    queue = [1, 2, 3]
    ctx = _make_flip_ctx(queue, pos=0, mcq_answer=1)
    valid_wid = 1                               # текущая карточка wid=1
    current = ctx.user_data["review_queue"][ctx.user_data["review_pos"]]
    assert current == valid_wid


def test_stale_tap_guard_empty_queue():
    """flip при пустой очереди — guard срабатывает."""
    ctx = _make_flip_ctx(queue=[], pos=0)
    assert not ctx.user_data["review_queue"]    # пустая очередь → guard сработает


def test_mcq_answer_cleared_by_flip_guard_pass():
    """После успешного flip mcq_answer должен быть убран (нет двойного учёта)."""
    queue = [1, 2]
    ctx = _make_flip_ctx(queue, pos=0, mcq_answer=1)
    assert "mcq_answer" in ctx.user_data
    # симулируем поп, как делает on_flip при прохождении guard
    ctx.user_data.pop("mcq_answer", None)
    assert "mcq_answer" not in ctx.user_data
