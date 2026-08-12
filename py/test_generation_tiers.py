"""Routine tiers use bounded alias clones and deferred deep proofs.

Two corpus-scale costs lived on routine paths: a WAL/open failure could send
`_connect_read_snapshot` through a corpus-scale byte copy, and a Windows
ContentSha256 proof without a USN made `search_generation_health` hash the
complete artifact set. Routine callers now try a filesystem clone before they
bound the byte-copy fallback and defer the content proof honestly; `agrep
doctor --deep` semantics stay complete, and a deferral can never overwrite a
real recorded freshness failure.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
from test_search_freshness import _family_meta, _publish  # noqa: E402


def _sqlite_family(root: Path) -> Path:
    path = root / "corpus.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES('build_id', 'fixture')")
    db.commit()
    db.close()
    return path


class AliasCloneBound(unittest.TestCase):
    def test_alias_attempt_precedes_logical_size_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _sqlite_family(Path(raw))
            copied = []

            def clone_without_charge(source, target, *, max_copy_bytes=None):
                source.seek(0)
                target.write_bytes(source.read())
                copied.append(max_copy_bytes)
                return 0

            with mock.patch.object(
                    corpusdb, "_copy_sqlite_file",
                    side_effect=clone_without_charge):
                db = corpusdb._connect_read_alias(path, 0, 16)
            try:
                self.assertEqual(
                    db.execute("SELECT value FROM meta").fetchone(),
                    ("fixture",))
            finally:
                db.close()
            self.assertEqual(copied, [16])

    def test_clone_success_is_not_charged_to_byte_copy_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.db"
            target = root / "target.db"
            source_path.write_bytes(b"x" * 64)
            with source_path.open("rb") as source, mock.patch.object(
                    corpusdb, "_try_clone_sqlite_file", return_value=True):
                charged = corpusdb._copy_sqlite_file(
                    source, target, max_copy_bytes=1)
        self.assertEqual(charged, 0)

    def test_byte_copy_fallback_refuses_before_creating_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.db"
            target = root / "target.db"
            source_path.write_bytes(b"x" * 64)
            with source_path.open("rb") as source, mock.patch.object(
                    corpusdb, "_try_clone_sqlite_file", return_value=False):
                with self.assertRaises(corpusdb.AliasCloneRefused):
                    corpusdb._copy_sqlite_file(
                        source, target, max_copy_bytes=16)
            self.assertFalse(target.exists())

    def test_live_rollback_journal_reads_committed_snapshot_without_alias(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _sqlite_family(Path(raw))
            committed = sqlite3.connect(path)
            committed.execute(
                "INSERT INTO meta VALUES('schema', ?)", (corpusdb._SCHEMA,))
            committed.commit()
            committed.close()

            writer = sqlite3.connect(path)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE meta SET value = 'uncommitted' "
                "WHERE key = 'build_id'")
            journal = Path(f"{path}-journal")
            self.assertTrue(journal.exists())
            self.assertGreater(journal.stat().st_size, 0)
            try:
                with mock.patch.object(corpusdb, "DB_PATH", path):
                    snapshot = corpusdb._foreign_stale_db(0)
                self.assertIsNotNone(snapshot)
                try:
                    self.assertEqual(
                        snapshot.execute(
                            "SELECT value FROM meta WHERE key = 'build_id'"
                        ).fetchone(),
                        ("fixture",))
                    self.assertNotIsInstance(
                        snapshot, corpusdb._AliasedConnection)
                finally:
                    snapshot.close()
                # The source transaction and its journal are still untouched.
                self.assertEqual(
                    writer.execute(
                        "SELECT value FROM meta WHERE key = 'build_id'"
                    ).fetchone(),
                    ("uncommitted",))
                self.assertTrue(journal.exists())
            finally:
                writer.rollback()
                writer.close()

    def test_foreign_snapshot_rejects_a_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _sqlite_family(Path(raw))
            db = sqlite3.connect(path)
            db.execute("INSERT INTO meta VALUES('schema', 'obsolete')")
            db.commit()
            db.close()
            with mock.patch.object(corpusdb, "DB_PATH", path):
                snapshot = corpusdb._foreign_stale_db(0)
        self.assertIsNone(snapshot)

    def test_bounded_snapshot_refuses_where_it_would_have_cloned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _sqlite_family(Path(raw))
            with mock.patch.object(
                    corpusdb, "_open", side_effect=OSError("wal")), \
                    mock.patch.object(
                        corpusdb, "_try_clone_sqlite_file",
                        return_value=False):
                with self.assertRaises(corpusdb.AliasCloneRefused):
                    corpusdb._connect_read_snapshot(
                        path, 0, max_clone_bytes=16)

    def test_unbounded_default_keeps_the_foreign_reader_path(self) -> None:
        # Owned-by proofs read through this exact fallback; the
        # bound is opt-in, never a deletion of the alias path.
        with tempfile.TemporaryDirectory() as raw:
            path = _sqlite_family(Path(raw))
            with mock.patch.object(
                    corpusdb, "_open", side_effect=OSError("wal")):
                db = corpusdb._connect_read_snapshot(path, 0)
            try:
                rows = db.execute("SELECT value FROM meta").fetchall()
            finally:
                db.close()
        self.assertEqual(rows, [("fixture",)])

    def test_routine_health_maps_the_refusal_to_generation_unavailable(
            self) -> None:
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
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        corpusdb, "_ROUTINE_ALIAS_CLONE_MAX_BYTES", 1), \
                    mock.patch.object(
                        corpusdb, "_open", side_effect=OSError("alias needed")), \
                    mock.patch.object(
                        corpusdb, "_try_clone_sqlite_file",
                        return_value=False):
                routine = corpusdb.search_generation_health(routine=True)
                deep = corpusdb.search_generation_health()
        self.assertEqual(routine["state"], "generation-unavailable")
        # The deep tier never consults the routine bound: same fixture, same
        # patched constant, and it still reads through to the stamp compare.
        self.assertEqual(deep["state"], "search-index-stale")


def _publish_content_proofs(root: Path) -> None:
    """A committed generation whose proof rows carry ContentSha256 tokens -
    the Windows no-USN shape that costs a complete content hash to verify."""
    signature = "0:fixture-generation"
    bodies = {
        "messages.jsonl": b"m\n",
        "replies.jsonl": b"r\n",
        "sessions.jsonl": b"s\n",
        common.SESSION_FAMILY_META_FILE: _family_meta(signature),
        "boundary_stats.json": b"{}",
        ".boundary_stats.bin": b"fixture",
        "event_stats.json": b"{}",
    }
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, body in bodies.items():
        path = root / name
        path.write_bytes(body)
        identity = corpusdb._proof_file_identity(path)
        rows.append({
            "name": name,
            "len": identity[0],
            "modified_ns": identity[1],
            "change_token": {
                "ContentSha256": list(hashlib.sha256(body).digest())},
            "edge_hash": corpusdb._edge_hash(path, identity[0]),
        })
    signal = root / ".ingest.sig"
    signal.write_text(signature, encoding="utf-8")
    old = time.time() - 3.0
    os.utime(signal, (old, old))
    (root / ".derived_generation.json").write_text(
        json.dumps({
            "version": corpusdb._DERIVED_PROOF_VERSION,
            "signature": signature,
            "files": rows,
        }, separators=(",", ":")), encoding="utf-8")


class RoutineGenerationTier(unittest.TestCase):
    def _health(self, *, routine: bool, content_hash=None) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish_content_proofs(root)
            patches = [
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"),
                mock.patch.object(
                    corpusdb, "_windows_file_state",
                    lambda path, include_usn: ("basic", 0)),
            ]
            if content_hash is not None:
                patches.append(mock.patch.object(
                    corpusdb, "_content_sha256", side_effect=content_hash))
            with ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                return corpusdb._derived_publication_health(routine=routine)

    def test_routine_tier_defers_instead_of_hashing_the_artifact_set(
            self) -> None:
        def refuse(*_args, **_kwargs):
            raise AssertionError("routine tier hashed content")
        health = self._health(routine=True, content_hash=refuse)
        self.assertEqual(
            health["state"], corpusdb.GENERATION_VERIFICATION_DEFERRED)
        self.assertIn("doctor --deep", health["detail"])
        self.assertIsNotNone(health["corpus_age_s"])

    def test_deep_tier_still_verifies_the_complete_content(self) -> None:
        health = self._health(routine=False)
        self.assertEqual(health["state"], "ready")

    def test_the_default_tier_is_deep_so_doctor_keeps_its_semantics(
            self) -> None:
        signature = inspect.signature(corpusdb.search_generation_health)
        self.assertFalse(signature.parameters["routine"].default)
        publication = inspect.signature(corpusdb._derived_publication_health)
        self.assertFalse(publication.parameters["routine"].default)

    def test_deferral_never_overwrites_a_recorded_failure(self) -> None:
        deferred = {
            "state": corpusdb.GENERATION_VERIFICATION_DEFERRED,
            "detail": "deferred", "corpus_age_s": 4.2}
        recorded = {
            "state": "degraded", "failing": True, "may_be_stale": True,
            "code": "consecutive-failures", "consecutive_failures": 44}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=deferred):
            fields = corpusdb.machine_freshness_fields(dict(recorded))
        self.assertEqual(fields["freshness"], recorded)
        self.assertEqual(fields["corpus_age_s"], 4.2)

    def test_deferral_never_invents_a_failure_on_a_clean_box(self) -> None:
        deferred = {
            "state": corpusdb.GENERATION_VERIFICATION_DEFERRED,
            "detail": "deferred", "corpus_age_s": 4.2}
        clean = {"state": "no-known-failure", "failing": False,
                 "checked": True}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=deferred):
            fields = corpusdb.machine_freshness_fields(dict(clean))
        self.assertEqual(fields["freshness"], clean)


class PublicationTransitionDisclosure(unittest.TestCase):
    clean = {"state": "no-known-failure", "failing": False,
             "checked": True}

    def test_query_publication_requires_an_exact_live_owner(self) -> None:
        for live in (False, True):
            with self.subTest(live=live), mock.patch.object(
                    corpusdb, "_live_refresh_lock",
                    return_value=live) as owner:
                self.assertEqual(corpusdb.query_publication_active(), live)
            owner.assert_called_once_with()

    def test_query_publication_observes_a_real_exact_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / ".index.lock"
            with mock.patch.object(common, "INDEX_LOCK_PATH", lock_path), \
                    mock.patch.object(
                        corpusdb.index_lock, "INDEX_LOCK_PATH", lock_path):
                self.assertFalse(corpusdb.query_publication_active())
                with common.IndexLock("publication-fixture", timeout=0.1):
                    self.assertTrue(corpusdb.query_publication_active())
                self.assertFalse(corpusdb.query_publication_active())

    def test_live_torn_publication_is_nonfailing_work_in_progress(self) -> None:
        torn = {"state": "torn-generation", "detail": "messages moved",
                "corpus_age_s": None}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=torn), \
                mock.patch.object(
                    corpusdb, "_live_refresh_lock", return_value=True) as live:
            fields = corpusdb.machine_freshness_fields(
                dict(self.clean), publication_converging=True)
        live.assert_called_once_with()
        self.assertEqual(fields["freshness"], {
            "state": "index-behind", "failing": False,
            "may_be_stale": True, "code": "index-behind",
            "cause": "publication-in-progress", "checked": True,
        })

    def test_static_torn_generation_stays_a_failure(self) -> None:
        torn = {"state": "torn-generation", "detail": "messages moved",
                "corpus_age_s": None}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=torn) as health, \
                mock.patch.object(
                    corpusdb, "_live_refresh_lock", return_value=False):
            fields = corpusdb.machine_freshness_fields(
                dict(self.clean), publication_converging=True)
        self.assertEqual(health.call_count, 2)
        self.assertEqual(fields["freshness"]["state"], "degraded")
        self.assertEqual(fields["freshness"]["code"], "torn-generation")

    def test_generation_movement_under_verified_owner_is_nonfailing(
            self) -> None:
        moving = {"state": "generation-moving", "detail": "proof moved",
                  "corpus_age_s": 0.1}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=moving), \
                mock.patch.object(corpusdb, "_live_refresh_lock") as live:
            fields = corpusdb.machine_freshness_fields(
                dict(self.clean), publication_converging=True)
        live.assert_not_called()
        self.assertEqual(
            fields["freshness"]["cause"], "publication-in-progress")
        self.assertFalse(fields["freshness"]["failing"])

    def test_verified_owner_does_not_hide_other_integrity_failures(self) -> None:
        unavailable = {
            "state": "generation-unavailable", "detail": "cannot inspect",
            "corpus_age_s": None}
        with mock.patch.object(
                corpusdb, "search_generation_health",
                return_value=unavailable):
            fields = corpusdb.machine_freshness_fields(
                dict(self.clean), publication_converging=True)
        self.assertEqual(fields["freshness"]["state"], "degraded")
        self.assertEqual(
            fields["freshness"]["code"], "generation-unavailable")

    def test_recorded_failure_outranks_publication_movement(self) -> None:
        moving = {"state": "generation-moving", "detail": "proof moved",
                  "corpus_age_s": 0.1}
        recorded = {
            "state": "degraded", "failing": True, "may_be_stale": True,
            "code": "consecutive-failures", "consecutive_failures": 3,
        }
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=moving):
            fields = corpusdb.machine_freshness_fields(
                dict(recorded), publication_converging=True)
        self.assertEqual(fields["freshness"], recorded)

    def test_unchecked_request_cannot_claim_owned_publication(self) -> None:
        moving = {"state": "generation-moving", "detail": "proof moved",
                  "corpus_age_s": 0.1}
        unchecked = {"state": "unchecked", "failing": False,
                     "checked": False, "may_be_stale": True}
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=moving):
            fields = corpusdb.machine_freshness_fields(
                dict(unchecked), publication_converging=True)
        self.assertEqual(fields["freshness"]["state"], "degraded")
        self.assertEqual(fields["freshness"]["code"], "generation-moving")

    def test_known_store_drift_remains_the_stronger_nonfailure(self) -> None:
        moving = {"state": "generation-moving", "detail": "proof moved",
                  "corpus_age_s": 0.1}
        behind = {
            "state": "index-behind", "failing": False,
            "may_be_stale": True, "code": "index-behind",
            "cause": "store-drift", "changed_stores": 2, "checked": True,
        }
        with mock.patch.object(
                corpusdb, "search_generation_health", return_value=moving):
            fields = corpusdb.machine_freshness_fields(
                dict(behind), publication_converging=True)
        self.assertEqual(fields["freshness"], behind)


if __name__ == "__main__":
    unittest.main()
