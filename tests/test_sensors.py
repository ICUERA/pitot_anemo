import math
import struct
import unittest

import support
from bmp280 import BMP280
from telemetry import Telemetry


class SensorBus:
    def __init__(self):
        self.writes = []
        self.chip_id = 0x58
        self.status = 0
        self.raw = (415148, 519888)

    def readfrom_mem(self, address, register, count):
        if register == 0xD0:
            return bytes((self.chip_id,))
        if register == 0xF3:
            return bytes((self.status,))
        if register == 0x88:
            return struct.pack("<HhhHhhhhhhhh", 27504, 26435, -1000, 36477,
                               -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)
        if register == 0xF7:
            return b"".join(bytes((value >> 12, (value >> 4) & 255, (value & 15) << 4))
                            for value in self.raw)
        raise AssertionError(register)

    def writeto_mem(self, address, register, data):
        self.writes.append((address, register, data))


class BMP280Tests(unittest.TestCase):
    def test_bosch_reference_compensation(self):
        bus = SensorBus()
        sensor = BMP280(bus, 0x76)
        sensor.start_measurement()
        pressure, temperature = sensor.read_measurement()
        self.assertAlmostEqual(pressure, 100653.25, places=2)
        self.assertAlmostEqual(temperature, 25.08, places=2)
        self.assertIn((0x76, 0xF4, b"\x55"), bus.writes)
        sensor.sleep()
        self.assertEqual(bus.writes[-1], (0x76, 0xF4, b"\x00"))

    def test_wrong_chip_rejected(self):
        bus = SensorBus()
        bus.chip_id = 0x60
        with self.assertRaises(OSError):
            BMP280(bus, 0x77)

    def test_busy_and_skipped_readings_rejected(self):
        bus = SensorBus()
        sensor = BMP280(bus, 0x77)
        bus.status = 8
        with self.assertRaises(OSError):
            sensor.read_measurement()
        bus.status = 0
        bus.raw = (0x80000, 519888)
        with self.assertRaises(OSError):
            sensor.read_measurement()


class TelemetryTests(unittest.TestCase):
    def test_pair_averages_density_and_profile_coefficient(self):
        statistics = Telemetry()
        statistics.add((100110, 20), (100000, 22))
        statistics.add((100090, 24), (100000, 22))
        payload = statistics.payload(2, 10, 2, True)
        density = 100000 / (287.05 * (22 + 273.15))
        self.assertEqual(payload["samples"], 2)
        self.assertEqual(payload["pressure_pa"], 100050)
        self.assertEqual(payload["temperature_c"], 22)
        self.assertEqual(payload["differential_pressure_pa"], 90)
        self.assertAlmostEqual(payload["wind_m_s"], math.sqrt(90 / density), places=3)
        self.assertEqual(payload["wind_status"], "ok")

    def test_negative_pressure_not_converted_with_absolute_value(self):
        statistics = Telemetry()
        statistics.add((99990, 20), (100000, 20))
        payload = statistics.payload(1, 0, 2, False)
        self.assertIsNone(payload["wind_m_s"])
        self.assertEqual(payload["wind_status"], "negative_dp")

    def test_small_negative_noise_clamped_after_averaging(self):
        statistics = Telemetry()
        statistics.add((99999, 20), (100000, 20))
        payload = statistics.payload(1, 0, 2, False)
        self.assertEqual(payload["wind_m_s"], 0)
        self.assertFalse(payload["wind_calibrated"])
        self.assertEqual(payload["wind_status"], "uncalibrated")

    def test_no_measurements_reported_as_null(self):
        payload = Telemetry().payload(1, 0, 2, False)
        self.assertIsNone(payload["wind_m_s"])
        self.assertIsNone(payload["pressure_pa"])
        self.assertEqual(payload["wind_status"], "no_samples")
