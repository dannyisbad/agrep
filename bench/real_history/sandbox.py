"""Sealed subprocess environment for running the checkout's CLI against a chosen store home."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / ("agrep-rs.exe" if sys.platform == "win32" else "agrep-rs")
# Home-relative store roots, mirroring crates/agrep-core/src/ingest/registry.rs adapters.
STORE_ROOTS = {
    "claude": (".claude/projects",),
    "codex": (".codex/sessions",),
    "pi": (".pi/agent/sessions", ".pi/agent/archive/sessions",
           ".omp/agent/sessions", ".omp/agent/archive/sessions"),
    "opencode": (".local/share/opencode",),
    "antigravity": (".gemini/antigravity-cli",),
    "kimi": (".kimi/sessions",),
    "cline": (".cline/tasks",),
    "gemini": (".gemini/tmp",),
    "crush": (".local/share/crush",),
    "cursor": ("Library/Application Support/Cursor/User/globalStorage/state.vscdb",
               ".config/Cursor/User/globalStorage/state.vscdb"),
}
# Fixture directories are stored without the leading dot; the sandbox home gets the real name.
FIXTURE_HOME_DIRS = {"claude": ".claude", "codex": ".codex", "pi": ".pi", "omp": ".omp"}
OPENCODE_DB = Path(".local/share/opencode/opencode.db")


def production_data_dir(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "agrep"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "agrep"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "agrep"


def resolve_binary() -> Path:
    override = os.environ.get("AGREP_RS_BIN")
    return Path(override) if override else RELEASE_BIN


def sealed_env(*, home: Path, data: Path, scratch: Path, binary: Path,
               store_home: Path | None = None) -> dict[str, str]:
    """Only the listed variables exist in the child; nothing of the caller's session leaks in."""
    runtime = scratch / "rt"
    for name in ("tmp", "rt", "config", "cache", "share", "models", "callers"):
        (scratch / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    store_home = store_home or home
    return {
        "HOME": str(store_home), "AGREP_HOME": str(store_home),
        "AGREP_DATA_DIR": str(data), "AGREP_DATA_DIR_SOURCE": "env",
        "TMPDIR": str(scratch / "tmp"), "XDG_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(scratch / "config"), "XDG_CACHE_HOME": str(scratch / "cache"),
        "AGREP_MODEL_DIR": str(scratch / "models"),
        "AGREP_CALLER_PUBLICATION_DIR": str(scratch / "callers"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb",
        "AGREP_NO_DAEMON": "1", "AGREP_NO_RESIDENT": "1", "AGREP_NO_SEM_WORKER": "1",
        "AGREP_NO_FETCH": "1", "AGREP_RS_BIN": str(binary),
        "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


class CliRunner:
    """Runs cli.py and agrep-rs under one sealed environment and reaps any background child."""

    def __init__(self, env: dict[str, str], cwd: Path, python: str = sys.executable,
                 default_timeout: float = 600):
        self.env = env
        self.cwd = cwd
        self.python = python
        self.default_timeout = default_timeout
        self.timings: list[tuple[str, float]] = []

    def cli(self, argv, *, timeout: float | None = None, label: str | None = None):
        return self._run([self.python, str(CLI), *argv], timeout=timeout, label=label or argv[0])

    def rs(self, argv, *, timeout: float | None = None):
        return self._run([self.env["AGREP_RS_BIN"], *argv], timeout=timeout, label="rs " + argv[0])

    def _run(self, command, *, timeout, label):
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=self.cwd, env=self.env, input="", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout or self.default_timeout,
            check=False)
        self.timings.append((label, time.monotonic() - started))
        return result

    def json_rows(self, result) -> list[dict]:
        rows = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                rows.append(json.loads(line))
        return rows

    def background_pids(self) -> set[int]:
        """Children still bound to this data dir; POSIX ps shows argv only, Linux adds /proc environ."""
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "args="], env={"PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            check=True)
        data = self.env["AGREP_DATA_DIR"]
        marker = f"AGREP_DATA_DIR={data}".encode()
        pids = set()
        for line in listing.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or int(fields[0]) == os.getpid():
                continue
            pid = int(fields[0])
            if data in fields[1]:
                pids.add(pid)
                continue
            environ = Path("/proc") / str(pid) / "environ"
            try:
                if environ.exists() and marker in environ.read_bytes().split(b"\0"):
                    pids.add(pid)
            except OSError:
                continue
        return pids

    def reap(self) -> int:
        stopped = 0
        for sig in (signal.SIGTERM, signal.SIGKILL):
            pids = self.background_pids()
            if not pids:
                break
            for pid in pids:
                try:
                    os.kill(pid, sig)
                    stopped += 1
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 2
            while self.background_pids() and time.monotonic() < deadline:
                time.sleep(0.05)
        remaining = self.background_pids()
        if remaining:
            raise RuntimeError(f"sandbox processes survived cleanup: {sorted(remaining)}")
        return stopped
