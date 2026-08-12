"""Disposable resident semantic worker and its loopback client.

One-shot agent commands should not pay ONNX/session/artifact startup on every
meaning search, but the freshness daemon must also not retain native-runtime RSS
forever.  This worker is the middle ground: one authenticated, strictly serial
loopback owner keeps the model hot for a short lease, then process exit reliably
returns RAM (and future GPU memory) on macOS and Windows.

Semantic commands use this worker as the single serialized model owner.
Any transport failure after a request is sent is terminal for the semantic attempt;
callers fall back to keyword instead of stacking a duplicate six-thread ONNX query
in the CLI process.
"""

from __future__ import annotations

import hmac
import hashlib
import http.client
import json
import math
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

import common
import indexd_runtime
import ownerfile
import removal_fence

PROTOCOL = 10
CAPABILITIES = (
    "family-diversity-v1", "q8-shadow-v1", "semantic-policy-v1",
    "worker-stop-v2", "timing-v1", "exclude-session-family-v1",
    "owner-generation-v1", "request-deadline-v1", "deadline-cancel-v1",
    "exact-tree-drain-v1", "request-serialization-v1",
    "model-download-consent-v1")
MAX_QUERY_CHARS = 64 * 1024
MAX_BODY_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_DEADLINE_HEADER = "X-Agrep-Deadline-Monotonic-Ns"
MAX_REQUEST_DEADLINE_NS = 300_000_000_000
CONNECT_TIMEOUT_S = 0.25
REQUEST_SLOT_POLL_S = 0.005
TIMEOUT_RETIRE_S = 0.5
START_TIMEOUT_S = 4.0
STARTUP_ORPHAN_S = 30.0
STOP_ACK_ORPHAN_S = 10.0
START_CLAIM_GRACE_S = START_TIMEOUT_S + 2.0
OWNER_POLL_S = 5.0
HANDOFF_POLL_S = 0.25
IDLE_POLICY_REFRESH_S = 30.0
_UNVERIFIABLE_STARTS = frozenset(("", "None", "unknown"))
_WORKER_OWNER_BYTES = 64 * 1024
_LAUNCH_CLAIM_ENV = "AGREP_SEMANTIC_LAUNCH_NONCE"
_COORDINATION_NAMESPACE_VERSION = 1


def _data_dir_readonly() -> bool:
    return common.data_dir_readonly(common.DATA_DIR)


def _mutation_refusal_reason() -> str | None:
    if _data_dir_readonly():
        return "AGREP_DATA_READONLY protects the data directory"
    ownership = indexd_runtime.derived_writer_mutation_info()
    return None if ownership.writable else ownership.reason


def _mutation_refused() -> bool:
    return _mutation_refusal_reason() is not None


def _request_coordination_refused() -> bool:
    return _request_coordination_refusal_reason() is not None


def _worker_coordination_refusal_reason() -> str | None:
    if _data_dir_readonly():
        return "AGREP_DATA_READONLY protects the data directory"
    if (removal_fence.background_removal_active()
            and not removal_fence.background_removal_owned_by_current_process()):
        return "agrep removal is active"
    ownership = indexd_runtime.derived_writer_mutation_info()
    if ownership.writable or ownership.journal_blocked:
        return None
    return ownership.reason


def _worker_lock_coordination_refusal_reason() -> str | None:
    """Refusal for the ephemeral single-model-owner record.

    Callers coordinate this record outside the derived-data store, so read-only
    and sandboxed readers may participate without gaining authority to mutate
    corpus state.  The removal fence still blocks every new model owner.
    """
    if (removal_fence.background_removal_active()
            and not removal_fence.background_removal_owned_by_current_process()):
        return "agrep removal is active"
    return None


def _semantic_timing_enabled() -> bool:
    value = os.environ.get("AGREP_SEM_TIMING", "")
    return common.DEBUG or value.lower() not in ("", "0", "false", "no", "off")


def _compute_build_id() -> str:
    """Behavior identity that changes across in-place installs/dev edits.

    A package version alone is insufficient during development and editable-wheel
    upgrades. Hash the small semantic implementation surface loaded by the resident;
    an old process keeps the old constant even if its files are replaced on disk.
    """
    digest = hashlib.sha256()
    digest.update(f"protocol={PROTOCOL}\n".encode())
    digest.update("\n".join(CAPABILITIES).encode())
    here = Path(__file__).resolve().parent
    for name in (
        "semworker.py", "semantic.py", "ask.py", "common.py", "corpusdb.py",
        "embedder.py", "embedding_segments.py", "semantic_q8.py",
        "segment_query.py", "ownerfile.py",
        "winjob.py",
        # The modules extracted from common.py still run in the resident;
        # hashing the re-export shim alone would miss edits to them.
        "proc.py", "events.py", "console.py", "settings.py", "dist.py",
        "resources.py", "session_context.py", "embedding_store.py",
        "fileops.py", "index_lock.py", "removal_fence.py",
    ):
        digest.update(name.encode())
        try:
            digest.update((here / name).read_bytes())
        except OSError:
            digest.update(b"<missing>")
    # The detached script starts with py/ on sys.path, while its client starts at repo root.
    digest.update(common.package_version().encode())
    return digest.hexdigest()[:24]


WORKER_BUILD_ID = _compute_build_id()


class ResidentSemanticUnavailable(RuntimeError):
    """The resident semantic path could not answer without unsafe duplicate work."""


class ResidentSemanticTimeout(ResidentSemanticUnavailable):
    """A semantic deadline elapsed; the bare class may mean work was accepted."""


class ResidentSemanticProtocolMismatch(ResidentSemanticUnavailable):
    """A pre-upgrade worker rejected a request before running inference."""


class ResidentSemanticPreflightUnavailable(ResidentSemanticUnavailable):
    """The request failed before any semantic inference could start."""


class ResidentSemanticLoopbackUnavailable(
        ResidentSemanticPreflightUnavailable):
    """Local socket policy blocked IPC before inference; child fallback is safe."""


class ResidentSemanticPreflightTimeout(
        ResidentSemanticPreflightUnavailable, ResidentSemanticTimeout):
    """The request's deadline elapsed before a worker could accept it."""


def descriptor_path() -> Path:
    return common.DATA_DIR / ".semantic-worker.json"


def _ephemeral_coordination_path(name: str) -> Path:
    """Per-user runtime record reachable by sandboxed read-only callers.

    These locks serialize native inference; they are not corpus state. Keeping
    them beside the corpus made a healthy installed worker unreachable from an
    agent sandbox that can read Application Support but may write only its
    private temporary directory. Exact-owner files remain mode 0600 and bind
    the path to this data directory.
    """
    root = _prepare_ephemeral_coordination_dir()
    return root / name.lstrip(".")


def _coordination_base_path() -> Path:
    # POSIX /tmp is process-environment independent; on macOS it resolves to
    # /private/tmp.  Using TMPDIR here would let a sandbox and its resident
    # daemon silently choose different model-owner locks.
    root = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    return root.resolve(strict=False)


def _ephemeral_coordination_dir() -> Path:
    data_identity = os.path.normcase(os.path.realpath(common.DATA_DIR))
    uid_fn = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    uid = str(uid_fn()) if uid_fn is not None else "user"
    digest = hashlib.sha256(
        f"v{_COORDINATION_NAMESPACE_VERSION}\0{uid}\0".encode()
        + os.fsencode(data_identity)).hexdigest()[:20]
    return _coordination_base_path() / (
        f"agrep-semantic-v{_COORDINATION_NAMESPACE_VERSION}-{uid}-{digest}")


