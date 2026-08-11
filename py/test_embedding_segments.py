from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import doctor
import embedder
import embedding_store
import embedding_segments as segments
import semantic
import semworker


def _fnv(payload: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in payload:
        value ^= byte
        value = (value * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _inputs(root: Path, label: str, mids: list[str]) -> tuple[dict, list[str], list[dict]]:
    dim = 2
    generation = hashlib.md5(label.encode("ascii")).digest()
    q8_payload = b"".join(struct.pack("<fbb", 1.0, 1, 0) for _ in mids)
    q8_header = bytearray(64)
    struct.pack_into("<4sIIIQ16sIIQ", q8_header, 0, b"AGQ8", 1, dim, 1, len(mids),
                     generation, dim + 4, 0, _fnv(q8_payload))
    group_payload = b"".join(struct.pack("<I", 1) for _ in mids)
    group_header = bytearray(64)
    struct.pack_into("<4sIIIQ16sIIQ", group_header, 0, b"AGQG", 1, 0, 2, len(mids),
                     generation, 4, 0, _fnv(group_payload))
    artifacts = {}
    payloads = {
        "f32": b"\0" * (len(mids) * dim * 4),
        "f16": b"\0" * (len(mids) * dim * 2),
        "q8": bytes(q8_header) + q8_payload,
        "groups": bytes(group_header) + group_payload,
    }
    for name, payload in payloads.items():
        path = root / f"{label}.{name}"
        path.write_bytes(payload)
        artifacts[name] = path
    hashes = [hashlib.blake2b(mid.encode(), digest_size=8).hexdigest() for mid in mids]
    refs = [{
        "mid": mid, "text_hash": text_hash, "agent": "codex", "project": "p",
        "session": f"s-{mid}", "ts": index, "turn": index, "who": "user",
        "model": None, "model_source": "explicit", "family_id": 1,
        "family_label": f"f:s-{mid}",
        "side": False,
        "metadata_hash": hashlib.blake2b(
            f"metadata:{mid}".encode(), digest_size=16).hexdigest(),
    } for index, (mid, text_hash) in enumerate(zip(mids, hashes, strict=True))]
    return artifacts, hashes, refs


class EmbeddingSegmentTests(unittest.TestCase):
    def test_pre_model_source_refs_schema_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.refs.sqlite"
            db = sqlite3.connect(path)
            try:
                columns = ",".join(
                    f"{name} {kind}"
                    for name, kind in segments._REF_COLUMNS_V3
                )
                db.execute(f"CREATE TABLE refs({columns})")
                db.execute("CREATE UNIQUE INDEX refs_row_ref ON refs(row_ref)")
                db.execute("CREATE UNIQUE INDEX refs_mid ON refs(mid)")
                db.execute("CREATE INDEX refs_session ON refs(session,turn)")
                db.commit()
            finally:
                db.close()
            opened = segments._open_refs(path)
            try:
                self.assertFalse(segments._refs_have_model_source(opened))
            finally:
                opened.close()

    def test_windows_manifest_replace_retries_reader_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "embeddings.meta"
            target.write_bytes(b"old\n")
            original = os.replace
            calls = 0

            def transient(source, destination):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise PermissionError("mapped reader")
                return original(source, destination)

            with (mock.patch.object(segments, "_WINDOWS", True),
                  mock.patch.object(segments.os, "replace", side_effect=transient),
                  mock.patch.object(segments.time, "sleep") as sleep):
                segments._write_bytes(target, b"new\n")
            self.assertEqual(target.read_bytes(), b"new\n")
            self.assertEqual(calls, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_windows_identity_uses_one_handle_across_path_swap(self) -> None:
        identity_a = (11, 12, 13, 14, 15)
        identity_b = (21, 22, 23, 24, 25)
        state = {"current": identity_a}

        class SwappingPath:
            stat_calls = 0

            def stat(self):
                self.stat_calls += 1
                captured = state["current"]
                state["current"] = identity_b
                values = ("st_dev", "st_ino", "st_size", "st_mtime_ns",
                          "st_ctime_ns")
                return type("Stat", (), dict(zip(values, captured, strict=True)))()

        path = SwappingPath()

        def capture_one_handle(_path):
            captured = state["current"]
            state["current"] = identity_b
            return captured

        with (
            mock.patch.object(segments, "_WINDOWS", True),
            mock.patch.object(
                segments, "_windows_file_identity",
                side_effect=capture_one_handle) as capture,
        ):
            observed = segments._file_identity(path)

        self.assertEqual(observed, identity_a)
        self.assertEqual(state["current"], identity_b)
        self.assertEqual(path.stat_calls, 0)
        capture.assert_called_once_with(path)

    def test_delta_does_not_rehash_immutable_segment_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            prefix = {
                segments.artifact_path(base, descriptor)
                for segment in base["segments"]
                for descriptor in segment["artifacts"].values()
            }
            original = segments._sha_file

            def guarded(path: Path) -> str:
                if Path(path) in prefix:
                    raise AssertionError(f"immutable prefix was rehashed: {path}")
                return original(path)

            artifacts, hashes, refs = _inputs(root, "delta", ["c"])
            with mock.patch.object(segments, "_sha_file", side_effect=guarded):
                updated = segments.publish_delta(
                    meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                    ids=["c"], hashes=hashes, refs=refs, coverage={"total": 3},
                    expected_generation=base["generation"])
            self.assertEqual(updated["live_rows"], 3)
            self.assertEqual(segments.load_manifest(meta)["generation"],
                             updated["generation"])

    def test_stale_delta_and_rebind_generations_are_publication_races(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a"], hashes=hashes, refs=refs,
                coverage={"total": 1})
            winner = segments.publish_rebind(
                meta, source={"ingest_signature": "two"},
                coverage={"total": 1},
                expected_generation=base["generation"])
            artifacts, hashes, refs = _inputs(root, "delta", ["b"])

            with self.assertRaises(segments.SegmentPublicationRace):
                segments.publish_delta(
                    meta, source={"ingest_signature": "two"},
                    artifacts=artifacts, ids=["b"], hashes=hashes, refs=refs,
                    coverage={"total": 2},
                    expected_generation=base["generation"])
            with self.assertRaises(segments.SegmentPublicationRace):
                segments.publish_rebind(
                    meta, source={"ingest_signature": "three"},
                    coverage={"total": 1},
                    expected_generation=base["generation"])

            current = segments.load_manifest(meta)
            self.assertEqual(current["generation"], winner["generation"])
            self.assertEqual(segments.orphan_artifacts(current)["count"], 0)

    def test_delta_repairs_byte_identical_prefix_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            original = segments.publication_artifact_identities(base)
            self.assertIsNotNone(original)
            for index, path in enumerate(original):
                replacement = root / f"relocated-{index}"
                shutil.copy2(path, replacement)
                os.replace(replacement, path)
            self.assertTrue(any(
                segments._file_identity(path) != identity
                for path, identity in original.items()))

            artifacts, hashes, refs = _inputs(root, "delta", ["c"])
            updated = segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["c"], hashes=hashes, refs=refs, coverage={"total": 3},
                expected_generation=base["generation"])

            self.assertEqual(updated["live_rows"], 3)
            self.assertEqual(
                segments.load_manifest(meta, verify_hashes=True)["generation"],
                updated["generation"])

    def test_delta_rejects_damaged_relocated_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            matrix = segments.artifact_path(
                base, base["segments"][0]["artifacts"]["f32"])
            replacement = root / "relocated-damaged"
            shutil.copy2(matrix, replacement)
            payload = bytearray(replacement.read_bytes())
            payload[0] ^= 0x01
            replacement.write_bytes(payload)
            stat = matrix.stat()
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(replacement, matrix)

            artifacts, hashes, refs = _inputs(root, "delta", ["c"])
            with self.assertRaisesRegex(segments.SegmentError, "digest mismatch"):
                segments.publish_delta(
                    meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                    ids=["c"], hashes=hashes, refs=refs, coverage={"total": 3},
                    expected_generation=base["generation"])

    def test_diagnostic_hash_verification_catches_size_preserving_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            manifest = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            matrix = segments.artifact_path(
                manifest, manifest["segments"][0]["artifacts"]["f32"])
            payload = bytearray(matrix.read_bytes())
            payload[0] ^= 0x01
            matrix.write_bytes(payload)

            self.assertEqual(
                segments.load_manifest(meta)["generation"],
                manifest["generation"])
            with self.assertRaisesRegex(segments.SegmentError, "digest mismatch"):
                segments.load_manifest(
                    meta, verify_hashes=True, validate_liveness=True)

    def test_doctor_detects_size_preserving_refs_digest_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            manifest = segments.publish_base(
                meta, source={"ingest_signature": "one"},
                model_id="model", dim=2, artifacts=artifacts,
                ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            refs_path = segments.artifact_path(
                manifest, manifest["segments"][0]["artifacts"]["refs"])
            payload = bytearray(refs_path.read_bytes())
            payload[-1] ^= 0x01
            refs_path.write_bytes(payload)
            repair = {"state": "recorded", "persistent": True}
            coherence = {
                "coherent": True, "searchable": True, "state": "current",
            }
            with (
                mock.patch.object(
                    doctor.common, "EMBEDDINGS_PATH",
                    root / "embeddings.f32"),
                mock.patch.object(doctor, "_dep_present", return_value=True),
                mock.patch("importlib.import_module"),
                mock.patch.object(embedder, "ensure_model"),
                mock.patch.object(
                    semantic, "embedding_coherence",
                    return_value=coherence),
                mock.patch.object(
                    semantic, "verify_embedding_integrity",
                    wraps=semantic.verify_embedding_integrity) as verify,
                mock.patch.object(
                    semantic, "request_full_rebuild",
                    return_value=repair) as request,
                mock.patch.object(
                    semantic, "read_embed_state", return_value={}),
                mock.patch.object(
                    semantic, "embed_running", return_value=False),
                mock.patch.object(
                    semworker, "resident_status",
                    return_value={"running": False}),
            ):
                result = doctor._semantic_probe()

            verify.assert_called_once_with()
            request.assert_called_once()
            reason = request.call_args.args[0]
            self.assertIn("SegmentError", reason)
            self.assertIn("digest mismatch", reason)
            self.assertIn("refs", reason)
            self.assertFalse(result["live"])
            self.assertEqual(result["embeddings"], "corrupt-embeddings")
            self.assertEqual(
                result["embedding_integrity"]["state"], "corrupt")
            self.assertEqual(
                result["embedding_integrity"]["repair"], repair)

    def test_prefix_movement_before_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            prefix_path = segments.artifact_path(
                base, base["segments"][0]["artifacts"]["f32"])
            before = meta.read_bytes()
            artifacts, hashes, refs = _inputs(root, "delta", ["c"])

            def move_prefix(stage: str) -> None:
                if stage == "before_manifest_replace":
                    stat = prefix_path.stat()
                    os.utime(prefix_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

            with self.assertRaisesRegex(segments.SegmentError, "prefix moved"):
                segments.publish_delta(
                    meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                    ids=["c"], hashes=hashes, refs=refs, coverage={"total": 3},
                    expected_generation=base["generation"], _on_stage=move_prefix)
            self.assertEqual(meta.read_bytes(), before)

    def test_publish_update_delete_and_publish_last_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            base_rows = segments.active_rows(base)
            self.assertEqual([row["mid"] for row in base_rows], ["a", "b"])
            self.assertTrue(all(len(row["metadata_hash"]) == 32
                                for row in base_rows))
            self.assertEqual(
                [row["family_label"] for row in base_rows],
                ["f:s-a", "f:s-b"],
            )
            self.assertEqual(
                [row["model_source"] for row in base_rows],
                ["explicit", "explicit"],
            )

            artifacts, hashes, refs = _inputs(root, "delta", ["a", "c"])
            updated = segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["a", "c"], hashes=hashes, refs=refs, shadows=[0, 1],
                coverage={"total": 2}, expected_generation=base["generation"])
            self.assertEqual([row["mid"] for row in segments.active_rows(updated)], ["a", "c"])
            self.assertEqual(updated["live_rows"], 2)

            before = meta.read_bytes()
            artifacts, hashes, refs = _inputs(root, "failed", ["d"])

            def fail(stage: str) -> None:
                if stage == "before_manifest_replace":
                    raise RuntimeError("injected")

            with self.assertRaises(RuntimeError):
                segments.publish_delta(
                    meta, source={"ingest_signature": "three"}, artifacts=artifacts,
                    ids=["d"], hashes=hashes, refs=refs, shadows=[2],
                    coverage={"total": 3},
                    expected_generation=updated["generation"], _on_stage=fail)
            self.assertEqual(meta.read_bytes(), before)
            current = segments.load_manifest(meta)
            self.assertEqual(current["generation"], updated["generation"])
            self.assertEqual(segments.orphan_artifacts(current)["count"], 0)

    def test_rebind_race_cleans_its_candidate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model",
                dim=2, artifacts=artifacts, ids=["a"], hashes=hashes,
                refs=refs, coverage={"total": 1})
            before = meta.read_bytes()

            def fail(stage: str) -> None:
                if stage == "before_manifest_replace":
                    raise segments.SegmentPublicationRace("injected")

            with self.assertRaises(segments.SegmentPublicationRace):
                segments.publish_rebind(
                    meta, source={"ingest_signature": "two"},
                    coverage={"total": 1},
                    expected_generation=base["generation"], _on_stage=fail)
            self.assertEqual(meta.read_bytes(), before)
            current = segments.load_manifest(meta)
            self.assertEqual(segments.orphan_artifacts(current)["count"], 0)

    def test_final_replace_fence_runs_after_publication_guard_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            base = segments.publish_base(
                meta, source={"ingest_signature": "bound"}, model_id="model",
                dim=2, artifacts=artifacts, ids=["a"], hashes=hashes,
                refs=refs, coverage={"total": 1})
            before = meta.read_bytes()
            live_source = [{"ingest_signature": "bound"}]
            original_enter = embedding_store.EmbeddingPublicationGuard.__enter__

            def enter(guard):
                entered = original_enter(guard)
                live_source[0] = {"ingest_signature": "moved"}
                return entered

            def fence() -> None:
                if live_source[0] != {"ingest_signature": "bound"}:
                    raise RuntimeError("source moved while waiting for guard")

            with (
                mock.patch.object(
                    embedding_store.EmbeddingPublicationGuard, "__enter__", enter),
                self.assertRaisesRegex(RuntimeError, "waiting for guard"),
            ):
                segments.publish_rebind(
                    meta, source={"ingest_signature": "bound"},
                    coverage={"total": 1},
                    expected_generation=base["generation"],
                    _before_replace=fence)

            self.assertEqual(meta.read_bytes(), before)
            current = segments.load_manifest(meta)
            self.assertEqual(current["generation"], base["generation"])
            self.assertEqual(segments.orphan_artifacts(current)["count"], 0)

    def test_repeated_update_delete_and_readd_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            current = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})

            artifacts, hashes, refs = _inputs(root, "update-one", ["a"])
            current = segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["a"], hashes=hashes, refs=refs, shadows=[0],
                coverage={"total": 2}, expected_generation=current["generation"])
            artifacts, hashes, refs = _inputs(root, "update-two", ["a"])
            current = segments.publish_delta(
                meta, source={"ingest_signature": "three"}, artifacts=artifacts,
                ids=["a"], hashes=hashes, refs=refs, shadows=[2],
                coverage={"total": 2}, expected_generation=current["generation"])
            self.assertEqual(
                [(row["mid"], row["row_ref"]) for row in segments.active_rows(current)],
                [("b", 1), ("a", 3)])

            current = segments.publish_delta(
                meta, source={"ingest_signature": "four"}, artifacts=None,
                shadows=[3], coverage={"total": 1},
                expected_generation=current["generation"])
            self.assertEqual([row["mid"] for row in segments.active_rows(current)], ["b"])

            artifacts, hashes, refs = _inputs(root, "readd", ["a"])
            current = segments.publish_delta(
                meta, source={"ingest_signature": "five"}, artifacts=artifacts,
                ids=["a"], hashes=hashes, refs=refs, coverage={"total": 2},
                expected_generation=current["generation"])
            self.assertEqual(
                [(row["mid"], row["row_ref"]) for row in segments.active_rows(current)],
                [("b", 1), ("a", 4)])
            self.assertEqual((current["live_rows"], current["physical_rows"]), (2, 5))

    def test_orphan_cleanup_defers_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            current = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a"], hashes=hashes, refs=refs,
                coverage={"total": 1})
            orphan = root / segments.SEGMENT_DIR / "orphan.bin"
            orphan.write_bytes(b"old")
            before = meta.read_bytes()

            deferred = segments.prune_orphans(
                current, grace_seconds=0,
                _unlink=mock.Mock(side_effect=PermissionError("mapped reader")))
            self.assertEqual(deferred, {"removed": [], "deferred": [orphan]})
            self.assertTrue(orphan.exists())
            self.assertEqual(meta.read_bytes(), before)

            retried = segments.prune_orphans(current, grace_seconds=0)
            self.assertEqual(retried, {"removed": [orphan], "deferred": []})
            self.assertFalse(orphan.exists())
            self.assertTrue(all(path.exists() for path in segments.referenced_paths(current)))

    def test_a_publication_reclaims_the_generation_it_supersedes(self) -> None:
        # A6: the superseded set is collected by the publication that orphaned
        # it, so no scanner ever has a backlog to report.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            first = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model",
                dim=2, artifacts=artifacts, ids=["a"], hashes=hashes,
                refs=refs, coverage={"total": 1})
            superseded = root / segments.SEGMENT_DIR / "orphan.bin"
            superseded.write_bytes(b"old")
            os.utime(superseded, (2_000, 2_000))

            artifacts, hashes, refs = _inputs(root, "next", ["b"])
            second = segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["b"], hashes=hashes, refs=refs, coverage={"total": 2},
                expected_generation=first["generation"])

            self.assertFalse(superseded.exists())
            self.assertEqual(segments.orphan_artifacts(second)["count"], 0)
            self.assertTrue(
                all(path.exists()
                    for path in segments.referenced_paths(second)))

    def test_a_publication_survives_an_unreclaimable_predecessor(self) -> None:
        # Reclaiming is best effort: a mapped reader holding an old file must
        # never fail the publication that replaced it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            first = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model",
                dim=2, artifacts=artifacts, ids=["a"], hashes=hashes,
                refs=refs, coverage={"total": 1})
            artifacts, hashes, refs = _inputs(root, "next", ["b"])
            with mock.patch.object(
                    segments, "prune_orphans",
                    side_effect=OSError("mapped reader")):
                second = segments.publish_delta(
                    meta, source={"ingest_signature": "two"},
                    artifacts=artifacts, ids=["b"], hashes=hashes, refs=refs,
                    coverage={"total": 2},
                    expected_generation=first["generation"])
            self.assertEqual(second["live_rows"], 2)

    def test_publication_proof_is_manifest_bound_and_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            current = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes, refs=refs,
                coverage={"total": 2})
            proof_path = segments.artifact_path(
                current, current[segments.PROOF_KEY])
            identities = segments.publication_artifact_identities(current)
            self.assertIsNotNone(identities)
            self.assertIn(proof_path, segments.referenced_paths(current))
            self.assertNotIn(proof_path, identities)
            self.assertEqual(
                set(identities), segments.referenced_paths(current) - {proof_path})

            proof = json.loads(proof_path.read_bytes())
            proof["generation"] = "0" * 32
            payload = segments._canonical(proof)
            proof_path.write_bytes(payload)
            record = json.loads(meta.read_bytes())
            record[segments.PROOF_KEY].update(
                size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            meta.write_bytes(segments._canonical(record))
            with self.assertRaisesRegex(
                    segments.SegmentError, "proof does not bind"):
                segments.load_manifest(meta)

    def test_orphan_inventory_does_not_protect_future_mtimes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            current = segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a"], hashes=hashes, refs=refs,
                coverage={"total": 1})
            orphan = root / segments.SEGMENT_DIR / "future-orphan.bin"
            orphan.write_bytes(b"orphan")
            os.utime(orphan, (2_000, 2_000))

            inventory = segments.orphan_artifacts(
                current, grace_seconds=300, now=1_000)

            self.assertEqual(inventory["count"], 1)
            self.assertEqual(inventory["bytes"], 6)
            self.assertEqual(inventory["paths"], (orphan,))

    def test_streaming_validator_schema_failure_closes_connection(self) -> None:
        connection = mock.Mock()
        connection.executescript.side_effect = RuntimeError("injected")
        with mock.patch.object(
                segments.sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                segments._validate_active_rows_streaming(
                    {"physical_rows": 0, "live_rows": 0})
        connection.close.assert_called_once_with()

    def test_shadow_liveness_uses_one_byte_per_physical_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / segments.SEGMENT_DIR
            directory.mkdir()
            shadow = directory / "shadow.bin"
            rows = 100_000
            shadow.write_bytes(b"".join(
                struct.pack("<Q", value) for value in range(rows)))
            manifest = segments.LoadedManifest({
                "physical_rows": 1_000_000,
                "shadows": [{
                    "before_row_ref": 1_000_000,
                    "rows": rows,
                    "artifact": {
                        "path": f"{segments.SEGMENT_DIR}/{shadow.name}",
                    },
                }],
            }, root / "embeddings.meta")
            dead = segments._dead_row_mask(manifest)
        self.assertIsInstance(dead, bytearray)
        self.assertEqual(len(dead), 1_000_000)
        self.assertEqual(sum(dead), rows)


if __name__ == "__main__":
    unittest.main()
