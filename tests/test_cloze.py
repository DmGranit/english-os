"""Этап 5.1: cloze-карточка (box 2) — коллокация с пропуском целевого слова."""
import bot

WORD = {"word_id": 1, "word": "traction", "ru": "импульс роста",
        "dna_idea": "Growth", "root": None, "family": [], "phrasal": [],
        "collocations": ["gain traction", "lose traction"],
        "example": "The product is gaining traction.",
        "scenario": "Pitching", "thinking_frame": None, "register": "neutral",
        "level": "B1", "ipa_uk": None, "ipa_us": None}


def test_cloze_built_from_collocation():
    assert bot._cloze_for(WORD) == "gain ___"


def test_cloze_none_when_word_not_in_collocations():
    w = dict(WORD, collocations=["market fit"])
    assert bot._cloze_for(w) is None


def test_box2_question_is_cloze(fresh_db):
    text = bot._review_card_text(WORD, 1, 5, reveal=False, productive=False,
                                 variant="flat", box=2)
    assert "___" in text                          # пропуск на месте
    assert "traction" not in text                 # ответ не подсветился
    assert "импульс роста" in text                # подсказка-перевод


def test_box2_reveal_shows_full_collocation(fresh_db):
    text = bot._review_card_text(WORD, 1, 5, reveal=True, productive=False,
                                 variant="flat", box=2)
    assert "gain traction" in text and "traction" in text


def test_box1_keeps_recognition_format(fresh_db):
    text = bot._review_card_text(WORD, 1, 5, reveal=False, productive=False,
                                 variant="flat", box=1)
    assert "___" not in text                      # обычное узнавание
    assert "traction" in text


def test_box2_without_cloze_falls_back(fresh_db):
    w = dict(WORD, collocations=[])
    text = bot._review_card_text(w, 1, 5, reveal=False, productive=False,
                                 variant="flat", box=2)
    assert "___" not in text and "traction" in text
