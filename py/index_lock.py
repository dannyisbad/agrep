"""Cross-language ownership protocol for the corpus generation lock."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

from events import DATA_DIR, EVENT_STORE_NAME, data_dir_readonly, open_regular_binary
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


def holder_live(path: Path | None = None, *, alive=None, start_identity=None) -> bool:
    """Whether the lock record at `path` names its exact live process."""
    alive = pid_alive if alive is None else alive
    start_identity = process_start_identity if start_identity is None else start_identity
    try:
        observed = _snapshot(INDEX_LOCK_PATH if path is None else path)
        owner = parse_owner(observed.raw) if observed is not None else None
        if owner is None or owner.start in (None, "unknown"):
            return False
        return alive(owner.pid) and start_identity(owner.pid) == owner.start
    except OSError:
        return False


_SQLITE_SIDECARS = (
    ("-journal", "rollback journal"), ("-wal", "write-ahead log"), ("-shm", "shared-memory file"))


def _user_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def _first_byte_nonzero(path: Path) -> bool:
    # SQLite takes no POSIX lock on a journal, so this read cannot drop one a connection holds.
    try:
        with open_regular_binary(path) as stream:
            return stream.read(1) not in (b"", b"\0")
    except OSError:
        return False


def _wal_mode(database: Path) -> bool:
    # Read only for a blocked -shm with no -wal; every reader already opens this header.
    try:
        with open_regular_binary(database) as stream:
            header = stream.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18:20] == b"\x02\x02"


def _sidecar_blocks(database: Path, suffix: str) -> bool:
    """Whether this sidecar, already known not readable and writable here, stops every pass.
    The writer itself gets past a readable inert journal (debris), a -shm a DELETE-mode store
    never uses (corpus.db's only if readable), and the event store's empty -wal beside a
    DELETE-mode header (its settle removes it); anything else it cannot use."""
    path = Path(f"{database}{suffix}")
    if suffix == "-journal":
        return not os.access(path, os.R_OK) or _first_byte_nonzero(path)
    if suffix == "-shm":
        # the Rust writer's corpus.db ownership probe reads every sidecar's first bytes, and
        # serves read-only when it cannot
        if database.name != EVENT_STORE_NAME:
            if not os.access(path, os.R_OK):
                return True
            return Path(f"{database}-wal").exists() or _wal_mode(database)
        return not _settled_wal(database) or _wal_mode(database)
    if suffix == "-wal" and database.name == EVENT_STORE_NAME:
        return not _settled_wal(database) or _wal_mode(database)
    return True


def _settled_wal(database: Path) -> bool:
    # absent or empty: SQLite reads a zero-byte -wal as no WAL at all
    try:
        return Path(f"{database}-wal").lstat().st_size == 0
    except FileNotFoundError:
        return True
    except OSError:
        return False


def inaccessible_sidecars(database: Path, store: str) -> list[dict]:
    """SQLite sidecars beside `database` this user cannot both read and write and no pass can
    clean up. The sidecars themselves are never opened beyond a journal's first byte, which
    SQLite never locks: closing a probe fd on a locked file would drop SQLite's own locks."""
    getuid = getattr(os, "getuid", None)
    found = []
    for suffix, kind in _SQLITE_SIDECARS:
        path = Path(f"{database}{suffix}")
        try:
            info = path.lstat()
        except OSError:
            continue
        if (not stat.S_ISREG(info.st_mode) or os.access(path, os.R_OK | os.W_OK)
                or not _sidecar_blocks(database, suffix)):
            continue
        if getuid is not None and info.st_uid != getuid():
            access = "owner"
        elif info.st_mode & 0o600 == 0o600:
            access = "flags"  # the mode allows it, so an immutable flag or an ACL denies it
        else:
            access = "mode"
        found.append({"store": store, "kind": kind, "path": str(path), "access": access,
                      "owner": _user_name(info.st_uid) if access == "owner" else None})
    return found


def rollback_journal_state(database: Path, lock_path: Path | None = None) -> dict:
    """The rollback journal beside `database`: `state` (`none`, `live-pass`, `interrupted-pass`
    or `inaccessible` to this user), `bytes`, and `hot` (read only without a live pass): SQLite
    replays it, so read-only readers fail until a write-capable open."""
    journal = Path(f"{database}-journal")

    def observed() -> tuple[int, int, int, int] | None:
        try:
            info = journal.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return None
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def result(state: str, observation: tuple | None, hot: bool = False) -> dict:
        return {"state": state, "bytes": observation[2] if observation else 0, "hot": hot}

    before = observed()
    if before is None:
        return result("none", None)
    # Every writer of corpus.db and the event store is an index pass holding this lock, so a
    # journal without a live holder is a killed pass's. A live one's journal is never opened:
    # on Windows that open could collide with the writer deleting it at commit.
    if holder_live(lock_path):
        return result("live-pass", before)
    if not os.access(journal, os.R_OK | os.W_OK) and _sidecar_blocks(database, "-journal"):
        return result("inaccessible", before)
    hot = _first_byte_nonzero(journal)
    # A pass that committed and released (or started) around these reads replaced the journal.
    after = observed()
    if after != before:
        return result("none" if after is None else "live-pass", after)
    return result("interrupted-pass", before, hot)


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
        # what the exit found in place of this process's record, if anything
        self.displaced: str | None = None

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
            token = secrets.token_hex(16)
            raw = render_record(
                pid=pid,
                start=process_start,
                token=token,
                label=self.label,
                time_ms=time.time_ns() // 1_000_000,
            )
            try:
                self._handle = ownerfile.create_held(
                    INDEX_LOCK_PATH, raw, token=token, mode=0o600, retain_fd=True,
                    exact_mode=True)
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
                        stranded = ownerfile.stranded_own_record(
                            owner.pid, owner.start,
                            _owner_field(_record_text(observed.raw) or "", "token"),
                            process_start=process_start_identity)
                        if ((not alive or reused or stranded)
                                and ownerfile.remove_exact(
                                    INDEX_LOCK_PATH, observed)):
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

    def verify(self) -> None:
        """The record at the path is still exactly this process's. Called before each
        derived commit: a holder whose record was reaped or replaced excludes nobody."""
        handle = self._handle
        if handle is None:
            raise OSError(f"index lock is not held: {INDEX_LOCK_PATH}")
        try:
            observed = ownerfile.snapshot(handle.path)
        except FileNotFoundError as error:
            raise OSError(
                f"index lock disappeared while held: {handle.path}") from error
        except OSError as error:
            raise OSError(
                f"could not verify index lock {handle.path}: {error}") from error
        if (observed.identity[:3] != handle.snapshot.identity[:3]
                or observed.raw != handle.snapshot.raw):
            raise OSError(
                f"index lock was displaced while held: {handle.path} "
                f"now held by {_holder_description(observed)}")

    def __exit__(self, exc_type, exc, tb) -> None:
        """Remove this process's record. A record neither at the path nor in a tomb was
        removed or replaced while this writer ran; every derived commit re-verified the
        record first, so that is disclosed on stderr and kept in `displaced`, never raised,
        and whatever now holds the slot is never touched. A record of ours that could not
        be removed raises."""
        handle = self._handle
        self._handle = None
        self.displaced = None
        if handle is not None:
            if data_dir_readonly(handle.path.parent):
                handle.close()
                ownerfile.forget_held(handle.token)
                raise PermissionError(
                    "AGREP_DATA_READONLY protects this data directory")
            released = handle.release()
            if released:
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
                if (observed.identity == handle.snapshot.identity
                        and observed.raw == handle.snapshot.raw):
                    raise OSError(
                        f"could not release owned index lock: {candidate}")
            try:
                observed = _snapshot(handle.path)
            except OSError:
                observed = None
            self.displaced = (
                f"index lock disappeared while held: {handle.path}"
                if observed is None else
                f"index lock was displaced while held: {handle.path} "
                f"now held by {_holder_description(observed)}")
            print(f"{self.displaced}; another writer may have run alongside "
                  "this one: `agrep doctor`", file=sys.stderr, flush=True)
