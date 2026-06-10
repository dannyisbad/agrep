#!/usr/bin/env python
"""tilt up — the one command: index what's new, start the server, open the browser.

This is the fast path. It does NOT run the ML stages (embeddings / affect / summaries);
those come from `python reindex.py` when you want them. A fresh clone with nothing but
Python + Rust gets a fully working explorer from this command alone: browse, keyword
search, chat detail, event trees, live view, native resume.

    python up.py                 # index -> serve -> open browser
    python up.py --port 9000     # different port
    python up.py --no-open       # don't launch the browser
    python up.py --no-index      # serve what's already indexed

The Rust binary is built automatically on first run (needs cargo; rustup.rs).
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
VENV_PY = ROOT / "py" / ".venv" / ("Scripts" if WIN else "bin") / ("python.exe" if WIN else "python")
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
TILT = ROOT / "target" / "release" / ("tilt.exe" if WIN else "tilt")


def ensure_binary() -> bool:
    if TILT.exists():
        return True
    if not shutil.which("cargo"):
        print("  ! no tilt binary and no cargo on PATH.")
        print("    install rust (https://rustup.rs) and re-run, or skip indexing with --no-index.")
        return False
    print("=== first run: building the ingest binary (cargo build --release) ===", flush=True)
    r = subprocess.run(["cargo", "build", "--release"], cwd=str(ROOT))
    return r.returncode == 0 and TILT.exists()


def wait_for(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="index what's new, serve, open the browser")
    ap.add_argument("--port", type=int, default=8732)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--no-index", action="store_true", help="serve the existing index as-is")
    args = ap.parse_args()

    if not args.no_index:
        if ensure_binary():
            print("=== indexing transcripts ===", flush=True)
            t = time.perf_counter()
            r = subprocess.run([str(TILT), "index", "--agent", "all"], cwd=str(ROOT))
            if r.returncode != 0:
                print("  ! ingest failed; serving whatever index exists.", flush=True)
            else:
                print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)
        else:
            print("  continuing without indexing.", flush=True)

    url = f"http://127.0.0.1:{args.port}"
    print(f"=== serving {url} ===", flush=True)
    srv = subprocess.Popen([PY, str(ROOT / "py" / "server.py"), "--port", str(args.port)],
                           cwd=str(ROOT))
    try:
        if not wait_for(args.port):
            print("  ! server didn't come up within 30s; check the output above.")
            srv.terminate()
            return 1
        if not args.no_open:
            webbrowser.open(url)
        print("  ctrl-c stops the server.", flush=True)
        return srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
