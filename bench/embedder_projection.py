#!/usr/bin/env python3
"""Fail-closed current-layout semantic indexing projections.

The projection keeps model inference, source planning, f32 publication, and
derived accelerator work separate. Only the q8/f16 stages use a multi-size
fit; the incremental embed phases are explicit extrapolations from one measured
top-up and remain labelled as such.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
DEFAULT_TARGET_ROWS = 10_000_000
HASH_BYTES_PER_ROW = 17


class ProjectionError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _positive_int(value: Any, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProjectionError(f"{where} must be a positive integer")
    return value


def _seconds(value: Any, where: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectionError(f"{where} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        qualifier = "positive " if positive else "non-negative "
        raise ProjectionError(f"{where} must be a {qualifier}finite number")
    return number


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectionError(f"{where} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], keys: set[str], where: str) -> None:
    missing = keys - set(value)
    unknown = set(value) - keys
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise ProjectionError(f"{where}: {'; '.join(detail)}")


def _ceil_scale(value: int, numerator: int, denominator: int) -> int:
    return (value * numerator + denominator - 1) // denominator


def _fit(points: list[tuple[int, float]], label: str,
         target_rows: int) -> dict[str, Any]:
    ordered = sorted(points)
    if len(ordered) < 2 or len({rows for rows, _ in ordered}) < 2:
        raise ProjectionError(f"{label} needs at least two distinct measured sizes")
    slopes = [
        (right_s - left_s) / ((right_rows - left_rows) / 1_000_000.0)
        for index, (left_rows, left_s) in enumerate(ordered)
        for right_rows, right_s in ordered[index + 1:]
        if right_rows != left_rows
    ]
    slope = max(0.0, statistics.median(slopes))
    intercept = max(0.0, statistics.median(
        seconds - slope * rows / 1_000_000.0 for rows, seconds in ordered))
    estimate = intercept + slope * target_rows / 1_000_000.0
    residuals = [
        seconds - (intercept + slope * rows / 1_000_000.0)
        for rows, seconds in ordered
    ]
    positive_residual = max(0.0, max(residuals))
    return {
        "method": "theil-sen-affine",
        "basis": [
            {"rows": rows, "seconds": round(seconds, 6)}
            for rows, seconds in ordered
        ],
        "measured_row_range": [ordered[0][0], ordered[-1][0]],
        "intercept_s": round(intercept, 6),
        "seconds_per_million_rows": round(slope, 6),
        "fit_estimate_s": round(estimate, 3),
        "max_positive_basis_residual_s": round(positive_residual, 6),
        "projected_s": round(estimate + positive_residual, 3),
    }


def _q8_scale_basis(report: Any, dim: int, target_rows: int) -> dict[str, Any]:
    value = _require_mapping(report, "q8_scale_report")
    if type(value.get("schema")) is not int or value["schema"] != 2:
        raise ProjectionError("q8_scale_report.schema must equal integer 2")
    campaigns = value.get("campaigns")
    if not isinstance(campaigns, list) or not campaigns:
        raise ProjectionError("q8_scale_report.campaigns must be a non-empty array")
    q8_points = []
    group_points = []
    f16_points = []
    seen_initial_rows = set()
    seen_topup_rows = set()
    for index, raw in enumerate(campaigns):
        where = f"q8_scale_report.campaigns[{index}]"
        campaign = _require_mapping(raw, where)
        rows = _positive_int(campaign.get("rows"), f"{where}.rows")
        observed_dim = _positive_int(campaign.get("dim"), f"{where}.dim")
        if observed_dim != dim:
            raise ProjectionError(
                f"{where}.dim is {observed_dim}, expected selected dimension {dim}")
        if rows in seen_initial_rows:
            raise ProjectionError(f"q8 scale repeats the {rows}-row measurement")
        seen_initial_rows.add(rows)
        storage = _require_mapping(campaign.get("storage"), f"{where}.storage")
        build = _require_mapping(campaign.get("build"), f"{where}.build")
        expected = {
            "f32_bytes": rows * dim * 4,
            "f16_bytes": rows * dim * 2,
            "q8_bytes": 64 + rows * (dim + 4),
            "group_bytes": 64 + rows * 4,
        }
        for name, wanted in expected.items():
            actual = storage.get(name)
            if type(actual) is not int or actual != wanted:
                raise ProjectionError(
                    f"{where}.storage.{name} is {actual!r}, expected {wanted}")
        topup = _positive_int(build.get("topup_rows"), f"{where}.build.topup_rows")
        rebuilt_rows = rows + topup
        if rebuilt_rows in seen_topup_rows:
            raise ProjectionError(
                f"q8 scale repeats the {rebuilt_rows}-row top-up rebuild")
        seen_topup_rows.add(rebuilt_rows)
        expected_topup = 64 + rebuilt_rows * (dim + 4)
        actual_topup = storage.get("q8_topup_bytes")
        if type(actual_topup) is not int or actual_topup != expected_topup:
            raise ProjectionError(
                f"{where}.storage.q8_topup_bytes is {actual_topup!r}, "
                f"expected {expected_topup}")
        q8_points.append((
            rebuilt_rows,
            _seconds(build.get("q8_full_rebuild_s"),
                     f"{where}.build.q8_full_rebuild_s"),
        ))
        group_points.append((
            rows,
            _seconds(build.get("group_source_initial_s"),
                     f"{where}.build.group_source_initial_s"),
        ))
        f16_points.append((
            rows,
            _seconds(build.get("f16_initial_s"),
                     f"{where}.build.f16_initial_s"),
        ))
    return {
        "report_digest": _digest(value),
        "campaign_count": len(campaigns),
        "family_group_source": _fit(
            group_points, "family-group source fit", target_rows),
        "q8_group_derivation": _fit(
            q8_points, "q8 full-rebuild fit", target_rows),
        "exact_f16_derivation": _fit(
            f16_points, "f16 derivation fit", target_rows),
    }


def project_current_layout(*, dim: int, measured_incremental: Any,
                           q8_scale_report: Any,
                           target_rows: int = DEFAULT_TARGET_ROWS) -> dict[str, Any]:
    """Project one selected model with a measured incremental publication.

    ``measured_incremental`` must describe the files and phase telemetry from the
    same completed top-up generation. q8 measurements must contain two or more
    exact-format campaigns for the selected dimension.
    """
    dim = _positive_int(dim, "dim")
    target_rows = _positive_int(target_rows, "target_rows")
    measured = _require_mapping(measured_incremental, "measured_incremental")
    keys = {
        "added_rows", "published_rows", "inferred_rows", "source_rows",
        "source_bytes", "f32_bytes", "ids_bytes", "hashes_bytes", "phases_s",
    }
    _require_exact_keys(measured, keys, "measured_incremental")
    added_rows = _positive_int(measured["added_rows"], "measured_incremental.added_rows")
    published_rows = _positive_int(
        measured["published_rows"], "measured_incremental.published_rows")
    inferred_rows = _positive_int(
        measured["inferred_rows"], "measured_incremental.inferred_rows")
    source_rows = _positive_int(
        measured["source_rows"], "measured_incremental.source_rows")
    source_bytes = _positive_int(
        measured["source_bytes"], "measured_incremental.source_bytes")
    f32_bytes = _positive_int(measured["f32_bytes"], "measured_incremental.f32_bytes")
    ids_bytes = _positive_int(measured["ids_bytes"], "measured_incremental.ids_bytes")
    hashes_bytes = _positive_int(
        measured["hashes_bytes"], "measured_incremental.hashes_bytes")
    if inferred_rows > added_rows:
        raise ProjectionError("inferred_rows cannot exceed the measured top-up")
    if source_rows != published_rows:
        raise ProjectionError(
            "the measured top-up must be a full-coverage source/publication generation")
    if target_rows < published_rows:
        raise ProjectionError("target_rows cannot be smaller than the measured generation")
    expected_f32 = published_rows * dim * 4
    if f32_bytes != expected_f32:
        raise ProjectionError(
            f"measured f32 size is {f32_bytes}, expected {expected_f32}")
    expected_hashes = published_rows * HASH_BYTES_PER_ROW
    if hashes_bytes != expected_hashes:
        raise ProjectionError(
            f"measured hash size is {hashes_bytes}, expected {expected_hashes}")

    phases = _require_mapping(measured["phases_s"], "measured_incremental.phases_s")
    phase_keys = {"plan", "load", "inference", "f32_publish"}
    _require_exact_keys(phases, phase_keys, "measured_incremental.phases_s")
    plan_s = _seconds(phases["plan"], "measured_incremental.phases_s.plan")
    load_s = _seconds(
        phases["load"], "measured_incremental.phases_s.load", positive=False)
    inference_s = _seconds(
        phases["inference"], "measured_incremental.phases_s.inference")
    publication_s = _seconds(
        phases["f32_publish"], "measured_incremental.phases_s.f32_publish")

    target_source_bytes = _ceil_scale(
        source_bytes, target_rows, source_rows)
    plan_scale = max(
        target_rows / source_rows,
        target_source_bytes / source_bytes,
    )
    projected_plan_s = plan_s * plan_scale
    unique_fraction = inferred_rows / added_rows
    inference_rows_per_s = inferred_rows / inference_s
    publication_scale = target_rows / published_rows
    projected_publication_s = publication_s * publication_scale

    target_f32_bytes = target_rows * dim * 4
    target_hash_bytes = target_rows * HASH_BYTES_PER_ROW
    target_ids_bytes = _ceil_scale(ids_bytes, target_rows, published_rows)
    q8_basis = _q8_scale_basis(q8_scale_report, dim, target_rows)
    q8_bytes = 64 + target_rows * (dim + 4)
    group_bytes = 64 + target_rows * 4
    exact_f16_bytes = target_rows * dim * 2
    q8_stages_s = sum(float(q8_basis[name]["projected_s"]) for name in (
        "family_group_source", "q8_group_derivation", "exact_f16_derivation"))
    modeled_sum = (
        projected_plan_s + load_s + inference_s
        + projected_publication_s + q8_stages_s)

    return {
        "schema": 1,
        "layout": "monolithic-f32-plus-derived-q8-group-f16",
        "dim": dim,
        "target_rows": target_rows,
        "incremental_inference": {
            "added_rows": added_rows,
            "inferred_unique_rows": inferred_rows,
            "unique_fraction": round(unique_fraction, 9),
            "seconds": round(inference_s, 6),
            "unique_rows_per_s": round(inference_rows_per_s, 3),
            "projected_s": round(inference_s, 6),
            "method": "measured delta inference; independent of retained corpus rows",
        },
        "source_reconcile_and_plan": {
            "basis_rows": source_rows,
            "basis_source_bytes": source_bytes,
            "basis_seconds": round(plan_s, 6),
            "target_source_bytes_estimated": target_source_bytes,
            "projected_s": round(projected_plan_s, 3),
            "method": "row and current-corpus-byte proportional; larger ratio wins",
        },
        "model_load": {
            "measured_fixed_s": round(load_s, 6),
            "method": "measured top-up load; not multiplied by corpus size",
        },
        "full_f32_publication": {
            "basis_rows": published_rows,
            "basis_seconds": round(publication_s, 6),
            "basis_f32_bytes_exact": f32_bytes,
            "basis_ids_bytes_observed": ids_bytes,
            "basis_hash_bytes_exact": hashes_bytes,
            "target_f32_bytes_exact": target_f32_bytes,
            "target_ids_bytes_estimated": target_ids_bytes,
            "target_hash_bytes_exact": target_hash_bytes,
            "projected_s": round(projected_publication_s, 3),
            "method": "measured whole-publication phase scaled by published rows",
        },
        "accelerator_full_rebuild": {
            "target_q8_bytes_exact": q8_bytes,
            "target_group_bytes_exact": group_bytes,
            "target_exact_f16_bytes_exact": exact_f16_bytes,
            "target_materialized_bytes_exact": q8_bytes + group_bytes + exact_f16_bytes,
            "family_group_source": q8_basis["family_group_source"],
            "q8_group_derivation": q8_basis["q8_group_derivation"],
            "exact_f16_derivation": q8_basis["exact_f16_derivation"],
            "projected_s": round(q8_stages_s, 3),
            "fit_scope": "same-dimension disposable multi-size campaigns",
            "basis_report_digest": q8_basis["report_digest"],
            "basis_campaigns": q8_basis["campaign_count"],
        },
        "modeled_components_sum_s": round(modeled_sum, 3),
        "composition": (
            "sum of named current-layout stages; never a current-wall multiplier"
        ),
        "not_modeled": [
            "semantic refs sidecar preparation",
            "fixed commit and marker swaps",
            "filesystem cache, throttling, and concurrent-load variance",
            "future segmented-index behavior",
        ],
    }


def project_full_rebuild(*, dim: int, measured_full: Any,
                         accelerator_projection: Any,
                         target_rows: int = DEFAULT_TARGET_ROWS) -> dict[str, Any]:
    dim = _positive_int(dim, "dim")
    target_rows = _positive_int(target_rows, "target_rows")
    measured = _require_mapping(measured_full, "measured_full")
    _require_exact_keys(
        measured, {"published_rows", "inferred_rows", "phases_s"},
        "measured_full")
    published_rows = _positive_int(
        measured["published_rows"], "measured_full.published_rows")
    inferred_rows = _positive_int(
        measured["inferred_rows"], "measured_full.inferred_rows")
    if inferred_rows > published_rows or target_rows < published_rows:
        raise ProjectionError("full rebuild row counts are incoherent")
    phases = _require_mapping(measured["phases_s"], "measured_full.phases_s")
    _require_exact_keys(
        phases, {"plan", "load", "inference", "f32_publish"},
        "measured_full.phases_s")
    plan_s = _seconds(phases["plan"], "measured_full.phases_s.plan")
    load_s = _seconds(
        phases["load"], "measured_full.phases_s.load", positive=False)
    inference_s = _seconds(
        phases["inference"], "measured_full.phases_s.inference")
    publish_s = _seconds(
        phases["f32_publish"], "measured_full.phases_s.f32_publish")
    accelerator = _require_mapping(
        accelerator_projection, "accelerator_projection")
    if (accelerator.get("dim") != dim
            or accelerator.get("target_rows") != target_rows):
        raise ProjectionError("accelerator projection dimension or target drifted")
    accelerator_stage = _require_mapping(
        accelerator.get("accelerator_full_rebuild"),
        "accelerator_projection.accelerator_full_rebuild")
    accelerator_s = _seconds(
        accelerator_stage.get("projected_s"),
        "accelerator_projection.accelerator_full_rebuild.projected_s")
    scale = target_rows / published_rows
    projected = {
        "source_reconcile_and_plan_s": round(plan_s * scale, 3),
        "model_load_s": round(load_s, 6),
        "inference_s": round(inference_s * scale, 3),
        "f32_publication_s": round(publish_s * scale, 3),
        "accelerator_rebuild_s": round(accelerator_s, 3),
    }
    return {
        "schema": 1,
        "layout": "monolithic-full-rebuild",
        "dim": dim,
        "target_rows": target_rows,
        "basis_rows": published_rows,
        "basis_inferred_unique_rows": inferred_rows,
        "projected_inferred_unique_rows": math.ceil(
            target_rows * inferred_rows / published_rows),
        "components": projected,
        "modeled_components_sum_s": round(sum(projected.values()), 3),
        "method": (
            "row-proportional measured full-build phases plus the same-dimension "
            "multi-size accelerator fit"
        ),
    }


def project_from_record(value: Any) -> dict[str, Any]:
    record = _require_mapping(value, "input")
    _require_exact_keys(
        record,
        {"schema", "dim", "target_rows", "measured_incremental", "q8_scale_report"},
        "input",
    )
    if record["schema"] != 1:
        raise ProjectionError("input.schema must be 1")
    return project_current_layout(
        dim=record["dim"], target_rows=record["target_rows"],
        measured_incremental=record["measured_incremental"],
        q8_scale_report=record["q8_scale_report"],
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProjectionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProjectionError(f"non-finite JSON number {value}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"could not read {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = project_from_record(_read_json(args.input))
        payload = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        if args.output is None:
            sys.stdout.write(payload)
        else:
            temporary = args.output.with_name(f".{args.output.name}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ProjectionError) as exc:
        print(f"embedder projection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
