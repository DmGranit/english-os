"""Доступ-по-заявке: роли, решение вахтёра, бутстрап из env, анти-спам уведомлений."""
import db, bot
from conftest import UID

STRANGER = 777000111


# ---------- роли и заявки ----------

def test_request_access_first_time_only(fresh_db):
    assert fresh_db.request_access(STRANGER, "Вася (@vasya)") is True    # первая заявка
    assert fresh_db.get_role(STRANGER) == "pending"
    assert fresh_db.request_access(STRANGER, "Вася (@vasya)") is False   # повтор — без уведомления


def test_roles_change_decision(fresh_db):
    fresh_db.request_access(STRANGER, "Вася")
    fresh_db.set_role(STRANGER, "approved")
    assert bot._access_decision(STRANGER) == "allowed"
    fresh_db.set_role(STRANGER, "blocked")
    assert bot._access_decision(STRANGER) == "blocked"


# ---------- решающая функция вахтёра ----------

def test_access_decision_owner_and_env(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_ID", UID)
    monkeypatch.setattr(bot, "ALLOWED_USERS", {424242})
    assert bot._access_decision(UID) == "allowed"        # владелец
    assert bot._access_decision(424242) == "allowed"     # env — аварийный замок


def test_access_decision_stranger_flow(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "OWNER_ID", UID)
    monkeypatch.setattr(bot, "ALLOWED_USERS", set())
    assert bot._access_decision(STRANGER) == "pending_new"   # решение чистое, без побочек
    fresh_db.request_access(STRANGER, "Вася")                # заявку создаёт вахтёр (_guard)
    assert bot._access_decision(STRANGER) == "pending"       # дальше — ждёт, не спамим


# ---------- бутстрап ролей из env при старте ----------

def test_ensure_roles_bootstrap_idempotent(fresh_db):
    fresh_db.ensure_roles(UID, {UID, STRANGER})
    assert fresh_db.get_role(UID) == "owner"
    assert fresh_db.get_role(STRANGER) == "approved"
    fresh_db.set_role(STRANGER, "blocked")               # ручное решение...
    fresh_db.ensure_roles(UID, {UID, STRANGER})          # ...повторный старт его НЕ затирает
    assert fresh_db.get_role(STRANGER) == "blocked"


# ---------- /users: данные списка ----------

def test_list_users_shows_roles_and_progress(fresh_db):
    fresh_db.ensure_roles(UID, {UID})
    fresh_db.start_learning([1, 2], UID)
    fresh_db.review(1, True, UID)
    fresh_db.review(1, True, UID)
    fresh_db.review(1, True, UID)                        # invest -> box 3 (освоено)
    fresh_db.request_access(STRANGER, "Вася (@vasya)")
    rows = {r["user_id"]: r for r in fresh_db.list_users()}
    assert rows[UID]["role"] == "owner"
    assert rows[UID]["mastered"] == 1
    assert rows[STRANGER]["role"] == "pending"
    assert "Вася" in (rows[STRANGER]["name"] or "")
