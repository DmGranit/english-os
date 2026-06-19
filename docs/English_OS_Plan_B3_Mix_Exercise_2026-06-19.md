# Фаза B3 — Закрепление сразу (упражнение-микс) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сразу после богатого предъявления (B2) давать активное упражнение-закрепление вместо MCQ-заглушки: гнездовое слово → СБОРКА производного (база+аффикс), простое/германское → ПРОДУКЦИЯ RU→EN; объективный зачёт. Поток на КАЖДОЕ слово урока (предъявление→упражнение→след).

**Architecture:** `db.exercise_for_word(word_id)` (pure, решает тип по данным + строит spec) + обработчик ответа `_handle_mix_answer` (объективный грейдер `_one_edit_away`, запись в SRS через `_record_review`, продвижение). Поурочный loop: `_lesson_present` показывает encoding_view+«Дальше», `on_enc_next` показывает упражнение, ответ → след. слово. Reuse B2 (`encoding_view`/`decompose`/`detect_affix`), `_one_edit_away`, `_record_review`, `_next_card`/`_finish_lesson`.

**Tech Stack:** Python 3.13, sqlite3, python-telegram-bot, pytest. Без новых зависимостей. Стоит на B1+B2.

## Global Constraints

- **Это Такт-2 канона: НЕ «вспомнил/забыл», а активное использование с объективным зачётом** (грейдер, не самооценка). Заменяет MCQ-проверку box1-NEW. Maintenance/REVIEW (зрелые слова) НЕ трогаем — там «вспомнил/забыл» остаётся.
- **Критерий типа (D3, операционально):** есть член гнезда (`content.family`) с УВЕРЕННО определённым аффиксом (`decompose` точный ИЛИ `detect_affix(member, base=word)`) → **сборка** этого члена; иначе → **продукция** RU→EN заглавного слова. Неоднозначно/нет данных → продукция (всегда валидна).
- **Корректность (ценность владельца):** сборка спрашивает только уверенно-разобранный член (тот, что показан в предъявлении B2) — без ложной морфологии.
- **SRS сохраняется:** верный/неверный ответ пишется `_record_review` (card_type='assembly'|'prod_typed') — слово зреет box1→box2 как раньше; провал → доработка (rework). Направление считает `db.review` по box (recog для box1) — A/B-чистота не ломается, формат фиксируется через card_type.
- **Поток на каждое слово** урока: предъявление (B2) → упражнение (B3) → следующее слово. Закрывает отложенное B2 «предъявление на КАЖДОЕ слово».
- Тесты: `PYTHONUTF8=1 python -m pytest -q` зелёный (сейчас 422). Фикстура `fresh_db`; ctx-стаб как в tests/test_mcq.py.

---

## File Structure

- `db.py` — Modify: `exercise_for_word(word_id)` (рядом с `encoding_view`).
- `bot.py` — Modify:
  - `_lesson_present(out_or_q, ctx, uid)` — показать encoding_view текущего слова + «Дальше» (общий для `_enter_mode` и продвижения).
  - `on_enc_next` — вместо `_card_payload` показать упражнение-микс (из `exercise_for_word`), сохранить `ctx.user_data["mix"]`.
  - `_handle_mix_answer(update, ctx, uid, text)` — грейд + `_record_review` + продвижение к след. предъявлению; вызвать из `_process_user_text` (перед `typed_wid`).
  - `_enter_mode` "new" и продвижение урока — через `_lesson_present`.
- `tests/test_mix_exercise.py` — Create.

---

## Task 1: `exercise_for_word` — выбор типа и сборка spec

**Files:**
- Modify: `db.py` (рядом с `encoding_view`)
- Test: `tests/test_mix_exercise.py`

