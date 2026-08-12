"""The semantic engine: coherence, freshness, and local search.

CLI commands use semworker.py's authenticated, serial, disposable owner so repeated
queries can reuse the ONNX runtime without making the freshness daemon retain it. An
in-process fallback preserves availability when local IPC is unavailable.

Freshness is generation-based and self-healing:
  - `embedding_coherence()` compares the transcript generation against the
    segmented manifest, or against the generation marker for a legacy flat
    bundle, so queries never read stale vectors.
  - a stale lane calls `ensure_fresh_async()`, which spawns ONE detached
    `embed.py` (embed.py itself holds the cross-process claim, so concurrent
    CLIs and indexd can both ask without stacking embedders) and
    reports `SemanticUnavailable` for this query - callers fall back to keyword.
  - a capped newest-first pass publishes an explicitly PARTIAL generation. Its
    vectors are source/hash coherent and immediately searchable while later
    background passes drain history; results disclose indexed/total coverage.
  - embed.py binds full or partial coverage only after validating the transcript
    generation. If ingest advances during ONNX work, unchanged covered hashes can
    rebase safely; changed or deleted covered rows refuse publication and retry.
    No index-wide lock is held while embedding.
"""

from __future__ import annotations

import json
import importlib.util
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import common
import ownerfile
import removal_fence
import surface_policy as surface

BOOTSTRAP_RETRY_S = max(60, int(os.environ.get("AGREP_SEM_BOOTSTRAP_RETRY_S", "600")))
# First crash costs seconds; the cap above is earned by a persistent crash loop.
BOOTSTRAP_RETRY_BASE_S = max(
    1, int(os.environ.get("AGREP_SEM_BOOTSTRAP_RETRY_BASE_S", "5")))
SEMANTIC_MAX_RESULTS = 200
# Bootstrap favors a small usable publication; background passes handle throughput.
# 256 rows ≈ 3.3 s at the foreground thread budget; embed.py's bootstrap
# deadline stops and publishes early when real rows run long.
SEMANTIC_BOOTSTRAP_MAX_NEW = max(
    1, int(os.environ.get("AGREP_SEM_BOOTSTRAP_MAX_NEW", "256")))
SEMANTIC_REFRESH_MAX_NEW = max(
    1, int(os.environ.get("AGREP_SEM_DEMAND_MAX_NEW", "500")))
SEMANTIC_REFS_DEMAND_S = max(
    60, int(os.environ.get("AGREP_SEM_REFS_DEMAND_S", "600")))
SEMANTIC_DEMAND_REFRESH_S = max(
    10, int(os.environ.get("AGREP_SEM_DEMAND_REFRESH_S", "60")))
# Readers can briefly see a new transcript source before its semantic rebind.
# Wait only while the publication lock proves convergence, plus a short handoff.
# The total stays below the automatic semantic lane's normal request budget.
SEMANTIC_PUBLICATION_WAIT_S = 0.5
SEMANTIC_PUBLICATION_GRACE_S = 0.12
SEMANTIC_PUBLICATION_POLL_MIN_S = 0.01
SEMANTIC_PUBLICATION_POLL_MAX_S = 0.05
SEMANTIC_REOPEN_DELAY_S = 0.01
SEMANTIC_UNSTABLE_SETTLE_DELAYS = (0.005, 0.01, 0.015)
# A fast automatic attempt stays cheap. Only a typed transient earns this
# separate window for publication convergence, serialized owner handoff, a
# cached-model load, and one retry.
SEMANTIC_QUERY_RECOVERY_MAX_S = 12.0


def bounded_query_recovery_wait_s(value: object) -> float:
    """Return a finite reviewed recovery window.

    The environment override is a test/operator brake, not permission to make
    an optional meaning lane hold keyword output longer than the product cap.
    Invalid and non-finite values retain the safe default; negative values
    explicitly disable the recovery pass.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SEMANTIC_QUERY_RECOVERY_MAX_S
    if not math.isfinite(parsed):
        return SEMANTIC_QUERY_RECOVERY_MAX_S
    return min(SEMANTIC_QUERY_RECOVERY_MAX_S, max(0.0, parsed))


SEMANTIC_QUERY_RECOVERY_WAIT_S = bounded_query_recovery_wait_s(
    os.environ.get("AGREP_SEM_QUERY_RECOVERY_WAIT_S", "12.0"))
SEMANTIC_QUERY_RECOVERY_POLL_DEFAULT_S = 0.025
SEMANTIC_QUERY_RECOVERY_POLL_MIN_S = 0.001
SEMANTIC_QUERY_RECOVERY_POLL_MAX_S = 0.25


def bounded_query_recovery_poll_s(value: object) -> float:
    """Return a finite responsive poll interval for the bounded recovery pass."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SEMANTIC_QUERY_RECOVERY_POLL_DEFAULT_S
    if not math.isfinite(parsed):
        return SEMANTIC_QUERY_RECOVERY_POLL_DEFAULT_S
    return min(
        SEMANTIC_QUERY_RECOVERY_POLL_MAX_S,
        max(SEMANTIC_QUERY_RECOVERY_POLL_MIN_S, parsed))


SEMANTIC_QUERY_RECOVERY_POLL_S = bounded_query_recovery_poll_s(
    os.environ.get("AGREP_SEM_QUERY_RECOVERY_POLL_S", "0.025"))
# Discarding a sound generation needs proof the bundle is wrong, not one
# surprising row: a text mismatch must be repeated AND dominate its page.
SEMANTIC_INTEGRITY_MIN_MISMATCH = max(
    1, int(os.environ.get("AGREP_SEM_INTEGRITY_MIN_DROPS", "3")))
SEMANTIC_INTEGRITY_MISMATCH_SHARE = 0.5

_LAST_USE = {"mono": 0.0}


class SemanticUnavailable(RuntimeError):
    """This query cannot be answered semantically right now (stale embeddings,
    missing model, wrong-space index). Callers degrade to keyword search."""


class SemanticRefreshSpawnDenied(RuntimeError):
    """The OS denied only the detached semantic-refresh process launch."""


LEGACY_PUBLICATION_REPAIR = surface.REMEDIES["legacy-publication"].text


def _data_dir_readonly() -> bool:
    """Whether this process is forbidden to mutate the active data directory."""
    return common.data_dir_readonly(common.DATA_DIR)


def _readonly_refresh_state() -> dict:
    return {
        "state": "read-only",
        "reason": "AGREP_DATA_READONLY protects this data directory",
    }


def _derived_mutation_refusal() -> dict | None:
    """Return the cross-build read-only state without importing corpusdb."""
    import indexd_runtime
    ownership = indexd_runtime.derived_writer_mutation_info()
    if ownership.writable:
        return None
    state = {
        "state": "read-only",
        "reason": ownership.reason,
    }
    if ownership.build_id is not None:
        state["owner_build"] = ownership.build_id
    return state


def _mutation_refusal() -> dict | None:
    if _data_dir_readonly():
        return _readonly_refresh_state()
    return _derived_mutation_refusal()


def _semantic_timing_enabled() -> bool:
    value = os.environ.get("AGREP_SEM_TIMING", "")
    return common.DEBUG or value.lower() not in ("", "0", "false", "no", "off")


def runtime_dependencies_available() -> bool:
    """Cheap pre-spawn proof for the optional native semantic runtime.

    Core-only installs must never launch ``embed.py`` just to discover in a child
    that NumPy/ONNX/tokenizers are absent. Import validation remains doctor/model
    work; this hot scheduler check deliberately performs no native imports.
    """
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in ("numpy", "onnxruntime", "tokenizers"))
    except (ImportError, AttributeError, ValueError):
        return False


def _runtime_unavailable() -> dict:
    return {
        "state": "optional-runtime-unavailable",
        "optional": True,
        "reason": "install agrep[semantic] on a supported OS/Python",
    }


# ---- generation / coherence ----

def generation_marker_path() -> Path:
    return common.DATA_DIR / ".semantic-embeddings-generation.json"


class _LegacyEmbeddingBundle(ValueError):
    pass


_FULL_REBUILD_LOCAL: set[str] = set()


def integrity_rebuild_path() -> Path:
    return common.DATA_DIR / ".semantic-full-rebuild"


def _integrity_rebuild_key() -> str:
    return os.path.abspath(os.fspath(integrity_rebuild_path()))


