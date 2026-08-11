from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_scale():
    path = Path(__file__).with_name("semantic_segment_scale.py")
    spec = importlib.util.spec_from_file_location("agrep_semantic_segment_scale_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SemanticSegmentScaleTests(unittest.TestCase):
    def test_windows_large_fixture_fails_before_allocating(self) -> None:
        scale = _load_scale()
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(scale, "WINDOWS", True):
            with self.assertRaisesRegex(RuntimeError, "NTFS sparse-file support"):
                scale.run_case(
                    Path(temporary), base_rows=10_000_000,
                    topup_rows=1_000, dim=384)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_sparse_base_real_delta_publication_contract(self) -> None:
        scale = _load_scale()
        with tempfile.TemporaryDirectory() as temporary:
            report = scale.run_case(
                Path(temporary), base_rows=100_000, topup_rows=32, dim=24)
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["base_immutable"])
        self.assertTrue(report["fresh_open_coherent"])
        self.assertTrue(report["base_publication_proven"])
        self.assertEqual(report["temp_leftovers"], [])
        self.assertLess(report["new_artifact_bytes"],
                        scale.BUDGETS["new_artifact_bytes"])

    def test_delta_quantized_artifacts_are_fully_valid(self) -> None:
        scale = _load_scale()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, ids, hashes = scale._prepare_delta(root, 17, 31)
            try:
                group_count = scale.segments._matrix_headers(
                    prepared.artifacts["q8"], prepared.artifacts["groups"],
                    17, 31, verify_payload=True)
                refs = prepared.refs
            finally:
                prepared.close()
        self.assertEqual(group_count, 6)
        self.assertEqual((len(ids), len(hashes), len(refs)), (17, 17, 17))


if __name__ == "__main__":
    unittest.main()
