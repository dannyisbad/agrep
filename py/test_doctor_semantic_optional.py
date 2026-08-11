"""Regressions for healthy core-only installs without semantic dependencies."""

from __future__ import annotations

import contextlib
from collections import Counter
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir, without_store_override  # noqa: E402

isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)

import cli  # noqa: E402
import corpusdb  # noqa: E402
import doctor  # noqa: E402
import embedder  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402
import teach  # noqa: E402


def _missing_semantic() -> dict:
    return {
        "live": False,
        "available": False,
        "optional": True,
        "install_hint": doctor.SEMANTIC_INSTALL_HINT,
        "unavailable_reason": "missing-optional-dependencies",
        "deps": {"numpy": False, "onnxruntime": False, "tokenizers": False},
        "model_cached": False,
        "embeddings": "unknown",
        "embedding_coverage": None,
        "embed_job": "idle",
        "resident_worker": {"running": False},
    }


class CorpusQualityDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        # readiness weighs the corpus against the stores it can discover, and
        # these fixtures are written for platform discovery, not for the
        # harness's empty sandbox home.
        override = without_store_override()
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)

    @staticmethod
    def _write_corpus(
            path: Path, rows: list[tuple], *,
            schema: str | None = None, stamp: str | None = None,
            family_stamp: str | None = None,
            include_family_meta: bool = True,
            include_family_table: bool = True) -> None:
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute(
            "CREATE TABLE msgs ("
            "agent TEXT, model TEXT, model_source TEXT, who TEXT)"
        )
        if include_family_table:
            db.execute(
                "CREATE TABLE session_family ("
                "session TEXT PRIMARY KEY, root TEXT NOT NULL, "
                "side INTEGER NOT NULL CHECK(side IN (0, 1)))"
            )
        db.executemany(
            "INSERT INTO msgs (agent, model, model_source, who) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        meta = [
            ("schema", corpusdb._SCHEMA if schema is None else schema),
            ("stamp", corpusdb._stamp() if stamp is None else stamp),
            ("build_id", "0123456789abcdef0123"),
        ]
        if include_family_meta:
            meta.append((
                "family_stamp",
                (doctor.common.SESSION_FAMILY_MISSING_STAMP
                 if family_stamp is None else family_stamp),
            ))
        db.executemany("INSERT INTO meta VALUES (?, ?)", meta)
        db.commit()
        db.close()

    def test_quality_reads_published_source_rows_with_legacy_semantics(self) -> None:
        rows = [
            ("codex", "gpt-5", "session", "user"),
            ("codex", "", "unknown", "user"),
            ("claude", "sonnet", "", "user"),
            ("codex", "   ", None, "user"),
            ("codex", "", None, "subagent"),
            ("codex", "", None, "control"),
            ("codex", "", "session", "recap"),
            ("codex", "", None, None),
            ("codex", "gpt-5", "session", "agent"),
            ("codex", "", "unknown", "tool"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.object(doctor.common, "MESSAGES_PATH",
                                    mock.Mock()) as messages):
                self._write_corpus(root / "corpus.db", rows)
                quality = doctor._corpus_quality()
        messages.open.assert_not_called()
        self.assertEqual(quality["readiness"]["state"], "ready")
        self.assertEqual(quality["n"], 8)
        self.assertEqual(quality["accountable"], 4)
        self.assertEqual(quality["with_model"], 2)
        self.assertEqual(quality["unknown"], 2)
        self.assertEqual(
            quality["by_who"],
            {"user": 4, "subagent": 1, "control": 1, "recap": 1, None: 1},
        )
        self.assertEqual(
            quality["by_source"],
            {"session": 2, "unknown": 5, "explicit": 1},
        )
        self.assertEqual(quality["unknown_by_agent"], {"codex": 2})

    def test_quality_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "corpus.db"
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                self._write_corpus(
                    path, [("codex", "gpt-5", "session", "user")])
                before = path.read_bytes()
                quality = doctor._corpus_quality()
            self.assertEqual(quality["n"], 1)
            self.assertEqual(quality["readiness"]["state"], "ready")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / "corpus.db-wal").exists())
            self.assertFalse((root / "corpus.db-journal").exists())

    def test_readiness_distinguishes_missing_corrupt_and_stale(self) -> None:
        expected = {
            "missing": "missing",
            "corrupt": "corrupt",
            "wrong-schema": "stale",
            "old-source": "stale",
        }
        for fixture, state in expected.items():
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / "corpus.db"
                with mock.patch.object(doctor.common, "DATA_DIR", root):
                    if fixture == "corrupt":
                        path.write_text("not sqlite", encoding="utf-8")
                    elif fixture == "wrong-schema":
                        self._write_corpus(path, [], schema="obsolete")
                    elif fixture == "old-source":
                        self._write_corpus(path, [], stamp="obsolete")
                    readiness = doctor._corpus_db_readiness()
                    quality = doctor._corpus_quality(readiness)
                self.assertEqual(readiness["state"], state)
                self.assertEqual(quality["readiness"]["state"], state)
                self.assertEqual(quality["n"], 0)
                self.assertEqual(quality["accountable"], 0)
                self.assertEqual(quality["with_model"], 0)
                self.assertEqual(quality["unknown"], 0)
                self.assertFalse(quality["by_who"])
                self.assertFalse(quality["by_source"])
                self.assertFalse(quality["unknown_by_agent"])

    def test_quick_check_raise_and_non_ok_rows_are_corrupt(self) -> None:
        # Where the corruption is caught differs by SQLite build (macOS at
        # quick_check, Linux in the vtable constructor); both are the same verdict.
        for shadow, expected in (
                ("config", ("vtable constructor",)),
                ("data", ("quick_check failed", "vtable constructor"))):
            with self.subTest(shadow=shadow), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / "corpus.db"
                with mock.patch.object(doctor.common, "DATA_DIR", root):
                    self._write_corpus(path, [])
                    db = sqlite3.connect(path)
                    db.execute("CREATE VIRTUAL TABLE msgs_fts USING fts5(body)")
                    db.execute("INSERT INTO msgs_fts VALUES ('fixture')")
                    db.execute(f"DROP TABLE msgs_fts_{shadow}")
                    db.commit()
                    db.close()
                    readiness = doctor._corpus_db_readiness(deep=True)
            self.assertEqual(readiness["state"], "corrupt")
            self.assertTrue(
                any(mark in readiness["detail"] for mark in expected),
                readiness["detail"])

    def test_readiness_reports_bound_rebuild_and_safe_locked_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "corpus.db"
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                self._write_corpus(path, [])
                identity = corpusdb._optional_sqlite_identity(path)
                path.with_name(".corpusdb-rebuild").write_text(json.dumps({
                    "version": corpusdb._QUERY_REBUILD_MARKER_VERSION,
                    "database_identity": list(identity),
                    "build_id": "0123456789abcdef0123",
                }), encoding="utf-8")
                before_db = path.read_bytes()
                before_marker = path.with_name(".corpusdb-rebuild").read_bytes()
                pending = doctor._corpus_db_readiness()
                self.assertEqual(path.read_bytes(), before_db)
                self.assertEqual(
                    path.with_name(".corpusdb-rebuild").read_bytes(), before_marker)
                path.with_name(".corpusdb-rebuild").unlink()
                # isolation_level=None: true autocommit, so the manual
                # BEGIN survives the DDL (pre-3.12 sqlite3 implicitly commits
                # around CREATE TABLE otherwise and the lock evaporates).
                holder = sqlite3.connect(path, timeout=0, isolation_level=None)
                holder.execute("PRAGMA locking_mode=EXCLUSIVE")
                holder.execute("BEGIN EXCLUSIVE")
                # A write engages the exclusive lock for real; BEGIN alone
                # leaves Linux readers unblocked.
                holder.execute("CREATE TABLE _lock_probe(x)")
                try:
                    busy = doctor._corpus_db_readiness()
                finally:
                    holder.rollback()
                    holder.close()
        self.assertEqual(pending["state"], "rebuild-pending")
        self.assertEqual(busy["state"], "busy")

    def test_model_attribution_has_a_shape_in_every_corpus_state(self) -> None:
        unavailable = (
            ({"state": "never-built"}, {"state": "missing"}, "never-built"),
            ({"state": "proof-damaged"}, {"state": "ready"}, "proof-damaged"),
            ({"state": "ready", "sessions": 1, "messages": 1},
             {"state": "stale"}, "stale"),
        )
        for summary, readiness, reason in unavailable:
            with self.subTest(reason=reason):
                # Attribution is a deep check now (law 4: routine renders
                # nothing about a scan it declined to run), so the shape
                # this pins is the deep one it always meant.
                value = doctor._model_attribution(
                    summary, readiness, deep=True)
                self.assertEqual(value["state"], "unavailable")
                self.assertEqual(value["reason"], reason)
                self.assertIn("percent", value)
                json.dumps(value)
        quality = {
            "readiness": {"state": "ready"}, "accountable": 2,
            "with_model": 1, "unknown": 1, "by_source": Counter(),
            "unknown_by_agent": Counter({"codex": 1}), "by_who": Counter(),
        }
        with mock.patch.object(doctor, "_corpus_quality", return_value=quality):
            value = doctor._model_attribution(
                {"state": "ready", "sessions": 1, "messages": 2},
                {"state": "ready"}, deep=True)
        self.assertEqual(value["state"], "partial")
        self.assertEqual(value["percent"], 50.0)

    def test_index_state_distinguishes_virgin_from_damaged_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(doctor.common, "DATA_DIR", root), \
                    mock.patch.object(doctor.common, "MESSAGES_PATH",
                                      root / "messages.jsonl"), \
                    mock.patch.object(doctor.common, "INGEST_SIG_PATH",
                                      root / ".ingest.sig"), \
                    mock.patch.object(doctor.common, "index_summary",
                                      return_value=None):
                self.assertEqual(
                    doctor._index_summary_state()["state"], "never-built")
                (root / "corpus.db").write_bytes(b"served corpus")
                self.assertEqual(
                    doctor._index_summary_state()["state"], "proof-damaged")

    def test_doctor_json_always_carries_corpus_archive_and_attribution_state(self) -> None:
        archive_state = {
            "enabled": True, "state": "capture-blocked",
            "manifest_state": "future", "files": 1,
            "last_pass": {"outcome": "capture-blocked", "age_s": 3.0,
                          "fresh": True},
        }
        with mock.patch.object(doctor, "_semantic_probe",
                               return_value=_missing_semantic()), \
                mock.patch.object(doctor, "_orphan_inventory",
                                  return_value={"count": 0, "bytes": 0}), \
                mock.patch.object(doctor, "_store_counts", return_value=[]), \
                mock.patch.object(doctor, "_corpus_db_readiness",
                                  return_value={"state": "missing"}), \
                mock.patch.object(doctor.common, "index_summary",
                                  return_value={"state": "proof-damaged"}), \
                mock.patch.object(doctor, "_archive_probe", return_value=archive_state), \
                mock.patch.object(doctor.common, "data_dir_usage",
                                  return_value={"files": 0, "bytes": 0}), \
                mock.patch.object(doctor.indexd_runtime, "indexd_resource_status",
                                  return_value={"running": False}), \
                mock.patch.object(doctor.common, "detected_stores", return_value=[]):
            payload = doctor.probe()
        self.assertEqual(payload["core"]["indexed"]["state"], "proof-damaged")
        self.assertEqual(payload["archive"]["manifest_state"], "future")
        # A routine probe does not scan for attribution, so the machine
        # shape says exactly that rather than borrowing a failure word.
        self.assertEqual(
            payload["model_attribution"]["state"], "not-inspected")
        json.dumps(payload)

    def test_old_schema_without_current_columns_is_stale_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "corpus.db"
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("CREATE TABLE msgs (agent TEXT, who TEXT)")
                db.executemany("INSERT INTO meta VALUES (?, ?)", [
                    ("schema", "10"),
                    ("stamp", "legacy"),
                ])
                db.commit()
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                readiness = doctor._corpus_db_readiness()
        self.assertEqual(readiness["state"], "stale")
        self.assertIn("schema 10", readiness["detail"])

    def test_readiness_rejects_stale_or_missing_family_index(self) -> None:
        fixtures = {
            "stale-family": ({"family_stamp": "obsolete"}, "stale"),
            "missing-family-meta": ({"include_family_meta": False}, "corrupt"),
            "missing-family-table": ({"include_family_table": False}, "corrupt"),
        }
        for fixture, (options, state) in fixtures.items():
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                with mock.patch.object(doctor.common, "DATA_DIR", root):
                    self._write_corpus(root / "corpus.db", [], **options)
                    readiness = doctor._corpus_db_readiness()
                self.assertEqual(readiness["state"], state)

    def test_quality_exposes_busy_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "corpus.db"
            path.write_bytes(b"busy fixture")
            busy_error = sqlite3.OperationalError(
                "cannot commit transaction - SQL statements in progress")
            busy_error.sqlite_errorcode = getattr(sqlite3, "SQLITE_BUSY", 5)
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.object(
                      doctor.sqlite3, "connect",
                      side_effect=busy_error)):
                quality = doctor._corpus_quality()
        self.assertEqual(quality["readiness"]["state"], "busy")
        self.assertEqual(quality["n"], 0)

    def test_sqlite_wording_cannot_override_the_structural_error_code(self) -> None:
        corrupt = sqlite3.DatabaseError("database is locked and busy")
        corrupt.sqlite_errorcode = getattr(sqlite3, "SQLITE_CORRUPT", 11)
        busy = sqlite3.OperationalError(
            "cannot commit transaction - SQL statements in progress")
        busy.sqlite_errorcode = getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)
        self.assertEqual(doctor._sqlite_failure(corrupt)["state"], "corrupt")
        self.assertEqual(doctor._sqlite_failure(busy)["state"], "busy")

    def test_sqlite_error_without_structural_code_is_unavailable(self) -> None:
        # Python 3.10 never exposes sqlite_errorcode; SQLite's stable
        # diagnostic strings still classify. Only an opaque message stays
        # honestly indistinguishable.
        locked = sqlite3.OperationalError("database is locked")
        self.assertFalse(hasattr(locked, "sqlite_errorcode"))
        self.assertEqual(doctor._sqlite_failure(locked)["state"], "busy")
        notadb = sqlite3.DatabaseError("file is not a database")
        self.assertEqual(doctor._sqlite_failure(notadb)["state"], "corrupt")
        opaque = sqlite3.OperationalError("mysterious failure")
        failure = doctor._sqlite_failure(opaque)
        self.assertEqual(failure["state"], "unavailable")
        self.assertIn("cannot be distinguished safely", failure["detail"])

    def test_a_stale_database_renders_neither_a_row_nor_a_borrowed_zero(
            self) -> None:
        # Was: a stale-db row plus an attribution row blaming it - one
        # fault charged twice. A stale database is agrep's own rebuild
        # (law 1) and renders nothing; the borrowed zero stays forbidden.
        summary = {
            "messages": 1,
            "sessions": 1,
            "age_s": 0,
            "agents": [],
            "per_agent": [],
        }
        readiness = {"state": "stale", "detail": "source generation is newer"}
        output = io.StringIO()
        missing_semantic = _missing_semantic()
        with (mock.patch.object(doctor.common, "index_summary",
                               return_value=summary),
              mock.patch.object(doctor, "_corpus_db_readiness",
                                return_value=readiness),
              mock.patch.object(
                  corpusdb, "search_generation_health",
                  return_value={"state": "ready"}),
              mock.patch.object(doctor, "_corpus_quality",
                                return_value={
                                    "n": 0, "accountable": 0,
                                    "readiness": readiness,
                                }),
              mock.patch.object(doctor.indexd_runtime, "indexd_resource_status",
                                return_value={"running": False}),
              mock.patch.object(doctor.indexd_runtime, "indexd_failing",
                                return_value=(0, "")),
              mock.patch.object(
                  doctor.indexd_runtime, "kick_background_repair"),
              mock.patch.object(doctor, "_semantic_probe",
                                return_value=missing_semantic),
              mock.patch.object(doctor, "_store_counts", return_value=[]),
              mock.patch.object(doctor.common, "detected_stores",
                                return_value=[]),
              mock.patch.object(doctor.common, "data_dir_usage",
                                return_value={"bytes": 0, "files": 0}),
              mock.patch.object(doctor, "_footprint_breakdown",
                                return_value=""),
              contextlib.redirect_stdout(output)):
            doctor.report(deep=True)
        rendered = output.getvalue()
        self.assertNotIn("stale - source generation is newer", rendered)
        # the deep banner names the check it runs; what may not appear is a
        # rendered ROW for it
        self.assertNotRegex(rendered, r"\]\s+model attribution")
        self.assertNotIn("not available because", rendered)
        self.assertNotIn("0.0% of your turns", rendered)

    def test_report_explains_each_protected_freshness_owner(self) -> None:
        summary = {
            "messages": 1,
            "sessions": 1,
            "age_s": 0,
            "agents": [],
            "per_agent": [],
        }
        readiness = {"state": "ready", "detail": "current"}

        def render(
                status: dict[str, object],
                kick: indexd_runtime.RepairKick | None = None) -> str:
            output = io.StringIO()
            kick = kick or indexd_runtime.RepairKick(True, "")
            with mock.patch.object(
                    doctor.common, "index_summary",
                    return_value=summary), \
                    mock.patch.object(
                        doctor, "_corpus_db_readiness",
                        return_value=readiness), \
                    mock.patch.object(
                        doctor, "_corpus_quality",
                        return_value={
                            "n": 0,
                            "accountable": 0,
                            "readiness": readiness,
                        }), \
                    mock.patch.object(
                        doctor.indexd_runtime, "indexd_resource_status",
                        return_value=status), \
                    mock.patch.object(
                        doctor.indexd_runtime, "indexd_failing",
                        return_value=(0, "")), \
                    mock.patch.object(
                        doctor.indexd_runtime, "indexing_failure",
                        return_value=None), \
                    mock.patch.object(
                        doctor.indexd_runtime, "host_block_escalation",
                        return_value=""), \
                    mock.patch.object(
                        doctor.indexd_runtime, "kick_background_repair",
                        return_value=kick), \
                    mock.patch.object(
                        doctor, "_semantic_probe",
                        return_value=_missing_semantic()), \
                    mock.patch.object(
                        doctor, "_store_counts", return_value=[]), \
                    mock.patch.object(
                        doctor.common, "detected_stores",
                        return_value=[]), \
                    mock.patch.object(
                        doctor.common, "data_dir_usage",
                        return_value={"bytes": 0, "files": 0}), \
                    mock.patch.object(
                        doctor, "_footprint_breakdown",
                        return_value=""), \
                    contextlib.redirect_stdout(output):
                doctor.report()
            return output.getvalue()

        # every blocked ownership state is one reader situation - the index is
        # not moving - so all five render the one sentence surface owns, and
        # none of them names the ownership machinery (law 4)
        states = (
            "hostile", "unverifiable", "legacy-owner", "incompatible",
            "spawn-guard")
        for state in states:
            with self.subTest(state=state):
                rendered = render({
                    "running": False,
                    "blocked": True,
                    "state": state,
                })
                self.assertIn(
                    "new chats are not being indexed - another agrep version "
                    "holds the indexer here", rendered)
                self.assertIn("searches serve the last good index", rendered)
                for leak in ("ownership state", "daemon restart fenced",
                             "replacement launch", state):
                    self.assertNotIn(leak, rendered)

        starting = render({
            "running": False,
            "starting": True,
            "age_s": 1.2,
        })
        self.assertIn("freshness startup in progress - 1.2s", starting)
        self.assertNotIn("daemon starting", starting)
        backoff = render({
            "running": False,
            "backoff": True,
            "age_s": 8.0,
        })
        self.assertIn(
            "daemon launch cooling down - a later background attempt retries "
            "after cooldown",
            backoff,
        )
        self.assertNotIn("searches refresh inline", backoff)

        launched = render({"running": False})
        self.assertIn("daemon starting - a publication is in flight", launched)
        self.assertNotIn("agrep index", launched)

        declined = render(
            {"running": False}, indexd_runtime.RepairKick(False, "readonly"))
        self.assertIn("daemon not running - the data dir is read-only", declined)
        self.assertNotIn("agrep index", declined)

        unverified = render(
            {"running": False}, indexd_runtime.RepairKick(False, "probe-failed"))
        self.assertIn(
            "daemon not running - the repair state could not be verified",
            unverified)

    def test_quality_uses_the_sidecar_free_snapshot_reader(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "corpus.db"
            path.write_bytes(b"fixture")
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.object(
                      corpusdb, "_connect_read_snapshot",
                      side_effect=sqlite3.OperationalError("unavailable"),
                  ) as connect):
                quality = doctor._corpus_quality()
        self.assertEqual(quality["n"], 0)
        connect.assert_called_once_with(
            path, 0,
            max_clone_bytes=corpusdb._ROUTINE_ALIAS_CLONE_MAX_BYTES)

    def test_protected_wal_diagnostics_never_create_live_shm_or_mutate_source(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-wal-") as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            seed = root / "seed.db"
            writer = sqlite3.connect(seed)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                writer.execute(
                    "CREATE TABLE msgs ("
                    "agent TEXT, model TEXT, model_source TEXT, who TEXT)")
                writer.execute(
                    "CREATE TABLE session_family ("
                    "session TEXT PRIMARY KEY, root TEXT NOT NULL, "
                    "side INTEGER NOT NULL CHECK(side IN (0, 1)))")
                writer.executemany("INSERT INTO meta VALUES (?, ?)", [
                    ("schema", corpusdb._SCHEMA),
                    ("stamp", "stable"),
                    ("family_stamp", "family"),
                ])
                writer.execute(
                    "INSERT INTO msgs VALUES ('codex', 'gpt-5', "
                    "'session', 'user')")
                writer.commit()
                seed_wal = Path(f"{seed}-wal")
                self.assertTrue(seed_wal.exists())
                db_path = data / "corpus.db"
                wal_path = data / "corpus.db-wal"
                shutil.copyfile(seed, db_path)
                shutil.copyfile(seed_wal, wal_path)
                self.assertFalse((data / "corpus.db-shm").exists())

                def snapshot() -> tuple:
                    entries = []
                    for path in sorted(data.iterdir(), key=lambda p: p.name):
                        found = path.lstat()
                        entries.append((
                            path.name, path.read_bytes(), found.st_mode,
                            found.st_size, found.st_mtime_ns, found.st_ctime_ns,
                        ))
                    found = data.stat()
                    return (
                        found.st_mode, found.st_mtime_ns, found.st_ctime_ns,
                        tuple(entries),
                    )

                before = snapshot()
                with (
                    mock.patch.object(doctor.common, "DATA_DIR", data),
                    mock.patch.object(corpusdb, "_stamp", return_value="stable"),
                    mock.patch.object(
                        corpusdb, "_stamps_equal",
                        side_effect=lambda left, right: left == right),
                    mock.patch.object(
                        doctor.common, "session_family_source_stamp",
                        return_value="family"),
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": os.fspath(data)},
                        clear=False),
                ):
                    readiness = doctor._corpus_db_readiness()
                    quality = doctor._corpus_quality(readiness)
                self.assertEqual(readiness["state"], "ready", readiness)
                self.assertEqual(quality["n"], 1)
                self.assertEqual(snapshot(), before)
                self.assertFalse((data / "corpus.db-shm").exists())
            finally:
                writer.close()


