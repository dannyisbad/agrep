#!/usr/bin/env python3
"""Fabricate a synthetic embedding index to validate the Rust search spine WITHOUT a GPU.

Pure stdlib (struct + math + json + random) — no torch, no numpy. Writes the exact
contract artifacts the Rust reader expects:

  data/embeddings.f32  : raw little-endian f32, row-major, N x D, each ROW L2-normalized.
  data/embeddings.ids  : UTF-8, one message id per line; row r <-> line r.
  data/query.f32       : a single D-dim L2-normalized f32 vector == one planted row.

The query is set EQUAL to a specific planted row, so that row's cosine == 1.0 and it
MUST rank #1. This exercises the AVX2 dot kernel + mmap index + id mapping + CLI join.

Usage:
  python synth_index.py            # 200 rows, plant row 137
  python synth_index.py --rows 50000 --plant 31337
"""
from __future__ import annotations

import argparse
import json
import math
import random
import struct
from pathlib import Path

DIM = 256
DATA = Path(__file__).resolve().parent / "data"


def l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n <= 0.0:
        return v
    return [x / n for x in v]


def make_row(seed: int, dim: int) -> list[float]:
    """Deterministic pseudo-vector from the row seed (reproducible, no clock)."""
    rng = random.Random(seed * 2654435761 + 12345)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


def real_ids(n: int) -> list[str]:
    """Sample n real ids from data/messages.jsonl if present, else synthesize."""
    msgs = DATA / "messages.jsonl"
    ids: list[str] = []
    if msgs.exists():
        with msgs.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = obj.get("id")
                if mid:
                    ids.append(mid)
                if len(ids) >= n:
                    break
    # Pad with synthetic ids if messages.jsonl is short / absent.
    while len(ids) < n:
        ids.append(f"synthetic:row:{len(ids)}")
    return ids[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--plant", type=int, default=137)
    args = ap.parse_args()

    rows = args.rows
    plant = args.plant % rows
    DATA.mkdir(parents=True, exist_ok=True)

    ids = real_ids(rows)

    # Stream rows to embeddings.f32 as little-endian f32; capture the planted row.
    emb_path = DATA / "embeddings.f32"
    planted: list[float] | None = None
    with emb_path.open("wb") as f:
        for r in range(rows):
            row = l2_normalize(make_row(r, DIM))
            if r == plant:
                planted = row[:]
            f.write(struct.pack(f"<{DIM}f", *row))

    assert planted is not None

    (DATA / "embeddings.ids").write_text("\n".join(ids) + "\n", encoding="utf-8", newline="\n")

    # query.f32 == the planted row exactly -> cosine 1.0, must rank #1.
    (DATA / "query.f32").write_bytes(struct.pack(f"<{DIM}f", *planted))

    print(f"rows={rows} dim={DIM} plant_row={plant} planted_id={ids[plant]}")
    print(f"wrote {emb_path} ({emb_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
