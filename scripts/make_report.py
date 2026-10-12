"""Этап 4. Отчёт за неделю в консоль и в файлы (без Telegram).

Запуск:  .venv/bin/python -m scripts.make_report          — за прошлую неделю
         .venv/bin/python -m scripts.make_report 28.09    — за неделю, в которую входит эта дата
"""

import sys

from app import db
from app.report import REPORTS_DIR, build_report, last_full_week, parse_week_arg


def main():
    monday = parse_week_arg(sys.argv[1]) if len(sys.argv) > 1 else last_full_week()
    conn = db.connect()
    try:
        text, excel = build_report(conn, monday)
    finally:
        conn.close()
    print(text)
    REPORTS_DIR.mkdir(exist_ok=True)
    txt_path = REPORTS_DIR / f"посещаемость_{monday:%Y-%m-%d}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print(f"\n--- сохранено: {txt_path.name}" + (f", {excel.rsplit('/', 1)[-1]}" if excel else ""))


if __name__ == "__main__":
    main()
