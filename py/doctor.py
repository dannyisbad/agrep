"""agrep doctor - bounded diagnostic: data, index, daemons, dependencies.

Every row answers what it is, what state it's in, and - when something is held
or broken - why, and what fixes it. `agrep status` invokes this bounded report;
`agrep doctor --deep` adds expanded integrity, attribution, and archive proofs
(each probe keeps a safety timeout so a wedged store cannot hang the report).

examples:
  agrep doctor          bounded operational report
  agrep doctor --deep   full integrity, attribution, and archive proofs
  agrep doctor --fix    repair safe faults and prefetch available semantics
  agrep doctor --json   machine-readable bounded report

exit: 0 diagnostic/action complete, 1 requested repair failed, 2 invalid options.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from collections import Counter
from pathlib import Path

import common
import console
import dist
import fileops
import indexd_runtime
import install_lag
import ownerfile
import settings
import surface_policy as surface

ROOT = common.REPO_ROOT
INGEST_BIN = common.ingest_bin()

OK, MISS, OPT, WARN = "ok", "MISSING", "--", "warn"

SEMANTIC_INSTALL_COMMAND = dist.semantic_install_command()
SEMANTIC_INSTALL_ACTION = dist.semantic_install_hint()
SEMANTIC_INSTALL_HINT = (
    "optional; on a supported OS/Python, enable it with "
    f"{SEMANTIC_INSTALL_ACTION}"
)
SEMANTIC_UNLOCK = (
    "enable optional semantic search on a supported OS/Python: "
    f"{SEMANTIC_INSTALL_ACTION}"
)

_DOCTOR_EVIDENCE_VERSION = 1
_DOCTOR_EVIDENCE_NAME = ".doctor-evidence.json"
_DOCTOR_EVIDENCE_MAX_BYTES = 64 * 1024
_SEMANTIC_STATE_MAX_BYTES = 64 * 1024
_QUICK_CHECK_FALLBACK_BYTES_PER_S = 64 * 1024 * 1024
_QUICK_CHECK_PROGRESS_S = 5.0
_QUICK_CHECK_MEMORY: dict[str, dict] = {}
_MODEL_CHECK_MEMORY: dict[str, dict] = {}
# Census parity with the search path's probe (_DRIFT_PROBE_TIMEOUT_S): a real
# 29k-file census costs ~0.3s, so a 0.20s cap starved every healthy run there.
_DIAGNOSTIC_STORE_TIMEOUT_S = 0.45
# the documented --deep safety timeout: a wedged census child must not hang
# the report forever; this cap bounds the hang, not a healthy census.
_DIAGNOSTIC_STORE_DEEP_TIMEOUT_S = 10.0
_DIAGNOSTIC_DETECT_TIMEOUT_S = 0.15
_DIAGNOSTIC_SENTINEL_TIMEOUT_S = 0.15
_DIAGNOSTIC_QUALITY_TIMEOUT_S = 0.15
_DIAGNOSTIC_ARCHIVE_TIMEOUT_S = 0.15
_DIAGNOSTIC_ROUTINE_TIMEOUT_S = 0.80
_DIAGNOSTIC_RENDER_HEADROOM_S = 0.20
_SEMANTIC_QUERY_PROBE_TIMEOUT_S = 1.5

# Emitter and checker share this: only a verdict earns a row. Routine runs
# every bounded in-process check and declines the unbounded ones and those
# loading optional native code; what it declines is absent, never narrated.
_NOT_RUN = frozenset({
    "status-deferred", "budget-exceeded", "not-verified",
    "not-inspected", "not-checked", "skipped",
})


def _ran(observation: object) -> bool:
    if not isinstance(observation, dict):
        return False
    return str(observation.get("state") or "") not in _NOT_RUN


def _concluded(observation: object) -> bool:
    """A verdict a machine may gate on: the check ran and reached an answer.

    Machines are told about both halves of the gap - a check that never ran and
    one that ran without concluding - because neither is green evidence.
    """
    if not _ran(observation):
        return False
    return str(
        (observation or {}).get("state")) not in ("unavailable", "unreadable")


def _cli_command(*args: object) -> str:
    return console.shell_command(
        *dist.cli_invocation(*args), fallback="<agrep command>")


def _command_remedy(name: str, *args: object, **values: object) -> str:
    return surface.render_remedy(
        name, command=_cli_command(*args), **values)


def _routine_deadline(*, deep: bool) -> float | None:
    return (
        None if deep
        else time.monotonic() + _DIAGNOSTIC_ROUTINE_TIMEOUT_S
    )


def _remaining_timeout(
        deadline: float | None, local_cap_s: float,
) -> float:
    """The next optional probe may spend only its cap and the shared remainder."""
    cap = max(0.0, float(local_cap_s))
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - time.monotonic()))


def _budget_observation(what: str) -> dict:
    return {"state": "budget-exceeded", "check": what}


def _budget_detail(observation: dict) -> str:
    """The sentence a caller renders for a check that never ran.

    The observation itself stays detail-free so a deferral can never read
    as a fault with an explanation; callers that must render prose derive
    it here instead of indexing a key that was never set (a 22GB corpus
    exceeded the census budget and doctor died on the KeyError)."""
    what = str(observation.get("check") or "this check")
    return f"{what} exceeded its diagnostic time budget"


def _deep_notice(*, stream=None) -> None:
    target = sys.stdout if stream is None else stream
    print(
        "deep diagnostics: verifying SQLite integrity, full model "
        "attribution, and the archive manifest; this opt-in work may "
        "take time (every probe keeps a safety timeout).",
        file=target,
        flush=True,
    )

_RUST_STAGING_ARTIFACTS = frozenset({
    ".boundary_stats.bin",
    ".changed_sessions",
    ".derived-owner.json",
    ".derived_generation.json",
    ".harness_prefixes.snapshot",
    ".ingest.sig",
    ".ingest_cache.bin",
    ".ingest_cache.bin.journal",
    ".ingest_pending.bin",
    ".source-health.json",
    ".source_absence_pending",
    ".source_snapshot.bin",
    "boundary_stats.json",
    "corpus.db",
    "event_stats.json",
    "intake_stats.json",
    "messages.jsonl",
    "replies.jsonl",
    "session_family.meta.json",
    "sessions.jsonl",
})


def _rust_staging_owner(name: str) -> int | None:
    base, separator, suffix = name.rpartition(".tmp.")
    if not separator or base not in _RUST_STAGING_ARTIFACTS:
        return None
    if base == "corpus.db":
        for ending in ("-journal", "-wal", "-shm"):
            if suffix.endswith(ending):
                suffix = suffix[:-len(ending)]
                break
    parts = suffix.split(".")
    if len(parts) not in (2, 3):
        return None
    limits = (2**32 - 1, 2**128 - 1, 2**64 - 1)
    values = []
    for raw, limit in zip(parts, limits[:len(parts)], strict=True):
        if re.fullmatch(r"[0-9]+", raw) is None:
            return None
        value = int(raw)
        if value > limit:
            return None
        values.append(value)
    return values[0]


def _rust_staging_orphans() -> dict:
    paths = []
    size = 0
    complete = True
    try:
        entries = os.scandir(common.DATA_DIR)
    except OSError as exc:
        return {
            "state": "unavailable", "complete": False,
            "count": None, "bytes": None, "paths": (),
            "detail": f"staging orphan census is unavailable ({type(exc).__name__})",
        }
    with entries:
        for entry in entries:
            pid = _rust_staging_owner(entry.name)
            if pid is None:
                continue
            try:
                if (not entry.is_file(follow_symlinks=False)
                        or common.pid_alive(pid)):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                complete = False
                continue
            paths.append(Path(entry.path))
            size += int(stat.st_size)
    if not complete:
        return {
            "state": "unavailable", "complete": False,
            "count": None, "bytes": None, "paths": tuple(paths),
            "detail": "staging orphan census changed or was unreadable",
        }
    return {
        "state": "complete", "complete": True,
        "count": len(paths), "bytes": size, "paths": tuple(paths),
    }


def _orphan_inventory(
        *, deep: bool = True, deadline: float | None = None) -> dict:
    # Three directory listings, measured at ~1 ms on a 5,700-file store: the
    # routine tier runs this rather than describing why it did not.
    if deadline is not None and time.monotonic() >= deadline:
        return {
            "state": "budget-exceeded", "complete": False,
            "count": None, "bytes": None,
            "corpus": {"count": None, "bytes": None},
            "rust_staging": {"count": None, "bytes": None},
            "embeddings": {"count": None, "bytes": None},
        }
    def complete_group(value: dict) -> dict:
        return {
            "state": "complete", "complete": True,
            "count": int(value["count"]), "bytes": int(value["bytes"]),
        }

    def unavailable_group(label: str, exc: BaseException) -> dict:
        return {
            "state": "unavailable", "complete": False,
            "count": None, "bytes": None,
            "detail": f"{label} orphan census is unavailable "
                      f"({type(exc).__name__})",
        }

    try:
        import corpusdb
        corpus = complete_group(corpusdb.orphan_temp_artifacts())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        corpus = unavailable_group("search database", exc)
    try:
        staging_observation = _rust_staging_orphans()
        staging = (
            {
                key: value for key, value in staging_observation.items()
                if key != "paths"
            }
            if staging_observation.get("complete") is False
            else complete_group(staging_observation)
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        staging = unavailable_group("ingest staging", exc)
    try:
        import embedding_segments
        meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        embeddings = complete_group(embedding_segments.orphan_artifacts(
            meta, allow_missing_manifest=not meta.exists()))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        embeddings = unavailable_group("embedding", exc)
    groups = {
        "corpus": corpus,
        "rust_staging": staging,
        "embeddings": embeddings,
    }
    incomplete = [
        name for name, group in groups.items() if not group.get("complete")]
    if incomplete:
        return {
            "state": "unavailable", "complete": False,
            "count": None, "bytes": None,
            "detail": "orphan census incomplete: " + ", ".join(incomplete),
            **groups,
        }
    return {
        "state": "complete", "complete": True,
        "count": sum(group["count"] for group in groups.values()),
        "bytes": sum(group["bytes"] for group in groups.values()),
        **groups,
    }


def _data_footprint(*, deadline: float | None = None) -> dict:
    """One traversal supplies both total usage and the human bucket detail."""
    buckets: dict[str, int] = {}
    labels = {
        "corpus.db": "search db",
        "events": "events",
        "models": "model",
        "archive": "archive",
        ".ingest_cache.bin": "parse cache",
    }
    files = 0
    total = 0

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def deferred() -> dict:
        return {
            "state": "budget-exceeded", "complete": False,
            "files": None, "bytes": None, "archive_bytes": None,
            "breakdown": "",
        }

    def unavailable() -> dict:
        return {
            "state": "unavailable", "complete": False,
            "files": None, "bytes": None, "archive_bytes": None,
            "breakdown": "",
            "detail": (
                "footprint census is incomplete because an entry changed or "
                "could not be read; no partial total is reported"
            ),
        }

    def bucket(name: str) -> str:
        return (labels.get(name) or
                ("embeddings" if name.startswith("embeddings") else
                 "search db" if name.startswith("corpus.db") else "other"))

    observation_failed = False
    model_cache_key = os.path.normcase(os.path.abspath(
        os.fspath(_model_cache_root())))

    def walk_error(_error: OSError) -> None:
        nonlocal observation_failed
        observation_failed = True

    try:
        if expired():
            return deferred()
        for current, dirs, names in os.walk(
                common.DATA_DIR, topdown=True, followlinks=False,
                onerror=walk_error):
            if expired():
                return deferred()
            # Windows junctions are reparse directories but are not uniformly
            # represented as symlinks. Prune both forms before os.walk can
            # descend outside the bounded data tree.
            kept_dirs = []
            for name in dirs:
                if expired():
                    return deferred()
                candidate = Path(current) / name
                if os.path.normcase(os.path.abspath(
                        os.fspath(candidate))) == model_cache_key:
                    continue
                try:
                    observed = candidate.lstat()
                except OSError:
                    observation_failed = True
                    continue
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (stat.S_ISDIR(observed.st_mode)
                        and not stat.S_ISLNK(observed.st_mode)
                        and not bool(getattr(
                            observed, "st_file_attributes", 0) & reparse)):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in names:
                if expired():
                    return deferred()
                path = Path(current) / name
                try:
                    info = path.lstat()
                    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if (not stat.S_ISREG(info.st_mode)
                            or stat.S_ISLNK(info.st_mode)
                            or bool(getattr(
                                info, "st_file_attributes", 0) & reparse)):
                        continue
                    size = int(info.st_size)
                    relative = path.relative_to(common.DATA_DIR)
                except (OSError, ValueError):
                    observation_failed = True
                    continue
                files += 1
                total += size
                key = bucket(relative.parts[0])
                buckets[key] = buckets.get(key, 0) + size
    except OSError:
        return unavailable()
    if observation_failed:
        return unavailable()
    top = sorted(buckets.items(), key=lambda item: -item[1])[:4]
    parts = ", ".join(
        f"{name} {size / (1024 ** 2):.0f}"
        for name, size in top if size > 1024 ** 2)
    return {
        "state": "complete", "complete": True,
        "files": files,
        "bytes": total,
        "archive_bytes": buckets.get("archive", 0),
        "breakdown": f" ({parts} MiB)" if parts else "",
    }


def _model_cache_root() -> Path:
    override = os.environ.get("AGREP_MODEL_DIR")
    return (Path(override).expanduser() if override
            else common.DEFAULT_DATA_DIR / "models")


def _path_contains(parent: Path, child: Path) -> bool:
    parent_key = os.path.normcase(os.path.abspath(os.fspath(parent)))
    child_key = os.path.normcase(os.path.abspath(os.fspath(child)))
    try:
        return os.path.commonpath((parent_key, child_key)) == parent_key
    except ValueError:
        return False


def _model_cache_footprint(*, deadline: float | None = None) -> dict:
    root = _model_cache_root()

    def result(state: str, *, complete: bool, files=None, bytes_=None,
               detail: str | None = None) -> dict:
        return {
            "state": state, "complete": complete, "path": str(root),
            "files": files, "bytes": bytes_, "detail": detail,
        }

    if deadline is not None and time.monotonic() >= deadline:
        return result("budget-exceeded", complete=False)
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return result("complete", complete=True, files=0, bytes_=0)
    except OSError as exc:
        return result(
            "unavailable", complete=False,
            detail=f"model cache cannot be inspected ({type(exc).__name__}: {exc})")
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or bool(getattr(root_info, "st_file_attributes", 0) & reparse)):
        return result(
            "unavailable", complete=False,
            detail="model cache path is not a plain directory")
    if _path_contains(root, common.DATA_DIR):
        return result(
            "unavailable", complete=False,
            detail=(
                "model cache contains the data directory; its footprint "
                "cannot be separated safely"
            ))

    files = 0
    total = 0
    failed = False

    def walk_error(_error: OSError) -> None:
        nonlocal failed
        failed = True

    try:
        for current, dirs, names in os.walk(
                root, topdown=True, followlinks=False, onerror=walk_error):
            if deadline is not None and time.monotonic() >= deadline:
                return result("budget-exceeded", complete=False)
            try:
                current_info = Path(current).lstat()
            except OSError:
                failed = True
                dirs[:] = []
                continue
            if (not stat.S_ISDIR(current_info.st_mode)
                    or stat.S_ISLNK(current_info.st_mode)
                    or bool(getattr(
                        current_info, "st_file_attributes", 0) & reparse)):
                failed = True
                dirs[:] = []
                continue
            kept_dirs = []
            for name in dirs:
                if deadline is not None and time.monotonic() >= deadline:
                    return result("budget-exceeded", complete=False)
                path = Path(current) / name
                try:
                    info = path.lstat()
                except OSError:
                    failed = True
                    continue
                if (stat.S_ISDIR(info.st_mode)
                        and not stat.S_ISLNK(info.st_mode)
                        and not bool(getattr(
                            info, "st_file_attributes", 0) & reparse)):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in names:
                if deadline is not None and time.monotonic() >= deadline:
                    return result("budget-exceeded", complete=False)
                path = Path(current) / name
                try:
                    info = path.lstat()
                except OSError:
                    failed = True
                    continue
                if (not stat.S_ISREG(info.st_mode)
                        or stat.S_ISLNK(info.st_mode)
                        or bool(getattr(
                            info, "st_file_attributes", 0) & reparse)):
                    continue
                files += 1
                total += int(info.st_size)
    except OSError as exc:
        return result(
            "unavailable", complete=False,
            detail=f"model cache cannot be inspected ({type(exc).__name__}: {exc})")
    if failed:
        return result(
            "unavailable", complete=False,
            detail="model cache changed or contained an unreadable entry")
    return result("complete", complete=True, files=files, bytes_=total)


def _footprint_breakdown() -> str:
    """Compatibility helper for focused callers; report uses one shared walk."""
    return str(_data_footprint()["breakdown"])


_GLYPH = surface.STATUS_GLYPHS


def _row(name: str, status: str, detail: str = "") -> None:
    glyph, code = _GLYPH.get(status, ("?  ", "d"))
    color = common.color_enabled(sys.stdout)
    print(f"  [{common.paint(code, glyph, color)}] {name:<17} {detail}")


def _deep_progress(event: dict) -> None:
    phase = str(event.get("phase") or "")
    if phase == "start":
        size_mib = int(event.get("bytes") or 0) / (1024 ** 2)
        estimate = float(event.get("estimate_s") or 0)
        print(
            f"  [.. ] integrity          PRAGMA quick_check scans "
            f"{size_mib:.1f} MiB; estimate ~{estimate:.1f}s; running ...",
            flush=True,
        )
    elif phase == "running":
        print(
            f"       integrity          still running "
            f"({float(event.get('elapsed_s') or 0):.1f}s elapsed) ...",
            flush=True,
        )
    elif phase == "cached":
        print(
            "  [.. ] integrity          unchanged DB+WAL identity; "
            "using the cached quick_check verdict",
            flush=True,
        )


def _resident_worker_detail(
        resident: dict, *, automatic: bool = True,
        readiness: dict | None = None) -> str:
    readiness = readiness or {}
    if not resident.get("running"):
        if readiness.get("query_serving") is False:
            reason = common.terminal_safe(
                readiness.get("reason") or "reason unavailable")
            return f"not query-serving: {reason}"
        if resident.get("blocked"):
            reason = str(
                resident.get("reason")
                or resident.get("owner_state")
                or "unverifiable")
            return (f"ownership is blocked: {reason}; in-process semantic fallback is "
                    "blocked to avoid duplicate model memory; keyword search "
                    "remains available")
        if resident.get("inprocess"):
            return (f"pid {resident.get('pid')} owns semantic resources "
                    "in-process; resident startup is deferred")
        if resident.get("starting"):
            return (f"pid {resident.get('pid')} is transitioning; semantic searches "
                    "stay single-owner until its endpoint is ready")
        trigger = (
            "the first semantic search (-s, or the automatic zero-hit fallback)"
            if automatic else "an explicit semantic search (-s)")
        return f"starts with {trigger}; exits after idle"
    detail = f"pid {resident['pid']}"
    rss = resident.get("rss_bytes")
    if isinstance(rss, int) and rss >= 0:
        detail += (
            f", {rss / (1024 ** 2):.1f} MiB root RSS; children excluded")
    if readiness.get("query_serving") is True:
        elapsed = readiness.get("roundtrip_ms")
        proof = (f"query round-trip ready in {float(elapsed):.1f}ms"
                 if isinstance(elapsed, (int, float)) else
                 "query round-trip ready")
    elif readiness.get("query_serving") is False:
        proof = ("alive, but the bounded query failed: "
                 + common.terminal_safe(
                     readiness.get("reason") or "reason unavailable"))
    else:
        proof = "alive; query round-trip not verified"
    return detail + f" - {proof}; exits after idle"


def _semantic_readiness_detail(readiness: dict) -> str:
    generation = str(readiness.get("generation_state") or "unknown")
    probe_error = common.terminal_safe(
        readiness.get("generation_probe_error") or "")
    if not readiness.get("current_generation"):
        detail = f"current generation not ready - embeddings {generation}"
        if probe_error:
            detail += f"; {probe_error}"
        refresh = readiness.get("refresh") or {}
        if refresh.get("running"):
            bits = [str(refresh["phase"])] if refresh.get("phase") else []
            if refresh.get("total"):
                bits.append(
                    f"{int(refresh.get('done') or 0):,}/"
                    f"{int(refresh['total']):,} rows")
            detail += "; refresh running"
            if bits:
                detail += f" ({' · '.join(bits)})"
            return detail + "; retry shortly"
        return detail + "; " + _command_remedy(
            "semantic-fix", "doctor", "--fix")
    if readiness.get("query_serving") is True:
        elapsed = readiness.get("roundtrip_ms")
        suffix = (f"; bounded query {float(elapsed):.1f}ms"
                  if isinstance(elapsed, (int, float)) else "")
        probe = f"; {probe_error}" if probe_error else ""
        return f"current generation ready{probe}{suffix}"
    if readiness.get("query_serving") is False:
        return ("current generation ready, but this caller cannot query it: "
                + common.terminal_safe(
                    readiness.get("reason") or "reason unavailable"))
    if readiness.get("reason"):
        return ("current generation ready; query round-trip unverified: "
                + common.terminal_safe(readiness["reason"]))
    return "current generation ready; no resident worker to probe"


def _semantic_failure_detail(
        smart: dict, *, embeddings_off: bool,
        lane: surface.SemanticLanePolicy) -> str:
    why = smart.get("embed_fail_reason")
    return (
        "last build failed" + (f": {why}" if why else "")
        + " - " + ("still searchable on the previous build; "
                   if smart.get("generation_ready", smart["live"]) else "")
        + (f"retries {lane.disabled_tail}"
           if embeddings_off else
           f"{lane.retries} (log: semantic-embed.log)")
    )


def _semantic_failure_visible(
        smart: dict, *, embeddings_off: bool, running: bool = False,
) -> bool:
    return (
        smart.get("embed_job") == "failed"
        and not embeddings_off
        and not running
    )


def _routine_lane_row(snapshot: dict, smart: dict, *,
                      embeddings_off: bool) -> tuple[str, str] | None:
    """A lane fact the routine tier can prove without loading native code.

    `_model_cache_footprint` already runs on the routine surface (it is the
    `model cache` row above): a *completed* probe reporting zero files proves
    the pinned weights are absent, which proves the lane can never build
    itself, because automatic search never downloads them. Same law as the
    recorded-failure row - only a probe that actually ran may speak.
    """
    if smart.get("runtime_state") != "not-inspected" or embeddings_off:
        return None                      # the deep tier owns the richer rows
    cache = snapshot["resources"].get("semantic_model_cache") or {}
    if cache.get("complete") is not True or cache.get("files") != 0:
        return None
    return ("embeddings",
            "never built - the pinned model is not cached and automatic "
            "search never fetches it; `agrep -s <query>` or "
            f"`{_cli_command('setup')}` permits the one-time ~50MB fetch")


def _routine_coverage_row(smart: dict) -> tuple[str, str] | None:
    """The built lane's own row, for the tier that cannot probe its runtime.

    Without this the lane is invisible exactly when it works: routine declines
    to import native wheels, so a healthy box printed nothing about meaning
    search at all. Published coverage is not a runtime probe - it is the same
    bounded artifact read the search path reports its coverage from - so the
    numbers can be spoken here, while serving (which needs the worker) cannot.
    """
    if smart.get("runtime_state") != "not-inspected":
        return None                      # the deep tier owns the richer rows
    try:
        import semantic
        output = semantic.output_generation()
        coverage = (output or {}).get("coverage")
        if not isinstance(coverage, dict):
            return None
        indexed, total = int(coverage["indexed"]), int(coverage["total"])
        bound = output.get("source") == semantic.source_generation()
    except Exception:  # noqa: BLE001 -- an unreadable lane stays unspoken
        return None
    rows = f"{indexed:,}/{total:,} rows embedded"
    if not bound:
        return (OPT, f"{rows} · published from an older index generation; "
                     "a background pass refreshes them")
    building = " · a build is running" if smart.get("embed_running") else ""
    if indexed < total:
        return (OPT, f"{rows} · current index; "
                     f"background passes close the gap{building}")
    return (OK, f"{rows} · current index{building}")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _dep_present(mod: str) -> bool | None:
    """Importable in THIS python, without importing it (find_spec is stat-cheap)."""
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:  # noqa: BLE001 -- finder failure is not proof of absence
        return None


def _routine_semantic_job() -> dict:
    """Read only the bounded historical failure record; never load its runtime."""
    path = common.DATA_DIR / ".semantic-embed-state.json"
    try:
        record = json.loads(ownerfile.snapshot(
            path, max_bytes=_SEMANTIC_STATE_MAX_BYTES).raw.decode("utf-8"))
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        return {}
    if not isinstance(record, dict) or record.get("state") != "failed":
        return {}
    import semantic
    if semantic.embed_failure_superseded(record):
        # routine cannot probe coherence, but the staleness law needs no
        # runtime: an unrefreshed old failure measured a crash loop that ended
        return {}
    reason = record.get("reason") or record.get("error")
    return {
        "embed_job": "failed",
        "embed_fail_reason": (
            common.terminal_safe(reason) if reason is not None else None),
    }


def _store_embedding_identity() -> str | None:
    """The identity string the published rows were written under."""
    try:
        return common.read_index_meta(
            common.EMBEDDINGS_PATH.parent / "embeddings.meta")[1]
    except Exception:  # noqa: BLE001 -- a missing/odd store is a diagnostic state
        return None


def _store_embedding_lane() -> str | None:
    """Which engine wrote the published rows, per the store's own identity.

    Worth a row of its own because the two lanes rank the same corpus slightly
    differently: knowing which one a store holds is what makes "this box cannot
    serve it" a readable answer rather than a mystery refusal.
    """
    try:
        import embedder
        return embedder.lane_of(_store_embedding_identity())
    except Exception:  # noqa: BLE001 -- a missing/odd store is a diagnostic state
        return None


# embedder.lane_of owns this mapping but costs numpy; the routine tier parses
# only the suffix (CPU lane is the bare profile string, so no suffix = no row).
_LANE_IDENTITY_SUFFIX = ":lane-"


def _store_lane_notice() -> str | None:
    """The disclosure a store built on a non-default lane owes its reader.

    Without this row, reading embeddings.meta by hand is the only way to learn
    that a store holds the faster engine's vectors - which agree with the CPU
    lane to ~0.9993 and therefore still rank near-threshold queries differently.
    """
    identity = _store_embedding_identity()
    if not identity:
        return None
    _, _, lane = str(identity).partition(_LANE_IDENTITY_SUFFIX)
    if not lane:
        return None
    return (f"store built on the {lane} lane (experimental): near-threshold "
            "results may differ from the default cpu lane")


def _semantic_probe(*, deep: bool = True, fix: bool = True) -> dict:
    """Semantic state; public routine callers explicitly select the cheap tier."""
    if not deep:
        # Never a confident claim from an unprobed tier: live/model_cached
        # speak the unknown vocabulary (None), and only artifact-backed facts
        # (the worker claim, the failure record) may say anything stronger.
        routine = {
            "live": None,
            "available": None,
            "runtime_verified": False,
            "runtime_state": "not-inspected",
            "optional": True,
            "install_hint": None,
            "unavailable_reason": None,
            "deps": {
                "numpy": None, "onnxruntime": None, "tokenizers": None},
            "model_cached": None,
            "model_integrity": {
                "state": "not-checked", "checked": False,
                "verified": False, "cached": False,
            },
            "embedding_lane": None,
            "embeddings": "not-verified",
            "embedding_integrity": {
                "state": "not-checked", "verified": False},
            "embedding_coverage": None,
            "embed_job": "not-inspected",
            "embed_fail_reason": None,
            "embed_pid": None,
            "embed_phase": None,
            "embed_done": None,
            "embed_total": None,
            "embed_running": None,
            "resident_worker": {
                "running": False, "state": "status-deferred"},
            "query_readiness": {
                "state": "not-inspected", "ready": None,
                "query_serving": None,
            },
        }
        routine.update(_routine_semantic_job())
        try:
            import semantic
            running = (semantic.embed_running()
                       and semantic.read_embed_state().get("state") == "running")
            routine["embed_running"] = bool(running)
            if running:
                routine["embed_job"] = "running"
        except Exception:  # noqa: BLE001 -- unknown stays None, never a guess
            pass
        return routine
    deps = {m: _dep_present(m) for m in ("numpy", "onnxruntime", "tokenizers")}
    dependency_probe_unavailable = any(value is None for value in deps.values())
    deps_ready = all(value is True for value in deps.values())
    runtime_ready = deps_ready
    runtime_verified = not deps_ready and not dependency_probe_unavailable
    runtime_state = (
        "dependency-discovery-unavailable" if dependency_probe_unavailable
        else "missing-optional-dependencies" if not deps_ready
        else "detected-unverified" if not deep
        else "verifying")
    unavailable_reason = (
        None if deps_ready
        else "optional-dependency-discovery-unavailable"
        if dependency_probe_unavailable
        else "missing-optional-dependencies")
    model_cached = False
    model_integrity = {
        "state": "not-checked", "checked": False,
        "verified": False, "cached": False,
    }
    coherence = {"state": "unknown"}
    integrity = {"state": "not-checked", "verified": False}
    embed_state = {}
    embed_live = False
    resident = {"running": False}
    query_status = {
        "state": "not-ready", "ready": False,
        "query_serving": False,
    }
    generation_ready = False
    generation_readiness = {
        "state": "not-ready", "generation_state": "unknown",
        "current_generation": False, "complete": False,
        "coverage": None,
        "generation_probe_state": "not-inspected",
        "refresh": {"state": "idle", "running": False,
                    "phase": None, "done": None, "total": None},
    }
    if deps_ready and deep:
        try:
            # Discovery alone is not availability: native wheels can be present
            # yet unloadable on an unsupported CPU/OS or after a partial upgrade.
            import importlib
            for dependency in deps:
                importlib.import_module(dependency)
            import embedder
            import semantic
        except Exception:  # noqa: BLE001 -- broken optional native runtime is unavailable
            runtime_ready = False
            runtime_verified = True
            runtime_state = "load-failed"
            unavailable_reason = "optional-dependency-import-failed"
        else:
            runtime_verified = True
            runtime_state = "verified"
            model_cached, model_integrity = _model_cache_probe(
                embedder, deep=deep)
            try:
                coherence = semantic.embedding_coherence()
                meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
                if deep and meta.exists():
                    try:
                        integrity = semantic.verify_embedding_integrity()
                    except Exception as exc:  # noqa: BLE001 -- report the artifact failure
                        reason = f"{type(exc).__name__}: {exc}"
                        repair = (
                            semantic.request_full_rebuild(reason, launch=False)
                            if fix else {"state": "not-requested"}
                        )
                        integrity = {
                            "state": "corrupt", "verified": False,
                            "reason": reason, "repair": repair,
                        }
                        coherence = {
                            "coherent": False, "searchable": False,
                            "state": "corrupt-embeddings",
                            "reason": reason, "basis": None,
                        }
                embed_state = semantic.read_embed_state()
                if semantic.embed_failure_superseded(embed_state, coherence):
                    # a complete lane (or the staleness law) contradicts the
                    # recorded failure; never report what newer evidence refutes
                    embed_state = {"state": "idle"}
                # a refs-only child holds the claim without publishing; its
                # window must not render the previous pass's numbers as live
                embed_live = (semantic.embed_running()
                              and embed_state.get("state") == "running")
                generation_readiness = {
                    **semantic.query_readiness(coherence),
                    "generation_probe_state": "verified",
                }
            except Exception as exc:  # noqa: BLE001 -- expose a failed probe
                inferred_ready = bool(
                    runtime_ready and coherence.get(
                        "searchable", coherence.get("coherent", False)))
                generation_readiness = {
                    **generation_readiness,
                    "state": "ready" if inferred_ready else "not-ready",
                    "generation_state": str(
                        coherence.get("state") or "unknown"),
                    "current_generation": inferred_ready,
                    "complete": bool(coherence.get("coherent")),
                    "coverage": coherence.get("coverage"),
                    "generation_probe_state": "diagnostic-failed",
                    "generation_probe_error": (
                        "semantic generation-readiness probe failed "
                        f"({type(exc).__name__}: {common.terminal_safe(exc)})"),
                }
            try:
                import semworker
                generation_ready = bool(
                    runtime_ready and coherence.get(
                        "searchable", coherence.get("coherent", False)))
                query_status = semworker.diagnostic_query_status(
                    ready=generation_ready,
                    timeout_s=_SEMANTIC_QUERY_PROBE_TIMEOUT_S)
                resident = query_status.get("resident_after") or resident
            except Exception as exc:  # noqa: BLE001 -- expose a failed probe
                query_status = {
                    "state": "diagnostic-failed",
                    "ready": generation_ready,
                    "query_serving": False,
                    "reason": (
                        "semantic query-readiness probe failed "
                        f"({type(exc).__name__}: {common.terminal_safe(exc)})"),
                }
    return {"live": query_status.get("query_serving") is True,
            "generation_ready": generation_ready,
            "available": runtime_ready,
            "runtime_verified": runtime_verified,
            "runtime_state": runtime_state,
            "optional": True,
            "install_hint": (
                SEMANTIC_INSTALL_HINT
                if runtime_verified and unavailable_reason
                == "missing-optional-dependencies" else None),
            "unavailable_reason": unavailable_reason,
            "deps": deps, "model_cached": model_cached,
            "model_integrity": model_integrity,
            "embedding_lane": _store_embedding_lane(),
            "embeddings": coherence.get("state", "unknown"),
            "embedding_integrity": integrity,
            "embedding_coverage": coherence.get("coverage"),
            "embed_job": embed_state.get("state", "idle"),
            # the exception path publishes "error"; only no-messages uses "reason"
            "embed_fail_reason": embed_state.get("reason") or embed_state.get("error"),
            "embed_pid": embed_state.get("pid"),
            "embed_phase": embed_state.get("phase"),
            "embed_done": embed_state.get("done"),
            "embed_total": embed_state.get("total"),
            "embed_running": embed_live,
            "resident_worker": resident,
            "query_readiness": {
                **generation_readiness,
                **query_status,
            }}


def _source_health_issues() -> list[dict]:
    path = common.DATA_DIR / ".source-health.json"

    def unavailable(reason: str) -> list[dict]:
        return [{
            "agent": "all",
            "path": str(path),
            "kind": "source-health-unavailable",
            "reason": reason,
        }]

    try:
        raw = ownerfile.snapshot(path, max_bytes=1024 * 1024).raw
        record = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return unavailable(f"source health record is unreadable ({exc})")
    if not isinstance(record, dict) or record.get("code") != "source-unreadable":
        return unavailable("source health record is malformed")
    issues = record.get("issues")
    if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
        return unavailable("source health record is malformed")
    return issues


def _store_counts(
        indexed_agents: tuple[str, ...] = (),
        observed: list[dict] | None = None) -> list[dict]:
    """Detected registered stores, with Rust as the single discovery authority."""
    rows = [{
        "agent": str(item.get("name") or ""),
        "found": int(item.get("files") or 0),
        "state": str(item.get("state") or "available"),
        "issues": list(item.get("issues") or []),
    } for item in (common.store_freshness() if observed is None else observed)
        if item.get("name")]
    durable = _source_health_issues()
    global_issues = [issue for issue in durable
                     if str(issue.get("agent") or "all") == "all"]
    by_agent: dict[str, list[dict]] = {}
    for issue in durable:
        agent = str(issue.get("agent") or "all")
        if agent != "all":
            by_agent.setdefault(agent, []).append(issue)
    for row in rows:
        issues = by_agent.pop(row["agent"], []) + global_issues
        if issues:
            row["state"] = "source-unreadable"
            row["issues"].extend(
                issue for issue in issues if issue not in row["issues"])
    rows.extend({"agent": agent, "found": 0, "state": "source-unreadable",
                 "issues": issues}
                for agent, issues in sorted(by_agent.items()))
    if global_issues and not rows:
        rows.append({"agent": "all", "found": 0,
                     "state": "source-unreadable", "issues": global_issues})
    observed = {row["agent"] for row in rows}
    if not global_issues:
        rows.extend({"agent": agent, "found": 0, "state": "absent-indexed",
                     "issues": []}
                    for agent in sorted(set(indexed_agents) - observed))
    return rows


def _source_issue_remedy(issue: dict) -> str:
    kind = str(issue.get("kind") or "")
    if kind == "permission-denied":
        return surface.render_remedy("store-unreadable")
    if kind == "source-read-incomplete":
        return _command_remedy("source-read-incomplete", "index")
    if kind == "not-found":
        return surface.render_remedy("source-not-found")
    if kind == "source-health-unavailable":
        return _command_remedy("source-health-unavailable", "index")
    return _command_remedy("source-inspect", "index")


def _installed_build_detail(observation: dict) -> str:
    detail = str(observation.get("detail") or "provenance unavailable")
    if observation.get("state") != "lagging":
        return detail
    remedy = surface.REMEDIES.get(str(observation.get("remedy") or ""))
    argv = observation.get("remedy_argv")
    if remedy is None or remedy.kind != "consent":
        return detail
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(value, str) for value in argv)):
        return detail
    command = console.shell_command(
        *argv,
        fallback="uv tool install --force --from <verified-local-source> agrep",
    )
    return (
        f"{detail} - "
        f"{surface.render_remedy(str(observation['remedy']), command=command)}")


def _runtime_build_kind() -> str:
    return "source-checkout" if dist._is_dev_checkout() else "installed-package"


def _native_binary_identity(*, deadline: float | None = None) -> dict:
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0.0:
        return {
            "native_binary_build_id": None,
            "native_binary_build_state": "status-deferred",
            "native_binary_build_detail": (
                "routine budget expired before native binary identity"),
        }
    try:
        if remaining is None:
            value = dist.native_binary_build_id(common.ingest_bin())
        else:
            value = dist.bounded_native_binary_build_id(
                common.ingest_bin(), timeout_s=remaining)
    except TimeoutError:
        return {
            "native_binary_build_id": None,
            "native_binary_build_state": "status-deferred",
            "native_binary_build_detail": (
                "routine budget expired during native binary identity"),
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "native_binary_build_id": None,
            "native_binary_build_state": "unavailable",
            "native_binary_build_detail": common.terminal_safe(exc),
        }
    return {
        "native_binary_build_id": value,
        "native_binary_build_state": "verified",
    }


def _runtime_build_detail(identity: dict | None = None) -> str:
    location = common.terminal_safe(Path(__file__).resolve().parent)
    kind = _runtime_build_kind().replace("-", " ")
    if identity is None:
        try:
            distribution = common.distribution_build_id()
        except (OSError, RuntimeError, TypeError, ValueError):
            distribution = "unavailable"
        native_identity = _native_binary_identity()
    else:
        distribution = identity.get("distribution_build_id") or "unavailable"
        native_identity = identity
    native = native_identity.get("native_binary_build_id") or "unavailable"
    return (
        f"{common.package_version()} · distribution {distribution} · runtime "
        f"{indexd_runtime.INDEXD_BUILD_ID} · native {native} · "
        f"{kind} at {location}")


def _stores_row_owns(code: str, snapshot: dict) -> bool:
    """True when the agent-stores row already renders this cause itself.

    Law 5: the store row names the store and the path, so a freshness clause
    repeating it is one cause counted twice. Only a row that will actually
    render may claim ownership - suppressing under a row that stays silent
    would delete the reader's only mention."""
    if code != "source-unreadable" or not snapshot["core"].get("binary"):
        return False
    observation = snapshot["core"].get("store_observation") or {}
    if observation.get("state") != "complete":
        return False
    return any(item["state"] == "source-unreadable"
               for item in snapshot["core"]["stores"])


