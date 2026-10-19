"""`agrep resume [reference]` - reopen a session in its own agent, cd'd to where it ran.
Paste a session id or result handle, name a project, or quote part of the chat's first
line. With no reference, pick from the most recent sessions; ambiguous human references
restrict that picker to matching chats.

The agent takes over the current terminal (no new window); when it exits you're back at
your shell. Session-id matching is shared through common.py and handle parsing through
compact.py; per-agent resume commands live in native.py.
"""

from __future__ import annotations

import argparse
import sys

import common
import compact
from hookless import native
import surface_policy as surface

_C = surface.PALETTE


def _sessions() -> list[dict]:
    """All indexed sessions, newest first, via the shared damage-tolerant
    aggregate reader: a schema-mutant row ("last_ts":"yesterday") is skipped
    and healing (law 1), never a crash that outlives the row."""
    import explore
    rows = list(explore._session_index().values())
    rows.sort(key=lambda o: o.get("last_ts", 0), reverse=True)
    return rows


def _session_identity(value: str) -> str:
    text = compact.normalize_session_arg(value)
    if compact.is_result_handle(text):
        return compact.parse_result_handle(text)[0]
    session, colon, turns = text.rpartition(":")
    if colon:
        start, dash, end = turns.partition("-")
        try:
            identity, first = compact.parse_result_handle(f"{session}:{start}")
            if dash:
                _, last = compact.parse_result_handle(f"{session}:{end}")
                if last < first:
                    return text
            return identity
        except compact.CompactError:
            pass
    return text


def _resolve_reference(rows: list[dict], q: str) -> tuple[list[dict], bool]:
    """Return matching sessions and whether a human reference permits a picker."""
    raw = compact.normalize_session_arg(q)
    if not raw:
        return [], False
    exact = [r for r in rows if r.get("session") == raw]
    if exact:
        return exact, False
    identity = _session_identity(q)
    ids = set(common.match_session_ids((r.get("session") for r in rows), identity))
    if ids:
        return [r for r in rows if r.get("session") in ids], False
    needle = identity.lower()
    if len(needle) >= 6 and all(c in "0123456789abcdef" for c in needle):
        matches = [r for r in rows if needle in str(r.get("session") or "").lower()]
        if matches:
            return matches, False
    query = q.strip()
    matches = [r for r in rows if surface.project_label_matches(r.get("project"), query)]
    if not matches:
        needle = query.casefold()
        matches = [r for r in rows if needle in (r.get("first_text") or "").casefold()]
    return sorted(matches, key=lambda r: r.get("last_ts", 0), reverse=True), True


def _match(rows: list[dict], q: str) -> list[dict]:
    """Resolve session references, then project labels, then first-line substrings."""
    return _resolve_reference(rows, q)[0]


def _live_match(value: str) -> tuple[list[dict], bool]:
    import livetui
    return livetui.resolve_exact_live_session(_session_identity(value))


def _label(r: dict, color: bool, session_index=None) -> str:
    who = common.terminal_safe(f"{r.get('agent', '?')} · {r.get('project') or '-'}")
    txt = common.terminal_safe(" ".join((r.get("first_text") or "").split()))[:70]
    sess = common.terminal_safe(compact.encode_session_target(
        r.get("session"), session_index=session_index))
    if color:
        return f"{_C['a']}{who}{_C['r']} {_C['d']}{sess}{_C['r']}  {txt}"
    return f"{who}  {sess}  {txt}"


