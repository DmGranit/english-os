# -*- coding: utf-8 -*-
"""
Обогащение новых слов под логику English OS: слово -> ИИ -> полный 7-слойный JSON
-> валидация -> очередь `pending` (далее подтверждаешь в боте через /pending).

Запуск:
    python enrich.py invest traction generate
    python enrich.py --file words.txt
Нужен LLM_API_KEY (как у бота). Без ключа llm.chat вернёт заглушку — не JSON —
и слова уйдут в "failed": это нормально, плумбинг при этом проверяем на мок-ИИ.
"""
import os, sys, json, re, urllib.parse, urllib.request
import db, llm


def _notify_owner(text):
    """Одна сводка владельцу после пакетного наполнения (бот пишет первым).
    Без TELEGRAM_TOKEN/OWNER_ID — тихо пропускается (тесты, локальные прогоны)."""
    tok = os.environ.get("TELEGRAM_TOKEN")
    owner = os.environ.get("OWNER_ID")
    if not (tok and owner):
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": owner, "text": text}).encode()),
            timeout=10)
    except Exception:
        pass

LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
REGISTERS = {"formal", "neutral", "informal"}
_JSON = re.compile(r"\{.*\}", re.DOTALL)       # достаём JSON-объект из ответа модели
_JSON_ARR = re.compile(r"\[.*\]", re.DOTALL)   # достаём JSON-массив (suggest_words)


