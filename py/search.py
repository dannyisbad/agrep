"""agrep's terminal search — grep your agent history from the shell.

    agrep "rust simd"            # every message across every agent that matches
    agrep deadlock --agent codex # filter to one agent
    agrep -E 'TODO|FIXME'        # regex
    agrep -l auth                # list matching chats, not every line (like grep -l)
    agrep "memory leak" --json   # one JSON object per hit, for piping

Keyword is the default: instant, no model, runs straight off the materialized corpus
(core tier, any python). --semantic upgrades to meaning-search through a running
server. Output is grep-style and pipe-friendly — the match is highlighted only when
stdout is a TTY, a trailing count goes to stderr so stdout stays clean.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

import common
import explore

# bold red match (what grep --color uses), dim metadata
_C = {"m": "\033[1;31m", "d": "\033[2m", "a": "\033[36m", "r": "\033[0m"}


def _color_on(when: str) -> bool:
    if when == "always":
        return True
    if when == "never":
        return False
    return sys.stdout.isatty()


def _proj(p: str) -> str:
    """Last path segment of a project dir, for compact display."""
    p = (p or "").rstrip("/\\")
    return re.split(r"[/\\]", p)[-1] if p else "—"


def _hl(snippet: str, pat: re.Pattern | None, color: bool) -> str:
    if not color or pat is None:
        return snippet
    return pat.sub(lambda m: _C["m"] + m.group(0) + _C["r"], snippet)


def _hl_regex(q: str, regex: bool) -> re.Pattern | None:
    """The pattern used to RE-highlight the match inside a snippet. Keyword mode mirrors
    explore.keyword_search's separator-flexible matcher (so 'cyber filter' lights up
    'cyber_filter'); regex mode uses the user's pattern verbatim."""
    try:
        if regex:
            return re.compile(q, re.I)
        toks = [re.escape(t) for t in re.split(r"[\s\-_]+", q.strip()) if t]
        return re.compile(r"[\s\-_]*".join(toks), re.I) if toks else None
    except re.error:
        return None


