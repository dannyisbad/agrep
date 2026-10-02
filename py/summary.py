"""agrep summary: per-project briefings from the published index.

    agrep summary                        # last 7d: time, chats, worked-on, open items per project
    agrep summary --since 30d            # the month in review
    agrep summary pending --project api  # only open items
    agrep summary time --group week      # estimated active time table

"Estimated active time" is derived from turn timestamps: each turn is credited
until the next one, capped by an idle cap (20m), intervals are merged per chat
and unioned across a chat and its side chats so a subagent minute never counts
twice. It is never elapsed session span and never billable time.

Pending status reads each root chat's final reply, its latest tool events and
its last todo list, and reports a status with a stated confidence. Explicit
completion always wins over an open-looking bullet.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field

import common
import compact
import console
import indexd_runtime
import search
import surface_policy as surface

DEFAULT_SINCE = "7d"
DEFAULT_IDLE_CAP = "20m"
MODES = ("pending", "time")
GROUPS = ("day", "week", "month")
METRIC = "estimated active time"
# the Rust ingest caps indexed replies at this many chars; a capped reply hides its ending
_REPLY_CAP_CHARS = 64_000

STATUS_CONFIDENCE = {
    "waiting_on_user": "high",
    "open_next_steps": "high",
    "agent_work_incomplete": "medium",
    "todo_open": "medium",
    "unknown": "low",
}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+")
_MARKUP_RE = re.compile(r"[*_~#>]+")
_REQUEST_RE = re.compile(
    r"\b(should i|shall i|do you want|would you like|which|what do you|what would you|"
    r"can you|could you|please confirm|your call|confirm|choose|prefer|"
    r"do you|are you|is it ok|ok to|may i|want me to|how should|where should|"
    r"tell me)\b", re.IGNORECASE)
# "let me know if..." is a sign-off, not a blocker: only an explicit wait counts without a "?"
_REQUEST_NO_QUESTION_RE = re.compile(
    r"\b(please confirm|awaiting your|waiting for your|waiting on you|your call)\b",
    re.IGNORECASE)
# the heading must be the whole line: "To do this, I changed:" introduces a done list, not a todo
_SECTION_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?(next steps?|remaining|follow[- ]?ups?|to-?do|open items?|"
    r"outstanding|still to do|left to do)(?:\*\*)?\s*:?\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$")
_CHECKED_RE = re.compile(r"^\[[xX✓✔]\]")
_UNCHECKED_RE = re.compile(r"^\[\s\]")
_ITEM_DONE_RE = re.compile(
    r"(?:^~~.*~~$)|(?:\((?:done|completed|cancelled|canceled|skipped)\)\s*$)|"
    r"(?:[-:—]\s*(?:done|completed|cancelled|canceled|skipped)\.?\s*$)", re.IGNORECASE)
_TODO_TOOL_RE = re.compile(r"todo", re.IGNORECASE)
_TODO_OPEN_STATUSES = frozenset({
    "pending", "in_progress", "in-progress", "not_started", "not-started", "open",
    "todo", "active", "blocked", "queued"})
_TODO_CLOSED_STATUSES = frozenset({
    "completed", "complete", "done", "cancelled", "canceled", "skipped", "closed"})
_CONTROL_STOP_RE = re.compile(r"interrupt|abort|cancel|stop", re.IGNORECASE)


@dataclass
class _Turn:
    turn: int
    ts: int
    who: str
    text: str
    digest: str | None


@dataclass
class _Chat:
    session: str
    agent: str
    project: str
    root: str
    side: bool
    first_ts: int
    last_ts: int
    first_text: str
    turns: list[_Turn] = field(default_factory=list)
    replies: dict[int, str] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    # turns the caller's live window withholds; the chat's state after them is not history
    withheld: bool = False

    def last_turn(self) -> _Turn | None:
        return max(self.turns, key=lambda row: (row.turn, row.ts)) if self.turns else None

    @property
    def label(self) -> str:
        """The project name chats and search print; adapters store either it or a cwd path."""
        return surface.project_leaf(self.project) or "-"


def _parse_duration(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*([smh])\s*", value.lower())
    if match is None:
        return None
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)] * 1000


def _duration_label(ms: int) -> str:
    minutes = int(round(max(0, ms) / 60000))
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m" if rest else f"{hours}h"


def _parser() -> surface.ArgumentParser:
    ap = surface.ArgumentParser(
        prog="agrep summary",
        description="per-project briefings from indexed chats: estimated active "
                    "time, chats and turns, what was worked on, what is still open",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep summary                     last 7 days, one briefing per project\n"
               "  agrep summary --since 30d         the month in review\n"
               "  agrep summary pending             open items only, most confident first\n"
               "  agrep summary time --group week   estimated active time per week and project\n"
               "  agrep summary --project webapp --json\n"
               "\nTime is an estimate from turn timestamps (idle cap, side chats folded "
               "into their family), never elapsed span or billable time. Pending "
               "statuses: waiting_on_user (high), open_next_steps (high), "
               "agent_work_incomplete (medium), todo_open (medium), unknown (low).\n"
               "exit: 0 something to report, 1 proven nothing, 2 no index or unverified.")
    ap.add_argument("mode", nargs="?", choices=MODES, default=None,
                    help="pending: open items only; time: estimated active time table; "
                         "omitted: full briefing")
    ap.add_argument("--since", metavar="WHEN", default=None,
                    help=f"window start (7d / 24h / 2w / 30m, or 2026-06-01); default {DEFAULT_SINCE}")
    ap.add_argument("--until", "--before", dest="until", metavar="WHEN",
                    help="window end (same formats as --since)")
    ap.add_argument("--project", help=surface.PROJECT_HELP)
    ap.add_argument("--agent", help=f"only this agent ({', '.join(common.KNOWN_AGENTS)})")
    ap.add_argument("--group", choices=GROUPS, default=None,
                    help="time mode: bucket by local day (default), ISO week or month")
    ap.add_argument("--idle-cap", metavar="DURATION", default=DEFAULT_IDLE_CAP,
                    help=f"longest gap between turns still counted as active "
                         f"(5m / 20m / 1h; default {DEFAULT_IDLE_CAP})")
    ap.add_argument("-n", "--max", type=int, default=5, metavar="N",
                    help="briefing: chats listed per project (default 5; 0 = all)")
    self_group = ap.add_mutually_exclusive_group()
    self_group.add_argument("--self", dest="include_self", action="store_true",
                            help="include the calling agent's current-window turns")
    self_group.add_argument("--no-self", dest="force_no_self", action="store_true",
                            help="exclude the calling session and its indexed family, "
                                 "even outside agent shells")
    ap.add_argument("--json", action="store_true",
                    help="one agrep-meta object, then one JSON object per project row, "
                         "pending item or time row")
    ap.add_argument("--no-auto", action="store_true", help=surface.NO_AUTO_HELP)
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    return ap


# --- loading ---------------------------------------------------------------

def _family_metadata(sessions: list[str], index: dict[str, dict]) -> dict[str, tuple[str, bool]]:
    """session -> (family root, side) from the published family index, else sessions.jsonl."""
    indexed = common.indexed_family_metadata(sessions)
    if indexed is not None:
        return indexed
    parents = {
        session: str(row.get("parent") or "")
        for session, row in index.items() if row.get("parent")}
    memo: dict[str, str] = {}
    return {
        session: (common.family_root(session, parents, memo), session in parents)
        for session in sessions}


def _load_transcripts(chats: dict[str, _Chat]) -> bool:
    """Fill turns and replies from the search db, else from the materialized JSONL."""
    sessions = list(chats)
    try:
        db = search._load_corpusdb().connect(allow_stale=True)
    except (OSError, sqlite3.DatabaseError, RuntimeError, TypeError, ValueError):
        db = None
    if db is not None:
        seen: dict[str, set[int]] = {session: set() for session in sessions}
        try:
            for start in range(0, len(sessions), 400):
                page = sessions[start:start + 400]
                marks = ",".join("?" for _ in page)
                rows = db.execute(
                    "SELECT session,turn,ts,who,text,content_digest FROM msgs "
                    f"WHERE session IN ({marks}) AND who<>'tool' "
                    "ORDER BY session,turn,rowid", page)
                for session, turn, ts, who, text, digest in rows:
                    session = str(session)
                    chat = chats[session]
                    turn = int(turn or 0)
                    if who == "agent":
                        chat.replies.setdefault(turn, str(text or ""))
                        continue
                    if turn in seen[session]:
                        continue
                    seen[session].add(turn)
                    chat.turns.append(_Turn(turn, int(ts or 0), str(who or "user"),
                                            str(text or ""), digest))
            return True
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            for chat in chats.values():
                chat.turns.clear()
                chat.replies.clear()
        finally:
            db.close()
    import explore
    messages = explore._messages_by_session()
    replies = explore._reply_records_by_id()
    for session, chat in chats.items():
        seen: set[int] = set()
        for row in sorted(messages.get(session, ()), key=lambda r: int(r.get("turn") or 0)):
            turn = int(row.get("turn") or 0)
            if turn in seen or row.get("who") == "tool":
                continue
            seen.add(turn)
            text = str(row.get("text") or "")
            chat.turns.append(_Turn(turn, int(row.get("ts") or 0),
                                    str(row.get("who") or "user"), text, None))
            reply = replies.get(str(row.get("id") or ""))
            if reply is not None and reply["reply"]:
                chat.replies[turn] = reply["reply"]
    return False


def _load_events(chat: _Chat) -> None:
    import explore
    try:
        chat.events = explore.get_events(chat.agent, chat.session)
    except Exception:  # noqa: BLE001 -- events are evidence, never a reason to fail a briefing
        chat.events = []


# --- time ------------------------------------------------------------------

def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _span(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in intervals)


def _chat_intervals(chat: _Chat, cap_ms: int) -> tuple[list[tuple[int, int]], int]:
    """Merged active intervals for one chat plus its count of unknown-timestamp turns."""
    usable = sorted((row.ts, row.turn) for row in chat.turns if row.ts > 0)
    unknown = sum(1 for row in chat.turns if row.ts <= 0)
    if not usable:
        return [], unknown
    intervals = []
    for (start, _), (following, _) in zip(usable, usable[1:]):
        intervals.append((start, min(following, start + cap_ms)))
    last = usable[-1][0]
    latest_event = max((int(event.get("ts") or 0) for event in chat.events), default=0)
    if latest_event > last:
        intervals.append((last, min(latest_event, last + cap_ms)))
    return _merge(intervals), unknown


def _clip(intervals: list[tuple[int, int]], since: int | None,
          until: int | None) -> list[tuple[int, int]]:
    out = []
    for start, end in intervals:
        if since is not None:
            start = max(start, since)
        if until is not None:
            end = min(end, until)
        if end > start:
            out.append((start, end))
    return out


def _local(ts_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts_ms / 1000)


def _next_midnight_ms(ts_ms: int) -> int:
    day = _local(ts_ms).replace(hour=0, minute=0, second=0, microsecond=0)
    following = day + _dt.timedelta(days=1)
    midnight = int(following.timestamp() * 1000)
    # a DST fold can place the next calendar midnight at or before the start; step past it
    while midnight <= ts_ms:
        following += _dt.timedelta(days=1)
        midnight = int(following.timestamp() * 1000)
    return midnight


def _split_days(intervals: list[tuple[int, int]]) -> dict[str, int]:
    """Milliseconds per local calendar day, every interval split at local midnight."""
    out: dict[str, int] = {}
    for start, end in intervals:
        cursor = start
        while cursor < end:
            boundary = min(end, _next_midnight_ms(cursor))
            key = _local(cursor).strftime("%Y-%m-%d")
            out[key] = out.get(key, 0) + (boundary - cursor)
            cursor = boundary
    return out


def _period(day: str, group: str) -> str:
    date = _dt.date.fromisoformat(day)
    if group == "week":
        year, week, _ = date.isocalendar()
        return f"{year}-W{week:02d}"
    if group == "month":
        return day[:7]
    return day


# --- pending ---------------------------------------------------------------

def _prose(reply: str) -> str:
    text = _FENCE_RE.sub(" ", reply)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    text = _URL_RE.sub(" ", text)
    return text


def _final_sentence(prose: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", prose) if p.strip()]
    if not paragraphs:
        return ""
    tail = _MARKUP_RE.sub(" ", paragraphs[-1])
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", common.one_line(tail)) if s.strip()]
    return sentences[-1] if sentences else ""


def _direct_request(reply: str) -> str | None:
    """The final sentence when it asks the human something; None otherwise."""
    prose = _prose(reply)
    sentence = _final_sentence(prose)
    if not sentence:
        return None
    if sentence.endswith("?") and _REQUEST_RE.search(sentence):
        return sentence
    if _REQUEST_NO_QUESTION_RE.search(sentence):
        return sentence
    return None


def _open_section_items(reply: str) -> tuple[str, list[str], int] | None:
    """(heading, open items, closed count) for the last next-steps style section."""
    lines = _prose(reply).splitlines()
    found = None
    for index, line in enumerate(lines):
        heading = _SECTION_RE.match(line)
        if heading is None:
            continue
        open_items: list[str] = []
        closed = 0
        for candidate in lines[index + 1:]:
            if not candidate.strip():
                if open_items or closed:
                    break
                continue
            bullet = _BULLET_RE.match(candidate)
            if bullet is None:
                if _SECTION_RE.match(candidate) or open_items or closed:
                    break
                continue
            item = bullet.group(1).strip()
            if _CHECKED_RE.match(item) or _ITEM_DONE_RE.search(item):
                closed += 1
                continue
            item = _UNCHECKED_RE.sub("", item).strip()
            open_items.append(common.one_line(_MARKUP_RE.sub(" ", item)))
        if open_items or closed:
            found = (heading.group(1).lower(), open_items, closed)
    return found


def _todo_items(event: dict) -> list[tuple[str, str]] | None:
    """(item, status) pairs from a todo tool's captured input; None when unparseable."""
    raw = str(event.get("input") or "")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    items = payload.get("todos") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if isinstance(item, str):
            out.append((common.one_line(item), "open"))
            continue
        if not isinstance(item, dict):
            continue
        name = next((str(item[key]) for key in ("content", "title", "task", "text", "name")
                     if isinstance(item.get(key), str) and item[key].strip()), "")
        status = str(item.get("status") or item.get("state") or "").strip().lower()
        if name:
            out.append((common.one_line(name), status))
    return out


