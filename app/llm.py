"""ИИ-часть бота: комментарий к отчёту и ответы на вопросы о посещаемости.

Принципы:
- все цифры считает код; модель только читает их через инструменты и пересказывает;
- в модель уходят только агрегаты по группам (без данных клиентов), педагоги — «Имя Ф.»;
- если ключа нет, бот работает без ИИ (enabled() == False).

Провайдер — любой OpenAI-совместимый API; по умолчанию Yandex AI Studio.
"""

import json
import logging
import time
from datetime import UTC, date, datetime, timedelta

from app import config, db
from app.attendance import loaded_weeks, week_totals
from app.report import (group_labels, last_full_week, parse_date, recent_series,
                        week_changes, week_of, week_stats)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — аналитик посещаемости танцевальной студии. Отвечай по-русски, кратко (до 8 предложений).
Правила:
1. Цифры бери только из результатов инструментов. Не выдумывай и не округляй сильнее одного знака.
2. Чтобы найти группу по названию или педагогу, сначала вызови list_groups с query.
3. Разделяй: «По данным: …» — факты; «Возможная причина: …» — гипотезы.
4. Если данных нет (выручка, причины ухода конкретного клиента, личные данные) — прямо скажи, что у бота этих данных нет.
5. «Чел./занятие» — среднее число людей на одном состоявшемся занятии за неделю. Недели — с понедельника.
Сегодня {today}. Последняя полная неделя начинается {last_week}."""

COMMENT_PROMPT = """Ниже цифры отчёта о посещаемости за неделю (JSON). Напиши комментарий из 3–5 предложений:
что бросается в глаза и какие гипотезы стоит проверить. Используй только эти цифры.
Факты начинай с «По данным», гипотезы — с «Возможно». Без приветствий и списков.

{facts}"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_groups",
        "description": "Список групп с id, названием (направление · педагог · время), статусом и чел./занятие "
                       "за последнюю полную неделю. query — поиск по названию направления или имени педагога.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Часть названия направления или имени педагога, необязательно"},
        }}}},
    {"type": "function", "function": {
        "name": "get_week_summary",
        "description": "Итоги недели по студии и по каждой группе: посещения, занятия, статусы.",
        "parameters": {"type": "object", "properties": {
            "week": {"type": "string", "description": "Любая дата недели: ДД.ММ, ДД.ММ.ГГГГ или ГГГГ-ММ-ДД"},
        }, "required": ["week"]}}},
    {"type": "function", "function": {
        "name": "get_group_history",
        "description": "История группы по неделям: чел./занятие, посещения, занятия, отмены, статус.",
        "parameters": {"type": "object", "properties": {
            "group_id": {"type": "integer", "description": "id группы из list_groups"},
            "weeks": {"type": "integer", "description": "Сколько последних недель, по умолчанию 8, максимум 52"},
        }, "required": ["group_id"]}}},
    {"type": "function", "function": {
        "name": "compare_weeks",
        "description": "Сравнить две недели: итоги студии и чел./занятие по каждой группе.",
        "parameters": {"type": "object", "properties": {
            "week_a": {"type": "string", "description": "Дата первой недели"},
            "week_b": {"type": "string", "description": "Дата второй недели"},
        }, "required": ["week_a", "week_b"]}}},
    {"type": "function", "function": {
        "name": "get_cancellations",
        "description": "Отмены, замены педагогов и переносы занятий за неделю.",
        "parameters": {"type": "object", "properties": {
            "week": {"type": "string", "description": "Любая дата недели"},
        }, "required": ["week"]}}},
]


class ToolError(Exception):
    """Ошибка в аргументах инструмента — возвращается модели, чтобы она повторила вызов."""


def enabled() -> bool:
    if not config.LLM_API_KEY or not config.LLM_BASE_URL:
        return False
    return config.LLM_PROVIDER != "yandex" or bool(config.LLM_FOLDER_ID)


