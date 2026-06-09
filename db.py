"""
English OS — слой данных и SRS.
Канон: SQLite. Excel/JSON — одноразовый seed. Единственный писатель — бот.

Таблицы:
  content  — онтология (seed + подтверждённые добавления). Иммутабельна по смыслу.
  state    — прогресс SRS на пользователя и слово.
  pending  — слова, сгенерённые агентом, ждущие подтверждения человеком.
  reviews  — журнал повторений (для аналитики).
"""
import sqlite3, json, shutil, datetime, os, re
from contextlib import contextmanager

DB_PATH = os.environ.get("ENGLISH_OS_DB", "english_os.db")
DEFAULT_USER = 1                      # пока один пользователь; user_id заложен на вырост
DAILY_NEW_CAP = int(os.environ.get("NEW_CAP", "7"))   # дневная норма новых слов (настраивается NEW_CAP)
INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}   # Лейтнер, как в исходном файле

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
    word_id INTEGER NOT NULL, ts TEXT NOT NULL, remembered INTEGER NOT NULL
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
-- профиль/настройки пользователя (онбординг, уровень, цель)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, level TEXT, goal TEXT,
    onboarded INTEGER NOT NULL DEFAULT 0, reminder_hour INTEGER, created_at TEXT
);
-- описания слоёв (reference): этимология корней и пояснения фреймов
CREATE TABLE IF NOT EXISTS root_ref  (root TEXT PRIMARY KEY, idea TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS frame_ref (name TEXT PRIMARY KEY, ru TEXT, when_use TEXT, example TEXT);
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
    ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if ucols and "reminder_hour" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN reminder_hour INTEGER")

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
    """Залить описания слоёв из reference: корни (этимология) и фреймы (пояснения)."""
    ref = data.get("reference", {})
    for r in ref.get("1. Roots", []):
        root = (r.get("Корень") or "").strip()
        if root:
            c.execute("INSERT OR REPLACE INTO root_ref (root, idea, origin) VALUES (?,?,?)",
                      (root, r.get("Идея"), r.get("Происхождение")))
    for f in ref.get("7. Thinking Frames", []):
        name = (f.get("Шаблон") or "").strip()
        if name:
            c.execute("""INSERT OR REPLACE INTO frame_ref (name, ru, when_use, example)
                         VALUES (?,?,?,?)""",
                      (name, f.get("Перевод"), f.get("Когда использовать"), f.get("Пример в речи")))

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
    """Кому слать напоминание в этот час."""
    with _conn() as c:
        return [r["user_id"] for r in c.execute(
            "SELECT user_id FROM users WHERE reminder_hour=?", (hour,)).fetchall()]

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

def adapt_band(user_id, window=12):
    """Тихо двигаем полосу по последним повторениям: высокий успех -> сложнее, низкий -> проще.
    Пользователю уровень НЕ показываем. Возвращает новую полосу, если сдвинули, иначе None."""
    with _conn() as c:
        rows = c.execute("SELECT remembered FROM reviews WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, window)).fetchall()
    if len(rows) < window:
        return None
    rate = sum(r["remembered"] for r in rows) / len(rows)
    cur = get_band(user_id)
    i = _BANDS.index(cur) if cur in _BANDS else 0
    if rate >= 0.85 and i < len(_BANDS) - 1:
        set_band(user_id, _BANDS[i + 1]); return _BANDS[i + 1]
    if rate < 0.5 and i > 0:
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
    """Слова из базы, встретившиеся в реплике — для грунтовки диалога их коллокациями/сетью."""
    seen, toks = set(), []
    for t in re.findall(r"[a-zA-Z]+", (text or "").lower()):
        if t not in seen:
            seen.add(t); toks.append(t)
    out = []
    if toks:
        with _conn() as c:
            for t in toks:
                r = c.execute("SELECT * FROM content WHERE LOWER(word)=? LIMIT 1", (t,)).fetchone()
                if r:
                    out.append(_row_to_word(r))
                    if len(out) >= limit:
                        break
    return out

def root_info(root):
    """Этимология корня из reference (None, если нет)."""
    if not root or root == "—":
        return None
    with _conn() as c:
        r = c.execute("SELECT root, idea, origin FROM root_ref WHERE root=?", (root,)).fetchone()
    return dict(r) if r else None

def frame_info(name):
    """Пояснение мыслительного фрейма из reference (None, если нет)."""
    if not name:
        return None
    with _conn() as c:
        r = c.execute("SELECT name, ru, when_use, example FROM frame_ref WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None

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

# ---------- темы: навигация по DNA-идеям и сценариям ----------
def idea_list():
    with _conn() as c:
        return [(r["dna_idea"], r["n"]) for r in c.execute(
            """SELECT dna_idea, COUNT(*) n FROM content
               WHERE dna_idea IS NOT NULL AND dna_idea<>'' GROUP BY dna_idea ORDER BY n DESC""").fetchall()]

def scenario_list():
    with _conn() as c:
        return [(r["scenario"], r["n"]) for r in c.execute(
            """SELECT scenario, COUNT(*) n FROM content
               WHERE scenario IS NOT NULL AND scenario<>'' GROUP BY scenario ORDER BY n DESC""").fetchall()]

def theme_words(axis, value, user_id=DEFAULT_USER, n=5):
    """Слова темы (axis 'idea'|'scn'): сначала ещё не введённые (new), потом любые."""
    col = "dna_idea" if axis == "idea" else "scenario"
    with _conn() as c:
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

def mature_words(user_id=DEFAULT_USER, limit=25):
    """Хорошо усвоенные слова (known или box>=3) — известная база текста (правило 98%)."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND (s.status='known' OR s.box>=3)
                            ORDER BY s.box DESC, c.priority DESC LIMIT ?""",
                         (user_id, limit)).fetchall()
    return [_row_to_word(r) for r in rows]

def target_words(user_id=DEFAULT_USER, limit=4):
    """Незрелые активные слова (learning/forgot, box<3) — это «новые ~2%» для текста ввода."""
    with _conn() as c:
        rows = c.execute("""SELECT c.* FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status IN ('learning','forgot') AND s.box<3
                            ORDER BY c.priority DESC, c.word_id ASC LIMIT ?""",
                         (user_id, limit)).fetchall()
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
    """Ввести в оборot до n самых ценных new-слов, но не больше дневного лимита."""
    remaining = max(0, DAILY_NEW_CAP - promoted_today(user_id))
    n = min(n, remaining)
    if n <= 0:
        return []
    pool = new_pool(user_id, limit=n, band=get_band(user_id))   # адаптивно: по полосе комфорта
    today = _today()
    with _conn() as c:
        for w in pool:
            c.execute("""UPDATE state SET status='learning', box=1,
                         last_review=?, next_review=?, promoted_at=?
                         WHERE user_id=? AND word_id=?""",
                      (today, today, today, user_id, w["word_id"]))
    return pool

def start_learning(ids, user_id=DEFAULT_USER):
    """Ввести конкретные слова в оборот по явной просьбе «учим X» (минуя дневной лимит)."""
    today = _today()
    with _conn() as c:
        for wid in ids:
            c.execute("""UPDATE state SET status='learning', box=1,
                         last_review=?, next_review=?, promoted_at=?
                         WHERE user_id=? AND word_id=? AND status='new'""",
                      (today, today, today, user_id, wid))

def due_today(user_id=DEFAULT_USER):
    """Слова к повторению: learning/forgot с next_review<=сегодня. Самые просроченные первыми."""
    today = _today()
    with _conn() as c:
        rows = c.execute("""SELECT c.*, s.status AS s_status, s.box AS s_box,
                                   s.next_review AS s_next
                            FROM state s JOIN content c USING(word_id)
                            WHERE s.user_id=? AND s.status IN ('learning','forgot')
                              AND s.next_review IS NOT NULL AND s.next_review<=?
                            ORDER BY s.next_review ASC, c.priority DESC""",
                         (user_id, today)).fetchall()
    words = [_row_to_word(r) for r in rows]
    st = {r["word_id"]: {"status": r["s_status"], "box": r["s_box"]} for r in rows}
    return words, st

def review(word_id, remembered, user_id=DEFAULT_USER, variant=None, ms=None):
    """Обновить SRS после повторения. remembered: True/False.
    variant ('layered'/'flat') и ms (время ответа) — инструментовка для будущего A/B."""
    today = _today()
    with _conn() as c:
        r = c.execute("SELECT box,status FROM state WHERE user_id=? AND word_id=?",
                      (user_id, word_id)).fetchone()
        if r is None:
            raise ValueError(f"нет state для word_id={word_id}")
        box = r["box"] or 1
        if remembered:
            box = min(box + 1, 5)
            status = "known" if box == 5 else "learning"
        else:
            box = 1
            status = "forgot"
        nxt = (datetime.date.today() + datetime.timedelta(days=INTERVALS[box])).isoformat()
        c.execute("""UPDATE state SET box=?, status=?, last_review=?, next_review=?
                     WHERE user_id=? AND word_id=?""",
                  (box, status, today, nxt, user_id, word_id))
        c.execute("""INSERT INTO reviews (user_id, word_id, ts, remembered, variant, ms)
                     VALUES (?,?,?,?,?,?)""",
                  (user_id, word_id, datetime.datetime.now().isoformat(),
                   int(remembered), variant, ms))
    return {"word_id": word_id, "box": box, "status": status, "next_review": nxt}

def variant_stats(user_id=DEFAULT_USER):
    """Сводка по A/B: точность и среднее время ответа по вариантам карточки.
    Это шина для будущей проверки «слои vs плоско», а не само доказательство."""
    with _conn() as c:
        rows = c.execute("""SELECT variant,
                                   COUNT(*) n,
                                   SUM(remembered) ok,
                                   AVG(ms) avg_ms
                            FROM reviews
                            WHERE user_id=? AND variant IS NOT NULL
                            GROUP BY variant""", (user_id,)).fetchall()
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

def apply_session_summary(data, user_id=DEFAULT_USER):
    """Применить машинный итог сессии: reviewed -> SRS (review), add -> очередь pending.
    Мусор (неизвестное слово / нет state) пропускается, не падает.
    Возвращает счётчики {'ok','fail','added','skipped'}."""
    ok = fail = added = skipped = 0
    for item in (data.get("reviewed") or []):
        wid = find_word_id(item.get("word", ""))
        if wid is None:
            skipped += 1
            continue
        try:
            review(wid, bool(item.get("ok")), user_id)
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

def cando_progress(user_id=DEFAULT_USER, ready_ratio=0.6):
    """Прогресс по can-do: доля освоенных (known/box>=3) слов в сценарии каждого пункта.
    ready = есть слова, доля >= ready_ratio и освоено хотя бы 2. Прокси, не сертификация."""
    out = []
    with _conn() as c:
        for cd in CANDO:
            row = c.execute("""SELECT COUNT(*) total,
                       SUM(CASE WHEN s.status='known' OR s.box>=3 THEN 1 ELSE 0 END) mastered
                     FROM content cc JOIN state s USING(word_id)
                     WHERE s.user_id=? AND cc.scenario=?""",
                    (user_id, cd["scenario"])).fetchone()
            total = row["total"] or 0
            mastered = row["mastered"] or 0
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
    return ("ПРОФИЛЬ УЧЕНИКА (факты из базы — опирайся на них, прошлое не выдумывай): "
            + "; ".join(parts) + ".")

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
