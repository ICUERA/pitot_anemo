# LilyGO T-SIM7000G: BLE Fluger -> MQTT через Beeline.
import asyncio
import aioble
import binascii
import bluetooth
import json
import machine
import math
import utilities  # Тот же utilities.py, что используется в рабочем примере.
import utime

try:
    import esp32
except ImportError:
    esp32 = None

BOOT_TICKS_MS = utime.ticks_ms()

# LilyGO T-SIM7000G (ESP32): UART уже соединён на плате.
# Направления указаны со стороны ESP32.
MODEM_UART_ID = 1
MODEM_TX = 27
MODEM_RX = 26
MODEM_BAUD = 9600
MODEM_PWRKEY = utilities.BOARD_PWRKEY_PIN
MODEM_DTR = utilities.MODEM_DTR_PIN
MODEM_POWERON = getattr(utilities, "BOARD_POWERON_PIN", None)
MODEM_RESET = getattr(utilities, "MODEM_RESET_PIN", None)
MODEM_RESET_LEVEL = getattr(utilities, "MODEM_RESET_LEVEL", None)
# T-SIM7000G: синий пользовательский LED на GPIO12, активный LOW.
BOARD_LED_PIN = getattr(utilities, "BOARD_LED_PIN", 12)
# Последовательность PWRKEY из приложенного рабочего main(3).py.
MODEM_PWRKEY_ACTIVE = 1
MODEM_PWRKEY_PULSE_MS = 200
# 2 = автоматический выбор; 13 = только GSM, если нужен GPRS.
MODEM_NETWORK_MODE = 2
APN = "internet.beeline.ru"
APN_USER = "beeline"
APN_PASSWORD = "beeline"
MQTT_HOST = "212.34.149.112"
MQTT_PORT = 1883  # Обычный MQTT без TLS, как в указанной команде.
MQTT_USER = "avtuser"
MQTT_PASSWORD = "lab415"
MQTT_FREQUENCY_TOPIC = "fluger/frequency"
MQTT_SENSORS_TOPIC = "fluger/sensors"
FREQUENCY_INTERVAL_S = 60
SENSORS_INTERVAL_S = 60
ACTIVE_TIME_MS = 5 * 60 * 1000
# Ожидание свежих показаний BLE; MQTT подключается параллельно.
BLE_WAIT_MS = 60 * 1000
DEEP_SLEEP_MS = 15 * 60 * 1000
SHUTDOWN_TIMEOUT_MS = 12000  # Дополнительное время только на выключение.
TTGO_BAT_ADC_PIN = 35  # ADC1, штатный вход батареи T-SIM7000G.
TTGO_BAT_DIVIDER = 2.0
TTGO_BAT_CALIBRATION = 1.0  # При необходимости уточнить по мультиметру.
TTGO_BAT_SAMPLES = 32

# NTC 10 кОм при 25 C, B=3950 K, постоянный резистор 9800 Ом.
# Подключение: 3V3 -> NTC -> GPIO34 -> резистор 9800 Ом -> GND.
NTC_ADC_PIN = 34
NTC_R_NOMINAL_OHM = 10000.0
NTC_R_FIXED_OHM = 9800.0
NTC_BETA_K = 3950.0
NTC_T_NOMINAL_C = 25.0
NTC_SUPPLY_V = 3.315  # Напряжение питания ДЕЛИТЕЛЯ, не напряжение батареи.
NTC_TO_VCC = True  # NTC в верхнем плече, между 3V3 и GPIO34.
NTC_ADC_SAMPLES = 32
NTC_TEMP_OFFSET_C = 0.0  # Поправка после сверки с эталонным термометром.
# Рабочий калиброванный диапазон ESP32 ADC при ATTN_11DB.
# Вне него отправляем null вместо недостоверной температуры.
NTC_ADC_MIN_V = 0.150
NTC_ADC_MAX_V = 2.450

MAX_SAMPLE_AGE_MS = 120000  # Увеличьте, если S приходит реже двух минут.
# H следует за S примерно через 80 мс. Ждём ограниченное время, чтобы
# отсутствие/ошибка SHT41 не блокировали публикацию BMP180 и уход в сон.
SHT41_WAIT_MS = 2000