def model_id() -> str:
    if config.LLM_PROVIDER == "yandex":
        return f"gpt://{config.LLM_FOLDER_ID}/{config.LLM_MODEL}"
    return config.LLM_MODEL


def make_client():
    from openai import OpenAI
    kwargs = {"api_key": config.LLM_API_KEY, "base_url": config.LLM_BASE_URL, "timeout": config.LLM_TIMEOUT_SEC}
    if config.LLM_PROVIDER == "yandex":
        kwargs["project"] = config.LLM_FOLDER_ID          # каталог Yandex Cloud
    return OpenAI(**kwargs)


# ---------- инструменты: читают уже посчитанные данные из базы ----------

def _week(conn, value) -> date:
    if not value or not isinstance(value, str):
        raise ToolError("укажи неделю датой, например 05.10 или 2026-10-05")
    try:
        d = date.fromisoformat(value) if "-" in value else parse_date(value)
    except ValueError:
        raise ToolError(f"не понял дату «{value}», формат ДД.ММ или ГГГГ-ММ-ДД")
    monday = week_of(d)
    if monday not in loaded_weeks(conn):
        weeks = loaded_weeks(conn)
        raise ToolError(f"за неделю {monday:%d.%m.%Y} данных нет; есть недели с {weeks[0]:%d.%m.%Y} "
                        f"по {weeks[-1]:%d.%m.%Y}")
    return monday


def _num(x, digits=1):
    return None if x is None or x != x else round(float(x), digits)


def _group_rows(stats) -> list[dict]:
    return [{
        "group_id": int(r["group_id"]), "группа": r["Группа"], "статус": r["status"],
        "пояснение": r["status_reason"] or None,
        "чел_на_занятие": _num(r["avg_per_session"]), "обычный_уровень": _num(r["baseline"]),
        "изменение_проц": None if r["change_pct"] != r["change_pct"] or r["change_pct"] is None
        else round(r["change_pct"] * 100),
        "посещений": int(r["visits_total"]), "занятий_состоялось": int(r["sessions_held"]),
        "отменено": int(r["sessions_cancelled"]), "маленькая_греть": bool(r["small"]),
    } for _, r in stats.iterrows()]


def normalize_query(query: str) -> str:
    """«Хип-хоп» → «hip hop»: направления в CRM записаны латиницей, а спрашивают по-русски."""
    q = " ".join(query.lower().replace("-", " ").replace("ё", "е").split())
    for ru, crm in sorted(config.STYLE_SYNONYMS.items(), key=lambda x: -len(x[0])):
        if ru.replace("ё", "е") in q:
            return crm
    return q


def tool_list_groups(conn, query: str | None = None) -> dict:
    monday = last_full_week()
    stats = week_stats(conn, monday)
    rows = [{"group_id": g["group_id"], "группа": g["группа"], "статус": g["статус"],
             "чел_на_занятие": g["чел_на_занятие"], "маленькая_греть": g["маленькая_греть"]}
            for g in _group_rows(stats)]
    if query:
        q = normalize_query(query)
        rows = [r for r in rows if q in r["группа"].lower().replace("-", " ")]
        if not rows:
            # группа могла не заниматься на прошлой неделе — ищем по всем группам
            rows = [{"group_id": gid, "группа": label, "статус": "нет занятий на прошлой неделе"}
                    for gid, label in group_labels(conn).items()
                    if q in label.lower().replace("-", " ")][:20]
    return {"неделя": f"{monday:%d.%m.%Y}", "группы": rows}


def tool_get_week_summary(conn, week: str) -> dict:
    monday = _week(conn, week)
    t = week_totals(conn, monday)
    return {
        "неделя": f"{monday:%d.%m}–{monday + timedelta(days=6):%d.%m.%Y}",
        "посещений": t["visits_total"], "по_абонементу": t["visits_subscription"], "разовых": t["visits_single"],
        "среднее_посещений_за_4_прошлые_недели": _num(t["avg_previous"], 0),
        "занятий_состоялось": t["sessions_held"], "отменено": t["sessions_cancelled"],
        "праздники": t["holidays"] or None,
        "данные_полные": t["complete"], "комментарий_к_полноте": t["completeness_note"] or None,
        "группы": _group_rows(week_stats(conn, monday)),
    }