def _prepare_ephemeral_coordination_dir() -> Path:
    """Create and verify the private cross-sandbox coordination namespace."""
    path = _ephemeral_coordination_dir()
    path.mkdir(mode=0o700, exist_ok=True)
    before = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & reparse)):
        raise OSError(
            f"semantic coordination path is not a private directory: {path}")
    if os.name == "nt":
        return path
    owner_fn = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    owner = before.st_uid if owner_fn is None else owner_fn()
    if before.st_uid != owner:
        raise PermissionError(
            f"semantic coordination directory has a foreign owner: {path}")
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISDIR(opened.st_mode)):
            raise OSError(
                f"semantic coordination directory changed while opening: {path}")
        if stat.S_IMODE(opened.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise PermissionError(
                f"semantic coordination directory is not private: {path}")
    finally:
        os.close(fd)
    return path


def worker_lock_path() -> Path:
    return _ephemeral_coordination_path("worker.lock")


def request_lock_path() -> Path:
    return _ephemeral_coordination_path("worker.request")


def retire_request_path(owner_nonce: str) -> Path:
    nonce = _hex_token(owner_nonce, 32)
    if nonce is None:
        raise ValueError("invalid semantic owner nonce")
    return _ephemeral_coordination_path(f"retire-{nonce}")


def legacy_worker_lock_path() -> Path:
    """Protocol-9 model-owner record kept for one safe upgrade boundary."""
    return common.DATA_DIR / ".semantic-worker.lock"


def start_claim_path() -> Path:
    return common.DATA_DIR / ".semantic-worker.starting"


def _diagnostic_command() -> str:
    return f"`{common.cli_name()} doctor --deep`"


def _record_failure(
        label: str, path: Path, operation: str, detail: object) -> str:
    rendered_path = common.terminal_safe(path)
    reason = common.terminal_safe(detail)
    return (f"{label} cannot {operation} {rendered_path}: "
            f"{reason}; inspect with {_diagnostic_command()}")


def _coordination_failure(operation: str, detail: object) -> str:
    return _record_failure(
        "semantic request coordination",
        _ephemeral_coordination_dir() / "worker.request",
        operation, detail)


def _request_coordination_refusal_reason() -> str | None:
    if removal_fence.background_removal_active():
        return _coordination_failure(
            "write", "agrep removal is active")
    return None


def loopback_bind_status() -> dict:
    """Whether this caller may create the worker's local loopback endpoint."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
    except OSError as exc:
        reason = common.terminal_safe(f"{type(exc).__name__}: {exc}")
        return {
            "bindable": False,
            "operation": "bind 127.0.0.1:0",
            "reason": (
                "semantic worker cannot bind loopback 127.0.0.1:0 "
                f"({reason}); allow local loopback sockets, then inspect with "
                f"{_diagnostic_command()}"),
        }
    finally:
        if sock is not None:
            sock.close()
    return {
        "bindable": True,
        "operation": "bind 127.0.0.1:0",
        "reason": None,
    }


def _discard_record(path: Path, observed: ownerfile.Snapshot) -> bool:
    if _worker_coordination_refusal_reason() is not None:
        return False
    try:
        return ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    except OSError:
        return False


def _discard_request_record(
        path: Path, observed: ownerfile.Snapshot) -> bool:
    if _request_coordination_refused():
        return False
    try:
        return ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    except OSError:
        return False


def _discard_worker_record(
        path: Path, observed: ownerfile.Snapshot) -> bool:
    if _worker_lock_coordination_refusal_reason() is not None:
        return False
    try:
        return ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    except OSError:
        return False


def _verify_record(path: Path, expected: ownerfile.Snapshot) -> None:
    observed = ownerfile.snapshot(path)
    if observed.identity != expected.identity or observed.raw != expected.raw:
        raise ownerfile.OwnershipLost(f"semantic record changed: {path}")


def _hex_token(value: object, width: int) -> str | None:
    if not isinstance(value, str):
        return None
    token = value
    if (len(token) != width
            or any(ch not in "0123456789abcdef" for ch in token)):
        return None
    return token


def _record_is_fresh(mtime: float) -> bool:
    age = time.time() - mtime
    return 0.0 <= age <= START_CLAIM_GRACE_S


def _descriptor_reclaimable(
        observed: ownerfile.Snapshot, owner_nonce: str) -> bool:
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("semantic descriptor must be an object")
        if _hex_token(record.get("owner_nonce"), 32) == owner_nonce:
            return True
        pid = int(record.get("pid") or 0)
        expected_start = str(record.get("process_start") or "")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        return not _record_is_fresh(observed.mtime)
    if expected_start in _UNVERIFIABLE_STARTS:
        return True
    state = ownerfile.classify_process(
        pid, expected_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    return state in (
        ownerfile.ProcessOwner.DEAD, ownerfile.ProcessOwner.REUSED)


def _publish_descriptor(
        lifetime: ownerfile.Handle, raw: str,
        owner_nonce: str) -> ownerfile.Snapshot:
    refusal = _worker_coordination_refusal_reason()
    if refusal is not None:
        raise ownerfile.OwnershipLost(
            f"{refusal}; cannot publish the semantic worker descriptor")
    path = descriptor_path()
    body = raw.encode("utf-8")
    for _ in range(2):
        lifetime.verify(require_stable_mtime=True)
        try:
            published = ownerfile.create_exclusive(path, body, mode=0o600)
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path)
                lifetime.verify(require_stable_mtime=True)
            except OSError as exc:
                raise ownerfile.OwnershipLost(
                    f"semantic endpoint cannot be inspected: {path}") from exc
            if (not _descriptor_reclaimable(observed, owner_nonce)
                    or not _discard_record(path, observed)):
                raise ownerfile.OwnershipLost(
                    f"semantic endpoint is owned by another generation: {path}")
            continue
        try:
            lifetime.verify(require_stable_mtime=True)
        except OSError:
            published.release(
                tombstone=True, require_stable_mtime=True)
            raise
        return published.snapshot
    raise ownerfile.OwnershipLost(
        f"semantic endpoint could not be published: {path}")


def _process_owner(pid: int, expected_start: str) -> ownerfile.ProcessOwner:
    return ownerfile.classify_process(
        pid, expected_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)


def _descriptor_compatible(rec: dict) -> bool:
    try:
        capabilities = tuple(str(value) for value in rec.get("capabilities", ()))
        return (int(rec.get("version")) == PROTOCOL
                and str(rec.get("build_id") or "") == WORKER_BUILD_ID
                and capabilities == CAPABILITIES
                and _hex_token(rec.get("token"), 64) is not None
                and _hex_token(rec.get("owner_nonce"), 32) is not None
                and math.isfinite(float(rec.get("started_at")))
                and rec.get("tree_bound") is True
                and rec.get("named_job") is common.WIN)
    except (OverflowError, TypeError, ValueError):
        return False


def _request_worker_stop(rec: dict, timeout_s: float = 0.5) -> bool:
    """Ask a current-enough worker to retire; old workers may simply return 404."""
    token = _hex_token(rec.get("token"), 64)
    try:
        port = int(rec["port"])
    except (KeyError, TypeError, ValueError):
        return False
    if token is None or not 0 < port < 65536:
        return False
    try:
        timeout_s = max(0.0, min(3.0, float(timeout_s)))
    except (TypeError, ValueError):
        return False
    if timeout_s <= 0:
        return False
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("semantic stop deadline expired")
        return max(0.001, budget)

    conn = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=remaining())
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(remaining())
        conn.request("POST", "/v1/stop", body=b"", headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": "0", "Connection": "close",
        })
        if conn.sock is not None:
            conn.sock.settimeout(remaining())
        response = conn.getresponse()
        if conn.sock is not None:
            conn.sock.settimeout(remaining())
        response.read(4096)
        return response.status == 200
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


class _WorkerOwnerState(Enum):
    ABSENT = "absent"
    EXACT = "exact"
    ORPHANED_GROUP = "orphaned-group"
    UNVERIFIABLE = "unverifiable"
    RECLAIMABLE = "reclaimable"
    MALFORMED_FRESH = "malformed-fresh"
    MALFORMED_STALE = "malformed-stale"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class _WorkerOwner:
    state: _WorkerOwnerState
    record: dict | None = None
    snapshot: ownerfile.Snapshot | None = None


_STOP_REASONS = {
    "active": "the semantic worker did not exit before the deadline",
    "blocked": "the semantic owner record cannot be inspected safely",
    "descriptor-blocked": "the semantic endpoint record cannot be reconciled safely",
    "descriptor-incompatible": (
        "the semantic endpoint belongs to an incompatible live worker"),
    "descriptor-invalid": "the semantic endpoint record is invalid",
    "descriptor-legacy": (
        "the semantic endpoint lacks a verifiable process identity"),
    "descriptor-malformed-fresh": (
        "the semantic endpoint record is still being published"),
    "descriptor-protected": (
        "the semantic endpoint is bound to protected ownership"),
    "descriptor-unverifiable": (
        "the semantic endpoint identity cannot be verified safely"),
    "in-process": "an in-process semantic search still owns the model",
    "legacy-tree": (
        "the pre-upgrade Windows worker has no exact process-tree handle"),
    "malformed-fresh": "the semantic owner record is still being published",
    "orphaned-group": (
        "the semantic owner exited while its process group remains active"),
    "owner-record-blocked": "the semantic owner record could not be removed safely",
    "replacement-churn": "semantic ownership kept changing during teardown",
    "start-claim": "a semantic worker launch is still in progress",
    "unverifiable": "the semantic owner identity cannot be verified safely",
}


def _owner_state_reason(state: str) -> str:
    return _STOP_REASONS.get(
        state, "semantic ownership cannot be reconciled safely")


def _stop_outcome(
        ok: bool, *, running: bool | None = None, blocked: bool = False,
        owner_state: str | None = None, stop_ack: bool = False,
        fallback_termination: bool = False, pid: int | None = None,
        replacements: int = 0) -> dict:
    if ok and running is None:
        running = False
    state = owner_state or ("active" if running is True else "absent")
    return {
        "ok": bool(ok), "running": running, "blocked": bool(blocked),
        "owner_state": state,
        "reason": None if ok else _owner_state_reason(state),
        "stop_ack": bool(stop_ack),
        "fallback_termination": bool(fallback_termination),
        "pid": pid, "replacements": int(replacements),
    }


def _malformed_worker_owner(observed: ownerfile.Snapshot) -> _WorkerOwner:
    state = (
        _WorkerOwnerState.MALFORMED_FRESH
        if _record_is_fresh(observed.mtime)
        else _WorkerOwnerState.MALFORMED_STALE)
    return _WorkerOwner(state, snapshot=observed)


def _inspect_worker_lock_path(path: Path) -> _WorkerOwner:
    try:
        observed = ownerfile.snapshot(path, max_bytes=_WORKER_OWNER_BYTES)
    except FileNotFoundError:
        return _WorkerOwner(_WorkerOwnerState.ABSENT)
    except OSError:
        return _WorkerOwner(_WorkerOwnerState.BLOCKED)
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("semantic worker owner must be an object")
        raw_pid = record.get("pid")
        raw_start = record.get("process_start")
        raw_started_at = record.get("started_at")
        if (not isinstance(raw_pid, int)
                or isinstance(raw_pid, bool)
                or raw_pid <= 0
                or (raw_start is not None
                    and not isinstance(raw_start, str))
                or not isinstance(raw_started_at, (int, float))
                or isinstance(raw_started_at, bool)):
            raise TypeError("semantic worker owner schema is invalid")
        pid = raw_pid
        started_at = float(raw_started_at)
        expected_start = str(raw_start or "")
        nonce = _hex_token(record.get("nonce"), 32)
        tree_bound = record.get("tree_bound")
        named_job = record.get("named_job", False)
        if (not math.isfinite(started_at)
                or nonce is None
                or not isinstance(tree_bound, bool)
                or not isinstance(named_job, bool)):
            raise ValueError("semantic worker owner schema is invalid")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        return _malformed_worker_owner(observed)
    if expected_start in _UNVERIFIABLE_STARTS:
        try:
            live = pid > 0 and common.pid_alive(pid)
        except (OSError, OverflowError, ValueError):
            live = True
        if not live and not common.WIN and tree_bound is True:
            group_active = common._process_group_active(pid)
            if group_active is True:
                return _WorkerOwner(
                    _WorkerOwnerState.ORPHANED_GROUP, record, observed)
            if group_active is None:
                return _WorkerOwner(
                    _WorkerOwnerState.UNVERIFIABLE, record, observed)
        return _WorkerOwner(
            (_WorkerOwnerState.UNVERIFIABLE
             if live else _WorkerOwnerState.RECLAIMABLE),
            record, observed)
    process_owner = ownerfile.classify_process(
        pid, expected_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    if (not common.WIN and tree_bound is True
            and process_owner in (
                ownerfile.ProcessOwner.DEAD,
                ownerfile.ProcessOwner.REUSED)):
        group_active = common._process_group_active(pid)
        if group_active is True:
            state = (
                _WorkerOwnerState.ORPHANED_GROUP
                if process_owner is ownerfile.ProcessOwner.DEAD
                else _WorkerOwnerState.UNVERIFIABLE)
        elif group_active is None:
            state = _WorkerOwnerState.UNVERIFIABLE
        else:
            state = _WorkerOwnerState.RECLAIMABLE
    else:
        state = {
            ownerfile.ProcessOwner.EXACT_LIVE: _WorkerOwnerState.EXACT,
            ownerfile.ProcessOwner.UNVERIFIABLE: _WorkerOwnerState.UNVERIFIABLE,
            ownerfile.ProcessOwner.DEAD: _WorkerOwnerState.RECLAIMABLE,
            ownerfile.ProcessOwner.REUSED: _WorkerOwnerState.RECLAIMABLE,
        }[process_owner]
    return _WorkerOwner(state, record, observed)


def _inspect_worker_lock() -> _WorkerOwner:
    return _inspect_worker_lock_path(worker_lock_path())


def _inspect_legacy_worker_lock() -> _WorkerOwner:
    return _inspect_worker_lock_path(legacy_worker_lock_path())


def _legacy_worker_lock_clear_for_acquire() -> bool:
    """Fence a still-live protocol-9 model owner during the v10 transition."""
    inspected = _inspect_legacy_worker_lock()
    if inspected.state is _WorkerOwnerState.ABSENT:
        return True
    if inspected.state is _WorkerOwnerState.RECLAIMABLE:
        # Exact PID/start classification proves the old model owner is gone.
        # A sandboxed reader may be unable to delete DATA_DIR state, but that
        # must not turn a dead pre-upgrade record into permanent unavailability.
        if inspected.snapshot is not None:
            _discard_record(legacy_worker_lock_path(), inspected.snapshot)
        return True
    if (inspected.state is _WorkerOwnerState.MALFORMED_STALE
            and inspected.snapshot is not None
            and _discard_record(
                legacy_worker_lock_path(), inspected.snapshot)):
        return _inspect_legacy_worker_lock().state is _WorkerOwnerState.ABSENT
    return False


def _worker_owner_nonce(lifetime: ownerfile.Handle) -> str:
    observed = lifetime.verify(require_stable_mtime=True)
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("semantic worker owner must be an object")
        pid = int(record.get("pid") or 0)
        process_start = str(record.get("process_start") or "")
        nonce = _hex_token(record.get("nonce"), 32)
        tree_bound = record.get("tree_bound")
        named_job = record.get("named_job", False)
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError) as exc:
        raise ownerfile.OwnershipLost(
            "semantic worker owner record is malformed") from exc
    if (pid != os.getpid()
            or process_start != str(common.process_start_identity(pid) or "")
            or nonce is None
            or tree_bound is not True
            or named_job is not common.WIN):
        raise ownerfile.OwnershipLost(
            "semantic worker owner identity is not current")
    return nonce


def _same_owner(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("pid", "process_start"))


def _descriptor_bound_to_lock(rec: dict, lock: _WorkerOwner) -> bool:
    if (lock.state is not _WorkerOwnerState.EXACT
            or lock.record is None
            or lock.record.get("tree_bound") is not True
            or not _same_owner(lock.record, rec)):
        return False
    try:
        version = int(rec.get("version"))
        port = int(rec.get("port"))
        started_at = float(rec.get("started_at"))
    except (OverflowError, TypeError, ValueError):
        return False
    if (rec.get("tree_bound") is not True
            or not 0 < port < 65536
            or not math.isfinite(started_at)
            or _hex_token(rec.get("token"), 64) is None):
        return False
    if 0 < version < PROTOCOL:
        return True
    lock_nonce = _hex_token(lock.record.get("nonce"), 32)
    descriptor_nonce = _hex_token(rec.get("owner_nonce"), 32)
    return (
        version == PROTOCOL
        and lock_nonce is not None
        and lock_nonce == descriptor_nonce
        and lock.record.get("named_job") is rec.get("named_job"))


def _request_and_drain_worker(rec: dict, descriptor, timeout_s: float):
    pid = int(rec.get("pid") or 0)
    expected_start = str(rec.get("process_start") or "")
    deadline = time.monotonic() + max(0.0, float(timeout_s))

    def request_stop(target: dict):
        remaining = deadline - time.monotonic()
        tree = None
        if common.WIN and target.get("named_job") is True:
            import winjob

            tree = winjob.open_exact(pid, expected_start)
            if tree is None:
                return False, None
        acknowledged = (
            remaining > 0
            and _request_worker_stop(
                target, timeout_s=min(0.5, remaining)))
        if not acknowledged and tree is not None:
            tree.close()
            tree = None
        return acknowledged, tree

    stop_ack, ack_tree = (
        request_stop(rec) if descriptor is not None else (False, None))
    drained_after_ack = False
    try:
        while True:
            if stop_ack and not drained_after_ack:
                drained_after_ack = True
                remaining = max(0.0, deadline - time.monotonic())
                if ack_tree is not None:
                    drained = ack_tree.terminate_and_wait(remaining)
                else:
                    drained = (
                        remaining > 0
                        and _terminate_worker_tree(rec, remaining))
                if drained:
                    return True, descriptor, True
            state = _process_owner(pid, expected_start)
            if state in (
                    ownerfile.ProcessOwner.DEAD,
                    ownerfile.ProcessOwner.REUSED):
                if common.WIN and rec.get("named_job") is True:
                    return False, descriptor, stop_ack
                if (not common.WIN and rec.get("tree_bound") is True
                        and common._process_group_active(pid) is not False):
                    if time.monotonic() >= deadline:
                        return False, descriptor, stop_ack
                    time.sleep(0.02)
                    continue
                return True, descriptor, stop_ack
            if time.monotonic() >= deadline:
                return False, descriptor, stop_ack
            if descriptor is None:
                candidate = _reconcile_descriptor(deadline=deadline)
                if (candidate is not None
                        and _descriptor_bound_to_lock(
                            candidate[0],
                            _WorkerOwner(_WorkerOwnerState.EXACT, rec))):
                    descriptor = candidate
                    stop_ack, ack_tree = request_stop(candidate[0])
            time.sleep(0.02)
    finally:
        if ack_tree is not None:
            ack_tree.close()


def _terminate_worker_tree(rec: dict, timeout_s: float) -> bool:
    pid = int(rec.get("pid") or 0)
    expected_start = str(rec.get("process_start") or "")
    if rec.get("tree_bound") is not True:
        return False
    wait_s = max(0.0, timeout_s)
    if common.WIN:
        if rec.get("named_job") is not True:
            return False
        return common.terminate_exact_process_tree(
            pid, expected_start, wait_s=wait_s,
            require_bound_tree=True)
    return common.terminate_exact_process_tree(
        pid, expected_start, wait_s=wait_s,
        require_bound_tree=True)


def _stop_worker_under_start_claim(
        grace_s: float, fallback_s: float, deadline: float) -> dict:
    stop_ack = False
    fallback_termination = False
    first_pid = None
    replacements = 0
    last_generation = None
    inspections = 0
    while True:
        inspections += 1
        if inspections > 256:
            return _stop_outcome(
                False, running=True, blocked=True,
                owner_state="replacement-churn", stop_ack=stop_ack,
                fallback_termination=fallback_termination,
                pid=first_pid, replacements=replacements)
        descriptor = _reconcile_descriptor(deadline=deadline)
        inspected = _inspect_worker_lock()
        if descriptor is not None:
            rec = descriptor[0]
            lock = (
                (inspected.record, inspected.snapshot)
                if (inspected.state is _WorkerOwnerState.EXACT
                    and inspected.record is not None
                    and inspected.snapshot is not None
                    and _descriptor_bound_to_lock(rec, inspected))
                else None)
        elif (inspected.state is _WorkerOwnerState.EXACT
              and inspected.record is not None
              and inspected.snapshot is not None):
            rec = inspected.record
            lock = (inspected.record, inspected.snapshot)
        elif inspected.state in (
                _WorkerOwnerState.RECLAIMABLE,
                _WorkerOwnerState.MALFORMED_STALE):
            if (inspected.snapshot is None
                    or not _discard_worker_record(
                        worker_lock_path(), inspected.snapshot)):
                return _stop_outcome(
                    False, running=True, blocked=True,
                    owner_state="owner-record-blocked", stop_ack=stop_ack,
                    fallback_termination=fallback_termination,
                    pid=first_pid, replacements=replacements)
            continue
        elif inspected.state is _WorkerOwnerState.ABSENT:
            if _descriptor_entry_present():
                return _stop_outcome(
                    False, blocked=True, owner_state="descriptor-blocked",
                    stop_ack=stop_ack,
                    fallback_termination=fallback_termination,
                    pid=first_pid, replacements=replacements)
            legacy = _inspect_legacy_worker_lock()
            if legacy.state in (
                    _WorkerOwnerState.RECLAIMABLE,
                    _WorkerOwnerState.MALFORMED_STALE):
                if (legacy.snapshot is None
                        or not _discard_record(
                            legacy_worker_lock_path(), legacy.snapshot)):
                    return _stop_outcome(
                        False, running=True, blocked=True,
                        owner_state="owner-record-blocked",
                        stop_ack=stop_ack,
                        fallback_termination=fallback_termination,
                        pid=first_pid, replacements=replacements)
                continue
            if (legacy.state is _WorkerOwnerState.EXACT
                    and legacy.record is not None):
                legacy_pid = int(legacy.record.get("pid") or 0)
                if first_pid is None:
                    first_pid = legacy_pid
                if legacy.record.get("tree_bound") is not True:
                    if time.monotonic() >= deadline:
                        return _stop_outcome(
                            False, running=True, blocked=True,
                            owner_state="in-process", stop_ack=stop_ack,
                            fallback_termination=fallback_termination,
                            pid=first_pid, replacements=replacements)
                    time.sleep(0.02)
                    continue
                if (common.WIN
                        and legacy.record.get("named_job") is not True):
                    return _stop_outcome(
                        False, running=True, blocked=True,
                        owner_state="legacy-tree", stop_ack=stop_ack,
                        fallback_termination=fallback_termination,
                        pid=first_pid, replacements=replacements)
                remaining = max(0.0, deadline - time.monotonic())
                terminated = (
                    remaining > 0
                    and _terminate_worker_tree(
                        legacy.record, min(fallback_s, remaining)))
                fallback_termination = (
                    fallback_termination or terminated)
                if terminated:
                    continue
                return _stop_outcome(
                    False, running=True, owner_state="active",
                    stop_ack=stop_ack,
                    fallback_termination=fallback_termination,
                    pid=first_pid, replacements=replacements)
            if legacy.state is not _WorkerOwnerState.ABSENT:
                return _stop_outcome(
                    False, running=True, blocked=True,
                    owner_state=legacy.state.value, stop_ack=stop_ack,
                    fallback_termination=fallback_termination,
                    pid=first_pid, replacements=replacements)
            return _stop_outcome(
                True, stop_ack=stop_ack,
                fallback_termination=fallback_termination,
                pid=first_pid, replacements=replacements)
        else:
            return _stop_outcome(
                False, running=True, blocked=True,
                owner_state=inspected.state.value, stop_ack=stop_ack,
                fallback_termination=fallback_termination,
                pid=first_pid, replacements=replacements)
        pid = int(rec.get("pid") or 0)
        generation = (
            pid, str(rec.get("process_start") or ""),
            str(rec.get("owner_nonce") or rec.get("nonce") or ""))
        if last_generation is None:
            first_pid = pid
        elif generation != last_generation:
            replacements += 1
        last_generation = generation
        if descriptor is None and rec.get("tree_bound") is not True:
            owner_snapshot = lock[1] if lock is not None else None
            release_deadline = min(
                deadline, time.monotonic() + grace_s)
            while owner_snapshot is not None:
                try:
                    _verify_record(worker_lock_path(), owner_snapshot)
                except OSError:
                    break
                if time.monotonic() >= release_deadline:
                    return _stop_outcome(
                        False, running=True, blocked=True,
                        owner_state="in-process", stop_ack=stop_ack,
                        fallback_termination=fallback_termination,
                        pid=first_pid, replacements=replacements)
                time.sleep(0.02)
            continue
        if (common.WIN and rec.get("tree_bound") is True
                and rec.get("named_job") is not True):
            return _stop_outcome(
                False, running=True, blocked=True,
                owner_state="legacy-tree", stop_ack=stop_ack,
                fallback_termination=fallback_termination,
                pid=first_pid, replacements=replacements)
        remaining = max(0.0, deadline - time.monotonic())
        request_budget = min(grace_s, remaining)
        exited, descriptor, acknowledged = _request_and_drain_worker(
            rec, descriptor, request_budget)
        stop_ack = stop_ack or acknowledged
        if not exited:
            remaining = max(0.0, deadline - time.monotonic())
            used_fallback = (
                remaining > 0
                and _terminate_worker_tree(
                    rec, min(fallback_s, remaining)))
            fallback_termination = (
                fallback_termination or used_fallback)
            exited = used_fallback
        if not exited:
            final_owner = _inspect_worker_lock()
            if final_owner.state in (
                    _WorkerOwnerState.ABSENT,
                    _WorkerOwnerState.RECLAIMABLE,
                    _WorkerOwnerState.MALFORMED_STALE):
                continue
            if (final_owner.state is _WorkerOwnerState.EXACT
                    and final_owner.record is not None):
                final_nonce = (
                    final_owner.record.get("owner_nonce")
                    or final_owner.record.get("nonce") or "")
                final_generation = (
                    int(final_owner.record.get("pid") or 0),
                    str(final_owner.record.get("process_start") or ""),
                    str(final_nonce))
                if final_generation != generation:
                    continue
            elif final_owner.state is not _WorkerOwnerState.EXACT:
                return _stop_outcome(
                    False, running=True, blocked=True,
                    owner_state=final_owner.state.value,
                    stop_ack=stop_ack,
                    fallback_termination=fallback_termination,
                    pid=first_pid, replacements=replacements)
            return _stop_outcome(
                False, running=True, owner_state="active",
                stop_ack=stop_ack,
                fallback_termination=fallback_termination,
                pid=first_pid, replacements=replacements)
        if descriptor is not None:
            _discard_record(descriptor_path(), descriptor[1])
        if lock is not None:
            _discard_worker_record(worker_lock_path(), lock[1])


def stop_worker_and_wait(
        grace_s: float = 5.0, fallback_s: float = 2.0) -> dict:
    """Fence new launches, then stop and settle the exact semantic owner."""
    if _worker_coordination_refusal_reason() is not None:
        return _stop_outcome(
            False, running=False, blocked=True, owner_state="read-only")
    grace_s = float(grace_s)
    fallback_s = float(fallback_s)
    grace_s = (
        max(0.0, min(300.0, grace_s))
        if math.isfinite(grace_s) else 0.0)
    fallback_s = (
        max(0.0, min(300.0, fallback_s))
        if math.isfinite(fallback_s) else 0.0)
    deadline = time.monotonic() + grace_s + fallback_s
    claim = None
    while claim is None:
        claim = _acquire_start_claim()
        if claim is not None:
            try:
                claim.verify(require_stable_mtime=True)
            except OSError:
                _release_start_claim(claim)
                claim = None
                break
            continue
        if not _start_claim_active() or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    if claim is None:
        return _stop_outcome(
            False, running=True, blocked=True,
            owner_state="start-claim")
    try:
        return _stop_worker_under_start_claim(
            grace_s, fallback_s, deadline)
    finally:
        _release_start_claim(claim)


def _retire_incompatible(
        rec: dict, observed: ownerfile.Snapshot, *, bound_owner: bool,
        deadline: float | None = None) -> None:
    """Retire the exact old semantic process without a 4s failed-start detour."""
    if _worker_coordination_refusal_reason() is not None:
        return
    pid = int(rec["pid"])
    expected_start = str(rec.get("process_start") or "")
    if common.WIN and rec.get("named_job") is not True:
        if not bound_owner:
            _discard_record(descriptor_path(), observed)
        return
    _discard_record(descriptor_path(), observed)
    if (not bound_owner or pid == os.getpid()
            or expected_start in _UNVERIFIABLE_STARTS):
        return
    now = time.monotonic()
    deadline = min(
        now + 1.0,
        float(deadline) if deadline is not None else now + 1.0)
    remaining = max(0.0, deadline - now)
    acknowledged = (
        remaining > 0
        and _request_worker_stop(rec, timeout_s=min(0.5, remaining)))
    if acknowledged:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0 and _terminate_worker_tree(rec, remaining):
            return
    state = _process_owner(pid, expected_start)
    grace_deadline = min(deadline, time.monotonic() + 0.25)
    while (state is ownerfile.ProcessOwner.EXACT_LIVE
           and time.monotonic() < grace_deadline):
        time.sleep(0.01)
        state = _process_owner(pid, expected_start)
    if state is ownerfile.ProcessOwner.EXACT_LIVE:
        _terminate_worker_tree(
            rec, max(0.0, deadline - time.monotonic()))


class _DescriptorState(Enum):
    ABSENT = "absent"
    READY = "ready"
    BLOCKED = "blocked"
    MALFORMED_FRESH = "malformed-fresh"
    MALFORMED_STALE = "malformed-stale"
    LEGACY = "legacy"
    UNVERIFIABLE = "unverifiable"
    RECLAIMABLE = "reclaimable"
    INCOMPATIBLE = "incompatible"
    UNBOUND = "unbound"
    PROTECTED = "protected"


@dataclass(frozen=True)
class _DescriptorObservation:
    state: _DescriptorState
    record: dict | None = None
    snapshot: ownerfile.Snapshot | None = None
    bound_owner: bool = False


def _inspect_descriptor() -> _DescriptorObservation:
    """Observe the endpoint and its exact owner without changing either."""
    path = descriptor_path()
    try:
        observed = ownerfile.snapshot(path)
    except FileNotFoundError:
        return _DescriptorObservation(_DescriptorState.ABSENT)
    except OSError:
        return _DescriptorObservation(_DescriptorState.BLOCKED)
    try:
        rec = json.loads(observed.raw)
        if not isinstance(rec, dict):
            raise TypeError("semantic worker descriptor must be an object")
        raw_pid = rec.get("pid")
        raw_port = rec.get("port")
        raw_start = rec.get("process_start")
        if (not isinstance(raw_pid, int)
                or isinstance(raw_pid, bool)
                or not isinstance(raw_port, int)
                or isinstance(raw_port, bool)
                or not isinstance(raw_start, str)):
            raise TypeError("semantic worker descriptor schema is invalid")
        pid, port, expected_start = raw_pid, raw_port, raw_start
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        state = (
            _DescriptorState.MALFORMED_FRESH
            if _record_is_fresh(observed.mtime)
            else _DescriptorState.MALFORMED_STALE)
        return _DescriptorObservation(state, snapshot=observed)
    if pid <= 0 or not 0 < port < 65536:
        state = (
            _DescriptorState.MALFORMED_FRESH
            if _record_is_fresh(observed.mtime)
            else _DescriptorState.MALFORMED_STALE)
        return _DescriptorObservation(state, rec, observed)
    if expected_start in _UNVERIFIABLE_STARTS:
        return _DescriptorObservation(
            _DescriptorState.LEGACY, rec, observed)
    owner = ownerfile.classify_process(
        pid, expected_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    if owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        return _DescriptorObservation(
            _DescriptorState.UNVERIFIABLE, rec, observed)
    if owner is not ownerfile.ProcessOwner.EXACT_LIVE:
        return _DescriptorObservation(
            _DescriptorState.RECLAIMABLE, rec, observed)
    lock = _inspect_worker_lock()
    bound_owner = _descriptor_bound_to_lock(rec, lock)
    compatible = _descriptor_compatible(rec)
    if not compatible and not bound_owner:
        legacy_lock = _inspect_legacy_worker_lock()
        bound_owner = _descriptor_bound_to_lock(rec, legacy_lock)
        if (not bound_owner and legacy_lock.state not in (
                _WorkerOwnerState.ABSENT,
                _WorkerOwnerState.RECLAIMABLE,
                _WorkerOwnerState.MALFORMED_STALE)):
            return _DescriptorObservation(
                _DescriptorState.PROTECTED, rec, observed)
    if not compatible:
        return _DescriptorObservation(
            _DescriptorState.INCOMPATIBLE, rec, observed, bound_owner)
    if bound_owner:
        return _DescriptorObservation(
            _DescriptorState.READY, rec, observed, True)
    if lock.state in (
            _WorkerOwnerState.ABSENT,
            _WorkerOwnerState.EXACT,
            _WorkerOwnerState.RECLAIMABLE,
            _WorkerOwnerState.MALFORMED_STALE):
        return _DescriptorObservation(
            _DescriptorState.UNBOUND, rec, observed)
    return _DescriptorObservation(
        _DescriptorState.PROTECTED, rec, observed)


def _reconcile_descriptor(
        *, deadline: float | None = None
        ) -> tuple[dict, ownerfile.Snapshot] | None:
    """Reconcile stale endpoint state and return one usable generation."""
    inspected = _inspect_descriptor()
    rec = inspected.record
    observed = inspected.snapshot
    if (inspected.state is _DescriptorState.READY
            and rec is not None and observed is not None):
        return rec, observed
    if _worker_coordination_refusal_reason() is not None:
        return None
    if (inspected.state is _DescriptorState.LEGACY
            and rec is not None and observed is not None):
        remaining = (
            0.5 if deadline is None
            else max(0.0, min(0.5, deadline - time.monotonic())))
        if remaining > 0:
            _request_worker_stop(rec, timeout_s=remaining)
        _discard_record(descriptor_path(), observed)
    elif (inspected.state is _DescriptorState.INCOMPATIBLE
          and rec is not None and observed is not None):
        _retire_incompatible(
            rec, observed, bound_owner=inspected.bound_owner,
            deadline=deadline)
    elif (inspected.state in (
            _DescriptorState.MALFORMED_STALE,
            _DescriptorState.RECLAIMABLE,
            _DescriptorState.UNBOUND)
          and observed is not None):
        _discard_record(descriptor_path(), observed)
    return None


def _descriptor_entry_present() -> bool:
    try:
        descriptor_path().lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def resident_status(*, include_resources: bool = False) -> dict:
    """Public, token-free worker status for doctor/status surfaces."""
    descriptor = _inspect_descriptor()
    if descriptor.state is not _DescriptorState.READY:
        inspected = _inspect_worker_lock()
        descriptor_blocked = descriptor.state not in (
            _DescriptorState.ABSENT,
            _DescriptorState.MALFORMED_STALE,
            _DescriptorState.RECLAIMABLE,
            _DescriptorState.UNBOUND)
        if (inspected.state is _WorkerOwnerState.EXACT
                and inspected.record is not None):
            pid = int(inspected.record.get("pid") or 0)
            inprocess = inspected.record.get("tree_bound") is not True
            blocked = descriptor_blocked
            return {
                "running": False,
                "starting": not inprocess and not blocked,
                "inprocess": inprocess and not blocked,
                "protected": True, "blocked": blocked,
                "pid": pid,
                "owner_state": (
                    f"descriptor-{descriptor.state.value}"
                    if blocked else inspected.state.value),
                "reason": (
                    _owner_state_reason(
                        f"descriptor-{descriptor.state.value}")
                    if blocked else None),
                "descriptor_state": descriptor.state.value,
                "rss_bytes": (
                    common.process_rss_bytes(pid)
                    if include_resources else None),
            }
        blocked = descriptor_blocked or inspected.state in (
            _WorkerOwnerState.ORPHANED_GROUP,
            _WorkerOwnerState.UNVERIFIABLE,
            _WorkerOwnerState.MALFORMED_FRESH,
            _WorkerOwnerState.BLOCKED)
        owner_state = (
            f"descriptor-{descriptor.state.value}"
            if descriptor_blocked
            else inspected.state.value)
        return {
            "running": False, "starting": False, "protected": blocked,
            "blocked": blocked, "owner_state": owner_state,
            "reason": _owner_state_reason(owner_state) if blocked else None,
            "descriptor_state": descriptor.state.value,
            "descriptor_blocked": descriptor_blocked,
        }
    rec = descriptor.record
    if rec is None:
        return {
            "running": False, "starting": False, "protected": True,
            "blocked": True, "owner_state": "descriptor-invalid",
            "reason": _owner_state_reason("descriptor-invalid"),
        }
    pid = int(rec["pid"])
    return {
        "running": True,
        "pid": pid,
        "owner": str(rec.get("owner") or "disposable"),
        "build_id": str(rec.get("build_id") or ""),
        "descriptor_state": descriptor.state.value,
        "age_s": max(0, int(time.time() - float(rec.get("started_at") or time.time()))),
        "rss_bytes": common.process_rss_bytes(pid) if include_resources else None,
    }


def _start_claim_active() -> bool:
    try:
        start = ownerfile.snapshot(start_claim_path())
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return _start_claim_protected(start)


def _semantic_owner_pending() -> bool:
    inspected = _inspect_worker_lock()
    if inspected.state in (
            _WorkerOwnerState.EXACT,
            _WorkerOwnerState.ORPHANED_GROUP,
            _WorkerOwnerState.UNVERIFIABLE,
            _WorkerOwnerState.MALFORMED_FRESH,
            _WorkerOwnerState.BLOCKED):
        return True
    return _descriptor_entry_present() or _start_claim_active()


def _timeout_s(override: float | None = None) -> float:
    if override is not None:
        return max(0.001, min(300.0, float(override)))
    try:
        return max(1.0, min(300.0, float(os.environ.get(
            "AGREP_SEM_WORKER_TIMEOUT_S", "120"))))
    except ValueError:
        return 120.0


def _read_json_response(response: http.client.HTTPResponse) -> dict:
    length = response.getheader("Content-Length")
    try:
        declared = int(length) if length is not None else -1
    except ValueError:
        declared = -1
    if declared > MAX_RESPONSE_BYTES:
        raise ValueError("resident semantic response exceeded the size limit")
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("resident semantic response exceeded the size limit")
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("resident semantic response was not an object")
    return obj


def _request_deadline_ns(headers) -> int:
    values = headers.get_all(REQUEST_DEADLINE_HEADER)
    if values is None or len(values) != 1:
        raise ValueError("missing or duplicate semantic request deadline")
    raw = values[0]
    if len(raw) > 20 or not raw.isascii() or not raw.isdecimal():
        raise ValueError("invalid semantic request deadline")
    deadline = int(raw)
    now = time.monotonic_ns()
    if deadline <= 0 or deadline - now > MAX_REQUEST_DEADLINE_NS:
        raise ValueError("invalid semantic request deadline")
    return deadline


def _cancel_timed_out_worker(rec: dict) -> bool:
    """Synchronously retire only the exact worker generation that timed out."""
    if int(rec.get("pid") or 0) == os.getpid():
        return False
    inspected = _inspect_descriptor()
    if (inspected.state is not _DescriptorState.READY
            or inspected.record is None or inspected.snapshot is None
            or not _same_worker_generation(inspected.record, rec)):
        return False
    if not _terminate_worker_tree(rec, TIMEOUT_RETIRE_S):
        return False
    after = _inspect_descriptor()
    if (after.record is not None and after.snapshot is not None
            and _same_worker_generation(after.record, rec)):
        _discard_record(descriptor_path(), after.snapshot)
    return True


def _request_slot_raw() -> bytes | None:
    process_start = common.process_start_identity(os.getpid())
    if process_start is None:
        return None
    return json.dumps({
        "pid": os.getpid(), "process_start": process_start,
        "nonce": secrets.token_hex(16),
    }, separators=(",", ":")).encode()


def _request_slot_reclaimable(observed: ownerfile.Snapshot) -> bool:
    try:
        record = json.loads(observed.raw)
        pid = record.get("pid")
        process_start = record.get("process_start")
        nonce = _hex_token(record.get("nonce"), 32)
        if (not isinstance(record, dict) or not isinstance(pid, int)
                or isinstance(pid, bool) or pid <= 0
                or not isinstance(process_start, str) or not process_start
                or nonce is None):
            raise ValueError("invalid request owner")
    except (AttributeError, RecursionError, TypeError, ValueError,
            json.JSONDecodeError):
        return not _record_is_fresh(observed.mtime)
    owner = ownerfile.classify_process(
        pid, process_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    return owner in (ownerfile.ProcessOwner.DEAD, ownerfile.ProcessOwner.REUSED)


def _acquire_request_slot(deadline: float) -> ownerfile.Handle | None:
    refusal = _request_coordination_refusal_reason()
    if refusal is not None:
        raise ResidentSemanticPreflightUnavailable(refusal)
    raw = _request_slot_raw()
    if raw is None:
        raise ResidentSemanticPreflightUnavailable(_coordination_failure(
            "create", "this process has no verifiable start identity"))
    try:
        _prepare_ephemeral_coordination_dir()
    except OSError as exc:
        raise ResidentSemanticPreflightUnavailable(
            _coordination_failure("prepare", exc)) from exc
    path = request_lock_path()
    while True:
        try:
            return ownerfile.create_exclusive(
                path, raw, mode=0o600, retain_fd=True)
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path)
            except FileNotFoundError:
                continue
            except ownerfile.OwnershipLost:
                # The request slot is recreated between serialized callers, so
                # this exact handoff is contention rather than corruption. Stay
                # inside the caller's deadline, as for create/snapshot races.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResidentSemanticPreflightTimeout(
                        "semantic request expired while queued")
                time.sleep(min(REQUEST_SLOT_POLL_S, remaining))
                continue
            except OSError as exc:
                raise ResidentSemanticPreflightUnavailable(
                    _coordination_failure("inspect", exc)) from exc
            if (_request_slot_reclaimable(observed)
                    and _discard_request_record(path, observed)):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResidentSemanticPreflightTimeout(
                    "semantic request expired while queued")
            time.sleep(min(REQUEST_SLOT_POLL_S, remaining))
        except OSError as exc:
            raise ResidentSemanticPreflightUnavailable(
                _coordination_failure("create", exc)) from exc


def _release_request_slot(slot: ownerfile.Handle) -> bool:
    try:
        return bool(slot.release(tombstone=True, require_stable_mtime=True))
    except OSError:
        slot.close()
        return False


def _request_worker_retire_handoff(rec: dict) -> bool:
    """Ask an exact resident to yield when this sandbox cannot use loopback."""
    nonce = _hex_token(rec.get("owner_nonce"), 32)
    if nonce is None or removal_fence.background_removal_active():
        return False
    process_start = common.process_start_identity(os.getpid())
    if not process_start:
        return False
    path = retire_request_path(nonce)
    raw = json.dumps({
        "pid": os.getpid(), "process_start": process_start,
        "target_pid": int(rec.get("pid") or 0),
        "target_start": str(rec.get("process_start") or ""),
    }, separators=(",", ":")).encode("utf-8")
    try:
        ownerfile.create_exclusive(path, raw, mode=0o600)
        return True
    except FileExistsError:
        return True
    except OSError:
        return False


def _retire_handoff_requested(owner_nonce: str) -> bool:
    try:
        ownerfile.snapshot(retire_request_path(owner_nonce), max_bytes=4096)
        return True
    except OSError:
        return False


def _discard_retire_handoff(owner_nonce: str) -> None:
    path = retire_request_path(owner_nonce)
    try:
        observed = ownerfile.snapshot(path, max_bytes=4096)
        ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    except OSError:
        pass


def request_coordination_status(*, timeout_s: float = 0.10) -> dict:
    """Exercise the caller's exact request-serialization record safely."""
    deadline = time.monotonic() + max(0.01, min(1.0, float(timeout_s)))
    try:
        slot = _acquire_request_slot(deadline)
    except ResidentSemanticTimeout:
        return {
            "state": "busy", "writable": None,
            "path": str(request_lock_path()),
            "reason": "another semantic query currently owns serialization",
        }
    except ResidentSemanticPreflightUnavailable as exc:
        return {
            "state": "blocked", "writable": False,
            "path": str(request_lock_path()), "reason": str(exc),
        }
    if not _release_request_slot(slot):
        return {
            "state": "release-failed", "writable": False,
            "path": str(request_lock_path()),
            "reason": _coordination_failure(
                "release", "the exact probe record could not be removed"),
        }
    return {
        "state": "ready", "writable": True,
        "path": str(request_lock_path()), "reason": None,
    }


def _worker_request(rec: dict, payload: dict, timeout_s: float | None = None) -> dict | None:
    """Request an already-discovered worker.

    ``None`` is returned only when connect failed before the request was accepted,
    so an in-process retry cannot duplicate work. Once bytes may have been sent, a
    timeout/reset becomes ResidentSemanticUnavailable and the caller uses keyword.
    """
    request_started = time.perf_counter()
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > MAX_BODY_BYTES:
        raise ResidentSemanticUnavailable(
            "semantic request exceeds the transport size limit")
    query_timeout = _timeout_s(timeout_s)
    deadline = time.monotonic() + query_timeout
    slot = _acquire_request_slot(deadline)

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ResidentSemanticPreflightTimeout(
                "semantic request expired while queued")
        return max(0.001, value)

    conn = None
    watchdog = None
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", int(rec["port"]),
            timeout=min(CONNECT_TIMEOUT_S, remaining()))
        try:
            conn.connect()
        except PermissionError as exc:
            _request_worker_retire_handoff(rec)
            reason = common.terminal_safe(f"{type(exc).__name__}: {exc}")
            raise ResidentSemanticLoopbackUnavailable(
                "semantic client cannot connect to the local worker at "
                f"127.0.0.1:{int(rec['port'])} ({reason}); allow local "
                f"loopback sockets, then inspect with {_diagnostic_command()}") from exc
        except (ConnectionRefusedError, OSError):
            return None
        request_budget = remaining()
        if conn.sock is not None:
            conn.sock.settimeout(request_budget)
        deadline_ns = (
            time.monotonic_ns()
            + math.ceil(request_budget * 1_000_000_000))
        # Per-op socket timeouts reset on every recv, so a trickled response
        # could outlive the deadline unboundedly; the watchdog severs the wire
        # (bound at arm time: getresponse() nulls conn.sock on close responses).
        expired = threading.Event()
        wire = conn.sock

        def _expire() -> None:
            expired.set()
            if wire is not None:
                try:
                    wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        watchdog = threading.Timer(request_budget, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.request(
                "POST", "/v1/search", body=body,
                headers={
                    "Authorization": f"Bearer {rec['token']}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                    REQUEST_DEADLINE_HEADER: str(deadline_ns),
                })
            response = conn.getresponse()
            try:
                obj = _read_json_response(response)
            except (RecursionError, UnicodeDecodeError, ValueError,
                    json.JSONDecodeError) as exc:
                raise ResidentSemanticUnavailable(
                    "resident semantic worker returned an invalid response") from exc
        except TimeoutError as exc:
            _cancel_timed_out_worker(rec)
            raise ResidentSemanticTimeout("resident semantic query timed out") from exc
        except ResidentSemanticUnavailable:
            if expired.is_set():
                _cancel_timed_out_worker(rec)
                raise ResidentSemanticTimeout(
                    "resident semantic query timed out")
            raise
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            _cancel_timed_out_worker(rec)
            if expired.is_set():
                raise ResidentSemanticTimeout(
                    "resident semantic query timed out") from exc
            raise ResidentSemanticUnavailable(
                "resident semantic worker disconnected during the query") from exc
        if response.status == 200 and obj.get("ok") is True:
            result = obj.get("result")
            if isinstance(result, dict):
                if payload.get("timing") is True:
                    timing = result.setdefault("_semantic_timing", {})
                    roundtrip = (time.perf_counter() - request_started) * 1000.0
                    timing["client_roundtrip_ms"] = round(roundtrip, 3)
                    worker_ms = timing.get("worker_search_ms")
                    if isinstance(worker_ms, (int, float)):
                        timing["client_overhead_ms"] = round(
                            max(0.0, roundtrip - float(worker_ms)), 3)
                return result
            raise ResidentSemanticUnavailable(
                "resident semantic worker returned an invalid result")
        if response.status == 409:
            raise ResidentSemanticUnavailable(str(obj.get("error") or
                                                  "semantic index unavailable"))
        if response.status == 408:
            raise ResidentSemanticTimeout(str(obj.get("error") or
                                              "semantic request expired"))
        if response.status == 400:
            raise ValueError(str(obj.get("error") or "invalid semantic request"))
        raise ResidentSemanticUnavailable(str(obj.get("error") or
                                              "resident semantic worker failed"))
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if conn is not None:
            conn.close()
        _release_request_slot(slot)


def _start_claim_raw() -> bytes | None:
    process_start = common.process_start_identity(os.getpid())
    if process_start is None:
        return None
    return json.dumps({
        "pid": os.getpid(), "at": time.time(),
        "process_start": process_start,
        "nonce": secrets.token_hex(16),
    }, separators=(",", ":")).encode()


@dataclass(frozen=True)
class _StartClaimRecord:
    pid: int
    at: float
    process_start: str
    nonce: str


def _parse_start_claim(
        observed: ownerfile.Snapshot) -> _StartClaimRecord | None:
    try:
        rec = json.loads(observed.raw)
        if not isinstance(rec, dict):
            raise TypeError("start claim must be an object")
        raw_pid = rec.get("pid")
        raw_at = rec.get("at")
        process_start = rec.get("process_start")
        nonce = _hex_token(rec.get("nonce"), 32)
        if (not isinstance(raw_pid, int)
                or isinstance(raw_pid, bool)
                or raw_pid <= 0
                or not isinstance(raw_at, (int, float))
                or isinstance(raw_at, bool)
                or not isinstance(process_start, str)
                or process_start in _UNVERIFIABLE_STARTS
                or nonce is None):
            raise ValueError("start claim schema is invalid")
        at = float(raw_at)
        if not math.isfinite(at):
            raise ValueError("start claim age is not finite")
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None
    return _StartClaimRecord(raw_pid, at, process_start, nonce)


def _start_claim_protected(observed: ownerfile.Snapshot) -> bool:
    claim = _parse_start_claim(observed)
    if claim is None:
        return _record_is_fresh(observed.mtime)
    age = time.time() - claim.at
    owner = ownerfile.classify_process(
        claim.pid, claim.process_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)
    return (
        0.0 <= age <= START_CLAIM_GRACE_S
        and owner in (
            ownerfile.ProcessOwner.EXACT_LIVE,
            ownerfile.ProcessOwner.UNVERIFIABLE))


def _launch_claim_current(
        expected: ownerfile.Snapshot, nonce: str) -> bool:
    try:
        observed = ownerfile.snapshot(start_claim_path())
    except OSError:
        return False
    claim = _parse_start_claim(observed)
    return (
        observed.identity == expected.identity
        and observed.raw == expected.raw
        and claim is not None
        and claim.nonce == nonce
        and _start_claim_protected(observed))


def _acquire_start_claim() -> ownerfile.Handle | None:
    refusal = _worker_coordination_refusal_reason()
    if refusal is not None:
        return None
    path = start_claim_path()
    for _ in range(2):
        raw = _start_claim_raw()
        if raw is None:
            return None
        try:
            return ownerfile.create_exclusive(path, raw, mode=0o600)
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path)
            except OSError:
                return None
            if _start_claim_protected(observed):
                return None
            try:
                removed = ownerfile.remove_exact(
                    path, observed, tombstone=True,
                    require_stable_mtime=True)
            except OSError:
                return None
            if not removed:
                return None
        except OSError as exc:
            if isinstance(exc, PermissionError):
                raise ResidentSemanticPreflightUnavailable(_record_failure(
                    "semantic worker startup coordination", path,
                    "create", exc)) from exc
            return None
    return None


