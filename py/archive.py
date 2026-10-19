"""Archive of the agents' own source stores + `agrep restore`.

The index deliberately propagates deletions (privacy-correct); this is the explicit
backup tier BELOW it. Capture snapshots every discovered store file; restore rehydrates
the archived bytes back to the store path, so the agent's own CLI can try to resume the
session and the normal ingest re-indexes it - nothing downstream changes. Plain files
are stored byte-for-byte; sqlite stores are backup-api snapshots (see below).

Storage model (data/archive/):

    manifest.jsonl   append-only, one record per captured version. The newest record
                     per path wins. {path, agent, sessions, sha256, size, mtime_ns,
                     ts, chunks: [{sha256, len, base}], codec}
    store/ab/<sha256>.xz   content-addressed chunk bodies (lzma). A chunk is either a
                     whole file or an appended suffix - identical content across
                     versions/paths is stored once.

Transcripts are append-only event logs (compaction APPENDS a summary record, it does
not rewrite history), so the common capture is a prefix-delta: if the file still starts
with the previous version's bytes, only the new suffix becomes a chunk. Any non-append
change falls back to a whole-file chunk. Restore concatenates the chunk chain and
verifies the pinned sha256 before writing a single byte - a failed pin means the
archive is damaged and restore refuses rather than resurrecting a corrupt store.

sqlite stores are snapshotted through sqlite's backup API (a raw copy of a live db can
be torn mid-transaction; a torn restore is worse than none), then chunked the same way.

Discovery mirrors the Rust adapters' store globs. It is a static table here - the two
lists drift only when a new adapter lands, and the selftest count-checks them.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import lzma
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import common
import ownerfile
import surface_policy as surface
from hookless.locators import discovery_home
from hookless.native import opencode_data_dirs, opencode_db_paths

HOME = Path(discovery_home())
ARCHIVE_DIR = common.DATA_DIR / "archive"
MANIFEST = ARCHIVE_DIR / "manifest.jsonl"
STORE = ARCHIVE_DIR / "store"
CONFIG = ARCHIVE_DIR / "config.json"
HEALTH = ARCHIVE_DIR / "capture-health.json"
_CODEC = "xz"  # recorded per chunk so a future codec swap never breaks old archives
_SQLITE_SETTLE_S = 900  # don't image a db someone wrote in the last 15 min
# a sqlite store is these files together; a restore publishes the set, not the db
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# Restore only tolerates transient Windows locks; a live store must fail fast.
_RESTORE_SIDECAR_MOVE_ATTEMPTS = 6
_MAX_ARCHIVE_BYTES = 16 * 1024**3
_MAX_CHUNKS = 100_000
_MAX_MANIFEST_BYTES = 256 * 1024**2
_MAX_MANIFEST_LINE = 2 * 1024**2
_MAX_MANIFEST_RECORDS = 250_000
# growth chunks a record carries before a fresh whole-file capture: ~110 bytes per
# descriptor keeps the record well inside _MAX_MANIFEST_LINE (2 MiB)
_MAX_APPEND_CHAIN = 8192
_TEMP_PREFIXES = (".copy-", ".snapshot-", ".progress-")  # pass temps; orphans are swept
_SPAWNER_ENV = "AGREP_ARCHIVE_SPAWNER"  # "<pid>:<start>" of the daemon that spawned the pass
# a store rewritten in place on every message is re-imaged by the tick at most this often
_REWRITE_RECAPTURE_MS = 10 * 60_000
_REWRITE_REASON = ("rewritten in place; re-captured at most every "
                   f"{_REWRITE_RECAPTURE_MS // 60_000} min on the indexer tick")
_MAX_REGISTRY_BYTES = 4 * 1024**2
_MAX_CONFIG_BYTES = 64 * 1024
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_UNVERIFIABLE_LOCK_STARTS = frozenset(("unknown",))
_LOCK_PUBLISH_GRACE_S = 3.0
_LOCK_WEDGED_S = 300.0
# a running pass that reports no progress for this long is stuck, not slow: one source
# step (a stat, a copy, one backup, one xz) is far shorter even on a loaded box
STALL_BOUND_S = 900.0


class ArchiveStatusBudget(RuntimeError):
    """Routine diagnostics exhausted their manifest-inspection deadline."""

# (agent, glob relative to HOME, sqlite?) - mirrors the Rust adapters' discovery.
# `**` spans zero or more directories; a `*` before it is a required level.
ROOTS: list[tuple[str, str, bool]] = [
    ("claude", ".claude/projects/*/**/*.jsonl", False),
    ("codex", ".codex/sessions/**/rollout-*.jsonl", False),
    ("codex", ".codex/archived_sessions/rollout-*.jsonl", False),
    ("gemini", ".gemini/tmp/*/chats/*.json", False),
    ("gemini", ".gemini/tmp/*/chats/*.jsonl", False),
    ("gemini", ".gemini/tmp/*/chats/*/*.jsonl", False),
    ("antigravity", ".gemini/antigravity-cli/brain/**/*.json", False),
    ("antigravity", ".gemini/antigravity-cli/brain/**/*.jsonl", False),
    ("kimi", ".kimi/sessions/**/*.jsonl", False),
    ("kimi", ".kimi/kimi.json", False),
    ("opencode", ".local/share/opencode/opencode*.db", True),
    ("opencode", ".local/share/opencode/storage/**/*.json", False),
    ("crush", ".local/share/crush/**/crush.db", True),
    ("cursor", "AppData/Roaming/Cursor/User/globalStorage/state.vscdb", True),
    ("cursor", "Library/Application Support/Cursor/User/globalStorage/state.vscdb", True),
    ("cursor", ".config/Cursor/User/globalStorage/state.vscdb", True),
    ("pi", ".pi/agent/sessions/**/*.jsonl", False),
    ("pi", ".pi/agent/sessions/**/*.jsonl.gz", False),
    ("pi", ".pi/agent/archive/sessions/**/*.jsonl", False),
    ("pi", ".pi/agent/archive/sessions/**/*.jsonl.gz", False),
    ("pi", ".omp/agent/sessions/**/*.jsonl", False),
    ("pi", ".omp/agent/sessions/**/*.jsonl.gz", False),
    ("pi", ".omp/agent/archive/sessions/**/*.jsonl", False),
    ("pi", ".omp/agent/archive/sessions/**/*.jsonl.gz", False),
]

# a session id in the filename (uuid-ish or codex rollout stamp) -> restore-by-session
_SESSION_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _data_dir_readonly() -> bool:
    """Whether this archive instance belongs to the exact protected data dir."""
    return common.data_dir_readonly(ARCHIVE_DIR.parent)


def _readonly_error() -> PermissionError:
    return PermissionError(
        "AGREP_DATA_READONLY protects this data directory")


def _config_observation() -> dict:
    """Bounded archive configuration evidence for diagnostic surfaces.

    A missing file is the documented disabled default.  Every other failure is
    distinct from that default so doctor/status cannot call damaged evidence
    "disabled".
    """
    try:
        raw = _read_regular_file(CONFIG, _MAX_CONFIG_BYTES, ARCHIVE_DIR)
    except FileNotFoundError:
        return {"state": "missing", "value": {}}
    except (OSError, ValueError) as exc:
        return {
            "state": "unavailable", "value": None,
            "detail": (
                "archive configuration is unavailable "
                f"({type(exc).__name__}: {exc})"),
        }
    try:
        value = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeError, ValueError) as exc:
        return {
            "state": "unavailable", "value": None,
            "detail": (
                "archive configuration is malformed "
                f"({type(exc).__name__}: {exc})"),
        }
    if not isinstance(value, dict):
        return {
            "state": "unavailable", "value": None,
            "detail": "archive configuration is malformed (not an object)",
        }
    if "enabled" in value and type(value["enabled"]) is not bool:
        return {
            "state": "unavailable", "value": None,
            "detail": "archive configuration has a non-boolean enabled value",
        }
    return {"state": "verified", "value": value}


def _config() -> dict:
    observed = _config_observation()
    if observed["state"] == "unavailable":
        return {}
    return dict(observed["value"])


def _set_config(**kv) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    body = json.dumps({**_config(), **kv}, separators=(",", ":")).encode("utf-8")
    _publish_restore(CONFIG, ARCHIVE_DIR, body, True)


def enabled() -> bool:
    return _config().get("enabled", False)


def set_enabled(on: bool) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    _set_config(enabled=on)


def _keep() -> int:
    """Versions retained per source path; 0 = never prune."""
    k = _config().get("keep", 3)
    return k if isinstance(k, int) and k >= 0 else 3


def _human_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    unit = "B"
    for larger in ("KB", "MB", "GB"):
        if size < 999.95:
            break
        size /= 1000
        unit = larger
    if unit == "B":
        return f"{size:.0f} B"
    return f"{size:.2f} GB" if unit == "GB" else f"{size:.1f} {unit}"


def _captures_retained() -> bool:
    """Captured copies (manifest or chunk store) exist, whatever the config says."""
    return any(_plain_target_exists(path) if path is MANIFEST else path.is_dir()
               for path in (MANIFEST, STORE))


def _disabled_detail(stored: int | None) -> str:
    if not stored or not _captures_retained():
        return "automatic capture is disabled"
    return (f"automatic capture is disabled; {_human_bytes(stored)} of earlier "
            f"captures stay in {ARCHIVE_DIR} (`{common.cli_name()} archive --purge` "
            "deletes them)")


def _try_lock() -> ownerfile.Handle | None:
    """Archive-only lock (NOT the index lock: a capture must never queue behind a
    30-minute reindex, or block one). Non-blocking - the indexer tick just skips a
    pass someone else is running. A dead holder is reclaimed instantly."""
    if _data_dir_readonly():
        return None
    _ensure_plain_parent(ARCHIVE_DIR, ARCHIVE_DIR)
    lock = ARCHIVE_DIR / "lock"
    pid = os.getpid()
    start = common.process_start_identity(pid) or "unknown"
    token = secrets.token_hex(16)
    daemon = os.environ.get(_SPAWNER_ENV, "")
    body = (f"pid={pid} start={start} token={token}"
            + (f" daemon={daemon}" if re.fullmatch(r"[0-9]+:[^\s=]+", daemon) else "")
            + "\n").encode()
    for _ in range(2):
        try:
            return ownerfile.create_held(lock, body, token=token, retain_fd=True)
        except FileExistsError:
            try:
                leaf = lock.lstat()
                if _linklike(lock, leaf) or not stat.S_ISREG(leaf.st_mode):
                    return None
                observed = ownerfile.snapshot(lock)
                leaf_identity = (
                    leaf.st_dev, leaf.st_ino, leaf.st_size,
                    getattr(leaf, "st_mtime_ns", int(leaf.st_mtime * 1e9)))
                if observed.identity != leaf_identity:
                    return None
            except OSError:
                return None
            fields = {}
            try:
                fields = dict(
                    part.split("=", 1) for part in observed.raw.decode().split())
                pid = int(fields.get("pid", "0"))
            except (UnicodeError, ValueError):
                pid = 0
            if pid > 0:
                state = ownerfile.classify_process(
                    pid, fields.get("start"), pid_alive=common.pid_alive,
                    process_start=common.process_start_identity,
                    unverifiable_starts=_UNVERIFIABLE_LOCK_STARTS)
                stranded = ownerfile.stranded_own_record(
                    pid, fields.get("start"), fields.get("token"),
                    process_start=common.process_start_identity)
                if state in (ownerfile.ProcessOwner.EXACT_LIVE,
                             ownerfile.ProcessOwner.UNVERIFIABLE) and not stranded:
                    return None
            else:
                age = time.time() - observed.mtime
                if 0.0 <= age <= _LOCK_PUBLISH_GRACE_S:
                    return None
            if not ownerfile.remove_exact(
                    lock, observed, tombstone=True, require_stable_mtime=True):
                return None
    return None


def _spawner_live(value: str | None) -> bool:
    """The daemon recorded as the pass's spawner is still that exact process, so it
    holds the child's handle and will stop it once the stall bound passes."""
    if not value or ":" not in value:
        return False
    pid_text, start = value.split(":", 1)
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    return pid > 0 and ownerfile.classify_process(
        pid, start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity,
        unverifiable_starts=_UNVERIFIABLE_LOCK_STARTS) == ownerfile.ProcessOwner.EXACT_LIVE


def _lock_status() -> dict:
    """Inspect archive ownership without reclaiming or refreshing it."""
    lock = ARCHIVE_DIR / "lock"
    try:
        leaf = lock.lstat()
        if _linklike(lock, leaf) or not stat.S_ISREG(leaf.st_mode):
            return {"state": "lock-wedged", "path": str(lock),
                    "detail": "archive lock is not a regular file"}
        observed = ownerfile.snapshot(lock)
    except FileNotFoundError:
        return {"state": "unlocked", "path": str(lock)}
    except OSError as exc:
        return {"state": "lock-wedged", "path": str(lock),
                "detail": str(exc)}
    age = max(0.0, time.time() - observed.mtime)
    fields = {}
    try:
        fields = dict(part.split("=", 1) for part in observed.raw.decode().split())
        pid = int(fields.get("pid", "0"))
    except (UnicodeError, ValueError):
        pid = 0
    result = {"path": str(lock), "pid": pid or None, "age_s": age}
    if pid > 0:
        owner = ownerfile.classify_process(
            pid, fields.get("start"), pid_alive=common.pid_alive,
            process_start=common.process_start_identity,
            unverifiable_starts=_UNVERIFIABLE_LOCK_STARTS)
        if owner == ownerfile.ProcessOwner.EXACT_LIVE:
            daemon_live = _spawner_live(fields.get("daemon"))
            beat = progress_beat()
            # a spawned child is judged by its daemon's monotonic clock; only an orphan
            # (no live spawner, nothing will ever judge it) is judged by wall age
            recorded = _recorded_stall(pid)
            if daemon_live:
                idle = recorded if recorded is not None else 0.0
            else:
                idle = age if beat is None else min(beat[1], age)
            return {**result, "state": "busy", "detail": "verified live holder",
                    "idle_s": idle, "stalled": idle > STALL_BOUND_S,
                    "daemon_live": daemon_live}
        if owner == ownerfile.ProcessOwner.UNVERIFIABLE and age <= _LOCK_WEDGED_S:
            return {**result, "state": "lock-protected",
                    "detail": "holder identity is not yet verifiable"}
        if owner in (ownerfile.ProcessOwner.DEAD, ownerfile.ProcessOwner.REUSED):
            # a daemon stop or crash mid-pass: _try_lock reclaims this at once
            return {**result, "state": "interrupted",
                    "detail": "the last capture pass was interrupted; the next pass "
                              "resumes it"}
    elif age <= _LOCK_PUBLISH_GRACE_S:
        return {**result, "state": "lock-protected",
                "detail": "lock owner record is still being published"}
    return {**result, "state": "lock-wedged",
            "detail": "holder is malformed or unverifiable past the grace bound"}


