#!/usr/bin/env python3
"""Compare the shipped int8 embedder with a full-precision ONNX reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
if str(PY) not in sys.path:
    sys.path.insert(0, str(PY))

import common  # noqa: E402
import embedder  # noqa: E402


def _queries(path: Path) -> list[str]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [str(row["query"]) for row in rows]


def _passages(limit: int) -> list[str]:
    texts = [message.text for message in common.iter_messages() if message.text.strip()]
    if len(texts) <= limit:
        return texts
    positions = np.linspace(0, len(texts) - 1, num=limit, dtype=np.int64)
    return [texts[int(position)] for position in positions]


def _session(model: Path):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = embedder._thread_budget()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        str(model), sess_options=opts, providers=["CPUExecutionProvider"])


def _embed_with_session(model: embedder.Embedder, session, texts: list[str]) -> np.ndarray:
    model.sess = session
    model.inputs = {item.name for item in session.get_inputs()}
    return model.embed_texts(texts)


def _overlap(left: np.ndarray, right: np.ndarray, k: int) -> float:
    width = min(k, left.shape[0])
    a = np.argpartition(left, -width)[-width:]
    b = np.argpartition(right, -width)[-width:]
    return len(set(map(int, a)) & set(map(int, b))) / width


def evaluate(reference_model: Path, tasks: Path, sample: int) -> dict:
    queries = _queries(tasks)
    passages = _passages(sample)
    if not queries or not passages:
        raise RuntimeError("parity fixture requires queries and indexed messages")

    started = time.perf_counter()
    model = embedder.Embedder(download=False)
    int8_session = model.sess
    int8_queries = _embed_with_session(model, int8_session, queries)
    int8_passages = _embed_with_session(model, int8_session, passages)

    reference_session = _session(reference_model)
    reference_queries = _embed_with_session(model, reference_session, queries)
    reference_passages = _embed_with_session(model, reference_session, passages)

    query_cosines = np.sum(int8_queries * reference_queries, axis=1)
    passage_cosines = np.sum(int8_passages * reference_passages, axis=1)
    top1 = []
    top5 = []
    top10 = []
    score_deltas = []
    floor_disagreements = 0
    strong_disagreements = 0
    score_count = 0
    bands = embedder.semantic_bands()
    for iq, rq in zip(int8_queries, reference_queries):
        int8_scores = int8_passages @ iq
        reference_scores = reference_passages @ rq
        top1.append(int(np.argmax(int8_scores) == np.argmax(reference_scores)))
        top5.append(_overlap(int8_scores, reference_scores, 5))
        top10.append(_overlap(int8_scores, reference_scores, 10))
        score_deltas.extend(np.abs(int8_scores - reference_scores).tolist())
        floor_disagreements += int(np.count_nonzero(
            (int8_scores >= bands.floor)
            != (reference_scores >= bands.floor)))
        strong_disagreements += int(np.count_nonzero(
            (int8_scores >= bands.strong)
            != (reference_scores >= bands.strong)))
        score_count += len(int8_scores)

    return {
        "model": embedder.PROFILE_STRING,
        "reference": str(reference_model),
        "queries": len(queries),
        "passages": len(passages),
        "fixture_sha256": hashlib.sha256(
            "\0".join([*queries, *passages]).encode("utf-8")).hexdigest(),
        "query_cosine_min": round(float(np.min(query_cosines)), 6),
        "query_cosine_mean": round(float(np.mean(query_cosines)), 6),
        "passage_cosine_min": round(float(np.min(passage_cosines)), 6),
        "passage_cosine_mean": round(float(np.mean(passage_cosines)), 6),
        "top1_agreement": round(float(np.mean(top1)), 6),
        "top5_overlap": round(float(np.mean(top5)), 6),
        "top10_overlap": round(float(np.mean(top10)), 6),
        "score_abs_delta_mean": round(float(np.mean(score_deltas)), 6),
        "score_abs_delta_p99": round(float(np.quantile(score_deltas, 0.99)), 6),
        "floor_disagreement_rate": round(floor_disagreements / score_count, 6),
        "strong_disagreement_rate": round(strong_disagreements / score_count, 6),
        "wall_s": round(time.perf_counter() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-model", type=Path, required=True)
    parser.add_argument("--tasks", type=Path,
                        default=ROOT / "bench" / "semantic_worth_tasks.example.json")
    parser.add_argument("--sample", type=int, default=128)
    args = parser.parse_args()
    if args.sample < 10:
        parser.error("--sample must be at least 10")
    result = evaluate(args.reference_model, args.tasks, args.sample)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
