"""One-click setup: the panel's install buttons actually run the installs.

The old setup panel printed commands and hoped. These jobs run them server-side
with progress the UI polls (GET /setup/state). One job at a time. Steps:

  smart   create py/.venv (if missing) + pip install -r requirements.txt.
          Always flags restart_needed on success: the running server's import
          state predates the new deps, so it relaunches itself under the venv
          (POST /setup/restart) instead of telling anyone to run a command.
  named   install Ollama if it isn't reachable (winget on Windows, brew on
          macOS; Linux keeps the manual script -- it wants sudo), start it,
          pull the summarizer model via /api/pull (streamed, real %), then
          trigger the indexer so titles appear without further clicks.

Everything here is stdlib-only on purpose -- setup must work before setup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import common

ROOT = common.REPO_ROOT
WIN = sys.platform == "win32"
VENV_DIR = ROOT / "py" / ".venv"
VENV_PY = VENV_DIR / ("Scripts" if WIN else "bin") / ("python.exe" if WIN else "python")
PULL_MODEL = "qwen2.5:3b-instruct"  # smallest entry in summarize.MODELS -- keep in sync
OLLAMA_TAGS = "http://localhost:11434/api/tags"
OLLAMA_PULL = "http://localhost:11434/api/pull"

_MUT = threading.Lock()
STATE = {
    "step": "",          # smart | named
    "phase": "idle",     # idle | running | ok | err
    "label": "",         # current sub-action ("installing python deps")
    "line": "",          # last output line from the underlying tool
    "pct": None,         # 0-100 when the tool reports real progress, else None
    "err": "",
    "restart_needed": False,
}


def _set(**kw) -> None:
    with _MUT:
        STATE.update(kw)


def state() -> dict:
    with _MUT:
        s = dict(STATE)
    s["platform"] = sys.platform
    s["can_install_ollama"] = sys.platform in ("win32", "darwin")
    try:
        s["under_venv"] = VENV_PY.exists() and Path(sys.executable).resolve() == VENV_PY.resolve()
    except OSError:
        s["under_venv"] = False
    return s


def start(step: str) -> dict:
    fn = {"smart": _smart, "named": _named}.get(step)
    if not fn:
        return {"ok": False, "error": f"unknown step {step!r}"}
    with _MUT:
        if STATE["phase"] == "running":
            return {"ok": False, "error": "a setup job is already running"}
        STATE.update(step=step, phase="running", label="", line="", pct=None, err="")
    threading.Thread(target=_run, args=(fn,), daemon=True, name="tilt-setup").start()
    return {"ok": True}


def _run(fn) -> None:
    try:
        fn()
        _set(phase="ok", label="", pct=None)
    except Exception as e:  # noqa: BLE001 -- whatever broke, the card shows it verbatim
        common.log(f"setup job failed: {e}")
        _set(phase="err", err=str(e)[-300:], pct=None)


def _stream(cmd: list[str], label: str) -> None:
    """Run a tool, mirroring its output into STATE['line'] for the progress card."""
    _set(label=label, line="")
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    for ln in p.stdout:  # type: ignore[union-attr]
        ln = ln.strip()
        if ln:
            _set(line=ln[-180:])
    p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {p.returncode}): {STATE['line']}")


# ---- smart tier --------------------------------------------------------------

def _smart() -> None:
    if not VENV_PY.exists():
        _stream([sys.executable, "-m", "venv", str(VENV_DIR)], "creating py/.venv")
    _stream([str(VENV_PY), "-m", "pip", "install", "-r", "requirements.txt",
             "--progress-bar", "off"],
            "installing python deps (torch is the big one, ~2.5GB)")
    # The running server's failed `import ask` is cached and its interpreter may not
    # even be the venv -- a relaunch is the only honest way to load what we installed.
    _set(restart_needed=True)


# ---- named tier --------------------------------------------------------------

def _ollama_up() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


def _find_ollama() -> str | None:
    cands = [shutil.which("ollama")]
    if WIN:
        # winget's per-user install lands here; the server's PATH predates it
        cands.append(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "Programs", "Ollama", "ollama.exe"))
    else:
        cands += ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
    return next((c for c in cands if c and Path(c).exists()), None)


def _start_ollama() -> None:
    if _ollama_up():
        return
    exe = _find_ollama()
    if not exe:
        raise RuntimeError("ollama installed but the binary wasn't found -- "
                           "start it once by hand, then retry")
    _set(label="starting ollama", line="")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL}
    if WIN:
        kw["creationflags"] = 0x00000208  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([exe, "serve"], **kw)
    for _ in range(30):
        if _ollama_up():
            return
        time.sleep(1)
    raise RuntimeError("ollama didn't come up within 30s -- start it by hand, then retry")


def _named() -> None:
    if not _ollama_up():
        if _find_ollama():
            _start_ollama()
        elif WIN:
            _stream(["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
                     "--accept-package-agreements", "--accept-source-agreements"],
                    "installing ollama (winget)")
            _start_ollama()
        elif sys.platform == "darwin":
            if not shutil.which("brew"):
                raise RuntimeError("brew not found -- install ollama from ollama.com, "
                                   "then retry")
            _stream(["brew", "install", "ollama"], "installing ollama (brew)")
            _start_ollama()
        else:
            raise RuntimeError("ollama's installer needs sudo on linux -- run "
                               "`curl -fsSL https://ollama.com/install.sh | sh`, then retry")
    _pull()
    # the whole point: titles appear without anyone finding the reindex button
    import indexer
    idx = indexer.instance()
    if idx:
        idx.trigger()


def _pull() -> None:
    _set(label=f"pulling {PULL_MODEL}", line="", pct=0)
    req = urllib.request.Request(OLLAMA_PULL,
                                 data=json.dumps({"name": PULL_MODEL}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for raw in r:
            try:
                o = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if o.get("error"):
                raise RuntimeError(o["error"])
            tot, comp = o.get("total"), o.get("completed")
            pct = round(comp / tot * 100, 1) if tot and comp is not None else None
            _set(line=str(o.get("status", ""))[-180:],
                 **({"pct": pct} if pct is not None else {}))
    _set(pct=100)