def _latest_todo(chat: _Chat) -> tuple[list[tuple[str, str]] | None, bool]:
    """Items of the last todo event and whether a todo event existed at all."""
    for event in reversed(chat.events):
        if event.get("kind") == "tool" and _TODO_TOOL_RE.search(str(event.get("name") or "")):
            return _todo_items(event), True
    return None, False


def _open_todos(chat: _Chat) -> tuple[list[str], list[str]]:
    """(open todo items, caveats)."""
    items, present = _latest_todo(chat)
    if not present:
        return [], []
    if items is None:
        return [], ["todo list captured in an unparseable form (truncated or unknown shape)"]
    open_items = [name for name, status in items
                  if status in _TODO_OPEN_STATUSES or
                  (status not in _TODO_CLOSED_STATUSES and status == "")]
    return open_items, []


def _events_after(chat: _Chat, ts: int) -> list[dict]:
    return [event for event in chat.events if int(event.get("ts") or 0) >= ts]


def _classify_root(chat: _Chat) -> dict | None:
    last = chat.last_turn()
    if last is None:
        return None
    reply = chat.replies.get(last.turn, "")
    record = {"turn": last.turn, "turn_ts": last.ts, "signals": [], "evidence": "",
              "items": [], "caveats": []}
    request = _direct_request(reply) if reply else None
    if request is not None:
        record.update(status="waiting_on_user", signals=["final reply asks you something"],
                      evidence=request)
        return record
    section = _open_section_items(reply) if reply else None
    if section is not None and section[1]:
        heading, open_items, closed = section
        record.update(status="open_next_steps", items=open_items,
                      signals=[f"{heading} section lists {len(open_items)} open item"
                               f"{'s' if len(open_items) != 1 else ''}"
                               + (f" ({closed} done)" if closed else "")],
                      evidence=open_items[0])
        return record
    later = _events_after(chat, last.ts) if last.ts > 0 else list(chat.events)
    failed = next((event for event in reversed(later)
                   if event.get("kind") == "tool" and event.get("ok") is False), None)
    stopped = next((event for event in reversed(later)
                    if event.get("kind") == "control"
                    and _CONTROL_STOP_RE.search(str(event.get("name") or ""))), None)
    if not reply:
        signals = ["no captured reply to the last turn"]
        if failed is not None:
            signals.append(f"latest tool failed ({failed.get('name') or 'tool'})")
        if stopped is not None:
            signals.append(f"turn {stopped.get('name') or 'stopped'}")
        if last.ts <= 0:
            record.update(status="unknown", signals=signals + ["last turn has no timestamp"],
                          evidence=common.one_line(last.text))
        else:
            record.update(status="agent_work_incomplete", signals=signals,
                          evidence=common.one_line(failed.get("output") if failed
                                                   else last.text))
        return record
    open_items, caveats = _open_todos(chat)
    record["caveats"] = caveats
    if open_items:
        record.update(status="todo_open", items=open_items,
                      signals=[f"last todo list has {len(open_items)} open item"
                               f"{'s' if len(open_items) != 1 else ''}"],
                      evidence=open_items[0])
        return record
    if len(reply) >= _REPLY_CAP_CHARS:
        record.update(status="unknown", signals=["reply capped at index time; ending not indexed"],
                      evidence=_final_sentence(_prose(reply)))
        return record
    if caveats:
        record.update(status="unknown", signals=list(caveats), evidence=common.one_line(last.text))
        return record
    return None


