#!/usr/bin/env python
"""tilt reindex - one command to (re)build the whole index.

By default every stage is INCREMENTAL: the Rust ingest is always fast, and the
GPU/LLM stages skip messages/sessions they've already processed, so re-running
after a few new chats takes seconds instead of minutes. Pass --full to recompute
everything from scratch.

Each stage runs as its OWN process so a model's VRAM is released before the next
stage loads its model (matters on consumer-VRAM GPUs). Stages, in order:

    cargo build (release)         -> the agrep-rs ingest binary
    agrep-rs index                -> data/messages.jsonl + data/replies.jsonl + data/events/
    embed.py                      -> data/embeddings.{f32,ids}        (incremental)
    emotion.py                    -> data/emotions.jsonl              (incremental)
    judge.py                      -> LLM verdicts onto gate-routed rows (incremental)
    summarize.py                  -> data/summaries.jsonl             (incremental)
    embed_summaries.py            -> data/summary_emb.*               (incremental)
    concepts.py --source summary  -> data/concepts.json + session_concepts.jsonl
    label_concepts.py             -> Gemma4 topic names (+ dedupe merge)
    vibe.py                       -> data/vibe/*.json (top arcs)

Usage:
    python reindex.py                # incremental rebuild
    python reindex.py --full         # recompute every stage
    python reindex.py --no-build     # skip cargo build (binary already current)

Run it with any Python; it auto-selects the smart-tier venv for the heavy stages.
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
sys.path.insert(0, str(ROOT / "py"))
import common  # noqa: E402  -- single source for binary / venv / data paths

# the venv that has torch/sentence-transformers/sklearn; fall back to whatever ran us
PY = common.venv_python()
TILT = common.ingest_bin()


def run(desc: str, cmd: list[str], optional: bool = False) -> bool:
    print(f"\n=== {desc} ===", flush=True)
    t = time.perf_counter()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        if optional:
            # ML/LLM stages are enhancements, not requirements: a machine without the
            # py deps / GPU / ollama still gets a fully usable explorer (browse, keyword
            # search, events, live view) straight from the Rust ingest.
            print(f"  ! {desc} failed (exit {r.returncode}); skipping -- the explorer "
                  f"works without this stage.", flush=True)
            return False
        print(f"  ! {desc} failed (exit {r.returncode}); stopping.", flush=True)
        sys.exit(r.returncode)
    print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="rebuild the tilt index")
    ap.add_argument("--full", action="store_true",
                    help="recompute every stage instead of incrementally")
    ap.add_argument("--judge", action="store_true",
                    help="run the LLM affect judge over gate-routed rows (slow, optional)")
    ap.add_argument("--no-build", action="store_true", help="skip cargo build")
    ap.add_argument("--max-new", type=int, default=None,
                    help="cap summarize at N sessions this run (newest first); used by the "
                         "UI reindex button so one click is bounded, not a multi-hour grind")
    args = ap.parse_args()
    full = ["--full"] if args.full else []

    t0 = time.perf_counter()
    if not args.no_build:
        run("build rust (release)", ["cargo", "build", "--release"])
    # --full also bypasses the Rust per-file parse cache (clean re-parse of every store file)
    run("ingest transcripts (rust)", [str(TILT), "index", "--agent", "all", *full])

    # Fast path: if ingest produced byte-identical messages (no new chats since last
    # run), the whole downstream pipeline would reproduce what's already on disk, so
    # skip it. --full forces a rebuild regardless.
    msgs = common.MESSAGES_PATH
    sig_file = common.DATA_DIR / ".reindex.sig"
    sig = ""
    if msgs.exists():
        sig = f"{msgs.stat().st_size}:" + hashlib.md5(msgs.read_bytes()).hexdigest()
    if not args.full and sig and sig_file.exists() and sig_file.read_text().strip() == sig:
        print(f"\n  no new messages since last index - already up to date. "
              f"({time.perf_counter() - t0:.0f}s)")
        print("  (pass --full to rebuild embeddings/affect/topics/arcs anyway.)")
        return 0

    # Everything below is an ENHANCEMENT layer (GPU embeddings, affect, LLM summaries,
    # arcs). Each is optional: failures warn and skip, because the explorer is already
    # fully usable from the Rust ingest alone. Stages also respect their own
    # incrementality, and vibe arcs self-detect staleness (sig over the affect series),
    # so affect changes propagate automatically on the next run.
    ok_emb = run("embed messages", [PY, "py/embed.py", *full], optional=True)
    ok_emo = run("affect gate", [PY, "py/emotion.py", *full], optional=True)
    if ok_emo:
        if args.judge:  # correction layer on the affect gate; opt-in, it's slow
            run("affect judge (LLM, routed msgs)", [PY, "py/judge.py"], optional=True)
    cap = ["--max-new", str(args.max_new)] if args.max_new else []
    ok_sum = run("summarize sessions", [PY, "py/summarize.py", *full, *cap], optional=True)
    if ok_sum and ok_emb:
        run("embed summaries", [PY, "py/embed_summaries.py", *full], optional=True)
        run("cluster concepts", [PY, "py/concepts.py", "--source", "summary"], optional=True)
        run("name concepts", [PY, "py/label_concepts.py"], optional=True)
    if ok_sum:
        run("backfill titles", [PY, "py/titles.py"], optional=True)
    if ok_emo:
        # arcs for every substantive session, not a top-24 leaderboard: the explorer
        # shows a vibe-trace on any chat that has one, so coverage beats curation.
        run("vibe arcs", [PY, "py/vibe.py", "--top", "999", "--min-turns", "8"], optional=True)

    # Record what we just fully processed, so an unchanged re-run is instant. A capped
    # run (--max-new) may have left summaries pending, so don't stamp it complete -
    # the next run must reach the summarize stage again to drain the backlog.
    if sig and not args.max_new:
        sig_file.write_text(sig, encoding="utf-8")

    print(f"\n  reindex complete in {time.perf_counter() - t0:.0f}s.")
    print("  restart the server (python py/server.py) to load the new index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
