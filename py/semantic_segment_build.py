"""Build one immutable semantic delta before its manifest publication."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

import common
import semantic_q8

REFS_METADATA_VERSION = 3


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


class PreparedSegment:
    def __init__(self, root: Path, artifacts: dict[str, Path], refs: list[dict]):
        self.root = root
        self.artifacts = artifacts
        self.refs = refs

    def close(self) -> None:
        if not _data_dir_readonly():
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _catalog_path() -> Path:
    return common.DATA_DIR / ".semantic-family-catalog.sqlite"


def _family_label(message, parents: dict, memo: dict[str, str]) -> str | None:
    session = str(message.session or "")
    family = common.family_root(session, parents, memo) if session else ""
    return _family_label_for_root(message, family)


def _family_label_for_root(message, family: str) -> str | None:
    if (str(message.who or "").lower()
            in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES):
        return None
    if not family or "\n" in family or "\r" in family:
        raise RuntimeError("semantic message has no valid conversation family")
    return "f:" + family


def refs_metadata_fingerprint(
        message, family_label: str | None, side: bool = False,
) -> str:
    """Versioned identity of query-visible metadata, independent of vector text."""
    payload = json.dumps([
        REFS_METADATA_VERSION,
        str(message.agent or ""),
        str(message.project or ""),
        str(message.session or ""),
        int(message.ts or 0),
        int(message.turn or 0),
        str(message.who or "user"),
        str(message.model or ""),
        str(message.model_source or "unknown"),
        family_label,
        bool(side),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def message_metadata_fingerprint(
        message, parents: Mapping[str, str], memo: dict[str, str],
) -> str:
    return refs_metadata_fingerprint(
        message, _family_label(message, parents, memo),
        str(message.session or "") in parents)


def _seed_catalog(
        connection: sqlite3.Connection,
        parents: Mapping[str, str] | None = None, *,
        allow_legacy_reset: bool = False,
) -> bool:
    count = int(connection.execute("SELECT count(*) FROM families").fetchone()[0])
    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if count or not meta.exists():
        return False
    try:
        import embedding_segments
        manifest = embedding_segments.load_manifest(meta)
        rows = embedding_segments.iter_active_rows(manifest)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    memo: dict[str, str] = {}
    by_label: dict[str, int] = {}
    by_id: dict[int, str] = {}
    saw_legacy = False
    conflict = False
    for row in rows:
        family_id = int(row["family_id"])
        if family_id == 0:
            continue
        label = row.get("family_label")
        if label is None:
            saw_legacy = True
            if parents is None:
                parents = common.await_family_publication(
                    common.strict_family_parent_map)
                if parents is None:
                    raise RuntimeError(
                        "session-family publication is unavailable")
            session = str(row.get("session") or "")
            family = common.family_root(session, parents, memo) if session else ""
            label = "f:" + family if family else ""
        if (not label or by_label.get(label, family_id) != family_id
                or by_id.get(family_id, label) != label):
            conflict = True
            break
        by_label[label] = family_id
        by_id[family_id] = label
    if conflict:
        if allow_legacy_reset and saw_legacy:
            return True
        raise RuntimeError(
            "semantic family namespace cannot be reconstructed safely")
    connection.executemany(
        "INSERT INTO families(label,id) VALUES(?,?)",
        sorted(by_label.items(), key=lambda item: item[1]))
    return False


def _family_ids(
        messages: Sequence, *, allow_legacy_reset: bool = False,
        _include_sides: bool = False,
):
    _require_writable_data_dir("update the semantic family catalog")
    path = _catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS families(
                label TEXT PRIMARY KEY, id INTEGER NOT NULL UNIQUE);
        """)
        connection.execute("BEGIN IMMEDIATE")
        reset = _seed_catalog(
            connection, allow_legacy_reset=allow_legacy_reset)
        if reset:
            common.log("reset legacy semantic family ids during complete ref replacement")
        sessions = {
            str(message.session or "")
            for message in messages
            if (str(message.who or "").lower()
                not in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)
        }
        if _include_sides:
            metadata = common.indexed_family_metadata(sessions)
        else:
            roots = common.indexed_family_roots(sessions)
            metadata = (
                None if roots is None else
                {session: (root, False) for session, root in roots.items()})
        if (metadata is None
                and not (common.DATA_DIR / "sessions.jsonl").exists()
                and not (common.DATA_DIR
                         / common.SESSION_FAMILY_META_FILE).exists()):
            metadata = {session: (session, False) for session in sessions}
        if metadata is None:
            full_parents = common.await_family_publication(
                common.strict_family_parent_map)
            if full_parents is not None:
                memo: dict[str, str] = {}
                metadata = {
                    session: (
                        common.family_root(session, full_parents, memo),
                        session in full_parents,
                    )
                    for session in sessions
                }
        if metadata is None:
            raise RuntimeError("session-family publication is unavailable")
        parents = {session: root for session, (root, _) in metadata.items()}
        memo: dict[str, str] = {}
        labels = [_family_label(message, parents, memo) for message in messages]
        wanted = sorted({label for label in labels if label is not None})
        existing: dict[str, int] = {}
        if wanted:
            for start in range(0, len(wanted), 500):
                page = wanted[start:start + 500]
                marks = ",".join("?" for _ in page)
                existing.update(connection.execute(
                    f"SELECT label,id FROM families WHERE label IN ({marks})", page))
        next_id = int(connection.execute(
            "SELECT coalesce(max(id),0)+1 FROM families").fetchone()[0])
        for label in wanted:
            if label in existing:
                continue
            if next_id > 0xFFFFFFFF:
                raise RuntimeError("semantic family namespace exceeds u32")
            connection.execute(
                "INSERT INTO families(label,id) VALUES(?,?)", (label, next_id))
            existing[label] = next_id
            next_id += 1
        connection.commit()
        result = (
            [0 if label is None else existing[label] for label in labels],
            labels,
        )
        if not _include_sides:
            return result
        return (*result, [
            bool(metadata.get(str(message.session or ""), ("", False))[1])
            for message in messages
        ])
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _write_matrix(path: Path, parts: Sequence[np.ndarray], dim: int) -> int:
    _require_writable_data_dir("write a semantic segment matrix")
    rows = 0
    with path.open("wb") as stream:
        for part in parts:
            array = np.ascontiguousarray(part, dtype="<f4")
            if array.ndim != 2 or array.shape[1] != dim:
                raise ValueError(f"embedding segment part must be (N, {dim})")
            array.tofile(stream)
            rows += int(array.shape[0])
        stream.flush()
    return rows


