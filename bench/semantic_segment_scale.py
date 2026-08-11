#!/usr/bin/env python3
"""Bound segmented top-up publication against a logical 10M-row base."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import common  # noqa: E402
import embedding_segments as segments  # noqa: E402
import semantic_segment_build  # noqa: E402


DEFAULT_BASE_ROWS = 10_000_000
DEFAULT_TOPUP_ROWS = 1_000
DEFAULT_DIM = 384
MIB = 1024 * 1024
BUDGETS = {"publish_s": 10.0, "new_artifact_bytes": 100 * MIB}
WINDOWS = os.name == "nt"
WINDOWS_MAX_BASE_ROWS = 100_000


def _descriptor(path: Path, *, digest: str | None = None) -> dict:
    return {
        "path": f"{segments.SEGMENT_DIR}/{path.name}",
        "size": path.stat().st_size,
        "sha256": digest or hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _attach_publication_proof(meta: Path, record: dict) -> None:
    staged = segments.LoadedManifest(record, meta)
    identities = segments._prefix_identities(staged)
    descriptors = segments._proof_descriptors(record)
    payload = segments._canonical({
        "version": segments.PROOF_VERSION,
        "generation": record["generation"],
        "manifest_sha256": segments._proof_binding(record),
        "artifacts": [{
            "path": relative,
            "size": descriptor["size"],
            "sha256": descriptor["sha256"],
            "identity": list(identities[segments.artifact_path(staged, relative)]),
        } for relative, descriptor in sorted(descriptors.items())],
    })
    path = meta.parent / segments.SEGMENT_DIR / f"proof.{record['generation']}.json"
    path.write_bytes(payload)
    record[segments.PROOF_KEY] = _descriptor(path)


def _write_sparse(path: Path, size: int, header: bytes = b"") -> None:
    with path.open("wb") as stream:
        stream.write(header)
        stream.truncate(size)
        stream.flush()
        os.fsync(stream.fileno())


def _refs(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE refs(
                local_ord INTEGER PRIMARY KEY, row_ref INTEGER NOT NULL,
                mid TEXT NOT NULL, text_hash TEXT NOT NULL, agent TEXT NOT NULL,
                project TEXT NOT NULL, session TEXT NOT NULL, ts INTEGER NOT NULL,
                turn INTEGER NOT NULL, who TEXT NOT NULL, model TEXT,
                family_id INTEGER NOT NULL);
            CREATE UNIQUE INDEX refs_row_ref ON refs(row_ref);
            CREATE UNIQUE INDEX refs_mid ON refs(mid);
            CREATE INDEX refs_session ON refs(session,turn);
        """)
        connection.executemany("INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        connection.commit()
    finally:
        connection.close()


def _header(magic: bytes, generation: bytes, rows: int, dim: int,
            *, group_count: int = 0, checksum: int = 0) -> bytes:
    output = bytearray(64)
    if magic == b"AGQ8":
        struct.pack_into(
            "<4sIIIQ16sIIQ", output, 0, magic, 1, dim, 1, rows,
            generation, dim + 4, 0, checksum)
    else:
        struct.pack_into(
            "<4sIIIQ16sIIQ", output, 0, magic, 1, 0, group_count, rows,
            generation, 4, 0, checksum)
    return bytes(output)


def _sparse_base(root: Path, rows: int, dim: int) -> tuple[Path, object]:
    directory = root / segments.SEGMENT_DIR
    directory.mkdir(parents=True)
    generation = "01" * 16
    raw_generation = bytes.fromhex(generation)
    suffixes = {
        "f32": "f32", "q8": "q8", "f16": "f16", "groups": "q8g",
        "ids": "ids", "hashes": "hashes", "refs": "refs.sqlite",
    }
    paths = {key: directory / f"seg-{generation}.{suffix}"
             for key, suffix in suffixes.items()}
    _write_sparse(paths["f32"], rows * dim * 4)
    _write_sparse(
        paths["q8"], 64 + rows * (dim + 4),
        _header(b"AGQ8", raw_generation, rows, dim))
    _write_sparse(paths["f16"], rows * dim * 2)
    _write_sparse(
        paths["groups"], 64 + rows * 4,
        _header(b"AGQG", raw_generation, rows, dim, group_count=2))
    paths["ids"].write_text("base-first\nbase-last\n", encoding="utf-8")
    paths["hashes"].write_text("0" * 16 + "\n" + "1" * 16 + "\n", encoding="utf-8")
    edge_rows = [
        (0, 0, "base-first", "0" * 16, "codex", "p", "base-first",
         0, 0, "user", None, 1),
        (rows - 1, rows - 1, "base-last", "1" * 16, "codex", "p",
         "base-last", rows - 1, rows - 1, "user", None, 1),
    ]
    _refs(paths["refs"], edge_rows)
    artifacts = {}
    for key, path in paths.items():
        digest = None
        if key in {"f32", "q8", "f16", "groups"}:
            digest = hashlib.sha256(
                f"trusted-sparse:{key}:{path.stat().st_size}".encode()).hexdigest()
        artifacts[key] = _descriptor(path, digest=digest)
    record = {
        "version": segments.VERSION,
        "generation": generation,
        "published_at": 1.0,
        "source": {"fixture": "logical-10m-base"},
        "model": {"id": "segment-scale", "dim": dim},
        "coverage": {
            "indexed": rows, "total": rows, "pending": 0,
            "complete": True, "order": "newest-first",
        },
        "segments": [{
            "id": generation, "kind": "base", "row_base": 0,
            "rows": rows, "artifacts": artifacts,
        }],
        "shadows": [], "group_count": 2, "live_rows": rows,
        "physical_rows": rows, "next_row_ref": rows, "delta_count": 0,
        "set_manifest": {},
    }
    set_path = directory / f"set.{generation}.json"
    set_payload = segments._canonical(segments._native_set(record))
    set_path.write_bytes(set_payload)
    record["set_manifest"] = _descriptor(set_path)
    meta = root / "embeddings.meta"
    _attach_publication_proof(meta, record)
    meta.write_bytes(segments._canonical(record))
    return meta, segments.load_manifest(meta, retries=1)


def _prepare_delta(root: Path, rows: int, dim: int):
    rng = np.random.default_rng(71)
    matrix = rng.normal(size=(rows, dim)).astype("<f4")
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    ids = [f"scale:delta-{index:08d}:0" for index in range(rows)]
    hashes = [hashlib.blake2b(mid.encode(), digest_size=8).hexdigest() for mid in ids]
    messages = [common.Message(
        id=mid, agent="codex", project="p", session=f"delta-{index // 4}",
        ts=index, turn=index, text=f"scale row {index}", who="user",
        model="", model_source="unknown")
        for index, mid in enumerate(ids)]
    isolated_embeddings = root / "isolated" / "embeddings.f32"
    with (mock.patch.object(common, "DATA_DIR", root),
          mock.patch.object(common, "EMBEDDINGS_PATH", isolated_embeddings),
          mock.patch.object(common, "strict_family_parent_map", return_value={})):
        prepared = semantic_segment_build.prepare(
            [matrix], ids, hashes, messages, dim=dim, model_id="segment-scale")
    return prepared, ids, hashes


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
            int(stat.st_mtime_ns), int(stat.st_ctime_ns))


