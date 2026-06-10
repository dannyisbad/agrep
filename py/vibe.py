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
import hashlib
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


def find_spikes(rage: np.ndarray, hype: np.ndarray, peak: int, k: int) -> list[int]:
    """The turns worth a numbered marker: the FRUSTRATION SPIKES (local maxima of rage),
    plus the single biggest genuine hype moment. NOT the derivative of valence -- that
    flagged calm wiggles ('ok check now') as 'turning points'. A spike must clear a floor
    (so flat chats get no fake markers) and stand apart from the peak/each other.

    The global peak gets its own ring, so it's excluded here -- these are the SECONDARY
    moments that, with the peak, tell the arc's story."""
    n = len(rage)
    if n < 3:
        return []
    floor = max(0.30, 0.45 * float(rage.max()))
    cands: list[tuple[float, int]] = []
    for i in range(n):
        lo, hi = rage[i - 1] if i > 0 else -1, rage[i + 1] if i + 1 < n else -1
        if rage[i] >= floor and rage[i] >= lo and rage[i] > hi:  # local max (ties break left)
            cands.append((float(rage[i]), i))
    # the strongest positive beat, if there's a real one, earns a marker too
    if n and hype.max() >= 0.55:
        hi_t = int(hype.argmax())
        if all(abs(hi_t - i) > 2 for _, i in cands):
            cands.append((float(hype[hi_t]), hi_t))
    cands.sort(reverse=True)
    picked: list[int] = []
    for _, i in cands:
        if abs(i - peak) <= 2:
            continue  # the peak already owns this region (its ring)
        if all(abs(i - j) > 2 for j in picked):
            picked.append(i)
        if len(picked) >= k:
            break
    return sorted(picked)


def main() -> int:
    ap = argparse.ArgumentParser(description="build vibe-trace arcs")
    ap.add_argument("--top", type=int, default=24)
    ap.add_argument("--min-turns", type=int, default=8)
    ap.add_argument("--turning", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true", help="skip LLM annotation (arcs only)")
    args = ap.parse_args()

    if not (common.DATA_DIR / "emotions.jsonl").exists():
        common.log("vibe: no emotions.jsonl yet (run emotion.py first) -- skipping, "
                   "the explorer works fine without arcs.")
        return 0
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
        hype = np.array([v + r for v, r in zip(val, rage)])  # hype = valence + rage
        peak_turn = int(rage.argmax())
        tps = find_spikes(rage, hype, peak_turn, args.turning)
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

    # ---- automatic staleness: an arc's identity is its affect numbers. If the arc on
    # disk was built from the SAME valence/rage series, its verdict/labels still apply
    # and the LLM is skipped; if emotions.jsonl changed under it (new gate model, judge
    # verdicts, new turns), the sig differs and that session re-annotates. So a plain
    # reindex self-heals after any affect change -- no manual "vibe rebuild" step.
    vdir = common.DATA_DIR / "vibe"
    vdir.mkdir(exist_ok=True)
    todo = []
    for a in top:
        a["sig"] = hashlib.md5(json.dumps([a["valence"], a["rage"]]).encode()).hexdigest()
        old = None
        p = vdir / f"{a['session']}.json"
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = None
        if old and old.get("sig") == a["sig"] and old.get("verdict"):
            a["verdict"] = old["verdict"]
        else:
            todo.append(a)
    common.log(f"{len(arcs)} arcs (>= {args.min_turns} turns); top {len(top)} kept; "
               f"{len(todo)} stale/new need annotation ({len(top) - len(todo)} carried over)")

    if not args.no_llm and todo:
        annotate(todo)

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
    # Gemma 4 (via Ollama) writes the one-line verdicts. The style contract matters more
    # than the task description: without the banned-register examples the model emits
    # HR-incident-report mush ("intense instruction regarding build failure resolution").
    # Per-point labels are gone -- the UI quotes the actual message at each marker, which
    # says more than any 5-word paraphrase.
    sys_p = (
        "You annotate mood arcs from a developer's chats with AI coding agents. Voice: dry, "
        "deadpan, forensic -- a terse case note by someone who was there. Plain spoken "
        "English, active voice, lowercase fine, name the actual tech and what actually "
        "happened. BANNED: bureaucratic nominalizations ('instruction regarding', "
        "'completion reminder issued', 'escalates into'), passive voice, the words "
        "'session', 'interaction', 'regarding', 'utilize'.\n"
        "Verdict examples -- GOOD: 'the build kept self-resetting; he tore into the agent "
        "until it stuck to one plan' / 'three hours fighting vercel deploys, never landed'. "
        "BAD: 'Technical debugging session escalates into intense instruction regarding "
        "build failure resolution.'"
    )
    for a in top:
        texts = a["_texts"]
        tps = a["turning_points"]
        marks = sorted(set(tps + [a["peak_turn"], 0, len(texts) - 1]))
        lines = [f"turn {t}: " + " ".join(texts[t].split())[:200] for t in marks if 0 <= t < len(texts)]
        body = "\n".join(lines)
        user_p = (
            f"A developer's messages to an AI coding agent (key turns shown). Frustration "
            f"peaked at turn {a['peak_turn']}.\n\n{body}\n\n"
            "Output ONLY strict JSON: {\"verdict\": \"<one sentence, max 14 words, plain "
            "spoken English, names what actually happened>\"}"
        )
        a["verdict"] = parse_annotation(_ollama_gen(sys_p, user_p))


def parse_annotation(out: str) -> str:
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if m:
        try:
            return str(json.loads(m.group(0)).get("verdict", "")).strip()
        except Exception:  # noqa: BLE001
            pass
    return out.strip()[:160]


if __name__ == "__main__":
    raise SystemExit(main())
