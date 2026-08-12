"""Governed compaction for immutable semantic vector segments."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import common
import embedding_segments
import indexd_runtime
import ownerfile
import removal_fence
import semantic_q8
import semantic_segment_build


class CompactionDeferred(RuntimeError):
    pass


_CLAIM_HANDLE: ownerfile.Handle | None = None
_MALFORMED_CLAIM_STALE_S = 30.0


def _data_dir_readonly() -> bool:
    return _mutation_refusal_reason() is not None


def _mutation_refusal_info():
    if common.data_dir_readonly(common.DATA_DIR):
        return indexd_runtime.DerivedMutationInfo(
            "readonly", None,
            "AGREP_DATA_READONLY protects the data directory")
    return indexd_runtime.derived_writer_mutation_settled()


def _mutation_refusal_reason() -> str | None:
    info = _mutation_refusal_info()
    return None if info.writable else info.reason


def _require_writable_data_dir(action: str) -> None:
    info = _mutation_refusal_info()
    if info.writable:
        return
    if info.journal_blocked:
        raise indexd_runtime.DerivedWriteContended(
            f"{info.reason}; cannot {action}")
    raise PermissionError(f"{info.reason}; cannot {action}")


def _claim_path() -> Path:
    return common.DATA_DIR / ".semantic-compaction.lock"


def _acquire_claim() -> bool:
    global _CLAIM_HANDLE
    if _data_dir_readonly():
        return False
    if _CLAIM_HANDLE is not None:
        return False
    if removal_fence.background_removal_active():
        return False
    path = _claim_path()
    process_start = common.process_start_identity(os.getpid())
    if process_start in (None, "", "None", "unknown"):
        return False
    raw = json.dumps({
        "pid": os.getpid(),
        "token": uuid.uuid4().hex,
        "process_start": process_start,
    }).encode("utf-8")
    for _ in range(2):
        try:
            claimed = ownerfile.create_exclusive(path, raw)
            if removal_fence.background_removal_active():
                claimed.release(tombstone=True, require_stable_mtime=True)
                return False
            _CLAIM_HANDLE = claimed
            return True
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path)
            except OSError:
                continue
            try:
                record = json.loads(observed.raw.decode("utf-8"))
                if not isinstance(record, dict):
                    raise TypeError("claim record is not an object")
                pid = int(record.get("pid") or 0)
                state = ownerfile.classify_process(
                    pid, record.get("process_start"),
                    pid_alive=common.pid_alive,
                    process_start=common.process_start_identity)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                age = time.time() - observed.mtime
                if 0.0 <= age <= _MALFORMED_CLAIM_STALE_S:
                    return False
            else:
                if state in (
                        ownerfile.ProcessOwner.EXACT_LIVE,
                        ownerfile.ProcessOwner.UNVERIFIABLE):
                    return False
            if ownerfile.remove_exact(path, observed, tombstone=True):
                continue
            return False
        except OSError:
            return False
    return False


def _release_claim() -> None:
    global _CLAIM_HANDLE
    handle = _CLAIM_HANDLE
    _CLAIM_HANDLE = None
    if handle is None:
        return
    if _data_dir_readonly():
        handle.close()
        return
    try:
        handle.release(tombstone=True)
    except OSError:
        pass


def _governor_reason() -> str | None:
    import embed
    return embed._governor_deferral()


def _require_capacity(governor) -> None:
    reason = governor()
    if reason:
        raise CompactionDeferred(str(reason))


def _require_disk_capacity(manifest, root: Path) -> None:
    live = int(manifest["live_rows"])
    dim = int(manifest["model"]["dim"])
    sidecar_bytes = sum(
        int(segment["artifacts"][key]["size"])
        for segment in manifest["segments"]
        for key in ("ids", "hashes", "refs"))
    estimate = live * (7 * dim + 8) + sidecar_bytes
    required = int(estimate * 1.1) + 64 * 1024 * 1024
    free = int(shutil.disk_usage(root).free)
    if free < required:
        raise CompactionDeferred(
            f"insufficient disk for semantic compaction ({free} free, {required} required)")


def _create_refs(path: Path) -> sqlite3.Connection:
    _require_writable_data_dir("build compacted semantic refs")
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
        """)
        embedding_segments._create_refs_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _flush_vectors(output, matrix, ordinals: list[int]) -> None:
    if not ordinals:
        return
    block = np.asarray(matrix[np.asarray(ordinals, dtype=np.int64)], dtype="<f4")
    block.tofile(output)
    ordinals.clear()


