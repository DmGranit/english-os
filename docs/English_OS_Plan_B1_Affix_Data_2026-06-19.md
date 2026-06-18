# Фаза B1 — Фундамент данных аффиксного слоя — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Завести данные морфологического слоя — таблицу `affix_ref` (выверенные деривационные аффиксы) и поле `content.derivation` (разбор «база+аффикс»), плюс лукап-функции, на которых будут стоять предъявление (B2) и упражнение (B3).

**Architecture:** Зеркалим существующий паттерн справочников `db.py`: объявление в строке `SCHEMA` (executescript при `init_db`), идемпотентный `_seed_*` через `INSERT OR IGNORE` из модульной константы-сида, лёгкая колоночная миграция в `_migrate` через `PRAGMA table_info`. Никакой генерации слов по правилам — только материализованные выверенные данные (реш. спеки, §1).

**Tech Stack:** Python 3.13, sqlite3 (stdlib), pytest. Без новых зависимостей.

## Global Constraints

- SQLite — единственный SSOT; пишет только код через `db._conn()`. Повторный сид безопасен (идемпотентность через `INSERT OR IGNORE`).
- Аффиксы — только **деривационные** (меняют смысл/часть речи). Флексии (-s, -ed, -ing, сравнит. -er/-est) НЕ включать (это грамматика, не множитель). Источник выверки: Bauer & Nation 1993 (продуктивность), частотность приставок (Sowell/White).
- Миграции не идемпотентны на `ALTER` — проверять наличие колонки перед `ALTER` (паттерн `_migrate`).
- Тесты: `PYTHONUTF8=1 python -m pytest -q` должен оставаться зелёным (сейчас 408 passed). Фикстура `fresh_db` (tests/conftest.py) даёт чистую БД во временном файле.
- Кодировка вывода в БД — UTF-8; русские строки в сидах допустимы (схема и данные хранят `→`, кириллицу).

---

## File Structure

- `db.py` — Modify:
  - `SCHEMA` строка: добавить `CREATE TABLE IF NOT EXISTS affix_ref (...)` (рядом с `irregular_ref`, ~стр. 149-154).
  - Новая модульная константа `_AFFIX_SEED` (рядом с `_IRREGULAR_SEED`).
  - Новая `_seed_affixes(c)` (по образцу `_seed_irregular`, стр. 332-336); вызвать в `init_db` (стр. 318-323).
  - `_migrate(c)` (стр. 338): добавить ALTER `content.derivation` если колонки нет.
  - Лукапы: `affix_info(affix)`, `affixes_all(kind=None)`, `set_derivation(word_id, base, affix, gloss=None)`, `decompose(word_id)` (рядом с `irregular_for_word`, ~стр. 909).
- `tests/test_affix_data.py` — Create: тесты сида, миграции, лукапов.

---

## Task 1: `affix_ref` — таблица, сид, лукап

**Files:**
- Modify: `db.py` (SCHEMA ~149; `_AFFIX_SEED` рядом с `_IRREGULAR_SEED`; `_seed_affixes` рядом с `_seed_irregular` ~332; вызов в `init_db` ~323; `affix_info`/`affixes_all` ~909)
- Test: `tests/test_affix_data.py`

**Interfaces:**
- Produces: `db.affix_info(affix: str) -> dict | None` (ключи: `affix, kind, meaning_ru, function, examples`); `db.affixes_all(kind: str | None = None) -> list[dict]` (kind ∈ {"prefix","suffix"} или None=все).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_affix_data.py
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
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_affix_data.py -q`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'affix_info'`.

- [ ] **Step 3: Добавить таблицу в SCHEMA** (в `db.py`, в строке `SCHEMA` сразу после блока `irregular_ref`, перед `cando_words`):

```sql
CREATE TABLE IF NOT EXISTS affix_ref (
    affix TEXT PRIMARY KEY,          -- "un-", "-ize"
    kind TEXT NOT NULL,              -- prefix | suffix
    meaning_ru TEXT NOT NULL,        -- значение по-русски
    function TEXT,                   -- смена части речи / роль
    examples TEXT,                   -- 2-3 прозрачных примера через запятую
    note TEXT
);
```