def _unlock(owner: ownerfile.Handle) -> None:
    if _data_dir_readonly():
        owner.close()
        return
    try:
        owner.release(tombstone=True, require_stable_mtime=True)
    except OSError:
        pass


def setup_default() -> None:
    """Create the safe default config without retaining source-store history.

    Kept for callers from older installs; enabling retention now requires the
    explicit consent path in ``agrep setup`` or ``agrep archive --on``.
    """
    if CONFIG.exists():
        return
    set_enabled(False)


def _chunk_path(sha: str) -> Path:
    return STORE / sha[:2] / f"{sha}.xz"


def _store_chunk(data: bytes) -> dict:
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise ValueError("archive chunk exceeds the configured bound")
    sha = hashlib.sha256(data).hexdigest()
    p = _chunk_path(sha)
    if not _plain_target_exists(p):
        try:
            _publish_restore(p, STORE, lzma.compress(data, preset=6), False)
        except FileExistsError:
            _plain_target_exists(p)
    return {"sha256": sha, "len": len(data), "codec": _CODEC}


def _regular_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size,
            getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)))


def _open_regular_file(path: Path, maximum: int, root: Path
                       ) -> tuple[int, os.stat_result, tuple[int, int, int, int]]:
    if maximum < 0:
        raise ValueError("invalid file bound")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("bounded read escapes its root") from exc
    current = root
    for part in relative.parts[:-1]:
        info = current.lstat()
        if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe archive store path")
        current /= part
    info = current.lstat()
    if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("unsafe archive store path")
    leaf = path.lstat()
    if _linklike(path, leaf) or not stat.S_ISREG(leaf.st_mode):
        raise ValueError("bounded file is not a plain regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        identity = _regular_identity(opened)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum
                or (leaf.st_dev, leaf.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("bounded file is not a plausible regular file")
        return fd, opened, identity
    except Exception:
        os.close(fd)
        raise


def _still_holds(info: os.stat_result, identity: tuple[int, int, int, int],
                 length: int) -> bool:
    """The same inode still carries at least the bytes a capture read. Transcripts
    are append-only, so growth past them is fine; a shrink or a swap is not."""
    return (info.st_dev, info.st_ino) == identity[:2] and info.st_size >= length


def _hash_regular_file(path: Path, root: Path, *, prefix: int | None = None
                       ) -> tuple[str, int, str | None, str | None,
                                  tuple[int, int, int, int], os.stat_result]:
    """sha256 of the bytes present at open, split at `prefix` into before/after
    digests when asked. A write that lands during the read forces a second read of
    the same range: an append leaves it byte-identical, an in-place rewrite does not."""
    fd, opened, identity = _open_regular_file(path, _MAX_ARCHIVE_BYTES, root)
    wanted = opened.st_size
    whole = hashlib.sha256()
    before = hashlib.sha256() if prefix is not None else None
    after = hashlib.sha256() if prefix is not None else None
    try:
        if prefix is not None and not 0 <= prefix <= wanted:
            raise ValueError("archive prefix exceeds source size")
        total = 0
        while total < wanted:
            chunk = os.read(fd, min(1024**2, wanted - total))
            if not chunk:
                break
            whole.update(chunk)
            if before is not None and after is not None:
                split = max(0, min(len(chunk), prefix - total))
                before.update(chunk[:split])
                after.update(chunk[split:])
            total += len(chunk)
        current = os.fstat(fd)
        if total != wanted or not _still_holds(current, identity, wanted):
            raise ValueError("bounded file changed while being read")
        if _regular_identity(current) != identity:
            os.lseek(fd, 0, os.SEEK_SET)
            again = hashlib.sha256()
            total = 0
            while total < wanted:
                chunk = os.read(fd, min(1024**2, wanted - total))
                if not chunk:
                    break
                again.update(chunk)
                total += len(chunk)
            if (total != wanted or again.hexdigest() != whole.hexdigest()
                    or not _still_holds(os.fstat(fd), identity, wanted)):
                raise ValueError("source rewritten during archive capture")
    finally:
        os.close(fd)
    return (whole.hexdigest(), wanted, before.hexdigest() if before else None,
            after.hexdigest() if after else None, identity, opened)


def _store_file_chunk(path: Path, root: Path, offset: int, length: int,
                      sha: str, identity: tuple[int, int, int, int]) -> dict:
    if not 0 <= offset <= _MAX_ARCHIVE_BYTES or not 0 <= length <= _MAX_ARCHIVE_BYTES:
        raise ValueError("archive chunk exceeds the configured bound")
    target = _chunk_path(sha)
    if _plain_target_exists(target):
        return {"sha256": sha, "len": length, "codec": _CODEC}
    _require_copy_space(length + 64 * 1024)  # xz never exceeds its input by more than framing
    parent_identity = _ensure_plain_parent(STORE, target.parent)
    fd, name = tempfile.mkstemp(prefix=".chunk-", suffix=".xz", dir=target.parent)
    tmp = Path(name)
    source = -1
    try:
        source, opened, _ = _open_regular_file(path, _MAX_ARCHIVE_BYTES, root)
        if not _still_holds(opened, identity, offset + length):
            raise ValueError("source changed during archive capture")
        os.lseek(source, offset, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = length
        compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=6)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            while remaining:
                chunk = os.read(source, min(1024**2, remaining))
                if not chunk:
                    raise ValueError("source ended during archive capture")
                remaining -= len(chunk)
                if remaining % (64 * 1024**2) < len(chunk):
                    _progress(-remaining)
                digest.update(chunk)
                packed = compressor.compress(chunk)
                if packed:
                    stream.write(packed)
            stream.write(compressor.flush())
            stream.flush()
            os.fsync(stream.fileno())
        if (not _still_holds(os.fstat(source), identity, offset + length)
                or digest.hexdigest() != sha):
            raise ValueError("source changed during archive capture")
        if _ensure_plain_parent(STORE, target.parent) != parent_identity:
            raise ValueError("archive store parent changed during publication")
        try:
            _replace_restore_temp(tmp, target, parent_identity, False)
        except FileExistsError:
            _plain_target_exists(target)
    finally:
        if source >= 0:
            os.close(source)
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
    return {"sha256": sha, "len": length, "codec": _CODEC}


def _load_chunk(rec: dict) -> bytes:
    output = io.BytesIO()
    _stream_chunk(rec, output, None)
    return output.getvalue()


def _stream_chunk(rec: dict, output, aggregate: object | None) -> int:
    expected = rec.get("len")
    sha = rec.get("sha256")
    if (type(expected) is not int or not 0 <= expected <= _MAX_ARCHIVE_BYTES
            or not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None):
        raise ValueError("invalid chunk descriptor")
    path = _chunk_path(sha)
    packed_limit = min(_MAX_ARCHIVE_BYTES, expected + 1024**2)
    fd, opened, identity = _open_regular_file(path, packed_limit, STORE)
    decoder = lzma.LZMADecompressor()
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            packed = os.read(fd, 1024**2)
            if not packed:
                break
            if decoder.eof:
                raise ValueError(f"chunk {sha[:12]} has trailing data")
            while True:
                data = decoder.decompress(packed, max_length=expected - total + 1)
                packed = b""
                if data:
                    total += len(data)
                    if total > expected:
                        raise ValueError(f"chunk {sha[:12]} exceeds its pinned length")
                    digest.update(data)
                    if aggregate is not None:
                        aggregate.update(data)
                    output.write(data)
                if decoder.eof:
                    if decoder.unused_data:
                        raise ValueError(f"chunk {sha[:12]} has trailing data")
                    break
                if decoder.needs_input:
                    break
        if (_regular_identity(os.fstat(fd)) != identity
                or opened.st_size > packed_limit):
            raise ValueError(f"chunk {sha[:12]} changed while being read")
    finally:
        os.close(fd)
    if total != expected or not decoder.eof:
        raise ValueError(f"chunk {sha[:12]} has wrong length or framing")
    if digest.hexdigest() != sha:
        raise ValueError(f"chunk {sha[:12]} corrupt in archive store")
    return total


def _manifest_values(
        *, deadline: float | None = None,
) -> tuple[list[object], bool, bool]:
    """Parseable values plus torn-tail and unsafe-record state."""
    values: list[object] = []
    torn_tail = False
    unsafe_record = False
    try:
        leaf = MANIFEST.lstat()
    except FileNotFoundError:
        return values, torn_tail, unsafe_record
    if (_linklike(MANIFEST, leaf) or not stat.S_ISREG(leaf.st_mode)
            or leaf.st_size > _MAX_MANIFEST_BYTES):
        raise ValueError("archive manifest is not a bounded regular file")
    fd = os.open(MANIFEST, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    opened = os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode)
            or (leaf.st_dev, leaf.st_ino) != (opened.st_dev, opened.st_ino)):
        os.close(fd)
        raise ValueError("archive manifest changed during open")
    consumed = 0
    lines = 0
    with os.fdopen(fd, "rb") as f:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise ArchiveStatusBudget(
                    "archive manifest inspection exceeded its routine budget")
            line = f.readline(_MAX_MANIFEST_LINE + 1)
            if not line:
                break
            lines += 1
            if lines > _MAX_MANIFEST_RECORDS:
                raise ValueError("archive manifest exceeds its record bound")
            consumed += len(line)
            if consumed > _MAX_MANIFEST_BYTES:
                raise ValueError("archive manifest exceeds its byte bound")
            if len(line) > _MAX_MANIFEST_LINE:
                raise ValueError("archive manifest exceeds its record bound")
            try:
                values.append(json.loads(line.decode("utf-8")))
            except (TypeError, UnicodeError, ValueError):
                if line.endswith(b"\n"):
                    unsafe_record = True
                else:
                    torn_tail = True
    return values, torn_tail, unsafe_record


def _records() -> list[dict]:
    """Every strictly restorable record, oldest first."""
    values, _, _ = _manifest_values()
    return [rec for rec in values if _valid_manifest_record(rec)]


def _bounded_int(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _stored_absolute_parts(value: object) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    if value.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "//?/", "//./")):
        return None
    if value.startswith(("\\\\", "//")):
        return None
    if re.match(r"^[A-Za-z]:[\\/]", value):
        candidate = PureWindowsPath(value)
        if (not candidate.is_absolute()
                or re.fullmatch(r"[A-Za-z]:", candidate.drive) is None):
            return None
        parts = tuple(candidate.parts[1:])
        if not parts or any(not _portable_path_part(part) for part in parts):
            return None
        return "windows", parts
    if value.startswith("/"):
        candidate = PurePosixPath(value)
        parts = tuple(candidate.parts[1:])
        if not parts or any(not _portable_path_part(part) for part in parts):
            return None
        return "posix", parts
    return None


_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _portable_path_part(part: str) -> bool:
    if (not part or part in (".", "..") or "/" in part or "\\" in part
            or ":" in part or part.endswith((" ", "."))):
        return False
    if any(ord(char) < 32 for char in part):
        return False
    return part.split(".", 1)[0].upper() not in _WINDOWS_DEVICE_NAMES


def _match_segments(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_match_segments(parts[skip:], pattern[1:])
                   for skip in range(len(parts) + 1))
    return (bool(parts) and fnmatch.fnmatch(parts[0], pattern[0])
            and _match_segments(parts[1:], pattern[1:]))


def _match_root_pattern(relative: Path, pattern: str) -> bool:
    """Whole-path match of a ROOTS glob; Path.match would read `**` as one segment."""
    return _match_segments(relative.parts, Path(pattern).parts)


def _portable_source_relative(
        value: object, agent: object, is_sqlite: object) -> Path | None:
    if (not isinstance(value, str) or not value or "\0" in value
            or not isinstance(agent, str) or not isinstance(is_sqlite, bool)):
        return None
    parsed = _stored_absolute_parts(value)
    if parsed is None:
        return None
    _, parts = parsed
    for root_agent, pattern, root_sqlite in ROOTS:
        if agent != root_agent or is_sqlite != root_sqlite:
            continue
        for index in range(len(parts) - 1, -1, -1):
            relative = Path(*parts[index:])
            if _match_root_pattern(relative, pattern):
                return relative
    for index in range(len(parts) - 1, -1, -1):
        relative = Path(*parts[index:])
        if agent == "claude" and not is_sqlite and _claude_source(relative):
            return relative
        if agent == "antigravity" and not is_sqlite and _antigravity_source(relative):
            return relative
        if agent == "kimi" and not is_sqlite and _kimi_source(relative):
            return relative
        if agent == "cline" and not is_sqlite and _cline_portable_source(relative):
            return relative
        if agent == "opencode" and _opencode_portable_source(relative, is_sqlite):
            return relative
        if agent == "crush" and _crush_portable_source(relative, is_sqlite):
            return relative
    return None


def _cline_task_source(relative: Path) -> bool:
    return ((len(relative.parts) == 3 and relative.parts[0] == "tasks"
             and relative.suffix.lower() == ".json")
            or relative == Path("state/taskHistory.json"))


def _cline_portable_source(relative: Path) -> bool:
    parts = relative.parts
    roots = (
        (".cline", "data"),
        ("Library", "Application Support"),
        (".config",),
        ("AppData", "Roaming"),
    )
    for root in roots:
        if parts[:len(root)] != root:
            continue
        rest = parts[len(root):]
        if root == (".cline", "data"):
            return _cline_task_source(Path(*rest))
        if (len(rest) >= 5 and rest[0] in (
                "Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium")
                and rest[1:4] == (
                    "User", "globalStorage", "saoudrizwan.claude-dev")):
            return _cline_task_source(Path(*rest[4:]))
    return False


def _opencode_tail(relative: Path, is_sqlite: bool) -> bool:
    if is_sqlite:
        name = relative.name.lower()
        return (len(relative.parts) == 1 and name.startswith("opencode")
                and name.endswith(".db") and ".bak" not in name
                and ".corrupted" not in name)
    return _opencode_storage(relative)


