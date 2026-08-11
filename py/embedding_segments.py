"""Immutable semantic segments selected by one publish-last manifest."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import struct
import time
import uuid
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import fileops

VERSION = 2
SET_VERSION = 1
PROOF_VERSION = 1
PROOF_KEY = "publication_proof"
SEGMENT_DIR = "embedding-segments"
ARTIFACT_KEYS = ("f32", "q8", "f16", "groups", "ids", "hashes", "refs")
_HEX = frozenset("0123456789abcdef")
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x00000100000001B3
_WINDOWS = os.name == "nt"


def _readonly_contains(path: Path) -> bool:
    protected = os.environ.get("AGREP_DATA_READONLY")
    if not protected:
        return False
    try:
        root = os.path.normcase(os.path.realpath(protected))
        target = os.path.normcase(os.path.realpath(os.fspath(path)))
        return os.path.commonpath((root, target)) == root
    except (OSError, ValueError):
        return False


def _require_writable_publication(meta_path: Path, action: str) -> None:
    if _readonly_contains(Path(meta_path)):
        raise PermissionError(
            f"AGREP_DATA_READONLY protects the data directory; cannot {action}")


class SegmentError(RuntimeError):
    pass


class SegmentPublicationRace(SegmentError):
    """A concurrent publisher moved the artifacts this pass was building on.

    Evidence of concurrency, never of damage: the losing pass abandons its
    publication and the next one republishes against the newer generation."""


def identity_republished(before, after) -> bool:
    """Whether a path holds a DIFFERENT file (dev/ino), not a changed one."""
    return tuple(before)[:2] != tuple(after)[:2]


class LoadedManifest(dict):
    """A manifest dict retaining its non-serialized publication path."""

    def __init__(self, record: dict, path: Path):
        super().__init__(record)
        self.path = Path(path)


def _canonical(record: object) -> bytes:
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _replace(
        source: Path, target: Path, *, attempts: int = 80,
        before_attempt=None) -> None:
    """Keep publish-last atomic while transient Windows readers release the target."""
    delay = 0.025
    for attempt in range(max(1, attempts)):
        if before_attempt is not None:
            before_attempt()
        try:
            os.replace(source, target)
            return
        except OSError:
            if not _WINDOWS or attempt + 1 >= max(1, attempts):
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def _write_bytes(path: Path, payload: bytes, *, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path, before_attempt=before_replace)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file(source: Path, target: Path) -> dict:
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise SegmentError(f"segment input is not a regular file: {source}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while block := reader.read(8 * 1024 * 1024):
                writer.write(block)
                digest.update(block)
                size += len(block)
            writer.flush()
            os.fsync(writer.fileno())
        _replace(temporary, target)
        _fsync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": f"{SEGMENT_DIR}/{target.name}", "size": size,
            "sha256": digest.hexdigest()}


def _adopt_file(source: Path, target: Path) -> dict:
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise SegmentError(f"segment input is not a regular file: {source}")
    size = source.stat().st_size
    digest = _sha_file(source)
    _replace(source, target)
    _fsync_dir(target.parent)
    return {"path": f"{SEGMENT_DIR}/{target.name}", "size": size,
            "sha256": digest}


def _artifact_path(meta_path: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise SegmentError("segment artifact path is not a string")
    value = Path(relative)
    if (value.is_absolute() or len(value.parts) != 2
            or value.parts[0] != SEGMENT_DIR or value.name in ("", ".", "..")):
        raise SegmentError(f"invalid segment artifact path: {relative!r}")
    path = meta_path.parent / value
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SegmentError(f"missing segment artifact: {path}") from exc
    if not path.is_file() or path.is_symlink() or not metadata.st_size >= 0:
        raise SegmentError(f"segment artifact is not a regular file: {path}")
    return path


def artifact_path(manifest: LoadedManifest, record_or_relative: object) -> Path:
    """Resolve one validated descriptor or relative path against embeddings.meta."""
    if not isinstance(manifest, LoadedManifest):
        raise TypeError("artifact_path requires a LoadedManifest")
    relative = (record_or_relative.get("path")
                if isinstance(record_or_relative, Mapping) else record_or_relative)
    return _artifact_path(manifest.path, relative)


def _validate_descriptor(meta_path: Path, record: object, *, verify_hash: bool) -> Path:
    if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
        raise SegmentError("invalid segment artifact descriptor")
    size = record.get("size")
    digest = record.get("sha256")
    if (type(size) is not int or size < 0 or not isinstance(digest, str)
            or len(digest) != 64 or any(char not in _HEX for char in digest)):
        raise SegmentError("invalid segment artifact identity")
    path = _artifact_path(meta_path, record["path"])
    if path.stat().st_size != size:
        raise SegmentError(f"segment artifact size mismatch: {path}")
    if verify_hash and _sha_file(path) != digest:
        raise SegmentError(f"segment artifact digest mismatch: {path}")
    return path


def _fnv64(payload: bytes) -> int:
    value = _FNV_OFFSET
    for byte in payload:
        value ^= byte
        value = (value * _FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return value


def _matrix_headers(q8: Path, groups: Path, rows: int, dim: int,
                    *, verify_payload: bool) -> int:
    q8_bytes = q8.read_bytes() if verify_payload else None
    with q8.open("rb") as stream:
        qh = stream.read(64)
    with groups.open("rb") as stream:
        gh = stream.read(64)
    if (len(qh) != 64 or qh[:4] != b"AGQ8"
            or struct.unpack_from("<I", qh, 4)[0] != 1
            or struct.unpack_from("<I", qh, 8)[0] != dim
            or struct.unpack_from("<I", qh, 12)[0] != 1
            or struct.unpack_from("<Q", qh, 16)[0] != rows
            or struct.unpack_from("<I", qh, 40)[0] != dim + 4):
        raise SegmentError("q8 segment header does not match its descriptor")
    group_count = struct.unpack_from("<I", gh, 12)[0] if len(gh) == 64 else 0
    if (len(gh) != 64 or gh[:4] != b"AGQG"
            or struct.unpack_from("<I", gh, 4)[0] != 1
            or struct.unpack_from("<Q", gh, 16)[0] != rows
            or struct.unpack_from("<I", gh, 40)[0] != 4
            or qh[24:40] != gh[24:40] or group_count <= 0):
        raise SegmentError("group segment header does not match its descriptor")
    if q8.stat().st_size != 64 + rows * (dim + 4):
        raise SegmentError("q8 segment length does not match its descriptor")
    if groups.stat().st_size != 64 + rows * 4:
        raise SegmentError("group segment length does not match its descriptor")
    if verify_payload:
        group_bytes = groups.read_bytes()
        if (_fnv64(q8_bytes[64:]) != struct.unpack_from("<Q", qh, 48)[0]
                or _fnv64(group_bytes[64:]) != struct.unpack_from("<Q", gh, 48)[0]):
            raise SegmentError("quantized segment checksum mismatch")
        if any(value[0] >= group_count
               for value in struct.iter_unpack("<I", group_bytes[64:])):
            raise SegmentError("group id exceeds the segment group count")
    return group_count


_REF_COLUMNS_V1 = (
    ("local_ord", "INTEGER"), ("row_ref", "INTEGER"), ("mid", "TEXT"),
    ("text_hash", "TEXT"), ("agent", "TEXT"), ("project", "TEXT"),
    ("session", "TEXT"), ("ts", "INTEGER"), ("turn", "INTEGER"),
    ("who", "TEXT"), ("model", "TEXT"), ("family_id", "INTEGER"),
)
_REF_COLUMNS_V2 = _REF_COLUMNS_V1 + (("metadata_hash", "TEXT"),)
_REF_COLUMNS_V3 = _REF_COLUMNS_V2 + (("family_label", "TEXT"),)
_REF_COLUMNS_V4 = _REF_COLUMNS_V3 + (("model_source", "TEXT"),)
_REF_COLUMNS_V5 = _REF_COLUMNS_V4 + (("side", "INTEGER"),)
_REF_SCHEMAS = {
    tuple(_REF_COLUMNS_V1): 1, tuple(_REF_COLUMNS_V2): 2,
    tuple(_REF_COLUMNS_V3): 3, tuple(_REF_COLUMNS_V4): 4,
    tuple(_REF_COLUMNS_V5): 5,
}


def _create_refs_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE refs(
            local_ord INTEGER PRIMARY KEY, row_ref INTEGER NOT NULL,
            mid TEXT NOT NULL, text_hash TEXT NOT NULL, agent TEXT NOT NULL,
            project TEXT NOT NULL, session TEXT NOT NULL, ts INTEGER NOT NULL,
            turn INTEGER NOT NULL, who TEXT NOT NULL, model TEXT,
            family_id INTEGER NOT NULL, metadata_hash TEXT NOT NULL,
            family_label TEXT, model_source TEXT NOT NULL,
            side INTEGER NOT NULL CHECK(side IN (0, 1)));
        CREATE UNIQUE INDEX refs_row_ref ON refs(row_ref);
        CREATE UNIQUE INDEX refs_mid ON refs(mid);
        CREATE INDEX refs_session ON refs(session,turn);
    """)


