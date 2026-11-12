"""Питание TTGO T-Call и удержание GPIO при глубоком сне."""

import asyncio
import machine

import config

try:
    import esp32
except ImportError:
    esp32 = None


def release_sleep_pins():
    try:
        if esp32 is not None:
            esp32.gpio_deep_sleep_hold(False)
    except AttributeError:
        pass
    # Шина пока остаётся удержанной в LOW, питание датчиков ещё не подано.
    for number in (config.SENSOR_POWER_PIN,) + tuple(_sleep_values()):
        try:
            machine.Pin(number).init(hold=False)
        except (AttributeError, TypeError):
            pass


def enable_battery_power(i2c):
    if config.IP5306_ADDRESS is None:
        return
    # Keep-on нужен и во сне: иначе IP5306 отключит ESP32 при малой нагрузке.
    value = i2c.readfrom_mem(config.IP5306_ADDRESS, 0x00, 1)[0]
    i2c.writeto_mem(config.IP5306_ADDRESS, 0x00, bytes((value | 0x02,)))


def enable_sensor_power():
    machine.Pin(config.SENSOR_POWER_PIN, machine.Pin.OUT, value=1, drive=machine.Pin.DRIVE_3)


def release_i2c_pins():
    """Освободить линии только после включения и стабилизации питания датчиков."""
    for number in (config.I2C_SCL, config.I2C_SDA):
        machine.Pin(number).init(hold=False)


def read_battery(i2c):
    result = {"battery_percent": None, "battery_source": "ip5306"}
    if config.IP5306_ADDRESS is None:
        result["battery_source"] = None
        return result
    try:
        # Индикатор IP5306 имеет четыре ступени, а не точность fuel gauge.
        value = i2c.readfrom_mem(config.IP5306_ADDRESS, 0x78, 1)[0] & 0xF0
        levels = {0xF0: 0, 0xE0: 25, 0xC0: 50, 0x80: 75, 0x00: 100}
        result["battery_percent"] = levels.get(value)
    except OSError as exc:
        print("Батарея IP5306:", exc)
    return result


async def read_battery_voltage(samples=None):
    """Вернуть напряжение ячейки либо None; samples=1 — быстрый контроль нагрузки."""
    if config.BATTERY_ADC_PIN is None:
        return None
    if samples is None:
        samples = config.BATTERY_ADC_SAMPLES
    try:
        adc = machine.ADC(machine.Pin(config.BATTERY_ADC_PIN))
        adc.atten(machine.ADC.ATTN_11DB)
        for _ in range(4):
            adc.read_uv()
            await asyncio.sleep_ms(2)
        total_uv = 0
        for index in range(samples):
            voltage_uv = adc.read_uv()
            if not 150000 <= voltage_uv <= 2450000:
                return None
            total_uv += voltage_uv
            if index + 1 < samples:
                await asyncio.sleep_ms(2)
        voltage = (total_uv / samples / 1000000
                   * config.BATTERY_ADC_DIVIDER * config.BATTERY_ADC_CALIBRATION)
        return voltage if 2.0 <= voltage <= 4.5 else None
    except (OSError, AttributeError, ValueError) as exc:
        print("Напряжение батареи:", exc)
        return None


def disable_heater():
    machine.Pin(config.HEATER_PIN).init(machine.Pin.OUT, pull=None, value=0, hold=False)


def prepare_sleep_pins():
    # Вызывается после остановки всех задач: новых передач I2C уже не будет.
    try:
        _disable_sensor_power()
    except Exception as exc:
        print("Питание BMP280 не выключено: SDA/SCL LOW не подтверждены:", exc)
        try:
            _hold_pin(config.SENSOR_POWER_PIN, 1, machine.Pin.OUT)
        except Exception as exc:
            print("Не удалось удержать питание BMP280:", exc)
    for number, value in _sleep_values().items():
        try:
            pin = machine.Pin(number)
            try:
                pin.init(hold=False)
            except (AttributeError, TypeError):
                pass
            pin.init(machine.Pin.OUT, value=value)
            pin.init(hold=True)
        except Exception as exc:
            print("GPIO перед сном:", number, exc)
    try:
        if esp32 is not None:
            esp32.gpio_deep_sleep_hold(True)
    except AttributeError:
        pass


def _disable_sensor_power():
    # Сначала SCL: падение SDA после него не создаёт START на общей шине.
    scl = _hold_pin(config.I2C_SCL, 0, machine.Pin.OPEN_DRAIN)
    sda = _hold_pin(config.I2C_SDA, 0, machine.Pin.OPEN_DRAIN)
    if scl.value() != 0 or sda.value() != 0:
        raise OSError("SDA или SCL остаётся в HIGH")
    _hold_pin(config.SENSOR_POWER_PIN, 0, machine.Pin.OUT)


def _hold_pin(number, value, mode):
    pin = machine.Pin(number)
    pin.init(hold=False)
    pin.init(mode, pull=None, value=value)
    pin.init(hold=True)
    return pin


def _sleep_values():
    return {number: value for number, value in (
        (config.HEATER_PIN, 0),
        (config.MODEM_PWRKEY, 1 - config.MODEM_PWRKEY_ACTIVE),
        (config.MODEM_DTR, 1),
        (config.MODEM_RESET, 1 - config.MODEM_RESET_LEVEL),
        (config.MODEM_POWERON, 0),
        (config.BOARD_LED_PIN, config.BOARD_LED_OFF),
    ) if number is not None}
