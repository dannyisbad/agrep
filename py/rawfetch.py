"""Fetch ONE event's uncapped input/output straight from the source store.

The indexed event files (data/events/) hold capped summaries; the full payload never
leaves the agent's own store. Given (agent, session, call_id) — the same provenance the
Rust ingest stamped on every event — this re-opens the source and digs the whole thing
out. Used by the /event_raw endpoint when the UI's "full" button is clicked.

Pure stdlib, read-only, no caches: one click = one targeted scan of one session's data.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3

HOME = os.path.expanduser("~")

# Same action-event pairing the Rust antigravity adapter uses (kept in sync by the
# count-parity check in the verify suite).
_AGY_ACTIONS = {"RUN_COMMAND", "VIEW_FILE", "GREP_SEARCH", "CODE_ACTION",
                "LIST_DIRECTORY", "GENERIC"}
_OPENCODE_DBS = ["opencode.db", "opencode-dev.db", "opencode-local.db",
                 "opencode-dev-before-copy.db"]


def _not_found(why: str = "event not found in source store") -> dict:
    return {"error": why}


def _claude(session: str, call_id: str) -> dict:
    hits = glob.glob(os.path.join(HOME, ".claude", "projects", "*", f"{session}.jsonl"))
    out: dict = {}
    for path in hits:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if call_id not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (o.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if b.get("type") == "tool_use" and b.get("id") == call_id:
                        out["name"] = b.get("name", "")
                        out["input"] = json.dumps(b.get("input"), indent=2, ensure_ascii=False)
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") == call_id:
                        c = b.get("content")
                        if isinstance(c, str):
                            out["output"] = c
                        elif isinstance(c, list):
                            out["output"] = "\n".join(
                                x.get("text", "") for x in c if x.get("type") == "text")
                        out["ok"] = not bool(b.get("is_error"))
                if "input" in out and "output" in out:
                    return out
    return out or _not_found()


def _codex(session: str, call_id: str) -> dict:
    pattern = os.path.join(HOME, ".codex", "sessions", "**", f"rollout-*{session}.jsonl")
    out: dict = {}
    for path in glob.glob(pattern, recursive=True):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if call_id not in line:
                    continue
                try:
                    p = json.loads(line).get("payload") or {}
                except json.JSONDecodeError:
                    continue
                if p.get("call_id") != call_id:
                    continue
                ty = p.get("type", "")
                if ty in ("function_call", "custom_tool_call"):
                    out["name"] = p.get("name", "")
                    args = p.get("arguments") or p.get("input") or ""
                    try:  # arguments is a JSON string; pretty it when possible
                        out["input"] = json.dumps(json.loads(args), indent=2, ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError):
                        out["input"] = str(args)
                elif ty.endswith("_output"):
                    o = p.get("output")
                    out["output"] = o if isinstance(o, str) else json.dumps(o, indent=2, ensure_ascii=False)
        if "input" in out and "output" in out:
            return out
    return out or _not_found()


def _opencode(session: str, call_id: str) -> dict:
    base = os.path.join(HOME, ".local", "share", "opencode")
    for name in _OPENCODE_DBS:
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        uri = "file:{}?mode=ro&immutable=1".format(path.replace("\\", "/"))
        try:
            conn = sqlite3.connect(uri, uri=True)
            rows = conn.execute(
                "SELECT p.data FROM part p WHERE p.session_id = ? AND p.data LIKE ?",
                (session, f'%{call_id}%')).fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for (data,) in rows:
            try:
                o = json.loads(data)
            except json.JSONDecodeError:
                continue
            if o.get("type") != "tool" or o.get("callID") != call_id:
                continue
            st = o.get("state") or {}
            outp = st.get("output")
            return {
                "name": o.get("tool", ""),
                "input": json.dumps(st.get("input"), indent=2, ensure_ascii=False),
                "output": outp if isinstance(outp, str) else json.dumps(outp, indent=2, ensure_ascii=False),
                "ok": {"completed": True, "error": False}.get(st.get("status")),
            }
    return _not_found()


def _antigravity(session: str, call_id: str) -> dict:
    brain = os.path.join(HOME, ".gemini", "antigravity-cli", "brain", session)

    # Mailbox result: call_id is the mail's own id (or file stem).
    if not call_id.startswith("ag"):
        for path in glob.glob(os.path.join(brain, ".system_generated", "messages", "*.json")):
            try:
                m = json.loads(open(path, encoding="utf-8", errors="replace").read())
            except (json.JSONDecodeError, OSError):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            if m.get("id") == call_id or stem == call_id:
                return {"name": (m.get("renderDetails") or {}).get("messageTitle", ""),
                        "input": m.get("sender", ""), "output": m.get("content", "")}
        return _not_found()

    # Synthesized id "ag<N>": replay the same FIFO pairing the ingest did and stop at N.
    try:
        want = int(call_id[2:])
    except ValueError:
        return _not_found("bad antigravity call id")
    transcript = os.path.join(brain, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(transcript):
        return _not_found("transcript missing")
    announced: list[tuple[str, str]] = []
    seq = 0
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ty, src = e.get("type"), e.get("source")
            if ty == "PLANNER_RESPONSE" and src == "MODEL":
                for tc in e.get("tool_calls") or []:
                    announced.append((tc.get("name", "?"),
                                      json.dumps(tc.get("args"), indent=2, ensure_ascii=False)))
                continue
            if src != "MODEL":
                continue
            if ty in _AGY_ACTIONS or ty == "INVOKE_SUBAGENT":
                seq += 1
                if seq != want:
                    if ty in _AGY_ACTIONS and announced:
                        announced.pop(0)
                    continue
                if ty == "INVOKE_SUBAGENT":
                    return {"name": "subagent", "input": str(e.get("content") or "")}
                name, args = (announced.pop(0) if announced else (ty.lower(), ""))
                return {"name": name, "input": args,
                        "output": str(e.get("content") or ""),
                        "ok": {"DONE": True, "ERROR": False}.get(e.get("status"))}
    return _not_found()


def event_raw(agent: str, session: str, call_id: str) -> dict:
    """The full, uncapped payload of one event, straight from the agent's own store."""
    if not session or not call_id or any(c in session for c in "/\\.."):
        return _not_found("bad arguments")
    fn = {"claude": _claude, "codex": _codex,
          "opencode": _opencode, "antigravity": _antigravity}.get(agent)
    if fn is None:
        return _not_found(f"unknown agent {agent!r}")
    return fn(session, call_id)
