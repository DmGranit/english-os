"""B5.2: структурный гейт разбора derivation — корректность > полнота."""
import db


def test_accepts_clean_suffix():
    assert db.valid_derivation("valuation", "value", "-tion") is True   # value->valu (потеря e)
    assert db.valid_derivation("useless", "use", "-less") is True
    assert db.valid_derivation("happiness", "happy", "-ness") is True   # happy->happi (y->i)
    assert db.valid_derivation("commitment", "commit", "-ment") is True


def test_accepts_clean_prefix():
    assert db.valid_derivation("remove", "move", "re-") is True
    assert db.valid_derivation("unhappy", "happy", "un-") is True


def test_rejects_inflection_as_base():
    # база длиннее/равна слову — флексия или мн.ч., не база деривации
    assert db.valid_derivation("stakeholder", "stakeholders", "-er") is False
    assert db.valid_derivation("offer", "offers", "-er") is False
    assert db.valid_derivation("wonder", "wondering", "-er") is False


def test_rejects_etymological_cousin():
    # общий корень, но слово не кончается/не начинается базой
    assert db.valid_derivation("predict", "dictionary", "pre-") is False
    assert db.valid_derivation("capable", "capture", "-able") is False
    assert db.valid_derivation("remember", "memory", "re-") is False


def test_rejects_unknown_affix_or_short_base():
    assert db.valid_derivation("xyzzy", "xy", "-z") is False        # база < 3
    assert db.valid_derivation("blahblah", "blah", "-zzz") is False  # аффикса нет в affix_ref
