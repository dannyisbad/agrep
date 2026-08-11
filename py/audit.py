"""`agrep audit` - cross-check the ingest's own accounting, file by file.

Scope: this starts from the files the adapters discovered, so a store no adapter
looks at is outside what audit can see. The independent raw census is available
only where a dumb recount is well defined (the JSONL stores below); the other
formats get the accounting check without one.

Joins three views of every discovered store file and holds them to account:

  discovery   `agrep-rs stores --paths` - every content file the adapters would parse
  the book    data/intake_stats.json  - per-file tallies the parsers counted while
              parsing (seen / rows / agent-side / named skips / errors)
  the census  an independent dumb re-count of the raw file (non-blank lines for
              jsonl stores) that never touches adapter parsing logic. Routine
              mode reuses identity-matched cached evidence and recounts only
              uncached/changed files inside a hard budget; --full ignores the
              cache and recounts every eligible file.

Checks, per file:
  identity    seen == rows + agent_rows + skips + errors   (a parser may skip for a
              named reason or fail loudly - never lose a record silently)
  census      census == seen, where the store format makes dumb counting well-defined
  freshness   the tally's fingerprint still matches the file on disk
  coverage    every discovered file has a tally at all

Exit codes: 0 clean · 1 warning-only coverage/drift · 2 accounting or
evidence error. ``--strict`` promotes warnings to errors.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import stat as statmod
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import common
import events
import fileops
from hookless import registry
import ownerfile
import surface_policy as surface

BOOK_PATH = common.DATA_DIR / "intake_stats.json"
BOOK_VERSION = 1
_BOOK_MAX_BYTES = 128 * 1024 * 1024
_TOKEN_ID_PREFIX = "\0agrep-intake-token-v1\0"
CACHE_PATH = common.DATA_DIR / ".audit-census.json"
CACHE_VERSION = 1
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_STORE_OUTPUT_MAX_BYTES = 8 * 1024 * 1024
_STORE_OUTPUT_MAX_CHARS = _STORE_OUTPUT_MAX_BYTES  # compatibility for focused tests
_FULL_STORE_TIMEOUT_S = 30.0
_ROUTINE_BUDGET_S = 0.90
_ROUTINE_STORE_TIMEOUT_S = 0.40
_ROUTINE_FINAL_SNAPSHOT_RESERVE_S = 0.40
_ROUTINE_JSON_PARSE_MAX_BYTES = 16 * 1024 * 1024
_ROUTINE_SQLITE_COPY_MAX_BYTES = 64 * 1024 * 1024
_ROUTINE_INDEXED_SCAN_MAX_BYTES = 128 * 1024 * 1024
_ROUTINE_INDEXED_QUERY_BUDGET_S = 0.10
_SQLITE_PROGRESS_OPS = 1_000
_CENSUS_CHUNK_BYTES = 64 * 1024
_PROBLEM_SAMPLE_LIMIT = 40
_GAP_SAMPLE_LIMIT = 40
_DETAIL_MAX_CHARS = 2_048
_INDEXED_DEADLINE_OVERRIDE: float | None = None
_NO_CENSUS_PROBLEM = (
    "no source file census completed; audit cannot certify completeness")

# adapters whose census is a plain non-blank-line count of the file
JSONL_CENSUS = {"claude", "codex", "kimi", "pi"}


class AuditEvidenceError(RuntimeError):
    pass


class AuditRoutineBudget(AuditEvidenceError):
    """Routine evidence collection ran out of its bounded observation window."""


class AuditSnapshotDeferred(AuditRoutineBudget):
    """One routine external snapshot hit its local cap; other joins may proceed."""


class _DetailLog:
    """Count every encountered finding while retaining only bounded samples."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.total = 0
        self.items: list[str] = []

    def append(self, detail: object) -> None:
        self.total += 1
        if len(self.items) >= self.limit:
            return
        rendered = str(detail)
        if len(rendered) > _DETAIL_MAX_CHARS:
            omitted = len(rendered) - _DETAIL_MAX_CHARS
            rendered = rendered[:_DETAIL_MAX_CHARS] + f" … [+{omitted} chars]"
        self.items.append(rendered)

    @property
    def omitted(self) -> int:
        return self.total - len(self.items)

    def __bool__(self) -> bool:
        return bool(self.total)

    def __len__(self) -> int:
        return self.total

    def __getitem__(self, key):
        return self.items[key]


def _deadline_check(deadline: float | None, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise AuditRoutineBudget(
            f"routine audit incomplete: {label} exceeded the shared "
            "observation budget; run agrep audit --full")


def _unique_json_object(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate evidence field: {key}")
        record[key] = value
    return record


def _entry_identity(
        info: os.stat_result | fileops.FileIdentity
        ) -> tuple[int, int, int, int, int]:
    if not hasattr(info, "st_dev"):
        if len(info) != 5:
            raise ValueError("file identity must have five fields")
        return tuple(int(value) for value in info)
    return (int(info.st_dev), int(info.st_ino), int(info.st_size),
            int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9))),
            int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9))))


def _path_within(path: Path, root: Path) -> bool:
    try:
        candidate = os.path.normcase(os.path.realpath(os.fspath(path)))
        boundary = os.path.normcase(os.path.realpath(os.fspath(root)))
        return os.path.commonpath((candidate, boundary)) == boundary
    except (OSError, ValueError):
        return False


def _census_cache_path() -> Path | None:
    """Keep dev/test acceleration outside an explicitly protected data dir."""
    if not common.data_dir_readonly(common.DATA_DIR):
        return CACHE_PATH
    # A Windows shared-temp cache needs structural owner/ACL evidence.
    # Synthetic st_uid/st_mode establishes neither, so run without this
    # acceleration and keep the read-only audit correct.
    if os.name == "nt":
        return None
    identity = os.path.normcase(
        os.path.realpath(os.fspath(common.DATA_DIR))).encode(
            "utf-8", errors="surrogateescape")
    suffix = hashlib.sha256(identity).hexdigest()[:24]
    scratch = Path(tempfile.gettempdir()).resolve(strict=False)
    candidate = scratch / f"agrep-audit-census-{suffix}.json"
    if _path_within(candidate, common.DATA_DIR):
        return None
    return candidate


def _valid_cache_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    identity = record.get("identity")
    witness = record.get("identity_sha256")
    lines = record.get("nonblank_lines")
    return (
        isinstance(identity, list)
        and len(identity) == 5
        and all(type(value) is int for value in identity)
        and (witness is None or _lower_sha256(witness))
        and type(lines) is int
        and lines >= 0
    )


