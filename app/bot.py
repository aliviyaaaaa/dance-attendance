"""Telegram-бот: кнопки, команды, белый список, отправка отчётов.

Запуск:  .venv/bin/python -m app.bot
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

from app import config, db, llm, pg, sync
from app.admin_check import build_check_text, looks_like_summary, parse_admin
from app.crm_client import CrmError
from app.lessons import build_lessons_report, parse_period, recent_pay_periods
from app.metrics import STATUS_CANCELLED, STATUS_DROP, STATUS_GROW, STATUS_NEW, STATUS_OK, STATUS_WATCH
from app.report import build_report, fmt_num, last_full_week, parse_week_arg, week_stats

log = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
TELEGRAM_LIMIT = 4000

# Кнопки под полем ввода
BTN_REPORT = "📊 Отчёт"
BTN_GROUPS = "👥 Группы"
BTN_PAY = "💰 Зарплата"
BTN_REFRESH = "🔄 Обновить"
BTN_CHECK = "🔍 Сверка"
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_GROUPS)],
              [KeyboardButton(text=BTN_PAY), KeyboardButton(text=BTN_CHECK)],
              [KeyboardButton(text=BTN_REFRESH)]],
    resize_keyboard=True, is_persistent=True)

COMMANDS = [
    BotCommand(command="report", description="Отчёт о посещаемости за неделю"),
    BotCommand(command="groups", description="Группы и их статусы"),
    BotCommand(command="visits", description="Посещения по занятиям и зарплата"),
    BotCommand(command="check", description="Сверка сводки админов с CRM"),
    BotCommand(command="refresh", description="Обновить данные из CRM"),
    BotCommand(command="db", description="Данные из облачной базы Postgres"),
    BotCommand(command="start", description="Справка"),
]

HELP = f"""Я считаю посещаемость групп студии по данным Impulse CRM. Пользуйтесь кнопками внизу:

{BTN_REPORT} — отчёт о посещаемости за неделю (выберете какую)
{BTN_GROUPS} — все группы и их статусы за прошлую неделю
{BTN_PAY} — посещения по каждому занятию и зарплата педагогов за расчётный период
{BTN_CHECK} — сверка сводки админов с CRM
{BTN_REFRESH} — подтянуть свежие данные из CRM (последние {config.REFRESH_DAYS} дней)

Можно задать вопрос своими словами: «почему просел Afro?», «как менялся Hip-Hop за 2 месяца?»,
«какие группы растут?» — отвечу по посчитанным данным (если подключён ИИ).

Команды тоже работают: /report 28.09, /visits 16.09-30.09, /visits сентябрь.
Внизу каждого ответа — время, на которое загружены данные."""

CHECK_HELP = ("Скопируйте сводку админов и пришлите её мне обычным сообщением — я сам пойму, "
              "что это сводка, и сверю с CRM. Например:\n\n16.09\nАня - 5\nМаша (замена Оля) - 4\nЛена - 10")

dp = Dispatcher()
crm_lock = asyncio.Lock()   # не запускать две выгрузки из CRM одновременно


# ---------- белый список ----------

def is_allowed(event) -> bool:
    return event.from_user is not None and event.from_user.id in config.TELEGRAM_ALLOWED_USER_IDS


@dp.message(~F.func(is_allowed))
async def not_allowed(message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not config.TELEGRAM_ALLOWED_USER_IDS and message.text == "/start":
        # Режим первой настройки: белый список пуст — показываем ID, чтобы его можно было вписать
        log.info("Настройка: /start от пользователя id=%s", user_id)
        await message.answer(f"Бот ещё не настроен.\nВаш Telegram ID: {user_id}")
    else:
        log.info("Игнорирую сообщение от пользователя не из белого списка id=%s", user_id)


@dp.callback_query(~F.func(is_allowed))
async def not_allowed_button(callback: CallbackQuery):
    log.info("Игнорирую нажатие кнопки от пользователя не из белого списка id=%s", callback.from_user.id)
    await callback.answer()


# ---------- вспомогательное ----------

def with_db(func, *args):
    conn = db.connect()
    try:
        return func(conn, *args)
    finally:
        conn.close()


def data_loaded_at() -> str:
    value = with_db(lambda conn: conn.execute("SELECT MAX(loaded_at) FROM loaded_days").fetchone()[0])
    if not value:
        return "данные ещё не загружались"
    loaded = datetime.fromisoformat(value).replace(tzinfo=UTC).astimezone(MOSCOW)
    return f"данные из CRM на {loaded:%d.%m %H:%M} МСК"


async def send_long(message: Message, text: str):
    """Telegram принимает до 4096 символов — длинный текст режем по строкам."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > TELEGRAM_LIMIT:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, reply_markup=KEYBOARD)