class RTCSequence:
    # RTC user memory ESP32 (не flash); весь буфер используется этим приложением.
    # Храним СЛЕДУЮЩИЙ номер до передачи пакета, чтобы не повторить его после сна.
    PREFIX = b"FLUGER_SEQ_V1:"

    def __init__(self):
        self.rtc = machine.RTC()
        raw = self.rtc.memory()
        try:
            body, checksum = raw.rsplit(b":", 1)
            if not body.startswith(self.PREFIX) or int(checksum) != sum(body) % 65536:
                raise ValueError("bad RTC record")
            self.next_seq = int(body[len(self.PREFIX):])
            if self.next_seq < 1:
                raise ValueError("bad sequence")
        except (ValueError, TypeError):
            self.next_seq = 1
            self.save(self.next_seq)
            print("RTC: новый счётчик seq=1")
        else:
            print("RTC: восстановлен следующий seq", self.next_seq)

    def save(self, value):
        body = self.PREFIX + str(value).encode()
        record = body + b":" + str(sum(body) % 65536).encode()
        self.rtc.memory(record)
        if self.rtc.memory() != record:
            raise OSError("Не удалось сохранить seq в RTC")

    def reserve(self):
        value = self.next_seq
        self.save(value + 1)
        self.next_seq = value + 1
        return value


async def read_ttgo_battery():
    # Измеряем перед публикацией sensors; батарея BLE-датчика остаётся в bat.
    result = {"ttgo_bat_v": None}
    try:
        adc = machine.ADC(machine.Pin(TTGO_BAT_ADC_PIN))
        adc.atten(machine.ADC.ATTN_11DB)
        if not hasattr(adc, "read_uv"):
            print("TTGO battery: требуется MicroPython с ADC.read_uv(); отправляем null")
            return result
        # Первые отсчёты отбрасываем после переключения ADC.
        for _ in range(4):
            adc.read_uv()
            await asyncio.sleep_ms(2)
        total_uv = 0
        for _ in range(TTGO_BAT_SAMPLES):
            total_uv += adc.read_uv()
            await asyncio.sleep_ms(2)
        voltage = total_uv / TTGO_BAT_SAMPLES / 1000000 * TTGO_BAT_DIVIDER * TTGO_BAT_CALIBRATION
        if not 0 <= voltage <= 4.5:
            print("TTGO battery: вне диапазона", voltage)
            return result
        result["ttgo_bat_v"] = round(voltage, 3)
    except Exception as exc:
        print("TTGO battery:", exc)
    return result


def ntc_voltage_to_celsius(voltage):
    if not NTC_ADC_MIN_V <= voltage <= NTC_ADC_MAX_V:
        raise ValueError("ADC NTC: %.6f V, допустимо %.3f..%.3f V" %
                         (voltage, NTC_ADC_MIN_V, NTC_ADC_MAX_V))
    if not 0 < voltage < NTC_SUPPLY_V:
        raise ValueError("обрыв/замыкание NTC или неверное питание делителя")
    if NTC_TO_VCC:
        resistance = NTC_R_FIXED_OHM * (NTC_SUPPLY_V - voltage) / voltage
    else:
        resistance = NTC_R_FIXED_OHM * voltage / (NTC_SUPPLY_V - voltage)
    # Beta-модель: 1/T = 1/T0 + ln(R/R0)/B; температуры здесь в кельвинах.
    inverse_t = (1.0 / (NTC_T_NOMINAL_C + 273.15)
                 + math.log(resistance / NTC_R_NOMINAL_OHM) / NTC_BETA_K)
    if inverse_t <= 0:
        raise ValueError("неверные параметры термистора")
    temperature = 1.0 / inverse_t - 273.15 + NTC_TEMP_OFFSET_C
    if not -80 <= temperature <= 150:
        raise ValueError("температура NTC вне допустимого диапазона")
    return temperature


async def read_base_temperature():
    # Измеряем GPIO34 перед каждым MQTT-пакетом sensors; ошибка NTC
    # не мешает отправке остальных показаний и переходу в deep sleep.
    result = {"basest_temp": None}
    try:
        adc = machine.ADC(machine.Pin(NTC_ADC_PIN))
        adc.atten(machine.ADC.ATTN_11DB)
        if not hasattr(adc, "read_uv"):
            print("NTC: требуется MicroPython с ADC.read_uv(); отправляем null")
            return result
        for _ in range(4):
            adc.read_uv()
            await asyncio.sleep_ms(2)
        total_uv = 0
        for _ in range(NTC_ADC_SAMPLES):
            total_uv += adc.read_uv()
            await asyncio.sleep_ms(2)
        voltage = total_uv / NTC_ADC_SAMPLES / 1000000
        print("NTC GPIO%d: ADC=%.6f V; пределы=%.3f..%.3f V; R=%.0f Ом; NTC_TO_VCC=%s" %
              (NTC_ADC_PIN, voltage, NTC_ADC_MIN_V, NTC_ADC_MAX_V,
               NTC_R_FIXED_OHM, NTC_TO_VCC))
        # Контрольный сырой отсчёт помогает отличить насыщение ADC от
        # проблемы пересчёта read_uv(). Диагностика не влияет на результат.
        try:
            if hasattr(adc, "read_u16"):
                print("NTC raw_u16:", adc.read_u16())
        except Exception:
            pass
        result["basest_temp"] = round(ntc_voltage_to_celsius(voltage), 1)
    except Exception as exc:
        print("NTC:", exc)
    return result


