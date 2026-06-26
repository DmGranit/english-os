# -*- coding: utf-8 -*-
"""Live smoke for GR2 wrong-answer path: deliberately wrong transform must yield ❌
and a tense_aspect error row (LLM grader must NOT false-accept)."""
import os, re, json, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import db
# ВАЖНО: читать ТУ ЖЕ базу, что пишет бот (под live-харнессом = копия .live_check.db).
# Хардкод прод-пути ломал счётчик (бот пишет копию, драйвер читал прод -> DELTA 0).
db.DB_PATH = os.environ.get("ENGLISH_OS_DB", "english_os.db")
from playwright.sync_api import sync_playwright

PROFILE = r"C:\temp\engbot_smoke\profile"; BOT = "English_OS_BOT"

def open_bot(page):
    page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded"); page.wait_for_timeout(6000)
    try: page.get_by_text(BOT, exact=True).first.click()
    except Exception: page.get_by_text(BOT, exact=False).first.click()
    page.wait_for_timeout(3500)

def send(page, t, w=5000):
    b = page.locator('div[contenteditable="true"]').last; b.click(); b.type(t, delay=15)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w); print("SENT:", t[:60], flush=True)

def last_msgs(page, k=3):
    return page.evaluate("""(k)=>[...document.querySelectorAll('[class*=text-content i]')].slice(-k).map(e=>(e.innerText||'')).join(' ||| ')""", k)

def errors_count():
    with db._conn() as c:
        return c.execute("SELECT COUNT(*) FROM errors WHERE category='tense_aspect'").fetchone()[0]

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, headless=True, viewport={"width":1280,"height":900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    open_bot(page)
    before = errors_count(); print("ERRORS_BEFORE:", before, flush=True)
    send(page, "/grammar")
    card = last_msgs(page, 2)
    # источник активной карты = ПОСЛЕДНЯЯ «…» (last_msgs склеивает бабблы; первая может быть от прошлой карты)
    found = re.findall(r"«([^»]+)»", card)
    source = found[-1] if found else None
    print("CARD_SOURCE:", json.dumps(source, ensure_ascii=False), flush=True)
    # deliberately WRONG: send the source unchanged (wrong tense)
    send(page, source or "I go to work")
    page.wait_for_timeout(2500)      # дать async LLM-грейду + записи errors зафиксироваться
    fb = last_msgs(page, 3)
    after = errors_count()
    wrong_marked = ("Не совсем" in fb or "❌" in fb)
    accepted = ("Верно" in fb or "✅" in fb)
    print("GR2_WRONG_MARKED:", wrong_marked, flush=True)
    print("ERRORS_AFTER:", after, "DELTA:", after - before, flush=True)
    print("FEEDBACK:", json.dumps(fb.replace(chr(10), " ")[:300], ensure_ascii=False), flush=True)
    if wrong_marked and after - before >= 1:
        print("RESULT: PASS — неверный ответ отвергнут (❌) и записан в errors", flush=True)
    elif accepted:
        print("RESULT: FINDING — грейдер FALSE-ACCEPT'нул неизменённый source "
              f"(«{source}») как верный (грейдер слишком мягок к near-miss времени)", flush=True)
    elif wrong_marked:
        print("RESULT: CHECK — отвергнут (❌), но errors не вырос (DELTA 0)", flush=True)
    else:
        print("RESULT: CHECK — реакция не распознана (возможен рассинхрон карты)", flush=True)
    ctx.close()
print("DONE", flush=True)
