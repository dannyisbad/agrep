"""Event-store reader: filenames, verified stamps, generation snapshots, bounded reads.

Rust ingest publishes one capped event BLOB per session into
``events/.store.sqlite3`` behind the ``.generation`` marker; everything here
reads that store (or the pre-migration per-session JSONL files) without ever
trusting a torn write. This module also resolves the shared data dir: the event
paths derive from it, and every layer above imports the result from here.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

import fileops
from proc import WIN


def _user_data_dir() -> Path:
    """Per-OS writable home for the index when agrep is installed as a package.
    Stdlib only (no platformdirs dep): XDG on linux, Application Support on mac,
    LOCALAPPDATA on windows."""
    if WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agrep"


# $AGREP_DATA_DIR wins (wheel launcher, test/dev fixtures); else one shared per-user dir.
_env_data = os.environ.get("AGREP_DATA_DIR")
_env_data_source = os.environ.get("AGREP_DATA_DIR_SOURCE")
DATA_DIR_SOURCE = _env_data_source if _env_data_source in ("default", "env") else (
    "env" if _env_data else "default"
)
DEFAULT_DATA_DIR = _user_data_dir()
if _env_data:
    configured = Path(_env_data).expanduser()
    if configured.is_absolute():
        DATA_DIR = configured
    else:
        try:
            startup_dir = Path.cwd()
        except OSError as exc:
            raise OSError(
                "relative AGREP_DATA_DIR cannot be resolved because the startup "
                "working directory is unavailable; use an absolute AGREP_DATA_DIR "
                "or start agrep from an existing directory"
            ) from exc
        DATA_DIR = (startup_dir / configured).resolve(strict=False)
else:
    DATA_DIR = DEFAULT_DATA_DIR


def data_dir_readonly(path: Path | None = None) -> bool:
    """Import-safe form of the exact gate guard.

    events owns data-dir resolution and cannot import indexd_runtime without
    creating a cycle, so it must enforce the same boundary before its historic
    import-time mkdir/chmod.
    """
    path = DATA_DIR if path is None else path
    protected = os.environ.get("AGREP_DATA_READONLY")
    if not protected:
        return False
    try:
        return (
            os.path.normcase(os.path.realpath(protected))
            == os.path.normcase(os.path.realpath(os.fspath(path)))
        )
    except OSError:
        return False


_DATA_DIR_PROTECTED = data_dir_readonly(DATA_DIR)


def _prepare_data_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise OSError(f"data directory must be a real directory, not a symlink: {path}")
    if WIN:
        return
    owner = getattr(os, "geteuid", lambda: before.st_uid)()
    if before.st_uid != owner:
        raise PermissionError(f"data directory is not owned by the current user: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISDIR(opened.st_mode)):
            raise OSError(f"data directory changed while being secured: {path}")
        mode = stat.S_IMODE(opened.st_mode)
        if mode & 0o077:
            os.fchmod(fd, mode & ~0o077)
        if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            raise PermissionError(f"data directory permissions remain public: {path}")
    finally:
        os.close(fd)


if not _DATA_DIR_PROTECTED:
    _prepare_data_dir(DATA_DIR)
# Export the resolved dir so every child (Rust ingest, reindex, resident workers) agrees;
# without it the binary falls back to cwd-relative ./data and splits the world.
os.environ["AGREP_DATA_DIR"] = str(DATA_DIR)
os.environ["AGREP_DATA_DIR_SOURCE"] = DATA_DIR_SOURCE

EVENTS_DIR = DATA_DIR / "events"
EVENT_STORE_NAME = ".store.sqlite3"
EVENT_GENERATION_NAME = ".generation"
EVENT_PROOF_VERSION = 10

# Snapshot copies live in system temp under a pid-stamped name so any later
# process can prove an orphan's owner is dead before reclaiming it. A killed
# reader cannot clean up after itself; the next snapshotting process does.
_SNAPSHOT_DIR_PREFIX = "agrep-event-snapshot-"
_SNAPSHOT_SWEEP_DONE = False
_SNAPSHOT_LEGACY_REAP_AGE_S = 3600.0
_EVENT_REPAIR_REQUESTED = False
_EVENT_REPAIR_CALLBACK: Callable[[], bool] | None = None


def _sweep_dead_snapshot_dirs() -> None:
    """Reap snapshot dirs whose owning process is gone (once per process).

    Windows cannot delete files another process holds open, so a snapshot
    whose reader was killed outlives it in temp - at hundreds of MB per
    copied store that filled a disk once. Pid-stamped dirs are reclaimed the
    moment their owner is conclusively dead. Pre-stamp dirs carry no owner
    to verify, so they are reaped only on Windows, where a live reader's
    open handles make its files undeletable; on POSIX an unlink under a
    live WAL reader breaks its next page read, so ownerless dirs are left
    alone (a bounded one-time population from pre-stamp builds).
    """
    global _SNAPSHOT_SWEEP_DONE
    if _SNAPSHOT_SWEEP_DONE:
        return
    _SNAPSHOT_SWEEP_DONE = True
    try:
        from proc import pid_alive
        now = time.time()
        with os.scandir(tempfile.gettempdir()) as entries:
            candidates = [
                entry for entry in entries
                if entry.name.startswith(_SNAPSHOT_DIR_PREFIX)
                and entry.is_dir(follow_symlinks=False)
            ]
        for entry in candidates:
            token = entry.name[len(_SNAPSHOT_DIR_PREFIX):].split("-", 1)[0]
            if token.isdigit():
                if int(token) == os.getpid() or pid_alive(int(token)):
                    continue
            else:
                try:
                    if os.name != "nt" or now - entry.stat(
                            follow_symlinks=False,
                    ).st_mtime < _SNAPSHOT_LEGACY_REAP_AGE_S:
                        continue
                except OSError:
                    continue
            shutil.rmtree(entry.path, ignore_errors=True)
    except Exception:  # noqa: BLE001 -- reclaim must never fail a reader
        pass


def _safe_session(s: str) -> str:
    """Legacy event filename mapping, retained only while old stores migrate."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_.") else "_" for c in s)


def _readable_event_name(value: str, limit: int) -> str:
    name = "".join(
        c if c.isascii() and (c.isalnum() or c in "-_.") else "_"
        for c in value
    )[:limit]
    return name or "session"