def prepare_led_for_sleep():
    if BOARD_LED_PIN is None or BOARD_LED_PIN < 0:
        return
    try:
        led = machine.Pin(BOARD_LED_PIN)
        try:
            led.init(hold=False)
        except (AttributeError, TypeError):
            pass
        led.init(machine.Pin.OUT, value=1)  # LED OFF (active LOW).
        led.init(machine.Pin.IN, pull=None)  # Hi-Z, без внутренней подтяжки.
        try:
            led.init(hold=True)  # Сохраняем INPUT без подтяжек во сне.
        except (AttributeError, TypeError):
            pass
    except Exception as exc:
        print("LED shutdown:", exc)


class ModemError(Exception):
    pass


class SIM7000MQTT:
    def __init__(self):
        # Снимаем удержание GPIO, установленное перед предыдущим deep sleep.
        try:
            esp32.gpio_deep_sleep_hold(False)
            for number in (MODEM_PWRKEY, MODEM_DTR, MODEM_POWERON, MODEM_RESET, BOARD_LED_PIN):
                if number is not None and number >= 0:
                    machine.Pin(number).init(hold=False)
        except (AttributeError, TypeError):
            pass  # Старые прошивки без API удержания GPIO.
        self.hardware_initialized = False
        self.poweron = None
        self.reset_pin = None
        if MODEM_POWERON is not None and MODEM_POWERON >= 0:
            self.poweron = machine.Pin(MODEM_POWERON, machine.Pin.OUT, value=1)
        if (MODEM_RESET is not None and MODEM_RESET >= 0
                and MODEM_RESET_LEVEL is not None):
            self.reset_pin = machine.Pin(MODEM_RESET, machine.Pin.OUT,
                                         value=1 - int(MODEM_RESET_LEVEL))
        self.pwrkey = machine.Pin(MODEM_PWRKEY, machine.Pin.OUT,
                                  value=1 - MODEM_PWRKEY_ACTIVE)
        self.dtr = machine.Pin(MODEM_DTR, machine.Pin.OUT, value=0)
        self.uart = machine.UART(
            MODEM_UART_ID, baudrate=MODEM_BAUD,
            tx=machine.Pin(MODEM_TX), rx=machine.Pin(MODEM_RX),
            timeout=0, timeout_char=0, rxbuf=2048,
        )
        self.sequence = RTCSequence()
        self.client_id = "fluger-" + binascii.hexlify(machine.unique_id()).decode()

    async def power_pulse(self):
        # Как в рабочем примере: LOW 100 мс -> HIGH 100 мс -> LOW.
        self.dtr.value(0)
        self.pwrkey.value(0)
        await asyncio.sleep_ms(1000)
        self.pwrkey.value(1)
        try:
            await asyncio.sleep_ms(MODEM_PWRKEY_PULSE_MS)
        finally:
            self.pwrkey.value(0)

    async def hardware_reset(self):
        if self.reset_pin is None:
            print("SIM7000G: RESET не задан в utilities.py, пропускаем")
            return
        active = int(MODEM_RESET_LEVEL)
        self.reset_pin.value(1 - active)
        await asyncio.sleep_ms(100)
        self.reset_pin.value(active)
        try:
            await asyncio.sleep_ms(2600)
        finally:
            self.reset_pin.value(1 - active)

    async def recover_modem(self):
        if self.reset_pin is not None:
            print("SIM7000G: аппаратный RESET после трёх ошибок CNACT")
            await self.hardware_reset()
        else:
            print("SIM7000G: программный RESET AT+CFUN=1,1")
            try:
                await self.command("AT+CFUN=1,1", 5000)
            except ModemError as exc:
                # При перезапуске модем может не успеть вернуть OK.
                print("RESET:", exc)
        await asyncio.sleep(5)
        started = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), started) < 30000:
            try:
                await self.command("AT", 1000)
                print("SIM7000G: восстановлен ответ AT; повторная настройка сети")
                return
            except ModemError:
                await asyncio.sleep_ms(500)
        raise ModemError("Нет ответа AT после RESET")

    async def ensure_powered(self):
        self.dtr.value(0)
        if not self.hardware_initialized:
            print("SIM7000G: UART 9600, TX=27, RX=26; питание, PWRKEY, RESET")
            if self.poweron is not None:
                self.poweron.value(1)
            await asyncio.sleep(1)
            await self.power_pulse()
            await self.hardware_reset()
            self.hardware_initialized = True
        else:
            # При обычном MQTT-переподключении не сбрасываем работающий модем.
            try:
                await self.command("AT", 1000)
                return
            except ModemError:
                pass
            await self.power_pulse()

        while True:
            started = utime.ticks_ms()
            while True:
                remaining = 30000 - utime.ticks_diff(utime.ticks_ms(), started)
                if remaining <= 0:
                    break
                try:
                    await self.command("AT", min(1000, remaining))
                    print("SIM7000G: модем отвечает")
                    return
                except ModemError:
                    remaining = 30000 - utime.ticks_diff(utime.ticks_ms(), started)
                    if remaining > 0:
                        await asyncio.sleep_ms(min(500, remaining))
            print("SIM7000G: нет ответа за 30 секунд, повторяем PWRKEY")
            await self.power_pulse()

    async def power_off(self):
        # CPOWD отвечает NORMAL POWER DOWN, а не обычным OK.
        # Эта функция вызывается только после отмены MQTT-задачи.
        confirmed = False
        self.dtr.value(0)
        self.pwrkey.value(0)
        try:
            # Прямой запрос: обычная command() может ждать очистки UART.
            await self.write(b"AT+CPOWD=1\r\n")
            started = utime.ticks_ms()
            buf = b""
            while utime.ticks_diff(utime.ticks_ms(), started) < 6000:
                chunk = self.uart.read(512)
                if chunk:
                    buf = (buf + chunk)[-1024:]
                    if b"NORMAL POWER DOWN" in buf:
                        confirmed = True
                        print("SIM7000G: NORMAL POWER DOWN")
                        break
                await asyncio.sleep_ms(20)
            if not confirmed:
                # Не даём PWRKEY вслепую: выключенный модем он может включить.
                # Если AT отвечает, модем включён — выключаем длинным импульсом.
                try:
                    await self.command("AT", 500)
                    self.pwrkey.value(MODEM_PWRKEY_ACTIVE)
                    try:
                        await asyncio.sleep_ms(1500)
                    finally:
                        self.pwrkey.value(1 - MODEM_PWRKEY_ACTIVE)
                    await asyncio.sleep(2)
                except ModemError:
                    pass
        finally:
            self.prepare_sleep_pins()
        if not confirmed and self.poweron is None:
            print("SIM7000G: выключение не подтверждено; нет POWERON для снятия питания")

    def prepare_sleep_pins(self):
        # Каждая операция независима: ошибка одного GPIO не отменяет сон.
        pin_values = [(self.pwrkey, 0), (self.dtr, 1)]
        if self.reset_pin is not None:
            pin_values.append((self.reset_pin, 1 - int(MODEM_RESET_LEVEL)))
        if self.poweron is not None:
            pin_values.append((self.poweron, 0))
        for pin, value in pin_values:
            try:
                pin.value(value)
                pin.init(hold=True)
            except Exception as exc:
                print("GPIO перед сном:", exc)
        try:
            esp32.gpio_deep_sleep_hold(True)
        except (ImportError, AttributeError):
            print("GPIO hold недоступен в этой прошивке")
        try:
            self.uart.deinit()
        except Exception:
            pass

    async def write(self, data):
        offset = 0
        start = utime.ticks_ms()
        while offset < len(data):
            n = self.uart.write(data[offset:offset + 128])
            if n:
                offset += n
            if utime.ticks_diff(utime.ticks_ms(), start) > 5000:
                raise ModemError("UART write timeout")
            await asyncio.sleep_ms(10)

    async def response(self, timeout_ms, prompt=False):
        start = utime.ticks_ms()
        buf = b""
        while utime.ticks_diff(utime.ticks_ms(), start) < timeout_ms:
            chunk = self.uart.read(512)
            if chunk:
                buf += chunk
                lines = [line.strip() for line in buf.split(b"\n")[:-1]]
                if any(line == b"ERROR" or line.startswith(b"+CME ERROR:")
                       or line.startswith(b"+CMS ERROR:") for line in lines):
                    for line in lines:
                        if line == b"ERROR" or line.startswith(b"+CME ERROR:") or line.startswith(b"+CMS ERROR:"):
                            raise ModemError("AT ERROR: " + line.decode())
                if prompt and b">" in buf:
                    return buf.decode()
                if not prompt and b"OK" in lines:
                    return buf.decode()
                if len(buf) > 4096:
                    raise ModemError("AT response too long")
            await asyncio.sleep_ms(20)
        raise ModemError("AT response timeout")

    async def command(self, command, timeout_ms=5000, prompt=False):
        # Только эта фоновая задача имеет доступ к UART модема.
        while self.uart.any():
            self.uart.read(512)
            await asyncio.sleep_ms(0)
        await self.write((command + "\r\n").encode())
        try:
            return await self.response(timeout_ms, prompt)
        except ModemError as exc:
            # Не выводим аргументы команд: там могут быть пароли.
            name = command.split("=")[0]
            if command.startswith("AT+CNACT"):
                name = command  # APN не секрет; различаем =0, =1 и запрос состояния.
            raise ModemError(name + ": " + str(exc))

    async def optional(self, command, timeout_ms=5000):
        try:
            return await self.command(command, timeout_ms)
        except ModemError as exc:
            if "AT ERROR" not in str(exc):
                raise
            return ""

    @staticmethod
    def value(response, prefix):
        for line in response.splitlines():
            if line.strip().startswith(prefix):
                return line.strip()[len(prefix):].strip()
        return ""

    async def online(self):
        reply = await self.command("AT+SMSTATE?")
        return self.value(reply, "+SMSTATE:") == "1"

    async def read_signal_quality(self):
        # Тот же MQTT sender владеет UART: параллельных AT-запросов нет.
        result = {"modem_csq": None, "modem_rssi_dbm": None}
        try:
            reply = await self.command("AT+CSQ", 3000)
        except ModemError as exc:
            # ERROR означает, что запрос завершён; показания можно опустить.
            # При тайм-ауте синхронизация UART не гарантирована: обычный
            # механизм переподключения повторит публикацию.
            if "AT ERROR" not in str(exc):
                raise
            print("CSQ:", exc)
            return result
        try:
            fields = self.value(reply, "+CSQ:").split(",")
            if len(fields) != 2:
                raise ValueError("ожидаются RSSI и BER")
            csq, ber = (int(field.strip()) for field in fields)
            if (csq != 99 and not 0 <= csq <= 31) or (ber != 99 and not 0 <= ber <= 7):
                raise ValueError("значения CSQ вне диапазона")
            result["modem_csq"] = csq
            # Шкала SIM7000 Series AT Command Manual, раздел AT+CSQ.
            # 99 = неизвестно. Крайние значения 0 и 31 — пороги,
            # а не точные измерения: <= -115 и >= -52 dBm соответственно.
            if csq == 0:
                result["modem_rssi_dbm"] = -115
            elif csq == 1:
                result["modem_rssi_dbm"] = -111
            elif csq != 99:
                result["modem_rssi_dbm"] = -114 + 2 * csq
        except (ValueError, TypeError) as exc:
            print("CSQ: некорректный ответ:", exc)
        return result


    async def connect(self):
        print("MQTT: проверка SIM7000G...")
        await self.ensure_powered()
        await self.command("ATE0")
        await self.command("AT+CSCLK=0")
        await self.command("AT+CMEE=2")
        await self.command("AT+CFUN=1", 15000)
        reply = await self.command("AT+CPIN?")
        if self.value(reply, "+CPIN:") != "READY":
            raise ModemError("SIM не готова: проверьте SIM/PIN")
        await self.optional("AT+SMDISC", 60000)
        # Уже неактивный PDP не нужно повторно отключать.
        reply = await self.command("AT+CNACT?")
        pdp_state = self.value(reply, "+CNACT:").split(",")[0].strip()
        if pdp_state == "1":
            try:
                await self.command("AT+CNACT=0", 60000)
            except ModemError:
                # Деактивация могла завершиться, несмотря на ERROR.
                reply = await self.command("AT+CNACT?")
                if self.value(reply, "+CNACT:").split(",")[0].strip() != "0":
                    raise
        elif pdp_state != "0":
            raise ModemError("AT+CNACT?: непонятный ответ " + repr(reply))
        await self.command('AT+CGDCONT=1,"IP","%s"' % APN)
        await self.command('AT+CNCFG=1,"%s","%s","%s",1' %
                           (APN, APN_USER, APN_PASSWORD))  # IPv4, PAP
        await self.command("AT+CNMP=%d" % MODEM_NETWORK_MODE)
        print("MQTT: ожидание регистрации в сети...")
        start = utime.ticks_ms()
        while True:
            registered = False
            for command, prefix in (("AT+CGREG?", "+CGREG:"),
                                    ("AT+CEREG?", "+CEREG:")):
                reply = await self.command(command)
                fields = self.value(reply, prefix).split(",")
                if len(fields) >= 2 and fields[1].strip() in ("1", "5"):
                    registered = True
                    break
            if registered:
                break
            if utime.ticks_diff(utime.ticks_ms(), start) > 180000:
                raise ModemError("Нет регистрации в сети за 180 секунд")
            await asyncio.sleep(3)
        print("MQTT:", (await self.command("AT+CSQ")).strip())
        await self.command('AT+CNACT=1,"%s"' % APN, 60000)
        start = utime.ticks_ms()
        while True:
            reply = await self.command("AT+CNACT?")
            fields = self.value(reply, "+CNACT:").split(",")
            if (len(fields) >= 2 and fields[0].strip() == "1"
                    and fields[1].strip().strip('"') not in ("", "0.0.0.0")):
                break
            if utime.ticks_diff(utime.ticks_ms(), start) > 60000:
                raise ModemError("PDP: IP-адрес не получен")
            await asyncio.sleep(2)
        for command in (
            'AT+SMCONF="URL","%s","%d"' % (MQTT_HOST, MQTT_PORT),
            'AT+SMCONF="CLIENTID","%s"' % self.client_id,
            'AT+SMCONF="USERNAME","%s"' % MQTT_USER,
            'AT+SMCONF="PASSWORD","%s"' % MQTT_PASSWORD,
            'AT+SMCONF="KEEPTIME",60',
            'AT+SMCONF="CLEANSS",1',
            'AT+SMSSL=0',
            'AT+SMPUBHEX=0',
        ):
            await self.command(command)
        await self.command("AT+SMCONN", 90000)
        if not await self.online():
            raise ModemError("MQTT не подключён")
        print("MQTT: подключён")

    async def publish(self, topic, payload):
        payload = payload.copy()
        if topic == MQTT_SENSORS_TOPIC:
            payload.update(await read_ttgo_battery())
            payload.update(await read_base_temperature())
            payload.update(await self.read_signal_quality())
        payload["seq"] = self.sequence.next_seq
        data = json.dumps(payload).encode("utf-8")
        if len(data) > 512:
            raise ModemError("JSON превышает лимит 512 байт")
        # Резервируем номер до отправки: при потере подтверждения он
        # не будет присвоен другому пакету. При ошибках возможны пропуски.
        self.sequence.reserve()
        await self.command('AT+SMPUB="%s",%d,1,0' %
                           (topic, len(data)), 15000, prompt=True)
        # Ровно len(data) байт, без CR/LF и Ctrl+Z после JSON.
        await self.write(data)
        await self.response(90000)


