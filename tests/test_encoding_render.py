"""B2: богатое предъявление — detect_affix (консервативно) + encoding_view + wiring."""
import db
from conftest import UID


def test_detect_affix_confident_matches(fresh_db):
    assert fresh_db.detect_affix("deployment", "deploy")["affix"] == "-ment"
    assert fresh_db.detect_affix("strategic", "strategy")["affix"] == "-ic"
    assert fresh_db.detect_affix("unhappy")["affix"] == "un-"
    assert fresh_db.detect_affix("modernize")["affix"] == "-ize"

def test_detect_affix_rejects_false_splits(fresh_db):
    # 'table' заканчивается на 'able', но остаток 't' < 3 → не аффикс
    assert fresh_db.detect_affix("table") is None
    # нет уверенного аффикса в нашем наборе
    assert fresh_db.detect_affix("important") is None
    # пустое/короткое
    assert fresh_db.detect_affix("go") is None

def test_detect_affix_prefers_longest(fresh_db):
    # 'careless' → '-less' (4), не '-s'; '-s' и так не в наборе, но проверяем длину
    assert fresh_db.detect_affix("careless")["affix"] == "-less"

def test_detect_affix_uses_base_to_reject_unrelated(fresh_db):
    # 'commute' is not derived from 'mutual' — no shared stem → no affix
    assert fresh_db.detect_affix("commute", base="mutual") is None
    # still detects when base genuinely shares the stem
    assert fresh_db.detect_affix("deployment", base="deploy")["affix"] == "-ment"
    assert fresh_db.detect_affix("strategic", base="strategy")["affix"] == "-ic"
    # no base → unchanged behavior (prefix still works)
    assert fresh_db.detect_affix("unhappy")["affix"] == "un-"
