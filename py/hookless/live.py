"""Live watcher for the registry's supported hook-free agent stores.

No hooks, no agent-side install, no instrumentation. Supported agents already journal
what they do to disk (claude/codex/pi/antigravity append JSONL; opencode and Cursor
commit to SQLite). This module tails those stores directly:

  - JSONL stores are tailed by byte offset: each tick, stat the candidate files; on
    growth, read only the delta and parse the complete new lines.
  - OpenCode DBs are polled read-only (NOT immutable=1 -- immutable lets SQLite cache
    pages forever and new writes would be invisible) by stable updated-time watermarks.
  - The antigravity mailbox dir is scanned for new task-result mail.

"Active session" = its file/rows changed within ACTIVE_WINDOW seconds. On first sight
of an already-large file only the trailing TAIL_SEED bytes are parsed (seeding recent
history without replaying a 300MB session into the feed).

The watcher keeps an in-memory model {session -> recent events + current state} and
fans events out to subscribers used by `agrep board`, `agrep tail`, and indexd.
"""

from __future__ import annotations

import errno
import json
import os
import queue
import sqlite3
import stat
import threading
import time
from collections import deque
from pathlib import Path

from hookless import proc
from hookless.locators import (
    cursor_db_paths,
    discovery_home,
    store_content,
    store_root,
    store_roots,
)
from hookless import procscan
from hookless.native import opencode_db_paths
from hookless.registry import LIVE_AGENTS, require_exact

HOME = discovery_home()
POLL_S = 0.7
ACTIVE_WINDOW_S = 90          # session counts as "running" if its store moved within this
STALL_S = 240                 # thinking/replying with no store write this long = dead turn
TAIL_SEED = 64 * 1024         # on first sight, parse at most this many trailing bytes
SNIP = 400                    # tool I/O previews stay short (one-line collapsed view)
MSG = 4000                    # reply/user MESSAGE text: read-in-full on click-to-expand, not a SNIP teaser
RING = 600                    # global recent-events ring
PER_SESSION = 200
_SNAPSHOT_EXPIRE_S = ACTIVE_WINDOW_S * 10
_PROCESS_SCAN_S = 2.0
LIVE_TICKS = {
    "claude": "_tick_claude",
    "codex": "_tick_codex",
    "opencode": "_tick_opencode",
    "antigravity": "_tick_antigravity",
    "cursor": "_tick_cursor",
    "pi": "_tick_pi",
}
require_exact("live watcher ticks", LIVE_TICKS, LIVE_AGENTS)

_AGY_ACTIONS = {"RUN_COMMAND", "VIEW_FILE", "GREP_SEARCH", "CODE_ACTION",
                "LIST_DIRECTORY", "GENERIC"}
_INPUT_KEYS = ("command", "file_path", "notebook_path", "path", "pattern", "query",
               "url", "prompt", "description", "cmd")


def _source_identity(found: os.stat_result) -> tuple[int, int, int]:
    return found.st_dev, found.st_ino, stat.S_IFMT(found.st_mode)


def _source_reparse(found: os.stat_result) -> bool:
    return bool(getattr(found, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _probe_source(path: str) -> os.stat_result:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or _source_reparse(before):
        raise OSError(errno.ELOOP, "source path is a link or reparse point", path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    if stat.S_ISDIR(before.st_mode):
        if os.name == "nt":
            with os.scandir(path) as entries:
                next(entries, None)
            final = os.lstat(path)
            if (_source_identity(final) != _source_identity(before)
                    or _source_reparse(final)):
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "source identity changed while probing", path)
            return final
        flags |= getattr(os, "O_DIRECTORY", 0)
    elif not stat.S_ISREG(before.st_mode):
        raise OSError(errno.EINVAL, "source is not a regular file or directory", path)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (_source_identity(opened) != _source_identity(before)
                or (stat.S_ISDIR(before.st_mode) and not stat.S_ISDIR(opened.st_mode))
                or (stat.S_ISREG(before.st_mode) and not stat.S_ISREG(opened.st_mode))):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "source identity changed while opening", path)
        if stat.S_ISDIR(opened.st_mode):
            with os.scandir(descriptor) as entries:
                next(entries, None)
        else:
            os.read(descriptor, 1)
        final = os.lstat(path)
        if (_source_identity(final) != _source_identity(opened)
                or _source_reparse(final)):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "source identity changed while probing", path)
        return opened
    finally:
        os.close(descriptor)


def _plain_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _plain_file_stat(path: str) -> os.stat_result | None:
    try:
        found = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return found if stat.S_ISREG(found.st_mode) else None


def _noise(t: str) -> bool:
    """Shared cold/live wrapper vocabulary. Do not reject every '<...': prompts such as
    '<repo> keeps failing' are perfectly legitimate user text."""
    t = t.lstrip()
    if not t:
        return True
    prefixes = (
        "<command-name>", "<command-message>", "<command-args>", "<local-command",
        "<bash-input>", "<bash-stdout>", "<user-prompt-submit-hook>",
        "<system-reminder>", "<teammate-message", "<task-notification",
        "[SYSTEM NOTIFICATION", "<system-notification", "Caveat:",
    )
    if t.startswith(prefixes):
        return True
    if "<command-name>" in t and "</command-name>" in t:
        return True
    if t.startswith("{") and any(marker in t for marker in (
            '"idle_notification"', '"task_completed"', '"shutdown_request"',
            '"type":"idle"')):
        return True
    return False


def _codex_noise(t: str) -> bool:
    """Codex-only role:user injections, matching ingest/codex.rs."""
    t = t.lstrip()
    return (_noise(t) or t.startswith((
        "# AGENTS.md", "<environment_context", "<INSTRUCTIONS", "<permissions",
        "<user_instructions", "<turn_aborted", "<subagent_notification",
        "<goal_context", "<codex_internal_context", "<codex_delegation",
        "<realtime_delegation", "<user_action", "<image",
    )) or "<user_instructions>" in t)


def _opencode_noise(t: str) -> bool:
    t = t.lstrip()
    return _noise(t) or t.startswith((
        "<path>", "<type>", "<content>", "Called the ", "Image read successfully",
    ))


def _opencode_model_id(message: dict, session_model) -> str:
    for candidate in (message.get("model"), session_model):
        if isinstance(candidate, dict):
            model = candidate.get("id")
            if isinstance(model, str):
                return model
        elif isinstance(candidate, str) and candidate:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                return candidate
            if isinstance(decoded, dict) and isinstance(decoded.get("id"), str):
                return decoded["id"]
            if isinstance(decoded, str):
                return decoded
    return ""


def _opencode_v2_output(state: dict):
    output = state.get("output")
    if isinstance(output, str):
        return output
    content = state.get("content")
    if isinstance(content, list):
        texts = [
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    return output or ""


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
        # prefer real birth time (present since 3.12): CPython has announced
        # flipping windows st_ctime to change-time, which would read every
        # append as a rotation
        bt = getattr(st, "st_birthtime", None)
        return bt if bt is not None else st.st_ctime
    bt = getattr(st, "st_birthtime", None)
    if bt is not None:
        return bt
    return float(st.st_ino)


# the username segment is whoever runs agrep, never a hardcoded name
_USER_SEG = os.path.basename(HOME.rstrip("/\\")).lower()
_GENERIC_LEAF = {_USER_SEG, "desktop", "documents", "downloads", "src", "web",
                 "mobile", "app", "lib", "client", "server"}


def _project_of(cwd: str) -> str:
    """Exact live port of Rust ``project_name``.

    Generic leaves borrow their parent (``sampleapp/web``); a home directory from this
    or a migrated machine reads ``~``. Keep this deliberately boring: live project
    filters must use the same spelling as the indexed rows.
    """
    parts = [p.strip('"\' ') for p in cwd.replace("\\", "/").split("/")
             if p.strip('"\' ')]
    if not parts:
        return "unknown"
    leaf = parts[-1]
    if len(parts) >= 2 and len(parts) - 2 <= 1 and parts[-2].lower() in ("users", "home"):
        return "~"
    if leaf.lower() in _GENERIC_LEAF and len(parts) >= 2:
        return f"{parts[-2]}/{leaf}"
    return leaf


_ROOT_SKIP = {"users", _USER_SEG, "desktop", "documents", "downloads", "onedrive",
              "home", "tmp", "temp", "appdata", "src"}


def _project_root(path: str) -> str:
    """Project ROOT for an arbitrary path (mirrors the Rust ingest's project_root):
    ``~/Desktop/agrep/py/module.py`` maps to ``agrep``. Empty means no signal."""
    p = path.replace("\\", "/")
    home = HOME.replace("\\", "/").lower()
    if p.lower().startswith(home):
        p = p[len(home):]
    skip_user = False
    for raw in p.split("/"):
        seg = raw.strip('"\' ')
        lower = seg.lower()
        if skip_user:
            skip_user = False
            continue
        if lower in ("users", "home"):
            skip_user = True
            continue
        if not seg or seg.endswith(":") or lower in _ROOT_SKIP:
            continue
        return seg
    return ""


def _codex_call_output(out) -> tuple[str, bool | None]:
    """Normalize Codex tool output exactly like the cold ingester.

    Journals use a plain string, a JSON-shaped string, or a nested object depending on
    the command wrapper/version. Return clean display text plus a recorded exit status.
    """
    value = out
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            value = json.loads(value.lstrip())
        except json.JSONDecodeError:
            value = out
    if isinstance(value, dict):
        text = value.get("output")
        text = text if isinstance(text, str) else json.dumps(value, ensure_ascii=False)
        code = (value.get("metadata") or {}).get("exit_code")
        if isinstance(code, int):
            return text, code == 0
    else:
        text = value if isinstance(value, str) else (json.dumps(value, ensure_ascii=False)
                                                     if value is not None else "")
    i = text.find("Process exited with code ")
    if i < 0:
        return text, None
    j = i + len("Process exited with code ")
    k = j
    while k < len(text) and text[k].isdigit():
        k += 1
    return text, (None if k == j else text[j:k] == "0")


def _codex_ok(out) -> bool | None:
    """Compatibility helper for callers that only need the normalized outcome."""
    return _codex_call_output(out)[1]


def _ts_ms(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _uuid7_after(candidate: str, earlier: str) -> bool:
    """True only for canonical UUIDv7 ids ordered after another UUIDv7 id."""
    def valid(value: str) -> bool:
        return (len(value) == 36 and value[8] == value[13] == value[18] == value[23] == "-"
                and value[14].lower() == "7"
                and all(c in "0123456789abcdefABCDEF" for i, c in enumerate(value)
                        if i not in (8, 13, 18, 23)))

    return valid(candidate) and valid(earlier) and candidate > earlier


def _codex_text(payload: dict) -> str:
    return "\n".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if b.get("type") in ("input_text", "output_text", "text") and b.get("text")
    )

def _pi_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str) and block["text"]
    )


def _pi_ts_ms(value) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return int(value * 1000 if abs(value) < 100_000_000_000 else value)
        except (OverflowError, ValueError):
            return 0
    return _ts_ms(value if isinstance(value, str) else None)


def _header_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(key):
            return line[len(key):].strip()
    return ""


