"""Метрики посещаемости и статусы групп. Чистые функции: без CRM, базы и LLM.

Правила классификации занятий (согласовано со студией):
- отменено в CRM → отменено (посещения на нём, если есть, идут только в итог недели);
- 0 посещений и 0 записей → «пустое» (скорее всего ошибка расписания), не считается состоявшимся;
- посещения «вне расписания» с указанным временем → отдельное состоявшееся занятие;
- посещения без времени → только в итог недели.
"""

from collections import Counter
from dataclasses import dataclass, field

from app import config

HELD, CANCELLED, EMPTY = "held", "cancelled", "empty"

STATUS_CANCELLED = "⛔ Отменено"
STATUS_NEW = "🆕 Новая"
STATUS_DROP = "🔴 Просела"
STATUS_WATCH = "🟡 Под наблюдением"
STATUS_GROW = "🟢 Растёт"
STATUS_OK = "Норма"


@dataclass
class Session:
    group_id: int
    date: str
    minutes_begin: int | None
    kind: str                          # HELD / CANCELLED / EMPTY
    visits: Counter = field(default_factory=Counter)   # тип услуги → посещений
    booked: int = 0
    capacity: int | None = None
    off_schedule: bool = False         # восстановлено по посещениям вне расписания
    schedule_id: int | None = None     # слот расписания CRM (нет у занятий вне расписания)


@dataclass
class GroupWeek:
    group_id: int
    sessions_planned: int = 0          # по расписанию CRM
    sessions_cancelled: int = 0
    sessions_empty: int = 0
    sessions_off_schedule: int = 0
    sessions_held: int = 0             # состоялось (включая вне расписания)
    visits_total: int = 0              # на состоявшихся занятиях
    visits_subscription: int = 0
    visits_single: int = 0
    visits_other: int = 0              # необычные типы услуг
    visits_on_cancelled: int = 0
    booked: int = 0
    capacity_total: int = 0            # сумма вместимостей состоявшихся занятий
    avg_per_session: float | None = None
    fill_rate: float | None = None
    baseline: float | None = None
    change_pct: float | None = None
    history_weeks: int = 0
    status: str = STATUS_OK
    status_reason: str = ""


# ---------- занятия ----------

def classify_sessions(scheduled, visits_by_session, bookings, unmatched, capacity_of) -> list[Session]:
    """Расписание + посещения → список занятий с видом (состоялось / отменено / пустое).

    scheduled: [{schedule_id, date, group_id, hall_id, minutes_begin, cancelled}]
    visits_by_session: {(schedule_id, date): Counter(тип услуги → n)}
    bookings: {(schedule_id, date): n}
    unmatched: [{date, group_id, minutes_begin, hall_id, service_entity, visits}] — вне расписания
    capacity_of: функция (group_id, hall_id) → вместимость или None
    """
    sessions = []
    for s in scheduled:
        key = (s["schedule_id"], s["date"])
        visits = Counter(visits_by_session.get(key, {}))
        booked = bookings.get(key, 0)
        if s["cancelled"]:
            kind = CANCELLED
        elif sum(visits.values()) == 0 and booked == 0:
            kind = EMPTY
        else:
            kind = HELD
        sessions.append(Session(s["group_id"], s["date"], s["minutes_begin"], kind, visits, booked,
                                capacity_of(s["group_id"], s["hall_id"]), schedule_id=s["schedule_id"]))

    off = {}
    for u in unmatched:
        if u["minutes_begin"] is None:
            continue  # посещение без времени — занятие не восстановить
        key = (u["group_id"], u["date"], u["minutes_begin"])
        if key not in off:
            off[key] = Session(u["group_id"], u["date"], u["minutes_begin"], HELD,
                               capacity=capacity_of(u["group_id"], u["hall_id"]), off_schedule=True)
        off[key].visits[u["service_entity"]] += u["visits"]
    return sessions + list(off.values())


def aggregate_week(sessions: list[Session]) -> dict[int, GroupWeek]:
    """Занятия недели → показатели по каждой группе."""
    weeks: dict[int, GroupWeek] = {}
    for s in sessions:
        w = weeks.setdefault(s.group_id, GroupWeek(s.group_id))
        n = sum(s.visits.values())
        if s.off_schedule:
            w.sessions_off_schedule += 1
        else:
            w.sessions_planned += 1
        if s.kind == CANCELLED:
            w.sessions_cancelled += 1
            w.visits_on_cancelled += n
        elif s.kind == EMPTY:
            w.sessions_empty += 1
        else:
            w.sessions_held += 1
            w.visits_total += n
            w.visits_subscription += s.visits.get("groupAccount", 0)
            w.visits_single += s.visits.get("groupSingle", 0)
            w.visits_other += n - s.visits.get("groupAccount", 0) - s.visits.get("groupSingle", 0)
            w.booked += s.booked
            w.capacity_total += s.capacity or 0
    for w in weeks.values():
        if w.sessions_held:
            w.avg_per_session = w.visits_total / w.sessions_held
        if w.capacity_total:
            w.fill_rate = w.visits_total / w.capacity_total
    return weeks


