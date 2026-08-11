"""Ownership protocol that blocks background writers during removal."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable

from events import DATA_DIR, data_dir_readonly
import ownerfile
from proc import _MAX_PROCESS_ID, pid_alive, process_start_identity


_BACKGROUND_REMOVAL_GRACE_S = 30.0


def background_removal_path() -> Path:
    return DATA_DIR / ".background-removal"


def background_removal_cooldown_path() -> Path:
    return DATA_DIR / ".background-removal.cooldown"


def _background_removal_protected(observed: ownerfile.Snapshot) -> bool:
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("removal fence must be an object")
        pid = record.get("pid")
        process_start = record.get("process_start")
        started_at = record.get("started_at")
        nonce = record.get("nonce")
        if (not isinstance(pid, int) or isinstance(pid, bool)
                or not 0 < pid <= _MAX_PROCESS_ID
                or not isinstance(process_start, str)
                or process_start in ("", "None", "unknown")
                or not isinstance(started_at, (int, float))
                or isinstance(started_at, bool)
                or not math.isfinite(float(started_at))
                or not isinstance(nonce, str)
                or re.fullmatch(r"[0-9a-f]{32}", nonce) is None):
            raise ValueError("removal fence schema is invalid")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        age = time.time() - observed.mtime
        return 0.0 <= age <= _BACKGROUND_REMOVAL_GRACE_S
    owner = ownerfile.classify_process(
        pid, process_start, pid_alive=pid_alive,
        process_start=process_start_identity)
    if owner in (
            ownerfile.ProcessOwner.EXACT_LIVE,
            ownerfile.ProcessOwner.UNVERIFIABLE):
        return True
    return False


def _background_cooldown_protected(observed: ownerfile.Snapshot) -> bool:
    try:
        record = json.loads(observed.raw)
        if not isinstance(record, dict):
            raise TypeError("removal cooldown must be an object")
        completed_at = record.get("completed_at")
        expires_at = record.get("expires_at")
        nonce = record.get("nonce")
        if (not isinstance(completed_at, (int, float))
                or isinstance(completed_at, bool)
                or not math.isfinite(float(completed_at))
                or not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not math.isfinite(float(expires_at))
                or not 0.0 <= float(expires_at) - float(completed_at)
                <= _BACKGROUND_REMOVAL_GRACE_S
                or not isinstance(nonce, str)
                or re.fullmatch(r"[0-9a-f]{32}", nonce) is None):
            raise ValueError("removal cooldown schema is invalid")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        age = time.time() - observed.mtime
        return 0.0 <= age <= _BACKGROUND_REMOVAL_GRACE_S
    age = time.time() - float(completed_at)
    lifetime = float(expires_at) - float(completed_at)
    return 0.0 <= age <= lifetime


def _owner_record_active(
        path: Path,
        protected: Callable[[ownerfile.Snapshot], bool],
        *, settle: bool = True,
) -> bool:
    try:
        observed = ownerfile.snapshot(path, max_bytes=1024)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if protected(observed):
        return True
    if not settle or data_dir_readonly(path.parent):
        return False
    try:
        return not ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    except OSError:
        return True


def background_removal_active() -> bool:
    settle = not data_dir_readonly(DATA_DIR)
    if _owner_record_active(
            background_removal_path(), _background_removal_protected,
            settle=settle):
        return True
    return _owner_record_active(
        background_removal_cooldown_path(), _background_cooldown_protected,
        settle=settle)


def background_removal_owned_by_current_process() -> bool:
    """Return whether this process owns the exact live removal fence."""
    try:
        observed = ownerfile.snapshot(background_removal_path(), max_bytes=1024)
        record = json.loads(observed.raw)
        pid = record["pid"]
        process_start = record["process_start"]
    except (FileNotFoundError, KeyError, OSError, RecursionError, TypeError,
            UnicodeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(pid, int) or isinstance(pid, bool) or pid != os.getpid():
        return False
    if not isinstance(process_start, str) or not process_start:
        return False
    current_start = process_start_identity(pid)
    return current_start is not None and current_start == process_start


def acquire_background_removal_fence() -> ownerfile.Handle | None:
    if data_dir_readonly(DATA_DIR):
        return None
    if background_removal_active():
        return None
    path = background_removal_path()
    pid = os.getpid()
    process_start = process_start_identity(pid)
    if process_start is None:
        return None
    raw = json.dumps({
        "pid": pid, "process_start": process_start,
        "started_at": time.time(), "nonce": secrets.token_hex(16),
    }, separators=(",", ":")).encode("utf-8")
    for _ in range(2):
        try:
            return ownerfile.create_exclusive(
                path, raw, mode=0o600, fsync=True, retain_fd=True)
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path, max_bytes=1024)
                if (_background_removal_protected(observed)
                        or not ownerfile.remove_exact(
                            path, observed, tombstone=True,
                            require_stable_mtime=True)):
                    return None
            except OSError:
                return None
        except OSError:
            return None
    return None


def _close_failed_removal_fence(owner: ownerfile.Handle) -> None:
    if data_dir_readonly(DATA_DIR):
        owner.close()
        return
    try:
        owner.touch()
    except OSError:
        pass
    owner.close()


def finish_background_removal_fence(owner: ownerfile.Handle) -> bool:
    """Hand a live removal transaction to a bounded ownerless cooldown."""
    if data_dir_readonly(DATA_DIR):
        owner.close()
        return False
    try:
        owner.verify(require_stable_mtime=True)
    except OSError:
        owner.close()
        return False
    path = background_removal_cooldown_path()
    now = time.time()
    raw = json.dumps({
        "completed_at": now,
        "expires_at": now + _BACKGROUND_REMOVAL_GRACE_S,
        "nonce": secrets.token_hex(16),
    }, separators=(",", ":")).encode("utf-8")
    cooldown = None
    for _ in range(2):
        try:
            cooldown = ownerfile.create_exclusive(
                path, raw, mode=0o600, fsync=True)
            break
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path, max_bytes=1024)
                if (_background_cooldown_protected(observed)
                        or not ownerfile.remove_exact(
                            path, observed, tombstone=True,
                            require_stable_mtime=True)):
                    _close_failed_removal_fence(owner)
                    return False
            except OSError:
                _close_failed_removal_fence(owner)
                return False
        except OSError:
            _close_failed_removal_fence(owner)
            return False
    if cooldown is None:
        _close_failed_removal_fence(owner)
        return False
    cooldown.close()
    try:
        released = owner.release(
            tombstone=True, require_stable_mtime=True)
    except OSError:
        return False
    return bool(released)


def clear_background_removal_fence() -> bool:
    if data_dir_readonly(DATA_DIR):
        return False
    path = background_removal_path()
    try:
        observed = ownerfile.snapshot(path, max_bytes=1024)
    except FileNotFoundError:
        observed = None
    except OSError:
        return False
    if observed is not None:
        try:
            record = json.loads(observed.raw)
            pid = int(record["pid"])
            process_start = str(record["process_start"])
        except (KeyError, OverflowError, RecursionError, TypeError, UnicodeError,
                ValueError, json.JSONDecodeError):
            if _background_removal_protected(observed):
                return False
        else:
            owner = ownerfile.classify_process(
                pid, process_start, pid_alive=pid_alive,
                process_start=process_start_identity)
            if owner in (
                    ownerfile.ProcessOwner.EXACT_LIVE,
                    ownerfile.ProcessOwner.UNVERIFIABLE):
                return False
        try:
            if not ownerfile.remove_exact(
                    path, observed, tombstone=True,
                    require_stable_mtime=True):
                return False
        except OSError:
            return False
    cooldown_path = background_removal_cooldown_path()
    try:
        cooldown = ownerfile.snapshot(cooldown_path, max_bytes=1024)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        return ownerfile.remove_exact(
            cooldown_path, cooldown, tombstone=True,
            require_stable_mtime=True)
    except OSError:
        return False