def _codex_delegated_input(text: str) -> str:
    stripped = text.lstrip()
    if not (stripped.startswith("<codex_delegation")
            or stripped.startswith("<realtime_delegation")):
        return ""
    _before, marker, rest = stripped.partition("<input>")
    if not marker:
        return ""
    body, marker, _after = rest.partition("</input>")
    if not marker:
        return ""
    # Delegation wrappers are XML-escaped. Match Rust's closed XML entity set, not
    # Python's broader HTML named-entity table, so live and indexed text agree.
    body = body.strip()
    return (body.replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


# a cursor tool bubble's toolFormerData.status; anything here is a settled call
_CUR_TERMINAL = ("completed", "error", "errored", "failed", "cancelled", "canceled",
                 "rejected", "aborted")
_CUR_SEED_ROWS = 40          # first sight: replay at most this many trailing bubble rows
_CUR_DONE_RECHECK_S = 3.0    # generatingBubbleIds recheck cadence for active cursor sessions
_VANISH_SWEEP_S = 20.0       # cadence for noticing tailed store files that disappeared


def _cursor_db_paths() -> list[str]:
    """Native Cursor store first, with copied cross-OS profiles as fallback."""
    return cursor_db_paths(HOME)


def _cur_loads(s):
    """cursor stores tool args as a JSON string; decode when it is one, else pass through."""
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


class _SessionMap(dict):
    """Dictionary whose structural views are snapshots.

    The HTTP status path historically iterated ``watcher.sessions`` directly while the
    watcher added sessions, raising ``RuntimeError: dictionary changed size``. Internal state
    updates use the same RLock; snapshot-valued views also keep legacy capture/status callers
    safe without requiring them to know the watcher's locking protocol.
    """
    def __init__(self, lock):
        super().__init__()
        self._lock = lock

    def setdefault(self, key, default=None):
        with self._lock:
            return super().setdefault(key, default)

    def get(self, key, default=None):
        with self._lock:
            return super().get(key, default)

    def values(self):
        with self._lock:
            return tuple(super().values())

    def items(self):
        with self._lock:
            return tuple(super().items())

    def __len__(self):
        with self._lock:
            return super().__len__()

    def __delitem__(self, key):
        with self._lock:
            return super().__delitem__(key)


class LiveWatcher(threading.Thread):
    def __init__(self, *, headless_indexd: bool = False):
        super().__init__(daemon=True, name="agrep-live")
        self._offsets: dict[str, int] = {}        # jsonl path -> consumed byte offset
        self._ctimes: dict[str, float] = {}       # jsonl path -> creation time at first sight
        self._partials: dict[str, bytes] = {}     # jsonl path -> trailing partial bytes
        self._codex_meta: dict[str, tuple[str, str]] = {}   # path -> (session, project)
        self._codex_model: dict[str, str] = {}              # path -> model (from turn_context)
        self._codex_cwd: dict[str, str] = {}                # path -> cwd (for relative image paths)
        self._codex_sub: dict[str, dict] = {}                # path -> fork boundary/state
        self._codex_ignored: set[str] = set()                # internal guardian control rollouts
        self._codex_old_scan = 0.0                           # next old-shard discovery scan
        self._codex_old_active: set[str] = set()             # old paths currently writing
        self._claude_projects: dict[str, dict[str, int]] = {}  # session -> project vote counts
        self._claude_pending: dict[tuple[str, str], dict] = {}  # (session, call_id) -> event
        self._codex_pending: dict[tuple[str, str], dict] = {}
        self._pi_meta: dict[str, dict] = {}                  # path -> stable session metadata
        self._pi_pending: dict[tuple[str, str], dict] = {}   # (session, call_id) -> event
        self._agy_announced: dict[str, deque] = {}          # session -> queued (name, input)
        self._agy_mail_seen: set[str] = set()
        self._oc_watermark: dict[str, tuple[int, str]] = {}  # db path -> (updated, part id)
        self._oc_msg_wm: dict[str, tuple[int, str]] = {}     # db path -> (updated, message id)
        self._oc_done: dict[str, int] = {}                  # session -> last done ts emitted
        self._oc_part_st: dict[str, str] = {}               # part id -> last seen status/kind
        self._oc_text_messages: dict[str, str] = {}         # message id -> last emitted text
        self._oc_mutable_parts: dict[str, set[str]] = {}    # db path -> in-place part ids
        self._oc_unfinished_messages: dict[str, set[str]] = {}  # db path -> message ids
        self._oc_session_meta: dict[str, tuple[str, str]] = {}  # session -> (dir, title)
        self._oc_schema: dict[str, str] = {}                  # db path -> v1/v2
        # cursor (the editor's built-in agent): state.vscdb `cursorDiskKV`, tailed by
        # bubble rowid the same way opencode is polled by part.time_updated. See _tick_cursor.
        self._cur_rowid: dict[str, int] = {}       # db path -> consumed bubbleId rowid watermark
        self._cur_bubbles: dict[str, int] = {}     # db path -> last observed bubble row count
        self._cur_meta: dict[str, tuple] = {}       # composerId -> (project, title, model)
        self._cur_pending: dict[str, tuple] = {}    # bubble key -> (cid, name, ts) awaiting terminal status
        self._cur_text: dict[str, str] = {}         # bubble key -> last emitted assistant text
        self._cur_text_pending: set[str] = set()    # assistant bubbles that can mutate in place
        self._cur_active: dict[str, float] = {}     # composerId -> last-traffic wall time
        self._cur_done: dict[str, int] = {}         # composerId -> last done ts emitted
        self._cur_done_check = 0.0                  # next generatingBubbleIds recheck wall time
        self._parent_miss: set[str] = set()        # parent ids with no locatable file
        self._state_lock = threading.RLock()
        self.sessions: dict[str, dict] = _SessionMap(self._state_lock)
        self.ring: deque = deque(maxlen=RING)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._booted = False
        self._boot_complete = threading.Event()
        self._boot_stale_before_ms = (
            time.time() - _SNAPSHOT_EXPIRE_S) * 1000
        self._last_event_wall = 0.0  # wall time of the last emitted event (adaptive poll)
        self._n_emitted = 0  # post-boot emissions (delivered to subscribers)
        self._global_errors: dict[str, str] = {}
        self._degraded_sources: dict[tuple[str, str], str] = {}
        self._writer_identity: dict[str, tuple[int, str, str]] = {}
        self._process_scan_due = 0.0
        self._tick_ms: dict[str, int] = {}
        self._n_loops = 0
        self._vanish_sweep_due = 0.0
        self._headless_indexd = headless_indexd
        self._poll_s = POLL_S
        self._work_ms = 0
        self._loop_ms = 0
        self._work_total_ms = 0

    # ------------------------------------------------------------- subscriptions

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def wait_boot(self, timeout: float | None = None) -> bool:
        return self._boot_complete.wait(timeout)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, ev: dict) -> None:
        with self._state_lock:
            self._emit_unlocked(ev)

    def _emit_unlocked(self, ev: dict) -> None:
        ts = ev.get("ts")
        if (self._headless_indexd and not self._booted
                and isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and ts > 0 and ts < self._boot_stale_before_ms):
            # A touched history copy must not become resident daemon state.
            agent = ev.get("agent")
            pending = (self._claude_pending if agent == "claude" else
                       self._codex_pending if agent == "codex" else
                       self._pi_pending if agent == "pi" else None)
            if pending is not None and ev.get("type") == "tool":
                pending.pop((ev.get("session"), ev.get("call_id")), None)
            return
        # keep live provenance aligned with the cold classifier: a side session's
        # human-shaped turns index as who="subagent", so the board says the same
        if ev.get("sub_session") and ev.get("type") == "user":
            ev.setdefault("who", "subagent")
        key = f"{ev['agent']}:{ev['session']}"
        s = self.sessions.setdefault(key, {
            "agent": ev["agent"], "session": ev["session"], "project": "",
            "title": "", "last_text": "", "model": "", "last_ts": 0,
            "state": "", "turn_n": 0,
            "working": False, "pending": {}, "recent": deque(maxlen=PER_SESSION),
        })
        # turn number rides every event so the feed can group a turn's work under
        # the prompt that caused it (a user message opens the next turn)
        if ev["type"] == "user":
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
        if ev.get("parent"):
            s["parent"] = ev["parent"]
        # a subagent's first user message IS its task -> use it as the row label
        if ev.get("sub_session") and not s.get("title") and ev.get("type") == "user":
            tt = (ev.get("text") or "").strip()
            if tt:
                s["title"] = tt[:110]
        s["last_ts"] = max(s["last_ts"], ev.get("ts", 0))
        if ev.get("sub_session"):
            s["sub"] = True
        t = ev["type"]
        if t in ("user", "reply") and str(ev.get("text") or "").strip():
            s["last_text"] = " ".join(str(ev["text"]).split())[:1000]
        # inline Claude sidechain duplicates never reach here, so child sessions
        # drive their own state exactly like roots
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
        # state reflects what is ACTUALLY outstanding, not just the last event: "thinking"
        # only when no tool is still running (parallel calls would otherwise mislabel it).
        # state_ts = when this state began, so the UI can tick a live elapsed counter.
        if t in ("tool", "tool_done"):
            if pend:
                nm, t0 = next(reversed(pend.values()))
                extra = f" +{len(pend) - 1}" if len(pend) > 1 else ""
                s["state"] = f"⚙ {nm}{extra}"
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
        # Sticky "working" = mid-turn: the STABLE busy signal, unlike `active` (write-recency)
        # which flaps idle across in-turn quiet gaps; cleared only by `done` or the _decay stall.
        if t in ("tool", "tool_done", "user", "reply"):
            s["working"] = True
        elif t == "done":
            s["working"] = False
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
        ev["sworking"] = s["working"]  # the stable mid-turn flag rides alongside it
        if s.get("state_ts"):
            ev["sts"] = s["state_ts"]
        if ev.get("model") or s["model"]:
            ev["smodel"] = s["model"]
        # Expiring replayed history is state cleanup, not new source activity.
        if not (t == "done" and ev.get("why") == "expired"):
            self._last_event_wall = time.time()
        if t in ("done", "queued"):
            if t == "done":
                self._writer_identity.pop(key, None)
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
        with self._state_lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict:
        now = time.time() * 1000
        if self._booted:
            self._expire_working(now)
            self._prune_stale_sessions(now)

        def row(s: dict) -> dict:
            # system/heartbeat store writes (live_ts) keep a working session "live"
            # between conversational events
            eff = max(s["last_ts"], s.get("live_ts", 0))
            return {
                "agent": s["agent"], "session": s["session"], "project": s["project"],
                "title": s["title"], "last_text": s.get("last_text", ""),
                "model": s.get("model", ""),
                "last_ts": int(eff), "state": s["state"],
                "working": s.get("working", False),
                "parent": s.get("parent"),
                "state_ts": s.get("state_ts", 0),
                "queued": s.get("queued", 0), "queued_text": s.get("queued_text", ""),
                "sub": s.get("sub", False),
                "active": (now - eff) <= ACTIVE_WINDOW_S * 1000,
                "recent": list(s["recent"])[-60:],
            }

        active = []
        for s in self.sessions.values():
            eff = max(s["last_ts"], s.get("live_ts", 0))
            if now - eff > _SNAPSHOT_EXPIRE_S * 1000:
                continue  # long-idle sessions drop from the snapshot entirely
            active.append(row(s))
        # a parent orchestrating subagents can go store-quiet past the window while
        # its children work; dropping it leaves headless sub rows, so pull it back
        kept = {(a["agent"], a["session"]) for a in active}
        for a in list(active):
            agent = a["agent"]
            pid = a.get("parent")
            while pid and (agent, pid) not in kept:
                parent = self.sessions.get(f"{agent}:{pid}")
                if parent is None:
                    break
                active.append(row(parent))
                kept.add((agent, pid))
                pid = parent.get("parent")
        active.sort(key=lambda s: -s["last_ts"])
        if self._global_errors:
            last_err = next(reversed(self._global_errors.values()))
        elif self._degraded_sources:
            (agent, path), error = next(iter(sorted(
                self._degraded_sources.items())))
            last_err = f"{agent} source unreadable: {path}: {error}"
        else:
            last_err = ""
        return {"now": int(now), "window_s": ACTIVE_WINDOW_S, "sessions": active,
                "booting": not self._booted,
                "n_subs": len(self._subs), "n_emitted": self._n_emitted,
                "n_tracked": len(self._offsets), "last_err": last_err,
                "degraded_sources": [
                    {"agent": agent, "path": path, "error": error}
                    for (agent, path), error in sorted(self._degraded_sources.items())
                ],
                "tick_ms": dict(self._tick_ms), "n_loops": self._n_loops,
                "watch_mode": "indexd" if self._headless_indexd else "foreground",
                "poll_s": self._poll_s, "work_ms": self._work_ms,
                "loop_ms": self._loop_ms, "work_total_ms": self._work_total_ms}

    def _refresh_source_health(
            self, agent: str, roots: list[str]) -> dict[str, os.stat_result]:
        problems = {}
        readable = {}
        for path in roots:
            try:
                readable[path] = _probe_source(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                problems[(agent, path)] = str(error)
        with self._state_lock:
            for key in tuple(self._degraded_sources):
                if key[0] == agent:
                    del self._degraded_sources[key]
            self._degraded_sources.update(problems)
        return readable

    def _record_source_error(self, agent: str, path: str, error: OSError) -> None:
        with self._state_lock:
            self._degraded_sources[(agent, path)] = str(error)

    def _source_entries(self, agent: str, path: str):
        try:
            return list(os.scandir(path))
        except FileNotFoundError:
            return None
        except OSError as error:
            self._record_source_error(agent, path, error)
            return None

    def _walk_source_error(self, agent: str):
        def record(error: OSError) -> None:
            self._record_source_error(
                agent, str(getattr(error, "filename", "") or "source tree"), error)
        return record

    @staticmethod
    def _process_identity(process: dict) -> tuple[int, str, str] | None:
        pid = int(process.get("pid") or 0)
        start = str(process.get("process_start") or "")
        if pid > 0 and not start:
            start = proc.process_start_identity(pid) or ""
        if not start:
            created = float(process.get("create_ts") or 0.0)
            start = f"time:{created:.6f}" if created > 0 else ""
        if pid <= 0 or not start:
            return None
        return (pid, start, str(process.get("name") or "").lower())

    @staticmethod
    def _writer_identity_live(identity: tuple[int, str, str]) -> bool | None:
        pid, expected_start, _name = identity
        if not proc.pid_alive(pid):
            return False
        actual_start = proc.process_start_identity(pid)
        if actual_start is None or expected_start.startswith("time:"):
            return None
        return actual_start == expected_start

    def _refresh_writer_liveness(self, now: float) -> None:
        if now < self._process_scan_due:
            return
        interval = 10.0 if self._headless_indexd else _PROCESS_SCAN_S
        self._process_scan_due = now + interval
        processes, error = procscan.scan_agents_checked()
        if error:
            with self._state_lock:
                self._global_errors["procscan"] = (
                    f"procscan unavailable: {error}")
            return
        live = {identity: process for process in processes
                if (identity := self._process_identity(process)) is not None}
        with self._state_lock:
            self._global_errors.pop("procscan", None)
            sessions = tuple(self.sessions.items())
            for process in processes:
                session = process.get("session")
                if not session:
                    continue
                key = f"{process.get('agent')}:{session}"
                identity = self._process_identity(process)
                if self.sessions.get(key) is not None and identity is not None:
                    self._writer_identity[key] = identity
            for key, session in sessions:
                pid = session.get("pty_pid")
                if pid and key not in self._writer_identity:
                    match = next((process for process in processes
                                  if process.get("pid") == pid
                                  and process.get("agent") == session.get("agent")), None)
                    identity = self._process_identity(match or {})
                    if identity is not None:
                        self._writer_identity[key] = identity

            for key, identity in tuple(self._writer_identity.items()):
                session = self.sessions.get(key)
                process = live.get(identity)
                if session is None or not session.get("working"):
                    self._writer_identity.pop(key, None)
                elif process is not None and process.get("agent") == session.get("agent"):
                    session["live_ts"] = now * 1000
                else:
                    if self._writer_identity_live(identity) is not False:
                        session["live_ts"] = now * 1000
                        continue
                    self._writer_identity.pop(key, None)
                    self._emit_unlocked({
                        "agent": session["agent"], "session": session["session"],
                        "ts": int(now * 1000), "type": "done",
                        "why": "interrupted",
                    })

    def _expire_working(self, nowms: float) -> None:
        expired = []
        for session in self.sessions.values():
            effective = max(session.get("last_ts", 0), session.get("live_ts", 0))
            if (session.get("working") and effective
                    and nowms - effective > _SNAPSHOT_EXPIRE_S * 1000):
                expired.append((session, int(effective)))
        for session, effective in expired:
            self._emit_unlocked({
                "agent": session["agent"], "session": session["session"],
                "ts": effective + 1, "type": "done", "why": "expired",
            })

    def _prune_stale_sessions(self, nowms: float) -> None:
        """Release expired feed bodies without forgetting source byte offsets."""
        rows = dict(self.sessions.items())
        keep = {
            key for key, session in rows.items()
            if (nowms - max(session.get("last_ts", 0),
                            session.get("live_ts", 0))
                <= _SNAPSHOT_EXPIRE_S * 1000)
            or key in self._writer_identity
        }
        pending = list(keep)
        while pending:
            key = pending.pop()
            session = rows.get(key)
            if session is None:
                continue
            parent = session.get("parent")
            parent_key = f"{session['agent']}:{parent}" if parent else ""
            if parent_key and parent_key in rows and parent_key not in keep:
                keep.add(parent_key)
                pending.append(parent_key)
        stale = set(rows) - keep
        if not stale:
            return
        stale_pairs = {
            (rows[key]["agent"], rows[key]["session"]) for key in stale
        }
        for key in stale:
            del self.sessions[key]
            self._writer_identity.pop(key, None)
        retained_events = [
            event for event in self.ring
            if (event.get("agent"), event.get("session")) not in stale_pairs
        ]
        self.ring.clear()
        self.ring.extend(retained_events)
        for agent, mapping in (
                ("claude", self._claude_pending),
                ("codex", self._codex_pending),
                ("pi", self._pi_pending)):
            for key in tuple(mapping):
                if (agent, key[0]) in stale_pairs:
                    mapping.pop(key, None)

    # ------------------------------------------------------------- jsonl tailing

    def _dispatch_delta(self, path: str, size: int, ctime: float,
                        handler, *, agent: str = "agent-history") -> int:
        """Dispatch complete lines and advance each byte offset only after its handler."""
        off = self._offsets.get(path)
        first = off is None
        # Rotation/recreation: same name but the file shrank or its _fid identity moved;
        # re-seed from the tail. Known hole: recreated LARGER under the same name within
        # ~15s is invisible (NTFS creation-time tunneling); real stores are uuid-named.
        if not first and (size < off or (ctime and self._ctimes.get(path) not in (None, ctime))):
            first = True
            self._partials[path] = b""
            # shrink/recreation is deletion-shaped activity even when zero lines dispatch
            self._mark_store_mutation(time.time())
        if first:
            off = max(0, size - TAIL_SEED)
            if ctime:
                self._ctimes[path] = ctime
        if size <= off:
            self._offsets[path] = size  # keep current (handles the rotation-to-smaller case)
            return 0
        try:
            with open(path, "rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
        except OSError as error:
            self._record_source_error(agent, path, error)
            return 0
        if first and off > 0:
            nl = chunk.find(b"\n")
            if nl < 0:
                self._offsets[path] = size
                self._partials[path] = b""
                return 0
            off += nl + 1
            chunk = chunk[nl + 1:]
            self._offsets[path] = off
        prefix = self._partials.get(path, b"")
        base = off - len(prefix)
        buf = prefix + chunk
        start = 0
        dispatched = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            raw = buf[start:nl]
            if raw.strip():
                handler(raw.decode("utf-8", "replace"))
                dispatched += 1
            self._offsets[path] = base + nl + 1
            self._partials[path] = b""
            start = nl + 1
        self._offsets[path] = size
        self._partials[path] = buf[start:]
        return dispatched

    def _read_delta(self, path: str, size: int, ctime: float = 0.0) -> list[str]:
        """Return complete delta lines while preserving post-dispatch offset semantics."""
        lines = []
        self._dispatch_delta(path, size, ctime, lines.append)
        return lines

    # ------------------------------------------------------------- claude

    def _claude_project(self, session: str, candidate: str) -> str:
        votes = self._claude_projects.setdefault(session, {})
        if candidate:
            votes[candidate] = votes.get(candidate, 0) + 1
        return sorted(votes, key=lambda project: (-votes[project], project))[0] if votes else ""

    def _tick_claude(self, now: float) -> None:
        root = store_root("claude", HOME)
        if not self._refresh_source_health("claude", [root]) or not _plain_dir(root):
            return
        root_entries = self._source_entries("claude", root)
        if root_entries is None:
            return
        proj_dirs = []
        for entry in root_entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    proj_dirs.append(entry.path)
            except OSError as error:
                self._record_source_error("claude", entry.path, error)
        files: list[str] = []
        for d in proj_dirs:
            entries = self._source_entries("claude", d)
            if entries is None:
                continue
            for e in entries:
                try:
                    if (store_content("claude", e.path)
                            and e.is_file(follow_symlinks=False)):
                        files.append(e.path)
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError as error:
                    self._record_source_error("claude", e.path, error)
                    continue
                subagents = os.path.join(e.path, "subagents")
                if not _plain_dir(subagents):
                    continue
                children = self._source_entries("claude", subagents)
                if children is None:
                    continue
                for child in children:
                    try:
                        if (store_content("claude", child.path)
                                and child.is_file(follow_symlinks=False)):
                            files.append(child.path)
                    except OSError as error:
                        self._record_source_error("claude", child.path, error)
        for path in files:
            st = _plain_file_stat(path)
            if st is None:
                continue
            name = os.path.basename(path)[:-6]
            known = path in self._offsets
            if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                continue
            def dispatch(ln):
                nested = "/subagents/" in path.replace("\\", "/")
                self._claude_line(name, ln, nested=nested)
            if self._dispatch_delta(
                    path, st.st_size, _fid(st), dispatch, agent="claude"):
                # file growth = alive, even if these lines emitted no event
                self._mark_live(f"claude:{name}", now)

    def _claude_line(self, file_session: str, ln: str, *, nested: bool = False) -> None:
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            return
        if not isinstance(o, dict):
            return
        # Claude duplicates child traffic inline in the parent's JSONL; the authoritative copy
        # lives under subagents/. Without path context, isSidechain would self-parent the file stem.
        if o.get("isSidechain") and not nested:
            return
        sess = o.get("sessionId") or file_session
        if not isinstance(sess, str):
            return
        is_sub = nested
        parent_sess = ""
        if is_sub:
            # Claude writes the SPAWNER's sessionId into subagent lines: inner id = parent,
            # file stem = the subagent's own id. Key by file id for a first-class session.
            parent_sess = o.get("sessionId") or ""
            parent_sess = parent_sess if isinstance(parent_sess, str) else ""
            sess = file_session
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
        cwd = cwd if isinstance(cwd, str) else ""
        if cwd:
            proj = self._claude_project(sess, _project_root(cwd) or _project_of(cwd))
        msg = o.get("message") or {}
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        base = {"agent": "claude", "session": sess, "ts": ts, "project": proj}
        if is_sub:
            # marks the session as a subagent (for grouping) WITHOUT suppressing its own state
            base["sub_session"] = True
            if parent_sess:
                base["parent"] = parent_sess
        model = msg.get("model") or ""
        model = model if isinstance(model, str) else ""
        if model and not (model.startswith("<") and model.endswith(">")):
            base["model"] = model
        if o.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
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
                                    base = {**base, "project": self._claude_project(sess, cand)}
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
                        self._claude_pending[(sess, ev["call_id"])] = ev
                    self._emit(ev)
                elif (bt == "text" and isinstance(b.get("text"), str)
                      and b["text"].strip()):
                    self._emit({**base, "type": "reply", "text": _snip(b["text"], MSG)})
            # The final assistant message of a turn carries a terminal stop_reason
            # (tool_use = mid-turn). This is what flips the card off "replying".
            if msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens"):
                self._emit({**base, "type": "done"})
        elif o.get("type") == "user":
            human = (not o.get("isMeta")
                     and o.get("userType") in (None, "", "external"))
            if isinstance(content, list):
                texts = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        text = b.get("text")
                        if isinstance(text, str):
                            texts.append(text)
                        continue
                    if b.get("type") != "tool_result":
                        continue
                    cid = b.get("tool_use_id", "")
                    started = self._claude_pending.pop((sess, cid), None)
                    c = b.get("content")
                    out = c if isinstance(c, str) else "\n".join(
                        x.get("text", "") for x in c
                        if isinstance(x, dict) and x.get("type") == "text"
                        and isinstance(x.get("text"), str)) if isinstance(c, list) else ""
                    done = {**base, "type": "tool_done", "call_id": cid,
                            "name": (started or {}).get("name", ""),
                            "ok": not bool(b.get("is_error")),
                            "output": _snip(out)}
                    if started and started.get("ts"):
                        done["dur"] = max(0, ts - started["ts"])
                    self._emit(done)
                # typed messages with attachments arrive as text blocks, not a plain string
                joined = "\n".join(t for t in texts if t.strip())
                if human and joined and not _noise(joined):
                    self._emit({**base, "type": "user", "text": _snip(joined, MSG)})
            elif isinstance(content, str):
                if not human:
                    return
                if content.lstrip().startswith("[Request interrupted"):
                    # the turn is dead right now -- don't leave the card on "⚙ tool"
                    self._emit({**base, "type": "done", "why": "interrupted"})
                elif not _noise(content):
                    self._emit({**base, "type": "user", "text": _snip(content, MSG)})

    # ------------------------------------------------------------- pi / oh-my-pi

    @staticmethod
    def _pi_head(path: str, root: str) -> dict | None:
        """Read identity from the small header even when the live seed starts at EOF."""
        title = ""
        header = None
        try:
            with open(path, "rb") as stream:
                for _ in range(8):
                    raw = stream.readline(TAIL_SEED)
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        return None
                    try:
                        row = json.loads(raw.decode("utf-8", "replace"))
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    if row.get("type") in ("title", "title_change"):
                        named = row.get("title")
                        if isinstance(named, str) and named.strip():
                            title = named
                    if row.get("type") == "session":
                        header = row
                        break
        except OSError:
            return None
        if header is None:
            return None

        stem = os.path.basename(path)[:-6]
        fallback = stem.rsplit("_", 1)[-1] or stem
        session = header.get("id")
        session = session if isinstance(session, str) and session else fallback
        cwd = header.get("cwd")
        cwd = cwd if isinstance(cwd, str) else ""
        try:
            parts = Path(os.path.relpath(path, root)).parts
        except ValueError:
            return None
        slug = parts[0] if parts else ""
        project = (_project_of(cwd) if cwd else
                   (slug.strip("-").replace("-", "/") or "pi"))
        parent = ""
        if len(parts) >= 3:
            parent = parts[1].rsplit("_", 1)[-1]
        return {
            "session": session, "cwd": cwd, "project": project,
            "title": title, "model": "", "parent": parent,
        }

    def _tick_pi(self, now: float) -> None:
        roots = store_roots("pi", HOME)
        healthy = self._refresh_source_health("pi", roots)
        candidates: dict[str, tuple[os.stat_result, str]] = {}
        for root in roots:
            if root not in healthy or not _plain_dir(root):
                continue
            pending_dirs = [root]
            while pending_dirs:
                directory = pending_dirs.pop()
                entries = self._source_entries("pi", directory)
                if entries is None:
                    continue
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending_dirs.append(entry.path)
                            continue
                        if (not store_content("pi", entry.path)
                                or not entry.is_file(follow_symlinks=False)):
                            continue
                        found = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        self._record_source_error("pi", entry.path, error)
                        continue
                    known = entry.path in self._offsets
                    if now - found.st_mtime <= ACTIVE_WINDOW_S or known:
                        candidates[entry.path] = (found, root)

        for path, (found, root) in candidates.items():
            identity = _fid(found)
            rotated = (path in self._offsets
                       and self._ctimes.get(path) not in (None, identity))
            if path not in self._pi_meta or rotated:
                meta = self._pi_head(path, root)
                if meta is None:
                    continue
                self._pi_meta[path] = meta
            count = self._dispatch_delta(
                path, found.st_size, identity,
                lambda line, selected=path: self._pi_line(selected, line),
                agent="pi")
            if count:
                self._mark_live(f"pi:{self._pi_meta[path]['session']}", now)

    def _pi_line(self, path: str, ln: str) -> None:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            return
        if not isinstance(row, dict):
            return
        meta = self._pi_meta.get(path)
        if meta is None:
            return
        record_type = row.get("type")
        if record_type in ("title", "title_change"):
            title = row.get("title")
            if isinstance(title, str) and title.strip():
                meta["title"] = title
                with self._state_lock:
                    session = self.sessions.get(f"pi:{meta['session']}")
                    if session is not None:
                        session["title"] = title
            return
        if record_type == "model_change":
            model = row.get("modelId") or row.get("model")
            if isinstance(model, str) and model:
                meta["model"] = model
                with self._state_lock:
                    session = self.sessions.get(f"pi:{meta['session']}")
                    if session is not None:
                        session["model"] = model
            return
        if record_type == "session":
            return

        ts = _pi_ts_ms(row.get("timestamp"))
        base = {
            "agent": "pi", "session": meta["session"], "ts": ts,
            "project": meta["project"],
        }
        if meta["title"]:
            base["title"] = meta["title"]
        if meta["model"]:
            base["model"] = meta["model"]
        if meta["parent"]:
            base.update({"sub_session": True, "parent": meta["parent"]})

        if record_type == "custom" and row.get("customType") == "session_exit":
            with self._state_lock:
                session = self.sessions.get(f"pi:{meta['session']}")
                working = bool(session and session.get("working"))
            if working:
                data = row.get("data")
                data = data if isinstance(data, dict) else {}
                event = {**base, "type": "done"}
                if data.get("kind") == "signal":
                    event["why"] = "interrupted"
                self._emit(event)
            return
        if record_type != "message":
            return
        message = row.get("message")
        if not isinstance(message, dict):
            return
        ts = (_pi_ts_ms(message.get("timestamp"))
              or _pi_ts_ms(row.get("timestamp")))
        base["ts"] = ts
        role = message.get("role")
        content = message.get("content")

        if role == "assistant":
            model = message.get("model")
            if isinstance(model, str) and model:
                meta["model"] = model
                base["model"] = model
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str) and content.strip():
                self._emit({**base, "type": "reply", "text": _snip(content, MSG)})
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if (block_type == "text" and isinstance(block.get("text"), str)
                        and block["text"].strip()):
                    self._emit({
                        **base, "type": "reply", "text": _snip(block["text"], MSG),
                    })
                    continue
                if block_type != "toolCall":
                    continue
                arguments = block.get("arguments")
                call_id = block.get("id")
                call_id = call_id if isinstance(call_id, str) else ""
                event = {
                    **base, "type": "tool", "name": block.get("name", "?"),
                    "input": _summarize_input(arguments), "call_id": call_id,
                }
                images = _image_files(arguments)
                if images:
                    event["imgs"] = _img_records(images, meta["cwd"])
                    caption = (arguments.get("caption")
                               if isinstance(arguments, dict) else None)
                    if isinstance(caption, str) and caption.strip():
                        event["imgcap"] = _snip(caption, 200)
                if call_id:
                    self._pi_pending[(meta["session"], call_id)] = event
                self._emit(event)
            stop = message.get("stopReason")
            if isinstance(stop, str) and stop and stop not in ("toolUse", "tool_use"):
                event = {**base, "type": "done"}
                if stop in ("aborted", "error"):
                    event["why"] = "interrupted"
                self._emit(event)
            return

        if role == "user":
            text = _pi_text(content)
            if text.strip() and not _noise(text):
                self._emit({**base, "type": "user", "text": _snip(text, MSG)})
            return

        if role == "toolResult":
            call_id = message.get("toolCallId")
            call_id = call_id if isinstance(call_id, str) else ""
            started = self._pi_pending.pop((meta["session"], call_id), None)
            name = message.get("toolName") or (started or {}).get("name", "")
            event = {
                **base, "type": "tool_done", "call_id": call_id,
                "name": name, "output": _snip(_pi_text(content)),
            }
            is_error = message.get("isError")
            if isinstance(is_error, bool):
                event["ok"] = not is_error
            if started and started.get("ts"):
                event["dur"] = max(0, ts - started["ts"])
            self._emit(event)
            return

        if role == "bashExecution":
            command = message.get("command")
            command = command if isinstance(command, str) else ""
            call_id = row.get("id")
            call_id = call_id if isinstance(call_id, str) else ""
            self._emit({
                **base, "type": "tool", "name": "bash",
                "input": _snip(command), "call_id": call_id,
            })
            output = message.get("output")
            output = output if isinstance(output, str) else ""
            event = {
                **base, "type": "tool_done", "name": "bash",
                "call_id": call_id, "output": _snip(output),
            }
            exit_code = message.get("exitCode")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                event["ok"] = (exit_code == 0 and not bool(message.get("cancelled")))
            self._emit(event)

    # ------------------------------------------------------------- codex

    def _tick_codex(self, now: float) -> None:
        root = store_root("codex", HOME)
        if not self._refresh_source_health("codex", [root]):
            return
        days = {time.strftime("%Y/%m/%d", time.localtime(now - dt)) for dt in (0, 86400)}
        candidates = {}
        for day in days:
            d = os.path.join(root, *day.split("/"))
            entries = self._source_entries("codex", d)
            if entries is None:
                continue
            for e in entries:
                if not store_content("codex", e.path):
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError as error:
                    self._record_source_error("codex", e.path, error)
                    continue
                known = e.path in self._offsets
                if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                    continue
                candidates[e.path] = st

        for path in tuple(self._codex_old_active):
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                self._codex_old_active.discard(path)
                continue
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime <= ACTIVE_WINDOW_S:
                candidates[path] = st
            else:
                self._codex_old_active.discard(path)
        if now >= self._codex_old_scan:
            self._codex_old_scan = now + 15.0
            for directory, dirs, names in os.walk(
                    root, followlinks=False,
                    onerror=self._walk_source_error("codex")):
                dirs[:] = [name for name in dirs
                           if not os.path.islink(os.path.join(directory, name))]
                for name in names:
                    if not store_content("codex", name):
                        continue
                    path = os.path.join(directory, name)
                    if path in candidates:
                        continue
                    try:
                        st = os.stat(path, follow_symlinks=False)
                    except OSError as error:
                        self._record_source_error("codex", path, error)
                        continue
                    if stat.S_ISREG(st.st_mode) and now - st.st_mtime <= ACTIVE_WINDOW_S:
                        candidates[path] = st
                        self._codex_old_active.add(path)

        for path, st in candidates.items():
            if path in self._codex_ignored:
                identity = _fid(st)
                old_size = self._offsets.get(path, 0)
                old_identity = self._ctimes.get(path)
                if st.st_size >= old_size and old_identity in (None, identity):
                    self._offsets[path] = st.st_size
                    self._ctimes[path] = identity
                    continue
                # Same-path replacement/rotation: forget the guardian verdict
                # and classify the new first line from scratch.
                self._codex_ignored.discard(path)
                self._offsets.pop(path, None)
                self._ctimes.pop(path, None)
                self._partials.pop(path, None)
                self._codex_meta.pop(path, None)
                self._codex_sub.pop(path, None)
                self._codex_model.pop(path, None)
                self._codex_cwd.pop(path, None)
            if path not in self._codex_meta:
                head = self._codex_head(path)
                if head is None:
                    continue  # first metadata line is still being written; retry next tick
                sess, proj, sub = head
                if sub is not None and sub.get("internal_guardian"):
                    self._codex_ignored.add(path)
                    self._offsets[path] = st.st_size
                    self._ctimes[path] = _fid(st)
                    continue
                self._codex_meta[path] = (sess, proj)
                if sub is not None:
                    self._codex_sub[path] = sub
                    boundary_end = self._codex_seed_subagent(path, st.st_size)
                    if boundary_end is not None:
                        self._offsets[path] = self._codex_tail_offset(
                            path, st.st_size, boundary_end)
                        self._ctimes[path] = _fid(st)
            count = self._dispatch_delta(
                path, st.st_size, _fid(st), lambda ln: self._codex_line(path, ln),
                agent="codex")
            if count:  # file growth = alive, even if these lines emitted no event
                self._mark_live(f"codex:{self._codex_meta[path][0]}", now)

    @staticmethod
    def _codex_head(path: str) -> tuple[str, str, dict | None] | None:
        """Identity and fork metadata from a complete first session_meta line."""
        sess = os.path.basename(path)[:-6]
        parts = sess.split("-")
        if len(parts) >= 5:
            sess = "-".join(parts[-5:])
        try:
            with open(path, "rb") as f:
                raw = f.readline()
            if not raw.endswith(b"\n"):
                return None
            o = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(o, dict):
                return None
            if o.get("type") != "session_meta":
                return None
            p = o.get("payload") or {}
            if not isinstance(p, dict):
                return None
            sess = p.get("id") or sess
            cwd = p.get("cwd") or ""
            proj = _project_of(cwd) if cwd else ""
            sub = None
            if p.get("thread_source") == "subagent":
                source = p.get("source")
                source = source if isinstance(source, dict) else {}
                sub_source = source.get("subagent") or {}
                sub_source = sub_source if isinstance(sub_source, dict) else {}
                if sub_source.get("other") == "guardian":
                    sub = {"internal_guardian": True}
                else:
                    spawn = sub_source.get("thread_spawn") or {}
                    sub = {
                        "agent_path": p.get("agent_path") or spawn.get("agent_path") or "",
                        "parent": p.get("parent_thread_id") or spawn.get("parent_thread_id") or "",
                        "started": False,
                        "start_turn_id": "",
                        "replayed_parent": False,
                        "seen": set(),
                    }
            return sess, proj, sub
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None

    def _codex_seed_subagent(self, path: str, size: int) -> int | None:
        """Scan only fork-boundary records, returning the byte after the first task anchor."""
        state = self._codex_sub[path]
        boundary_end = None
        try:
            with open(path, "rb") as f:
                while f.tell() < size:
                    raw = f.readline(size - f.tell())
                    end = f.tell()
                    if not raw.endswith(b"\n"):
                        break
                    if not any(needle in raw for needle in
                               (b'"session_meta"', b'"task_started"', b'"turn_context"',
                                b'"agent_message"', b'"role":"user"')):
                        continue
                    before = state["started"]
                    self._codex_line(path, raw.decode("utf-8", "replace").rstrip("\r\n"))
                    if not before and state["started"] and boundary_end is None:
                        boundary_end = end
        except OSError:
            return None
        return boundary_end

    @staticmethod
    def _codex_tail_offset(path: str, size: int, boundary_end: int) -> int:
        """Line-aligned tail offset that can never move before the child boundary."""
        target = max(boundary_end, size - TAIL_SEED)
        if target <= boundary_end:
            return boundary_end
        try:
            with open(path, "rb") as f:
                f.seek(target - 1)
                if f.read(1) == b"\n":
                    return target
                f.readline()
                return f.tell()
        except OSError:
            return boundary_end

    def _codex_agent_anchor(self, path: str, o: dict, p: dict, base: dict) -> None:
        state = self._codex_sub[path]
        agent_path = state["agent_path"]
        if not agent_path or p.get("recipient") != agent_path:
            return
        visible = _codex_text(p)
        message_type = _header_value(visible, "Message Type:")
        if message_type not in ("NEW_TASK", "MESSAGE", "FOLLOWUP_TASK"):
            return
        label = "task" if message_type == "NEW_TASK" else "message"
        body = visible.partition("Payload:")[2].strip() if "Payload:" in visible else ""
        task = _header_value(visible, "Task name:") or agent_path
        sender = p.get("author") or _header_value(visible, "Sender:")
        text = f"[subagent {label}] {body}" if body else f"[subagent {label}] {task}"
        if sender and not body:
            text += f" (from {sender})"
        meta = p.get("internal_chat_message_metadata_passthrough") or {}
        fp = (o.get("timestamp"), p.get("author"), p.get("recipient"),
              meta.get("turn_id"), visible[:MSG])
        if fp in state["seen"]:
            return
        state["seen"].add(fp)
        state["started"] = True
        self._emit({**base, "type": "user", "text": _snip(text, MSG),
                    "sub_session": True, "parent": state["parent"]})

    def _codex_native_anchor(self, path: str, o: dict, p: dict, base: dict) -> None:
        state = self._codex_sub[path]
        raw_text = _codex_text(p)
        delegated = _codex_delegated_input(raw_text)
        text = delegated or raw_text
        if not text.strip() or (not delegated and _codex_noise(text)):
            return
        meta = p.get("internal_chat_message_metadata_passthrough") or {}
        turn_id = meta.get("turn_id") or ""
        if not state["started"]:
            if not state["start_turn_id"]:
                return
            if turn_id:
                if turn_id != state["start_turn_id"]:
                    return
            elif state["replayed_parent"]:
                return
        label = "message" if state["started"] else "task"
        fp = (o.get("timestamp"), "native", turn_id, text[:MSG])
        if fp in state["seen"]:
            return
        state["seen"].add(fp)
        state["started"] = True
        self._emit({**base, "type": "user", "text": _snip(f"[subagent {label}] {text}", MSG),
                    "sub_session": True, "parent": state["parent"]})

    def _codex_line(self, path: str, ln: str) -> None:
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            return
        if not isinstance(o, dict):
            return
        p = o.get("payload") or {}
        if not isinstance(p, dict):
            return
        if o.get("type") == "session_meta":
            sub = self._codex_sub.get(path)
            sess = self._codex_meta.get(path, ("?", ""))[0]
            if sub is not None and p.get("id") and p.get("id") != sess:
                sub["replayed_parent"] = True
                # old fork journals omit parent_thread_id and replay the parent's
                # session_meta; recover the parent (as cold ingest does) or the child orphans
                sub["parent"] = p["id"]
            return
        if o.get("type") == "turn_context":
            # The cold corpus owns one stable project bucket from the rollout's first
            # session_meta; a per-turn live project would make the same chat unfilterable
            # in search. Keep cwd only for relative image resolution.
            cwd = p.get("cwd") or ""
            if cwd:
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
        sub = self._codex_sub.get(path)
        if sub is not None:
            if o.get("type") == "event_msg" and p.get("type") == "task_started":
                turn_id = p.get("turn_id") or ""
                if not sub["started"] and _uuid7_after(turn_id, sess):
                    sub["start_turn_id"] = turn_id
                return
            if o.get("type") == "response_item" and p.get("type") == "agent_message":
                self._codex_agent_anchor(path, o, p, base)
                return
            if (o.get("type") == "response_item" and p.get("type") == "message"
                    and p.get("role") == "user" and not sub["agent_path"]):
                self._codex_native_anchor(path, o, p, base)
                return
            if not sub["started"]:
                return
            base.update({"sub_session": True, "parent": sub["parent"]})
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
                self._codex_pending[(sess, ev["call_id"])] = ev
            self._emit(ev)
        elif ty in ("function_call_output", "custom_tool_call_output"):
            cid = p.get("call_id", "")
            started = self._codex_pending.pop((sess, cid), None)
            out, ok = _codex_call_output(p.get("output"))
            done = {**base, "type": "tool_done", "call_id": cid,
                    "name": (started or {}).get("name", ""),
                    "output": _snip(out)}
            if ok is not None:
                done["ok"] = ok
            if started and started.get("ts"):
                done["dur"] = max(0, base["ts"] - started["ts"])
            self._emit(done)
        elif ty == "message":
            text = _codex_text(p)
            if not text.strip():
                return
            if p.get("role") == "assistant":
                self._emit({**base, "type": "reply", "text": _snip(text, MSG)})
            elif p.get("role") == "user" and not _codex_noise(text):
                self._emit({**base, "type": "user", "text": _snip(text, MSG)})

    # ------------------------------------------------------------- opencode

    def _tick_opencode(self, now: float) -> None:
        if not self._refresh_source_health(
                "opencode", store_roots("opencode", HOME)):
            return
        for path in opencode_db_paths(HOME, include_default=True):
            try:
                mt = _probe_source(path).st_mtime
                wal = path + "-wal"
                try:
                    mt = max(mt, _probe_source(wal).st_mtime)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    self._record_source_error("opencode", wal, error)
                    continue
            except FileNotFoundError:
                continue
            except OSError as error:
                self._record_source_error("opencode", path, error)
                continue
            first = path not in self._oc_watermark
            if now - mt > ACTIVE_WINDOW_S and not first:
                continue
            self._poll_opencode_db(path, first)

    def _opencode_schema_for(self, conn: sqlite3.Connection, path: str) -> str:
        """Identify the live database generation once per path.

        OpenCode's v2 migration drops every v1 table, so the presence of
        ``session_message`` is the unambiguous cutover signal.
        """
        cached = self._oc_schema.get(path)
        if cached:
            return cached
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('session_message','part')")
        }
        schema = "v2" if "session_message" in tables else "v1" if "part" in tables else ""
        if schema:
            self._oc_schema[path] = schema
        return schema

    def _poll_opencode_db(self, path: str, first: bool) -> None:
        # read-only WITHOUT immutable: we *want* to see the writer's new pages.
        try:
            _probe_source(path)
        except OSError as error:
            self._record_source_error("opencode", path, error)
            return
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            schema = self._opencode_schema_for(conn, path)
            if schema == "v2":
                self._poll_opencode_v2(conn, path, first)
                return
            if schema != "v1":
                return
            if first:
                row = conn.execute("SELECT COALESCE(MAX(time_updated),0) FROM part").fetchone()
                # seed: start from 2 minutes back so a just-started session shows context
                self._oc_watermark[path] = (max(0, (row[0] or 0) - 120_000), "")
                row = conn.execute("SELECT COALESCE(MAX(time_updated),0) FROM message").fetchone()
                # same 2-min backfill as parts: replay a just-completed turn's done
                # marker so the card isn't stuck "replying"
                self._oc_msg_wm[path] = (max(0, (row[0] or 0) - 120_000), "")
            wm_ts, wm_id = self._oc_watermark[path]
            # Poll by time_UPDATED, not time_created: a tool part is born "running" and
            # its row mutates in place to "completed" -- a created-watermark sees it once
            # and the transition never lands (cards stuck on "⚙ tool" forever).
            part_select = (
                "SELECT m.session_id, p.id, p.time_updated, p.data, m.data, "
                "       COALESCE(s.directory,''), COALESCE(s.title,''), s.parent_id, "
                "       p.message_id, s.id IS NOT NULL "
                "FROM part p JOIN message m ON p.message_id = m.id "
                "LEFT JOIN session s ON m.session_id = s.id ")
            primary_rows = conn.execute(
                part_select +
                "WHERE p.time_updated > ? OR (p.time_updated = ? AND p.id > ?) "
                "ORDER BY p.time_updated, p.id LIMIT 800",
                (wm_ts, wm_ts, wm_id)).fetchall()
            mutable_parts = self._oc_mutable_parts.setdefault(path, set())
            row_ids = {row[1] for row in primary_rows}
            recheck_rows = []
            for pid in tuple(mutable_parts - row_ids):
                row = conn.execute(part_select + "WHERE p.id = ?", (pid,)).fetchone()
                if row is None:
                    mutable_parts.discard(pid)
                else:
                    recheck_rows.append(row)
            # A user/assistant message can contain several text parts. Cold ingest joins them
            # into one row; snapshot the whole message when any of its parts first appears so
            # live does not inflate turn counts one part at a time.
            text_by_message = {}
            for row in (*primary_rows, *recheck_rows):
                try:
                    if json.loads(row[3]).get("type") != "text":
                        continue
                except (json.JSONDecodeError, AttributeError):
                    continue
                mid = row[8]
                if mid in text_by_message:
                    continue
                parts = conn.execute(
                    "SELECT data FROM part WHERE message_id=? "
                    "ORDER BY time_created, id", (mid,)).fetchall()
                text_by_message[mid] = [p[0] for p in parts]
            # Turn end: opencode stamps message.data.time.completed when the assistant turn
            # finishes, which bumps message.time_updated -- a watermark on that catches the
            # completion even though no new part row is ever written for it.
            msg_ts, msg_id = self._oc_msg_wm[path]
            msg_select = "SELECT m.id, m.session_id, m.time_updated, m.data FROM message m "
            primary_mrows = conn.execute(
                msg_select +
                "WHERE m.time_updated > ? OR (m.time_updated = ? AND m.id > ?) "
                "ORDER BY m.time_updated, m.id LIMIT 200",
                (msg_ts, msg_ts, msg_id)).fetchall()
            unfinished = self._oc_unfinished_messages.setdefault(path, set())
            message_ids = {row[0] for row in primary_mrows}
            recheck_mrows = []
            for mid in tuple(unfinished - message_ids):
                row = conn.execute(msg_select + "WHERE m.id = ?", (mid,)).fetchone()
                if row is None:
                    unfinished.discard(mid)
                else:
                    recheck_mrows.append(row)
        except sqlite3.Error:
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        if len(self._oc_part_st) > 8000:  # bound dedupe state; transient dupes beat growth
            self._oc_part_st.clear()
            self._oc_text_messages.clear()
        for _sess, pid, ts, *_rest in primary_rows:
            self._oc_watermark[path] = max(self._oc_watermark[path], (ts, pid))
        for (sess, pid, ts, pdata, mdata, directory, title, parent_id, mid,
             has_session) in (*primary_rows, *recheck_rows):
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
                if running:
                    mutable_parts.add(pid)
                else:
                    mutable_parts.discard(pid)
                # emit only on transitions: born->running = one "tool", ->terminal =
                # one "tool_done"; repeated updates at the same stage stay silent
                if prev == status or (running and prev in ("pending", "running")):
                    continue
                self._oc_part_st[pid] = status
                out = st.get("output")
                call_id = pd.get("callID") or str(pid)
                ev = {**base, "type": "tool" if running else "tool_done",
                      "name": pd.get("tool", "?"),
                      "input": _summarize_input(st.get("input")),
                      "output": _snip(out if isinstance(out, str) else out or ""),
                      "ok": {"completed": True, "error": False}.get(status),
                      "call_id": call_id}
                tm = st.get("time") or {}
                if not running and tm.get("start") and tm.get("end"):
                    ev["dur"] = max(0, tm["end"] - tm["start"])
                if running and tm.get("start"):
                    ev["ts"] = tm["start"]  # elapsed ticks from the real start
                self._emit(ev)
            elif pt == "text" and pd.get("text", "").strip():
                if role == "assistant" and not (md.get("time") or {}).get("completed"):
                    mutable_parts.add(pid)
                else:
                    mutable_parts.discard(pid)
                pieces = []
                for raw_part in text_by_message.get(mid, (pdata,)):
                    try:
                        part = json.loads(raw_part)
                    except json.JSONDecodeError:
                        continue
                    text = part.get("text") if part.get("type") == "text" else None
                    if isinstance(text, str) and text.strip() and not (
                            role == "user" and _opencode_noise(text)):
                        pieces.append(text)
                t = "\n".join(pieces)
                previous_text = self._oc_text_messages.get(mid)
                if (not t or previous_text == t
                        or (role != "assistant" and previous_text is not None)):
                    continue
                if not has_session:
                    continue  # cold keeps orphan tool events, but not orphan text rows
                self._oc_part_st[pid] = "text"
                self._oc_text_messages[mid] = t
                if role == "assistant":
                    self._emit({**base, "type": "reply", "text": _snip(t, MSG)})
                elif role == "user":
                    self._emit({**base, "type": "user", "text": _snip(t, MSG)})
        for mid, _sess, tu, _mdata in primary_mrows:
            self._oc_msg_wm[path] = max(self._oc_msg_wm[path], (tu, mid))
        for mid, sess, tu, mdata in (*primary_mrows, *recheck_mrows):
            try:
                md = json.loads(mdata)
            except json.JSONDecodeError:
                continue
            done_ts = (md.get("time") or {}).get("completed")
            if md.get("role") == "assistant" and not done_ts:
                unfinished.add(mid)
            else:
                unfinished.discard(mid)
            if md.get("role") == "assistant" and done_ts and self._oc_done.get(sess) != done_ts:
                self._oc_done[sess] = done_ts
                self._emit({"agent": "opencode", "session": sess, "ts": done_ts,
                            "type": "done"})

    def _poll_opencode_v2(
            self, conn: sqlite3.Connection, path: str, first: bool) -> None:
        """Poll OpenCode 2's message envelopes while preserving v1 live semantics.

        Unlike v1, each row owns every text and tool part for one message and mutates
        in place while streaming. Unfinished rows therefore replace v1's mutable-part
        rechecks, while the updated/id watermark still drains bounded backlogs safely.
        """
        if first:
            row = conn.execute(
                "SELECT COALESCE(MAX(time_updated),0) FROM session_message"
            ).fetchone()
            seed = max(0, (row[0] or 0) - 120_000)
            self._oc_watermark[path] = (seed, "")
            self._oc_msg_wm[path] = (seed, "")

        message_select = (
            "SELECT sm.id, sm.session_id, sm.type, sm.time_created, sm.time_updated, "
            "       sm.data, COALESCE(s.directory,''), COALESCE(s.title,''), "
            "       s.parent_id, s.id IS NOT NULL, COALESCE(s.model,'') "
            "FROM session_message sm "
            "LEFT JOIN session_v2 s ON sm.session_id = s.id ")
        wm_ts, wm_id = self._oc_watermark[path]
        primary_rows = conn.execute(
            message_select +
            "WHERE sm.time_updated > ? OR (sm.time_updated = ? AND sm.id > ?) "
            "ORDER BY sm.time_updated, sm.id LIMIT 800",
            (wm_ts, wm_ts, wm_id)).fetchall()

        unfinished = self._oc_unfinished_messages.setdefault(path, set())
        row_ids = {row[0] for row in primary_rows}
        recheck_rows = []
        for mid in tuple(unfinished - row_ids):
            row = conn.execute(
                message_select + "WHERE sm.id = ?", (mid,)).fetchone()
            if row is None:
                unfinished.discard(mid)
            else:
                recheck_rows.append(row)

        for mid, _sess, _role, _tc, tu, *_rest in primary_rows:
            watermark = (tu, mid)
            self._oc_watermark[path] = max(
                self._oc_watermark[path], watermark)
            self._oc_msg_wm[path] = max(self._oc_msg_wm[path], watermark)

        if len(self._oc_part_st) > 8000:
            self._oc_part_st.clear()
            self._oc_text_messages.clear()
        mutable_parts = self._oc_mutable_parts.setdefault(path, set())
        for row in (*primary_rows, *recheck_rows):
            self._opencode_v2_message(row, unfinished, mutable_parts)

    def _opencode_v2_message(
            self, row: tuple, unfinished: set[str],
            mutable_parts: set[str]) -> None:
        (mid, sess, role, time_created, time_updated, raw_message, directory,
         title, parent_id, has_session, session_model) = row
        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(message, dict):
            return

        timing = message.get("time")
        timing = timing if isinstance(timing, dict) else {}
        ts = timing.get("created") or time_created
        if role == "assistant":
            ts = time_updated
        base = {
            "agent": "opencode", "session": sess, "ts": ts,
            "project": _project_of(directory) if directory else "",
            "title": title,
        }
        model = _opencode_model_id(message, session_model)
        if model:
            base["model"] = model
        if parent_id:
            base["sub_session"] = True

        if role == "user":
            unfinished.discard(mid)
            text = message.get("text")
            previous = self._oc_text_messages.get(mid)
            if (isinstance(text, str) and text.strip()
                    and not _opencode_noise(text) and previous is None
                    and has_session):
                self._oc_text_messages[mid] = text
                self._emit({**base, "type": "user", "text": _snip(text, MSG)})
            return
        if role != "assistant":
            unfinished.discard(mid)
            return

        done_ts = timing.get("completed")
        if done_ts:
            unfinished.discard(mid)
        else:
            unfinished.add(mid)
        content = message.get("content")
        content = content if isinstance(content, list) else []
        text = "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str) and part["text"].strip())
        text_handled = False

        for index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if (part_type == "text" and not text_handled
                    and isinstance(part.get("text"), str)
                    and part["text"].strip()):
                text_handled = True
                previous = self._oc_text_messages.get(mid)
                if text and previous != text and has_session:
                    self._oc_text_messages[mid] = text
                    self._emit({
                        **base, "type": "reply", "text": _snip(text, MSG),
                    })
                continue
            if part_type != "tool":
                continue

            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            status = state.get("status", "")
            running = status in ("pending", "running")
            call_id = part.get("id") or f"{mid}:{index}"
            if running:
                mutable_parts.add(call_id)
            else:
                mutable_parts.discard(call_id)
            previous = self._oc_part_st.get(call_id)
            if (previous == status
                    or (running and previous in ("pending", "running"))):
                continue
            self._oc_part_st[call_id] = status

            part_time = part.get("time")
            part_time = part_time if isinstance(part_time, dict) else {}
            start = part_time.get("ran") or part_time.get("created")
            end = part_time.get("completed")
            event = {
                **base,
                "type": "tool" if running else "tool_done",
                "name": part.get("name", "?"),
                "input": _summarize_input(state.get("input")),
                "output": _snip(_opencode_v2_output(state)),
                "ok": {"completed": True, "error": False}.get(status),
                "call_id": call_id,
            }
            if running and start:
                event["ts"] = start
            elif not running and end:
                event["ts"] = end
            if not running and start and end:
                event["dur"] = max(0, end - start)
            self._emit(event)

        if done_ts and self._oc_done.get(sess) != done_ts:
            self._oc_done[sess] = done_ts
            self._emit({
                "agent": "opencode", "session": sess, "ts": done_ts,
                "type": "done",
            })

    # ------------------------------------------------------------- cursor

    def _tick_cursor(self, now: float) -> None:
        # Cursor (the editor's built-in agent) journals every turn to its own SQLite KV
        # store as it happens — no JSONL, but the same class of source as opencode's DB.
        paths = _cursor_db_paths()
        if not self._refresh_source_health("cursor", paths):
            return
        for path in paths:
            try:
                mt = _probe_source(path).st_mtime
                wal = path + "-wal"
                try:
                    mt = max(mt, _probe_source(wal).st_mtime)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    self._record_source_error("cursor", wal, error)
                    continue
            except FileNotFoundError:
                continue
            except OSError as error:
                self._record_source_error("cursor", path, error)
                continue
            first = path not in self._cur_rowid
            # editor UI state shares this DB, so mtime alone over-triggers; the rowid
            # watermark below makes a spurious wake cost one indexed SELECT.
            if now - mt > ACTIVE_WINDOW_S and not first:
                continue
            self._poll_cursor_db(path, first, now)

    def _poll_cursor_db(self, path: str, first: bool, now: float) -> None:
        # read-only WITHOUT immutable, same as the opencode poll: we want the writer's new WAL pages
        try:
            _probe_source(path)
        except OSError as error:
            self._record_source_error("cursor", path, error)
            return
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            if first:
                row = conn.execute(
                    "SELECT COALESCE(MAX(rowid),0) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
                ).fetchone()
                # seed shallow by row COUNT (no indexed timestamp);
                # the last ~40 bubble rows ≈ the latest turn's tail
                self._cur_rowid[path] = max(0, (row[0] or 0) - (_CUR_SEED_ROWS if row[0] else 0))
            census = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM cursorDiskKV "
                "WHERE key LIKE 'bubbleId:%'").fetchone()
            bubbles, max_rowid = int(census[0] or 0), int(census[1] or 0)
            known_bubbles = self._cur_bubbles.get(path)
            if known_bubbles is not None and bubbles < known_bubbles:
                # bubbles vanished (a conversation was deleted): stamp it as activity and
                # re-open the watermark so reused tail rowids stay visible to future polls
                self._mark_store_mutation(now)
                self._cur_rowid[path] = min(self._cur_rowid[path], max_rowid)
            self._cur_bubbles[path] = bubbles
            rows = conn.execute(
                "SELECT rowid, key, value FROM cursorDiskKV "
                "WHERE rowid > ? AND key LIKE 'bubbleId:%' ORDER BY rowid LIMIT 500",
                (self._cur_rowid[path],)).fetchall()
            header_cache = {}
            for rid, key, val in rows:
                self._cur_rowid[path] = max(self._cur_rowid[path], rid)
                self._cursor_bubble(conn, key, val, now, header_cache=header_cache)
            # a tool bubble is born "loading" and its status mutates IN PLACE to a terminal
            # value — an UPDATE never moves rowid, so re-read the few still-pending tool
            # bubbles each tick until they settle.
            for bk in list(self._cur_pending):
                r = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (bk,)).fetchone()
                if r:
                    self._cursor_bubble(conn, bk, r[0], now, recheck=True,
                                        header_cache=header_cache)
            for bk in list(self._cur_text_pending):
                r = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (bk,)).fetchone()
                if r:
                    self._cursor_bubble(conn, bk, r[0], now, recheck=True,
                                        header_cache=header_cache)
                else:
                    self._cur_text_pending.discard(bk)
                    self._cur_text.pop(bk, None)
            # turn end = composerData.generatingBubbleIds emptying (no per-bubble done marker)
            if now >= self._cur_done_check and self._cur_active:
                self._cur_done_check = now + _CUR_DONE_RECHECK_S
                self._cursor_check_done(conn, now)
        except sqlite3.Error:
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _cursor_meta(self, conn, cid: str) -> tuple:
        m = self._cur_meta.get(cid)
        if m is not None:
            return m
        proj = title = model = ""
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                           ("composerData:" + cid,)).fetchone()
        if row:
            try:
                o = json.loads(row[0])
                fs = ((o.get("workspaceIdentifier") or {}).get("uri") or {}).get("fsPath", "")
                proj = _project_of(fs) if fs else ""
                title = o.get("name") or ""
                model = (o.get("modelConfig") or {}).get("modelName") or ""
            except (json.JSONDecodeError, AttributeError):
                pass
        self._cur_meta[cid] = (proj, title, model)
        return self._cur_meta[cid]

    @staticmethod
    def _cursor_headers(conn, cid: str, cache: dict[str, set[str]]) -> set[str]:
        """Current canonical bubble membership for one composer (same authority as cold)."""
        if cid in cache:
            return cache[cid]
        members = set()
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                           ("composerData:" + cid,)).fetchone()
        if row:
            try:
                data = json.loads(row[0])
                members = {h.get("bubbleId") for h in
                           (data.get("fullConversationHeadersOnly") or [])
                           if isinstance(h, dict) and isinstance(h.get("bubbleId"), str)}
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        cache[cid] = members
        return members

    def _cursor_bubble(self, conn, key: str, val, now: float, recheck: bool = False,
                       header_cache: dict[str, set[str]] | None = None) -> None:
        try:
            b = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return
        try:
            _, cid, bid = key.split(":", 2)
        except ValueError:
            return
        header_cache = header_cache if header_cache is not None else {}
        if bid not in self._cursor_headers(conn, cid, header_cache):
            # Regeneration leaves orphan bubble rows in the KV store. They are not part of
            # Cursor's current conversation and cold ingest correctly excludes them.
            self._cur_pending.pop(key, None)
            self._cur_text_pending.discard(key)
            self._cur_text.pop(key, None)
            return
        proj, title, model = self._cursor_meta(conn, cid)
        base = {"agent": "cursor", "session": cid, "ts": _ts_ms(b.get("createdAt")),
                "project": proj, "title": title}
        if model:
            base["model"] = model
        tfd = b.get("toolFormerData")
        if isinstance(tfd, dict) and tfd:
            self._cur_text_pending.discard(key)
            self._cur_text.pop(key, None)
            status = str(tfd.get("status", ""))
            terminal = status in _CUR_TERMINAL
            call_id = tfd.get("toolCallId") or f"cursor:{bid}"
            args = tfd.get("params")
            if not isinstance(args, str) or not args.strip():
                args = tfd.get("rawArgs")
            if not recheck:
                self._cur_active[cid] = now
                self._emit({**base, "type": "tool", "name": tfd.get("name", "?"),
                            "input": _summarize_input(_cur_loads(args)),
                            "call_id": call_id})
                if not terminal:
                    self._cur_pending[key] = (cid, tfd.get("name", "?"), base["ts"])
                    return
            elif not terminal:
                return  # still running; stay pending, stay silent
            self._cur_pending.pop(key, None)
            self._cur_active[cid] = now
            self._emit({**base, "type": "tool_done", "name": tfd.get("name", "?"),
                        "call_id": call_id,
                        "ok": status == "completed",
                        "output": _snip(tfd.get("result") or "")})
        elif (b.get("type") == 1 and (b.get("text") or "").strip()
              and not _noise(b["text"])):
            if recheck:
                return
            self._cur_active[cid] = now
            self._emit({**base, "type": "user", "text": _snip(b["text"], MSG)})
        elif b.get("type") == 2 and (b.get("text") or "").strip() and not b.get("isThought"):
            text = _snip(b["text"], MSG)
            if self._cur_text.get(key) == text:
                return
            self._cur_text[key] = text
            self._cur_text_pending.add(key)
            self._cur_active[cid] = now
            self._emit({**base, "type": "reply", "text": text})
        elif recheck:
            return

    def _cursor_forget_text(self, cid: str) -> None:
        prefix = f"bubbleId:{cid}:"
        for key in tuple(self._cur_text_pending):
            if key.startswith(prefix):
                self._cur_text_pending.discard(key)
                self._cur_text.pop(key, None)

    def _cursor_check_done(self, conn, now: float) -> None:
        for cid in list(self._cur_active):
            if now - self._cur_active[cid] > ACTIVE_WINDOW_S * 3:
                del self._cur_active[cid]
                self._cursor_forget_text(cid)
                continue
            row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                               ("composerData:" + cid,)).fetchone()
            if not row:
                continue
            try:
                o = json.loads(row[0])
            except json.JSONDecodeError:
                continue
            ts = o.get("lastUpdatedAt") or int(now * 1000)
            if not o.get("generatingBubbleIds") and self._cur_done.get(cid) != ts:
                self._cur_done[cid] = ts
                ev = {"agent": "cursor", "session": cid, "ts": ts, "type": "done"}
                if o.get("status") == "aborted":
                    ev["why"] = "interrupted"
                self._emit(ev)
                del self._cur_active[cid]
                self._cursor_forget_text(cid)

    # ------------------------------------------------------------- antigravity

    def _tick_antigravity(self, now: float) -> None:
        root = store_root("antigravity", HOME)
        if (not self._refresh_source_health("antigravity", [root])
                or not _plain_dir(root)):
            return
        root_entries = self._source_entries("antigravity", root)
        if root_entries is None:
            return
        dirs = []
        for entry in root_entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry)
            except OSError as error:
                self._record_source_error("antigravity", entry.path, error)
        for d in dirs:
            generated = os.path.join(d.path, ".system_generated")
            logs = os.path.join(generated, "logs")
            if not _plain_dir(generated) or not _plain_dir(logs):
                continue
            tr = os.path.join(logs, "transcript.jsonl")
            st = _plain_file_stat(tr)
            if st is None:
                continue
            known = tr in self._offsets
            if now - st.st_mtime > ACTIVE_WINDOW_S and not known:
                continue
            count = self._dispatch_delta(
                tr, st.st_size, _fid(st), lambda ln: self._agy_line(d.name, ln),
                agent="antigravity")
            if count:  # file growth = alive, even if these lines emitted no event
                self._mark_live(f"antigravity:{d.name}", now)
            self._agy_mailbox(d.path, d.name, now)

    def _agy_line(self, sess: str, ln: str) -> None:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            return
        if not isinstance(e, dict):
            return
        ty, src = e.get("type"), e.get("source")
        ts = _ts_ms(e.get("created_at"))
        base = {"agent": "antigravity", "session": sess, "ts": ts}
        q = self._agy_announced.setdefault(sess, deque(maxlen=50))
        if ty == "PLANNER_RESPONSE" and src == "MODEL":
            tcs = e.get("tool_calls") or []
            tcs = tcs if isinstance(tcs, list) else []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args") or {}
                args = args if isinstance(args, dict) else {}
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
        if not _plain_dir(mdir):
            return
        entries = self._source_entries("antigravity", mdir)
        if entries is None:
            return
        for e in entries:
            if not e.name.endswith(".json") or e.path in self._agy_mail_seen:
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    continue
                self._agy_mail_seen.add(e.path)
                if not self._booted and now - st.st_mtime > ACTIVE_WINDOW_S:
                    continue  # old mail: mark seen, don't emit
                with open(e.path, encoding="utf-8", errors="replace") as stream:
                    m = json.load(stream)
            except OSError as error:
                self._record_source_error("antigravity", e.path, error)
                continue
            except json.JSONDecodeError:
                continue
            if not isinstance(m, dict):
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

    def _seed_unknown_parents(self) -> None:
        """A sub row names its parent, but a waiting orchestrator's file can be
        older than every scan window (its store is quiet while children work,
        and codex scans only today's date dirs). Locate the parent by session id
        and read it once - from then on the scans own the path as known."""
        want: dict[str, str] = {}
        for s in list(self.sessions.values()):
            pid = s.get("parent")
            if (pid and self.sessions.get(f"{s['agent']}:{pid}") is None
                    and pid not in self._parent_miss):
                want.setdefault(pid, s["agent"])
        for pid, agent in want.items():
            path = None
            st = None
            if agent == "codex":
                root = store_root("codex", HOME)
                if _plain_dir(root):
                    for directory, dirs, names in os.walk(
                            root, followlinks=False,
                            onerror=self._walk_source_error("codex")):
                        dirs[:] = [name for name in dirs
                                   if _plain_dir(os.path.join(directory, name))]
                        for name in names:
                            if not (store_content("codex", name) and pid in name):
                                continue
                            candidate = os.path.join(directory, name)
                            found = _plain_file_stat(candidate)
                            if found is not None and (path is None or candidate > path):
                                path, st = candidate, found
            elif agent == "claude":
                root = store_root("claude", HOME)
                if _plain_dir(root):
                    projects = self._source_entries("claude", root) or []
                    expected = f"{pid}.jsonl"
                    for project in projects:
                        try:
                            if not project.is_dir(follow_symlinks=False):
                                continue
                            entries = self._source_entries("claude", project.path)
                            found_entry = next((entry for entry in entries or []
                                                if entry.name == expected
                                                and entry.is_file(
                                                    follow_symlinks=False)), None)
                        except OSError as error:
                            self._record_source_error(
                                "claude", project.path, error)
                            continue
                        if found_entry is not None:
                            path = found_entry.path
                            st = _plain_file_stat(path)
                            break
            if st is None:
                self._parent_miss.add(pid)
                continue
            if agent == "codex" and path not in self._codex_meta:
                # the scan's own head classification: session_meta sits at the top
                # of the rollout, outside the tail-seed window on a large file
                head = self._codex_head(path)
                if head is None:
                    continue
                sess, proj, sub = head
                if sub is not None and sub.get("internal_guardian"):
                    self._parent_miss.add(pid)
                    continue
                self._codex_meta[path] = (sess, proj)
                if sub is not None:
                    self._codex_sub[path] = sub
            def dispatch(ln):
                if agent == "codex":
                    self._codex_line(path, ln)
                else:
                    self._claude_line(os.path.basename(path)[:-6], ln)
            self._dispatch_delta(
                path, st.st_size, _fid(st), dispatch, agent=agent)

    def run(self) -> None:
        while True:
            t0 = time.time()
            now = t0
            for tick_name in LIVE_TICKS.values():
                tick = getattr(self, tick_name)
                tt = time.perf_counter()
                try:
                    tick(now)
                except Exception as e:  # noqa: BLE001 -- one bad store must not kill the watcher
                    import traceback
                    with self._state_lock:
                        self._global_errors[tick_name] = (
                            f"{tick.__name__}: {e!r} @ "
                            f"{traceback.format_exc(limit=3).splitlines()[-2].strip()}")
                else:
                    with self._state_lock:
                        self._global_errors.pop(tick_name, None)
                with self._state_lock:
                    self._tick_ms[tick.__name__] = int((time.perf_counter() - tt) * 1000)
            try:
                self._seed_unknown_parents()
            except Exception as e:  # noqa: BLE001 -- a bad locate must not kill the watcher
                with self._state_lock:
                    self._global_errors["seed_parents"] = f"seed_parents: {e!r}"
            else:
                with self._state_lock:
                    self._global_errors.pop("seed_parents", None)
            self._refresh_writer_liveness(now)
            self._sweep_vanished_paths(now)
            work_ms = int((time.time() - t0) * 1000)
            with self._state_lock:
                self._n_loops += 1
                self._booted = True
                self._boot_complete.set()
                self._work_ms = work_ms
                self._work_total_ms += work_ms
            self._decay(now)
            # Interactive consumers stay sub-second. The headless index daemon does not need to
            # rescan every store tree 1.4 times/second: five seconds while active is still near
            # real-time search freshness, then it backs off sharply while idle.
            with self._state_lock:
                age = time.time() - self._last_event_wall
                busy = any(session.get("working") for session in self.sessions.values())
            if self._headless_indexd:
                poll = 5.0 if (busy or age < 30) else (10.0 if age < 300 else 15.0)
            else:
                poll = 0.35 if age < 10 else POLL_S
            elapsed = time.time() - t0
            with self._state_lock:
                self._poll_s = poll
                self._loop_ms = int(elapsed * 1000)
            time.sleep(max(0.1, poll - elapsed))

    def _mark_store_mutation(self, now: float) -> None:
        """A store shrank, vanished, or dropped rows: deletion-shaped activity with no
        session to credit. Stamp the wall clock the auto-indexer's gate reads, or the
        pass that would notice the deletion never gets scheduled. Post-boot only."""
        with self._state_lock:
            if self._booted:
                self._last_event_wall = now

    def _sweep_vanished_paths(self, now: float) -> None:
        """A tailed file that disappeared never re-enters a tick's scan, so its deletion
        would otherwise be invisible: stamp it once and drop the tail bookkeeping."""
        if now < self._vanish_sweep_due:
            return
        self._vanish_sweep_due = now + _VANISH_SWEEP_S
        gone = [path for path in list(self._offsets) if not os.path.lexists(path)]
        for path in gone:
            self._offsets.pop(path, None)
            self._partials.pop(path, None)
            self._ctimes.pop(path, None)
        if gone:
            self._mark_store_mutation(now)

    def _mark_live(self, key: str, now: float) -> None:
        """Mark a session alive at wall-clock NOW because its store just grew. Tailing reads
        EVERY new line, but only conversational ones (user/assistant/tool) emit events and move
        last_ts - so a session writing only system / progress / heartbeat lines (a long xhigh
        think, or a harness with custom line types like bridge-session/agent-name records)
        looked idle while clearly working. Freshness keys off this wall stamp too, so a growing
        file reads as active regardless of line type. Post-boot only: the startup seed read
        replays history and must not fake liveness on old files."""
        with self._state_lock:
            if not self._booted:
                return
            # the auto-indexer's gate reads _last_event_wall too; without this stamp,
            # system-only growth would never trigger reindex and search would stay stale
            self._last_event_wall = now
            s = self.sessions.get(key)
            if s is not None:
                s["live_ts"] = now * 1000

    def _decay(self, now: float) -> None:
        """A turn that goes silent is a turn that died: 'thinking'/'replying' with no
        store write for STALL_S means the CLI was closed or crashed mid-turn (hooks
        would get a session-end callback; silence is the passive equivalent). Tool
        states are exempt -- a quiet 10-minute cargo build is normal and the ticking
        elapsed already tells that story."""
        nowms = now * 1000
        with self._state_lock:
            self._expire_working(nowms)
            for s in self.sessions.values():
                effective = max(s.get("last_ts", 0), s.get("live_ts", 0))
                if s.get("state") in ("thinking", "replying") and effective \
                        and nowms - effective > STALL_S * 1000:
                    # keep the original last_ts (+1ms) so the card stays faded-idle
                    self._emit({"agent": s["agent"], "session": s["session"],
                                "ts": s["last_ts"] + 1, "type": "done", "why": "stalled"})


_WATCHER: LiveWatcher | None = None


def watcher(*, headless_indexd: bool = False) -> LiveWatcher:
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = LiveWatcher(headless_indexd=headless_indexd)
        _WATCHER.start()
    elif _WATCHER._headless_indexd != headless_indexd:
        raise RuntimeError("live watcher mode cannot change after startup")
    return _WATCHER
