from pathlib import Path
import tempfile
import unittest
from unittest import mock

import settings


class SettingsSafetyTests(unittest.TestCase):
    def test_non_object_reads_use_the_requested_fallback(self):
        for value in ("[]", '"off"', "null"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "settings.json"
                path.write_text(value, encoding="utf-8")
                with mock.patch("settings.SETTINGS_PATH", path):
                    self.assertEqual(settings.setting("embeddings", "fallback"), "fallback")

    def test_invalid_existing_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            original = '{"embeddings": "off"'
            path.write_text(original, encoding="utf-8")
            with (
                mock.patch("settings.SETTINGS_PATH", path),
                mock.patch("settings.IndexLock"),
                self.assertRaises(settings.SettingsError),
            ):
                settings.set_setting("embeddings", "on")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_non_object_existing_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with (
                mock.patch("settings.SETTINGS_PATH", path),
                mock.patch("settings.IndexLock"),
                self.assertRaises(settings.SettingsError),
            ):
                settings.set_setting("embeddings", "on")
            self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_missing_file_can_be_created(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with (
                mock.patch("settings.SETTINGS_PATH", path),
                mock.patch("settings.IndexLock"),
            ):
                settings.set_setting("embeddings", "off")
            self.assertIn('"embeddings": "off"', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