def _opencode_portable_source(relative: Path, is_sqlite: bool) -> bool:
    for root in (
            Path(".local/share/opencode"),
            Path("AppData/Local/opencode")):
        try:
            tail = relative.relative_to(root)
        except ValueError:
            continue
        return _opencode_tail(tail, is_sqlite)
    return False


def _crush_portable_source(relative: Path, is_sqlite: bool) -> bool:
    for root in (Path(".local/share/crush"), Path("AppData/Local/crush")):
        try:
            tail = relative.relative_to(root)
        except ValueError:
            continue
        # `**/crush.db` in the source table is literal: crush keeps per-project
        # stores at any depth under the root, capture takes them, so restore has
        # to accept them too. The registry itself is only ever at the root.
        return (tail.name == "crush.db" if is_sqlite
                else tail == Path("projects.json"))
    return False


def _portable_home_location(relative: Path) -> tuple[Path, Path]:
    parts = relative.parts
    if parts[:2] == ("AppData", "Roaming") and os.environ.get("APPDATA"):
        return Path(os.path.abspath(os.environ["APPDATA"])), Path(*parts[2:])
    if parts[:2] == ("AppData", "Local") and os.environ.get("LOCALAPPDATA"):
        return Path(os.path.abspath(os.environ["LOCALAPPDATA"])), Path(*parts[2:])
    return HOME.absolute(), relative


def _portable_restore_location(
        value: object, agent: object, is_sqlite: object,
) -> tuple[Path, Path] | None:
    relative = _portable_source_relative(value, agent, is_sqlite)
    if relative is not None:
        return _portable_home_location(relative)
    if not isinstance(value, str) or not isinstance(agent, str):
        return None
    parsed = _stored_absolute_parts(value)
    if parsed is None:
        return None
    _, parts = parsed
    if agent == "opencode" and isinstance(is_sqlite, bool):
        for index, part in enumerate(parts[:-1]):
            if part.lower() != "opencode":
                continue
            tail = Path(*parts[index + 1:])
            if _opencode_tail(tail, is_sqlite):
                return Path(opencode_data_dirs(str(HOME))[0]).absolute(), tail
    if agent == "crush" and isinstance(is_sqlite, bool):
        for index, part in enumerate(parts[:-1]):
            if part.lower() != "crush":
                continue
            tail = Path(*parts[index + 1:])
            if ((is_sqlite and tail == Path("crush.db"))
                    or (not is_sqlite and tail == Path("projects.json"))):
                return _crush_data_dirs()[0].absolute(), tail
    if agent == "cline" and is_sqlite is False:
        for index, part in enumerate(parts[:-1]):
            if part != "data":
                continue
            tail = Path(*parts[index + 1:])
            if _cline_task_source(tail):
                return _cline_data_roots()[-1], tail
    return None


def _manifest_source_shape(value: object, agent: object, is_sqlite: object) -> bool:
    if (not isinstance(value, str) or not value or "\0" in value
            or not isinstance(agent, str) or not isinstance(is_sqlite, bool)):
        return False
    parsed = _stored_absolute_parts(value)
    if parsed is None:
        return False
    _, parts = parsed
    name = parts[-1]
    if _portable_source_relative(value, agent, is_sqlite) is not None:
        return True
    if agent == "cursor":
        return is_sqlite and name.lower() == "state.vscdb"
    if agent == "opencode":
        return is_sqlite or (not is_sqlite and name.lower().endswith(".json")
                             and "storage" in parts)
    if agent == "cline" and not is_sqlite:
        return ((len(parts) >= 3 and parts[-3] == "tasks"
                 and name.lower().endswith(".json"))
                or parts[-2:] == ("state", "taskHistory.json"))
    if agent == "crush":
        return ((is_sqlite and name.lower() == "crush.db")
                or (not is_sqlite and name.lower() == "projects.json"))
    return False


