# -*- coding: utf-8 -*-
"""Живая проверка контракта ИТОГ на реальной модели (стоит денег, в pytest не входит).

Сценарий: история сессии с повторением слов из базы, ошибкой порядка слов и новым
словом -> команда завершения -> модель должна выдать отчёт + машинный JSON ПОСЛЕДНИМ
блоком -> парсим -> применяем к КОПИИ базы. Рабочая english_os.db не трогается.

Запуск (ключ в окружении): python live_check_summary.py
"""
import os, shutil, sys

# копия базы ДО импорта db (DB_PATH читается при импорте); системный %TEMP% закрыт на запись
_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".live_check.db")
shutil.copy("english_os.db", _tmp)
os.environ["ENGLISH_OS_DB"] = _tmp

import db, prompts, llm, bot  # noqa: E402

UID = db.DEFAULT_USER

HISTORY = [
    {"role": "user", "content": "Let's review my words."},
    {"role": "assistant", "content": "Sure! How do you say «инвестировать» in English?"},
    {"role": "user", "content": "invest. We need to invest in marketing."},
    {"role": "assistant", "content": "Correct! And what does «deadline» mean?"},
    {"role": "user", "content": "Hmm... I don't remember this word, sorry."},
    {"role": "assistant", "content": "It means «крайний срок». We'll repeat it next time."},
    {"role": "user", "content": "Ok. Also I yesterday sent the report about our traction to the investor."},
    {"role": "assistant", "content": "Nice progress! Anything else?"},
    {"role": "user", "content": "Закончили"},
]

END = ("<КОНЕЦ СЕССИИ> Заверши занятие: дай человеческий отчёт ИТОГ, а в самом конце — "
       "машинный JSON-блок (reviewed/add/errors). Не переводи это сообщение и не проси повторить.")

def main():
    system = prompts.assemble("flow", with_summary=True) + "\n\n" + db.learner_profile(UID)
    messages = HISTORY[:-1] + [{"role": "user", "content": END}]
    reply = llm.chat(system, messages, max_tokens=bot.SUMMARY_MAX_TOKENS)

    print("=" * 60)
    print("ОТВЕТ МОДЕЛИ (как увидел бы бэкенд):")
    print(reply)
    print("=" * 60)

    data, clean = bot._extract_summary(reply)
    if data is None:
        print("❌ ПРОВАЛ: машинный JSON-блок не найден или битый")
        sys.exit(1)

    print("✅ JSON-блок распарсен. Содержимое:")
    print("  reviewed:", [(i.get("word"), i.get("ok")) for i in data.get("reviewed", [])])
    print("  add:     ", [i.get("word") for i in data.get("add", [])])
    print("  errors:  ", [i.get("category") for i in data.get("errors", [])])

    res = db.apply_session_summary(data, UID)
    print("✅ Применено к копии базы:", res)
    print(f"(копия: {_tmp}; рабочая база не тронута)")

    ok = True
    words_reviewed = {i.get("word", "").lower() for i in data.get("reviewed", [])}
    if "invest" not in words_reviewed or "deadline" not in words_reviewed:
        print("⚠️ Модель не отметила invest/deadline в reviewed"); ok = False
    if not any(e.get("category") == "word_order" for e in data.get("errors", [])):
        print("⚠️ Ошибка «I yesterday sent» не попала в errors/word_order"); ok = False
    if "```json" in clean:
        print("⚠️ В тексте для пользователя остался json-блок"); ok = False
    print("ИТОГ ПРОВЕРКИ:", "✅ всё по контракту" if ok else "⚠️ есть замечания (выше)")

if __name__ == "__main__":
    main()
