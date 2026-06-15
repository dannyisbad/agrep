"""Live watcher: passive, hook-free real-time view of every running agent session.

No hooks, no agent-side install, no instrumentation. Agents already journal everything
they do to disk as they do it (claude/codex/antigravity append JSONL; opencode commits
to SQLite). This module tails those stores directly:

  - JSONL stores are tailed by byte offset: each tick, stat the candidate files; on
    growth, read only the delta and parse the complete new lines.
  - The opencode DB is polled read-only (NOT immutable=1 -- immutable lets SQLite cache
    pages forever and new writes would be invisible) by part.time_created watermark.
  - The antigravity mailbox dir is scanned for new task-result mail.

"Active session" = its file/rows changed within ACTIVE_WINDOW seconds. On first sight
of an already-large file only the trailing TAIL_SEED bytes are parsed (seeding recent
history without replaying a 300MB session into the feed).

The watcher keeps an in-memory model {session -> recent events + current state} and
fans events out to SSE subscriber queues (see /live/state and /live/stream in server.py).
"""

from __future__ import annotations

import glob
import json
import os
import queue
import sqlite3
import threading
import time
from collections import deque

HOME = os.path.expanduser("~")
POLL_S = 0.7
ACTIVE_WINDOW_S = 90          # session counts as "running" if its store moved within this
STALL_S = 240                 # thinking/replying with no store write this long = dead turn
TAIL_SEED = 64 * 1024         # on first sight, parse at most this many trailing bytes
SNIP = 400                    # tool I/O previews stay short (one-line collapsed view)
MSG = 4000                    # reply/user MESSAGE text: carry enough to read in full on
                              # click-to-expand, not just a 400-char teaser
RING = 600                    # global recent-events ring
PER_SESSION = 200

_AGY_ACTIONS = {"RUN_COMMAND", "VIEW_FILE", "GREP_SEARCH", "CODE_ACTION",
                "LIST_DIRECTORY", "GENERIC"}
_OPENCODE_DBS = ["opencode.db", "opencode-dev.db", "opencode-local.db",
                 "opencode-dev-before-copy.db"]
_INPUT_KEYS = ("command", "file_path", "notebook_path", "path", "pattern", "query",
               "url", "prompt", "description", "cmd")

# Harness plumbing that gets logged as "user" text but was never typed by a person:
# XML-ish wrappers (<task-notification>, <command-name>, <local-command-caveat>, ...),
# interrupt markers, compaction recaps, injected AGENTS.md, replayed tool-call echoes.
_NOISE_PREFIX = ("<", "Caveat:", "[Request interrupted",
                 "This session is being continued", "# AGENTS.md", "Called the ")


def _noise(t: str) -> bool:
    t = t.lstrip()
    return not t or t.startswith(_NOISE_PREFIX)


def _snip(s, n=SNIP):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif")


def _is_img(s) -> bool:
    return isinstance(s, str) and s.lower().endswith(_IMG_EXTS)


def _image_files(v) -> list:
    """Every on-disk image a tool input points at -- a single Read `file_path`, OR a whole
    `SendUserFile` `files` array (agents ship comps in batches of up to 5 with a caption).
    Returns absolute paths; the files exist the instant the agent touches them, so the live
    view renders the pictures themselves instead of '[image] foo.png' stubs."""
    if not isinstance(v, dict):
        return []
    out = []
    for k in ("file_path", "path", "notebook_path"):
        if _is_img(v.get(k)):
            out.append(v[k])
    for k in ("files", "paths", "images", "attachments"):
        if isinstance(v.get(k), list):
            out.extend(x for x in v[k] if _is_img(x))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _img_records(imgs: list, cwd: str) -> list:
    """{p: served-path, n: display-name} for each image. A relative path (the agent
    referenced the file from its working dir, e.g. `audit_shots/x.png`) is resolved
    against the session's cwd so /file can find it on disk; the display name keeps the
    original basename."""
    recs = []
    for p in imgs:
        ap = p if (os.path.isabs(p) or not cwd) else os.path.normpath(os.path.join(cwd, p))
        recs.append({"p": ap, "n": os.path.basename(p)})
    return recs


def _summarize_input(v) -> str:
    if isinstance(v, dict):
        parts = []
        for k in _INPUT_KEYS:
            x = v.get(k)
            if isinstance(x, str) and x.strip():
                parts.append(x.strip())
            elif isinstance(x, list):
                joined = " ".join(str(i) for i in x)
                if joined:
                    parts.append(joined)
        if parts:
            return _snip(" · ".join(parts))
    return _snip(v) if v else ""


