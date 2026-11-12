"""BMP280: forced mode, компенсация Bosch целочисленным алгоритмом."""

import struct
import utime


class BMP280:
    def __init__(self, i2c, address):
        self.i2c = i2c
        self.address = address
        if self._read(0xD0, 1)[0] != 0x58:
            raise OSError("По адресу 0x%02x не найден BMP280" % address)
        self._write(0xE0, 0xB6)
        utime.sleep_ms(10)
        started = utime.ticks_ms()
        while self._read(0xF3, 1)[0] & 1:
            if utime.ticks_diff(utime.ticks_ms(), started) > 100:
                raise OSError("BMP280: NVM не готова")
            utime.sleep_ms(2)
        self.calibration = struct.unpack("<HhhHhhhhhhhh", self._read(0x88, 24))
        if not self.calibration[0] or not self.calibration[3]:
            raise OSError("BMP280: повреждена калибровка")
        self._write(0xF5, 0)  # Без IIR: усреднение выполняется по окну измерений.

    def start_measurement(self):
        # T x2, P x16, forced mode; максимум преобразования 43.225 мс.
        self._write(0xF4, 0x55)

    def read_measurement(self):
        """Вернуть давление в Па и температуру в °C после преобразования."""
        if self._read(0xF3, 1)[0] & 0x09:
            raise OSError("BMP280: преобразование не завершено")
        raw = self._read(0xF7, 6)
        adc_p = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
        adc_t = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
        if adc_p == 0x80000 or adc_t == 0x80000:
            raise OSError("BMP280: измерение пропущено")
        pressure, temperature = self._compensate(adc_p, adc_t)
        if not 30000 <= pressure <= 110000 or not -40 <= temperature <= 85:
            raise OSError("BMP280: показания вне рабочего диапазона")
        return pressure, temperature

    def sleep(self):
        self._write(0xF4, 0)

    def _read(self, register, count):
        return self.i2c.readfrom_mem(self.address, register, count)

    def _write(self, register, value):
        self.i2c.writeto_mem(self.address, register, bytes((value,)))

    def _compensate(self, adc_p, adc_t):
        t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = self.calibration
        var1 = (((adc_t >> 3) - (t1 << 1)) * t2) >> 11
        var2 = (((((adc_t >> 4) - t1) ** 2) >> 12) * t3) >> 14
        t_fine = var1 + var2
        temperature = ((t_fine * 5 + 128) >> 8) / 100
        var1 = t_fine - 128000
        var2 = var1 * var1 * p6 + ((var1 * p5) << 17) + (p4 << 35)
        var1 = (((var1 * var1 * p3) >> 8) + ((var1 * p2) << 12))
        var1 = (((1 << 47) + var1) * p1) >> 33
        if not var1:
            raise OSError("BMP280: нулевой делитель компенсации")
        pressure = ((((1048576 - adc_p) << 31) - var2) * 3125) // var1
        var1 = (p9 * (pressure >> 13) * (pressure >> 13)) >> 25
        var2 = (p8 * pressure) >> 19
        pressure = ((pressure + var1 + var2) >> 8) + (p7 << 4)
        return pressure / 256, temperature
