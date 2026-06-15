"""Read-only data layer for the tilt explorer (no GPU, no LLM).

These power the browse/organize/detail half of the app. Everything here is a pure
file read over the already-built index, so the explorer is instant:

  list_chats()           -> the organizer list (one row per summarized session)
  list_concepts()        -> concept threads, for grouping/filter chips
  chats_in_concept(cid)  -> the summarized chats that belong to one concept thread
  get_chat(session)      -> one chat's full detail: summary + per-turn transcript w/ affect
  get_vibe(session)      -> the on-demand vibe-trace arc, or None
  stats()                -> honest corpus totals (all sessions/msgs, not just summarized)

Built on: data/summaries.jsonl, data/emotions.jsonl, data/messages.jsonl,
data/concepts.json, data/session_concepts.jsonl, data/vibe/*.json.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import common

HOT_T = 0.15  # same threshold report.py uses: a message above this "reads hot"
EVENTS_DIR_NAME = "events"
# Claude's compaction summary is logged as a user message; tag it as a "recap" rather than
# treating it as something the user wrote (it's machine-generated continuation context).
RECAP_PREFIX = "This session is being continued from a previous conversation"


# --------------------------------------------------------------------------- caches
# The index is static between rebuilds; cache the parsed tables so repeat requests
# (every chat the user opens) don't re-read all of messages.jsonl. BUT the server
# is long-lived and reindexes happen under it, so every public entry point first
# checks a generation stamp (mtime+size of the index files) and drops all caches
# when the index moved -- otherwise the explorer serves week-old rows until restart.

_GEN_FILES = ("messages.jsonl", "summaries.jsonl", "emotions.jsonl", "replies.jsonl",
              "concepts.json", "session_concepts.jsonl", "vibe/index.json",
              "sessions.jsonl")
_GEN: tuple | None = None


def _freshen() -> None:
    global _GEN
    gen = []
    for name in _GEN_FILES:
        try:
            st = (common.DATA_DIR / name).stat()
            gen.append((st.st_mtime_ns, st.st_size))
        except OSError:
            gen.append(None)
    gen = tuple(gen)
    if gen == _GEN:
        return
    if _GEN is not None:  # first call just records; later changes invalidate
        for fn in (_vibe_index, _summaries, _concept_names, _session_concept,
                   _concept_sessions, _summary_by_session, _messages_by_session,
                   _emotions_by_id, _replies_by_id, _kw_corpus, _event_sessions,
                   _session_index):
            fn.cache_clear()
    _GEN = gen


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
def _session_index() -> dict[str, dict]:
    """session -> tiny aggregate {agent, project, n, first_ts, last_ts, first_text}.
    Materialized by `agrep index` (data/sessions.jsonl, small) precisely so the rail
    never has to parse the much larger messages.jsonl. Falls back to deriving the same
    shape from the big file for indexes built before sessions.jsonl existed."""
    p = common.DATA_DIR / "sessions.jsonl"
    out: dict[str, dict] = {}
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    o = json.loads(line)
                    out[o["session"]] = o
        return out
    for s, rows in _messages_by_session().items():
        first = next((r.get("text", "") for r in sorted(rows, key=lambda r: r.get("turn", 0))
                      if r.get("text", "").strip()
                      and not r["text"].startswith(RECAP_PREFIX)), "")
        out[s] = {"session": s, "agent": rows[0].get("agent", ""),
                  "project": rows[0].get("project", ""), "n": len(rows),
                  "first_ts": min((r.get("ts", 0) for r in rows if r.get("ts", 0)), default=0),
                  "last_ts": max((r.get("ts", 0) for r in rows), default=0),
                  "first_text": " ".join(first.split())[:120]}
    return out


@functools.lru_cache(maxsize=1)
def _messages_by_session() -> dict[str, list[dict]]:
    """session -> its message rows, parsed once. get_chat used to scan all of messages.jsonl
    on every open; this loads it a single time so each open is a lookup."""
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


@functools.lru_cache(maxsize=1)
def _kw_corpus() -> list[dict]:
    """Flat, pre-lowercased search corpus: one entry per user turn AND per agent reply, with a
    ready-made lowercase string. Keyword search scans these instead of lowercasing ~80 MB of
    text on every keystroke, which is what makes live search-as-you-type instant. Built once."""
    msgs = _messages_by_session()
    reps = _replies_by_id()
    concept = _session_concept()
    out: list[dict] = []
    for session, rows in msgs.items():
        c = concept.get(session, "")
        for o in rows:
            t = o.get("text", "") or ""
            if t:
                out.append({"session": session, "turn": o.get("turn", 0), "ts": o.get("ts", 0),
                            "agent": o.get("agent", ""),
                            "project": o.get("project", ""), "concept": c,
                            "model": o.get("model", ""),
                            "who": "recap" if t.startswith(RECAP_PREFIX) else "you",
                            "text": t, "low": t.lower()})
            r = reps.get(o.get("id", ""), "")
            if r:
                out.append({"session": session, "turn": o.get("turn", 0), "ts": o.get("ts", 0),
                            "agent": o.get("agent", ""),
                            "project": o.get("project", ""), "concept": c,
                            "model": o.get("model", ""),
                            "who": "agent", "text": r, "low": r.lower()})
    return out


def warm_caches() -> None:
    """Pre-build every read cache so even the first chat-open / search is instant (server boot)."""
    _session_index(); _summaries(); _summary_by_session(); _session_concept()
    _messages_by_session(); _emotions_by_id(); _replies_by_id(); _kw_corpus()


# --------------------------------------------------------------------------- endpoints

def list_chats() -> list[dict]:
    """Every session worth a rail row, newest-feeling first (the client re-sorts).

    Summarized sessions carry their title/summary/tags/vibe. UNSUMMARIZED sessions are
    included too (>= 2 messages -- one-shot throwaways stay reachable through
    keyword search), titled by their first typed message and flagged `thin` so the UI
    can dim them. This is what makes a fresh install useful immediately: the rail fills
    straight from `agrep index`, before any LLM stage has ever run. When NO summaries
    exist yet, even 1-message sessions are listed rather than an empty rail."""
    _freshen()
    vib = _vibe_index()
    concept = _session_concept()
    idx = _session_index()
    out = []
    seen = set()
    for o in _summaries():
        s = o["session"]
        v = vib.get(s)
        row = idx.get(s) or {}
        seen.add(s)
        out.append({
            "session": s,
            "agent": o.get("agent", ""),
            # prefer the freshly-ingested project (the inferred most-worked-in dir)
            # over the summary's stale cwd_project, so the rail reflects re-index.
            "project": row.get("project") or o.get("cwd_project", ""),
            "concept": concept.get(s, ""),
            "n_msgs": o.get("n_msgs", 0),
            "last_ts": row.get("last_ts", 0),
            "title": o.get("title", ""),
            "summary": o.get("summary", ""),
            "tags": o.get("tags", []),
            "has_vibe": v is not None,
            "juice": round(v["juice"], 2) if v else None,
            "verdict": v.get("verdict", "") if v else "",
        })
    floor = 2 if out else 1
    for s, row in idx.items():
        if s in seen or row.get("n", 0) < floor:
            continue
        out.append({
            "session": s,
            "agent": row.get("agent", ""),
            "project": row.get("project", ""),
            "concept": concept.get(s, ""),
            "n_msgs": row.get("n", 0),
            "last_ts": row.get("last_ts", 0),
            "title": row.get("first_text", "")[:90],
            "summary": "",
            "tags": [],
            "thin": True,
            "has_vibe": False,
            "juice": None,
            "verdict": "",
        })
    out.sort(key=lambda r: r["n_msgs"], reverse=True)
    return out


def list_concepts(k: int = 40) -> list[dict]:
    """Concept threads (already ranked by n_messages in concepts.json)."""
    _freshen()
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
    _freshen()
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
    """Case-insensitive, separator-flexible substring search over every user turn AND agent
    reply. Returns EVERY hit (one row per match, like grep) grouped by chat. Scans the
    pre-lowercased corpus, with a plain-substring fast path for single-token queries (the
    common case while typing), so it's fast enough to run live on every keystroke."""
    _freshen()
    q = q.strip()
    if not q:
        return {"hits": [], "total": 0, "chats": 0}
    corpus = _kw_corpus()
    fields = ("session", "agent", "project", "concept", "model", "turn", "ts", "who")
    toks = [t for t in re.split(r"[\s\-_]+", q) if t]
    hits = []
    if len(toks) <= 1:  # single token -> plain substring (fastest)
        ql = q.lower()
        for e in corpus:
            i = e["low"].find(ql)
            if i >= 0:
                hits.append({**{f: e[f] for f in fields}, "snippet": _snip_at(e["text"], i, i + len(ql))})
    else:  # multi-token -> separator-flexible regex (matches cyber-filter / cyber_filter)
        pat = re.compile(r"[\s\-_]*".join(re.escape(t) for t in toks))
        for e in corpus:
            m = pat.search(e["low"])
            if m:
                hits.append({**{f: e[f] for f in fields}, "snippet": _snip_at(e["text"], m.start(), m.end())})
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def resolve_session(q: str) -> list[str]:
    """Resolve a session id query to full ids: exact match wins, else prefix (covers the
    8-char short ids search/around print). Same semantics resume uses, shared here so
    every verb that takes an id accepts the same spellings."""
    _freshen()
    q = q.strip()
    idx = _session_index()
    if q in idx:
        return [q]
    return [s for s in idx if s.startswith(q)]


