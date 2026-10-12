"""Тесты расчёта метрик и статусов. Запуск: .venv/bin/pytest"""

from collections import Counter

import pytest

from app.metrics import (CANCELLED, EMPTY, HELD, STATUS_CANCELLED, STATUS_DROP, STATUS_GROW, STATUS_NEW,
                         STATUS_OK, STATUS_WATCH, GroupWeek, aggregate_week, assign_status, baseline,
                         check_completeness, classify_sessions)


def week(avg, held=2, fill=0.6):
    """Неделя группы с заданной средней «чел./занятие»."""
    return GroupWeek(1, sessions_planned=held, sessions_held=held, visits_total=round(avg * held),
                     avg_per_session=avg, fill_rate=fill)


def status_of(current, history):
    return assign_status(current, history).status


# ---------- baseline ----------

def test_baseline_is_mean_of_last_4_active_weeks():
    history = [week(100), week(10), week(12), week(14), week(16)]
    assert baseline(history) == pytest.approx(13)


def test_baseline_skips_weeks_without_lessons():
    history = [week(10), None, week(12), GroupWeek(1, sessions_cancelled=2), week(14), week(16)]
    assert baseline(history) == pytest.approx(13)


def test_baseline_needs_4_active_weeks():
    assert baseline([week(10), week(12), week(14)]) is None


# ---------- просадка ----------

def test_drop_when_25_percent_and_3_people():
    assert status_of(week(9), [week(12)] * 4) == STATUS_DROP      # −25%, −3 чел.


def test_no_drop_on_small_group_noise():
    # 4 → 2 это −50%, но всего 2 человека: шум, не просадка
    assert status_of(week(2), [week(4)] * 4) == STATUS_OK


def test_no_drop_when_less_than_25_percent():
    assert status_of(week(16), [week(20)] * 4) == STATUS_OK      # −20%, хоть и −4 чел.


def test_drop_when_few_people_two_weeks_in_row():
    history = [week(5)] * 3 + [week(3.5)]
    assert status_of(week(3), history) == STATUS_DROP


def test_few_people_one_week_is_not_drop():
    assert status_of(week(3.5), [week(5)] * 4) == STATUS_OK


def test_low_fill_rate_alone_is_not_drop():
    # заполняемость 10% при 8 чел./занятие — нормально для зала на 50+ мест
    assert status_of(week(8, fill=0.1), [week(8, fill=0.1)] * 4) == STATUS_OK


# ---------- наблюдение и рост ----------

def test_watch_when_declines_3_weeks_in_row():
    history = [week(12), week(12), week(11.5), week(11)]
    assert status_of(week(10.5), history) == STATUS_WATCH


def test_no_watch_if_decline_interrupted():
    history = [week(12), week(11.5), week(11.8), week(11)]
    assert status_of(week(10.5), history) == STATUS_OK


def test_grow():
    assert status_of(week(15), [week(12)] * 4) == STATUS_GROW     # +25%, +3 чел.


def test_small_growth_is_ok():
    assert status_of(week(5), [week(4)] * 4) == STATUS_OK         # +25%, но +1 чел.


# ---------- новые группы и отмены ----------

def test_new_group_has_no_status():
    current = assign_status(week(3), [week(12)] * 3)
    assert current.status == STATUS_NEW
    assert current.avg_per_session == 3                           # цифры всё равно показываем


def test_all_cancelled_is_not_drop():
    current = GroupWeek(1, sessions_planned=2, sessions_cancelled=2)
    assert status_of(current, [week(12)] * 4) == STATUS_CANCELLED


def test_partly_cancelled_uses_held_sessions_only():
    # 1 занятие отменено, на втором 12 человек — средняя 12, не 6
    sessions = classify_sessions(
        [{"schedule_id": 1, "date": "2026-10-05", "group_id": 7, "hall_id": 1, "minutes_begin": 1140, "cancelled": 0},
         {"schedule_id": 2, "date": "2026-10-07", "group_id": 7, "hall_id": 1, "minutes_begin": 1140, "cancelled": 1}],
        {(1, "2026-10-05"): Counter(groupAccount=12)}, {}, [], lambda g, h: 25)
    w = aggregate_week(sessions)[7]
    assert (w.sessions_held, w.sessions_cancelled, w.avg_per_session) == (1, 1, 12)
    assert status_of(w, [week(12)] * 4) == STATUS_OK


def test_empty_lessons_of_established_group_are_drop():
    current = GroupWeek(1, sessions_planned=2, sessions_empty=2)
    assert status_of(current, [week(12)] * 4) == STATUS_DROP


# ---------- классификация занятий ----------

