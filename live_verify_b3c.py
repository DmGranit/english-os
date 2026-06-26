# -*- coding: utf-8 -*-
"""Финальная допроверка B3: «🌅 Новые» → предъявление → «Дальше» → упражнение (СБОРКА/
продукция) → ответ через exercise_for_word → ✅. Self-contained; БД = та же, что у бота."""
import os, re, sys, json
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import db  # db.DB_PATH = env ENGLISH_OS_DB (под харнессом = копия, что и у бота)
from playwright.sync_api import sync_playwright
PROFILE = r"C:\temp\engbot_smoke\profile"; SHOTS = r"C:\temp\engbot_smoke\shots"; BOT = "English_OS_BOT"

def last_text(page):
    return page.evaluate("""() => { const m=[...document.querySelectorAll('[class*=text-content i]')]; return m.length? (m[m.length-1].innerText||''):''; }""")
def recent(page, k=3):
    return page.evaluate("""(k)=>[...document.querySelectorAll('[class*=text-content i]')].slice(-k).map(e=>(e.innerText||'')).join(' ||| ')""", k)
def send(page, t, w=4500):
    b = page.locator('div[contenteditable="true"]').last; b.click(); b.type(t, delay=15)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w); print("SENT:", t[:40], flush=True)
def click(page, t, w=4000):
    try: page.locator(f'button:has-text("{t}")').last.click(timeout=5000); page.wait_for_timeout(w); print("CLICK:", t, flush=True); return True
    except Exception: return False
def headword(txt):
    m = re.search(r"🆕\s*([A-Za-z][A-Za-z .'-]*?)\s*[—-]", txt) or re.search(r"\b([a-z]{3,})\b\s*[—-]", txt)
    return m.group(1).strip() if m else None

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, headless=True, viewport={"width":1280,"height":980})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded"); page.wait_for_timeout(6000)
        try: page.get_by_text(BOT, exact=True).first.click()
        except Exception: page.get_by_text(BOT, exact=False).first.click()
        page.wait_for_timeout(3500)

        send(page, "🌅 Новые", 5000)
        enc = last_text(page)
        if "норма новых слов уже взята" in enc or "Новые — завтра" in enc:
            print("DAILY_QUOTA_HIT — нужен свежий снимок базы.", flush=True); raise SystemExit(0)
        word = headword(enc)
        print("ENC_WORD:", word, "|", json.dumps(enc.replace(chr(10)," ")[:120], ensure_ascii=False), flush=True)
        if not click(page, "Дальше", 4500):
            print("RESULT: нет «Дальше» — предъявление не пришло.", flush=True); raise SystemExit(0)
        ex = last_text(page); page.screenshot(path=os.path.join(SHOTS, "b3c_exercise.png"))
        kind = "assembly" if "Собери" in ex else ("production" if "Как сказать" in ex else "?")
        print("EXERCISE(%s):" % kind, json.dumps(ex.replace(chr(10)," ")[:200], ensure_ascii=False), flush=True)
        wid = db.find_word_id(word) if word else None
        ans = db.exercise_for_word(wid)["expected"] if wid else None
        print("EXPECTED:", ans, flush=True)
        if ans:
            send(page, ans, 4500)
            res = recent(page, 3)   # грейд в предпоследнем баббле (бот сразу шлёт след. карту)
            page.screenshot(path=os.path.join(SHOTS, "b3c_after.png"))
            ok = ("Верно" in res or "✅" in res or "Почти" in res or "Засчитано" in res)
            adv = ("гнезд" in res or "🆕" in res or "Урок завершён" in res)
            print("AFTER:", json.dumps(res.replace(chr(10)," ")[:240], ensure_ascii=False), flush=True)
            print("GRADED_OK:", ok, "| ADVANCED:", adv, flush=True)
            print("RESULT:", "PASS — предъявление -> упражнение -> грейд" if (ok or adv) else "STOP — грейд не распознан", flush=True)
    finally:
        try: ctx.close()
        except Exception: pass
print("DONE", flush=True)
