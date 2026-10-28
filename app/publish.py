"""Публикация снимка данных в Postgres — общий шаг для кнопок и для расписания.

Postgres не обязателен: если его нет или он недоступен, бот работает как обычно
(на локальной базе SQLite).
"""

import asyncio
import logging

from app import pg

log = logging.getLogger(__name__)


async def publish_to_pg():
    """Выложить снимок данных в Postgres — оттуда читает дашборд."""
    if not pg.enabled():
        return
    try:
        stats = await asyncio.to_thread(pg.publish)
        log.info("Postgres обновлён: %s", stats)
    except Exception as e:
        log.warning("Postgres не обновился (%s) — бот продолжает работать на SQLite", e)
