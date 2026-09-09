"""Process-bound regex regressions for both indexed and JSONL search lanes."""

from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
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
        cls._index_path = common.DATA_DIR / "regex-guard.db"
        with contextlib.closing(sqlite3.connect(cls._index_path)) as db:
            db.executescript(corpusdb._SCHEMA_SQL)
            db.executemany(corpusdb._INS, (
                (row["session"], row["turn"], row["ts"], row["agent"],
                 "", "", "", "", row["who"], row["text"])
                for row in rows))
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            db.execute(
                "INSERT INTO msgs_prose_fts(rowid, text) SELECT id, text FROM msgs")
            db.commit()

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

    def _indexed_connection(self, **_kwargs):
        return sqlite3.connect(self._index_path)

    def _assert_timeout(self, pattern: str) -> None:
        for indexed in ((False,) if common.WIN else (True, False)):
            with self.subTest(engine="corpusdb" if indexed else "jsonl"):
                workers = []
                original_start = multiprocessing.process.BaseProcess.start

                def start(process):
                    original_start(process)
                    workers.append(process)

                started = time.monotonic()
                with (
                    contextlib.ExitStack() as stack,
                    mock.patch.dict(
                        os.environ, {"AGREP_REGEX_TIMEOUT_S": "0.15"}),
                    mock.patch(
                        "multiprocessing.process.BaseProcess.start", start),
                ):
                    stack.enter_context(mock.patch.object(
                        corpusdb, "connect",
                        side_effect=self._indexed_connection if indexed else None,
                        return_value=None))
                    with self.assertRaises(search.RegexTimeoutError):
                        search.run_query(pattern, mode="regex", limit=40)
                self.assertLess(time.monotonic() - started, 2.0)
                worker = next(p for p in workers if p.name == "agrep-regex")
                self.assertFalse(worker.is_alive())
                self.assertIsNotNone(worker.exitcode)

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

    @unittest.skipIf(common.WIN, "fork-inherited delayed corpus readers")
    def test_progressing_scans_outlive_the_match_budget(self) -> None:
        import explore

        for indexed in (True, False):
            with self.subTest(engine="corpusdb" if indexed else "jsonl"):
                module, name = ((corpusdb, "_candidates") if indexed
                                else (explore, "_iter_kw_corpus"))
                original_rows = getattr(module, name)

                def delayed_rows(*args, **kwargs):
                    for row in original_rows(*args, **kwargs):
                        time.sleep(0.12)
                        yield row

                started = time.monotonic()
                with (
                    contextlib.ExitStack() as stack,
                    mock.patch.dict(
                        os.environ, {"AGREP_REGEX_TIMEOUT_S": "0.05"}),
                    mock.patch.object(module, name, delayed_rows),
                ):
                    stack.enter_context(mock.patch.object(
                        corpusdb, "connect",
                        side_effect=self._indexed_connection if indexed else None,
                        return_value=None))
                    result = search.run_query("TODO", mode="regex", limit=40)
                self.assertGreater(time.monotonic() - started, 0.1)
                self.assertEqual(
                    result["engine"], "corpusdb" if indexed else "jsonl")
                self.assertEqual(
                    [hit["session"] for hit in result["hits"]], ["regex-safe"])
                self.assertEqual((result["total"], result["chats"]), (1, 1))

    def test_interrupt_terminates_and_joins_worker(self) -> None:
        workers = []
        original_start = multiprocessing.process.BaseProcess.start

        def start(process):
            original_start(process)
            workers.append(process)

        with (
            mock.patch("multiprocessing.process.BaseProcess.start", start),
            mock.patch(
                "multiprocessing.connection._ConnectionBase.poll",
                side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            search.run_query("TODO", mode="regex")

        worker = next(p for p in workers if p.name == "agrep-regex")
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(worker.exitcode)


    def test_windows_worker_fails_closed_without_parent_lifetime_job(self) -> None:
        with (
            mock.patch.object(search.common, "WIN", True),
            mock.patch.object(
                search.common, "bind_descendants_to_process_lifetime",
                return_value=False),
            self.assertRaises(search.RegexWorkerError),
        ):
            search._guarded_regex_query(mock.sentinel.spec)

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


class RegexMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        deadline = multiprocessing.get_context().RawValue("d", 0.0)
        search.regex_guard.install(deadline, 1.0)
        self.addCleanup(search.regex_guard.uninstall)

    def test_match_budget_resets_after_success_and_timeout(self) -> None:
        pattern = search.regex_guard.compile("TODO")
        with mock.patch.object(
                search.regex_guard.time, "monotonic",
                side_effect=[0.0, 0.6, 0.7, 1.3, 1.4, 2.5, 2.6, 2.7]):
            self.assertEqual(pattern.search("TODO").span(), (0, 4))
            self.assertEqual(pattern.search("TODO").span(), (0, 4))
            with self.assertRaises(search.regex_guard.MatchTimeoutError):
                pattern.search("TODO")
            self.assertEqual(pattern.search("TODO").span(), (0, 4))

    def test_iteration_budget_excludes_consumer_time(self) -> None:
        pattern = search.regex_guard.compile(r"(?P<word>TODO)|(?=!)")
        with mock.patch.object(
                search.regex_guard.time, "monotonic",
                side_effect=[0.0, 0.6, 2.0, 2.6, 4.0, 4.6]):
            matches = [(m.span(), m.groupdict()) for m in pattern.finditer("TODO!")]
        self.assertEqual(
            matches, [((0, 4), {"word": "TODO"}), ((4, 4), {"word": None})])

    def test_guard_preserves_stdlib_match_and_substitution_semantics(self) -> None:
        query = r"(?P<word>(?-i:TODO))|(?=!)"
        guarded = search.regex_guard.compile(query, re.I)
        native = re.compile(query, re.I)
        self.assertIsNone(guarded.fullmatch("todo"))
        self.assertEqual(guarded.fullmatch("TODO").groupdict(), {"word": "TODO"})
        self.assertIsNone(guarded.match("prefix TODO"))
        self.assertEqual(guarded.search("todo!").span(), (4, 4))
        self.assertEqual(
            guarded.sub(r"<\g<word>>", "TODO todo!"),
            native.sub(r"<\g<word>>", "TODO todo!"))


if __name__ == "__main__":
    unittest.main()
