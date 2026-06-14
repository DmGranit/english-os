"""
English OS — слой данных и SRS.
Канон: SQLite. Excel/JSON — одноразовый seed. Единственный писатель — бот.

Таблицы:
  content  — онтология (seed + подтверждённые добавления). Иммутабельна по смыслу.
  state    — прогресс SRS на пользователя и слово.
  pending  — слова, сгенерённые агентом, ждущие подтверждения человеком.
  reviews  — журнал повторений (для аналитики).
"""
import sqlite3, json, shutil, datetime, os, re, random
from contextlib import contextmanager

DB_PATH = os.environ.get("ENGLISH_OS_DB", "english_os.db")
DEFAULT_USER = 1                      # пока один пользователь; user_id заложен на вырост
DAILY_NEW_CAP = int(os.environ.get("NEW_CAP", "7"))   # дневная норма новых слов (настраивается NEW_CAP)
DIRECT_BUDGET_MULT = 2   # прямой ввод (темы/ветка/«учим X»/сценарий) — до cap×2/день
                         # (раньше шёл мимо капа -> лавина повторений; единый бюджет её гасит)
DECK_CAP = int(os.environ.get("DECK_CAP", "20"))      # макс. карточек за одну колоду повторения
INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}   # Лейтнер, как в исходном файле
MAINTENANCE_DAYS = 90     # known-слова возвращаются на «проверку выживания» (канон Ч.1:
                          # без извлечения след угасает; can-do-прокси не должен врать)
PRODUCTIVE_FROM_BOX = 3   # box 1–2: узнавание EN→RU (recog); box 3+: продукция RU→EN (prod)
NATION_TARGET = 3000      # ориентир покрытия: ~3000 семей слов ≈ 95% текстов (Nation)

SCHEMA = """
CREATE TABLE IF NOT EXISTS content (
    word_id INTEGER PRIMARY KEY, word TEXT NOT NULL, ru TEXT,
    dna_idea TEXT, root TEXT, family TEXT, collocations TEXT, phrasal TEXT,
    example TEXT, scenario TEXT, thinking_frame TEXT, register TEXT, level TEXT,
    ipa_uk TEXT, ipa_us TEXT, freq INTEGER, useful INTEGER,
    priority INTEGER, origin TEXT DEFAULT 'seed'
);
CREATE TABLE IF NOT EXISTS state (
    user_id INTEGER NOT NULL, word_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',   -- new|learning|forgot|known
    box INTEGER NOT NULL DEFAULT 0,       -- 0 = ещё не введено в оборот, далее 1..5
    last_review TEXT, next_review TEXT, promoted_at TEXT,
    PRIMARY KEY (user_id, word_id),
    FOREIGN KEY (word_id) REFERENCES content(word_id)
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    word TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'   -- pending|confirmed|rejected
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    word_id INTEGER NOT NULL, ts TEXT NOT NULL, remembered INTEGER NOT NULL,
    variant TEXT, ms INTEGER, direction TEXT, card_type TEXT
);
-- структурные ошибки речи (движок фреймов): категория универсальна, без привязки к L1
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    ts TEXT NOT NULL, category TEXT NOT NULL, wrong TEXT, correct TEXT, note TEXT
);
-- запрашиваемая «сеть»: списочные поля слова вынесены в связи (наполняются при seed)
CREATE TABLE IF NOT EXISTS word_collocation (
    word_id INTEGER NOT NULL, text TEXT NOT NULL,
    FOREIGN KEY (word_id) REFERENCES content(word_id)
);
CREATE TABLE IF NOT EXISTS word_family (
    word_id INTEGER NOT NULL, member TEXT NOT NULL,
    FOREIGN KEY (word_id) REFERENCES content(word_id)
);
CREATE TABLE IF NOT EXISTS word_phrasal (
    word_id INTEGER NOT NULL, text TEXT NOT NULL,
    FOREIGN KEY (word_id) REFERENCES content(word_id)
);
-- профиль/настройки пользователя (онбординг, уровень, цель, программа занятий)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, level TEXT, goal TEXT,
    onboarded INTEGER NOT NULL DEFAULT 0, reminder_hour INTEGER, created_at TEXT,
    program TEXT DEFAULT 'free',          -- free | cycle (программа дня)
    -- часы слотов: МИНУТЫ от полуночи (540 = 9:00); схема v1, см. _migrate
    remind_morning INTEGER DEFAULT 540, remind_day INTEGER DEFAULT 840,
    remind_evening INTEGER DEFAULT 1140,
    role TEXT,                            -- доступ: owner|approved|pending|blocked (NULL = незнакомец)
    name TEXT,                            -- имя/username (обновляется на каждом заходе)
    last_seen TEXT                        -- последний заход (CRM)
);
-- след завершённых сессий (ИТОГ): нужен для карты дня (слот SCENARIO)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    ts TEXT NOT NULL, date TEXT NOT NULL, mode TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_user_date ON sessions(user_id, date);
-- технический журнал: ошибки хэндлеров и фидбек тестеров (для следующих правок)
CREATE TABLE IF NOT EXISTS tech_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    user_id INTEGER, kind TEXT NOT NULL,   -- error | feedback
    summary TEXT, trace TEXT
);
-- описания слоёв (reference): этимология корней и пояснения фреймов
CREATE TABLE IF NOT EXISTS root_ref  (root TEXT PRIMARY KEY, idea TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS frame_ref (name TEXT PRIMARY KEY, ru TEXT, when_use TEXT, example TEXT);
-- C1: слои Excel 4–10, 12
CREATE TABLE IF NOT EXISTS phrasal_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrasal TEXT NOT NULL, meaning TEXT, example TEXT, logic TEXT, category TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_phrasal_ref_phrasal ON phrasal_ref(phrasal);
CREATE TABLE IF NOT EXISTS colloc_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    core TEXT NOT NULL,        -- ядро (слово)
    verbs TEXT,                -- типичные глаголы (JSON array)
    adjs TEXT,                 -- типичные прилагательные (JSON array)
    ru TEXT,                   -- русский аналог
    anti TEXT                  -- «осторожно — не говорят» (C1.b)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_colloc_ref_core ON colloc_ref(core);
CREATE TABLE IF NOT EXISTS scenario_ref (
    scenario TEXT PRIMARY KEY,
    opener TEXT, key_phrases TEXT, closer TEXT, context TEXT
);
CREATE TABLE IF NOT EXISTS grammar_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL, when_use TEXT, formula TEXT, example TEXT, ru_mistake TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_grammar_ref_topic ON grammar_ref(topic);
CREATE TABLE IF NOT EXISTS mistakes_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT, wrong TEXT, right TEXT, why TEXT, context TEXT
);
CREATE TABLE IF NOT EXISTS bre_ame_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT, bre TEXT, ame TEXT, ru TEXT
);
CREATE TABLE IF NOT EXISTS confuse_ref (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root TEXT, trap TEXT, group_a TEXT, group_b TEXT, how_to TEXT
);
-- снапшот словников can-do: фиксирует набор слов ДО контент-волн (Dm5)
CREATE TABLE IF NOT EXISTS cando_words (
    cando_id TEXT NOT NULL,
    word_id  INTEGER NOT NULL REFERENCES content(word_id),
    PRIMARY KEY (cando_id, word_id)
);
CREATE INDEX IF NOT EXISTS ix_coll_text   ON word_collocation(text);
CREATE INDEX IF NOT EXISTS ix_fam_member  ON word_family(member);
CREATE INDEX IF NOT EXISTS ix_content_root ON content(root);
CREATE INDEX IF NOT EXISTS ix_content_idea ON content(dna_idea);
CREATE INDEX IF NOT EXISTS ix_err_cat ON errors(category);
"""

@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()        # успех — сохранить
    except Exception:
        c.rollback()      # ошибка — откатить
        raise
    finally:
        c.close()         # в любом случае — закрыть

def _today():
    return datetime.date.today().isoformat()

# ---------- инициализация и seed ----------

def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)

