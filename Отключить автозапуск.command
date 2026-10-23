#!/bin/zsh
AGENT_DOMAIN="gui/$(id -u)"
AGENT_LABEL=local.garage.attendance
if launchctl print "$AGENT_DOMAIN/$AGENT_LABEL" >/dev/null 2>&1; then
    launchctl bootout "$AGENT_DOMAIN/$AGENT_LABEL" || exit 1
fi
rm -f "$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
print 'Автозапуск отключён. Бот можно запускать вручную.'
read '?Нажмите Enter для закрытия.'
