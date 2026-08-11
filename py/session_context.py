"""Session-family publication, addressing, and calling-agent identity."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat as statmod
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Sequence

from dist import _is_dev_checkout
from events import DATA_DIR, open_sqlite_snapshot
import fileops
from hookless.registry import (
    ADAPTER_INPUT_NAMES,
    AGENT_CONTEXT_ENV_KEYS,
    normalize_agent_name,
)
import ownerfile


_FAMILY_META_MAX_BYTES = 1024 * 1024
_INGEST_SIGNATURE_MAX_BYTES = 4096
_DERIVED_PROOF_MAX_BYTES = 1024 * 1024
# A sessions.jsonl row is compact summary metadata, never transcript payload.
# One MiB leaves ample headroom while bounding a corrupt/no-newline read.
SESSION_JSONL_ROW_MAX_BYTES = 1024 * 1024
_LEGACY_DERIVED_PROOF_VERSION = 4
_LEGACY_DERIVED_PROOF_ORDER = (
    "messages.jsonl",
    "replies.jsonl",
    "sessions.jsonl",
    "boundary_stats.json",
    ".boundary_stats.bin",
    "event_stats.json",
)
_LEGACY_DERIVED_PROOF_NAMES = frozenset(_LEGACY_DERIVED_PROOF_ORDER)


class LegacyPublication(RuntimeError):
    """A stable pre-family ingest generation that needs one explicit re-index."""


class TranscriptPublicationRace(RuntimeError):
    """A coherent transcript publication is moving between commit markers."""


def _legacy_edge_hash(
        path: Path, identity: fileops.FileIdentity, *,
        include_content_hash: bool,
) -> tuple[int, bytes | None]:
    _device, _inode, size, _modified, _changed = identity
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        if fileops.file_identity_fd(fd) != identity:
            raise OSError(f"legacy derived artifact changed before reading: {path}")
        edge = 512
        head = os.read(fd, min(size, edge))
        if len(head) != min(size, edge):
            raise OSError(f"legacy derived artifact was truncated: {path}")
        tail = b""
        if size > edge:
            tail_len = min(size, edge)
            os.lseek(fd, -tail_len, os.SEEK_END)
            tail = os.read(fd, tail_len)
            if len(tail) != tail_len:
                raise OSError(f"legacy derived artifact was truncated: {path}")
        body = size.to_bytes(8, "little") + head + tail
        value = 0xCBF29CE484222325
        for byte in body:
            value ^= byte
            value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF

        content_digest = None
        if include_content_hash:
            os.lseek(fd, 0, os.SEEK_SET)
            import hashlib

            content = hashlib.sha256()
            for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                content.update(chunk)
            content_digest = content.digest()
        if (fileops.file_identity_fd(fd) != identity
                or fileops.file_identity(path) != identity):
            raise OSError(f"legacy derived artifact changed while reading: {path}")
        return value, content_digest
    finally:
        os.close(fd)


def _legacy_unix_change_token(ctime_ns: int) -> int:
    seconds, nanos = divmod(int(ctime_ns), 1_000_000_000)
    value = seconds & 0xFFFFFFFFFFFFFFFF
    rotated = ((value << 17) | (value >> 47)) & 0xFFFFFFFFFFFFFFFF
    return rotated ^ nanos


def _legacy_file_proof(root: Path, row: object) -> bool:
    if (not isinstance(row, dict)
            or set(row) != {
                "name", "len", "modified_ns", "change_token", "edge_hash"}):
        return False
    name = row.get("name")
    if not isinstance(name, str) or name not in _LEGACY_DERIVED_PROOF_NAMES:
        return False
    for field in ("len", "modified_ns", "edge_hash"):
        value = row.get(field)
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
            return False
    token = row.get("change_token")
    if not isinstance(token, dict) or len(token) != 1:
        return False
    metadata_token = token.get("Metadata")
    content_token = token.get("ContentSha256")
    if "Metadata" in token:
        if (type(metadata_token) is not int
                or not 0 <= metadata_token <= 0xFFFFFFFFFFFFFFFF):
            return False
    elif "ContentSha256" in token:
        if (not isinstance(content_token, list)
                or len(content_token) != 32
                or any(type(byte) is not int or not 0 <= byte <= 255
                       for byte in content_token)):
            return False
    else:
        return False

    path = root / name
    try:
        identity = fileops.file_identity(path)
        _device, _inode, size, modified_ns, changed_ns = identity
        needs_content_hash = os.name == "nt" and "ContentSha256" in token
        edge_hash, content_sha256 = _legacy_edge_hash(
            path, identity, include_content_hash=needs_content_hash)
        if (row["len"] != size
                or row["modified_ns"] != modified_ns
                or row["edge_hash"] != edge_hash):
            return False
        if os.name == "posix":
            return token == {
                "Metadata": _legacy_unix_change_token(changed_ns)}
        if os.name == "nt":
            if "Metadata" in token:
                matched = metadata_token == fileops.windows_rust_change_token(
                    path, expected_identity=identity)
            else:
                matched = (
                    content_sha256 is not None
                    and content_token == list(content_sha256)
                )
            return matched and fileops.file_identity(path) == identity
        return token == {"Metadata": 0}
    except (OSError, TypeError, ValueError):
        return False


def _legacy_publication_proof(
        root: Path,
        proof: ownerfile.Snapshot | None,
        signature: ownerfile.Snapshot | None,
) -> bool:
    """Require positive, stable v0.2 proof rather than inferring legacy from a missing file."""
    if proof is None or signature is None:
        return False

    def unique_object(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError(f"duplicate JSON key: {key}")
            record[key] = value
        return record

    try:
        record = json.loads(
            proof.raw.decode("utf-8"),
            object_pairs_hook=unique_object)
        committed = signature.raw.decode("utf-8").strip()
    except (RecursionError, UnicodeError, ValueError, TypeError):
        return False
    if not isinstance(record, dict) or set(record) != {
            "version", "signature", "files"}:
        return False
    files = record.get("files")
    if (record.get("version") != _LEGACY_DERIVED_PROOF_VERSION
            or not committed
            or record.get("signature") != committed
            or not isinstance(files, list)
            or len(files) != len(_LEGACY_DERIVED_PROOF_NAMES)):
        return False
    names = [row.get("name") for row in files if isinstance(row, dict)]
    return (
        len(names) == len(files)
        and all(isinstance(name, str) for name in names)
        and tuple(names) == _LEGACY_DERIVED_PROOF_ORDER
        and all(_legacy_file_proof(root, row) for row in files)
    )


def transcript_generation(data_dir: Path | None = None, attempts: int = 4) -> dict | None:
    """Stable, content-aware identity of the searchable message publication.

    The Rust ingest signature fingerprints every normalized message field and
    reply. File identities additionally catch direct replacement of the derived
    JSONL files. Signature mtime is intentionally excluded: an unchanged ingest
    touches it to record a freshness check without changing semantic content.
    """
    import hashlib

    root = data_dir or DATA_DIR
    messages = root / "messages.jsonl"
    replies = root / "replies.jsonl"
    sessions = root / "sessions.jsonl"
    family_meta = root / SESSION_FAMILY_META_FILE
    signature = root / ".ingest.sig"
    derived_proof = root / ".derived_generation.json"
    def file_identity(path: Path) -> dict[str, int]:
        stat = path.lstat()
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not statmod.S_ISREG(stat.st_mode)
                or bool(getattr(stat, "st_file_attributes", 0) & reparse)):
            raise OSError(f"transcript artifact is not a plain regular file: {path}")
        return {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "dev": int(stat.st_dev),
            "ino": int(stat.st_ino),
        }

    def optional_identity(path: Path) -> dict[str, int] | None:
        try:
            return file_identity(path)
        except FileNotFoundError:
            return None

    def optional_snapshot(path: Path, max_bytes: int):
        try:
            return ownerfile.snapshot(path, max_bytes=max_bytes)
        except FileNotFoundError:
            return None

    tries = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(tries):
        try:
            identities_before = (
                optional_identity(messages), optional_identity(replies),
                optional_identity(sessions), optional_identity(family_meta),
                optional_identity(signature),
            )
            if identities_before[0] is None:
                return None
            sig_before = optional_snapshot(
                signature, _INGEST_SIGNATURE_MAX_BYTES)
            family_before = optional_snapshot(
                family_meta, _FAMILY_META_MAX_BYTES)
            need_legacy_proof = (
                identities_before[2] is not None
                and identities_before[3] is None
                and identities_before[4] is not None
            )
            proof_before = (
                optional_snapshot(derived_proof, _DERIVED_PROOF_MAX_BYTES)
                if need_legacy_proof else None
            )
            paths = [messages] + ([replies] if identities_before[1] is not None else [])
            before = {path.name: file_identity(path) for path in paths}
            identities_after = (
                optional_identity(messages), optional_identity(replies),
                optional_identity(sessions), optional_identity(family_meta),
                optional_identity(signature),
            )
            sig_after = optional_snapshot(
                signature, _INGEST_SIGNATURE_MAX_BYTES)
            family_after = optional_snapshot(
                family_meta, _FAMILY_META_MAX_BYTES)
            proof_after = (
                optional_snapshot(derived_proof, _DERIVED_PROOF_MAX_BYTES)
                if need_legacy_proof else None
            )
            after = {path.name: file_identity(path) for path in paths}
            if (identities_before != identities_after
                    or before != after
                    or ((sig_before is None) != (sig_after is None))
                    or ((family_before is None) != (family_after is None))
                    or ((proof_before is None) != (proof_after is None))
                    or (sig_before is not None and sig_after is not None
                        and (sig_before.identity != sig_after.identity
                             or sig_before.raw != sig_after.raw))
                    or (family_before is not None and family_after is not None
                        and (family_before.identity != family_after.identity
                             or family_before.raw != family_after.raw))
                    or (proof_before is not None and proof_after is not None
                        and (proof_before.identity != proof_after.identity
                             or proof_before.raw != proof_after.raw))):
                raise TranscriptPublicationRace(
                    "transcript publication changed during generation read")
            family_proof = (
                decode_session_family_proof(family_after.raw, sig_after.raw)
                if family_after is not None and sig_after is not None else None)
            if (identities_after[2] is not None
                    and identities_after[3] is None
                    and identities_after[4] is not None
                    and _legacy_publication_proof(root, proof_after, sig_after)
                    and _legacy_publication_proof(root, proof_after, sig_after)):
                raise LegacyPublication(
                    "session-family proof is absent from a legacy ingest publication")
            if (family_after is not None and family_proof is None
                    and _family_meta_precedes_signature(
                        family_after.raw,
                        sig_after.raw if sig_after is not None else None)):
                raise TranscriptPublicationRace(
                    "session-family publication precedes its ingest signature")
            if (identities_after[3] is not None and family_proof is None) or (
                    identities_after[2] is not None and identities_after[3] is None):
                raise RuntimeError("session-family publication is not generation-bound")
            return {
                "version": 4,
                "files": after,
                "family": (
                    family_proof.stamp
                    if family_proof is not None
                    else SESSION_FAMILY_MISSING_STAMP
                ),
                "ingest_signature": (
                    hashlib.sha256(sig_after.raw).hexdigest()
                    if sig_after is not None else None),
            }
        except LegacyPublication as exc:
            raise LegacyPublication(
                f"could not read stable transcript generation: {exc}") from exc
        except TranscriptPublicationRace as exc:
            last_error = exc
            if attempt + 1 < tries:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise TranscriptPublicationRace(
                f"could not read stable transcript generation: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < tries:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise RuntimeError(f"could not read stable transcript generation: {exc}") from exc
    assert last_error is not None
    raise last_error


# v2 reuses MD5+FNV64 to avoid another dependency; it detects torn local publications.
# The ingest signature commits generations; full readers verify rows before reconciliation.
SESSION_FAMILY_INDEX_VERSION = 2
SESSION_FAMILY_DERIVATION_VERSION = 1
SESSION_FAMILY_DIGEST_ALGORITHM = "md5-fnv64-v1"
SESSION_FAMILY_META_FILE = "session_family.meta.json"
SESSION_FAMILY_MISSING_STAMP = json.dumps(
    [SESSION_FAMILY_DERIVATION_VERSION, None, None], separators=(",", ":"))
SESSION_FAMILY_MAX_MEMBERS = 4_096
SESSION_PREFIX_MAX_CANDIDATES = 4_096


class SessionFamilyProof(NamedTuple):
    ingest_signature: str
    count: int
    digest: str
    stamp: str


class SessionFamilyCensus(NamedTuple):
    proof: SessionFamilyProof
    sessions: frozenset[str]
    parents: Mapping[str, str]


_SESSION_FAMILY_CENSUS = {"key": None, "value": None}


def decode_session_family_proof(
        raw: bytes | None, ingest_signature: bytes | str | None,
) -> SessionFamilyProof | None:
    """Validate one Rust-authored logical family proof."""
    if raw is None or ingest_signature is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        expected = (
            ingest_signature.decode("utf-8")
            if isinstance(ingest_signature, bytes) else ingest_signature
        ).strip()
    except (RecursionError, UnicodeError, ValueError, TypeError):
        return None
    if (not isinstance(record, dict)
            or set(record) != {
                "version", "algorithm", "ingest_signature", "count", "digest"}
            or record.get("version") != SESSION_FAMILY_INDEX_VERSION
            or record.get("algorithm") != SESSION_FAMILY_DIGEST_ALGORITHM
            or record.get("ingest_signature") != expected
            or type(record.get("count")) is not int
            or record["count"] < 0):
        return None
    digest = record.get("digest")
    if (not isinstance(digest, str) or len(digest) != 48
            or any(char not in "0123456789abcdef" for char in digest)):
        return None
    stamp = json.dumps(
        [SESSION_FAMILY_DERIVATION_VERSION, record["count"], digest],
        separators=(",", ":"),
    )
    return SessionFamilyProof(expected, record["count"], digest, stamp)


def _family_meta_precedes_signature(
        raw: bytes, ingest_signature: bytes | None,
) -> bool:
    try:
        record = json.loads(raw.decode("utf-8"))
        candidate = record.get("ingest_signature") if isinstance(record, dict) else None
        committed = (
            ingest_signature.decode("utf-8").strip()
            if ingest_signature is not None else None)
    except (RecursionError, UnicodeError, ValueError, TypeError):
        return False
    return (
        isinstance(candidate, str)
        and decode_session_family_proof(raw, candidate) is not None
        and candidate.strip() != committed
    )


def read_session_family_proof(
        data_dir: Path | None = None, attempts: int = 3,
) -> SessionFamilyProof | None:
    """Read the logical commit marker without opening the session census."""
    root = data_dir or DATA_DIR
    meta_path = root / SESSION_FAMILY_META_FILE
    signature_path = root / ".ingest.sig"
    for attempt in range(max(1, int(attempts))):
        if attempt:
            # only reached mid-republish; a breath outlives most torn windows
            time.sleep(0.005 * attempt)
        try:
            meta_before = ownerfile.snapshot(
                meta_path, max_bytes=_FAMILY_META_MAX_BYTES)
            signature_before = ownerfile.snapshot(
                signature_path, max_bytes=_INGEST_SIGNATURE_MAX_BYTES)
            meta_after = ownerfile.snapshot(
                meta_path, max_bytes=_FAMILY_META_MAX_BYTES)
            signature_after = ownerfile.snapshot(
                signature_path, max_bytes=_INGEST_SIGNATURE_MAX_BYTES)
        except OSError:
            continue
        if (meta_before.identity == meta_after.identity
                and meta_before.raw == meta_after.raw
                and signature_before.identity == signature_after.identity
                and signature_before.raw == signature_after.raw):
            return decode_session_family_proof(meta_after.raw, signature_after.raw)
    return None


class _SessionFamilyDigest:
    __slots__ = ("md5", "fnv")

    def __init__(self) -> None:
        import hashlib

        self.md5 = hashlib.md5(usedforsecurity=False)
        self.fnv = 0xCBF29CE484222325
        self._update_fnv(1)
        self._update(b"agrep-session-family-v1\0")

    def _update_fnv(self, byte: int) -> None:
        self.fnv ^= byte
        self.fnv = (self.fnv * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF

    def _update(self, chunk: bytes) -> None:
        self.md5.update(chunk)
        for byte in chunk:
            self._update_fnv(byte)

    def add(self, session: str, parent: str) -> None:
        for value in (session, parent):
            encoded = value.encode("utf-8")
            self._update(len(encoded).to_bytes(8, "little"))
            self._update(encoded)

    def hexdigest(self) -> str:
        return f"{self.md5.hexdigest()}{self.fnv:016x}"


def session_family_digest(rows: Iterable[tuple[str, str]]) -> str:
    """Hash session-parent rows supplied in canonical session order."""
    digest = _SessionFamilyDigest()
    for session, parent in rows:
        digest.add(session, parent)
    return digest.hexdigest()


def _session_family_identity(
        identity: fileops.FileIdentity) -> tuple[int, int, int, int, int]:
    device, inode, size, modified, changed = identity
    return size, modified, changed, device, inode


def _session_family_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        return _session_family_identity(fileops.file_identity(path))
    except (FileNotFoundError, PermissionError):
        raise
    except OSError as error:
        raise OSError(
            f"session-family source is not a plain regular file: {path}") from error


@contextmanager
def _open_session_family_rows(path: Path):
    before = _session_family_file_identity(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    stream = None
    try:
        opened = os.fstat(fd)
        opened_identity = _session_family_identity(fileops.file_identity_fd(fd))
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not statmod.S_ISREG(opened.st_mode)
                or bool(getattr(opened, "st_file_attributes", 0) & reparse)
                or opened_identity != before):
            raise OSError(f"session-family source changed before reading: {path}")
        stream = os.fdopen(fd, "rb")
        fd = -1
        yield stream, before
        after_identity = _session_family_identity(
            fileops.file_identity_fd(stream.fileno()))
        if (after_identity != before
                or _session_family_file_identity(path) != before):
            raise OSError(f"session-family source changed while reading: {path}")
    finally:
        if stream is not None:
            stream.close()
        elif fd >= 0:
            os.close(fd)


def read_session_family_census(
        data_dir: Path | None = None, attempts: int = 3,
        *, deadline: float | None = None,
) -> SessionFamilyCensus | None:
    """Validate the compact family rows against their generation-bound proof."""
    def budget_check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("session-family census budget exceeded")

    root = data_dir or DATA_DIR
    sessions_path = root / "sessions.jsonl"
    meta_path = root / SESSION_FAMILY_META_FILE
    signature_path = root / ".ingest.sig"
    cache_path = str(sessions_path)
    for attempt in range(max(1, int(attempts))):
        budget_check()
        if attempt:
            # only reached mid-republish; a breath outlives most torn windows
            delay = 0.005 * attempt
            if (deadline is not None
                    and time.monotonic() + delay >= deadline):
                raise TimeoutError("session-family census budget exceeded")
            time.sleep(delay)
            budget_check()
        try:
            before = _session_family_file_identity(sessions_path)
            budget_check()
            meta_snapshot = ownerfile.snapshot(
                meta_path, max_bytes=_FAMILY_META_MAX_BYTES)
            budget_check()
            signature_snapshot = ownerfile.snapshot(
                signature_path, max_bytes=_INGEST_SIGNATURE_MAX_BYTES)
            budget_check()
            meta_before = meta_snapshot.raw
            signature_before = signature_snapshot.raw
            key = (
                cache_path, SESSION_FAMILY_DERIVATION_VERSION,
                before, meta_before, signature_before,
            )
            cached = _SESSION_FAMILY_CENSUS.get("value")
            if _SESSION_FAMILY_CENSUS.get("key") == key and cached is not None:
                after = _session_family_file_identity(sessions_path)
                budget_check()
                meta_after = ownerfile.snapshot(
                    meta_path, max_bytes=_FAMILY_META_MAX_BYTES)
                budget_check()
                signature_after = ownerfile.snapshot(
                    signature_path, max_bytes=_INGEST_SIGNATURE_MAX_BYTES)
                budget_check()
                if (before == after
                        and meta_snapshot.identity == meta_after.identity
                        and meta_before == meta_after.raw
                        and signature_snapshot.identity == signature_after.identity
                        and signature_before == signature_after.raw):
                    return cached
                continue

            parents: dict[str, str] = {}
            seen: set[str] = set()
            previous_session: str | None = None
            digest = _SessionFamilyDigest()
            with _open_session_family_rows(sessions_path) as (stream, opened_identity):
                if opened_identity != before:
                    continue
                while True:
                    budget_check()
                    wire = stream.readline(SESSION_JSONL_ROW_MAX_BYTES + 1)
                    budget_check()
                    if not wire:
                        break
                    if (len(wire) > SESSION_JSONL_ROW_MAX_BYTES
                            or not wire.endswith(b"\n")):
                        return None
                    line = wire.decode("utf-8")
                    if not line.strip():
                        continue
                    budget_check()
                    row = json.loads(line)
                    budget_check()
                    if not isinstance(row, dict):
                        return None
                    session = row.get("session")
                    parent = row.get("parent", "")
                    if (not isinstance(session, str) or not session
                            or "\n" in session or "\r" in session
                            or not isinstance(parent, str)
                            or "\n" in parent or "\r" in parent
                            or (previous_session is not None
                                and session <= previous_session)):
                        return None
                    seen.add(session)
                    if parent:
                        parents[session] = parent
                    digest.add(session, parent)
                    previous_session = session

            after = _session_family_file_identity(sessions_path)
            budget_check()
            meta_after_snapshot = ownerfile.snapshot(
                meta_path, max_bytes=_FAMILY_META_MAX_BYTES)
            budget_check()
            signature_after_snapshot = ownerfile.snapshot(
                signature_path, max_bytes=_INGEST_SIGNATURE_MAX_BYTES)
            budget_check()
            meta_after = meta_after_snapshot.raw
            signature_after = signature_after_snapshot.raw
            if (before != after or meta_before != meta_after
                    or signature_before != signature_after
                    or meta_snapshot.identity != meta_after_snapshot.identity
                    or signature_snapshot.identity != signature_after_snapshot.identity):
                continue
            proof = decode_session_family_proof(meta_after, signature_after)
            if (proof is None or proof.count != len(seen)
                    or proof.digest != digest.hexdigest()):
                return None
            from types import MappingProxyType
            census = SessionFamilyCensus(
                proof, frozenset(seen), MappingProxyType(parents))
            _SESSION_FAMILY_CENSUS.update(key=key, value=census)
            return census
        except TimeoutError:
            raise
        except OSError:
            continue
        except (RecursionError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None
    return None


def session_family_source_stamp(data_dir: Path | None = None) -> str | None:
    """Logical identity committed by the family meta and ingest signature."""
    root = data_dir or DATA_DIR
    if not (root / "sessions.jsonl").exists() and not (
            root / SESSION_FAMILY_META_FILE).exists():
        return SESSION_FAMILY_MISSING_STAMP
    proof = read_session_family_proof(root)
    if proof is not None:
        return proof.stamp
    return None


def strict_family_parent_map(
        data_dir: Path | None = None,
) -> Mapping[str, str] | None:
    """Return verified parents, empty for no publication, or None for invalid."""
    root = data_dir or DATA_DIR
    census = read_session_family_census(root)
    if census is not None:
        return census.parents
    if not (root / "sessions.jsonl").exists() and not (
            root / SESSION_FAMILY_META_FILE).exists():
        return {}
    return None


FAMILY_PUBLICATION_WAIT_S = max(
    0.0, float(os.environ.get("AGREP_FAMILY_PUBLICATION_WAIT_S", "10")))


def await_family_publication(
        read, *, timeout_s: float | None = None,
        data_dir: Path | None = None,
):
    """Bounded re-read of a torn family publication, for background writers only.

    A concurrent ingest republishes sessions.jsonl, the family meta, and the
    ingest signature in sequence; a reader landing inside that window sees None
    even though a valid publication is seconds away. Embed-side callers that
    would otherwise crash (arming the bootstrap backoff) wait here instead.
    Hot query paths keep their fail-open single read - never route them here.
    A valid family marker ahead of the ingest signature is reported as a
    publication race. Other invalid states still return None after the deadline.
    """
    deadline = time.monotonic() + (
        FAMILY_PUBLICATION_WAIT_S if timeout_s is None else max(0.0, timeout_s))
    delay = 0.02
    result = read()
    while result is None and time.monotonic() < deadline:
        time.sleep(min(delay, max(0.001, deadline - time.monotonic())))
        delay = min(delay * 2, 0.25)
        result = read()
    if result is None:
        try:
            transcript_generation(data_dir, attempts=1)
        except TranscriptPublicationRace:
            raise
        except (LegacyPublication, OSError, RuntimeError):
            pass
        else:
            result = read()
    return result


SIDECHAIN_SESSION_PREFIX = "agent-"


def is_sidechain_session(session: str) -> bool:
    return str(session or "").startswith(SIDECHAIN_SESSION_PREFIX)


def is_side_session(row: Mapping[str, object]) -> bool:
    """Classify indexed and live session summaries with one policy."""
    texts = (
        str(row.get(name) or "").lstrip().lower()
        for name in ("title", "last_text", "first_text")
    )
    return bool(
        row.get("sub") or row.get("parent")
        or is_sidechain_session(str(row.get("session") or ""))
        or any(text.startswith(("[subagent task]", "[subagent message]"))
               for text in texts))

class CallingFamily(NamedTuple):
    session: str
    root: str
    members: frozenset[str]
    resolved: bool = True
    recap_turn: int | None = None
    side_members: frozenset[str] = frozenset()

    def contains(self, session: str) -> bool:
        return session in self.members


class SelfExclusion(NamedTuple):
    family: CallingFamily
    boundary: int | None
    reason: str

    @property
    def windowed(self) -> bool:
        return self.boundary is not None

    def excludes(self, session: str, turn: object) -> bool:
        if not self.windowed:
            return self.family.contains(session)
        if session != self.family.session:
            # A recap turn is evidence about the caller's transcript only.
            # Family membership cannot project that boundary onto a child or
            # sibling whose turn space and active context are independent.
            return False
        if type(turn) is not int or type(self.boundary) is not int:  # noqa: E721
            return False
        return turn >= self.boundary

    def labels(self, session: str, turn: object) -> bool:
        return (self.windowed and session == self.family.session
                and type(turn) is int and type(self.boundary) is int  # noqa: E721
                and turn < self.boundary)

    def announced(self, session: str, turn: object) -> bool:
        """Rows an exclusion notice can truthfully say were withheld."""
        return self.excludes(session, turn)

    def query_filters(self) -> dict:
        if self.boundary is not None and type(self.boundary) is not int:  # noqa: E721
            return {}
        out = {"exclude_session": self.family.session}
        if self.windowed:
            out["exclude_session_from_turn"] = self.boundary
        return out


def family_root(
        session: str, parents: Mapping, memo: dict[str, str] | None = None,
) -> str:
    """Root conversation for a root/child/grandchild session chain."""
    memo = memo if memo is not None else {}
    if session in memo:
        return memo[session]
    current, path, positions = session, [], {}
    root = session
    while current:
        if current in positions:
            # Ingest publishes a DAG, but canonicalize malformed cycles so the
            # same corrupt family cannot split by traversal starting point.
            root = min(path[positions[current]:])
            break
        positions[current] = len(path)
        path.append(current)
        raw = parents.get(current, "")
        parent = (raw.get("parent") if isinstance(raw, dict) else raw) or ""
        if not parent:
            root = current
            break
        current = str(parent)
    for item in path:
        memo[item] = root
    return root


def _open_session_family_index():
    import sqlite3
    path = DATA_DIR / "corpus.db"
    before = session_family_source_stamp()
    if before is None or not path.exists():
        return None
    db = None
    try:
        db = open_sqlite_snapshot(path, 0)
        db.execute("PRAGMA busy_timeout=0")
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute(
            "SELECT value FROM meta WHERE key='family_stamp'").fetchone()
        after = session_family_source_stamp()
        source_stable = getattr(db, "source_stable", None)
        if (row and row[0] == before and after == before
                and (source_stable is None or source_stable())):
            return db
    except (OSError, sqlite3.DatabaseError):
        pass
    if db is not None:
        db.close()
    return None


def _indexed_calling_family_state_in_db(
        db, session: str,
) -> tuple[str, frozenset[str], int | None] | None:
    """Resolve one caller, its family, and recap boundary in an owned snapshot."""
    import sqlite3
    try:
        rows = list(db.execute(
            "SELECT caller.root, member.session "
            "FROM session_family AS caller "
            "JOIN session_family AS member ON member.root=caller.root "
            "WHERE caller.session=? ORDER BY member.session LIMIT ?",
            (session, SESSION_FAMILY_MAX_MEMBERS + 1),
        ))
        if (not rows or not rows[0][0]
                or len(rows) > SESSION_FAMILY_MAX_MEMBERS):
            return None
        root = str(rows[0][0])
        members = frozenset(str(row[1]) for row in rows)
        recap_turn = None
        try:
            recap = db.execute(
                "SELECT MAX(turn) FROM msgs WHERE session=? AND who='recap'",
                (session,),
            ).fetchone()
            # A turn boundary is trustworthy only when SQLite stored it as an
            # integer.  Coercing REAL/TEXT values with int() would manufacture
            # a window from malformed evidence instead of failing open.
            if recap and type(recap[0]) is int:  # noqa: E721 - exact DB type
                recap_turn = recap[0] if recap[0] >= 0 else None
        except (sqlite3.DatabaseError, TypeError, ValueError):
            pass
        return root, members, recap_turn
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None

def _indexed_family_side_members_in_db(
        db, root: str, members: Iterable[str] = (),
) -> frozenset[str]:
    """Read structurally proven side sessions from the owned family snapshot."""
    import sqlite3
    try:
        rows = list(db.execute(
            "SELECT session FROM session_family "
            "WHERE root=? AND side=1 ORDER BY session LIMIT ?",
            (root, SESSION_FAMILY_MAX_MEMBERS + 1),
        ))
        if len(rows) > SESSION_FAMILY_MAX_MEMBERS:
            return frozenset()
        return frozenset(str(row[0]) for row in rows if row and row[0])
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        # Schema-14 snapshots can exist only during upgrade and in old fixtures.
        return frozenset(
            session for session in members if is_sidechain_session(session))


def _indexed_calling_family_details(
        session: str,
) -> tuple[str, frozenset[str], int | None, frozenset[str]] | None:
    db = _open_session_family_index()
    if db is None:
        return None
    try:
        state = _indexed_calling_family_state_in_db(db, session)
        if state is None:
            return None
        root, members, recap_turn = state
        return (
            root, members, recap_turn,
            _indexed_family_side_members_in_db(db, root, members),
        )
    finally:
        db.close()


def _indexed_calling_family_state(
        session: str,
) -> tuple[str, frozenset[str], int | None] | None:
    """Resolve one caller, its family, and recap boundary from one snapshot."""
    db = _open_session_family_index()
    if db is None:
        return None
    try:
        return _indexed_calling_family_state_in_db(db, session)
    finally:
        db.close()


def indexed_calling_family(
        session: str,
) -> tuple[str, frozenset[str]] | None:
    """Resolve one caller and all family members from one immutable snapshot."""
    state = _indexed_calling_family_state(session)
    return (state[0], state[1]) if state is not None else None

def indexed_calling_family_with_sides(
        session: str,
) -> tuple[str, frozenset[str], frozenset[str]] | None:
    """Resolve one family plus its structural side-session subset."""
    state = _indexed_calling_family_details(session)
    return (state[0], state[1], state[3]) if state is not None else None


def indexed_family_metadata(
        sessions: Iterable[str],
) -> dict[str, tuple[str, bool]] | None:
    """Resolve requested roots and side provenance through one family snapshot."""
    import sqlite3
    wanted = tuple(dict.fromkeys(
        str(session) for session in sessions if session))
    if not wanted:
        return {}
    db = _open_session_family_index()
    if db is None:
        return None
    metadata = {session: (session, False) for session in wanted}
    try:
        for start in range(0, len(wanted), 500):
            page = wanted[start:start + 500]
            marks = ",".join("?" for _ in page)
            metadata.update(
                (str(session), (str(root), bool(side)))
                for session, root, side in db.execute(
                    "SELECT session, root, side FROM session_family "
                    f"WHERE session IN ({marks})",
                    page,
                )
            )
        return metadata
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        db.close()


def indexed_family_roots(sessions: Iterable[str]) -> dict[str, str] | None:
    """Resolve only the requested sessions through one coherent family snapshot."""
    import sqlite3
    wanted = tuple(dict.fromkeys(
        str(session) for session in sessions if session))
    if not wanted:
        return {}
    db = _open_session_family_index()
    if db is None:
        return None
    roots = {session: session for session in wanted}
    try:
        for start in range(0, len(wanted), 500):
            page = wanted[start:start + 500]
            marks = ",".join("?" for _ in page)
            roots.update(
                (str(session), str(root))
                for session, root in db.execute(
                    "SELECT session, root FROM session_family "
                    f"WHERE session IN ({marks})",
                    page,
                )
            )
        return roots
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        db.close()


def indexed_session_matches(query: str) -> list[str] | None:
    """Resolve one exact session or prefix without materializing the census."""
    import sqlite3
    query = query.strip()
    if not query:
        return []
    db = _open_session_family_index()
    if db is None:
        return None
    try:
        exact = db.execute(
            "SELECT session FROM session_family WHERE session=?", (query,)
        ).fetchone()
        if exact:
            return [str(exact[0])]
        upper = query + "\U0010ffff"
        return [
            str(row[0]) for row in db.execute(
                "SELECT session FROM session_family "
                "WHERE session>=? AND session<? ORDER BY session LIMIT ?",
                (query, upper, SESSION_PREFIX_MAX_CANDIDATES + 1),
            )
        ]
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        db.close()


@dataclass(frozen=True)
class SessionPrefixIndex(Sequence[str]):
    ordered: tuple[str, ...]
    force_full: frozenset[str] = frozenset()

    def __len__(self) -> int:
        return len(self.ordered)

    def __getitem__(self, index):
        return self.ordered[index]


def session_prefix_index(
        sessions: Iterable[str], *,
        force_full: Iterable[str] = (),
) -> SessionPrefixIndex:
    """Precompute once per page the sorted ids encode_result_handle sizes against."""
    ordered = tuple(sorted({str(value) for value in sessions}))
    forced = frozenset(str(value) for value in force_full) & frozenset(ordered)
    return SessionPrefixIndex(ordered, forced)


def indexed_session_prefix_candidates(
        sessions: Iterable[str], prefix_chars: int = 8,
) -> Sequence[str]:
    """Return only collisions needed to render unambiguous result handles."""
    import sqlite3
    targets = tuple(sorted({
        str(session) for session in sessions if session
    }))
    if not targets:
        return session_prefix_index(())
    candidates = set(targets)
    force_full: set[str] = set()
    db = _open_session_family_index()
    if db is None:
        force_full.update(targets)
    else:
        try:
            grouped: dict[str, list[str]] = {}
            for session in targets:
                width = max(3, min(int(prefix_chars), len(session)))
                if width >= len(session):
                    continue
                prefix = session[:width]
                grouped.setdefault(prefix, []).append(session)
            for prefix, members in grouped.items():
                rows = [
                    str(row[0]) for row in db.execute(
                        "SELECT session FROM session_family "
                        "WHERE session>=? AND session<? ORDER BY session LIMIT ?",
                        (prefix, prefix + "\U0010ffff",
                         SESSION_PREFIX_MAX_CANDIDATES + 1),
                    )
                ]
                if len(rows) > SESSION_PREFIX_MAX_CANDIDATES:
                    force_full.update(members)
                else:
                    present = set(rows)
                    force_full.update(
                        session for session in members if session not in present)
                    candidates.update(rows)
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            force_full.update(targets)
        finally:
            db.close()
    return session_prefix_index(candidates, force_full=force_full)


def indexed_session_prose_count(session: str) -> int | None:
    """Count source-like prose rows for one session from the coherent index."""
    import sqlite3
    db = _open_session_family_index()
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT count(*) FROM msgs WHERE session=? "
            "AND who NOT IN ('agent', 'tool')",
            (session,),
        ).fetchone()
        return int(row[0]) if row else 0
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        db.close()


def indexed_self_exclusion_has_rows(policy: SelfExclusion) -> bool | None:
    """Whether the current coherent corpus contains rows the policy excludes."""
    import sqlite3
    db = _open_session_family_index()
    if db is None:
        return None
    caller = policy.family.session
    try:
        if policy.windowed:
            row = db.execute(
                "SELECT 1 FROM msgs WHERE "
                "(session=? AND (turn IS NULL OR turn>=?)) OR "
                "(session<>? AND session IN ("
                "SELECT member.session FROM session_family AS member "
                "JOIN session_family AS owner ON owner.root=member.root "
                "WHERE owner.session=? AND member.side=1)) LIMIT 1",
                (caller, int(policy.boundary), caller, caller),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT 1 FROM msgs WHERE session=? OR session IN ("
                "SELECT member.session FROM session_family AS member "
                "JOIN session_family AS owner ON owner.root=member.root "
                "WHERE owner.session=?) LIMIT 1",
                (caller, caller),
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        db.close()


def cli_name() -> str:
    """How the user invokes us in prose/help: `agrep` when installed OR when they came
    in through a shim that says so ($AGREP_CLI_NAME, set by agrep.cmd); bare
    `python cli.py` only when actually run that way in a checkout. Single source so
    every message names the command the same way (status banner, doctor, the
    auto-index notices)."""
    env = os.environ.get("AGREP_CLI_NAME")
    if env:
        return env
    return "python cli.py" if _is_dev_checkout() else "agrep"


KNOWN_AGENTS = ADAPTER_INPUT_NAMES


def match_session_ids(ids, query: str) -> list[str]:
    """Resolve a full session id or any unambiguous/ambiguous prefix consistently.

    Callers decide how to present ambiguity; exact matches always win. Keeping this tiny
    primitive below explorer avoids pulling its corpus caches into ``resume`` startup.
    """
    query = query.strip()
    ordered = list(dict.fromkeys(str(session) for session in ids if session))
    if query in ordered:
        return [query]
    return [session for session in ordered if session.startswith(query)]


def agent_filter_error(argv: Sequence[str]) -> str | None:
    """Reject an unusable --agent value up front: unknown would silently match
    nothing, empty would silently match everything. Returns the message to print,
    or None when the value is fine."""
    import surface_policy
    for i, tok in enumerate(argv):
        if tok == "--":
            break
        if tok == "--agent":
            val = argv[i + 1] if i + 1 < len(argv) else None
        elif tok.startswith("--agent="):
            val = tok[len("--agent="):]
        else:
            continue
        if val is None:
            continue
        if not val.strip():
            # one owned sentence for every blank filter value, whichever seam catches it
            return surface_policy.empty_filter_notice(
                surface_policy.FILTERS_BY_FLAG["--agent"])
        if val not in KNOWN_AGENTS:
            return f"unknown agent '{val}' - valid: {', '.join(KNOWN_AGENTS)}"
    return None


def in_agent_context(environ: Mapping[str, str] | None = None) -> bool:
    """Best-effort: is a coding agent rather than a human driving this shell?"""
    env = os.environ if environ is None else environ
    return any(env.get(key) for key in AGENT_CONTEXT_ENV_KEYS)


# which agent's history a given env fingerprint belongs to (calling_session)
_CLAUDE_ENV = ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT")


class CallerIdentity(NamedTuple):
    session: str | None
    reason: str


def calling_identity(
        environ: Mapping[str, str] | None = None,
) -> CallerIdentity:
    """Resolve only identities exported by the calling agent."""
    env = os.environ if environ is None else environ
    identities = {
        "claude": env.get("CLAUDE_CODE_SESSION_ID", "").strip(),
        "codex": env.get("CODEX_THREAD_ID", "").strip(),
        "pi": env.get("AGREP_PI_SESSION_ID", "").strip(),
    }
    claude_present = bool(
        identities["claude"] or any(env.get(key) for key in _CLAUDE_ENV))
    nonempty = [(agent, value) for agent, value in identities.items() if value]
    if (claude_present and not identities["claude"] and nonempty
            or len({value for _, value in nonempty}) > 1):
        return CallerIdentity(None, "identity-conflict")
    if len(nonempty) > 1:
        return CallerIdentity(nonempty[0][1], "corroborated")
    if nonempty:
        agent, session = nonempty[0]
        return CallerIdentity(session, agent)
    return CallerIdentity(None, "caller-unresolved")


def calling_session() -> str | None:
    """The directly exported caller session, or None when absent or conflicting."""
    return calling_identity().session


def self_exclusion_unavailable_notice(reason: str) -> str:
    if reason == "identity-conflict":
        return (
            "caller session identities conflict; self-exclusion is disabled "
            "and current conversations may appear")
    return (
        "could not identify this session - results may include "
        "the current conversation")


def calling_family() -> CallingFamily | None:
    """Resolve the caller and its root-session family once per command."""
    session = calling_session()
    if not session:
        return None
    indexed = _indexed_calling_family_details(session)
    root, members, recap_turn, side_members = (
        indexed or (session, frozenset({session}), None, frozenset()))
    return CallingFamily(
        session=session,
        root=root,
        members=members | {session},
        resolved=indexed is not None,
        recap_turn=recap_turn,
        side_members=side_members,
    )


@contextmanager
def calling_family_snapshot():
    """Yield caller identity, family, and their still-open corpus snapshot.

    A context-resume reader must not resolve lineage in one generation and read
    transcript rows from another. ``None`` for the database means the caller or
    generation-bound family index could not be proven; callers fail closed.
    """
    identity = calling_identity()
    if not identity.session:
        yield identity, None, None
        return
    db = _open_session_family_index()
    if db is None:
        yield identity, None, None
        return
    try:
        indexed = _indexed_calling_family_state_in_db(db, identity.session)
        if indexed is None:
            yield identity, None, db
            return
        root, members, recap_turn = indexed
        family = CallingFamily(
            session=identity.session,
            root=root,
            members=members | {identity.session},
            resolved=True,
            recap_turn=recap_turn,
            side_members=_indexed_family_side_members_in_db(
                db, root, members),
        )
        yield identity, family, db
    finally:
        db.close()


def calling_self_exclusion(*, conservative: bool = False) -> SelfExclusion | None:
    """Resolve a proven caller window, or the explicitly forced family scope.

    Automatic exclusion fails open: without both a coherent family lookup and
    a recap boundary there is no evidence for where the current context starts.
    ``--no-self`` is the separate, intentional whole-family operation.
    """
    family = calling_family()
    if family is None:
        return None
    if conservative:
        return SelfExclusion(family, None, "forced")
    if not family.resolved or family.recap_turn is None:
        return None
    return SelfExclusion(family, family.recap_turn, "window")


def setup_hint() -> str:
    """The crisp next step every "no index yet" message ends with, consistent across
    commands; an agent context is told why it should bother."""
    tail = " to make this machine's agent history searchable" if in_agent_context() else ""
    return f"next: run `{cli_name()} setup`{tail}."