def _db_session_rows(session: str) -> list[dict] | None:
    """One session's turns from the derived corpus db — the fast path that keeps
    `agrep around` from parsing 50 MB of jsonl. The db stores user text and agent
    replies as separate rows; merge them back to one row per turn. None when the db
    is unavailable (caller falls back to the in-memory caches)."""
    import corpusdb  # late: corpusdb is optional and this module is imported by the server
    db = corpusdb.connect()
    if db is None:
        return None
    merged: dict[int, dict] = {}
    try:
        for o in corpusdb.session_rows(db, session):
            t = o["turn"]
            m = merged.setdefault(t, {"turn": t, "ts": o["ts"], "agent": o["agent"],
                                      "project": o["project"], "who": "you",
                                      "text": "", "reply": ""})
            if o["who"] == "agent":
                m["reply"] = o["text"]
            else:
                m["text"] = o["text"]
                m["who"] = o["who"]
    finally:
        db.close()
    return [merged[t] for t in sorted(merged)]


def get_window(session: str, center: int, n: int = 4) -> dict:
    """A contiguous window of one chat: turns [center-n, center+n] with replies, plus the
    tool/subagent events that happened during each of those turns. This is the middle
    tier between a search snippet and the whole transcript — callers (around CLI, agents)
    pull the local story of a hit for a few KB instead of re-reading a 50 MB session.

    Events carry only a ts, so they're attributed to the latest turn whose user message
    precedes them; the window's event range extends to the next turn AFTER the window
    (or forever on the last turn), since turn N's tool work happens before turn N+1."""
    _freshen()
    rows = _db_session_rows(session)
    if rows is None:  # no corpus db -> legacy in-memory path (parses messages.jsonl)
        rep = _replies_by_id()
        rows = []
        for o in sorted(_messages_by_session().get(session, []),
                        key=lambda o: o.get("turn", 0)):
            txt = o.get("text", "")
            rows.append({"turn": o.get("turn", 0), "ts": o.get("ts", 0),
                         "agent": o.get("agent", ""), "project": o.get("project", ""),
                         "who": "recap" if txt.startswith(RECAP_PREFIX) else "you",
                         "text": txt, "reply": rep.get(o.get("id", ""), "")})
    if not rows:
        return {"error": f"session {session} not found"}
    turn_nums = [o["turn"] for o in rows]
    center = max(turn_nums[0], min(center, turn_nums[-1]))  # clamp, never error
    lo, hi = center - n, center + n

    turns = [{"turn": o["turn"], "ts": o["ts"], "text": o["text"], "who": o["who"],
              "reply": o["reply"]} for o in rows if lo <= o["turn"] <= hi]

    agent = rows[0].get("agent", "")
    events = []
    if turns and has_events(agent, session):
        # ts boundaries: [first selected turn's msg, next turn after the window's msg)
        starts = {o.get("turn", 0): o.get("ts", 0) for o in rows}
        t0 = turns[0]["ts"]
        after = [starts[t] for t in turn_nums if t > turns[-1]["turn"] and starts.get(t)]
        t1 = min(after) if after else float("inf")
        sel = [t["turn"] for t in turns]
        for e in get_events(agent, session):
            ts = e.get("ts", 0)
            if t0 <= ts < t1:
                owner = max((t for t in sel if starts.get(t, 0) <= ts), default=sel[0])
                out = e.get("output", "") or ""
                events.append({"turn": owner, "ts": ts, "kind": e.get("kind", "tool"),
                               "name": e.get("name", ""), "input": e.get("input", ""),
                               "ok": e.get("ok", True), "output": out,
                               "output_chars": len(out)})

    summ = _summary_by_session().get(session)
    return {
        "session": session,
        "agent": agent,
        "project": rows[0].get("project", ""),
        "concept": _session_concept().get(session, ""),
        "title": summ.get("title", "") if summ else "",
        "n_msgs": len(rows),
        "first_turn": turn_nums[0],
        "last_turn": turn_nums[-1],
        "center": center,
        "turns": turns,
        "events": events,
    }


