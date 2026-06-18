"""B1: данные аффиксного слоя — affix_ref (деривационные аффиксы) + content.derivation."""
import db
from conftest import UID


def test_affix_ref_seeded_and_lookup(fresh_db):
    ize = fresh_db.affix_info("-ize")
    assert ize is not None
    assert ize["kind"] == "suffix"
    assert "глагол" in ize["meaning_ru"].lower() or "делать" in ize["meaning_ru"].lower()
    assert "modernize" in ize["examples"].lower()
    un = fresh_db.affix_info("un-")
    assert un and un["kind"] == "prefix"


def test_affixes_all_filters_by_kind(fresh_db):
    allx = fresh_db.affixes_all()
    prefixes = fresh_db.affixes_all("prefix")
    suffixes = fresh_db.affixes_all("suffix")
    assert len(allx) >= 30                                  # курированный набор
    assert len(prefixes) + len(suffixes) == len(allx)
    assert all(a["kind"] == "prefix" for a in prefixes)
    # флексии исключены — множителя ради
    bases = {a["affix"] for a in allx}
    assert "-ed" not in bases and "-ing" not in bases and "-s" not in bases


def test_affixes_all_shape_matches_affix_info(fresh_db):
    # парность формы: один контракт affix-словаря для витрины (B2) и точечного лукапа
    one = fresh_db.affixes_all()[0]
    info = fresh_db.affix_info(one["affix"])
    assert set(one.keys()) == set(info.keys())              # те же ключи, включая note


def test_derivation_roundtrip_and_affix_join(fresh_db):
    # слово 1 = invest (есть в conftest WORDS); привяжем как производное (демо-связка)
    fresh_db.set_derivation(1, base="vest", affix="in-", gloss="вкладывать внутрь")
    d = fresh_db.decompose(1)
    assert d["base"] == "vest" and d["affix"] == "in-"
    assert d["gloss"] == "вкладывать внутрь"
    assert "отрицание" in (d.get("affix_meaning") or "")   # подмешан meaning_ru из affix_ref (in-)


def test_decompose_none_when_absent(fresh_db):
    assert fresh_db.decompose(2) is None                    # у deadline нет derivation