async def send_with_file(message: Message, text: str, excel: str | None):
    await send_long(message, f"{text}\n\n🕒 {await asyncio.to_thread(data_loaded_at)}")
    if excel:
        await message.answer_document(FSInputFile(excel))


async def publish_to_pg():
    """Выложить снимок данных в Postgres — оттуда читает дашборд.

    Postgres не обязателен: если его нет или он недоступен, бот работает как обычно.
    """
    if not pg.enabled():
        return
    try:
        stats = await asyncio.to_thread(pg.publish)
        log.info("Postgres обновлён: %s", stats)
    except Exception as e:
        log.warning("Postgres не обновился (%s) — бот продолжает работать на SQLite", e)


# ---------- действия (их вызывают и команды, и кнопки) ----------

async def do_report(message: Message, monday: date):
    await message.answer(f"Собираю отчёт за неделю {monday:%d.%m}…")
    await send_with_file(message, *await asyncio.to_thread(with_db, build_report, monday))
    await publish_to_pg()


async def reload_period(message: Message, start: date, end: date):
    """Перед расчётом зарплаты и сверкой — заново загрузить период из CRM:
    админы отмечают посещения задним числом, иногда спустя две недели."""
    mondays, monday = [], sync.monday_of(start)
    while monday <= min(end, date.today()):
        mondays.append(monday)
        monday += timedelta(weeks=1)
    if crm_lock.locked():
        await message.answer("Сейчас уже идёт обновление из CRM, считаю по загруженным данным.")
        return
    async with crm_lock:
        try:
            await asyncio.to_thread(sync.sync_weeks, mondays)
        except CrmError as e:
            log.error("%s", e)
            await message.answer(f"⚠️ Не удалось обновить данные из CRM ({e.problem}), "
                                 "считаю по последним загруженным.")


async def do_visits(message: Message, start: date, end: date):
    await message.answer(f"Обновляю данные из CRM за {start:%d.%m}–{end:%d.%m} и считаю зарплату, "
                         "это около полуминуты…")
    await reload_period(message, start, end)
    await send_with_file(message, *await asyncio.to_thread(with_db, build_lessons_report, start, end))


async def do_groups(message: Message):
    monday = last_full_week()
    stats = await asyncio.to_thread(with_db, week_stats, monday)
    if stats.empty:
        await message.answer(f"Данных за прошлую неделю нет. Нажмите «{BTN_REFRESH}».")
        return
    lines = [f"Группы на неделе {monday:%d.%m} (чел./занятие):"]
    for status in (STATUS_DROP, STATUS_WATCH, STATUS_GROW, STATUS_OK, STATUS_NEW, STATUS_CANCELLED):
        rows = stats[stats["status"] == status]
        if rows.empty:
            continue
        lines += ["", status]
        lines += [f"• {r['Группа']} — {fmt_num(r['avg_per_session'])}{' 🧊' if r['small'] else ''}"
                  for _, r in rows.iterrows()]
    lines += ["", f"🧊 — маленькая группа, стоит греть (в среднем меньше {config.SMALL_GROUP_PEOPLE} чел. "
                  f"за {config.SMALL_GROUP_WEEKS} нед.)"]
    await send_long(message, "\n".join(lines))


async def do_refresh(message: Message):
    if crm_lock.locked():
        await message.answer("Обновление уже идёт, подождите.")
        return
    async with crm_lock:
        await message.answer(f"Обновляю данные за последние {config.REFRESH_DAYS} дней из CRM, это около минуты…")
        stats = await asyncio.to_thread(sync.refresh, config.REFRESH_DAYS)
    await publish_to_pg()
    await message.answer(f"Готово: занятий {stats.get('sessions', 0)}, посещений {stats.get('visits', 0)}, "
                         f"недель {stats.get('weeks', 0)}.", reply_markup=KEYBOARD)


async def do_check(message: Message, text: str):
    try:
        days = [date.fromisoformat(r["date"]) for r in parse_admin(text, date.today())]
        if not days:
            raise ValueError("в сводке не нашлось ни одной строки вида «Имя - N»")
    except ValueError as e:
        await message.answer(f"Не получилось разобрать сводку: {e}")
        return
    await message.answer("Обновляю данные из CRM за период сводки и сверяю, это около полуминуты…")
    await reload_period(message, min(days), max(days))
    result = await asyncio.to_thread(with_db, build_check_text, text)
    await send_long(message, f"{result}\n\n🕒 {await asyncio.to_thread(data_loaded_at)}")


# ---------- выбор периода кнопками ----------

