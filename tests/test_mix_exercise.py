"""B3: упражнение-микс — exercise_for_word (сборка/продукция) + грейд + поурочный loop."""
import db
from conftest import UID


def test_exercise_assembly_when_family_decomposable(fresh_db):
    # invest (word_id 1) с гнездом, где investment = invest + -ment (уверенно)
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment", "investor"]',))
    ex = fresh_db.exercise_for_word(1)
    assert ex["kind"] == "assembly"
    assert ex["expected"] == "investment"
    assert "invest" in ex["prompt"] and "-ment" in ex["prompt"]

def test_exercise_production_when_no_decomposable_family(fresh_db):
    # deadline (word_id 2): нет гнезда → продукция RU→EN
    ex = fresh_db.exercise_for_word(2)
    assert ex["kind"] == "production"
    assert ex["expected"] == "deadline"
    assert "крайний срок" in ex["prompt"]
