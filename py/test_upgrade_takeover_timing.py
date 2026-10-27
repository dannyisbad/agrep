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

from _test_support import fixture_tree, isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import index_lock  # noqa: E402
import ownerfile  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"
CLI = ROOT / "cli.py"
RELEASE_BIN = Path(os.environ.get("AGREP_RS_BIN") or ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs"))
OLD_BUILD = "a" * 20
OLD_TEXT = "old takeover snapshot needle"
NEW_TEXT = "slow successor publication needle"
# A foreground command that waits on the paused adoption spends at least one
# acquire wait doing so; its own phase report shows that without the
# interpreter start, imports and query work a loaded host inflates.
ADOPTION_WAIT_S = indexd_runtime._INDEXD_ACQUIRE_WAIT_S
COORDINATION_BUDGET_S = ADOPTION_WAIT_S / 2
_PHASE_REPORT = re.compile(r"(?m)^took (\d+\.\d+)s: (.*)$")


def phase_seconds(stderr: str) -> dict[str, float]:
    """Phases of the CLI's forced slow-command report (AGREP_TIMING=1)."""
    match = _PHASE_REPORT.search(stderr)
    if match is None:
        raise AssertionError(f"no phase report in stderr: {stderr!r}")
    phases = {"total": float(match.group(1))}
    for part in match.group(2).split(" · "):
        name, span = part.rsplit(" ", 1)
        phases[name.split("[", 1)[0]] = float(span.rstrip("s"))
    return phases


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

    @classmethod
    def _forge_released(cls, path: Path, owner: str, text: str, stamp: str,
                        schema: str = "15") -> None:
        """What every release through 0.4.2 published: this build's search surface without
        the reply_shape side table, labelled schema 15."""
        cls._forge(path, owner, text, stamp)
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE reply_shape")
            db.execute("UPDATE meta SET value=? WHERE key='schema'", (schema,))
            db.commit()

    def _foreign_owner_patches(self, path: Path):
        """The upgrade's first moments: the previous build still owns every derived store."""
        reason = f"derived stores owned-by {self.OLD_OWNER}; this build is {self.NEW_OWNER}"
        return (
            mock.patch.object(corpusdb, "DB_PATH", path),
            mock.patch.object(
                indexd_runtime, "derived_writer_build_id",
                return_value=self.NEW_OWNER),
            mock.patch.object(
                indexd_runtime, "derived_owner_info",
                return_value=indexd_runtime.DerivedOwnerInfo(
                    "foreign", self.OLD_OWNER, None, reason)),
            mock.patch.object(
                indexd_runtime, "ingest_cache_owner_info",
                return_value=indexd_runtime.IngestCacheOwnerInfo(
                    "foreign", self.OLD_OWNER,
                    f"parse cache owned-by {self.OLD_OWNER}; this build is {self.NEW_OWNER}")),
            mock.patch.object(indexd_runtime, "assert_python_runtime_unchanged"),
            mock.patch.object(indexd_runtime, "_set_fts_delegated"),
            mock.patch.object(corpusdb, "_stamps_equal", side_effect=lambda a, b: a == b),
        )

    def _assert_serves_needle(self, reader) -> None:
        self.assertIsNotNone(reader)
        try:
            hits = corpusdb.keyword(reader, "takeover snapshot needle", 10)["hits"]
            self.assertEqual([(hit["session"], hit["who"]) for hit in hits], [("s", "user")])
            self.assertEqual(
                corpusdb.session_family_roots(reader, ["s"]), {"s": "s"})
        finally:
            reader.close()

    @staticmethod
    def _family(path: Path) -> tuple:
        return (corpusdb._proof_file_identity(path),) + tuple(
            Path(f"{path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))

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
        with fixture_tree(prefix="agrep-retained-reader-proof-") as raw:
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
        with fixture_tree(prefix="agrep-retained-reader-swap-") as raw:
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
        with fixture_tree(prefix="agrep-retained-schema13-") as raw:
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

    def test_released_schema_is_served_read_only_through_the_takeover(self) -> None:
        # 0.4.2 published schema 15; the Rust takeover retains it for readers until the atomic
        # rebuild lands, so every search in that window answers from it - current while its
        # stamp matches, disclosed-stale once the successor republishes - never by a JSONL crawl
        with fixture_tree(prefix="agrep-retained-released-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge_released(path, self.OLD_OWNER, OLD_TEXT, "old")
            proof = self._proof(path)
            before = self._family(path)
            with ExitStack() as stack:
                for patch in self._ownership_patches(path, proof):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership(for_write=True)
                self.assertTrue(ownership.replace_retained_db)
                for stamp, current in (("old", True), ("new", False)):
                    reader = corpusdb._interactive_snapshot(
                        stamp, repair_required=False, ownership=ownership)
                    self.assertIsNotNone(reader, stamp)
                    self.assertTrue(reader._retained_snapshot)
                    self.assertIs(reader._source_stamp_current, current)
                    # the briefing's side table is the one surface the released schema
                    # lacks: its read fails as a database error, which summary answers
                    # from the transcripts, never as an empty shape
                    with self.assertRaises(sqlite3.DatabaseError):
                        corpusdb.reply_shapes(reader, [("s", 1)])
                    self._assert_serves_needle(reader)
                self._assert_serves_needle(corpusdb.connect(read_only=True))
            self.assertEqual(self._family(path), before)

    def test_writers_never_reuse_a_released_schema_database(self) -> None:
        # reading the released schema grants no writer anything: even under this build's own
        # owner its validation fails, so the refresh rebuilds rather than updating it in place
        with fixture_tree(prefix="agrep-released-writer-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge_released(path, self.NEW_OWNER, OLD_TEXT, "old")
            with mock.patch.object(corpusdb, "DB_PATH", path), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_build_id",
                        return_value=self.NEW_OWNER), \
                    mock.patch.object(
                        corpusdb, "_stamps_equal", side_effect=lambda a, b: a == b):
                self.assertIsNone(corpusdb._valid_db("old", 0))
                self._assert_serves_needle(
                    corpusdb._stale_db(0, expected_build_id=self.NEW_OWNER))

    def test_released_schema_is_served_while_the_previous_build_still_owns_it(self) -> None:
        # before the successor's ingest takes over (and under AGREP_NO_DAEMON, for good) the
        # 0.4.2 build still owns the stores: its publication is read-only, and readable
        with fixture_tree(prefix="agrep-foreign-released-") as raw:
            path = Path(raw) / "corpus.db"
            self._forge_released(path, self.OLD_OWNER, OLD_TEXT, "old")
            before = self._family(path)
            with ExitStack() as stack:
                for patch in self._foreign_owner_patches(path):
                    stack.enter_context(patch)
                ownership = corpusdb._derived_write_ownership(for_write=True)
                self.assertFalse(ownership.writable)
                self.assertFalse(ownership.replace_retained_db)
                self._assert_serves_needle(corpusdb._interactive_snapshot(
                    "old", repair_required=False, ownership=ownership))
                self._assert_serves_needle(corpusdb._interactive_snapshot(
                    "new", repair_required=False, ownership=ownership))
                self._assert_serves_needle(corpusdb.connect(read_only=True))
            self.assertEqual(self._family(path), before)

    def test_only_the_released_schema_joins_the_readers(self) -> None:
        # 16 never shipped, so nothing can be relying on it being served; a future schema is
        # a newer build's and a damaged label is no schema at all
        for schema in ("16", "18", ""):
            with self.subTest(schema=schema), \
                    fixture_tree(prefix="agrep-unreleased-schema-") as raw:
                path = Path(raw) / "corpus.db"
                self._forge_released(path, self.OLD_OWNER, OLD_TEXT, "old", schema=schema)
                proof = self._proof(path)
                with ExitStack() as stack:
                    for patch in self._ownership_patches(path, proof):
                        stack.enter_context(patch)
                    ownership = corpusdb._derived_write_ownership(for_write=True)
                    self.assertTrue(ownership.replace_retained_db)
                    self.assertIsNone(corpusdb._interactive_snapshot(
                        "old", repair_required=False, ownership=ownership))
                with ExitStack() as stack:
                    for patch in self._foreign_owner_patches(path):
                        stack.enter_context(patch)
                    self.assertIsNone(corpusdb._foreign_stale_db())

    def test_readers_serve_exactly_the_schemas_the_rust_takeover_keeps(self) -> None:
        # the takeover retains a previous release's database only for these readers: a schema
        # bumped on one side alone either discards what readers could serve or retains what
        # they refuse, and the upgrade crawls the transcripts until the rebuild lands
        source = (ROOT / "crates" / "agrep-cli" / "src" / "main.rs").read_text(encoding="utf-8")
        constants = dict(re.findall(
            r'^const (CORPUS_DB_(?:RETAINED_)?SCHEMA_VERSION): &str = "([0-9]+)";',
            source, re.MULTILINE))
        self.assertEqual(constants, {
            "CORPUS_DB_SCHEMA_VERSION": corpusdb._SCHEMA,
            "CORPUS_DB_RETAINED_SCHEMA_VERSION": corpusdb._RETAINED_READER_SCHEMA,
        })

    def test_final_retained_validation_hashes_once(self) -> None:
        with fixture_tree(prefix="agrep-retained-final-hash-") as raw:
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
        with fixture_tree(prefix="agrep-retained-hash-mismatch-") as raw:
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
        with fixture_tree(prefix="agrep-retained-retry-swap-") as raw:
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
        with fixture_tree(prefix="agrep-retained-corpus-") as raw:
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
        with fixture_tree(prefix="agrep-schema13-fallback-") as raw:
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
        with fixture_tree(prefix="agrep-retained-recheck-") as raw:
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
        with fixture_tree(prefix="agrep-retained-crash-") as raw:
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
            "AGREP_TIMING": "1",
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
                            state: dict[str, object], barrier: index_lock.IndexLock) -> None:
        while not found.is_set():
            for path in data.glob(".indexd.v*.child.*"):
                try:
                    raw = path.read_text(encoding="ascii")
                except OSError:
                    continue
                state["record"] = raw
                match = re.search(
                    r"(?:^| )target=(\d+)(?: |\n).*?"
                    r"target_start=([^ \n]+)(?: |\n)", raw)
                if match is None:
                    continue
                pid = int(match.group(1))
                birth = match.group(2)
                observed_start = common.process_start_identity(pid)
                state.update(candidate=pid, observed_start=observed_start)
                if observed_start != birth:
                    continue
                while not found.is_set() and common.process_start_identity(pid) == birth:
                    # macOS drops a stop that lands inside the target's execve into the successor.
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except OSError:
                        break
                    try:
                        status = subprocess.run(
                            ["/bin/ps", "-p", str(pid), "-o", "stat="],
                            capture_output=True, text=True, timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        break
                    state["status"] = (status.returncode, status.stdout, status.stderr)
                    if (status.returncode == 0 and status.stdout.strip().startswith("T")
                            and common.process_start_identity(pid) == birth):
                        state.update(pid=pid, start=birth, fence=path, stopped=True)
                        barrier.__exit__(None, None, None)
                        found.set()
                        return
                    time.sleep(0.005)
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

    def _explicit_index_during_adoption(
            self, *, expires: bool, current_database: bool = False,
            full: bool = False) -> None:
        with fixture_tree(prefix="agrep-explicit-adoption-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 1)
            env = self._env(home, data, RELEASE_BIN)
            env.pop("AGREP_DEBUG")
            env.update(AGREP_NO_FETCH="1", AGREP_NO_SEM_WORKER="1", AGREP_NO_RESIDENT="1")
            initial = subprocess.run(
                [sys.executable, str(CLI), "index"], cwd=ROOT,
                env={**env, "AGREP_NO_DAEMON": "1"},
                capture_output=True, text=True, timeout=300)
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            if not current_database:
                (data / "corpus.db").unlink()
            writer = self._owner_build(data)
            published = (data / "messages.jsonl").read_bytes()
            self._sources(home, NEW_TEXT, 2, 1)
            daemon = foreground = None
            daemon_log = root / "daemon.log"
            try:
                with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                        index_lock.IndexLock("adoption-barrier"), \
                        daemon_log.open("w") as log:
                    daemon = subprocess.Popen(
                        [sys.executable, str(PY_DIR / "indexd.py")],
                        cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
                    claim_path = data / ".indexd.lock"
                    self._wait_for(
                        claim_path.is_file, 10, "daemon did not acquire its adoption claim")
                    claim = dict(field.split("=", 1) for field in claim_path.read_text().split())
                    self.assertEqual(claim["state"], "derived-adoption")
                    self.assertEqual(claim["writer"], writer)
                    self.assertIs(
                        ownerfile.classify_process(
                            int(claim["pid"]), claim["start"], pid_alive=common.pid_alive,
                            process_start=common.process_start_identity),
                        ownerfile.ProcessOwner.EXACT_LIVE)
                    daemon_record = (data / ".indexd.v2.lock").read_text()
                    self.assertIn(f"pid={daemon.pid} ", daemon_record)
                    self.assertIn(f"writer={writer} ", daemon_record)
                    self.assertEqual((data / "messages.jsonl").read_bytes(), published)
                    command = [sys.executable, str(CLI), "index"] + (["--full"] if full else [])
                    if expires:
                        command = [
                            sys.executable, "-c",
                            "import indexd_runtime,runpy,sys; "
                            "indexd_runtime._UPGRADE_SETTLEMENT_WAIT_S=0.15; "
                            f"sys.argv=[{str(CLI)!r},'index']; "
                            f"runpy.run_path({str(CLI)!r},run_name='__main__')"]
                    foreground = subprocess.Popen(
                        command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
                    if expires:
                        stdout, stderr = foreground.communicate(timeout=15)
                        self.assertEqual(foreground.returncode, 1, stdout + stderr)
                        self.assertIn(
                            f"freshness daemon is still adopting the index (pid {claim['pid']})",
                            stderr)
                        self.assertEqual(stderr.count(
                            "waiting for the freshness daemon to finish updating the index"), 1, stderr)
                        self.assertIn("finishes on its own", stderr)
                        self.assertNotIn("legacy or ambiguous", stderr)
                        self.assertNotIn("nothing is building", stderr)
                        self.assertTrue(common.pid_alive(int(claim["pid"])))
                        self.assertEqual((data / "messages.jsonl").read_bytes(), published)
                        self.assertEqual(
                            dict(field.split("=", 1) for field in claim_path.read_text().split()),
                            claim)
                        return
                    try:
                        stdout, stderr = foreground.communicate(timeout=8)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        self.fail(
                            f"explicit index did not wait: exit={foreground.returncode}\n"
                            f"stdout={stdout}\nstderr={stderr}")
                stdout, stderr = foreground.communicate(timeout=30)
                self.assertEqual(foreground.returncode, 0, stdout + stderr)
                self.assertEqual(stderr.count(
                    "waiting for the freshness daemon to finish updating the index"), 1, stderr)
                self.assertNotIn("legacy or ambiguous", stderr)
                self.assertNotIn("nothing is building", stderr)
                self.assertIn(NEW_TEXT, (data / "messages.jsonl").read_text())
            finally:
                if foreground is not None:
                    if foreground.poll() is None:
                        foreground.kill()
                    foreground.communicate(timeout=5)
                if daemon is not None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(daemon.pid, signal.SIGKILL)
                        daemon.wait(timeout=5)

    def test_explicit_index_waits_for_live_same_build_adoption(self) -> None:
        self._explicit_index_during_adoption(expires=False)

    def test_explicit_index_adoption_wait_budget_expires_honestly(self) -> None:
        self._explicit_index_during_adoption(expires=True)

    def test_explicit_index_waits_even_when_the_published_database_is_current(self) -> None:
        self._explicit_index_during_adoption(expires=False, current_database=True)

    def test_full_explicit_index_waits_for_live_same_build_adoption(self) -> None:
        self._explicit_index_during_adoption(
            expires=False, current_database=True, full=True)

    def test_explicit_index_names_a_stranger_indexer_without_daemon_promises(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-stranger-adoption-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 1)
            env = self._env(home, data, RELEASE_BIN)
            env.pop("AGREP_DEBUG")
            env.update(AGREP_NO_DAEMON="1", AGREP_NO_FETCH="1",
                       AGREP_NO_SEM_WORKER="1", AGREP_NO_RESIDENT="1")
            initial = subprocess.run(
                [sys.executable, str(CLI), "index"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            self._sources(home, NEW_TEXT, 2, 1)
            holder = None
            try:
                with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                        index_lock.IndexLock("adoption-barrier"):
                    # A plain explicit index of this build claims the stores, then blocks
                    # on the held index lock: a live same-build claim with no daemon behind it.
                    holder = subprocess.Popen(
                        [sys.executable, str(CLI), "index"], cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    claim_path = data / ".indexd.lock"
                    self._wait_for(
                        claim_path.is_file, 10, "the holder never claimed the stores")
                    claim = dict(field.split("=", 1) for field in claim_path.read_text().split())
                    self.assertEqual(claim["state"], "derived-adoption")
                    self.assertFalse((data / f".indexd.v2.child.{claim['token']}").exists())
                    later = subprocess.run(
                        [sys.executable, "-c",
                         "import indexd_runtime,runpy,sys; "
                         "indexd_runtime._UPGRADE_SETTLEMENT_WAIT_S=0.15; "
                         f"sys.argv=[{str(CLI)!r},'index']; "
                         f"runpy.run_path({str(CLI)!r},run_name='__main__')"],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(later.returncode, 1, later.stdout + later.stderr)
                    self.assertIn(
                        f"waiting for another agrep indexer (pid {claim['pid']}) "
                        "to finish updating the index", later.stderr)
                    self.assertIn(
                        f"another agrep indexer (pid {claim['pid']}) is still updating "
                        "the index; the wait budget expired", later.stderr)
                    self.assertNotIn("daemon", later.stderr)
                    self.assertNotIn("finishes on its own", later.stderr)
                    self.assertNotIn("legacy or ambiguous", later.stderr)
                    self.assertTrue(common.pid_alive(int(claim["pid"])))
                stdout, stderr = holder.communicate(timeout=30)
                self.assertEqual(holder.returncode, 0, stdout + stderr)
                self.assertIn(NEW_TEXT, (data / "messages.jsonl").read_text())
            finally:
                if holder is not None and holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=5)

    def test_only_explicit_launches_carry_the_adoption_wait_budget(self) -> None:
        inherited = {"AGREP_DERIVED_ADOPTION_WAIT_MS": "5", "AGREP_RUNTIME_BUILD_ID": "a" * 20}
        with mock.patch.object(indexd_runtime, "rust_writer_env",
                               side_effect=lambda binary=None: dict(inherited)):
            explicit = indexd_runtime.explicit_rust_writer_env(Path("agrep-rs"))
        self.assertEqual(explicit["AGREP_DERIVED_ADOPTION_WAIT_MS"], "180000")
        self.assertEqual(explicit["AGREP_RUNTIME_BUILD_ID"], "a" * 20)
        binary = Path(tempfile.mkdtemp(prefix="agrep-wait-env-")) / "agrep-rs"
        binary.write_bytes(b"fixture")
        self.addCleanup(shutil.rmtree, binary.parent)
        with mock.patch.dict(os.environ, inherited), \
                mock.patch.object(indexd_runtime, "assert_python_runtime_unchanged"), \
                mock.patch.object(indexd_runtime, "derived_writer_build_id",
                                  return_value="b" * 20), \
                mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                                  return_value=SimpleNamespace(writable=True, state="current")), \
                mock.patch.object(indexd_runtime, "derived_writer_launchable", return_value=True), \
                mock.patch.object(indexd_runtime, "_inspect_indexd_owner",
                                  return_value=SimpleNamespace(
                                      state=indexd_runtime._IndexdOwnerState.ABSENT,
                                      snapshot=None)):
            background = indexd_runtime.rust_writer_env(binary)
        self.assertNotIn("AGREP_DERIVED_ADOPTION_WAIT_MS", background)

    def test_one_passing_ownership_probe_is_the_explicit_verdict(self) -> None:
        # the daemon's follow-up corpus.db work starts in response to the publication
        # this ingest just made; a probe that passed must not be second-guessed by a
        # later journal-blocked one
        verdicts = iter([SimpleNamespace(writable=True, journal_blocked=False),
                         SimpleNamespace(writable=False, journal_blocked=True)])
        rerun = mock.Mock(return_value=0)
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               side_effect=lambda *a, **k: next(verdicts)), \
                mock.patch.object(indexd_runtime, "_await_upgrade_settlement",
                                  side_effect=AssertionError("settlement waited")), \
                mock.patch.object(indexd_runtime.common, "log",
                                  side_effect=AssertionError("declined a published index")):
            self.assertTrue(indexd_runtime.explicit_ingest_published(rerun))
        rerun.assert_not_called()

    def test_declined_full_index_reruns_cold_even_after_the_sig_moved(self) -> None:
        import cli
        with tempfile.TemporaryDirectory(prefix="agrep-full-rerun-") as raw:
            data = Path(raw)
            sig = data / ".ingest.sig"
            sig.write_text("1:seed\n")
            commands: list[list[str]] = []

            def run(cmd, **_kwargs):
                commands.append(list(cmd))
                return SimpleNamespace(returncode=0)

            def published(rerun):
                # the settlement window: the daemon's own pass rewrites the sig
                sig.write_text("2:daemon\n")
                os.utime(sig, ns=(1, 1))
                return rerun() == 0

            with mock.patch.object(cli.common, "DATA_DIR", data), \
                    mock.patch.object(cli.common, "ingest_bin",
                                      return_value=data / "agrep-rs"), \
                    mock.patch.object(cli, "_ensure_binary", return_value=True), \
                    mock.patch.object(cli.subprocess, "run", side_effect=run), \
                    mock.patch.object(indexd_runtime, "retire_displaceable_indexd_owner"), \
                    mock.patch.object(indexd_runtime, "explicit_rust_writer_env",
                                      return_value={}), \
                    mock.patch.object(indexd_runtime, "explicit_ingest_published",
                                      side_effect=published), \
                    mock.patch.object(indexd_runtime, "record_binary_identity"), \
                    mock.patch.object(indexd_runtime, "hand_off_search_index",
                                      return_value=True), \
                    mock.patch.object(cli.common, "lap"), \
                    mock.patch.object(cli.sys, "stdout", io.StringIO()):
                args = SimpleNamespace(rest=[], full=True)
                self.assertEqual(cli.cmd_index(args), 0)
        self.assertEqual([command[-1] for command in commands], ["--full", "--full"])

    def test_a_daemon_that_took_the_slot_after_the_reapers_judgement_survives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-reap-gap-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 1)
            env = self._env(home, data, RELEASE_BIN)
            env.pop("AGREP_DEBUG")
            env.update(AGREP_NO_FETCH="1", AGREP_NO_SEM_WORKER="1", AGREP_NO_RESIDENT="1")
            initial = subprocess.run(
                [sys.executable, str(CLI), "index"], cwd=ROOT,
                env={**env, "AGREP_NO_DAEMON": "1"},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            writer = self._owner_build(data)
            owner_path = data / ".indexd.v2.lock"
            # the dead claimer's record this reaper sets out to reclaim
            owner_path.write_bytes(
                f"state=derived-adoption pid={common._MAX_PROCESS_ID} start=fixture "
                f"writer={writer} token={'a' * 32}\n".encode("ascii"))
            os.utime(owner_path, (time.time() - 30, time.time() - 30))
            stale = ownerfile.snapshot(owner_path)
            daemon_log = root / "daemon.log"
            state: dict[str, object] = {}
            real_mutex = ownerfile._slot_mutex

            def record(path: Path) -> str:
                try:
                    return path.read_text(encoding="ascii")
                except OSError:
                    return ""

            def daemon_settled() -> bool:
                return (f"pid={state['daemon'].pid} " in record(owner_path)
                        and "keeping the index fresh" in daemon_log.read_text(encoding="utf-8")
                        and not (data / ".indexd.lock").exists())

            def daemon_takes_the_slot_first(path, **kwargs):
                if path == owner_path and "daemon" not in state:
                    # the live daemon reclaims the stale record and takes the slot between
                    # this reaper's judgement and its locked re-check
                    state["daemon"] = subprocess.Popen(
                        [sys.executable, str(PY_DIR / "indexd.py")], cwd=ROOT, env=env,
                        stdout=state["log"], stderr=state["log"], start_new_session=True)
                    self._wait_for(daemon_settled, 20, "the daemon never settled into its record")
                    state["live"] = ownerfile.snapshot(owner_path)
                return real_mutex(path, **kwargs)

            try:
                with daemon_log.open("w") as log:
                    state["log"] = log
                    with mock.patch.object(
                            ownerfile, "_slot_mutex", side_effect=daemon_takes_the_slot_first):
                        self.assertFalse(ownerfile.remove_exact(owner_path, stale))
                    live = state["live"]
                    daemon = state["daemon"]
                    observed = ownerfile.snapshot(owner_path)
                    self.assertEqual((observed.identity[:3], observed.raw),
                                     (live.identity[:3], live.raw))
                    self.assertEqual(list(data.glob(".*.owner-reap-*")), [])
                    # a claimer meeting the live daemon is refused outright; nothing moves
                    claimer = subprocess.run(
                        [str(RELEASE_BIN), "index", "--agent", "all"], cwd=ROOT,
                        env={**env, "AGREP_RUNTIME_BUILD_ID": writer},
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(claimer.returncode, 0, claimer.stderr)
                    self.assertIn("serving the published snapshot read-only", claimer.stderr)
                    self.assertFalse((data / ".indexd.lock").exists())
                    # the daemon's next heartbeat verifies the record it still holds
                    time.sleep(indexd.BEAT_S + 1.5)
                    self.assertIsNone(daemon.poll(), daemon_log.read_text(encoding="utf-8"))
                    self.assertNotIn("ownership", daemon_log.read_text(encoding="utf-8"))
                    observed = ownerfile.snapshot(owner_path)
                    self.assertEqual((observed.identity[:3], observed.raw),
                                     (live.identity[:3], live.raw))
            finally:
                daemon = state.get("daemon")
                if daemon is not None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(daemon.pid, signal.SIGKILL)
                        daemon.wait(timeout=5)

    def test_successor_publishes_rebuilt_cache_before_daemon_start(self) -> None:
        with fixture_tree(prefix="agrep-rebuilt-adoption-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 1)
            initial_env = self._env(home, data, RELEASE_BIN)
            initial_env["AGREP_NO_DAEMON"] = "1"
            initial_env["AGREP_NO_FETCH"] = "1"
            initial = subprocess.run(
                [sys.executable, os.fspath(CLI), "index"],
                cwd=ROOT, env=initial_env, capture_output=True, text=True, timeout=300)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            cache_path = data / ".ingest_cache.bin"
            cache = bytearray(cache_path.read_bytes())
            cache[-1] ^= 0xFF
            cache_path.write_bytes(cache)
            (data / ".events_complete.cline.json").unlink()
            self._sources(home, NEW_TEXT, 2, 1)
            successor = self._successor_binary(root)
            env = self._env(home, data, successor)
            env["AGREP_NO_DAEMON"] = "1"
            env["AGREP_NO_FETCH"] = "1"
            try:
                rebuilt = subprocess.run(
                    [sys.executable, os.fspath(CLI), "index"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
                self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
                with closing(sqlite3.connect(data / "corpus.db")) as db:
                    self.assertEqual(
                        db.execute("SELECT text FROM msgs WHERE who='user'").fetchone()[0],
                        NEW_TEXT)
                env.pop("AGREP_NO_DAEMON")

                def recovered() -> bool:
                    latest, _elapsed = self._run_search(env, NEW_TEXT)
                    if latest.returncode != 0 or not any(
                            NEW_TEXT in row.get("snippet", "")
                            for row in self._json_rows(latest)):
                        return False
                    status = subprocess.run(
                        [sys.executable, os.fspath(CLI), "status", "--json"],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(status.returncode, 0, status.stderr)
                    packet = json.loads(status.stdout)
                    return (packet["daemon"]["running"]
                            and packet["search_index_ready"])

                self._wait_for(
                    recovered, 20,
                    "successor daemon could not serve the rebuilt publication")
            finally:
                stopped = subprocess.run(
                    [sys.executable, os.fspath(CLI), "remove"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

    def test_search_reads_old_publication_while_successor_adoption_is_active(self) -> None:
        with fixture_tree(prefix="agrep-active-adoption-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 8)
            initial_env = self._env(home, data, RELEASE_BIN)
            initial_env["AGREP_RUNTIME_BUILD_ID"] = OLD_BUILD
            initial = subprocess.run(
                [str(RELEASE_BIN), "index", "--agent", "cline"],
                cwd=ROOT, env=initial_env, capture_output=True, text=True, timeout=300)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            published = (data / "messages.jsonl").read_bytes()
            self._sources(home, NEW_TEXT, 2, 8)
            successor = self._successor_binary(root)
            env = self._env(home, data, successor)
            process = None
            try:
                with mock.patch.object(
                        index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                        index_lock.IndexLock("adoption-barrier"):
                    process = subprocess.Popen(
                        [str(successor), "index", "--agent", "cline"],
                        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)

                    def adopting():
                        for path in data.glob(".indexd*.lock"):
                            try:
                                text = path.read_text(encoding="ascii")
                            except OSError:
                                continue
                            if (f"pid={process.pid} " in text
                                    and "state=derived-adoption " in text):
                                return True
                        return False

                    self._wait_for(adopting, 5.0, "successor did not enter adoption")
                    result, _elapsed = self._run_search(env, OLD_TEXT)
                    self.assertEqual((data / "messages.jsonl").read_bytes(), published)
                    self.assertEqual(self._owner_build(data), OLD_BUILD)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    self.assertTrue(any(
                        OLD_TEXT in str(row.get("snippet") or "")
                        for row in self._json_rows(result)))
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertIn(NEW_TEXT, (data / "messages.jsonl").read_text(encoding="utf-8"))
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
                subprocess.run(
                    [sys.executable, "-c",
                     "import indexd_runtime; "
                     "indexd_runtime.stop_indexd_owner(wait_s=5.0)"],
                    cwd=PY_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                    check=False)

    def test_released_schema_index_serves_every_search_across_the_takeover(self) -> None:
        # The 0.4.2 upgrade: the previous build left a schema-15 corpus.db. Before the
        # successor's ingest takes over, while its rebuild is owed, and after it lands, every
        # search answers from an index - never the transcript scan, which is a multi-second
        # crawl on a real corpus, nor its publication timeout
        self.assertTrue(RELEASE_BIN.is_file(), RELEASE_BIN)
        with fixture_tree(prefix="agrep-released-schema-upgrade-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            data.mkdir()
            self._sources(home, OLD_TEXT, 1, 2)
            previous = self._env(home, data, RELEASE_BIN)
            previous.update(AGREP_NO_DAEMON="1", AGREP_NO_FETCH="1")
            initial = subprocess.run(
                [sys.executable, os.fspath(CLI), "index"], cwd=ROOT, env=previous,
                capture_output=True, text=True, timeout=300)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            with closing(sqlite3.connect(data / "corpus.db")) as db:
                db.execute("DROP TABLE reply_shape")
                db.execute("UPDATE meta SET value='15' WHERE key='schema'")
                db.commit()
            binary = self._successor_binary(root)
            env = self._env(home, data, binary)
            env.update(AGREP_NO_DAEMON="1", AGREP_NO_FETCH="1")

            def search(text: str) -> tuple[dict, list[dict]]:
                result, _elapsed = self._run_search(env, text)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout.splitlines()[0]), self._json_rows(result)

            def assert_indexed_hit(text: str) -> None:
                meta, rows = search(text)
                self.assertEqual(meta.get("engine"), "corpusdb", meta)
                self.assertTrue(any(
                    text in str(row.get("snippet") or "") for row in rows), rows)

            # the previous build still owns every store
            assert_indexed_hit(OLD_TEXT)

            task = home / ".cline" / "data" / "tasks" / "2"
            task.mkdir(parents=True)
            (task / "api_conversation_history.json").write_text(json.dumps([
                {"role": "user", "content": NEW_TEXT, "ts": 2},
            ]), encoding="utf-8")
            writer = subprocess.run(
                [sys.executable, "-c",
                 "import json, indexd_runtime; env = indexd_runtime.rust_writer_env(); "
                 "print(json.dumps({key: env[key] for key in ("
                 "'AGREP_RUNTIME_BUILD_ID', 'AGREP_PYTHON_RUNTIME_BUILD_ID')}))"],
                cwd=PY_DIR, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(writer.returncode, 0, writer.stderr)
            takeover = subprocess.run(
                [os.fspath(binary), "index", "--agent", "all"], cwd=ROOT,
                env={**env, **json.loads(writer.stdout)},
                capture_output=True, text=True, timeout=300)
            self.assertEqual(takeover.returncode, 0, takeover.stderr)
            self.assertIn("search db retained read-only for atomic rebuild", takeover.stderr)
            owner = json.loads((data / ".derived-owner.json").read_text(encoding="utf-8"))
            self.assertIn("retained_corpus_db", owner)
            self.assertIn(NEW_TEXT, (data / "messages.jsonl").read_text(encoding="utf-8"))

            # the successor owns the stores, its rebuild is owed: the retained index serves,
            # behind the transcripts and saying so
            assert_indexed_hit(OLD_TEXT)

            rebuilt = subprocess.run(
                [sys.executable, os.fspath(CLI), "index"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=300)
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            with closing(sqlite3.connect(data / "corpus.db")) as db:
                self.assertEqual(db.execute(
                    "SELECT value FROM meta WHERE key='schema'").fetchone()[0], corpusdb._SCHEMA)
            assert_indexed_hit(NEW_TEXT)

    def test_slow_successor_adoption_outlives_foreground_search(self) -> None:
        self.assertTrue(RELEASE_BIN.is_file(), RELEASE_BIN)
        with fixture_tree(
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
                encoding="utf-8", errors="replace", timeout=300)
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
            barrier = index_lock.IndexLock("takeover-pause")
            monitor = threading.Thread(
                target=self._pause_owned_ingest,
                args=(data, paused, pause_state, barrier), daemon=True)
            child_pid = None
            target_pid = None
            continued = False
            guards: list[Path] = []
            child_path = data / "missing-spawn-child"
            try:
                with mock.patch.object(
                        index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                        barrier:
                    monitor.start()
                    kick = subprocess.run(
                        [sys.executable, "-c",
                         "import indexd_runtime; print(indexd_runtime.kick_background_repair())"],
                        cwd=PY_DIR, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(kick.returncode, 0, kick.stderr)
                    try:
                        self._wait_for(
                            paused.is_set, 5.0,
                            "did not pause the exact successor ingest target")
                    except AssertionError as exc:
                        log = data / "indexd.log"
                        detail = log.read_text(encoding="utf-8") if log.exists() else ""
                        self.fail(
                            f"{exc}; kick={kick.stdout!r}; "
                            f"pause={pause_state!r}; log={detail!r}")
                    target_pid = int(pause_state["pid"])
                    target_start = str(pause_state["start"])
                    self.assertIs(
                        ownerfile.classify_process(
                            target_pid, target_start, pid_alive=common.pid_alive,
                            process_start=common.process_start_identity),
                        ownerfile.ProcessOwner.EXACT_LIVE)
                    self.assertEqual(self._owner_build(data), OLD_BUILD)
                    self.assertIn(
                        OLD_TEXT,
                        (data / "messages.jsonl").read_text(encoding="utf-8"))

                foreground, _elapsed = self._run_search(env, OLD_TEXT)
                self.assertEqual(
                    foreground.returncode, 0,
                    foreground.stderr + "\n" + foreground.stdout + "\n" + json.dumps({
                        "paused": paused.is_set(),
                        "owner": self._owner_build(data),
                        "messages_bytes": (data / "messages.jsonl").stat().st_size,
                        "old_rows_present": OLD_TEXT in (
                            data / "messages.jsonl").read_text(encoding="utf-8"),
                        "new_rows_present": NEW_TEXT in (
                            data / "messages.jsonl").read_text(encoding="utf-8"),
                    }))
                self.assertLess(
                    phase_seconds(foreground.stderr)["freshen"],
                    COORDINATION_BUDGET_S, foreground.stderr)
                self.assertTrue(any(
                    OLD_TEXT in str(row.get("snippet") or "")
                    for row in self._json_rows(foreground)))

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
                # Doctor's routine budget can defer the daemon observation on a
                # loaded box; only a reached verdict is asserted.
                for _attempt in range(5):
                    doctor = subprocess.run(
                        [sys.executable, os.fspath(CLI), "doctor", "--json"],
                        cwd=ROOT, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=20)
                    self.assertEqual(doctor.returncode, 0, doctor.stderr)
                    doctor_payload = json.loads(doctor.stdout)
                    deferred = doctor_payload["diagnostics"]["deferred"]
                    if "daemon resource observation" not in deferred:
                        break
                self.assertNotIn(
                    "daemon resource observation", deferred, doctor_payload["diagnostics"])
                self.assertTrue(
                    doctor_payload["resources"]["indexd"]["running"],
                    doctor_payload["resources"]["indexd"])
                doctor_phases = phase_seconds(doctor.stderr)
                self.assertLess(
                    doctor_phases["total"] - doctor_phases["imports"],
                    ADOPTION_WAIT_S, doctor.stderr)
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
                second, _elapsed = self._run_search(debug_env, NEW_TEXT)
                self.assertEqual(second.returncode, 0, second.stderr)
                rows = self._json_rows(second)
                self.assertTrue(any(
                    NEW_TEXT in str(row.get("snippet") or "") for row in rows))
                self.assertIn("engine: corpusdb", second.stderr)
                self.assertLess(
                    phase_seconds(second.stderr)["freshen"],
                    COORDINATION_BUDGET_S, second.stderr)

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
