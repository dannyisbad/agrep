#!/usr/bin/env python
"""agrep - grep and explore your cross-agent chat history.

  agrep "race condition"    grep your whole agent history; print matches  (the namesake)
  agrep around <id> <turn>  show the conversation around a search hit, tools inline
  agrep resume <id>         jump back into a past session in its agent, cd'd there
  agrep ui                  the explorer (tilt): serve/open; builds index if missing
  agrep doctor              check what's installed and what each tier needs
  agrep index               just (re)build the index from your agent stores
  agrep reindex             full pipeline: index + embeddings + affect + topics + arcs
  agrep serve               just run the server (it auto-indexes in the background)
  agrep warm                serve + preload semantic models for fast meaning search
  agrep tail                follow live agent events as JSON lines

A bare first argument that isn't a command is treated as a search, so `agrep deadlock`
greps. Bare `agrep` prints status + usage (it never starts a server). In a dev checkout
the same commands run as `python cli.py <cmd>`. Run `agrep <command> --help` for a
command's own options.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
sys.path.insert(0, str(ROOT / "py"))
import common  # noqa: E402  -- single source for binary / venv / data paths

INGEST_BIN = common.ingest_bin()


def _version() -> str:
    """Single-sourced from agrep/__init__.py (pyproject reads the same file).
    Resolves both installed (site-packages/agrep) and dev (repo root/agrep)."""
    try:
        from agrep import __version__
        return __version__
    except ImportError:
        return "dev"


def _server_python() -> str:
    """The python the SERVER runs under. Semantic search needs the smart tier's
    deps (numpy/torch/...), which live in the venv - launching the server with
    whatever python invoked us silently downgrades every 'meaning' search
    to keyword when that python lacks them (it logs one line and carries on).
    Prefer the venv whenever it exists; plain interpreters still run the core."""
    return common.venv_python()


def _ensure_binary() -> bool:
    if INGEST_BIN.exists():
        return True
    import shutil
    if not shutil.which("cargo"):
        print("  ! no ingest binary and no cargo on PATH.")
        print(f"    install Rust (https://rustup.rs) and re-run, or `{common.cli_name()} serve` "
              "to view an existing index.")
        return False
    print("=== first run: building the ingest binary (cargo build --release) ===", flush=True)
    return subprocess.run(["cargo", "build", "--release"], cwd=str(ROOT)).returncode == 0 \
        and INGEST_BIN.exists()


def _index() -> bool:
    # the ingest invocation + derived-db refresh live in common.build_index() (shared
    # with the auto-index-on-first-search path); here we just wrap it with progress.
    print("=== indexing transcripts ===", flush=True)
    t = time.perf_counter()
    ok = common.build_index()
    if ok:
        print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)
    return ok


def _wait_for(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


# --- status (bare `agrep`) ------------------------------------------------

def _fmt_age(seconds: float) -> str:
    """Compact human age: '3s', '12m', '5h', '2d' ago. Coarse on purpose - the
    status line wants a glance, not a stopwatch."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _status_lines(cli: str) -> list[str]:
    """Cheap index summary for the bare-`agrep` banner. Reads sessions.jsonl (one
    line per chat, with the per-chat message count `n` and `agent`) - never parses
    messages.jsonl, which is ~50 MB. mtime of messages.jsonl dates the last index;
    corpus.db's presence + size says whether keyword search is ready; the smart-tier
    venv's existence says whether meaning search is installed."""
    sessions = common.DATA_DIR / "sessions.jsonl"
    out: list[str] = [f"  data dir: {common.DATA_DIR} ({common.data_dir_source()})"]
    for warning in common.data_dir_warnings():
        out.append(f"  warning: {warning}")
    if not sessions.exists():
        out.append(f"  no index yet - any search will build it on first run, "
                   f"or run `{cli} index`.")
        return out

    n_sessions = n_messages = 0
    agents: set[str] = set()
    with sessions.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_sessions += 1
            n_messages += int(o.get("n", 0))
            a = o.get("agent")
            if a:
                agents.add(a)

    ag = ", ".join(sorted(agents)) if agents else "-"
    out.append(f"  {n_messages:,} messages · {n_sessions:,} sessions · "
               f"{len(agents)} agent{'s' if len(agents) != 1 else ''} ({ag})")

    msgs = common.MESSAGES_PATH
    if msgs.exists():
        out.append(f"  last indexed {_fmt_age(time.time() - msgs.stat().st_mtime)} ago")

    db = common.DATA_DIR / "corpus.db"
    ready = db.exists() and db.stat().st_size > 0
    out.append(f"  search index: {'ready' if ready else 'missing (builds on first search)'}")
    out.append(f"  freshness: {common.freshness_status()}")

    smart = "installed" if Path(common.venv_python()) != Path(sys.executable) else \
        f"not installed (keyword only; `{cli} doctor` to add meaning search)"
    out.append(f"  smart tier: {smart}")
    return out


