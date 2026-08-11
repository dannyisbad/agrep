from __future__ import annotations

import json
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bench import embedder_federate as federate
from bench import embedder_selection as selection


ROOT = Path(__file__).resolve().parents[1]
FIXED_NS = 1_700_000_000_000_000_000


def _environment() -> dict:
    return {
        "schema": 1,
        "host": {
            "os": "darwin", "os_release": "fixture", "architecture": "arm64",
            "cpu_identity": "fixture cpu", "physical_cores": 4,
            "logical_cores": 8,
        },
        "runtime": {
            "python_implementation": "CPython", "python_version": "3.14.0",
            "onnxruntime_version": "1.23.0", "numpy_version": "2.3.0",
            "tokenizers_version": "0.22.0",
        },
        "execution": {
            "requested_provider": "CPUExecutionProvider",
            "actual_providers": ["CPUExecutionProvider"],
            "semantic_threads": 4, "process_affinity": None,
            "power_policy": "host-default-uncontrolled",
            "accelerator_policy": "cpu-only",
        },
    }


def _quality_protocol(task_digest: str) -> dict:
    return {
        "schema": 1, "task_digest": task_digest, "hits": 3,
        "cli": {
            "mode": "recall-json", "lanes": selection.QUALITY_LANES,
            "output_budget": selection.QUALITY_OUTPUT_BUDGET,
            "output_cap": 3, "auto_escalation": False,
        },
        "exclusions": {"self": True, "query_echo": False},
    }


def _performance_protocol(run_dir: Path, profile_root: Path) -> dict:
    query_tasks = [
        {"id": "real-0", "query": "meaning 0"},
        {"id": "real-1", "query": "meaning 1"},
    ]
    query_digest = federate._digest(query_tasks)
    return {
        "schema": 1, "query_count": 2, "query_task_digest": query_digest,
        "query_ids": [task["id"] for task in query_tasks],
        "lanes": selection.QUALITY_LANES, "query_hits": 3,
        "output_budget": selection.QUALITY_OUTPUT_BUDGET,
        "warmup_sample_policy": {
            "worker_reset_per_lane": True, "cold_samples_per_lane": 1,
            "warm_samples_per_lane": 1, "order": "frozen-task-order",
        },
        "top_up_rows": 1_000,
        "scale": {
            "rows": list(selection.Q8_SCALE_ROWS),
            "top_up_rows": selection.Q8_SCALE_TOP_UP_ROWS,
        },
        "resource_sample_interval_s": selection.SEMANTIC_RESOURCE_SAMPLE_S,
        "paths": {
            "run_dir": str(run_dir.resolve()),
            "snapshot": str((run_dir / "snapshot").resolve()),
            "profile_root": str(profile_root.resolve()),
            "prepared_bundle": str(
                (profile_root / "prepared-embedding-bundle").resolve()),
            "model_root": str((profile_root / "models").resolve()),
        },
    }


def _q8_campaign(rows: int, dim: int) -> dict:
    topup = selection.Q8_SCALE_TOP_UP_ROWS
    return {
        "rows": rows, "dim": dim,
        "storage": {
            "f32_bytes": rows * dim * 4,
            "f16_bytes": rows * dim * 2,
            "q8_bytes": 64 + rows * (dim + 4),
            "group_bytes": 64 + rows * 4,
            "q8_topup_bytes": 64 + (rows + topup) * (dim + 4),
        },
        "build": {
            "topup_rows": topup,
            "q8_full_rebuild_s": rows / 100_000.0,
            "group_source_initial_s": rows / 1_000_000.0,
            "f16_initial_s": rows / 500_000.0,
        },
    }


def _projection_evidence(
        dim: int, environment: dict) -> tuple[dict, dict]:
    rows = 1_000
    measured = {
        "added_rows": rows, "published_rows": rows, "inferred_rows": 800,
        "source_rows": rows, "source_bytes": 100_000,
        "f32_bytes": rows * dim * 4, "ids_bytes": 16_000,
        "hashes_bytes": rows * 17,
        "phases_s": {
            "plan": 0.01, "load": 0.01, "inference": 0.2,
            "f32_publish": 0.02,
        },
    }
    report = {
        "schema": 2,
        "provenance": {
            "os": "fixture", "machine": environment["host"]["architecture"],
            "cpu": environment["host"]["cpu_identity"],
            "logical_cpus": environment["host"]["logical_cores"],
            "physical_memory_bytes": 16 * 1024 ** 3,
            "python": environment["runtime"]["python_version"],
            "numpy": environment["runtime"]["numpy_version"],
            "git_commit": "a" * 40, "git_dirty": False,
            "rust_binary": str((ROOT / "target/release/agrep-rs").resolve()),
            "rust_binary_sha256": "f" * 64,
        },
        "campaigns": [
            _q8_campaign(rows, dim) for rows in selection.Q8_SCALE_ROWS
        ],
    }
    return measured, report


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _runtime(profile_id: str, dim: int = 8) -> dict:
    return {
        "schema": 1,
        "id": profile_id,
        "repo": "owner/model",
        "revision": "a" * 40,
        "license": "apache-2.0",
        "license_permissive": True,
        "dim": dim,
        "native_dim": dim,
        "max_seq": 128,
        "pooling": "masked_mean",
        "normalize": True,
        "query_prefix": "",
        "document_prefix": "",
        "layernorm_before_truncate": False,
        "quantization": "q8",
        "runtime": "onnxruntime",
        "provider": "CPUExecutionProvider",
        "files": {
            "model.onnx": {
                "remote_path": "model.onnx", "size": 11, "sha256": "1" * 64,
            },
            "tokenizer.json": {
                "remote_path": "tokenizer.json", "size": 7, "sha256": "2" * 64,
            },
        },
        "model_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "model_bytes": 11,
        "output": {"index": 0},
        "search_bands": {"floor": 0.82, "strong": 0.84},
    }