def integrity_rebuild_requested() -> bool:
    if _integrity_rebuild_key() in _FULL_REBUILD_LOCAL:
        return True
    try:
        os.lstat(integrity_rebuild_path())
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _mark_integrity_rebuild() -> bool:
    key = _integrity_rebuild_key()
    if _data_dir_readonly():
        # No marker can be written and no embedder can ever run here, so a latch
        # would kill this process's semantic lane with no path back. Degrade the
        # query instead; a real fault re-proves itself on the next one.
        return False
    if _derived_mutation_refusal() is not None:
        return False
    marker = integrity_rebuild_path()
    temporary = common.embedding_temp_path(marker, "full_rebuild")
    try:
        with temporary.open("wb") as stream:
            stream.write(b"full semantic rebuild required\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        common.replace_with_retry(temporary, marker)
        _FULL_REBUILD_LOCAL.discard(key)
        return True
    except OSError:
        _FULL_REBUILD_LOCAL.add(key)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def clear_integrity_rebuild_request() -> None:
    if _data_dir_readonly():
        _FULL_REBUILD_LOCAL.discard(_integrity_rebuild_key())
        return
    if _derived_mutation_refusal() is not None:
        return
    try:
        integrity_rebuild_path().unlink(missing_ok=True)
    except OSError:
        return
    _FULL_REBUILD_LOCAL.discard(_integrity_rebuild_key())


def request_full_rebuild(reason: str, *, launch: bool = True) -> dict:
    if _data_dir_readonly():
        persistent = _mark_integrity_rebuild()
        return {
            "state": "recorded",
            "persistent": persistent,
            "reason": str(reason),
        }
    refusal = _derived_mutation_refusal()
    if refusal is not None:
        return {**refusal, "persistent": False, "request_reason": str(reason)}
    persistent = _mark_integrity_rebuild()
    if not launch:
        return {"state": "recorded", "persistent": persistent}
    try:
        started = ensure_fresh_async(
            max_new=SEMANTIC_BOOTSTRAP_MAX_NEW, force_full=True)
        state = str(started.get("state") or "unknown")
    except Exception as exc:  # noqa: BLE001 -- lexical fallback remains available
        state = f"launch-failed:{type(exc).__name__}"
    return {"state": state, "persistent": persistent, "reason": str(reason)}


def verify_embedding_integrity() -> dict:
    """Spend immutable segment digests during an explicit diagnostic pass."""
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    record = json.loads(meta.read_bytes())
    if not isinstance(record, dict) or record.get("version") != 2:
        return {"state": "not-recorded", "verified": False}
    import embedding_segments
    manifest = embedding_segments.load_manifest(
        meta, verify_hashes=True, validate_liveness=True)
    return {
        "state": "verified",
        "verified": True,
        "generation": manifest["generation"],
        "artifacts": sum(
            len(segment["artifacts"]) for segment in manifest["segments"])
        + len(manifest["shadows"]) + 1,
    }


def source_generation(attempts: int = 4) -> dict | None:
    return common.transcript_generation(attempts=attempts)


def output_generation() -> dict | None:
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if not meta.exists():
        return None
    try:
        meta_record = json.loads(meta.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"embedding metadata is invalid: {exc}") from exc
    if isinstance(meta_record, dict) and meta_record.get("version") == 2:
        state = common.committed_embedding_artifact_state(
            meta, common.EMBEDDINGS_PATH, common.IDS_PATH)
        manifest = state.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("segmented embedding state has no manifest")
        raw_coverage = manifest["coverage"]
        indexed, total = int(raw_coverage["indexed"]), int(raw_coverage["total"])
        coverage = {
            "indexed": indexed, "total": total, "pending": total - indexed,
            "fraction": round(indexed / total, 6) if total else 1.0,
            "complete": bool(raw_coverage["complete"]),
            "order": str(raw_coverage["order"]),
        }
        return {
            "version": 2, "format": "segments-v2", "segmented": True,
            "bundle": state["identity"], "commit": manifest["generation"],
            "generation": manifest["generation"], "rows": manifest["live_rows"],
            "live_rows": manifest["live_rows"], "physical_rows": manifest["physical_rows"],
            "source": manifest["source"], "coverage": coverage,
            "model": manifest["model"], "artifacts": state["artifacts"],
            "set_manifest": manifest["set_manifest"],
        }
    if not all(path.exists() for path in (common.EMBEDDINGS_PATH, common.IDS_PATH)):
        return None
    state = common.committed_embedding_artifact_state(
        meta, common.EMBEDDINGS_PATH, common.IDS_PATH)
    commit = state["commit"]
    if commit is None:
        raise _LegacyEmbeddingBundle(
            "embedding pair predates the publish-last generation contract")
    return {
        "version": 2,
        "bundle": state["identity"],
        "commit": str(commit["generation"]),
        "rows": int(commit["rows"]),
        "artifacts": state["artifacts"],
    }


def _lane_mismatch(actual_model: str | None,
                   expected_model: str | None) -> dict | None:
    """(built, active) lanes when the ONLY thing wrong is which engine ran.

    Worth separating from any other profile change because the remedy differs:
    a real profile change wants a rebuild, whereas a lane mismatch is usually a
    machine that can reach the other engine.
    """
    import embedder
    built = embedder.lane_of(actual_model)
    active = embedder.lane_of(expected_model)
    if built is None or active is None or built == active:
        return None
    return {"built": built, "active": active}


def _active_embedding_profile(recorded_model: str | None = None) -> tuple[int, str]:
    """The query model contract for a store recording ``recorded_model``.

    Lane-aware: a Metal-built store resolves to the Metal identity where that
    lane can open and to the CPU one where it cannot, so an unavailable lane
    lands in the profile-mismatch refusal below instead of being served with
    vectors from the other space.
    """
    import embedder
    return int(embedder.PROFILE["dim"]), embedder.store_profile_string(recorded_model)


def _marker_coverage(marker: dict, output: dict) -> dict | None:
    """Validate and normalize the marker's searchable row coverage.

    Version 2 is the historical all-rows contract. Version 3 adds an honest
    partial generation without weakening source/output binding: every published
    vector must still belong to the exact current transcript generation.
    """
    try:
        rows = int(output["rows"])
    except (KeyError, TypeError, ValueError):
        return None
    if marker.get("version") == 2:
        return {
            "indexed": rows, "total": rows, "pending": 0,
            "fraction": 1.0, "complete": True, "order": "complete",
        }
    if marker.get("version") != 3 or not isinstance(marker.get("coverage"), dict):
        return None
    raw = marker["coverage"]
    try:
        indexed = int(raw["indexed"])
        total = int(raw["total"])
    except (KeyError, TypeError, ValueError):
        return None
    complete = indexed == total
    try:
        pending = int(raw["pending"])
    except (KeyError, TypeError, ValueError):
        return None
    if (indexed != rows or indexed <= 0 or total < indexed
            or pending != total - indexed):
        return None
    return {
        "indexed": indexed,
        "total": total,
        "pending": total - indexed,
        "fraction": round(indexed / total, 6),
        "complete": complete,
        "order": str(raw.get("order") or "newest-first"),
    }


def embedding_coherence() -> dict:
    """Whether vector rows were published from the current transcript generation."""
    try:
        source = source_generation()
    except common.LegacyPublication as exc:
        return {
            "coherent": False,
            "searchable": False,
            "state": "legacy-publication",
            "basis": None,
            "reason": (
                "legacy-publication: the transcript predates generation binding; "
                f"{LEGACY_PUBLICATION_REPAIR}"
            ),
            "detail": str(exc),
        }
    except RuntimeError as exc:
        return {"coherent": False, "searchable": False,
                "state": "unstable-source", "basis": None, "reason": str(exc)}
    if source is None:
        return {"coherent": False, "searchable": False,
                "state": "missing-source", "basis": None}
    source_mtime = max(
        (value["mtime_ns"] for value in source["files"].values()), default=0)
    if integrity_rebuild_requested():
        return {
            "coherent": False, "searchable": False,
            "state": "corrupt-embeddings", "basis": None,
            "reason": "a deterministic integrity failure requires a full rebuild",
            "source_mtime_ns": source_mtime,
        }
    try:
        output = output_generation()
    except _LegacyEmbeddingBundle as exc:
        return {"coherent": False, "searchable": False,
                "state": "legacy-embeddings", "basis": None,
                "reason": str(exc), "source_mtime_ns": source_mtime}
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"coherent": False, "searchable": False,
                "state": "corrupt-embeddings", "basis": None,
                "reason": str(exc), "source_mtime_ns": source_mtime}
    if output is None:
        return {"coherent": False, "searchable": False,
                "state": "missing-embeddings", "basis": None,
                "source_mtime_ns": source_mtime}
    try:
        actual_dim, actual_model = common.read_index_meta(
            common.EMBEDDINGS_PATH.parent / "embeddings.meta")
        expected_dim, expected_model = _active_embedding_profile(actual_model)
    except (ImportError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"coherent": False, "searchable": False,
                "state": "profile-unavailable", "basis": None,
                "reason": str(exc), "source_mtime_ns": source_mtime}
    if actual_dim != expected_dim or actual_model != expected_model:
        return {
            "coherent": False,
            "searchable": False,
            "state": "profile-mismatch",
            "basis": None,
            "reason": (f"embedding profile {actual_model!r}/{actual_dim} != "
                       f"active {expected_model!r}/{expected_dim}"),
            "lane_mismatch": _lane_mismatch(actual_model, expected_model),
            "source_mtime_ns": source_mtime,
        }
    if output.get("segmented"):
        try:
            source_after = source_generation()
            output_after = output_generation()
        except (OSError, RuntimeError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            return {"coherent": False, "searchable": False,
                    "state": "unstable-embeddings", "basis": None,
                    "reason": str(exc), "source_mtime_ns": source_mtime}
        if source_after != source:
            return {"coherent": False, "searchable": False,
                    "state": "unstable-source", "basis": None,
                    "reason": "transcript generation moved during coherence check",
                    "source_mtime_ns": source_mtime}
        if output_after != output:
            return {"coherent": False, "searchable": False,
                    "state": "unstable-embeddings", "basis": None,
                    "reason": "embedding generation moved during coherence check",
                    "source_mtime_ns": source_mtime}
        bound = output.get("source") == source
        coverage = output.get("coverage") if bound else None
        searchable = isinstance(coverage, dict)
        exact = bool(searchable and coverage.get("complete"))
        output_mtime = min(
            (value["mtime_ns"] for value in output["artifacts"].values()), default=0)
        state = "current" if exact else "partial" if searchable else "stale"
        return {
            "coherent": exact, "searchable": searchable, "state": state,
            "layout": "segments-v2", "migration_pending": False,
            "basis": ("generation" if exact else
                      "partial-generation" if searchable else None),
            "coverage": coverage, "source_mtime_ns": source_mtime,
            "embedding_mtime_ns": output_mtime,
            "source_files": len(source["files"]),
            "embedding_files": len(output["artifacts"]),
        }
    try:
        marker = json.loads(generation_marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    # Close the check around the marker read. Without this second observation, a
    # source or vector publication landing between the first snapshots and marker
    # read could be reported as current even though a newer generation already won.
    try:
        source_after = source_generation()
        output_after = output_generation()
    except RuntimeError as exc:
        return {"coherent": False, "searchable": False,
                "state": "unstable-source", "basis": None,
                "reason": str(exc), "source_mtime_ns": source_mtime}
    except (_LegacyEmbeddingBundle, OSError, ValueError, TypeError,
            json.JSONDecodeError) as exc:
        return {"coherent": False, "searchable": False,
                "state": "unstable-embeddings", "basis": None,
                "reason": str(exc), "source_mtime_ns": source_mtime}
    if source_after != source:
        return {"coherent": False, "searchable": False,
                "state": "unstable-source", "basis": None,
                "reason": "transcript generation moved during coherence check",
                "source_mtime_ns": source_mtime}
    if output_after != output:
        return {"coherent": False, "searchable": False,
                "state": "unstable-embeddings", "basis": None,
                "reason": "embedding generation moved during coherence check",
                "source_mtime_ns": source_mtime}
    marker_output = marker.get("output")
    if (marker.get("source") == source and isinstance(marker_output, dict)
            and marker_output.get("commit") == output.get("commit")
            and marker_output != output):
        return {
            "coherent": False, "searchable": False,
            "state": "corrupt-embeddings", "basis": None,
            "reason": "committed embedding generation changed without a new commit",
            "source_mtime_ns": source_mtime,
        }
    bound = marker.get("source") == source and marker_output == output
    coverage = _marker_coverage(marker, output) if bound else None
    searchable = coverage is not None
    exact = bool(searchable and coverage["complete"])
    output_mtime = min(
        (value["mtime_ns"] for value in output["artifacts"].values()), default=0)
    state = "current" if exact else "partial" if searchable else "stale"
    return {"coherent": exact, "searchable": searchable, "state": state,
            "layout": "flat-v1", "migration_pending": searchable,
            "basis": ("generation" if exact else
                      "partial-generation" if searchable else None),
            "coverage": coverage,
            "source_mtime_ns": source_mtime, "embedding_mtime_ns": output_mtime,
            "source_files": len(source["files"]),
            "embedding_files": len(output["artifacts"])}


def write_generation_marker(source: dict, *, indexed_rows: int | None = None,
                            total_rows: int | None = None,
                            expected_output: dict | None = None) -> None:
    """Bind a verified output to the transcript generation captured before work.

    Segmented manifests already carry this authority, so they are validated
    without a second write. Flat compatibility bundles use the v2 or progressive
    v3 marker until their background migration publishes segmented version 2.
    """
    if _data_dir_readonly():
        raise OSError(
            "AGREP_DATA_READONLY protects the semantic generation marker")
    refusal = _derived_mutation_refusal()
    if refusal is not None:
        raise OSError(str(refusal["reason"]))
    output = output_generation()
    if output is None:
        raise RuntimeError("embedding refresh did not publish a complete index")
    if expected_output is not None and output != expected_output:
        raise RuntimeError("embedding output moved before generation marker publish")
    if (indexed_rows is None) != (total_rows is None):
        raise ValueError("indexed_rows and total_rows must be supplied together")
    if output.get("segmented"):
        if output.get("source") != source:
            raise RuntimeError("segmented embedding source does not match the requested marker")
        coverage = output.get("coverage") or {}
        if indexed_rows is not None and (
                int(indexed_rows) != int(coverage.get("indexed", -1))
                or int(total_rows) != int(coverage.get("total", -1))):
            raise ValueError("requested coverage does not match segmented embeddings")
        return
    record = {"version": 2, "source": source, "output": output,
              "refreshed_at": time.time()}
    if indexed_rows is not None and total_rows is not None:
        indexed_rows, total_rows = int(indexed_rows), int(total_rows)
        if (indexed_rows != int(output["rows"]) or indexed_rows <= 0
                or total_rows < indexed_rows):
            raise ValueError(
                f"invalid semantic coverage {indexed_rows}/{total_rows} for "
                f"{output['rows']} published rows")
        record = {
            "version": 3, "source": source, "output": output,
            "coverage": {
                "indexed": indexed_rows, "total": total_rows,
                "pending": total_rows - indexed_rows,
                "order": "newest-first",
            },
            "refreshed_at": time.time(),
        }
    path = generation_marker_path()
    tmp = common.embedding_temp_path(path, "source_generation")
    try:
        tmp.write_text(json.dumps(record, separators=(",", ":")),
                       encoding="utf-8")
        common.replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def hashes_aligned() -> bool:
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    try:
        record = json.loads(meta.read_bytes())
        if isinstance(record, dict) and record.get("version") == 2:
            return bool(output_generation())
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        pass
    hashes = common.EMBEDDINGS_PATH.with_suffix(".hashes")
    try:
        ids_n = sum(1 for line in common.IDS_PATH.read_text(encoding="utf-8").split("\n")
                    if line)
        hashes_n = sum(1 for line in hashes.read_text(encoding="utf-8").split("\n")
                       if line)
        return ids_n == hashes_n and ids_n > 0
    except OSError:
        return False


def _needs_unverified_bundle_rebuild() -> bool:
    """Whether an existing bundle lacks trustworthy per-row source hashes.

    Do not parse the output generation here. Legacy and crash-partial bundles are
    precisely the inputs that can make that parser raise; the refresh launcher
    must remain a total, best-effort operation and let embed.py rebuild cleanly.
    """
    if hashes_aligned():
        return False
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    try:
        record = json.loads(meta.read_bytes())
        if isinstance(record, dict) and record.get("version") == 2:
            return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return any(path.exists() for path in (common.EMBEDDINGS_PATH, common.IDS_PATH, meta))


# ---- the single-owner embed claim (held by embed.py itself) ----

def embed_claim_path() -> Path:
    return common.DATA_DIR / ".semantic-embed.lock"


def embed_state_path() -> Path:
    return common.DATA_DIR / ".semantic-embed-state.json"


def compaction_claim_path() -> Path:
    return common.DATA_DIR / ".semantic-compaction.lock"


def _inspect_background_writer_claim(path: Path) -> dict:
    try:
        observed = ownerfile.snapshot(path, max_bytes=4096)
    except FileNotFoundError:
        return {"state": "absent", "snapshot": None, "pid": None,
                "process_start": None}
    except OSError:
        return {"state": "hostile", "snapshot": None, "pid": None,
                "process_start": None}
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("background writer claim must be an object")
        pid = record.get("pid")
        process_start = record.get("process_start")
        token = record.get("token")
        if (not isinstance(pid, int) or isinstance(pid, bool)
                or pid <= 0 or pid > common._MAX_PROCESS_ID
                or not isinstance(process_start, str)
                or process_start in ("", "None", "unknown")):
            raise ValueError("background writer claim identity is invalid")
        if (not isinstance(token, str)
                or len(token) != 32
                or any(char not in "0123456789abcdef" for char in token)):
            return {
                "state": "malformed-token", "snapshot": observed,
                "pid": None, "process_start": None,
            }
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        age = time.time() - observed.mtime
        state = (
            "malformed-fresh" if 0.0 <= age <= 30.0
            else "malformed-stale")
        return {"state": state, "snapshot": observed, "pid": None,
                "process_start": None}
    owner = ownerfile.classify_process(
        pid, process_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    return {
        "state": owner.value,
        "snapshot": observed,
        "pid": pid,
        "process_start": process_start,
    }


def _stop_background_writer_claim(
        path: Path, *, wait_s: float,
) -> dict:
    deadline = time.monotonic() + max(0.0, wait_s)
    stopped = False
    replacements = 0
    previous = None
    for _ in range(8):
        inspected = _inspect_background_writer_claim(path)
        state = inspected["state"]
        snapshot = inspected["snapshot"]
        if state == "absent":
            return {
                "ok": True, "state": "absent",
                "stopped": stopped, "replacements": replacements,
            }
        generation = (
            snapshot.identity, snapshot.raw) if snapshot is not None else None
        if previous is not None and generation != previous:
            return {
                "ok": False, "state": "replaced",
                "stopped": stopped, "replacements": replacements + 1,
            }
        previous = generation
        if state == ownerfile.ProcessOwner.EXACT_LIVE.value:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0.0 or not common.terminate_exact_process_tree(
                    inspected["pid"], inspected["process_start"],
                    wait_s=remaining):
                return {
                    "ok": False, "state": "exact-live",
                    "stopped": stopped, "replacements": replacements,
                }
            stopped = True
        elif state not in (
                ownerfile.ProcessOwner.DEAD.value,
                ownerfile.ProcessOwner.REUSED.value):
            return {
                "ok": False, "state": state,
                "stopped": stopped, "replacements": replacements,
            }
        if snapshot is None:
            return {
                "ok": False, "state": state,
                "stopped": stopped, "replacements": replacements,
            }
        try:
            ownerfile.remove_exact(
                path, snapshot, tombstone=True,
                require_stable_mtime=True)
        except OSError:
            return {
                "ok": False, "state": "inspection-failed",
                "stopped": stopped, "replacements": replacements,
            }
    return {
        "ok": False, "state": "unsettled",
        "stopped": stopped, "replacements": replacements,
    }


def stop_background_writers_for_removal(*, wait_s: float = 5.0) -> dict:
    """Stop exact semantic writers while the removal transaction excludes new ones."""
    if _data_dir_readonly():
        return {
            "ok": False,
            "state": "read-only",
            "stopped": (),
            "claims": {},
        }
    refusal = _derived_mutation_refusal()
    if refusal is not None:
        return {
            "ok": False,
            **refusal,
            "stopped": (),
            "claims": {},
        }
    if not removal_fence.background_removal_active():
        return {
            "ok": False, "state": "removal-fence-missing",
            "stopped": (), "claims": {},
        }
    deadline = time.monotonic() + max(0.0, wait_s)
    claims = {}
    stopped = []
    for label, path in (
            ("semantic embed", embed_claim_path()),
            ("semantic compactor", compaction_claim_path())):
        outcome = _stop_background_writer_claim(
            path, wait_s=max(0.0, deadline - time.monotonic()))
        claims[label] = outcome
        if outcome.get("stopped") and outcome.get("ok"):
            stopped.append(label)
    failed = next(
        (f"{label}: {outcome.get('state')}"
         for label, outcome in claims.items() if not outcome.get("ok")),
        None)
    return {
        "ok": failed is None,
        "state": "absent" if failed is None else failed,
        "stopped": tuple(stopped),
        "claims": claims,
    }


def read_embed_state() -> dict:
    try:
        state = json.loads(embed_state_path().read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"state": "idle"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"state": "idle"}


def lane_change_rebuild(state: dict | None = None) -> dict | None:
    """The lane change behind an unfinished full re-embed, or None.

    A lane change keeps the model and re-embeds every row anyway, so partial
    coverage after one is not the ordinary newest-first catch-up it looks like.
    The embed pass records the reason; a finished rebuild ("ready") stops
    speaking, and only a real change between two known lanes ever does.

    Callers that already hold a state record pass it in: the partial-coverage
    footer reads it for the freshness stamp a few lines earlier, and one render
    owes the reader one read.
    """
    state = read_embed_state() if state is None else state
    change = state.get("lane_change")
    if not isinstance(change, dict) or state.get("state") == "ready":
        return None
    built, serving = change.get("from"), change.get("to")
    if not built or not serving or built == serving:
        return None
    return {"from": str(built), "to": str(serving)}


def query_readiness(
        coherence: dict | None = None, *, refresh: dict | None = None) -> dict:
    """Current-generation readiness and bounded background-build progress."""
    coherence = embedding_coherence() if coherence is None else coherence
    state = str(coherence.get("state") or "unknown")
    searchable = bool(
        coherence.get("searchable", coherence.get("coherent", False)))
    progress = read_embed_state()
    running = bool(
        progress.get("state") == "running" and embed_running())
    phase = progress.get("phase") if running else None
    done = progress.get("done") if running else None
    total = progress.get("total") if running else None
    try:
        done = max(0, int(done)) if done is not None else None
        total = max(0, int(total)) if total is not None else None
    except (TypeError, ValueError):
        done = total = None
    if done is not None and total is not None:
        done = min(done, total)
    refresh_state = str(
        (refresh or {}).get("state")
        or ("running" if running else progress.get("state") or "idle"))
    return {
        "state": "ready" if searchable else "not-ready",
        "generation_state": state,
        "current_generation": searchable,
        "complete": bool(coherence.get("coherent")),
        "coverage": coherence.get("coverage"),
        "refresh": {
            "state": refresh_state,
            "running": running,
            "phase": str(phase) if phase else None,
            "done": done,
            "total": total,
        },
    }


def _query_unavailable_reason(coherence: dict, refresh: dict | None) -> str:
    readiness = query_readiness(coherence, refresh=refresh)
    generation = readiness["generation_state"]
    state = str((refresh or {}).get("state") or "not-started")
    lanes = coherence.get("lane_mismatch")
    if lanes:
        # Deliberately not phrased as a transient index update: no amount of
        # retrying opens the other engine, and the rebuild that would fix it
        # re-embeds the whole store rather than catching up a few rows.
        return (f"these embeddings were built on the {lanes['built']} lane, "
                f"which is not open here; ranking them against {lanes['active']} "
                f"vectors would answer from a different vector space. Run where "
                f"{lanes['built']} is available, or let the rebuild onto "
                f"{lanes['active']} finish (refresh {state})")
    detail = f"embeddings {generation} for the current generation; refresh {state}"
    progress = readiness["refresh"]
    if progress["running"]:
        bits = [str(progress["phase"])] if progress["phase"] else []
        if progress["total"]:
            bits.append(f"{progress['done'] or 0:,}/{progress['total']:,} rows")
        if bits:
            detail += f" ({' · '.join(bits)})"
    retry_at = (refresh or {}).get("retry_at")
    if retry_at:
        try:
            wait = max(0.0, float(retry_at) - time.time())
        except (TypeError, ValueError):
            wait = None
        if wait is not None:
            detail += f"; auto-retry in {wait:.0f}s"
    cli = common.cli_name()
    if state == "running":
        return detail + "; retry shortly or inspect with `" + cli + " doctor --deep`"
    return detail + "; inspect and repair with `" + cli + " doctor --deep`"


def _query_corpus_update_active() -> bool:
    """Read the corpus writer's publication lock without making it required."""
    try:
        import segment_query
        return bool(segment_query.corpus_update_active())
    except (ImportError, OSError, RuntimeError):
        return False


def _semantic_refresh_delegate_alive() -> bool:
    """Whether this exact build has a responsive freshness owner to delegate to.

    Agent sandboxes can read and query the local corpus yet be forbidden from
    launching the detached embed refresher.  The already-running compatible
    index daemon performs that same deduplicated refresh after publication. Its
    public liveness probe binds process identity, build/writer identity,
    topology, readiness, and responsiveness; a mere lock file is never enough.
    """
    try:
        import indexd_runtime
        status = indexd_runtime.indexd_resource_status(
            observe_only=True, include_rss=False)
        return status.get("running") is True
    except Exception:  # noqa: BLE001 -- a recovery hint fails closed
        return False


def wait_for_query_recovery(*, timeout_s: float | None = None) -> dict:
    """Wait for one *proven-converging* current generation, never stale data.

    This is deliberately called only after a semantic request reports a
    retryable publication/update failure.  A stale segmented generation may
    launch the same deduplicated background refresh that ``search()`` already
    requests.  We then poll generation coherence and the corpus publication
    lock.  Missing, disabled, read-only, profile-mismatched, and corrupt lanes
    return immediately unless a live writer/embedding owner proves convergence
    is actually underway.
    """
    wait_s = (SEMANTIC_QUERY_RECOVERY_WAIT_S if timeout_s is None
              else max(0.0, float(timeout_s)))
    deadline = time.monotonic() + wait_s
    refresh: dict | None = None
    waited = False
    delegate_check_at = 0.0

    while True:
        coherence = embedding_coherence()
        state = str(coherence.get("state") or "unknown")
        searchable = bool(
            coherence.get("searchable", coherence.get("coherent", False)))
        corpus_active = _query_corpus_update_active()
        if searchable and not corpus_active:
            return {
                "state": "ready", "waited": waited,
                "coherence": coherence, "refresh": refresh,
            }

        # Source movement over an immutable segmented bundle is the safe,
        # incremental rebase case.  Start at most one deduplicated refresher;
        # changed/deleted rows remain excluded by the existing hash proof.
        if (refresh is None and state == "stale"
                and coherence.get("layout") == "segments-v2"):
            try:
                refresh = ensure_fresh_async(
                    max_new=SEMANTIC_BOOTSTRAP_MAX_NEW,
                    allow_model_download=False)
            except Exception as exc:  # noqa: BLE001 -- caller keeps original status
                # A sandbox can forbid launching detached work while the exact
                # compatible daemon already owns semantic catch-up: wait on that
                # proven delegate, same deadline; other failures stay explicit.
                coherence_after = embedding_coherence()
                corpus_active = _query_corpus_update_active()
                searchable_after = bool(coherence_after.get(
                    "searchable", coherence_after.get("coherent", False)))
                if searchable_after and not corpus_active:
                    return {
                        "state": "ready", "waited": waited,
                        "coherence": coherence_after, "refresh": refresh,
                    }
                coherence = coherence_after
                if (isinstance(exc, SemanticRefreshSpawnDenied)
                        and _semantic_refresh_delegate_alive()):
                    refresh = {
                        "state": "delegated-indexd",
                        "reason": "compatible freshness owner is active",
                        "coherence": coherence,
                    }
                    delegate_check_at = time.monotonic() + 0.25
                    common.dbg(
                        "semantic refresh launch denied; waiting on the "
                        "compatible freshness owner")
                else:
                    return {
                        "state": "launch-failed", "waited": waited,
                        "reason": type(exc).__name__, "coherence": coherence,
                    }
            repaired = refresh.get("coherence")
            if (refresh.get("state") == "ready"
                    and isinstance(repaired, dict)
                    and repaired.get(
                        "searchable", repaired.get("coherent", False))
                    and not _query_corpus_update_active()):
                return {
                    "state": "ready", "waited": waited,
                    "coherence": repaired, "refresh": refresh,
                }

        embed_active = embed_running()
        delegated = (refresh or {}).get("state") == "delegated-indexd"
        if (delegated and not corpus_active and not embed_active
                and time.monotonic() >= delegate_check_at):
            if not _semantic_refresh_delegate_alive():
                return {
                    "state": "not-converging", "waited": waited,
                    "coherence": coherence, "refresh": refresh,
                }
            delegate_check_at = time.monotonic() + 0.25
        refresh_running = bool(
            (refresh or {}).get("state") == "running"
            or delegated or embed_active)
        # Millisecond source/manifest movement is transient only while a real
        # publication owner exists.  Never turn an unexplained stale state into
        # an eight-second sleep on every command.
        converging = bool(corpus_active or refresh_running)
        if not converging:
            return {
                "state": "not-converging", "waited": waited,
                "coherence": coherence, "refresh": refresh,
            }

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return {
                "state": "timeout", "waited": waited,
                "coherence": coherence, "refresh": refresh,
            }
        waited = True
        time.sleep(min(SEMANTIC_QUERY_RECOVERY_POLL_S, remaining))


def embed_failure_streak(prior: dict, now: float | None = None) -> int:
    """Consecutive-failure count the NEXT failed state should record (min 1).

    A "failed" state counts its own failure; a "running" state carries the
    streak before that pass, so a worker that dies without publishing still
    increments. Any successful publish drops the field and resets the streak,
    and a FAILED streak older than twice the retry cap is stale evidence: the
    crash loop it measured ended, so a fresh crash starts over at seconds.
    Running priors never decay - their stamp is pass start, and a pass that
    outlives the cap before dying must still step the backoff.
    """
    now = time.time() if now is None else now
    if prior.get("state") not in ("failed", "running"):
        return 1
    try:
        failures = int(prior.get("failures") or 0)
        stamp = float(prior.get("finished_at") or prior.get("started_at") or 0)
    except (TypeError, ValueError):
        return 1
    if failures < 0 or not stamp:
        return 1
    # A running prior's stamp is pass START: duration is not quiet time, so
    # only a finished failure can decay as stale (death stamps finished_at).
    if prior.get("state") == "failed":
        if now - stamp > 2 * BOOTSTRAP_RETRY_S:
            return 1
        failures = max(1, failures)
    return failures + 1


def stale_embed_failure(record: dict, now: float | None = None) -> bool:
    """A FAILED record older than twice the retry cap is stale evidence.

    Any persistent crash loop restamps finished_at on every retry, well inside
    that window - the same law embed_failure_streak applies to backoff - so an
    old, unrefreshed failure measured a loop that ended.
    """
    if record.get("state") != "failed":
        return False
    try:
        stamp = float(record.get("finished_at") or 0)
    except (TypeError, ValueError):
        return False
    if stamp <= 0:
        return False
    now = time.time() if now is None else now
    return now - stamp > 2 * BOOTSTRAP_RETRY_S


def embed_failure_superseded(
        record: dict, coherence: dict | None = None,
        now: float | None = None) -> bool:
    """Newer evidence contradicts a recorded build failure.

    A fully published lane owes no rows, so the failure that once explained a
    gap explains nothing now; and a record past the staleness law above
    measured a crash loop that ended. Either way surfaces must not report it.
    """
    if record.get("state") != "failed":
        return False
    if (coherence is not None and coherence.get("coherent")
            and not coherence.get("migration_pending")):
        return True
    return stale_embed_failure(record, now)


def clear_superseded_embed_failure(coherence: dict) -> None:
    """Drop a failure record a fully published lane contradicts.

    Only complete coverage clears: a partial or stale lane keeps its record
    because the failure may be exactly why the gap is not closing. Absence is
    the honest replacement - no state is fabricated for a pass that never ran.
    """
    if not coherence.get("coherent") or coherence.get("migration_pending"):
        return
    if read_embed_state().get("state") != "failed":
        return
    if _data_dir_readonly():
        return
    try:
        embed_state_path().unlink(missing_ok=True)
    except OSError:
        pass


def bootstrap_backoff_s(prior: dict) -> float:
    """Stepped crash backoff: doubling from BOOTSTRAP_RETRY_BASE_S, capped."""
    try:
        failures = max(1, int(prior.get("failures") or 1))
    except (TypeError, ValueError):
        failures = 1
    return float(min(BOOTSTRAP_RETRY_S,
                     BOOTSTRAP_RETRY_BASE_S * (1 << min(failures - 1, 30))))


def embed_running() -> bool:
    try:
        rec = json.loads(embed_claim_path().read_text(encoding="utf-8"))
        pid = int(rec.get("pid") or 0)
        if pid <= 0 or not common.pid_alive(pid):
            return False
        expected_start = rec.get("process_start")
        if not expected_start:
            return False  # a recycled PID cannot inherit a legacy claim
        actual_start = common.process_start_identity(pid)
        return actual_start is not None and actual_start == expected_start
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _embedding_refresh_disabled() -> dict | None:
    if common.setting("embeddings") == "off":
        return {
            "state": "disabled",
            "reason": "embeddings=off disables semantic embedding refresh",
        }
    return None


def _background_refresh_disabled() -> dict | None:
    if removal_fence.background_removal_active():
        return {
            "state": "disabled",
            "reason": "agrep removal blocks background semantic refresh",
        }
    disabled = _embedding_refresh_disabled()
    if disabled is not None:
        return disabled
    if not os.environ.get("AGREP_NO_DAEMON"):
        return None
    return {
        "state": "disabled",
        "reason": "AGREP_NO_DAEMON disables background semantic refresh",
    }


def ensure_fresh_async(max_new: int | None = None, *,
                       spawn_env: dict[str, str] | None = None,
                       ignore_battery: bool = False,
                       force_full: bool = False,
                       allow_model_download: bool = False) -> dict:
    """Spawn one detached embed.py when the lane is stale. Idempotent and cheap:
    embed.py's own claim dedupes concurrent spawns; a recent failure backs off
    stepped (bootstrap_backoff_s) instead of crash-looping - one crash costs
    seconds, only a persistent failure streak earns the BOOTSTRAP_RETRY_S cap."""
    refusal = _mutation_refusal()
    if refusal is not None:
        return refusal
    disabled = _background_refresh_disabled()
    if disabled is not None:
        return disabled
    if not runtime_dependencies_available():
        return _runtime_unavailable()
    force_full = bool(force_full or integrity_rebuild_requested())
    # Retry the millisecond-scale publication window before falling back.
    coh = embedding_coherence()
    for attempt in range(3):
        if coh.get("state") not in ("unstable-source", "unstable-embeddings"):
            break
        time.sleep(0.005 * (attempt + 1))
        coh = embedding_coherence()
    if coh.get("state") == "legacy-publication":
        return {
            "state": "legacy-publication",
            "reason": str(coh.get("reason") or LEGACY_PUBLICATION_REPAIR),
            "repair": LEGACY_PUBLICATION_REPAIR,
            "coherence": coh,
        }
    if (coh.get("coherent") and not coh.get("migration_pending")
            and not force_full):
        # a complete lane contradicts any lingering failure record; without
        # this, a crash that followed a successful publish reads as failed
        # forever (no later pass runs to overwrite it)
        clear_superseded_embed_failure(coh)
        return {"state": "ready", "coherence": coh}
    if embed_running():
        return {"state": "running", "coherence": coh}
    if not allow_model_download:
        try:
            import embedder
            embedder.ensure_model(download=False)
        except Exception as exc:  # noqa: BLE001 -- automatic refresh stays offline
            return {
                "state": "model-not-cached",
                "reason": str(exc),
                "coherence": coh,
            }
    prior = read_embed_state()
    if prior.get("state") == "running":
        # dead claim + "running" state = the worker died without publishing
        # (signal/OOM skips the except handler). Synthesize the failure so a
        # deterministic native crash backs off like a Python one.
        prior = {"state": "failed", "finished_at": time.time(),
                 "failures": embed_failure_streak(prior),
                 "reason": f"worker died without publishing "
                           f"(pid {prior.get('pid')})"}
        try:
            embed_state_path().write_text(json.dumps(prior), encoding="utf-8")
        except OSError:
            pass
    if prior.get("state") == "failed":
        backoff_s = bootstrap_backoff_s(prior)
        retry_at = float(prior.get("finished_at") or 0) + backoff_s
        if time.time() < retry_at:
            return {**prior, "retry_at": retry_at, "backoff_s": backoff_s,
                    "coherence": coh}
    cmd = [sys.executable, str(common.PY_DIR / "embed.py")]
    cmd.append("--background")
    if not allow_model_download:
        cmd.append("--no-model-download")
    if force_full or _needs_unverified_bundle_rebuild():
        # A no-hash bundle cannot prove that retained text still matches its vectors.
        cmd.append("--full")
    if max_new:
        cmd += ["--max-new", str(max_new)]
    logf = common.open_bounded_log("semantic-embed.log")
    child_env = dict(os.environ)
    child_env.pop("AGREP_EMBED_IGNORE_BATTERY", None)
    if ignore_battery:
        child_env["AGREP_EMBED_IGNORE_BATTERY"] = "1"
    if spawn_env:
        # Keep the scheduling surface deliberately closed: callers may choose
        # resource policy, never arbitrary child configuration.
        for name in ("AGREP_SEM_THREADS", "AGREP_SEM_BG_NICE",
                     "AGREP_SEM_BG_POLICY"):
            if name in spawn_env:
                child_env[name] = str(spawn_env[name])
    child_env[common.LOG_STAMP_ENV] = "1"
    kw = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
          "cwd": str(common.REPO_ROOT), "env": child_env, "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = common.windows_background_child_flags(0x08004208)
    else:
        kw["start_new_session"] = True
    try:
        disabled = _background_refresh_disabled()
        if disabled is not None:
            return {**disabled, "coherence": coh}
        try:
            subprocess.Popen(cmd, **kw)
        except PermissionError as exc:
            raise SemanticRefreshSpawnDenied(
                "semantic refresh process launch was denied") from exc
    finally:
        logf.close()
    return {"state": "running", "coherence": coh}


def ensure_refs_async() -> dict:
    """Prepare missing candidate metadata out of the requesting agent's path.

    The refs-only child shares embed.py's cross-process claim with vector writers,
    so a moved bundle is never paired with an obsolete sidecar and query bursts do
    not stack 10x transcript scans. The current query falls back to keyword while
    this derived artifact is prepared.
    """
    refusal = _mutation_refusal()
    if refusal is not None:
        return refusal
    disabled = _background_refresh_disabled()
    if disabled is not None:
        return disabled
    if not runtime_dependencies_available():
        return _runtime_unavailable()
    if integrity_rebuild_requested():
        return ensure_fresh_async(force_full=True)
    if embed_running():
        return {"state": "running"}
    cmd = [sys.executable, str(common.PY_DIR / "embed.py"),
           "--background", "--refs-only"]
    logf = common.open_bounded_log("semantic-embed.log")
    kw = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
          "cwd": str(common.REPO_ROOT),
          "env": {**os.environ, common.LOG_STAMP_ENV: "1"}, "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = common.windows_background_child_flags(0x08004208)
    else:
        kw["start_new_session"] = True
    try:
        disabled = _background_refresh_disabled()
        if disabled is not None:
            return disabled
        subprocess.Popen(cmd, **kw)
    finally:
        logf.close()
    return {"state": "running"}


def refresh_embeddings_sync(max_new: int | None = None,
                            timeout_s: float = 3600.0) -> dict:
    """Run one embed pass to completion in a bounded child (indexer + reindex path)."""
    refusal = _mutation_refusal()
    if refusal is not None:
        return {"ok": False, **refusal}
    disabled = _embedding_refresh_disabled()
    if disabled is not None:
        return {"ok": False, **disabled}
    if not runtime_dependencies_available():
        return {"ok": False, **_runtime_unavailable()}
    force_full = integrity_rebuild_requested()
    coh = embedding_coherence()
    if (coh.get("coherent") and not coh.get("migration_pending")
            and not force_full):
        return {"ok": True, "reason": "already current", "coherence": coh}
    if embed_running():
        return {"ok": False, "reason": "another embed owns the claim",
                "coherence": coh}
    cmd = [sys.executable, str(common.PY_DIR / "embed.py")]
    if force_full or _needs_unverified_bundle_rebuild():
        cmd.append("--full")
    if max_new:
        cmd += ["--max-new", str(max_new)]
    with common.open_bounded_log("semantic-embed.log") as logf:
        kw = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
              "cwd": str(common.REPO_ROOT),
              "env": {**os.environ, common.LOG_STAMP_ENV: "1"}}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08004000  # NO_WINDOW | BELOW_NORMAL
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kw)
        try:
            rc = proc.wait(timeout=max(1.0, timeout_s))
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "reason": "embed pass timed out",
                    "coherence": embedding_coherence()}
    after = embedding_coherence()
    return {"ok": rc == 0, "reason": f"embed exited {rc}", "coherence": after}


