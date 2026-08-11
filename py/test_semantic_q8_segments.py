import hashlib
import io
import json
import struct
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np

import common
import semantic_q8


@contextmanager
def _temporary_directory():
    with tempfile.TemporaryDirectory() as raw:
        try:
            yield raw
        finally:
            semantic_q8.close_scanner()


class FakeProcess:
    def __init__(self, ready: bytes):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(ready)

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class FakeScanner:
    def __init__(self, ordinals):
        self.ordinals = np.asarray(ordinals, dtype=np.uint64)

    def top(self, _query, _generation, _k, *, grouped=False, heads=8, eligible=None):
        if heads <= 0:
            raise AssertionError("candidate head count must be positive")
        if eligible is not None:
            allowed = set(np.flatnonzero(eligible) if np.asarray(eligible).dtype == np.bool_
                          else map(int, eligible))
            values = np.asarray(
                [row for row in self.ordinals if int(row) in allowed], dtype=np.uint64)
        else:
            values = self.ordinals.copy()
        return values, np.arange(len(values), dtype=np.float32)


class SegmentFixture:
    def __init__(self, root: Path):
        self.root = root
        self.artifacts = root / "segments"
        self.artifacts.mkdir()
        self.generation = "4a" * 16
        self.dim = 2
        self.segments = []
        self._segment("a", 0, np.asarray(
            [[1.0, 0.0], [0.5, 0.5]], dtype="<f2"), [0, 1], "11" * 16)
        self._segment("b", 100, np.asarray(
            [[1.0, 0.0], [0.0, 1.0]], dtype="<f2"), [2, 3], "22" * 16)
        native = {
            "version": 1,
            "generation": self.generation,
            "dim": self.dim,
            "row_high_water": 102,
            "live_rows": 4,
            "group_count": 4,
            "segments": [{
                "row_base": segment["row_base"],
                "rows": segment["rows"],
                "artifact": Path(segment["artifacts"]["q8"]["path"]).name,
                "groups": Path(segment["artifacts"]["groups"]["path"]).name,
            } for segment in self.segments],
            "shadows": [],
        }
        self.set_path = self.artifacts / "set.json"
        set_manifest = self._artifact(
            "set.json", json.dumps(native).encode("utf-8"))
        self.record = {
            "version": 2,
            "generation": self.generation,
            "source": {"generation": "source"},
            "model": {"id": "fixture", "dim": self.dim},
            "coverage": {
                "indexed": 4, "total": 7, "pending": 3, "complete": False,
            },
            "segments": self.segments,
            "live_rows": 4,
            "physical_rows": 4,
            "next_row_ref": 102,
            "group_count": 4,
            "set_manifest": set_manifest,
        }

    def _artifact(self, name: str, payload: bytes) -> dict:
        path = self.artifacts / name
        path.write_bytes(payload)
        return {
            "path": f"segments/{name}",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _segment(self, name: str, row_base: int, exact: np.ndarray,
                 groups: list[int], generation: str) -> None:
        rows = len(exact)
        q8 = bytes(64 + rows * (self.dim + 4))
        group_payload = b"".join(struct.pack("<I", group) for group in groups)
        group_header = semantic_q8._GROUP_HEADER.pack(
            b"AGQG", 1, 0, max(groups) + 1, rows,
            bytes.fromhex(generation), 4, 0, 0, b"\0" * 8)
        self.segments.append({
            "id": name,
            "row_base": row_base,
            "rows": rows,
            "artifacts": {
                "q8": self._artifact(f"{name}.q8", q8),
                "groups": self._artifact(
                    f"{name}.q8g", group_header + group_payload),
                "f16": self._artifact(f"{name}.f16", exact.tobytes()),
            },
        })


class SemanticQ8SegmentTests(unittest.TestCase):
    def setUp(self):
        semantic_q8.close_scanner()

    def tearDown(self):
        semantic_q8.close_scanner()

    def _fixture(self, root: Path):
        fixture = SegmentFixture(root)
        patcher = mock.patch.object(
            common, "EMBEDDINGS_PATH", root / "embeddings.f32")
        return fixture, patcher

    def test_v2_manifest_validates_native_and_rich_segment_ranges(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest["storage_version"], 2)
            self.assertEqual(manifest["rows"], 102)
            self.assertEqual(manifest["live_rows"], 4)
            self.assertEqual(
                [segment["row_base"] for segment in manifest["segments"]],
                [0, 100])

    def test_native_scanner_launches_set_and_checks_set_generation(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
            ready = semantic_q8._READY.pack(
                b"AQ8R", semantic_q8.PROTOCOL, 102, 2,
                bytes.fromhex(fixture.generation))
            with mock.patch.object(
                    semantic_q8.subprocess, "Popen",
                    return_value=FakeProcess(ready)) as popen:
                scanner = semantic_q8._Q8Scanner(
                    manifest, binary=Path("fake-agrep-rs"))
                scanner.close()
            command = popen.call_args.args[0]
            self.assertEqual(command[:3], [
                "fake-agrep-rs", "semantic-q8-serve", "--set"])
            self.assertEqual(Path(command[3]), fixture.set_path.resolve())
            self.assertNotIn("--artifact", command)

    def test_native_scanner_sends_fixed_global_eligibility_mask(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
            generation = bytes.fromhex(fixture.generation)
            candidates = np.zeros(2, dtype=semantic_q8._CANDIDATE)
            candidates["ordinal"] = [101, 0]
            candidates["score"] = [1.0, 0.5]
            response = semantic_q8._RESPONSE.pack(
                b"AQ8T", semantic_q8.PROTOCOL, 0, 0, 2, generation)
            process = FakeProcess(semantic_q8._READY.pack(
                b"AQ8R", semantic_q8.PROTOCOL, 102, 2, generation)
                + response + candidates.tobytes())
            eligible = np.zeros(102, dtype=np.bool_)
            eligible[[0, 101]] = True
            with mock.patch.object(
                    semantic_q8.subprocess, "Popen", return_value=process):
                scanner = semantic_q8._Q8Scanner(
                    manifest, binary=Path("fake-agrep-rs"))
                ordinals, _ = scanner.top(
                    np.asarray([1.0, 0.0], dtype=np.float32), fixture.generation,
                    2, grouped=True, heads=1, eligible=eligible)
                request = process.stdin.getvalue()
            np.testing.assert_array_equal(ordinals, [101, 0])
            self.assertEqual(request[:4], b"AQ8H")
            mask_at = semantic_q8._GROUP_TOP_REQUEST.size + fixture.dim * 4
            self.assertEqual(len(request), mask_at + 13)
            self.assertEqual(request[mask_at], 1)
            self.assertEqual(request[-1], 1 << 5)

    def test_exact_rerank_batches_segments_and_uses_global_stable_ties(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
                scanner = FakeScanner([101, 100, 1, 0])
                with (mock.patch.object(
                        semantic_q8, "_ready_manifest", return_value=manifest),
                      mock.patch.object(
                          semantic_q8, "_scanner_for_manifest",
                          return_value=scanner)):
                    result = semantic_q8.grouped_exact_candidates(
                        np.asarray([1.0, 0.0], dtype=np.float32),
                        fixture.generation, k=4, heads=2)
            self.assertIsNotNone(result)
            ordinals, scores, groups, count = result
            np.testing.assert_array_equal(ordinals, [0, 100, 1, 101])
            np.testing.assert_allclose(scores, [1.0, 1.0, 0.5, 0.0])
            np.testing.assert_array_equal(groups, [0, 2, 1, 3])
            self.assertEqual(count, 4)

    def test_exact_rerank_accepts_sparse_global_row_references(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
                scanner = FakeScanner([101, 100, 1, 0])
                with (mock.patch.object(
                        semantic_q8, "_ready_manifest", return_value=manifest),
                      mock.patch.object(
                          semantic_q8, "_scanner_for_manifest",
                          return_value=scanner)):
                    result = semantic_q8.grouped_exact_candidates(
                        np.asarray([1.0, 0.0], dtype=np.float32),
                        fixture.generation, k=4, heads=2, eligible=[101, 0])
            self.assertIsNotNone(result)
            ordinals, scores, groups, _ = result
            np.testing.assert_array_equal(ordinals, [0, 101])
            np.testing.assert_allclose(scores, [1.0, 0.0])
            np.testing.assert_array_equal(groups, [0, 3])

    def test_flat_exact_rerank_accepts_boolean_global_mask(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
                scanner = FakeScanner([101, 100, 1, 0])
                eligible = np.zeros(102, dtype=np.bool_)
                eligible[[1, 100]] = True
                with (mock.patch.object(
                        semantic_q8, "_ready_manifest", return_value=manifest),
                      mock.patch.object(
                          semantic_q8, "_scanner_for_manifest",
                          return_value=scanner)):
                    result = semantic_q8.exact_candidates(
                        np.asarray([1.0, 0.0], dtype=np.float32),
                        fixture.generation, k=4, eligible=eligible)
            self.assertIsNotNone(result)
            ordinals, scores = result
            np.testing.assert_array_equal(ordinals, [100, 1])
            np.testing.assert_allclose(scores, [1.0, 0.5])

    def test_generation_switch_closes_every_segment_mapping(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
                first = semantic_q8._segment_sidecars_for_manifest(manifest)
                mappings = [getattr(segment[name], "_mmap")
                            for segment in first.items
                            for name in ("exact", "groups")]
                moved = dict(manifest)
                moved["artifact_generation"] = "5b" * 16
                second = semantic_q8._segment_sidecars_for_manifest(moved)
            self.assertIsNot(first, second)
            self.assertTrue(all(mapping.closed for mapping in mappings))

    def test_coverage_uses_live_rows_not_sparse_row_high_water(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
            with mock.patch.object(
                    semantic_q8, "_ready_manifest", return_value=manifest):
                coverage = semantic_q8.accelerator_coverage(fixture.generation)
            self.assertEqual(coverage, {
                "indexed": 4, "total": 7, "pending": 3, "complete": False,
            })

    def test_native_rich_path_mismatch_is_rejected(self):
        with _temporary_directory() as raw:
            fixture, patcher = self._fixture(Path(raw))
            native = json.loads(fixture.set_path.read_text(encoding="utf-8"))
            native["segments"][0]["artifact"] = "b.q8"
            fixture.set_path.write_text(json.dumps(native), encoding="utf-8")
            with patcher:
                manifest = semantic_q8._validated_manifest(
                    {"commit": fixture.record})
            self.assertIsNone(manifest)


if __name__ == "__main__":
    unittest.main()