def _migrate(c):
    """Лёгкие миграции для уже существующих баз (ALTER не идемпотентен, проверяем колонки)."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(reviews)").fetchall()}
    if "variant" not in cols:                 # инструментовка A/B: какой карточкой учили
        c.execute("ALTER TABLE reviews ADD COLUMN variant TEXT")
    if "ms" not in cols:                      # время ответа (time-on-task), мс
        c.execute("ALTER TABLE reviews ADD COLUMN ms INTEGER")
    if "direction" not in cols:               # направление карточки: recog (EN→RU) | prod (RU→EN)
        c.execute("ALTER TABLE reviews ADD COLUMN direction TEXT")
    if "card_type" not in cols:               # тип карточки: mcq|cloze|typed|assembly|self
        c.execute("ALTER TABLE reviews ADD COLUMN card_type TEXT")  # для честного A/B (несравнимы)
    scols = {r["name"] for r in c.execute("PRAGMA table_info(state)").fetchall()}
    if scols and "promoted_via" not in scols:  # источник ввода: new|direct|scenario (A1.3)
        c.execute("ALTER TABLE state ADD COLUMN promoted_via TEXT")
    ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if ucols and "reminder_hour" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN reminder_hour INTEGER")
    if ucols and "program" not in ucols:      # программа дня: free | cycle (+ часы слотов)
        c.execute("ALTER TABLE users ADD COLUMN program TEXT DEFAULT 'free'")
        c.execute("ALTER TABLE users ADD COLUMN remind_morning INTEGER DEFAULT 540")
        c.execute("ALTER TABLE users ADD COLUMN remind_day INTEGER DEFAULT 840")
        c.execute("ALTER TABLE users ADD COLUMN remind_evening INTEGER DEFAULT 1140")
    if ucols and "role" not in ucols:         # доступ-по-заявке: роль и имя для /users
        c.execute("ALTER TABLE users ADD COLUMN role TEXT")
        c.execute("ALTER TABLE users ADD COLUMN name TEXT")
    if ucols and "last_seen" not in ucols:    # CRM: когда был последний раз
        c.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
    if c.execute("PRAGMA user_version").fetchone()[0] < 1:
        # v1: слоты перешли с часов на минуты от полуночи. Пересборка users нужна целиком:
        # и данные (часы -> минуты), и ДЕФОЛТЫ колонок — ALTER-дефолты прошлой версии (9/14/19)
        # иначе продолжили бы выдавать «часы» новым строкам. Старый формат мог хранить
        # только целые часы 0–23, поэтому условие <24 безопасно.
        c.execute("ALTER TABLE users RENAME TO users_v0")
        c.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, level TEXT, goal TEXT,
            onboarded INTEGER NOT NULL DEFAULT 0, reminder_hour INTEGER, created_at TEXT,
            program TEXT DEFAULT 'free',
            remind_morning INTEGER DEFAULT 540, remind_day INTEGER DEFAULT 840,
            remind_evening INTEGER DEFAULT 1140, role TEXT, name TEXT, last_seen TEXT)""")
        v0cols = {r["name"] for r in c.execute("PRAGMA table_info(users_v0)")}
        ls = "last_seen" if "last_seen" in v0cols else "NULL"
        c.execute(f"""INSERT INTO users
            SELECT user_id, level, goal, onboarded, reminder_hour, created_at, program,
              CASE WHEN remind_morning IS NOT NULL AND remind_morning < 24
                   THEN remind_morning*60 ELSE remind_morning END,
              CASE WHEN remind_day IS NOT NULL AND remind_day < 24
                   THEN remind_day*60 ELSE remind_day END,
              CASE WHEN remind_evening IS NOT NULL AND remind_evening < 24
                   THEN remind_evening*60 ELSE remind_evening END,
              role, name, {ls}
            FROM users_v0""")
        c.execute("DROP TABLE users_v0")
        c.execute("PRAGMA user_version = 1")

def _index_word_links(c, word_id, family, collocations, phrasal):
    """Переиндексировать связи слова в реляционные таблицы. Идемпотентно по word_id."""
    c.execute("DELETE FROM word_collocation WHERE word_id=?", (word_id,))
    c.execute("DELETE FROM word_family WHERE word_id=?", (word_id,))
    c.execute("DELETE FROM word_phrasal WHERE word_id=?", (word_id,))
    for t in (collocations or []):
        if str(t).strip():
            c.execute("INSERT INTO word_collocation (word_id, text) VALUES (?,?)", (word_id, str(t).strip()))
    for m in (family or []):
        if str(m).strip():
            c.execute("INSERT INTO word_family (word_id, member) VALUES (?,?)", (word_id, str(m).strip()))
    for t in (phrasal or []):
        if str(t).strip():
            c.execute("INSERT INTO word_phrasal (word_id, text) VALUES (?,?)", (word_id, str(t).strip()))

def seed_from_json(path="english_os.json", reset=False):
    """Залить контент из JSON. origin='seed'. Идемпотентно (INSERT OR REPLACE)."""
    data = json.load(open(path, encoding="utf-8"))
    init_db()
    with _conn() as c:
        if reset:
            # контент обновляется upsert-ом (FK-safe, прогресс в state цел);
            # сбрасываем только производные связи — они пересоберутся
            c.execute("DELETE FROM word_collocation")
            c.execute("DELETE FROM word_family")
            c.execute("DELETE FROM word_phrasal")
        for w in data["words"]:
            pr = (w.get("freq") or 0) * (w.get("useful") or 0)
            c.execute("""INSERT INTO content
                (word_id, word, ru, dna_idea, root, family, collocations, phrasal,
                 example, scenario, thinking_frame, register, level, ipa_uk, ipa_us,
                 freq, useful, priority, origin)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'seed')
                ON CONFLICT(word_id) DO UPDATE SET
                  word=excluded.word, ru=excluded.ru, dna_idea=excluded.dna_idea,
                  root=excluded.root, family=excluded.family,
                  collocations=excluded.collocations, phrasal=excluded.phrasal,
                  example=excluded.example, scenario=excluded.scenario,
                  thinking_frame=excluded.thinking_frame, register=excluded.register,
                  level=excluded.level, ipa_uk=excluded.ipa_uk, ipa_us=excluded.ipa_us,
                  freq=excluded.freq, useful=excluded.useful, priority=excluded.priority,
                  origin=excluded.origin""",
                (w["id"], w["word"], w.get("ru"), w.get("dna_idea"), w.get("root"),
                 json.dumps(w.get("family", []), ensure_ascii=False),
                 json.dumps(w.get("collocations", []), ensure_ascii=False),
                 json.dumps(w.get("phrasal", []), ensure_ascii=False),
                 w.get("example"), w.get("scenario"), w.get("thinking_frame"),
                 w.get("register"), w.get("level"), w.get("ipa_uk"), w.get("ipa_us"),
                 w.get("freq"), w.get("useful"), pr))
            _index_word_links(c, w["id"], w.get("family", []),
                              w.get("collocations", []), w.get("phrasal", []))
        _seed_reference(c, data)
    return count_content()

def _seed_reference(c, data):
    """Залить описания слоёв из reference (идемпотентно: INSERT OR REPLACE/IGNORE)."""
    ref = data.get("reference", {})

    # 1. Roots
    for r in ref.get("1. Roots", []):
        root = (r.get("Корень") or "").strip()
        if root:
            c.execute("INSERT OR REPLACE INTO root_ref (root, idea, origin) VALUES (?,?,?)",
                      (root, r.get("Идея"), r.get("Происхождение")))

    # 4. Phrasal Verbs
    for r in ref.get("4. Phrasal Verbs", []):
        ph = (r.get("Фразовый") or "").strip()
        if ph:
            c.execute("""INSERT OR IGNORE INTO phrasal_ref (phrasal, meaning, example, logic, category)
                         VALUES (?,?,?,?,?)""",
                      (ph, r.get("Значение"), r.get("Пример"),
                       r.get("Логика предлога"), r.get("Категория")))

    # 5. Collocations
    for r in ref.get("5. Collocations", []):
        core = (r.get("Ядро (слово)") or "").strip()
        if core:
            verbs = json.dumps((r.get("Типичные глаголы") or "").split(", "), ensure_ascii=False)
            adjs  = json.dumps((r.get("Типичные прилагательные") or "").split(", "), ensure_ascii=False)
            c.execute("""INSERT OR IGNORE INTO colloc_ref (core, verbs, adjs, ru, anti)
                         VALUES (?,?,?,?,?)""",
                      (core, verbs, adjs, r.get("Русский аналог"),
                       r.get("Осторожно — не говорят")))

    # 6. Scenarios
    for r in ref.get("6. Scenarios", []):
        scn = (r.get("Сценарий") or "").strip()
        if scn:
            c.execute("""INSERT OR REPLACE INTO scenario_ref
                         (scenario, opener, key_phrases, closer, context)
                         VALUES (?,?,?,?,?)""",
                      (scn, r.get("Открытие"), r.get("Ключевые фразы"),
                       r.get("Закрытие"), r.get("Контекст")))

    # 7. Thinking Frames
    for f in ref.get("7. Thinking Frames", []):
        name = (f.get("Шаблон") or "").strip()
        if name:
            c.execute("""INSERT OR REPLACE INTO frame_ref (name, ru, when_use, example)
                         VALUES (?,?,?,?)""",
                      (name, f.get("Перевод"), f.get("Когда использовать"), f.get("Пример в речи")))

    # 8. Grammar
    for r in ref.get("8. Grammar", []):
        topic = (r.get("Тема") or "").strip()
        if topic:
            c.execute("""INSERT OR IGNORE INTO grammar_ref
                         (topic, when_use, formula, example, ru_mistake)
                         VALUES (?,?,?,?,?)""",
                      (topic, r.get("Когда использовать"), r.get("Формула"),
                       r.get("Пример"), r.get("Ошибка русскоговорящего")))

    # 9. Mistakes (калька → правильно) — идемпотентность через очистку перед заливкой
    if ref.get("9. Mistakes"):
        c.execute("DELETE FROM mistakes_ref")
        for r in ref["9. Mistakes"]:
            wrong = (r.get("❌ Неправильно (калька)") or "").strip()
            right = (r.get("✅ Правильно") or "").strip()
            if wrong or right:
                c.execute("""INSERT INTO mistakes_ref (category, wrong, right, why, context)
                             VALUES (?,?,?,?,?)""",
                          (r.get("Категория"), wrong, right,
                           r.get("Почему"), r.get("Контекст / RU перевод")))

    # 10. BrE vs AmE — идемпотентность через очистку перед заливкой
    if ref.get("10. BrE vs AmE"):
        c.execute("DELETE FROM bre_ame_ref")
        for r in ref["10. BrE vs AmE"]:
            bre = (r.get("🇬🇧 British") or "").strip()
            ame = (r.get("🇺🇸 American") or "").strip()
            if bre or ame:
                c.execute("""INSERT INTO bre_ame_ref (category, bre, ame, ru)
                             VALUES (?,?,?,?)""",
                          (r.get("Категория"), bre, ame, r.get("Русский")))

    # 12. Do Not Confuse
    if ref.get("12. Do Not Confuse"):
        c.execute("DELETE FROM confuse_ref")
        for r in ref["12. Do Not Confuse"]:
            root = (r.get("Корень") or "").strip()
            if root:
                c.execute("""INSERT INTO confuse_ref
                             (root, trap, group_a, group_b, how_to)
                             VALUES (?,?,?,?,?)""",
                          (root, r.get("В чём ловушка"),
                           r.get("Группа A (смысл 1)"), r.get("Группа B (смысл 2)"),
                           r.get("Как различать")))