- [ ] **Step 4: Добавить константу-сид `_AFFIX_SEED`** (в `db.py`, рядом с `_IRREGULAR_SEED`). Кортежи `(affix, kind, meaning_ru, function, examples)`:

```python
# Деривационные аффиксы (Bauer & Nation 1993 — продуктивность; приставки — Sowell/White).
# Флексии (-s/-ed/-ing/сравнит. -er/-est) НЕ входят: это грамматика, не множитель словаря.
_AFFIX_SEED = [
    # --- приставки ---
    ("un-",     "prefix", "отрицание / обратное действие", "часть речи не меняется", "unhappy, undo, unfair"),
    ("re-",     "prefix", "снова / назад",                  "часть речи не меняется", "rewrite, return, rebuild"),
    ("in-",     "prefix", "отрицание (не-); варианты im-/il-/ir-", "→ прил.", "invisible, impossible, illegal, irregular"),
    ("dis-",    "prefix", "отрицание / противоположность",  "часть речи не меняется", "disagree, disappear, dishonest"),
    ("pre-",    "prefix", "до / заранее",                    "часть речи не меняется", "preview, prepare, predict"),
    ("mis-",    "prefix", "неправильно / ошибочно",          "часть речи не меняется", "misunderstand, mislead, misuse"),
    ("over-",   "prefix", "сверх / чрезмерно",               "часть речи не меняется", "overwork, overload, overcome"),
    ("under-",  "prefix", "недо- / под",                     "часть речи не меняется", "underestimate, undergo, underline"),
    ("out-",    "prefix", "превзойти / вне",                 "часть речи не меняется", "outperform, outgrow, outcome"),
    ("sub-",    "prefix", "под / ниже",                      "часть речи не меняется", "submarine, subway, substandard"),
    ("inter-",  "prefix", "между / взаимно",                 "часть речи не меняется", "international, interact, interface"),
    ("trans-",  "prefix", "через / пере-",                   "часть речи не меняется", "transfer, transform, translate"),
    ("de-",     "prefix", "снятие / обратное",               "часть речи не меняется", "devalue, decode, deactivate"),
    ("non-",    "prefix", "не- / отсутствие",                "часть речи не меняется", "nonsense, nonstop, nonprofit"),
    ("anti-",   "prefix", "против",                          "часть речи не меняется", "antivirus, antisocial, antibody"),
    ("co-",     "prefix", "совместно",                       "часть речи не меняется", "cooperate, coworker, coordinate"),
    ("super-",  "prefix", "сверх / над",                     "часть речи не меняется", "supervise, superpower, supermarket"),
    ("ex-",     "prefix", "бывший / из",                     "часть речи не меняется", "ex-partner, export, exit"),
    ("en-",     "prefix", "придавать качество",              "→ глагол",               "enable, enrich, encourage"),
    ("fore-",   "prefix", "заранее / перед",                 "часть речи не меняется", "forecast, foresee, foreword"),
    # --- суффиксы ---
    ("-er",     "suffix", "тот, кто делает (деятель); вариант -or", "глагол → сущ.", "teacher, worker, actor"),
    ("-tion",   "suffix", "действие / состояние; вариант -sion",    "глагол → сущ.", "action, creation, decision"),
    ("-ment",   "suffix", "действие / результат",            "глагол → сущ.",          "agreement, development, payment"),
    ("-ness",   "suffix", "качество / состояние",            "прил. → сущ.",           "happiness, kindness, darkness"),
    ("-ity",    "suffix", "качество / состояние",            "прил. → сущ.",           "ability, security, activity"),
    ("-ance",   "suffix", "состояние / качество; вариант -ence", "→ сущ.",             "importance, performance, difference"),
    ("-ize",    "suffix", "делать / превращать; вариант -ise", "сущ./прил. → глагол",  "modernize, organize, realize"),
    ("-ify",    "suffix", "делать / придавать свойство",     "→ глагол",               "simplify, clarify, justify"),
    ("-able",   "suffix", "способный / -имый; вариант -ible","глагол → прил.",         "readable, comfortable, possible"),
    ("-ful",    "suffix", "полный чего-то",                  "сущ. → прил.",           "helpful, useful, careful"),
    ("-less",   "suffix", "без / лишённый",                  "сущ. → прил.",           "useless, hopeless, careless"),
    ("-al",     "suffix", "относящийся к",                   "сущ. → прил.",           "natural, personal, central"),
    ("-ous",    "suffix", "обладающий качеством",            "сущ. → прил.",           "dangerous, famous, nervous"),
    ("-ive",    "suffix", "склонный / имеющий свойство",     "глагол → прил.",         "active, creative, effective"),
    ("-ic",     "suffix", "относящийся к",                   "сущ. → прил.",           "basic, economic, specific"),
    ("-ly",     "suffix", "образ действия (наречие)",        "прил. → нареч.",         "quickly, clearly, easily"),
    ("-ist",    "suffix", "человек (профессия/приверженец)", "→ сущ.",                 "artist, scientist, specialist"),
    ("-ism",    "suffix", "учение / явление",                "→ сущ.",                 "tourism, realism, criticism"),
]
```

