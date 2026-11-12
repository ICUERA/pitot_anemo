"""Один цикл измерения, связи и подготовки модема ко сну."""

import asyncio
import binascii
import machine
import math
import utime

import board
import config
from bmp280 import BMP280
from heater import run_heater
from remote_settings import default_settings
from sim800 import SIM800
from telemetry import Telemetry


async def run_cycle():
    modem = None
    sampler = None
    settings = default_settings()
    try:
        _validate_config()
        board.release_sleep_pins()
        board.enable_sensor_power()
        await asyncio.sleep_ms(config.SENSOR_POWER_ON_DELAY_MS)
        board.release_i2c_pins()
        i2c = machine.I2C(
            config.I2C_ID, sda=machine.Pin(config.I2C_SDA), scl=machine.Pin(config.I2C_SCL),
            freq=config.I2C_FREQUENCY,
        )
        try:
            board.enable_battery_power(i2c)
        except OSError as exc:
            print("IP5306: проверьте ревизию платы и питание от батареи:", exc)
        statistics = Telemetry()
        sampler = asyncio.create_task(_sample(i2c, statistics))
        modem = SIM800()
        settings = await asyncio.wait_for_ms(
            _transmit(modem, sampler, i2c, statistics), config.NETWORK_TIMEOUT_MS,
        )
    except Exception as exc:
        print("Ошибка рабочего цикла, сон по умолчанию:", exc)
    finally:
        # Сначала прекращаем владельцев I2C/UART, затем выключаем модем.
        if sampler is not None:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print("Завершение опроса BMP280:", exc)
        if modem is not None:
            try:
                await asyncio.wait_for_ms(modem.power_off(), config.SHUTDOWN_TIMEOUT_MS)
            except Exception as exc:
                print("Завершение работы SIM800:", exc)
    try:
        print("Оттаивание:", await run_heater(settings["heater"]))
    except Exception as exc:
        print("Оттаивание не выполнено:", exc)
    return settings["sleep_minutes"] * 60000


async def _sample(i2c, statistics):
    sensors = []
    started = utime.ticks_ms()
    try:
        while utime.ticks_diff(utime.ticks_ms(), started) < config.SAMPLE_WINDOW_MS:
            tick = utime.ticks_ms()
            try:
                if not sensors:
                    for address in (config.PRESSURE_TOTAL_ADDRESS, config.PRESSURE_STATIC_ADDRESS):
                        sensors.append(BMP280(i2c, address))
                for sensor in sensors:
                    sensor.start_measurement()
                await asyncio.sleep_ms(config.CONVERSION_MS)
                statistics.add(sensors[0].read_measurement(), sensors[1].read_measurement())
            except OSError as exc:
                statistics.errors += 1
                if statistics.errors == 1:
                    print("BMP280: ошибка пары измерений:", exc)
                _sleep_sensors(sensors)
                sensors = []
            remaining = config.SAMPLE_WINDOW_MS - utime.ticks_diff(utime.ticks_ms(), started)
            delay = config.SAMPLE_INTERVAL_MS - utime.ticks_diff(utime.ticks_ms(), tick)
            if remaining > 0:
                await asyncio.sleep_ms(min(remaining, max(0, delay)))
    finally:
        _sleep_sensors(sensors)
    print("BMP280: пар измерений", statistics.samples, "ошибок", statistics.errors)


async def _transmit(modem, sampler, i2c, statistics):
    client_id = "pito-" + binascii.hexlify(machine.unique_id()).decode()
    # При потере соединения за минуту измерений разрешена одна новая сессия.
    for attempt in range(2):
        try:
            await modem.connect_network()
            await modem.connect_mqtt(client_id)
            await sampler
            payload = statistics.payload(
                config.PROFILE_PRESSURE_COEFFICIENT, config.PRESSURE_ZERO_OFFSET_PA,
                config.NEGATIVE_PRESSURE_TOLERANCE_PA, config.WIND_CALIBRATED,
            )
            payload.update(board.read_battery(i2c))
            payload["battery_voltage_v"] = await board.read_battery_voltage()
            payload["device_id"] = client_id
            payload["sample_window_s"] = config.SAMPLE_WINDOW_MS / 1000
            await modem.publish(config.MQTT_TOPIC, payload)
            print("MQTT: публикация подтверждена, топик", config.MQTT_TOPIC)
            try:
                await modem.disconnect_mqtt()
            except Exception as exc:
                print("MQTT: завершение сессии:", exc)
            break
        except Exception as exc:
            print("MQTT: попытка", attempt + 1, "не удалась:", exc)
    await sampler
    try:
        settings = await modem.read_settings()
        print("HTTP: сон на", settings["sleep_minutes"], "минут")
        return settings
    except Exception as exc:
        print("HTTP: сон по умолчанию:", exc)
        return default_settings()


def _sleep_sensors(sensors):
    for sensor in sensors:
        try:
            sensor.sleep()
        except OSError as exc:
            print("BMP280: переход в сон:", exc)


def _validate_config():
    if not (50 <= config.CONVERSION_MS <= config.SAMPLE_INTERVAL_MS <= config.SAMPLE_WINDOW_MS
            < config.NETWORK_TIMEOUT_MS < 2 ** 29):
        raise ValueError("Недопустимые интервалы измерения или связи")
    if not 1 <= config.MIN_SLEEP_MINUTES <= config.DEFAULT_SLEEP_MINUTES <= config.MAX_SLEEP_MINUTES <= 1440:
        raise ValueError("Недопустимые пределы сна")
    if (config.PRESSURE_TOTAL_ADDRESS == config.PRESSURE_STATIC_ADDRESS
            or config.PRESSURE_TOTAL_ADDRESS not in (0x76, 0x77)
            or config.PRESSURE_STATIC_ADDRESS not in (0x76, 0x77)):
        raise ValueError("Нужны разные адреса BMP280: 0x76 и 0x77")
    if (not math.isfinite(config.PROFILE_PRESSURE_COEFFICIENT)
            or config.PROFILE_PRESSURE_COEFFICIENT <= 0
            or not math.isfinite(config.PRESSURE_ZERO_OFFSET_PA)
            or not math.isfinite(config.NEGATIVE_PRESSURE_TOLERANCE_PA)
            or config.NEGATIVE_PRESSURE_TOLERANCE_PA < 0):
        raise ValueError("Недопустимая аэродинамическая калибровка")
