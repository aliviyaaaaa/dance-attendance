"""Этап 3. Пересчёт показателей и калибровка порогов на истории.

Запуск:  .venv/bin/python -m scripts.calibrate
"""

import dataclasses
from collections import Counter, defaultdict
from types import SimpleNamespace

import pandas as pd

from app import config, db
from app.attendance import loaded_weeks, recompute_all, week_totals
from app.metrics import STATUS_DROP, GroupWeek, assign_status


def config_with(**overrides):
    values = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    return SimpleNamespace(**{**values, **overrides})


def replay_statuses(stats: pd.DataFrame, cfg) -> pd.DataFrame:
    """Заново присвоить статусы с другими порогами (без записи в базу)."""
    fields = [f.name for f in dataclasses.fields(GroupWeek)]
    history, out = defaultdict(list), []
    for _, r in stats.sort_values("week_start").iterrows():
        w = GroupWeek(**{f: r[f] for f in fields if f in r and pd.notna(r[f])})
        assign_status(w, history[w.group_id], cfg)
        history[w.group_id].append(w)
        out.append({"week_start": r["week_start"], "group_id": w.group_id,
                    "status": w.status, "reason": w.status_reason})
    return pd.DataFrame(out)


def main():
    conn = db.connect()
    print(f"Пересчитано строк «группа × неделя»: {recompute_all(conn)}\n")
    stats = pd.read_sql_query(
        """SELECT w.*, g.style, h.place_count AS hall_capacity
           FROM weekly_group_stats w JOIN groups g ON g.id = w.group_id
           LEFT JOIN (SELECT group_id, MAX(hall_id) AS hall_id FROM sessions GROUP BY group_id) s
                  ON s.group_id = w.group_id
           LEFT JOIN halls h ON h.id = s.hall_id""", conn)
    held = stats[stats["sessions_held"] > 0]

    print("=== Полнота данных по неделям ===")
    for monday in loaded_weeks(conn):
        t = week_totals(conn, monday)
        print(f"  {monday:%d.%m}: посещений {t['visits_total']:>3}, занятий {t['sessions_held']:>2}, "
              f"отмен {t['sessions_cancelled']:>2}, пустых {t['sessions_empty']:>2}"
              + ("" if t["complete"] else f"  ⚠️ {t['completeness_note']}"))

    print("\n=== Заполняемость (посещения / вместимость зала), все группы-недели ===")
    bins = [0, .1, .2, .3, .4, .5, .6, 1.01]
    labels = ["0–10%", "10–20%", "20–30%", "30–40%", "40–50%", "50–60%", "60%+"]
    dist = pd.cut(held["fill_rate"], bins, labels=labels, right=False).value_counts().reindex(labels)
    for label, n in dist.items():
        print(f"  {label:>7}: {n:>3} {'█' * n}")
    print(f"  медиана {held['fill_rate'].median():.0%}, среднее {held['fill_rate'].mean():.0%}")
    print("\n  По залам (медиана заполняемости и «чел./занятие»):")
    for cap, g in held.groupby("hall_capacity"):
        print(f"    зал на {cap:.0f} мест: заполняемость {g['fill_rate'].median():.0%}, "
              f"{g['avg_per_session'].median():.1f} чел./занятие, групп-недель {len(g)}")

    weeks_with_status = sorted(stats.loc[stats["history_weeks"] >= config.NEW_GROUP_MIN_WEEKS, "week_start"].unique())
    groups_per_week = stats[stats["week_start"].isin(weeks_with_status)].groupby("week_start").size().mean()
    print(f"\n=== Сколько групп помечено 🔴 в среднем за неделю "
          f"(недели {len(weeks_with_status)}, групп в неделе ≈{groups_per_week:.0f}) ===")
    latest = stats[stats["week_start"] == stats["week_start"].max()]
    for n in (4, 5, 6):
        small = (latest["small_avg"] < n).sum()
        print(f"  🧊 маленьких при пороге {n} чел./занятие (за {config.SMALL_GROUP_WEEKS} нед.): {small}")

    print("\n=== Статусы с текущими порогами, по неделям ===")
    table = stats[stats["week_start"].isin(weeks_with_status)].pivot_table(
        index="week_start", columns="status", values="group_id", aggfunc="count", fill_value=0)
    print(table.to_string())

    print("\n=== Все 🔴 и 🟡 с текущими порогами ===")
    flagged = stats[stats["status"].str.contains("🔴|🟡")].sort_values(["week_start", "status"])
    for _, r in flagged.iterrows():
        print(f"  {r['week_start'][5:]} {r['status']:<18} {r['style']:<24} {r['status_reason']}")


if __name__ == "__main__":
    main()
