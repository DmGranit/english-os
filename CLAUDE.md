# English OS Bot — CLAUDE.md

## Стерео-ящик (оперативный обмен executor↔reviewer)
- **Ящик проекта:** `C:\stereo\english_os\`
- `R2W.md` — пишет ТОЛЬКО reviewer → executor
- `W2R.md` — пишет ТОЛЬКО executor → reviewer
- Пинг идёт через владельца. Файлы вне git, вне канона.
- **Старт сессии:** читай `R2W.md` — там указания от ревьюера.

## Session captures
- Папка: `C:\CLAUDE_CODE_PROJECTS\English_OS\SESSION_CAPTURES\` (вне репо, не коммитится)
- Движок: единый `session-capture` (SSOT `C:\claude-tooling\skills\session-capture\`), подключён
  **junction'ом** `.claude/skills/session-capture/` → SSOT (ноль дрейфа). Форк `session-capture-engbot` упразднён.
- Проектные параметры: `.claude/session-capture-profile.md` (project_name, captures_dir, marker_file и т.д.)
- Маркер: `.claude/last_capture_ref` — HEAD прошлого захвата (since-anchor; движок читает + пишет write-back)
- Запуск: командой `/session-capture` → меню Light / Full (NL-триггера «зафиксируй» больше нет)
- Весь `.claude/` в `.gitignore` (локальная оснастка, не коммитится)

## SSOT
- Код и план: `origin/main` (GitHub `DmGranit/english-os`)
- База: `english_os.db` (SQLite, в `.gitignore` — только на диске)
- Бэкапы базы: `backups/` (в `.gitignore`)

## DO-NOT
- Не коммить `english_os.db`, `.env`, `backups/`
- Не мержить в main без зелёных тестов (`python -m pytest -q`)
- Не трогать боевую БД без бэкапа (`db.backup()` перед записью)
