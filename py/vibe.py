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


def arc_juice(rage: np.ndarray, val: np.ndarray) -> float:
    return float(rage.max() * 2 + val.std() * 3 + (rage > 0.4).mean() * 2
                 + longest_run(rage > 0.4) / max(len(rage), 1))


# Compaction recaps are machine text quoting the user; they carry no live affect.
_RECAP_PREFIX = "This session is being continued from a previous conversation"


def verify_markers(top: list[dict], emo: dict[str, dict], k: int) -> None:
    """LLM-judge the turns each top arc will cite (peak + spikes) and rebuild the
    arc's markers from the corrected numbers.

    Correcting a marker can move the peak / promote the next-loudest turn, so this
    iterates: judge the current candidate set, recompute, repeat until the set is
    stable (or 3 rounds). Judged rows are written back to emotions.jsonl exactly like
    judge.py does (gate values preserved, judged=true), so reruns cost nothing and
    every downstream reader sees the corrected read."""
    try:
        from judge import ask_judge
        from summarize import pick_model
        model = pick_model()
    except Exception:  # noqa: BLE001 -- no ollama: markers keep the gate's read
        common.log("vibe: judge model unavailable -- markers stay gate-scored.")
        return
    def flush() -> None:
        tmp = common.EMOTIONS_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for row in emo.values():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(common.EMOTIONS_PATH)

    t0 = time.perf_counter()
    judged = failed = unflushed = 0
    for a in top:
        ids, texts = a["_ids"], a["_texts"]
        rage = np.array(a["rage"], dtype=float)
        val = np.array(a["valence"], dtype=float)
        attempted: set[int] = set()  # judge failures keep gate values but don't re-loop
        for _ in range(3):
            hype = val + rage
            peak = int(rage.argmax())
            tps = find_spikes(rage, hype, peak, k)
            cand = [t for t in {peak, *tps}
                    if t not in attempted and not emo.get(ids[t], {}).get("judged")]
            if not cand:
                break
            for t in cand:
                attempted.add(t)
                row = emo.get(ids[t])
                if row is None:
                    continue
                if texts[t].startswith(_RECAP_PREFIX):
                    r_j, h_j = 0.0, 0.0  # machine text: no developer affect
                else:
                    v = ask_judge(model, texts[t])
                    if v is None:
                        failed += 1
                        continue
                    r_j, h_j = v
                row["rage_gate"], row["hype_gate"] = row["rage_raw"], row["hype_raw"]
                # same ×1.5 rescale as judge.py: judge grades 0..1, gate sums run ~0..2.5
                row["rage_raw"], row["hype_raw"] = round(r_j * 1.5, 6), round(h_j * 1.5, 6)
                row["judged"] = True
                rage[t] = r_j * 1.5
                val[t] = (h_j - r_j) * 1.5
                judged += 1
                unflushed += 1
                if judged % 25 == 0:
                    common.log(f"  ... {judged} marker turns judged "
                               f"({judged / (time.perf_counter() - t0):.2f}/s)")
                if unflushed >= 50:  # checkpoint: an interrupt loses <=50 verdicts, not all
                    flush()
                    unflushed = 0
        hype = val + rage
        a["peak_turn"] = int(rage.argmax())
        a["turning_points"] = find_spikes(rage, hype, a["peak_turn"], k)
        a["rage"] = [round(float(r), 4) for r in rage]
        a["valence"] = [round(float(v), 4) for v in val]
        a["swing"] = round(float(val.std()), 4)
        a["drift"] = round(float(val[-3:].mean() - val[:3].mean()), 4)
        a["juice"] = round(arc_juice(rage, val), 4)
    if unflushed:
        flush()
    common.log(f"vibe: marker verification -- {judged} turns judged, {failed} judge "
               f"failures, {time.perf_counter() - t0:.0f}s")


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
        arcs.append({
            "session": s, "agent": meta[s][0], "project": meta[s][1],
            "n_turns": len(val),
            "valence": [round(float(v), 4) for v in val],
            "rage": [round(float(r), 4) for r in rage],
            "peak_turn": peak_turn,
            "swing": round(float(val.std()), 4),
            "drift": round(float(val[-3:].mean() - val[:3].mean()), 4),
            "juice": round(arc_juice(rage, val), 4),
            "turning_points": tps,
            "_texts": texts,
            "_ids": ids,
        })

    arcs.sort(key=lambda a: a["juice"], reverse=True)
    top = arcs[: args.top]
    top_set = {a["session"] for a in top}

    # Every arc file in data/vibe/ is served on its chat page regardless of today's top
    # list, so a stale file keeps showing unverified markers forever. Refresh any on-disk
    # arc that still qualifies this run: numbers + markers heal, the verdict is kept.
    vdir = common.DATA_DIR / "vibe"
    vdir.mkdir(exist_ok=True)
    have = {p.stem for p in vdir.glob("*.json")} - {"index"}
    refresh = [a for a in arcs[args.top:] if a["session"] in have]
    keep = top + refresh

    # The markers (peak + spikes) are the only per-turn affect claims the UI makes, and
    # the gate model over-scores mild dev chat ("not able to bc of remote debugging flag"
    # read as 0.88 rage). Verify exactly those turns with the LLM judge before publishing
    # them; corrections persist to emotions.jsonl, so each turn is judged at most once.
    if not args.no_llm:
        verify_markers(keep, emo, args.turning)

    # ---- automatic staleness: an arc's identity is its affect numbers. If a TOP arc on
    # disk was built from the SAME valence/rage series, its verdict still applies and the
    # LLM is skipped; if emotions.jsonl changed under it (new gate model, judge verdicts,
    # new turns), the sig differs and that session re-annotates. Refresh-only arcs always
    # keep their old verdict -- the verdict is impressionistic, the numbers are the claim.
    todo = []
    for a in keep:
        a["sig"] = hashlib.md5(json.dumps([a["valence"], a["rage"]]).encode()).hexdigest()
        old = None
        p = vdir / f"{a['session']}.json"
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = None
        if old and old.get("verdict") and (old.get("sig") == a["sig"]
                                           or a["session"] not in top_set):
            a["verdict"] = old["verdict"]
        elif a["session"] in top_set:
            todo.append(a)
    common.log(f"{len(arcs)} arcs (>= {args.min_turns} turns); top {len(top)} + "
               f"{len(refresh)} refreshed; {len(todo)} need annotation")

    if not args.no_llm and todo:
        annotate(todo)

    # write per-session arcs + an index (index = today's top only)
    index = []
    for a in keep:
        texts = a.pop("_texts")
        a.pop("_ids", None)
        # attach short per-turn snippets for the renderer tooltips
        a["snippets"] = [" ".join(t.split())[:140] for t in texts]
        (vdir / f"{a['session']}.json").write_text(json.dumps(a), encoding="utf-8")
        if a["session"] in top_set:
            index.append({k: a[k] for k in ("session", "agent", "project", "n_turns",
                                            "peak_turn", "swing", "drift", "juice",
                                            "verdict") if k in a})
    (vdir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"\n  wrote {len(keep)} vibe-traces ({len(top)} top + {len(refresh)} refreshed) -> {vdir}")
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
