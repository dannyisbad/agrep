"""Mechanical tests for the semantic scale gate's fixtures and math."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "semantic_q8_scale.py"
SPEC = importlib.util.spec_from_file_location("semantic_q8_scale_bench", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
scale = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale)


class SemanticScaleTests(unittest.TestCase):
    def test_append_continues_the_same_deterministic_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            complete = root / "complete.f32"
            appended = root / "appended.f32"
            seed, _ = scale._write_f32(complete, 12_345, 24)
            scale._write_f32(appended, 10_003, 24)
            scale._append_f32(appended, 10_003, 2_342, 24, seed)
            self.assertEqual(complete.read_bytes(), appended.read_bytes())

    def test_adversarial_groups_include_one_giant_session(self) -> None:
        groups = scale._adversarial_groups(1_000)
        self.assertEqual(int(np.count_nonzero(groups == 0)), 750)
        self.assertEqual(len(set(map(int, groups))), 64)

    def test_row_overfetch_cannot_preserve_adversarial_session_head(self) -> None:
        rows = 1_000
        groups = scale._adversarial_groups(rows)
        scores = np.linspace(1, 0, rows, dtype=np.float32)
        oracle = scale._session_page(scores, groups)
        row_candidates = scale._top_indices(scores, scale.CANDIDATE_K)
        page = scale._candidate_page(
            row_candidates, scores[row_candidates], groups, scale.TOP_K)
        recall = scale._candidate_recall(oracle, page, scale.TOP_K)
        self.assertLess(recall, 0.5)

    def test_parity_reports_retrieval_drift_not_only_error(self) -> None:
        expected = np.linspace(0, 1, 100, dtype=np.float32)
        actual = expected.copy()
        actual[99], actual[20] = actual[20], actual[99]
        parity = scale._parity(expected, actual)
        self.assertFalse(parity["top1_same"])
        self.assertLess(parity["top10_overlap"], 1.0)
        self.assertLess(parity["top40_overlap"], 1.0)
        self.assertGreater(parity["max_abs_error"], 0.5)

    def test_sorted_rerank_restores_candidate_score_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "matrix.f16"
            matrix = np.asarray([
                [1, 0], [0, 1], [0.5, 0.5], [-1, 0],
            ], dtype="<f2")
            path.write_bytes(matrix.tobytes())
            ordinals = np.asarray([3, 0, 2], dtype=np.int64)
            query = np.asarray([1, 0], dtype=np.float32)
            scores = scale._rerank(path, "<f2", 4, 2, query, ordinals)
            np.testing.assert_allclose(scores, [-1, 1, 0.5])

    def test_group_subset_keeps_every_head_for_selected_groups(self) -> None:
        groups = np.asarray([0, 0, 1, 1, 2, 2], dtype=np.uint32)
        ordinals = np.asarray([0, 2, 4, 1, 3, 5], dtype=np.uint64)
        scores = np.asarray([.9, .8, .7, .6, .5, .4], dtype=np.float32)
        subset = scale._group_candidate_subset(
            ordinals, scores, groups, count=2)
        self.assertEqual(set(map(int, subset)), {0, 1, 2, 3})

    def test_single_point_projection_discloses_through_origin_fallback(self) -> None:
        campaign = {
            "rows": 2_000_000,
            "dim": 384,
            "latency": {
                "q8_grouped_top128x8": {"max_ms": 30},
                "q8_f16_retrieval": {"max_ms": 40},
            },
            "cold": {"q8_process_cold_ms": 100},
            "build": {"q8_initial_s": 3, "q8_full_rebuild_s": 3.1},
            "memory": {"warm_query_peak": {"private_mib": 31.629}},
        }
        report = scale._projections([campaign])
        projected = report["targets"]["10000000"]
        self.assertEqual(
            report["fits"]["q8_scan_max_ms"]["method"],
            "single-point-through-origin")
        self.assertAlmostEqual(projected["q8_private_affine_mib"], 158.145, places=3)
        self.assertGreaterEqual(projected["q8_private_mib"], 158.145)
        self.assertAlmostEqual(projected["q8_process_cold_ms"], 100, places=3)
        self.assertAlmostEqual(projected["q8_artifact_gib"], 3.613532, places=6)

    def test_multi_point_projection_uses_robust_affine_fit(self) -> None:
        campaigns = []
        for rows, scan in ((100_000, 5.2), (1_000_000, 7.0), (2_000_000, 9.0)):
            campaigns.append({
                "rows": rows,
                "dim": 384,
                "latency": {
                    "q8_grouped_top128x8": {"max_ms": scan},
                    "q8_f16_retrieval": {"max_ms": scan + 1},
                },
                "cold": {"q8_process_cold_ms": 6},
                "build": {"q8_initial_s": scan / 10,
                          "q8_full_rebuild_s": scan / 9},
                "memory": {"warm_query_peak": {"private_mib": 4}},
            })
        report = scale._projections(campaigns)
        fit = report["fits"]["q8_scan_max_ms"]
        self.assertEqual(fit["method"], "theil-sen-affine")
        self.assertAlmostEqual(fit["intercept"], 5.0, places=5)
        self.assertAlmostEqual(fit["per_million"], 2.0, places=5)
        self.assertAlmostEqual(
            report["targets"]["10000000"]["q8_scan_max_ms"], 25.0, places=3)

    def test_unique_group_private_bound_is_sixteen_bytes_per_row(self) -> None:
        campaigns = [{
            "rows": 1_000_000,
            "dim": 384,
            "latency": {
                "q8_grouped_top128x8": {"max_ms": 10},
                "q8_f16_retrieval": {"max_ms": 12},
            },
            "cold": {"q8_process_cold_ms": 5},
            "build": {"q8_initial_s": 2, "q8_full_rebuild_s": 2},
            "memory": {"warm_query_peak": {"private_mib": 10}},
        }]
        projected = scale._projections(campaigns)["targets"]["10000000"]
        self.assertGreaterEqual(projected["q8_private_upper_mib"], 152.588)
        self.assertEqual(projected["q8_private_mib"],
                         projected["q8_private_upper_mib"])


if __name__ == "__main__":
    unittest.main()
