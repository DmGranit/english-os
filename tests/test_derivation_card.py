"""Фаза 1b: вывод разбора деривации в сети карточки (_network_block)."""
import bot


def test_shows_derivation_line(fresh_db):
    fresh_db.set_derivation(1, base="vest", affix="in-", gloss="вкладывать внутрь")  # word 1 = invest
    word = fresh_db.get_word(1)
    block = bot._network_block(word)
    d = fresh_db.decompose(1)
    am = (d["affix_meaning"] or "").split(";")[0].strip()
    assert f"🧩 разбор: vest + in- — {am}" in block


def test_no_line_without_derivation(fresh_db):
    word = fresh_db.get_word(2)            # deadline — без derivation
    block = bot._network_block(word)
    assert "🧩" not in block


def test_order_derivation_after_root_before_family(fresh_db):
    fresh_db.set_derivation(1, base="vest", affix="in-", gloss="вкладывать внутрь")
    word = dict(fresh_db.get_word(1))
    word["family"] = ["investment"]        # семья присутствует -> проверяем порядок
    block = bot._network_block(word)
    assert "🧩 разбор" in block and "🌱 семья" in block
    assert block.index("🧩 разбор") < block.index("🌱 семья")