def suggest_words(scenario, n=10, level=None):
    """Подобрать до n слов под сценарий из частотных бизнес-списков (NGSL Business,
    Oxford 3000/5000), которых ещё нет в базе. Дубли и имеющиеся отсеиваются.
    level — CEFR-уровень (A2, B1, B2, ...) для целевого подбора."""
    with db._conn() as c:
        existing = [r["word"] for r in c.execute("SELECT word FROM content ORDER BY word")]
    level_hint = f" Приоритет — слова уровня {level}." if level else ""
    system = (
        "Ты — лексикограф делового английского. Подбери частотные, реально полезные "
        "слова и короткие словосочетания уровня A2–C1 под заданный сценарий, опираясь "
        "на частотные списки (NGSL Business, Oxford 3000/5000)."
        + level_hint + " "
        "Верни СТРОГО один JSON-массив строк, без пояснений вокруг.\n"
        "Этих слов НЕ предлагать (уже в базе): " + ", ".join(existing))
    raw = llm.chat(system, [{"role": "user",
                             "content": f"Сценарий: {scenario}. Нужно слов: {n}."}])
    m = _JSON_ARR.search(raw or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out, seen = [], set()
    for w in arr:
        w = str(w).strip()
        if w and w.lower() not in seen and db.find_word_id(w) is None:
            seen.add(w.lower())
            out.append(w)
        if len(out) >= n:
            break
    return out

def _allowed():
    """Существующие DNA-идеи, фреймы и сценарии — чтобы ИИ переиспользовал таксономию, а не плодил новую."""
    with db._conn() as c:
        ideas = [r["dna_idea"] for r in c.execute(
            "SELECT DISTINCT dna_idea FROM content WHERE dna_idea IS NOT NULL ORDER BY dna_idea")]
        frames = [r["name"] for r in c.execute("SELECT name FROM frame_ref ORDER BY name")]
        scenarios = [r["scenario"] for r in c.execute(
            "SELECT DISTINCT scenario FROM content WHERE scenario IS NOT NULL ORDER BY scenario")]
    return ideas, frames, scenarios

def _system_prompt(ideas, frames, scenarios):
    return (
        "Ты — лексикограф English OS. По английскому слову верни СТРОГО один JSON-объект "
        "с полным набором слоёв. Только валидный JSON, без пояснений вокруг.\n"
        "Поля: word, ru, dna_idea, root, family[], collocations[], phrasal[], example, "
        "scenario, thinking_frame, register, level, ipa_uk, freq, useful.\n"
        f"dna_idea выбирай ИЗ списка: {', '.join(ideas)}.\n"
        f"thinking_frame по возможности ИЗ списка: {', '.join(frames)}.\n"
        f"scenario выбирай ОДИН короткий тег ИЗ списка (не предложение!): {', '.join(scenarios)}.\n"
        "root указывай ТОЛЬКО при уверенности (греко-латинский/германский), иначе null — не выдумывай латынь.\n"
        "register ∈ {formal, neutral, informal}; level ∈ {A1,A2,B1,B2,C1,C2}; "
        "freq и useful — целые 1..5 (насколько часто и полезно в деловом английском).\n"
        "family — однокоренные; collocations — 2-4 устойчивых сочетания; example — 1 живое предложение."
    )

def _parse(text):
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def validate(payload, word):
    """Привести к схеме content/payload. None, если нет минимума (ru)."""
    if not isinstance(payload, dict):
        return None
    ru = (payload.get("ru") or "").strip()
    if not ru:
        return None
    def as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [s.strip() for s in str(v or "").split(",") if s.strip()]
    def clamp(v, lo=1, hi=5, default=3):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    raw_root = (payload.get("root") or "").strip()
    root = None if raw_root.lower() in ("", "null", "none", "—", "-") else raw_root
    reg = (payload.get("register") or "neutral").strip().lower()
    lvl = (payload.get("level") or "B1").strip().upper()
    return {
        "word": word.strip(),
        "ru": ru,
        "dna_idea": payload.get("dna_idea"),
        "root": root,
        "family": as_list(payload.get("family")),
        "collocations": as_list(payload.get("collocations")),
        "phrasal": as_list(payload.get("phrasal")),
        "example": payload.get("example"),
        "scenario": payload.get("scenario"),
        "thinking_frame": payload.get("thinking_frame"),
        "register": reg if reg in REGISTERS else "neutral",
        "level": lvl if lvl in LEVELS else "B1",
        "ipa_uk": payload.get("ipa_uk"),
        "freq": clamp(payload.get("freq")),
        "useful": clamp(payload.get("useful")),
    }

def qa_payload(payload):
    """QA-шаг «стерео» (опора № 4): второй ИИ-проход проверяет карточку перед очередью.
    Возвращает (payload|None, причина). Сомнительный корень вычищается — не выдумываем латынь."""
    system = (
        "Ты — въедливый рецензент словарных карточек делового английского. Проверь карточку:\n"
        "1) ru — точный перевод слова word; 2) collocations — РЕАЛЬНЫЕ устойчивые сочетания "
        "именно с этим словом; 3) root: ЕСЛИ УКАЗАН — настоящий ли корень "
        "(сомнительно/выдумано -> drop_root=true); root=null — это НОРМА, не недостаток; "
        "4) example — естественное предложение.\n"
        'Верни СТРОГО один JSON: {"ok": true|false, "drop_root": true|false, "reason": "кратко"}.\n'
        "ok=false ТОЛЬКО при серьёзной ошибке (неверный перевод, выдуманные коллокации, "
        "пример с другим словом). Сомнительный корень — это drop_root, а не ok=false; "
        "отсутствие корня — вообще не замечание.")
    raw = llm.chat(system, [{"role": "user",
                             "content": json.dumps(payload, ensure_ascii=False)}])
    verdict = _parse(raw)
    if not isinstance(verdict, dict) or "ok" not in verdict:
        return None, "QA не вернул вердикт"
    if not verdict.get("ok"):
        return None, verdict.get("reason") or "отклонено QA"
    if verdict.get("drop_root") and payload.get("root"):
        payload = dict(payload, root=None)
    return payload, "ok"


def enrich_word(word, ideas, frames, scenarios, scenario_override=None, sense=None):
    user = f"Слово: {word}"
    if sense:
        user += f"\nЦелевой смысл: {sense} — переведи и разбери ИМЕННО этот смысл, не доминирующий."
    raw = llm.chat(_system_prompt(ideas, frames, scenarios),
                   [{"role": "user", "content": user}])
    payload = validate(_parse(raw), word)
    if payload:
        if scenario_override:                       # батч-наполнение темы: тег принудительный
            payload["scenario"] = scenario_override
        elif payload.get("scenario") not in scenarios:   # фолбэк, если ИИ дал свой сценарий
            payload["scenario"] = "Universal"
        if payload.get("dna_idea") not in ideas:
            payload["dna_idea"] = None
        if payload.get("thinking_frame") not in frames:
            payload["thinking_frame"] = None
    return payload

def run(words, user_id=db.DEFAULT_USER, scenario=None, senses=None):
    """scenario задаёт принудительный тег сценария всем словам (батч-наполнение темы)."""
    db.init_db()
    ideas, frames, scenarios = _allowed()
    senses = senses or {}
    res = {"added": 0, "skipped": 0, "failed": 0}
    for w in words:
        w = w.strip()
        if not w:
            continue
        if db.find_word_id(w) is not None:          # уже в базе — не дублируем
            res["skipped"] += 1
            print(f"= {w}: уже в базе, пропуск")
            continue
        payload = enrich_word(w, ideas, frames, scenarios, scenario_override=scenario, sense=senses.get(w))
        if not payload:
            res["failed"] += 1
            print(f"✗ {w}: ИИ не вернул валидный JSON")
            continue
        payload, reason = qa_payload(payload)          # «стерео»: перекрёстная проверка
        if not payload:
            res["failed"] += 1
            print(f"✗ {w}: QA отклонил — {reason}")
            continue
        db.add_pending(w, payload, user_id)
        res["added"] += 1
        print(f"✓ {w}: в очередь /pending ({payload['ru']}, {payload['level']})")
    print("Итог:", res, "— подтверди слова в боте командой /pending")
    if res["added"]:                                # сводка владельцу: пора разбирать очередь
        _notify_owner(f"📥 Очередь пополнена: +{res['added']} слов "
                      f"(ждут {len(db.list_pending(user_id))}). Подтвердить: /pending")
    return res

def _load_words(argv):
    if len(argv) >= 2 and argv[0] == "--file":
        return [l.strip() for l in open(argv[1], encoding="utf-8") if l.strip()]
    return argv

if __name__ == "__main__":
    words = _load_words(sys.argv[1:])
    if not words:
        print("Использование: python enrich.py word1 word2 ...  |  python enrich.py --file words.txt")
        raise SystemExit(1)
    run(words)
