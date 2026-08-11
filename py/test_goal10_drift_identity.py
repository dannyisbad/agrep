"""Goal 10 D3 content-identity controls: drift the mtime clock cannot see.

The published source observation was (files, newest_mtime_ms); a same-count
rewrite with the newest mtime restored false-greened as current. The record
now carries change-sensitive identity - the ingest generation signature it
vouches for and per-store digests over member (path, size, mtime) rows - and
the debounce horizon only defends drift a background owner will absorb.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)
import surface_policy as surface  # noqa: E402
from test_goal10_freshness import _census, _publication  # noqa: E402


class StoreChangeDigest(unittest.TestCase):
    def test_restored_mtime_rewrite_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            member = Path(raw) / "session.jsonl"
            member.write_text("{}\n" * 4, encoding="utf-8")
            stamp = member.stat()
            before = indexd_runtime._store_change_digest([str(member)])
            member.write_text("{}\n" * 9, encoding="utf-8")
            os.utime(member, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            after = indexd_runtime._store_change_digest([str(member)])
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)

    def test_unchanged_members_keep_a_stable_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            member = Path(raw) / "session.jsonl"
            member.write_text("{}\n", encoding="utf-8")
            first = indexd_runtime._store_change_digest([str(member)])
            second = indexd_runtime._store_change_digest([str(member)])
        self.assertEqual(first, second)

    def test_oversized_or_unreadable_stores_claim_nothing(self) -> None:
        too_many = [f"/nope/{i}" for i in range(
            indexd_runtime._STORE_DIGEST_MAX_FILES + 1)]
        self.assertIsNone(indexd_runtime._store_change_digest(too_many))
        self.assertIsNone(
            indexd_runtime._store_change_digest(["/nope/missing.jsonl"]))


class RestoredMtimeRewrite(unittest.TestCase):
    """The exact false-green: same count, newest mtime restored, bytes moved."""

    def _fixture(self, rewrite: bool):
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        store = Path(raw.name) / "store"
        store.mkdir()
        members = [store / "a.jsonl", store / "b.jsonl"]
        old = time.time() - 9000
        for member in members:
            member.write_text("{}\n" * 3, encoding="utf-8")
            os.utime(member, (old, old))
        paths = [str(member) for member in members]
        digest = indexd_runtime._store_change_digest(paths)
        if rewrite:
            members[1].write_text("{}\n" * 8, encoding="utf-8")
            os.utime(members[1], (old, old))
        newest_ms = int(old * 1000)
        record = {"version": 1, "ts": time.time() - 2520,
                  "census": {"claude": [2, newest_ms]},
                  "digests": {"claude": digest}}
        return Path(raw.name) / "data", _census(newest_ms, files=2), record, {
            "claude": paths}

    def test_rewrite_under_a_restored_clock_is_disclosed(self) -> None:
        root, census, record, paths = self._fixture(rewrite=True)
        root.mkdir()
        with _publication(root, census, record=record), \
                mock.patch.object(
                    indexd_runtime, "_consume_paths_probe",
                    return_value=paths):
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("behind", notice)
        self.assertIn("1 store changed", notice)
        self.assertTrue(disclosure["may_be_stale"])
        self.assertEqual(disclosure["changed_stores"], 1)

    def test_live_freshener_never_outranks_the_provable_rewrite(self) -> None:
        # The live-but-stale control: a hot freshener heartbeat must not
        # suppress drift the member identity proves - truth over liveness.
        root, census, record, paths = self._fixture(rewrite=True)
        root.mkdir()
        with _publication(root, census, record=record), \
                mock.patch.object(
                    indexd_runtime, "_consume_paths_probe",
                    return_value=paths), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=True):
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("behind", notice)
        self.assertTrue(disclosure["may_be_stale"])

    def test_untouched_members_stay_green(self) -> None:
        root, census, record, paths = self._fixture(rewrite=False)
        root.mkdir()
        with _publication(root, census, record=record), \
                mock.patch.object(
                    indexd_runtime, "_consume_paths_probe",
                    return_value=paths):
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")

    def test_idle_restamp_refuses_to_launder_the_contradiction(self) -> None:
        root, census, record, paths = self._fixture(rewrite=True)
        root.mkdir()
        live = {"claude": tuple(record["census"]["claude"])}
        with _publication(root, census, record=record), \
                mock.patch.object(
                    indexd_runtime, "_store_paths_census",
                    return_value=paths), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)), \
                mock.patch.object(
                    indexd_runtime, "indexd_failing", return_value=(0, "")), \
                mock.patch.object(
                    indexd_runtime, "_census_map",
                    return_value=(live, ())):
            self.assertFalse(indexd_runtime.stamp_verified_current())


class YoungDriftWithoutAnOwner(unittest.TestCase):
    """Grace defends the daemon's catch-up window, not its absence."""

    def test_unabsorbed_young_drift_serves_last_good_with_a_line(self) -> None:
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 30,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(time.time() * 1000), files=3)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record), \
                mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}):
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("last-good", notice)
        self.assertIn("1 store changed", notice)
        self.assertNotIn("behind", notice)
        self.assertNotIn("catching up", notice)
        self.assertEqual(disclosure["state"], "index-behind")
        self.assertTrue(disclosure["may_be_stale"])

    def test_the_same_drift_stays_green_when_a_daemon_may_absorb_it(
            self) -> None:
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 30,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(time.time() * 1000), files=3)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")
            story = indexd_runtime.freshness_story()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(story.state, "current")
        self.assertTrue(story.absorbed_drift)
        self.assertEqual(
            surface.grep_absence_exit(exact=True, freshness=story), 2)
        self.assertEqual(disclosure["state"], "index-behind")
        self.assertTrue(disclosure["may_be_stale"])
        self.assertEqual(disclosure["cause"], "store-drift")
        self.assertEqual(disclosure["changed_stores"], 1)