def _release_start_claim(claim: ownerfile.Handle) -> None:
    if _worker_coordination_refusal_reason() is not None:
        claim.close()
        return
    try:
        claim.release(tombstone=True, require_stable_mtime=True)
    except OSError:
        pass


def _spawn_worker(claim: ownerfile.Handle) -> subprocess.Popen:
    refusal = _worker_coordination_refusal_reason()
    if refusal is not None:
        raise OSError(f"{refusal}; cannot launch the semantic worker")
    bind = loopback_bind_status()
    if not bind["bindable"]:
        raise ResidentSemanticLoopbackUnavailable(str(bind["reason"]))
    parsed = _parse_start_claim(claim.snapshot)
    if parsed is None:
        raise ownerfile.OwnershipLost(
            "semantic launch claim is malformed")
    logf = common.open_bounded_log("semantic-worker.log")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--serve"]
    env = dict(os.environ)
    env[_LAUNCH_CLAIM_ENV] = parsed.nonce
    kw = {
        "stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
        "cwd": str(common.REPO_ROOT), "env": env, "close_fds": True,
    }
    if sys.platform == "win32":
        kw["creationflags"] = common.windows_background_child_flags(0x08000208)
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kw)
    finally:
        logf.close()


def _wait_for_worker(
        wait_s: float, *, process: subprocess.Popen | None = None) -> dict | None:
    deadline = time.monotonic() + wait_s
    while True:
        hit = _reconcile_descriptor()
        if hit is not None:
            return hit[0]
        if process is not None:
            if process.poll() is not None:
                return None
        else:
            inspected = _inspect_worker_lock()
            if inspected.state is _WorkerOwnerState.EXACT:
                if (inspected.record is None
                        or inspected.record.get("tree_bound") is not True):
                    return None
            elif inspected.state in (
                    _WorkerOwnerState.ORPHANED_GROUP,
                    _WorkerOwnerState.UNVERIFIABLE,
                    _WorkerOwnerState.MALFORMED_FRESH,
                    _WorkerOwnerState.BLOCKED):
                return None
            elif not _start_claim_active():
                return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _worker_query_disabled_reason() -> str | None:
    if os.environ.get("AGREP_NO_SEM_WORKER"):
        return "AGREP_NO_SEM_WORKER disables semantic worker queries"
    if removal_fence.background_removal_active():
        return "agrep removal blocks semantic worker queries"
    return None


