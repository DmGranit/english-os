# Фаза B2 — Богатое предъявление (encoding-рендер) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При первом контакте с новым словом показывать богатую карточку-узел: слово + перевод + ≤3 производных из гнезда с акцентом на аффиксе (где разбор надёжен) + пример — вместо тонкого «что значит X?».

**Architecture:** Отдельный текст-билдер `encoding_view(word_id)` в db.py (сосед `deep_view`, чистый, тестируемый — реш. D2), опирается на B1 (`decompose`/`affix_info`/`affixes_all`) и новый `detect_affix`. Wiring в bot.py показывает его первым шагом NEW-урока, затем существующая карточка-проверка. Maintenance/SRS не трогаем.

**Tech Stack:** Python 3.13, sqlite3, pytest. Без новых зависимостей. Стоит на B1 (`affix_ref`, `content.derivation`).

## Global Constraints

- **Корректность важнее полноты (ценность владельца).** Аффикс показываем с акцентом ТОЛЬКО когда разбор надёжен: (1) точный `db.decompose(word_id)` если у слова есть `derivation`; иначе (2) консервативный `detect_affix` — матч по `affix_ref` с защитой: остаток-основа ≥3 букв (иначе ложный сплит `table`→`-able`), самый длинный аффикс первым. Неуверенно → показываем производное БЕЗ аффикса. Никакой ложной морфологии.
- **Источник производных** — поле `content.family` (86% слов непусты; список строк). Разбор производного — `detect_affix` (эвристика) или его собственный `derivation` (после B5, точнее).
- **≤3 производных за первый контакт** (когнитивная нагрузка; реш. D4); остальное гнездо — по «🔍 Глубже» (`deep_view`, не трогаем). Насмотренность продольная.
- **Зависимость:** живой бот должен крутить код с B1 (миграция `content.derivation` применяется при `init_db`). На момент B2 боевая БД ещё без колонки — редеплой бота применит миграцию. `encoding_view` обязан работать и когда `derivation` пуст (graceful, через `detect_affix`).
- **Германское/фразовое** — аффиксный разбор не натягиваем (он там не работает); для таких слов карточка = слово+перевод+пример (degradation), это норма.
- Тесты: `PYTHONUTF8=1 python -m pytest -q` зелёный (сейчас 413). Фикстура `fresh_db`; ctx-стаб как в tests/test_mcq.py для async-врапперов.

---

## File Structure

- `db.py` — Modify:
  - `detect_affix(word, base=None)` — консервативное определение аффикса по `affix_ref` (рядом с `affix_info`/`decompose`).
  - `encoding_view(word_id)` — текст-билдер богатого предъявления (рядом с `deep_view`).
- `bot.py` — Modify:
  - NEW-урок: показать `encoding_view` первым контактом нового слова, затем — существующая карточка (кнопка «Дальше ▶»). Точка: `_enter_mode` ветка `mode=="new"` / новый хэндлер шага.
- `tests/test_encoding_render.py` — Create: тесты `detect_affix`, `encoding_view`, wiring.

---

## Task 1: `detect_affix` — консервативное определение аффикса

**Files:**
- Modify: `db.py` (рядом с `affix_info`)
- Test: `tests/test_encoding_render.py`

**Interfaces:**
- Consumes: `db.affix_info` (B1), `db.affixes_all` (B1).
- Produces: `db.detect_affix(word: str, base: str | None = None) -> dict | None` — возвращает `affix_info`-словарь распознанного аффикса (+ ключ `stem` = остаток-основа), или None если уверенного разбора нет.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_encoding_render.py
"""B2: богатое предъявление — detect_affix (консервативно) + encoding_view + wiring."""
import db
from conftest import UID


def test_detect_affix_confident_matches(fresh_db):
    assert fresh_db.detect_affix("deployment", "deploy")["affix"] == "-ment"
    assert fresh_db.detect_affix("strategic", "strategy")["affix"] == "-ic"
    assert fresh_db.detect_affix("unhappy")["affix"] == "un-"
    assert fresh_db.detect_affix("modernize")["affix"] == "-ize"

def test_detect_affix_rejects_false_splits(fresh_db):
    # 'table' заканчивается на 'able', но остаток 't' < 3 → не аффикс
    assert fresh_db.detect_affix("table") is None
    # нет уверенного аффикса в нашем наборе
    assert fresh_db.detect_affix("important") is None
    # пустое/короткое
    assert fresh_db.detect_affix("go") is None

def test_detect_affix_prefers_longest(fresh_db):
    # 'careless' → '-less' (4), не '-s'; '-s' и так не в наборе, но проверяем длину
    assert fresh_db.detect_affix("careless")["affix"] == "-less"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py::test_detect_affix_confident_matches -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'detect_affix'`.

- [ ] **Step 3: Реализовать `detect_affix`** (в `db.py`, рядом с `affix_info`):

```python
_MIN_STEM = 3   # остаток-основа короче → матч аффикса считаем ложным (table→-able)

