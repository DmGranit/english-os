"""Dm1: вставить 28 core verbs из reference["3. Core Verbs"] в content.
   Без LLM. Дубли (ask/feel/leave) — skip. Бэкап до записи.
   Запустить один раз: python dm1_seed_core_verbs.py
"""
import json, sqlite3, sys, db

sys.stdout.reconfigure(encoding="utf-8")

DNA_MAP = {
    "get": "Exchange",      "make": "Creation",    "take": "Ownership",
    "put": "Movement",      "come": "Movement",    "go": "Movement",
    "keep": "Commitment",   "hold": "Ownership",   "bring": "Exchange",
    "give": "Exchange",     "set": "Creation",     "run": "Leadership",
    "see": "Knowledge",     "look": "Knowledge",   "work": "Growth",
    "stand": "Commitment",  "have": "Ownership",   "do": "Creation",
    "find": "Knowledge",    "know": "Knowledge",   "want": "Vision",
    "ask": "Communication", "feel": "Knowledge",   "leave": "Movement",
    "become": "Change",     "mean": "Clarity",     "tell": "Communication",
    "try": "Commitment",    "let": "Decision",      "speak": "Communication",
    "play": "Growth",
}

LEVEL_MAP = {
    "get": "A1", "make": "A1", "take": "A1", "put": "A1", "come": "A1",
    "go": "A1",  "give": "A1", "see": "A1",  "look": "A1", "have": "A1",
    "do": "A1",  "find": "A1", "know": "A1", "want": "A1", "tell": "A1",
    "let": "A1", "keep": "A2", "hold": "A2", "bring": "A2", "set": "A2",
    "run": "A2", "work": "A2", "stand": "A2", "become": "A2", "mean": "A2",
    "try": "A2", "speak": "A2", "play": "A2", "ask": "A2", "feel": "A2",
    "leave": "B1",
}

with open("english_os.json", encoding="utf-8") as f:
    data = json.load(f)

verbs = data["reference"]["3. Core Verbs"]

con = sqlite3.connect("english_os.db")
existing = {r[0] for r in con.execute("SELECT word FROM content").fetchall()}

to_insert = [v for v in verbs if v["Глагол"] not in existing]
skipped   = [v["Глагол"] for v in verbs if v["Глагол"] in existing]

print(f"Всего в reference: {len(verbs)}")
print(f"Уже в базе (skip): {skipped}")
print(f"К вставке: {len(to_insert)} — {[v['Глагол'] for v in to_insert]}")

if not to_insert:
    print("Нечего вставлять — выход.")
    sys.exit(0)

# Бэкап до записи
db.backup()
print("Бэкап сделан.")

inserted = 0
with con:
    for v in to_insert:
        verb = v["Глагол"]
        ru   = v["Основной смысл"]
        # первый пример из «Типичные использования» (до запятой)
        examples_raw = v.get("Типичные использования", "")
        example = examples_raw.split(",")[0].strip() if examples_raw else None
        # фразовые через запятую → список
        phrasal_raw = v.get("Частые фразовые", "")
        phrasal = [p.strip() for p in phrasal_raw.split(",") if p.strip()] if phrasal_raw else []

        dna   = DNA_MAP.get(verb, "Growth")
        level = LEVEL_MAP.get(verb, "A2")
        freq, useful = 5, 5
        priority = freq * useful

        con.execute(
            """INSERT OR IGNORE INTO content
               (word, ru, dna_idea, root, family, collocations, phrasal,
                example, scenario, thinking_frame, register, level,
                ipa_uk, ipa_us, freq, useful, priority, origin)
               VALUES (?,?,?,NULL,'[]','[]',?,?,
                       'General', NULL, 'neutral', ?,
                       NULL, NULL, ?,?,?, 'dm1')""",
            (verb, ru, dna,
             json.dumps(phrasal, ensure_ascii=False),
             example, level,
             freq, useful, priority)
        )
        inserted += 1
        print(f"  + {verb:12} | {ru:30} | {dna:15} | {level}")

print(f"\nВставлено: {inserted} слов. Всего в базе: {con.execute('SELECT COUNT(*) FROM content').fetchone()[0]}")