# ---- the in-process search lane ----

def available() -> bool:
    """Whether optional runtime specs and nonempty vector artifacts are present."""
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if not runtime_dependencies_available() or not meta.exists():
        return False
    try:
        record = json.loads(meta.read_bytes())
        if isinstance(record, dict) and record.get("version") == 2:
            import embedding_segments
            return int(embedding_segments.load_manifest(meta)["live_rows"]) > 0
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return all(path.exists() and path.stat().st_size > 0
               for path in (common.EMBEDDINGS_PATH, common.IDS_PATH))


def last_use_mono() -> float:
    return _LAST_USE["mono"]


def release() -> bool:
    """Unconditionally release semantic model and artifact ownership."""
    ask_module = sys.modules.get("ask")
    embedder_module = sys.modules.get("embedder")
    segments_module = sys.modules.get("embedding_segments")
    model_loaded = (
        bool(embedder_module.model_loaded())
        if embedder_module is not None else False)
    if ask_module is not None:
        ask_module.clear_artifact_cache()
    if model_loaded:
        embedder_module.release()
    if segments_module is not None and _mutation_refusal() is None:
        try:
            import semantic_q8
            segments_module.prune_legacy_layout(
                embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH,
                q8_manifest_path=semantic_q8.MANIFEST_PATH,
                q8_artifact_dir=semantic_q8.ARTIFACT_DIR,
                generation_marker_path=generation_marker_path(),
            )
        except (OSError, RuntimeError, ValueError, TypeError,
                json.JSONDecodeError):
            pass
    _LAST_USE["mono"] = 0.0
    if ask_module is not None or segments_module is not None:
        common.dbg(
            "semantic lane released artifact caches"
            + (" + model" if model_loaded else ""))
    return True


