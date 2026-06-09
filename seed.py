"""Одноразовый seed: контент из JSON в SQLite + завести state пользователю."""
import db
n = db.seed_from_json("english_os.json", reset=True)
db.ensure_user_state()
print(f"Залито {n} слов. Прогресс: {db.progress()}")
