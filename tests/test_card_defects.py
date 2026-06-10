"""B2: дефекты карточек + честность данных (cloze \\b, MCQ-дубли, card_type, регистр, ctx)."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- 2.1 cloze с границей слова ----------

def test_cloze_word_boundary_not_substring():
    w = {"word": "investor", "ru": "инвестор",
         "collocations": ["attract investors", "meet the investor"]}
    cl = bot._cloze_for(w)
    # «investor» как подстрока в «investors» НЕ должна давать «attract ___s»
    assert cl is None or "___" in cl and not cl.endswith("s")
    # точное вхождение целым словом — берём
    w2 = {"word": "invest", "ru": "инвестировать", "collocations": ["invest in startups"]}
    assert bot._cloze_for(w2) == "___ in startups"


def test_cloze_skips_inflected_only():
    w = {"word": "please", "ru": "пожалуйста", "collocations": ["pleased to meet you"]}
    assert bot._cloze_for(w) is None        # «pleased» — не целое «please»


# ---------- 2.2 MCQ не показывает дубль-перевод ----------

def test_mcq_no_duplicate_ru(fresh_db):
    with fresh_db._conn() as c:             # два слова с одинаковым ru, одна идея
        c.execute("""INSERT INTO content (word_id, word, ru, dna_idea, level, priority,
                     family, collocations, phrasal) VALUES
                     (50,'assess','оценивать','Decision','B2',9,'[]','[]','[]'),
                     (51,'evaluate','оценивать','Decision','B2',9,'[]','[]','[]'),
                     (52,'decide','решать','Decision','B2',9,'[]','[]','[]')""")
    opts = db.mcq_options(50, k=4)          # ответ assess=оценивать
    rus = [o["ru"] for o in opts]
    assert rus.count("оценивать") == 1      # дубль-перевод не попал дистрактором


# ---------- 2.3 card_type в журнале reviews ----------

def test_review_logs_card_type(fresh_db):
    fresh_db.start_learning([1], UID)
    fresh_db.review(1, True, UID, card_type="mcq")
    with fresh_db._conn() as c:
        r = c.execute("SELECT card_type FROM reviews WHERE user_id=? ORDER BY id DESC LIMIT 1",
                      (UID,)).fetchone()
    assert r["card_type"] == "mcq"


# ---------- 2.4 сборка не палит регистром ----------

def test_assembly_tokens_normalize_case():
    toks = bot._asm_tokens("We need to meet the deadline")
    assert toks is not None
    # первое слово не должно выделяться заглавной среди остальных
    assert not (toks[0][0].isupper() and all(t[0].islower() for t in toks[1:]))


# ---------- 2.5 ctx_checked сбрасывается при входе в режим ----------

def test_enter_mode_resets_ctx_checked(fresh_db):
    ctx = types.SimpleNamespace(user_data={"ctx_checked": True})

    async def out(text, markup=None):
        pass

    asyncio.run(bot._enter_mode(out, ctx, UID, "flow"))
    assert "ctx_checked" not in ctx.user_data        # цель снова можно поймать в новой сессии
