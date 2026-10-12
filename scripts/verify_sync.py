"""Этап 2. Сверка загруженных данных с CRM.

Заново запрашивает данные из CRM, пересчитывает их напрямую (без кода app/sync.py)
и сравнивает с базой и с готовой Excel-таблицей.

Запуск:  .venv/bin/python -m scripts.verify_sync
"""

import logging
from collections import Counter
from datetime import date, timedelta

import pandas as pd

from app import db
from app.crm_client import CrmClient, day_ts, week_range
from app.sync import ts_to_date
from app.report import group_labels
from scripts.backfill import OUT_PATH

problems = []


def check(label, ok, details=""):
    print(f"{'✅' if ok else '❌'} {label}" + (f" — {details}" if details else ""))
    if not ok:
        problems.append(label)


def diff_counters(a: Counter, b: Counter, limit=10) -> list[str]:
    keys = sorted(set(a) | set(b), key=str)
    return [f"{k}: CRM {a.get(k, 0)} / у нас {b.get(k, 0)}" for k in keys if a.get(k, 0) != b.get(k, 0)][:limit]


def main():
    conn = db.connect()
    days = [date.fromisoformat(r[0]) for r in conn.execute("SELECT date FROM loaded_days ORDER BY date")]
    mondays = sorted({d - timedelta(days=d.weekday()) for d in days})
    week_of = lambda d: (d - timedelta(days=d.weekday())).strftime("%d.%m")
    print(f"Проверяю {len(mondays)} недель, {len(days)} дней: {days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}\n")

    raw_visits = Counter()          # (дата, группа) → посещений, прямой подсчёт из CRM
    raw_services = Counter()        # тип услуги → посещений
    raw_bookings = Counter()        # (дата, schedule_id) → записей
    raw_cancels = set()             # (дата, schedule_id)
    afro_0510 = Counter()
    with CrmClient() as crm:
        for monday in mondays:
            body = week_range(monday, monday + timedelta(days=6))
            for ts, slots in crm.call("schedule/places", body)["group"].items():
                for sid, n in slots.items():
                    raw_bookings[(ts_to_date(int(ts)).isoformat(), int(sid))] += n
            for c in crm.call("schedule/changes", body)["items"]:
                if c["entity"] == "cancel":
                    raw_cancels.add((ts_to_date(c["date"]).isoformat(), c["schedule"]["id"]))
        for d in days:
            for v in crm.call("check_visits/visits", {"date": day_ts(d), "branch_id": [1]}):
                if v["target"]["entity"] != "group":
                    continue
                raw_visits[(d.isoformat(), v["target"]["id"])] += 1
                raw_services[v["service"]["entity"]] += 1
                if d == date(2026, 10, 5) and v["target"]["style"]["name"].strip().lower() == "afro":
                    afro_0510[v["service"]["entity"]] += 1

    print("1. Посещения: CRM → база")
    db_visits = Counter({(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT date, group_id, SUM(visits) FROM visits_agg GROUP BY date, group_id")})
    bad = diff_counters(raw_visits, db_visits)
    check(f"Каждая группа в каждый из {len(days)} дней", not bad, "; ".join(bad))
    check("Всего групповых посещений", sum(raw_visits.values()) == sum(db_visits.values()),
          f"CRM {sum(raw_visits.values())} / у нас {sum(db_visits.values())}")
    db_services = Counter({r[0]: r[1] for r in conn.execute(
        "SELECT service_entity, SUM(visits) FROM visits_agg GROUP BY service_entity")})
    check("По типам услуг", raw_services == db_services, f"CRM {dict(raw_services)}")

    print("\n2. Записи и отмены: CRM → база")
    db_bookings = Counter({(r[0], r[1]): r[2] for r in conn.execute("SELECT date, schedule_id, booked FROM bookings")})
    bad = diff_counters(raw_bookings, db_bookings)
    check(f"Записи по каждому занятию ({len(raw_bookings)} шт.)", not bad, "; ".join(bad))
    db_sessions = {(r[0], r[1]): r[2] for r in conn.execute("SELECT date, schedule_id, cancelled FROM sessions")}
    db_cancels = {k for k, c in db_sessions.items() if c}
    known_cancels = {k for k in raw_cancels if k in db_sessions}
    check("Отмены занятий из расписания", known_cancels == db_cancels,
          f"{len(db_cancels)} отмен; ещё {len(raw_cancels) - len(known_cancels)} — по удалённым шаблонам")
    held = {k for k, c in db_sessions.items() if not c}
    only_places = {k for k in raw_bookings if k not in db_sessions}
    only_ours = {k for k in held if k not in raw_bookings}
    check("Занятия: наше расписание совпадает с занятиями, на которые CRM ведёт запись",
          not only_places and not only_ours,
          f"только в записях CRM: {sorted(only_places)[:5]} ({len(only_places)}); "
          f"только у нас: {sorted(only_ours)[:5]} ({len(only_ours)})")

    print("\n3. База → Excel-таблица")
    pivot = pd.read_excel(OUT_PATH, sheet_name="Посещения по неделям", index_col=0)
    unmatched = pd.read_excel(OUT_PATH, sheet_name="Вне расписания")
    labels = group_labels(conn)
    check("Названия групп в таблице не повторяются", len(set(labels.values())) == len(labels))
    label_to_id = {v: k for k, v in labels.items()}
    on_cancelled = Counter()
    for r in conn.execute("""SELECT v.date, v.group_id, SUM(v.visits) FROM visits_agg v
                             JOIN sessions s ON s.schedule_id = v.schedule_id AND s.date = v.date
                             WHERE s.cancelled = 1 GROUP BY v.date, v.group_id"""):
        on_cancelled[(week_of(date.fromisoformat(r[0])), r[1])] += r[2]
    raw_week = Counter()
    for (d, gid), n in raw_visits.items():
        raw_week[(week_of(date.fromisoformat(d)), gid)] += n
    table_week = Counter()
    for label, row in pivot.drop(index="ИТОГО").iterrows():
        for week, n in row.items():
            table_week[(week, label_to_id[label])] += int(n)
    for _, r in unmatched.iterrows():
        d = date.fromisoformat(str(r["Дата"])[:10])
        table_week[(week_of(d), label_to_id[r["Группа"]])] += int(r["Посещений"])
    table_week.update(on_cancelled)
    bad = diff_counters(+raw_week, +table_week)
    check(f"Таблица «группа × неделя» = CRM ({len(+raw_week)} непустых ячеек)", not bad, "; ".join(bad))
    check("Посещений на отменённых занятиях", not on_cancelled, f"{sum(on_cancelled.values())}")

    print("\n4. Ваши ручные проверки")
    check("05.10: групповых посещений 57 (58 минус 1 аренда)",
          sum(n for (d, _), n in raw_visits.items() if d == "2026-10-05") == 57
          and sum(n for (d, _), n in db_visits.items() if d == "2026-10-05") == 57)
    check("05.10: Afro 10 по абонементу + 4 разовых", (afro_0510["groupAccount"], afro_0510["groupSingle"]) == (10, 4))
    n_0510 = conn.execute("SELECT COUNT(*) FROM sessions WHERE date BETWEEN '2026-10-05' AND '2026-10-11'").fetchone()[0]
    check("Неделя 05.10: 31 занятие по расписанию", n_0510 == 31, f"у нас {n_0510}")

    print("\n5. Персональные данные")
    columns = [c[1].lower() for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
               for c in conn.execute(f"PRAGMA table_info({t[0]})")]
    check("В базе нет полей с данными клиентов", not any("client" in c for c in columns))

    print(f"\nИтог: {'всё сходится' if not problems else 'есть расхождения: ' + '; '.join(problems)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    main()
