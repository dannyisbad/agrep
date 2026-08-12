from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import ask
import common
import conceptpair
import corpusdb
import embed
import embedder
import embedding_segments
import embedding_store
import indexd_runtime
import ownerfile
import semantic
import semantic_q8
import semantic_segment_build
import semantic_segment_compact
import semworker
import segment_query
import test_segment_query


BUILD_A = "aaaaaaaaaaaaaaaaaaaa"
BUILD_B = "bbbbbbbbbbbbbbbbbbbb"


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
        observed = path.lstat()
        entries[relative] = (
            metadata(path),
            path.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
        )
    return {"root": metadata(root), "entries": entries}


class SemanticWriterEntrypointTests(unittest.TestCase):
    def _assert_exact_refusal(self, root: Path, label: str, call) -> None:
        before = _census(root)
        with self.subTest(entrypoint=label):
            call()
            self.assertEqual(_census(root), before)

    def test_embed_writer_entrypoints_refuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-embed-readonly-") as raw:
            root = Path(raw)
            (root / "keep.bin").write_bytes(b"writer-owned bytes")
            (root / "embeddings.pending.db").write_bytes(b"pending")
            (root / ".semantic-embed.lock").write_bytes(b"stale owner")
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(embed, "_CLAIM_HANDLE", None),
                mock.patch.object(common, "log"),
            ):
                self._assert_exact_refusal(
                    root, "main",
                    lambda: self.assertEqual(
                        self._embed_main(["embed.py", "--full"]), 1))
                self._assert_exact_refusal(
                    root, "claim",
                    lambda: self.assertFalse(embed._acquire_claim()))
                self._assert_exact_refusal(
                    root, "pending-plan-delete",
                    embed._drop_pending_plan)
                self._assert_exact_refusal(
                    root, "pending-plan-build",
                    lambda: self._assert_permission_error(
                        lambda: embed._PendingPlanBuilder(
                            {"generation": "g"}, "out", "model")))
                self._assert_exact_refusal(
                    root, "pending-plan-advance",
                    lambda: self.assertFalse(embed._advance_pending_plan(
                        {"generation": "g"}, "old", "new", ["m1"])))
                self._assert_exact_refusal(
                    root, "state-publication",
                    lambda: embed._publish_state({"state": "running"}))
                self._assert_exact_refusal(
                    root, "segment-publication",
                    lambda: self._assert_permission_error(
                        lambda: embed._publish_segment_generation(
                            None, None, 0, [], [], [], [], [], [],
                            dim=1, append_segmented=False)))
                self._assert_exact_refusal(
                    root, "generation-rebase",
                    lambda: self.assertIsNone(
                        embed.rebase_generation_marker()))
                self._assert_exact_refusal(
                    root, "segment-compaction-launch",
                    lambda: self.assertFalse(
                        embed._schedule_segment_compaction()))
                self._assert_exact_refusal(
                    root, "legacy-prune",
                    lambda: self.assertEqual(
                        embed._prune_legacy_embedding_layout(),
                        {"removed": 0, "deferred": 0}))
                self._assert_exact_refusal(
                    root, "generation-stamp",
                    lambda: self.assertIsNone(embed._stamp(
                        None, indexed_ids=[], expected_hashes=None,
                        total_rows=0)))

    def test_foreign_database_fences_direct_semantic_writer_entrypoints(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semantic-foreign-db-") as raw:
            base = Path(raw)
            root = base / "data"
            root.mkdir()
            runtime = base / "runtime"
            runtime.mkdir()
            owner_path = root / ".derived-owner.json"
            owner_path.write_text(json.dumps({
                "version": 1, "build_id": BUILD_A,
            }, separators=(",", ":")), encoding="utf-8")
            cache_path = root / ".ingest_cache.bin"
            header = bytearray(44)
            header[:4] = int(19).to_bytes(4, "little")
            header[12:20] = b"AGRPCB01"
            header[20:24] = int(4).to_bytes(4, "little")
            header[24:44] = BUILD_A.encode("ascii")
            cache_path.write_bytes(header)
            db_path = root / "corpus.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (BUILD_B,))
            db.commit()
            db.close()
            refs_path = root / "embeddings.refs.sqlite"
            refs_path.write_bytes(b"foreign refs")
            before = _census(root)
            create_exclusive = ownerfile.create_exclusive
            external_claims = []

            def guarded_create_exclusive(path, *args, **kwargs):
                path = Path(path)
                expected = semworker._ephemeral_coordination_dir() / "worker.lock"
                if path != expected:
                    raise AssertionError(
                        f"foreign derived-store claim created: {path}")
                external_claims.append(path)
                return create_exclusive(path, *args, **kwargs)

            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(
                    semworker, "_coordination_base_path",
                    return_value=runtime),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    indexd_runtime, "DERIVED_OWNER_PATH", owner_path),
                mock.patch.object(
                    indexd_runtime, "INGEST_CACHE_PATH", cache_path),
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A),
                mock.patch.object(embed, "_CLAIM_HANDLE", None),
                mock.patch.object(
                    semantic_segment_compact, "_CLAIM_HANDLE", None),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": ""}, clear=False),
                mock.patch.object(common, "log"),
                mock.patch.object(
                    common, "open_bounded_log",
                    side_effect=AssertionError("foreign semantic log opened")),
                mock.patch.object(
                    ownerfile, "create_exclusive",
                    side_effect=guarded_create_exclusive),
                mock.patch.object(
                    embed.subprocess, "Popen",
                    side_effect=AssertionError("foreign semantic child spawned")),
                mock.patch.dict(
                    ask._MESSAGE_REFS, {"path": refs_path}, clear=False),
                mock.patch.object(
                    ask, "_quarantine_message_refs_path",
                    side_effect=AssertionError("foreign refs quarantined")),
            ):
                refusal = indexd_runtime.derived_writer_mutation_info()
                self.assertFalse(refusal.writable)
                self.assertIn(f"corpus.db owned-by {BUILD_B}", refusal.reason)
                with (
                    mock.patch.object(
                        ask, "_fast_cached_message_refs", return_value=None),
                    mock.patch.object(
                        ask, "_message_refs_identity",
                        return_value='{"fixture":"missing"}'),
                    mock.patch.object(
                        ask, "_existing_message_refs",
                        return_value=(None, None)),
                    self.assertRaisesRegex(
                        ask.MessageRefsUnavailable,
                        f"corpus.db owned-by {BUILD_B}"),
                ):
                    ask._message_refs(
                        ["missing"], object(),
                        coverage={"indexed": 1, "total": 1, "pending": 0})
                self.assertEqual(self._embed_main(["embed.py", "--full"]), 1)
                self.assertFalse(embed._acquire_claim())
                self.assertFalse(embed._schedule_segment_compaction())
                with self.assertRaisesRegex(
                        PermissionError, f"corpus.db owned-by {BUILD_B}"):
                    semantic_q8._publish_manifest({})
                with self.assertRaisesRegex(
                        PermissionError, f"corpus.db owned-by {BUILD_B}"):
                    semantic_segment_build._write_matrix(
                        root / "matrix", [], 1)
                self.assertFalse(semantic_segment_compact._acquire_claim())
                compacted = semantic_segment_compact.compact()
                self.assertEqual(compacted["state"], "read-only")
                self.assertIn(
                    f"corpus.db owned-by {BUILD_B}", compacted["reason"])
                self.assertEqual(semantic_segment_compact.main([]), 1)
                self.assertIsNone(semworker._acquire_start_claim())
                worker_owner = semworker._acquire_worker_lock()
                self.assertIsInstance(worker_owner, ownerfile.Handle)
                self.assertEqual(worker_owner.path, semworker.worker_lock_path())
                self.assertFalse(worker_owner.path.is_relative_to(root))
                self.assertFalse(
                    indexd_runtime.derived_writer_mutation_info().writable)
                with self.assertRaisesRegex(
                        PermissionError, f"corpus.db owned-by {BUILD_B}"):
                    semantic_q8._publish_manifest({})
                semworker.finish_inprocess_owner(
                    worker_owner, resources_released=True)
                self.assertIsNone(semworker._ensure_worker())
                self.assertIsNone(semworker.acquire_resident_owner())
                local_owner = semworker.acquire_inprocess_owner()
                self.assertIsInstance(local_owner, ownerfile.Handle)
                self.assertEqual(local_owner.path, semworker.worker_lock_path())
                self.assertFalse(local_owner.path.is_relative_to(root))
                self.assertFalse(
                    indexd_runtime.derived_writer_mutation_info().writable)
                semworker.finish_inprocess_owner(
                    local_owner, resources_released=True)
                with self.assertRaisesRegex(
                        OSError, f"corpus.db owned-by {BUILD_B}"):
                    semworker._spawn_worker(mock.Mock())
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
                    mock.patch.object(
                        semantic, "embedding_coherence",
                        return_value=coherence),
                    mock.patch.object(embedder, "get", return_value=object()),
                    mock.patch.object(
                        ask, "tool_search_hybrid",
                        side_effect=ask.CorruptMessageRefs("foreign corrupt refs")),
                    self.assertRaisesRegex(
                        semantic.SemanticUnavailable,
                        "full rebuild read-only"),
                ):
                    semantic.search("foreign query")
                self.assertEqual(external_claims, [
                    semworker.worker_lock_path(),
                    semworker.worker_lock_path(),
                ])
            self.assertEqual(_census(root), before)

    def test_refs_publications_recheck_ownership_after_long_build_and_at_pointer(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-refs-owner-race-") as raw:
            root = Path(raw)
            owner_path = root / ".derived-owner.json"
            owner_path.write_text(json.dumps({
                "version": 1, "build_id": BUILD_A,
            }, separators=(",", ":")), encoding="utf-8")
            cache_path = root / ".ingest_cache.bin"
            header = bytearray(44)
            header[:4] = int(19).to_bytes(4, "little")
            header[12:20] = b"AGRPCB01"
            header[20:24] = int(4).to_bytes(4, "little")
            header[24:44] = BUILD_A.encode("ascii")
            cache_path.write_bytes(header)
            db_path = root / "corpus.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (BUILD_A,))
            db.commit()
            db.close()

            def set_db_owner(build: str) -> None:
                connection = sqlite3.connect(db_path)
                try:
                    connection.execute(
                        "UPDATE meta SET value=? WHERE key='build_id'",
                        (build,))
                    connection.commit()
                finally:
                    connection.close()

            class FlipBeforeReplace:
                flipped = False

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def verify(self):
                    if not self.flipped:
                        set_db_owner(BUILD_B)
                        self.flipped = True

            def build_temp(path, *_args, **_kwargs):
                path.write_bytes(b"complete temporary refs")

            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    indexd_runtime, "DERIVED_OWNER_PATH", owner_path),
                mock.patch.object(
                    indexd_runtime, "INGEST_CACHE_PATH", cache_path),
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": ""}, clear=False),
                mock.patch.object(
                    ask, "_fast_cached_message_refs", return_value=None),
                mock.patch.object(
                    ask, "_message_refs_identity",
                    return_value='{"fixture":"race"}'),
                mock.patch.object(
                    ask, "_existing_message_refs",
                    return_value=(None, None)),
                mock.patch.object(
                    ask, "_create_message_refs_db",
                    side_effect=build_temp),
                mock.patch.object(
                    common, "EmbeddingPublishLock",
                    return_value=FlipBeforeReplace()),
            ):
                ask._close_message_refs()
                with self.assertRaisesRegex(
                        ask.MessageRefsUnavailable,
                        f"corpus.db owned-by {BUILD_B}"):
                    ask._message_refs(
                        ["m1"], object(),
                        coverage={"indexed": 1, "total": 1, "pending": 0})
                self.assertEqual(
                    list(root.glob("embeddings.refs-*.db")), [])
                self.assertEqual(
                    list(root.glob(".embeddings.refs-build-*.tmp")), [])
                self.assertFalse(ask._message_refs_pointer_path().exists())

                set_db_owner(BUILD_A)
                candidate = root / "candidate.db"
                candidate.write_bytes(b"sealed refs")

                def flip_pointer_owner() -> None:
                    set_db_owner(BUILD_B)

                with self.assertRaisesRegex(
                        ask.MessageRefsUnavailable,
                        f"corpus.db owned-by {BUILD_B}"):
                    ask._publish_message_refs_pointer(
                        candidate, '{"fixture":"pointer"}', 1,
                        before_publish=flip_pointer_owner)
                self.assertFalse(ask._message_refs_pointer_path().exists())
                self.assertEqual(
                    list(root.glob(".embeddings.refs.meta.*")), [])

    def test_foreign_windows_embedding_read_uses_external_snapshot_only(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-win-foreign-map-") as raw:
            root = Path(raw)
            embeddings = root / "segments" / "s1" / "embeddings.f32"
            embeddings.parent.mkdir(parents=True)
            ids_path = embeddings.with_name("embeddings.ids")
            meta_path = embeddings.with_name("embeddings.meta")
            expected = np.asarray([[1.0, 0.0]], dtype="<f4")
            common.write_embeddings(
                ["m1"], expected,
                embeddings_path=embeddings, ids_path=ids_path,
                dim=2, model_id="fixture", text_hashes=["a" * 16])
            owner_path = root / ".derived-owner.json"
            owner_path.write_text(json.dumps({
                "version": 1, "build_id": BUILD_A,
            }, separators=(",", ":")), encoding="utf-8")
            cache_path = root / ".ingest_cache.bin"
            header = bytearray(44)
            header[:4] = int(19).to_bytes(4, "little")
            header[12:20] = b"AGRPCB01"
            header[20:24] = int(4).to_bytes(4, "little")
            header[24:44] = BUILD_A.encode("ascii")
            cache_path.write_bytes(header)
            db_path = root / "corpus.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (BUILD_B,))
            db.commit()
            db.close()
            before = _census(root)
            mapped = None
            snapshot = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "EMBEDDINGS_PATH", embeddings),
                mock.patch.object(common, "IDS_PATH", ids_path),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    indexd_runtime, "DERIVED_OWNER_PATH", owner_path),
                mock.patch.object(
                    indexd_runtime, "INGEST_CACHE_PATH", cache_path),
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A),
                mock.patch.object(embedding_store, "WIN", True),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": ""}, clear=False),
                mock.patch.object(
                    embedding_store, "_prune_embedding_snapshots",
                    side_effect=AssertionError(
                        "foreign reader pruned owner-root aliases")) as prune,
                mock.patch.object(
                    embedding_store.os, "link",
                    side_effect=AssertionError(
                        "foreign reader hardlinked an owner-root alias")) as link,
            ):
                try:
                    loaded, mapped = ask._load_message_embeddings(2, meta_path)
                    snapshot = Path(mapped._agrep_snapshot_path)
                    self.assertEqual(loaded, ("m1",))
                    np.testing.assert_array_equal(mapped, expected)
                    self.assertTrue(mapped._agrep_snapshot_external)
                    self.assertNotEqual(snapshot.parent, embeddings.parent)
                    self.assertTrue(snapshot.exists())
                    self.assertEqual(_census(root), before)
                finally:
                    if mapped is not None:
                        common.close_embedding_matrix(mapped)
            prune.assert_not_called()
            link.assert_not_called()
            self.assertIsNotNone(snapshot)
            self.assertFalse(snapshot.exists())
            self.assertEqual(_census(root), before)

            protected_map = None
            protected_snapshot = None
            with (
                mock.patch.object(embedding_store, "WIN", True),
                mock.patch.dict(
                    os.environ,
                    {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    embedding_store, "_prune_embedding_snapshots",
                    side_effect=AssertionError(
                        "protected descendant aliases were pruned")),
                mock.patch.object(
                    embedding_store.os, "link",
                    side_effect=AssertionError(
                        "protected descendant alias was hardlinked")),
            ):
                try:
                    protected_ids, protected_map = (
                        embedding_store.read_embeddings(
                            embeddings, ids_path, dim=2,
                            meta_path=meta_path, attempts=1))
                    protected_snapshot = Path(
                        protected_map._agrep_snapshot_path)
                    self.assertEqual(protected_ids, ["m1"])
                    self.assertTrue(protected_map._agrep_snapshot_external)
                    self.assertNotEqual(
                        protected_snapshot.parent, embeddings.parent)
                    self.assertEqual(_census(root), before)
                finally:
                    if protected_map is not None:
                        embedding_store.close_embedding_matrix(protected_map)
            self.assertIsNotNone(protected_snapshot)
            self.assertFalse(protected_snapshot.exists())
            self.assertEqual(_census(root), before)

    def test_foreign_segment_reader_hashes_but_skips_integrity_receipt(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-segment-foreign-") as raw:
            root = Path(raw)
            fixture = test_segment_query.SegmentQueryTests()
            source, meta, _manifest, db_path, _rows = fixture._fixture(root)
            record = json.loads(meta.read_text(encoding="utf-8"))
            record.pop(embedding_segments.PROOF_KEY)
            meta.write_text(
                json.dumps(record, separators=(",", ":")),
                encoding="utf-8")
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (BUILD_B,))
            connection.commit()
            connection.close()
            owner_path = root / ".derived-owner.json"
            owner_path.write_text(json.dumps({
                "version": 1, "build_id": BUILD_A,
            }, separators=(",", ":")), encoding="utf-8")
            cache_path = root / ".ingest_cache.bin"
            header = bytearray(44)
            header[:4] = int(19).to_bytes(4, "little")
            header[12:20] = b"AGRPCB01"
            header[20:24] = int(4).to_bytes(4, "little")
            header[24:44] = BUILD_A.encode("ascii")
            cache_path.write_bytes(header)
            receipt = segment_query._integrity_receipt_path(meta)
            before = _census(root)
            hashed = []
            original_hash = segment_query._sha256_file
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    indexd_runtime, "DERIVED_OWNER_PATH", owner_path),
                mock.patch.object(
                    indexd_runtime, "INGEST_CACHE_PATH", cache_path),
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": ""}, clear=False),
                mock.patch.object(
                    common, "transcript_generation", return_value=source),
                mock.patch.object(common, "log") as disclosure,
                mock.patch.object(
                    segment_query, "_sha256_file",
                    side_effect=lambda path: (
                        hashed.append(Path(path)), original_hash(path))[1]),
            ):
                segment_query.close_cache()
                manifest, _, refs, coverage = segment_query.open_current(meta)
                self.assertEqual(coverage["indexed"], manifest["live_rows"])
                self.assertEqual(refs.rows, manifest["next_row_ref"])
                self.assertTrue(hashed)
                self.assertFalse(receipt.exists())
                self.assertFalse(any(
                    path.name.startswith(f".{receipt.name}.")
                    for path in root.iterdir()))
                self.assertEqual(_census(root), before)
                disclosure.assert_any_call(
                    "semantic integrity receipt skipped: "
                    f"corpus.db owned-by {BUILD_B}; this build is {BUILD_A}")
                segment_query.close_cache()

                connection = sqlite3.connect(db_path)
                connection.execute(
                    "UPDATE meta SET value=? WHERE key='build_id'",
                    (BUILD_A,))
                connection.commit()
                connection.close()
                segment_query.open_current(meta)
                self.assertTrue(receipt.exists())
                segment_query.close_cache()

    @staticmethod
    def _embed_main(argv: list[str]) -> int:
        with mock.patch.object(sys, "argv", argv):
            return embed.main()

    def _assert_permission_error(self, call) -> None:
        with self.assertRaisesRegex(PermissionError, "AGREP_DATA_READONLY"):
            call()

    def test_q8_writer_entrypoints_refuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-q8-readonly-") as raw:
            root = Path(raw)
            artifact_dir = root / "semantic-q8"
            artifact_dir.mkdir()
            keep = []
            for name in ("keep.q8", "keep.q8g", "keep.f16", "obsolete.q8"):
                path = artifact_dir / name
                path.write_bytes(name.encode("ascii"))
                keep.append(path)
            manifest = {
                "artifact_path": keep[0],
                "group_artifact_path": keep[1],
                "exact_artifact_path": keep[2],
            }
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(
                    semantic_q8, "MANIFEST_PATH",
                    root / "embeddings.q8.meta"),
                mock.patch.object(semantic_q8, "ARTIFACT_DIR", artifact_dir),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
            ):
                calls = {
                    "f16-build": lambda: semantic_q8._build_f16(
                        root / "embeddings.f32", artifact_dir,
                        generation="a" * 32, rows=1, dim=1),
                    "q8-build": lambda: semantic_q8.build_from_f32(
                        root / "embeddings.f32", root / "embeddings.meta",
                        artifact_dir),
                    "manifest-publish": lambda: semantic_q8._publish_manifest({}),
                    "family-groups": lambda: semantic_q8._write_family_groups(
                        artifact_dir / "groups.ids"),
                    "ensure": lambda: semantic_q8.ensure_artifact({}),
                    "rebind": lambda: semantic_q8._rebind_append_generation({}, {}),
                    "generation-publish": lambda: semantic_q8.publish_for_generation(
                        {"indexed": 1, "pending": 0}),
                }
                for label, call in calls.items():
                    self._assert_exact_refusal(
                        root, label,
                        lambda call=call: self._assert_permission_error(call))
                self._assert_exact_refusal(
                    root, "obsolete-prune",
                    lambda: semantic_q8._prune_obsolete_artifacts(manifest))

    def test_segment_builder_entrypoints_refuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-segment-build-readonly-") as raw:
            root = Path(raw)
            prepared_root = root / ".semantic-segment-existing"
            prepared_root.mkdir()
            (prepared_root / "keep").write_bytes(b"prepared")
            prepared = semantic_segment_build.PreparedSegment(
                prepared_root, {}, [])
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
            ):
                calls = {
                    "family-catalog": lambda: semantic_segment_build._family_ids([]),
                    "matrix": lambda: semantic_segment_build._write_matrix(
                        root / "matrix", [], 1),
                    "prepare": lambda: semantic_segment_build.prepare(
                        [], [], [], [], dim=1, model_id="fixture"),
                }
                for label, call in calls.items():
                    self._assert_exact_refusal(
                        root, label,
                        lambda call=call: self._assert_permission_error(call))
                self._assert_exact_refusal(root, "prepared-cleanup", prepared.close)

    def test_segment_compactor_entrypoints_refuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-segment-compact-readonly-") as raw:
            root = Path(raw)
            (root / ".semantic-compaction.lock").write_bytes(b"stale")
            (root / "keep").write_bytes(b"compactor")
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(semantic_segment_compact, "_CLAIM_HANDLE", None),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(common, "log"),
            ):
                self._assert_exact_refusal(
                    root, "claim",
                    lambda: self.assertFalse(
                        semantic_segment_compact._acquire_claim()))
                self._assert_exact_refusal(
                    root, "compact",
                    lambda: self.assertEqual(
                        semantic_segment_compact.compact()["state"],
                        "read-only"))
                self._assert_exact_refusal(
                    root, "main",
                    lambda: self.assertEqual(
                        semantic_segment_compact.main([]), 1))
                calls = {
                    "refs": lambda: semantic_segment_compact._create_refs(
                        root / "refs.sqlite"),
                    "metadata": lambda: semantic_segment_compact._current_metadata_map(
                        None, root),
                    "stream": lambda: semantic_segment_compact._stream_live_base(
                        None, root, governor=lambda: None, check_every_rows=1),
                    "artifacts": lambda: semantic_segment_compact._derive_artifacts(
                        root, root / "base.f32", root / "groups", 0, 1),
                }
                for label, call in calls.items():
                    self._assert_exact_refusal(
                        root, label,
                        lambda call=call: self._assert_permission_error(call))

    def test_embedding_store_publication_entrypoints_refuse_exact_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-embedding-store-readonly-") as raw:
            root = Path(raw)
            matrix = root / "embeddings.f32"
            ids = root / "embeddings.ids"
            meta = root / "embeddings.meta"
            matrix.write_bytes(b"\0\0\0\0")
            ids.write_bytes(b"m1\n")
            meta.write_bytes(b"legacy")
            derived = root / "derived"
            derived.mkdir()
            stale = root / ".embeddings.f32.old-0-none-dead.tmp"
            stale.write_bytes(b"temp")
            with (
                mock.patch.object(embedding_store, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
            ):
                calls = {
                    "index-meta": lambda: embedding_store.write_index_meta(
                        meta, 1, "fixture"),
                    "nested-index-meta": lambda: embedding_store.write_index_meta(
                        derived / "embeddings.meta", 1, "fixture"),
                    "commit": lambda: embedding_store.write_embedding_commit(
                        meta, 1, "fixture", matrix, ids, 1),
                    "publish-lock": lambda: embedding_store.EmbeddingPublishLock(
                        matrix, timeout=0).__enter__(),
                    "publication-guard": lambda: embedding_store.EmbeddingPublicationGuard(
                        meta, matrix, timeout=0).__enter__(),
                    "barrier": lambda: embedding_store._write_embedding_publication_barrier(
                        meta, 1, "fixture", lambda: None),
                    "commit-upgrade": lambda: embedding_store.ensure_embedding_commit(
                        matrix, ids, meta),
                    "matrix": lambda: embedding_store.write_embeddings(
                        ["m1"], np.zeros((1, 1), dtype=np.float32),
                        matrix, ids, dim=1),
                    "matrix-parts": lambda: embedding_store.write_embeddings_parts(
                        ["m1"], [np.zeros((1, 1), dtype=np.float32)],
                        matrix, ids, dim=1),
                    "files": lambda: embedding_store._write_embedding_files(
                        ["m1"], [np.zeros((1, 1), dtype=np.float32)],
                        matrix, ids, 1, "fixture", None),
                }
                for label, call in calls.items():
                    self._assert_exact_refusal(
                        root, label,
                        lambda call=call: self._assert_permission_error(call))
                self._assert_exact_refusal(
                    root, "temp-prune",
                    lambda: embedding_store._prune_embedding_temps(matrix))

    def test_embedding_store_guard_does_not_capture_a_sibling_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-embedding-store-boundary-") as raw:
            parent = Path(raw)
            protected = parent / "protected"
            writable = parent / "writable"
            protected.mkdir()
            writable.mkdir()
            (protected / "keep").write_bytes(b"protected")
            before = _census(protected)
            meta = writable / "embeddings.meta"
            with (
                mock.patch.object(embedding_store, "DATA_DIR", protected),
                mock.patch.dict(
                    os.environ,
                    {"AGREP_DATA_READONLY": os.fspath(protected)},
                    clear=False),
            ):
                embedding_store.write_index_meta(meta, 1, "fixture")
            self.assertEqual(_census(protected), before)
            self.assertEqual(
                embedding_store.read_index_meta(meta), (1, "fixture"))

    def test_concept_pair_keeps_unicode_line_separators_inside_strings(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-concept-lines-") as raw:
            root = Path(raw)
            sessions = [{
                "session": "fixture",
                "label": "before\u2028middle\u2029next\u0085after",
            }]
            conceptpair.publish(root, [], sessions)
            _concepts, loaded, _manifest = conceptpair.read(root)
            self.assertEqual(loaded, sessions)


    def test_segment_and_concept_publications_refuse_descendants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-derived-readonly-") as raw:
            root = Path(raw)
            derived = root / "derived"
            derived.mkdir()
            (derived / "keep").write_bytes(b"derived")
            meta = derived / "embeddings.meta"
            with mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False):
                calls = {
                    "segment-base": lambda: embedding_segments.publish_base(
                        meta, source={}, model_id="fixture", dim=1,
                        artifacts={}, ids=[], hashes=[], refs=[],
                        coverage={"total": 0}),
                    "segment-delta": lambda: embedding_segments.publish_delta(
                        meta, source={}, artifacts=None,
                        coverage={"total": 0}),
                    "segment-rebind": lambda: embedding_segments.publish_rebind(
                        meta, source={}, coverage={"total": 0},
                        expected_generation="fixture"),
                    "concept-pair": lambda: conceptpair.publish(
                        derived, [], []),
                }
                for label, call in calls.items():
                    self._assert_exact_refusal(
                        root, label,
                        lambda call=call: self._assert_permission_error(call))

    def test_model_download_refuses_descendant_but_read_only_check_remains(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-model-readonly-") as raw:
            root = Path(raw)
            model_root = root / "models" / "fixture"
            model_root.mkdir(parents=True)
            model_file = model_root / "model.bin"
            model_file.write_bytes(b"x")
            stale = model_root / ".stale.part"
            stale.write_bytes(b"partial")
            profile = {
                **embedder.PROFILE,
                "files": {
                    "model.bin": (
                        1, hashlib.sha256(b"x").hexdigest()),
                },
            }
            with (
                mock.patch.object(embedder, "PROFILE", profile),
                mock.patch.object(embedder, "model_dir", return_value=model_root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    embedder.urllib.request, "urlopen",
                    side_effect=AssertionError("network was reached")) as network,
            ):
                self._assert_exact_refusal(
                    root, "model-cached-download",
                    lambda: self.assertEqual(
                        embedder.ensure_model(download=True), model_root))
                self._assert_exact_refusal(
                    root, "model-read",
                    lambda: self.assertEqual(
                        embedder.ensure_model(download=False), model_root))
                self._assert_exact_refusal(
                    root, "model-claim",
                    lambda: self._assert_embedder_unavailable(
                        lambda: embedder._acquire_download_claim(model_root)))
                self._assert_exact_refusal(
                    root, "model-part-delete",
                    lambda: self.assertFalse(
                        embedder._discard_download_part(stale)))
                self._assert_exact_refusal(
                    root, "model-fetch",
                    lambda: self._assert_embedder_unavailable(
                        lambda: embedder._fetch_pinned(
                            "https://invalid.test/model", stale, 1)))
                missing_profile = {
                    **profile,
                    "files": {
                        **profile["files"],
                        "missing.bin": (
                            1, hashlib.sha256(b"m").hexdigest()),
                    },
                }
                with mock.patch.object(embedder, "PROFILE", missing_profile):
                    self._assert_exact_refusal(
                        root, "model-download",
                        lambda: self._assert_embedder_unavailable(
                            lambda: embedder.ensure_model(download=True)))
                    self._assert_exact_refusal(
                        root, "model-missing-read",
                        lambda: self._assert_missing_model(
                            lambda: embedder.ensure_model(download=False)))
                network.assert_not_called()

    def _assert_embedder_unavailable(self, call) -> None:
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable, "AGREP_DATA_READONLY"):
            call()

    def _assert_missing_model(self, call) -> None:
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable, "model files missing"):
            call()


if __name__ == "__main__":
    unittest.main()