def weeks_keyboard(count: int = 4) -> InlineKeyboardMarkup:
    monday = last_full_week()
    weeks = [monday - timedelta(weeks=i) for i in range(count)]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{m:%d.%m}–{m + timedelta(days=6):%d.%m}" + (" (прошлая)" if i == 0 else ""),
                              callback_data=f"r:{m.isoformat()}")]
        for i, m in enumerate(weeks)])


def periods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{a:%d.%m}–{b:%d.%m}" + (" (идёт сейчас)" if current else ""),
                              callback_data=f"v:{a.isoformat()}:{b.isoformat()}")]
        for a, b, current in recent_pay_periods(date.today())])


@dp.callback_query(F.data.startswith("r:"))
async def on_week_button(callback: CallbackQuery):
    await callback.answer()
    await do_report(callback.message, date.fromisoformat(callback.data[2:]))


@dp.callback_query(F.data.startswith("v:"))
async def on_period_button(callback: CallbackQuery):
    await callback.answer()
    _, start, end = callback.data.split(":")
    await do_visits(callback.message, date.fromisoformat(start), date.fromisoformat(end))


# ---------- кнопки под полем ввода ----------

@dp.message(F.text == BTN_REPORT)
async def btn_report(message: Message):
    await message.answer("За какую неделю?", reply_markup=weeks_keyboard())


@dp.message(F.text == BTN_PAY)
async def btn_pay(message: Message):
    await message.answer("За какой расчётный период?", reply_markup=periods_keyboard())


@dp.message(F.text == BTN_GROUPS)
async def btn_groups(message: Message):
    await do_groups(message)


@dp.message(F.text == BTN_REFRESH)
async def btn_refresh(message: Message):
    await do_refresh(message)


@dp.message(F.text == BTN_CHECK)
async def btn_check(message: Message):
    await message.answer(CHECK_HELP)


# ---------- команды ----------

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(HELP, reply_markup=KEYBOARD)


@dp.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("За какую неделю?", reply_markup=weeks_keyboard())
        return
    try:
        monday = parse_week_arg(command.args)
    except ValueError as e:
        await message.answer(str(e))
        return
    await do_report(message, monday)


@dp.message(Command("visits"))
async def cmd_visits(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("За какой расчётный период?", reply_markup=periods_keyboard())
        return
    try:
        start, end = parse_period(command.args)
    except ValueError as e:
        await message.answer(str(e))
        return
    await do_visits(message, start, end)


@dp.message(Command("groups"))
async def cmd_groups(message: Message):
    await do_groups(message)


@dp.message(Command("refresh"))
async def cmd_refresh(message: Message):
    await do_refresh(message)


@dp.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(CHECK_HELP)
        return
    await do_check(message, command.args)


@dp.message(Command("db"))
async def cmd_db(message: Message):
    """Показать данные из облачной базы Postgres."""
    if not pg.enabled():
        await message.answer("Облачная база не подключена: в .env нет DATABASE_URL.")
        return
    await message.answer("Читаю из облачной базы…")
    try:
        text = await asyncio.to_thread(pg.dashboard_summary)
    except Exception as e:
        log.warning("Postgres не прочитан: %s", e)
        await message.answer(f"Не смог прочитать облачную базу: {e}")
        return
    await message.answer(text)


# ---------- обычный текст ----------

@dp.message(F.text.func(looks_like_summary))
async def summary_text(message: Message):
    await do_check(message, message.text)


@dp.message(F.text)
async def question_text(message: Message):
    if not llm.enabled():
        await message.answer("Отвечать на вопросы я пока не умею: не подключён ИИ. "
                             "Пользуйтесь кнопками внизу.", reply_markup=KEYBOARD)
        return
    await message.answer("Смотрю данные…")
    answer, _ = await asyncio.to_thread(llm.ask, message.text)
    await send_long(message, answer)


@dp.message()
async def other_message(message: Message):
    await message.answer("Я понимаю текст, кнопки и команды.", reply_markup=KEYBOARD)


@dp.errors()
async def on_error(event):
    """Любая ошибка — понятное сообщение пользователю, подробности — в лог."""
    error = event.exception
    update = event.update
    message = update.message or (update.callback_query.message if update.callback_query else None)
    if isinstance(error, CrmError):
        text = f"❌ {error}"
        log.error("%s", error)
    else:
        text = f"❌ Что-то пошло не так ({type(error).__name__}). Подробности в логе."
        log.exception("Ошибка при обработке", exc_info=error)
    if message:
        await message.answer(text)
    return True


async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("В .env не задан TELEGRAM_BOT_TOKEN")
    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    log.info("Бот @%s запущен. В белом списке: %d пользователь(ей)", me.username,
             len(config.TELEGRAM_ALLOWED_USER_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
