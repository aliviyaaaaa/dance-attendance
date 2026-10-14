"""Загрузка данных из Impulse CRM в SQLite.

Данные загружаются целыми неделями (пн–вс). Повторная загрузка недели
не создаёт дублей: старые строки за эти даты удаляются и пишутся заново.
"""

import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from app import db
from app.crm_client import CrmClient, CrmError, day_ts, week_range

log = logging.getLogger(__name__)

KNOWN_SERVICES = {"groupAccount", "groupSingle"}


# ---------- вспомогательные функции ----------

def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def ts_to_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


def require(endpoint: str, obj, keys: list[str], where: str):
    """Проверка, что в ответе CRM есть ожидаемые поля. Иначе — понятная ошибка."""
    if not isinstance(obj, dict):
        raise CrmError(endpoint, f"{where}: ожидался объект, пришло {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise CrmError(endpoint, f"{where}: нет полей {', '.join(missing)}")


def require_items(endpoint: str, resp) -> list:
    require(endpoint, resp, ["items"], "ответ")
    if not isinstance(resp["items"], list):
        raise CrmError(endpoint, "поле items не список")
    return resp["items"]


def teacher_name(t: dict | None) -> str | None:
    """Только имя и первая буква фамилии: «Аня К.»"""
    if not t:
        return None
    name = (t.get("name") or "").strip()
    last = (t.get("lastName") or "").strip()
    return f"{name} {last[0]}." if last else name or None


def clean_style(raw: str | None) -> str:
    return " ".join((raw or "").split()) or "Без направления"


# ---------- запись в базу ----------

def upsert_group(conn, g: dict, now: str):
    style_raw = (g.get("style") or {}).get("name")
    conn.execute(
        """INSERT INTO groups (id, number, style_raw, style, teacher1, teacher2,
                               place_count, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               number=excluded.number, style_raw=excluded.style_raw, style=excluded.style,
               teacher1=excluded.teacher1, teacher2=excluded.teacher2,
               place_count=excluded.place_count, status=excluded.status,
               updated_at=excluded.updated_at""",
        (g["id"], g.get("number"), style_raw, clean_style(style_raw),
         teacher_name(g.get("teacher1")), teacher_name(g.get("teacher2")),
         g.get("placeCount"), g.get("status"), now),
    )


def sync_halls(crm: CrmClient, conn):
    for h in require_items("hall/list", crm.call("hall/list", {})):
        require("hall/list", h, ["id", "name", "placeCount"], "зал")
        conn.execute(
            """INSERT INTO halls (id, name, place_count) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, place_count=excluded.place_count""",
            (h["id"], h["name"].strip(), h["placeCount"]),
        )


def expand_schedule(items: list, days: list[date]) -> list[dict]:
    """Регулярные шаблоны и разовые занятия → конкретные занятия на даты недели."""
    sessions = []
    for it in items:
        if it["type"] != 0 or not it.get("group"):
            continue  # не группа
        for d in days:
            ts = day_ts(d)
            if it["regular"]:
                if d.isoweekday() != it["day"]:
                    continue
                if it.get("dateBegin") and ts < it["dateBegin"]:
                    continue
                if it.get("dateEnd") and ts > it["dateEnd"]:
                    continue
            elif it.get("date") != ts:
                continue
            sessions.append({
                "schedule_id": it["id"],
                "date": d.isoformat(),
                "group_id": it["group"]["id"],
                "hall_id": (it.get("hall") or {}).get("id"),
                "minutes_begin": it["minutesBegin"],
                "minutes_end": it.get("minutesEnd"),
                "regular": int(bool(it["regular"])),
            })
    return sessions


def match_session(sessions_by_key: dict, group_id: int, minutes_begin: int, hall_id) -> int | None:
    """Посещение → занятие по (группа, время); если таких несколько — уточняем залом."""
    candidates = sessions_by_key.get((group_id, minutes_begin), [])
    if len(candidates) == 1:
        return candidates[0]["schedule_id"]
    for s in candidates:
        if s["hall_id"] == hall_id:
            return s["schedule_id"]
    return None


def aggregate_visits(visits: list) -> tuple[Counter, Counter, dict]:
    """Посещения за день → счётчики по (группа, время, зал, тип услуги).

    clientId не читается и никуда не сохраняется.
    Возвращает: счётчики групповых посещений, счётчик прочих (аренда и т.п.), группы из ответа.
    """
    group_counts, other_counts, groups = Counter(), Counter(), {}
    for v in visits:
        require("check_visits/visits", v, ["target", "service", "minutesBegin", "hallId"], "посещение")
        target, service = v["target"] or {}, v["service"] or {}
        if target.get("entity") != "group":
            other_counts[target.get("entity")] += 1
            continue
        require("check_visits/visits", target, ["id"], "посещение.target")
        key = (target["id"], v["minutesBegin"], v["hallId"], service.get("entity") or "unknown")
        group_counts[key] += 1
        groups[target["id"]] = target
    return group_counts, other_counts, groups


def sync_week(crm: CrmClient, conn, monday: date, today: date) -> dict:
    """Загрузить одну неделю. Посещения — только за прошедшие дни (до вчера)."""
    days = [monday + timedelta(days=i) for i in range(7)]
    body = week_range(days[0], days[-1])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    stats = Counter()

    # Расписание
    items = require_items("schedule/list", crm.call("schedule/list", body))
    for it in items:
        require("schedule/list", it, ["id", "regular", "type", "minutesBegin", "group"], "занятие")
    sessions = expand_schedule(items, days)

    # Отмены и замены
    changes = require_items("schedule/changes", crm.call("schedule/changes", body))
    for c in changes:
        require("schedule/changes", c, ["id", "entity", "date", "schedule"], "изменение")

    # Записи
    places = crm.call("schedule/places", body)
    require("schedule/places", places, ["group"], "ответ")

    # Посещения по дням
    day_visits = {}
    for d in days:
        if d >= today:
            continue
        resp = crm.call("check_visits/visits", {"date": day_ts(d), "branch_id": [1]})
        if not isinstance(resp, list):
            raise CrmError("check_visits/visits", f"ожидался список, пришло {type(resp).__name__}")
        day_visits[d] = aggregate_visits(resp)

    # Запись в базу одной транзакцией
    dates = [d.isoformat() for d in days]
    marks = ",".join("?" * len(dates))
    with conn:
        for table in ("sessions", "bookings", "changes"):
            conn.execute(f"DELETE FROM {table} WHERE date IN ({marks})", dates)
        for d in day_visits:
            conn.execute("DELETE FROM visits_agg WHERE date = ?", (d.isoformat(),))

        for it in items:
            if it["type"] == 0 and it.get("group"):
                upsert_group(conn, it["group"], now)

        for s in sessions:
            conn.execute(
                """INSERT INTO sessions (schedule_id, date, group_id, hall_id,
                                         minutes_begin, minutes_end, regular)
                   VALUES (:schedule_id, :date, :group_id, :hall_id,
                           :minutes_begin, :minutes_end, :regular)""", s)
        stats["sessions"] = len(sessions)

        for c in changes:
            c_date = ts_to_date(c["date"]).isoformat()
            schedule_id = (c["schedule"] or {}).get("id")
            conn.execute(
                "INSERT OR REPLACE INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                (c["id"], c_date, schedule_id, c["entity"],
                 teacher_name(c.get("sourceTeacher")), teacher_name(c.get("teacher"))),
            )
            if c["entity"] in ("cancel", "transfer"):
                # перенос: исходное занятие в старое время не проходит, новое разовое уже есть в расписании
                cur = conn.execute("UPDATE sessions SET cancelled = 1 WHERE schedule_id = ? AND date = ?",
                                   (schedule_id, c_date))
                if cur.rowcount:
                    stats["cancelled"] += 1
                else:
                    # шаблон удалён из расписания CRM — отмену храним только в журнале
                    stats["cancel_orphan"] += 1
            elif c["entity"] == "substitute":
                stats["substitutes"] += 1
            elif c["entity"] == "extraPlace":
                pass  # добавили места на занятие — на посещаемость не влияет
            else:
                log.warning("Неизвестный вид изменения в расписании: %s", c["entity"])

        for ts, per_slot in (places["group"] or {}).items():
            p_date = ts_to_date(int(ts)).isoformat()
            for schedule_id, booked in per_slot.items():
                conn.execute("INSERT INTO bookings VALUES (?, ?, ?)", (int(schedule_id), p_date, booked))
                stats["booked"] += booked

        for d, (group_counts, other_counts, groups) in day_visits.items():
            for gid, target in groups.items():
                if not conn.execute("SELECT 1 FROM groups WHERE id = ?", (gid,)).fetchone():
                    upsert_group(conn, target, now)
            by_key = {}
            for s in sessions:
                if s["date"] == d.isoformat():
                    by_key.setdefault((s["group_id"], s["minutes_begin"]), []).append(s)
            for (gid, minutes, hall_id, service), n in group_counts.items():
                schedule_id = match_session(by_key, gid, minutes, hall_id)
                conn.execute("INSERT INTO visits_agg VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (d.isoformat(), gid, minutes, hall_id, service, schedule_id, n))
                stats["visits"] += n
                if schedule_id is None:
                    stats["visits_unmatched"] += n
                if service not in KNOWN_SERVICES:
                    log.warning("Необычный тип услуги %s: %d посещ. (%s)", service, n, d)
                    stats["visits_other_service"] += n
            stats["non_group_visits"] += sum(other_counts.values())
            conn.execute("INSERT OR REPLACE INTO loaded_days VALUES (?, ?)", (d.isoformat(), now))

    log.info("Неделя %s: занятий %d, отмен %d, посещений %d (вне расписания %d), записей %d",
             monday.strftime("%d.%m"), stats["sessions"], stats["cancelled"],
             stats["visits"], stats["visits_unmatched"], stats["booked"])
    return dict(stats)


def sync_weeks(mondays: list[date], today: date | None = None) -> dict:
    today = today or date.today()
    total = Counter()
    conn = db.connect()
    try:
        with CrmClient() as crm:
            with conn:
                sync_halls(crm, conn)
            for monday in mondays:
                total.update(sync_week(crm, conn, monday, today))
    finally:
        conn.close()
    total["weeks"] = len(mondays)
    return dict(total)


def backfill(weeks: int = 12, today: date | None = None) -> dict:
    """Первичная загрузка: N полных недель до текущей."""
    today = today or date.today()
    current = monday_of(today)
    return sync_weeks([current - timedelta(weeks=i) for i in range(weeks, 0, -1)], today)


def refresh(days: int = 14, today: date | None = None) -> dict:
    """Регулярное обновление: перезагрузить недели, которые задевают последние N дней."""
    today = today or date.today()
    first = monday_of(today - timedelta(days=days))
    mondays = []
    while first <= today:
        mondays.append(first)
        first += timedelta(weeks=1)
    return sync_weeks(mondays, today)