# ---------- статусы ----------

def active(history: list[GroupWeek | None]) -> list[GroupWeek]:
    """Недели, когда группа занималась (было хотя бы одно состоявшееся занятие)."""
    return [w for w in history if w is not None and w.sessions_held > 0]


def baseline(history: list[GroupWeek | None], weeks: int = config.BASELINE_WEEKS) -> float | None:
    """Среднее «чел./занятие» за последние N недель, когда группа занималась.

    history — прошлые недели по порядку (старые → новые), без текущей.
    """
    recent = active(history)[-weeks:]
    if len(recent) < weeks:
        return None
    return sum(w.avg_per_session for w in recent) / len(recent)


def assign_status(current: GroupWeek, history: list[GroupWeek | None], cfg=config) -> GroupWeek:
    """Статус группы за неделю. history — прошлые недели (старые → новые), без текущей."""
    past = active(history)
    current.history_weeks = len(past)
    current.baseline = baseline(history, cfg.BASELINE_WEEKS)
    base = current.baseline
    avg = current.avg_per_session

    def done(status, reason=""):
        current.status, current.status_reason = status, reason
        return current

    if current.sessions_held == 0:
        if current.sessions_cancelled and not current.sessions_empty:
            return done(STATUS_CANCELLED, f"отменено занятий: {current.sessions_cancelled}")
        if current.sessions_empty and base:
            current.change_pct = -1.0
            return done(STATUS_DROP, f"никто не пришёл (пустых занятий: {current.sessions_empty}), "
                                     f"обычно {base:.1f} чел./занятие")
        return done(STATUS_NEW if len(past) < cfg.NEW_GROUP_MIN_WEEKS else STATUS_OK, "занятий не было")

    if base:
        current.change_pct = avg / base - 1

    if len(past) < cfg.NEW_GROUP_MIN_WEEKS:
        return done(STATUS_NEW, f"история {len(past)} нед.")

    if current.change_pct <= -cfg.DROP_PCT and base - avg >= cfg.DROP_MIN_PEOPLE:
        return done(STATUS_DROP, f"{base:.1f} → {avg:.1f} чел./занятие ({current.change_pct:+.0%})")

    previous = past[len(past) - (cfg.MIN_PEOPLE_WEEKS - 1):]
    if all(w.avg_per_session < cfg.MIN_PEOPLE for w in previous + [current]):
        return done(STATUS_DROP, f"меньше {cfg.MIN_PEOPLE} чел./занятие {cfg.MIN_PEOPLE_WEEKS} нед. подряд: "
                    + " → ".join(f"{w.avg_per_session:.1f}" for w in previous + [current]))

    series = [w.avg_per_session for w in past[-cfg.WATCH_DECLINE_WEEKS:]] + [avg]
    if len(series) == cfg.WATCH_DECLINE_WEEKS + 1 and all(b < a for a, b in zip(series, series[1:])):
        return done(STATUS_WATCH, "снижается " + str(cfg.WATCH_DECLINE_WEEKS) + " нед. подряд: "
                    + " → ".join(f"{x:.1f}" for x in series))

    if current.change_pct >= cfg.GROW_PCT and avg - base >= cfg.GROW_MIN_PEOPLE:
        return done(STATUS_GROW, f"{base:.1f} → {avg:.1f} чел./занятие ({current.change_pct:+.0%})")

    return done(STATUS_OK)


# ---------- полнота данных ----------

def check_completeness(week_total: int, previous_totals: list[int], holidays_in_week: list[str],
                       cfg=config) -> tuple[bool, str]:
    """Можно ли доверять данным недели. Возвращает (ок, пояснение)."""
    recent = previous_totals[-cfg.BASELINE_WEEKS:]
    if week_total == 0:
        ok, why = False, "за неделю не отмечено ни одного посещения"
    elif recent and week_total < cfg.COMPLETENESS_MIN_SHARE * (sum(recent) / len(recent)):
        avg = sum(recent) / len(recent)
        ok, why = False, f"посещений {week_total} — меньше {cfg.COMPLETENESS_MIN_SHARE:.0%} от обычных {avg:.0f}"
    else:
        return True, ""
    if holidays_in_week:
        why += f". На неделе праздники ({', '.join(holidays_in_week)}) — возможно, это нормально"
    return ok, why
