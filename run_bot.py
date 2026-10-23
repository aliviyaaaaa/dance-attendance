"""Запуск с журналом и защитой от двух экземпляров одной копии проекта."""

import fcntl
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import runpy


def main():
    root = Path(__file__).resolve().parent
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    with (logs / "bot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Эта копия бота уже запущена. Откройте её окно Терминала.")
            return
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.StreamHandler(), RotatingFileHandler(
                logs / "bot_console.log", maxBytes=2_000_000,
                backupCount=3, encoding="utf-8")],
        )
        try:
            runpy.run_module("app.bot", run_name="__main__")
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
