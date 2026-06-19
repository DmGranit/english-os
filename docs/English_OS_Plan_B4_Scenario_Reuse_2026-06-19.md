# Фаза B4 — Перенос сегодняшних слов в сценарий (Такт-3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сценарий-сессия приоритетно вплетает слова, выученные СЕГОДНЯ (в encoding-уроке B2/B3), чтобы ученик использовал их в реальном контексте — замыкание дуги научения (предъявление → упражнение → перенос).

**Architecture:** Новый чистый селектор `db.scenario_target_words(uid, scenario, n, today_max, band)` — сегодняшние выученные слова (`recognized_today`, box≥2 promoted сегодня) ПЕРВЫМИ, добор словами темы сценария (`theme_words`), дедуп, кап. `_begin_scenario` использует его вместо только `theme_words`; `scn_words` (грунтовка каждой реплики, уже есть) теперь несёт сегодняшние слова. `start_learning` безопасен (вводит только `status='new'` — сегодняшние box≥2 не трогает).

**Tech Stack:** Python 3.13, sqlite3, python-telegram-bot, pytest. Без новых зависимостей. Стоит на B1–B3.

## Global Constraints

- **Такт-3 канона:** перенос свежих слов в продукцию (сценарий). Приоритет — сегодняшние выученные; добор — тема сценария. Нет сегодняшних (урока не было) → поведение прежнее (только тема) — graceful.
- **Не ломать SRS/слот:** `start_learning(ids, via="scenario")` уже введёт ТОЛЬКО `status='new'` (rowcount-gated на `WHERE status='new'`), поэтому сегодняшние box≥2 слова он не сбрасывает и слот NEW не закрывает (A1.3). Сохранить этот вызов как есть.
- **Сегодняшние слова берём из `recognized_today`** (box≥2 promoted сегодня — то, что ученик реально узнал на уроке; не box1-провалы).
- **Кап целевых слов сценария ~4–5** (как сейчас n=4) — не перегружать сцену.
- Сценарий по-прежнему ролевой, ошибки молча → ИТОГ; B4 меняет ТОЛЬКО состав целевых слов, не механику сценария.
- Тесты: `PYTHONUTF8=1 python -m pytest -q` зелёный (сейчас 432). Фикстура `fresh_db` (word_id 1=invest/Pitching, 2=deadline/Status update, 3=revenue/Pitching, 4=stakeholder/Negotiating).

---

## File Structure

- `db.py` — Modify: `scenario_target_words(...)` (рядом с `theme_words`/`recognized_today`).
- `bot.py` — Modify: `_begin_scenario` (~1168-1170) — источник `ids` через `scenario_target_words`.
- `tests/test_scenario_reuse.py` — Create.

---

## Task 1: `scenario_target_words` — сегодняшние слова приоритетно + добор темой

**Files:**
- Modify: `db.py` (рядом с `recognized_today`)
- Test: `tests/test_scenario_reuse.py`

**Interfaces:**
- Consumes: `recognized_today(uid, limit)`, `theme_words(axis, value, uid, n, band)`.
- Produces: `db.scenario_target_words(user_id, scenario, n=4, today_max=3, band=None) -> list[int]` — word_ids: сегодняшние выученные (до today_max) первыми, добор `theme_words("scn", scenario)`, дедуп, кап n.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_scenario_reuse.py
"""B4: перенос сегодняшних слов в сценарий — scenario_target_words + _begin_scenario."""
import datetime
import db
from conftest import UID


def _learned_today(db_, wid):
    today = datetime.date.today().isoformat()
    with db_._conn() as c:
        c.execute("""UPDATE state SET status='learning', box=2, promoted_at=?, next_review=?
                     WHERE user_id=? AND word_id=?""", (today, today, UID, wid))


def test_today_words_come_first(fresh_db):
    _learned_today(fresh_db, 2)                          # deadline выучен сегодня (тема Status update)
    ids = fresh_db.scenario_target_words(UID, "Pitching", n=4)
    assert ids[0] == 2                                   # сегодняшнее — первым, даже из другой темы
    assert any(i in ids for i in (1, 3))                 # добор словами Pitching (invest/revenue)
    assert len(ids) <= 4 and len(set(ids)) == len(ids)   # кап + без дублей


def test_graceful_when_no_today_words(fresh_db):
    ids = fresh_db.scenario_target_words(UID, "Pitching", n=4)
    assert all(isinstance(i, int) for i in ids)          # только тема, без падений
    assert 2 not in ids                                  # deadline не сегодняшний → не подмешан
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scenario_reuse.py -q`
Expected: FAIL — `AttributeError: ... 'scenario_target_words'`.

- [ ] **Step 3: Реализовать** (в `db.py`, рядом с `recognized_today`):

```python
def scenario_target_words(user_id=DEFAULT_USER, scenario=None, n=4, today_max=3, band=None):
    """B4 Такт-3: целевые слова сценария — приоритетно СЕГОДНЯШНИЕ выученные
    (recognized_today, box>=2 promoted сегодня), добор словами темы сценария. Дедуп, кап n.
    Нет сегодняшних → только тема (graceful)."""
    ids, seen = [], set()
    for w in recognized_today(user_id, limit=today_max):
        if w["word_id"] not in seen:
            seen.add(w["word_id"]); ids.append(w["word_id"])
    for w in theme_words("scn", scenario, user_id, n=n, band=band):
        if len(ids) >= n:
            break
        if w["word_id"] not in seen:
            seen.add(w["word_id"]); ids.append(w["word_id"])
    return ids[:n]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scenario_reuse.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Коммит**