def tool_get_group_history(conn, group_id, weeks=8) -> dict:
    try:
        group_id, weeks = int(group_id), min(max(int(weeks or 8), 1), 52)
    except (TypeError, ValueError):
        raise ToolError("group_id и weeks должны быть числами; group_id возьми из list_groups")
    labels = group_labels(conn)
    if group_id not in labels:
        raise ToolError(f"группы с id {group_id} нет; вызови list_groups, чтобы найти id")
    rows = conn.execute(
        """SELECT week_start, avg_per_session, visits_total, sessions_held, sessions_cancelled, status
           FROM weekly_group_stats WHERE group_id = ? ORDER BY week_start DESC LIMIT ?""",
        (group_id, weeks)).fetchall()
    return {"группа": labels[group_id], "недели": [
        {"неделя": f"{date.fromisoformat(r['week_start']):%d.%m.%Y}", "чел_на_занятие": _num(r["avg_per_session"]),
         "посещений": r["visits_total"], "занятий": r["sessions_held"], "отменено": r["sessions_cancelled"],
         "статус": r["status"]} for r in reversed(rows)]}


def tool_compare_weeks(conn, week_a: str, week_b: str) -> dict:
    a, b = _week(conn, week_a), _week(conn, week_b)
    ta, tb = week_totals(conn, a), week_totals(conn, b)
    sa = {r["group_id"]: r for r in _group_rows(week_stats(conn, a))}
    sb = {r["group_id"]: r for r in _group_rows(week_stats(conn, b))}
    labels = group_labels(conn)
    return {
        "неделя_a": f"{a:%d.%m.%Y}", "неделя_b": f"{b:%d.%m.%Y}",
        "посещений_a": ta["visits_total"], "посещений_b": tb["visits_total"],
        "группы": [{"группа": labels.get(g, str(g)),
                    "чел_на_занятие_a": sa.get(g, {}).get("чел_на_занятие"),
                    "чел_на_занятие_b": sb.get(g, {}).get("чел_на_занятие")}
                   for g in sorted(set(sa) | set(sb), key=lambda g: labels.get(g, ""))],
    }


def tool_get_cancellations(conn, week: str) -> dict:
    monday = _week(conn, week)
    names = {"cancel": "отмена", "substitute": "замена", "transfer": "перенос"}
    return {"неделя": f"{monday:%d.%m.%Y}", "изменения": [
        {"дата": f"{date.fromisoformat(c['date']):%d.%m}", "что": names.get(c["entity"], c["entity"]),
         "группа": c["Группа"], "вела": c["teacher"] if c["entity"] == "substitute" else None}
        for _, c in week_changes(conn, monday).iterrows()]}


TOOL_FUNCS = {
    "list_groups": tool_list_groups, "get_week_summary": tool_get_week_summary,
    "get_group_history": tool_get_group_history, "compare_weeks": tool_compare_weeks,
    "get_cancellations": tool_get_cancellations,
}


def run_tool(conn, name: str, arguments: str) -> str:
    """Вызвать инструмент. Ошибки в аргументах возвращаются модели текстом — она повторит вызов."""
    if name not in TOOL_FUNCS:
        return json.dumps({"ошибка": f"нет инструмента {name}; доступны: {', '.join(TOOL_FUNCS)}"},
                          ensure_ascii=False)
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return json.dumps({"ошибка": "аргументы должны быть JSON-объектом, повтори вызов"}, ensure_ascii=False)
    try:
        result = TOOL_FUNCS[name](conn, **args)
    except ToolError as e:
        result = {"ошибка": f"{e}. Исправь аргументы и повтори вызов"}
    except TypeError:
        result = {"ошибка": f"неверные параметры для {name}; смотри описание инструмента и повтори вызов"}
    return json.dumps(result, ensure_ascii=False, default=str)


