"""Настройки проекта. Секреты читаются из .env, пороги задаются здесь."""

import json
import os
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")

# --- Impulse CRM ---
IMPULSE_BASE_URL = os.getenv("IMPULSE_BASE_URL", "").rstrip("/")
IMPULSE_LOGIN = os.getenv("IMPULSE_LOGIN", "")
IMPULSE_PASSWORD = os.getenv("IMPULSE_PASSWORD", "")
IMPULSE_API_KEY = os.getenv("IMPULSE_API_KEY", "").strip()   # официальный вход; если задан — логин и пароль не нужны
BRANCH_ID = 1

# Сетевые параметры: таймаут, повторы и пауза между запросами, чтобы не нагружать CRM
HTTP_TIMEOUT_SEC = 30
HTTP_RETRIES = 3
HTTP_RETRY_PAUSE_SEC = 5
REQUEST_PAUSE_SEC = 0.5

# --- Пороги статусов (меняйте здесь) ---
BASELINE_WEEKS = 4            # baseline = среднее «чел./занятие» за столько последних недель с занятиями
NEW_GROUP_MIN_WEEKS = 4       # меньше недель истории → 🆕 новая, статус не присваиваем
DROP_PCT = 0.25               # 🔴 просела: ниже baseline на 25% и больше…
DROP_MIN_PEOPLE = 3           # …и при этом минимум на 3 человека на занятие
GROW_PCT = 0.25               # 🟢 растёт: выше baseline на 25% и больше…
GROW_MIN_PEOPLE = 3           # …и минимум на 3 человека на занятие
SMALL_GROUP_PEOPLE = 5        # 🧊 маленькая (стоит греть): в среднем меньше 5 чел./занятие…
SMALL_GROUP_WEEKS = 4         # …за последние 4 недели с занятиями (заполняемость — только для справки:
                              #    вместимость залов 25–50 мест, обычная группа — 5–8 человек)
WATCH_DECLINE_WEEKS = 3       # 🟡 «чел./занятие» снижается столько недель подряд
COMPLETENESS_MIN_SHARE = 0.5  # посещений за неделю < 50% от среднего за 4 недели → данные неполные

# --- Праздники и нерабочие дни (используются в проверке полноты данных) ---
# Сверьте с производственным календарём и добавьте дни, когда студия закрыта.
HOLIDAYS = {
    "2026-11-04": "День народного единства",
    "2026-12-31": "31 декабря",
    "2027-01-01": "Новогодние каникулы", "2027-01-02": "Новогодние каникулы",
    "2027-01-03": "Новогодние каникулы", "2027-01-04": "Новогодние каникулы",
    "2027-01-05": "Новогодние каникулы", "2027-01-06": "Новогодние каникулы",
    "2027-01-07": "Рождество", "2027-01-08": "Новогодние каникулы",
    "2027-02-23": "День защитника Отечества",
    "2027-03-08": "8 Марта",
    "2027-05-01": "Праздник Весны и Труда",
    "2027-05-09": "День Победы",
    "2027-06-12": "День России",
    "2027-11-04": "День народного единства",
}

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Белый список: Telegram ID через запятую. Остальным бот не отвечает.
TELEGRAM_ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
REFRESH_DAYS = 14             # при обновлении перезагружаем последние 14 дней (посещения отмечают задним числом)

# --- Расписание: агент работает сам, без кнопок ---
# Планировщик живёт внутри бота, поэтому не зависит от хостинга (Mac, Railway, VPS — одинаково).
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
try:
    TZ = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError:      # нет базы часовых поясов — работаем по UTC
    TZ = ZoneInfo("UTC")
SCHEDULE_ENABLED = os.getenv("SCHEDULE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "6"))        # ежедневная выгрузка свежих данных из CRM
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))
REPORT_WEEKDAY = int(os.getenv("REPORT_WEEKDAY", "1"))   # 1 = понедельник
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))
SYNC_STALE_HOURS = int(os.getenv("SYNC_STALE_HOURS", "20"))  # при запуске: если данные старше — обновить
BACKFILL_WEEKS = int(os.getenv("BACKFILL_WEEKS", "12"))     # столько недель доливаем на пустом сервере

# --- Зарплата педагогов ---
# Ставка за одно занятие по числу пришедших: (от, до включительно, рублей). Одна для всех педагогов.
PAY_RATES = [
    (1, 3, 600),
    (4, 7, 1000),
    (8, 10, 1500),
    (11, 15, 2000),
    (16, 20, 3000),
    (21, 29, 4000),
]

# Как админы называют педагогов в сводках → как педагог записан в CRM (для сверки).
# Хранится в teacher_aliases.json рядом с проектом, формат: {"Варя": "Иванова В."}
_aliases_file = PROJECT_DIR / "teacher_aliases.json"   # не публикуется: см. teacher_aliases.example.json
TEACHER_ALIASES = json.loads(_aliases_file.read_text(encoding="utf-8")) if _aliases_file.exists() else {}

# --- LLM: комментарий к отчёту и ответы на вопросы ---
# По умолчанию Yandex AI Studio (OpenAI-совместимый API, серверы в РФ, оплата в рублях).
# Чтобы сменить провайдера, достаточно поменять эти настройки в .env.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "yandex")
LLM_MODEL = os.getenv("LLM_MODEL") or "aliceai-llm-flash"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_FOLDER_ID = os.getenv("LLM_FOLDER_ID", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or (
    "https://ai.api.cloud.yandex.net/v1" if LLM_PROVIDER == "yandex" else "")
LLM_TEMPERATURE = 0.2
LLM_MAX_TOOL_ROUNDS = 4       # сколько раз подряд модель может обратиться к данным за один вопрос
LLM_TIMEOUT_SEC = 60

# --- Postgres: постоянное хранилище для дашборда студии ---
# Рабочее хранилище бота остаётся SQLite (локально и быстро).
# Postgres нужен, чтобы данные жили не в памяти и не на этом компьютере:
# из него потом читает дашборд, и он переживает перезапуск бота.
# Railway даёт DATABASE_URL; на случай если дали только PGHOST/PGUSER/... — собираем строку сами.
def _pg_dsn() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("PGHOST") or "").strip()
    user = (os.getenv("PGUSER") or "").strip()
    password = os.getenv("PGPASSWORD") or ""
    dbname = (os.getenv("PGDATABASE") or "").strip()
    if not (host and user and dbname):
        return ""
    port = (os.getenv("PGPORT") or "5432").strip()
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{dbname}"


DATABASE_URL = _pg_dsn()
PG_CONNECT_TIMEOUT = 15      # секунд на установку соединения
PG_STATEMENT_TIMEOUT = 60    # секунд на запрос

# Русские названия направлений → как они записаны в CRM (для поиска групп в вопросах к ИИ)
STYLE_SYNONYMS = {
    "хип хоп": "hip hop", "хипхоп": "hip hop", "афро": "afro", "хорео": "choreo", "хореограф": "choreo",
    "контемп": "contemporary", "стретч": "stretching", "растяжк": "stretching", "хилс": "high heels",
    "хай хилс": "high heels", "дэнсхолл": "dancehall", "дансхолл": "dancehall", "денсхолл": "dancehall",
    "вог": "vogue", "джаз фанк": "jazz funk", "джазфанк": "jazz funk", "герли": "girly", "гёрли": "girly",
    "хаус": "house", "авторск": "author's",
}
