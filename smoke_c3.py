# -*- coding: utf-8 -*-
"""C3 deep_view (live): «учим inform» → бот даёт кнопку «🔍 inform — …» → тап →
deep_view с IPA/фрейм/корень/разбор. (Старый путь через 📒 Слова устарел — там теперь
текстовый список, не кнопки.) Reply-кнопки не нужны; шлём текст «учим …»."""
import os, json
from playwright.sync_api import sync_playwright
PROFILE=r"C:\temp\engbot_smoke\profile"; SHOTS=r"C:\temp\engbot_smoke\shots"; BOT="English_OS_BOT"
WORD="inform"   # есть IPA (🔊) + фрейм (🧠) + корень/разбор

def shot(page,n): page.screenshot(path=os.path.join(SHOTS,n)); print("SHOT:",n,flush=True)
def open_bot(page):
    page.goto("https://web.telegram.org/a/",wait_until="domcontentloaded"); page.wait_for_timeout(6000)
    try: page.get_by_text(BOT, exact=True).first.click()
    except Exception: page.get_by_text(BOT, exact=False).first.click()
    page.wait_for_timeout(3500)
def send(page,t,w=5000):
    b=page.locator('div[contenteditable="true"]').last; b.click(); b.type(t,delay=20)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w); print("SENT:",t,flush=True)
def last_text(page):
    return page.evaluate("""() => {const m=[...document.querySelectorAll('[class*=text-content i]')]; return m.length?(m[m.length-1].innerText||''):'';}""")

with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(PROFILE,headless=True,viewport={"width":1280,"height":920})
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        open_bot(page)
        # run_bot_testcopy даёт изолированный свежий ctx -> «учим X» первым попадёт в
        # learn-intent (а не в персистентную колоду). Один раз, повтор портит состояние.
        send(page, f"учим {WORD}", 6000)
        shot(page,"c3_01_taught.png")
        # дождаться кнопку 🔍 поллингом, БЕЗ повторной отправки
        clicked=False
        for _ in range(6):
            if page.locator(f'button:has-text("{WORD}")').count()>0:
                try:
                    page.locator(f'button:has-text("{WORD}")').last.click(timeout=4000); page.wait_for_timeout(3500)
                    clicked=True; print("TAPPED 🔍:", WORD, flush=True); break
                except Exception as e:
                    print("DEEP_BTN_FAIL:", str(e)[:60], flush=True); break
            page.wait_for_timeout(1500)
        if not clicked:
            print("DEEP_BTN_FAIL: кнопка 🔍 не появилась за поллинг", flush=True)
        shot(page,"c3_02_deep.png")
        txt=last_text(page)
        print("DEEP_TEXT:", json.dumps(txt.replace(chr(10)," ")[:400],ensure_ascii=False), flush=True)
        # ВАЖНО: DOM Telegram СРЕЗАЕТ эмодзи -> проверяем по PLAIN-тексту, не по 🔍/🌳/🧩/🔊/🧠
        seen={
            "header":     (WORD in txt and "—" in txt),
            "root":       ("корень" in txt),
            "derivation": ("разбор" in txt),
            "IPA":        ("ˈ" in txt or "/ɪ" in txt or "ː" in txt),
            "frame":      ("фрейм" in txt),
        }
        for lbl in seen: print(f"  HAS_{lbl}:", seen[lbl], flush=True)
        core = seen["header"] and (seen["root"] or seen["derivation"] or seen["IPA"] or seen["frame"])
        print("RESULT:", "PASS — deep_view раскрыт с насмотренностью" if (clicked and core) else "CHECK — deep_view не подтверждён", flush=True)
    finally:
        try: ctx.close()
        except Exception: pass
print("DONE", flush=True)
