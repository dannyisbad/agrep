"""Per-session summaries -> data/summaries.jsonl  (Phase C, Gemma 4 via Ollama).

A specific 1-2 sentence summary + concept tags per session. These are the chat-level
signal behind chat search and the concept map. Thresholded: only sessions with
>= --min-msgs real messages. Run AFTER `agrep index`.

Usage:
  python summarize.py --smoke 6
  python summarize.py --min-msgs 5
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from collections import defaultdict

import common

MODELS = ["gemma4:e4b-it-qat", "gemma4:e4b", "qwen2.5:3b-instruct"]
OLLAMA = "http://localhost:11434/api/chat"

SYS = ("You summarize a developer's chat session with an AI coding agent. Be specific and "
       "factual: name the actual tech, project, and goal. One or two sentences. Start DIRECTLY "
       "with the action verb (e.g. 'Building...', 'Debugging...', 'Reverse-engineering...'). Do "
       "NOT begin with 'The developer', 'The user', 'The session', or 'This session'. No preamble, "
       "no hedging, no generic filler.")
PROMPT = ("Below are the developer's messages from one session, in order. Summarize what they "
          "were actually building or debugging in 1-2 specific sentences, then a line "
          "'Title: ...' with a scannable 3-8 word noun-phrase title (name the actual tech/project; "
          "no filler like 'AI assistant' or 'session about'), then a final line "
          "'Tags: a, b, c' of 3-6 short lowercase topic tags (concrete tech/project nouns).\n\n"
          "MESSAGES:\n{body}")


def load_sessions(min_msgs: int):
    rows = defaultdict(list)
    agent, cwd = {}, {}
    last_ts = defaultdict(int)  # epoch ms of each session's latest message
    with common.MESSAGES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = o.get("session") or o.get("id")
            rows[s].append(o.get("text", ""))
            agent.setdefault(s, o.get("agent", ""))
            cwd.setdefault(s, o.get("project", ""))
            ts = o.get("ts") or 0
            if isinstance(ts, (int, float)) and ts > last_ts[s]:
                last_ts[s] = int(ts)
    # newest sessions first, so a capped run (--max-new) titles recent work, not 2024
    sel = sorted(((s, m) for s, m in rows.items() if len(m) >= min_msgs),
                 key=lambda x: last_ts[x[0]], reverse=True)
    return sel, agent, cwd


def build_body(msgs, max_msgs=34, per=260):
    keep = msgs if len(msgs) <= max_msgs else msgs[: max_msgs * 2 // 3] + msgs[-max_msgs // 3:]
    out = []
    for m in keep:
        t = " ".join(m.split())
        if t:
            out.append("- " + (t if len(t) <= per else t[:per] + "…"))
    return "\n".join(out)[:9000]


def gen(model, body):
    payload = {"model": model, "stream": False,
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": PROMPT.format(body=body)}],
               "options": {"num_ctx": 8192, "temperature": 0.3},
               "keep_alive": os.environ.get("AGREP_OLLAMA_KEEP_ALIVE", "5m")}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())["message"]["content"]


def pick_model():
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10) as r:
            have = {m["name"] for m in json.loads(r.read().decode()).get("models", [])}
    except Exception as e:  # noqa: BLE001
        common.log(f"ollama not reachable ({e}); start `ollama serve`")
        raise SystemExit(1)
    for m in MODELS:
        hit = next((h for h in have if h == m or h.startswith(m.split(":")[0])), None)
        if hit:
            return hit
    common.log(f"no model; ollama pull {MODELS[0]} (have={have})")
    raise SystemExit(1)


def parse(text):
    text = text.strip()
    idx = text.lower().rfind("tags:")
    tags = []
    summary = text
    if idx != -1:
        tail = text[idx + 5:]
        tagline = tail.splitlines()[0] if tail.strip() else ""
        tags = [t.strip().lstrip("-*# ").lower() for t in tagline.split(",") if t.strip()]
        summary = text[:idx].strip()
    title = ""
    tidx = summary.lower().rfind("title:")
    if tidx != -1:
        tail = summary[tidx + 6:]
        title = (tail.splitlines()[0] if tail.strip() else "").strip().strip('"*# ').strip()
        summary = summary[:tidx].strip()
    return summary.strip(), title, tags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-msgs", type=int, default=5)
    ap.add_argument("--smoke", type=int, default=None)
    ap.add_argument("--full", action="store_true",
                    help="Re-summarize every session. Default is incremental: only "
                         "summarize sessions not already in summaries.jsonl.")
    ap.add_argument("--max-new", type=int, default=None,
                    help="Cap this run at N sessions (newest first). Lets the UI reindex "
                         "button make bounded progress instead of grinding a huge backlog.")
    args = ap.parse_args()

    model = pick_model()
    sel, agent, cwd = load_sessions(args.min_msgs)
    if args.smoke:
        sel = sel[: args.smoke]

    out_path = common.DATA_DIR / "summaries.jsonl"
    incremental = not args.full and args.smoke is None and out_path.exists()
    if incremental:
        done = common.jsonl_ids(out_path, key="session")
        before = len(sel)
        sel = [(s, m) for s, m in sel if s not in done]
        common.log(f"incremental: {len(done)} already summarized, {len(sel)} new (of {before})")
        if not sel:
            print(f"  summaries already up to date (0 new) -> {out_path}")
            return 0
    if args.max_new is not None and len(sel) > args.max_new:
        common.log(f"capping at {args.max_new} newest of {len(sel)} pending")
        sel = sel[: args.max_new]

    common.log(f"model={model} sessions={len(sel)} (min_msgs={args.min_msgs})")
    f_out = None if args.smoke else out_path.open("a" if incremental else "w", encoding="utf-8")
    t0 = time.perf_counter()
    done = 0
    for s, msgs in sel:
        body = build_body(msgs)
        try:
            text = gen(model, body)
        except Exception as e:  # noqa: BLE001
            text = ""
            common.log(f"  warn: gen failed for {s[:12]}: {e}")
        summary, title, tags = parse(text)
        if not summary:  # never write empty: fall back to first substantive line
            summary = next((" ".join(m.split())[:200] for m in msgs if len(m.split()) > 4), "(no summary)")
        rec = {"session": s, "agent": agent[s], "cwd_project": cwd[s],
               "n_msgs": len(msgs), "title": title, "summary": summary, "tags": tags}
        done += 1
        if args.smoke:
            print(f"\n[{agent[s]} · {cwd[s]} · {len(msgs)} msgs]\n  {title}\n  {summary}\n  tags: {', '.join(tags)}")
        else:
            f_out.write(json.dumps(rec) + "\n")
            f_out.flush()  # a killed/timed-out run keeps everything generated so far
            if done % 25 == 0:
                common.log(f"  ... {done}/{len(sel)} ({done/(time.perf_counter()-t0):.2f}/s)")
    if f_out:
        f_out.close()
        print(f"\n  wrote {done} summaries -> {out_path} in {time.perf_counter()-t0:.0f}s using {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
