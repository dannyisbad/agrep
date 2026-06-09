"""Read-only data layer for the tilt explorer (no GPU, no LLM).

These power the browse/organize/detail half of the app. Everything here is a pure
file read over the already-built index, so the explorer is instant:

  list_chats()           -> the organizer list (one row per summarized session)
  list_concepts()        -> concept threads, for grouping/filter chips
  chats_in_concept(cid)  -> the summarized chats that belong to one concept thread
  get_chat(session)      -> one chat's full detail: summary + per-turn transcript w/ affect
  get_vibe(session)      -> the on-demand vibe-trace arc, or None

Built on: data/summaries.jsonl, data/emotions.jsonl, data/messages.jsonl,
data/concepts.json, data/session_concepts.jsonl, data/vibe/*.json.
"""

from __future__ import annotations

import functools
import json

import common

HOT_T = 0.15  # same threshold report.py uses: a message above this "reads hot"


# --------------------------------------------------------------------------- caches
# The index is static between rebuilds; cache the parsed tables so repeat requests
# (every chat the user opens) don't re-read 58 MB of messages.jsonl.

@functools.lru_cache(maxsize=1)
def _vibe_index() -> dict[str, dict]:
    """session -> its vibe index entry (peak_turn, juice, verdict, ...)."""
    p = common.DATA_DIR / "vibe" / "index.json"
    if not p.exists():
        return {}
    return {e["session"]: e for e in json.loads(p.read_text(encoding="utf-8"))}


@functools.lru_cache(maxsize=1)
def _summaries() -> list[dict]:
    p = common.DATA_DIR / "summaries.jsonl"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


@functools.lru_cache(maxsize=1)
def _concept_names() -> dict[int, str]:
    """concept_id -> clean display name (Gemma 4 'name', falling back to the raw label)."""
    p = common.DATA_DIR / "concepts.json"
    out: dict[int, str] = {}
    if p.exists():
        for r in json.loads(p.read_text(encoding="utf-8")):
            out[int(r["concept_id"])] = (r.get("name") or r.get("label") or "").strip()
    return out


@functools.lru_cache(maxsize=1)
def _session_concept() -> dict[str, str]:
    """session -> clean concept name (the cwd-independent thread it belongs to)."""
    names = _concept_names()
    p = common.DATA_DIR / "session_concepts.jsonl"
    out: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                out[o["session"]] = names.get(int(o.get("concept_id", -1))) or o.get("label", "")
    return out


@functools.lru_cache(maxsize=1)
def _concept_sessions() -> dict[int, list[dict]]:
    """concept_id -> [session_concept rows] for every session in that thread.
    Rows carry agent/cwd_project/n_msgs straight from session_concepts.jsonl, so
    organizing the full chat list by concept needs no messages.jsonl scan."""
    p = common.DATA_DIR / "session_concepts.jsonl"
    out: dict[int, list[dict]] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                out.setdefault(int(o["concept_id"]), []).append(o)
    return out


@functools.lru_cache(maxsize=1)
def _summary_by_session() -> dict[str, dict]:
    return {o["session"]: o for o in _summaries()}


@functools.lru_cache(maxsize=1)
def _messages_by_session() -> dict[str, list[dict]]:
    """session -> its message rows, parsed once. get_chat used to scan all of messages.jsonl
    (~50 MB / 25k lines) on every open; this loads it a single time so each open is a lookup."""
    out: dict[str, list[dict]] = {}
    p = common.MESSAGES_PATH
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                s = o.get("session")
                if s:
                    out.setdefault(s, []).append(o)
    return out


@functools.lru_cache(maxsize=1)
def _emotions_by_id() -> dict[str, dict]:
    """message id -> affect row, parsed once (mirrors the messages cache)."""
    out: dict[str, dict] = {}
    p = common.DATA_DIR / "emotions.jsonl"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id"):
                    out[o["id"]] = o
    return out


@functools.lru_cache(maxsize=1)
def _replies_by_id() -> dict[str, str]:
    """message id -> agent reply text, parsed once."""
    out: dict[str, str] = {}
    p = common.DATA_DIR / "replies.jsonl"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id"):
                    out[o["id"]] = o.get("reply", "")
    return out


