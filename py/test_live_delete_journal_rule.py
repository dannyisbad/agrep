"""Adversarial proof substrate for the Windows live DELETE-journal reader."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import corpusdb  # noqa: E402


BUILD_A = "a" * 20
BUILD_B = "b" * 20


def _database(path: Path, *, build_id: str = BUILD_A,
              schema: str | None = None) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO meta VALUES(?, ?)", (
        ("schema", schema or corpusdb._SCHEMA),
        ("build_id", build_id),
    ))
    db.execute("CREATE TABLE payload(id INTEGER PRIMARY KEY, value TEXT)")
    db.executemany(
        "INSERT INTO payload(value) VALUES(?)",
        (("a" * 512,) for _ in range(2048)),
    )
    db.commit()
    db.close()


def _source_bytes(path: Path) -> dict[str, tuple[bytes, int]]:
    result = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        member = Path(f"{path}{suffix}")
        if member.exists():
            result[suffix] = (member.read_bytes(), member.stat().st_mtime_ns)
    return result


def _crash_writer(path: Path) -> None:
    script = "\n".join((
        "import os, sqlite3, sys",
        "db = sqlite3.connect(sys.argv[1])",
        "db.execute('PRAGMA cache_size=5')",
        "db.execute('PRAGMA cache_spill=ON')",
        "db.execute('BEGIN IMMEDIATE')",
        f"db.execute(\"UPDATE meta SET value='{BUILD_B}' "
        "WHERE key='build_id'\")",
        "db.execute(\"UPDATE payload SET value=replace(value, 'a', 'b')\")",
        "os._exit(0)",
    ))
    subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True, capture_output=True)


class LiveDeleteJournalRule(unittest.TestCase):
    def test_live_writer_uses_sqlite_locking_without_copy_or_source_write(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            writer = sqlite3.connect(path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE meta SET value=? WHERE key='build_id'", (BUILD_B,))
            journal = Path(f"{path}-journal")
            self.assertGreater(journal.stat().st_size, 0)
            before = _source_bytes(path)
            try:
                with mock.patch.object(
                    corpusdb, "_try_clone_sqlite_file",
                        side_effect=AssertionError("live reader cloned")):
                    reader = corpusdb._connect_read_snapshot(path, 0)
                try:
                    self.assertEqual(dict(reader.execute(
                        "SELECT key, value FROM meta")), {
                            "schema": corpusdb._SCHEMA,
                            "build_id": BUILD_A,
                        })
                    self.assertNotIsInstance(
                        reader, corpusdb._AliasedConnection)
                    self.assertEqual(
                        reader._source_identity,
                        corpusdb._sqlite_file_identity(path),
                    )
                finally:
                    reader.close()
                self.assertEqual(_source_bytes(path), before)
                self.assertEqual(writer.execute(
                    "SELECT value FROM meta WHERE key='build_id'"
                ).fetchone(), (BUILD_B,))
            finally:
                writer.rollback()
                writer.close()

    def test_short_exclusive_commit_waits_for_sqlite_instead_of_cloning(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            script = "\n".join((
                "import sqlite3, sys, time",
                "db = sqlite3.connect(sys.argv[1], timeout=0)",
                "db.execute('BEGIN EXCLUSIVE')",
                f"db.execute(\"UPDATE meta SET value='{BUILD_B}' "
                "WHERE key='build_id'\")",
                "print('ready', flush=True)",
                "time.sleep(0.05)",
                "db.commit()",
                "db.close()",
            ))
            writer = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(writer.stdout.readline().strip(), "ready")
            started = time.monotonic()
            try:
                with mock.patch.object(
                        corpusdb, "_open", wraps=corpusdb._open) as opened, \
                        mock.patch.object(
                        corpusdb, "_connect_read_alias",
                        side_effect=AssertionError("live contention cloned")):
                    reader = corpusdb._connect_read_snapshot(path, 0.25)
                try:
                    self.assertEqual(
                        reader.execute(
                            "SELECT value FROM meta WHERE key='build_id'"
                        ).fetchone(),
                        (BUILD_B,),
                    )
                    self.assertNotIsInstance(
                        reader, corpusdb._AliasedConnection)
                    self.assertEqual(
                        reader._source_identity,
                        corpusdb._sqlite_file_identity(path),
                    )
                finally:
                    reader.close()
            finally:
                _writer_stdout, writer_stderr = writer.communicate(timeout=2.0)
            self.assertEqual(writer.returncode, 0, writer_stderr)
            self.assertEqual(opened.call_count, 2)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_live_exclusive_timeout_never_enters_dead_journal_recovery(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            writer = sqlite3.connect(path, timeout=0)
            writer.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with mock.patch.object(
                    corpusdb, "_connect_read_alias",
                    side_effect=AssertionError("live contention cloned")):
                try:
                    with self.assertRaises(sqlite3.DatabaseError) as raised:
                        corpusdb._connect_read_snapshot(path, 0.01)
                    code = getattr(
                        raised.exception, "sqlite_errorcode", None)
                    if type(code) is int:
                        self.assertEqual(
                            corpusdb.query_database_error_kind(
                                raised.exception),
                            "transient",
                        )
                    else:
                        # 3.10 has no code; the busy raise routes by prose
                        self.assertLess(sys.version_info, (3, 11))
                        self.assertTrue(
                            corpusdb.sqlite_failure_is_contention(
                                raised.exception))
                finally:
                    writer.rollback()
                    writer.close()
                self.assertLess(time.monotonic() - started, 0.25)
                reader = corpusdb._connect_read_snapshot(path, 0)
                try:
                    self.assertEqual(
                        reader.execute(
                            "SELECT value FROM meta WHERE key='build_id'"
                        ).fetchone(),
                        (BUILD_A,),
                    )
                    self.assertNotIsInstance(
                        reader, corpusdb._AliasedConnection)
                finally:
                    reader.close()

    def test_uncoded_sqlite_failure_never_enters_alias_recovery(self) -> None:
        # the coded-world contract: where errors carry codes, an uncoded one
        # is surfaced, never aliased. The genuine 3.10 world (no codes) keeps
        # its alias path - test_python310_uncoded_failure below asserts it.
        error = sqlite3.DatabaseError("unattributed failure")
        self.assertFalse(hasattr(error, "sqlite_errorcode"))
        with mock.patch.object(
                corpusdb, "_SQLITE_ERRORS_HAVE_CODES", True), \
                mock.patch.object(corpusdb, "_open", side_effect=error), \
                mock.patch.object(
                    corpusdb, "_connect_read_alias",
                    side_effect=AssertionError("uncoded failure cloned")):
            with self.assertRaises(sqlite3.DatabaseError) as raised:
                corpusdb._connect_read_snapshot(Path("unused"), 0)
        self.assertIs(raised.exception, error)

    def test_journal_contention_routes_through_the_bounded_reader(self) -> None:
        ownership = corpusdb._DerivedWriteOwnership(
            "refused", journal_blocked=True)
        with mock.patch.object(
                corpusdb, "_query_failure_matches_current", return_value=False), \
                mock.patch.object(
                    corpusdb, "_foreign_stale_db", return_value=None) as stale, \
                mock.patch.object(
                    corpusdb.indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A), \
                mock.patch.object(
                    corpusdb.indexd_runtime, "disclose_foreground_snapshot"):
            result = corpusdb._interactive_snapshot(
                "stamp", repair_required=False, ownership=ownership)
        self.assertIsNone(result)
        stale.assert_called_once_with(
            corpusdb._CONTENDED_READER_WAIT_MS,
            expected_identity=None,
            expected_build_id=None,
        )

    def test_commit_starting_after_ownership_probe_gets_the_same_wait(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            writer = sqlite3.connect(path, timeout=0, check_same_thread=False)
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE payload SET value='committed' WHERE id=1")

            def release() -> None:
                time.sleep(0.05)
                writer.commit()
                writer.close()

            thread = threading.Thread(target=release)
            thread.start()
            started = time.monotonic()
            try:
                with mock.patch.object(corpusdb, "DB_PATH", path), \
                        mock.patch.object(
                            corpusdb, "_query_failure_matches_current",
                            return_value=False), \
                        mock.patch.object(
                            corpusdb.indexd_runtime, "derived_writer_build_id",
                            return_value=BUILD_A), \
                        mock.patch.object(
                            corpusdb.indexd_runtime,
                            "disclose_foreground_snapshot"), \
                        mock.patch.object(
                            corpusdb, "_open", wraps=corpusdb._open) as opened, \
                        mock.patch.object(
                            corpusdb, "_connect_read_alias",
                            side_effect=AssertionError("live contention cloned")):
                    reader = corpusdb._interactive_snapshot(
                        "stamp", repair_required=False,
                        ownership=corpusdb._DerivedWriteOwnership("current"))
                self.assertIsNotNone(reader)
                try:
                    self.assertEqual(
                        reader.execute(
                            "SELECT value FROM payload WHERE id=1"
                        ).fetchone(),
                        ("committed",),
                    )
                    self.assertNotIsInstance(
                        reader, corpusdb._AliasedConnection)
                    self.assertEqual(
                        reader._source_identity,
                        corpusdb._sqlite_file_identity(path),
                    )
                finally:
                    reader.close()
            finally:
                thread.join(1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(opened.call_count, 2)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_dead_hot_journal_refuses_readonly_then_recovers_only_in_alias(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            _crash_writer(path)
            journal = Path(f"{path}-journal")
            self.assertGreater(journal.stat().st_size, 512)
            before = _source_bytes(path)

            with self.assertRaises(sqlite3.DatabaseError) as raised:
                corpusdb._open(path, 0)
            code = getattr(raised.exception, "sqlite_errorcode", None)
            if type(code) is int:
                self.assertEqual(
                    code,
                    getattr(sqlite3, "SQLITE_READONLY_ROLLBACK", 776))
            else:
                self.assertLess(sys.version_info, (3, 11))
            self.assertEqual(_source_bytes(path), before)

            reader = corpusdb._connect_read_snapshot(path, 0)
            try:
                self.assertEqual(reader.execute(
                    "SELECT value FROM meta WHERE key='build_id'"
                ).fetchone(), (BUILD_A,))
                self.assertIsInstance(reader, corpusdb._AliasedConnection)
            finally:
                reader.close()
            self.assertEqual(_source_bytes(path), before)

    def test_python310_uncoded_failure_recovers_only_a_private_alias(
            self) -> None:
        error = sqlite3.DatabaseError("legacy sqlite failure")
        alias = mock.Mock()
        with mock.patch.object(
                corpusdb, "_SQLITE_ERRORS_HAVE_CODES", False), \
                mock.patch.object(
                    corpusdb, "_connect_read_direct", side_effect=error), \
                mock.patch.object(
                    corpusdb, "_connect_read_alias", return_value=alias) as connect:
            self.assertIs(
                corpusdb._connect_read_snapshot(Path("legacy.db"), 0.25),
                alias,
            )
        connect.assert_called_once_with(Path("legacy.db"), 0.25, None)

    def test_dead_hot_journal_retains_the_windows_byte_copy_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path)
            _crash_writer(path)
            before = _source_bytes(path)
            with mock.patch.object(
                    corpusdb, "_try_clone_sqlite_file", return_value=False):
                with self.assertRaises(corpusdb.AliasCloneRefused):
                    corpusdb._connect_read_snapshot(
                        path, 0, max_clone_bytes=1)
            self.assertEqual(_source_bytes(path), before)

    def test_live_reader_exposes_schema_and_build_for_same_snapshot_policy(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            _database(path, build_id=BUILD_B, schema="obsolete")
            writer = sqlite3.connect(path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE meta SET value=? WHERE key='build_id'", (BUILD_A,))
            before = _source_bytes(path)
            try:
                reader = corpusdb._connect_read_snapshot(path, 0)
                try:
                    meta = dict(reader.execute(
                        "SELECT key, value FROM meta "
                        "WHERE key IN ('schema', 'build_id')"))
                finally:
                    reader.close()
                self.assertEqual(meta, {
                    "schema": "obsolete", "build_id": BUILD_B})
                self.assertEqual(_source_bytes(path), before)
            finally:
                writer.rollback()
                writer.close()


if __name__ == "__main__":
    unittest.main()
