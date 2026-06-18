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