def release_if_idle(idle_s: float) -> bool:
    """Drop the model + artifact caches after idle_s without a semantic query.
    Resident workers call this; one-shot CLIs simply exit."""
    if not _LAST_USE["mono"] or time.monotonic() - _LAST_USE["mono"] <= idle_s:
        return False
    return release()


def semantic_use_path() -> Path:
    return common.DATA_DIR / ".semantic-use.beat"


def note_semantic_use() -> None:
    """Cheap cross-process demand signal for background refs prewarming."""
    if _mutation_refusal() is not None:
        return
    try:
        semantic_use_path().touch()
    except OSError:
        pass


def semantic_recently_used(max_age_s: float = SEMANTIC_REFS_DEMAND_S) -> bool:
    try:
        age = time.time() - semantic_use_path().stat().st_mtime
        return 0.0 <= age <= max_age_s
    except OSError:
        return False


def _deterministic_integrity_error(error: BaseException) -> bool:
    """Damage requires a rebuild; a concurrent republication requires a retry.

    A transient marker anywhere in the chain wins: ambiguous evidence must never
    be converted into a corruption verdict about the user's data."""
    import embedding_segments
    import segment_query
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, (
            segment_query.SegmentArtifactMoved,
            embedding_segments.SegmentPublicationRace)) for item in chain):
        return False
    return any(isinstance(item, (
        embedding_segments.SegmentError,
        segment_query.SegmentIntegrityError)) for item in chain)


