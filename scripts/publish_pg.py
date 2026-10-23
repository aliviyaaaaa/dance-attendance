"""Публикация данных студии в Postgres и проверка, что они на месте.

Запуск:  .venv/bin/python -m scripts.publish_pg           — выложить снимок
         .venv/bin/python -m scripts.publish_pg --check   — посмотреть, что лежит
"""

import sys

from app import pg


def main():
    if not pg.enabled():
        print("Postgres не настроен: в .env нет DATABASE_URL.")
        print("Добавьте строку подключения Railway и повторите.")
        return 1

    if "--check" in sys.argv:
        info = pg.check()
        print("Что лежит в Postgres:")
        for key, value in info.items():
            print(f"  {key}: {value}")
        return 0

    stats = pg.publish()
    print("Опубликовано:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