def stats() -> dict:
    """Honest corpus totals. The rail lists only summarized sessions, but keyword search
    reaches every message of every session — these numbers describe that full corpus so
    the UI never undersells (or oversells) what's actually indexed."""
    _freshen()
    idx = _session_index()
    return {
        "n_sessions": len(idx),
        "n_msgs": sum(r.get("n", 0) for r in idx.values()),
        "n_summarized": len(_summaries()),
        "n_summarized_msgs": sum(o.get("n_msgs", 0) for o in _summaries()),
        "n_vibes": len(_vibe_index()),
        # the username path segment, so the web client can treat "Users/<name>" as a
        # generic container — whoever runs tilt, without hardcoding anyone's name
        "user_seg": Path.home().name.lower(),
    }


def get_vibe(session: str) -> dict | None:
    p = common.DATA_DIR / "vibe" / f"{session}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- events

def _safe_name(s: str) -> str:
    """Mirror of the Rust ingest's file-name sanitizer (cache.rs safe_name)."""
    return "".join(c if (c.isascii() and c.isalnum()) or c in "-_." else "_" for c in s)


def events_path(agent: str, session: str):
    return common.DATA_DIR / EVENTS_DIR_NAME / f"{agent}-{_safe_name(session)}.jsonl"


@functools.lru_cache(maxsize=1)
def _event_sessions() -> set[str]:
    """Set of '{agent}-{safe_session}' stems that have an event file. One scandir,
    cached — lets list/detail rows say has_events without touching 12k files."""
    d = common.DATA_DIR / EVENTS_DIR_NAME
    if not d.exists():
        return set()
    return {p.name[:-6] for p in d.iterdir() if p.name.endswith(".jsonl")}


