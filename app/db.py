"""База SQLite: один файл data/garage.sqlite.

Персональные данные клиентов сюда не попадают: посещения хранятся только
агрегатами (сколько человек пришло на занятие и по какому типу услуги).
Даты хранятся строками ГГГГ-ММ-ДД.
"""

import sqlite3

from app import config

DB_PATH = config.PROJECT_DIR / "data" / "garage.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS halls (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    place_count INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    number      TEXT,
    style_raw   TEXT,            -- название направления как в CRM
    style       TEXT,            -- то же, с обрезанными пробелами
    teacher1    TEXT,            -- «Имя Ф.»
    teacher2    TEXT,
    place_count INTEGER,         -- вместимость группы, если её заполнят в CRM
    status      INTEGER,
    updated_at  TEXT NOT NULL
);

-- Конкретные занятия: регулярный шаблон, развёрнутый на дату, или разовое занятие
CREATE TABLE IF NOT EXISTS sessions (
    schedule_id   INTEGER NOT NULL,
    date          TEXT NOT NULL,
    group_id      INTEGER NOT NULL,
    hall_id       INTEGER,
    minutes_begin INTEGER NOT NULL,
    minutes_end   INTEGER,
    regular       INTEGER NOT NULL,   -- 1 = по регулярному расписанию, 0 = разовое
    cancelled     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id, date)
);

-- Посещения, агрегированные по занятию и типу услуги (без clientId).
-- schedule_id = NULL → посещение не нашло занятие в расписании («вне расписания»).
CREATE TABLE IF NOT EXISTS visits_agg (
    date           TEXT NOT NULL,
    group_id       INTEGER NOT NULL,
    minutes_begin  INTEGER,           -- бывает пустым: посещение отмечено «на группу» без занятия
    hall_id        INTEGER,
    service_entity TEXT NOT NULL,     -- groupAccount / groupSingle / другое
    schedule_id    INTEGER,
    visits         INTEGER NOT NULL,
    PRIMARY KEY (date, group_id, minutes_begin, hall_id, service_entity)
);

CREATE TABLE IF NOT EXISTS bookings (
    schedule_id INTEGER NOT NULL,
    date        TEXT NOT NULL,
    booked      INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, date)
);

CREATE TABLE IF NOT EXISTS changes (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    schedule_id    INTEGER,
    entity         TEXT NOT NULL,     -- cancel / substitute
    source_teacher TEXT,
    teacher        TEXT
);

-- Показатели группы за неделю (считает app/attendance.py, пересчитываются целиком)
CREATE TABLE IF NOT EXISTS weekly_group_stats (
    week_start            TEXT NOT NULL,
    group_id              INTEGER NOT NULL,
    sessions_planned      INTEGER NOT NULL,
    sessions_cancelled    INTEGER NOT NULL,
    sessions_empty        INTEGER NOT NULL,
    sessions_off_schedule INTEGER NOT NULL,
    sessions_held         INTEGER NOT NULL,
    visits_total          INTEGER NOT NULL,
    visits_subscription   INTEGER NOT NULL,
    visits_single         INTEGER NOT NULL,
    visits_other          INTEGER NOT NULL,
    visits_on_cancelled   INTEGER NOT NULL,
    booked                INTEGER NOT NULL,
    capacity_total        INTEGER NOT NULL,
    avg_per_session       REAL,
    fill_rate             REAL,
    baseline              REAL,
    change_pct            REAL,
    history_weeks         INTEGER NOT NULL,
    status                TEXT NOT NULL,
    status_reason         TEXT,
    PRIMARY KEY (week_start, group_id)
);

-- Какие дни уже загружены и когда (для проверки полноты)
CREATE TABLE IF NOT EXISTS loaded_days (
    date      TEXT PRIMARY KEY,
    loaded_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