def _classify_side(chat: _Chat, root_last_ts: int) -> dict | None:
    """Side-chat evidence only rolls up when the side chat was the family's latest activity."""
    last = chat.last_turn()
    if last is None or last.ts <= 0 or last.ts < root_last_ts:
        return None
    record = {"turn": last.turn, "turn_ts": last.ts, "signals": [], "evidence": "",
              "items": [], "caveats": []}
    if not chat.replies.get(last.turn, ""):
        failed = next((event for event in reversed(_events_after(chat, last.ts))
                       if event.get("kind") == "tool" and event.get("ok") is False), None)
        signals = ["side chat has no captured reply to its last turn"]
        if failed is not None:
            signals.append(f"latest tool failed ({failed.get('name') or 'tool'})")
        record.update(status="agent_work_incomplete", signals=signals,
                      evidence=common.one_line(last.text))
        return record
    open_items, _caveats = _open_todos(chat)
    if open_items:
        record.update(status="todo_open", items=open_items,
                      signals=[f"side chat todo list has {len(open_items)} open item"
                               f"{'s' if len(open_items) != 1 else ''}"],
                      evidence=open_items[0])
        return record
    return None


def _pending_item(root: _Chat, sides: list[_Chat], handles) -> dict | None:
    record = _classify_root(root)
    source_chat = root
    source = "root"
    if record is None:
        for side in sorted(sides, key=lambda chat: -chat.last_ts):
            record = _classify_side(side, root.last_ts)
            if record is not None:
                source_chat, source = side, "side-chat"
                break
    if record is None:
        return None
    status = record["status"]
    item = {
        "kind": "pending",
        "status": status,
        "confidence": STATUS_CONFIDENCE[status],
        "project": root.label,
        "project_label": root.project,
        "agent": root.agent,
        "session": root.session,
        "session_handle": handles.session(root.session),
        "last_ts": root.last_ts,
        "turn": record["turn"],
        "handle": handles.turn(source_chat, record["turn"]),
        "source": source,
        "signals": record["signals"],
        "evidence": record["evidence"][:240],
        "items": record["items"],
        "first_text": root.first_text,
    }
    if source == "side-chat":
        item["evidence_session"] = source_chat.session
    if record["caveats"]:
        item["caveats"] = record["caveats"]
    return item