def _refs_schema_version(connection: sqlite3.Connection) -> int:
    columns = tuple(
        (row[1], row[2]) for row in connection.execute("PRAGMA table_info(refs)"))
    return int(_REF_SCHEMAS.get(columns, 0))


def _open_refs(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    if not _refs_schema_version(connection):
        connection.close()
        raise SegmentError("segment refs schema is incompatible")
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(refs)")}
    if not {"refs_row_ref", "refs_mid", "refs_session"}.issubset(indexes):
        connection.close()
        raise SegmentError("segment refs indexes are incomplete")
    return connection


def _refs_have_metadata(connection: sqlite3.Connection) -> bool:
    return any(row[1] == "metadata_hash"
               for row in connection.execute("PRAGMA table_info(refs)"))


def _refs_have_family_label(connection: sqlite3.Connection) -> bool:
    return any(row[1] == "family_label"
               for row in connection.execute("PRAGMA table_info(refs)"))


def _refs_have_model_source(connection: sqlite3.Connection) -> bool:
    return any(row[1] == "model_source"
               for row in connection.execute("PRAGMA table_info(refs)"))


def _refs_have_side(connection: sqlite3.Connection) -> bool:
    return any(row[1] == "side"
               for row in connection.execute("PRAGMA table_info(refs)"))


def refs_schema_versions(manifest: LoadedManifest) -> frozenset[int]:
    versions: set[int] = set()
    for segment in manifest["segments"]:
        connection = _open_refs(artifact_path(manifest, segment["artifacts"]["refs"]))
        try:
            versions.add(_refs_schema_version(connection))
        finally:
            connection.close()
    return frozenset(versions)


def _lines(path: Path) -> Iterable[str]:
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n") or b"\r" in raw:
                raise SegmentError(f"segment line artifact is not LF-delimited: {path}")
            try:
                value = raw[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SegmentError(f"segment line artifact is not UTF-8: {path}") from exc
            if not value:
                raise SegmentError(f"segment line artifact has an empty row: {path}")
            yield value


def _validate_refs(path: Path, ids: Path, hashes: Path, row_base: int, rows: int,
                   group_count: int) -> None:
    connection = _open_refs(path)
    try:
        has_metadata = _refs_have_metadata(connection)
        columns = "local_ord,row_ref,mid,text_hash,family_id"
        if has_metadata:
            columns += ",metadata_hash"
        has_family_label = _refs_have_family_label(connection)
        if has_family_label:
            columns += ",family_label"
        has_model_source = _refs_have_model_source(connection)
        if has_model_source:
            columns += ",model_source"
        has_side = _refs_have_side(connection)
        if has_side:
            columns += ",side"
        cursor = connection.execute(
            f"SELECT {columns} FROM refs ORDER BY local_ord")
        count = 0
        for count, (record, mid, text_hash) in enumerate(
                zip(cursor, _lines(ids), _lines(hashes), strict=True), 1):
            local_ord, row_ref, ref_mid, ref_hash, family_id = record[:5]
            metadata_hash = record[5] if has_metadata else None
            family_label = record[6] if has_family_label else None
            model_source = record[7] if has_model_source else None
            side = record[8] if has_side else None
            ordinal = count - 1
            if (local_ord != ordinal or row_ref != row_base + ordinal
                    or ref_mid != mid or ref_hash != text_hash
                    or type(family_id) is not int or not 0 <= family_id < group_count
                    or has_metadata and (
                        not isinstance(metadata_hash, str)
                        or len(metadata_hash) != 32
                        or any(char not in _HEX for char in metadata_hash))
                    or has_family_label and (
                        (family_id == 0) != (family_label is None)
                        or family_label is not None and (
                            not isinstance(family_label, str)
                            or not family_label.startswith("f:")
                            or "\n" in family_label or "\r" in family_label))
                    or has_model_source and (
                        not isinstance(model_source, str) or not model_source
                        or "\n" in model_source or "\r" in model_source)
                    or has_side and (
                        type(side) is not int or side not in (0, 1))):
                raise SegmentError("segment refs are not aligned with ids and hashes")
        if count != rows:
            raise SegmentError("segment refs row count does not match its descriptor")
    finally:
        connection.close()


def _validate_refs_fast(path: Path, row_base: int, rows: int,
                        group_count: int) -> None:
    connection = _open_refs(path)
    try:
        first = connection.execute(
            "SELECT local_ord,row_ref,family_id FROM refs WHERE local_ord=0").fetchone()
        last = connection.execute(
            "SELECT local_ord,row_ref,family_id FROM refs WHERE local_ord=?",
            (rows - 1,)).fetchone()
        if (first is None or last is None
                or first[:2] != (0, row_base)
                or last[:2] != (rows - 1, row_base + rows - 1)
                or not 0 <= int(first[2]) < group_count
                or not 0 <= int(last[2]) < group_count):
            raise SegmentError("segment refs edge rows do not match their descriptor")
    finally:
        connection.close()


def _iter_shadow_values(path: Path, rows: int) -> Iterable[int]:
    previous = -1
    remaining = int(rows)
    with path.open("rb") as stream:
        while remaining:
            count = min(remaining, 65_536)
            payload = stream.read(count * 8)
            if len(payload) != count * 8:
                raise SegmentError(
                    "shadow artifact length does not match its descriptor")
            for value, in struct.iter_unpack("<Q", payload):
                if value <= previous:
                    raise SegmentError(
                        "shadow row references are not sorted and unique")
                previous = value
                yield value
            remaining -= count
        if stream.read(1):
            raise SegmentError(
                "shadow artifact length does not match its descriptor")


def _dead_row_mask(manifest: LoadedManifest) -> bytearray:
    dead = bytearray(int(manifest["physical_rows"]))
    for shadow in manifest["shadows"]:
        limit = int(shadow["before_row_ref"])
        path = artifact_path(manifest, shadow["artifact"])
        for value in _iter_shadow_values(path, int(shadow["rows"])):
            if value >= limit or value >= len(dead) or dead[value]:
                raise SegmentError("shadow liveness is inconsistent")
            dead[value] = 1
    return dead


def _validate_manifest_header(
        record: object,
        trusted_prefix: Mapping | None,
) -> tuple[dict, int, list, int, int]:
    top = {"version", "generation", "published_at", "source", "model", "coverage",
           "segments", "shadows", "group_count", "live_rows", "physical_rows",
           "next_row_ref", "delta_count", "set_manifest"}
    allowed = (top, top | {PROOF_KEY})
    if (not isinstance(record, dict) or set(record) not in allowed
            or record.get("version") != VERSION):
        raise SegmentError("unsupported segmented embedding manifest")
    generation = record.get("generation")
    if (not isinstance(generation, str) or len(generation) != 32
            or any(char not in _HEX for char in generation)):
        raise SegmentError("invalid segmented embedding generation")
    model = record.get("model")
    if (not isinstance(model, dict) or set(model) != {"id", "dim"}
            or not isinstance(model.get("id"), str) or not model["id"]
            or type(model.get("dim")) is not int or not 0 < model["dim"] <= 16_384):
        raise SegmentError("invalid segmented embedding model")
    if not isinstance(record.get("source"), dict) or not record["source"]:
        raise SegmentError("segmented embeddings have no source binding")
    if (not isinstance(record.get("published_at"), (int, float))
            or not math.isfinite(record["published_at"])):
        raise SegmentError("invalid segmented embedding publication time")
    dim = model["dim"]
    segments = record.get("segments")
    if not isinstance(segments, list) or not segments:
        raise SegmentError("segmented embeddings have no vector segment")
    trusted_segments = list((trusted_prefix or {}).get("segments") or [])
    trusted_shadows = list((trusted_prefix or {}).get("shadows") or [])
    if trusted_prefix is not None:
        if (record.get("model") != trusted_prefix.get("model")
                or segments[:len(trusted_segments)] != trusted_segments
                or record.get("shadows", [])[:len(trusted_shadows)] != trusted_shadows):
            raise SegmentError("segmented embedding prefix changed during publication")
    return record, dim, segments, len(trusted_segments), len(trusted_shadows)


def _validate_segments(
        record: Mapping,
        meta_path: Path,
        segments: Sequence,
        dim: int,
        trusted_count: int,
        *,
        verify_hashes: bool,
        validate_liveness: bool,
) -> int:
    row_high_water = 0
    highest_group = 0
    seen_ids: set[str] = set()
    for index, segment in enumerate(segments):
        keys = {"id", "kind", "row_base", "rows", "artifacts"}
        if not isinstance(segment, dict) or set(segment) != keys:
            raise SegmentError("invalid vector segment descriptor")
        segment_id = segment.get("id")
        if (not isinstance(segment_id, str) or len(segment_id) != 32
                or any(char not in _HEX for char in segment_id) or segment_id in seen_ids):
            raise SegmentError("invalid or duplicate vector segment id")
        seen_ids.add(segment_id)
        if segment.get("kind") != ("base" if index == 0 else "delta"):
            raise SegmentError("vector segment kind or order is invalid")
        if (type(segment.get("row_base")) is not int
                or segment["row_base"] != row_high_water
                or type(segment.get("rows")) is not int or segment["rows"] <= 0):
            raise SegmentError("vector segment row range is invalid")
        artifacts = segment.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_KEYS):
            raise SegmentError("vector segment artifact set is incomplete")
        trusted = index < trusted_count
        paths = {key: _validate_descriptor(
            meta_path, artifacts[key], verify_hash=verify_hashes and not trusted)
            for key in ARTIFACT_KEYS}
        rows = segment["rows"]
        if paths["f32"].stat().st_size != rows * dim * 4:
            raise SegmentError("f32 segment length does not match its descriptor")
        if paths["f16"].stat().st_size != rows * dim * 2:
            raise SegmentError("f16 segment length does not match its descriptor")
        group_count = _matrix_headers(
            paths["q8"], paths["groups"], rows, dim,
            verify_payload=verify_hashes and not trusted)
        highest_group = max(highest_group, group_count)
        if validate_liveness or (verify_hashes and not trusted):
            _validate_refs(paths["refs"], paths["ids"], paths["hashes"],
                           segment["row_base"], rows, group_count)
        else:
            _validate_refs_fast(
                paths["refs"], segment["row_base"], rows, group_count)
        row_high_water += rows
    if (type(record.get("group_count")) is not int
            or record["group_count"] < highest_group or record["group_count"] <= 0):
        raise SegmentError("global semantic group count is invalid")
    return row_high_water


def _validate_shadows(
        record: Mapping,
        meta_path: Path,
        row_high_water: int,
        trusted_count: int,
        *,
        verify_hashes: bool,
) -> tuple[bytearray, set[str]]:
    dead = bytearray(row_high_water)
    shadow_ids: set[str] = set()
    previous_limit = 0
    shadows = record.get("shadows")
    if not isinstance(shadows, list):
        raise SegmentError("invalid shadow descriptor list")
    for shadow_index, shadow in enumerate(shadows):
        keys = {"id", "before_row_ref", "rows", "artifact"}
        if not isinstance(shadow, dict) or set(shadow) != keys:
            raise SegmentError("invalid shadow descriptor")
        shadow_id = shadow.get("id")
        if (not isinstance(shadow_id, str) or len(shadow_id) != 32
                or any(char not in _HEX for char in shadow_id) or shadow_id in shadow_ids):
            raise SegmentError("invalid or duplicate shadow id")
        shadow_ids.add(shadow_id)
        limit, rows = shadow.get("before_row_ref"), shadow.get("rows")
        if (type(limit) is not int or limit < previous_limit or limit > row_high_water
                or type(rows) is not int or rows <= 0):
            raise SegmentError("shadow publication range is invalid")
        previous_limit = limit
        trusted = shadow_index < trusted_count
        path = _validate_descriptor(
            meta_path, shadow["artifact"], verify_hash=verify_hashes and not trusted)
        for value in _iter_shadow_values(path, rows):
            if value >= limit or dead[value]:
                raise SegmentError("shadow targets a future or already-dead row")
            dead[value] = 1
    return dead, shadow_ids


def _validate_manifest_accounting(
        record: Mapping,
        meta_path: Path,
        segments: Sequence,
        row_high_water: int,
        dead: bytearray,
        shadow_ids: set[str],
) -> None:
    physical = sum(segment["rows"] for segment in segments)
    live = physical - sum(dead)
    delta_ids = {segment["id"] for segment in segments[1:]} | shadow_ids
    if (record.get("physical_rows") != physical or record.get("next_row_ref") != row_high_water
            or record.get("live_rows") != live or record.get("delta_count") != len(delta_ids)):
        raise SegmentError("segmented embedding row accounting is inconsistent")
    coverage = record.get("coverage")
    coverage_keys = {"indexed", "total", "pending", "complete", "order"}
    if (not isinstance(coverage, dict) or set(coverage) != coverage_keys
            or coverage.get("indexed") != live
            or type(coverage.get("total")) is not int or coverage["total"] < live
            or coverage.get("pending") != coverage["total"] - live
            or coverage.get("complete") is not (coverage["total"] == live)
            or not isinstance(coverage.get("order"), str) or not coverage["order"]):
        raise SegmentError("segmented embedding coverage is inconsistent")
    set_path = _validate_descriptor(meta_path, record["set_manifest"], verify_hash=True)
    expected_set = _native_set(record)
    try:
        observed_set = json.loads(set_path.read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise SegmentError("native segment-set manifest is invalid") from exc
    if observed_set != expected_set:
        raise SegmentError("native segment-set manifest does not match embeddings.meta")
    _validated_publication_proof(record, meta_path)


def _proof_descriptors(record: Mapping) -> dict[str, dict]:
    values = [record["set_manifest"]]
    for segment in record["segments"]:
        values.extend(segment["artifacts"].values())
    values.extend(shadow["artifact"] for shadow in record["shadows"])
    output = {}
    for descriptor in values:
        relative = descriptor.get("path") if isinstance(descriptor, dict) else None
        if not isinstance(relative, str) or relative in output:
            raise SegmentError("publication proof has duplicate artifact paths")
        output[relative] = descriptor
    return output


def _proof_binding(record: Mapping) -> str:
    value = json.loads(json.dumps(record, allow_nan=False))
    value.pop(PROOF_KEY, None)
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validated_publication_proof(
        record: Mapping, meta_path: Path) -> list[dict] | None:
    descriptor = record.get(PROOF_KEY)
    if descriptor is None:
        return None
    path = _validate_descriptor(meta_path, descriptor, verify_hash=False)
    if path.stat().st_size > 1024 * 1024:
        raise SegmentError("segmented publication proof is too large")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SegmentError("segmented publication proof is unreadable") from exc
    if (len(payload) != descriptor["size"]
            or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]):
        raise SegmentError("segmented publication proof digest mismatch")
    try:
        proof = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SegmentError("segmented publication proof is invalid") from exc
    keys = {"version", "generation", "manifest_sha256", "artifacts"}
    if (not isinstance(proof, dict) or set(proof) != keys
            or proof.get("version") != PROOF_VERSION
            or proof.get("generation") != record.get("generation")
            or proof.get("manifest_sha256") != _proof_binding(record)):
        raise SegmentError("segmented publication proof does not bind the manifest")
    expected = _proof_descriptors(record)
    artifacts = proof.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(expected):
        raise SegmentError("segmented publication proof artifact set is invalid")
    observed = {}
    for artifact in artifacts:
        if (not isinstance(artifact, dict)
                or set(artifact) != {"path", "size", "sha256", "identity"}):
            raise SegmentError("segmented publication proof artifact is invalid")
        relative = artifact.get("path")
        identity = artifact.get("identity")
        if (not isinstance(relative, str) or relative in observed
                or not isinstance(identity, list) or len(identity) != 5
                or any(type(value) is not int for value in identity)):
            raise SegmentError("segmented publication proof identity is invalid")
        descriptor = expected.get(relative)
        if (descriptor is None
                or artifact.get("size") != descriptor.get("size")
                or artifact.get("sha256") != descriptor.get("sha256")):
            raise SegmentError("segmented publication proof artifact is unbound")
        observed[relative] = artifact
    if set(observed) != set(expected):
        raise SegmentError("segmented publication proof artifact set is incomplete")
    return [observed[relative] for relative in sorted(observed)]


def _schema(record: object, meta_path: Path, *, verify_hashes: bool,
            validate_liveness: bool, trusted_prefix: Mapping | None = None) -> dict:
    record, dim, segments, trusted_segment_count, trusted_shadow_count = (
        _validate_manifest_header(record, trusted_prefix))
    row_high_water = _validate_segments(
        record, meta_path, segments, dim, trusted_segment_count,
        verify_hashes=verify_hashes, validate_liveness=validate_liveness)
    dead, shadow_ids = _validate_shadows(
        record, meta_path, row_high_water, trusted_shadow_count,
        verify_hashes=verify_hashes)
    _validate_manifest_accounting(
        record, meta_path, segments, row_high_water, dead, shadow_ids)
    if validate_liveness:
        _validate_active_rows_streaming(
            LoadedManifest(record, meta_path), dead)
    return record


def load_manifest(path: Path, *, retries: int = 3, verify_hashes: bool = False,
                  validate_liveness: bool = False) -> LoadedManifest:
    """Open one coherent manifest; movement retries and corruption fails closed."""
    path = Path(path)
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            before = path.read_bytes()
            record = json.loads(before)
            _schema(record, path, verify_hashes=verify_hashes,
                    validate_liveness=validate_liveness)
            if before != path.read_bytes():
                raise RuntimeError("segmented embedding manifest moved while opening")
            return LoadedManifest(record, path)
        except (OSError, ValueError, TypeError, SegmentError, RuntimeError) as exc:
            last_error = exc
            try:
                moved = path.read_bytes() != locals().get("before", b"")
            except OSError:
                moved = False
            if moved and attempt + 1 < max(1, retries):
                time.sleep(0.005 * (attempt + 1))
                continue
            break
    raise SegmentError(f"cannot open segmented embeddings: {last_error}") from last_error


def _windows_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    return fileops.windows_file_identity(path)


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    if _WINDOWS:
        return _windows_file_identity(path)
    return fileops.file_identity(path)


def publication_artifact_identities(
        manifest: LoadedManifest,
) -> dict[Path, tuple[int, int, int, int, int]] | None:
    """Return publisher-observed identities, or None for a legacy generation."""
    proof = _validated_publication_proof(manifest, manifest.path)
    if proof is None:
        return None
    return {
        artifact_path(manifest, artifact["path"]): tuple(artifact["identity"])
        for artifact in proof
    }


def publication_artifacts_still_bound(manifest: LoadedManifest) -> bool:
    """Whether the current proof still names every artifact's live identity."""
    expected = publication_artifact_identities(manifest)
    if expected is None:
        return False
    try:
        _require_prefix_identities(expected)
    except SegmentPublicationRace:
        return False
    return True


def _verified_prefix_identities(
    manifest: LoadedManifest,
) -> dict[Path, tuple[int, int, int, int, int]]:
    """Pin a sound prefix, repairing byte-identical artifact relocation."""
    expected = publication_artifact_identities(manifest)
    if expected is not None:
        try:
            _require_prefix_identities(expected)
            return expected
        except SegmentPublicationRace:
            pass
    observed = _prefix_identities(manifest)
    verified = load_manifest(
        manifest.path, retries=1, verify_hashes=True,
        validate_liveness=True)
    if _canonical(verified) != _canonical(manifest):
        raise SegmentPublicationRace(
            "embedding manifest moved while repairing its publication proof")
    _require_prefix_identities(observed)
    return observed


def _require_publication_proof(manifest: LoadedManifest) -> None:
    _verified_prefix_identities(manifest)


def _publication_snapshot(
    meta_path: Path,
) -> tuple[
    LoadedManifest,
    bytes,
    dict[Path, tuple[int, int, int, int, int]],
]:
    current = load_manifest(meta_path)
    prefix_identities = _verified_prefix_identities(current)
    raw = meta_path.read_bytes()
    if raw != _canonical(current):
        raise SegmentPublicationRace(
            "embedding manifest moved before publication")
    return current, raw, prefix_identities


def _prefix_identities(
    manifest: LoadedManifest,
) -> dict[Path, tuple[int, int, int, int, int]]:
    paths = set()
    for segment in manifest["segments"]:
        paths.update(artifact_path(manifest, value)
                     for value in segment["artifacts"].values())
    paths.update(artifact_path(manifest, shadow["artifact"])
                 for shadow in manifest["shadows"])
    paths.add(artifact_path(manifest, manifest["set_manifest"]))
    if manifest.get(PROOF_KEY) is not None:
        paths.add(artifact_path(manifest, manifest[PROOF_KEY]))
    return {path: _file_identity(path) for path in paths}


def _require_prefix_identities(
    expected: Mapping[Path, tuple[int, int, int, int, int]],
) -> None:
    """The prefix proof is a movement check, not a damage check: it cannot tell
    corruption from a concurrent publish, so it never claims one."""
    try:
        current = {path: _file_identity(path) for path in expected}
    except OSError as exc:
        raise SegmentPublicationRace(
            "segmented embedding prefix disappeared during publication") from exc
    if current != dict(expected):
        raise SegmentPublicationRace(
            "segmented embedding prefix moved during publication")


def _native_set(record: Mapping) -> dict:
    def local(descriptor: Mapping) -> str:
        return Path(descriptor["path"]).name
    return {
        "version": SET_VERSION,
        "generation": record["generation"],
        "dim": record["model"]["dim"],
        "row_high_water": record["next_row_ref"],
        "live_rows": record["live_rows"],
        "group_count": record["group_count"],
        "segments": [{
            "row_base": segment["row_base"], "rows": segment["rows"],
            "artifact": local(segment["artifacts"]["q8"]),
            "groups": local(segment["artifacts"]["groups"]),
        } for segment in record["segments"]],
        "shadows": [{"path": local(shadow["artifact"]), "rows": shadow["rows"]}
                    for shadow in record["shadows"]],
    }


def _normalize_lines(values: Sequence[str] | Path, label: str) -> list[str]:
    if isinstance(values, (str, Path)):
        return list(_lines(Path(values)))
    rows = list(values)
    if any(not isinstance(value, str) or not value or "\n" in value or "\r" in value
           for value in rows):
        raise SegmentError(f"invalid {label} row")
    return rows


def _refs_db(path: Path, refs: Sequence[Mapping], ids: Sequence[str], hashes: Sequence[str],
             row_base: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
        """)
        _create_refs_schema(connection)
        if len(refs) != len(ids):
            raise SegmentError("refs and ids row counts differ")
        output = []
        for ordinal, (raw, mid, text_hash) in enumerate(zip(refs, ids, hashes, strict=True)):
            if not isinstance(raw, Mapping):
                raise SegmentError("segment ref is not an object")
            ref_mid = raw.get("mid", raw.get("id"))
            ref_hash = raw.get("text_hash", raw.get("hash"))
            if ref_mid != mid or ref_hash != text_hash:
                raise SegmentError("segment ref does not match its id/hash")
            agent, project, session = raw.get("agent"), raw.get("project"), raw.get("session")
            who, model = raw.get("who"), raw.get("model")
            ts, turn, family_id = raw.get("ts"), raw.get("turn"), raw.get("family_id")
            metadata_hash = raw.get("metadata_hash")
            family_label = raw.get("family_label")
            model_source = raw.get("model_source")
            side = raw.get("side")
            if (not all(isinstance(value, str) for value in (agent, project, session, who))
                    or not agent or not session or not who or model is not None
                    and not isinstance(model, str) or type(ts) is not int
                    or type(turn) is not int or type(family_id) is not int
                    or not 0 <= family_id <= 0xFFFFFFFF
                    or not isinstance(metadata_hash, str)
                    or len(metadata_hash) != 32
                    or any(char not in _HEX for char in metadata_hash)
                    or family_label is not None and (
                        not isinstance(family_label, str)
                        or not family_label.startswith("f:")
                        or "\n" in family_label or "\r" in family_label)
                    or (family_id == 0) != (family_label is None)):
                raise SegmentError("segment ref metadata is invalid")
            if (not isinstance(model_source, str) or not model_source
                    or "\n" in model_source or "\r" in model_source):
                raise SegmentError("segment ref model source is invalid")
            if type(side) not in (bool, int) or int(side) not in (0, 1):
                raise SegmentError("segment ref side provenance is invalid")
            output.append((ordinal, row_base + ordinal, mid, text_hash, agent, project,
                           session, ts, turn, who, model, family_id,
                           metadata_hash, family_label, model_source, int(side)))
        connection.executemany(
            "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", output)
        connection.commit()
    except sqlite3.Error as exc:
        raise SegmentError(f"cannot build segment refs: {exc}") from exc
    finally:
        connection.close()
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _line_file(path: Path, values: Sequence[str]) -> None:
    _write_bytes(path, "".join(f"{value}\n" for value in values).encode("utf-8"))


def _coverage(value: Mapping, live: int) -> dict:
    if not isinstance(value, Mapping):
        raise SegmentError("semantic coverage must be an object")
    total = value.get("total")
    if type(total) is not int or total < live:
        raise SegmentError("semantic coverage total is invalid")
    return {"indexed": live, "total": total, "pending": total - live,
            "complete": total == live, "order": str(value.get("order") or "newest-first")}


def _bundle(meta_path: Path, publication: str, kind: str, row_base: int,
            dim: int, artifacts: Mapping[str, Path], ids: Sequence[str] | Path,
            hashes: Sequence[str] | Path, refs: Sequence[Mapping] | Path,
            *, adopt_inputs: bool = False) -> tuple[dict, int]:
    if adopt_inputs:
        if not all(isinstance(value, (str, Path)) for value in (ids, hashes, refs)):
            raise SegmentError("adopted segment metadata must be file-backed")
        connection = _open_refs(Path(refs))
        try:
            rows = int(connection.execute("SELECT count(*) FROM refs").fetchone()[0])
        finally:
            connection.close()
        if rows <= 0:
            raise SegmentError("adopted segment has no rows")
        ids_rows = hash_rows = None
    else:
        ids_rows = _normalize_lines(ids, "id")
        hash_rows = _normalize_lines(hashes, "hash")
        if (len(ids_rows) != len(hash_rows) or not ids_rows
                or len(set(ids_rows)) != len(ids_rows)):
            raise SegmentError("segment ids/hashes are empty, duplicated, or misaligned")
        rows = len(ids_rows)
    directory = meta_path.parent / SEGMENT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    suffixes = {"f32": "f32", "q8": "q8", "f16": "f16", "groups": "q8g",
                "ids": "ids", "hashes": "hashes", "refs": "refs.sqlite"}
    records: dict[str, dict] = {}
    transfer = _adopt_file if adopt_inputs else _copy_file
    for key in ("f32", "q8", "f16", "groups"):
        if key not in artifacts:
            raise SegmentError(f"missing segment input: {key}")
        records[key] = transfer(
            Path(artifacts[key]), directory / f"seg-{publication}.{suffixes[key]}")
    ids_path = directory / f"seg-{publication}.ids"
    hashes_path = directory / f"seg-{publication}.hashes"
    refs_path = directory / f"seg-{publication}.refs.sqlite"
    if adopt_inputs:
        records["ids"] = _adopt_file(Path(ids), ids_path)
        records["hashes"] = _adopt_file(Path(hashes), hashes_path)
        records["refs"] = _adopt_file(Path(refs), refs_path)
    else:
        _line_file(ids_path, ids_rows)
        _line_file(hashes_path, hash_rows)
        records["ids"] = {
            "path": f"{SEGMENT_DIR}/{ids_path.name}", "size": ids_path.stat().st_size,
            "sha256": _sha_file(ids_path),
        }
        records["hashes"] = {
            "path": f"{SEGMENT_DIR}/{hashes_path.name}",
            "size": hashes_path.stat().st_size, "sha256": _sha_file(hashes_path),
        }
        if isinstance(refs, (str, Path)):
            records["refs"] = _copy_file(Path(refs), refs_path)
        else:
            _refs_db(refs_path, refs, ids_rows, hash_rows, row_base)
            records["refs"] = {
                "path": f"{SEGMENT_DIR}/{refs_path.name}",
                "size": refs_path.stat().st_size, "sha256": _sha_file(refs_path),
            }
    q8_path, groups_path = (meta_path.parent / records[key]["path"]
                            for key in ("q8", "groups"))
    group_count = _matrix_headers(q8_path, groups_path, rows, dim, verify_payload=True)
    if (records["f32"]["size"] != rows * dim * 4
            or records["f16"]["size"] != rows * dim * 2):
        raise SegmentError("segment matrix inputs do not match rows/dimension")
    _validate_refs(refs_path, ids_path, hashes_path, row_base, rows, group_count)
    return {"id": publication, "kind": kind, "row_base": row_base,
            "rows": rows, "artifacts": records}, group_count


def _shadow(meta_path: Path, publication: str, values: Sequence[int], limit: int) -> dict | None:
    normalized = sorted(values)
    if len(normalized) != len(set(normalized)) or any(
            type(value) is not int or value < 0 or value >= limit for value in normalized):
        raise SegmentError("shadow references are duplicated or out of range")
    if not normalized:
        return None
    path = meta_path.parent / SEGMENT_DIR / f"shadow-{publication}.u64"
    payload = b"".join(struct.pack("<Q", value) for value in normalized)
    _write_bytes(path, payload)
    return {"id": publication, "before_row_ref": limit, "rows": len(normalized),
            "artifact": {"path": f"{SEGMENT_DIR}/{path.name}", "size": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()}}


def _cleanup_unpublished_publication(meta_path: Path, publication: str) -> None:
    try:
        current = json.loads(meta_path.read_bytes())
        if (isinstance(current, dict) and current.get("version") == VERSION
                and current.get("generation") == publication):
            return
    except (OSError, ValueError, TypeError):
        return
    directory = meta_path.parent / SEGMENT_DIR
    for pattern in (
            f"seg-{publication}.*", f"shadow-{publication}.u64",
            f"set.{publication}.json",
            f"proof.{publication}.json"):
        for path in directory.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def _publish(meta_path: Path, record: dict, *, expected_raw: bytes | None,
             trusted_prefix: LoadedManifest | None = None,
             prefix_identities: Mapping | None = None,
             on_stage=None, before_replace=None) -> LoadedManifest:
    record.pop(PROOF_KEY, None)
    directory = meta_path.parent / SEGMENT_DIR
    set_path = directory / f"set.{record['generation']}.json"
    set_payload = _canonical(_native_set(record))
    _write_bytes(set_path, set_payload)
    record["set_manifest"] = {"path": f"{SEGMENT_DIR}/{set_path.name}",
                              "size": len(set_payload),
                              "sha256": hashlib.sha256(set_payload).hexdigest()}
    if trusted_prefix is not None:
        if prefix_identities is None:
            raise SegmentError("segmented publication has no prefix identity proof")
        _require_prefix_identities(prefix_identities)
    staged = LoadedManifest(record, meta_path)
    validated_identities = _prefix_identities(staged)
    _schema(record, meta_path, verify_hashes=True,
            validate_liveness=trusted_prefix is None,
            trusted_prefix=trusted_prefix)
    _require_prefix_identities(validated_identities)
    proof_path = directory / f"proof.{record['generation']}.json"
    descriptors = _proof_descriptors(record)
    proof_payload = _canonical({
        "version": PROOF_VERSION,
        "generation": record["generation"],
        "manifest_sha256": _proof_binding(record),
        "artifacts": [{
            "path": relative,
            "size": descriptor["size"],
            "sha256": descriptor["sha256"],
            "identity": list(validated_identities[
                artifact_path(staged, relative)]),
        } for relative, descriptor in sorted(descriptors.items())],
    })
    _write_bytes(proof_path, proof_payload)
    record[PROOF_KEY] = {
        "path": f"{SEGMENT_DIR}/{proof_path.name}",
        "size": len(proof_payload),
        "sha256": hashlib.sha256(proof_payload).hexdigest(),
    }
    _schema(record, meta_path, verify_hashes=False, validate_liveness=False,
            trusted_prefix=trusted_prefix)
    candidate = LoadedManifest(record, meta_path)
    _require_publication_proof(candidate)
    publication_identities = _prefix_identities(candidate)
    if on_stage:
        on_stage("before_manifest_replace")
    import embedding_store
    with embedding_store.EmbeddingPublicationGuard(meta_path) as publication:
        observed = meta_path.read_bytes() if meta_path.exists() else None
        if observed != expected_raw:
            raise SegmentPublicationRace(
                "embedding manifest moved before publication")

        def verify_and_fence() -> None:
            _require_prefix_identities(publication_identities)
            if before_replace is not None:
                before_replace()
            publication.verify()

        _write_bytes(
            meta_path, _canonical(record), before_replace=verify_and_fence)
    if on_stage:
        on_stage("after_manifest_replace")
    published = load_manifest(meta_path)
    # The generation this manifest replaced is unreachable, so the publication
    # that orphaned it reclaims it rather than leaving it for a scanner. Best
    # effort: a set that outlives one publication is collected by the next.
    try:
        prune_orphans(published)
    except (OSError, SegmentError, ValueError):
        pass
    return published


def publish_base(meta_path: Path, *, source: Mapping, model_id: str, dim: int,
                 artifacts: Mapping[str, Path], ids: Sequence[str] | Path,
                 hashes: Sequence[str] | Path, refs: Sequence[Mapping] | Path,
                 coverage: Mapping, expected_generation: str | None = None,
                 _on_stage=None, _before_replace=None,
                 _adopt_inputs: bool = False) -> LoadedManifest:
    """Publish a first or compacted base without mutating any old artifact."""
    meta_path = Path(meta_path)
    _require_writable_publication(meta_path, "publish a semantic segment base")
    before = meta_path.read_bytes() if meta_path.exists() else None
    if before is not None:
        current = {}
        try:
            current = json.loads(before)
            actual = (current.get("generation") if current.get("version") == VERSION
                      else (current.get("commit") or {}).get("generation"))
        except (ValueError, TypeError):
            actual = None
        if (expected_generation is not None and actual != expected_generation
                or expected_generation is None and current.get("version") == VERSION):
            raise SegmentError("base publication did not match the existing generation")
    elif expected_generation is not None:
        raise SegmentError("base publication expected a missing generation")
    publication = uuid.uuid4().hex
    try:
        segment, group_count = _bundle(
            meta_path, publication, "base", 0, int(dim),
            artifacts, ids, hashes, refs, adopt_inputs=_adopt_inputs)
        live = segment["rows"]
        normalized_source = json.loads(json.dumps(source, allow_nan=False))
        record = {
            "version": VERSION, "generation": publication, "published_at": time.time(),
            "source": normalized_source,
            "model": {"id": str(model_id), "dim": int(dim)},
            "coverage": _coverage(coverage, live), "segments": [segment], "shadows": [],
            "group_count": group_count, "live_rows": live, "physical_rows": live,
            "next_row_ref": live, "delta_count": 0, "set_manifest": {},
        }
        return _publish(
            meta_path, record, expected_raw=before, on_stage=_on_stage,
            before_replace=_before_replace)
    except Exception:
        _cleanup_unpublished_publication(meta_path, publication)
        raise


def publish_delta(meta_path: Path, *, source: Mapping, artifacts: Mapping[str, Path] | None,
                  ids: Sequence[str] | Path = (), hashes: Sequence[str] | Path = (),
                  refs: Sequence[Mapping] | Path = (), shadows: Sequence[int] = (),
                  coverage: Mapping, expected_generation: str | None = None,
                  _on_stage=None, _before_replace=None) -> LoadedManifest:
    """Append one changed-row bundle and/or a deletion shadow generation."""
    meta_path = Path(meta_path)
    _require_writable_publication(meta_path, "publish a semantic segment delta")
    current, before, prefix_identities = _publication_snapshot(meta_path)
    if expected_generation is not None and current["generation"] != expected_generation:
        raise SegmentPublicationRace(
            "delta publication did not match the expected generation")
    publication = uuid.uuid4().hex
    record = json.loads(json.dumps(current))
    dead = _dead_row_mask(current)
    if any(type(value) is int and 0 <= value < len(dead) and dead[value]
           for value in shadows):
        raise SegmentError("delta shadows an already-dead row")
    shadow = _shadow(meta_path, publication, shadows, current["next_row_ref"])
    new_ids = _normalize_lines(ids, "id")
    if new_ids:
        if artifacts is None:
            raise SegmentError("delta rows have no matrix artifacts")
        for segment in current["segments"]:
            refs_path = artifact_path(current, segment["artifacts"]["refs"])
            connection = _open_refs(refs_path)
            try:
                for mid in new_ids:
                    hit = connection.execute("SELECT row_ref FROM refs WHERE mid=?", (mid,)).fetchone()
                    if hit and not dead[int(hit[0])] and hit[0] not in shadows:
                        raise SegmentError("delta adds an id without shadowing its live row")
            finally:
                connection.close()
        segment, group_count = _bundle(
            meta_path, publication, "delta", current["next_row_ref"],
            current["model"]["dim"], artifacts, new_ids, hashes, refs)
        record["segments"].append(segment)
        record["group_count"] = max(record["group_count"], group_count)
        record["physical_rows"] += segment["rows"]
        record["next_row_ref"] += segment["rows"]
    elif any((artifacts, _normalize_lines(hashes, "hash"), refs)):
        raise SegmentError("empty delta has unexpected row artifacts")
    if shadow is not None:
        record["shadows"].append(shadow)
    if not new_ids and shadow is None:
        raise SegmentError("delta publication has no changes")
    record["generation"] = publication
    record["published_at"] = time.time()
    record["source"] = json.loads(json.dumps(source, allow_nan=False))
    record["live_rows"] = record["physical_rows"] - sum(
        value["rows"] for value in record["shadows"])
    record["coverage"] = _coverage(coverage, record["live_rows"])
    record["delta_count"] += 1
    record["set_manifest"] = {}
    try:
        return _publish(
            meta_path, record, expected_raw=before, trusted_prefix=current,
            prefix_identities=prefix_identities, on_stage=_on_stage,
            before_replace=_before_replace)
    except Exception:
        _cleanup_unpublished_publication(meta_path, publication)
        raise


def publish_rebind(meta_path: Path, *, source: Mapping, coverage: Mapping,
                   expected_generation: str, _on_stage=None,
                   _before_replace=None) -> LoadedManifest:
    """Bind an unchanged live set to a newer transcript generation."""
    meta_path = Path(meta_path)
    _require_writable_publication(meta_path, "rebind a semantic segment generation")
    current, before, prefix_identities = _publication_snapshot(meta_path)
    if current["generation"] != expected_generation:
        raise SegmentPublicationRace(
            "rebind did not match the expected generation")
    record = json.loads(json.dumps(current))
    record["generation"] = uuid.uuid4().hex
    record["published_at"] = time.time()
    record["source"] = json.loads(json.dumps(source, allow_nan=False))
    record["coverage"] = _coverage(coverage, record["live_rows"])
    record["set_manifest"] = {}
    try:
        return _publish(
            meta_path, record, expected_raw=before, trusted_prefix=current,
            prefix_identities=prefix_identities, on_stage=_on_stage,
            before_replace=_before_replace)
    except Exception:
        _cleanup_unpublished_publication(meta_path, record["generation"])
        raise


def iter_active_rows(
        manifest: LoadedManifest, dead: set[int] | bytearray | None = None,
) -> Iterable[dict]:
    dead = dead if dead is not None else _dead_row_mask(manifest)
    for segment_index, segment in enumerate(manifest["segments"]):
        connection = _open_refs(artifact_path(manifest, segment["artifacts"]["refs"]))
        try:
            has_metadata = _refs_have_metadata(connection)
            columns = (
                "row_ref,local_ord,mid,text_hash,agent,project,session,ts,turn,"
                "who,model,family_id"
            )
            if has_metadata:
                columns += ",metadata_hash"
            has_family_label = _refs_have_family_label(connection)
            if has_family_label:
                columns += ",family_label"
            has_model_source = _refs_have_model_source(connection)
            if has_model_source:
                columns += ",model_source"
            has_side = _refs_have_side(connection)
            if has_side:
                columns += ",side"
            for record in connection.execute(
                    f"SELECT {columns} FROM refs ORDER BY local_ord"):
                row_ref = int(record[0])
                is_dead = (
                    bool(dead[row_ref])
                    if isinstance(dead, bytearray) else row_ref in dead)
                if is_dead:
                    continue
                keys = ("row_ref", "local_ord", "mid", "text_hash", "agent", "project",
                        "session", "ts", "turn", "who", "model", "family_id")
                row = dict(zip(keys, record[:12], strict=True))
                offset = 12
                row["metadata_hash"] = record[offset] if has_metadata else None
                offset += int(has_metadata)
                row["family_label"] = record[offset] if has_family_label else None
                offset += int(has_family_label)
                row["model_source"] = (
                    str(record[offset]) if has_model_source else "unknown")
                offset += int(has_model_source)
                row["model_source_stored"] = has_model_source
                row["side"] = bool(record[offset]) if has_side else False
                row["side_stored"] = has_side
                row["segment"] = segment_index
                yield row
        finally:
            connection.close()


def _active_rows(
        manifest: LoadedManifest,
        dead: set[int] | bytearray | None = None) -> list[dict]:
    rows: list[dict] = []
    live_ids: set[str] = set()
    for row in iter_active_rows(manifest, dead):
        mid = str(row["mid"])
        if mid in live_ids:
            raise SegmentError(f"duplicate live embedding id: {mid}")
        live_ids.add(mid)
        rows.append(row)
    rows.sort(key=lambda row: row["row_ref"])
    if len(rows) != manifest["live_rows"]:
        raise SegmentError("active row reconstruction disagrees with the manifest")
    return rows


def _validate_active_rows_streaming(
        manifest: LoadedManifest,
        dead: set[int] | bytearray | None = None) -> None:
    connection = sqlite3.connect("")
    try:
        connection.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE live_ids(mid TEXT PRIMARY KEY) WITHOUT ROWID;
        """)
        count = 0
        previous_ref = -1
        buffer: list[tuple[str]] = []
        for row in iter_active_rows(manifest, dead):
            row_ref = int(row["row_ref"])
            if row_ref <= previous_ref:
                raise SegmentError("active embedding row order is invalid")
            previous_ref = row_ref
            buffer.append((str(row["mid"]),))
            count += 1
            if len(buffer) >= 4096:
                connection.executemany("INSERT INTO live_ids VALUES(?)", buffer)
                buffer.clear()
        if buffer:
            connection.executemany("INSERT INTO live_ids VALUES(?)", buffer)
        if count != manifest["live_rows"]:
            raise SegmentError(
                "active row reconstruction disagrees with the manifest")
    except sqlite3.IntegrityError as exc:
        raise SegmentError("duplicate live embedding id") from exc
    finally:
        connection.close()


def active_rows(manifest_or_path: LoadedManifest | Path) -> list[dict]:
    manifest = (manifest_or_path if isinstance(manifest_or_path, LoadedManifest)
                else load_manifest(Path(manifest_or_path)))
    return _active_rows(manifest)


def referenced_paths(manifest_or_path: LoadedManifest | Path) -> set[Path]:
    manifest = (manifest_or_path if isinstance(manifest_or_path, LoadedManifest)
                else load_manifest(Path(manifest_or_path)))
    output = {artifact_path(manifest, manifest["set_manifest"])}
    for segment in manifest["segments"]:
        output.update(
            artifact_path(manifest, value)
            for value in segment["artifacts"].values())
    output.update(
        artifact_path(manifest, shadow["artifact"])
        for shadow in manifest["shadows"])
    if manifest.get(PROOF_KEY) is not None:
        output.add(artifact_path(manifest, manifest[PROOF_KEY]))
    return output


def should_compact(manifest_or_path: LoadedManifest | Path, *, detailed: bool = False):
    manifest = (manifest_or_path if isinstance(manifest_or_path, LoadedManifest)
                else load_manifest(Path(manifest_or_path)))
    dead = manifest["physical_rows"] - manifest["live_rows"]
    dead_fraction = dead / max(1, manifest["live_rows"])
    hard = manifest["delta_count"] >= 16
    soft = manifest["delta_count"] >= 8 or dead_fraction >= 0.05
    if detailed:
        return {"needed": soft, "hard": hard, "deltas": manifest["delta_count"],
                "dead_rows": dead, "dead_fraction": dead_fraction}
    return soft


def prune_legacy_layout(
        *, embeddings_path: Path, ids_path: Path,
        q8_manifest_path: Path, q8_artifact_dir: Path,
        generation_marker_path: Path | None = None,
) -> dict[str, int]:
    """Remove monolithic artifacts after this process releases their mappings."""
    meta_path = embeddings_path.parent / "embeddings.meta"
    try:
        load_manifest(meta_path)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return {"removed": 0, "deferred": 0}
    candidates = [
        embeddings_path, ids_path,
        embeddings_path.with_suffix(".hashes"), q8_manifest_path,
    ]
    if generation_marker_path is not None:
        candidates.append(generation_marker_path)
    directory = q8_artifact_dir
    try:
        if directory.is_symlink():
            candidates.append(directory)
        elif directory.is_dir():
            candidates.extend(directory.iterdir())
    except OSError:
        return {"removed": 0, "deferred": 1}
    removed = deferred = 0
    for path in candidates:
        try:
            if path.is_dir() and not path.is_symlink():
                deferred += 1
            elif path.exists() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError:
            deferred += 1
    if directory.is_dir() and not directory.is_symlink():
        try:
            directory.rmdir()
        except OSError:
            deferred += 1
    return {"removed": removed, "deferred": deferred}


def orphan_artifacts(
        manifest_or_path: LoadedManifest | Path, *,
        grace_seconds: float = 300.0, now: float | None = None,
        allow_missing_manifest: bool = False) -> dict:
    """Inventory mature files outside the publish-last manifest."""
    if isinstance(manifest_or_path, LoadedManifest):
        manifest = manifest_or_path
        meta_path = manifest.path
        protected = referenced_paths(manifest)
    else:
        meta_path = Path(manifest_or_path)
        missing = False
        if allow_missing_manifest:
            try:
                meta_path.lstat()
            except FileNotFoundError:
                missing = True
        if missing:
            protected = set()
        else:
            manifest = load_manifest(meta_path)
            protected = referenced_paths(manifest)
    directory = meta_path.parent / SEGMENT_DIR
    current = time.time() if now is None else float(now)
    paths = []
    size = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return {"count": 0, "bytes": 0, "paths": ()}
    for path in entries:
        try:
            if path in protected or not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
        except OSError:
            continue
        age = current - stat.st_mtime
        if 0.0 <= age < max(0.0, grace_seconds):
            continue
        paths.append(path)
        size += int(stat.st_size)
    return {"count": len(paths), "bytes": size, "paths": tuple(paths)}


def prune_orphans(manifest_or_path: LoadedManifest | Path, *, grace_seconds: float = 300.0,
                  _unlink=None) -> dict:
    manifest = (manifest_or_path if isinstance(manifest_or_path, LoadedManifest)
                else load_manifest(Path(manifest_or_path)))
    inventory = orphan_artifacts(manifest, grace_seconds=grace_seconds)
    removed, deferred = [], []
    unlink = _unlink or (lambda path: path.unlink())
    for path in inventory["paths"]:
        try:
            unlink(path)
            removed.append(path)
        except OSError:
            deferred.append(path)
    return {"removed": removed, "deferred": deferred}
