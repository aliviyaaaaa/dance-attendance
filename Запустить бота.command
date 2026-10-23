#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ -x .venv/bin/python ]]; then
    BOT_PYTHON="$PWD/.venv/bin/python"
else
    print 'Не найден Python с библиотеками бота. Нужна установка по README.md.'
    read '?Нажмите Enter для закрытия.'
    exit 1
fi
print 'Запускаю Garage Аналитик. Оставьте это окно открытым.'
print 'Для остановки нажмите Control+C. Claude и Codex для работы не нужны.'
"$BOT_PYTHON" run_bot.py
BOT_EXIT=$?
if (( BOT_EXIT != 0 )); then
    print 'Бот завершился с ошибкой. Подробности выше и в logs/bot_console.log.'
    read '?Нажмите Enter для закрытия.'
fi
exit "$BOT_EXIT"