def _profile(profile_id: str, baseline: bool, dim: int = 8) -> dict:
    return {
        "id": profile_id,
        "baseline": baseline,
        "status": "runnable",
        "adoption_eligible": True,
        "runtime_profile": _runtime(profile_id, dim),
        "native_max_seq": 128,
        "platforms": ["all"],
    }


def _snapshot(path: Path) -> dict:
    path.mkdir(parents=True)
    (path / "messages.jsonl").write_text(
        json.dumps({"id": "m1", "text": "frozen"}) + "\n", encoding="utf-8")
    (path / "sessions.jsonl").write_text("\n", encoding="utf-8")
    (path / "corpus.db").write_bytes(b"sqlite fixture")
    for item in path.iterdir():
        os.utime(item, ns=(FIXED_NS, FIXED_NS))
    return selection._snapshot_inventory(path)


def _prepare_result(runtime: dict, profile_root: Path) -> dict:
    bundle = profile_root / "prepared-embedding-bundle"
    bundle.mkdir(parents=True)
    (bundle / "embedding.bin").write_bytes(b"prepared")
    inventory = selection._bundle_file_inventory(bundle, ["embedding.bin"])
    stats = {
        "rows": 1,
        "published_rows": 1,
        "inferred": 1,
        "reused": 0,
        "elapsed_s": 0.02,
        "phases_s": {
            "plan": 0.001, "load": 0.001, "inference": 0.0125,
            "f32_publish": 0.001,
        },
        "inference_s": 0.013,
        "raw_rows_per_s": 80.0,
        "dedup_effective_rows_per_s": 80.0,
    }
    environment = _environment()
    environment_digest = federate._digest(environment)
    return {
        "model": {
            "returncode": 0, "wall_ms": 2.0, "cpu_ms": 1.0,
            "peak_rss_mib": 20.0,
        },
        "session": {
            "returncode": 0, "wall_ms": 2.0, "cpu_ms": 1.0,
            "peak_rss_mib": 30.0,
            "actual_providers": ["CPUExecutionProvider"],
            "semantic_threads": 4,
        },
        "embed": {
            "returncode": 0, "wall_ms": 20.0, "cpu_ms": 15.0,
            "peak_rss_mib": 50.0,
            "benchmark_environment_digest": environment_digest,
        },
        "embed_stats": stats,
        "embedding_bundle": {
            "schema": 1,
            "inventory": inventory,
            "embedding": {"rows": 1, "files": ["embedding.bin"]},
        },
        "embed_skipped": False,
        "profile_digest": federate._digest(runtime),
        "model_bytes": runtime["model_bytes"],
        "model_graph_bytes": runtime["model_bytes"],
        "pinned_cache_bytes": sum(row["size"] for row in runtime["files"].values()),
        "campaign_unreferenced_model_bytes": 0,
        "dim": runtime["dim"],
        "corpus": {},
        "benchmark_environment": environment,
        "benchmark_environment_digest": environment_digest,
    }


def _calibration(profile_id: str, state: dict, runtime: dict,
                 baseline: bool) -> dict:
    real_score = 0.85 if baseline else 0.90
    gibberish_scores = [0.839, 0.819, 0.819, 0.819] if baseline else [
        0.70, 0.60, 0.60, 0.60,
    ]
    observations = [{
        "id": f"real-{index}", "kind": "real", "query": f"meaning {index}",
        "scores": [real_score], "rows": 1, "excluded_query_echoes": 0,
        "exit_ms": 1.0,
    } for index in range(7)]
    observations.extend({
        "id": f"gibberish-{index}", "kind": "gibberish", "query": f"noise {index}",
        "scores": [score], "rows": 1, "excluded_query_echoes": 0,
        "exit_ms": 1.0,
    } for index, score in enumerate(gibberish_scores))
    calculated = selection.calibrate_scores(observations)
    effective = {
        "floor": calculated["floor"], "strong": calculated["strong"],
    }
    return {
        "schema": 1,
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "profile": profile_id,
        "snapshot_digest": state["snapshot"]["digest"],
        "task_digest": state["inputs"]["calibration_digest"],
        "campaign_inputs": state["inputs"],
        "base_profile_digest": federate._digest(runtime),
        "calibration": calculated,
        "calibration_usable": True,
        "effective_search_bands": effective,
        "observations": observations,
    }


