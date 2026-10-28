#!/usr/bin/env bash
# Деплой бота-аналитика на Railway одной командой.
#
# Что делает:
#   1. создаёт проект и сервис в Railway и делает первый деплой (пока без ключей);
#   2. переносит ключи из .env в Variables Railway (значения идут через stdin —
#      они не попадают ни в историю команд, ни в список процессов);
#   3. передеплоит уже с ключами и показывает, что делать дальше.
#
# Требуется один раз: railway login (откроется браузер — войти через GitHub).
#
# Запуск:  bash scripts/railway_up.sh
# Логи:    railway logs
set -euo pipefail
cd "$(dirname "$0")/.."

KEYS=(
  TELEGRAM_BOT_TOKEN
  TELEGRAM_ALLOWED_USER_IDS
  IMPULSE_BASE_URL
  IMPULSE_API_KEY
  IMPULSE_LOGIN
  IMPULSE_PASSWORD
  DATABASE_URL
  LLM_PROVIDER
  LLM_MODEL
  LLM_BASE_URL
  LLM_API_KEY
  LLM_FOLDER_ID
  TIMEZONE
)

if [ ! -f .env ]; then
  echo "✗ Нет файла .env. Скопируйте .env.example в .env и заполните ключи."
  exit 1
fi

echo "→ 1/3: создаю проект и сервис (первый деплой пока без ключей)"
railway up --detach || true

echo
echo "→ 2/3: переношу ключи из .env в Variables Railway"
for key in "${KEYS[@]}"; do
  value="$(sed -n "s/^${key}=//p" .env | head -1 | tr -d '\r')"
  if [ -z "$value" ]; then
    continue
  fi
  printf '%s' "$value" | railway variable set "$key" --stdin --skip-deploys >/dev/null
  echo "   ✓ $key"
done

echo
echo "→ 3/3: передеплою с ключами"
railway up --detach

cat <<'TIP'

Готово. Дальше:
  railway logs        — журнал бота (ищите «Бот @garage_spb_analytics_bot запущен»
                       и «Расписание включено»)
  написать боту /status в Telegram — он ответит, когда следующее обновление

Не забудьте: после того как серверный бот заработал, выключите бота на Mac
(иначе два бота с одним токеном будут мешать друг другу):
  launchctl bootout gui/$(id -u)/local.garage.attendance
TIP