def cmd_status(a) -> int:
    """Bare `agrep`: print where the index stands and how to drive the tool, then exit.
    Deliberately does NOT start a server - the explorer is `agrep ui`."""
    # the banner carries em-dashes / middots; windows consoles default to cp1252.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a reconfigurable stream (piped oddly)
            pass
    cli = common.cli_name()  # `python cli.py` in a dev checkout, `agrep` once installed
    print("agrep - grep and explore your cross-agent chat history\n")
    for line in _status_lines(cli):
        print(line)
    print("\ntry:")
    print(f'  {cli} "race condition"        grep every agent for a phrase')
    print(f"  {cli} deadlock --agent codex  filter to one agent")
    print(f"  {cli} -E 'TODO|FIXME'         regex search")
    print(f"  {cli} -l auth                 which chats mention it")
    print(f"  {cli} around <id> <turn>      the conversation around a hit")
    print(f"  {cli} resume <id>             reopen a past session in its agent")
    print(f"  {cli} ui                      the explorer: serve/open, auto-refresh")
    print(f"  {cli} warm                    keep semantic search warm in this terminal")
    print(f"\n{cli} <command> -h for a command's own options.")
    return 0


# --- subcommands ----------------------------------------------------------

def cmd_up(a) -> int:
    if not a.no_index:
        need_index = not common.MESSAGES_PATH.exists()
        if getattr(a, "force_index", False) or need_index:
            if not _ensure_binary():
                return 1
            if not _index():
                return 1
        else:
            print("=== using existing index; server will refresh in the background ===",
                  flush=True)
    url = f"http://127.0.0.1:{a.port}"
    print(f"=== serving {url} ===", flush=True)
    cmd = [_server_python(), str(ROOT / "py" / "server.py"), "--port", str(a.port)]
    if getattr(a, "warm", False):
        cmd.append("--warm")
    srv = subprocess.Popen(cmd, cwd=str(ROOT))
    try:
        if not _wait_for(a.port):
            print("  ! server didn't come up within 30s; see the output above.")
            srv.terminate()
            return 1
        if not a.no_open:
            webbrowser.open(url)
        print("  ctrl-c stops the server.", flush=True)
        return srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
        return 0


def cmd_index(a) -> int:
    if not _ensure_binary():
        return 1
    return 0 if _index() else 1


