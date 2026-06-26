# -*- coding: utf-8 -*-
"""Живая проверка B2 end-to-end: «🌅 Новые» → карточка-предъявление (🆕 + гнездо) →
жму «Дальше» → поток продолжается упражнением/проверкой. Self-contained (делает setup)."""
import os, re, sys, json
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from playwright.sync_api import sync_playwright

PROFILE = r"C:\temp\engbot_smoke\profile"; SHOTS = r"C:\temp\engbot_smoke\shots"; BOT = "English_OS_BOT"

def last_text(page):
    return page.evaluate("""() => { const m=[...document.querySelectorAll('[class*=text-content i]')]; return m.length? (m[m.length-1].innerText||''):''; }""")
def last_btns(page):
    return page.evaluate("""() => { const ms=[...document.querySelectorAll('.Message, .message')]; if(!ms.length) return []; const l=ms[ms.length-1]; return [...l.querySelectorAll('button')].map(x=>(x.innerText||'').trim()).filter(Boolean); }""")
def send(page, t, w=5000):
    b = page.locator('div[contenteditable="true"]').last; b.click(); b.type(t, delay=15)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w); print("SENT:", t[:40], flush=True)
def click(page, t, w=4000):
    try: page.locator(f'button:has-text("{t}")').last.click(timeout=5000); page.wait_for_timeout(w); print("CLICK:", t, flush=True); return True
    except Exception as e: print("CLICK_FAIL:", t, str(e)[:50], flush=True); return False

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, headless=True, viewport={"width":1280,"height":980})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded"); page.wait_for_timeout(6000)
        try: page.get_by_text(BOT, exact=True).first.click()
        except Exception: page.get_by_text(BOT, exact=False).first.click()
        page.wait_for_timeout(3500)

        send(page, "🌅 Новые", 5000)
        enc = last_text(page); page.screenshot(path=os.path.join(SHOTS, "verify_01_enc.png"))
        print("ENC_CARD:", json.dumps(enc.replace(chr(10)," ")[:200], ensure_ascii=False), flush=True)
        is_enc = ("🆕" in enc) or ("гнезд" in enc) or ("из того же" in enc)
        quota = "норма новых слов уже взята" in enc or "Новые — завтра" in enc
        print("IS_ENCODING_CARD:", is_enc, "| DAILY_QUOTA_HIT:", quota, flush=True)
        if quota:
            print("RESULT: квота новых исчерпана на этой базе — нужен свежий снимок (см. харнесс).", flush=True)
            raise SystemExit(0)
        if not click(page, "Дальше", 4500):
            print("RESULT: нет кнопки «Дальше» — карточка-предъявление не пришла.", flush=True)
            raise SystemExit(0)
        after = last_text(page); abt = last_btns(page)
        page.screenshot(path=os.path.join(SHOTS, "verify_02_after_dalee.png"))
        print("AFTER_DALEE:", json.dumps(after.replace(chr(10)," ")[:220], ensure_ascii=False), flush=True)
        advanced = ("Собери" in after or "Как сказать" in after or "Что значит" in after
                    or any("показать ответ" in b.lower() for b in abt) or len(abt) >= 3)
        print("ADVANCED_TO_CHECK:", advanced, flush=True)
        print("RESULT:", "PASS — предъявление -> Дальше -> проверка" if advanced else "STOP — поток не продолжился", flush=True)
    finally:
        try: ctx.close()
        except Exception: pass
print("DONE", flush=True)
