# -*- coding: utf-8 -*-
"""Поднять бота для ЖИВОГО теста на КОПИИ базы (НЕ боевой english_os.db).
Драйв повторения пишет реальный SRS — поэтому всегда копия. См. память
live-test-bot-on-db-copy. Использование: PYTHONUTF8=1 python run_bot_testcopy.py
Затем драйвить Playwright'ом (live_check_derivation.py). Копию потом удалить."""
import os, shutil

SRC = "english_os.db"
COPY = ".live_check.db"
STATE = ".live_check_state.pickle"           # изолированный ctx (PicklePersistence)

shutil.copy(SRC, COPY)                        # свежий снимок боевой -> копия
os.environ["ENGLISH_OS_DB"] = COPY            # ВАЖНО: до import bot (db.DB_PATH берётся при импорте)

# Свежий ctx: бот по умолчанию пишет bot_state.pickle (ОБЩИЙ с боевым ботом!) — live-тест
# читал/мутировал персистентную сессию прода (активная колода и т.п.) и тащил состояние
# между прогонами. Изолируем: свой throwaway-pickle, удаляем при старте.
for _p in (STATE,):
    try: os.remove(_p)
    except FileNotFoundError: pass
os.environ["ENGLISH_OS_STATE"] = STATE        # тоже до import bot (_persistence читает env)
print(f"[testcopy] бот читает КОПИЮ {COPY} + изолированный ctx {STATE} "
      f"(боевые {SRC}/bot_state.pickle не тронутся)", flush=True)

import bot                                    # noqa: E402  (после установки env)
bot.main()
