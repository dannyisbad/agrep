from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_projection():
    path = Path(__file__).with_name("embedder_projection.py")
    spec = importlib.util.spec_from_file_location("agrep_embedder_projection_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _campaign(rows: int, dim: int, *, topup: int,
              q8_s: float, group_s: float, f16_s: float) -> dict:
    return {
        "rows": rows,
        "dim": dim,
        "storage": {
            "f32_bytes": rows * dim * 4,
            "f16_bytes": rows * dim * 2,
            "q8_bytes": 64 + rows * (dim + 4),
            "group_bytes": 64 + rows * 4,
            "q8_topup_bytes": 64 + (rows + topup) * (dim + 4),
        },
        "build": {
            "topup_rows": topup,
            "q8_full_rebuild_s": q8_s,
            "group_source_initial_s": group_s,
            "f16_initial_s": f16_s,
        },
    }


def _basis(dim: int = 384) -> tuple[dict, dict]:
    rows = 16_000
    measured = {
        "added_rows": 1_000,
        "published_rows": rows,
        "inferred_rows": 800,
        "source_rows": rows,
        "source_bytes": 16_000_000,
        "f32_bytes": rows * dim * 4,
        "ids_bytes": 640_000,
        "hashes_bytes": rows * 17,
        "phases_s": {
            "plan": 0.08,
            "load": 0.2,
            "inference": 2.0,
            "f32_publish": 0.1,
        },
    }
    report = {
        "schema": 2,
        "campaigns": [
            _campaign(100_000, dim, topup=1_000,
                      q8_s=1.01, group_s=0.1, f16_s=0.2),
            _campaign(1_000_000, dim, topup=1_000,
                      q8_s=10.01, group_s=1.0, f16_s=2.0),
            _campaign(2_000_000, dim, topup=1_000,
                      q8_s=20.01, group_s=2.0, f16_s=4.0),
        ],
    }
    return measured, report


