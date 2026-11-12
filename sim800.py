"""Встроенные MQTT и HTTP SIM800; UART принадлежит одной сетевой задаче."""

import asyncio
import json
import machine
import utime

import config
from remote_settings import parse_settings


class ModemError(Exception):
    pass


class SIM800:
    def __init__(self):
        self.buffer = b""
        self.poweron = self._pin(config.MODEM_POWERON, 1)
        self.reset = self._pin(config.MODEM_RESET, 1 - config.MODEM_RESET_LEVEL)
        self.pwrkey = self._pin(config.MODEM_PWRKEY, 1 - config.MODEM_PWRKEY_ACTIVE)
        self.dtr = self._pin(config.MODEM_DTR, 0)
        self.uart = machine.UART(
            config.MODEM_UART_ID, baudrate=config.MODEM_BAUD,
            tx=machine.Pin(config.MODEM_TX), rx=machine.Pin(config.MODEM_RX),
            timeout=0, timeout_char=0, rxbuf=4096,
        )

    async def connect_network(self):
        await self._wake()
        await self.command("ATE0")
        print("Модем:", b"; ".join(await self.command("ATI")).decode())
        await self.command("AT+CMEE=2")
        await self.command("AT+CSCLK=0")
        await self.command("AT+CFUN=1", 15000)
        if b"+CPIN: READY" not in await self.command("AT+CPIN?"):
            raise ModemError("SIM не готова: проверьте SIM/PIN")
        # Встроенный MQTT требует прошивки с поддержкой SM-команд.
        await self.command("AT+SMCONF=?")
        started = utime.ticks_ms()
        while True:
            reply = await self.command("AT+CGREG?")
            if any(line.startswith(b"+CGREG:") and line.split(b",")[1].strip() in (b"1", b"5")
                   for line in reply if b"," in line):
                break
            if utime.ticks_diff(utime.ticks_ms(), started) >= 120000:
                raise ModemError("Нет регистрации GPRS за 120 секунд")
            await asyncio.sleep_ms(2000)
        await self.disconnect_mqtt()
        reply = await self.command("AT+SAPBR=2,1")
        if any(line.startswith(b"+SAPBR: 1,1,") for line in reply):
            await self.command("AT+SAPBR=0,1", 65000)
        for name, value in (("Contype", "GPRS"), ("APN", config.APN),
                            ("USER", config.APN_USER), ("PWD", config.APN_PASSWORD)):
            await self.command('AT+SAPBR=3,1,"%s",%s' % (name, _quote(value)))
        await self.command("AT+SAPBR=1,1", 85000)
        reply = await self.command("AT+SAPBR=2,1")
        if not any(line.startswith(b"+SAPBR: 1,1,") and b'"0.0.0.0"' not in line for line in reply):
            raise ModemError("GPRS не получил IP-адрес")

    async def connect_mqtt(self, client_id):
        # У SIM800 URL включает порт, а KEEPALIVE отличается от KEEPTIME SIM7000.
        for name, value in (
            ("CID", "1"), ("URL", _quote("%s:%d" % (config.MQTT_HOST, config.MQTT_PORT))),
            ("CLIENTID", _quote(client_id)), ("USERNAME", _quote(config.MQTT_USER)),
            ("PASSWORD", _quote(config.MQTT_PASSWORD)), ("KEEPALIVE", "120"), ("CLEANSS", "1"),
        ):
            await self.command('AT+SMCONF="%s",%s' % (name, value))
        await self.command("AT+SMSSL=0")
        await self.command("AT+SMCONN", 60000)
        if b"+SMSTATE: 1" not in await self.command("AT+SMSTATE?"):
            raise ModemError("Встроенный MQTT не подключён")

    async def publish(self, topic, payload):
        message = json.dumps(payload)
        if len(message.encode()) > 1024:
            raise ValueError("Сообщение MQTT длиннее 1024 байт")
        # SIM800 принимает сообщение внутри AT-команды; синтаксис с длиной
        # и приглашением '>' из примера SIM7000 здесь неприменим.
        reply = await self.command('AT+SMPUB=%s,1,0,%s' % (_quote(topic), _quote(message)))
        deadline = utime.ticks_add(utime.ticks_ms(), 30000)
        while True:
            for line in reply:
                if line.startswith(b"+SMPUB:"):
                    fields = line.split(b":", 1)[1].split(b",")
                    if len(fields) != 2 or int(fields[0]) < 1 or int(fields[1]) != 0:
                        raise ModemError("Встроенный MQTT не подтвердил публикацию")
                    return
            line = await self._line(deadline)
            self._check_error(line)
            reply = [line]

    async def disconnect_mqtt(self):
        if b"+SMSTATE: 1" in await self.command("AT+SMSTATE?"):
            await self.command("AT+SMDISC", 10000)

    async def read_settings(self):
        return await asyncio.wait_for_ms(self._read_settings(), config.HTTP_TIMEOUT_MS)

    async def command(self, text, timeout_ms=5000, success=b"OK"):
        await self._write((text + "\r\n").encode())
        try:
            return await self._expect(success, utime.ticks_add(utime.ticks_ms(), timeout_ms))
        except ModemError as exc:
            # Аргументы AT-команд могут содержать пароли.
            raise ModemError(text.split("=")[0] + ": " + str(exc))

    async def power_off(self):
        """Выключить радиомодуль перед удержанием GPIO и сном ESP32."""
        if self.dtr is not None:
            self.dtr.value(0)
        try:
            await self.command("AT+CPOWD=1", 8000, b"NORMAL POWER DOWN")
            print("SIM800: выключение подтверждено")
        except ModemError as exc:
            print("SIM800: выключение не подтверждено:", exc)
            # Резервный сон по DTR, без импульса PWRKEY вслепую.
            try:
                await self.command("AT+CSCLK=1", 2000)
            except ModemError:
                pass
        finally:
            if self.dtr is not None:
                self.dtr.value(1)
            if self.poweron is not None:
                self.poweron.value(0)
            self.uart.deinit()

    async def _read_settings(self):
        await self.command("AT+HTTPINIT")
        try:
            await self.command('AT+HTTPPARA="CID",1')
            await self.command('AT+HTTPPARA="URL",%s' % _quote(
                "http://%s:%d%s" % (config.HTTP_HOST, config.HTTP_PORT, config.HTTP_PATH),
            ))
            reply = await self.command("AT+HTTPACTION=0")
            deadline = utime.ticks_add(utime.ticks_ms(), config.HTTP_TIMEOUT_MS)
            while not any(line.startswith(b"+HTTPACTION:") for line in reply):
                line = await self._line(deadline)
                self._check_error(line)
                reply = [line]
            action = next(line for line in reply if line.startswith(b"+HTTPACTION:"))
            fields = action.split(b":", 1)[1].split(b",")
            if len(fields) != 3 or int(fields[0]) != 0 or int(fields[1]) != 200:
                raise ModemError("Файл настройки HTTP недоступен")
            length = int(fields[2])
            if not 1 <= length <= config.HTTP_SETTINGS_MAX_BYTES:
                raise ModemError("Недопустимый размер JSON-настроек")
            await self._write(("AT+HTTPREAD=0,%d\r\n" % length).encode())
            while True:
                line = await self._line(deadline)
                self._check_error(line)
                if line.startswith(b"+HTTPREAD:"):
                    if int(line.split(b":", 1)[1]) != length:
                        raise ModemError("Неполное тело HTTP")
                    break
            while len(self.buffer) < length:
                await self._fill(deadline)
            body = self.buffer[:length]
            self.buffer = self.buffer[length:]
            await self._expect(b"OK", deadline)
            return parse_settings(body)
        finally:
            try:
                await self.command("AT+HTTPTERM", 2000)
            except ModemError as exc:
                print("HTTPTERM:", exc)

    async def _wake(self):
        await asyncio.sleep_ms(1000)
        try:
            await self.command("AT", 1000)
            return
        except ModemError:
            pass
        self.pwrkey.value(config.MODEM_PWRKEY_ACTIVE)
        try:
            await asyncio.sleep_ms(config.MODEM_PWRKEY_PULSE_MS)
        finally:
            self.pwrkey.value(1 - config.MODEM_PWRKEY_ACTIVE)
        deadline = utime.ticks_add(utime.ticks_ms(), 20000)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            try:
                await self.command("AT", 1000)
                return
            except ModemError:
                await asyncio.sleep_ms(500)
        raise ModemError("SIM800 не отвечает после PWRKEY")

    async def _write(self, data):
        offset = 0
        deadline = utime.ticks_add(utime.ticks_ms(), 5000)
        while offset < len(data):
            count = self.uart.write(data[offset:offset + 128])
            if count:
                offset += count
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                raise ModemError("Тайм-аут записи UART")
            await asyncio.sleep_ms(0)

    async def _fill(self, deadline):
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            chunk = self.uart.read(512)
            if chunk:
                self.buffer += chunk
                if len(self.buffer) > 4096:
                    raise ModemError("Переполнение буфера UART")
                return
            await asyncio.sleep_ms(10)
        raise ModemError("Тайм-аут ответа SIM800")

    async def _line(self, deadline):
        while True:
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                raise ModemError("Тайм-аут строки SIM800")
            end = self.buffer.find(b"\n")
            if end >= 0:
                line = self.buffer[:end].strip()
                self.buffer = self.buffer[end + 1:]
                if line:
                    return line
            else:
                await self._fill(deadline)

    async def _expect(self, success, deadline):
        lines = []
        while True:
            line = await self._line(deadline)
            self._check_error(line)
            if line == success:
                return lines
            if len(lines) >= 32:
                raise ModemError("Слишком длинный ответ SIM800")
            lines.append(line)

    @staticmethod
    def _check_error(line):
        if line == b"ERROR" or line.startswith((b"+CME ERROR:", b"+CMS ERROR:")):
            raise ModemError(line.decode())

    @staticmethod
    def _pin(number, value):
        if number is not None:
            return machine.Pin(number, machine.Pin.OUT, value=value)
        return None


def _quote(value):
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("AT-строка должна содержать печатные символы ASCII")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
