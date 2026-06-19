# Фаза B5 — Качество конвейера enrich (целевой смысл + hardening) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поднять качество карточек конвейера `enrich.py` под наполнение базы: передавать ЦЕЛЕВОЙ СМЫСЛ слова из сбора (чинит сенс-дрейф `patient→терпеливый`, `medicine→медицина`), и ужесточить `validate` (не выдуманный root-«null», не форсить бизнес-фрейм/идею на бытовое A1-слово). Плюс перенос-гигиена `set_derivation`→bool.

**Architecture:** Точечные правки `enrich.py` (`enrich_word` принимает `sense`; `run` прокидывает `senses`; `validate`/`enrich_word` нормализуют root/dna_idea/thinking_frame по разрешённым спискам) + `db.set_derivation`→bool. QA-«стерео» (`qa_payload`) и сам пайплайн `run`→`pending` не меняем по структуре.

**Tech Stack:** Python 3.13, sqlite3, pytest. Без новых зависимостей. Стоит на B1.

## Global Constraints

- **Целевой смысл — опционален, дефолт прежнее поведение.** Нет `sense` → enrich работает как сейчас (backward-compat). `run(..., senses=None)`.
- **Корректность контента (ценность владельца):** root указывается ТОЛЬКО при уверенности — строки «null»/«none»/пусто → None (не выдуманная латынь). `dna_idea`/`thinking_frame` берутся ТОЛЬКО из существующей таксономии (переиспользуем `_allowed()`); не из списка → None (фрейм/идея — необязательные L1/L7, лучше пусто, чем форс бизнес-фрейма на «water»).
- **QA-«стерео» сохраняется** как есть (второй ИИ-проход, drop_root). Морфо-проверку family и генерацию точного `derivation` НЕ делаем здесь — отложено в B5.2 (encoding работает на консервативной эвристике B2, ложной морфологии нет).
- `set_derivation` (B1) сейчас никем не вызывается; делаем bool-возврат (rowcount) — гигиена под будущего вызывающего (B5.2/enrich), без смены поведения существующего (нет вызывающих).
- Тесты: `PYTHONUTF8=1 python -m pytest -q` зелёный (сейчас 435). Паттерн моков llm — как в `tests/test_batch_enrich.py` (`_qa_aware`, `_chat_seq`, `_fake_payload`).

---

## File Structure

- `db.py` — Modify: `set_derivation` → возвращать bool.
- `enrich.py` — Modify: `enrich_word` (+`sense`), `run` (+`senses`), `validate` (root null→None), `enrich_word` нормализация `dna_idea`/`thinking_frame` по `_allowed()`.
- `tests/test_enrich_quality.py` — Create.

---

## Task 1: `set_derivation` → bool (перенос-гигиена B1)

**Files:**
- Modify: `db.py` (`set_derivation` ~988)
- Test: `tests/test_enrich_quality.py`

**Interfaces:**
- Produces: `db.set_derivation(word_id, base, affix, gloss=None) -> bool` — True если строка обновлена (слово существует), иначе False.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_enrich_quality.py
"""B5: качество enrich — целевой смысл + hardening validate; set_derivation→bool."""
import json
import db, enrich, llm
from conftest import UID


def test_set_derivation_returns_bool(fresh_db):
    assert fresh_db.set_derivation(1, base="vest", affix="in-") is True     # word_id 1 = invest есть
    assert fresh_db.set_derivation(99999, base="x", affix="-y") is False    # нет такого слова
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py::test_set_derivation_returns_bool -v`
Expected: FAIL (сейчас `set_derivation` возвращает None).

- [ ] **Step 3: Реализовать** (в `db.py`, `set_derivation`):

```python
def set_derivation(word_id, base, affix, gloss=None):
    """B1/B5: записать разбор словообразования (база+аффикс) как JSON. Возвращает
    True, если слово существует и строка обновлена, иначе False (граф. skip)."""
    payload = json.dumps({"base": base, "affix": affix, "gloss": gloss}, ensure_ascii=False)
    with _conn() as c:
        cur = c.execute("UPDATE content SET derivation=? WHERE word_id=?", (payload, word_id))
        return cur.rowcount > 0
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Коммит**

```bash
git add db.py tests/test_enrich_quality.py
git commit -m "feat(B5): set_derivation возвращает bool (rowcount)"
```