def _future_manifest_record(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    allowed = {"path", "agent", "sessions", "sha256", "size", "mtime_ns", "ts",
               "sqlite", "chunks", "source_sig"}
    required = allowed - {"source_sig"}
    if not required.issubset(value) or set(value).issubset(allowed):
        return False
    legacy = {key: value[key] for key in value if key in allowed}
    return _valid_manifest_record(legacy)


def _record_migrated(record: dict) -> bool:
    if _source_location(record.get("path")) is not None:
        return False
    return _portable_restore_location(
        record.get("path"), record.get("agent"), record.get("sqlite")) is not None


def _valid_manifest_record(rec: object) -> bool:
    required = {"path", "agent", "sessions", "sha256", "size", "mtime_ns", "ts",
                "sqlite", "chunks"}
    if (not isinstance(rec, dict)
            or set(rec) not in (required, required | {"source_sig"})):
        return False
    if (not isinstance(rec["path"], str) or len(rec["path"]) > 32_768
            or not _manifest_source_shape(rec["path"], rec["agent"], rec["sqlite"])
            or not isinstance(rec["agent"], str)
            or re.fullmatch(r"[a-z0-9_-]{1,32}", rec["agent"]) is None
            or not isinstance(rec["sqlite"], bool)
            or not isinstance(rec["sha256"], str)
            or _SHA_RE.fullmatch(rec["sha256"]) is None
            or not _bounded_int(rec["size"], _MAX_ARCHIVE_BYTES)
            or not _bounded_int(rec["mtime_ns"], 2**64 - 1)
            or not _bounded_int(rec["ts"], 32_503_680_000_000)):
        return False
    if ("source_sig" in rec
            and (not rec["sqlite"] or not isinstance(rec["source_sig"], str)
                 or _SHA_RE.fullmatch(rec["source_sig"]) is None)):
        return False
    sessions = rec["sessions"]
    if (not isinstance(sessions, list) or len(sessions) > 4096
            or any(not isinstance(session, str) or len(session) > 256
                   or re.fullmatch(r"[A-Za-z0-9._-]*", session) is None
                   for session in sessions)):
        return False
    chunks = rec["chunks"]
    if not isinstance(chunks, list) or not chunks or len(chunks) > _MAX_CHUNKS:
        return False
    total = 0
    for chunk in chunks:
        if (not isinstance(chunk, dict) or set(chunk) != {"sha256", "len", "codec"}
                or not isinstance(chunk["sha256"], str)
                or _SHA_RE.fullmatch(chunk["sha256"]) is None
                or not _bounded_int(chunk["len"], _MAX_ARCHIVE_BYTES)
                or chunk["codec"] != _CODEC):
            return False
        total += chunk["len"]
        if total > _MAX_ARCHIVE_BYTES:
            return False
    return total == rec["size"]


def _valid_opencode_db(path: Path) -> bool:
    name = path.name.lower()
    return (name.startswith("opencode") and name.endswith(".db")
            and ".bak" not in name and ".corrupted" not in name)


def _opencode_db_candidates(failures: list[dict]) -> list[Path]:
    directories = [Path(value) for value in opencode_data_dirs(str(HOME))]
    paths: list[Path] = []
    override = os.environ.get("OPENCODE_DB")
    if override:
        raw = Path(os.path.expanduser(override))
        if raw.is_absolute():
            paths.append(raw)
        elif len(raw.parts) == 1 and directories:
            paths.append(directories[0] / raw)
    for directory in directories:
        try:
            entries = os.scandir(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            _note_discovery_failure(failures, directory, exc, "scandir")
            continue
        try:
            with entries:
                for entry in entries:
                    path = Path(entry.path)
                    if not _valid_opencode_db(path):
                        continue
                    try:
                        regular = entry.is_file(follow_symlinks=False)
                    except OSError as exc:
                        _note_discovery_failure(failures, path, exc, "lstat")
                        continue
                    if regular:
                        paths.append(path)
        except OSError as exc:
            _note_discovery_failure(failures, directory, exc, "scandir")
    return sorted({os.path.normcase(os.path.abspath(path)): path for path in paths}.values())


def _opencode_storage(relative: Path) -> bool:
    return (len(relative.parts) >= 2 and relative.parts[0] == "storage"
            and relative.suffix.lower() == ".json" and ".." not in relative.parts)


def _antigravity_source(relative: Path) -> bool:
    return (len(relative.parts) >= 5
            and relative.parts[:3] == (".gemini", "antigravity-cli", "brain")
            and relative.suffix.lower() in (".json", ".jsonl")
            and ".." not in relative.parts)


def _kimi_source(relative: Path) -> bool:
    return (len(relative.parts) >= 5 and relative.parts[:2] == (".kimi", "sessions")
            and relative.suffix.lower() == ".jsonl" and ".." not in relative.parts)


def _claude_throwaway(name: str) -> bool:
    return "Temp-claude" in name or "claude-worker" in name


def _claude_source(relative: Path) -> bool:
    return (4 <= len(relative.parts) <= 9
            and relative.parts[:2] == (".claude", "projects")
            and relative.suffix.lower() == ".jsonl"
            and ".." not in relative.parts
            and not any(_claude_throwaway(part) for part in relative.parts[2:]))


def _note_discovery_failure(failures: list[dict] | None, path: Path,
                            exc: OSError | str, phase: str) -> None:
    if failures is None:
        return
    error = str(exc) or type(exc).__name__
    item = {"path": str(path), "error": error, "phase": phase}
    if item not in failures:
        failures.append(item)


def _path_kind(path: Path, failures: list[dict], *, missing_is_error: bool) -> str | None:
    try:
        info = path.lstat()
        if _linklike(path, info):
            return None
    except (FileNotFoundError, NotADirectoryError) as exc:
        if missing_is_error:
            _note_discovery_failure(failures, path, exc, "lstat")
        return None
    except OSError as exc:
        _note_discovery_failure(failures, path, exc, "lstat")
        return None
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "file" if stat.S_ISREG(info.st_mode) else None


def _scan_entries(directory: Path, failures: list[dict], *, missing_is_error: bool,
                  phase: str) -> list[os.DirEntry]:
    entries = []
    try:
        iterator = os.scandir(directory)
    except (FileNotFoundError, NotADirectoryError) as exc:
        if missing_is_error:
            _note_discovery_failure(failures, directory, exc, phase)
        return entries
    except OSError as exc:
        _note_discovery_failure(failures, directory, exc, phase)
        return entries
    try:
        with iterator as opened:
            entries.extend(opened)
    except OSError as exc:
        _note_discovery_failure(failures, directory, exc, phase)
    return sorted(entries, key=lambda entry: entry.name)


def _entry_kind(entry: os.DirEntry, failures: list[dict]) -> str | None:
    path = Path(entry.path)
    try:
        info = entry.stat(follow_symlinks=False)
        if _linklike(path, info):
            return None
    except OSError as exc:
        _note_discovery_failure(failures, path, exc, "lstat")
        return None
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "file" if stat.S_ISREG(info.st_mode) else None


def _glob_paths(root: Path, pattern: str, failures: list[dict]) -> list[Path]:
    parts = Path(pattern).parts
    paths: set[Path] = set()

    def accept(entry: os.DirEntry, kind: str | None, index: int) -> None:
        if not fnmatch.fnmatch(entry.name, parts[index]):
            return
        path = Path(entry.path)
        if index == len(parts) - 1:
            if kind == "file":
                paths.add(path)
        elif kind == "directory":
            visit(path, index + 1, True)

    def visit(directory: Path, index: int, discovered: bool) -> None:
        part = parts[index]
        last = index == len(parts) - 1
        if part == "**":
            # zero or more directories: one scan serves the descent and the
            # continuation at this level alike
            for entry in _scan_entries(
                    directory, failures, missing_is_error=discovered, phase="walk"):
                kind = _entry_kind(entry, failures)
                if kind == "directory":
                    visit(Path(entry.path), index, True)
                if not last:
                    accept(entry, kind, index + 1)
            return
        if not any(char in part for char in "*?["):
            # An absent literal under a wildcard match is an ordinary
            # non-match: `.gemini/tmp/*/chats` visits every sibling of tmp and
            # most have no chats/. Only a lost enumerated entry is a race.
            path = directory / part
            kind = _path_kind(path, failures, missing_is_error=False)
            if last and kind == "file":
                paths.add(path)
            elif not last and kind == "directory":
                visit(path, index + 1, discovered)
            return
        for entry in _scan_entries(
                directory, failures, missing_is_error=discovered, phase="glob"):
            if fnmatch.fnmatch(entry.name, part):
                accept(entry, _entry_kind(entry, failures), index)

    if parts:
        visit(root, 0, False)
    return sorted(paths)


def _claude_paths(failures: list[dict] | None = None) -> list[Path]:
    root = HOME / ".claude" / "projects"
    files: list[Path] = []

    def gather(directory: Path, depth: int) -> None:
        if depth > 5:
            return
        try:
            info = directory.lstat()
        except OSError as exc:
            _note_discovery_failure(failures, directory, exc, "lstat")
            return
        if _linklike(directory, info) or not stat.S_ISDIR(info.st_mode):
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            _note_discovery_failure(failures, directory, exc, "walk")
            return
        for path in entries:
            if _claude_throwaway(path.name):
                continue
            try:
                child = path.lstat()
            except OSError as exc:
                _note_discovery_failure(failures, path, exc, "lstat")
                continue
            if _linklike(path, child):
                continue
            if stat.S_ISDIR(child.st_mode):
                gather(path, depth + 1)
            elif stat.S_ISREG(child.st_mode) and path.suffix.lower() == ".jsonl":
                files.append(path)

    current = HOME
    for part in (None, ".claude", "projects"):
        current = current if part is None else current / part
        try:
            info = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return files
        except OSError as exc:
            _note_discovery_failure(failures, current, exc, "lstat")
            return files
        if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
            return files
    try:
        projects = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        _note_discovery_failure(failures, root, exc, "walk")
        return files
    for project in projects:
        if not _claude_throwaway(project.name):
            gather(project, 0)
    return files


def _cursor_db_locations(platform_name: str | None = None) -> list[tuple[Path, Path]]:
    platform_name = os.name if platform_name is None else platform_name
    home = HOME.absolute()
    appdata = os.environ.get("APPDATA") if platform_name == "nt" else None
    if appdata:
        windows_root = Path(os.path.abspath(os.path.expanduser(appdata)))
        windows = (windows_root, Path("Cursor/User/globalStorage/state.vscdb"))
    else:
        windows = (home, Path("AppData/Roaming/Cursor/User/globalStorage/state.vscdb"))
    macos = (home, Path("Library/Application Support/Cursor/User/globalStorage/state.vscdb"))
    linux = (home, Path(".config/Cursor/User/globalStorage/state.vscdb"))
    ordered = ((windows, macos, linux) if platform_name == "nt"
               else (macos, windows, linux) if platform_name == "posix" and sys.platform == "darwin"
               else (linux, windows, macos))
    out = []
    seen = set()
    for root, relative in ordered:
        key = os.path.normcase(os.path.abspath(root / relative))
        if key not in seen:
            seen.add(key)
            out.append((root, relative))
    return out


def _cursor_location(candidate: Path) -> tuple[Path, Path] | None:
    key = os.path.normcase(os.path.abspath(candidate))
    for root, relative in _cursor_db_locations():
        if key == os.path.normcase(os.path.abspath(root / relative)):
            return root, relative
    return None


def _cline_data_roots() -> list[Path]:
    products = ("Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium")
    if os.name == "nt":
        bases = [Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")]
    else:
        bases = [HOME / "Library" / "Application Support",
                 Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")]
    roots = [base / product / "User" / "globalStorage" /
             "saoudrizwan.claude-dev" for base in bases for product in products]
    cline = Path(os.environ.get("CLINE_DIR") or HOME / ".cline")
    roots.append(cline / "data")
    out = []
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            out.append(root.absolute())
    return out


def _cline_location(candidate: Path) -> tuple[Path, Path] | None:
    for root in _cline_data_roots():
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        task_json = (len(relative.parts) == 3 and relative.parts[0] == "tasks"
                     and relative.suffix.lower() == ".json")
        if task_json or relative == Path("state/taskHistory.json"):
            return root, relative
    return None


def _crush_data_dirs() -> list[Path]:
    legacy = HOME / ".local" / "share" / "crush"
    override = os.environ.get("CRUSH_GLOBAL_DATA")
    xdg = os.environ.get("XDG_DATA_HOME")
    if override:
        primary = Path(os.path.abspath(os.path.expanduser(override)))
    elif xdg:
        primary = Path(os.path.abspath(os.path.expanduser(xdg))) / "crush"
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(HOME / "AppData" / "Local")
        primary = Path(os.path.abspath(os.path.expanduser(local))) / "crush"
    else:
        primary = legacy
    out = []
    seen = set()
    for path in (primary, legacy):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _linklike(path: Path, info: os.stat_result | None = None) -> bool:
    info = info or path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse):
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction and junction())


def _read_regular_file(path: Path, maximum: int, root: Path | None = None) -> bytes:
    if maximum < 0:
        raise ValueError("invalid file bound")
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("bounded read escapes its root") from exc
        current = root
        for part in relative.parts[:-1]:
            info = current.lstat()
            if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("unsafe archive store path")
            current /= part
        info = current.lstat()
        if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe archive store path")
    leaf = path.lstat()
    if _linklike(path, leaf) or not stat.S_ISREG(leaf.st_mode):
        raise ValueError("bounded file is not a plain regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > maximum
                or (leaf.st_dev, leaf.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("bounded file is not a plausible regular file")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(1024**2, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        before_id = (before.st_dev, before.st_ino, before.st_size,
                     getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9)))
        after_id = (after.st_dev, after.st_ino, after.st_size,
                    getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9)))
        if len(data) > maximum or before_id != after_id or len(data) != before.st_size:
            raise ValueError("bounded file changed while being read")
        return data
    finally:
        os.close(fd)


def _crush_project_records(registry: Path) -> list[dict]:
    try:
        payload = json.loads(_read_regular_file(
            registry, _MAX_REGISTRY_BYTES, registry.parent).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    records: object = payload.get("projects", payload) if isinstance(payload, dict) else payload
    if isinstance(records, dict):
        records = list(records.values())
    if not isinstance(records, list) or len(records) > 100_000:
        return []
    return [record for record in records if isinstance(record, dict)]


def _crush_db_paths(*, include_missing: bool = False) -> list[Path]:
    candidates = [directory / "crush.db" for directory in _crush_data_dirs()]
    candidates.append(HOME / ".crush" / "crush.db")
    for directory in _crush_data_dirs():
        for record in _crush_project_records(directory / "projects.json"):
            value = record.get("data_dir")
            if not isinstance(value, str) or not value or "\0" in value or len(value) > 32_768:
                continue
            data_dir = Path(os.path.expanduser(value))
            if not data_dir.is_absolute():
                project = record.get("path")
                if not isinstance(project, str) or not project or "\0" in project:
                    continue
                data_dir = Path(os.path.expanduser(project)) / data_dir
            candidates.append(Path(os.path.abspath(data_dir)) / "crush.db")
    out = []
    seen = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if include_missing or _plain_path_at(path.parent, Path(path.name)):
            out.append(path)
    return out


def _source_location(value: object) -> tuple[Path, Path] | None:
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    home = HOME.absolute()
    try:
        relative = candidate.relative_to(home)
    except ValueError:
        relative = None
    if (relative is not None and ".." not in relative.parts
            and (any(_match_root_pattern(relative, pattern)
                     for agent, pattern, _ in ROOTS if agent != "cursor")
                 or _antigravity_source(relative) or _kimi_source(relative)
                 or _claude_source(relative))
            and ("opencode" not in relative.parts or candidate.suffix.lower() != ".db"
                 or _valid_opencode_db(candidate))):
        return home, relative
    cursor = _cursor_location(candidate)
    if cursor is not None:
        return cursor
    cline = _cline_location(candidate)
    if cline is not None:
        return cline
    key = os.path.normcase(os.path.abspath(candidate))
    explicit = {os.path.normcase(os.path.abspath(path))
                for path in opencode_db_paths(str(HOME), include_default=True)}
    if key in explicit:
        return candidate.parent.absolute(), Path(candidate.name)
    for directory in opencode_data_dirs(str(HOME)):
        base = Path(directory).absolute()
        try:
            relative = candidate.relative_to(base)
        except ValueError:
            continue
        if ((_valid_opencode_db(candidate) and len(relative.parts) == 1)
                or _opencode_storage(relative)):
            return base, relative
    crush = {os.path.normcase(os.path.abspath(path))
             for path in _crush_db_paths(include_missing=True)}
    if key in crush:
        return candidate.parent.absolute(), Path(candidate.name)
    for directory in _crush_data_dirs():
        base = directory.absolute()
        try:
            relative = candidate.relative_to(base)
        except ValueError:
            continue
        if relative in (Path("crush.db"), Path("projects.json")):
            return base, relative
    return None


def _restore_location(record: dict) -> tuple[Path, Path] | None:
    current = _source_location(record.get("path"))
    if current is not None:
        return current
    return _portable_restore_location(
        record.get("path"), record.get("agent"), record.get("sqlite"))


def _manifest() -> dict[str, dict]:
    """Newest record per path."""
    return {rec["path"]: rec for rec in _records()}


def _append_manifest(rec: dict) -> None:
    _ensure_plain_parent(ARCHIVE_DIR, ARCHIVE_DIR)
    body = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    if len(body) > _MAX_MANIFEST_LINE:
        raise ValueError("archive manifest record is too large")
    try:
        leaf = MANIFEST.lstat()
    except FileNotFoundError:
        leaf = None
    if leaf is not None and (_linklike(MANIFEST, leaf) or not stat.S_ISREG(leaf.st_mode)):
        raise ValueError("archive manifest target is unsafe")
    flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(MANIFEST, flags, 0o600)
    try:
        opened = os.fstat(fd)
        current = MANIFEST.lstat()
        same_leaf = (not _linklike(MANIFEST, current)
                     and stat.S_ISREG(current.st_mode)
                     and (current.st_dev, current.st_ino)
                     == (opened.st_dev, opened.st_ino))
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not same_leaf):
            raise ValueError("archive manifest target is unsafe or full")
        repair = b""
        current_size = opened.st_size
        if current_size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                window = min(current_size, _MAX_MANIFEST_LINE + 1)
                os.lseek(fd, current_size - window, os.SEEK_SET)
                chunks = []
                remaining = window
                while remaining:
                    chunk = os.read(fd, remaining)
                    if not chunk:
                        raise ValueError("archive manifest changed during repair")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                tail = b"".join(chunks)
                fragment = tail[tail.rfind(b"\n") + 1:]
                if len(fragment) > _MAX_MANIFEST_LINE:
                    raise ValueError("archive manifest exceeds its record bound")
                try:
                    json.loads(fragment.decode("utf-8"))
                except (TypeError, UnicodeError, ValueError):
                    current_size -= len(fragment)
                    os.ftruncate(fd, current_size)
                else:
                    repair = b"\n"
        if current_size + len(repair) + len(body) > _MAX_MANIFEST_BYTES:
            raise ValueError("archive manifest target is unsafe or full")
        view = memoryview(repair + body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("archive manifest append made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sqlite_snapshot(path: Path) -> Path:
    before = path.lstat()
    if (_linklike(path, before) or not stat.S_ISREG(before.st_mode)
            or before.st_size > _MAX_ARCHIVE_BYTES):
        raise ValueError("sqlite source is not a plausible regular file")
    # the backup writes every page the db plus its WAL hold: the largest copy the
    # archive ever makes, so the volume is checked before a page is written
    needed = before.st_size
    try:
        needed += path.with_name(path.name + "-wal").lstat().st_size
    except OSError:
        pass
    _require_copy_space(needed)
    _ensure_plain_parent(ARCHIVE_DIR, ARCHIVE_DIR)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".db", dir=ARCHIVE_DIR)
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        src = dst = None
        try:
            src = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            src.backup(dst, pages=4096,
                       progress=lambda _status, remaining, _total: _progress(-remaining))
            dst.commit()
        finally:
            try:
                if dst is not None:
                    dst.close()
            finally:
                if src is not None:
                    src.close()
        after = path.lstat()
        if (_linklike(path, after)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise ValueError("sqlite source changed identity during backup")
        if tmp.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("sqlite snapshot exceeds the configured bound")
        return tmp
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _read_source(path: Path, is_sqlite: bool) -> bytes:
    if not is_sqlite:
        location = _source_location(str(path))
        if location is None:
            raise ValueError("source is outside documented store roots")
        return _read_regular_file(path, _MAX_ARCHIVE_BYTES, location[0])
    tmp = _sqlite_snapshot(path)
    try:
        return _read_regular_file(tmp, _MAX_ARCHIVE_BYTES, ARCHIVE_DIR)
    finally:
        tmp.unlink(missing_ok=True)


def _file_usn_change_token(usn: int) -> tuple[str, int] | None:
    if usn == 0:
        return None
    return ("usn", usn)


def _metadata_change_token(
        path: Path, info: os.stat_result) -> tuple[str, int] | None:
    if os.name != "nt":
        return ("ctime", int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9))))
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = (("creation", ctypes.c_longlong),
                    ("access", ctypes.c_longlong),
                    ("write", ctypes.c_longlong),
                    ("change", ctypes.c_longlong),
                    ("attributes", wintypes.DWORD))

    class ReadFileUsnData(ctypes.Structure):
        _fields_ = (("minimum", wintypes.WORD), ("maximum", wintypes.WORD))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID)
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path), 0x0080, 0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = FileBasicInfo()
        if not kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.attributes & 0x00000400:
            raise ValueError("sqlite source or WAL became a reparse point")
        request = ReadFileUsnData(2, 3)
        output = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
                handle, 0x000900EB, ctypes.byref(request), ctypes.sizeof(request),
                output, ctypes.sizeof(output), ctypes.byref(returned), None):
            return None
        raw = bytes(output[:returned.value])
        if len(raw) < 8:
            raise ValueError("invalid file USN response")
        record_length = int.from_bytes(raw[0:4], "little")
        major = int.from_bytes(raw[4:6], "little")
        usn_offset = 24 if major == 2 else 40 if major == 3 else -1
        if (usn_offset < 0 or record_length > len(raw)
                or record_length < usn_offset + 8):
            raise ValueError("invalid file USN response")
        usn = int.from_bytes(
            raw[usn_offset:usn_offset + 8], "little", signed=True)
        return _file_usn_change_token(usn)
    finally:
        kernel32.CloseHandle(handle)