def _cache_entry_owned(path: Path) -> bool:
    """A shared temp directory must not make another uid's census authoritative."""
    if os.name == "nt":
        try:
            # Keep this string-based so platform simulation does not ask
            # pathlib to instantiate a foreign-platform concrete path class.
            shared_temp = os.path.realpath(tempfile.gettempdir())
        except OSError:
            return False
        if _path_within(path, shared_temp):
            return False
    try:
        info = _plain_entry(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and int(getattr(info, "st_uid", -1)) != int(getuid()):
        return False
    # Outside shared temp, Windows trust is ACL-based; its synthetic stat mode
    # is commonly 0666 and therefore cannot be interpreted as POSIX provenance.
    return os.name == "nt" or not bool(
        statmod.S_IMODE(info.st_mode) & 0o022)


def _cache_snapshot(path: Path) -> ownerfile.Snapshot:
    """Read one cache only while its strong path identity remains unchanged."""
    before = fileops.file_identity(path)
    snapshot = ownerfile.snapshot(path, max_bytes=_CACHE_MAX_BYTES)
    after = fileops.file_identity(path)
    snapshot_identity = tuple(snapshot.identity)
    strong_identity = tuple(after[:4])
    same_entry = (
        snapshot_identity[1:] == strong_identity[1:]
        if os.name == "nt" else snapshot_identity == strong_identity)
    if before != after or not same_entry:
        raise ownerfile.OwnershipLost(
            f"census cache was replaced while reading: {path}")
    return snapshot


def _valid_indexed_cache_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    identity = record.get("identity")
    agents = record.get("agents")
    return (
        isinstance(identity, list)
        and all(
            isinstance(member, list)
            and len(member) == 2
            and isinstance(member[0], str)
            and isinstance(member[1], list)
            and len(member[1]) == 5
            and all(type(value) is int for value in member[1])
            for member in identity
        )
        and isinstance(agents, list)
        and len(set(agents)) == len(agents)
        and all(
            isinstance(agent, str) and agent in registry.ADAPTER_NAMES
            for agent in agents
        )
    )


def _read_audit_cache(
        path: Path | None, *, deadline: float | None = None,
        ) -> tuple[dict[str, dict], dict | None]:
    if path is None:
        return {}, None
    _deadline_check(deadline, "census-cache inspection")
    if not _cache_entry_owned(path):
        return {}, None
    try:
        if deadline is not None:
            size = int(_plain_entry(path).st_size)
            if size > _ROUTINE_JSON_PARSE_MAX_BYTES:
                raise AuditRoutineBudget(
                    "routine census-cache parsing deferred: "
                    f"{_human_bytes(size)} exceeds the "
                    f"{_human_bytes(_ROUTINE_JSON_PARSE_MAX_BYTES)} "
                    "routine JSON tier")
        raw = _cache_snapshot(path).raw
        _deadline_check(deadline, "census-cache read")
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        _deadline_check(deadline, "census-cache parsing")
    except FileNotFoundError:
        return {}, None
    except AuditRoutineBudget:
        raise
    except (OSError, UnicodeError, RecursionError, ValueError):
        # The cache is acceleration, never an authority required to audit.
        return {}, None
    if (not isinstance(data, dict)
            or data.get("version") != CACHE_VERSION
            or not isinstance(data.get("files"), dict)):
        return {}, None
    files = data["files"]
    if any(not isinstance(path, str) or not _valid_cache_record(record)
           for path, record in files.items()):
        return {}, None
    indexed = data.get("indexed_agents")
    if indexed is not None and not _valid_indexed_cache_record(indexed):
        indexed = None
    return files, indexed


def _read_census_cache(
        path: Path | None, *, deadline: float | None = None,
        ) -> dict[str, dict]:
    return _read_audit_cache(path, deadline=deadline)[0]


def _cache_count(
        cache: dict[str, dict], path: str, *,
        identity: tuple[int, int, int, int, int] | None = None,
        identity_sha256: str | None = None) -> int | None:
    record = cache.get(path)
    if not _valid_cache_record(record):
        return None
    if identity_sha256 is not None:
        if record.get("identity_sha256") != identity_sha256:
            return None
    elif identity is not None:
        if tuple(record.get("identity") or ()) != identity:
            return None
    else:
        return None
    return int(record["nonblank_lines"])


def _write_census_cache(
        path: Path | None, files: dict[str, dict], *,
        indexed_agents: dict | None = None,
        ) -> bool:
    if path is None:
        return False
    if (common.data_dir_readonly(common.DATA_DIR)
            and _path_within(path, common.DATA_DIR)):
        return False
    if not _cache_entry_owned(path):
        return False
    payload = {"version": CACHE_VERSION, "files": files}
    if indexed_agents is not None:
        payload["indexed_agents"] = indexed_agents
    raw = (json.dumps(
        payload,
        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8")
    if len(raw) > _CACHE_MAX_BYTES:
        return False
    fd = -1
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        common.replace_with_retry(temporary, path)
        temporary = None
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _human_bytes(size: int) -> str:
    amount = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return (f"{int(amount)} {unit}" if unit == "B"
                    else f"{amount:.1f} {unit}")
        amount /= 1024.0
    return f"{size} B"


def _plain_entry(path: str | os.PathLike) -> os.stat_result:
    value = os.fspath(path)
    info = os.lstat(value)
    reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not statmod.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        raise OSError(f"source is not a plain regular file: {value}")
    return info


@contextmanager
def _plain_binary(path: str | os.PathLike):
    value = os.fspath(path)
    entry = fileops.file_identity(Path(value))
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(value, flags)
    stream = None
    try:
        opened = os.fstat(fd)
        opened_identity = fileops.file_identity_fd(fd)
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not statmod.S_ISREG(opened.st_mode)
                or bool(getattr(opened, "st_file_attributes", 0) & reparse)
                or entry != opened_identity):
            raise OSError(f"source changed before reading: {value}")
        stream = os.fdopen(fd, "rb")
        fd = -1
        yield stream
        after = fileops.file_identity_fd(stream.fileno())
        if (opened_identity != after
                or fileops.file_identity(Path(value)) != after):
            raise OSError(f"source changed while reading: {value}")
    finally:
        if stream is not None:
            stream.close()
        elif fd >= 0:
            os.close(fd)


def _store_command(
        flag: str | list[str], label: str,
        timeout_s: float) -> subprocess.CompletedProcess:
    """Run one read-only store snapshot with bounded time and output."""
    if timeout_s <= 0:
        raise AuditEvidenceError(
            f"{label} unavailable: no observation budget remained")
    binp = common.ingest_bin()
    flags = [flag] if isinstance(flag, str) else list(flag)
    try:
        result = subprocess.run(
            [str(binp), "stores", *flags], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        if timeout_s <= _ROUTINE_STORE_TIMEOUT_S:
            raise AuditSnapshotDeferred(
                f"{label} unavailable: read-only store snapshot timed out "
                f"after {timeout_s:.3f}s within the routine budget; "
                "run agrep audit --full") from error
        raise AuditEvidenceError(
            f"{label} unavailable: read-only store snapshot timed out after "
            f"{timeout_s:.3f}s") from error
    except OSError as error:
        raise AuditEvidenceError(
            f"{label} unavailable: cannot launch read-only store snapshot: "
            f"{error}") from error
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if len(stdout.encode("utf-8")) > _STORE_OUTPUT_MAX_BYTES:
        raise AuditEvidenceError(
            f"{label} unavailable: store snapshot output exceeded "
            f"{_STORE_OUTPUT_MAX_BYTES} bytes")
    if result.returncode != 0:
        detail = stderr.strip()[:200] or f"exit {result.returncode}"
        raise AuditEvidenceError(f"{label} failed: {detail}")
    return result


def _lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(
            byte in "0123456789abcdef"
            for byte in value
        )
    )


def _stat_key_valid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    fields = value.split(":")
    return (
        len(fields) == 3
        and fields[0] == "s"
        and fields[1].isdigit()
        and fields[2].isdigit()
    )


def _discovered(
        *, timeout_s: float = _FULL_STORE_TIMEOUT_S,
        ) -> list[tuple[str, str]]:
    r = _store_command("--paths", "store discovery", timeout_s)
    try:
        payload = json.loads(r.stdout or "[]")
        if not isinstance(payload, list):
            raise ValueError("store discovery output is not a list")
        rows = []
        seen = set()
        unhealthy = []
        for item in payload:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("path"), str)):
                raise ValueError("store discovery output has an invalid entry")
            state = item.get("state")
            if state != "available":
                kind = str(item.get("kind") or "source-read-failed")
                reason = str(item.get("reason") or "source could not be read")
                unhealthy.append(
                    f"{item['name']}: {item['path']}: {kind}: {reason}")
                continue
            if item["name"] not in registry.ADAPTER_NAMES:
                raise ValueError(
                    f"store discovery output names unknown adapter {item['name']!r}")
            row = (item["name"], item["path"])
            if row in seen:
                raise ValueError("store discovery output has a duplicate entry")
            seen.add(row)
            rows.append(row)
        if unhealthy:
            detail = "; ".join(unhealthy[:3])
            if len(unhealthy) > 3:
                detail += f"; and {len(unhealthy) - 3} more"
            raise AuditEvidenceError(f"store discovery is degraded: {detail}")
        return rows
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise AuditEvidenceError(f"store discovery evidence is invalid: {error}") from error