def ensure_user_state(user_id=DEFAULT_USER):
    """Завести state='new' для всех слов контента, которых ещё нет у пользователя."""
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO state (user_id, word_id, status, box)
                     SELECT ?, word_id, 'new', 0 FROM content""", (user_id,))

# ---------- пользователь: онбординг / уровень ----------

def is_onboarded(user_id):
    with _conn() as c:
        r = c.execute("SELECT onboarded FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(r and r["onboarded"])

def mark_onboarded(user_id, level):
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, level, onboarded, reminder_hour, created_at)
                     VALUES (?,?,1,10,?)
                     ON CONFLICT(user_id) DO UPDATE SET level=excluded.level, onboarded=1,
                       reminder_hour=COALESCE(users.reminder_hour, 10)""",
                  (user_id, level, _today()))

def set_reminder(user_id, hour):
    """hour 0..23 — включить ежедневное напоминание; None — выключить."""
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, reminder_hour, created_at) VALUES (?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET reminder_hour=excluded.reminder_hour""",
                  (user_id, hour, _today()))

def get_reminder(user_id):
    with _conn() as c:
        r = c.execute("SELECT reminder_hour FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["reminder_hour"] if r else None

def reminder_users(hour):
    """Кому слать обычное напоминание в этот час (только free — cycle ходит по слотам)."""
    with _conn() as c:
        return [r["user_id"] for r in c.execute(
            """SELECT user_id FROM users WHERE reminder_hour=?
               AND (program IS NULL OR program<>'cycle')""", (hour,)).fetchall()]

# ---------- доступ-по-заявке (роли) ----------

def get_role(user_id):
    with _conn() as c:
        r = c.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["role"] if r else None

def set_role(user_id, role):
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, role, created_at) VALUES (?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET role=excluded.role""",
                  (user_id, role, _today()))

def request_access(user_id, name):
    """Заявка незнакомца. True — только при ПЕРВОМ обращении (одно уведомление владельцу
    на заявку); повторные сообщения стучащегося возвращают False."""
    with _conn() as c:
        r = c.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
        if r and r["role"]:                    # роль уже есть (pending/blocked/…) — не повторяем
            return False
        c.execute("""INSERT INTO users (user_id, role, name, created_at) VALUES (?,?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET role='pending', name=excluded.name""",
                  (user_id, "pending", name, _today()))
    return True

def ensure_roles(owner_id, allowed_ids):
    """Бутстрап при старте: владелец -> owner, env-список -> approved.
    Только для строк БЕЗ роли — ручные решения (blocked и т.п.) не затираются."""
    for uid in allowed_ids or ():
        if uid != owner_id and get_role(uid) is None:
            set_role(uid, "approved")
    if owner_id and get_role(owner_id) != "owner":
        set_role(owner_id, "owner")

def touch_user(user_id, name=None):
    """Отметить заход: обновить имя/username и last_seen. Роль НЕ трогаем.
    Зовётся на каждом апдейте — чтобы CRM знала живые имена, а не голые id."""
    now = datetime.datetime.now().isoformat()
    with _conn() as c:
        if name:
            c.execute("""INSERT INTO users (user_id, name, last_seen, created_at)
                         VALUES (?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,
                           last_seen=excluded.last_seen""", (user_id, name, now, _today()))
        else:
            c.execute("""INSERT INTO users (user_id, last_seen, created_at) VALUES (?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen""",
                      (user_id, now, _today()))

