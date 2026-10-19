"""Exact-owner files for small cross-process arbitration records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Callable


_MAX_RECORD_BYTES = 64 * 1024
_DEFAULT_UNVERIFIABLE_STARTS = frozenset(("", "None", "unknown"))
_TRANSIENT_RETRY_DELAYS = (0.0, 0.005, 0.01)
_WINDOWS = os.name == "nt"
# Sidecar every reap in a directory locks (kernel lock: a crashed holder frees it); minted by the
# first reap, so it only ever appears where agrep reaps, and removed only with its directory.
SLOT_MUTEX_NAME = ".owner-mutex"
_SLOT_MUTEX_WAIT_S = 2.0
_SLOT_MUTEX_POLL_S = 0.002


def _touch_windows_fd(fd: int) -> None:
    import ctypes
    import msvcrt

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", ctypes.c_uint32),
            ("high", ctypes.c_uint32),
        ]

    handle = msvcrt.get_osfhandle(fd)
    if handle == -1:
        raise OSError(errno.EBADF, f"invalid retained owner descriptor: {fd}")
    ticks = time.time_ns() // 100 + 116_444_736_000_000_000
    modified = FileTime(ticks & 0xFFFFFFFF, ticks >> 32)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
    ]
    set_file_time.restype = ctypes.c_int
    if not set_file_time(
            ctypes.c_void_p(handle), None, None, ctypes.byref(modified)):
        code = ctypes.get_last_error()
        raise OSError(
            code, f"SetFileTime failed for retained owner descriptor: {fd}")


def _touch_retained_fd(fd: int) -> None:
    if _WINDOWS:
        _touch_windows_fd(fd)
        return
    if os.utime in os.supports_fd:
        os.utime(fd, None)
        return
    unsupported = getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)
    raise OSError(
        unsupported, "descriptor mtime refresh is unsupported on this platform")


class OwnershipLost(OSError):
    """The created directory entry no longer names the record that was written."""


class ProcessOwner(Enum):
    EXACT_LIVE = "exact-live"
    DEAD = "dead"
    REUSED = "reused"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Snapshot:
    identity: tuple[int, int, int, int]
    mtime: float
    raw: bytes


@dataclass
class Handle:
    path: Path
    snapshot: Snapshot
    fd: int | None = None
    _released: bool = False
    # registered in _HELD_TOKENS by create_held; forgotten by release, whatever its outcome
    token: str | None = None

    def close(self) -> None:
        fd = self.fd
        self.fd = None
        if fd is not None:
            os.close(fd)

    def verify(self, *, require_stable_mtime: bool = False) -> Snapshot:
        if self._released:
            raise OwnershipLost(f"owner record handle was released: {self.path}")
        observed = _retry_snapshot(self.path)
        if observed is None and self.fd is not None:
            try:
                observed = _snapshot_retained_entry(
                    self.path, self.fd, self.snapshot.identity, self.snapshot.raw)
            except OSError:
                observed = None
        if observed is None:
            raise OwnershipLost(f"owner record cannot be verified: {self.path}")
        if not _same_record(
                observed, self.snapshot,
                require_stable_mtime=require_stable_mtime):
            raise OwnershipLost(f"owner record ownership changed: {self.path}")
        return observed

    def touch(self) -> Snapshot:
        """Refresh one retained owner's diagnostic mtime without changing bytes."""
        if self.fd is None:
            raise OwnershipLost(f"owner record descriptor is not retained: {self.path}")
        self.verify()
        _touch_retained_fd(self.fd)
        observed = self.verify()
        self.snapshot = observed
        return observed

    def release(self, *, tombstone: bool = True,
                require_stable_mtime: bool = False) -> bool:
        if self._released:
            return False
        self._released = True
        try:
            self.close()
            tomb_path = _new_tomb_path(self.path) if tombstone else None
            for delay in _TRANSIENT_RETRY_DELAYS:
                if delay:
                    time.sleep(delay)
                if remove_exact(
                        self.path, self.snapshot, tombstone=tombstone,
                        require_stable_mtime=require_stable_mtime,
                        _tomb_path=tomb_path, mint_mutex=False):
                    return True
            return False
        finally:
            forget_held(self.token)


# tokens of the records this process's live holders own; a record naming this process under a
# token that is not here was stranded by a release that could not finish, and only this
# process may reclaim it (every other process sees a live holder)
_HELD_TOKENS: set[str] = set()
_HELD_TOKENS_LOCK = threading.Lock()


def create_held(path: Path, raw: bytes, *, token: str, **kwargs: object) -> Handle:
    """`create_exclusive` for a record this process holds for a while and may re-acquire
    later. The token is registered before the create, so a sibling thread never judges the
    fresh record a stranded one of its own; it stays registered until the handle's release."""
    with _HELD_TOKENS_LOCK:
        _HELD_TOKENS.add(token)
    try:
        handle = create_exclusive(path, raw, **kwargs)
    except BaseException:
        forget_held(token)
        raise
    handle.token = token
    return handle