def _transient_semantic_publication_error(error: BaseException) -> bool:
    """Whether a query failed because a newer semantic publication won.

    These typed failures are concurrency evidence, not permission to retry an
    arbitrary runtime error.  A deterministic integrity error, missing model,
    or unavailable source therefore never enters this path.
    """
    import embedding_segments
    import segment_query

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (
                segment_query.SegmentArtifactMoved,
                embedding_segments.SegmentPublicationRace)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _await_publishing_coherence(initial: dict) -> dict:
    """Wait briefly for a verified corpus publication to rebind semantics.

    Strict source equality remains the gate: this returns a searchable record
    only after ``embedding_coherence`` proves it.  Persistent stale/missing,
    model, profile, and integrity states return immediately when no live corpus
    publisher accounts for them.
    """
    transient = frozenset(("stale", "unstable-source", "unstable-embeddings"))
    if (initial.get("searchable", initial.get("coherent", False))
            or initial.get("state") not in transient):
        return initial
    deadline = time.monotonic() + SEMANTIC_PUBLICATION_WAIT_S
    coherence = initial
    # Preserve the lock-independent fast path: an atomic manifest replacement
    # need not overlap the corpus lock. This exact schedule remains the reader's
    # bounded race tolerance before verified-publisher waiting begins.
    if coherence.get("state") in ("unstable-source", "unstable-embeddings"):
        for delay in SEMANTIC_UNSTABLE_SETTLE_DELAYS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return coherence
            time.sleep(min(delay, remaining))
            coherence = embedding_coherence()
            if coherence.get("searchable", coherence.get("coherent", False)):
                return coherence
            if coherence.get("state") not in (
                    "unstable-source", "unstable-embeddings"):
                break
        if coherence.get("state") not in transient:
            return coherence
    try:
        import segment_query
        active = bool(segment_query.corpus_update_active())
    except (ImportError, OSError):
        active = False
    if not active:
        return coherence

    released_at: float | None = None
    delay = SEMANTIC_PUBLICATION_POLL_MIN_S
    while True:
        now = time.monotonic()
        if now >= deadline:
            return coherence
        time.sleep(min(delay, deadline - now))
        coherence = embedding_coherence()
        if coherence.get("searchable", coherence.get("coherent", False)):
            return coherence
        if coherence.get("state") not in transient:
            return coherence
        try:
            active = bool(segment_query.corpus_update_active())
        except OSError:
            active = False
        now = time.monotonic()
        if active:
            released_at = None
        elif released_at is None:
            released_at = now
        elif now - released_at >= SEMANTIC_PUBLICATION_GRACE_S:
            return coherence
        delay = min(delay * 2, SEMANTIC_PUBLICATION_POLL_MAX_S)