def cmd_reindex(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "reindex.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def _server_args(a, *, force_warm: bool = False) -> list[str]:
    args = ["--port", str(a.port)]
    if force_warm or getattr(a, "warm", False):
        args.append("--warm")
    if getattr(a, "no_autoindex", False):
        args.append("--no-autoindex")
    return [*args, *getattr(a, "rest", [])]


def cmd_serve(a) -> int:
    return subprocess.run([_server_python(), str(ROOT / "py" / "server.py"),
                           *_server_args(a)],
                          cwd=str(ROOT)).returncode


def cmd_warm(a) -> int:
    return subprocess.run([_server_python(), str(ROOT / "py" / "server.py"),
                           *_server_args(a, force_warm=True)],
                          cwd=str(ROOT)).returncode


def cmd_doctor(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "doctor.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_tail(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "tail.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_search(a) -> int:
    # in-process (stdlib-only, like resume): spawning a second interpreter doubled
    # the cold-start cost of the single hottest command. --semantic just queries a
    # running server, so no torch in this process either way.
    import search
    return search.main(a.rest)


def cmd_around(a) -> int:
    # core-tier like search: stdlib over the materialized index, runs in-process.
    import around
    return around.main(a.rest)


def cmd_resume(a) -> int:
    # imported and called in-process (not a subprocess) so the resumed agent is a direct
    # child of this process and cleanly inherits the terminal.
    import resume
    return resume.main(a.rest)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="agrep", description="grep and explore your cross-agent chat history")
    p.add_argument("-V", "--version", action="version", version=f"agrep {_version()}")
    sub = p.add_subparsers(dest="cmd")

    # `ui` is the explorer (tilt): serve + open, building the base index only when
    # it is missing. `up` is a kept-working alias
    # (it lives in scripts / muscle memory) but only `ui` is advertised - a subparser
    # added without help= is omitted from the command listing, so `up` stays hidden.
    for name in ("ui", "up"):
        kw = {"help": "serve and open the explorer (tilt)"} if name == "ui" else {}
        u = sub.add_parser(name, **kw)
        u.add_argument("--port", type=int, default=8732)
        u.add_argument("--warm", action="store_true",
                       help="preload semantic models after opening the explorer")
        u.add_argument("--no-open", action="store_true", help="don't open the browser")
        u.add_argument("--no-index", action="store_true", help="serve the existing index as-is")
        u.add_argument("--force-index", action="store_true",
                       help="rebuild the base index before opening")
        u.set_defaults(fn=cmd_up)

    di = sub.add_parser("doctor", help="check installed tiers; --fix does safe setup")
    di.set_defaults(fn=cmd_doctor)

    ix = sub.add_parser("index", help="rebuild the base index from your agent stores")
    ix.set_defaults(fn=cmd_index)

    rx = sub.add_parser("reindex", help="full pipeline (embeddings/affect/topics/arcs)")
    rx.set_defaults(fn=cmd_reindex)

    sv = sub.add_parser("serve", help="run the server only (auto-indexes in background)")
    sv.add_argument("--port", type=int, default=8732)
    sv.add_argument("--warm", action="store_true",
                    help="pre-load semantic models at startup")
    sv.add_argument("--no-autoindex", action="store_true",
                    help="don't auto-rebuild the index on new activity")
    sv.set_defaults(fn=cmd_serve)

    wm = sub.add_parser("warm", help="run the server and preload semantic models")
    wm.add_argument("--port", type=int, default=8732)
    wm.add_argument("--no-autoindex", action="store_true",
                    help="don't auto-rebuild the index on new activity")
    wm.set_defaults(fn=cmd_warm)

    ta = sub.add_parser("tail", help="follow live agent events as JSON lines (turn ends by default)")
    ta.set_defaults(fn=cmd_tail)

    se = sub.add_parser("search", help="grep your chat history (keyword; --semantic for meaning)")
    se.set_defaults(fn=cmd_search)

    ar = sub.add_parser("around", help="show the conversation around one turn of a chat")
    ar.set_defaults(fn=cmd_around)

    rs = sub.add_parser("resume", help="resume a past session in its own agent, cd'd there")
    rs.set_defaults(fn=cmd_resume)

    # The agrep promise: a bare pattern greps. If the first arg isn't a known verb
    # (and isn't a global flag), treat the whole invocation as a search - so
    # `agrep "rust simd"` works, while `agrep ui` / `agrep serve --port N` still
    # dispatch. `agrep` alone prints status + usage; `agrep -h` shows top-level help.
    raw = sys.argv[1:]
    verbs = set(sub.choices)
    delegated = {
        "doctor": cmd_doctor,
        "reindex": cmd_reindex,
        "resume": cmd_resume,
        "search": cmd_search,
        "tail": cmd_tail,
        "around": cmd_around,
    }
    if (raw and raw[0] in delegated
            and any(x in ("-h", "--help") for x in raw[1:])):
        return delegated[raw[0]](argparse.Namespace(rest=raw[1:]))

    if raw and raw[0] not in verbs and raw[0] not in ("-h", "--help", "-V", "--version"):
        return cmd_search(argparse.Namespace(rest=raw))

    # parse_known_args instead of REMAINDER positionals: REMAINDER errors on
    # leading optionals (`agrep serve --port N` never reached the server), and
    # mixing it with parse_known_args scrambles token order. Unknown args pass
    # through to the subcommand verbatim.
    args, unknown = p.parse_known_args()
    args.rest = unknown
    if not getattr(args, "fn", None):
        # bare `agrep` prints status + usage and exits - the explorer is `agrep ui`.
        return cmd_status(args)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
