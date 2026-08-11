"""Embedded NULs must not make FTS postings depend on the SQLite version."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import compact
import corpusdb


def _row(session: str, turn: int, who: str, text: str) -> tuple:
    return (session, turn, turn, "codex", "agrep", "", "", "unknown",
            who, text, compact.content_digest(text))


class EmbeddedNulFts(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(corpusdb._SCHEMA_SQL)

    def tearDown(self) -> None:
        self.db.close()

    def _publish(self, rows: list[tuple]) -> None:
        corpusdb._insert_index_rows(self.db, rows)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        self.db.execute(
            "INSERT INTO msgs_prose_fts(msgs_prose_fts) VALUES('rebuild')")
        self.db.executescript(corpusdb._TRIGGERS_SQL)

    def _ids(self, table: str, term: str) -> list[int]:
        return [int(row[0]) for row in self.db.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY rowid",
            (term,))]

    def _assert_integrity(self) -> None:
        for table in ("msgs_fts", "msgs_prose_fts"):
            self.db.execute(
                f"INSERT INTO {table}({table}, rank) "
                "VALUES('integrity-check', 1)")

    def test_full_build_publishes_normalized_views_and_raw_rows(self) -> None:
        rows = [
            _row("prose", 1, "user", "alpha abc\0def omega"),
            _row("tool", 1, "tool", "tool ghi\0jkl suffix"),
        ]
        snapshot = corpusdb._SessionFamilySnapshot(
            "family-stamp", frozenset({"prose", "tool"}), {})
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            with mock.patch.object(
                    corpusdb, "_protected_derived_target", return_value=False), \
                    mock.patch.object(corpusdb, "_stamp", return_value="stable"), \
                    mock.patch.object(
                        corpusdb, "_read_session_families", return_value=snapshot), \
                    mock.patch.object(
                        corpusdb, "_scan", return_value={
                            "prose": [rows[0]], "tool": [rows[1]]}), \
                    mock.patch.object(
                        corpusdb, "_read_boundary_stats", return_value=[]), \
                    mock.patch.object(
                        corpusdb.indexd_runtime, "derived_writer_build_id",
                        return_value="a" * 20):
                corpusdb._build(path, "stable")

            # closing(), not the bare connection: sqlite3's context manager
            # never closes, and the open handle breaks tempdir removal on
            # Windows (WinError 32)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute(
                    "SELECT value FROM meta WHERE key='schema'").fetchone(),
                    (corpusdb._SCHEMA,))
                self.assertEqual(list(db.execute(
                    "SELECT text, fts_text FROM msgs ORDER BY id")), [
                        (rows[0][-2], "alpha abc def omega"),
                        (rows[1][-2], "tool ghi jkl suffix"),
                    ])
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM msgs_fts "
                    "WHERE msgs_fts MATCH 'jkl'").fetchone(), (1,))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM msgs_prose_fts "
                    "WHERE msgs_prose_fts MATCH 'jkl'").fetchone(), (0,))
                for table in ("msgs_fts", "msgs_prose_fts"):
                    db.execute(
                        f"INSERT INTO {table}({table}, rank) "
                        "VALUES('integrity-check', 1)")

    def test_sidecar_preserves_raw_text_without_cross_nul_trigrams(self) -> None:
        raw = "alpha abc\0def omega"
        tool = "tool ghi\0jkl suffix"
        self._publish([
            _row("prose", 1, "user", raw),
            _row("tool", 1, "tool", tool),
            _row("plain", 1, "agent", "ordinary prose"),
        ])

        stored = list(self.db.execute(
            "SELECT text, fts_text FROM msgs ORDER BY id"))
        self.assertEqual(stored, [
            (raw, "alpha abc def omega"),
            (tool, "tool ghi jkl suffix"),
            ("ordinary prose", None),
        ])
        self.assertEqual(self._ids("msgs_fts", "def"), [1])
        self.assertEqual(self._ids("msgs_prose_fts", "def"), [1])
        self.assertEqual(self._ids("msgs_fts", "jkl"), [2])
        self.assertEqual(self._ids("msgs_prose_fts", "jkl"), [])
        self.assertEqual(self._ids("msgs_fts", "cde"), [])
        self.assertEqual(self._ids("msgs_fts", "ijk"), [])
        self.assertIn("\\u0000", json.dumps({"text": stored[0][0]}))
        self._assert_integrity()

    def test_incremental_tool_prose_text_and_delete_transitions(self) -> None:
        first = _row("moving", 1, "tool", "before abc\0def suffix")
        self._publish([first])
        self.assertEqual(self._ids("msgs_fts", "def"), [1])
        self.assertEqual(self._ids("msgs_prose_fts", "def"), [])

        prose = _row("moving", 1, "agent", first[-2])
        self.assertEqual(
            corpusdb._apply_session_row_diff(self.db, "moving", [prose]),
            (1, 1))
        self.assertEqual(self._ids("msgs_prose_fts", "def"), [1])

        changed = _row("moving", 1, "agent", "after ghi\0jkl ending")
        self.assertEqual(
            corpusdb._apply_session_row_diff(self.db, "moving", [changed]),
            (1, 1))
        self.assertEqual(self._ids("msgs_fts", "def"), [])
        self.assertEqual(self._ids("msgs_fts", "jkl"), [1])
        self.assertEqual(self.db.execute(
            "SELECT text, fts_text FROM msgs").fetchone(),
            (changed[-2], "after ghi jkl ending"))

        plain = _row("moving", 1, "agent", "plain final ending")
        corpusdb._apply_session_row_diff(self.db, "moving", [plain])
        self.assertEqual(self.db.execute(
            "SELECT text, fts_text FROM msgs").fetchone(),
            (plain[-2], None))
        self.assertEqual(self._ids("msgs_fts", "jkl"), [])
        self.assertTrue(self._ids("msgs_prose_fts", "final"))

        corpusdb._apply_session_row_diff(self.db, "moving", [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM msgs").fetchone(), (0,))
        self._assert_integrity()

    def test_targeted_diff_repairs_a_stale_sidecar(self) -> None:
        row = _row("repair", 1, "user", "alpha abc\0def omega")
        indexed = (*row[:-1], "stale posting text", row[-1])
        self.db.execute(corpusdb._INS_INDEXED, indexed)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        self.db.execute(
            "INSERT INTO msgs_prose_fts(msgs_prose_fts) VALUES('rebuild')")
        self.db.executescript(corpusdb._TRIGGERS_SQL)

        self.assertEqual(self._ids("msgs_fts", "stale"), [1])
        self.assertEqual(
            corpusdb._apply_session_row_diff(self.db, "repair", [row]),
            (0, 0))
        self.assertEqual(self.db.execute(
            "SELECT fts_text FROM msgs").fetchone(), ("alpha abc def omega",))
        self.assertEqual(self._ids("msgs_fts", "stale"), [])
        self.assertEqual(self._ids("msgs_fts", "def"), [1])
        self._assert_integrity()


if __name__ == "__main__":
    unittest.main()