def _worker_query_disabled() -> bool:
    return _worker_query_disabled_reason() is not None


def _worker_disabled() -> bool:
    return bool(_worker_query_disabled()
                or _worker_coordination_refusal_reason() is not None)


def _ensure_worker(start_timeout_s: float | None = None) -> dict | None:
    wait_s = (START_TIMEOUT_S if start_timeout_s is None else
              max(0.05, min(START_TIMEOUT_S, float(start_timeout_s))))
    if _worker_disabled():
        return None
    hit = _reconcile_descriptor()
    if hit is not None:
        return hit[0]
    if _descriptor_entry_present():
        hit = _reconcile_descriptor()
        if hit is not None:
            return hit[0]
        return None
    inspected = _inspect_worker_lock()
    if inspected.state is _WorkerOwnerState.EXACT:
        if (inspected.record is not None
                and inspected.record.get("tree_bound") is True):
            return _wait_for_worker(wait_s)
        return None
    if inspected.state in (
            _WorkerOwnerState.ORPHANED_GROUP,
            _WorkerOwnerState.UNVERIFIABLE,
            _WorkerOwnerState.MALFORMED_FRESH,
            _WorkerOwnerState.BLOCKED):
        return None
    claim = _acquire_start_claim()
    if claim is not None:
        try:
            try:
                claim.verify(require_stable_mtime=True)
            except OSError:
                return None
            inspected = _inspect_worker_lock()
            if inspected.state in (
                    _WorkerOwnerState.EXACT,
                    _WorkerOwnerState.ORPHANED_GROUP,
                    _WorkerOwnerState.UNVERIFIABLE,
                    _WorkerOwnerState.MALFORMED_FRESH,
                    _WorkerOwnerState.BLOCKED):
                return None
            try:
                process = _spawn_worker(claim)
            except OSError:
                return None
            return _wait_for_worker(wait_s, process=process)
        finally:
            _release_start_claim(claim)
    return _wait_for_worker(wait_s)


