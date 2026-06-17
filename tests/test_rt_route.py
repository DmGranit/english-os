"""RT: Маршрут — программа-как-ценность (план v2.6, стр.99–106).

Спека:
- ROUTE-конфиг (как CANDO): недели = фокус + can-do-веха (+grammar-привязка).
- users.program += 'route' (рядом с free|cycle). Прогресс-дуга поверх cando_progress.
- «Неделя 3/8 · фокус: прошедшее время · веха: могу рассказать про вчера — 60%».
- Маршрут ПОДСВЕЧИВАЕТ след. шаг, ничего не блокирует.
- Веха закрывается: «показал в сценарии» (cando ready) + «пережил 90 дней» (box5 / maintenance).
"""
import db

UID = 1


# ── конфиг ──────────────────────────────────────────────────────────────────

def test_route_config_valid(fresh_db):
    """ROUTE: каждая неделя привязана к существующему can-do; номера 1..N подряд."""
    assert len(db.ROUTE) >= 1
    cando_ids = {c["id"] for c in db.CANDO}
    for i, w in enumerate(db.ROUTE, 1):
        assert w["week"] == i, f"неделя #{i} имеет week={w['week']}"
        assert w["cando"] in cando_ids, f"неделя {i}: can-do '{w['cando']}' нет в CANDO"
        assert w["focus"].strip() and w["ru"].strip()


def test_route_grammar_binding_real(fresh_db):
    """grammar-привязка недели (если есть) — реальная transform-тема (GR2-связка)."""
    topics = set(db.transform_topics())
    for w in db.ROUTE:
        if w.get("grammar"):
            assert w["grammar"] in topics, f"неделя {w['week']}: grammar '{w['grammar']}' не из transform_topics"


def test_route_week3_matches_canon_example(fresh_db):
    """Канонический пример спеки: Неделя 3 · прошедшее время · «…рассказать про вчера»."""
    w3 = db.ROUTE[2]
    assert "прошед" in w3["focus"].lower()
    assert "вчера" in w3["ru"].lower()


# ── прогресс-дуга ────────────────────────────────────────────────────────────

def test_route_progress_fresh_user(fresh_db):
    """Новый ученик — на 1-й неделе, ничего не закрыто, не завершён."""
    rp = db.route_progress(UID)
    assert rp["week"] == 1
    assert rp["total"] == len(db.ROUTE)
    assert rp["closed"] == 0
    assert rp["done"] is False
    assert 0.0 <= rp["pct"] <= 1.0
    assert rp["focus"] and rp["milestone_ru"]


# ── закрытие вехи (ready + survival) ─────────────────────────────────────────

def test_milestone_not_closed_without_survival(fresh_db):
    """Освоено (box>=3), но ни одно слово не пережило 90 дней (box5) → веха НЕ закрыта."""
    entry = {"week": 1, "focus": "питч", "cando": "pitch",
             "grammar": "Past Simple", "ru": "могу презентовать"}
    with db._conn() as c:   # Pitching = invest(1), revenue(3)
        c.execute("UPDATE state SET status='learning', box=3 WHERE user_id=? AND word_id IN (1,3)", (UID,))
    assert db.route_milestone_closed(UID, entry) is False


def test_milestone_closed_with_survival(fresh_db):
    """Освоено И хотя бы одно дошло до box5 (пережило maintenance) → веха закрыта."""
    entry = {"week": 1, "focus": "питч", "cando": "pitch",
             "grammar": "Past Simple", "ru": "могу презентовать"}
    with db._conn() as c:
        c.execute("UPDATE state SET status='known', box=5 WHERE user_id=? AND word_id IN (1,3)", (UID,))
    assert db.route_milestone_closed(UID, entry) is True


def test_route_advances_when_milestone_closed(fresh_db, monkeypatch):
    """Закрытие вехи продвигает текущую неделю."""
    monkeypatch.setattr(db, "ROUTE", [
        {"week": 1, "focus": "питч", "cando": "pitch", "grammar": "Past Simple", "ru": "могу презентовать"},
        {"week": 2, "focus": "переговоры", "cando": "negotiate", "grammar": "Present Perfect", "ru": "могу договориться"},
    ])
    assert db.route_progress(UID)["week"] == 1
    with db._conn() as c:
        c.execute("UPDATE state SET status='known', box=5 WHERE user_id=? AND word_id IN (1,3)", (UID,))
    rp = db.route_progress(UID)
    assert rp["closed"] == 1
    assert rp["week"] == 2


def test_route_done_when_all_closed(fresh_db, monkeypatch):
    """Все вехи закрыты → done=True."""
    monkeypatch.setattr(db, "ROUTE", [
        {"week": 1, "focus": "питч", "cando": "pitch", "grammar": "Past Simple", "ru": "могу презентовать"},
    ])
    with db._conn() as c:
        c.execute("UPDATE state SET status='known', box=5 WHERE user_id=? AND word_id IN (1,3)", (UID,))
    rp = db.route_progress(UID)
    assert rp["done"] is True
    assert rp["closed"] == 1


# ── режим программы ───────────────────────────────────────────────────────────

def test_program_route_roundtrip(fresh_db):
    """program='route' сохраняется и читается (рядом с free|cycle)."""
    db.set_program(UID, "route")
    assert db.get_program(UID) == "route"
