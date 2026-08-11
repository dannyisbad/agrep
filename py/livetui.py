"""agrep board: the bounded live-activity window on one screen.

The same LiveWatcher `agrep tail` rides, rendered as a
one-screen TUI: every session across each supported agent, with real state (thinking,
which tool is running, queued prompts), age, model, and the last few events.
Zero config - sessions appear the moment any agent works, including ones
started in other terminals, and the subagents they spawn.

Keys: ↑↓ select · enter focus that session's feed (↑↓ scrolls it, esc back)
· a cycle agent filter · c compact/expanded · 1-9 jump · q quit

  agrep board                 # interactive board
  agrep board --agent claude  # start filtered to one agent
  agrep board --session 96c8  # focus a session id or pasted result handle
  agrep board --once          # print one live-window frame and exit
  agrep board --once --json --sort updated --state active --roots -n 3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import common
import compact as compact_handles
import console
import dist
from hookless import live, registry
import indexd_runtime
import settings
import surface_policy as surface

BOLD, DIM, RESET = (
    surface.PALETTE["bold"], surface.PALETTE["d"], surface.PALETTE["r"])
GREEN, CYAN, YELLOW = (
    surface.PALETTE["g"], surface.PALETTE["a"], surface.PALETTE["y"])
_ONESHOT_BOOT_TIMEOUT_S = 0.18
_ONESHOT_RETRY_WAIT_S = 5.0
_MAX_ONESHOT_WAIT_S = 10.0
_MACHINE_DEFAULT_ROWS = 20
_MACHINE_MAX_ROWS = 100

if os.name == "nt":
    import msvcrt

    _NT_ARROWS = {"H": "up", "P": "down", "M": "right", "K": "left"}

    def _getkey() -> str:
        if not msvcrt.kbhit():
            return ""
        k = msvcrt.getwch()
        if k in ("\x00", "\xe0"):  # two-char arrow/function key
            return _NT_ARROWS.get(msvcrt.getwch() if msvcrt.kbhit() else "", "")
        if k == "\x1b":
            return "esc"
        if k == "\r":
            return "enter"
        return k
else:
    import select
    import termios
    import tty

    _VT_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}

    def _getkey() -> str:
        # os.read on the raw fd: buffered sys.stdin slurps an arrow's "[A" tail
        # into python's buffer, after which select() on the fd truthfully reports
        # no data - and every arrow key mis-parses as a bare esc
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], 0)[0]:
            return ""
        k = os.read(fd, 1).decode("utf-8", "replace")
        if k in ("\r", "\n"):
            return "enter"
        if k != "\x1b":
            return k
        # arrows arrive as a burst: esc [ A - give the tail 10ms to land
        seq = ""
        while select.select([fd], [], [], 0.01)[0]:
            c = os.read(fd, 1).decode("utf-8", "replace")
            seq += c
            if c.isalpha() or c == "~":
                break
        if not seq:
            return "esc"
        return _VT_ARROWS.get(seq[-1], "")


def _enable_ansi() -> bool:
    """Windows conhost ships with VT processing off; flip it on (no-op elsewhere)."""
    return common.enable_vt()


def _age(ms: int, now: int) -> str:
    s = max(0, (now - ms) // 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


_cell_clusters = surface.cell_clusters
_cells = surface.cell_width
_pad_cells = surface.pad_cells


def _trunc(s: str, w: int) -> str:
    s = " ".join(common.terminal_safe(
        surface.redact_live_secrets(s)).split())
    return surface.truncate_cells(s, w)


_EVENT_GLYPH = surface.EVENT_GLYPHS


def _family_root_key(
        key: tuple[str, str], parents: dict[tuple[str, str], tuple[str, str]],
) -> tuple[str, str]:
    current = key
    path: list[tuple[str, str]] = []
    positions: dict[tuple[str, str], int] = {}
    while True:
        if current in positions:
            return min(path[positions[current]:])
        positions[current] = len(path)
        path.append(current)
        parent = parents.get(current)
        if parent is None:
            return current
        current = parent


def _family_rows(sessions: list[dict]) -> list[dict]:
    """Annotate a snapshot without mutating the resident watcher's rows."""
    rows = [dict(row) for row in sessions if isinstance(row, dict)]
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        agent = str(row.get("agent") or "")
        session = str(row.get("session") or "")
        parent = str(row.get("parent") or "")
        row["sub"] = common.is_side_session(row)
        if parent:
            parents[(agent, session)] = (agent, parent)
    families: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("agent") or ""), str(row.get("session") or ""))
        root = _family_root_key(key, parents)
        # A missing parent still identifies the family, even though it has no
        # root card in this bounded live window.
        row["_family_key"] = root
        families.setdefault(root, []).append(row)
    for family in families.values():
        last_ts = max((int(row.get("last_ts") or 0) for row in family), default=0)
        working = any(bool(row.get("working")) for row in family)
        active = any(bool(row.get("active")) for row in family)
        sides = [row for row in family if row.get("sub")]
        projects = tuple(sorted({
            str(row.get("project") or "").lower() for row in family
            if row.get("project")
        }))
        for row in family:
            row["_family_last_ts"] = last_ts
            row["_family_working"] = working
            row["_family_active"] = active
            row["_family_projects"] = projects
            row["_side_count"] = len(sides)
            row["_side_working"] = sum(
                bool(side.get("working")) for side in sides)
    # Invalid empty ids cannot be useful selectors and can collapse unrelated rows.
    return [row for row in rows if row.get("session")]


