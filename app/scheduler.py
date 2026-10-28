"""Расписание: бот сам обновляет данные из CRM и сам шлёт еженедельный отчёт.

Зачем это нужно: аналитика должна работать, пока вы спите, а компьютер выключен.
Планировщик живёт внутри бота, поэтому не зависит от хостинга — Mac, Railway и VPS
работают одинаково.

Расписание (московское время, настраивается в .env):
  SYNC_HOUR / SYNC_MINUTE      — ежедневная выгрузка свежих данных из CRM;
  REPORT_WEEKDAY / REPORT_HOUR — отчёт за прошлую неделю (1 = понедельник).

Состояние (когда последний раз обновляли и отправляли отчёт) лежит в
data/scheduler_state.json — чтобы при перезапуске не слать отчёт дважды.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from aiogram.types import FSInputFile

from app import config, db, pg, sync
from app.locks import crm_lock
from app.publish import publish_to_pg
from app.report import REPORTS_DIR, build_report, last_full_week

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000
STATE_PATH = config.PROJECT_DIR / "data" / "scheduler_state.json"
WEEKDAY_NAMES = {1: "понедельник", 2: "вторник", 3: "среда", 4: "четверг",
                 5: "пятница", 6: "суббота", 7: "воскресенье"}


# ---------- время ----------

def now_local() -> datetime:
    """Текущее время в зоне расписания (по умолчанию Москва)."""
    return datetime.now(config.TZ)


def next_run(now: datetime, hour: int, minute: int = 0, weekday: int | None = None) -> datetime:
    """Ближайший момент в hour:minute.

    weekday — номер дня недели по ISO (1 = понедельник … 7 = воскресенье);
    если задан, берём только этот день недели.
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        return candidate if candidate > now else candidate + timedelta(days=1)
    candidate += timedelta(days=(weekday - candidate.isoweekday()) % 7)
    return candidate if candidate > now else candidate + timedelta(days=7)


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram не принимает длинные сообщения — режем по строкам."""
    chunks, chunk = [], ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        chunks.append(chunk)
    return chunks


# ---------- состояние между запусками ----------

def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(**changes) -> dict:
    state = read_state()
    state.update(changes)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Не удалось сохранить состояние планировщика: %s", e)
    return state


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.TZ)
    return parsed.astimezone(config.TZ)


def last_loaded_at() -> datetime | None:
    """Когда последний раз загружали данные из CRM."""
    conn = db.connect()
    try:
        value = conn.execute("SELECT MAX(loaded_at) FROM loaded_days").fetchone()[0]
    finally:
        conn.close()
    return _parse_dt(value)


def loaded_days_count() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM loaded_days").fetchone()[0]
    finally:
        conn.close()


# ---------- сами задачи ----------

async def sync_now(days: int | None = None) -> dict:
    """Выгрузить свежие данные из CRM и обновить снимок в Postgres."""
    days = config.REFRESH_DAYS if days is None else days
    if crm_lock.locked():
        log.info("Автосинхронизация пропущена: выгрузка уже идёт")
        return {}
    async with crm_lock:
        stats = await asyncio.to_thread(sync.refresh, days)
    await publish_to_pg()
    write_state(last_sync=now_local().isoformat(timespec="seconds"))
    log.info("Автосинхронизация: недель %s, посещений %s", stats.get("weeks"), stats.get("visits"))
    return stats


async def backfill_if_empty() -> bool:
    """На новом сервере базы ещё нет — заливаем историю за BACKFILL_WEEKS недель."""
    if loaded_days_count():
        return False
    log.info("Локальная база пустая — загружаю историю за %d недель из CRM", config.BACKFILL_WEEKS)
    async with crm_lock:
        stats = await asyncio.to_thread(sync.backfill, config.BACKFILL_WEEKS)
    await publish_to_pg()
    write_state(last_sync=now_local().isoformat(timespec="seconds"))
    log.info("История загружена: %s", stats)
    return True


async def send_report(bot, monday: date | None = None) -> str:
    """Отчёт за прошлую неделю — всем, кто в белом списке."""
    monday = monday or last_full_week()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    try:
        text, excel = await asyncio.to_thread(build_report, conn, monday)
    finally:
        conn.close()
    for chat_id in sorted(config.TELEGRAM_ALLOWED_USER_IDS):
        try:
            for chunk in split_text(text):
                await bot.send_message(chat_id, chunk)
            if excel:
                await bot.send_document(chat_id, FSInputFile(excel))
        except Exception as e:
            log.warning("Отчёт не доставлен пользователю %s: %s", chat_id, e)
    write_state(last_report=now_local().isoformat(timespec="seconds"),
                last_report_week=monday.isoformat())
    log.info("Еженедельный отчёт за %s отправлен", monday)
    return monday.isoformat()


# ---------- сводка для /status ----------

def pg_status() -> str:
    if not pg.enabled():
        return "не подключена (нет DATABASE_URL)"
    try:
        info = pg.check()
    except Exception as e:
        return f"недоступна ({type(e).__name__})"
    day = info.get("last_loaded_day") or "—"
    return f"подключена, групп {info.get('groups', 0)}, данные на {day}"


def status_lines() -> list[str]:
    """Чем бот занят сам по себе и когда что будет — для команды /status."""
    now = now_local()
    lines = [f"🟢 Агент на связи — работаю сам, без вашей помощи. Сейчас {now:%d.%m %H:%M} МСК"]
    loaded = last_loaded_at()
    lines.append(f"🗄 Данные из CRM: {loaded:%d.%m %H:%M} МСК" if loaded
                 else "🗄 Данные из CRM: ещё не загружались")
    nxt_sync = next_run(now, config.SYNC_HOUR, config.SYNC_MINUTE)
    nxt_report = next_run(now, config.REPORT_HOUR, config.REPORT_MINUTE, config.REPORT_WEEKDAY)
    lines.append(f"♻️ Следующая автосинхронизация: {nxt_sync:%d.%m %H:%M}")
    day_name = WEEKDAY_NAMES.get(config.REPORT_WEEKDAY, "—")
    lines.append(f"📬 Следующий отчёт: {nxt_report:%d.%m %H:%M} ({day_name})")
    lines.append(f"☁️ Облачная база: {pg_status()}")
    return lines


# ---------- фоновый цикл ----------

async def startup(bot) -> None:
    """При запуске: долить историю, обновить протухшее, отправить пропущенный отчёт."""
    try:
        await backfill_if_empty()
    except Exception:
        log.exception("Не удалось загрузить историю из CRM")

    state = read_state()
    last_sync = _parse_dt(state.get("last_sync"))
    if last_sync is None or (now_local() - last_sync).total_seconds() > config.SYNC_STALE_HOURS * 3600:
        try:
            await sync_now()
        except Exception:
            log.exception("Стартовое обновление из CRM не удалось")

    now = now_local()
    monday = last_full_week()
    if now.isoweekday() == config.REPORT_WEEKDAY and now.hour >= config.REPORT_HOUR:
        if state.get("last_report_week") != monday.isoformat():
            try:
                await send_report(bot, monday)
            except Exception:
                log.exception("Пропущенный еженедельный отчёт не отправлен")


async def run(bot) -> None:
    """Фоновый цикл планировщика: автосинхронизация и еженедельный отчёт."""
    await startup(bot)
    while True:
        now = now_local()
        nxt_sync = next_run(now, config.SYNC_HOUR, config.SYNC_MINUTE)
        nxt_report = next_run(now, config.REPORT_HOUR, config.REPORT_MINUTE, config.REPORT_WEEKDAY)
        if nxt_sync <= nxt_report:
            wait = (nxt_sync - now).total_seconds()
            log.info("Следующая автосинхронизация — %s (через %d ч %d мин)",
                     nxt_sync.strftime("%d.%m %H:%M"), int(wait // 3600), int(wait % 3600 // 60))
            await asyncio.sleep(max(1.0, wait))
            try:
                await sync_now()
            except Exception:
                log.exception("Автосинхронизация не удалась")
        else:
            wait = (nxt_report - now).total_seconds()
            log.info("Следующий еженедельный отчёт — %s", nxt_report.strftime("%d.%m %H:%M"))
            await asyncio.sleep(max(1.0, wait))
            try:
                await send_report(bot)
            except Exception:
                log.exception("Еженедельный отчёт не отправлен")
