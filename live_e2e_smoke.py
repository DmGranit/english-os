# -*- coding: utf-8 -*-
"""Сквозной живой смоук v2.6 против ЖИВОГО бота (TG Web, один сеанс браузера).
Покрывает: /help, GR1 /irregular, GR2 /grammar, EX1 /calque, RT /program+/progress,
/mistakes, и прогон REVIEW (классификация карточек: mcq / recall / продуктивная typed).
Каждый шаг защищён try/except; скриншоты + stdout-флаги для разбора."""
import os, re, json, sys, traceback
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import db
db.DB_PATH = "english_os.db"
from playwright.sync_api import sync_playwright

PROFILE = r"C:\temp\engbot_smoke\profile"
SHOTS   = r"C:\temp\engbot_smoke\shots"
BOT     = "English_OS_BOT"
os.makedirs(SHOTS, exist_ok=True)

def shot(page, n):
    try: page.screenshot(path=os.path.join(SHOTS, n)); print("SHOT:", n, flush=True)
    except Exception as e: print("SHOT_FAIL", n, e, flush=True)

def open_bot(page):
    page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    try: page.get_by_text(BOT, exact=True).first.click()
    except Exception: page.get_by_text(BOT, exact=False).first.click()
    page.wait_for_timeout(3500)

def header_title(page):
    try:
        return page.evaluate("""() => {
            for (const s of ['.ChatInfo .title','[class*="ChatInfo"] [class*="title" i]','h3.title']) {
                const e = document.querySelector(s); if (e && e.innerText) return e.innerText.trim();
            } return '';
        }""")
    except Exception:
        return ''

def send(page, t, w=4500):
    b = page.locator('div[contenteditable="true"]').last; b.click(); b.type(t, delay=15)
    page.wait_for_timeout(300); page.keyboard.press("Enter"); page.wait_for_timeout(w)
    print("SENT:", t[:60], flush=True)

def last_msgs(page, k=4):
    return page.evaluate("""(k) => [...document.querySelectorAll('[class*=text-content i]')]
        .slice(-k).map(e=>(e.innerText||'')).join(' ||| ')""", k)

def buttons(page):
    """Тексты inline-кнопок последнего сообщения."""
    try:
        return page.evaluate("""() => {
            const btns=[...document.querySelectorAll('.inline-buttons button, [class*=inline-button i] button, [class*=InlineButton i]')];
            return btns.slice(-12).map(b=>(b.innerText||'').trim()).filter(Boolean);
        }""")
    except Exception:
        return []

def click_btn(page, text, w=4000):
    try:
        page.locator(f'button:has-text("{text}")').last.click(); page.wait_for_timeout(w)
        print("CLICK:", text, flush=True); return True
    except Exception as e:
        print("CLICK_FAIL:", text, str(e)[:80], flush=True); return False

def step(name, fn):
    print(f"\n===== STEP: {name} =====", flush=True)
    try: fn()
    except Exception:
        print(f"STEP_ERROR {name}:", traceback.format_exc().splitlines()[-1], flush=True)

