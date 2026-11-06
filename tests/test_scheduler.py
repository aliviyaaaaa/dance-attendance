"""Расписание агента: когда что запускается само. Запуск: .venv/bin/pytest"""

import asyncio
from datetime import date, datetime

from app import config, scheduler
from app.scheduler import next_run, split_text


def test_next_run_today():
    now = datetime(2026, 10, 28, 13, 41, tzinfo=config.TZ)
    assert next_run(now, 18, 30) == datetime(2026, 10, 28, 18, 30, tzinfo=config.TZ)


def test_next_run_tomorrow():
    # время уже прошло сегодня → ближайший запуск завтра
    now = datetime(2026, 10, 28, 13, 41, tzinfo=config.TZ)
    assert next_run(now, 6, 0) == datetime(2026, 10, 29, 6, 0, tzinfo=config.TZ)


def test_next_run_next_weekday():
    # 28.10.2026 — среда; отчёт по понедельникам → 2 ноября
    now = datetime(2026, 10, 28, 13, 41, tzinfo=config.TZ)
    assert next_run(now, 9, 0, weekday=1) == datetime(2026, 11, 2, 9, 0, tzinfo=config.TZ)


def test_next_run_same_weekday_before_time():
    # понедельник, до 9:00 → сегодня же
    now = datetime(2026, 10, 26, 8, 0, tzinfo=config.TZ)
    assert next_run(now, 9, 0, weekday=1) == datetime(2026, 10, 26, 9, 0, tzinfo=config.TZ)


def test_next_run_same_weekday_after_time():
    # понедельник, уже после 9:00 → следующий понедельник
    now = datetime(2026, 10, 26, 10, 0, tzinfo=config.TZ)
    assert next_run(now, 9, 0, weekday=1) == datetime(2026, 11, 2, 9, 0, tzinfo=config.TZ)


def test_split_text_respects_telegram_limit():
    text = "\n".join(["строка " * 50] * 20)
    chunks = split_text(text)
    assert chunks
    assert all(len(c) <= scheduler.TELEGRAM_LIMIT + 1 for c in chunks)
    assert "".join(chunks).strip() == text.strip()


def test_split_text_short_message_stays_whole():
    assert split_text("коротко") == ["коротко\n"]


def test_state_survives_restart(tmp_path, monkeypatch):
    # после перезапуска бота нельзя слать отчёт второй раз — состояние лежит в файле
    path = tmp_path / "scheduler_state.json"
    monkeypatch.setattr(scheduler, "STATE_PATH", path)
    assert scheduler.read_state() == {}
    scheduler.write_state(last_report_week="2026-10-19")
    assert scheduler.read_state()["last_report_week"] == "2026-10-19"


def test_status_lines_report_schedule(monkeypatch):
    monkeypatch.setattr(scheduler, "last_loaded_at",
                        lambda: datetime(2026, 10, 28, 6, 0, tzinfo=config.TZ))
    monkeypatch.setattr(scheduler, "pg_status", lambda: "подключена")
    lines = scheduler.status_lines()
    assert len(lines) == 5
    assert any("автосинхронизация" in line.lower() for line in lines)
    assert any("отчёт" in line.lower() for line in lines)


def test_watchdog_thresholds_are_ordered():
    # сначала сбрасываем залипшие соединения, и только потом перезапускаем процесс
    assert 0 < scheduler.WATCHDOG_RESET_AFTER < scheduler.WATCHDOG_EXIT_AFTER
    assert scheduler.WATCHDOG_INTERVAL_SEC <= 120


def test_reachability_check_without_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert not asyncio.run(scheduler.telegram_reachable())


def test_pending_report_is_found_when_it_was_missed(tmp_path, monkeypatch):
    # компьютер спал в понедельник утром — отчёт за прошлую неделю не ушёл
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    monday = date(2026, 11, 9)
    monkeypatch.setattr(scheduler, "last_full_week", lambda today=None: monday)
    assert scheduler.pending_report_week(date(2026, 11, 10)) == monday


def test_pending_report_is_not_repeated(tmp_path, monkeypatch):
    # отчёт за эту неделю уже отправлен — второй раз не отправляем
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    monday = date(2026, 11, 9)
    monkeypatch.setattr(scheduler, "last_full_week", lambda today=None: monday)
    scheduler.write_state(last_report_week=monday.isoformat())
    assert scheduler.pending_report_week(date(2026, 11, 10)) is None


def test_pending_report_ignores_very_old_week(tmp_path, monkeypatch):
    # отчёт за две недели назад уже неактуален — не мучаем сообщениями
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    monday = date(2026, 10, 19)
    monkeypatch.setattr(scheduler, "last_full_week", lambda today=None: monday)
    assert scheduler.pending_report_week(date(2026, 11, 10)) is None
