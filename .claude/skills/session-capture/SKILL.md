---
name: session-capture-engbot
description: >-
  Скил фиксации рабочей сессии English OS Bot для кросс-сессионной непрерывности.
  Используй, когда оператор просит «зафиксируй сессию» / «session capture» / «сохрани
  контекст» (обычно в конце сессии). Режимы: «зафиксируй» = light (git-манифест .md +
  JSON-капсула); «зафиксируй полностью» = full (FULL.md разбор + JSON + опц. Internal-
  transfer для стерео/resume). Каркас из git (faithful), синтез из контекста. Non-canon.
  Проект: english_os_bot · Захваты: C:\CLAUDE_CODE_PROJECTS\English_OS\SESSION_CAPTURES\
---

# session-capture-engbot (english-os-bot)
# Version: v0.1 · Status: Draft · Class: meta/ops
# Companion: scripts/git_manifest.py
# Repo: C:\CLAUDE_CODE_PROJECTS\English_OS\english_os_bot
# Output: C:\CLAUDE_CODE_PROJECTS\English_OS\SESSION_CAPTURES\

## 1. Роль
Фиксация сессии для непрерывности. Источники: (а) контекст разговора — синтез; (б) git —
каркас «что сделано», faithful by construction. Решений не принимаешь, статусов не ставишь.

## 2. Режим
- «зафиксируй сессию» / «сохрани контекст» = light (дефолт).
- «зафиксируй полностью» = full.
- В отчёте подскажи: «N коммитов / K артефактов — похоже на узловую, скажите »полностью«».
  Решает человек.

## 3. Источники (детерминированный каркас)
1. Время: shell `date '+%Y-%m-%d_%H%M'` для имён; человекочитаемое `date` в тело. НЕ выдумывай.
2. Манифест: `python .claude/skills/session-capture/scripts/git_manifest.py --repo . --timestamp "$TS"`
   → коммиты+файлы С ПРОШЛОЙ КАПСУЛЫ. Каркас «что сделано» ОТСЮДА, не из памяти.

## 4. Пути (проектные)
- Репо бота: `C:\CLAUDE_CODE_PROJECTS\English_OS\english_os_bot`
- Session captures: `C:\CLAUDE_CODE_PROJECTS\English_OS\SESSION_CAPTURES\` (вне репо, не коммитится)
- Стерео-ящик: `C:\stereo\english_os\` (W2R.md — executor→reviewer)

## 5. Light — 2 файла в SESSION_CAPTURES/
- `Session_Capture_<ts>.md`: git-манифест + 3–5 строк синтеза (о чём сессия, ключевое
  решение, указатель состояния) + открытые вопросы + лоссовая пометка («длинная сессия —
  раннее могло быть сжато компакцией; истина = git + артефакты»).
- `Session_Capture_<ts>.json`: `{date, mode, manifest:{commits,files}, summary,
  decisions[], state, open_questions[], lossy_note}`.

## 6. Full — 2–3 файла
- `Session_Capture_<ts>_FULL.md` — полный разбор (нити, решения с обоснованием, состояние,
  открытые вопросы). Заменяет тонкий .md.
- `Session_Capture_<ts>.json` — та же капсула.
- (опц.) `Context_Transfer_<ts>_Internal.md` — якоря состояния, next step, индекс файлов.

## 7. Достоверность (жёстко)
- Каркас — из git-манифеста §3, не из пересказа.
- Синтез — с метками FACT (проверено) / ASSESS (суждение).
- НЕ выдумывай решений. Тупики/ошибки — честно, без приукрашивания.

## 8. Память — курируемо
- Light: память НЕ трогай.
- Full: только при долговечном неочевидном факте — предложи запись; обнови ОДИН
  project-state файл; не плоди «current state»-дубли.

## 9. Самопроверка перед записью
1. Компакция: длинная сессия → недозафиксировал раннее? → пометка.
2. Тупики/ошибки честно? 3. Заземление в git-манифест? 4. FACT/ASSESS? 5. Решения не выдуманы?

## 10. Поток
`режим → date($TS) → git_manifest → [empty-delta guard] → синтез(FACT/ASSESS) →
самопроверка → записать в SESSION_CAPTURES/ → [full: предложить память] →
отчёт владельцу`.

Captures вне репо → НЕ коммитить (git add не нужен).
Empty-delta guard: `commits=0` И `uncommitted=0` с прошлой капсулы → НЕ плоди файлы,
сообщи «ничего нового — капсула не нужна», заверши.

# ChangeLog
# v0.1 | 2026-06-13 | Адаптация универсального двигателя под english-os-bot (governance убрана; captures вне репо).
# v0.2 | 2026-06-13 | Переименован в session-capture-engbot — устранение коллизии с Navigator (одинаковое name: session-capture).
