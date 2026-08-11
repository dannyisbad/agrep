"""Auto-indexer: keep the materialized index current in the background.

The live watcher detects new activity immediately. This thread does the slower job of
materializing it: re-running Rust ingest so sessions get permanent rows, titles, and
event trees. It is gated so it does not burn CPU needlessly:

  - it only runs after the watcher has seen new activity since the last index, AND
  - that activity has settled for QUIET_S (reindex after a work burst, not mid-keystroke), AND
  - it's been at least MIN_GAP_S since the last run (rate-limit),
  - with a MAX_STALE_S backstop that forces a run during marathon sessions.

Internal callers can use status() to inspect indexing, idle, last-run, and error state.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, NamedTuple

import common
from hookless import native
import indexd_runtime
import ownerfile
import surface_policy as surface

WIN = sys.platform == "win32"
INGEST = common.ingest_bin()

CHECK_S = 20
QUIET_S = 30
MIN_GAP_S = 120
MAX_STALE_S = 1800


def configure_indexd_mode() -> None:
    """Select the freshness daemon's tighter cadence before its thread starts."""
    global CHECK_S, QUIET_S, MIN_GAP_S, MAX_STALE_S, HEALTH_CHECK_S
    CHECK_S = 3
    QUIET_S = 4
    MIN_GAP_S = 12
    MAX_STALE_S = 60
    # an explicit override still wins; this is only the daemon's default
    HEALTH_CHECK_S = _interval("AGREP_HEALTH_CHECK_S", 15.0)