def _same_worker_generation(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in (
        "version", "pid", "port", "token", "process_start", "build_id",
        "owner_nonce"))


def search_worker(query: str, *, level: str, k: int,
                  filters: dict | None = None,
                  timing: bool | None = None,
                  timeout_s: float | None = None,
                  start_timeout_s: float | None = None) -> dict | None:
    if _worker_query_disabled():
        return None
    timing_requested = _semantic_timing_enabled() if timing is None else bool(timing)
    try:
        query, level, k, filters, timing_requested = _validate_request({
            "query": query, "level": level, "k": k, "filters": filters or {},
            "timing": timing_requested,
        })
    except ValueError:
        return None
    deadline = (
        None if timeout_s is None
        else time.monotonic() + _timeout_s(timeout_s)
    )

    def remaining() -> float | None:
        if deadline is None:
            return None
        value = deadline - time.monotonic()
        if value <= 0:
            raise ResidentSemanticPreflightTimeout(
                "semantic request expired while queued")
        return value

    start_budget = start_timeout_s
    if deadline is not None:
        available = remaining()
        start_budget = (
            available if start_budget is None
            else min(float(start_budget), available)
        )
    if _worker_coordination_refusal_reason() is not None:
        current = _reconcile_descriptor(deadline=deadline)
        rec = current[0] if current is not None else None
    else:
        rec = (_ensure_worker() if start_budget is None
               else _ensure_worker(start_budget))
        if (rec is None
                and _worker_coordination_refusal_reason() is not None):
            current = _reconcile_descriptor(deadline=deadline)
            rec = current[0] if current is not None else None
    if rec is None:
        if (_worker_query_disabled()
                or _worker_coordination_refusal_reason() is not None):
            return None
        if _semantic_owner_pending():
            # No descriptor or sent bytes: a guarded local fallback may own the
            # model. Classify preflight so the bounded pass queues on that owner
            # without treating its inference as a possible duplicate.
            raise ResidentSemanticPreflightUnavailable(
                "semantic worker ownership is still settling")
        return None

    def request(target: dict) -> dict | None:
        try:
            payload = {
                "query": query, "level": level, "k": k, "filters": filters,
                "timing": timing_requested,
            }
            request_budget = remaining()
            return (_worker_request(target, payload) if request_budget is None
                    else _worker_request(target, payload, request_budget))
        except ValueError as exc:
            if not ({"_family_diverse", "_exclude_who", "_include_who",
                     "_exclude_sessions",
                     "exclude_project",
                     "exclude_session",
                     "exclude_session_from_turn", "exclude_family"}
                    & set(filters)):
                raise ResidentSemanticUnavailable(
                    "resident semantic worker rejected the request") from exc
            raise ResidentSemanticProtocolMismatch(
                "resident semantic worker predates the retrieval policy") from exc

    result = request(rec)
    if result is None:
        delay = 0.02 if deadline is None else min(0.02, remaining())
        time.sleep(delay)
        current = _reconcile_descriptor(deadline=deadline)
        if (current is not None
                and _same_worker_generation(current[0], rec)):
            result = request(rec)
            if result is None:
                # _worker_request returns None only before request acceptance.
                raise ResidentSemanticPreflightUnavailable(
                    "resident semantic worker is busy or unreachable")
            return result
        restart_budget = start_budget
        if deadline is not None and restart_budget is not None:
            restart_budget = min(restart_budget, remaining())
        replacement = (
            current[0] if current is not None else
            (_ensure_worker() if restart_budget is None
             else _ensure_worker(restart_budget)))
        if replacement is not None:
            result = request(replacement)
            if result is None:
                # _worker_request returns None only before request acceptance.
                raise ResidentSemanticPreflightUnavailable(
                    "resident semantic worker is busy or unreachable")
            return result
        if _semantic_owner_pending():
            raise ResidentSemanticPreflightUnavailable(
                "semantic worker ownership is still settling")
    return result