class Telemetry:
    def __init__(self):
        self.reset()

    def reset(self):
        self.data = {}
        self.f_time = None
        self.s_time = None
        self.h_time = None

    def feed(self, msg):
        if not msg or msg[0] not in ("F", "S", "H"):
            return
        kind = msg[0]
        parts = msg[1:].rstrip(";").split(";")
        if kind == "F":
            if len(parts) != 4:
                raise ValueError("F: ожидаются четыре поля")
            values = dict(freq_current=int(parts[0]) / 100,
                          freq_1min=int(parts[1]) / 100,
                          freq_5min=int(parts[2]) / 100, angle=int(parts[3]))
            self.data.update(values)
            self.f_time = utime.ticks_ms()
        elif kind == "S":
            if len(parts) != 3:
                raise ValueError("S: ожидаются три поля")
            values = dict(temp=int(parts[0]) / 10,
                          pressure=int(parts[1]), bat=int(parts[2]))
            self.data.update(values)
            self.s_time = utime.ticks_ms()
            # Новый S открывает новое окно ожидания H. Старые значения SHT41
            # не должны незаметно попадать в следующий цикл измерений.
            self.h_time = None
            for key in ("sht41_temp", "sht41_humidity", "sht41_heater"):
                self.data.pop(key, None)
        else:
            # H<temperature10>;<humidity10>;<heater_used>;\n
            if len(parts) != 3:
                raise ValueError("H: ожидаются три поля")
            temp10, humidity10, heater = (int(part) for part in parts)
            if not -450 <= temp10 <= 1300 or not 0 <= humidity10 <= 1000:
                raise ValueError("H: показания вне диапазона")
            if heater not in (0, 1):
                raise ValueError("H: heater должен быть 0 или 1")
            now = utime.ticks_ms()
            # Привязываем H только к предшествующему S этого же BLE-сеанса.
            if self.s_time is None:
                return
            age = utime.ticks_diff(now, self.s_time)
            if age < 0 or age > SHT41_WAIT_MS:
                return
            self.data.update(sht41_temp=temp10 / 10,
                             sht41_humidity=humidity10 / 10,
                             sht41_heater=heater)
            self.h_time = now

    def snapshot(self, group):
        if group == "frequency":
            stamp = self.f_time
            keys = ("freq_current", "freq_1min", "freq_5min", "angle")
        elif group == "sensors":
            stamp = self.s_time
            keys = ("bat", "temp", "pressure")
        else:
            raise ValueError("Unknown telemetry group")
        now = utime.ticks_ms()
        if stamp is None or utime.ticks_diff(now, stamp) > MAX_SAMPLE_AGE_MS:
            return None
        if group == "sensors":
            if self.h_time is None and utime.ticks_diff(now, stamp) < SHT41_WAIT_MS:
                return None  # Не публикуем S и не засыпаем до получения H/тайм-аута.
            if self.h_time is not None:
                keys += ("sht41_temp", "sht41_humidity", "sht41_heater")
        return {key: self.data[key] for key in keys}


