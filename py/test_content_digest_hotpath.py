"""Persisted handle digests must make result hydration independent of row size."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import compact
import corpusdb


def _row(session: str, text: str, digest: str | None = None) -> tuple:
    base = (session, 0, 1, "codex", "agrep", "", "", "unknown", "user", text)
    return base if digest is None else (*base, digest)


class _CorpusFixture:
    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        return db

    @staticmethod
    def _index(db: sqlite3.Connection) -> None:
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute(
            "INSERT INTO msgs_prose_fts(rowid,text) "
            "SELECT id,text FROM msgs WHERE who <> 'tool'")


class PersistedDigestHotPath(_CorpusFixture, unittest.TestCase):
    def test_large_result_page_never_hashes_text_in_python(self) -> None:
        db = self._db()
        try:
            texts = ["needle " + "x" * (64 * 1024) for _ in range(40)]
            texts.append("needle " + "y" * (1024 * 1024))
            rows = [_row(f"session-{index}", text, compact.content_digest(text))
                    for index, text in enumerate(texts)]
            db.executemany(corpusdb._INS_DIGEST, rows)
            self._index(db)
            with mock.patch.object(
                    corpusdb.compact, "content_digest",
                    side_effect=AssertionError("full text was hashed")):
                started = time.perf_counter()
                result = corpusdb.keyword(
                    db, "needle", len(rows), position_order=False)
                elapsed = time.perf_counter() - started
            self.assertEqual(len(result["hits"]), len(rows))
            self.assertLess(elapsed, 0.5)
            self.assertEqual(
                {hit["content_digest"] for hit in result["hits"]},
                {row[-1] for row in rows})
        finally:
            db.close()


class PublishedDigestValidation(unittest.TestCase):
    def _scan(self, message: dict, reply: dict | None = None):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text(json.dumps(message) + "\n", encoding="utf-8")
            if reply is not None:
                (root / "replies.jsonl").write_text(
                    json.dumps(reply) + "\n", encoding="utf-8")
            with mock.patch.object(corpusdb.common, "DATA_DIR", root), \
                    mock.patch.object(corpusdb.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(corpusdb.common, "setting", return_value="off"):
                return corpusdb._scan()

    def test_legacy_jsonl_without_digest_is_upgraded_in_memory(self) -> None:
        message = {
            "id": "codex:s:0", "agent": "codex", "project": "agrep",
            "session": "s", "turn": 0, "ts": 1, "who": "user",
            "text": "legacy prompt",
        }
        reply = {"id": "codex:s:0", "reply": "legacy reply"}
        rows = self._scan(message, reply)["s"]
        self.assertEqual(rows[0][-1], compact.content_digest("legacy prompt"))
        self.assertEqual(rows[1][-1], compact.content_digest("legacy reply"))

    def test_malformed_published_digest_aborts_the_generation(self) -> None:
        message = {
            "id": "codex:s:0", "agent": "codex", "project": "agrep",
            "session": "s", "turn": 0, "ts": 1, "who": "user",
            "text": "poisoned", "content_digest": "123X",
        }
        with self.assertRaisesRegex(
                corpusdb._SourceMoved, "invalid content digest"):
            self._scan(message)


class IndexedDigestValidation(_CorpusFixture, unittest.TestCase):

    def test_schema12_without_digest_is_never_promoted_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                "INSERT INTO meta VALUES('schema','12');"
                "INSERT INTO meta VALUES('stamp','legacy');"
                "CREATE TABLE msgs(id INTEGER PRIMARY KEY,text TEXT);")
            db.commit()
            db.close()
            with mock.patch.object(corpusdb, "DB_PATH", path):
                self.assertIsNone(corpusdb._incremental("current"))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute(
                    "SELECT value FROM meta WHERE key='schema'").fetchone(),
                    ("12",))
                self.assertNotIn(
                    "content_digest",
                    {row[1] for row in db.execute("PRAGMA table_info(msgs)")})

    def test_legacy_null_digest_recomputes_from_full_text(self) -> None:
        db = self._db()
        try:
            db.execute(corpusdb._INS, _row("legacy", "needle legacy"))
            self._index(db)
            with mock.patch.object(
                    corpusdb.compact, "content_digest",
                    wraps=compact.content_digest) as digest:
                hit = corpusdb.keyword(db, "needle", 1)["hits"][0]
            self.assertEqual(hit["content_digest"], compact.content_digest(
                "needle legacy"))
            digest.assert_called_once_with("needle legacy")
        finally:
            db.close()

    def test_malformed_indexed_digest_fails_closed(self) -> None:
        db = self._db()
        try:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute(corpusdb._INS_DIGEST, _row(
                "poisoned", "needle poisoned", "NOT-A-DIGEST"))
            self._index(db)
            with self.assertRaisesRegex(
                    sqlite3.DatabaseError, "invalid content digest"):
                corpusdb.keyword(db, "needle", 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
