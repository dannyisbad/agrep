"""Semantic search WITH reranking + dedup. Two levels:

  --level message  : find a specific line (bi-encoder over message vectors -> rerank)
  --level chat     : find what a whole SESSION was about (bi-encoder over session
                     centroids from concepts.py -> rerank on the session's snippet)

The bi-encoder is a cheap recall net; the cross-encoder rerank is what makes the top
results actually relevant. Chat-level is the right tool for "what was I working on
about X" questions, where no single message answers but a whole session does.

Usage:
  python search.py "your query" [--k 10] [--pool 50] [--level message|chat]
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

import common
from embed import CANDIDATES, QWEN_QUERY_INSTRUCTION, build_loader, is_qwen

# Cross-encoder rerankers, primary first (both load via sentence-transformers CrossEncoder).
RERANKERS = ["BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-base"]


def embed_query(text: str, device: str) -> tuple[np.ndarray, str]:
    resolved = common.resolve_model(CANDIDATES, build_loader(device), label="embed-q")
    model = resolved.obj
    if is_qwen(resolved.id):
        q = f"Instruct: {QWEN_QUERY_INSTRUCTION}\nQuery: {text}"
    elif "bge" in resolved.id.lower():
        q = f"Represent this sentence for searching relevant passages: {text}"
    else:
        q = text
    v = model.encode([q], convert_to_numpy=True, normalize_embeddings=False, device=device)[0]
    v = common.matryoshka_truncate(v.reshape(1, -1), dim=common.EMBED_DIM)[0]
    return v.astype(np.float32), resolved.id


def load_meta() -> dict[str, tuple[str, str, str]]:
    meta: dict[str, tuple[str, str, str]] = {}
    with common.MESSAGES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            meta[o["id"]] = (o.get("agent", ""), o.get("project", ""), o.get("text", ""))
    return meta


def load_session_index() -> tuple[list[str], np.ndarray, dict[str, dict]]:
    meta_dim = int((common.DATA_DIR / "session_emb.meta").read_text().strip())
    ids = (common.DATA_DIR / "session_emb.ids").read_text(encoding="utf-8").splitlines()
    mat = np.fromfile(common.DATA_DIR / "session_emb.f32", dtype="<f4").reshape(-1, meta_dim)
    snips: dict[str, dict] = {}
    with (common.DATA_DIR / "session_snip.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                o = json.loads(line)
                snips[o["session"]] = o
    return ids, mat, snips


def snippet(text: str, n: int = 90) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description="semantic search + rerank")
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--pool", type=int, default=50, help="candidate pool from the bi-encoder")
    ap.add_argument("--level", choices=["message", "chat"], default="message",
                    help="message = find a line; chat = find what a session was about")
    args = ap.parse_args()

    device = common.pick_device()
    if device == "cuda":
        try:
            import torch

            torch.cuda.set_per_process_memory_fraction(0.85)
        except Exception:  # noqa: BLE001
            pass

    t0 = time.perf_counter()
    qv, emb_id = embed_query(args.query, device)

    # build a uniform candidate list: (left_label, doc_text, cosine)
    cand: list[tuple[str, str, float]] = []
    if args.level == "chat":
        sum_f = common.DATA_DIR / "summary_emb.f32"
        if sum_f.exists():
            # preferred: per-session LLM summaries (sharp chat-level signal)
            dim = int((common.DATA_DIR / "summary_emb.meta").read_text().strip())
            sids = (common.DATA_DIR / "summary_emb.ids").read_text(encoding="utf-8").splitlines()
            mat = np.fromfile(sum_f, dtype="<f4").reshape(-1, dim)
            recs: dict[str, dict] = {}
            for line in (common.DATA_DIR / "summaries.jsonl").read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    o = json.loads(line)
                    recs[o["session"]] = o
            sims = mat @ qv
            for i in np.argsort(-sims)[: args.pool]:
                o = recs.get(sids[int(i)])
                if not o:
                    continue
                doc = (o.get("summary", "") + " " + ", ".join(o.get("tags", []))).strip()
                cand.append((f"{o.get('agent','')}/{o.get('cwd_project','')[:22]}", doc, float(sims[int(i)])))
            src = f"{len(sids)} chat summaries"
        else:
            # fallback: centroid + longest-message snippet (weaker)
            ids, mat, snips = load_session_index()
            sims = mat @ qv
            for i in np.argsort(-sims)[: args.pool]:
                o = snips.get(ids[int(i)])
                if not o:
                    continue
                cand.append((f"{o.get('agent','')} · {o.get('concept','')[:30]}",
                             o.get("snippet", ""), float(sims[int(i)])))
            src = f"{len(ids)} sessions (centroid)"
    else:
        ids, mat = common.read_embeddings()
        meta = load_meta()
        sims = mat @ qv
        pool_raw = np.argpartition(-sims, min(args.pool * 3, len(sims) - 1))[: args.pool * 3]
        pool_raw = pool_raw[np.argsort(-sims[pool_raw])]
        seen: set[str] = set()
        for i in pool_raw:
            m = meta.get(ids[int(i)])
            if not m:
                continue
            key = " ".join(m[2].split())[:160].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            cand.append((f"{m[0]}/{m[1]}", m[2], float(sims[int(i)])))
            if len(cand) >= args.pool:
                break
        src = f"{len(ids)} messages"

    if not cand:
        print("  no candidates (run `tilt embed` / `concepts.py` first?)")
        return 1

    from sentence_transformers import CrossEncoder

    def load_ce(model_id: str):
        return CrossEncoder(model_id, device=device, max_length=512)

    rr = common.resolve_model(RERANKERS, load_ce, label="rerank")
    rscores = rr.obj.predict([(args.query, doc) for _, doc, _ in cand], show_progress_bar=False)
    order = np.argsort(-np.asarray(rscores))[: args.k]
    elapsed = time.perf_counter() - t0

    print()
    print(f'  tilt search [{args.level}] · "{args.query}"  ({src} · rerank={rr.id})')
    print(f"  reranked {len(cand)} candidates in {elapsed:.1f}s")
    print(f"  {'rank':>4}  {'rerank':>7}  {'cos':>5}  {'agent/concept':<32}  snippet")
    for rank, oi in enumerate(order, 1):
        left, doc, cos = cand[int(oi)]
        left = left if len(left) <= 32 else left[:31] + "…"
        print(f"  {rank:>4}  {float(rscores[int(oi)]):>7.3f}  {cos:>5.2f}  {left:<32}  {snippet(doc)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
