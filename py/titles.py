"""Backfill scannable titles onto existing summaries.jsonl records (Gemma 4 via Ollama).

summarize.py now emits a `title` per new session; this gives every already-summarized
record one too, derived from its summary text (cheap: one short prompt per record, no
re-read of the transcript). Incremental: records that already have a non-empty title are
left untouched. The file is rewritten atomically when done.

Usage: python titles.py [--smoke 8]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

import common
from summarize import OLLAMA, pick_model  # same model discovery as the summarizer

SYS = ("You write scannable titles for a developer's chat-session summaries. Output ONLY the "
       "title: a specific 3-8 word noun phrase naming the actual tech/project/task. No quotes, "
       "no trailing period, no filler words like 'AI assistant', 'session', 'agent named', "
       "'the developer'. Examples: 'Candence semantic review prompts' / 'opencode dynamic "
       "workflow engine' / 'Rowhammer PTE attack research'.")


def gen_title(model: str, body: str) -> str:
    payload = {"model": model, "stream": False,
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": body}],
               "options": {"num_ctx": 2048, "temperature": 0.3}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())["message"]["content"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=None, help="print N titles, write nothing")
    args = ap.parse_args()

    path = common.DATA_DIR / "summaries.jsonl"
    recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = [r for r in recs if not r.get("title")]
    if args.smoke:
        todo = todo[: args.smoke]
    if not todo:
        print("  every summary already has a title.")
        return 0

    model = pick_model()
    common.log(f"model={model} backfilling {len(todo)} titles (of {len(recs)} summaries)")
    t0 = time.perf_counter()
    for i, r in enumerate(todo, 1):
        body = (f"Summary: {r.get('summary', '')}\n"
                f"Tags: {', '.join(r.get('tags', []))}\n\nTitle:")
        try:
            out = gen_title(model, body).strip()
        except Exception as e:  # noqa: BLE001
            common.log(f"  warn: gen failed for {r['session'][:12]}: {e}")
            out = ""
        # first line, stripped of quote/markdown wrap; clamp runaway outputs
        title = (out.splitlines()[0] if out else "").strip().strip('"*#`').strip().rstrip(".")
        if len(title.split()) > 12:
            title = " ".join(title.split()[:12])
        r["title"] = title
        if args.smoke:
            print(f"  {title}\n    <- {r.get('summary', '')[:90]}")
        elif i % 25 == 0:
            common.log(f"  ... {i}/{len(todo)} ({i/(time.perf_counter()-t0):.2f}/s)")

    if args.smoke:
        return 0
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    tmp.replace(path)
    print(f"  wrote {len(todo)} titles -> {path} in {time.perf_counter()-t0:.0f}s using {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
