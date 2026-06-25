"""Унификация морфо-рендера: db.morpho_lines + переиконка deep_view (2026-06-25)."""
import db


# ---------- db.morpho_lines (единый источник) ----------

def test_morpho_lines_order_root_deriv_family(fresh_db):
    with db._conn() as c:
        c.execute("UPDATE content SET root='vest', family='[\"investment\"]' WHERE word_id=1")
        c.execute("INSERT OR REPLACE INTO root_ref (root, idea, origin) VALUES ('vest','одевать','лат. vestire')")
    db.set_derivation(1, base="vest", affix="in-", gloss="вкладывать внутрь")
    lines = db.morpho_lines(db.get_word(1))
    assert len(lines) == 3
    assert lines[0].startswith("🌳 корень")
    assert lines[1].startswith("🧩 разбор")
    assert lines[2].startswith("🌱 семья")


def test_morpho_lines_root_format_canon(fresh_db):
    with db._conn() as c:
        c.execute("UPDATE content SET root='vest' WHERE word_id=1")
        c.execute("INSERT OR REPLACE INTO root_ref (root, idea, origin) VALUES ('vest','одевать','лат. vestire')")
    lines = db.morpho_lines(db.get_word(1))
    assert "🌳 корень vest: одевать · лат. vestire" in lines


def test_morpho_lines_empty_when_no_data(fresh_db):
    # word 2 = deadline: без root/derivation/family
    assert db.morpho_lines(db.get_word(2)) == []