# ---------- вопрос → инструменты → ответ ----------

def _system_prompt() -> str:
    return SYSTEM_PROMPT.format(today=f"{date.today():%d.%m.%Y}", last_week=f"{last_full_week():%d.%m.%Y}")


def ask(question: str, client=None) -> tuple[str, dict]:
    """Ответ на вопрос о посещаемости. Возвращает (текст ответа, сведения для журнала)."""
    client = client or make_client()
    started = time.monotonic()
    meta = {"tools": [], "rounds": 0, "error": None}
    messages = [{"role": "system", "content": _system_prompt()}, {"role": "user", "content": question}]
    conn = db.connect()
    answer = ""
    try:
        for round_no in range(config.LLM_MAX_TOOL_ROUNDS + 1):
            last_round = round_no == config.LLM_MAX_TOOL_ROUNDS
            resp = client.chat.completions.create(
                model=model_id(), messages=messages, temperature=config.LLM_TEMPERATURE,
                **({} if last_round else {"tools": TOOLS, "tool_choice": "auto"}))
            msg = resp.choices[0].message
            meta["rounds"] = round_no + 1
            if not msg.tool_calls or last_round:
                answer = (msg.content or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls]})
            for call in msg.tool_calls:
                meta["tools"].append({"name": call.function.name, "args": call.function.arguments})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": run_tool(conn, call.function.name, call.function.arguments)})
        if not answer:
            answer = "Не получилось сформулировать ответ. Попробуйте спросить иначе."
            meta["error"] = "пустой ответ модели"
    except Exception as e:                       # сеть, ключ, лимиты провайдера
        log.error("LLM: ошибка при ответе на вопрос: %s", type(e).__name__)
        meta["error"] = type(e).__name__
        answer = "❌ ИИ сейчас недоступен. Команды и отчёты работают как обычно."
    finally:
        meta["duration_ms"] = int((time.monotonic() - started) * 1000)
        log_question(conn, question, answer, meta)
        conn.close()
    return answer, meta


def log_question(conn, question: str, answer: str, meta: dict):
    with conn:
        conn.execute(
            """INSERT INTO questions (asked_at, model, question, tools, rounds, answer_len, error, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(UTC).isoformat(timespec="seconds"), config.LLM_MODEL, question,
             json.dumps(meta["tools"], ensure_ascii=False), meta["rounds"], len(answer), meta["error"],
             meta.get("duration_ms")))


# ---------- комментарий к отчёту ----------

def report_facts(conn, monday: date) -> dict:
    """Короткая выжимка отчёта для комментария: только агрегаты."""
    summary = tool_get_week_summary(conn, monday.isoformat())
    series = recent_series(conn, monday, config.SMALL_GROUP_WEEKS)
    interesting = [g for g in summary["группы"] if g["статус"] != "Норма" or g["маленькая_греть"]]
    for g in interesting:
        g["динамика_чел_на_занятие"] = series.get(g["group_id"])
        g.pop("group_id")
    summary["группы"] = interesting
    summary["отмены_и_замены"] = tool_get_cancellations(conn, monday.isoformat())["изменения"]
    return summary


def comment(conn, monday: date, client=None) -> str | None:
    """Комментарий ИИ к отчёту за неделю. None — если ИИ выключен или недоступен."""
    if not enabled() and client is None:
        return None
    try:
        client = client or make_client()
        facts = json.dumps(report_facts(conn, monday), ensure_ascii=False, default=str)
        resp = client.chat.completions.create(
            model=model_id(), temperature=config.LLM_TEMPERATURE,
            messages=[{"role": "user", "content": COMMENT_PROMPT.format(facts=facts)}])
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        log.error("LLM: не удалось получить комментарий: %s", type(e).__name__)
        return None