telemetry = Telemetry()


async def mqtt_sender(state):
    modem = None
    connected = False
    retry_s = 5
    cnact_errors = 0
    last_publish = {"frequency": None, "sensors": None}
    schedules = (
        ("frequency", MQTT_FREQUENCY_TOPIC, FREQUENCY_INTERVAL_S),
        ("sensors", MQTT_SENSORS_TOPIC, SENSORS_INTERVAL_S),
    )
    while True:
        try:
            if modem is None:
                modem = SIM7000MQTT()
                state["modem"] = modem
            if not connected:
                await modem.connect()
                connected = True
            if not await modem.online():
                raise ModemError("MQTT-соединение потеряно")
            for group, topic, interval_s in schedules:
                # Свежий снимок каждой группы после ожиданий модема.
                payload = telemetry.snapshot(group)
                now = utime.ticks_ms()
                base_only = False
                if (group == "sensors" and payload is None
                        and not state["sent"]["sensors"]
                        and utime.ticks_diff(now, BOOT_TICKS_MS) >= BLE_WAIT_MS):
                    # snapshot может ждать H после уже полученного S.
                    # В таком случае даём закончиться обычному окну SHT41,
                    # вместо замены свежих показаний посылкой базовой станции.
                    s_time = telemetry.s_time
                    waiting_for_h = (s_time is not None and
                                     0 <= utime.ticks_diff(now, s_time) <= MAX_SAMPLE_AGE_MS)
                    if not waiting_for_h:
                        # publish() добавит свежие ttgo_bat_v, basest_temp, seq.
                        # Не подставляем нули или старые показания nRF.
                        payload = {}
                        base_only = True
                previous = last_publish[group]
                if payload is not None and (previous is None or
                        utime.ticks_diff(now, previous) >= interval_s * 1000):
                    last_publish[group] = utime.ticks_ms()
                    await modem.publish(topic, payload)
                    # Интервал считаем после успеха, при ошибке — от попытки.
                    last_publish[group] = utime.ticks_ms()
                    print("MQTT: данные отправлены в", topic)
                    state["sent"][group] = True
                    if base_only:
                        print("Нет свежих данных nRF — отправлены NTC, батарея базы и сигнал модема")
                    if base_only or (state["sent"]["frequency"] and state["sent"]["sensors"]):
                        state["complete"].set()
                        return  # Нужные публикации подтверждены; выключаем модем.
            retry_s = 5
            cnact_errors = 0
            await asyncio.sleep(5)
        except Exception as exc:
            connected = False
            if "AT+CNACT" in str(exc):
                cnact_errors += 1
            else:
                cnact_errors = 0
            if cnact_errors >= 3 and modem is not None:
                cnact_errors = 0
                try:
                    await modem.recover_modem()
                except Exception as reset_exc:
                    print("Восстановление модема:", reset_exc)
                retry_s = 5
            print("MQTT:", exc, "повтор через", retry_s, "сек.")
            # Храним только последние показания, без очереди старых измерений.
            await asyncio.sleep(retry_s)
            retry_s = min(retry_s * 2, 60)


