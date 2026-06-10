"""Мусорный батч: офсайт-бэкап, запас слов, _LEARN_RE, /remind off, match_words."""
import bot, db

from conftest import UID


# ---------- ежедневный бэкап с копией на офсайт ----------

def test_daily_backup_copies_offsite(fresh_db, tmp_path, monkeypatch):
    src = tmp_path / "english_os-test.db"
    src.write_bytes(b"backup-bytes")
    monkeypatch.setattr(db, "backup", lambda: str(src))
    monkeypatch.setattr(bot, "OFFSITE_DIR", str(tmp_path / "offsite"))
    dst = bot._daily_backup()
    assert dst and dst.endswith("english_os-test.db")
    assert (tmp_path / "offsite" / "english_os-test.db").read_bytes() == b"backup-bytes"


def test_daily_backup_survives_missing_offsite(fresh_db, tmp_path, monkeypatch):
    src = tmp_path / "x.db"
    src.write_bytes(b"x")
    monkeypatch.setattr(db, "backup", lambda: str(src))
    monkeypatch.setattr(bot, "OFFSITE_DIR", r"Q:\нет\такого\диска")
    assert bot._daily_backup() is None          # офсайт недоступен — не падаем


# ---------- запас новых слов ----------

def test_stock_days_for_active_learner(fresh_db):
    fresh_db.ensure_user_state(UID)
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID)               # активность за последнюю неделю
    days = fresh_db.stock_days()
    assert days == round(3 / fresh_db.DAILY_NEW_CAP, 1)   # осталось 3 new-слова


def test_stock_days_none_without_active(fresh_db):
    assert fresh_db.stock_days() is None        # никто не занимался — сигналить не о ком


# ---------- _LEARN_RE: ложное срабатывание (A6) ----------

def test_learn_intent_new_words_sentence_not_captured():
    assert bot._parse_learn_intent("New words are hard for me") is None


def test_learn_intent_still_works():
    assert bot._parse_learn_intent("учим pivot, leverage") == ["pivot", "leverage"]
    assert bot._parse_learn_intent("I'd like to learn traction") == ["traction"]


# ---------- /remind off для программы дня ----------

def test_slot_reminders_can_be_disabled(fresh_db):
    fresh_db.set_program(UID, "cycle")          # дефолты 540/840/1140
    for slot in ("morning", "day", "evening"):
        fresh_db.set_slot_time(UID, slot, None)  # выключение
    assert fresh_db.slot_users(540) == []        # напоминания молчат
    assert fresh_db.get_slot_times(UID)["morning"] is None


# ---------- match_words: приоритет, а не порядок в фразе (A9) ----------

def test_match_words_prefers_priority(fresh_db):
    out = fresh_db.match_words("stakeholder revenue deadline invest", limit=2)
    assert [w["word"] for w in out] == ["invest", "deadline"]   # priority 25 и 20