def _live_tokens(
        *, timeout_s: float = _FULL_STORE_TIMEOUT_S,
        ) -> dict[str, tuple[str, str]]:
    result = _store_command("--tokens", "token census", timeout_s)
    try:
        payload = json.loads(result.stdout or "[]")
        if not isinstance(payload, list):
            raise ValueError("token census output is not a list")
        rows: dict[str, tuple[str, str]] = {}
        unhealthy = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("state"), str):
                raise ValueError("token census output has an invalid entry")
            if item["state"] == "source-unreadable":
                unhealthy.append(
                    f"{item.get('name', '?')}: {item.get('path', '')}: "
                    f"{item.get('kind', 'token-census-unreadable')}: "
                    f"{item.get('reason', 'read failed')}")
                continue
            if (item["state"] != "token"
                    or item.get("name") not in registry.ADAPTER_NAMES
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("key"), str) or not item["key"]
                    or item["id"] in rows):
                raise ValueError("token census output has an invalid or duplicate token")
            rows[item["id"]] = (item["name"], item["key"])
        if unhealthy:
            raise AuditEvidenceError(
                "token census is degraded: " + "; ".join(unhealthy[:3]))
        return rows
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise AuditEvidenceError(f"token census evidence is invalid: {error}") from error


def _parse_store_audit_payload(raw: str, *, selection: str = "all") -> dict:
    try:
        payload = json.loads(
            raw or "null", object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict):
            raise ValueError("top level is not an object")
        expected = {
            "schema", "version", "selection",
            "snapshot_sha256", "boundary_sha256",
            "paths", "tokens", "issues", "complete",
        }
        if set(payload) != expected:
            raise ValueError("top-level fields do not match schema v1")
        if (payload["schema"] != "agrep.store-audit"
                or payload["version"] != 1):
            raise ValueError("schema name or version is unsupported")
        if payload["selection"] != selection:
            raise ValueError("snapshot selection does not match the request")
        if (not _lower_sha256(payload["snapshot_sha256"])
                or not _lower_sha256(payload["boundary_sha256"])):
            raise ValueError("snapshot digest is not lowercase SHA-256")
        if type(payload["complete"]) is not bool:
            raise ValueError("complete is not boolean")

        paths = payload["paths"]
        if not isinstance(paths, list):
            raise ValueError("paths is not a list")
        discovered = []
        witnesses = {}
        for item in paths:
            if (not isinstance(item, dict)
                    or set(item) != {
                        "name", "path", "stat_key", "identity_sha256"}):
                raise ValueError("path row fields do not match schema v1")
            name = item["name"]
            path = item["path"]
            stat_key = item["stat_key"]
            identity_sha256 = item["identity_sha256"]
            if (name not in registry.ADAPTER_NAMES
                    or not isinstance(path, str) or not path
                    or not _stat_key_valid(stat_key)
                    or not _lower_sha256(identity_sha256)):
                raise ValueError("path row has an invalid value")
            row = (name, path)
            if row in witnesses:
                raise ValueError("path rows contain a duplicate")
            discovered.append(row)
            witnesses[row] = (stat_key, identity_sha256)

        token_payload = payload["tokens"]
        if not isinstance(token_payload, list):
            raise ValueError("tokens is not a list")
        tokens = {}
        for item in token_payload:
            if (not isinstance(item, dict)
                    or set(item) != {"name", "id", "key"}):
                raise ValueError("token row fields do not match schema v1")
            name = item["name"]
            row_id = item["id"]
            key = item["key"]
            if (name not in registry.ADAPTER_NAMES
                    or not isinstance(row_id, str) or not row_id
                    or not isinstance(key, str) or not key
                    or row_id in tokens):
                raise ValueError("token row has an invalid or duplicate value")
            tokens[row_id] = (name, key)

        issues = payload["issues"]
        if not isinstance(issues, list):
            raise ValueError("issues is not a list")
        issue_rows = []
        issue_seen = set()
        for item in issues:
            if (not isinstance(item, dict)
                    or set(item) != {"agent", "path", "kind", "reason"}):
                raise ValueError("issue row fields do not match schema v1")
            row = tuple(item[field] for field in (
                "agent", "path", "kind", "reason"))
            if (not all(isinstance(value, str) and value for value in row)
                    or row[0] not in {*registry.ADAPTER_NAMES, "all"}
                    or row in issue_seen):
                raise ValueError("issue row has an invalid or duplicate value")
            issue_seen.add(row)
            issue_rows.append(row)
        if payload["complete"] and issue_rows:
            raise ValueError("complete snapshot carries named issues")
        if not payload["complete"]:
            if issue_rows:
                detail = "; ".join(
                    f"{agent}: {path}: {kind}: {reason}"
                    for agent, path, kind, reason in issue_rows[:3])
                if len(issue_rows) > 3:
                    detail += f"; and {len(issue_rows) - 3} more"
                raise AuditEvidenceError(
                    f"store audit snapshot is degraded: {detail}")
            raise AuditEvidenceError(
                "store audit snapshot is incomplete without a named issue")
        return {
            "snapshot_sha256": payload["snapshot_sha256"],
            "boundary_sha256": payload["boundary_sha256"],
            "selection": selection,
            "discovered": discovered,
            "tokens": tokens,
            "witnesses": witnesses,
        }
    except AuditEvidenceError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
        raise AuditEvidenceError(
            f"store audit snapshot evidence is invalid: {error}") from error


def _combined_store_snapshot(
        *, selection: str = "all",
        timeout_s: float = _FULL_STORE_TIMEOUT_S) -> dict:
    result = _store_command(
        ["--audit", "--agent", selection],
        "combined store audit snapshot", timeout_s)
    return _parse_store_audit_payload(
        result.stdout or "", selection=selection)


def _store_helpers_overridden() -> bool:
    """Keep focused legacy mocks useful without changing production dispatch."""
    return any(
        getattr(helper, "__module__", None) != __name__
        for helper in (_discovered, _live_tokens)
    )


