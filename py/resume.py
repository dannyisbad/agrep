"""`agrep resume [id]` — jump back into a past session in its own agent, cd'd to where
it ran. The id is whatever you see in `agrep` output: the short 8-char prefix, a full
uuid, or an opencode `ses_…`. With no id, pick from your most recent sessions.

The agent takes over the current terminal (no new window); when it exits you're back at
your shell. Resolution and the per-agent resume command live in native.py.
"""

from __future__ import annotations

import argparse
import json
import sys

import common
import native

_C = {"a": "\033[36m", "d": "\033[2m", "n": "\033[1;33m", "r": "\033[0m"}


def _sessions() -> list[dict]:
    """All indexed sessions, newest first. Small file (session/agent/project/ts/text)."""
    p = common.DATA_DIR / "sessions.jsonl"
    rows = []
    if p.exists():
        for line in p.open(encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda o: o.get("last_ts", 0), reverse=True)
    return rows


def _match(rows: list[dict], q: str) -> list[dict]:
    """Resolve an id query: exact wins; else prefix on the full id or the short 8-char."""
    q = q.strip()
    exact = [r for r in rows if r.get("session") == q]
    if exact:
        return exact[:1]
    return [r for r in rows if r.get("session", "").startswith(q) or r.get("session", "")[:8] == q]


def _label(r: dict, color: bool) -> str:
    who = f"{r.get('agent', '?')} · {r.get('project') or '—'}"
    txt = " ".join((r.get("first_text") or "").split())[:70]
    sess = (r.get("session") or "")[:8]
    if color:
        return f"{_C['a']}{who}{_C['r']} {_C['d']}{sess}{_C['r']}  {txt}"
    return f"{who}  {sess}  {txt}"


def _pick(rows: list[dict], n: int, color: bool) -> dict | None:
    """Numbered list of recent sessions + a prompt. Clickless, robust, no fullscreen."""
    if not sys.stdin.isatty():
        common.log("no session id given (and stdin isn't a terminal to pick from). "
                   "pass an id, e.g. `agrep resume 00da9752`.")
        return None
    shown = rows[:n]
    for i, r in enumerate(shown, 1):
        num = f"{_C['n']}{i:>2}{_C['r']}" if color else f"{i:>2}"
        print(f"{num}  {_label(r, color)}", file=sys.stderr)
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
    m = _match(rows, raw)
    if len(m) == 1:
        return m[0]
    common.log(f"'{raw}' isn't a listed number or a unique id.")
    return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 -> mojibake without this
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(
        prog="agrep resume", description="resume a past session in its own agent, cd'd "
                                         "to where it ran")
    ap.add_argument("id", nargs="?", help="session id or prefix (the 8-char from `agrep` "
                                          "output, a uuid, or ses_…); omit to pick")
    ap.add_argument("-n", "--max", type=int, default=15, metavar="N",
                    help="how many recent sessions to list when picking (default 15)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="just list recent sessions; don't resume")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = ap.parse_args(argv)
    color = args.color == "always" or (args.color == "auto" and sys.stderr.isatty())

    rows = _sessions()
    if not rows:
        cli = "python tilt.py" if common._is_dev_checkout() else "agrep"
        common.log(f"no index yet — run `{cli} index` first.")
        return 2

    if args.list:
        for r in rows[: args.max]:
            print(_label(r, color))
        return 0

    if args.id:
        m = _match(rows, args.id)
        if not m:
            common.log(f"no session matches '{args.id}'. try `agrep resume -l` to see recent ones.")
            return 1
        if len(m) > 1:
            common.log(f"'{args.id}' is ambiguous — {len(m)} sessions match:")
            for r in m[:12]:
                print(f"  {_label(r, color)}", file=sys.stderr)
            return 1
        chosen = m[0]
    else:
        chosen = _pick(rows, args.max, color)
        if not chosen:
            return 0

    return native.resume_in_place(chosen.get("agent", ""), chosen.get("session", ""))


if __name__ == "__main__":
    raise SystemExit(main())