**Interfaces:**
- Consumes: `get_word`, `decompose`, `detect_affix`, `find_word_id` (B1/B2).
- Produces: `db.exercise_for_word(word_id) -> dict` — `{"kind": "assembly"|"production", "wid": int, "prompt": str, "expected": str}`. Сборка: prompt «🧩 Собери: <word> + <affix> → ? (подсказка: <meaning>)», expected=член гнезда. Продукция: prompt «✍️ Как сказать по-английски: «<ru>»?», expected=<word>.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_mix_exercise.py
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py -q`
Expected: FAIL — `AttributeError: ... 'exercise_for_word'`.

- [ ] **Step 3: Реализовать** (в `db.py`, рядом с `encoding_view`):

```python
def exercise_for_word(word_id):
    """B3 Такт-2: построить упражнение-закрепление. Гнездовое (есть член с уверенным
    аффиксом) → сборка этого члена; иначе → продукция RU→EN. Объективный зачёт."""
    w = get_word(word_id)
    if not w:
        return {"kind": "production", "wid": word_id, "prompt": "", "expected": ""}
    for member in (w.get("family") or []):
        m = str(member).strip()
        if not m or m.lower() == w["word"].lower():
            continue
        affix = meaning = None
        mid = find_word_id(m)
        if mid:
            d = decompose(mid)
            if d and d.get("affix"):
                affix, meaning = d["affix"], d.get("affix_meaning")
        if not affix:
            det = detect_affix(m, base=w["word"])
            if det:
                affix, meaning = det["affix"], det.get("meaning_ru")
        if affix:
            hint = f" (подсказка: {meaning})" if meaning else ""
            return {"kind": "assembly", "wid": word_id, "expected": m,
                    "prompt": f"🧩 Собери производное: {w['word']} + {affix} → ?{hint}\n\nНапиши слово."}
    return {"kind": "production", "wid": word_id, "expected": w["word"],
            "prompt": f"✍️ Как сказать по-английски:\n«{w['ru']}»?\n\nНапиши ответ."}
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Коммит**

```bash
git add db.py tests/test_mix_exercise.py
git commit -m "feat(B3): exercise_for_word — микс сборка/продукция по данным гнезда"
```

---

## Task 2: грейд ответа `_handle_mix_answer` + показ упражнения в on_enc_next

**Files:**
- Modify: `bot.py` (`on_enc_next` ~1806; новый `_handle_mix_answer`; роутинг в `_process_user_text`)
- Test: `tests/test_mix_exercise.py`

**Interfaces:**
- Consumes: `db.exercise_for_word` (Task 1), `_one_edit_away`, `_record_review`, `_next_card`.
- Produces: `ctx.user_data["mix"]` = `{"wid","expected","kind"}`; `on_enc_next` показывает упражнение (typed-input); `_handle_mix_answer(update, ctx, uid, text)` грейдит и продвигает.

- [ ] **Step 1: Прочитать текущий `on_enc_next` и роутинг typed**

Run: `sed -n '1806,1814p' bot.py` и `grep -n "typed_wid" bot.py | head`
Понять: `on_enc_next` сейчас делает `_card_payload`+edit. `_process_user_text` роутит typed_wid ~стр.981.

- [ ] **Step 2: Написать падающий тест** (ctx-стаб):

```python
import asyncio, types
import bot

def test_mix_answer_assembly_correct_records_and_advances(fresh_db, monkeypatch):
    with fresh_db._conn() as c:
        c.execute("UPDATE content SET family=? WHERE word_id=1", ('["investment"]',))
    recorded = {}
    async def rec(ctx, uid, wid, ok, ms, card_type=None): recorded.update(wid=wid, ok=ok, ct=card_type)
    advanced = {}
    async def nxt(q, ctx, uid): advanced["yes"] = True
    monkeypatch.setattr(bot, "_record_review", rec)
    monkeypatch.setattr(bot, "_next_card", nxt)
    ctx = types.SimpleNamespace(user_data={"mix": {"wid": 1, "expected": "investment", "kind": "assembly"},
                                           "review_queue": [1], "review_pos": 0})
    msg = types.SimpleNamespace(reply_text=lambda *a, **k: _async_none())
    update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=_areply(msg)),
                                   effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot._handle_mix_answer(update, ctx, UID, "investment"))
    assert recorded["ok"] is True and recorded["ct"] == "assembly"
    assert advanced.get("yes") and "mix" not in ctx.user_data

def _async_none():
    import asyncio
    f = asyncio.Future(); f.set_result(None); return f
def _areply(_m):
    async def r(*a, **k): return None
    return r
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py::test_mix_answer_assembly_correct_records_and_advances -v`
Expected: FAIL — `AttributeError: ... '_handle_mix_answer'`.

- [ ] **Step 4: Реализовать** (в `bot.py`):

