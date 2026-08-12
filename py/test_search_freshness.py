from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402
import session_context  # noqa: E402


def _family_meta(signature: str) -> bytes:
    return json.dumps({
        "version": common.SESSION_FAMILY_INDEX_VERSION,
        "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
        "ingest_signature": signature,
        "count": 0,
        "digest": "0" * 48,
    }, separators=(",", ":")).encode()


def _proof_row(path: Path) -> dict:
    identity = corpusdb._proof_file_identity(path)
    if os.name == "posix":
        token = {"Metadata": corpusdb._unix_change_token(identity[2])}
    elif os.name == "nt":
        try:
            _, value = corpusdb._windows_file_state(path, include_usn=True)
            token = {"Metadata": value}
        except OSError:
            token = {"ContentSha256": list(corpusdb._content_sha256(path))}
    else:
        token = {"Metadata": 0}
    return {
        "name": path.name,
        "len": identity[0],
        "modified_ns": identity[1],
        "change_token": token,
        "edge_hash": corpusdb._edge_hash(path, identity[0]),
    }


def _publish(root: Path, *, age_s: float = 3.0) -> float:
    signature = "0:fixture-generation"
    bodies = {
        "messages.jsonl": b"",
        "replies.jsonl": b"",
        "sessions.jsonl": b"",
        common.SESSION_FAMILY_META_FILE: _family_meta(signature),
        "boundary_stats.json": b"{}",
        ".boundary_stats.bin": b"fixture",
        "event_stats.json": b"{}",
    }
    root.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        (root / name).write_bytes(body)
    signal = root / ".ingest.sig"
    signal.write_text(signature, encoding="utf-8")
    signal_time = time.time() - age_s
    os.utime(signal, (signal_time, signal_time))
    proof = {
        "version": corpusdb._DERIVED_PROOF_VERSION,
        "signature": signature,
        "files": [_proof_row(root / name)
                  for name in corpusdb._DERIVED_PROOF_NAMES],
    }
    (root / ".derived_generation.json").write_text(
        json.dumps(proof, separators=(",", ":")), encoding="utf-8")
    return signal_time


def _empty_result() -> dict:
    return {
        "hits": [], "total": 0, "chats": 0, "tool_hits": 0,
        "engine": "corpusdb", "mode": "keyword", "totals_exact": True,
        "truncated": False, "fallback_recommended": False,
        "semantic_status": None, "semantic_coverage": None,
    }


