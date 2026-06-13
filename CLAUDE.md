# English OS Bot — CLAUDE.md

## Стерео-ящик (оперативный обмен executor↔reviewer)
- **Ящик проекта:** `C:\stereo\english_os\`
- `R2W.md` — пишет ТОЛЬКО reviewer → executor
- `W2R.md` — пишет ТОЛЬКО executor → reviewer
- Пинг идёт через владельца. Файлы вне git, вне канона.

## Session captures
- Папка: `C:\CLAUDE_CODE_PROJECTS\English_OS\SESSION_CAPTURES\` (вне репо, не коммитится)
- Скил: `session-capture-engbot` · папка `.claude/skills/session-capture/` (project-scoped)
- Маркер: `.claude/last_capture_ref` (в .gitignore — HEAD прошлого захвата, не коммитится)
- Скрипт манифеста: `PYTHONUTF8=1 python .claude/skills/session-capture/scripts/git_manifest.py --repo . --marker-file .claude/last_capture_ref`
- ⚠️ Платформа не авто-загружает project-scoped скилы — выполнять вручную по SKILL.md

## SSOT
- Код и план: `origin/main` (GitHub `DmGranit/english-os`)
- База: `english_os.db` (SQLite, в `.gitignore` — только на диске)
- Бэкапы базы: `backups/` (в `.gitignore`)

## DO-NOT
- Не коммить `english_os.db`, `.env`, `backups/`
- Не мержить в main без зелёных тестов (`python -m pytest -q`)
- Не трогать боевую БД без бэкапа (`db.backup()` перед записью)