- [ ] **Step 5: Добавить `_seed_affixes` и вызвать в `init_db`** (в `db.py`, по образцу `_seed_irregular`):

```python
def _seed_affixes(c):
    """B1: заполнить affix_ref деривационными аффиксами (идемпотентно)."""
    for affix, kind, meaning_ru, function, examples in _AFFIX_SEED:
        c.execute("""INSERT OR IGNORE INTO affix_ref (affix, kind, meaning_ru, function, examples)
                     VALUES (?,?,?,?,?)""", (affix, kind, meaning_ru, function, examples))
```

В `init_db` добавить вызов после `_seed_irregular(c)`:

```python
        _seed_irregular(c)
        _seed_affixes(c)
```

- [ ] **Step 6: Добавить лукапы** (в `db.py`, рядом с `irregular_for_word`):

```python
def affix_info(affix):
    """Карточка аффикса из affix_ref (для разбора словообразования). None, если нет."""
    with _conn() as c:
        r = c.execute("""SELECT affix, kind, meaning_ru, function, examples, note
                         FROM affix_ref WHERE affix=?""", (affix,)).fetchone()
        return dict(r) if r else None

def affixes_all(kind=None):
    """Все аффиксы (или одного типа prefix|suffix) — для насмотренности/витрины."""
    with _conn() as c:
        if kind:
            rows = c.execute("""SELECT affix, kind, meaning_ru, function, examples
                                FROM affix_ref WHERE kind=? ORDER BY affix""", (kind,)).fetchall()
        else:
            rows = c.execute("""SELECT affix, kind, meaning_ru, function, examples
                                FROM affix_ref ORDER BY kind, affix""").fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 7: Запустить тест — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_affix_data.py -q`
Expected: PASS (2 passed).

- [ ] **Step 8: Коммит**

```bash
git add db.py tests/test_affix_data.py
git commit -m "feat(B1): affix_ref — деривационные аффиксы (сид + лукапы)"
```

---

## Task 2: `content.derivation` — миграция и доступ

**Files:**
- Modify: `db.py` (`_migrate` ~338; `set_derivation`/`decompose` рядом с `affix_info`)
- Test: `tests/test_affix_data.py`

**Interfaces:**
- Consumes: `db.affix_info` (из Task 1).
- Produces: `db.set_derivation(word_id: int, base: str, affix: str, gloss: str | None = None) -> None` (пишет JSON в `content.derivation`); `db.decompose(word_id: int) -> dict | None` (возвращает `{"base","affix","gloss"}` + подмешивает `affix_meaning` из `affix_ref`, или None).

- [ ] **Step 1: Написать падающий тест**

```python
def test_derivation_roundtrip_and_affix_join(fresh_db):
    # слово 1 = invest (есть в conftest WORDS); привяжем как производное (демо-связка)
    fresh_db.set_derivation(1, base="vest", affix="in-", gloss="вкладывать внутрь")
    d = fresh_db.decompose(1)
    assert d["base"] == "vest" and d["affix"] == "in-"
    assert d["gloss"] == "вкладывать внутрь"
    assert "отрицание" in (d.get("affix_meaning") or "")   # подмешан meaning_ru из affix_ref (in-)

def test_decompose_none_when_absent(fresh_db):
    assert fresh_db.decompose(2) is None                    # у deadline нет derivation
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_affix_data.py::test_derivation_roundtrip_and_affix_join -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'set_derivation'`.