def get_name(user_id):
    with _conn() as c:
        r = c.execute("SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["name"] if r and r["name"] else None

def crm_rows():
    """Мини-CRM: все известные люди с ролью, именем, контекстом, активностью."""
    with _conn() as c:
        rows = c.execute("""SELECT u.user_id, u.role, u.name, u.goal, u.last_seen, u.created_at,
                              (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.user_id) sessions,
                              (SELECT COUNT(*) FROM state st
                               WHERE st.user_id=u.user_id AND (st.status='known' OR st.box>=3)) mastered
                            FROM users u
                            ORDER BY (u.last_seen IS NULL), u.last_seen DESC, u.user_id""").fetchall()
    return [dict(r) for r in rows]

def list_users():
    """Все известные пользователи: роль, имя, дата, освоено слов (для /users)."""
    with _conn() as c:
        rows = c.execute("""SELECT u.user_id, u.role, u.name, u.created_at,
                              (SELECT COUNT(*) FROM state s
                               WHERE s.user_id=u.user_id AND (s.status='known' OR s.box>=3)) mastered
                            FROM users u ORDER BY u.created_at, u.user_id""").fetchall()
    return [dict(r) for r in rows]

# ---------- программа дня (free | cycle) ----------

_SLOT_COLS = {"morning": "remind_morning", "day": "remind_day", "evening": "remind_evening"}

def get_goal(user_id):
    """Контекст ученика (профессия/цель) — для персонализации примеров и сценариев."""
    with _conn() as c:
        r = c.execute("SELECT goal FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["goal"] if r and r["goal"] else None

def set_goal(user_id, goal):
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, goal, created_at) VALUES (?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal""",
                  (user_id, goal, _today()))

def get_program(user_id):
    with _conn() as c:
        r = c.execute("SELECT program FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["program"] if r and r["program"] else "free"

def set_program(user_id, program):
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, program, created_at) VALUES (?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET program=excluded.program""",
                  (user_id, program, _today()))

def get_slot_times(user_id):
    """Времена слотов программы дня в МИНУТАХ от полуночи. None у слота = напоминание
    выключено явно; нет строки пользователя — дефолты 540/840/1140."""
    with _conn() as c:
        r = c.execute("""SELECT remind_morning, remind_day, remind_evening
                         FROM users WHERE user_id=?""", (user_id,)).fetchone()
    if not r:
        return {"morning": 540, "day": 840, "evening": 1140}
    return {slot: r[col] for slot, col in _SLOT_COLS.items()}

def set_slot_time(user_id, slot, minutes):
    """minutes — минуты от полуночи (0–1439)."""
    col = _SLOT_COLS[slot]                    # только наши имена колонок, не ввод пользователя
    with _conn() as c:
        c.execute(f"""INSERT INTO users (user_id, {col}, created_at) VALUES (?,?,?)
                      ON CONFLICT(user_id) DO UPDATE SET {col}=excluded.{col}""",
                  (user_id, minutes, _today()))

def slot_users(minute_of_day):
    """Кому в эту минуту суток слотовое напоминание (program='cycle'): [(user_id, slot)],
    slot ∈ new|review|scenario. Закрыт ли слот — решает вызывающий по day_map."""
    out = []
    with _conn() as c:
        rows = c.execute("""SELECT user_id, remind_morning, remind_day, remind_evening
                            FROM users WHERE program='cycle'""").fetchall()
    for r in rows:
        for slot, t in (("new", r["remind_morning"]), ("review", r["remind_day"]),
                        ("scenario", r["remind_evening"])):
            if t is not None and t == minute_of_day:   # None = слот выключен явно
                out.append((r["user_id"], slot))
    return out

def log_session(user_id, mode):
    """След завершённой сессии (пишется при ИТОГе) — критерий слота SCENARIO."""
    with _conn() as c:
        c.execute("INSERT INTO sessions (user_id, ts, date, mode) VALUES (?,?,?,?)",
                  (user_id, datetime.datetime.now().isoformat(), _today(), mode))

def day_map(user_id, day=None):
    """Карта дня: закрыты ли слоты. NEW — введены слова сегодня (кнопкой 🌅 или прямой
    просьбой; сценарные слова НЕ закрывают слот — A1.3); REVIEW — было повторение
    сегодня; SCENARIO — завершена сценарная сессия сегодня."""
    day = day or _today()
    with _conn() as c:
        new = c.execute("""SELECT 1 FROM state WHERE user_id=? AND promoted_at=?
                           AND (promoted_via IS NULL OR promoted_via IN ('new','direct'))
                           LIMIT 1""",
                        (user_id, day)).fetchone() is not None
        rev = c.execute("SELECT 1 FROM reviews WHERE user_id=? AND ts LIKE ? LIMIT 1",
                        (user_id, day + "%")).fetchone() is not None
        scn = c.execute("""SELECT 1 FROM sessions WHERE user_id=? AND date=?
                           AND mode='scenario' LIMIT 1""", (user_id, day)).fetchone() is not None
    return {"new": new, "review": rev, "scenario": scn}

# ---------- внутренняя «полоса комфорта» (НЕ показываем как уровень/статус) ----------
_BANDS = ["A2", "B1", "B2"]

def get_band(user_id):
    with _conn() as c:
        r = c.execute("SELECT level FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r["level"] if r and r["level"] else "A2"

def set_band(user_id, band):
    with _conn() as c:
        c.execute("""INSERT INTO users (user_id, level, created_at) VALUES (?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET level=excluded.level""",
                  (user_id, band, _today()))

def nudge_band(user_id, direction):
    """Ручной руль сложности (Слой Б): ученик сам двигает скрытую полосу.
    direction +1 сложнее / -1 проще. Возвращает (новая_полоса, сдвинулась?)."""
    cur = get_band(user_id)
    i = _BANDS.index(cur) if cur in _BANDS else 0
    j = max(0, min(len(_BANDS) - 1, i + direction))
    if j != i:
        set_band(user_id, _BANDS[j])
        return _BANDS[j], True
    return _BANDS[i], False

def adapt_band(user_id, window=12):
    """Тихо двигаем полосу по последним повторениям, РАЗДЕЛЬНО по направлениям
    (recog EN→RU легче, prod RU→EN труднее — общее окно дёргало полосу от состава колоды).
    Участвуют направления, набравшие полное окно; вверх — ВСЕ такие >= 0.85, вниз — ЛЮБОЕ < 0.5.
    Пользователю уровень НЕ показываем. Возвращает новую полосу, если сдвинули, иначе None."""
    rates = []
    with _conn() as c:
        for d in ("recog", "prod"):
            rows = c.execute("""SELECT remembered FROM reviews
                                WHERE user_id=? AND direction=? ORDER BY id DESC LIMIT ?""",
                             (user_id, d, window)).fetchall()
            if len(rows) == window:
                rates.append(sum(r["remembered"] for r in rows) / window)
    if not rates:
        return None
    cur = get_band(user_id)
    i = _BANDS.index(cur) if cur in _BANDS else 0
    if all(r >= 0.85 for r in rates) and i < len(_BANDS) - 1:
        set_band(user_id, _BANDS[i + 1]); return _BANDS[i + 1]
    if any(r < 0.5 for r in rates) and i > 0:
        set_band(user_id, _BANDS[i - 1]); return _BANDS[i - 1]
    return None

def seed_starter_words(user_id, level, n=5):
    """Ввести в SRS n слов под уровень (и ниже) — стартовый набор после placement."""
    order = ["A1", "A2", "B1", "B2", "C1", "C2"]
    allowed = order[:order.index(level) + 1] if level in order else ["A1", "A2", "B1"]
    qmarks = ",".join("?" * len(allowed))
    with _conn() as c:
        rows = c.execute(f"""SELECT s.word_id FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status='new' AND c.level IN ({qmarks})
                            ORDER BY c.priority DESC LIMIT ?""",
                         (user_id, *allowed, n)).fetchall()
    ids = [r["word_id"] for r in rows]
    start_learning(ids, user_id)
    return ids

# ---------- чтение / форматирование контента (контракт «бэкенд -> агент») ----------

def _row_to_word(r):
    return {
        "word_id": r["word_id"], "word": r["word"], "ru": r["ru"],
        "dna_idea": r["dna_idea"], "root": r["root"],
        "family": json.loads(r["family"] or "[]"),
        "collocations": json.loads(r["collocations"] or "[]"),
        "phrasal": json.loads(r["phrasal"] or "[]"),
        "example": r["example"], "scenario": r["scenario"],
        "thinking_frame": r["thinking_frame"], "register": r["register"],
        "level": r["level"], "ipa_uk": r["ipa_uk"], "ipa_us": r["ipa_us"],
    }

def get_word(word_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM content WHERE word_id=?", (word_id,)).fetchone()
    return _row_to_word(r) if r else None

def format_for_agent(words, with_state=None):
    """Компактный блок данных слова для инъекции в контекст модели.
    Это и есть зафиксированный контракт бэкенд->агент."""
    lines = []
    for w in words:
        root = w["root"] or "—"
        ri = root_info(w["root"])
        if ri:                                  # проверенная этимология из базы (не выдумка ИИ)
            root = f"{root} ({ri['idea']}; {ri['origin']})"
        frame = w["thinking_frame"] or "—"
        fi = frame_info(w["thinking_frame"])
        if fi and fi.get("when_use"):
            frame = f"{frame} — {fi['when_use']}"
        st = ""
        if with_state and w["word_id"] in with_state:
            s = with_state[w["word_id"]]
            st = f" [status={s['status']} box={s['box']}]"
        lines.append(
            f"- {w['word']} ({w['ru']}){st}\n"
            f"  idea={w['dna_idea']} | root={root} | level={w['level']} | register={w['register']}\n"
            f"  family={', '.join(w['family']) or '—'}\n"
            f"  collocations={', '.join(w['collocations']) or '—'} | phrasal={', '.join(w['phrasal']) or '—'}\n"
            f"  example=\"{w['example']}\"\n"
            f"  scenario={w['scenario']} | frame={frame}"
        )
    return "\n".join(lines)

def match_words(text, limit=4):
    """Слова из базы, встретившиеся в реплике — для грунтовки диалога их коллокациями/сетью.
    При избытке совпадений берём самые ценные (priority), а не первые по порядку фразы."""
    toks = sorted({t for t in re.findall(r"[a-zA-Z]+", (text or "").lower())})
    if not toks:
        return []
    qm = ",".join("?" * len(toks))
    with _conn() as c:
        rows = c.execute(f"""SELECT * FROM content WHERE LOWER(word) IN ({qm})
                             ORDER BY priority DESC LIMIT ?""",
                         (*toks, limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def root_info(root):
    """Этимология корня из reference (None, если нет)."""
    if not root or root == "—":
        return None
    with _conn() as c:
        r = c.execute("SELECT root, idea, origin FROM root_ref WHERE root=?", (root,)).fetchone()
    return dict(r) if r else None

def scenario_ref_get(scenario):
    """Opener/key_phrases/closer из scenario_ref для грунтовки _begin_scenario (C1.b)."""
    if not scenario:
        return None
    with _conn() as c:
        r = c.execute(
            "SELECT opener, key_phrases, closer, context FROM scenario_ref WHERE scenario=?",
            (scenario,)
        ).fetchone()
    return dict(r) if r else None

def colloc_anti_matches(words, text):
    """Найти anti-коллокации из colloc_ref для слов, встретившихся в тексте (C1.b).
    words — список dict (из match_words); text — реплика пользователя.
    Возвращает список (core, anti) для тех ядер, чей anti встречается в тексте."""
    if not words or not text:
        return []
    text_lower = text.lower()
    cores = [w["word"].lower() for w in words if w.get("word")]
    if not cores:
        return []
    qm = ",".join("?" * len(cores))
    with _conn() as c:
        rows = c.execute(
            f"SELECT core, anti FROM colloc_ref WHERE LOWER(core) IN ({qm}) AND anti IS NOT NULL",
            cores
        ).fetchall()
    hits = []
    for r in rows:
        anti = (r["anti"] or "").strip().lower()
        if anti and anti in text_lower:
            hits.append((r["core"], r["anti"]))
    return hits

def frame_info(name):
    """Пояснение мыслительного фрейма из reference (None, если нет)."""
    if not name:
        return None
    with _conn() as c:
        r = c.execute("SELECT name, ru, when_use, example FROM frame_ref WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None

def mcq_options(word_id, k=4):
    """k вариантов для карточки-выбора box 1: верный + дистракторы из базы.
    Дистракторы — сначала из той же DNA-идеи/уровня (смысловая близость = честная
    проверка), потом добор любыми. Возвращает [{word_id, word, ru}], верный включён."""
    w = get_word(word_id)
    if not w:
        return []
    with _conn() as c:
        # дистрактор с ТЕМ ЖЕ переводом, что у ответа, — недопустим (в базе есть дубли ru:
        # «оценивать» ×3); иначе ученик видит две одинаковые кнопки и проигрывает честно
        near = c.execute("""SELECT word_id, word, ru FROM content
                            WHERE word_id<>? AND ru IS NOT NULL AND ru<>'' AND ru<>?
                              AND (dna_idea=? OR level=?)
                            ORDER BY RANDOM() LIMIT ?""",
                         (word_id, w["ru"], w["dna_idea"], w["level"], k - 1)).fetchall()
        picked, seen_ru = [], {w["ru"]}
        for r in near:                           # на всякий — дедуп по ru и внутри дистракторов
            if r["ru"] not in seen_ru:
                picked.append(dict(r)); seen_ru.add(r["ru"])
        if len(picked) < k - 1:                  # близких мало — добираем любыми (тоже без дублей ru)
            have = {r["word_id"] for r in picked} | {word_id}
            qm = ",".join("?" * len(have))
            qru = ",".join("?" * len(seen_ru))
            more = c.execute(f"""SELECT word_id, word, ru FROM content
                                 WHERE word_id NOT IN ({qm}) AND ru NOT IN ({qru})
                                   AND ru IS NOT NULL AND ru<>''
                                 ORDER BY RANDOM() LIMIT ?""",
                             (*have, *seen_ru, k - 1 - len(picked))).fetchall()
            for r in more:
                if r["ru"] not in seen_ru:
                    picked.append(dict(r)); seen_ru.add(r["ru"])
    return picked + [{"word_id": word_id, "word": w["word"], "ru": w["ru"]}]

def deep_view(word_id):
    """Глубокий разбор слова ИЗ ГРАФА (для кнопки «🔍 Глубже»): корень/семья/однокоренные/коллокации/фрейм.
    Детерминированно, без ИИ — progressive disclosure по тапу."""
    w = get_word(word_id)
    if not w:
        return ""
    lines = [f"🔍 {w['word']} — {w['ru']}"]
    ri = root_info(w["root"])
    if ri:
        lines.append(f"🌳 корень {w['root']}: {ri['idea']} · {ri['origin']}")
    if w["family"]:
        lines.append("🌱 семья: " + ", ".join(w["family"]))
    if w["root"] and w["root"] != "—":
        sib = [x["word"] for x in words_by_root(w["root"]) if x["word"] != w["word"]]
        if sib:
            lines.append("🧩 однокоренные в базе: " + ", ".join(sib))
    if w["collocations"]:
        lines.append("💬 коллокации: " + ", ".join(w["collocations"]))
    if w["thinking_frame"]:
        fi = frame_info(w["thinking_frame"])
        lines.append("🧠 фрейм: " + w["thinking_frame"] + (f" — {fi['ru']}" if fi and fi.get("ru") else ""))
    return "\n".join(lines)

def branch_words(word_id, user_id=DEFAULT_USER, n=5):
    """«Ветка» слова: однокоренные + члены семьи, что есть отдельными словами в базе (без самого слова)."""
    w = get_word(word_id)
    if not w:
        return []
    out = {}
    if w["root"] and w["root"] != "—":
        for x in words_by_root(w["root"]):
            if x["word_id"] != word_id:
                out[x["word_id"]] = x
    for fam in (w["family"] or []):
        wid = find_word_id(fam)
        if wid and wid != word_id and wid not in out:
            out[wid] = get_word(wid)
    return list(out.values())[:n]

# ---------- темы: навигация по DNA-идеям и сценариям ----------
def idea_list():
    with _conn() as c:
        return [(r["dna_idea"], r["n"]) for r in c.execute(
            """SELECT dna_idea, COUNT(*) n FROM content
               WHERE dna_idea IS NOT NULL AND dna_idea<>'' GROUP BY dna_idea ORDER BY n DESC""").fetchall()]

def scenario_list(min_n=5):
    """Сценарии для меню «Темы». Огрызки (< min_n слов) не показываем —
    пустая полка хуже её отсутствия; слова при этом остаются доступны в учёбе."""
    with _conn() as c:
        return [(r["scenario"], r["n"]) for r in c.execute(
            """SELECT scenario, COUNT(*) n FROM content
               WHERE scenario IS NOT NULL AND scenario<>'' GROUP BY scenario
               HAVING n >= ? ORDER BY n DESC""", (min_n,)).fetchall()]

def scenario_deficit():
    """Число слов в content по (scenario, level) — для отчёта /fill.
    Возвращает {scenario: {level: count, ...}, ...}."""
    with _conn() as c:
        rows = c.execute("""
            SELECT scenario, level, COUNT(*) n FROM content
            WHERE scenario IS NOT NULL AND level IS NOT NULL
            GROUP BY scenario, level ORDER BY scenario, level
        """).fetchall()
    agg = {}
    for r in rows:
        scn = r["scenario"]
        if scn not in agg:
            agg[scn] = {}
        agg[scn][r["level"]] = r["n"]
    return agg

def theme_words(axis, value, user_id=DEFAULT_USER, n=5, band=None):
    """Слова темы (axis 'idea'|'scn'): сначала ещё не введённые (new), потом любые.
    band — полоса комфорта: слова уровня полосы и ниже идут первыми (скрытая адаптация)."""
    col = "dna_idea" if axis == "idea" else "scenario"
    with _conn() as c:
        if band:
            order = ["A1", "A2", "B1", "B2", "C1", "C2"]
            allowed = order[:order.index(band) + 1] if band in order else order
            qm = ",".join("?" * len(allowed))
            rows = c.execute(f"""SELECT c.word_id FROM content c
                                 JOIN state s ON s.word_id=c.word_id AND s.user_id=?
                                 WHERE c.{col}=?
                                 ORDER BY (c.level IN ({qm})) DESC, (s.status='new') DESC,
                                          c.priority DESC LIMIT ?""",
                             (user_id, value, *allowed, n)).fetchall()
        else:
            rows = c.execute(f"""SELECT c.word_id FROM content c
                                 JOIN state s ON s.word_id=c.word_id AND s.user_id=?
                                 WHERE c.{col}=?
                                 ORDER BY (s.status='new') DESC, c.priority DESC LIMIT ?""",
                             (user_id, value, n)).fetchall()
    return [get_word(r["word_id"]) for r in rows]

# ---------- сеть: запросы по связям ----------

def words_by_root(root):
    """Все слова с данным корнем. Реализует тезис «один корень -> много слов»."""
    if not root or root == "—":
        return []
    with _conn() as c:
        rows = c.execute("SELECT word_id, word, ru FROM content WHERE root=? ORDER BY word",
                         (root,)).fetchall()
    return [dict(r) for r in rows]

def words_by_idea(idea):
    """Все слова данной DNA-идеи."""
    if not idea:
        return []
    with _conn() as c:
        rows = c.execute("SELECT word_id, word, ru FROM content WHERE dna_idea=? ORDER BY word",
                         (idea,)).fetchall()
    return [dict(r) for r in rows]

def search_collocation(text):
    """Слова, у которых есть коллокация, содержащая подстроку text."""
    if not text:
        return []
    with _conn() as c:
        rows = c.execute("""SELECT DISTINCT c.word_id, c.word, wc.text AS collocation
                            FROM word_collocation wc JOIN content c USING(word_id)
                            WHERE wc.text LIKE ? ORDER BY c.word""",
                         (f"%{text}%",)).fetchall()
    return [dict(r) for r in rows]

def neighbors(word_id):
    """Соседи слова по сети: другие слова с тем же корнем и с той же DNA-идеей."""
    w = get_word(word_id)
    if not w:
        return {}
    same_root = ([x for x in words_by_root(w["root"]) if x["word_id"] != word_id]
                 if w["root"] and w["root"] != "—" else [])
    same_idea = [x for x in words_by_idea(w["dna_idea"]) if x["word_id"] != word_id]
    return {"word": w["word"], "root": w["root"], "idea": w["dna_idea"],
            "same_root": same_root, "same_idea": same_idea,
            "family": w["family"], "collocations": w["collocations"]}

# ---------- SRS ----------

def new_pool(user_id=DEFAULT_USER, limit=20, band=None):
    """Кандидаты на ввод в оборот: status='new', по убыванию ценности.
    band задаёт полосу комфорта — слова этого уровня и ниже идут первыми (адаптация)."""
    with _conn() as c:
        if band:
            order = ["A1", "A2", "B1", "B2", "C1", "C2"]
            allowed = order[:order.index(band) + 1] if band in order else order
            qm = ",".join("?" * len(allowed))
            rows = c.execute(f"""SELECT c.* FROM state s JOIN content c USING(word_id)
                                WHERE s.user_id=? AND s.status='new'
                                ORDER BY (c.level IN ({qm})) DESC, c.priority DESC, c.word_id ASC
                                LIMIT ?""", (user_id, *allowed, limit)).fetchall()
        else:
            rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                                WHERE s.user_id=? AND s.status='new'
                                ORDER BY c.priority DESC, c.word_id ASC LIMIT ?""",
                             (user_id, limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def learning_words(user_id=DEFAULT_USER, limit=8):
    """Активный словарь (status learning/forgot) — материал для понятного ввода i+1."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status IN ('learning','forgot')
                            ORDER BY c.priority DESC, c.word_id ASC LIMIT ?""",
                         (user_id, limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def mature_words(user_id=DEFAULT_USER, limit=60):
    """Хорошо усвоенные слова (known или box>=3) — известная база текста (правило 98%).
    Лимит 60 (A5.1): модель должна видеть реальную базу ученика, а не верхушку."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND (s.status='known' OR s.box>=3)
                            ORDER BY s.box DESC, c.priority DESC LIMIT ?""",
                         (user_id, limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def target_words(user_id=DEFAULT_USER, limit=4):
    """Незрелые активные слова (learning/forgot, box<3) — это «новые ~2%» для текста ввода.
    B8: из приоритетного пула (3×limit) берём случайные limit — два /read подряд не крутят
    одни и те же слова. Пул узок (< limit) → отдаём всё; рандом включается, когда есть из чего."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status IN ('learning','forgot') AND s.box<3
                            ORDER BY c.priority DESC, c.word_id ASC LIMIT ?""",
                         (user_id, limit * 3)).fetchall()
    rows = list(rows)
    if len(rows) > limit:
        rows = random.sample(rows, limit)
    return [_row_to_word(r) for r in rows]

def new_remaining(user_id=DEFAULT_USER):
    """Сколько ещё слов со статусом 'new' осталось в базе у пользователя."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND status='new'",
                         (user_id,)).fetchone()[0]

def promoted_today(user_id=DEFAULT_USER):
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND promoted_at=?",
                         (user_id, _today())).fetchone()[0]

def promote_new(user_id=DEFAULT_USER, n=DAILY_NEW_CAP):
    """Ввести в оборот до n самых ценных new-слов (кнопка 🌅), но не больше дневного
    лимита И не больше остатка единого бюджета ввода (прямой ввод его уже мог потратить)."""
    remaining = min(max(0, DAILY_NEW_CAP - promoted_today(user_id)),
                    intake_budget_left(user_id))
    n = min(n, remaining)
    if n <= 0:
        return []
    pool = new_pool(user_id, limit=n, band=get_band(user_id))   # адаптивно: по полосе комфорта
    today = _today()
    with _conn() as c:
        for w in pool:
            c.execute("""UPDATE state SET status='learning', box=1,
                         last_review=?, next_review=?, promoted_at=?, promoted_via='new'
                         WHERE user_id=? AND word_id=?""",
                      (today, today, today, user_id, w["word_id"]))
    return pool

def intake_budget_left(user_id=DEFAULT_USER):
    """Сколько слов ещё можно ввести сегодня прямым путём (темы/ветка/«учим»/сценарий).
    Единый дневной бюджет cap×MULT — защита от лавины (раньше эти пути шли мимо капа)."""
    return max(0, DAILY_NEW_CAP * DIRECT_BUDGET_MULT - promoted_today(user_id))

def start_learning(ids, user_id=DEFAULT_USER, via="direct"):
    """Ввести конкретные слова в оборот («учим X», темы, ветка, сценарий) В ПРЕДЕЛАХ
    дневного бюджета. via — источник ввода (direct|scenario): сценарные слова не должны
    закрывать слот NEW в карте дня (A1.3). Возвращает список реально введённых word_id."""
    budget = intake_budget_left(user_id)
    if budget <= 0:
        return []
    today = _today()
    added = []
    with _conn() as c:
        for wid in ids:
            if len(added) >= budget:
                break
            cur = c.execute("""UPDATE state SET status='learning', box=1,
                               last_review=?, next_review=?, promoted_at=?, promoted_via=?
                               WHERE user_id=? AND word_id=? AND status='new'""",
                            (today, today, today, via, user_id, wid))
            if cur.rowcount:
                added.append(wid)
    return added

def due_count(user_id=DEFAULT_USER):
    """Сколько всего слов к повторению сегодня (без капа) — для «ещё N ждут»."""
    today = _today()
    with _conn() as c:
        return c.execute("""SELECT COUNT(*) FROM state
                            WHERE user_id=? AND status IN ('learning','forgot','known')
                              AND next_review IS NOT NULL AND next_review<=?""",
                         (user_id, today)).fetchone()[0]

def _interleave_by_theme(rows):
    """A4.3 (интерливинг): внутри ОДНОГО дня просрочки чередуем темы — слова одного
    батча/сценария не идут стопкой (перемешивание усиливает retention). Порядок дней
    («самые просроченные раньше») сохраняется; внутри темы — исходный порядок (priority)."""
    out, i = [], 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j]["s_next"] == rows[i]["s_next"]:
            j += 1                                   # [i:j] — один день просрочки
        groups, order = {}, []
        for r in rows[i:j]:
            key = r["scenario"] or r["dna_idea"] or ""
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)
        while any(groups[k] for k in order):         # round-robin по темам дня
            for k in order:
                if groups[k]:
                    out.append(groups[k].pop(0))
        i = j
    return out

def due_today(user_id=DEFAULT_USER, limit=None):
    """Слова к повторению: learning/forgot + созревшие known (maintenance) с
    next_review<=сегодня. Самые просроченные первыми, внутри дня — темы чередуются
    (A4.3). limit — кап колоды (B1)."""
    today = _today()
    cap = f"LIMIT {int(limit)}" if limit else ""
    with _conn() as c:
        rows = c.execute(f"""SELECT c.*, s.status AS s_status, s.box AS s_box,
                                   s.next_review AS s_next
                            FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status IN ('learning','forgot','known')
                              AND s.next_review IS NOT NULL AND s.next_review<=?
                            ORDER BY s.next_review ASC, c.priority DESC {cap}""",
                         (user_id, today)).fetchall()
    rows = _interleave_by_theme(rows)
    words = [_row_to_word(r) for r in rows]
    st = {r["word_id"]: {"status": r["s_status"], "box": r["s_box"]} for r in rows}
    return words, st

def fresh_today(user_id=DEFAULT_USER, limit=1):
    """Слова, введённые СЕГОДНЯ и ещё в box 1 — кандидаты на «тёплое превью» продукции
    (A4.1): показать ядро продукта в день 1, вне SRS-учёта."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.promoted_at=? AND s.box=1
                              AND s.status='learning'
                            ORDER BY c.priority DESC LIMIT ?""",
                         (user_id, _today(), limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def review(word_id, remembered, user_id=DEFAULT_USER, variant=None, ms=None, card_type=None):
    """Обновить SRS после повторения. remembered: True/False.
    variant ('layered'/'flat'), ms (время ответа), card_type (mcq/cloze/typed/assembly/self)
    — инструментовка для честного A/B (форматы несравнимы по ms и по типу усилия)."""
    today = _today()
    with _conn() as c:
        r = c.execute("SELECT box,status FROM state WHERE user_id=? AND word_id=?",
                      (user_id, word_id)).fetchone()
        if r is None:
            raise ValueError(f"нет state для word_id={word_id}")
        box = r["box"] or 1
        direction = "prod" if box >= PRODUCTIVE_FROM_BOX else "recog"   # по box ДО обновления
        if remembered:
            already_known = box == 5            # maintenance: слово уже было «выучено»
            box = min(box + 1, 5)
            status = "known" if box == 5 else "learning"
            days = MAINTENANCE_DAYS if already_known else INTERVALS[box]
        else:
            # мягкий Лейтнер (A3.1) + провал проверки выживания (4.1, вариант c, канон Ч.3.8):
            # box 5 (maintenance) провален -> в оборот (box 1): провал выживания = реальное
            # угасание, освоенным числиться не должен (can-do-прокси не врёт);
            # box 3-4 (ещё созревают) -> мягкий минус-1; незрелое (1-2) -> в начало.
            box = 1 if box == 5 else (box - 1 if box >= 3 else 1)
            status = "forgot"
            days = INTERVALS[box]
        nxt = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
        c.execute("""UPDATE state SET box=?, status=?, last_review=?, next_review=?
                     WHERE user_id=? AND word_id=?""",
                  (box, status, today, nxt, user_id, word_id))
        c.execute("""INSERT INTO reviews
                     (user_id, word_id, ts, remembered, variant, ms, direction, card_type)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (user_id, word_id, datetime.datetime.now().isoformat(),
                   int(remembered), variant, ms, direction, card_type))
    return {"word_id": word_id, "box": box, "status": status, "next_review": nxt}

def variant_stats(user_id=DEFAULT_USER):
    """Сводка A/B с атрибуцией к КОДИРОВАНИЮ: сеть показывается при раскрытии ответа,
    значит влияет на СЛЕДУЮЩЕЕ вспоминание слова — результат повторения относим к
    варианту предыдущего показа. Первое повторение слова (нет прошлого показа) не участвует.
    Это шина для проверки «слои vs плоско», а не само доказательство."""
    with _conn() as c:
        rows = c.execute("""SELECT prev.variant variant,
                                   COUNT(*) n,
                                   SUM(cur.remembered) ok,
                                   AVG(cur.ms) avg_ms
                            FROM reviews cur
                            JOIN reviews prev ON prev.id =
                              (SELECT MAX(p.id) FROM reviews p
                               WHERE p.user_id=cur.user_id AND p.word_id=cur.word_id
                                 AND p.id < cur.id)
                            WHERE cur.user_id=? AND prev.variant IS NOT NULL
                            GROUP BY prev.variant""", (user_id,)).fetchall()
    out = []
    for r in rows:
        n = r["n"] or 0
        out.append({"variant": r["variant"], "n": n,
                    "accuracy": round((r["ok"] or 0) / n, 3) if n else None,
                    "avg_ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None})
    return out

# ---------- интейк новых слов (агент -> pending -> подтверждение) ----------

def add_pending(word, payload, user_id=DEFAULT_USER):
    with _conn() as c:
        cur = c.execute("""INSERT INTO pending (user_id, word, payload, created_at)
                           VALUES (?,?,?,?)""",
                        (user_id, word, json.dumps(payload, ensure_ascii=False), _today()))
        return cur.lastrowid

def list_pending(user_id=DEFAULT_USER):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pending WHERE user_id=? AND status='pending' ORDER BY id",
            (user_id,)).fetchall()]

def confirm_pending(pending_id, user_id=DEFAULT_USER):
    """Подтвердить: перенести сгенерённое слово в content (origin='added') + завести state='new'."""
    with _conn() as c:
        p = c.execute("SELECT * FROM pending WHERE id=? AND user_id=?",
                      (pending_id, user_id)).fetchone()
        if p is None or p["status"] != "pending":
            return None
        payload = json.loads(p["payload"] or "{}")
        existing = c.execute("SELECT word_id FROM content WHERE LOWER(word)=LOWER(?)",
                             (p["word"].strip(),)).fetchone()
        if existing:                       # слово уже есть — не дублируем
            c.execute("UPDATE pending SET status='confirmed' WHERE id=?", (pending_id,))
            return existing["word_id"]
        new_id = (c.execute("SELECT COALESCE(MAX(word_id),0)+1 FROM content").fetchone()[0])
        pr = (payload.get("freq") or 3) * (payload.get("useful") or 3)
        c.execute("""INSERT INTO content
            (word_id, word, ru, dna_idea, root, family, collocations, phrasal,
             example, scenario, thinking_frame, register, level, ipa_uk, ipa_us,
             freq, useful, priority, origin)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'added')""",
            (new_id, p["word"], payload.get("ru"), payload.get("dna_idea"),
             payload.get("root"),
             json.dumps(payload.get("family", []), ensure_ascii=False),
             json.dumps(payload.get("collocations", []), ensure_ascii=False),
             json.dumps(payload.get("phrasal", []), ensure_ascii=False),
             payload.get("example"), payload.get("scenario"),
             payload.get("thinking_frame"), payload.get("register", "neutral"),
             payload.get("level", "B1"), payload.get("ipa_uk"), payload.get("ipa_us"),
             payload.get("freq", 3), payload.get("useful", 3), pr))
        _index_word_links(c, new_id, payload.get("family", []),
                          payload.get("collocations", []), payload.get("phrasal", []))
        c.execute("""INSERT OR IGNORE INTO state (user_id, word_id, status, box)
                     VALUES (?,?, 'new', 0)""", (user_id, new_id))
        c.execute("UPDATE pending SET status='confirmed' WHERE id=?", (pending_id,))
    return new_id

def confirm_all_pending(user_id=DEFAULT_USER):
    """Подтвердить всю очередь разом (кнопка «Подтвердить все»). Возвращает число
    обработанных; слова, уже попавшие в content, схлопываются в confirm_pending."""
    n = 0
    for item in list_pending(user_id):
        if confirm_pending(item["id"], user_id) is not None:
            n += 1
    return n

def reject_pending(pending_id, user_id=DEFAULT_USER):
    """Отклонить слово из очереди (status='rejected'). True, если что-то изменилось."""
    with _conn() as c:
        cur = c.execute("UPDATE pending SET status='rejected' "
                        "WHERE id=? AND user_id=? AND status='pending'",
                        (pending_id, user_id))
        return cur.rowcount > 0

# ---------- применение машинного итога сессии (ИТОГ -> reviews/pending) ----------

def find_word_id(word):
    """Найти word_id по тексту слова (без учёта регистра). None, если нет в базе."""
    if not word:
        return None
    with _conn() as c:
        r = c.execute("SELECT word_id FROM content WHERE LOWER(word)=LOWER(?)",
                      (word.strip(),)).fetchone()
    return r["word_id"] if r else None

def find_ru_clash(ru, exclude_word=None):
    """Найти слова в content с таким же ru (без учёта регистра/пробелов).
    Python-сравнение: SQLite LOWER() не покрывает кириллицу.
    Возвращает список слов-дублей (исключая exclude_word, если задан)."""
    if not ru:
        return []
    target = ru.strip().lower()
    with _conn() as c:
        rows = c.execute("SELECT word, ru FROM content WHERE ru IS NOT NULL").fetchall()
    results = [r["word"] for r in rows if r["ru"] and r["ru"].strip().lower() == target]
    if exclude_word:
        results = [w for w in results if w.lower() != exclude_word.lower()]
    return results

def _itog_can_move(word_id, user_id):
    """A1.1: ИТОГ двигает SRS осторожно. False — слово введено СЕГОДНЯ (только что
    закодировано, интервала не было — «вспомнил» тривиален) или уже двигалось ИТОГом
    сегодня (повторный ИТОГ/двойной учёт размывает SRS-математику)."""
    today = _today()
    with _conn() as c:
        r = c.execute("SELECT promoted_at FROM state WHERE user_id=? AND word_id=?",
                      (user_id, word_id)).fetchone()
        if r is not None and r["promoted_at"] == today:
            return False
        moved = c.execute("""SELECT 1 FROM reviews WHERE user_id=? AND word_id=?
                             AND card_type='itog' AND ts LIKE ? LIMIT 1""",
                          (user_id, word_id, today + "%")).fetchone()
    return moved is None

def apply_session_summary(data, user_id=DEFAULT_USER):
    """Применить машинный итог сессии: reviewed -> SRS (review), add -> очередь pending.
    Мусор (неизвестное слово / нет state) пропускается, не падает.
    reviewed двигает box максимум на +1 и не чаще раза в день (card_type='itog');
    слова, введённые сегодня, не двигаются вовсе (A1.1).
    Возвращает счётчики {'ok','fail','added','skipped'}."""
    ok = fail = added = skipped = 0
    for item in (data.get("reviewed") or []):
        wid = find_word_id(item.get("word", ""))
        if wid is None:
            skipped += 1
            continue
        if not _itog_can_move(wid, user_id):     # свежевыученное / уже двигалось ИТОГом
            skipped += 1
            continue
        try:
            review(wid, bool(item.get("ok")), user_id, card_type="itog")
        except ValueError:          # нет state для слова — пропустить
            skipped += 1
            continue
        if item.get("ok"):
            ok += 1
        else:
            fail += 1
    for item in (data.get("add") or []):
        w = (item.get("word") or "").strip()
        if not w:
            skipped += 1
            continue
        if find_word_id(w) is not None:     # уже в базе — не дублируем
            skipped += 1
            continue
        add_pending(w, item, user_id)
        added += 1
    errors = 0
    for e in (data.get("errors") or []):
        cat = (e.get("category") or "other").strip().lower()
        if cat not in ERROR_CATEGORIES:
            cat = "other"
        log_error(cat, e.get("wrong", ""), e.get("correct", ""), e.get("cause", ""), user_id)
        errors += 1
    return {"ok": ok, "fail": fail, "added": added, "skipped": skipped, "errors": errors}

# ---------- движок структурных ошибок (фреймы) ----------

ERROR_CATEGORIES = {"word_order", "article", "preposition", "tense_aspect",
                    "agreement", "word_choice", "other"}

def log_error(category, wrong, correct, note, user_id=DEFAULT_USER):
    """Записать одну структурную ошибку речи."""
    with _conn() as c:
        c.execute("""INSERT INTO errors (user_id, ts, category, wrong, correct, note)
                     VALUES (?,?,?,?,?,?)""",
                  (user_id, datetime.datetime.now().isoformat(), category, wrong, correct, note))

def error_patterns(user_id=DEFAULT_USER, limit=5):
    """Повторяющиеся структурные паттерны: счётчик по категориям (по убыванию)."""
    with _conn() as c:
        rows = c.execute("""SELECT category, COUNT(*) n FROM errors WHERE user_id=?
                            GROUP BY category ORDER BY n DESC LIMIT ?""",
                         (user_id, limit)).fetchall()
    return [dict(r) for r in rows]

def recent_errors(category, user_id=DEFAULT_USER, limit=3):
    """Последние примеры ошибок данной категории."""
    with _conn() as c:
        rows = c.execute("""SELECT wrong, correct, note FROM errors
                            WHERE user_id=? AND category=? ORDER BY id DESC LIMIT ?""",
                         (user_id, category, limit)).fetchall()
    return [dict(r) for r in rows]

# ---------- измеримость: CEFR can-do (без BEC) ----------
# Банк деловых «могу сделать X», сформулированных в стиле CEFR-дескрипторов и привязанных
# к РЕАЛЬНЫМ сценариям контента. Это собственный task-bank (ориентир), а не сертификация.
CANDO = [
    {"id": "smalltalk", "level": "A2", "scenario": "Small talk",
     "ru": "Могу поддержать короткий small talk до встречи"},
    {"id": "help",      "level": "A2", "scenario": "Asking for help",
     "ru": "Могу попросить о помощи и уточнить непонятное"},
    {"id": "email_req", "level": "B1", "scenario": "Email — request",
     "ru": "Могу написать короткое деловое письмо-просьбу"},
    {"id": "status",    "level": "B1", "scenario": "Status update",
     "ru": "Могу доложить статус задачи на встрече"},
    {"id": "followup",  "level": "B1", "scenario": "Email — follow-up",
     "ru": "Могу написать письмо-напоминание (follow-up)"},
    {"id": "network",   "level": "B1", "scenario": "Networking",
     "ru": "Могу представиться и завязать профессиональный контакт"},
    {"id": "interview", "level": "B1", "scenario": "Job interview",
     "ru": "Могу пройти базовое собеседование"},
    {"id": "feedback",  "level": "B2", "scenario": "Giving feedback",
     "ru": "Могу дать конструктивную обратную связь коллеге"},
    {"id": "disagree",  "level": "B2", "scenario": "Disagreeing politely",
     "ru": "Могу вежливо выразить несогласие в обсуждении"},
    {"id": "negotiate", "level": "B2", "scenario": "Negotiating",
     "ru": "Могу обсудить условия и прийти к договорённости"},
    {"id": "pitch",     "level": "B2", "scenario": "Pitching",
     "ru": "Могу кратко презентовать идею или продукт"},
    {"id": "brainstorm","level": "B2", "scenario": "Brainstorm",
     "ru": "Могу участвовать в мозговом штурме и предлагать идеи"},
]

def my_words(user_id=DEFAULT_USER):
    """Список слов в работе + счётчики освоенного/нового — для «📒 Мои слова».
    Детерминированно из базы (без LLM): ученик видит, ЧТО он учит."""
    with _conn() as c:
        learning = [{"word": r["word"], "ru": r["ru"], "box": r["box"]}
                    for r in c.execute(
                        """SELECT cc.word, cc.ru, s.box FROM state s JOIN content cc USING(word_id)
                           WHERE s.user_id=? AND s.status IN ('learning','forgot') AND s.box<3
                           ORDER BY s.box DESC, cc.priority DESC""", (user_id,))]
        mastered_n = c.execute("""SELECT COUNT(*) FROM state
                                  WHERE user_id=? AND (status='known' OR box>=3)""",
                               (user_id,)).fetchone()[0]
        new_n = c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND status='new'",
                          (user_id,)).fetchone()[0]
    return {"learning": learning, "mastered_n": mastered_n, "new_n": new_n}

def progress_summary(user_id=DEFAULT_USER):
    """Сводка пути для /progress. Показывает УСИЛИЕ (повторения, точность) и три ступени
    зрелости, а не только финал — иначе после большой сессии «освоено 0» демотивирует.
    Числа — дорога к покрытию (Nation), а не трофеи (канон Ч.5)."""
    with _conn() as c:
        mastered = c.execute("""SELECT COUNT(*) FROM state
                                WHERE user_id=? AND (status='known' OR box>=3)""",
                             (user_id,)).fetchone()[0]
        familiar = c.execute("""SELECT COUNT(*) FROM state
                                WHERE user_id=? AND status IN ('learning','forgot') AND box<3""",
                             (user_id,)).fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND status='new'",
                        (user_id,)).fetchone()[0]
        sessions = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?",
                             (user_id,)).fetchone()[0]
        inputs = c.execute("""SELECT COUNT(*) FROM sessions
                              WHERE user_id=? AND mode='input'""",   # A5.2: чтение видно
                           (user_id,)).fetchone()[0]
        reviews = c.execute("SELECT COUNT(*) FROM reviews WHERE user_id=?",
                            (user_id,)).fetchone()[0]
        ok = c.execute("SELECT COALESCE(SUM(remembered),0) FROM reviews WHERE user_id=?",
                       (user_id,)).fetchone()[0]
        first = c.execute("""SELECT MIN(d) FROM (
                               SELECT MIN(date) d FROM sessions WHERE user_id=?
                               UNION ALL
                               SELECT MIN(substr(ts,1,10)) d FROM reviews WHERE user_id=?)""",
                          (user_id, user_id)).fetchone()[0]
    return {"mastered": mastered, "familiar": familiar, "learning": familiar, "new": new,
            "sessions": sessions, "inputs": inputs, "reviews": reviews,
            "accuracy": round(ok / reviews * 100) if reviews else None,
            "since": first, "nation_target": NATION_TARGET}

def cando_snapshot(force=False):
    """Зафиксировать текущий набор слов по сценарию в cando_words (Dm5).
    Идемпотентно: пропускает can-do, у которых снапшот уже есть (если не force=True).
    Возвращает dict {cando_id: words_added}."""
    added = {}
    with _conn() as c:
        for cd in CANDO:
            if not force:
                exists = c.execute(
                    "SELECT 1 FROM cando_words WHERE cando_id=? LIMIT 1",
                    (cd["id"],)
                ).fetchone()
                if exists:
                    added[cd["id"]] = 0
                    continue
            rows = c.execute(
                "SELECT word_id FROM content WHERE scenario=?",
                (cd["scenario"],)
            ).fetchall()
            n = 0
            for r in rows:
                try:
                    c.execute("INSERT OR IGNORE INTO cando_words(cando_id, word_id) VALUES(?,?)",
                              (cd["id"], r["word_id"]))
                    n += 1
                except Exception:
                    pass
            added[cd["id"]] = n
    return added

def cando_progress(user_id=DEFAULT_USER, ready_ratio=0.6):
    """Прогресс по can-do: доля освоенных (known/box>=3) слов сценария.
    Знаменатель — снапшот cando_words (Dm5); если снапшота нет — живой счёт.
    ready = есть слова, доля >= ready_ratio и освоено хотя бы 2."""
    out = []
    with _conn() as c:
        for cd in CANDO:
            snap_rows = c.execute(
                """SELECT COUNT(*) snap_total,
                          SUM(CASE WHEN s.status='known' OR s.box>=3 THEN 1 ELSE 0 END) mastered
                   FROM cando_words cw
                   LEFT JOIN state s ON s.word_id=cw.word_id AND s.user_id=?
                   WHERE cw.cando_id=?""",
                (user_id, cd["id"])
            ).fetchone()
            snap_total = snap_rows["snap_total"] or 0
            if snap_total > 0:
                total = snap_total
                mastered = snap_rows["mastered"] or 0
            else:
                # снапшота нет — фолбэк на живой счёт (до первого cando_snapshot())
                live = c.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN s.status='known' OR s.box>=3 THEN 1 ELSE 0 END) mastered
                       FROM content cc JOIN state s USING(word_id)
                       WHERE s.user_id=? AND cc.scenario=?""",
                    (user_id, cd["scenario"])
                ).fetchone()
                total = live["total"] or 0
                mastered = live["mastered"] or 0
            pct = round(mastered / total, 2) if total else 0.0
            out.append({**cd, "mastered": mastered, "total": total, "pct": pct,
                        "ready": total > 0 and pct >= ready_ratio and mastered >= 2})
    return out

# ---------- профиль ученика («память» тьютора, строго из данных) ----------

_CAT_RU = {"word_order": "порядок слов", "article": "артикли", "preposition": "предлоги",
           "tense_aspect": "время/вид", "agreement": "согласование",
           "word_choice": "выбор слова", "other": "прочее"}

def learner_profile(user_id=DEFAULT_USER, focus_n=5):
    """Компактный профиль ученика из БД для инъекции в промпт. Только факты, без выдумок."""
    with _conn() as c:
        mastered = c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND (status='known' OR box>=3)",
                             (user_id,)).fetchone()[0]
        learning = c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND status IN ('learning','forgot') AND box<3",
                             (user_id,)).fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM state WHERE user_id=? AND status='new'",
                        (user_id,)).fetchone()[0]
        lvl = {r["lvl"]: r["n"] for r in c.execute(
            """SELECT cc.level lvl, COUNT(*) n FROM state s JOIN content cc USING(word_id)
               WHERE s.user_id=? AND (s.status='known' OR s.box>=3) AND cc.level IS NOT NULL
               GROUP BY cc.level""", (user_id,))}
        focus = [r["word"] for r in c.execute(
            """SELECT cc.word FROM state s JOIN content cc USING(word_id)
               WHERE s.user_id=? AND s.status IN ('learning','forgot')
               ORDER BY cc.priority DESC LIMIT ?""", (user_id, focus_n))]
        errs = [(r["category"], r["n"]) for r in c.execute(
            """SELECT category, COUNT(*) n FROM errors WHERE user_id=?
               GROUP BY category ORDER BY n DESC LIMIT 3""", (user_id,))]
    est = "—"
    for L in ["A1", "A2", "B1", "B2", "C1", "C2"]:
        if lvl.get(L, 0) >= 3:
            est = L
    parts = [f"уровень ~{est}", f"освоено {mastered}, учит {learning}, новых {new}"]
    if focus:
        parts.append("сейчас в работе: " + ", ".join(focus))
    if errs:
        parts.append("частые ошибки: " + ", ".join(f"{_CAT_RU.get(c, c)} ({n}×)" for c, n in errs))
    out = ("ПРОФИЛЬ УЧЕНИКА (факты из базы — опирайся на них, прошлое не выдумывай): "
           + "; ".join(parts) + ".")
    goal = get_goal(user_id)
    if goal:                                   # личный контекст: примеры/сценарии — про него
        out += (f"\nКОНТЕКСТ УЧЕНИКА: {goal}. Строй примеры, тексты и сценарии вокруг "
                f"его работы и цели — это закрепляет сильнее (self-reference).")
    return out

