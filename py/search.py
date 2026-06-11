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

# bold red match (what grep --color uses), bold-cyan chat header, dim metadata
_C = {"m": "\033[1;31m", "hd": "\033[1;36m", "d": "\033[2m", "a": "\033[36m",
      "y": "\033[33m", "r": "\033[0m"}


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


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _word_scan(q: str, k: int) -> dict:
    """Whole-word search, the fast way: a C-level substring `.find` prefilter, then a
    boundary check only on the (few) entries that contain the word. Far cheaper than a
    `\\bword\\b` regex over the whole corpus — that regex walks every entry char by char;
    this only pays the boundary check where the substring already hit."""
    ql = q.lower()
    n = len(ql)
    fields = ("session", "agent", "project", "concept", "turn", "ts", "who")
    hits = []
    for e in explore._kw_corpus():
        low = e["low"]
        i = low.find(ql)
        while i >= 0:
            j = i + n
            if (i == 0 or not _is_word_char(low[i - 1])) and \
               (j >= len(low) or not _is_word_char(low[j])):
                hits.append({**{f: e[f] for f in fields},
                             "snippet": explore._snip_at(e["text"], i, j)})
                break  # one hit per entry, like keyword_search
            i = low.find(ql, i + 1)
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


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


def _group(hits):
    """hits -> OrderedDict[session] = list of hits, preserving first-seen order."""
    from collections import OrderedDict
    g = OrderedDict()
    for h in hits:
        g.setdefault(h["session"], []).append(h)
    return g


def _chat_head(hs0, n, color):
    """A chat's header line: agent · project · «topic»   sess8 · N hits."""
    label = hs0.get("concept") or _proj(hs0["project"])
    crumbs = f"{hs0['agent']} · {_proj(hs0['project'])}"
    if label and label != _proj(hs0["project"]):
        crumbs += f" · {label}"
    sess, cnt = hs0["session"][:8], f"{n} hit{'s' if n != 1 else ''}"
    if color:
        return f"{_C['hd']}{crumbs}{_C['r']}  {_C['d']}{sess} · {cnt}{_C['r']}"
    return f"{crumbs}  [{sess} · {cnt}]"


def _emit_grouped(hits, pat, color):
    """ripgrep-style: a header per chat, its matching turns indented beneath."""
    for i, (_, hs) in enumerate(_group(hits).items()):
        if i:
            print()
        print(_chat_head(hs[0], len(hs), color))
        for h in hs:
            turn = h.get("turn")
            tn = str(turn) if turn is not None else "·"
            who = h.get("who")
            mark = "→" if who == "agent" else "›" if who else " "
            snip = _hl(h["snippet"], pat, color)
            if color:
                print(f"  {_C['y']}{tn:>4}{_C['r']} {_C['d']}{mark}{_C['r']} {snip}")
            else:
                print(f"  {tn:>4} {mark} {snip}")


def _emit_flat(hits, pat, color):
    """One TAB-separated row per hit for piping: session, agent, project, turn, who,
    snippet. Stable columns so awk/cut compose; this is the default when piped."""
    for h in hits:
        turn = h.get("turn")
        print("\t".join([h["session"], h["agent"], _proj(h["project"]),
                         "" if turn is None else str(turn), h.get("who") or "",
                         _hl(h["snippet"], pat, color)]))