def _store_status(store_rows: list[dict]) -> tuple[str, str]:
    found = [item["agent"] for item in store_rows if item["found"]]
    unreadable = [
        item for item in store_rows if item["state"] == "source-unreadable"]
    if unreadable:
        details = []
        remedies = []
        for item in unreadable:
            for issue in item["issues"] or ({},):
                path = common.terminal_safe(issue.get("path") or item["agent"])
                details.append(f"{item['agent']} at {path}")
                remedies.append(_source_issue_remedy(issue))
        remedy = "; ".join(dict.fromkeys(remedies))
        suffix = f" - {remedy}" if remedy else ""
        return MISS, "store unreadable: " + "; ".join(details) + suffix
    return (
        OK if found else MISS,
        ", ".join(found) if found
        else "none under ~ - start a supported agent session, then re-run doctor",
    )


def _sqlite_failure(error: BaseException) -> dict:
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 0xff if isinstance(code, int) else None
    # sqlite_errorcode was added after our Python 3.10 floor, but third-party
    # drivers can expose it there too. SQLite's primary BUSY/LOCKED codes are
    # stable ABI values; avoid depending on the newer module constants.
    if primary in (5, 6):
        return {"state": "busy", "detail": "an index writer holds the database"}
    if isinstance(error, sqlite3.Error) and primary is None:
        # Python 3.10 exposes no sqlite_errorcode. These diagnostic strings
        # have been stable in SQLite for decades; classifying by them beats
        # reporting a provably-broken file as merely unavailable.
        message = str(error).lower()
        if any(mark in message for mark in (
                "not a database", "malformed", "vtable constructor",
                "no such table", "corrupt")):
            return {"state": "corrupt",
                    "detail": f"SQLite could not read the database: {error}"}
        if "locked" in message or "busy" in message:
            return {"state": "busy",
                    "detail": "an index writer holds the database"}
        return {
            "state": "unavailable",
            "detail": (
                "SQLite did not expose a structural error code; "
                "busy and corruption cannot be distinguished safely"
            ),
        }
    return {"state": "corrupt",
            "detail": f"SQLite could not read the database: {error}"}