def diagnostic_query_status(
        *, ready: bool, timeout_s: float = 1.5) -> dict:
    """Probe each prerequisite, then complete one real semantic request."""
    resident_before = resident_status(include_resources=True)
    bind = loopback_bind_status()
    coordination = request_coordination_status(
        timeout_s=min(0.10, max(0.01, float(timeout_s))))
    discovered = bool(
        resident_before.get("running")
        and resident_before.get("descriptor_state") == "ready")
    alive = bool(
        resident_before.get("running")
        or resident_before.get("starting")
        or resident_before.get("inprocess"))
    result = {
        "discovered": discovered,
        "alive": alive,
        "ready": bool(ready),
        "query_serving": False if not ready else None,
        "coordination": coordination,
        "loopback": bind,
        "resident_before": resident_before,
        "resident_after": resident_before,
        "roundtrip_ms": None,
        "reason": None,
    }
    if not ready:
        result["state"] = "generation-not-ready"
        result["reason"] = "the current semantic generation is not query-ready"
        return result
    if resident_before.get("protected") and not discovered:
        owner_state = str(
            resident_before.get("owner_state") or "protected")
        result["state"] = "worker-protected"
        result["query_serving"] = False
        result["reason"] = (
            "semantic worker ownership is protected "
            f"({common.terminal_safe(owner_state)}); use the active installed "
            "agrep build or wait for its current owner to exit")
        return result
    coordination_busy = (
        coordination.get("state") == "busy"
        and coordination.get("writable") is None)
    if coordination.get("writable") is False:
        result["state"] = "coordination-blocked"
        result["query_serving"] = False
        result["reason"] = str(coordination.get("reason") or
                               "semantic request coordination is unavailable")
        return result
    if coordination.get("writable") is not True and not coordination_busy:
        result["state"] = "coordination-unverified"
        result["query_serving"] = None
        result["reason"] = str(coordination.get("reason") or
                               "semantic request coordination is unverified")
        return result
    if coordination_busy and not discovered:
        result["state"] = "coordination-busy"
        result["query_serving"] = None
        result["reason"] = str(coordination.get("reason") or
                               "another semantic query owns serialization")
        return result
    if not bind["bindable"] and not discovered:
        result["state"] = "loopback-blocked"
        result["query_serving"] = False
        result["reason"] = str(bind["reason"])
        return result
    worker_refusal = _worker_coordination_refusal_reason()
    if worker_refusal is not None and not discovered:
        result["state"] = "owner-blocked"
        result["query_serving"] = False
        result["reason"] = (
            "semantic worker startup is blocked: "
            f"{common.terminal_safe(worker_refusal)}; use the active installed "
            "agrep build or finish upgrading it")
        return result
    started = time.perf_counter()
    try:
        response = search_worker(
            "agrep semantic readiness", level="message", k=1,
            filters={
                "_allow_model_download": False,
                "_diagnostic_only": True,
            },
            timing=False, timeout_s=timeout_s,
            start_timeout_s=min(0.75, max(0.05, float(timeout_s))))
    except ResidentSemanticTimeout as exc:
        result["state"] = "query-busy" if coordination_busy else "query-timeout"
        result["query_serving"] = None if coordination_busy else False
        result["reason"] = str(exc)
    except ResidentSemanticUnavailable as exc:
        result["state"] = "query-failed"
        result["query_serving"] = False
        result["reason"] = str(exc)
    else:
        if isinstance(response, dict):
            integrity = response.get("semantic_integrity")
            semantic_status = response.get("semantic_status")
            status_detail = (
                semantic_status if isinstance(semantic_status, dict) else {})
            unavailable = bool(
                response.get("semantic_unavailable")
                or response.get("score_kind") == "unavailable"
                or status_detail.get("state") == "unavailable")
            if unavailable:
                detail = integrity if isinstance(integrity, dict) else {}
                state = str(
                    detail.get("state")
                    or status_detail.get("state") or "unavailable")
                reason = str(
                    detail.get("reason")
                    or status_detail.get("reason")
                    or response.get("reason")
                    or "semantic retrieval returned an unavailable envelope")
                result["state"] = "query-unavailable"
                result["query_serving"] = False
                result["reason"] = (
                    f"semantic readiness query returned {state}: "
                    f"{common.terminal_safe(reason)}")
            elif not isinstance(response.get("results"), list):
                result["state"] = "query-invalid"
                result["query_serving"] = False
                result["reason"] = (
                    "semantic readiness query returned no result envelope")
            else:
                result["state"] = "query-serving"
                result["query_serving"] = True
        else:
            result["state"] = "worker-unavailable"
            result["query_serving"] = False
            result["reason"] = (
                "no semantic worker accepted the bounded readiness query; "
                f"inspect with {_diagnostic_command()}")
    result["roundtrip_ms"] = round(
        (time.perf_counter() - started) * 1000.0, 3)
    resident_after = resident_status(include_resources=True)
    result["resident_after"] = resident_after
    result["discovered"] = bool(
        resident_after.get("running")
        and resident_after.get("descriptor_state") == "ready")
    result["alive"] = bool(
        resident_after.get("running")
        or resident_after.get("starting")
        or resident_after.get("inprocess"))
    return result


