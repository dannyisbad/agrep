"""Vibe-trace: per-chat emotional arc + LLM-annotated turning points -> data/vibe/.

Reads per-message affect (emotions.jsonl) + messages.jsonl, builds each substantive
session's valence/rage arc over its turns, ranks the juiciest chats (peak rage, widest
swing, longest sustained-rage run), locates the turning points, and uses a small local
LLM to write a dry one-line verdict + label what triggered each shift. The HTML renderer
turns these JSONs into the vibe-trace.

Run AFTER emotion.py. Usage: python vibe.py [--top 24] [--min-turns 8] [--turning 3]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict

import numpy as np

import common

# small local LLM for annotation. gemma 3n e4b is gated (needs HF auth) -> falls back to
# the already-present Qwen2.5-3B if it can't load.
LLM_CANDIDATES = ["google/gemma-3n-e4b-it", "Qwen/Qwen2.5-3B-Instruct"]


def smooth(x: np.ndarray, w: int = 3) -> np.ndarray:
    if len(x) < w:
        return x
    k = np.ones(w) / w
    return np.convolve(x, k, mode="same")


def longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="build vibe-trace arcs")
    ap.add_argument("--top", type=int, default=24)
    ap.add_argument("--min-turns", type=int, default=8)
    ap.add_argument("--turning", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true", help="skip LLM annotation (arcs only)")
    args = ap.parse_args()

    emo: dict[str, dict] = {}
    with (common.DATA_DIR / "emotions.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                o = json.loads(line)
                emo[o["id"]] = o

    sess: dict[str, list] = defaultdict(list)
    meta: dict[str, tuple[str, str]] = {}
    with common.MESSAGES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            s = o.get("session") or o["id"]
            sess[s].append((o.get("turn", 0), o["id"], o.get("ts", 0), o.get("text", "")))
            meta.setdefault(s, (o.get("agent", ""), o.get("project", "")))

    arcs = []
    for s, msgs in sess.items():
        if len(msgs) < args.min_turns:
            continue
        msgs.sort(key=lambda m: m[0])
        val, rage, texts, ids = [], [], [], []
        for (_turn, mid, _ts, text) in msgs:
            e = emo.get(mid)
            if not e:
                continue
            r = float(e.get("rage_raw", 0.0))
            h = float(e.get("hype_raw", 0.0))
            val.append(h - r)
            rage.append(r)
            texts.append(text)
            ids.append(mid)
        if len(val) < args.min_turns:
            continue
        val = np.array(val)
        rage = np.array(rage)
        sm = smooth(val)
        deltas = np.abs(np.diff(sm))
        n_tp = min(args.turning, len(deltas))
        tps = sorted(int(i) + 1 for i in np.argsort(-deltas)[:n_tp]) if n_tp else []
        peak_turn = int(rage.argmax())
        juice = float(rage.max() * 2 + val.std() * 3 + (rage > 0.4).mean() * 2
                      + longest_run(rage > 0.4) / max(len(rage), 1))
        arcs.append({
            "session": s, "agent": meta[s][0], "project": meta[s][1],
            "n_turns": len(val),
            "valence": [round(float(v), 4) for v in val],
            "rage": [round(float(r), 4) for r in rage],
            "peak_turn": peak_turn,
            "swing": round(float(val.std()), 4),
            "drift": round(float(val[-3:].mean() - val[:3].mean()), 4),
            "juice": round(juice, 4),
            "turning_points": tps,
            "_texts": texts,
        })

    arcs.sort(key=lambda a: a["juice"], reverse=True)
    top = arcs[: args.top]
    common.log(f"{len(arcs)} arcs (>= {args.min_turns} turns); annotating top {len(top)} by juice")

    if not args.no_llm and top:
        annotate(top)

    # write per-session arcs + an index
    vdir = common.DATA_DIR / "vibe"
    vdir.mkdir(exist_ok=True)
    index = []
    for a in top:
        texts = a.pop("_texts")
        # attach short per-turn snippets for the renderer tooltips
        a["snippets"] = [" ".join(t.split())[:140] for t in texts]
        (vdir / f"{a['session']}.json").write_text(json.dumps(a), encoding="utf-8")
        index.append({k: a[k] for k in ("session", "agent", "project", "n_turns",
                                        "peak_turn", "swing", "drift", "juice",
                                        "verdict") if k in a})
    (vdir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"\n  wrote {len(top)} vibe-traces -> {vdir}")
    for a in top[:10]:
        v = a.get("verdict", "")
        print(f"  juice {a['juice']:>5.2f}  {a['agent']}/{a['project'][:18]:<18}  {a['n_turns']:>3}t  {v[:70]}")
    print()
    return 0


def _ollama_gen(system: str, user: str) -> str:
    import urllib.request
    for model in ("gemma4:e4b-it-qat", "gemma4:e4b", "qwen2.5:3b-instruct"):
        payload = {"model": model, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "options": {"num_ctx": 8192, "temperature": 0.3}}
        try:
            req = urllib.request.Request("http://localhost:11434/api/chat",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())["message"]["content"]
        except Exception:  # noqa: BLE001
            continue
    return ""


def annotate(top: list[dict]) -> None:
    # Gemma 4 (via Ollama) writes the verdicts/labels. Tight prompt to kill repetition.
    sys_p = ("You label conversation mood arcs for a developer's chat logs. Dry, factual, "
             "specific. No hype, no repetition, no filler.")
    for a in top:
        texts = a["_texts"]
        tps = a["turning_points"]
        marks = sorted(set(tps + [a["peak_turn"], 0, len(texts) - 1]))
        lines = [f"turn {t}: " + " ".join(texts[t].split())[:200] for t in marks if 0 <= t < len(texts)]
        body = "\n".join(lines)
        user_p = (
            f"A developer's messages to an AI coding agent (key turns shown). Mood shifted at turns "
            f"{tps}; rage peaked at turn {a['peak_turn']}.\n\n{body}\n\n"
            "Output ONLY strict JSON: {\"verdict\": \"<one dry sentence, max 14 words, no repeated "
            "words, name the actual topic>\", \"points\": {"
            + ", ".join(f'"{t}": "<=5 word trigger>"' for t in tps) + "}}"
        )
        out = _ollama_gen(sys_p, user_p)
        a["verdict"], a["tp_labels"] = parse_annotation(out, tps)


def parse_annotation(out: str, tps: list[int]) -> tuple[str, dict]:
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if m:
        try:
            o = json.loads(m.group(0))
            verdict = str(o.get("verdict", "")).strip()
            pts = {str(k): str(v) for k, v in (o.get("points") or {}).items()}
            return verdict, pts
        except Exception:  # noqa: BLE001
            pass
    return out.strip()[:160], {}


if __name__ == "__main__":
    raise SystemExit(main())
