"""agrep's terminal search - grep your agent history from the shell.

    agrep "rust simd"            # every message across every agent that matches
    agrep deadlock --agent codex # filter to one agent
    agrep -E 'TODO|FIXME'        # regex
    agrep -l auth                # list matching chats, not every line (like grep -l)
    agrep "memory leak" --json   # one JSON object per hit, for piping

Keyword is the default: instant, no model, runs straight off the materialized corpus
(core tier, any python). --semantic upgrades to meaning-search through a running
server. Output is grep-style and pipe-friendly - the match is highlighted only when
stdout is a TTY, a trailing count goes to stderr so stdout stays clean.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import common
import corpusdb
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


def _osc8_ok() -> bool:
    """Does this terminal render OSC 8 hyperlinks? There's no query for it, so use a
    conservative allowlist of terminals known to support them - unknown terminals get
    plain text (a dead-looking link is more annoying than no link). Apple's Terminal.app
    explicitly does NOT support OSC 8. Opt out with AGREP_NO_HYPERLINKS=1."""
    env = os.environ
    if env.get("AGREP_NO_HYPERLINKS"):
        return False
    tp = env.get("TERM_PROGRAM", "")
    if tp == "Apple_Terminal":
        return False
    if any(env.get(k) for k in ("WT_SESSION", "KITTY_WINDOW_ID", "WEZTERM_PANE",
                                "KONSOLE_VERSION", "GHOSTTY_RESOURCES_DIR")):
        return True
    if tp in ("iTerm.app", "WezTerm", "vscode", "Hyper", "ghostty", "rio", "tabby"):
        return True
    vte = env.get("VTE_VERSION", "")
    return vte.isdigit() and int(vte) >= 5000  # VTE 0.50+ (gnome-terminal, tilix, …)


def _reachable(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _server_port() -> int | None:
    """The port of a RUNNING server, from the portfile it drops, verified reachable.
    None if no server is up. Lets the CLI find the server without being told the port."""
    pf = common.DATA_DIR / ".server"
    try:
        port = int(json.loads(pf.read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if _reachable(port) else None


def _osc8(url: str, text: str) -> str:
    return f"\033]8;;{url}\a{text}\033]8;;\a"


def _linker(port: int | None):
    """Returns session -> clickable-URL when links should be emitted (a reachable server
    AND an OSC-8-capable terminal), else None. The URL deep-links the web app to the
    chat (#chat=<id>), so a click opens that conversation in the browser."""
    if not port or not _osc8_ok():
        return None
    base = f"http://127.0.0.1:{port}/#chat="
    return lambda session: base + urllib.parse.quote(session)


def _proj(p: str) -> str:
    """Last path segment of a project dir, for compact display."""
    p = (p or "").rstrip("/\\")
    return re.split(r"[/\\]", p)[-1] if p else "-"


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
    `\\bword\\b` regex over the whole corpus - that regex walks every entry char by char;
    this only pays the boundary check where the substring already hit."""
    ql = q.lower()
    n = len(ql)
    fields = ("session", "agent", "project", "concept", "model", "model_source",
              "turn", "ts", "who")
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
    fields = ("session", "agent", "project", "concept", "model", "model_source",
              "turn", "ts", "who")
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
    CLI call would cost seconds). Queries CHAT level - semantic search's useful unit is
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
        model = o.get("model", "")
        if not model and o.get("session"):
            try:
                model = explore.get_chat(o["session"]).get("model", "")
            except Exception:  # noqa: BLE001 -- model is enrichment only
                model = ""
        snip = o.get("title") or (o.get("summary") or "")[:140] or o.get("text", "")
        hits.append({"session": o.get("session", ""), "agent": o.get("agent", ""),
                     "project": o.get("project", "") or o.get("cwd_project", ""),
                     "model": model, "model_source": "summary" if model else "unknown",
                     "turn": None, "ts": o.get("ts", 0), "who": "",
                     "snippet": snip, "summary": o.get("summary", "")})
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _filtered(hits: list[dict], agent: str | None, project: str | None,
              who: str | None, model: str | None, model_soft: bool) -> list[dict]:
    out = hits
    if agent:
        ag = agent.lower()
        out = [h for h in out if ag in (h.get("agent") or "").lower()]
    if project:
        pr = project.lower()
        out = [h for h in out if pr in (h.get("project") or "").lower()]
    if who:
        out = [h for h in out if h.get("who") == who]
    if model:
        needle = model.lower()
        if model_soft:
            out = [h for h in out if needle in (h.get("model") or "").lower()]
        else:
            out = [h for h in out if needle == (h.get("model") or "").lower()]
    return out


def _group(hits):
    """hits -> OrderedDict[session] = list of hits, preserving first-seen order."""
    from collections import OrderedDict
    g = OrderedDict()
    for h in hits:
        g.setdefault(h["session"], []).append(h)
    return g


def _chat_head(hs0, n, color, link=None):
    """A chat's header line: agent · project · «topic»   sess8 · N hits. When `link` is
    given, the whole header becomes an OSC 8 hyperlink to that chat in the web app."""
    label = hs0.get("concept") or _proj(hs0["project"])
    crumbs = f"{hs0['agent']} · {_proj(hs0['project'])}"
    if label and label != _proj(hs0["project"]):
        crumbs += f" · {label}"
    sess, cnt = hs0["session"][:8], f"{n} hit{'s' if n != 1 else ''}"
    if color:
        s = f"{_C['hd']}{crumbs}{_C['r']}  {_C['d']}{sess} · {cnt}{_C['r']}"
    else:
        s = f"{crumbs}  [{sess} · {cnt}]"
    return _osc8(link(hs0["session"]), s) if link else s


def _emit_grouped(hits, pat, color, link=None):
    """ripgrep-style: a header per chat, its matching turns indented beneath."""
    for i, (_, hs) in enumerate(_group(hits).items()):
        if i:
            print()
        print(_chat_head(hs[0], len(hs), color, link))
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


def _emit_chats(hits, color, link=None):
    """One line per matching chat (grep -l), with topic + hit count."""
    for _, hs in _group(hits).items():
        print(_chat_head(hs[0], len(hs), color, link))


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
               "  agrep bug --model gpt-5            only turns from that exact model\n"
               "  agrep bug --model spark --soft     model contains spark\n"
               "  agrep -w leak                      whole word only\n"
               "  agrep -E 'TODO|FIXME' --who agent  regex, agent replies only\n"
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
    ap.add_argument("--agent", help="only this agent (claude/codex/opencode/antigravity/kimi/cline)")
    ap.add_argument("--project", help="only chats whose project path contains this")
    ap.add_argument("--model", help="only turns from this exact model name")
    ap.add_argument("--soft", "--model-soft", dest="model_soft", action="store_true",
                    help="with --model, substring-match the model name (like *model*)")
    ap.add_argument("--who", choices=("user", "agent", "control", "synthetic", "recap"),
                    help="only real user turns, agent replies, control markers, "
                         "test traffic, or continuation recaps")
    ap.add_argument("-s", "--semantic", action="store_true",
                    help="meaning search: most relevant CHATS via a running server "
                         "(keyword greps message lines; falls back to keyword if no server)")
    ap.add_argument("--strict-semantic", action="store_true",
                    help="with --semantic, exit instead of falling back to keyword search")
    ap.add_argument("--port", type=int, default=None,
                    help="server port for --semantic / links (default: auto-detect a "
                         "running server, else 8732)")
    ap.add_argument("--json", action="store_true", help="one JSON object per hit (for piping)")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't auto-build a missing index")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = ap.parse_args(argv)

    q = " ".join(args.pattern).strip()
    if not q:
        common.log("empty pattern - give me something to grep for.")
        return 2
    color = _color_on(args.color)
    filters = bool(args.agent or args.project or args.who or args.model)
    # Fetch enough to be accurate. Counting, --max 0, or post-filtering all need the
    # FULL match set; otherwise over-fetch a bounded window and trust the engine's
    # true totals (keyword/regex report total/chats computed before the display cap).
    big = 10_000_000 if (args.count or args.max == 0 or filters) else max(args.max * 10, 500)

    # Fresh install / never indexed: build it on first use rather than dead-ending. On a
    # success we fall through into the normal keyword path; ensure_index logs an actionable
    # message and we exit 2 when it can't. (--semantic hits the server, which has its own
    # empty-index handling, so skip the local build there.)
    if not args.semantic and not common.MESSAGES_PATH.exists():
        if not common.ensure_index(auto=not args.no_auto):
            return 2

    # find a running server once: powers --semantic's default port AND clickable links
    running = _server_port()
    sem_used = False
    if args.semantic:
        sem_port = args.port or running or 8732
        res = _semantic(q, big, sem_port)
        if res is None:
            msg = f"no server on :{sem_port} for --semantic; start one with `agrep warm`"
            if args.strict_semantic:
                common.log(msg)
                return 2
            common.log(msg + " - using keyword search instead")
        else:
            sem_used = True
    if not sem_used:
        # indexed engine when the corpus db is available (cold calls skip the 50 MB
        # jsonl parse entirely); identical hit shape, legacy scans as fallback.
        db = corpusdb.connect()
        try:
            if args.word:  # whole-word: substring prefilter + boundary check (fast)
                res = corpusdb.word(db, q, big) if db else _word_scan(q, big)
            elif args.regex:
                try:
                    re.compile(q, re.I)
                except re.error as e:
                    common.log(f"bad regex: {e}")
                    return 2
                res = corpusdb.regex(db, q, big) if db else _regex_scan(q, big)
            else:
                res = corpusdb.keyword(db, q, big) if db else explore.keyword_search(q, big)
        finally:
            if db:
                db.close()

    filtered = _filtered(res["hits"], args.agent, args.project, args.who,
                         args.model, args.model_soft)
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
    else:
        # clickable headers only when a server is up AND the terminal supports OSC 8 AND
        # we're rendering for humans (color) - never on piped/flat output.
        link_port = args.port if (args.port and _reachable(args.port)) else running
        link = _linker(link_port) if color else None
        if args.chats:
            _emit_chats(hits, color, link)
        elif args.flat or not color:
            _emit_flat(hits, pat, color)  # flat TSV machine default (piped/--color never)
        else:
            _emit_grouped(hits, pat, color, link)

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