class OptionalSemanticDoctorTests(unittest.TestCase):
    def test_routine_semantic_probe_does_not_invent_an_install_remedy(self) -> None:
        with (mock.patch.object(doctor, "_routine_semantic_job", return_value={}),
              mock.patch.object(semantic, "embed_running", return_value=False)):
            observed = doctor._semantic_probe(deep=False)
        self.assertFalse(observed["runtime_verified"])
        self.assertIsNone(observed["install_hint"])

    def test_doctor_and_status_share_the_generated_install_command(self) -> None:
        self.assertIsNotNone(doctor.SEMANTIC_INSTALL_COMMAND)
        self.assertEqual(
            doctor.SEMANTIC_INSTALL_COMMAND, cli.SEMANTIC_INSTALL_COMMAND)

    def test_scheduler_never_spawns_optional_worker_without_runtime(self) -> None:
        with (mock.patch.object(semantic, "runtime_dependencies_available",
                               return_value=False),
              mock.patch.object(semantic.subprocess, "Popen") as spawn,
              mock.patch.object(semantic, "embedding_coherence") as coherence):
            fresh = semantic.ensure_fresh_async(max_new=128)
            refs = semantic.ensure_refs_async()
            sync = semantic.refresh_embeddings_sync(max_new=128)
        self.assertEqual(fresh["state"], "optional-runtime-unavailable")
        self.assertEqual(refs["state"], "optional-runtime-unavailable")
        self.assertEqual(sync["state"], "optional-runtime-unavailable")
        self.assertFalse(sync["ok"])
        spawn.assert_not_called()
        coherence.assert_not_called()

    def test_fix_skips_prefetch_and_succeeds_for_each_missing_dependency(self) -> None:
        for missing in ("numpy", "onnxruntime", "tokenizers"):
            with self.subTest(missing=missing):
                output = io.StringIO()
                with (mock.patch.object(
                        doctor, "_dep_present", side_effect=lambda name, m=missing: name != m),
                      contextlib.redirect_stdout(output)):
                    self.assertEqual(doctor.fix(), 0)
                rendered = output.getvalue()
                self.assertIn("semantic model prefetch skipped", rendered)
                self.assertIn("semantic search is optional", rendered)
                self.assertIn("pip install", rendered)
                self.assertIn("agrep[semantic]==0.2.0", rendered)
                self.assertNotIn("hard dependency", rendered)

    def test_doctor_fix_command_succeeds_without_semantic_runtime(self) -> None:
        output = io.StringIO()
        with (mock.patch.object(doctor, "report", return_value={}),
              mock.patch.object(doctor, "_semantic_probe", return_value=_missing_semantic()),
              contextlib.redirect_stdout(output)):
            self.assertEqual(doctor.main(["--fix"]), 0)
        self.assertIn("prefetch skipped", output.getvalue())

    def test_setup_succeeds_and_explains_optional_unlock(self) -> None:
        before = {"tiers": ["core"], "fixes": [doctor.SEMANTIC_UNLOCK]}
        output = io.StringIO()
        with (mock.patch.object(doctor, "_json_report", return_value=before),
              mock.patch.object(doctor, "_semantic_probe", return_value=_missing_semantic()),
              contextlib.redirect_stdout(output)):
            self.assertEqual(doctor.setup(), 0)
        rendered = output.getvalue()
        self.assertIn("tiers now: core", rendered)
        self.assertIn("prefetch skipped", rendered)
        self.assertIn("pip install", rendered)
        self.assertIn("agrep[semantic]==0.2.0", rendered)

    def test_setup_no_semantic_is_one_run_network_sterile(self) -> None:
        output = io.StringIO()
        with (mock.patch.object(
                  doctor, "_json_report",
                  return_value={"tiers": ["core"], "fixes": []}),
              mock.patch.object(doctor, "fix") as prefetch,
              contextlib.redirect_stdout(output)):
            self.assertEqual(
                doctor.setup(prefetch_semantic=False), 0)
        prefetch.assert_not_called()
        self.assertIn(
            "semantic model prefetch skipped for this setup run",
            output.getvalue())

    def test_doctor_rejects_no_semantic_without_setup(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(doctor.main(["--no-semantic"]), 2)
        self.assertIn("has no effect without --setup", error.getvalue())

    def test_cli_setup_no_semantic_skips_both_semantic_starts(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("", encoding="utf-8")
            (root / "teach.json").write_text("{}", encoding="utf-8")
            with (mock.patch.object(doctor, "main", return_value=0) as diagnose,
                  mock.patch.object(cli.common, "setting", return_value="auto"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "ready"}),
                  mock.patch.object(
                      cli.common, "index_summary",
                      return_value={"messages": 0, "sessions": 0}),
                  mock.patch.object(cli.common, "lap") as lap,
                  mock.patch.object(teach, "teach", return_value=0),
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(embedder, "ensure_model") as model,
                  mock.patch.object(semantic, "ensure_fresh_async") as start,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=[], yes=True, no_teach=False, no_semantic=True,
                    archive=False, no_archive=True))
        self.assertEqual(rc, 0)
        diagnose.assert_called_once_with(["--setup", "--no-semantic"])
        lap.assert_called_once_with("dependencies")
        model.assert_not_called()
        start.assert_not_called()
        self.assertIn("setup complete", output.getvalue())

    def test_cli_setup_explains_a_semantic_start_exception_once(self) -> None:
        output = io.StringIO()
        with (mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "runtime_dependencies_available",
                                return_value=True),
              mock.patch.object(semantic, "ensure_fresh_async",
                                side_effect=OSError("spawn failed")),
              contextlib.redirect_stdout(output)):
            cli._setup_start_semantic("agrep")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("meaning search deferred"), 1)
        self.assertIn("background worker did not start", rendered)
        self.assertIn("keyword search is ready", rendered)

    def test_cli_setup_explains_a_semantic_runtime_probe_error_once(self) -> None:
        output = io.StringIO()
        with (mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "runtime_dependencies_available",
                                side_effect=OSError("probe failed")),
              contextlib.redirect_stdout(output)):
            cli._setup_start_semantic("agrep")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("meaning search deferred"), 1)
        self.assertIn("optional runtime could not load", rendered)
        self.assertIn("keyword search is ready", rendered)

    def test_cli_setup_explains_nonrunning_semantic_states_once(self) -> None:
        for state, detail in (
                ("failed", "waiting to retry"),
                ("read-only", "data directory is read-only"),
                ("unexpected-state", "background worker did not start")):
            output = io.StringIO()
            with self.subTest(state=state), \
                    mock.patch.object(embedder, "ensure_model"), \
                    mock.patch.object(
                        semantic, "runtime_dependencies_available",
                        return_value=True), \
                    mock.patch.object(
                        semantic, "ensure_fresh_async",
                        return_value={"state": state}), \
                    contextlib.redirect_stdout(output):
                cli._setup_start_semantic("agrep")
            rendered = output.getvalue()
            self.assertEqual(rendered.count("meaning search deferred"), 1)
            self.assertIn(detail, rendered)
            self.assertIn("keyword search is ready", rendered)

    def test_cli_setup_does_not_invent_a_foreign_owner_upgrade(self) -> None:
        owner = "0123456789abcdef0123"
        output = io.StringIO()
        with (mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(
                  semantic, "runtime_dependencies_available",
                  return_value=True),
              mock.patch.object(
                  semantic, "ensure_fresh_async",
                  return_value={
                      "state": "read-only",
                      "reason": f"derived stores owned-by {owner}",
                      "owner_build": owner,
                  }),
              contextlib.redirect_stdout(output)):
            cli._setup_start_semantic("agrep")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("meaning search deferred"), 1)
        self.assertIn("another agrep build owns its current index", rendered)
        self.assertIn("keyword search is ready", rendered)
        self.assertNotIn("data directory is read-only", rendered)
        self.assertNotIn("upgrade is still finishing", rendered)
        self.assertNotIn(owner, rendered)

    def test_cli_setup_reuses_a_verified_index_upgrade_disclosure(self) -> None:
        owner = "0123456789abcdef0123"
        output = io.StringIO()
        with (mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(
                  semantic, "runtime_dependencies_available",
                  return_value=True),
              mock.patch.object(
                  semantic, "ensure_fresh_async",
                  return_value={
                      "state": "read-only",
                      "reason": f"derived stores owned-by {owner}",
                      "owner_build": owner,
                  }),
              contextlib.redirect_stdout(output)):
            cli._setup_start_semantic(
                "agrep", index_upgrade_disclosed=True)
        self.assertEqual(output.getvalue(), "")

    def test_cli_setup_accepts_ready_or_running_semantic_states(self) -> None:
        for state in ("ready", "running"):
            output = io.StringIO()
            with self.subTest(state=state), \
                    mock.patch.object(embedder, "ensure_model"), \
                    mock.patch.object(
                        semantic, "runtime_dependencies_available",
                        return_value=True), \
                    mock.patch.object(
                        semantic, "ensure_fresh_async",
                        return_value={"state": state}), \
                    contextlib.redirect_stdout(output):
                cli._setup_start_semantic("agrep")
            rendered = output.getvalue()
            self.assertNotIn("deferred", rendered)
            if state == "running":
                self.assertIn("building the embeddings index", rendered)
            else:
                self.assertEqual(rendered, "")

    def test_setup_with_embeddings_off_is_network_sterile_and_enrollment_safe(
            self) -> None:
        before = {"tiers": ["core", "semantic"], "fixes": []}
        output = io.StringIO()
        with (mock.patch.object(doctor, "_json_report", return_value=before),
              mock.patch.object(doctor.common, "setting", return_value="off"),
              mock.patch.object(doctor, "_semantic_probe") as probe,
              mock.patch.object(embedder, "ensure_model") as fetch,
              contextlib.redirect_stdout(output)):
            self.assertEqual(doctor.setup(), 0)
        probe.assert_not_called()
        fetch.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("semantic tier deferred", rendered)
        self.assertIn("blocks model downloads", rendered)
        self.assertIn("agent enrollment remain fully available", rendered)
        self.assertIn("set embeddings auto", rendered)

    def test_setup_defers_an_offline_optional_fetch_instead_of_aborting(
            self) -> None:
        available = {
            **_missing_semantic(),
            "available": True,
            "deps": {
                "numpy": True, "onnxruntime": True, "tokenizers": True},
        }
        output = io.StringIO()
        with (mock.patch.object(
                  doctor, "_json_report",
                  return_value={"tiers": ["core", "semantic"], "fixes": []}),
              mock.patch.object(doctor.common, "setting", return_value="auto"),
              mock.patch.object(
                  doctor, "_semantic_probe", return_value=available),
              mock.patch.object(
                  embedder, "ensure_model",
                  side_effect=embedder.EmbedderUnavailable("network offline")),
              contextlib.redirect_stdout(output)):
            self.assertEqual(doctor.setup(), 0)
        rendered = output.getvalue()
        self.assertIn("model fetch failed", rendered)
        self.assertIn("semantic tier deferred", rendered)
        self.assertIn("agent enrollment continue", rendered)
        self.assertIn("when network and cache access return", rendered)

    def test_default_setup_discloses_the_pinned_model_prefetch(self) -> None:
        available = {
            **_missing_semantic(),
            "available": True,
            "deps": {
                "numpy": True, "onnxruntime": True, "tokenizers": True},
        }
        output = io.StringIO()
        with (mock.patch.object(doctor.common, "setting", return_value="auto"),
              mock.patch.object(
                  doctor, "_semantic_probe", return_value=available),
              mock.patch.object(embedder, "ensure_model"),
              contextlib.redirect_stdout(output)):
            self.assertEqual(doctor.fix(), 0)
        self.assertIn(
            "pinned semantic model (~52 MiB, one time)", output.getvalue())

    def test_cli_setup_offline_reaches_enrollment_when_embeddings_are_off(
            self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("", encoding="utf-8")
            (root / "teach.json").write_text("{}", encoding="utf-8")

            def run_doctor(*_args, **_kwargs):
                return SimpleNamespace(returncode=doctor.setup())

            with (mock.patch.object(doctor, "_json_report",
                                    return_value={"tiers": ["core"], "fixes": []}),
                  mock.patch.object(doctor.common, "setting", return_value="off"),
                  mock.patch.object(cli.common, "setting", return_value="off"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "ready"}),
                  mock.patch.object(cli.common, "index_summary",
                                    return_value={"messages": 0, "sessions": 0}),
                  mock.patch.object(cli.subprocess, "run",
                                    side_effect=run_doctor),
                  mock.patch.object(teach, "teach", return_value=0) as enroll,
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(embedder, "ensure_model") as fetch,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=["--yes", "--no-archive"], yes=False,
                    no_teach=False, archive=False, no_archive=False))
        self.assertEqual(rc, 0)
        enroll.assert_called_once_with(yes=True)
        fetch.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("setup 2/5: agent instructions", rendered)
        self.assertIn("semantic tier deferred", rendered)
        self.assertIn("setup complete", rendered)

    def test_cli_setup_auto_offline_enrolls_without_a_second_network_try(
            self) -> None:
        output = io.StringIO()
        available = {
            **_missing_semantic(),
            "available": True,
            "deps": {
                "numpy": True, "onnxruntime": True, "tokenizers": True},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("", encoding="utf-8")
            (root / "teach.json").write_text("{}", encoding="utf-8")

            def run_doctor(*_args, **_kwargs):
                return SimpleNamespace(returncode=doctor.setup())

            with (mock.patch.object(
                      doctor, "_json_report",
                      return_value={"tiers": ["core", "semantic"], "fixes": []}),
                  mock.patch.object(doctor, "_semantic_probe",
                                    return_value=available),
                  mock.patch.object(doctor.common, "setting",
                                    return_value="auto"),
                  mock.patch.object(cli.common, "setting",
                                    return_value="auto"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "ready"}),
                  mock.patch.object(cli.common, "index_summary",
                                    return_value={"messages": 0, "sessions": 0}),
                  mock.patch.object(cli.subprocess, "run",
                                    side_effect=run_doctor),
                  mock.patch.object(teach, "teach", return_value=0) as enroll,
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(
                      embedder, "ensure_model",
                      side_effect=embedder.EmbedderUnavailable(
                          "network offline")) as model,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=["--yes", "--no-archive"], yes=False,
                    no_teach=False, archive=False, no_archive=False))
        self.assertEqual(rc, 0)
        enroll.assert_called_once_with(yes=True)
        self.assertEqual(
            model.call_args_list,
            [mock.call(), mock.call(download=False)],
        )
        rendered = output.getvalue()
        self.assertIn("semantic tier deferred", rendered)
        self.assertIn("will not retry the network fetch", rendered)
        self.assertIn("setup complete", rendered)

    def test_json_report_marks_semantics_optional_with_install_hint(self) -> None:
        payload = {
            "paths": {},
            "core": {"live": True, "rust": True, "binary": True},
            "semantic": _missing_semantic(),
            "resources": {},
            "detected": [],
        }
        with (mock.patch.object(doctor, "probe", return_value=payload),
              mock.patch.object(doctor.common, "detected_stores", return_value=[])):
            report = doctor._json_report()
        self.assertTrue(report["semantic"]["optional"])
        self.assertEqual(report["semantic"]["unavailable_reason"],
                         "missing-optional-dependencies")
        self.assertIn("pip install", report["semantic"]["install_hint"])
        self.assertIn(
            "agrep[semantic]==0.2.0", report["semantic"]["install_hint"])
        fixes = "\n".join(report["fixes"])
        self.assertIn("pip install", fixes)
        self.assertIn("agrep[semantic]==0.2.0", fixes)
        self.assertNotIn("reinstall agrep", "\n".join(report["fixes"]))

    def test_json_report_does_not_offer_semantic_repairs_while_off(self) -> None:
        unavailable = _missing_semantic()
        missing_embeddings = {
            **_missing_semantic(),
            "available": True,
            "deps": {
                "numpy": True,
                "onnxruntime": True,
                "tokenizers": True,
            },
            "embeddings": "missing-embeddings",
        }
        for label, semantic_state in (
                ("runtime unavailable", unavailable),
                ("embeddings missing", missing_embeddings)):
            payload = {
                "paths": {},
                "core": {"live": True, "rust": True, "binary": True},
                "semantic": semantic_state,
                "resources": {},
                "detected": [],
                "settings": {
                    "embeddings": {
                        "state": "verified", "value": "off",
                        "source": "settings.json",
                    },
                },
            }
            with (
                self.subTest(label=label),
                mock.patch.object(doctor, "probe", return_value=payload),
                  mock.patch.object(
                    doctor.common, "detected_stores", return_value=[]),
            ):
                report = doctor._json_report()
            self.assertEqual(
                report["semantic"]["embeddings_setting"], "off")
            self.assertEqual(report["semantic"]["refresh"], "disabled")
            self.assertIsNone(report["semantic"]["install_hint"])
            self.assertEqual(report["fixes"], [])

    def test_resident_worker_rss_survives_json_and_human_rendering(self) -> None:
        resident = {"running": True, "pid": 42,
                    "rss_bytes": 128 * 1024 ** 2}
        payload = {
            "paths": {},
            "core": {"live": True, "rust": True, "binary": True},
            "semantic": {**_missing_semantic(), "resident_worker": resident},
            "resources": {},
            "detected": [],
        }
        with (mock.patch.object(doctor, "probe", return_value=payload),
              mock.patch.object(doctor.common, "detected_stores", return_value=[])):
            report = doctor._json_report()
        self.assertEqual(report["semantic"]["resident_worker"]["rss_bytes"],
                         128 * 1024 ** 2)
        rendered = doctor._resident_worker_detail(resident)
        self.assertIn(
            "pid 42, 128.0 MiB root RSS; children excluded", rendered)
        self.assertIn("exits after idle", rendered)

    def test_resident_worker_detail_distinguishes_owner_states(self) -> None:
        starting = doctor._resident_worker_detail({
            "running": False, "starting": True, "pid": 42})
        inprocess = doctor._resident_worker_detail({
            "running": False, "inprocess": True, "pid": 43})
        blocked = doctor._resident_worker_detail({
            "running": False, "blocked": True,
            "owner_state": "unverifiable",
            "reason": "the semantic owner identity cannot be verified safely"})
        self.assertIn("pid 42 is transitioning", starting)
        self.assertIn("pid 43 owns semantic resources in-process", inprocess)
        self.assertIn(
            "ownership is blocked: the semantic owner identity cannot be verified safely",
            blocked)
        self.assertIn("duplicate model memory", blocked)

    def test_resident_alive_is_not_claimed_query_serving_without_a_round_trip(
            self) -> None:
        resident = {"running": True, "pid": 42}
        unproven = doctor._resident_worker_detail(resident)
        failed = doctor._resident_worker_detail(
            resident,
            readiness={
                "query_serving": False,
                "reason": "coordination path is blocked",
            })
        proven = doctor._resident_worker_detail(
            resident,
            readiness={"query_serving": True, "roundtrip_ms": 18.25})
        self.assertIn("query round-trip not verified", unproven)
        self.assertIn("alive, but the bounded query failed", failed)
        self.assertIn("query round-trip ready in 18.2ms", proven)
        self.assertNotIn("answers semantic searches", unproven)

    def test_absent_worker_does_not_promise_startup_after_probe_failure(
            self) -> None:
        detail = doctor._resident_worker_detail(
            {"running": False},
            readiness={
                "state": "owner-blocked", "query_serving": False,
                "reason": "another agrep build owns worker startup",
            })
        self.assertIn("not query-serving", detail)
        self.assertIn("another agrep build owns worker startup", detail)
        self.assertNotIn("starts with", detail)

    def test_readiness_detail_exposes_generation_and_live_progress(self) -> None:
        detail = doctor._semantic_readiness_detail({
            "generation_state": "unstable-source",
            "current_generation": False,
            "query_serving": False,
            "refresh": {
                "running": True, "phase": "embedding",
                "done": 411, "total": 33_830,
            },
        })
        self.assertIn("current generation not ready", detail)
        self.assertIn("embeddings unstable-source", detail)
        self.assertIn("refresh running (embedding · 411/33,830 rows)", detail)
        self.assertIn("retry shortly", detail)

    def test_semantic_probe_requests_resident_resource_sample(self) -> None:
        resident = {"running": True, "pid": 42, "rss_bytes": 123_456}
        coherence = {"coherent": True, "state": "current"}
        readiness = {
            "state": "query-serving", "ready": True,
            "query_serving": True, "resident_after": resident,
        }
        with (mock.patch.object(doctor, "_dep_present", return_value=True),
              mock.patch("importlib.import_module"),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value=coherence),
              mock.patch.object(semantic, "read_embed_state", return_value={}),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(
                  semworker, "diagnostic_query_status",
                  return_value=readiness) as query_status):
            result = doctor._semantic_probe()
        query_status.assert_called_once_with(
            ready=True, timeout_s=doctor._SEMANTIC_QUERY_PROBE_TIMEOUT_S)
        self.assertEqual(result["resident_worker"], resident)
        self.assertTrue(result["generation_ready"])
        self.assertTrue(result["live"])
        self.assertTrue(result["query_readiness"]["query_serving"])

    def test_generation_ready_is_not_reported_live_when_query_fails(self) -> None:
        coherence = {"coherent": True, "searchable": True, "state": "current"}
        blocked = {
            "state": "owner-blocked", "ready": True,
            "query_serving": False, "reason": "foreign writer owns startup",
        }
        with (
            mock.patch.object(doctor, "_dep_present", return_value=True),
            mock.patch("importlib.import_module"),
            mock.patch.object(embedder, "ensure_model"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(semantic, "read_embed_state", return_value={}),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(
                semworker, "diagnostic_query_status", return_value=blocked),
        ):
            result = doctor._semantic_probe()
        self.assertTrue(result["generation_ready"])
        self.assertFalse(result["live"])
        self.assertFalse(result["query_readiness"]["query_serving"])

    def test_query_success_does_not_hide_generation_probe_failure(self) -> None:
        coherence = {"coherent": True, "searchable": True, "state": "current"}
        serving = {
            "state": "query-serving", "ready": True,
            "query_serving": True, "roundtrip_ms": 12.5,
        }
        with (
            mock.patch.object(doctor, "_dep_present", return_value=True),
            mock.patch("importlib.import_module"),
            mock.patch.object(embedder, "ensure_model"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(semantic, "read_embed_state", return_value={}),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(
                semantic, "query_readiness",
                side_effect=RuntimeError("fixture readiness failure")),
            mock.patch.object(
                semworker, "diagnostic_query_status", return_value=serving),
        ):
            result = doctor._semantic_probe()
        readiness = result["query_readiness"]
        self.assertTrue(result["generation_ready"])
        self.assertTrue(result["live"])
        self.assertTrue(readiness["current_generation"])
        self.assertTrue(readiness["query_serving"])
        self.assertEqual(
            readiness["generation_probe_state"], "diagnostic-failed")
        self.assertIn("fixture readiness failure",
                      readiness["generation_probe_error"])
        self.assertIn("generation-readiness probe failed",
                      doctor._semantic_readiness_detail(readiness))

    def test_runtime_identity_names_build_kind_location_and_id(self) -> None:
        distribution = "d" * 20
        with mock.patch.object(
                doctor.common, "distribution_build_id",
                return_value=distribution), mock.patch.object(
                    doctor.dist, "_is_dev_checkout", return_value=False):
            installed = doctor._runtime_build_detail()
        with mock.patch.object(
                doctor.common, "distribution_build_id",
                return_value=distribution), mock.patch.object(
                    doctor.dist, "_is_dev_checkout", return_value=True):
            source = doctor._runtime_build_detail()
        self.assertIn("installed package at", installed)
        self.assertIn("source checkout at", source)
        self.assertIn(distribution, installed)
        self.assertIn(indexd_runtime.INDEXD_BUILD_ID, installed)
        self.assertIn(doctor.common.package_version(), source)

    def test_runtime_identity_names_native_bytes_without_calling_them_writer(self) -> None:
        native = "b" * 20
        with mock.patch.object(
                doctor.dist, "native_binary_build_id", return_value=native):
            detail = doctor._runtime_build_detail()
            identity = doctor._native_binary_identity()
        self.assertIn(f"native {native}", detail)
        self.assertNotIn("writer", detail)
        self.assertEqual(identity["native_binary_build_id"], native)
        self.assertEqual(identity["native_binary_build_state"], "verified")

    def test_runtime_identity_reports_native_failure_as_data(self) -> None:
        with mock.patch.object(
                doctor.dist, "native_binary_build_id",
                side_effect=FileNotFoundError("native missing")):
            identity = doctor._native_binary_identity()
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertEqual(identity["native_binary_build_state"], "unavailable")
        self.assertIn("native missing", identity["native_binary_build_detail"])

    def test_probe_json_exposes_native_content_without_writer_ownership(self) -> None:
        native = "b" * 20
        with mock.patch.object(
                doctor.dist, "native_binary_build_id", return_value=native):
            payload = doctor.probe(routine_deadline=time.monotonic() + 1.0)
        identity = payload["runtime_identity"]
        self.assertEqual(identity["native_binary_build_id"], native)
        self.assertEqual(identity["native_binary_build_state"], "verified")
        self.assertNotIn("writer_build_id", identity)

    def test_expired_probe_defers_native_identity_without_hashing(self) -> None:
        with mock.patch.object(
                doctor.dist, "native_binary_build_id",
                side_effect=AssertionError("expired probe hashed native")) as native:
            payload = doctor.probe(routine_deadline=0.0)
        identity = payload["runtime_identity"]
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertEqual(
            identity["native_binary_build_state"], "status-deferred")
        self.assertIn(
            "native binary identity", payload["diagnostics"]["deferred"])
        native.assert_not_called()

    def test_routine_native_identity_converts_timeout_to_deferred_state(self) -> None:
        with mock.patch.object(
                doctor.dist, "bounded_native_binary_build_id",
                side_effect=TimeoutError("native deadline")):
            identity = doctor._native_binary_identity(
                deadline=time.monotonic() + 1.0)
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertEqual(
            identity["native_binary_build_state"], "status-deferred")

    @unittest.skipIf(doctor.dist.WIN, "fork-only worker timing fixture")
    def test_routine_native_identity_stops_slow_hashing(self) -> None:
        def slow_native(*_args, **_kwargs):
            time.sleep(2.0)
            return "b" * 20

        started = time.monotonic()
        with mock.patch.object(
                doctor.dist, "native_binary_build_id",
                side_effect=slow_native):
            identity = doctor._native_binary_identity(
                deadline=time.monotonic() + 0.01)
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertEqual(
            identity["native_binary_build_state"], "status-deferred")

    def test_semantic_probe_verifies_hashes_and_records_full_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            embeddings = root / "embeddings.f32"
            embeddings.with_name("embeddings.meta").write_text(
                "{}", encoding="utf-8")
            coherence = {"coherent": True, "searchable": True,
                         "state": "current"}
            repair = {"state": "recorded", "persistent": True}
            damage = RuntimeError("f32 digest mismatch")
            with (
                mock.patch.object(doctor.common, "EMBEDDINGS_PATH", embeddings),
                mock.patch.object(doctor, "_dep_present", return_value=True),
                mock.patch("importlib.import_module"),
                mock.patch.object(embedder, "ensure_model"),
                mock.patch.object(
                    semantic, "embedding_coherence",
                    return_value=coherence),
                mock.patch.object(
                    semantic, "verify_embedding_integrity",
                    side_effect=damage) as verify,
                mock.patch.object(
                    semantic, "request_full_rebuild",
                    return_value=repair) as request,
                mock.patch.object(semantic, "read_embed_state", return_value={}),
                mock.patch.object(semantic, "embed_running", return_value=False),
                mock.patch.object(
                    semworker, "resident_status",
                    return_value={"running": False}),
            ):
                result = doctor._semantic_probe()
        verify.assert_called_once_with()
        request.assert_called_once_with(
            "RuntimeError: f32 digest mismatch", launch=False)
        self.assertFalse(result["live"])
        self.assertEqual(result["embeddings"], "corrupt-embeddings")
        self.assertEqual(result["embedding_integrity"]["state"], "corrupt")
        self.assertEqual(result["embedding_integrity"]["repair"], repair)

    def test_human_report_does_not_mark_optional_deps_as_core_failures(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (mock.patch.object(doctor, "INGEST_BIN", root / "missing-binary"),
                  mock.patch.object(doctor, "_semantic_probe", return_value=_missing_semantic()),
                  mock.patch.object(doctor, "_store_counts", return_value=[]),
                      mock.patch.object(doctor.shutil, "which", return_value=None),
                  mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.object(doctor.common, "data_dir_source", return_value="test"),
                  mock.patch.object(doctor.common, "data_dir_warnings", return_value=[]),
                  mock.patch.object(doctor.common, "data_dir_usage",
                                    return_value={"files": 0, "bytes": 0}),
                  mock.patch.object(doctor.indexd_runtime, "indexd_resource_status",
                                    return_value={"running": False}),
                  mock.patch.object(
                      doctor.indexd_runtime, "kick_background_repair"),
                  mock.patch.object(doctor.common, "detected_stores", return_value=[]),
                  contextlib.redirect_stdout(output)):
                report = doctor.report()
        rendered = output.getvalue()
        self.assertIn("semantic search - optional", rendered)
        self.assertIn("numpy", rendered)
        self.assertIn("not installed (optional)", rendered)
        self.assertNotIn("[MISS] numpy", rendered)
        self.assertIn("pip install", rendered)
        self.assertIn("agrep[semantic]==0.2.0", rendered)
        self.assertIn(doctor.SEMANTIC_UNLOCK, report["fixes"])

    def test_human_report_describes_off_as_disabled_not_self_healing(
            self) -> None:
        output = io.StringIO()
        smart = {
            **_missing_semantic(),
            "available": True,
            "deps": {
                "numpy": True,
                "onnxruntime": True,
                "tokenizers": True,
            },
            "embeddings": "missing-embeddings",
            "embed_running": False,
            "embed_job": "failed",
            "embed_fail_reason": "stale failure from before embeddings=off",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "agrep-rs"
            binary.write_text("", encoding="utf-8")
            with (
                mock.patch.object(doctor, "INGEST_BIN", binary),
                mock.patch.object(
                    doctor, "_semantic_probe", return_value=smart),
                mock.patch.object(doctor, "_store_counts", return_value=[]),
                  mock.patch.object(
                    doctor, "_corpus_db_readiness",
                    return_value={"state": "missing", "detail": "missing"}),
                mock.patch.object(
                    doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor.settings, "setting_observation",
                    return_value={
                        "state": "verified", "value": "off",
                        "source": "settings.json"}),
                mock.patch.object(
                    doctor.common, "index_summary", return_value=None),
                mock.patch.object(
                    doctor.common, "data_dir_source", return_value="test"),
                mock.patch.object(
                    doctor.common, "data_dir_warnings", return_value=[]),
                mock.patch.object(
                    doctor.common, "data_dir_usage",
                    return_value={"files": 0, "bytes": 0}),
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}),
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_failing",
                    return_value=(0, "")),
                mock.patch.object(
                    doctor.indexd_runtime, "kick_background_repair"),
                mock.patch.object(
                    doctor.common, "detected_stores", return_value=[]),
                contextlib.redirect_stdout(output),
            ):
                report = doctor.report()
        rendered = output.getvalue()
        self.assertIn("downloads disabled by embeddings=off", rendered)
        self.assertIn("refresh disabled by embeddings=off", rendered)
        self.assertIn("an explicit semantic search (-s)", rendered)
        self.assertNotIn("downloads (~50MB)", rendered)
        self.assertNotIn("builds on the first search or status", rendered)
        self.assertNotIn("automatic zero-hit fallback", rendered)
        self.assertNotIn("last build failed", rendered)
        self.assertNotIn("stale failure from before embeddings=off", rendered)
        self.assertNotIn(doctor.SEMANTIC_UNLOCK, report["fixes"])

    def test_status_human_and_json_describe_optional_capability(self) -> None:
        status = {
            "data_dir": "/tmp/agrep-test",
            "data_dir_source": "test",
            "warnings": [],
            "index_built": True,
            "sessions": 1,
            "messages": 2,
            "agents": ["codex"],
            "search_index_ready": True,
            "search_index_state": "ready",
            "freshness": {"state": "no-known-failure"},
            "daemon": {"running": False},
            "semantic_deps": False,
            "detected_not_indexed": [],
        }
        with (mock.patch.object(cli, "_status_core", return_value=dict(status)),
              mock.patch.object(cli, "_status_semantic",
                                return_value={"semantic_deps": False})):
            rendered = "\n".join(cli._status_lines("agrep"))
        self.assertIn("optional dependencies are not installed", rendered)
        self.assertIn("pip install", rendered)
        self.assertIn("agrep[semantic]==0.2.0", rendered)
        self.assertNotIn("reinstall agrep", rendered)
        # a working keyword search is the healthy default and earns no line
        self.assertNotIn("keyword search works", rendered)

        with tempfile.TemporaryDirectory() as td:
            with (mock.patch.object(cli.common, "DATA_DIR", Path(td)),
                  mock.patch.object(
                      cli.indexd_runtime, "kick_background_repair"),
                  mock.patch("importlib.util.find_spec", return_value=None)):
                machine = cli._status_data()
        self.assertTrue(machine["semantic_optional"])
        self.assertEqual(machine["semantic_status"],
                         "not-inspected")
        self.assertIsNone(machine["semantic_deps"])
        self.assertIsNone(machine["semantic_install_hint"])

        with tempfile.TemporaryDirectory() as td:
            with (mock.patch.object(cli.common, "DATA_DIR", Path(td)),
                  mock.patch.object(
                      cli.indexd_runtime, "kick_background_repair"),
                  mock.patch(
                      "importlib.util.find_spec",
                      side_effect=AssertionError(
                          "routine status performed dependency discovery")) as finder):
                detected = cli._status_data()
        self.assertEqual(detected["semantic_status"], "not-inspected")
        self.assertIsNone(detected["semantic_deps"])
        finder.assert_not_called()

    def test_never_built_lane_row_is_one_sentence_not_two_spliced(self) -> None:
        snapshot = {"resources": {
            "semantic_model_cache": {"complete": True, "files": 0}}}
        row = doctor._routine_lane_row(
            snapshot, {"runtime_state": "not-inspected"}, embeddings_off=False)
        self.assertIsNotNone(row)
        name, detail = row
        self.assertEqual(name, "embeddings")
        self.assertTrue(detail.endswith("permits the one-time ~50MB fetch"))
        # the remedy's own sentence used to be spliced in mid-clause
        self.assertNotIn("teaches your agents", detail)
        self.assertIn(doctor._cli_command("setup"), detail)

    def _coverage_row(self, coverage: object, *, bound: bool = True,
                      running: bool = False,
                      state: str = "not-inspected") -> tuple[str, str] | None:
        output = {"coverage": coverage, "source": {"generation": "current"}}
        with (mock.patch.object(semantic, "output_generation",
                               return_value=output),
              mock.patch.object(
                  semantic, "source_generation",
                  return_value=output["source"] if bound else {"generation": "old"})):
            return doctor._routine_coverage_row(
                {"runtime_state": state, "embed_running": running})

    def test_routine_tier_names_the_built_lane_without_probing_it(self) -> None:
        complete = self._coverage_row({"indexed": 20, "total": 20})
        self.assertEqual(complete, (doctor.OK, "20/20 rows embedded · current index"))
        partial = self._coverage_row({"indexed": 12, "total": 20}, running=True)
        self.assertEqual(
            partial,
            (doctor.OPT, "12/20 rows embedded · current index; "
                         "background passes close the gap · a build is running"))
        older = self._coverage_row({"indexed": 20, "total": 20}, bound=False)
        self.assertEqual(
            older,
            (doctor.OPT, "20/20 rows embedded · published from an older index "
                         "generation; a background pass refreshes them"))
        # the deep tier owns the richer rows, and an unpublished lane says nothing
        self.assertIsNone(self._coverage_row({"indexed": 20, "total": 20},
                                             state="ready"))
        self.assertIsNone(self._coverage_row(None))

    def test_routine_coverage_row_stays_silent_when_artifacts_are_unreadable(
            self) -> None:
        with mock.patch.object(semantic, "output_generation",
                               side_effect=OSError("no such file")):
            self.assertIsNone(doctor._routine_coverage_row(
                {"runtime_state": "not-inspected"}))

    def test_model_cache_footprint_is_separate_from_overridden_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "isolated-data"
            default = root / "shared-data"
            models = default / "models" / "fixture"
            data.mkdir()
            models.mkdir(parents=True)
            (data / "corpus.db").write_bytes(b"index")
            (models / "model.onnx").write_bytes(b"model-bytes")
            with (mock.patch.object(doctor.common, "DATA_DIR", data),
                  mock.patch.object(
                      doctor.common, "DEFAULT_DATA_DIR", default),
                  mock.patch.dict(os.environ, {}, clear=False)):
                os.environ.pop("AGREP_MODEL_DIR", None)
                cache = doctor._model_cache_footprint()
                footprint = doctor._data_footprint()
        self.assertTrue(cache["complete"])
        self.assertEqual(cache["files"], 1)
        self.assertEqual(cache["bytes"], len(b"model-bytes"))
        self.assertEqual(cache["path"], str(default / "models"))
        self.assertEqual(footprint["bytes"], len(b"index"))

    def test_expired_model_cache_census_does_not_walk(self) -> None:
        with (mock.patch.object(doctor.time, "monotonic", return_value=1.0),
              mock.patch.object(
                  doctor.os, "walk",
                  side_effect=AssertionError("expired census began a walk"))):
            cache = doctor._model_cache_footprint(deadline=0.5)
        self.assertEqual(cache["state"], "budget-exceeded")
        self.assertFalse(cache["complete"])
        self.assertIsNone(cache["bytes"])

    def test_default_data_footprint_does_not_double_count_model_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models" / "fixture"
            models.mkdir(parents=True)
            (root / "corpus.db").write_bytes(b"index")
            (models / "model.onnx").write_bytes(b"model-bytes")
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.object(doctor.common, "DEFAULT_DATA_DIR", root),
                  mock.patch.dict(os.environ, {}, clear=False)):
                os.environ.pop("AGREP_MODEL_DIR", None)
                footprint = doctor._data_footprint()
                cache = doctor._model_cache_footprint()
        self.assertEqual(footprint["bytes"], len(b"index"))
        self.assertEqual(cache["bytes"], len(b"model-bytes"))

    def test_overridden_model_cache_inside_data_is_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "nested" / "models"
            profile = models / "fixture"
            profile.mkdir(parents=True)
            (root / "corpus.db").write_bytes(b"index")
            (profile / "model.onnx").write_bytes(b"model-bytes")
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.dict(
                      os.environ, {"AGREP_MODEL_DIR": str(models)})):
                footprint = doctor._data_footprint()
                cache = doctor._model_cache_footprint()
        self.assertEqual(footprint["bytes"], len(b"index"))
        self.assertEqual(cache["bytes"], len(b"model-bytes"))

    def test_data_directory_cannot_double_as_model_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = root / "fixture"
            profile.mkdir()
            (root / "corpus.db").write_bytes(b"index")
            (profile / "model.onnx").write_bytes(b"model-bytes")
            with (mock.patch.object(doctor.common, "DATA_DIR", root),
                  mock.patch.dict(
                      os.environ, {"AGREP_MODEL_DIR": str(root)})):
                footprint = doctor._data_footprint()
                cache = doctor._model_cache_footprint()
        self.assertTrue(footprint["complete"])
        self.assertFalse(cache["complete"])
        self.assertEqual(cache["state"], "unavailable")
        self.assertIsNone(cache["bytes"])
        self.assertIn("cannot be separated safely", cache["detail"])

    def test_model_cache_parent_of_data_fails_instead_of_counting_corpus(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "shared"
            data = models / "corpus"
            profile = models / "fixture"
            data.mkdir(parents=True)
            profile.mkdir()
            (data / "corpus.db").write_bytes(b"index")
            (profile / "model.onnx").write_bytes(b"model-bytes")
            with (mock.patch.object(doctor.common, "DATA_DIR", data),
                  mock.patch.dict(
                      os.environ, {"AGREP_MODEL_DIR": str(models)})):
                footprint = doctor._data_footprint()
                cache = doctor._model_cache_footprint()
        self.assertEqual(footprint["bytes"], len(b"index"))
        self.assertFalse(cache["complete"])
        self.assertEqual(cache["state"], "unavailable")
        self.assertIsNone(cache["bytes"])
        self.assertIn("contains the data directory", cache["detail"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_model_cache_footprint_refuses_a_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            (target / "model.onnx").write_bytes(b"secret")
            link = root / "models"
            link.symlink_to(target, target_is_directory=True)
            with mock.patch.dict(
                    os.environ, {"AGREP_MODEL_DIR": str(link)}):
                cache = doctor._model_cache_footprint()
        self.assertFalse(cache["complete"])
        self.assertEqual(cache["state"], "unavailable")
        self.assertIsNone(cache["bytes"])

    # goal10 audit B10: a directory (or any unprovable corpus) at the
    # messages path printed "already built - 0 messages" and exited 0
    def test_setup_uses_a_verified_current_daemon_during_index_repair(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("published\n", encoding="utf-8")
            with (mock.patch.object(doctor, "main", return_value=0),
                  mock.patch.object(cli.common, "setting", return_value="off"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "generation-unavailable"}),
                  mock.patch.object(
                      corpusdb, "_derived_publication_health",
                      return_value={"state": "ready"}),
                  mock.patch.object(
                      cli.common, "index_summary",
                      return_value={"messages": 14, "sessions": 3}),
                  mock.patch.object(
                      cli.indexd_runtime, "indexd_resource_status",
                      return_value={"running": True}) as daemon,
                  mock.patch.object(teach, "detected_agents", return_value=[]),
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(
                      cli, "cmd_index",
                      side_effect=AssertionError("setup raced the daemon")) as build,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=[], yes=True, no_teach=True, no_semantic=True,
                    archive=False, no_archive=True))
        self.assertEqual(rc, 0)
        build.assert_not_called()
        daemon.assert_called_once_with(observe_only=True, include_rss=False)
        rendered = output.getvalue()
        self.assertIn("14 messages across 3 sessions remain keyword-searchable", rendered)
        self.assertIn("background index upgrade is finishing", rendered)
        self.assertNotIn("setup incomplete", rendered)

    def test_setup_rebuilds_when_no_current_daemon_owns_the_repair(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("published\n", encoding="utf-8")
            with (mock.patch.object(doctor, "main", return_value=0),
                  mock.patch.object(cli.common, "setting", return_value="off"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "generation-unavailable"}),
                  mock.patch.object(
                      corpusdb, "_derived_publication_health",
                      return_value={"state": "ready"}),
                  mock.patch.object(
                      cli.common, "index_summary",
                      return_value={"messages": 14, "sessions": 3}),
                  mock.patch.object(
                      cli.indexd_runtime, "indexd_resource_status",
                      return_value={"running": False}),
                  mock.patch.object(teach, "detected_agents", return_value=[]),
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(cli, "cmd_index", return_value=0) as build,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=[], yes=True, no_teach=True, no_semantic=True,
                    archive=False, no_archive=True))
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertTrue(build.call_args.args[0].full)
        self.assertNotIn("background index upgrade is finishing", output.getvalue())

    def test_cli_setup_rebuilds_instead_of_vouching_for_a_damaged_index(
            self) -> None:
        for build_ok, expected_rc, expected_line in (
                (False, 1, "setup incomplete"),
                (True, 0, "setup complete")):
            output = io.StringIO()
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                messages = root / "messages.jsonl"
                messages.mkdir()  # damage: a directory at the corpus path
                (messages / "keep.txt").write_text("keep\n", encoding="utf-8")
                (root / "teach.json").write_text("{}", encoding="utf-8")
                with (mock.patch.object(doctor, "main", return_value=0),
                      mock.patch.object(cli.common, "setting",
                                        return_value="off"),
                      mock.patch.object(cli.common, "DATA_DIR", root),
                      mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                      mock.patch.object(
                          cli.common, "index_summary",
                          side_effect=AssertionError(
                              "no summary proof exists for a directory")),
                      mock.patch.object(teach, "teach", return_value=0),
                      mock.patch.object(cli, "_setup_archive"),
                      mock.patch.object(
                          cli, "cmd_index",
                          return_value=0 if build_ok else 1) as build,
                      contextlib.redirect_stdout(output)):
                    rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                        rest=["--yes", "--no-archive"], yes=False,
                        no_teach=False, archive=False, no_archive=False))
                    preserved = list(root.glob(".messages.jsonl.invalid-*"))
                    preserved_directory = (
                        len(preserved) == 1 and preserved[0].is_dir())
                    preserved_content = (
                        (preserved[0] / "keep.txt").read_text(encoding="utf-8")
                        if preserved_directory else "")
            with self.subTest(build_ok=build_ok):
                self.assertEqual(rc, expected_rc)
                build.assert_called_once()
                self.assertTrue(build.call_args.args[0].full)
                self.assertEqual(len(preserved), 1)
                self.assertTrue(preserved_directory)
                self.assertEqual(preserved_content, "keep\n")
                rendered = output.getvalue()
                self.assertNotIn("already built", rendered)
                self.assertIn("preserved invalid messages.jsonl", rendered)
                self.assertIn(expected_line, rendered)

    def test_setup_rebuilds_a_regular_leaf_that_fails_generation_proof(
            self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("this is not a corpus\n", encoding="utf-8")
            (root / "teach.json").write_text("{}", encoding="utf-8")
            with (mock.patch.object(doctor, "main", return_value=0),
                  mock.patch.object(cli.common, "setting", return_value="off"),
                  mock.patch.object(cli.common, "DATA_DIR", root),
                  mock.patch.object(cli.common, "MESSAGES_PATH", messages),
                  mock.patch.object(
                      corpusdb, "search_generation_health",
                      return_value={"state": "torn-generation"}),
                  mock.patch.object(
                      cli.common, "index_summary",
                      side_effect=AssertionError(
                          "a damaged publication has no valid summary")),
                  mock.patch.object(teach, "teach", return_value=0),
                  mock.patch.object(cli, "_setup_archive"),
                  mock.patch.object(cli, "cmd_index", return_value=0) as build,
                  contextlib.redirect_stdout(output)):
                rc = cli.cmd_setup(SimpleNamespace(
                    no_hook=True,
                    rest=["--yes", "--no-archive"], yes=False,
                    no_teach=False, archive=False, no_archive=False))
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertTrue(build.call_args.args[0].full)
        self.assertNotIn("already built", output.getvalue())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_setup_preserves_a_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "outside.jsonl"
            target.write_text("outside\n", encoding="utf-8")
            link = root / "messages.jsonl"
            link.symlink_to(target)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertTrue(cli._preserve_invalid_corpus_leaf(link))
            preserved = list(root.glob(".messages.jsonl.invalid-*"))
            self.assertEqual(len(preserved), 1)
            self.assertTrue(preserved[0].is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "outside\n")


if __name__ == "__main__":
    unittest.main()
