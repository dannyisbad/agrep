"""Generation-bound q8 candidates with an f16 exact-rerank source."""

from __future__ import annotations

import atexit
import hashlib
import importlib
import json
import os
import secrets
import struct
import subprocess
import threading
from pathlib import Path

import numpy as np

import common

VERSION = 1
MANIFEST_VERSION = 3
PROTOCOL = 1
SCORE_KIND = "cosine-q8-v1"
EXACT_SCORE_KIND = "cosine-f16-v1"
MANIFEST_PATH = common.DATA_DIR / "embeddings.q8.meta"
ARTIFACT_DIR = common.DATA_DIR / "semantic-q8"
GROUP_POLICY_VERSION = 1
EXCLUDED_GROUP = "x:default-excluded"
_READY = struct.Struct("<4sIQI16s")
_REQUEST = struct.Struct("<4sII16s")
_TOP_REQUEST = struct.Struct("<4sIII16s")
_GROUP_TOP_REQUEST = struct.Struct("<4sIIII16s")
_RESPONSE = struct.Struct("<4sIIIQ16s")
_GROUP_HEADER = struct.Struct("<4sIIIQ16sIIQ8s")
_CANDIDATE = np.dtype([("ordinal", "<u8"), ("score", "<f4")])
MAX_CANDIDATES = 4096
_SCANNER = None
_F16 = None
_F16_IDENTITY = None
_GROUPS = None
_GROUPS_IDENTITY = None
_SEGMENT_SIDECARS = None
_SEGMENT_SIDECARS_IDENTITY = None
_LOCK = threading.Lock()


def _data_dir_readonly() -> bool:
    return _mutation_refusal_reason() is not None


def _mutation_refusal_info():
    runtime = importlib.import_module("indexd_runtime")
    if common.data_dir_readonly(common.DATA_DIR):
        return runtime.DerivedMutationInfo(
            "readonly", None,
            "AGREP_DATA_READONLY protects the data directory")
    return runtime.derived_writer_mutation_settled()


def _mutation_refusal_reason() -> str | None:
    info = _mutation_refusal_info()
    return None if info.writable else info.reason


def _require_writable_data_dir(action: str) -> None:
    runtime = importlib.import_module("indexd_runtime")
    info = _mutation_refusal_info()
    if info.writable:
        return
    if info.journal_blocked:
        raise runtime.DerivedWriteContended(f"{info.reason}; cannot {action}")
    raise PermissionError(f"{info.reason}; cannot {action}")


class PackedEligibility:
    __slots__ = ("bits", "rows", "count")

    def __init__(self, bits, rows: int, count: int):
        self.bits = np.ascontiguousarray(bits, dtype=np.uint8).reshape(-1)
        self.rows = int(rows)
        self.count = int(count)