def _emit_chats(hits, color):
    """One line per matching chat (grep -l), with topic + hit count."""
    for _, hs in _group(hits).items():
        print(_chat_head(hs[0], len(hs), color))


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; snippets are full of em-dashes, smart quotes,
    # and non-breaking hyphens. Force UTF-8 so output renders (and --json never crashes).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a reconfigurable stream (piped/redirected oddly)
            pass

    ap = argparse.ArgumentParser(
        prog="agrep", description="grep your cross-agent chat history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep \"race condition\"            grep every agent for a phrase\n"
               "  agrep deadlock --agent codex       just codex\n"
               "  agrep -w leak                      whole word only\n"
               "  agrep -E 'TODO|FIXME' --who agent  regex, agent turns only\n"
               "  agrep -l auth                      which chats mention it\n"
               "  agrep -c oom                       just the count\n"
               "  agrep \"flaky test\" -s              meaning search (needs a server)\n"
               "  agrep memory --json | jq .         pipe structured hits\n"
               "\nsearch is case-insensitive. exit: 0 found, 1 none, 2 no index yet.")
    ap.add_argument("pattern", nargs="+", help="text to search for (joined with spaces)")
    ap.add_argument("-n", "--max", type=int, default=40, metavar="N",
                    help="show at most N hits (default 40; 0 = no limit)")
    ap.add_argument("-E", "--regex", action="store_true", help="treat pattern as a regex")
    ap.add_argument("-w", "--word", action="store_true", help="match whole words only")
    ap.add_argument("-i", "--ignore-case", action="store_true",
                    help="(default; search is always case-insensitive)")
    ap.add_argument("-l", "--chats", action="store_true",
                    help="list matching chats, not every line (like grep -l)")
    ap.add_argument("-c", "--count", action="store_true",
                    help="print only the match count (like grep -c)")
    ap.add_argument("--flat", action="store_true",
                    help="one tab-separated row per hit (the default when piped)")
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

    q = " ".join(args.pattern).strip()
    if not q:
        common.log("empty pattern — give me something to grep for.")
        return 2
    color = _color_on(args.color)
    filters = bool(args.agent or args.project or args.who)
    # Fetch enough to be accurate. Counting, --max 0, or post-filtering all need the
    # FULL match set; otherwise over-fetch a bounded window and trust the engine's
    # true totals (keyword/regex report total/chats computed before the display cap).
    big = 10_000_000 if (args.count or args.max == 0 or filters) else max(args.max * 10, 500)

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
        if args.word:  # whole-word: substring prefilter + boundary check (fast)
            res = _word_scan(q, big)
        elif args.regex:
            res = _regex_scan(q, big)
        else:
            res = explore.keyword_search(q, big)

    filtered = _filtered(res["hits"], args.agent, args.project, args.who)
    # true totals: the engine reports them pre-cap; with filters we fetched everything
    if filters:
        n_total = len(filtered)
        n_chats = len({h["session"] for h in filtered})
    else:
        n_total = res.get("total", len(filtered))
        n_chats = res.get("chats", len({h["session"] for h in filtered}))
    hits = filtered if args.max == 0 else filtered[: args.max]

    # highlight pattern mirrors the search mode (none for semantic chat titles)
    if sem_used:
        pat = None
    elif args.word:
        pat = re.compile(r"\b" + re.escape(q) + r"\b", re.I)
    else:
        pat = _hl_regex(q, args.regex)

    # output: count -> json -> chats -> grouped(tty) / flat(pipe or --flat)
    if args.count:
        print(f"{n_total}")
    elif args.json:
        for h in hits:
            print(json.dumps(h, ensure_ascii=False))
    elif args.chats:
        _emit_chats(hits, color)
    elif args.flat or not color:
        # flat TSV is the machine default (piped, or --color never, or --flat)
        _emit_flat(hits, pat, color)
    else:
        _emit_grouped(hits, pat, color)

    if not args.json and not args.count and sys.stderr.isatty():
        more = f", showing {args.max}" if args.max and n_total > args.max else ""
        mode = "semantic" if sem_used else "regex" if args.regex else \
            "word" if args.word else "keyword"
        kind = "chat" if sem_used else "hit"
        common.log(f"{n_total} {kind}{'s' if n_total != 1 else ''} in {n_chats} "
                   f"chat{'s' if n_chats != 1 else ''} · {mode}{more}")
    return 0 if hits else 1  # grep convention: exit 1 when nothing matched


if __name__ == "__main__":
    raise SystemExit(main())
