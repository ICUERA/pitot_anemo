import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import support
import app
import board
import config
import main


class Sensor:
    events = []
    failed = False

    def __init__(self, i2c, address):
        if support.Pin.values.get(config.SENSOR_POWER_PIN) != 1:
            raise OSError("Питание датчиков не включено")
        self.address = address

    def start_measurement(self):
        self.events.append("sample_start")

    def read_measurement(self):
        if self.failed and self.address == 0x77:
            raise OSError("Нет статического датчика")
        return (100010 if self.address == 0x76 else 100000), 20

    def sleep(self):
        self.events.append("sensor_sleep")


class Modem:
    events = []
    payloads = []
    fail_publish = False
    block_network = False
    fail_http = False

    async def connect_network(self):
        self.events.append("network_start")
        try:
            await asyncio.sleep(10 if self.block_network else 0.01)
        finally:
            self.events.append("network_end")

    async def connect_mqtt(self, client_id):
        self.events.append("mqtt_connect")

    async def publish(self, topic, payload):
        if self.fail_publish:
            raise OSError("Нет связи")
        self.events.append("publish")
        self.payloads.append((topic, payload))

    async def disconnect_mqtt(self):
        self.events.append("mqtt_disconnect")

    async def read_settings(self):
        self.events.append("http")
        if self.fail_http:
            raise ValueError("404")
        return {"sleep_minutes": 12, "heater": {"enabled": False, "duration_s": 60}}

    async def power_off(self):
        self.events.append("power_off")


class CheckedPin(support.Pin):
    events = []
    stuck_high = set()
    fail_hold = set()

    def init(self, mode=None, value=None, hold=None, drive=None, pull=None):
        if hold and self.number in self.fail_hold:
            raise OSError("Не удалось удержать линию")
        super().init(mode, value=value, hold=hold, drive=drive, pull=pull)
        if value is not None:
            self.events.append(("value", self.number, value))
        if hold is not None:
            self.events.append(("hold", self.number, hold))

    def value(self, value=None):
        if value is not None:
            return super().value(value)
        level = 1 if self.number in self.stuck_high else super().value()
        self.events.append(("read", self.number, level))
        return level


class CycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        Sensor.events = Modem.events = []
        Sensor.failed = Modem.fail_publish = Modem.fail_http = Modem.block_network = False
        Modem.payloads = []
        for name, value in (("SAMPLE_WINDOW_MS", 120), ("SAMPLE_INTERVAL_MS", 60),
                            ("CONVERSION_MS", 50), ("NETWORK_TIMEOUT_MS", 200)):
            self.patches.enter_context(patch.object(config, name, value))
        self.patches.enter_context(patch.object(app, "BMP280", Sensor))
        self.patches.enter_context(patch.object(app, "SIM800", Modem))
        self.patches.enter_context(patch.object(board, "enable_battery_power"))
        self.patches.enter_context(patch.object(board, "read_battery", return_value={"battery_percent": 75}))

    async def test_measurement_overlaps_network_and_http_sets_sleep(self):
        self.assertEqual(await app.run_cycle(), 12 * 60000)
        events = Modem.events
        self.assertLess(events.index("network_start"), events.index("sample_start"))
        self.assertLess(events.index("sample_start"), events.index("network_end"))
        self.assertLess(events.index("sensor_sleep"), events.index("publish"))
        self.assertEqual(events[-1], "power_off")
        self.assertEqual(len(Modem.payloads), 1)
        topic, payload = Modem.payloads[0]
        self.assertEqual(topic, "fluger_p")
        self.assertEqual(payload["samples"], 2)
        self.assertEqual(payload["battery_percent"], 75)
        self.assertEqual(payload["device_id"], "pito-010203")

    async def test_network_timeout_cancels_before_power_off(self):
        Modem.block_network = True
        self.assertEqual(await app.run_cycle(), 5 * 60000)
        self.assertLess(Modem.events.index("network_end"), Modem.events.index("power_off"))
        self.assertNotIn("publish", Modem.events)

    async def test_http_failure_keeps_default_sleep(self):
        Modem.fail_http = True
        self.assertEqual(await app.run_cycle(), 5 * 60000)
        self.assertIn("publish", Modem.events)
        self.assertEqual(Modem.events[-1], "power_off")

    async def test_sensor_failure_sends_null_instead_of_fabricated_wind(self):
        Sensor.failed = True
        self.assertEqual(await app.run_cycle(), 12 * 60000)
        payload = Modem.payloads[0][1]
        self.assertEqual(payload["samples"], 0)
        self.assertEqual(payload["sample_errors"], 2)
        self.assertIsNone(payload["wind_m_s"])
        self.assertEqual(payload["wind_status"], "no_samples")

    async def test_publish_failure_still_finishes_measurement_and_reads_http(self):
        Modem.fail_publish = True
        self.assertEqual(await app.run_cycle(), 12 * 60000)
        self.assertLess(Modem.events.index("sensor_sleep"), Modem.events.index("http"))
        self.assertEqual(Modem.events.count("mqtt_connect"), 2)
        self.assertEqual(Modem.events[-1], "power_off")

    async def test_broken_modem_constructor_still_stops_sensors(self):
        self.patches.enter_context(patch.object(app, "SIM800", side_effect=OSError("UART")))
        self.assertEqual(await app.run_cycle(), 5 * 60000)
        self.assertNotIn("network_start", Modem.events)

    async def test_remote_heat_command_runs_after_modem_shutdown(self):
        settings = {"sleep_minutes": 12, "heater": {"enabled": True, "duration_s": 30}}
        received = []

        async def read_settings(modem):
            Modem.events.append("http")
            return settings

        async def run_heater(command):
            received.append(command)
            Modem.events.append("heat")
            return "complete"

        with patch.object(Modem, "read_settings", read_settings), patch.object(app, "run_heater", run_heater):
            self.assertEqual(await app.run_cycle(), 12 * 60000)
        self.assertEqual(received, [settings["heater"]])
        self.assertLess(Modem.events.index("sensor_sleep"), Modem.events.index("heat"))
        self.assertLess(Modem.events.index("power_off"), Modem.events.index("heat"))

    async def test_heating_error_keeps_received_sleep_period(self):
        async def run_heater(command):
            raise OSError("Ошибка нагрева")

        with patch.object(app, "run_heater", run_heater):
            self.assertEqual(await app.run_cycle(), 12 * 60000)


