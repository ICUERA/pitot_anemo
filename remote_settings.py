"""Проверка JSON-настроек сна и команды оттаивания, полученных по HTTP."""

import json

import config


def default_settings():
    return {
        "sleep_minutes": config.DEFAULT_SLEEP_MINUTES,
        "heater": {"enabled": False, "duration_s": config.HEATER_DEFAULT_DURATION_S},
    }


def parse_settings(data):
    settings = json.loads(data.decode("utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("Ожидался объект JSON")
    minutes = settings.get("sleep_minutes", config.DEFAULT_SLEEP_MINUTES)
    if type(minutes) is not int or not config.MIN_SLEEP_MINUTES <= minutes <= config.MAX_SLEEP_MINUTES:
        raise ValueError("sleep_minutes должен быть целым числом в допустимых пределах")
    heater = settings.get("heater", {})
    if not isinstance(heater, dict):
        raise ValueError("heater должен быть объектом JSON")
    enabled = heater.get("enabled", False)
    duration = heater.get("duration_s", config.HEATER_DEFAULT_DURATION_S)
    if type(enabled) is not bool:
        raise ValueError("heater.enabled должен быть true или false")
    if type(duration) is not int or not 1 <= duration <= config.HEATER_MAX_DURATION_S:
        raise ValueError("heater.duration_s вне допустимых пределов")
    return {"sleep_minutes": minutes, "heater": {"enabled": enabled, "duration_s": duration}}
