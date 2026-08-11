"""`agrep resume [id]` - jump back into a past session in its own agent, cd'd to where
it ran. The id is whatever you see in `agrep` output: the short 8-char prefix, a full
uuid, or an opencode `ses_…`. With no id, pick from your most recent sessions.

The agent takes over the current terminal (no new window); when it exits you're back at
your shell. Session-id resolution is shared through common.py; per-agent resume commands
live in native.py.
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


def _match(rows: list[dict], q: str) -> list[dict]:
    """Resolve an id query: exact wins; else prefix on the full id or the short 8-char.
    The `@` agrep prints in front of an id is part of the identity it prints,
    so it pastes back here rather than reading as an unknown session."""
    q = compact.normalize_session_arg(q)
    matches = set(common.match_session_ids((r.get("session") for r in rows), q))
    return [r for r in rows if r.get("session") in matches]


def _live_identity(value: str) -> str:
    if compact.is_result_handle(value):
        return compact.parse_result_handle(value)[0]
    return compact.normalize_session_arg(value)


def _live_match(value: str) -> tuple[list[dict], bool]:
    import livetui
    return livetui.resolve_exact_live_session(_live_identity(value))


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
    common.log(f"'{common.terminal_safe(raw)}' isn't a listed number or a unique id.")
    return None


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()

    ap = surface.ArgumentParser(
        prog="agrep resume", description="resume a past session in its own agent, cd'd "
                                         "to where it ran",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep resume @11111111       resume the session from a search hit\n"
               "  agrep resume --list          list recent resumable sessions\n"
               "\nexit: 0 selected/listed or the resumed agent exited 0; "
               "1 no unique match or launch failed; 2 unavailable data or invalid "
               "arguments. Other resumed-agent exit codes are passed through.",
        allow_abbrev=False)
    ap.add_argument("id", nargs="?", help="session id or prefix (the 8-char from `agrep` "
                                          "output, a uuid, or ses_…); omit to pick")
    ap.add_argument("-n", "--max", type=int, default=15, metavar="N",
                    help="how many recent sessions to list when picking (default 15)")
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
        m = _match(rows, args.id)
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
            for r in m[:12]:
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
