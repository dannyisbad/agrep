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
its todo list (whole-list writes, omp's ops replayed, codex plans), and reports
a status with a stated confidence. Explicit completion always wins over an
open-looking bullet. A compaction recap is never a turn of its own, and a side
chat that handed its result back (a terminal yield, or a delegation result that
carries its reply) never reopens a finished family.
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
# the span DEFAULT_SINCE names; --until alone anchors it at the window end instead of now
_DEFAULT_SPAN_MS = 7 * 86_400_000
_RELATIVE_WHEN_RE = re.compile(r"\d+\s*[a-z]+", re.IGNORECASE)
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
    r"^\s*(?:#+\s*)?(?:\*\*|__)?\s*(next steps?|remaining(?: work| items?| tasks?| steps?)?|"
    r"follow[- ]?ups?|to-?dos?|open items?|outstanding(?: work| items?| tasks?)?|still to do|"
    r"left to do|what'?s left)\s*:?\s*(?:\*\*|__)?\s*:?\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$")
_CHECKED_RE = re.compile(r"^\[[xX✓✔]\]")
_UNCHECKED_RE = re.compile(r"^\[\s\]")
_ITEM_DONE_RE = re.compile(
    r"(?:^~~.*~~$)|(?:\((?:done|completed|cancelled|canceled|skipped)\)\s*$)|"
    r"(?:[-:—]\s*(?:done|completed|cancelled|canceled|skipped)\.?\s*$)", re.IGNORECASE)
# a bullet that says the list is empty: "- None", "- None for now", "- N/A", "- Nothing else"
_NO_ITEM_RE = re.compile(
    r"^(?:(?:none|nothing)(?: else| more| further| remaining| left| open| outstanding| pending|"
    r" for now| so far| at (?:the|this) (?:moment|time|point)| at present)?|n/?a|all done|"
    r"no(?:thing)? (?:open|remaining|outstanding|further|more|pending)\b.*)\.?$", re.IGNORECASE)
# a closing courtesy after a list offers more help; any other prose after the list supersedes it
_SIGN_OFF_RE = re.compile(
    r"\b(let me know|tell me|say the word|happy to|glad to|feel free|if you(?:'d| would)? "
    r"(?:like|want|prefer|need)|want me to|i can (?:also|then|take)|just (?:say|ask)|shout|"
    r"ping me)\b", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")
# claude TodoWrite/TodoRead, opencode todowrite/todoread, omp todo, codex update_plan
_TODO_TOOL_RE = re.compile(r"todo|update_plan", re.IGNORECASE)
_TODO_OPEN_STATUSES = frozenset({
    "pending", "in_progress", "in-progress", "not_started", "not-started", "open",
    "todo", "active", "blocked", "queued"})
_TODO_CLOSED_STATUSES = frozenset({
    "completed", "complete", "done", "cancelled", "canceled", "skipped", "closed", "abandoned"})
# omp's op-based todo tool; a missing op is inferred the way omp infers it
_TODO_OPS = frozenset({"init", "append", "start", "done", "drop", "block", "unblock", "rm", "view"})
_TODO_DEFAULT_PHASE = "Tasks"
_TODO_OP_RE = re.compile(r'"op"\s*:\s*"([a-z_]+)"')
_TODO_PHASE_RE = re.compile(r'"phase"\s*:\s*"((?:[^"\\]|\\.)*)"')
# root tools that run a delegated agent and return its result: omp/pi/opencode task, codex collab
_DELEGATION_TOOL_RE = re.compile(r"^(?:task|agent|subagent|spawn_agent|wait_agent)$", re.IGNORECASE)
# the omp task result envelope; the delegate's own words sit inside output/preview
_TASK_RESULT_BODY_RE = re.compile(r"<(?:output|preview)[^>]*>(.*?)(?:</(?:output|preview)>|$)",
                                  re.DOTALL)
_YIELD_SECTION_RE = re.compile(r'"type"\s*:\s*\[')
_CONTROL_STOP_RE = re.compile(r"interrupt|abort|cancel|stop", re.IGNORECASE)
# Claude's synthetic rows (isApiErrorMessage, model "<synthetic>") end a turn's reply; the ingest
# keeps them as prose joined by one space, so the reply's ending identifies them
_API_ERROR_RE = re.compile(
    r"(?:^|[.!?:)\]`]\s+|\n\s*)(API Error: .+|You've hit your .*?limit\b.*|"
    r"Claude AI usage limit reached\|\S*|"
    r"Prompt is too long(?: ·.*)?|Input is too long for requested model.*|"
    r"Context limit reached ·.*|Request timed out\b.*|Unable to connect to API\b.*|"
    r"Credit balance is too low\b.*|Server is temporarily limiting requests\b.*)\s*$")
# tools that stop the turn until the human answers: claude AskUserQuestion/ExitPlanMode, omp ask,
# codex request_user_input, opencode question
_QUESTION_TOOL_RE = re.compile(
    r"^(?:AskUserQuestion|ExitPlanMode|ask|request_user_input|question)$", re.IGNORECASE)


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
    # compaction moments: activity for the time estimate, never a turn
    recap_ts: list[int] = field(default_factory=list)
    # turns the caller's live window withholds; the chat's state after them is not history
    withheld: bool = False
    # the unindexed parent of a side chat promoted to a family root of its own
    parent: str = ""

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
                    help=f"window start (7d / 24h / 2w / 30m, or 2026-06-01); default "
                         f"{DEFAULT_SINCE} before now, or before --until when only --until is given")
    ap.add_argument("--until", "--before", dest="until", metavar="WHEN",
                    help=f"window end (same formats as --since); alone, the window is the "
                         f"{DEFAULT_SINCE} before it")
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


