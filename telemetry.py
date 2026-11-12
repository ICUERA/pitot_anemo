"""Усреднение пар измерений и расчёт скорости по перепаду давления."""

import math


class Telemetry:
    def __init__(self):
        self.samples = 0
        self.errors = 0
        self.total_pressure = 0.0
        self.static_pressure = 0.0
        self.differential_pressure = 0.0
        self.temperature = 0.0

    def add(self, total, static):
        # В статистику попадают только полные пары одного цикла измерений.
        self.samples += 1
        self.total_pressure += total[0]
        self.static_pressure += static[0]
        # Сумма малых перепадов сохраняет точность лучше разности больших сумм.
        self.differential_pressure += total[0] - static[0]
        self.temperature += (total[1] + static[1]) / 2

    def payload(self, coefficient, zero_offset, negative_tolerance, calibrated):
        result = {
            "samples": self.samples,
            "sample_errors": self.errors,
            "pressure_total_pa": None,
            "pressure_static_pa": None,
            "pressure_pa": None,
            "temperature_c": None,
            "differential_pressure_pa": None,
            "air_density_kg_m3": None,
            "wind_m_s": None,
            "wind_calibrated": calibrated,
            "wind_status": "no_samples",
        }
        if not self.samples:
            return result
        total = self.total_pressure / self.samples
        static = self.static_pressure / self.samples
        temperature = self.temperature / self.samples
        differential = self.differential_pressure / self.samples - zero_offset
        density = static / (287.05 * (temperature + 273.15))
        result.update({
            "pressure_total_pa": round(total, 3),
            "pressure_static_pa": round(static, 3),
            "pressure_pa": round((total + static) / 2, 3),
            "temperature_c": round(temperature, 2),
            "differential_pressure_pa": round(differential, 3),
            "air_density_kg_m3": round(density, 5),
        })
        if differential < -negative_tolerance:
            result["wind_status"] = "negative_dp"
        else:
            result["wind_m_s"] = round(math.sqrt(2 * max(0, differential) / (density * coefficient)), 3)
            result["wind_status"] = "ok" if calibrated else "uncalibrated"
        return result
