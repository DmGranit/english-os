"""B5: качество enrich — целевой смысл + hardening validate; set_derivation→bool."""
import json
import db, enrich, llm
from conftest import UID


def test_set_derivation_returns_bool(fresh_db):
    assert fresh_db.set_derivation(1, base="vest", affix="in-") is True     # word_id 1 = invest есть
    assert fresh_db.set_derivation(99999, base="x", affix="-y") is False    # нет такого слова
