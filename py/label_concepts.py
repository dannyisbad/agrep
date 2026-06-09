"""Name concept clusters with Gemma 4 -> adds a clean 'name' to data/concepts.json.

The raw c-TF-IDF labels pick up cussing ("opencode, model, shit") and persona names
("jordan, begin_untrusted_imessage"). This gives each concept a short Title Case topic
name from its keywords + sample chat summaries. Run after concepts.py + summarize.py.
"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter, defaultdict

import common

MODELS = ["gemma4:e4b-it-qat", "gemma4:e4b", "qwen2.5:3b-instruct"]
OLLAMA = "http://localhost:11434/api/chat"


def pick_model():
    with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10) as r:
        have = {m["name"] for m in json.loads(r.read().decode()).get("models", [])}
    for m in MODELS:
        hit = next((h for h in have if h == m or h.startswith(m.split(":")[0])), None)
        if hit:
            return hit
    raise SystemExit("no ollama model")


def gen(model, body):
    payload = {"model": model, "stream": False,
               "messages": [
                   {"role": "system", "content": "You name a cluster of a developer's coding chats. "
                    "Reply with ONLY a 2 to 4 word Title Case label (a noun phrase). Name the SPECIFIC "
                    "project, driver, tool, or subject (use the proper nouns in the keywords/folders: "
                    "e.g. 'EFI Audit Pipeline', 'AMD Ryzen Driver', 'iMessage Automation'). AVOID generic "
                    "filler words like Structural, Verification, System, Analysis, Workflow, Development, "
                    "Configuration unless nothing more specific exists. No profanity, no persona names "
                    "(jordan/marcus/kerry/candence), no quotes, no punctuation."},
                   {"role": "user", "content": body}],
               "options": {"num_ctx": 4096, "temperature": 0.2}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())["message"]["content"]


def main(top_n: int = 80) -> int:
    model = pick_model()
    concepts = json.loads((common.DATA_DIR / "concepts.json").read_text(encoding="utf-8"))

    summ = {}
    p = common.DATA_DIR / "summaries.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                summ[o["session"]] = o.get("summary", "")
    members = defaultdict(list)
    sc = common.DATA_DIR / "session_concepts.jsonl"
    if sc.exists():
        for line in sc.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                members[o["concept_id"]].append(o["session"])

    order = sorted(range(len(concepts)), key=lambda i: concepts[i].get("n_messages", 0), reverse=True)
    common.log(f"naming top {min(top_n, len(order))} concepts with {model}")
    done = 0
    for i in order[:top_n]:
        c = concepts[i]
        terms = ", ".join(c.get("terms", [])[:8])
        folders = ", ".join(list(c.get("cwd_buckets", {}).keys())[:4])
        sums = [summ[s] for s in members.get(c["concept_id"], []) if summ.get(s)][:7]
        body = ("keywords: " + terms + "\nfolders: " + folders + "\nsample chats:\n"
                + "\n".join("- " + s[:200] for s in sums))
        try:
            name = gen(model, body).strip().strip('"').splitlines()[0]
            name = name.rstrip(".").strip()[:42]
            c["name"] = name or None
        except Exception as e:  # noqa: BLE001
            c["name"] = None
            common.log(f"  warn concept {c['concept_id']}: {e}")
        done += 1
        if done % 20 == 0:
            common.log(f"  ... {done}")

    concepts = merge_duplicate_names(concepts)
    (common.DATA_DIR / "concepts.json").write_text(json.dumps(concepts, indent=1), encoding="utf-8")
    named = sum(1 for c in concepts if c.get("name"))
    print(f"  named {len(concepts)} topics ({named} via gemma)")
    for c in sorted(concepts, key=lambda r: r["n_sessions"], reverse=True)[:24]:
        print(f"    {c['n_sessions']:>3}  {c.get('name') or c['label'][:40]}")
    return 0


def merge_duplicate_names(concepts: list[dict]) -> list[dict]:
    """Collapse topics that the namer couldn't tell apart: if two clusters got the same
    Title Case name, they're one topic to a human — merge them and remap session_concepts.
    """
    def key(c):
        return (c.get("name") or c.get("label") or "").strip().lower()

    groups: dict[str, list[dict]] = defaultdict(list)
    for c in concepts:
        groups[key(c)].append(c)

    remap: dict[int, int] = {}
    merged: list[dict] = []
    for grp in groups.values():
        keeper = max(grp, key=lambda r: r.get("n_sessions", 0))
        kid = keeper["concept_id"]
        agents, cwds, terms = Counter(), Counter(), []
        for r in grp:
            remap[r["concept_id"]] = kid
            for a, n in (r.get("agents") or {}).items():
                agents[a] += n
            for cw, n in (r.get("cwd_buckets") or {}).items():
                cwds[cw] += n
            terms += r.get("terms", [])
        nc = dict(keeper)
        nc["n_sessions"] = sum(r.get("n_sessions", 0) for r in grp)
        nc["n_messages"] = sum(r.get("n_messages", 0) for r in grp)
        nc["agents"] = dict(agents)
        nc["cwd_buckets"] = dict(cwds.most_common(6))
        nc["terms"] = terms[:8]
        merged.append(nc)
    merged.sort(key=lambda r: r["n_sessions"], reverse=True)

    if len(merged) < len(concepts):
        common.log(f"merged {len(concepts)} -> {len(merged)} topics (dropped duplicate names)")
        sc = common.DATA_DIR / "session_concepts.jsonl"
        name_of = {c["concept_id"]: (c.get("name") or c.get("label") or "") for c in merged}
        if sc.exists():
            rows = [json.loads(l) for l in sc.read_text(encoding="utf-8").splitlines() if l.strip()]
            with sc.open("w", encoding="utf-8") as f:
                for o in rows:
                    cid = remap.get(o["concept_id"], o["concept_id"])
                    o["concept_id"] = cid
                    o["label"] = name_of.get(cid, o.get("label", ""))
                    f.write(json.dumps(o) + "\n")
    return merged


if __name__ == "__main__":
    raise SystemExit(main())
