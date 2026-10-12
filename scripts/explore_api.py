"""Этап 1. Разведка API Impulse CRM за неделю 05–11.10.2026.

Печатает только структуру ответов (имена полей и типы) и агрегаты.
Значения полей не печатаются, кроме безопасных: названия направлений, залов, счётчики.

Запуск:  .venv/bin/python -m scripts.explore_api
"""

import logging
from collections import Counter
from datetime import date, timedelta

from app.crm_client import CrmClient, CrmError, day_ts, week_range

MONDAY = date(2026, 10, 5)
SUNDAY = MONDAY + timedelta(days=6)

EXPECTED_REGULAR_GROUP_LESSONS = 31
EXPECTED_VISITS_0510 = 58


def shape(value, depth=0, max_depth=3):
    """Скелет значения: для словаря — поля и их типы, для списка — скелет первого элемента."""
    if isinstance(value, dict):
        if depth >= max_depth:
            return "{...}"
        return {k: shape(v, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "[] (пусто)"
        return [f"{len(value)} шт.", shape(value[0], depth + 1, max_depth)]
    return type(value).__name__


def print_shape(title, value):
    print(f"\n=== {title} ===")
    _print_tree(shape(value), indent=2)


def _print_tree(node, indent):
    pad = " " * indent
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                _print_tree(v, indent + 2)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(node, list):
        print(f"{pad}{node[0]}, каждый вида:")
        _print_tree(node[1], indent + 2)
    else:
        print(f"{pad}{node}")


def check(label, actual, expected):
    mark = "✅" if actual == expected else "❌"
    print(f"{mark} {label}: получено {actual}, ожидалось {expected}")


def style_name(obj):
    style = (obj or {}).get("style") or {}
    return (style.get("name") or "?").strip()


def main():
    body = week_range(MONDAY, SUNDAY)
    with CrmClient() as crm:
        # Справочники
        branches = crm.call("branch/list", {})
        print_shape("branch/list", branches)
        halls = crm.call("hall/list", {})
        print_shape("hall/list", halls)
        hall_items = halls.get("items", halls) if isinstance(halls, dict) else halls
        if isinstance(hall_items, list):
            print("  Залы:", ", ".join(f"{h.get('name')} ({h.get('placeCount')})" for h in hall_items))

        # Расписание
        schedule = crm.call("schedule/list", body)
        print_shape("schedule/list", schedule)
        items = schedule.get("items", [])
        kinds = Counter(
            ("регулярное" if it.get("regular") else "разовое", it.get("type"), it.get("typeTitle"))
            for it in items
        )
        print("\n  Состав расписания (вид, type, typeTitle → кол-во):")
        for k, n in sorted(kinds.items(), key=str):
            print(f"    {k}: {n}")
        regular_groups = [it for it in items if it.get("regular") and it.get("type") == 0]
        print(f"  total в ответе: {schedule.get('total')}")
        print("  Направления:", sorted({style_name(it.get("group")) for it in regular_groups}))

        # Записи, замены и отмены
        places = crm.call("schedule/places", body)
        print_shape("schedule/places", places)
        booked = sum(sum(day.values()) for day in (places.get("group") or {}).values())
        print(f"  Всего записей за неделю: {booked}")

        changes = crm.call("schedule/changes", body)
        print_shape("schedule/changes", changes)
        print("  Виды изменений:", dict(Counter(c.get("entity") for c in changes.get("items", []))))

        # Посещения по дням
        print("\n=== check_visits/visits по дням ===")
        visits_0510 = None
        first_visits = None
        for i in range(7):
            d = MONDAY + timedelta(days=i)
            visits = crm.call("check_visits/visits", {"date": day_ts(d), "branch_id": [1]})
            if first_visits is None and visits:
                first_visits = visits
            targets = Counter((v.get("target") or {}).get("entity") for v in visits)
            services = Counter((v.get("service") or {}).get("entity") for v in visits)
            print(f"  {d:%d.%m} ({d:%a}): {len(visits)} посещений; "
                  f"target={dict(targets)}; service={dict(services)}")
            if d == MONDAY:
                visits_0510 = visits
        if first_visits:
            print_shape("check_visits/visits (структура одного посещения)", first_visits)

        afro = Counter(
            (v.get("service") or {}).get("entity")
            for v in visits_0510 or []
            if style_name(v.get("target")).lower().startswith("afro")
        )
        print(f"\n  Afro 05.10 по типу услуги: {dict(afro)}")

        print("\n=== Сверка с ручной проверкой ===")
        check("Регулярных групповых занятий в расписании", len(regular_groups), EXPECTED_REGULAR_GROUP_LESSONS)
        check("Посещений за 05.10", len(visits_0510 or []), EXPECTED_VISITS_0510)
        check("Afro 05.10 (абонемент, разовые)",
              (afro.get("groupAccount", 0), afro.get("groupSingle", 0)), (10, 4))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except CrmError as e:
        print(f"\n❌ {e}")
        raise SystemExit(1)