def answer_for(source):
    s = source.strip().lower()
    for topic, label, src, ans in db._TRANSFORM_SEED:
        if src.strip().lower() == s:
            return ans
    return None

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROFILE, headless=True, viewport={"width":1280,"height":960})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    open_bot(page)
    title = header_title(page)
    shot(page, "e2e_00_open.png")
    print("IN_BOT_CHAT:", BOT.lower() in (title or '').lower(), "| title:", json.dumps(title, ensure_ascii=False), flush=True)
    if BOT.lower() not in (title or '').lower():
        print("ABORT: not in bot chat", flush=True); ctx.close(); raise SystemExit(0)

    # 1. /help — дискаверабилити новых команд
    def s_help():
        send(page, "/help"); txt = last_msgs(page, 8); shot(page, "e2e_01_help.png")
        for cmd in ["/irregular","/calque","/grammar","/program","/mistakes"]:
            print(f"HELP_HAS {cmd}:", cmd in txt, flush=True)
        print("HELP_TXT:", json.dumps(txt.replace(chr(10)," ")[:500], ensure_ascii=False), flush=True)
    step("/help", s_help)

    # 2. GR1 /irregular
    def s_gr1():
        send(page, "/irregular"); card = last_msgs(page, 3); shot(page, "e2e_02_irregular.png")
        print("GR1_CARD:", json.dumps(card.replace(chr(10)," ")[:250], ensure_ascii=False), flush=True)
        m = re.search(r"\b([a-z]+)\b\?", card.lower()) or re.search(r"глагол[а-я]*\s+([A-Za-z]+)", card)
        # надёжнее: ищем любое base из irregular_ref, упомянутое в карточке
        base = None
        low = card.lower()
        for r in db._iter_irregular() if hasattr(db,'_iter_irregular') else []:
            pass
        # fallback: первое английское слово 2+ букв
        cand = re.findall(r"[A-Za-z]{2,}", card)
        for w in cand:
            if db.irregular_for_word(w.lower()):
                base = w.lower(); break
        if base:
            row = db.irregular_for_word(base)
            past = row.get("past") if isinstance(row, dict) else None
            print("GR1_PARSED:", base, "-> past:", past, flush=True)
            if past:
                send(page, past); fb = last_msgs(page, 3); shot(page, "e2e_02b_irregular_ans.png")
                print("GR1_CORRECT:", ("Верно" in fb or "✅" in fb), flush=True)
                print("GR1_FB:", json.dumps(fb.replace(chr(10)," ")[:250], ensure_ascii=False), flush=True)
        else:
            print("GR1_PARSE_FAIL", flush=True)
    step("GR1 /irregular", s_gr1)

    # 3. GR2 /grammar
    def s_gr2():
        send(page, "/grammar"); card = last_msgs(page, 3); shot(page, "e2e_03_grammar.png")
        print("GR2_CARD:", json.dumps(card.replace(chr(10)," ")[:250], ensure_ascii=False), flush=True)
        m = re.search(r"«([^»]+)»", card)
        if m:
            src = m.group(1); ans = answer_for(src)
            print("GR2_SRC:", json.dumps(src, ensure_ascii=False), "-> ans:", json.dumps(ans, ensure_ascii=False), flush=True)
            if ans:
                send(page, ans, 6000); fb = last_msgs(page, 3); shot(page, "e2e_03b_grammar_ans.png")
                print("GR2_CORRECT:", ("Верно" in fb or "✅" in fb), flush=True)
                print("GR2_FB:", json.dumps(fb.replace(chr(10)," ")[:250], ensure_ascii=False), flush=True)
        else:
            print("GR2_PARSE_FAIL", flush=True)
    step("GR2 /grammar", s_gr2)

    # 4. EX1 /calque
    def s_ex1():
        send(page, "/calque", 6000); card = last_msgs(page, 3); btns = buttons(page); shot(page, "e2e_04_calque.png")
        print("EX1_CARD:", json.dumps(card.replace(chr(10)," ")[:250], ensure_ascii=False), flush=True)
        print("EX1_BUTTONS:", json.dumps(btns, ensure_ascii=False), flush=True)
        if btns:
            click_btn(page, btns[0]); fb = last_msgs(page, 4); shot(page, "e2e_04b_calque_ans.png")
            print("EX1_FB:", json.dumps(fb.replace(chr(10)," ")[:300], ensure_ascii=False), flush=True)
    step("EX1 /calque", s_ex1)

    # 5. RT /program -> Маршрут
    def s_rt():
        send(page, "/program"); btns = buttons(page); shot(page, "e2e_05_program.png")
        print("RT_PROG_BUTTONS:", json.dumps(btns, ensure_ascii=False), flush=True)
        click_btn(page, "Маршрут")
        intro = last_msgs(page, 3); shot(page, "e2e_05b_route.png")
        print("RT_INTRO:", json.dumps(intro.replace(chr(10)," ")[:300], ensure_ascii=False), flush=True)
        send(page, "/progress"); prog = last_msgs(page, 4); shot(page, "e2e_05c_progress.png")
        print("RT_PROGRESS:", json.dumps(prog.replace(chr(10)," ")[:400], ensure_ascii=False), flush=True)
        print("RT_HAS_ROUTE_ARC:", ("Маршрут" in prog or "Неделя" in prog or "неделя" in prog), flush=True)
    step("RT /program+/progress", s_rt)

    # 6. /mistakes — журнал ошибок
    def s_mist():
        send(page, "/mistakes"); txt = last_msgs(page, 4); shot(page, "e2e_06_mistakes.png")
        print("MISTAKES:", json.dumps(txt.replace(chr(10)," ")[:400], ensure_ascii=False), flush=True)
    step("/mistakes", s_mist)

    # 7. REVIEW — что реально подаётся; ищем продуктивную карточку
    def s_review():
        send(page, "☀️ Повторить", 5000)   # reply-кнопка шлётся ТЕКСТОМ, не click
        shot(page, "e2e_07_review_start.png")
        prod_seen = False; types = []
        for i in range(10):
            card = last_msgs(page, 2); btns = buttons(page)
            low = card.lower()
            if "как сказать по-английски" in low or "✍️" in card:
                ctype = "PRODUCTIVE(typed)"; prod_seen = True
            elif any("показать ответ" in b.lower() for b in btns):
                ctype = "recall(show-answer)"
            elif any(b for b in btns if b and ("ё" not in b)) and len(btns) >= 3:
                ctype = f"mcq?({len(btns)}btn)"
            else:
                ctype = f"other(btns={btns})"
            types.append(ctype)
            print(f"REVIEW[{i}]:", ctype, "|", json.dumps(card.replace(chr(10)," ")[:140], ensure_ascii=False), flush=True)
            if i < 3: shot(page, f"e2e_07_review_{i}.png")
            # продвинуться
            if ctype.startswith("PRODUCTIVE"):
                print("  >>> ПРОДУКТИВНАЯ КАРТОЧКА НАЙДЕНА", flush=True); break
            if any("показать ответ" in b.lower() for b in btns):
                click_btn(page, "Показать ответ", 1500); click_btn(page, "Вспомнил", 2500)
            elif btns:
                click_btn(page, btns[0], 2500)
            else:
                print("  нет кнопок — стоп", flush=True); break
        print("REVIEW_PROD_SEEN:", prod_seen, "| types:", json.dumps(types, ensure_ascii=False), flush=True)
    step("REVIEW walk", s_review)

    shot(page, "e2e_99_final.png")
    ctx.close()
print("\nDONE", flush=True)