# --- Конец модуля SIM7000G ---

async def find_sensor():
    # Scan for 5 seconds, in active mode, with very low interval/window (to
    # maximise detection rate).
    async with aioble.scan(5000, interval_us=30000, window_us=30000, active=True) as scanner:
        async for result in scanner:
            # See if it matches our name and the environmental sensing service.
            if "Fluger" in str(result.name()):
                print(f"found {result.name()} {result.services()}")
                return result.device
    return None


async def main():
    while True:
        telemetry.reset()
        device = await find_sensor()
        if not device:
            print("Сенсор не найден, повторный поиск через 2 секунды...")
            await asyncio.sleep(2)
            continue

        try:
            print("Подключение к", device)
            connection = await device.connect()
        except asyncio.TimeoutError:
            print("Таймаут при подключении. Повтор...")
            continue
        except Exception as e:
            print(f"Ошибка подключения: {e}")
            await asyncio.sleep(1)
            continue


        async with connection:
            try:
                temp_service = await connection.service(bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E"))
                if temp_service is None:
                    print("Сервис не найден! Отключение...")
                    continue

                tx_char = await temp_service.characteristic(bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"))
                if tx_char is None:
                    print("Характеристика не найдена! Отключение...")
                    continue

                await tx_char.subscribe(notify=True)
                print("Подписка на уведомления успешна.")

            except Exception as e:
                print(f"Ошибка при поиске сервисов/характеристик: {e}")
                continue


            try:
                while connection.is_connected():
                    data = await tx_char.notified()
                    try:
                        msg = data.decode().strip()
                        telemetry.feed(msg)
                    except (ValueError, IndexError, UnicodeError):
                        print("Некорректный BLE-пакет, пропуск")
                        continue

                    if not (msg.startswith("F") or msg.startswith("S") or msg.startswith("H")):
                        print("Unknown:", msg)

            except Exception as e:

                telemetry.reset()
                print(f"Соединение разорвано: {e}")


        telemetry.reset()
        print("Переподключение через 2 секунды...")
        await asyncio.sleep(2)


async def run():
    state = {
        "modem": None,
        "sent": {"frequency": False, "sensors": False},
        "complete": asyncio.Event(),
    }
    sender = asyncio.create_task(mqtt_sender(state))
    receiver = asyncio.create_task(main())
    print("Цикл: frequency+sensors; без S за", BLE_WAIT_MS // 1000,
          "сек. отправляем NTC/батарею и сигнал модема; максимум 5 минут; сон 15 минут")
    try:
        # sensors публикуется после H либо тайм-аута SHT41. Затем можно спать.
        # Без данных nRF достаточно подтверждённой посылки базовой станции.
        # Общий дедлайн по-прежнему ограничивает сетевые ошибки пятью минутами.
        remaining = ACTIVE_TIME_MS - utime.ticks_diff(utime.ticks_ms(), BOOT_TICKS_MS)
        if remaining > 0:
            try:
                await asyncio.wait_for_ms(state["complete"].wait(), remaining)
                print("Необходимые данные отправлены — ранний уход в сон")
            except asyncio.TimeoutError:
                print("Истекли 5 минут — уходим в сон независимо от результата")
    finally:
        print("Рабочее окно завершено — останавливаем BLE и MQTT")
        sender.cancel()
        receiver.cancel()
        # Не ждём завершения сетевых операций/отмены BLE бесконечно.
        await asyncio.sleep_ms(100)
        modem = state["modem"]
        try:
            try:
                bluetooth.BLE().active(False)
            except Exception as exc:
                print("BLE shutdown:", exc)
            if modem is not None:
                try:
                    await asyncio.wait_for_ms(modem.power_off(), SHUTDOWN_TIMEOUT_MS)
                except Exception as exc:
                    print("Modem shutdown:", exc)
        finally:
            # Уходим спать даже при ERROR, тайм-ауте и отсутствии модема.
            try:
                if modem is not None:
                    modem.prepare_sleep_pins()
                elif MODEM_POWERON is not None and MODEM_POWERON >= 0:
                    # Конструктор мог включить питание и завершиться ошибкой.
                    pin = machine.Pin(MODEM_POWERON, machine.Pin.OUT, value=0)
                    try:
                        pin.init(hold=True)
                        esp32.gpio_deep_sleep_hold(True)
                    except (ImportError, AttributeError, TypeError):
                        pass
            finally:
                prepare_led_for_sleep()
                print("Deep sleep на 15 минут")
                machine.deepsleep(DEEP_SLEEP_MS)


try:
    asyncio.run(run())
except KeyboardInterrupt:
    # При ручном прерывании также выполняем запрошенный цикл сна.
    machine.deepsleep(DEEP_SLEEP_MS)