def _current_metadata_map(db, root: Path) -> Path:
    _require_writable_data_dir("build compacted semantic metadata")
    path = root / "current-metadata.sqlite"
    output = sqlite3.connect(path)
    try:
        output.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE rows(
                agent TEXT NOT NULL, session TEXT NOT NULL,
                turn INTEGER NOT NULL, who TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                ts INTEGER NOT NULL, project TEXT NOT NULL, model TEXT NOT NULL,
                model_source TEXT NOT NULL, ambiguous INTEGER NOT NULL,
                PRIMARY KEY(agent,session,turn,who,text_hash)
            ) WITHOUT ROWID;
            CREATE TABLE families(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))
            ) WITHOUT ROWID;
        """)
        insert = """
            INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,0)
            ON CONFLICT(agent,session,turn,who,text_hash)
            DO UPDATE SET ambiguous=1
        """
        buffer: list[tuple] = []
        source = db.execute(
            "SELECT session,agent,turn,who,text,ts,project,model,model_source "
            "FROM msgs WHERE who <> 'tool'")
        for session, agent, turn, who, text, ts, project, model, model_source in source:
            buffer.append((
                str(agent or ""), str(session or ""), int(turn or 0),
                str(who or ""), common.semantic_text_hash(str(text or "")),
                int(ts or 0),
                str(project or ""), str(model or ""),
                str(model_source or "unknown"),
            ))
            if len(buffer) >= 4096:
                output.executemany(insert, buffer)
                buffer.clear()
        if buffer:
            output.executemany(insert, buffer)
        try:
            family_columns = {
                str(row[1]) for row in db.execute(
                    "PRAGMA table_info(session_family)")
            }
            if {"session", "root", "side"}.issubset(family_columns):
                families = (
                    (str(session), str(family), int(bool(side)))
                    for session, family, side in db.execute(
                        "SELECT session,root,side FROM session_family")
                )
            elif {"session", "root"}.issubset(family_columns):
                families = (
                    (str(session), str(family),
                     int(common.is_sidechain_session(str(session))))
                    for session, family in db.execute(
                        "SELECT session,root FROM session_family")
                )
            else:
                raise sqlite3.DatabaseError("session_family schema is unavailable")
            output.executemany("INSERT INTO families VALUES(?,?,?)", families)
        except sqlite3.DatabaseError:
            output.execute("DELETE FROM families")
            parents = common.await_family_publication(
                common.strict_family_parent_map)
            if parents is None:
                raise embedding_segments.SegmentError(
                    "session-family publication is unavailable")
            memo: dict[str, str] = {}
            output.executemany(
                "INSERT INTO families VALUES(?,?,?)",
                (
                    (
                        str(session),
                        common.family_root(str(session), parents, memo),
                        int(str(session) in parents),
                    )
                    for session, in output.execute(
                        "SELECT DISTINCT session FROM rows")
                ),
            )
        output.commit()
    finally:
        output.close()
    return path


class _MetadataMerge:
    def __init__(self, metadata_path: Path | None):
        self.path = metadata_path
        self.parents = None
        self.memo: dict[str, str] = {}
        self.family_by_label: dict[str, int] = {}
        self.label_by_family: dict[int, str] = {}
        self.current = True

    def attach(self, connection: sqlite3.Connection) -> None:
        if self.path is None:
            return
        uri = self.path.resolve().as_uri() + "?mode=ro&immutable=1"
        connection.execute(
            "ATTACH DATABASE ? AS current_meta", (uri,))

    def bind_family(self, family_id: int, label: str | None) -> None:
        if ((family_id == 0) != (label is None)
                or label is not None and (
                    self.family_by_label.get(label, family_id) != family_id
                    or self.label_by_family.get(family_id, label) != label)):
            raise embedding_segments.SegmentError(
                "semantic family namespace is split")
        if label is not None:
            self.family_by_label[label] = family_id
            self.label_by_family[family_id] = label

    def parent_map(self):
        if self.parents is None:
            self.parents = common.await_family_publication(
                common.strict_family_parent_map)
            if self.parents is None:
                raise embedding_segments.SegmentError(
                    "session-family publication is unavailable")
        return self.parents

    def merge(
        self, record, mid: str, *,
        has_metadata: bool, has_family_label: bool,
        has_model_source: bool, has_side: bool,
    ) -> tuple[str, int, str, str, str | None, str, bool]:
        offset = 12
        metadata_hash = record[offset] if has_metadata else None
        offset += int(has_metadata)
        family_label = record[offset] if has_family_label else None
        offset += int(has_family_label)
        model_source = (
            str(record[offset]) if has_model_source else "unknown")
        offset += int(has_model_source)
        side = bool(record[offset]) if has_side else False
        offset += int(has_side)
        project, ts, model = str(record[5]), int(record[7]), str(record[10] or "")
        used_current = False
        if self.path is not None:
            current = record[offset:offset + 8]
            if current[0] is None or int(current[5] or 0) != 0:
                self.current = False
            else:
                ts = int(current[1] or 0)
                project = str(current[2] or "")
                model = str(current[3] or "")
                model_source = str(current[4] or "unknown")
                side = bool(current[7])
                message = common.Message(
                    id=mid, agent=str(record[4]), project=project,
                    session=str(record[6]), ts=ts, turn=int(record[8]),
                    text="", who=str(record[9]), model=model,
                    model_source=model_source)
                family_label = semantic_segment_build._family_label_for_root(
                    message, str(current[6] or record[6]))
                metadata_hash = (
                    semantic_segment_build.refs_metadata_fingerprint(
                        message, family_label, side))
                used_current = True
        family_id = int(record[11])
        if family_id != 0 and family_label is None:
            root_session = common.family_root(
                str(record[6]), self.parent_map(), self.memo)
            family_label = "f:" + root_session
        if not used_current and not has_side:
            side = str(record[6]) in self.parent_map()
        self.bind_family(family_id, family_label)
        if not used_current and (
                metadata_hash is None or not has_model_source or not has_side):
            message = common.Message(
                id=mid, agent=str(record[4]), project=project,
                session=str(record[6]), ts=ts, turn=int(record[8]),
                text="", who=str(record[9]), model=model,
                model_source=model_source)
            metadata_hash = semantic_segment_build.refs_metadata_fingerprint(
                message, family_label, side)
        return (
            project, ts, model, str(metadata_hash),
            family_label, model_source, side)


def _ref_cursor(
        connection: sqlite3.Connection, merge: _MetadataMerge):
    has_metadata = embedding_segments._refs_have_metadata(connection)
    has_family_label = embedding_segments._refs_have_family_label(connection)
    has_model_source = embedding_segments._refs_have_model_source(connection)
    has_side = embedding_segments._refs_have_side(connection)
    columns = (
        "r.local_ord,r.row_ref,r.mid,r.text_hash,r.agent,"
        "r.project,r.session,r.ts,r.turn,r.who,r.model,r.family_id"
    )
    if has_metadata:
        columns += ",r.metadata_hash"
    if has_family_label:
        columns += ",r.family_label"
    if has_model_source:
        columns += ",r.model_source"
    if has_side:
        columns += ",r.side"
    if merge.path is not None:
        columns += (
            ",c.text_hash,c.ts,c.project,c.model,c.model_source,c.ambiguous,"
            "coalesce(f.root,r.session),coalesce(f.side,0)")
        query = (
            f"SELECT {columns} FROM refs AS r "
            "LEFT JOIN current_meta.rows AS c "
            "ON c.agent=r.agent AND c.session=r.session "
            "AND c.turn=r.turn AND c.who=r.who "
            "AND c.text_hash=r.text_hash "
            "LEFT JOIN current_meta.families AS f ON f.session=r.session "
            "ORDER BY r.local_ord"
        )
    else:
        query = f"SELECT {columns} FROM refs AS r ORDER BY r.local_ord"
    return (
        connection.execute(query),
        has_metadata, has_family_label, has_model_source, has_side)


@dataclass
class _StreamState:
    threshold: int
    next_check: int
    output_row: int = 0
    visited: int = 0


def _stream_segment(
        manifest, segment, dead: bytearray, dim: int,
        merge: _MetadataMerge, state: _StreamState, governor,
        f32_output, ids_output, hashes_output, groups_output,
        refs_output, ref_buffer: list[tuple],
) -> None:
    rows = int(segment["rows"])
    row_base = int(segment["row_base"])
    artifacts = segment["artifacts"]
    matrix_path = embedding_segments.artifact_path(
        manifest, artifacts["f32"])
    ids_source = embedding_segments.artifact_path(
        manifest, artifacts["ids"])
    hashes_source = embedding_segments.artifact_path(
        manifest, artifacts["hashes"])
    groups_source = embedding_segments.artifact_path(
        manifest, artifacts["groups"])
    refs_source = None
    matrix = None
    family_ids = None
    vector_rows: list[int] = []
    try:
        refs_source = embedding_segments._open_refs(
            embedding_segments.artifact_path(manifest, artifacts["refs"]))
        merge.attach(refs_source)
        matrix = np.memmap(
            matrix_path, dtype="<f4", mode="r", shape=(rows, dim))
        family_ids = np.memmap(
            groups_source, dtype="<u4", mode="r", offset=64, shape=(rows,))
        cursor, has_metadata, has_family_label, has_model_source, has_side = (
            _ref_cursor(refs_source, merge))
        source_rows = zip(
            cursor, embedding_segments._lines(ids_source),
            embedding_segments._lines(hashes_source), strict=True)
        for local_ord, (record, mid, text_hash) in enumerate(source_rows):
            if (record[0] != local_ord
                    or record[1] != row_base + local_ord
                    or record[2] != mid or record[3] != text_hash
                    or int(record[11]) != int(family_ids[local_ord])):
                raise embedding_segments.SegmentError(
                    "compaction input refs are misaligned")
            state.visited += 1
            if not dead[int(record[1])]:
                (project, ts, model, metadata_hash, family_label,
                 model_source, side) = merge.merge(
                    record, str(mid), has_metadata=has_metadata,
                    has_family_label=has_family_label,
                    has_model_source=has_model_source, has_side=has_side)
                family_id = int(record[11])
                vector_rows.append(local_ord)
                ids_output.write(mid + "\n")
                hashes_output.write(text_hash + "\n")
                groups_output.write(f"{family_id}\n")
                ref_buffer.append((
                    state.output_row, state.output_row, mid, text_hash,
                    record[4], project, record[6], ts, int(record[8]),
                    record[9], model, family_id, metadata_hash,
                    family_label, model_source, int(side),
                ))
                state.output_row += 1
            if len(vector_rows) >= 4096:
                _flush_vectors(f32_output, matrix, vector_rows)
            if len(ref_buffer) >= 4096:
                refs_output.executemany(
                    "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ref_buffer)
                ref_buffer.clear()
            if state.visited >= state.next_check:
                _require_capacity(governor)
                state.next_check += state.threshold
        _flush_vectors(f32_output, matrix, vector_rows)
    finally:
        if refs_source is not None:
            refs_source.close()
        if matrix is not None:
            common.close_embedding_matrix(matrix)
        if family_ids is not None:
            common.close_embedding_matrix(family_ids)


def _stream_live_base(
    manifest: embedding_segments.LoadedManifest,
    root: Path,
    *,
    governor,
    check_every_rows: int,
    metadata_path: Path | None = None,
) -> tuple[dict[str, Path], int, bool]:
    _require_writable_data_dir("stream a compacted semantic base")
    dim = int(manifest["model"]["dim"])
    dead = embedding_segments._dead_row_mask(manifest)
    f32_path = root / "base.f32"
    ids_path = root / "base.ids"
    hashes_path = root / "base.hashes"
    groups_path = root / "base.groups.ids"
    refs_path = root / "base.refs.sqlite"
    merge = _MetadataMerge(metadata_path)
    refs_output = _create_refs(refs_path)
    insert_sql = "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ref_buffer: list[tuple] = []
    threshold = max(1, int(check_every_rows))
    state = _StreamState(threshold=threshold, next_check=threshold)

    try:
        with (f32_path.open("wb") as f32_output,
              ids_path.open("w", encoding="utf-8", newline="\n") as ids_output,
              hashes_path.open("w", encoding="utf-8", newline="\n") as hashes_output,
              groups_path.open("w", encoding="utf-8", newline="\n") as groups_output):
            for segment in manifest["segments"]:
                _stream_segment(
                    manifest, segment, dead, dim, merge, state, governor,
                    f32_output, ids_output, hashes_output, groups_output,
                    refs_output, ref_buffer)
            if ref_buffer:
                refs_output.executemany(insert_sql, ref_buffer)
            if state.output_row != int(manifest["live_rows"]):
                raise embedding_segments.SegmentError(
                    "compaction output does not match manifest liveness")
            refs_output.commit()
            for stream in (f32_output, ids_output, hashes_output, groups_output):
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        refs_output.close()
    with refs_path.open("r+b") as stream:
        os.fsync(stream.fileno())
    return {
        "f32": f32_path, "ids": ids_path, "hashes": hashes_path,
        "groups_ids": groups_path, "refs": refs_path,
    }, state.output_row, merge.current


def _derive_artifacts(root: Path, f32: Path, groups: Path,
                      rows: int, dim: int) -> dict[str, Path]:
    _require_writable_data_dir("derive compacted semantic artifacts")
    generation = uuid.uuid4().hex
    meta = root / "base.meta"
    meta.write_text(json.dumps({
        "dim": int(dim), "model": "segment-compaction",
        "commit": {"version": 1, "generation": generation,
                   "rows": int(rows), "matrix": {"size": f32.stat().st_size}},
    }, separators=(",", ":")), encoding="utf-8")
    built = semantic_q8.build_from_f32(
        f32, meta, root, groups_path=groups, numeric_groups=True)
    exact = semantic_q8._build_f16(
        f32, root, generation=generation, rows=rows, dim=dim)
    return {
        "f32": f32,
        "q8": Path(str(built["artifact"])),
        "groups": Path(str(built["group_artifact"])),
        "f16": Path(str(exact["exact_artifact"])),
    }


def compact(
    meta_path: Path | None = None,
    *,
    force: bool = False,
    refresh_metadata: bool = False,
    governor=None,
    check_every_rows: int = 65_536,
    _on_stage=None,
) -> dict:
    mutation = _mutation_refusal_info()
    if not mutation.writable:
        return {
            "state": ("deferred" if mutation.journal_blocked else "read-only"),
            "reason": mutation.reason,
        }
    meta_path = Path(meta_path or (common.DATA_DIR / "embeddings.meta"))
    manifest = embedding_segments.load_manifest(meta_path)
    policy = embedding_segments.should_compact(manifest, detailed=True)
    if not force and not policy["needed"]:
        return {"state": "not-needed", **policy}
    governor = governor or _governor_reason
    metadata_db = None
    try:
        import semantic

        _require_capacity(governor)
        _require_disk_capacity(manifest, meta_path.parent)
        source = manifest["source"]
        total = int(manifest["coverage"]["total"])
        try:
            source_was_current = semantic.source_generation() == source
        except (OSError, RuntimeError, ValueError, TypeError):
            source_was_current = False
        if refresh_metadata:
            import corpusdb

            source = semantic.source_generation()
            if source is None:
                raise CompactionDeferred("transcript generation is unavailable")
            metadata_db = corpusdb.connect(quiet=True)
            if metadata_db is None:
                raise CompactionDeferred("current transcript database is unavailable")
            total = int(metadata_db.execute(
                "SELECT count(*) FROM msgs WHERE who <> 'tool'").fetchone()[0])
            if semantic.source_generation() != source:
                raise CompactionDeferred(
                    "transcript generation moved before metadata compaction")
        root = Path(tempfile.mkdtemp(
            prefix=".semantic-compaction-", dir=meta_path.parent))
        try:
            metadata_path = None
            if metadata_db is not None:
                metadata_path = _current_metadata_map(metadata_db, root)
                metadata_db.close()
                metadata_db = None
            paths, rows, metadata_current = _stream_live_base(
                manifest, root, governor=governor,
                check_every_rows=check_every_rows,
                metadata_path=metadata_path)
            _require_capacity(governor)
            bind_current = (
                refresh_metadata and metadata_current
                and semantic.source_generation() == source
            )
            if refresh_metadata and not bind_current:
                source = manifest["source"]
                total = int(manifest["coverage"]["total"])
            artifacts = _derive_artifacts(
                root, paths["f32"], paths["groups_ids"], rows,
                int(manifest["model"]["dim"]))
            _require_capacity(governor)

            def before_replace() -> None:
                _require_capacity(governor)
                if ((bind_current or source_was_current)
                        and semantic.source_generation() != source):
                    raise CompactionDeferred(
                        "transcript generation moved before semantic publication")

            def on_stage(stage: str) -> None:
                if _on_stage is not None:
                    _on_stage(stage)

            published = embedding_segments.publish_base(
                meta_path, source=source,
                model_id=str(manifest["model"]["id"]),
                dim=int(manifest["model"]["dim"]), artifacts=artifacts,
                ids=paths["ids"], hashes=paths["hashes"], refs=paths["refs"],
                coverage={"total": total,
                          "order": str(manifest["coverage"]["order"])},
                expected_generation=str(manifest["generation"]),
                _on_stage=on_stage, _before_replace=before_replace,
                _adopt_inputs=True)
            return {
                "state": "compacted", "generation": published["generation"],
                "rows": int(published["live_rows"]),
                "old_segments": len(manifest["segments"]),
                "old_deltas": int(manifest["delta_count"]),
                "metadata_refresh_requested": refresh_metadata,
                "metadata_refreshed": bind_current,
            }
        finally:
            shutil.rmtree(root, ignore_errors=True)
    except (
            CompactionDeferred,
            indexd_runtime.DerivedWriteContended,
            common.TranscriptPublicationRace,
            embedding_segments.SegmentPublicationRace,
    ) as exc:
        return {"state": "deferred", "reason": str(exc), **policy}
    finally:
        if metadata_db is not None:
            metadata_db.close()


def main(argv: list[str] | None = None) -> int:
    common.stamp_stdio_lines()
    parser = argparse.ArgumentParser(
        description="Compact immutable semantic segments", allow_abbrev=False)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--refresh-metadata", action="store_true")
    args = parser.parse_args(argv)
    mutation = _mutation_refusal_info()
    if not mutation.writable:
        if mutation.journal_blocked:
            common.log(f"semantic compaction deferred: {mutation.reason}")
            return 0
        common.log(f"semantic compaction refused: {mutation.reason}")
        return 1
    if not _acquire_claim():
        common.log("semantic compaction: already running")
        return 0
    try:
        if os.name != "nt":
            try:
                os.nice(15)
            except OSError:
                pass
        result = compact(
            force=args.force, refresh_metadata=args.refresh_metadata)
        common.log("semantic compaction: " + json.dumps(result, separators=(",", ":")))
        return 0
    finally:
        _release_claim()


if __name__ == "__main__":
    raise SystemExit(main())
