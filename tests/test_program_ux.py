"""UX программы дня: ввод трёх времён текстом, минуты в слотах, минутный тик."""
import asyncio, types

import bot, db
from conftest import UID


# ---------- парсинг времён ----------

def test_parse_slot_times_plain_hours():
    assert bot._parse_slot_times("9 14 19") == [540, 840, 1140]


def test_parse_slot_times_with_minutes_and_mixed():
    assert bot._parse_slot_times("8:30 13 19:15") == [510, 780, 1155]


def test_parse_slot_times_rejects_garbage():
    assert bot._parse_slot_times("утром днём вечером") is None
    assert bot._parse_slot_times("9 14") is None            # не три
    assert bot._parse_slot_times("9 14 19 21") is None
    assert bot._parse_slot_times("25 14 19") is None        # час вне диапазона
    assert bot._parse_slot_times("9:75 14 19") is None      # минуты вне диапазона
    assert bot._parse_slot_times("") is None


def test_parse_slot_times_tolerant_formats():
    assert bot._parse_slot_times("9, 14, 19") == [540, 840, 1140]      # запятые (Ю2)
    assert bot._parse_slot_times("8.30 13 19.15") == [510, 780, 1155]  # точка как двоеточие (Ю2)


# ---------- режим ожидания ввода часов ----------

def test_slot_input_near_miss_keeps_awaiting(fresh_db):
    """Опечатка во времени (Ю1): подсказка формата, ожидание НЕ сбрасывается."""
    fresh_db.set_program(UID, "cycle")
    ud = {"awaiting_slot_hours": True}
    reply = bot._handle_slot_input(ud, UID, "9:5 14 19")
    assert reply and "9 14 19" in reply                      # подсказка формата
    assert ud.get("awaiting_slot_hours") is True             # не бросаем настройку
    h = fresh_db.get_slot_times(UID)
    assert (h["morning"], h["day"], h["evening"]) == (540, 840, 1140)


def test_enter_mode_clears_awaiting_flag(fresh_db):
    """Р2: ушёл учиться — настройка часов отменена, флаг не висит."""
    ctx = types.SimpleNamespace(user_data={"awaiting_slot_hours": True})

    async def out(text, markup=None):
        pass

    asyncio.run(bot._enter_mode(out, ctx, UID, "flow"))
    assert "awaiting_slot_hours" not in ctx.user_data


def test_remind_single_number_for_cycle_gives_instruction(fresh_db):
    """Ю3: /remind 9 при программе дня — инструкция, а не мёртвая настройка."""
    fresh_db.set_program(UID, "cycle")
    sent = []

    async def reply_text(text, reply_markup=None):
        sent.append(text)

    update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=reply_text),
                                   effective_user=types.SimpleNamespace(id=UID))
    ctx = types.SimpleNamespace(args=["9"], user_data={})
    asyncio.run(bot.remind_cmd(update, ctx))
    assert sent and "три времени" in sent[0]
    assert ctx.user_data.get("awaiting_slot_hours") is True
    assert fresh_db.get_reminder(UID) is None                # reminder_hour не записан

def test_slot_input_applies_and_clears_flag(fresh_db):
    fresh_db.set_program(UID, "cycle")
    ud = {"awaiting_slot_hours": True}
    confirm = bot._handle_slot_input(ud, UID, "8:30 13 19:15")
    assert confirm and "8:30" in confirm and "13:00" in confirm and "19:15" in confirm
    assert "awaiting_slot_hours" not in ud
    h = fresh_db.get_slot_times(UID)
    assert (h["morning"], h["day"], h["evening"]) == (510, 780, 1155)


def test_slot_input_non_numeric_clears_flag_keeps_settings(fresh_db):
    fresh_db.set_program(UID, "cycle")
    ud = {"awaiting_slot_hours": True}
    assert bot._handle_slot_input(ud, UID, "🌅 Новые") is None   # кнопка/текст — не застреваем
    assert "awaiting_slot_hours" not in ud
    h = fresh_db.get_slot_times(UID)
    assert (h["morning"], h["day"], h["evening"]) == (540, 840, 1140)   # часы прежние


def test_slot_input_outside_awaiting_not_intercepted(fresh_db):
    fresh_db.set_program(UID, "cycle")
    ud = {}                                                  # флага нет — свободный текст
    assert bot._handle_slot_input(ud, UID, "9 14 19") is None
    h = fresh_db.get_slot_times(UID)
    assert (h["morning"], h["day"], h["evening"]) == (540, 840, 1140)   # не применилось


# ---------- минутное напоминание: тик в нужную минуту ----------

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text):
        self.sent.append((uid, text))


def test_reminder_tick_fires_at_exact_minute(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.set_slot_time(UID, "morning", 510)              # 8:30; new-слот открыт (4 new-слова)
    app = types.SimpleNamespace(bot=_FakeBot())
    asyncio.run(bot._reminder_tick(app, 510))
    assert app.bot.sent and "🌅" in app.bot.sent[0][1]
    app.bot.sent.clear()
    asyncio.run(bot._reminder_tick(app, 511))                # минутой позже — тишина
    assert app.bot.sent == []


def test_reminder_tick_silent_when_slot_done(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.set_slot_time(UID, "morning", 510)
    fresh_db.start_learning([1], UID)                        # слот NEW закрыт заранее
    app = types.SimpleNamespace(bot=_FakeBot())
    asyncio.run(bot._reminder_tick(app, 510))
    assert app.bot.sent == []


def test_reminder_tick_free_on_round_hour(fresh_db):
    fresh_db.set_reminder(UID, 10)                           # free: как раньше, в свой час
    fresh_db.start_learning([1], UID)                        # есть due
    app = types.SimpleNamespace(bot=_FakeBot())
    asyncio.run(bot._reminder_tick(app, 600))                # 10:00
    assert app.bot.sent and "повторения" in app.bot.sent[0][1]
    app.bot.sent.clear()
    asyncio.run(bot._reminder_tick(app, 610))                # 10:10 — не круглый час
    assert app.bot.sent == []


# ---------- старый путь /remind утро 8:30 и миграция ----------

def test_parse_time_for_advanced_command():
    assert bot._parse_time("8") == 480                       # /remind утро 8
    assert bot._parse_time("8:30") == 510                    # /remind утро 8:30
    assert bot._parse_time("24") is None
    assert bot._parse_time("abc") is None


def test_set_slot_time_minutes(fresh_db):
    fresh_db.set_program(UID, "cycle")
    fresh_db.set_slot_time(UID, "morning", 510)
    assert (UID, "new") in fresh_db.slot_users(510)
    assert fresh_db.slot_users(540) == []                    # старый дефолт уехал


def test_migration_converts_hours_to_minutes(fresh_db):
    with fresh_db._conn() as c:                              # имитируем базу прошлой версии
        c.execute("""INSERT INTO users (user_id, program, remind_morning, remind_day,
                     remind_evening, created_at) VALUES (?,?,?,?,?,?)""",
                  (UID, "cycle", 9, 14, 19, "2026-06-10"))
        c.execute("PRAGMA user_version = 0")
    fresh_db.init_db()                                       # миграция при старте
    h = fresh_db.get_slot_times(UID)
    assert (h["morning"], h["day"], h["evening"]) == (540, 840, 1140)
