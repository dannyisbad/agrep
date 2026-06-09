"""Embed every message in data/messages.jsonl into data/embeddings.f32 + .ids.

Pipeline:
  1. resolve-or-fallback the embedding model (Qwen3 primary, BGE/MiniLM fallback)
  2. embed all message texts as PASSAGES (no query instruction)
  3. Matryoshka-truncate to D=256, L2-normalize each row
  4. write the contract pair (embeddings.f32 + embeddings.ids)

Model-specific handling, branched on which candidate actually loaded:

  Qwen/Qwen3-Embedding-0.6B (PRIMARY, 1024-d):
    - last-token pooling, LEFT padding (the EOS/last real token must be the
      final position for last-token pooling to read the right hidden state)
    - asymmetric instruction: PASSAGES get NO prefix; QUERIES (embed_query.py)
      get an "Instruct: ...\nQuery: {q}" prefix.
    Implemented via sentence-transformers, which ships the correct pooling
    config for this model and exposes prompt_name="query" for the query side.

  BAAI/bge-base-en-v1.5 (FALLBACK, 768-d):
    - mean pooling (handled by sentence-transformers)
    - passages: no prefix. queries: "Represent this sentence for searching
      relevant passages: {q}" (applied in embed_query.py).

  sentence-transformers/all-MiniLM-L6-v2 (FALLBACK, 384-d):
    - mean pooling. NO prefix on either side.

Usage:
  python embed.py            # embed everything
  python embed.py --smoke 8  # embed only the first 8 messages (quick check)

Prints: model used, device, count, dim, elapsed, approx VRAM.

NOTE: the affect gate lives in emotion.py; the LLM judge (Qwen3.5-4B) is a
SEPARATE, LATER stage and is not invoked here.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

import common

# Candidate models, primary first. The fallbacks are permissively licensed and
# small enough to run on CPU if Qwen3 can't be fetched/loaded.
PRIMARY = "Qwen/Qwen3-Embedding-0.6B"
FALLBACKS = ["BAAI/bge-base-en-v1.5", "sentence-transformers/all-MiniLM-L6-v2"]
CANDIDATES = [PRIMARY] + FALLBACKS

# The Qwen3 query-side instruction. PASSAGES intentionally get no prefix; only
# embed_query.py applies this (must stay byte-identical there).
QWEN_QUERY_INSTRUCTION = "Retrieve developer-chat messages relevant to the query"

# Dev-chat messages include huge pastes (whole files, logs). Qwen3-Embedding's
# native context is 32k; without a cap, one long message batch-padded to its
# length OOMs a 10GB GPU (64 x 26k x 1024 x 4B ~= 6.9 GiB in a single fp32 tensor).
# Peak memory is ~ batch x seq (linear), so a generous cap is nearly free with a
# modest batch: 2048 tokens keeps the VAST majority of messages whole (only true
# file-dumps get trimmed), preserving semantics, while batch=32 x 2048 ~= 270 MB.
# Tunable via --max-seq-len. (A future option for huge messages: chunk + average
# multiple vectors rather than truncate.)
DEFAULT_MAX_SEQ = 2048


def is_qwen(model_id: str) -> bool:
    return "Qwen3-Embedding" in model_id or "qwen3-embedding" in model_id.lower()


def is_bge(model_id: str) -> bool:
    return "bge" in model_id.lower()


def build_loader(device: str, max_seq_len: int = DEFAULT_MAX_SEQ):
    """Return a loader(model_id) -> SentenceTransformer for resolve_model.

    sentence-transformers already knows the right pooling for each of these
    repos (last-token + left padding for Qwen3, mean for bge/minilm), so we
    don't hand-roll pooling. We DO pin Qwen3's padding side to left defensively,
    cap the sequence length (memory + speed), and enable fp16 on CUDA.
    """
    from sentence_transformers import SentenceTransformer  # local import: heavy

    def loader(model_id: str) -> SentenceTransformer:
        import torch  # local import: torch may not exist at module import time

        model_kwargs = {}
        if device == "cuda":
            # fp16 weights on GPU; the 3080 has 10GB and a 0.6B model fits easily.
            model_kwargs["torch_dtype"] = torch.float16

        model = SentenceTransformer(
            model_id,
            device=device,
            trust_remote_code=False,  # these repos load with stock code
            model_kwargs=model_kwargs or None,
        )

        if is_qwen(model_id):
            # Last-token pooling requires the final non-pad token to sit at the
            # rightmost position -> LEFT padding. Qwen3-Embedding ships this in
            # its tokenizer config, but pin it so a stale cache can't flip it.
            try:
                model.tokenizer.padding_side = "left"
            except Exception:  # noqa: BLE001
                common.log("warn: could not set tokenizer.padding_side='left'")

        # Cap sequence length so a giant pasted message can't OOM the GPU. This
        # truncates over-long inputs to the first `max_seq_len` tokens.
        try:
            model.max_seq_length = max_seq_len
        except Exception:  # noqa: BLE001
            common.log(f"warn: could not set max_seq_length={max_seq_len}")
        return model

    return loader


def embed_passages(model, texts: list[str], device: str, token_budget: int = 6144) -> np.ndarray:
    """Embed texts as PASSAGES (no instruction prefix), at the model's native dim.

    LENGTH-AWARE TOKEN-BUDGET BATCHING. On this Windows/torch build, SDPA uses the
    math backend, which materializes the full batch x heads x seq^2 attention
    matrix — so a fixed batch_size OOMs whenever a batch contains long messages
    (one 2048-token batch of 32 ~= 8.6 GiB). We instead bound `batch_count *
    max_seq_in_batch <= token_budget`, so attention memory stays ~constant
    (~token_budget x heads x seq x 2B) no matter the length mix: long messages get
    tiny batches (full context preserved, no extra truncation), short messages get
    big batches (fast). Rows are written back in ORIGINAL order so ids stay aligned.

    Returns float32 (N, native_dim); truncation/normalization happens in the caller.
    """
    msl = getattr(model, "max_seq_length", None) or 2048
    # cheap token estimate (chars/2 over-counts vs real tokenization => safer/smaller
    # batches), capped at the model's max_seq_length since longer is truncated anyway.
    est = [min(max(1, len(t) // 2), msl) for t in texts]
    order = sorted(range(len(texts)), key=lambda i: est[i])  # ascending length

    out: list[np.ndarray | None] = [None] * len(texts)
    n_batches = 0
    i = 0
    while i < len(order):
        j, maxlen = i, 0
        while j < len(order):
            cand = est[order[j]]
            nm = maxlen if maxlen > cand else cand
            if (j - i + 1) * nm > token_budget and j > i:
                break
            maxlen, j = nm, j + 1
        idx = order[i:j]
        emb = model.encode(
            [texts[k] for k in idx],
            batch_size=len(idx),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=False,
            device=device,
        )
        emb = np.asarray(emb, dtype=np.float32)
        for pos, k in enumerate(idx):
            out[k] = emb[pos]
        i, n_batches = j, n_batches + 1
        if n_batches % 25 == 0:
            common.log(f"  ... {i}/{len(order)} messages embedded ({n_batches} batches)")

    common.log(f"  encoded in {n_batches} length-bucketed batches (token_budget={token_budget})")
    return np.vstack(out).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed messages.jsonl -> embeddings.f32 + .ids")
    ap.add_argument(
        "--smoke",
        type=int,
        metavar="N",
        default=None,
        help="Embed only the first N messages (quick end-to-end check).",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Encode batch size. Default: 32 on CUDA, 16 on CPU.",
    )
    ap.add_argument(
        "--max-seq-len",
        type=int,
        default=DEFAULT_MAX_SEQ,
        help=f"Truncate each message to this many tokens (default {DEFAULT_MAX_SEQ}).",
    )
    ap.add_argument(
        "--token-budget",
        type=int,
        default=6144,
        help="Max (batch_count x max_seq_in_batch) tokens per batch; bounds attention "
             "memory regardless of message length (default 6144).",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Re-embed every message. Default is incremental: keep existing vectors, "
             "drop ones whose message is gone, and embed only NEW messages.",
    )
    args = ap.parse_args()

    device = common.pick_device()
    if device == "cuda":
        # Cap the allocator so a runaway batch OOMs CLEANLY in-process instead of
        # spilling into Windows shared memory (system RAM) and crashing the session.
        try:
            import torch

            torch.cuda.set_per_process_memory_fraction(0.85)
        except Exception:  # noqa: BLE001
            pass

    # Load messages (ids + texts) honoring --smoke.
    msgs = list(common.iter_messages(limit=args.smoke))
    if not msgs:
        common.log("error: no messages to embed (messages.jsonl empty or missing).")
        return 1

    # --- incremental: keep existing vectors, prune gone messages, embed only new ---
    kept_ids: list[str] = []
    kept_mat = np.zeros((0, common.EMBED_DIM), dtype=np.float32)
    incremental = (not args.full and args.smoke is None
                   and common.EMBEDDINGS_PATH.exists() and common.IDS_PATH.exists())
    if incremental:
        try:
            old_ids, old_mat = common.read_embeddings()
            cur = {m.id for m in msgs}
            keep = [i for i, mid in enumerate(old_ids) if mid in cur]
            kept_ids = [old_ids[i] for i in keep]
            kept_mat = old_mat[keep] if keep else kept_mat
            have = set(kept_ids)
            msgs = [m for m in msgs if m.id not in have]
            common.log(f"incremental: kept {len(kept_ids)}, pruned {len(old_ids) - len(kept_ids)}, "
                       f"new {len(msgs)}")
            if not msgs:
                if len(kept_ids) != len(old_ids):
                    common.write_embeddings(kept_ids, kept_mat, dim=common.EMBED_DIM)
                    common.log("pruned stale rows; nothing new to embed.")
                else:
                    common.log("embeddings already up to date (0 new).")
                return 0
        except Exception as e:  # noqa: BLE001 — corrupt/old index -> fall back to full
            common.log(f"incremental read failed ({e}); doing a full re-embed.")
            kept_ids, kept_mat = [], np.zeros((0, common.EMBED_DIM), dtype=np.float32)

    ids = [m.id for m in msgs]
    texts = [m.text for m in msgs]

    common.log(f"device={device} token_budget={args.token_budget} max_seq_len={args.max_seq_len} "
               f"messages={len(msgs)}"
               + (f" (smoke, first {args.smoke})" if args.smoke else ""))

    resolved = common.resolve_model(
        CANDIDATES, build_loader(device, args.max_seq_len), label="embed"
    )

    t0 = time.perf_counter()
    native = embed_passages(resolved.obj, texts, device, args.token_budget)
    # Truncate to the contract dim, then renormalize each row.
    mat = common.matryoshka_truncate(native, dim=common.EMBED_DIM)
    if kept_ids:  # splice the freshly-embedded rows onto the retained ones
        ids = kept_ids + ids
        mat = np.vstack([kept_mat, mat])
    common.write_embeddings(ids, mat, dim=common.EMBED_DIM)
    elapsed = time.perf_counter() - t0

    vram = common.approx_vram_mb()
    vram_str = f"{vram:.0f} MiB" if vram is not None else "n/a (cpu)"

    # Summary to stderr (stdout stays clean).
    branch = "qwen3:last-token+left-pad" if is_qwen(resolved.id) else (
        "bge:mean" if is_bge(resolved.id) else "minilm:mean"
    )
    common.log(
        "embed done | "
        f"model={resolved.id} ({branch}) | device={device} | "
        f"count={len(ids)} | native_dim={native.shape[1]} -> dim={mat.shape[1]} | "
        f"elapsed={elapsed:.2f}s | approx_vram={vram_str}"
    )
    common.log(f"wrote {common.EMBEDDINGS_PATH} and {common.IDS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
