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

def test_encoding_view_shows_word_translation_example(fresh_db):
    # conftest: word_id 1 = invest / инвестировать, есть example? (в conftest example пуст —
    # проверяем устойчивость: заголовок и перевод обязательно есть)
    txt = fresh_db.encoding_view(1)
    assert "invest" in txt and "инвестировать" in txt

def test_encoding_view_decomposes_family_with_affix(fresh_db):
    # дать invest гнездо с явным деривационным членом
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment", "investor"]',))
    txt = fresh_db.encoding_view(1)
    assert "-ment" in txt            # аффикс показан
    assert "invest·ment" in txt      # акцент на аффиксе (насмотренность), не голое слово

def test_encoding_view_caps_nest_at_3(fresh_db):
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1",
                  ('["investment", "investor", "investing", "reinvest", "divest"]',))
    txt = fresh_db.encoding_view(1)
    shown = [m for m in ["investment", "investor", "investing", "reinvest", "divest"] if m in txt]
    assert len(shown) <= 3           # ≤3 производных за первый контакт (D4)
