"""Strict, pure-stdlib validation for frozen SABEL retrieval-pool documents.

The v1 observer exposes the rows returned by one serving semantic call.  That is
one semantic lane; its pool does **not** prove the q8 candidate set, the f16
rerank set, or an exhaustive dense scan unless a future observer emits those as
separate, explicitly named lane artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping


POOL_SCHEMA = "agrep.sabel.retrieval-pool.v1"
POOL_STAGES = frozenset({
    "run_query_return",
    "merged_pre_self",
    "post_self",
    "post_meta_sort",
    "final_selected",
})

_POOL_KEYS = {
    "schema", "artifact_id", "retrieval_id", "call_id", "lane", "stage",
    "ordered", "candidate_count", "duration_ns", "result_meta", "candidates",
}
_CANDIDATE_KEYS = {
    "candidate_id", "raw_rank", "duplicate_of_raw_rank", "lane",
    "lane_score", "session", "family", "evidence", "view", "exact_hashes",
    "raw_hit",
}
_LANE_SCORE_KEYS = {"value", "score", "sem_score", "score_kind", "matched"}
_FAMILY_KEYS = {"state", "root", "source"}
_EVIDENCE_KEYS = {
    "turn", "who", "ts", "kind", "event_kind", "name", "event_identity",
    "match_span", "content_digest",
}
_VIEW_KEYS = {"snippet", "summary", "title", "semantic_source"}
_HASH_KEYS = {
    "raw_hit_sha256", "source_bytes_sha256", "source_bytes_state",
}
_HEX = frozenset("0123456789abcdef")


class PoolDocumentError(ValueError):
    """A frozen retrieval pool is malformed or contradicts its binding."""


def _fail(where: str, message: str) -> None:
    raise PoolDocumentError(f"{where}: {message}")


def _object(value: object, keys: set[str], where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(where, "must be an object")
    actual = set(value)
    if actual != keys:
        _fail(where, f"keys mismatch (missing={sorted(keys - actual)}, "
              f"extra={sorted(actual - keys)})")
    return value


def _string(value: object, where: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(where, "must be a non-empty NUL-free string")
    return value


def _integer(value: object, where: str, *, nullable: bool = False,
             minimum: int = 0) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < minimum:
        _fail(where, f"must be an integer >= {minimum}")
    return value


def _number(value: object, where: str, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "must be a finite number")
    if not math.isfinite(float(value)):
        _fail(where, "must be a finite number")
    return value


def _digest(value: object, where: str, *, nullable: bool = False) -> str | None:
    text = _string(value, where, nullable=nullable)
    if text is None:
        return None
    if len(text) != 64 or any(character not in _HEX for character in text):
        _fail(where, "must be a lowercase SHA-256 digest")
    return text


def _validate_json(value: object, where: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(where, "contains a non-finite number")
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
    _fail(where, f"contains unsupported value {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    _validate_json(value, "pool")
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_candidate(value: object, index: int, lane: str, stage: str,
                        previous: list[dict]) -> dict:
    where = f"pool.candidates[{index}]"
    candidate = _object(value, _CANDIDATE_KEYS, where)
    candidate_id = _string(candidate["candidate_id"], f"{where}.candidate_id")
    assert candidate_id is not None
    rank = _integer(candidate["raw_rank"], f"{where}.raw_rank", minimum=1)
    assert rank is not None
    if rank != index + 1:
        _fail(f"{where}.raw_rank", "must equal its contiguous one-based array rank")
    duplicate = _integer(
        candidate["duplicate_of_raw_rank"], f"{where}.duplicate_of_raw_rank",
        nullable=True, minimum=1)
    if duplicate is not None:
        if duplicate >= rank:
            _fail(f"{where}.duplicate_of_raw_rank", "must name an earlier rank")
        if previous[duplicate - 1]["candidate_id"] != candidate_id:
            _fail(f"{where}.duplicate_of_raw_rank",
                  "must name an earlier row with the same candidate_id")
    elif any(row["candidate_id"] == candidate_id for row in previous):
        _fail(f"{where}.duplicate_of_raw_rank",
              "repeated candidate_id must name its first rank")

    candidate_lane = _string(candidate["lane"], f"{where}.lane")
    assert candidate_lane is not None
    if stage == "run_query_return" and candidate_lane != lane:
        _fail(f"{where}.lane", "run_query rows must match the self-bound pool lane")
    lane_score = _object(candidate["lane_score"], _LANE_SCORE_KEYS,
                         f"{where}.lane_score")
    score = _number(lane_score["value"], f"{where}.lane_score.value")
    score_kind = _string(
        lane_score["score_kind"], f"{where}.lane_score.score_kind")
    _number(lane_score["score"], f"{where}.lane_score.score", nullable=True)
    _number(lane_score["sem_score"], f"{where}.lane_score.sem_score", nullable=True)
    _string(lane_score["matched"], f"{where}.lane_score.matched", nullable=True)
    _string(candidate["session"], f"{where}.session", nullable=True)

    family = _object(candidate["family"], _FAMILY_KEYS, f"{where}.family")
    family_state = family["state"]
    if family_state not in ("captured", "unavailable"):
        _fail(f"{where}.family.state", "must be captured or unavailable")
    family_root = _string(
        family["root"], f"{where}.family.root", nullable=True)
    _string(family["source"], f"{where}.family.source")
    if (family_state == "captured") != (family_root is not None):
        _fail(f"{where}.family", "captured family requires a root; unavailable forbids one")

    evidence = _object(candidate["evidence"], _EVIDENCE_KEYS, f"{where}.evidence")
    _integer(evidence["turn"], f"{where}.evidence.turn", nullable=True, minimum=0)
    _number(evidence["ts"], f"{where}.evidence.ts", nullable=True)
    for key in ("who", "kind", "event_kind", "name", "event_identity",
                "content_digest"):
        _string(evidence[key], f"{where}.evidence.{key}", nullable=True)
    match_span = evidence["match_span"]
    if match_span is not None:
        if (not isinstance(match_span, list) or len(match_span) != 2):
            _fail(f"{where}.evidence.match_span", "must be null or [start,end]")
        start = _integer(match_span[0], f"{where}.evidence.match_span[0]", minimum=0)
        end = _integer(match_span[1], f"{where}.evidence.match_span[1]", minimum=0)
        if end is None or start is None or end <= start:
            _fail(f"{where}.evidence.match_span", "must be non-empty and ordered")

    view = _object(candidate["view"], _VIEW_KEYS, f"{where}.view")
    for key in _VIEW_KEYS:
        _string(view[key], f"{where}.view.{key}", nullable=True)
    hashes = _object(candidate["exact_hashes"], _HASH_KEYS,
                     f"{where}.exact_hashes")
    raw_sha = _digest(hashes["raw_hit_sha256"],
                      f"{where}.exact_hashes.raw_hit_sha256")
    source_sha = _digest(
        hashes["source_bytes_sha256"],
        f"{where}.exact_hashes.source_bytes_sha256", nullable=True)
    source_state = _string(
        hashes["source_bytes_state"], f"{where}.exact_hashes.source_bytes_state")
    assert raw_sha is not None and source_state is not None
    if source_sha is None and not source_state.startswith("unavailable"):
        _fail(f"{where}.exact_hashes",
              "missing source hash must remain explicitly unavailable")
    if source_sha is not None and source_state != "captured":
        _fail(f"{where}.exact_hashes",
              "a source hash may only be claimed as captured")
    if not isinstance(candidate["raw_hit"], Mapping):
        _fail(f"{where}.raw_hit", "must be an object")
    _validate_json(candidate["raw_hit"], f"{where}.raw_hit")
    if canonical_sha256(candidate["raw_hit"]) != raw_sha:
        _fail(f"{where}.exact_hashes.raw_hit_sha256",
              "does not bind the canonical raw_hit bytes")
    assert score is not None and score_kind is not None
    return {
        "candidate_id": candidate_id,
        "rank": rank,
        "score": score,
        "score_kind": score_kind,
        "lane": candidate_lane,
    }


def validate_pool_document(
        value: object, *, expected_artifact_id: str | None = None,
        expected_retrieval_id: str | None = None,
        expected_lane: str | None = None,
        expected_stage: str | None = None) -> dict:
    """Validate one decoded canonical pool and return its comparison projection."""
    pool = _object(value, _POOL_KEYS, "pool")
    if pool["schema"] != POOL_SCHEMA:
        _fail("pool.schema", f"must equal {POOL_SCHEMA!r}")
    artifact_id = _string(pool["artifact_id"], "pool.artifact_id")
    retrieval_id = _string(pool["retrieval_id"], "pool.retrieval_id")
    _string(pool["call_id"], "pool.call_id", nullable=True)
    lane = _string(pool["lane"], "pool.lane")
    stage = _string(pool["stage"], "pool.stage")
    assert artifact_id is not None and retrieval_id is not None
    assert lane is not None and stage is not None
    if stage not in POOL_STAGES:
        _fail("pool.stage", f"must be one of {sorted(POOL_STAGES)}")
    for observed, expected, where in (
            (artifact_id, expected_artifact_id, "pool.artifact_id"),
            (retrieval_id, expected_retrieval_id, "pool.retrieval_id"),
            (lane, expected_lane, "pool.lane"),
            (stage, expected_stage, "pool.stage")):
        if expected is not None and observed != expected:
            _fail(where, f"does not match sealed binding {expected!r}")
    if pool["ordered"] is not True:
        _fail("pool.ordered", "must be true")
    count = _integer(pool["candidate_count"], "pool.candidate_count", minimum=0)
    _integer(pool["duration_ns"], "pool.duration_ns", nullable=True, minimum=0)
    if not isinstance(pool["result_meta"], Mapping):
        _fail("pool.result_meta", "must be an object")
    _validate_json(pool["result_meta"], "pool.result_meta")
    if not isinstance(pool["candidates"], list):
        _fail("pool.candidates", "must be an array")
    assert count is not None
    if len(pool["candidates"]) != count:
        _fail("pool.candidate_count", "must equal the ordered candidates length")
    parsed: list[dict] = []
    for index, candidate in enumerate(pool["candidates"]):
        parsed.append(_validate_candidate(candidate, index, lane, stage, parsed))
    return {
        "record": pool,
        "artifact_id": artifact_id,
        "retrieval_id": retrieval_id,
        "lane": lane,
        "stage": stage,
        "candidate_count": count,
        "candidates": parsed,
    }


def parse_pool_document(
        data: bytes, *, expected_artifact_id: str | None = None,
        expected_retrieval_id: str | None = None,
        expected_lane: str | None = None,
        expected_stage: str | None = None) -> dict:
    """Strictly decode, canonicalize, and validate one frozen pool artifact."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PoolDocumentError("pool: is not UTF-8 JSON") from error

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("pool", f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value: str):
        _fail("pool", f"non-finite JSON number {value!r} is forbidden")

    try:
        value = json.loads(
            text, object_pairs_hook=unique_pairs,
            parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise PoolDocumentError(f"pool: invalid JSON: {error.msg}") from error
    parsed = validate_pool_document(
        value, expected_artifact_id=expected_artifact_id,
        expected_retrieval_id=expected_retrieval_id,
        expected_lane=expected_lane, expected_stage=expected_stage)
    if data != canonical_bytes(value):
        _fail("pool", "bytes are not the canonical frozen JSON encoding")
    return parsed