def _sqlite_source_state(path: Path) -> tuple[str, float, os.stat_result]:
    identities: list[object] = []
    activity = 0.0
    main = None
    for index, candidate in enumerate((path, path.with_name(path.name + "-wal"))):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if index == 0:
                raise
            identities.append(None)
            continue
        if _linklike(candidate, info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("sqlite source or WAL is not a plain regular file")
        if info.st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("sqlite source or WAL exceeds the configured bound")
        if index and not info.st_size:
            # An empty WAL carries no frames. Merely opening a WAL store creates
            # one - including our own read-only backup connection - so it is
            # neither source content nor evidence that anyone wrote.
            identities.append(None)
            continue
        if index == 0:
            main = info
        activity = max(activity, info.st_mtime)
        freshness: tuple[str, int | str] | None = _metadata_change_token(candidate, info)
        if freshness is None:
            digest, _, _, _, _, opened = _hash_regular_file(candidate, candidate.parent)
            after = candidate.lstat()
            if (_linklike(candidate, after)
                    or _regular_identity(opened) != _regular_identity(info)
                    or _regular_identity(after) != _regular_identity(info)):
                raise ValueError("sqlite source or WAL changed while hashing")
            freshness = ("sha256", digest)
        identities.append((info.st_dev, info.st_ino, info.st_size,
                           getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)),
                           freshness))
    if main is None:
        raise FileNotFoundError(path)
    encoded = json.dumps(identities, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest(), activity, main


def _sessions_of(path: Path) -> list[str]:
    return sorted({m.group(0).lower() for m in _SESSION_RE.finditer(str(path))})


def _plain_path_at(root: Path, relative: Path, *, failures: list[dict] | None = None,
                   missing_is_error: bool = False) -> bool:
    current = root
    try:
        root_info = current.lstat()
        if _linklike(current, root_info) or not stat.S_ISDIR(root_info.st_mode):
            return False
        info = root_info
        for part in relative.parts:
            current /= part
            info = current.lstat()
            if _linklike(current, info):
                return False
        return stat.S_ISREG(info.st_mode)
    except OSError as exc:
        if missing_is_error or not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            _note_discovery_failure(failures, current, exc, "lstat")
        return False
    except UnboundLocalError:
        return False


def _plain_source_path(path: Path, failures: list[dict] | None = None, *,
                       missing_is_error: bool = False) -> bool:
    location = _source_location(str(path))
    if location is None:
        # discovery and classification disagree: the file was found under a store
        # root, so dropping it here would be a silent capture gap
        _note_discovery_failure(
            failures, path, "found under a store root but not recognized as a "
            "store file; not captured", "classify")
        return False
    return _plain_path_at(
        *location, failures=failures, missing_is_error=missing_is_error)


def _discovered_sources(failures: list[dict] | None = None):
    failures = [] if failures is None else failures
    seen = set()
    for agent, pattern, is_sqlite in ROOTS:
        if agent == "cursor" or (agent == "opencode" and is_sqlite):
            continue
        paths = (_claude_paths(failures) if agent == "claude"
                 else _glob_paths(HOME, pattern, failures))
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen and _plain_source_path(
                    path, failures, missing_is_error=True):
                seen.add(key)
                yield agent, path, is_sqlite
    for root, relative in _cursor_db_locations():
        path = root / relative
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and _plain_source_path(path, failures):
            seen.add(key)
            yield "cursor", path, True
    for path in _opencode_db_candidates(failures):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and _plain_source_path(
                path, failures, missing_is_error=True):
            seen.add(key)
            yield "opencode", path, True
    for root in _cline_data_roots():
        discovered = _glob_paths(root, "tasks/*/*.json", failures)
        paths = [(path, True) for path in discovered]
        paths.append((root / "state" / "taskHistory.json", False))
        for path, missing_is_error in sorted(paths, key=lambda item: item[0]):
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen and _plain_source_path(
                    path, failures, missing_is_error=missing_is_error):
                seen.add(key)
                yield "cline", path, False
    for directory in opencode_data_dirs(str(HOME)):
        storage = Path(directory) / "storage"
        paths = _glob_paths(storage, "**/*.json", failures)
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen and _plain_source_path(
                    path, failures, missing_is_error=True):
                seen.add(key)
                yield "opencode", path, False
    for directory in _crush_data_dirs():
        registry = directory / "projects.json"
        key = os.path.normcase(os.path.abspath(registry))
        if key not in seen and _plain_source_path(registry, failures):
            seen.add(key)
            yield "crush", registry, False
    for path in _crush_db_paths(include_missing=True):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and _plain_source_path(path, failures):
            seen.add(key)
            yield "crush", path, True


_DEFERRAL_KINDS = frozenset(("settle", "rewrite", "busy", "disk"))
_HEALTH_MAX_BYTES = _MAX_CONFIG_BYTES * 16  # one bound for the writer's trim and the reader
_DEFERRAL_WORDING = {
    "settle": "in use; captured once quiet for 15 min",
    "rewrite": "rewritten in place; next capture within 10 min",
    "busy": "written while being copied; retried next pass",
    "disk": "too little free disk for a point-in-time copy",
}


def _tick_interval_s() -> float:
    try:
        return max(1.0, float(os.environ.get("AGREP_ARCHIVE_CHECK_S", "60")))
    except (TypeError, ValueError):
        return 60.0


def _running_line(lock: dict) -> str:
    """'capture running (pid N, since 12s)' for a verified live lock holder, or the
    stalled form when it has made no progress for longer than the bound."""
    pid = lock.get("pid") or "unknown"
    since = surface.brief_duration(float(lock.get("age_s") or 0.0))
    if lock.get("stalled"):
        idle = surface.brief_duration(float(lock.get("idle_s") or 0.0))
        if lock.get("daemon_live"):
            action = "the daemon that started it stops it on its next tick"
        elif os.name == "nt":
            action = f"stop it with `taskkill /PID {pid} /F`"
        else:
            action = f"stop it with `kill {pid}`"
        return (f"capture stalled (pid {pid}, running {since}, no progress for {idle}); "
                f"{action}")
    return f"capture running (pid {pid}, since {since})"


def _valid_deferrals(value: object, now_ms: int) -> dict[str, dict]:
    """{path: {since_ms, kind}} from a health record; anything malformed is dropped."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for path, item in value.items():
        if (not isinstance(path, str) or not isinstance(item, dict)
                or item.get("kind") not in _DEFERRAL_KINDS
                or not _bounded_int(item.get("since_ms"), now_ms + 60_000)):
            continue
        out[path] = {"since_ms": item["since_ms"], "kind": item["kind"]}
    return out


def _write_capture_health(
        outcome: str, *, detail: str = "", failed: int = 0,
        completed: bool = False, deferrals: dict[str, dict] | None = None) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    previous = _capture_health()
    now_ms = int(time.time() * 1000)
    payload = {
        "version": 1,
        "last_pass_ms": now_ms if completed else previous.get("last_pass_ms"),
        "last_attempt_ms": now_ms,
        "outcome": outcome,
        "detail": str(detail)[:2048],
        "failed": max(0, int(failed)),
        "deferrals": {},
    }
    kept = dict(sorted(((previous.get("deferrals") or {}) if deferrals is None
                        else deferrals).items(),
                       key=lambda item: (item[1]["since_ms"], item[0])))
    # the oldest waits are the ones status must keep naming: trim the newest
    # until the record fits the reader's bound
    body = json.dumps({**payload, "deferrals": kept}, separators=(",", ":")).encode("utf-8")
    while kept and len(body) > _HEALTH_MAX_BYTES:
        kept.pop(next(reversed(kept)))
        body = json.dumps({**payload, "deferrals": kept},
                          separators=(",", ":")).encode("utf-8")
    _publish_restore(HEALTH, ARCHIVE_DIR, body, True)


def record_capture_failure(error: BaseException) -> None:
    _write_capture_health(
        "capture-blocked", detail=f"{type(error).__name__}: {error}")


def _capture_health() -> dict:
    try:
        value = json.loads(_read_regular_file(
            HEALTH, _HEALTH_MAX_BYTES, ARCHIVE_DIR).decode("utf-8"))
    except FileNotFoundError:
        return {"outcome": "never", "last_pass_ms": None, "age_s": None,
                "last_attempt_ms": None, "attempt_age_s": None, "deferrals": {}}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"outcome": "unknown", "last_pass_ms": None, "age_s": None,
                "last_attempt_ms": None, "attempt_age_s": None,
                "detail": str(exc), "deferrals": {}}
    now_ms = int(time.time() * 1000)
    last_pass_ms = value.get("last_pass_ms") if isinstance(value, dict) else None
    last_attempt_ms = (value.get("last_attempt_ms", last_pass_ms)
                       if isinstance(value, dict) else None)
    if (not isinstance(value, dict) or value.get("version") != 1
            or (last_pass_ms is not None
                and (type(last_pass_ms) is not int or last_pass_ms < 0
                     or last_pass_ms > now_ms + 60_000))
            or type(last_attempt_ms) is not int
            or last_attempt_ms < 0
            or last_attempt_ms > now_ms + 60_000
            or type(value.get("failed", 0)) is not int
            or value.get("failed", 0) < 0
            or value.get("outcome") not in {
                "healthy", "partial", "busy", "capture-blocked"}):
        return {"outcome": "unknown", "last_pass_ms": None, "age_s": None,
                "last_attempt_ms": None, "attempt_age_s": None,
                "detail": "capture health record is invalid", "deferrals": {}}
    return {
        "outcome": value["outcome"],
        "last_pass_ms": last_pass_ms,
        "age_s": (max(0.0, time.time() - last_pass_ms / 1000)
                  if last_pass_ms is not None else None),
        "last_attempt_ms": last_attempt_ms,
        "attempt_age_s": max(0.0, time.time() - last_attempt_ms / 1000),
        "detail": str(value.get("detail") or ""),
        "failed": value.get("failed", 0),
        "deferrals": _valid_deferrals(value.get("deferrals"), now_ms),
    }


def waiting_summary(health: dict) -> dict | None:
    """What the persisted deferrals say is still waiting for capture, or None. Every
    kind is a window working as designed, never a verdict."""
    deferrals = health.get("deferrals") or {}
    if not deferrals:
        return None
    now_ms = int(time.time() * 1000)
    by_kind: dict[str, int] = {}
    for item in deferrals.values():
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    oldest = min(deferrals, key=lambda path: deferrals[path]["since_ms"])
    return {"count": len(deferrals), "kinds": sorted(by_kind), "by_kind": by_kind,
            "oldest_age_s": max(0.0, (now_ms - deferrals[oldest]["since_ms"]) / 1000),
            "oldest_path": oldest}


def disk_line(health: dict) -> str | None:
    """One line for a low archive volume, however many sources wait on it."""
    deferrals = health.get("deferrals") or {}
    short = [path for path, item in deferrals.items() if item["kind"] == "disk"]
    if not short:
        return None
    return (f"{len(short)} source(s) not captured: too little free space on the "
            f"volume holding {ARCHIVE_DIR} for a point-in-time copy; free space there "
            "(or move the data dir) and the next pass captures them")


def waiting_line(health: dict) -> str | None:
    waiting = waiting_summary(health)
    if waiting is None:
        return None
    by_kind = {kind: count for kind, count in waiting["by_kind"].items() if kind != "disk"}
    if not by_kind:
        return None
    parts = [f"{count} {_DEFERRAL_WORDING[kind]}" for kind, count in sorted(by_kind.items())]
    line = (f"{sum(by_kind.values())} source(s) not captured yet: {'; '.join(parts)}; "
            f"oldest {surface.brief_duration(waiting['oldest_age_s'])}")
    if set(by_kind) - {"settle"}:
        line += f"; `{common.cli_name()} archive` captures them now"
    return line


PROGRESS_NAME = "progress"
STALL_NAME = "stalled"
_BEAT_EVERY_S = 5.0  # well under STALL_BOUND_S; an idle pass over 10k sources beats once
_last_beat = 0.0
_beat_sequence = 0
_beat_nonce = secrets.token_hex(4)  # distinguishes this pass's beats from a predecessor's


def _progress(step: int, *, force: bool = False) -> None:
    """The pass's heartbeat: a tiny file the daemon reads to tell a slow child
    from a stuck one. The beat is a per-pass sequence, not a clock, so a sleep,
    a wake or a clock jump cannot make a healthy pass look idle. Rate-limited and
    unsynced - the atomic replace keeps a reader from a torn line. Best effort:
    a failed beat never fails the pass."""
    global _last_beat, _beat_sequence
    now = time.monotonic()
    if not force and now - _last_beat < _BEAT_EVERY_S:
        return
    _last_beat = now
    _beat_sequence += 1
    try:
        fd, name = tempfile.mkstemp(prefix=".progress-", dir=ARCHIVE_DIR)
    except OSError:
        return
    try:
        try:
            os.write(fd, f"{_beat_nonce}:{_beat_sequence} {step} "
                     f"{int(time.time() * 1000)}\n".encode("ascii"))
        finally:
            os.close(fd)
        os.replace(name, ARCHIVE_DIR / PROGRESS_NAME)
    except OSError:
        Path(name).unlink(missing_ok=True)


def progress_beat() -> tuple[str, float] | None:
    """(sequence token, wall age in seconds) of the running pass's last beat."""
    try:
        raw = _read_regular_file(ARCHIVE_DIR / PROGRESS_NAME, 128, ARCHIVE_DIR)
        token, _step, stamp = raw.decode("ascii").split()
        return token, max(0.0, time.time() - int(stamp) / 1000)
    except (OSError, UnicodeError, ValueError):
        return None


class StallWatch:
    """The daemon's judgement of its own child. The child is stalled only once the
    beat token has stayed the same across at least `observations_required()` ticks
    AND for STALL_BOUND_S of monotonic time. Wall time never enters; and because a
    suspend counts on the monotonic clock too on Windows, a sleep of any length is
    one observation and can never satisfy the count on its own, while a stuck child
    still trips after roughly STALL_BOUND_S of ticks."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.since = time.monotonic()
        self.observations = 0

    @staticmethod
    def observations_required() -> int:
        """Consecutive unchanged ticks that span the stall bound; never fewer than two."""
        return max(2, math.ceil(STALL_BOUND_S / _tick_interval_s()))

    def observe(self) -> tuple[float, bool]:
        """(monotonic seconds the token has been unchanged, stalled?)"""
        beat = progress_beat()
        token = beat[0] if beat else None
        now = time.monotonic()
        if token != self.token:
            self.token, self.since, self.observations = token, now, 0
        self.observations += 1
        idle = now - self.since
        return idle, (idle > STALL_BOUND_S
                      and self.observations >= self.observations_required())


def record_stall(pid: int, idle_s: float) -> None:
    """The daemon's verdict on the child it is about to stop, for status to show."""
    try:
        _publish_restore(ARCHIVE_DIR / STALL_NAME, ARCHIVE_DIR,
                         f"{pid} {int(idle_s)}\n".encode("ascii"), True)
    except (OSError, ValueError):
        pass


def _recorded_stall(pid: int) -> float | None:
    """Idle seconds the spawning daemon recorded for holder `pid`, if it judged it."""
    try:
        raw = _read_regular_file(ARCHIVE_DIR / STALL_NAME, 64, ARCHIVE_DIR)
        recorded, idle = raw.decode("ascii").split()
        return float(idle) if int(recorded) == pid else None
    except (OSError, UnicodeError, ValueError):
        return None


def _empty_stats() -> dict:
    return {"files": 0, "unchanged": 0, "appended": 0, "full": 0, "deferred": 0,
            "bytes_stored": 0, "failed": 0, "failures": [], "deferrals": []}


def capture(verbose: bool = False, *, scheduled: bool = False) -> dict:
    """One capture pass over every discovered store file, then a prune of versions
    beyond `keep`. Idle sources are stat-only; changed sqlite stores use coherent
    snapshots; changed plain files are copied at one instant and compressed from
    the copy. A pass never queues behind another archive pass. A `scheduled`
    pass (the daemon's child) re-images a rewritten store at most once per
    window; an explicit pass captures everything now."""
    if _data_dir_readonly():
        raise _readonly_error()
    fd = _try_lock()
    if fd is None:
        lock = _lock_status()
        _write_capture_health("busy", detail=str(lock.get("detail") or ""))
        return {**_empty_stats(), "busy": True, "lock": lock}
    try:
        previous = _capture_health().get("deferrals") or {}
        stats = _capture_locked(verbose, scheduled)
        outcome = "partial" if stats["failed"] else "healthy"
        detail = (f"{stats['failed']} source path(s) failed"
                  if stats["failed"] else "capture pass completed")
        if stats["deferred"]:
            detail += f"; {stats['deferred']} source path(s) deferred to the next pass"
        now_ms = int(time.time() * 1000)
        deferrals = {
            item["path"]: {
                "since_ms": previous.get(item["path"], {}).get("since_ms", now_ms),
                "kind": item["kind"]}
            for item in stats["deferrals"]}
        _write_capture_health(
            outcome, detail=detail, failed=stats["failed"], completed=True,
            deferrals=deferrals)
        return stats
    except Exception as exc:
        record_capture_failure(exc)
        raise
    finally:
        _unlock(fd)


def _sweep_temps() -> int:
    """Remove point-in-time copies and sqlite snapshots no capture holds. Only the
    lock holder calls this, so every one found is an orphan of a crashed pass."""
    removed = 0
    try:
        entries = list(os.scandir(ARCHIVE_DIR))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_TEMP_PREFIXES):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


