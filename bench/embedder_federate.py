#!/usr/bin/env python3
"""Federate independently staged embedder campaigns without rewriting provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import stat
import sys
from pathlib import Path
from typing import Any

try:
    from . import embedder_selection as selection
except ImportError:
    import embedder_selection as selection


SCHEMA = 1
SHA256_RE = selection.SHA256_RE
SCRIPT = Path(__file__).resolve()


class FederationError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    try:
        metadata = expanded.lstat()
    except OSError as exc:
        raise FederationError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise FederationError(f"{label} must be a real directory: {path}")
    return expanded.resolve()


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FederationError(f"could not inspect optional artifact {path}: {exc}") from exc


def _input_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _regular_digest(path: Path, label: str) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise FederationError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise FederationError(f"{label} must be a regular file: {path}")
    digest = _file_digest(path)
    after = path.lstat()
    identity = lambda row: (
        row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_dev, row.st_ino
    )
    if identity(before) != identity(after):
        raise FederationError(f"{label} moved while hashing: {path}")
    return digest


def _read_regular_json(path: Path, label: str) -> tuple[Any, str]:
    digest = _regular_digest(path, label)
    try:
        value = selection._read_json(path)
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if _regular_digest(path, label) != digest:
        raise FederationError(f"{label} moved while parsing: {path}")
    return value, digest


def _write_json(path: Path, value: Any) -> None:
    path = _input_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def _keys(value: Any, required: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise FederationError(f"{where} has an invalid shape")
    return value


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise FederationError(f"{where} must be a lowercase SHA-256")
    return value


def _integer(value: Any, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FederationError(f"{where} must be an integer >= {minimum}")
    return value


def _number(value: Any, where: str, minimum: float = 0.0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < minimum):
        raise FederationError(f"{where} must be finite and >= {minimum}")
    return float(value)


def _validate_runtime_types(runtime: dict, profile_id: str) -> None:
    if type(runtime.get("schema")) is not int or runtime["schema"] != SCHEMA:
        raise FederationError(f"{profile_id} runtime schema is not {SCHEMA}")
    for key in ("dim", "native_dim", "max_seq", "model_bytes"):
        _integer(runtime.get(key), f"{profile_id} runtime {key}", 1)
    for name, artifact in runtime.get("files", {}).items():
        _integer(artifact.get("size"), f"{profile_id} artifact {name} size", 1)


def _validate_state(value: Any, run_dir: Path) -> dict:
    if (isinstance(value, dict)
            and not selection._current_campaign_contract(
                value.get("campaign_contract_version"))):
        raise FederationError(f"{run_dir} campaign contract is stale")
    state = _keys(
        value,
        {"schema", "created_at", "inputs", "snapshot", "profiles", "prepare",
         "benchmark_environments", "campaign_contract_version"},
        f"{run_dir} state",
    )
    if type(state["schema"]) is not int or state["schema"] != SCHEMA:
        raise FederationError(f"{run_dir} state schema is not {SCHEMA}")
    if not selection._current_campaign_contract(
            state["campaign_contract_version"]):
        raise FederationError(f"{run_dir} campaign contract is stale")
    _integer(state["created_at"], f"{run_dir} created_at")
    inputs = _keys(
        state["inputs"],
        {"manifest_digest", "calibration_digest", "quality_digest", "code_digest"},
        f"{run_dir} campaign inputs",
    )
    for key, value in inputs.items():
        _sha(value, f"{run_dir} {key}")
    profiles = state["profiles"]
    if (not isinstance(profiles, list) or not profiles
            or any(not isinstance(item, str) or not selection.ID_RE.fullmatch(item)
                   for item in profiles)
            or profiles != sorted(profiles) or len(profiles) != len(set(profiles))):
        raise FederationError(f"{run_dir} state profile list is invalid")
    if not isinstance(state["prepare"], dict) or set(state["prepare"]) != set(profiles):
        raise FederationError(f"{run_dir} prepare checkpoint does not match profiles")
    environments = state["benchmark_environments"]
    if not isinstance(environments, dict) or set(environments) != set(profiles):
        raise FederationError(f"{run_dir} benchmark environments do not match profiles")
    for profile_id in profiles:
        try:
            selection.validate_benchmark_environment(environments[profile_id])
        except selection.HarnessError as exc:
            raise FederationError(str(exc)) from exc
        prepared = state["prepare"][profile_id]
        if (not isinstance(prepared, dict)
                or environments[profile_id]
                != prepared.get("benchmark_environment")):
            raise FederationError(
                f"{profile_id} benchmark environment is not state-bound")
    snapshot = _keys(
        state["snapshot"],
        {"files", "message_rows", "reply_rows", "rows", "event_mode", "digest"},
        f"{run_dir} snapshot",
    )
    if not isinstance(snapshot["files"], list):
        raise FederationError(f"{run_dir} snapshot inventory is not a list")
    names = set()
    for index, row in enumerate(snapshot["files"]):
        row = _keys(
            row, {"path", "size", "mtime_ns", "sha256"},
            f"{run_dir} snapshot file {index}",
        )
        if (not isinstance(row["path"], str) or not row["path"]
                or row["path"] in names):
            raise FederationError(f"{run_dir} snapshot file identity is invalid")
        names.add(row["path"])
        _integer(row["size"], f"{run_dir} snapshot file size")
        _integer(row["mtime_ns"], f"{run_dir} snapshot file mtime")
        _sha(row["sha256"], f"{run_dir} snapshot file digest")
    for key in ("message_rows", "reply_rows", "rows"):
        _integer(snapshot[key], f"{run_dir} snapshot {key}")
    if snapshot["rows"] != snapshot["message_rows"] + snapshot["reply_rows"]:
        raise FederationError(f"{run_dir} snapshot row totals are inconsistent")
    if snapshot["event_mode"] not in {"current", "legacy"}:
        raise FederationError(f"{run_dir} snapshot event mode is invalid")
    _sha(snapshot["digest"], f"{run_dir} snapshot digest")
    _require_directory(run_dir / "snapshot", f"{run_dir} snapshot root")
    try:
        observed = selection._snapshot_inventory(run_dir / "snapshot")
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if observed != state["snapshot"]:
        raise FederationError(f"{run_dir} frozen snapshot moved after prepare")
    return state


def _validate_calibration(report: dict, profile_id: str) -> None:
    required = {
        "schema", "profile", "snapshot_digest", "task_digest", "campaign_inputs",
        "base_profile_digest", "calibration", "calibration_usable",
        "effective_search_bands", "observations", "campaign_contract_version",
    }
    _keys(report, required, f"{profile_id} calibration")
    if type(report["schema"]) is not int or report["schema"] != SCHEMA:
        raise FederationError(f"{profile_id} calibration schema is not {SCHEMA}")
    if not selection._current_campaign_contract(
            report["campaign_contract_version"]):
        raise FederationError(f"{profile_id} calibration campaign contract is stale")
    observations = report["observations"]
    if not isinstance(observations, list) or not observations:
        raise FederationError(f"{profile_id} calibration has no observations")
    seen = set()
    for index, row in enumerate(observations):
        where = f"{profile_id} calibration observation {index}"
        required_row = {
            "id", "kind", "query", "scores", "rows", "excluded_query_echoes",
            "exit_ms",
        }
        _keys(row, required_row, where)
        if (not isinstance(row["id"], str) or not row["id"]
                or row["id"] in seen or row["kind"] not in {"real", "gibberish"}
                or not isinstance(row["query"], str) or not row["query"].strip()
                or not isinstance(row["scores"], list)):
            raise FederationError(f"{where} has invalid identity or scores")
        seen.add(row["id"])
        _integer(row["rows"], f"{where} rows")
        _integer(row["excluded_query_echoes"], f"{where} echoes")
        if (row["excluded_query_echoes"] > row["rows"]
                or len(row["scores"]) > row["rows"]):
            raise FederationError(f"{where} row counts are inconsistent")
        _number(row["exit_ms"], f"{where} exit_ms")
        for score in row["scores"]:
            _number(score, f"{where} score", -1.0)
            if float(score) > 1.0:
                raise FederationError(f"{where} score exceeds 1")
    if not isinstance(report["calibration_usable"], bool):
        raise FederationError(f"{profile_id} calibration usability is not boolean")
    try:
        calculated = selection.calibrate_scores(observations)
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if calculated != report["calibration"]:
        raise FederationError(f"{profile_id} calibration result does not match its rows")


def _validate_quality(report: dict, profile_id: str) -> int:
    required = {
        "schema", "profile", "snapshot_digest", "task_digest", "campaign_inputs",
        "runtime_profile_digest", "protocol", "protocol_digest", "scores", "outcomes",
        "campaign_contract_version",
    }
    _keys(report, required, f"{profile_id} quality")
    if type(report["schema"]) is not int or report["schema"] != SCHEMA:
        raise FederationError(f"{profile_id} quality schema is not {SCHEMA}")
    try:
        selection.validate_quality_artifact_protocol(report, report["task_digest"])
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    scores = _keys(report["scores"], {"semantic", "hybrid"}, f"{profile_id} scores")
    outcomes = report["outcomes"]
    if not isinstance(outcomes, list) or not outcomes:
        raise FederationError(f"{profile_id} quality has no outcome rows")
    tasks_by_lane: dict[str, set[str]] = {}
    for lane in ("semantic", "hybrid"):
        row = _keys(
            scores[lane], {"correct", "total", "exit_ms"},
            f"{profile_id} {lane} score",
        )
        total = _integer(row["total"], f"{profile_id} {lane} total", 1)
        correct = _integer(row["correct"], f"{profile_id} {lane} correct")
        if correct > total or not isinstance(row["exit_ms"], list) \
                or len(row["exit_ms"]) != total:
            raise FederationError(f"{profile_id} {lane} score totals are inconsistent")
        for value in row["exit_ms"]:
            _number(value, f"{profile_id} {lane} exit_ms")
        lane_rows = [item for item in outcomes if isinstance(item, dict)
                     and item.get("lane") == lane]
        if len(lane_rows) != total or sum(item.get("correct") is True
                                           for item in lane_rows) != correct:
            raise FederationError(f"{profile_id} {lane} outcome rows are incomplete")
        if row["exit_ms"] != [item.get("exit_ms") for item in lane_rows]:
            raise FederationError(f"{profile_id} {lane} exit rows are stale")
        identities = [item.get("task") for item in lane_rows]
        if (any(not isinstance(item, str) or not item for item in identities)
                or len(identities) != len(set(identities))):
            raise FederationError(f"{profile_id} {lane} task rows are invalid")
        tasks_by_lane[lane] = set(identities)
    if tasks_by_lane["semantic"] != tasks_by_lane["hybrid"]:
        raise FederationError(f"{profile_id} quality lanes cover different tasks")
    for index, row in enumerate(outcomes):
        where = f"{profile_id} quality outcome {index}"
        if not isinstance(row, dict):
            raise FederationError(f"{where} is not an object")
        required_row = {
            "task", "lane", "correct", "expected_rank", "exit_ms",
            "launcher_tree_cpu_ms", "launcher_tree_peak_rss_mib", "engine", "hits",
        }
        _keys(row, required_row, where)
        if row["lane"] not in {"semantic", "hybrid"} \
                or not isinstance(row["correct"], bool) \
                or not isinstance(row["hits"], list):
            raise FederationError(f"{where} has invalid lane, result, or hits")
        if row["expected_rank"] is not None:
            _integer(row["expected_rank"], f"{where} expected_rank", 1)
        if (row["correct"] is True) != (row["expected_rank"] is not None):
            raise FederationError(f"{where} correctness and rank disagree")
        _number(row["exit_ms"], f"{where} exit_ms")
        _number(row["launcher_tree_cpu_ms"], f"{where} CPU")
        _number(row["launcher_tree_peak_rss_mib"], f"{where} RSS")
        if any(not isinstance(hit, dict)
               or not isinstance(hit.get("evidence"), str)
               or not isinstance(hit.get("project"), (str, type(None)))
               for hit in row["hits"]):
            raise FederationError(f"{where} contains malformed hits")
    return len(tasks_by_lane["semantic"])


def _validate_prepared(prepared: dict, profile_id: str) -> None:
    if (prepared.get("embed_skipped") is not False
            or not isinstance(prepared.get("embedding_bundle"), dict)
            or not isinstance(prepared.get("embed_stats"), dict)
            or not isinstance(prepared.get("embed"), dict)):
        raise FederationError(
            f"{profile_id} requires a completed prepared full embedding bundle")
    embed = prepared["embed"]
    if type(embed.get("returncode")) is not int or embed["returncode"] != 0:
        raise FederationError(f"{profile_id} prepared embedding did not succeed")
    for key in ("wall_ms", "cpu_ms", "peak_rss_mib"):
        _number(embed.get(key), f"{profile_id} prepared embed {key}")
    try:
        environment = selection.validate_benchmark_environment(
            prepared.get("benchmark_environment"))
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if (prepared.get("benchmark_environment_digest") != _digest(environment)
            or embed.get("benchmark_environment_digest")
            != prepared.get("benchmark_environment_digest")):
        raise FederationError(f"{profile_id} prepared environment binding is stale")
    stats = _keys(
        prepared["embed_stats"],
        {"rows", "published_rows", "inferred", "reused", "elapsed_s", "phases_s",
         "inference_s", "raw_rows_per_s", "dedup_effective_rows_per_s"},
        f"{profile_id} prepared embed stats",
    )
    rows = _integer(stats["rows"], f"{profile_id} prepared rows", 1)
    published = _integer(
        stats["published_rows"], f"{profile_id} published rows", 1)
    inferred = _integer(stats["inferred"], f"{profile_id} inferred rows", 1)
    reused = _integer(stats["reused"], f"{profile_id} reused rows")
    if rows != published or inferred + reused != rows:
        raise FederationError(f"{profile_id} prepared embedding row totals disagree")
    _number(stats["elapsed_s"], f"{profile_id} prepared elapsed", 0.000001)
    _number(
        stats["inference_s"], f"{profile_id} prepared inference", 0.000001)
    phases = _keys(
        stats["phases_s"], {"plan", "load", "inference", "f32_publish"},
        f"{profile_id} prepared phases",
    )
    for name, value in phases.items():
        _number(value, f"{profile_id} prepared {name} phase")
    inference = float(phases["inference"])
    if inference <= 0:
        raise FederationError(f"{profile_id} prepared inference phase must be positive")
    expected_raw = round(inferred / inference, 3)
    expected_effective = round(rows / inference, 3)
    if (stats["inference_s"] != round(float(phases["inference"]), 3)
            or stats["raw_rows_per_s"] != expected_raw
            or stats["dedup_effective_rows_per_s"] != expected_effective):
        raise FederationError(f"{profile_id} prepared throughput is not reproducible")
    _number(stats["raw_rows_per_s"], f"{profile_id} prepared raw throughput")
    _number(
        stats["dedup_effective_rows_per_s"],
        f"{profile_id} prepared effective throughput",
    )


def _warm_exit_metrics(row: Any, profile_id: str, lane: str) -> None:
    if not isinstance(row, dict) or not isinstance(row.get("warm_samples"), list) \
            or not row["warm_samples"] or not isinstance(row.get("cold"), dict):
        raise FederationError(f"{profile_id} {lane} exit samples are incomplete")
    samples = [row["cold"], *row["warm_samples"]]
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise FederationError(f"{profile_id} {lane} sample {index} is malformed")
        _number(sample.get("exit_ms"), f"{profile_id} {lane} sample {index} exit")
    warm = [sample["exit_ms"] for sample in row["warm_samples"]]
    expected = {
        "warm_median": selection._percentile(warm, 0.5),
        "warm_p90": selection._percentile(warm, 0.9),
        "warm_max": round(max(warm), 3),
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise FederationError(f"{profile_id} {lane} warm summary is stale")
    for key in expected:
        _number(row[key], f"{profile_id} {lane} {key}")


def _validate_performance(report: dict, profile_id: str, tasks: int,
                          prepared: dict, runtime: dict, run_dir: Path,
                          profile_root: Path, calibration: dict,
                          code_digest: str) -> None:
    if type(report.get("schema")) is not int or report["schema"] != SCHEMA:
        raise FederationError(f"{profile_id} performance schema is not {SCHEMA}")
    try:
        protocol = selection.validate_performance_artifact_protocol(
            report, prepared, expected_code_digest=code_digest)
        calibration_queries = [
            {"id": row["id"], "query": row["query"]}
            for row in calibration["observations"] if row["kind"] == "real"
        ][:protocol["query_count"]]
        selection.validate_performance_protocol(
            protocol, query_tasks=calibration_queries,
            run_dir=run_dir, profile_root=profile_root)
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if protocol["query_count"] < 2:
        raise FederationError(f"{profile_id} performance used fewer than two queries")
    if _integer(report.get("quality_fixture_tasks"),
                f"{profile_id} performance task count", 1) != tasks:
        raise FederationError(f"{profile_id} performance task count is stale")
    _integer(
        report.get("index_projection_10m", {}).get("q8_bytes"),
        f"{profile_id} q8 projection", 1,
    )
    if report.get("index_projection_10m") != selection.index_projection(runtime["dim"]):
        raise FederationError(f"{profile_id} q8 projection is stale")
    _number(
        report.get("full_rebuild", {}).get("dedup_effective_rows_per_s"),
        f"{profile_id} rebuild throughput",
    )
    full_rebuild = report.get("full_rebuild")
    if not isinstance(full_rebuild, dict):
        raise FederationError(f"{profile_id} full rebuild proof is absent")
    expected_full = {
        **prepared["embed_stats"],
        "measured_stage": "prepare",
        "wall_ms": prepared["embed"]["wall_ms"],
        "cpu_ms": prepared["embed"]["cpu_ms"],
        "peak_rss_mib": prepared["embed"]["peak_rss_mib"],
    }
    if any(full_rebuild.get(key) != value for key, value in expected_full.items()):
        raise FederationError(f"{profile_id} full rebuild is not bound to prepare")
    if not isinstance(report.get("accelerator_used"), bool):
        raise FederationError(f"{profile_id} accelerator flag is not boolean")
    _sha(report.get("query_task_digest"), f"{profile_id} query task digest")
    requested = runtime.get("provider", "CPUExecutionProvider")
    actual = prepared["session"]["actual_providers"]
    if (report.get("requested_provider") != requested
            or report.get("actual_session_providers") != actual
            or report["accelerator_used"] != any(
                provider != "CPUExecutionProvider" for provider in actual)
            or report.get("model_graph_bytes") != prepared["model_graph_bytes"]
            or report.get("pinned_cache_bytes") != prepared["pinned_cache_bytes"]
            or report.get("campaign_unreferenced_model_bytes") != 0):
        raise FederationError(f"{profile_id} performance runtime proof is stale")
    exits = report.get("query_exit_ms")
    if not isinstance(exits, dict):
        raise FederationError(f"{profile_id} query exit metrics are absent")
    for lane in ("semantic", "hybrid"):
        _warm_exit_metrics(exits.get(lane), profile_id, lane)
    resources = report.get("semantic_resources")
    if not isinstance(resources, dict):
        raise FederationError(f"{profile_id} semantic resource proof is absent")
    for key in ("semantic_resident_rss_mib", "semantic_peak_rss_mib"):
        _number(resources.get(key), f"{profile_id} {key}")
    _integer(report.get("model_graph_bytes"), f"{profile_id} model bytes", 1)
    for section in (
            "full_rebuild_projection_10m", "current_layout_top_up_projection_10m"):
        value = report.get(section)
        if not isinstance(value, dict):
            raise FederationError(f"{profile_id} {section} is absent")
        _number(
            value.get("modeled_components_sum_s"),
            f"{profile_id} {section} modeled sum",
        )


def _load_source(run_path: Path, manifest_path: Path, source_id: str) -> dict:
    run_dir = _require_directory(run_path, f"{source_id} campaign")
    manifest_input = _input_path(manifest_path)
    manifest_hash = _regular_digest(manifest_input, f"{source_id} manifest")
    manifest_path = manifest_input.resolve()
    try:
        manifest = selection.load_manifest(manifest_path)
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if _regular_digest(manifest_input, f"{source_id} manifest") != manifest_hash:
        raise FederationError(f"{source_id} manifest moved while loading")
    if type(manifest.get("schema")) is not int or manifest["schema"] != SCHEMA:
        raise FederationError(f"{source_id} manifest schema is not {SCHEMA}")
    state_value, state_hash = _read_regular_json(run_dir / "state.json", "campaign state")
    state = _validate_state(state_value, run_dir)
    if state["inputs"]["manifest_digest"] != _digest(manifest):
        raise FederationError(f"{source_id} exact manifest does not match prepared state")
    by_id = {profile["id"]: profile for profile in manifest["profiles"]}
    missing = set(state["profiles"]) - set(by_id)
    if missing:
        raise FederationError(
            f"{source_id} manifest omits prepared profiles: {', '.join(sorted(missing))}")
    profiles = {}
    source_hashes = {"manifest": manifest_hash, "state.json": state_hash}
    for profile_id in state["profiles"]:
        profile = by_id[profile_id]
        if profile["status"] != "runnable":
            raise FederationError(f"{profile_id} is prepared but not runnable in its manifest")
        runtime = selection._runtime_profile(profile)
        _validate_runtime_types(runtime, profile_id)
        root = run_dir / "profiles" / profile_id
        _require_directory(root, f"{profile_id} profile root")
        prepare, prepare_hash = _read_regular_json(root / "prepare.json", "prepare artifact")
        if isinstance(prepare, dict) and isinstance(prepare.get("result"), dict) \
                and prepare["result"].get("embedding_bundle") is not None:
            _require_directory(
                root / "prepared-embedding-bundle",
                f"{profile_id} prepared embedding bundle",
            )
        try:
            recovered = selection._validate_recovered_prepare(
                prepare, profile_id=profile_id, runtime=runtime,
                snapshot=state["snapshot"], inputs=state["inputs"],
                bundle_dir=root / "prepared-embedding-bundle",
            )
        except selection.HarnessError as exc:
            raise FederationError(str(exc)) from exc
        if recovered != state["prepare"][profile_id]:
            raise FederationError(f"{profile_id} prepare artifact differs from state")
        _validate_prepared(recovered, profile_id)
        calibration, calibration_hash = _read_regular_json(
            root / "calibration.json", "calibration artifact")
        _validate_calibration(calibration, profile_id)
        try:
            checked_calibration, calibrated_path, runtime_digest = (
                selection._validated_calibrated_profile(root, profile, state))
        except selection.HarnessError as exc:
            raise FederationError(str(exc)) from exc
        if checked_calibration != calibration:
            raise FederationError(f"{profile_id} calibration changed while validating")
        calibrated_hash = _regular_digest(calibrated_path, "calibrated profile")
        quality, quality_hash = _read_regular_json(root / "quality.json", "quality artifact")
        tasks = _validate_quality(quality, profile_id)
        performance, performance_hash = _read_regular_json(
            root / "performance.json", "performance artifact")
        _validate_performance(
            performance, profile_id, tasks, recovered, runtime, run_dir, root,
            calibration, state["inputs"]["code_digest"])
        q8_name = f"q8-scale/dim-{runtime['dim']}.json"
        q8_path = selection._q8_scale_cache_path(run_dir, runtime["dim"])
        q8_hash = _regular_digest(q8_path, f"{profile_id} canonical q8 scale cache")
        if q8_name in source_hashes and source_hashes[q8_name] != q8_hash:
            raise FederationError(
                f"{profile_id} canonical q8 scale cache changed between profiles")
        source_hashes[q8_name] = q8_hash
        try:
            metrics = selection._result_metrics(profile, root, state)
        except selection.HarnessError as exc:
            raise FederationError(str(exc)) from exc
        if (quality.get("runtime_profile_digest") != runtime_digest
                or performance.get("runtime_profile_digest") != runtime_digest):
            raise FederationError(f"{profile_id} staged artifact uses another runtime profile")
        artifact_hashes = {
            "prepare.json": prepare_hash,
            "calibration.json": calibration_hash,
            "profile-calibrated.json": calibrated_hash,
            "quality.json": quality_hash,
            "performance.json": performance_hash,
        }
        for name, digest in artifact_hashes.items():
            if _regular_digest(root / name, f"{profile_id} {name}") != digest:
                raise FederationError(f"{profile_id} {name} moved during validation")
        if _regular_digest(q8_path, f"{profile_id} canonical q8 scale cache") \
                != q8_hash:
            raise FederationError(
                f"{profile_id} canonical q8 scale cache moved during validation")
        profiles[profile_id] = {
            "manifest_profile": profile,
            "runtime_profile_digest": runtime_digest,
            "artifact_hashes": artifact_hashes,
            "metrics": metrics,
            "quality": quality,
            "performance": performance,
            "tasks": tasks,
            "root": root,
        }
    profile_ids = sorted(profiles)
    platform_support = {
        profile_id: {
            "complete": False,
            "measured": [],
            "missing": list(selection.REQUIRED_CPU_PLATFORMS),
        } for profile_id in profile_ids
    }
    platform_path = run_dir / "platform-evidence.json"
    if _present(platform_path):
        platform_hash = _regular_digest(platform_path, "platform evidence")
        try:
            _bundle, platform_support = selection._validated_platform_support(
                platform_path, manifest, profile_ids)
        except selection.HarnessError as exc:
            raise FederationError(str(exc)) from exc
        if _regular_digest(platform_path, "platform evidence") != platform_hash:
            raise FederationError("platform evidence moved while loading")
        source_hashes["platform-evidence.json"] = platform_hash
    return {
        "source_id": source_id,
        "run_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "state": state,
        "hashes": source_hashes,
        "profiles": profiles,
        "platform_support": platform_support,
    }


def validate_campaigns(specs: list[tuple[Path, Path]]) -> dict:
    if len(specs) < 2:
        raise FederationError("federation needs at least two campaign/manifest pairs")
    sources = [
        _load_source(run_dir, manifest, f"input-{index:02d}")
        for index, (run_dir, manifest) in enumerate(specs, 1)
    ]
    reference = sources[0]["state"]
    for source in sources[1:]:
        state = source["state"]
        if state["snapshot"] != reference["snapshot"]:
            raise FederationError("campaign snapshot digest or inventory differs")
        for key in ("code_digest", "calibration_digest", "quality_digest"):
            if state["inputs"][key] != reference["inputs"][key]:
                raise FederationError(f"campaign {key} differs")
    for source in sources:
        content = {
            "campaign_inputs": source["state"]["inputs"],
            "input_hashes": source["hashes"],
            "profiles": {
                profile_id: {
                    "baseline": row["manifest_profile"]["baseline"],
                    "runtime_profile_digest": row["runtime_profile_digest"],
                    "artifact_hashes": row["artifact_hashes"],
                } for profile_id, row in sorted(source["profiles"].items())
            },
        }
        source["content_digest"] = _digest(content)
        source["source_id"] = f"source-{source['content_digest']}"
    measured_profiles = [
        row for source in sources for row in source["profiles"].values()
    ]
    benchmark_environments = {
        _digest(row["performance"]["benchmark_environment"])
        for row in measured_profiles
    }
    quality_protocols = {
        row["quality"]["protocol_digest"] for row in measured_profiles
    }
    performance_protocols = {
        _digest(selection.performance_protocol_comparison(
            row["performance"]["protocol"]))
        for row in measured_profiles
    }
    if len(benchmark_environments) != 1:
        raise FederationError(
            "campaign performance benchmark environments differ")
    if len(quality_protocols) != 1:
        raise FederationError("campaign quality protocols differ")
    if len(performance_protocols) != 1:
        raise FederationError("campaign performance protocols differ")
    sources.sort(key=lambda source: source["content_digest"])
    if len({source["source_id"] for source in sources}) != len(sources):
        raise FederationError("duplicate campaign content provenance")
    profile_records = {}
    baselines = []
    for source in sources:
        for profile_id, row in source["profiles"].items():
            if profile_id in profile_records:
                raise FederationError(f"duplicate federated profile id {profile_id}")
            profile_records[profile_id] = (source, row)
            if row["manifest_profile"]["baseline"]:
                baselines.append(profile_id)
    if len(baselines) != 1:
        raise FederationError("federation needs exactly one included baseline provenance")
    script_hash = _file_digest(SCRIPT)
    selected = sorted(profile_records)
    source_public = []
    source_binding = []
    profiles_public = {}
    for source in sources:
        bound = {
            "source_id": source["source_id"],
            "content_digest": source["content_digest"],
            "campaign_inputs": source["state"]["inputs"],
            "input_hashes": source["hashes"],
            "profile_ids": sorted(source["profiles"]),
        }
        source_binding.append(bound)
        source_public.append({
            **bound,
            "diagnostic_paths": {
                "run_dir": source["run_dir"],
                "manifest": source["manifest_path"],
            },
        })
        for profile_id, row in source["profiles"].items():
            profiles_public[profile_id] = {
                "source_id": source["source_id"],
                "baseline": row["manifest_profile"]["baseline"],
                "runtime_profile_digest": row["runtime_profile_digest"],
                "artifact_hashes": row["artifact_hashes"],
                "source_campaign_inputs": source["state"]["inputs"],
            }
    shared = {
        "snapshot": reference["snapshot"],
        "campaign_contract_version": selection.CAMPAIGN_CONTRACT_VERSION,
        "code_digest": reference["inputs"]["code_digest"],
        "calibration_digest": reference["inputs"]["calibration_digest"],
        "quality_digest": reference["inputs"]["quality_digest"],
        "benchmark_environment_digest": next(iter(benchmark_environments)),
        "quality_protocol_digest": next(iter(quality_protocols)),
        "performance_protocol_digest": next(iter(performance_protocols)),
        "benchmark_environment": measured_profiles[0]["performance"][
            "benchmark_environment"],
        "quality_protocol": measured_profiles[0]["quality"]["protocol"],
        "performance_protocol": selection.performance_protocol_comparison(
            measured_profiles[0]["performance"]["protocol"]),
    }
    binding_inputs = {
        "schema": SCHEMA,
        "script_sha256": script_hash,
        "selected_profile_ids": selected,
        "baseline": baselines[0],
        "shared": shared,
        "sources": source_binding,
        "profiles": profiles_public,
    }
    public = {
        "schema": SCHEMA,
        "kind": "embedder-campaign-federation",
        "script_sha256": script_hash,
        "federation_digest": _digest(binding_inputs),
        "shared": shared,
        "selected_profile_ids": selected,
        "baseline": baselines[0],
        "sources": source_public,
        "profiles": profiles_public,
    }
    return {"public": public, "sources": sources, "profiles": profile_records}


def build_report(
        federation: dict, *, blind_private: Path | None = None,
        blind_scores: Path | None = None, quality_path: Path | None = None,
        draft: bool = False) -> dict:
    public = federation["public"]
    blind_paths = (blind_private, blind_scores, quality_path)
    if any(path is not None for path in blind_paths) \
            and not all(path is not None for path in blind_paths):
        raise FederationError(
            "report needs blind private, judgments, and quality tasks together")
    blind = import_blind_scores(
        federation, blind_private, blind_scores, quality_path,
    ) if all(path is not None for path in blind_paths) else None
    missing_platform = [
        profile_id for profile_id in public["selected_profile_ids"]
        if not federation["profiles"][profile_id][0]["platform_support"][
            profile_id]["complete"]
    ]
    missing = []
    if blind is None:
        missing.append("cross_campaign_blind_review")
    if missing_platform:
        missing.append("physical_mac_and_windows_cpu:" + ",".join(missing_platform))
    if missing and not draft:
        raise FederationError(
            "report evidence is incomplete; use --draft only for diagnostics: "
            + "; ".join(missing))
    baseline_id = public["baseline"]
    baseline_source, baseline_row = federation["profiles"][baseline_id]
    baseline = dict(baseline_row["metrics"])
    baseline["physical_cpu_support"] = baseline_source["platform_support"][baseline_id][
        "complete"]
    reproduction = selection.baseline_reproduction(baseline)
    complete = not missing
    rows = {}
    for profile_id in public["selected_profile_ids"]:
        source, row = federation["profiles"][profile_id]
        metrics = dict(row["metrics"])
        support = source["platform_support"][profile_id]
        metrics["physical_cpu_support"] = support["complete"]
        decision = None
        if profile_id != baseline_id:
            try:
                decision = selection.adoption_decision(
                    baseline, metrics,
                    baseline_blind=blind["profiles"][baseline_id] if blind else None,
                    candidate_blind=blind["profiles"][profile_id] if blind else None,
                )
            except selection.HarnessError as exc:
                raise FederationError(str(exc)) from exc
            if blind is None:
                decision = {
                    **decision, "adopt": False,
                    "draft_pending": ["cross_campaign_blind_review"],
                }
        rows[profile_id] = {
            "metrics": metrics,
            "platform_support": support,
            "blind": blind["profiles"][profile_id] if blind else None,
            "decision": decision,
            "eligible_for_selection": bool(
                complete and reproduction["pass"]
                and decision and decision["adopt"]),
        }
    winners = [profile_id for profile_id, row in rows.items()
               if row["eligible_for_selection"]]
    winners.sort(key=lambda profile_id: (
        -rows[profile_id]["blind"]["points"] / rows[profile_id]["blind"]["possible"],
        -rows[profile_id]["metrics"]["semantic_correct"],
        -rows[profile_id]["metrics"]["hybrid_correct"],
        -rows[profile_id]["metrics"]["calibration_gap"],
        -rows[profile_id]["metrics"]["dedup_effective_rows_per_s"],
        rows[profile_id]["metrics"]["q8_10m_bytes"], profile_id,
    ))
    report = {
        "schema": SCHEMA,
        "kind": "embedder-federated-report",
        "federation_digest": public["federation_digest"],
        "snapshot_digest": public["shared"]["snapshot"]["digest"],
        "measurement_contract": {
            key: public["shared"][key] for key in (
                "campaign_contract_version", "benchmark_environment",
                "benchmark_environment_digest", "quality_protocol",
                "quality_protocol_digest", "performance_protocol",
                "performance_protocol_digest")
        },
        "baseline": baseline_id,
        "baseline_reproduction": reproduction,
        "draft": draft,
        "complete": complete,
        "missing_evidence": missing,
        "campaign_valid": bool(complete and reproduction["pass"]),
        "cross_campaign_blind_complete": blind is not None,
        "blind_review": blind,
        "profiles": rows,
        "winner": winners[0] if winners else None,
        "winner_order": winners,
    }
    report["report_digest"] = _digest(report)
    return report


def build_blind_packet(federation: dict, quality_path: Path) -> tuple[dict, dict]:
    quality_input = _input_path(quality_path)
    quality_hash = _regular_digest(quality_input, "quality task fixture")
    quality_path = quality_input.resolve()
    try:
        quality = selection.load_quality(quality_path)
    except selection.HarnessError as exc:
        raise FederationError(str(exc)) from exc
    if _regular_digest(quality_input, "quality task fixture") != quality_hash:
        raise FederationError("quality task fixture moved while loading")
    public = federation["public"]
    if _digest(quality) != public["shared"]["quality_digest"]:
        raise FederationError("quality task fixture does not match the campaigns")
    profile_ids = public["selected_profile_ids"]
    seed = int(hashlib.sha256(
        (public["federation_digest"] + public["shared"]["quality_digest"]).encode("ascii")
    ).hexdigest()[:16], 16)
    rng = random.Random(seed)
    mapping = {}
    packet_tasks = []
    for task in quality:
        order = list(profile_ids)
        rng.shuffle(order)
        labels = [chr(ord("A") + index) for index in range(len(order))]
        mapping[task["id"]] = dict(zip(labels, order))
        options = []
        for label, profile_id in zip(labels, order):
            _source, row = federation["profiles"][profile_id]
            matches = [item for item in row["quality"]["outcomes"]
                       if item["task"] == task["id"] and item["lane"] == "semantic"]
            if len(matches) != 1:
                raise FederationError(f"{profile_id} is missing blind task {task['id']}")
            hits = [{"evidence": hit.get("evidence"), "project": hit.get("project")}
                    for hit in matches[0]["hits"]]
            options.append({"label": label, "hits": hits})
        packet_tasks.append({
            "id": task["id"], "query": task["query"], "target": task["target"],
            "options": options,
        })
    packet = {
        "schema": SCHEMA,
        "federation_digest": public["federation_digest"],
        "snapshot_digest": public["shared"]["snapshot"]["digest"],
        "quality_digest": public["shared"]["quality_digest"],
        "rubric": {"0": "not useful", "1": "useful", "2": "directly answers"},
        "tasks": packet_tasks,
    }
    packet["blind_digest"] = _digest(packet)
    private = {
        "schema": SCHEMA,
        "federation_digest": public["federation_digest"],
        "snapshot_digest": public["shared"]["snapshot"]["digest"],
        "quality_digest": public["shared"]["quality_digest"],
        "profile_ids": profile_ids,
        "blind_digest": packet["blind_digest"],
        "mapping": mapping,
    }
    return packet, private


def import_blind_scores(federation: dict, private_path: Path,
                        scores_path: Path, quality_path: Path) -> dict:
    private, _private_hash = _read_regular_json(
        _input_path(private_path), "federated blind private map")
    scores, _scores_hash = _read_regular_json(
        _input_path(scores_path), "federated blind judgments")
    _packet, expected_private = build_blind_packet(federation, quality_path)
    quality_hash = _regular_digest(_input_path(quality_path), "quality task fixture")
    if private != expected_private:
        raise FederationError("federated blind private map is not the deterministic export")
    public = federation["public"]
    private = _keys(
        private,
        {"schema", "federation_digest", "snapshot_digest", "quality_digest",
         "profile_ids", "blind_digest", "mapping"},
        "federated blind private map",
    )
    expected_private = {
        "schema": SCHEMA,
        "federation_digest": public["federation_digest"],
        "snapshot_digest": public["shared"]["snapshot"]["digest"],
        "quality_digest": public["shared"]["quality_digest"],
        "profile_ids": public["selected_profile_ids"],
    }
    if any(private.get(key) != value for key, value in expected_private.items()):
        raise FederationError("federated blind private map is stale")
    if type(private["schema"]) is not int:
        raise FederationError("federated blind private schema must be an integer")
    mapping = private["mapping"]
    profile_ids = public["selected_profile_ids"]
    if not isinstance(mapping, dict) or not mapping:
        raise FederationError("federated blind private map has no tasks")
    for task_id, labels in mapping.items():
        if (not isinstance(task_id, str) or not task_id or not isinstance(labels, dict)
                or len(labels) != len(profile_ids) or set(labels.values()) != set(profile_ids)
                or any(not isinstance(label, str) or not label for label in labels)):
            raise FederationError("federated blind private map is incomplete")
    _sha(private["blind_digest"], "federated blind digest")
    scores = _keys(
        scores,
        {"schema", "federation_digest", "blind_digest", "judgments"},
        "federated blind judgments",
    )
    if (type(scores["schema"]) is not int or scores["schema"] != SCHEMA
            or scores["federation_digest"] != public["federation_digest"]
            or scores["blind_digest"] != private["blind_digest"]
            or not isinstance(scores["judgments"], list)):
        raise FederationError("federated blind judgments are stale")
    totals = {profile_id: {"points": 0, "possible": 0}
              for profile_id in public["selected_profile_ids"]}
    seen = set()
    for judgment in scores["judgments"]:
        if (not isinstance(judgment, dict)
                or set(judgment) - {"task", "ratings", "notes"}):
            raise FederationError("federated blind judgment shape is invalid")
        task_id = judgment.get("task")
        ratings = judgment.get("ratings")
        if task_id not in mapping or task_id in seen or not isinstance(ratings, dict):
            raise FederationError("federated blind judgment task is invalid")
        if set(ratings) != set(mapping[task_id]):
            raise FederationError(f"blind ratings for {task_id} omit an option")
        seen.add(task_id)
        for label, rating in ratings.items():
            if type(rating) is not int or not 0 <= rating <= 2:
                raise FederationError("blind ratings must be integers from 0 through 2")
            profile_id = mapping[task_id][label]
            totals[profile_id]["points"] += rating
            totals[profile_id]["possible"] += 2
    if seen != set(mapping):
        raise FederationError("federated blind judgments omit tasks")
    result = {
        "schema": SCHEMA,
        "federation_digest": public["federation_digest"],
        "blind_digest": private["blind_digest"],
        "task_count": len(mapping),
        "profiles": totals,
        "input_hashes": {
            "private": _private_hash,
            "scores": _scores_hash,
            "quality_tasks": quality_hash,
        },
    }
    result["result_digest"] = _digest(result)
    return result


def _add_campaigns(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--campaign", action="append", nargs=2, required=True,
        metavar=("RUN_DIR", "MANIFEST"),
        help="repeat once per exact campaign run directory and manifest",
    )


def _specs(args: argparse.Namespace) -> list[tuple[Path, Path]]:
    return [(Path(run_dir), Path(manifest)) for run_dir, manifest in args.campaign]


def _collision_path(path: Path) -> Path:
    return _input_path(path).resolve()


def _guard_output_paths(args: argparse.Namespace) -> None:
    outputs = []
    if getattr(args, "output", None) is not None:
        outputs.append(args.output)
    if args.command == "blind-export":
        outputs.append(args.private)
    if not outputs:
        return
    resolved_outputs = [_collision_path(path) for path in outputs]
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise FederationError("output paths must be distinct")
    specs = _specs(args)
    roots = [_collision_path(run_dir) for run_dir, _manifest in specs]
    inputs = [_collision_path(manifest) for _run_dir, manifest in specs]
    for name in ("quality_tasks", "private", "scores", "blind_private", "blind_scores"):
        value = getattr(args, name, None)
        if value is not None and not (args.command == "blind-export" and name == "private"):
            inputs.append(_collision_path(value))
    for output in resolved_outputs:
        if output in inputs:
            raise FederationError(f"output path collides with an input: {output}")
        for root in roots:
            if output == root or root in output.parents or output in root.parents:
                raise FederationError(f"output path collides with campaign tree: {output}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate and bind staged campaigns")
    _add_campaigns(validate)
    validate.add_argument("--output", type=Path)
    report = sub.add_parser("report", help="compare adoption gates across campaigns")
    _add_campaigns(report)
    report.add_argument("--blind-private", type=Path)
    report.add_argument("--blind-scores", type=Path)
    report.add_argument("--quality-tasks", type=Path)
    report.add_argument("--draft", action="store_true")
    report.add_argument("--output", type=Path)
    export = sub.add_parser("blind-export", help="create one cross-campaign blind packet")
    _add_campaigns(export)
    export.add_argument("--quality-tasks", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--private", type=Path, required=True)
    imported = sub.add_parser("blind-import", help="validate cross-campaign blind ratings")
    _add_campaigns(imported)
    imported.add_argument("--private", type=Path, required=True)
    imported.add_argument("--scores", type=Path, required=True)
    imported.add_argument("--quality-tasks", type=Path, required=True)
    imported.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _guard_output_paths(args)
        federation = validate_campaigns(_specs(args))
        if args.command == "validate":
            result = federation["public"]
        elif args.command == "report":
            result = build_report(
                federation, blind_private=args.blind_private,
                blind_scores=args.blind_scores, quality_path=args.quality_tasks,
                draft=args.draft,
            )
        elif args.command == "blind-export":
            packet, private = build_blind_packet(federation, args.quality_tasks)
            _write_json(args.output, packet)
            _write_json(args.private, private)
            result = {
                "federation_digest": federation["public"]["federation_digest"],
                "blind_digest": packet["blind_digest"],
                "output": str(args.output.expanduser().resolve()),
                "private": str(args.private.expanduser().resolve()),
            }
        else:
            result = import_blind_scores(
                federation, args.private, args.scores, args.quality_tasks)
            _write_json(args.output, result)
        if getattr(args, "output", None) is not None and args.command in {"validate", "report"}:
            _write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (FederationError, selection.HarnessError, OSError, TypeError, ValueError) as exc:
        print(f"embedder federation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