`_handle_mix_answer` (рядом с `_handle_warm_answer`):
```python
async def _handle_mix_answer(update, ctx, uid, text):
    """B3 Такт-2: объективный зачёт упражнения-микс → запись в SRS → следующее слово."""
    mix = ctx.user_data.pop("mix")
    given = (text or "").strip()
    expected = mix["expected"]
    ok = given.lower() == expected.lower() or _one_edit_away(given, expected)
    head = "✅ Верно!" if ok else f"❌ Правильно: {expected}"
    await update.message.reply_text(head)
    await _record_review(ctx, uid, mix["wid"], ok, None, card_type=mix["kind"])
    # продвижение колоды урока обрабатывает _next_card (нужен callback-объект);
    # имитируем «следующую карточку» через служебный объект сообщения
    await _lesson_advance(ctx, uid, update.message)
```

`on_enc_next` — заменить показ `_card_payload` на упражнение:
```python
async def on_enc_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = _learner(update)
    if not ctx.user_data.pop("enc_pending", None):
        return
    queue = ctx.user_data.get("review_queue") or []
    pos = ctx.user_data.get("review_pos", 0)
    if pos >= len(queue):
        return
    ex = db.exercise_for_word(queue[pos])
    ctx.user_data["mix"] = {"wid": ex["wid"], "expected": ex["expected"], "kind": ex["kind"]}
    await q.edit_message_text(ex["prompt"])
```

`_lesson_advance` (продвинуть к следующему слову или финал; в lesson-режиме показываем предъявление след. слова):
```python
async def _lesson_advance(ctx, uid, message):
    """Сдвинуть урок к следующему слову: показать его предъявление (B2) + «Дальше»,
    либо финал урока. (Reuse-обёртка над _next_card для текстового пути B3.)"""
    ctx.user_data["review_pos"] = ctx.user_data.get("review_pos", 0) + 1
    queue = ctx.user_data.get("review_queue", [])
    if ctx.user_data["review_pos"] >= len(queue):
        ctx.user_data["mode"] = "flow"
        await message.reply_text("🌅 Урок завершён! Слова теперь в очереди — вернутся на повторение 📅",
                                 reply_markup=MAIN_KB)
        return
    nxt = queue[ctx.user_data["review_pos"]]
    ctx.user_data["enc_pending"] = True
    await message.reply_text(db.encoding_view(nxt),
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Дальше ▶", callback_data="enc:next")]]))
```

Роутинг в `_process_user_text` — перед `typed_wid`-веткой добавить:
```python
    if ctx.user_data.get("mix"):                 # B3: ждём ответ упражнения-микс
        await _handle_mix_answer(update, ctx, uid, text)
        return
```

- [ ] **Step 5: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Коммит**

```bash
git add bot.py tests/test_mix_exercise.py
git commit -m "feat(B3): упражнение-микс — грейд + запись SRS + продвижение урока"
```

---

## Task 3: поурочный loop — предъявление+упражнение на каждое слово

**Files:**
- Modify: `bot.py` (`_enter_mode` "new" ~834-843 — через `_lesson_present`; убрать зависимость первого слова от прямого encoding-вызова, унифицировать)
- Test: `tests/test_mix_exercise.py`

**Interfaces:**
- Consumes: `_lesson_advance`/`encoding_view` (Task 2/B2).
- Produces: NEW-урок = последовательность [предъявление → упражнение] по каждому слову до финала.

- [ ] **Step 1: Написать падающий интеграционный тест**

```python
def test_new_lesson_full_loop_two_words(fresh_db, monkeypatch):
    monkeypatch.setattr(bot.db, "promote_new", lambda uid: [bot.db.get_word(1), bot.db.get_word(2)])
    sent = []
    async def out(text, markup=None): sent.append(text)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(bot._enter_mode(out, ctx, UID, "new"))
    # первое слово предъявлено, ждём «Дальше» → упражнение
    assert any("🆕" in (t or "") for t in sent)
    assert ctx.user_data.get("enc_pending") and ctx.user_data.get("review_queue") == [1, 2]
```