class _SourceBusy(ValueError):
    """The source changed under the point-in-time copy; the next pass retries."""


_COPY_SPACE_RESERVE = 64 * 1024**2  # a byte copy never takes the volume below this


class _NoSpace(ValueError):
    """A byte copy would take the archive's volume under its reserve."""

    def __init__(self, needed: int, shortfall: int) -> None:
        super().__init__(f"needs {_human_bytes(needed)} of free space beside the "
                         f"archive ({_human_bytes(shortfall)} short)")
        self.needed = needed
        self.shortfall = shortfall


def _require_copy_space(needed: int) -> None:
    """Called right before bytes are written to the archive's volume."""
    try:
        free = shutil.disk_usage(ARCHIVE_DIR if ARCHIVE_DIR.exists()
                                 else ARCHIVE_DIR.parent).free
    except OSError:
        return
    shortfall = needed + _COPY_SPACE_RESERVE - free
    if shortfall > 0:
        raise _NoSpace(needed, shortfall)


def _point_in_time_copy(path: Path, root: Path) -> tuple[Path, os.stat_result]:
    """A copy of `path` taken at one instant: cloned where the filesystem can (no
    bytes, no space check), else copied after the space check, and accepted only
    if the source identity (inode, size, mtime) is the same before and after.
    Compression then reads the copy at leisure."""
    _ensure_plain_parent(ARCHIVE_DIR, ARCHIVE_DIR)
    fd, name = tempfile.mkstemp(prefix=".copy-", suffix=".src", dir=ARCHIVE_DIR)
    os.close(fd)
    tmp = Path(name)
    try:
        source, before, _ = _open_regular_file(path, _MAX_ARCHIVE_BYTES, root)
        os.close(source)
        cloned = False
        if sys.platform == "darwin":
            cloned = subprocess.run(
                ["/bin/cp", "-c", "--", str(path), str(tmp)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False).returncode == 0
        if not cloned:
            _require_copy_space(before.st_size)
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024**2)
        after = path.lstat()
        if (_linklike(path, after)
                or _regular_identity(after) != _regular_identity(before)
                or tmp.stat().st_size != before.st_size):
            raise _SourceBusy("source was written while being copied")
        return tmp, before
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _capture_locked(verbose: bool, scheduled: bool = False) -> dict:
    _ensure_plain_parent(ARCHIVE_DIR, ARCHIVE_DIR)
    _sweep_temps()
    _progress(0, force=True)
    (ARCHIVE_DIR / STALL_NAME).unlink(missing_ok=True)  # a fresh pass has no verdict yet
    values, torn_tail, unsafe_record = _manifest_values()
    if (unsafe_record
            or any(not _valid_manifest_record(record) for record in values)):
        raise ValueError(
            "archive contains an unrecognized manifest record; refusing to capture")
    if torn_tail:
        _prune()
        values, _, _ = _manifest_values()
    records = values
    latest = {record["path"]: record for record in records}
    needs_prune = _retention_exceeded(records)
    stats = _empty_stats()
    now_ms = int(time.time() * 1000)
    manifest_changed = False
    discovery_failures: list[dict] = []

    def defer(path: Path, kind: str, reason: str) -> None:
        stats["deferred"] += 1
        stats["deferrals"].append({"path": str(path), "kind": kind, "reason": reason})

    def in_rewrite_window(old: dict | None) -> bool:
        return (scheduled and old is not None
                and now_ms - old["ts"] < _REWRITE_RECAPTURE_MS)

    for agent, p, is_sqlite in _discovered_sources(discovery_failures):
        stats["files"] += 1
        temp: Path | None = None
        _progress(stats["files"])
        try:
            key = str(p)
            old = latest.get(key)
            source_sig = None
            grown = rebase = False
            if is_sqlite:
                source_sig, activity, st = _sqlite_source_state(p)
                if old and old.get("source_sig") == source_sig:
                    stats["unchanged"] += 1
                    continue
                if time.time() - activity < _SQLITE_SETTLE_S:
                    defer(p, "settle", f"written in the last {_SQLITE_SETTLE_S // 60} min")
                    continue
                temp = _sqlite_snapshot(p)
                after_sig, _, st = _sqlite_source_state(p)
                if source_sig != after_sig:
                    raise ValueError("sqlite source changed during backup")
            else:
                st = p.lstat()
                if (_linklike(p, st) or not stat.S_ISREG(st.st_mode)
                        or st.st_size > _MAX_ARCHIVE_BYTES):
                    raise ValueError("source is not a plausible regular file")
                if old and old["mtime_ns"] == st.st_mtime_ns and old["size"] == st.st_size:
                    stats["unchanged"] += 1
                    continue
                location = _source_location(key)
                if location is None:
                    raise ValueError("source is outside documented store roots")
                larger = bool(old and st.st_size > old["size"])
                # a long append chain is rebased onto a fresh whole-file capture
                # before its manifest record nears the line bound
                rebase = larger and len(old["chunks"]) >= _MAX_APPEND_CHAIN
                grown = larger and not rebase
                if old and not larger and in_rewrite_window(old):
                    defer(p, "rewrite", _REWRITE_REASON)
                    continue
                source, root = p, location[0]
                if grown:
                    # append-only growth needs no copy: the suffix is read from the
                    # live file under the torn-read guard, as the prefix check proves
                    sha, size, prefix_sha, suffix_sha, identity, _ = _hash_regular_file(
                        source, root, prefix=old["size"])
                    if prefix_sha != old["sha256"]:
                        # a rewrite that happens to be longer: whole-document rules
                        if in_rewrite_window(old):
                            defer(p, "rewrite", _REWRITE_REASON)
                            continue
                        grown = False
                if not grown:
                    try:
                        temp, st = _point_in_time_copy(p, root)
                    except _SourceBusy as exc:
                        defer(p, "busy", str(exc))
                        continue
                    source, root = temp, ARCHIVE_DIR
            if not grown:
                sha, size, prefix_sha, suffix_sha, identity, _ = _hash_regular_file(
                    temp, ARCHIVE_DIR)
            if old and not rebase and old["sha256"] == sha:
                rec = {**old, "mtime_ns": st.st_mtime_ns, "ts": int(time.time() * 1000)}
                if source_sig is not None:
                    rec["source_sig"] = source_sig
                _append_manifest(rec)
                latest[key] = rec
                manifest_changed = True
                stats["unchanged"] += 1
                continue
            if grown:
                suffix_len = size - old["size"]
                chunk = _store_file_chunk(source, root, old["size"], suffix_len,
                                          str(suffix_sha), identity)
                chunks = old["chunks"] + [chunk]
                stored = suffix_len
            else:
                chunks = [_store_file_chunk(temp, ARCHIVE_DIR, 0, size, sha, identity)]
                stored = size
            appended = grown
            rec = {"path": key, "agent": agent, "sessions": _sessions_of(p),
                   "sha256": sha, "size": size, "mtime_ns": st.st_mtime_ns,
                   "ts": int(time.time() * 1000), "sqlite": is_sqlite,
                   "chunks": chunks}
            if source_sig is not None:
                rec["source_sig"] = source_sig
            _append_manifest(rec)
            latest[key] = rec
            manifest_changed = True
            stats["appended" if appended else "full"] += 1
            stats["bytes_stored"] += stored
            if verbose:
                verb = "appended" if appended else "captured"
                common.log(f"archive: {verb} {common.terminal_safe(p.name)} ({size} bytes)")
        except _NoSpace as exc:
            defer(p, "disk", str(exc))
        except (OSError, sqlite3.Error, ValueError, lzma.LZMAError) as exc:
            stats["failed"] += 1
            stats["failures"].append({"path": str(p), "error": str(exc)})
            common.log(f"archive: failed {common.terminal_safe(p)}: "
                       f"{common.terminal_safe(exc)}")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    for failure in discovery_failures:
        stats["failed"] += 1
        stats["failures"].append(failure)
        common.log(f"archive: failed {common.terminal_safe(failure['path'])}: "
                   f"{common.terminal_safe(failure['error'])}")
    if manifest_changed or needs_prune:
        stats["pruned"] = _prune().get("reclaimed", 0)
    return stats


def capture_child_command() -> list[str]:
    """The daemon's capture pass: this module run as a program, scheduled mode."""
    return [sys.executable, str(Path(__file__).resolve()), "--scheduled-pass"]


def spawn_capture_child() -> subprocess.Popen | None:
    """Start one unbounded capture pass as a niced child in the caller's process
    group (so stopping the daemon's tree stops it), or None while a pass already
    holds the lock or retention is off. The caller never waits on it."""
    if not enabled() or _data_dir_readonly():
        return None
    if _lock_status().get("state") in ("busy", "lock-protected"):
        return None
    command = capture_child_command()
    if os.name != "nt":
        command = ["/usr/bin/nice", "-n", "19", *command]
    launch: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # the daemon's children must not flash a console; BELOW_NORMAL stands in for nice
        launch["creationflags"] = 0x08004000  # NO_WINDOW | BELOW_NORMAL
    env = {**os.environ,
           _SPAWNER_ENV: f"{os.getpid()}:{common.process_start_identity(os.getpid()) or 'unknown'}"}
    return subprocess.Popen(command, env=env, **launch)


def _retention_exceeded(records: list[dict]) -> bool:
    keep = _keep()
    if keep == 0:
        return False
    seen: dict[str, set[str]] = {}
    for record in reversed(records):
        shas = seen.setdefault(record["path"], set())
        if record["sha256"] in shas or (keep > 0 and len(shas) >= keep):
            return True
        shas.add(record["sha256"])
    return False


def _prune(verbose: bool = False) -> dict:
    """Drop versions beyond the newest `keep` distinct contents per path, rewrite
    the manifest, then sweep chunks no kept record references. Chunks are shared
    across versions (prefix chains, cross-path dedupe), so liveness is refcounted
    over the kept records, never inferred from record age. Caller holds the lock."""
    if _data_dir_readonly():
        raise _readonly_error()
    keep = _keep()
    values, torn_tail, unsafe_record = _manifest_values()
    if unsafe_record:
        raise ValueError("archive contains an unsafe manifest record; refusing to prune")
    if any(not _valid_manifest_record(rec) for rec in values):
        raise ValueError("archive contains an unrecognized manifest record; refusing to prune")
    recs = values
    selected = set(range(len(recs))) if keep == 0 else set()
    if keep > 0:
        shas_kept: dict[str, set[str]] = {}
        for index in range(len(recs) - 1, -1, -1):
            rec = recs[index]
            shas = shas_kept.setdefault(rec["path"], set())
            if rec["sha256"] in shas or len(shas) >= keep:
                continue
            shas.add(rec["sha256"])
            selected.add(index)
    kept = [rec for index, rec in enumerate(recs) if index in selected]
    dropped = len(recs) - len(kept)
    if dropped or torn_tail:
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
        _publish_restore(MANIFEST, ARCHIVE_DIR, body.encode("utf-8"), True)
    referenced = {c["sha256"] for r in kept for c in r["chunks"]}
    reclaimed = 0
    if STORE.exists():
        for f in STORE.rglob("*.xz"):
            if f.stem not in referenced:
                reclaimed += f.stat().st_size
                f.unlink(missing_ok=True)
    if verbose and (dropped or reclaimed):
        common.log(f"archive: pruned {dropped} old version record(s), "
                   f"reclaimed {reclaimed / 1e6:.1f} MB")
    return {"dropped": dropped, "reclaimed": reclaimed}


def _purge_locked() -> None:
    """Delete the manifest, every stored chunk and the capture-health record; the
    config (on/off, keep) and the lock survive. Caller holds the lock."""
    if _data_dir_readonly():
        raise _readonly_error()
    _sweep_temps()
    for path in (MANIFEST, HEALTH, ARCHIVE_DIR / PROGRESS_NAME, ARCHIVE_DIR / STALL_NAME):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _linklike(path, info) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"unsafe archive entry: {path}")
        path.unlink()
    try:
        info = STORE.lstat()
    except FileNotFoundError:
        return
    if _linklike(STORE, info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"unsafe archive store: {STORE}")
    for current, dirs, names in os.walk(STORE, topdown=False, followlinks=False):
        for name in names:
            path = Path(current) / name
            found = path.lstat()
            if _linklike(path, found) or not stat.S_ISREG(found.st_mode):
                raise ValueError(f"unsafe archive entry: {path}")
            path.unlink()
        for name in dirs:
            path = Path(current) / name
            if _linklike(path):
                raise ValueError(f"unsafe archive entry: {path}")
            path.rmdir()
    STORE.rmdir()