# --- handles ---------------------------------------------------------------

class _Handles:
    def __init__(self, sessions: list[str]) -> None:
        self.index = common.indexed_session_prefix_candidates(sessions)

    def session(self, session: str) -> str | None:
        return compact.encode_session_handle(session, session_index=self.index)

    def turn(self, chat: _Chat, turn: int) -> str | None:
        row = next((row for row in chat.turns if row.turn == turn), None)
        if row is None:
            return None
        try:
            claim = row.digest or compact.content_digest(row.text)
            return compact.encode_bound_result_handle(
                {"session": chat.session, "turn": turn, "content_digest": claim},
                session_index=self.index)
        except (compact.CompactError, TypeError, ValueError):
            return None


# --- main ------------------------------------------------------------------

def _timezone_label() -> str:
    now = _dt.datetime.now().astimezone()
    offset = now.strftime("%z")
    return f"{now.tzname() or 'local'} (UTC{offset[:3]}:{offset[3:]})"


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()
    ap = _parser()
    args = ap.parse_args(argv)
    blank_filter = surface.filter_value_error(args)
    if blank_filter:
        ap.error(blank_filter)
    args.mode = args.mode or "briefing"
    if args.group and args.mode != "time":
        ap.error("--group applies to `agrep summary time`")
    group = args.group or "day"
    cap_ms = _parse_duration(args.idle_cap)
    if cap_ms is None or cap_ms <= 0:
        ap.error("--idle-cap must be a positive duration like 5m, 20m or 1h")
    if args.max < 0:
        ap.error("--max must be 0 or greater")
    if args.agent:
        args.agent = common.normalize_agent_name(args.agent.lower())
    since_text = args.since or DEFAULT_SINCE
    try:
        since_ms = search._parse_when(since_text)
        until_ms = search._parse_when(args.until) if args.until else None
    except SystemExit:
        return 2
    inverted = surface.window_bounds_error(since_text, since_ms, args.until, until_ms)
    if inverted:
        common.log(inverted)
        return 2

    machine_stdout = bool(args.json or not sys.stdout.isatty())
    if not indexd_runtime.ensure_index(auto=not args.no_auto, quiet=machine_stdout):
        return 2
    common.lap("freshen")
    import explore
    index = explore._session_index()
    side_sessions = frozenset(search.indexed_side_sessions())
    family = _family_metadata(list(index), index)
    agent = (args.agent or "").lower()

    self_policy = None
    if not args.include_self and (args.force_no_self or common.in_agent_context()):
        self_policy = common.calling_self_exclusion(conservative=args.force_no_self)
        if self_policy is None and args.force_no_self and not args.json:
            identity = common.calling_identity()
            if not identity.session:
                common.log("--no-self was not applied: "
                           + common.self_exclusion_unavailable_notice(identity.reason))

    def passes_filters(row: dict) -> bool:
        if agent and agent not in str(row.get("agent") or "").lower():
            return False
        if args.project and not surface.project_label_matches(row.get("project"), args.project):
            return False
        return True

    def overlaps_window(row: dict) -> bool:
        if (row.get("last_ts") or 0) < since_ms:
            return False
        return until_ms is None or (row.get("first_ts") or 0) < until_ms

    def chat_row(session: str, raw: dict, *, root: str, side: bool,
                 agent_fallback: str = "") -> _Chat:
        return _Chat(session=session, agent=str(raw.get("agent") or agent_fallback),
                     project=str(raw.get("project") or ""), root=root, side=side,
                     first_ts=int(raw.get("first_ts") or 0), last_ts=int(raw.get("last_ts") or 0),
                     first_text=common.one_line(raw.get("first_text") or ""))

    candidates: list[tuple[str, bool]] = []
    side_members: dict[str, list[str]] = {}
    for session in index:
        root, side = family.get(session, (session, False))
        side = side or session in side_sessions
        if side and root != session:
            side_members.setdefault(root, []).append(session)
        else:
            candidates.append((session, side))
    # filters judge the family's root; the window admits a family on any member's activity
    chats: dict[str, _Chat] = {}
    roots: dict[str, _Chat] = {}
    sides: dict[str, list[_Chat]] = {}
    for session, side in candidates:
        raw = index[session]
        if not passes_filters(raw):
            continue
        kin = side_members.get(session, ())
        if not (overlaps_window(raw) or any(overlaps_window(index[member]) for member in kin)):
            continue
        chat = chat_row(session, raw, root=session, side=side)
        roots[session] = chats[session] = chat
        for member in kin:
            side_chat = chat_row(member, index[member], root=session, side=True,
                                 agent_fallback=chat.agent)
            chats[member] = side_chat
            sides.setdefault(session, []).append(side_chat)
    common.lap("identity-index", f"{len(roots)} root chats")

    _load_transcripts(chats)
    self_dropped = 0
    if self_policy is not None:
        for session in list(chats):
            chat = chats[session]
            kept = [row for row in chat.turns if not self_policy.excludes(session, row.turn)]
            if len(kept) != len(chat.turns):
                chat.withheld = True
                chat.turns = kept
                kept_turns = {row.turn for row in kept}
                chat.replies = {turn: text for turn, text in chat.replies.items()
                                if turn in kept_turns}
                if not kept:
                    chats.pop(session)
                    if session in roots:
                        roots.pop(session)
                        self_dropped += 1
                        sides.pop(session, None)
                    else:
                        sides[chat.root] = [c for c in sides.get(chat.root, ()) if c is not chat]
                elif session in roots:
                    self_dropped += 1
    for chat in chats.values():
        _load_events(chat)
    common.lap("transcripts", f"{len(chats)} chats")

    # --- time per family, split per local day ---
    unknown_rows = 0
    family_dedup_ms = 0
    family_days: dict[str, dict[str, int]] = {}
    family_ms: dict[str, int] = {}
    for root, chat in roots.items():
        members = [chat, *sides.get(root, ())]
        merged_each = []
        for member in members:
            intervals, unknown = _chat_intervals(member, cap_ms)
            unknown_rows += unknown
            merged_each.append(intervals)
        union = _merge([span for intervals in merged_each for span in intervals])
        family_dedup_ms += sum(_span(intervals) for intervals in merged_each) - _span(union)
        clipped = _clip(union, since_ms, until_ms)
        family_days[root] = _split_days(clipped)
        family_ms[root] = _span(clipped)

    def turns_in_window(chat: _Chat) -> int:
        return sum(1 for row in chat.turns if row.ts > 0 and row.ts >= since_ms
                   and (until_ms is None or row.ts < until_ms))

    handles = _Handles(list(chats))
    pending_items = []
    if args.mode != "time":
        for root, chat in roots.items():
            if chat.withheld:
                continue
            item = _pending_item(chat, sides.get(root, []), handles)
            if item is not None:
                pending_items.append(item)
        pending_items.sort(key=lambda item: (_CONFIDENCE_RANK[item["confidence"]],
                                             -(item["last_ts"] or 0)))
    common.lap("classify", f"{len(pending_items)} pending")

    story = (surface.FreshnessStory("unverified", code="freshness-unchecked",
                                    detail=indexd_runtime.NO_AUTO_REFRESH_REASON)
             if args.no_auto else indexd_runtime.freshness_story())
    totals_exact = explore._session_index_skipped() == 0
    caveats = [f"{METRIC} is estimated from turn timestamps with a {args.idle_cap} idle cap; "
               "it is not elapsed session span and not billable time",
               "side chats are folded into their family; overlapping minutes count once"]
    if unknown_rows:
        caveats.append(f"{surface.count_noun(unknown_rows, 'turn')} without a usable timestamp "
                       "excluded from time and turn counts")
    if not totals_exact:
        caveats.append("the session index skipped corrupt rows; counts are a floor")
    window_text = f"{since_text}" + (f"..{args.until}" if args.until else "")

    rows: list[dict] = []
    if args.mode == "time":
        buckets: dict[tuple[str, str], dict] = {}
        for root, chat in roots.items():
            for day, ms in family_days[root].items():
                key = (_period(day, group), chat.label)
                bucket = buckets.setdefault(key, {"ms": 0, "chats": set()})
                bucket["ms"] += ms
                bucket["chats"].add(root)
        for (period, project), bucket in sorted(buckets.items(),
                                               key=lambda kv: (kv[0][0], -kv[1]["ms"], kv[0][1])):
            rows.append({"kind": "time", "period": period, "group": group, "project": project,
                         "estimated_active_ms": bucket["ms"],
                         "estimated_active": _duration_label(bucket["ms"]),
                         "chats": len(bucket["chats"])})
    elif args.mode == "pending":
        rows = pending_items
    else:
        projects: dict[str, dict] = {}
        for root, chat in roots.items():
            entry = projects.setdefault(chat.label, {
                "kind": "project", "project": chat.label, "project_labels": [],
                "estimated_active_ms": 0, "chats": 0, "turns": 0, "last_ts": 0,
                "worked_on": [], "open": []})
            if chat.project not in entry["project_labels"]:
                entry["project_labels"].append(chat.project)
            entry["estimated_active_ms"] += family_ms[root]
            entry["chats"] += 1
            entry["turns"] += turns_in_window(chat)
            entry["last_ts"] = max(entry["last_ts"], chat.last_ts)
            last = chat.last_turn()
            entry["worked_on"].append({
                "session": root, "session_handle": handles.session(root),
                "agent": chat.agent, "project_label": chat.project,
                "first_text": chat.first_text,
                "last_ts": chat.last_ts, "turns": turns_in_window(chat),
                "estimated_active_ms": family_ms[root],
                "latest_handle": handles.turn(chat, last.turn) if last else None,
                "side_chats": len(sides.get(root, ())),
                **({"self": True} if chat.withheld else {})})
        for item in pending_items:
            projects[item["project"]]["open"].append(item)
        for entry in projects.values():
            entry["estimated_active"] = _duration_label(entry["estimated_active_ms"])
            entry["last_age"] = common.age_label(entry["last_ts"])
            entry["worked_on"].sort(key=lambda row: -(row["last_ts"] or 0))
            if args.max:
                entry["worked_on"] = entry["worked_on"][:args.max]
        rows = sorted(projects.values(),
                      key=lambda entry: (-entry["estimated_active_ms"], -entry["last_ts"],
                                         entry["project"]))
    common.lap("render-prep")

    if args.json:
        publication_converging = indexd_runtime.foreground_refresh_converging(
            checked=not args.no_auto)
        freshness = indexd_runtime.machine_freshness(
            checked=not args.no_auto, publication_converging=publication_converging)
        machine_fields = search._load_corpusdb().machine_freshness_fields(
            freshness, publication_converging=publication_converging)
        fields = dict(machine_fields)
        fields["freshness"] = surface.row_freshness_disclosure(
            machine_fields["freshness"], first=True)
        meta = {
            "kind": "agrep-meta", "mode": args.mode,
            "window": {"since": since_text, "since_ts": since_ms,
                       "until": args.until, "until_ts": until_ms},
            "timezone": _timezone_label(),
            "idle_cap": args.idle_cap, "idle_cap_ms": cap_ms,
            "metric": METRIC,
            "chats": len(roots), "side_chats": sum(len(v) for v in sides.values()),
            "unknown_timestamp_rows": unknown_rows,
            "family_dedup_ms": family_dedup_ms,
            "self_excluded": self_dropped,
            "totals_exact": totals_exact,
            "caveats": caveats,
            **fields,
        }
        if args.mode == "time":
            meta["group"] = group
            meta["total_estimated_active_ms"] = sum(family_ms.values())
        if not rows:
            meta["hits"] = []
        print(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    elif rows:
        _render(args, rows, group=group, window_text=window_text, total_ms=sum(family_ms.values()))
    if rows:
        sys.stdout.flush()
    if not args.json:
        if args.mode == "time":
            common.log(f"{METRIC} for {surface.count_noun(len(roots), 'chat')} in the last "
                       f"{window_text}, idle cap {args.idle_cap}, local time {_timezone_label()}"
                       f" · not elapsed span, not billable")
        elif args.mode == "pending":
            common.log(f"{surface.count_noun(len(rows), 'open item')} across "
                       f"{surface.count_noun(len(roots), 'chat')} in the last {window_text}, "
                       "most confident first")
        else:
            common.log(f"{surface.count_noun(len(rows), 'project')}, "
                       f"{surface.count_noun(len(roots), 'chat')} in the last {window_text}, "
                       f"most active first · {METRIC} with a {args.idle_cap} idle cap, "
                       "side chats folded into their family")
        if unknown_rows:
            common.log(f"{surface.count_noun(unknown_rows, 'turn')} without a usable timestamp "
                       "excluded from time and turn counts")
        if family_dedup_ms:
            common.log(f"overlap between chats and their side chats counted once "
                       f"({_duration_label(family_dedup_ms)} de-duplicated)")
        if self_policy is not None and self_dropped:
            common.log(surface.self_exclusion_notice(
                resolved=self_policy.family.resolved, dropped=self_dropped,
                windowed=self_policy.windowed, noun="chat"))
        notice = search.escalated_freshness_notice(surface.freshness_story_line(story))
        if notice:
            common.log(notice)
        search._note_family_index_behind()
    if rows:
        return 0
    return surface.grep_absence_exit(exact=totals_exact, freshness=story)


def _render(args, rows: list[dict], *, group: str, window_text: str, total_ms: int) -> None:
    color = common.color_enabled(sys.stdout, args.color)
    palette = surface.PALETTE
    safe = common.terminal_safe

    def head(text: str) -> str:
        return f"{palette['hd']}{text}{palette['r']}" if color else text

    def dim(text: str) -> str:
        return f"{palette['d']}{text}{palette['r']}" if color else text

    def pending_line(item: dict, indent: str = "") -> str:
        handle = item.get("handle") or item.get("session_handle") or f"session={item['session']}"
        if item["items"]:
            detail = f"{len(item['items'])} open: " + "; ".join(item["items"][:3])
            if len(item["items"]) > 3:
                detail += f"; +{len(item['items']) - 3} more"
        elif item["status"] in ("waiting_on_user", "unknown") and item["evidence"]:
            detail = f"\u201c{item['evidence'][:120]}\u201d"
        else:
            detail = "; ".join(item["signals"])
        source = "  [side chat]" if item["source"] == "side-chat" else ""
        return (f"{indent}{item['confidence'].upper():<6} {item['status']:<21} "
                f"{safe(handle)}{source}  {safe(detail)}")

    if args.mode == "time":
        label_width = max(len("project"), *(len(row["project"]) for row in rows))
        period_width = max(len(group), *(len(row["period"]) for row in rows))
        print(head(f"{group:<{period_width}}  {'project':<{label_width}}  {'time':>7}  chats"))
        for row in rows:
            print(f"{row['period']:<{period_width}}  {safe(row['project']):<{label_width}}  "
                  f"{row['estimated_active']:>7}  {row['chats']}")
        print(dim(f"{'total':<{period_width}}  {'':<{label_width}}  "
                  f"{_duration_label(total_ms):>7}"))
        return
    if args.mode == "pending":
        for item in rows:
            print(pending_line(item) + f"  {dim(safe(item['project']))}")
        return
    for entry in rows:
        print(head(f"{safe(entry['project'])}") + dim(
            f"  ·  {METRIC} {entry['estimated_active']}  ·  "
            f"{surface.count_noun(entry['chats'], 'chat')}  ·  "
            f"{surface.count_noun(entry['turns'], 'turn')}  ·  last {entry['last_age']}"))
        print("  worked on:")
        for chat in entry["worked_on"]:
            handle = chat["session_handle"] or f"session={chat['session']}"
            marks = (" ~self" if chat.get("self") else "") + (
                f" (+{chat['side_chats']} side)" if chat["side_chats"] else "")
            followup = (console.shell_command("agrep", "around", chat["latest_handle"], fallback="")
                        if chat["latest_handle"] else "")
            tail = f" · {followup}" if followup else ""
            print(f"    {safe(handle)} {safe(chat['agent'])} {common.age_label(chat['last_ts'])} "
                  f"{chat['turns']}t{marks}  {dim(safe(chat['first_text'])[:96] + tail)}")
        if entry["open"]:
            print("  open:")
            for item in entry["open"]:
                print(pending_line(item, indent="    "))


if __name__ == "__main__":
    sys.exit(main())
