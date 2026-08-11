"""Process-bound regex regressions for both indexed and JSONL search lanes."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir, publish_derived_generation


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import search  # noqa: E402


class RegexGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._data_backup = tempfile.TemporaryDirectory(
            prefix="agrep-regex-guard-backup-")
        cls._data_existed = common.DATA_DIR.exists()
        cls._backup_path = Path(cls._data_backup.name) / "data"
        if cls._data_existed:
            shutil.copytree(common.DATA_DIR, cls._backup_path, symlinks=True)
            shutil.rmtree(common.DATA_DIR)
        rows = [
            {
                "id": "codex:regex-word:1",
                "agent": "codex",
                "session": "regex-word",
                "turn": 1,
                "ts": 1,
                "who": "user",
                "text": "a" * 28 + "!",
                "reply": "",
            },
            {
                "id": "codex:regex-divider:1",
                "agent": "codex",
                "session": "regex-divider",
                "turn": 1,
                "ts": 2,
                "who": "user",
                "text": "SUMMARY\n" + "=" * 28 + "X",
                "reply": "",
            },
            {
                "id": "codex:regex-safe:1",
                "agent": "codex",
                "session": "regex-safe",
                "turn": 1,
                "ts": 3,
                "who": "user",
                "text": "ordinary TODO marker",
                "reply": "",
            },
        ]
        publish_derived_generation(
            common.DATA_DIR, rows, common, corpusdb,
            signature="regex-guard-generation")
        # swapping the sandbox directory does not swap explore's process-wide
        # caches: after an earlier discovery module warms them, every search
        # here would serve pre-swap rows and the guard never engages
        import explore
        explore._GEN = ("regex-guard-fixture",)
        explore._freshen()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if common.DATA_DIR.exists():
                shutil.rmtree(common.DATA_DIR)
            if cls._data_existed:
                shutil.copytree(cls._backup_path, common.DATA_DIR, symlinks=True)
        finally:
            cls._data_backup.cleanup()
            import explore
            explore._GEN = ("regex-guard-restored",)
            explore._freshen()

    def _assert_timeout(self, pattern: str) -> None:
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"AGREP_REGEX_TIMEOUT_S": "0.15"}):
            with self.assertRaises(search.RegexTimeoutError):
                search.run_query(pattern, mode="regex", limit=40)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_nested_word_quantifier_is_bounded(self) -> None:
        self._assert_timeout(r"(\w+\s*)+$")

    def test_nested_divider_quantifier_is_bounded(self) -> None:
        self._assert_timeout(r"(=+ ?)+SUMMARY")

    def test_safe_regex_preserves_normal_results(self) -> None:
        result = search.run_query(r"TODO|FIXME", mode="regex", limit=40)
        self.assertEqual([hit["session"] for hit in result["hits"]], ["regex-safe"])
        self.assertEqual(result["total"], 1)
        hit = result["hits"][0]
        self.assertEqual(
            hit["_regex_color_snippet"],
            search._hl(hit["snippet"], search._hl_regex(r"TODO|FIXME", True), True))
        self.assertIn("TODO", hit["_regex_compact_snippet"])

    def test_interrupt_terminates_and_joins_worker(self) -> None:
        receive = mock.Mock()
        receive.poll.side_effect = KeyboardInterrupt
        send = mock.Mock()
        process = mock.Mock()
        process.is_alive.side_effect = [True, False]
        context = mock.Mock()
        context.Pipe.return_value = (receive, send)
        context.Process.return_value = process

        with (
            mock.patch("multiprocessing.get_context", return_value=context),
            self.assertRaises(KeyboardInterrupt),
        ):
            search._guarded_regex_query(mock.sentinel.spec)

        process.start.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.join.assert_called_once_with(timeout=1.0)
        receive.close.assert_called_once_with()

    def test_windows_worker_inherits_parent_lifetime_job(self) -> None:
        receive = mock.Mock()
        receive.poll.side_effect = KeyboardInterrupt
        send = mock.Mock()
        process = mock.Mock()
        process.is_alive.side_effect = [True, False]
        context = mock.Mock()
        context.Pipe.return_value = (receive, send)
        context.Process.return_value = process

        with (
            mock.patch.object(search.common, "WIN", True),
            mock.patch.object(
                search.common, "bind_descendants_to_process_lifetime",
                return_value=True) as bind,
            mock.patch("multiprocessing.get_context", return_value=context),
            self.assertRaises(KeyboardInterrupt),
        ):
            search._guarded_regex_query(mock.sentinel.spec)

        bind.assert_called_once_with()
        context.Process.assert_called_once_with(
            target=search._regex_worker_main,
            args=(send, mock.sentinel.spec, search._REGEX_TIMEOUT_S),
            name="agrep-regex",
            daemon=True)

    def test_windows_worker_fails_closed_without_parent_lifetime_job(self) -> None:
        with (
            mock.patch.object(search.common, "WIN", True),
            mock.patch.object(
                search.common, "bind_descendants_to_process_lifetime",
                return_value=False),
            mock.patch("multiprocessing.get_context") as context,
            self.assertRaisesRegex(
                search.RegexWorkerError, "lifetime boundary"),
        ):
            search._guarded_regex_query(mock.sentinel.spec)
        context.assert_not_called()

    @unittest.skipIf(common.WIN, "POSIX timer contract")
    def test_parent_death_does_not_orphan_catastrophic_worker(self) -> None:
        script = """
import multiprocessing.process
import os
from pathlib import Path
from types import SimpleNamespace
import search

search._finalize_query = lambda _spec, _lane: {
    "hits": [{"snippet": "a" * 200000 + "!"}]}
search._keyword_candidates = lambda _spec: None
original_start = multiprocessing.process.BaseProcess.start
def start(process):
    original_start(process)
    Path(os.environ["AGREP_REGEX_PID_FILE"]).write_text(
        str(process.pid), encoding="ascii")
multiprocessing.process.BaseProcess.start = start
search._guarded_regex_query(SimpleNamespace(q="(a+)+$"))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
        env["AGREP_REGEX_TIMEOUT_S"] = "1.0"
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "worker.pid"
            env["AGREP_REGEX_PID_FILE"] = str(pid_file)
            parent = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env)
            child_pid = None
            try:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if pid_file.exists() and pid_file.stat().st_size:
                        break
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text(encoding="ascii"))
                self.assertTrue(common.pid_alive(child_pid))
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=2.0)
                deadline = time.monotonic() + 3.0
                while (
                        common.pid_alive(child_pid)
                        and time.monotonic() < deadline):
                    time.sleep(0.02)
                self.assertFalse(common.pid_alive(child_pid))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=2.0)
                if child_pid is not None and common.pid_alive(child_pid):
                    os.kill(child_pid, signal.SIGKILL)
                if parent.stdout is not None:
                    parent.stdout.close()
                if parent.stderr is not None:
                    parent.stderr.close()

    def test_timeout_is_a_machine_error_with_exit_two(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AGREP_REGEX_TIMEOUT_S": "0.15"}),
            mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(search, "_stream_first_run", return_value=None),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = search.main([r"(\w+\s*)+$", "-E", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "regex-timeout")
        self.assertIn("safety limit", stderr.getvalue())
        # A caller who cannot narrow and cannot raise the budget is stuck; the
        # refusal carries both levers, and the json reason carries the same one.
        self.assertIn("--agent", stderr.getvalue())
        self.assertIn("AGREP_REGEX_TIMEOUT_S", stderr.getvalue())
        self.assertIn("AGREP_REGEX_TIMEOUT_S", payload["error"]["reason"])


if __name__ == "__main__":
    unittest.main()
