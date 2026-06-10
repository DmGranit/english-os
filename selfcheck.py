# -*- coding: utf-8 -*-
"""Самопроверка: быстрые проверки целостности. Запускается при старте бота
и по команде /health (владелец). Без сети и без LLM — мгновенно."""
import db, prompts

REQUIRED_BLOCKS = {"ЯДРО", "NEW", "REVIEW", "SCENARIO", "FLOW", "INPUT", "ИТОГ"}


def checks():
    """Список (имя, ок, детали). Информационные пункты всегда ок=True."""
    out = []
    try:
        n = db.count_content()
        with db._conn() as c:
            ver = c.execute("PRAGMA user_version").fetchone()[0]
        out.append(("db", n > 0, f"{n} слов · схема v{ver}"))
    except Exception as e:
        out.append(("db", False, f"недоступна: {e}"))
    missing = REQUIRED_BLOCKS - set(prompts.BLOCKS)
    out.append(("prompt", not missing,
                "все блоки на месте" if not missing else f"нет блоков: {missing}"))
    try:
        out.append(("pending", True, f"{len(db.list_pending())} в очереди"))
    except Exception as e:
        out.append(("pending", False, str(e)))
    try:
        out.append(("errors24h", True, f"{db.tech_count_24h()} за сутки"))
    except Exception as e:
        out.append(("errors24h", False, str(e)))
    try:
        days = db.stock_days()
        if days is None:
            out.append(("stock", True, "нет активных учеников"))
        else:
            out.append(("stock", days >= 14, f"новых слов на ~{days} дн. у самого активного"))
    except Exception as e:
        out.append(("stock", False, str(e)))
    return out


def ok(results=None):
    return all(r[1] for r in (results if results is not None else checks()))
