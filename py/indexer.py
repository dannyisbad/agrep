"""Auto-indexer: keep the materialized index current while the server runs, with no
manual command.

The live watcher already surfaces new chats in real time (the rail overlay). This thread
does the slower job of MATERIALIZING them - re-running the Rust ingest so new sessions get
permanent rows, titles, and event trees. It's gated so it doesn't burn CPU needlessly:

  - it only runs after the watcher has seen new activity since the last index, AND
  - that activity has settled for QUIET_S (reindex after a work burst, not mid-keystroke), AND
  - it's been at least MIN_GAP_S since the last run (rate-limit),
  - with a MAX_STALE_S backstop that forces a run during marathon sessions.

The "refresh now" button in the UI calls trigger() to bypass all gates. State is exposed
through /status so the app shows indexing/idle/last-run/errors instead of telling anyone to
go run a CLI command.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import common

WIN = sys.platform == "win32"
TILT = common.ingest_bin()
REINDEX = common.REPO_ROOT / "reindex.py"

CHECK_S = 20        # how often the thread wakes to check the gates (cheap)
QUIET_S = 90        # reindex once live activity has been quiet this long
MIN_GAP_S = 300     # at most one automatic run per this interval
MAX_STALE_S = 1800  # force a run mid-activity if the last index is older than this
FULL_MAX_NEW = 150  # summaries generated per forced run -- bounds one click to minutes


class AutoIndexer(threading.Thread):
    def __init__(self, watcher):
        super().__init__(daemon=True, name="tilt-indexer")
        self._w = watcher
        self._force = threading.Event()
        self._lock = threading.Lock()
        self._last_index_wall = 0.0
        self.state = {
            "phase": "idle",      # idle | indexing | error
            "last_run": 0.0,      # epoch seconds of the last completed run
            "last_dur": 0.0,      # seconds the last run took
            "last_err": "",
            "runs": 0,
        }

    # ---- public ----------------------------------------------------------

    def trigger(self) -> None:
        """Force a reindex now (the UI 'refresh' button), bypassing the gates."""
        self._force.set()

    def status(self) -> dict:
        with self._lock:
            s = dict(self.state)
        s["available"] = TILT.exists()
        return s

    # ---- loop ------------------------------------------------------------

    def run(self) -> None:
        # Fresh clone with no index yet: build one immediately so the app isn't empty.
        if not (common.DATA_DIR / "messages.jsonl").exists() and TILT.exists():
            self._index()
        while True:
            forced = self._force.wait(timeout=CHECK_S)
            self._force.clear()
            if forced:
                # The UI button runs the WHOLE pipeline (embeddings/affect/titles/arcs),
                # capped so one click is minutes, not a multi-hour backlog grind.
                # Automatic runs stay rust-only: they fire mid-work-session, where a
                # surprise GPU+LLM load would fight the user's actual job.
                self._index(full=True)
            elif self._should_run():
                self._index()

    def _should_run(self) -> bool:
        if not TILT.exists() or self.state["phase"] == "indexing":
            return False
        activity = self._w._last_event_wall  # wall seconds of the last live event
        if activity <= self._last_index_wall:  # nothing new since we last indexed
            return False
        now = time.time()
        gap = now - self.state["last_run"]
        if gap < MIN_GAP_S:
            return False
        return (now - activity) >= QUIET_S or gap >= MAX_STALE_S

    def _index(self, full: bool = False) -> None:
        with self._lock:
            if self.state["phase"] == "indexing":
                return
            self.state["phase"] = "indexing"
        # stamp the attempt up front so activity arriving DURING the run still counts
        # as "new" for the next cycle (we don't lose turns written mid-ingest)
        self._last_index_wall = time.time()
        t = time.perf_counter()
        err = ""
        if full and REINDEX.exists():
            # reindex.py self-selects the smart-tier venv for the heavy stages,
            # so any python works
            cmd = [sys.executable, str(REINDEX), "--no-build",
                   "--max-new", str(FULL_MAX_NEW)]
            timeout = 3600
        else:
            cmd = [str(TILT), "index", "--agent", "all"]
            timeout = 1800
        try:
            r = subprocess.run(cmd, cwd=str(common.REPO_ROOT),
                               capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "ingest failed").strip()[-300:]
            else:
                # Keep the CLI's derived FTS index hot too. Otherwise the server
                # does the ingest in the background, but the next `agrep <pattern>`
                # pays the sqlite rebuild.
                common.refresh_search_index()
        except Exception as e:  # noqa: BLE001 -- surface anything to the status chip
            err = f"{type(e).__name__}: {e}"[-300:]
        dur = time.perf_counter() - t
        with self._lock:
            self.state.update(phase="error" if err else "idle",
                              last_run=time.time(), last_dur=round(dur, 1),
                              last_err=err, runs=self.state["runs"] + 1)
        if err:
            common.log(f"auto-index failed: {err}")


_INSTANCE: AutoIndexer | None = None


def start(watcher) -> AutoIndexer:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoIndexer(watcher)
        _INSTANCE.start()
    return _INSTANCE


def instance() -> AutoIndexer | None:
    return _INSTANCE
