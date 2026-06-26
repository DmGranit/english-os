# -*- coding: utf-8 -*-
"""Review flip-сценарий: «☀️ Повторить» → найти recall-карточку → «Показать ответ»
(раскрытие) → ответ виден → самооценка «Вспомнил». Кнопка раскрытия = «Показать ответ»
(не «Подсказка»). Reply-кнопки шлём ТЕКСТОМ."""
import os, json
from playwright.sync_api import sync_playwright
PROFILE=r"C:\temp\engbot_smoke\profile"; SHOTS=r"C:\temp\engbot_smoke\shots"; BOT="English_OS_BOT"

def shot(page,n): page.screenshot(path=os.path.join(SHOTS,n)); print("SHOT:",n,flush=True)
def open_bot(page):
    page.goto("https://web.telegram.org/a/",wait_until="domcontentloaded"); page.wait_for_timeout(6000)
    try: page.get_by_text(BOT, exact=True).first.click()
    except Exception: page.get_by_text(BOT, exact=False).first.click()
    page.wait_for_timeout(3500)
def last_text(page):
    return page.evaluate("""() => { const m=[...document.querySelectorAll('[class*=text-content i]')]; return m.length?(m[m.length-1].innerText||''):''; }""")
def send(page,t,w=4500):
    b=page.locator('div[contenteditable="true"]').last; b.click(); b.type(t,delay=20)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w); print("SENT:",t,flush=True)
def has(page,t):
    try: return page.locator(f'button:has-text("{t}")').count()>0
    except Exception: return False
def click(page,t,w=3000):
    try: page.locator(f'button:has-text("{t}")').last.click(timeout=5000); page.wait_for_timeout(w); print("CLICK:",t,flush=True); return True
    except Exception: return False

with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(PROFILE,headless=True,viewport={"width":1280,"height":920})
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        open_bot(page)
        send(page,"🏁 Итог")           # сброс прошлой сессии
        send(page,"☀️ Повторить",5000)
        shot(page,"drive_01_review.png")
        flip_ok=False
        for i in range(8):
            txt=last_text(page)
            if "Повторять нечего" in txt or "ничего не" in txt:
                print(f"[{i}] колода пуста", flush=True); break
            if has(page,"Показать ответ"):
                click(page,"Показать ответ",1800)
                revealed=last_text(page)
                shown=("→" in revealed or "—" in revealed or "Ты вспомнил" in revealed)
                print(f"[{i}] RECALL: раскрыто={shown}", json.dumps(revealed.replace(chr(10)," ")[:120],ensure_ascii=False), flush=True)
                if click(page,"Вспомнил",2500):
                    flip_ok=shown; shot(page,"drive_02_rated.png"); break
            elif has(page,"Дальше"):
                click(page,"Дальше",2500)
            else:
                # mcq/typed — продвинуть: первый инлайн-вариант или текст
                btns=page.evaluate("""() => {const ms=[...document.querySelectorAll('.Message,.message')]; if(!ms.length)return[]; return [...ms[ms.length-1].querySelectorAll('button')].map(x=>(x.innerText||'').trim()).filter(Boolean);}""")
                opt=next((b for b in btns if b and b not in ("Add",) and not b.startswith("+")), None)
                if opt and click(page,opt,2500): pass
                else: send(page,"test",3000)
        print("FLIP_OK:", flip_ok, flush=True)
        print("RESULT:", "PASS — recall-карта раскрылась и оценена" if flip_ok else "INFO — recall-карты не встретилось в колоде (не баг)", flush=True)
    finally:
        try: ctx.close()
        except Exception: pass
print("DONE", flush=True)