---

## Task 2: `enrich_word` целевой смысл + `run` проводка

**Files:**
- Modify: `enrich.py` (`enrich_word` ~165, `run` ~176)
- Test: `tests/test_enrich_quality.py`

**Interfaces:**
- Produces: `enrich.enrich_word(word, ideas, frames, scenarios, scenario_override=None, sense=None)` — при `sense` добавляет в user-сообщение «Целевой смысл: <sense>». `enrich.run(words, user_id=db.DEFAULT_USER, scenario=None, senses=None)` — `senses`: dict `{word: sense}`; прокидывает в `enrich_word`.

- [ ] **Step 1: Написать падающий тест**

```python
def _qa_ok(generate):
    def chat(system, messages, **k):
        if "рецензент" in system:
            return '{"ok": true, "drop_root": false, "reason": "ok"}'
        return generate(system, messages)
    return chat

def test_sense_passed_into_generation_prompt(fresh_db, monkeypatch):
    captured = {}
    def gen(system, messages):
        captured["user"] = messages[-1]["content"]
        return json.dumps({"word": "patient", "ru": "пациент", "scenario": "Universal", "level": "A2"})
    monkeypatch.setattr(llm, "chat", _qa_ok(gen))
    enrich.run(["patient"], user_id=UID, senses={"patient": "пациент (роль в клинике)"})
    assert "пациент (роль в клинике)" in captured["user"]      # смысл дошёл до промпта
    assert json.loads(fresh_db.list_pending(UID)[0]["payload"])["ru"] == "пациент"

def test_no_sense_keeps_old_behaviour(fresh_db, monkeypatch):
    captured = {}
    def gen(system, messages):
        captured["user"] = messages[-1]["content"]
        return json.dumps({"word": "menu", "ru": "меню", "scenario": "Universal", "level": "A2"})
    monkeypatch.setattr(llm, "chat", _qa_ok(gen))
    enrich.run(["menu"], user_id=UID)
    assert "Целевой смысл" not in captured["user"]              # без sense — старый промпт
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py::test_sense_passed_into_generation_prompt -v`
Expected: FAIL — `run() got an unexpected keyword argument 'senses'`.

- [ ] **Step 3: Реализовать** (в `enrich.py`):

`enrich_word` — добавить параметр и инъекцию смысла в user-сообщение:
```python
def enrich_word(word, ideas, frames, scenarios, scenario_override=None, sense=None):
    user = f"Слово: {word}"
    if sense:
        user += f"\nЦелевой смысл: {sense} — переведи и разбери ИМЕННО этот смысл, не доминирующий."
    raw = llm.chat(_system_prompt(ideas, frames, scenarios),
                   [{"role": "user", "content": user}])
    payload = validate(_parse(raw), word)
    if payload:
        if scenario_override:
            payload["scenario"] = scenario_override
        elif payload.get("scenario") not in scenarios:
            payload["scenario"] = "Universal"
    return payload
```

`run` — добавить `senses` и прокинуть:
```python
def run(words, user_id=db.DEFAULT_USER, scenario=None, senses=None):
    db.init_db()
    ideas, frames, scenarios = _allowed()
    senses = senses or {}
    res = {"added": 0, "skipped": 0, "failed": 0}
    for w in words:
        w = w.strip()
        if not w:
            continue
        if db.find_word_id(w) is not None:
            res["skipped"] += 1
            print(f"= {w}: уже в базе, пропуск")
            continue
        payload = enrich_word(w, ideas, frames, scenarios,
                              scenario_override=scenario, sense=senses.get(w))
        # ... остальное БЕЗ изменений (qa_payload, add_pending, печать, notify)
```
(Сохранить весь остаток тела `run` как есть — qa_payload, add_pending, счётчики, `_notify_owner`.)

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Коммит**

```bash
git add enrich.py tests/test_enrich_quality.py
git commit -m "feat(B5): enrich_word принимает целевой смысл — чинит сенс-дрейф наполнения"
```

---

## Task 3: `validate`/`enrich_word` hardening (root null, idea/frame по таксономии)

**Files:**
- Modify: `enrich.py` (`validate` ~104, `enrich_word` нормализация)
- Test: `tests/test_enrich_quality.py`

