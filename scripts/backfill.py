"""Этап 2. Первичная загрузка 12 недель и таблица «группа × неделя × посещения».

Запуск:  .venv/bin/python -m scripts.backfill
Результат: data/garage.sqlite и reports/группы_по_неделям.xlsx
"""

import logging
import time

import pandas as pd

from app import config, db
from app.crm_client import CrmError
from app.report import group_labels, hhmm
from app.sync import backfill

OUT_PATH = config.PROJECT_DIR / "reports" / "группы_по_неделям.xlsx"


def export_table(conn):
    detail = pd.read_sql_query(
        """SELECT s.date, s.group_id, s.minutes_begin, h.name AS hall, s.cancelled,
                  COALESCE(b.booked, 0) AS booked,
                  COALESCE(SUM(CASE WHEN v.service_entity = 'groupAccount' THEN v.visits END), 0) AS subscription,
                  COALESCE(SUM(CASE WHEN v.service_entity = 'groupSingle' THEN v.visits END), 0) AS single,
                  COALESCE(SUM(v.visits), 0) AS visits
           FROM sessions s
           LEFT JOIN halls h ON h.id = s.hall_id
           LEFT JOIN bookings b ON b.schedule_id = s.schedule_id AND b.date = s.date
           LEFT JOIN visits_agg v ON v.schedule_id = s.schedule_id AND v.date = s.date
           WHERE s.date IN (SELECT date FROM loaded_days)
           GROUP BY s.schedule_id, s.date
           ORDER BY s.date, s.minutes_begin""", conn)
    unmatched = pd.read_sql_query(
        """SELECT date, group_id, minutes_begin, service_entity, visits
           FROM visits_agg WHERE schedule_id IS NULL ORDER BY date""", conn)

    labels = group_labels(conn)
    for df in (detail, unmatched):
        df["Группа"] = df["group_id"].map(labels)
        df["Время"] = df["minutes_begin"].map(hhmm)
    detail["date"] = pd.to_datetime(detail["date"])
    detail["Неделя"] = (detail["date"] - pd.to_timedelta(detail["date"].dt.weekday, unit="D")).dt.strftime("%d.%m")

    held = detail[detail["cancelled"] == 0]
    pivot = held.pivot_table(index="Группа", columns="Неделя", values="visits", aggfunc="sum", fill_value=0)
    sessions = held.pivot_table(index="Группа", columns="Неделя", values="visits", aggfunc="count", fill_value=0)
    week_order = sorted(pivot.columns, key=lambda w: detail.loc[detail["Неделя"] == w, "date"].min())
    pivot, sessions = pivot[week_order], sessions.reindex(columns=week_order, fill_value=0)
    pivot.loc["ИТОГО"] = pivot.sum()

    detail_out = detail.assign(
        Дата=detail["date"].dt.strftime("%d.%m.%Y"),
        Статус=detail["cancelled"].map({0: "состоялось", 1: "отменено"}),
    )[["Неделя", "Дата", "Время", "Группа", "hall", "Статус", "booked", "subscription", "single", "visits"]]
    detail_out.columns = ["Неделя", "Дата", "Время", "Группа", "Зал", "Статус",
                          "Записались", "По абонементу", "Разовые", "Посещений всего"]
    unmatched_out = unmatched[["date", "Время", "Группа", "service_entity", "visits"]]
    unmatched_out.columns = ["Дата", "Время", "Группа", "Тип услуги", "Посещений"]

    OUT_PATH.parent.mkdir(exist_ok=True)
    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as xl:
        pivot.to_excel(xl, sheet_name="Посещения по неделям")
        sessions.to_excel(xl, sheet_name="Занятий состоялось")
        detail_out.to_excel(xl, sheet_name="Все занятия", index=False)
        unmatched_out.to_excel(xl, sheet_name="Вне расписания", index=False)
        for ws in xl.book.worksheets:
            ws.column_dimensions["A"].width = 45 if ws.title != "Все занятия" else 8
            ws.freeze_panes = "B2"
        xl.book["Все занятия"].column_dimensions["D"].width = 45
    return pivot


def main():
    started = time.monotonic()
    stats = backfill(weeks=12)
    print(f"\nЗагружено за {time.monotonic() - started:.0f} с: {stats}")
    conn = db.connect()
    try:
        pivot = export_table(conn)
    finally:
        conn.close()
    print(f"Групп: {len(pivot) - 1}. Таблица: {OUT_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except CrmError as e:
        print(f"\n❌ {e}")
        raise SystemExit(1)