def warm_caches() -> None:
    """Pre-build every read cache so even the first chat-open is instant (called at server boot)."""
    _summaries(); _summary_by_session(); _session_concept()
    _messages_by_session(); _emotions_by_id(); _replies_by_id()


# --------------------------------------------------------------------------- endpoints

def list_chats() -> list[dict]:
    """One row per summarized session, newest-feeling first (by message count desc as a
    proxy for substance; the client re-sorts). Carries the vibe flag so the list can mark
    chats that have an emotional arc to open."""
    vib = _vibe_index()
    concept = _session_concept()
    msgs = _messages_by_session()
    out = []
    for o in _summaries():
        s = o["session"]
        v = vib.get(s)
        rows = msgs.get(s, [])
        last_ts = max((r.get("ts", 0) for r in rows), default=0)
        # prefer the freshly-ingested project on the message rows (the inferred primary
        # working dir) over the summary's stale cwd_project, so the rail reflects re-index.
        project = (rows[0].get("project") if rows else "") or o.get("cwd_project", "")
        out.append({
            "session": s,
            "agent": o.get("agent", ""),
            "project": project,
            "concept": concept.get(s, ""),
            "n_msgs": o.get("n_msgs", 0),
            "last_ts": last_ts,
            "summary": o.get("summary", ""),
            "tags": o.get("tags", []),
            "has_vibe": v is not None,
            "juice": round(v["juice"], 2) if v else None,
            "verdict": v.get("verdict", "") if v else "",
        })
    out.sort(key=lambda r: r["n_msgs"], reverse=True)
    return out


def list_concepts(k: int = 40) -> list[dict]:
    """Concept threads (already ranked by n_messages in concepts.json)."""
    p = common.DATA_DIR / "concepts.json"
    if not p.exists():
        return []
    recs = json.loads(p.read_text(encoding="utf-8"))
    return [{
        "concept_id": r["concept_id"],
        "label": (r.get("name") or r["label"]),
        "terms": r.get("terms", []),
        "n_sessions": r["n_sessions"],
        "n_messages": r["n_messages"],
        "agents": r.get("agents", {}),
        "where": list(r.get("cwd_buckets", {}).keys())[:5],
    } for r in recs[:k]]


def chats_in_concept(concept_id: int) -> dict:
    """One concept thread + every chat inside it. This is the 'organize my chats by
    concept' view: the spine that fixes the cwd-bucketing problem. Each chat row is the
    session_concepts record, enriched with its summary/tags + vibe flag when those exist
    (most sessions are too small to have a summary, so those fields are just empty)."""
    recs = json.loads((common.DATA_DIR / "concepts.json").read_text(encoding="utf-8"))
    head = next((r for r in recs if r["concept_id"] == concept_id), None)
    if head is None:
        return {"error": f"concept {concept_id} not found"}
    summ = _summary_by_session()
    vib = _vibe_index()
    chats = []
    for o in _concept_sessions().get(concept_id, []):
        s = o["session"]
        sm = summ.get(s)
        v = vib.get(s)
        chats.append({
            "session": s,
            "agent": o.get("agent", ""),
            "project": o.get("cwd_project", ""),
            "n_msgs": o.get("n_msgs", 0),
            "summary": sm.get("summary", "") if sm else "",
            "tags": sm.get("tags", []) if sm else [],
            "has_summary": sm is not None,
            "has_vibe": v is not None,
            "juice": round(v["juice"], 2) if v else None,
            "verdict": v.get("verdict", "") if v else "",
        })
    chats.sort(key=lambda r: (r["has_summary"], r["n_msgs"]), reverse=True)
    return {
        "concept_id": concept_id,
        "label": (head.get("name") or head["label"]),
        "terms": head.get("terms", []),
        "n_sessions": head["n_sessions"],
        "n_messages": head["n_messages"],
        "agents": head.get("agents", {}),
        "where": list(head.get("cwd_buckets", {}).keys())[:5],
        "chats": chats,
    }