class EmbedderProjectionTests(unittest.TestCase):
    def test_projects_separate_current_layout_components(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        result = projection.project_current_layout(
            dim=384, target_rows=10_000_000,
            measured_incremental=measured, q8_scale_report=q8)
        self.assertEqual(result["incremental_inference"]["added_rows"], 1000)
        self.assertEqual(result["incremental_inference"]["inferred_unique_rows"], 800)
        self.assertEqual(result["incremental_inference"]["projected_s"], 2.0)
        self.assertEqual(result["source_reconcile_and_plan"]["projected_s"], 50.0)
        self.assertEqual(result["full_f32_publication"]["projected_s"], 62.5)
        self.assertEqual(
            result["full_f32_publication"]["target_f32_bytes_exact"],
            10_000_000 * 384 * 4)
        self.assertEqual(
            result["full_f32_publication"]["target_hash_bytes_exact"],
            10_000_000 * 17)
        accelerator = result["accelerator_full_rebuild"]
        self.assertEqual(accelerator["target_q8_bytes_exact"],
                         64 + 10_000_000 * 388)
        self.assertEqual(accelerator["target_group_bytes_exact"],
                         64 + 10_000_000 * 4)
        self.assertEqual(accelerator["target_exact_f16_bytes_exact"],
                         10_000_000 * 384 * 2)
        self.assertEqual(accelerator["q8_group_derivation"]["projected_s"], 100.0)
        self.assertEqual(result["modeled_components_sum_s"], 244.7)
        self.assertNotIn("wall_multiplier", result)
        self.assertIn("never a current-wall multiplier", result["composition"])

    def test_dimension_changes_every_exact_artifact_width(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis(256)
        result = projection.project_current_layout(
            dim=256, measured_incremental=measured,
            q8_scale_report=q8, target_rows=1_000_000)
        accelerator = result["accelerator_full_rebuild"]
        self.assertEqual(accelerator["target_q8_bytes_exact"],
                         64 + 1_000_000 * 260)
        self.assertEqual(accelerator["target_exact_f16_bytes_exact"],
                         1_000_000 * 512)

    def test_full_rebuild_projection_is_separate_from_delta_inference(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        incremental = projection.project_current_layout(
            dim=384, measured_incremental=measured,
            q8_scale_report=q8, target_rows=10_000_000)
        full = projection.project_full_rebuild(
            dim=384,
            measured_full={
                "published_rows": 16_000,
                "inferred_rows": 12_000,
                "phases_s": measured["phases_s"],
            },
            accelerator_projection=incremental,
            target_rows=10_000_000,
        )
        self.assertEqual(full["projected_inferred_unique_rows"], 7_500_000)
        self.assertEqual(full["components"]["inference_s"], 1250.0)
        self.assertEqual(incremental["incremental_inference"]["projected_s"], 2.0)

    def test_full_rebuild_rejects_mismatched_accelerator_projection(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        incremental = projection.project_current_layout(
            dim=384, measured_incremental=measured, q8_scale_report=q8)
        with self.assertRaisesRegex(projection.ProjectionError, "drifted"):
            projection.project_full_rebuild(
                dim=256,
                measured_full={
                    "published_rows": 16_000,
                    "inferred_rows": 12_000,
                    "phases_s": measured["phases_s"],
                },
                accelerator_projection=incremental,
            )

    def test_rejects_single_size_q8_projection(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        q8["campaigns"] = q8["campaigns"][:1]
        with self.assertRaisesRegex(projection.ProjectionError, "at least two"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)

    def test_rejects_wrong_dimension_or_artifact_size(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        q8["campaigns"][0]["dim"] = 256
        with self.assertRaisesRegex(projection.ProjectionError, "selected dimension"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)
        measured, q8 = _basis()
        q8["campaigns"][0]["storage"]["q8_topup_bytes"] += 1
        with self.assertRaisesRegex(projection.ProjectionError, "q8_topup_bytes"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)

    def test_rejects_incoherent_incremental_generation(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        measured["source_rows"] -= 1
        with self.assertRaisesRegex(projection.ProjectionError, "full-coverage"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)
        measured, q8 = _basis()
        measured["hashes_bytes"] -= 1
        with self.assertRaisesRegex(projection.ProjectionError, "hash size"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)

    def test_rejects_non_finite_or_unknown_measurements(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        measured["phases_s"]["inference"] = float("nan")
        with self.assertRaisesRegex(projection.ProjectionError, "finite"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)
        measured, q8 = _basis()
        measured["surprise"] = 1
        with self.assertRaisesRegex(projection.ProjectionError, "unknown surprise"):
            projection.project_current_layout(
                dim=384, measured_incremental=measured, q8_scale_report=q8)

    def test_cli_rejects_duplicate_json_keys(self) -> None:
        projection = _load_projection()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "input.json"
            path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
            self.assertEqual(projection.main(["--input", str(path)]), 1)

    def test_cli_writes_a_complete_projection(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        record = {
            "schema": 1,
            "dim": 384,
            "target_rows": 10_000_000,
            "measured_incremental": measured,
            "q8_scale_report": q8,
        }
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "input.json"
            output = Path(raw) / "output.json"
            source.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(projection.main([
                "--input", str(source), "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["schema"], 1)
            self.assertFalse(output.with_name(".output.json.tmp").exists())

    def test_record_contract_is_strict(self) -> None:
        projection = _load_projection()
        measured, q8 = _basis()
        record = {
            "schema": 1,
            "dim": 384,
            "target_rows": 10_000_000,
            "measured_incremental": measured,
            "q8_scale_report": q8,
        }
        result = projection.project_from_record(json.loads(json.dumps(record)))
        self.assertEqual(result["target_rows"], 10_000_000)
        record["schema"] = 2
        with self.assertRaisesRegex(projection.ProjectionError, "schema"):
            projection.project_from_record(record)


if __name__ == "__main__":
    unittest.main()