def _event_identity(agent: str, session: str) -> str:
    import hashlib

    raw = agent.encode() + b"\0" + session.encode()
    value = 0xCBF29CE484222325
    for byte in b"\1" + raw:
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{hashlib.md5(raw).hexdigest()}{value:016x}"


EVENT_FILENAME_MAX_BYTES = 117


def event_filename(agent: str, session: str) -> str:
    """Collision-safe event filename shared with cache.rs."""
    return (
        f"{_readable_event_name(agent, 20)}-"
        f"{_readable_event_name(session, 40)}--{_event_identity(agent, session)}.jsonl"
    )


def event_path_candidates(agent: str, session: str) -> tuple[Path, ...]:
    """Current path first, then the pre-hash migration path."""
    current = EVENTS_DIR / event_filename(agent, session)
    legacy = EVENTS_DIR / f"{_safe_session(agent)}-{_safe_session(session)}.jsonl"
    return (current,) if current == legacy else (current, legacy)


_EVENT_READER_LOCAL = threading.local()


class _EventSnapshotConnection(sqlite3.Connection):
    _snapshot_cleanup = None
    _snapshot_source = None
    _snapshot_stamp = None

    def source_stable(self) -> bool:
        try:
            return (
                self._snapshot_source is not None
                and _event_store_stamp(self._snapshot_source)
                == self._snapshot_stamp
            )
        except OSError:
            return False

    def close(self):
        cleanup, self._snapshot_cleanup = self._snapshot_cleanup, None
        self._snapshot_source = None
        self._snapshot_stamp = None
        try:
            return super().close()
        finally:
            if cleanup is not None:
                cleanup.cleanup()


def _close_event_reader() -> None:
    """Release this thread's cached event DB handle."""
    state = getattr(_EVENT_READER_LOCAL, "state", None)
    if state is not None:
        try:
            state["connection"].close()
        except Exception:  # noqa: BLE001 -- cleanup must tolerate a broken SQLite handle
            pass
    _EVENT_READER_LOCAL.state = None


def _event_is_regular(metadata) -> bool:
    import stat

    return (stat.S_ISREG(metadata.st_mode)
            and not getattr(metadata, "st_file_attributes", 0) & 0x400)


def _event_metadata_stamp(metadata) -> tuple[int, int, int, int, int]:
    if not _event_is_regular(metadata):
        raise OSError("event path is not a regular file")
    return (metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size,
            metadata.st_dev, metadata.st_ino)


def _event_identity_stamp(
        identity: fileops.FileIdentity) -> tuple[int, int, int, int, int]:
    device, inode, size, modified, changed = identity
    # Preserve the historic stamp layout: callers use element two as size.
    return modified, changed, size, device, inode


def _event_file_stamp(path: Path) -> tuple[int, int, int, int, int]:
    # fileops opens without following links and supplies Win32 ChangeTime in
    # field five. Windows stat().st_ctime is creation time on older supported
    # Pythons and cannot fence a same-size rewrite with restored mtime.
    return _event_identity_stamp(fileops.file_identity(path))


def _event_fd_stamp(fd: int) -> tuple[int, int, int, int, int]:
    return _event_identity_stamp(fileops.file_identity_fd(fd))


def _event_store_stamp(store: Path) -> tuple:
    stamps = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{store}{suffix}")
        try:
            stamps.append((suffix, _event_file_stamp(path)))
        except FileNotFoundError:
            if not suffix:
                raise
    return tuple(stamps)


