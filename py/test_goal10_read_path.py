"""Goal 10 D2: readers never pay writer costs.

The 57s class of failure - an interactive search rebuilding derived stores
because the daemon is wedged - is pinned deleted: a wedged daemon yields the
last-good snapshot fast, with the failing disclosure, and without moving a
byte of the published derived stores.
"""

from __future__ import annotations

import json
import os
import time
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd_runtime  # noqa: E402


@contextmanager
def _wedged_box(root: Path, *, streak: int = 44):
    """A published corpus whose daemon is alive but failing: messages, a
    garbage corpus.db a reader must not repair, an old signal, and a
    persisted failure streak."""
    messages = root / "messages.jsonl"
    messages.write_text(
        json.dumps({"id": "codex:a:1", "session": "a", "agent": "codex",
                    "turn": 1, "text": "needle"}) + "\n",
        encoding="utf-8")
    db_path = root / "corpus.db"
    db_path.write_bytes(b"not a sqlite database, and nobody may fix that here")
    signature = root / ".ingest.sig"
    signature.write_text("1:fixture\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(signature, (old, old))
    os.utime(db_path, (old, old))
    (root / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
        json.dumps({"streak": streak, "last_err": "ingest exited 101",
                    "ts": time.time()}), encoding="utf-8")
    absent = indexd_runtime._IndexdOwnerInspection(
        indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
    with ExitStack() as stack:
        for patch in (
            mock.patch.object(common, "DATA_DIR", root),
            mock.patch.object(common, "MESSAGES_PATH", messages),
            mock.patch.object(common, "INGEST_SIG_PATH", signature),
            mock.patch.object(common, "setting", return_value="off"),
            mock.patch.object(corpusdb, "DB_PATH", db_path),
            mock.patch.object(corpusdb, "INGEST_SIG_PATH", signature),
            mock.patch.object(
                corpusdb, "BOUNDARY_STATS_PATH", root / "boundary_stats.json"),
            mock.patch.object(corpusdb, "CHANGED_PATH", root / ".changed"),
            mock.patch.object(
                common, "ingest_bin", return_value=Path(__file__)),
            mock.patch.object(indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"),
            mock.patch.object(
                indexd_runtime, "freshener_alive", return_value=True),
            mock.patch.object(indexd_runtime, "_arm_drift_probe"),
            mock.patch.object(indexd_runtime, "_store_census", return_value=[]),
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner", return_value=absent),
        ):
            stack.enter_context(patch)
        indexd_runtime._clear_freshen_failure()
        try:
            yield db_path
        finally:
            indexd_runtime._clear_freshen_failure()


class ReadersNeverPayWriterCosts(unittest.TestCase):
    def test_wedged_daemon_serves_stale_fast_without_rewriting_stores(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                _wedged_box(Path(raw)) as db_path:
            before_bytes = db_path.read_bytes()
            before_stat = db_path.stat()
            started = time.perf_counter()
            self.assertTrue(indexd_runtime.ensure_index(auto=True))
            db = corpusdb.connect(allow_stale=True)
            notice = indexd_runtime.agent_freshness_notice()
            elapsed = time.perf_counter() - started
            if db is not None:
                db.close()
            after_stat = db_path.stat()
            after_bytes = db_path.read_bytes()
        self.assertLess(elapsed, 0.5)
        # the reader served what was published and repaired nothing
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        # and the staleness is disclosed, not hidden
        self.assertIn("44 consecutive", notice)
        self.assertIn("history may be stale", notice)

    def test_interactive_read_lane_cannot_reach_writer_machinery(self) -> None:
        wall = mock.Mock(
            side_effect=AssertionError("writer machinery on the read lane"))
        with tempfile.TemporaryDirectory() as raw, \
                _wedged_box(Path(raw)), \
                mock.patch.object(corpusdb, "_build", wall), \
                mock.patch.object(corpusdb, "_incremental", wall), \
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps", wall), \
                mock.patch.object(indexd_runtime, "build_index", wall):
            self.assertTrue(indexd_runtime.ensure_index(auto=True))
            db = corpusdb.connect(allow_stale=True)
            if db is not None:
                db.close()

    def test_the_inline_reingest_mechanism_is_deleted(self) -> None:
        # C2's mechanism: the pre-search inline re-ingest. Pinned gone, so a
        # regression must consciously reintroduce it against this test.
        self.assertFalse(hasattr(indexd_runtime, "_sync_freshen"))


if __name__ == "__main__":
    unittest.main()
