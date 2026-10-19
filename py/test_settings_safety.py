from _test_support import isolate_data_dir

isolate_data_dir()
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import index_lock
import ownerfile
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
                self.assertRaises(settings.SettingsError),
            ):
                settings.set_setting("embeddings", "on")
            self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_missing_file_can_be_created(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with mock.patch("settings.SETTINGS_PATH", path):
                settings.set_setting("embeddings", "off")
            self.assertIn('"embeddings": "off"', path.read_text(encoding="utf-8"))
            self.assertFalse((Path(td) / settings.SETTINGS_LOCK_NAME).exists())


class SettingsLockTests(unittest.TestCase):
    """`agrep set` must not queue behind an index run: settings.json has its own lock."""

    def test_setting_write_returns_promptly_while_the_index_lock_is_held(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "settings.json"
            with (
                mock.patch("settings.SETTINGS_PATH", path),
                mock.patch.object(index_lock, "INDEX_LOCK_PATH", root / ".index.lock"),
                index_lock.IndexLock("ingest"),
            ):
                started = time.monotonic()
                settings.set_setting("embeddings", "off")
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "a knob flip waited behind the index lock")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["embeddings"], "off")

    def test_concurrent_writers_serialize_without_losing_updates(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("settings.SETTINGS_PATH", Path(td) / "settings.json"):
            failures: list[BaseException] = []

            def bump(key: str) -> None:
                try:
                    for _ in range(5):
                        settings.update_setting(key, lambda cur: (cur or 0) + 1)
                except BaseException as exc:  # noqa: BLE001 -- surfaced by the assertion
                    failures.append(exc)

            threads = [threading.Thread(target=bump, args=(f"k{i}",)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            for i in range(4):
                self.assertEqual(settings.setting(f"k{i}", 0), 5)

    def test_dead_holder_record_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            lock = path.with_name(settings.SETTINGS_LOCK_NAME)
            dead = b"pid=4000000000 start=unknown token=" + b"0" * 32 + b"\n"
            handle = ownerfile.create_exclusive(lock, dead)
            handle.close()
            stale = time.time() - 60
            os.utime(lock, (stale, stale))
            with mock.patch.object(settings, "pid_alive", return_value=False), \
                    mock.patch("settings.SETTINGS_PATH", path):
                settings.set_setting("embeddings", "off")
            self.assertIn('"embeddings": "off"', path.read_text(encoding="utf-8"))

    def test_live_stranded_holder_record_is_reclaimed_on_reacquire(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            lock = path.with_name(settings.SETTINGS_LOCK_NAME)
            start = settings.process_start_identity(os.getpid()) or "unknown"
            stranded = f"pid={os.getpid()} start={start} token={'1' * 32}\n".encode()
            handle = ownerfile.create_exclusive(lock, stranded)
            handle.close()
            with mock.patch("settings.SETTINGS_PATH", path):
                settings.set_setting("embeddings", "off")
            self.assertIn('"embeddings": "off"', path.read_text(encoding="utf-8"))
            self.assertFalse(lock.exists())

    def test_live_holder_times_out_with_a_settings_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            lock = path.with_name(settings.SETTINGS_LOCK_NAME)
            start = settings.process_start_identity(os.getpid()) or "unknown"
            live = f"pid={os.getpid()} start={start} token={'1' * 32}\n".encode()
            handle = ownerfile.create_held(lock, live, token="1" * 32)
            try:
                with (
                    mock.patch("settings.SETTINGS_PATH", path),
                    mock.patch.object(settings, "SETTINGS_LOCK_TIMEOUT_S", 0.3),
                    self.assertRaises(settings.SettingsError),
                ):
                    settings.set_setting("embeddings", "off")
            finally:
                handle.close()
                ownerfile.forget_held(handle.token)
            self.assertTrue(path.with_name(settings.SETTINGS_LOCK_NAME).exists())
            lock.unlink()


if __name__ == "__main__":
    unittest.main()
