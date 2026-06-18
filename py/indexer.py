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

WIN = sys.platform == "win32"
TILT = common.ingest_bin()
REINDEX = common.REPO_ROOT / "reindex.py"

# The headless freshness daemon (indexd) sets AGREP_INDEXD=1 and runs MUCH tighter gates:
# its whole job is to keep search current, and the rust ingest + incremental corpus refresh
# are cheap (skip-on-unchanged + per-session FTS), so it can react within seconds of activity
# settling. The in-app server keeps the relaxed defaults (it has the live rail for immediacy
# and shouldn't reindex every few seconds while also serving the explorer / loading models).
_INDEXD = os.environ.get("AGREP_INDEXD") == "1"
CHECK_S = 3 if _INDEXD else 20      # how often the thread wakes to check the gates (cheap)
QUIET_S = 4 if _INDEXD else 30      # reindex once live activity has been quiet this long
MIN_GAP_S = 12 if _INDEXD else 120  # at most one automatic run per this interval
MAX_STALE_S = 60 if _INDEXD else 1800  # force a run mid-activity if the index is older than this
FULL_MAX_NEW = 150  # summaries generated per forced run -- bounds one click to minutes
SMART_MIN_GAP_S = 600
SMART_IDLE_S = 180
SMART_TIMEOUT_S = 600
SMART_EMOTION_MAX_NEW = 128
SMART_SUMMARY_MAX_NEW = 6
SMART_TITLE_LIMIT = 20
SMART_JUDGE_LIMIT = 10
OLLAMA_TAGS = "http://localhost:11434/api/tags"

# Console children of the detached (console-less) daemon would each flash a blank conhost
# every reindex; CREATE_NO_WINDOW suppresses it. Windows-only.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WIN else {}