def _load_transcripts(chats: dict[str, _Chat], excludes=None) -> bool:
    """Fill turns and replies from the search db, else from the materialized JSONL. A db behind
    sessions.jsonl is skipped whole so one chat never mixes generations. `excludes(session,
    turn)` withholds rows before recaps fold, so a withheld recap is never activity or a reply."""
    sessions = list(chats)
    try:
        db = search._load_corpusdb().connect(allow_stale=True)
    except (OSError, sqlite3.DatabaseError, RuntimeError, TypeError, ValueError):
        db = None
    if db is not None and getattr(db, "_source_stamp_current", None) is False:
        db.close()
        db = None
    if db is not None:
        seen: dict[str, set[int]] = {session: set() for session in sessions}
        orphan_ts: dict[tuple[str, int], int] = {}
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
                        orphan_ts.setdefault((session, turn), int(ts or 0))
                        continue
                    if turn in seen[session]:
                        continue
                    seen[session].add(turn)
                    chat.turns.append(_Turn(turn, int(ts or 0), str(who or "user"),
                                            str(text or ""), digest))
            # the db keeps no empty-text row, so a codex compaction survives only as the reply
            # written after it, filed under the recap's turn: that turn is the recap
            for (session, turn), ts in orphan_ts.items():
                if turn not in seen[session]:
                    seen[session].add(turn)
                    chats[session].turns.append(_Turn(turn, ts, "recap", "", None))
            for chat in chats.values():
                _withhold(chat, excludes)
                _fold_recaps(chat)
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
        _withhold(chat, excludes)
        _fold_recaps(chat)
    return False


def _withhold(chat: _Chat, excludes) -> None:
    if excludes is None:
        return
    kept = [row for row in chat.turns if not excludes(chat.session, row.turn)]
    if len(kept) == len(chat.turns):
        return
    chat.withheld = True
    chat.turns = kept
    kept_turns = {row.turn for row in kept}
    chat.replies = {turn: text for turn, text in chat.replies.items() if turn in kept_turns}