- [ ] **Step 2: Запустить — убедиться, что падает или проходит частично**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py::test_new_lesson_full_loop_two_words -v`
Expected: PASS уже возможен (B2 ставит enc_pending для первого слова). Если так — тест фиксирует инвариант; добавить проверку, что после mix-ответа первого слова приходит предъявление второго (через _lesson_advance), что и есть новое поведение Task 3.

Усиль тест:
```python
    # сымитировать ответ на упражнение первого слова → должно прийти предъявление второго
    ctx.user_data["mix"] = {"wid": 1, "expected": "x", "kind": "production"}
    replies = []
    msg = types.SimpleNamespace(reply_text=_collect(replies))
    update = types.SimpleNamespace(message=msg, effective_user=types.SimpleNamespace(id=UID))
    asyncio.run(bot._handle_mix_answer(update, ctx, UID, "wrong"))
    assert any("🆕" in (t or "") for t in replies)        # предъявление второго слова
    assert ctx.user_data["review_pos"] == 1
```
с хелпером:
```python
def _collect(buf):
    async def r(text=None, **k): buf.append(text); return None
    return r
```

- [ ] **Step 3: Реализовать `_lesson_present` и применить в `_enter_mode`** (в `bot.py`):

В ветке `mode=="new"` заменить блок прямого показа encoding (B2) на вызов общего хелпера:
```python
        ctx.user_data["review_pos"] = 0
        await _lesson_present(out, ctx, uid)
        return
```
Хелпер (показ предъявления текущего слова; `out` — функция показа из `_enter_mode`, либо `message.reply_text`):
```python
async def _lesson_present(out, ctx, uid):
    queue = ctx.user_data.get("review_queue", [])
    pos = ctx.user_data.get("review_pos", 0)
    if pos >= len(queue):
        return
    ctx.user_data["enc_pending"] = True
    await out(db.encoding_view(queue[pos]),
              InlineKeyboardMarkup([[InlineKeyboardButton("Дальше ▶", callback_data="enc:next")]]))
```
(_lesson_advance из Task 2 уже использует `message.reply_text` напрямую — оставить; оно — текстовый аналог для пути после ответа.)

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `PYTHONUTF8=1 python -m pytest tests/test_mix_exercise.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Коммит**

```bash
git add bot.py tests/test_mix_exercise.py
git commit -m "feat(B3): поурочный loop — предъявление+упражнение на каждое слово"
```

---

## Task 4: Регресс + живой smoke

- [ ] **Step 1: Полный набор**

Run: `PYTHONUTF8=1 python -m pytest -q`
Expected: PASS — было 422, стало 422 + ~4 = **~426 passed**, 0 регрессий. (Старый `on_mcq`/MCQ-путь остаётся для REVIEW-режима — не удалять; NEW-урок больше не использует MCQ, но REVIEW использует.)

- [ ] **Step 2: Дымовая проверка exercise_for_word на боевой копии**

Run:
```bash
cp english_os.db .b3_check.db
ENGLISH_OS_DB=.b3_check.db PYTHONUTF8=1 python -c "import db; print(db.exercise_for_word(db.find_word_id('invest')))"
rm -f .b3_check.db
```
Expected: dict с kind=assembly или production и непустым prompt/expected.

---

## Self-Review (выполнено автором плана)

- **Покрытие спеки §3-такт2 + D3:** микс сборка/продукция ✅; критерий по уверенно-разобранному гнезду (операционализация D3) ✅; объективный зачёт (не «вспомнил/забыл») ✅; поток на каждое слово ✅ (закрывает отложенное B2).
- **Корректность:** сборка только уверенно-разобранного члена (decompose/detect_affix) — без ложной морфологии ✅.
- **SRS цел:** `_record_review` пишет результат, слово зреет, провал → rework ✅; REVIEW-режим и его MCQ/«вспомнил/забыл» не тронуты ✅.
- **Плейсхолдеры:** нет — код дословно.
- **Согласованность:** `exercise_for_word→{kind,wid,prompt,expected}`; `ctx.user_data["mix"]={wid,expected,kind}`; `_handle_mix_answer`/`_lesson_present`/`_lesson_advance` — имена и поля совпадают между задачами и тестами.
- **Риск/оговорка:** Task 2/3 трогают живой поток NEW-урока (текстовый ответ + продвижение); REVIEW не затрагиваем. Голос (STT) на упражнение — как у warm (текст/голос), но в этом плане проверяем текстовый путь; голосовой — наследует общий STT-роутинг.
- **Вне scope B3:** B4 (перенос сегодняшних слов в сценарий), B5 (enrich += derivation + целевой смысл + set_derivation→bool); расширение набора аффиксов (`-ible` и пр.) — данными B5.