class SearchGenerationFreshnessContracts(unittest.TestCase):
    def _search_json(self, root: Path) -> tuple[int, dict]:
        stdout = io.StringIO()
        indexd_runtime._clear_freshen_failure()
        try:
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(indexd_runtime, "indexing_failure", return_value=None), \
                    mock.patch.object(indexd_runtime, "_store_census", return_value=[]), \
                    mock.patch.object(common, "in_agent_context", return_value=True), \
                    mock.patch.object(common, "calling_self_exclusion", return_value=None), \
                    mock.patch.object(search, "_semantic_runtime_installed", return_value=False), \
                    mock.patch.object(search, "run_query", return_value=_empty_result()), \
                    contextlib.redirect_stdout(stdout):
                rc = search.main(["needle", "--json", "--lexical"])
        finally:
            indexd_runtime._clear_freshen_failure()
        return rc, json.loads(stdout.getvalue())

    def test_torn_generation_never_rearms_green_on_following_search(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            (root / common.SESSION_FAMILY_META_FILE).unlink()

            first_rc, first = self._search_json(root)
            second_rc, second = self._search_json(root)

        self.assertEqual((first_rc, second_rc), (1, 1))
        for payload in (first, second):
            self.assertEqual(payload["freshness"]["state"], "degraded")
            self.assertEqual(payload["freshness"]["code"], "torn-generation")
            self.assertTrue(payload["freshness"]["may_be_stale"])
            self.assertIsNone(payload["corpus_age_s"])

    def test_committed_generation_reports_trusted_corpus_age(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root, age_s=3.0)
            rc, payload = self._search_json(root)

        self.assertEqual(rc, 1)
        self.assertEqual(payload["freshness"]["state"], "no-known-failure")
        self.assertTrue(2.0 <= payload["corpus_age_s"] <= 5.0)

    def test_aged_consistent_generation_degrades_on_age_not_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            signal_time = _publish(root, age_s=130.0)
            with mock.patch.object(common, "DATA_DIR", root):
                health = corpusdb.search_generation_health(now=signal_time + 130.0)
                fields = corpusdb.machine_freshness_fields({
                    "state": "degraded", "failing": True,
                    "may_be_stale": True, "code": "stale-ingest-signal",
                    "reason": "the ingest freshness signal is old",
                }, now=signal_time + 130.0)

        self.assertEqual(health["state"], "ready")
        self.assertEqual(health["corpus_age_s"], 130.0)
        self.assertEqual(fields["freshness"]["code"], "stale-ingest-signal")
        self.assertEqual(fields["corpus_age_s"], 130.0)

    def test_search_database_generation_mismatch_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            db = sqlite3.connect(root / "corpus.db")
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO meta VALUES(?, ?)", (
                ("schema", corpusdb._SCHEMA),
                ("stamp", "[]"),
                ("family_stamp", common.SESSION_FAMILY_MISSING_STAMP),
            ))
            db.commit()
            db.close()
            with mock.patch.object(common, "DATA_DIR", root):
                fields = corpusdb.machine_freshness_fields({
                    "state": "no-known-failure", "failing": False,
                    "checked": True,
                })

        self.assertEqual(fields["freshness"]["state"], "degraded")
        self.assertEqual(fields["freshness"]["code"], "search-index-stale")
        self.assertIsNotNone(fields["corpus_age_s"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_generation_commit_special_files_fail_without_blocking(self) -> None:
        for name in (".ingest.sig", ".derived_generation.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _publish(root)
                path = root / name
                path.unlink()
                os.mkfifo(path)
                script = (
                    "import json\n"
                    "from pathlib import Path\n"
                    "import common, corpusdb\n"
                    f"common.DATA_DIR = Path({str(root)!r})\n"
                    "print(json.dumps(corpusdb.search_generation_health()))\n"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", script], cwd=Path(__file__).parent,
                    capture_output=True, text=True, timeout=2)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stdout)["state"], "torn-generation")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_derived_artifact_fifo_fails_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            path = root / "messages.jsonl"
            path.unlink()
            os.mkfifo(path)
            script = (
                "import json\n"
                "from pathlib import Path\n"
                "import common, corpusdb\n"
                f"common.DATA_DIR = Path({str(root)!r})\n"
                "print(json.dumps(corpusdb.search_generation_health()))\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=Path(__file__).parent,
                capture_output=True, text=True, timeout=2)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["state"], "torn-generation")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_derived_artifact_swap_to_fifo_fails_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"fixture")
            row = _proof_row(path)
            real_edge_hash = corpusdb._edge_hash

            def swap_then_hash(target, size, identity):
                target.unlink()
                os.mkfifo(target)
                return real_edge_hash(target, size, identity)

            with mock.patch.object(
                    corpusdb, "_edge_hash", side_effect=swap_then_hash):
                started = time.perf_counter()
                problem = corpusdb._validate_derived_file(root, row)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertIn("cannot be verified", problem)

    def test_deep_generation_proof_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            (root / ".derived_generation.json").write_text(
                ("[" * 2000) + "0" + ("]" * 2000), encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root):
                health = corpusdb.search_generation_health()
        self.assertEqual(health["state"], "torn-generation")

    def test_deep_family_meta_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            family = root / common.SESSION_FAMILY_META_FILE
            family.write_text(
                ("[" * 10_000) + "0" + ("]" * 10_000), encoding="utf-8")
            proof_path = root / ".derived_generation.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["files"] = [
                _proof_row(family) if row["name"] == family.name else row
                for row in proof["files"]
            ]
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root):
                health = corpusdb.search_generation_health()
        self.assertEqual(health["state"], "torn-generation")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_post_proof_small_artifact_swap_never_blocks(self) -> None:
        for name in (".ingest.sig", common.SESSION_FAMILY_META_FILE):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _publish(root)
                real_publication = corpusdb._derived_publication_health

                def swap_after_proof(now=None, **kwargs):
                    result = real_publication(now, **kwargs)
                    path = root / name
                    path.unlink()
                    os.mkfifo(path)
                    return result

                with mock.patch.object(common, "DATA_DIR", root), \
                        mock.patch.object(
                            corpusdb, "_derived_publication_health",
                            side_effect=swap_after_proof):
                    started = time.perf_counter()
                    health = corpusdb.search_generation_health()
                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(health["state"], "torn-generation")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_corpus_db_swap_before_connect_never_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            path = root / "corpus.db"
            path.write_bytes(b"fixture")
            real_connect = corpusdb._connect_read_alias

            def swap_before_connect(target, timeout):
                target.unlink()
                os.mkfifo(target)
                return real_connect(target, timeout)

            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        corpusdb, "_connect_read_alias",
                        side_effect=swap_before_connect):
                started = time.perf_counter()
                health = corpusdb.search_generation_health()
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(health["state"], "generation-unavailable")

    def test_corpus_db_swap_after_connect_cannot_certify_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            path = root / "corpus.db"
            with mock.patch.object(common, "DATA_DIR", root):
                stamp = corpusdb._stamp()
                family_stamp = common.session_family_source_stamp(root)
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO meta VALUES(?, ?)", (
                ("schema", corpusdb._SCHEMA),
                ("stamp", stamp),
                ("family_stamp", family_stamp),
            ))
            db.commit()
            db.close()
            bad = root / "bad.db"
            bad_db = sqlite3.connect(bad)
            bad_db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            bad_db.execute("INSERT INTO meta VALUES('schema', 'bad')")
            bad_db.commit()
            bad_db.close()
            real_connect = corpusdb._connect_read_alias

            def replace_after_connect(target, timeout):
                connection = real_connect(target, timeout)
                os.replace(bad, target)
                return connection

            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        corpusdb, "_connect_read_alias",
                        side_effect=replace_after_connect):
                health = corpusdb.search_generation_health()
        self.assertEqual(health["state"], "generation-unavailable")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_transcript_generation_small_fifo_never_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            family = root / common.SESSION_FAMILY_META_FILE
            family.unlink()
            os.mkfifo(family)
            started = time.perf_counter()
            with self.assertRaises(RuntimeError):
                session_context.transcript_generation(root, attempts=1)
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_generation_proof_change_during_validation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            proof = root / ".derived_generation.json"
            real_validate = corpusdb._validate_derived_file
            changed = False

            def mutate_once(*args, **kwargs):
                nonlocal changed
                result = real_validate(*args, **kwargs)
                if not changed:
                    changed = True
                    proof.write_bytes(proof.read_bytes() + b"\n")
                return result

            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        corpusdb, "_validate_derived_file", side_effect=mutate_once):
                health = corpusdb.search_generation_health()

        self.assertEqual(health["state"], "generation-moving")

    def test_validated_artifact_change_before_return_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root)
            real_validate = corpusdb._validate_derived_file
            calls = 0

            def mutate_after_last(*args, **kwargs):
                nonlocal calls
                result = real_validate(*args, **kwargs)
                calls += 1
                if calls == len(corpusdb._DERIVED_PROOF_NAMES):
                    (root / "messages.jsonl").write_text(
                        "now bad\n", encoding="utf-8")
                return result

            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        corpusdb, "_validate_derived_file",
                        side_effect=mutate_after_last):
                health = corpusdb.search_generation_health()
        self.assertEqual(health["state"], "generation-moving")

    def _stale_reader(self, root: Path) -> sqlite3.Connection:
        """A readable published snapshot whose stamp predates the sources."""
        path = root / "corpus.db"
        db = sqlite3.connect(path, factory=corpusdb._PublishedConnection)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('stamp', 'old')")
        db.commit()
        return db

    def _snapshot_while_pending(
            self, root: Path, pending: bool) -> sqlite3.Connection | None:
        stale = self._stale_reader(root)
        with mock.patch.object(common, "DATA_DIR", root), \
                mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"), \
                mock.patch.object(
                    corpusdb, "_query_failure_matches_current",
                    return_value=False), \
                mock.patch.object(
                    corpusdb, "_stale_db", return_value=stale), \
                mock.patch.object(
                    corpusdb.indexd_runtime, "derived_writer_build_id",
                    return_value="a" * 20), \
                mock.patch.object(
                    corpusdb.indexd_runtime, "disclose_foreground_snapshot"), \
                mock.patch.object(
                    corpusdb.indexd_runtime, "search_index_build_pending",
                    return_value=pending):
            return corpusdb._interactive_snapshot(
                "new", repair_required=False,
                ownership=corpusdb._DerivedWriteOwnership("current"))

    def test_queued_build_sends_a_behind_snapshot_to_the_direct_scan(
            self) -> None:
        """B1: `agrep index` returns announcing that searches scan the
        transcripts until the search database lands. Serving the snapshot it
        just superseded answers a fresh query from the retired generation - a
        zero over rows messages.jsonl already holds, claiming full coverage."""
        with tempfile.TemporaryDirectory() as raw:
            reader = self._snapshot_while_pending(Path(raw), pending=True)
        self.assertIsNone(reader)

    def test_a_behind_snapshot_still_serves_when_no_build_is_queued(
            self) -> None:
        """The refusal is scoped to the announced handoff: ordinary drift on a
        live box keeps reading the published index rather than paying a
        corpus-scale scan on every search."""
        with tempfile.TemporaryDirectory() as raw:
            reader = self._snapshot_while_pending(Path(raw), pending=False)
            self.assertIsNotNone(reader)
            self.assertIs(reader._source_stamp_current, False)
            reader.close()