def _fold_recaps(chat: _Chat) -> None:
    """A compaction recap is not a prompt: its timestamp is kept as activity, and the reply the
    adapters attached to it continues the prompt before it. A recap that opens a chat stays."""
    kept: list[_Turn] = []
    for row in sorted(chat.turns, key=lambda row: (row.turn, row.ts)):
        if row.who != "recap" or not kept:
            kept.append(row)
            continue
        if row.ts > 0:
            chat.recap_ts.append(row.ts)
        carried = chat.replies.pop(row.turn, "")
        if carried:
            prior = kept[-1].turn
            chat.replies[prior] = "\n\n".join(
                part for part in (chat.replies.get(prior, ""), carried) if part)
    chat.turns = kept


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
    points = sorted({row.ts for row in chat.turns if row.ts > 0} | set(chat.recap_ts))
    unknown = sum(1 for row in chat.turns if row.ts <= 0)
    if not points:
        return [], unknown
    intervals = [(start, min(following, start + cap_ms))
                 for start, following in zip(points, points[1:])]
    last = points[-1]
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
    """(heading, open items, closed count) for a next-steps style section that ends the reply.
    A turn's reply joins every assistant text block, so prose after the bullets is a later
    block that supersedes the list; only a short sign-off may follow it."""
    lines = _prose(reply).splitlines()
    heading_at = next((i for i in range(len(lines) - 1, -1, -1) if _SECTION_RE.match(lines[i])),
                      None)
    if heading_at is None:
        return None
    heading = _SECTION_RE.match(lines[heading_at]).group(1).lower()
    bullets: list[str] = []
    trailing: list[str] = []
    for candidate in lines[heading_at + 1:]:
        if not candidate.strip():
            continue
        bullet = _BULLET_RE.match(candidate)
        if bullet is not None and not trailing:
            bullets.append(bullet.group(1).strip())
        elif bullets:
            trailing.append(candidate.strip())
    if not bullets:
        return None
    # the ingest joins blocks with one space, so a later block glued to an unpunctuated last
    # bullet reads as part of it; a newline join would change stored replies and their handles
    head, *rest = _SENTENCE_END_RE.split(bullets[-1], maxsplit=1)
    bullets[-1] = head
    tail = " ".join(rest + trailing)
    if tail and not (len(tail) <= 200 and _SIGN_OFF_RE.search(tail)):
        return None
    open_items: list[str] = []
    closed = 0
    for item in bullets:
        if _CHECKED_RE.match(item) or _ITEM_DONE_RE.search(item):
            closed += 1
            continue
        item = common.one_line(_MARKUP_RE.sub(" ", _UNCHECKED_RE.sub("", item).strip())).strip()
        if item and not _NO_ITEM_RE.match(item):
            open_items.append(item)
    return heading, open_items, closed


def _input_capped(event: dict) -> bool:
    """Did the ingest cap cut this event's input? The row keeps the source length to say so."""
    raw = str(event.get("input") or "")
    try:
        chars = int(event.get("input_chars") or 0)
    except (TypeError, ValueError):
        return False
    return raw.endswith("…") and chars >= len(raw)


