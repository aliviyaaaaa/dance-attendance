"""Оценка нагрузки для бота записи: сколько новых людей приходит в студию.

Номера клиентов используются только в памяти, никуда не сохраняются;
результат — агрегаты по месяцам.

Запуск:  .venv/bin/python -m scripts.estimate_load 2025-04-01 2026-10-11
"""

import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

from app.crm_client import CrmClient, day_ts


def main(start: date, end: date):
    first_seen: dict[int, date] = {}          # клиент → дата первого посещения (только в памяти)
    first_service: dict[int, str] = {}
    active = defaultdict(set)                 # месяц → клиенты (только в памяти)
    single = Counter()
    days_in_month = Counter()
    with CrmClient() as crm:
        d = start
        while d <= end:
            month = d.strftime("%Y-%m")
            days_in_month[month] += 1
            for v in crm.call("check_visits/visits", {"date": day_ts(d), "branch_id": [1]}):
                if (v.get("target") or {}).get("entity") != "group":
                    continue
                cid, service = v.get("clientId"), (v.get("service") or {}).get("entity")
                active[month].add(cid)
                if service == "groupSingle":
                    single[month] += 1
                if cid not in first_seen:
                    first_seen[cid], first_service[cid] = d, service
            d += timedelta(days=1)

    new = Counter(first_seen[c].strftime("%Y-%m") for c in first_seen)
    new_single = Counter(first_seen[c].strftime("%Y-%m") for c in first_seen if first_service[c] == "groupSingle")
    print("месяц    | активных клиентов | новых | новых с разового | разовых посещений | новых в день")
    for month in sorted(days_in_month):
        print(f"{month}  | {len(active[month]):>17} | {new[month]:>5} | {new_single[month]:>16} | "
              f"{single[month]:>17} | {new[month] / days_in_month[month]:>12.1f}")
    print("\nПервые месяцы завышают «новых»: до начала периода истории нет, все кажутся новыми.")


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]))