def _eligibility_bits(eligible, rows: int) -> np.ndarray | None:
    if eligible is None:
        return None
    if isinstance(eligible, PackedEligibility):
        if (eligible.rows != rows or len(eligible.bits) != (rows + 7) // 8
                or eligible.count < 0 or eligible.count > rows):
            raise ValueError("q8 packed eligibility does not match the index")
        if (rows % 8 and int(eligible.bits[-1])
                & ~((1 << (rows % 8)) - 1)):
            raise ValueError("q8 packed eligibility has nonzero padding bits")
        return eligible.bits
    if isinstance(eligible, np.ndarray):
        values = eligible
    else:
        try:
            values = np.asarray(list(eligible))
        except TypeError as exc:
            raise ValueError("q8 eligibility must be row references or a boolean mask") from exc
    if values.ndim != 1:
        raise ValueError("q8 eligibility must be one-dimensional")
    if values.dtype == np.bool_:
        if len(values) != rows:
            raise ValueError("q8 eligibility mask length does not match the index")
        return np.ascontiguousarray(np.packbits(values, bitorder="little"))
    if values.size and not np.issubdtype(values.dtype, np.integer):
        raise ValueError("q8 eligible row references must be integers")
    if values.size and (np.any(values < 0) or np.any(values >= rows)):
        raise ValueError("q8 eligible row reference is out of range")
    refs = np.asarray(values, dtype=np.intp)
    bits = np.zeros((rows + 7) // 8, dtype=np.uint8)
    if len(refs):
        masks = np.left_shift(
            np.uint8(1), np.asarray(refs % 8, dtype=np.uint8))
        np.bitwise_or.at(bits, refs // 8, masks)
    return bits


def enabled() -> bool:
    value = os.environ.get("AGREP_SEMANTIC_Q8_SHADOW", "")
    return value.lower() not in ("", "0", "false", "no", "off")


def _read_exact(stream, size: int) -> bytes:
    parts = bytearray(size)
    view = memoryview(parts)
    offset = 0
    while offset < size:
        got = stream.readinto(view[offset:])
        if not got:
            raise EOFError("q8 scanner closed its protocol stream")
        offset += got
    return bytes(parts)


def _generation_bytes(value: str) -> bytes:
    raw = bytes.fromhex(value)
    if len(raw) != 16:
        raise ValueError("q8 generation is not 16 bytes")
    return raw


def _artifact_generation(manifest: dict) -> str:
    return str(manifest.get("artifact_generation")
               or manifest.get("f32_generation") or "")


def _f32_state() -> dict:
    return common.embedding_artifact_state(
        common.EMBEDDINGS_PATH.parent / "embeddings.meta",
        common.EMBEDDINGS_PATH,
        common.IDS_PATH,
    )


def _path_identity(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino]
    except OSError:
        return None


def _group_source_identity(expected: dict) -> dict:
    ids = ((expected.get("commit") or {}).get("ids") or {}).get("sha256")
    return {
        "version": GROUP_POLICY_VERSION,
        "excluded_roles": sorted(common.SEMANTIC_DEFAULT_EXCLUDED_ROLES),
        "messages": _path_identity(common.MESSAGES_PATH),
        "family": common.session_family_source_stamp(),
        "ids_sha256": ids,
    }


def _current_commit() -> dict:
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    for _ in range(3):
        before = _path_identity(meta_path)
        raw = meta_path.read_bytes()
        record = json.loads(raw)
        if isinstance(record, dict) and int(record.get("version", 0)) == 2:
            commit = record
        else:
            commit = common.read_embedding_commit(meta_path)
        after = _path_identity(meta_path)
        if (before is not None and before == after
                and raw == meta_path.read_bytes() and isinstance(commit, dict)):
            return commit
    raise RuntimeError("embedding commit moved during q8 validation")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _build_f16(
    embeddings_path: Path,
    output_dir: Path,
    *,
    generation: str,
    rows: int,
    dim: int,
) -> dict:
    _require_writable_data_dir("build the semantic f16 accelerator")
    expected_size = rows * dim * 4
    if embeddings_path.stat().st_size != expected_size:
        raise RuntimeError("f32 matrix moved before f16 derivation")
    output_dir.mkdir(parents=True, exist_ok=True)
    common._prune_embedding_temps(output_dir / "exact.f16")
    temporary = common.embedding_temp_path(
        output_dir / "exact.f16", "f16_derivation")
    matrix = None
    digest = hashlib.sha256()
    try:
        try:
            matrix = np.memmap(
                embeddings_path, dtype="<f4", mode="r", shape=(rows, dim))
            with temporary.open("wb") as stream:
                chunk_rows = max(1, (32 * 1024 * 1024) // max(2, dim * 2))
                for start in range(0, rows, chunk_rows):
                    block = np.asarray(matrix[start:start + chunk_rows], dtype="<f2")
                    payload = block.tobytes(order="C")
                    stream.write(payload)
                    digest.update(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if matrix is not None:
                mapping = getattr(matrix, "_mmap", None)
                if mapping is not None:
                    mapping.close()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    checksum = digest.hexdigest()
    expected_f16_size = rows * dim * 2
    if temporary.stat().st_size != expected_f16_size:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("f16 derivation produced an invalid matrix length")
    legacy = output_dir / f"exact.{generation}.{checksum[:16]}.f16"
    candidates = [legacy, *sorted(output_dir.glob(
        f"exact.{generation}.{checksum[:16]}.*.f16"))]
    try:
        artifact = next((candidate for candidate in candidates
                         if candidate.is_file()
                         and candidate.stat().st_size == expected_f16_size
                         and _sha256_file(candidate) == checksum), None)
        if artifact is None:
            artifact = output_dir / (
                f"exact.{generation}.{checksum[:16]}.{secrets.token_hex(8)}.f16")
            os.replace(temporary, artifact)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "exact_artifact": artifact,
        "exact_artifact_size": expected_f16_size,
        "exact_checksum": checksum,
        "exact_dtype": "<f2",
        "exact_score_kind": EXACT_SCORE_KIND,
    }


def build_from_f32(
    embeddings_path: Path,
    meta_path: Path,
    output_dir: Path,
    *,
    binary: Path | None = None,
    groups_path: Path | None = None,
    numeric_groups: bool = False,
) -> dict:
    _require_writable_data_dir("build the semantic q8 accelerator")
    binary = binary or common.ingest_bin()
    temporary_groups = None
    if groups_path is None:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rows = int((meta.get("commit") or {}).get("rows") or 0)
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary_groups = common.embedding_temp_path(
            output_dir / "groups.ids", "q8_unique_groups")
        with temporary_groups.open("w", encoding="utf-8", newline="\n") as stream:
            for ordinal in range(rows):
                stream.write(f"{ordinal}\n")
        groups_path = temporary_groups
    command = [
        str(binary), "semantic-q8-build",
        "--embeddings", str(embeddings_path),
        "--meta", str(meta_path),
        "--groups", str(groups_path),
        "--output-dir", str(output_dir),
    ]
    if numeric_groups:
        command.append("--numeric-groups")
    kwargs = {
        "capture_output": True, "text": True, "encoding": "utf-8", "timeout": 300,
    }
    if common.WIN:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(command, **kwargs)
    finally:
        if temporary_groups is not None:
            temporary_groups.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = result.stderr.strip()[-500:]
        # name the binary that failed: an env-overridden or installed copy
        # running against newer python is a mixed-version wedge, not a build bug
        origin = " (from $AGREP_RS_BIN)" if os.environ.get("AGREP_RS_BIN") else ""
        raise RuntimeError(
            f"q8 derivation failed via {binary}{origin}: "
            f"{detail or result.returncode}")
    record = json.loads(result.stdout)
    if (record.get("score_kind") != SCORE_KIND
            or int(record.get("version", 0)) != VERSION
            or int(record.get("group_version", 0)) != VERSION):
        raise RuntimeError("q8 builder returned an incompatible format")
    return record


def _normal_relative_file(root: Path, value) -> Path | None:
    try:
        raw = str(value)
        relative = Path(raw)
        if (not raw or relative.is_absolute()
                or any(part in ("", ".", "..") for part in relative.parts)):
            return None
        base = root.resolve()
        candidate = root / relative
        if candidate.is_symlink():
            return None
        path = candidate.resolve()
        path.relative_to(base)
        if not path.is_file():
            return None
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def _described_artifact(
    root: Path,
    record,
    expected_size: int | None = None,
) -> Path | None:
    if not isinstance(record, dict):
        return None
    path = _normal_relative_file(root, record.get("path"))
    digest = str(record.get("sha256") or "")
    size = int(record.get("size", -1))
    if (path is None or size < 0 or path.stat().st_size != size
            or (expected_size is not None and size != expected_size)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)):
        return None
    return path


def _validated_segment_manifest(record: dict) -> dict | None:
    try:
        generation = str(record.get("generation") or "")
        _generation_bytes(generation)
        model = record.get("model") or {}
        coverage = record.get("coverage") or {}
        dim = int(model.get("dim", 0))
        live_rows = int(record.get("live_rows", -1))
        physical_rows = int(record.get("physical_rows", -1))
        row_high_water = int(record.get("next_row_ref", -1))
        group_count = int(record.get("group_count", 0))
        indexed = int(coverage.get("indexed", live_rows))
        total = int(coverage.get("total", indexed))
        pending = int(coverage.get("pending", total - indexed))
        rich_segments = record.get("segments")
        if (int(record.get("version", 0)) != 2 or dim <= 0 or dim > 16_384
                or live_rows <= 0 or physical_rows < live_rows
                or row_high_water <= 0 or group_count <= 0 or indexed != live_rows
                or total < indexed or pending != total - indexed
                or not isinstance(rich_segments, list) or not rich_segments):
            return None
        root = common.EMBEDDINGS_PATH.parent
        set_path = _described_artifact(root, record.get("set_manifest"))
        if set_path is None:
            return None
        native = json.loads(set_path.read_text(encoding="utf-8"))
        if (not isinstance(native, dict) or int(native.get("version", 0)) != 1
                or str(native.get("generation") or "") != generation
                or int(native.get("dim", 0)) != dim
                or int(native.get("row_high_water", -1)) != row_high_water
                or int(native.get("live_rows", -1)) != live_rows
                or int(native.get("group_count", 0)) != group_count
                or not isinstance(native.get("segments"), list)
                or not isinstance(native.get("shadows"), list)):
            return None
        native_by_range = {}
        for segment in native["segments"]:
            if not isinstance(segment, dict):
                return None
            key = (int(segment.get("row_base", -1)), int(segment.get("rows", -1)))
            if key[0] < 0 or key[1] <= 0 or key in native_by_range:
                return None
            native_by_range[key] = segment
        if len(native_by_range) != len(rich_segments):
            return None

        normalized = []
        seen_ranges = set()
        for segment in rich_segments:
            if not isinstance(segment, dict):
                return None
            row_base = int(segment.get("row_base", -1))
            rows = int(segment.get("rows", -1))
            key = (row_base, rows)
            native_segment = native_by_range.get(key)
            artifacts = segment.get("artifacts") or {}
            if (row_base < 0 or rows <= 0 or key in seen_ranges
                    or native_segment is None or not isinstance(artifacts, dict)
                    or row_base + rows > row_high_water):
                return None
            seen_ranges.add(key)
            q8 = _described_artifact(
                root, artifacts.get("q8"), 64 + rows * (dim + 4))
            groups = _described_artifact(
                root, artifacts.get("groups"), 64 + rows * 4)
            exact = _described_artifact(
                root, artifacts.get("f16"), rows * dim * 2)
            native_q8 = _normal_relative_file(
                set_path.parent, native_segment.get("artifact"))
            native_groups = _normal_relative_file(
                set_path.parent, native_segment.get("groups"))
            if (q8 is None or groups is None or exact is None
                    or native_q8 != q8 or native_groups != groups):
                return None
            normalized.append({
                "id": segment.get("id"),
                "row_base": row_base,
                "rows": rows,
                "artifact_path": q8,
                "group_artifact_path": groups,
                "group_artifact_size": groups.stat().st_size,
                "exact_artifact_path": exact,
                "exact_artifact_size": exact.stat().st_size,
            })
        normalized.sort(key=lambda item: item["row_base"])
        previous_end = 0
        for index, segment in enumerate(normalized):
            if index and segment["row_base"] < previous_end:
                return None
            previous_end = segment["row_base"] + segment["rows"]
        if sum(segment["rows"] for segment in normalized) != physical_rows:
            return None
        return {
            "storage_version": 2,
            "score_kind": SCORE_KIND,
            "exact_score_kind": EXACT_SCORE_KIND,
            "exact_dtype": "<f2",
            "f32_generation": generation,
            "artifact_generation": generation,
            "generation_relation": "exact",
            "rows": row_high_water,
            "live_rows": live_rows,
            "physical_rows": physical_rows,
            "f32_rows": total,
            "pending_rows": pending,
            "complete": bool(coverage.get("complete", pending == 0)),
            "dim": dim,
            "group_count": int(native["group_count"]),
            "set_manifest_path": set_path,
            "segments": normalized,
        }
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _validated_manifest(expected: dict, *, source_current: bool = True) -> dict | None:
    commit = expected.get("commit") or {}
    if int(commit.get("version", 0)) == 2:
        return _validated_segment_manifest(commit)
    generation = str(commit.get("generation") or "")
    try:
        record = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        rows = int(record.get("rows", -1))
        f32_rows = int(record.get("f32_rows", -1))
        dim = int(record.get("dim", 0))
        artifact_generation = str(record.get("artifact_generation") or "")
        relation = str(record.get("generation_relation") or "")
        identity = expected.get("identity")
        if (int(record.get("version", 0)) != MANIFEST_VERSION
                or record.get("score_kind") != SCORE_KIND
                or record.get("exact_score_kind") != EXACT_SCORE_KIND
                or record.get("exact_dtype") != "<f2"
                or record.get("f32_generation") != generation
                or (identity is not None
                    and record.get("f32_identity") != identity)
                or f32_rows != int(commit.get("rows", -2))
                or rows <= 0 or rows > f32_rows
                or dim <= 0 or dim > 16_384
                or relation not in ("exact", "prefix")
                or (relation == "exact"
                    and (rows != f32_rows or artifact_generation != generation))
                or (relation == "prefix"
                    and (rows >= f32_rows or artifact_generation == generation))
                or (source_current and relation == "exact"
                    and record.get("group_source_identity")
                    != _group_source_identity(expected))):
            return None
        _generation_bytes(generation)
        _generation_bytes(artifact_generation)
        artifact = (ARTIFACT_DIR / str(record["artifact"])).resolve()
        if artifact.parent != ARTIFACT_DIR.resolve() or not artifact.is_file():
            return None
        expected_q8_size = 64 + rows * (dim + 4)
        if (artifact.stat().st_size != int(record.get("artifact_size", -1))
                or int(record.get("artifact_size", -1)) != expected_q8_size):
            return None
        group_artifact = (ARTIFACT_DIR / str(record["group_artifact"])).resolve()
        if (group_artifact.parent != ARTIFACT_DIR.resolve()
                or not group_artifact.is_file()
                or group_artifact.stat().st_size
                != int(record.get("group_artifact_size", -1))
                or int(record.get("group_artifact_size", -1))
                != 64 + rows * 4
                or int(record.get("group_count", 0)) <= 0):
            return None
        exact_artifact = (ARTIFACT_DIR / str(record["exact_artifact"])).resolve()
        expected_exact_size = rows * dim * 2
        if (exact_artifact.parent != ARTIFACT_DIR.resolve()
                or not exact_artifact.is_file()
                or exact_artifact.stat().st_size != expected_exact_size
                or expected_exact_size != int(record.get("exact_artifact_size", -1))):
            return None
        record["artifact_path"] = artifact
        record["group_artifact_path"] = group_artifact
        record["exact_artifact_path"] = exact_artifact
        return record
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _publish_manifest(record: dict) -> None:
    _require_writable_data_dir("publish the semantic q8 manifest")
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = common.embedding_temp_path(MANIFEST_PATH, "q8_manifest")
    try:
        temp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        common.replace_with_retry(temp, MANIFEST_PATH)
    finally:
        temp.unlink(missing_ok=True)


def _write_family_groups(
    path: Path,
    *,
    ids_path: Path | None = None,
    messages_path: Path | None = None,
    parents: dict | None = None,
) -> None:
    _require_writable_data_dir("write semantic family groups")
    ids_path = ids_path or common.IDS_PATH
    messages_path = messages_path or common.MESSAGES_PATH
    if parents is None:
        parents = common.await_family_publication(common.strict_family_parent_map)
        if parents is None:
            raise RuntimeError("session-family publication is unavailable")
    excluded_ids = {
        message.id for message in common.iter_messages(path=messages_path)
        if (str(message.who or "").lower()
            in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)
    }
    memo: dict[str, str] = {}
    with (ids_path.open(encoding="utf-8") as source,
          path.open("w", encoding="utf-8", newline="\n") as output):
        for raw in source:
            message_id = raw.rstrip("\n")
            if message_id in excluded_ids:
                output.write(EXCLUDED_GROUP + "\n")
                continue
            parts = message_id.split(":", 2)
            if len(parts) != 3 or not parts[1]:
                raise RuntimeError("embedding id cannot be grouped by conversation family")
            family = common.family_root(parts[1], parents, memo)
            if not family or "\n" in family or "\r" in family:
                raise RuntimeError("embedding family id is invalid")
            output.write("f:" + family + "\n")


def _prune_obsolete_artifacts(manifest: dict) -> None:
    if _data_dir_readonly():
        return
    keep = {
        Path(manifest[name]).resolve() for name in (
            "artifact_path", "group_artifact_path", "exact_artifact_path")
    }
    for pattern in ("embeddings.*.q8", "groups.*.q8g", "exact.*.f16"):
        for candidate in ARTIFACT_DIR.glob(pattern):
            if candidate.resolve() in keep:
                continue
            try:
                candidate.unlink()
            except OSError:
                pass


def ensure_artifact(expected: dict | None = None) -> dict:
    _require_writable_data_dir("publish a semantic accelerator")
    expected = expected or _f32_state()
    commit = expected.get("commit")
    if not isinstance(commit, dict) or not commit.get("generation"):
        raise RuntimeError("semantic accelerator requires a committed f32 generation")
    ready = _validated_manifest(expected)
    if ready is not None:
        _prune_obsolete_artifacts(ready)
        return ready

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    common._prune_embedding_temps(ARTIFACT_DIR / "groups.ids")
    groups_path = common.embedding_temp_path(
        ARTIFACT_DIR / "groups.ids", "q8_family_groups")
    group_source_identity = _group_source_identity(expected)
    try:
        _write_family_groups(groups_path)
        built = build_from_f32(
            common.EMBEDDINGS_PATH,
            common.EMBEDDINGS_PATH.parent / "embeddings.meta",
            ARTIFACT_DIR,
            groups_path=groups_path,
        )
        exact = _build_f16(
            common.EMBEDDINGS_PATH,
            ARTIFACT_DIR,
            generation=str(built["f32_generation"]),
            rows=int(built["rows"]),
            dim=int(built["dim"]),
        )
    finally:
        groups_path.unlink(missing_ok=True)
    after = _f32_state()
    if ((after.get("commit") or {}).get("generation")
            != built.get("f32_generation")):
        raise RuntimeError("f32 generation moved during q8 derivation")
    if _group_source_identity(after) != group_source_identity:
        raise RuntimeError("conversation families moved during q8 derivation")
    artifact = Path(str(built["artifact"])).resolve()
    group_artifact = Path(str(built["group_artifact"])).resolve()
    if (artifact.parent != ARTIFACT_DIR.resolve()
            or group_artifact.parent != ARTIFACT_DIR.resolve()):
        raise RuntimeError("q8 builder returned an artifact outside its data directory")
    record = {
        "version": MANIFEST_VERSION,
        "score_kind": SCORE_KIND,
        "exact_score_kind": str(exact["exact_score_kind"]),
        "f32_generation": built["f32_generation"],
        "f32_identity": after.get("identity"),
        "f32_rows": int((after.get("commit") or {}).get("rows", 0)),
        "artifact_generation": built["f32_generation"],
        "generation_relation": "exact",
        "rows": int(built["rows"]),
        "dim": int(built["dim"]),
        "artifact": artifact.name,
        "artifact_size": int(built["artifact_size"]),
        "checksum": str(built["checksum"]),
        "group_artifact": group_artifact.name,
        "group_artifact_size": int(built["group_artifact_size"]),
        "group_count": int(built["group_count"]),
        "group_checksum": str(built["group_checksum"]),
        "group_source_identity": group_source_identity,
        "exact_artifact": Path(exact["exact_artifact"]).name,
        "exact_artifact_size": int(exact["exact_artifact_size"]),
        "exact_checksum": str(exact["exact_checksum"]),
        "exact_dtype": str(exact["exact_dtype"]),
    }
    _publish_manifest(record)
    ready = _validated_manifest(after)
    if ready is None:
        raise RuntimeError("q8 manifest did not bind to the committed f32 generation")
    _prune_obsolete_artifacts(ready)
    return ready


def _rebind_append_generation(previous: dict, current: dict) -> dict | None:
    """Bind an immutable prefix accelerator to one proven append generation."""
    _require_writable_data_dir("rebind the semantic accelerator")
    prior = _validated_manifest(previous, source_current=False)
    previous_commit = previous.get("commit") or {}
    current_commit = current.get("commit") or {}
    if (prior is None
            or prior.get("f32_identity") != previous.get("identity")
            or int(current_commit.get("rows", 0))
            <= int(previous_commit.get("rows", 0))):
        return None
    record = {key: value for key, value in prior.items()
              if not key.endswith("_path")}
    record.update({
        "f32_generation": str(current_commit.get("generation") or ""),
        "f32_identity": current.get("identity"),
        "f32_rows": int(current_commit.get("rows", 0)),
        "generation_relation": "prefix",
    })
    _publish_manifest(record)
    after = _f32_state()
    if (after.get("identity") != current.get("identity")
            or (after.get("commit") or {}).get("generation")
            != current_commit.get("generation")):
        return None
    ready = _validated_manifest(after)
    if ready is None:
        return None
    _prune_obsolete_artifacts(ready)
    return ready


def _partial_publish_due(indexed: int) -> bool:
    try:
        record = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if int(record.get("version", 0)) != MANIFEST_VERSION:
            return True
        previous = max(0, int(record.get("rows", 0)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return True
    if indexed <= previous:
        return False
    threshold = min(previous * 2, previous + 100_000)
    return previous == 0 or indexed >= threshold


def publish_for_generation(coverage: dict, *, previous_state: dict | None = None,
                           strict_append: bool = False) -> dict | None:
    """Publish or safely rebind native candidates for one vector generation."""
    _require_writable_data_dir("publish a semantic accelerator generation")
    indexed = max(0, int(coverage.get("indexed") or 0))
    pending = max(0, int(coverage.get("pending") or 0))
    if not indexed:
        return None
    current = _f32_state()
    if int((current.get("commit") or {}).get("rows", -1)) != indexed:
        raise RuntimeError("semantic accelerator coverage does not match f32 rows")
    ready = _validated_manifest(current)
    if ready is not None:
        return ready
    if pending and not _partial_publish_due(indexed):
        if strict_append and previous_state is not None:
            ready = _rebind_append_generation(previous_state, current)
            if ready is not None:
                return ready
        return None
    return ensure_artifact(current)


class _Q8Scanner:
    def __init__(self, manifest: dict, *, binary: Path | None = None):
        command = [str(binary or common.ingest_bin()), "semantic-q8-serve"]
        set_manifest = manifest.get("set_manifest_path")
        group_artifact = manifest.get("group_artifact_path")
        if set_manifest is not None:
            command.extend(("--set", str(set_manifest)))
        else:
            command.extend(("--artifact", str(manifest["artifact_path"])))
            if group_artifact is not None:
                command.extend(("--groups", str(group_artifact)))
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "bufsize": 0,
            "close_fds": True,
        }
        if common.WIN:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(command, **kwargs)
        if self.process.stdin is None or self.process.stdout is None:
            self.close()
            raise RuntimeError("q8 scanner has no protocol pipes")
        raw = _read_exact(self.process.stdout, _READY.size)
        magic, protocol, rows, dim, generation = _READY.unpack(raw)
        artifact_generation = _artifact_generation(manifest)
        expected_generation = _generation_bytes(artifact_generation)
        if (magic != b"AQ8R" or protocol != PROTOCOL
                or rows != int(manifest["rows"])
                or dim != int(manifest["dim"])
                or generation != expected_generation):
            self.close()
            raise RuntimeError("q8 scanner readiness did not match its manifest")
        self.rows = int(rows)
        self.dim = int(dim)
        self.generation = generation
        self.identity = _scanner_identity(manifest)

    def score(self, query: np.ndarray, generation: str) -> np.ndarray:
        vector = self._query(query)
        expected = _generation_bytes(generation)
        self.process.stdin.write(_REQUEST.pack(
            b"AQ8Q", PROTOCOL, self.dim, expected))
        self.process.stdin.write(vector.tobytes(order="C"))
        self.process.stdin.flush()
        raw = _read_exact(self.process.stdout, _RESPONSE.size)
        magic, protocol, status, reserved, rows, actual = _RESPONSE.unpack(raw)
        if (magic != b"AQ8S" or protocol != PROTOCOL or reserved != 0
                or actual != self.generation):
            raise RuntimeError("q8 scanner returned an invalid response header")
        if status != 0 or rows != self.rows:
            raise RuntimeError(f"q8 scanner refused generation or query ({status})")
        scores = np.frombuffer(
            _read_exact(self.process.stdout, self.rows * 4), dtype="<f4").copy()
        if len(scores) != self.rows or not np.all(np.isfinite(scores)):
            raise RuntimeError("q8 scanner returned an invalid score vector")
        return scores

    def top(self, query: np.ndarray, generation: str, k: int, *,
            grouped: bool = False, heads: int = 8,
            eligible=None) -> tuple[np.ndarray, np.ndarray]:
        vector = self._query(query)
        k = int(k)
        heads = int(heads) if grouped else 1
        if k <= 0 or heads <= 0 or k * heads > MAX_CANDIDATES:
            raise ValueError(
                f"q8 candidate output must be 1..{MAX_CANDIDATES} rows")
        expected = _generation_bytes(generation)
        eligibility = _eligibility_bits(eligible, self.rows)
        magic = (b"AQ8H" if grouped else b"AQ8F") if eligibility is not None else (
            b"AQ8G" if grouped else b"AQ8K")
        request = (_GROUP_TOP_REQUEST.pack(
            magic, PROTOCOL, self.dim, k, heads, expected)
            if grouped else _TOP_REQUEST.pack(
                magic, PROTOCOL, self.dim, k, expected))
        self.process.stdin.write(request)
        self.process.stdin.write(vector.tobytes(order="C"))
        if eligibility is not None:
            self.process.stdin.write(eligibility.tobytes(order="C"))
        self.process.stdin.flush()
        raw = _read_exact(self.process.stdout, _RESPONSE.size)
        magic, protocol, status, reserved, count, actual = _RESPONSE.unpack(raw)
        if (magic != b"AQ8T" or protocol != PROTOCOL or reserved != 0
                or actual != self.generation):
            raise RuntimeError("q8 scanner returned an invalid candidate header")
        if status != 0 or count > min(k * heads, self.rows):
            raise RuntimeError(f"q8 scanner refused generation or query ({status})")
        records = np.frombuffer(
            _read_exact(self.process.stdout, int(count) * _CANDIDATE.itemsize),
            dtype=_CANDIDATE)
        ordinals = records["ordinal"].copy()
        scores = records["score"].copy()
        if (len(set(map(int, ordinals))) != len(ordinals)
                or np.any(ordinals >= self.rows)
                or not np.all(np.isfinite(scores))
                or np.any(scores[1:] > scores[:-1])
                or np.any((scores[1:] == scores[:-1])
                          & (ordinals[1:] < ordinals[:-1]))):
            raise RuntimeError("q8 scanner returned invalid candidates")
        if eligibility is not None and len(ordinals):
            selected = eligibility[ordinals // 8]
            masks = np.left_shift(
                np.uint8(1), np.asarray(ordinals % 8, dtype=np.uint8))
            if np.any(selected & masks == 0):
                raise RuntimeError("q8 scanner returned an ineligible candidate")
        return ordinals, scores

    def _query(self, query: np.ndarray) -> np.ndarray:
        vector = np.asarray(query, dtype="<f4").reshape(-1)
        if len(vector) != self.dim or not np.all(np.isfinite(vector)):
            raise ValueError("q8 query does not match the artifact dimension")
        return vector

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self.process = None


def _scanner_identity(manifest: dict) -> tuple:
    set_manifest = manifest.get("set_manifest_path")
    if set_manifest is not None:
        segments = tuple(
            (int(segment["row_base"]), int(segment["rows"]),
             str(segment["artifact_path"]), str(segment["group_artifact_path"]))
            for segment in manifest["segments"])
        return ("set", str(set_manifest), _artifact_generation(manifest),
                int(manifest["rows"]), int(manifest["dim"]), segments)
    return (
        "legacy", str(manifest["artifact_path"]), _artifact_generation(manifest),
        str(manifest.get("group_artifact_path") or ""),
        int(manifest["rows"]), int(manifest["dim"]),
    )


class _SegmentSidecars:
    def __init__(self, manifest: dict):
        self.items = []
        dim = int(manifest["dim"])
        global_groups = int(manifest["group_count"])
        for segment in manifest["segments"]:
            rows = int(segment["rows"])
            group_path = segment["group_artifact_path"]
            with group_path.open("rb") as stream:
                raw = stream.read(_GROUP_HEADER.size)
            if len(raw) != _GROUP_HEADER.size:
                self.close()
                raise RuntimeError("q8 segment group header is truncated")
            (magic, version, flags, header_count, header_rows, _, stride,
             reserved, _, tail) = _GROUP_HEADER.unpack(raw)
            if (magic != b"AGQG" or version != VERSION or flags != 0
                    or header_count <= 0 or header_count > global_groups
                    or header_rows != rows or stride != 4 or reserved != 0
                    or tail != b"\0" * 8):
                self.close()
                raise RuntimeError("q8 segment group header is invalid")
            try:
                exact = np.memmap(
                    segment["exact_artifact_path"], dtype="<f2", mode="r",
                    shape=(rows, dim))
            except Exception:
                self.close()
                raise
            try:
                groups = np.memmap(
                    group_path, dtype="<u4", mode="r", offset=_GROUP_HEADER.size,
                    shape=(rows,))
            except Exception:
                mapping = getattr(exact, "_mmap", None)
                if mapping is not None:
                    mapping.close()
                self.close()
                raise
            self.items.append({
                "row_base": int(segment["row_base"]),
                "rows": rows,
                "exact": exact,
                "groups": groups,
            })

    def gather(self, ordinals: np.ndarray, vector: np.ndarray,
               group_count: int) -> tuple[np.ndarray, np.ndarray]:
        scores = np.empty(len(ordinals), dtype=np.float32)
        groups = np.empty(len(ordinals), dtype=np.uint32)
        covered = np.zeros(len(ordinals), dtype=np.bool_)
        for segment in self.items:
            start = segment["row_base"]
            stop = start + segment["rows"]
            mask = (ordinals >= start) & (ordinals < stop)
            if not np.any(mask):
                continue
            local = np.asarray(ordinals[mask] - start, dtype=np.intp)
            vectors = np.asarray(segment["exact"][local], dtype=np.float32)
            scores[mask] = np.asarray(vectors @ vector, dtype=np.float32)
            groups[mask] = np.asarray(segment["groups"][local], dtype=np.uint32)
            covered[mask] = True
        if (not np.all(covered) or not np.all(np.isfinite(scores))
                or np.any(groups >= group_count)):
            raise RuntimeError("q8 exact segment candidates are invalid")
        return scores, groups

    def close(self) -> None:
        for segment in getattr(self, "items", ()):
            for name in ("exact", "groups"):
                mapping = getattr(segment[name], "_mmap", None)
                if mapping is not None:
                    try:
                        mapping.close()
                    except (OSError, ValueError):
                        pass
        self.items = []


def _close_sidecars_locked() -> None:
    global _F16, _F16_IDENTITY, _GROUPS, _GROUPS_IDENTITY
    global _SEGMENT_SIDECARS, _SEGMENT_SIDECARS_IDENTITY
    for sidecar in (_F16, _GROUPS):
        mapping = getattr(sidecar, "_mmap", None)
        if mapping is not None:
            try:
                mapping.close()
            except (OSError, ValueError):
                pass
    _F16 = _F16_IDENTITY = None
    _GROUPS = _GROUPS_IDENTITY = None
    if _SEGMENT_SIDECARS is not None:
        _SEGMENT_SIDECARS.close()
    _SEGMENT_SIDECARS = _SEGMENT_SIDECARS_IDENTITY = None


def close_scanner() -> None:
    global _SCANNER
    with _LOCK:
        if _SCANNER is not None:
            _SCANNER.close()
            _SCANNER = None
        _close_sidecars_locked()


def _ready_manifest(generation: str) -> dict | None:
    if not generation:
        return None
    commit = _current_commit()
    if str(commit.get("generation") or "") != generation:
        return None
    return _validated_manifest({"commit": commit})


def _scanner_for_manifest(manifest: dict) -> _Q8Scanner:
    global _SCANNER
    identity = _scanner_identity(manifest)
    if _SCANNER is None or _SCANNER.identity != identity:
        if _SCANNER is not None:
            _SCANNER.close()
        _close_sidecars_locked()
        _SCANNER = _Q8Scanner(manifest)
    return _SCANNER


def _segment_sidecars_for_manifest(manifest: dict) -> _SegmentSidecars:
    global _SEGMENT_SIDECARS, _SEGMENT_SIDECARS_IDENTITY
    identity = (
        _artifact_generation(manifest), int(manifest["dim"]),
        int(manifest["group_count"]),
        tuple((int(segment["row_base"]), int(segment["rows"]),
               str(segment["exact_artifact_path"]),
               int(segment["exact_artifact_size"]),
               str(segment["group_artifact_path"]),
               int(segment["group_artifact_size"]))
              for segment in manifest["segments"]),
    )
    if (_SEGMENT_SIDECARS is None
            or _SEGMENT_SIDECARS_IDENTITY != identity):
        _close_sidecars_locked()
        _SEGMENT_SIDECARS = _SegmentSidecars(manifest)
        _SEGMENT_SIDECARS_IDENTITY = identity
    return _SEGMENT_SIDECARS


def _f16_for_manifest(manifest: dict):
    global _F16, _F16_IDENTITY
    identity = (
        str(manifest["exact_artifact_path"]), _artifact_generation(manifest),
        int(manifest["rows"]), int(manifest["dim"]),
        int(manifest["exact_artifact_size"]),
    )
    if _F16 is None or _F16_IDENTITY != identity:
        if _F16 is not None:
            mapping = getattr(_F16, "_mmap", None)
            if mapping is not None:
                mapping.close()
        _F16 = np.memmap(
            manifest["exact_artifact_path"], dtype="<f2", mode="r",
            shape=(int(manifest["rows"]), int(manifest["dim"])))
        _F16_IDENTITY = identity
    return _F16


def _groups_for_manifest(manifest: dict):
    global _GROUPS, _GROUPS_IDENTITY
    path = manifest["group_artifact_path"]
    rows = int(manifest["rows"])
    count = int(manifest["group_count"])
    identity = (
        str(path), _artifact_generation(manifest), rows, count,
        int(manifest["group_artifact_size"]),
    )
    if _GROUPS is None or _GROUPS_IDENTITY != identity:
        if _GROUPS is not None:
            mapping = getattr(_GROUPS, "_mmap", None)
            if mapping is not None:
                mapping.close()
        with path.open("rb") as stream:
            raw = stream.read(_GROUP_HEADER.size)
        if len(raw) != _GROUP_HEADER.size:
            raise RuntimeError("q8 group sidecar header is truncated")
        (magic, version, flags, header_count, header_rows, generation,
         stride, reserved, _, tail) = _GROUP_HEADER.unpack(raw)
        if (magic != b"AGQG" or version != VERSION or flags != 0
                or header_count != count or header_rows != rows or stride != 4
                or reserved != 0 or tail != b"\0" * 8
                or generation != _generation_bytes(_artifact_generation(manifest))):
            raise RuntimeError("q8 group sidecar header does not match its manifest")
        _GROUPS = np.memmap(
            path, dtype="<u4", mode="r", offset=_GROUP_HEADER.size,
            shape=(rows,))
        _GROUPS_IDENTITY = identity
    return _GROUPS


def artifact_available(generation: str) -> bool:
    """Whether a generation-bound native bundle is ready without deriving it."""
    try:
        ready = _ready_manifest(str(generation or "")) is not None
        if not ready:
            close_scanner()
        return ready
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError):
        close_scanner()
        return False


def accelerator_coverage(generation: str) -> dict | None:
    """Return the generation-bound vector prefix searched by the native lane."""
    try:
        manifest = _ready_manifest(str(generation or ""))
        if manifest is None:
            close_scanner()
            return None
        indexed = int(manifest.get("live_rows", manifest["rows"]))
        total = int(manifest["f32_rows"])
        return {
            "indexed": indexed, "total": total,
            "pending": total - indexed,
            "complete": bool(manifest.get("complete", indexed == total)),
        }
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError):
        close_scanner()
        return None


def _exact_candidate_scores(manifest: dict, ordinals: np.ndarray,
                            vector: np.ndarray) -> np.ndarray:
    if manifest.get("storage_version") == 2:
        scores, _ = _segment_sidecars_for_manifest(manifest).gather(
            ordinals, vector, int(manifest["group_count"]))
        return scores
    exact = _f16_for_manifest(manifest)
    vectors = np.asarray(exact[ordinals], dtype=np.float32)
    return np.asarray(vectors @ vector, dtype=np.float32)


def _exact_candidate_sidecars(manifest: dict, ordinals: np.ndarray,
                              vector: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if manifest.get("storage_version") == 2:
        return _segment_sidecars_for_manifest(manifest).gather(
            ordinals, vector, int(manifest["group_count"]))
    scores = _exact_candidate_scores(manifest, ordinals, vector)
    groups = _groups_for_manifest(manifest)
    return scores, np.asarray(groups[ordinals], dtype=np.uint32)


def eligibility_without_group(
        generation: str,
        group_id: int,
        live,
        eligible=None,
) -> PackedEligibility | None:
    """Clear one family from an existing live-row eligibility bitmap."""
    try:
        manifest = _ready_manifest(str(generation or ""))
        if manifest is None:
            return None
        rows = int(manifest["rows"])
        if live is None:
            allowed = np.ones(rows, dtype=np.bool_)
        else:
            live_rows = np.asarray(live, dtype=np.bool_).reshape(-1)
            if len(live_rows) < rows:
                raise ValueError("q8 live-row mask does not match the index")
            allowed = live_rows[:rows].copy()
        if isinstance(eligible, PackedEligibility) and eligible.rows >= rows:
            width = (rows + 7) // 8
            packed = eligible.bits[:width]
        else:
            packed = _eligibility_bits(eligible, rows)
        if packed is not None:
            base = np.unpackbits(packed, bitorder="little")[:rows]
            allowed &= np.asarray(base, dtype=np.bool_)
        group_id = int(group_id)
        group_count = int(manifest["group_count"])
        if group_id < 0:
            raise ValueError("q8 excluded family is out of range")
        if group_id < group_count and manifest.get("storage_version") == 2:
            for segment in _segment_sidecars_for_manifest(manifest).items:
                start = int(segment["row_base"])
                stop = start + int(segment["rows"])
                allowed[start:stop] &= (
                    np.asarray(segment["groups"]) != group_id)
        elif group_id < group_count:
            allowed &= np.asarray(_groups_for_manifest(manifest)) != group_id
        bits = np.packbits(allowed, bitorder="little")
        result = PackedEligibility(bits, rows, int(np.count_nonzero(allowed)))
        return result if _ready_manifest(generation) is not None else None
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError):
        return None


def exact_candidates(
    query: np.ndarray,
    generation: str,
    k: int = 128,
    eligible=None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Generation-safe flat q8 candidates reranked from immutable f16."""
    try:
        manifest = _ready_manifest(str(generation or ""))
        if manifest is None:
            close_scanner()
            return None
        vector = np.asarray(query, dtype=np.float32).reshape(-1)
        with _LOCK:
            scanner = _scanner_for_manifest(manifest)
            ordinals, _ = scanner.top(
                vector, _artifact_generation(manifest), k, eligible=eligible)
            scores = _exact_candidate_scores(manifest, ordinals, vector)
            if not np.all(np.isfinite(scores)):
                raise RuntimeError("q8 exact candidate sidecars are invalid")
            order = np.lexsort((ordinals, -scores))
            ordinals, scores = ordinals[order], scores[order]
        if _ready_manifest(generation) is None:
            close_scanner()
            return None
        return ordinals, scores
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, subprocess.SubprocessError, EOFError):
        close_scanner()
        return None


def _grouped_exact_candidates(
    query: np.ndarray,
    generation: str,
    k: int = 128,
    heads: int = 8,
    eligible=None,
    *, capture_shadow: bool = False,
):
    """Generation-safe grouped q8 candidates reranked from immutable f16.

    The optional diagnostic return reuses the exact serving scan and sidecar
    gather.  It never performs another model call or candidate scan.
    """
    try:
        manifest = _ready_manifest(str(generation or ""))
        if manifest is None:
            close_scanner()
            return None
        vector = np.asarray(query, dtype=np.float32).reshape(-1)
        with _LOCK:
            scanner = _scanner_for_manifest(manifest)
            q8_ordinals, q8_scores = scanner.top(
                vector, _artifact_generation(manifest), k,
                grouped=True, heads=heads, eligible=eligible)
            scores, group_ids = _exact_candidate_sidecars(
                manifest, q8_ordinals, vector)
            if (not np.all(np.isfinite(scores))
                    or np.any(group_ids >= int(manifest["group_count"]))):
                raise RuntimeError("q8 exact candidate sidecars are invalid")
            order = np.lexsort((q8_ordinals, -scores))
            ordinals = q8_ordinals[order]
            scores = scores[order]
            group_ids = group_ids[order]
        if _ready_manifest(generation) is None:
            close_scanner()
            return None
        result = (ordinals, scores, group_ids, int(manifest["group_count"]))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, subprocess.SubprocessError, EOFError):
        close_scanner()
        return None
    if not capture_shadow:
        return result
    try:
        diagnostic = {
            "q8_ordinals": q8_ordinals.astype(np.int64, copy=False).tolist(),
            "q8_scores": q8_scores.astype(np.float32, copy=False).tolist(),
            "f16_ordinals": ordinals.astype(np.int64, copy=False).tolist(),
            "f16_scores": scores.astype(np.float32, copy=False).tolist(),
            "f16_groups": group_ids.astype(np.uint32, copy=False).tolist(),
            "group_count": int(manifest["group_count"]),
        }
    except Exception:  # noqa: BLE001 -- observer diagnostics cannot wound serving
        diagnostic = None
    return result, diagnostic


def grouped_exact_candidates(
    query: np.ndarray,
    generation: str,
    k: int = 128,
    heads: int = 8,
    eligible=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int] | None:
    """Generation-safe grouped q8 candidates reranked from immutable f16."""
    return _grouped_exact_candidates(
        query, generation, k=k, heads=heads, eligible=eligible)


def grouped_exact_candidates_with_shadow(
    query: np.ndarray,
    generation: str,
    k: int = 128,
    heads: int = 8,
    eligible=None,
):
    """Return serving candidates plus their already-computed q8/f16 trace.

    This is an internal observer seam.  Callers must not use the trace to
    influence the serving result.
    """
    return _grouped_exact_candidates(
        query, generation, k=k, heads=heads, eligible=eligible,
        capture_shadow=True)


def shadow_scores(query: np.ndarray, matrix) -> np.ndarray | None:
    if not enabled():
        return None
    try:
        generation = str(getattr(matrix, "_agrep_commit_generation", ""))
        state = _f32_state()
        commit = state.get("commit") or {}
        if not generation or generation != str(commit.get("generation") or ""):
            return None
        manifest = _validated_manifest(state)
        if (manifest is None
                or manifest.get("storage_version") == 2
                or int(manifest["rows"]) != int(commit.get("rows", -1))):
            return None
        with _LOCK:
            scores = _scanner_for_manifest(manifest).score(
                query, _artifact_generation(manifest))
        after = _f32_state()
        if str((after.get("commit") or {}).get("generation") or "") != generation:
            close_scanner()
            return None
        return scores
    except (OSError, RuntimeError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, subprocess.SubprocessError, EOFError):
        close_scanner()
        return None


def comparison(f32_scores: np.ndarray, q8_scores: np.ndarray, depth: int = 40) -> dict:
    f32 = np.asarray(f32_scores, dtype=np.float32).reshape(-1)
    q8 = np.asarray(q8_scores, dtype=np.float32).reshape(-1)
    if len(f32) != len(q8):
        raise ValueError("q8 shadow score count does not match f32")
    if not len(f32):
        return {
            "state": "compared", "score_kind": SCORE_KIND, "rows": 0,
            "used_for_ranking": False, "top_overlap": 1.0,
            "max_abs_error": 0.0, "mean_abs_error": 0.0,
        }
    count = min(max(1, int(depth)), len(f32))
    f32_top = set(np.argpartition(f32, len(f32) - count)[-count:].tolist())
    q8_top = set(np.argpartition(q8, len(q8) - count)[-count:].tolist())
    error = np.abs(f32 - q8)
    return {
        "state": "compared",
        "score_kind": SCORE_KIND,
        "rows": len(f32),
        "used_for_ranking": False,
        "top_overlap": round(len(f32_top & q8_top) / count, 6),
        "top1_same": int(np.argmax(f32)) == int(np.argmax(q8)),
        "max_abs_error": round(float(np.max(error)), 8),
        "mean_abs_error": round(float(np.mean(error)), 8),
    }


atexit.register(close_scanner)
