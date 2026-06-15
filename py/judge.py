"""LLM affect judge: the second tier the emotion gate routes to.

emotion.py flags messages it can't confidently read (profanity + ambiguous
rage/hype) with routed_to_judge=true. This stage feeds exactly those messages to
a local LLM (Gemma 4 via Ollama) with a developer-chat rubric and OVERWRITES
their rage_raw/hype_raw in data/emotions.jsonl with the judge's graded verdict.
The gate's original numbers are preserved as rage_gate/hype_gate and the row is
marked judged=true, so re-runs skip already-judged rows (incremental by default).

Why an LLM here: encoder classifiers chronically misread dev-slang profanity --
"this is sick as fuck" is praise, "this fucking sucks" is rage, and the gate
admits it can't tell. A small instruction-tuned model with three lines of rubric
gets these right, and at ~6% of messages the cost is a one-time batch.

Downstream (explore/report/vibe) keeps reading rage_raw/hype_raw unchanged.

Usage:
  python judge.py              # judge all routed, not-yet-judged messages
  python judge.py --smoke 8    # print 8 verdicts, write nothing
  python judge.py --limit 500  # cap this run (resume later; incremental)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request

import common
from summarize import OLLAMA, pick_model  # same model discovery as the summarizer

SYS = (
    "You read single messages a software developer typed to a coding agent and grade the "
    "developer's emotion. Dev slang matters: 'sick as fuck', 'goes hard', 'insane' as praise "
    "are POSITIVE; swearing at tools/bugs/agents ('this fucking sucks', 'why is it broken "
    "AGAIN') is NEGATIVE; neutral technical text with incidental profanity is neither. "
    'Output ONLY JSON: {"rage": <0.0-1.0>, "hype": <0.0-1.0>} where rage = '
    "frustration/anger/disapproval intensity and hype = excitement/approval/delight "
    "intensity. Both may be low; both are rarely high."
)

_JSON_RE = re.compile(r'\{[^{}]*"rage"[^{}]*\}')


def ask_judge(model: str, text: str) -> tuple[float, float] | None:
    """One verdict. Returns (rage, hype) in [0,1], or None on any failure."""
    payload = {
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": text[:2000]}],
        "options": {"num_ctx": 1024, "temperature": 0.0},
        "format": "json",
        "keep_alive": os.environ.get("AGREP_OLLAMA_KEEP_ALIVE", "5m"),
    }
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode())["message"]["content"]
        m = _JSON_RE.search(out)
        o = json.loads(m.group(0) if m else out)
        rage, hype = float(o["rage"]), float(o["hype"])
    except Exception:  # noqa: BLE001 -- malformed output => skip, keep gate's read
        return None
    clamp = lambda v: max(0.0, min(1.0, v))  # noqa: E731
    return clamp(rage), clamp(hype)


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM judge for gate-routed messages")
    ap.add_argument("--smoke", type=int, default=None, help="judge N, print, write nothing")
    ap.add_argument("--limit", type=int, default=None, help="cap how many to judge this run")
    args = ap.parse_args()

    rows = [json.loads(l) for l in
            common.EMOTIONS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = {m.id: m.text for m in common.iter_messages()}
    # Skip compaction recaps: machine-written continuation context, not the user's affect
    # (the gate routes them when the recap quotes profanity).
    RECAP = "This session is being continued from a previous conversation"
    todo = [r for r in rows if r.get("routed_to_judge") and not r.get("judged")
            and r["id"] in texts and not texts[r["id"]].startswith(RECAP)]
    if args.smoke:
        todo = todo[: args.smoke]
    elif args.limit:
        todo = todo[: args.limit]
    if not todo:
        common.log("judge: nothing routed and unjudged. done.")
        return 0

    model = pick_model()
    common.log(f"judge: model={model} verdicts={len(todo)} "
               f"(of {sum(1 for r in rows if r.get('routed_to_judge'))} routed)")
    t0 = time.perf_counter()
    done = fails = 0
    for r in todo:
        v = ask_judge(model, texts[r["id"]])
        if v is None:
            fails += 1
            continue
        rage, hype = v
        if args.smoke:
            print(f"  rage={rage:.2f} hype={hype:.2f}  (gate {r['rage_raw']:.2f}/"
                  f"{r['hype_raw']:.2f})  {' '.join(texts[r['id']].split())[:90]!r}")
            continue
        r["rage_gate"], r["hype_gate"] = r["rage_raw"], r["hype_raw"]
        # The gate's group sums range ~0..2.5; the judge grades 0..1. Scale the judge
        # onto the gate's scale so one judged message ranks comparably in heat sorts.
        r["rage_raw"], r["hype_raw"] = round(rage * 1.5, 6), round(hype * 1.5, 6)
        r["judged"] = True
        done += 1
        if done % 50 == 0:
            common.log(f"  ... {done}/{len(todo)} ({done/(time.perf_counter()-t0):.2f}/s)")

    if not args.smoke:
        tmp = common.EMOTIONS_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        common.replace_with_retry(tmp, common.EMOTIONS_PATH)
        common.log(f"judge: {done} verdicts written ({fails} failed parses) "
                   f"in {time.perf_counter()-t0:.0f}s -> {common.EMOTIONS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
