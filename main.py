"""Точка входа MicroPython; каждый запуск заканчивается глубоким сном."""

import asyncio
import machine

import board
import config
from app import run_cycle


def main():
    sleep_ms = config.DEFAULT_SLEEP_MINUTES * 60000
    try:
        board.disable_heater()
        sleep_ms = asyncio.run(run_cycle())
    except (Exception, KeyboardInterrupt) as exc:
        print("Завершение прошивки:", exc)
    finally:
        try:
            board.prepare_sleep_pins()
        finally:
            print("Глубокий сон на", sleep_ms // 60000, "минут")
            machine.deepsleep(sleep_ms)


if __name__ == "__main__":
    main()