```bash
git add db.py tests/test_scenario_reuse.py
git commit -m "feat(B4): scenario_target_words — сегодняшние слова приоритетно + добор темой"
```

---

## Task 2: `_begin_scenario` использует scenario_target_words

**Files:**
- Modify: `bot.py` (`_begin_scenario` ~1168-1170)
- Test: `tests/test_scenario_reuse.py`

**Interfaces:**
- Consumes: `db.scenario_target_words` (Task 1).
- Produces: `ctx.user_data["scn_words"]` включает сегодняшние выученные слова (грунтуются в каждую реплику через `_call`, уже есть).

- [ ] **Step 1: Прочитать текущий блок выбора слов**

Run: `sed -n '1167,1172p' bot.py`
Сейчас: `wd = db.theme_words("scn", scenario, uid, n=4, band=db.get_band(uid))` → `ids = [w["word_id"] for w in wd]` → `start_learning(ids, ...)` → `scn_words=ids`.

- [ ] **Step 2: Написать падающий тест** (мок llm/_ask и msg — проверяем состав scn_words):

```python
import asyncio, types
import bot, db

def test_begin_scenario_injects_today_words(fresh_db, monkeypatch):
    _learned_today(fresh_db, 2)                          # deadline выучен сегодня
    async def _noask(*a, **k): return None               # не дёргаем LLM
    monkeypatch.setattr(bot, "_ask", _noask)
    monkeypatch.setattr(db, "backup", lambda: None)
    sent = []
    async def reply(*a, **k): sent.append(a); return types.SimpleNamespace(reply_text=reply)
    msg = types.SimpleNamespace(reply_text=reply)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._begin_scenario(msg, ctx, UID, "Pitching"))
    assert 2 in ctx.user_data.get("scn_words", [])       # сегодняшнее слово — среди целевых
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scenario_reuse.py::test_begin_scenario_injects_today_words -v`
Expected: FAIL — `2` отсутствует в `scn_words` (сейчас только theme_words, deadline=Status update не входит в Pitching).

- [ ] **Step 4: Реализовать** (в `bot.py`, в `_begin_scenario`, заменить выбор `ids`):

Было:
```python
    wd = db.theme_words("scn", scenario, uid, n=4, band=db.get_band(uid))
    ids = [w["word_id"] for w in wd]
    db.start_learning(ids, uid, via="scenario")   # A1.3: не закрывает слот NEW в карте дня
```
Стало:
```python
    ids = db.scenario_target_words(uid, scenario, n=4, band=db.get_band(uid))  # B4: сегодняшние слова приоритетно
    db.start_learning(ids, uid, via="scenario")   # вводит только status='new'; сегодняшние box>=2 не трогает (A1.3)
```

- [ ] **Step 5: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_scenario_reuse.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Коммит**

```bash
git add bot.py tests/test_scenario_reuse.py
git commit -m "feat(B4): сценарий вплетает сегодняшние выученные слова (Такт-3)"
```

---

## Task 3: Регресс + живой smoke

- [ ] **Step 1: Полный набор**

Run: `PYTHONUTF8=1 python -m pytest -q`
Expected: PASS — было 432, стало 432 + 3 = **435 passed**, 0 регрессий.

- [ ] **Step 2: Дымовая проверка selector на боевой копии**

Run:
```bash
cp english_os.db .b4_check.db
ENGLISH_OS_DB=.b4_check.db PYTHONUTF8=1 python -c "import db; print('Pitching цели:', db.scenario_target_words(6634008084, 'Pitching', n=4))"
rm -f .b4_check.db
```
Expected: список из ≤4 word_id (сегодняшние выученные владельца, если есть, первыми; иначе слова Pitching).

---

## Self-Review (выполнено автором плана)

- **Покрытие спеки §3-такт3:** сценарий приоритетно вплетает сегодняшние слова ✅; graceful без сегодняшних ✅; кап ✅.
- **Безопасность SRS/слота:** `start_learning` only `status='new'` — сегодняшние box≥2 не сбрасываются (проверено по коду db.py:1640-1644), слот NEW не закрывается (via="scenario", A1.3) ✅.
- **Механика сценария не тронута:** меняется ТОЛЬКО состав `scn_words`; роль/ИТОГ/opener-closer — как есть ✅.
- **Плейсхолдеры:** нет — код дословно.
- **Согласованность:** `scenario_target_words(...)→list[int]`; `_begin_scenario` использует его; `scn_words` потребляется `_call` (существующая грунтовка). Имена совпадают между задачами и тестами.
- **Вне scope B4:** B5 (enrich += derivation + целевой смысл + set_derivation→bool); наполнение базы; расширение набора аффиксов.
- **Риск:** низкий — селектор чистый, вызов в `_begin_scenario` однострочный; REVIEW/lesson не тронуты.
