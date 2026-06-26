"""GR2: ВРЕМЕНА через трансформацию (/grammar) — первый SRS-шаг для грамматики.

Спека (план v2.6, стр.90–98):
- grammar_state(user_id, topic, box, next_review) — тема зреет по той же Лейтнер-логике.
- Карточка card_type='transform' на box≥2 («Поставь в Past: I go to work» → «I went to work»).
- Грейдер: exact ‖ узкий LLM-чек (LLM — сторона бота). При провале — подсказка ru_mistake.
- Ошибка времени → errors.category='tense_aspect' (тот же журнал, что сценарные).

Покрытие (db-контракт + чистый грейдер из bot):
"""
import db

UID = 1


# ── сид трансформаций ─────────────────────────────────────────────────────────

def test_transform_seed_available(fresh_db):
    """Сид трансформаций содержит ≥3 разных грамм-темы."""
    topics = db.transform_topics()
    assert len(set(topics)) >= 3


def test_transform_seed_topics_nonempty(fresh_db):
    """Все темы — непустые строки (для ru_mistake-хинта по теме)."""
    assert all(isinstance(t, str) and t.strip() for t in db.transform_topics())


# ── grammar_state: интейк ──────────────────────────────────────────────────────

def test_ensure_grammar_state_transform_ready(fresh_db):
    """ensure_grammar_state заводит темы transform-готовыми (box≥2)."""
    db.ensure_grammar_state(UID)
    with db._conn() as c:
        rows = c.execute("SELECT topic, box FROM grammar_state WHERE user_id=?", (UID,)).fetchall()
    assert rows, "grammar_state пуст после ensure"
    assert all(r["box"] >= 2 for r in rows), "темы входят transform-готовыми (box≥2)"


def test_ensure_grammar_state_idempotent(fresh_db):
    """Повторный ensure не плодит дублей."""
    db.ensure_grammar_state(UID)
    db.ensure_grammar_state(UID)
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM grammar_state WHERE user_id=?", (UID,)).fetchone()[0]
    assert n == len(db.transform_topics())


# ── transform_card ──────────────────────────────────────────────────────────────

def test_transform_card_structure(fresh_db):
    """transform_card (auto-ensure) возвращает dict с topic/label/source/answer."""
    card = db.transform_card(UID)
    assert card is not None
    for key in ("topic", "label", "source", "answer"):
        assert key in card and card[key], f"нет/пусто поле {key}"


def test_transform_card_none_when_nothing_due(fresh_db):
    """После верного повторения всех тем — due пуст, transform_card=None."""
    db.ensure_grammar_state(UID)
    for topic in db.transform_topics():
        db.grammar_review(UID, topic, True)
    assert db.transform_card(UID) is None


# ── grammar_review: тот же Лейтнер ──────────────────────────────────────────────

def test_grammar_review_promotes_and_schedules(fresh_db):
    """Верно → box +1 и next_review в будущее."""
    db.ensure_grammar_state(UID)
    topic = db.transform_topics()[0]
    before = _box(UID, topic)
    res = db.grammar_review(UID, topic, True)
    assert res["box"] == min(before + 1, 5)
    assert res["next_review"] > db._today()


def test_grammar_review_box_capped_at_5(fresh_db):
    """Box не превышает 5."""
    db.ensure_grammar_state(UID)
    topic = db.transform_topics()[0]
    res = None
    for _ in range(10):
        res = db.grammar_review(UID, topic, True)
    assert res["box"] == 5


def test_grammar_review_failure_floored_at_box2(fresh_db):
    """Провал не роняет тему ниже box 2 — transform-карточка остаётся доступной."""
    db.ensure_grammar_state(UID)
    topic = db.transform_topics()[0]
    res = None
    for _ in range(5):
        res = db.grammar_review(UID, topic, False)
    assert res["box"] >= 2


def test_grammar_review_not_in_reviews_table(fresh_db):
    """Грамматика зреет в grammar_state, не засоряет словарную reviews."""
    db.ensure_grammar_state(UID)
    before = _reviews(UID)
    db.grammar_review(UID, db.transform_topics()[0], True)
    assert _reviews(UID) == before


