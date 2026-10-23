"""Посещения по каждому занятию за период: кто вёл и сколько человек пришло.

Нужно для расчёта зарплаты педагогов (ставка зависит от количества человек на занятии).
Позже сюда добавится формула ставки.
"""

from datetime import date, timedelta

import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app import config
from app.attendance import period_sessions
from app.metrics import CANCELLED, EMPTY
from app.report import DAYS, REPORTS_DIR, hhmm, last_full_week, parse_date, parse_week_arg

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MAX_DAYS = 62


def parse_period(text: str | None, today: date | None = None) -> tuple[date, date]:
    """Период для /visits:
    пусто → последний закончившийся расчётный период (1–15 или 16–конец месяца);
    «28.09» → неделя с этой датой; «16.09-30.09» → с и по; «сентябрь» → весь месяц."""
    today = today or date.today()
    text = (text or "").strip().lower()
    if not text:
        return last_pay_period(today)
    if text in MONTHS:
        month = MONTHS.index(text) + 1
        year = today.year if month <= today.month else today.year - 1
        start = date(year, month, 1)
        end = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
        return start, min(end, today - timedelta(days=1))
    for dash in ("–", "—", "-"):
        if dash in text:
            a, b = text.split(dash, 1)
            start = parse_date(a, today)
            end = parse_date(b, today)
            if start > end:
                start = start.replace(year=start.year - 1)
            if (end - start).days >= MAX_DAYS:
                raise ValueError(f"Период слишком длинный: максимум {MAX_DAYS} дня")
            return start, end
    monday = parse_week_arg(text, today)
    return monday, monday + timedelta(days=6)


def rate_for(people: int, rates=None) -> int | None:
    """Ставка за занятие по числу человек. None — для такого числа ставка не задана (0 или больше 29)."""
    for low, high, rub in rates or config.PAY_RATES:
        if low <= people <= high:
            return rub
    return None


def last_pay_period(today: date) -> tuple[date, date]:
    """Зарплата дважды в месяц: 1–15 и 16–последний день. Возвращает последний закончившийся период."""
    if today.day > 15:
        return today.replace(day=1), today.replace(day=15)
    last_month_end = today.replace(day=1) - timedelta(days=1)
    return last_month_end.replace(day=16), last_month_end


def pay_period_end(start: date) -> date:
    """Конец расчётного периода: 15-е число или последний день месяца."""
    if start.day == 1:
        return start.replace(day=15)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def recent_pay_periods(today: date, count: int = 3) -> list[tuple[date, date, bool]]:
    """Расчётные периоды для кнопок, новые сверху: (начало, конец, идёт ли сейчас)."""
    periods = []
    current = today.replace(day=1 if today.day <= 15 else 16)
    if today > current:                                   # в текущем периоде уже есть прошедшие дни
        periods.append((current, today - timedelta(days=1), True))
    start = current
    for _ in range(count):
        start = last_pay_period(start)[0]
        periods.append((start, pay_period_end(start), False))
    return periods


def merge_joint_lessons(df: pd.DataFrame) -> pd.DataFrame:
    """Один педагог, один день, одно время, несколько групп → одно объединённое занятие.

    В CRM объединение выглядит как «замена»: педагог «заменяет» вторую группу в то же время,
    когда ведёт свою. Платим за одно занятие с суммой людей (правило подтверждено студией:
    одно время у одного педагога — всегда одно занятие).
    """
    counted = df[df["counts"] & df["date"].notna()]
    keys = ["date", "minutes", "Вёл"]
    joint = counted[counted.duplicated(keys, keep=False)]
    if joint.empty:
        return df
    merged = []
    for _, part in joint.groupby(keys, sort=False):
        first = part.iloc[0].to_dict()
        first.update({
            "Направление": " + ".join(part["Направление"]),
            "Замена за": ", ".join(x for x in part["Замена за"] if x),
            "Человек": part["Человек"].sum(), "по абонементу": part["по абонементу"].sum(),
            "разовых": part["разовых"].sum(), "Записались": part["Записались"].sum(),
            "Статус": f"объединённое занятие ({len(part)} группы)", "joint": True,
        })
        merged.append(first)
    return pd.concat([df.drop(joint.index), pd.DataFrame(merged)], ignore_index=True)