def _event_payload_hash(payload: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in payload:
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _event_payload_digest(payload: bytes) -> bytes:
    import hashlib

    return hashlib.md5(payload).digest()


def _read_regular_bytes(path: Path) -> bytes:
    before = fileops.file_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = fileops.file_identity_fd(descriptor)
        if opened != before:
            raise OSError(f"event path changed before opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            body = source.read()
        after_open = fileops.file_identity_fd(descriptor)
    finally:
        os.close(descriptor)
    after = fileops.file_identity(path)
    if opened != after_open or after_open != after:
        raise OSError(f"event path changed while reading: {path}")
    return body


def open_regular_binary(path: Path):
    """Open one stable regular file without following symlinks where the OS supports it."""
    import contextlib
    @contextlib.contextmanager
    def opened():
        before = fileops.file_identity(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        source = os.fdopen(descriptor, "rb")
        try:
            opened_identity = fileops.file_identity_fd(source.fileno())
            if opened_identity != before:
                raise OSError(f"event path changed before opening: {path}")
            yield source
            after_open = fileops.file_identity_fd(source.fileno())
        finally:
            source.close()
        after = fileops.file_identity(path)
        if opened_identity != after_open or after_open != after:
            raise OSError(f"event path changed while reading: {path}")

    return opened()


def _event_generation_token(manifest: bytes) -> bytes:
    return f"{_event_payload_hash(manifest):016x}:{len(manifest)}".encode()


def _event_generation_snapshot(state: dict, marker: Path) -> tuple[tuple, bytes]:
    for _attempt in range(2):
        before = _event_file_stamp(marker)
        if state.get("generation_stamp") == before:
            return before, state["generation"]
        body = _read_regular_bytes(marker)
        if _event_file_stamp(marker) == before:
            state["generation_stamp"] = before
            state["generation"] = body
            return before, body
    raise OSError("event generation changed while reading")


def _event_seal_identity(stamp: tuple[int, int, int, int, int]) -> dict[str, int]:
    modified, changed, size, device, inode = stamp
    return {
        "device": int(device), "inode": int(inode), "size": int(size),
        "modified_ns": int(modified), "changed_ns": int(changed),
    }


def _event_proof_path(agent: str) -> Path:
    if (not agent or len(agent) > 64
            or re.fullmatch(r"[A-Za-z0-9_.-]+", agent) is None):
        raise ValueError("event proof has an unsafe agent name")
    return EVENTS_DIR.parent / f".events_complete.{agent}.json"


def _event_proof_integer(value) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError("event proof contains an invalid integer")
    return value


def _event_proof_identity(value) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("event proof contains an invalid file identity")
    fields = ("device", "inode", "size", "modified_ns", "changed_ns")
    if set(value) != set(fields):
        raise ValueError("event proof contains an incomplete file identity")
    return {field: _event_proof_integer(value[field]) for field in fields}


def _event_proof_generation(value) -> bytes:
    if (not isinstance(value, list) or len(value) > 4096
            or any(type(item) is not int or not 0 <= item <= 255 for item in value)):
        raise ValueError("event proof contains an invalid generation")
    return bytes(value)


def _read_event_proof(agent: str) -> dict:
    path = _event_proof_path(agent)
    before = fileops.file_identity(path)
    body = _read_regular_bytes(path)
    if len(body) > 256 * 1024:
        raise ValueError("event proof exceeds its size limit")
    proof = json.loads(body)
    if not isinstance(proof, dict):
        raise ValueError("event proof is not an object")
    if proof.get("version") != EVENT_PROOF_VERSION or proof.get("agents") != [agent]:
        raise ValueError("event proof has the wrong authority")
    parsed = {
        "store": _event_proof_identity(proof.get("store")),
        "wal": (None if proof.get("wal") is None
                else _event_proof_identity(proof.get("wal"))),
        "generation": _event_proof_identity(proof.get("generation")),
        "generation_value": _event_proof_generation(proof.get("generation_value")),
    }
    for field in (
            "inventory_hash", "inventory_hash_b", "inventory_count",
            "stats_hash", "stats_hash_b"):
        parsed[field] = _event_proof_integer(proof.get(field))
    parsed["path"] = path
    parsed["file_identity"] = before
    parsed["body"] = body
    return parsed


def _event_inventory_authority(connection) -> dict[str, tuple[int, int, int]]:
    mask = 0xFFFF_FFFF_FFFF_FFFF
    rows = connection.execute(
        "SELECT agent,row_count,root_a,root_b FROM event_agent_state")
    return {
        str(agent): (int(count), int(root_a) & mask, int(root_b) & mask)
        for agent, count, root_a, root_b in rows
    }


def _event_filter_authority(
        connection, expected_agents: set[str], store_stamp: tuple,
        generation_stamp: tuple, generation_value: bytes) -> dict | None:
    try:
        family = dict(store_stamp)
        store_identity = _event_seal_identity(family[""])
        wal_identity = (
            None if family.get("-wal") is None
            else _event_seal_identity(family["-wal"]))
        generation_identity = _event_seal_identity(generation_stamp)
        inventory = _event_inventory_authority(connection)
        represented = {
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT agent FROM event_sessions")
        }
        agents = represented | set(inventory) | expected_agents
        proofs = {}
        for agent in sorted(agents):
            proof = _read_event_proof(agent)
            if (proof["store"] != store_identity
                    or proof["wal"] != wal_identity
                    or proof["generation"] != generation_identity
                    or proof["generation_value"] != generation_value):
                return None
            count, root_a, root_b = inventory.get(agent, (0, 0, 0))
            if (proof["inventory_count"] != count
                    or proof["inventory_hash"] != root_a
                    or proof["inventory_hash_b"] != root_b):
                return None
            proofs[agent] = proof
        return {
            "store_stamp": store_stamp,
            "generation_stamp": generation_stamp,
            "generation_value": generation_value,
            "proofs": proofs,
        }
    except (FileNotFoundError, OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _event_filter_authority_stable(
        authority: dict, connection, store: Path, marker: Path) -> bool:
    try:
        if (not connection.source_stable()
                or _event_store_stamp(store) != authority["store_stamp"]
                or _event_file_stamp(marker) != authority["generation_stamp"]
                or _read_regular_bytes(marker) != authority["generation_value"]):
            return False
        for proof in authority["proofs"].values():
            if (fileops.file_identity(proof["path"]) != proof["file_identity"]
                    or _read_regular_bytes(proof["path"]) != proof["body"]):
                return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def set_event_repair_callback(callback: Callable[[], bool]) -> None:
    """Install the higher-layer repair scheduler without reversing imports."""
    global _EVENT_REPAIR_CALLBACK
    _EVENT_REPAIR_CALLBACK = callback


def _request_event_repair() -> None:
    global _EVENT_REPAIR_REQUESTED
    if (_EVENT_REPAIR_REQUESTED or EVENTS_DIR != DATA_DIR / "events"
            or data_dir_readonly(DATA_DIR) or _EVENT_REPAIR_CALLBACK is None):
        return
    try:
        if _EVENT_REPAIR_CALLBACK():
            _EVENT_REPAIR_REQUESTED = True
    except Exception:  # noqa: BLE001 -- repair scheduling cannot fail a reader
        pass


def _open_verified_event_store(
        store: Path, expected_stamp: tuple, timeout_s: float = 1.0,
):
    try:
        expected = dict(expected_stamp)
        structured_stamp = (
            len(expected) == len(expected_stamp)
            and "" in expected
            and all(
                suffix in {"", "-wal", "-shm", "-journal"}
                and isinstance(stamp, tuple) and len(stamp) == 5
                for suffix, stamp in expected_stamp
            )
        )
    except (TypeError, ValueError):
        expected = {}
        structured_stamp = False
    journal = expected.get("-journal")
    if structured_stamp and journal is not None and journal[2] != 0:
        raise OSError("event store has a live rollback journal")

    # A checkpointed DELETE publication needs no WAL shared-memory sidecar and
    # stays direct. Copy every other publication to a system-temp main+WAL
    # snapshot; never hardlink or create aliases beside the canonical store.
    header = b""
    with open_regular_binary(store) as source:
        header = source.read(20)
    wal = expected.get("-wal")
    direct = (
        not structured_stamp
        or (
            len(header) == 20
            and header[:16] == b"SQLite format 3\0"
            and header[18:20] == b"\x01\x01"
            and (wal is None or wal[2] == 0)
        )
    )
    cleanup = None
    alias = store
    if not direct:
        _sweep_dead_snapshot_dirs()
        cleanup = tempfile.TemporaryDirectory(
            prefix=f"{_SNAPSHOT_DIR_PREFIX}{os.getpid()}-",
            ignore_cleanup_errors=True)
        alias = Path(cleanup.name) / store.name
        try:
            for suffix in ("", "-wal"):
                identity = expected.get(suffix)
                if identity is None or (suffix and identity[2] == 0):
                    continue
                source_path = Path(f"{store}{suffix}")
                target_path = Path(f"{alias}{suffix}")
                with open_regular_binary(source_path) as source, \
                        target_path.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            if _event_store_stamp(store) != expected_stamp:
                raise OSError("event store changed while snapshotting")
        except BaseException:
            cleanup.cleanup()
            raise

    connection = None
    try:
        connection = sqlite3.connect(
            f"{alias.absolute().as_uri()}?mode=ro", uri=True,
            timeout=max(0.0, timeout_s), factory=_EventSnapshotConnection)
        if _event_store_stamp(store) != expected_stamp:
            raise OSError("event store changed while opening")
        connection._snapshot_source = store
        connection._snapshot_stamp = expected_stamp
        if cleanup is not None:
            connection._snapshot_cleanup = cleanup
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        if cleanup is not None:
            cleanup.cleanup()
        raise


def open_sqlite_snapshot(
        path: Path, timeout_s: float = 0,
) -> sqlite3.Connection:
    """Open any SQLite family without creating a sidecar beside its source."""
    path = Path(path)
    stamp = _event_store_stamp(path)
    return _open_verified_event_store(path, stamp, timeout_s)


def _event_reader_connection(store: Path):
    import sqlite3

    store_stamp = _event_store_stamp(store)
    state = getattr(_EVENT_READER_LOCAL, "state", None)
    key = str(store.absolute())
    if (state is not None and state.get("store") == key
            and state.get("store_stamp") == store_stamp):
        return state
    if state is not None:
        try:
            state["connection"].close()
        except sqlite3.Error:
            pass
        _EVENT_READER_LOCAL.state = None
    connection = _open_verified_event_store(store, store_stamp)
    state = {"store": key, "store_stamp": store_stamp, "connection": connection}
    _EVENT_READER_LOCAL.state = state
    return state


_EVENT_LEGACY = 0
_EVENT_CURRENT = 1
_EVENT_BLOCKED = 2


def _event_legacy_allowed(directory: Path) -> bool:
    try:
        (directory / EVENT_GENERATION_NAME).lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _event_store_lookup(agent: str, session: str) -> tuple[int, bytes | None]:
    """Return migration/current/blocked status and one generation-stable DB row."""
    import sqlite3

    store = EVENTS_DIR / EVENT_STORE_NAME
    marker = EVENTS_DIR / EVENT_GENERATION_NAME
    try:
        state = _event_reader_connection(store)
    except FileNotFoundError:
        return (_EVENT_LEGACY if _event_legacy_allowed(EVENTS_DIR)
                else _EVENT_BLOCKED), None
    except (OSError, sqlite3.Error, ValueError):
        return _EVENT_BLOCKED, None
    try:
        for attempt in range(3):
            stamp, expected = _event_generation_snapshot(state, marker)
            row = state["connection"].execute(
                "SELECT m.value, s.digest, s.payload FROM event_meta AS m "
                "LEFT JOIN event_sessions AS s "
                "ON s.name=? "
                "WHERE m.key='generation'",
                (event_filename(agent, session),),
            ).fetchone()
            if _event_file_stamp(marker) != stamp:
                state.pop("generation_stamp", None)
                continue
            if row is None:
                seeded = state["connection"].execute(
                    "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
                legacy = seeded is None and _event_legacy_allowed(EVENTS_DIR)
                return (_EVENT_LEGACY if legacy else _EVENT_BLOCKED), None
            if bytes(row[0]) == expected:
                payload = None if row[2] is None else bytes(row[2])
                if payload is not None:
                    try:
                        valid = _event_payload_digest(payload) == bytes(row[1])
                    except (TypeError, ValueError):
                        valid = False
                    if not valid:
                        return _EVENT_BLOCKED, None
                return _EVENT_CURRENT, payload
            state.pop("generation_stamp", None)
            if attempt < 2:
                time.sleep(0.001)
        _close_event_reader()
        return _EVENT_BLOCKED, None
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower() or "no such column" in str(error).lower():
            try:
                seeded = state["connection"].execute(
                    "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
            except sqlite3.Error:
                seeded = None
            legacy = seeded is None and _event_legacy_allowed(EVENTS_DIR)
            return (_EVENT_LEGACY if legacy else _EVENT_BLOCKED), None
        _close_event_reader()
        return _EVENT_BLOCKED, None
    except OSError:
        try:
            seeded = state["connection"].execute(
                "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
        except sqlite3.Error:
            seeded = (1,)
        if seeded is None and _event_legacy_allowed(EVENTS_DIR):
            return _EVENT_LEGACY, None
        _close_event_reader()
        return _EVENT_BLOCKED, None
    except (sqlite3.Error, TypeError, ValueError):
        state = getattr(_EVENT_READER_LOCAL, "state", None)
        if state is not None and state.get("store") == str(store.absolute()):
            _close_event_reader()
        return _EVENT_BLOCKED, None


def event_store_blob(agent: str, session: str) -> tuple[bool, bytes | None]:
    """Read one DB row, distinguishing a current empty store from migration fallback."""
    status, payload = _event_store_lookup(agent, session)
    return status != _EVENT_LEGACY, payload if status == _EVENT_CURRENT else None


def event_blob(agent: str, session: str) -> bytes | None:
    """Read a session event blob from SQLite, then pre-migration JSONL if needed."""
    status, payload = _event_store_lookup(agent, session)
    if status == _EVENT_CURRENT:
        return payload
    if status == _EVENT_BLOCKED:
        return None
    for path in event_path_candidates(agent, session):
        try:
            return _read_regular_bytes(path)
        except OSError:
            continue
    return None


def event_exists(agent: str, session: str) -> bool:
    """Check one event identity without loading its session BLOB or the global name set."""
    import sqlite3

    store = EVENTS_DIR / EVENT_STORE_NAME
    marker = EVENTS_DIR / EVENT_GENERATION_NAME
    try:
        state = _event_reader_connection(store)
    except FileNotFoundError:
        if not _event_legacy_allowed(EVENTS_DIR):
            return False
        state = None
    except (OSError, sqlite3.Error, ValueError):
        return False
    if state is not None:
        try:
            for attempt in range(3):
                stamp, expected = _event_generation_snapshot(state, marker)
                row = state["connection"].execute(
                    "SELECT m.value, EXISTS(SELECT 1 FROM event_sessions WHERE name=?1) "
                    "FROM event_meta AS m WHERE m.key='generation'",
                    (event_filename(agent, session),),
                ).fetchone()
                if _event_file_stamp(marker) != stamp:
                    state.pop("generation_stamp", None)
                    continue
                if row is not None and bytes(row[0]) == expected:
                    return bool(row[1])
                if row is None:
                    seeded = state["connection"].execute(
                        "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
                    if seeded is None:
                        state = None
                        break
                    return False
                state.pop("generation_stamp", None)
                if attempt < 2:
                    time.sleep(0.001)
            return False
        except (OSError, sqlite3.Error, TypeError, ValueError):
            try:
                seeded = state["connection"].execute(
                    "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
            except sqlite3.Error:
                seeded = (1,)
            if seeded is not None:
                return False
    if not _event_legacy_allowed(EVENTS_DIR):
        return False
    for path in event_path_candidates(agent, session):
        try:
            return _event_is_regular(path.lstat())
        except OSError:
            continue
    return False


def event_names(events_dir: Path | None = None) -> set[str]:
    """Return current event row names, falling back to legacy JSONL files."""
    import sqlite3

    directory = events_dir or EVENTS_DIR
    store = directory / EVENT_STORE_NAME
    marker = directory / EVENT_GENERATION_NAME
    def legacy_names() -> set[str]:
        try:
            return {path.name for path in directory.iterdir()
                    if path.name.endswith(".jsonl")
                    and _event_is_regular(path.lstat())}
        except OSError:
            return set()

    try:
        metadata = store.lstat()
    except FileNotFoundError:
        return legacy_names() if _event_legacy_allowed(directory) else set()
    except OSError:
        return set()
    if not _event_is_regular(metadata):
        return set()
    try:
        store_stamp = _event_store_stamp(store)
        con = _open_verified_event_store(store, store_stamp)
        try:
            state: dict = {}
            for attempt in range(3):
                try:
                    stamp, expected = _event_generation_snapshot(state, marker)
                except FileNotFoundError:
                    seeded = con.execute(
                        "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
                    return set() if seeded else legacy_names()
                con.execute("BEGIN")
                generation = con.execute(
                    "SELECT value FROM event_meta WHERE key='generation'").fetchone()
                if generation is None:
                    seeded = con.execute(
                        "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
                    con.rollback()
                    legacy = seeded is None and _event_legacy_allowed(directory)
                    return legacy_names() if legacy else set()
                names = {str(row[0]) for row in con.execute("SELECT name FROM event_sessions")}
                con.rollback()
                if _event_file_stamp(marker) != stamp:
                    state.pop("generation_stamp", None)
                    continue
                if bytes(generation[0]) == expected:
                    return names
                state.pop("generation_stamp", None)
                if attempt < 2:
                    time.sleep(0.001)
            return set()
        finally:
            con.close()
    except sqlite3.OperationalError as error:
        legacy = "no such table" in str(error).lower() and _event_legacy_allowed(directory)
        return legacy_names() if legacy else set()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return set()


def _event_filterable_literals(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Literals whose canonical match requires the same raw JSON text."""
    return tuple(
        token for token in tokens
        if token and token.isascii() and token.isalnum()
        and token.lower() not in "failed"
    )


def _event_sql_literal_filter(
    tokens: tuple[str, ...],
) -> tuple[str, tuple[str | bytes, ...]]:
    """A reject-safe SQLite filter for canonical UTF-8 JSON event BLOBs.

    Prefer one ordinary run that Python re.I cannot spell with an exceptional
    code point.  If none is selective, one whole-token scan plus its relevant
    i/s/k code points is still a constant-size necessary condition: a match is
    either ASCII-case-equivalent or contains one of those UTF-8 spellings.
    Canonical store rows come from serde_json and therefore keep Unicode as
    UTF-8; the exact BLOB matcher additionally covers escaped legacy JSON.
    """
    tokens = _event_filterable_literals(tokens)
    runs: list[str] = []
    words: list[str] = []
    for token in tokens:
        if not token or not token.isascii() or not token.isalnum():
            continue
        word = token.lower()
        words.append(word)
        current: list[str] = []
        for char in word:
            if char.isalnum() and char not in "isk":
                current.append(char)
                continue
            if current:
                runs.append("".join(current))
                current = []
        if current:
            runs.append("".join(current))
    safe = max(runs, key=lambda value: (len(value), value), default="")
    if len(safe) >= 3:
        return (
            "instr(lower(CAST(payload AS TEXT)), ?) > 0 "
            "OR instr(payload, ?) > 0",
            (safe, b"\\u00"),
        )
    if not words:
        return "", ()
    required = max(words, key=lambda value: (len(value), value))
    if len(required) < 3:
        return "", ()
    exceptional = {
        "i": (b"\xc4\xb0", b"\xc4\xb1"),
        "s": (b"\xc5\xbf",),
        "k": (b"\xe2\x84\xaa",),
    }
    params: list[str | bytes] = [
        required, b"\\u0" if "i" in required or "s" in required else b"\\u00"]
    for char in "isk":
        if char in required:
            params.extend(exceptional[char])
    if "k" in required:
        params.append(b"\\u212")
    clauses = ["instr(lower(CAST(payload AS TEXT)), ?) > 0"]
    clauses.extend("instr(payload, ?) > 0" for _value in params[1:])
    return " OR ".join(clauses), tuple(params)


def event_blobs_bulk(
    keys: Iterable[tuple[str, str]], *, full: bool,
    required_literals: tuple[str, ...] = (),
) -> Iterator[tuple[str, str, bytes]]:
    """Yield one coherent event generation, scanning the DB once on full builds.

    A stable v10 proof authorizes candidate-only reads. Without one, every
    payload is validated before matching rows are replayed.
    """
    import sqlite3

    key_list = list(keys)
    literal_fast = _event_literal_fast_matchers(
        _event_filterable_literals(required_literals))

    def payload_candidate(body: bytes) -> bool:
        return (not literal_fast
                or _event_bytes_maybe_literals(body, literal_fast))

    requested = (None if full else
                 {event_filename(agent, session): (agent, session)
                  for agent, session in key_list})
    requested_keys = key_list if requested is None else requested.values()
    expected_agents = {str(agent) for agent, _session in key_list}
    store = EVENTS_DIR / EVENT_STORE_NAME
    marker = EVENTS_DIR / EVENT_GENERATION_NAME
    try:
        metadata = store.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise RuntimeError("event store metadata is unreadable") from error
    if metadata is None:
        if not _event_legacy_allowed(EVENTS_DIR):
            raise RuntimeError("event store is missing for the published generation")
        for agent, session in requested_keys:
            for path in event_path_candidates(agent, session):
                try:
                    body = _read_regular_bytes(path)
                    if payload_candidate(body):
                        yield agent, session, body
                    break
                except OSError:
                    continue
        return
    if not _event_is_regular(metadata):
        raise RuntimeError("event store is not a regular file")
    try:
        store_stamp = _event_store_stamp(store)
        connection = _open_verified_event_store(store, store_stamp)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise RuntimeError("event store cannot be opened") from error
    pending = None
    pending_rows: list[tuple[tuple[str, str], int, int]] = []
    try:
        state: dict = {}
        coherent = False
        for attempt in range(3):
            try:
                stamp, expected = _event_generation_snapshot(state, marker)
            except OSError:
                try:
                    seeded = connection.execute(
                        "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
                except sqlite3.Error:
                    seeded = (1,)
                if seeded is None and _event_legacy_allowed(EVENTS_DIR):
                    for agent, session in requested_keys:
                        for path in event_path_candidates(agent, session):
                            try:
                                body = _read_regular_bytes(path)
                                if payload_candidate(body):
                                    yield agent, session, body
                                break
                            except OSError:
                                continue
                    return
                raise RuntimeError("event generation marker is incoherent")
            connection.execute("BEGIN")
            generation = connection.execute(
                "SELECT value FROM event_meta WHERE key='generation'").fetchone()
            if generation is not None and bytes(generation[0]) == expected:
                coherent = True
                break
            connection.rollback()
            state.pop("generation_stamp", None)
            if attempt < 2:
                time.sleep(0.001)
        if not coherent:
            raise RuntimeError("event store generation is incoherent")
        if full:
            blank_names = {str(row[0]) for row in connection.execute(
                "SELECT name FROM event_sessions WHERE session='' ")}
            blank_identities = {}
            if blank_names:
                for agent, session in key_list:
                    name = event_filename(agent, session)
                    if name in blank_names:
                        blank_identities[name] = (agent, session)
            authority = (_event_filter_authority(
                connection, expected_agents, store_stamp, stamp, expected)
                if literal_fast else None)
            candidate_only = bool(literal_fast and authority is not None)
            if literal_fast and not candidate_only:
                _request_event_repair()
            if authority is not None and not _event_filter_authority_stable(
                    authority, connection, store, marker):
                raise RuntimeError("event proof changed during bulk read")
            buffered = bool(literal_fast)
            if buffered:
                import tempfile
                pending = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
            literal_sql, literal_params = (
                _event_sql_literal_filter(required_literals)
                if candidate_only else ("", ()))
            rows = connection.execute(
                "SELECT name,agent,session,digest,payload "
                "FROM event_sessions "
                + (f"WHERE {literal_sql} " if literal_sql else "")
                + "ORDER BY name",
                literal_params)
            for name, agent, session, digest, payload in rows:
                identity = ((str(agent), str(session)) if session else
                            blank_identities.get(str(name)))
                if identity is not None:
                    body = bytes(payload)
                    if candidate_only and not payload_candidate(body):
                        continue
                    if _event_payload_digest(body) != bytes(digest):
                        _request_event_repair()
                        raise RuntimeError("event payload hash mismatch")
                    if not payload_candidate(body):
                        continue
                    if buffered:
                        offset = pending.tell()
                        pending.write(body)
                        pending_rows.append((identity, offset, len(body)))
                    else:
                        yield *identity, body
        else:
            authority = (_event_filter_authority(
                connection, expected_agents, store_stamp, stamp, expected)
                if literal_fast else None)
            candidate_only = bool(literal_fast and authority is not None)
            if literal_fast and not candidate_only:
                _request_event_repair()
            if authority is not None and not _event_filter_authority_stable(
                    authority, connection, store, marker):
                raise RuntimeError("event proof changed during bulk read")
            buffered = bool(literal_fast)
            if buffered:
                import tempfile
                pending = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
            names = list(requested)
            for offset in range(0, len(names), 500):
                batch = names[offset:offset + 500]
                marks = ",".join("?" for _ in batch)
                literal_sql, literal_params = (
                    _event_sql_literal_filter(required_literals)
                    if candidate_only else ("", ()))
                rows = connection.execute(
                    f"SELECT name,digest,payload FROM event_sessions "
                    f"WHERE name IN ({marks})"
                    + (f" AND ({literal_sql})" if literal_sql else ""),
                    (*batch, *literal_params))
                for name, digest, payload in rows:
                    body = bytes(payload)
                    if candidate_only and not payload_candidate(body):
                        continue
                    if _event_payload_digest(body) != bytes(digest):
                        _request_event_repair()
                        raise RuntimeError("event payload hash mismatch")
                    if not payload_candidate(body):
                        continue
                    identity = requested[str(name)]
                    if buffered:
                        offset = pending.tell()
                        pending.write(body)
                        pending_rows.append((identity, offset, len(body)))
                    else:
                        yield *identity, body
        if authority is not None:
            if not _event_filter_authority_stable(
                    authority, connection, store, marker):
                raise RuntimeError("event proof changed during bulk read")
        elif (not connection.source_stable()
              or _event_store_stamp(store) != store_stamp
              or _event_file_stamp(marker) != stamp):
            raise RuntimeError("event generation changed during bulk read")
        if pending is not None:
            for identity, offset, length in pending_rows:
                pending.seek(offset)
                body = pending.read(length)
                if len(body) != length:
                    raise RuntimeError("event validation buffer is incomplete")
                yield *identity, body
    except sqlite3.Error as error:
        message = str(error).lower()
        legacy_schema = (isinstance(error, sqlite3.OperationalError)
                         and ("no such table" in message or "no such column" in message))
        try:
            seeded = connection.execute(
                "SELECT 1 FROM event_meta WHERE key='manifest'").fetchone()
        except sqlite3.Error:
            seeded = None
        if legacy_schema and seeded is None and _event_legacy_allowed(EVENTS_DIR):
            for agent, session in requested_keys:
                for path in event_path_candidates(agent, session):
                    try:
                        body = _read_regular_bytes(path)
                        if payload_candidate(body):
                            yield agent, session, body
                        break
                    except OSError:
                        continue
            return
        raise RuntimeError("event store read failed") from error
    finally:
        try:
            if pending is not None:
                pending.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()


_EVENT_COUNT_MAX = (1 << 63) - 1


@functools.lru_cache(maxsize=64)
def _event_literal_patterns(tokens: tuple[str, ...]) -> tuple[re.Pattern[bytes], ...]:
    """Necessary-condition byte matchers for Python text ``re.I`` literals.

    Event payloads are canonical UTF-8 JSON.  A bytes regex normally has
    ASCII-only case folding; include the Unicode spellings Python's text re.I
    admits for ASCII literals and the Kelvin sign that ``str.lower`` maps to k.
    JSON-escaped forms keep hand-authored/legacy payloads lossless too.
    """
    special = {
        "i": (b"\xc4\xb0", b"\xc4\xb1",
              re.escape(b"\\u0130"), re.escape(b"\\u0131")),
        "s": (b"\xc5\xbf", re.escape(b"\\u017f")),
        "k": (b"\xe2\x84\xaa", re.escape(b"\\u212a")),
    }
    patterns = []
    for token in tokens:
        if not token or not token.isascii():
            continue
        parts = []
        for char in token.lower():
            ordinary = re.escape(char.encode("ascii"))
            escaped = {
                re.escape(f"\\u{ord(char):04x}".encode("ascii")),
                re.escape(f"\\u{ord(char.upper()):04x}".encode("ascii")),
            }
            short_escape = {
                '"': b'\\"', "\\": b"\\\\", "/": b"\\/",
                "\b": b"\\b", "\f": b"\\f", "\n": b"\\n",
                "\r": b"\\r", "\t": b"\\t",
            }.get(char)
            variants = tuple(dict.fromkeys((
                ordinary,
                *(special.get(char) or ()),
                *sorted(escaped),
                *((re.escape(short_escape),)
                  if short_escape is not None else ()),
            )))
            parts.append(b"(?:" + b"|".join(variants) + b")")
        patterns.append(re.compile(b"".join(parts), re.I))
    return tuple(patterns)


@functools.lru_cache(maxsize=64)
def _event_literal_fast_matchers(
    tokens: tuple[str, ...],
) -> tuple[tuple[bytes, tuple[bytes, ...]], ...]:
    """Ordinary-ASCII fast path plus markers that justify the exact regex."""
    special_markers = {
        "i": (b"\xc4\xb0", b"\xc4\xb1"),
        "s": (b"\xc5\xbf",),
        "k": (b"\xe2\x84\xaa", b"\\u212"),
        "/": (b"\\/",),
        '"': (b'\\"',),
        "\\": (b"\\\\",),
    }
    out = []
    for token in tokens:
        if not token or not token.isascii():
            continue
        word = token.lower()
        markers = [b"\\u0" if "i" in word or "s" in word else b"\\u00"]
        for char in word:
            markers.extend(special_markers.get(char, ()))
        out.append((
            token.lower().encode("ascii"),
            tuple(dict.fromkeys(markers)),
        ))
    return tuple(out)


def _event_bytes_contain_literals(payload: bytes, tokens: tuple[str, ...]) -> bool:
    tokens = _event_filterable_literals(tokens)
    exact = _event_literal_patterns(tokens)
    fast = _event_literal_fast_matchers(tokens)
    lowered = payload.lower()
    if not _event_bytes_maybe_literals(payload, fast, lowered):
        return False
    for pattern, (ordinary, markers) in zip(exact, fast):
        if ordinary in lowered:
            continue
        if pattern.search(payload) is None:
            return False
    return True


def _event_bytes_maybe_literals(
    payload: bytes,
    fast: tuple[tuple[bytes, tuple[bytes, ...]], ...],
    lowered: bytes | None = None,
) -> bool:
    """Reject a line only when no raw spelling can decode to the literal."""
    lowered = payload.lower() if lowered is None else lowered
    for ordinary, markers in fast:
        if ordinary in lowered:
            continue
        for marker in markers:
            if marker in payload:
                break
        else:
            return False
    return True


def event_payload_contains_literals(payload: bytes, tokens: tuple[str, ...]) -> bool:
    """Whether a JSON event payload can contain every case-insensitive literal."""
    return _event_bytes_contain_literals(payload, tokens)


def _event_utf8_text(event: dict, name: str) -> str:
    value = event.get(name, "")
    if type(value) is not str:
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return value


def _tool_search_record(
    event: dict,
) -> tuple[str, tuple[int, int] | None]:
    """Canonical searchable text plus its exact output span, if any."""
    head = _event_utf8_text(event, "name").strip()
    kind = _event_utf8_text(event, "kind")
    if kind == "control":
        return "", None
    if kind == "subagent_start":
        head = f"subagent {head}".strip()
    elif kind == "subagent_result":
        head = f"subagent result {head}".strip()
    if event.get("ok") is False:
        head += " [failed]"
    input_text = _event_utf8_text(event, "input").strip()
    text = ": ".join(
        value for value in (head, input_text) if value)
    output = _event_utf8_text(event, "output")
    if not output:
        return text, None
    combined = text + "\n" + output
    left_trimmed = len(combined) - len(combined.lstrip())
    source_end = len(combined.rstrip())
    rendered = combined[left_trimmed:source_end]
    output_start = max(len(text) + 1, left_trimmed) - left_trimmed
    output_end = source_end - left_trimmed
    bounds = (
        (output_start, output_end)
        if output_start < output_end else
        None
    )
    return rendered, bounds


_TOOL_EVENT_ID_BYTES = 12
_TOOL_EVENT_ID_TEXT_MAX_BYTES = 16 * 1024
_TOOL_EVENT_ID_SESSION_MAX_BYTES = 4096


def tool_event_identity(
        session: object, turn: object, ts: object, text: object,
) -> str | None:
    """Return a bounded stable identity for one canonical tool event."""
    if (type(session) is not str or not session
            or type(turn) is not int or type(ts) is not int
            or type(text) is not str or not text):
        return None
    try:
        session_bytes = session.encode("utf-8")
        text_bytes = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if (len(session_bytes) > _TOOL_EVENT_ID_SESSION_MAX_BYTES
            or len(text_bytes) > _TOOL_EVENT_ID_TEXT_MAX_BYTES):
        return None
    digest = hashlib.blake2b(
        digest_size=_TOOL_EVENT_ID_BYTES, person=b"agrep-event-v1")
    for value in (session_bytes, str(ts).encode("ascii"), text_bytes):
        digest.update(len(value).to_bytes(8, "little"))
        digest.update(value)
    return digest.hexdigest()


def tool_search_record(
        event: dict,
) -> tuple[str, tuple[int, int] | None]:
    """Render canonical searchable text plus its exact output bounds."""
    if not isinstance(event, dict):
        return "", None
    return _tool_search_record(event)


def tool_search_text(event: dict) -> str:
    """Render the canonical searchable text for one tool event."""
    return tool_search_record(event)[0]


def _event_ts_turn(event: dict, marks: list[tuple[int, int]]) -> tuple[int, int]:
    import bisect

    raw_ts = event.get("ts", 0)
    ts = (raw_ts if type(raw_ts) is int
          and -_EVENT_COUNT_MAX <= raw_ts <= _EVENT_COUNT_MAX else 0)
    index = bisect.bisect_right(marks, (ts, float("inf"))) - 1 if ts else -1
    turn = marks[index][1] if index >= 0 else (marks[0][1] if marks else 0)
    return ts, turn


def _tool_literal_occurrences(
    event: dict, literal: str, pattern: re.Pattern[str] | None = None,
) -> int:
    """Count one plain alphanumeric literal in canonical searchable fields."""
    kind = _event_utf8_text(event, "kind")
    if kind == "control":
        return 0
    head = _event_utf8_text(event, "name").strip()
    if kind == "subagent_start":
        head = f"subagent {head}".strip()
    elif kind == "subagent_result":
        head = f"subagent result {head}".strip()
    if event.get("ok") is False:
        head += " [failed]"
    parts = (head, _event_utf8_text(event, "input").strip(),
             _event_utf8_text(event, "output"))
    if pattern is None:
        return (parts[0].lower().count(literal)
                + parts[1].lower().count(literal)
                + parts[2].lower().count(literal))
    return sum(1 for part in parts for _match in pattern.finditer(part))


def tool_literal_matches_from_payload(
    payload: bytes,
    turns: "list[tuple[int, int]]",
    literal: str,
    required_literals: tuple[str, ...] = (),
    *,
    strict: bool = False,
):
    """Yield exact lightweight matches without constructing full tool rows."""
    import io

    marks = sorted((ts, turn) for ts, turn in turns if ts)
    fast = _event_literal_fast_matchers(
        _event_filterable_literals(required_literals))
    pattern = (re.compile(re.escape(literal), re.I)
               if "i" in literal or "s" in literal else None)
    try:
        for raw in io.BytesIO(payload):
            if fast and not _event_bytes_maybe_literals(raw, fast):
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if strict:
                    raise RuntimeError("malformed tool-event row") from exc
                continue
            if not isinstance(event, dict):
                if strict:
                    raise RuntimeError("malformed tool-event row")
                continue
            occurrences = _tool_literal_occurrences(event, literal, pattern)
            if occurrences:
                ts, turn = _event_ts_turn(event, marks)
                yield event, ts, turn, occurrences
    except (AttributeError, TypeError, ValueError) as exc:
        if strict:
            raise RuntimeError("malformed tool-event payload") from exc
        return


def tool_row_from_event(event: dict, ts: int, turn: int) -> dict | None:
    """Materialize one canonical tool row after lightweight selection."""
    text, payload_bounds = _tool_search_record(event)
    if not text:
        return None

    def count(name: str) -> int | None:
        value = event.get(name)
        return (value if type(value) is int
                and 0 <= value <= _EVENT_COUNT_MAX else None)

    def boolean(name: str) -> bool | None:
        if name not in event:
            return None
        value = event.get(name)
        return value if type(value) is bool else None

    def truncated(name: str, text_value: str, count_name: str) -> bool | None:
        if name in event:
            return boolean(name)
        declared = count(count_name)
        return ((declared is not None and declared > len(text_value))
                or len(text_value) > 800)

    input_text = _event_utf8_text(event, "input")
    output_text = _event_utf8_text(event, "output")
    return {
        "turn": turn, "ts": ts, "text": text,
        "payload_bounds": payload_bounds,
        "kind": _event_utf8_text(event, "kind"),
        "name": _event_utf8_text(event, "name"),
        "input": input_text, "output": output_text,
        "input_chars": count("input_chars"),
        "output_chars": count("output_chars"),
        "output_bytes": count("output_bytes"),
        "input_truncated": truncated(
            "input_truncated", input_text, "input_chars"),
        "output_truncated": truncated(
            "output_truncated", output_text, "output_chars"),
        "ok": boolean("ok"),
        "call_id": _event_utf8_text(event, "call_id"),
        "child": _event_utf8_text(event, "child"),
    }


def tool_rows_from_payload(
    payload: bytes,
    turns: "list[tuple[int, int]]",
    required_literals: tuple[str, ...] = (),
    *,
    strict: bool = False,
) -> list[dict]:
    """Compose searchable tool rows from one canonical event BLOB."""
    import io

    marks = sorted((ts, turn) for ts, turn in turns if ts)
    fast = _event_literal_fast_matchers(
        _event_filterable_literals(required_literals))
    out: list[dict] = []
    try:
        for raw in io.BytesIO(payload):
            # A search match lives within one canonical event row.  Reject the
            # overwhelmingly common miss before UTF-8 decoding, JSON parsing,
            # and construction of every searchable/provenance field.
            if fast and not _event_bytes_maybe_literals(raw, fast):
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if strict:
                    raise RuntimeError("malformed tool-event row") from exc
                continue
            if not isinstance(event, dict):
                if strict:
                    raise RuntimeError("malformed tool-event row")
                continue
            ts, turn = _event_ts_turn(event, marks)
            row = tool_row_from_event(event, ts, turn)
            if row is not None:
                out.append(row)
    except (AttributeError, TypeError, ValueError) as exc:
        if strict:
            raise RuntimeError("malformed tool-event payload") from exc
        return []
    return out
