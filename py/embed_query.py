"""Embed a single search query into data/query.f32.

The query vector MUST come from the same model family that produced
data/embeddings.f32 (cosine only means anything within one embedding space).
We can't see which model embed.py loaded at runtime, so we run the same
resolve-or-fallback list here and apply the query-side prefix that matches
whichever model actually loaded:

  Qwen3-Embedding (PRIMARY): asymmetric instruction prefix
      "Instruct: Retrieve developer-chat messages relevant to the query\nQuery: {q}"
      (passages got NO prefix in embed.py; queries get this one.)
  BGE (FALLBACK): "Represent this sentence for searching relevant passages: {q}"
  MiniLM (FALLBACK): no prefix.

Output: data/query.f32 — one D=256 L2-normalized little-endian float32 vector.

Usage:
  python embed_query.py "why is the build still broken"

NOTE: this is the retrieval query path. The LLM judge (Qwen3.5-4B) is a
separate, later stage and is not involved here.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

import common
from embed import CANDIDATES, QWEN_QUERY_INSTRUCTION, build_loader, is_bge, is_qwen

# Must match embed.py's Qwen passage handling: only the query side is prefixed.
QWEN_QUERY_TEMPLATE = "Instruct: {instruction}\nQuery: {query}"
# bge-*-en-v1.5 official retrieval query instruction.
BGE_QUERY_TEMPLATE = "Represent this sentence for searching relevant passages: {query}"


def format_query(model_id: str, query: str) -> str:
    """Apply the model-appropriate query-side prefix."""
    if is_qwen(model_id):
        return QWEN_QUERY_TEMPLATE.format(instruction=QWEN_QUERY_INSTRUCTION, query=query)
    if is_bge(model_id):
        return BGE_QUERY_TEMPLATE.format(query=query)
    # MiniLM and anything else: no prefix.
    return query


def embed_one(model, text: str, device: str) -> np.ndarray:
    """Embed a single string at the model's native dim (no normalization yet)."""
    emb = model.encode(
        [text],
        batch_size=1,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=False,
        device=device,
    )
    return np.asarray(emb, dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed one query string -> query.f32")
    ap.add_argument("query", help="The search query text.")
    args = ap.parse_args()

    if not args.query.strip():
        common.log("error: empty query.")
        return 1

    device = common.pick_device()
    resolved = common.resolve_model(CANDIDATES, build_loader(device), label="embed_query")

    formatted = format_query(resolved.id, args.query)
    common.log(f"query model={resolved.id} device={device}")
    common.log(f"formatted query: {formatted!r}")

    t0 = time.perf_counter()
    native = embed_one(resolved.obj, formatted, device)
    vec = common.matryoshka_truncate(native, dim=common.EMBED_DIM).reshape(-1)
    common.write_query(vec, dim=common.EMBED_DIM)
    elapsed = time.perf_counter() - t0

    common.log(
        f"query done | dim={vec.shape[0]} | elapsed={elapsed:.2f}s | "
        f"wrote {common.QUERY_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