def has_events(agent: str, session: str) -> bool:
    return f"{agent}-{_safe_name(session)}" in _event_sessions()


def get_events(agent: str, session: str) -> list[dict]:
    """The tool/subagent event stream for one session, in ts order (as ingested).
    Read per-request from the per-session file — small, and deliberately NOT a global
    cache (the full event corpus can run to hundreds of MB)."""
    p = events_path(agent, session)
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            o = json.loads(line)
            o["i"] = i
            out.append(o)
    return out


def get_chat(session: str) -> dict:
    """Full detail for one chat: header + per-turn transcript annotated with affect, plus the
    vibe arc if one was built. Reads from the in-process caches (messages-by-session, affect,
    replies), so each open is a dict lookup over this session's rows rather than a scan of the
    50 MB messages.jsonl (+ emotions + replies) on every request."""
    _freshen()
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
        txt = o.get("text", "")
        turns.append({
            "turn": o.get("turn", 0),
            "ts": o.get("ts", 0),
            "text": txt,
            "rage": round(rage, 4),
            "hype": round(hype, 4),
            "hot": rage > HOT_T,
            "top": e.get("top", []),
            "model": o.get("model", ""),
            "reply": rep.get(o["id"], ""),
            "recap": txt.startswith(RECAP_PREFIX),
        })
    turns.sort(key=lambda t: t["turn"])

    # every model the chat ran on, most-used first. "<synthetic>" is claude's placeholder
    # on harness-fabricated turns (API-error retries etc.), not a model anyone picked.
    mcount: dict[str, int] = {}
    for t in turns:
        m = t.get("model", "")
        if m and m != "<synthetic>":
            mcount[m] = mcount.get(m, 0) + 1
    models = [{"name": m, "n": n}
              for m, n in sorted(mcount.items(), key=lambda kv: -kv[1])]
    session_model = models[0]["name"] if models else ""

    # header from the summary record if present
    summ = _summary_by_session().get(session)
    hot_n = sum(1 for t in turns if t["hot"])
    return {
        "session": session,
        "agent": agent or (summ.get("agent", "") if summ else ""),
        "project": project or (summ.get("cwd_project", "") if summ else ""),
        "model": session_model,
        "models": models,
        "first_ts": min((t["ts"] for t in turns if t["ts"]), default=0),
        "last_ts": max((t["ts"] for t in turns if t["ts"]), default=0),
        "concept": _session_concept().get(session, ""),
        "title": summ.get("title", "") if summ else "",
        "summary": summ.get("summary", "") if summ else "",
        "tags": summ.get("tags", []) if summ else [],
        "n_msgs": len(turns),
        "hot_n": hot_n,
        "hot_pct": round(hot_n / max(1, len(turns)) * 100, 1),
        "has_events": has_events(agent or (summ.get("agent", "") if summ else ""), session),
        "turns": turns,
        "vibe": get_vibe(session),
    }
