"""Settings and data-dir provenance: placement warnings and atomic knobs."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import time

from dist import REPO_ROOT
from events import DATA_DIR, DATA_DIR_SOURCE, data_dir_readonly
from fileops import replace_with_retry
import ownerfile
from proc import WIN, pid_alive, process_start_identity
import surface_policy as surface

# The pre-package layout kept the index repo-local at <repo>/data.
LEGACY_REPO_DATA_DIR = REPO_ROOT / "data"


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a.absolute() == b.absolute()


def _data_artifacts(root: Path) -> dict[str, os.stat_result]:
    out = {}
    for name in (
        "messages.jsonl",
        "sessions.jsonl",
        "session_family.meta.json",
        "replies.jsonl",
        "emotions.jsonl",
        "corpus.db",
        ".reindex.sig",
    ):
        p = root / name
        try:
            if p.exists():
                out[name] = p.stat()
        except OSError:
            continue
    return out


def data_dir_source() -> str:
    return DATA_DIR_SOURCE


def data_dir_warnings() -> list[str]:
    """Warn when an old repo-local data dir can be mistaken for the active index."""
    warnings: list[str] = []
    if not WIN:
        try:
            mode = DATA_DIR.stat().st_mode & 0o777
        except OSError:
            mode = 0
        if mode & 0o077:
            warnings.append(
                f"the data dir is not private (mode {mode:03o}); agent "
                "transcripts may be readable by other local users of this "
                "machine."
            )
    legacy = LEGACY_REPO_DATA_DIR
    if _same_path(legacy, DATA_DIR):
        return warnings
    legacy_files = _data_artifacts(legacy)
    if not legacy_files:
        return warnings

    active_files = _data_artifacts(DATA_DIR)
    only_legacy = [n for n in legacy_files if n not in active_files]
    newer_legacy = [
        n for n, st in legacy_files.items()
        if n in active_files and st.st_mtime > active_files[n].st_mtime + 5
    ]
    bits: list[str] = []
    if only_legacy:
        bits.append("only there: " + ", ".join(only_legacy[:4]))
    if newer_legacy:
        bits.append("newer there: " + ", ".join(newer_legacy[:4]))
    if not bits:
        bits.append("old artifacts present")

    warnings.append(
        "repo-local data dir ignored: "
        f"{legacy} ({'; '.join(bits)}). active: {DATA_DIR}. "
        f"Set AGREP_DATA_DIR={legacy} to use it intentionally."
    )
    return warnings


SETTINGS_PATH = DATA_DIR / "settings.json"
_MAX_SETTINGS_BYTES = 64 * 1024


_SETTING_DEFAULT = object()


class SettingsError(RuntimeError):
    pass


def settings_observation() -> dict:
    """Bounded, descriptor-stable settings evidence for diagnostics."""
    try:
        raw = ownerfile.snapshot(
            SETTINGS_PATH, max_bytes=_MAX_SETTINGS_BYTES).raw
    except FileNotFoundError:
        return {"state": "missing", "value": {}}
    except OSError as exc:
        return {
            "state": "unavailable", "value": None,
            "detail": f"settings are unavailable ({type(exc).__name__}: {exc})",
        }
    try:
        value = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeError, ValueError) as exc:
        return {
            "state": "unavailable", "value": None,
            "detail": f"settings are malformed ({type(exc).__name__}: {exc})",
        }
    if not isinstance(value, dict):
        return {
            "state": "unavailable", "value": None,
            "detail": "settings are malformed (top level is not an object)",
        }
    return {"state": "verified", "value": value}


def setting_observation(key: str, default=_SETTING_DEFAULT) -> dict:
    """One validated setting without laundering damaged evidence to a default."""
    observed = settings_observation()
    if observed["state"] == "unavailable":
        return observed
    fallback = (
        surface.setting_default(key)
        if default is _SETTING_DEFAULT else default)
    value = observed["value"].get(key, fallback)
    spec = surface.SETTINGS.get(key)
    if spec is not None and spec.choices and value not in spec.choices:
        return {
            "state": "unavailable", "value": None,
            "detail": f"setting {key!r} has an unsupported value",
        }
    return {
        "state": "verified", "value": value,
        "source": (
            "default" if key not in observed["value"] else "settings.json"),
    }


def _read_settings(*, for_update: bool) -> dict:
    try:
        raw = SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if for_update:
            raise SettingsError(
                f"cannot read existing settings file {SETTINGS_PATH}: {exc}") from exc
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        if for_update:
            raise SettingsError(
                f"refusing to replace unreadable settings file {SETTINGS_PATH}: {exc}") from exc
        return {}
    if not isinstance(value, dict):
        if for_update:
            raise SettingsError(
                f"refusing to replace non-object settings file {SETTINGS_PATH}")
        return {}
    return value


def setting(key: str, default=_SETTING_DEFAULT):
    """One user-facing knob (`agrep set <key> <value>`). Read fresh every call -
    the file is tiny, and a cached value would make `agrep set` lie."""
    fallback = (surface.setting_default(key)
                if default is _SETTING_DEFAULT else default)
    return _read_settings(for_update=False).get(key, fallback)


SETTINGS_LOCK_NAME = ".settings.lock"
SETTINGS_LOCK_TIMEOUT_S = 5.0
_LOCK_PUBLISH_GRACE_S = 3.0


def _reclaim_settings_lock(lock: Path) -> bool:
    """Remove a dead or malformed holder's record; False while a live one holds it."""
    try:
        observed = ownerfile.snapshot(lock)
    except OSError:
        return False
    fields: dict[str, str] = {}
    try:
        fields = dict(part.split("=", 1) for part in observed.raw.decode().split())
        pid = int(fields.get("pid", "0"))
    except (UnicodeError, ValueError):
        pid = 0
    if pid > 0:
        state = ownerfile.classify_process(
            pid, fields.get("start"), pid_alive=pid_alive,
            process_start=process_start_identity,
            unverifiable_starts=frozenset(("unknown",)))
        if state in (ownerfile.ProcessOwner.EXACT_LIVE,
                     ownerfile.ProcessOwner.UNVERIFIABLE):
            if ownerfile.stranded_own_record(
                    pid, fields.get("start"), fields.get("token"),
                    process_start=process_start_identity):
                return ownerfile.remove_exact(
                    lock, observed, tombstone=True, require_stable_mtime=True)
            return False
    elif 0.0 <= time.time() - observed.mtime <= _LOCK_PUBLISH_GRACE_S:
        return False
    return ownerfile.remove_exact(lock, observed, tombstone=True, require_stable_mtime=True)


