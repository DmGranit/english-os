# -*- coding: utf-8 -*-
"""Живая проверка дисциплины режимов (backlog A1). Стоит денег, в pytest не входит.

SCENARIO: модель в роли, ЗАПРЕЩЕНО исправлять ошибки/показывать блоки до ИТОГа.
FLOW: живой собеседник, без объявления режима («Switching to flow mode»).
Реплики ученика содержат намеренные ошибки — соблазн исправить.

Запуск: python live_check_modes.py
"""
import os, sys

os.environ.setdefault("ENGLISH_OS_DB", ".live_check.db")   # копия от live_check_summary
import db, prompts, llm  # noqa: E402

UID = db.DEFAULT_USER
MODELS = ["gpt-4o", "gpt-4o-mini"]

SCENARIO_HISTORY = [
    {"role": "user", "content": "Let's practice: I pitch my startup to you, you are the investor."},
    {"role": "assistant", "content": "Great, I'm your investor. So, tell me about your company — what do you do?"},
    # намеренные ошибки: порядок слов, артикль, калька
    {"role": "user", "content": "I yesterday founded company. We are making the app for deliver food. "
                                "Our revenue grow very quick and we search investment."},
]

FLOW_HISTORY = [
    {"role": "user", "content": "Hi! Lets just talk about something. How you think, what is more important "
                                "for career — hard skills or soft skills? I think second is more important."},
]

# маркеры учительских блоков, запрещённых в SCENARIO во время диалога
TEACHER_MARKS = ["❌", "→ ✅", "💬", "🌳", "🧠", "Natural:", "correct way", "правильно будет"]
FLOW_MARKS = ["Switching", "switching", "flow mode", "FLOW", "режим потока"]
MARKDOWN_MARKS = ["**", "##", "```"]

def check(mode, history, bad_marks):
    system = prompts.assemble(mode) + "\n\n" + db.learner_profile(UID)
    results = {}
    for model in MODELS:
        reply = llm.chat(system, history, model=model)
        hits = [m for m in bad_marks if m in reply]
        md = [m for m in MARKDOWN_MARKS if m in reply]
        results[model] = (reply, hits, md)
    return results

def report(title, results):
    print("=" * 60)
    print(title)
    for model, (reply, hits, md) in results.items():
        verdict = "✅ дисциплина соблюдена" if not hits else f"❌ НАРУШЕНИЕ: {hits}"
        mdnote = f" · ⚠️ markdown в ответе: {md}" if md else ""
        print(f"\n--- {model}: {verdict}{mdnote}")
        print(reply)

def main():
    report("SCENARIO (в роли инвестора, ошибки ученика НЕ исправлять):",
           check("scenario", SCENARIO_HISTORY, TEACHER_MARKS))
    report("FLOW (живой разговор, без объявления режима):",
           check("flow", FLOW_HISTORY, FLOW_MARKS))

if __name__ == "__main__":
    main()