def _integrity_not_verified() -> dict:
    return {
        "state": "not-verified",
        "checked": False,
        "verified": False,
        "cached": False,
    }


def _integrity_skipped(reason: str) -> dict:
    # "skipped" is a not-run state, so this reason is evidence for machines
    # and never a row: the search-db row above already gave the reader it.
    return {
        "state": "skipped",
        "checked": False,
        "verified": False,
        "cached": False,
        "detail": f"not checked because {reason}",
    }


def _doctor_evidence_path() -> Path:
    return common.DATA_DIR / _DOCTOR_EVIDENCE_NAME


def _doctor_evidence_entry_identity(
        path: Path, *, missing_ok: bool) -> tuple[int, ...] | None:
    """Trust only a stable, private regular evidence entry owned by this uid."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        raise OSError(f"doctor evidence is not a plain regular file: {path}")
    getuid = getattr(os, "geteuid", None)
    owner = int(getattr(info, "st_uid", -1))
    if getuid is not None and owner != int(getuid()):
        raise PermissionError(f"doctor evidence is owned by another uid: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if os.name != "nt" and mode & 0o022:
        raise PermissionError(f"doctor evidence is group/world writable: {path}")
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9))),
        mode, owner,
    )


def _regular_file_identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        raise OSError(f"diagnostic evidence target is not a regular file: {path}")
    # NTFS ChangeTime is not reliable on every Windows volume; the shared
    # identity falls back to the Rust USN token or a stable content proof.
    return [
        int(value) for value in fileops.change_sensitive_file_identity(path)]


def _quick_check_identity(path: Path) -> dict:
    identity = _regular_file_identity(path)
    if identity is None:
        raise FileNotFoundError(path)
    return {
        "db": identity,
        "wal": _regular_file_identity(Path(f"{path}-wal")),
    }


def _read_doctor_evidence() -> dict:
    empty = {"version": _DOCTOR_EVIDENCE_VERSION}
    path = _doctor_evidence_path()
    try:
        before = _doctor_evidence_entry_identity(path, missing_ok=False)
        observed = ownerfile.snapshot(
            path, max_bytes=_DOCTOR_EVIDENCE_MAX_BYTES)
        after = _doctor_evidence_entry_identity(path, missing_ok=False)
        if (before != after
                or tuple(observed.identity) != tuple(after[:4])):
            return empty
        value = json.loads(observed.raw.decode("utf-8"))
    except (FileNotFoundError, OSError, RecursionError, UnicodeError,
            ValueError, TypeError):
        return empty
    if (not isinstance(value, dict)
            or value.get("version") != _DOCTOR_EVIDENCE_VERSION):
        return empty
    return value


def _quick_check_memory_key(identity: dict) -> str:
    return (
        os.path.abspath(os.fspath(_doctor_evidence_path())) + "\0"
        + json.dumps(identity, sort_keys=True, separators=(",", ":"))
    )


def _valid_quick_check_record(record: object, identity: dict) -> dict | None:
    if not isinstance(record, dict) or record.get("identity") != identity:
        return None
    rows = record.get("rows")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 3
            or not all(isinstance(row, str) and len(row) <= 1024 for row in rows)):
        return None
    try:
        elapsed_s = float(record.get("elapsed_s"))
        checked_at = float(record.get("checked_at"))
        size = int(record.get("bytes"))
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(elapsed_s) or not math.isfinite(checked_at)
            or elapsed_s < 0 or checked_at < 0 or size < 0):
        return None
    return {
        "identity": identity,
        "rows": list(rows),
        "elapsed_s": elapsed_s,
        "checked_at": checked_at,
        "bytes": size,
    }


def _cached_quick_check(identity: dict) -> dict | None:
    key = _quick_check_memory_key(identity)
    cached = _valid_quick_check_record(_QUICK_CHECK_MEMORY.get(key), identity)
    if cached is not None:
        return cached
    cached = _valid_quick_check_record(
        _read_doctor_evidence().get("quick_check"), identity)
    if cached is not None:
        _QUICK_CHECK_MEMORY[key] = cached
    return cached


def _save_doctor_evidence(section: str, record: dict) -> bool:
    if common.data_dir_readonly(common.DATA_DIR):
        return False
    path = _doctor_evidence_path()
    try:
        expected = _doctor_evidence_entry_identity(path, missing_ok=True)
    except OSError:
        # An untrusted cache is ignored, but never replaced as a side effect.
        return False
    value = _read_doctor_evidence()
    try:
        if _doctor_evidence_entry_identity(path, missing_ok=True) != expected:
            return False
        value["version"] = _DOCTOR_EVIDENCE_VERSION
        value[section] = record
        payload = (json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n").encode("utf-8")
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    if len(payload) > _DOCTOR_EVIDENCE_MAX_BYTES:
        return False
    temporary = common.embedding_temp_path(path, "doctor_evidence")
    try:
        ownerfile.create_exclusive(
            temporary, payload, mode=0o600, fsync=True, exact_mode=True)
        _doctor_evidence_entry_identity(temporary, missing_ok=False)
        if _doctor_evidence_entry_identity(path, missing_ok=True) != expected:
            return False
        common.replace_with_retry(temporary, path)
        _doctor_evidence_entry_identity(path, missing_ok=False)
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _save_quick_check(record: dict) -> bool:
    identity = record["identity"]
    _QUICK_CHECK_MEMORY[_quick_check_memory_key(identity)] = dict(record)
    return _save_doctor_evidence("quick_check", record)


def _model_identity(embedder) -> dict | None:
    files = {}
    root = embedder.model_dir()
    for name, spec in sorted(embedder.PROFILE["files"].items()):
        try:
            expected_size = int(spec[0])
            identity = _regular_file_identity(root / name)
        except (OSError, TypeError, ValueError):
            return None
        if identity is None or int(identity[2]) != expected_size:
            return None
        files[name] = identity
    return {
        "profile": str(embedder.PROFILE["id"]),
        "files": files,
    } if files else None


def _model_memory_key(identity: dict) -> str:
    return (
        os.path.abspath(os.fspath(_doctor_evidence_path())) + "\0model\0"
        + json.dumps(identity, sort_keys=True, separators=(",", ":"))
    )


def _valid_model_record(record: object, identity: dict) -> dict | None:
    if (not isinstance(record, dict) or record.get("identity") != identity
            or type(record.get("verified")) is not bool):
        return None
    try:
        checked_at = float(record.get("checked_at"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(checked_at) or checked_at < 0:
        return None
    return {
        "identity": identity,
        "verified": bool(record["verified"]),
        "checked_at": checked_at,
    }


def _cached_model_check(identity: dict) -> dict | None:
    key = _model_memory_key(identity)
    cached = _valid_model_record(_MODEL_CHECK_MEMORY.get(key), identity)
    if cached is not None:
        return cached
    cached = _valid_model_record(
        _read_doctor_evidence().get("model_sha256"), identity)
    if cached is not None:
        _MODEL_CHECK_MEMORY[key] = cached
    return cached


def _save_model_check(record: dict) -> bool:
    _MODEL_CHECK_MEMORY[_model_memory_key(record["identity"])] = dict(record)
    return _save_doctor_evidence("model_sha256", record)


def _model_cache_probe(embedder, *, deep: bool) -> tuple[bool, dict]:
    identity = _model_identity(embedder)
    if identity is not None:
        cached = _cached_model_check(identity)
        if cached is not None:
            verified = bool(cached["verified"])
            return verified, {
                "state": "verified" if verified else "failed",
                "checked": True,
                "verified": verified,
                "cached": True,
                "detail": "cached model SHA256 verified for unchanged files",
            }
    if not deep:
        return identity is not None, {
            "state": "not-verified",
            "checked": False,
            "verified": False,
            "cached": False,
        }
    try:
        embedder.ensure_model(download=False)
        verified = True
    except Exception:  # noqa: BLE001 -- absent/corrupt model is a diagnostic state
        verified = False
    after = _model_identity(embedder)
    persisted = False
    if after is not None and (identity is None or identity == after):
        record = {
            "identity": after,
            "verified": verified,
            "checked_at": time.time(),
        }
        persisted = _save_model_check(record)
    return verified, {
        "state": "verified" if verified else "failed",
        "checked": True,
        "verified": verified,
        "cached": False,
        "persisted": persisted,
        "detail": (
            "model SHA256 verified" if verified else
            "model SHA256 verification failed"
        ),
    }


def _quick_check_bytes(identity: dict) -> int:
    wal = identity.get("wal")
    return int(identity["db"][2]) + (int(wal[2]) if wal is not None else 0)


def _quick_check_estimate_s(path: Path, identity: dict | None = None) -> float:
    if identity is not None:
        size = _quick_check_bytes(identity)
    else:
        try:
            size = int(path.stat().st_size)
        except OSError:
            size = 0
    prior = _read_doctor_evidence().get("quick_check")
    rate = float(_QUICK_CHECK_FALLBACK_BYTES_PER_S)
    if isinstance(prior, dict):
        try:
            prior_size = int(prior.get("bytes"))
            prior_elapsed = float(prior.get("elapsed_s"))
            if (prior_size > 0 and prior_elapsed > 0
                    and math.isfinite(prior_elapsed)):
                rate = prior_size / prior_elapsed
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            pass
    return max(0.1, size / max(1.0, rate))


def _quick_check_result(
        rows: list[str], *, cached: bool, elapsed_s: float,
        estimate_s: float, persisted: bool = False) -> dict:
    passed = rows == ["ok"]
    if cached:
        detail = (
            "cached PRAGMA quick_check passed for the unchanged database"
            if passed else
            "cached PRAGMA quick_check failure for the unchanged database"
        )
    else:
        detail = (
            f"PRAGMA quick_check passed in {elapsed_s:.2f}s"
            if passed else
            "PRAGMA quick_check failed: "
            + ("; ".join(rows[:3]) or "no result")
        )
    return {
        "state": "verified" if passed else "failed",
        "checked": True,
        "verified": passed,
        "cached": cached,
        "persisted": persisted,
        "elapsed_s": elapsed_s,
        "estimate_s": estimate_s,
        "rows": list(rows),
        "detail": detail,
    }


def _run_quick_check(
        db, path: Path, progress=None,
        *, expected_identity: dict | None = None,
) -> dict:
    try:
        before = _quick_check_identity(path)
    except OSError as error:
        failure = _sqlite_failure(error)
        return {**_integrity_skipped(failure["detail"]), "failure": failure}
    if expected_identity is not None and before != expected_identity:
        return {
            **_integrity_skipped(
                "the database publication changed before the scan"),
            "state": "moved",
        }
    scanned_bytes = _quick_check_bytes(before)
    estimate_s = _quick_check_estimate_s(path, before)
    cached = _cached_quick_check(before)
    if cached is not None:
        try:
            current = _quick_check_identity(path)
        except OSError:
            current = None
        if current != before:
            return {
                **_integrity_skipped(
                    "the database publication changed before the cached verdict"),
                "state": "moved",
            }
        if progress is not None:
            progress({"phase": "cached", "bytes": scanned_bytes,
                      "estimate_s": estimate_s})
        return _quick_check_result(
            cached["rows"], cached=True,
            elapsed_s=float(cached["elapsed_s"]), estimate_s=estimate_s,
            persisted=True)

    if progress is not None:
        progress({"phase": "start", "bytes": scanned_bytes,
                  "estimate_s": estimate_s})
    started = time.monotonic()
    last_notice = started

    def heartbeat() -> int:
        nonlocal last_notice
        now = time.monotonic()
        if progress is not None and now - last_notice >= _QUICK_CHECK_PROGRESS_S:
            last_notice = now
            progress({"phase": "running", "elapsed_s": now - started})
        return 0

    set_progress = getattr(db, "set_progress_handler", None)
    if callable(set_progress):
        set_progress(heartbeat, 50_000)
    try:
        rows = []
        for row in db.execute("PRAGMA quick_check"):
            rows.append(str(row[0]))
            if len(rows) >= 3:
                break
        if not rows:
            rows = ["quick_check returned no result"]
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        failure = _sqlite_failure(error)
        return {
            "state": "error", "checked": False, "verified": False,
            "cached": False, "detail": failure["detail"],
            "failure": failure, "estimate_s": estimate_s,
            "elapsed_s": time.monotonic() - started,
        }
    finally:
        if callable(set_progress):
            set_progress(None, 0)
    elapsed_s = time.monotonic() - started
    try:
        after = _quick_check_identity(path)
    except OSError as error:
        return {**_integrity_skipped(str(error)), "state": "moved"}
    if before != after:
        return {
            **_integrity_skipped("the database publication changed during the scan"),
            "state": "moved", "elapsed_s": elapsed_s,
            "estimate_s": estimate_s,
        }
    record = {
        "identity": after, "rows": rows, "elapsed_s": elapsed_s,
        "checked_at": time.time(), "bytes": _quick_check_bytes(after),
    }
    persisted = _save_quick_check(record)
    if progress is not None:
        progress({"phase": "complete", "elapsed_s": elapsed_s})
    return _quick_check_result(
        rows, cached=False, elapsed_s=elapsed_s,
        estimate_s=estimate_s, persisted=persisted)


class _PostAdoptionClobber(OSError):
    """The one-use legacy adoption proof was spent, then an old writer returned."""


class _DerivedOwnerRefusal(OSError):
    """Another build owns this data directory, so no read snapshot is offered.

    Not a fact about the database: the freshness row already names this cause,
    and the build identities behind it are ours, not the reader's business.
    """


def _post_adoption_clobber_remedy(path: Path) -> str:
    data = common.terminal_safe(path.parent)
    database = common.terminal_safe(path)
    sidecars = common.terminal_safe(path.with_name(f"{path.name}-*"))
    return _command_remedy(
        "post-adoption-clobber", "index", "--full",
        data=data, database=database, sidecars=sidecars,
        verify_command=_cli_command("doctor", "--deep"),
    )


def _open_corpus_diagnostic_snapshot(
        path: Path, *, routine: bool = True):
    """Open corpus.db without attaching SQLite to a protected live WAL."""
    import corpusdb

    # In production these are the same resolved path. Tests may supply an
    # isolated fixture by patching common.DATA_DIR alone; ownership globals do
    # not describe that fixture and therefore do not gate it.
    try:
        canonical = path.resolve(strict=False)
        owned_path = Path(corpusdb.DB_PATH).resolve(strict=False)
    except OSError:
        canonical, owned_path = path, Path(corpusdb.DB_PATH)
    if canonical == owned_path:
        ownership = corpusdb._derived_write_ownership(for_write=True)
        if not ownership.writable:
            sqlite_failure = getattr(ownership, "sqlite_failure", None)
            if sqlite_failure is not None:
                raise sqlite_failure
            if ownership.state == "post-adoption-clobber":
                raise _PostAdoptionClobber(ownership.reason)
            raise _DerivedOwnerRefusal(ownership.reason)
    if routine:
        return corpusdb._connect_read_snapshot(
            path, 0,
            max_clone_bytes=corpusdb._ROUTINE_ALIAS_CLONE_MAX_BYTES)
    return corpusdb._connect_read_snapshot(path, 0)


def _corpus_db_readiness(
        *, deep: bool = False, progress=None,
        generation: dict | None = None) -> dict:
    """Cheap metadata verdict, with the full SQLite scan only on ``deep``.

    The metadata/schema/source verdict deliberately precedes ``quick_check``:
    an obsolete or moving publication already has a precise diagnosis and must
    not make a routine diagnostic pay to scan every database page.
    """
    import corpusdb
    path = common.DATA_DIR / "corpus.db"

    def finish(result: dict, integrity: dict | None = None) -> dict:
        if integrity is None:
            integrity = (
                _integrity_skipped(result.get("detail") or result["state"])
                if deep else _integrity_not_verified()
            )
        return {**result, "integrity": integrity}

    try:
        if not path.is_file():
            return finish({"state": "missing", "detail": "database does not exist"})
        if path.stat().st_size == 0:
            return finish({"state": "corrupt", "detail": "database file is empty"})
    except OSError:
        return finish({
            "state": "corrupt", "detail": "database metadata is unreadable"})

    db = None
    try:
        if generation is None:
            generation = corpusdb.search_generation_health()
        if generation.get("state") in ("torn-generation", "generation-moving"):
            return finish({"state": "stale", "code": generation["state"],
                           "detail": generation["detail"]})
        publication_identity = _quick_check_identity(path) if deep else None
        before = corpusdb._stamp()
        db = _open_corpus_diagnostic_snapshot(path, routine=not deep)
        db.execute("PRAGMA query_only=ON")

        # Verdict-first ordering: schema and required metadata are milliseconds;
        # quick_check is an explicit deep scan and stays below this whole block.
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('schema', 'stamp', 'family_stamp')"))
        if meta.get("schema") != corpusdb._SCHEMA:
            return finish({
                "state": "stale",
                "detail": (
                    f"schema {meta.get('schema') or 'missing'}; "
                    f"expected {corpusdb._SCHEMA}"
                ),
            })
        if "family_stamp" not in meta:
            return finish({
                "state": "corrupt",
                "detail": "session-family metadata is missing",
            })
        db.execute(
            "SELECT agent, model, model_source, who FROM msgs LIMIT 0"
        ).fetchall()
        db.execute(
            "SELECT session, root FROM session_family LIMIT 0"
        ).fetchall()
        after = corpusdb._stamp()
        if not corpusdb._stamps_equal(before, after):
            return finish({
                "state": "stale",
                "detail": "source generation changed during the check",
            })
        family_stamp = common.session_family_source_stamp()
        if family_stamp is None:
            return finish({
                "state": "stale",
                "detail": "session-family source is incomplete or unstable",
            })
        if meta["family_stamp"] != family_stamp:
            return finish({
                "state": "stale",
                "detail": "session-family source is newer than the search database",
            })
        if not corpusdb._stamps_equal(meta.get("stamp", ""), after):
            return finish({
                "state": "stale",
                "detail": "source generation is newer than the search database",
            })
        source_stable = getattr(db, "source_stable", None)
        if source_stable is not None and not source_stable():
            return finish({
                "state": "stale",
                "detail": "database publication changed during the check",
            })
        if corpusdb._query_rebuild_marker_applies(path):
            return finish({"state": "rebuild-pending",
                           "detail": "a full search-database rebuild is pending"})

        ready = {
            "state": "ready",
            "detail": "schema, source generation, and family index current",
        }
        if not deep:
            return finish(ready)
        integrity = _run_quick_check(
            db, path, progress=progress,
            expected_identity=publication_identity)
        if integrity.get("state") == "failed":
            detail = "; ".join(integrity.get("rows") or [])
            return finish({
                "state": "corrupt",
                "detail": f"quick_check failed: {detail or 'no result'}",
            }, integrity)
        if integrity.get("state") == "error":
            return finish(dict(integrity["failure"]), integrity)
        if integrity.get("state") == "moved":
            return finish({
                "state": "stale",
                "detail": "database publication changed during the integrity scan",
            }, integrity)
        return finish(ready, integrity)
    except _PostAdoptionClobber as error:
        reason = common.terminal_safe(error)
        return finish({
            "state": "post-adoption-clobber",
            "code": "post-adoption-clobber",
            "detail": reason,
            "remedy": _post_adoption_clobber_remedy(path),
        })
    except _DerivedOwnerRefusal as error:
        return finish({
            "state": "owned-elsewhere",
            "detail": common.terminal_safe(error),
        })
    except corpusdb.AliasCloneRefused:
        # A non-checkpointed publication larger than the routine clone bound.
        # Refusing to copy a corpus is not a verdict about the database.
        return finish({"state": "budget-exceeded"})
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return finish(_sqlite_failure(error))
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass


def _corpus_quality(
        readiness: dict | None = None, *,
        deep: bool = False,
        timeout_s: float | None = None,
) -> dict:
    readiness = readiness or _corpus_db_readiness()
    out = {
        "n": 0,
        "accountable": 0,
        "with_model": 0,
        "unknown": 0,
        "by_who": Counter(),
        "by_source": Counter(),
        "unknown_by_agent": Counter(),
        "readiness": readiness,
    }
    if readiness.get("state") != "ready":
        return out
    db = None
    expired = False
    deadline = (
        None if timeout_s is None
        else time.monotonic() + max(0.0, timeout_s)
    )
    try:
        path = common.DATA_DIR / "corpus.db"
        db = _open_corpus_diagnostic_snapshot(path, routine=not deep)
        db.execute("PRAGMA query_only=ON")
        set_progress = getattr(db, "set_progress_handler", None)
        if deadline is not None and not callable(set_progress):
            out["readiness"] = {"state": "budget-exceeded"}
            return out

        def heartbeat() -> int:
            nonlocal expired
            expired = (
                deadline is not None and time.monotonic() >= deadline)
            return int(expired)

        if callable(set_progress) and deadline is not None:
            set_progress(heartbeat, 10_000)
        rows = db.execute(
            """
            SELECT
                who AS row_who,
                COALESCE(
                    NULLIF(model_source, ''),
                    CASE
                        WHEN TRIM(COALESCE(model, '')) <> '' THEN 'explicit'
                        ELSE 'unknown'
                    END
                ) AS row_source,
                COALESCE(NULLIF(agent, ''), '?') AS row_agent,
                CASE
                    WHEN TRIM(COALESCE(model, '')) <> '' THEN 1
                    ELSE 0
                END AS has_model,
                COUNT(*)
            FROM msgs
            WHERE who IS NULL OR who NOT IN ('agent', 'tool')
            GROUP BY row_who, row_source, row_agent, has_model
            """
        )
        for who, source, agent, has_model, count in rows:
            count = int(count)
            out["n"] += count
            out["by_who"][who] += count
            out["by_source"][source] += count
            if who != "user":
                continue
            out["accountable"] += count
            if has_model:
                out["with_model"] += count
            else:
                out["unknown"] += count
                out["unknown_by_agent"][agent] += count
        if deadline is not None and time.monotonic() >= deadline:
            expired = True
            out["readiness"] = {"state": "budget-exceeded"}
            return out
        source_stable = getattr(db, "source_stable", None)
        if source_stable is not None and not source_stable():
            out["readiness"] = {
                "state": "stale",
                "detail": "database publication changed during the check",
            }
            return out
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        if expired:
            out["readiness"] = {"state": "budget-exceeded"}
            return out
        out["readiness"] = _sqlite_failure(error)
        return out
    finally:
        if db is not None:
            try:
                set_progress = getattr(db, "set_progress_handler", None)
                if callable(set_progress):
                    set_progress(None, 0)
                db.close()
            except sqlite3.Error:
                pass
    return out


def _archive_probe(
        *, stored_bytes: int | None = None, deep: bool = False,
        timeout_s: float | None = _DIAGNOSTIC_ARCHIVE_TIMEOUT_S,
) -> dict:
    if not deep and (timeout_s is None or timeout_s <= 0.0):
        budget = _budget_observation("archive manifest validation")
        return {
            "enabled": None,
            "state": "status-deferred",
            "detail": _budget_detail(budget),
            "manifest_state": "not-inspected",
            "invalid_records": None,
            "migration_records": None,
            "files": None,
            "raw_bytes": None,
            "stored_bytes": (
                None if stored_bytes is None
                else max(0, int(stored_bytes))),
            "ratio": None,
            "lock": {"state": "not-inspected"},
            "last_pass": {
                "outcome": "unknown", "age_s": None, "fresh": False},
        }
    try:
        import archive
        return archive.status(
            stored_bytes=stored_bytes,
            manifest_timeout_s=(
                None if deep else timeout_s),
        )
    except (OSError, TypeError, ValueError) as exc:
        return {"enabled": None, "state": "capture-blocked",
                "detail": f"archive status is unreadable: {exc}",
                "manifest_state": "unreadable", "files": None,
                "last_pass": {"outcome": "unknown", "age_s": None,
                              "fresh": False}}


def _teach_reconcile_probe(*, deadline: float | None = None) -> dict:
    def budget_expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    # A reconcile that did not run has refused nothing; an empty refusal list
    # is what keeps the render silent instead of listing the non-event.
    def budget_deferred(enrollment: dict | None = None) -> dict:
        return {
            "version": 1, "state": "status-deferred",
            "deferred_kind": "budget-exceeded", "repaired": [],
            "refusals": [], "preserved_newer": [],
            "_enrollment": enrollment or {
                "state": "status-deferred", "targets": None},
        }

    def deferred(enrollment: dict, deferred_kind: str) -> dict:
        return {
            "version": 1, "state": "status-deferred",
            "deferred_kind": deferred_kind, "repaired": [],
            "refusals": [], "preserved_newer": [],
            "_enrollment": enrollment,
        }

    if budget_expired():
        return budget_deferred()
    state_path = common.DATA_DIR / "teach.json"
    try:
        found = state_path.lstat()
        if not stat.S_ISREG(found.st_mode):
            raise OSError("enrollment state is not a regular file")
    except FileNotFoundError:
        return {
            "version": 1, "state": "unenrolled", "repaired": [],
            "refusals": [], "preserved_newer": [],
            "_enrollment": {"state": "unenrolled", "targets": 0},
        }
    except OSError as exc:
        return {
            "version": 1, "state": "unreadable", "repaired": [],
            "refusals": [{
                "path": common.terminal_safe(state_path),
                "kind": "health-unreadable",
                "reason": common.terminal_safe(f"{type(exc).__name__}: {exc}"),
            }],
            "preserved_newer": [],
            "_enrollment": {
                "state": "unreadable", "targets": None,
                "detail": common.terminal_safe(
                    f"{type(exc).__name__}: {exc}"),
            },
        }
    if budget_expired():
        return budget_deferred()
    try:
        # Mirror teach's descriptor-stable 64 KiB contract without importing
        # its reconciliation machinery in the routine tier.
        raw = ownerfile.snapshot(state_path, max_bytes=64 * 1024).raw
        state = json.loads(raw.decode("utf-8"))
        if not isinstance(state, dict):
            raise ValueError("enrollment state is not an object")
        targets = state.get("targets")
        enrollment = (
            {"state": "enrolled", "targets": len(targets)}
            if isinstance(targets, list)
            and all(isinstance(item, str) for item in targets)
            else {
                "state": "unreadable", "targets": None,
                "detail": "enrollment state is malformed or exceeds 64 KiB",
            }
        )
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return {
            "version": 1, "state": "unreadable", "repaired": [],
            "refusals": [{
                "path": common.terminal_safe(common.DATA_DIR / "teach-reconcile.json"),
                "kind": "health-unreadable",
                "reason": common.terminal_safe(f"{type(exc).__name__}: {exc}"),
            }],
            "preserved_newer": [],
            "_enrollment": {
                "state": "unreadable", "targets": None,
                "detail": common.terminal_safe(
                    f"{type(exc).__name__}: {exc}"),
            },
        }
    if budget_expired():
        return deferred(enrollment, "budget-exceeded")
    try:
        import teach
        if budget_expired():
            return deferred(enrollment, "budget-exceeded")
        if deadline is not None:
            try:
                (common.DATA_DIR / teach.RECONCILE_HEALTH).lstat()
            except FileNotFoundError:
                return deferred(enrollment, "routine-tier")
            except OSError:
                pass
            if budget_expired():
                return deferred(enrollment, "budget-exceeded")
        health = teach.reconcile_health()
        if budget_expired():
            return deferred(enrollment, "budget-exceeded")
        if (deadline is not None
                and health.get("state") == "not-checked"):
            return deferred(enrollment, "routine-tier")
        return {**health, "_enrollment": enrollment}
    except Exception as exc:  # noqa: BLE001 -- diagnostics report their own unreadability
        return {
            "version": 1, "state": "unreadable", "repaired": [],
            "refusals": [{
                "path": common.terminal_safe(
                    common.DATA_DIR / "teach-reconcile.json"),
                "kind": "health-unreadable",
                "reason": common.terminal_safe(
                    f"{type(exc).__name__}: {exc}"),
            }],
            "preserved_newer": [],
            "_enrollment": enrollment,
        }


def _report_teach_reconcile(health: dict) -> None:
    def reason(item: dict) -> str:
        if item.get("kind") == "drifted":
            return "target drifted; " + _command_remedy(
                "setup-reconcile", "setup")
        return str(item.get("reason") or "reason unavailable")

    refusals = health.get("refusals") or []
    preserved = health.get("preserved_newer") or []
    state = health.get("state")
    if state == "status-deferred":
        return
    if state == "health-unavailable" and refusals:
        first = refusals[0]
        _row("instructions sync", WARN,
             f"health unavailable: {reason(first)}; repairs still applied")
    elif state in ("refused", "unreadable", "unavailable") and refusals:
        first = refusals[0]
        suffix = f" (+{len(refusals) - 1} more)" if len(refusals) > 1 else ""
        _row("instructions sync", WARN,
             f"refused {first['path']}: {reason(first)}{suffix}; "
             "other enrolled targets still reconcile")
    elif preserved:
        first = preserved[0]
        suffix = f" (+{len(preserved) - 1} more)" if len(preserved) > 1 else ""
        _row("instructions sync", OK,
             f"preserved-newer {first['path']}{suffix}")


def _sentinel_probe(
        enrollment: dict, *, deadline: float | None = None) -> dict:
    if enrollment.get("state") != "enrolled":
        return {
            "state": "not-applicable", "armed": None,
            "detail": "uninstall sentinel is not applicable while unenrolled",
        }
    if deadline is not None and time.monotonic() >= deadline:
        return {
            **_budget_observation("uninstall-sentinel verification"),
            "armed": None,
        }
    try:
        import teach
        if deadline is not None and time.monotonic() >= deadline:
            return {
                **_budget_observation("uninstall-sentinel verification"),
                "armed": None,
            }
        timeout = (
            None if deadline is None
            else _remaining_timeout(deadline, _DIAGNOSTIC_SENTINEL_TIMEOUT_S))
        if timeout == 0.0:
            return {
                **_budget_observation("uninstall-sentinel verification"),
                "armed": None,
            }
        observed = teach.sentinel_status(timeout_s=timeout)
        if not isinstance(observed, dict):
            raise TypeError("sentinel probe returned a non-object")
        return observed
    except Exception as exc:  # noqa: BLE001 -- diagnostics preserve unavailability
        return {
            "state": "unavailable", "armed": None,
            "detail": (
                "uninstall-sentinel verification is unavailable "
                f"({type(exc).__name__}: {common.terminal_safe(exc)})"),
        }


def _model_attribution(
        summary: dict, readiness: dict, *, deep: bool = False,
        timeout_s: float | None = _DIAGNOSTIC_QUALITY_TIMEOUT_S,
) -> dict:
    # Grouping every accountable turn is a full scan of the message table -
    # 0.8s on a cold 1.5 GiB corpus, more than the whole routine budget. It is
    # a deep check, and routine renders nothing about it.
    if not deep:
        return {
            "state": "not-inspected", "reason": None,
            "accountable": None, "with_model": None,
            "unknown": None, "percent": None,
        }
    corpus_state = str(summary.get("state") or (
        "ready" if "sessions" in summary and "messages" in summary
        else "proof-damaged"))
    if corpus_state != "ready":
        return {"state": (
                    "status-deferred"
                    if corpus_state == "status-deferred" else "unavailable"),
                "reason": corpus_state,
                "accountable": None, "with_model": None,
                "unknown": None, "percent": None}
    readiness_state = str(readiness.get("state") or "unavailable")
    if readiness_state != "ready":
        return {"state": (
                    "status-deferred"
                    if readiness_state in ("not-verified", "status-deferred")
                    else "unavailable"),
                "reason": readiness_state,
                "accountable": None, "with_model": None,
                "unknown": None, "percent": None}
    quality = _corpus_quality(readiness, deep=True, timeout_s=None)
    if quality["readiness"].get("state") != "ready":
        return {"state": "unavailable",
                "reason": quality["readiness"].get("state"),
                "accountable": None, "with_model": None,
                "unknown": None, "percent": None}
    accountable = int(quality.get("accountable") or 0)
    with_model = int(quality.get("with_model") or 0)
    unknown = int(quality.get("unknown") or 0)
    return {
        "state": ("empty" if accountable == 0 else
                  "complete" if unknown == 0 else "partial"),
        "reason": None, "accountable": accountable,
        "with_model": with_model, "unknown": unknown,
        "percent": (100.0 * with_model / accountable if accountable else None),
        "by_source": dict(quality.get("by_source") or {}),
        "unknown_by_agent": dict(quality.get("unknown_by_agent") or {}),
        "by_who": dict(quality.get("by_who") or {}),
    }


def _index_summary_state(*, deadline: float | None = None) -> dict:
    if deadline is not None and time.monotonic() >= deadline:
        return {
            **_budget_observation("the index summary"),
            "state": "status-deferred",
        }
    try:
        summary = (
            common.index_summary()
            if deadline is None
            else common.index_summary(deadline=deadline)
        )
    except TimeoutError:
        return {
            **_budget_observation("the index summary"),
            "state": "status-deferred",
        }
    except (OSError, TypeError, ValueError):
        summary = None
    if summary is not None:
        state = summary.get("state")
        if isinstance(state, str) and state:
            return summary
        valid_counts = (
            type(summary.get("sessions")) is int
            and summary["sessions"] >= 0
            and type(summary.get("messages")) is int
            and summary["messages"] >= 0
            and isinstance(summary.get("agents"), list)
            and isinstance(summary.get("per_agent"), list)
        )
        return {
            **summary,
            "state": "ready" if valid_counts else "proof-damaged",
        }
    candidates = (
        common.MESSAGES_PATH, common.DATA_DIR / "sessions.jsonl",
        common.DATA_DIR / "corpus.db",
        common.DATA_DIR / "session_family.meta.json", common.INGEST_SIG_PATH,
    )
    for path in candidates:
        try:
            path.lstat()
            return {"state": "proof-damaged"}
        except FileNotFoundError:
            continue
        except OSError:
            return {"state": "proof-damaged"}
    return {"state": "never-built"}


def _machine_freshness_fields(
        freshness: dict, generation: dict) -> dict:
    """Render machine freshness from the already-observed generation proof."""
    import corpusdb

    out = dict(freshness)
    generation_state = generation.get("state")
    if generation_state == "status-deferred":
        out["checked"] = False
        if out.get("failing") is not True:
            out.update(
                state="unchecked", failing=False, may_be_stale=True,
                code="diagnostic-budget-exceeded",
                reason=common.terminal_safe(generation.get("detail") or ""),
                consecutive_failures=0,
            )
        return {
            "freshness": out,
            "corpus_age_s": None,
        }
    problem = generation_state not in (
        "ready", "never-built",
        corpusdb.GENERATION_VERIFICATION_DEFERRED)
    if (problem and (
            generation_state == "torn-generation"
            or out.get("state") in ("no-known-failure", "unchecked"))):
        out.update(
            state="degraded", failing=True, may_be_stale=True,
            code=generation_state,
            reason=common.terminal_safe(generation.get("detail") or ""),
            consecutive_failures=0,
        )
    return {
        "freshness": out,
        "corpus_age_s": generation.get("corpus_age_s"),
    }


def probe(
        *, deep: bool = False, progress=None,
        for_report: bool = False,
        routine_deadline: float | None = None) -> dict:
    """Structured report; routine probes share every expensive observation."""
    deadline = (
        None if deep else
        routine_deadline
        if routine_deadline is not None else _routine_deadline(deep=False)
    )
    has_cargo = shutil.which("cargo") is not None
    has_bin = INGEST_BIN.exists()
    # Start the census child now so its wall time overlaps the probes below;
    # the consume near the census cap then pays only the remainder.
    if deep or _remaining_timeout(deadline, _DIAGNOSTIC_STORE_TIMEOUT_S) > 0.0:
        indexd_runtime.arm_store_census()
    embeddings_setting = settings.setting_observation("embeddings")
    smart = _semantic_probe(deep=deep, fix=False)
    orphans = _orphan_inventory(deep=deep, deadline=deadline)
    footprint = _data_footprint(
        deadline=None if deep else deadline)
    model_cache = _model_cache_footprint(
        deadline=None if deep else deadline)
    summary = _index_summary_state(
        deadline=None if deep else deadline)
    indexed_agents = tuple(
        str(row.get("agent") or "") for row in summary.get("per_agent", [])
        if row.get("agent"))
    store_timeout = (
        _DIAGNOSTIC_STORE_DEEP_TIMEOUT_S if deep
        else _remaining_timeout(deadline, _DIAGNOSTIC_STORE_TIMEOUT_S))
    if store_timeout > 0.0:
        store_rows, drift_report = indexd_runtime.observe_store_drift(
            timeout_s=store_timeout)
        store_observation = (
            {"state": "complete", "detail": "live store census completed"}
            if store_rows is not None else
            _budget_observation("the live store census")
            if deadline is not None and time.monotonic() >= deadline else {
                "state": "unavailable",
                "detail": (
                    drift_report.detail
                    or "live store census did not return a verdict"),
            }
        )
    else:
        store_rows = None
        store_observation = _budget_observation("the live store census")
        drift_report = indexd_runtime.DriftReport(
            "unknown", code="diagnostic-budget-exceeded",
            detail=_budget_detail(store_observation))
    observed_stores = store_rows or []
    # Durable source-read failures are still a cheap boundary observation when
    # the live census is unavailable. Do not synthesize absent-indexed rows
    # from an observation that never ran.
    stores = _store_counts(
        indexed_agents if store_rows is not None else (),
        observed_stores,
    )
    import corpusdb
    generation = corpusdb.search_generation_health(routine=not deep)
    # Schema, generation and family metadata are three small reads behind a
    # clone-bounded open; routine pays them so the verdict is real. Only the
    # page scan below them is deep.
    search_db = (
        _corpus_db_readiness(deep=deep, progress=progress, generation=generation)
        if deep or deadline is None or time.monotonic() < deadline
        else {**_budget_observation("search database readiness"),
              "integrity": _integrity_not_verified()}
    )
    indexed = {"state": str(summary.get("state") or (
        "ready" if "sessions" in summary and "messages" in summary
        else "proof-damaged"))}
    if indexed["state"] == "ready":
        indexed.update(sessions=int(summary["sessions"]),
                       messages=int(summary["messages"]),
                       age_s=int(summary.get("age_s") or 0))
    attribution = _model_attribution(
        summary, search_db, deep=deep,
        timeout_s=(
            None if deep else _remaining_timeout(
                deadline, _DIAGNOSTIC_QUALITY_TIMEOUT_S)),
    )
    archive_timeout = (
        None if deep else
        0.0 if not footprint.get("complete") else
        _remaining_timeout(
            deadline, _DIAGNOSTIC_ARCHIVE_TIMEOUT_S)
    )
    archive_state = _archive_probe(
        stored_bytes=(
            int(footprint["archive_bytes"])
            if footprint.get("complete") else None),
        deep=deep,
        timeout_s=archive_timeout,
    )
    daemon = (
        {"state": "status-deferred", "running": False}
        if (not deep and deadline is not None
            and time.monotonic() >= deadline)
        else indexd_runtime.indexd_resource_status(
            observe_only=True, include_rss=deep)
    )
    indexing_failure = indexd_runtime.indexing_failure(
        daemon_status=daemon, drift_report=drift_report)
    freshness_fields = _machine_freshness_fields(
        indexd_runtime.machine_freshness(
            checked=store_rows is not None,
            failure=indexing_failure,
            drift_report=drift_report),
        generation)
    detect_timeout = _remaining_timeout(
        deadline, _DIAGNOSTIC_DETECT_TIMEOUT_S)
    if detect_timeout > 0.0:
        detection = {}
        detected = common.detected_stores(
            timeout_s=detect_timeout, observation=detection)
        if not detection:
            # Compatibility with an injected/older helper must fail open; an
            # empty list without an outcome is not proof that no stores exist.
            detection = {
                "state": "unavailable",
                "detail": "unsupported-store detection returned no verdict",
            }
    else:
        detected = []
        detection = _budget_observation("unsupported-store detection")
    installed_build = install_lag.installed_master_lag(deadline=deadline)
    teach_reconcile = _teach_reconcile_probe(
        deadline=None if deep else deadline)
    teach_enrollment = dict(teach_reconcile.pop(
        "_enrollment",
        {"state": "unavailable", "targets": None,
         "detail": "enrollment observation is unavailable"},
    ))
    sentinel = _sentinel_probe(
        teach_enrollment, deadline=None if deep else deadline)
    native_identity = _native_binary_identity(
        deadline=None if deep else deadline)
    deferred = [
        label for label, observation in (
            ("store census", store_observation),
            ("semantic runtime", {"state": (
                smart.get("runtime_state")
                if smart.get("runtime_state") == "not-inspected"
                else "complete" if smart.get("runtime_verified", True)
                else "unavailable")}),
            ("search generation", {"state": (
                "budget-exceeded"
                if generation.get("state") == "status-deferred"
                else "not-inspected"
                if (generation.get("state")
                    == corpusdb.GENERATION_VERIFICATION_DEFERRED)
                else "complete")}),
            ("search database readiness", search_db),
            ("model attribution", attribution),
            ("archive manifest", archive_state),
            ("data footprint", footprint),
            ("semantic model cache", model_cache),
            ("orphan inventory", orphans),
            ("index summary", summary),
            ("daemon resource observation", daemon),
            ("daemon RSS", {"state": (
                daemon.get("rss_state")
                if daemon.get("running") and (
                    daemon.get("rss_state") == "not-inspected")
                else "unavailable"
                if daemon.get("running") and daemon.get("rss_bytes") is None
                else "complete")}),
            ("unsupported-store detection", detection),
            ("installed-build provenance", installed_build),
            ("native binary identity", {
                "state": native_identity["native_binary_build_state"]}),
            ("embeddings setting", embeddings_setting),
            ("instruction enrollment", teach_enrollment),
            ("instruction reconciliation", teach_reconcile),
            ("uninstall sentinel", sentinel),
        )
        if not _concluded(observation)
    ]
    try:
        distribution_identity = {
            "distribution_build_id": common.distribution_build_id(),
            "distribution_build_state": "verified",
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        distribution_identity = {
            "distribution_build_id": None,
            "distribution_build_state": "unavailable",
            "distribution_build_detail": common.terminal_safe(exc),
        }
    result = {
        "paths": {
            "data_dir": str(common.DATA_DIR),
            "data_source": common.data_dir_source(),
            "warnings": common.data_dir_warnings(),
        },
        "core": {"live": has_bin, "rust": has_cargo, "binary": has_bin,
                 "stores": stores, "indexed": indexed,
                 "search_db": search_db,
                 "store_observation": store_observation},
        "archive": archive_state,
        "teach_reconcile": teach_reconcile,
        "teach_enrollment": teach_enrollment,
        "sentinel": sentinel,
        "model_attribution": attribution,
        "install_lag": installed_build,
        "runtime_identity": {
            "version": common.package_version(),
            **distribution_identity,
            **native_identity,
            "runtime_build_id": indexd_runtime.INDEXD_BUILD_ID,
            "kind": _runtime_build_kind(),
            "module_path": str(Path(__file__).resolve().parent),
        },
        "settings": {"embeddings": embeddings_setting},
        "semantic": smart,
        "resources": {
            "data": {
                "state": footprint.get("state", "complete"),
                "complete": bool(footprint.get("complete")),
                "files": footprint["files"], "bytes": footprint["bytes"],
                "detail": footprint.get("detail"),
            },
            "semantic_model_cache": model_cache,
            "indexd": daemon,
            "orphans": orphans,
        },
        "self_exclusion": surface.self_exclusion_disclosure(
            None, inactive_reason="not-applicable"),
        **freshness_fields,
        "semantic_coverage": smart.get("embedding_coverage"),
        # Detection is distinct from an ingest adapter; never report these as indexed.
        "detected": detected,
        "detection": detection,
        "diagnostics": {
            "tier": "deep" if deep else "routine",
            "state": "partial" if deferred else "complete",
            "budget_s": None if deep else _DIAGNOSTIC_ROUTINE_TIMEOUT_S,
            "render_headroom_s": (
                None if deep else _DIAGNOSTIC_RENDER_HEADROOM_S),
            "deferred": deferred,
        },
    }
    if for_report:
        fails = (
            int(indexing_failure.consecutive_failures)
            if indexing_failure is not None else 0
        )
        failure_detail = (
            str(indexing_failure.reason)
            if indexing_failure is not None else ""
        )
        result["_render"] = {
            "summary": summary,
            "footprint_breakdown": footprint["breakdown"],
            "freshness_failures": fails,
            "freshness_failure_detail": failure_detail,
            "indexing_failure": indexing_failure,
            "generation": generation,
        }
    return result


def _json_report(*, deep: bool = False) -> dict:
    """probe() plus the tier roll-up and unlock commands, as one JSON-ready dict."""
    if deep:
        _deep_notice(stream=sys.stderr)
    p = probe(deep=deep)
    fixes: list[str] = []
    embeddings_observation = (
        p.get("settings", {}).get("embeddings")
        or {
            "state": "unavailable", "value": None,
            "detail": "embeddings setting observation is unavailable",
        })
    embeddings_off = (
        embeddings_observation.get("state") == "verified"
        and embeddings_observation.get("value") == "off")
    if embeddings_off:
        p["semantic"] = {
            **p["semantic"],
            "embeddings_setting": "off",
            "refresh": "disabled",
            "install_hint": None,
        }
    if not p["core"]["rust"]:
        fixes.append("install Rust: https://rustup.rs")
    if not p["core"]["binary"] and p["core"]["rust"]:
        fixes.append(_command_remedy("index-binary", "index"))
    if (not embeddings_off
            and p["semantic"].get("runtime_verified", True)
            and not p["semantic"].get(
                "available", all(p["semantic"]["deps"].values()))):
        fixes.append(SEMANTIC_UNLOCK)
    elif (not embeddings_off
          and p["semantic"]["embeddings"]
          in ("missing-embeddings", "missing-source")):
        fixes.append(
            "semantic embeddings build in the background on first use; "
            + _command_remedy("semantic-reindex", "reindex"))
    tiers = []
    if p["core"]["binary"] or p["core"]["rust"]:
        tiers.append("core")
    if p["semantic"]["live"]:
        tiers.append("semantic")
    p["tiers"] = tiers
    # tiers lists only verified-live membership; an unprobed lane (routine
    # leaves live=None) is unknown, not absent - --deep resolves it.
    p["tiers_unknown"] = (
        ["semantic"] if p["semantic"]["live"] is None else [])
    p["fixes"] = fixes
    # Reuse probe's single Rust observations; JSON adds aliases, not new probes.
    p["detected_not_indexed"] = p["detected"]
    return p


def report(*, deep: bool = False, fix_actions: bool = False) -> dict:
    import corpusdb  # the owner of the tables that say what repairs itself
    fixes: list[str] = []
    if deep:
        _deep_notice()
    deadline = _routine_deadline(deep=deep)
    snapshot = probe(
        deep=deep, progress=_deep_progress if deep else None,
        for_report=True, routine_deadline=deadline)
    render = snapshot["_render"]

    print("\ndata")
    paths = snapshot["paths"]
    _row("location", OK, f"{paths['data_dir']} ({paths['data_source']})")
    for warning in paths["warnings"]:
        _row("data warning", WARN, warning)
    footprint = snapshot["resources"]["data"]
    if footprint.get("complete"):
        _row("footprint", OK,
             f"{footprint['bytes'] / (1024 ** 2):.1f} MiB across "
             f"{footprint['files']:,} files{render['footprint_breakdown']}")
    elif _ran(footprint):
        _row("footprint", WARN,
             str(footprint.get("detail") or "footprint is unavailable"))
    model_cache = snapshot["resources"].get("semantic_model_cache") or {}
    if model_cache.get("complete") and model_cache.get("files"):
        _row(
            "model cache", OK,
            f"{model_cache['bytes'] / (1024 ** 2):.1f} MiB across "
            f"{model_cache['files']:,} files · shared at {model_cache['path']}")
    elif model_cache and _ran(model_cache) and not model_cache.get("complete"):
        _row(
            "model cache", WARN,
            str(model_cache.get("detail") or "model cache is unavailable"))
    # Superseded artifacts are reclaimed by the publication that orphans them
    # (`embedding_segments._publish`), so there is no housekeeping left here to
    # announce. The inventory stays in the snapshot for `--json` forensics.

    print("\nindex")
    summary = render["summary"]
    summary_state = str(snapshot["core"]["indexed"]["state"])
    detected = snapshot["detected"]
    if summary_state == "ready":
        age_s = summary.get("age_s")
        age = ("" if age_s is None else
               " · updated just now" if age_s < 60 else
               f" · updated {common.age_label((time.time() - age_s) * 1000)} ago")
        _row("corpus", OK,
             f"{summary['messages']:,} messages · {summary['sessions']:,} sessions{age}")
        pad = max((len(a["agent"]) for a in summary["per_agent"]), default=0)
        for a in summary["per_agent"]:
            print(common.paint(
                "d", f"        {a['agent'].ljust(pad)}  "
                     f"{a['messages']:,} messages · {a['sessions']:,} sessions",
                common.color_enabled(sys.stdout)))
    elif (snapshot["core"]["search_db"]["state"]
          == "post-adoption-clobber"):
        _row(
            "corpus", WARN,
            "published corpus retained - search database ownership is locked "
            "and no automatic rebuild will run")
    elif summary_state == "status-deferred":
        pass
    elif summary_state == "never-built":
        _row("corpus", WARN,
             "none yet - "
             + _command_remedy("index-publish", "index")
             + "; searches never "
             "build the corpus inline")
    elif summary_state in corpusdb.SELF_REPAIRING_CORPUS_STATES:
        # Law 1: the census and the proof are agrep's own product, rebuilt from
        # the transcripts by the daemon's health tick. A task, not a row.
        pass
    else:
        _row("corpus", WARN, f"unavailable - {summary_state}")

    db_readiness = snapshot["core"]["search_db"]
    db_state = db_readiness["state"]
    db_status, db_detail = WARN, ""
    if not _ran(db_readiness):
        pass
    elif db_state == "ready":
        db_status = OK
        db_detail = f"ready - {db_readiness['detail']}"
    elif db_state == "missing" and summary_state == "never-built":
        db_status, db_detail = OPT, "missing - waiting for the first corpus"
    elif db_state in corpusdb.SELF_REPAIRING_DB_STATES:
        # Laws 1/3: the search db is a pure projection the daemon
        # republishes from bytes it already holds. Saying so and handing
        # over the command anyway was the self-refuting row.
        pass
    elif db_state == "busy":
        # An index writer holding the database is agrep working, not agrep
        # broken. Asking the reader to re-run the diagnostic to route around
        # our own lock is the diagnostic refusing to do its one job.
        pass
    elif db_state == "post-adoption-clobber":
        db_status = WARN
        db_detail = f"ownership lockout - {db_readiness['detail']}"
    elif db_state == "owned-elsewhere":
        # one cause, one line: the freshness row below already tells the reader
        # another agrep holds this directory, and names the command that ends it
        blocked = snapshot["resources"]["indexd"].get("blocked")
        failure = render["indexing_failure"]
        if not (blocked or (failure is not None
                            and failure.code == "blocked-owner")):
            # Ownership and liveness are different facts. Only a LIVE foreign
            # owner earns this line; a killed daemon's leftover claim is reaped
            # by this build's next publication, which the kick below starts.
            if indexd_runtime.live_indexer_claim():
                db_detail = (
                    "held by another agrep installation - "
                    + _command_remedy("index-publish", "index"))
    else:
        db_status = WARN
        db_detail = f"unreadable - {db_readiness['detail']}"
    if db_detail:
        _row("search db", db_status, db_detail)
    # Integrity is a field of the row above, not a peer check, so it can
    # never disagree with its parent; it earns a line only when the
    # database was readable enough to have been checked at all.
    integrity = db_readiness.get("integrity") or {}
    if db_state == "ready" and _ran(integrity):
        _row("integrity",
             OK if integrity.get("state") == "verified" else
             WARN if integrity.get("state") in ("failed", "error") else OPT,
             str(integrity.get("detail") or "integrity state is unavailable"))

    archive_state = snapshot["archive"]
    archive_name = str(archive_state.get("state") or "capture-blocked")
    last_pass = archive_state.get("last_pass") or {}
    pass_age = last_pass.get("age_s")
    pass_outcome = str(last_pass.get("outcome") or "unknown")
    age_detail = (
        "last pass never recorded"
        if pass_age is None and pass_outcome == "never"
        else "last pass not inspected"
        if pass_age is None and pass_outcome == "not-inspected"
        else "last pass unavailable"
        if pass_age is None
        else f"last pass {float(pass_age):.1f}s ago"
        + ("" if last_pass.get("fresh") else " (older than capture cadence)"))
    archive_reason = common.terminal_safe(
        archive_state.get("detail")
        or (
            "automatic capture is disabled"
            if archive_name == "disabled" else
            "archive state is unavailable"
        ))
    if _ran(archive_state):
        # freshness-unknown on a box that has never captured is the first
        # scheduled pass not having run yet - agrep's own task (laws 1/3),
        # not an alarm to hand the reader seconds after setup.
        never_captured = (archive_name == "freshness-unknown"
                          and pass_outcome == "never")
        _row("archive",
             OPT if archive_name == "disabled" else
             OK if archive_name == "healthy" and last_pass.get("fresh") else
             OPT if archive_name in ("busy", "migration") else
             OPT if never_captured else WARN,
             archive_reason if archive_name == "disabled"
             else "first capture pass is pending; capture runs in the "
                  "background" if never_captured
             else f"{archive_reason}; {age_detail}")

    daemon = snapshot["resources"]["indexd"]
    machine_freshness = snapshot.get("freshness") or {}
    behind_line = ""
    behind_base = ""
    if machine_freshness.get("state") == "index-behind":
        behind_base = surface.freshness_story_line(surface.FreshnessStory(
            "behind",
            behind_s=machine_freshness.get("behind_s"),
            changed_stores=int(
                machine_freshness.get("changed_stores") or 0),
            converging=True,
        )).removesuffix(" - daemon catching up")
        behind_line = (
            behind_base + " - "
            + _command_remedy("index-publish", "index"))
    fails = int(render["freshness_failures"])
    freshness_failure = render["indexing_failure"]
    generation = render["generation"]
    generation_state = str(generation.get("state") or "")
    # A generation the daemon rebuilds may not fold into - and rename - a
    # failure that is not its symptom: recoding "permission denied" as
    # torn-generation once deleted the one row law 2 requires.
    if (generation_state in ("torn-generation", "generation-moving")
            and not corpusdb.state_self_clears(generation_state)):
        generation_failure = surface.FreshnessFailure(
            generation_state, str(generation.get("detail") or ""))
        if freshness_failure is None:
            freshness_failure = generation_failure
        elif generation_state == "torn-generation":
            freshness_failure = surface.FreshnessFailure(
                generation_state,
                f"{generation_failure.reason}; {freshness_failure.reason}",
                freshness_failure.consecutive_failures,
            )
    # Law 5, once at the source: symptoms of the daemon's own rebuild and
    # of a named volume vanish, keyed on the failure's OWN code only - a
    # permission to grant or an escalated streak speaks, repair queued or not.
    host_line = indexd_runtime.host_block_escalation()
    if freshness_failure is not None and (
            host_line or corpusdb.state_self_clears(freshness_failure.code)
            or _stores_row_owns(freshness_failure.code, snapshot)):
        freshness_failure = None
    if host_line:
        behind_line = behind_base = ""
    tail = f" - {surface.FRESHNESS_POLICY.last_good_index_tail}"
    persistent_failure = surface.persistent_freshness_failure(fails)
    freshness_reason = (
        common.terminal_safe(freshness_failure.reason)
        if freshness_failure is not None else "")
    if db_state == "post-adoption-clobber":
        _row(
            "freshness", WARN,
            "manual recovery required - search and daemon auto-repair are "
            "disabled until the backup-and-reindex remedy below is completed")
    elif host_line:
        # the disk row below is this row's cause; a daemon that cannot write
        # looks blocked, looks behind and looks failing, all from one fact
        pass
    elif daemon.get("state") == "status-deferred":
        if freshness_failure is not None:
            _row("freshness", WARN, f"{freshness_reason}{tail}")
    elif daemon.get("running"):
        rss = daemon.get("rss_bytes")
        detail = f"daemon running - pid {daemon.get('pid')}"
        if isinstance(rss, int):
            detail += f", {rss / (1024 ** 2):.1f} MiB RSS"
        elif daemon.get("rss_state") == "not-inspected":
            pass
        elif rss is None:
            detail += ", RSS observation unavailable"
        if persistent_failure:
            detail += f", FAILING ({fails}+ consecutive): {freshness_reason}{tail}"
        elif freshness_failure is not None:
            detail += f", DEGRADED: {freshness_reason}{tail}"
        elif behind_line:
            detail += f", {behind_line}"
        _row(
            "freshness",
            WARN if freshness_failure is not None else OPT if behind_line else OK,
            detail)
    elif daemon.get("blocked"):
        # six ownership states, one reader situation: the index is not moving
        # and one command moves it. surface owns the sentence so bare status,
        # search and this row cannot spell the condition three ways.
        detail = surface.indexing_advice_line(
            surface.FreshnessFailure("blocked-owner", ""), _cli_command())
        if (freshness_failure is not None
                and freshness_failure.code != "blocked-owner"):
            detail += f"; {freshness_reason}"
        if behind_base:
            detail += f"; {behind_base}"
        _row("freshness", WARN, f"{detail}{tail}")
    elif daemon.get("starting"):
        detail = ("freshness startup in progress - "
                  f"{float(daemon.get('age_s') or 0):.1f}s")
        if behind_base:
            detail += f"; {behind_base}"
        if freshness_failure is not None:
            detail += f"; {freshness_reason}{tail}"
        _row("freshness", WARN if freshness_failure is not None else OPT, detail)
    elif daemon.get("backoff"):
        detail = (
            "daemon launch cooling down - a later background attempt retries "
            "after cooldown")
        if behind_base:
            detail += f"; {behind_base}"
        if freshness_failure is not None:
            detail += f"; {freshness_reason}{tail}"
        _row("freshness", WARN if freshness_failure is not None else OPT, detail)
    elif freshness_failure is not None:
        if persistent_failure:
            detail = (
                f"daemon down after {fails}+ consecutive failures: "
                f"{freshness_reason}")
        else:
            detail = freshness_reason
        _row("freshness", WARN, f"{detail}{tail}")
    else:
        # A missing daemon is owned state: report the kick's result, not a command.
        # A decline renders its one factual cause.
        kick = indexd_runtime.kick_background_repair()
        if kick.in_flight:
            detail = "daemon starting - a publication is in flight"
            level = OPT
        else:
            detail = ("daemon not running - "
                      + surface.repair_decline_line(kick.cause))
            level = WARN if behind_base else OPT
        if behind_base:
            detail = f"{behind_base} - {detail}"
        _row("freshness", level, detail)

    # Law 2, the one thing here the reader can act on: what is left under a
    # rebuild that cannot land is the volume. No command - the rebuild
    # resumes by itself once the room exists.
    if host_line:
        _row("disk", WARN, host_line)

    attribution = snapshot["model_attribution"]
    if not _ran(attribution):
        pass
    elif attribution["state"] == "unavailable":
        # Law 5: every reason here restates the corpus or search-db row
        # above. Attribution has no failure of its own, so it has no line
        # of its own; it returns when the row it depends on does.
        pass
    elif attribution["state"] in ("complete", "partial"):
        accountable = int(attribution["accountable"])
        with_model = int(attribution["with_model"])
        unknown = int(attribution["unknown"])
        pct = float(attribution["percent"])
        _row("model attribution", OK if unknown == 0 else OPT,
             f"{pct:.1f}% of your turns know their model "
             f"({with_model:,}/{accountable:,}; drives --model filters) - "
             f"backfilled {attribution['by_source'].get('session', 0):,}, "
             "inferred from timing "
             f"{attribution['by_source'].get('temporal_session', 0):,}, "
             f"unknown {unknown:,}")
        non_model = {
            k: attribution["by_who"].get(k, 0)
            for k in ("control", "synthetic", "recap", "harness")
            if attribution["by_who"].get(k, 0)
        }
        if non_model:
            _row("non-model turns", OPT,
                 "excluded from the % above: "
                 + ", ".join(f"{k} {v:,}" for k, v in non_model.items()))
        if unknown:
            _row("unknown by agent", OPT,
                 ", ".join(
                     f"{k} {v:,}" for k, v in sorted(
                         attribution["unknown_by_agent"].items(),
                         key=lambda item: -item[1])))
    else:
        _row("model attribution", OPT,
             "no user turns in the current search database")

    print("\ncore (required)")
    has_cargo = bool(snapshot["core"]["rust"])
    has_bin = bool(snapshot["core"]["binary"])
    try:
        bin_disp = str(INGEST_BIN.relative_to(ROOT))  # dev: target/release/...
    except ValueError:
        bin_disp = str(INGEST_BIN)                     # bundled / $AGREP_RS_BIN: outside the tree
    _row("ingest binary", OK if has_bin else MISS,
         bin_disp if has_bin else
         "not built - " + _command_remedy("index-binary", "index"))
    if not has_bin:
        # rust only matters while there is no binary to run
        _row("rust / cargo", OK if has_cargo else MISS,
             "can build from source" if has_cargo else "install from https://rustup.rs")
        if not has_cargo:
            fixes.append("install Rust: https://rustup.rs")
        else:
            fixes.append(_command_remedy("index-binary", "index"))

    pyok = sys.version_info >= (3, 10)
    _row("python", OK if pyok else MISS,
         ".".join(map(str, sys.version_info[:3]))
         + ("" if pyok else " - agrep needs 3.10+"))

    installed_build = snapshot["install_lag"]
    installed_state = str(installed_build.get("state") or "unavailable")
    _row(
        "runtime build", OK,
        _runtime_build_detail(snapshot.get("runtime_identity")))
    if installed_state != "not-installed" and _ran(installed_build):
        _row("installed build",
             WARN if installed_state == "lagging" else
             OK if installed_state == "current" else OPT,
             _installed_build_detail(installed_build))

    store_rows = snapshot["core"]["stores"]
    store_observation = snapshot["core"].get(
        "store_observation",
        {"state": "unavailable",
         "detail": "live store census observation is unavailable"},
    )
    absent_indexed = [
        item for item in store_rows if item["state"] == "absent-indexed"]
    if not has_bin:
        _row("agent stores", OPT, "checked by the ingest registry after the binary is built")
    elif store_observation.get("state") != "complete":
        if _ran(store_observation):
            _row("agent stores", WARN,
                 str(store_observation.get("detail")
                     or "live store census did not complete"))
    else:
        store_state, store_detail = _store_status(store_rows)
        _row("agent stores", store_state, store_detail)
    for item in absent_indexed:
        _row("store contradiction", MISS,
             f"{item['agent']} has indexed messages but its source store is absent; "
             "restore the store or run a deliberate full rebuild after confirming deletion")
    enrollment = snapshot.get("teach_enrollment") or {
        "state": "unavailable", "targets": None,
        "detail": "enrollment observation is unavailable",
    }
    if enrollment.get("state") == "enrolled":
        n_targets = int(enrollment.get("targets") or 0)
        _row("instructions", OK,
             f"installed in {n_targets} agent file{'s' if n_targets != 1 else ''} "
             f"({_command_remedy('setup-resync', 'setup')})")
        sentinel = snapshot.get("sentinel") or {
            "state": "unavailable", "armed": None,
            "detail": "uninstall-sentinel observation is unavailable",
        }
        sentinel_state = str(sentinel.get("state") or "unavailable")
        if sentinel_state == "armed":
            _row(
                "uninstall sentinel", OK,
                "armed - strips agent instructions seconds after agrep is deleted")
        elif not _ran(sentinel):
            pass
        elif sentinel_state == "not-armed":
            _row(
                "uninstall sentinel", WARN,
                str(sentinel.get("detail") or "not armed")
                + "; instructions may linger after uninstall; "
                + _command_remedy("setup-resync", "setup"))
        else:
            _row(
                "uninstall sentinel", WARN,
                str(sentinel.get("detail")
                    or "uninstall-sentinel verification is unavailable"))
    elif enrollment.get("state") == "status-deferred":
        pass
    elif enrollment.get("state") in ("unreadable", "unavailable"):
        _row(
            "instructions", WARN,
            "state unreadable - "
            + str(enrollment.get("detail") or "enrollment state is unavailable")
            + "; " + _command_remedy("setup-resync", "setup"),
        )
    else:
        # unenrolled with a built index = the product is off: agents have no
        # agrep prior, so nothing ever uses this history until setup runs
        _row("instructions", WARN if summary else OPT,
             "not installed - "
             + _command_remedy("setup-enroll", "setup")
             + " (they will never find agrep on their own)")

    _report_teach_reconcile(snapshot["teach_reconcile"])

    embeddings_observation = (
        snapshot.get("settings", {}).get("embeddings")
        or {
            "state": "unavailable", "value": None,
            "detail": "embeddings setting observation is unavailable",
        })
    embeddings_off = (
        embeddings_observation.get("state") == "verified"
        and embeddings_observation.get("value") == "off")
    lane = surface.SEMANTIC_LANE_POLICY
    smart = snapshot["semantic"]
    # Verifying this lane means importing optional native wheels, which can
    # abort a process rather than raise. Routine declines that and says nothing
    # about it; a recorded build failure is a cheap file read and still speaks.
    inspected = smart.get("runtime_state") != "not-inspected"
    visible_failed_job = _semantic_failure_visible(
        smart, embeddings_off=embeddings_off)
    routine_lane = _routine_lane_row(
        snapshot, smart, embeddings_off=embeddings_off)
    # Every tier: a suffixed identity is a file read, not a runtime probe, and a
    # store nobody told you was approximate is the whole reason for the row.
    store_lane = _store_lane_notice()
    routine_coverage = _routine_coverage_row(smart)
    if (inspected or visible_failed_job or routine_lane or store_lane
            or routine_coverage):
        print("\nsemantic search - optional")
    if routine_lane is not None:
        _row(routine_lane[0], OPT, routine_lane[1])
    if routine_coverage is not None:
        _row("meaning", routine_coverage[0], routine_coverage[1])
    if store_lane is not None:
        _row("embedding lane", WARN, store_lane)
    if (embeddings_observation.get("state") != "verified"
            and (inspected or visible_failed_job)):
        _row(
            "embeddings setting", WARN,
            str(embeddings_observation.get("detail")
                or "setting state is unavailable"),
        )
    if (fix_actions and smart["available"] and smart["embeddings"] != "current"
            and not smart["embed_running"]
            and not embeddings_off
            and (common.DATA_DIR / "sessions.jsonl").exists()):
        # the diagnostic surface self-heals like bare `agrep`: viewing a stale
        # lane starts the repair its row describes (spawn is deduped/backed-off)
        try:
            import semantic
            kicked = semantic.ensure_fresh_async(
                max_new=semantic.SEMANTIC_REFRESH_MAX_NEW)
            if kicked.get("state") == "running":
                smart = {**smart, "embed_running": True,
                         "embed_job": "running"}
        except Exception:  # noqa: BLE001 -- report must render without the kick
            pass
    if not smart.get("runtime_verified", True):
        if not inspected:
            if _semantic_failure_visible(
                    smart, embeddings_off=embeddings_off):
                _row(
                    "embeddings", WARN,
                    _semantic_failure_detail(
                        smart, embeddings_off=embeddings_off, lane=lane))
        else:
            _row(
                "runtime", WARN,
                "optional dependency discovery is unavailable; absence was not "
                "verified and keyword search remains usable")
            _row("model", WARN, "not verified because runtime discovery failed")
            _row(
                "embeddings", WARN,
                "state not verified because runtime discovery failed; "
                "keyword search works")
    elif not smart["available"]:
        missing = [m for m, present in smart["deps"].items() if not present]
        if embeddings_off:
            detail = (
                "installed but unavailable"
                if smart.get("unavailable_reason")
                == "optional-dependency-import-failed"
                else "not installed (optional)")
            _row("runtime", OPT,
                 f"{detail}; refresh {lane.disabled_tail}; "
                 "keyword search works")
        elif smart.get("unavailable_reason") == "optional-dependency-import-failed":
            _row("runtime", WARN,
                 "installed but failed to load; " + SEMANTIC_INSTALL_HINT)
        else:
            _row("runtime", OPT,
                 "not installed (optional): " + ", ".join(missing or smart["deps"])
                 + " - keyword search is fully usable; " + SEMANTIC_INSTALL_HINT)
        if not embeddings_off:
            fixes.append(SEMANTIC_UNLOCK)
    else:
        _row("runtime", OK, ", ".join(smart["deps"]))
        model_integrity = smart.get("model_integrity") or {}
        verified = _ran(model_integrity)
        if smart["model_cached"]:
            model_detail = str(
                (model_integrity.get("detail") if verified else None)
                or "cached model")
        elif embeddings_off:
            model_detail = f"not cached; downloads {lane.disabled_tail}"
        else:
            model_detail = "downloads (~50MB) with the first embedding build"
        if verified and not smart["model_cached"] and model_integrity.get("detail"):
            model_detail += f"; {model_integrity['detail']}"
        if smart.get("embedding_lane"):
            model_detail += f"; store lane {smart['embedding_lane']}"
        _row("model", OK if smart["model_cached"] else OPT, model_detail)
        coverage = smart.get("embedding_coverage")
        cov = (f" ({coverage['indexed']:,}/{coverage['total']:,} rows)"
               if coverage and not coverage.get("complete", True) else "")
        bits = [f"pid {smart['embed_pid']}"] if smart.get("embed_pid") else []
        if smart.get("embed_phase"):
            bits.append(smart["embed_phase"])
        if smart.get("embed_total"):
            # the worker's live counters; published files only update at pass end
            bits.append(f"{smart.get('embed_done') or 0:,}/"
                        f"{smart['embed_total']:,} rows this pass")
        elif coverage and not coverage.get("complete", True):
            bits.append(f"{coverage['indexed']:,}/{coverage['total']:,} rows")
        live_cov = f" ({' · '.join(bits)})" if bits else ""
        running = bool(smart.get("embed_running"))
        state = smart["embeddings"]
        deferral = (
            surface.observed_semantic_deferral(
                common.available_memory_fraction,
                common.battery_state,
                common.host_cpu_fraction,
            )
            if (not running and state != "current"
                and not embeddings_off) else None
        )
        paused = (
            f"background build paused - {deferral.surface_reason}; "
            "resumes automatically"
            if deferral else None
        )
        # one resolved story per state - never advice that contradicts the blocker
        if state == "corrupt-embeddings":
            integrity = smart.get("embedding_integrity") or {}
            reason = common.terminal_safe(
                integrity.get("reason") or
                "the integrity check rejected the published embeddings")
            detail = (
                f"semantic embeddings are corrupt: {reason}; "
                + _command_remedy("semantic-reindex", "reindex")
            )
        elif state == "legacy-publication":
            detail = (
                "semantic embeddings use a legacy publication; "
                + _command_remedy(
                    "legacy-publication-upgrade", "index"))
        elif embeddings_off and running:
            detail = (f"{state} - an existing build is running{live_cov}; "
                      f"new refresh launches are {lane.disabled_tail}")
        elif embeddings_off and state == "current":
            detail = ("current - cached semantic search is available; "
                      f"refresh {lane.disabled_tail}")
        elif embeddings_off and state == "partial":
            detail = (f"partial - searchable now{cov}; "
                      f"refresh {lane.disabled_tail}")
        elif embeddings_off and state in ("missing-embeddings", "unknown"):
            detail = f"never built - refresh {lane.disabled_tail}"
        elif embeddings_off:
            detail = f"{state}{cov} - refresh {lane.disabled_tail}"
        elif state == "current":
            detail = "current"
        elif state == "partial":
            detail = "partial - searchable now; " + (
                f"a build is closing the gap{live_cov}" if running else
                paused or f"background passes close the gap{cov}")
        elif state == "stale":
            detail = "stale - " + (
                f"rebuilding{live_cov}" if running else
                paused or
                "repairs on the first semantic search; "
                + _command_remedy("semantic-fix", "doctor", "--fix"))
        elif state in ("missing-embeddings", "unknown"):
            detail = "never built - " + (
                f"first build running{live_cov}" if running else
                paused or
                "builds on the first semantic search; "
                + _command_remedy("semantic-fix", "doctor", "--fix"))
        elif state == "missing-source":
            detail = "waiting on the first index build"
        else:
            integrity = smart.get("embedding_integrity") or {}
            reason = integrity.get("reason")
            detail = state + cov + (f": {reason}" if reason else "")
        if state == "corrupt-embeddings":
            _row("embeddings", WARN, detail)
        elif _semantic_failure_visible(
                smart, embeddings_off=embeddings_off, running=running):
            # a failed build outranks the state story; carry its reason, not homework
            detail = _semantic_failure_detail(
                smart, embeddings_off=embeddings_off, lane=lane)
            _row("embeddings", WARN, detail)
        else:
            _row(
                "embeddings",
                OK if smart.get("generation_ready", smart["live"]) else OPT,
                detail)
        readiness = smart.get("query_readiness") or {}
        readiness_state = str(readiness.get("state") or "not-inspected")
        if readiness_state != "not-inspected":
            readiness_status = (
                OK if readiness.get("query_serving") is True else
                WARN if (readiness.get("current_generation")
                         and readiness.get("query_serving") is False) else
                OPT)
            _row(
                "query readiness", readiness_status,
                _semantic_readiness_detail(readiness))
            coordination = readiness.get("coordination") or {}
            if coordination:
                writable = coordination.get("writable")
                coordination_detail = (
                    "writable - "
                    + common.terminal_safe(coordination.get("path") or "")
                    if writable is True else
                    common.terminal_safe(
                        coordination.get("reason")
                        or "request coordination is busy"))
                _row(
                    "request access",
                    OK if writable is True else
                    OPT if writable is None else WARN,
                    coordination_detail)
            loopback = readiness.get("loopback") or {}
            if loopback:
                bindable = loopback.get("bindable")
                _row(
                    "worker startup", OK if bindable else WARN,
                    "loopback bind allowed"
                    if bindable else common.terminal_safe(
                        loopback.get("reason")
                        or "loopback bind is unavailable"))
        resident = smart.get("resident_worker") or {}
        worker_status = (WARN if resident.get("blocked") else
                         WARN if readiness.get("query_serving") is False else
                         OK if readiness.get("query_serving") is True else OPT)
        _row("semantic worker", worker_status,
             _resident_worker_detail(
                 resident, automatic=not embeddings_off,
                 readiness=readiness))

    detection = snapshot.get("detection") or {
        "state": "unavailable",
        "detail": "unsupported-store detection observation is unavailable",
    }
    if detection.get("state") != "complete" and _ran(detection):
        _row("unsupported stores", WARN,
             str(detection.get("detail")
                 or "unsupported-store detection is unavailable"))
    if detected:
        print("\ndetected, not yet indexed (parser not shipped - these are NOT searched)")
        for d in detected:
            _row(d["name"], OPT, f"{d['count']} session(s) found; support is planned, not live")

    if db_state == "post-adoption-clobber":
        print(
            "\nrecovery remedy: "
            + str(db_readiness.get("remedy")
                  or "manual recovery details are unavailable"))
    if fixes:
        print("\nto unlock more:")
        for f in dict.fromkeys(fixes):  # dedupe, keep order
            print(f"  - {f}")
    print()
    # After the render, never before it: what was observed is what prints.
    # But doctor may not leave a fault agrep repairs by itself standing just
    # because the reader ran doctor and not a search. Nothing waits on it.
    if (str(snapshot["core"]["indexed"].get("state") or "")
            in corpusdb.SELF_REPAIRING_CORPUS_STATES
            or str(snapshot["core"]["search_db"].get("state") or "")
            in corpusdb.SELF_REPAIRING_DB_STATES
            # a dead owner's claim: the kick's own liveness guard decides
            or str(snapshot["core"]["search_db"].get("state") or "")
            == "owned-elsewhere"):
        indexd_runtime.kick_background_repair()
    return {"fixes": fixes, "has_cargo": has_cargo, "deps": smart["deps"]}


def fix() -> int:
    """Prefetch the optional semantic model when its runtime is available.

    Missing optional dependencies are a healthy core-only installation, not a
    failed setup. In that case keyword search remains ready and this exits zero.
    """
    if common.setting("embeddings") == "off":
        print("semantic tier deferred: embeddings=off blocks model downloads.")
        print("keyword search and agent enrollment remain fully available.")
        print(_command_remedy(
            "embeddings-enable", "set", "embeddings", "auto") + ".")
        return 0
    smart = _semantic_probe()
    if not smart["available"]:
        missing = [name for name, present in smart["deps"].items() if not present]
        why = (f"missing: {', '.join(missing)}" if missing
               else "the installed optional runtime could not be loaded")
        print(f"semantic model prefetch skipped ({why}).")
        print("keyword search remains fully usable; semantic search is optional.")
        print(f"on a supported OS/Python, enable it with "
              f"{SEMANTIC_INSTALL_ACTION}.")
        return 0
    if smart["model_cached"]:
        print("semantic model already cached.")
        return 0
    try:
        import embedder
        print("fetching the pinned semantic model (~52 MiB, one time) ...",
              flush=True)
        embedder.ensure_model()
    except (ImportError, ModuleNotFoundError) as e:
        # A module may have a discoverable package directory but fail to import
        # because an optional native dependency is unavailable on this tuple.
        print(f"semantic model prefetch skipped (optional runtime unavailable: {e}).")
        print("keyword search remains fully usable; semantic search is optional.")
        print(f"on a supported OS/Python, enable it with "
              f"{SEMANTIC_INSTALL_ACTION}.")
        return 0
    except Exception as e:  # noqa: BLE001 -- a real fetch/cache failure is actionable
        print(f"  ! model fetch failed ({e}); it retries on first semantic use.")
        return 1
    print("\ndone.")
    return 0


def _metal_lane_note() -> None:
    """One line about the GPU lane, only where it could matter.

    Apple silicon is the only platform with a Metal lane, and the semantic
    tier has to exist before an accelerator for it means anything - both
    misses stay silent so a Linux box or a keyword-only install never reads
    about a lane it cannot have.
    """
    import platform

    if not (sys.platform == "darwin"
            and platform.machine() == "arm64"):
        return
    if common.setting("embeddings") == "off":
        # embeddings=off setups are network- and probe-sterile; a GPU note
        # about a tier the owner disabled is noise.
        return
    if not _semantic_probe()["available"]:
        return
    try:
        # mlx_embed needs numpy at import; a core-only install has neither
        # tier and gets no GPU note.
        import mlx_embed
    except ImportError:
        return
    ok, reason = mlx_embed.available()
    if ok:
        print("metal lane: on - new embedding stores use the GPU "
              "(~10.9x the CPU lane on the benchmark host); "
              "AGREP_MLX=off opts out.")
        return
    if "disabled" in reason:
        print("metal lane: off (AGREP_MLX=off); unset it to re-enable "
              "GPU embedding for new stores.")
        return
    hint = dist.semantic_install_hint(extra="metal")
    print(f"available on this Mac: the Metal GPU lane, measured ~10.9x "
          f"faster than the CPU lane on the repository's benchmark host - "
          f"{hint}, then `agrep reindex --full` if you want "
          f"existing history rebuilt on it.")


def setup(
        ml: bool | None = None, *, prefetch_semantic: bool = True) -> int:
    """Report tier states and prefetch semantics when the optional runtime exists.

    Core-only installs skip that prefetch successfully. `ml` is accepted for old
    scripts and ignored.
    """
    before = _json_report()
    print(f"tiers now: {', '.join(before['tiers']) or 'none'}")
    if prefetch_semantic:
        rc = fix()
    else:
        print("semantic model prefetch skipped for this setup run.")
        rc = 0
    _metal_lane_note()
    if rc != 0:
        print("semantic tier deferred; core setup and agent enrollment continue.")
        print(_command_remedy("setup-retry", "setup")
              + "; the first semantic use also retries the pinned model fetch.")
    if before["fixes"]:
        print(f"next unlock: {before['fixes'][0]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The module is also directly executable; normalize a piped Windows stdout
    # before any report or argument error reaches a cp1252-strict stream.
    common.utf8_stdio()
    argv = argv if argv is not None else sys.argv[1:]
    known = {"--deep", "--fix", "--json", "--setup", "--no-semantic"}
    unknown = [argument for argument in argv if argument not in known]
    if unknown:
        return surface.argument_error(
            "agrep doctor", "unrecognized argument(s): " + ", ".join(unknown),
            argv=argv, search_word="doctor")
    conflict = surface.doctor_action_conflict(argv)
    if conflict is not None:
        return surface.argument_error(
            "agrep doctor", conflict, argv=argv, search_word="doctor")
    if "--no-semantic" in argv and "--setup" not in argv:
        return surface.argument_error(
            "agrep doctor", "--no-semantic has no effect without --setup",
            argv=argv, search_word="doctor")
    deep = "--deep" in argv
    if "--json" in argv:
        print(json.dumps(_json_report(deep=deep), ensure_ascii=False))
        return 0
    if "--setup" in argv:
        return setup(prefetch_semantic="--no-semantic" not in argv)
    if "--fix" in argv:
        report(deep=deep, fix_actions=True)
        return fix()
    report(deep=deep, fix_actions=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
