#!/usr/bin/env python
"""tilt — one command for your cross-agent chat history.

  python tilt.py up         build the index, start the server, open the app  (default)
  python tilt.py doctor     check what's installed and what each tier needs
  python tilt.py index      just (re)build the index from your agent stores
  python tilt.py reindex    full pipeline: index + embeddings + affect + topics + arcs
  python tilt.py serve      just run the server (it auto-indexes in the background)

(On Unix `./tilt <cmd>` works via the wrapper; on Windows `tilt <cmd>` via tilt.cmd.)
Run `python tilt.py <command> --help` for a command's own options.
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
TILT_RS = ROOT / "target" / "release" / ("tilt-rs.exe" if WIN else "tilt-rs")


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
    srv = subprocess.Popen([sys.executable, str(ROOT / "py" / "server.py"),
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
    return subprocess.run([sys.executable, str(ROOT / "py" / "server.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_doctor(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "doctor.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_tail(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "py" / "tail.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def main() -> int:
    p = argparse.ArgumentParser(
        prog="tilt", description="one command for your cross-agent chat history")
    sub = p.add_subparsers(dest="cmd")

    up = sub.add_parser("up", help="index, serve, and open the app (default)")
    up.add_argument("--port", type=int, default=8732)
    up.add_argument("--no-open", action="store_true", help="don't open the browser")
    up.add_argument("--no-index", action="store_true", help="serve the existing index as-is")
    up.set_defaults(fn=cmd_up)

    di = sub.add_parser("doctor", help="check installed tiers; --fix does safe setup")
    di.add_argument("rest", nargs=argparse.REMAINDER)
    di.set_defaults(fn=cmd_doctor)

    ix = sub.add_parser("index", help="rebuild the base index from your agent stores")
    ix.set_defaults(fn=cmd_index)

    rx = sub.add_parser("reindex", help="full pipeline (embeddings/affect/topics/arcs)")
    rx.add_argument("rest", nargs=argparse.REMAINDER)
    rx.set_defaults(fn=cmd_reindex)

    sv = sub.add_parser("serve", help="run the server only (auto-indexes in background)")
    sv.add_argument("rest", nargs=argparse.REMAINDER)
    sv.set_defaults(fn=cmd_serve)

    ta = sub.add_parser("tail", help="follow live agent events as JSON lines (turn ends by default)")
    ta.add_argument("rest", nargs=argparse.REMAINDER)
    ta.set_defaults(fn=cmd_tail)

    args = p.parse_args()
    if not getattr(args, "fn", None):
        # bare `tilt` == `tilt up`, the common case
        return cmd_up(argparse.Namespace(port=8732, no_open=False, no_index=False))
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
