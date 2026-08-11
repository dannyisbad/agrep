"""Versioned, shadow-only schemas for SABEL-Lite candidate-loss traces.

This module deliberately has no reader, writer, ranking, or rendering hooks.  A
validated generation manifest is metadata for a manifest-last publication; it
is not proof that a publisher actually followed that protocol.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import PurePosixPath
from typing import Mapping


SCHEMA_VERSION = 1
GENERATION_SCHEMA = "agrep.sabel-shadow-generation"
TRACE_SCHEMA = "agrep.sabel-shadow-trace"
CANDIDATE_TRACE_SCHEMA = "agrep.sabel-candidate-loss-trace"
CANDIDATE_TRACE_VERSION = 2
TRIAL_SCHEMA = "agrep.sabel-task-trial"
ACTION_TRACE_SCHEMA = "agrep.sabel-observed-action-trace"
RETRIEVAL_SCHEMA = "agrep.sabel-retrieval-invocation"
RENDERER_SCHEMA = "agrep.sabel-rendered-observation"
ANNOTATION_SCHEMA = "agrep.sabel-task-annotation"
FINAL_CLAIM_SCHEMA = "agrep.sabel-final-claim-assessment"
POLICY_EVALUATION_SCHEMA = "agrep.sabel-policy-evaluation"
SEMANTIC_CANDIDATE_LOSS_SCHEMA = "agrep.sabel-semantic-candidate-loss"
SEMANTIC_CANDIDATE_LOSS_VERSION = 2
SEMANTIC_CANDIDATE_LOSS_FIELD = "_sabel_semantic_candidate_loss"
SEMANTIC_EFFECTIVE_REQUEST_SCHEMA = "agrep.sabel-semantic-effective-request.v1"
SEMANTIC_EFFECTIVE_REQUEST_LEVELS = frozenset(("hybrid", "message-session"))
SEMANTIC_EFFECTIVE_REQUEST_MAX_RESULTS = 200
SEMANTIC_EFFECTIVE_REQUEST_DEFAULT_EXCLUDED_ROLES = (
    "control", "harness", "recap", "synthetic",
)
SEMANTIC_EFFECTIVE_REQUEST_FILTER_KEYS = frozenset((
    "agent", "project", "exclude_project", "who", "model", "model_soft",
    "chat", "since_ms", "until_ms", "exclude_session",
    "exclude_session_from_turn", "exclude_family", "_family_diverse",
    "_exclude_who", "_include_who", "_exclude_sessions",
))
SEMANTIC_CANDIDATE_LOSS_H = (2, 4, 8)
SEMANTIC_SERVING_STAGES = (
    "eligibility_filter", "session_max", "family_max", "output",
)
SEMANTIC_ABLATION_STAGES = (
    "exact_copy", "session_top_h", "family_top_h",
)
STAGES = (
    "raw", "view", "exact_identity", "session", "family", "render",
)
CHECKPOINTS = ("frozen_source", "index", *STAGES)

_HEX = frozenset("0123456789abcdef")
_COVERAGE_STATES = frozenset(
    ("complete", "index_incomplete", "snapshot_partial", "unknown")
)
_TRUNCATION_STATES = frozenset(
    ("not_truncated", "pool_truncated", "render_truncated",
     "multiple_truncations", "unknown")
)
_BUDGET_STATES = frozenset(
    ("within_budget", "budget_exhausted", "not_measured")
)
_SUPPORT_STATES = frozenset(
    ("direct", "partial", "topical_only", "contradictory", "unrelated",
     "omitted_by_view", "unavailable_or_unjudged", "not_judged")
)
_TRUSTED_LABEL_KINDS = frozenset(("human", "adjudicated", "fixture"))
_GOLD_STATES = frozenset(
    ("gold_not_judged", "source_bound", "current_authority_required",
     "mixed_authority_required")
)
_AVAILABILITY_STATES = frozenset(
    ("present", "not_present_in_observed_set", "outside_bounded_coverage",
     "gold_not_judged", "not_applicable")
)
_RESULT_STATES = frozenset(
    ("direct_historical_support", "partial_historical_support",
     "contradictory_historical_evidence", "no_supported_evidence_found",
     "current_authority_required", "not_evaluated")
)
_CAPTURE_STATES = frozenset(
    ("captured", "unavailable", "not_shown", "not_applicable")
)
_CONTEXT_CONDITIONS = frozenset(
    ("fresh_chat", "mid_flow", "immediate_post_compaction", "replay")
)
_TRIAL_INPUT_SURFACES = (
    "user_request", "visible_conversation", "system_instructions",
    "developer_instructions", "persistent_policy", "post_compaction_hook",
    "ordinary_prompt_hook", "tool_registry", "tool_help_contract",
    "workspace_state", "provider_native_events",
)
_ACTION_EVENT_KINDS = frozenset(
    ("context_boundary", "hook_receipt", "assistant_text",
     "tool_invocation", "tool_result", "final_response")
)
_ACTION_STATUSES = frozenset(
    ("observed", "proposed", "rejected_by_harness_or_policy",
     "failed_before_execution", "executed_failed", "executed_completed",
     "result_unavailable")
)
_ACTION_TRIGGERS = frozenset(
    ("agent_decision_after_prompt", "hook_side_effect", "replay_harness",
     "unknown")
)
_RETRIEVAL_STATUSES = frozenset(
    ("proposed", "rejected_by_harness_or_policy", "failed_before_execution",
     "executed_failed", "executed_completed", "result_unavailable")
)
_RETRIEVAL_SURFACES = frozenset(("recall", "search", "around"))
_SEMANTIC_STATES = frozenset(
    ("not_requested", "not_run", "available", "partial",
     "meaning_unavailable", "failed")
)
_LANE_CAPTURE_STATES = frozenset(
    ("captured", "not_requested", "not_run", "unavailable", "failed")
)
_LANE_KINDS = frozenset(("lexical", "semantic"))
_LANE_ROLES = frozenset(("serving", "diagnostic"))
_SELF_EXCLUSION_STATES = frozenset(
    ("profile_default", "include_self", "exclude_self_family")
)
_MAPPING_KINDS = frozenset(
    ("exact_decoded_source", "source_derived_preview", "metadata_only",
     "unresolved")
)
_MATERIALIZATION_STATES = frozenset(
    ("deterministically_materializable", "source_absent",
     "intentionally_unretained", "retained_unindexed",
     "indexed_outside_pool", "snapshot_unavailable", "redacted",
     "not_evaluable_by_policy", "unresolved")
)
_AUTHORITY_KINDS = frozenset(
    ("historical_span", "current_snapshot", "visible_context")
)
_HISTORY_REQUIREMENTS = frozenset(
    ("required", "allowed_or_mixed", "unnecessary", "explicit_opt_out")
)
_PREVIEW_WARRANT_STATES = frozenset(
    ("open_warranted", "abstain_warranted", "genuinely_ambiguous",
     "unavailable")
)
_SOURCE_SUPPORT_STATES = frozenset(
    ("direct", "partial", "topical_only", "contradictory", "unrelated",
     "omitted_by_view", "unavailable_or_unjudged")
)
_CLAIM_AUTHORITY_STATES = frozenset(
    ("visible_context", "opened_history_span", "current_authority",
     "mixed_authority", "clarification", "unsupported")
)
_VALID_OUTCOMES = frozenset(
    ("history_retrieval", "current_authority", "visible_context_only",
     "clarification", "abstention", "no_retrieval", "multiple_routes")
)
_CONSTRAINT_KINDS = frozenset(
    ("requirement", "exclusion", "contrast", "rejected_alternative")
)
_ANNOTATION_STATES = frozenset(
    ("single_annotator", "adjudicated", "needs_adjudication")
)
_ANNOTATION_LABEL_KINDS = frozenset(("preview_warrant", "source_support"))
_SLOT_RESULT_STATES = frozenset(
    ("direct", "partial", "contradictory", "unresolved", "not_evaluated")
)
_FINAL_CLAIM_OUTCOMES = frozenset(
    ("supported", "partial", "contradicted", "unresolved", "clarification",
     "unsupported")
)
_SLOT_COVERAGE_STATES = frozenset(("claimed", "omitted", "clarified"))


class ShadowSchemaError(ValueError):
    """A shadow record is malformed or makes an unsafe claim."""


def _fail(where: str, message: str) -> None:
    raise ShadowSchemaError(f"{where}: {message}")


def _object(value: object, keys: set[str], where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(where, "must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        _fail(where, f"keys mismatch (missing={missing}, extra={extra})")
    return value


def _string(value: object, where: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(where, "must be a non-empty NUL-free string")
    return value


def _integer(value: object, where: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        _fail(where, "must be a non-negative integer")
    return value


def _boolean(value: object, where: str) -> bool:
    if type(value) is not bool:
        _fail(where, "must be a boolean")
    return value


def _utc_timestamp(value: object, where: str, *, nullable: bool = False) -> str | None:
    text = _string(value, where, nullable=nullable)
    if text is None:
        return None
    if not text.endswith("Z"):
        _fail(where, "must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _fail(where, "must be an RFC 3339 UTC timestamp")
    return text


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        _fail(where, "must be a finite number")
    return result


def _digest(value: object, where: str) -> str:
    value = _string(value, where)
    assert value is not None
    if len(value) != 64 or any(character not in _HEX for character in value):
        _fail(where, "must be a lowercase SHA-256 hex digest")
    return value


def _string_list(value: object, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        _fail(where, "must be an array")
    result = []
    for index, item in enumerate(value):
        text = _string(item, f"{where}[{index}]")
        assert text is not None
        result.append(text)
    if nonempty and not result:
        _fail(where, "must not be empty")
    if len(result) != len(set(result)):
        _fail(where, "must not contain duplicates")
    return result


def _validate_json(value: object, where: str = "record") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(where, "non-finite numbers are not canonical JSON")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json(child, f"{where}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail(where, "object keys must be strings")
            _validate_json(child, f"{where}.{key}")
        return
    _fail(where, f"unsupported JSON value {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """Return the one UTF-8 JSON encoding used by shadow record hashes."""
    _validate_json(value)
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _semantic_effective_filters(filters: Mapping[str, object] | None) -> dict:
    """Return the canonical retrieval-affecting semantic filter envelope."""
    raw = dict(filters or {})
    unknown = set(raw) - SEMANTIC_EFFECTIVE_REQUEST_FILTER_KEYS
    if unknown:
        _fail("semantic_effective_request.filters",
              f"unsupported filters {sorted(unknown)}")
    clean: dict[str, object] = {}
    string_keys = {
        "agent", "project", "exclude_project", "who", "model", "chat",
        "exclude_session",
    }
    integer_keys = {"since_ms", "until_ms", "exclude_session_from_turn"}
    boolean_keys = {"model_soft", "exclude_family", "_family_diverse"}
    array_keys = {"_exclude_who", "_include_who", "_exclude_sessions"}
    for key in sorted(raw):
        value = raw[key]
        where = f"semantic_effective_request.filters.{key}"
        if key in string_keys:
            if not isinstance(value, str) or not value or "\x00" in value:
                _fail(where, "must be a non-empty NUL-free string")
            clean[key] = value
        elif key in integer_keys:
            if type(value) is not int:
                _fail(where, "must be an integer")
            if key == "exclude_session_from_turn" and value < 0:
                _fail(where, "must be non-negative")
            clean[key] = value
        elif key in boolean_keys:
            if type(value) is not bool:
                _fail(where, "must be a boolean")
            clean[key] = value
        elif key in array_keys:
            if (not isinstance(value, (list, tuple, set, frozenset))
                    or any(not isinstance(item, str) or not item or "\x00" in item
                           for item in value)):
                _fail(where, "must be an array of non-empty strings")
            items = sorted(set(value))
            if key == "_include_who" and not items:
                _fail(where, "must not be empty")
            clean[key] = items
    # These defaults affect grouping even when their wire keys were omitted.
    clean.setdefault("exclude_family", True)
    clean.setdefault("_family_diverse", True)
    if ("exclude_session_from_turn" in clean
            and "exclude_session" not in clean):
        _fail("semantic_effective_request.filters.exclude_session_from_turn",
              "requires exclude_session")
    return clean


def semantic_effective_request(
        query: str, level: str, fetch_k: int,
        filters: Mapping[str, object] | None) -> dict:
    """Seal the normalized semantic request that produced a shadow pool."""
    normalized = query.strip() if isinstance(query, str) else ""
    if not normalized:
        _fail("semantic_effective_request.query", "must not be empty")
    if level not in SEMANTIC_EFFECTIVE_REQUEST_LEVELS:
        _fail("semantic_effective_request.level", "is unsupported")
    if (type(fetch_k) is not int
            or not 1 <= fetch_k <= SEMANTIC_EFFECTIVE_REQUEST_MAX_RESULTS):
        _fail("semantic_effective_request.fetch_k", "is outside the schema bound")
    body = {
        "schema": SEMANTIC_EFFECTIVE_REQUEST_SCHEMA,
        "query_sha256": hashlib.sha256(
            normalized.encode("utf-8", "surrogatepass")).hexdigest(),
        "level": level,
        "fetch_k": fetch_k,
        "filters": _semantic_effective_filters(filters),
    }
    return {**body, "request_sha256": canonical_sha256(body)}


def validate_semantic_effective_request(value: object) -> None:
    request = _object(value, {
        "schema", "query_sha256", "level", "fetch_k", "filters",
        "request_sha256",
    }, "semantic_effective_request")
    if request["schema"] != SEMANTIC_EFFECTIVE_REQUEST_SCHEMA:
        _fail("semantic_effective_request.schema", "is unsupported")
    _digest(request["query_sha256"], "semantic_effective_request.query_sha256")
    if request["level"] not in SEMANTIC_EFFECTIVE_REQUEST_LEVELS:
        _fail("semantic_effective_request.level", "is unsupported")
    fetch_k = _integer(request["fetch_k"], "semantic_effective_request.fetch_k")
    assert fetch_k is not None
    if not 1 <= fetch_k <= SEMANTIC_EFFECTIVE_REQUEST_MAX_RESULTS:
        _fail("semantic_effective_request.fetch_k", "is outside the schema bound")
    if not isinstance(request["filters"], Mapping):
        _fail("semantic_effective_request.filters", "must be an object")
    canonical_filters = _semantic_effective_filters(request["filters"])
    if dict(request["filters"]) != canonical_filters:
        _fail("semantic_effective_request.filters", "is not canonical")
    supplied = _digest(
        request["request_sha256"],
        "semantic_effective_request.request_sha256")
    body = {key: request[key] for key in (
        "schema", "query_sha256", "level", "fetch_k", "filters")}
    if canonical_sha256(body) != supplied:
        _fail("semantic_effective_request.request_sha256",
              "canonical hash mismatch")


def _validate_coverage(value: object, where: str) -> None:
    record = _object(value, {
        "state", "observed_count", "expected_count", "pending_count", "detail",
    }, where)
    state = record["state"]
    if state not in _COVERAGE_STATES:
        _fail(f"{where}.state", f"must be one of {sorted(_COVERAGE_STATES)}")
    observed = _integer(record["observed_count"], f"{where}.observed_count")
    expected = _integer(
        record["expected_count"], f"{where}.expected_count", nullable=True)
    pending = _integer(
        record["pending_count"], f"{where}.pending_count", nullable=True)
    detail = _string(record["detail"], f"{where}.detail", nullable=True)
    if expected is not None and observed > expected:
        _fail(where, "observed_count cannot exceed expected_count")
    if pending is not None and expected is not None and observed + pending != expected:
        _fail(where, "observed_count + pending_count must equal expected_count")
    if state == "complete":
        if expected is None or pending != 0 or observed != expected or detail is not None:
            _fail(where, "complete coverage requires observed=expected, pending=0, detail=null")
    elif detail is None:
        _fail(where, "non-complete coverage requires detail")


def _validate_diagnostics(value: object, where: str) -> None:
    record = _object(value, {"coverage", "truncation", "budget"}, where)
    _validate_coverage(record["coverage"], f"{where}.coverage")

    truncation = _object(record["truncation"],
                         {"state", "limit", "observed_count", "detail"},
                         f"{where}.truncation")
    truncation_state = truncation["state"]
    if truncation_state not in _TRUNCATION_STATES:
        _fail(f"{where}.truncation.state",
              f"must be one of {sorted(_TRUNCATION_STATES)}")
    limit = _integer(truncation["limit"], f"{where}.truncation.limit", nullable=True)
    _integer(truncation["observed_count"],
             f"{where}.truncation.observed_count")
    truncation_detail = _string(
        truncation["detail"], f"{where}.truncation.detail", nullable=True)
    if truncation_state == "not_truncated":
        if truncation_detail is not None:
            _fail(f"{where}.truncation", "not_truncated requires detail=null")
    elif truncation_detail is None:
        _fail(f"{where}.truncation", "truncated or unknown state requires detail")
    if truncation_state in ("pool_truncated", "render_truncated") and not limit:
        _fail(f"{where}.truncation", "bounded truncation requires a positive limit")

    budget = _object(record["budget"],
                     {"state", "limit", "used", "unit", "detail"},
                     f"{where}.budget")
    budget_state = budget["state"]
    if budget_state not in _BUDGET_STATES:
        _fail(f"{where}.budget.state",
              f"must be one of {sorted(_BUDGET_STATES)}")
    budget_limit = _integer(budget["limit"], f"{where}.budget.limit", nullable=True)
    budget_used = _integer(budget["used"], f"{where}.budget.used", nullable=True)
    unit = _string(budget["unit"], f"{where}.budget.unit", nullable=True)
    budget_detail = _string(budget["detail"], f"{where}.budget.detail", nullable=True)
    if budget_state == "not_measured":
        if any(item is not None for item in
               (budget_limit, budget_used, unit, budget_detail)):
            _fail(f"{where}.budget", "not_measured requires all diagnostics null")
    else:
        if budget_limit is None or budget_used is None or unit is None:
            _fail(f"{where}.budget", "measured budget requires limit, used, and unit")
        if budget_state == "within_budget" and budget_used > budget_limit:
            _fail(f"{where}.budget", "within_budget cannot exceed its limit")
        if budget_state == "budget_exhausted":
            if budget_used < budget_limit or budget_detail is None:
                _fail(f"{where}.budget",
                      "budget_exhausted requires used>=limit and explicit detail")


def _validate_artifact(value: object, where: str) -> tuple[str, str]:
    record = _object(value, {"artifact_id", "path", "size_bytes", "sha256"}, where)
    artifact_id = _string(record["artifact_id"], f"{where}.artifact_id")
    path = _string(record["path"], f"{where}.path")
    assert artifact_id is not None and path is not None
    parsed = PurePosixPath(path)
    if (parsed.is_absolute() or ".." in parsed.parts or not parsed.name
            or str(parsed) != path or "\\" in path):
        _fail(f"{where}.path", "must be a normalized relative POSIX path")
    if parsed.name == "manifest.json":
        _fail(f"{where}.path", "the manifest commit marker is not an artifact")
    _integer(record["size_bytes"], f"{where}.size_bytes")
    _digest(record["sha256"], f"{where}.sha256")
    return artifact_id, path


def _validate_content_artifact(value: object, where: str) -> tuple[str, int]:
    record = _object(value, {
        "artifact_id", "path", "size_bytes", "sha256", "media_type",
        "encoding",
    }, where)
    artifact_id = _string(record["artifact_id"], f"{where}.artifact_id")
    path = _string(record["path"], f"{where}.path")
    assert artifact_id is not None and path is not None
    parsed = PurePosixPath(path)
    if (parsed.is_absolute() or ".." in parsed.parts or not parsed.name
            or str(parsed) != path or "\\" in path):
        _fail(f"{where}.path", "must be a normalized relative POSIX path")
    if parsed.name == "manifest.json":
        _fail(f"{where}.path", "the manifest commit marker is not content")
    size = _integer(record["size_bytes"], f"{where}.size_bytes")
    assert size is not None
    _digest(record["sha256"], f"{where}.sha256")
    _string(record["media_type"], f"{where}.media_type")
    encoding = _string(record["encoding"], f"{where}.encoding", nullable=True)
    if encoding not in (None, "utf-8", "binary"):
        _fail(f"{where}.encoding", "must be utf-8, binary, or null")
    return artifact_id, size


def _validate_capture(value: object, where: str) -> tuple[str, str | None, int | None]:
    record = _object(value, {"state", "artifact", "detail"}, where)
    state = record["state"]
    if state not in _CAPTURE_STATES:
        _fail(f"{where}.state", f"must be one of {sorted(_CAPTURE_STATES)}")
    detail = _string(record["detail"], f"{where}.detail", nullable=True)
    if state == "captured":
        if record["artifact"] is None:
            _fail(where, "captured content requires an artifact")
        artifact_id, size = _validate_content_artifact(
            record["artifact"], f"{where}.artifact")
        if detail is not None:
            _fail(where, "captured content requires detail=null")
        return state, artifact_id, size
    if record["artifact"] is not None:
        _fail(where, "uncaptured content cannot carry an artifact")
    if detail is None:
        _fail(where, "uncaptured content requires an explicit detail")
    return state, None, None


def _validate_source_event(value: object, where: str) -> None:
    record = _object(value, {
        "adapter", "session_id", "record_locator", "record_sha256",
    }, where)
    for key in ("adapter", "session_id", "record_locator"):
        _string(record[key], f"{where}.{key}")
    _digest(record["record_sha256"], f"{where}.record_sha256")


def _validate_source_range(value: object, where: str) -> tuple[str, str, int, int]:
    record = _object(value, {
        "source_span_id", "atom_id", "decoded_start", "decoded_end",
    }, where)
    span_id = _string(record["source_span_id"], f"{where}.source_span_id")
    atom_id = _string(record["atom_id"], f"{where}.atom_id")
    start = _integer(record["decoded_start"], f"{where}.decoded_start")
    end = _integer(record["decoded_end"], f"{where}.decoded_end")
    assert span_id is not None and atom_id is not None
    assert start is not None and end is not None
    if end <= start:
        _fail(where, "decoded range must be non-empty and ordered")
    return span_id, atom_id, start, end


def _ranges_overlap(first: tuple[str, str, int, int],
                    second: tuple[str, str, int, int]) -> bool:
    return first[1] == second[1] and first[2] < second[3] and second[2] < first[3]


def _range_within(child: tuple[str, str, int, int],
                  parent: tuple[str, str, int, int]) -> bool:
    return (child[1] == parent[1] and parent[2] <= child[2]
            and child[3] <= parent[3])


def _validate_source_binding(value: object, where: str) -> tuple[str, str, int, int]:
    record = _object(value, {
        "source_range", "input_id", "adapter", "materialization_state",
    }, where)
    source_range = _validate_source_range(record["source_range"],
                                          f"{where}.source_range")
    _string(record["input_id"], f"{where}.input_id")
    _string(record["adapter"], f"{where}.adapter")
    if record["materialization_state"] not in _MATERIALIZATION_STATES:
        _fail(f"{where}.materialization_state",
              f"must be one of {sorted(_MATERIALIZATION_STATES)}")
    return source_range


def _validate_input(value: object, where: str) -> str:
    record = _object(value, {"input_id", "kind", "identity", "size_bytes", "sha256"},
                     where)
    input_id = _string(record["input_id"], f"{where}.input_id")
    _string(record["kind"], f"{where}.kind")
    _string(record["identity"], f"{where}.identity")
    _integer(record["size_bytes"], f"{where}.size_bytes")
    _digest(record["sha256"], f"{where}.sha256")
    assert input_id is not None
    return input_id


def _validate_manifest(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "generation_id", "created_at", "publication",
        "inputs", "artifacts", "coverage", "trace_count",
    }
    if require_hash:
        keys.add("manifest_sha256")
    value = _object(record, keys, "manifest")
    if value["schema"] != GENERATION_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("manifest", "unsupported schema or version")
    _string(value["generation_id"], "manifest.generation_id")
    _utc_timestamp(value["created_at"], "manifest.created_at")
    publication = _object(value["publication"],
                          {"protocol", "manifest_path", "artifacts_sealed"},
                          "manifest.publication")
    if publication != {
        "protocol": "manifest_last", "manifest_path": "manifest.json",
        "artifacts_sealed": True,
    }:
        _fail("manifest.publication", "must describe a sealed manifest-last generation")
    if not isinstance(value["inputs"], list) or not value["inputs"]:
        _fail("manifest.inputs", "must be a non-empty array")
    input_ids = [_validate_input(item, f"manifest.inputs[{index}]")
                 for index, item in enumerate(value["inputs"])]
    if len(input_ids) != len(set(input_ids)):
        _fail("manifest.inputs", "input_id values must be unique")
    if not isinstance(value["artifacts"], list) or not value["artifacts"]:
        _fail("manifest.artifacts", "must be a non-empty array")
    artifact_records = [
        _validate_artifact(item, f"manifest.artifacts[{index}]")
        for index, item in enumerate(value["artifacts"])
    ]
    artifact_ids = [item[0] for item in artifact_records]
    artifact_paths = [item[1] for item in artifact_records]
    if len(artifact_ids) != len(set(artifact_ids)):
        _fail("manifest.artifacts", "artifact_id values must be unique")
    if len(artifact_paths) != len(set(artifact_paths)):
        _fail("manifest.artifacts", "artifact paths must be unique")
    coverage = _object(value["coverage"], {"source", "index"}, "manifest.coverage")
    _validate_coverage(coverage["source"], "manifest.coverage.source")
    _validate_coverage(coverage["index"], "manifest.coverage.index")
    _integer(value["trace_count"], "manifest.trace_count")
    if require_hash:
        supplied = _digest(value["manifest_sha256"], "manifest.manifest_sha256")
        body = dict(value)
        del body["manifest_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("manifest.manifest_sha256", "canonical hash mismatch")


def validate_manifest(record: object) -> None:
    _validate_json(record)
    _validate_manifest(record, require_hash=True)


def seal_manifest(record: Mapping[str, object]) -> dict:
    """Copy, validate, and hash an unsealed manifest body."""
    body = copy.deepcopy(dict(record))
    body.pop("manifest_sha256", None)
    _validate_json(body)
    _validate_manifest(body, require_hash=False)
    body["manifest_sha256"] = canonical_sha256(body)
    validate_manifest(body)
    return body


def _validate_trial(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "trial_id", "case_id", "created_at",
        "build_id", "context_condition", "agent", "generation",
        "input_surfaces", "action_trace_id", "retrieval_invocation_ids",
        "claim_assessment_id", "final_response",
    }
    if require_hash:
        keys.add("trial_sha256")
    value = _object(record, keys, "trial")
    if value["schema"] != TRIAL_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("trial", "unsupported schema or version")
    for key in ("trial_id", "case_id", "build_id", "action_trace_id",
                "claim_assessment_id"):
        _string(value[key], f"trial.{key}")
    _utc_timestamp(value["created_at"], "trial.created_at")
    if value["context_condition"] not in _CONTEXT_CONDITIONS:
        _fail("trial.context_condition",
              f"must be one of {sorted(_CONTEXT_CONDITIONS)}")

    agent = _object(value["agent"], {
        "provider", "surface", "model", "harness", "session_id",
        "context_boundary_id",
    }, "trial.agent")
    for key in ("provider", "surface", "model", "harness", "session_id"):
        _string(agent[key], f"trial.agent.{key}")
    boundary = _string(
        agent["context_boundary_id"], "trial.agent.context_boundary_id",
        nullable=True)
    if value["context_condition"] == "immediate_post_compaction" and boundary is None:
        _fail("trial.agent.context_boundary_id",
              "post-compaction trials require an exact boundary id")

    generation = _object(value["generation"], {
        "source_generation_id", "index_generation_id", "source_coverage",
        "index_coverage",
    }, "trial.generation")
    _string(generation["source_generation_id"],
            "trial.generation.source_generation_id")
    _string(generation["index_generation_id"],
            "trial.generation.index_generation_id")
    _validate_coverage(generation["source_coverage"],
                       "trial.generation.source_coverage")
    _validate_coverage(generation["index_coverage"],
                       "trial.generation.index_coverage")

    if not isinstance(value["input_surfaces"], list):
        _fail("trial.input_surfaces", "must be an array")
    names = []
    for index, item in enumerate(value["input_surfaces"]):
        where = f"trial.input_surfaces[{index}]"
        surface = _object(item, {"surface", "capture"}, where)
        name = _string(surface["surface"], f"{where}.surface")
        assert name is not None
        names.append(name)
        state, _, _ = _validate_capture(surface["capture"], f"{where}.capture")
        if name == "user_request" and state != "captured":
            _fail(f"{where}.capture", "the exact user request must be captured")
    if names != list(_TRIAL_INPUT_SURFACES):
        _fail("trial.input_surfaces",
              f"must appear exactly in order {list(_TRIAL_INPUT_SURFACES)}")

    retrieval_ids = _string_list(
        value["retrieval_invocation_ids"], "trial.retrieval_invocation_ids")
    if len(retrieval_ids) != len(set(retrieval_ids)):
        _fail("trial.retrieval_invocation_ids", "must be unique")
    final_state, _, _ = _validate_capture(
        value["final_response"], "trial.final_response")
    if final_state in ("not_shown", "not_applicable"):
        _fail("trial.final_response",
              "a completed trial must capture the response or mark it unavailable")

    if require_hash:
        supplied = _digest(value["trial_sha256"], "trial.trial_sha256")
        body = dict(value)
        del body["trial_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("trial.trial_sha256", "canonical hash mismatch")


def validate_trial(record: object) -> None:
    _validate_json(record)
    _validate_trial(record, require_hash=True)


def seal_trial(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("trial_sha256", None)
    _validate_json(body)
    _validate_trial(body, require_hash=False)
    body["trial_sha256"] = canonical_sha256(body)
    validate_trial(body)
    return body


def _validate_action_trace(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "action_trace_id", "trial_id", "completeness",
        "events",
    }
    if require_hash:
        keys.add("action_trace_sha256")
    value = _object(record, keys, "action_trace")
    if (value["schema"] != ACTION_TRACE_SCHEMA
            or value["version"] != SCHEMA_VERSION):
        _fail("action_trace", "unsupported schema or version")
    for key in ("action_trace_id", "trial_id"):
        _string(value[key], f"action_trace.{key}")
    _validate_coverage(value["completeness"], "action_trace.completeness")
    if not isinstance(value["events"], list):
        _fail("action_trace.events", "must be an array")

    event_ids: set[str] = set()
    invocations: dict[str, tuple[str, str]] = {}
    results: set[str] = set()
    final_responses = 0
    for index, item in enumerate(value["events"]):
        where = f"action_trace.events[{index}]"
        event = _object(item, {
            "event_id", "sequence", "observed_at", "kind", "source",
            "action_id", "parent_event_id", "observation_event_ids",
            "tool_surface", "trigger", "arguments", "status", "payload",
            "hook_id", "hook_version", "hook_effect", "exit_code",
            "elapsed_ms", "error_code",
        }, where)
        event_id = _string(event["event_id"], f"{where}.event_id")
        assert event_id is not None
        if event_id in event_ids:
            _fail("action_trace.events", "event_id values must be unique")
        sequence = _integer(event["sequence"], f"{where}.sequence")
        if sequence != index + 1:
            _fail(f"{where}.sequence", "must be contiguous and one-based")
        _utc_timestamp(event["observed_at"], f"{where}.observed_at")
        kind = event["kind"]
        if kind not in _ACTION_EVENT_KINDS:
            _fail(f"{where}.kind",
                  f"must be one of {sorted(_ACTION_EVENT_KINDS)}")
        _validate_source_event(event["source"], f"{where}.source")
        action_id = _string(event["action_id"], f"{where}.action_id", nullable=True)
        parent_event_id = _string(
            event["parent_event_id"], f"{where}.parent_event_id", nullable=True)
        if parent_event_id is not None and parent_event_id not in event_ids:
            _fail(f"{where}.parent_event_id", "must name an earlier event")
        observations = _string_list(
            event["observation_event_ids"], f"{where}.observation_event_ids")
        if any(observation not in event_ids for observation in observations):
            _fail(f"{where}.observation_event_ids",
                  "must name only earlier events")
        tool_surface = _string(
            event["tool_surface"], f"{where}.tool_surface", nullable=True)
        trigger = _string(event["trigger"], f"{where}.trigger", nullable=True)
        status = _string(event["status"], f"{where}.status", nullable=True)
        if status is not None and status not in _ACTION_STATUSES:
            _fail(f"{where}.status",
                  f"must be one of {sorted(_ACTION_STATUSES)} or null")
        argument_state, _, _ = _validate_capture(
            event["arguments"], f"{where}.arguments")
        payload_state, _, _ = _validate_capture(
            event["payload"], f"{where}.payload")
        hook_id = _string(event["hook_id"], f"{where}.hook_id", nullable=True)
        hook_version = _string(
            event["hook_version"], f"{where}.hook_version", nullable=True)
        hook_effect = _string(
            event["hook_effect"], f"{where}.hook_effect", nullable=True)
        exit_code = _integer(event["exit_code"], f"{where}.exit_code", nullable=True)
        elapsed = _integer(event["elapsed_ms"], f"{where}.elapsed_ms", nullable=True)
        error_code = _string(
            event["error_code"], f"{where}.error_code", nullable=True)

        if kind == "tool_invocation":
            if action_id is None or tool_surface is None:
                _fail(where, "tool invocations require action_id and tool_surface")
            if action_id in invocations:
                _fail("action_trace.events", "action_id invocations must be unique")
            if trigger not in _ACTION_TRIGGERS:
                _fail(f"{where}.trigger",
                      f"must be one of {sorted(_ACTION_TRIGGERS)}")
            if status != "proposed" or argument_state != "captured":
                _fail(where, "tool invocations require proposed status and exact arguments")
            if payload_state != "not_applicable":
                _fail(f"{where}.payload", "tool invocation payload is represented by arguments")
            if any(value is not None for value in
                   (hook_id, hook_version, hook_effect, exit_code, elapsed, error_code)):
                _fail(where, "tool invocations cannot claim execution results")
            invocations[action_id] = (event_id, tool_surface)
        elif kind == "tool_result":
            if action_id is None or action_id not in invocations:
                _fail(where, "tool results require an earlier matching invocation")
            invocation_event_id, invocation_surface = invocations[action_id]
            if action_id in results:
                _fail("action_trace.events", "each action may have only one result")
            if parent_event_id != invocation_event_id or tool_surface != invocation_surface:
                _fail(where, "tool result must join its exact invocation and surface")
            if trigger is not None or argument_state != "not_applicable":
                _fail(where, "tool results cannot carry a trigger or arguments")
            if any(value is not None for value in (hook_id, hook_version, hook_effect)):
                _fail(where, "tool results cannot carry hook identity")
            if status in (None, "observed", "proposed"):
                _fail(where, "tool results require a terminal action status")
            if status == "executed_completed" and exit_code != 0:
                _fail(where, "completed execution requires exit_code=0")
            if status == "executed_failed" and (exit_code is None or exit_code == 0):
                _fail(where, "failed execution requires a non-zero exit code")
            if status in ("executed_completed", "executed_failed", "result_unavailable"):
                if elapsed is None:
                    _fail(where, "executed results require elapsed_ms")
            elif any(value is not None for value in (exit_code, elapsed)):
                _fail(where, "pre-execution results cannot carry execution metrics")
            if status in ("rejected_by_harness_or_policy", "failed_before_execution",
                          "executed_failed", "result_unavailable") and error_code is None:
                _fail(where, "non-success results require an error_code")
            if status == "executed_completed" and payload_state != "captured":
                _fail(where, "completed tool results require exact shown bytes")
            if status != "executed_completed" and payload_state == "not_applicable":
                _fail(where, "non-success results require shown, unavailable, or not-shown state")
            results.add(action_id)
        else:
            if any(value is not None for value in (action_id, tool_surface, trigger)):
                _fail(where, "non-tool events cannot carry tool action fields")
            if status != "observed" or argument_state != "not_applicable":
                _fail(where, "non-tool events require observed status and no arguments")
            if kind == "hook_receipt":
                if hook_id is None or hook_version is None:
                    _fail(where, "hook receipts require exact hook id and version")
                if hook_effect not in ("prompt_only", "retrieval_side_effect", "unknown"):
                    _fail(f"{where}.hook_effect",
                          "hook receipts require prompt_only, retrieval_side_effect, or unknown")
                if exit_code is None or elapsed is None:
                    _fail(where, "hook receipts require observed exit code and elapsed time")
                if exit_code != 0 and error_code is None:
                    _fail(where, "failed hooks require an error_code")
                if exit_code == 0 and error_code is not None:
                    _fail(where, "successful hooks require error_code=null")
            else:
                if any(value is not None for value in
                       (hook_id, hook_version, hook_effect, exit_code, elapsed, error_code)):
                    _fail(where, "only hook receipts carry hook execution fields")
            if payload_state == "not_applicable":
                _fail(f"{where}.payload", "observed events require captured or explicit unavailable content")
            if kind == "final_response":
                final_responses += 1
        event_ids.add(event_id)

    if final_responses > 1:
        _fail("action_trace.events", "may contain at most one final response")
    observed_count = value["completeness"]["observed_count"]
    if observed_count != len(value["events"]):
        _fail("action_trace.completeness.observed_count",
              "must equal the number of recorded events")

    if require_hash:
        supplied = _digest(
            value["action_trace_sha256"], "action_trace.action_trace_sha256")
        body = dict(value)
        del body["action_trace_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("action_trace.action_trace_sha256", "canonical hash mismatch")


def validate_action_trace(record: object) -> None:
    _validate_json(record)
    _validate_action_trace(record, require_hash=True)


def seal_action_trace(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("action_trace_sha256", None)
    _validate_json(body)
    _validate_action_trace(body, require_hash=False)
    body["action_trace_sha256"] = canonical_sha256(body)
    validate_action_trace(body)
    return body


def _validate_retrieval(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "retrieval_invocation_id", "trial_id",
        "action_trace_id", "action_id", "query_variant_id", "surface",
        "status", "query", "structured_args", "execution_options",
        "retriever", "semantic", "query_provenance", "lanes",
        "returned_handles", "opened_handle", "rendered_output", "candidate_trace_id",
        "renderer_record_id", "exit_code", "elapsed_ms",
    }
    if require_hash:
        keys.add("retrieval_sha256")
    value = _object(record, keys, "retrieval")
    if value["schema"] != RETRIEVAL_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("retrieval", "unsupported schema or version")
    for key in ("retrieval_invocation_id", "trial_id", "action_trace_id",
                "action_id", "query_variant_id"):
        _string(value[key], f"retrieval.{key}")
    if value["surface"] not in _RETRIEVAL_SURFACES:
        _fail("retrieval.surface", f"must be one of {sorted(_RETRIEVAL_SURFACES)}")
    status = value["status"]
    if status not in _RETRIEVAL_STATUSES:
        _fail("retrieval.status", f"must be one of {sorted(_RETRIEVAL_STATUSES)}")
    query_state, _, query_size = _validate_capture(value["query"], "retrieval.query")
    args_state, _, _ = _validate_capture(
        value["structured_args"], "retrieval.structured_args")
    if query_state != "captured" or args_state != "captured":
        _fail("retrieval", "every proposal requires exact query bytes and structured arguments")
    assert query_size is not None
    if query_size == 0:
        _fail("retrieval.query", "query bytes must not be empty")

    options = _object(value["execution_options"], {
        "semantic_requested", "self_exclusion", "output_profile",
        "byte_budget",
    }, "retrieval.execution_options")
    semantic_requested = _boolean(
        options["semantic_requested"],
        "retrieval.execution_options.semantic_requested")
    if options["self_exclusion"] not in _SELF_EXCLUSION_STATES:
        _fail("retrieval.execution_options.self_exclusion",
              f"must be one of {sorted(_SELF_EXCLUSION_STATES)}")
    _string(options["output_profile"], "retrieval.execution_options.output_profile")
    byte_budget = _integer(
        options["byte_budget"], "retrieval.execution_options.byte_budget",
        nullable=True)
    if byte_budget == 0:
        _fail("retrieval.execution_options.byte_budget", "must be positive or null")

    retriever = _object(value["retriever"], {
        "build_id", "profile_id", "source_generation_id",
        "index_generation_id", "dense_model_id", "lane_config_id",
        "query_constructor_id",
    }, "retrieval.retriever")
    for key in ("build_id", "profile_id", "source_generation_id",
                "index_generation_id", "lane_config_id", "query_constructor_id"):
        _string(retriever[key], f"retrieval.retriever.{key}")
    dense_model = _string(
        retriever["dense_model_id"], "retrieval.retriever.dense_model_id",
        nullable=True)

    semantic = _object(value["semantic"], {"state", "detail"}, "retrieval.semantic")
    semantic_state = semantic["state"]
    if semantic_state not in _SEMANTIC_STATES:
        _fail("retrieval.semantic.state",
              f"must be one of {sorted(_SEMANTIC_STATES)}")
    semantic_detail = _string(
        semantic["detail"], "retrieval.semantic.detail", nullable=True)
    if not semantic_requested:
        if semantic_state != "not_requested" or dense_model is not None:
            _fail("retrieval.semantic",
                  "unrequested semantic retrieval requires not_requested and no model")
    elif semantic_state == "not_requested" or dense_model is None:
        _fail("retrieval.semantic",
              "requested semantic retrieval requires an explicit state and model")
    if semantic_state in ("not_run", "partial", "meaning_unavailable", "failed") and semantic_detail is None:
        _fail("retrieval.semantic", "unrun, partial, unavailable, or failed semantics require detail")
    if semantic_state in ("available", "not_requested") and semantic_detail is not None:
        _fail("retrieval.semantic", "successful or unrequested semantics require detail=null")

    if not isinstance(value["query_provenance"], list):
        _fail("retrieval.query_provenance", "must be an array")
    previous_end = 0
    for index, item in enumerate(value["query_provenance"]):
        where = f"retrieval.query_provenance[{index}]"
        provenance = _object(item, {
            "query_start", "query_end", "state", "origin_surface_ids",
            "origin_ranges",
        }, where)
        start = _integer(provenance["query_start"], f"{where}.query_start")
        end = _integer(provenance["query_end"], f"{where}.query_end")
        assert start is not None and end is not None
        if start != previous_end or end <= start or end > query_size:
            _fail(where, "query provenance must be a contiguous in-bounds partition")
        previous_end = end
        origins = _string_list(
            provenance["origin_surface_ids"], f"{where}.origin_surface_ids")
        if not isinstance(provenance["origin_ranges"], list):
            _fail(f"{where}.origin_ranges", "must be an array")
        origin_ranges = []
        for origin_index, origin_value in enumerate(provenance["origin_ranges"]):
            origin_where = f"{where}.origin_ranges[{origin_index}]"
            origin = _object(origin_value, {
                "origin_surface_id", "artifact_id", "origin_start",
                "origin_end", "transform",
            }, origin_where)
            origin_surface_id = _string(
                origin["origin_surface_id"], f"{origin_where}.origin_surface_id")
            artifact_id = _string(
                origin["artifact_id"], f"{origin_where}.artifact_id")
            origin_start = _integer(
                origin["origin_start"], f"{origin_where}.origin_start")
            origin_end = _integer(
                origin["origin_end"], f"{origin_where}.origin_end")
            assert origin_surface_id is not None and artifact_id is not None
            assert origin_start is not None and origin_end is not None
            if origin_end <= origin_start:
                _fail(origin_where, "origin byte range must be non-empty")
            if origin["transform"] not in (
                    "exact_copy", "normalized_copy", "derived"):
                _fail(f"{origin_where}.transform",
                      "must be exact_copy, normalized_copy, or derived")
            if origin_surface_id not in origins:
                _fail(origin_where,
                      "origin range must name a declared origin surface")
            if (origin["transform"] == "exact_copy"
                    and origin_end - origin_start != end - start):
                _fail(origin_where,
                      "exact-copy origin length must equal its query span")
            origin_ranges.append(origin)
        if provenance["state"] == "observed" and (not origins or not origin_ranges):
            _fail(where,
                  "observed query spans require exact origin coordinates")
        if provenance["state"] == "novel_to_observation" and (
                origins or origin_ranges):
            _fail(where, "novel query spans cannot claim observed origins")
        if provenance["state"] not in ("observed", "novel_to_observation"):
            _fail(f"{where}.state", "must be observed or novel_to_observation")
    if previous_end != query_size:
        _fail("retrieval.query_provenance",
              "must account for every exact query byte")

    if not isinstance(value["lanes"], list):
        _fail("retrieval.lanes", "must be an array")
    lane_names = []
    captured_lanes = 0
    for index, item in enumerate(value["lanes"]):
        where = f"retrieval.lanes[{index}]"
        lane = _object(item, {
            "lane", "kind", "role", "state", "requested", "available",
            "executed", "score_kind", "diagnostics", "pool",
        }, where)
        name = _string(lane["lane"], f"{where}.lane")
        assert name is not None
        lane_names.append(name)
        if lane["kind"] not in _LANE_KINDS:
            _fail(f"{where}.kind", f"must be one of {sorted(_LANE_KINDS)}")
        if lane["role"] not in _LANE_ROLES:
            _fail(f"{where}.role", f"must be one of {sorted(_LANE_ROLES)}")
        lane_state = lane["state"]
        if lane_state not in _LANE_CAPTURE_STATES:
            _fail(f"{where}.state",
                  f"must be one of {sorted(_LANE_CAPTURE_STATES)}")
        requested = _boolean(lane["requested"], f"{where}.requested")
        available = _boolean(lane["available"], f"{where}.available")
        executed = _boolean(lane["executed"], f"{where}.executed")
        score_kind = _string(lane["score_kind"], f"{where}.score_kind", nullable=True)
        _validate_diagnostics(lane["diagnostics"], f"{where}.diagnostics")
        pool_state, _, _ = _validate_capture(lane["pool"], f"{where}.pool")
        if lane_state == "captured":
            if not (requested and available and executed) or score_kind is None:
                _fail(where, "captured lanes must be requested, available, executed, and scored")
            if pool_state != "captured":
                _fail(where, "captured lanes require an exact frozen pool artifact")
            captured_lanes += 1
        elif lane_state == "not_requested":
            if requested or available or executed or score_kind is not None:
                _fail(where, "not_requested lanes cannot claim work or scores")
            if pool_state != "not_applicable":
                _fail(where, "not_requested lanes require pool=not_applicable")
        elif lane_state == "not_run":
            if not requested or executed:
                _fail(where, "not_run lanes were requested but never executed")
            if pool_state != "not_shown":
                _fail(where, "not_run lanes require pool=not_shown")
        elif lane_state == "unavailable":
            if not requested or available or executed:
                _fail(where, "unavailable lanes are requested but neither available nor executed")
            if pool_state != "unavailable":
                _fail(where, "unavailable lanes require pool=unavailable")
        else:
            if not requested or not executed:
                _fail(where, "failed lanes must have been requested and executed")
            if pool_state != "unavailable":
                _fail(where, "failed lanes require pool=unavailable")
    if len(lane_names) != len(set(lane_names)):
        _fail("retrieval.lanes", "lane names must be unique")
    semantic_lane_states = [
        lane["state"] for lane in value["lanes"]
        if lane["kind"] == "semantic" and lane["role"] == "serving"
    ]
    if semantic_requested:
        if not semantic_lane_states:
            _fail("retrieval.semantic", "requested semantics requires a serving semantic lane")
        captured = semantic_lane_states.count("captured")
        if captured == len(semantic_lane_states):
            derived_semantic_state = "available"
        elif captured:
            derived_semantic_state = "partial"
        elif all(state == "not_run" for state in semantic_lane_states):
            derived_semantic_state = "not_run"
        elif any(state == "failed" for state in semantic_lane_states):
            derived_semantic_state = "failed"
        else:
            derived_semantic_state = "meaning_unavailable"
        if semantic_state != derived_semantic_state:
            _fail("retrieval.semantic",
                  f"state must equal serving-lane-derived {derived_semantic_state}")
    elif any(lane["kind"] == "semantic" and lane["state"] != "not_requested"
             for lane in value["lanes"]):
        _fail("retrieval.semantic",
              "semantic_requested=false forbids requested or executed semantic lanes")

    returned_handles = _string_list(
        value["returned_handles"], "retrieval.returned_handles")
    opened_handle = _string(
        value["opened_handle"], "retrieval.opened_handle", nullable=True)
    if value["surface"] == "around":
        if opened_handle is None or returned_handles:
            _fail("retrieval.opened_handle",
                  "around requires one explicit opened handle and returns no candidate handles")
        if len(opened_handle.encode("utf-8")) != query_size:
            _fail("retrieval.query",
                  "around query artifact size must equal the exact opened handle bytes")
        query_artifact = _capture_artifact(value["query"])
        assert query_artifact is not None
        if (query_artifact["encoding"] != "utf-8"
                or query_artifact["sha256"]
                != hashlib.sha256(opened_handle.encode("utf-8")).hexdigest()):
            _fail("retrieval.query",
                  "around query artifact must hash the exact opened handle bytes")
    elif opened_handle is not None:
        _fail("retrieval.opened_handle", "candidate retrieval cannot claim an opened handle")
    rendered_state, _, _ = _validate_capture(
        value["rendered_output"], "retrieval.rendered_output")
    candidate_trace_id = _string(
        value["candidate_trace_id"], "retrieval.candidate_trace_id", nullable=True)
    renderer_record_id = _string(
        value["renderer_record_id"], "retrieval.renderer_record_id", nullable=True)
    exit_code = _integer(value["exit_code"], "retrieval.exit_code", nullable=True)
    elapsed = _integer(value["elapsed_ms"], "retrieval.elapsed_ms", nullable=True)
    if status == "executed_completed":
        if exit_code != 0 or elapsed is None or rendered_state != "captured":
            _fail("retrieval", "completed invocation requires exit 0, elapsed time, and exact output")
        if value["surface"] in ("recall", "search") and not captured_lanes:
            _fail("retrieval.lanes", "completed candidate retrieval requires a captured lane")
        if value["surface"] == "around" and value["lanes"]:
            _fail("retrieval.lanes", "around opens source and cannot claim candidate lanes")
        if renderer_record_id is None:
            _fail("retrieval.renderer_record_id", "completed invocation requires a renderer record")
        if value["surface"] in ("recall", "search") and candidate_trace_id is None:
            _fail("retrieval.candidate_trace_id",
                  "completed candidate retrieval requires a candidate-loss trace")
        if value["surface"] == "around" and candidate_trace_id is not None:
            _fail("retrieval.candidate_trace_id", "around is an opened-source observation, not a candidate pool")
    elif status in ("executed_failed", "result_unavailable"):
        if elapsed is None or exit_code is None:
            _fail("retrieval", "failed or unavailable execution requires elapsed time and exit status")
        if status == "executed_failed" and exit_code == 0:
            _fail("retrieval", "failed execution requires a non-zero exit status")
        if rendered_state == "not_applicable":
            _fail("retrieval.rendered_output", "execution failure must be captured or explicitly unavailable")
    else:
        if exit_code is not None or elapsed is not None:
            _fail("retrieval", "non-executed invocation cannot carry execution metrics")
        if any(lane["executed"] for lane in value["lanes"]):
            _fail("retrieval.lanes", "non-executed invocation cannot claim executed lanes")
        if semantic_state not in ("not_requested", "not_run"):
            _fail("retrieval.semantic",
                  "non-executed invocation cannot claim semantic availability")
        if candidate_trace_id is not None or renderer_record_id is not None:
            _fail("retrieval", "non-executed invocation cannot claim trace or renderer artifacts")
        if rendered_state not in ("not_shown", "unavailable"):
            _fail("retrieval.rendered_output", "non-executed invocation must record no shown result")

    if require_hash:
        supplied = _digest(value["retrieval_sha256"], "retrieval.retrieval_sha256")
        body = dict(value)
        del body["retrieval_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("retrieval.retrieval_sha256", "canonical hash mismatch")


def validate_retrieval(record: object) -> None:
    _validate_json(record)
    _validate_retrieval(record, require_hash=True)


def seal_retrieval(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("retrieval_sha256", None)
    _validate_json(body)
    _validate_retrieval(body, require_hash=False)
    body["retrieval_sha256"] = canonical_sha256(body)
    validate_retrieval(body)
    return body


def _validate_renderer(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "renderer_record_id", "trial_id",
        "retrieval_invocation_id", "renderer", "diagnostics", "output",
        "segments",
    }
    if require_hash:
        keys.add("renderer_sha256")
    value = _object(record, keys, "renderer_record")
    if value["schema"] != RENDERER_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("renderer_record", "unsupported schema or version")
    for key in ("renderer_record_id", "trial_id", "retrieval_invocation_id"):
        _string(value[key], f"renderer_record.{key}")
    renderer = _object(value["renderer"], {
        "build_id", "profile", "format_id",
    }, "renderer_record.renderer")
    for key in ("build_id", "profile", "format_id"):
        _string(renderer[key], f"renderer_record.renderer.{key}")
    _validate_diagnostics(value["diagnostics"], "renderer_record.diagnostics")
    output_state, _, output_size = _validate_capture(
        value["output"], "renderer_record.output")
    if output_state != "captured":
        _fail("renderer_record.output", "a renderer record requires exact displayed bytes")
    assert output_size is not None

    if not isinstance(value["segments"], list):
        _fail("renderer_record.segments", "must be an array")
    candidate_ids = []
    prior_output_end = 0
    for index, item in enumerate(value["segments"]):
        where = f"renderer_record.segments[{index}]"
        segment = _object(item, {
            "candidate_id", "displayed_handle", "handle_output_start",
            "handle_output_end", "handle_sha256", "display_order",
            "output_start", "output_end", "mapping_kind",
            "visible_source_ranges", "source_mappings",
            "omitted_source_ranges",
        }, where)
        candidate_id = _string(segment["candidate_id"], f"{where}.candidate_id")
        assert candidate_id is not None
        candidate_ids.append(candidate_id)
        displayed_handle = _string(
            segment["displayed_handle"], f"{where}.displayed_handle",
            nullable=True)
        if _integer(segment["display_order"], f"{where}.display_order") != index + 1:
            _fail(f"{where}.display_order", "must be contiguous and one-based")
        output_start = _integer(segment["output_start"], f"{where}.output_start")
        output_end = _integer(segment["output_end"], f"{where}.output_end")
        assert output_start is not None and output_end is not None
        if (output_start < prior_output_end or output_end <= output_start
                or output_end > output_size):
            _fail(where, "displayed byte ranges must be ordered, non-overlapping, and in bounds")
        prior_output_end = output_end
        handle_start = _integer(
            segment["handle_output_start"], f"{where}.handle_output_start",
            nullable=True)
        handle_end = _integer(
            segment["handle_output_end"], f"{where}.handle_output_end",
            nullable=True)
        handle_sha = segment["handle_sha256"]
        if displayed_handle is None:
            if handle_start is not None or handle_end is not None or handle_sha is not None:
                _fail(where, "segments without a handle cannot claim handle bytes")
        else:
            if handle_start is None or handle_end is None:
                _fail(where, "displayed handles require an exact output byte range")
            supplied_handle_sha = _digest(handle_sha, f"{where}.handle_sha256")
            handle_bytes = displayed_handle.encode("utf-8")
            if (handle_start < output_start or handle_end > output_end
                    or handle_end - handle_start != len(handle_bytes)):
                _fail(where, "handle byte range must be in-segment and exact length")
            if supplied_handle_sha != hashlib.sha256(handle_bytes).hexdigest():
                _fail(where, "handle digest must match the displayed handle bytes")
        mapping_kind = segment["mapping_kind"]
        if mapping_kind not in _MAPPING_KINDS:
            _fail(f"{where}.mapping_kind",
                  f"must be one of {sorted(_MAPPING_KINDS)}")

        if not isinstance(segment["visible_source_ranges"], list):
            _fail(f"{where}.visible_source_ranges", "must be an array")
        visible = [
            _validate_source_range(item, f"{where}.visible_source_ranges[{range_index}]")
            for range_index, item in enumerate(segment["visible_source_ranges"])
        ]
        for range_index, source_range in enumerate(visible):
            if range_index and _ranges_overlap(visible[range_index - 1], source_range):
                _fail(f"{where}.visible_source_ranges",
                      "visible ranges for one atom must not overlap")
        if not isinstance(segment["source_mappings"], list):
            _fail(f"{where}.source_mappings", "must be an array")
        source_mappings = []
        for mapping_index, mapping_value in enumerate(segment["source_mappings"]):
            mapping_where = f"{where}.source_mappings[{mapping_index}]"
            mapping = _object(mapping_value, {
                "source_range", "output_start", "output_end",
            }, mapping_where)
            source_range = _validate_source_range(
                mapping["source_range"], f"{mapping_where}.source_range")
            mapped_start = _integer(
                mapping["output_start"], f"{mapping_where}.output_start")
            mapped_end = _integer(
                mapping["output_end"], f"{mapping_where}.output_end")
            assert mapped_start is not None and mapped_end is not None
            if (mapped_start < output_start or mapped_end > output_end
                    or mapped_end <= mapped_start):
                _fail(mapping_where,
                      "source/output mapping must be non-empty and inside its segment")
            if mapped_end - mapped_start != source_range[3] - source_range[2]:
                _fail(mapping_where,
                      "source and output byte ranges must have exactly equal length")
            if (handle_start is not None and handle_end is not None
                    and mapped_start < handle_end and handle_start < mapped_end):
                _fail(mapping_where,
                      "source evidence cannot overlap displayed handle bytes")
            source_mappings.append((source_range, mapped_start, mapped_end))
        mapped_output_ranges = [(item[1], item[2]) for item in source_mappings]
        if any(first_start < second_end and second_start < first_end
               for mapping_index, (first_start, first_end) in enumerate(
                   mapped_output_ranges)
               for second_start, second_end in
                   mapped_output_ranges[mapping_index + 1:]):
            _fail(f"{where}.source_mappings",
                  "mapped output byte ranges cannot overlap")
        mapped_ranges = [item[0] for item in source_mappings]
        if mapping_kind == "exact_decoded_source":
            if not visible or mapped_ranges != visible:
                _fail(where,
                      "exact source ranges require one ordered output mapping each")
        elif visible or source_mappings:
            _fail(where,
                  "derived, metadata, and unresolved bytes are not exact visible evidence")

        if not isinstance(segment["omitted_source_ranges"], list):
            _fail(f"{where}.omitted_source_ranges", "must be an array")
        omitted = []
        for range_index, item in enumerate(segment["omitted_source_ranges"]):
            omitted_where = f"{where}.omitted_source_ranges[{range_index}]"
            omission = _object(item, {"source_range", "reason_code", "detail"},
                               omitted_where)
            source_range = _validate_source_range(
                omission["source_range"], f"{omitted_where}.source_range")
            _string(omission["reason_code"], f"{omitted_where}.reason_code")
            _string(omission["detail"], f"{omitted_where}.detail", nullable=True)
            omitted.append(source_range)
        if any(_ranges_overlap(visible_range, omitted_range)
               for visible_range in visible for omitted_range in omitted):
            _fail(where, "the same decoded bytes cannot be both visible and omitted")
    if len(candidate_ids) != len(set(candidate_ids)):
        _fail("renderer_record.segments", "candidate rows must be unique")
    observed = value["diagnostics"]["truncation"]["observed_count"]
    if observed != len(value["segments"]):
        _fail("renderer_record.diagnostics.truncation.observed_count",
              "must equal the number of displayed candidate segments")
    budget = value["diagnostics"]["budget"]
    if budget["state"] != "not_measured" and budget["unit"] == "bytes":
        if budget["used"] != output_size:
            _fail("renderer_record.diagnostics.budget.used",
                  "byte budget usage must equal exact displayed output size")

    if require_hash:
        supplied = _digest(value["renderer_sha256"],
                           "renderer_record.renderer_sha256")
        body = dict(value)
        del body["renderer_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("renderer_record.renderer_sha256", "canonical hash mismatch")


def validate_renderer(record: object) -> None:
    _validate_json(record)
    _validate_renderer(record, require_hash=True)


def seal_renderer(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("renderer_sha256", None)
    _validate_json(body)
    _validate_renderer(body, require_hash=False)
    body["renderer_sha256"] = canonical_sha256(body)
    validate_renderer(body)
    return body


def _validate_annotation(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "annotation_id", "trial_id", "created_at",
        "history_requirement", "valid_outcomes", "constraints", "authorities",
        "slots", "labels", "provenance",
    }
    if require_hash:
        keys.add("annotation_sha256")
    value = _object(record, keys, "annotation")
    if value["schema"] != ANNOTATION_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("annotation", "unsupported schema or version")
    for key in ("annotation_id", "trial_id"):
        _string(value[key], f"annotation.{key}")
    _utc_timestamp(value["created_at"], "annotation.created_at")
    if value["history_requirement"] not in _HISTORY_REQUIREMENTS:
        _fail("annotation.history_requirement",
              f"must be one of {sorted(_HISTORY_REQUIREMENTS)}")
    valid_outcomes = _string_list(
        value["valid_outcomes"], "annotation.valid_outcomes", nonempty=True)
    if any(outcome not in _VALID_OUTCOMES for outcome in valid_outcomes):
        _fail("annotation.valid_outcomes",
              f"must contain only {sorted(_VALID_OUTCOMES)}")

    if not isinstance(value["constraints"], list):
        _fail("annotation.constraints", "must be an array")
    constraint_ids = []
    for index, item in enumerate(value["constraints"]):
        where = f"annotation.constraints[{index}]"
        constraint = _object(item, {"constraint_id", "kind", "text"}, where)
        constraint_id = _string(constraint["constraint_id"],
                                f"{where}.constraint_id")
        assert constraint_id is not None
        constraint_ids.append(constraint_id)
        if constraint["kind"] not in _CONSTRAINT_KINDS:
            _fail(f"{where}.kind", f"must be one of {sorted(_CONSTRAINT_KINDS)}")
        text_state, _, _ = _validate_capture(constraint["text"], f"{where}.text")
        if text_state != "captured":
            _fail(f"{where}.text", "constraint polarity requires exact captured text")
    if len(constraint_ids) != len(set(constraint_ids)):
        _fail("annotation.constraints", "constraint_id values must be unique")

    if not isinstance(value["authorities"], list):
        _fail("annotation.authorities", "must be an array")
    authority_ids = []
    authority_kinds: dict[str, str] = {}
    for index, item in enumerate(value["authorities"]):
        where = f"annotation.authorities[{index}]"
        authority = _object(item, {
            "authority_id", "kind", "source_ranges", "artifact",
        }, where)
        authority_id = _string(authority["authority_id"], f"{where}.authority_id")
        assert authority_id is not None
        authority_ids.append(authority_id)
        kind = authority["kind"]
        if kind not in _AUTHORITY_KINDS:
            _fail(f"{where}.kind", f"must be one of {sorted(_AUTHORITY_KINDS)}")
        if not isinstance(authority["source_ranges"], list):
            _fail(f"{where}.source_ranges", "must be an array")
        source_ranges = [
            _validate_source_range(source_range,
                                   f"{where}.source_ranges[{range_index}]")
            for range_index, source_range in enumerate(authority["source_ranges"])
        ]
        artifact_state, _, _ = _validate_capture(
            authority["artifact"], f"{where}.artifact")
        if kind == "historical_span":
            if not source_ranges or artifact_state != "not_applicable":
                _fail(where, "historical authority requires exact source ranges only")
        elif source_ranges or artifact_state != "captured":
            _fail(where, "current or visible authority requires a captured artifact only")
        authority_kinds[authority_id] = kind
    if len(authority_ids) != len(set(authority_ids)):
        _fail("annotation.authorities", "authority_id values must be unique")

    if not isinstance(value["slots"], list):
        _fail("annotation.slots", "must be an array")
    slot_ids = []
    dependencies: dict[str, list[str]] = {}
    required_slots = 0
    for index, item in enumerate(value["slots"]):
        where = f"annotation.slots[{index}]"
        slot = _object(item, {
            "slot_id", "claim", "required", "depends_on_all",
            "depends_on_any", "acceptable_authority_sets", "ambiguity",
            "confidence",
        }, where)
        slot_id = _string(slot["slot_id"], f"{where}.slot_id")
        assert slot_id is not None
        slot_ids.append(slot_id)
        claim_state, _, _ = _validate_capture(slot["claim"], f"{where}.claim")
        if claim_state != "captured":
            _fail(f"{where}.claim", "slot claims require exact captured text")
        required = _boolean(slot["required"], f"{where}.required")
        required_slots += int(required)
        depends_all = _string_list(slot["depends_on_all"], f"{where}.depends_on_all")
        depends_any = _string_list(slot["depends_on_any"], f"{where}.depends_on_any")
        if set(depends_all).intersection(depends_any):
            _fail(where, "the same dependency cannot be both all-of and any-of")
        dependencies[slot_id] = depends_all + depends_any
        if not isinstance(slot["acceptable_authority_sets"], list):
            _fail(f"{where}.acceptable_authority_sets", "must be an array")
        normalized_sets = []
        for set_index, authority_set in enumerate(slot["acceptable_authority_sets"]):
            ids = _string_list(
                authority_set,
                f"{where}.acceptable_authority_sets[{set_index}]",
                nonempty=True)
            if len(ids) != len(set(ids)):
                _fail(f"{where}.acceptable_authority_sets[{set_index}]",
                      "one authority cannot appear twice in an AND alternative")
            if any(authority_id not in authority_kinds for authority_id in ids):
                _fail(f"{where}.acceptable_authority_sets[{set_index}]",
                      "must resolve to declared authorities")
            normalized_sets.append(frozenset(ids))
        if len(normalized_sets) != len(set(normalized_sets)):
            _fail(f"{where}.acceptable_authority_sets",
                  "alternative authority sets must be unique")
        if required and not normalized_sets and not set(valid_outcomes).intersection(
                ("clarification", "abstention")):
            _fail(where, "required answer slots need authority or an explicit non-answer route")
        if slot["ambiguity"] not in ("clear", "ambiguous", "unresolved"):
            _fail(f"{where}.ambiguity", "must be clear, ambiguous, or unresolved")
        confidence = _number(slot["confidence"], f"{where}.confidence")
        if not 0 <= confidence <= 1:
            _fail(f"{where}.confidence", "must be between 0 and 1")
    if len(slot_ids) != len(set(slot_ids)):
        _fail("annotation.slots", "slot_id values must be unique")
    slot_set = set(slot_ids)
    for slot_id, dependency_ids in dependencies.items():
        if slot_id in dependency_ids or any(item not in slot_set for item in dependency_ids):
            _fail(f"annotation.slots.{slot_id}",
                  "dependencies must name other declared slots")
    if required_slots == 0 and value["history_requirement"] == "required":
        _fail("annotation.history_requirement",
              "recall cannot be required when no answer slot is required")

    if not isinstance(value["labels"], list):
        _fail("annotation.labels", "must be an array")
    label_ids = []
    for index, item in enumerate(value["labels"]):
        where = f"annotation.labels[{index}]"
        label = _object(item, {
            "label_id", "kind", "candidate_id", "slot_id", "state",
        }, where)
        label_id = _string(label["label_id"], f"{where}.label_id")
        assert label_id is not None
        label_ids.append(label_id)
        if label["kind"] not in _ANNOTATION_LABEL_KINDS:
            _fail(f"{where}.kind",
                  f"must be one of {sorted(_ANNOTATION_LABEL_KINDS)}")
        _string(label["candidate_id"], f"{where}.candidate_id")
        slot_id = _string(label["slot_id"], f"{where}.slot_id", nullable=True)
        if label["kind"] == "preview_warrant":
            if slot_id is not None or label["state"] not in _PREVIEW_WARRANT_STATES:
                _fail(where, "preview labels require no slot and a preview-warrant state")
        else:
            if slot_id not in slot_set or label["state"] not in _SOURCE_SUPPORT_STATES:
                _fail(where, "source-support labels require a declared slot and support state")
    if len(label_ids) != len(set(label_ids)):
        _fail("annotation.labels", "label_id values must be unique")

    provenance = _object(value["provenance"], {
        "annotator_id", "kind", "adjudication_state", "split", "blind_to",
        "revision_of", "sealed_at",
    }, "annotation.provenance")
    _string(provenance["annotator_id"], "annotation.provenance.annotator_id")
    if provenance["kind"] not in _TRUSTED_LABEL_KINDS:
        _fail("annotation.provenance.kind",
              f"must be one of {sorted(_TRUSTED_LABEL_KINDS)}")
    if provenance["adjudication_state"] not in _ANNOTATION_STATES:
        _fail("annotation.provenance.adjudication_state",
              f"must be one of {sorted(_ANNOTATION_STATES)}")
    _string(provenance["split"], "annotation.provenance.split")
    _string_list(provenance["blind_to"], "annotation.provenance.blind_to")
    _string(provenance["revision_of"], "annotation.provenance.revision_of",
            nullable=True)
    _utc_timestamp(provenance["sealed_at"], "annotation.provenance.sealed_at")

    if require_hash:
        supplied = _digest(value["annotation_sha256"],
                           "annotation.annotation_sha256")
        body = dict(value)
        del body["annotation_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("annotation.annotation_sha256", "canonical hash mismatch")


def validate_annotation(record: object) -> None:
    _validate_json(record)
    _validate_annotation(record, require_hash=True)


def seal_annotation(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("annotation_sha256", None)
    _validate_json(body)
    _validate_annotation(body, require_hash=False)
    body["annotation_sha256"] = canonical_sha256(body)
    validate_annotation(body)
    return body


def _validate_final_claim_assessment(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "claim_assessment_id", "trial_id",
        "annotation_id", "final_response", "claims", "slot_coverage",
        "provenance",
    }
    if require_hash:
        keys.add("claim_assessment_sha256")
    value = _object(record, keys, "claim_assessment")
    if value["schema"] != FINAL_CLAIM_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("claim_assessment", "unsupported schema or version")
    for key in ("claim_assessment_id", "trial_id", "annotation_id"):
        _string(value[key], f"claim_assessment.{key}")
    _, response_size = _validate_content_artifact(
        value["final_response"], "claim_assessment.final_response")
    if not isinstance(value["claims"], list):
        _fail("claim_assessment.claims", "must be an array")
    claim_ids = []
    for index, item in enumerate(value["claims"]):
        where = f"claim_assessment.claims[{index}]"
        claim = _object(item, {
            "claim_id", "response_start", "response_end", "slot_id", "outcome",
            "claim_authority", "evidence",
        }, where)
        claim_id = _string(claim["claim_id"], f"{where}.claim_id")
        assert claim_id is not None
        claim_ids.append(claim_id)
        start = _integer(claim["response_start"], f"{where}.response_start")
        end = _integer(claim["response_end"], f"{where}.response_end")
        assert start is not None and end is not None
        if end <= start or end > response_size:
            _fail(where, "claim byte range must be non-empty and inside the final response")
        _string(claim["slot_id"], f"{where}.slot_id", nullable=True)
        if claim["outcome"] not in _FINAL_CLAIM_OUTCOMES:
            _fail(f"{where}.outcome",
                  f"must be one of {sorted(_FINAL_CLAIM_OUTCOMES)}")
        if claim["claim_authority"] not in _CLAIM_AUTHORITY_STATES:
            _fail(f"{where}.claim_authority",
                  f"must be one of {sorted(_CLAIM_AUTHORITY_STATES)}")
        if not isinstance(claim["evidence"], list):
            _fail(f"{where}.evidence", "must be an array")
        evidence_kinds = []
        for evidence_index, evidence_value in enumerate(claim["evidence"]):
            evidence_where = f"{where}.evidence[{evidence_index}]"
            evidence = _object(evidence_value, {
                "kind", "authority_id", "artifact_id",
                "retrieval_invocation_id", "candidate_id", "source_ranges",
            }, evidence_where)
            kind = evidence["kind"]
            if kind not in ("visible_context", "opened_history_span",
                            "current_authority"):
                _fail(f"{evidence_where}.kind",
                      "must be visible_context, opened_history_span, or current_authority")
            evidence_kinds.append(kind)
            _string(evidence["authority_id"], f"{evidence_where}.authority_id")
            _string(evidence["artifact_id"], f"{evidence_where}.artifact_id")
            retrieval_id = _string(
                evidence["retrieval_invocation_id"],
                f"{evidence_where}.retrieval_invocation_id", nullable=True)
            candidate_id = _string(
                evidence["candidate_id"], f"{evidence_where}.candidate_id",
                nullable=True)
            if not isinstance(evidence["source_ranges"], list):
                _fail(f"{evidence_where}.source_ranges", "must be an array")
            source_ranges = [
                _validate_source_range(source_range,
                                       f"{evidence_where}.source_ranges[{range_index}]")
                for range_index, source_range in enumerate(evidence["source_ranges"])
            ]
            if kind == "opened_history_span":
                if retrieval_id is None or candidate_id is None or not source_ranges:
                    _fail(evidence_where,
                          "opened history evidence requires around, candidate, and exact ranges")
            elif retrieval_id is not None or candidate_id is not None or source_ranges:
                _fail(evidence_where,
                      "visible/current artifact evidence cannot claim historical source ranges")
        outcome = claim["outcome"]
        authority = claim["claim_authority"]
        if outcome in ("supported", "partial", "contradicted"):
            if authority not in ("visible_context", "opened_history_span",
                                 "current_authority", "mixed_authority"):
                _fail(where, "evidence-bearing claims require an acceptable authority")
            if not evidence_kinds:
                _fail(where, "evidence-bearing claims require source-bound evidence")
            if authority == "mixed_authority" and len(set(evidence_kinds)) < 2:
                _fail(where, "mixed-authority claims require at least two authority kinds")
            if authority != "mixed_authority" and any(
                    kind != authority for kind in evidence_kinds):
                _fail(where, "claim evidence must match its declared authority")
        if outcome == "unresolved" and (
                authority != "unsupported" or evidence_kinds
                or claim["slot_id"] is None):
            _fail(where,
                  "unresolved claims require a requested slot, unsupported authority, and no evidence")
        if outcome == "clarification" and (authority != "clarification" or evidence_kinds):
            _fail(where, "clarification claims cannot pretend to use evidence")
        if outcome == "unsupported" and (authority != "unsupported" or evidence_kinds):
            _fail(where, "unsupported claims must remain explicitly unsupported")
    if len(claim_ids) != len(set(claim_ids)):
        _fail("claim_assessment.claims", "claim_id values must be unique")

    if not isinstance(value["slot_coverage"], list):
        _fail("claim_assessment.slot_coverage", "must be an array")
    covered_slots = []
    claim_id_set = set(claim_ids)
    claims_by_id = {claim["claim_id"]: claim for claim in value["claims"]}
    for index, item in enumerate(value["slot_coverage"]):
        where = f"claim_assessment.slot_coverage[{index}]"
        coverage = _object(item, {"slot_id", "state", "claim_ids"}, where)
        slot_id = _string(coverage["slot_id"], f"{where}.slot_id")
        assert slot_id is not None
        covered_slots.append(slot_id)
        if coverage["state"] not in _SLOT_COVERAGE_STATES:
            _fail(f"{where}.state",
                  f"must be one of {sorted(_SLOT_COVERAGE_STATES)}")
        ids = _string_list(coverage["claim_ids"], f"{where}.claim_ids")
        if any(claim_id not in claim_id_set for claim_id in ids):
            _fail(f"{where}.claim_ids", "must resolve to assessed response claims")
        if any(claims_by_id[claim_id]["slot_id"] != slot_id for claim_id in ids):
            _fail(f"{where}.claim_ids", "must name claims for this exact slot")
        if coverage["state"] == "claimed" and not ids:
            _fail(where, "claimed slots require at least one assessed response claim")
        if coverage["state"] == "omitted" and ids:
            _fail(where, "omitted slots cannot hide assessed response claims")
        if coverage["state"] == "clarified" and (
                not ids or any(claims_by_id[claim_id]["outcome"] != "clarification"
                               for claim_id in ids)):
            _fail(where, "clarified slots require clarification claims only")
    if len(covered_slots) != len(set(covered_slots)):
        _fail("claim_assessment.slot_coverage", "slot_id values must be unique")
    claimed_ids = {claim_id for coverage in value["slot_coverage"]
                   for claim_id in coverage["claim_ids"]}
    slotted_claim_ids = {claim["claim_id"] for claim in value["claims"]
                         if claim["slot_id"] is not None}
    if claimed_ids != slotted_claim_ids:
        _fail("claim_assessment.slot_coverage",
              "must account for every slotted response claim exactly once")

    provenance = _object(value["provenance"], {
        "annotator_id", "kind", "adjudication_state", "blind_to", "sealed_at",
    }, "claim_assessment.provenance")
    _string(provenance["annotator_id"],
            "claim_assessment.provenance.annotator_id")
    if provenance["kind"] not in _TRUSTED_LABEL_KINDS:
        _fail("claim_assessment.provenance.kind",
              f"must be one of {sorted(_TRUSTED_LABEL_KINDS)}")
    if provenance["adjudication_state"] not in _ANNOTATION_STATES:
        _fail("claim_assessment.provenance.adjudication_state",
              f"must be one of {sorted(_ANNOTATION_STATES)}")
    _string_list(provenance["blind_to"], "claim_assessment.provenance.blind_to")
    _utc_timestamp(provenance["sealed_at"], "claim_assessment.provenance.sealed_at")

    if require_hash:
        supplied = _digest(value["claim_assessment_sha256"],
                           "claim_assessment.claim_assessment_sha256")
        body = dict(value)
        del body["claim_assessment_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("claim_assessment.claim_assessment_sha256", "canonical hash mismatch")


def validate_final_claim_assessment(record: object) -> None:
    _validate_json(record)
    _validate_final_claim_assessment(record, require_hash=True)


def seal_final_claim_assessment(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("claim_assessment_sha256", None)
    _validate_json(body)
    _validate_final_claim_assessment(body, require_hash=False)
    body["claim_assessment_sha256"] = canonical_sha256(body)
    validate_final_claim_assessment(body)
    return body


def _validate_candidate_trace(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "trace_id", "trial_id",
        "retrieval_invocation_id", "query_variant_id", "generation_id",
        "annotation_id", "renderer_record_id", "lane_diagnostics",
        "candidates", "stages", "gold_slots", "result",
    }
    if require_hash:
        keys.add("trace_sha256")
    value = _object(record, keys, "candidate_trace")
    if (value["schema"] != CANDIDATE_TRACE_SCHEMA
            or value["version"] != CANDIDATE_TRACE_VERSION):
        _fail("candidate_trace", "unsupported schema or version")
    for key in ("trace_id", "trial_id", "retrieval_invocation_id",
                "query_variant_id", "generation_id", "annotation_id",
                "renderer_record_id"):
        _string(value[key], f"candidate_trace.{key}")

    if not isinstance(value["lane_diagnostics"], list) or not value["lane_diagnostics"]:
        _fail("candidate_trace.lane_diagnostics", "must be a non-empty array")
    lane_diagnostics: dict[str, Mapping[str, object]] = {}
    lane_score_kinds: dict[str, str] = {}
    for index, item in enumerate(value["lane_diagnostics"]):
        where = f"candidate_trace.lane_diagnostics[{index}]"
        lane = _object(item, {
            "lane", "kind", "role", "score_kind", "diagnostics",
            "pool_artifact_id",
        }, where)
        name = _string(lane["lane"], f"{where}.lane")
        score_kind = _string(lane["score_kind"], f"{where}.score_kind")
        assert name is not None and score_kind is not None
        if lane["kind"] not in _LANE_KINDS:
            _fail(f"{where}.kind", f"must be one of {sorted(_LANE_KINDS)}")
        if lane["role"] not in _LANE_ROLES:
            _fail(f"{where}.role", f"must be one of {sorted(_LANE_ROLES)}")
        if name in lane_diagnostics:
            _fail("candidate_trace.lane_diagnostics", "lane names must be unique")
        _validate_diagnostics(lane["diagnostics"], f"{where}.diagnostics")
        _string(lane["pool_artifact_id"], f"{where}.pool_artifact_id")
        lane_diagnostics[name] = lane["diagnostics"]
        lane_score_kinds[name] = score_kind

    if not isinstance(value["candidates"], list):
        _fail("candidate_trace.candidates", "must be an array")
    candidates: dict[str, Mapping[str, object]] = {}
    candidate_ranges: dict[str, list[tuple[str, str, int, int]]] = {}
    candidate_slots: dict[str, dict[str, Mapping[str, object]]] = {}
    candidate_opened: dict[str, bool] = {}
    candidate_selected_ranges: dict[str, list[tuple[str, str, int, int]]] = {}
    lane_ranks: dict[str, list[int]] = {lane: [] for lane in lane_diagnostics}
    exact_identities: dict[str, str] = {}
    raw_sequences = []
    candidate_keys = {
        "candidate_id", "evidence_id", "view_id", "exact_identity_id",
        "exact_content_sha256", "session_id", "family_id", "source_bindings",
        "unresolved_reason", "lane_scores", "raw_sequence", "preview_warrant",
        "displayed_handle", "slot_support", "selection",
    }
    for index, item in enumerate(value["candidates"]):
        where = f"candidate_trace.candidates[{index}]"
        candidate = _object(item, candidate_keys, where)
        candidate_id = _string(candidate["candidate_id"], f"{where}.candidate_id")
        assert candidate_id is not None
        if candidate_id in candidates:
            _fail("candidate_trace.candidates", "candidate_id values must be unique")
        identities = {}
        for key in ("evidence_id", "view_id", "exact_identity_id", "session_id",
                    "family_id"):
            identities[key] = _string(candidate[key], f"{where}.{key}", nullable=True)
        content_sha = _digest(candidate["exact_content_sha256"],
                              f"{where}.exact_content_sha256")
        exact_id = identities["exact_identity_id"]
        if exact_id is not None:
            prior_sha = exact_identities.setdefault(exact_id, content_sha)
            if prior_sha != content_sha:
                _fail(f"{where}.exact_content_sha256",
                      "one exact identity cannot name different content hashes")

        if not isinstance(candidate["source_bindings"], list):
            _fail(f"{where}.source_bindings", "must be an array")
        bindings = [
            _validate_source_binding(binding,
                                     f"{where}.source_bindings[{binding_index}]")
            for binding_index, binding in enumerate(candidate["source_bindings"])
        ]
        binding_ids = [source_range[0] for source_range in bindings]
        if len(binding_ids) != len(set(binding_ids)):
            _fail(f"{where}.source_bindings", "source_span_id values must be unique")
        unresolved_reason = _string(
            candidate["unresolved_reason"], f"{where}.unresolved_reason",
            nullable=True)
        fully_materialized = (
            bool(bindings)
            and all(identity is not None for identity in identities.values())
            and all(binding["materialization_state"]
                    == "deterministically_materializable"
                    for binding in candidate["source_bindings"])
        )
        if fully_materialized and unresolved_reason is not None:
            _fail(where, "fully materialized candidates require unresolved_reason=null")
        if not fully_materialized and unresolved_reason is None:
            _fail(where, "unresolved identities or source bindings require a reason")

        if not isinstance(candidate["lane_scores"], list) or not candidate["lane_scores"]:
            _fail(f"{where}.lane_scores", "must record at least one lane")
        seen_lanes = set()
        for score_index, score_value in enumerate(candidate["lane_scores"]):
            score_where = f"{where}.lane_scores[{score_index}]"
            score = _object(score_value, {"lane", "rank", "score", "score_kind"},
                            score_where)
            lane_name = _string(score["lane"], f"{score_where}.lane")
            assert lane_name is not None
            if lane_name not in lane_diagnostics:
                _fail(f"{score_where}.lane", "must resolve to declared lane diagnostics")
            if lane_name in seen_lanes:
                _fail(f"{where}.lane_scores", "lane names must be unique per candidate")
            seen_lanes.add(lane_name)
            rank = _integer(score["rank"], f"{score_where}.rank")
            if rank == 0:
                _fail(f"{score_where}.rank", "ranks are one-based")
            if score["score_kind"] != lane_score_kinds[lane_name]:
                _fail(f"{score_where}.score_kind",
                      "must match the declared lane score semantics")
            _number(score["score"], f"{score_where}.score")
            lane_ranks[lane_name].append(rank)

        raw_sequence = _integer(candidate["raw_sequence"], f"{where}.raw_sequence")
        if raw_sequence == 0:
            _fail(f"{where}.raw_sequence", "must be one-based")
        assert raw_sequence is not None
        raw_sequences.append(raw_sequence)

        preview = _object(candidate["preview_warrant"], {"state", "label_id"},
                          f"{where}.preview_warrant")
        if preview["state"] not in _PREVIEW_WARRANT_STATES:
            _fail(f"{where}.preview_warrant.state",
                  f"must be one of {sorted(_PREVIEW_WARRANT_STATES)}")
        _string(preview["label_id"], f"{where}.preview_warrant.label_id")
        _string(candidate["displayed_handle"], f"{where}.displayed_handle",
                nullable=True)

        if not isinstance(candidate["slot_support"], list):
            _fail(f"{where}.slot_support", "must be an array")
        slots: dict[str, Mapping[str, object]] = {}
        for support_index, support_value in enumerate(candidate["slot_support"]):
            support_where = f"{where}.slot_support[{support_index}]"
            support = _object(support_value, {
                "slot_id", "state", "supporting_source_ranges", "label_id",
            }, support_where)
            slot_id = _string(support["slot_id"], f"{support_where}.slot_id")
            assert slot_id is not None
            if slot_id in slots:
                _fail(f"{where}.slot_support", "slot_id values must be unique")
            if support["state"] not in _SOURCE_SUPPORT_STATES:
                _fail(f"{support_where}.state",
                      f"must be one of {sorted(_SOURCE_SUPPORT_STATES)}")
            if not isinstance(support["supporting_source_ranges"], list):
                _fail(f"{support_where}.supporting_source_ranges", "must be an array")
            supporting_ranges = [
                _validate_source_range(
                    source_range,
                    f"{support_where}.supporting_source_ranges[{range_index}]")
                for range_index, source_range in
                enumerate(support["supporting_source_ranges"])
            ]
            if support["state"] in ("direct", "partial", "contradictory"):
                if not supporting_ranges:
                    _fail(support_where,
                          "direct, partial, or contradictory judgments require exact ranges")
                if any(not any(_range_within(source_range, binding)
                               for binding in bindings)
                       for source_range in supporting_ranges):
                    _fail(support_where,
                          "supporting ranges must be carried by this candidate")
            elif supporting_ranges:
                _fail(support_where, "non-support labels cannot carry supporting ranges")
            _string(support["label_id"], f"{support_where}.label_id")
            slots[slot_id] = support

        selection = _object(candidate["selection"], {
            "opened", "selected_alias_atom_id", "selected_source_ranges",
            "around_invocation_id",
        }, f"{where}.selection")
        opened = _boolean(selection["opened"], f"{where}.selection.opened")
        selected_atom = _string(
            selection["selected_alias_atom_id"],
            f"{where}.selection.selected_alias_atom_id", nullable=True)
        around_id = _string(
            selection["around_invocation_id"],
            f"{where}.selection.around_invocation_id", nullable=True)
        if not isinstance(selection["selected_source_ranges"], list):
            _fail(f"{where}.selection.selected_source_ranges", "must be an array")
        selected_ranges = [
            _validate_source_range(source_range,
                                   f"{where}.selection.selected_source_ranges[{range_index}]")
            for range_index, source_range in
            enumerate(selection["selected_source_ranges"])
        ]
        if opened:
            if selected_atom is None or around_id is None or not selected_ranges:
                _fail(f"{where}.selection",
                      "opened candidates require alias, exact ranges, and around invocation")
            if selected_atom not in {binding[1] for binding in bindings}:
                _fail(f"{where}.selection.selected_alias_atom_id",
                      "must resolve to an atom alias carried by this candidate")
            if any(not any(_range_within(source_range, binding)
                           for binding in bindings)
                   for source_range in selected_ranges):
                _fail(f"{where}.selection.selected_source_ranges",
                      "opened ranges must be carried by the selected candidate")
        elif selected_atom is not None or around_id is not None or selected_ranges:
            _fail(f"{where}.selection", "unopened candidates cannot claim selected evidence")

        candidates[candidate_id] = candidate
        candidate_ranges[candidate_id] = bindings
        candidate_slots[candidate_id] = slots
        candidate_opened[candidate_id] = opened
        candidate_selected_ranges[candidate_id] = selected_ranges
    if sorted(raw_sequences) != list(range(1, len(candidates) + 1)):
        _fail("candidate_trace.candidates",
              "raw_sequence values must be contiguous and one-based")
    for lane_name, ranks in lane_ranks.items():
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            _fail(f"candidate_trace.lane_diagnostics.{lane_name}",
                  "recorded lane ranks must be contiguous from one")
        observed = lane_diagnostics[lane_name]["truncation"]["observed_count"]
        if observed != len(ranks):
            _fail(f"candidate_trace.lane_diagnostics.{lane_name}",
                  "observed_count must equal recorded lane candidates")

    raw_order = [candidate_id for candidate_id, candidate in sorted(
        candidates.items(), key=lambda item: item[1]["raw_sequence"])]
    if not isinstance(value["stages"], list):
        _fail("candidate_trace.stages", "must be an array")
    stage_names = [item.get("stage") if isinstance(item, Mapping) else None
                   for item in value["stages"]]
    if stage_names != list(STAGES):
        _fail("candidate_trace.stages",
              f"must appear exactly in order {list(STAGES)}")
    active = raw_order
    active_ranges = {candidate_id: list(candidate_ranges[candidate_id])
                     for candidate_id in active}
    outputs: dict[str, list[str]] = {}
    output_ranges: dict[str, list[tuple[str, str, int, int]]] = {}
    identity_for_stage = {
        "view": "view_id", "exact_identity": "exact_identity_id",
        "session": "session_id", "family": "family_id",
    }
    for stage_index, stage_value in enumerate(value["stages"]):
        stage_name = STAGES[stage_index]
        stage = _object(stage_value, {"stage", "decisions"},
                        f"candidate_trace.stages[{stage_index}]")
        if not isinstance(stage["decisions"], list):
            _fail(f"candidate_trace.stages[{stage_index}].decisions",
                  "must be an array")
        decision_ids = [item.get("candidate_id") if isinstance(item, Mapping) else None
                        for item in stage["decisions"]]
        if decision_ids != active:
            _fail(f"candidate_trace.stages[{stage_index}].decisions",
                  "must account once, in order, for every active candidate")
        kept = []
        next_ranges: dict[str, list[tuple[str, str, int, int]]] = {}
        parsed = []
        for decision_index, decision_value in enumerate(stage["decisions"]):
            where = f"candidate_trace.stages[{stage_index}].decisions[{decision_index}]"
            decision = _object(decision_value, {
                "candidate_id", "decision", "reason_code", "reason_detail",
                "retained_as", "visible_source_ranges",
            }, where)
            candidate_id = decision["candidate_id"]
            if decision["decision"] not in ("kept", "dropped"):
                _fail(f"{where}.decision", "must be kept or dropped")
            _string(decision["reason_code"], f"{where}.reason_code")
            _string(decision["reason_detail"], f"{where}.reason_detail",
                    nullable=True)
            retained_as = _string(decision["retained_as"], f"{where}.retained_as",
                                  nullable=True)
            if not isinstance(decision["visible_source_ranges"], list):
                _fail(f"{where}.visible_source_ranges", "must be an array")
            visible_ranges = [
                _validate_source_range(source_range,
                                       f"{where}.visible_source_ranges[{range_index}]")
                for range_index, source_range in
                enumerate(decision["visible_source_ranges"])
            ]
            if any(_ranges_overlap(first, second)
                   for range_index, first in enumerate(visible_ranges)
                   for second in visible_ranges[range_index + 1:]):
                _fail(f"{where}.visible_source_ranges", "visible ranges cannot overlap")
            if decision["decision"] == "kept":
                if retained_as != candidate_id:
                    _fail(where, "kept candidates must retain themselves")
                if stage_name != "render" and visible_ranges != active_ranges[candidate_id]:
                    _fail(where, "only the render stage may trim source ranges")
                if stage_name == "render" and any(
                        not any(_range_within(source_range, parent)
                                for parent in active_ranges[candidate_id])
                        for source_range in visible_ranges):
                    _fail(where, "rendered ranges must be subsets of candidate source ranges")
                kept.append(candidate_id)
                next_ranges[candidate_id] = visible_ranges
            elif visible_ranges:
                _fail(where, "dropped candidates cannot expose visible source ranges")
            parsed.append((candidate_id, decision["decision"], retained_as, where))
        kept_set = set(kept)
        for candidate_id, decision, retained_as, where in parsed:
            if stage_name == "raw" and decision != "kept":
                _fail(where, "raw stage is strict pass-through")
            if (stage_name in identity_for_stage
                    and candidates[candidate_id][identity_for_stage[stage_name]] is None
                    and decision != "kept"):
                _fail(where, "unresolved candidates must survive collapse stages")
            if decision != "dropped":
                continue
            identity_key = identity_for_stage.get(stage_name)
            if identity_key is not None:
                if retained_as not in kept_set:
                    _fail(where, "collapse drops require a kept representative")
                if candidates[candidate_id][identity_key] != candidates[retained_as][identity_key]:
                    _fail(where, f"retained representative must share {identity_key}")
                if stage_name == "exact_identity" and (
                        candidates[candidate_id]["exact_content_sha256"]
                        != candidates[retained_as]["exact_content_sha256"]):
                    _fail(where, "exact-collapse representatives must share content hash")
            elif stage_name == "render":
                if retained_as is not None:
                    _fail(where, "render drops are omissions, not identity merges")
            else:
                _fail(where, "raw stage cannot drop candidates")
        active = kept
        active_ranges = next_ranges
        outputs[stage_name] = list(active)
        output_ranges[stage_name] = [source_range for candidate_id in active
                                     for source_range in active_ranges[candidate_id]]

    displayed_handles = []
    for candidate_id, candidate in candidates.items():
        handle = candidate["displayed_handle"]
        if candidate_id in set(outputs["render"]):
            if handle is None:
                _fail(f"candidate_trace.candidates.{candidate_id}.displayed_handle",
                      "rendered candidates require the exact displayed handle")
            displayed_handles.append(handle)
        elif handle is not None:
            _fail(f"candidate_trace.candidates.{candidate_id}.displayed_handle",
                  "non-rendered candidates cannot claim a displayed handle")
    if len(displayed_handles) != len(set(displayed_handles)):
        _fail("candidate_trace.candidates", "displayed handles must be unique")

    if not isinstance(value["gold_slots"], list) or not value["gold_slots"]:
        _fail("candidate_trace.gold_slots", "must be a non-empty array")
    gold_slot_ids = []
    required_slot_ids = []
    gold_by_slot: dict[str, Mapping[str, object]] = {}
    for index, item in enumerate(value["gold_slots"]):
        where = f"candidate_trace.gold_slots[{index}]"
        gold = _object(item, {
            "slot_id", "required", "state", "selected_authority_ids",
            "source_ranges", "availability", "first_loss_stage",
        }, where)
        slot_id = _string(gold["slot_id"], f"{where}.slot_id")
        assert slot_id is not None
        gold_slot_ids.append(slot_id)
        if _boolean(gold["required"], f"{where}.required"):
            required_slot_ids.append(slot_id)
        if gold["state"] not in _GOLD_STATES:
            _fail(f"{where}.state", f"must be one of {sorted(_GOLD_STATES)}")
        selected_authority_ids = _string_list(
            gold["selected_authority_ids"], f"{where}.selected_authority_ids")
        if not isinstance(gold["source_ranges"], list):
            _fail(f"{where}.source_ranges", "must be an array")
        gold_ranges = [
            _validate_source_range(source_range,
                                   f"{where}.source_ranges[{range_index}]")
            for range_index, source_range in enumerate(gold["source_ranges"])
        ]
        availability = _object(gold["availability"], set(CHECKPOINTS),
                               f"{where}.availability")
        for checkpoint in CHECKPOINTS:
            if availability[checkpoint] not in _AVAILABILITY_STATES:
                _fail(f"{where}.availability.{checkpoint}",
                      f"must be one of {sorted(_AVAILABILITY_STATES)}")
        first_loss = _string(gold["first_loss_stage"], f"{where}.first_loss_stage",
                             nullable=True)
        if first_loss is not None and first_loss not in STAGES:
            _fail(f"{where}.first_loss_stage", "must name a trace stage or null")
        if gold["state"] in ("source_bound", "mixed_authority_required"):
            if (not selected_authority_ids or not gold_ranges
                    or availability["frozen_source"] != "present"):
                _fail(where, "source-bound slots require selected authority and frozen exact ranges")
            stage_presence = {}
            for stage_name in STAGES:
                present = all(any(_range_within(gold_range, visible_range)
                                  for visible_range in output_ranges[stage_name])
                              for gold_range in gold_ranges)
                stage_presence[stage_name] = present
                expected_state = "present" if present else "not_present_in_observed_set"
                if availability[stage_name] != expected_state:
                    _fail(f"{where}.availability.{stage_name}",
                          "must be derived from exact visible ranges at this stage")
            if stage_presence["raw"] and availability["index"] != "present":
                _fail(f"{where}.availability.index",
                      "raw-present gold must be index-present")
            derived_loss = None
            if availability["index"] == "present":
                derived_loss = next((stage for stage in STAGES
                                     if not stage_presence[stage]), None)
            if first_loss != derived_loss:
                _fail(f"{where}.first_loss_stage",
                      f"must equal derived first loss stage {derived_loss!r}")
            for candidate_id, slots in candidate_slots.items():
                support = slots.get(slot_id)
                if support is None:
                    _fail(f"candidate_trace.candidates.{candidate_id}.slot_support",
                          f"missing independently judged slot {slot_id}")
                if support["state"] in ("direct", "partial"):
                    for supporting_range in support["supporting_source_ranges"]:
                        parsed_range = _validate_source_range(
                            supporting_range,
                            f"candidate_trace.candidates.{candidate_id}.slot_support.{slot_id}")
                        if not any(_range_within(parsed_range, gold_range)
                                   for gold_range in gold_ranges):
                            _fail(f"candidate_trace.candidates.{candidate_id}.slot_support.{slot_id}",
                                  "supporting ranges must be independently source-bound gold")
        elif gold["state"] == "current_authority_required":
            if not selected_authority_ids or gold_ranges or first_loss is not None or any(
                    availability[checkpoint] != "not_applicable"
                    for checkpoint in CHECKPOINTS):
                _fail(where, "current-authority slots cannot bind historical ranges")
        else:
            if selected_authority_ids or gold_ranges or first_loss is not None or any(
                    availability[checkpoint] != "gold_not_judged"
                    for checkpoint in CHECKPOINTS):
                _fail(where, "unjudged slots cannot claim ranges or availability")
        gold_by_slot[slot_id] = gold
    if len(gold_slot_ids) != len(set(gold_slot_ids)):
        _fail("candidate_trace.gold_slots", "slot_id values must be unique")

    result = _object(value["result"], {
        "status", "candidate_ids", "slot_results", "detail",
    }, "candidate_trace.result")
    if result["status"] not in _RESULT_STATES:
        _fail("candidate_trace.result.status",
              f"must be one of {sorted(_RESULT_STATES)}")
    result_ids = _string_list(result["candidate_ids"],
                              "candidate_trace.result.candidate_ids")
    render_survivors = set(outputs["render"])
    if any(candidate_id not in render_survivors for candidate_id in result_ids):
        _fail("candidate_trace.result.candidate_ids",
              "must refer to render-stage survivors")
    if any(not candidate_opened[candidate_id] for candidate_id in result_ids):
        _fail("candidate_trace.result.candidate_ids",
              "supporting evidence must have been opened, not merely previewed")
    if not isinstance(result["slot_results"], list):
        _fail("candidate_trace.result.slot_results", "must be an array")
    slot_result_ids = []
    aggregate_ids = []
    required_states = {}
    slot_result_records: dict[str, Mapping[str, object]] = {}
    for index, item in enumerate(result["slot_results"]):
        where = f"candidate_trace.result.slot_results[{index}]"
        slot_result = _object(item, {
            "slot_id", "status", "candidate_ids", "claim_authority",
        }, where)
        slot_id = _string(slot_result["slot_id"], f"{where}.slot_id")
        assert slot_id is not None
        slot_result_ids.append(slot_id)
        if slot_id not in gold_by_slot:
            _fail(f"{where}.slot_id", "must resolve to a declared gold slot")
        if slot_result["status"] not in _SLOT_RESULT_STATES:
            _fail(f"{where}.status", f"must be one of {sorted(_SLOT_RESULT_STATES)}")
        if slot_result["claim_authority"] not in _CLAIM_AUTHORITY_STATES:
            _fail(f"{where}.claim_authority",
                  f"must be one of {sorted(_CLAIM_AUTHORITY_STATES)}")
        ids = _string_list(slot_result["candidate_ids"], f"{where}.candidate_ids")
        if any(candidate_id not in result_ids for candidate_id in ids):
            _fail(f"{where}.candidate_ids", "must be included in aggregate evidence ids")
        if slot_result["claim_authority"] == "opened_history_span":
            if not ids or any(not candidate_opened[candidate_id] for candidate_id in ids):
                _fail(where, "opened-history authority requires exact opened evidence")
            allowed_support_states = {
                "direct": {"direct"},
                "partial": {"direct", "partial"},
                "contradictory": {"contradictory"},
            }.get(slot_result["status"])
            if allowed_support_states is None:
                _fail(where,
                      "opened-history authority requires direct, partial, or contradictory judgment")
            combined_support_ranges = []
            for candidate_id in ids:
                support = candidate_slots[candidate_id].get(slot_id)
                if support is None or support["state"] not in allowed_support_states:
                    _fail(where, "slot result exceeds independent source-support labels")
                for supporting_range in support["supporting_source_ranges"]:
                    parsed_range = _validate_source_range(
                        supporting_range,
                        f"candidate_trace.candidates.{candidate_id}.slot_support.{slot_id}")
                    if not any(_range_within(parsed_range, opened_range)
                               for opened_range in candidate_selected_ranges[candidate_id]):
                        _fail(where,
                              "slot result exceeds the exact source ranges opened by around")
                    combined_support_ranges.append(parsed_range)
            if slot_result["status"] == "direct":
                gold_ranges = [
                    _validate_source_range(source_range, f"{where}.gold")
                    for source_range in gold_by_slot[slot_id]["source_ranges"]
                ]
                if any(not any(_range_within(gold_range, support_range)
                               for support_range in combined_support_ranges)
                       for gold_range in gold_ranges):
                    _fail(where,
                          "direct support must cover every selected gold authority range")
        elif ids:
            _fail(where, "non-history authority cannot cite history candidates")
        if (slot_result["status"] in ("direct", "partial")
                and slot_result["claim_authority"] == "unsupported"):
            _fail(where, "supported slots cannot use unsupported authority")
        aggregate_ids.extend(ids)
        required_states[slot_id] = slot_result["status"]
        slot_result_records[slot_id] = slot_result
    if slot_result_ids != gold_slot_ids:
        _fail("candidate_trace.result.slot_results",
              "must account once, in gold-slot order, for every slot")
    if set(aggregate_ids) != set(result_ids):
        _fail("candidate_trace.result.candidate_ids",
              "must equal the union of per-slot evidence candidates")
    required_result_states = [required_states[slot_id]
                              for slot_id in required_slot_ids]
    if result["status"] == "direct_historical_support":
        if (not required_result_states or not result_ids
                or any(state != "direct" for state in required_result_states)
                or any(gold_by_slot[slot_id]["state"] != "source_bound"
                       or slot_result_records[slot_id]["claim_authority"]
                       != "opened_history_span"
                       or not slot_result_records[slot_id]["candidate_ids"]
                       for slot_id in required_slot_ids)):
            _fail("candidate_trace.result",
                  "direct historical result requires opened evidence for every required historical slot")
    elif result["status"] == "partial_historical_support":
        historical_slot_ids = [
            slot_id for slot_id in required_slot_ids
            if gold_by_slot[slot_id]["state"] in (
                "source_bound", "mixed_authority_required")
        ]
        supported_historical_ids = [
            slot_id for slot_id in historical_slot_ids
            if (slot_result_records[slot_id]["status"] in ("direct", "partial")
                and slot_result_records[slot_id]["claim_authority"]
                == "opened_history_span"
                and slot_result_records[slot_id]["candidate_ids"])
        ]
        fully_direct_historical = (
            historical_slot_ids == required_slot_ids
            and all(gold_by_slot[slot_id]["state"] == "source_bound"
                    and slot_result_records[slot_id]["status"] == "direct"
                    for slot_id in historical_slot_ids)
        )
        if (not supported_historical_ids or fully_direct_historical
                or any(slot_result_records[slot_id]["status"] in (
                    "direct", "partial")
                       and slot_id not in historical_slot_ids
                       for slot_id in required_slot_ids)):
            _fail("candidate_trace.result",
                  "partial historical result requires some exact historical support without claiming full task support")
    elif result["status"] == "contradictory_historical_evidence":
        contradictory_ids = [
            slot_id for slot_id in required_slot_ids
            if (slot_result_records[slot_id]["status"] == "contradictory"
                and slot_result_records[slot_id]["claim_authority"]
                == "opened_history_span"
                and slot_result_records[slot_id]["candidate_ids"])
        ]
        if not contradictory_ids or any(
                required_states[slot_id] in ("direct", "partial")
                for slot_id in required_slot_ids):
            _fail("candidate_trace.result",
                  "contradictory historical result requires exact opened contradiction without supported-slot inflation")
    elif result["status"] == "no_supported_evidence_found":
        if any(state in ("direct", "partial") for state in required_result_states):
            _fail("candidate_trace.result",
                  "no-evidence result contradicts a supported required slot")
    elif result["status"] == "not_evaluated":
        if any(state != "not_evaluated" for state in required_states.values()):
            _fail("candidate_trace.result", "not_evaluated requires unjudged slot results")
    elif result["status"] == "current_authority_required":
        if not any(gold_by_slot[slot_id]["state"] in (
                "current_authority_required", "mixed_authority_required")
               for slot_id in required_slot_ids):
            _fail("candidate_trace.result",
                  "current-authority result requires a current-authority slot")
    detail = _string(result["detail"], "candidate_trace.result.detail", nullable=True)
    if result["status"] in ("no_supported_evidence_found",
                            "current_authority_required") and detail is None:
        _fail("candidate_trace.result", "bounded or routed results require detail")

    if require_hash:
        supplied = _digest(value["trace_sha256"],
                           "candidate_trace.trace_sha256")
        body = dict(value)
        del body["trace_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("candidate_trace.trace_sha256", "canonical hash mismatch")


def validate_candidate_trace(record: object) -> None:
    _validate_json(record)
    _validate_candidate_trace(record, require_hash=True)


def seal_candidate_trace(record: Mapping[str, object]) -> dict:
    body = copy.deepcopy(dict(record))
    body.pop("trace_sha256", None)
    _validate_json(body)
    _validate_candidate_trace(body, require_hash=False)
    body["trace_sha256"] = canonical_sha256(body)
    validate_candidate_trace(body)
    return body


def _capture_artifact(value: Mapping[str, object]) -> Mapping[str, object] | None:
    if value["state"] != "captured":
        return None
    artifact = value["artifact"]
    assert isinstance(artifact, Mapping)
    return artifact


_CONTENT_ARTIFACT_KEYS = frozenset({
    "artifact_id", "path", "size_bytes", "sha256", "media_type", "encoding",
})


def _iter_content_artifacts(value: object):
    if isinstance(value, Mapping):
        if frozenset(value) == _CONTENT_ARTIFACT_KEYS:
            yield value
            return
        for child in value.values():
            yield from _iter_content_artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_content_artifacts(child)


def _validate_bundle_artifact_registry(bundle: Mapping[str, object]) -> None:
    by_id: dict[str, Mapping[str, object]] = {}
    by_path: dict[str, Mapping[str, object]] = {}
    for artifact in _iter_content_artifacts(bundle):
        artifact_id = str(artifact["artifact_id"])
        path = str(artifact["path"])
        prior_id = by_id.setdefault(artifact_id, artifact)
        if prior_id != artifact:
            _fail("trial_bundle.artifacts",
                  f"artifact_id {artifact_id!r} resolves to multiple descriptors")
        prior_path = by_path.setdefault(path, artifact)
        if prior_path != artifact:
            _fail("trial_bundle.artifacts",
                  f"artifact path {path!r} resolves to multiple descriptors")


def validate_trial_bundle(bundle: object) -> None:
    """Validate sealed records and their load-bearing cross-record joins.

    This proves identity and artifact continuity inside a supplied bundle.  It
    does not prove that artifact bytes exist on disk; the manifest-last reader
    must re-open, size-check, and hash those bytes before this verifier runs.
    """
    value = _object(bundle, {
        "trial", "action_trace", "retrievals", "renderers",
        "candidate_traces", "annotation", "claim_assessment",
    }, "trial_bundle")
    trial = value["trial"]
    action_trace = value["action_trace"]
    annotation = value["annotation"]
    claim_assessment = value["claim_assessment"]
    validate_trial(trial)
    validate_action_trace(action_trace)
    validate_annotation(annotation)
    validate_final_claim_assessment(claim_assessment)
    assert isinstance(trial, Mapping)
    assert isinstance(action_trace, Mapping)
    assert isinstance(annotation, Mapping)
    assert isinstance(claim_assessment, Mapping)
    trial_id = trial["trial_id"]
    if action_trace["trial_id"] != trial_id:
        _fail("trial_bundle.action_trace.trial_id", "does not match the trial")
    if action_trace["action_trace_id"] != trial["action_trace_id"]:
        _fail("trial_bundle.action_trace.action_trace_id",
              "does not match the trial link")
    if annotation["trial_id"] != trial_id:
        _fail("trial_bundle.annotation.trial_id", "does not match the trial")
    if (claim_assessment["trial_id"] != trial_id
            or claim_assessment["claim_assessment_id"]
            != trial["claim_assessment_id"]):
        _fail("trial_bundle.claim_assessment",
              "does not match the trial claim-assessment link")
    if claim_assessment["annotation_id"] != annotation["annotation_id"]:
        _fail("trial_bundle.claim_assessment.annotation_id",
              "does not match the independent annotation")

    final_events = [event for event in action_trace["events"]
                    if event["kind"] == "final_response"]
    if len(final_events) != 1:
        _fail("trial_bundle.action_trace.events",
              "a completed trial requires exactly one final-response event")
    if (_capture_artifact(final_events[0]["payload"])
            != _capture_artifact(trial["final_response"])):
        _fail("trial_bundle.trial.final_response",
              "must be the exact artifact shown in the final-response event")
    if claim_assessment["final_response"] != _capture_artifact(
            trial["final_response"]):
        _fail("trial_bundle.claim_assessment.final_response",
              "does not match the exact final response artifact")

    invocations = {event["action_id"]: event for event in action_trace["events"]
                   if event["kind"] == "tool_invocation"}
    action_results = {event["action_id"]: event for event in action_trace["events"]
                      if event["kind"] == "tool_result"}
    events_by_id = {event["event_id"]: event for event in action_trace["events"]}
    observed_before_final_event_ids: set[str] = set()
    pending_observations = list(final_events[0]["observation_event_ids"])
    while pending_observations:
        event_id = pending_observations.pop()
        if event_id in observed_before_final_event_ids:
            continue
        observed_before_final_event_ids.add(event_id)
        event = events_by_id[event_id]
        pending_observations.extend(event["observation_event_ids"])
        if event["parent_event_id"] is not None:
            pending_observations.append(event["parent_event_id"])
    trial_origin_ids = {
        origin
        for surface in trial["input_surfaces"]
        for artifact in [_capture_artifact(surface["capture"])]
        if artifact is not None
        for origin in (surface["surface"], artifact["artifact_id"])
    }
    trial_origin_artifacts = {
        artifact["artifact_id"]: artifact
        for surface in trial["input_surfaces"]
        for artifact in [_capture_artifact(surface["capture"])]
        if artifact is not None
    }
    observed_before_final_artifact_ids = {
        artifact["artifact_id"]
        for surface in trial["input_surfaces"]
        for artifact in [_capture_artifact(surface["capture"])]
        if artifact is not None
    }
    observed_before_final_artifact_ids.update(
        artifact["artifact_id"]
        for event_id in observed_before_final_event_ids
        for artifact in [_capture_artifact(events_by_id[event_id]["payload"])]
        if artifact is not None)
    for action_id, invocation in invocations.items():
        if invocation["trigger"] != "hook_side_effect":
            continue
        observed_hooks = [events_by_id[event_id]
                          for event_id in invocation["observation_event_ids"]
                          if events_by_id[event_id]["kind"] == "hook_receipt"]
        if not any(hook["hook_effect"] == "retrieval_side_effect"
                   for hook in observed_hooks):
            _fail(f"trial_bundle.action_trace.actions.{action_id}",
                  "hook-side-effect trigger contradicts the observed prompt-only hooks")

    if not isinstance(value["retrievals"], list):
        _fail("trial_bundle.retrievals", "must be an array")
    retrievals: dict[str, Mapping[str, object]] = {}
    for index, retrieval_value in enumerate(value["retrievals"]):
        validate_retrieval(retrieval_value)
        assert isinstance(retrieval_value, Mapping)
        retrieval_id = retrieval_value["retrieval_invocation_id"]
        if retrieval_id in retrievals:
            _fail("trial_bundle.retrievals", "retrieval ids must be unique")
        if retrieval_value["trial_id"] != trial_id:
            _fail(f"trial_bundle.retrievals[{index}].trial_id",
                  "does not match the trial")
        if retrieval_value["action_trace_id"] != action_trace["action_trace_id"]:
            _fail(f"trial_bundle.retrievals[{index}].action_trace_id",
                  "does not match the observed action trace")
        if (retrieval_value["retriever"]["source_generation_id"]
                != trial["generation"]["source_generation_id"]
                or retrieval_value["retriever"]["index_generation_id"]
                != trial["generation"]["index_generation_id"]):
            _fail(f"trial_bundle.retrievals[{index}].retriever",
                  "does not match the trial-frozen source/index generation")
        if retrieval_value["retriever"]["build_id"] != trial["build_id"]:
            _fail(f"trial_bundle.retrievals[{index}].retriever.build_id",
                  "does not match the trial build")
        action_id = retrieval_value["action_id"]
        if action_id not in invocations:
            _fail(f"trial_bundle.retrievals[{index}].action_id",
                  "does not resolve to an observed tool invocation")
        invocation_event = invocations[action_id]
        if (_capture_artifact(invocation_event["arguments"])
                != _capture_artifact(retrieval_value["structured_args"])):
            _fail(f"trial_bundle.retrievals[{index}].structured_args",
                  "must be the exact arguments observed at tool invocation")
        expected_tool_surface = f"agrep {retrieval_value['surface']}"
        if invocation_event["tool_surface"] != expected_tool_surface:
            _fail(f"trial_bundle.retrievals[{index}].surface",
                  "does not match the observed logical tool surface")
        available_origin_ids = set(trial_origin_ids)
        available_origin_artifacts = dict(trial_origin_artifacts)
        pending_origins = list(invocation_event["observation_event_ids"])
        seen_origins = set()
        while pending_origins:
            event_id = pending_origins.pop()
            if event_id in seen_origins:
                continue
            seen_origins.add(event_id)
            event = events_by_id[event_id]
            available_origin_ids.add(event_id)
            for field in ("arguments", "payload"):
                artifact = _capture_artifact(event[field])
                if artifact is not None:
                    available_origin_ids.add(artifact["artifact_id"])
                    available_origin_artifacts[artifact["artifact_id"]] = artifact
            pending_origins.extend(event["observation_event_ids"])
            if event["parent_event_id"] is not None:
                pending_origins.append(event["parent_event_id"])
        for provenance in retrieval_value["query_provenance"]:
            if provenance["state"] == "observed" and any(
                    origin not in available_origin_ids
                    for origin in provenance["origin_surface_ids"]):
                _fail(f"trial_bundle.retrievals[{index}].query_provenance",
                      "observed query origins must resolve to supplied observations")
            for origin in provenance["origin_ranges"]:
                artifact = available_origin_artifacts.get(origin["artifact_id"])
                if (origin["origin_surface_id"] not in available_origin_ids
                        or artifact is None):
                    _fail(f"trial_bundle.retrievals[{index}].query_provenance",
                          "origin coordinates must resolve to an observed artifact")
                if origin["origin_end"] > artifact["size_bytes"]:
                    _fail(f"trial_bundle.retrievals[{index}].query_provenance",
                          "origin byte coordinates exceed the observed artifact")
        if retrieval_value["status"] != "proposed":
            if action_id not in action_results:
                _fail(f"trial_bundle.retrievals[{index}]",
                      "terminal retrieval lacks an observed tool result")
            action_result = action_results[action_id]
            if action_result["status"] != retrieval_value["status"]:
                _fail(f"trial_bundle.retrievals[{index}].status",
                      "does not match the observed tool result")
            if (action_result["exit_code"] != retrieval_value["exit_code"]
                    or action_result["elapsed_ms"] != retrieval_value["elapsed_ms"]):
                _fail(f"trial_bundle.retrievals[{index}]",
                      "execution metrics do not match the observed result")
            if (_capture_artifact(action_result["payload"])
                    != _capture_artifact(retrieval_value["rendered_output"])):
                _fail(f"trial_bundle.retrievals[{index}].rendered_output",
                      "must be the exact artifact shown to the agent")
        elif action_id in action_results:
            _fail(f"trial_bundle.retrievals[{index}].status",
                  "proposed retrieval contradicts an observed terminal result")
        retrievals[retrieval_id] = retrieval_value
    if list(retrievals) != trial["retrieval_invocation_ids"]:
        _fail("trial_bundle.retrievals",
              "must appear exactly in the trial-declared invocation order")

    if not isinstance(value["renderers"], list):
        _fail("trial_bundle.renderers", "must be an array")
    renderers: dict[str, Mapping[str, object]] = {}
    for index, renderer_value in enumerate(value["renderers"]):
        validate_renderer(renderer_value)
        assert isinstance(renderer_value, Mapping)
        renderer_id = renderer_value["renderer_record_id"]
        if renderer_id in renderers:
            _fail("trial_bundle.renderers", "renderer ids must be unique")
        retrieval_id = renderer_value["retrieval_invocation_id"]
        if renderer_value["trial_id"] != trial_id or retrieval_id not in retrievals:
            _fail(f"trial_bundle.renderers[{index}]",
                  "does not resolve to this trial and retrieval")
        retrieval = retrievals[retrieval_id]
        if renderer_value["renderer"]["build_id"] != trial["build_id"]:
            _fail(f"trial_bundle.renderers[{index}].renderer.build_id",
                  "does not match the trial build")
        if (renderer_value["renderer"]["profile"]
                != retrieval["execution_options"]["output_profile"]):
            _fail(f"trial_bundle.renderers[{index}].renderer.profile",
                  "does not match the retrieval output profile")
        if retrieval["renderer_record_id"] != renderer_id:
            _fail(f"trial_bundle.renderers[{index}].renderer_record_id",
                  "does not match the retrieval link")
        if (_capture_artifact(renderer_value["output"])
                != _capture_artifact(retrieval["rendered_output"])):
            _fail(f"trial_bundle.renderers[{index}].output",
                  "does not match exact retrieval stdout")
        renderers[renderer_id] = renderer_value

    labels = {label["label_id"]: label for label in annotation["labels"]}
    annotation_slots = {slot["slot_id"]: slot for slot in annotation["slots"]}
    authorities = {authority["authority_id"]: authority
                   for authority in annotation["authorities"]}
    if not isinstance(value["candidate_traces"], list):
        _fail("trial_bundle.candidate_traces", "must be an array")
    traces: dict[str, Mapping[str, object]] = {}
    around_links = set()
    trace_candidates: list[Mapping[str, object]] = []
    for index, trace_value in enumerate(value["candidate_traces"]):
        validate_candidate_trace(trace_value)
        assert isinstance(trace_value, Mapping)
        trace_id = trace_value["trace_id"]
        if trace_id in traces:
            _fail("trial_bundle.candidate_traces", "trace ids must be unique")
        retrieval_id = trace_value["retrieval_invocation_id"]
        if trace_value["trial_id"] != trial_id or retrieval_id not in retrievals:
            _fail(f"trial_bundle.candidate_traces[{index}]",
                  "does not resolve to this trial and retrieval")
        retrieval = retrievals[retrieval_id]
        if retrieval["candidate_trace_id"] != trace_id:
            _fail(f"trial_bundle.candidate_traces[{index}].trace_id",
                  "does not match the retrieval link")
        if trace_value["query_variant_id"] != retrieval["query_variant_id"]:
            _fail(f"trial_bundle.candidate_traces[{index}].query_variant_id",
                  "does not match the executed query variant")
        if trace_value["generation_id"] != retrieval["retriever"]["index_generation_id"]:
            _fail(f"trial_bundle.candidate_traces[{index}].generation_id",
                  "does not match the retrieval index generation")
        if trace_value["renderer_record_id"] != retrieval["renderer_record_id"]:
            _fail(f"trial_bundle.candidate_traces[{index}].renderer_record_id",
                  "does not match the retrieval renderer")
        if trace_value["annotation_id"] != annotation["annotation_id"]:
            _fail(f"trial_bundle.candidate_traces[{index}].annotation_id",
                  "does not match the independent annotation")
        captured_retrieval_lanes = [lane for lane in retrieval["lanes"]
                                    if lane["state"] == "captured"]
        if [lane["lane"] for lane in trace_value["lane_diagnostics"]] != [
                lane["lane"] for lane in captured_retrieval_lanes]:
            _fail(f"trial_bundle.candidate_traces[{index}].lane_diagnostics",
                  "must account for every captured retrieval lane in order")
        for trace_lane, retrieval_lane in zip(
                trace_value["lane_diagnostics"], captured_retrieval_lanes):
            pool_artifact = _capture_artifact(retrieval_lane["pool"])
            assert pool_artifact is not None
            if (trace_lane["kind"] != retrieval_lane["kind"]
                    or trace_lane["role"] != retrieval_lane["role"]
                    or trace_lane["score_kind"] != retrieval_lane["score_kind"]
                    or trace_lane["diagnostics"] != retrieval_lane["diagnostics"]
                    or trace_lane["pool_artifact_id"]
                    != pool_artifact["artifact_id"]):
                _fail(f"trial_bundle.candidate_traces[{index}].lane_diagnostics.{trace_lane['lane']}",
                      "does not match the frozen retrieval lane pool")
        for candidate in trace_value["candidates"]:
            trace_candidates.append(candidate)
            candidate_id = candidate["candidate_id"]
            preview = candidate["preview_warrant"]
            label = labels.get(preview["label_id"])
            if label != {
                    "label_id": preview["label_id"], "kind": "preview_warrant",
                    "candidate_id": candidate_id, "slot_id": None,
                    "state": preview["state"]}:
                _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.preview_warrant",
                      "does not resolve to the sealed annotation label registry")
            for support in candidate["slot_support"]:
                label = labels.get(support["label_id"])
                if label != {
                        "label_id": support["label_id"], "kind": "source_support",
                        "candidate_id": candidate_id, "slot_id": support["slot_id"],
                        "state": support["state"]}:
                    _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.slot_support",
                          "does not resolve to the sealed annotation label registry")
            selection = candidate["selection"]
            if (candidate["displayed_handle"] is not None
                    and candidate["displayed_handle"] not in retrieval["returned_handles"]):
                _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.displayed_handle",
                      "was not returned by the parent retrieval")
            if selection["opened"]:
                around_id = selection["around_invocation_id"]
                around = retrievals.get(around_id)
                if (around is None or around["surface"] != "around"
                        or around["status"] != "executed_completed"):
                    _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection",
                          "does not resolve to a completed observed around invocation")
                if around["opened_handle"] != candidate["displayed_handle"]:
                    _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection",
                          "around did not open this candidate's displayed handle")
                parent_result = action_results[retrieval["action_id"]]
                around_invocation = invocations[around["action_id"]]
                if around_invocation["sequence"] <= parent_result["sequence"]:
                    _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection",
                          "around must occur after the parent retrieval result was observed")
                around_renderer = renderers.get(around["renderer_record_id"])
                if around_renderer is None:
                    _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection",
                          "opened source lacks an exact around renderer")
                exact_opened_ranges = [
                    _validate_source_range(source_range,
                                           "trial_bundle.around.visible_source_ranges")
                    for segment in around_renderer["segments"]
                    if (segment["candidate_id"] == candidate_id
                        and segment["mapping_kind"] == "exact_decoded_source")
                    for source_range in segment["visible_source_ranges"]
                ]
                for source_range in selection["selected_source_ranges"]:
                    parsed = _validate_source_range(
                        source_range,
                        f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection")
                    if not any(_range_within(parsed, opened_range)
                               for opened_range in exact_opened_ranges):
                        _fail(f"trial_bundle.candidate_traces[{index}].candidates.{candidate_id}.selection",
                              "selected ranges were not exact bytes shown by around")
                around_links.add(around_id)

        renderer = renderers.get(trace_value["renderer_record_id"])
        if renderer is None:
            _fail(f"trial_bundle.candidate_traces[{index}].renderer_record_id",
                  "does not resolve to a supplied renderer")
        render_decisions = trace_value["stages"][-1]["decisions"]
        kept_decisions = [decision for decision in render_decisions
                          if decision["decision"] == "kept"]
        segments = renderer["segments"]
        if [decision["candidate_id"] for decision in kept_decisions] != [
                segment["candidate_id"] for segment in segments]:
            _fail(f"trial_bundle.candidate_traces[{index}].stages.render",
                  "render survivors do not match displayed renderer segments")
        for decision, segment in zip(kept_decisions, segments):
            candidate = next(candidate for candidate in trace_value["candidates"]
                             if candidate["candidate_id"] == decision["candidate_id"])
            if segment["displayed_handle"] != candidate["displayed_handle"]:
                _fail(f"trial_bundle.candidate_traces[{index}].stages.render",
                      "renderer handle contradicts the candidate handle")
            expected_ranges = (segment["visible_source_ranges"]
                               if segment["mapping_kind"] == "exact_decoded_source"
                               else [])
            if decision["visible_source_ranges"] != expected_ranges:
                _fail(f"trial_bundle.candidate_traces[{index}].stages.render",
                      "visible source ranges contradict the renderer mapping")

        trace_slot_ids = [gold["slot_id"] for gold in trace_value["gold_slots"]]
        if trace_slot_ids != list(annotation_slots):
            _fail(f"trial_bundle.candidate_traces[{index}].gold_slots",
                  "must match the annotation slot order")
        for gold in trace_value["gold_slots"]:
            slot = annotation_slots[gold["slot_id"]]
            if gold["required"] != slot["required"]:
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "required status does not match the annotation")
            selected_ids = gold["selected_authority_ids"]
            if not any(set(selected_ids) == set(authority_set)
                       for authority_set in slot["acceptable_authority_sets"]):
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "selected authority set must preserve one complete annotated AND alternative")
            selected = [authorities[authority_id] for authority_id in selected_ids]
            historical = [authority for authority in selected
                          if authority["kind"] == "historical_span"]
            nonhistorical = [authority for authority in selected
                             if authority["kind"] != "historical_span"]
            if gold["state"] == "source_bound" and (not historical or nonhistorical):
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "source-bound slot must select only historical authorities")
            if gold["state"] == "current_authority_required" and (
                    historical or not selected
                    or any(authority["kind"] != "current_snapshot"
                           for authority in selected)):
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "current-authority slot must select only current authorities")
            if gold["state"] == "mixed_authority_required" and (
                    not historical or not nonhistorical):
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "mixed authority slot requires both historical and nonhistorical authority")
            expected_historical_ranges = {
                _validate_source_range(source_range,
                                       "trial_bundle.annotation.authorities")
                for authority in historical
                for source_range in authority["source_ranges"]
            }
            observed_gold_ranges = {
                _validate_source_range(source_range,
                                       "trial_bundle.candidate_trace.gold_slots")
                for source_range in gold["source_ranges"]
            }
            if observed_gold_ranges != expected_historical_ranges:
                _fail(f"trial_bundle.candidate_traces[{index}].gold_slots.{gold['slot_id']}",
                      "gold ranges must exactly preserve every selected historical authority")
        traces[trace_id] = trace_value

    visible_authority_artifacts = {
        surface["capture"]["artifact"]["artifact_id"]
        for surface in trial["input_surfaces"]
        if (surface["surface"] in ("user_request", "visible_conversation",
                                   "workspace_state")
            and surface["capture"]["state"] == "captured")
    }
    current_authority_artifacts = {
        authority["artifact"]["artifact"]["artifact_id"]
        for authority in annotation["authorities"]
        if (authority["kind"] == "current_snapshot"
            and authority["artifact"]["state"] == "captured")
    }
    for index, claim in enumerate(claim_assessment["claims"]):
        where = f"trial_bundle.claim_assessment.claims[{index}]"
        slot_id = claim["slot_id"]
        if slot_id is not None and slot_id not in annotation_slots:
            _fail(f"{where}.slot_id", "does not resolve to an annotated slot")
        if claim["outcome"] in (
                "supported", "partial", "contradicted", "unresolved"
        ) and slot_id is None:
            _fail(where,
                  "assessed response claims must resolve to a requested slot")
        claim_authority_ids = []
        for evidence in claim["evidence"]:
            authority = authorities.get(evidence["authority_id"])
            if authority is None:
                _fail(f"{where}.evidence", "does not resolve to annotated authority")
            expected_kind = {
                "historical_span": "opened_history_span",
                "current_snapshot": "current_authority",
                "visible_context": "visible_context",
            }[authority["kind"]]
            if evidence["kind"] != expected_kind:
                _fail(f"{where}.evidence",
                      "evidence kind contradicts its annotated authority")
            claim_authority_ids.append(evidence["authority_id"])
            if evidence["kind"] == "visible_context":
                annotated_artifact = _capture_artifact(authority["artifact"])
                if (annotated_artifact is None
                        or evidence["artifact_id"] != annotated_artifact["artifact_id"]
                        or evidence["artifact_id"] not in visible_authority_artifacts):
                    _fail(f"{where}.evidence",
                          "visible-context evidence was not supplied to the agent")
                continue
            if evidence["kind"] == "current_authority":
                annotated_artifact = _capture_artifact(authority["artifact"])
                if (annotated_artifact is None
                        or evidence["artifact_id"] != annotated_artifact["artifact_id"]
                        or evidence["artifact_id"] not in current_authority_artifacts
                        or evidence["artifact_id"]
                        not in observed_before_final_artifact_ids):
                    _fail(f"{where}.evidence",
                          "current authority does not resolve to the frozen annotation")
                continue
            around = retrievals.get(evidence["retrieval_invocation_id"])
            if (around is None or around["surface"] != "around"
                    or around["status"] != "executed_completed"):
                _fail(f"{where}.evidence",
                      "opened-history claim lacks a completed around invocation")
            around_artifact = _capture_artifact(around["rendered_output"])
            assert around_artifact is not None
            if evidence["artifact_id"] != around_artifact["artifact_id"]:
                _fail(f"{where}.evidence",
                      "opened-history claim cites bytes other than around output")
            around_result_event = action_results[around["action_id"]]
            if around_result_event["event_id"] not in observed_before_final_event_ids:
                _fail(f"{where}.evidence",
                      "final response did not observe the cited around result")
            matches = [
                (candidate, trace)
                for trace in traces.values()
                for candidate in trace["candidates"]
                if (candidate["candidate_id"] == evidence["candidate_id"]
                    and candidate["selection"]["opened"]
                    and candidate["selection"]["around_invocation_id"]
                    == evidence["retrieval_invocation_id"])
            ]
            if len(matches) != 1:
                _fail(f"{where}.evidence",
                      "opened-history claim does not resolve to one selected candidate")
            candidate, owner_trace = matches[0]
            trace_slot_result = next(
                (slot_result for slot_result in owner_trace["result"]["slot_results"]
                 if slot_result["slot_id"] == slot_id), None)
            allowed_trace_states = {
                "supported": {"direct"},
                "partial": {"direct", "partial"},
                "contradicted": {"direct", "contradictory"},
            }.get(claim["outcome"])
            if (allowed_trace_states is not None
                    and (trace_slot_result is None
                         or trace_slot_result["status"] not in allowed_trace_states
                         or candidate["candidate_id"]
                         not in trace_slot_result["candidate_ids"])):
                _fail(f"{where}.evidence",
                      "final claim contradicts the sealed retrieval slot result")
            parsed_evidence_ranges = [
                _validate_source_range(source_range, f"{where}.evidence")
                for source_range in evidence["source_ranges"]
            ]
            authority_ranges = [
                _validate_source_range(source_range, f"{where}.authority")
                for source_range in authority["source_ranges"]
            ]
            if any(not any(_range_within(source_range, authority_range)
                           for authority_range in authority_ranges)
                   for source_range in parsed_evidence_ranges):
                _fail(f"{where}.evidence",
                      "opened-history claim exceeds its annotated authority")
            selected_ranges = [
                _validate_source_range(source_range, f"{where}.selection")
                for source_range in candidate["selection"]["selected_source_ranges"]
            ]
            if any(not any(_range_within(source_range, selected_range)
                           for selected_range in selected_ranges)
                   for source_range in parsed_evidence_ranges):
                _fail(f"{where}.evidence",
                      "claim evidence exceeds exact source bytes opened by around")
            if slot_id is not None and claim["outcome"] in (
                    "supported", "partial", "contradicted"):
                support = next((support for support in candidate["slot_support"]
                                if support["slot_id"] == slot_id), None)
                allowed_support_states = {
                    "supported": {"direct"},
                    "partial": {"direct", "partial"},
                    # Support is judged against the requested slot; contradiction
                    # is judged against the agent's final proposition. Keep those
                    # as separate axes.
                    "contradicted": {"direct", "contradictory"},
                }[claim["outcome"]]
                if support is None or support["state"] not in allowed_support_states:
                    _fail(f"{where}.evidence",
                          "final claim exceeds independent source-support labels")
                supporting_ranges = [
                    _validate_source_range(source_range, f"{where}.support")
                    for source_range in support["supporting_source_ranges"]
                ]
                if any(not any(_range_within(source_range, evidence_range)
                               for evidence_range in parsed_evidence_ranges)
                       for source_range in supporting_ranges):
                    _fail(f"{where}.evidence",
                          "final claim omits its exact independently labeled support")
        if slot_id is not None and claim["outcome"] in (
                "supported", "partial", "contradicted"):
            acceptable_sets = annotation_slots[slot_id]["acceptable_authority_sets"]
            observed_set = set(claim_authority_ids)
            if claim["outcome"] in ("supported", "contradicted"):
                if not any(observed_set == set(authority_set)
                           for authority_set in acceptable_sets):
                    _fail(where,
                          "conclusive claim must satisfy one complete annotated authority set")
            elif not any(observed_set and observed_set.issubset(set(authority_set))
                         for authority_set in acceptable_sets):
                _fail(where,
                      "partial claim evidence must belong to one annotated authority set")
        if slot_id is not None and claim["outcome"] == "unresolved":
            trace_states = [
                slot_result["status"]
                for trace in traces.values()
                for slot_result in trace["result"]["slot_results"]
                if slot_result["slot_id"] == slot_id
            ]
            if (annotation_slots[slot_id]["ambiguity"] == "clear"
                    and "unresolved" not in trace_states):
                _fail(where,
                      "unresolved claim requires annotated ambiguity or an unresolved retrieval result")

    coverage_slot_ids = [coverage["slot_id"]
                         for coverage in claim_assessment["slot_coverage"]]
    if coverage_slot_ids != list(annotation_slots):
        _fail("trial_bundle.claim_assessment.slot_coverage",
              "must account once, in annotation order, for every requested slot")

    for retrieval_id, retrieval in retrievals.items():
        if retrieval["candidate_trace_id"] is not None and retrieval["candidate_trace_id"] not in traces:
            _fail(f"trial_bundle.retrievals.{retrieval_id}.candidate_trace_id",
                  "does not resolve to a supplied candidate trace")
        if retrieval["renderer_record_id"] is not None and retrieval["renderer_record_id"] not in renderers:
            _fail(f"trial_bundle.retrievals.{retrieval_id}.renderer_record_id",
                  "does not resolve to a supplied renderer record")
    supplied_around = {retrieval_id for retrieval_id, retrieval in retrievals.items()
                       if retrieval["surface"] == "around"}
    if not around_links.issubset(supplied_around):
        _fail("trial_bundle.retrievals",
              "selected candidates must join supplied around invocations")
    for retrieval_id, retrieval in retrievals.items():
        if retrieval["surface"] not in ("recall", "search"):
            continue
        renderer = renderers.get(retrieval["renderer_record_id"])
        if renderer is None:
            continue
        rendered_handles = [
            segment["displayed_handle"] for segment in renderer["segments"]
            if segment["displayed_handle"] is not None
        ]
        if retrieval["returned_handles"] != rendered_handles:
            _fail(f"trial_bundle.retrievals.{retrieval_id}.returned_handles",
                  "must exactly equal rendered candidate handles in display order")
    _validate_bundle_artifact_registry(value)


def _validate_policy_evaluation(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "trial_id", "history_requirement",
        "context_condition", "input_hashes", "outcomes",
    }
    if require_hash:
        keys.add("evaluation_sha256")
    value = _object(record, keys, "policy_evaluation")
    if (value["schema"] != POLICY_EVALUATION_SCHEMA
            or value["version"] != SCHEMA_VERSION):
        _fail("policy_evaluation", "unsupported schema or version")
    _string(value["trial_id"], "policy_evaluation.trial_id")
    if value["history_requirement"] not in _HISTORY_REQUIREMENTS:
        _fail("policy_evaluation.history_requirement",
              f"must be one of {sorted(_HISTORY_REQUIREMENTS)}")
    if value["context_condition"] not in _CONTEXT_CONDITIONS:
        _fail("policy_evaluation.context_condition",
              f"must be one of {sorted(_CONTEXT_CONDITIONS)}")
    hashes = _object(value["input_hashes"], {
        "trial", "action_trace", "annotation", "claim_assessment",
        "retrievals", "renderers", "candidate_traces",
    }, "policy_evaluation.input_hashes")
    for key in ("trial", "action_trace", "annotation", "claim_assessment"):
        _digest(hashes[key], f"policy_evaluation.input_hashes.{key}")
    for key in ("retrievals", "renderers", "candidate_traces"):
        digests = _string_list(hashes[key], f"policy_evaluation.input_hashes.{key}")
        for index, digest in enumerate(digests):
            _digest(digest, f"policy_evaluation.input_hashes.{key}[{index}]")
    outcomes = _object(value["outcomes"], {
        "invocation", "recall_count", "around_count", "recall_first",
        "around_immediately_after_recall", "opening", "hook",
        "semantic_coverage", "required_slot_coverage", "final_grounding",
    }, "policy_evaluation.outcomes")
    if outcomes["invocation"] not in (
            "satisfied", "allowed", "not_needed", "required_missed",
            "recall_first_violated", "turn_allowance_exceeded",
            "unnecessary_invocation", "explicit_opt_out_violated",
            "observation_incomplete"):
        _fail("policy_evaluation.outcomes.invocation", "unknown invocation outcome")
    _integer(outcomes["recall_count"], "policy_evaluation.outcomes.recall_count")
    _integer(outcomes["around_count"], "policy_evaluation.outcomes.around_count")
    for key in ("recall_first", "around_immediately_after_recall"):
        if outcomes[key] is not None:
            _boolean(outcomes[key], f"policy_evaluation.outcomes.{key}")
    if outcomes["opening"] not in (
            "within_policy", "missed_warranted_preview", "unwarranted_open",
            "genuinely_ambiguous", "too_many_opens", "unjoined_open",
            "observation_incomplete"):
        _fail("policy_evaluation.outcomes.opening", "unknown opening outcome")
    if outcomes["hook"] not in (
            "prompt_only", "retrieval_side_effect", "unknown", "not_observed"):
        _fail("policy_evaluation.outcomes.hook", "unknown hook outcome")
    if outcomes["semantic_coverage"] not in (
            "available", "partial", "unknown_not_searched", "failed",
            "not_run", "not_requested"):
        _fail("policy_evaluation.outcomes.semantic_coverage",
              "unknown semantic coverage outcome")
    if outcomes["required_slot_coverage"] not in (
            "complete", "omitted", "clarified", "no_required_slots"):
        _fail("policy_evaluation.outcomes.required_slot_coverage",
              "unknown slot coverage outcome")
    if outcomes["final_grounding"] not in (
            "grounded_or_clarified", "partial_or_unresolved",
            "unsupported_or_contradicted", "invalid_route", "no_claims"):
        _fail("policy_evaluation.outcomes.final_grounding",
              "unknown final grounding outcome")
    if require_hash:
        supplied = _digest(value["evaluation_sha256"],
                           "policy_evaluation.evaluation_sha256")
        body = dict(value)
        del body["evaluation_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("policy_evaluation.evaluation_sha256", "canonical hash mismatch")


def validate_policy_evaluation(record: object) -> None:
    _validate_json(record)
    _validate_policy_evaluation(record, require_hash=True)


def evaluate_trial_policy(bundle: object) -> dict:
    """Derive policy/use outcomes without making failed trials unrepresentable."""
    validate_trial_bundle(bundle)
    assert isinstance(bundle, Mapping)
    trial = bundle["trial"]
    action_trace = bundle["action_trace"]
    annotation = bundle["annotation"]
    claim_assessment = bundle["claim_assessment"]
    retrievals = bundle["retrievals"]
    traces = bundle["candidate_traces"]
    retrieval_by_action = {retrieval["action_id"]: retrieval
                           for retrieval in retrievals}
    tool_invocations = [event for event in action_trace["events"]
                        if event["kind"] == "tool_invocation"]
    recall_records = [retrieval for retrieval in retrievals
                      if retrieval["surface"] == "recall"]
    around_records = [retrieval for retrieval in retrievals
                      if retrieval["surface"] == "around"]
    recall_count = len(recall_records)
    around_count = len(around_records)
    first_tool = tool_invocations[0] if tool_invocations else None
    recall_first = None
    if recall_count:
        recall_first = (first_tool is not None
                        and retrieval_by_action.get(first_tool["action_id"], {}).get(
                            "surface") == "recall")

    requirement = annotation["history_requirement"]
    observation_complete = action_trace["completeness"]["state"] == "complete"
    postcompact_mandatory = (
        trial["context_condition"] == "immediate_post_compaction"
        and requirement != "explicit_opt_out")
    recall_required = requirement == "required" or postcompact_mandatory
    if requirement == "explicit_opt_out" and recall_count:
        invocation_outcome = "explicit_opt_out_violated"
    elif recall_count > 1:
        invocation_outcome = "turn_allowance_exceeded"
    elif not observation_complete:
        invocation_outcome = "observation_incomplete"
    elif recall_required and recall_count == 0:
        invocation_outcome = "required_missed"
    elif recall_required and not recall_first:
        invocation_outcome = "recall_first_violated"
    elif requirement == "unnecessary" and recall_count and not postcompact_mandatory:
        invocation_outcome = "unnecessary_invocation"
    elif recall_required:
        invocation_outcome = "satisfied"
    elif requirement == "allowed_or_mixed":
        invocation_outcome = "allowed"
    else:
        invocation_outcome = "not_needed"

    selected = [candidate for trace in traces for candidate in trace["candidates"]
                if candidate["selection"]["opened"]]
    rendered_candidate_ids = {
        decision["candidate_id"]
        for trace in traces
        for decision in trace["stages"][-1]["decisions"]
        if decision["decision"] == "kept"
    }
    open_warranted = [
        candidate for trace in traces for candidate in trace["candidates"]
        if (candidate["candidate_id"] in rendered_candidate_ids
            and candidate["preview_warrant"]["state"] == "open_warranted")
    ]
    if around_count > 1 or len(selected) > 1:
        opening_outcome = "too_many_opens"
    elif around_count and not selected:
        opening_outcome = "unjoined_open"
    elif not observation_complete:
        opening_outcome = "observation_incomplete"
    elif not selected and open_warranted:
        opening_outcome = "missed_warranted_preview"
    elif selected and selected[0]["preview_warrant"]["state"] == "abstain_warranted":
        opening_outcome = "unwarranted_open"
    elif selected and selected[0]["preview_warrant"]["state"] == "genuinely_ambiguous":
        opening_outcome = "genuinely_ambiguous"
    else:
        opening_outcome = "within_policy"

    immediate_around = None
    if recall_count == 1 and around_count == 1:
        tool_action_ids = [event["action_id"] for event in tool_invocations]
        recall_action = recall_records[0]["action_id"]
        around_action = around_records[0]["action_id"]
        immediate_around = (recall_action in tool_action_ids
                            and around_action in tool_action_ids
                            and tool_action_ids.index(around_action)
                            == tool_action_ids.index(recall_action) + 1)

    hook_events = [event for event in action_trace["events"]
                   if event["kind"] == "hook_receipt"]
    if (any(event["hook_effect"] == "retrieval_side_effect" for event in hook_events)
            or any(event["trigger"] == "hook_side_effect"
                   for event in tool_invocations)):
        hook_outcome = "retrieval_side_effect"
    elif not hook_events:
        hook_outcome = "not_observed"
    elif any(event["hook_effect"] == "unknown" for event in hook_events):
        hook_outcome = "unknown"
    else:
        hook_outcome = "prompt_only"

    semantic_states = [retrieval["semantic"]["state"] for retrieval in recall_records]
    if not semantic_states:
        semantic_outcome = "not_run"
    elif "meaning_unavailable" in semantic_states:
        semantic_outcome = "unknown_not_searched"
    elif "failed" in semantic_states:
        semantic_outcome = "failed"
    elif "partial" in semantic_states:
        semantic_outcome = "partial"
    elif all(state == "not_requested" for state in semantic_states):
        semantic_outcome = "not_requested"
    elif any(state == "not_run" for state in semantic_states):
        semantic_outcome = "not_run"
    else:
        semantic_outcome = "available"

    annotation_slots = {slot["slot_id"]: slot for slot in annotation["slots"]}
    required_coverage = [coverage for coverage in claim_assessment["slot_coverage"]
                         if annotation_slots[coverage["slot_id"]]["required"]]
    if not required_coverage:
        slot_coverage_outcome = "no_required_slots"
    elif any(coverage["state"] == "omitted" for coverage in required_coverage):
        slot_coverage_outcome = "omitted"
    elif all(coverage["state"] == "clarified" for coverage in required_coverage):
        slot_coverage_outcome = "clarified"
    else:
        slot_coverage_outcome = "complete"

    claim_outcomes = [claim["outcome"] for claim in claim_assessment["claims"]]
    assessed_routes = set()
    for claim in claim_assessment["claims"]:
        if claim["outcome"] == "clarification":
            assessed_routes.add("clarification")
        elif claim["outcome"] == "unresolved":
            assessed_routes.add("abstention")
        elif claim["claim_authority"] == "opened_history_span":
            assessed_routes.add("history_retrieval")
        elif claim["claim_authority"] == "current_authority":
            assessed_routes.add("current_authority")
        elif claim["claim_authority"] == "visible_context":
            assessed_routes.add("visible_context_only")
        elif claim["claim_authority"] == "mixed_authority":
            assessed_routes.add("multiple_routes")
    route_valid = assessed_routes.issubset(set(annotation["valid_outcomes"]))
    if assessed_routes and not route_valid:
        final_grounding = "invalid_route"
    elif not claim_outcomes:
        final_grounding = "no_claims"
    elif any(outcome in ("unsupported", "contradicted")
             for outcome in claim_outcomes):
        final_grounding = "unsupported_or_contradicted"
    elif any(outcome in ("partial", "unresolved") for outcome in claim_outcomes):
        final_grounding = "partial_or_unresolved"
    else:
        final_grounding = "grounded_or_clarified"

    body = {
        "schema": POLICY_EVALUATION_SCHEMA,
        "version": SCHEMA_VERSION,
        "trial_id": trial["trial_id"],
        "history_requirement": requirement,
        "context_condition": trial["context_condition"],
        "input_hashes": {
            "trial": trial["trial_sha256"],
            "action_trace": action_trace["action_trace_sha256"],
            "annotation": annotation["annotation_sha256"],
            "claim_assessment": claim_assessment["claim_assessment_sha256"],
            "retrievals": [retrieval["retrieval_sha256"]
                           for retrieval in retrievals],
            "renderers": [renderer["renderer_sha256"]
                          for renderer in bundle["renderers"]],
            "candidate_traces": [trace["trace_sha256"] for trace in traces],
        },
        "outcomes": {
            "invocation": invocation_outcome,
            "recall_count": recall_count,
            "around_count": around_count,
            "recall_first": recall_first,
            "around_immediately_after_recall": immediate_around,
            "opening": opening_outcome,
            "hook": hook_outcome,
            "semantic_coverage": semantic_outcome,
            "required_slot_coverage": slot_coverage_outcome,
            "final_grounding": final_grounding,
        },
    }
    body["evaluation_sha256"] = canonical_sha256(body)
    validate_policy_evaluation(body)
    return body


def _validate_trace(record: object, *, require_hash: bool) -> None:
    keys = {
        "schema", "version", "trace_id", "generation_id", "query_id",
        "diagnostics", "candidates", "stages", "gold", "result",
    }
    if require_hash:
        keys.add("trace_sha256")
    value = _object(record, keys, "trace")
    if value["schema"] != TRACE_SCHEMA or value["version"] != SCHEMA_VERSION:
        _fail("trace", "unsupported schema or version")
    for key in ("trace_id", "generation_id", "query_id"):
        _string(value[key], f"trace.{key}")
    _validate_diagnostics(value["diagnostics"], "trace.diagnostics")

    if not isinstance(value["candidates"], list):
        _fail("trace.candidates", "must be an array")
    candidates: dict[str, Mapping[str, object]] = {}
    supports: dict[str, str] = {}
    support_spans: dict[str, set[str]] = {}
    support_label_kinds: dict[str, str] = {}
    unresolved: set[str] = set()
    ranks = []
    lane_ranks: dict[str, set[int]] = {}
    candidate_keys = {
        "candidate_id", "evidence_id", "view_id", "exact_identity_id",
        "session_id", "family_id", "source_span_ids", "unresolved_reason",
        "lane_scores", "support", "raw_rank",
    }
    for index, item in enumerate(value["candidates"]):
        where = f"trace.candidates[{index}]"
        candidate = _object(item, candidate_keys, where)
        candidate_id = _string(candidate["candidate_id"], f"{where}.candidate_id")
        assert candidate_id is not None
        if candidate_id in candidates:
            _fail("trace.candidates", "candidate_id values must be unique")
        identities = []
        for key in ("evidence_id", "view_id", "exact_identity_id", "session_id",
                    "family_id"):
            identities.append(_string(
                candidate[key], f"{where}.{key}", nullable=True))
        source_spans = _string_list(
            candidate["source_span_ids"], f"{where}.source_span_ids")
        unresolved_reason = _string(
            candidate["unresolved_reason"], f"{where}.unresolved_reason",
            nullable=True)
        is_unresolved = any(identity is None for identity in identities) or not source_spans
        if is_unresolved and unresolved_reason is None:
            _fail(where, "missing identities or source spans require unresolved_reason")
        if not is_unresolved and unresolved_reason is not None:
            _fail(where, "resolved candidates require unresolved_reason=null")
        if is_unresolved:
            unresolved.add(candidate_id)

        if not isinstance(candidate["lane_scores"], list):
            _fail(f"{where}.lane_scores", "must be an array")
        if not candidate["lane_scores"]:
            _fail(f"{where}.lane_scores", "must record at least one retrieval lane")
        score_lanes = []
        for score_index, score_value in enumerate(candidate["lane_scores"]):
            score_where = f"{where}.lane_scores[{score_index}]"
            score = _object(
                score_value, {"lane", "rank", "score", "score_kind"}, score_where)
            lane = _string(score["lane"], f"{score_where}.lane")
            assert lane is not None
            lane_rank = _integer(score["rank"], f"{score_where}.rank")
            if lane_rank == 0:
                _fail(f"{score_where}.rank", "ranks are one-based")
            observed_ranks = lane_ranks.setdefault(lane, set())
            if lane_rank in observed_ranks:
                _fail(f"{where}.lane_scores", f"{lane} ranks must be unique")
            observed_ranks.add(lane_rank)
            _number(score["score"], f"{score_where}.score")
            _string(score["score_kind"], f"{score_where}.score_kind")
            score_lanes.append(lane)
        if len(score_lanes) != len(set(score_lanes)):
            _fail(f"{where}.lane_scores", "lane names must be unique per candidate")

        support = _object(
            candidate["support"],
            {"state", "supporting_span_ids", "label_provenance"},
            f"{where}.support")
        support_state = support["state"]
        if support_state not in _SUPPORT_STATES:
            _fail(f"{where}.support.state",
                  f"must be one of {sorted(_SUPPORT_STATES)}")
        supporting_spans = _string_list(
            support["supporting_span_ids"], f"{where}.support.supporting_span_ids")
        if not set(supporting_spans).issubset(source_spans):
            _fail(f"{where}.support.supporting_span_ids",
                  "must be exact spans carried by this candidate")
        if support_state in ("direct", "partial") and not supporting_spans:
            _fail(f"{where}.support", "support labels require exact supporting spans")
        if support_state not in ("direct", "partial") and supporting_spans:
            _fail(f"{where}.support", "non-support labels cannot carry supporting spans")
        provenance = _object(
            support["label_provenance"], {"kind", "label_id"},
            f"{where}.support.label_provenance")
        provenance_kind = _string(
            provenance["kind"], f"{where}.support.label_provenance.kind")
        assert provenance_kind is not None
        _string(provenance["label_id"], f"{where}.support.label_provenance.label_id")
        supports[candidate_id] = support_state
        support_spans[candidate_id] = set(supporting_spans)
        support_label_kinds[candidate_id] = provenance_kind

        rank = _integer(candidate["raw_rank"], f"{where}.raw_rank")
        if rank == 0:
            _fail(f"{where}.raw_rank", "ranks are one-based")
        candidates[candidate_id] = candidate
        ranks.append(rank)
    if len(ranks) != len(set(ranks)):
        _fail("trace.candidates", "raw_rank values must be unique")
    pool_observed = value["diagnostics"]["truncation"]["observed_count"]
    if pool_observed != len(candidates):
        _fail("trace.diagnostics.truncation.observed_count",
              "must equal the number of recorded candidates")
    raw_order = [candidate_id for candidate_id, candidate in sorted(
        candidates.items(), key=lambda item: item[1]["raw_rank"])]

    if not isinstance(value["stages"], list):
        _fail("trace.stages", "must be an array")
    stage_names = [item.get("stage") if isinstance(item, Mapping) else None
                   for item in value["stages"]]
    if stage_names != list(STAGES):
        _fail("trace.stages", f"must appear exactly in order {list(STAGES)}")
    active = raw_order
    outputs: dict[str, list[str]] = {}
    for stage_index, stage_value in enumerate(value["stages"]):
        stage_name = STAGES[stage_index]
        stage = _object(stage_value, {"stage", "decisions"},
                        f"trace.stages[{stage_index}]")
        if not isinstance(stage["decisions"], list):
            _fail(f"trace.stages[{stage_index}].decisions", "must be an array")
        decisions = stage["decisions"]
        decision_ids = [item.get("candidate_id") if isinstance(item, Mapping) else None
                        for item in decisions]
        if decision_ids != active:
            _fail(f"trace.stages[{stage_index}].decisions",
                  "must account once, in order, for every active candidate")
        kept = []
        parsed_decisions = []
        for decision_index, decision_value in enumerate(decisions):
            where = f"trace.stages[{stage_index}].decisions[{decision_index}]"
            decision = _object(
                decision_value, {"candidate_id", "decision", "reason", "retained_as"},
                where)
            candidate_id = decision["candidate_id"]
            if decision["decision"] not in ("kept", "dropped"):
                _fail(f"{where}.decision", "must be kept or dropped")
            _string(decision["reason"], f"{where}.reason")
            retained_as = _string(
                decision["retained_as"], f"{where}.retained_as", nullable=True)
            if decision["decision"] == "kept":
                if retained_as != candidate_id:
                    _fail(where, "kept candidates must retain themselves")
                kept.append(candidate_id)
            parsed_decisions.append((candidate_id, decision["decision"], retained_as,
                                     where))
        kept_set = set(kept)
        identity_key = {
            "view": "evidence_id", "exact_identity": "exact_identity_id",
            "session": "session_id", "family": "family_id",
        }.get(stage_name)
        for candidate_id, decision, retained_as, where in parsed_decisions:
            if stage_name == "raw" and decision != "kept":
                _fail(where, "raw stage is strict pass-through")
            if (stage_name in ("view", "exact_identity", "session", "family")
                    and candidate_id in unresolved and decision != "kept"):
                _fail(where, "unresolved candidates must survive collapse stages")
            if decision != "dropped":
                continue
            if identity_key is not None:
                if retained_as is None:
                    _fail(where, "collapse drops require a retained representative")
                if retained_as not in kept_set:
                    _fail(where, "retained_as must name a candidate kept at this stage")
                if (candidates[candidate_id][identity_key] is None
                        or candidates[candidate_id][identity_key]
                        != candidates[retained_as][identity_key]):
                    _fail(where, f"retained_as must share resolved {identity_key}")
            elif stage_name == "render":
                if retained_as is not None:
                    _fail(where, "render drops are budget removals, not identity merges")
                truncation_state = value["diagnostics"]["truncation"]["state"]
                budget_state = value["diagnostics"]["budget"]["state"]
                if (budget_state != "budget_exhausted"
                        and truncation_state not in
                        ("render_truncated", "multiple_truncations")):
                    _fail(where, "render drops require an explicit render budget limit")
        active = kept
        outputs[stage_name] = list(active)

    gold = _object(
        value["gold"],
        {"state", "source_span_ids", "availability", "first_loss_stage"},
        "trace.gold")
    if gold["state"] not in _GOLD_STATES:
        _fail("trace.gold.state", f"must be one of {sorted(_GOLD_STATES)}")
    gold_spans = _string_list(gold["source_span_ids"], "trace.gold.source_span_ids")
    availability = _object(gold["availability"], set(CHECKPOINTS),
                           "trace.gold.availability")
    first_loss_stage = _string(
        gold["first_loss_stage"], "trace.gold.first_loss_stage", nullable=True)
    if first_loss_stage is not None and first_loss_stage not in STAGES:
        _fail("trace.gold.first_loss_stage", f"must be one of {list(STAGES)} or null")
    for checkpoint in CHECKPOINTS:
        if availability[checkpoint] not in _AVAILABILITY_STATES:
            _fail(f"trace.gold.availability.{checkpoint}",
                  f"must be one of {sorted(_AVAILABILITY_STATES)}")
    if gold["state"] == "gold_not_judged":
        if (gold_spans or first_loss_stage is not None
                or any(availability[key] != "gold_not_judged"
                       for key in CHECKPOINTS)):
            _fail("trace.gold", "unjudged gold must have no spans and unjudged checkpoints")
    elif gold["state"] == "source_bound":
        if not gold_spans or availability["frozen_source"] != "present":
            _fail("trace.gold", "source-bound gold requires spans present in frozen_source")
        gold_set = set(gold_spans)
        stage_presence = {}
        for stage_name in STAGES:
            present = any(gold_set.intersection(candidates[candidate_id]["source_span_ids"])
                          for candidate_id in outputs[stage_name])
            stage_presence[stage_name] = present
            state = availability[stage_name]
            if present and state != "present":
                _fail(f"trace.gold.availability.{stage_name}",
                      "must be present when a surviving candidate contains a gold span")
            if not present and state == "present":
                _fail(f"trace.gold.availability.{stage_name}",
                      "cannot be present without a surviving gold candidate")
        if stage_presence["raw"] and availability["index"] != "present":
            _fail("trace.gold.availability.index", "raw-present gold must be index-present")
        derived_loss = None
        if availability["index"] == "present":
            derived_loss = next(
                (stage for stage in STAGES if not stage_presence[stage]), None)
        if first_loss_stage != derived_loss:
            _fail("trace.gold.first_loss_stage",
                  f"must equal derived first loss stage {derived_loss!r}")
        if derived_loss is not None:
            loss_index = STAGES.index(derived_loss)
            for stage_name in STAGES[loss_index:]:
                if availability[stage_name] != "not_present_in_observed_set":
                    _fail(
                        f"trace.gold.availability.{stage_name}",
                        "a recorded collapse loss must remain not_present_in_observed_set",
                    )
        for candidate_id, candidate in candidates.items():
            carried_gold = gold_set.intersection(candidate["source_span_ids"])
            labeled_spans = support_spans[candidate_id]
            support_state = supports[candidate_id]
            if support_state in ("direct", "partial"):
                if not labeled_spans.issubset(gold_set):
                    _fail(
                        f"trace.candidates.{candidate_id}.support",
                        "supporting spans must be source-bound gold spans",
                    )
            elif carried_gold:
                _fail(
                    f"trace.candidates.{candidate_id}.support",
                    "a candidate carrying a gold span cannot be labeled non-support",
                )
    elif gold_spans or first_loss_stage is not None:
        _fail("trace.gold", "current-authority routing does not bind historical spans or loss")

    result = _object(value["result"], {"status", "candidate_ids", "detail"},
                     "trace.result")
    if result["status"] not in _RESULT_STATES:
        _fail("trace.result.status", f"must be one of {sorted(_RESULT_STATES)}")
    result_ids = _string_list(result["candidate_ids"], "trace.result.candidate_ids")
    if any(candidate_id not in set(outputs["render"]) for candidate_id in result_ids):
        _fail("trace.result.candidate_ids", "must refer to render-stage survivors")
    detail = _string(result["detail"], "trace.result.detail", nullable=True)
    if result["status"] in ("direct_historical_support", "partial_historical_support"):
        if not result_ids:
            _fail("trace.result", "support results require at least one rendered candidate")
    elif result_ids:
        _fail("trace.result", "non-support results cannot carry evidence candidates")
    if result["status"] == "no_supported_evidence_found" and detail is None:
        _fail("trace.result", "bounded no-evidence results require detail")

    result_status = result["status"]
    gold_state = gold["state"]
    if gold_state == "current_authority_required":
        if (result_status != "current_authority_required"
                or any(availability[key] != "not_applicable" for key in CHECKPOINTS)):
            _fail("trace", "current-authority gold requires a current-authority result")
    elif result_status == "current_authority_required":
        _fail("trace.result", "current-authority result requires current-authority gold")
    if gold_state == "gold_not_judged" and result_status != "not_evaluated":
        _fail("trace.result", "unjudged gold permits only not_evaluated")
    if gold_state == "gold_not_judged" and any(
            support != "not_judged" for support in supports.values()):
        _fail("trace.candidates", "unjudged gold requires unjudged support assessments")
    if result_status in ("direct_historical_support", "partial_historical_support"):
        if gold_state != "source_bound":
            _fail("trace.result", "historical support requires source-bound gold")
        allowed = ({"direct"} if result_status == "direct_historical_support"
                   else {"direct", "partial"})
        if any(supports[candidate_id] not in allowed for candidate_id in result_ids):
            _fail("trace.result.candidate_ids",
                  "result candidates do not satisfy their query support labels")
        if any(support_label_kinds[candidate_id] not in _TRUSTED_LABEL_KINDS
               for candidate_id in result_ids):
            _fail("trace.result.candidate_ids",
                  "historical support results require trusted label provenance")
        if (result_status == "partial_historical_support"
                and not any(supports[candidate_id] == "partial"
                            for candidate_id in result_ids)):
            _fail("trace.result.candidate_ids",
                  "partial results require a partially supporting candidate")
    if result_status == "no_supported_evidence_found" and any(
            supports[candidate_id] in ("direct", "partial")
            for candidate_id in outputs["render"]):
        _fail("trace.result", "no-evidence result contradicts rendered support labels")
    if result_status == "no_supported_evidence_found" and any(
            supports[candidate_id] == "not_judged"
            for candidate_id in outputs["render"]):
        _fail("trace.result", "no-evidence result cannot rely on unjudged survivors")

    if require_hash:
        supplied = _digest(value["trace_sha256"], "trace.trace_sha256")
        body = dict(value)
        del body["trace_sha256"]
        if canonical_sha256(body) != supplied:
            _fail("trace.trace_sha256", "canonical hash mismatch")


def validate_trace(record: object) -> None:
    _validate_json(record)
    _validate_trace(record, require_hash=True)


def seal_trace(record: Mapping[str, object]) -> dict:
    """Copy, validate, and hash an unsealed candidate-loss trace body."""
    body = copy.deepcopy(dict(record))
    body.pop("trace_sha256", None)
    _validate_json(body)
    _validate_trace(body, require_hash=False)
    body["trace_sha256"] = canonical_sha256(body)
    validate_trace(body)
    return body


def _validate_semantic_loss_stage(
        value: object, where: str, expected_stage: str,
        active: list[str], candidates: Mapping[str, Mapping[str, object]],
        *, h: int | None = None, output_limit: int | None = None) -> list[str]:
    stage = _object(value, {"stage", "state", "reason", "decisions"}, where)
    if stage["stage"] != expected_stage:
        _fail(f"{where}.stage", f"must equal {expected_stage!r}")
    if stage["state"] not in ("applied", "not_applied"):
        _fail(f"{where}.state", "must be applied or not_applied")
    _string(stage["reason"], f"{where}.reason")
    decisions = stage["decisions"]
    if not isinstance(decisions, list):
        _fail(f"{where}.decisions", "must be an array")
    ids = [item.get("candidate_id") if isinstance(item, Mapping) else None
           for item in decisions]
    if ids != active:
        _fail(f"{where}.decisions",
              "must account once, in order, for every active candidate")
    kept: list[str] = []
    parsed: list[tuple[str, str, str | None, str]] = []
    expected_keep_reason = {
        "eligibility_filter": "eligible",
        "session_max": "session_f16_max",
        "family_max": "family_f16_max",
        "output": "within_output_limit",
        "exact_copy": "unique_exact_copy",
        "session_top_h": "within_session_top_h",
        "family_top_h": "within_family_top_h",
    }.get(expected_stage)
    for index, item in enumerate(decisions):
        decision_where = f"{where}.decisions[{index}]"
        decision = _object(item, {
            "candidate_id", "decision", "reason_code", "retained_as",
        }, decision_where)
        candidate_id = _string(
            decision["candidate_id"], f"{decision_where}.candidate_id")
        assert candidate_id is not None
        if decision["decision"] not in ("kept", "dropped"):
            _fail(f"{decision_where}.decision", "must be kept or dropped")
        reason_code = _string(
            decision["reason_code"], f"{decision_where}.reason_code")
        retained_as = _string(
            decision["retained_as"], f"{decision_where}.retained_as",
            nullable=True)
        assert reason_code is not None
        if decision["decision"] == "kept":
            if retained_as != candidate_id:
                _fail(decision_where, "kept candidates must retain themselves")
            if stage["state"] == "applied" and reason_code != expected_keep_reason:
                _fail(decision_where, f"{expected_stage} keep reason is invalid")
            kept.append(candidate_id)
        parsed.append((candidate_id, decision["decision"], retained_as,
                       reason_code))

    if stage["state"] == "not_applied":
        if kept != active or any(reason != "not_applied"
                                 for _cid, _decision, _retained, reason in parsed):
            _fail(where, "not_applied stages must be strict pass-through")
        return kept

    kept_set = set(kept)
    for candidate_id, decision, retained_as, reason_code in parsed:
        if decision != "dropped":
            continue
        candidate = candidates[candidate_id]
        if expected_stage == "exact_copy":
            if retained_as not in kept_set:
                _fail(where, "exact-copy drops require a kept representative")
            if (candidate["exact_copy_sha256"]
                    != candidates[retained_as]["exact_copy_sha256"]):
                _fail(where, "exact-copy representative has different bytes")
            if reason_code != "exact_copy_of":
                _fail(where, "exact-copy drops require exact_copy_of")
        elif expected_stage == "session_max":
            if retained_as not in kept_set:
                _fail(where, "session-max drops require a kept representative")
            if (candidate["session_id"]
                    != candidates[retained_as]["session_id"]):
                _fail(where, "session-max representative has a different session")
            if reason_code != "lower_f16_within_session":
                _fail(where, "session-max drop reason is invalid")
        elif expected_stage == "family_max":
            if retained_as not in kept_set:
                _fail(where, "family-max drops require a kept representative")
            if (candidate["family_group"]
                    != candidates[retained_as]["family_group"]):
                _fail(where, "family-max representative has a different family")
            if reason_code != "lower_f16_within_family":
                _fail(where, "family-max drop reason is invalid")
        elif expected_stage in ("eligibility_filter", "output",
                                "session_top_h", "family_top_h"):
            if retained_as is not None:
                _fail(where, f"{expected_stage} drops cannot claim a representative")
            allowed_reason = {
                "eligibility_filter": {"excluded_family", "row_filter"},
                "output": {"outside_output_limit"},
                "session_top_h": {"outside_session_top_h"},
                "family_top_h": {"outside_family_top_h"},
            }[expected_stage]
            if reason_code not in allowed_reason:
                _fail(where, f"{expected_stage} drop reason is invalid")
        else:
            _fail(where, f"unsupported semantic loss stage {expected_stage!r}")

    # A forensic artifact proves the exact survivor set, not a count bound.
    # F16 score plus ordinal defines order, so first-by-key is the serving max
    # and first H is this schema's retention ablation.
    expected_kept: list[str] | None = None
    expected_representative: dict[str, str] = {}
    if expected_stage in ("exact_copy", "session_max", "family_max"):
        key_field = {
            "exact_copy": "exact_copy_sha256",
            "session_max": "session_id",
            "family_max": "family_group",
        }[expected_stage]
        first: dict[object, str] = {}
        expected_kept = []
        for candidate_id in active:
            key = candidates[candidate_id][key_field]
            representative = first.setdefault(key, candidate_id)
            if representative == candidate_id:
                expected_kept.append(candidate_id)
            else:
                expected_representative[candidate_id] = representative
    elif expected_stage == "output":
        if output_limit is None:
            _fail(where, "output stage requires its serving limit")
        expected_kept = active[:output_limit]
    elif expected_stage in ("session_top_h", "family_top_h"):
        if h is None:
            _fail(where, "top-H stage requires H")
        key_field = ("session_id" if expected_stage == "session_top_h"
                     else "family_group")
        counts: dict[object, int] = {}
        expected_kept = []
        for candidate_id in active:
            key = candidates[candidate_id][key_field]
            count = counts.get(key, 0)
            if count < h:
                expected_kept.append(candidate_id)
            counts[key] = count + 1
    if expected_kept is not None and kept != expected_kept:
        _fail(where, "does not retain the deterministic highest-ranked candidates")
    if expected_representative:
        for candidate_id, decision, retained_as, _reason_code in parsed:
            if (decision == "dropped"
                    and retained_as != expected_representative.get(candidate_id)):
                _fail(where, "drop does not bind the deterministic representative")

    if expected_stage == "exact_copy":
        exact_ids = [str(candidates[candidate_id]["exact_copy_sha256"])
                     for candidate_id in kept]
        if len(exact_ids) != len(set(exact_ids)):
            _fail(where, "exact-copy stage retained duplicate source bytes")
    if h is not None:
        exact_ids = [str(candidates[candidate_id]["exact_copy_sha256"])
                     for candidate_id in kept]
        source_ids = [str(candidates[candidate_id]["source_id"])
                      for candidate_id in kept]
        if (len(exact_ids) != len(set(exact_ids))
                or len(source_ids) != len(set(source_ids))):
            _fail(where, "top-H stages must retain unique source rows")
        if expected_stage == "session_top_h":
            keys = [str(candidates[candidate_id]["session_id"])
                    for candidate_id in kept]
        elif expected_stage == "family_top_h":
            keys = [str(candidates[candidate_id]["family_group"])
                    for candidate_id in kept]
        else:
            keys = []
        if keys and any(keys.count(key) > h for key in set(keys)):
            _fail(where, f"stage retained more than H={h} candidates per group")
    return kept


def validate_semantic_candidate_loss(
        record: object, *, expected_artifact_id: str | None = None,
        expected_retrieval_id: str | None = None) -> None:
    """Validate an unlabeled, observer-only q8/f16 candidate-loss snapshot.

    This record deliberately stops before support judgment.  Its H ablations
    expose candidate retention only; they cannot claim evidence quality or alter
    serving order.
    """
    _validate_json(record)
    value = _object(record, {
        "schema", "version", "artifact_id", "retrieval_id", "pipeline",
        "generation", "query_sha256", "effective_request", "score_kinds",
        "group_count", "candidates", "serving", "ablations",
    }, "semantic_candidate_loss")
    if (value["schema"] != SEMANTIC_CANDIDATE_LOSS_SCHEMA
            or value["version"] != SEMANTIC_CANDIDATE_LOSS_VERSION):
        _fail("semantic_candidate_loss", "unsupported schema or version")
    artifact_id = _string(
        value["artifact_id"], "semantic_candidate_loss.artifact_id")
    retrieval_id = _string(
        value["retrieval_id"], "semantic_candidate_loss.retrieval_id")
    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        _fail("semantic_candidate_loss.artifact_id", "does not match binding")
    if expected_retrieval_id is not None and retrieval_id != expected_retrieval_id:
        _fail("semantic_candidate_loss.retrieval_id", "does not match binding")
    if value["pipeline"] != "grouped_q8_f16":
        _fail("semantic_candidate_loss.pipeline", "must be grouped_q8_f16")
    _string(value["generation"], "semantic_candidate_loss.generation")
    query_sha256 = _digest(
        value["query_sha256"], "semantic_candidate_loss.query_sha256")
    validate_semantic_effective_request(value["effective_request"])
    if value["effective_request"]["query_sha256"] != query_sha256:
        _fail("semantic_candidate_loss.effective_request.query_sha256",
              "does not match the candidate-loss query")
    score_kinds = _object(
        value["score_kinds"], {"q8", "f16"},
        "semantic_candidate_loss.score_kinds")
    if score_kinds["q8"] != "cosine-q8-v1":
        _fail("semantic_candidate_loss.score_kinds.q8", "unexpected score semantics")
    if score_kinds["f16"] != "cosine-f16-v1":
        _fail("semantic_candidate_loss.score_kinds.f16", "unexpected score semantics")
    group_count = _integer(
        value["group_count"], "semantic_candidate_loss.group_count")
    assert group_count is not None

    if not isinstance(value["candidates"], list):
        _fail("semantic_candidate_loss.candidates", "must be an array")
    candidates: dict[str, Mapping[str, object]] = {}
    ordinals: set[int] = set()
    q8_ranks: list[int] = []
    f16_ranks: list[int] = []
    candidate_order: list[str] = []
    q8_order: list[tuple[int, float, int]] = []
    previous_f16: tuple[float, int] | None = None
    candidate_keys = {
        "candidate_id", "ordinal", "source_id", "session_id",
        "family_group", "turn", "who", "content_digest",
        "exact_copy_sha256", "q8", "f16",
    }
    for index, item in enumerate(value["candidates"]):
        where = f"semantic_candidate_loss.candidates[{index}]"
        candidate = _object(item, candidate_keys, where)
        candidate_id = _string(candidate["candidate_id"], f"{where}.candidate_id")
        assert candidate_id is not None
        if candidate_id in candidates:
            _fail("semantic_candidate_loss.candidates", "candidate ids must be unique")
        ordinal = _integer(candidate["ordinal"], f"{where}.ordinal")
        assert ordinal is not None
        if ordinal in ordinals:
            _fail("semantic_candidate_loss.candidates", "ordinals must be unique")
        ordinals.add(ordinal)
        _string(candidate["source_id"], f"{where}.source_id")
        _string(candidate["session_id"], f"{where}.session_id")
        family_group = _integer(candidate["family_group"], f"{where}.family_group")
        assert family_group is not None
        if family_group >= group_count:
            _fail(f"{where}.family_group", "is outside group_count")
        _integer(candidate["turn"], f"{where}.turn")
        _string(candidate["who"], f"{where}.who")
        _string(candidate["content_digest"], f"{where}.content_digest")
        _digest(candidate["exact_copy_sha256"], f"{where}.exact_copy_sha256")
        q8 = _object(candidate["q8"], {"rank", "score"}, f"{where}.q8")
        f16 = _object(candidate["f16"], {"rank", "score"}, f"{where}.f16")
        q8_rank = _integer(q8["rank"], f"{where}.q8.rank")
        f16_rank = _integer(f16["rank"], f"{where}.f16.rank")
        if q8_rank == 0 or f16_rank != index + 1:
            _fail(where, "q8 ranks are one-based and f16 rank must match array order")
        q8_score = _number(q8["score"], f"{where}.q8.score")
        f16_score = _number(f16["score"], f"{where}.f16.score")
        current_f16 = (-f16_score, ordinal)
        if previous_f16 is not None and current_f16 < previous_f16:
            _fail("semantic_candidate_loss.candidates",
                  "f16 order must be score-descending with ordinal ties")
        previous_f16 = current_f16
        q8_ranks.append(q8_rank)
        f16_ranks.append(f16_rank)
        q8_order.append((q8_rank, q8_score, ordinal))
        candidates[candidate_id] = candidate
        candidate_order.append(candidate_id)
    expected_ranks = list(range(1, len(candidates) + 1))
    if sorted(q8_ranks) != expected_ranks or f16_ranks != expected_ranks:
        _fail("semantic_candidate_loss.candidates", "q8/f16 ranks must be contiguous")
    ranked_q8 = sorted(q8_order)
    if any((-score, ordinal) < (-ranked_q8[index - 1][1],
                                ranked_q8[index - 1][2])
           for index, (_rank, score, ordinal) in enumerate(ranked_q8[1:], start=1)):
        _fail("semantic_candidate_loss.candidates",
              "q8 order must be score-descending with ordinal ties")

    serving = _object(value["serving"], {
        "limit", "stages", "output_candidate_ids",
    }, "semantic_candidate_loss.serving")
    limit = _integer(serving["limit"], "semantic_candidate_loss.serving.limit")
    assert limit is not None
    if not isinstance(serving["stages"], list) or len(serving["stages"]) != len(
            SEMANTIC_SERVING_STAGES):
        _fail("semantic_candidate_loss.serving.stages", "has the wrong stage count")
    active = list(candidate_order)
    serving_outputs: dict[str, list[str]] = {}
    for index, stage_name in enumerate(SEMANTIC_SERVING_STAGES):
        active = _validate_semantic_loss_stage(
            serving["stages"][index],
            f"semantic_candidate_loss.serving.stages[{index}]",
            stage_name, active, candidates,
            output_limit=(limit if stage_name == "output" else None))
        serving_outputs[stage_name] = list(active)
    output_ids = _string_list(
        serving["output_candidate_ids"],
        "semantic_candidate_loss.serving.output_candidate_ids")
    if output_ids != active or len(output_ids) > limit:
        _fail("semantic_candidate_loss.serving.output_candidate_ids",
              "must equal the bounded output-stage survivors")

    if not isinstance(value["ablations"], list):
        _fail("semantic_candidate_loss.ablations", "must be an array")
    hs = [item.get("h") if isinstance(item, Mapping) else None
          for item in value["ablations"]]
    if hs != list(SEMANTIC_CANDIDATE_LOSS_H):
        _fail("semantic_candidate_loss.ablations",
              f"must contain H values {list(SEMANTIC_CANDIDATE_LOSS_H)}")
    ablation_input = serving_outputs["eligibility_filter"]
    for index, item in enumerate(value["ablations"]):
        where = f"semantic_candidate_loss.ablations[{index}]"
        ablation = _object(item, {
            "h", "stages", "candidate_ids", "support_selection",
            "family_diversity",
        }, where)
        h = _integer(ablation["h"], f"{where}.h")
        assert h is not None
        if not isinstance(ablation["stages"], list) or len(ablation["stages"]) != len(
                SEMANTIC_ABLATION_STAGES):
            _fail(f"{where}.stages", "has the wrong stage count")
        active = list(ablation_input)
        for stage_index, stage_name in enumerate(SEMANTIC_ABLATION_STAGES):
            active = _validate_semantic_loss_stage(
                ablation["stages"][stage_index],
                f"{where}.stages[{stage_index}]", stage_name,
                active, candidates, h=h)
        candidate_ids = _string_list(ablation["candidate_ids"],
                                     f"{where}.candidate_ids")
        if candidate_ids != active:
            _fail(f"{where}.candidate_ids", "must equal retained candidates")
        support = _object(ablation["support_selection"], {"state", "reason"},
                          f"{where}.support_selection")
        if support["state"] != "not_run":
            _fail(f"{where}.support_selection.state", "must remain not_run")
        _string(support["reason"], f"{where}.support_selection.reason")
        diversity = _object(ablation["family_diversity"], {"state", "reason"},
                            f"{where}.family_diversity")
        if diversity["state"] != "deferred_after_support_selection":
            _fail(f"{where}.family_diversity.state",
                  "must remain deferred until support selection")
        _string(diversity["reason"], f"{where}.family_diversity.reason")
