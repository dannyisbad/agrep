"""`agrep around <session> <turn>` — the context window around a search hit.

    agrep around 00da9752 144          # ±4 turns around turn 144, tool calls inline
    agrep around 00da9752:144 -n 10    # wider window, colon form pastes from --json
    agrep around 00da9752 144 --full   # uncapped text (single-turn deep read: -n 0)
    agrep around 00da9752 144 --json   # one object per message/event, for piping

Search tells you WHICH session touched a thing; around tells you WHAT happened —
the local story of a hit (error, attempts, fix) for a few KB instead of a whole
transcript. Tool calls show name/input/ok by default but never their output (the
token bomb); opt in with --tool-output N. Truncated messages end with the exact
command that prints the rest, so a follow-up never needs guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

import common
import explore

# same palette as search.py: bold-cyan header, dim metadata, yellow you, cyan agent
_C = {"hd": "\033[1;36m", "d": "\033[2m", "y": "\033[33m", "a": "\033[36m",
      "g": "\033[32m", "bad": "\033[1;31m", "r": "\033[0m"}


def _color_on(when: str) -> bool:
    if when == "always":
        return True
    if when == "never":
        return False
    return sys.stdout.isatty()


def _parse_target(args_session: str, args_turn: str | None) -> tuple[str, int]:
    """Accept `around <session> <turn>` and `around <session>:<turn>` (the colon form
    pastes straight from a --json hit's fields). Exit 2 on an unparseable turn."""
    s = args_session
    if args_turn is None and ":" in s:
        s, _, t = s.rpartition(":")
        args_turn = t
    if args_turn is None:
        common.log("need a turn: `agrep around <session> <turn>` "
                   "(turns come from `agrep <pattern> --json`).")
        raise SystemExit(2)
    try:
        return s, int(args_turn)
    except ValueError:
        common.log(f"turn must be an integer, got {args_turn!r}.")
        raise SystemExit(2)


def _cap(text: str, limit: int, expand_cmd: str) -> tuple[str, int]:
    """Cap text at a whitespace boundary; the marker carries the command that prints
    the rest, so agents never have to derive the follow-up call."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    cut = text[:limit]
    cut = cut[: cut.rfind(" ")] if " " in cut[limit - 200:] else cut
    omitted = len(text) - len(cut)
    return f"{cut} [+{omitted:,} chars — {expand_cmd}]", omitted


def _ts_label(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def _tool_line(e: dict, color: bool, out_cap: int) -> str:
    inp = " ".join((e.get("input") or "").split())
    if len(inp) > 120:
        inp = inp[:120] + "…"
    if e["kind"] == "subagent_start":
        body = f"⇒ subagent {inp}"
    else:
        mark = "⚙" if e.get("ok", True) else "✗ FAILED"
        size = f" ({e['output_chars']:,}c)" if e.get("output_chars") else ""
        body = f"{mark} {e['name']} {inp}{size}"
    line = f"  {body}"
    if color:
        c = _C["bad"] if not e.get("ok", True) else _C["d"]
        line = f"  {c}{body}{_C['r']}"
    if out_cap > 0 and e.get("output"):
        out = e["output"][:out_cap]
        more = len(e["output"]) - len(out)
        tail = f" [+{more:,} chars]" if more > 0 else ""
        line += "\n" + "\n".join(f"    {ln}" for ln in out.splitlines()) + tail
    return line


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a reconfigurable stream
            pass

    ap = argparse.ArgumentParser(
        prog="agrep around", description="show the conversation around one turn of a chat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep around 00da9752 144            ±4 turns, tool calls inline\n"
               "  agrep around 00da9752:144 -n 10      wider window (colon form ok)\n"
               "  agrep around 00da9752 144 -n 0 --full  one turn, nothing truncated\n"
               "  agrep around 00da9752 144 --tool-output 800  include tool results\n"
               "  agrep around 00da9752 144 --json     one object per message/event\n"
               "\nsession ids and turns come from `agrep <pattern> --json`.\n"
               "exit: 0 ok, 2 bad target / no index.")
    ap.add_argument("session", help="session id: full uuid, 8-char prefix, or session:turn")
    ap.add_argument("turn", nargs="?", help="turn number to center on")
    ap.add_argument("-n", type=int, default=4, metavar="N",
                    help="turns each side of center (default 4)")
    ap.add_argument("--max-chars", type=int, default=4000, metavar="M",
                    help="per-message text cap (default 4000; 0 = uncapped)")
    ap.add_argument("--full", action="store_true", help="uncap all message text")
    ap.add_argument("--no-tools", action="store_true", help="hide tool-call lines")
    ap.add_argument("--tool-output", type=int, default=0, metavar="N",
                    help="include each tool result, capped at N chars (default: hidden)")
    ap.add_argument("--who", choices=("you", "agent"), help="only your turns or only replies")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per message/event (for piping)")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't auto-build a missing index")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = ap.parse_args(argv)

    sess_q, center = _parse_target(args.session, args.turn)
    # build the index on first use rather than dead-ending; ensure_index logs an
    # actionable message and we exit 2 when it can't (or with --no-auto).
    if not common.MESSAGES_PATH.exists():
        if not common.ensure_index(auto=not args.no_auto):
            return 2

    cands = explore.resolve_session(sess_q)
    if not cands:
        common.log(f"no session matches {sess_q!r} — ids come from `agrep <pattern> --json`.")
        return 2
    if len(cands) > 1:
        common.log(f"{sess_q!r} is ambiguous ({len(cands)} sessions):")
        for s in cands[:10]:
            common.log(f"  {s}")
        return 2

    w = explore.get_window(cands[0], center, max(0, args.n))
    if "error" in w:
        common.log(w["error"])
        return 2
    if w["center"] != center and sys.stderr.isatty():
        common.log(f"turn {center} is out of range — centered on {w['center']} "
                   f"(session has turns {w['first_turn']}–{w['last_turn']}).")

    cap = 0 if args.full else args.max_chars
    sess8 = w["session"][:8]
    events_by_turn: dict[int, list[dict]] = {}
    if not args.no_tools:
        for e in w["events"]:
            events_by_turn.setdefault(e["turn"], []).append(e)

    if args.json:
        for t in w["turns"]:
            for who, text in ((t["who"], t["text"]), ("agent", t["reply"])):
                if not text or (args.who and who != args.who
                                and not (args.who == "you" and who == "recap")):
                    continue
                expand = f"agrep around {sess8} {t['turn']} -n 0 --full"
                capped, omitted = _cap(text, cap, expand)
                print(json.dumps({"kind": "msg", "session": w["session"], "turn": t["turn"],
                                  "who": who, "ts": t["ts"], "text": capped,
                                  "omitted_chars": omitted}, ensure_ascii=False))
            for e in events_by_turn.get(t["turn"], []):
                o = {"kind": e["kind"], "turn": e["turn"], "ts": e["ts"], "name": e["name"],
                     "input": e["input"], "ok": e["ok"], "output_chars": e["output_chars"]}
                if args.tool_output > 0:
                    o["output"] = e["output"][: args.tool_output]
                print(json.dumps(o, ensure_ascii=False))
        return 0

    color = _color_on(args.color)
    head = " · ".join(x for x in (sess8, w["agent"], w["project"], w["concept"],
                                  w["title"]) if x)
    span = (f"turns {w['turns'][0]['turn']}–{w['turns'][-1]['turn']}"
            f" of {w['first_turn']}–{w['last_turn']}")
    print(f"{_C['hd']}{head}{_C['r']}  {_C['d']}{span}{_C['r']}" if color
          else f"{head}  {span}")

    for t in w["turns"]:
        bar = f"── turn {t['turn']} " + "─" * 40 + f" {_ts_label(t['ts'])}"
        print(f"{_C['d']}{bar}{_C['r']}" if color else bar)
        expand = f"agrep around {sess8} {t['turn']} -n 0 --full"
        if t["text"] and (not args.who or args.who == "you"):
            tag = t["who"]  # "you" or "recap"
            body, _ = _cap(" ".join(t["text"].split()) if cap else t["text"], cap, expand)
            print(f"{_C['y']}{tag}:{_C['r']} {body}" if color else f"{tag}: {body}")
        for e in events_by_turn.get(t["turn"], []):
            print(_tool_line(e, color, args.tool_output))
        if t["reply"] and (not args.who or args.who == "agent"):
            body, _ = _cap(t["reply"], cap, expand)
            print(f"{_C['a']}agent:{_C['r']} {body}" if color else f"agent: {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
