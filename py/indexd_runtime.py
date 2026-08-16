"""Freshness-daemon ownership, spawning, and index-build coordination."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

import common
import events
import fileops
import ownerfile
import removal_fence
import surface_policy as surface


# Write-side rate limit for freshness stamps (re-ingest / verified-current
# re-stamps). Demoted from the read-time FRESHNESS_MAX_AGE_S verdict: read
# surfaces judge drift against the published generation, never this clock.
FRESHNESS_WRITE_RATE_S = 120.0
# Protocol versions retire old packages; dynamic imports require the build id to
# cover every shipped module. Editable over-retirement costs one daemon respawn.
INDEXD_PROTOCOL = 2


def _indexd_build_files() -> tuple[str, ...]:
    """Cover exactly the shipped Python runtime, including its data contracts."""
    manifest = common.PY_DIR / "runtime_manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        raw_files = payload["files"]
    except (OSError, RecursionError, UnicodeError,
            json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid runtime manifest {manifest}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(raw_files, list):
        raise RuntimeError(f"invalid runtime manifest schema: {manifest}")
    members: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"source", "member"}:
            raise RuntimeError(f"invalid runtime manifest entry: {item!r}")
        member = item["member"]
        if not isinstance(member, str):
            raise RuntimeError(f"invalid runtime manifest member: {member!r}")
        if member.startswith("agrep/py/"):
            relative = member.removeprefix("agrep/py/")
            path = Path(relative)
            if (not relative or "\\" in relative or path.is_absolute()
                    or path.as_posix() != relative
                    or any(part in {"", ".", ".."} for part in path.parts)):
                raise RuntimeError(f"invalid runtime manifest member: {member!r}")
            members.add(relative)
    expected = sum(
        isinstance(item, dict)
        and isinstance(item.get("member"), str)
        and item["member"].startswith("agrep/py/")
        for item in raw_files
    )
    if len(members) != expected:
        raise RuntimeError(f"duplicate runtime manifest member: {manifest}")
    if not members:
        raise RuntimeError(f"empty Python runtime manifest: {manifest}")
    if "runtime_manifest.json" not in members:
        raise RuntimeError(f"runtime manifest must list itself: {manifest}")
    return tuple(sorted(members))


INDEXD_BUILD_FILES = _indexd_build_files()
_PYTHON_RUNTIME_DIGEST_LOCK = threading.Lock()
_PYTHON_RUNTIME_DIGEST_CACHE: tuple[
    tuple[tuple[str, fileops.FileIdentity], ...], bytes,
] | None = None


def _python_runtime_digest(
        files: tuple[str, ...] | None = None,
        root: Path | None = None,
) -> bytes:
    """Hash exact stable bytes for every Python module that can write a store."""
    global _PYTHON_RUNTIME_DIGEST_CACHE

    cacheable = files is None and root is None
    members = files or INDEXD_BUILD_FILES
    base = root or common.PY_DIR
    try:
        identities = tuple(
            (member, fileops.file_identity(base / member))
            for member in members
        )
    except OSError as exc:
        raise RuntimeError(
            f"cannot identify Python runtime member under {base}: {exc}") from exc
    if cacheable:
        _PYTHON_RUNTIME_DIGEST_LOCK.acquire()
        cached = _PYTHON_RUNTIME_DIGEST_CACHE
        if cached is not None and cached[0] == identities:
            _PYTHON_RUNTIME_DIGEST_LOCK.release()
            return cached[1]
    digest = hashlib.sha256()
    try:
        digest.update(b"agrep-indexd-python-runtime-v1\0")
        digest.update(repr(sys.version_info[:3]).encode("ascii"))
        digest.update(b"\0")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        for member, before in identities:
            path = base / member
            try:
                fd = os.open(path, flags)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError(
                            f"Python runtime member is not regular: {path}")
                    if fileops.file_identity_fd(fd) != before:
                        raise OSError(
                            f"Python runtime member changed before hashing: {path}")
                    content = hashlib.sha256()
                    length = 0
                    for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                        content.update(chunk)
                        length += len(chunk)
                    if (fileops.file_identity_fd(fd) != before
                            or fileops.file_identity(path) != before):
                        raise OSError(
                            f"Python runtime member changed while hashing: {path}")
                finally:
                    os.close(fd)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot identify Python runtime member {path}: {exc}") from exc
            encoded = member.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "little"))
            digest.update(encoded)
            digest.update(length.to_bytes(8, "little"))
            digest.update(content.digest())
        value = digest.digest()
        if cacheable:
            _PYTHON_RUNTIME_DIGEST_CACHE = (identities, value)
        return value
    finally:
        if cacheable:
            _PYTHON_RUNTIME_DIGEST_LOCK.release()


INDEXD_BUILD_DIGEST = _python_runtime_digest()
INDEXD_BUILD_ID = INDEXD_BUILD_DIGEST.hex()[:20]
INDEXD_LOCK_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.lock"
INDEXD_READY_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.ready"
INDEXD_CHILD_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.child"
INDEXD_LIVE_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.live"
LEGACY_INDEXD_LOCK_PATH = common.DATA_DIR / ".indexd.lock"
_INDEXD_PUBLICATION_GRACE_S = 3.0
_INDEXD_OWNER_MAX_BYTES = 4096
_INDEXD_LIVE_MAX_BYTES = 60 * 1024
_INDEXD_LIVE_MAX_AGE_S = 12.0
_INDEXD_LIVE_FUTURE_S = 2.0
_INDEXD_LIVE_SCHEMA = 1
_INDEXD_ACQUIRE_WAIT_S = 3.0
_INDEXD_READY_WAIT_S = 3.0
_INDEXD_FOREGROUND_RETIRE_S = 0.05
_INDEXD_AUTORETIRE_MIN_S = 0.5
_INDEXD_STARTUP_GRACE_S = 10.0
_LEGACY_ORPHAN_GRACE_S = 3700.0
# Keeping an exact incompatible daemon until its idle exit strands searches on
# the emergency lane; the identity-checked successor handoff retires it below.
SEARCH_BEAT_PATH = common.DATA_DIR / f".agrep.search.v{INDEXD_PROTOCOL}"
_SPAWN_GUARD_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.spawn"
_SPAWN_GUARD_S = 30.0
_INDEXD_RESPONSE_PATH = common.DATA_DIR / f".indexd.v{INDEXD_PROTOCOL}.probe"
_INDEXD_HEARTBEAT_STALE_S = 15.0
_INDEXD_RESPONSE_GRACE_S = 7.0
_INDEXD_RESPONSE_MAX_BYTES = 4096
_SOURCE_HEALTH_MAX_BYTES = 1024 * 1024
DERIVED_OWNER_PATH = common.DATA_DIR / ".derived-owner.json"
INGEST_CACHE_PATH = common.DATA_DIR / ".ingest_cache.bin"
_DERIVED_OWNER_VERSION = 1
_DERIVED_OWNER_MAX_BYTES = 4096
_INGEST_CACHE_OWNER_BYTES = 44
_INGEST_CACHE_BASE_MAGIC = b"AGRPCB01"
_INGEST_CACHE_OWNER_OFFSET = 24
_INGEST_CACHE_LEGACY_VERSIONS = frozenset((2, 3))
_INGEST_CACHE_OWNER_VERSION = 4
_BUILD_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_DERIVED_ADOPTION_OWNER_TOKEN_ENV = (
    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN")
_DERIVED_WRITER_IDENTITY_BLOCKED_ENV = (
    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED")
_PYTHON_RUNTIME_BUILD_ID_ENV = "AGREP_PYTHON_RUNTIME_BUILD_ID"
_INDEXD_REFRESH_EXPECTED_WRITER_ENV = "AGREP_INDEXD_REFRESH_EXPECTED_WRITER"
_INDEXD_REFRESH_EXPECTED_WRITER = os.environ.pop(
    _INDEXD_REFRESH_EXPECTED_WRITER_ENV, "")
_BINARY_DIGEST_LOCK = threading.Lock()
_BINARY_DIGEST_CACHE: tuple[
    tuple[str, fileops.FileIdentity], bytes] | None = None


class DerivedOwnerInfo(NamedTuple):
    state: str
    build_id: str | None
    legacy_corpus_db: Mapping[str, object] | None
    reason: str
    retained_corpus_db: Mapping[str, object] | None = None


class IngestCacheOwnerInfo(NamedTuple):
    state: str
    build_id: str | None
    reason: str


class DerivedMutationInfo(NamedTuple):
    state: str
    build_id: str | None
    reason: str
    # a rollback journal, not an owner, is what stopped the probe: no Python
    # writer may proceed, and only the lock-holding Rust writer can adjudicate it
    journal_blocked: bool = False

    @property
    def writable(self) -> bool:
        return self.state in {"absent", "current", "legacy"}


def derived_writer_launchable(info: DerivedMutationInfo) -> bool:
    """The single launchability predicate every writer entrypoint consults.

    A foreign or journaled family launches anyway: the Rust writer holds the
    index lock and daemon fence, so it takes over dead owners, finishes dead
    rollbacks, or serves read-only. A journaled database is recoverable-by-us,
    not owned-by-someone-else, so it belongs in the same class as foreign
    everywhere; Python writers keep refusing both. Verdicts that name a state
    no writer may act on (an unreadable anchor, a clobber fence) stay closed.
    """
    return info.writable or info.state == "foreign" or info.journal_blocked


DERIVED_JOURNAL_SETTLE_S = max(0.0, float(
    os.environ.get("AGREP_DERIVED_JOURNAL_SETTLE_S", "1.0")))
_DERIVED_JOURNAL_POLL_S = 0.02


class DerivedWriteContended(OSError):
    """A transient writer refusal: another writer's open transaction, not an owner.

    Same class of event as the publication races - the bundle is untouched and the
    next pass republishes - so callers must record contention, never a failed build.
    """


def derived_writer_mutation_settled(
        current_build_id: str | None = None, *,
        allow_legacy_adoption: bool = False,
        settle_s: float | None = None) -> DerivedMutationInfo:
    """`derived_writer_mutation_info`, but wait out a live rollback journal.

    A journal is someone's open transaction; it clears on its own, and
    `derived_writer_launchable` already treats it as recoverable-by-us. The
    foreground reader gives the same condition _CONTENDED_READER_WAIT_MS; a writer
    preflight that samples it once turns a sub-second window into a refusal. Every
    other verdict returns on the first probe, so no real refusal is delayed.
    """
    budget = DERIVED_JOURNAL_SETTLE_S if settle_s is None else max(0.0, settle_s)
    deadline = time.monotonic() + budget
    while True:
        info = derived_writer_mutation_info(
            current_build_id, allow_legacy_adoption=allow_legacy_adoption)
        if not info.journal_blocked or time.monotonic() >= deadline:
            return info
        time.sleep(_DERIVED_JOURNAL_POLL_S)


class DerivedWriteFenced(OSError):
    """One writer launch refused by a structured ownership verdict, not a crash.

    An unreadable anchor or parse cache is a repairable local condition. It
    reaches callers carrying the module's own verdict so a diagnostic path can
    state the cause instead of surfacing an unexpected error.
    """

    def __init__(self, info: DerivedMutationInfo) -> None:
        super().__init__(info.reason)
        self.info = info


def _fenced_reason(exc: OSError) -> str:
    """Name a refused writer launch in the ownership vocabulary, always."""
    info = getattr(exc, "info", None)
    return str(getattr(info, "reason", "") or exc) or type(exc).__name__


def _ingest_binary_digest(path: Path | None = None) -> bytes:
    """Hash one stable resolved executable, caching by its exact change identity."""
    global _BINARY_DIGEST_CACHE

    binary = Path(path or common.ingest_bin()).resolve(strict=True)
    before = fileops.file_identity(binary)
    key = (os.fspath(binary), before)
    with _BINARY_DIGEST_LOCK:
        if _BINARY_DIGEST_CACHE is not None and _BINARY_DIGEST_CACHE[0] == key:
            return _BINARY_DIGEST_CACHE[1]
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(binary, flags)
    try:
        if fileops.file_identity_fd(fd) != before:
            raise OSError(f"ingest binary changed before hashing: {binary}")
        digest = hashlib.md5()
        for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
            digest.update(chunk)
        value = digest.digest()
        if (fileops.file_identity_fd(fd) != before
                or fileops.file_identity(binary) != before):
            raise OSError(f"ingest binary changed while hashing: {binary}")
    finally:
        os.close(fd)
    with _BINARY_DIGEST_LOCK:
        _BINARY_DIGEST_CACHE = (key, value)
    return value


_WRITER_BUILD_OBSERVATION = threading.local()


def _writer_binary_key(binary: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(binary)))


@contextlib.contextmanager
def use_observed_writer_build_id(
        binary: Path, build_id: str | None, detail: str = ""):
    prior = getattr(_WRITER_BUILD_OBSERVATION, "value", None)
    _WRITER_BUILD_OBSERVATION.value = (
        _writer_binary_key(binary), build_id, detail)
    try:
        yield
    finally:
        if prior is None:
            try:
                del _WRITER_BUILD_OBSERVATION.value
            except AttributeError:
                pass
        else:
            _WRITER_BUILD_OBSERVATION.value = prior


def derived_writer_build_id(
        binary: Path | None = None, *, require_binary: bool = False) -> str:
    """Identity shared by the Python derived DB and the exact Rust writer bytes."""
    selected = Path(binary or common.ingest_bin())
    observed = getattr(_WRITER_BUILD_OBSERVATION, "value", None)
    if observed is not None and observed[0] == _writer_binary_key(selected):
        if observed[1] is not None:
            return str(observed[1])
        if require_binary:
            raise OSError(str(observed[2]) or "writer identity is unavailable")
        binary_digest = b"unavailable"
    else:
        try:
            binary_digest = _ingest_binary_digest(selected)
        except OSError:
            if require_binary:
                raise
            binary_digest = b"unavailable"
    digest = hashlib.md5()
    digest.update(b"agrep-derived-writer-v2\0")
    digest.update(INDEXD_BUILD_ID.encode("ascii"))
    digest.update(b"\0")
    digest.update(binary_digest)
    return digest.hexdigest()[:20]


def assert_python_runtime_unchanged() -> None:
    """Fence a resident process after its exact writer sources move on disk."""
    try:
        observed = _python_runtime_digest()
    except RuntimeError as exc:
        raise OSError(str(exc)) from exc
    if observed != INDEXD_BUILD_DIGEST:
        raise OSError(
            "Python derived-writer runtime changed after process start; "
            "restart this process before writing derived stores")


def rust_writer_env(binary: Path | None = None) -> dict[str, str]:
    """Prepare one child environment immediately before a Rust writer launch."""
    if _data_dir_readonly():
        raise DerivedWriteFenced(DerivedMutationInfo(
            "unavailable", None,
            "AGREP_DATA_READONLY protects this data directory from writer "
            "preparation"))
    try:
        assert_python_runtime_unchanged()
        build_id = derived_writer_build_id(binary, require_binary=True)
    except OSError as exc:
        raise DerivedWriteFenced(
            DerivedMutationInfo("unavailable", None, str(exc))) from exc
    ownership = derived_writer_mutation_info(
        build_id, allow_legacy_adoption=True)
    if not derived_writer_launchable(ownership):
        raise DerivedWriteFenced(ownership)
    env = dict(os.environ)
    env["AGREP_RUNTIME_BUILD_ID"] = build_id
    env[_PYTHON_RUNTIME_BUILD_ID_ENV] = INDEXD_BUILD_ID
    env.pop(_DERIVED_WRITER_IDENTITY_BLOCKED_ENV, None)
    env.pop(_DERIVED_ADOPTION_OWNER_TOKEN_ENV, None)
    env.pop(_INDEXD_REFRESH_EXPECTED_WRITER_ENV, None)
    inspect_owner = globals().get("_inspect_indexd_owner")
    if callable(inspect_owner):
        settle_owner = globals().get("_settle_indexd_owner")
        if callable(settle_owner):
            settle_owner(allow_retire=False, retire_budget_s=0.0)
        retire_legacy = globals().get("_retire_legacy_indexd")
        if callable(retire_legacy):
            retire_legacy(allow_retire=False, retire_budget_s=0.0)
        inspected = inspect_owner(current_writer_id=build_id)
        if (inspected.state is _IndexdOwnerState.COMPATIBLE
                and inspected.snapshot is not None):
            body = inspected.snapshot.raw.decode("utf-8", errors="replace")
            token = common._owner_field(body, "token")
            writer = common._owner_field(body, "writer")
            if (writer == build_id and token is not None
                    and re.fullmatch(r"[0-9a-f]{32}", token) is not None):
                env[_DERIVED_ADOPTION_OWNER_TOKEN_ENV] = token
    return env


# A frozen direct Rust launch inherits the exact Python half; the executable
# binds its own bytes at process start. Final combined IDs are launch-scoped and
# must never leak from a parent process into a different binary.
os.environ[_PYTHON_RUNTIME_BUILD_ID_ENV] = INDEXD_BUILD_ID
os.environ.pop("AGREP_RUNTIME_BUILD_ID", None)
os.environ.pop(_DERIVED_WRITER_IDENTITY_BLOCKED_ENV, None)
os.environ.pop(_DERIVED_ADOPTION_OWNER_TOKEN_ENV, None)


def _valid_change_token(value: object) -> bool:
    if not isinstance(value, dict) or len(value) != 1:
        return False
    if "Metadata" in value:
        token = value["Metadata"]
        return (type(token) is int
                and 0 <= token <= 0xFFFF_FFFF_FFFF_FFFF)
    if "ContentSha256" in value:
        token = value["ContentSha256"]
        return (isinstance(token, list) and len(token) == 32
                and all(type(byte) is int and 0 <= byte <= 255
                        for byte in token))
    return False


def _valid_legacy_corpus_proof(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
            "name", "len", "modified_ns", "change_token", "edge_hash"}:
        return False
    return (
        value["name"] == "corpus.db"
        and all(
            type(value[field]) is int
            and 0 <= value[field] <= 0xFFFF_FFFF_FFFF_FFFF
            for field in ("len", "modified_ns", "edge_hash")
        )
        and _valid_change_token(value["change_token"])
    )


def _valid_reader_identity(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {
            "len", "modified_ns", "changed_ns", "device", "inode"}
        and all(
            type(value[field]) is int
            and 0 <= value[field] <= 0xFFFF_FFFF_FFFF_FFFF
            for field in value
        )
    )


def _valid_retained_corpus(value: object) -> bool:
    if (not isinstance(value, dict)
            or set(value) != {"build_id", "proof", "reader_identity"}
            or not isinstance(value["build_id"], str)
            or _BUILD_ID_RE.fullmatch(value["build_id"]) is None
            or not _valid_legacy_corpus_proof(value["proof"])
            or not _valid_reader_identity(value["reader_identity"])):
        return False
    proof = value["proof"]
    reader = value["reader_identity"]
    return (proof["len"] == reader["len"]
            and proof["modified_ns"] == reader["modified_ns"])


def derived_owner_info(
        current_build_id: str | None = None) -> DerivedOwnerInfo:
    """Read the Rust-published derived-store owner without following links.

    Python never replaces this anchor. Its proofs bind either one legacy
    adoption or one foreign snapshot retained until atomic replacement.
    """
    try:
        observed = ownerfile.snapshot(
            DERIVED_OWNER_PATH, max_bytes=_DERIVED_OWNER_MAX_BYTES)
        record = json.loads(
            observed.raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object)
    except FileNotFoundError:
        return DerivedOwnerInfo("absent", None, None, "")
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return DerivedOwnerInfo(
            "unavailable", None, None,
            f"derived-store ownership record {DERIVED_OWNER_PATH} "
            f"is unreadable: {exc}")
    allowed = {
        "version", "build_id", "legacy_corpus_db", "retained_corpus_db"}
    if (not isinstance(record, dict)
            or not {"version", "build_id"} <= set(record) <= allowed
            or record.get("version") != _DERIVED_OWNER_VERSION
            or not isinstance(record.get("build_id"), str)
            or _BUILD_ID_RE.fullmatch(record["build_id"]) is None
            or (record.get("legacy_corpus_db") is not None
                and record.get("retained_corpus_db") is not None)
            or ("legacy_corpus_db" in record
                and record["legacy_corpus_db"] is not None
                and not _valid_legacy_corpus_proof(
                    record["legacy_corpus_db"]))
            or ("retained_corpus_db" in record
                and record["retained_corpus_db"] is not None
                and not _valid_retained_corpus(
                    record["retained_corpus_db"]))):
        return DerivedOwnerInfo(
            "unavailable", None, None,
            f"derived-store ownership record {DERIVED_OWNER_PATH} is malformed")
    owner = record["build_id"]
    proof = record.get("legacy_corpus_db")
    retained = record.get("retained_corpus_db")
    current = current_build_id or derived_writer_build_id()
    if owner != current:
        return DerivedOwnerInfo(
            "foreign", owner, proof,
            f"derived stores owned-by {owner}; this build is {current}", retained)
    return DerivedOwnerInfo("current", owner, proof, "", retained)


def _stable_regular_prefix(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes from one unchanged no-follow regular file."""
    before = fileops.file_identity(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        if fileops.file_identity_fd(fd) != before:
            raise OSError(f"parse cache changed before ownership probe: {path}")
        chunks = []
        remaining = limit
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if (fileops.file_identity_fd(fd) != before
                or fileops.file_identity(path) != before):
            raise OSError(f"parse cache changed during ownership probe: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def ingest_cache_owner_info(
        current_build_id: str | None = None) -> IngestCacheOwnerInfo:
    """Probe only the fixed parse-cache ownership prefix, never its payload."""
    current = current_build_id or derived_writer_build_id()
    try:
        header = _stable_regular_prefix(
            INGEST_CACHE_PATH, _INGEST_CACHE_OWNER_BYTES)
    except FileNotFoundError:
        return IngestCacheOwnerInfo("absent", None, "")
    except OSError:
        return IngestCacheOwnerInfo(
            "unavailable", None,
            f"parse cache ownership is unreadable at {INGEST_CACHE_PATH}")

    # This exactly follows Rust's bounded CacheOwnerProbe: pre-owner formats
    # (including the pre-framed legacy body) are adoption candidates.
    if header[12:20] != _INGEST_CACHE_BASE_MAGIC:
        return IngestCacheOwnerInfo("legacy", None, "")
    if len(header) < _INGEST_CACHE_OWNER_OFFSET:
        return IngestCacheOwnerInfo(
            "malformed", None,
            f"parse cache ownership is malformed at {INGEST_CACHE_PATH}")
    storage_version = int.from_bytes(header[20:24], "little")
    if storage_version in _INGEST_CACHE_LEGACY_VERSIONS:
        return IngestCacheOwnerInfo("legacy", None, "")
    if storage_version < _INGEST_CACHE_OWNER_VERSION:
        return IngestCacheOwnerInfo(
            "malformed", None,
            f"parse cache ownership is malformed at {INGEST_CACHE_PATH}")
    raw_owner = header[
        _INGEST_CACHE_OWNER_OFFSET:
        _INGEST_CACHE_OWNER_OFFSET + 20
    ]
    try:
        owner = raw_owner.decode("ascii")
    except UnicodeDecodeError:
        owner = ""
    if len(raw_owner) != 20 or _BUILD_ID_RE.fullmatch(owner) is None:
        return IngestCacheOwnerInfo(
            "malformed", None,
            f"parse cache ownership is malformed at {INGEST_CACHE_PATH}")
    if owner != current:
        return IngestCacheOwnerInfo(
            "foreign", owner,
            f"parse cache owned-by {owner}; this build is {current}")
    return IngestCacheOwnerInfo("current", owner, "")


def derived_mutation_info(
        current_build_id: str | None = None) -> DerivedMutationInfo:
    """Compose the bounded durable-owner probes before any derived mutation.

    The explicit Rust anchor and fixed parse-cache owner prefix are the
    cross-build fence available below corpusdb.  Missing stores and pre-owner
    legacy caches retain their one-time adoption behavior; malformed,
    unreadable, and foreign ownership fail closed before a daemon claim, log,
    marker, or child can be published.
    """
    current = current_build_id or derived_writer_build_id()
    anchor = derived_owner_info(current)
    cache = ingest_cache_owner_info(current)
    if anchor.state in {"foreign", "unavailable"}:
        return DerivedMutationInfo(
            anchor.state, anchor.build_id, anchor.reason)
    if cache.state in {"foreign", "malformed", "unavailable"}:
        state = "foreign" if cache.state == "foreign" else "unavailable"
        return DerivedMutationInfo(state, cache.build_id, cache.reason)
    if anchor.state == "current":
        if cache.state not in {"absent", "current"}:
            return DerivedMutationInfo(
                "unavailable", anchor.build_id,
                f"derived stores owned-by {anchor.build_id}, but the existing "
                f"parse cache is {cache.state} and has no matching "
                "writing-build identity")
        return DerivedMutationInfo("current", current, "")
    if cache.state == "current":
        return DerivedMutationInfo("current", current, "")
    if cache.state == "legacy":
        return DerivedMutationInfo("legacy", None, "")
    return DerivedMutationInfo("absent", None, "")


def derived_writer_mutation_info(
        current_build_id: str | None = None, *,
        allow_legacy_adoption: bool = False) -> DerivedMutationInfo:
    """Extend the low owner/cache fence through the derived DB publication.

    corpusdb owns SQLite-format probing, so writer entrypoints call it lazily
    without a module cycle. Explicit DB ownership always wins. Only callers
    launching the Rust ingest may opt into ownerless legacy adoption; semantic
    and daemon coordination writers require a current family (or a truly empty
    store with nothing yet to own).
    """
    current = current_build_id or derived_writer_build_id()
    low = derived_mutation_info(current)
    if not low.writable:
        return low
    try:
        import corpusdb
        db_state, db_owner, db_error = corpusdb._database_build_id()
    except Exception as exc:  # noqa: BLE001 -- writer preflight fails closed
        return DerivedMutationInfo(
            "unavailable", None,
            f"derived database ownership cannot be verified: "
            f"{type(exc).__name__}: {exc}")
    if (db_state == "owned" and db_owner != current
            and low.state != "current"):
        return DerivedMutationInfo(
            "foreign", db_owner,
            f"corpus.db owned-by {db_owner}; this build is {current}")
    if low.state != "current":
        if db_state == "unavailable":
            return DerivedMutationInfo(
                "unavailable", None,
                str(db_error)
                if db_error else "corpus.db ownership cannot be verified")
        if (allow_legacy_adoption
                and db_state in {"absent", "unowned"}):
            return low
        # A truly empty store has no family to conflict with. Once any legacy
        # derived publication exists, only the Rust adoption path may establish
        # its durable owner.
        if low.state == "absent" and db_state == "absent":
            return low
        return DerivedMutationInfo(
            "unavailable", None,
            "derived-store ownership is not established for this writer")
    # A durable current anchor delegates damaged/busy database states to
    # corpusdb's structured repair fence. That layer distinguishes a
    # same-family replaceable database from a moving or foreign publication.
    try:
        ownership = corpusdb._derived_write_ownership(for_write=True)
    except Exception as exc:  # noqa: BLE001 -- writer preflight fails closed
        return DerivedMutationInfo(
            "unavailable", None,
            f"derived database ownership cannot be verified: "
            f"{type(exc).__name__}: {exc}")
    if ownership.writable:
        return low
    if db_state == "owned" and db_owner != current:
        return DerivedMutationInfo(
            "foreign", db_owner,
            f"corpus.db owned-by {db_owner}; this build is {current}")
    reason = str(
        ownership.reason
        or "derived database ownership cannot authorize this writer")
    # corpusdb's structured state is authoritative. Do not reverse-classify
    # its human disclosure text to invent a more specific owner/state here.
    return DerivedMutationInfo(
        "unavailable", None, reason, journal_blocked=ownership.journal_blocked)


def explicit_index_declined() -> bool:
    """True when the ingest just declined the derived stores; emits the one line saying so.

    A declined ingest exits 0 because a read-only pass is the right outcome for the
    pre-search freshen. An explicit index must not inherit that silence: both index
    surfaces read the ownership artifacts the ingest itself decided from, so neither
    prints an elapsed time over work that did not happen. The cause is the ingest's
    line to say and it already said it; this one adds only the verdict.
    """
    if derived_writer_mutation_info().writable:
        return False
    common.log("indexing declined; nothing was rebuilt")
    return True


SEARCH_INDEX_REFRESH_UNSUPPORTED_RC = 3


def refresh_search_index(quiet: bool = True) -> bool | None:
    """Rebuild the derived FTS db now so the next search doesn't pay for it.

    True means refreshed/current, None means this SQLite lacks trigram FTS (the
    supported JSONL fallback), and False means a real refresh failure. Callers decide
    whether that failure is retryable or fatal. `quiet=False` (interactive callers)
    announces before a build that will actually take time - connect() can chew
    a large corpus for minutes and must never do so silently at a prompt.
    """
    try:
        import corpusdb  # lazy: only the CLI keyword paths reach here, and it imports us
        if not corpusdb._trigram_ok():
            return None
        db_path = common.DATA_DIR / "corpus.db"
        if not quiet and (not db_path.exists() or not common.MESSAGES_PATH.exists()
                          or db_path.stat().st_mtime < common.MESSAGES_PATH.stat().st_mtime):
            # The size of this history is not measured here, so the line warns
            # about what a first build CAN cost instead of asserting the box
            # has a large one - a two-message corpus was told otherwise.
            common.log("building the search db (a first build can take a while "
                "on a large history) …" if not db_path.exists() else
                "refreshing the search db …")
        db = corpusdb.connect(quiet=True)
        if db:
            try:
                meta = dict(db.execute(
                    "SELECT key, value FROM meta WHERE key IN "
                    "('stamp', 'schema', 'fts_triggers', 'build_id')"))
                current = (
                    meta.get("stamp") == corpusdb._stamp()
                    and meta.get("schema") == corpusdb._SCHEMA
                    and meta.get("fts_triggers") == corpusdb._TRIGGER_SCHEMA
                    and meta.get("build_id") == derived_writer_build_id(
                        require_binary=True)
                )
            finally:
                db.close()
            if current and not corpusdb.query_rebuild_required():
                return True
        # A foreign owner is a deliberate read-only outcome, not a failed
        # refresh. AutoIndexer must clear its retry streak instead of escalating
        # a correctly fenced build to --full.
        if not corpusdb._derived_write_ownership(for_write=True).writable:
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        common.log(f"search-index refresh failed: {type(exc).__name__}: {exc}")
        return False


def search_index_refresh_child_exit() -> int:
    """Translate the tri-state refresh result for the owned daemon child."""
    expected = _INDEXD_REFRESH_EXPECTED_WRITER
    if _BUILD_ID_RE.fullmatch(expected) is None:
        common.log("search-index refresh child has no valid parent writer identity")
        return 1
    try:
        import corpusdb  # noqa: F401 -- freeze the module before runtime proof
        assert_python_runtime_unchanged()
        current = derived_writer_build_id(require_binary=True)
    except OSError as exc:
        common.log(f"search-index refresh child cannot verify its writer: {exc}")
        return 1
    if current != expected:
        common.log(
            "search-index refresh child runtime changed before start; "
            "the request remains queued")
        return 1
    refreshed = refresh_search_index()
    try:
        assert_python_runtime_unchanged()
        current = derived_writer_build_id(require_binary=True)
    except OSError as exc:
        common.log(f"search-index refresh child lost its writer proof: {exc}")
        return 1
    if current != expected:
        common.log(
            "search-index refresh child runtime changed during publication; "
            "the request remains queued")
        return 1
    if refreshed is None:
        return SEARCH_INDEX_REFRESH_UNSUPPORTED_RC
    if not refreshed:
        return 1
    ownership = derived_writer_mutation_info(expected)
    if ownership.state != "current" or ownership.build_id != expected:
        common.log(
            "search-index refresh child did not retain the parent writer "
            f"identity: {ownership.reason or ownership.state}")
        return 1
    return 0


def _data_dir_readonly() -> bool:
    """AGREP_DATA_READONLY names one protected data dir (a gate run guarding
    the box's live corpus); writes to any other dir - sandboxes - proceed."""
    return common.data_dir_readonly(common.DATA_DIR)


def _same_build_adoption_claim(build_id: str) -> bool:
    """Whether a first-use writer of this build owns the pre-publication fence."""
    found = False
    for path in (LEGACY_INDEXD_LOCK_PATH, INDEXD_LOCK_PATH):
        try:
            observed = ownerfile.snapshot(path, max_bytes=_INDEXD_OWNER_MAX_BYTES)
        except (FileNotFoundError, OSError):
            continue
        body = observed.raw.decode("utf-8", errors="replace")
        token = common._owner_field(body, "token")
        if (observed.raw.endswith(b"\n")
                and common._owner_field(body, "state") == "derived-adoption"
                and common._owner_field(body, "writer") == build_id
                and token is not None
                and re.fullmatch(r"[0-9a-f]{32}", token) is not None):
            found = True
            continue
        return False
    return found


# How long an explicit index waits for a live daemon of this exact build to
# adopt the derived stores after an upgrade; the takeover lands with the
# daemon's first publication pass, observed in the tens of seconds.
_UPGRADE_SETTLEMENT_WAIT_S = 45.0


def _await_upgrade_settlement(timeout_s: float) -> bool:
    """Wait while this build's own daemon adopts the derived stores.

    True once this writer may publish. Only a live freshness owner of this
    exact build (or its adoption fence) is worth waiting on - a foreign
    co-install has no such daemon and fails immediately, as before."""
    try:
        writer = derived_writer_build_id(require_binary=True)
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if derived_writer_mutation_info().writable:
            return True
        inspected = _inspect_indexd_owner(current_writer_id=writer)
        if (inspected.state is not _IndexdOwnerState.COMPATIBLE
                and not _same_build_adoption_claim(writer)):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return derived_writer_mutation_info().writable
        time.sleep(min(0.5, remaining))


def _await_first_publication(build_id: str, *, timeout_s: float) -> bool:
    """Wait briefly for the same-build cold writer instead of racing its empty state."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if not _same_build_adoption_claim(build_id):
            return _first_publication_committed(build_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(0.025, remaining))


def _first_publication_committed(build_id: str) -> bool:
    """Verify the cold writer reached its final source and derived commit boundary."""
    source = common.DATA_DIR / ".source_snapshot.bin"
    pending = common.DATA_DIR / ".ingest_pending.bin"
    if _same_build_adoption_claim(build_id):
        return False
    try:
        pending.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    try:
        observed = source.lstat()
    except OSError:
        return False
    if (not stat.S_ISREG(observed.st_mode) or source.is_symlink()
            or observed.st_size <= 0):
        return False
    if derived_mutation_info(build_id).state != "current":
        return False
    try:
        import corpusdb
        health = corpusdb._derived_publication_health()
    except Exception:  # noqa: BLE001 -- a cold join fails closed on proof errors
        return False
    if health.get("state") != "ready":
        return False
    try:
        source_after = source.lstat()
    except OSError:
        return False
    if source_after != observed:
        return False
    try:
        pending.lstat()
    except FileNotFoundError:
        return not _same_build_adoption_claim(build_id)
    except OSError:
        return False
    else:
        return False


def _captured_ingest_failure(result: subprocess.CompletedProcess) -> str:
    detail = common.one_line(str(getattr(result, "stderr", "") or "")).strip()
    return f": {common.terminal_safe(detail[:300])}" if detail else ""


def build_index(
        quiet: bool = False, delegate_fts: bool = False,
        require_search_index: bool = False) -> bool:
    """Run the Rust ingest over every agent store, then refresh the derived FTS db.

    The single ingest invocation shared by `agrep index`/`ui` (a forced rebuild),
    ensure_index() (build-on-first-use), and the CLI's pre-search freshen. `quiet`
    captures the ingest's stdout/stderr (the freshen path runs on every search and
    must stay silent when nothing changed); the loud path streams progress as before.
    Assumes the binary exists - callers that might not have it check
    ingest_bin().exists() first. Returns True on success.

    `delegate_fts` (first run only): hand the derived-FTS build to the indexd
    daemon instead of building inline. A cold trigram build over a large corpus
    takes minutes; the first search must not sit silently behind it. This process
    serves its search via the JSONL scan (corpusdb.connect sees the delegation
    flag and declines), and searches find the published db once indexd lands it.
    `require_search_index` marks the explicit index/setup callers. They also
    hand the FTS build to indexd - the corpus is published and served the
    moment ingest returns, so holding the terminal for minutes buys nothing -
    and return False only when that handoff degraded to a failed inline build.
    Read paths keep the JSONL availability floor when only the derived database
    refresh fails.
    """
    if _data_dir_readonly():
        common.dbg("build_index: AGREP_DATA_READONLY covers this data dir -> no writes")
        return False
    kw = ({"capture_output": True, "text": True, "encoding": "utf-8",
           "errors": "replace"} if quiet else {})
    # quiet = console-less caller (the daemon): suppress agrep-rs's conhost flash.
    if quiet and common.WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    ingest = common.ingest_bin()
    # Callers normally prove the binary exists. The missing-path branch preserves
    # launch failure semantics without inventing an identity; every real executable
    # is bound to its exact Python+Rust build before launch.
    try:
        kw["env"] = (
            rust_writer_env(ingest) if ingest.exists() else dict(os.environ))
    except OSError as fenced:
        # An unreadable ownership anchor is a local condition with a cause to
        # name, never an unexpected error for the caller's crash handler.
        common.log(f"indexing declined; {_fenced_reason(fenced)}")
        return False
    cmd = [str(ingest), "index", "--agent", "all"]
    common.dbg(
        f"ingest exec: {' '.join(cmd)} (cwd={common.REPO_ROOT})", ">")
    common._close_event_reader()
    t = time.time()
    census_before, unreadable_before = _census_map(
        _store_census(timeout_s=30.0))
    digests_before = (
        _store_digests(census_before, _store_paths_census(timeout_s=30.0))
        if census_before is not None else {})
    before = common.MESSAGES_PATH.stat().st_size if common.MESSAGES_PATH.exists() else 0
    r = subprocess.run(cmd, cwd=str(common.REPO_ROOT), **kw)
    dt = (time.time() - t) * 1000.0
    if r.returncode == 0:
        build_id = str(kw["env"].get("AGREP_RUNTIME_BUILD_ID") or "")
        if build_id and _same_build_adoption_claim(build_id):
            common.dbg("cold ingest joined an existing same-build publication")
            if not _await_first_publication(
                    build_id, timeout_s=_INDEXD_STARTUP_GRACE_S):
                common.log(
                    "another first-run indexer is still publishing; retry this search")
                return False
        elif not common.MESSAGES_PATH.exists():
            common.log(
                "indexing declined before publishing a searchable corpus"
                f"{_captured_ingest_failure(r)}")
            return False
    after = common.MESSAGES_PATH.stat().st_size if common.MESSAGES_PATH.exists() else 0
    grew = f", messages.jsonl {before}->{after}B ({'+' if after >= before else ''}{after - before})"
    common.dbg(f"ingest done: rc={r.returncode} in {dt:.0f}ms{grew}", "<" if r.returncode == 0 else "!")
    if r.returncode != 0:
        if quiet:
            common.log(
                f"indexing failed with exit {r.returncode}"
                f"{_captured_ingest_failure(r)}")
        return False
    if require_search_index and not derived_writer_mutation_info().writable:
        # An upgrade's takeover republishes under the new writer within one
        # daemon pass; an explicit index landing inside that window waits for
        # the settlement and reruns the ingest instead of failing a healthy box.
        if _await_upgrade_settlement(_UPGRADE_SETTLEMENT_WAIT_S):
            r = subprocess.run(cmd, cwd=str(common.REPO_ROOT), **kw)
            common.dbg(
                f"ingest rerun after writer settlement: rc={r.returncode}",
                "<" if r.returncode == 0 else "!")
        if r.returncode != 0 or explicit_index_declined():
            # a declined ingest published nothing for the census below to vouch for
            return False
    if census_before is not None and not unreadable_before:
        # The pre-pass census under-claims what the ingest consumed, so a
        # green verdict from this record can never cover unconsumed changes.
        # An unreadable store means the census cannot vouch: no record.
        record_verified_current(census_before, wall=t, digests=digests_before)
    streak, _ = indexd_failing()
    if streak:
        # A successful explicit ingest ends the failure streak, or every
        # surface keeps telling a failing-daemon story the user just fixed.
        record_auto_index_health(0, "")
    if delegate_fts:
        _set_fts_delegated(_delegate_fts_build())
    elif require_search_index:
        # explicit index: hand the multi-minute FTS build to its owner rather
        # than hold the caller, and still answer for the outcome
        if not hand_off_search_index():
            return False
    else:
        refreshed = refresh_search_index(quiet=quiet)
        if require_search_index and refreshed is False:
            return False
    kick_semantic_bootstrap()
    try:
        import indexer
        indexer.run_post_index_hooks()
    except Exception as e:  # noqa: BLE001 -- enrichment cannot wound a good index
        common.log(f"post_index hooks unavailable: {type(e).__name__}: {e}")
    return True


def kick_semantic_bootstrap() -> None:
    """Start warming a stale meaning lane the moment an index publishes.

    Nothing used to schedule embeddings until the FIRST SEMANTIC QUERY hit
    the stale lane (or the daemon's later loop), so every fresh box answered
    its opening searches "meaning unavailable; keyword-only" - and an agent
    reading that hedged zero re-probes, rationally. The kick is detached and
    deduped by embed.py's claim; a missing model is never fetched from here
    (first use owns that consent), so this is silent and cheap when declined.
    """
    try:
        import semantic
        # ensure_fresh_async owns the offline refusal (typed model-not-cached);
        # a model-cached pre-check here would keep two divergent copies.
        coherence = semantic.embedding_coherence()
        if coherence.get("coherent") and not coherence.get("migration_pending"):
            return
        semantic.ensure_fresh_async(max_new=semantic.SEMANTIC_BOOTSTRAP_MAX_NEW)
    except Exception:  # noqa: BLE001 -- warming can never wound a good index
        pass


def finish_streamed_index(*, allow_inline_fallback: bool = True) -> None:
    """Post-ingest tail for the streamed first-run search (search.py runs the
    `--emit-rows` ingest itself): the same follow-through build_index does after
    its subprocess returns - FTS handed to indexd, then the enrichment hooks.

    A complete bounded page may durably queue the expensive FTS fallback when
    daemon startup fails; underfilled pages still need the exact indexed lane."""
    if _data_dir_readonly():
        common.dbg(
            "streamed-index tail skipped: AGREP_DATA_READONLY protects this data dir")
        return
    index_outcome = _run_search_index_build(
        allow_inline_fallback=allow_inline_fallback)
    _set_fts_delegated(index_outcome in _DELEGATING_OUTCOMES)
    kick_semantic_bootstrap()
    try:
        import indexer
        indexer.run_post_index_hooks()
    except Exception as e:  # noqa: BLE001 -- enrichment cannot wound a good index
        common.log(f"post_index hooks unavailable: {type(e).__name__}: {e}")


# this process handed the FTS build to indexd -> corpusdb.connect declines to build
# inline (other processes defer via the freshener check).
_fts_delegated = False
_fts_delegated_at = 0.0
# set when delegation degraded to an inline build that then failed
_inline_refresh_failed = False


def _set_fts_delegated(active: bool) -> None:
    global _fts_delegated, _fts_delegated_at
    _fts_delegated = bool(active)
    _fts_delegated_at = time.monotonic() if active else 0.0


def fts_delegation_active() -> bool:
    if not _fts_delegated:
        return False
    if time.monotonic() - _fts_delegated_at < _SPAWN_GUARD_S:
        return True
    inspected = _inspect_indexd_owner()
    if inspected.state in _INDEXD_DELEGATING_STATES:
        return True
    if not _retire_legacy_indexd():
        return True
    _set_fts_delegated(False)
    return False


_SEARCH_INDEX_REQUEST = ".search_index_request"
_SEARCH_INDEX_REQUEST_MAX_BYTES = 1024
# Both outcomes leave the build outside this process; only the command whose
# job IS the build distinguishes them.
_DELEGATING_OUTCOMES = frozenset({
    surface.IndexBuildOutcome.DELEGATED, surface.IndexBuildOutcome.BLOCKED})


def _search_index_request_path() -> Path:
    return common.DATA_DIR / _SEARCH_INDEX_REQUEST


def request_search_index_build() -> bool:
    """Durably queue the derived FTS build for whichever daemon generation
    serves next. False means nothing was queued, so nothing may be claimed.

    A delegated build has to be a work item the daemon polls, not merely a
    process that exists: nothing else in its loop schedules a MISSING
    corpus.db - an unbuilt db is not damage, and this moves no watched store."""
    path = _search_index_request_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"requested": time.time()}), encoding="utf-8")
        common.replace_with_retry(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def search_index_build_pending() -> bool:
    """Whether a valid durable FTS build request is waiting to be served."""
    try:
        ownerfile.snapshot(
            _search_index_request_path(),
            max_bytes=_SEARCH_INDEX_REQUEST_MAX_BYTES)
    except OSError:
        return False
    return True


def serve_search_index_request(
        refresh: Callable[[], bool | None] | None = None) -> bool:
    """Daemon tick: run a queued FTS build and retire exactly that request.

    A request outlives the daemon that failed to finish it - it is only
    unlinked after a build that did not fail, and never when a newer request
    landed under this one."""
    path = _search_index_request_path()
    try:
        before = ownerfile.snapshot(
            path, max_bytes=_SEARCH_INDEX_REQUEST_MAX_BYTES)
    except OSError:
        return False
    if not derived_writes_permitted():
        return False
    run_refresh = refresh_search_index if refresh is None else refresh
    if run_refresh() is False:
        return False
    try:
        ownerfile.remove_exact(
            path, before, tombstone=True, require_stable_mtime=True)
    except OSError:
        pass
    return True


def _blocked_build_cause() -> str:
    """Which blocking condition refused the spawn, in the policy's vocabulary."""
    if _data_dir_readonly():
        return "data-readonly"
    if removal_fence.background_removal_active():
        return "removal"
    if not derived_writes_permitted():
        return "foreign-owner"
    return "blocked-owner"


def _inline_fts_build(reason: str) -> surface.IndexBuildOutcome:
    """Build the FTS here and report which of the three things happened.

    `quiet=False`: the announcement is conditional on a build that will
    actually cost something, which is the only honest version of this line."""
    global _inline_refresh_failed
    refreshed = refresh_search_index(quiet=False)
    if refreshed is False:
        _inline_refresh_failed = True
        _set_freshen_failure("inline-refresh-failed", reason)
        return surface.IndexBuildOutcome.FAILED
    if refreshed is None:
        return surface.IndexBuildOutcome.UNSUPPORTED
    return surface.IndexBuildOutcome.BUILT


def _search_db_state() -> str:
    """What the derived FTS lane holds for the published corpus right now:
    `current` (nothing to build), `stale` (a build is owed), or `unsupported`
    (this SQLite has no trigram FTS, so the JSONL lane is the design)."""
    db = None
    try:
        import corpusdb
        if not corpusdb._trigram_ok():
            return "unsupported"
        db = corpusdb._valid_db(corpusdb._stamp(), 1000)
    except Exception:  # noqa: BLE001 -- an unverifiable db is one to rebuild
        return "stale"
    if db is None:
        return "stale"
    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass
    return "current"


def _delegate_fts_build(announce: bool = True) -> bool:
    """First run: hand the multi-minute FTS build to the indexd daemon (the
    process that owns index freshness anyway) instead of blocking the first
    search behind it. True means background/protected work owns the build;
    False means this process completed the inline fallback.

    The read paths need only that much: they are serving results either way.
    Inline failure stays readable via inline_refresh_failed() so
    `require_search_index` keeps its verdict."""
    outcome = _run_search_index_build(announce=announce)
    return outcome in _DELEGATING_OUTCOMES


def _run_search_index_build(
        announce: bool = True, *,
        allow_inline_fallback: bool = True) -> surface.IndexBuildOutcome:
    """Get the derived FTS build owned: queued to the daemon, or run here.

    Returns what actually happened. A reader that cannot delegate still gets
    its results, so BLOCKED earns no line here - the command whose only job is
    the build renders that refusal itself."""
    global _inline_refresh_failed
    _inline_refresh_failed = False
    if os.environ.get("AGREP_NO_DAEMON"):
        return _inline_fts_build(
            "AGREP_NO_DAEMON is set and inline search-index refresh failed")
    spawned = _reclassify_indexd_spawn_failure(_spawn_indexd())
    if spawned in {
            _IndexdSpawnResult.READY,
            _IndexdSpawnResult.IN_FLIGHT,
    }:
        if request_search_index_build():
            if announce:
                common.log(surface.BACKGROUND_INDEX_BUILD_LINE)
            return surface.IndexBuildOutcome.DELEGATED
        # nothing was queued, so nothing may be promised: build it here
        return _inline_fts_build(
            "the background search-index build could not be queued and the "
            "inline refresh failed")
    if spawned is _IndexdSpawnResult.BLOCKED:
        _set_freshen_failure(
            "blocked-owner", "the freshness owner is blocked")
        return surface.IndexBuildOutcome.BLOCKED
    if not allow_inline_fallback and request_search_index_build():
        common.dbg(
            "background indexing failed to start; the full streamed page "
            "queued the optional search-index build for the next owner", "!")
        _clear_own_spawn_guard(force=True)
        return surface.IndexBuildOutcome.BLOCKED
    common.log(surface.INLINE_INDEX_BUILD_LINE)
    try:
        return _inline_fts_build(
            "background indexing failed to start and inline search-index "
            "refresh failed")
    finally:
        _clear_own_spawn_guard(force=True)


def inline_refresh_failed() -> bool:
    """Whether the last delegation attempt fell back inline and that build failed."""
    return _inline_refresh_failed


def _keep_freshness_owner_running() -> None:
    """Start the daemon without asking it for a build: it owns freshness from
    here whether or not this run left the search database anything to do."""
    if not os.environ.get("AGREP_NO_DAEMON"):
        _reclassify_indexd_spawn_failure(_spawn_indexd())


def explicit_index_outcome() -> tuple[surface.IndexBuildOutcome, str]:
    """What an explicit `agrep index` did about the search database, and why,
    when nothing can build it. A db that already serves this corpus is not a
    background build, and must never be reported as one."""
    state = _search_db_state()
    if state in ("current", "unsupported"):
        _keep_freshness_owner_running()
        return (surface.IndexBuildOutcome.CURRENT if state == "current"
                else surface.IndexBuildOutcome.UNSUPPORTED), ""
    outcome = _run_search_index_build(announce=False)
    return outcome, (_blocked_build_cause()
                     if outcome is surface.IndexBuildOutcome.BLOCKED else "")


# How long an explicit index waits out a daemon owner-record beat before
# reporting the block; takeover/adoption transitions settle inside the
# publication grace, so this comfortably covers a healthy box.
_EXPLICIT_INDEX_OWNER_WAIT_S = 5.0


def hand_off_search_index() -> bool:
    """Explicit `agrep index`: return once the corpus is published and served,
    leaving the derived FTS build to the daemon that will run it.

    The line and the exit status are both rendered from what this run did, so
    a build nobody owns can never read as a delegated one."""
    global _inline_refresh_failed
    _inline_refresh_failed = False
    outcome, cause = explicit_index_outcome()
    if outcome is surface.IndexBuildOutcome.BLOCKED and cause == "blocked-owner":
        # A daemon takeover or adoption rewrites its owner records in a short
        # beat; an explicit index landing inside it retries until the beat
        # settles instead of failing a healthy box.
        deadline = time.monotonic() + _EXPLICIT_INDEX_OWNER_WAIT_S
        while (outcome is surface.IndexBuildOutcome.BLOCKED
               and cause == "blocked-owner"
               and time.monotonic() < deadline):
            time.sleep(0.5)
            outcome, cause = explicit_index_outcome()
    _set_fts_delegated(outcome is surface.IndexBuildOutcome.DELEGATED)
    line = surface.index_build_line(outcome, cause, cli=_cli_invocation_name())
    if line:
        common.log(line)
    return surface.index_build_succeeded(outcome)


def _cli_invocation_name() -> str:
    """The name this install answers to, so a remedy is copyable where it prints."""
    try:
        import console
        import dist
        return console.shell_command(
            *dist.cli_invocation(), fallback="agrep") or "agrep"
    except Exception:  # noqa: BLE001 -- a remedy must render on any install
        return "agrep"


class _IndexdOwnerState(Enum):
    ABSENT = "absent"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    ORPHANED_GROUP = "orphaned-group"
    DEAD = "dead"
    REUSED = "reused"
    UNVERIFIABLE = "unverifiable"
    MALFORMED_FRESH = "malformed-fresh"
    MALFORMED_STALE = "malformed-stale"
    HOSTILE = "hostile"


_INDEXD_RECLAIMABLE_STATES = frozenset({
    _IndexdOwnerState.DEAD,
    _IndexdOwnerState.REUSED,
    _IndexdOwnerState.MALFORMED_STALE,
})
_INDEXD_EXACT_STATES = frozenset({
    _IndexdOwnerState.COMPATIBLE,
    _IndexdOwnerState.INCOMPATIBLE,
})
_INDEXD_UNRETIRABLE_STATES = frozenset({
    _IndexdOwnerState.HOSTILE,
    _IndexdOwnerState.UNVERIFIABLE,
    _IndexdOwnerState.MALFORMED_FRESH,
})
_INDEXD_INLINE_BLOCKING_STATES = frozenset({
    *_INDEXD_UNRETIRABLE_STATES,
    _IndexdOwnerState.INCOMPATIBLE,
    _IndexdOwnerState.ORPHANED_GROUP,
})
_INDEXD_DELEGATING_STATES = frozenset({
    *_INDEXD_INLINE_BLOCKING_STATES,
    _IndexdOwnerState.COMPATIBLE,
})
_INDEXD_STARTING_STATES = frozenset({
    _IndexdOwnerState.COMPATIBLE,
    _IndexdOwnerState.ORPHANED_GROUP,
})


class _IndexdOwnerInspection(NamedTuple):
    state: _IndexdOwnerState
    snapshot: ownerfile.Snapshot | None
    pid: int | None
    process_start: str | None


def indexd_generation_token(owner_snapshot: ownerfile.Snapshot) -> str:
    body = owner_snapshot.raw.decode("utf-8", errors="replace")
    token = common._owner_field(body, "token")
    if token is None or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ownerfile.OwnershipLost("indexd owner has no valid generation token")
    return token


def _indexd_has_exact_tree(owner_snapshot: ownerfile.Snapshot) -> bool:
    if not common.WIN:
        return False
    body = owner_snapshot.raw.decode("utf-8", errors="replace")
    return common._owner_field(body, "tree") == common.WINDOWS_DESCENDANT_TREE


def indexd_child_path(owner_snapshot: ownerfile.Snapshot) -> Path:
    token = indexd_generation_token(owner_snapshot)
    return INDEXD_CHILD_PATH.with_name(f"{INDEXD_CHILD_PATH.name}.{token}")


class _IndexdChildRecord(NamedTuple):
    path: Path
    snapshot: ownerfile.Snapshot
    guard: int
    guard_start: str
    target: int
    target_start: str
    group: int
    valid: bool


def _read_indexd_child(
        owner_snapshot: ownerfile.Snapshot) -> _IndexdChildRecord:
    token = indexd_generation_token(owner_snapshot)
    path = indexd_child_path(owner_snapshot)
    child = ownerfile.snapshot(path, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    body = child.raw.decode("utf-8", errors="replace")
    try:
        guard = int(common._owner_field(body, "guard") or "")
        target = int(common._owner_field(body, "target") or "")
        group = int(common._owner_field(body, "group") or "")
    except ValueError:
        guard = target = group = 0
    guard_start = common._owner_field(body, "guard_start") or ""
    target_start = common._owner_field(body, "target_start") or ""
    valid = (
        child.raw.endswith(b"\n")
        and common._owner_field(body, "owner") == token
        and 0 < guard <= common._MAX_PROCESS_ID
        and 0 < target <= common._MAX_PROCESS_ID
        and group == target
        and guard_start not in ("", "None", "unknown")
        and target_start not in ("", "None", "unknown")
    )
    return _IndexdChildRecord(
        path, child, guard, guard_start, target, target_start, group, valid)


def _remove_indexd_child(record: _IndexdChildRecord) -> bool:
    return ownerfile.remove_exact(
        record.path, record.snapshot, tombstone=True,
        require_stable_mtime=True)


def _indexd_child_active(
        owner_snapshot: ownerfile.Snapshot, *, settle: bool = True,
) -> bool | None:
    try:
        record = _read_indexd_child(owner_snapshot)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if not record.valid:
        return None
    guard_owner = ownerfile.classify_process(
        record.guard, record.guard_start, pid_alive=common.pid_alive,
        process_start=lambda owner_pid: common.process_start_identity(owner_pid))
    target_owner = ownerfile.classify_process(
        record.target, record.target_start, pid_alive=common.pid_alive,
        process_start=lambda owner_pid: common.process_start_identity(owner_pid))
    if (guard_owner is ownerfile.ProcessOwner.EXACT_LIVE
            or target_owner is ownerfile.ProcessOwner.EXACT_LIVE):
        return True
    if (guard_owner is ownerfile.ProcessOwner.UNVERIFIABLE
            or target_owner is ownerfile.ProcessOwner.UNVERIFIABLE):
        return None
    target_reused = target_owner is ownerfile.ProcessOwner.REUSED
    present = common._owner_group_active(
        record.group, record.target, target_reused)
    if target_reused:
        if present is not False:
            return None
        if not settle:
            return False
        return False if _remove_indexd_child(record) else None
    if present is not False:
        return present
    if not settle:
        return False
    return False if _remove_indexd_child(record) else None


def _retire_indexd_child(
        owner_snapshot: ownerfile.Snapshot, *, wait_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, wait_s)
    try:
        record = _read_indexd_child(owner_snapshot)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not record.valid:
        return False
    guard_owner = ownerfile.classify_process(
        record.guard, record.guard_start, pid_alive=common.pid_alive,
        process_start=lambda owner_pid: common.process_start_identity(owner_pid))
    if guard_owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        return False
    if guard_owner is ownerfile.ProcessOwner.EXACT_LIVE:
        if not common.terminate_exact_process_tree(
                record.guard, record.guard_start,
                wait_s=max(0.0, deadline - time.monotonic()),
                require_bound_tree=True):
            return False
    try:
        record = _read_indexd_child(owner_snapshot)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not record.valid:
        return False
    target_owner = ownerfile.classify_process(
        record.target, record.target_start, pid_alive=common.pid_alive,
        process_start=lambda owner_pid: common.process_start_identity(owner_pid))
    if target_owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        return False
    if target_owner is ownerfile.ProcessOwner.EXACT_LIVE:
        if not common.terminate_exact_process_tree(
                record.target, record.target_start,
                wait_s=max(0.0, deadline - time.monotonic()),
                require_bound_tree=True):
            return False
    elif target_owner in (
            ownerfile.ProcessOwner.DEAD,
            ownerfile.ProcessOwner.REUSED):
        if common._owner_group_active(
                record.group, record.target,
                target_owner is ownerfile.ProcessOwner.REUSED) is not False:
            return False
    return _remove_indexd_child(record)


def _inspect_indexd_owner(
        *, current_writer_id: str | None = None, settle_child: bool = True,
) -> _IndexdOwnerInspection:
    """Classify one bounded, no-follow snapshot of the lifetime owner."""
    try:
        observed = ownerfile.snapshot(
            INDEXD_LOCK_PATH, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    except FileNotFoundError:
        return _IndexdOwnerInspection(
            _IndexdOwnerState.ABSENT, None, None, None)
    except OSError:
        return _IndexdOwnerInspection(
            _IndexdOwnerState.HOSTILE, None, None, None)
    body = observed.raw.decode("utf-8", errors="replace")
    pid_text = common._owner_field(body, "pid")
    process_start = common._owner_field(body, "start")
    protocol = common._owner_field(body, "protocol")
    package = common._owner_field(body, "package")
    build = common._owner_field(body, "build")
    writer = common._owner_field(body, "writer")
    token = common._owner_field(body, "token")
    group_text = common._owner_field(body, "group")
    tree_text = common._owner_field(body, "tree")
    try:
        pid = int(pid_text) if pid_text is not None else None
    except ValueError:
        pid = None
    valid_identity = (
        pid is not None and 0 < pid <= common._MAX_PROCESS_ID
        and process_start not in (None, "", "None", "unknown")
    )
    complete = (
        observed.raw.endswith(b"\n")
        and valid_identity
        and protocol is not None and package is not None and build is not None
        and token is not None
        and re.fullmatch(r"[0-9a-f]{32}", token) is not None
    )
    if not complete:
        age = time.time() - observed.mtime
        if 0.0 <= age < _INDEXD_PUBLICATION_GRACE_S:
            state = _IndexdOwnerState.MALFORMED_FRESH
        elif valid_identity:
            process_owner = ownerfile.classify_process(
                int(pid), str(process_start), pid_alive=common.pid_alive,
                process_start=lambda owner_pid:
                common.process_start_identity(owner_pid))
            if process_owner is ownerfile.ProcessOwner.EXACT_LIVE:
                state = _IndexdOwnerState.HOSTILE
            elif process_owner is ownerfile.ProcessOwner.UNVERIFIABLE:
                state = _IndexdOwnerState.UNVERIFIABLE
            else:
                state = _IndexdOwnerState.MALFORMED_STALE
        else:
            state = _IndexdOwnerState.MALFORMED_STALE
        return _IndexdOwnerInspection(state, observed, pid, process_start)
    process_owner = ownerfile.classify_process(
        pid, process_start, pid_alive=common.pid_alive,
        process_start=lambda owner_pid: common.process_start_identity(owner_pid))
    if process_owner is ownerfile.ProcessOwner.DEAD:
        child_active = _indexd_child_active(observed, settle=settle_child)
        if child_active is True:
            return _IndexdOwnerInspection(
                _IndexdOwnerState.ORPHANED_GROUP,
                observed, pid, process_start)
        if child_active is None:
            return _IndexdOwnerInspection(
                _IndexdOwnerState.UNVERIFIABLE,
                observed, pid, process_start)
        if common.WIN:
            state = _IndexdOwnerState.DEAD
        else:
            try:
                recorded_group = int(group_text) if group_text is not None else pid
            except ValueError:
                recorded_group = 0
            if recorded_group != pid:
                state = _IndexdOwnerState.HOSTILE
            else:
                present = common._process_group_active(recorded_group)
                if present is True:
                    state = _IndexdOwnerState.ORPHANED_GROUP
                elif present is False:
                    state = _IndexdOwnerState.DEAD
                else:
                    state = _IndexdOwnerState.UNVERIFIABLE
    elif process_owner is ownerfile.ProcessOwner.REUSED:
        child_active = _indexd_child_active(observed, settle=settle_child)
        if child_active is True:
            state = _IndexdOwnerState.ORPHANED_GROUP
        elif child_active is None:
            state = _IndexdOwnerState.UNVERIFIABLE
        elif not common.WIN:
            try:
                recorded_group = int(
                    group_text) if group_text is not None else pid
            except ValueError:
                recorded_group = 0
            if recorded_group != pid:
                state = _IndexdOwnerState.HOSTILE
            else:
                present = common._owner_group_active(
                    recorded_group, pid, True)
                if present is False:
                    state = _IndexdOwnerState.REUSED
                else:
                    state = _IndexdOwnerState.UNVERIFIABLE
        else:
            state = _IndexdOwnerState.REUSED
    elif process_owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        state = _IndexdOwnerState.UNVERIFIABLE
    else:
        if current_writer_id is None:
            try:
                current_writer_id = derived_writer_build_id(
                    common._resolved_ingest_bin(), require_binary=True)
            except OSError:
                current_writer_id = None
        wire_identity = (
            protocol == str(INDEXD_PROTOCOL)
            and package == common.package_version()
            and build == INDEXD_BUILD_ID
            and writer == current_writer_id)
        wire_compatible = (
            wire_identity
            and (not common.WIN
                 or tree_text == common.WINDOWS_DESCENDANT_TREE))
        if common.WIN:
            topology = (
                group_text == "job"
                and (not wire_identity
                     or tree_text == common.WINDOWS_DESCENDANT_TREE))
        else:
            try:
                actual_group = os.getpgid(pid)
                actual_session = os.getsid(pid)
            except OSError:
                topology = None
            else:
                try:
                    recorded_group = (
                        int(group_text) if group_text is not None else None)
                except ValueError:
                    topology = False
                else:
                    record_matches = (
                        recorded_group == pid
                        if recorded_group is not None else not wire_compatible)
                    topology = (
                        record_matches
                        and actual_group == actual_session == pid)
        if topology is None:
            state = _IndexdOwnerState.UNVERIFIABLE
        elif not topology:
            state = _IndexdOwnerState.HOSTILE
        else:
            state = (_IndexdOwnerState.COMPATIBLE
                     if wire_compatible else _IndexdOwnerState.INCOMPATIBLE)
    return _IndexdOwnerInspection(state, observed, pid, process_start)


def _indexd_ready_path(owner_snapshot: ownerfile.Snapshot) -> Path:
    token = indexd_generation_token(owner_snapshot)
    return INDEXD_READY_PATH.with_name(f"{INDEXD_READY_PATH.name}.{token}")


def indexd_live_path(owner_snapshot: ownerfile.Snapshot) -> Path:
    """Private live-state publication for one exact daemon generation."""
    token = indexd_generation_token(owner_snapshot)
    return INDEXD_LIVE_PATH.with_name(f"{INDEXD_LIVE_PATH.name}.{token}")


def _same_indexd_generation(
        left: ownerfile.Snapshot, right: ownerfile.Snapshot) -> bool:
    # Heartbeats legitimately move mtime. The retained owner entry, bytes, and
    # secret token do not change for the lifetime of a generation.
    return left.identity[:3] == right.identity[:3] and left.raw == right.raw


def _indexd_live_bytes(
        owner_snapshot: ownerfile.Snapshot,
        live_snapshot: object,
) -> bytes | None:
    """Encode every session row, trimming only old feed events if necessary."""
    if not isinstance(live_snapshot, dict):
        return None
    sessions = live_snapshot.get("sessions")
    if live_snapshot.get("booting") is not False or not isinstance(sessions, list):
        return None
    rows: list[dict] = []
    recents: list[list] = []
    for session in sessions:
        if not isinstance(session, dict):
            return None
        recent = session.get("recent")
        if (not isinstance(recent, list)
                or not all(isinstance(event, dict) for event in recent)):
            return None
        row = dict(session)
        row["recent"] = []
        rows.append(row)
        recents.append(recent)
    payload = dict(live_snapshot)
    payload["sessions"] = []
    token = indexd_generation_token(owner_snapshot)
    total_events = sum(len(recent) for recent in recents)
    metadata = {
        "version": _INDEXD_LIVE_SCHEMA,
        "protocol": INDEXD_PROTOCOL,
        "build": INDEXD_BUILD_ID,
        "generation": token,
        "owner_sha256": hashlib.sha256(owner_snapshot.raw).hexdigest(),
        "published_at_ms": int(time.time() * 1000),
        "recent_trimmed": total_events > 0,
        "recent_events_omitted": total_events,
    }
    payload["_agrep_live_ipc"] = metadata

    def encoded(value: object) -> bytes | None:
        try:
            return (
                json.dumps(
                    value, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ) + "\n"
            ).encode("ascii")
        except (RecursionError, TypeError, ValueError):
            return None

    base = encoded(payload)
    if base is None:
        return None
    floor_size = len(base)
    for index, row in enumerate(rows):
        row_raw = encoded(row)
        if row_raw is None:
            return None
        floor_size += len(row_raw) - 1 + (1 if index else 0)
        if floor_size > _INDEXD_LIVE_MAX_BYTES:
            return None
    payload["sessions"] = rows
    floor = encoded(payload)
    if floor is None or len(floor) > _INDEXD_LIVE_MAX_BYTES:
        return None
    if not total_events:
        return floor
    # A suffix per session preserves the newest feed evidence without a giant trial encode.
    remaining = _INDEXD_LIVE_MAX_BYTES - len(floor) - (1 if total_events else 0)
    retained = [0] * len(rows)
    candidates = [index for index, recent in enumerate(recents) if recent]
    while candidates and remaining > 0:
        progressed = False
        next_candidates = []
        for index in candidates:
            recent = recents[index]
            count = retained[index]
            if count >= len(recent):
                continue
            event_raw = encoded(recent[-count - 1])
            if event_raw is None:
                return None
            cost = len(event_raw) - 1 + (1 if count else 0)
            if cost > remaining:
                continue
            retained[index] += 1
            remaining -= cost
            progressed = True
            if retained[index] == len(recent):
                continue
            next_candidates.append(index)
        candidates = next_candidates
        if not progressed:
            break
    kept = sum(retained)
    for index, count in enumerate(retained):
        if count:
            rows[index]["recent"] = recents[index][-count:]
    metadata["recent_trimmed"] = kept < total_events
    metadata["recent_events_omitted"] = total_events - kept
    raw = encoded(payload)
    if raw is None or len(raw) > _INDEXD_LIVE_MAX_BYTES:
        return None
    return raw


def publish_indexd_live_snapshot(
        owner: ownerfile.Handle,
        live_snapshot: object,
) -> ownerfile.Snapshot | None:
    """Atomically publish one bounded, generation-bound resident snapshot."""
    if (_data_dir_readonly()
            or not isinstance(live_snapshot, dict)
            or live_snapshot.get("booting") is not False
            or not isinstance(live_snapshot.get("sessions"), list)):
        return None
    replaced = False
    published_raw: bytes | None = None
    path: Path | None = None
    temporary: Path | None = None
    try:
        owner_snapshot = owner.verify()
        published_raw = _indexd_live_bytes(owner_snapshot, live_snapshot)
        if published_raw is None:
            return None
        path = indexd_live_path(owner_snapshot)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        ownerfile.create_exclusive(
            temporary, published_raw, mode=0o600, fsync=True,
            exact_mode=True)
        owner.verify()
        common.replace_with_retry(
            temporary, path, before_attempt=owner.verify)
        replaced = True
        owner.verify()
        observed = ownerfile.snapshot(
            path, max_bytes=_INDEXD_LIVE_MAX_BYTES)
        if observed.raw != published_raw:
            raise OSError(
                "resident live snapshot changed during publication")
        return observed
    except (OSError, RecursionError, TypeError, ValueError) as exc:
        if replaced and path is not None and published_raw is not None:
            try:
                observed = ownerfile.snapshot(
                    path, max_bytes=_INDEXD_LIVE_MAX_BYTES)
                if observed.raw == published_raw:
                    ownerfile.remove_exact(
                        path, observed, tombstone=True,
                        require_stable_mtime=True)
            except OSError:
                pass
        if isinstance(exc, ownerfile.OwnershipLost):
            raise
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _private_live_file(path: Path) -> ownerfile.Snapshot | None:
    try:
        info = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISREG(info.st_mode)
                or bool(getattr(info, "st_file_attributes", 0) & reparse)
                or (not common.WIN and info.st_mode & 0o077)):
            return None
        observed = ownerfile.snapshot(
            path, max_bytes=_INDEXD_LIVE_MAX_BYTES)
        after = path.lstat()
    except OSError:
        return None
    if (not observed.raw.endswith(b"\n")
            or observed.identity[:3]
            != (int(info.st_dev), int(info.st_ino), int(info.st_size))
            or observed.identity[:3]
            != (int(after.st_dev), int(after.st_ino), int(after.st_size))
            or not stat.S_ISREG(after.st_mode)
            or (not common.WIN and after.st_mode & 0o077)):
        return None
    return observed


_LIVE_TOP_LEVEL_FIELDS = frozenset({
    "now", "window_s", "sessions", "booting", "n_subs", "n_emitted",
    "n_tracked", "last_err", "degraded_sources", "tick_ms", "n_loops",
    "watch_mode", "poll_s", "work_ms", "loop_ms", "work_total_ms",
})
_LIVE_SESSION_FIELDS = frozenset({
    "agent", "session", "project", "title", "model", "last_ts", "state",
    "working", "parent", "state_ts", "queued", "queued_text", "sub",
    "active", "recent",
})


def _valid_indexd_live_payload(
        payload: object,
        owner_snapshot: ownerfile.Snapshot,
        *,
        now_ms: int,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if not _LIVE_TOP_LEVEL_FIELDS.issubset(payload):
        return False
    metadata = payload.get("_agrep_live_ipc")
    sessions = payload.get("sessions")
    if (not isinstance(metadata, dict)
            or metadata.get("version") != _INDEXD_LIVE_SCHEMA
            or metadata.get("protocol") != INDEXD_PROTOCOL
            or metadata.get("build") != INDEXD_BUILD_ID
            or metadata.get("generation")
            != indexd_generation_token(owner_snapshot)
            or metadata.get("owner_sha256")
            != hashlib.sha256(owner_snapshot.raw).hexdigest()
            or type(metadata.get("published_at_ms")) is not int
            or type(metadata.get("recent_trimmed")) is not bool
            or type(metadata.get("recent_events_omitted")) is not int
            or metadata["recent_events_omitted"] < 0
            or (metadata["recent_trimmed"]
                != (metadata["recent_events_omitted"] > 0))
            or payload.get("booting") is not False
            or payload.get("watch_mode") != "indexd"
            or not isinstance(sessions, list)
            or not isinstance(payload.get("last_err"), str)
            or not isinstance(payload.get("degraded_sources"), list)):
        return False
    integer_fields = (
        "now", "n_subs", "n_emitted", "n_tracked", "n_loops", "work_ms",
        "loop_ms", "work_total_ms",
    )
    if (any(type(payload.get(name)) is not int or payload[name] < 0
            for name in integer_fields)
            or not isinstance(payload.get("window_s"), (int, float))
            or isinstance(payload.get("window_s"), bool)
            or not math.isfinite(float(payload["window_s"]))
            or payload["window_s"] < 0
            or not isinstance(payload.get("poll_s"), (int, float))
            or isinstance(payload.get("poll_s"), bool)
            or not math.isfinite(float(payload["poll_s"]))
            or payload["poll_s"] < 0):
        return False
    published_at = metadata["published_at_ms"]
    if (published_at > now_ms + int(_INDEXD_LIVE_FUTURE_S * 1000)
            or now_ms - published_at
            > int(_INDEXD_LIVE_MAX_AGE_S * 1000)):
        return False
    for row in sessions:
        if not isinstance(row, dict) or not _LIVE_SESSION_FIELDS.issubset(row):
            return False
        if (not all(isinstance(row.get(name), str) for name in (
                "agent", "session", "project", "title", "model", "state",
                "queued_text"))
                or (row.get("last_text") is not None
                    and not isinstance(row.get("last_text"), str))
                or type(row.get("working")) is not bool
                or type(row.get("sub")) is not bool
                or type(row.get("active")) is not bool
                or any(type(row.get(name)) is not int or row[name] < 0
                       for name in ("last_ts", "state_ts", "queued"))
                or row.get("parent") is not None
                and not isinstance(row.get("parent"), str)
                or not isinstance(row.get("recent"), list)
                or not all(isinstance(event, dict)
                           for event in row["recent"])):
            return False
    return True


def resident_indexd_live_snapshot() -> dict | None:
    """Read a ready daemon's live snapshot without mutating daemon state."""
    before = _inspect_indexd_owner(settle_child=False)
    if (before.state is not _IndexdOwnerState.COMPATIBLE
            or before.snapshot is None
            or not _indexd_ready(before)):
        return None
    try:
        path = indexd_live_path(before.snapshot)
    except ownerfile.OwnershipLost:
        return None
    observed = _private_live_file(path)
    if observed is None:
        return None
    try:
        payload = json.loads(
            observed.raw.decode("ascii"),
            object_pairs_hook=_unique_json_object)
    except (RecursionError, UnicodeError, ValueError):
        return None
    after = _inspect_indexd_owner(settle_child=False)
    if (after.state is not _IndexdOwnerState.COMPATIBLE
            or after.snapshot is None
            or not _same_indexd_generation(before.snapshot, after.snapshot)
            or not _indexd_ready(after)):
        return None
    try:
        valid = _valid_indexd_live_payload(
            payload, after.snapshot, now_ms=int(time.time() * 1000))
    except (RecursionError, TypeError, ValueError):
        return None
    return payload if valid else None


def remove_indexd_live_snapshot(
        owner_snapshot: ownerfile.Snapshot,
        published_snapshot: ownerfile.Snapshot,
) -> bool:
    """Remove only the exact live file this daemon generation published."""
    try:
        path = indexd_live_path(owner_snapshot)
        observed = ownerfile.snapshot(
            path, max_bytes=_INDEXD_LIVE_MAX_BYTES)
        payload = json.loads(
            observed.raw.decode("ascii"),
            object_pairs_hook=_unique_json_object)
        metadata = payload.get("_agrep_live_ipc")
    except (OSError, RecursionError, UnicodeError, ValueError, AttributeError):
        return False
    if (observed != published_snapshot
            or not isinstance(metadata, dict)
            or metadata.get("generation")
            != indexd_generation_token(owner_snapshot)
            or metadata.get("owner_sha256")
            != hashlib.sha256(owner_snapshot.raw).hexdigest()):
        return False
    return ownerfile.remove_exact(
        path, observed, tombstone=True, require_stable_mtime=True)


def _remove_indexd_ready_for(owner_snapshot: ownerfile.Snapshot) -> bool:
    try:
        path = _indexd_ready_path(owner_snapshot)
    except ownerfile.OwnershipLost:
        return False
    try:
        ready = ownerfile.snapshot(
            path, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if ready.raw != owner_snapshot.raw:
        return False
    return ownerfile.remove_exact(
        path, ready, tombstone=True,
        require_stable_mtime=True)


def _settle_indexd_owner(
        *, allow_retire: bool = False,
        retire_budget_s: float = 5.0) -> _IndexdOwnerInspection:
    """Retire only owners whose exact snapshot and process identity permit it."""
    retire_deadline = time.monotonic() + max(0.0, retire_budget_s)
    for _ in range(3):
        inspected = _inspect_indexd_owner()
        if inspected.state is _IndexdOwnerState.INCOMPATIBLE:
            if inspected.pid is None or inspected.process_start is None:
                return inspected
            if (common.WIN and inspected.snapshot is not None
                    and common._owner_field(
                        inspected.snapshot.raw.decode(
                            "utf-8", errors="replace"),
                        "group") != "job"):
                return inspected
            if not allow_retire:
                return inspected
            remaining = max(0.0, retire_deadline - time.monotonic())
            if remaining < _INDEXD_AUTORETIRE_MIN_S:
                return inspected
            named_job = (
                inspected.snapshot is not None
                and _indexd_has_exact_tree(inspected.snapshot))
            if common.WIN and not named_job:
                return inspected
            if not common.terminate_exact_process_tree(
                    inspected.pid, inspected.process_start,
                    wait_s=remaining, require_bound_tree=True):
                return inspected
            child_active = _indexd_child_active(inspected.snapshot)
            if child_active is not False:
                return _inspect_indexd_owner()
            if (not common.WIN
                    and common._process_group_active(inspected.pid) is not False):
                return _inspect_indexd_owner()
            reclaim = True
        else:
            reclaim = inspected.state in _INDEXD_RECLAIMABLE_STATES
        if not reclaim or inspected.snapshot is None:
            return inspected
        removed = ownerfile.remove_exact(
            INDEXD_LOCK_PATH, inspected.snapshot, tombstone=True,
            require_stable_mtime=True)
        if removed:
            _remove_indexd_ready_for(inspected.snapshot)
    return _inspect_indexd_owner()


class _SpawnGuardInspection(NamedTuple):
    snapshot: ownerfile.Snapshot
    age: float
    holder_pid: int
    holder_start: str | None
    token: str | None
    complete: bool
    owner: ownerfile.ProcessOwner | None


def _inspect_spawn_guard() -> _SpawnGuardInspection:
    observed = ownerfile.snapshot(_SPAWN_GUARD_PATH, max_bytes=256)
    record = observed.raw.decode("utf-8", errors="replace")
    state = common._owner_field(record, "state")
    holder = common._owner_field(record, "pid")
    holder_start = common._owner_field(record, "start")
    token = common._owner_field(record, "token")
    try:
        holder_pid = int(holder or "")
    except ValueError:
        holder_pid = 0
    complete = (
        observed.raw.endswith(b"\n")
        and state == "launching"
        and 0 < holder_pid <= common._MAX_PROCESS_ID
        and holder_start not in (None, "", "None", "unknown")
        and token is not None
        and re.fullmatch(r"[0-9a-f]{32}", token) is not None
    )
    process_owner = None
    if complete:
        process_owner = ownerfile.classify_process(
            holder_pid, holder_start, pid_alive=common.pid_alive,
            process_start=common.process_start_identity)
    return _SpawnGuardInspection(
        observed, time.time() - observed.mtime, holder_pid,
        holder_start, token, complete, process_owner)


class _SpawnChildInspection(NamedTuple):
    path: Path
    snapshot: ownerfile.Snapshot
    age: float
    child_pid: int
    child_start: str | None
    complete: bool
    owner: ownerfile.ProcessOwner | None


class _SpawnChildState(Enum):
    ABSENT = "absent"
    ACTIVE = "active"
    BLOCKED = "blocked"
    RECLAIMED = "reclaimed"
    # provably dead, left in place because this pass may not write
    STALE = "stale"


def _spawn_child_path(token: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ownerfile.OwnershipLost("spawn guard has no valid generation token")
    return _SPAWN_GUARD_PATH.with_name(
        f"{_SPAWN_GUARD_PATH.name}.{token}.child")


def _inspect_spawn_child(token: str) -> _SpawnChildInspection:
    path = _spawn_child_path(token)
    observed = ownerfile.snapshot(path, max_bytes=256)
    record = observed.raw.decode("utf-8", errors="replace")
    holder = common._owner_field(record, "pid")
    child_start = common._owner_field(record, "start")
    try:
        child_pid = int(holder or "")
    except ValueError:
        child_pid = 0
    complete = (
        observed.raw.endswith(b"\n")
        and common._owner_field(record, "state") == "spawned"
        and common._owner_field(record, "owner") == token
        and 0 < child_pid <= common._MAX_PROCESS_ID
        and child_start is not None
    )
    process_owner = None
    if complete:
        process_owner = ownerfile.classify_process(
            child_pid, child_start, pid_alive=common.pid_alive,
            process_start=common.process_start_identity)
    return _SpawnChildInspection(
        path, observed, time.time() - observed.mtime, child_pid,
        child_start, complete, process_owner)


def _settle_spawn_child(
        guard: _SpawnGuardInspection, *, settle: bool = True,
) -> _SpawnChildState:
    if guard.token is None:
        return _SpawnChildState.ABSENT
    try:
        child = _inspect_spawn_child(guard.token)
    except FileNotFoundError:
        return _SpawnChildState.ABSENT
    except (OSError, ownerfile.OwnershipLost):
        return _SpawnChildState.BLOCKED
    if not child.complete:
        if 0.0 <= child.age < _INDEXD_PUBLICATION_GRACE_S:
            return _SpawnChildState.ACTIVE
    elif child.owner is ownerfile.ProcessOwner.EXACT_LIVE:
        return _SpawnChildState.ACTIVE
    elif child.owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        return _SpawnChildState.BLOCKED
    if not settle:
        return _SpawnChildState.STALE
    try:
        removed = ownerfile.remove_exact(
            child.path, child.snapshot, tombstone=True,
            require_stable_mtime=True)
    except OSError:
        removed = False
    return (_SpawnChildState.RECLAIMED if removed
            else _SpawnChildState.BLOCKED)


def _publish_spawn_child(
        guard: ownerfile.Handle,
        process: subprocess.Popen,
) -> ownerfile.ProcessOwner:
    guard_snapshot = guard.verify(require_stable_mtime=True)
    token = common._owner_field(
        guard_snapshot.raw.decode("utf-8", errors="replace"), "token")
    if token is None:
        raise ownerfile.OwnershipLost("spawn guard has no generation token")
    child_pid = int(process.pid)
    if not 0 < child_pid <= common._MAX_PROCESS_ID:
        raise ownerfile.OwnershipLost("spawn child has an invalid process id")
    if process.poll() is not None:
        return ownerfile.ProcessOwner.DEAD
    child_start = None
    deadline = time.monotonic() + _INDEXD_FOREGROUND_RETIRE_S
    while process.poll() is None:
        child_start = common.process_start_identity(child_pid)
        if child_start is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    recorded_start = child_start or "unknown"
    raw = (
        f"state=spawned owner={token} pid={child_pid} "
        f"start={recorded_start}\n"
    ).encode("ascii")
    child = ownerfile.create_exclusive(
        _spawn_child_path(token), raw, mode=0o600, fsync=True)
    try:
        guard.verify(require_stable_mtime=True)
    except BaseException:
        child.release(tombstone=True, require_stable_mtime=True)
        raise
    return ownerfile.classify_process(
        child_pid, child_start, pid_alive=common.pid_alive,
        process_start=common.process_start_identity)


def _reap_dead_spawn_guard(inspected: _SpawnGuardInspection) -> None:
    """Remove exactly the launch claim whose launcher is provably gone."""
    if _data_dir_readonly() or not derived_writes_permitted():
        return
    try:
        ownerfile.remove_exact(
            _SPAWN_GUARD_PATH, inspected.snapshot, tombstone=True,
            require_stable_mtime=True)
    except OSError:
        pass


def _spawn_guard_resource_status(
        *, settle_child: bool = True,
) -> dict[str, object] | None:
    try:
        inspected = _inspect_spawn_guard()
    except FileNotFoundError:
        return None
    except OSError:
        return {"running": False, "blocked": True, "state": "spawn-guard"}
    age = inspected.age
    if not inspected.complete:
        if 0.0 <= age < _INDEXD_PUBLICATION_GRACE_S:
            return {"running": False, "starting": True,
                    "age_s": round(age, 1)}
        return None
    if inspected.owner is ownerfile.ProcessOwner.EXACT_LIVE:
        return {"running": False, "starting": True,
                "age_s": round(max(0.0, age), 1)}
    if inspected.owner is ownerfile.ProcessOwner.UNVERIFIABLE:
        if _settle_spawn_child(
                inspected, settle=settle_child) is _SpawnChildState.ACTIVE:
            return {"running": False, "starting": True,
                    "age_s": round(max(0.0, age), 1)}
        return {"running": False, "blocked": True, "state": "spawn-guard"}
    child_state = _settle_spawn_child(inspected, settle=settle_child)
    if child_state is _SpawnChildState.ACTIVE:
        return {"running": False, "starting": True,
                "age_s": round(max(0.0, age), 1)}
    if child_state is _SpawnChildState.BLOCKED:
        return {"running": False, "blocked": True, "state": "spawn-guard"}
    # A dead launcher with no live child arbitrates nothing. Whether this
    # process may reap the claim is a separate fact from whether it fences:
    # a diagnostic pass declines the write, not the verdict.
    if settle_child:
        _reap_dead_spawn_guard(inspected)
    return None


_GENERATION_RECORD_RE = re.compile(
    rf"^\.indexd\.v{INDEXD_PROTOCOL}\.(?:"
    r"(?:ready|child|live)\.[0-9a-f]{32}"
    r"|spawn\.[0-9a-f]{32}\.child)$")
_GENERATION_RECORD_PID_RE = re.compile(r"(?:^|\s)(?:pid|guard|target)=(\d+)")


def reclaim_dead_generation_records() -> int:
    """Remove the readiness and handoff records of daemon generations that ended.

    Every one of these is addressed through the live owner's token, so a record
    written by a generation whose processes are all gone is unreachable rather
    than authoritative - it fences nothing and no reader can reach it. One file
    per daemon launch accumulates forever otherwise."""
    if _data_dir_readonly() or not derived_writes_permitted():
        return 0
    live: set[str] = set()
    inspected = _inspect_indexd_owner(settle_child=False)
    if inspected.snapshot is not None:
        try:
            live.add(indexd_generation_token(inspected.snapshot))
        except ownerfile.OwnershipLost:
            return 0        # the live generation cannot be named: touch nothing
    try:
        live.add(_inspect_spawn_guard().token or "")
    except FileNotFoundError:
        pass
    except OSError:
        return 0
    try:
        entries = list(os.scandir(common.DATA_DIR))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if (_GENERATION_RECORD_RE.fullmatch(entry.name) is None
                or any(token and token in entry.name for token in live)):
            continue
        try:
            observed = ownerfile.snapshot(
                Path(entry.path), max_bytes=_INDEXD_LIVE_MAX_BYTES)
        except (FileNotFoundError, OSError, ownerfile.OwnershipLost):
            continue
        body = observed.raw.decode("utf-8", errors="replace")
        pids = [int(value) for value
                in _GENERATION_RECORD_PID_RE.findall(body)[:8]]
        if pids:
            if any(common.pid_alive(pid) for pid in pids):
                continue    # a live process still answers for this record
        elif time.time() - observed.mtime <= _INDEXD_LIVE_MAX_AGE_S:
            continue        # a pidless snapshot is only stale once unreadable
        try:
            if ownerfile.remove_exact(
                    Path(entry.path), observed, tombstone=True,
                    require_stable_mtime=True):
                removed += 1
        except OSError:
            continue
    return removed


def _clear_indexd_response_probe() -> None:
    if (_data_dir_readonly()
            or not derived_writes_permitted()):
        return
    try:
        _INDEXD_RESPONSE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _write_indexd_response_probe(record: dict) -> bool:
    if (_data_dir_readonly()
            or not derived_writes_permitted()):
        return False
    temporary = _INDEXD_RESPONSE_PATH.with_name(
        f".{_INDEXD_RESPONSE_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        common.replace_with_retry(temporary, _INDEXD_RESPONSE_PATH)
        return True
    except OSError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _indexd_responsiveness(
        inspected: _IndexdOwnerInspection, *, probe: bool = True,
) -> tuple[str, float]:
    if inspected.snapshot is None or inspected.pid is None:
        return "unavailable", 0.0
    age = time.time() - inspected.snapshot.mtime
    import proc
    if proc.process_execution_state(inspected.pid) == "stopped":
        return "unresponsive", max(0.0, age)
    if 0.0 <= age <= _INDEXD_HEARTBEAT_STALE_S:
        if probe:
            _clear_indexd_response_probe()
        return "responsive", age
    if not probe:
        return "unprobed", max(0.0, age)
    try:
        token = indexd_generation_token(inspected.snapshot)
        heartbeat = int(inspected.snapshot.identity[3])
        observed = ownerfile.snapshot(
            _INDEXD_RESPONSE_PATH, max_bytes=_INDEXD_RESPONSE_MAX_BYTES)
        record = json.loads(observed.raw.decode("utf-8"))
    except (FileNotFoundError, OSError, RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError, ownerfile.OwnershipLost):
        record = None
    now = time.time()
    if (isinstance(record, dict)
            and record.get("token") == token
            and record.get("heartbeat") == heartbeat):
        try:
            observed_at = float(record["observed_at"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            elapsed = now - observed_at
            if 0.0 <= elapsed:
                if elapsed >= _INDEXD_RESPONSE_GRACE_S:
                    return "unresponsive", max(0.0, age)
                return "checking", max(0.0, age)
    if not _write_indexd_response_probe({
            "token": token, "heartbeat": heartbeat, "observed_at": now}):
        return "unverifiable", max(0.0, age)
    return "checking", max(0.0, age)


def indexd_resource_status(
        *, observe_only: bool = False, include_rss: bool = True,
        current_writer_id: str | None = None,
) -> dict[str, object]:
    """Ownership and RSS of the current compatible freshness daemon.

    ``observe_only`` is the diagnostic contract: it never settles or retires
    ownership records and never publishes or clears a heartbeat challenge.
    """
    def rss_fields(pid: int | None) -> dict[str, object]:
        if not include_rss:
            return {"rss_state": "not-inspected"}
        rss = common.process_rss_bytes(pid or 0)
        return {"rss_bytes": rss}

    protected = _data_dir_readonly()
    if protected or observe_only:
        # Status is diagnostic. Under the exact data-dir guard it must not
        # settle stale owners, remove ready/child records, retire a legacy
        # owner, or publish a heartbeat challenge.
        inspect_args = {"settle_child": False}
        if current_writer_id is not None:
            inspect_args["current_writer_id"] = current_writer_id
        inspected = _inspect_indexd_owner(**inspect_args)
        if inspected.state is _IndexdOwnerState.ABSENT:
            try:
                ownerfile.snapshot(
                    LEGACY_INDEXD_LOCK_PATH,
                    max_bytes=_INDEXD_OWNER_MAX_BYTES)
            except FileNotFoundError:
                return (
                    _spawn_guard_resource_status(settle_child=False)
                    or {"running": False}
                )
            except OSError:
                pass
            return {"running": False, "blocked": True,
                    "state": "legacy-owner"}
        if inspected.state is not _IndexdOwnerState.COMPATIBLE:
            return {"running": False, "blocked": True,
                    "state": inspected.state.value}
        if not _indexd_ready(inspected):
            age = (max(0.0, time.time() - inspected.snapshot.mtime)
                   if inspected.snapshot is not None else 0.0)
            return {"running": False, "starting": True,
                    "age_s": round(age, 1)}
        pid = inspected.pid
        responsiveness, heartbeat_age = _indexd_responsiveness(
            inspected, probe=False)
        if responsiveness in ("unresponsive", "unverifiable"):
            return {
                "running": False, "blocked": True,
                "state": f"daemon-{responsiveness}", "pid": pid,
                "heartbeat_age_s": round(heartbeat_age, 1),
                "protocol": INDEXD_PROTOCOL, "build": INDEXD_BUILD_ID,
            }
        return {
            "running": True, "pid": pid,
            "protocol": INDEXD_PROTOCOL, "build": INDEXD_BUILD_ID,
            "responsive": responsiveness == "responsive",
            "heartbeat_age_s": round(heartbeat_age, 1),
            **rss_fields(pid),
            **({"state": "heartbeat-unprobed"}
               if responsiveness == "unprobed" else {}),
        }
    ownership = derived_writer_mutation_info()
    if not ownership.writable:
        return {
            "running": False,
            "blocked": True,
            "state": DERIVED_STORE_OWNER_CODE,
            "reason": ownership.reason,
        }
    inspected = _settle_indexd_owner(
        retire_budget_s=_INDEXD_FOREGROUND_RETIRE_S)
    if inspected.state in _INDEXD_INLINE_BLOCKING_STATES:
        return {"running": False, "blocked": True,
                "state": inspected.state.value}
    if inspected.state is not _IndexdOwnerState.COMPATIBLE:
        if not _retire_legacy_indexd():
            return {"running": False, "blocked": True,
                    "state": "legacy-owner"}
        return _spawn_guard_resource_status() or {"running": False}
    if not _indexd_ready(inspected):
        age = (max(0.0, time.time() - inspected.snapshot.mtime)
               if inspected.snapshot is not None else 0.0)
        return {"running": False, "starting": True,
                "age_s": round(age, 1)}
    pid = inspected.pid
    responsiveness, heartbeat_age = _indexd_responsiveness(inspected)
    if responsiveness in ("unresponsive", "unverifiable"):
        return {
            "running": False, "blocked": True,
            "state": f"daemon-{responsiveness}", "pid": pid,
            "heartbeat_age_s": round(heartbeat_age, 1),
            "protocol": INDEXD_PROTOCOL, "build": INDEXD_BUILD_ID,
        }
    return {
        "running": True, "pid": pid,
        "protocol": INDEXD_PROTOCOL, "build": INDEXD_BUILD_ID,
        "responsive": responsiveness == "responsive",
        "heartbeat_age_s": round(heartbeat_age, 1),
        **rss_fields(pid),
        **({"state": "checking-heartbeat"}
           if responsiveness == "checking" else {}),
    }


def indexd_owner_body() -> str:
    """Exact ownership descriptor written by the current freshness daemon."""
    lifetime = common.descendant_lifetime_contract()
    if lifetime is None:
        raise ownerfile.OwnershipLost(
            "indexd has no verified descendant lifetime boundary")
    process_start = common.process_start_identity(os.getpid())
    if process_start is None:
        raise ownerfile.OwnershipLost(
            "indexd cannot establish its kernel process identity")
    try:
        writer = derived_writer_build_id(
            common._resolved_ingest_bin(), require_binary=True)
    except OSError as exc:
        raise ownerfile.OwnershipLost(
            f"indexd cannot establish its derived-writer identity: {exc}") from exc
    tree = f" tree={lifetime.tree}" if lifetime.tree else ""
    return (
        f"pid={os.getpid()} start={process_start} "
        f"protocol={INDEXD_PROTOCOL} package={common.package_version()} "
        f"build={INDEXD_BUILD_ID} writer={writer} "
        f"group={lifetime.group}{tree} token={secrets.token_hex(16)} "
        f"time={time.time():.3f}\n"
    )


def _create_indexd_owner(raw: bytes) -> ownerfile.Handle | None:
    if _data_dir_readonly():
        return None
    if not derived_writes_permitted():
        return None
    if removal_fence.background_removal_active():
        return None
    owner = ownerfile.create_exclusive(
        INDEXD_LOCK_PATH, raw, mode=0o600, fsync=True, retain_fd=True)
    if not removal_fence.background_removal_active():
        return owner
    try:
        owner.release(tombstone=True, require_stable_mtime=True)
    except OSError:
        owner.close()
    return None


def acquire_indexd_owner() -> ownerfile.Handle | None:
    """Acquire the daemon lifetime owner or yield to a protected contender."""
    if _data_dir_readonly():
        return None
    if not derived_writes_permitted():
        return None
    deadline = time.monotonic() + _INDEXD_ACQUIRE_WAIT_S
    attempts = 0
    while True:
        attempts += 1
        if removal_fence.background_removal_active():
            return None
        try:
            raw = indexd_owner_body().encode("utf-8")
        except ownerfile.OwnershipLost:
            return None
        try:
            return _create_indexd_owner(raw)
        except FileExistsError:
            remaining = max(0.0, deadline - time.monotonic())
            inspected = _settle_indexd_owner(
                allow_retire=True, retire_budget_s=remaining)
            if inspected.state is _IndexdOwnerState.ABSENT:
                try:
                    return _create_indexd_owner(raw)
                except FileExistsError:
                    if attempts >= 256 or time.monotonic() >= deadline:
                        return None
                    continue
                except OSError:
                    return None
            if inspected.state is _IndexdOwnerState.MALFORMED_FRESH:
                remaining = deadline - time.monotonic()
                if remaining > 0 and attempts < 256:
                    time.sleep(min(0.02, remaining))
                    continue
                if (inspected.snapshot is not None
                        and ownerfile.remove_exact(
                            INDEXD_LOCK_PATH, inspected.snapshot, tombstone=True,
                            require_stable_mtime=True)):
                    try:
                        return _create_indexd_owner(raw)
                    except FileExistsError:
                        return None
                return None
            return None


def heartbeat_indexd_owner(owner: ownerfile.Handle) -> None:
    """Refresh the diagnostic heartbeat only while exact ownership survives."""
    if _data_dir_readonly():
        return
    if not derived_writes_permitted():
        raise ownerfile.OwnershipLost(
            derived_writer_mutation_info().reason)
    owner.touch()


def publish_indexd_ready(owner: ownerfile.Handle) -> ownerfile.Handle:
    """Publish operational readiness bound to this exact owner generation."""
    if _data_dir_readonly():
        raise PermissionError(
            "AGREP_DATA_READONLY protects this data directory")
    if not derived_writes_permitted():
        raise ownerfile.OwnershipLost(
            derived_writer_mutation_info().reason)
    owner_snapshot = owner.verify()
    path = _indexd_ready_path(owner_snapshot)
    try:
        ready = ownerfile.create_exclusive(
            path, owner_snapshot.raw, mode=0o600, fsync=True)
    except FileExistsError as exc:
        raise ownerfile.OwnershipLost(
            f"indexd readiness already exists: {path}") from exc
    try:
        owner.verify()
    except OSError:
        ready.release(tombstone=True, require_stable_mtime=True)
        raise
    # This generation is the successor, so every other one's records are
    # superseded the moment this readiness lands.
    reclaim_dead_generation_records()
    return ready


def _indexd_ready(inspected: _IndexdOwnerInspection) -> bool:
    if (inspected.state is not _IndexdOwnerState.COMPATIBLE
            or inspected.snapshot is None):
        return False
    try:
        path = _indexd_ready_path(inspected.snapshot)
        ready = ownerfile.snapshot(
            path, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    except OSError:
        return False
    return ready.raw == inspected.snapshot.raw


def _await_indexd_child_clear(
        owner_snapshot: ownerfile.Snapshot, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if _indexd_child_active(owner_snapshot) is False:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def stop_indexd_owner(*, wait_s: float = 5.0) -> bool:
    """Stop the exact freshness owner and preserve any replacement descriptor."""
    inspected = _inspect_indexd_owner()
    if (inspected.state not in _INDEXD_EXACT_STATES or inspected.pid is None
            or inspected.process_start is None
            or inspected.snapshot is None):
        return False
    deadline = time.monotonic() + max(0.0, wait_s)
    named_job = _indexd_has_exact_tree(inspected.snapshot)
    if common.WIN and not named_job:
        return False
    stopped = common.terminate_exact_process_tree(
        inspected.pid, inspected.process_start, wait_s=wait_s,
        require_bound_tree=True,
        # Leave enough TERM grace to drain a detached post-index process group
        # before exact-tree escalation kills the daemon's own group.
        term_grace_s=max(0.0, wait_s - min(1.0, wait_s / 2.0)))
    if not stopped:
        return False
    if not _await_indexd_child_clear(
            inspected.snapshot,
            timeout_s=max(0.0, deadline - time.monotonic())):
        return False
    if (not common.WIN
            and common._process_group_active(inspected.pid) is not False):
        return False
    settled = _settle_indexd_owner(
        retire_budget_s=max(0.0, deadline - time.monotonic()))
    _remove_indexd_ready_for(inspected.snapshot)
    return settled.state is _IndexdOwnerState.ABSENT


def stop_indexers_for_removal(*, wait_s: float = 5.0) -> dict:
    """Stop current and legacy freshness owners, then report settled state."""
    stopped: list[str] = []
    deadline = time.monotonic() + max(0.0, wait_s)
    try:
        stopped_current = stop_indexd_owner(wait_s=wait_s)
        inspected = _settle_indexd_owner(
            retire_budget_s=max(0.0, deadline - time.monotonic()))
        current_ok = inspected.state is _IndexdOwnerState.ABSENT
        if stopped_current and current_ok:
            stopped.append("indexd")
        current_state = inspected.state.value
    except OSError:
        current_ok = False
        current_state = "inspection-failed"

    try:
        try:
            LEGACY_INDEXD_LOCK_PATH.lstat()
            legacy_present = True
        except FileNotFoundError:
            legacy_present = False
        legacy_live = _legacy_indexd_live() if legacy_present else False
        legacy_ok = (
            not legacy_present
            or _retire_legacy_indexd(
                allow_retire=not common.WIN, retire_budget_s=wait_s))
        if legacy_ok and legacy_live:
            stopped.append("legacy indexd")
        legacy_state = "absent" if legacy_ok else "protected"
    except OSError:
        legacy_ok = False
        legacy_state = "inspection-failed"

    if not current_ok:
        owner_state = current_state
    elif not legacy_ok:
        owner_state = f"legacy-{legacy_state}"
    else:
        owner_state = "absent"
    return {
        "ok": current_ok and legacy_ok,
        "stopped": tuple(stopped),
        "owner_state": owner_state,
        "current_state": current_state,
        "legacy_state": legacy_state,
    }


def _retire_legacy_indexd(
        *, allow_retire: bool = False, retire_budget_s: float = 5.0) -> bool:
    """Retire only a provable legacy owner; protect ambiguous live records."""
    path = LEGACY_INDEXD_LOCK_PATH
    try:
        observed = ownerfile.snapshot(path, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    body = observed.raw.decode("utf-8", errors="replace")
    pid_text = common._owner_field(body, "pid")
    try:
        pid = int(pid_text) if pid_text is not None else None
    except ValueError:
        pid = None
    if pid is None or pid <= 0 or pid > common._MAX_PROCESS_ID:
        age = time.time() - observed.mtime
        if 0.0 <= age < _INDEXD_PUBLICATION_GRACE_S:
            return False
        return ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    if not common.pid_alive(pid):
        if not common.WIN:
            group_present = common._process_group_active(pid)
            if group_present is not False:
                return False
        else:
            age = time.time() - observed.mtime
            if 0.0 <= age < _LEGACY_ORPHAN_GRACE_S:
                return False
        return ownerfile.remove_exact(
            path, observed, tombstone=True, require_stable_mtime=True)
    expected_start = common._owner_field(body, "start")
    if expected_start not in (None, "unknown"):
        if not allow_retire or retire_budget_s < _INDEXD_AUTORETIRE_MIN_S:
            return False
        if common.WIN:
            return False
        if common.terminate_exact_process_tree(
                pid, expected_start, wait_s=retire_budget_s):
            if common._process_group_active(pid) is not False:
                return False
            return ownerfile.remove_exact(
                path, observed, tombstone=True, require_stable_mtime=True)
        return False
    return False


def _legacy_indexd_live() -> bool:
    """True only for the exact live process named by the legacy descriptor."""
    try:
        observed = ownerfile.snapshot(
            LEGACY_INDEXD_LOCK_PATH, max_bytes=_INDEXD_OWNER_MAX_BYTES)
    except OSError:
        return False
    body = observed.raw.decode("utf-8", errors="replace")
    try:
        pid = int(common._owner_field(body, "pid") or "")
    except ValueError:
        return False
    expected_start = common._owner_field(body, "start")
    if (not 0 < pid <= common._MAX_PROCESS_ID
            or expected_start in (None, "", "None", "unknown")
            or not common.pid_alive(pid)):
        return False
    return common.process_start_identity(pid) == expected_start


def _await_indexd_ready(
        process: subprocess.Popen | None, *,
        timeout_s: float = _INDEXD_READY_WAIT_S) -> bool:
    """Wait for the child to publish compatible ownership or fail."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        inspected = _inspect_indexd_owner()
        if _indexd_ready(inspected):
            return True
        if process is not None and process.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def freshener_alive() -> bool:
    """True when the headless daemon is already keeping the index hot."""
    if not derived_writes_permitted():
        return False
    inspected = _settle_indexd_owner(
        retire_budget_s=_INDEXD_FOREGROUND_RETIRE_S)
    ready = _indexd_ready(inspected)
    responsiveness = (
        _indexd_responsiveness(inspected)[0] if ready else "unavailable")
    common.dbg(
        f"freshener check: indexd_ready={ready} responsive={responsiveness}")
    return ready and responsiveness in ("responsive", "checking")

AUTO_INDEX_HEALTH = "auto-index-health.json"
_AUTO_INDEX_HEALTH_MAX_BYTES = 64 * 1024
_AUTO_INDEX_ERROR_MAX_CHARS = 4096
_FRESHNESS_RENDER_MAX_CHARS = 512
_FRESHEN_FAILURE = threading.local()
_FOREGROUND_SNAPSHOT = threading.local()
READONLY_REFRESH_FENCE_REASON = (
    "AGREP_DATA_READONLY is set, so agrep will not update the index; searches "
    "use the index as it stands"
)
NO_AUTO_REFRESH_REASON = (
    "automatic freshness checks are disabled by --no-auto"
)


def _set_freshen_failure(code: str, reason: str) -> None:
    _FRESHEN_FAILURE.value = surface.FreshnessFailure(code, reason)


# The ownership-refusal code every emitter and checker shares: corpusdb
# discloses it, _daemon_will_converge refuses to promise convergence on it.
DERIVED_STORE_OWNER_CODE = surface.DERIVED_STORE_OWNER_CODE


def defer_foreground_refresh(reason: str) -> None:
    """Remember why this search cannot wait for the derived FTS refresh.

    The actual stale/direct snapshot choice belongs to corpusdb. Keeping only
    the reason here lets a later ownership guard replace the policy without
    teaching the daemon layer how a particular derived artifact is encoded.
    """
    _FOREGROUND_SNAPSHOT.context = common.terminal_safe(reason)


def foreground_refresh_deferred() -> bool:
    """Whether this search delegated freshness beyond the foreground reader."""
    return bool(str(
        getattr(_FOREGROUND_SNAPSHOT, "context", "") or "").strip())


def disclose_foreground_snapshot(
        *, direct_scan: bool,
        code: str = "search-index-stale", reason: str | None = None) -> None:
    """Record an interactive stale-snapshot selection for every surface.

    Rendering belongs to freshness_story() alone: exactly one
    freshness line per search, so the selection never prints its own hedge.
    Machine JSON reads the structured record through machine_freshness().
    `code` and `reason` are seams for stronger policies such as build
    ownership.
    """
    # The recorded reason is the cause alone (law 5): the served lane is the
    # engine fact in the footer and --json, and gluing mechanism onto every
    # deferral built the exact wall the render budget outlaws.
    context = str(
        reason or getattr(_FOREGROUND_SNAPSHOT, "context", "") or "").strip()
    detail = context or (
        "searching the published transcript snapshot directly"
        if direct_scan else "serving the published search snapshot")
    if getattr(_FOREGROUND_SNAPSHOT, "failure", None) is None:
        # A stronger policy (for example derived-store ownership) may have
        # recorded the boundary before corpusdb selected its read lane.
        _FOREGROUND_SNAPSHOT.failure = surface.FreshnessFailure(code, detail)


def _clear_freshen_failure() -> None:
    _FRESHEN_FAILURE.value = None
    _FOREGROUND_SNAPSHOT.context = ""
    _FOREGROUND_SNAPSHOT.failure = None
    _DRIFT_CACHE.value = None


def record_auto_index_health(
        streak: int, last_err: str, escalated: bool = False) -> None:
    """The indexer writes its own failure streak after every run - counting
    "auto-index failed" lines in the shared log undercounted whenever a
    multi-line error or any interleaved log line landed between failures.
    `escalated` marks a streak that already spent its one automatic --full."""
    if _data_dir_readonly():
        common.dbg(
            "auto-index health skipped: AGREP_DATA_READONLY protects this data dir")
        return
    if not derived_writes_permitted():
        return
    reason = str(last_err)
    if len(reason) > _AUTO_INDEX_ERROR_MAX_CHARS:
        half = (_AUTO_INDEX_ERROR_MAX_CHARS - 3) // 2
        reason = f"{reason[:half]}...{reason[-half:]}"
    previous = _auto_index_health()
    _write_auto_index_health({
        "streak": int(streak), "last_err": reason, "ts": time.time(),
        "escalated": bool(escalated),
        "repair": previous.repair, "repair_streak": previous.repair_streak,
    })


def record_derived_repair(code: str, streak: int) -> None:
    """Persist the queued rebuild and how many rebuilds it has outlived.

    Nothing renders from this while a repair is working (law 3); it exists so
    the surfaces can tell a first attempt from one law 6 lets them escalate."""
    if _data_dir_readonly() or not derived_writes_permitted():
        return
    previous = _auto_index_health()
    if (previous.state == "available" and previous.repair == str(code)[:64]
            and previous.repair_streak == int(streak)):
        return  # an unchanged verdict is not a write
    _write_auto_index_health({
        "streak": previous.streak if previous.state == "available" else 0,
        "last_err": previous.reason if previous.state == "available" else "",
        "ts": time.time(),
        "escalated": bool(previous.escalated
                          and previous.state == "available"),
        "repair": str(code)[:64], "repair_streak": max(0, int(streak)),
    })


def _write_auto_index_health(payload: dict) -> None:
    path = common.DATA_DIR / AUTO_INDEX_HEALTH
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        common.replace_with_retry(tmp, path)
    except OSError as exc:
        _set_freshen_failure(
            "freshness-ledger-unavailable",
            f"the freshness health record cannot be written ({exc})")
        stale = time.time() - FRESHNESS_WRITE_RATE_S - 1.0
        try:
            os.utime(common.INGEST_SIG_PATH, (stale, stale))
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        current = getattr(_FRESHEN_FAILURE, "value", None)
        if (isinstance(current, surface.FreshnessFailure)
                and current.code == "freshness-ledger-unavailable"):
            _clear_freshen_failure()


def _unique_json_object(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate health field: {key}")
        record[key] = value
    return record


class AutoIndexHealth(NamedTuple):
    """The one ledger the repair loop writes and every surface reads."""
    state: str          # available | absent | unavailable
    streak: int = 0
    reason: str = ""
    ts: float = 0.0
    escalated: bool = False
    repair: str = ""    # the derived state a queued rebuild is closing
    repair_streak: int = 0  # rebuilds that ran and left the damage standing


def _auto_index_health() -> AutoIndexHealth:
    path = common.DATA_DIR / AUTO_INDEX_HEALTH
    try:
        snapshot = ownerfile.snapshot(
            path, max_bytes=_AUTO_INDEX_HEALTH_MAX_BYTES)
        rec = json.loads(
            snapshot.raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object)
    except FileNotFoundError:
        return AutoIndexHealth("absent")
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return AutoIndexHealth(
            "unavailable", reason=f"{path} cannot be read ({exc})")
    if not isinstance(rec, dict):
        return AutoIndexHealth(
            "unavailable", reason=f"{path} is not a health record")
    streak, reason, timestamp = (
        rec.get("streak"), rec.get("last_err"), rec.get("ts"))
    escalated = rec.get("escalated", False)
    repair, repair_streak = rec.get("repair", ""), rec.get("repair_streak", 0)
    try:
        numeric_timestamp = float(timestamp)
    except (OverflowError, TypeError, ValueError):
        numeric_timestamp = math.nan
    if (type(streak) is not int or not 0 <= streak <= 2_147_483_647
            or not isinstance(reason, str)
            or len(reason) > _AUTO_INDEX_ERROR_MAX_CHARS
            or bool(streak) != bool(reason)
            or not isinstance(escalated, bool)
            or (escalated and not streak)
            or not isinstance(repair, str) or len(repair) > 64
            or type(repair_streak) is not int
            or not 0 <= repair_streak <= 2_147_483_647
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(numeric_timestamp) or numeric_timestamp < 0):
        return AutoIndexHealth(
            "unavailable", reason=f"{path} has invalid health fields")
    return AutoIndexHealth("available", streak, reason, numeric_timestamp,
                           escalated, repair, repair_streak)


# What a rebuild restages beside the live set before renaming over it.
_DERIVED_SET_NAMES = (
    "messages.jsonl", "sessions.jsonl", "corpus.db",
    "session_family.meta.json", ".ingest_cache.bin")


def rebuild_space_shortfall() -> int:
    """Bytes the data volume is short of what a rebuild needs, or 0.

    One statvfs and five lstats - cheaper than any check that reports on it,
    which is why the condition that caused the owner's screen was the one
    thing never observed."""
    try:
        free = shutil.disk_usage(common.DATA_DIR).free
    except OSError:
        return 0
    published = 0
    for name in _DERIVED_SET_NAMES:
        try:
            published += (common.DATA_DIR / name).lstat().st_size
        except OSError:
            pass
    return surface.rebuild_shortfall_bytes(free, published)


def derived_writes_permitted() -> bool:
    """The write fence with its liveness half restored.

    `derived_writer_mutation_info` compares build identities, which cannot
    distinguish an old build still writing from one that was killed months
    ago. A foreign anchor with no live indexer claim is a dead owner's; the
    Rust ingest's successor takeover exists exactly to reap it, and holding
    the fence closed left boxes that only ever search foreign forever.

    Launchability is `derived_writer_launchable`'s answer, never a second
    private copy of it: a state the Rust writer may act on and no live claim
    over it is the escape, so a cold rollback journal self-heals like a dead
    foreign owner does. Only the liveness half may keep an owner fenced."""
    info = derived_writer_mutation_info()
    if info.writable:
        return True
    return derived_writer_launchable(info) and not live_indexer_claim()


def live_indexer_claim() -> bool:
    """True only while a LIVE process verifiably holds the indexer here.

    Ownership and liveness are different facts: a build-id anchor left behind
    by a killed daemon is a dead owner's claim, and rendering it as "another
    agrep holds the indexer" tells the reader to fight a process that no
    longer exists. Unverifiable and hostile states stay True - a claim we
    cannot prove dead is not ours to reap. This build's own compatible
    daemon is no one's blocker - it IS the writer the takeover installs."""
    inspected = _inspect_indexd_owner(settle_child=False)
    return not (inspected.state is _IndexdOwnerState.ABSENT
                or inspected.state is _IndexdOwnerState.COMPATIBLE
                or inspected.state in _INDEXD_RECLAIMABLE_STATES)


class RepairKick(NamedTuple):
    """One verdict every surface shares: is repair in flight, and if not, the
    single cause. Emitter and checker, one artifact - a lane that renders a
    decline and the freshness story that hedges on it can never disagree."""
    in_flight: bool
    cause: str


def _automatic_refresh_disabled() -> bool:
    return str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "") \
        == NO_AUTO_REFRESH_REASON


def kick_background_repair() -> RepairKick:
    """Fire and forget: make sure something is running that will repair damage
    agrep owns. Callers reach here only for states `corpusdb.repairs_itself`
    accepts, so a healthy box pays nothing for this.

    Diagnostics used to start no work at all, and that contract was written
    against a world where starting work meant a foreground rebuild - slow, and
    mutating the very state the reader was trying to inspect. A background kick
    is neither. This process stays fast and read-only and reports what it saw
    at the moment it saw it; the daemon rebuilds afterwards. The alternative is
    worse than the rule it protected: a reader who runs `agrep status` to check
    her index, and only ever that, would see repairable damage reported forever
    and never once have it repaired.

    Silent by construction (law 3) and guarded exactly as the search path is: a
    read-only data dir, a disabled daemon, a missing binary, a freshener already
    hot, or derived stores this build does not own all decline.

    `in_flight` is True only when repair is verifiably running - a freshener
    already hot, or a spawn this call made. Law 4 hangs on this: a surface may
    go quiet about a fault only while something real is erasing it. A decline
    names its one cause so every surface renders the same fact; user-chosen
    states (readonly, no-daemon) and states the freshness footer already owns
    (no-binary, spawn failures) are causes, not extra lines."""
    if _automatic_refresh_disabled():
        return RepairKick(False, "no-auto")
    try:
        if _data_dir_readonly():
            return RepairKick(False, "readonly")
        if os.environ.get("AGREP_NO_DAEMON"):
            return RepairKick(False, "no-daemon")
        if not common.ingest_bin().exists():
            return RepairKick(False, "no-binary")
        if freshener_alive():
            return RepairKick(True, "")  # already hot; it will reach the damage
        if not derived_writes_permitted():
            # An exact incompatible daemon is an upgrade predecessor, not an
            # indefinite fence. _spawn_indexd performs the process-identity
            # checked handoff; every hostile/unverifiable claim stays closed.
            info = derived_writer_mutation_info()
            inspected = _inspect_indexd_owner(settle_child=False)
            if (not derived_writer_launchable(info)
                    or inspected.state is not _IndexdOwnerState.INCOMPATIBLE):
                return RepairKick(
                    False,
                    "held-foreign-owner" if derived_writer_launchable(info)
                    else "owner-unverifiable")
    except Exception:  # noqa: BLE001 - a diagnostic kick cannot fail its surface
        return RepairKick(False, "probe-failed")
    try:
        result = _spawn_indexd()
    except Exception:                # noqa: BLE001 - a kick never fails a surface
        return RepairKick(False, "spawn-failed")
    if result in (_IndexdSpawnResult.READY, _IndexdSpawnResult.IN_FLIGHT):
        return RepairKick(True, "")
    if result is _IndexdSpawnResult.BLOCKED:
        # Preserve the ownership cause when exact retirement lost a race or
        # the predecessor would not drain. A generic spawn result would make
        # the degraded scan silent even though the owner is still present.
        try:
            info = derived_writer_mutation_info()
            inspected = _inspect_indexd_owner(settle_child=False)
        except Exception:  # noqa: BLE001 - disclosure probing stays bounded
            return RepairKick(False, "owner-unverifiable")
        if (derived_writer_launchable(info)
                and inspected.state is _IndexdOwnerState.INCOMPATIBLE):
            return RepairKick(False, "held-foreign-owner")
        if inspected.state in _INDEXD_UNRETIRABLE_STATES:
            return RepairKick(False, "owner-unverifiable")
    return RepairKick(False, f"spawn-{result.value}")


def _repair_event_store() -> bool:
    kick = kick_background_repair()
    if not kick.in_flight:
        return False
    SEARCH_BEAT_PATH.touch()
    return True


events.set_event_repair_callback(_repair_event_store)


def host_block_escalation() -> str:
    """Law 2's line, or "" - which on a working box is always.

    Two ways to earn it, and neither is a guess: the OS named the condition in
    the writer's own error text, or a rebuild agrep queued has now failed the
    number of times law 6 allows it to speak and the volume explains why."""
    health = _auto_index_health()
    if health.state != "available":
        return ""
    block = surface.host_block(health.reason)
    shortfall = rebuild_space_shortfall()
    if block is None:
        if (not health.repair
                or health.repair_streak < surface.REPAIR_ESCALATE_AFTER
                or shortfall <= 0):
            return ""
        block = surface.HOST_BLOCKS["out-of-space"]
    return surface.host_block_line(block, shortfall)


def auto_index_escalated() -> bool:
    """True while the persisted failure streak has already spent its one
    automatic --full escalation; a successful run clears it with the streak."""
    health = _auto_index_health()
    return health.state == "available" and health.escalated


def indexd_failure_state() -> tuple[int, str, float]:
    """Persisted failure streak, reason, and last-attempt wall time."""
    health = _auto_index_health()
    if health.state != "available":
        return 0, "", 0.0
    return health.streak, health.reason, health.ts


def indexd_failing() -> tuple[int, str]:
    """(consecutive auto-index failures, the latest failure's text).
    A successful pass explicitly clears the streak, so age must not hide a
    daemon that failed and then stopped running."""
    streak, reason, _ = indexd_failure_state()
    return streak, reason


# Freshness is source drift against the published generation, not age or daemon
# liveness; the Rust census and verified-current record provide the evidence.

VERIFIED_CURRENT_FILE = "verified-current.json"
_VERIFIED_CURRENT_MAX_BYTES = 64 * 1024
# The interactive charter: a sub-500ms observation or an immediate honest
# unknown verdict - the census may never stall the search path. Stampers and
# doctor pass their own slower budgets explicitly.
_DRIFT_PROBE_TIMEOUT_S = 0.45
_DRIFT_REAP_WAIT_S = 0.02
_DRIFT_CACHE_TTL_S = 5.0
_DRIFT_PROBE = threading.local()
_DRIFT_CACHE = threading.local()
# The daemon's designed catch-up horizon (QUIET_S settle + the MAX_STALE_S
# marathon backstop + ingest headroom): younger drift is the debounce working
# as designed and renders green; only drift older than this is "behind".
DRIFT_GRACE_S = 90.0
# The record-less fallback reads sessions.jsonl once for the published store
# set; corpora past this size skip the set compare rather than pay for it.
_PUBLISHED_AGENTS_MAX_BYTES = 64 * 1024 * 1024
# Change-sensitive identity: a per-store digest over member (path, size,
# mtime) rows catches the restored-mtime rewrite; larger stores keep the
# (files, newest_mtime_ms) observation rather than pay a stat pass.
_STORE_DIGEST_MAX_FILES = 1024
_PATHS_PROBE_MAX_BYTES = 8 * 1024 * 1024
_INGEST_SIG_MAX_BYTES = 4096
class DriftReport(NamedTuple):
    state: str  # current | drifted | unknown
    changed_stores: int = 0
    behind_s: float | None = None
    code: str = ""
    detail: str = ""
    # Drift younger than the daemon's debounce horizon, reported only when no
    # background owner exists to absorb it (the horizon defends nothing then).
    young: bool = False
    # current only: stores changed inside the horizon while a background owner
    # may absorb them. Human output stays quiet, but machines and empty-result
    # exits keep the observation rather than claiming proven currency.
    absorbed: int = 0


_UNOBSERVED_STORES = object()
_UNOBSERVED_DRIFT = object()


def _census_popen_kw() -> dict:
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
          "text": True, "encoding": "utf-8", "errors": "replace"}
    if common.WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


# Every census child ever launched and not yet waited on, across threads: a
# command that errors out between arming the probe and rendering its verdict
# must not strand a zombie child, so process exit reaps whatever is left.
_DRIFT_PROBE_LOCK = threading.Lock()
_DRIFT_PROBE_LIVE: set[subprocess.Popen] = set()


def _track_drift_probe(proc: subprocess.Popen) -> None:
    with _DRIFT_PROBE_LOCK:
        _DRIFT_PROBE_LIVE.add(proc)


def _untrack_drift_probe(proc: subprocess.Popen) -> None:
    with _DRIFT_PROBE_LOCK:
        _DRIFT_PROBE_LIVE.discard(proc)


def _reap_killed_drift_probe(proc: subprocess.Popen) -> None:
    """Finish waiting outside the caller's latency budget."""
    try:
        proc.communicate()
    except (subprocess.SubprocessError, OSError, TypeError, ValueError):
        pass
    finally:
        _untrack_drift_probe(proc)


def _new_drift_reaper(proc: subprocess.Popen) -> threading.Thread:
    return threading.Thread(
        target=_reap_killed_drift_probe,
        args=(proc,),
        daemon=True,
        name="agrep-drift-reaper",
    )


def _kill_drift_probe(proc: subprocess.Popen) -> None:
    """Kill promptly; hand a pathological wait to a tracked daemon reaper."""
    killed = True
    try:
        proc.kill()
    except (subprocess.SubprocessError, OSError, TypeError, ValueError):
        # The child may already have exited; the bounded wait below still
        # gives Popen a chance to collect it.
        killed = False
    try:
        proc.communicate(timeout=_DRIFT_REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        if not killed:
            # Never give an unbounded daemon waiter a child we could not stop.
            # Retain it for the process-exit fence instead.
            return
        try:
            reaper = _new_drift_reaper(proc)
            reaper.start()
        except (RuntimeError, TypeError, ValueError):
            # Keep it in _DRIFT_PROBE_LIVE so the atexit fence can retry.
            pass
        return
    except (subprocess.SubprocessError, OSError, TypeError, ValueError):
        # Likewise retain uncertain children for the atexit fence.
        return
    _untrack_drift_probe(proc)


def _discard_drift_probe(proc: subprocess.Popen) -> None:
    """Kill and wait one census child so no exit path leaves a zombie."""
    _untrack_drift_probe(proc)
    try:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        pass


def _reap_drift_probes() -> None:
    """Early-exit safety net (also registered atexit): reap every armed
    census probe that no verdict ever consumed."""
    with _DRIFT_PROBE_LOCK:
        procs = tuple(_DRIFT_PROBE_LIVE)
    for proc in procs:
        _discard_drift_probe(proc)


atexit.register(_reap_drift_probes)


def _drift_probe_before_fork() -> None:
    _DRIFT_PROBE_LOCK.acquire()


def _drift_probe_after_fork_parent() -> None:
    _DRIFT_PROBE_LOCK.release()


def _drift_probe_after_fork_child() -> None:
    """A fork inherits every armed census pipe but owns none of them.

    Reading one steals the parent's verdict - the parent's own
    communicate() then finds the pipe already at EOF and reports the census
    unavailable - and reaping one kills a child the parent still waits on.
    A forked child therefore starts with no probe and no cached drift
    verdict; if it needs a census it spawns its own.
    """
    try:
        for attr in ("proc", "paths_proc"):
            proc = getattr(_DRIFT_PROBE, attr, None)
            setattr(_DRIFT_PROBE, attr, None)
            if proc is None:
                continue
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            # Not this process's child: never wait on it, never warn on it.
            proc.returncode = 0
        _DRIFT_PROBE_LIVE.clear()
        _DRIFT_CACHE.value = None
    finally:
        _DRIFT_PROBE_LOCK.release()


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(
        before=_drift_probe_before_fork,
        after_in_parent=_drift_probe_after_fork_parent,
        after_in_child=_drift_probe_after_fork_child)


def _arm_drift_probe() -> None:
    """Start the census concurrently with the query it will be judged against."""
    if getattr(_DRIFT_PROBE, "proc", None) is not None:
        return
    ingest = common.ingest_bin()
    if not ingest.exists():
        return
    try:
        proc = subprocess.Popen(
            [str(ingest), "stores"], **_census_popen_kw())
    except OSError:
        _DRIFT_PROBE.proc = None
        return
    _track_drift_probe(proc)
    _DRIFT_PROBE.proc = proc
    _DRIFT_PROBE.armed_at = time.monotonic()
    record = _read_verified_record()
    if record is not None and record.digests:
        # The record carries change-sensitive store identities; arm the
        # registry-owned member listing so the verdict can re-derive them
        # without paying the child's wall time on the render path.
        _discard_paths_probe()
        try:
            paths_proc = subprocess.Popen(
                [str(ingest), "stores", "--paths"], **_census_popen_kw())
        except OSError:
            return
        _track_drift_probe(paths_proc)
        _DRIFT_PROBE.paths_proc = paths_proc


def arm_store_census() -> None:
    """Diagnostic arming hook (doctor): start the census child early so its
    wall time overlaps the caller's other probes instead of following them -
    the same overlap the search path buys inside ensure_index."""
    _arm_drift_probe()


def _discard_paths_probe() -> None:
    """Kill a leftover member-listing child a verdict never consumed."""
    leftover = getattr(_DRIFT_PROBE, "paths_proc", None)
    if leftover is not None:
        _DRIFT_PROBE.paths_proc = None
        _kill_drift_probe(leftover)


def _consume_paths_probe(
        timeout_s: float = _DRIFT_PROBE_TIMEOUT_S,
) -> dict[str, list[str]] | None:
    """Member paths per store from an armed `stores --paths` child; never
    spawns one - an unarmed caller honestly gets no member identity."""
    proc = getattr(_DRIFT_PROBE, "paths_proc", None)
    _DRIFT_PROBE.paths_proc = None
    if proc is None:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError, TypeError, ValueError):
        _kill_drift_probe(proc)
        return None
    _untrack_drift_probe(proc)
    if proc.returncode != 0 or len(out) > _PATHS_PROBE_MAX_BYTES:
        return None
    try:
        rows = json.loads(out)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    grouped: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        name, path = row.get("name"), row.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            return None
        grouped.setdefault(name, []).append(path)
    return grouped


def _store_paths_census(timeout_s: float) -> dict[str, list[str]] | None:
    """One synchronous member listing for the stamp side of the record."""
    ingest = common.ingest_bin()
    if not ingest.exists():
        return None
    _discard_paths_probe()
    try:
        proc = subprocess.Popen(
            [str(ingest), "stores", "--paths"], **_census_popen_kw())
    except OSError:
        return None
    _track_drift_probe(proc)
    _DRIFT_PROBE.paths_proc = proc
    return _consume_paths_probe(timeout_s=timeout_s)


def _store_change_digest(paths: list[str]) -> str | None:
    """sha256 over sorted member (path, size, mtime) rows - the identity a
    restored newest-mtime rewrite cannot keep. None claims nothing."""
    if len(paths) > _STORE_DIGEST_MAX_FILES:
        return None
    digest = hashlib.sha256()
    for path in sorted(paths):
        try:
            observed = os.lstat(path)
        except OSError:
            return None
        digest.update(
            f"{path}\x00{observed.st_size}"
            f"\x00{observed.st_mtime_ns // 1_000_000}\n"
            .encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _store_digests(
        census: dict[str, tuple[int, int]],
        paths_by_store: dict[str, list[str]] | None) -> dict[str, str]:
    if not paths_by_store:
        return {}
    out: dict[str, str] = {}
    for name, (files, _newest) in census.items():
        if files > _STORE_DIGEST_MAX_FILES or name not in paths_by_store:
            continue
        digest = _store_change_digest(paths_by_store[name])
        if digest is not None:
            out[name] = digest
    return out


def _store_census(timeout_s: float = _DRIFT_PROBE_TIMEOUT_S) -> list | None:
    """Rows from `agrep-rs stores`, or None when the census cannot be trusted."""
    proc = getattr(_DRIFT_PROBE, "proc", None)
    _DRIFT_PROBE.proc = None
    if proc is not None and (
            time.monotonic() - getattr(_DRIFT_PROBE, "armed_at", 0.0) > 30.0):
        # A probe left over from an earlier search in a long-lived process
        # would answer for a stale moment; a fresh census replaces it.
        _kill_drift_probe(proc)
        proc = None
    if proc is None:
        ingest = common.ingest_bin()
        if not ingest.exists():
            return None
        try:
            proc = subprocess.Popen([str(ingest), "stores"], **_census_popen_kw())
        except OSError:
            return None
        _track_drift_probe(proc)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError, TypeError, ValueError):
        _kill_drift_probe(proc)
        return None
    _untrack_drift_probe(proc)
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(out)
    except (RecursionError, TypeError, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def _census_map(
        rows: list | None,
) -> tuple[dict[str, tuple[int, int]] | None, tuple[str, ...]]:
    """({store: (files, newest_mtime_ms)}, unreadable store names).

    A None map means the census as a whole cannot be trusted (foreign shape,
    missing binary). One unreadable store never invalidates the rest: it is
    returned by name so the verdict can degrade for that store alone."""
    if rows is None:
        return None, ()
    out: dict[str, tuple[int, int]] = {}
    unreadable: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            return None, ()
        name = row.get("name")
        if not isinstance(name, str) or not name:
            return None, ()
        if row.get("state") != "available":
            unreadable.append(name)
            continue
        files, newest = row.get("files"), row.get("newest_mtime_ms")
        if (type(files) is not int or files < 0
                or type(newest) is not int or newest < 0):
            return None, ()
        if files:
            out[name] = (files, newest)
    return out, tuple(unreadable)


def _verified_current_path() -> Path:
    return common.DATA_DIR / VERIFIED_CURRENT_FILE


class _VerifiedRecord(NamedTuple):
    ts: float
    census: dict[str, tuple[int, int]]
    digests: dict[str, str]
    signature: str | None


_DIGEST_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


def _read_verified_record() -> _VerifiedRecord | None:
    try:
        snapshot = ownerfile.snapshot(
            _verified_current_path(), max_bytes=_VERIFIED_CURRENT_MAX_BYTES)
        rec = json.loads(
            snapshot.raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, RecursionError, UnicodeError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("version") != 1:
        return None
    ts, census = rec.get("ts"), rec.get("census")
    if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
            or not math.isfinite(float(ts)) or float(ts) < 0.0
            or not isinstance(census, dict)):
        return None
    out: dict[str, tuple[int, int]] = {}
    for name, entry in census.items():
        if (not isinstance(name, str) or not name
                or not isinstance(entry, list) or len(entry) != 2
                or any(type(value) is not int or value < 0 for value in entry)):
            return None
        out[name] = (entry[0], entry[1])
    digests = rec.get("digests", {})
    if (not isinstance(digests, dict)
            or any(not isinstance(name, str) or name not in out
                   or not isinstance(value, str)
                   or not _DIGEST_HEX_RE.fullmatch(value)
                   for name, value in digests.items())):
        return None
    signature = rec.get("signature")
    if signature is not None and (
            not isinstance(signature, str) or not signature
            or len(signature) > _INGEST_SIG_MAX_BYTES):
        return None
    return _VerifiedRecord(float(ts), out, dict(digests), signature)


def read_verified_current() -> tuple[float, dict[str, tuple[int, int]]] | None:
    record = _read_verified_record()
    if record is None:
        return None
    return record.ts, record.census


def _ingest_signature_text() -> str | None:
    try:
        raw = (common.DATA_DIR / ".ingest.sig").read_bytes()
    except OSError:
        return None
    if len(raw) > _INGEST_SIG_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8").strip() or None
    except UnicodeError:
        return None


def record_verified_current(
        census: dict[str, tuple[int, int]], *, wall: float | None = None,
        digests: dict[str, str] | None = None) -> bool:
    """Stamp proof that the published generation covered this store census."""
    if _data_dir_readonly() or not derived_writer_mutation_info().writable:
        return False
    path = _verified_current_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = {
        "version": 1,
        "ts": time.time() if wall is None else wall,
        "census": {name: [files, newest]
                   for name, (files, newest) in sorted(census.items())},
    }
    if digests:
        payload["digests"] = {
            name: digests[name] for name in sorted(digests) if name in census}
    signature = _ingest_signature_text()
    if signature is not None:
        payload["signature"] = signature
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        common.replace_with_retry(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _signal_identity() -> tuple[int, int] | None:
    try:
        observed = (common.DATA_DIR / ".ingest.sig").stat()
    except OSError:
        return None
    return observed.st_mtime_ns, observed.st_size


def _optional_drift_file_identity(
        path: Path) -> fileops.FileIdentity | None:
    try:
        return fileops.file_identity(path)
    except OSError:
        return None


def _drift_cache_key() -> tuple[
        str, fileops.FileIdentity | None,
        fileops.FileIdentity | None, fileops.FileIdentity | None,
]:
    root = common.DATA_DIR
    return (
        os.fspath(root),
        _optional_drift_file_identity(root / "messages.jsonl"),
        _optional_drift_file_identity(root / ".ingest.sig"),
        _optional_drift_file_identity(root / VERIFIED_CURRENT_FILE),
    )


def stamp_verified_current() -> bool:
    """Cheap compare-and-restamp for an idle daemon (and its idle-exit): prove
    the sources did not move, so a later daemonless search can answer green."""
    streak, _ = indexd_failing()
    if streak:
        return False
    sig_before = _signal_identity()
    live, unreadable = _census_map(_store_census(timeout_s=30.0))
    if live is None or unreadable:
        return False
    digests = _store_digests(live, _store_paths_census(timeout_s=30.0))
    record = _read_verified_record()
    if record is not None and live == record.census:
        if any(name in digests and digests[name] != recorded
               for name, recorded in record.digests.items()):
            # A member identity moved under an identical census: the sources
            # did move, so restamping would launder the drift into proof.
            return False
        return record_verified_current(live, digests=digests)
    # Sig-cover bootstrap: a census entirely at-or-below the *pinned* signal
    # proves coverage. A publication landing mid-census could post-date what
    # it never read, so any signal movement defers to the next idle lapse.
    if sig_before is None or _signal_identity() != sig_before:
        return False
    sig_mtime = sig_before[0] / 1e9
    if any(newest / 1000.0 > sig_mtime for _, newest in live.values()):
        return False
    return record_verified_current(live, digests=digests)


def _drift_report(now: float | None = None) -> DriftReport:
    """Live stores vs the published generation. Nothing here consults a
    wall-clock bound or a process: an idle weekend box is current forever."""
    if str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "") \
            == NO_AUTO_REFRESH_REASON:
        # --no-auto opted out of freshness work: no census may spawn on its
        # behalf, so the verdict is honestly unchecked, never guessed.
        return DriftReport("unknown", code="freshness-unchecked",
                           detail=NO_AUTO_REFRESH_REASON)
    # A resident reader can span the first or a replacement publication.
    # Reuse its census only while the publication identities are unchanged.
    key = _drift_cache_key()
    cached = getattr(_DRIFT_CACHE, "value", None)
    if (cached is not None and cached[0] == key
            and time.monotonic() - cached[1] < _DRIFT_CACHE_TTL_S):
        return cached[2]
    report = _compute_drift_report(now)
    _DRIFT_CACHE.value = (key, time.monotonic(), report)
    return report


def _compute_drift_report(
        now: float | None = None, *,
        store_rows: list | None | object = _UNOBSERVED_STORES,
) -> DriftReport:
    # Paths derive from the data dir the verdict is about, not import-time
    # constants: drift is a property of one publication.
    if not (common.DATA_DIR / "messages.jsonl").exists():
        # Nothing published: the first-run story owns this surface.
        return DriftReport("current")
    now = time.time() if now is None else now
    if store_rows is _UNOBSERVED_STORES:
        store_rows = _store_census()
    live, unreadable = _census_map(store_rows)
    if live is None:
        return DriftReport(
            "unknown", code="census-unavailable",
            detail="the live store census is unavailable, so drift "
                   "against the published index cannot be checked")
    if unreadable:
        # One unreadable store degrades only its own verdict: name it, hedge
        # (may-be-stale), and leave the census standing for the next probe.
        names = ", ".join(sorted(unreadable))
        plural = "s" if len(unreadable) != 1 else ""
        return DriftReport(
            "unknown", code="store-unreadable",
            detail=f"the {names} agent store{plural} could not be read, so "
                   "drift against the published index cannot be checked "
                   "for it")
    record = _read_verified_record()
    if record is not None and record.signature is not None \
            and record.signature != _ingest_signature_text():
        # The record vouches for one publication generation (the ingest
        # signature it pinned); a different live generation gets no vouching.
        record = None
    if record is not None and record.ts <= now + FRESHNESS_WRITE_RATE_S:
        return _record_drift(
            live, record.ts, record.census, now,
            recorded_digests=record.digests,
            live_digests=_live_store_digests(record.digests, live))
    try:
        sig_mtime = (common.DATA_DIR / ".ingest.sig").stat().st_mtime
    except FileNotFoundError:
        return DriftReport(
            "unknown", code="missing-ingest-signal",
            detail="the ingest freshness signal is missing for the "
                   "published corpus")
    except OSError as exc:
        return DriftReport(
            "unknown", code="unreadable-ingest-signal",
            detail=f"the ingest freshness signal cannot be read ({exc})")
    if sig_mtime - now > FRESHNESS_WRITE_RATE_S:
        # Skew safety: a future-dated clock could green-light anything.
        return DriftReport(
            "unknown", code="future-ingest-signal",
            detail="the ingest freshness signal is future-dated")
    return _signal_drift(live, sig_mtime, now)


def observe_store_drift(
        *, timeout_s: float = _DRIFT_PROBE_TIMEOUT_S,
) -> tuple[list | None, DriftReport]:
    """Observe source rows once and derive drift from that exact census."""
    rows = _store_census(timeout_s=timeout_s)
    return rows, _compute_drift_report(store_rows=rows)


def _live_store_digests(
        recorded_digests: dict[str, str],
        live: dict[str, tuple[int, int]]) -> dict[str, str]:
    """Re-derive the recorded stores' member identities from the armed
    listing; a store with no live identity simply claims nothing."""
    if not recorded_digests:
        return {}
    # The listing was armed alongside the census, whose consume already paid
    # most of the interactive wait; the remainder keeps the sub-500ms bound.
    paths = _consume_paths_probe(timeout_s=0.1)
    if paths is None:
        return {}
    return _store_digests(
        {name: entry for name, entry in live.items()
         if name in recorded_digests},
        paths)


def _record_drift(
        live: dict[str, tuple[int, int]], verified_ts: float,
        recorded: dict[str, tuple[int, int]], now: float, *,
        recorded_digests: dict[str, str] | None = None,
        live_digests: dict[str, str] | None = None) -> DriftReport:
    """Live stores vs the verified-current record, minus designed debounce."""
    recorded_digests = recorded_digests or {}
    live_digests = live_digests or {}
    changed = sorted(
        name for name in set(live) | set(recorded)
        if live.get(name) != recorded.get(name)
        or (name in recorded_digests and name in live_digests
            and recorded_digests[name] != live_digests[name]))
    behind = [
        name for name in changed
        if not _change_within_grace(
            live.get(name), recorded.get(name), verified_ts, now)]
    if behind:
        return DriftReport("drifted", len(behind), max(0.0, now - verified_ts))
    if changed and not _refresh_owner_possible():
        # Within the horizon but nothing absorbs it: the debounce defends
        # nothing, so the reader serves last-good and says so.
        return DriftReport(
            "drifted", len(changed), max(0.0, now - verified_ts),
            young=True)
    if changed:
        return DriftReport("current", absorbed=len(changed))
    return DriftReport("current")


def _change_within_grace(
        entry: tuple[int, int] | None, rec: tuple[int, int] | None,
        verified_ts: float, now: float) -> bool:
    """Is this store's deviation younger than the daemon's debounce horizon?

    Every deviation from an accurate record post-dates its stamp, so a fresh
    record bounds the drift's age outright. Past that, only a store whose
    newest write advanced beyond the record can date its own change; shrink
    and backdated changes have no fresh write to date them and stay behind."""
    if now - verified_ts <= DRIFT_GRACE_S:
        return True
    if entry is None or (rec is not None and entry[1] <= rec[1]):
        return False
    age = now - entry[1] / 1000.0
    # A far-future mtime must not greenwash forever (clock skew safety).
    return -FRESHNESS_WRITE_RATE_S <= age <= DRIFT_GRACE_S


def _refresh_owner_possible() -> bool:
    """Whether any background owner may absorb young drift. The debounce
    horizon is the daemon's designed catch-up window; with background
    refresh fenced off, drift of any age stays until the user acts."""
    return (not os.environ.get("AGREP_NO_DAEMON")
            and not _data_dir_readonly())


def _signal_drift(
        live: dict[str, tuple[int, int]], sig_mtime: float,
        now: float) -> DriftReport:
    """Record-less fallback: live stores vs the published signal's moment."""
    behind = 0
    graced = 0
    for name, (_files, newest) in live.items():
        if newest / 1000.0 <= sig_mtime:
            continue
        age = now - newest / 1000.0
        if -FRESHNESS_WRITE_RATE_S <= age <= DRIFT_GRACE_S:
            graced += 1
            continue
        behind += 1
    behind += _vanished_published_stores(live)
    if behind:
        return DriftReport("drifted", behind, max(0.0, now - sig_mtime))
    if graced and not _refresh_owner_possible():
        return DriftReport(
            "drifted", graced, max(0.0, now - sig_mtime), young=True)
    if graced:

        return DriftReport("current", absorbed=graced)
    return DriftReport("current")


def _vanished_published_stores(live: dict[str, tuple[int, int]]) -> int:
    """Shrink the newest-mtime compare cannot see: a published store with no
    live files left is a store-set change. The published agent set (one
    sessions.jsonl pass) is the only baseline the record-less fallback has;
    an unreadable or oversized corpus skips the compare rather than guess."""
    path = common.DATA_DIR / "sessions.jsonl"
    published: set[str] = set()
    try:
        if path.stat().st_size > _PUBLISHED_AGENTS_MAX_BYTES:
            return 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (RecursionError, TypeError, ValueError):
                    continue
                agent = row.get("agent") if isinstance(row, dict) else None
                if isinstance(agent, str) and agent:
                    published.add(agent)
    except (OSError, UnicodeError):
        return 0
    return sum(1 for name in published if name not in live)


def _last_good_publication_age() -> float | None:
    try:
        mtime = (common.DATA_DIR / ".ingest.sig").stat().st_mtime
    except OSError:
        return None
    return max(0.0, time.time() - mtime)


def _source_health_failure() -> surface.FreshnessFailure | None:
    path = common.DATA_DIR / ".source-health.json"
    try:
        observed = ownerfile.snapshot(path, max_bytes=_SOURCE_HEALTH_MAX_BYTES)
        record = json.loads(observed.raw.decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return surface.FreshnessFailure(
            "source-unreadable", f"the source health record is unreadable ({exc})")
    if not isinstance(record, dict) or record.get("code") != "source-unreadable":
        return surface.FreshnessFailure(
            "source-unreadable", "the source health record is malformed")
    issues = record.get("issues")
    issue = issues[0] if isinstance(issues, list) and issues else None
    if not isinstance(issue, dict):
        return surface.FreshnessFailure(
            "source-unreadable", "an agent-history source could not be read")
    source_path = str(issue.get("path") or "an agent-history source")
    reason = str(issue.get("reason") or "read failed")
    return surface.FreshnessFailure(
        "source-unreadable", f"{source_path} could not be read ({reason})")


def indexing_failure(
        *, daemon_status: dict[str, object] | None = None,
        drift_report: DriftReport | object = _UNOBSERVED_DRIFT,
) -> surface.FreshnessFailure | None:
    ledger = _auto_index_health()
    ledger_state, failures, reason = (
        ledger.state, ledger.streak, ledger.reason)
    source_failure = _source_health_failure()
    if ledger_state == "unavailable":
        if source_failure is not None:
            reason = f"{reason}; also: {source_failure.reason}"
        return surface.FreshnessFailure(
            "freshness-ledger-unavailable", reason)
    if source_failure is not None:
        return source_failure
    transient = getattr(_FRESHEN_FAILURE, "value", None)
    if isinstance(transient, surface.FreshnessFailure):
        return transient
    if surface.persistent_freshness_failure(failures):
        return surface.FreshnessFailure(
            "consecutive-failures", reason, failures)
    daemon = (
        indexd_resource_status()
        if daemon_status is None else daemon_status
    )
    daemon_state = str(daemon.get("state") or "")
    if daemon_state in ("daemon-unresponsive", "daemon-unverifiable"):
        return surface.FreshnessFailure(
            daemon_state,
            "the freshness daemon stopped acknowledging its heartbeat")
    blocking_states = {
        state.value for state in _INDEXD_INLINE_BLOCKING_STATES
        if state is not _IndexdOwnerState.MALFORMED_FRESH
    }
    if daemon_state in blocking_states:
        return surface.FreshnessFailure(
            "blocked-owner",
            f"the freshness owner is blocked ({daemon_state})")
    ingest = common.ingest_bin()
    if not ingest.exists():
        return surface.FreshnessFailure(
            "missing-ingest-binary", f"ingest binary is missing ({ingest})")
    # The verdict: drift against the published generation. Current means
    # green - regardless of signal age, daemon presence, or idle time.
    drift = (
        _drift_report()
        if drift_report is _UNOBSERVED_DRIFT else drift_report
    )
    if drift.state == "unknown" and drift.code != "freshness-unchecked":
        # --no-auto skipping the check is a choice, not a failure.
        return surface.FreshnessFailure(drift.code, drift.detail)
    return None


_UNOBSERVED_FAILURE = object()


def machine_freshness(
        *, checked: bool | None = None,
        failure: surface.FreshnessFailure | None | object = _UNOBSERVED_FAILURE,
        drift_report: DriftReport | object = _UNOBSERVED_DRIFT,
        publication_converging: bool | None = None,
) -> dict:
    if publication_converging is None:
        publication_converging = foreground_refresh_converging(
            checked=checked is not False)
    if failure is _UNOBSERVED_FAILURE:
        failure = indexing_failure(drift_report=drift_report)
    behind = None
    publication_in_progress = False
    if failure is None:
        if checked is not False:
            drift = (
                _drift_report()
                if drift_report is _UNOBSERVED_DRIFT else drift_report
            )
            if drift.state == "drifted":
                # A quantified drift verdict outranks the process-local
                # "this search skipped the refresh" note: behind states how
                # far, which is the answer the skip note only hedges about.
                behind = surface.freshness_behind_disclosure(
                    surface.FreshnessStory(
                        "behind", behind_s=drift.behind_s,
                        changed_stores=drift.changed_stores))
            elif drift.state == "current" and drift.absorbed:
                # Human output stays silent while the debounce owner works,
                # but machine callers must not mistake observed young drift
                # for proof that an empty result covered current sources.
                behind = surface.freshness_behind_disclosure(
                    surface.FreshnessStory(
                        "behind", changed_stores=drift.absorbed,
                        converging=True, young=True))
        if behind is None:
            deferred = getattr(_FOREGROUND_SNAPSHOT, "failure", None)
            if isinstance(deferred, surface.FreshnessFailure):
                if (deferred.code == "search-index-stale"
                        and publication_converging
                        and checked is not False):
                    publication_in_progress = True
                else:
                    failure = deferred
    if (failure is not None
            and failure.code == "freshness-ledger-unavailable"):
        out = {
            "state": "unknown", "failing": True, "may_be_stale": True,
            "code": failure.code,
        }
        checked = False
    elif behind is not None:
        out = behind
    elif publication_in_progress:
        out = surface.publication_freshness_disclosure()
    else:
        out = surface.freshness_disclosure(failure)
    if failure is not None:
        out["reason"] = _bounded_freshness_reason(failure.reason)
    if checked is not None:
        out["checked"] = checked
        if not checked and failure is None:
            out.update(state="unchecked", may_be_stale=True)
    return out


def sources_verified_current() -> bool:
    """Cheap census verdict for read-lane policy: True only when the live
    stores provably match the published generation. A reader that served a
    stamp-current snapshot skipped nothing in that case, so it has nothing
    to disclose instead of hedging on daemon duty-cycle.

    --no-auto opts out of freshness checks entirely: no census runs on its
    behalf, and its deferred-refresh hedge stays disclosed as before."""
    context = str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "")
    if context == NO_AUTO_REFRESH_REASON:
        return False
    return _drift_report().state == "current"


# Deferral reasons that name a live daemon generation as the refresh owner:
# only these justify promising that drift converges without user action.
FRESHENER_OWNS_REFRESH_REASON = "the background indexer owns the refresh"
REFRESH_DELEGATED_REASON = "search-index refresh is delegated to the background"
_CONVERGING_CONTEXTS = frozenset(
    (FRESHENER_OWNS_REFRESH_REASON, REFRESH_DELEGATED_REASON))
DELEGATED_PUBLICATION_WAIT_S = 1.0


def foreground_refresh_converging(*, checked: bool = True) -> bool:
    """Whether this request already verified an owner for deferred refresh."""
    context = str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "")
    return checked and _refresh_owner_possible() and context in _CONVERGING_CONTEXTS


def wait_for_delegated_publication(
        *, timeout_s: float = DELEGATED_PUBLICATION_WAIT_S) -> bool:
    """Give a newly spawned daemon one bounded chance to erase known drift."""
    context = str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "")
    if context != REFRESH_DELEGATED_REASON:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        report = _drift_report()
        if report.state == "current" and not report.absorbed:
            return True
        if not _daemon_will_converge():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(0.025, remaining))


# DriftReport "unknown" codes: the census could not vouch either way, so the
# per-search snapshot record (which names the cause and what was served) is
# the stronger evidence for the one rendered line.
_DRIFT_UNKNOWN_CODES = frozenset((
    "census-unavailable", "store-unreadable", "missing-ingest-signal",
    "unreadable-ingest-signal", "future-ingest-signal", "freshness-unchecked"))


def _daemon_will_converge() -> bool:
    """Whether a background indexer is actually going to absorb the drift."""
    if not _refresh_owner_possible():
        return False
    deferred = getattr(_FOREGROUND_SNAPSHOT, "failure", None)
    if getattr(deferred, "code", "") == DERIVED_STORE_OWNER_CODE:
        # A dead foreign owner is no longer a dead end: the kick's successor
        # takeover replaces it, and its verdict is the convergence fact. Only
        # a claim the kick cannot reclaim (a held live owner) stays False.
        return kick_background_repair().in_flight
    context = str(getattr(_FOREGROUND_SNAPSHOT, "context", "") or "")
    if context:
        return context in _CONVERGING_CONTEXTS
    return freshener_alive()


def rebuild_promise() -> str:
    """The slow-lane line's honest tail: who rebuilds, if anyone - and
    nothing when a recorded snapshot deferral means the freshness story
    renders the remedy itself (one owner; no census spawned here)."""
    if getattr(_FOREGROUND_SNAPSHOT, "failure", None) is not None:
        return ""
    if _daemon_will_converge():
        return "; the daemon rebuilds in the background"
    return f"; {surface.REMEDIES['index-behind-manual'].text}"


# Ownership faults are agrep's own bytes to reclaim (law 1): the story kicks
# the repair itself and converges only when the kick verifiably took (law 4).
# A LIVE foreign owner fails inside the kick, so that one state still renders.
_OWNERSHIP_FAULT_CODES = frozenset({DERIVED_STORE_OWNER_CODE, "blocked-owner"})


def _ownership_fault_healing(code: str) -> bool:
    if code not in _OWNERSHIP_FAULT_CODES:
        return False
    return kick_background_repair().in_flight


def freshness_story() -> surface.FreshnessStory:
    """The single freshness authority behind every display surface (D3)."""
    failure = indexing_failure()
    if failure is not None:
        if failure.code == "consecutive-failures":
            return surface.FreshnessStory(
                "failing", code=failure.code,
                consecutive_failures=failure.consecutive_failures,
                escalated=auto_index_escalated(),
                last_good_age_s=_last_good_publication_age(),
                detail=_bounded_freshness_reason(failure.reason))
        detail = failure.reason
        deferred = getattr(_FOREGROUND_SNAPSHOT, "failure", None)
        if (failure.code in _DRIFT_UNKNOWN_CODES
                and isinstance(deferred, surface.FreshnessFailure)
                and deferred.reason):
            detail = deferred.reason
        return surface.FreshnessStory(
            "unverified", code=failure.code,
            detail=_bounded_freshness_reason(detail),
            converging=_ownership_fault_healing(failure.code))
    drift = _drift_report()
    if drift.state == "drifted":
        converging = _daemon_will_converge()
        if not converging:
            # An observed drift is scheduled, not prescribed (laws 1 and 2):
            # the kick revives or confirms the daemon and the beat gives it a
            # publication to run now; only a decline leaves the manual line.
            kick = kick_background_repair()
            if kick.in_flight:
                try:
                    SEARCH_BEAT_PATH.touch()
                except OSError:
                    pass
                converging = True
        return surface.FreshnessStory(
            "behind", code="index-behind", behind_s=drift.behind_s,
            changed_stores=drift.changed_stores,
            converging=converging, young=drift.young)
    # The census compares live stores to the publication; the served search
    # index's own stamp is invisible to it, so a recorded read-time deferral
    # outranks a "current" verdict too - display must match machine_freshness.
    deferred = getattr(_FOREGROUND_SNAPSHOT, "failure", None)
    if isinstance(deferred, surface.FreshnessFailure):
        # A deferral naming a live refresh owner is work in flight, not
        # staleness (law 3): the verdict gating the rebuild promise gates this
        # hedge, so it can never render beside a daemon that owns the refresh.
        return surface.FreshnessStory(
            "unverified", code=deferred.code,
            detail=_bounded_freshness_reason(deferred.reason),
            converging=_daemon_will_converge())
    return surface.FreshnessStory(
        "current", absorbed_drift=bool(getattr(drift, "absorbed", 0)))


def agent_freshness_notice(environ: Mapping[str, str] | None = None) -> str:
    """One warning line for every human or agent display surface - or nothing,
    the common case. Rendering lives in surface_policy so search, recall, and
    around cannot drift apart on wording."""
    return surface.freshness_story_line(freshness_story())


# One full lane-down story per cause per window; repeats inside it render
# the bare line. Ten minutes: long enough to stop a per-query lecture, short
# enough that a persisting outage re-states its cause within one sitting.
_LANE_NOTICE_TTL_S = 600.0
_LANE_NOTICE_PATH_NAME = ".lane-notice.json"


def semantic_notice_brief(reason: object) -> bool:
    """Was this exact lane-down cause already told within the window?

    Disclosure-only state: a True never silences the notice, it only drops
    the repeated cause/retry tail (surface_policy renders the bare story).
    The stamp keeps its original timestamp while the cause persists, so a
    standing outage re-prints its full story once per window even under
    steady querying. Any unreadable or unwritable state answers False:
    the full story is always the safe rendering.
    """
    key = str(reason or "")
    path = common.DATA_DIR / _LANE_NOTICE_PATH_NAME
    now = time.time()
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
        age = now - float(stamp.get("ts") or 0.0)
        if stamp.get("reason") == key and 0 <= age < _LANE_NOTICE_TTL_S:
            return True
    except (OSError, ValueError, TypeError):
        pass
    if common.data_dir_readonly(common.DATA_DIR):
        return False
    try:
        path.write_text(
            json.dumps({"reason": key, "ts": now}), encoding="utf-8")
    except OSError:
        pass
    return False


def _bounded_freshness_reason(reason: object) -> str:
    rendered = common.terminal_safe(reason)
    if len(rendered) <= _FRESHNESS_RENDER_MAX_CHARS:
        return rendered
    half = (_FRESHNESS_RENDER_MAX_CHARS - 3) // 2
    return f"{rendered[:half]}...{rendered[-half:]}"


class _IndexdSpawnResult(Enum):
    READY = "ready"
    IN_FLIGHT = "in-flight"
    BLOCKED = "blocked"
    FAILED = "failed"


_OWN_SPAWN_GUARD = threading.local()


def _release_spawn_guard(guard: ownerfile.Handle) -> bool:
    released = guard.release(
        tombstone=True, require_stable_mtime=True)
    if not released:
        _OWN_SPAWN_GUARD.snapshot = guard.snapshot
    return released


def _reclassify_indexd_spawn_failure(
        result: _IndexdSpawnResult) -> _IndexdSpawnResult:
    """FAILED is the only result that permits inline work, so recheck ownership."""
    if result is not _IndexdSpawnResult.FAILED:
        return result
    if not _retire_legacy_indexd():
        _clear_own_spawn_guard(force=True)
        return _IndexdSpawnResult.BLOCKED
    inspected = _inspect_indexd_owner()
    if inspected.state is _IndexdOwnerState.COMPATIBLE:
        _clear_own_spawn_guard(force=True)
        return _IndexdSpawnResult.IN_FLIGHT
    if inspected.state in _INDEXD_INLINE_BLOCKING_STATES:
        _clear_own_spawn_guard(force=True)
        return _IndexdSpawnResult.BLOCKED
    return result


def _retain_spawn_guard(guard: ownerfile.Handle) -> None:
    _OWN_SPAWN_GUARD.snapshot = guard.snapshot
    try:
        guard.close()
    except OSError:
        pass


def _clear_own_spawn_guard(*, force: bool = False) -> None:
    snapshot = getattr(_OWN_SPAWN_GUARD, "snapshot", None)
    if snapshot is None:
        return
    current_guard = None
    try:
        candidate = _inspect_spawn_guard()
    except (FileNotFoundError, OSError):
        pass
    else:
        if (candidate.snapshot.identity == snapshot.identity
                and candidate.snapshot.raw == snapshot.raw):
            current_guard = candidate
    child_state = (
        _settle_spawn_child(current_guard)
        if current_guard is not None else _SpawnChildState.ABSENT)
    if child_state in (_SpawnChildState.ACTIVE, _SpawnChildState.BLOCKED):
        return
    if not force:
        if (current_guard is not None
                and child_state is not _SpawnChildState.RECLAIMED):
            if (current_guard.complete
                    and current_guard.owner in (
                        ownerfile.ProcessOwner.EXACT_LIVE,
                        ownerfile.ProcessOwner.UNVERIFIABLE)):
                return
            if (not current_guard.complete
                    and 0.0 <= current_guard.age < _INDEXD_PUBLICATION_GRACE_S):
                return
    try:
        removed = ownerfile.remove_exact(
            _SPAWN_GUARD_PATH, snapshot, tombstone=True,
            require_stable_mtime=True)
    except OSError:
        return
    if removed:
        _OWN_SPAWN_GUARD.snapshot = None
        return
    try:
        current = ownerfile.snapshot(_SPAWN_GUARD_PATH, max_bytes=256)
    except FileNotFoundError:
        _OWN_SPAWN_GUARD.snapshot = None
    except OSError:
        return
    else:
        if (current.identity != snapshot.identity
                or current.raw != snapshot.raw):
            _OWN_SPAWN_GUARD.snapshot = None


def _stop_unready_child(process: subprocess.Popen) -> bool:
    if common.WIN:
        if process.poll() is not None:
            return True
        try:
            process.terminate()
            process.wait(timeout=1.0)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None

    def reap_drained() -> bool:
        try:
            process.wait(timeout=1.0)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    if process.poll() is not None:
        return common._process_group_active(process.pid) is False
    process_start = common.process_start_identity(process.pid)
    if process_start:
        stopped = common.terminate_exact_process_tree(
            process.pid, process_start, wait_s=1.0,
            require_bound_tree=True)
        return stopped and reap_drained()

    import signal

    root_witness = common._ProcessExitWitness(process.pid)
    if not root_witness.registered or root_witness.exited():
        root_witness.close()
        return (
            common._process_group_active(process.pid) is False
            and reap_drained())
    watched = []
    try:
        try:
            process_group = os.getpgid(process.pid)
            process_session = os.getsid(process.pid)
            caller_group = os.getpgrp()
        except ProcessLookupError:
            return (
                common._process_group_active(process.pid) is False
                and reap_drained())
        except OSError:
            return False
        if (process_group != process.pid or process_session != process.pid
                or process_group == caller_group):
            return False
        watched = common._watch_process_group_members(
            process_group, process_session, process.pid)
        if root_witness.exited():
            return False

        deadline = time.monotonic() + 1.0
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            if common._process_group_active(process_group) is False:
                return reap_drained()
            return False
        except OSError:
            return False
        while time.monotonic() < deadline:
            active = common._process_group_active(process_group)
            if active is False:
                return reap_drained()
            if active is None:
                return False
            time.sleep(0.02)

        if root_witness.exited():
            if not common._original_group_survivor(
                    watched, process_group, process_session):
                return False
        else:
            try:
                topology_stable = (
                    os.getpgid(process.pid) == process_group
                    and os.getsid(process.pid) == process_session)
            except OSError:
                topology_stable = False
            if not topology_stable or root_witness.exited():
                return False
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return (
                common._process_group_active(process_group) is False
                and reap_drained())
        except OSError:
            return False
        final_deadline = time.monotonic() + 1.0
        while time.monotonic() < final_deadline:
            active = common._process_group_active(process_group)
            if active is False:
                return reap_drained()
            if active is None:
                return False
            time.sleep(0.02)
        return False
    finally:
        root_witness.close()
        for member in watched:
            member.exit_witness.close()


def _spawn_indexd() -> _IndexdSpawnResult:
    """Launch one detached daemon behind an exact in-flight/backoff claim."""
    if _data_dir_readonly():
        return _IndexdSpawnResult.BLOCKED
    if removal_fence.background_removal_active():
        return _IndexdSpawnResult.BLOCKED
    if not derived_writes_permitted():
        # Only an exact topology-verified incompatible daemon is retireable;
        # hostile or unverifiable claims remain fenced before the spawn guard.
        ownership = derived_writer_mutation_info()
        inspected = _inspect_indexd_owner(settle_child=False)
        if (not derived_writer_launchable(ownership)
                or inspected.state is not _IndexdOwnerState.INCOMPATIBLE):
            return _IndexdSpawnResult.BLOCKED
        inspected = _settle_indexd_owner(
            allow_retire=True, retire_budget_s=_INDEXD_ACQUIRE_WAIT_S)
        if (inspected.state is not _IndexdOwnerState.ABSENT
                or not derived_writes_permitted()):
            return _IndexdSpawnResult.BLOCKED
    _clear_own_spawn_guard()
    script = common.PY_DIR / "indexd.py"
    if not script.exists():
        return _IndexdSpawnResult.FAILED
    has_snapshot = (common.DATA_DIR / "corpus.db").exists()
    if not _retire_legacy_indexd():
        return _IndexdSpawnResult.BLOCKED
    inspected = _inspect_indexd_owner()
    if _indexd_ready(inspected):
        responsiveness = _indexd_responsiveness(inspected)[0]
        if responsiveness in ("responsive", "checking"):
            return _IndexdSpawnResult.READY
        if responsiveness == "unverifiable":
            return _IndexdSpawnResult.BLOCKED
    if (inspected.state is _IndexdOwnerState.ORPHANED_GROUP
            and inspected.snapshot is not None):
        owner_age = time.time() - inspected.snapshot.mtime
        if (has_snapshot
                and 0.0 <= owner_age < _INDEXD_STARTUP_GRACE_S):
            return _IndexdSpawnResult.IN_FLIGHT
    if inspected.state in _INDEXD_UNRETIRABLE_STATES:
        return _IndexdSpawnResult.BLOCKED
    if (inspected.state is _IndexdOwnerState.COMPATIBLE
            and inspected.snapshot is not None):
        owner_age = time.time() - inspected.snapshot.mtime
        if (has_snapshot
                and 0.0 <= owner_age < _INDEXD_STARTUP_GRACE_S):
            return _IndexdSpawnResult.IN_FLIGHT
    guard = None
    for _ in range(3):
        try:
            launcher_start = common.process_start_identity(os.getpid())
            if launcher_start is None:
                return _IndexdSpawnResult.FAILED
            raw = (
                f"state=launching pid={os.getpid()} "
                f"start={launcher_start} "
                f"token={secrets.token_hex(16)}\n"
            ).encode("ascii")
            guard = ownerfile.create_exclusive(
                _SPAWN_GUARD_PATH, raw, fsync=True, retain_fd=True)
            break
        except FileExistsError:
            try:
                guard_info = _inspect_spawn_guard()
                observed = guard_info.snapshot
                age = guard_info.age
                complete = guard_info.complete
                if (0.0 <= age < _INDEXD_PUBLICATION_GRACE_S
                        and not complete):
                    return _IndexdSpawnResult.IN_FLIGHT
                if complete:
                    if guard_info.owner is ownerfile.ProcessOwner.EXACT_LIVE:
                        return _IndexdSpawnResult.IN_FLIGHT
                    if guard_info.owner is ownerfile.ProcessOwner.UNVERIFIABLE:
                        if (_settle_spawn_child(guard_info)
                                is _SpawnChildState.ACTIVE):
                            return _IndexdSpawnResult.IN_FLIGHT
                        return _IndexdSpawnResult.BLOCKED
                    inspected = _inspect_indexd_owner()
                    if _indexd_ready(inspected):
                        responsiveness = _indexd_responsiveness(inspected)[0]
                        if responsiveness in ("responsive", "checking"):
                            return _IndexdSpawnResult.READY
                        if responsiveness == "unverifiable":
                            return _IndexdSpawnResult.BLOCKED
                    if inspected.state in _INDEXD_UNRETIRABLE_STATES:
                        return _IndexdSpawnResult.BLOCKED
                    if inspected.state is _IndexdOwnerState.INCOMPATIBLE:
                        inspected = _settle_indexd_owner(
                            allow_retire=True,
                            retire_budget_s=_INDEXD_ACQUIRE_WAIT_S)
                        if inspected.state is not _IndexdOwnerState.ABSENT:
                            return _IndexdSpawnResult.BLOCKED
                    child_state = _settle_spawn_child(guard_info)
                    if child_state is _SpawnChildState.ACTIVE:
                        return _IndexdSpawnResult.IN_FLIGHT
                    if child_state is _SpawnChildState.BLOCKED:
                        return _IndexdSpawnResult.BLOCKED
                removed = ownerfile.remove_exact(
                    _SPAWN_GUARD_PATH, observed, tombstone=True,
                    require_stable_mtime=True)
                if not removed:
                    return _IndexdSpawnResult.IN_FLIGHT
            except FileNotFoundError:
                continue
            except OSError:
                return _IndexdSpawnResult.BLOCKED
        except OSError:
            return _IndexdSpawnResult.FAILED
    if guard is None:
        return _IndexdSpawnResult.FAILED
    inspected = _inspect_indexd_owner()
    if _indexd_ready(inspected):
        responsiveness = _indexd_responsiveness(inspected)[0]
        if responsiveness in ("responsive", "checking"):
            _release_spawn_guard(guard)
            return _IndexdSpawnResult.READY
        if (responsiveness == "unverifiable"
                or not stop_indexd_owner(wait_s=_INDEXD_ACQUIRE_WAIT_S)):
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
        inspected = _settle_indexd_owner()
    if inspected.state is _IndexdOwnerState.INCOMPATIBLE:
        inspected = _settle_indexd_owner(
            allow_retire=True, retire_budget_s=_INDEXD_ACQUIRE_WAIT_S)
    if (inspected.state in _INDEXD_UNRETIRABLE_STATES
            or inspected.state is _IndexdOwnerState.INCOMPATIBLE):
        _release_spawn_guard(guard)
        return _IndexdSpawnResult.BLOCKED
    if inspected.state is _IndexdOwnerState.ORPHANED_GROUP:
        if (inspected.snapshot is None
                or not _retire_indexd_child(
                    inspected.snapshot,
                    wait_s=_INDEXD_ACQUIRE_WAIT_S)):
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
        inspected = _settle_indexd_owner()
        if inspected.state is not _IndexdOwnerState.ABSENT:
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
    if inspected.state is _IndexdOwnerState.COMPATIBLE:
        if inspected.snapshot is not None:
            owner_age = time.time() - inspected.snapshot.mtime
            if 0.0 <= owner_age < _INDEXD_STARTUP_GRACE_S:
                _release_spawn_guard(guard)
                return _IndexdSpawnResult.IN_FLIGHT
        if not stop_indexd_owner(wait_s=_INDEXD_ACQUIRE_WAIT_S):
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.IN_FLIGHT
    if removal_fence.background_removal_active():
        _release_spawn_guard(guard)
        return _IndexdSpawnResult.BLOCKED
    try:
        logf = common.open_bounded_log("indexd.log")
    except OSError:
        logf = subprocess.DEVNULL
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
                "cwd": str(common.REPO_ROOT)}
    if common.WIN:
        kw["creationflags"] = common.windows_background_child_flags(0x00000208)
    else:
        kw["start_new_session"] = True
    try:
        process = subprocess.Popen([sys.executable, str(script)], **kw)
        try:
            child_owner = _publish_spawn_child(guard, process)
        except (OSError, TypeError, ValueError):
            if _stop_unready_child(process):
                _retain_spawn_guard(guard)
                return _IndexdSpawnResult.FAILED
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
        if child_owner is ownerfile.ProcessOwner.UNVERIFIABLE:
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
        if child_owner is ownerfile.ProcessOwner.EXACT_LIVE:
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.IN_FLIGHT
        if _settle_spawn_child(
                _inspect_spawn_guard()) is _SpawnChildState.BLOCKED:
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.BLOCKED
        ready = _await_indexd_ready(
            process,
            timeout_s=_INDEXD_ACQUIRE_WAIT_S + _INDEXD_READY_WAIT_S)
        if ready:
            _release_spawn_guard(guard)
            return _IndexdSpawnResult.READY
        inspected = _inspect_indexd_owner()
        if (inspected.state is _IndexdOwnerState.COMPATIBLE
                and inspected.pid == process.pid):
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.IN_FLIGHT
        if inspected.state in _INDEXD_DELEGATING_STATES:
            if _stop_unready_child(process):
                _release_spawn_guard(guard)
            else:
                _retain_spawn_guard(guard)
            if inspected.state is _IndexdOwnerState.COMPATIBLE:
                return _IndexdSpawnResult.IN_FLIGHT
            return _IndexdSpawnResult.BLOCKED
        if _stop_unready_child(process):
            _retain_spawn_guard(guard)
            return _IndexdSpawnResult.FAILED
        _retain_spawn_guard(guard)
        return _IndexdSpawnResult.IN_FLIGHT
    except OSError:
        _retain_spawn_guard(guard)
        return _IndexdSpawnResult.FAILED
    finally:
        if hasattr(logf, "close"):
            try:
                logf.close()
            except OSError:
                pass


def _maybe_freshen() -> None:
    """Keep the index current before a search, the cheap way.

    An existence-only check would serve an index hours stale while new sessions
    pile up, so freshness leans on the headless daemon (indexd): when it is
    keeping the index hot, we just drop a search heartbeat and
    return, and the search reads the latest published corpus.db. If nothing is
    keeping it fresh, we spawn the daemon; an existing snapshot is served while it
    converges, while a cold install still builds inline.

    AGREP_NO_DAEMON disables background work. Once a transcript snapshot exists,
    the foreground reader serves it as-is; only the true first-run path may
    build its derived FTS inline.
    """
    _clear_freshen_failure()
    if _data_dir_readonly():
        common.dbg("freshen: AGREP_DATA_READONLY covers this data dir -> serve as-is")
        defer_foreground_refresh(READONLY_REFRESH_FENCE_REASON)
        return
    ingest = common.ingest_bin()
    if not ingest.exists():
        common.dbg(
            f"freshen: no ingest binary ({ingest}) -> "
            "cannot re-ingest, serving corpus as-is", "!")
        _set_freshen_failure(
            "missing-ingest-binary", f"ingest binary is missing ({ingest})")
        return
    if os.environ.get("AGREP_NO_DAEMON"):
        common.dbg(
            "freshen: AGREP_NO_DAEMON set -> published snapshot stays read-only")
        defer_foreground_refresh("background indexing is disabled")
        return
    if freshener_alive():
        try:
            SEARCH_BEAT_PATH.touch()
        except OSError:
            pass
        # Liveness is a claim, not proof: _daemon_will_converge re-checks it
        # against drift truth before any surface promises convergence.
        defer_foreground_refresh(FRESHENER_OWNS_REFRESH_REASON)
        common.dbg("freshen: a freshener is already hot -> just search the live corpus (no inline ingest)")
        return
    common.dbg("freshen: nothing keeping index hot -> spawn indexd daemon")
    spawned = _reclassify_indexd_spawn_failure(_spawn_indexd())
    # Materialized JSONL is the nonblocking fallback while the daemon publishes FTS.
    # Mark this process too, or a second first-run search would rebuild FTS inline.
    if (spawned in {
            _IndexdSpawnResult.READY,
            _IndexdSpawnResult.IN_FLIGHT,
    }
            and common.MESSAGES_PATH.exists()):
        _set_fts_delegated(True)
        defer_foreground_refresh(REFRESH_DELEGATED_REASON)
        common.dbg("freshen: serving published messages while the new daemon catches up")
        return
    if spawned is _IndexdSpawnResult.BLOCKED:
        if common.MESSAGES_PATH.exists():
            _set_fts_delegated(True)
        defer_foreground_refresh("background refresh is safely fenced")
        common.dbg("freshen: protected work prevents an inline refresh", "!")
        _set_freshen_failure(
            "blocked-owner", "the freshness owner is blocked")
        return
    if spawned is _IndexdSpawnResult.FAILED:
        common.dbg(
            "freshen: daemon launch failed -> serve the published snapshot", "!")
        _clear_own_spawn_guard(force=True)
        defer_foreground_refresh("the background indexer failed to start")
        return

    # ensure_index calls this function only after observing messages.jsonl. Reaching
    # an inline build here would redefine an existing publication as a first run.
    defer_foreground_refresh("background refresh is unavailable")


def ensure_index(auto: bool = True, *, quiet: bool = False) -> bool:
    """Make sure data/messages.jsonl exists and is reasonably fresh, building it on
    first use (and re-ingesting stale indexes) when we can.

    Returns True when the materialized corpus is present (already there, or freshly
    ingested), False when it's missing and we couldn't build it. The CLI's keyword
    paths call this so a fresh clone's first `agrep <pattern>` indexes itself instead
    of dead-ending on "no index yet", and so a later search picks up new sessions
    instead of serving a stale snapshot. `auto=False` (the --no-auto flag) skips both
    the build and the freshen: script-friendly, no daemon spawn, no surprise ingest.
    `quiet=True` keeps the first build's child output off structured stdout.
    """
    _clear_freshen_failure()
    common.dbg(
        f"ensure_index(auto={auto}): "
        f"messages.jsonl exists={common.MESSAGES_PATH.exists()}")
    if common.MESSAGES_PATH.exists():
        if auto:
            # The census answers "did the sources drift?" at render time;
            # start it here so it runs concurrently with the query instead of
            # after it. --no-auto spawns nothing, census included.
            _arm_drift_probe()
            _maybe_freshen()
        else:
            defer_foreground_refresh(NO_AUTO_REFRESH_REASON)
        return True
    cli = common.cli_name()
    if not auto:
        common.log(
            f"no index yet (--no-auto skipped the build). {common.setup_hint()}")
        return False
    if not common.ingest_bin().exists():
        _set_freshen_failure(
            "missing-ingest-binary",
            f"ingest binary is missing ({common.ingest_bin()})")
        common.log(f"no index yet, and no ingest binary (`{cli} doctor` shows the options). "
            f"next: run `{cli} index` - it offers to fetch the prebuilt binary, or "
            f"builds from source if Rust is installed.")
        return False
    common.log("first run - indexing your agent stores (one-time)…")
    return build_index(quiet=quiet, delegate_fts=True)