def _regex_scan(pattern: str, k: int) -> dict:
    """Regex search over the same corpus keyword_search uses (-E mode). Same hit shape."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        common.log(f"bad regex: {e}")
        raise SystemExit(2)
    fields = ("session", "agent", "project", "concept", "turn", "ts", "who")
    hits = []
    for e in explore._kw_corpus():
        m = rx.search(e["low"]) or rx.search(e["text"])
        if m:
            hits.append({**{f: e[f] for f in fields},
                         "snippet": explore._snip_at(e["text"], m.start(), m.end())})
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _semantic(q: str, k: int, port: int) -> dict | None:
    """Meaning search via a running server (the model is warm there; loading it per
    CLI call would cost seconds). Queries CHAT level — semantic search's useful unit is
    the relevant chat, and that endpoint is rich (session/title/summary), unlike the
    sparse message level. Returns None if no server is reachable. The 30s timeout
    covers a cold server lazy-loading its embedder on the first call."""
    url = f"http://127.0.0.1:{port}/search?" + urllib.parse.urlencode(
        {"q": q, "level": "chat", "k": k})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None
    rows = data.get("results") or data.get("hits") or []
    hits = []
    for o in rows:
        snip = o.get("title") or (o.get("summary") or "")[:140] or o.get("text", "")
        hits.append({"session": o.get("session", ""), "agent": o.get("agent", ""),
                     "project": o.get("project", "") or o.get("cwd_project", ""),
                     "turn": None, "ts": o.get("ts", 0), "who": "",
                     "snippet": snip, "summary": o.get("summary", "")})
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _filtered(hits: list[dict], agent: str | None, project: str | None,
              who: str | None) -> list[dict]:
    out = hits
    if agent:
        ag = agent.lower()
        out = [h for h in out if ag in (h.get("agent") or "").lower()]
    if project:
        pr = project.lower()
        out = [h for h in out if pr in (h.get("project") or "").lower()]
    if who:
        out = [h for h in out if (h.get("who") == "agent") == (who == "agent")]
    return out


def _print_hits(hits, pat, color, total, shown_cap):
    for h in hits:
        turn = h.get("turn")
        loc = f"{h['session'][:8]}:{turn}" if turn is not None else h["session"][:8]
        meta = f"{h['agent']} · {_proj(h['project'])} · {loc}"
        snip = _hl(h["snippet"], pat, color)
        who = h.get("who")
        mark = (" → " if who == "agent" else " › ") if who else " "  # side, when known
        if color:
            print(f"{_C['a']}{meta}{_C['r']}{_C['d']}{mark}{_C['r']}{snip}")
        else:
            print(f"{meta}\t{snip}")


def _print_chats(hits, color):
    """One line per matching chat (like grep -l), with hit counts."""
    from collections import Counter, OrderedDict
    by = OrderedDict()
    for h in hits:
        s = h["session"]
        if s not in by:
            by[s] = {"agent": h["agent"], "project": h["project"], "n": 0,
                     "first": h["snippet"]}
        by[s]["n"] += 1
    for s, c in by.items():
        meta = f"{c['agent']} · {_proj(c['project'])} · {s[:8]}"
        cnt = f"({c['n']} hit{'s' if c['n'] != 1 else ''})"
        if color:
            print(f"{_C['a']}{meta}{_C['r']}  {_C['d']}{cnt}{_C['r']}")
        else:
            print(f"{meta}\t{cnt}")


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; snippets are full of em-dashes, smart quotes,
    # and non-breaking hyphens. Force UTF-8 so output renders (and --json never crashes).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a reconfigurable stream (piped/redirected oddly)
            pass

    ap = argparse.ArgumentParser(
        prog="agrep", description="grep your cross-agent chat history")
    ap.add_argument("pattern", nargs="+", help="text to search for (joined with spaces)")
    ap.add_argument("-n", "--max", type=int, default=40, metavar="N",
                    help="show at most N hits (default 40)")
    ap.add_argument("-E", "--regex", action="store_true", help="treat pattern as a regex")
    ap.add_argument("-l", "--chats", action="store_true",
                    help="list matching chats with counts, not every line")
    ap.add_argument("--agent", help="only this agent (claude/codex/opencode/antigravity)")
    ap.add_argument("--project", help="only chats whose project path contains this")
    ap.add_argument("--who", choices=("user", "agent"), help="only your turns or only the agent's")
    ap.add_argument("-s", "--semantic", action="store_true",
                    help="meaning search: most relevant CHATS via a running server "
                         "(keyword greps message lines; falls back to keyword if no server)")
    ap.add_argument("--port", type=int, default=8732, help="server port for --semantic")
    ap.add_argument("--json", action="store_true", help="one JSON object per hit (for piping)")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = ap.parse_args(argv)

    q = " ".join(args.pattern)
    color = _color_on(args.color)
    # over-fetch so post-filtering still has material, then trim to --max
    big = max(args.max * 10, 500)

    # Fresh install / never indexed: a silent "no matches" would be baffling. Point at
    # the fix. (--semantic hits the server, which has its own empty-index handling.)
    if not args.semantic and not common.MESSAGES_PATH.exists():
        cli = "python tilt.py" if common._is_dev_checkout() else "agrep"
        common.log(f"no index yet — run `{cli} index` (or `{cli} up`) to scan your "
                   f"agent stores, then search.")
        return 2

    sem_used = False
    if args.semantic:
        res = _semantic(q, big, args.port)
        if res is None:
            common.log(f"no server on :{args.port} — using keyword search "
                       f"(start one with `agrep serve` for --semantic)")
        else:
            sem_used = True
    if not sem_used:
        res = _regex_scan(q, big) if args.regex else explore.keyword_search(q, big)

    hits = _filtered(res["hits"], args.agent, args.project, args.who)
    n_total = len(hits)
    hits = hits[: args.max]
    pat = None if sem_used else _hl_regex(q, args.regex)

    if args.json:
        for h in hits:
            print(json.dumps(h, ensure_ascii=False))
    elif args.chats:
        _print_chats(hits, color)
    else:
        _print_hits(hits, pat, color, n_total, args.max)

    if not args.json and sys.stderr.isatty():
        n_chats = len({h["session"] for h in hits})
        more = f" (showing {args.max} of {n_total})" if n_total > args.max else ""
        mode = "semantic" if sem_used else ("regex" if args.regex else "keyword")
        common.log(f"{n_total} hit{'s' if n_total != 1 else ''} in {n_chats} "
                   f"chat{'s' if n_chats != 1 else ''} · {mode}{more}")
    return 0 if hits else 1  # grep convention: exit 1 when nothing matched


if __name__ == "__main__":
    raise SystemExit(main())
