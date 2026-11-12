import asyncio
import unittest
from unittest.mock import patch

import support
import config
from sim800 import ModemError, SIM800


class ScriptedUART(support.UART):
    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.pending = b""
        self.commands = []

    def write(self, data):
        # Проверяем обработку неполной записи UART.
        count = min(7, len(data))
        self.pending += data[:count]
        if self.pending.endswith(b"\r\n"):
            text = self.pending[:-2].decode()
            self.commands.append(text)
            self.data += self.replies(text)
            self.pending = b""
        return count


class ModemTests(unittest.IsolatedAsyncioTestCase):
    def modem(self, replies):
        modem = SIM800()
        modem.uart = ScriptedUART(replies)
        return modem

    async def test_publish_waits_for_native_mqtt_success(self):
        for reply in (b"\r\nOK\r\n+SMPUB: 7,0\r\n", b"\r\n+SMPUB: 7,0\r\nOK\r\n"):
            modem = self.modem(lambda text: reply)
            await modem.publish("fluger_p", {"wind_m_s": 3.5})
            self.assertEqual(modem.uart.commands, ['AT+SMPUB="fluger_p",1,0,"{\\"wind_m_s\\": 3.5}"'])

    async def test_publish_failure_is_not_acknowledged(self):
        modem = self.modem(lambda text: b"\r\nOK\r\n+SMPUB: 3,1\r\n")
        with self.assertRaises(ModemError):
            await modem.publish("fluger_p", {"wind_m_s": 3.5})

    async def test_plain_ok_does_not_confirm_publish(self):
        modem = self.modem(lambda text: b"\r\nOK\r\n")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(modem.publish("fluger_p", {}), 0.03)

    async def test_connect_uses_sim800_url_and_keepalive(self):
        modem = self.modem(lambda text: b"\r\n+SMSTATE: 1\r\nOK\r\n" if text == "AT+SMSTATE?" else b"\r\nOK\r\n")
        await modem.connect_mqtt("pito-test")
        self.assertIn('AT+SMCONF="URL","212.34.149.112:1883"', modem.uart.commands)
        self.assertIn('AT+SMCONF="KEEPALIVE",120', modem.uart.commands)
        self.assertIn("AT+SMCONN", modem.uart.commands)

    async def test_network_registration_and_sapbr_setup(self):
        async def wake():
            pass

        opened = False

        def replies(text):
            nonlocal opened
            if text == "ATI":
                return b"\r\nSIM800L\r\nRevision: test\r\nOK\r\n"
            if text == "AT+CPIN?":
                return b"\r\n+CPIN: READY\r\nOK\r\n"
            if text == "AT+CGREG?":
                return b"\r\n+CGREG: 0,5\r\nOK\r\n"
            if text == "AT+SMSTATE?":
                return b"\r\n+SMSTATE: 0\r\nOK\r\n"
            if text == "AT+SAPBR=1,1":
                opened = True
            if text == "AT+SAPBR=2,1":
                return (b'\r\n+SAPBR: 1,1,"10.0.0.1"\r\nOK\r\n' if opened
                        else b'\r\n+SAPBR: 1,3,"0.0.0.0"\r\nOK\r\n')
            return b"\r\nOK\r\n"

        modem = self.modem(replies)
        with patch.object(modem, "_wake", wake):
            await modem.connect_network()
        self.assertIn("AT+SMCONF=?", modem.uart.commands)
        self.assertIn('AT+SAPBR=3,1,"APN","internet.tele2.ru"', modem.uart.commands)
        self.assertEqual(modem.uart.commands[-1], "AT+SAPBR=2,1")

    async def test_unsupported_native_mqtt_fails_with_command_name(self):
        async def wake():
            pass

        modem = self.modem(lambda text: b"\r\n+CPIN: READY\r\nOK\r\n" if text == "AT+CPIN?"
                           else b"\r\nERROR\r\n" if text == "AT+SMCONF=?" else b"\r\nOK\r\n")
        with patch.object(modem, "_wake", wake), self.assertRaisesRegex(ModemError, "AT\\+SMCONF"):
            await modem.connect_network()
        self.assertNotIn("AT+SAPBR=1,1", modem.uart.commands)

    async def test_http_action_before_or_after_ok_and_fragmented_body(self):
        body = b'{"sleep_minutes": 15, "heater": {"enabled": true, "duration_s": 30}}'
        action = b"+HTTPACTION: 0,200,%d\r\n" % len(body)
        for reply in (b"\r\nOK\r\n" + action, b"\r\n" + action + b"OK\r\n"):
            def replies(text):
                if text == "AT+HTTPACTION=0":
                    return reply
                if text.startswith("AT+HTTPREAD="):
                    return b"\r\n+HTTPREAD: %d\r\n" % len(body) + body + b"\r\nOK\r\n"
                return b"\r\nOK\r\n"

            modem = self.modem(replies)
            self.assertEqual(await modem.read_settings(), {
                "sleep_minutes": 15, "heater": {"enabled": True, "duration_s": 30},
            })
            self.assertEqual(modem.uart.commands[-1], "AT+HTTPTERM")

    async def test_missing_or_oversized_http_file_rejected_and_terminated(self):
        for action in (b"0,404,0", b"0,200,4096", b"0,200,0"):
            modem = self.modem(lambda text: b"\r\nOK\r\n+HTTPACTION: " + action + b"\r\n"
                               if text == "AT+HTTPACTION=0" else b"\r\nOK\r\n")
            with self.assertRaises(ModemError):
                await modem.read_settings()
            self.assertEqual(modem.uart.commands[-1], "AT+HTTPTERM")
            self.assertFalse(any(text.startswith("AT+HTTPREAD") for text in modem.uart.commands))

    async def test_error_logs_do_not_expose_command_credentials(self):
        modem = self.modem(lambda text: b"\r\n+CME ERROR: 4\r\n")
        with self.assertRaises(ModemError) as error:
            await modem.command('AT+SMCONF="PASSWORD","private-value"')
        self.assertNotIn("private-value", str(error.exception))
        self.assertIn("AT+SMCONF", str(error.exception))

    async def test_power_off_and_gpio_cleanup(self):
        modem = self.modem(lambda text: b"\r\nNORMAL POWER DOWN\r\n")
        await modem.power_off()
        self.assertEqual(modem.uart.commands, ["AT+CPOWD=1"])
        self.assertEqual(modem.poweron.value(), 0)
        self.assertEqual(modem.dtr.value(), 1)
        self.assertTrue(modem.uart.deinitialized)

    async def test_power_off_error_still_closes_uart_and_power(self):
        modem = self.modem(lambda text: b"\r\nERROR\r\n")
        await modem.power_off()
        self.assertEqual(modem.uart.commands, ["AT+CPOWD=1", "AT+CSCLK=1"])
        self.assertEqual(modem.poweron.value(), 0)
        self.assertTrue(modem.uart.deinitialized)

    async def test_http_timeout_still_terminates_http_service(self):
        modem = self.modem(lambda text: b"\r\nOK\r\n")
        with patch.object(config, "HTTP_TIMEOUT_MS", 30):
            with self.assertRaises(asyncio.TimeoutError):
                await modem.read_settings()
        self.assertEqual(modem.uart.commands[-1], "AT+HTTPTERM")