def test_classify_sessions():
    scheduled = [
        {"schedule_id": 1, "date": "d", "group_id": 7, "hall_id": 1, "minutes_begin": 1140, "cancelled": 0},
        {"schedule_id": 2, "date": "d", "group_id": 8, "hall_id": 1, "minutes_begin": 1200, "cancelled": 0},
        {"schedule_id": 3, "date": "d", "group_id": 9, "hall_id": 1, "minutes_begin": 1200, "cancelled": 0},
        {"schedule_id": 4, "date": "d", "group_id": 9, "hall_id": 1, "minutes_begin": 1080, "cancelled": 1},
    ]
    visits = {(1, "d"): Counter(groupAccount=10, groupSingle=4), (4, "d"): Counter(groupSingle=1)}
    bookings = {(3, "d"): 5}                                   # записались, но никто не пришёл
    unmatched = [
        {"date": "d", "group_id": 62, "minutes_begin": 1140, "hall_id": 21, "service_entity": "groupAccount", "visits": 9},
        {"date": "d", "group_id": 62, "minutes_begin": 1140, "hall_id": 21, "service_entity": "groupSingle", "visits": 2},
        {"date": "d", "group_id": 58, "minutes_begin": None, "hall_id": None, "service_entity": "groupSingle", "visits": 1},
    ]
    kinds = {(s.group_id, s.minutes_begin): (s.kind, sum(s.visits.values()), s.off_schedule)
             for s in classify_sessions(scheduled, visits, bookings, unmatched, lambda g, h: 25)}
    assert kinds == {
        (7, 1140): (HELD, 14, False),
        (8, 1200): (EMPTY, 0, False),       # 0 посещений и 0 записей → пустое
        (9, 1200): (HELD, 0, False),        # были записи → состоялось, пришло 0
        (9, 1080): (CANCELLED, 1, False),
        (62, 1140): (HELD, 11, True),       # вне расписания с временем → отдельное занятие
    }                                       # посещение без времени (группа 58) занятием не стало


def test_fill_rate_uses_capacity_of_each_session():
    sessions = classify_sessions(
        [{"schedule_id": 1, "date": "a", "group_id": 7, "hall_id": 1, "minutes_begin": 1140, "cancelled": 0},
         {"schedule_id": 2, "date": "b", "group_id": 7, "hall_id": 2, "minutes_begin": 1140, "cancelled": 0}],
        {(1, "a"): Counter(groupAccount=10), (2, "b"): Counter(groupAccount=10)}, {}, [],
        lambda g, h: {1: 50, 2: 25}[h])
    assert aggregate_week(sessions)[7].fill_rate == pytest.approx(20 / 75)


# ---------- полнота данных ----------

def test_completeness_zero_visits():
    ok, why = check_completeness(0, [200, 190, 210, 200], [])
    assert not ok and "ни одного" in why


def test_completeness_less_than_half():
    ok, _ = check_completeness(90, [200, 190, 210, 200], [])
    assert not ok


def test_completeness_normal_week():
    assert check_completeness(150, [200, 190, 210, 200], []) == (True, "")


def test_completeness_mentions_holidays():
    ok, why = check_completeness(80, [200, 190, 210, 200], ["04.11 День народного единства"])
    assert not ok and "праздники" in why


# ---------- период для /visits ----------

def test_parse_period():
    from datetime import date
    from app.lessons import parse_period
    today = date(2026, 10, 12)
    assert parse_period(None, today) == (date(2026, 9, 16), date(2026, 9, 30))       # прошлый расчётный период
    assert parse_period(None, date(2026, 10, 20)) == (date(2026, 10, 1), date(2026, 10, 15))
    assert parse_period(None, date(2026, 3, 2)) == (date(2026, 2, 16), date(2026, 2, 28))
    assert parse_period("28.09", today) == (date(2026, 9, 28), date(2026, 10, 4))
    assert parse_period("16.09–30.09", today) == (date(2026, 9, 16), date(2026, 9, 30))
    assert parse_period("сентябрь", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert parse_period("октябрь", today) == (date(2026, 10, 1), date(2026, 10, 11))   # только прошедшие дни
    with pytest.raises(ValueError):
        parse_period("01.07-30.09", today)


def test_joint_lessons_are_merged():
    import pandas as pd
    from app.lessons import merge_joint_lessons
    base = {"date": "2026-09-28", "minutes": 1200, "group_id": 1, "по абонементу": 0, "разовых": 0,
            "Записались": 0, "Статус": "состоялось", "counts": True, "joint": False}
    df = pd.DataFrame([
        {**base, "Направление": "Hip-Hop", "Вёл": "Педагог А.", "Замена за": "", "Человек": 6},
        {**base, "Направление": "Электро", "Вёл": "Педагог А.", "Замена за": "Педагог Б.", "Человек": 7},
        {**base, "Направление": "Afro", "Вёл": "Педагог В.", "Замена за": "", "Человек": 12},
    ])
    out = merge_joint_lessons(df)
    assert len(out) == 2
    joint = out[out["joint"]].iloc[0]
    assert (joint["Направление"], joint["Человек"]) == ("Hip-Hop + Электро", 13)


def test_pay_rates():
    from app.lessons import rate_for
    assert [rate_for(n) for n in (1, 3, 4, 7, 8, 10, 11, 15, 16, 20, 21, 29)] == \
        [600, 600, 1000, 1000, 1500, 1500, 2000, 2000, 3000, 3000, 4000, 4000]
    assert rate_for(0) is None and rate_for(30) is None      # ставка не задана — считаем вручную


def test_parse_admin_summary(monkeypatch):
    from datetime import date
    from app import config
    from app.admin_check import parse_admin
    monkeypatch.setattr(config, "TEACHER_ALIASES", {"Аня": "Иванова А.", "Маша": "Петрова М.", "Оля": "Сидорова О.", "Сидорова": "Сидорова О."})
    rows = parse_admin("28.09\n\nАня (замена Маша) - 4\nОля - 6 \nМаша (замена Сидорова О.С.) - 4",
                       date(2026, 10, 12))
    assert [(r["date"], r["led"], r["people"]) for r in rows] == [
        ("2026-09-28", "Петрова М.", 4),         # платим тому, кто вёл (в скобках)
        ("2026-09-28", "Сидорова О.", 6),
        ("2026-09-28", "Сидорова О.", 4),
    ]