class SqliteAliasPortabilityContracts(unittest.TestCase):
    def _build_fts(self, path: Path) -> None:
        if not corpusdb._trigram_ok():
            self.skipTest("SQLite trigram FTS5 is unavailable")
        db = sqlite3.connect(path)
        try:
            db.executescript(corpusdb._SCHEMA_SQL)
            db.execute(corpusdb._INS, (
                "portable-session", 1, 1, "codex", "p", "", "", "unknown",
                "user", "portable needle from sqlite",
            ))
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            db.commit()
        finally:
            db.close()

    def _assert_fts_hit(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT m.session FROM msgs_fts "
            "JOIN msgs m ON m.id = msgs_fts.rowid "
            "WHERE msgs_fts MATCH 'portable'").fetchone()
        self.assertEqual(row, ("portable-session",))

    @unittest.skipUnless(
        os.name == "posix" and getattr(os, "geteuid", lambda: 0)() != 0,
        "read-only directory permissions need unprivileged POSIX",
    )
    def test_read_only_data_dir_uses_locked_main_without_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "corpus.db"
            self._build_fts(path)
            before = (
                set(root.iterdir()),
                corpusdb._proof_file_identity(path),
                path.read_bytes(),
            )
            root.chmod(0o555)
            db = None
            try:
                db = corpusdb._open(path, 0)
                self.assertNotIsInstance(db, corpusdb._AliasedConnection)
                self._assert_fts_hit(db)
                opened = Path(db.execute("PRAGMA database_list").fetchone()[2])
                self.assertEqual(opened.resolve(), path.resolve())
            finally:
                if db is not None:
                    db.close()
                root.chmod(0o755)
            self.assertEqual(
                (set(root.iterdir()),
                 corpusdb._proof_file_identity(path),
                 path.read_bytes()),
                before)

    def test_private_snapshot_uses_fd_copy_without_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "corpus.db"
            self._build_fts(path)
            with mock.patch.object(
                    corpusdb.os, "link",
                    side_effect=AssertionError("live SQLite was hardlinked")) as link, \
                    mock.patch.object(
                        corpusdb, "_try_clone_sqlite_file",
                        return_value=False) as clone:
                db = corpusdb._connect_read_alias(path, 0)
            try:
                self._assert_fts_hit(db)
                self.assertTrue(db.source_stable())
                opened = Path(db.execute("PRAGMA database_list").fetchone()[2])
                self.assertNotEqual(opened.stat().st_ino, path.stat().st_ino)
            finally:
                db.close()
            link.assert_not_called()
            clone.assert_called()

    def test_simultaneous_private_snapshots_remain_stable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            self._build_fts(path)
            first = corpusdb._connect_read_alias(path, 0)
            second = None
            try:
                self.assertTrue(first.source_stable())
                second = corpusdb._connect_read_alias(path, 0)
                self.assertTrue(first.source_stable())
                self.assertTrue(second.source_stable())
                second.close()
                second = None
                self.assertTrue(first.source_stable())
            finally:
                if second is not None:
                    second.close()
                first.close()

    def test_database_and_wal_mutations_break_alias_stability(self) -> None:
        for journal_mode in ("DELETE", "WAL"):
            with self.subTest(journal_mode=journal_mode), \
                    tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "corpus.db"
                self._build_fts(path)
                writer = sqlite3.connect(path)
                reader = None
                try:
                    actual_mode = writer.execute(
                        f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
                    self.assertEqual(actual_mode.lower(), journal_mode.lower())
                    writer.execute(
                        "UPDATE msgs SET text='portable needle from writer'")
                    writer.commit()
                    reader = corpusdb._connect_read_alias(path, 0)
                    self.assertTrue(reader.source_stable())
                    writer.execute(
                        "UPDATE msgs SET text='portable needle changed now'")
                    writer.commit()
                    with mock.patch.object(
                            corpusdb, "_content_sha256",
                            side_effect=AssertionError(
                                "hot SQLite validation must stay bounded")) as digest:
                        self.assertFalse(reader.source_stable())
                    digest.assert_not_called()
                finally:
                    if reader is not None:
                        reader.close()
                    writer.close()

    def test_live_delete_journal_reads_the_committed_snapshot_directly(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            self._build_fts(path)
            writer = sqlite3.connect(path)
            try:
                writer.execute(
                    "INSERT INTO meta VALUES('schema', ?)",
                    (corpusdb._SCHEMA,))
                writer.commit()
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE msgs SET text='uncommitted replacement'")
                self.assertTrue(Path(f"{path}-journal").exists())
                with mock.patch.object(corpusdb, "DB_PATH", path):
                    reader = corpusdb._stale_db(0)
                self.assertIsNotNone(reader)
                try:
                    self.assertNotIsInstance(
                        reader, corpusdb._AliasedConnection)
                    self.assertEqual(
                        reader.execute(
                            "SELECT text FROM msgs").fetchone()[0],
                        "portable needle from sqlite")
                finally:
                    reader.close()
            finally:
                writer.rollback()
                writer.close()


class WindowsDerivedIdentityContracts(unittest.TestCase):
    def _row(self, path: Path, token: dict) -> dict:
        identity = corpusdb._proof_file_identity(path)
        return {
            "name": path.name,
            "len": identity[0],
            "modified_ns": identity[1],
            "change_token": token,
            "edge_hash": corpusdb._edge_hash(path, identity[0]),
        }

    def test_windows_usn_transform_rejects_zero_and_matches_rust_fixtures(
            self) -> None:
        with self.assertRaisesRegex(OSError, "no journal evidence"):
            corpusdb._rust_windows_usn_token(0)
        self.assertEqual(
            corpusdb._rust_windows_usn_token(0x0102030405060708),
            0xD452CC5DC54AC845,
        )
        self.assertEqual(corpusdb._rust_windows_usn_token(-1), 0xAAACB1A0B9B6B3BA)

    def test_legacy_zero_usn_metadata_proof_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "corpus.db"
            path.write_bytes(b"fixture")
            proof = self._row(
                path, {"Metadata": 0x55534E5F46494C45})
            with mock.patch.object(corpusdb, "DB_PATH", path), \
                    mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state",
                        side_effect=lambda _path, include_usn: (
                            91, corpusdb._rust_windows_usn_token(0))) as state:
                matches = corpusdb._legacy_corpus_proof_matches(proof)

        self.assertFalse(matches)
        state.assert_called_once_with(path, include_usn=True)


    def test_derived_open_uses_one_kernel_identity_across_crt_ctime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "messages.jsonl"
            path.write_bytes(b"fixture")
            crt = corpusdb._stat_file_identity(path.stat())
            native = (crt[3] + 10, crt[4] + 20, crt[0], crt[1], crt[2] + 30)
            with mock.patch.object(
                    corpusdb.fileops, "file_identity", return_value=native), \
                    mock.patch.object(
                        corpusdb.fileops, "file_identity_fd", return_value=native):
                expected = corpusdb._proof_file_identity(path)
                with corpusdb._open_derived_file(path, expected) as stream:
                    self.assertEqual(stream.read(), b"fixture")

        self.assertEqual(expected[2], native[4])
        self.assertNotEqual(expected[2], crt[2])

    def test_windows_metadata_token_is_checked_without_full_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"fixture")
            token = 0xD452CC5DC54AC845
            row = self._row(path, {"Metadata": token})
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state",
                        return_value=(91, token)) as state, \
                    mock.patch.object(
                        corpusdb, "_content_sha256",
                        side_effect=AssertionError("metadata proof must stay bounded")):
                problem = corpusdb._validate_derived_file(root, row)

        self.assertIsNone(problem)
        self.assertEqual(state.call_count, 1)

    def test_windows_metadata_token_rejects_same_stat_interior_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"a" * 2048)
            token = 0xD452CC5DC54AC845
            row = self._row(path, {"Metadata": token})
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state",
                        return_value=(92, token + 1)):
                problem = corpusdb._validate_derived_file(root, row)

        self.assertIn("change identity does not match", problem)

    def test_windows_metadata_token_fails_closed_when_usn_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"fixture")
            row = self._row(path, {"Metadata": 1})
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state",
                        side_effect=OSError("USN unavailable")):
                problem = corpusdb._validate_derived_file(root, row)

        self.assertIn("cannot be verified", problem)

    def test_windows_content_proof_uses_non_usn_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"fixture")
            row = self._row(
                path, {"ContentSha256": list(corpusdb._content_sha256(path))})
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state",
                        return_value=(93, None)) as state:
                problem = corpusdb._validate_derived_file(root, row)

        self.assertIsNone(problem)
        self.assertEqual(
            state.call_args_list,
            [
                mock.call(path, include_usn=False),
                mock.call(path, include_usn=False),
            ],
        )

    def test_windows_content_fallback_rejects_unedged_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"a" * 2048)
            row = self._row(
                path, {"ContentSha256": list(corpusdb._content_sha256(path))})
            stamp = path.stat().st_mtime_ns
            with path.open("r+b") as stream:
                stream.seek(900)
                stream.write(b"b" * 32)
            os.utime(path, ns=(stamp, stamp))
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"), \
                    mock.patch.object(
                        corpusdb, "_windows_file_state", return_value=(93, None)):
                problem = corpusdb._validate_derived_file(root, row)

        self.assertIn("content identity does not match", problem)

    @unittest.skipUnless(os.name == "nt", "Windows file-USN contract")
    def test_real_windows_usn_rejects_restored_mtime_interior_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            path.write_bytes(b"a" * 2048)
            try:
                _, token = corpusdb._windows_file_state(path, include_usn=True)
            except OSError as error:
                self.skipTest(f"volume has no file USN: {error}")
            row = self._row(path, {"Metadata": token})
            self.assertIsNone(corpusdb._validate_derived_file(root, row))
            stamp = path.stat().st_mtime_ns
            with path.open("r+b") as stream:
                stream.seek(900)
                stream.write(b"b" * 32)
            os.utime(path, ns=(stamp, stamp))
            problem = corpusdb._validate_derived_file(root, row)

        self.assertIn("change identity does not match", problem)


if __name__ == "__main__":
    unittest.main()