- [ ] **Step 3: Добавить миграцию колонки в `_migrate`** (в `db.py`, внутри `_migrate`, после блока проверки `reviews`-колонок; добавить проверку content):

```python
    ccols = {r["name"] for r in c.execute("PRAGMA table_info(content)").fetchall()}
    if ccols and "derivation" not in ccols:   # B1: разбор «база+аффикс» производного слова (JSON)
        c.execute("ALTER TABLE content ADD COLUMN derivation TEXT")
```

- [ ] **Step 4: Добавить `set_derivation` и `decompose`** (в `db.py`, рядом с `affix_info`):

```python
def set_derivation(word_id, base, affix, gloss=None):
    """B1: записать разбор словообразования производного слова (база+аффикс) как JSON."""
    payload = json.dumps({"base": base, "affix": affix, "gloss": gloss}, ensure_ascii=False)
    with _conn() as c:
        c.execute("UPDATE content SET derivation=? WHERE word_id=?", (payload, word_id))

def decompose(word_id):
    """B1: разбор «база+аффикс» слова + значение аффикса из affix_ref. None, если нет."""
    with _conn() as c:
        r = c.execute("SELECT derivation FROM content WHERE word_id=?", (word_id,)).fetchone()
    if not r or not r["derivation"]:
        return None
    try:
        d = json.loads(r["derivation"])
    except (ValueError, TypeError):
        return None
    info = affix_info(d.get("affix")) if d.get("affix") else None
    if info:
        d["affix_meaning"] = info["meaning_ru"]
    return d
```

> Примечание: `json` уже импортирован в `db.py` (используется в pending/payload). Если нет — добавить `import json` в шапку.

- [ ] **Step 5: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_affix_data.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Коммит**

```bash
git add db.py tests/test_affix_data.py
git commit -m "feat(B1): content.derivation — разбор база+аффикс (миграция + set/decompose)"
```

---

## Task 3: Регресс-проверка всего набора

**Files:** —

- [ ] **Step 1: Прогнать полный набор**

Run: `PYTHONUTF8=1 python -m pytest -q`
Expected: PASS — было 408, стало 408 + 4 новых = **412 passed**, 0 регрессий.

- [ ] **Step 2: Проверить идемпотентность сида на боевой схеме (копия)**

Run:
```bash
ENGLISH_OS_DB=.b1_check.db PYTHONUTF8=1 python -c "import db; db.init_db(); db.init_db(); print('affixes:', len(db.affixes_all()), '| -ize:', db.affix_info('-ize')['meaning_ru'])"
rm -f .b1_check.db
```
Expected: `affixes: 38 | -ize: делать / превращать; вариант -ise` (двойной init_db не плодит дублей).

- [ ] **Step 3: Коммит (если были правки) — иначе пропустить**

```bash
git status --short
```

---

## Self-Review (выполнено автором плана)

- **Покрытие спеки §2:** `affix_ref` (Task 1) ✅; `content.derivation` (Task 2) ✅; лукапы (`affix_info`/`affixes_all`/`decompose`) ✅; переиспользование root_ref/family — не трогаем (как в спеке).
- **Флексии исключены** (Global Constraints + тест `test_affixes_all_filters_by_kind`) ✅ — соответствует реш. D1.
- **Плейсхолдеры:** нет — весь сид и код приведены дословно.
- **Согласованность типов:** `affix_info`→dict с ключами `affix/kind/meaning_ru/function/examples/note`; `decompose`→dict `base/affix/gloss/affix_meaning`; `affixes_all(kind)` фильтр — имена совпадают между задачами и тестами.
- **Вне scope B1 (следующие планы):** B2 (encoding-рендер предъявления), B3 (упражнение-микс), B4 (сценарий), B5 (enrich += derivation + целевой смысл). B1 не меняет поведение для пользователя — это фундамент данных, как договорено («плясать от хранения»).