def _order(sessions: list[dict], sort: str = "working") -> list[dict]:
    """Order root families, then their visible children, deterministically."""
    rows = ([dict(row) for row in sessions]
            if all("_family_key" in row for row in sessions)
            else _family_rows(sessions))
    roots = [row for row in rows if not row.get("sub")]
    children: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("sub"):
            children.setdefault(row["_family_key"], []).append(row)

    def root_key(row: dict) -> tuple:
        stable = (str(row.get("agent") or ""), str(row.get("session") or ""))
        if sort == "updated":
            return (-int(row.get("_family_last_ts") or 0), *stable)
        return (not bool(row.get("_family_working")),
                -(int(row.get("_family_last_ts") or 0) // 30_000), *stable)

    def child_key(row: dict) -> tuple:
        if sort == "updated":
            return (-int(row.get("last_ts") or 0),
                    str(row.get("agent") or ""), str(row.get("session") or ""))
        return (not bool(row.get("working")), -int(row.get("last_ts") or 0),
                str(row.get("agent") or ""), str(row.get("session") or ""))

    roots.sort(key=root_key)
    out: list[dict] = []
    for root in roots:
        out.append(root)
        out.extend(sorted(children.pop(root["_family_key"], []), key=child_key))
    orphans = [row for family in children.values() for row in family]
    out.extend(sorted(orphans, key=child_key))
    return out


def _event_line(ev: dict, width: int) -> str:
    """One event as a line: its text, else tool name + input head, else its type."""
    text = " ".join((ev.get("text") or "").split())
    if not text and ev.get("type") == "subagent_result" and ev.get("output"):
        result = " ".join(str(ev["output"]).split())
        text = f"{ev.get('name') or 'subagent'}: {result}"
    if not text and ev.get("name"):
        inp = " ".join((ev.get("input") or "").split())
        text = f"{ev['name']}{': ' + inp if inp else ''}"
    glyph = _EVENT_GLYPH.get(ev.get("type", ""), surface.GLYPHS.unknown)
    return f"{glyph} {_trunc(text or ev.get('type', ''), width)}"


def _live_title(x: dict) -> str:
    """A row's identity: its title, else the last thing anyone said in it -
    always more orienting than a raw session id."""
    t = x.get("title")
    if t:
        return t
    if x.get("last_text"):
        return x["last_text"]
    for ev in reversed(x.get("recent") or []):
        text = " ".join((ev.get("text") or "").split())
        if text:
            return text
    project = str(x.get("project") or "history").replace("\\", "/")
    project = project.rstrip("/").rsplit("/", 1)[-1] or "history"
    short = str(x.get("session") or "")[:8]
    label = f"{x.get('agent') or 'agent'} in {project}"
    return f"{label} · {short}" if short else label


def _state_color(state: str) -> str:
    if state.startswith((surface.GLYPHS.tool, surface.GLYPHS.success)):
        return CYAN
    if "thinking" in state or "working" in state:
        return YELLOW
    return ""


def _row(x: dict, now: int, width: int, color: bool, n: int = 0) -> str:
    working = bool(x.get("_family_working")
                   if not x.get("sub") else x.get("working"))
    dot = (f"{GREEN}{surface.GLYPHS.working}{RESET}" if working and color
           else surface.GLYPHS.working if working else surface.GLYPHS.idle)
    agent = _trunc(x.get("agent") or "?", 9)
    project = _trunc(x.get("project") or "~", 16)
    family_only = working and not x.get("working") and not x.get("sub")
    state_text = (f"{int(x.get('_side_working') or 0)} side working"
                  if family_only else x.get("state") or
                  ("working" if working else "idle"))
    state = _trunc(state_text, 18)
    queued = f" {surface.GLYPHS.queued}{x['queued']}" if x.get("queued") else ""
    collapsed = int(x.get("_collapsed_side_count") or 0)
    sides = f" +{collapsed} side" if collapsed else ""
    age = _age(x.get("last_ts") or now, now)
    session = str(x.get("session") or "")
    handle = compact_handles.encode_session_handle(
        session, prefix_chars=max(8, len(session)))
    identity = handle or f"session={common.terminal_safe(session or '?')}"
    num = str(n) if 1 <= n <= 9 else " "
    lead = f"{num} └ " if x.get("sub") else f"{num} "
    agent_field = _pad_cells(agent, 9)
    project_field = _pad_cells(project, 16)
    state_field = _pad_cells(state, 18)
    left = (f"{lead}{dot} {agent_field} {project_field} {state_field}"
            f"{queued}{sides} {age:>4} {identity}  ")
    # the visible width of `left` ignores ANSI codes; strip for the math
    vis = _cells(left) - (len(GREEN) + len(RESET) if (working and color) else 0)
    title = _trunc(_live_title(x), max(8, width - vis))
    if not working and color:
        return f"{DIM}{left}{title}{RESET}"
    if color and working:
        sc = _state_color(state)
        if sc:
            colored = f"{sc}{state}{RESET}" + " " * max(0, 18 - _cells(state))
            left = left.replace(state_field, colored, 1)
    return left + title


def _activity(x: dict, width: int, color: bool, title: str) -> list[str]:
    """The working row's two-line story: what was said, then what it is doing.
    The tool line appears only when tooling is newer than the words (a finished
    reply must not sit above a stale tool call), and the said line is dropped
    when the title already fell back to the same text."""
    d, r = (DIM, RESET) if color else ("", "")
    recent = x.get("recent") or []
    said = next((ev for ev in reversed(recent)
                 if ev.get("type") in ("user", "reply")
                 and (ev.get("text") or "").strip()
                 # bracketed dispatch markers are routing, not speech
                 and not (ev.get("text") or "").lstrip().startswith("[subagent")),
                None)
    tool = next((ev for ev in reversed(recent)
                 if ev.get("type") in ("tool", "tool_done") and ev.get("name")), None)
    if tool and tool.get("type") == "tool_done" and not tool.get("input"):
        # done events drop the input; borrow it from the call they closed
        call = next((ev for ev in reversed(recent)
                     if ev.get("type") == "tool" and ev.get("name") == tool.get("name")
                     and ev.get("input")), None)
        if call:
            tool = {**tool, "input": call["input"]}
    picks = []
    if said and " ".join((said.get("text") or "").split()) != title:
        picks.append(said)
    if tool and (not said or (tool.get("ts") or 0) >= (said.get("ts") or 0)):
        picks.append(tool)
    return [f"{d}       {_event_line(ev, width - 12)}{r}" for ev in picks]


def _detail(x: dict, now: int, width: int, color: bool,
            events: int = 3, meta: bool = True) -> list[str]:
    d = DIM if color else ""
    r = RESET if color else ""
    lines = []
    if meta:
        bits = []
        if x.get("model"):
            bits.append(x["model"])
        if x.get("queued"):
            bits.append(
                f"{surface.GLYPHS.queued} {x['queued']} queued: "
                f"{_trunc(x.get('queued_text') or '', 40)}")
        if bits:
            lines.append(f"{d}       {_trunc(' · '.join(bits), width - 7)}{r}")
    # events that carry neither text nor a tool name render as a bare glyph - skip
    rich = [ev for ev in (x.get("recent") or [])
            if (ev.get("text") or "").strip() or ev.get("name")]
    for ev in rich[-events:]:
        lines.append(f"{d}       {_event_line(ev, width - 12)}{r}")
    return lines


def _visible(
        snap: dict, agent_filter: str, *, project: str = "",
        state: str = "all", side_mode: str = "collapse",
        sort: str = "working", session: str = "",
) -> list[dict]:
    sessions = _family_rows(snap.get("sessions") or [])
    project = project.lower()

    def admits(row: dict) -> bool:
        is_root = not row.get("sub")
        if agent_filter != "all" and row.get("agent") != agent_filter:
            return False
        if session and session not in str(row.get("session") or ""):
            return False
        if project:
            if is_root:
                if not any(project in value for value in row["_family_projects"]):
                    return False
            elif project not in str(row.get("project") or "").lower():
                return False
        working = bool(row.get("_family_working") if is_root else row.get("working"))
        active = bool(row.get("_family_active") if is_root else row.get("active"))
        if state == "working" and not working:
            return False
        if state == "active" and not active:
            return False
        if state == "idle" and working:
            return False
        if side_mode == "roots" and not is_root:
            return False
        if side_mode == "only" and is_root:
            return False
        if (side_mode == "collapse" and not is_root and not row.get("working")
                and not session):
            return False
        return True

    visible = [row for row in sessions if admits(row)]
    visible_keys = {
        (str(row.get("agent") or ""), str(row.get("session") or ""))
        for row in visible
    }
    for row in visible:
        if row.get("sub"):
            continue
        family_sides = [member for member in sessions
                        if member.get("sub")
                        and member.get("_family_key") == row.get("_family_key")]
        row["_collapsed_side_count"] = sum(
            (str(member.get("agent") or ""), str(member.get("session") or ""))
            not in visible_keys for member in family_sides)
    return _order(visible, sort=sort)


def _hdr(left: str, keys: str, width: int, color: bool) -> list[str]:
    b, d, r = (BOLD, DIM, RESET) if color else ("", "", "")
    head = f"{b}agrep board{r} · {left}"
    if not keys:
        return [head, d + "─" * width + r]
    tail = f"{d}{keys}{r}"
    pad = max(1, width - (len(head) - (len(b) + len(r) if color else 0))
              - (len(tail) - (len(d) + len(r) if color else 0)))
    return [head + " " * pad + tail, d + "─" * width + r]


def _legend(width: int, color: bool) -> str:
    d, r = (DIM, RESET) if color else ("", "")
    g = surface.GLYPHS
    text = (f"{g.working} working  {g.idle} idle  {g.prompt} prompt  "
            f"{g.reply} reply  {g.tool} tool  {g.success} closed  "
            f"{g.done} done  {g.queued} queued  {g.child_result} child result")
    return f"{d}{_trunc(text, width)}{r}"


def _frame(sessions: list[dict], now: int, compact: bool, agent_filter: str,
           width: int, height: int, color: bool, sel: int = -1,
           scanning: bool = False, last_err: str = "",
           degraded_sources: list[dict] | None = None,
           interactive: bool = True) -> str:
    d, r = (DIM, RESET) if color else ("", "")
    degraded_sources = degraded_sources or []
    working = sum(1 for x in sessions if x.get("working"))
    lines = _hdr(f"{working} working · {len(sessions)} live-window · "
                 f"agent: {_trunc(agent_filter, 20)}",
                 ("↑↓ select · enter focus · a agent · c compact · q quit"
                  if interactive else ""),
                 width, color)
    if interactive:
        lines.append(_legend(width, color))
    if not sessions:
        lines.append("")
        if scanning:
            empty = "scanning for sessions…"
        elif degraded_sources:
            empty = "live coverage degraded; unreadable stores may hide active sessions"
        else:
            empty = ("no sessions active in the last 90s; quiet long tool calls "
                     "appear on their next write")
        lines.append(f"  {d}{empty}{r}")
    notices = []
    if scanning:
        notices.append(
            "snapshot still scanning; more live sessions may appear")
    if degraded_sources:
        first = degraded_sources[0]
        notices.append(
            f"! unreadable {first.get('agent') or 'agent'} store: "
            f"{first.get('path') or '?'}: {first.get('error') or 'access failed'}")
        if len(degraded_sources) > 1:
            notices[-1] += f" (+{len(degraded_sources) - 1} more)"
    source_error = any(
        last_err.startswith(
            f"{item.get('agent')} source unreadable: {item.get('path')}:")
        for item in degraded_sources)
    if last_err and not source_error:
        notices.append(f"! live watcher: {last_err}")
    budget = height - 4 - len(notices)
    shown = 0
    for i, x in enumerate(sessions):
        if i == sel:
            plain = _row(x, now, width, False, n=i + 1)
            row = (f"\x1b[7m{_pad_cells(plain, width)}\x1b[27m" if color
                   else "▸" + plain[1:])
            rows = [row]
        else:
            rows = [_row(x, now, width, color, n=i + 1)]
        if not compact:
            rows.extend(_detail(x, now, width, color))
        elif x.get("working"):
            # a working row always says what it is doing, without a keypress
            rows.extend(_activity(x, width, color, _live_title(x)))
        if shown + len(rows) > budget:
            lines.append(f"  {d}… more sessions than fit - resize or filter (a){r}")
            break
        lines.extend(rows)
        shown += len(rows)
    lines.extend(f"{d}{_trunc(notice, width)}{r}" for notice in notices)
    return "\n".join(lines)


def _focus_frame(x: dict, now: int, width: int, height: int, color: bool,
                 scroll: int = 0, interactive: bool = True,
                 scanning: bool = False) -> str:
    """One session, full screen: identity up top, its event feed below.

    scroll counts events back from the tail; 0 = pinned to newest.
    """
    d, r = (DIM, RESET) if color else ("", "")
    working = bool(x.get("working"))
    dot = (f"{GREEN}{surface.GLYPHS.working}{RESET}" if working and color
           else surface.GLYPHS.working if working else surface.GLYPHS.idle)
    who = _trunc(f"{x.get('agent') or '?'} · {x.get('project') or '~'}", 40)
    state = _trunc(x.get("state") or "idle", 24)
    lines = _hdr(f"{dot} {who} · {state}",
                 "↑↓ scroll · esc back · q quit" if interactive else "",
                 width, color)
    if interactive:
        lines.append(_legend(width, color))
    # identity block: the title is the headline, not dim metadata; the full
    # session id is copyable straight into `agrep around`/`resume`
    title = _live_title(x)
    if title != x.get("session", "")[:12]:
        lines.append(_trunc(title, width))
    meta = [f"session {x.get('session', '')}"]
    if x.get("model"):
        meta.append(x["model"])
    meta.append(f"last event {_age(x.get('last_ts') or now, now)} ago")
    lines.append(f"{d}{_trunc(' · '.join(meta), width)}{r}")
    if x.get("queued"):
        q = (f"{surface.GLYPHS.queued} {x['queued']} queued: "
             f"{_trunc(x.get('queued_text') or '', width - 12)}")
        lines.append(f"{YELLOW}{q}{RESET}" if color else q)
    lines.append("")
    events = x.get("recent") or []
    budget = max(1, height - len(lines) - 1)
    scroll = max(0, min(scroll, len(events) - budget))
    window = events[-(budget + scroll):len(events) - scroll or None]
    for ev in window:
        age = _age(ev.get("ts") or now, now)
        line = f" {age:>4} {_event_line(ev, width - 9)}"
        lines.append(f"{d}{line}{r}" if ev.get("type") not in ("user", "reply")
                     else line)
    if scroll:
        lines.append(f"{d}   ↓ {scroll} newer - ↓ to catch up{r}")
    if not events:
        empty = ("no events visible yet" if scanning
                 else "no events yet this window")
        lines.append(f"  {d}{empty}{r}")
    if scanning:
        lines.append(
            f"{d}snapshot still scanning; session events/state may be incomplete{r}")
    return "\n".join(lines)


def _snapshot_global_error(snap: dict) -> str:
    error = str(snap.get("last_err") or "")
    for item in snap.get("degraded_sources") or []:
        prefix = (
            f"{item.get('agent')} source unreadable: {item.get('path')}:")
        if error.startswith(prefix):
            return ""
    return error


def _snapshot_completeness(snap: dict, agent_filter: str) -> tuple[bool, list[dict]]:
    degraded = snap.get("degraded_sources") or []
    if agent_filter != "all":
        degraded = [row for row in degraded if row.get("agent") == agent_filter]
    complete = (snap.get("booting") is False and not degraded
                and not _snapshot_global_error(snap))
    return complete, degraded


def _machine_session(row: dict) -> dict:
    session = str(row.get("session") or "")
    handle = compact_handles.encode_session_handle(
        session, prefix_chars=max(8, len(session)))
    return {
        "active": bool(row.get("active")),
        "agent": str(row.get("agent") or ""),
        "family_active": bool(row.get("_family_active")),
        "family_updated_ms": int(row.get("_family_last_ts") or 0),
        "family_working": bool(row.get("_family_working")),
        "handle": handle,
        "handle_unavailable_reason": (
            None if handle is not None else
            "session id is outside the public handle grammar"),
        "parent": str(row.get("parent") or ""),
        "project": str(row.get("project") or ""),
        "session": session,
        "side": bool(row.get("sub")),
        "side_count": int(row.get("_side_count") or 0),
        "side_working": int(row.get("_side_working") or 0),
        "state": str(row.get("state") or ""),
        "title": common.one_line(surface.redact_live_secrets(
            str(_live_title(row))))[:200],
        "updated_ms": int(row.get("last_ts") or 0),
        "working": bool(row.get("working")),
    }


def _retry_argv(a) -> list[str]:
    base = dist.cli_invocation(
        "board", "--once", "--wait", str(int(_ONESHOT_RETRY_WAIT_S)))
    argv = list(base)
    if a.json:
        argv.append("--json")
    if a.sort != "working":
        argv.extend(("--sort", a.sort))
    if a.agent:
        argv.extend(("--agent", a.agent))
    if a.state != "all":
        argv.extend(("--state", a.state))
    if a.roots:
        argv.append("--roots")
    elif a.side:
        argv.append("--side")
    elif a.side_only:
        argv.append("--side-only")
    if a.max is not None:
        argv.extend(("-n", str(a.max)))
    if a.project:
        argv.extend(("--project", a.project))
    if a.session:
        argv.extend(("--session", a.session))
    return argv


def _retry_command(a) -> str | None:
    return console.shell_command(*_retry_argv(a), fallback="") or None


def _select_sessions(
        sessions: list[dict], query: str, *, complete: bool,
) -> tuple[list[dict], dict, int | None]:
    if not query:
        return sessions, {
            "exact_matches": 0, "matches": len(sessions),
            "query": "", "status": "unscoped",
        }, None
    exact = [row for row in sessions if row.get("session") == query]
    candidates = exact if exact else sessions
    selector = {
        "exact_matches": len(exact), "matches": len(sessions),
        "query": query, "status": "unresolved",
    }
    if not complete:
        return candidates, selector, 2
    if not candidates:
        selector["status"] = "none"
        return [], selector, 1
    if len(candidates) > 1:
        selector["status"] = "ambiguous"
        return candidates, selector, 2
    selector["status"] = "resolved"
    return candidates, selector, None


def resolve_exact_live_session(
        session: str, *, wait_s: float = _ONESHOT_RETRY_WAIT_S,
) -> tuple[list[dict], bool]:
    """Resolve one full live id without rendering or widening to prefixes."""
    session = compact_handles.normalize_session_arg(session)

    def inspect(snap: dict) -> tuple[list[dict], bool]:
        rows = _visible(
            snap, "all", side_mode="show", sort="updated", session=session)
        exact = [row for row in rows if row.get("session") == session]
        scope = str(exact[0].get("agent") or "all") if len(exact) == 1 else "all"
        complete, _ = _snapshot_completeness(snap, scope)
        return exact, complete

    resident = indexd_runtime.resident_indexd_live_snapshot()
    if resident is not None:
        exact, complete = inspect(resident)
        if complete:
            return exact, True
    watcher = live.watcher()
    watcher.wait_boot(max(0.0, min(wait_s, _MAX_ONESHOT_WAIT_S)))
    return inspect(watcher.snapshot())


def _machine_snapshot(
        snap: dict, sessions: list[dict], matched: int, *, complete: bool,
        degraded: list[dict], truncated: bool, source: str, a,
        selector: dict | None = None,
) -> dict:
    retry = None if complete else _retry_command(a)
    retry_argv = None if complete else _retry_argv(a)
    ipc = snap.get("_agrep_live_ipc")
    ipc = ipc if isinstance(ipc, dict) else {}
    published_at = ipc.get("published_at_ms")
    age_ms = (max(0, int(time.time() * 1000) - published_at)
              if type(published_at) is int else None)
    recent_trimmed = bool(ipc.get("recent_trimmed"))
    snapshot_error = common.one_line(surface.redact_live_secrets(
        _snapshot_global_error(snap)))[:500] or None
    return {
        "completeness": {
            "booting": bool(snap.get("booting")),
            "matched": matched,
            "matched_basis": "exact" if complete else "floor",
            "retry": retry,
            "retry_argv": retry_argv,
            "retry_command_unavailable": (
                "the current shell cannot safely quote every filter; execute retry_argv"
                if retry_argv is not None and retry is None else None),
            "sessions_complete": complete,
            "shown": len(sessions),
            "source_errors": len(degraded),
            "snapshot_error": snapshot_error,
            "source_error_details": [
                {
                    "agent": str(row.get("agent") or ""),
                    "error": str(row.get("error") or ""),
                    "path": str(row.get("path") or ""),
                }
                for row in degraded[:5]
            ],
            "truncated": truncated,
        },
        "feed": {
            "recent_events_omitted_by_transport": int(
                ipc.get("recent_events_omitted") or 0),
            "scope": "recent-tail",
            "transport_complete": not recent_trimmed,
        },
        "filters": {
            "agent": registry.normalize_agent_name(a.agent.lower()) if a.agent else "all",
            "project": a.project or "",
            "session": a.session or "",
            "side": ("roots" if a.roots else "show" if a.side else
                     "only" if a.side_only else "collapse"),
            "state": a.state,
        },
        "history_command": console.shell_command(
            *dist.cli_invocation("chats", "--json"),
            fallback="agrep chats --json"),
        "kind": "agrep-board-snapshot",
        "page_complete": not truncated,
        "partial": not complete,
        "scope": "live-window",
        "selector": selector or {
            "exact_matches": 0, "matches": matched,
            "query": "", "status": "unscoped",
        },
        "snapshot_complete": complete,
        "sessions": [_machine_session(row) for row in sessions],
        "snapshot_age_ms": age_ms,
        "sort": a.sort,
        "source": source,
        "window_seconds": int(snap.get("window_s") or live.ACTIVE_WINDOW_S),
    }


def main(argv: list[str] | None = None) -> int:
    p = surface.ArgumentParser(
        prog="agrep board",
        description="show the bounded live-activity window (not indexed chat history)",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "interactive keys:\n"
            "  ↑/↓ select · enter focus · esc back · a agent filter\n"
            "  c compact/expanded · 1-9 jump · q quit\n"
            "\nexamples:\n"
            "  agrep board                 interactive board\n"
            "  agrep board --once          one printable live-window frame\n"
            "  agrep board --once --json --sort updated --agent claude "
            "--state active --roots -n 3\n"
            "  agrep board --agent codex   filter the initial view\n"
            "  agrep board --session @ID   focus a copied chat/result handle\n"
            "  agrep chats                 indexed history, newest first\n"
            "\nexit: 0 shown, 1 no live session match, "
            "2 partial snapshot, invalid, or ambiguous selector.\n"
            "a partial snapshot means some store was unreadable: it is not "
            "proof that no other agent is live."
        ))
    p.add_argument("--agent", default=None, help="filter to one supported agent "
                   "(one-shot default: all; interactive remembers its prior view)")
    p.add_argument("--session", "--chat", dest="session",
                   help="focus one live session (id substring or pasted @handle)")
    p.add_argument("--once", action="store_true",
                   help="print one frame and exit (implied when not a tty)")
    p.add_argument("--json", action="store_true",
                   help="one bounded live-snapshot object (implies --once)")
    p.add_argument("--sort", choices=("working", "updated"), default="working",
                   help="family order: working first or latest family activity")
    p.add_argument("--project", help="only families whose project contains this text")
    p.add_argument("--state", choices=("all", "active", "working", "idle"),
                   default="all", help="session/family state filter")
    side = p.add_mutually_exclusive_group()
    side.add_argument("--roots", action="store_true",
                      help="root sessions only")
    side.add_argument("--side", action="store_true",
                      help="show completed side sessions too")
    side.add_argument("--side-only", action="store_true",
                      help="side sessions only")
    p.add_argument("-n", "--max", type=int, default=None, metavar="N",
                   help=f"show at most N sessions (JSON default {_MACHINE_DEFAULT_ROWS}; "
                        f"maximum {_MACHINE_MAX_ROWS})")
    p.add_argument("--wait", type=float, default=_ONESHOT_BOOT_TIMEOUT_S,
                   metavar="SECONDS", help="bounded one-shot startup wait "
                   f"(default {_ONESHOT_BOOT_TIMEOUT_S:g}; max {_MAX_ONESHOT_WAIT_S:g})")
    p.add_argument("--expanded", action="store_true",
                   help="start in expanded mode (per-session events + model)")
    a = p.parse_args(argv)
    blank_session = surface.blank_value_error(
        "--session/--chat", a.session, "a substring of a session id",
        "open the unscoped board")
    if blank_session:
        p.error(blank_session)
    if a.project is not None and not a.project.strip():
        p.error("--project requires non-blank text")
    if a.max is not None and not 1 <= a.max <= _MACHINE_MAX_ROWS:
        p.error(f"--max must be between 1 and {_MACHINE_MAX_ROWS}")
    if not 0 <= a.wait <= _MAX_ONESHOT_WAIT_S:
        p.error(f"--wait must be between 0 and {_MAX_ONESHOT_WAIT_S:g} seconds")
    if a.json and a.expanded:
        p.error("--expanded cannot be combined with --json")
    if a.session:
        raw_session = a.session.strip()
        try:
            if ((raw_session.startswith("@") and ":" in raw_session)
                    or compact_handles.is_result_handle(raw_session)):
                a.session = compact_handles.parse_result_handle(raw_session)[0]
            else:
                a.session = compact_handles.normalize_session_arg(raw_session)
        except compact_handles.CompactError as exc:
            p.error(str(exc))
    requested_agent = (a.agent or "").strip().lower()
    if a.agent is not None and not requested_agent:
        p.error("--agent requires a supported agent name or all")
    if requested_agent and requested_agent != "all":
        error = registry.capability_error("live", requested_agent)
        if error:
            p.error(error)
        requested_agent = registry.normalize_agent_name(requested_agent)

    # the frame is full of glyphs; Windows pipes default to cp1252 and choke
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    tty_out = sys.stdout.isatty()
    ansi = _enable_ansi()
    once = a.once or a.json or not tty_out or not ansi
    color = (tty_out and ansi) or "FORCE_COLOR" in os.environ

    resident_snapshot = (
        indexd_runtime.resident_indexd_live_snapshot() if once else None)
    w = None
    waited = False
    if resident_snapshot is not None and a.wait > 0:
        resident_complete, _ = _snapshot_completeness(
            resident_snapshot, requested_agent or "all")
        if not resident_complete:
            w = live.watcher()
            w.wait_boot(a.wait)
            waited = True
            resident_snapshot = None
    if resident_snapshot is None and w is None:
        w = live.watcher()
    if once and w is not None and not waited:
        w.wait_boot(a.wait)
    elif not once and a.session:
        assert w is not None
        w.wait_boot()

    def current_snapshot() -> dict:
        if resident_snapshot is not None:
            return resident_snapshot
        assert w is not None
        return w.snapshot()

    # Interactive a/c toggles persist as hidden UI state; flags still win.
    # One-shot diagnostics skip preference restoration so settings I/O or
    # damaged preferences cannot extend their boot deadline.
    stored_view = None if once else common.setting("board_view")
    view = stored_view if isinstance(stored_view, dict) else {}
    compact = not (a.expanded or view.get("expanded"))
    remembered_agent = str(view.get("agent") or "").lower()
    if (remembered_agent != "all"
            and registry.capability_error("live", remembered_agent)):
        remembered_agent = "all"
    agent_filter = requested_agent or remembered_agent or "all"

    def remember_view() -> None:
        settings.set_setting("board_view",
                             {"agent": agent_filter, "expanded": not compact})
    focus = ""    # session id of the focused feed, "" = board
    sel_id = ""   # board row the cursor is on (by id: rows reorder between ticks)
    sel_prev = 0  # last row index, held when the selected session vanishes
    scroll = 0   # focus feed: events back from the tail, 0 = pinned to newest

    started = time.monotonic()

    side_mode = ("roots" if a.roots else "show" if a.side else
                 "only" if a.side_only else "collapse")

    def visible(snap: dict) -> list[dict]:
        return _visible(
            snap, agent_filter, project=(a.project or "").strip(),
            state=a.state, side_mode=side_mode, sort=a.sort,
            session=a.session or "")

    def render(
            snap: dict, width: int, height: int, sel: int = -1,
            sessions_override: list[dict] | None = None,
    ) -> str:
        wall_now = int(time.time() * 1000)
        ipc = snap.get("_agrep_live_ipc")
        # A resident snapshot is deliberately bounded-age IPC, not a live
        # shared-memory view. Age events against the actual render time so the
        # cached frame never makes activity look newer than it is.
        now = wall_now if isinstance(ipc, dict) else (
            snap.get("now") or wall_now)
        sessions = (visible(snap) if sessions_override is None
                    else sessions_override)
        frame = None
        if focus:
            for x in sessions:
                if x.get("session") == focus:
                    frame = _focus_frame(
                        x, now, width, height, color, scroll,
                        interactive=not once,
                        scanning=bool(snap.get("booting")))
                    break
            # focused session left the window: fall through to the board
        if frame is None:
            frame = _frame(
                sessions, now, compact, agent_filter, width, height, color,
                sel=sel,
                scanning=bool(snap.get("booting")),
                last_err=str(snap.get("last_err") or ""),
                degraded_sources=snap.get("degraded_sources") or [],
                interactive=not once)
        if isinstance(ipc, dict):
            published_at = ipc.get("published_at_ms")
            if type(published_at) is int:
                age_s = max(0, (wall_now - published_at + 999) // 1000)
                frame += "\n" + _trunc(
                    f"resident snapshot {age_s}s old", width)
            if ipc.get("recent_trimmed"):
                omitted = int(ipc.get("recent_events_omitted") or 0)
                frame += "\n" + _trunc(
                    f"resident snapshot omitted {omitted} older feed event(s); "
                    "all session states are present",
                    width)
        return frame

    if a.session and not once:
        initial = current_snapshot()
        hits = [x for x in visible(initial)
                if a.session in (x.get("session") or "")]
        exact = [x for x in hits if a.session == (x.get("session") or "")]
        if exact:
            hits = exact
        if not hits:
            print(
                f"no live session matching "
                f"{common.terminal_safe(a.session)!r}",
                file=sys.stderr)
            return 1
        elif len(hits) > 1:
            print(
                f"ambiguous live session {common.terminal_safe(a.session)!r}; "
                f"matches {len(hits)} sessions:",
                file=sys.stderr)
            for hit in hits:
                print(
                    f"  {common.terminal_safe(hit.get('agent') or '?')} "
                    f"{common.terminal_safe(hit.get('session') or '?')}",
                    file=sys.stderr)
            return 2
        else:
            focus = hits[0]["session"]

    if once:
        snapshot = current_snapshot()
        sessions = visible(snapshot)
        complete, degraded = _snapshot_completeness(snapshot, agent_filter)
        sessions, selector, selector_rc = _select_sessions(
            sessions, a.session or "", complete=complete)
        matched = len(sessions)
        limit = a.max if a.max is not None else (
            _MACHINE_DEFAULT_ROWS if a.json else None)
        shown = sessions[:limit] if limit is not None else sessions
        if a.json:
            payload = _machine_snapshot(
                snapshot, shown, matched, complete=complete,
                degraded=degraded, truncated=len(shown) < matched,
                source="resident" if resident_snapshot is not None else "foreground",
                a=a, selector=selector)
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")))
            if selector_rc is not None:
                return selector_rc
            return 0 if complete else 2
        if selector["status"] == "none":
            print(
                f"no live session matching "
                f"{common.terminal_safe(a.session)!r}",
                file=sys.stderr)
            return 1
        if selector["status"] == "ambiguous":
            print(
                f"ambiguous live session {common.terminal_safe(a.session)!r}; "
                f"matches {len(sessions)} sessions:",
                file=sys.stderr)
            for hit in sessions:
                print(
                    f"  {common.terminal_safe(hit.get('agent') or '?')} "
                    f"{common.terminal_safe(hit.get('session') or '?')}",
                    file=sys.stderr)
            return 2
        if a.session and len(sessions) == 1:
            focus = str(sessions[0].get("session") or "")
        width, height = shutil.get_terminal_size((110, 34))
        # The human renderer applies the same selectors; its screen-height cut
        # remains a presentation concern, not a different session population.
        print(render(snapshot, width, height, sessions_override=shown))
        if not complete:
            retry = _retry_command(a)
            action = (f"retry: {retry}" if retry else
                      "rerun the same command with --wait 5")
            print(f"partial live snapshot; {action}", file=sys.stderr)
            return 2
        return 0

    assert w is not None
    unix_state = None
    screen_entered = False
    try:
        if os.name != "nt":
            unix_state = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        screen_entered = True
        sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt screen, hide cursor
        last_draw = 0.0
        while True:
            k = _getkey()
            now = time.monotonic()
            # keys repaint immediately; the data tick repaints at 2Hz (faster while
            # the watcher seeds, so the board fills the moment sessions appear).
            # The idle path is a 30ms input poll, so navigation never waits.
            tick = 0.15 if now - started < 2.0 else 0.5
            if not k and now - last_draw < tick:
                time.sleep(0.03)
                continue
            if k in ("q", "Q", "\x03"):
                return 0
            if k in ("esc", "b", "B"):
                focus, scroll = "", 0
            if k in ("c", "C"):
                compact = not compact
                remember_view()
            if k in ("a", "A"):
                focus, scroll = "", 0
                agents = sorted({x.get("agent") or "?"
                                 for x in (w.snapshot().get("sessions") or [])})
                cycle = ["all", *agents]
                try:
                    agent_filter = cycle[(cycle.index(agent_filter) + 1) % len(cycle)]
                except ValueError:
                    agent_filter = "all"
                remember_view()

            snap = w.snapshot()
            sessions = visible(snap)
            ids = [x["session"] for x in sessions]
            # a vanished selection holds its position instead of snapping to the top
            sel = (ids.index(sel_id) if sel_id in ids
                   else min(sel_prev, len(ids) - 1) if ids else -1)
            if focus:
                feed = next((len(x.get("recent") or []) for x in sessions
                             if x["session"] == focus), 0)
                if k in ("up", "k"):
                    scroll = min(scroll + 1, feed)  # display clamps the rest
                if k in ("down", "j"):
                    scroll = max(0, scroll - 1)
            else:
                if k in ("up", "k") and ids:
                    sel = max(0, sel - 1)
                if k in ("down", "j") and ids:
                    sel = min(len(ids) - 1, sel + 1)
                if k.isdigit() and k != "0" and int(k) <= len(ids):
                    sel = int(k) - 1
                    focus, scroll = ids[sel], 0
                if k == "enter" and 0 <= sel < len(ids):
                    focus, scroll = ids[sel], 0
            sel_id = ids[sel] if 0 <= sel < len(ids) else ""
            sel_prev = max(0, sel)

            width, height = shutil.get_terminal_size((110, 34))
            frame = render(snap, width, height, sel=sel)
            # repaint in place, clearing each line's tail - no full-clear flicker
            out = "".join(ln + "\x1b[K\n" for ln in frame.split("\n"))
            sys.stdout.write("\x1b[H" + out + "\x1b[0J")
            sys.stdout.flush()
            last_draw = now
    except KeyboardInterrupt:
        return 130
    finally:
        if screen_entered:
            try:
                sys.stdout.write("\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
            except OSError:
                pass
        if unix_state is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, unix_state)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
