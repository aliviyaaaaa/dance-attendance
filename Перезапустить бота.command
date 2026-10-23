#!/bin/zsh
if launchctl kickstart -k "gui/$(id -u)/local.garage.attendance"; then
    print 'Бот перезапускается с обновлённым кодом. Через несколько секунд запросите отчёт заново в Telegram.'
else
    print 'Перезапуск не удался. Сохраните текст ошибки выше.'
fi
read '?Нажмите Enter для закрытия.'
