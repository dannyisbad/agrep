"""Cross-language ownership protocol for the corpus generation lock."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time

from events import DATA_DIR, data_dir_readonly
import ownerfile
from proc import WIN, pid_alive, process_start_identity


PROTOCOL = "index-lock-v1"
TIMEOUT_MS = 1_800_000
PUBLICATION_GRACE_MS = 3_000
RETRY_INITIAL_MS = 50
RETRY_FACTOR_NUMERATOR = 3
RETRY_FACTOR_DENOMINATOR = 2
RETRY_MAX_MS = 1_000
MAX_RECORD_BYTES = 4_096
MAX_PID = 0xFFFFFFFF if WIN else 0x7FFFFFFF
INDEX_LOCK_PATH = DATA_DIR / ".index.lock"
WAIT_NOTICE_S = 1.0
WAIT_REPEAT_S = 30.0

_LABEL_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class Owner:
    pid: int
    start: str | None


def _owner_field(body: str, field: str) -> str | None:
    """Read the first ``field=value`` token from one ownership snapshot."""
    match = re.search(
        rf"(?:^|\s){re.escape(field)}=(\S+)", body, flags=re.ASCII)
    return match.group(1) if match else None


def _record_text(raw: bytes) -> str | None:
    if (not raw or len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1 or b"\0" in raw):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    body = text[:-1]
    return body[:-1] if body.endswith("\r") else body


def parse_owner(raw: bytes) -> Owner | None:
    """Extract the forward-compatible safety core from one complete record."""
    body = _record_text(raw)
    if body is None:
        return None
    parts = body.encode("utf-8").split()
    pids = [
        part.removeprefix(b"pid=")
        for part in parts
        if part.startswith(b"pid=")
    ]
    starts = [
        part.removeprefix(b"start=")
        for part in parts
        if part.startswith(b"start=")
    ]
    if len(pids) != 1 or len(starts) > 1 or (starts and not starts[0]):
        return None
    if not pids[0] or any(byte < ord("0") or byte > ord("9")
                          for byte in pids[0]):
        return None
    try:
        pid = int(pids[0], 10)
        start = starts[0].decode("utf-8") if starts else None
    except (UnicodeDecodeError, ValueError):
        return None
    if not 0 < pid <= MAX_PID:
        return None
    return Owner(pid, start)


def render_record(*, pid: int, start: str, token: str, label: str,
                  time_ms: int) -> bytes:
    """Render a strict canonical writer record."""
    if type(pid) is not int or not 0 < pid <= MAX_PID:
        raise ValueError(f"pid must be in 1..{MAX_PID}")
    if (not start or len(start) > 128 or not start.isascii()
            or any(not "!" <= character <= "~" or character == "="
                   for character in start)):
        raise ValueError("start must be one nonempty ASCII field value")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("token must be exactly 32 lowercase hex characters")
    if not _LABEL_RE.fullmatch(label):
        raise ValueError("label must match [A-Za-z0-9._:-]{1,64}")
    if (type(time_ms) is not int
            or not 0 <= time_ms <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError("time_ms must be an unsigned 64-bit integer")
    seconds, millis = divmod(time_ms, 1000)
    raw = (
        f"pid={pid} start={start} token={token} label={label} "
        f"time={seconds}.{millis:03d}\n"
    ).encode("ascii")
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f"index lock record exceeds {MAX_RECORD_BYTES} bytes")
    return raw


def _snapshot(path: Path) -> ownerfile.Snapshot | None:
    delay = RETRY_INITIAL_MS / 1000
    for attempt in range(3):
        try:
            return ownerfile.snapshot(path, max_bytes=MAX_RECORD_BYTES)
        except FileNotFoundError:
            return None
        except ownerfile.OwnershipLost:
            if attempt == 2:
                return None
            time.sleep(delay)
            delay = min(
                delay * RETRY_FACTOR_NUMERATOR / RETRY_FACTOR_DENOMINATOR,
                RETRY_MAX_MS / 1000,
            )
    return None


def _lock_field(path: Path, field: str) -> str | None:
    """Read one field from one complete ownership snapshot."""
    try:
        observed = _snapshot(path)
    except OSError:
        return None
    body = _record_text(observed.raw) if observed is not None else None
    return _owner_field(body, field) if body is not None else None


def _lock_holder_pid(path: Path) -> int | None:
    """Return the valid platform PID from one ownership snapshot."""
    try:
        observed = _snapshot(path)
    except OSError:
        return None
    owner = parse_owner(observed.raw) if observed is not None else None
    return owner.pid if owner is not None else None


def _unlink_if_unchanged(path: Path, expected: str | bytes) -> bool:
    """Tombstone and remove only the exact raw record observed by the caller."""
    if data_dir_readonly(path.parent):
        return False
    raw = expected.encode("utf-8") if isinstance(expected, str) else expected
    try:
        observed = _snapshot(path)
    except OSError:
        return False
    if observed is None or observed.raw != raw:
        return False
    return ownerfile.remove_exact(path, observed)


def _holder_description(observed: ownerfile.Snapshot) -> str:
    owner = parse_owner(observed.raw)
    if owner is None:
        return "an unparseable owner record"
    body = _record_text(observed.raw) or ""
    label = _owner_field(body, "label")
    suffix = f" label={label}" if label and _LABEL_RE.fullmatch(label) else ""
    return f"pid={owner.pid}{suffix}"


def _debug_enabled() -> bool:
    return os.environ.get("AGREP_DEBUG", "").lower() not in (
        "", "0", "false", "no", "off")


def _public_wait_notice(holder: str) -> str:
    fields = set(holder.split())
    if _holder_is_corpusdb(fields):
        return "search database update is finishing; waiting..."
    if "label=agrep-rs" in fields:
        return "another transcript index is finishing; waiting..."
    return "another index task is finishing; waiting..."


def _public_timeout_error(holder: str) -> str:
    fields = set(holder.split())
    if _holder_is_corpusdb(fields):
        return ("search database update did not finish; retry, or set "
                "AGREP_DEBUG=1 for details")
    if "label=agrep-rs" in fields:
        return ("another transcript index did not finish; retry, or set "
                "AGREP_DEBUG=1 for details")
    return ("another index task did not finish; retry, or set "
            "AGREP_DEBUG=1 for details")


def _holder_is_corpusdb(fields: set[str]) -> bool:
    return any(field == "label=corpusdb"
               or field.startswith("label=corpusdb:") for field in fields)


class IndexLock:
    """Cross-process lock shared with the Rust ingest binary."""

    def __init__(self, label: str, timeout: float = TIMEOUT_MS / 1000):
        if not _LABEL_RE.fullmatch(label):
            raise ValueError("label must match [A-Za-z0-9._:-]{1,64}")
        if (isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or timeout < 0):
            raise ValueError("timeout must be a finite nonnegative number")
        self.label = label
        self.timeout = timeout
        self._handle: ownerfile.Handle | None = None

    def __enter__(self) -> "IndexLock":
        if self._handle is not None:
            raise RuntimeError("index lock instance is already held")
        if data_dir_readonly(INDEX_LOCK_PATH.parent):
            raise PermissionError(
                "AGREP_DATA_READONLY protects this data directory")
        started = time.monotonic()
        next_notice = started + WAIT_NOTICE_S
        debug = _debug_enabled()
        public_notice_sent = False
        delay = RETRY_INITIAL_MS / 1000
        holder = "an unreadable owner record"
        while True:
            pid = os.getpid()
            process_start = process_start_identity(pid) or "unknown"
            raw = render_record(
                pid=pid,
                start=process_start,
                token=secrets.token_hex(16),
                label=self.label,
                time_ms=time.time_ns() // 1_000_000,
            )
            try:
                handle = ownerfile.create_exclusive(
                    INDEX_LOCK_PATH, raw, mode=0o600, retain_fd=True,
                    exact_mode=True)
                self._handle = handle
                return self
            except FileExistsError:
                try:
                    observed = _snapshot(INDEX_LOCK_PATH)
                except OSError:
                    observed = None
                if observed is None:
                    pass
                else:
                    holder = _holder_description(observed)
                    owner = parse_owner(observed.raw)
                    if owner is not None:
                        alive = pid_alive(owner.pid)
                        actual_start = (
                            process_start_identity(owner.pid) if alive else None)
                        reused = (
                            owner.start not in (None, "unknown")
                            and actual_start is not None
                            and actual_start != owner.start
                        )
                        if (not alive or reused) and ownerfile.remove_exact(
                                INDEX_LOCK_PATH, observed):
                            continue
                    else:
                        age_ms = (time.time() - observed.mtime) * 1000
                        if (not 0.0 <= age_ms <= PUBLICATION_GRACE_MS
                                and ownerfile.remove_exact(
                                    INDEX_LOCK_PATH, observed)):
                            continue
                now = time.monotonic()
                if now - started >= self.timeout:
                    if debug:
                        raise TimeoutError(
                            f"timed out waiting for {INDEX_LOCK_PATH} "
                            f"held by {holder}")
                    raise TimeoutError(_public_timeout_error(holder))
                if now >= next_notice:
                    remaining = max(0.0, self.timeout - (now - started))
                    if debug:
                        print(
                            f"waiting for {INDEX_LOCK_PATH} held by {holder}; "
                            f"timeout in {remaining:.0f}s",
                            file=sys.stderr, flush=True)
                    elif not public_notice_sent:
                        print(_public_wait_notice(holder), file=sys.stderr,
                              flush=True)
                        public_notice_sent = True
                    next_notice = now + WAIT_REPEAT_S
                time.sleep(delay)
                delay = min(
                    delay * RETRY_FACTOR_NUMERATOR / RETRY_FACTOR_DENOMINATOR,
                    RETRY_MAX_MS / 1000,
                )

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            if data_dir_readonly(handle.path.parent):
                handle.close()
                raise PermissionError(
                    "AGREP_DATA_READONLY protects this data directory")
            if handle.release():
                return
            candidates = [handle.path]
            prefix = f".{handle.path.name}.owner-reap-"
            try:
                candidates.extend(
                    entry for entry in handle.path.parent.iterdir()
                    if entry.name.startswith(prefix))
            except FileNotFoundError:
                candidates = [handle.path]
            except OSError as error:
                raise OSError(
                    f"could not verify index lock release: {handle.path}"
                ) from error
            for candidate in candidates:
                try:
                    observed = _snapshot(candidate)
                except OSError as error:
                    raise OSError(
                        f"could not verify index lock release: {candidate}"
                    ) from error
                if observed is None:
                    try:
                        candidate.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise OSError(
                            f"could not verify index lock release: {candidate}"
                        ) from error
                    raise OSError(
                        f"could not verify index lock release: {candidate}")
                if (observed is not None
                        and observed.identity == handle.snapshot.identity
                        and observed.raw == handle.snapshot.raw):
                    raise OSError(
                        f"could not release owned index lock: {candidate}")
