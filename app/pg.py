"""Postgres — постоянное хранилище для дашборда студии.

Рабочее хранилище бота остаётся SQLite: локально, быстро, работает без интернета.
Postgres — второе хранилище, куда выкладывается снимок данных:
  * studio_groups      — группы студии (направление, педагоги, вместимость);
  * group_week_stats  — показатели группы за неделю со статусом (просела / растёт / …);
  * attendance_daily  — посещаемость по дням и группам;
  * studio_meta       — служебное: какие дни загружены, когда обновляли.

Из Postgres читает дашборд. Данные там переживают перезапуск бота и не зависят
от компьютера Алины.

Правило безопасности: если Postgres недоступен, бот продолжает работать на SQLite.
Ошибка публикации не должна ломать отчёты и не должна ронять бота.
"""

import logging
from datetime import datetime, timezone

from app import config, db

log = logging.getLogger(__name__)


def enabled() -> bool:
    """Задана ли строка подключения к Postgres."""
    return bool(config.DATABASE_URL)


def connect():
    """Соединение с Postgres. Бросает исключение, если не настроено или недоступно."""
    import psycopg

    if not enabled():
        raise RuntimeError("Postgres не настроен: нет DATABASE_URL в .env")
    return psycopg.connect(
        config.DATABASE_URL,
        connect_timeout=config.PG_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={config.PG_STATEMENT_TIMEOUT * 1000}",
    )


# ---------------------------------------------------------------- схема

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS studio_groups (
    group_id    INTEGER PRIMARY KEY,
    number      TEXT,
    style       TEXT,
    style_raw   TEXT,
    teacher1    TEXT,
    teacher2    TEXT,
    place_count INTEGER,
    updated_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS group_week_stats (
    week_start            DATE NOT NULL,
    group_id              INTEGER NOT NULL,
    sessions_planned      INTEGER NOT NULL DEFAULT 0,
    sessions_cancelled    INTEGER NOT NULL DEFAULT 0,
    sessions_empty        INTEGER NOT NULL DEFAULT 0,
    sessions_off_schedule INTEGER NOT NULL DEFAULT 0,
    sessions_held         INTEGER NOT NULL DEFAULT 0,
    visits_total          INTEGER NOT NULL DEFAULT 0,
    visits_subscription   INTEGER NOT NULL DEFAULT 0,
    visits_single         INTEGER NOT NULL DEFAULT 0,
    visits_other          INTEGER NOT NULL DEFAULT 0,
    visits_on_cancelled   INTEGER NOT NULL DEFAULT 0,
    booked                INTEGER NOT NULL DEFAULT 0,
    capacity_total        INTEGER NOT NULL DEFAULT 0,
    avg_per_session       DOUBLE PRECISION,
    fill_rate             DOUBLE PRECISION,
    baseline              DOUBLE PRECISION,
    change_pct            DOUBLE PRECISION,
    history_weeks         INTEGER NOT NULL DEFAULT 0,
    small                 BOOLEAN NOT NULL DEFAULT FALSE,
    small_avg             DOUBLE PRECISION,
    status                TEXT NOT NULL DEFAULT '',
    status_reason         TEXT,
    PRIMARY KEY (week_start, group_id)
);

CREATE TABLE IF NOT EXISTS attendance_daily (
    date                DATE NOT NULL,
    group_id            INTEGER NOT NULL,
    visits_total        INTEGER NOT NULL DEFAULT 0,
    visits_subscription INTEGER NOT NULL DEFAULT 0,
    visits_single       INTEGER NOT NULL DEFAULT 0,
    booked              INTEGER NOT NULL DEFAULT 0,
    sessions_planned    INTEGER NOT NULL DEFAULT 0,
    sessions_cancelled  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, group_id)
);

CREATE TABLE IF NOT EXISTS studio_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_week_stats_group ON group_week_stats (group_id, week_start DESC);
CREATE INDEX IF NOT EXISTS idx_daily_group ON attendance_daily (group_id, date DESC);

CREATE OR REPLACE VIEW dashboard_groups AS
SELECT w.week_start, w.group_id, g.style, g.teacher1, g.teacher2, g.place_count,
       w.avg_per_session, w.baseline, w.change_pct, w.status, w.status_reason,
       w.small, w.small_avg, w.visits_total, w.visits_subscription, w.visits_single,
       w.booked, w.sessions_held, w.sessions_cancelled, w.sessions_empty,
       w.capacity_total, w.fill_rate, w.history_weeks
FROM group_week_stats w
JOIN studio_groups g ON g.group_id = w.group_id;

