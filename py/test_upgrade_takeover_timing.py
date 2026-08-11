from __future__ import annotations

from contextlib import ExitStack, closing, nullcontext
import json
import io
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import ownerfile  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"
CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
OLD_BUILD = "a" * 20
OLD_TEXT = "old takeover snapshot needle"
NEW_TEXT = "slow successor publication needle"


class RetainedCorpusPublicationTests(unittest.TestCase):
    OLD_OWNER = "a" * 20
    NEW_OWNER = "b" * 20

    def test_daemon_keeps_only_the_sanitized_takeover_result(self) -> None:
        stderr = (
            "unrelated debug line\n"
            "foreign owner; no live writer holds them - this build took over: "
            "search db retained read-only\x1b[31m\n")
        with mock.patch.object(indexer.common, "log") as logged:
            indexer._log_takeover_result(stderr)
        logged.assert_called_once()
        line = str(logged.call_args.args[0])
        self.assertIn("search db retained read-only", line)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("unrelated", line)

    @staticmethod
    def _forge(path: Path, owner: str, text: str, stamp: str) -> None:
        path.unlink(missing_ok=True)
        db = sqlite3.connect(path)
        try:
            db.executescript(corpusdb._SCHEMA_SQL)
            db.execute(
                "INSERT INTO msgs(session, turn, ts, agent, project, concept, "
                "model, model_source, who, text, content_digest) "
                "VALUES('s', 1, 1, 'codex', 'p', '', '', '', 'user', ?, 'abcd')",
                (text,))
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            db.execute(
                "INSERT INTO msgs_prose_fts(rowid, text) "
                "SELECT id, text FROM msgs WHERE who <> 'tool'")
            db.executescript(corpusdb._TRIGGERS_SQL)
            for key, value in (
                    ("stamp", stamp), ("schema", corpusdb._SCHEMA),
                    ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
                    ("build_id", owner)):
                db.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (key, value))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _forge_schema13(path: Path, owner: str, text: str, stamp: str) -> None:
        path.unlink(missing_ok=True)
        db = sqlite3.connect(path)
        try:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE msgs(
                    id INTEGER PRIMARY KEY,
                    session TEXT NOT NULL, turn INTEGER, ts INTEGER,
                    agent TEXT, project TEXT, concept TEXT, model TEXT,
                    model_source TEXT, who TEXT, text TEXT,
                    content_digest TEXT CHECK(
                        content_digest IS NULL OR
                        (length(content_digest) = 4 AND
                         content_digest NOT GLOB '*[^0-9a-f]*')));
                CREATE INDEX msgs_session ON msgs(session, turn);
                CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
                    WHERE who <> 'tool';
                CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
                CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
                    instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
                    OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
                CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL) WITHOUT ROWID;
                CREATE INDEX session_family_root ON session_family(root);
                CREATE TABLE boundary_stats(
                    token TEXT PRIMARY KEY, n INTEGER NOT NULL,
                    s INTEGER NOT NULL, q INTEGER NOT NULL) WITHOUT ROWID;
                CREATE VIRTUAL TABLE msgs_fts USING fts5(
                    text, content='msgs', content_rowid='id', tokenize='trigram');
                CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                    text, content='msgs', content_rowid='id', tokenize='trigram');
            """)
            db.execute(
                "INSERT INTO msgs(session, turn, ts, agent, project, concept, "
                "model, model_source, who, text, content_digest) "
                "VALUES('s', 1, 1, 'codex', 'p', '', '', '', 'user', ?, 'abcd')",
                (text,))
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            db.execute(
                "INSERT INTO msgs_prose_fts(rowid, text) "
                "SELECT id, text FROM msgs WHERE who <> 'tool'")
            for key, value in (
                    ("stamp", stamp), ("schema", "13"),
                    ("fts_triggers", "3"), ("build_id", owner)):
                db.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (key, value))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _proof(path: Path) -> dict:
        identity = corpusdb._proof_file_identity(path)
        size, modified, changed, _device, _inode = identity
        if corpusdb._PLATFORM_NAME == "posix":
            token = {"Metadata": corpusdb._unix_change_token(changed)}
        elif corpusdb._PLATFORM_NAME == "nt":
            token = {"ContentSha256": list(
                corpusdb._content_sha256(path, identity))}
        else:
            token = {"Metadata": 0}
        return {
            "name": "corpus.db", "len": size, "modified_ns": modified,
            "change_token": token,
            "edge_hash": corpusdb._edge_hash(path, size, identity),
        }

    @classmethod
    def _content_proof(cls, path: Path) -> dict:
        proof = cls._proof(path)
        identity = corpusdb._proof_file_identity(path)
        proof["change_token"] = {
            "ContentSha256": list(corpusdb._content_sha256(path, identity))}
        return proof

    @staticmethod
    def _reader_identity(path: Path) -> dict:
        identity = corpusdb._proof_file_identity(path)
        return dict(zip(
            ("len", "modified_ns", "changed_ns", "device", "inode"),
            identity, strict=True))

    def _ownership_patches(self, path: Path, proof: dict):
        retained = {
            "build_id": self.OLD_OWNER,
            "proof": proof,
            "reader_identity": self._reader_identity(path),
        }
        return (
            mock.patch.object(corpusdb, "DB_PATH", path),
            mock.patch.object(
                indexd_runtime, "derived_writer_build_id",
                return_value=self.NEW_OWNER),
            mock.patch.object(
                indexd_runtime, "derived_owner_info",
                return_value=indexd_runtime.DerivedOwnerInfo(
                    "current", self.NEW_OWNER, None, "", retained)),
            mock.patch.object(
                indexd_runtime, "ingest_cache_owner_info",
                return_value=indexd_runtime.IngestCacheOwnerInfo(
                    "current", self.NEW_OWNER, "")),
            mock.patch.object(indexd_runtime, "assert_python_runtime_unchanged"),
            mock.patch.object(indexd_runtime, "_set_fts_delegated"),
            mock.patch.object(corpusdb, "_consume_changed"),
            mock.patch.object(corpusdb, "_clear_query_rebuild_request"),
            mock.patch.object(corpusdb, "_stamps_equal", side_effect=lambda a, b: a == b),
        )

    @staticmethod
    def _windows_content_patches():
        return (
            mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"),
            mock.patch.object(
                corpusdb, "_windows_file_state", return_value=(123, None)),
        )

    def test_content_hash_is_never_used_by_retained_readers_or_prebuild(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-reader-proof-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._content_proof(path)
            with ExitStack() as stack:
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                for patch in self._windows_content_patches():
                    stack.enter_context(patch)
                hashed = stack.enter_context(mock.patch.object(
                    corpusdb, "_content_sha256",
                    side_effect=AssertionError("reader hashed retained corpus")))
                read_owner = corpusdb._derived_write_ownership()
                write_owner = corpusdb._derived_write_ownership(for_write=True)
                reader = corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=read_owner)
                self.assertIsNotNone(reader)
                reader.close()
                read_only = corpusdb.connect(read_only=True)
                self.assertIsNotNone(read_only)
                read_only.close()
            self.assertTrue(read_owner.replace_retained_db)
            self.assertTrue(write_owner.replace_retained_db)
            hashed.assert_not_called()

    def test_retained_reader_rejects_a_path_swap_after_proof(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-reader-swap-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            with ExitStack() as stack:
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership()
                self.assertTrue(ownership.replace_retained_db)
                self._forge(path, self.OLD_OWNER, "swapped snapshot", "old")
                reader = corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=ownership)
            self.assertIsNone(reader)

    def test_schema13_reader_is_rejected_even_with_retained_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-schema13-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge_schema13(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            with ExitStack() as stack:
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership(for_write=True)
                self.assertTrue(ownership.replace_retained_db)
                reader = corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=ownership)
                self.assertIsNone(reader)
                self.assertIsNone(corpusdb._foreign_stale_db(
                    expected_build_id=self.OLD_OWNER))

    def test_final_retained_validation_hashes_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-final-hash-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._content_proof(path)
            real_hash = corpusdb._content_sha256

            def build(target: Path, _stamp: str) -> None:
                self.assertEqual(hashed.call_count, 0)
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                for patch in self._windows_content_patches():
                    stack.enter_context(patch)
                hashed = stack.enter_context(mock.patch.object(
                    corpusdb, "_content_sha256", wraps=real_hash))
                published = corpusdb._rebuild_and_publish("new", False)
                owner = published.execute(
                    "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
                published.close()
            self.assertEqual(owner, self.NEW_OWNER)
            self.assertEqual(hashed.call_count, 1)

    def test_exact_hash_mismatch_preserves_retained_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-hash-mismatch-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._content_proof(path)
            proof["change_token"]["ContentSha256"][0] ^= 0xFF
            real_hash = corpusdb._content_sha256

            def build(target: Path, _stamp: str) -> None:
                self.assertEqual(hashed.call_count, 0)
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                for patch in self._windows_content_patches():
                    stack.enter_context(patch)
                hashed = stack.enter_context(mock.patch.object(
                    corpusdb, "_content_sha256", wraps=real_hash))
                retained = corpusdb._rebuild_and_publish("new", False)
                text = retained.execute("SELECT text FROM msgs").fetchone()[0]
                retained.close()
            self.assertEqual(text, OLD_TEXT)
            self.assertEqual(hashed.call_count, 1)
            self.assertFalse(path.with_name("corpus.db.building").exists())

    def test_retry_fence_rejects_swap_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-retry-swap-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._content_proof(path)
            real_hash = corpusdb._content_sha256

            def build(target: Path, _stamp: str) -> None:
                self.assertEqual(hashed.call_count, 0)
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")

            def swap_before_retry(_source, target, *, before_attempt=None) -> None:
                before_attempt()
                self._forge(Path(target), self.OLD_OWNER, "retry swap", "old")
                before_attempt()
                self.fail("the cheap retry fence accepted a replacement path")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                stack.enter_context(mock.patch.object(
                    corpusdb.common, "replace_with_retry",
                    side_effect=swap_before_retry))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                for patch in self._windows_content_patches():
                    stack.enter_context(patch)
                hashed = stack.enter_context(mock.patch.object(
                    corpusdb, "_content_sha256", wraps=real_hash))
                retained = corpusdb._rebuild_and_publish("new", False)
                text = retained.execute("SELECT text FROM msgs").fetchone()[0]
                retained.close()
            self.assertEqual(text, "retry swap")
            self.assertEqual(hashed.call_count, 1)
            self.assertFalse(path.with_name("corpus.db.building").exists())

    def test_retained_fts_survives_build_and_publishes_atomically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-corpus-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            before = corpusdb._proof_file_identity(path)
            def source_family() -> tuple:
                sidecars = []
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = Path(f"{path}{suffix}")
                    sidecars.append((
                        suffix, sidecar.exists(),
                        sidecar.read_bytes() if sidecar.exists() else None,
                    ))
                return path.read_bytes(), corpusdb._proof_file_identity(path), tuple(sidecars)

            source_before = source_family()
            built, release = threading.Event(), threading.Event()

            def build(target: Path, _stamp: str) -> None:
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")
                built.set()
                self.assertTrue(release.wait(5.0))

            result: dict[str, object] = {}
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership(for_write=True)
                self.assertTrue(ownership.replace_retained_db)
                self.assertTrue(
                    indexd_runtime.derived_writer_mutation_info().writable)

                def publish() -> None:
                    try:
                        published = corpusdb._rebuild_and_publish("new", False)
                        result["owner"] = published.execute(
                            "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
                        result["hits"] = published.execute(
                            "SELECT count(*) FROM msgs_fts "
                            "WHERE msgs_fts MATCH 'successor'").fetchone()[0]
                        published.close()
                    except BaseException as error:  # noqa: BLE001 - returned to the test thread
                        result["error"] = error

                worker = threading.Thread(target=publish)
                worker.start()
                self.assertTrue(built.wait(5.0))
                self.assertEqual(corpusdb._proof_file_identity(path), before)
                self.assertTrue(path.with_name("corpus.db.building").is_file())
                reader = corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=ownership)
                self.assertIsNotNone(reader)
                try:
                    self.assertEqual(reader.execute(
                        "SELECT count(*) FROM msgs_fts "
                        "WHERE msgs_fts MATCH 'takeover'").fetchone()[0], 1)
                finally:
                    reader.close()
                read_only = corpusdb.connect(read_only=True)
                self.assertIsNotNone(read_only)
                try:
                    self.assertEqual(read_only.execute(
                        "SELECT count(*) FROM msgs_fts "
                        "WHERE msgs_fts MATCH 'takeover'").fetchone()[0], 1)
                finally:
                    read_only.close()
                messages = path.with_name("messages.jsonl")
                rows = [
                    {
                        "id": f"m-{index}", "session": f"new-{index}",
                        "turn": 1, "ts": index + 2, "agent": "codex",
                        "project": "p", "who": "user", "text": NEW_TEXT,
                    }
                    for index in range(2)
                ]
                messages.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")
                import explore
                import search

                search.corpusdb = corpusdb
                explore._messages_by_session_read.cache_clear()
                explore._messages_by_session.cache_clear()
                with mock.patch.object(common, "DATA_DIR", path.parent), \
                        mock.patch.object(common, "MESSAGES_PATH", messages), \
                        mock.patch.object(explore, "_freshen"), \
                        mock.patch.object(
                            explore, "direct_snapshot_attempt",
                            side_effect=lambda **_kwargs: nullcontext()), \
                        mock.patch.object(
                            search, "_jsonl_bounded_single_keyword_rows",
                            return_value=None), \
                        mock.patch.object(
                            search.indexd_runtime, "kick_background_repair",
                            return_value=SimpleNamespace(cause=None)), \
                        mock.patch.object(
                            search.indexd_runtime, "ensure_index",
                            return_value=True):
                    counted = search.run_query(
                        NEW_TEXT, exhaustive=True, include_tools=False,
                        who="user", allow_fallback=False)
                    tiered = search.run_query(
                        NEW_TEXT, limit=0, exhaustive=True,
                        include_tools=False, who="user", allow_fallback=False)
                    absent = search.run_query(
                        "changed-only-absent", exhaustive=True,
                        include_tools=False, who="user", allow_fallback=False)
                    ordinary = search.run_query(
                        OLD_TEXT, include_tools=False, who="user",
                        allow_fallback=False)
                    stale = corpusdb._open(path, 0)
                    stale._retained_snapshot = False
                    stale._source_stamp_current = False
                    with mock.patch.object(
                            corpusdb, "connect", return_value=stale):
                        unmarked_counted = search.run_query(
                            NEW_TEXT, exhaustive=True, include_tools=False,
                            who="user", allow_fallback=False)
                    rendered = {}
                    for flag in ("-c", "--count-by-tier"):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with mock.patch.object(sys, "stdout", stdout), \
                                mock.patch.object(sys, "stderr", stderr):
                            rc = search.main([
                                NEW_TEXT, flag, "--no-auto", "--who", "user"])
                        rendered[flag] = (rc, stdout.getvalue(), stderr.getvalue())
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with mock.patch.object(sys, "stdout", stdout), \
                            mock.patch.object(sys, "stderr", stderr):
                        rc = search.main([
                            "changed-only-absent", "--no-auto", "--who", "user",
                            "--classic"])
                    rendered["miss"] = (rc, stdout.getvalue(), stderr.getvalue())
                explore._messages_by_session_read.cache_clear()
                explore._messages_by_session.cache_clear()
                self.assertEqual(counted["engine"], "jsonl")
                self.assertEqual(counted["total"], 2)
                self.assertTrue(counted.get("totals_exact", True))
                self.assertEqual(
                    sum(search._count_tiers(tiered["hits"]).values()), 2)
                self.assertEqual(tiered["engine"], "jsonl")
                self.assertEqual(absent["engine"], "jsonl")
                self.assertEqual(absent["total"], 0)
                self.assertEqual(ordinary["engine"], "corpusdb", ordinary)
                self.assertEqual(ordinary["total"], 1)
                self.assertEqual(unmarked_counted["engine"], "jsonl")
                self.assertEqual(unmarked_counted["total"], 2)
                self.assertEqual(rendered["-c"][0], 0, rendered["-c"][2])
                self.assertEqual(rendered["-c"][1], "2\n")
                self.assertEqual(
                    rendered["--count-by-tier"][0], 0,
                    rendered["--count-by-tier"][2])
                self.assertIn("total=2", rendered["--count-by-tier"][1])
                self.assertEqual(rendered["miss"][0], 2, rendered["miss"][2])
                self.assertTrue(path.with_name("corpus.db.building").is_file())
                self.assertEqual(source_family(), source_before)
                owner_reader = corpusdb._open(path, 0)
                try:
                    self.assertEqual(owner_reader.execute(
                        "SELECT value FROM meta WHERE key='build_id'").fetchone()[0],
                        self.OLD_OWNER)
                finally:
                    owner_reader.close()
                self.assertEqual(source_family(), source_before)
                release.set()
                worker.join(5.0)

            self.assertNotIn("error", result)
            self.assertEqual(result.get("owner"), self.NEW_OWNER)
            self.assertEqual(result.get("hits"), 1)
            self.assertFalse(path.with_name("corpus.db.building").exists())

    def test_rejected_schema13_uses_jsonl_until_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-schema13-fallback-") as raw:
            root = Path(raw)
            path = root / "corpus.db"
            messages = root / "messages.jsonl"
            messages.write_text(
                "".join(json.dumps({
                    "id": f"m-{index}", "session": f"s-{index}",
                    "turn": 1, "ts": index + 1, "agent": "codex",
                    "project": "p", "who": "user", "text": NEW_TEXT,
                }) + "\n" for index in range(2)),
                encoding="utf-8",
            )
            built, release = threading.Event(), threading.Event()
            result: dict[str, object] = {}

            def build(target: Path, _stamp: str) -> None:
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")
                built.set()
                self.assertTrue(release.wait(5.0))

            def publish() -> None:
                try:
                    published = corpusdb._rebuild_and_publish("new", False)
                    result["owner"] = published.execute(
                        "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
                    published.close()
                except BaseException as error:  # noqa: BLE001 - returned to the test thread
                    result["error"] = error

            with mock.patch.object(corpusdb, "DB_PATH", path), \
                    mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(corpusdb, "_build", side_effect=build), \
                    mock.patch.object(corpusdb, "_stamp", return_value="new"), \
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        return_value={"removed": 0, "removed_bytes": 0}), \
                    mock.patch.object(
                        corpusdb, "_derived_write_ownership",
                        return_value=corpusdb._DerivedWriteOwnership("current")), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_build_id",
                        return_value=self.NEW_OWNER), \
                    mock.patch.object(corpusdb, "_consume_changed"), \
                    mock.patch.object(corpusdb, "_clear_query_rebuild_request"), \
                    mock.patch.object(indexd_runtime, "_set_fts_delegated"):
                worker = threading.Thread(target=publish)
                worker.start()
                self.assertTrue(built.wait(5.0))
                self.assertFalse(path.exists())
                self.assertTrue(path.with_name("corpus.db.building").is_file())

                import explore
                import search

                search.corpusdb = corpusdb
                explore._messages_by_session_read.cache_clear()
                explore._messages_by_session.cache_clear()
                with mock.patch.object(explore, "_freshen"), \
                        mock.patch.object(
                            explore, "direct_snapshot_attempt",
                            side_effect=lambda **_kwargs: nullcontext()), \
                        mock.patch.object(
                            search, "_jsonl_bounded_single_keyword_rows",
                            return_value=None):
                    fallback = search.run_query(
                        NEW_TEXT, exhaustive=True, include_tools=False,
                        who="user", allow_fallback=False)
                self.assertEqual(fallback["engine"], "jsonl")
                self.assertEqual(fallback["total"], 2)
                self.assertTrue(fallback.get("totals_exact", True))

                release.set()
                worker.join(5.0)
                explore._messages_by_session_read.cache_clear()
                explore._messages_by_session.cache_clear()

            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", result)
            self.assertEqual(result.get("owner"), self.NEW_OWNER)
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_name("corpus.db.building").exists())
            # closing(): sqlite3's context manager never closes, and an open
            # handle breaks the tempdir removal on Windows
            with closing(sqlite3.connect(path)) as published:
                self.assertEqual(dict(published.execute(
                    "SELECT key, value FROM meta WHERE key IN ('schema', 'build_id')"
                )), {"schema": corpusdb._SCHEMA, "build_id": self.NEW_OWNER})

    def test_retained_proof_change_blocks_final_replace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-recheck-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            built, release = threading.Event(), threading.Event()

            def build(target: Path, _stamp: str) -> None:
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")
                built.set()
                self.assertTrue(release.wait(5.0))

            result: dict[str, object] = {}
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                def publish() -> None:
                    published = corpusdb._rebuild_and_publish("new", False)
                    result["returned_owner"] = published.execute(
                        "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
                    published.close()

                worker = threading.Thread(target=publish)
                worker.start()
                self.assertTrue(built.wait(5.0))
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                release.set()
                worker.join(5.0)

            self.assertFalse(worker.is_alive())
            database = sqlite3.connect(path)
            try:
                owner = database.execute(
                    "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
            finally:
                database.close()
            self.assertEqual(owner, self.OLD_OWNER)
            self.assertFalse(path.with_name("corpus.db.building").exists())
            self.assertEqual(result.get("returned_owner"), self.OLD_OWNER)

    def test_failed_build_keeps_the_retained_fts_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-retained-crash-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            before = corpusdb._proof_file_identity(path)

            def fail_build(target: Path, _stamp: str) -> None:
                self._forge(target, self.NEW_OWNER, NEW_TEXT, "new")
                raise RuntimeError("simulated builder crash")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    corpusdb, "_build", side_effect=fail_build))
                stack.enter_context(mock.patch.object(
                    corpusdb, "_stamp", return_value="new"))
                stack.enter_context(mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={"removed": 0, "removed_bytes": 0}))
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership(for_write=True)
                with self.assertRaisesRegex(RuntimeError, "builder crash"):
                    corpusdb._rebuild_and_publish("new", False)
                reader = corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=ownership)
                self.assertIsNotNone(reader)
                reader.close()

            self.assertEqual(corpusdb._proof_file_identity(path), before)
            self.assertFalse(path.with_name("corpus.db.building").exists())


@unittest.skipIf(os.name == "nt", "POSIX detached-process timing contract")
class UpgradeTakeoverTimingTests(unittest.TestCase):
    def _env(self, home: Path, data: Path, binary: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "AGREP_DATA_DIR": os.fspath(data),
            "AGREP_HOME": os.fspath(home),
            "HOME": os.fspath(home),
            "USERPROFILE": os.fspath(home),
            "APPDATA": os.fspath(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": os.fspath(home / "AppData" / "Local"),
            "AGREP_RS_BIN": os.fspath(binary),
            "AGREP_INDEXD_IDLE_S": "60",
            "AGREP_DEBUG": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(PY_DIR),
        })
        for name in (
                "AGREP_DATA_READONLY", "AGREP_NO_DAEMON",
                "AGREP_RUNTIME_BUILD_ID", "AGREP_PYTHON_RUNTIME_BUILD_ID"):
            env.pop(name, None)
        return env

    @staticmethod
    def _sources(home: Path, text: str, ts: int, count: int) -> list[Path]:
        tasks = home / ".cline" / "data" / "tasks"
        paths = []
        body = json.dumps([
            {"role": "user", "content": text, "ts": ts},
        ])
        for index in range(count):
            path = tasks / str(index) / "api_conversation_history.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            paths.append(path)
        return paths

    @staticmethod
    def _successor_binary(root: Path) -> Path:
        path = root / "successor-agrep-rs"
        shutil.copy2(RELEASE_BIN, path)
        with path.open("ab") as stream:
            stream.write(b"\nsuccessor-build\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    @staticmethod
    def _pause_owned_ingest(data: Path, found: threading.Event,
                            state: dict[str, object]) -> None:
        while not found.is_set():
            for path in data.glob(".indexd.v*.child.*"):
                try:
                    raw = path.read_text(encoding="ascii")
                except OSError:
                    continue
                match = re.search(
                    r"(?:^| )target=(\d+)(?: |\n).*?"
                    r"target_start=([^ \n]+)(?: |\n)", raw)
                if match is None:
                    continue
                pid = int(match.group(1))
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    continue
                state.update(pid=pid, start=match.group(2), fence=path)
                found.set()
                return
            time.sleep(0.0005)

    @staticmethod
    def _run_search(env: dict[str, str], text: str) -> tuple[
            subprocess.CompletedProcess[str], float]:
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, os.fspath(CLI), "search", text,
             "--json", "--lexical"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20)
        return result, time.perf_counter() - started

    @staticmethod
    def _wait_for(predicate, timeout: float, detail: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError(detail)

    @staticmethod
    def _owner_build(data: Path) -> str | None:
        try:
            payload = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = payload.get("build_id")
        return value if isinstance(value, str) else None

    @staticmethod
    def _json_rows(result: subprocess.CompletedProcess[str]) -> list[dict]:
        records = [json.loads(line) for line in result.stdout.splitlines() if line]
        if not records or records[0].get("kind") != "agrep-meta":
            raise AssertionError("search JSON did not lead with agrep-meta")
        if any(row.get("kind") == "agrep-meta" for row in records[1:]):
            raise AssertionError("search JSON emitted multiple agrep-meta records")
        return records[1:]

    def test_slow_successor_adoption_outlives_foreground_search(self) -> None:
        self.assertTrue(RELEASE_BIN.is_file(), RELEASE_BIN)
        with tempfile.TemporaryDirectory(
                prefix="agrep-upgrade-takeover-timing-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            sources = self._sources(home, OLD_TEXT, 1, 4000)

            initial_env = self._env(home, data, RELEASE_BIN)
            initial_env["AGREP_RUNTIME_BUILD_ID"] = OLD_BUILD
            initial = subprocess.run(
                [os.fspath(RELEASE_BIN), "index", "--agent", "cline"],
                cwd=ROOT, env=initial_env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            self.assertEqual(self._owner_build(data), OLD_BUILD)
            self.assertIn(
                OLD_TEXT,
                (data / "messages.jsonl").read_text(encoding="utf-8"))

            changed = json.dumps([
                {"role": "user", "content": NEW_TEXT, "ts": 2},
            ])
            for source in sources:
                source.write_text(changed, encoding="utf-8")
            successor = self._successor_binary(root)
            env = self._env(home, data, successor)
            paused = threading.Event()
            pause_state: dict[str, object] = {}
            monitor = threading.Thread(
                target=self._pause_owned_ingest,
                args=(data, paused, pause_state), daemon=True)
            monitor.start()
            child_pid = None
            target_pid = None
            continued = False
            guards: list[Path] = []
            child_path = data / "missing-spawn-child"
            try:
                foreground, elapsed = self._run_search(env, OLD_TEXT)
                self.assertEqual(foreground.returncode, 0, foreground.stderr)
                self.assertLess(elapsed, 3.0, foreground.stderr)
                self.assertTrue(any(
                    OLD_TEXT in str(row.get("snippet") or "")
                    for row in self._json_rows(foreground)))
                self._wait_for(
                    paused.is_set, 5.0,
                    "did not pause the exact successor ingest target")
                target_pid = int(pause_state["pid"])
                target_start = str(pause_state["start"])
                self.assertIs(
                    ownerfile.classify_process(
                        target_pid, target_start, pid_alive=common.pid_alive,
                        process_start=common.process_start_identity),
                    ownerfile.ProcessOwner.EXACT_LIVE)
                self.assertEqual(self._owner_build(data), OLD_BUILD)

                guards = list(data.glob(".indexd.v*.spawn"))
                self.assertEqual(len(guards), 1)
                guard_raw = guards[0].read_text(encoding="ascii")
                token_match = re.search(
                    r"(?:^| )token=([0-9a-f]{32})(?:\n| )", guard_raw)
                self.assertIsNotNone(token_match)
                token = token_match.group(1)
                child_path = guards[0].with_name(
                    f"{guards[0].name}.{token}.child")
                child_raw = child_path.read_text(encoding="ascii")
                child_pid = int(re.search(
                    r"(?:^| )pid=(\d+)(?:\n| )", child_raw).group(1))
                child_start = re.search(
                    r"(?:^| )start=([^ \n]+)(?:\n| )", child_raw).group(1)
                self.assertIs(
                    ownerfile.classify_process(
                        child_pid, child_start, pid_alive=common.pid_alive,
                        process_start=common.process_start_identity),
                    ownerfile.ProcessOwner.EXACT_LIVE)

                time.sleep(
                    indexd_runtime._INDEXD_ACQUIRE_WAIT_S
                    + indexd_runtime._INDEXD_READY_WAIT_S + 0.5)
                self.assertEqual(self._owner_build(data), OLD_BUILD)
                self.assertTrue(common.pid_alive(child_pid))

                owner_path = data / ".derived-owner.json"
                locks = list(data.glob(".indexd.v*.lock"))
                self.assertEqual(len(locks), 1)

                def stable_bytes(path: Path) -> tuple[bytes, int, int]:
                    observed = path.stat()
                    return path.read_bytes(), observed.st_dev, observed.st_ino

                before_doctor = {
                    path: stable_bytes(path)
                    for path in (owner_path, guards[0], child_path, locks[0])
                }
                doctor_started = time.perf_counter()
                doctor = subprocess.run(
                    [sys.executable, os.fspath(CLI), "doctor", "--json"],
                    cwd=ROOT, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=20)
                doctor_elapsed = time.perf_counter() - doctor_started
                self.assertEqual(doctor.returncode, 0, doctor.stderr)
                doctor_payload = json.loads(doctor.stdout)
                self.assertTrue(
                    doctor_payload["resources"]["indexd"]["running"],
                    doctor_payload["resources"]["indexd"])
                self.assertLess(doctor_elapsed, 3.0, doctor.stderr)
                self.assertEqual({
                    path: stable_bytes(path)
                    for path in before_doctor
                }, before_doctor)
                self.assertEqual(self._owner_build(data), OLD_BUILD)
                self.assertTrue(common.pid_alive(child_pid))
                os.kill(target_pid, signal.SIGCONT)
                continued = True

                try:
                    self._wait_for(
                        lambda: self._owner_build(data) not in (None, OLD_BUILD),
                        20.0,
                        "successor ingest never published its derived owner")
                except AssertionError as exc:
                    log = data / "indexd.log"
                    detail = (
                        log.read_text(encoding="utf-8") if log.exists() else "")
                    self.fail(
                        f"{exc}; owner={self._owner_build(data)!r}; "
                        f"daemon_alive={common.pid_alive(child_pid)}; "
                        f"log={detail!r}")
                self._wait_for(
                    lambda: (data / "corpus.db").is_file(),
                    10.0, "successor daemon never published corpus.db")
                self._wait_for(
                    lambda: NEW_TEXT in (data / "messages.jsonl").read_text(
                        encoding="utf-8"),
                    10.0,
                    "successor ingest never published the changed transcript")

                debug_env = dict(env)
                debug_env["AGREP_DEBUG"] = "1"
                second, second_elapsed = self._run_search(debug_env, NEW_TEXT)
                self.assertEqual(second.returncode, 0, second.stderr)
                rows = self._json_rows(second)
                self.assertTrue(any(
                    NEW_TEXT in str(row.get("snippet") or "") for row in rows))
                self.assertIn("engine: corpusdb", second.stderr)
                self.assertLess(second_elapsed, 3.0, second.stderr)

                database = sqlite3.connect(data / "corpus.db")
                try:
                    db_owner = database.execute(
                        "SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
                finally:
                    database.close()
                self.assertEqual(db_owner, self._owner_build(data))

                stopped = subprocess.run(
                    [sys.executable, "-c",
                     "import indexd_runtime; "
                     "raise SystemExit(0 if indexd_runtime.stop_indexd_owner("
                     "wait_s=5.0) else 3)"],
                    cwd=PY_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                reclaimed = subprocess.run(
                    [sys.executable, "-c",
                     "import indexd_runtime; "
                     "raise SystemExit(0 if "
                     "indexd_runtime._spawn_guard_resource_status() is None "
                     "else 4)"],
                    cwd=PY_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10)
                self.assertEqual(reclaimed.returncode, 0, reclaimed.stderr)
                self.assertFalse(guards[0].exists())
                self.assertFalse(child_path.exists())
            finally:
                paused.set()
                monitor.join(timeout=1.0)
                if target_pid is not None and not continued:
                    try:
                        os.kill(target_pid, signal.SIGCONT)
                    except OSError:
                        pass
                subprocess.run(
                    [sys.executable, "-c",
                     "import indexd_runtime; "
                     "indexd_runtime.stop_indexd_owner(wait_s=5.0)"],
                    cwd=PY_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                    check=False)


if __name__ == "__main__":
    unittest.main()