class MainTests(unittest.TestCase):
    def test_deep_sleep_after_cycle(self):
        async def run_cycle():
            return 12 * 60000

        with patch.object(main, "run_cycle", run_cycle), patch.object(board, "prepare_sleep_pins") as pins:
            with patch.object(main.machine, "deepsleep") as sleep:
                main.main()
        pins.assert_called_once_with()
        sleep.assert_called_once_with(12 * 60000)

    def test_deep_sleep_even_after_cycle_and_gpio_errors(self):
        async def run_cycle():
            raise OSError("Ошибка запуска")

        support.Pin.values[config.HEATER_PIN] = 1
        with patch.object(main, "run_cycle", run_cycle):
            with patch.object(board, "prepare_sleep_pins", side_effect=OSError("GPIO")):
                with patch.object(main.machine, "deepsleep") as sleep, self.assertRaises(OSError):
                    main.main()
        sleep.assert_called_once_with(5 * 60000)
        self.assertEqual(support.Pin.values[config.HEATER_PIN], 0)


class BatteryTests(unittest.TestCase):
    def test_ip5306_battery_steps_and_missing_device(self):
        for value, percent in ((0xF0, 0), (0xE0, 25), (0xC0, 50), (0x80, 75), (0, 100), (0xA0, None)):
            with self.subTest(value=value):
                bus = unittest.mock.Mock()
                bus.readfrom_mem.return_value = bytes((value,))
                self.assertEqual(board.read_battery(bus)["battery_percent"], percent)
        bus = unittest.mock.Mock()
        bus.readfrom_mem.side_effect = OSError("I2C")
        self.assertIsNone(board.read_battery(bus)["battery_percent"])


class SensorPowerTests(unittest.TestCase):
    def setUp(self):
        CheckedPin.events = []
        CheckedPin.stuck_high = set()
        CheckedPin.fail_hold = set()
        self.pins = patch.object(board.machine, "Pin", CheckedPin)
        self.pins.start()
        self.addCleanup(self.pins.stop)

    def test_maximum_drive_and_power_off_held_during_sleep(self):
        board.release_sleep_pins()
        board.enable_sensor_power()
        self.assertEqual(support.Pin.values[18], 1)
        self.assertEqual(support.Pin.drives[18], support.Pin.DRIVE_3)
        CheckedPin.events.clear()
        board.prepare_sleep_pins()
        self.assertEqual(support.Pin.values[18], 0)
        self.assertTrue(support.Pin.holds[18])
        events = CheckedPin.events
        cutoff = events.index(("value", 18, 0))
        self.assertLess(events.index(("value", config.I2C_SCL, 0)), events.index(("value", config.I2C_SDA, 0)))
        for number in (config.I2C_SCL, config.I2C_SDA):
            self.assertLess(events.index(("hold", number, True)), cutoff)
            self.assertLess(events.index(("read", number, 0)), cutoff)
            self.assertTrue(support.Pin.holds[number])
        board.release_sleep_pins()
        self.assertFalse(support.Pin.holds[18])

    def test_high_sda_or_scl_prevents_power_cut(self):
        for number in (config.I2C_SCL, config.I2C_SDA):
            with self.subTest(number=number):
                board.release_sleep_pins()
                board.enable_sensor_power()
                CheckedPin.events.clear()
                CheckedPin.stuck_high = {number}
                board.prepare_sleep_pins()
                self.assertEqual(support.Pin.values[18], 1)
                self.assertTrue(support.Pin.holds[18])
                self.assertNotIn(("value", 18, 0), CheckedPin.events)

    def test_failed_bus_hold_prevents_power_cut(self):
        board.release_sleep_pins()
        board.enable_sensor_power()
        CheckedPin.events.clear()
        CheckedPin.fail_hold = {config.I2C_SDA}
        board.prepare_sleep_pins()
        self.assertEqual(support.Pin.values[18], 1)
        self.assertTrue(support.Pin.holds[18])
        self.assertNotIn(("value", 18, 0), CheckedPin.events)

    def test_wake_keeps_bus_low_until_sensors_are_powered(self):
        board.prepare_sleep_pins()
        CheckedPin.events.clear()
        board.release_sleep_pins()
        for number in (config.I2C_SCL, config.I2C_SDA):
            self.assertTrue(support.Pin.holds[number])
            self.assertEqual(support.Pin.values[number], 0)
        board.enable_sensor_power()
        board.release_i2c_pins()
        power_on = CheckedPin.events.index(("value", 18, 1))
        for number in (config.I2C_SCL, config.I2C_SDA):
            self.assertLess(power_on, CheckedPin.events.index(("hold", number, False)))
            self.assertFalse(support.Pin.holds[number])
