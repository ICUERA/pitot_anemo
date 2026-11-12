import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import support
import board
import config
import heater


class HeaterPin(support.Pin):
    levels = []

    def init(self, mode=None, value=None, hold=None, drive=None, pull=None):
        super().init(mode, value=value, hold=hold, drive=drive, pull=pull)
        if self.number == config.HEATER_PIN and value is not None:
            self.levels.append(value)

    def value(self, value=None):
        if self.number == config.HEATER_PIN and value is not None:
            self.levels.append(value)
        return super().value(value)


class HeaterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.now_ms = 1000000000
        support.RTC.data = b""
        HeaterPin.levels = []
        self.patches.enter_context(patch.object(heater.machine, "Pin", HeaterPin))
        self.patches.enter_context(patch.object(heater.utime, "time", lambda: self.now_ms // 1000))
        self.patches.enter_context(patch.object(heater.utime, "ticks_ms", lambda: self.now_ms % (1 << 30)))
        self.patches.enter_context(patch.object(heater.asyncio, "sleep_ms", self.sleep_ms))
        self.voltage = self.patches.enter_context(patch.object(board, "read_battery_voltage", new_callable=AsyncMock))
        self.voltage.return_value = 3.8
        self.command = {"enabled": True, "duration_s": 10}

    async def sleep_ms(self, milliseconds):
        self.now_ms += milliseconds
        await asyncio.sleep(0)

    def allow_first_cycle(self):
        cooldown = heater.HeaterCooldown()
        cooldown.allow_battery(3.8)
        self.now_ms += config.HEATER_INTERVAL_S * 1000

    async def test_cold_start_waits_three_hours(self):
        self.assertEqual(await heater.run_heater(self.command), "cooldown")
        self.assertNotIn(1, HeaterPin.levels)

    async def test_disabled_command_never_switches_on(self):
        self.allow_first_cycle()
        self.command["enabled"] = False
        self.assertEqual(await heater.run_heater(self.command), "disabled")
        self.assertNotIn(1, HeaterPin.levels)

    async def test_threshold_allows_exactly_3_3_volts_and_switches_off_after_duration(self):
        self.allow_first_cycle()
        self.voltage.return_value = 3.3
        started = self.now_ms
        self.assertEqual(await heater.run_heater(self.command), "complete")
        self.assertEqual(self.now_ms - started, config.HEATER_IDLE_SETTLE_MS + 10000)
        self.assertEqual(HeaterPin.levels, [0, 1, 0])

    async def test_low_missing_and_invalid_voltage_prevent_start(self):
        self.allow_first_cycle()
        for voltage in (3.299, None, float("nan"), float("inf"), 0, 5):
            with self.subTest(voltage=voltage):
                self.voltage.return_value = voltage
                self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
                self.assertNotIn(1, HeaterPin.levels)
        self.voltage.return_value = 3.8
        self.assertEqual(await heater.run_heater(self.command), "complete")

    async def test_voltage_drop_stops_heat_and_keeps_cooldown(self):
        self.allow_first_cycle()
        self.voltage.side_effect = [3.8, 3.8, 3.2]
        started = self.now_ms
        self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
        self.assertEqual(self.now_ms - started, config.HEATER_IDLE_SETTLE_MS + config.HEATER_CHECK_INTERVAL_MS)
        self.assertEqual(HeaterPin.levels[-1], 0)
        self.voltage.side_effect = None
        self.voltage.return_value = 3.8
        self.assertEqual(await heater.run_heater(self.command), "cooldown")

    async def test_adc_exception_always_turns_gate_off(self):
        self.allow_first_cycle()
        self.voltage.side_effect = [3.8, OSError("ADC")]
        with self.assertRaises(OSError):
            await heater.run_heater(self.command)
        self.assertEqual(HeaterPin.levels[-1], 0)
        self.assertEqual(support.Pin.values[config.HEATER_PIN], 0)

    async def test_cancellation_always_turns_gate_off(self):
        async def cancelled(milliseconds):
            if milliseconds == config.HEATER_CHECK_INTERVAL_MS:
                raise asyncio.CancelledError
            await self.sleep_ms(milliseconds)

        self.allow_first_cycle()
        with patch.object(heater.asyncio, "sleep_ms", cancelled), self.assertRaises(asyncio.CancelledError):
            await heater.run_heater(self.command)
        self.assertEqual(HeaterPin.levels, [0, 1, 0])

    async def test_cooldown_survives_new_instances_and_allows_only_after_three_hours(self):
        self.allow_first_cycle()
        cooldown = heater.HeaterCooldown()
        self.assertTrue(cooldown.reserve())
        cooldown.complete()
        self.assertFalse(heater.HeaterCooldown().reserve())
        self.now_ms += (config.HEATER_INTERVAL_S - 1) * 1000
        self.assertFalse(heater.HeaterCooldown().reserve())
        self.now_ms += 1000
        self.assertTrue(heater.HeaterCooldown().reserve())

    async def test_sag_lock_survives_reboot_and_needs_3_7_volts_without_load(self):
        self.allow_first_cycle()
        self.voltage.side_effect = [3.8, 3.2]
        self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
        self.assertTrue(heater.HeaterCooldown().battery_locked)
        self.now_ms += config.HEATER_INTERVAL_S * 1000
        self.voltage.side_effect = None
        self.voltage.return_value = 3.699
        self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
        self.assertEqual(HeaterPin.levels.count(1), 1)
        self.voltage.return_value = 3.7
        self.assertEqual(await heater.run_heater(self.command), "complete")
        self.assertEqual(HeaterPin.levels.count(1), 2)
        self.assertFalse(heater.HeaterCooldown().battery_locked)

    async def test_recovery_voltage_does_not_bypass_three_hour_interval(self):
        self.allow_first_cycle()
        self.voltage.side_effect = [3.8, 3.2]
        self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
        self.voltage.side_effect = None
        self.voltage.return_value = 3.7
        self.assertEqual(await heater.run_heater(self.command), "cooldown")
        self.assertEqual(HeaterPin.levels.count(1), 1)

    async def test_normal_completion_allows_3_5_volts_next_time(self):
        self.allow_first_cycle()
        self.voltage.return_value = 3.5
        self.assertEqual(await heater.run_heater(self.command), "complete")
        self.now_ms += config.HEATER_INTERVAL_S * 1000
        self.assertEqual(await heater.run_heater(self.command), "complete")

    async def test_recovery_voltage_is_measured_with_gate_off_after_settling(self):
        self.allow_first_cycle()
        started = self.now_ms

        async def read_voltage(samples=None):
            if samples is None:
                self.assertEqual(support.Pin.values[config.HEATER_PIN], 0)
                self.assertGreaterEqual(self.now_ms - started, config.HEATER_IDLE_SETTLE_MS)
                return 3.7
            self.assertEqual(samples, 1)
            self.assertEqual(support.Pin.values[config.HEATER_PIN], 1)
            return 3.2

        self.voltage.side_effect = read_voltage
        self.assertEqual(await heater.run_heater(self.command), "battery_lockout")
        self.assertEqual(support.Pin.values[config.HEATER_PIN], 0)

    async def test_incomplete_heating_leaves_persistent_battery_lock(self):
        self.allow_first_cycle()
        self.assertTrue(heater.HeaterCooldown().reserve())
        self.now_ms += config.HEATER_INTERVAL_S * 1000
        rebooted = heater.HeaterCooldown()
        self.assertFalse(rebooted.allow_battery(3.6))
        self.assertFalse(rebooted.reserve())
        self.assertTrue(rebooted.allow_battery(3.7))
        self.assertTrue(rebooted.reserve())

    async def test_missing_history_requires_recovery_voltage_and_cooldown(self):
        cooldown = heater.HeaterCooldown()
        self.assertFalse(cooldown.allow_battery(3.6))
        self.assertTrue(cooldown.allow_battery(3.7))
        self.assertFalse(cooldown.reserve())

    async def test_corrupt_history_and_clock_rollback_restart_lockout(self):
        self.allow_first_cycle()
        support.RTC.data = b"broken record"
        self.assertFalse(heater.HeaterCooldown().reserve())
        self.now_ms -= 1000
        self.assertFalse(heater.HeaterCooldown().reserve())

    async def test_failed_rtc_write_prevents_gate_on(self):
        self.allow_first_cycle()
        record = support.RTC.data
        with patch.object(support.RTC, "memory", return_value=record), self.assertRaises(OSError):
            await heater.run_heater(self.command)
        self.assertNotIn(1, HeaterPin.levels)

    async def test_failed_completion_record_leaves_gate_off_and_lock_set(self):
        self.allow_first_cycle()
        memory = support.RTC.memory

        def fail_unlock(rtc, data=None):
            if data is not None and data.rsplit(b":", 1)[0].endswith(b":0"):
                self.assertEqual(support.Pin.values[config.HEATER_PIN], 0)
                return memory(rtc)
            return memory(rtc, data)

        with patch.object(support.RTC, "memory", fail_unlock), self.assertRaises(OSError):
            await heater.run_heater(self.command)
        self.assertEqual(HeaterPin.levels, [0, 1, 0])
        self.assertTrue(heater.HeaterCooldown().battery_locked)

    async def test_overlong_heating_rejected_before_gate_on(self):
        self.allow_first_cycle()
        self.command["duration_s"] = config.HEATER_MAX_DURATION_S + 1
        with self.assertRaises(ValueError):
            await heater.run_heater(self.command)
        self.assertNotIn(1, HeaterPin.levels)


class BatteryVoltageTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_voltage_measurement_returns_unknown(self):
        with patch.object(config, "BATTERY_ADC_PIN", None):
            self.assertIsNone(await board.read_battery_voltage())

    async def test_external_divider_reads_actual_battery_voltage(self):
        adc = Mock()
        adc.read_uv.return_value = 1650000
        factory = Mock(return_value=adc, ATTN_11DB=3)
        with patch.object(config, "BATTERY_ADC_PIN", 35), patch.object(board.machine, "ADC", factory, create=True):
            self.assertAlmostEqual(await board.read_battery_voltage(), 3.3)
        adc.atten.assert_called_once_with(3)

    async def test_adc_rail_and_read_errors_return_unknown(self):
        for value in (0, 149000, 2500000, OSError("ADC")):
            adc = Mock()
            adc.read_uv.side_effect = value if isinstance(value, Exception) else None
            adc.read_uv.return_value = value
            factory = Mock(return_value=adc, ATTN_11DB=3)
            with self.subTest(value=value), patch.object(config, "BATTERY_ADC_PIN", 35):
                with patch.object(board.machine, "ADC", factory, create=True):
                    self.assertIsNone(await board.read_battery_voltage())
