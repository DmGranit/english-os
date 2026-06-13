"""Dm5: cando_snapshot + cando_progress с фиксированным знаменателем."""
import db
from conftest import UID, WORDS


def _add_word(conn, word_id, word, ru, scenario, level="B1"):
    conn.execute("""INSERT OR IGNORE INTO content
                    (word_id, word, ru, priority, level, scenario, family, collocations, phrasal)
                    VALUES (?,?,?,10,?,?, '[]','[]','[]')""",
                 (word_id, word, ru, level, scenario))


# ---------- cando_snapshot ----------

def test_snapshot_populates_cando_words(fresh_db):
    # conftest: invest/deadline → Pitching/Status update (B1); stakeholder → Negotiating (B2)
    result = db.cando_snapshot()
    # Pitching снапшот должен содержать invest + revenue (из conftest WORDS)
    assert result["pitch"] >= 1


def test_snapshot_is_idempotent(fresh_db):
    r1 = db.cando_snapshot()
    r2 = db.cando_snapshot()
    # второй прогон не добавляет ничего (все cando_id уже заполнены)
    assert all(r2[cid] == 0 for cid in r2)


def test_snapshot_force_repopulates(fresh_db):
    db.cando_snapshot()
    # добавляем новое слово в Pitching и принудительно переснимаем
    with db._conn() as c:
        _add_word(c, 99, "traction", "тяга", "Pitching")
    r = db.cando_snapshot(force=True)
    assert r["pitch"] >= 3  # invest + revenue + traction


# ---------- cando_progress со снапшотом ----------

def test_cando_progress_uses_snapshot_denominator(fresh_db):
    db.cando_snapshot()
    snap_total = db.cando_progress(user_id=UID)
    # Pitching: 2 слова в снапшоте (invest word_id=1, revenue word_id=3), ни одно не освоено
    pitch = next(p for p in snap_total if p["id"] == "pitch")
    assert pitch["total"] == 2
    assert pitch["mastered"] == 0

    # добавляем 10 слов в Pitching (волна контента) — снапшот НЕ меняется
    with db._conn() as c:
        for i in range(100, 110):
            _add_word(c, i, f"word_{i}", f"перевод_{i}", "Pitching")
    progress_after = db.cando_progress(user_id=UID)
    pitch_after = next(p for p in progress_after if p["id"] == "pitch")
    assert pitch_after["total"] == 2  # знаменатель = снапшот, не +10


def test_cando_progress_fallback_without_snapshot(fresh_db):
    # без снапшота — фолбэк на живой счёт (не падает)
    result = db.cando_progress(user_id=UID)
    pitch = next(p for p in result if p["id"] == "pitch")
    assert pitch["total"] >= 0  # живой счёт: может быть 0, если user не в state для этих слов


def test_cando_progress_ready_flag(fresh_db):
    db.cando_snapshot()
    # освоим invest (word_id=1) и revenue (word_id=3) → 2 из 2 → ready
    db.ensure_user_state(UID)
    with db._conn() as c:
        c.execute("UPDATE state SET status='known' WHERE user_id=? AND word_id IN (1,3)", (UID,))
    progress = db.cando_progress(user_id=UID)
    pitch = next(p for p in progress if p["id"] == "pitch")
    assert pitch["ready"] is True
    assert pitch["pct"] == 1.0
