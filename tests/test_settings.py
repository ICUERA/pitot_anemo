import json
import unittest
from pathlib import Path

import config
from remote_settings import default_settings, parse_settings


class SettingsTests(unittest.TestCase):
    def test_example_and_defaults_never_request_heat(self):
        example = Path(__file__).resolve().parents[1] / "flugerp.example.json"
        self.assertEqual(parse_settings(example.read_bytes()), default_settings())
        self.assertEqual(parse_settings(b"{}"), default_settings())

    def test_valid_json_heater_command(self):
        self.assertEqual(parse_settings(b'{"sleep_minutes":15,"heater":{"enabled":true,"duration_s":30}}'), {
            "sleep_minutes": 15, "heater": {"enabled": True, "duration_s": 30},
        })

    def test_invalid_json_and_old_number_format_rejected(self):
        for body in (b"", b"5", b"null", b"[]", b"false", b"<html>error</html>"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_settings(body)

    def test_invalid_sleep_limits_and_types_rejected(self):
        for minutes in (0, 1441, -1, True, 5.0, "5", None):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                parse_settings(json.dumps({"sleep_minutes": minutes}).encode())

    def test_invalid_heater_flags_and_duration_rejected(self):
        for heater in (True, [], "on", {"enabled": "true"}, {"enabled": 1},
                       {"duration_s": 0}, {"duration_s": -1}, {"duration_s": True},
                       {"duration_s": 60.0}, {"duration_s": config.HEATER_MAX_DURATION_S + 1}):
            with self.subTest(heater=heater), self.assertRaises(ValueError):
                parse_settings(json.dumps({"heater": heater}).encode())
