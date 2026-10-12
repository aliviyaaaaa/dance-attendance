"""Белый список бота. Запуск: .venv/bin/pytest"""

from types import SimpleNamespace

from app import bot, config


def message_from(user_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id) if user_id else None)


def test_only_whitelisted_users_allowed(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", {111})
    assert bot.is_allowed(message_from(111))
    assert not bot.is_allowed(message_from(222))
    assert not bot.is_allowed(message_from(None))      # сообщения без отправителя (каналы)


def test_empty_whitelist_allows_nobody(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", set())
    assert not bot.is_allowed(message_from(111))


def test_buttons_respect_whitelist(monkeypatch):
    # нажатие кнопки (CallbackQuery) проверяется той же функцией, что и сообщения
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_IDS", {111})
    assert bot.is_allowed(SimpleNamespace(from_user=SimpleNamespace(id=111)))
    assert not bot.is_allowed(SimpleNamespace(from_user=SimpleNamespace(id=222)))


def test_period_buttons_fit_telegram_limit():
    # callback_data у Telegram — не длиннее 64 байт
    for row in bot.periods_keyboard().inline_keyboard + bot.weeks_keyboard().inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64
