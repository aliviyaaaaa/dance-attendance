"""Сверка сводки админов с данными CRM: каждое занятие и итог к выплате по педагогам.

Формат сводки: дата «16.09», затем строки «Имя - N» или «Имя (замена Кто_вёл) - N».
Платим тому, кто вёл: в скобках после «замена».
"""

import re
from collections import defaultdict
from datetime import date

import pandas as pd

from app import config
from app.lessons import lesson_rows, rate_for
from app.report import parse_date

LINE = re.compile(r"^(?P<name>[^(\-–]+?)\s*(\(замена\s+(?P<led>[^)]+)\))?\s*[-–]\s*(?P<n>\d+)\s*$")


def teacher(name: str) -> str:
    first = name.strip().split()[0].rstrip(".")
    return config.TEACHER_ALIASES.get(first, name.strip())


def parse_admin(text: str, today: date) -> list[dict]:
    rows, day = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", line):
            day = parse_date(line, today).isoformat()
            continue
        m = LINE.match(line)
        if not m or day is None:
            raise ValueError(f"Не понял строку сводки: «{line}»")
        rows.append({"date": day, "led": teacher(m["led"] or m["name"]), "people": int(m["n"]), "line": line})
    return rows


def compare(conn, text: str, today: date | None = None):
    """Сводка админов vs бот. Возвращает (таблица по занятиям, деньги по педагогам, период, число строк)."""
    admin = parse_admin(text, today or date.today())
    if not admin:
        raise ValueError("В сводке не нашлось ни одной строки вида «Имя - N»")
    start, end = date.fromisoformat(admin[0]["date"]), date.fromisoformat(admin[-1]["date"])
    bot = lesson_rows(conn, start, end)
    bot = bot[bot["counts"] & bot["date"].notna()]

    # Сопоставляем по (дата, кто вёл) в порядке времени
    bot_by = defaultdict(list)
    for _, r in bot.iterrows():
        bot_by[(r["date"], r["Вёл"])].append(r)
    adm_by = defaultdict(list)
    for a in admin:
        adm_by[(a["date"], a["led"])].append(a)

    result = []
    for key in sorted(set(bot_by) | set(adm_by)):
        b_list, a_list = bot_by.get(key, []), adm_by.get(key, [])
        if len(b_list) != len(a_list) and len(b_list) == 1 and len(a_list) > 1:
            # у бота одно объединённое занятие, у админов несколько строк
            b = b_list[0]
            result.append({"Дата": key[0][5:], "Педагог": key[1],
                           "Админы": " + ".join(str(a["people"]) for a in a_list), "Бот": b["Человек"],
                           "₽ админы": sum(rate_for(a["people"]) or 0 for a in a_list),
                           "₽ бот": b["Оплата, ₽"], "Итог": "разное деление (объединённое)"})
            continue
        # сначала пары с одинаковым числом людей, остальное — по порядку времени
        pairs, a_left, b_left = [], list(a_list), list(b_list)
        for a in list(a_left):
            b = next((b for b in b_left if b["Человек"] == a["people"]), None)
            if b is not None:
                pairs.append((a, b)); a_left.remove(a); b_left = [x for x in b_left if x is not b]
        for i in range(max(len(a_left), len(b_left))):
            pairs.append((a_left[i] if i < len(a_left) else None, b_left[i] if i < len(b_left) else None))
        for a, b in pairs:
            a_n, b_n = (a["people"] if a else None), (b["Человек"] if b is not None else None)
            status = "✅" if a_n == b_n else ("нет у админов" if a is None else "нет в CRM" if b is None else "❌ расходится")
            result.append({"Дата": key[0][5:], "Педагог": key[1],
                           "Админы": a_n if a_n is not None else "—", "Бот": b_n if b_n is not None else "—",
                           "₽ админы": rate_for(a_n) if a_n else 0, "₽ бот": (b["Оплата, ₽"] if b is not None else 0),
                           "Итог": status})

    df = pd.DataFrame(result).sort_values(["Дата", "Педагог"])
    money = df.groupby("Педагог")[["₽ админы", "₽ бот"]].sum().astype(int)
    money["Разница"] = money["₽ бот"] - money["₽ админы"]
    return df, money, (start, end), len(admin)


def rub(x) -> str:
    return f"{x:+,.0f} ₽".replace(",", " ") if x else "0 ₽"


def build_check_text(conn, text: str) -> str:
    df, money, (start, end), n_admin = compare(conn, text)
    ok = (df["Итог"] == "✅").sum()
    lines = [f"🔍 Сверка со сводкой админов {start:%d.%m}–{end:%d.%m}",
             f"Совпало занятий: {ok} из {len(df)} (строк в сводке {n_admin})"]
    bad = df[df["Итог"] != "✅"]
    if len(bad):
        lines += ["", "Расхождения (админы → CRM):"]
        for _, r in bad.iterrows():
            note = {"нет у админов": "нет в сводке", "нет в CRM": "нет в CRM"}.get(r["Итог"], "")
            if r["Итог"].startswith("разное"):
                note = "объединённое занятие (одно время), а в сводке посчитано как отдельные 🔗"
            money_note = "" if r["₽ админы"] == r["₽ бот"] else f", деньги {r['₽ админы']:.0f} → {r['₽ бот']:.0f} ₽"
            lines.append(f"• {r['Дата'][3:]}.{r['Дата'][:2]} {r['Педагог']}: {r['Админы']} → {r['Бот']}"
                         f"{' — ' + note if note else ''}{money_note}")
    diff = money[money["Разница"] != 0]
    lines += ["", "💰 Разница к выплате (по CRM минус по сводке):"]
    lines += [f"• {t}: {rub(r['Разница'])}" for t, r in diff.iterrows()] or ["нет"]
    total_a, total_b = money["₽ админы"].sum(), money["₽ бот"].sum()
    plain = lambda x: f"{x:,.0f} ₽".replace(",", " ")
    lines += ["", f"Итого: по сводке {plain(total_a)}, по CRM {plain(total_b)}, разница {rub(total_b - total_a)}"]
    unknown = sorted({t for t in df["Педагог"] if t not in config.TEACHER_ALIASES.values()})
    if unknown:
        lines.append(f"❓ Не узнал педагогов: {', '.join(unknown)} — допишите их в TEACHER_ALIASES")
    return "\n".join(lines)


def looks_like_summary(text: str) -> bool:
    """Похоже ли сообщение на сводку админов: есть дата и хотя бы 3 строки «Имя - N»."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    has_date = any(re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", x) for x in lines)
    return has_date and sum(bool(LINE.match(x)) for x in lines) >= 3