def _purge_main(assume_yes: bool) -> int:
    s = status()
    stored = s.get("stored_bytes")
    if stored is None:
        print(f"archive purge blocked: {common.terminal_safe(s.get('detail'))}")
        return 1
    # leftovers of a crashed pass count as archived bytes too: they are swept
    # whether or not a manifest ever existed. Config, health and the lock are not.
    bookkeeping = 0
    for entry in os.scandir(ARCHIVE_DIR) if ARCHIVE_DIR.is_dir() else ():
        if entry.is_file(follow_symlinks=False) and (
                entry.name in (CONFIG.name, HEALTH.name, "lock")
                or entry.name.startswith(".lock.owner-reap-")):
            bookkeeping += entry.stat(follow_symlinks=False).st_size
    retained = stored - bookkeeping
    if retained <= 0 and not _captures_retained():
        print("archive holds no captured copies; nothing to purge")
        return 0
    target = common.terminal_safe(ARCHIVE_DIR)
    if not assume_yes:
        if not sys.stdin.isatty():
            print(f"archive purge would delete {_human_bytes(retained)} of archived copies "
                  f"in {target}; pass --yes to confirm")
            return 1
        answer = input(f"delete {_human_bytes(retained)} of archived copies in {target}, "
                       "including copies of chats their agents already deleted? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("archive purge cancelled; nothing was deleted")
            return 1
    _purge_locked()
    print(f"archive purged: {_human_bytes(retained)} of archived copies deleted "
          f"from {target}")
    if s.get("enabled"):
        print(f"retention is still on: the next capture pass re-archives the live "
              f"stores (`{common.cli_name()} archive --off` stops that)")
    return 0


def _reconstruct(rec: dict) -> bytes:
    output = io.BytesIO()
    _reconstruct_to_stream(rec, output)
    return output.getvalue()


def _reconstruct_to_stream(rec: dict, output) -> None:
    digest = hashlib.sha256()
    total = sum(_stream_chunk(chunk, output, digest) for chunk in rec["chunks"])
    if digest.hexdigest() != rec["sha256"]:
        raise ValueError(f"reconstruction of {rec['path']} fails its pinned hash")
    if total != rec["size"]:
        raise ValueError(f"reconstruction of {rec['path']} has wrong length")


def _matches_restore_needle(value: object, needle: str) -> bool:
    if not isinstance(value, dict):
        return False
    path = value.get("path")
    sessions = value.get("sessions")
    path_needle = needle.replace("\\", "/")
    return ((isinstance(path, str)
             and path_needle in path.lower().replace("\\", "/"))
            or (isinstance(sessions, list)
                and any(isinstance(session, str) and session.startswith(needle)
                        for session in sessions)))


def _find(needle: str) -> list[dict]:
    """Manifest records matching a session id (prefix ok, 8+ chars) or a path
    substring - newest version per path."""
    needle = needle.lower()
    values, torn_tail, unsafe_record = _manifest_values()
    records = [rec for rec in values if _valid_manifest_record(rec)]
    out = []
    latest = {rec["path"]: rec for rec in records}
    for rec in latest.values():
        if _matches_restore_needle(rec, needle):
            out.append(rec)
    selected_paths = {rec["path"] for rec in out}
    uncertain = torn_tail or unsafe_record
    for value in values:
        if _valid_manifest_record(value) or not isinstance(value, dict):
            continue
        path = value.get("path")
        if ((isinstance(path, str) and path in selected_paths)
                or _matches_restore_needle(value, needle)):
            uncertain = True
            break
    if uncertain:
        raise ValueError(
            "archive manifest cannot prove the requested archive version")
    return sorted(out, key=lambda r: r["ts"], reverse=True)


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _ensure_plain_parent(root: Path, parent: Path) -> tuple[int, int]:
    root.mkdir(parents=True, exist_ok=True)
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("restore destination escapes its selected root") from exc
    current = root
    for part in (None, *relative.parts):
        current = current if part is None else current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            info = current.lstat()
        if _linklike(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"unsafe restore parent: {current}")
    return _directory_identity(info)


def _plain_target_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if _linklike(path, info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"unsafe restore target: {path}")
    return True


def _replace_restore_temp(
        tmp: Path, dst: Path, parent_identity: tuple[int, int], force: bool) -> None:
    if os.name == "nt":
        if _ensure_plain_parent(dst.parent, dst.parent) != parent_identity:
            raise ValueError("restore parent changed during publication")
        if _plain_target_exists(dst) and not force:
            raise FileExistsError(dst)
        common.replace_with_retry(tmp, dst)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(dst.parent, flags)
    try:
        if _directory_identity(os.fstat(directory)) != parent_identity:
            raise ValueError("restore parent changed during publication")
        try:
            target = os.stat(dst.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            target = None
        if target is not None:
            if stat.S_ISLNK(target.st_mode) or not stat.S_ISREG(target.st_mode):
                raise ValueError(f"unsafe restore target: {dst}")
            if not force:
                raise FileExistsError(dst)
        source = os.stat(tmp.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(source.st_mode):
            raise ValueError("restore temporary file is not regular")
        if force:
            os.replace(tmp.name, dst.name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            # link publishes atomically: EEXIST proves a file appeared in the
            # stat-to-publish window, and without --force it must survive
            os.link(tmp.name, dst.name, src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(tmp.name, dir_fd=directory)
        try:
            os.fsync(directory)
        except OSError:
            pass
    finally:
        os.close(directory)


def _sqlite_sidecars(dst: Path) -> tuple[Path, ...]:
    return tuple(dst.with_name(dst.name + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)


def _discard_uncommitted_sidecar_parking(aside: Path) -> None:
    try:
        aside.unlink(missing_ok=True)
    except OSError:
        print(common.terminal_safe(
            surface.restore_unused_parking_residue_line(aside)))


def _park_sqlite_sidecars(dst: Path) -> list[tuple[Path, Path]]:
    """Move a destination's live -wal/-shm/-journal aside so the restored main
    file can never be published beside sidecars from another generation. The
    move is reversible until the caller settles it."""
    parked: list[tuple[Path, Path]] = []
    try:
        for sidecar in _sqlite_sidecars(dst):
            try:
                info = sidecar.lstat()
            except FileNotFoundError:
                continue
            if _linklike(sidecar, info) or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"unsafe restore sidecar: {sidecar}")
            fd, aside = tempfile.mkstemp(
                prefix=f".{sidecar.name}.superseded-", dir=dst.parent)
            parking = Path(aside)
            try:
                os.close(fd)
            except Exception:
                _discard_uncommitted_sidecar_parking(parking)
                raise
            try:
                common.replace_with_retry(
                    sidecar, parking,
                    attempts=_RESTORE_SIDECAR_MOVE_ATTEMPTS)
            except Exception as exc:
                _discard_uncommitted_sidecar_parking(parking)
                if getattr(exc, "winerror", None) in (32, 33):
                    raise ValueError(
                        surface.restore_locked_sidecar_error(sidecar)) from exc
                raise
            parked.append((sidecar, parking))
    except Exception:
        _disclose_parked_residue(_settle_parked_sidecars(parked, False))
        raise
    return parked


def _settle_parked_sidecars(
        parked: list[tuple[Path, Path]], committed: bool) -> list[tuple[Path, Path]]:
    """Drop the superseded sidecars once the main file landed, else put them
    back. A failed rollback leaves the parked copy on disk rather than a
    database sitting beside a sidecar it does not belong to; every failure is
    returned to the caller, never swallowed."""
    failures: list[tuple[Path, Path]] = []
    for sidecar, aside in reversed(parked):
        try:
            if committed:
                aside.unlink(missing_ok=True)
            else:
                common.replace_with_retry(aside, sidecar)
        except OSError:
            failures.append((sidecar, aside))
    return failures


def _disclose_parked_residue(failures: list[tuple[Path, Path]]) -> None:
    for sidecar, aside in failures:
        print(common.terminal_safe(
            surface.restore_rollback_residue_line(sidecar, aside)))


def _publish_restore_writer(dst: Path, root: Path, writer, force: bool, *,
                            sqlite_store: bool = False) -> None:
    parent_identity = _ensure_plain_parent(root, dst.parent)
    if _plain_target_exists(dst) and not force:
        raise FileExistsError(dst)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.restore-", dir=dst.parent)
    tmp = Path(tmp_name)
    parked: list[tuple[Path, Path]] = []
    committed = False
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if _ensure_plain_parent(root, dst.parent) != parent_identity:
            raise ValueError("restore parent changed during publication")
        if _plain_target_exists(dst) and not force:
            raise FileExistsError(dst)
        if sqlite_store:
            parked = _park_sqlite_sidecars(dst)
        _replace_restore_temp(tmp, dst, parent_identity, force)
        committed = True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            parent = dst.parent.lstat()
            if (not _linklike(dst.parent, parent)
                    and _directory_identity(parent) == parent_identity):
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        residue = _settle_parked_sidecars(parked, committed)
        if residue and committed:
            # published fine, but stale sidecar copies survived cleanup: that
            # must fail the verdict instead of printing "sha256 verified"
            raise ValueError(surface.restore_residue_error(
                [aside for _, aside in residue]))
        _disclose_parked_residue(residue)


def _verify_published_restore(dst: Path, root: Path, rec: dict,
                              sqlite_store: bool) -> None:
    """Prove the claim the restore line is about to make, against what the next
    reader will open - not against the bytes just written."""
    if sqlite_store:
        for sidecar in _sqlite_sidecars(dst):
            if sidecar.exists() or sidecar.is_symlink():
                raise ValueError(
                    f"restored store still carries a superseded sidecar: {sidecar.name}")
            for residue in sorted(dst.parent.glob(f".{sidecar.name}.superseded-*")):
                raise ValueError(surface.restore_residue_error([residue]))
    sha, size, _, _, _, _ = _hash_regular_file(dst, root)
    if sha != rec["sha256"] or size != rec["size"]:
        raise ValueError("published restore does not match its pinned hash")


def _publish_restore(dst: Path, root: Path, data: bytes, force: bool) -> None:
    _publish_restore_writer(dst, root, lambda stream: stream.write(data), force)


def _one_session(matches: list[dict], needle: str) -> bool:
    """Every match carries a session id the needle selects: one session's files
    (a Claude transcript with its subagents/ side files, a pi session and its
    sidecar directory), restorable together because their paths never collide."""
    shared = set.intersection(*(set(rec.get("sessions") or ()) for rec in matches))
    return any(session.startswith(needle.lower()) for session in shared)


def _never_archived_sources(needle: str) -> list[str]:
    """Lines for discovered sqlite stores the needle names that have no archived
    copy: a store written in the last settle window is deferred on every pass."""
    lines = []
    try:
        for _agent, path, is_sqlite in _discovered_sources():
            if not is_sqlite or not _matches_restore_needle(
                    {"path": str(path), "sessions": []}, needle.lower()):
                continue
            try:
                _sig, activity, _st = _sqlite_source_state(path)
            except (OSError, ValueError):
                continue
            if time.time() - activity < _SQLITE_SETTLE_S:
                lines.append(
                    f"{common.terminal_safe(path)} has no archived copy yet: capture "
                    f"deferred it (written in the last {_SQLITE_SETTLE_S // 60} min)")
            else:
                lines.append(f"{common.terminal_safe(path)} has no archived copy yet: "
                             f"run `{common.cli_name()} archive`")
    except (OSError, ValueError):
        return lines
    return lines


def restore(needle: str, to: str | None = None, force: bool = False) -> int:
    matches = _find(needle)
    safe_needle = common.terminal_safe(needle)
    if not matches:
        print(f"no archived file matches {safe_needle!r}")
        for line in _never_archived_sources(needle):
            print(line)
        return 1
    if len(matches) > 1 and to is None and not _one_session(matches, needle):
        print(f"{len(matches)} archived files match {safe_needle!r}:")
        for rec in matches[:10]:
            print(f"  {common.terminal_safe(rec['path'])}  ({rec['size']} bytes, "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['ts'] / 1000))})")
        print("narrow the match, or --to a directory to restore all of them")
        return 1
    rc = 0
    plan: list[tuple[dict, Path, Path]] = []
    for rec in matches:
        location = _restore_location(rec)
        if to:
            root = Path(to).expanduser().absolute()
            if location is not None:
                relative = location[1]
            else:
                parsed = _stored_absolute_parts(rec.get("path"))
                if parsed is None:
                    print(f"cannot restore {common.terminal_safe(rec.get('path'))}: "
                          "manifest path is not a portable absolute path")
                    rc = 1
                    continue
                relative = Path(parsed[1][-1])
            dst = root / relative
        else:
            if location is None:
                print(f"cannot restore {common.terminal_safe(rec.get('path'))}: "
                      "manifest path is outside documented store roots")
                rc = 1
                continue
            root, relative = location
            dst = root / relative
        plan.append((rec, root, dst))
    # Full-plan collision gate: two archived sources aiming at one restored
    # file would overwrite each other, so nothing is written when names clash.
    claimed: dict[str, dict] = {}
    for rec, _root, dst in plan:
        prior = claimed.setdefault(os.path.normcase(str(dst)), rec)
        if prior is not rec:
            print(f"refusing to restore: {common.terminal_safe(prior['path'])} "
                  f"and {common.terminal_safe(rec['path'])} would both restore "
                  f"to {common.terminal_safe(dst)} - narrow the match")
            return 1
    for rec, root, dst in plan:
        sqlite_store = bool(rec.get("sqlite"))
        try:
            _publish_restore_writer(
                dst, root, lambda stream, record=rec: _reconstruct_to_stream(record, stream),
                force, sqlite_store=sqlite_store)
            _verify_published_restore(dst, root, rec, sqlite_store)
        except FileExistsError:
            print("exists, not overwriting (--force to replace): "
                  f"{common.terminal_safe(dst)}")
            rc = 1
            continue
        except (OSError, ValueError, lzma.LZMAError) as e:
            print(f"cannot restore {common.terminal_safe(rec['path'])}: "
                  f"{common.terminal_safe(e)}")
            rc = 1
            continue
        print(f"restored {common.terminal_safe(dst)} "
              f"({rec['size']} bytes, sha256 verified: {rec['sha256']})")
    return rc


def _manifest_status(*, timeout_s: float | None = None) -> dict:
    deadline = (
        None if timeout_s is None
        else time.monotonic() + max(0.0, timeout_s)
    )
    try:
        values, torn_tail, unsafe_record = _manifest_values(
            deadline=deadline)
        valid: list[dict] = []
        invalid: list[object] = []
        future = 0
        migration = 0
        for value in values:
            if deadline is not None and time.monotonic() >= deadline:
                raise ArchiveStatusBudget(
                    "archive manifest validation exceeded its routine budget")
            if _valid_manifest_record(value):
                valid.append(value)
                migration += int(_record_migrated(value))
            else:
                invalid.append(value)
                future += int(_future_manifest_record(value))
    except ArchiveStatusBudget as exc:
        return {
            "state": "budget-exceeded", "detail": str(exc), "records": [],
            "invalid_records": None, "migration_records": None,
        }
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "detail": str(exc), "records": [],
                "invalid_records": 1, "migration_records": 0}
    if unsafe_record or invalid:
        state = "future" if future == len(invalid) and not unsafe_record else "unrecognized"
    elif torn_tail:
        state = "repairable-tail"
    elif migration:
        state = "migration"
    else:
        state = "healthy"
    return {"state": state, "records": valid,
            "invalid_records": len(invalid) + int(unsafe_record),
            "future_records": future, "migration_records": migration,
            "torn_tail": torn_tail}


def status(
        *, stored_bytes: int | None = None,
        manifest_timeout_s: float | None = None,
) -> dict:
    deadline = (
        None if manifest_timeout_s is None
        else time.monotonic() + max(0.0, manifest_timeout_s)
    )

    def remaining() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def deferred(
            phase: str, *, on: bool | None = None,
            stored: int | None = None, lock: dict | None = None,
            last_pass: dict | None = None,
            config_state: str = "not-inspected",
    ) -> dict:
        return {
            "enabled": on,
            "config_state": config_state,
            "state": "status-deferred",
            "detail": (
                f"routine archive budget expired before {phase}; "
                "run `agrep doctor --deep` for the full archive verdict"),
            "manifest_state": "not-inspected",
            "invalid_records": None,
            "migration_records": None,
            "files": None,
            "raw_bytes": None,
            "stored_bytes": stored,
            "ratio": None,
            "lock": lock or {"state": "not-inspected"},
            "last_pass": last_pass or {
                "outcome": "not-inspected", "age_s": None, "fresh": False},
        }

    if expired():
        return deferred("archive configuration")
    config = _config_observation()
    if expired():
        return deferred("archive configuration")
    if config["state"] == "unavailable":
        return {
            "enabled": None,
            "config_state": "unavailable",
            "state": "capture-blocked",
            "detail": str(config.get("detail") or
                          "archive configuration is unavailable"),
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
                "outcome": "not-inspected", "age_s": None, "fresh": False},
        }
    on = bool(config["value"].get("enabled", False))
    config_state = str(config["state"])

    if stored_bytes is None:
        stored = 0
        try:
            # the figure doctor's footprint reports too: every file under data/archive
            if ARCHIVE_DIR.exists():
                for current, dirs, names in os.walk(
                        ARCHIVE_DIR, topdown=True, followlinks=False):
                    if expired():
                        return deferred(
                            "stored-byte accounting", on=on, stored=None,
                            config_state=config_state)
                    # Archive storage is private and flat in normal operation.
                    # Do not let a planted link turn status into an outside walk.
                    dirs[:] = [
                        name for name in dirs
                        if not (Path(current) / name).is_symlink()
                    ]
                    for name in names:
                        if expired():
                            return deferred(
                                "stored-byte accounting", on=on, stored=None,
                                config_state=config_state)
                        found = (Path(current) / name).lstat()
                        reparse = getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                        if (stat.S_ISLNK(found.st_mode) or bool(getattr(
                                found, "st_file_attributes", 0) & reparse)):
                            continue  # a planted link is the manifest reader's verdict
                        if not stat.S_ISREG(found.st_mode):
                            raise OSError(
                                "archive store contains a non-regular entry")
                        stored += int(found.st_size)
        except OSError as exc:
            return {
                **deferred(
                    "stored-byte accounting", on=on, stored=None,
                    config_state=config_state),
                "state": "capture-blocked",
                "detail": (
                    "archive stored-byte accounting is unavailable "
                    f"({type(exc).__name__}: {exc})"),
            }
    else:
        stored = max(0, int(stored_bytes))
    if not on:
        return {
            "enabled": False,
            "config_state": config_state,
            "state": "disabled",
            "detail": _disabled_detail(stored),
            "manifest_state": "not-inspected",
            "invalid_records": None,
            "migration_records": None,
            "files": None,
            "raw_bytes": None,
            "stored_bytes": stored,
            "ratio": None,
            "lock": {"state": "not-inspected"},
            "last_pass": {
                "outcome": "not-inspected", "age_s": None, "fresh": False},
        }
    if expired():
        return deferred(
            "capture-health observation", on=on, stored=stored,
            config_state=config_state)
    health = _capture_health()
    if expired():
        return deferred(
            "archive-lock observation", on=on, stored=stored,
            last_pass={**health, "fresh": False},
            config_state=config_state)
    lock = _lock_status()
    if expired():
        return deferred(
            "manifest validation", on=on, stored=stored, lock=lock,
            last_pass={**health, "fresh": False},
            config_state=config_state)
    age = health.get("age_s")
    tick = _tick_interval_s()
    last_pass = {**health, "fresh": age is not None and age <= tick * 2.0,
                 "tick_interval_s": tick}
    waiting = waiting_summary(health)
    manifest = _manifest_status(timeout_s=remaining())
    if manifest["state"] == "budget-exceeded":
        return {
            "enabled": True,
            "config_state": config_state,
            "state": "status-deferred",
            "detail": (
                f"{manifest['detail']}; run `agrep doctor --deep` for the "
                "full archive verdict"),
            "manifest_state": "not-inspected",
            "invalid_records": None,
            "migration_records": None,
            "files": None,
            "raw_bytes": None,
            "stored_bytes": stored,
            "ratio": None,
            "lock": lock,
            "last_pass": last_pass,
        }
    latest = {record["path"]: record for record in manifest["records"]}
    raw = sum(record["size"] for record in latest.values())
    if manifest["state"] in ("future", "unrecognized", "unreadable"):
        state = "capture-blocked"
        detail = ("future manifest record requires a newer archive reader"
                  if manifest["state"] == "future" else
                  manifest.get("detail") or "manifest contains an unrecognized record")
    elif lock["state"] == "busy":
        state = "stalled" if lock.get("stalled") else "busy"
        detail = _running_line(lock)
    elif lock["state"] == "interrupted":
        state, detail = "interrupted", str(lock.get("detail"))
    elif lock["state"] == "lock-wedged":
        state, detail = "lock-wedged", str(lock.get("detail") or "archive lock is wedged")
    elif lock["state"] == "lock-protected":
        state, detail = "lock-protected", str(lock.get("detail") or "lock is protected")
    elif manifest["state"] == "repairable-tail":
        state, detail = "repairable-tail", "manifest ends in an unframed torn record"
    elif health["outcome"] == "partial":
        state, detail = "partial", str(health.get("detail") or "source capture was partial")
    elif health["outcome"] in ("capture-blocked", "unknown"):
        state = "capture-blocked"
        detail = str(health.get("detail") or "last capture outcome is unavailable")
    elif health["outcome"] in ("never", "busy"):
        state = "freshness-unknown"
        detail = ("no capture pass has completed"
                  if health["outcome"] == "never" else
                  "the last capture attempt did not acquire the archive lock")
    elif manifest["state"] == "migration":
        state = "migration"
        detail = f"{manifest['migration_records']} record(s) re-anchor to the current home"
    elif waiting is not None and "disk" in waiting["by_kind"]:
        state, detail = "disk-full", str(disk_line(health))
    else:
        state, detail = "healthy", "archive manifest and capture state are readable"
    return {"enabled": on, "config_state": config_state,
            "state": state, "detail": detail,
            "manifest_state": manifest["state"],
            "invalid_records": manifest["invalid_records"],
            "migration_records": manifest["migration_records"],
            "files": len(latest), "raw_bytes": raw, "stored_bytes": stored,
            "ratio": round(raw / stored, 2) if stored else None,
            "lock": lock, "last_pass": last_pass, "waiting": waiting}