class AutoIndexer(threading.Thread):
    def __init__(self, watcher, auto_smart: bool = True):
        super().__init__(daemon=True, name="tilt-indexer")
        self._w = watcher
        self._auto_smart = auto_smart and os.environ.get("AGREP_AUTO_SMART", "1") != "0"
        self._force = threading.Event()
        self._lock = threading.Lock()
        self._last_index_wall = 0.0
        self._last_smart_wall = 0.0
        self._smart_deps_cache: tuple[float, bool] = (0.0, False)
        self.state = {
            "phase": "idle",      # idle | indexing | error
            "last_run": 0.0,      # epoch seconds of the last completed run
            "last_dur": 0.0,      # seconds the last run took
            "last_err": "",
            "runs": 0,
            "smart_enabled": self._auto_smart,
            "smart_phase": "idle",      # idle | indexing | error | skipped
            "smart_last_run": 0.0,
            "smart_last_dur": 0.0,
            "smart_last_stage": "",
            "smart_last_err": "",
            "smart_runs": 0,
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
            elif self._should_smart_run():
                self._smart_nibble()

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
                               capture_output=True, text=True, timeout=timeout,
                               **_NO_WINDOW)
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

    # ---- opportunistic smart/named refresh ---------------------------------

    def _active_sessions(self, window_s: int = SMART_IDLE_S) -> int:
        now_ms = time.time() * 1000
        return sum(
            1 for s in self._w.sessions.values()
            if now_ms - float(s.get("last_ts") or 0) <= window_s * 1000
        )

    def _ollama_up(self) -> bool:
        try:
            with urllib.request.urlopen(OLLAMA_TAGS, timeout=2) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _smart_deps_present(self) -> bool:
        if not common.VENV_PY.exists():
            return False
        now = time.time()
        cached_at, cached_ok = self._smart_deps_cache
        if now - cached_at < 300:
            return cached_ok
        try:
            r = subprocess.run(
                [str(common.VENV_PY), "-c", "import torch, transformers"],
                cwd=str(common.REPO_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                **_NO_WINDOW,
            )
            ok = r.returncode == 0
        except Exception:  # noqa: BLE001
            ok = False
        self._smart_deps_cache = (now, ok)
        return ok

    def _gpu_looks_free(self) -> bool:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return True
        try:
            r = subprocess.run(
                [
                    exe,
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                **_NO_WINDOW,
            )
            if r.returncode != 0:
                return True
            for line in r.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 3:
                    continue
                util, used, total = (float(p) for p in parts)
                if util > 25 or (total and used / total > 0.65):
                    return False
        except Exception:  # noqa: BLE001
            return True
        return True

    def _emotion_pending(self) -> bool:
        if not common.MESSAGES_PATH.exists():
            return False
        try:
            done = common.jsonl_ids(common.EMOTIONS_PATH)
            with common.MESSAGES_PATH.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        mid = json.loads(line).get("id")
                    except json.JSONDecodeError:
                        continue
                    if mid and mid not in done:
                        return True
        except OSError:
            return False
        return False

    def _summaries_pending(self) -> bool:
        if not common.MESSAGES_PATH.exists():
            return False
        path = common.DATA_DIR / "summaries.jsonl"
        done = common.jsonl_ids(path, key="session")
        counts: dict[str, int] = {}
        try:
            with common.MESSAGES_PATH.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("who", "user") != "user":
                        continue
                    session = row.get("session")
                    if not session or session in done:
                        continue
                    counts[session] = counts.get(session, 0) + 1
                    if counts[session] >= 5:
                        return True
        except OSError:
            return False
        return False

    def _titles_pending(self) -> bool:
        path = common.DATA_DIR / "summaries.jsonl"
        if not path.exists():
            return False
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        if not json.loads(line).get("title"):
                            return True
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return False
        return False

    def _judge_pending(self) -> bool:
        if not common.EMOTIONS_PATH.exists():
            return False
        try:
            with common.EMOTIONS_PATH.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("routed_to_judge") and not row.get("judged"):
                        return True
        except OSError:
            return False
        return False

    def _should_smart_run(self) -> bool:
        if not self._auto_smart:
            return False
        if self.state["phase"] == "indexing" or self.state["smart_phase"] == "indexing":
            return False
        if not common.MESSAGES_PATH.exists():
            return False
        now = time.time()
        if now - self._last_smart_wall < SMART_MIN_GAP_S:
            return False
        if self._active_sessions() > 0:
            return False
        if not self._gpu_looks_free():
            return False
        has_emotion = self._smart_deps_present() and self._emotion_pending()
        has_named = self._ollama_up() and (
            self._summaries_pending() or self._titles_pending() or self._judge_pending()
        )
        return has_emotion or has_named

    def _smart_nibble(self) -> None:
        with self._lock:
            if self.state["smart_phase"] == "indexing":
                return
            self.state.update(smart_phase="indexing", smart_last_err="", smart_last_stage="")
        self._last_smart_wall = time.time()
        t = time.perf_counter()
        stage = ""
        err = ""
        env = dict(os.environ)
        env.setdefault("AGREP_OLLAMA_KEEP_ALIVE", "30s")
        try:
            if self._smart_deps_present() and self._emotion_pending():
                stage = "emotion"
                cmd = [
                    str(common.VENV_PY),
                    "py/emotion.py",
                    "--max-new",
                    str(SMART_EMOTION_MAX_NEW),
                ]
            elif self._ollama_up() and self._summaries_pending():
                stage = "summaries"
                cmd = [
                    sys.executable,
                    "py/summarize.py",
                    "--max-new",
                    str(SMART_SUMMARY_MAX_NEW),
                ]
            elif self._ollama_up() and self._titles_pending():
                stage = "titles"
                cmd = [sys.executable, "py/titles.py", "--limit", str(SMART_TITLE_LIMIT)]
            elif self._ollama_up() and self._judge_pending():
                stage = "judge"
                cmd = [sys.executable, "py/judge.py", "--limit", str(SMART_JUDGE_LIMIT)]
            else:
                with self._lock:
                    self.state.update(smart_phase="idle")
                return
            r = subprocess.run(
                cmd,
                cwd=str(common.REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=SMART_TIMEOUT_S,
                env=env,
                **_NO_WINDOW,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or f"{stage} failed").strip()[-300:]
            else:
                common.refresh_search_index()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[-300:]
        dur = time.perf_counter() - t
        with self._lock:
            self.state.update(
                smart_phase="error" if err else "idle",
                smart_last_run=time.time(),
                smart_last_dur=round(dur, 1),
                smart_last_stage=stage,
                smart_last_err=err,
                smart_runs=self.state["smart_runs"] + (1 if stage else 0),
            )
        if err:
            common.log(f"auto-smart failed ({stage}): {err}")


_INSTANCE: AutoIndexer | None = None


def start(watcher, auto_smart: bool = True) -> AutoIndexer:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoIndexer(watcher, auto_smart=auto_smart)
        _INSTANCE.start()
    return _INSTANCE


def instance() -> AutoIndexer | None:
    return _INSTANCE