@contextmanager
def _settings_lock():
    """Serializes the read-modify-write of settings.json between agrep processes.
    Deliberately NOT the index lock: `agrep set` must never queue behind an index
    run, and nothing needs settings frozen for one - the search-db build stamps
    the `tools` value into its source generation and retries when it moves."""
    lock = SETTINGS_PATH.with_name(SETTINGS_LOCK_NAME)
    deadline = time.monotonic() + SETTINGS_LOCK_TIMEOUT_S
    delay = 0.02
    pid = os.getpid()
    start = process_start_identity(pid) or "unknown"
    token = secrets.token_hex(16)
    while True:
        body = f"pid={pid} start={start} token={token}\n".encode()
        try:
            handle = ownerfile.create_held(
                lock, body, token=token, retain_fd=True)
            break
        except FileExistsError:
            if _reclaim_settings_lock(lock):
                continue
            if time.monotonic() >= deadline:
                raise SettingsError(
                    "another agrep process is updating settings; try again") from None
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)
    try:
        yield
    finally:
        try:
            handle.release(tombstone=True, require_stable_mtime=True)
        except OSError:
            pass


def update_setting(key: str, mutate):
    """Atomically replace one setting from its current value."""
    if data_dir_readonly(SETTINGS_PATH.parent):
        raise SettingsError(
            "AGREP_DATA_READONLY protects this data directory")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _settings_lock():
        path = SETTINGS_PATH
        cur = _read_settings(for_update=True)
        value = mutate(cur.get(key))
        cur[key] = value
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
            replace_with_retry(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return value


def set_setting(key: str, value) -> None:
    update_setting(key, lambda _current: value)