def _query_corpus_update_active() -> bool:
    """Observe the corpus publication owner without making it required."""
    try:
        import segment_query
        return bool(segment_query.corpus_update_active())
    except (ImportError, OSError, RuntimeError):
        return False


def wait_for_query_recovery(*, timeout_s: float | None = None) -> dict:
    """Wait only while verified local state proves convergence is possible.

    This is called after an automatic query has already returned an exactly
    classified transient. A healthy searchable generation returns immediately;
    stale or unstable state is polled only while the corpus publisher or the
    embedding publisher is demonstrably alive. Missing, corrupt, mismatched, or
    unexplained stale state therefore never becomes a fake success or a fixed
    sleep on every command.
    """
    wait_s = bounded_query_recovery_wait_s(
        SEMANTIC_QUERY_RECOVERY_WAIT_S if timeout_s is None else timeout_s)
    deadline = time.monotonic() + wait_s
    waited = False
    while True:
        coherence = embedding_coherence()
        searchable = bool(
            coherence.get("searchable", coherence.get("coherent", False)))
        coverage = coherence.get("coverage")
        complete_current = bool(
            searchable
            and coherence.get("coherent") is True
            and coherence.get("state") == "current"
            and isinstance(coverage, dict)
            and coverage.get("complete") is True
            and type(coverage.get("indexed")) is int
            and type(coverage.get("total")) is int
            and type(coverage.get("pending")) is int
            and coverage["indexed"] == coverage["total"]
            and coverage["pending"] == 0)
        corpus_active = _query_corpus_update_active()
        # Only a verified complete/current generation bypasses an active publisher.
        # Partial generations wait; query pinning and one typed reopen still catch
        # publication that wins after this observation.
        if searchable and (not corpus_active or complete_current):
            return {
                "state": "ready", "waited": waited,
                "coherence": coherence,
            }
        embed_active = embed_running()
        if not (corpus_active or embed_active):
            return {
                "state": "not-converging", "waited": waited,
                "coherence": coherence,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return {
                "state": "timeout", "waited": waited,
                "coherence": coherence,
            }
        waited = True
        time.sleep(min(SEMANTIC_QUERY_RECOVERY_POLL_S, remaining))


def _reset_transient_semantic_readers() -> None:
    """Drop generation-pinned readers before one typed movement retry."""
    import segment_query
    segment_query.close_cache()
    try:
        import semantic_q8
        semantic_q8.close_scanner()
    except ImportError:
        pass


def integrity_verdict_escalates(integrity: dict) -> bool:
    """Whether dropped rows prove the BUNDLE is untrustworthy.

    Rows absent from the deliberately-stale corpus mirror are coverage, never
    damage; a text mismatch escalates only once it dominates the page."""
    if str(integrity.get("state") or "") == "rows-uncorroborated":
        return False
    dropped = int(integrity.get("dropped") or 0)
    mismatched = int(integrity.get("mismatched", dropped) or 0)
    if mismatched < SEMANTIC_INTEGRITY_MIN_MISMATCH:
        return False
    considered = int(integrity.get("considered") or 0)
    return (mismatched >= considered * SEMANTIC_INTEGRITY_MISMATCH_SHARE
            if considered > 0 else True)


def _integrity_unavailable_payload(
        error: BaseException, repair: dict, coherence: dict, level: str,
) -> dict:
    repair_state = str(repair.get("state") or "unknown")
    return {
        "results": [],
        "candidate_sessions": 0,
        "truncated": False,
        "score_kind": "unavailable",
        "level": level,
        "semantic_coverage": coherence.get("coverage"),
        "partial": True,
        "semantic_unavailable": True,
        "semantic_integrity": {
            "state": "generation-rejected",
            "dropped": 0,
            "reason": str(error),
            "repair": (
                "not-requested" if repair_state == "not-requested" else
                "full-rebuild-requested"),
            "repair_state": repair_state,
            "repair_persistent": bool(repair.get("persistent")),
        },
    }


def _legacy_publication_unavailable_payload(coherence: dict, level: str) -> dict:
    reason = str(coherence.get("reason") or (
        "legacy-publication: the transcript predates generation binding; "
        f"{LEGACY_PUBLICATION_REPAIR}"))
    return {
        "results": [],
        "candidate_sessions": 0,
        "truncated": False,
        "score_kind": "unavailable",
        "level": level,
        "semantic_coverage": None,
        "partial": True,
        "semantic_unavailable": True,
        # The existing query seam promotes this structured refusal into the
        # semantic status envelope on every CLI surface.
        "semantic_integrity": {
            "state": "legacy-publication",
            "dropped": 0,
            "reason": reason,
            "repair": LEGACY_PUBLICATION_REPAIR,
        },
    }


def search(query: str, *, level: str = "hybrid", k: int = 10,
           filters: dict | None = None, refresh_if_stale: bool = True,
           timing: bool | None = None,
           allow_model_download: bool = False,
           diagnostic_only: bool = False) -> dict:
    """One semantic query, in-process. Returns the payload dict (results,
    candidate_sessions, score_kind, ...) or raises SemanticUnavailable with a
    reason a caller can log before falling back to keyword."""
    query = (query or "").strip()
    if not query:
        raise ValueError("missing semantic query")
    filters = dict(filters or {})
    refresh_if_stale = bool(refresh_if_stale and not diagnostic_only)
    allow_model_download = bool(allow_model_download and not diagnostic_only)
    timing_enabled = _semantic_timing_enabled() if timing is None else timing
    coherence_started = time.perf_counter()
    # Record intent even when stale so the repair pass can prewarm refs.
    if not diagnostic_only:
        note_semantic_use()
    coh = _await_publishing_coherence(embedding_coherence())
    if coh.get("state") == "corrupt-embeddings":
        reason = str(
            coh.get("reason") or "semantic embedding integrity failed")
        repair = (
            {"state": "not-requested", "persistent": False}
            if diagnostic_only else request_full_rebuild(reason))
        return _integrity_unavailable_payload(
            RuntimeError(reason), repair, coh, level)
    coherence_ms = (time.perf_counter() - coherence_started) * 1000.0
    if coh.get("state") == "legacy-publication":
        return _legacy_publication_unavailable_payload(coh, level)
    if not coh.get("searchable", coh.get("coherent", False)):
        if refresh_if_stale:
            try:
                started = ensure_fresh_async(
                    max_new=SEMANTIC_BOOTSTRAP_MAX_NEW,
                    allow_model_download=allow_model_download)
            except Exception as exc:  # noqa: BLE001 -- keyword fallback stays available
                raise SemanticUnavailable(
                    f"embeddings {coh.get('state')}; refresh failed: "
                    f"{type(exc).__name__}; inspect and repair with "
                    f"`{common.cli_name()} doctor --deep`") from exc
            repaired = started.get("coherence") if isinstance(started, dict) else None
            if (started.get("state") == "ready" and isinstance(repaired, dict)
                    and repaired.get("searchable", repaired.get("coherent", False))):
                coh = repaired
            else:
                raise SemanticUnavailable(_query_unavailable_reason(coh, started))
        else:
            raise SemanticUnavailable(
                f"embeddings {coh.get('state')} for the current generation; "
                f"inspect with `{common.cli_name()} doctor --deep`")
    # Defer partial-coverage continuation until AFTER this answer so background
    # ONNX cannot contend with the resident query's latency-sensitive session.
    continue_maintenance = bool(
        refresh_if_stale
        and (not coh.get("coherent") or coh.get("migration_pending")))
    # Mark before model access so a failed first load stays idle-releasable;
    # the serial owner prevents reap overlap.
    _LAST_USE["mono"] = time.monotonic()
    try:
        import ask
    except ImportError as exc:
        raise SemanticUnavailable(f"semantic deps unavailable: {exc}") from exc
    import embedder
    k = max(1, min(int(k), SEMANTIC_MAX_RESULTS))
    ask_started = time.perf_counter()
    try:
        # Warm the lane the STORE was built with (the default would build an
        # ONNX session the first query replaces); no readable meta yet means
        # warm the default lane rather than fail the query.
        try:
            recorded = common.read_index_meta(
                common.EMBEDDINGS_PATH.parent / "embeddings.meta")[1]
        except (OSError, ValueError, TypeError, KeyError,
                json.JSONDecodeError):
            recorded = None
        embedder.get(download=allow_model_download,
                     lane=embedder.resolve_lane(recorded))
        for attempt in range(2):
            try:
                if level == "hybrid":
                    payload = json.loads(ask.tool_search_hybrid(
                        query, k=k, filters=filters, timing=timing_enabled))
                elif level == "message":
                    payload = json.loads(ask.tool_search_messages(
                        query, k=k, filters=filters, envelope=True))
                elif level == "message-session":
                    payload = json.loads(ask.tool_search_messages(
                        query, k=k, filters=filters,
                        group_session=True, envelope=True))
                elif level == "chats":
                    payload = json.loads(ask.tool_search_chats(
                        query, k=k, filters=filters, envelope=True))
                else:
                    raise ValueError(f"unknown semantic level {level!r}")
                break
            except RuntimeError as exc:
                if attempt or not _transient_semantic_publication_error(exc):
                    raise
                _reset_transient_semantic_readers()
                time.sleep(SEMANTIC_REOPEN_DELAY_S)
                coh = _await_publishing_coherence(embedding_coherence())
                if not coh.get("searchable", coh.get("coherent", False)):
                    raise SemanticUnavailable(
                        f"embeddings {coh.get('state')} after semantic "
                        "republication") from exc
    except ask.CorruptMessageRefs as exc:
        if diagnostic_only:
            return _integrity_unavailable_payload(
                exc, {"state": "not-requested", "persistent": False},
                coh, level)
        ask.invalidate_message_refs()
        repair = request_full_rebuild(str(exc))
        raise SemanticUnavailable(
            f"{exc}; full rebuild {repair['state']}") from exc
    except ask.MessageRefsUnavailable as exc:
        if diagnostic_only:
            raise SemanticUnavailable(
                f"{exc}; diagnostic retrieval made no changes") from exc
        try:
            state = ensure_refs_async().get("state", "running")
        except Exception as launch_exc:  # noqa: BLE001 -- keyword fallback wins
            raise SemanticUnavailable(
                f"{exc}; refs launch failed: {type(launch_exc).__name__}") from launch_exc
        raise SemanticUnavailable(f"{exc}; refs {state}") from exc
    except embedder.EmbedderUnavailable as exc:
        raise SemanticUnavailable(f"embedder unavailable: {exc}") from exc
    except RuntimeError as exc:
        if _transient_semantic_publication_error(exc):
            raise SemanticUnavailable(
                surface.SEMANTIC_INDEX_UPDATE_REASON) from exc
        if _deterministic_integrity_error(exc):
            repair = (
                {"state": "not-requested", "persistent": False}
                if diagnostic_only else request_full_rebuild(str(exc)))
            return _integrity_unavailable_payload(exc, repair, coh, level)
        # Profile-guard and stale-index refusals degrade without destroying the bundle.
        raise SemanticUnavailable(str(exc)) from exc
    ask_ms = (time.perf_counter() - ask_started) * 1000.0
    _LAST_USE["mono"] = time.monotonic()
    payload["level"] = level
    if timing_enabled:
        timing = payload.setdefault("_semantic_timing", {})
        phases = timing.setdefault("phases_ms", {})
        phases["coherence"] = round(coherence_ms, 3)
        timing["semantic_dispatch_ms"] = round(ask_ms, 3)
        timing["coherence_state"] = str(coh.get("state") or "unknown")
    # Message/hybrid tools report the exact generation they validated. Preserve
    # that if a marker-only rebase lands between the outer check and retrieval.
    payload.setdefault("semantic_coverage", coh.get("coverage"))
    payload.setdefault("partial", not coh.get("coherent", False))
    integrity = payload.get("semantic_integrity")
    if isinstance(integrity, dict) and int(integrity.get("dropped") or 0) > 0:
        if integrity_verdict_escalates(integrity):
            repair = (
                {"state": "not-requested", "persistent": False}
                if diagnostic_only else request_full_rebuild(
                    str(integrity.get("reason") or "integrity")))
            integrity["repair_state"] = repair["state"]
            integrity["repair_persistent"] = repair["persistent"]
        else:
            integrity["repair"] = "coverage-disclosed"
            integrity["repair_state"] = "not-requested"
            integrity["repair_persistent"] = False
    if continue_maintenance:
        try:
            prior = read_embed_state()
            finished = float(prior.get("finished_at") or 0)
            if not finished or time.time() - finished >= SEMANTIC_DEMAND_REFRESH_S:
                ensure_fresh_async(
                    max_new=SEMANTIC_REFRESH_MAX_NEW,
                    allow_model_download=allow_model_download)
        except Exception:  # noqa: BLE001 -- this answer is already complete
            pass
    return payload