**Interfaces:**
- Consumes: `_allowed()` (ideas/frames/scenarios).
- Produces: `validate` нормализует root-строки «null»/«none»/пусто → None. `enrich_word` после `validate`: `dna_idea ∉ ideas` → None; `thinking_frame ∉ frames` → None.

- [ ] **Step 1: Написать падающий тест**

```python
def test_validate_strips_null_root_and_bogus_idea_frame(fresh_db, monkeypatch):
    bogus = json.dumps({"word": "water", "ru": "вода", "root": "null",
                        "dna_idea": "НесуществующаяИдея", "thinking_frame": "НесуществующийФрейм",
                        "scenario": "Universal", "level": "A1"})
    monkeypatch.setattr(llm, "chat", _qa_ok(lambda s, m: bogus))
    enrich.run(["water"], user_id=UID)
    p = json.loads(fresh_db.list_pending(UID)[0]["payload"])
    assert p["root"] is None                       # «null»-строка → None
    assert p["dna_idea"] is None                   # идея не из таксономии → None (не форсим)
    assert p["thinking_frame"] is None             # фрейм не из таксономии → None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py::test_validate_strips_null_root_and_bogus_idea_frame -v`
Expected: FAIL — root=="null" сохраняется строкой; dna_idea/frame не нормализуются.

- [ ] **Step 3: Реализовать** (в `enrich.py`):

В `validate`, заменить строку root:
```python
    raw_root = (payload.get("root") or "").strip()
    root = None if raw_root.lower() in ("", "null", "none", "—", "-") else raw_root
```
и использовать `root` в возвращаемом dict (`"root": root,`).

В `enrich_word`, после блока scenario-fallback добавить нормализацию идеи/фрейма по таксономии:
```python
    if payload:
        if payload.get("dna_idea") not in ideas:
            payload["dna_idea"] = None
        if payload.get("thinking_frame") not in frames:
            payload["thinking_frame"] = None
```
(Поставить ДО/после scenario-fallback — порядок не важен; idea/frame независимы от scenario.)

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_enrich_quality.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Коммит**

```bash
git add enrich.py tests/test_enrich_quality.py
git commit -m "feat(B5): validate hardening — root «null»→None, idea/frame по таксономии (не форсим)"
```

---

## Task 4: Регресс

- [ ] **Step 1: Полный набор**

Run: `PYTHONUTF8=1 python -m pytest -q`
Expected: PASS — было 435, стало 435 + 4 = **439 passed**, 0 регрессий. (Существующие тесты `test_batch_enrich.py` — `run`/`enrich_word`/`validate` — должны остаться зелёными: новые параметры опциональны, дефолт = старое поведение.)

- [ ] **Step 2: Дымовая проверка целевого смысла (без LLM-ключа — мок не нужен, проверяем сигнатуры)**

Run:
```bash
PYTHONUTF8=1 python -c "import inspect, enrich; print('enrich_word sense:', 'sense' in inspect.signature(enrich.enrich_word).parameters); print('run senses:', 'senses' in inspect.signature(enrich.run).parameters)"
```
Expected: оба True.

---

## Self-Review (выполнено автором плана)

- **Покрытие спеки §4 (частично, осознанно):** целевой смысл ✅ (чинит сенс-дрейф пилота); validate hardening (root null, idea/frame) ✅; set_derivation→bool ✅. **Отложено в B5.2** (явно): генерация точного `derivation` (enrich→payload→`confirm_pending`) + QA-морфология family. Причина: encoding работает на консервативной эвристике B2 (без ложной морфологии), derivation-инфра B1 ждёт вызывающего — это enhancement, не блокер наполнения.
- **Backward-compat:** новые параметры (`sense`, `senses`) опциональны, дефолт = старое поведение; существующие `test_batch_enrich.py` не должны падать.
- **Корректность:** root только при уверенности; idea/frame только из таксономии (не форс бизнес-фрейма на бытовое слово).
- **Плейсхолдеры:** нет — код дословно; остаток `run` сохраняется явной инструкцией.
- **Согласованность:** `enrich_word(..., sense=None)`; `run(..., senses=None)`; `validate` root-норм; `set_derivation→bool`. Имена/сигнатуры совпадают между задачами и тестами.
- **Риск:** низкий — изменения локальны в enrich/validate; пайплайн run→qa→pending структурно не тронут.