def _store_snapshot(
        *, selection: str = "all",
        timeout_s: float = _FULL_STORE_TIMEOUT_S) -> dict:
    if not _store_helpers_overridden():
        return _combined_store_snapshot(
            selection=selection, timeout_s=timeout_s)
    # Compatibility tests replace these readers independently.
    # Production keeps the store snapshot in one Rust subprocess.
    tokens = _live_tokens(timeout_s=timeout_s)
    discovered = _discovered(timeout_s=timeout_s)
    if selection != "all":
        discovered = [
            row for row in discovered if row[0] == selection]
        tokens = {
            key: value for key, value in tokens.items()
            if value[0] == selection}
    normalized = json.dumps(
        [sorted(discovered), sorted(tokens.items())],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(normalized).hexdigest()
    return {
        "snapshot_sha256": digest,
        "boundary_sha256": digest,
        "selection": selection,
        "discovered": discovered,
        "tokens": tokens,
        "witnesses": {},
    }


def _book(*, deadline: float | None = None) -> dict:
    try:
        _deadline_check(deadline, "evidence-book inspection")
        if deadline is not None:
            size = int(_plain_entry(BOOK_PATH).st_size)
            if size > _ROUTINE_JSON_PARSE_MAX_BYTES:
                raise AuditRoutineBudget(
                    "routine evidence-book parsing deferred: "
                    f"{_human_bytes(size)} exceeds the "
                    f"{_human_bytes(_ROUTINE_JSON_PARSE_MAX_BYTES)} "
                    "routine JSON tier; run agrep audit --full")
        raw = ownerfile.snapshot(BOOK_PATH, max_bytes=_BOOK_MAX_BYTES).raw
        _deadline_check(deadline, "evidence-book read")
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        _deadline_check(deadline, "evidence-book parsing")
    except FileNotFoundError:
        return {}
    except AuditRoutineBudget:
        raise
    except (OSError, UnicodeError) as error:
        raise AuditEvidenceError(
            f"cannot read evidence book {BOOK_PATH}: {error}") from error
    except (RecursionError, ValueError) as error:
        raise AuditEvidenceError(
            f"evidence book {BOOK_PATH} is corrupt: {error}") from error
    if (not isinstance(data, dict) or data.get("version") != BOOK_VERSION
            or not isinstance(data.get("files"), dict)):
        raise AuditEvidenceError(
            f"evidence book {BOOK_PATH} has an invalid version or files table")
    return data["files"]


def _stat_evidence(
        path: str) -> tuple[str, tuple[int, int, int, int, int]]:
    try:
        with _plain_binary(path) as stream:
            stream.read(1)
            info = os.fstat(stream.fileno())
            # fileops supplies Win32 ChangeTime in field five. os.stat().st_ctime
            # is creation time on older Windows Python and cannot invalidate a
            # same-size rewrite whose mtime was restored.
            identity = _entry_identity(
                fileops.file_identity_fd(stream.fileno()))
        key = f"s:{int(info.st_mtime_ns // 1_000_000)}:{info.st_size}"
        return key, identity
    except OSError as error:
        raise AuditEvidenceError(f"cannot stat source {path}: {error}") from error


def _stat_key(path: str) -> str:
    return _stat_evidence(path)[0]


def _census_jsonl(path: str, *, deadline: float | None = None) -> int:
    try:
        with _plain_binary(path) as stream:
            nonblank = 0
            line_has_content = False
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise AuditRoutineBudget(
                        "routine audit census budget exhausted")
                chunk = stream.read(_CENSUS_CHUNK_BYTES)
                if deadline is not None and time.monotonic() >= deadline:
                    raise AuditRoutineBudget(
                        "routine audit census budget exhausted")
                if not chunk:
                    return nonblank + int(line_has_content)
                parts = chunk.split(b"\n")
                for part in parts[:-1]:
                    line_has_content = line_has_content or bool(part.strip())
                    nonblank += int(line_has_content)
                    line_has_content = False
                line_has_content = line_has_content or bool(parts[-1].strip())
    except AuditRoutineBudget:
        raise
    except OSError as error:
        raise AuditEvidenceError(f"cannot census source {path}: {error}") from error


def _path_state(path: str) -> tuple[str, str]:
    try:
        _plain_entry(path)
        return "present", ""
    except FileNotFoundError:
        return "missing", ""
    except OSError as error:
        return "unreadable", str(error)


def _optional_plain_identity(
        path: os.PathLike) -> tuple[int, int, int, int, int] | None:
    try:
        # `st_ctime_ns` is creation time on supported older Windows Pythons.
        # Use the source census's no-follow identity so restored mtimes still
        # advance FILE_BASIC_INFO.ChangeTime after same-size rewrites.
        return _entry_identity(fileops.file_identity(Path(path)))
    except FileNotFoundError:
        return None


def _sqlite_family_identity(path: Path) -> tuple:
    """Strong, cheap identity for final-boundary comparison without a requery."""
    family = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        member = Path(f"{path}{suffix}")
        try:
            family.append((
                suffix,
                _entry_identity(fileops.file_identity(member)),
            ))
        except FileNotFoundError:
            if not suffix:
                return ()
        except OSError as error:
            raise AuditEvidenceError(
                f"cannot stat indexed-row evidence {member}: {error}") from error
    return tuple(family)


def _indexed_cache_record(identity: tuple, agents: set[str]) -> dict:
    return {
        "identity": [
            [suffix, list(member)] for suffix, member in identity
        ],
        "agents": sorted(
            agent for agent in agents if agent in registry.ADAPTER_NAMES),
    }


def _indexed_cache_matches(record: dict | None, identity: tuple) -> bool:
    if not _valid_indexed_cache_record(record):
        return False
    observed = tuple(
        (str(suffix), tuple(int(value) for value in member))
        for suffix, member in record["identity"]
    )
    return observed == identity


def _sqlite_direct_read_eligible(path: Path, family: tuple) -> bool:
    members = dict(family)
    wal = members.get("-wal")
    journal = members.get("-journal")
    if ((wal is not None and wal[2] > 0)
            or (journal is not None and journal[2] > 0)):
        return False
    try:
        with _plain_binary(path) as stream:
            header = stream.read(20)
    except OSError:
        return False
    return (
        len(header) == 20
        and header[:16] == b"SQLite format 3\0"
        and header[18:20] == b"\x01\x01"
    )


def _indexed_agents(*, deadline: float | None = None) -> set[str]:
    if deadline is None:
        deadline = _INDEXED_DEADLINE_OVERRIDE
    path = common.DATA_DIR / "corpus.db"
    state, detail = _path_state(str(path))
    if state == "missing":
        return set()
    if state == "unreadable":
        raise AuditEvidenceError(
            f"cannot stat indexed-row evidence {path}: {detail}")
    db = None
    progress_installed = False
    try:
        _deadline_check(deadline, "indexed-row snapshot")
        family = _sqlite_family_identity(path)
        if deadline is not None:
            family_bytes = sum(identity[2] for _suffix, identity in family)
            direct = _sqlite_direct_read_eligible(path, family)
            if not direct and family_bytes > _ROUTINE_SQLITE_COPY_MAX_BYTES:
                raise AuditRoutineBudget(
                    "routine indexed-row snapshot deferred: a live SQLite "
                    f"family of {_human_bytes(family_bytes)} exceeds the "
                    f"{_human_bytes(_ROUTINE_SQLITE_COPY_MAX_BYTES)} "
                    "routine copy tier; run agrep audit --full")
            main_size = dict(family).get("", (0, 0, 0, 0, 0))[2]
            if main_size > _ROUTINE_INDEXED_SCAN_MAX_BYTES:
                raise AuditRoutineBudget(
                    "routine indexed-agent DISTINCT census deferred: "
                    f"{_human_bytes(main_size)} exceeds the "
                    f"{_human_bytes(_ROUTINE_INDEXED_SCAN_MAX_BYTES)} "
                    "unindexed scan tier; run agrep audit --full once to "
                    "publish identity-bound indexed-agent evidence")
            remaining = deadline - time.monotonic()
            _deadline_check(deadline, "indexed-row snapshot")
            timeout_s = min(0.25, max(0.0, remaining))
        else:
            timeout_s = 0.25
        db = events.open_sqlite_snapshot(path, timeout_s)
        _deadline_check(deadline, "indexed-row snapshot")
        set_progress = getattr(db, "set_progress_handler", None)
        if deadline is not None and callable(set_progress):
            set_progress(
                lambda: int(time.monotonic() >= deadline),
                _SQLITE_PROGRESS_OPS)
            progress_installed = True
        agents = {
            str(row[0]) for row in db.execute(
                "SELECT DISTINCT agent FROM msgs WHERE agent <> ''")
            if row[0]
        }
        _deadline_check(deadline, "indexed-row DISTINCT census")
        source_stable = getattr(db, "source_stable", None)
        if source_stable is not None and not source_stable():
            raise OSError("SQLite evidence changed during inspection")
        return agents
    except (OSError, sqlite3.Error) as error:
        if deadline is not None and time.monotonic() >= deadline:
            raise AuditRoutineBudget(
                "routine audit incomplete: indexed-row DISTINCT census "
                "exceeded the shared observation budget; "
                "run agrep audit --full") from error
        raise AuditEvidenceError(
            f"cannot inspect indexed-row evidence {path}: {error}") from error
    finally:
        if db is not None:
            if progress_installed:
                try:
                    db.set_progress_handler(None, 0)
                except sqlite3.Error:
                    pass
            db.close()


def _merge_entries(
        entries: list[dict], *, deadline: float | None = None,
        ) -> dict:
    """Sum per-conversation tallies of one sqlite store into a db-level view."""
    out = {"agent": entries[0].get("agent", ""), "key": "merged",
           "seen": 0, "rows": 0, "agent_rows": 0, "events": 0,
           "errors": 0, "skips": {}, "first_error": None}
    for e in entries:
        _deadline_check(deadline, "per-source tally merge")
        for k in ("seen", "rows", "agent_rows", "events", "errors"):
            out[k] += int(e.get(k, 0))
        for k, v in (e.get("skips") or {}).items():
            out["skips"][k] = out["skips"].get(k, 0) + int(v)
        if out["first_error"] is None:
            out["first_error"] = e.get("first_error")
    return out


def _entry_problem(key: object, entry: object) -> str:
    if not isinstance(key, str) or not isinstance(entry, dict):
        return f"evidence book has an invalid file entry: {key!r}"
    if not isinstance(entry.get("agent"), str) or entry["agent"] not in registry.ADAPTER_NAMES:
        return f"evidence book entry {key!r} has an invalid adapter"
    if not isinstance(entry.get("key"), str):
        return f"evidence book entry {key!r} has an invalid fingerprint"
    for field in ("seen", "rows", "agent_rows", "events", "errors"):
        value = entry.get(field)
        if type(value) is not int or value < 0:
            return f"evidence book entry {key!r} has an invalid {field} count"
    skips = entry.get("skips")
    if (not isinstance(skips, dict) or any(
            not isinstance(name, str) or type(value) is not int or value < 0
            for name, value in skips.items())):
        return f"evidence book entry {key!r} has invalid skip counts"
    return ""


def _token_identity(value: str) -> tuple[str, str] | None:
    if not value.startswith(_TOKEN_ID_PREFIX):
        return None
    try:
        payload = json.loads(value[len(_TOKEN_ID_PREFIX):])
    except (RecursionError, ValueError):
        return None
    if (not isinstance(payload, list) or len(payload) != 2
            or not all(isinstance(item, str) and item for item in payload)):
        return None
    return payload[0], payload[1]


def _legacy_token_alias(identity: tuple[str, str]) -> str | None:
    """Return the old token key only when its separator is unambiguous."""
    path, session = identity
    if "#" in path or "#" in session:
        return None
    return f"{path}#{session}"


def _store_snapshot_timeout(
        deadline: float | None, label: str) -> float:
    """Return this snapshot's cap without allowing routine calls past deadline."""
    if deadline is None:
        return _FULL_STORE_TIMEOUT_S
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AuditRoutineBudget(
            f"{label} unavailable: routine audit budget exhausted; "
            "routine audit incomplete/deferred; run agrep audit --full")
    return min(_ROUTINE_STORE_TIMEOUT_S, remaining)


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()
    ap = surface.ArgumentParser(
        prog="agrep audit",
        description="hold the ingest to its accounting: nothing seen goes missing",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  agrep audit                 bounded cached accounting check\n"
            "  agrep audit --agent codex   check one source adapter\n"
            "  agrep audit --full          recount every eligible source file\n"
            "\nexit: 0 clean, 1 evidence warning, 2 accounting error or invalid input."
        ))
    ap.add_argument("--agent", action="append", help="audit only this agent")
    ap.add_argument("--json", action="store_true", help="machine output")
    ap.add_argument("--strict", action="store_true",
                    help="promote coverage/drift warnings to errors (exit 2)")
    ap.add_argument(
        "--full", action="store_true",
        help="trustless raw-file recount (prints an estimate and ignores cache)")
    args = ap.parse_args(argv)
    if args.agent and len(args.agent) > 1:
        ap.error("--agent may be supplied only once")
    args.agent = args.agent[0] if args.agent else None
    # `--agent ""` would audit every agent and report it as one agent's audit
    blank_filter = surface.filter_value_error(args)
    if blank_filter:
        ap.error(blank_filter)
    if args.agent:
        selected = registry.normalize_agent_name(args.agent)
        if selected not in registry.ADAPTER_NAMES:
            ap.error("--agent must name one of: "
                     + ", ".join(registry.ADAPTER_NAMES))
        args.agent = selected
    if args.full:
        print(
            "full audit: trustless recount; discovering sources and evidence "
            "for the estimate",
            file=sys.stderr, flush=True)

    routine_deadline = (
        None if args.full else time.monotonic() + _ROUTINE_BUDGET_S)
    work_deadline = (
        None if routine_deadline is None else
        routine_deadline - _ROUTINE_FINAL_SNAPSHOT_RESERVE_S)
    problems = _DetailLog(_PROBLEM_SAMPLE_LIMIT)
    gaps = _DetailLog(_GAP_SAMPLE_LIMIT)
    routine_deferred_reason = ""
    snapshot_deferred_reasons: list[str] = []

    def defer_routine(reason: object) -> None:
        nonlocal routine_deferred_reason
        if not routine_deferred_reason:
            routine_deferred_reason = str(reason)

    def defer_snapshot(reason: object) -> None:
        rendered = str(reason)
        if rendered not in snapshot_deferred_reasons:
            snapshot_deferred_reasons.append(rendered)

    def work_available(label: str) -> bool:
        if routine_deferred_reason:
            return False
        try:
            _deadline_check(work_deadline, label)
            return True
        except AuditRoutineBudget as error:
            defer_routine(error)
            return False

    cache_path = _census_cache_path()
    # --full is a trustless recount and never even parses prior cached evidence.
    cache_deferred_reason = ""
    if args.full:
        census_cache = {}
        cached_indexed_record = None
    else:
        try:
            cached_files_table, cached_indexed_record = _read_audit_cache(
                cache_path, deadline=work_deadline)
            census_cache = dict(cached_files_table)
        except AuditRoutineBudget as error:
            # Cache is acceleration, not audit authority. Continue with raw
            # census work and disclose why the warm path was unavailable.
            census_cache = {}
            cached_indexed_record = None
            cache_deferred_reason = str(error)
    full_recount_cache: dict[str, dict] = {}
    cache_dirty = False
    cached_files = 0
    recounted_files = 0
    recounted_bytes = 0
    pending_files = 0
    pending_bytes = 0
    indexed_deferred_reason = ""
    indexed_cache_hit = False
    indexed_record_to_publish: dict | None = None

    try:
        book_identity_before = _optional_plain_identity(BOOK_PATH)
    except OSError as error:
        book_identity_before = None
        problems.append(f"cannot stat evidence book {BOOK_PATH}: {error}")
    try:
        book = _book(deadline=work_deadline)
    except AuditRoutineBudget as error:
        book = {}
        defer_routine(error)
    except AuditEvidenceError as error:
        book = {}
        problems.append(str(error))
    discovered_before: list[tuple[str, str]] = []
    live_tokens_before: dict[str, tuple[str, str]] = {}
    source_witnesses: dict[tuple[str, str], tuple[str, str]] = {}
    store_snapshot_before: dict | None = None
    selection = args.agent or "all"
    if work_available("combined store audit snapshot"):
        try:
            store_snapshot_before = _store_snapshot(
                selection=selection,
                timeout_s=_store_snapshot_timeout(
                    work_deadline, "combined store audit snapshot"))
            discovered_before = store_snapshot_before["discovered"]
            live_tokens_before = store_snapshot_before["tokens"]
            source_witnesses = store_snapshot_before["witnesses"]
        except AuditSnapshotDeferred as error:
            defer_snapshot(error)
        except AuditRoutineBudget as error:
            defer_routine(error)
        except AuditEvidenceError as error:
            problems.append(str(error))
    corpus_path = common.DATA_DIR / "corpus.db"
    try:
        indexed_identity_before = _sqlite_family_identity(corpus_path)
    except AuditEvidenceError as error:
        indexed_identity_before = ()
        problems.append(str(error))
    indexed_agents: set[str] = set()
    index_reader_is_real = (
        getattr(_indexed_agents, "__module__", None) == __name__)
    if (not args.full
            and bool(indexed_identity_before)
            and index_reader_is_real
            and _indexed_cache_matches(
                cached_indexed_record, indexed_identity_before)):
        indexed_agents = set(cached_indexed_record["agents"])
        indexed_cache_hit = True
        indexed_record_to_publish = cached_indexed_record
    elif work_available("indexed-row census"):
        try:
            global _INDEXED_DEADLINE_OVERRIDE
            prior_indexed_deadline = _INDEXED_DEADLINE_OVERRIDE
            indexed_deadline = (
                None if args.full else
                min(
                    work_deadline,
                    time.monotonic() + _ROUTINE_INDEXED_QUERY_BUDGET_S,
                )
            )
            _INDEXED_DEADLINE_OVERRIDE = indexed_deadline
            try:
                indexed_agents = _indexed_agents()
            finally:
                _INDEXED_DEADLINE_OVERRIDE = prior_indexed_deadline
        except AuditRoutineBudget as error:
            indexed_deferred_reason = str(error)
        except AuditEvidenceError as error:
            problems.append(str(error))
        else:
            if indexed_identity_before:
                indexed_record_to_publish = _indexed_cache_record(
                    indexed_identity_before, indexed_agents)
                cache_dirty = True
    discovered = discovered_before
    live_tokens = live_tokens_before
    if args.agent:
        discovered_before = [
            row for row in discovered_before if row[0] == args.agent
        ]
        discovered = [(a, p) for a, p in discovered if a == args.agent]
        book = {key: entry for key, entry in book.items()
                if isinstance(entry, dict) and entry.get("agent") == args.agent}
        indexed_agents &= {args.agent}
        live_tokens_before = {
            key: value for key, value in live_tokens_before.items()
            if value[0] == args.agent
        }
        live_tokens = {
            key: value for key, value in live_tokens.items()
            if value[0] == args.agent
        }
        source_witnesses = {
            row: witness for row, witness in source_witnesses.items()
            if row[0] == args.agent
        }

    # Use the Rust snapshot's opaque identity to find and recount cache misses
    # before cached rows. Complete accounting remains authoritative, and final
    # boundary instability still withholds the cache update.
    routine_inventory_paths: set[str] = set()
    routine_pending_census: dict[str, tuple[int, str, str]] = {}
    routine_witness_inventory_complete = False
    if not args.full and store_snapshot_before is not None:
        witness_inventory: dict[str, tuple[str, str]] = {}
        witness_inventory_valid = True
        for agent, path in discovered:
            if agent not in JSONL_CENSUS:
                continue
            source_witness = source_witnesses.get((agent, path))
            if source_witness is None:
                witness_inventory_valid = False
                break
            prior = witness_inventory.get(path)
            if prior is not None and prior != source_witness:
                witness_inventory_valid = False
                break
            witness_inventory[path] = source_witness

        if witness_inventory_valid:
            routine_witness_inventory_complete = True
            routine_inventory_paths = set(witness_inventory)
            for path, (
                    stat_key, identity_sha256) in witness_inventory.items():
                source_size = int(stat_key.rsplit(":", 1)[1])
                if _cache_count(
                        census_cache, path,
                        identity_sha256=identity_sha256) is not None:
                    cached_files += 1
                    continue
                routine_pending_census[path] = (
                    source_size, stat_key, identity_sha256)

    if (routine_witness_inventory_complete
            and routine_pending_census
            and work_available("priority changed-file census")):
        priority_candidates = [
            (source_size, path, stat_key, identity_sha256)
            for path, (
                source_size, stat_key,
                identity_sha256) in routine_pending_census.items()
        ]
        priority_candidates.sort(key=lambda row: (row[0], row[1]))

        for source_size, path, stat_key, identity_sha256 in priority_candidates:
            if not work_available("priority changed-file census"):
                break
            try:
                observed_key, source_identity = _stat_evidence(path)
            except AuditEvidenceError as error:
                problems.append(f"priority census: {error}")
                continue
            if observed_key != stat_key:
                # The final source snapshot owns the concurrent-change
                # decision.  Never cache evidence from the moved generation.
                continue
            try:
                n = _census_jsonl(path, deadline=work_deadline)
                _after_key, after_identity = _stat_evidence(path)
                _deadline_check(
                    work_deadline, "priority census final identity check")
            except AuditRoutineBudget as error:
                defer_routine(error)
                break
            except AuditEvidenceError as error:
                problems.append(f"priority census: {error}")
                continue
            if after_identity != source_identity:
                problems.append(
                    "source changed while priority census evidence was "
                    f"collected: {path}")
                continue
            record = {
                "identity": list(source_identity),
                "identity_sha256": identity_sha256,
                "nonblank_lines": int(n),
            }
            if census_cache.get(path) != record:
                census_cache[path] = record
                cache_dirty = True
            routine_pending_census.pop(path, None)
            recounted_files += 1
            recounted_bytes += source_size

    full_estimated_files = 0
    full_estimated_bytes = 0
    full_unknown_sizes = 0
    full_attempted_files = 0
    full_last_progress = time.monotonic()
    if args.full:
        for agent, path in discovered:
            if agent not in JSONL_CENSUS:
                continue
            full_estimated_files += 1
            try:
                full_estimated_bytes += int(_plain_entry(path).st_size)
            except OSError:
                full_unknown_sizes += 1
        unknown = (f"; {full_unknown_sizes} size(s) unavailable"
                   if full_unknown_sizes else "")
        print(
            "full audit: trustless recount; estimated up to "
            f"{surface.count_noun(full_estimated_files, 'file')}, "
            f"{_human_bytes(full_estimated_bytes)}{unknown}",
            file=sys.stderr, flush=True)

    instrumented = set(registry.ADAPTER_NAMES)
    if args.agent:
        instrumented &= {args.agent}
    per_agent: dict[str, dict] = {}
    verified_by_agent: dict[str, int] = {}
    fresh_by_agent: dict[str, int] = {}

    def agent_bucket(name: str) -> dict:
        return per_agent.setdefault(name, {
            "files": 0, "tallied": 0, "stale": 0, "untallied": 0,
            "seen": 0, "rows": 0, "agent_rows": 0, "errors": 0,
            "skips": {}, "identity_breaks": 0, "census_mismatch": 0,
            "error_samples": [],
        })

    # Token stores tally per conversation, then join discovery by source path.
    # A missing initial source boundary makes every downstream join
    # indeterminate; the named budget deferral is the only honest result.
    validation_complete = (
        store_snapshot_before is not None
        and work_available("token-census join")
    )
    live_by_path: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
    legacy_live_identities: dict[str, tuple[str, str]] = {}
    for key, value in (live_tokens.items() if validation_complete else ()):
        if not work_available("token-census join"):
            validation_complete = False
            break
        identity = _token_identity(key)
        if identity is None:
            problems.append(f"token census has an invalid identity: {key!r}")
            continue
        source = identity[0]
        rows = live_by_path.setdefault(source, {})
        if identity in rows:
            problems.append(f"token census has a duplicate identity for {source}")
            continue
        rows[identity] = value
        legacy_alias = _legacy_token_alias(identity)
        if legacy_alias is not None:
            legacy_live_identities[legacy_alias] = identity

    by_path: dict[str, list[tuple[object, dict]]] = {}
    book_token_entries: dict[tuple[str, str], dict] = {}
    for key, entry in (book.items() if validation_complete else ()):
        if not work_available("evidence-book join"):
            validation_complete = False
            break
        invalid = _entry_problem(key, entry)
        if invalid:
            problems.append(invalid)
            continue
        if entry["key"].startswith("s:"):
            identity = key
            source = key
        else:
            identity = _token_identity(key)
            if identity is None:
                # Transitional v18 books used ``path#session``. Resolve that
                # spelling only through the canonical live intake identity and
                # only when neither component contains the old separator.
                identity = legacy_live_identities.get(key)
            if identity is None:
                problems.append(f"evidence book has an invalid token identity: {key!r}")
                continue
            source = identity[0]
            if identity in book_token_entries:
                if book_token_entries[identity] != entry:
                    problems.append(
                        "evidence book has conflicting tallies for duplicate "
                        f"token identity in {source}")
                continue
            book_token_entries[identity] = entry
        by_path.setdefault(source, []).append((identity, entry))

    discovered_pairs = set(discovered) if validation_complete else set()
    for path, tokens in (live_by_path.items() if validation_complete else ()):
        if not work_available("token-source coverage join"):
            validation_complete = False
            break
        token_agents = {value[0] for value in tokens.values()}
        if len(token_agents) != 1:
            problems.append(f"token census has conflicting adapters for {path}")
            continue
        agent = next(iter(token_agents))
        if (agent, path) not in discovered_pairs:
            gaps.append(f"{agent}: token source omitted from discovery: {path}")

    seen_paths = set()
    eligible_source_candidates = sum(
        1 for found_agent, _found_path in discovered
        if found_agent in JSONL_CENSUS)
    eligible_candidates_started = 0
    active_eligible_candidate = False
    for agent, path in (discovered if validation_complete else ()):
        active_eligible_candidate = False
        if not work_available("source-accounting join"):
            validation_complete = False
            break
        if agent in JSONL_CENSUS:
            eligible_candidates_started += 1
            active_eligible_candidate = True
        b = agent_bucket(agent)
        b["files"] += 1
        seen_paths.add(path)
        keyed_entries = by_path.get(path) or []
        if not keyed_entries:
            b["untallied"] += 1
            if agent in instrumented:
                gaps.append(f"{agent}: never tallied: {path}")
            continue
        entries = []
        entry_agents: set[str] = set()
        token_entries = []
        stat_entries = []
        for keyed_identity, keyed_entry in keyed_entries:
            if not work_available("per-source tally join"):
                validation_complete = False
                break
            entries.append(keyed_entry)
            entry_agents.add(str(keyed_entry.get("agent") or ""))
            if keyed_entry["key"].startswith("s:"):
                stat_entries.append((keyed_identity, keyed_entry))
            else:
                token_entries.append((keyed_identity, keyed_entry))
        if not validation_complete:
            break
        b["tallied"] += 1
        if entry_agents != {agent}:
            problems.append(
                f"{agent}: tally adapter mismatch for {path}: {sorted(entry_agents)}")
            continue
        if token_entries and stat_entries:
            problems.append(f"{agent}: mixed stat and token tallies for {path}")
            continue
        try:
            entry = (
                entries[0] if len(entries) == 1 else
                _merge_entries(entries, deadline=work_deadline)
            )
        except AuditRoutineBudget as error:
            defer_routine(error)
            validation_complete = False
            break
        source_identity = None
        source_identity_sha256 = None
        source_size = 0
        if token_entries:
            expected = {}
            for key, item in token_entries:
                if not work_available("token freshness join"):
                    validation_complete = False
                    break
                expected[key] = (item["agent"], item["key"])
            if not validation_complete:
                break
            observed = live_by_path.get(path, {})
            fresh = expected == observed
            if fresh:
                verified_by_agent[agent] = verified_by_agent.get(agent, 0) + 1
        else:
            if len(stat_entries) != 1:
                problems.append(f"{agent}: duplicate stat tallies for {path}")
                continue
            source_witness = source_witnesses.get((agent, path))
            if source_witness is not None:
                stat_key, source_identity_sha256 = source_witness
                source_size = int(stat_key.rsplit(":", 1)[1])
            else:
                try:
                    stat_key, source_identity = _stat_evidence(path)
                except AuditEvidenceError as error:
                    problems.append(f"{agent}: {error}")
                    continue
                source_size = source_identity[2]
            if args.full and source_identity is None:
                try:
                    observed_key, source_identity = _stat_evidence(path)
                except AuditEvidenceError as error:
                    problems.append(f"{agent}: {error}")
                    continue
                if observed_key != stat_key:
                    problems.append(
                        f"{agent}: source changed after store snapshot: {path}")
                    continue
                source_size = source_identity[2]
            fresh = entry.get("key") == stat_key
        if not fresh:
            b["stale"] += 1
            detail = "token census mismatch" if token_entries else "source moved"
            gaps.append(f"{agent}: stale tally ({detail}): {path}")
        else:
            # Freshness and structural intake accounting remain useful evidence
            # even when a bounded routine census later remains pending.
            fresh_by_agent[agent] = fresh_by_agent.get(agent, 0) + 1

        seen = int(entry.get("seen", 0))
        rows = int(entry.get("rows", 0))
        agent_rows = int(entry.get("agent_rows", 0))
        errors = int(entry.get("errors", 0))
        skips = entry.get("skips", {}) or {}
        skip_total = sum(int(v) for v in skips.values())
        b["seen"] += seen
        b["rows"] += rows
        b["agent_rows"] += agent_rows
        b["errors"] += errors
        for k, v in skips.items():
            if not work_available("skip-accounting join"):
                validation_complete = False
                break
            b["skips"][k] = b["skips"].get(k, 0) + int(v)
        if not validation_complete:
            break

        if seen != rows + agent_rows + skip_total + errors:
            b["identity_breaks"] += 1
            problems.append(
                f"{agent}: IDENTITY BROKEN {path}: seen={seen} != "
                f"rows={rows}+agent={agent_rows}+skips={skip_total}+errors={errors}")
        if errors and entry.get("first_error"):
            if len(b["error_samples"]) < 3:
                b["error_samples"].append(f"{path}: {entry['first_error']}")
        # Census only when the tally is fresh. The cache stores a prior independent
        # raw read under the exact strong identity observed above; --full never uses it.
        if fresh and agent in JSONL_CENSUS:
            n = None if args.full else _cache_count(
                census_cache, path,
                identity=source_identity,
                identity_sha256=source_identity_sha256)
            if n is not None:
                if path not in routine_inventory_paths:
                    cached_files += 1
            else:
                if args.full:
                    full_attempted_files += 1
                if source_identity is None:
                    try:
                        observed_key, source_identity = _stat_evidence(path)
                    except AuditEvidenceError as error:
                        problems.append(f"{agent}: {error}")
                        continue
                    if observed_key != stat_key:
                        problems.append(
                            f"{agent}: source changed after store snapshot: {path}")
                        continue
                    source_size = source_identity[2]
                try:
                    n = _census_jsonl(path, deadline=work_deadline)
                    _after_key, after_identity = _stat_evidence(path)
                    _deadline_check(
                        work_deadline, "source census final identity check")
                except AuditRoutineBudget as error:
                    if not routine_witness_inventory_complete:
                        pending_files += 1
                        pending_bytes += source_size
                    defer_routine(error)
                    validation_complete = False
                    break
                except AuditEvidenceError as error:
                    problems.append(f"{agent}: {error}")
                    continue
                if after_identity != source_identity:
                    problems.append(
                        f"{agent}: source changed while census evidence was collected: "
                        f"{path}")
                    continue
                recounted_files += 1
                recounted_bytes += source_size
                record = {
                    "identity": list(source_identity),
                    "nonblank_lines": int(n),
                }
                if source_identity_sha256 is not None:
                    record["identity_sha256"] = source_identity_sha256
                if args.full:
                    full_recount_cache[path] = record
                elif census_cache.get(path) != record:
                    census_cache[path] = record
                    cache_dirty = True
                if routine_witness_inventory_complete:
                    routine_pending_census.pop(path, None)
                if args.full and time.monotonic() - full_last_progress >= 2.0:
                    print(
                        "full audit: progress "
                        f"{surface.count_noun(full_attempted_files, 'file')}, "
                        f"{_human_bytes(recounted_bytes)} recounted",
                        file=sys.stderr, flush=True)
                    full_last_progress = time.monotonic()
            verified_by_agent[agent] = verified_by_agent.get(agent, 0) + 1
            if n != seen:
                b["census_mismatch"] += 1
                problems.append(
                    f"{agent}: CENSUS MISMATCH {path}: raw non-blank lines={n}, "
                    f"parser saw={seen} - records exist the parser never iterated")
        elif fresh and not token_entries:
            verified_by_agent[agent] = verified_by_agent.get(agent, 0) + 1
        active_eligible_candidate = False

    if routine_witness_inventory_complete:
        pending_files = len(routine_pending_census)
        pending_bytes = sum(
            row[0] for row in routine_pending_census.values())
    elif not validation_complete:
        untouched_candidates = (
            eligible_source_candidates - eligible_candidates_started
            + int(active_eligible_candidate))
        pending_files = max(pending_files, max(0, untouched_candidates))

    orphan_count = 0
    book_agents: set[str] = set()
    for path, keyed_entries in (by_path.items() if validation_complete else ()):
        if not work_available("orphaned-tally join"):
            validation_complete = False
            break
        for _key, entry in keyed_entries:
            agent = str(entry.get("agent") or "")
            if agent:
                book_agents.add(agent)
        if path in seen_paths:
            continue
        state, detail = _path_state(path)
        if state == "missing":
            orphan_count += 1
        elif state == "unreadable":
            problems.append(f"cannot stat tallied source {path}: {detail}")
        else:
            agents = sorted({str(entry.get("agent") or "")
                             for _key, entry in keyed_entries})
            visible = [agent for agent in agents
                       if any(found == agent for found, _source in discovered)]
            if visible:
                gaps.append(
                    f"{','.join(visible)}: tallied source omitted from discovery: {path}")

    if validation_complete:
        discovered_agents = {agent for agent, _path in discovered}
        evidence_agents = set(book_agents) | indexed_agents | {
            value[0] for value in live_tokens.values()
        }
        for agent in sorted((evidence_agents & instrumented) - discovered_agents):
            if not work_available("adapter-coverage join"):
                validation_complete = False
                break
            gaps.append(
                f"{agent}: zero source files discovered despite existing "
                f"{'tallies' if agent in book_agents else 'indexed rows'}")

    if validation_complete:
        for agent in sorted(book_agents & instrumented):
            if not work_available("fresh-evidence join"):
                validation_complete = False
                break
            if fresh_by_agent.get(agent, 0) == 0:
                problems.append(
                    f"{agent}: no tallied source file had fresh readable evidence")

    if (validation_complete and not verified_by_agent
            and not problems and not gaps
            and not snapshot_deferred_reasons
            and not indexed_deferred_reason):
        problems.append(_NO_CENSUS_PROBLEM)

    if args.full:
        print(
            "full audit: recount pass read "
            f"{surface.count_noun(recounted_files, 'file')}, "
            f"{_human_bytes(recounted_bytes)}; "
            "validating the independent evidence snapshots",
            file=sys.stderr, flush=True)

    store_boundary_stable = False
    if store_snapshot_before is not None:
        try:
            store_snapshot_after = _store_snapshot(
                selection=selection,
                timeout_s=_store_snapshot_timeout(
                    routine_deadline, "final combined store audit snapshot"))
            store_boundary_stable = (
                store_snapshot_before["boundary_sha256"]
                == store_snapshot_after["boundary_sha256"])
            if not store_boundary_stable:
                problems.append("store source evidence changed during audit")
        except AuditSnapshotDeferred as error:
            defer_snapshot(error)
        except AuditRoutineBudget as error:
            defer_routine(error)
        except AuditEvidenceError as error:
            problems.append(str(error))
    try:
        indexed_identity_after = _sqlite_family_identity(corpus_path)
    except AuditEvidenceError as error:
        indexed_identity_after = ()
        problems.append(str(error))
    try:
        book_identity_after = _optional_plain_identity(BOOK_PATH)
    except OSError as error:
        book_identity_after = None
        problems.append(f"cannot stat evidence book {BOOK_PATH}: {error}")
    if indexed_identity_before != indexed_identity_after:
        indexed_record_to_publish = None
        cache_dirty = True
        problems.append("indexed-row evidence changed during audit")
    if book_identity_before != book_identity_after:
        problems.append("evidence book changed during audit")

    strict_gap_count = len(gaps)
    incomplete_reasons = [
        reason for reason in (
            routine_deferred_reason, indexed_deferred_reason,
            *snapshot_deferred_reasons)
        if reason
    ]
    if incomplete_reasons:
        gaps.append(
            "routine audit incomplete/deferred within the routine budget: "
            f"{'; '.join(incomplete_reasons)}; "
            "no clean result was certified; "
            "run agrep audit --full")

    total_errors = sum(b["errors"] for b in per_agent.values())
    if problems or total_errors or (args.strict and strict_gap_count):
        exit_code = 2
        status = "error"
    elif gaps or incomplete_reasons:
        exit_code = 1
        status = "warning"
    else:
        exit_code = 0
        status = "clean"

    cache_state = "unchanged"
    if cache_path is None:
        cache_state = "unavailable"
    elif args.full:
        # Each record is an independent raw read bracketed by exact identity.
        # A full run publishes only completed records, so unrelated failures
        # cannot poison reusable evidence or accumulate stale entries.
        census_cache = full_recount_cache
        cache_dirty = bool(
            full_recount_cache or indexed_record_to_publish)
    if not store_boundary_stable:
        cache_dirty = False
        if cache_path is not None and (args.full or recounted_files):
            cache_state = "update withheld: store boundary was not stable"
    if cache_path is not None and cache_dirty:
        if _write_census_cache(
                cache_path, census_cache,
                indexed_agents=indexed_record_to_publish):
            cache_state = (
                "updated with completed identity-bound evidence"
                if problems or total_errors or gaps else
                "updated")
        else:
            cache_state = "update unavailable"

    if args.full:
        census_disclosure = (
            "trustless recount read "
            f"{surface.count_noun(recounted_files, 'file')}; cache was ignored")
    else:
        if pending_files:
            pending_disclosure = (
                f"{'at least ' if routine_deferred_reason else ''}"
                f"{surface.count_noun(pending_files, 'file')} remained unverified "
                f"({_human_bytes(pending_bytes)} of source bytes known) "
                "when the routine budget expired; ")
        elif routine_deferred_reason or snapshot_deferred_reasons:
            pending_disclosure = (
                "changed-file pending total was not established before the "
                "routine cutoff; ")
        else:
            pending_disclosure = "no changed file remained pending; "
        census_disclosure = (
            f"{surface.count_noun(cached_files, 'file')} verified from cache; "
            "routine recounted "
            f"{surface.count_noun(recounted_files, 'uncached or changed file')} "
            f"({_human_bytes(recounted_bytes)}); "
            f"{pending_disclosure}"
            "--full ignores cache and recounts every eligible file")
        if cache_deferred_reason:
            census_disclosure += (
                f"; census-cache acceleration deferred: {cache_deferred_reason}")
        if incomplete_reasons:
            census_disclosure += (
                f"; routine validation incomplete/deferred: "
                f"{'; '.join(incomplete_reasons)}")
    if cache_state in ("unavailable", "update unavailable"):
        census_disclosure += f"; census cache {cache_state}"
    census_summary = {
        "full": bool(args.full),
        "cached_files": cached_files,
        "recounted_files": recounted_files,
        "recounted_bytes": recounted_bytes,
        "pending_files": pending_files,
        "pending_bytes": pending_bytes,
        "estimated_files": full_estimated_files if args.full else None,
        "estimated_bytes": full_estimated_bytes if args.full else None,
        "cache": cache_state,
        "disclosure": census_disclosure,
    }

    if args.full:
        print(
            f"full audit: evidence validation finished with status {status}",
            file=sys.stderr, flush=True)

    if args.json:
        print(json.dumps({
            "status": status, "exit_code": exit_code,
            "agents": per_agent,
            "problems": problems.items,
            "problem_count": len(problems),
            "problems_omitted": problems.omitted,
            "gaps": len(gaps),
            "gap_details": gaps.items,
            "gap_details_omitted": gaps.omitted,
            "orphaned_tallies": orphan_count,
            "routine": {
                "complete": not bool(incomplete_reasons),
                "deferred_reason": (
                    "; ".join(incomplete_reasons)
                    if incomplete_reasons else None),
                "indexed_agents_from_cache": indexed_cache_hit,
            },
            "census": census_summary,
        }, ensure_ascii=False))
    elif (not per_agent and not gaps and not orphan_count
          and total_errors == 0 and problems.items == [_NO_CENSUS_PROBLEM]):
        scope = (f"no {common.terminal_safe(args.agent)} source files"
                 if args.agent else "no supported source files")
        print(f"audit unavailable: {scope} were found; "
              "no completeness claim was made.")
    else:
        print(f"\n{'agent':<12} {'files':>6} {'tallied':>8} {'stale':>6} "
              f"{'seen':>9} {'rows':>8} {'agent':>8} {'errors':>7}")
        print("─" * 70)
        for agent in sorted(per_agent):
            b = per_agent[agent]
            mark = "" if agent in instrumented else "  (not instrumented yet)"
            safe_agent = common.terminal_safe(agent)
            print(f"{safe_agent:<12} {b['files']:>6} {b['tallied']:>8} {b['stale']:>6} "
                  f"{b['seen']:>9,} {b['rows']:>8,} {b['agent_rows']:>8,} "
                  f"{b['errors']:>7}{mark}")
            if b["skips"]:
                kinds = " · ".join(f"{k} {v:,}" for k, v in sorted(b["skips"].items()))
                print(f"{'':<12} skips: {kinds}")
            for s in b["error_samples"]:
                print(f"{'':<12} ! {common.terminal_safe(s)}")
        print(f"\ncounted: {census_disclosure}")
        if problems:
            print(f"\n{len(problems)} accounting/evidence error(s):")
            for p in problems[:20]:
                print(f"  ✗ {common.terminal_safe(p)}")
            if len(problems) > 20:
                print(f"  … {len(problems) - 20} additional error(s) omitted")
        if gaps:
            if exit_code == 2 and (problems or total_errors):
                gap_severity = " - warnings in addition to the errors above"
            elif args.strict and strict_gap_count:
                gap_severity = " - errors because --strict was requested"
            else:
                gap_severity = " - warnings"
            print(f"\n{len(gaps)} audit evidence gap(s) "
                  f"({surface.REMEDIES['audit-gap'].text})"
                  + gap_severity)
            for g in gaps[:10]:
                print(f"  - {common.terminal_safe(g)}")
            if len(gaps) > 10:
                print(f"  … {len(gaps) - 10} additional gap(s) omitted")
        if orphan_count:
            print(
                f"\n{surface.count_noun(orphan_count, 'tallied file')} "
                "no longer on disk "
                "(rotated/deleted)")
        if status == "clean" and verified_by_agent:
            print("\naudit clean: every seen record is accounted for.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