def _pick(rows: list[dict], n: int, color: bool, session_index=None) -> dict | None:
    """Numbered list of recent sessions + a prompt. Clickless, robust, no fullscreen."""
    if not sys.stdin.isatty():
        common.log("no session id given (and stdin isn't a terminal to pick from). "
                   "pass an id, e.g. `agrep resume 11111111`.")
        return None
    shown = rows[:n]
    for i, r in enumerate(shown, 1):
        num = f"{_C['n']}{i:>2}{_C['r']}" if color else f"{i:>2}"
        print(f"{num}  {_label(r, color, session_index)}", file=sys.stderr)
    try:
        raw = input("\nresume # (enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(shown):
        return shown[int(raw) - 1]
    # let them type an id at the prompt too
    try:
        m = _match(rows, raw)
    except compact.CompactError as exc:
        common.log(str(exc))
        return None
    if len(m) == 1:
        return m[0]
    common.log(f"'{common.terminal_safe(raw)}' isn't a listed number or a unique reference.")
    return None


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()

    ap = surface.ArgumentParser(
        prog="agrep resume", description="resume a past session in its own agent, cd'd "
                                         "to where it ran",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="IDs and handles take priority. A hex fragment of at least 6 characters\n"
               "also matches inside an id when no prefix matches. Project labels use\n"
               "the same exact-label-or-leaf rule as --project (including * and ? globs);\n"
               "otherwise, match a first-line substring, ignoring case.\n"
               "Multiple project/first-line matches open a restricted picker on a\n"
               "terminal; without one, candidates are listed and nothing is launched.\n"
               "\nexamples:\n"
               "  agrep resume @01a06003:4.82c5   reopen the session from a search hit\n"
               "  agrep resume solo-finder        match a project; pick if several\n"
               "  agrep resume                    pick from recent sessions\n"
               "  agrep resume --list             list recent resumable sessions\n"
               "\nexit: 0 listed, picker closed, or the resumed agent exited 0; "
               "1 no unique match or launch failed; 2 unavailable data or invalid "
               "arguments. Other resumed-agent exit codes are passed through.",
        allow_abbrev=False)
    ap.add_argument("id", nargs="?", metavar="REFERENCE",
                    help="session id/prefix (with optional @, uuid or ses_…), search-hit "
                         "handle, project label, or first-line substring; omit to pick")
    ap.add_argument("-n", "--max", type=int, default=15, metavar="N",
                    help="how many recent/matching sessions to show in the picker or list (default 15)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="just list recent sessions; don't resume")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = surface.parse_args_with_presence(ap, argv)
    # an option a surface renders inert is refused, never dropped: `-l <id>`
    # once listed the recent chats and said nothing about the id it ignored
    gated = surface.option_gate_error(args, surface.RESUME_OPTION_GATES)
    if gated:
        ap.error(gated)
    # a negative count is not a smaller list: rows[:-1] serves every session
    # but the oldest, silently - nonsense values are refused like siblings do
    if args.max < 0:
        ap.error("-n must be 0 or greater")
    if args.id:
        try:
            compact.normalize_session_arg(args.id)
        except compact.CompactError as exc:
            common.log(str(exc))
            return 2
    color = common.color_enabled(sys.stderr, args.color)

    rows = _sessions()
    session_index = compact.session_prefix_index(
        r.get("session") for r in rows if r.get("session"))

    if args.list:
        if not rows:
            common.log(f"no index yet - {common.setup_hint()}")
            return 2
        for r in rows[: args.max]:
            print(_label(r, color, session_index))
        return 0

    if args.id:
        m, human_reference = _resolve_reference(rows, args.id)
        if not m:
            live, complete = _live_match(args.id)
            if len(live) == 1 and complete:
                chosen = live[0]
            elif len(live) > 1:
                common.log(
                    f"'{common.terminal_safe(args.id)}' matches multiple live sessions; "
                    "copy the full id from `agrep board --once --json`.")
                return 2
            elif not complete:
                common.log(
                    "live session lookup is incomplete; retry `agrep resume "
                    f"{common.terminal_safe(args.id)}`.")
                return 2
            else:
                common.log(f"no session matches '{common.terminal_safe(args.id)}' "
                           "- recent ones:")
                for r in rows[: args.max]:
                    print(_label(r, color, session_index))
                return 1
        elif len(m) > 1:
            common.log(f"'{common.terminal_safe(args.id)}' is ambiguous - "
                       f"{len(m)} sessions match:")
            if human_reference and sys.stdin.isatty():
                chosen = _pick(m, args.max, color, session_index)
                if not chosen:
                    return 0
            else:
                for r in m:
                    print(f"  {_label(r, color, session_index)}", file=sys.stderr)
                return 1
        else:
            chosen = m[0]
    else:
        if not rows:
            common.log(f"no index yet - {common.setup_hint()}")
            return 2
        chosen = _pick(rows, args.max, color, session_index)
        if not chosen:
            return 0

    return native.resume_in_place(chosen.get("agent", ""), chosen.get("session", ""))


if __name__ == "__main__":
    raise SystemExit(main())