def restore_main(argv: list[str] | None = None) -> int:
    import argparse
    ap = surface.ArgumentParser(
        prog="agrep restore",
        description="restore an archived store file, hash-verified, back to its "
                    "path so its agent can try to resume the session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep restore 11111111              restore one session's store file\n"
               "  agrep restore rollout-2026 --to .  restore matching files here\n"
               "\nexit: 0 restored; 1 no unique match or a file was not restored; "
               "2 archive unavailable or invalid arguments.",
        allow_abbrev=False)
    ap.add_argument("needle", help="session id (8+ chars) or a path substring")
    ap.add_argument("--to", help="restore into this directory instead of the original path")
    ap.add_argument("--force", action="store_true", help="overwrite an existing live file")
    args = ap.parse_args(argv)
    # a blank value here restores files: an empty needle matches every archived
    # file, and an empty --to falls back to the paths it was told not to use
    for flag, value, vocabulary, effect in (
            ("needle", args.needle, "a session id (8+ chars) or a path substring",
             "match every archived file"),
            ("--to", args.to, "a directory to restore into",
             "restore to the original path instead")):
        blank = surface.blank_value_error(flag, value, vocabulary, effect)
        if blank:
            ap.error(blank)
    try:
        return restore(args.needle, to=args.to, force=args.force)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"archive restore unavailable: {common.terminal_safe(exc)}")
        return 2


def _main(argv: list[str] | None = None) -> int:
    import argparse

    def nonnegative(value: str) -> int:
        parsed = int(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be zero or greater")
        return parsed

    ap = surface.ArgumentParser(
        prog="agrep archive",
        description="snapshot the agents' own store files (files byte-for-byte, "
                    "sqlite via backup api; content-addressed, compressed); "
                    "`agrep restore` brings one back",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep archive --status  inspect retention and the last capture\n"
               "  agrep archive --on      enable automatic source-store captures\n"
               "  agrep archive           capture changed source stores now\n"
               "  agrep archive --purge   delete every archived copy (asks first)\n"
               "\nexit: 0 complete; 1 incomplete, busy, or unhealthy; "
               "2 archive unavailable or invalid arguments.",
        allow_abbrev=False)
    # one verb per run: `--on --off` (or any pair) has no defined winner, so
    # argparse rejects the combination instead of a branch order picking one
    actions = ap.add_mutually_exclusive_group()
    actions.add_argument("--on", action="store_true", help="enable auto-capture on the indexer tick")
    actions.add_argument("--off", action="store_true",
                         help="disable auto-capture (archived copies stay until --purge)")
    actions.add_argument("--status", action="store_true", help="show archive size/ratio")
    actions.add_argument("--keep", type=nonnegative, metavar="N",
                         help="versions kept per file (persists; 0 = never prune; default 3)")
    actions.add_argument("--prune", action="store_true", help="prune old versions now")
    actions.add_argument("--purge", action="store_true",
                         help="delete every archived copy and the manifest; the "
                              "on/off and keep settings stay")
    ap.add_argument("--yes", action="store_true", help="with --purge: skip the confirmation")
    ap.add_argument("--scheduled-pass", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.yes and not args.purge:
        ap.error("--yes only confirms --purge")
    if args.on or args.off:
        set_enabled(args.on)
        print(f"archive auto-capture {'enabled' if args.on else 'disabled'}")
        if args.off:
            stored = status().get("stored_bytes")
            if stored and _captures_retained():
                print(f"the copies already taken stay: {_human_bytes(stored)} in "
                      f"{common.terminal_safe(ARCHIVE_DIR)}; "
                      f"`{common.cli_name()} archive --purge` deletes them")
        return 0
    if args.keep is not None:
        _set_config(keep=args.keep)
        print("archive keep = "
              f"{args.keep if args.keep > 0 else '0 (never prune)'} "
              "version(s) per source file")
        return 0
    if args.prune or args.purge:
        verb = "purge" if args.purge else "prune"
        fd = _try_lock()
        if fd is None:
            lock = _lock_status()
            if lock.get("state") == "lock-wedged":
                print(f"archive {verb} blocked: lock-wedged at "
                      f"{common.terminal_safe(lock.get('path'))}; "
                      f"pid={lock.get('pid') or 'unknown'} "
                      f"age={float(lock.get('age_s') or 0):.1f}s; "
                      f"repair required before {verb[:-1]}ing")
            else:
                print(f"archive {verb} blocked by an owned or protected lock; "
                      "inspect `agrep archive --status`")
            return 1
        try:
            if args.purge:
                return _purge_main(args.yes)
            r = _prune(verbose=True)
        finally:
            _unlock(fd)
        print(f"pruned {r['dropped']} old version record(s), "
              f"reclaimed {_human_bytes(r['reclaimed'])}")
        return 0
    if args.status:
        s = status()
        age = s["last_pass"].get("age_s")
        # sentences, not a state dump: a measurement agrep does not have is
        # omitted rather than printed as a placeholder
        if not s["enabled"]:
            print(f"archive retention is off (`{common.cli_name()} archive --on` "
                  "turns it on)")
            if s["stored_bytes"] and _captures_retained():
                print(f"earlier captures still stored: {_human_bytes(s['stored_bytes'])} "
                      f"in {common.terminal_safe(ARCHIVE_DIR)}; "
                      f"`{common.cli_name()} archive --purge` deletes them")
        else:
            parts = []
            if type(s["files"]) is int:
                parts.append(f"{s['files']:,} files")
            if type(s["stored_bytes"]) is int:
                parts.append(f"{_human_bytes(s['stored_bytes'])} stored")
            if s["ratio"]:
                parts.append(f"{s['ratio']}x smaller than the originals")
            if age is None and s["state"] == "interrupted":
                parts.append("capture interrupted before a pass completed")
            else:
                parts.append("never captured yet" if age is None
                             else f"last capture {surface.brief_duration(age)} ago")
            keep = _keep()
            retention = (f"keeps the last {keep} version(s)" if keep
                         else "keeps every version (never prunes)")
            print(f"archive {retention} per source file: " + ", ".join(parts))
            if s.get("waiting"):
                line = waiting_line(s["last_pass"])
                if line:
                    print(f"archive {line}")
        if s["state"] not in ("healthy", "disabled", "migration", "interrupted"):
            print(f"archive {s['state']}: {common.terminal_safe(s['detail'])}")
            return 2 if s["manifest_state"] == "unreadable" else 1
        if s["state"] == "interrupted":
            print(f"archive: {common.terminal_safe(s['detail'])}")
        return 0
    t0 = time.time()
    stats = capture(verbose=True, scheduled=args.scheduled_pass)
    if stats.get("busy"):
        lock = stats.get("lock") or {}
        if lock.get("state") == "lock-wedged":
            print(f"archive lock is wedged at {common.terminal_safe(lock.get('path'))}; "
                  f"pid={lock.get('pid') or 'unknown'} age={float(lock.get('age_s') or 0):.1f}s")
        else:
            print(f"archive: {_running_line(lock)}; it captures the same sources, "
                  f"inspect `{common.cli_name()} archive --status`")
        return 1
    deferred = f", {stats['deferred']} deferred" if stats["deferred"] else ""
    print(f"archive: {stats['files']} files - {stats['unchanged']} unchanged, "
          f"{stats['appended']} appended, {stats['full']} full captures{deferred}, "
          f"{_human_bytes(stats['bytes_stored'])} new content in {time.time() - t0:.1f}s")
    for item in stats["deferrals"][:10]:
        print(f"  deferred ({common.terminal_safe(item['reason'])}): "
              f"{common.terminal_safe(item['path'])}")
    if stats["failed"]:
        print(f"archive incomplete: {stats['failed']} source path(s) failed; "
              "successful captures were kept")
        for failure in stats["failures"][:10]:
            print(f"  {common.terminal_safe(failure['path'])}: "
                  f"{common.terminal_safe(failure['error'])}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"archive unavailable: {common.terminal_safe(exc)}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
