"""agrep-indexd: the headless freshness daemon.

Keeps the materialized index + corpus.db hot in the background so `agrep` searches are
instant AND current without anyone running `agrep warm` / `agrep ui`. It is the live store
watcher plus the auto-indexer (rust ingest + incremental FTS refresh) and NOTHING else - no
web server, no semantic models, no GPU/LLM stages. The CLI spawns it on a search when nothing
is keeping the index fresh, and it self-exits after IDLE_EXIT_S with no search/agent activity.

A single .indexd.lock both prevents duplicate daemons and serves as the liveness heartbeat:
acquired O_EXCL at startup (a still-fresh lock means another daemon owns freshness, so we
exit), its mtime bumped every BEAT_S, removed on exit. The CLI reads that mtime to decide
whether a daemon is alive (see common._freshener_alive). Portable - no pid-liveness syscalls.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Must be set before importing indexer: it reads this at module load to pick its (tight) gates.
os.environ["AGREP_INDEXD"] = "1"

import common  # noqa: E402
import live  # noqa: E402
import indexer  # noqa: E402

# Shared with the CLI side via common so the two can't drift (the CLI reads this lock's mtime
# to decide a daemon is alive; reclaim/liveness must agree on the same path + window).
LOCK = common.INDEXD_LOCK_PATH
SEARCH_BEAT = common.SEARCH_BEAT_PATH  # the CLI touches this on each search
STALE_S = common.INDEXD_STALE_S        # a lock older than this is a dead daemon; reclaim it
BEAT_S = 5.0                           # how often we bump the lock mtime (our heartbeat)
# Exit this long after the last SEARCH. The daemon exists to serve searches, so it idles out
# on search inactivity, NOT on agent activity - no point reindexing for hours if nobody greps.
# The next search just respawns it. Tunable (and testable) via AGREP_INDEXD_IDLE_S.
try:
    IDLE_EXIT_S = float(os.environ.get("AGREP_INDEXD_IDLE_S", "") or 30 * 60)
except ValueError:
    IDLE_EXIT_S = 30 * 60


def _acquire() -> int | None:
    """O_EXCL the lock. A still-fresh lock means a live daemon already owns freshness -> None
    (caller exits). A stale lock from a dead daemon is reclaimed."""
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"pid={os.getpid()} ts={time.time():.0f}\n".encode())
            return fd
        except FileExistsError:
            try:
                age = time.time() - LOCK.stat().st_mtime
            except OSError:
                continue  # vanished between create and stat; retry the create
            if age < STALE_S:
                return None
            try:
                LOCK.unlink()
            except OSError:
                return None


def main() -> int:
    if not common.ingest_bin().exists():
        common.log("indexd: no ingest binary; nothing to keep fresh. exiting.")
        return 0
    fd = _acquire()
    if fd is None:
        common.log("indexd: another daemon already owns freshness; exiting.")
        return 0
    try:
        w = live.watcher()  # passive store tailing -> feeds the auto-indexer's activity gate
        indexer.start(w, auto_smart=False)  # rust ingest + corpus refresh only; no GPU/LLM
        common.log(f"indexd: keeping the index fresh (pid {os.getpid()}); "
                   f"idle-exit {IDLE_EXIT_S / 60:.0f}m after the last search.")
        started = time.time()  # floor so a daemon with no search beat yet doesn't insta-exit
        while True:
            time.sleep(BEAT_S)
            try:
                os.utime(LOCK, None)  # heartbeat: bump mtime so the CLI sees us alive
            except OSError:
                return 0  # our lock was removed out from under us; bow out
            # The full explorer server keeps the index fresh too; if one came up, step aside
            # rather than run two indexers fighting over the same lock.
            if common._server_running():
                common.log("indexd: explorer server now owns freshness; exiting.")
                return 0
            try:
                last_search = SEARCH_BEAT.stat().st_mtime
            except OSError:
                last_search = 0.0
            if time.time() - max(last_search, started) > IDLE_EXIT_S:
                common.log("indexd: no searches recently; exiting (next search respawns me).")
                return 0
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
