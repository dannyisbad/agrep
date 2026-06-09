"""Embed per-session SUMMARIES (from summarize.py) -> data/summary_emb.f32 + .ids.

These chat-level vectors are what make "what was this chat about" search good: a
summary sentence ("shader byte-swap to draw ESP outlines, evading Vanguard") embeds
far more sharply to a natural-language query than a raw-message centroid does.

Run AFTER summarize.py. Usage: python embed_summaries.py
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

import common
from embed import CANDIDATES, build_loader

F32 = "summary_emb.f32"
IDS = "summary_emb.ids"
META = "summary_emb.meta"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="Re-embed every summary. Default is incremental: keep existing "
                         "vectors, embed only summaries not already in summary_emb.ids.")
    args = ap.parse_args()

    device = common.pick_device()
    if device == "cuda":
        try:
            import torch

            torch.cuda.set_per_process_memory_fraction(0.85)
        except Exception:  # noqa: BLE001
            pass

    path = common.DATA_DIR / "summaries.jsonl"
    if not path.exists():
        common.log("no data/summaries.jsonl; run summarize.py first")
        return 1
    recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [r for r in recs if (r.get("summary") or r.get("tags"))]
    sids = [r["session"] for r in recs]
    # embed summary + tags together (tags add topical keywords the sentence may omit)
    texts = [(r.get("summary", "") + " . tags: " + ", ".join(r.get("tags", []))).strip() for r in recs]

    # --- incremental: keep existing vectors, prune gone, embed only new summaries ---
    f32p, idsp, metap = (common.DATA_DIR / F32, common.DATA_DIR / IDS, common.DATA_DIR / META)
    kept_ids: list[str] = []
    kept_mat = np.zeros((0, common.EMBED_DIM), dtype=np.float32)
    incremental = not args.full and f32p.exists() and idsp.exists()
    if incremental:
        try:
            old_ids = idsp.read_text(encoding="utf-8").splitlines()
            old_mat = np.fromfile(f32p, dtype="<f4").reshape(-1, common.EMBED_DIM)
            cur = set(sids)
            keep = [i for i, s in enumerate(old_ids) if s in cur and i < old_mat.shape[0]]
            kept_ids = [old_ids[i] for i in keep]
            kept_mat = old_mat[keep] if keep else kept_mat
            have = set(kept_ids)
            order = [i for i, s in enumerate(sids) if s not in have]
            sids = [sids[i] for i in order]
            texts = [texts[i] for i in order]
            common.log(f"incremental: kept {len(kept_ids)}, new {len(sids)}")
            if not sids:
                if len(kept_ids) != len(old_ids):
                    _write(kept_ids, kept_mat, f32p, idsp, metap)
                    common.log("pruned stale summary vectors; nothing new to embed.")
                else:
                    common.log("summary embeddings already up to date (0 new).")
                return 0
        except Exception as e:  # noqa: BLE001
            common.log(f"incremental read failed ({e}); full re-embed.")
            kept_ids, kept_mat = [], np.zeros((0, common.EMBED_DIM), dtype=np.float32)

    resolved = common.resolve_model(CANDIDATES, build_loader(device), label="embed-sum")
    t0 = time.perf_counter()
    emb = resolved.obj.encode(texts, batch_size=32, show_progress_bar=False,
                              convert_to_numpy=True, normalize_embeddings=False, device=device)
    mat = common.matryoshka_truncate(np.asarray(emb, dtype=np.float32), dim=common.EMBED_DIM)
    if kept_ids:
        sids = kept_ids + sids
        mat = np.vstack([kept_mat, mat])

    _write(sids, mat, f32p, idsp, metap)
    common.log(f"embedded {len(sids)} summaries -> summary_emb.f32 (dim={common.EMBED_DIM}) "
               f"in {time.perf_counter()-t0:.1f}s using {resolved.id}")
    return 0


def _write(sids, mat, f32p, idsp, metap):
    np.ascontiguousarray(mat, dtype="<f4").tofile(f32p)
    metap.write_text(str(common.EMBED_DIM), encoding="utf-8")
    with idsp.open("w", encoding="utf-8", newline="\n") as f:
        for s in sids:
            f.write(s + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