def _fid(st) -> float:
    """Stable per-file identity for rotation detection. Windows st_ctime IS creation
    time; macOS exposes real birth time as st_birthtime; on Linux st_ctime changes on
    every write (inode change time -- would read every append as a rotation), so the
    inode number is the identity there: a replaced file gets a new inode, appends never
    change it."""
    if os.name == "nt":
        return st.st_ctime
    bt = getattr(st, "st_birthtime", None)
    if bt is not None:
        return bt
    return float(st.st_ino)


# the username segment is whoever runs tilt, never a hardcoded name
_USER_SEG = os.path.basename(HOME.rstrip("/\\")).lower()
_GENERIC_LEAF = {_USER_SEG, "desktop", "documents", "downloads", "users", "user", "home",
                 "src", "web", "app", "lib", "client", "server", ""}


def _project_of(cwd: str) -> str:
    """Project bucket for a cwd, mirroring the Rust ingest's disambiguation: a
    generic leaf borrows its parent ('finance2/web'), a bare home dir reads '~'."""
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    leaf = parts[-1]
    if leaf.lower() not in _GENERIC_LEAF:
        return leaf
    if len(parts) >= 2 and parts[-2].lower() not in _GENERIC_LEAF:
        return f"{parts[-2]}/{leaf}"
    return "~"


_ROOT_SKIP = {"users", "user", _USER_SEG, "desktop", "documents", "downloads", "onedrive",
              "home", "tmp", "temp", "appdata", "src"}


def _project_root(path: str) -> str:
    """Project ROOT for an arbitrary path (mirrors the Rust ingest's project_root):
    '~/Desktop/tilt/py/live.py's dir' -> 'tilt'. '' when no signal."""
    p = path.replace("\\", "/")
    home = HOME.replace("\\", "/").lower()
    if p.lower().startswith(home):
        p = p[len(home):]
    for seg in p.split("/"):
        if not seg or seg.endswith(":") or seg.lower() in _ROOT_SKIP:
            continue
        return seg
    return ""


def _codex_ok(out) -> bool | None:
    """codex journals no structured outcome, but its shell wrapper prints
    'Process exited with code N' and JSON-shaped outputs carry metadata.exit_code."""
    if not isinstance(out, str) or not out:
        return None
    t = out.lstrip()
    if t.startswith("{"):
        try:
            o = json.loads(t)
            c = (o.get("metadata") or {}).get("exit_code")
            if isinstance(c, int):
                return c == 0
            out = o.get("output") or ""
        except (json.JSONDecodeError, AttributeError):
            pass
    i = out.find("Process exited with code ")
    if i < 0:
        return None
    j = i + len("Process exited with code ")
    k = j
    while k < len(out) and out[k].isdigit():
        k += 1
    return None if k == j else out[j:k] == "0"


def _ts_ms(iso: str | None) -> int:
    if not iso:
        return int(time.time() * 1000)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


class LiveWatcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="tilt-live")
        self._offsets: dict[str, int] = {}        # jsonl path -> consumed byte offset
        self._ctimes: dict[str, float] = {}       # jsonl path -> creation time at first sight
        self._partials: dict[str, str] = {}       # jsonl path -> trailing partial line
        self._codex_meta: dict[str, tuple[str, str]] = {}   # path -> (session, project)
        self._codex_model: dict[str, str] = {}              # path -> model (from turn_context)
        self._codex_cwd: dict[str, str] = {}                # path -> cwd (for relative image paths)
        self._claude_pending: dict[str, dict] = {}          # call_id -> live tool event
        self._codex_pending: dict[str, dict] = {}
        self._agy_announced: dict[str, deque] = {}          # session -> queued (name, input)
        self._agy_mail_seen: set[str] = set()
        self._oc_watermark: dict[str, int] = {}             # db path -> part.time_updated
        self._oc_msg_wm: dict[str, int] = {}                # db path -> message.time_updated
        self._oc_done: dict[str, int] = {}                  # session -> last done ts emitted
        self._oc_part_st: dict[str, str] = {}               # part id -> last seen status/kind
        self._oc_session_meta: dict[str, tuple[str, str]] = {}  # session -> (dir, title)
        self.sessions: dict[str, dict] = {}        # "agent:session" -> live session model
        self.ring: deque = deque(maxlen=RING)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._booted = False
        self._last_event_wall = 0.0  # wall time of the last emitted event (adaptive poll)
        self._n_emitted = 0  # post-boot emissions (delivered to subscribers)
        self._last_err = ""  # most recent tick exception (surfaced in /live/state)
        self._tick_ms: dict[str, int] = {}
        self._n_loops = 0

    # ------------------------------------------------------------- subscriptions

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, ev: dict) -> None:
        key = f"{ev['agent']}:{ev['session']}"
        s = self.sessions.setdefault(key, {
            "agent": ev["agent"], "session": ev["session"], "project": "",
            "title": "", "model": "", "last_ts": 0, "state": "", "turn_n": 0,
            "pending": {}, "recent": deque(maxlen=PER_SESSION),
        })
        # turn number rides every event so the feed can group a turn's work under
        # the prompt that caused it (a user message opens the next turn)
        if ev["type"] == "user" and not ev.get("sub"):
            s["turn_n"] += 1
        ev["tn"] = s["turn_n"]
        # "~" is the no-signal bucket; never let it clobber a real project the
        # session already showed (home-launched sessions flap between the two).
        p = ev.get("project")
        if p and (p != "~" or not s["project"]):
            s["project"] = p
        if ev.get("title"):
            s["title"] = ev["title"]
        if ev.get("model"):
            s["model"] = ev["model"]
        s["last_ts"] = max(s["last_ts"], ev.get("ts", 0))
        if ev.get("sub_session"):
            s["sub"] = True
        t = ev["type"]
        # Sidechain (subagent) events ride in the parent's file; they belong in the feed
        # but must not drive the parent's state (the parent is still inside its Task call).
        if not ev.get("sub"):
            pend: dict = s["pending"]
            ets = ev.get("ts") or int(time.time() * 1000)
            if t == "tool":
                cid = ev.get("call_id") or f"anon-{ets}"
                pend[cid] = (ev.get("name", "?"), ets)
            elif t == "tool_done":
                pend.pop(ev.get("call_id", ""), None)
            elif t in ("user", "done"):
                # turn boundary either way: whatever was pending is dead history
                pend.clear()
            # state reflects what is ACTUALLY outstanding, not just the last event:
            # "thinking" only when no tool is still running (parallel calls were the
            # big lie -- one result came back and the card said thinking while two
            # commands were still going). state_ts = when this state began, so the
            # UI can tick a live elapsed counter against it.
            if t in ("tool", "tool_done"):
                if pend:
                    nm, t0 = next(reversed(pend.values()))
                    extra = f" +{len(pend) - 1}" if len(pend) > 1 else ""
                    s["state"] = f"⚒ {nm}{extra}"
                    s["state_ts"] = t0
                else:
                    s["state"] = "thinking"
                    s["state_ts"] = ets
            elif t == "user":
                s["state"] = "thinking"  # the agent starts on it immediately
                s["state_ts"] = ets
            elif t == "reply":
                s["state"] = "replying"
                s["state_ts"] = ets
            elif t == "done":
                s["state"] = ev.get("why") or "done"
                s["state_ts"] = ets
        if t == "queued":
            if ev.get("op") == "enqueue":
                s["queued"] = s.get("queued", 0) + 1
                s["queued_text"] = ev.get("text", "")
            else:  # dequeue (consumed into a real user turn) or remove (cancelled)
                s["queued"] = max(0, s.get("queued", 0) - 1)
                if not s["queued"]:
                    s["queued_text"] = ""
            ev["squeued"] = s["queued"]
            ev["squeued_text"] = s["queued_text"]
        ev["sstate"] = s["state"]  # the session's resolved state rides every event
        if s.get("state_ts"):
            ev["sts"] = s["state_ts"]
        if ev.get("model") or s["model"]:
            ev["smodel"] = s["model"]
        self._last_event_wall = time.time()
        if t in ("done", "queued"):
            # state-only: turn-end / queue markers don't belong in the feed, but
            # subscribers need the push so the card updates without waiting on a poll.
            if self._booted:
                self._n_emitted += 1
                with self._lock:
                    for q in self._subs:
                        try:
                            q.put_nowait(ev)
                        except queue.Full:
                            pass
            return
        s["recent"].append(ev)
        self.ring.append(ev)
        if self._booted:  # seed parses fill history silently; only stream true deltas
            self._n_emitted += 1
            with self._lock:
                for q in self._subs:
                    try:
                        q.put_nowait(ev)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(ev)
                        except queue.Empty:
                            pass

    def snapshot(self) -> dict:
        now = time.time() * 1000
        active = []
        for s in self.sessions.values():
            if now - s["last_ts"] > ACTIVE_WINDOW_S * 1000 * 10:
                continue  # long-idle sessions drop from the snapshot entirely
            active.append({
                "agent": s["agent"], "session": s["session"], "project": s["project"],
                "title": s["title"], "model": s.get("model", ""),
                "last_ts": s["last_ts"], "state": s["state"],
                "state_ts": s.get("state_ts", 0),
                "queued": s.get("queued", 0), "queued_text": s.get("queued_text", ""),
                "sub": s.get("sub", False),
                "active": (now - s["last_ts"]) <= ACTIVE_WINDOW_S * 1000,
                "recent": list(s["recent"])[-60:],
            })
        active.sort(key=lambda s: -s["last_ts"])
        return {"now": int(now), "window_s": ACTIVE_WINDOW_S, "sessions": active,
                "n_subs": len(self._subs), "n_emitted": self._n_emitted,
                "n_tracked": len(self._offsets), "last_err": self._last_err,
                "tick_ms": dict(self._tick_ms), "n_loops": self._n_loops}

    # ------------------------------------------------------------- jsonl tailing

    def _read_delta(self, path: str, size: int, ctime: float = 0.0) -> list[str]:
        """New complete lines since the last consumed offset (seeding the tail on first sight)."""
        off = self._offsets.get(path)
        first = off is None
        # Rotation/recreation: same name but the file shrank, or its identity moved
        # (_fid: creation time / birthtime / inode, per platform). Re-seed from the tail.
        # Known hole: recreated LARGER under the same name within ~15s is invisible (NTFS
        # creation-time tunneling) -- can't happen for real stores (uuid-named sessions).
        if not first and (size < off or (ctime and self._ctimes.get(path) not in (None, ctime))):
            first = True
            self._partials[path] = ""
        if first:
            off = max(0, size - TAIL_SEED)
            if ctime:
                self._ctimes[path] = ctime
        if size <= off:
            self._offsets[path] = size  # keep current (handles the rotation-to-smaller case)
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(off)
                chunk = f.read(size - off)
        except OSError:
            return []
        self._offsets[path] = size
        if first and off > 0:
            # dropped into the middle of a line: discard up to the first newline
            nl = chunk.find("\n")
            chunk = chunk[nl + 1:] if nl >= 0 else ""
        buf = self._partials.get(path, "") + chunk
        lines = buf.split("\n")
        self._partials[path] = lines.pop()  # trailing partial (or "")
        return [ln for ln in lines if ln.strip()]

    # ------------------------------------------------------------- claude

    def _tick_claude(self, now: float) -> None:
        root = os.path.join(HOME, ".claude", "projects")
        try:
            proj_dirs = [e.path for e in os.scandir(root) if e.is_dir()]
        except OSError:
            return
        for d in proj_dirs:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".jsonl"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                known = e.path in self._offsets
                if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                    continue
                for ln in self._read_delta(e.path, st.st_size, _fid(st)):
                    self._claude_line(e.name[:-6], ln)

    def _claude_line(self, file_session: str, ln: str) -> None:
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            return
        sess = o.get("sessionId") or file_session
        if o.get("type") == "queue-operation":
            # prompts typed while the agent is mid-turn: hooks never see these;
            # the journal does (enqueue / dequeue when consumed / remove on cancel)
            self._emit({"agent": "claude", "session": sess,
                        "ts": _ts_ms(o.get("timestamp")), "type": "queued",
                        "op": o.get("operation", ""),
                        "text": _snip(str(o.get("content") or ""), 160)})
            return
        ts = _ts_ms(o.get("timestamp"))
        proj = ""
        cwd = o.get("cwd") or ""
        if cwd:
            proj = _project_of(cwd)
        msg = o.get("message") or {}
        content = msg.get("content")
        sub = bool(o.get("isSidechain"))
        base = {"agent": "claude", "session": sess, "ts": ts, "project": proj}
        if sub:
            base["sub"] = True
        model = msg.get("model") or ""
        if model and not (model.startswith("<") and model.endswith(">")):
            base["model"] = model
        if o.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                bt = b.get("type")
                if bt == "tool_use":
                    inp = b.get("input")
                    # home-launched session editing a real project by absolute path:
                    # the touched file is better project signal than the cwd
                    if (not proj or proj == "~") and isinstance(inp, dict):
                        for k in ("file_path", "notebook_path", "path"):
                            v = inp.get(k)
                            if isinstance(v, str) and "/" in v.replace("\\", "/"):
                                cand = _project_root(v.replace("\\", "/").rsplit("/", 1)[0])
                                if cand:
                                    base = {**base, "project": cand}
                                    break
                    ev = {**base, "type": "tool", "name": b.get("name", "?"),
                          "input": _summarize_input(inp),
                          "call_id": b.get("id", "")}
                    imgs = _image_files(inp)
                    if imgs:
                        ev["imgs"] = _img_records(imgs, cwd)
                        cap = inp.get("caption") if isinstance(inp, dict) else None
                        if isinstance(cap, str) and cap.strip():
                            ev["imgcap"] = _snip(cap, 200)
                    if ev["call_id"]:
                        self._claude_pending[ev["call_id"]] = ev
                    self._emit(ev)
                elif bt == "text" and b.get("text", "").strip():
                    self._emit({**base, "type": "reply", "text": _snip(b["text"], MSG)})
            # The final assistant message of a turn carries a terminal stop_reason
            # (tool_use = mid-turn). This is what flips the card off "replying".
            if not sub and msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens"):
                self._emit({**base, "type": "done"})
        elif o.get("type") == "user" and not o.get("isMeta"):
            if isinstance(content, list):
                texts = []
                for b in content:
                    if b.get("type") == "text":
                        texts.append(b.get("text", ""))
                        continue
                    if b.get("type") != "tool_result":
                        continue
                    cid = b.get("tool_use_id", "")
                    started = self._claude_pending.pop(cid, None)
                    c = b.get("content")
                    out = c if isinstance(c, str) else "\n".join(
                        x.get("text", "") for x in c if x.get("type") == "text") if isinstance(c, list) else ""
                    done = {**base, "type": "tool_done", "call_id": cid,
                            "name": (started or {}).get("name", ""),
                            "ok": not bool(b.get("is_error")),
                            "output": _snip(out)}
                    if started and started.get("ts"):
                        done["dur"] = max(0, ts - started["ts"])
                    self._emit(done)
                # typed messages with attachments arrive as text blocks, not a plain string
                joined = "\n".join(t for t in texts if t.strip())
                if not sub and joined and not _noise(joined):
                    self._emit({**base, "type": "user", "text": _snip(joined, MSG)})
            elif isinstance(content, str) and not sub:
                if content.lstrip().startswith("[Request interrupted"):
                    # the turn is dead right now -- don't leave the card on "⚒ tool"
                    self._emit({**base, "type": "done", "why": "interrupted"})
                elif not _noise(content):
                    self._emit({**base, "type": "user", "text": _snip(content, MSG)})

    # ------------------------------------------------------------- codex

    def _tick_codex(self, now: float) -> None:
        # only today's (+ yesterday's, for midnight spans) date dirs can be live
        root = os.path.join(HOME, ".codex", "sessions")
        days = {time.strftime("%Y/%m/%d", time.localtime(now - dt)) for dt in (0, 86400)}
        for day in days:
            d = os.path.join(root, *day.split("/"))
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not (e.name.startswith("rollout-") and e.name.endswith(".jsonl")):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                known = e.path in self._offsets
                if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                    continue
                if e.path not in self._codex_meta:
                    self._codex_meta[e.path] = self._codex_head(e.path)
                for ln in self._read_delta(e.path, st.st_size, _fid(st)):
                    self._codex_line(e.path, ln)

    @staticmethod
    def _codex_head(path: str) -> tuple[str, str]:
        """(session, project) from the rollout's session_meta first line."""
        sess = os.path.basename(path)[:-6]
        parts = sess.split("-")
        if len(parts) >= 5:
            sess = "-".join(parts[-5:])
        proj = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                o = json.loads(f.readline())
            p = o.get("payload") or {}
            sess = p.get("id") or sess
            cwd = p.get("cwd") or ""
            proj = _project_of(cwd) if cwd else ""
        except (OSError, json.JSONDecodeError):
            pass
        return sess, proj

    def _codex_line(self, path: str, ln: str) -> None:
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            return
        p = o.get("payload") or {}
        if o.get("type") == "turn_context":
            # per-turn truth: codex re-journals cwd + model at every turn start
            sess0, proj0 = self._codex_meta.get(path, ("?", ""))
            cwd = p.get("cwd") or ""
            if cwd:
                self._codex_meta[path] = (sess0, _project_of(cwd) or proj0)
                self._codex_cwd[path] = cwd
            if p.get("model"):
                self._codex_model[path] = p["model"]
            return
        sess, proj = self._codex_meta.get(path, ("?", ""))
        base = {"agent": "codex", "session": sess, "ts": _ts_ms(o.get("timestamp")),
                "project": proj}
        m = self._codex_model.get(path)
        if m:
            base["model"] = m
        if o.get("type") == "event_msg":
            # codex journals its own lifecycle: task_complete = the turn actually
            # ended; turn_aborted = the user killed it (esc / ctrl-c)
            pt = p.get("type")
            if pt == "task_complete":
                self._emit({**base, "type": "done"})
            elif pt == "turn_aborted":
                self._emit({**base, "type": "done", "why": "interrupted"})
            return
        if o.get("type") != "response_item":
            return
        ty = p.get("type")
        if ty in ("function_call", "custom_tool_call"):
            args = p.get("arguments")
            argd = None
            try:
                argd = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                argd = None
            inp = _summarize_input(argd) if isinstance(argd, dict) else _snip(args or p.get("input") or "")
            ev = {**base, "type": "tool", "name": p.get("name", "?"), "input": inp,
                  "call_id": p.get("call_id", "")}
            imgs = _image_files(argd)
            if imgs:
                ev["imgs"] = _img_records(imgs, self._codex_cwd.get(path, ""))
                cap = argd.get("caption") if isinstance(argd, dict) else None
                if isinstance(cap, str) and cap.strip():
                    ev["imgcap"] = _snip(cap, 200)
            if ev["call_id"]:
                self._codex_pending[ev["call_id"]] = ev
            self._emit(ev)
        elif ty in ("function_call_output", "custom_tool_call_output"):
            cid = p.get("call_id", "")
            started = self._codex_pending.pop(cid, None)
            out = p.get("output")
            done = {**base, "type": "tool_done", "call_id": cid,
                    "name": (started or {}).get("name", ""),
                    "output": _snip(out if isinstance(out, str) else out or "")}
            ok = _codex_ok(out)
            if ok is not None:
                done["ok"] = ok
            if started and started.get("ts"):
                done["dur"] = max(0, base["ts"] - started["ts"])
            self._emit(done)
        elif ty == "message":
            blocks = p.get("content") or []
            text = "\n".join(b.get("text", "") for b in blocks
                             if b.get("type") in ("input_text", "output_text", "text"))
            if not text.strip():
                return
            if p.get("role") == "assistant":
                self._emit({**base, "type": "reply", "text": _snip(text, MSG)})
            elif p.get("role") == "user" and not _noise(text):
                self._emit({**base, "type": "user", "text": _snip(text, MSG)})

    # ------------------------------------------------------------- opencode

    def _tick_opencode(self, now: float) -> None:
        base_dir = os.path.join(HOME, ".local", "share", "opencode")
        for name in _OPENCODE_DBS:
            path = os.path.join(base_dir, name)
            try:
                mt = os.path.getmtime(path)
                wal = path + "-wal"
                if os.path.exists(wal):
                    mt = max(mt, os.path.getmtime(wal))
            except OSError:
                continue
            first = path not in self._oc_watermark
            if now - mt > ACTIVE_WINDOW_S and not first:
                continue
            self._poll_opencode_db(path, first)

    def _poll_opencode_db(self, path: str, first: bool) -> None:
        # read-only WITHOUT immutable: we *want* to see the writer's new pages.
        uri = "file:{}?mode=ro".format(path.replace("\\", "/"))
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            if first:
                row = conn.execute("SELECT COALESCE(MAX(time_updated),0) FROM part").fetchone()
                # seed: start from 2 minutes back so a just-started session shows context
                self._oc_watermark[path] = max(0, (row[0] or 0) - 120_000)
                row = conn.execute("SELECT COALESCE(MAX(time_updated),0) FROM message").fetchone()
                # same 2-min backfill as parts: a turn that completed just before boot
                # replays its done marker instead of leaving the card stuck "replying"
                self._oc_msg_wm[path] = max(0, (row[0] or 0) - 120_000)
            wm = self._oc_watermark[path]
            # Poll by time_UPDATED, not time_created: a tool part is born "running" and
            # its row mutates in place to "completed" -- a created-watermark sees it once
            # and the transition never lands (cards stuck on "⚒ tool" forever).
            rows = conn.execute(
                "SELECT m.session_id, p.id, p.time_updated, p.data, m.data, "
                "       COALESCE(s.directory,''), COALESCE(s.title,''), s.parent_id "
                "FROM part p JOIN message m ON p.message_id = m.id "
                "JOIN session s ON m.session_id = s.id "
                "WHERE p.time_updated > ? ORDER BY p.time_updated LIMIT 800", (wm,)).fetchall()
            # Turn end: opencode stamps message.data.time.completed when the assistant turn
            # finishes, which bumps message.time_updated -- a watermark on that catches the
            # completion even though no new part row is ever written for it.
            mrows = conn.execute(
                "SELECT m.session_id, m.time_updated, m.data FROM message m "
                "WHERE m.time_updated > ? ORDER BY m.time_updated LIMIT 200",
                (self._oc_msg_wm[path],)).fetchall()
            conn.close()
        except sqlite3.Error:
            return
        if len(self._oc_part_st) > 8000:  # bound the dedupe map; transient dupes beat growth
            self._oc_part_st.clear()
        for sess, pid, ts, pdata, mdata, directory, title, parent_id in rows:
            self._oc_watermark[path] = max(self._oc_watermark[path], ts)
            try:
                pd = json.loads(pdata)
                md = json.loads(mdata) or {}
            except json.JSONDecodeError:
                continue
            role = md.get("role", "")
            proj = _project_of(directory) if directory else ""
            base = {"agent": "opencode", "session": sess, "ts": ts, "project": proj,
                    "title": title}
            if md.get("modelID"):
                base["model"] = md["modelID"]
            if parent_id:
                base["sub_session"] = True
            pt = pd.get("type")
            prev = self._oc_part_st.get(pid)
            if pt == "tool":
                st = pd.get("state") or {}
                status = st.get("status", "")
                running = status in ("pending", "running")
                # emit only on transitions: born->running = one "tool", ->terminal =
                # one "tool_done"; repeated updates at the same stage stay silent
                if prev == status or (running and prev in ("pending", "running")):
                    continue
                self._oc_part_st[pid] = status
                out = st.get("output")
                ev = {**base, "type": "tool" if running else "tool_done",
                      "name": pd.get("tool", "?"),
                      "input": _summarize_input(st.get("input")),
                      "output": _snip(out if isinstance(out, str) else out or ""),
                      "ok": {"completed": True, "error": False}.get(status),
                      "call_id": pd.get("callID", "")}
                tm = st.get("time") or {}
                if not running and tm.get("start") and tm.get("end"):
                    ev["dur"] = max(0, tm["end"] - tm["start"])
                if running and tm.get("start"):
                    ev["ts"] = tm["start"]  # elapsed ticks from the real start
                self._emit(ev)
            elif pt == "text" and pd.get("text", "").strip():
                if prev is not None:
                    continue  # text parts mutate as the reply streams; one feed row is enough
                self._oc_part_st[pid] = "text"
                t = pd["text"]
                if role == "assistant":
                    self._emit({**base, "type": "reply", "text": _snip(t, MSG)})
                elif role == "user" and not _noise(t):
                    self._emit({**base, "type": "user", "text": _snip(t, MSG)})
        for sess, tu, mdata in mrows:
            self._oc_msg_wm[path] = max(self._oc_msg_wm[path], tu)
            try:
                md = json.loads(mdata)
            except json.JSONDecodeError:
                continue
            done_ts = (md.get("time") or {}).get("completed")
            if md.get("role") == "assistant" and done_ts and self._oc_done.get(sess) != done_ts:
                self._oc_done[sess] = done_ts
                self._emit({"agent": "opencode", "session": sess, "ts": done_ts,
                            "type": "done"})

    # ------------------------------------------------------------- antigravity

    def _tick_antigravity(self, now: float) -> None:
        root = os.path.join(HOME, ".gemini", "antigravity-cli", "brain")
        try:
            dirs = [e for e in os.scandir(root) if e.is_dir()]
        except OSError:
            return
        for d in dirs:
            tr = os.path.join(d.path, ".system_generated", "logs", "transcript.jsonl")
            try:
                st = os.stat(tr)
            except OSError:
                continue
            known = tr in self._offsets
            if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                continue
            for ln in self._read_delta(tr, st.st_size, _fid(st)):
                self._agy_line(d.name, ln)
            self._agy_mailbox(d.path, d.name, now)

    def _agy_line(self, sess: str, ln: str) -> None:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            return
        ty, src = e.get("type"), e.get("source")
        ts = _ts_ms(e.get("created_at"))
        base = {"agent": "antigravity", "session": sess, "ts": ts}
        q = self._agy_announced.setdefault(sess, deque(maxlen=50))
        if ty == "PLANNER_RESPONSE" and src == "MODEL":
            tcs = e.get("tool_calls") or []
            for tc in tcs:
                args = tc.get("args") or {}
                inp = ""
                for k in ("CommandLine", "AbsolutePath", "Query", "toolSummary"):
                    v = args.get(k)
                    if isinstance(v, str) and v.strip():
                        inp = v.strip().strip('"')
                        break
                q.append((tc.get("name", "?"), _snip(inp)))
            c = e.get("content")
            if isinstance(c, str) and c.strip():
                self._emit({**base, "type": "reply", "text": _snip(c, MSG)})
            if not tcs:
                # the planner answering without queueing any tool call IS the turn end
                self._emit({**base, "type": "done"})
        elif src == "MODEL" and ty in _AGY_ACTIONS:
            name, inp = q.popleft() if q else (ty.lower(), "")
            self._emit({**base, "type": "tool_done", "name": name, "input": inp,
                        "output": _snip(str(e.get("content") or "")),
                        "ok": {"DONE": True, "ERROR": False}.get(e.get("status"))})
        elif ty == "USER_INPUT" and src == "USER_EXPLICIT":
            c = str(e.get("content") or "")
            o, cl = c.find("<USER_REQUEST>"), c.find("</USER_REQUEST>")
            if o >= 0 and cl > o:
                c = c[o + len("<USER_REQUEST>"):cl]
            if c.strip():
                self._emit({**base, "type": "user", "text": _snip(c, MSG)})

    def _agy_mailbox(self, brain_path: str, sess: str, now: float) -> None:
        mdir = os.path.join(brain_path, ".system_generated", "messages")
        try:
            entries = list(os.scandir(mdir))
        except OSError:
            return
        for e in entries:
            if not e.name.endswith(".json") or e.path in self._agy_mail_seen:
                continue
            self._agy_mail_seen.add(e.path)
            try:
                st = e.stat()
                if not self._booted and now - st.st_mtime > ACTIVE_WINDOW_S:
                    continue  # old mail: mark seen, don't emit
                m = json.loads(open(e.path, encoding="utf-8", errors="replace").read())
            except (OSError, json.JSONDecodeError):
                continue
            sender = m.get("sender", "")
            if "/task-" not in sender:
                continue
            self._emit({"agent": "antigravity", "session": sess,
                        "ts": int(st.st_mtime * 1000), "type": "subagent_result",
                        "name": (m.get("renderDetails") or {}).get("messageTitle", "")
                        or sender.rsplit("/", 1)[-1],
                        "output": _snip(m.get("content") or "")})

    # ------------------------------------------------------------- loop

    def run(self) -> None:
        while True:
            t0 = time.time()
            now = t0
            for tick in (self._tick_claude, self._tick_codex,
                         self._tick_opencode, self._tick_antigravity):
                tt = time.perf_counter()
                try:
                    tick(now)
                except Exception as e:  # noqa: BLE001 -- one bad store must not kill the watcher
                    import traceback
                    self._last_err = f"{tick.__name__}: {e!r} @ {traceback.format_exc(limit=3).splitlines()[-2].strip()}"
                self._tick_ms[tick.__name__] = int((time.perf_counter() - tt) * 1000)
            self._n_loops += 1
            self._booted = True
            self._decay(now)
            # adaptive cadence: tight on the heels of activity, relaxed when quiet
            poll = 0.35 if (time.time() - self._last_event_wall) < 10 else POLL_S
            time.sleep(max(0.1, poll - (time.time() - t0)))

    def _decay(self, now: float) -> None:
        """A turn that goes silent is a turn that died: 'thinking'/'replying' with no
        store write for STALL_S means the CLI was closed or crashed mid-turn (hooks
        would get a session-end callback; silence is the passive equivalent). Tool
        states are exempt -- a quiet 10-minute cargo build is normal and the ticking
        elapsed already tells that story."""
        nowms = now * 1000
        for s in list(self.sessions.values()):
            if s.get("state") in ("thinking", "replying") and s.get("last_ts") \
                    and nowms - s["last_ts"] > STALL_S * 1000:
                # keep the original last_ts (+1ms) so the card stays faded-idle
                self._emit({"agent": s["agent"], "session": s["session"],
                            "ts": s["last_ts"] + 1, "type": "done", "why": "stalled"})


_WATCHER: LiveWatcher | None = None


def watcher() -> LiveWatcher:
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = LiveWatcher()
        _WATCHER.start()
    return _WATCHER
