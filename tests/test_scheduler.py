"""Расписание агента: когда что запускается само. Запуск: .venv/bin/pytest"""

from datetime import datetime

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
