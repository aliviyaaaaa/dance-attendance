# Настройка компьютера

Дата установки: 12 октября 2026

## Система

- macOS 14.5 (Sonoma), сборка 23F79
- Процессор: Apple M2 (Apple Silicon, arm64)

## Установленные инструменты

| Инструмент  | Версия                | Откуда                                       |
|-------------|-----------------------|----------------------------------------------|
| Claude Code | 2.1.296               | официальный установщик (claude.ai/install.sh) |
| Git         | 2.39.5 (Apple Git-154) | уже был (Apple Command Line Tools)          |
| Node.js     | v24.21.0 (LTS)        | официальная сборка с nodejs.org              |
| npm         | 11.19.0               | вместе с Node.js                             |
| Python      | 3.13.7                | уже был (python.org)                         |
| Homebrew    | 7.0.9                 | официальный репозиторий, в /opt/homebrew     |

## Заметки

- Для macOS 14 у Homebrew уровень поддержки Tier 3: готовых сборок нет, поэтому
  Node.js поставлен не через Homebrew, а напрямую с nodejs.org (в ~/.local/node).
- Пути добавлены в ~/.zshrc (~/.local/bin) и ~/.zprofile (Homebrew).
