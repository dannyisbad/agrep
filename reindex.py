#!/usr/bin/env python
"""tilt reindex — one command to (re)build the whole index.

By default every stage is INCREMENTAL: the Rust ingest is always fast, and the
GPU/LLM stages skip messages/sessions they've already processed, so re-running
after a few new chats takes seconds instead of minutes. Pass --full to recompute
everything from scratch.

Each stage runs as its OWN process so a model's VRAM is released before the next
stage loads its model (important on the 10GB GPU). Stages, in order:

    cargo build (release)         -> the tilt binary
    tilt index                    -> data/messages.jsonl + data/replies.jsonl
    embed.py                      -> data/embeddings.{f32,ids}        (incremental)
    emotion.py                    -> data/emotions.jsonl              (incremental)
    summarize.py                  -> data/summaries.jsonl             (incremental)
    embed_summaries.py            -> data/summary_emb.*               (incremental)
    concepts.py --source summary  -> data/concepts.json + session_concepts.jsonl
    label_concepts.py             -> Gemma4 topic names (+ dedupe merge)
    vibe.py                       -> data/vibe/*.json (top arcs)

Usage:
    python reindex.py                # incremental rebuild
    python reindex.py --full         # recompute every stage
    python reindex.py --no-build     # skip cargo build (binary already current)

Run it with any Python; it auto-selects py/.venv for the heavy stages.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
# the venv that has torch/sentence-transformers/sklearn; fall back to whatever ran us
VENV_PY = ROOT / "py" / ".venv" / ("Scripts" if WIN else "bin") / ("python.exe" if WIN else "python")
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
TILT = ROOT / "target" / "release" / ("tilt.exe" if WIN else "tilt")


def run(desc: str, cmd: list[str]) -> None:
    print(f"\n=== {desc} ===", flush=True)
    t = time.perf_counter()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"  ! {desc} failed (exit {r.returncode}); stopping.", flush=True)
        sys.exit(r.returncode)
    print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="rebuild the tilt index")
    ap.add_argument("--full", action="store_true",
                    help="recompute every stage instead of incrementally")
    ap.add_argument("--no-build", action="store_true", help="skip cargo build")
    args = ap.parse_args()
    full = ["--full"] if args.full else []

    t0 = time.perf_counter()
    if not args.no_build:
        run("build rust (release)", ["cargo", "build", "--release"])
    run("ingest transcripts (rust)", [str(TILT), "index", "--agent", "all"])

    # Fast path: if ingest produced byte-identical messages (no new chats since last
    # run), the whole downstream pipeline would reproduce what's already on disk, so
    # skip it. --full forces a rebuild regardless.
    msgs = ROOT / "data" / "messages.jsonl"
    sig_file = ROOT / "data" / ".reindex.sig"
    sig = ""
    if msgs.exists():
        sig = f"{msgs.stat().st_size}:" + hashlib.md5(msgs.read_bytes()).hexdigest()
    if not args.full and sig and sig_file.exists() and sig_file.read_text().strip() == sig:
        print(f"\n  no new messages since last index — already up to date. "
              f"({time.perf_counter() - t0:.0f}s)")
        print("  (pass --full to rebuild embeddings/affect/topics/arcs anyway.)")
        return 0

    run("embed messages", [PY, "py/embed.py", *full])
    run("affect gate", [PY, "py/emotion.py", *full])
    run("summarize sessions", [PY, "py/summarize.py", *full])
    run("embed summaries", [PY, "py/embed_summaries.py", *full])
    run("cluster concepts", [PY, "py/concepts.py", "--source", "summary"])
    run("name concepts", [PY, "py/label_concepts.py"])
    run("vibe arcs", [PY, "py/vibe.py"])

    if sig:  # record what we just fully processed, so an unchanged re-run is instant
        sig_file.write_text(sig, encoding="utf-8")

    print(f"\n  reindex complete in {time.perf_counter() - t0:.0f}s.")
    print("  restart the server (python py/server.py) to load the new index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