def _allocated(path: Path) -> int:
    stat = path.stat()
    blocks = getattr(stat, "st_blocks", None)
    return stat.st_size if blocks is None else int(blocks) * 512


def run_case(root: Path, *, base_rows: int, topup_rows: int, dim: int) -> dict:
    if WINDOWS and base_rows > WINDOWS_MAX_BASE_ROWS:
        raise RuntimeError(
            "Windows scale fixtures above 100000 rows require explicit NTFS sparse-file support")
    meta, base = _sparse_base(root, base_rows, dim)
    base_paths = segments.referenced_paths(base)
    base_identities = {path: _identity(path) for path in base_paths}
    before_files = set((root / segments.SEGMENT_DIR).iterdir())
    started = time.perf_counter()
    prepared, ids, hashes = _prepare_delta(root, topup_rows, dim)
    try:
        published = segments.publish_delta(
            meta, source={"fixture": "logical-10m-plus-1k"},
            artifacts=prepared.artifacts,
            ids=ids, hashes=hashes, refs=prepared.refs,
            coverage={"total": base_rows + topup_rows},
            expected_generation=base["generation"])
    finally:
        prepared.close()
    publish_s = time.perf_counter() - started

    after_files = set((root / segments.SEGMENT_DIR).iterdir())
    new_files = sorted(after_files - before_files)
    fresh = segments.load_manifest(meta, retries=1)
    base_immutable = all(_identity(path) == identity
                         for path, identity in base_identities.items())
    coherent = (
        fresh["generation"] == published["generation"]
        and fresh["live_rows"] == base_rows + topup_rows
        and fresh["physical_rows"] == base_rows + topup_rows
        and fresh["coverage"]["complete"]
        and len(fresh["segments"]) == 2
        and fresh["segments"][-1]["rows"] == topup_rows
    )
    base_publication_proven = segments.publication_artifacts_still_bound(base)
    temp_leftovers = sorted(
        str(path.relative_to(root)) for path in root.rglob("*.tmp"))
    new_bytes = sum(path.stat().st_size for path in new_files) + meta.stat().st_size
    failures = []
    if publish_s >= BUDGETS["publish_s"]:
        failures.append("publication wall")
    if new_bytes >= BUDGETS["new_artifact_bytes"]:
        failures.append("new artifact bytes")
    if not base_immutable:
        failures.append("base mutation")
    if not coherent:
        failures.append("fresh open coherence")
    if temp_leftovers:
        failures.append("temporary leftovers")
    return {
        "base_rows": base_rows, "topup_rows": topup_rows, "dim": dim,
        "publish_s": round(publish_s, 6),
        "new_artifact_bytes": new_bytes,
        "new_artifact_mib": round(new_bytes / MIB, 3),
        "new_files": [path.name for path in new_files],
        "base_logical_gib": round(sum(path.stat().st_size for path in base_paths) / 2**30, 3),
        "base_allocated_mib": round(sum(_allocated(path) for path in base_paths) / MIB, 3),
        "base_immutable": base_immutable, "fresh_open_coherent": coherent,
        "base_publication_proven": base_publication_proven,
        "temp_leftovers": temp_leftovers, "budgets": BUDGETS,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-rows", type=int, default=DEFAULT_BASE_ROWS)
    parser.add_argument("--topup-rows", type=int, default=DEFAULT_TOPUP_ROWS)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.base_rows < 2 or args.topup_rows <= 0 or args.dim <= 0:
        parser.error("base rows must be at least two; top-up rows and dimension must be positive")
    with tempfile.TemporaryDirectory(prefix="agrep-semantic-segment-scale-") as raw:
        report = run_case(
            Path(raw), base_rows=args.base_rows,
            topup_rows=args.topup_rows, dim=args.dim)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['base_rows']:,} + {report['topup_rows']:,} rows · "
              f"publish {report['publish_s']:.3f}s · "
              f"new artifacts {report['new_artifact_mib']:.3f} MiB")
        print(f"sparse base {report['base_logical_gib']:.3f} GiB logical / "
              f"{report['base_allocated_mib']:.3f} MiB allocated")
        print("semantic-segment gate: " + (
            "FAIL " + ", ".join(report["failures"])
            if report["failures"] else "PASS"))
    return 1 if args.check and report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