def lesson_rows(conn, start: date, end: date) -> pd.DataFrame:
    """Одна строка = одно занятие: дата, время, направление, кто вёл, сколько пришло."""
    sessions, no_time = period_sessions(conn, start.isoformat(), end.isoformat())
    groups = {r["id"]: dict(r) for r in conn.execute("SELECT id, style, teacher1, teacher2 FROM groups")}
    substitutes = {(r["schedule_id"], r["date"]): (r["source_teacher"], r["teacher"]) for r in conn.execute(
        "SELECT schedule_id, date, source_teacher, teacher FROM changes WHERE entity = 'substitute' "
        "AND date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))}

    rows = []
    for s in sessions:
        g = groups[s.group_id]
        planned = " и ".join(t for t in (g["teacher1"], g["teacher2"]) if t) or "?"
        led_by, replaced = planned, ""
        if (s.schedule_id, s.date) in substitutes:
            source, teacher = substitutes[(s.schedule_id, s.date)]
            led_by, replaced = teacher or "?", source or planned
        people = sum(s.visits.values())
        if s.kind == CANCELLED:
            status = "отменено" + (f", но отмечено посещений: {people}" if people else "")
        elif s.kind == EMPTY:
            status = "пустое: никто не записан и не пришёл"
        else:
            status = "вне расписания CRM" if s.off_schedule else "состоялось"
        rows.append({
            "date": s.date, "minutes": s.minutes_begin, "group_id": s.group_id,
            "Направление": g["style"], "Вёл": led_by, "Замена за": replaced,
            "Человек": people, "по абонементу": s.visits.get("groupAccount", 0),
            "разовых": s.visits.get("groupSingle", 0), "Записались": s.booked,
            "Статус": status, "counts": s.kind not in (CANCELLED, EMPTY),
        })
    for gid, n in no_time.items():
        g = groups[gid]
        rows.append({"date": None, "minutes": None, "group_id": gid, "Направление": g["style"],
                     "Вёл": g["teacher1"] or "?", "Замена за": "", "Человек": n, "по абонементу": None,
                     "разовых": None, "Записались": None, "counts": False,
                     "Статус": "посещения отмечены без даты занятия — проверить вручную"})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["joint"] = False
    df = merge_joint_lessons(df)
    # После объединения групп: максимум суммарных записей и суммарных посещений.
    # Фактические посещения сохраняем отдельно для аналитики.
    df["Для зарплаты"] = [max(int(n), int(booked or 0)) if counts else None
                          for n, booked, counts in zip(df["Человек"], df["Записались"], df["counts"])]
    df["Оплата, ₽"] = [rate_for(int(n)) if counts else None
                       for n, counts in zip(df["Для зарплаты"], df["counts"])]
    return df.sort_values(["date", "minutes", "Направление"], na_position="last").reset_index(drop=True)


def missing_days(conn, start: date, end: date, today: date | None = None) -> list[date]:
    today = today or date.today()
    loaded = {r[0] for r in conn.execute("SELECT date FROM loaded_days WHERE date BETWEEN ? AND ?",
                                         (start.isoformat(), end.isoformat()))}
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [d for d in days if d < today and d.isoformat() not in loaded]


def rub(x) -> str:
    return f"{x:,.0f} ₽".replace(",", " ")


def build_text(rows: pd.DataFrame, start: date, end: date, missing: list[date]) -> str:
    lines = [f"👥 Посещения по занятиям {start:%d.%m}–{end:%d.%m.%Y}"]
    if missing:
        lines.append(f"⚠️ Нет загруженных данных за {len(missing)} дн. "
                     f"({missing[0]:%d.%m}–{missing[-1]:%d.%m}) — сделайте /refresh или выберите другой период")
    if rows.empty:
        return "\n".join(lines + ["", "Занятий за период нет."])

    lines.append("Для зарплаты берём большее из записавшихся и отмеченных; это оценка, не число уникальных людей.")
    for day, day_rows in rows[rows["date"].notna()].groupby("date", sort=True):
        d = date.fromisoformat(day)
        lines += ["", f"{d:%d.%m}, {DAYS[d.weekday()]}"]
        for _, r in day_rows.iterrows():
            if r["joint"]:
                who, mark = r["Вёл"], f"  🔗 {r['Статус']}"
            else:
                who = r["Вёл"] + (f" (замена за {r['Замена за']})" if r["Замена за"] else "")
                mark = "" if r["Статус"] == "состоялось" else f"  ⚠️ {r['Статус']}"
            pay = ""
            if r["counts"]:
                pay = f" · {rub(r['Оплата, ₽'])}" if pd.notna(r["Оплата, ₽"]) else " · ❓ставка не задана"
            total = str(int(r["Для зарплаты"])) if r["counts"] else "не учитывается"
            lines.append(f"• {hhmm(r['minutes'])} {r['Направление']} · {who} — "
                         f"для зарплаты: {total}; записались: {int(r['Записались'])}; "
                         f"отмечены: {int(r['Человек'])}{pay}{mark}")

    extra = rows[rows["date"].isna()]
    for _, r in extra.iterrows():
        lines += ["", f"⚠️ {r['Направление']} · {r['Вёл']}: {r['Человек']} — {r['Статус']}"]

    counted = rows[rows["counts"]]
    totals = counted.groupby("Вёл").agg(lessons=("Человек", "size"), people=("Для зарплаты", "sum"),
                                        attended=("Человек", "sum"), booked=("Записались", "sum"),
                                        pay=("Оплата, ₽", "sum"), no_rate=("Оплата, ₽", lambda x: x.isna().sum()))
    lines += ["", "💰 Итого по педагогам:"]
    for teacher, t in totals.sort_index().iterrows():
        warn = f" (+{t['no_rate']:.0f} зан. без ставки ❓)" if t["no_rate"] else ""
        lines.append(f"• {teacher}: {rub(t['pay'])} — занятий {int(t['lessons'])}, для зарплаты {int(t['people'])}, "
                     f"записались {int(t['booked'])}, отмечены {int(t['attended'])}{warn}")
    lines += ["", f"Всего: {rub(counted['Оплата, ₽'].sum())} — занятий {len(counted)}, "
                  f"для зарплаты {int(counted['Для зарплаты'].sum())}, "
                  f"записались {int(counted['Записались'].sum())}, отмечены {int(counted['Человек'].sum())}"]
    if counted["Оплата, ₽"].isna().any():
        lines.append("❓ — число людей вне таблицы ставок (0 или больше 29): посчитайте вручную")
    return "\n".join(lines)


def build_excel(rows: pd.DataFrame, start: date, end: date) -> str:
    table = rows.copy()
    table.insert(0, "Дата", table["date"].map(lambda d: f"{date.fromisoformat(d):%d.%m.%Y}" if d else "—"))
    table.insert(1, "День", table["date"].map(lambda d: DAYS[date.fromisoformat(d).weekday()] if d else ""))
    table.insert(2, "Время", table["minutes"].map(hhmm))
    table = table.drop(columns=["date", "minutes", "group_id", "counts", "joint"])

    table = table.rename(columns={"Человек": "Отмечены", "по абонементу": "Отмечены по абонементу",
                                  "разовых": "Отмечены разовые"})
    columns = list(table.columns)
    columns.remove("Для зарплаты")
    columns.insert(columns.index("Отмечены"), "Для зарплаты")
    table = table[columns]

    counted = rows[rows["counts"]]
    by_teacher = counted.groupby("Вёл").agg(**{
        "Занятий": ("Человек", "size"), "Для зарплаты всего": ("Для зарплаты", "sum"),
        "Записались всего": ("Записались", "sum"), "Отмечены всего": ("Человек", "sum"),
        "Отмечены в среднем на занятии": ("Человек", "mean"),
        "Из них замен": ("Замена за", lambda x: (x != "").sum()),
        "Объединённых": ("joint", "sum"),
        "К выплате, ₽": ("Оплата, ₽", "sum"),
    }).round(1)

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"посещения_по_занятиям_{start:%Y-%m-%d}_{end:%Y-%m-%d}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        table.to_excel(xl, sheet_name="Занятия", index=False)
        by_teacher.to_excel(xl, sheet_name="По педагогам")
        for ws in xl.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, col in enumerate(ws.columns, 1):
                width = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 50)
            ws.freeze_panes = "A2"
    return str(path)


def build_lessons_report(conn, start: date, end: date) -> tuple[str, str | None]:
    rows = lesson_rows(conn, start, end)
    text = build_text(rows, start, end, missing_days(conn, start, end))
    return text, (build_excel(rows, start, end) if not rows.empty else None)
