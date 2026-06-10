"""tilt ask: a local tool-using agent over your chat history.

The model (via Ollama) is given tools that wrap tilt's index, decides which to call,
and answers in natural language. This is the engine behind the "ask tilt about your
chats" UI.

Tools:
  search_chats(query)      -- which past CHATS were about something (summary-level)
  search_messages(query)   -- find specific MESSAGES you wrote
  top_concepts()           -- the concept threads you've worked on
  rage_ranking(scope)      -- rage index by agent or project

Usage: python ask.py "what project was I doing the DRM driver work in"
Requires: ollama running + a tool-capable model pulled. Falls back across models.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict

import numpy as np

import common
from embed import CANDIDATES as EMB_CANDIDATES, build_loader, is_qwen, QWEN_QUERY_INSTRUCTION

OLLAMA_URL = "http://localhost:11434"
# tool-capable models, primary first. gemma4 e4b (Apache-2.0, ungated, has tools) per the user;
# qwen2.5 kept as a reliable tool-calling fallback.
LLM_MODELS = ["gemma4:e4b-it-qat", "gemma4:e4b", "qwen2.5:3b-instruct", "qwen2.5:7b-instruct"]

_EMB = {"model": None, "id": None}
_RR = {"model": None}


# ----------------------------- tool implementations -----------------------------

def _embedder():
    if _EMB["model"] is None:
        dev = common.pick_device()
        r = common.resolve_model(EMB_CANDIDATES, build_loader(dev), label="ask-embed")
        _EMB["model"], _EMB["id"] = r.obj, r.id
    return _EMB["model"], _EMB["id"]


def _embed_query(text: str) -> np.ndarray:
    model, mid = _embedder()
    if is_qwen(mid):
        q = f"Instruct: {QWEN_QUERY_INSTRUCTION}\nQuery: {text}"
    elif "bge" in mid.lower():
        q = f"Represent this sentence for searching relevant passages: {text}"
    else:
        q = text
    v = model.encode([q], convert_to_numpy=True, normalize_embeddings=False, device=common.pick_device())[0]
    return common.matryoshka_truncate(v.reshape(1, -1), dim=common.EMBED_DIM)[0].astype(np.float32)


def _reranker():
    if _RR["model"] is None:
        from sentence_transformers import CrossEncoder
        dev = common.pick_device()
        r = common.resolve_model(["BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-base"],
                                 lambda m: CrossEncoder(m, device=dev, max_length=512), label="ask-rerank")
        _RR["model"] = r.obj
    return _RR["model"]


def tool_search_chats(query: str, k: int = 5) -> str:
    qv = _embed_query(query)
    dim = int((common.DATA_DIR / "summary_emb.meta").read_text().strip())
    ids = (common.DATA_DIR / "summary_emb.ids").read_text(encoding="utf-8").splitlines()
    mat = np.fromfile(common.DATA_DIR / "summary_emb.f32", dtype="<f4").reshape(-1, dim)
    recs = {}
    for line in (common.DATA_DIR / "summaries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line); recs[o["session"]] = o
    sims = mat @ qv
    pool = np.argsort(-sims)[: max(k * 6, 30)]
    cand = [(ids[int(i)], recs.get(ids[int(i)])) for i in pool if recs.get(ids[int(i)])]
    rr = _reranker()
    scores = rr.predict([(query, c[1].get("summary", "")) for c in cand], show_progress_bar=False)
    order = np.argsort(-np.asarray(scores))[:k]
    import explore
    sc = explore._session_concept()
    out = [{"session": cand[i][0], "concept": sc.get(cand[i][0], ""),
            "project": cand[i][1].get("cwd_project", ""),
            "agent": cand[i][1].get("agent", ""), "title": cand[i][1].get("title", ""),
            "summary": cand[i][1].get("summary", ""),
            "tags": cand[i][1].get("tags", []), "n_msgs": cand[i][1].get("n_msgs", 0)} for i in order]
    return json.dumps(out)


def tool_search_messages(query: str, k: int = 5) -> str:
    qv = _embed_query(query)
    ids, mat = common.read_embeddings()
    meta = {}
    for line in common.MESSAGES_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line); meta[o["id"]] = o
    sims = mat @ qv
    pool = np.argsort(-sims)[: max(k * 6, 40)]
    seen, cand = set(), []
    for i in pool:
        m = meta.get(ids[int(i)])
        if not m:
            continue
        key = " ".join(m["text"].split())[:140].lower()
        if key in seen:
            continue
        seen.add(key); cand.append(m)
    rr = _reranker()
    scores = rr.predict([(query, c["text"]) for c in cand], show_progress_bar=False)
    order = np.argsort(-np.asarray(scores))[:k]
    out = [{"project": cand[i].get("project", ""), "agent": cand[i].get("agent", ""),
            "text": " ".join(cand[i].get("text", "").split())[:240]} for i in order]
    return json.dumps(out)


def tool_top_concepts(k: int = 12) -> str:
    recs = json.loads((common.DATA_DIR / "concepts.json").read_text(encoding="utf-8"))
    out = [{"concept": (r.get("name") or r["label"]), "sessions": r["n_sessions"],
            "messages": r["n_messages"], "where": list(r.get("cwd_buckets", {}).keys())[:4]} for r in recs[:k]]
    return json.dumps(out)


def tool_rage_ranking(scope: str = "agent") -> str:
    emo = {}
    for line in (common.DATA_DIR / "emotions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line); emo[o["id"]] = o
    agg = defaultdict(lambda: {"msgs": 0, "rage": 0.0})
    for line in common.MESSAGES_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line); e = emo.get(o["id"])
        if not e:
            continue
        key = o.get("agent" if scope == "agent" else "project", "?")
        agg[key]["msgs"] += 1
        agg[key]["rage"] += float(e.get("rage_raw", 0))
    rows = [{"name": k, "rage_per_1k": round(v["rage"] / v["msgs"] * 1000, 1), "msgs": v["msgs"]}
            for k, v in agg.items() if v["msgs"] >= (1 if scope == "agent" else 25)]
    rows.sort(key=lambda r: r["rage_per_1k"], reverse=True)
    return json.dumps(rows[:12])


TOOLS = {
    "search_chats": tool_search_chats,
    "search_messages": tool_search_messages,
    "top_concepts": tool_top_concepts,
    "rage_ranking": tool_rage_ranking,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_chats", "description": "Find past CHAT SESSIONS that were about a topic (uses chat summaries). Best for 'what project/chat was I doing X in'.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_messages", "description": "Find specific MESSAGES the user wrote matching a query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "top_concepts", "description": "List the main concept threads (topics) the user has worked on across all chats.",
        "parameters": {"type": "object", "properties": {"k": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "rage_ranking", "description": "Rage index ranking by 'agent' or 'project' (who/what the user was most frustrated with).",
        "parameters": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["agent", "project"]}}}}},
]


# ----------------------------- ollama plumbing -----------------------------

def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def _available_model() -> str:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=10) as r:
            have = {m["name"] for m in json.loads(r.read().decode()).get("models", [])}
    except Exception as e:  # noqa: BLE001
        common.log(f"ollama not reachable at {OLLAMA_URL} ({e}). Is `ollama serve` running?")
        sys.exit(1)
    for m in LLM_MODELS:
        if m in have or any(h.startswith(m.split(":")[0]) for h in have):
            return next((h for h in have if h == m), next(h for h in have if h.startswith(m.split(":")[0])))
    common.log(f"no tool-capable model pulled. have={have}. try: ollama pull {LLM_MODELS[0]}")
    sys.exit(1)


def ask(question: str, model: str | None = None) -> dict:
    """Run the agent. Returns {answer, steps:[{tool,args,result}], model}."""
    model = model or _available_model()
    common.log(f"ask: using {model}")
    msgs = [
        {"role": "system", "content": "You are tilt, an assistant over the user's own coding-chat history. "
         "Use the tools to look things up before answering. Be concise and factual. Cite project/agent names. "
         "Prefer the concept/topic name over a generic folder name like 'Users/<you>'. "
         "Chain tools before answering: a ranking or list tells you WHICH, not WHY — when the "
         "question asks why/what-happened/what-about, follow up with search_chats or "
         "search_messages on the top result instead of answering that the data doesn't say. "
         "Never claim the data lacks detail until a search tool has come back empty."},
        {"role": "user", "content": question},
    ]
    steps = []
    for _ in range(6):
        # num_ctx override: Ollama defaults Gemma 4 to 4K, which truncates tool results.
        resp = _post("/api/chat", {"model": model, "messages": msgs, "tools": TOOL_SCHEMAS,
                                   "stream": False, "options": {"num_ctx": 8192, "temperature": 0.2}})
        m = resp.get("message", {})
        msgs.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            return {"answer": m.get("content", "").strip() or "(no answer)", "steps": steps, "model": model}
        for c in calls:
            fn = c["function"]["name"]
            args = c["function"].get("arguments", {}) or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}
            common.log(f"  -> tool {fn}({args})")
            try:
                result = TOOLS[fn](**args) if fn in TOOLS else json.dumps({"error": "unknown tool"})
            except Exception as e:  # noqa: BLE001
                result = json.dumps({"error": str(e)})
            steps.append({"tool": fn, "args": args, "result": result})
            msgs.append({"role": "tool", "content": result[:6000], "tool_name": fn})
    return {"answer": msgs[-1].get("content", "").strip() or "(no answer)", "steps": steps, "model": model}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    print("\n" + ask(args.question, args.model)["answer"] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
