# Бот-аналитика студии: контейнер для Railway, VPS и любого Docker-хостинга.
# Сборка: docker build -t garage-bot .
# Запуск:  docker run -d --env-file .env -v ./data:/app/data garage-bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

# tzdata нужен, чтобы расписание считалось по московскому времени
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY run_bot.py ./

# Работаем не под root: ключи из окружения, база в отдельной папке
RUN useradd --create-home --uid 10001 bot \
 && mkdir -p /app/data /app/reports /app/logs \
 && chown -R bot:bot /app
USER bot

VOLUME ["/app/data", "/app/reports"]

CMD ["python", "-m", "app.bot"]