def detect_affix(word, base=None):
    """Консервативно определить деривационный аффикс слова по affix_ref.
    Возвращает affix_info-словарь (+ 'stem'), либо None если разбор ненадёжен.
    Корректность важнее полноты: ложная морфология недопустима (B2)."""
    w = (word or "").strip().lower()
    if len(w) < _MIN_STEM + 1:
        return None
    suffixes = [a for a in affixes_all("suffix")]
    prefixes = [a for a in affixes_all("prefix")]
    # суффиксы: самый длинный первым; остаток-основа ≥ _MIN_STEM
    for a in sorted(suffixes, key=lambda x: -len(x["affix"])):
        suf = a["affix"].lstrip("-")
        if w.endswith(suf) and len(w) - len(suf) >= _MIN_STEM:
            return dict(a, stem=w[: len(w) - len(suf)])
    # приставки
    for a in sorted(prefixes, key=lambda x: -len(x["affix"])):
        pre = a["affix"].rstrip("-")
        if w.startswith(pre) and len(w) - len(pre) >= _MIN_STEM:
            return dict(a, stem=w[len(pre):])
    return None
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Коммит**

```bash
git add db.py tests/test_encoding_render.py
git commit -m "feat(B2): detect_affix — консервативное определение аффикса (защита от ложных сплитов)"
```

---

## Task 2: `encoding_view` — текст богатого предъявления

**Files:**
- Modify: `db.py` (рядом с `deep_view`)
- Test: `tests/test_encoding_render.py`

**Interfaces:**
- Consumes: `db.get_word`, `db.decompose` (B1), `db.detect_affix` (Task 1), `db.find_word_id`.
- Produces: `db.encoding_view(word_id: int) -> str` — многострочный текст: заголовок «слово — перевод», блок гнезда (≤3 производных с разбором аффикса где надёжно), строка примера. Пусто-безопасно (нет гнезда → слово+перевод+пример).

- [ ] **Step 1: Написать падающий тест**

```python
def test_encoding_view_shows_word_translation_example(fresh_db):
    # conftest: word_id 1 = invest / инвестировать, есть example? (в conftest example пуст —
    # проверяем устойчивость: заголовок и перевод обязательно есть)
    txt = fresh_db.encoding_view(1)
    assert "invest" in txt and "инвестировать" in txt

def test_encoding_view_decomposes_family_with_affix(fresh_db):
    # дать invest гнездо с явным деривационным членом
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment", "investor"]',))
    txt = fresh_db.encoding_view(1)
    assert "investment" in txt
    assert "-ment" in txt            # аффикс показан с акцентом (investment = invest + -ment)

def test_encoding_view_caps_nest_at_3(fresh_db):
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1",
                  ('["investment", "investor", "investing", "reinvest", "divest"]',))
    txt = fresh_db.encoding_view(1)
    shown = [m for m in ["investment", "investor", "investing", "reinvest", "divest"] if m in txt]
    assert len(shown) <= 3           # ≤3 производных за первый контакт (D4)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py::test_encoding_view_shows_word_translation_example -v`
Expected: FAIL — `AttributeError: ... 'encoding_view'`.

- [ ] **Step 3: Реализовать `encoding_view`** (в `db.py`, рядом с `deep_view`):

```python
ENCODING_NEST_MAX = 3   # ≤3 производных за первый контакт (D4); остальное — в «🔍 Глубже»

def encoding_view(word_id):
    """B2: богатое предъявление слова при первом контакте — узел + гнездо (≤3 производных
    с акцентом на аффиксе, где разбор надёжен) + пример. Деградирует мягко: нет гнезда/
    аффикса → слово+перевод+пример. Насмотренность копится продольно."""
    w = get_word(word_id)
    if not w:
        return ""
    lines = [f"🆕 {w['word']} — {w['ru']}"]
    # гнездо производных
    fam = w.get("family") or []
    shown = []
    for member in fam:
        if len(shown) >= ENCODING_NEST_MAX:
            break
        m = str(member).strip()
        if not m or m.lower() == w["word"].lower():
            continue
        # точный разбор, если член есть в базе со своим derivation; иначе эвристика
        info = None
        mid = find_word_id(m)
        if mid:
            d = decompose(mid)
            if d and d.get("affix"):
                info = {"affix": d["affix"], "meaning_ru": d.get("affix_meaning")}
        if not info:
            det = detect_affix(m, base=w["word"])
            if det:
                info = {"affix": det["affix"], "meaning_ru": det["meaning_ru"]}
        if info and info.get("affix"):
            acc = m.replace(info["affix"].strip("-"), f"·{info['affix'].strip('-')}", 1) \
                if info["affix"].startswith("-") else m
            tail = f" — {info['meaning_ru']}" if info.get("meaning_ru") else ""
            lines.append(f"   ↳ {acc}  ({info['affix']}{tail})")
        else:
            lines.append(f"   ↳ {m}")
        shown.append(m)
    if shown:
        lines.insert(1, "🌱 из того же гнезда:")
    # пример
    if w.get("example"):
        lines.append(f"📝 {w['example']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Коммит**

```bash
git add db.py tests/test_encoding_render.py
git commit -m "feat(B2): encoding_view — богатое предъявление (гнездо ≤3 + разбор аффикса + пример)"
```

---

## Task 3: Wiring — показать encoding_view первым контактом NEW-урока

**Files:**
- Modify: `bot.py` (`_enter_mode` ветка `mode=="new"` ~810-833; новый callback-шаг «enc:next»)
- Test: `tests/test_encoding_render.py`

**Interfaces:**
- Consumes: `db.encoding_view` (Task 2).
- Produces: поведение — при входе в NEW-урок первая выдача = `encoding_view` первого слова + кнопка «Дальше ▶» (callback `enc:next`); по нажатию показывается существующая карточка-проверка (`_card_payload`). Остальные слова урока — без изменений (B3 добавит предъявление каждому).

- [ ] **Step 1: Прочитать текущую ветку `mode=="new"`**

Run: `sed -n '809,834p' bot.py`
Понять: после `promote_new` строится box1-дека и сразу `_card_payload`+`out`. Вставляем шаг предъявления ПЕРЕД первой карточкой.

- [ ] **Step 2: Написать падающий тест** (ctx-стаб, как в test_mcq.py):

```python
import asyncio, types
import bot