# ── подсказка ru_mistake ────────────────────────────────────────────────────────

def test_grammar_ru_mistake_from_ref(fresh_db):
    """grammar_ru_mistake берёт ru_mistake из grammar_ref по теме."""
    topic = db.transform_topics()[0]
    with db._conn() as c:
        c.execute("INSERT OR IGNORE INTO grammar_ref (topic, ru_mistake) VALUES (?,?)",
                  (topic, "типичная калька RU"))
    assert db.grammar_ru_mistake(topic) == "типичная калька RU"


def test_grammar_ru_mistake_missing(fresh_db):
    assert db.grammar_ru_mistake("Несуществующая тема") is None


# ── журнал ошибок ───────────────────────────────────────────────────────────────

def test_tense_error_logged_as_tense_aspect(fresh_db):
    """Ошибка времени → errors.category='tense_aspect' (тот же журнал)."""
    before = _errors(UID)
    db.log_error("tense_aspect", "I go to work", "I went to work", "transform", UID)
    assert _errors(UID) == before + 1
    with db._conn() as c:
        rec = c.execute("SELECT * FROM errors WHERE user_id=? ORDER BY id DESC LIMIT 1",
                        (UID,)).fetchone()
    assert rec["category"] == "tense_aspect"


# ── грейдер (чистая функция из bot) ─────────────────────────────────────────────

def test_clear_other_exercise_isolation():
    """Старт одного стендалон-упражнения гасит typed-answer-состояние другого,
    но не трогает своё и чужие (не-упражнения) состояния. Иначе stale gr1_card
    перехватывает ввод для GR2 (поймано живым смоуком)."""
    from bot import _clear_other_exercise
    ud = {"gr1_card": {"x": 1}, "gr1": {"n": 2}, "gr2_card": {"y": 1}, "warm_wid": 5}
    _clear_other_exercise(ud, "gr2")              # старт GR2 → гасит GR1
    assert "gr1_card" not in ud and "gr1" not in ud
    assert ud["gr2_card"] == {"y": 1}             # своё не трогаем
    assert ud["warm_wid"] == 5                    # чужие (не-упражнения) не трогаем

    ud2 = {"gr1_card": {"x": 1}, "gr1": {"n": 2}, "gr2_card": {"y": 1}}
    _clear_other_exercise(ud2, "gr1")             # старт GR1 → гасит GR2
    assert "gr2_card" not in ud2
    assert ud2["gr1_card"] == {"x": 1} and ud2["gr1"] == {"n": 2}


def test_transform_exact_grader():
    """GR2 exact-грейдер: ТОЛЬКО нормализация (регистр/хвостовая пунктуация/пробелы); БЕЗ
    допуска-на-опечатку. Для трансформации времён одна буква формы (build/built) — это
    грамматика, а не опечатка; толерантность к опечаткам/синонимам отдана узкому LLM-чеку."""
    from bot import _transform_exact_ok
    assert _transform_exact_ok("I went to work", "I went to work")
    assert _transform_exact_ok("  i went to work.  ", "I went to work")     # нормализация
    assert not _transform_exact_ok("I go to work", "I went to work")        # неверное время
    # near-miss формы НЕ должны проходить exact (баг build/built — раньше _one_edit_away засчитывал)
    assert not _transform_exact_ok("They build houses", "They built houses")
    assert not _transform_exact_ok("I went to wrok", "I went to work")      # опечатка → теперь не exact (уйдёт в LLM)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _box(uid, topic):
    with db._conn() as c:
        return c.execute("SELECT box FROM grammar_state WHERE user_id=? AND topic=?",
                         (uid, topic)).fetchone()["box"]

def _errors(uid):
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM errors WHERE user_id=?", (uid,)).fetchone()[0]

def _reviews(uid):
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM reviews WHERE user_id=?", (uid,)).fetchone()[0]