CREATE OR REPLACE VIEW dashboard_daily AS
SELECT d.date, d.group_id, g.style, g.teacher1,
       d.visits_total, d.visits_subscription, d.visits_single, d.booked,
       d.sessions_planned, d.sessions_cancelled
FROM attendance_daily d
JOIN studio_groups g ON g.group_id = d.group_id;
"""


def ensure_schema(pg) -> None:
    """Создать таблицы и представления, если их ещё нет."""
    with pg.cursor() as cur:
        cur.execute(SCHEMA_SQL)


# ------------------------------------------------------- чтение из SQLite

GROUPS_SQL = """
SELECT id, number, style, style_raw, teacher1, teacher2, place_count, updated_at
FROM groups
"""

WEEK_STATS_SQL = """
SELECT week_start, group_id, sessions_planned, sessions_cancelled, sessions_empty,
       sessions_off_schedule, sessions_held, visits_total, visits_subscription,
       visits_single, visits_other, visits_on_cancelled, booked, capacity_total,
       avg_per_session, fill_rate, baseline, change_pct, history_weeks, small,
       small_avg, status, status_reason
FROM weekly_group_stats
"""

DAILY_VISITS_SQL = """
SELECT date, group_id,
       SUM(visits) AS visits_total,
       SUM(CASE WHEN service_entity = 'groupAccount' THEN visits ELSE 0 END) AS visits_subscription,
       SUM(CASE WHEN service_entity = 'groupSingle' THEN visits ELSE 0 END) AS visits_single
