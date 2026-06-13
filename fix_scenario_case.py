"""Фикс дубля сценария: 'apologizing' (строчные) -> 'Apologizing'.
Запустить один раз: python fix_scenario_case.py
"""
import sqlite3, sys, db

sys.stdout.reconfigure(encoding="utf-8")

con = sqlite3.connect("english_os.db")

# Диагностика
rows = con.execute(
    "SELECT word, scenario FROM content WHERE scenario='apologizing'"
).fetchall()

if not rows:
    print("Дубля нет — уже чисто, выход.")
    sys.exit(0)

print(f"Найдено {len(rows)} слов с scenario='apologizing':")
for word, scn in rows:
    print(f"  {word!r} → {scn!r}")

# Бэкап до записи
db.backup()
print("Бэкап сделан.")

# Фикс
with con:
    n = con.execute(
        "UPDATE content SET scenario='Apologizing' WHERE scenario='apologizing'"
    ).rowcount
print(f"Обновлено {n} строк: 'apologizing' → 'Apologizing'.")

# Проверка
remaining = con.execute(
    "SELECT COUNT(*) FROM content WHERE scenario='apologizing'"
).fetchone()[0]
total_correct = con.execute(
    "SELECT COUNT(*) FROM content WHERE scenario='Apologizing'"
).fetchone()[0]
print(f"Осталось 'apologizing': {remaining} (должно быть 0)")
print(f"Итого 'Apologizing': {total_correct}")
