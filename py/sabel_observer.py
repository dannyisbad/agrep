"""Opt-in, shadow-only retrieval trace observer.

The observer is deliberately outside the retrieval contract.  With
``AGREP_SABEL_TRACE_DIR`` unset every public entry point returns before touching
the filesystem.  With it set, failures are swallowed by the ``safe_*`` API so
diagnostics can never change a recall/around result.

Each invocation publishes immutable artifacts first and ``manifest.json`` last.
Readers reject missing manifests, unlisted files, and any descriptor whose bytes
do not match its recorded SHA-256 and size.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import sabel_pool
import sabel_shadow


TRACE_ENV = "AGREP_SABEL_TRACE_DIR"
MANIFEST_SCHEMA = "agrep.sabel.retrieval-observer-manifest.v1"
POOL_SCHEMA = sabel_pool.POOL_SCHEMA
WINDOW_SCHEMA = "agrep.sabel.window-pool.v1"
CANDIDATE_LOSS_SCHEMA = sabel_shadow.SEMANTIC_CANDIDATE_LOSS_SCHEMA
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_ARTIFACTS = 4096
MAX_TOTAL_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024

_MANIFEST_KEYS = {
    "schema", "version", "run_id", "retrieval_id", "command",
    "started_wall_ns", "ended_wall_ns", "duration_ns", "exit_code", "error",
    "complete", "metadata", "lane_states", "returned_handles",
    "opened_handle", "stdout_artifact_id", "artifacts", "events",
}
_DESCRIPTOR_KEYS = {
    "artifact_id", "kind", "stage", "sequence", "path", "media_type",
    "schema", "size", "sha256",
}
_ARTIFACT_SCHEMAS = {
    "argv": "agrep.sabel.argv.v1",
    "query": "agrep.sabel.query.v1",
    "search-call": "agrep.sabel.search-call.v1",
    "candidate-loss": CANDIDATE_LOSS_SCHEMA,
    "pool": POOL_SCHEMA,
    "windows": WINDOW_SCHEMA,
    "around-source": "agrep.sabel.around-source.v1",
    "stdout": None,
}
_ARTIFACT_STAGES = {
    "argv": {"invocation"},
    "query": {"query"},
    "search-call": {"run_query_return"},
    "candidate-loss": {"semantic_candidate_loss"},
    "pool": set(sabel_pool.POOL_STAGES),
    "windows": {"hydrated_windows", "expanded_windows"},
    "around-source": {"around_opened_source"},
    "stdout": {"rendered_stdout"},
}
_LANE_STATES = {"captured", "not_run", "unavailable", "failed"}
_EVENT_KEYS = {
    "query": {"artifact_id", "query_count"},
    "pool": {"artifact_id", "call_id", "lane", "candidate_count"},
    "search_call": {
        "lane", "call_id", "state", "reason", "query_artifact_id",
        "pool_artifact_id",
    },
    "candidate_loss": {"artifact_id", "pipeline", "candidate_count"},
    "windows": {"artifact_id", "window_count"},
    "outcome": {"outcome", "code", "reason"},
    "around_open": {
        "artifact_id", "requested_handle", "session", "requested_turn",
        "served_turn",
    },
}
_EVENT_BASE_KEYS = {"sequence", "kind", "stage", "monotonic_offset_ns"}
_CALL_KEYS = {
    "call_id", "state", "reason", "query_artifact_id", "pool_artifact_id",
}
_SEARCH_CALL_KEYS = {
    "schema", "artifact_id", "retrieval_id", "call_id", "lane", "state",
    "reason", "query", "kwargs", "duration_ns", "error", "result_meta",
    "semantic_pipeline",
}
_SEMANTIC_PIPELINE_KEYS = {
    "effective_request", "q8_candidates", "f16_rerank",
    "exhaustive_f16_diagnostic",
}
_SEMANTIC_PIPELINE_STAGE_KEYS = {
    "state", "execution_observed", "coverage", "reason", "artifact_id",
}
_ARGV_KEYS = {"schema", "artifact_id", "command", "argv", "process_argv"}
_QUERY_KEYS = {
    "schema", "artifact_id", "retrieval_id", "raw_queries", "queries",
    "surface", "semantic_requested", "lexical_only",
}
_WINDOW_KEYS = {
    "schema", "artifact_id", "retrieval_id", "stage", "ordered",
    "window_count", "pairs",
}
_AROUND_SOURCE_KEYS = {
    "schema", "artifact_id", "retrieval_id", "requested_handle", "session",
    "requested_turn", "served_turn", "window",
}
_HEX = frozenset("0123456789abcdef")


class ObserverTraceError(RuntimeError):
    """An opt-in trace failed without changing the observed command."""


class ObserverBoundsError(ObserverTraceError):
    """A trace was abandoned before publishing out-of-contract bytes."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _decode_json(payload: bytes, where: str) -> object:
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str):
        raise ValueError(f"non-finite JSON number {value}")

    try:
        return json.loads(
            payload, object_pairs_hook=unique_pairs,
            parse_constant=reject_constant)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is invalid JSON: {exc}") from exc


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        # Retrieval rows are expected to be JSON-safe.  Preserve an unexpected
        # byte field by identity, not by a lossy decode.
        return {"bytes_hex": value.hex(), "size": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    # SpeakerFilter and other bounded query values are not part of the result
    # pool.  Their exact Python type and stable repr retain more truth than
    # silently dropping them.
    return {"python_type": type(value).__qualname__, "repr": repr(value)}


def _speaker_filter_parts(value: object) -> tuple[object, object]:
    if hasattr(value, "include") and hasattr(value, "exclude"):
        return value.include, value.exclude
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    raise ValueError("semantic who filter has an unsupported shape")


def _semantic_effective_request_from_call(
        query: str, kwargs: dict) -> dict:
    """Mirror the bounded request that ``search._semantic_local`` sends."""
    if not isinstance(kwargs, dict):
        raise ValueError("semantic search-call kwargs must be an object")
    maximum = sabel_shadow.SEMANTIC_EFFECTIVE_REQUEST_MAX_RESULTS
    session_limit = kwargs.get("session_limit")
    requested = (session_limit if session_limit is not None
                 else kwargs.get("limit", 40))
    requested = int(requested)
    if (bool(kwargs.get("exhaustive")) or requested == 0
            or kwargs.get("sort", "score") == "time"):
        semantic_k = maximum
    else:
        semantic_k = min(requested or 40, maximum)
    semantic_k = min(max(0, int(semantic_k)), maximum)
    who = kwargs.get("who")
    fetch_k = (maximum if semantic_k == 0 else
               semantic_k if who is not None else
               min(maximum, max(semantic_k * 4, 40)))

    filters = {key: value for key, value in {
        "agent": kwargs.get("agent"),
        "project": kwargs.get("project"),
        "exclude_project": kwargs.get("exclude_project"),
        "model": kwargs.get("model"),
        "chat": kwargs.get("chat"),
        "since_ms": kwargs.get("since_ms"),
        "until_ms": kwargs.get("until_ms"),
    }.items() if value is not None and value != ""}
    if isinstance(who, str):
        filters["who"] = who
    elif who is None:
        filters["_exclude_who"] = tuple(sorted(
            sabel_shadow.SEMANTIC_EFFECTIVE_REQUEST_DEFAULT_EXCLUDED_ROLES))
    else:
        include, exclude = _speaker_filter_parts(who)
        if include is not None:
            filters["_include_who"] = tuple(include)
        if exclude:
            filters["_exclude_who"] = tuple(exclude)
    exclude_session = kwargs.get("exclude_session")
    if exclude_session:
        filters["exclude_session"] = exclude_session
    boundary = kwargs.get("exclude_session_from_turn")
    if boundary is not None:
        filters["exclude_session_from_turn"] = int(boundary)
    excluded_sessions = kwargs.get("_exclude_sessions")
    if excluded_sessions:
        filters["_exclude_sessions"] = tuple(excluded_sessions)
    filters["exclude_family"] = bool(kwargs.get("exclude_family", True))
    model = kwargs.get("model")
    if model and bool(kwargs.get("model_soft")):
        filters["model_soft"] = True
    family_diverse = kwargs.get("family_diverse")
    filters["_family_diverse"] = (
        True if family_diverse is None else bool(family_diverse))
    return sabel_shadow.semantic_effective_request(
        query, "hybrid", fetch_k, filters)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        # Windows cannot open directories with os.open; atomic replacement is
        # still the publication boundary there.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stable_candidate_identity(hit: dict) -> dict:
    identity = {
        "session": hit.get("session"),
        "turn": hit.get("turn"),
        "who": hit.get("who"),
        "ts": hit.get("ts"),
        "kind": hit.get("kind"),
        "event_kind": hit.get("event_kind"),
        "name": hit.get("name"),
        "event_identity": hit.get("_event_identity"),
        "match_span": hit.get("_match_span"),
        "content_digest": hit.get("content_digest"),
    }
    # Old/fallback rows can lack the source digest.  Bind the identifier to the
    # exact exposed view in that case and label source bytes unavailable below.
    if not identity["content_digest"]:
        identity["fallback_view_sha256"] = _sha256(_canonical_json({
            "snippet": hit.get("snippet"),
            "summary": hit.get("summary"),
            "title": hit.get("title"),
        }))
    return identity


def _candidate_record(hit: dict, raw_rank: int, lane: str,
                      first_rank: dict[str, int]) -> dict:
    raw = _jsonable(hit)
    raw_bytes = _canonical_json(raw)
    identity = _stable_candidate_identity(hit)
    candidate_id = "cand-" + _sha256(_canonical_json(identity))
    family_root = hit.get("_family_root")
    if isinstance(family_root, str) and family_root:
        family = {"state": "captured", "root": family_root,
                  "source": "run_query_hit._family_root"}
    else:
        family = {"state": "unavailable", "root": None,
                  "source": "not_exposed_on_hit"}
    semantic = lane == "semantic" or hit.get("lane") == "semantic"
    lane_score = hit.get("sem_score") if semantic else hit.get("score")
    score_kind = hit.get("score_kind")
    if not isinstance(score_kind, str) or not score_kind:
        score_kind = "cosine" if semantic else "agrep-keyword-score-v1"
    first = first_rank.setdefault(candidate_id, raw_rank)
    return {
        "candidate_id": candidate_id,
        "raw_rank": raw_rank,
        "duplicate_of_raw_rank": None if first == raw_rank else first,
        "lane": str(hit.get("lane") or lane),
        "lane_score": {
            "value": lane_score,
            "score": hit.get("score"),
            "sem_score": hit.get("sem_score"),
            "score_kind": score_kind,
            "matched": hit.get("matched"),
        },
        "session": hit.get("session"),
        "family": family,
        "evidence": {
            "turn": hit.get("turn"),
            "who": hit.get("who"),
            "ts": hit.get("ts"),
            "kind": hit.get("kind"),
            "event_kind": hit.get("event_kind"),
            "name": hit.get("name"),
            "event_identity": hit.get("_event_identity"),
            "match_span": hit.get("_match_span"),
            "content_digest": hit.get("content_digest"),
        },
        "view": {
            "snippet": hit.get("snippet"),
            "summary": hit.get("summary"),
            "title": hit.get("title"),
            "semantic_source": hit.get("semantic_source"),
        },
        "exact_hashes": {
            "raw_hit_sha256": _sha256(raw_bytes),
            "source_bytes_sha256": None,
            "source_bytes_state": "unavailable_at_v1_run_query_boundary",
        },
        "raw_hit": raw,
    }


class _Trace:
    def __init__(self, root: Path, command: str, argv: list[str]):
        now_ns = time.time_ns()
        self.run_id = (
            f"{now_ns:019d}-{os.getpid()}-{secrets.token_hex(6)}")
        self.retrieval_id = "retrieval-" + self.run_id
        self.root = root
        self.path = root / self.run_id
        self.path.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.command = command
        self.argv = list(argv)
        self.started_wall_ns = now_ns
        self.started_mono_ns = time.monotonic_ns()
        self.sequence = 0
        self.call_sequence = 0
        self.artifacts: list[tuple[dict, bytes]] = []
        self.artifact_bytes = 0
        self.events: list[dict] = []
        self.metadata: dict[str, object] = {}
        self.stdout = bytearray()
        self.returned_handles: list[str] = []
        self.opened_handle: str | None = None
        self.lane_calls: dict[str, list[dict]] = {}
        self.lane_not_run_reasons: dict[str, str] = {}
        self.fatal_record_error: Exception | None = None
        self.closed = False
        self.lock = threading.RLock()
        self._add_json_artifact(
            "argv", "agrep.sabel.argv.v1",
            {"command": command, "argv": self.argv,
             "process_argv": list(sys.argv)}, stage="invocation")

    def _next_artifact_id(self, kind: str) -> str:
        self.sequence += 1
        return f"{self.retrieval_id}:artifact:{self.sequence:04d}:{kind}"

    def _add_artifact(self, kind: str, payload: bytes, *, media_type: str,
                      schema: str | None, stage: str,
                      artifact_id: str | None = None) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("observer artifact payload must be bytes")
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ObserverBoundsError(
                "observer artifact exceeds its per-file bound")
        if len(self.artifacts) >= MAX_ARTIFACTS:
            raise ObserverBoundsError("observer artifact count exceeds its bound")
        if self.artifact_bytes + len(payload) > MAX_TOTAL_ARTIFACT_BYTES:
            raise ObserverBoundsError("observer artifact bytes exceed their bound")
        artifact_id = artifact_id or self._next_artifact_id(kind)
        suffix = ".json" if media_type == "application/json" else ".bin"
        filename = f"{self.sequence:04d}-{kind}{suffix}"
        descriptor = {
            "artifact_id": artifact_id,
            "kind": kind,
            "stage": stage,
            "sequence": self.sequence,
            "path": filename,
            "media_type": media_type,
            "schema": schema,
            "size": len(payload),
            "sha256": _sha256(payload),
        }
        self.artifacts.append((descriptor, payload))
        self.artifact_bytes += len(payload)
        return artifact_id

    def add_stdout(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("observer stdout payload must be bytes")
        if len(self.stdout) + len(payload) > MAX_ARTIFACT_BYTES:
            raise ObserverBoundsError(
                "observer stdout exceeds its per-file bound")
        if (self.artifact_bytes + len(self.stdout) + len(payload)
                > MAX_TOTAL_ARTIFACT_BYTES):
            raise ObserverBoundsError("observer artifact bytes exceed their bound")
        self.stdout.extend(payload)

    def _add_json_artifact(self, kind: str, schema: str, body: dict, *,
                           stage: str) -> str:
        artifact_id = self._next_artifact_id(kind)
        document = {"schema": schema, "artifact_id": artifact_id, **body}
        payload = _canonical_json(document)
        return self._add_artifact(
            kind, payload, media_type="application/json", schema=schema,
            stage=stage, artifact_id=artifact_id)

    def add_event(self, kind: str, stage: str, **fields: object) -> None:
        self.events.append({
            "sequence": len(self.events) + 1,
            "kind": kind,
            "stage": stage,
            "monotonic_offset_ns": time.monotonic_ns() - self.started_mono_ns,
            **_jsonable(fields),
        })

    def add_pool(self, stage: str, hits: list[dict], *, lane: str,
                 call_id: str | None = None, result_meta: dict | None = None,
                 duration_ns: int | None = None) -> str:
        first_rank: dict[str, int] = {}
        candidates = [
            _candidate_record(hit, rank, lane, first_rank)
            for rank, hit in enumerate(hits, start=1)
        ]
        artifact_id = self._next_artifact_id("pool")
        document = _jsonable({
            "schema": POOL_SCHEMA,
            "artifact_id": artifact_id,
            "retrieval_id": self.retrieval_id,
            "call_id": call_id,
            "lane": lane,
            "stage": stage,
            "ordered": True,
            "candidate_count": len(candidates),
            "duration_ns": duration_ns,
            "result_meta": result_meta or {},
            "candidates": candidates,
        })
        if not isinstance(document, dict):
            raise TypeError("normalized observer pool must be an object")
        # Pool documents are a public evidence seam, not merely diagnostic
        # JSON.  Refuse to publish an observer generation whose frozen pool is
        # malformed or does not bind its own artifact/retrieval/lane/stage.
        sabel_pool.validate_pool_document(
            document, expected_artifact_id=artifact_id,
            expected_retrieval_id=self.retrieval_id,
            expected_lane=lane, expected_stage=stage)
        payload = sabel_pool.canonical_bytes(document)
        self._add_artifact(
            "pool", payload, media_type="application/json",
            schema=POOL_SCHEMA, stage=stage, artifact_id=artifact_id)
        self.add_event(
            "pool", stage, artifact_id=artifact_id, call_id=call_id,
            lane=lane, candidate_count=len(candidates))
        return artifact_id

    def add_candidate_loss(
            self, capture: dict, *, effective_request: dict) -> tuple[str, int]:
        if not isinstance(capture, dict) or set(capture) != {"state", "record"}:
            raise ValueError("captured candidate-loss envelope is malformed")
        if capture["state"] != "captured" or not isinstance(capture["record"], dict):
            raise ValueError("captured candidate-loss envelope is contradictory")
        body = dict(capture["record"])
        if "artifact_id" in body or "retrieval_id" in body:
            raise ValueError("candidate-loss body cannot preclaim observer bindings")
        sabel_shadow.validate_semantic_effective_request(effective_request)
        if body.get("query_sha256") != effective_request["query_sha256"]:
            raise ValueError("candidate-loss query does not match its search call")
        if body.get("effective_request") != effective_request:
            raise ValueError(
                "candidate-loss request does not match its search call")
        artifact_id = self._next_artifact_id("candidate-loss")
        document = {
            **body,
            "artifact_id": artifact_id,
            "retrieval_id": self.retrieval_id,
        }
        sabel_shadow.validate_semantic_candidate_loss(
            document, expected_artifact_id=artifact_id,
            expected_retrieval_id=self.retrieval_id)
        payload = sabel_shadow.canonical_bytes(document)
        self._add_artifact(
            "candidate-loss", payload, media_type="application/json",
            schema=CANDIDATE_LOSS_SCHEMA, stage="semantic_candidate_loss",
            artifact_id=artifact_id)
        count = len(document["candidates"])
        self.add_event(
            "candidate_loss", "semantic_candidate_loss",
            artifact_id=artifact_id, pipeline=document["pipeline"],
            candidate_count=count)
        return artifact_id, count

    def add_search_call(self, query: str, mode: str, kwargs: dict,
                        result: dict | None, duration_ns: int,
                        error: BaseException | None) -> None:
        self.call_sequence += 1
        call_id = f"{self.retrieval_id}:call:{self.call_sequence:04d}"
        status = _lane_call_state(mode, result, error)
        result_meta = ({key: value for key, value in (result or {}).items()
                        if key not in {
                            "hits", sabel_shadow.SEMANTIC_CANDIDATE_LOSS_FIELD,
                        }})
        pipeline = None
        if mode == "semantic":
            effective_request = _semantic_effective_request_from_call(
                query, kwargs)
            accelerator = (result or {}).get("semantic_accelerator_coverage")
            capture = (result or {}).get(
                sabel_shadow.SEMANTIC_CANDIDATE_LOSS_FIELD)
            candidate_loss_id = None
            capture_reason = None
            if isinstance(capture, dict) and capture.get("state") == "captured":
                candidate_loss_id, _candidate_count = self.add_candidate_loss(
                    capture, effective_request=effective_request)
            elif isinstance(capture, dict) and capture.get("state") == "unavailable":
                if set(capture) != {"state", "reason"}:
                    raise ValueError("unavailable candidate-loss envelope is malformed")
                capture_reason = capture.get("reason")
                if not isinstance(capture_reason, str) or not capture_reason:
                    raise ValueError("unavailable candidate-loss reason is missing")
            unobserved = {
                "state": "unavailable",
                "execution_observed": False,
                "coverage": _jsonable(accelerator),
                "reason": (capture_reason or
                           "serving result exposed no grouped q8/f16 trace"),
                "artifact_id": None,
            }
            observed_coverage = _jsonable(accelerator)
            if candidate_loss_id is not None:
                if not isinstance(observed_coverage, dict):
                    raise ValueError(
                        "captured candidate-loss has no accelerator coverage")
                observed_coverage = {
                    **observed_coverage,
                    "generation": capture["record"]["generation"],
                }
            observed = {
                "state": "captured",
                "execution_observed": True,
                "coverage": observed_coverage,
                "reason": None,
                "artifact_id": candidate_loss_id,
            }
            pipeline = {
                "effective_request": effective_request,
                "q8_candidates": dict(observed if candidate_loss_id else unobserved),
                "f16_rerank": dict(observed if candidate_loss_id else unobserved),
                "exhaustive_f16_diagnostic": {
                    "state": "not_run", "execution_observed": False,
                    "reason": "v1 shadow observer never invokes a diagnostic lane",
                    "artifact_id": None, "coverage": None,
                },
            }
        query_artifact = self._add_json_artifact(
            "search-call", "agrep.sabel.search-call.v1", {
                "retrieval_id": self.retrieval_id,
                "call_id": call_id,
                "lane": mode,
                "state": status["state"],
                "reason": status.get("reason"),
                "query": query,
                "kwargs": kwargs,
                "duration_ns": duration_ns,
                "error": ({"type": type(error).__qualname__,
                           "message": str(error)} if error else None),
                "result_meta": result_meta,
                "semantic_pipeline": pipeline,
            }, stage="run_query_return")
        pool_id = None
        if isinstance(result, dict):
            pool_id = self.add_pool(
                "run_query_return", list(result.get("hits") or []), lane=mode,
                call_id=call_id, result_meta=result_meta,
                duration_ns=duration_ns)
        call = {
            "call_id": call_id, "state": status["state"],
            "reason": status.get("reason"), "query_artifact_id": query_artifact,
            "pool_artifact_id": pool_id,
        }
        self.lane_calls.setdefault(mode, []).append(call)
        self.add_event("search_call", "run_query_return", lane=mode, **call)

    def finish(self, exit_code: int | None,
               error: BaseException | None) -> Path:
        with self.lock:
            if self.closed:
                raise RuntimeError("observer trace is already closed")
            self.closed = True
            if self.fatal_record_error is not None:
                raise self.fatal_record_error
            stdout_id = self._add_artifact(
                "stdout", bytes(self.stdout), media_type="application/octet-stream",
                schema=None, stage="rendered_stdout")
            lane_states = self._lane_states()
            ended_wall_ns = time.time_ns()
            ended_mono_ns = time.monotonic_ns()
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "version": 1,
                "run_id": self.run_id,
                "retrieval_id": self.retrieval_id,
                "command": self.command,
                "started_wall_ns": self.started_wall_ns,
                "ended_wall_ns": ended_wall_ns,
                "duration_ns": ended_mono_ns - self.started_mono_ns,
                "exit_code": exit_code,
                "error": ({"type": type(error).__qualname__,
                           "message": str(error)} if error else None),
                "complete": error is None,
                "metadata": _jsonable(self.metadata),
                "lane_states": lane_states,
                "returned_handles": list(self.returned_handles),
                "opened_handle": self.opened_handle,
                "stdout_artifact_id": stdout_id,
                "artifacts": [descriptor for descriptor, _payload in self.artifacts],
                "events": self.events,
            }
            artifact_payloads = {
                descriptor["artifact_id"]: payload
                for descriptor, payload in self.artifacts
            }
            _validate_manifest_envelope(manifest, expected_run_name=self.path.name)
            _validate_manifest_topology(manifest, artifact_payloads)
            manifest_bytes = _canonical_json(manifest)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise ObserverBoundsError("observer manifest exceeds its bound")
            # Write every content artifact atomically before publishing the
            # already reader-validated manifest commit marker.
            for descriptor, payload in self.artifacts:
                _atomic_write(self.path / str(descriptor["path"]), payload)
            _fsync_dir(self.path)
            _atomic_write(self.path / "manifest.json", manifest_bytes)
            _fsync_dir(self.path)
            return self.path

    def _lane_states(self) -> dict[str, dict]:
        lanes = set(self.lane_calls) | {"keyword", "semantic"}
        out: dict[str, dict] = {}
        for lane in sorted(lanes):
            calls = self.lane_calls.get(lane, [])
            if not calls:
                out[lane] = {
                    "state": "not_run", "calls": [], "partial": False,
                    "reason": self.lane_not_run_reasons.get(
                        lane, "no search.run_query call was observed"),
                }
                continue
            states = [str(call["state"]) for call in calls]
            if "failed" in states:
                state = "failed"
            elif "unavailable" in states:
                state = "unavailable"
            elif all(item == "not_run" for item in states):
                state = "not_run"
            else:
                state = "captured"
            out[lane] = {
                "state": state, "calls": calls,
                "partial": len(set(states)) > 1,
                "reason": None,
            }
        return out


def _lane_call_state(mode: str, result: dict | None,
                     error: BaseException | None) -> dict[str, str]:
    if error is not None:
        return {"state": "failed", "reason": type(error).__qualname__}
    if result is None:
        return {"state": "unavailable", "reason": "run_query returned None"}
    if mode != "semantic":
        return {"state": "captured", "reason": "run_query returned a result"}
    status = result.get("semantic_status")
    if not isinstance(status, dict):
        return {"state": "unavailable",
                "reason": "semantic_status is missing or malformed"}
    state = status.get("state")
    if not isinstance(state, str) or not state:
        return {"state": "unavailable",
                "reason": "semantic_status.state is missing or malformed"}
    if state == "query-rejected":
        return {"state": "not_run", "reason": "semantic query rejected before search"}
    if state in {"unavailable", "generation-rejected", "generation-moving"}:
        return {"state": "unavailable", "reason": state}
    if state in {"ready", "no-confident-match"}:
        return {"state": "captured", "reason": state}
    return {"state": "unavailable",
            "reason": f"unknown semantic_status.state: {state}"}


class Scope:
    def __init__(self, trace: _Trace, token: contextvars.Token):
        self.trace = trace
        self.token = token


_CURRENT: contextvars.ContextVar[_Trace | None] = contextvars.ContextVar(
    "agrep_sabel_retrieval_trace", default=None)


def enabled() -> bool:
    return bool(os.environ.get(TRACE_ENV, "").strip())


def active() -> bool:
    """Whether this execution context owns an enabled observer trace."""
    return _CURRENT.get() is not None


def safe_begin(command: str, argv: list[str]) -> Scope | None:
    raw = os.environ.get(TRACE_ENV, "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).expanduser()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        trace = _Trace(root, command, argv)
        return Scope(trace, _CURRENT.set(trace))
    except Exception:
        return None


def safe_finish(scope: Scope | None, exit_code: int | None, *,
                error: BaseException | None = None) -> Path | None:
    if scope is None:
        return None
    try:
        return scope.trace.finish(exit_code, error)
    except Exception:
        return None
    finally:
        try:
            _CURRENT.reset(scope.token)
        except Exception:
            pass


def bind_current_context(function: Callable[[], Any]) -> Callable[[], Any]:
    """Propagate only an already-enabled trace into a semantic worker thread."""
    trace = _CURRENT.get()
    if trace is None:
        return function

    def run() -> Any:
        token = _CURRENT.set(trace)
        try:
            return function()
        finally:
            _CURRENT.reset(token)

    return run


def _with_trace(action: Callable[[_Trace], None]) -> None:
    trace = _CURRENT.get()
    if trace is None:
        return
    try:
        with trace.lock:
            if trace.closed:
                return
            action(trace)
    except Exception as exc:
        trace.fatal_record_error = exc


def record_metadata(key: str, value: object) -> None:
    _with_trace(lambda trace: trace.metadata.__setitem__(str(key), _jsonable(value)))


def record_query(raw_queries: list[str], queries: list[str], **fields: object) -> None:
    def add(trace: _Trace) -> None:
        unknown = set(fields) - {
            "surface", "semantic_requested", "lexical_only",
        }
        if unknown:
            raise ValueError(f"unsupported observer query fields: {sorted(unknown)}")
        artifact_id = trace._add_json_artifact(
            "query", "agrep.sabel.query.v1", {
                "retrieval_id": trace.retrieval_id,
                "raw_queries": raw_queries,
                "queries": queries,
                "surface": fields.get("surface"),
                "semantic_requested": fields.get("semantic_requested"),
                "lexical_only": fields.get("lexical_only"),
            }, stage="query")
        trace.add_event("query", "query", artifact_id=artifact_id,
                        query_count=len(queries))
    _with_trace(add)


def record_search_call(query: str, mode: str, kwargs: dict,
                       result: dict | None, duration_ns: int,
                       error: BaseException | None = None) -> None:
    _with_trace(lambda trace: trace.add_search_call(
        query, mode, kwargs, result, duration_ns, error))


def record_pool(stage: str, hits: list[dict], *, lane: str = "merged") -> None:
    _with_trace(lambda trace: trace.add_pool(stage, list(hits), lane=lane))


def record_windows(stage: str, pairs: list[tuple[dict, dict]]) -> None:
    def add(trace: _Trace) -> None:
        artifact_id = trace._add_json_artifact(
            "windows", WINDOW_SCHEMA, {
                "retrieval_id": trace.retrieval_id,
                "stage": stage,
                "ordered": True,
                "window_count": len(pairs),
                "pairs": pairs,
            }, stage=stage)
        trace.add_event("windows", stage, artifact_id=artifact_id,
                        window_count=len(pairs))
    _with_trace(add)


def record_stdout(payload: bytes) -> None:
    _with_trace(lambda trace: trace.add_stdout(payload))


def record_returned_handles(handles: list[str]) -> None:
    def add(trace: _Trace) -> None:
        for handle in handles:
            if handle and handle not in trace.returned_handles:
                trace.returned_handles.append(handle)
    _with_trace(add)


def record_lane_not_run(lane: str, reason: str) -> None:
    _with_trace(lambda trace: trace.lane_not_run_reasons.__setitem__(lane, reason))


def record_outcome(kind: str, code: str, reason: str | None = None) -> None:
    _with_trace(lambda trace: trace.add_event(
        "outcome", "command_outcome", outcome=kind, code=code,
        reason=reason))


def record_around_open(requested_handle: str | None, *, session: str,
                       requested_turn: int, served_turn: int,
                       window: dict) -> None:
    def add(trace: _Trace) -> None:
        trace.opened_handle = requested_handle
        artifact_id = trace._add_json_artifact(
            "around-source", "agrep.sabel.around-source.v1", {
                "retrieval_id": trace.retrieval_id,
                "requested_handle": requested_handle,
                "session": session,
                "requested_turn": requested_turn,
                "served_turn": served_turn,
                "window": window,
            }, stage="around_opened_source")
        trace.add_event(
            "around_open", "around_opened_source", artifact_id=artifact_id,
            requested_handle=requested_handle, session=session,
            requested_turn=requested_turn, served_turn=served_turn)
    _with_trace(add)


def newest_run(root: Path) -> Path:
    runs = sorted(path for path in root.iterdir() if path.is_dir())
    if not runs:
        raise FileNotFoundError("no observer run exists")
    return runs[-1]


def _exact_object(value: object, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"{where} keys mismatch (missing={sorted(keys - actual)}, "
            f"extra={sorted(actual - keys)})")
    return value


def _nonempty_string(value: object, where: str, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{where} must be a non-empty NUL-free string")
    return value


def _nonnegative_int(value: object, where: str, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")
    return value


def _nullable_reason(value: object, where: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{where} must be null or a string")


def _validate_error(value: object, where: str) -> None:
    if value is None:
        return
    error = _exact_object(value, {"type", "message"}, where)
    _nonempty_string(error["type"], f"{where}.type")
    if not isinstance(error["message"], str):
        raise ValueError(f"{where}.message must be a string")


def _validate_manifest_envelope(
        manifest: object, *, expected_run_name: str | None = None) -> dict:
    manifest = _exact_object(manifest, _MANIFEST_KEYS, "observer manifest")
    if (manifest["schema"] != MANIFEST_SCHEMA or manifest["version"] != 1):
        raise ValueError("observer manifest schema is invalid")
    run_id = _nonempty_string(manifest["run_id"], "observer manifest run_id")
    retrieval_id = _nonempty_string(
        manifest["retrieval_id"], "observer manifest retrieval_id")
    if retrieval_id != f"retrieval-{run_id}":
        raise ValueError("observer manifest retrieval_id is not self-bound")
    if expected_run_name is not None and run_id != expected_run_name:
        raise ValueError("observer manifest run_id does not match its directory")
    _nonempty_string(manifest["command"], "observer manifest command")
    started = _nonnegative_int(
        manifest["started_wall_ns"], "observer manifest started_wall_ns")
    ended = _nonnegative_int(
        manifest["ended_wall_ns"], "observer manifest ended_wall_ns")
    _nonnegative_int(manifest["duration_ns"], "observer manifest duration_ns")
    if ended < started:
        raise ValueError("observer manifest wall-clock interval is reversed")
    _nonnegative_int(
        manifest["exit_code"], "observer manifest exit_code", nullable=True)
    _validate_error(manifest["error"], "observer manifest error")
    if type(manifest["complete"]) is not bool:
        raise ValueError("observer manifest complete must be boolean")
    if manifest["complete"] != (manifest["error"] is None):
        raise ValueError("observer manifest complete contradicts error state")
    if not isinstance(manifest["metadata"], dict):
        raise ValueError("observer manifest metadata must be an object")
    if not isinstance(manifest["lane_states"], dict):
        raise ValueError("observer manifest lane_states must be an object")
    handles = manifest["returned_handles"]
    if (not isinstance(handles, list)
            or any(not isinstance(handle, str) or not handle for handle in handles)
            or len(handles) != len(set(handles))):
        raise ValueError("observer manifest returned_handles are invalid")
    _nonempty_string(
        manifest["opened_handle"], "observer manifest opened_handle",
        nullable=True)
    stdout_id = _nonempty_string(
        manifest["stdout_artifact_id"], "observer manifest stdout_artifact_id")

    descriptors = manifest["artifacts"]
    if not isinstance(descriptors, list) or len(descriptors) > MAX_ARTIFACTS:
        raise ValueError("observer manifest artifacts are invalid or excessive")
    if len(descriptors) < 2:
        raise ValueError("observer manifest must contain argv and stdout artifacts")
    by_id: dict[str, dict] = {}
    paths: set[str] = set()
    total_bytes = 0
    stdout_ids = []
    argv_count = 0
    for index, raw_descriptor in enumerate(descriptors, start=1):
        where = f"observer manifest artifacts[{index - 1}]"
        descriptor = _exact_object(raw_descriptor, _DESCRIPTOR_KEYS, where)
        sequence = _nonnegative_int(descriptor["sequence"], f"{where}.sequence")
        if sequence != index:
            raise ValueError("observer artifact descriptor order is invalid")
        kind = _nonempty_string(descriptor["kind"], f"{where}.kind")
        if kind not in _ARTIFACT_SCHEMAS:
            raise ValueError(f"{where}.kind is unsupported")
        stage = _nonempty_string(descriptor["stage"], f"{where}.stage")
        if stage not in _ARTIFACT_STAGES[kind]:
            raise ValueError(f"{where}.stage violates the artifact contract")
        artifact_id = _nonempty_string(
            descriptor["artifact_id"], f"{where}.artifact_id")
        expected_id = f"{retrieval_id}:artifact:{index:04d}:{kind}"
        if artifact_id != expected_id or artifact_id in by_id:
            raise ValueError("observer artifact identity is invalid")
        schema = _ARTIFACT_SCHEMAS[kind]
        media_type = ("application/json" if schema is not None
                      else "application/octet-stream")
        if (descriptor["schema"] != schema
                or descriptor["media_type"] != media_type):
            raise ValueError(f"{where} schema/media contract is invalid")
        suffix = ".json" if schema is not None else ".bin"
        expected_path = f"{index:04d}-{kind}{suffix}"
        name = descriptor["path"]
        if (name != expected_path or name in paths
                or Path(name).name != name):
            raise ValueError("observer artifact path is invalid")
        size = _nonnegative_int(descriptor["size"], f"{where}.size")
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("observer artifact exceeds its per-file bound")
        digest = descriptor["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in _HEX for character in digest)):
            raise ValueError(f"{where}.sha256 is invalid")
        total_bytes += size
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ValueError("observer artifact bytes exceed their bound")
        by_id[artifact_id] = descriptor
        paths.add(name)
        if kind == "stdout":
            stdout_ids.append(artifact_id)
        if kind == "argv":
            argv_count += 1
    if argv_count != 1 or descriptors[0]["kind"] != "argv":
        raise ValueError("observer publication must contain one leading argv artifact")
    if stdout_ids != [stdout_id] or descriptors[-1]["kind"] != "stdout":
        raise ValueError("observer stdout must be the sole trailing stdout descriptor")
    if not isinstance(manifest["events"], list):
        raise ValueError("observer manifest events must be an array")
    return by_id


def _aggregate_lane_state(states: list[str]) -> str:
    if "failed" in states:
        return "failed"
    if "unavailable" in states:
        return "unavailable"
    if states and all(state == "not_run" for state in states):
        return "not_run"
    return "captured"


def _validate_manifest_topology(
        manifest: dict, artifact_payloads: dict[str, bytes]) -> None:
    descriptors = {item["artifact_id"]: item for item in manifest["artifacts"]}
    if set(artifact_payloads) != set(descriptors):
        raise ValueError("observer artifact payload set differs from manifest")
    documents: dict[str, dict] = {}
    pools: dict[str, dict] = {}
    for artifact_id, descriptor in descriptors.items():
        payload = artifact_payloads[artifact_id]
        if (not isinstance(payload, bytes)
                or len(payload) != descriptor["size"]
                or _sha256(payload) != descriptor["sha256"]):
            raise ValueError("observer artifact bytes do not match descriptor")
        if descriptor["schema"] is None:
            continue
        if descriptor["kind"] == "pool":
            try:
                parsed = sabel_pool.parse_pool_document(
                    payload, expected_artifact_id=artifact_id,
                    expected_retrieval_id=manifest["retrieval_id"],
                    expected_stage=descriptor["stage"])
            except sabel_pool.PoolDocumentError as error:
                raise ValueError(f"observer pool artifact is invalid: {error}") from error
            document = parsed["record"]
            pools[artifact_id] = parsed
        else:
            document = _decode_json(payload, "observer JSON artifact")
            if payload != _canonical_json(document):
                raise ValueError("observer JSON artifact is not canonical")
        if (not isinstance(document, dict)
                or document.get("artifact_id") != artifact_id
                or document.get("schema") != descriptor["schema"]):
            raise ValueError("observer JSON artifact is not self-bound")
        if (descriptor["kind"] not in {"argv"}
                and document.get("retrieval_id") != manifest["retrieval_id"]):
            raise ValueError("observer JSON artifact retrieval_id is not self-bound")
        documents[artifact_id] = document

    for artifact_id, document in documents.items():
        kind = descriptors[artifact_id]["kind"]
        where = f"observer {kind} artifact {artifact_id}"
        if kind == "argv":
            _exact_object(document, _ARGV_KEYS, where)
            if document["command"] != manifest["command"]:
                raise ValueError("observer argv command contradicts manifest")
            for field in ("argv", "process_argv"):
                value = document[field]
                if (not isinstance(value, list)
                        or any(not isinstance(item, str) for item in value)):
                    raise ValueError(f"{where}.{field} must be a string array")
        elif kind == "query":
            _exact_object(document, _QUERY_KEYS, where)
            for field in ("raw_queries", "queries"):
                value = document[field]
                if (not isinstance(value, list)
                        or any(not isinstance(item, str) for item in value)):
                    raise ValueError(f"{where}.{field} must be a string array")
            _nonempty_string(
                document["surface"], f"{where}.surface", nullable=True)
            for field in ("semantic_requested", "lexical_only"):
                if document[field] is not None and type(document[field]) is not bool:
                    raise ValueError(f"{where}.{field} must be null or boolean")
        elif kind == "windows":
            _exact_object(document, _WINDOW_KEYS, where)
            if (document["stage"] != descriptors[artifact_id]["stage"]
                    or document["ordered"] is not True):
                raise ValueError("observer windows artifact stage/order is invalid")
            pairs = document["pairs"]
            count = _nonnegative_int(document["window_count"],
                                     f"{where}.window_count")
            if (not isinstance(pairs, list) or len(pairs) != count
                    or any(not isinstance(pair, list) or len(pair) != 2
                           or not all(isinstance(item, dict) for item in pair)
                           for pair in pairs)):
                raise ValueError("observer windows artifact pairs/count are invalid")
        elif kind == "around-source":
            _exact_object(document, _AROUND_SOURCE_KEYS, where)
            _nonempty_string(document["requested_handle"],
                             f"{where}.requested_handle", nullable=True)
            _nonempty_string(document["session"], f"{where}.session")
            _nonnegative_int(document["requested_turn"],
                             f"{where}.requested_turn")
            _nonnegative_int(document["served_turn"], f"{where}.served_turn")
            if not isinstance(document["window"], dict):
                raise ValueError(f"{where}.window must be an object")
        elif kind == "candidate-loss":
            try:
                sabel_shadow.validate_semantic_candidate_loss(
                    document, expected_artifact_id=artifact_id,
                    expected_retrieval_id=manifest["retrieval_id"])
            except sabel_shadow.ShadowSchemaError as error:
                raise ValueError(
                    f"observer candidate-loss artifact is invalid: {error}") from error

    search_documents: dict[str, tuple[str, dict]] = {}
    candidate_loss_owners: dict[str, list[str]] = {}
    for artifact_id, document in documents.items():
        if descriptors[artifact_id]["kind"] != "search-call":
            continue
        where = f"observer search-call artifact {artifact_id}"
        _exact_object(document, _SEARCH_CALL_KEYS, where)
        call_id = _nonempty_string(document["call_id"], f"{where}.call_id")
        if call_id in search_documents:
            raise ValueError("observer search call_id is duplicated")
        _nonempty_string(document["lane"], f"{where}.lane")
        if document["state"] not in _LANE_STATES:
            raise ValueError(f"{where}.state is invalid")
        _nullable_reason(document["reason"], f"{where}.reason")
        _nonempty_string(document["query"], f"{where}.query")
        if not isinstance(document["kwargs"], dict):
            raise ValueError(f"{where}.kwargs must be an object")
        _nonnegative_int(document["duration_ns"], f"{where}.duration_ns")
        _validate_error(document["error"], f"{where}.error")
        if not isinstance(document["result_meta"], dict):
            raise ValueError(f"{where}.result_meta must be an object")
        pipeline = document["semantic_pipeline"]
        if document["lane"] != "semantic":
            if pipeline is not None:
                raise ValueError(f"{where}.semantic_pipeline must be null")
        else:
            pipeline = _exact_object(
                pipeline, _SEMANTIC_PIPELINE_KEYS,
                f"{where}.semantic_pipeline")
            try:
                sabel_shadow.validate_semantic_effective_request(
                    pipeline["effective_request"])
            except sabel_shadow.ShadowSchemaError as error:
                raise ValueError(
                    f"{where} effective semantic request is invalid: {error}") from error
            expected_request = _semantic_effective_request_from_call(
                document["query"], document["kwargs"])
            if pipeline["effective_request"] != expected_request:
                raise ValueError(
                    f"{where} effective semantic request binding disagrees")
            stage_artifacts = []
            for stage_name in (
                    "q8_candidates", "f16_rerank",
                    "exhaustive_f16_diagnostic"):
                stage = _exact_object(
                    pipeline[stage_name], _SEMANTIC_PIPELINE_STAGE_KEYS,
                    f"{where}.semantic_pipeline.{stage_name}")
                if stage["state"] not in _LANE_STATES:
                    raise ValueError(f"{where} semantic pipeline state is invalid")
                if type(stage["execution_observed"]) is not bool:
                    raise ValueError(f"{where} semantic pipeline observation is invalid")
                _nullable_reason(stage["reason"], f"{where} semantic pipeline reason")
                artifact = _nonempty_string(
                    stage["artifact_id"], f"{where} semantic pipeline artifact",
                    nullable=True)
                captured = stage["state"] == "captured"
                if (captured != stage["execution_observed"]
                        or captured != (artifact is not None)
                        or captured == (stage["reason"] is not None)):
                    raise ValueError(f"{where} semantic pipeline state is contradictory")
                if artifact is not None:
                    descriptor = descriptors.get(artifact)
                    if descriptor is None or descriptor["kind"] != "candidate-loss":
                        raise ValueError(f"{where} semantic pipeline artifact is invalid")
                    stage_artifacts.append(artifact)
            if stage_artifacts:
                if (len(stage_artifacts) != 2
                        or stage_artifacts[0] != stage_artifacts[1]):
                    raise ValueError(f"{where} q8/f16 trace bindings disagree")
                loss_id = stage_artifacts[0]
                loss = documents[loss_id]
                expected_query = expected_request["query_sha256"]
                if loss.get("query_sha256") != expected_query:
                    raise ValueError(
                        f"{where} candidate-loss query binding disagrees")
                if loss.get("effective_request") != expected_request:
                    raise ValueError(
                        f"{where} candidate-loss request binding disagrees")
                for stage_name in ("q8_candidates", "f16_rerank"):
                    coverage = pipeline[stage_name]["coverage"]
                    if (not isinstance(coverage, dict)
                            or coverage.get("generation")
                            != loss.get("generation")):
                        raise ValueError(
                            f"{where} candidate-loss generation binding disagrees")
                candidate_loss_owners.setdefault(loss_id, []).append(call_id)
        search_documents[call_id] = (artifact_id, document)

    candidate_loss_ids = {
        artifact_id for artifact_id, descriptor in descriptors.items()
        if descriptor["kind"] == "candidate-loss"
    }
    if set(candidate_loss_owners) != candidate_loss_ids:
        raise ValueError("observer candidate-loss artifacts are not exactly referenced")
    if any(len(owners) != 1 for owners in candidate_loss_owners.values()):
        raise ValueError("observer candidate-loss artifact must have one search-call owner")

    events = manifest["events"]
    event_refs = {kind: [] for kind in _EVENT_KEYS if kind != "outcome"}
    prior_offset = -1
    search_events: dict[str, dict] = {}
    around_documents = []
    for index, raw_event in enumerate(events, start=1):
        if not isinstance(raw_event, dict):
            raise ValueError("observer event must be an object")
        kind = raw_event.get("kind")
        if kind not in _EVENT_KEYS:
            raise ValueError("observer event kind is invalid")
        event = _exact_object(
            raw_event, _EVENT_BASE_KEYS | _EVENT_KEYS[kind],
            f"observer events[{index - 1}]")
        if event["sequence"] != index:
            raise ValueError("observer event order is invalid")
        offset = _nonnegative_int(
            event["monotonic_offset_ns"],
            f"observer events[{index - 1}].monotonic_offset_ns")
        if offset < prior_offset:
            raise ValueError("observer event monotonic order is invalid")
        prior_offset = offset
        _nonempty_string(event["stage"], f"observer events[{index - 1}].stage")
        if kind == "outcome":
            _nonempty_string(event["outcome"], "observer outcome")
            _nonempty_string(event["code"], "observer outcome code")
            _nullable_reason(event["reason"], "observer outcome reason")
            continue
        artifact_field = ("query_artifact_id"
                          if kind == "search_call" else "artifact_id")
        artifact_id = event[artifact_field]
        descriptor = descriptors.get(artifact_id)
        expected_kind = {
            "search_call": "search-call", "around_open": "around-source",
            "candidate_loss": "candidate-loss",
        }.get(kind, kind)
        if (descriptor is None or descriptor["kind"] != expected_kind
                or descriptor["stage"] != event["stage"]):
            raise ValueError("observer event artifact reference is invalid")
        event_refs[kind].append(artifact_id)
        document = documents[artifact_id]
        if kind == "query":
            if (event["query_count"] != len(document.get("queries", []))
                    or type(event["query_count"]) is not int):
                raise ValueError("observer query event count is invalid")
        elif kind == "pool":
            parsed = pools[artifact_id]
            if (event["lane"] != parsed["lane"]
                    or event["candidate_count"] != parsed["candidate_count"]
                    or event["call_id"] != parsed["record"]["call_id"]):
                raise ValueError("observer pool event contradicts its artifact")
        elif kind == "windows":
            if (document.get("stage") != event["stage"]
                    or document.get("window_count") != event["window_count"]):
                raise ValueError("observer windows event contradicts its artifact")
        elif kind == "around_open":
            for field in ("requested_handle", "session", "requested_turn",
                          "served_turn"):
                if document.get(field) != event[field]:
                    raise ValueError("observer around event contradicts its artifact")
            around_documents.append(document)
        elif kind == "search_call":
            call_id = event["call_id"]
            if call_id in search_events:
                raise ValueError("observer search_call event is duplicated")
            found = search_documents.get(call_id)
            if found is None or found[0] != artifact_id:
                raise ValueError("observer search_call event call_id is invalid")
            for field in ("lane", "state", "reason", "call_id"):
                if event[field] != document[field]:
                    raise ValueError("observer search_call event contradicts artifact")
            pool_id = event["pool_artifact_id"]
            if pool_id is not None:
                parsed = pools.get(pool_id)
                if (parsed is None or parsed["record"]["call_id"] != call_id
                        or parsed["lane"] != event["lane"]
                        or parsed["stage"] != "run_query_return"):
                    raise ValueError("observer search_call pool reference is invalid")
            search_events[call_id] = event
        elif kind == "candidate_loss":
            if (document.get("pipeline") != event["pipeline"]
                    or len(document.get("candidates") or [])
                    != event["candidate_count"]):
                raise ValueError(
                    "observer candidate-loss event contradicts its artifact")

    for event_kind, artifact_kind in (
            ("query", "query"), ("pool", "pool"),
            ("search_call", "search-call"), ("windows", "windows"),
            ("around_open", "around-source"),
            ("candidate_loss", "candidate-loss")):
        expected = [artifact_id for artifact_id, descriptor in descriptors.items()
                    if descriptor["kind"] == artifact_kind]
        if event_refs[event_kind] != expected:
            raise ValueError(
                f"observer {event_kind} events do not exactly cover artifacts")

    lane_states = manifest["lane_states"]
    if not {"keyword", "semantic"}.issubset(lane_states):
        raise ValueError("observer lane_states omit required lanes")
    represented_calls: set[str] = set()
    for lane, raw_lane_state in lane_states.items():
        _nonempty_string(lane, "observer lane name")
        lane_state = _exact_object(
            raw_lane_state, {"state", "calls", "partial", "reason"},
            f"observer lane_states.{lane}")
        calls = lane_state["calls"]
        if not isinstance(calls, list):
            raise ValueError("observer lane calls must be an array")
        states = []
        for call_index, raw_call in enumerate(calls):
            call = _exact_object(
                raw_call, _CALL_KEYS,
                f"observer lane_states.{lane}.calls[{call_index}]")
            call_id = _nonempty_string(call["call_id"], "observer call_id")
            if call_id in represented_calls:
                raise ValueError("observer lane call_id is duplicated")
            found = search_documents.get(call_id)
            if found is None:
                raise ValueError("observer lane call does not resolve")
            artifact_id, document = found
            if (call["query_artifact_id"] != artifact_id
                    or call["pool_artifact_id"]
                    != search_events[call_id]["pool_artifact_id"]
                    or call["state"] != document["state"]
                    or call["reason"] != document["reason"]
                    or document["lane"] != lane):
                raise ValueError("observer lane call contradicts sealed artifacts")
            states.append(call["state"])
            represented_calls.add(call_id)
        if calls:
            if (lane_state["state"] != _aggregate_lane_state(states)
                    or lane_state["partial"] != (len(set(states)) > 1)
                    or lane_state["reason"] is not None):
                raise ValueError("observer aggregate lane state is invalid")
        elif (lane_state["state"] != "not_run"
              or lane_state["partial"] is not False
              or not isinstance(lane_state["reason"], str)
              or not lane_state["reason"]):
            raise ValueError("observer unobserved lane state is invalid")
    if represented_calls != set(search_documents):
        raise ValueError("observer lane states do not exactly cover search calls")

    if len(around_documents) > 1:
        raise ValueError("observer publication has multiple around source artifacts")
    if around_documents:
        if manifest["opened_handle"] != around_documents[0]["requested_handle"]:
            raise ValueError("observer opened_handle contradicts around source")
    elif manifest["opened_handle"] is not None:
        raise ValueError("observer opened_handle has no around source artifact")


def read_bundle(path: Path) -> dict:
    """Reopen and byte-verify one manifest-last observer publication."""
    path = Path(path)
    manifest_path = path / "manifest.json"
    try:
        info = manifest_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("observer publication has no manifest") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
        raise ValueError("observer manifest is not a bounded regular file")
    raw_manifest = manifest_path.read_bytes()
    manifest = _decode_json(raw_manifest, "observer manifest")
    if raw_manifest != _canonical_json(manifest):
        raise ValueError("observer manifest is not canonical")
    descriptors_by_id = _validate_manifest_envelope(
        manifest, expected_run_name=path.name)
    descriptors = manifest["artifacts"]
    expected = {"manifest.json"}
    artifact_payloads = {}
    actual_total_bytes = 0
    for descriptor in descriptors:
        artifact_id = descriptor.get("artifact_id")
        name = descriptor.get("path")
        expected.add(name)
        artifact_path = path / name
        try:
            artifact_info = artifact_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError("observer artifact is missing") from exc
        if not stat.S_ISREG(artifact_info.st_mode):
            raise ValueError("observer artifact is not a bounded regular file")
        if artifact_info.st_size != descriptor["size"]:
            raise ValueError("observer artifact bytes do not match descriptor")
        if artifact_info.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("observer artifact exceeds its per-file bound")
        actual_total_bytes += artifact_info.st_size
        if actual_total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ValueError("observer artifact bytes exceed their bound")
        payload = artifact_path.read_bytes()
        artifact_payloads[artifact_id] = payload
    actual = {item.name for item in path.iterdir()}
    if actual != expected:
        raise ValueError("observer publication contains unlisted files")
    if set(descriptors_by_id) != set(artifact_payloads):
        raise ValueError("observer publication is missing artifact bytes")
    _validate_manifest_topology(manifest, artifact_payloads)
    return manifest
