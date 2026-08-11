"""agrep-indexd: the headless freshness daemon.

Keeps the materialized index and corpus.db warm and converging in the background so searches
usually avoid a foreground refresh. It owns live-store watching and automatic Rust ingest plus
incremental FTS refresh; it does not host the web UI or semantic models. The CLI spawns it when
no compatible freshness owner exists, and it exits after IDLE_EXIT_S without search or source
activity.

A protocol/build-versioned descriptor prevents duplicate daemons and carries a diagnostic
heartbeat: acquired O_EXCL at startup (a lock held by the exact LIVE daemon means another
owns freshness, so we exit), its mtime bumped every BEAT_S, removed on exit. Kernel process
birth identity decides liveness; the mtime must not cause a healthy daemon to be killed when
a laptop resumes after sleep.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
from hookless import live  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import ownerfile  # noqa: E402

BEAT_S = 5.0  # how often we bump the diagnostic heartbeat
# Active transcript writers need a resident watcher even before the next search. Exit only when
# both searches and source events have been quiet; memory pressure retains its separate release.
try:
    IDLE_EXIT_S = float(os.environ.get("AGREP_INDEXD_IDLE_S", "") or 10 * 60)
except ValueError:
    IDLE_EXIT_S = 10 * 60
RESOURCE_CHECK_S = 60.0
PRESSURE_GRACE_S = 60.0


def _idle_reference(*, started: float, last_search: float,
                    last_source_event: float) -> float:
    return max(started, last_search, last_source_event)


def _resource_release_allowed(phase: str, memory_fraction: float | None) -> bool:
    """Only relinquish an idle daemon under actual host-memory pressure.

    The daemon's own CPU time is useful indexing work, not evidence that the
    host is contended.  In particular, never tear down a long cold build just
    because it has crossed a sampling or search-idle boundary.
    """
    return phase == "idle" and memory_fraction is not None and memory_fraction < 0.10


def _acquire() -> ownerfile.Handle | None:
    """Acquire the exact daemon lifetime owner without stealing live peers."""
    return indexd_runtime.acquire_indexd_owner()


def _available_memory_fraction() -> float | None:
    return common.available_memory_fraction()


def _publish_live(owner, watcher) -> ownerfile.Snapshot | None:
    """Keep optional board IPC failures from wounding freshness ownership."""
    try:
        return indexd_runtime.publish_indexd_live_snapshot(
            owner, watcher.snapshot())
    except ownerfile.OwnershipLost:
        raise
    except Exception as exc:  # noqa: BLE001 -- this cache is never authoritative
        common.dbg(
            f"indexd: resident live snapshot unavailable: "
            f"{type(exc).__name__}: {exc}")
        return None


def _await_operational(watcher, auto_indexer, owner, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if auto_indexer.wait_operational(timeout=min(1.0, remaining)):
            return watcher.is_alive() and auto_indexer.is_alive()
        if not watcher.is_alive() or not auto_indexer.is_alive():
            return False
        indexd_runtime.heartbeat_indexd_owner(owner)


def _install_shutdown_handlers(
        shutdown: threading.Event,
        caught: list[int | None]) -> dict[int, object]:
    """Turn catchable daemon signals into an orderly owned-tree teardown."""
    previous: dict[int, object] = {}

    def request_shutdown(signum, _frame) -> None:
        caught[0] = caught[0] or signum
        shutdown.set()

    for signum in (
            signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)
        except (OSError, ValueError):
            previous.pop(signum, None)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def main() -> int:
    if common.data_dir_readonly(common.DATA_DIR):
        common.log(
            "indexd: AGREP_DATA_READONLY protects this data directory; exiting.")
        return 0
    ownership = indexd_runtime.derived_writer_mutation_info()
    if not ownership.writable:
        # A dead owner's claim is reaped, not respected: with no live indexer
        # holding the directory, this daemon's first publication performs the
        # successor takeover in the Rust ingest.
        if not indexd_runtime.derived_writes_permitted():
            common.log(
                f"indexd: {ownership.reason}; serving remains read-only. "
                "exiting.")
            return 0
        common.log(f"indexd: {ownership.reason}; no live owner holds the "
                   "indexer - taking over on the next publication.")
    common.enable_log_timestamps()
    if not common.ingest_bin().exists():
        common.log("indexd: no ingest binary; nothing to keep fresh. exiting.")
        return 0
    indexer.configure_indexd_mode()
    if not common.bind_descendants_to_process_lifetime():
        common.log("indexd: could not bind child processes to daemon lifetime; exiting.")
        return 0
    if common.process_start_identity(os.getpid()) is None:
        common.log("indexd: kernel process identity is unavailable; exiting safely.")
        return 0
    owner = _acquire()
    if owner is None:
        common.log("indexd: another daemon already owns freshness; exiting.")
        return 0
    auto_indexer = None
    ready = None
    live_publication = None
    shutdown = threading.Event()
    caught_signal: list[int | None] = [None]
    previous_handlers = _install_shutdown_handlers(shutdown, caught_signal)

    def exit_code() -> int:
        return (128 + caught_signal[0]
                if caught_signal[0] is not None else 0)

    def owns_lifetime() -> bool:
        owner.verify()
        return True

    try:
        owner.verify()
        w = live.watcher(headless_indexd=True)
        owner.verify()
        auto_indexer = indexer.start(
            w, owns_lifetime=owns_lifetime,
            owner_snapshot=owner.snapshot)
        owner.verify()
        if (not _await_operational(w, auto_indexer, owner)
                or not w.is_alive() or not auto_indexer.is_alive()):
            common.log("indexd: freshness worker failed during startup; exiting.")
            return exit_code()
        if shutdown.is_set():
            common.log(
                f"indexd: shutdown requested by signal {caught_signal[0]}; exiting.")
            return exit_code()
        ready = indexd_runtime.publish_indexd_ready(owner)
        live_publication = _publish_live(owner, w)
        common.log(f"indexd: keeping the index fresh (pid {os.getpid()}); "
                   f"idle-exit {IDLE_EXIT_S / 60:.0f}m after search and "
                   "source activity stop.")
        started = time.time()  # floor so a daemon with no search beat yet doesn't insta-exit
        resource_wall = time.monotonic()
        resource_cpu = time.process_time()
        while not shutdown.wait(BEAT_S):
            try:
                indexd_runtime.heartbeat_indexd_owner(owner)
                ready.verify()
            except OSError as exc:
                common.log(
                    "indexd: ownership heartbeat failed: "
                    f"{type(exc).__name__}: {exc}")
                return exit_code()
            published = _publish_live(owner, w)
            if published is not None:
                live_publication = published
            if not w.is_alive() or not auto_indexer.is_alive():
                common.log("indexd: freshness worker exited; restarting on next search.")
                return exit_code()
            try:
                last_search = indexd_runtime.SEARCH_BEAT_PATH.stat().st_mtime
            except OSError:
                last_search = 0.0
            phase = str(auto_indexer.status().get("phase", "idle"))
            last_source_event = float(
                getattr(w, "_last_event_wall", 0.0) or 0.0)
            active_at = _idle_reference(
                started=started, last_search=last_search,
                last_source_event=last_source_event)
            if phase != "indexing" and time.time() - active_at > IDLE_EXIT_S:
                common.log("indexd: no searches or source activity recently; "
                           "exiting (next search respawns me).")
                return exit_code()
            now_mono = time.monotonic()
            if now_mono - resource_wall >= RESOURCE_CHECK_S:
                cpu_fraction = ((time.process_time() - resource_cpu)
                                / max(0.001, now_mono - resource_wall))
                memory_fraction = _available_memory_fraction()
                resource_wall, resource_cpu = now_mono, time.process_time()
                idle_age = time.time() - active_at
                if (_resource_release_allowed(phase, memory_fraction)
                        and idle_age > PRESSURE_GRACE_S):
                    mem = (f"{memory_fraction * 100:.1f}% free"
                           if memory_fraction is not None else "unknown memory")
                    common.log(f"indexd: releasing under resource pressure ({mem}, "
                               f"{cpu_fraction * 100:.1f}% daemon CPU); next search respawns me.")
                    return exit_code()
        common.log(
            f"indexd: shutdown requested by signal {caught_signal[0]}; exiting.")
        return exit_code()
    except ownerfile.OwnershipLost as exc:
        common.log(f"indexd: ownership lost: {exc}")
        return exit_code()
    finally:
        if live_publication is not None:
            try:
                indexd_runtime.remove_indexd_live_snapshot(
                    owner.snapshot, live_publication)
            except OSError:
                pass
        if ready is not None:
            try:
                ready.release(tombstone=True, require_stable_mtime=True)
            except OSError:
                pass
        release_owner = True
        if auto_indexer is not None:
            auto_indexer.stop()
            auto_indexer.join(timeout=3.0)
            release_owner = (
                not auto_indexer.is_alive()
                        and indexd_runtime._indexd_child_active(
                            owner.snapshot) is False
            )
        if release_owner:
            try:
                owner.release(tombstone=True, require_stable_mtime=True)
            except OSError:
                pass
        _restore_signal_handlers(previous_handlers)


def _entrypoint(argv: list[str]) -> int:
    if argv == ["--refresh-search-index-child"]:
        return indexd_runtime.search_index_refresh_child_exit()
    return main()


if __name__ == "__main__":
    raise SystemExit(_entrypoint(sys.argv[1:]))