def forget_held(token: str | None) -> None:
    if token is not None:
        with _HELD_TOKENS_LOCK:
            _HELD_TOKENS.discard(token)


def stranded_own_record(
        pid: object, start: object, token: object, *,
        process_start: Callable[[int], object | None]) -> bool:
    """A record naming this process (its pid and verifiable start identity) under a token no
    live holder of this process registered: stranded by a release that could not finish.
    Only this process may reclaim it; every other process sees a live holder."""
    if type(pid) is not int or pid != os.getpid() or not isinstance(token, str):
        return False
    own_start = process_start(pid)
    if own_start in (None, "") or str(own_start) in _DEFAULT_UNVERIFIABLE_STARTS:
        return False
    if str(own_start) != str(start):
        return False
    with _HELD_TOKENS_LOCK:
        return token not in _HELD_TOKENS


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9))),
    )


def _plain_entry(path: Path) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        raise OSError(f"owner record is not a plain regular file: {path}")
    return info


def snapshot(path: Path, *, max_bytes: int = _MAX_RECORD_BYTES) -> Snapshot:
    entry_before = _plain_entry(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0))
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"owner record is not a regular file: {path}")
        if _identity(entry_before) != _identity(before):
            raise OwnershipLost(f"owner record changed before reading: {path}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise OSError(f"owner record exceeds {max_bytes} bytes: {path}")
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise OwnershipLost(f"owner record changed while reading: {path}")
        if _identity(_plain_entry(path)) != _identity(after):
            raise OwnershipLost(f"owner record path changed while reading: {path}")
        return Snapshot(_identity(after), float(after.st_mtime), b"".join(chunks))
    finally:
        os.close(fd)


def _snapshot_retained_entry(
        path: Path, fd: int, created: tuple[int, int, int, int],
        raw: bytes) -> Snapshot:
    if len(raw) > _MAX_RECORD_BYTES:
        raise OwnershipLost(f"owner record exceeds {_MAX_RECORD_BYTES} bytes: {path}")
    info = None
    last_error = None
    for delay in _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            info = _plain_entry(path)
            break
        except OSError as exc:
            last_error = exc
    if info is None:
        raise OwnershipLost(f"owner record cannot be verified: {path}") from last_error
    identity = _identity(info)
    if (identity[1] == 0
            or identity[:3] != created[:3]):
        raise OwnershipLost(f"owner record was replaced after creation: {path}")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining = len(raw) + 1
    while remaining:
        chunk = os.read(fd, min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if b"".join(chunks) != raw:
        raise OwnershipLost(f"owner record changed after creation: {path}")
    if _identity(_plain_entry(path)) != identity:
        raise OwnershipLost(f"owner record path changed after creation: {path}")
    return Snapshot(identity, float(info.st_mtime), raw)


def create_exclusive(path: Path, raw: bytes, *, mode: int = 0o600,
                     fsync: bool = False, retain_fd: bool = False,
                     verify: bool = True, exact_mode: bool = False) -> Handle:
    if not isinstance(raw, bytes):
        raise TypeError("owner record must be bytes")
    if len(raw) > _MAX_RECORD_BYTES:
        raise ValueError(
            f"owner record exceeds {_MAX_RECORD_BYTES} bytes: {path}")
    flags = os.O_CREAT | os.O_EXCL
    flags |= os.O_RDWR if (verify or retain_fd) else os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as create_error:
        try:
            path.lstat()
        except FileNotFoundError:
            raise create_error
        except OSError:
            # Windows: a record a reader still holds open stays delete-pending, which refuses
            # both the exclusive create and the stat; the slot clears when the reader closes.
            if not (_WINDOWS and isinstance(create_error, PermissionError)):
                raise create_error
        raise FileExistsError(
            errno.EEXIST, os.strerror(errno.EEXIST), path) from create_error
    created = None
    written = 0
    try:
        created = _identity(os.fstat(fd))
        if exact_mode and not _WINDOWS:
            os.fchmod(fd, mode)
        view = memoryview(raw)
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError(f"short write creating owner record: {path}")
            written += count
        if fsync:
            os.fsync(fd)
        created_info = os.fstat(fd)
        created = _identity(created_info)
        observed = Snapshot(created, float(created_info.st_mtime), raw)
        if verify:
            try:
                observed = snapshot(path)
            except OSError:
                try:
                    observed = _snapshot_retained_entry(path, fd, created, raw)
                except OSError as fallback_exc:
                    raise OwnershipLost(
                        f"owner record disappeared after creation: {path}"
                    ) from fallback_exc
            if observed.identity[:3] != created[:3] or observed.raw != raw:
                raise OwnershipLost(f"owner record was replaced after creation: {path}")
        if not retain_fd:
            close_fd = fd
            fd = -1
            os.close(close_fd)
        return Handle(path=path, snapshot=observed, fd=fd if retain_fd else None)
    except BaseException:
        if fd >= 0:
            try:
                created = _identity(os.fstat(fd))
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if created is not None:
            try:
                partial = snapshot(path)
                same_entry = partial.identity[:3] == created[:3]
                if same_entry and partial.raw == raw[:written]:
                    remove_exact(path, partial, tombstone=False, mint_mutex=False)
            except OSError:
                pass
        raise


def classify_process(pid: int, expected_start: object,
                     *, pid_alive: Callable[[int], bool],
                     process_start: Callable[[int], object | None],
                     unverifiable_starts: frozenset[str] = (
                         _DEFAULT_UNVERIFIABLE_STARTS)) -> ProcessOwner:
    try:
        if pid <= 0 or not pid_alive(pid):
            return ProcessOwner.DEAD
    except (OSError, OverflowError, ValueError):
        return ProcessOwner.UNVERIFIABLE
    if expected_start is None:
        return ProcessOwner.UNVERIFIABLE
    expected = str(expected_start or "")
    if expected in unverifiable_starts:
        return ProcessOwner.UNVERIFIABLE
    try:
        actual = process_start(pid)
    except (OSError, OverflowError, ValueError):
        return ProcessOwner.UNVERIFIABLE
    if actual is None:
        return ProcessOwner.UNVERIFIABLE
    return (ProcessOwner.EXACT_LIVE
            if str(actual) == expected else ProcessOwner.REUSED)


def _same_record(left: Snapshot, right: Snapshot, *,
                 require_stable_mtime: bool = False) -> bool:
    width = 4 if require_stable_mtime else 3
    return left.identity[:width] == right.identity[:width] and left.raw == right.raw


def _retry_snapshot(path: Path) -> Snapshot | None:
    for delay in _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return snapshot(path)
        except OSError:
            pass
    return None


def _retry_unlink(path: Path) -> bool:
    for delay in _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            pass
    return False


def _retry_replace(source: Path, target: Path) -> bool:
    for delay in _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            os.replace(source, target)
            return True
        except OSError:
            pass
    return False


class _SlotMutex:
    """The directory's removal mutex: `held`, `busy` (a live holder outlasted the wait),
    `unsupported` (the store cannot lock, so the lock-less tomb protocol must stand in) or
    `absent` (not minted here, and this caller must not mint it)."""

    def __init__(self, directory: Path, *, mint: bool = True) -> None:
        self.state = "unsupported"
        self._fd: int | None = None
        flags = os.O_RDWR | (os.O_CREAT if mint else 0)
        flags |= (getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                  | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                  | getattr(os, "O_NOINHERIT", 0))
        try:
            fd = os.open(directory / SLOT_MUTEX_NAME, flags, 0o600)
        except FileNotFoundError:
            if not mint:
                self.state = "absent"
            return
        except OSError:
            return
        try:
            if _own_regular_file(os.fstat(fd)):
                self.state = _lock_descriptor(fd)
        finally:
            if self.state == "held":
                self._fd = fd
            else:
                os.close(fd)

    def __enter__(self) -> "_SlotMutex":
        return self

    def __exit__(self, *_exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                _unlock_descriptor(fd)
            finally:
                os.close(fd)


def _own_regular_file(info: os.stat_result) -> bool:
    # a planted sidecar another user can lock would only ever report busy: never trust one
    if not stat.S_ISREG(info.st_mode):
        return False
    owner_fn = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    return owner_fn is None or info.st_uid == owner_fn()


def _poll_lock(try_lock: Callable[[], str | None]) -> str:
    """Poll a non-blocking lock attempt for up to the wait; `None` means it would block.
    The clock is read only once the first attempt blocked: an uncontended lock costs
    one syscall and leaves the caller's own pacing alone."""
    deadline = None
    while True:
        state = try_lock()
        if state is not None:
            return state
        now = time.monotonic()
        if deadline is None:
            deadline = now + _SLOT_MUTEX_WAIT_S
        elif now >= deadline:
            return "busy"
        time.sleep(_SLOT_MUTEX_POLL_S)


if _WINDOWS:
    import msvcrt

    def _lock_descriptor(fd: int) -> str:
        def try_lock() -> str | None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return "held"
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EDEADLOCK, errno.EAGAIN):
                    return None
                return "unsupported"

        return _poll_lock(try_lock)

    def _unlock_descriptor(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_descriptor(fd: int) -> str:
        def try_lock() -> str | None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return "held"
            except BlockingIOError:
                return None
            except OSError:
                return "unsupported"

        return _poll_lock(try_lock)

    def _unlock_descriptor(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _restore_tomb(path: Path, tomb: Path) -> None:
    # A tomb is linked back only into a vacant slot: a record that published into the gap is
    # never overwritten, and an unprovable tomb is left as litter.
    restored = False
    for delay in _TRANSIENT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            os.link(tomb, path, follow_symlinks=False)
            restored = True
            break
        except FileExistsError:
            break
        except OSError:
            pass
    if restored:
        _retry_unlink(tomb)


def _new_tomb_path(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.owner-reap-{os.getpid()}-{secrets.token_hex(8)}")


def _entry_exists(path: Path) -> bool | None:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def remove_exact(path: Path, expected: Snapshot, *, tombstone: bool = True,
                 require_stable_mtime: bool = False,
                 _tomb_path: Path | None = None, mint_mutex: bool = True) -> bool:
    """Remove the record at `path` only while it is still exactly `expected`.

    A reap (the caller judged another process's record dead) takes the
    directory's slot mutex, minting it on first use, so the check and the
    removal are one step relative to every other remover: a live record swapped
    in since the caller's judgement is never touched. Creators need no lock;
    they only ever create into an empty slot. The record is renamed aside and
    the tomb unlinked, so the slot is free at once even where a reader still
    holds the record open (Windows). Where the store cannot lock, the same tomb
    protocol runs lock-less. `mint_mutex=False` is for a record this process
    owns (a release, a failed create's cleanup): it honours a mutex a reap
    minted and never mints one, so a directory only ever sees one once
    something was reaped there.
    """
    if _tomb_path is not None and _entry_exists(_tomb_path) is not False:
        return _remove_via_tomb(
            path, expected, tombstone=tombstone,
            require_stable_mtime=require_stable_mtime, tomb=_tomb_path)
    try:
        if not _same_record(
                snapshot(path), expected,
                require_stable_mtime=require_stable_mtime):
            return False
    except OSError:
        return False
    with _slot_mutex(path, mint=mint_mutex) as mutex:
        if mutex.state == "busy":
            return False
        if mutex.state == "held":
            try:
                current = snapshot(path)
            except OSError:
                return False
            if not _same_record(
                    current, expected,
                    require_stable_mtime=require_stable_mtime):
                return False
        return _remove_via_tomb(
            path, expected, tombstone=tombstone,
            require_stable_mtime=require_stable_mtime, tomb=_tomb_path)


def _slot_mutex(path: Path, *, mint: bool = True) -> _SlotMutex:
    return _SlotMutex(path.parent, mint=mint)


def remove_record_dir(path: Path) -> bool:
    """rmdir a directory holding nothing but its removal mutex: a lone sidecar counts as empty.

    The sidecar goes under its own lock right before the rmdir, when no remover is active in
    the directory; should the rmdir still fail, the next reap mints a fresh one."""
    sidecar = path / SLOT_MUTEX_NAME
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    if entries == [SLOT_MUTEX_NAME]:
        with _slot_mutex(sidecar, mint=False) as mutex:
            if mutex.state == "busy":
                return False
            try:
                if (mutex.state == "held"
                        and os.listdir(path) != [SLOT_MUTEX_NAME]):
                    return False
                sidecar.unlink()
            except OSError:
                return False
    elif entries:
        return False
    try:
        os.rmdir(path)
    except OSError:
        return False
    return True


def _remove_via_tomb(path: Path, expected: Snapshot, *, tombstone: bool,
                     require_stable_mtime: bool, tomb: Path | None) -> bool:
    tomb_state = _entry_exists(tomb) if tombstone and tomb is not None else False
    if tomb_state is None:
        return False
    if tomb_state:
        moved = _retry_snapshot(tomb)
        if (moved is None
                or not _same_record(
                    moved, expected,
                    require_stable_mtime=require_stable_mtime)):
            _restore_tomb(path, tomb)
            return False
        if not _retry_unlink(tomb):
            _restore_tomb(path, tomb)
            return False
        current = _retry_snapshot(path)
        if current is None:
            return _entry_exists(path) is False
        if not _same_record(
                current, expected,
                require_stable_mtime=require_stable_mtime):
            return True
    try:
        if not _same_record(
                snapshot(path), expected,
                require_stable_mtime=require_stable_mtime):
            return False
    except OSError:
        return False
    if not tombstone:
        return _retry_unlink(path)
    if tomb is None:
        tomb = _new_tomb_path(path)
    if not _retry_replace(path, tomb):
        return False
    observed = _retry_snapshot(tomb)
    if (observed is not None
            and _same_record(
                observed, expected,
                require_stable_mtime=require_stable_mtime)):
        if _retry_unlink(tomb):
            return True
    _restore_tomb(path, tomb)
    return False
