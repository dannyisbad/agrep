#!/usr/bin/env python3
"""Disposable semantic matrix scale and release benchmark.

The measured q8 lane is the production Rust mmap scanner and binary protocol.
The f32 and f16 lanes are blocked mmap oracles used for latency and recall
comparisons. No transcript, embedding model, or user data is touched.

    python bench/semantic_q8_scale.py --rows 100000 1000000 2000000
    python bench/semantic_q8_scale.py --check --rows 2000000
    python bench/semantic_q8_scale.py --quick --json
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
sys.path.insert(0, str(PY))

import common  # noqa: E402
import semantic_q8  # noqa: E402

DIM = 384
DEFAULT_ROWS = (100_000, 1_000_000, 2_000_000)
QUICK_ROWS = (10_000,)
PROJECTION_ROWS = (5_000_000, 10_000_000)
GROUP_SIZE = 4
TOP_K = 40
CANDIDATE_DEPTHS = (16, 32, 64, 128)
CANDIDATE_K = CANDIDATE_DEPTHS[-1]
CANDIDATE_HEADS = 8
MIB = 1024 * 1024
GIB = 1024 * MIB
RUST_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
_ISOLATED_DATA_ENV = "_AGREP_SEMANTIC_SCALE_DATA_DIR"

DESIGN_BUDGETS_10M = {
    "q8_artifact_gib": 3.70,
    "q8_f16_artifacts_gib": 11.0,
    "q8_scan_max_ms": 180.0,
    "q8_retrieval_max_ms": 220.0,
    "q8_process_cold_ms": 50.0,
    "q8_build_s": 45.0,
    "q8_topup_rebuild_s": 50.0,
    "q8_private_mib": 256.0,
    "f16_top40_overlap": 0.99,
    "f16_top1_rate": 1.0,
    "f16_max_abs_error": 0.002,
    "candidate_row_top8_recall": 1.0,
    "candidate_row_top1_recall": 1.0,
    "candidate_session_top8_recall": 1.0,
    "candidate_session_top1_recall": 1.0,
    "candidate_row_top40_recall": 0.95,
    "candidate_session_top40_recall": 0.95,
}
PORTABLE_MULTIPLIERS = {
    "q8_scan_max_ms": 3.0,
    "q8_retrieval_max_ms": 3.0,
    "q8_process_cold_ms": 3.0,
    "q8_build_s": 3.0,
    "q8_topup_rebuild_s": 3.0,
    "q8_private_mib": 2.0,
}


class _MacRusageV2(ctypes.Structure):
    _fields_ = (("uuid", ctypes.c_ubyte * 16), ("user_ns", ctypes.c_uint64),
                ("system_ns", ctypes.c_uint64), ("pkg_idle", ctypes.c_uint64),
                ("interrupt", ctypes.c_uint64), ("pageins", ctypes.c_uint64),
                ("wired", ctypes.c_uint64), ("resident", ctypes.c_uint64),
                ("footprint", ctypes.c_uint64), ("start_abs", ctypes.c_uint64),
                ("exit_abs", ctypes.c_uint64), ("child_user", ctypes.c_uint64),
                ("child_system", ctypes.c_uint64),
                ("child_pkg_idle", ctypes.c_uint64),
                ("child_interrupt", ctypes.c_uint64),
                ("child_pageins", ctypes.c_uint64),
                ("child_elapsed", ctypes.c_uint64),
                ("disk_read", ctypes.c_uint64), ("disk_write", ctypes.c_uint64))


def _process_memory(pid: int) -> dict[str, float | None]:
    rss = private = None
    if sys.platform == "darwin":
        try:
            fn = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int
            info = _MacRusageV2()
            if fn(pid, 2, ctypes.byref(info)) == 0:
                rss, private = int(info.resident), int(info.footprint)
        except (AttributeError, OSError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            fields = {}
            for line in Path(f"/proc/{pid}/status").read_text(
                    encoding="ascii").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key] = value.strip()
            rss = int(fields["VmRSS"].split()[0]) * 1024
            private = int(fields.get("RssAnon", "0 kB").split()[0]) * 1024
        except (KeyError, OSError, ValueError):
            pass
    elif sys.platform == "win32":
        class Counters(ctypes.Structure):
            _fields_ = (("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                        ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                        ("qpp", ctypes.c_size_t), ("qp", ctypes.c_size_t),
                        ("qnp", ctypes.c_size_t), ("qn", ctypes.c_size_t),
                        ("page", ctypes.c_size_t), ("peak_page", ctypes.c_size_t))
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            handle = k32.OpenProcess(0x0410, False, pid)
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if handle:
                try:
                    if psapi.GetProcessMemoryInfo(
                            handle, ctypes.byref(counters), counters.cb):
                        rss, private = int(counters.ws), int(counters.page)
                finally:
                    k32.CloseHandle(handle)
        except (AttributeError, OSError):
            pass
    else:
        try:
            value = subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(pid)], text=True,
                encoding="utf-8", timeout=2).strip()
            rss = int(value) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {
        "rss_mib": None if rss is None else round(rss / MIB, 3),
        "private_mib": None if private is None else round(private / MIB, 3),
    }


def _sample_memory(pid: int, action) -> tuple[object, dict[str, float | None]]:
    stop = threading.Event()
    samples = [_process_memory(pid)]

    def sample() -> None:
        while not stop.wait(0.002):
            samples.append(_process_memory(pid))

    thread = threading.Thread(target=sample, name="semantic-scale-rss", daemon=True)
    thread.start()
    try:
        result = action()
    finally:
        stop.set()
        thread.join(timeout=1)
        samples.append(_process_memory(pid))
    return result, {
        key: max((float(value[key]) for value in samples
                  if value.get(key) is not None), default=None)
        for key in ("rss_mib", "private_mib")
    }


def _seed_matrix(dim: int) -> np.ndarray:
    rng = np.random.default_rng(23)
    seed = rng.normal(size=(8192, dim)).astype("<f4")
    seed /= np.linalg.norm(seed, axis=1, keepdims=True)
    return seed


def _fill_rows(matrix, seed: np.ndarray, start: int, stop: int) -> None:
    width = len(seed)
    offset = start
    while offset < stop:
        block = offset // width
        within = offset % width
        count = min(width - within, stop - offset)
        chunk = np.roll(
            seed[within:within + count], block % seed.shape[1], axis=1)
        if block % 2:
            chunk = chunk.copy()
            chunk[:, (block * 17) % seed.shape[1]] *= -1
        matrix[offset:offset + count] = chunk
        offset += count


def _write_f32(path: Path, rows: int, dim: int) -> tuple[np.ndarray, float]:
    seed = _seed_matrix(dim)
    started = time.perf_counter()
    matrix = np.memmap(path, dtype="<f4", mode="w+", shape=(rows, dim))
    _fill_rows(matrix, seed, 0, rows)
    matrix.flush()
    del matrix
    return seed, time.perf_counter() - started


def _append_f32(path: Path, rows: int, delta: int, dim: int,
                seed: np.ndarray) -> float:
    started = time.perf_counter()
    with path.open("r+b") as handle:
        handle.truncate((rows + delta) * dim * 4)
    matrix = np.memmap(path, dtype="<f4", mode="r+", shape=(rows + delta, dim))
    _fill_rows(matrix, seed, rows, rows + delta)
    matrix.flush()
    del matrix
    return time.perf_counter() - started


def _write_f16(source: Path, target: Path, rows: int, dim: int,
               block_rows: int) -> float:
    started = time.perf_counter()
    f32 = np.memmap(source, dtype="<f4", mode="r", shape=(rows, dim))
    f16 = np.memmap(target, dtype="<f2", mode="w+", shape=(rows, dim))
    for offset in range(0, rows, block_rows):
        stop = min(rows, offset + block_rows)
        f16[offset:stop] = f32[offset:stop]
    f16.flush()
    del f16, f32
    return time.perf_counter() - started


def _blocked_scores(path: Path, dtype: str, rows: int, dim: int,
                    query: np.ndarray, block_rows: int) -> np.ndarray:
    matrix = np.memmap(path, dtype=dtype, mode="r", shape=(rows, dim))
    scores = np.empty(rows, dtype=np.float32)
    for offset in range(0, rows, block_rows):
        stop = min(rows, offset + block_rows)
        block = np.asarray(matrix[offset:stop], dtype=np.float32)
        scores[offset:stop] = block @ query
    del matrix
    return scores


def _top_indices(values: np.ndarray, count: int = TOP_K) -> np.ndarray:
    count = min(max(1, int(count)), len(values))
    if count == len(values):
        return np.argsort(-values)
    indexes = np.argpartition(-values, count - 1)[:count]
    return indexes[np.argsort(-values[indexes])]


def _adversarial_groups(rows: int) -> np.ndarray:
    ordinals = np.arange(rows, dtype=np.int64)
    groups = np.zeros(rows, dtype=np.uint32)
    normal = ordinals % GROUP_SIZE == 0
    groups[normal] = 1 + (ordinals[normal] // (GROUP_SIZE * GROUP_SIZE))
    return groups


def _write_group_labels(path: Path, rows: int) -> float:
    started = time.perf_counter()
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for offset in range(0, rows, 8192):
            stop = min(rows, offset + 8192)
            labels = [
                (f"family-{ordinal // (GROUP_SIZE * GROUP_SIZE)}\n"
                 if ordinal % GROUP_SIZE == 0 else "mega-session\n")
                for ordinal in range(offset, stop)
            ]
            stream.writelines(labels)
    return time.perf_counter() - started


def _session_page(scores: np.ndarray, groups: np.ndarray,
                  count: int = TOP_K) -> np.ndarray:
    if len(scores) != len(groups):
        raise ValueError("semantic scale group/score length mismatch")
    maxima = np.full(int(groups.max(initial=0)) + 1, -np.inf, dtype=np.float32)
    np.maximum.at(maxima, groups, scores)
    return _top_indices(maxima, min(count, len(maxima)))


def _candidate_page(candidate_ordinals: np.ndarray, candidate_scores: np.ndarray,
                    groups: np.ndarray, count: int) -> np.ndarray:
    sessions = groups[candidate_ordinals]
    best: dict[int, float] = {}
    for session, score in zip(sessions, candidate_scores):
        key = int(session)
        best[key] = max(best.get(key, -math.inf), float(score))
    ordered = sorted(best, key=lambda session: (-best[session], session))
    return np.asarray(ordered[:count], dtype=np.int64)


def _group_candidate_subset(ordinals: np.ndarray, scores: np.ndarray,
                            groups: np.ndarray, count: int) -> np.ndarray:
    best: dict[int, float] = {}
    for ordinal, score in zip(ordinals, scores):
        group = int(groups[int(ordinal)])
        best[group] = max(best.get(group, -math.inf), float(score))
    selected = set(sorted(best, key=lambda group: (-best[group], group))[:count])
    return np.asarray(
        [int(ordinal) for ordinal in ordinals
         if int(groups[int(ordinal)]) in selected], dtype=np.int64)


def _rerank(path: Path, dtype: str, rows: int, dim: int,
            query: np.ndarray, ordinals: np.ndarray) -> np.ndarray:
    matrix = np.memmap(path, dtype=dtype, mode="r", shape=(rows, dim))
    order = np.argsort(ordinals)
    vectors = np.asarray(matrix[ordinals[order]], dtype=np.float32)
    sorted_scores = np.asarray(vectors @ query, dtype=np.float32)
    scores = np.empty(len(ordinals), dtype=np.float32)
    scores[order] = sorted_scores
    del vectors, matrix
    return scores


def _candidate_recall(oracle: np.ndarray, candidates: np.ndarray,
                      count: int) -> float:
    wanted = set(map(int, oracle[:count]))
    found = set(map(int, candidates[:count]))
    return len(wanted & found) / max(1, len(wanted))


def _parity(expected: np.ndarray, actual: np.ndarray) -> dict[str, object]:
    expected_top = _top_indices(expected)
    actual_top = _top_indices(actual)
    overlap = len(set(map(int, expected_top)) & set(map(int, actual_top)))
    expected_ten = _top_indices(expected, 10)
    actual_ten = _top_indices(actual, 10)
    ten_overlap = len(set(map(int, expected_ten)) & set(map(int, actual_ten)))
    error = np.abs(expected - actual)
    return {
        "top40_overlap": round(overlap / len(expected_top), 6),
        "top10_overlap": round(ten_overlap / len(expected_ten), 6),
        "top1_same": int(expected_top[0]) == int(actual_top[0]),
        "max_abs_error": round(float(error.max(initial=0.0)), 8),
        "mean_abs_error": round(float(error.mean()), 8),
    }


def _aggregate_parity(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "queries": len(records),
        "min_top40_overlap": min(float(item["top40_overlap"]) for item in records),
        "mean_top40_overlap": round(statistics.mean(
            float(item["top40_overlap"]) for item in records), 6),
        "min_top10_overlap": min(float(item["top10_overlap"]) for item in records),
        "mean_top10_overlap": round(statistics.mean(
            float(item["top10_overlap"]) for item in records), 6),
        "top1_rate": round(statistics.mean(
            1.0 if item["top1_same"] else 0.0 for item in records), 6),
        "max_abs_error": max(float(item["max_abs_error"]) for item in records),
        "mean_abs_error": round(statistics.mean(
            float(item["mean_abs_error"]) for item in records), 8),
    }


def _aggregate_candidates(records: list[dict[str, dict[str, dict[str, float]]]]) -> dict:
    output = {}
    for reranker in ("f32", "f16"):
        output[reranker] = {}
        for depth in CANDIDATE_DEPTHS:
            key = str(depth)
            fields = records[0][reranker][key]
            output[reranker][key] = {
                name: {
                    "minimum": round(min(item[reranker][key][name]
                                         for item in records), 6),
                    "mean": round(statistics.mean(
                        item[reranker][key][name] for item in records), 6),
                }
                for name in fields
            }
    return output


def _meta(path: Path, rows: int, dim: int, generation: str,
          matrix_bytes: int) -> None:
    path.write_text(json.dumps({
        "dim": dim,
        "model": "synthetic-semantic-scale",
        "commit": {
            "version": 1,
            "generation": generation,
            "rows": rows,
            "matrix": {"size": matrix_bytes},
        },
    }, separators=(",", ":")), encoding="utf-8")


def _percentiles(samples: list[float]) -> dict[str, object]:
    return {
        "median_ms": round(statistics.median(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def _run_campaign(rows: int, args: argparse.Namespace, root: Path) -> dict[str, object]:
    dim = args.dim
    root.mkdir(parents=True, exist_ok=True)
    f32_path = root / "embeddings.f32"
    f16_path = root / "embeddings.f16"
    meta_path = root / "embeddings.meta"
    groups_path = root / "groups.ids"
    q8_dir = root / "q8"
    generation = hashlib.sha256(f"semantic-scale:{rows}:{dim}".encode()).hexdigest()[:32]

    seed, f32_write_s = _write_f32(f32_path, rows, dim)
    _meta(meta_path, rows, dim, generation, f32_path.stat().st_size)
    group_source_s = _write_group_labels(groups_path, rows)
    groups = _adversarial_groups(rows)
    f16_build_s = _write_f16(f32_path, f16_path, rows, dim, args.block_rows)
    started = time.perf_counter()
    built = semantic_q8.build_from_f32(
        f32_path, meta_path, q8_dir, binary=RUST_BIN,
        groups_path=groups_path)
    q8_build_s = time.perf_counter() - started
    manifest = {
        **built,
        "artifact_path": Path(built["artifact"]),
        "group_artifact_path": Path(built["group_artifact"]),
    }

    started = time.perf_counter()
    scanner = semantic_q8._Q8Scanner(manifest, binary=RUST_BIN)
    cold_ms = (time.perf_counter() - started) * 1000.0
    mapped_memory = _process_memory(scanner.process.pid)
    queries = [seed[index] for index in (7, 131, 1021)[:args.parity_queries]]
    f32_samples: list[float] = []
    f16_samples: list[float] = []
    q8_parity: list[dict[str, object]] = []
    f16_parity: list[dict[str, object]] = []
    candidate_parity = []
    try:
        for query in queries:
            started = time.perf_counter()
            expected = _blocked_scores(
                f32_path, "<f4", rows, dim, query, args.block_rows)
            f32_samples.append((time.perf_counter() - started) * 1000.0)
            started = time.perf_counter()
            f16_scores = _blocked_scores(
                f16_path, "<f2", rows, dim, query, args.block_rows)
            f16_samples.append((time.perf_counter() - started) * 1000.0)
            q8_scores = scanner.score(query, generation)
            q8_parity.append(_parity(expected, q8_scores))
            f16_parity.append(_parity(expected, f16_scores))
            q8_ordinals, _ = scanner.top(query, generation, CANDIDATE_K)
            grouped_ordinals, grouped_scores = scanner.top(
                query, generation, CANDIDATE_K, grouped=True,
                heads=CANDIDATE_HEADS)
            oracle_rows = _top_indices(expected, TOP_K)
            oracle_sessions = _session_page(expected, groups)
            record = {"f32": {}, "f16": {}}
            for depth in CANDIDATE_DEPTHS:
                selected = q8_ordinals[:depth]
                grouped_selected = _group_candidate_subset(
                    grouped_ordinals, grouped_scores, groups, depth)
                exact_f32 = expected[selected]
                exact_f16 = f16_scores[selected]
                grouped_f32 = expected[grouped_selected]
                grouped_f16 = f16_scores[grouped_selected]
                for name, scores, session_scores in (
                        ("f32", exact_f32, grouped_f32),
                        ("f16", exact_f16, grouped_f16)):
                    order = selected[_top_indices(scores, len(scores))]
                    sessions = _candidate_page(
                        grouped_selected, session_scores, groups, TOP_K)
                    values = {
                        "row_top1_recall": _candidate_recall(
                            oracle_rows, order, 1),
                        "row_top8_recall": _candidate_recall(
                            oracle_rows, order, min(8, depth)),
                        "session_top1_recall": _candidate_recall(
                            oracle_sessions, sessions, 1),
                        "session_top8_recall": _candidate_recall(
                            oracle_sessions, sessions, min(8, len(sessions))),
                    }
                    if depth >= TOP_K:
                        values["row_top40_recall"] = _candidate_recall(
                            oracle_rows, order, TOP_K)
                        values["session_top40_recall"] = _candidate_recall(
                            oracle_sessions, sessions, min(TOP_K, len(sessions)))
                    record[name][str(depth)] = values
            candidate_parity.append(record)
            del expected, f16_scores, q8_scores

        scan_samples: list[float] = []
        f16_retrieval_samples: list[float] = []
        f16_rerank_samples: list[float] = []
        session_samples: list[float] = []

        def measured_query() -> int:
            query = queries[len(scan_samples) % len(queries)]
            started = time.perf_counter()
            candidate_ordinals, _ = scanner.top(
                query, generation, CANDIDATE_K, grouped=True,
                heads=CANDIDATE_HEADS)
            scan_ms = (time.perf_counter() - started) * 1000.0
            rerank_started = time.perf_counter()
            f16_scores = _rerank(
                f16_path, "<f2", rows, dim, query, candidate_ordinals)
            f16_ms = (time.perf_counter() - rerank_started) * 1000.0
            grouped_started = time.perf_counter()
            head = _candidate_page(
                candidate_ordinals, f16_scores, groups, TOP_K)
            session_ms = (time.perf_counter() - grouped_started) * 1000.0
            scan_samples.append(scan_ms)
            f16_rerank_samples.append(f16_ms)
            session_samples.append(session_ms)
            f16_retrieval_samples.append(scan_ms + f16_ms + session_ms)
            return int(head[0])

        _, peak_memory = _sample_memory(scanner.process.pid, measured_query)
        for _ in range(max(1, args.repeats) - 1):
            measured_query()
    finally:
        scanner.close()

    delta = min(args.topup_rows, max(1, rows))
    append_s = _append_f32(f32_path, rows, delta, dim, seed)
    topup_generation = hashlib.sha256(
        f"semantic-scale:{rows + delta}:{dim}".encode()).hexdigest()[:32]
    _meta(meta_path, rows + delta, dim, topup_generation, f32_path.stat().st_size)
    topup_group_source_s = _write_group_labels(groups_path, rows + delta)
    started = time.perf_counter()
    topup = semantic_q8.build_from_f32(
        f32_path, meta_path, q8_dir, binary=RUST_BIN,
        groups_path=groups_path)
    topup_rebuild_s = time.perf_counter() - started

    q8_path = Path(built["artifact"])
    group_path = Path(built["group_artifact"])
    topup_path = Path(topup["artifact"])
    topup_group_path = Path(topup["group_artifact"])
    storage = {
        "f32_bytes": rows * dim * 4,
        "f16_bytes": f16_path.stat().st_size,
        "q8_bytes": q8_path.stat().st_size,
        "group_bytes": group_path.stat().st_size,
        "q8_bytes_per_row": round(q8_path.stat().st_size / rows, 6),
        "q8_vs_f32_fraction": round(q8_path.stat().st_size / (rows * dim * 4), 6),
        "q8_topup_bytes": topup_path.stat().st_size,
        "q8_plus_f16_bytes": (q8_path.stat().st_size + group_path.stat().st_size
                               + f16_path.stat().st_size),
        "publish_peak_bytes": (f32_path.stat().st_size + f16_path.stat().st_size
                               + q8_path.stat().st_size + group_path.stat().st_size
                               + topup_path.stat().st_size
                               + topup_group_path.stat().st_size),
    }
    return {
        "rows": rows,
        "dim": dim,
        "fixture": {
            "kind": "deterministic normalized f32 vectors",
            "generation": generation,
            "f32_materialize_s": round(f32_write_s, 3),
        },
        "storage": storage,
        "build": {
            "group_source_initial_s": round(group_source_s, 3),
            "f16_initial_s": round(f16_build_s, 3),
            "q8_initial_s": round(q8_build_s, 3),
            "topup_rows": delta,
            "f32_append_s": round(append_s, 3),
            "group_source_topup_s": round(topup_group_source_s, 3),
            "q8_full_rebuild_s": round(topup_rebuild_s, 3),
            "topup_strategy": "immutable full rebuild from appended f32 generation",
        },
        "cold": {
            "q8_process_cold_ms": round(cold_ms, 3),
            "scope": "new scanner process; header/size/generation only",
        },
        "memory": {
            "mapped_ready": mapped_memory,
            "warm_query_peak": peak_memory,
            "scope": "Rust scanner only; RSS includes clean file-backed mmap pages",
        },
        "latency": {
            "f32_blocked_scan": _percentiles(f32_samples),
            "f16_blocked_scan": _percentiles(f16_samples),
            "q8_grouped_top128x8": _percentiles(scan_samples),
            "f16_candidate_rerank": _percentiles(f16_rerank_samples),
            "candidate_session_top40": _percentiles(session_samples),
            "q8_f16_retrieval": _percentiles(f16_retrieval_samples),
            "retrieval_scope": (
                "q8 top128 groups x 8 heads plus f16 rerank and session top40"),
        },
        "parity": {
            "q8_direct_diagnostic": _aggregate_parity(q8_parity),
            "f16_vs_f32": _aggregate_parity(f16_parity),
            "q8_candidates": _aggregate_candidates(candidate_parity),
        },
    }


def _metric(campaign: dict[str, object], name: str) -> float | None:
    paths = {
        "q8_scan_max_ms": ("latency", "q8_grouped_top128x8", "max_ms"),
        "q8_retrieval_max_ms": ("latency", "q8_f16_retrieval", "max_ms"),
        "q8_process_cold_ms": ("cold", "q8_process_cold_ms"),
        "q8_build_s": ("build", "q8_initial_s"),
        "q8_topup_rebuild_s": ("build", "q8_full_rebuild_s"),
        "q8_private_mib": ("memory", "warm_query_peak", "private_mib"),
    }
    value: object = campaign
    try:
        for part in paths[name]:
            value = value[part]
        return None if value is None else float(value)
    except (KeyError, TypeError, ValueError):
        return None


def _affine_fit(campaigns: list[dict[str, object]], name: str) -> dict[str, object]:
    points = sorted((int(item["rows"]) / 1_000_000, _metric(item, name))
                    for item in campaigns)
    points = [(rows, value) for rows, value in points if value is not None]
    if not points:
        return {"method": "unavailable", "intercept": None, "per_million": None}
    if len(points) == 1:
        rows, value = points[0]
        return {"method": "single-point-through-origin", "intercept": 0.0,
                "per_million": float(value) / rows}
    slopes = [(right[1] - left[1]) / (right[0] - left[0])
              for index, left in enumerate(points)
              for right in points[index + 1:] if right[0] != left[0]]
    slope = max(0.0, statistics.median(slopes))
    intercept = max(0.0, statistics.median(
        value - slope * rows for rows, value in points))
    return {"method": "theil-sen-affine", "intercept": round(intercept, 6),
            "per_million": round(slope, 6)}


def _fit_value(fit: dict[str, object], rows: int) -> float | None:
    if fit.get("intercept") is None or fit.get("per_million") is None:
        return None
    return float(fit["intercept"]) + float(fit["per_million"]) * rows / 1_000_000


def _fixture_group_scratch_bytes(rows: int) -> int:
    normal_rows = math.ceil(rows / GROUP_SIZE)
    group_count = 1 + math.ceil(rows / (GROUP_SIZE * GROUP_SIZE))
    slots = normal_rows + min(max(0, rows - normal_rows), CANDIDATE_HEADS)
    return 4 * (group_count + 1) + 12 * slots


def _private_fixed_mib(campaigns: list[dict[str, object]]) -> float:
    residuals = []
    for campaign in campaigns:
        measured = _metric(campaign, "q8_private_mib")
        if measured is not None:
            scratch = _fixture_group_scratch_bytes(int(campaign["rows"])) / MIB
            residuals.append(max(0.0, measured - scratch))
    return max(residuals, default=0.0)


def _projections(campaigns: list[dict[str, object]]) -> dict[str, object]:
    basis = max(campaigns, key=lambda item: int(item["rows"]))
    basis_rows = int(basis["rows"])
    fitted_names = (
        "q8_scan_max_ms", "q8_retrieval_max_ms", "q8_build_s",
        "q8_topup_rebuild_s", "q8_private_mib")
    fits = {name: _affine_fit(campaigns, name) for name in fitted_names}
    cold = max((_metric(item, "q8_process_cold_ms") or 0.0)
               for item in campaigns)
    fixed_private = _private_fixed_mib(campaigns)
    output = {}
    for rows in PROJECTION_ROWS:
        metrics = {
            name: (None if _fit_value(fits[name], rows) is None else
                   round(float(_fit_value(fits[name], rows)), 3))
            for name in fitted_names
        }
        metrics["q8_process_cold_ms"] = round(cold, 3)
        affine_private = metrics["q8_private_mib"]
        upper_private = fixed_private + (16 * rows + 4) / MIB
        metrics["q8_private_affine_mib"] = affine_private
        metrics["q8_private_upper_mib"] = round(upper_private, 3)
        metrics["q8_private_mib"] = round(max(
            float(affine_private or 0.0), upper_private), 3)
        metrics.update({
            "q8_artifact_gib": round((64 + rows * (int(basis["dim"]) + 4)) / GIB, 6),
            "f16_artifact_gib": round(rows * int(basis["dim"]) * 2 / GIB, 6),
            "f32_artifact_gib": round(rows * int(basis["dim"]) * 4 / GIB, 6),
            "group_artifact_gib": round((64 + rows * 4) / GIB, 6),
            "q8_f16_artifacts_gib": round(
                (128 + rows * (int(basis["dim"]) + 8)
                 + rows * int(basis["dim"]) * 2) / GIB, 6),
        })
        if metrics["q8_private_mib"] is not None:
            mapped_mib = (128 + rows * (int(basis["dim"]) + 8)) / MIB
            metrics["q8_mapped_rss_mib"] = round(
                mapped_mib + float(metrics["q8_private_mib"]), 3)
        else:
            metrics["q8_mapped_rss_mib"] = None
        output[str(rows)] = metrics
    return {
        "basis_rows": basis_rows,
        "fits": fits,
        "method": (
            "exact format bytes; robust Theil-Sen affine timing across measured points; "
            "native top-k private memory uses the same affine fit; header-open uses "
            "the measured maximum because it is payload-size independent; the reported "
            "private ceiling is at least the all-unique-group scratch bound"),
        "why_linear": (
            "all candidates are scanned once; header-open does not touch the payload; "
            "immutable top-up rebuilds every row"),
        "targets": output,
    }


def _profile_budgets(profile: str) -> dict[str, float]:
    budgets = dict(DESIGN_BUDGETS_10M)
    if profile == "portable-ci":
        for name, multiplier in PORTABLE_MULTIPLIERS.items():
            budgets[name] *= multiplier
    return budgets


def _budget_failures(campaigns: list[dict[str, object]], projections: dict,
                     profile: str) -> list[str]:
    budgets = _profile_budgets(profile)
    failures = []
    for campaign in campaigns:
        rows = int(campaign["rows"])
        factor = rows / 10_000_000
        for name in ("q8_scan_max_ms", "q8_retrieval_max_ms", "q8_build_s",
                     "q8_topup_rebuild_s", "q8_private_mib"):
            measured = _metric(campaign, name)
            fixed = {
                "q8_scan_max_ms": 2.0,
                "q8_retrieval_max_ms": 20.0,
                "q8_build_s": 1.0,
                "q8_topup_rebuild_s": 1.0,
                "q8_private_mib": 48.0,
            }[name]
            limit = fixed + max(0.0, budgets[name] - fixed) * factor
            if measured is None:
                failures.append(f"{rows:,} {name}: not measured")
            elif measured > limit:
                failures.append(f"{rows:,} {name}: {measured:.3f} > {limit:.3f}")
        cold = _metric(campaign, "q8_process_cold_ms")
        if cold is None:
            failures.append(f"{rows:,} q8_process_cold_ms: not measured")
        elif cold > budgets["q8_process_cold_ms"]:
            failures.append(
                f"{rows:,} q8_process_cold_ms: {cold:.3f} > "
                f"{budgets['q8_process_cold_ms']:.3f}")
        storage = campaign["storage"]
        expected_bpr = _expected_q8_bytes_per_row(int(campaign["dim"]), rows)
        if not math.isclose(
                float(storage["q8_bytes_per_row"]), expected_bpr,
                rel_tol=0.0, abs_tol=0.000001):
            failures.append(
                f"{rows:,} q8_bytes_per_row: {storage['q8_bytes_per_row']} "
                f"!= {expected_bpr:.6f}")
        if int(storage["group_bytes"]) != 64 + rows * 4:
            failures.append(
                f"{rows:,} group_bytes: {storage['group_bytes']} != {64 + rows * 4}")
        parity = campaign["parity"]["f16_vs_f32"]
        for field, comparator in (
                ("min_top40_overlap", "minimum"),
                ("top1_rate", "minimum"),
                ("max_abs_error", "maximum")):
            field_name = "top40_overlap" if field == "min_top40_overlap" else field
            name = f"f16_{field_name}"
            limit = budgets[name]
            value = float(parity[field])
            bad = value < limit if comparator == "minimum" else value > limit
            if bad:
                failures.append(
                    f"{rows:,} {name}: {value:.6f} "
                    f"{'<' if comparator == 'minimum' else '>'} {limit:.6f}")
        candidates = campaign["parity"]["q8_candidates"]
        for reranker in ("f32", "f16"):
            depth = candidates[reranker][str(CANDIDATE_K)]
            for name in ("row_top1_recall", "row_top8_recall",
                         "session_top1_recall", "session_top8_recall",
                         "row_top40_recall", "session_top40_recall"):
                value = float(depth[name]["minimum"])
                limit = budgets[f"candidate_{name}"]
                if value < limit:
                    failures.append(
                        f"{rows:,} {reranker} candidate {name}: "
                        f"{value:.6f} < {limit:.6f}")

    ten_m = projections["targets"]["10000000"]
    for name in ("q8_scan_max_ms", "q8_retrieval_max_ms", "q8_process_cold_ms",
                 "q8_build_s", "q8_topup_rebuild_s", "q8_private_mib",
                 "q8_artifact_gib", "q8_f16_artifacts_gib"):
        value = ten_m.get(name)
        if value is None:
            failures.append(f"10,000,000 projected {name}: not measured")
        elif float(value) > budgets[name]:
            failures.append(
                f"10,000,000 projected {name}: {float(value):.3f} > "
                f"{budgets[name]:.3f}")
    return failures


def _expected_q8_bytes_per_row(dim: int, rows: int) -> float:
    if dim <= 0 or rows <= 0:
        raise ValueError("q8 dimensions and rows must be positive")
    return dim + 4.0 + 64.0 / rows


def _physical_memory() -> int | None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5)
        try:
            if result.returncode == 0:
                return int(result.stdout.strip())
        except ValueError:
            pass
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30)
        for line in result.stdout.splitlines():
            if line.strip().startswith("Memory:"):
                fields = line.split(":", 1)[1].strip().split()
                try:
                    value = float(fields[0])
                    unit = fields[1].upper()
                    return int(value * (GIB if unit == "GB" else MIB))
                except (IndexError, ValueError):
                    return None
        return None
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = (("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong),
                        ("available", ctypes.c_ulonglong),
                        ("page_total", ctypes.c_ulonglong),
                        ("page_available", ctypes.c_ulonglong),
                        ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_available", ctypes.c_ulonglong),
                        ("extended_available", ctypes.c_ulonglong))
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        try:
            return (int(status.total)
                    if ctypes.windll.kernel32.GlobalMemoryStatusEx(
                        ctypes.byref(status)) else None)
        except (AttributeError, OSError):
            return None
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def _provenance() -> dict[str, object]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10)
    binary_hash = hashlib.sha256(RUST_BIN.read_bytes()).hexdigest()
    cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") \
        or platform.machine()
    if sys.platform == "darwin":
        hardware = subprocess.run(
            ["system_profiler", "SPHardwareDataType"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30)
        for line in hardware.stdout.splitlines():
            if line.strip().startswith("Chip:"):
                cpu = line.split(":", 1)[1].strip()
                break
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "physical_memory_bytes": _physical_memory(),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "git_commit": result.stdout.strip() if result.returncode == 0 else "unavailable",
        "git_dirty": bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()),
        "rust_binary": str(RUST_BIN),
        "rust_binary_sha256": binary_hash,
    }


def _required_space(rows: int, dim: int, topup_rows: int) -> int:
    f32 = (rows + topup_rows) * dim * 4
    f16 = rows * dim * 2
    q8 = (64 + rows * (dim + 4)) + (64 + (rows + topup_rows) * (dim + 4))
    groups = (64 + rows * 4) + (64 + (rows + topup_rows) * 4)
    return f32 + f16 + q8 + groups + 2 * GIB


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", nargs="+", type=int, default=list(DEFAULT_ROWS))
    parser.add_argument("--dim", type=int, default=DIM)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--parity-queries", type=int, default=3)
    parser.add_argument("--block-rows", type=int, default=32_768)
    parser.add_argument("--topup-rows", type=int, default=10_000)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--budget-profile", choices=("target", "portable-ci"),
                        default="target")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force-low-space", action="store_true")
    args = parser.parse_args(argv)
    if args.quick:
        args.rows = list(QUICK_ROWS)
    if (any(rows <= 0 for rows in args.rows) or args.dim <= 0
            or args.repeats <= 0 or not 1 <= args.parity_queries <= 3
            or args.block_rows <= 0 or args.topup_rows <= 0):
        parser.error("row, dimension, repeat, parity, block, and top-up values must be positive")
    return args


def _print_campaign(campaign: dict[str, object]) -> None:
    rows = int(campaign["rows"])
    storage = campaign["storage"]
    build = campaign["build"]
    latency = campaign["latency"]
    memory = campaign["memory"]["warm_query_peak"]
    print(f"{rows:,} rows · q8 {storage['q8_bytes'] / MIB:.1f} MiB + "
          f"groups {storage['group_bytes'] / MIB:.1f} MiB "
          f"({storage['q8_vs_f32_fraction']:.3f}x f32)")
    print(f"  build q8 {build['q8_initial_s']:.3f}s · "
          f"+{build['topup_rows']:,} full rebuild {build['q8_full_rebuild_s']:.3f}s")
    print(f"  q8 process-cold {campaign['cold']['q8_process_cold_ms']:.1f}ms · "
          f"grouped top128x8 max {latency['q8_grouped_top128x8']['max_ms']:.1f}ms · "
          f"f16 retrieval max {latency['q8_f16_retrieval']['max_ms']:.1f}ms")
    print(f"  worker peak RSS {memory['rss_mib']} MiB · private {memory['private_mib']} MiB")
    for name in ("q8_direct_diagnostic", "f16_vs_f32"):
        parity = campaign["parity"][name]
        print(f"  {name}: top40 min {parity['min_top40_overlap']:.3f} · "
              f"top10 min {parity['min_top10_overlap']:.3f} · "
              f"top1 {parity['top1_rate']:.3f} · max error {parity['max_abs_error']:.6f}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not RUST_BIN.is_file():
        print(f"semantic scale benchmark needs {RUST_BIN}; run cargo build --release",
              file=sys.stderr)
        return 2
    newer = [path for path in (ROOT / "crates").rglob("*.rs")
             if path.stat().st_mtime_ns > RUST_BIN.stat().st_mtime_ns]
    if args.check and newer:
        print("release binary predates Rust sources; run cargo build --release",
              file=sys.stderr)
        return 2
    temp_root = args.temp_root.expanduser().resolve() if args.temp_root else None
    guard = temp_root or Path(tempfile.gettempdir())
    guard.mkdir(parents=True, exist_ok=True)
    campaigns = []
    try:
        for rows in args.rows:
            needed = _required_space(rows, args.dim, min(args.topup_rows, rows))
            free = shutil.disk_usage(guard).free
            if free < needed and not args.force_low_space:
                raise RuntimeError(
                    f"{rows:,} rows need {needed / GIB:.2f} GiB including reserve; "
                    f"only {free / GIB:.2f} GiB is free")
            if not args.json:
                print(f"building disposable {rows:,}-row semantic campaign", flush=True)
            with tempfile.TemporaryDirectory(
                    prefix=f"agrep-semantic-scale-{rows}-", dir=temp_root) as raw:
                campaigns.append(_run_campaign(rows, args, Path(raw)))
                if not args.json:
                    _print_campaign(campaigns[-1])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"semantic scale benchmark failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    projections = _projections(campaigns)
    output = {
        "schema": 2,
        "provenance": _provenance(),
        "scope": {
            "model_embed": "not measured by this scanner gate",
            "cold": "new Rust scanner process, header-only artifact validation",
            "query": "q8 top128 groups x 8 heads, f16 rerank, session top40",
            "group_fixture": (
                "adversarial skew: 75% of rows share one session; remaining sessions "
                "hold four rows"),
            "f32_f16": "blocked mmap comparison oracles",
        },
        "campaigns": campaigns,
        "projections": projections,
        "design_budgets_10m": _profile_budgets(args.budget_profile),
        "budget_profile": args.budget_profile,
    }
    failures = _budget_failures(campaigns, projections, args.budget_profile)
    output["budget_failures"] = failures
    if not args.json:
        ten_m = projections["targets"]["10000000"]
        print(f"10M projection · q8 {ten_m['q8_artifact_gib']:.3f} GiB · "
              f"scan {ten_m['q8_scan_max_ms']:.1f}ms · "
              f"retrieval {ten_m['q8_retrieval_max_ms']:.1f}ms · "
              f"process-cold {ten_m['q8_process_cold_ms']:.1f}ms")
        for failure in failures:
            print(f"semantic scale budget failed: {failure}", file=sys.stderr)
    else:
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 1 if args.check and failures else 0


def _isolated_main() -> int:
    if os.environ.get(_ISOLATED_DATA_ENV):
        return main()
    with tempfile.TemporaryDirectory(prefix="agrep-semantic-scale-owner-") as raw:
        data = Path(raw) / "data"
        data.mkdir()
        env = dict(os.environ)
        env["AGREP_DATA_DIR"] = str(data)
        env[_ISOLATED_DATA_ENV] = str(data)
        return subprocess.call(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env=env)


if __name__ == "__main__":
    raise SystemExit(_isolated_main())