def prepare(parts: Sequence[np.ndarray], ids: Sequence[str], hashes: Sequence[str],
            messages: Sequence, *, dim: int, model_id: str,
            allow_legacy_family_reset: bool = False) -> PreparedSegment:
    """Build f32/q8/f16/group artifacts without touching the live manifest."""
    _require_writable_data_dir("prepare a semantic segment")
    if not ids or len(ids) != len(hashes) or len(ids) != len(messages):
        raise ValueError("semantic segment rows, ids, hashes, and refs are misaligned")
    if any(message.id != mid for message, mid in zip(messages, ids, strict=True)):
        raise ValueError("semantic segment message order does not match its ids")
    root = Path(tempfile.mkdtemp(prefix=".semantic-segment-", dir=common.DATA_DIR))
    try:
        matrix = root / "segment.f32"
        rows = _write_matrix(matrix, parts, int(dim))
        if rows != len(ids):
            raise ValueError(f"semantic segment has {rows} vectors for {len(ids)} ids")
        generation = uuid.uuid4().hex
        meta = root / "segment.meta"
        meta.write_text(json.dumps({
            "dim": int(dim), "model": str(model_id),
            "commit": {"version": 1, "generation": generation,
                       "rows": rows, "matrix": {"size": matrix.stat().st_size}},
        }, separators=(",", ":")), encoding="utf-8")
        family_ids, family_labels, side_flags = _family_ids(
            messages, allow_legacy_reset=allow_legacy_family_reset,
            _include_sides=True)
        groups = root / "groups.ids"
        groups.write_text("".join(f"{value}\n" for value in family_ids),
                          encoding="utf-8", newline="\n")
        built = semantic_q8.build_from_f32(
            matrix, meta, root, groups_path=groups, numeric_groups=True)
        exact = semantic_q8._build_f16(
            matrix, root, generation=generation, rows=rows, dim=int(dim))
        refs = [{
            "mid": mid, "text_hash": text_hash,
            "agent": str(message.agent or ""),
            "project": str(message.project or ""),
            "session": str(message.session or ""),
            "ts": int(message.ts or 0), "turn": int(message.turn or 0),
            "who": str(message.who or "user"),
            "model": str(message.model or "") or None,
            "model_source": str(message.model_source or "unknown"),
            "family_id": family_id,
            "family_label": family_label,
            "side": bool(side),
            "metadata_hash": refs_metadata_fingerprint(
                message, family_label, side),
        } for mid, text_hash, message, family_id, family_label, side in zip(
            ids, hashes, messages, family_ids, family_labels, side_flags,
            strict=True)]
        artifacts = {
            "f32": matrix,
            "q8": Path(str(built["artifact"])),
            "groups": Path(str(built["group_artifact"])),
            "f16": Path(exact["exact_artifact"]),
        }
        return PreparedSegment(root, artifacts, refs)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