def stock_days(active_days=7):
    """Минимальный запас новых слов (в днях при DAILY_NEW_CAP) среди АКТИВНЫХ учеников
    (активный = есть повторения за последние active_days). None — активных нет.
    Сигнал владельцу «пора /fill», чтобы конвейер работал по данным, а не по памяти."""
    since = (datetime.date.today() - datetime.timedelta(days=active_days)).isoformat()
    with _conn() as c:
        uids = [r["user_id"] for r in c.execute(
            "SELECT DISTINCT user_id FROM reviews WHERE ts>=?", (since,)).fetchall()]
    vals = [new_remaining(u) / DAILY_NEW_CAP for u in uids]
    return round(min(vals), 1) if vals else None

# ---------- технический журнал (ошибки хэндлеров + фидбек) ----------

def log_tech(user_id, kind, summary, trace=None):
    """Записать техническую ошибку или фидбек тестера."""
    with _conn() as c:
        c.execute("INSERT INTO tech_errors (ts, user_id, kind, summary, trace) VALUES (?,?,?,?,?)",
                  (datetime.datetime.now().isoformat(), user_id, kind,
                   (summary or "")[:500], trace))

def recent_tech(kind=None, limit=10):
    """Последние записи журнала (свежие первыми); kind фильтрует error/feedback."""
    with _conn() as c:
        if kind:
            rows = c.execute("""SELECT * FROM tech_errors WHERE kind=?
                                ORDER BY id DESC LIMIT ?""", (kind, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM tech_errors ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
    return [dict(r) for r in rows]

def tech_count_24h():
    since = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM tech_errors WHERE ts>=?", (since,)).fetchone()[0]

# ---------- сервис ----------

def count_content():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM content").fetchone()[0]

def progress(user_id=DEFAULT_USER):
    with _conn() as c:
        rows = c.execute("""SELECT status, COUNT(*) n FROM state
                            WHERE user_id=? GROUP BY status""", (user_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}

def backup():
    """Снапшот всей базы. Дёргать в конце каждой сессии."""
    os.makedirs("backups", exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join("backups", f"english_os-{stamp}.db")
    shutil.copy(DB_PATH, dst)
    return dst
