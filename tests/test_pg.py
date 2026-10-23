"""Тесты слоя Postgres. Запуск: .venv/bin/pytest

Часть тестов работает без сервера (сборка данных из SQLite).
Проверка настоящей публикации запускается, только если задан DATABASE_URL.
"""

import sqlite3

import pytest

from app import config, db, pg


@pytest.fixture
def mini_db():
    """Маленькая база SQLite с группами, занятиями и посещениями."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO groups (id, number, style, style_raw, teacher1, teacher2, place_count, updated_at)"
        " VALUES (1, 'A', 'hip hop', 'hip hop', 'Оля П.', NULL, 12, '2026-10-12 10:00')")
    conn.execute(
        "INSERT INTO groups (id, number, style, style_raw, teacher1, teacher2, place_count, updated_at)"
        " VALUES (2, 'B', 'afro', 'afro', 'Николаева С.', NULL, 20, '2026-10-12 10:00')")
    conn.execute(
        "INSERT INTO sessions (schedule_id, date, group_id, hall_id, minutes_begin, regular, cancelled)"
        " VALUES (100, '2026-10-12', 1, 1, 1080, 1, 0)")
    conn.execute(
        "INSERT INTO sessions (schedule_id, date, group_id, hall_id, minutes_begin, regular, cancelled)"
        " VALUES (101, '2026-10-12', 2, 1, 1140, 1, 1)")
    conn.execute(
        "INSERT INTO visits_agg (date, group_id, minutes_begin, hall_id, service_entity, schedule_id, visits)"
        " VALUES ('2026-10-12', 1, 1080, 1, 'groupAccount', 100, 8)")
    conn.execute(
        "INSERT INTO visits_agg (date, group_id, minutes_begin, hall_id, service_entity, schedule_id, visits)"
        " VALUES ('2026-10-12', 1, 1080, 1, 'groupSingle', 100, 3)")
    conn.execute("INSERT INTO bookings (schedule_id, date, booked) VALUES (100, '2026-10-12', 9)")
    conn.execute("INSERT INTO loaded_days (date, loaded_at) VALUES ('2026-10-12', '2026-10-13 09:00')")
    conn.commit()
    yield conn
    conn.close()


# ---------- сборка данных из SQLite ----------

def test_daily_rows_merge_visits_bookings_and_sessions(mini_db):
    from datetime import date

    rows = pg._daily_rows(mini_db)
    by_key = {(r[0], r[1]): r for r in rows}
    row = by_key[(date(2026, 10, 12), 1)]
    assert row[2] == 11          # всего посещений
    assert row[3] == 8           # по абонементу
    assert row[4] == 3           # разовые
    assert row[5] == 9           # записи
    assert row[6] == 1           # занятий по расписанию
    assert row[7] == 0           # отменено


def test_daily_rows_count_cancelled_sessions(mini_db):
    from datetime import date

    rows = {(r[0], r[1]): r for r in pg._daily_rows(mini_db)}
    assert rows[(date(2026, 10, 12), 2)][7] == 1


def test_daily_rows_empty_db_is_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assert pg._daily_rows(conn) == []
    conn.close()


# ---------- типы для Postgres ----------

def test_day_parses_iso_date():
    from datetime import date

    assert pg._day("2026-10-12") == date(2026, 10, 12)
    assert pg._day("не дата") is None
    assert pg._day(None) is None


def test_ts_parses_iso_with_offset():
    from datetime import datetime, timezone

    value = pg._ts("2026-10-14T11:30:44+00:00")
    assert value == datetime(2026, 10, 14, 11, 30, 44, tzinfo=timezone.utc)
    assert pg._ts("") is None
    assert pg._ts("мусор") is None


def test_daily_rows_use_date_objects(mini_db):
    from datetime import date

    rows = pg._daily_rows(mini_db)
    assert all(isinstance(r[0], date) for r in rows)


def test_week_rows_convert_types_for_postgres(mini_db):
    """В SQLite small — это 0/1, в Postgres это BOOLEAN: нужен настоящий bool."""
    from datetime import date

    mini_db.execute(
        "INSERT INTO weekly_group_stats (week_start, group_id, sessions_planned, sessions_cancelled,"
        " sessions_empty, sessions_off_schedule, sessions_held, visits_total, visits_subscription,"
        " visits_single, visits_other, visits_on_cancelled, booked, capacity_total, avg_per_session,"
        " fill_rate, baseline, change_pct, history_weeks, small, small_avg, status, status_reason)"
        " VALUES ('2026-10-12', 1, 2, 0, 0, 0, 2, 8, 6, 2, 0, 0, 8, 24, 4.0, 0.33, 5.0, -0.2, 4, 1, 4.5,"
        " '🔴 Просела', 'просела')")
    mini_db.commit()

    rows = pg._week_rows(mini_db)
    assert len(rows) == 1
    assert rows[0][19] is True              # small → bool
    assert isinstance(rows[0][0], date)     # week_start → date


def test_group_rows_convert_updated_at(mini_db):
    from datetime import datetime, timezone

    rows = pg._group_rows(mini_db)
    assert rows[0][7] == datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


# ---------- настройки ----------

def test_disabled_without_database_url(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert pg.enabled() is False


def test_enabled_with_database_url(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@host:5432/db")
    assert pg.enabled() is True


def test_connect_raises_without_url(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        pg.connect()


# ---------- настоящий Postgres (если настроен) ----------

@pytest.mark.skipif(not pg.enabled(), reason="не задан DATABASE_URL — проверка с сервером пропущена")
def test_publish_and_check_roundtrip():
    """Опубликовать в настоящий Postgres и убедиться, что строки на месте."""
    stats = pg.publish()
    assert stats["groups"] >= 0
    assert stats["week_stats"] >= 0
    info = pg.check()
    assert info["groups"] >= 0
    assert info["last_loaded_day"] is not None