def test_new_lesson_starts_with_encoding_view(fresh_db, monkeypatch):
    # один новый слово-кандидат
    monkeypatch.setattr(bot.db, "promote_new", lambda uid: [bot.db.get_word(1)])
    sent = []
    async def out(text, markup=None): sent.append((text, markup))
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._enter_mode(out, ctx, UID, "new"))
    assert any("invest" in (t or "") and "🆕" in (t or "") for t, _ in sent)  # предъявление первым
    assert ctx.user_data.get("enc_pending")                                   # помечен шаг проверки
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py::test_new_lesson_starts_with_encoding_view -v`
Expected: FAIL (предъявления нет; `enc_pending` не ставится).

- [ ] **Step 4: Реализовать wiring** (в `bot.py`, в ветке `mode=="new"`, ПОСЛЕ построения деки `review_queue`/`review_pos`/счётчиков, ВМЕСТО немедленного `text, kb = _card_payload(...)`):

```python
        first_wid = wids[0]
        ctx.user_data["enc_pending"] = True       # после «Дальше ▶» — карточка-проверка
        await out(db.encoding_view(first_wid),
                  InlineKeyboardMarkup([[InlineKeyboardButton("Дальше ▶", callback_data="enc:next")]]))
        return
```

Добавить callback-хэндлер `on_enc_next` (рядом с другими `on_*`) и зарегистрировать `CallbackQueryHandler(on_enc_next, pattern="^enc:next$")`:

```python
async def on_enc_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = _learner(update)
    ctx.user_data.pop("enc_pending", None)
    text, kb = _card_payload(ctx, uid)            # существующая карточка-проверка первого слова
    await q.edit_message_text(text, reply_markup=kb)
```

- [ ] **Step 5: Запустить тест — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_encoding_render.py -q`
Expected: PASS (7 passed).

- [ ] **Step 6: Коммит**

```bash
git add bot.py tests/test_encoding_render.py
git commit -m "feat(B2): NEW-урок начинается с богатого предъявления (encoding_view + «Дальше»)"
```

---

## Task 4: Регресс

- [ ] **Step 1: Полный набор**

Run: `PYTHONUTF8=1 python -m pytest -q`
Expected: PASS — было 413, стало 413 + 7 = **420 passed**, 0 регрессий.

- [ ] **Step 2: Дымовая проверка encoding_view на боевых данных (копия, после init_db-миграции)**

Run:
```bash
cp english_os.db .b2_check.db
ENGLISH_OS_DB=.b2_check.db PYTHONUTF8=1 python -c "import db; db.init_db(); print(db.encoding_view(1)[:300])"
rm -f .b2_check.db
```
Expected: непустой текст с «🆕», переводом и (если у слова 1 есть family) блоком гнезда.

---

## Self-Review (выполнено автором плана)

- **Покрытие спеки §3-такт1 + §5 + D2/D4:** отдельный путь (не `_card_payload`) ✅ (D2); ≤3 производных ✅ (D4, тест `caps_nest_at_3`); разбор аффикса с акцентом ✅; пример ✅; насмотренность (показ в предъявлении) ✅; германское/пусто → degradation ✅.
- **Корректность (ценность владельца):** `detect_affix` консервативен — защита `_MIN_STEM` от ложных сплитов, тест `rejects_false_splits` (table/important) ✅; точный `decompose` приоритетнее эвристики ✅.
- **Зависимость B5/редеплой** зафиксирована в Global Constraints; `encoding_view` работает при пустом `derivation` (через detect_affix) ✅.
- **Плейсхолдеры:** нет — код приведён дословно.
- **Согласованность типов:** `detect_affix→dict(+stem)|None`; `encoding_view→str`; `enc_pending` флаг + `enc:next` callback — имена совпадают между задачами и тестами.
- **Вне scope B2 (следующие планы):** B3 (упражнение-микс вместо/после MCQ-проверки; предъявление каждому слову урока), B4 (сценарий), B5 (enrich += derivation + целевой смысл + set_derivation→bool).