def _quality(profile_id: str, state: dict, runtime_digest: str,
             semantic_correct: int, hybrid_correct: int) -> dict:
    outcomes = []
    for index in range(20):
        for lane, correct in (
                ("semantic", index < semantic_correct),
                ("hybrid", index < hybrid_correct)):
            outcomes.append({
                "task": f"task-{index:02d}",
                "lane": lane,
                "correct": correct,
                "expected_rank": 1 if correct else None,
                "exit_ms": 10.0,
                "launcher_tree_cpu_ms": 5.0,
                "launcher_tree_peak_rss_mib": 20.0,
                "engine": "semantic",
                "hits": [{"evidence": f"answer {index}", "project": "fixture"}],
            })
    protocol = _quality_protocol(state["inputs"]["quality_digest"])
    return {
        "schema": 1,
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "profile": profile_id,
        "snapshot_digest": state["snapshot"]["digest"],
        "task_digest": state["inputs"]["quality_digest"],
        "campaign_inputs": state["inputs"],
        "runtime_profile_digest": runtime_digest,
        "protocol": protocol,
        "protocol_digest": federate._digest(protocol),
        "scores": {
            "semantic": {
                "correct": semantic_correct, "total": 20,
                "exit_ms": [10.0] * 20,
            },
            "hybrid": {
                "correct": hybrid_correct, "total": 20,
                "exit_ms": [10.0] * 20,
            },
        },
        "outcomes": outcomes,
    }


def _performance(profile_id: str, state: dict, runtime_digest: str,
                 prepared: dict, model_bytes: int, run_dir: Path) -> dict:
    samples = [{"exit_ms": 12.0}]
    profile_root = run_dir / "profiles" / profile_id
    protocol = _performance_protocol(run_dir, profile_root)
    environment = prepared["benchmark_environment"]
    projection_basis, q8_scale_report = _projection_evidence(8, environment)
    scale_record = selection._q8_scale_cache_record(
        q8_scale_report, dim=8, code_digest=state["inputs"]["code_digest"],
        benchmark_env=environment)
    scale_path = selection._q8_scale_cache_path(run_dir, 8)
    _write(scale_path, scale_record)
    top_up = {
        "rows": 1_000, "added_rows": 1_000,
        "fixture": {"sampled_rows": 1_000},
        "projection_basis": projection_basis,
    }
    current_projection = selection._projection_module().project_current_layout(
        dim=8, target_rows=10_000_000,
        measured_incremental=projection_basis,
        q8_scale_report=q8_scale_report)
    full_projection = selection._projection_module().project_full_rebuild(
        dim=8, target_rows=10_000_000,
        measured_full={
            "published_rows": prepared["embed_stats"]["published_rows"],
            "inferred_rows": prepared["embed_stats"]["inferred"],
            "phases_s": prepared["embed_stats"]["phases_s"],
        },
        accelerator_projection=current_projection)
    return {
        "schema": 1,
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "profile": profile_id,
        "snapshot_digest": state["snapshot"]["digest"],
        "campaign_inputs": state["inputs"],
        "runtime_profile_digest": runtime_digest,
        "query_task_digest": protocol["query_task_digest"],
        "protocol": protocol, "protocol_digest": federate._digest(protocol),
        "benchmark_environment": prepared["benchmark_environment"],
        "benchmark_environment_digest": prepared["benchmark_environment_digest"],
        "requested_provider": "CPUExecutionProvider",
        "actual_session_providers": ["CPUExecutionProvider"],
        "session_load_peak_rss_mib": 50.0,
        "accelerator_used": False,
        "query_exit_ms": {
            "semantic": {
                "cold": {"exit_ms": 20.0}, "warm_samples": samples,
                "warm_median": 12.0, "warm_p90": 12.0, "warm_max": 12.0,
            },
            "hybrid": {
                "cold": {"exit_ms": 20.0}, "warm_samples": samples,
                "warm_median": 12.0, "warm_p90": 12.0, "warm_max": 12.0,
            },
        },
        "query_resource_scope": "fixture",
        "full_rebuild": {
            **prepared["embed_stats"],
            "measured_stage": "prepare",
            "wall_ms": prepared["embed"]["wall_ms"],
            "cpu_ms": prepared["embed"]["cpu_ms"],
            "peak_rss_mib": prepared["embed"]["peak_rss_mib"],
        },
        "top_up": top_up,
        "model_bytes": model_bytes,
        "model_graph_bytes": model_bytes,
        "pinned_cache_bytes": model_bytes + 7,
        "campaign_unreferenced_model_bytes": 0,
        "model_cache_bytes": model_bytes + 7,
        "index_projection_10m": selection.index_projection(8),
        "q8_scale_evidence": {
            "cache_path": str(scale_path),
            "cache_sha256": selection._file_digest(scale_path),
            "record": scale_record,
            "record_digest": federate._digest(scale_record),
        },
        "current_layout_top_up_projection_10m": current_projection,
        "full_rebuild_projection_10m": full_projection,
        "semantic_resources": {
            "semantic_resident_rss_mib": 100.0,
            "semantic_peak_rss_mib": 120.0,
        },
        "platform": {"os": "fixture", "python": "3"},
        "quality_fixture_tasks": 20,
    }