def _snip_at(text: str, start: int, end: int, pad: int = 80) -> str:
    """A one-line window of `text` around [start,end), with ellipses + collapsed whitespace."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    s = ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")
    return " ".join(s.split())


def _kw_pattern(q: str):
    """Compile a search pattern where any run of space/hyphen/underscore in the query matches
    any run (or none) of the same in the text — so "cyber filter" also finds "cyber-filter",
    "cyber_filter", "cyberfilter". This mirrors a grep `cyber[\\s-]*filter` and is what makes
    keyword search surface every real instance, not just the exact-spacing ones."""
    import re
    toks = [re.escape(t) for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if not toks:
        return None
    return re.compile(r"[\s\-_]*".join(toks), re.I)


def keyword_search(q: str, k: int = 300) -> dict:
    """Case-insensitive, separator-flexible substring search over the actual message AND
    agent-reply text (not summaries). Returns EVERY hit (one row per matching turn/reply,
    like grep), grouped by chat, so the UI surfaces all instances. Runs over the in-memory
    caches (RAM scan), so it's fast even though it touches every message."""
    pat = _kw_pattern(q)
    if pat is None:
        return {"hits": [], "total": 0, "chats": 0}
    msgs = _messages_by_session()
    reps = _replies_by_id()
    concept = _session_concept()
    hits = []
    for session, rows in msgs.items():
        for o in rows:
            base = {"session": session, "agent": o.get("agent", ""),
                    "project": o.get("project", ""), "concept": concept.get(session, ""),
                    "turn": o.get("turn", 0)}
            t = o.get("text", "") or ""
            m = pat.search(t)
            if m:
                hits.append({**base, "who": "you", "snippet": _snip_at(t, m.start(), m.end())})
            r = reps.get(o.get("id", ""), "")
            if r:
                m = pat.search(r)
                if m:
                    hits.append({**base, "who": "agent", "snippet": _snip_at(r, m.start(), m.end())})
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] == "you" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def get_vibe(session: str) -> dict | None:
    p = common.DATA_DIR / "vibe" / f"{session}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def get_chat(session: str) -> dict:
    """Full detail for one chat: header + per-turn transcript annotated with affect, plus the
    vibe arc if one was built. Reads from the in-process caches (messages-by-session, affect,
    replies), so each open is a dict lookup over this session's rows rather than a scan of the
    50 MB messages.jsonl (+ emotions + replies) on every request."""
    emo = _emotions_by_id()
    rep = _replies_by_id()
    rows = _messages_by_session().get(session, [])

    turns = []
    agent = project = ""
    for o in rows:
        agent = agent or o.get("agent", "")
        project = project or o.get("project", "")
        e = emo.get(o["id"], {})
        rage = float(e.get("rage_raw", 0.0))
        hype = float(e.get("hype_raw", 0.0))
        turns.append({
            "turn": o.get("turn", 0),
            "ts": o.get("ts", 0),
            "text": o.get("text", ""),
            "rage": round(rage, 4),
            "hype": round(hype, 4),
            "hot": rage > HOT_T,
            "top": e.get("top", []),
            "model": o.get("model", ""),
            "reply": rep.get(o["id"], ""),
        })
    turns.sort(key=lambda t: t["turn"])

    # the chat's headline model = the one most of its turns ran on
    mcount: dict[str, int] = {}
    for t in turns:
        if t.get("model"):
            mcount[t["model"]] = mcount.get(t["model"], 0) + 1
    session_model = max(mcount, key=mcount.get) if mcount else ""

    # header from the summary record if present
    summ = _summary_by_session().get(session)
    hot_n = sum(1 for t in turns if t["hot"])
    return {
        "session": session,
        "agent": agent or (summ.get("agent", "") if summ else ""),
        "project": project or (summ.get("cwd_project", "") if summ else ""),
        "model": session_model,
        "concept": _session_concept().get(session, ""),
        "summary": summ.get("summary", "") if summ else "",
        "tags": summ.get("tags", []) if summ else [],
        "n_msgs": len(turns),
        "hot_n": hot_n,
        "hot_pct": round(hot_n / max(1, len(turns)) * 100, 1),
        "turns": turns,
        "vibe": get_vibe(session),
    }