# Housekeeping walks agent-store roots or user instruction files. It is not part of
# the freshness gate and must not inherit indexd's 3-second poll cadence.
def _interval(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


ARCHIVE_CHECK_S = _interval("AGREP_ARCHIVE_CHECK_S", 60.0)
TEACH_CHECK_S = _interval("AGREP_TEACH_CHECK_S", 300.0)
# Hard-polite drain idle gap: per-pass footprint (128 rows, 2 threads, nice
# 15) is unchanged; 30 s just shrinks the 91%-idle wait between passes.
EMBED_CHECK_S = _interval("AGREP_EMBED_CHECK_S", 30.0)
# Huge-snapshot corpora batch ambient deltas to save SSD/battery (past ~100k rows the
# monolithic 384-d matrix + refs snapshot is hundreds of MB per rewrite); a semantic
# query still refreshes immediately.
EMBED_LARGE_CHECK_S = _interval("AGREP_EMBED_LARGE_CHECK_S", 600.0)
EMBED_LARGE_ROWS = 100_000
# Per-pass background cap: a backlog drains across passes, newest rows first, instead of monopolizing cores.
EMBED_MAX_NEW = _env_int("AGREP_EMBED_MAX_NEW", 5000)
EMBED_ACTIVE_WINDOW_S = _interval("AGREP_EMBED_ACTIVE_WINDOW_S", 120.0)
# Keep a useful generation serving while a tiny live tail accumulates.
# The threshold bounds rewrite churn; the horizon prevents indefinite deferral.
# This scheduler policy does not alter the pass size or resource governor.
EMBED_ACTIVE_BATCH_ROWS = _env_int(
    "AGREP_EMBED_ACTIVE_BATCH_ROWS", 128, 1)
EMBED_ACTIVE_MAX_DEFER_S = _interval(
    "AGREP_EMBED_ACTIVE_MAX_DEFER_S", 120.0)
# Idle AC tranches grow to bound immutable rewrites; a moving source still stops
# inference after its current 500-row chunk.
EMBED_CATCHUP_MAX_NEW = _env_int(
    "AGREP_EMBED_CATCHUP_MAX_NEW", 1000, 128)
EMBED_CATCHUP_MAX_GROWTH = _env_int(
    "AGREP_EMBED_CATCHUP_MAX_GROWTH", 1_000_000,
    EMBED_CATCHUP_MAX_NEW)
EMBED_CATCHUP_CHECK_S = _interval("AGREP_EMBED_CATCHUP_CHECK_S", 5.0)
EMBED_PRESSURE_CHECK_S = _interval("AGREP_EMBED_PRESSURE_CHECK_S", 30.0)
# The derived stores are verified on their own cadence: damage arrives without
# activity (a filled disk, a killed writer), so activity cannot be its trigger.
HEALTH_CHECK_S = _interval("AGREP_HEALTH_CHECK_S", 60.0)
REPAIR_MIN_GAP_S = _interval("AGREP_REPAIR_MIN_GAP_S", 300.0)
_EMBED_RESOURCE_CACHE: dict[str, object] = {"at": 0.0, "value": None}
HOOK_TIMEOUT_S = 600
INDEX_CHILD_TIMEOUT_S = 1800

# Windows: console children of the console-less daemon would each flash a blank conhost per reindex.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WIN else {}


def corpusdb_module():
    """corpusdb imports the indexer's peers; resolve it at call time."""
    import corpusdb
    return corpusdb


def _retry_gap(streak: int) -> float:
    """Back off a broken ingest without delaying its first retry."""
    if streak <= 1:
        return MIN_GAP_S
    return min(900.0, MIN_GAP_S * (2 ** min(streak - 1, 10)))


def _publication_pending() -> bool:
    """A retained .ingest_pending.bin after a clean exit is withheld work (a guarded
    deletion awaiting its confirming pass, or a drifted source), never success."""
    try:
        return (common.DATA_DIR / ".ingest_pending.bin").exists()
    except OSError:
        return False


def _ingest_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Name the failure from stderr or the exit mode; stdout is progress, never the error."""
    err = (stderr or "").strip()
    if err:
        # AGREP_DEBUG timing lines share stderr; the final Error: block is the diagnosis.
        lines = err.splitlines()
        for start in range(len(lines) - 1, -1, -1):
            if lines[start].startswith("Error:"):
                return "\n".join(lines[start:]).strip()
        return err
    if returncode < 0:
        err = f"ingest killed by signal {-returncode}"
    elif returncode == 125:
        err = "ingest exited 125: lifetime guard refused or lost ownership"
    elif returncode in (126, 127):
        err = f"ingest exited {returncode}: lifetime guard could not exec ingest"
    else:
        err = f"ingest exited {returncode} with no error output"
    tail = (stdout or "").strip()
    if tail:
        err += f" (last stdout: {tail.splitlines()[-1].strip()})"
    return err


def _log_takeover_result(stderr: str) -> None:
    marker = "no live writer holds them - this build took over:"
    for line in (stderr or "").splitlines():
        if marker in line:
            common.log(common.terminal_safe(line))


def _embedding_catchup_cap(indexed: int, pending: int) -> int:
    growth = min(max(1, int(indexed)), EMBED_CATCHUP_MAX_GROWTH)
    return min(max(0, int(pending)), max(EMBED_CATCHUP_MAX_NEW, growth))


class _EmbeddingRefreshAdmission(NamedTuple):
    admit: bool
    state: str
    reason: str
    pending: int | None


def _embedding_refresh_admission(
        coherence: dict, *, recently_active: bool,
        deferred_for_s: float) -> _EmbeddingRefreshAdmission:
    """Pure admission decision for low-priority semantic refresh work.

    Only a proven searchable generation with a small, known pending tail may
    wait.  Bootstrap, integrity/profile failures, migration, idle catch-up,
    substantial backlogs, and the bounded expiry all retain the old immediate
    behavior.
    """
    state = str(coherence.get("state") or "unknown")
    if (not coherence.get("searchable")
            or state in ("corrupt-embeddings", "legacy-publication",
                         "legacy-embeddings", "profile-mismatch")):
        return _EmbeddingRefreshAdmission(
            True, "admitted", "generation_unsearchable_or_repair", None)
    if coherence.get("migration_pending"):
        return _EmbeddingRefreshAdmission(
            True, "admitted", "migration_repair", None)
    coverage = coherence.get("coverage")
    try:
        raw_pending = coverage["pending"] if isinstance(coverage, dict) else None
        if isinstance(raw_pending, bool):
            raise ValueError("boolean pending count")
        pending = int(raw_pending)
        if pending < 0:
            raise ValueError("negative pending count")
    except (KeyError, TypeError, ValueError):
        return _EmbeddingRefreshAdmission(
            True, "admitted", "pending_coverage_unknown", None)
    if pending == 0:
        return _EmbeddingRefreshAdmission(
            True, "admitted", "no_pending_rows", pending)
    if not recently_active:
        return _EmbeddingRefreshAdmission(
            True, "admitted", "transcript_idle", pending)
    if pending >= EMBED_ACTIVE_BATCH_ROWS:
        return _EmbeddingRefreshAdmission(
            True, "admitted", "batch_threshold_reached", pending)
    if max(0.0, float(deferred_for_s)) >= EMBED_ACTIVE_MAX_DEFER_S:
        return _EmbeddingRefreshAdmission(
            True, "admitted", "max_defer_expired", pending)
    return _EmbeddingRefreshAdmission(
        False, "deferred_active_churn",
        "searchable_tiny_tail_during_active_churn", pending)


def _embedding_resources(now: float) -> tuple[bool | None, float | None, float | None]:
    """Cache host probes; macOS memory and power checks launch tiny utilities."""
    cached = _EMBED_RESOURCE_CACHE.get("value")
    if cached is not None and now - float(_EMBED_RESOURCE_CACHE["at"]) < 15.0:
        return cached  # type: ignore[return-value]
    value = (common.battery_state()[0], common.available_memory_fraction(),
             common.host_cpu_fraction())
    _EMBED_RESOURCE_CACHE.update(at=now, value=value)
    return value


def _embedding_backfill_policy(state: dict, *, recently_active: bool,
                               on_battery: bool | None,
                               memory_fraction: float | None,
                               cpu_fraction: float | None) -> dict[str, object]:
    """Choose a bounded pass from observable host pressure and backlog size."""
    indexed = max(0, int(state.get("indexed") or 0))
    total = max(indexed, int(state.get("total") or 0))
    pending = max(0, total - indexed)
    partial = state.get("state") == "partial" and pending > 0
    critical_memory = (
        memory_fraction is not None
        and memory_fraction < surface.SEMANTIC_GOVERNOR.memory_floor)
    cpu_unknown_pressure = WIN and cpu_fraction is None
    busy = (recently_active or cpu_unknown_pressure
            or (cpu_fraction is not None and cpu_fraction >= 0.75))
    healthy_memory = memory_fraction is None or memory_fraction >= 0.25

    if critical_memory:
        return {"cap": 0, "interval": EMBED_PRESSURE_CHECK_S,
                "threads": 1, "nice": 18, "mode": "memory-pressure"}
    if partial and not busy and on_battery is not True and healthy_memory:
        thread_cap = 6 if sys.platform == "darwin" else 8
        threads = max(2, min(thread_cap, max(2, (os.cpu_count() or 2) // 2)))
        return {"cap": _embedding_catchup_cap(indexed, pending),
                "interval": EMBED_CATCHUP_CHECK_S,
                "threads": threads, "nice": 5, "mode": "catch-up"}

    # Active work, battery, or moderate memory/load gets a small very-low-priority pass;
    # re-evaluate to enter catch-up soon after pressure clears, without polling power utilities each tick.
    hard_pressure = (on_battery is True or recently_active or cpu_unknown_pressure
                     or (cpu_fraction is not None and cpu_fraction >= 0.75))
    interval = EMBED_CHECK_S if hard_pressure else EMBED_PRESSURE_CHECK_S
    polite_cap = 128 if hard_pressure else 500
    cap = min(pending, polite_cap) if partial else polite_cap
    return {"cap": cap, "interval": interval,
            "threads": 2, "nice": 15, "mode": "polite"}


def split_user_command(command: str) -> list[str]:
    """Parse a configured command with the platform's real argv rules.

    POSIX shlex removes backslashes from Windows paths; ``shlex(posix=False)`` keeps
    surrounding quotes in argv. CommandLineToArgvW is the contract CreateProcess
    users actually expect and correctly handles both paths and quoted spaces.
    """
    if not WIN:
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    argc = ctypes.c_int()
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


def configured_post_index_hooks() -> list[tuple[str, list[str], int]]:
    """Return legacy + managed hooks as validated argv, in deterministic order.

    `post_index` remains the user-facing single command for compatibility. Apps use
    `post_index_hooks.{owner}.argv`, so setup/removal cannot overwrite another app's
    hook and no platform shell parser sits between a stored path and CreateProcess.
    """
    out: list[tuple[str, list[str], int]] = []
    legacy = common.setting("post_index")
    if isinstance(legacy, str) and legacy and legacy != "off":
        try:
            argv = split_user_command(legacy)
            if argv:
                out.append(("legacy", argv, HOOK_TIMEOUT_S))
        except Exception as e:  # one broken legacy entry must not suppress app hooks
            common.log(f"post_index legacy config error: {type(e).__name__}: {e}")
    managed = common.setting("post_index_hooks")
    if not isinstance(managed, dict):
        return out
    for owner, spec in sorted(managed.items()):
        if not isinstance(owner, str) or not isinstance(spec, dict):
            continue
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(
                isinstance(arg, str) and arg for arg in argv):
            continue
        try:
            timeout = max(1, min(HOOK_TIMEOUT_S, int(spec.get("timeout_s", 60))))
        except (TypeError, ValueError):
            timeout = 60
        out.append((owner, list(argv), timeout))
    return out


class _PostIndexHookCancelled(RuntimeError):
    """The owning indexer stopped while a managed hook was running."""


def run_post_index_hooks(
        *, stop_requested: Callable[[], bool] | None = None) -> None:
    """Run each user/app-owned post-index hook; failures never wound indexing."""
    if common.data_dir_readonly(common.DATA_DIR):
        return
    if not indexd_runtime.derived_writer_mutation_info().writable:
        return
    try:
        hooks = configured_post_index_hooks()
    except Exception as e:
        common.log(f"post_index hook config error: {type(e).__name__}: {e}")
        return

    def cancel_if_stopped(_process) -> None:
        if stop_requested is not None and stop_requested():
            raise _PostIndexHookCancelled()

    for owner, argv, timeout in hooks:
        if stop_requested is not None and stop_requested():
            return
        try:
            r = native.run_owned_process(
                argv, cwd=str(common.REPO_ROOT), capture_output=True,
                timeout=timeout, hide_window=WIN,
                relay_signals=stop_requested is None,
                tick=(cancel_if_stopped if stop_requested is not None else None))
            relayed_signal = getattr(r, "relayed_signal", None)
            if relayed_signal == signal.SIGINT:
                raise KeyboardInterrupt
            if relayed_signal is not None:
                raise SystemExit(128 + relayed_signal)
            if stop_requested is not None and stop_requested():
                return
            if r.returncode != 0:
                detail = common.terminal_safe(
                    (r.stderr or r.stdout or "").strip()[-200:])
                common.log(f"post_index hook {owner!r} failed ({r.returncode}): "
                           f"{detail}")
        except _PostIndexHookCancelled:
            return
        except Exception as e:  # noqa: BLE001
            common.log(f"post_index hook {owner!r} error: {type(e).__name__}: {e}")


class AutoIndexer(threading.Thread):
    def __init__(
            self, watcher, *,
            owns_lifetime: Callable[[], bool],
            owner_snapshot: ownerfile.Snapshot):
        if not isinstance(owner_snapshot, ownerfile.Snapshot):
            raise TypeError("auto-indexer requires an owned daemon generation")
        super().__init__(daemon=True, name="agrep-indexer")
        self._w = watcher
        self._owns_lifetime = owns_lifetime
        self._owner_snapshot = owner_snapshot
        self._stop_requested = threading.Event()
        self._post_index_idle = threading.Event()
        self._post_index_idle.set()
        self._search_refresh_active = threading.Event()
        self._operational = threading.Event()
        self._lock = threading.Lock()
        self._child: tuple[subprocess.Popen, str | None, int | None] | None = None
        self._last_index_wall = 0.0
        self._last_archive_check = 0.0
        self._last_teach_check = 0.0
        self._last_embed_check = 0.0
        self._embed_defer_started_mono = 0.0
        self._last_health_check = 0.0
        self._repair = corpusdb_module().NO_REPAIR
        self._repair_seen = ""
        self._repair_attempted = False
        self._repair_streak = 0
        self._last_repair_run = 0.0
        self._last_verify_stamp = time.monotonic()
        self._fail_streak, last_error, last_attempt = (
            indexd_runtime.indexd_failure_state())
        self._pending_streak = 0
        self._retry_needed = self._fail_streak > 0
        # The ledger keeps only the newest error text, so a restart treats the
        # persisted streak as identical; the one-shot guard still bounds --full.
        self._identical = self._fail_streak
        self._escalated = indexd_runtime.auto_index_escalated()
        self.state = {
            "phase": "error" if self._retry_needed else "idle",
            "last_run": (min(time.time(), last_attempt)
                         if self._retry_needed else 0.0),
            "last_dur": 0.0,      # seconds the last run took
            "last_err": last_error if self._retry_needed else "",
            "runs": 0,
            "teach_reconcile": {
                "version": 1, "state": "not-checked", "repaired": [],
                "refusals": [], "preserved_newer": [],
            },
        }

    # ---- public ----------------------------------------------------------

    def wait_operational(self, timeout: float | None = None) -> bool:
        return self._operational.wait(timeout)

    def stop(self) -> None:
        """Stop new work and retire the currently owned indexing child."""
        # Share the launch lock with _run_post_index_hooks: after this point a
        # hook is either visibly active or cannot cross its launch boundary.
        with self._lock:
            self._stop_requested.set()
        self._terminate_child()
        if threading.current_thread() is not self:
            self._post_index_idle.wait()

    def _terminate_child(self) -> None:
        with self._lock:
            active = self._child
        if active is None:
            return
        process, process_start, control_fd = active
        if control_fd is not None:
            try:
                os.close(control_fd)
            except OSError:
                pass
            with self._lock:
                if self._child == active:
                    self._child = (process, process_start, None)
            return
        stopped = False
        if process_start:
            stopped = common.terminate_exact_process_tree(
                process.pid, process_start, wait_s=2.0,
                require_bound_tree=True)
        if not stopped and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _launch_index_process(
            self, cmd: list[str], launch: dict) -> tuple[subprocess.Popen, int | None]:
        if WIN:
            return subprocess.Popen(cmd, **launch), None
        if indexd_runtime._indexd_child_active(
                self._owner_snapshot) is not False:
            raise ownerfile.OwnershipLost(
                "an earlier index child still owns this daemon generation")
        read_fd, write_fd = os.pipe()
        try:
            token = indexd_runtime.indexd_generation_token(self._owner_snapshot)
            guard = [
                sys.executable, str(common.PY_DIR / "lifetime.py"),
                "--parent-fd", str(read_fd),
                "--fence", str(indexd_runtime.indexd_child_path(
                    self._owner_snapshot)),
                "--owner-token", token, "--", *cmd,
            ]
            process = subprocess.Popen(
                guard, pass_fds=(read_fd,), start_new_session=True, **launch)
        except Exception:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        return process, write_fd

    def _clear_child(self, process: subprocess.Popen) -> None:
        with self._lock:
            active = self._child
            if active is None or active[0] is not process:
                return
            self._child = None
        if active[2] is not None:
            try:
                os.close(active[2])
            except OSError:
                pass

    def _drain_child(self, process: subprocess.Popen) -> None:
        self._terminate_child()
        try:
            process.communicate(timeout=3.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        indexd_runtime._retire_indexd_child(
            self._owner_snapshot, wait_s=3.0)
        try:
            process.communicate(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _refresh_search_index(self) -> bool | None:
        """Run SQLite refresh in the owned child whose allocator dies on exit."""
        if not self._ownership_survives():
            raise ownerfile.OwnershipLost("indexd lifetime owner changed")
        child_env = indexd_runtime.rust_writer_env(INGEST)
        child_env[indexd_runtime._INDEXD_REFRESH_EXPECTED_WRITER_ENV] = (
            child_env["AGREP_RUNTIME_BUILD_ID"])
        launch = {
            "cwd": str(common.REPO_ROOT),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": child_env,
            **_NO_WINDOW,
        }
        command = [
            sys.executable, str(common.PY_DIR / "indexd.py"),
            "--refresh-search-index-child",
        ]
        process, control_fd = self._launch_index_process(command, launch)
        process_start = common.process_start_identity(process.pid)
        with self._lock:
            self._child = (process, process_start, control_fd)
        try:
            if not self._ownership_survives():
                raise ownerfile.OwnershipLost("indexd lifetime owner changed")
            stdout, stderr = process.communicate(timeout=INDEX_CHILD_TIMEOUT_S)
            if not self._ownership_survives():
                raise ownerfile.OwnershipLost("indexd lifetime owner changed")
        except subprocess.TimeoutExpired:
            self._drain_child(process)
            common.log(
                f"search-index refresh timed out after {INDEX_CHILD_TIMEOUT_S}s")
            return False
        except Exception:
            self._drain_child(process)
            raise
        finally:
            self._clear_child(process)
        if process.returncode == 0:
            return True
        if process.returncode == indexd_runtime.SEARCH_INDEX_REFRESH_UNSUPPORTED_RC:
            return None
        detail = (stderr or stdout or "").strip()
        suffix = f": {detail}" if detail else " with no error output"
        common.log(f"search-index refresh child exited {process.returncode}{suffix}")
        return False

    def status(self) -> dict:
        with self._lock:
            s = dict(self.state)
        if self._search_refresh_active.is_set() and s.get("phase") != "indexing":
            s["phase"] = "indexing"
        s["available"] = INGEST.exists()
        return s

    # ---- loop ------------------------------------------------------------

    def _ownership_survives(self) -> bool:
        if self._stop_requested.is_set():
            return False
        try:
            return bool(self._owns_lifetime())
        except OSError:
            return False

    def run(self) -> None:
        wait_boot = getattr(self._w, "wait_boot", None)
        if callable(wait_boot) and not wait_boot(30.0):
            return
        if not self._ownership_survives():
            return
        if not indexd_runtime.derived_writes_permitted():
            return
        self._operational.set()
        # A watcher cannot report source changes that predate this daemon generation.
        if INGEST.exists():
            self._index(startup=True)
        while self._ownership_survives():
            if self._stop_requested.wait(timeout=CHECK_S):
                return
            if not self._ownership_survives():
                return
            self._run_housekeeping(time.monotonic())
            self._check_derived_health(time.monotonic())
            if self._should_run():
                self._index()
            else:
                self._maybe_verify_current(time.monotonic())
            self._serve_index_request()

    def _serve_index_request(self) -> None:
        """Run a search-index build an explicit `agrep index` queued here.

        Nothing else in this loop schedules a MISSING search db - source
        activity gates _index and an unbuilt db is not damage to repair - so
        without this the handoff promises a build no tick would run."""
        if self.state["phase"] == "indexing":
            return
        self._search_refresh_active.set()
        try:
            indexd_runtime.serve_search_index_request(
                refresh=self._refresh_search_index)
        except Exception as exc:  # noqa: BLE001 -- a queued build cannot wound the loop
            common.dbg(f"queued search-index build failed: {exc}", "!")
        finally:
            self._search_refresh_active.clear()

    def _run_housekeeping(self, now: float) -> None:
        """Run root-walking maintenance on its own cadence, independent of polling."""
        if not indexd_runtime.derived_writer_mutation_info().writable:
            return
        if now - self._last_teach_check >= TEACH_CHECK_S:
            self._last_teach_check = now
            self._reassert_teach()
        if now - self._last_archive_check >= ARCHIVE_CHECK_S:
            self._last_archive_check = now
            self._capture_archive()
        embed_interval = EMBED_CHECK_S
        embed_policy: dict[str, object] | None = None
        try:
            if common.setting("embeddings") == "off":
                return
            import semantic
            state = semantic.read_embed_state()
            recently_active = (
                self._w._last_event_wall > 0
                and time.time() - self._w._last_event_wall < EMBED_ACTIVE_WINDOW_S)
            power, memory, cpu = _embedding_resources(now)
            embed_policy = _embedding_backfill_policy(
                state, recently_active=recently_active, on_battery=power,
                memory_fraction=memory, cpu_fraction=cpu)
            embed_interval = float(embed_policy["interval"])
            if (state.get("state") == "ready"
                    and int(state.get("total") or 0) >= EMBED_LARGE_ROWS):
                embed_interval = EMBED_LARGE_CHECK_S
        except Exception:  # noqa: BLE001 -- default cadence remains safe
            pass
        if now - self._last_embed_check >= embed_interval:
            self._last_embed_check = now
            self._refresh_embeddings(embed_policy)

    def _clear_embedding_refresh_deferral(self) -> None:
        self._embed_defer_started_mono = 0.0
        with self._lock:
            self.state.pop("semantic_refresh", None)

    def _refresh_embeddings(self, policy: dict[str, object] | None = None) -> None:
        """Keep semantic vectors current with zero user action: one bounded,
        below-normal-priority embed.py child per pass when the lane is stale.
        embed.py owns the cross-process claim, so this never stacks embedders
        with a CLI-triggered refresh, and must never wound the indexer."""
        try:
            import semantic
            coherence = semantic.embedding_coherence()
            if (coherence.get("coherent")
                    and not coherence.get("migration_pending")):
                # a complete lane contradicts a lingering failure record;
                # clearing here keeps doctor honest without a new embed pass
                self._clear_embedding_refresh_deferral()
                semantic.clear_superseded_embed_failure(coherence)
                return
            # Active agents get short publish intervals; an idle machine drains
            # the backlog in larger throughput-oriented batches. embed.py also
            # stops an idle batch after its current chunk if the source advances.
            recently_active = (
                self._w._last_event_wall > 0
                and time.time() - self._w._last_event_wall < EMBED_ACTIVE_WINDOW_S)
            spawn_env = None
            governed = policy is not None
            if governed and int(policy.get("cap") or 0) <= 0:
                self._clear_embedding_refresh_deferral()
                return
            if coherence.get("searchable"):
                # The lane can answer while this runs. The scheduler's policy
                # controls both tranche size and the child ONNX thread/priority
                # budget; fall back to the legacy split for direct test callers.
                if policy is not None:
                    cap = int(policy.get("cap") or 0)
                    spawn_env = {
                        "AGREP_SEM_THREADS": str(policy["threads"]),
                        "AGREP_SEM_BG_NICE": str(policy["nice"]),
                        "AGREP_SEM_BG_POLICY": str(policy["mode"]),
                    }
                else:
                    cap = (semantic.SEMANTIC_REFRESH_MAX_NEW
                           if recently_active else EMBED_MAX_NEW)
            else:
                # Missing/stale = keyword fallback: publish a small generation
                # fast, but an indexd resource decision remains authoritative.
                if governed:
                    cap = min(int(policy["cap"]),
                              semantic.SEMANTIC_BOOTSTRAP_MAX_NEW)
                    spawn_env = {
                        "AGREP_SEM_THREADS": str(policy["threads"]),
                        "AGREP_SEM_BG_NICE": str(policy["nice"]),
                        "AGREP_SEM_BG_POLICY": str(policy["mode"]),
                    }
                else:
                    cap = semantic.SEMANTIC_BOOTSTRAP_MAX_NEW
                    spawn_env = {
                        "AGREP_SEM_THREADS": str(
                            max(1, min(6, (os.cpu_count() or 2) // 2))),
                        "AGREP_SEM_BG_NICE": "5",
                        "AGREP_SEM_BG_POLICY": "bootstrap",
                    }
            if cap <= 0:
                self._clear_embedding_refresh_deferral()
                return
            now_mono = time.monotonic()
            deferred_for_s = (
                max(0.0, now_mono - self._embed_defer_started_mono)
                if self._embed_defer_started_mono else 0.0)
            admission = _embedding_refresh_admission(
                coherence, recently_active=recently_active,
                deferred_for_s=deferred_for_s)
            if not admission.admit:
                if not self._embed_defer_started_mono:
                    self._embed_defer_started_mono = now_mono
                with self._lock:
                    self.state["semantic_refresh"] = {
                        "state": admission.state,
                        "reason": admission.reason,
                        "pending": admission.pending,
                    }
                common.dbg(
                    "embed pass deferred: "
                    f"state={admission.state} reason={admission.reason} "
                    f"pending={admission.pending}")
                return
            self._clear_embedding_refresh_deferral()
            state = semantic.ensure_fresh_async(
                max_new=cap, spawn_env=spawn_env,
                ignore_battery=bool(governed and not coherence.get("searchable")))
            if state.get("state") == "running":
                common.dbg(f"embed pass: cap={cap} policy={policy}")
                common.log("building the embeddings index in the background "
                           "(low priority) - bare `agrep` shows progress")
        except Exception:  # noqa: BLE001
            pass

    def _reassert_teach(self) -> None:
        """Keep the `agrep setup` doc blocks alive: dotfile syncs and agents
        editing their own memory files clobber them silently. Stat-cheap per tick,
        touches only enrolled targets, and must never wound the indexer."""
        try:
            import teach
            for t in teach.reconcile():
                common.log(f"teach: re-asserted recall block in {t}")
            health = teach.current_reconcile_health()
            with self._lock:
                previous = self.state.get("teach_reconcile")
                self.state["teach_reconcile"] = health
            if health != previous:
                refusals = health.get("refusals", [])
                if health.get("state") == "health-unavailable" and refusals:
                    common.log(
                        "teach: reconcile health unavailable: "
                        f"{refusals[0]['reason']}")
                else:
                    for refusal in refusals:
                        common.log(
                            "teach: reconcile refused "
                            f"{refusal['path']}: {refusal['reason']}")
        except Exception as exc:  # noqa: BLE001 -- instruction repair never wounds indexing
            health = {
                "version": 1, "state": "unavailable", "repaired": [],
                "refusals": [{
                    "path": "teach-reconcile",
                    "kind": "reconcile-unavailable",
                    "reason": common.terminal_safe(
                        f"{type(exc).__name__}: {exc}"),
                }],
                "preserved_newer": [],
            }
            with self._lock:
                previous = self.state.get("teach_reconcile")
                self.state["teach_reconcile"] = health
            if health != previous:
                common.log(
                    "teach: reconcile unavailable: "
                    f"{health['refusals'][0]['reason']}")

    def _capture_archive(self) -> None:
        """Incremental snapshot of the agents' own store files (the byte-identical
        backup `agrep restore` rehydrates from). Stat-cheap once seeded - unchanged
        files are skipped without a read - and must never wound the indexer. The
        first pass over a big store is the slow one; `agrep archive` runs it
        explicitly."""
        try:
            import archive
            if archive.enabled():
                s = archive.capture()
                if s["appended"] or s["full"]:
                    common.log(f"archive: {s['appended']} appended, {s['full']} full, "
                               f"+{s['bytes_stored'] / 1e6:.1f} MB")
                if s.get("failed"):
                    common.log(f"archive: {s['failed']} source path(s) failed; "
                               "successful captures were kept")
        except Exception as exc:  # noqa: BLE001 -- indexing survives archive failure
            common.log(f"archive: capture blocked: {type(exc).__name__}: {exc}")
            try:
                archive.record_capture_failure(exc)
            except Exception as health_exc:  # noqa: BLE001 -- retain both failures in the log
                common.log(
                    "archive: could not persist capture health: "
                    f"{type(health_exc).__name__}: {health_exc}")

    def _maybe_verify_current(self, now: float) -> None:
        """Idle lapse: prove (cheap census compare) that the sources did not
        move, so a daemonless search after idle-exit can answer green. Rate-
        limited by the demoted write-side freshness clock."""
        if now - self._last_verify_stamp < indexd_runtime.FRESHNESS_WRITE_RATE_S:
            return
        self._last_verify_stamp = now
        if self.state["phase"] != "idle" or self._retry_needed:
            return
        try:
            indexd_runtime.stamp_verified_current()
        except Exception:  # noqa: BLE001 -- verification never wounds indexing
            pass

    def _check_derived_health(self, now: float) -> None:
        """Law 1: the derived stores are agrep's own product, rebuildable from
        the transcripts at any time. Verify them on the idle tick and queue the
        rebuild here, where no user is involved and nothing is reported.

        Without this the corpus lane converges only on new activity, so an
        index damaged while the box sat idle stayed damaged forever while the
        daemon ran, capable, beside it."""
        if self.state["phase"] == "indexing":
            return
        if now - self._last_health_check < HEALTH_CHECK_S:
            return
        self._last_health_check = now
        if not indexd_runtime.derived_writes_permitted():
            # the ownership fence holds these bytes; replacing them is not ours
            self._repair = corpusdb_module().NO_REPAIR
            return
        try:
            plan = corpusdb_module().derived_repair_plan()
        except Exception:  # noqa: BLE001 -- a failed probe never wounds indexing
            return
        # A publication landing under the probe is indistinguishable from
        # damage on one look and never survives a second; damage always does.
        confirmed, self._repair_seen = plan.code == self._repair_seen, plan.code
        if plan.code and not confirmed:
            return
        if self._repair_attempted:
            self._repair_attempted = False
            # the rebuild ran and the damage outlived it: that is the streak
            self._repair_streak = self._repair_streak + 1 if plan.code else 0
        elif not plan.code:
            self._repair_streak = 0
        self._repair = plan
        if plan.code:
            common.dbg(f"derived repair queued: {plan.code} ({plan.scope}) "
                       f"attempt {self._repair_streak + 1}")
        indexd_runtime.record_derived_repair(plan.code, self._repair_streak)

    def _should_run(self) -> bool:
        if not INGEST.exists() or self.state["phase"] == "indexing":
            return False
        now = time.time()
        gap = now - self.state["last_run"]
        if self._retry_needed:
            required_gap = _retry_gap(
                max(self._fail_streak, self._pending_streak))
        elif self._repair.code:
            required_gap = _retry_gap(self._repair_streak)
        else:
            required_gap = MIN_GAP_S
        if gap < required_gap:
            return False
        if self._retry_needed:
            return True
        if self._repair.code:
            # a reparse is the most expensive thing this loop can do; a flapping
            # verdict must never be able to schedule them back to back
            return now - self._last_repair_run >= REPAIR_MIN_GAP_S
        activity = self._w._last_event_wall  # wall seconds of the last live event
        if activity <= self._last_index_wall:  # nothing new since we last indexed
            return False
        return (now - activity) >= QUIET_S or gap >= MAX_STALE_S

    def _index(self, *, startup: bool = False) -> None:
        if not self._ownership_survives():
            return
        # the permitted fence, not raw writable: a dead foreign anchor is
        # reaped here by the ingest's successor takeover
        if not indexd_runtime.derived_writes_permitted():
            return
        with self._lock:
            if self.state["phase"] == "indexing":
                return
            self.state["phase"] = "indexing"
        # stamp up front so activity arriving mid-ingest still counts as "new" next cycle
        self._last_index_wall = time.time()
        t = time.perf_counter()
        err = ""
        cmd = [str(INGEST), "index", "--agent", "all"]
        streak_before = self._fail_streak
        repair = self._repair
        if repair.code:
            # a queued repair owns this run; the health tick re-verifies after it
            self._repair_attempted = True
            self._last_health_check = 0.0
            self._last_repair_run = time.time()
            if repair.scope == "full":
                cmd.append("--full")
            common.dbg(f"auto-index: repairing {repair.code}: {repair.detail}")
        escalate = (not self._escalated and self._identical
                    >= surface.FRESHNESS_POLICY.failure_threshold)
        if escalate:
            if "--full" not in cmd:
                cmd.append("--full")
            common.log(
                f"auto-index: escalating to --full after {self._identical} "
                f"identical refusals: {self.state['last_err']!r}")
            # spend the streak's one --full before launch: a crash mid-run
            # must not leave the flag unset and re-escalate every backoff
            self._escalated = True
            indexd_runtime.record_auto_index_health(
                self._fail_streak, self.state.get("last_err") or "",
                escalated=True)
        timeout = INDEX_CHILD_TIMEOUT_S
        semantic_source_before = None
        try:
            import semantic
            semantic_source_before = semantic.source_generation()
        except Exception:  # noqa: BLE001 -- optional rebase proof only
            pass
        try:
            common._close_event_reader()
            if not self._ownership_survives():
                raise ownerfile.OwnershipLost("indexd lifetime owner changed")
            launch = {
                "cwd": str(common.REPO_ROOT),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": indexd_runtime.rust_writer_env(INGEST),
                **_NO_WINDOW,
            }
            process, control_fd = self._launch_index_process(cmd, launch)
            process_start = common.process_start_identity(process.pid)
            with self._lock:
                self._child = (process, process_start, control_fd)
            if not self._ownership_survives():
                raise ownerfile.OwnershipLost("indexd lifetime owner changed")
            stdout, stderr = process.communicate(timeout=timeout)
            self._clear_child(process)
            if not self._ownership_survives():
                raise ownerfile.OwnershipLost("indexd lifetime owner changed")
            if process.returncode != 0:
                err = _ingest_failure(process.returncode, stdout, stderr)
                if not (stderr or "").strip():
                    # exit-mode label only; the source-health record still names the source
                    try:
                        health = indexd_runtime._source_health_failure()
                    except Exception:  # noqa: BLE001 -- attribution is best-effort
                        health = None
                    if health is not None:
                        err = f"{err}; {health.reason}"
            else:
                _log_takeover_result(stderr)
                semantic_source_after = None
                try:
                    import semantic
                    semantic_source_after = semantic.source_generation()
                except Exception:  # noqa: BLE001 -- full validation remains available
                    pass
                # Grab the Rust delta before corpusdb consumes it so semantic
                # coverage rebinds by validating only moved sessions.
                semantic_delta = None
                try:
                    import corpusdb
                    semantic_delta = corpusdb._read_changed()
                except Exception:  # noqa: BLE001 -- optimization only
                    pass
                try:
                    import embed
                    embed.rebase_generation_marker(
                        semantic_delta,
                        expected_previous_source=semantic_source_before,
                        expected_current_source=semantic_source_after)
                except Exception:  # noqa: BLE001 -- keyword freshness still wins
                    pass
                # The child exits after SQLite publishes, returning its allocator to the OS.
                refreshed = self._refresh_search_index()
                if refreshed is False:
                    err = "derived search-index refresh failed; retry scheduled"
                else:
                    startup_unchanged = (
                        semantic_source_before is not None
                        and semantic_source_after is not None
                        and semantic_source_before == semantic_source_after
                    )
                    if not startup or not startup_unchanged:
                        self._run_post_index_hooks()
        except subprocess.TimeoutExpired:
            self._terminate_child()
            try:
                process.communicate(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.communicate(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            err = f"indexing timed out after {timeout}s"
        except ownerfile.OwnershipLost:
            self.stop()
            return
        except Exception as e:  # noqa: BLE001 -- surface anything to the status chip
            err = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                active = self._child
                self._child = None
            if active is not None and active[2] is not None:
                try:
                    os.close(active[2])
                except OSError:
                    pass
        dur = time.perf_counter() - t
        # Deletion-pending-confirmation is work, not success: a clean exit that
        # retained the pending marker must schedule its own confirming pass.
        pending = not err and _publication_pending()
        with self._lock:
            previous_err = self.state["last_err"]
            self._retry_needed = bool(err) or pending
            self._pending_streak = self._pending_streak + 1 if pending else 0
            self._fail_streak = self._fail_streak + 1 if err else 0
            if err:
                self._identical = (
                    self._identical + 1 if err == previous_err else 1)
            else:
                self._identical = 0
                self._escalated = False
            streak = self._fail_streak
            escalated = self._escalated
            self.state.update(phase="error" if err else "idle",
                              last_run=time.time(), last_dur=round(dur, 1),
                              last_err=err, runs=self.state["runs"] + 1)
        indexd_runtime.record_auto_index_health(
            streak, err or "", escalated=escalated)
        if not err:
            # the loop re-verifies on its next tick, refreshing the
            # verified-current record against the just-published generation
            self._last_verify_stamp = 0.0
        if pending:
            common.dbg("auto-index: publication pending after a clean pass; "
                       "confirming run scheduled")
        if err:
            common.log(f"auto-index failed: {err}")
        if escalate:
            common.log(
                "auto-index: --full escalation finished: failure streak "
                f"{streak_before} -> {streak}"
                + ("" if not err
                   else "; the next escalation requires a success first"))

    def _run_post_index_hooks(self) -> None:
        with self._lock:
            if self._stop_requested.is_set():
                return
            self._post_index_idle.clear()
        try:
            run_post_index_hooks(stop_requested=self._stop_requested.is_set)
        finally:
            self._post_index_idle.set()


_INSTANCE: AutoIndexer | None = None


def start(
        watcher, *,
        owns_lifetime: Callable[[], bool],
        owner_snapshot: ownerfile.Snapshot) -> AutoIndexer:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AutoIndexer(
            watcher, owns_lifetime=owns_lifetime,
            owner_snapshot=owner_snapshot)
        _INSTANCE.start()
    return _INSTANCE


def instance() -> AutoIndexer | None:
    return _INSTANCE
