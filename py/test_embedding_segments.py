from __future__ import annotations
from _test_support import isolate_data_dir

isolate_data_dir()

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import common
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

    def test_q8_payload_damage_is_caught_without_a_whole_file_python_pass(
            self) -> None:
        # Publication SHA-256s every artifact, so the per-byte Python FNV pass
        # over the payloads cost ~3 s per 100k rows twice per publish and added
        # no integrity. Damage must still be rejected, with no whole-file read.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            real_read_bytes = Path.read_bytes

            def no_whole_q8(path: Path):
                if path.suffix in (".q8", ".q8g"):
                    raise AssertionError(f"whole-file read of {path.name}")
                return real_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", autospec=True,
                                   side_effect=no_whole_q8):
                manifest = segments.publish_base(
                    meta, source={"ingest_signature": "one"}, model_id="model",
                    dim=2, artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                    refs=refs, coverage={"total": 2})
                segments.load_manifest(
                    meta, verify_hashes=True, validate_liveness=True)
            q8 = segments.artifact_path(
                manifest, manifest["segments"][0]["artifacts"]["q8"])
            intact = q8.read_bytes()
            payload = bytearray(intact)
            payload[64 + 3] ^= 0x01
            q8.write_bytes(payload)
            with self.assertRaisesRegex(segments.SegmentError, "digest mismatch"):
                segments.load_manifest(
                    meta, verify_hashes=True, validate_liveness=True)
            q8.write_bytes(intact)

            # The same damage on a staged input is refused before publication.
            staged, hashes, refs = _inputs(root, "delta", ["c"])
            damaged = bytearray(staged["q8"].read_bytes())
            damaged[64 + 1] ^= 0x01
            original_bundle = segments._bundle

            def corrupt_after_bundle(*args, **kwargs):
                record, group_count = original_bundle(*args, **kwargs)
                (meta.parent / record["artifacts"]["q8"]["path"]).write_bytes(
                    damaged)
                return record, group_count

            with mock.patch.object(
                    segments, "_bundle", side_effect=corrupt_after_bundle), \
                    self.assertRaisesRegex(segments.SegmentError, "digest mismatch"):
                segments.publish_delta(
                    meta, source={"ingest_signature": "one"}, artifacts=staged,
                    ids=["c"], hashes=hashes, refs=refs, shadows=[],
                    coverage={"total": 3},
                    expected_generation=manifest["generation"])

            # A group id past the declared count is still a structural refusal.
            bad, hashes, refs = _inputs(root, "bad", ["d"])
            groups = bytearray(bad["groups"].read_bytes())
            struct.pack_into("<I", groups, 64, 7)
            bad["groups"].write_bytes(groups)
            with self.assertRaisesRegex(
                    segments.SegmentError, "group id exceeds"):
                segments.publish_delta(
                    meta, source={"ingest_signature": "one"}, artifacts=bad,
                    ids=["d"], hashes=hashes, refs=refs, shadows=[],
                    coverage={"total": 3},
                    expected_generation=manifest["generation"])

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

    def _preparation(self, root: Path, name: str, *, owner: dict | None,
                     age_s: float = 0.0, payload: int = 1024) -> Path:
        path = root / name
        path.mkdir()
        (path / "segment.f32").write_bytes(b"\0" * payload)
        if owner is not None:
            (path / ".owner").write_text(json.dumps(owner), encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_abandoned_preparations_follow_the_owner_record(self) -> None:
        # A killed or OOM'd builder skips every handler and nothing scanned
        # the prefix: 2.7 KB per row leaked on every interrupted full build.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            me = {"pid": os.getpid(),
                  "process_start": str(common.process_start_identity(
                      os.getpid()))}
            with mock.patch.object(
                    common, "pid_alive",
                    side_effect=lambda pid: pid == os.getpid()):
                live = self._preparation(
                    root, ".semantic-segment-live", owner=me, age_s=90_000)
                dead = self._preparation(
                    root, ".semantic-segment-dead",
                    owner={"pid": 999_999_999, "process_start": "gone"},
                    payload=4096)
                recycled = self._preparation(
                    root, ".semantic-compaction-recycled",
                    owner={**me, "process_start": "another birth"})
                young = self._preparation(
                    root, ".semantic-segment-young", owner=None, age_s=60)
                stale = self._preparation(
                    root, ".semantic-compaction-stale", owner=None, age_s=7200)
                (root / ".semantic-segment-file").write_bytes(b"not a dir")
                if os.name != "nt":
                    os.symlink(dead, root / ".semantic-segment-link")
                found = segments.abandoned_preparations(root)
                self.assertEqual(
                    found["paths"], (recycled, stale, dead))
                self.assertEqual(found["count"], 3)
                self.assertGreaterEqual(found["bytes"], 4096 + 1024 + 1024)

                # An unrecorded, stale directory stays while its kind of
                # builder still holds the claim: it may be that builder's.
                (root / ".semantic-compaction.lock").write_text(
                    json.dumps({**me, "token": "t"}), encoding="utf-8")
                self.assertEqual(
                    segments.abandoned_preparations(root)["paths"],
                    (recycled, dead))

                done = segments.prune_abandoned_preparations(root)
            self.assertEqual(done["removed"], [recycled, dead])
            self.assertEqual(done["deferred"], [])
            self.assertEqual(
                sorted(p.name for p in root.iterdir() if p.is_dir()),
                [".semantic-compaction-stale", ".semantic-segment-live",
                 ".semantic-segment-young"])
            self.assertTrue(live.exists() and young.exists() and stale.exists())

    def test_prepare_stamps_its_owner_and_cleans_on_interrupt(self) -> None:
        import semantic_segment_build
        import types
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen: dict[str, object] = {}

            def interrupted(path: Path, *_args, **_kwargs):
                owner = json.loads((path.parent / ".owner").read_text())
                seen["dir"] = path.parent
                seen["owner"] = owner
                raise KeyboardInterrupt

            with mock.patch.object(semantic_segment_build.common, "DATA_DIR", root), \
                    mock.patch.object(
                        semantic_segment_build, "_require_writable_data_dir"), \
                    mock.patch.object(
                        semantic_segment_build, "_write_matrix",
                        side_effect=interrupted), \
                    self.assertRaises(KeyboardInterrupt):
                semantic_segment_build.prepare(
                    [], ["a"], ["h"], [types.SimpleNamespace(id="a")],
                    dim=2, model_id="model")
            self.assertEqual(seen["owner"]["pid"], os.getpid())
            self.assertTrue(seen["dir"].name.startswith(".semantic-segment-"))
            self.assertFalse(seen["dir"].exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_live_builder_with_unreadable_birth_is_not_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            me = {"pid": os.getpid(), "process_start": "darwin_birth"}
            fresh = self._preparation(root, ".semantic-segment-fresh", owner=me)
            old = self._preparation(root, ".semantic-segment-old", owner=me,
                                    age_s=7200)
            with mock.patch.object(common, "pid_alive", return_value=True), \
                    mock.patch.object(common, "process_start_identity",
                                      return_value=None):
                found = segments.abandoned_preparations(root)
            # Unknown liveness falls through to age + claim: only the stale
            # one with no live claim of its kind is reclaimable.
            self.assertEqual(found["paths"], (old,))
            self.assertTrue(fresh.exists())

    def test_embed_pass_and_doctor_reclaim_abandoned_preparations(self) -> None:
        import corpusdb
        import embed
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dead = {"pid": 999_999_999, "process_start": "gone"}
            with mock.patch.object(embed.common, "DATA_DIR", root), \
                    mock.patch.object(doctor.common, "DATA_DIR", root), \
                    mock.patch.object(doctor.common, "EMBEDDINGS_PATH",
                                      root / "embeddings.meta"), \
                    mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"), \
                    mock.patch.object(
                        common, "pid_alive", return_value=False), \
                    mock.patch.object(embed, "_data_dir_readonly",
                                      return_value=False), \
                    mock.patch.object(embed.common, "log") as log:
                self._preparation(root, ".semantic-segment-a", owner=dead)
                inventory = doctor._orphan_inventory(deep=False)
                self.assertEqual(
                    inventory["semantic_preparations"]["count"], 1)
                self.assertEqual(inventory["count"], 1)
                rows: list[tuple] = []
                with mock.patch.object(
                        doctor, "_row",
                        side_effect=lambda *a: rows.append(a)):
                    doctor._abandoned_preparation_row(
                        inventory["semantic_preparations"], fix_actions=False)
                self.assertEqual(rows[0][:2], ("semantic staging", doctor.WARN))
                self.assertIn("1 abandoned semantic build dir", rows[0][2])
                self.assertIn("doctor --fix", rows[0][2])
                self.assertTrue((root / ".semantic-segment-a").exists())

                embed._prune_loaded_segment_orphans(None)
                self.assertFalse((root / ".semantic-segment-a").exists())
                self.assertIn("reclaimed 1 abandoned preparation dir",
                              log.call_args.args[0])

                self._preparation(root, ".semantic-compaction-b", owner=dead)
                rows.clear()
                with mock.patch.object(
                        doctor, "_row",
                        side_effect=lambda *a: rows.append(a)):
                    doctor._abandoned_preparation_row(
                        doctor._orphan_inventory(deep=False)[
                            "semantic_preparations"], fix_actions=True)
                self.assertEqual(rows[0][:2], ("semantic staging", doctor.OK))
                self.assertFalse((root / ".semantic-compaction-b").exists())

    def test_disk_full_is_named_held_and_not_re_embedded(self) -> None:
        import errno
        import embed
        import semantic_segment_build as build
        import surface_policy as surface
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # numpy's tofile raised "N requested and 0 written" with no errno;
            # the buffer write keeps ENOSPC so the failure can be classified.
            full = OSError(errno.ENOSPC, "No space left on device")
            stream = mock.MagicMock()
            stream.__enter__.return_value.write.side_effect = full
            with mock.patch.object(build, "_require_writable_data_dir"), \
                    mock.patch.object(Path, "open", return_value=stream), \
                    self.assertRaisesRegex(
                        build.SegmentDiskFull,
                        r"no space left on device \(\d+ needed, \d+ free\) at ") \
                    as caught:
                build._write_matrix(
                    root / "segment.f32",
                    [np.zeros((3, 2), dtype=np.float32)], 2)
            self.assertIsNotNone(surface.host_block(str(caught.exception)))
            self.assertEqual(caught.exception.errno, errno.ENOSPC)

            usage = mock.Mock(free=1024)
            with mock.patch.object(build.shutil, "disk_usage", return_value=usage), \
                    self.assertRaisesRegex(build.SegmentDiskFull, "needed, 1024 free"):
                build.require_segment_capacity(root, 1000, 384)

            with mock.patch.object(embed, "_PRIOR_FAILURES", 0):
                record = embed._failure_record(
                    caught.exception, str(caught.exception))
            needed = build.segment_bytes(3, 2)
            self.assertEqual(record["disk_full"]["needed"], needed)
            with mock.patch.object(semantic.common, "DATA_DIR", root), \
                    mock.patch.object(semantic.shutil, "disk_usage",
                                      return_value=mock.Mock(free=needed - 1)):
                self.assertIn("no space left", semantic.disk_full_hold(record))
            with mock.patch.object(semantic.common, "DATA_DIR", root), \
                    mock.patch.object(semantic.shutil, "disk_usage",
                                      return_value=mock.Mock(free=needed)):
                self.assertIsNone(semantic.disk_full_hold(record))
            detail = doctor._semantic_failure_detail(
                {"embed_job": "failed", "live": False,
                 "embed_fail_reason": record["error"]},
                embeddings_off=False, lane=surface.SEMANTIC_LANE_POLICY)
            self.assertIn("free space on this volume", detail)
            self.assertNotIn("retries automatically", detail)

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