def _capped_list_items(raw: str, key: str) -> list:
    """Every element still complete in the `key` list of a JSON input the ingest cap cut short."""
    decoder = json.JSONDecoder()
    at = raw.find(f'"{key}"')
    pos = raw.find("[", at) if at >= 0 else -1
    if pos < 0:
        return []
    out: list = []
    pos += 1
    while True:
        while pos < len(raw) and raw[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(raw) or raw[pos] not in "{\"":
            return out
        try:
            item, pos = decoder.raw_decode(raw, pos)
        except ValueError:
            return out
        out.append(item)


def _todo_entry(item: object) -> tuple[str, str] | None:
    """(content, status) of one todo/plan element: a string, or a dict naming the item."""
    if isinstance(item, str):
        return (common.one_line(item), "open") if item.strip() else None
    if not isinstance(item, dict):
        return None
    name = next((str(item[key]) for key in ("content", "title", "task", "step", "text", "name")
                 if isinstance(item.get(key), str) and item[key].strip()), "")
    status = str(item.get("status") or item.get("state") or "").strip().lower()
    return (common.one_line(name), status) if name else None


def _snapshot_tasks(payload: object, raw: str, capped: bool) -> list[list[str]] | None:
    """[phase, content, status] rows from a whole-list todo write: claude/opencode `todos`,
    codex `plan`, or a bare list. None when the payload is not that shape."""
    key = next((k for k in ("todos", "plan") if isinstance(payload, dict) and k in payload), None)
    if capped:
        key = next((k for k in ("todos", "plan") if f'"{k}"' in raw), None)
        if key is None:
            return None
        items: object = _capped_list_items(raw, key)
    elif key is not None:
        items = payload[key]
    elif isinstance(payload, list):
        items = payload
    else:
        return None
    if not isinstance(items, list):
        return None
    rows = []
    for item in items:
        entry = _todo_entry(item)
        if entry is not None:
            rows.append(["", entry[0], entry[1]])
    return rows


def _todo_targets(tasks: list[list[str]], payload: dict) -> list[list[str]] | None:
    """The rows an omp op addresses: one task by verbatim content, one phase, or every task.
    None when the named task or phase does not exist (omp rejects the op)."""
    task, phase = payload.get("task"), payload.get("phase")
    if isinstance(task, str) and task:
        task = common.one_line(task)
        return [row for row in tasks if row[1] == task] or None
    if isinstance(phase, str) and phase:
        return [row for row in tasks if row[0] == phase] or None
    return tasks


def _todo_op(payload: dict, tasks: list[list[str]]) -> str | None:
    """The op an omp todo call names, or the one omp infers when it is missing."""
    op = payload.get("op")
    if isinstance(op, str):
        return op
    items = payload.get("items") if isinstance(payload.get("items"), list) else None
    if isinstance(payload.get("list"), list) and payload["list"]:
        return "init"
    if items and isinstance(payload.get("phase"), str) and payload["phase"]:
        return "append"
    if items and not tasks:
        return "init"
    return None


def _apply_todo_op(tasks: list[list[str]], payload: dict) -> list[list[str]] | None:
    """omp's todo ops replayed over [phase, content, status] rows; None when the op is one
    omp would have rejected, so the state is unchanged."""
    op = _todo_op(payload, tasks)
    items = payload.get("items") if isinstance(payload.get("items"), list) else None
    phases = payload.get("list") if isinstance(payload.get("list"), list) else None
    if op not in _TODO_OPS:
        return None
    if op == "init":
        if phases is None:
            if not items:
                return None
            phases = [{"phase": payload.get("phase") or _TODO_DEFAULT_PHASE, "items": items}]
        return [[str(phase.get("phase") or _TODO_DEFAULT_PHASE), common.one_line(item), "pending"]
                for phase in phases if isinstance(phase, dict)
                for item in (phase.get("items") or []) if isinstance(item, str) and item.strip()]
    if op == "append":
        phase = payload.get("phase")
        if not items or not isinstance(phase, str) or not phase:
            return None
        fresh = [common.one_line(item) for item in items if isinstance(item, str) and item.strip()]
        if any(row[1] == item for row in tasks for item in fresh):
            return None
        return tasks + [[phase, item, "pending"] for item in fresh]
    if op == "view":
        return tasks
    if op == "rm":
        targets = _todo_targets(tasks, payload)
        if targets is None:
            return None
        gone = {id(row) for row in targets}
        return [row for row in tasks if id(row) not in gone]
    if op in ("block", "unblock") and not (payload.get("task") or payload.get("phase")):
        return None
    if op == "start" and not payload.get("task"):
        return None
    targets = _todo_targets(tasks, payload)
    if targets is None:
        return None
    out = [list(row) for row in tasks]
    hit = {id(row) for row in targets}
    for row, fresh in zip(tasks, out):
        if op == "start" and fresh[2] == "in_progress":
            fresh[2] = "pending"
        if id(row) not in hit:
            continue
        if op == "start":
            fresh[2] = "in_progress"
        elif op == "done":
            fresh[2] = "completed"
        elif op == "drop":
            fresh[2] = "abandoned"
        elif op == "block" and fresh[2] in ("pending", "in_progress", "blocked"):
            fresh[2] = "blocked"
        elif op == "unblock" and fresh[2] == "blocked":
            fresh[2] = "pending"
    return out


def _capped_todo_op(raw: str, tasks: list[list[str]]) -> str | None:
    """The op of a todo call whose input the ingest cap cut, read from the text that survived."""
    match = _TODO_OP_RE.search(raw)
    if match:
        return match.group(1)
    if '"list"' in raw:
        return "init"
    if '"items"' in raw:
        return "append" if _TODO_PHASE_RE.search(raw) else ("init" if not tasks else None)
    return None


def _apply_capped_todo_op(tasks: list[list[str]], raw: str,
                          op: str | None) -> list[list[str]] | None:
    """A capped op replayed from the list elements still complete: only init and append can be;
    anything else leaves the state unknowable."""
    if op == "init":
        payload: dict = {"op": "init"}
        if '"list"' in raw:
            payload["list"] = _capped_list_items(raw, "list")
        else:
            payload["items"] = _capped_list_items(raw, "items")
        return _apply_todo_op([], payload)
    if op == "append":
        phase = _TODO_PHASE_RE.search(raw)
        items = [item for item in _capped_list_items(raw, "items")
                 if isinstance(item, str) and not any(row[1] == item for row in tasks)]
        return _apply_todo_op(tasks, {"op": "append", "items": items,
                                      "phase": phase.group(1) if phase else _TODO_DEFAULT_PHASE})
    return None


_TODO_UNKNOWN_SHAPE = "todo list captured in an unknown shape"
_TODO_UNREPLAYABLE = "todo list uses an op this build cannot replay"
_TODO_CAP_DRIFT = "todo list capped at index time; its later changes could not be replayed"


def _todo_state(chat: _Chat) -> tuple[list[tuple[str, str]] | None, bool, bool, str]:
    """(items after the last todo write, present, capped, why unknowable). Whole-list writes
    replace the state; omp ops replay in order. An op omp rejected (`ok` false) left its state
    alone; any other op the replay cannot apply is drift, so the state is unknown from there."""
    tasks: list[list[str]] = []
    present = capped = False
    unknown = ""
    for event in chat.events:
        if event.get("kind") != "tool" or not _TODO_TOOL_RE.search(str(event.get("name") or "")):
            continue
        present = True
        if event.get("ok") is False:
            continue
        raw = str(event.get("input") or "")
        cut = _input_capped(event)
        try:
            payload: object = json.loads(raw) if raw else {}
        except ValueError:
            if not cut:
                unknown = _TODO_UNKNOWN_SHAPE
                continue
            payload = None
        snapshot = _snapshot_tasks(payload, raw, cut)
        if snapshot is not None:
            tasks, capped, unknown = snapshot, cut, ""
            continue
        if payload == {}:
            continue
        if payload is None:
            op = _capped_todo_op(raw, tasks)
            applied = _apply_capped_todo_op(tasks, raw, op)
            if applied is None:
                unknown = _TODO_CAP_DRIFT
                continue
            tasks, capped = applied, True
            if op == "init":
                unknown = ""
            continue
        if not isinstance(payload, dict) or not any(
                key in payload for key in ("op", "list", "items", "task", "phase")):
            unknown = _TODO_UNKNOWN_SHAPE
            continue
        op = _todo_op(payload, tasks)
        applied = _apply_todo_op(tasks, payload)
        if applied is None:
            unknown = _TODO_CAP_DRIFT if capped else _TODO_UNREPLAYABLE
            continue
        tasks = applied
        if op == "init":
            capped, unknown = False, ""
    if unknown:
        return None, present, capped, unknown
    return [(content, status) for _phase, content, status in tasks], present, capped, ""


def _open_todos(chat: _Chat) -> tuple[list[str], list[str]]:
    """(open todo items, caveats). A capped list whose kept items are all closed is no evidence:
    the final reply decides."""
    items, present, capped, reason = _todo_state(chat)
    if not present:
        return [], []
    if items is None:
        return [], [reason]
    open_items = [name for name, status in items
                  if status in _TODO_OPEN_STATUSES or
                  (status not in _TODO_CLOSED_STATUSES and status == "")]
    if open_items and capped:
        return open_items, ["todo list capped at index time; items past the cap were not seen"]
    return open_items, []


def _events_after(chat: _Chat, ts: int) -> list[dict]:
    return [event for event in chat.events if int(event.get("ts") or 0) >= ts]


def _question_head(event: dict) -> str:
    """The question or plan a parked question tool put to the human, from its captured input."""
    raw = str(event.get("input") or "")
    try:
        payload = json.loads(raw)
    except ValueError:
        return common.one_line(raw)
    if isinstance(payload, dict):
        questions = payload.get("questions")
        if isinstance(questions, list):
            for item in questions:
                if isinstance(item, dict) and isinstance(item.get("question"), str):
                    return common.one_line(item["question"])
        for key in ("question", "plan", "prompt", "message"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return common.one_line(payload[key])
    return common.one_line(raw)


def _unanswered_question(chat: _Chat, last: _Turn) -> dict | None:
    """The question tool the last turn is parked on: its latest event, still without a result."""
    later = _events_after(chat, last.ts) if last.ts > 0 else list(chat.events)
    tools = [event for event in later if event.get("kind") == "tool"]
    if not tools:
        return None
    event = max(tools, key=lambda e: (int(e.get("ts") or 0), int(e.get("i") or 0)))
    if (_QUESTION_TOOL_RE.match(str(event.get("name") or "")) and event.get("ok") is None
            and not str(event.get("output") or "").strip()):
        return event
    return None


def _api_error(reply: str) -> str | None:
    """The synthetic API-error or usage-limit text a Claude turn ended on, or None."""
    match = _API_ERROR_RE.search(reply)
    return common.one_line(match.group(1)) if match else None


def _classify_root(chat: _Chat) -> dict | None:
    last = chat.last_turn()
    if last is None:
        return None
    reply = chat.replies.get(last.turn, "")
    record = {"turn": last.turn, "turn_ts": last.ts, "signals": [], "evidence": "",
              "items": [], "caveats": []}
    question = _unanswered_question(chat, last)
    if question is not None:
        record.update(status="waiting_on_user",
                      signals=[f"{question.get('name')} awaits your answer"],
                      evidence=_question_head(question))
        return record
    error = _api_error(reply) if reply else None
    if error is not None:
        record.update(status="agent_work_incomplete",
                      signals=["final reply is an API error"], evidence=error)
        return record
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


def _latest_ts(chat: _Chat) -> int:
    """The latest moment the index places in a chat: its last prompt, latest tool call or
    compaction. Replies carry no timestamp of their own."""
    last = chat.last_turn()
    return max(last.ts if last is not None else 0, *chat.recap_ts,
               *(int(event.get("ts") or 0) for event in chat.events), 0)


def _side_yielded(side: _Chat) -> bool:
    """Did the side chat end by submitting its result? pi/omp subagents hand back through a
    terminal `yield` call and often write no closing text; a `type` list marks an incremental
    section, which is not the hand-back."""
    if not side.events:
        return False
    event = max(side.events, key=lambda e: (int(e.get("ts") or 0), int(e.get("i") or 0)))
    if (event.get("kind") != "tool" or str(event.get("name") or "").lower() != "yield"
            or event.get("ok") is False):
        return False
    last = side.last_turn()
    if last is not None and last.ts > int(event.get("ts") or 0):
        return False
    raw = str(event.get("input") or "")
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        return _YIELD_SECTION_RE.search(raw) is None
    return not (isinstance(payload, dict) and isinstance(payload.get("type"), list))


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _json_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _json_strings(v)]
    return []


def _delegate_words(payload: object) -> list[str]:
    """The string leaves of a delegation result that are the delegate's own words. A codex wait
    reports per-agent status: only a `completed` entry carries the final message, and a wait
    that timed out delivered nothing."""
    if isinstance(payload, dict) and isinstance(payload.get("status"), dict):
        if payload.get("timed_out") is True:
            return []
        return [s for state in payload["status"].values() if isinstance(state, dict)
                for s in _json_strings(state.get("completed"))]
    return _json_strings(payload)


# a fragment this short ("running", an id) occurs in replies that were never handed back; only
# a candidate equal to the whole reply proves receipt at any length
_DELIVERY_MIN_CHARS = 24


def _delivers(output: str, final: str) -> bool:
    """Does a delegation result carry the side chat's final reply? Claude and opencode return
    the reply itself; omp wraps it in a task-result envelope; codex nests it in JSON."""
    text = " ".join(output.rstrip("…").split())
    body = _TASK_RESULT_BODY_RE.search(text) if text.startswith("<task-result") else None
    if body is not None:
        text = body.group(1).strip()
    candidates = [text]
    if text[:1] in ("{", "["):
        try:
            candidates += [" ".join(s.split()) for s in _delegate_words(json.loads(text))]
        except ValueError:
            pass
    reply_head = final[:160]
    for candidate in candidates:
        if candidate and candidate == final:
            return True
        head = candidate[:160]
        if len(head) >= _DELIVERY_MIN_CHARS and head in final:
            return True
        if len(reply_head) >= _DELIVERY_MIN_CHARS and reply_head in candidate:
            return True
    return False


def _result_received(root: _Chat, side: _Chat) -> bool | None:
    """Did the side chat hand its result back? A terminal `yield` is the hand-back itself; else
    the root's delegation result must carry the side chat's final reply, the only indexed proof
    the root went on after the side chat ended. None when the capped side reply cannot be compared."""
    if _side_yielded(side):
        return True
    last = side.last_turn()
    reply = side.replies.get(last.turn, "") if last is not None else ""
    if not reply:
        return False
    if len(reply) >= _REPLY_CAP_CHARS:
        return None
    final = " ".join(reply.split())
    for event in root.events:
        if event.get("kind") != "subagent_start" and not (
                event.get("kind") == "tool"
                and _DELEGATION_TOOL_RE.match(str(event.get("name") or ""))):
            continue
        if _delivers(str(event.get("output") or ""), final):
            return True
    return False


def _classify_side(chat: _Chat, root: _Chat) -> dict | None:
    """Side-chat evidence rolls up only when the side chat is provably the family's latest
    activity: after every timestamp the index holds for the root, with no result handed back."""
    last = chat.last_turn()
    if last is None or last.ts <= 0 or _latest_ts(chat) <= _latest_ts(root):
        return None
    if _result_received(root, chat) is not False:
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
    error = _api_error(chat.replies[last.turn])
    if error is not None:
        record.update(status="agent_work_incomplete",
                      signals=["side chat's final reply is an API error"], evidence=error)
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
        for side in sorted(sides, key=lambda chat: -_latest_ts(chat)):
            record = _classify_side(side, root)
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
    if root.parent:
        item.update(parent=root.parent, parent_indexed=False)
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
        until_ms = search._parse_when(args.until) if args.until else None
        if args.since is None and until_ms is not None:
            since_ms = until_ms - _DEFAULT_SPAN_MS
        else:
            since_ms = search._parse_when(since_text)
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
    # a side chat whose family root is not indexed heads a family of its own, parent disclosed
    orphan_parent: dict[str, str] = {}
    for session in index:
        root, side = family.get(session, (session, False))
        side = side or session in side_sessions
        if side and root != session and root in index:
            side_members.setdefault(root, []).append(session)
        else:
            candidates.append((session, side))
            if side and root != session:
                orphan_parent[session] = str(index[session].get("parent") or root)
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
        chat.parent = orphan_parent.get(session, "")
        roots[session] = chats[session] = chat
        for member in kin:
            side_chat = chat_row(member, index[member], root=session, side=True,
                                 agent_fallback=chat.agent)
            chats[member] = side_chat
            sides.setdefault(session, []).append(side_chat)
    common.lap("identity-index", f"{len(roots)} root chats")

    _load_transcripts(chats, self_policy.excludes if self_policy is not None else None)
    self_dropped = 0
    for session in list(chats):
        chat = chats[session]
        if not chat.withheld:
            continue
        if not chat.turns:
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
    if args.until and args.since is None:
        window_text = f"in the {DEFAULT_SINCE} before {args.until}"
    elif args.until:
        window_text = f"from {since_text} to {args.until}"
    elif _RELATIVE_WHEN_RE.fullmatch(since_text.strip()):
        window_text = f"in the last {since_text}"
    else:
        window_text = f"since {since_text}"

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
                **({"self": True} if chat.withheld else {}),
                **({"parent": chat.parent, "parent_indexed": False} if chat.parent else {})})
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
            common.log(f"{METRIC} for {surface.count_noun(len(roots), 'chat')} {window_text}, "
                       f"idle cap {args.idle_cap}, local time {_timezone_label()}"
                       f" · not elapsed span, not billable")
        elif args.mode == "pending":
            common.log(f"{surface.count_noun(len(rows), 'open item')} across "
                       f"{surface.count_noun(len(roots), 'chat')} {window_text}, "
                       "most confident first")
        else:
            common.log(f"{surface.count_noun(len(rows), 'project')}, "
                       f"{surface.count_noun(len(roots), 'chat')} {window_text}, "
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
        source = ("  [side chat]" if item["source"] == "side-chat" else
                  "  [side chat; parent not indexed]" if item.get("parent_indexed") is False
                  else "")
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
                f" (+{chat['side_chats']} side)" if chat["side_chats"] else "") + (
                " [side chat; parent not indexed]" if chat.get("parent_indexed") is False else "")
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