def _make_source(root: Path, name: str, profile_id: str, *, baseline: bool,
                 shared_snapshot: Path | None = None, code_digest: str = "c" * 64,
                 semantic_correct: int = 15) -> tuple[Path, Path]:
    run_dir = root / name
    snapshot_dir = run_dir / "snapshot"
    if shared_snapshot is None:
        snapshot = _snapshot(snapshot_dir)
    else:
        shutil.copytree(shared_snapshot, snapshot_dir, copy_function=shutil.copy2)
        snapshot = selection._snapshot_inventory(snapshot_dir)
    included = _profile(profile_id, baseline)
    manifest_profiles = [included]
    if not baseline:
        manifest_profiles.append(_profile(f"manifest-baseline-{name}", True))
    manifest = {"schema": 1, "profiles": manifest_profiles}
    manifest_path = root / f"{name}-manifest.json"
    _write(manifest_path, manifest)
    inputs = {
        "manifest_digest": federate._digest(manifest),
        "calibration_digest": "a" * 64,
        "quality_digest": "b" * 64,
        "code_digest": code_digest,
    }
    runtime = included["runtime_profile"]
    profile_root = run_dir / "profiles" / profile_id
    prepared = _prepare_result(runtime, profile_root)
    state = {
        "schema": 1,
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "created_at": 1,
        "inputs": inputs,
        "snapshot": snapshot,
        "profiles": [profile_id],
        "benchmark_environments": {
            profile_id: prepared["benchmark_environment"]},
        "prepare": {profile_id: prepared},
    }
    _write(run_dir / "state.json", state)
    _write(profile_root / "prepare.json", {
        "schema": 1,
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "profile": profile_id,
        "snapshot_digest": snapshot["digest"],
        "campaign_inputs": inputs,
        "runtime_profile_digest": federate._digest(runtime),
        "result": prepared,
    })
    calibration = _calibration(profile_id, state, runtime, baseline)
    _write(profile_root / "calibration.json", calibration)
    calibrated = json.loads(json.dumps(runtime))
    calibrated["search_bands"] = calibration["effective_search_bands"]
    _write(profile_root / "profile-calibrated.json", calibrated)
    runtime_digest = federate._digest(calibrated)
    _write(
        profile_root / "quality.json",
        _quality(
            profile_id, state, runtime_digest,
            14 if baseline else semantic_correct, 19,
        ),
    )
    _write(
        profile_root / "performance.json",
        _performance(profile_id, state, runtime_digest, prepared, 11, run_dir),
    )
    return run_dir, manifest_path


def _quality_fixture(path: Path) -> None:
    tasks = [{
        "id": f"task-{index:02d}",
        "category": "fixture",
        "query": f"query {index}",
        "target": f"target {index}",
        "expected": [f"session:{index}"],
    } for index in range(20)]
    _write(path, tasks)


class EmbedderFederationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        first = _make_source(
            self.root, "first", "baseline", baseline=True,
        )
        second = _make_source(
            self.root, "second", "candidate", baseline=False,
            shared_snapshot=first[0] / "snapshot",
        )
        self.specs = [first, second]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _bind_quality_fixture(self) -> Path:
        quality = self.root / "quality.json"
        _quality_fixture(quality)
        digest = federate._digest(json.loads(quality.read_text(encoding="utf-8")))
        for run_dir, _manifest in self.specs:
            state_path = run_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["inputs"] = {**state["inputs"], "quality_digest": digest}
            profile_id = state["profiles"][0]
            root = run_dir / "profiles" / profile_id
            for name in ("prepare.json", "calibration.json", "quality.json", "performance.json"):
                artifact = json.loads((root / name).read_text(encoding="utf-8"))
                artifact["campaign_inputs"] = state["inputs"]
                if name == "quality.json":
                    artifact["task_digest"] = digest
                    artifact["protocol"]["task_digest"] = digest
                    artifact["protocol_digest"] = federate._digest(
                        artifact["protocol"])
                _write(root / name, artifact)
            _write(state_path, state)
        return quality

    def _blind_inputs(self, rating: int = 1) -> tuple[Path, Path, Path, dict]:
        quality = self._bind_quality_fixture()
        federation = federate.validate_campaigns(self.specs)
        _packet, private = federate.build_blind_packet(federation, quality)
        private_path = self.root / "private.json"
        _write(private_path, private)
        judgments = [{
            "task": task_id,
            "ratings": {label: rating for label in labels},
        } for task_id, labels in private["mapping"].items()]
        scores = {
            "schema": 1,
            "federation_digest": federation["public"]["federation_digest"],
            "blind_digest": private["blind_digest"],
            "judgments": judgments,
        }
        scores_path = self.root / "scores.json"
        _write(scores_path, scores)
        return private_path, scores_path, quality, federation

    def test_validate_is_deterministic_and_preserves_provenance(self) -> None:
        first = federate.validate_campaigns(self.specs)["public"]
        second = federate.validate_campaigns(self.specs)["public"]
        self.assertEqual(first, second)
        self.assertEqual(first, federate.validate_campaigns(list(reversed(self.specs)))["public"])
        self.assertEqual(first["baseline"], "baseline")
        self.assertEqual(first["selected_profile_ids"], ["baseline", "candidate"])
        self.assertNotEqual(
            first["profiles"]["baseline"]["source_campaign_inputs"]["manifest_digest"],
            first["profiles"]["candidate"]["source_campaign_inputs"]["manifest_digest"],
        )
        self.assertRegex(first["federation_digest"], r"^[0-9a-f]{64}$")

    def test_report_is_deterministic_and_marks_blind_pending(self) -> None:
        federation = federate.validate_campaigns(self.specs)
        first = federate.build_report(federation, draft=True)
        second = federate.build_report(federation, draft=True)
        self.assertEqual(first, second)
        self.assertFalse(first["cross_campaign_blind_complete"])
        self.assertEqual(
            first["measurement_contract"]["campaign_contract_version"], 2)
        self.assertEqual(
            first["measurement_contract"]["performance_protocol"]["query_count"],
            2)
        self.assertNotIn(
            "paths", first["measurement_contract"]["performance_protocol"])
        self.assertEqual(
            first["profiles"]["candidate"]["decision"]["draft_pending"],
            ["cross_campaign_blind_review"],
        )
        self.assertIsNone(first["winner"])

    def test_report_requires_blind_and_complete_platform_evidence(self) -> None:
        federation = federate.validate_campaigns(self.specs)
        with self.assertRaisesRegex(federate.FederationError, "evidence is incomplete"):
            federate.build_report(federation)
        private, scores, quality, federation = self._blind_inputs()
        with self.assertRaisesRegex(federate.FederationError, "physical_mac"):
            federate.build_report(
                federation, blind_private=private, blind_scores=scores,
                quality_path=quality,
            )

    def test_report_cli_exits_two_unless_draft_is_explicit(self) -> None:
        arguments = ["report"]
        for run_dir, manifest in self.specs:
            arguments.extend(["--campaign", str(run_dir), str(manifest)])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(federate.main(arguments), 2)
            self.assertEqual(federate.main([*arguments, "--draft"]), 0)

    def test_complete_winner_uses_canonical_adoption_formula(self) -> None:
        private, scores, quality, federation = self._blind_inputs()
        for source in federation["sources"]:
            for profile_id in source["profiles"]:
                source["platform_support"][profile_id] = {
                    "complete": True,
                    "measured": list(selection.REQUIRED_CPU_PLATFORMS),
                    "missing": [],
                }
        blind = federate.import_blind_scores(federation, private, scores, quality)
        baseline = dict(federation["profiles"]["baseline"][1]["metrics"])
        candidate = dict(federation["profiles"]["candidate"][1]["metrics"])
        baseline["physical_cpu_support"] = True
        candidate["physical_cpu_support"] = True
        expected = selection.adoption_decision(
            baseline, candidate,
            baseline_blind=blind["profiles"]["baseline"],
            candidate_blind=blind["profiles"]["candidate"],
        )
        report = federate.build_report(
            federation, blind_private=private, blind_scores=scores,
            quality_path=quality,
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["winner"], "candidate")
        self.assertEqual(report["profiles"]["candidate"]["decision"], expected)

    def test_report_does_not_accept_self_hashed_blind_results(self) -> None:
        federation = federate.validate_campaigns(self.specs)
        forged = self.root / "forged-result.json"
        _write(forged, {
            "schema": 1,
            "profiles": {
                "baseline": {"points": 40, "possible": 40},
                "candidate": {"points": 40, "possible": 40},
            },
        })
        with self.assertRaises(TypeError):
            federate.build_report(federation, blind_result=forged)

    def test_rejects_input_digest_drift(self) -> None:
        third = _make_source(
            self.root, "third", "candidate-two", baseline=False,
            shared_snapshot=self.specs[0][0] / "snapshot", code_digest="d" * 64,
        )
        with self.assertRaisesRegex(federate.FederationError, "code_digest differs"):
            federate.validate_campaigns([self.specs[0], third])

    def test_rejects_duplicate_profile_ids(self) -> None:
        with self.assertRaisesRegex(federate.FederationError, "duplicate"):
            federate.validate_campaigns([self.specs[0], self.specs[0]])

    def test_rejects_stale_quality_artifact(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "quality.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["campaign_inputs"]["code_digest"] = "e" * 64
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "stale or malformed"):
            federate.validate_campaigns(self.specs)

    def test_rejects_cross_campaign_performance_environment_drift(self) -> None:
        run_dir = self.specs[1][0]
        profile_id = "candidate"
        root = run_dir / "profiles" / profile_id
        state_path = run_dir / "state.json"
        prepare_path = root / "prepare.json"
        performance_path = root / "performance.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        prepare = json.loads(prepare_path.read_text(encoding="utf-8"))
        performance = json.loads(performance_path.read_text(encoding="utf-8"))
        environment = json.loads(json.dumps(
            state["benchmark_environments"][profile_id]))
        environment["execution"]["power_policy"] = "battery-saver"
        digest = federate._digest(environment)
        result = prepare["result"]
        result["benchmark_environment"] = environment
        result["benchmark_environment_digest"] = digest
        result["embed"]["benchmark_environment_digest"] = digest
        state["prepare"][profile_id] = result
        state["benchmark_environments"][profile_id] = environment
        performance["benchmark_environment"] = environment
        performance["benchmark_environment_digest"] = digest
        scale_path = selection._q8_scale_cache_path(run_dir, result["dim"])
        scale = json.loads(scale_path.read_text(encoding="utf-8"))
        scale = selection._q8_scale_cache_record(
            scale["report"], dim=result["dim"],
            code_digest=state["inputs"]["code_digest"],
            benchmark_env=environment)
        _write(scale_path, scale)
        performance["q8_scale_evidence"] = {
            "cache_path": str(scale_path),
            "cache_sha256": selection._file_digest(scale_path),
            "record": scale,
            "record_digest": federate._digest(scale),
        }
        _write(prepare_path, prepare)
        _write(state_path, state)
        _write(performance_path, performance)
        with self.assertRaisesRegex(
                federate.FederationError, "benchmark environments differ"):
            federate.validate_campaigns(self.specs)

    def test_rejects_cross_campaign_quality_protocol_drift(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "quality.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["protocol"]["hits"] = 4
        value["protocol"]["cli"]["output_cap"] = 4
        value["protocol_digest"] = federate._digest(value["protocol"])
        _write(path, value)
        with self.assertRaisesRegex(
                federate.FederationError, "quality protocols differ"):
            federate.validate_campaigns(self.specs)

    def test_campaign_contract_rejects_float_versions_at_every_boundary(self) -> None:
        run_dir = self.specs[1][0]
        for relative in (
                Path("state.json"), Path("profiles/candidate/prepare.json"),
                Path("profiles/candidate/calibration.json"),
                Path("profiles/candidate/quality.json"),
                Path("profiles/candidate/performance.json")):
            with self.subTest(relative=relative):
                path = run_dir / relative
                original = path.read_text(encoding="utf-8")
                value = json.loads(original)
                value["campaign_contract_version"] = 2.0
                _write(path, value)
                try:
                    with self.assertRaises(federate.FederationError):
                        federate.validate_campaigns(self.specs)
                finally:
                    path.write_text(original, encoding="utf-8")

    def test_rejects_self_consistent_projection_forged_from_no_raw_evidence(self) -> None:
        run_dir = self.specs[1][0]
        path = run_dir / "profiles" / "candidate" / "performance.json"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        value = json.loads(path.read_text(encoding="utf-8"))
        current = value["current_layout_top_up_projection_10m"]
        current["incremental_inference"]["projected_s"] += 100.0
        current["modeled_components_sum_s"] += 100.0
        prepared = state["prepare"]["candidate"]
        value["full_rebuild_projection_10m"] = (
            selection._projection_module().project_full_rebuild(
                dim=prepared["dim"], target_rows=selection.TEN_MILLION,
                measured_full={
                    "published_rows": prepared["embed_stats"]["published_rows"],
                    "inferred_rows": prepared["embed_stats"]["inferred"],
                    "phases_s": prepared["embed_stats"]["phases_s"],
                },
                accelerator_projection=current))
        _write(path, value)
        with self.assertRaisesRegex(
                federate.FederationError, "current-layout projection is stale"):
            federate.validate_campaigns(self.specs)

    def test_rejects_rehashed_embedded_q8_evidence_without_cache_edit(self) -> None:
        run_dir = self.specs[1][0]
        path = run_dir / "profiles" / "candidate" / "performance.json"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        value = json.loads(path.read_text(encoding="utf-8"))
        evidence = value["q8_scale_evidence"]
        embedded = evidence["record"]
        embedded["report"]["campaigns"][0]["build"][
            "q8_full_rebuild_s"] += 5.0
        embedded["report_digest"] = federate._digest(embedded["report"])
        evidence["record_digest"] = federate._digest(embedded)
        prepared = state["prepare"]["candidate"]
        current = selection._projection_module().project_current_layout(
            dim=prepared["dim"], target_rows=selection.TEN_MILLION,
            measured_incremental=value["top_up"]["projection_basis"],
            q8_scale_report=embedded["report"])
        value["current_layout_top_up_projection_10m"] = current
        value["full_rebuild_projection_10m"] = (
            selection._projection_module().project_full_rebuild(
                dim=prepared["dim"], target_rows=selection.TEN_MILLION,
                measured_full={
                    "published_rows": prepared["embed_stats"]["published_rows"],
                    "inferred_rows": prepared["embed_stats"]["inferred"],
                    "phases_s": prepared["embed_stats"]["phases_s"],
                }, accelerator_projection=current))
        _write(path, value)
        with self.assertRaisesRegex(
                federate.FederationError, "differs from canonical cache"):
            federate.validate_campaigns(self.specs)

    def test_rejects_boolean_projection_measurements(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "performance.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["top_up"]["projection_basis"]["phases_s"]["inference"] = True
        _write(path, value)
        with self.assertRaisesRegex(
                federate.FederationError, "projection inputs are malformed"):
            federate.validate_campaigns(self.specs)

    def test_preliminary_state_without_environment_is_not_federatable(self) -> None:
        path = self.specs[1][0] / "state.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop("benchmark_environments")
        value.pop("campaign_contract_version")
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "contract is stale"):
            federate.validate_campaigns(self.specs)

    def test_rejects_skipped_prepare_with_synthetic_performance(self) -> None:
        run_dir = self.specs[1][0]
        root = run_dir / "profiles" / "candidate"
        prepare_path = root / "prepare.json"
        prepare = json.loads(prepare_path.read_text(encoding="utf-8"))
        result = prepare["result"]
        result.update({
            "embed": None,
            "embed_stats": None,
            "embedding_bundle": None,
            "embed_skipped": True,
        })
        _write(prepare_path, prepare)
        state_path = run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["prepare"]["candidate"] = result
        _write(state_path, state)
        shutil.rmtree(root / "prepared-embedding-bundle")
        with self.assertRaisesRegex(federate.FederationError, "completed prepared"):
            federate.validate_campaigns(self.specs)

    def test_rejects_boolean_consumed_metric(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "performance.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["semantic_resources"]["semantic_resident_rss_mib"] = True
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "must be finite"):
            federate.validate_campaigns(self.specs)

    def test_rejects_stale_warm_median_and_unbound_throughput(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "performance.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["query_exit_ms"]["semantic"]["warm_median"] = 13.0
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "warm summary is stale"):
            federate.validate_campaigns(self.specs)
        value["query_exit_ms"]["semantic"]["warm_median"] = 12.0
        value["full_rebuild"]["dedup_effective_rows_per_s"] = 9_999.0
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "bound to prepare"):
            federate.validate_campaigns(self.specs)

    def test_rejects_missing_quality_rows(self) -> None:
        path = self.specs[1][0] / "profiles" / "candidate" / "quality.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["outcomes"].pop()
        _write(path, value)
        with self.assertRaisesRegex(federate.FederationError, "outcome rows are incomplete"):
            federate.validate_campaigns(self.specs)

    def test_rejects_zero_and_multiple_included_baselines(self) -> None:
        zero_first = _make_source(
            self.root, "zero-first", "candidate-a", baseline=False,
            shared_snapshot=self.specs[0][0] / "snapshot",
        )
        zero_second = _make_source(
            self.root, "zero-second", "candidate-b", baseline=False,
            shared_snapshot=self.specs[0][0] / "snapshot",
        )
        with self.assertRaisesRegex(federate.FederationError, "exactly one"):
            federate.validate_campaigns([zero_first, zero_second])
        extra = _make_source(
            self.root, "extra", "baseline-two", baseline=True,
            shared_snapshot=self.specs[0][0] / "snapshot",
        )
        with self.assertRaisesRegex(federate.FederationError, "exactly one"):
            federate.validate_campaigns([self.specs[0], extra])

    def test_cross_campaign_blind_rejects_boolean_ratings(self) -> None:
        quality = self._bind_quality_fixture()
        federation = federate.validate_campaigns(self.specs)
        _packet, private = federate.build_blind_packet(federation, quality)
        private_path = self.root / "private.json"
        _write(private_path, private)
        judgments = []
        for task_id, labels in private["mapping"].items():
            judgments.append({
                "task": task_id,
                "ratings": {label: True for label in labels},
            })
        scores = {
            "schema": 1,
            "federation_digest": federation["public"]["federation_digest"],
            "blind_digest": private["blind_digest"],
            "judgments": judgments,
        }
        scores_path = self.root / "scores.json"
        _write(scores_path, scores)
        with self.assertRaisesRegex(federate.FederationError, "must be integers"):
            federate.import_blind_scores(federation, private_path, scores_path, quality)

    def test_cross_campaign_blind_round_trip_is_bound_and_deterministic(self) -> None:
        quality = self._bind_quality_fixture()
        federation = federate.validate_campaigns(self.specs)
        packet, private = federate.build_blind_packet(federation, quality)
        self.assertEqual((packet, private), federate.build_blind_packet(federation, quality))
        private_path = self.root / "private.json"
        _write(private_path, private)
        judgments = [{
            "task": task_id,
            "ratings": {label: 1 for label in labels},
        } for task_id, labels in private["mapping"].items()]
        scores = {
            "schema": 1,
            "federation_digest": federation["public"]["federation_digest"],
            "blind_digest": private["blind_digest"],
            "judgments": judgments,
        }
        scores_path = self.root / "scores.json"
        _write(scores_path, scores)
        first = federate.import_blind_scores(federation, private_path, scores_path, quality)
        second = federate.import_blind_scores(federation, private_path, scores_path, quality)
        self.assertEqual(first, second)
        result_path = self.root / "blind-result.json"
        _write(result_path, first)
        report = federate.build_report(
            federation, blind_private=private_path, blind_scores=scores_path,
            quality_path=quality, draft=True,
        )
        self.assertTrue(report["cross_campaign_blind_complete"])
        self.assertIn("blind_not_worse_than_baseline",
                      report["profiles"]["candidate"]["decision"]["checks"])

    def test_rejects_symlinked_input_artifact(self) -> None:
        linked = self.root / "linked-manifest.json"
        linked.symlink_to(self.specs[1][1])
        with self.assertRaisesRegex(federate.FederationError, "regular file"):
            federate.validate_campaigns([self.specs[0], (self.specs[1][0], linked)])

    def test_moving_campaign_directories_invalidates_bound_absolute_paths(self) -> None:
        federate.validate_campaigns(self.specs)
        moved = self.root / "moved"
        moved.mkdir()
        moved_specs = []
        for index, (run_dir, manifest) in enumerate(self.specs):
            new_run = moved / f"run-{index}"
            new_manifest = moved / f"manifest-{index}.json"
            shutil.copytree(run_dir, new_run, copy_function=shutil.copy2)
            shutil.copy2(manifest, new_manifest)
            moved_specs.append((new_run, new_manifest))
        with self.assertRaisesRegex(
                federate.FederationError, "run directory drifted"):
            federate.validate_campaigns(moved_specs)

    def test_rejects_output_collisions_before_writes(self) -> None:
        campaign_args = []
        for run_dir, manifest in self.specs:
            campaign_args.extend(["--campaign", str(run_dir), str(manifest)])
        args = federate._parser().parse_args([
            "validate", *campaign_args, "--output", str(self.specs[0][1]),
        ])
        with self.assertRaisesRegex(federate.FederationError, "collides with an input"):
            federate._guard_output_paths(args)
        same = self.root / "same.json"
        quality = self.root / "tasks.json"
        _quality_fixture(quality)
        args = federate._parser().parse_args([
            "blind-export", *campaign_args, "--quality-tasks", str(quality),
            "--output", str(same), "--private", str(same),
        ])
        with self.assertRaisesRegex(federate.FederationError, "must be distinct"):
            federate._guard_output_paths(args)

    def test_manifest_and_platform_reads_are_rehashed(self) -> None:
        original_load = selection.load_manifest

        def move_manifest(path: Path):
            value = original_load(path)
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            return value

        with mock.patch.object(selection, "load_manifest", side_effect=move_manifest):
            with self.assertRaisesRegex(federate.FederationError, "manifest moved"):
                federate.validate_campaigns(self.specs)

        platform = self.specs[0][0] / "platform-evidence.json"
        _write(platform, {})

        def move_platform(path: Path, _manifest: dict, profile_ids: list[str]):
            _write(path, {"moved": True})
            support = {
                profile_id: {
                    "complete": False,
                    "measured": [],
                    "missing": list(selection.REQUIRED_CPU_PLATFORMS),
                } for profile_id in profile_ids
            }
            return {}, support

        with mock.patch.object(
                selection, "_validated_platform_support", side_effect=move_platform):
            with self.assertRaisesRegex(federate.FederationError, "platform evidence moved"):
                federate.validate_campaigns(self.specs)

    def test_duplicate_json_keys_fail_closed(self) -> None:
        state = self.specs[1][0] / "state.json"
        text = state.read_text(encoding="utf-8").rstrip()
        state.write_text(text[:-1] + ',"schema":1}\n', encoding="utf-8")
        with self.assertRaisesRegex(federate.FederationError, "duplicate JSON key"):
            federate.validate_campaigns(self.specs)


if __name__ == "__main__":
    unittest.main()
