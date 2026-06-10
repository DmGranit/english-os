@echo off
rem ============================================================
rem  English OS - запуск бота. Двойной клик - и работает.
rem  Ключи читаются из .env (лежит рядом, в git не попадает).
rem  При падении бот перезапускается сам через 5 секунд.
rem  Остановить: закрыть окно или Ctrl+C.
rem ============================================================
cd /d "%~dp0"
set PYTHONUTF8=1
chcp 65001 >nul

if not exist ".env" (
    echo [ОШИБКА] Нет файла .env рядом с батником. Нужны TELEGRAM_TOKEN и LLM_API_KEY.
    pause
    exit /b 1
)

for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"

:loop
echo [%date% %time%] Запускаю English OS bot...
python -c "import logging; logging.basicConfig(level=logging.INFO, format='%%(asctime)s %%(name)s: %%(message)s'); logging.getLogger('httpx').setLevel(logging.WARNING); import bot; bot.main()"
echo [%date% %time%] Бот остановился (код %errorlevel%). Перезапуск через 5 сек... [Ctrl+C - выйти]
timeout /t 5 >nul
goto loop
