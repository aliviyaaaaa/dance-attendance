"""Сверка сводки админов с ботом из файла.

Запуск:  .venv/bin/python -m scripts.compare_admin data/admin_2026-09-16_30.txt
"""

import sys

from app import db
from app.admin_check import build_check_text

if __name__ == "__main__":
    print(build_check_text(db.connect(), open(sys.argv[1], encoding="utf-8").read()))
