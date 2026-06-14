"""C1: загрузка reference-слоёв (идемпотентность + счётчики + целостность state)."""
import json
import db


def _make_data():
    return {
        "words": [],
        "reference": {
            "4. Phrasal Verbs": [
                {"Фразовый": "bring up", "Значение": "поднять тему", "Пример": "He brought it up.",
                 "Логика предлога": "up = на поверхность", "Категория": "communication"},
            ],
            "5. Collocations": [
                {"Ядро (слово)": "deadline", "Типичные глаголы": "meet, miss, set",
                 "Типичные прилагательные": "tight, strict",
                 "Русский аналог": "крайний срок", "Осторожно — не говорят": "do a deadline"},
            ],
            "6. Scenarios": [
                {"Сценарий": "Pitching", "Открытие": "I'd like to present…",
                 "Ключевые фразы": "Our product…", "Закрытие": "Any questions?",
                 "Контекст": "B2 business"},
            ],
            "7. Thinking Frames": [
                {"Шаблон": "would + infinitive", "Перевод": "бы + инфинитив",
                 "Когда использовать": "вежливые просьбы", "Пример в речи": "I would like to…"},
            ],
            "8. Grammar": [
                {"Тема": "Present Perfect vs Past Simple",
                 "Когда использовать": "связь с настоящим", "Формула": "have/has + V3",
                 "Пример": "I have sent the report.", "Ошибка русскоговорящего": "I sent already"},
            ],
            "9. Mistakes": [
                {"Категория": "word_choice", "❌ Неправильно (калька)": "make homework",
                 "✅ Правильно": "do homework", "Почему": "do + routine",
                 "Контекст / RU перевод": "делать домашнее задание"},
            ],
            "10. BrE vs AmE": [
                {"Категория": "vocabulary", "🇬🇧 British": "flat", "🇺🇸 American": "apartment",
                 "Русский": "квартира"},
            ],
            "12. Do Not Confuse": [
                {"Корень": "affect/effect", "В чём ловушка": "глагол vs существительное",
                 "Группа A (смысл 1)": "affect (v)", "Группа B (смысл 2)": "effect (n)",
                 "Как различать": "A=Action, E=End result"},
            ],
        }
    }


# ---------- загрузка заполняет все таблицы ----------

def test_c1_all_tables_populated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    data = _make_data()
    with db._conn() as c:
        db._seed_reference(c, data)
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM phrasal_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM colloc_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM scenario_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM frame_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM grammar_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistakes_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM bre_ame_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM confuse_ref").fetchone()[0] == 1


# ---------- идемпотентность ----------

def test_c1_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    data = _make_data()
    with db._conn() as c:
        db._seed_reference(c, data)
        db._seed_reference(c, data)  # второй прогон
    with db._conn() as c:
        # ни одна таблица не задвоилась
        assert c.execute("SELECT COUNT(*) FROM phrasal_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM colloc_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM grammar_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistakes_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM bre_ame_ref").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM confuse_ref").fetchone()[0] == 1


# ---------- anti-паттерн в colloc_ref (нужен C1.b) ----------

def test_c1_colloc_ref_anti(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    data = _make_data()
    with db._conn() as c:
        db._seed_reference(c, data)
    with db._conn() as c:
        row = c.execute("SELECT anti FROM colloc_ref WHERE core='deadline'").fetchone()
    assert row and row["anti"] == "do a deadline"


# ---------- прогресс учеников цел после seed (state/reviews не тронуты) ----------

def test_c1_state_untouched(fresh_db):
    """seed_reference не затрагивает state и reviews."""
    import db as _db
    # У fresh_db уже есть UID в state. Записываем review.
    with _db._conn() as c:
        c.execute("INSERT INTO reviews (user_id,word_id,ts,remembered) VALUES (1,1,'2026-01-01',1)")
    data = _make_data()
    with _db._conn() as c:
        _db._seed_reference(c, data)
    with _db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reviews WHERE user_id=1").fetchone()[0]
    assert n == 1  # review цел
