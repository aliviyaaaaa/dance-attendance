#!/bin/zsh
cd -- "${0:A:h}" || exit 1
AGENT_LABEL=local.garage.attendance
AGENT_DOMAIN="gui/$(id -u)"
AGENT_PATH="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" || exit 1
cp "$AGENT_LABEL.plist" "$AGENT_PATH" || exit 1
chmod 600 "$AGENT_PATH"
if launchctl print "$AGENT_DOMAIN/$AGENT_LABEL" >/dev/null 2>&1; then
    print 'Автозапуск уже включён.'
else
    # Только Python с lock-файлом этой копии проекта.
    for BOT_PID in ${(f)$(lsof -t -a -c Python "$PWD/logs/bot.lock" 2>/dev/null)}; do
        kill -TERM "$BOT_PID" || exit 1
    done
    sleep 2
    if ! launchctl bootstrap "$AGENT_DOMAIN" "$AGENT_PATH"; then
        print 'Не удалось включить автозапуск. Сохраните текст ошибки выше.'
        read '?Нажмите Enter для закрытия.'
        exit 1
    fi
    print 'Автозапуск включён. Бот работает в фоне; Терминал можно закрыть.'
fi
print 'Проверьте /start в Telegram. Журнал: logs/bot_console.log.'
read '?Нажмите Enter для закрытия.'
