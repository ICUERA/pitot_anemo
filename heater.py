"""Оттаивание с гистерезисом защиты батареи и ограничением частоты по RTC."""

import asyncio
import machine
import math
import utime

import board
import config


class HeaterCooldown:
    PREFIX = b"PITO_HEATER_V2"

    def __init__(self):
        self.rtc = machine.RTC()
        now = utime.time()
        try:
            body, checksum = self.rtc.memory().rsplit(b":", 1)
            prefix, started, deadline, locked = body.split(b":")
            self.started, self.deadline = int(started), int(deadline)
            self.battery_locked = locked == b"1"
            if (prefix != self.PREFIX or int(checksum) != sum(body) % 65536
                    or locked not in (b"0", b"1") or self.started < 0
                    or self.deadline - self.started != config.HEATER_INTERVAL_S
                    or now < self.started):
                raise ValueError("Недостоверная запись RTC")
        except (ValueError, TypeError):
            # Утраченная история могла содержать просадку: нужны и новый
            # трёхчасовой интервал, и подтверждение 3.7 В без нагревателя.
            self.started, self.deadline = now, now + config.HEATER_INTERVAL_S
            self.battery_locked = True
            self._save()

    def allow_battery(self, voltage):
        """Проверить напряжение при выключенном нагревателе, снять защёлку при 3.7 В."""
        if not _voltage_allows_heating(voltage):
            if not self.battery_locked:
                self.battery_locked = True
                self._save()
            return False
        if self.battery_locked:
            if voltage < config.HEATER_RECOVERY_BATTERY_V:
                return False
            self.battery_locked = False
            self._save()
        return True

    def reserve(self):
        now = utime.time()
        if now < self.started:
            self.started, self.deadline = now, now + config.HEATER_INTERVAL_S
            self.battery_locked = True
            self._save()
        if now < self.deadline or self.battery_locked:
            return False
        self.started, self.deadline = now, now + config.HEATER_INTERVAL_S
        # До подачи нагрузки записываем защёлку. Даже сброс питания во время
        # просадки не должен превратить незавершённый нагрев в успешный.
        self.battery_locked = True
        self._save()
        return True

    def complete(self):
        """Снять предварительную защёлку после штатного выключения ключа."""
        self.battery_locked = False
        self._save()

    def _save(self):
        body = self.PREFIX + (":%d:%d:%d" % (
            self.started, self.deadline, int(self.battery_locked),
        )).encode()
        record = body + (":%d" % (sum(body) % 65536)).encode()
        self.rtc.memory(record)
        if self.rtc.memory() != record:
            raise OSError("Не удалось сохранить защиту нагрева в RTC")


async def run_heater(command):
    board.disable_heater()
    completed = False
    try:
        cooldown = HeaterCooldown()
        if command["enabled"] is not True:
            return "disabled"
        duration = command["duration_s"]
        if type(duration) is not int or not 1 <= duration <= config.HEATER_MAX_DURATION_S:
            raise ValueError("Недопустимая длительность оттаивания")
        await asyncio.sleep_ms(config.HEATER_IDLE_SETTLE_MS)
        if not cooldown.allow_battery(await board.read_battery_voltage()):
            return "battery_lockout"
        if not cooldown.reserve():
            return "cooldown"
        pin = machine.Pin(config.HEATER_PIN)
        started = utime.ticks_ms()
        pin.value(1)
        print("Оттаивание: включено на", duration, "секунд")
        while True:
            remaining = duration * 1000 - utime.ticks_diff(utime.ticks_ms(), started)
            if remaining <= 0:
                completed = True
                return "complete"
            # Первое чтение выполняется сразу после включения. Под нагрузкой
            # усреднение нескольких отсчётов могло бы скрыть краткую просадку.
            if not _voltage_allows_heating(await board.read_battery_voltage(samples=1)):
                return "battery_lockout"
            remaining = duration * 1000 - utime.ticks_diff(utime.ticks_ms(), started)
            if remaining > 0:
                await asyncio.sleep_ms(min(remaining, config.HEATER_CHECK_INTERVAL_MS))
    finally:
        # Ключ выключается до любых операций с RTC, даже при ошибке сохранения.
        board.disable_heater()
        if completed:
            cooldown.complete()


def _voltage_allows_heating(voltage):
    return (voltage is not None and math.isfinite(voltage)
            and config.HEATER_MIN_BATTERY_V <= voltage <= 4.5)
