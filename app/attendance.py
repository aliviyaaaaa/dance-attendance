"""Аналитик посещаемости: берёт данные из базы, считает метрики (app/metrics.py)
и сохраняет показатели групп по неделям в weekly_group_stats.
"""

import dataclasses
from collections import Counter, defaultdict
from datetime import date, timedelta

from app import config
from app.metrics import (GroupWeek, aggregate_week, assign_status, check_completeness,
                         classify_sessions)


def loaded_weeks(conn) -> list[date]:
    """Недели (понедельники), у которых загружены все 7 дней."""
    days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT date FROM loaded_days")]
    per_week = Counter(d - timedelta(days=d.weekday()) for d in days)
    return sorted(m for m, n in per_week.items() if n == 7)


def _week_dates(monday: date) -> tuple[str, str]:
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def week_sessions(conn, monday: date):
    """Занятия недели с классификацией + посещения без времени по группам."""
    return period_sessions(conn, *_week_dates(monday))


def period_sessions(conn, start: str, end: str):
    """Занятия за период (даты ГГГГ-ММ-ДД включительно) + посещения без времени по группам."""
    scheduled = [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE date BETWEEN ? AND ?", (start, end))]
    visits, unmatched, no_time = defaultdict(Counter), [], Counter()
    for r in conn.execute("SELECT * FROM visits_agg WHERE date BETWEEN ? AND ?", (start, end)):
        if r["schedule_id"] is not None:
            visits[(r["schedule_id"], r["date"])][r["service_entity"]] += r["visits"]
        else:
            unmatched.append(dict(r))
            if r["minutes_begin"] is None:
                no_time[r["group_id"]] += r["visits"]
    bookings = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT schedule_id, date, booked FROM bookings WHERE date BETWEEN ? AND ?", (start, end))}

    group_cap = {r[0]: r[1] for r in conn.execute("SELECT id, place_count FROM groups")}
    hall_cap = {r[0]: r[1] for r in conn.execute("SELECT id, place_count FROM halls")}

    def capacity_of(group_id, hall_id):
        # Вместимость группы, если её заполнят в CRM, иначе — вместимость зала
        return group_cap.get(group_id) or hall_cap.get(hall_id)

    return classify_sessions(scheduled, visits, bookings, unmatched, capacity_of), no_time


def week_totals(conn, monday: date) -> dict:
    """Итоги недели по студии: все групповые посещения, включая отменённые и без времени."""
    start, end = _week_dates(monday)
    r = conn.execute(
        """SELECT COALESCE(SUM(visits), 0),
                  COALESCE(SUM(CASE WHEN service_entity = 'groupAccount' THEN visits END), 0),
                  COALESCE(SUM(CASE WHEN service_entity = 'groupSingle' THEN visits END), 0)
           FROM visits_agg WHERE date BETWEEN ? AND ?""", (start, end)).fetchone()
    s = conn.execute(
        """SELECT COALESCE(SUM(sessions_held), 0), COALESCE(SUM(sessions_cancelled), 0),
                  COALESCE(SUM(sessions_empty), 0)
           FROM weekly_group_stats WHERE week_start = ?""", (start,)).fetchone()
    previous = [w for w in loaded_weeks(conn) if w < monday][-config.BASELINE_WEEKS:]
    previous_totals = [conn.execute("SELECT COALESCE(SUM(visits), 0) FROM visits_agg WHERE date BETWEEN ? AND ?",
                                    _week_dates(w)).fetchone()[0] for w in previous]
    holidays = [f"{date.fromisoformat(d):%d.%m} {name}" for d, name in config.HOLIDAYS.items() if start <= d <= end]
    complete, why = check_completeness(r[0], previous_totals, holidays)
    return {
        "week_start": start, "visits_total": r[0], "visits_subscription": r[1], "visits_single": r[2],
        "sessions_held": s[0], "sessions_cancelled": s[1], "sessions_empty": s[2],
        "avg_previous": sum(previous_totals) / len(previous_totals) if previous_totals else None,
        "holidays": holidays, "complete": complete, "completeness_note": why,
    }


def recompute_all(conn) -> int:
    """Пересчитать показатели всех загруженных недель. Возвращает число строк."""
    weeks = loaded_weeks(conn)
    history: dict[int, list[GroupWeek]] = defaultdict(list)  # прошлые недели группы, старые → новые
    rows = []
    for monday in weeks:
        sessions, _ = week_sessions(conn, monday)
        for gid, w in aggregate_week(sessions).items():
            assign_status(w, history[gid])
            history[gid].append(w)
            rows.append((monday.isoformat(), w))

    fields = [f.name for f in dataclasses.fields(GroupWeek)]
    with conn:
        conn.execute("DELETE FROM weekly_group_stats")
        conn.executemany(
            f"INSERT INTO weekly_group_stats (week_start, {', '.join(fields)}) "
            f"VALUES (?, {', '.join('?' * len(fields))})",
            [(week, *(getattr(w, f) for f in fields)) for week, w in rows])
    return len(rows)
