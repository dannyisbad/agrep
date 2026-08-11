"""Goal 10 doctor evidence tiers, budgets, and shared-observation contracts."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import corpusdb  # noqa: E402
import cli  # noqa: E402
import common  # noqa: E402
import doctor  # noqa: E402
import indexd_runtime  # noqa: E402
import semantic  # noqa: E402
import settings  # noqa: E402
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
        "model_integrity": {"state": "not-checked"},
        "embeddings": "unknown",
        "embedding_integrity": {"state": "not-checked", "verified": False},
        "embedding_coverage": None,
        "embed_job": "idle",
        "embed_running": False,
        "resident_worker": {"running": False},
    }


def _stale_semantic() -> dict:
    return {
        **_missing_semantic(),
        "available": True,
        "deps": {"numpy": True, "onnxruntime": True, "tokenizers": True},
        "model_cached": True,
        "model_integrity": {
            "state": "not-verified",
            "detail": "model SHA256 not verified this run (--deep)",
        },
        "embeddings": "stale",
    }


class _RecordingDb:
    def __init__(self, meta: dict[str, str]) -> None:
        self.meta = meta
        self.sql: list[str] = []
        self.quick_checks = 0
        self.progress = None

    def execute(self, sql: str):
        self.sql.append(sql)
        if sql == "PRAGMA quick_check":
            self.quick_checks += 1
            return [("ok",)]
        if "SELECT key, value FROM meta" in sql:
            return list(self.meta.items())
        return self

    def fetchall(self):
        return []

    def source_stable(self) -> bool:
        return True

    def set_progress_handler(self, callback, _steps: int) -> None:
        self.progress = callback

    def close(self) -> None:
        return None


class DoctorEvidenceTierTests(unittest.TestCase):
    def test_routine_semantic_probe_never_imports_native_runtime(self) -> None:
        with (
            mock.patch.object(
                doctor, "_dep_present",
                side_effect=AssertionError(
                    "routine semantic probe performed dependency discovery"),
            ) as dependency_probe,
            mock.patch(
                "importlib.import_module",
                side_effect=AssertionError(
                    "routine semantic detection imported native code"),
            ) as native_import,
        ):
            observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertIsNone(observed["available"])
        self.assertFalse(observed["runtime_verified"])
        self.assertEqual(observed["runtime_state"], "not-inspected")
        self.assertTrue(all(value is None for value in observed["deps"].values()))
        dependency_probe.assert_not_called()
        native_import.assert_not_called()

    def test_routine_semantic_probe_preserves_bounded_failed_job(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-semantic-state-") as td:
            root = Path(td)
            state = root / ".semantic-embed-state.json"
            state.write_text(json.dumps({
                "state": "failed",
                "finished_at": time.time(),
                "reason": "onnx session init failed",
            }), encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor, "_dep_present",
                    side_effect=AssertionError(
                        "routine failed-job probe discovered dependencies"),
                ) as dependency_probe,
                mock.patch.object(
                    doctor.ownerfile, "snapshot",
                    wraps=doctor.ownerfile.snapshot) as snapshot,
            ):
                observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertEqual(observed["embed_job"], "failed")
        self.assertEqual(
            observed["embed_fail_reason"], "onnx session init failed")
        snapshot.assert_called_once_with(
            state, max_bytes=doctor._SEMANTIC_STATE_MAX_BYTES)
        dependency_probe.assert_not_called()

    def test_deep_dependency_finder_failure_is_unavailable_not_missing(
            self) -> None:
        with (
            mock.patch.object(doctor, "_dep_present", return_value=None),
            mock.patch(
                "importlib.import_module",
                side_effect=AssertionError(
                    "unverified dependency discovery imported native code"),
            ) as native_import,
        ):
            observed = doctor._semantic_probe(deep=True, fix=False)
        self.assertFalse(observed["available"])
        self.assertFalse(observed["runtime_verified"])
        self.assertEqual(
            observed["runtime_state"], "dependency-discovery-unavailable")
        self.assertEqual(
            observed["unavailable_reason"],
            "optional-dependency-discovery-unavailable")
        self.assertTrue(all(value is None for value in observed["deps"].values()))
        native_import.assert_not_called()

    def test_routine_refusing_a_corpus_clone_is_not_a_verdict(self) -> None:
        """Routine reads the database's metadata but never copies a corpus.

        When the bounded open refuses, the check did not run: that must read as
        absence, never as a damaged database the reader is asked to repair.
        """
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-routine-db-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"nonempty")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor, "_open_corpus_diagnostic_snapshot",
                    side_effect=corpusdb.AliasCloneRefused("too large"),
                ) as opened,
            ):
                observed = doctor._corpus_db_readiness()
        self.assertFalse(doctor._ran(observed))
        self.assertNotIn("detail", observed)
        opened.assert_called_once_with(root / "corpus.db", routine=True)

    def test_diagnostic_snapshot_bounds_only_the_routine_alias_clone(
            self) -> None:
        marker = object()
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-clone-tier-") as td:
            path = Path(td) / "fixture.db"
            with mock.patch.object(
                    corpusdb, "_connect_read_snapshot",
                    return_value=marker) as connect:
                self.assertIs(
                    doctor._open_corpus_diagnostic_snapshot(
                        path, routine=True),
                    marker)
                self.assertIs(
                    doctor._open_corpus_diagnostic_snapshot(
                        path, routine=False),
                    marker)
        self.assertEqual(connect.call_args_list, [
            mock.call(
                path, 0,
                max_clone_bytes=corpusdb._ROUTINE_ALIAS_CLONE_MAX_BYTES),
            mock.call(path, 0),
        ])

    def test_damaged_census_owner_probe_never_uses_snapshot_alias(
            self) -> None:
        build_id = "a" * 20
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-routine-owner-") as td:
            root = Path(td)
            database = root / "corpus.db"
            created = sqlite3.connect(database)
            created.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            created.commit()
            created.close()
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(corpusdb, "DB_PATH", database),
                mock.patch.object(
                    corpusdb.indexd_runtime, "assert_python_runtime_unchanged"),
                mock.patch.object(
                    corpusdb.indexd_runtime, "derived_writer_build_id",
                    return_value=build_id),
                mock.patch.object(
                    corpusdb.indexd_runtime, "derived_owner_info",
                    return_value=corpusdb.indexd_runtime.DerivedOwnerInfo(
                        "current", build_id, None, "")),
                mock.patch.object(
                    corpusdb.indexd_runtime, "ingest_cache_owner_info",
                    return_value=corpusdb.indexd_runtime.IngestCacheOwnerInfo(
                        "current", build_id, "")),
                mock.patch.object(
                    corpusdb, "_connect_read_alias",
                    side_effect=AssertionError("routine owner probe cloned SQLite"),
                ) as alias,
            ):
                observed = doctor._corpus_db_readiness()
        self.assertEqual(observed["state"], "post-adoption-clobber")
        alias.assert_not_called()

    def setUp(self) -> None:
        doctor._QUICK_CHECK_MEMORY.clear()
        doctor._MODEL_CHECK_MEMORY.clear()

    def test_routine_readiness_never_runs_quick_check_and_discloses_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-routine-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"fixture")
            source_stamp = json.dumps([None] * len(corpusdb._SOURCES))
            db = _RecordingDb({
                "schema": corpusdb._SCHEMA,
                "stamp": source_stamp,
                "family_stamp": "family",
            })
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    corpusdb, "search_generation_health", return_value={"state": "ready"}),
                mock.patch.object(corpusdb, "_stamp", return_value=source_stamp),
                mock.patch.object(
                    doctor.common, "session_family_source_stamp", return_value="family"),
                mock.patch.object(
                    doctor, "_open_corpus_diagnostic_snapshot",
                    return_value=db) as opened,
            ):
                result = doctor._corpus_db_readiness()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(db.quick_checks, 0)
        self.assertFalse(result["integrity"]["checked"])
        # a scan that did not happen owes the reader no sentence about itself
        self.assertFalse(doctor._ran(result["integrity"]))
        self.assertNotIn("detail", result["integrity"])
        opened.assert_called_once_with(
            root / "corpus.db", routine=True)

    def test_deep_checks_metadata_before_quick_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-verdict-first-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"fixture")
            db = _RecordingDb({"schema": "obsolete"})
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    corpusdb, "search_generation_health", return_value={"state": "ready"}),
                mock.patch.object(corpusdb, "_stamp", return_value="source"),
                mock.patch.object(
                    doctor, "_open_corpus_diagnostic_snapshot",
                    return_value=db) as opened,
            ):
                result = doctor._corpus_db_readiness(deep=True)
        self.assertEqual(result["state"], "stale")
        self.assertIn("schema obsolete", result["detail"])
        self.assertEqual(db.quick_checks, 0)
        self.assertTrue(any("SELECT key, value FROM meta" in sql for sql in db.sql))
        opened.assert_called_once_with(
            root / "corpus.db", routine=False)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits do not model Windows ACLs")
    def test_group_world_writable_evidence_is_ignored_and_not_replaced(self) -> None:
        identity = {"db": [1, 2, 3, 4, 5], "wal": None}
        poison = {
            "version": doctor._DOCTOR_EVIDENCE_VERSION,
            "quick_check": {
                "identity": identity, "rows": ["forged"],
                "elapsed_s": 0.1, "checked_at": 1.0, "bytes": 3,
            },
        }
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-mode-poison-") as td:
            root = Path(td)
            evidence = root / doctor._DOCTOR_EVIDENCE_NAME
            evidence.write_text(json.dumps(poison), encoding="utf-8")
            evidence.chmod(0o666)
            if not evidence.stat().st_mode & 0o022:
                self.skipTest("filesystem did not retain writable mode bits")
            before = evidence.read_bytes()
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                self.assertEqual(
                    doctor._read_doctor_evidence(),
                    {"version": doctor._DOCTOR_EVIDENCE_VERSION},
                )
                self.assertIsNone(doctor._cached_quick_check(identity))
                self.assertFalse(doctor._save_doctor_evidence(
                    "quick_check", poison["quick_check"]))
            self.assertEqual(evidence.read_bytes(), before)

    def test_windows_mode_bits_do_not_reject_regular_evidence(self) -> None:
        payload = {
            "version": doctor._DOCTOR_EVIDENCE_VERSION,
            "fixture": {"verified": True},
        }
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-windows-mode-") as td:
            root = Path(td)
            evidence = root / doctor._DOCTOR_EVIDENCE_NAME
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            evidence.chmod(0o666)
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(doctor.os, "name", "nt"),
            ):
                self.assertEqual(doctor._read_doctor_evidence(), payload)

    def test_foreign_uid_evidence_cannot_poison_or_be_replaced(self) -> None:
        identity = {
            "profile": "fixture", "files": {"model.bin": [1, 2, 3, 4, 5]},
        }
        poison = {
            "version": doctor._DOCTOR_EVIDENCE_VERSION,
            "model_sha256": {
                "identity": identity, "verified": True, "checked_at": 1.0,
            },
        }
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-uid-poison-") as td:
            root = Path(td)
            evidence = root / doctor._DOCTOR_EVIDENCE_NAME
            evidence.write_text(json.dumps(poison), encoding="utf-8")
            evidence.chmod(0o600)
            before = evidence.read_bytes()
            owner = int(getattr(evidence.stat(), "st_uid", 0))
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor.os, "geteuid", return_value=owner + 1,
                    create=True),
            ):
                self.assertEqual(
                    doctor._read_doctor_evidence(),
                    {"version": doctor._DOCTOR_EVIDENCE_VERSION},
                )
                self.assertIsNone(doctor._cached_model_check(identity))
                self.assertFalse(doctor._save_doctor_evidence(
                    "model_sha256", poison["model_sha256"]))
            self.assertEqual(evidence.read_bytes(), before)

    def test_evidence_writer_caps_payload_and_publishes_private_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-evidence-cap-") as td:
            root = Path(td)
            evidence = root / doctor._DOCTOR_EVIDENCE_NAME
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                self.assertTrue(doctor._save_doctor_evidence(
                    "fixture", {"verified": True}))
                self.assertFalse(evidence.stat().st_mode & 0o022)
                self.assertEqual(
                    doctor._read_doctor_evidence()["fixture"],
                    {"verified": True},
                )
                before = evidence.read_bytes()
                with mock.patch.object(
                        doctor, "_DOCTOR_EVIDENCE_MAX_BYTES", 32):
                    self.assertFalse(doctor._save_doctor_evidence(
                        "oversized", {"body": "x" * 128}))
            self.assertEqual(evidence.read_bytes(), before)

    def test_quick_check_cache_includes_wal_change_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-cache-") as td:
            root = Path(td)
            database = root / "corpus.db"
            wal = root / "corpus.db-wal"
            database.write_bytes(b"database")
            wal.write_bytes(b"wal1")
            db = _RecordingDb({})
            progress: list[dict] = []
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.dict(os.environ, {"AGREP_DATA_READONLY": str(root)}),
                mock.patch.object(
                    doctor, "_read_doctor_evidence",
                    return_value={"version": doctor._DOCTOR_EVIDENCE_VERSION}),
            ):
                first = doctor._run_quick_check(db, database, progress.append)
                second = doctor._run_quick_check(db, database, progress.append)
                before = wal.stat()
                wal.write_bytes(b"wal2")
                os.utime(wal, ns=(before.st_atime_ns, before.st_mtime_ns))
                third = doctor._run_quick_check(db, database, progress.append)
                self.assertFalse((root / doctor._DOCTOR_EVIDENCE_NAME).exists())
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(third["cached"])
        self.assertEqual(db.quick_checks, 2)
        self.assertEqual(progress[0]["phase"], "start")
        self.assertGreater(progress[0]["estimate_s"], 0)
        self.assertIn("cached", [event["phase"] for event in progress])

    def test_identity_changes_after_same_size_rewrite_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-change-time-") as td:
            path = Path(td) / "corpus.db"
            path.write_bytes(b"before")
            before_stat = path.stat()
            before = doctor._regular_file_identity(path)
            path.write_bytes(b"after!")
            os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            after = doctor._regular_file_identity(path)
        self.assertNotEqual(before, after)
        self.assertEqual(before[2], after[2])
        self.assertEqual(before[3], after[3])
        self.assertNotEqual(before[4], after[4])

    def test_quick_check_rejects_publication_moved_since_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-open-race-") as td:
            root = Path(td)
            database = root / "corpus.db"
            database.write_bytes(b"old publication")
            expected = doctor._quick_check_identity(database)
            replacement = root / "replacement.db"
            replacement.write_bytes(b"new publication")
            replacement.replace(database)
            db = _RecordingDb({})
            result = doctor._run_quick_check(
                db, database, expected_identity=expected)
        self.assertEqual(result["state"], "moved")
        self.assertFalse(result["checked"])
        self.assertEqual(db.quick_checks, 0)
        self.assertIn("changed before the scan", result["detail"])

    def test_model_sha_verdict_is_cached_by_change_sensitive_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-model-cache-") as td:
            root = Path(td)
            model = root / "model.bin"
            model.write_bytes(b"model1")
            calls = []
            fake = SimpleNamespace(
                PROFILE={"id": "fixture", "files": {"model.bin": (6, "sha")}},
                model_dir=lambda: root,
                ensure_model=lambda download=False: calls.append(download) or root,
            )
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.dict(os.environ, {"AGREP_DATA_READONLY": str(root)}),
                mock.patch.object(
                    doctor, "_read_doctor_evidence",
                    return_value={"version": doctor._DOCTOR_EVIDENCE_VERSION}),
            ):
                routine, routine_integrity = doctor._model_cache_probe(fake, deep=False)
                first, first_integrity = doctor._model_cache_probe(fake, deep=True)
                second, second_integrity = doctor._model_cache_probe(fake, deep=True)
                before = model.stat()
                model.write_bytes(b"model2")
                os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns))
                third, third_integrity = doctor._model_cache_probe(fake, deep=True)
        self.assertTrue(routine)
        self.assertFalse(routine_integrity["checked"])
        self.assertTrue(first and second and third)
        self.assertFalse(first_integrity["cached"])
        self.assertTrue(second_integrity["cached"])
        self.assertFalse(third_integrity["cached"])
        self.assertEqual(calls, [False, False])

    def test_post_adoption_clobber_names_the_full_rebuild_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-clobber-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"fixture")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    corpusdb, "search_generation_health", return_value={"state": "ready"}),
                mock.patch.object(corpusdb, "_stamp", return_value="source"),
                mock.patch.object(
                    doctor, "_open_corpus_diagnostic_snapshot",
                    side_effect=doctor._PostAdoptionClobber(
                        "legacy owner returned; automatic repair is disabled; "
                        "run agrep doctor for the safe remedy")),
            ):
                result = doctor._corpus_db_readiness()
        self.assertEqual(result["state"], "post-adoption-clobber")
        command = doctor._cli_command("index", "--full")
        verify = doctor._cli_command("doctor", "--deep")
        self.assertIn(f"`{command}`", result["remedy"])
        self.assertEqual(result["remedy"].count(f"`{command}`"), 1)
        self.assertIn(f"`{verify}` reports ready", result["remedy"])
        self.assertNotIn("next search", result["detail"])

    def test_real_locked_canonical_database_is_busy_not_corrupt(self) -> None:
        build_id = "a" * 20
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-real-busy-") as td:
            root = Path(td)
            database = root / "corpus.db"
            created = sqlite3.connect(database)
            created.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            created.execute(
                "INSERT INTO meta VALUES ('build_id', ?)", (build_id,))
            created.commit()
            created.close()

            locker = sqlite3.connect(database, timeout=0)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                with (
                    mock.patch.object(doctor.common, "DATA_DIR", root),
                    mock.patch.object(corpusdb, "DB_PATH", database),
                    mock.patch.object(
                        corpusdb, "search_generation_health",
                        return_value={"state": "ready"}),
                    mock.patch.object(
                        corpusdb.indexd_runtime,
                        "assert_python_runtime_unchanged"),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "derived_writer_build_id",
                        return_value=build_id),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "derived_owner_info",
                        return_value=corpusdb.indexd_runtime.DerivedOwnerInfo(
                            "current", build_id, None, "")),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "ingest_cache_owner_info",
                        return_value=corpusdb.indexd_runtime.IngestCacheOwnerInfo(
                            "current", build_id, "")),
                ):
                    result = doctor._corpus_db_readiness()
            finally:
                locker.rollback()
                locker.close()
        self.assertEqual(result["state"], "busy")
        self.assertNotIn(result["state"], {"corrupt", "unavailable"})


class DoctorObservationSharingTests(unittest.TestCase):
    def test_index_summary_carries_no_private_drift_observation(self) -> None:
        # the mtime-vs-timestamp drift clock was deleted (law 4: a check that
        # cannot run truthfully is not a row); its private feed goes with it
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-summary-") as td:
            root = Path(td)
            sessions = root / "sessions.jsonl"
            sessions.write_text("\n".join((
                json.dumps({
                    "session": "one", "agent": "codex", "n": 1,
                    "last_ts": 100,
                }),
                json.dumps({
                    "session": "two", "agent": "codex", "n": 1,
                    "last_ts": 250,
                }),
                json.dumps({
                    "session": "three", "agent": "claude", "n": 1,
                    "last_ts": "malformed",
                }),
            )) + "\n", encoding="utf-8")
            census = SimpleNamespace(
                sessions=frozenset({"one", "two", "three"}),
                proof=SimpleNamespace(ingest_signature="3:fixture"),
            )
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor.common, "MESSAGES_PATH", root / "messages.jsonl"),
                mock.patch.object(
                    doctor.common, "read_session_family_census",
                    return_value=census),
            ):
                summary = doctor.common.index_summary()
        self.assertIsNotNone(summary)
        self.assertNotIn("_parsed_newest_ms", summary)
        self.assertEqual(
            set(summary["per_agent"][0]),
            {"agent", "messages", "sessions"},
        )

    def test_footprint_prunes_symlink_or_reparse_directories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-footprint-") as td:
            base = Path(td)
            root = base / "data"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            local = root / "local.bin"
            escaped = outside / "escaped.bin"
            local.write_bytes(b"local")
            escaped.write_bytes(b"outside bytes must not count")
            try:
                (root / "linked-outside").symlink_to(
                    outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks unavailable: {error}")
            with mock.patch.object(doctor.common, "DATA_DIR", root):
                footprint = doctor._data_footprint()
        self.assertEqual(footprint["files"], 1)
        self.assertEqual(footprint["bytes"], len(b"local"))

    def test_probe_reuses_one_walk_store_probe_and_session_observation(self) -> None:
        stores = [{"name": "codex", "files": 2}]
        current_drift = indexd_runtime.DriftReport("current")
        summary = {
            "state": "ready", "sessions": 1, "messages": 2, "age_s": 0,
            "per_agent": [{"agent": "codex", "sessions": 1, "messages": 2}],
        }
        search_db = {
            "state": "ready", "detail": "current",
            "integrity": doctor._integrity_not_verified(),
        }
        with (
            mock.patch.object(doctor, "_semantic_probe", return_value=_missing_semantic()) as semantic_probe,
            mock.patch.object(
                doctor, "_orphan_inventory",
                return_value={
                    "state": "complete", "complete": True,
                    "count": 0, "bytes": 0,
                },
            ) as orphan_probe,
            mock.patch.object(doctor, "_data_footprint", return_value={
                "state": "complete", "complete": True,
                "files": 3, "bytes": 4, "archive_bytes": 0,
                "breakdown": "",
            }) as footprint,
            mock.patch.object(
                doctor, "_index_summary_state", return_value=summary,
            ) as summary_probe,
            mock.patch.object(
                doctor.indexd_runtime, "observe_store_drift",
                return_value=(stores, current_drift),
            ) as store_probe,
            mock.patch.object(
                doctor.indexd_runtime, "_store_census",
                side_effect=AssertionError("injected drift ran a second census"),
            ) as fallback_census,
            mock.patch.object(doctor, "_store_counts", return_value=[]) as store_counts,
            mock.patch.object(doctor, "_corpus_db_readiness", return_value=search_db),
            mock.patch.object(doctor, "_model_attribution", return_value={"state": "empty"}),
            mock.patch.object(
                doctor, "_archive_probe", return_value={"state": "disabled"},
            ) as archive_probe,
            mock.patch.object(
                doctor.indexd_runtime, "machine_freshness",
                return_value={},
            ) as machine_freshness,
            mock.patch.object(doctor, "_machine_freshness_fields", return_value={}),
            mock.patch.object(
                doctor.indexd_runtime, "indexd_resource_status",
                return_value={},
            ) as daemon_probe,
            mock.patch.object(
                doctor.indexd_runtime, "indexing_failure",
                return_value=None,
            ) as failure_probe,
            mock.patch.object(
                doctor.install_lag, "installed_master_lag",
                return_value={
                    "state": "current", "detail": "within local-master bound"},
            ) as lag_probe,
            mock.patch.object(doctor, "_teach_reconcile_probe", return_value={}),
            mock.patch.object(doctor.common, "detected_stores", return_value=[]) as detect,
        ):
            payload = doctor.probe()
        footprint.assert_called_once_with(deadline=mock.ANY)
        orphan_probe.assert_called_once_with(deep=False, deadline=mock.ANY)
        summary_probe.assert_called_once_with(deadline=mock.ANY)
        archive_probe.assert_called_once_with(
            stored_bytes=0, deep=False,
            timeout_s=doctor._DIAGNOSTIC_ARCHIVE_TIMEOUT_S)
        store_probe.assert_called_once_with(
            timeout_s=doctor._DIAGNOSTIC_STORE_TIMEOUT_S)
        fallback_census.assert_not_called()
        detect.assert_called_once_with(
            timeout_s=doctor._DIAGNOSTIC_DETECT_TIMEOUT_S,
            observation=mock.ANY)
        store_counts.assert_called_once_with(("codex",), stores)
        semantic_probe.assert_called_once_with(deep=False, fix=False)
        lag_probe.assert_called_once_with(deadline=mock.ANY)
        daemon_probe.assert_called_once_with(
            observe_only=True, include_rss=False)
        failure_probe.assert_called_once_with(
            daemon_status={}, drift_report=current_drift)
        machine_freshness.assert_called_once_with(
            checked=True, failure=None, drift_report=current_drift)
        self.assertEqual(
            payload["resources"]["data"],
            {
                "state": "complete", "complete": True,
                "files": 3, "bytes": 4, "detail": None,
            },
        )
        self.assertEqual(
            payload["install_lag"],
            {"state": "current", "detail": "within local-master bound"},
        )

    def test_report_is_routine_read_only_and_reuses_observations(self) -> None:
        smart = _stale_semantic()
        current_drift = indexd_runtime.DriftReport("current")
        summary = {"state": "never-built"}
        readiness = {
            "state": "missing", "detail": "database does not exist",
            "integrity": doctor._integrity_not_verified(),
        }
        generation = {"state": "ready"}
        archive = {
            "state": "disabled", "detail": "disabled",
            "last_pass": {"age_s": None, "fresh": False},
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-report-") as td:
            root = Path(td)
            (root / "sessions.jsonl").write_text("", encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(doctor, "_data_footprint", return_value={
                    "state": "complete", "complete": True,
                    "files": 1, "bytes": 0, "archive_bytes": 0,
                    "breakdown": "",
                }) as footprint,
                mock.patch.object(
                    doctor, "_orphan_inventory",
                    return_value={
                        "state": "complete", "complete": True,
                        "count": 0, "bytes": 0,
                    },
                ) as orphan_probe,
                mock.patch.object(
                    doctor, "_index_summary_state", return_value=summary,
                ) as summary_probe,
                mock.patch.object(
                    doctor.indexd_runtime, "observe_store_drift",
                    return_value=([], current_drift),
                ) as store_probe,
                mock.patch.object(doctor.common, "detected_stores", return_value=[]) as detect,
                mock.patch.object(doctor, "_corpus_db_readiness", return_value=readiness) as db_probe,
                mock.patch.object(
                    doctor, "_archive_probe", return_value=archive,
                ) as archive_probe,
                mock.patch.object(doctor.indexd_runtime, "indexd_resource_status", return_value={"running": False}),
                mock.patch.object(doctor.indexd_runtime, "indexd_failing", return_value=(0, "")),
                mock.patch.object(doctor.indexd_runtime, "indexing_failure", return_value=None),
                mock.patch.object(
                    corpusdb, "search_generation_health",
                    return_value=generation,
                ) as generation_probe,
                mock.patch.object(doctor, "_store_counts", return_value=[]) as store_counts,
                mock.patch.object(doctor, "_semantic_probe", return_value=smart) as semantic_probe,
                mock.patch.object(
                    doctor.install_lag, "installed_master_lag",
                    return_value={
                        "state": "lagging",
                        "detail": "lags local master by 8.0 days",
                        "remedy": "replace-installed-tool",
                        "remedy_argv": [
                            "uv", "tool", "install", "--force", "--from",
                            "/fixture source", "agrep",
                        ],
                    },
                ) as lag_probe,
                # The embeddings row consults the LIVE host: below 30%
                # battery it renders "background build paused" and the pin
                # misses. Fix all three host probes to the healthy render.
                mock.patch.multiple(
                    doctor.common,
                    battery_state=lambda: (False, 100),
                    available_memory_fraction=lambda: 1.0,
                    host_cpu_fraction=lambda: 0.0),
                mock.patch.object(semantic, "ensure_fresh_async") as ensure,
                contextlib.redirect_stdout(output),
            ):
                doctor.report()
        footprint.assert_called_once_with(deadline=mock.ANY)
        orphan_probe.assert_called_once_with(deep=False, deadline=mock.ANY)
        summary_probe.assert_called_once_with(deadline=mock.ANY)
        archive_probe.assert_called_once_with(
            stored_bytes=0, deep=False,
            timeout_s=doctor._DIAGNOSTIC_ARCHIVE_TIMEOUT_S)
        store_probe.assert_called_once_with(
            timeout_s=doctor._DIAGNOSTIC_STORE_TIMEOUT_S)
        detect.assert_called_once_with(
            timeout_s=doctor._DIAGNOSTIC_DETECT_TIMEOUT_S,
            observation=mock.ANY)
        store_counts.assert_called_once_with((), [])
        db_probe.assert_called_once_with(
            deep=False, progress=None, generation=generation)
        generation_probe.assert_called_once_with(routine=True)
        semantic_probe.assert_called_once_with(deep=False, fix=False)
        lag_probe.assert_called_once_with(deadline=mock.ANY)
        ensure.assert_not_called()
        rendered = output.getvalue()
        # the page scan did not run, so no row speaks for it
        self.assertNotIn("integrity", rendered)
        self.assertIn("installed build", rendered)
        self.assertIn("lags local master by 8.0 days", rendered)
        self.assertIn("uv tool install --force --from", rendered)
        self.assertIn("without your consent", rendered)
        self.assertIn("first semantic search", rendered)
        self.assertIn(
            f"`{doctor._cli_command('doctor', '--fix')}`", rendered)
        self.assertNotIn("search or status", rendered)

    def test_archive_probe_reuses_the_shared_footprint_byte_count(self) -> None:
        import archive

        expected = {"state": "disabled", "stored_bytes": 123}
        with mock.patch.object(
                archive, "status", return_value=expected) as status:
            self.assertIs(
                doctor._archive_probe(stored_bytes=123), expected)
        status.assert_called_once_with(
            stored_bytes=123,
            manifest_timeout_s=doctor._DIAGNOSTIC_ARCHIVE_TIMEOUT_S,
        )

    def test_deep_report_generation_probe_is_shared_with_actual_readiness(self) -> None:
        generation = {"state": "ready", "detail": "fixture generation"}
        current_drift = indexd_runtime.DriftReport("current")
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-generation-") as td:
            root = Path(td)
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor, "_semantic_probe", return_value=_missing_semantic()),
                mock.patch.object(
                    doctor, "_index_summary_state",
                    return_value={"state": "never-built"}),
                mock.patch.object(
                    doctor.indexd_runtime, "observe_store_drift",
                    return_value=([], current_drift)),
                mock.patch.object(
                    doctor, "_archive_probe",
                    return_value={"state": "disabled"}),
                mock.patch.object(
                    corpusdb, "search_generation_health",
                    return_value=generation,
                ) as generation_probe,
                mock.patch.object(
                    doctor.indexd_runtime, "machine_freshness",
                    return_value={}),
                mock.patch.object(
                    doctor.common, "detected_stores", return_value=[]),
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}),
                mock.patch.object(
                    doctor.install_lag, "installed_master_lag",
                    return_value={
                        "state": "not-installed", "detail": "source checkout"}),
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_failing",
                    return_value=(0, "")),
                mock.patch.object(
                    doctor.indexd_runtime, "indexing_failure",
                    return_value=None),
            ):
                payload = doctor.probe(deep=True, for_report=True)
        generation_probe.assert_called_once_with(routine=False)
        self.assertEqual(payload["core"]["search_db"]["state"], "missing")
        self.assertIs(payload["_render"]["generation"], generation)

    def test_json_report_reuses_probe_detection(self) -> None:
        detected = [{"name": "fixture", "count": 1}]
        payload = {
            "paths": {},
            "core": {"live": True, "rust": True, "binary": True},
            "semantic": _missing_semantic(),
            "resources": {},
            "detected": detected,
        }
        with (
            mock.patch.object(doctor, "probe", return_value=payload) as probe,
            mock.patch.object(
                doctor.common, "detected_stores",
                side_effect=AssertionError("duplicate detected-stores probe")),
        ):
            result = doctor._json_report()
        probe.assert_called_once_with(deep=False)
        self.assertIs(result["detected_not_indexed"], detected)
        # the deleted drift clock must not resurface as a machine field
        self.assertNotIn("drift", result)

    def test_observe_only_daemon_status_never_mutates_owner_or_probe_files(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT,
            None, None, None)
        with (
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=absent,
            ) as inspect,
            mock.patch.object(
                indexd_runtime.ownerfile, "snapshot",
                side_effect=FileNotFoundError,
            ),
            mock.patch.object(
                indexd_runtime, "_spawn_guard_resource_status",
                return_value=None,
            ) as spawn_guard,
            mock.patch.object(
                indexd_runtime, "_settle_indexd_owner",
                side_effect=AssertionError("diagnostic settled an owner"),
            ) as settle,
            mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd",
                side_effect=AssertionError("diagnostic retired an owner"),
            ) as retire,
            mock.patch.object(
                indexd_runtime, "_write_indexd_response_probe",
                side_effect=AssertionError("diagnostic wrote a heartbeat probe"),
            ) as write_probe,
            mock.patch.object(
                indexd_runtime, "_clear_indexd_response_probe",
                side_effect=AssertionError("diagnostic cleared a heartbeat probe"),
            ) as clear_probe,
        ):
            status = indexd_runtime.indexd_resource_status(
                observe_only=True)
        self.assertEqual(status, {"running": False})
        inspect.assert_called_once_with(settle_child=False)
        spawn_guard.assert_called_once_with(settle_child=False)
        settle.assert_not_called()
        retire.assert_not_called()
        write_probe.assert_not_called()
        clear_probe.assert_not_called()

    def test_routine_daemon_status_omits_unbounded_rss_probe(self) -> None:
        compatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            mock.Mock(), 123, "fixture-start")
        with (
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=compatible),
            mock.patch.object(indexd_runtime, "_indexd_ready", return_value=True),
            mock.patch.object(
                indexd_runtime, "_indexd_responsiveness",
                return_value=("responsive", 0.0)),
            mock.patch.object(
                indexd_runtime.common, "process_rss_bytes",
                side_effect=AssertionError(
                    "routine diagnostic launched the RSS subprocess"),
            ) as rss,
        ):
            observed = indexd_runtime.indexd_resource_status(
                observe_only=True, include_rss=False)
        self.assertTrue(observed["running"])
        self.assertNotIn("rss_bytes", observed)
        rss.assert_not_called()

    def test_status_forwards_deep_and_unknown_arguments_to_doctor(self) -> None:
        with (
            mock.patch.object(cli.common, "utf8_stdio"),
            mock.patch.object(doctor, "main", return_value=0) as doctor_main,
        ):
            args = SimpleNamespace(
                json=False, fn=cli.cmd_status, rest=["--deep"])
            self.assertEqual(cli.cmd_status(args), 0)
            doctor_main.assert_called_once_with(["--deep"])
            doctor_main.reset_mock()
            args.rest = ["--mystery"]
            self.assertEqual(cli.cmd_status(args), 0)
            doctor_main.assert_called_once_with(["--mystery"])

    def test_cheap_status_observes_daemon_once_without_mutation(self) -> None:
        daemon = {"running": False, "state": "fixture"}
        drift = indexd_runtime.DriftReport("current")
        with (
            mock.patch.object(
                cli.indexd_runtime, "indexd_resource_status",
                return_value=daemon,
            ) as daemon_probe,
            mock.patch.object(
                cli.indexd_runtime, "observe_store_drift",
                return_value=([], drift),
            ) as drift_probe,
            mock.patch.object(
                cli.indexd_runtime, "indexing_failure",
                return_value=None,
            ) as failure_probe,
            mock.patch.object(cli.common, "index_summary", return_value=None),
        ):
            status = cli._status_core()
        self.assertFalse(status["index_built"])
        daemon_probe.assert_called_once_with(
            observe_only=True, include_rss=False)
        drift_probe.assert_called_once_with(timeout_s=mock.ANY)
        failure_probe.assert_called_once_with(
            daemon_status=daemon, drift_report=drift)

    def test_status_deadline_turns_unfinished_evidence_into_unknowns(self) -> None:
        clock = [0.0]
        drift = indexd_runtime.DriftReport("current")

        def stores(*, timeout_s: float):
            self.assertEqual(timeout_s, cli._STATUS_STORE_TIMEOUT_S)
            clock[0] += timeout_s
            return [], drift

        def summary(*, deadline: float):
            self.assertEqual(deadline, cli._STATUS_ROUTINE_TIMEOUT_S)
            clock[0] = deadline
            raise TimeoutError("fixture census exhausted the deadline")

        with tempfile.TemporaryDirectory(
                prefix="agrep-status-deadline-") as td:
            root = Path(td)
            with (
                mock.patch.object(cli.time, "monotonic",
                                  side_effect=lambda: clock[0]),
                mock.patch.object(cli.common, "DATA_DIR", root),
                mock.patch.object(cli.common, "MESSAGES_PATH",
                                  root / "messages.jsonl"),
                mock.patch.object(
                    cli.indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}),
                mock.patch.object(
                    cli.indexd_runtime, "observe_store_drift",
                    side_effect=stores),
                mock.patch.object(
                    cli.indexd_runtime, "indexing_failure",
                    return_value=None),
                mock.patch.object(
                    cli.common, "index_summary", side_effect=summary),
                mock.patch.object(
                    cli.common, "detected_stores",
                    side_effect=AssertionError(
                        "status detection ran after the deadline")) as detect,
            ):
                observed = cli._status_core(
                    deadline=cli._STATUS_ROUTINE_TIMEOUT_S)
        self.assertIsNone(observed["index_built"])
        self.assertIsNone(observed["search_index_ready"])
        self.assertIsNone(observed["agents_taught"])
        self.assertEqual(observed["diagnostics"]["state"], "partial")
        self.assertIn("index summary", observed["diagnostics"]["deferred"])
        self.assertIn(
            "unsupported-store detection",
            observed["diagnostics"]["deferred"])
        detect.assert_not_called()

    def test_status_unknown_census_never_renders_no_index_or_ready(self) -> None:
        core = {
            "data_dir": "/fixture", "data_dir_source": "fixture",
            "warnings": [], "index_built": None,
            "index_state": "status-deferred",
            "search_index_ready": None,
            "search_index_state": "status-deferred",
            "agents_taught": None,
            "daemon": {"running": False, "state": "status-deferred"},
            "detected_not_indexed": [],
            "diagnostics": {
                "tier": "routine", "state": "partial", "budget_s": 0.8,
                "deferred": ["index summary"],
                "details": {"index summary": "fixture deadline expired"},
            },
        }
        semantic = {
            "semantic_deps": None, "semantic_verified": False,
            "semantic_status": "status-deferred",
            "semantic_state": "not-verified", "semantic_ready": None,
            "semantic_embedding_now": None,
        }
        with (
            mock.patch.object(cli, "_status_core", return_value=core),
            mock.patch.object(
                cli, "_kick_repair_if_damaged",
                return_value=indexd_runtime.RepairKick(False, "fixture")),
            mock.patch.object(cli, "_status_semantic", return_value=semantic),
        ):
            rendered = "\n".join(cli._status_lines("agrep"))
        # a summary the routine budget never reached knows nothing, and a
        # line saying so is noise (law 6): the corpus block is simply absent
        self.assertNotIn("no index yet", rendered)
        self.assertNotIn("messages ·", rendered)
        self.assertNotIn("unreadable", rendered)
        for leak in ("not verified", "deferred", "census", "routine"):
            self.assertNotIn(leak, rendered)

    def test_status_does_not_call_an_active_publication_damage(self) -> None:
        drift = indexd_runtime.DriftReport("current")

        def observe(*, timeout_s: float, observation: dict) -> list:
            observation.update(state="complete")
            return []

        with tempfile.TemporaryDirectory(
                prefix="agrep-status-publishing-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"published")
            with (
                mock.patch.object(cli.common, "DATA_DIR", root),
                mock.patch.object(
                    cli.common, "MESSAGES_PATH", root / "messages.jsonl"),
                mock.patch.object(
                    cli.common, "data_dir_source", return_value="fixture"),
                mock.patch.object(
                    cli.common, "data_dir_warnings", return_value=[]),
                mock.patch.object(
                    cli.settings, "setting_observation",
                    return_value={
                        "state": "verified", "value": "auto",
                        "source": "default"}),
                mock.patch.object(
                    cli.indexd_runtime, "indexd_resource_status",
                    return_value={"running": True}),
                mock.patch.object(
                    cli.indexd_runtime, "observe_store_drift",
                    return_value=([], drift)),
                mock.patch.object(
                    cli.indexd_runtime, "indexing_failure", return_value=None),
                mock.patch.object(
                    cli.indexd_runtime, "machine_freshness",
                    return_value={"state": "no-known-failure"}),
                mock.patch.object(cli.common, "index_summary", return_value=None),
                mock.patch.object(
                    cli, "_status_instruction_enrollment",
                    return_value={"state": "verified", "taught": True}),
                mock.patch.object(
                    cli.common, "detected_stores", side_effect=observe),
                mock.patch.object(
                    doctor, "_corpus_db_readiness",
                    return_value={
                        "state": "stale", "code": "generation-moving"}),
            ):
                observed = cli._status_core(
                    deadline=time.monotonic() + 1.0)
        self.assertEqual(observed["index_state"], "status-deferred")
        self.assertEqual(observed["search_index_state"], "status-deferred")
        self.assertEqual(observed["search_index_code"], "generation-moving")
        semantic = {
            "semantic_deps": None, "semantic_verified": False,
            "semantic_status": "not-inspected",
            "semantic_state": "not-verified", "semantic_ready": None,
            "semantic_embedding_now": None,
        }
        with (
            mock.patch.object(cli, "_status_core", return_value=observed),
            mock.patch.object(
                cli, "_kick_repair_if_damaged",
                return_value=indexd_runtime.RepairKick(False, "fixture")),
            mock.patch.object(cli, "_status_semantic", return_value=semantic),
        ):
            rendered = "\n".join(cli._status_lines("agrep"))
        self.assertNotIn("unreadable", rendered)
        self.assertNotIn("missing", rendered)
        self.assertNotIn("`agrep index", rendered)

    def test_status_defers_every_active_publication_window(self) -> None:
        cases = (
            "missing-at-presence", "missing-after-presence",
            "metadata-moved", "replaced-while-opening", "uncoded-stale",
            "ready-db",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                    prefix="agrep-status-db-move-") as td:
                root = Path(td)
                database = root / "corpus.db"
                if case != "missing-at-presence":
                    database.write_bytes(b"published")

                def observe(*, timeout_s: float, observation: dict) -> list:
                    observation.update(
                        state="complete", detail="fixture complete")
                    return []

                def readiness() -> dict:
                    if case == "metadata-moved":
                        database.unlink()
                        return {
                            "state": "corrupt",
                            "detail": "database metadata is unreadable",
                        }
                    if case == "replaced-while-opening":
                        replacement = database.with_suffix(".replacement")
                        replacement.write_bytes(b"new publication")
                        replacement.replace(database)
                        return {
                            "state": "corrupt",
                            "detail": "SQLite source changed while opening",
                        }
                    if case == "uncoded-stale":
                        return {"state": "stale", "detail": "fixture drift"}
                    if case == "ready-db":
                        return {"state": "ready"}
                    return {"state": "missing"}

                with (
                    mock.patch.object(cli.common, "DATA_DIR", root),
                    mock.patch.object(
                        cli.common, "MESSAGES_PATH", root / "messages.jsonl"),
                    mock.patch.object(
                        cli.common, "data_dir_source", return_value="fixture"),
                    mock.patch.object(
                        cli.common, "data_dir_warnings", return_value=[]),
                    mock.patch.object(
                        cli.settings, "setting_observation",
                        return_value={
                            "state": "verified", "value": "auto",
                            "source": "default"}),
                    mock.patch.object(
                        cli.indexd_runtime, "indexd_resource_status",
                        return_value={"running": True}),
                    mock.patch.object(
                        cli.indexd_runtime, "observe_store_drift",
                        return_value=([], {"state": "complete"})),
                    mock.patch.object(
                        cli.indexd_runtime, "indexing_failure",
                        return_value=None),
                    mock.patch.object(
                        cli.indexd_runtime, "machine_freshness",
                        return_value={"state": "no-known-failure"}),
                    mock.patch.object(
                        cli.common, "index_summary", return_value=None),
                    mock.patch.object(
                        cli, "_status_instruction_enrollment",
                        return_value={"state": "verified", "taught": True}),
                    mock.patch.object(
                        cli.common, "detected_stores", side_effect=observe),
                    mock.patch.object(
                        doctor, "_corpus_db_readiness",
                        side_effect=readiness),
                ):
                    observed = cli._status_core(
                        deadline=time.monotonic() + 1.0)
                self.assertEqual(observed["index_state"], "status-deferred")
                expected = "ready" if case == "ready-db" else "status-deferred"
                self.assertEqual(observed["search_index_state"], expected)
                with (
                    mock.patch.object(
                        cli, "_status_core", return_value=observed),
                    mock.patch.object(
                        cli, "_kick_repair_if_damaged",
                        return_value=indexd_runtime.RepairKick(
                            False, "fixture")),
                    mock.patch.object(
                        cli, "_status_semantic", return_value={}),
                ):
                    rendered = "\n".join(cli._status_lines("agrep"))
                self.assertNotIn("unreadable", rendered)
                self.assertNotIn("missing", rendered)
                self.assertNotIn("`agrep index", rendered)

    def test_first_run_status_has_one_setup_remedy(self) -> None:
        core = {
            "data_dir": "/fixture", "data_dir_source": "fixture",
            "warnings": [], "index_built": False,
            "search_index_ready": False, "search_index_state": "missing",
            "agents_taught": True, "detected_not_indexed": [],
            "daemon": {"running": False, "state": "not-running"},
            "diagnostics": {
                "tier": "routine", "state": "complete", "budget_s": 0.8,
                "deferred": [], "details": {},
            },
        }
        semantic = {
            "semantic_deps": None, "semantic_verified": False,
            "semantic_status": "not-verified",
            "semantic_state": "not-verified", "semantic_ready": None,
            "semantic_embedding_now": None,
        }
        with (
            mock.patch.object(cli, "_status_core", return_value=core),
            mock.patch.object(
                cli, "_kick_repair_if_damaged",
                return_value=indexd_runtime.RepairKick(False, "fixture")) as kick,
            mock.patch.object(cli, "_status_semantic", return_value=semantic),
        ):
            rendered = "\n".join(cli._status_lines("agrep"))
        kick.assert_called_once_with(core)
        self.assertIn("no index yet", rendered)
        self.assertIn("`agrep setup`", rendered)
        self.assertNotIn("the search index is missing", rendered)
        self.assertEqual(rendered.count("`agrep "), 1)
        self.assertNotIn("background publication pending", rendered)
        # the deferral footer is gone: the routine tier never narrates its own
        # scheduling, so a healthy-but-unchecked box says nothing at all
        self.assertNotIn("doctor --deep", rendered)

    def test_status_empty_database_is_unavailable_not_missing(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-status-empty-db-") as td:
            root = Path(td)
            (root / "corpus.db").write_bytes(b"")
            observation = {}

            def detect(*, timeout_s: float, observation: dict) -> list:
                observation.update(
                    state="complete", detail="fixture detection complete")
                return []

            with (
                mock.patch.object(cli.common, "DATA_DIR", root),
                mock.patch.object(
                    cli.common, "MESSAGES_PATH", root / "messages.jsonl"),
                mock.patch.object(
                    cli.common, "data_dir_source", return_value="fixture"),
                mock.patch.object(
                    cli.common, "data_dir_warnings", return_value=[]),
                mock.patch.object(
                    cli.settings, "setting_observation",
                    return_value={
                        "state": "verified", "value": "auto",
                        "source": "default"}),
                mock.patch.object(
                    cli.indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}),
                mock.patch.object(
                    cli.indexd_runtime, "observe_store_drift",
                    return_value=([], indexd_runtime.DriftReport("current"))),
                mock.patch.object(
                    cli.indexd_runtime, "indexing_failure", return_value=None),
                mock.patch.object(
                    cli.indexd_runtime, "machine_freshness",
                    return_value={"state": "no-known-failure"}),
                mock.patch.object(cli.common, "index_summary", return_value=None),
                mock.patch.object(
                    cli.common, "detected_stores", side_effect=detect),
            ):
                observed = cli._status_core(
                    deadline=time.monotonic() + 1.0)
        self.assertIsNone(observed["search_index_ready"])
        self.assertEqual(observed["search_index_state"], "unavailable")
        self.assertIn(
            "search database readiness",
            observed["diagnostics"]["deferred"])
        self.assertNotEqual(observed["search_index_state"], "missing")

    def test_status_damaged_enrollment_is_never_true(self) -> None:
        cases = {
            "directory": lambda path: path.mkdir(),
            "malformed": lambda path: path.write_bytes(b"{"),
        }
        for label, create in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                    prefix=f"agrep-status-teach-{label}-") as td:
                root = Path(td)
                create(root / "teach.json")

                def detect(*, timeout_s: float, observation: dict) -> list:
                    observation.update(
                        state="complete", detail="fixture detection complete")
                    return []

                with (
                    mock.patch.object(cli.common, "DATA_DIR", root),
                    mock.patch.object(
                        cli.common, "MESSAGES_PATH", root / "messages.jsonl"),
                    mock.patch.object(
                        cli.common, "data_dir_source", return_value="fixture"),
                    mock.patch.object(
                        cli.common, "data_dir_warnings", return_value=[]),
                    mock.patch.object(
                        cli.settings, "setting_observation",
                        return_value={
                            "state": "verified", "value": "auto",
                            "source": "default"}),
                    mock.patch.object(
                        cli.indexd_runtime, "indexd_resource_status",
                        return_value={"running": False}),
                    mock.patch.object(
                        cli.indexd_runtime, "observe_store_drift",
                        return_value=(
                            [], indexd_runtime.DriftReport("current"))),
                    mock.patch.object(
                        cli.indexd_runtime, "indexing_failure",
                        return_value=None),
                    mock.patch.object(
                        cli.indexd_runtime, "machine_freshness",
                        return_value={"state": "no-known-failure"}),
                    mock.patch.object(
                        cli.common, "index_summary", return_value=None),
                    mock.patch.object(
                        cli.common, "detected_stores", side_effect=detect),
                ):
                    observed = cli._status_core(
                        deadline=time.monotonic() + 1.0)
            self.assertIsNone(observed["agents_taught"])
            self.assertEqual(
                observed["instruction_enrollment"]["state"], "unavailable")

    def test_main_wires_deep_and_fix_explicitly(self) -> None:
        with (
            mock.patch.object(doctor.common, "utf8_stdio"),
            mock.patch.object(doctor, "report", return_value={}) as report,
            mock.patch.object(doctor, "fix", return_value=0) as fix,
        ):
            self.assertEqual(doctor.main([]), 0)
            report.assert_called_once_with(deep=False, fix_actions=False)
            report.reset_mock()
            self.assertEqual(doctor.main(["--deep"]), 0)
            report.assert_called_once_with(deep=True, fix_actions=False)
            report.reset_mock()
            self.assertEqual(doctor.main(["--fix"]), 0)
            report.assert_called_once_with(deep=False, fix_actions=True)
            fix.assert_called_once_with()
            report.reset_mock()
            fix.reset_mock()
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(doctor.main(["--mystery"]), 2)
            report.assert_not_called()
            fix.assert_not_called()
            self.assertIn(
                "agrep doctor: unrecognized argument(s): --mystery",
                errors.getvalue())

        output = io.StringIO()
        with (
            mock.patch.object(doctor.common, "utf8_stdio"),
            mock.patch.object(doctor, "_json_report", return_value={"ok": True}) as machine,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(doctor.main(["--json", "--deep"]), 0)
        machine.assert_called_once_with(deep=True)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})

    def test_main_refuses_action_combos_instead_of_dropping_one(self) -> None:
        # B12: --json --fix used to print JSON and exit 0 with fix() silently
        # unreachable; every two-action combo now refuses by name (gate law).
        for argv, dropped, blocker in (
            (["--json", "--fix"], "--fix", "--json"),
            (["--fix", "--json"], "--fix", "--json"),
            (["--json", "--setup"], "--setup", "--json"),
            (["--fix", "--setup"], "--fix", "--setup"),
            (["--deep", "--setup"], "--deep", "--setup"),
        ):
            with self.subTest(argv=argv), (
                mock.patch.object(doctor.common, "utf8_stdio")), (
                mock.patch.object(doctor, "report")) as report, (
                mock.patch.object(doctor, "fix")) as fix, (
                mock.patch.object(doctor, "setup")) as setup, (
                mock.patch.object(doctor, "_json_report")) as machine:
                errors = io.StringIO()
                output = io.StringIO()
                with contextlib.redirect_stderr(errors), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(doctor.main(argv), 2)
                report.assert_not_called()
                fix.assert_not_called()
                setup.assert_not_called()
                machine.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                message = errors.getvalue()
                self.assertIn(dropped, message)
                self.assertIn(blocker, message)
                self.assertIn("cannot be combined", message)


class DiagnosticDeadlineTests(unittest.TestCase):
    def test_expired_footprint_discards_partial_counts_without_walking(self) -> None:
        with (
            mock.patch.object(doctor.time, "monotonic", return_value=1.0),
            mock.patch.object(
                doctor.os, "walk",
                side_effect=AssertionError("expired footprint began a walk"),
            ) as walk,
        ):
            observed = doctor._data_footprint(deadline=0.5)
        self.assertFalse(observed["complete"])
        self.assertIsNone(observed["files"])
        self.assertIsNone(observed["bytes"])
        self.assertIsNone(observed["archive_bytes"])
        walk.assert_not_called()

    def test_index_summary_timeout_is_deferred_not_proof_damage(self) -> None:
        with (
            mock.patch.object(doctor.time, "monotonic", return_value=0.0),
            mock.patch.object(
                doctor.common, "index_summary",
                side_effect=TimeoutError("fixture deadline"),
            ),
        ):
            observed = doctor._index_summary_state(deadline=1.0)
        self.assertEqual(observed["state"], "status-deferred")
        self.assertEqual(observed["check"], "the index summary")
        self.assertFalse(doctor._ran(observed))
        self.assertNotIn("detail", observed)

    def _probe_mocks(self, *, store_probe, model_probe, archive_probe,
                     detect_probe, lag_probe):
        summary = {
            "state": "ready", "sessions": 1, "messages": 1, "age_s": 0,
            "per_agent": [{"agent": "codex", "sessions": 1, "messages": 1}],
            "_parsed_newest_ms": {"codex": 1},
        }
        readiness = {
            "state": "ready", "detail": "current",
            "integrity": doctor._integrity_not_verified(),
        }
        return (
            mock.patch.object(
                doctor, "_semantic_probe", return_value=_missing_semantic()),
            mock.patch.object(
                doctor, "_orphan_inventory",
                return_value={
                    "state": "complete", "complete": True,
                    "count": 0, "bytes": 0,
                }),
            mock.patch.object(
                doctor, "_data_footprint",
                return_value={
                    "state": "complete", "complete": True,
                    "files": 1, "bytes": 1, "breakdown": "",
                    "archive_bytes": 0,
                }),
            mock.patch.object(
                doctor, "_index_summary_state", return_value=summary),
            mock.patch.object(
                doctor.indexd_runtime, "observe_store_drift",
                side_effect=store_probe),
            mock.patch.object(doctor, "_store_counts", return_value=[]),
            mock.patch.object(
                corpusdb, "search_generation_health",
                return_value={"state": "ready"}),
            mock.patch.object(
                doctor, "_corpus_db_readiness", return_value=readiness),
            mock.patch.object(
                doctor, "_model_attribution", side_effect=model_probe),
            mock.patch.object(
                doctor, "_archive_probe", side_effect=archive_probe),
            mock.patch.object(
                doctor.indexd_runtime, "indexd_resource_status",
                return_value={"running": False}),
            mock.patch.object(
                doctor.indexd_runtime, "indexing_failure", return_value=None),
            mock.patch.object(
                doctor.indexd_runtime, "machine_freshness",
                return_value={"state": "no-known-failure"}),
            mock.patch.object(
                doctor, "_machine_freshness_fields",
                return_value={"freshness": {"state": "no-known-failure"},
                              "corpus_age_s": 0}),
            mock.patch.object(
                doctor.common, "detected_stores", side_effect=detect_probe),
            mock.patch.object(
                doctor.install_lag, "installed_master_lag",
                side_effect=lag_probe),
            mock.patch.object(
                doctor, "_teach_reconcile_probe",
                return_value={
                    "version": 1, "state": "unenrolled", "repaired": [],
                    "refusals": [], "preserved_newer": [],
                    "_enrollment": {"state": "unenrolled", "targets": 0},
                }),
        )

    def test_routine_enrichments_share_one_cumulative_deadline(self) -> None:
        clock = [0.0]
        received: list[tuple[str, float]] = []

        def spend(label: str, seconds: float) -> None:
            received.append((label, seconds))
            clock[0] += seconds

        def stores(*, timeout_s: float):
            spend("stores", timeout_s)
            return ([{"name": "codex", "files": 1}],
                    indexd_runtime.DriftReport("current"))

        def model(_summary, _readiness, *, deep: bool, timeout_s: float):
            self.assertFalse(deep)
            spend("model", timeout_s)
            return {"state": "empty"}

        def archive(*, stored_bytes: int, deep: bool, timeout_s: float):
            self.assertEqual(stored_bytes, 0)
            self.assertFalse(deep)
            spend("archive", timeout_s)
            return {"state": "disabled"}

        def detect(*, timeout_s: float, observation: dict):
            spend("detect", timeout_s)
            observation.update(
                state="complete",
                detail="bounded ingest-registry detection completed")
            return []

        def lag(*, deadline: float):
            self.assertEqual(deadline, 0.8)
            clock[0] = deadline
            return {
                "state": "budget-exceeded",
                "detail": "shared routine budget expired",
            }

        patches = self._probe_mocks(
            store_probe=stores, model_probe=model, archive_probe=archive,
            detect_probe=detect, lag_probe=lag)
        with mock.patch.object(
                doctor.time, "monotonic", side_effect=lambda: clock[0]):
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                payload = doctor.probe(routine_deadline=0.8)

        self.assertEqual(
            received,
            [("stores", doctor._DIAGNOSTIC_STORE_TIMEOUT_S), ("model", 0.15),
             ("archive", 0.15),
             ("detect",
              0.8 - (doctor._DIAGNOSTIC_STORE_TIMEOUT_S + 0.15 + 0.15))],
        )
        self.assertLessEqual(clock[0], doctor._DIAGNOSTIC_ROUTINE_TIMEOUT_S)
        self.assertEqual(payload["install_lag"]["state"], "budget-exceeded")
        self.assertEqual(payload["diagnostics"]["state"], "partial")
        self.assertIn(
            "installed-build provenance", payload["diagnostics"]["deferred"])

    def test_expired_budget_never_becomes_empty_or_green_evidence(self) -> None:
        clock = [0.0]

        def stores(*, timeout_s: float):
            self.assertEqual(timeout_s, 0.1)
            clock[0] += timeout_s
            return (
                None,
                indexd_runtime.DriftReport(
                    "unknown", code="census-unavailable",
                    detail="live census unavailable"),
            )

        def model(_summary, _readiness, *, deep: bool, timeout_s: float):
            self.assertFalse(deep)
            self.assertEqual(timeout_s, 0.0)
            return {
                "state": "unavailable",
                "reason": "routine diagnostic budget expired",
                "accountable": None, "with_model": None,
                "unknown": None, "percent": None,
            }

        def archive(*, stored_bytes: int, deep: bool, timeout_s: float):
            self.assertFalse(deep)
            self.assertEqual(timeout_s, 0.0)
            return {
                "state": "status-deferred",
                "detail": "routine diagnostic budget expired",
            }

        def detect(*, timeout_s: float, observation: dict):
            raise AssertionError(
                f"detection ran after the deadline with {timeout_s}")

        def lag(*, deadline: float):
            self.assertEqual(deadline, 0.1)
            return {
                "state": "budget-exceeded",
                "detail": "routine diagnostic budget expired",
            }

        patches = self._probe_mocks(
            store_probe=stores, model_probe=model, archive_probe=archive,
            detect_probe=detect, lag_probe=lag)
        with mock.patch.object(
                doctor.time, "monotonic", side_effect=lambda: clock[0]):
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                payload = doctor.probe(routine_deadline=0.1)

        self.assertEqual(
            payload["core"]["store_observation"]["state"], "budget-exceeded")
        self.assertEqual(payload["model_attribution"]["state"], "unavailable")
        self.assertIsNone(payload["model_attribution"]["accountable"])
        self.assertEqual(payload["archive"]["state"], "status-deferred")
        self.assertEqual(payload["detection"]["state"], "budget-exceeded")
        self.assertEqual(payload["detected"], [])
        self.assertNotEqual(payload["diagnostics"]["state"], "complete")

    def test_unavailable_store_census_makes_the_aggregate_partial(self) -> None:
        def stores(*, timeout_s: float):
            self.assertEqual(timeout_s, doctor._DIAGNOSTIC_STORE_TIMEOUT_S)
            return (
                None,
                indexd_runtime.DriftReport(
                    "unknown", code="census-unavailable",
                    detail="live census unavailable"),
            )

        def model(_summary, _readiness, *, deep: bool, timeout_s: float):
            return {"state": "empty"}

        def archive(*, stored_bytes: int, deep: bool, timeout_s: float):
            return {"state": "disabled"}

        def detect(*, timeout_s: float, observation: dict):
            observation.update(
                state="complete",
                detail="bounded ingest-registry detection completed")
            return []

        def lag(*, deadline: float):
            return {"state": "current", "detail": "verified current"}

        patches = self._probe_mocks(
            store_probe=stores, model_probe=model, archive_probe=archive,
            detect_probe=detect, lag_probe=lag)
        with mock.patch.object(doctor.time, "monotonic", return_value=0.0):
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                payload = doctor.probe(routine_deadline=0.8)

        self.assertEqual(
            payload["core"]["store_observation"]["state"], "unavailable")
        self.assertEqual(payload["diagnostics"]["state"], "partial")
        self.assertEqual(
            payload["diagnostics"]["deferred"],
            ["store census"])

    def test_teach_enrollment_uses_the_bounded_loader_once(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-teach-bounded-") as td:
            data = Path(td)
            (data / "teach.json").write_text(
                '{"targets":["/one","/two"]}', encoding="utf-8")
            health = {
                "version": 1, "state": "clean", "repaired": [],
                "refusals": [], "preserved_newer": [],
            }
            with (
                mock.patch.object(doctor.common, "DATA_DIR", data),
                mock.patch.object(
                    doctor.ownerfile, "snapshot",
                    wraps=doctor.ownerfile.snapshot) as load,
                mock.patch.object(
                    teach, "reconcile_health", return_value=health),
                mock.patch.object(
                    Path, "read_text",
                    side_effect=AssertionError("unbounded second teach read")),
            ):
                observed = doctor._teach_reconcile_probe()
        load.assert_called_once_with(
            data / "teach.json", max_bytes=64 * 1024)
        self.assertEqual(
            observed["_enrollment"],
            {"state": "enrolled", "targets": 2},
        )

    def test_routine_teach_probe_defers_reconciliation_after_bounded_load(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-teach-routine-") as td:
            data = Path(td)
            (data / "teach.json").write_text(
                '{"targets":["/one"]}', encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", data),
                mock.patch.object(
                    teach, "reconcile_health",
                    side_effect=AssertionError(
                        "routine probe entered reconciliation")) as reconcile,
            ):
                observed = doctor._teach_reconcile_probe(
                    deadline=time.monotonic() + 1.0)
        self.assertEqual(observed["state"], "status-deferred")
        self.assertEqual(
            observed["_enrollment"],
            {"state": "enrolled", "targets": 1})
        # a reconcile that never ran has refused nothing to report
        self.assertEqual(observed["refusals"], [])
        reconcile.assert_not_called()

    def test_teach_load_consuming_deadline_never_continues_to_reconcile(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-teach-deadline-") as td:
            data = Path(td)
            (data / "teach.json").write_text(
                '{"targets":["/one"]}', encoding="utf-8")
            clock = [0.0]
            snapshot = doctor.ownerfile.snapshot

            def consume(path, *, max_bytes):
                observed = snapshot(path, max_bytes=max_bytes)
                clock[0] = 0.2
                return observed

            with (
                mock.patch.object(doctor.common, "DATA_DIR", data),
                mock.patch.object(
                    doctor.time, "monotonic",
                    side_effect=lambda: clock[0]),
                mock.patch.object(
                    doctor.ownerfile, "snapshot", side_effect=consume),
                mock.patch.object(
                    teach, "reconcile_health",
                    side_effect=AssertionError(
                        "reconciliation ran after the deadline")) as reconcile,
            ):
                observed = doctor._teach_reconcile_probe(deadline=0.1)
        self.assertEqual(observed["state"], "status-deferred")
        self.assertEqual(
            observed["_enrollment"],
            {"state": "enrolled", "targets": 1})
        self.assertEqual(observed["deferred_kind"], "budget-exceeded")
        self.assertEqual(observed["refusals"], [])
        reconcile.assert_not_called()

    def test_settings_damage_never_becomes_the_embeddings_default(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-settings-") as td:
            root = Path(td)
            cases = {
                "malformed": lambda path: path.write_bytes(b"{"),
                "oversize": lambda path: path.write_bytes(
                    b"x" * (settings._MAX_SETTINGS_BYTES + 1)),
                "nonregular": lambda path: path.mkdir(),
            }
            for label, create in cases.items():
                with self.subTest(case=label):
                    path = root / label
                    create(path)
                    with mock.patch.object(settings, "SETTINGS_PATH", path):
                        observed = settings.setting_observation("embeddings")
                    self.assertEqual(observed["state"], "unavailable")
                    self.assertIsNone(observed["value"])

    def test_deep_narrates_all_unbounded_work_before_probe(self) -> None:
        self.assertIn("attribution", doctor.__doc__)
        self.assertIn("archive", doctor.__doc__)

        human = io.StringIO()

        def stop_human(**_kwargs):
            notice = human.getvalue()
            self.assertIn("SQLite integrity", notice)
            self.assertIn("model attribution", notice)
            self.assertIn("archive manifest", notice)
            raise RuntimeError("stop after narration")

        with (
            mock.patch.object(doctor, "probe", side_effect=stop_human),
            contextlib.redirect_stdout(human),
            self.assertRaisesRegex(RuntimeError, "stop after narration"),
        ):
            doctor.report(deep=True)

        machine_err = io.StringIO()

        def stop_machine(**_kwargs):
            notice = machine_err.getvalue()
            self.assertIn("SQLite integrity", notice)
            self.assertIn("model attribution", notice)
            self.assertIn("archive manifest", notice)
            raise RuntimeError("stop after narration")

        with (
            mock.patch.object(doctor, "probe", side_effect=stop_machine),
            contextlib.redirect_stderr(machine_err),
            self.assertRaisesRegex(RuntimeError, "stop after narration"),
        ):
            doctor._json_report(deep=True)

    def test_routine_model_attribution_aborts_its_group_by(self) -> None:
        class SlowQualityDb:
            def __init__(self) -> None:
                self.progress = None
                self.closed = False

            def execute(self, sql: str):
                if sql == "PRAGMA query_only=ON":
                    return self
                if self.progress is not None:
                    self.progress()
                raise sqlite3.OperationalError("interrupted")

            def set_progress_handler(self, callback, _steps: int) -> None:
                self.progress = callback

            def close(self) -> None:
                self.closed = True

        database = SlowQualityDb()
        with (
            mock.patch.object(
                doctor, "_open_corpus_diagnostic_snapshot",
                return_value=database) as opened,
            mock.patch.object(
                doctor.time, "monotonic",
                side_effect=(10.0, 10.2)),
        ):
            quality = doctor._corpus_quality(
                {"state": "ready"}, timeout_s=0.1)
        self.assertEqual(
            quality["readiness"]["state"], "budget-exceeded")
        self.assertFalse(doctor._ran(quality["readiness"]))
        self.assertNotIn("detail", quality["readiness"])
        self.assertTrue(database.closed)
        opened.assert_called_once_with(
            mock.ANY, routine=True)

    def test_routine_archive_manifest_has_a_shared_deadline(self) -> None:
        import archive

        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-archive-deadline-") as td:
            manifest = Path(td) / "manifest.jsonl"
            manifest.write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(archive, "MANIFEST", manifest),
                mock.patch.object(
                    archive.time, "monotonic",
                    side_effect=(10.0, 10.2)),
            ):
                status = archive._manifest_status(timeout_s=0.1)
        self.assertEqual(status["state"], "budget-exceeded")

    def test_timed_out_store_census_reaps_outside_the_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-census-") as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"fixture")
            process = mock.Mock()
            process.communicate.side_effect = (
                indexd_runtime.subprocess.TimeoutExpired(
                    cmd=[str(binary), "stores"], timeout=0.125),
                indexd_runtime.subprocess.TimeoutExpired(
                    cmd=[str(binary), "stores"],
                    timeout=indexd_runtime._DRIFT_REAP_WAIT_S),
            )

            def finish_mock_reaper() -> None:
                process.communicate.side_effect = None
                indexd_runtime._reap_killed_drift_probe(process)

            self.addCleanup(finish_mock_reaper)
            reaper = mock.Mock()
            with (
                mock.patch.object(
                    indexd_runtime.common, "ingest_bin",
                    return_value=binary),
                mock.patch.object(
                    indexd_runtime.subprocess, "Popen",
                    return_value=process),
                mock.patch.object(
                    indexd_runtime.threading, "Thread",
                    return_value=reaper) as thread,
            ):
                self.assertIsNone(
                    indexd_runtime._store_census(timeout_s=0.125))
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.communicate.call_args_list,
            [
                mock.call(timeout=0.125),
                mock.call(timeout=indexd_runtime._DRIFT_REAP_WAIT_S),
            ],
        )
        thread.assert_called_once_with(
            target=indexd_runtime._reap_killed_drift_probe,
            args=(process,),
            daemon=True,
            name="agrep-drift-reaper",
        )
        reaper.start.assert_called_once_with()
        with indexd_runtime._DRIFT_PROBE_LOCK:
            self.assertIn(process, indexd_runtime._DRIFT_PROBE_LIVE)

    def test_store_drift_forwards_the_diagnostic_deadline(self) -> None:
        report = indexd_runtime.DriftReport("unknown", code="fixture")
        with (
            mock.patch.object(
                indexd_runtime, "_store_census", return_value=None) as census,
            mock.patch.object(
                indexd_runtime, "_compute_drift_report",
                return_value=report) as derive,
        ):
            self.assertEqual(
                indexd_runtime.observe_store_drift(timeout_s=0.125),
                (None, report),
            )
        census.assert_called_once_with(timeout_s=0.125)
        derive.assert_called_once_with(store_rows=None)

    def test_store_detection_forwards_the_diagnostic_deadline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-detect-") as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"fixture")
            completed = SimpleNamespace(returncode=0, stdout="[]")
            with (
                mock.patch.object(common, "ingest_bin", return_value=binary),
                mock.patch.object(
                    common.subprocess, "run",
                    return_value=completed) as run,
            ):
                self.assertEqual(
                    common.detected_stores(timeout_s=0.125), [])
        self.assertEqual(run.call_args.kwargs["timeout"], 0.125)

    def test_store_detection_timeout_is_not_a_verified_empty_list(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-detect-") as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"fixture")
            outcome = {}
            with (
                mock.patch.object(common, "ingest_bin", return_value=binary),
                mock.patch.object(
                    common.subprocess, "run",
                    side_effect=common.subprocess.TimeoutExpired(
                        cmd=[str(binary), "detect"], timeout=0.125)),
            ):
                self.assertEqual(
                    common.detected_stores(
                        timeout_s=0.125, observation=outcome), [])
        self.assertEqual(outcome["state"], "budget-exceeded")
        self.assertIn("bounded routine budget", outcome["detail"])

    def test_store_detection_malformed_output_is_explicitly_unavailable(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-detect-") as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"fixture")
            outcome = {}
            completed = SimpleNamespace(returncode=0, stdout="{}")
            with (
                mock.patch.object(common, "ingest_bin", return_value=binary),
                mock.patch.object(
                    common.subprocess, "run", return_value=completed),
            ):
                self.assertEqual(
                    common.detected_stores(
                        timeout_s=0.125, observation=outcome), [])
        self.assertEqual(outcome["state"], "unavailable")
        self.assertIn("malformed", outcome["detail"])

    def test_sentinel_timeout_is_a_bounded_negative_verdict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-sentinel-") as td:
            data = Path(td)
            for name in ("sentinel.json", "sentinel_watch.py"):
                (data / name).write_text("{}", encoding="utf-8")
            timeout = teach.subprocess.TimeoutExpired(
                cmd=["schtasks"], timeout=0.125)
            with (
                mock.patch.object(teach.common, "DATA_DIR", data),
                mock.patch.object(teach.sys, "platform", "win32"),
                mock.patch.object(
                    teach.subprocess, "run",
                    side_effect=timeout) as run,
            ):
                observed = teach.sentinel_status(timeout_s=0.125)
                self.assertFalse(teach.sentinel_armed(timeout_s=0.125))
        self.assertEqual(observed["state"], "budget-exceeded")
        self.assertGreater(run.call_args.kwargs["timeout"], 0.0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 0.125)

    def test_linux_sentinel_shares_one_deadline_across_units(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-doctor-sentinel-linux-") as td:
            data = Path(td)
            for name in ("sentinel.sh", "sentinel_strip.pl"):
                (data / name).write_text("fixture", encoding="utf-8")
            with (
                mock.patch.object(teach.common, "DATA_DIR", data),
                mock.patch.object(teach.sys, "platform", "linux"),
                mock.patch.object(
                    teach.time, "monotonic",
                    side_effect=(10.0, 10.02, 10.04, 10.06, 10.08)),
                mock.patch.object(
                    teach, "_systemctl_user", return_value=0) as systemctl,
            ):
                self.assertTrue(teach.sentinel_armed(timeout_s=0.15))
        self.assertEqual(
            [round(call.kwargs["timeout_s"], 2)
             for call in systemctl.call_args_list],
            [0.13, 0.11, 0.09, 0.07],
        )


class BudgetObservationShapeTests(unittest.TestCase):
    """A deferred check still has to say why it did not run.

    Two call sites render `_budget_observation(...)["detail"]`, and the
    helper never set the key - so on a corpus large enough to exceed the
    census budget (a real 22GB box), `agrep doctor` died with KeyError
    instead of reporting the deferral it had correctly decided on.
    """

    def test_a_deferral_derives_prose_without_carrying_a_detail(self) -> None:
        for what in ("the live store census", "archive manifest validation"):
            with self.subTest(check=what):
                observation = doctor._budget_observation(what)
                self.assertEqual(observation["state"], "budget-exceeded")
                self.assertNotIn("detail", observation)
                self.assertIn(what, doctor._budget_detail(observation))

    def test_the_deferring_call_sites_render_without_a_keyerror(self) -> None:
        probe = doctor._archive_probe(deep=False, timeout_s=0.0)
        self.assertEqual(probe["state"], "status-deferred")
        self.assertTrue(probe["detail"].strip())


class RoutineSemanticProbeSpeaksTheUnknownVocabulary(unittest.TestCase):
    """Routine never probes the semantic runtime, so it must never assert
    live/model_cached as False - an agent parsing doctor --json read "false"
    for a lane that answered queries at that instant (hunt 3 D3)."""

    def test_unprobed_flags_are_none_never_false(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-unknown-") as td:
            with mock.patch.object(doctor.common, "DATA_DIR", Path(td)):
                observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertIsNone(observed["live"])
        self.assertIsNone(observed["model_cached"])
        self.assertEqual(observed["runtime_state"], "not-inspected")
        # no claim file on disk IS evidence: the worker is not running
        self.assertIs(observed["embed_running"], False)

    def test_a_live_worker_claim_is_reported_from_the_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-claim-") as td:
            root = Path(td)
            start = common.process_start_identity(os.getpid())
            (root / ".semantic-embed.lock").write_text(json.dumps({
                "pid": os.getpid(), "process_start": start,
            }), encoding="utf-8")
            (root / ".semantic-embed-state.json").write_text(json.dumps({
                "state": "running", "started_at": time.time(),
            }), encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(semantic.common, "DATA_DIR", root),
            ):
                observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertIs(observed["embed_running"], True)
        self.assertEqual(observed["embed_job"], "running")
        self.assertIsNone(observed["live"])

    def test_tier_membership_is_unknown_not_absent_when_unprobed(self) -> None:
        with mock.patch.object(
                doctor, "probe",
                return_value=_routine_probe_snapshot(live=None)):
            payload = doctor._json_report(deep=False)
        self.assertNotIn("semantic", payload["tiers"])
        self.assertEqual(payload["tiers_unknown"], ["semantic"])

    def test_a_verified_dead_lane_is_absent_not_unknown(self) -> None:
        with mock.patch.object(
                doctor, "probe",
                return_value=_routine_probe_snapshot(live=False)):
            payload = doctor._json_report(deep=False)
        self.assertNotIn("semantic", payload["tiers"])
        self.assertEqual(payload["tiers_unknown"], [])


def _routine_probe_snapshot(*, live) -> dict:
    return {
        "core": {"live": True, "rust": True, "binary": True},
        "semantic": {**_missing_semantic(), "live": live},
        "settings": {},
        "detected": [],
    }


class StoreCensusBudgetFitsAHealthyCensus(unittest.TestCase):
    """A 0.20s cap starved the ~0.3s census a real 29k-file box needs, so
    doctor permanently reported budget-exceeded for a check that succeeds
    given 100 more ms (hunt 3 finding 2 / G1)."""

    def _census_timeout(self, *, deep: bool, deadline_s: float = 10.0) -> float:
        seen: dict[str, float] = {}

        def observe(*, timeout_s: float):
            seen["timeout_s"] = timeout_s
            return [], indexd_runtime.DriftReport("current")

        with (
            mock.patch.object(
                doctor, "_semantic_probe", return_value=_missing_semantic()),
            mock.patch.object(
                doctor.indexd_runtime, "observe_store_drift",
                side_effect=observe),
            mock.patch.object(
                doctor.indexd_runtime, "arm_store_census") as armed,
        ):
            doctor.probe(
                deep=deep,
                routine_deadline=(
                    None if deep else time.monotonic() + deadline_s))
        armed.assert_called_once()
        return seen["timeout_s"]

    def test_routine_census_budget_covers_the_measured_cost(self) -> None:
        timeout_s = self._census_timeout(deep=False)
        self.assertGreater(timeout_s, 0.30)
        self.assertLessEqual(timeout_s, doctor._DIAGNOSTIC_STORE_TIMEOUT_S)

    def test_deep_census_is_exempt_from_the_routine_cap(self) -> None:
        timeout_s = self._census_timeout(deep=True)
        self.assertEqual(timeout_s, doctor._DIAGNOSTIC_STORE_DEEP_TIMEOUT_S)
        self.assertGreaterEqual(timeout_s, 1.0)


class ASupersededEmbedFailureStopsSpeaking(unittest.TestCase):
    """A persisted failure record outlived its bug: passes succeeded (or the
    lane published completely) and doctor still rendered 'last build failed'.
    Newer evidence must silence the record (hunt 3 finding 3)."""

    def test_a_complete_lane_contradicts_the_record(self) -> None:
        record = {"state": "failed", "finished_at": time.time(),
                  "error": "AttributeError: 'int' object has no attribute 'get'"}
        self.assertTrue(semantic.embed_failure_superseded(
            record, {"coherent": True, "migration_pending": False}))
        self.assertFalse(semantic.embed_failure_superseded(
            record, {"coherent": False}))
        self.assertFalse(semantic.embed_failure_superseded(
            record, {"coherent": True, "migration_pending": True}))

    def test_an_unrefreshed_old_record_ages_out(self) -> None:
        old = time.time() - 2 * semantic.BOOTSTRAP_RETRY_S - 60
        self.assertTrue(semantic.stale_embed_failure(
            {"state": "failed", "finished_at": old}))
        self.assertFalse(semantic.stale_embed_failure(
            {"state": "failed", "finished_at": time.time()}))
        self.assertFalse(semantic.stale_embed_failure(
            {"state": "running", "started_at": old}))
        self.assertFalse(semantic.stale_embed_failure({"state": "failed"}))

    def test_embeddings_off_makes_a_stale_failure_non_actionable(self) -> None:
        failed = {"embed_job": "failed"}
        self.assertFalse(doctor._semantic_failure_visible(
            failed, embeddings_off=True))
        self.assertTrue(doctor._semantic_failure_visible(
            failed, embeddings_off=False))

    def test_routine_doctor_drops_an_aged_failure_record(self) -> None:
        old = time.time() - 2 * semantic.BOOTSTRAP_RETRY_S - 60
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-aged-") as td:
            root = Path(td)
            (root / ".semantic-embed-state.json").write_text(json.dumps({
                "state": "failed", "finished_at": old,
                "error": "AttributeError: gone since fixed",
            }), encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(semantic.common, "DATA_DIR", root),
            ):
                observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertEqual(observed["embed_job"], "not-inspected")
        self.assertIsNone(observed["embed_fail_reason"])

    def test_routine_doctor_still_reports_a_fresh_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-fresh-") as td:
            root = Path(td)
            (root / ".semantic-embed-state.json").write_text(json.dumps({
                "state": "failed", "finished_at": time.time(),
                "error": "onnx session init failed",
            }), encoding="utf-8")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(semantic.common, "DATA_DIR", root),
            ):
                observed = doctor._semantic_probe(deep=False, fix=False)
        self.assertEqual(observed["embed_job"], "failed")
        self.assertEqual(
            observed["embed_fail_reason"], "onnx session init failed")

    def test_a_coherent_refresh_check_clears_the_record_on_disk(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-clear-") as td:
            root = Path(td)
            state = root / ".semantic-embed-state.json"
            state.write_text(json.dumps({
                "state": "failed", "finished_at": time.time(),
                "error": "AttributeError: fixed since",
            }), encoding="utf-8")
            with mock.patch.object(semantic.common, "DATA_DIR", root):
                semantic.clear_superseded_embed_failure({"coherent": False})
                self.assertTrue(state.exists())
                semantic.clear_superseded_embed_failure(
                    {"coherent": True, "migration_pending": False})
                self.assertFalse(state.exists())

    def test_clearing_never_touches_a_non_failed_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-doctor-keep-") as td:
            root = Path(td)
            state = root / ".semantic-embed-state.json"
            state.write_text(json.dumps({
                "state": "running", "started_at": time.time(),
            }), encoding="utf-8")
            with mock.patch.object(semantic.common, "DATA_DIR", root):
                semantic.clear_superseded_embed_failure(
                    {"coherent": True, "migration_pending": False})
            self.assertTrue(state.exists())


if __name__ == "__main__":
    unittest.main()
