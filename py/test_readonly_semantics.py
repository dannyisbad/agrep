from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import ask
import common
import corpusdb
import embedding_store
import indexd_runtime
import segment_query
import semantic
import semworker


def _census(root: Path) -> dict:
    def metadata(path: Path) -> tuple[int, int, int, int]:
        observed = path.lstat()
        return (
            stat.S_IFMT(observed.st_mode) | stat.S_IMODE(observed.st_mode),
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )

    entries = {}
    for path in sorted(root.rglob("*"), key=lambda value: os.fspath(value)):
        relative = path.relative_to(root).as_posix()
        record = metadata(path)
        entries[relative] = (
            record,
            path.read_bytes() if stat.S_ISREG(path.lstat().st_mode) else None,
        )
    return {"root": metadata(root), "entries": entries}


def _refs_identity() -> str:
    return json.dumps({
        "schema": ask._MESSAGE_REFS_SCHEMA,
        "embedding": {"bundle": "fixture", "ids_sha256": "fixture"},
        "source": {"version": 1, "fixture": "read-only"},
        "messages": [1, 2, 3, 4],
        "replies": None,
        "hashes": [17, 18, 19, 20],
    }, sort_keys=True, separators=(",", ":"))


def _write_refs_db(path: Path, identity: str) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE refs(
                ord INTEGER PRIMARY KEY, mid TEXT NOT NULL,
                agent TEXT, session TEXT, project TEXT, model TEXT,
                who TEXT, side INTEGER NOT NULL CHECK(side IN (0, 1)),
                turn, ts INTEGER, valid INTEGER NOT NULL,
                row_seal TEXT NOT NULL);
            CREATE TABLE texts(
                ord INTEGER PRIMARY KEY, source_kind INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL,
                text_hash TEXT NOT NULL);
            CREATE TRIGGER refs_dirty_insert AFTER INSERT ON refs BEGIN
                SELECT 1; END;
            CREATE TRIGGER refs_dirty_update AFTER UPDATE ON refs BEGIN
                SELECT 1; END;
            CREATE TRIGGER refs_dirty_delete AFTER DELETE ON refs BEGIN
                SELECT 1; END;
            CREATE TRIGGER texts_dirty_insert AFTER INSERT ON texts BEGIN
                SELECT 1; END;
            CREATE TRIGGER texts_dirty_update AFTER UPDATE ON texts BEGIN
                SELECT 1; END;
            CREATE TRIGGER texts_dirty_delete AFTER DELETE ON texts BEGIN
                SELECT 1; END;
        """)
        db.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?)",
            (("identity", identity), ("rows", "1"), ("sealed", "1")),
        )
        db.execute(
            "INSERT INTO refs VALUES(0,'m1','','s','','','user',0,0,1,1,'seal')")
        db.execute("INSERT INTO texts VALUES(0,0,0,1,'0000000000000000')")
        db.commit()
    finally:
        db.close()


class ReadOnlySemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        ask.clear_artifact_cache()
        segment_query.close_cache()
        semantic._FULL_REBUILD_LOCAL.clear()

    def tearDown(self) -> None:
        ask.clear_artifact_cache()
        segment_query.close_cache()
        semantic._FULL_REBUILD_LOCAL.clear()

    def test_semantic_writer_entrypoints_leave_exact_protected_census_unchanged(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semantic-readonly-") as raw:
            root = Path(raw)
            (root / "keep.bin").write_bytes(b"protected bytes")
            marker = root / ".semantic-full-rebuild"
            marker.write_bytes(b"existing request")
            before = _census(root)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    semantic, "embedding_coherence",
                    side_effect=AssertionError("protected coherence writer path ran")),
                mock.patch.object(common, "open_bounded_log") as log,
                mock.patch.object(semantic.subprocess, "Popen") as spawn,
            ):
                self.assertTrue(semantic._data_dir_readonly())
                self.assertFalse(semantic._mark_integrity_rebuild())
                self.assertTrue(semantic.integrity_rebuild_requested())
                repair = semantic.request_full_rebuild("integrity failure")
                self.assertEqual(repair["state"], "recorded")
                self.assertFalse(repair["persistent"])
                self.assertEqual(
                    semantic.ensure_fresh_async()["state"], "read-only")
                self.assertEqual(
                    semantic.ensure_refs_async()["state"], "read-only")
                self.assertEqual(
                    semantic.refresh_embeddings_sync()["state"], "read-only")
                semantic.note_semantic_use()
                semantic.clear_integrity_rebuild_request()
                with self.assertRaisesRegex(OSError, "AGREP_DATA_READONLY"):
                    semantic.write_generation_marker({"fixture": True})
                self.assertEqual(
                    semantic.stop_background_writers_for_removal()["state"],
                    "read-only")
            self.assertEqual(_census(root), before)
            log.assert_not_called()
            spawn.assert_not_called()

    def test_protection_matches_only_the_exact_data_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semantic-boundary-") as raw:
            parent = Path(raw)
            protected = parent / "protected"
            writable = parent / "sandbox"
            protected.mkdir()
            writable.mkdir()
            meta = writable / "embeddings.meta"
            meta.write_bytes(b"fixture")
            with (
                mock.patch.object(common, "DATA_DIR", writable),
                mock.patch.object(
                    indexd_runtime, "DERIVED_OWNER_PATH",
                    writable / ".derived-owner.json"),
                mock.patch.object(
                    indexd_runtime, "INGEST_CACHE_PATH",
                    writable / ".ingest_cache.bin"),
                mock.patch.object(corpusdb, "DB_PATH", writable / "corpus.db"),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(protected)},
                    clear=False),
            ):
                self.assertFalse(semantic._data_dir_readonly())
                self.assertFalse(segment_query._data_dir_readonly())
                self.assertFalse(ask._data_dir_readonly())
                self.assertFalse(semworker._data_dir_readonly())
                semantic.note_semantic_use()
                segment_query._write_integrity_receipt(
                    meta, {"fixture": {"sha256": "0", "identity": [1]}})
            self.assertTrue((writable / ".semantic-use.beat").exists())
            self.assertTrue(
                segment_query._integrity_receipt_path(meta).exists())
            self.assertEqual(list(protected.iterdir()), [])

    def test_segment_integrity_verification_cannot_publish_a_protected_receipt(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-segment-readonly-") as raw:
            root = Path(raw)
            meta = root / "embeddings.meta"
            meta.write_bytes(b"immutable manifest")
            before = _census(root)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
            ):
                self.assertTrue(segment_query._data_dir_readonly())
                segment_query._write_integrity_receipt(
                    meta, {"artifact": {"sha256": "a" * 64, "identity": [1]}})
            self.assertEqual(_census(root), before)
            self.assertFalse(
                segment_query._integrity_receipt_path(meta).exists())

    def test_ask_reads_valid_refs_but_never_publishes_or_snapshots_in_place(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-refs-readonly-") as raw:
            root = Path(raw)
            identity = _refs_identity()
            path = root / (
                ask._message_refs_prefix(identity) + "-published.db")
            _write_refs_db(path, identity)
            hashes_path = root / "embeddings.hashes"
            hashes_path.write_bytes(b"0000000000000000\n")
            before = _census(root)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    ask, "_message_refs_identity", return_value=identity),
                mock.patch.object(
                    ask, "_stable_message_hash_blob",
                    return_value=b"0000000000000000\n"),
            ):
                refs = ask._message_refs(
                    ["m1"], coverage={"indexed": 1}, allow_build=True)
                self.assertEqual(refs.rows, 1)
                ask._close_message_refs()
                with (
                    mock.patch.object(common, "WIN", True),
                    mock.patch.object(
                        common, "_prune_embedding_snapshots",
                        side_effect=AssertionError(
                            "protected hash aliases were pruned")) as prune,
                    mock.patch.object(
                        ask.os, "link",
                        side_effect=AssertionError(
                            "protected hash alias was hardlinked")) as link,
                ):
                    blob = ask._MappedHashBlob(hashes_path, 1)
                    snapshot = Path(blob.file.name)
                    try:
                        self.assertTrue(blob.snapshot_external)
                        self.assertEqual(blob.snapshot, snapshot)
                        self.assertNotEqual(snapshot, hashes_path)
                        self.assertNotEqual(snapshot.parent, root)
                        self.assertTrue(snapshot.exists())
                        self.assertEqual(blob.at(0), "0000000000000000")
                        self.assertEqual(_census(root), before)
                    finally:
                        blob.close()
                    self.assertFalse(snapshot.exists())
                prune.assert_not_called()
                link.assert_not_called()
            self.assertEqual(_census(root), before)
            self.assertFalse((root / "embeddings.refs.meta").exists())
            self.assertFalse(any(
                path.name.startswith(".embeddings.refs-")
                for path in root.iterdir()))

    def test_windows_matrix_reader_maps_a_coherent_system_temp_copy(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-matrix-readonly-") as raw:
            root = Path(raw)
            embeddings = root / "embeddings.f32"
            ids_path = root / "embeddings.ids"
            meta_path = root / "embeddings.meta"
            expected = np.asarray(
                [[1.0, 0.0], [0.0, 1.0]], dtype="<f4")
            common.write_embeddings(
                ["m1", "m2"], expected,
                embeddings_path=embeddings,
                ids_path=ids_path,
                dim=2,
                model_id="fixture",
                text_hashes=["a" * 16, "b" * 16],
            )
            before = _census(root)
            mapped = None
            snapshot = None
            with (
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(embedding_store, "WIN", True),
                mock.patch.object(
                    embedding_store, "_prune_embedding_snapshots",
                    side_effect=AssertionError(
                        "protected matrix aliases were pruned")) as prune,
                mock.patch.object(
                    embedding_store.os, "link",
                    side_effect=AssertionError(
                        "protected matrix alias was hardlinked")) as link,
            ):
                try:
                    loaded_ids, mapped = embedding_store.read_embeddings(
                        embeddings, ids_path, dim=2, meta_path=meta_path,
                        attempts=1)
                    snapshot = Path(mapped._agrep_snapshot_path)
                    self.assertEqual(loaded_ids, ["m1", "m2"])
                    np.testing.assert_array_equal(mapped, expected)
                    self.assertTrue(mapped._agrep_snapshot_external)
                    self.assertFalse(mapped._agrep_snapshot_hardlink)
                    self.assertEqual(
                        Path(mapped.filename).resolve(), snapshot.resolve())
                    self.assertNotEqual(snapshot, embeddings)
                    self.assertNotEqual(snapshot.parent, root)
                    self.assertTrue(snapshot.exists())
                    self.assertEqual(_census(root), before)
                finally:
                    if mapped is not None:
                        embedding_store.close_embedding_matrix(mapped)
                self.assertIsNotNone(snapshot)
                self.assertFalse(snapshot.exists())
            prune.assert_not_called()
            link.assert_not_called()
            self.assertEqual(_census(root), before)

    def test_ask_absent_and_corrupt_refs_refuse_without_quarantine_or_build(
            self) -> None:
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory(
                    prefix="agrep-refs-unavailable-") as raw:
                root = Path(raw)
                identity = _refs_identity()
                if corrupt:
                    path = root / (
                        ask._message_refs_prefix(identity) + "-corrupt.db")
                    path.write_bytes(b"not sqlite")
                else:
                    (root / "keep.bin").write_bytes(b"no refs installed")
                before = _census(root)
                with (
                    mock.patch.object(common, "DATA_DIR", root),
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                        clear=False),
                    mock.patch.object(
                        ask, "_message_refs_identity", return_value=identity),
                    self.assertRaises(ask.MessageRefsUnavailable),
                ):
                    ask._message_refs(
                        ["m1"], coverage={"indexed": 1}, allow_build=True)
                self.assertEqual(_census(root), before)
                self.assertFalse(any(
                    path.name.endswith(".invalid") for path in root.iterdir()))

    def test_semantic_error_paths_fall_back_without_repair_side_effects(
            self) -> None:
        import embedder

        with tempfile.TemporaryDirectory(prefix="agrep-semantic-errors-") as raw:
            root = Path(raw)
            corrupt = root / "embeddings.refs-corrupt.db"
            corrupt.write_bytes(b"retain corrupt generation")
            before = _census(root)
            coherence = {
                "coherent": True,
                "searchable": True,
                "state": "current",
                "coverage": {
                    "indexed": 1, "total": 1, "pending": 0,
                    "complete": True,
                },
            }
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    semantic, "embedding_coherence", return_value=coherence),
                mock.patch.object(embedder, "get", return_value=object()),
                mock.patch.object(common, "open_bounded_log") as log,
                mock.patch.object(semantic.subprocess, "Popen") as spawn,
            ):
                ask._MESSAGE_REFS["path"] = corrupt
                with (
                    mock.patch.object(
                        ask, "tool_search_hybrid",
                        side_effect=ask.CorruptMessageRefs("broken refs")),
                    self.assertRaisesRegex(
                        semantic.SemanticUnavailable, "full rebuild recorded"),
                ):
                    semantic.search("fixture")
                with mock.patch.object(
                    ask, "tool_search_hybrid",
                    side_effect=segment_query.SegmentIntegrityError(
                        "artifact digest mismatch"),
                ):
                    payload = semantic.search("fixture")
                self.assertEqual(
                    payload["semantic_integrity"]["repair_state"], "recorded")
                self.assertFalse(
                    payload["semantic_integrity"]["repair_persistent"])
                with (
                    mock.patch.object(
                        ask, "tool_search_hybrid",
                        side_effect=ask.MessageRefsUnavailable("refs absent")),
                    self.assertRaisesRegex(
                        semantic.SemanticUnavailable, "refs read-only"),
                ):
                    semantic.search("fixture")
            self.assertEqual(_census(root), before)
            self.assertEqual(corrupt.read_bytes(), b"retain corrupt generation")
            log.assert_not_called()
            spawn.assert_not_called()

    def test_diagnostic_integrity_refusal_never_claims_a_rebuild(self) -> None:
        import embedder

        coverage = {
            "indexed": 1, "total": 1, "pending": 0, "complete": True,
        }
        corrupt = {
            "coherent": False, "searchable": False,
            "state": "corrupt-embeddings", "coverage": coverage,
            "reason": "fixture generation damage",
        }
        with mock.patch.object(
                semantic, "embedding_coherence", return_value=corrupt), \
                mock.patch.object(
                    semantic, "request_full_rebuild",
                    side_effect=AssertionError("diagnostic requested repair")):
            payload = semantic.search("fixture", diagnostic_only=True)
        integrity = payload["semantic_integrity"]
        self.assertEqual(integrity["repair"], "not-requested")
        self.assertEqual(integrity["repair_state"], "not-requested")
        self.assertFalse(integrity["repair_persistent"])

        current = {
            "coherent": True, "searchable": True,
            "state": "current", "coverage": coverage,
        }
        with mock.patch.object(
                semantic, "embedding_coherence", return_value=current), \
                mock.patch.object(embedder, "get", return_value=object()), \
                mock.patch.object(
                    ask, "tool_search_hybrid",
                    side_effect=segment_query.SegmentIntegrityError(
                        "fixture segment damage")), \
                mock.patch.object(
                    semantic, "request_full_rebuild",
                    side_effect=AssertionError("diagnostic requested repair")):
            payload = semantic.search("fixture", diagnostic_only=True)
        integrity = payload["semantic_integrity"]
        self.assertEqual(integrity["repair"], "not-requested")
        self.assertEqual(integrity["repair_state"], "not-requested")
        self.assertFalse(integrity["repair_persistent"])

    def test_worker_protection_preserves_stale_descriptor_and_uses_local_lease(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semworker-readonly-") as raw:
            root = Path(raw)
            descriptor = root / ".semantic-worker.json"
            descriptor.write_bytes(b"{stale descriptor")
            old = time.time() - semworker.START_CLAIM_GRACE_S - 30.0
            os.utime(descriptor, (old, old))
            before = _census(root)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    semworker, "_discard_record",
                    side_effect=AssertionError("durable descriptor mutated")) as discard,
                mock.patch.object(
                    semworker, "_acquire_start_claim",
                    side_effect=AssertionError("start claim acquired")) as claim,
                mock.patch.object(
                    semworker, "_spawn_worker",
                    side_effect=AssertionError("worker spawned")) as spawn_worker,
                mock.patch.object(common, "open_bounded_log") as log,
                mock.patch.object(semworker.subprocess, "Popen") as popen,
            ):
                self.assertTrue(semworker._worker_disabled())
                self.assertIsNone(semworker.search_worker(
                    "fixture", level="hybrid", k=1))
                self.assertIsNone(semworker.acquire_resident_owner())
                self.assertEqual(semworker.serve_main(), 0)
                with (
                    mock.patch.object(semworker, "_BoundedHTTPServer") as server,
                    self.assertRaisesRegex(
                        semworker.ownerfile.OwnershipLost,
                        "semantic worker startup is disabled"),
                ):
                    semworker.SemanticWorkerServer(lifetime=mock.Mock())
                server.assert_not_called()
                owner = semworker.acquire_inprocess_owner()
                self.assertIsInstance(owner, semworker.ownerfile.Handle)
                self.assertFalse(
                    semworker.worker_lock_path().is_relative_to(root))
                semworker.verify_inprocess_owner(owner)
                semworker.finish_inprocess_owner(
                    owner, resources_released=True)
                self.assertEqual(
                    semworker.stop_worker_and_wait()["owner_state"], "read-only")
            self.assertEqual(_census(root), before)
            discard.assert_not_called()
            claim.assert_not_called()
            spawn_worker.assert_not_called()
            log.assert_not_called()
            popen.assert_not_called()

    def test_an_unrecordable_rebuild_does_not_latch_a_corrupt_verdict(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semantic-latch-") as raw:
            root = Path(raw)
            before = _census(root)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(semantic.subprocess, "Popen") as spawn,
            ):
                repair = semantic.request_full_rebuild("a transient race")
                self.assertFalse(repair["persistent"])
                self.assertFalse(semantic.integrity_rebuild_path().exists())
                # No marker was published, so nothing may claim one is pending.
                self.assertFalse(semantic.integrity_rebuild_requested())
                self.assertEqual(semantic._FULL_REBUILD_LOCAL, set())
            self.assertEqual(_census(root), before)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