class GenerationBoundRecord(unittest.TestCase):
    def test_record_for_a_dead_generation_cannot_vouch(self) -> None:
        # The record pins its ingest signature; the fixture's live one says
        # "1:fixture", so the verdict falls to the signal fallback, which
        # sees the store write past the horizon.
        stale_ms = int((time.time() - 600) * 1000)
        record = {"version": 1, "ts": time.time() - 30,
                  "census": {"claude": [3, stale_ms]},
                  "signature": "1:some-other-generation"}
        live = _census(stale_ms)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)

    def test_matching_signature_keeps_the_record_vouching(self) -> None:
        stale_ms = int((time.time() - 600) * 1000)
        record = {"version": 1, "ts": time.time() - 30,
                  "census": {"claude": [3, stale_ms]},
                  "signature": "1:fixture"}
        live = _census(stale_ms)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")


class RecordIdentityRoundTrip(unittest.TestCase):
    def test_digests_and_signature_survive_the_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".ingest.sig").write_text("7:sigtext\n", encoding="utf-8")
            digest = "ab" * 32
            with mock.patch.object(
                    indexd_runtime.common, "DATA_DIR", root), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)):
                self.assertTrue(indexd_runtime.record_verified_current(
                    {"claude": (3, 123456)}, wall=100.0,
                    digests={"claude": digest}))
                record = indexd_runtime._read_verified_record()
        self.assertEqual(record.ts, 100.0)
        self.assertEqual(record.census, {"claude": (3, 123456)})
        self.assertEqual(record.digests, {"claude": digest})
        self.assertEqual(record.signature, "7:sigtext")

    def test_malformed_identity_fields_invalidate_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / indexd_runtime.VERIFIED_CURRENT_FILE
            base = {"version": 1, "ts": 1, "census": {"claude": [3, 4]}}
            for bad in (
                {**base, "digests": {"claude": "zz" * 32}},
                {**base, "digests": {"unknown-store": "ab" * 32}},
                {**base, "digests": {"claude": 7}},
                {**base, "signature": ""},
                {**base, "signature": 12},
            ):
                path.write_text(json.dumps(bad), encoding="utf-8")
                with mock.patch.object(
                        indexd_runtime.common, "DATA_DIR", root):
                    self.assertIsNone(
                        indexd_runtime._read_verified_record(), bad)


class InteractiveProbeBudget(unittest.TestCase):
    def test_the_interactive_census_never_stalls_the_search_path(self) -> None:
        # Sub-500ms observation or an immediate honest unknown - the charter
        # bound; slow callers (stampers, doctor) pass their budgets explicitly.
        self.assertLessEqual(indexd_runtime._DRIFT_PROBE_TIMEOUT_S, 0.5)


if __name__ == "__main__":
    unittest.main()
