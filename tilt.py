#!/usr/bin/env python
"""agrep — grep and explore your cross-agent chat history.

  agrep "race condition"    grep your whole agent history; print matches  (the namesake)
  agrep resume <id>         jump back into a past session in its agent, cd'd there
  agrep up                  build the index, start the server, open the app
  agrep doctor              check what's installed and what each tier needs
  agrep index               just (re)build the index from your agent stores
  agrep reindex             full pipeline: index + embeddings + affect + topics + arcs
  agrep serve               just run the server (it auto-indexes in the background)
  agrep tail                follow live agent events as JSON lines

A bare first argument that isn't a command is treated as a search, so `agrep deadlock`
greps. Bare `agrep` opens the app. In a dev checkout the same commands run as
`python tilt.py <cmd>`. Run `agrep <command> --help` for a command's own options.
"""

from __future__ import annotations

import argparse
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

TILT_RS = common.tilt_rs_bin()


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
    deps (numpy/torch/...), which live in the venv — launching the server with
    whatever python invoked tilt.py silently downgrades every 'meaning' search
    to keyword when that python lacks them (it logs one line and carries on).
    Prefer the venv whenever it exists; plain interpreters still run the core."""
    return common.venv_python()


def _ensure_binary() -> bool:
    if TILT_RS.exists():
        return True
    import shutil
    if not shutil.which("cargo"):
        print("  ! no ingest binary and no cargo on PATH.")
        print("    install Rust (https://rustup.rs) and re-run, or `python tilt.py serve` "
              "to view an existing index.")
        return False
    print("=== first run: building the ingest binary (cargo build --release) ===", flush=True)
    return subprocess.run(["cargo", "build", "--release"], cwd=str(ROOT)).returncode == 0 \
        and TILT_RS.exists()


def _index() -> bool:
    print("=== indexing transcripts ===", flush=True)
    t = time.perf_counter()
    r = subprocess.run([str(TILT_RS), "index", "--agent", "all"], cwd=str(ROOT))
    if r.returncode == 0:
        print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)
    return r.returncode == 0


def _wait_for(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


# --- subcommands ----------------------------------------------------------

def cmd_up(a) -> int:
    if not a.no_index and _ensure_binary():
        _index()
    url = f"http://127.0.0.1:{a.port}"
    print(f"=== serving {url} ===", flush=True)
    srv = subprocess.Popen([_server_python(), str(ROOT / "py" / "server.py"),
                            "--port", str(a.port)], cwd=str(ROOT))
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


def cmd_serve(a) -> int:
    return subprocess.run([_server_python(), str(ROOT / "py" / "server.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_doctor(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "doctor.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_tail(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "tail.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_search(a) -> int:
    # keyword search is core-tier (stdlib over the materialized corpus), so any python
    # runs it; --semantic just queries a running server, no torch in this process.
    return subprocess.run([sys.executable, str(ROOT / "py" / "search.py"), *a.rest],
                          cwd=str(ROOT)).returncode


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

    up = sub.add_parser("up", help="index, serve, and open the app (default)")
    up.add_argument("--port", type=int, default=8732)
    up.add_argument("--no-open", action="store_true", help="don't open the browser")
    up.add_argument("--no-index", action="store_true", help="serve the existing index as-is")
    up.set_defaults(fn=cmd_up)

    di = sub.add_parser("doctor", help="check installed tiers; --fix does safe setup")
    di.set_defaults(fn=cmd_doctor)

    ix = sub.add_parser("index", help="rebuild the base index from your agent stores")
    ix.set_defaults(fn=cmd_index)

    rx = sub.add_parser("reindex", help="full pipeline (embeddings/affect/topics/arcs)")
    rx.set_defaults(fn=cmd_reindex)

    sv = sub.add_parser("serve", help="run the server only (auto-indexes in background)")
    sv.set_defaults(fn=cmd_serve)

    ta = sub.add_parser("tail", help="follow live agent events as JSON lines (turn ends by default)")
    ta.set_defaults(fn=cmd_tail)

    se = sub.add_parser("search", help="grep your chat history (keyword; --semantic for meaning)")
    se.set_defaults(fn=cmd_search)

    rs = sub.add_parser("resume", help="resume a past session in its own agent, cd'd there")
    rs.set_defaults(fn=cmd_resume)

    # The agrep promise: a bare pattern greps. If the first arg isn't a known verb
    # (and isn't a global flag), treat the whole invocation as a search — so
    # `agrep "rust simd"` works, while `agrep up` / `agrep serve --port N` still
    # dispatch. `agrep` alone opens the app; `agrep -h` shows top-level help.
    raw = sys.argv[1:]
    verbs = set(sub.choices)
    if raw and raw[0] not in verbs and raw[0] not in ("-h", "--help", "-V", "--version"):
        return cmd_search(argparse.Namespace(rest=raw))

    # parse_known_args instead of REMAINDER positionals: REMAINDER errors on
    # leading optionals (`tilt serve --port N` never reached the server), and
    # mixing it with parse_known_args scrambles token order. Unknown args pass
    # through to the subcommand verbatim.
    args, unknown = p.parse_known_args()
    args.rest = unknown
    if not getattr(args, "fn", None):
        # bare `tilt` == `tilt up`, the common case
        return cmd_up(argparse.Namespace(port=8732, no_open=False, no_index=False))
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
