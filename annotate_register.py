# -*- coding: utf-8 -*-
"""Регистр-батч (A2): пакетная разметка register (formal/neutral/informal) для слов,
у которых регистр пуст или дефолтный neutral. Пишет НАПРЯМУЮ в content (с бэкапом до
прогона): это разметка существующих слов, а не новые слова — очередь /pending не нужна.

Запуск: python annotate_register.py            (нужен LLM_API_KEY)
"""
import json, re, sys

import db, llm

REGISTERS = {"formal", "neutral", "informal"}
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def fetch_targets():
    """Слова без осмысленной разметки: register пуст или дефолтный neutral."""
    with db._conn() as c:
        return [r["word"] for r in c.execute(
            """SELECT word FROM content
               WHERE register IS NULL OR register='' OR register='neutral'
               ORDER BY word""")]


def batches(items, size=25):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_map(raw, expected):
    """Достать {слово: регистр} из ответа модели; чужие слова и мусорные значения — мимо."""
    m = _JSON_OBJ.search(raw or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    return {w: r for w, r in data.items()
            if isinstance(r, str) and r in REGISTERS and w in expected}


def apply_map(mapping):
    """Записать разметку в content. Возвращает число обновлённых строк."""
    n = 0
    with db._conn() as c:
        for word, reg in mapping.items():
            n += c.execute("UPDATE content SET register=? WHERE word=?",
                           (reg, word)).rowcount
    return n


def _system_prompt():
    return ("Ты — лексикограф делового английского. Для каждого слова укажи РЕГИСТР "
            "употребления в деловой среде:\n"
            "formal — официальные письма/документы; neutral — универсально (встречи, Slack, "
            "email); informal — разговорное, в официальном письме неуместно.\n"
            "Верни СТРОГО один JSON-объект {слово: регистр} по ВСЕМ словам списка, "
            "без пояснений.")


def run():
    db.init_db()
    targets = fetch_targets()
    if not targets:
        print("Размечать нечего — все слова уже имеют регистр.")
        return {"updated": 0, "skipped": 0}
    bak = db.backup()
    print(f"Бэкап: {bak}\nК разметке: {len(targets)} слов, батчами по 25…")
    updated = 0
    for i, chunk in enumerate(batches(targets), 1):
        raw = llm.chat(_system_prompt(),
                       [{"role": "user", "content": ", ".join(chunk)}],
                       max_tokens=1200)
        mapping = parse_map(raw, expected=set(chunk))
        n = apply_map(mapping)
        updated += n
        print(f"  батч {i}: {n}/{len(chunk)}")
    with db._conn() as c:
        dist = {r["register"]: r["n"] for r in c.execute(
            "SELECT register, COUNT(*) n FROM content GROUP BY register")}
    print(f"Готово: обновлено {updated} из {len(targets)}. Распределение: {dist}")
    return {"updated": updated, "skipped": len(targets) - updated}


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