def _validate_request(obj: object) -> tuple[str, str, int, dict, bool]:
    if not isinstance(obj, dict) or set(obj) - {
            "query", "level", "k", "filters", "timing"}:
        raise ValueError("invalid semantic request fields")
    query = obj.get("query")
    level = obj.get("level")
    filters = obj.get("filters") or {}
    timing = obj.get("timing", False)
    if not isinstance(timing, bool):
        raise ValueError("invalid semantic timing flag")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError("invalid semantic query")
    if level not in {"hybrid", "message", "message-session", "chats"}:
        raise ValueError("invalid semantic level")
    try:
        # OverflowError: json accepts 1e1000 as float('inf'); int() then blows
        # past the 400 tuple instead of reading as an out-of-range limit.
        k = int(obj.get("k"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid semantic result limit") from exc
    if not 1 <= k <= 200 or not isinstance(filters, dict):
        raise ValueError("invalid semantic request")
    allowed = {"agent", "project", "exclude_project", "who", "model",
               "model_soft", "chat",
               "since_ms", "until_ms", "exclude_session",
               "exclude_session_from_turn", "exclude_family",
               "_family_diverse", "_exclude_who", "_include_who",
               "_exclude_sessions",
               "_allow_model_download", "_diagnostic_only"}
    if set(filters) - allowed:
        raise ValueError("invalid semantic filter")
    clean = {}
    for key, value in filters.items():
        if key in {"model_soft", "_family_diverse", "exclude_family",
                   "_allow_model_download", "_diagnostic_only"}:
            if not isinstance(value, bool):
                raise ValueError("invalid semantic filter")
        elif key in {"_exclude_who", "_include_who", "_exclude_sessions"}:
            limit = 4 if key == "_exclude_sessions" else 16
            max_chars = 1024 if key == "_exclude_sessions" else 64
            if (not isinstance(value, (list, tuple)) or len(value) > limit
                    or any(not isinstance(item, str) or not item
                           or len(item) > max_chars for item in value)):
                raise ValueError("invalid semantic filter")
            if key == "_include_who" and not value:
                raise ValueError("invalid semantic filter")
            value = tuple(sorted(set(value)))
        elif key == "exclude_session":
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError("invalid semantic filter")
        elif key in {"since_ms", "until_ms", "exclude_session_from_turn"}:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("invalid semantic filter")
            if key == "exclude_session_from_turn" and (
                    value < 0 or not filters.get("exclude_session")):
                raise ValueError("invalid semantic filter")
        elif not isinstance(value, str) or len(value) > 4096:
            raise ValueError("invalid semantic filter")
        clean[key] = value
    if len(json.dumps(clean, separators=(",", ":")).encode()) > MAX_BODY_BYTES // 2:
        raise ValueError("invalid semantic filter")
    return query.strip(), str(level), k, clean, timing


def _acquire_worker_lock(
        tree_bound: bool | None = None) -> ownerfile.Handle | None:
    if _worker_lock_coordination_refusal_reason() is not None:
        return None
    if not _legacy_worker_lock_clear_for_acquire():
        return None
    try:
        _prepare_ephemeral_coordination_dir()
    except OSError:
        return None
    path = worker_lock_path()
    pid = os.getpid()
    process_start = common.process_start_identity(pid)
    if process_start is None:
        return None
    raw = json.dumps({
        "pid": pid, "started_at": time.time(),
        "process_start": process_start, "nonce": secrets.token_hex(16),
        "tree_bound": bool(tree_bound),
        "named_job": bool(tree_bound and common.WIN),
    }, separators=(",", ":")).encode("utf-8")
    for _ in range(2):
        try:
            owner = ownerfile.create_exclusive(
                path, raw, mode=0o600, retain_fd=True)
            if _legacy_worker_lock_clear_for_acquire():
                return owner
            _release_unpublished_owner(owner)
            return None
        except FileExistsError:
            inspected = _inspect_worker_lock()
            if inspected.state is _WorkerOwnerState.ABSENT:
                continue
            if (inspected.state not in (
                    _WorkerOwnerState.RECLAIMABLE,
                    _WorkerOwnerState.MALFORMED_STALE)
                    or inspected.snapshot is None
                    or not _discard_worker_record(path, inspected.snapshot)):
                return None
        except OSError:
            return None
    return None


def _release_unpublished_owner(owner: ownerfile.Handle) -> None:
    if _worker_lock_coordination_refusal_reason() is not None:
        owner.close()
        return
    try:
        owner.release(
            tombstone=True, require_stable_mtime=True)
    except OSError:
        owner.close()


def acquire_resident_owner() -> ownerfile.Handle | None:
    """Acquire a process-tree-bound owner for an explicitly hosted server."""
    if _worker_disabled():
        return None
    if not common.bind_descendants_to_process_lifetime():
        return None
    owner = _acquire_worker_lock(tree_bound=True)
    if owner is not None and removal_fence.background_removal_active():
        _release_unpublished_owner(owner)
        return None
    return owner


def _acquire_launched_owner(nonce: str) -> ownerfile.Handle | None:
    if _worker_disabled():
        return None
    try:
        claim = ownerfile.snapshot(start_claim_path())
    except OSError:
        return None
    if not _launch_claim_current(claim, nonce):
        return None
    if not common.bind_descendants_to_process_lifetime():
        return None
    if not _launch_claim_current(claim, nonce):
        return None
    owner = _acquire_worker_lock(tree_bound=True)
    if owner is None:
        return None
    if (_launch_claim_current(claim, nonce)
            and not removal_fence.background_removal_active()):
        return owner
    _release_unpublished_owner(owner)
    return None


def acquire_inprocess_owner() -> ownerfile.Handle | None:
    """Serialize the explicit in-process fallback with resident model owners."""
    if removal_fence.background_removal_active():
        return None
    owner = _acquire_worker_lock(tree_bound=False)
    if owner is not None and removal_fence.background_removal_active():
        _release_unpublished_owner(owner)
        return None
    return owner


def verify_inprocess_owner(owner: ownerfile.Handle) -> None:
    try:
        owner.verify(require_stable_mtime=True)
    except OSError as exc:
        raise ResidentSemanticUnavailable(
            "in-process semantic ownership changed") from exc


def finish_inprocess_owner(
        owner: ownerfile.Handle, *,
        resources_released: bool) -> None:
    if not resources_released:
        owner.close()
        return
    try:
        owner.release(tombstone=True, require_stable_mtime=True)
    except OSError:
        owner.close()


class _BoundedHTTPServer(HTTPServer):
    """Apply the socket deadline before BaseHTTPRequestHandler parses headers."""

    request_queue_size = 1

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10.0)
        return sock, address


class SemanticWorkerServer:
    """Single-thread semantic owner. All cache/SQLite/model access stays here."""

    def __init__(self, search_fn: Callable | None = None, *,
                 lifetime: ownerfile.Handle):
        if _worker_disabled():
            raise ownerfile.OwnershipLost(
                "semantic worker startup is disabled")
        self.token = secrets.token_hex(32)
        self.search_fn = search_fn
        self.lifetime = lifetime
        self.owner_nonce = _worker_owner_nonce(lifetime)
        self.descriptor_snapshot = None
        self.tree_bound = common.bind_descendants_to_process_lifetime()
        if not self.tree_bound:
            raise ownerfile.OwnershipLost(
                "semantic worker has no verified descendant lifetime")
        self.started_mono = time.monotonic()
        self.last_use_mono = 0.0
        self.requests = 0
        self._idle_sample_at = 0.0
        self._idle_sample_requests = -1
        self._idle_sample_value = 0.0
        self._release_lock = threading.Lock()
        self._resources_released = False
        self._semantic_loaded = False
        self.retire_deadline = 0.0
        self.stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, _format, *_args):
                return

            def _send(self, status: int, obj: dict) -> None:
                raw = json.dumps(obj, separators=(",", ":"),
                                 ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
                self.connection.settimeout(10.0)
                auth = self.headers.get("Authorization", "")
                supplied = auth[7:] if auth.startswith("Bearer ") else ""
                supplied_token = _hex_token(supplied, 64)
                if (supplied_token is None
                        or not hmac.compare_digest(supplied_token, owner.token)):
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return
                if self.path == "/v1/stop":
                    owner.retire_deadline = (
                        time.monotonic() + STOP_ACK_ORPHAN_S)
                    released = owner._release_resources()
                    # The Job owner stays alive until the caller drains its tree.
                    self._send(200 if released else 500, {
                        "ok": released,
                        "error": None if released else "semantic release failed",
                    })
                    return
                if self.path != "/v1/search":
                    self._send(404, {"ok": False, "error": "not found"})
                    return
                if owner.retire_deadline:
                    self._send(503, {
                        "ok": False, "error": "semantic worker is retiring"})
                    return
                if not owner._owns_lifetime():
                    self._send(503, {
                        "ok": False, "error": "semantic ownership changed"})
                    return
                try:
                    deadline_ns = _request_deadline_ns(self.headers)
                except ValueError as exc:
                    self._send(400, {"ok": False, "error": str(exc)})
                    return
                if time.monotonic_ns() >= deadline_ns:
                    self._send(408, {
                        "ok": False, "error": "semantic request expired"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY_BYTES:
                    self._send(413, {"ok": False, "error": "request too large"})
                    return
                try:
                    obj = json.loads(self.rfile.read(length).decode("utf-8"))
                    query, level, k, filters, timing_requested = _validate_request(obj)
                except (RecursionError, UnicodeDecodeError,
                        json.JSONDecodeError, ValueError) as exc:
                    self._send(400, {"ok": False, "error": str(exc)})
                    return
                if time.monotonic_ns() >= deadline_ns:
                    self._send(408, {
                        "ok": False, "error": "semantic request expired"})
                    return
                owner.requests += 1
                owner.last_use_mono = time.monotonic()
                search_started = time.perf_counter()
                try:
                    diagnostic_only = bool(
                        filters.pop("_diagnostic_only", False))
                    allow_model_download = bool(
                        filters.pop("_allow_model_download", False))
                    if diagnostic_only:
                        allow_model_download = False
                    else:
                        try:
                            indexd_runtime.SEARCH_BEAT_PATH.touch()
                        except OSError:
                            pass
                    if owner.search_fn is None:
                        owner._semantic_loaded = True
                        import semantic
                        result = semantic.search(
                            query, level=level, k=k, filters=filters,
                            timing=timing_requested,
                            allow_model_download=allow_model_download,
                            diagnostic_only=diagnostic_only)
                    else:
                        result = owner.search_fn(
                            query, level=level, k=k, filters=filters)
                    if timing_requested and isinstance(result, dict):
                        timing = result.setdefault("_semantic_timing", {})
                        timing["worker_search_ms"] = round(
                            (time.perf_counter() - search_started) * 1000.0, 3)
                except Exception as exc:  # classify without importing model stack early
                    semantic_module = sys.modules.get("semantic")
                    unavailable_type = (
                        getattr(semantic_module, "SemanticUnavailable", None)
                        if semantic_module is not None else None)
                    unavailable = (
                        isinstance(unavailable_type, type)
                        and isinstance(exc, unavailable_type))
                    if unavailable:
                        self._send(409, {"ok": False, "error": str(exc)})
                    else:
                        # Request shape was fully validated above. Any ValueError
                        # here is model/index/data failure, not client misuse; return
                        # an unavailable response so the CLI falls back to keyword.
                        common.log("semantic worker query failed: "
                                   f"{type(exc).__name__}")
                        owner.stop.set()
                        self._send(500, {"ok": False,
                                         "error": "semantic worker failed"})
                    return
                finally:
                    owner.last_use_mono = time.monotonic()
                if time.monotonic_ns() >= deadline_ns:
                    owner.stop.set()
                    self._send(408, {
                        "ok": False, "error": "semantic request expired"})
                    return
                if not owner._owns_lifetime():
                    self._send(503, {
                        "ok": False, "error": "semantic ownership changed"})
                    return
                self._send(200, {"ok": True, "result": result})

        try:
            self.http = _BoundedHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            reason = common.terminal_safe(f"{type(exc).__name__}: {exc}")
            raise ResidentSemanticPreflightUnavailable(
                "semantic worker cannot bind loopback 127.0.0.1:0 "
                f"({reason}); allow local loopback sockets, then inspect with "
                f"{_diagnostic_command()}") from exc
        self.http.timeout = min(OWNER_POLL_S, HANDOFF_POLL_S)
        self.port = int(self.http.server_address[1])
        self.record = json.dumps({
            "version": PROTOCOL, "pid": os.getpid(), "port": self.port,
            "token": self.token, "started_at": time.time(),
            "process_start": common.process_start_identity(os.getpid()),
            "build_id": WORKER_BUILD_ID, "capabilities": list(CAPABILITIES),
            "owner": "disposable", "tree_bound": self.tree_bound,
            "named_job": bool(common.WIN),
            "owner_nonce": self.owner_nonce,
        }, separators=(",", ":"))
        try:
            self._verify_lifetime()
            self.descriptor_snapshot = _publish_descriptor(
                self.lifetime, self.record, self.owner_nonce)
            self._verify_lifetime()
        except BaseException:
            if self.descriptor_snapshot is not None:
                _discard_record(descriptor_path(), self.descriptor_snapshot)
            self.http.server_close()
            raise

    def _verify_lifetime(self) -> None:
        self.lifetime.verify(require_stable_mtime=True)
        if self.descriptor_snapshot is not None:
            _verify_record(descriptor_path(), self.descriptor_snapshot)

    def _owns_lifetime(self) -> bool:
        try:
            self._verify_lifetime()
            return True
        except OSError:
            self.stop.set()
            return False

    def _release_resources(self) -> bool:
        with self._release_lock:
            if self._resources_released:
                return True
            try:
                semantic_module = (
                    sys.modules.get("semantic")
                    if self._semantic_loaded else None)
                if (semantic_module is not None
                        and not bool(semantic_module.release())):
                    return False
            except Exception as exc:  # noqa: BLE001 -- process exit is the final fallback
                common.log(f"semantic worker release failed: {type(exc).__name__}")
                return False
            self._resources_released = True
            return True

    def _idle_limit(self) -> float:
        now = time.monotonic()
        if (self.requests != self._idle_sample_requests
                or now - self._idle_sample_at >= IDLE_POLICY_REFRESH_S):
            self._idle_sample_value = common.semantic_idle_seconds(self.requests)
            self._idle_sample_at = now
            self._idle_sample_requests = self.requests
        return self._idle_sample_value

    def serve(self) -> None:
        common.log(f"semantic worker ready (pid {os.getpid()}, adaptive idle lease)")
        exit_reason = "stop-event"
        try:
            while not self.stop.is_set():
                if _retire_handoff_requested(self.owner_nonce):
                    exit_reason = "retire-handoff"
                    break
                if not self._owns_lifetime():
                    exit_reason = "ownership-lost"
                    break
                self.http.handle_request()
                if _retire_handoff_requested(self.owner_nonce):
                    exit_reason = "retire-handoff"
                    break
                if not self._owns_lifetime():
                    exit_reason = "ownership-lost"
                    break
                now = time.monotonic()
                if self.retire_deadline and now >= self.retire_deadline:
                    exit_reason = "stop-request"
                    break
                if not self.requests and now - self.started_mono > STARTUP_ORPHAN_S:
                    exit_reason = "startup-orphan"
                    break
                if (self.last_use_mono and
                        now - self.last_use_mono > self._idle_limit()):
                    exit_reason = "idle"
                    break
        finally:
            released = self._release_resources()
            if self.descriptor_snapshot is not None:
                _discard_record(descriptor_path(), self.descriptor_snapshot)
            _discard_retire_handoff(self.owner_nonce)
            self.http.server_close()
            suffix = "" if released else " after failed explicit release"
            common.log(
                "semantic worker process exiting and releasing RSS"
                f"{suffix} ({exit_reason})")

    def request_stop(self) -> None:
        self.stop.set()
        try:
            with socket.create_connection(
                    ("127.0.0.1", self.port), timeout=0.1):
                pass
        except OSError:
            pass


def serve_main() -> int:
    if _worker_disabled():
        return 0
    launch_nonce = _hex_token(
        os.environ.pop(_LAUNCH_CLAIM_ENV, ""), 32)
    owner = (
        _acquire_launched_owner(launch_nonce)
        if launch_nonce is not None else None)
    if owner is None:
        common.log(
            "semantic worker launch generation is no longer current; exiting.")
        return 0
    try:
        owner.verify(require_stable_mtime=True)
        server = SemanticWorkerServer(lifetime=owner)
        server.serve()
    except ownerfile.OwnershipLost:
        return 0
    except ResidentSemanticPreflightUnavailable as exc:
        common.log(str(exc))
        return 2
    finally:
        owner.close()
        # The persistent exact-owner record bridges descriptor removal to process exit.
        # The next starter, status-aware stopper, or crash recovery reaps it.
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_main() if "--serve" in sys.argv[1:] else 2)