FROM visits_agg
GROUP BY date, group_id
"""

DAILY_BOOKINGS_SQL = """
SELECT s.date, s.group_id, SUM(b.booked) AS booked
FROM bookings b
JOIN sessions s ON s.schedule_id = b.schedule_id AND s.date = b.date
GROUP BY s.date, s.group_id
"""

DAILY_SESSIONS_SQL = """
SELECT date, group_id, COUNT(*) AS planned, SUM(cancelled) AS cancelled
FROM sessions
GROUP BY date, group_id
"""

LOADED_DAYS_SQL = "SELECT date, loaded_at FROM loaded_days"


def _daily_rows(conn) -> list[tuple]:
    """Собрать посещаемость по дням и группам из трёх таблиц SQLite."""
    visits = {(r["date"], r["group_id"]): (r["visits_total"], r["visits_subscription"], r["visits_single"])
              for r in conn.execute(DAILY_VISITS_SQL)}
    bookings = {(r["date"], r["group_id"]): r["booked"] for r in conn.execute(DAILY_BOOKINGS_SQL)}
    sessions = {(r["date"], r["group_id"]): (r["planned"], r["cancelled"]) for r in conn.execute(DAILY_SESSIONS_SQL)}

    rows = []
    for key in set(visits) | set(bookings) | set(sessions):
        v = visits.get(key, (0, 0, 0))
        s = sessions.get(key, (0, 0))
        rows.append((key[0], key[1], v[0], v[1], v[2], bookings.get(key, 0), s[0], s[1]))
    return sorted(rows)


# ------------------------------------------------------------- публикация

UPSERT_GROUPS = """
INSERT INTO studio_groups (group_id, number, style, style_raw, teacher1, teacher2,
                            place_count, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (group_id) DO UPDATE SET
    number = EXCLUDED.number, style = EXCLUDED.style, style_raw = EXCLUDED.style_raw,
    teacher1 = EXCLUDED.teacher1, teacher2 = EXCLUDED.teacher2,
    place_count = EXCLUDED.place_count, updated_at = EXCLUDED.updated_at
"""

UPSERT_WEEK_STATS = """
INSERT INTO group_week_stats (week_start, group_id, sessions_planned, sessions_cancelled,
    sessions_empty, sessions_off_schedule, sessions_held, visits_total, visits_subscription,
    visits_single, visits_other, visits_on_cancelled, booked, capacity_total,
    avg_per_session, fill_rate, baseline, change_pct, history_weeks, small, small_avg,
    status, status_reason)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (week_start, group_id) DO UPDATE SET
    sessions_planned = EXCLUDED.sessions_planned,
    sessions_cancelled = EXCLUDED.sessions_cancelled,
    sessions_empty = EXCLUDED.sessions_empty,
    sessions_off_schedule = EXCLUDED.sessions_off_schedule,
    sessions_held = EXCLUDED.sessions_held,
    visits_total = EXCLUDED.visits_total,
    visits_subscription = EXCLUDED.visits_subscription,
    visits_single = EXCLUDED.visits_single,
    visits_other = EXCLUDED.visits_other,
    visits_on_cancelled = EXCLUDED.visits_on_cancelled,
    booked = EXCLUDED.booked,
    capacity_total = EXCLUDED.capacity_total,
    avg_per_session = EXCLUDED.avg_per_session,
    fill_rate = EXCLUDED.fill_rate,
    baseline = EXCLUDED.baseline,
    change_pct = EXCLUDED.change_pct,
    history_weeks = EXCLUDED.history_weeks,
    small = EXCLUDED.small,
    small_avg = EXCLUDED.small_avg,
    status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason
"""

UPSERT_DAILY = """
INSERT INTO attendance_daily (date, group_id, visits_total, visits_subscription,
                               visits_single, booked, sessions_planned, sessions_cancelled)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (date, group_id) DO UPDATE SET
    visits_total = EXCLUDED.visits_total,
    visits_subscription = EXCLUDED.visits_subscription,
    visits_single = EXCLUDED.visits_single,
    booked = EXCLUDED.booked,
    sessions_planned = EXCLUDED.sessions_planned,
    sessions_cancelled = EXCLUDED.sessions_cancelled
"""

UPSERT_META = """
INSERT INTO studio_meta (key, value) VALUES (%s, %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""


def publish(sqlite_conn=None) -> dict:
    """Выложить снимок данных из SQLite в Postgres.

    Пересчёт идемпотентен: повторный вызов просто обновляет те же строки.
    Возвращает, сколько чего записано.
    """
    from app.attendance import recompute_all

    own_conn = sqlite_conn is None
    conn = sqlite_conn or db.connect()
    try:
        stats_rows = recompute_all(conn)       # недельные показатели со статусами
        groups = [tuple(r) for r in conn.execute(GROUPS_SQL)]
        week_stats = [tuple(r) for r in conn.execute(WEEK_STATS_SQL)]
        daily = _daily_rows(conn)
        loaded = [(r["date"], r["loaded_at"]) for r in conn.execute(LOADED_DAYS_SQL)]
        last_loaded = max((d for d, _ in loaded), default="")
        total_visits = conn.execute("SELECT COALESCE(SUM(visits), 0) FROM visits_agg").fetchone()[0]
    finally:
        if own_conn:
            conn.close()

    pg = connect()
    try:
        ensure_schema(pg)
        with pg.cursor() as cur:
            if groups:
                cur.executemany(UPSERT_GROUPS, groups)
            if week_stats:
                cur.executemany(UPSERT_WEEK_STATS, week_stats)
            if daily:
                cur.executemany(UPSERT_DAILY, daily)
            now = datetime.now(timezone.utc).isoformat()
            for key, value in [
                ("last_loaded_day", last_loaded),
                ("visits_total", str(total_visits)),
                ("published_at", now),
                ("weeks_loaded", str(len({d for d, _ in loaded}))),
            ]:
                cur.execute(UPSERT_META, (key, value))
        pg.commit()
    finally:
        pg.close()

    result = {
        "groups": len(groups),
        "week_stats": len(week_stats),
        "daily_rows": len(daily),
        "days": len({d for d, _ in loaded}),
        "last_loaded_day": last_loaded,
        "visits_total": total_visits,
        "recomputed_rows": stats_rows,
    }
    log.info("Postgres: опубликовано %s", result)
    return result


def check() -> dict:
    """Что сейчас лежит в Postgres — для проверки «данные на месте»."""
    pg = connect()
    try:
        with pg.cursor() as cur:
            out = {}
            for label, sql in [
                ("groups", "SELECT COUNT(*) FROM studio_groups"),
                ("week_stats", "SELECT COUNT(*) FROM group_week_stats"),
                ("daily_rows", "SELECT COUNT(*) FROM attendance_daily"),
                ("loaded_days", "SELECT COUNT(*) FROM studio_meta WHERE key = 'last_loaded_day'"),
            ]:
                cur.execute(sql)
                out[label] = cur.fetchone()[0]
            cur.execute("SELECT value FROM studio_meta WHERE key = 'last_loaded_day'")
            row = cur.fetchone()
            out["last_loaded_day"] = row[0] if row else None
            cur.execute("SELECT value FROM studio_meta WHERE key = 'published_at'")
            row = cur.fetchone()
            out["published_at"] = row[0] if row else None
            cur.execute("""SELECT week_start, COUNT(*) FROM group_week_stats
                           GROUP BY week_start ORDER BY week_start DESC LIMIT 5""")
            out["last_weeks"] = cur.fetchall()
            cur.execute("""SELECT COUNT(*) FROM group_week_stats WHERE status LIKE '%Просела'""")
            out["dropped_rows"] = cur.fetchone()[0]
    finally:
        pg.close()
    return out
