"""Минимальные замены аппаратных модулей для проверок на CPython."""

import asyncio
import sys
import time
import types


class Pin:
    OUT = 1
    OPEN_DRAIN = 3
    DRIVE_3 = 3
    values = {}
    drives = {}
    holds = {}

    def __init__(self, number, mode=None, value=None, drive=None):
        self.number = number
        self.init(mode, value=value, drive=drive)

    def init(self, mode=None, value=None, hold=None, drive=None, pull=None):
        if value is not None:
            self.values[self.number] = value
        if drive is not None:
            self.drives[self.number] = drive
        if hold is not None:
            self.holds[self.number] = hold

    def value(self, value=None):
        if value is not None:
            self.values[self.number] = value
        return self.values.get(self.number, 0)


class UART:
    def __init__(self, *args, **kwargs):
        self.data = b""
        self.writes = []
        self.deinitialized = False

    def read(self, count):
        # Ответы намеренно разбиты в середине CR/LF и служебных слов.
        chunk = self.data[:min(count, 3)]
        self.data = self.data[len(chunk):]
        return chunk or None

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def deinit(self):
        self.deinitialized = True


class RTC:
    data = b""

    def memory(self, data=None):
        if data is not None:
            type(self).data = bytes(data)
        return type(self).data


async def sleep_ms(milliseconds):
    await asyncio.sleep(milliseconds / 1000)


async def wait_for_ms(awaitable, milliseconds):
    return await asyncio.wait_for(awaitable, milliseconds / 1000)


asyncio.sleep_ms = sleep_ms
asyncio.wait_for_ms = wait_for_ms
sys.modules.setdefault("utime", types.SimpleNamespace(
    time=lambda: int(time.time()),
    ticks_ms=lambda: int(time.monotonic() * 1000) % (1 << 30),
    ticks_diff=lambda a, b: (a - b + (1 << 29)) % (1 << 30) - (1 << 29),
    ticks_add=lambda value, delta: (value + delta) % (1 << 30),
    sleep_ms=lambda milliseconds: time.sleep(milliseconds / 1000),
))
sys.modules.setdefault("machine", types.SimpleNamespace(
    Pin=Pin, UART=UART, RTC=RTC, unique_id=lambda: b"\x01\x02\x03",
    I2C=lambda *args, **kwargs: None, deepsleep=lambda milliseconds: None,
))
