"""Freshness is drift, not duty-cycle.

The three-state contract says an undrifted box answers green with no daemon at
all, real drift is disclosed with its age, and a wedged daemon serves the
last-good index behind a fast, honest disclosure. The wall-clock signal age is
dead as a read-time verdict.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()


# This suite is about daemon lifecycle semantics, so the isolation default
# (AGREP_NO_DAEMON) lifts for exactly this module's run - at setUpModule
# time, never import time; every real spawn below is mocked or walled.
_daemon_default = None


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)


import common  # noqa: E402
import session_context  # noqa: E402
import surface_policy as surface  # noqa: E402


def _census(newest_ms: int, files: int = 3, name: str = "claude") -> list[dict]:
    return [{"name": name, "files": files, "state": "available",
             "newest_mtime_ms": newest_ms}]


@contextmanager
def _publication(
        root: Path, census: list[dict], *,
        sig_age_s: float | None = 7200.0,
        record: dict | None = None,
        streak: int = 0, streak_reason: str = ""):
    """A published corpus in `root` with a controlled drift fixture: no part
    of the verdict may depend on the developer box's live stores or daemon."""
    (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
    signature = root / ".ingest.sig"
    if sig_age_s is not None:
        signature.write_text("1:fixture\n", encoding="utf-8")
        old = time.time() - sig_age_s
        os.utime(signature, (old, old))
    if record is not None:
        (root / indexd_runtime.VERIFIED_CURRENT_FILE).write_text(
            json.dumps(record), encoding="utf-8")
    if streak:
        (root / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
            json.dumps({"streak": streak, "last_err": streak_reason,
                        "ts": time.time()}), encoding="utf-8")
    absent = indexd_runtime._IndexdOwnerInspection(
        indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
    with ExitStack() as stack:
        for patch in (
            mock.patch.object(common, "DATA_DIR", root),
            mock.patch.object(common, "MESSAGES_PATH", root / "messages.jsonl"),
            mock.patch.object(common, "INGEST_SIG_PATH", signature),
            mock.patch.object(common, "ingest_bin", return_value=Path(__file__)),
            mock.patch.object(
                indexd_runtime, "_store_census", return_value=census),
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner", return_value=absent),
            # The developer box running these tests may itself be a calling
            # agent session; caller identity is a per-test fixture, never
            # ambient.
            mock.patch.object(
                session_context, "calling_identity",
                return_value=session_context.CallerIdentity(
                    None, "caller-unresolved")),
        ):
            stack.enter_context(patch)
        indexd_runtime._clear_freshen_failure()
        try:
            yield
        finally:
            indexd_runtime._clear_freshen_failure()


class DriftIsTheFreshnessVerdict(unittest.TestCase):
    def test_first_publication_invalidates_the_process_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        indexd_runtime, "_store_census", return_value=[]):
                indexd_runtime._clear_freshen_failure()
                self.addCleanup(indexd_runtime._clear_freshen_failure)
                self.assertEqual(indexd_runtime._drift_report().state, "current")
                (root / "messages.jsonl").write_text("", encoding="utf-8")
                report = indexd_runtime._drift_report()
        self.assertEqual(report.state, "unknown")
        self.assertEqual(report.code, "missing-ingest-signal")

    def test_burst_opener_after_idle_on_undrifted_corpus_is_green(self) -> None:
        # The Monte-Carlo killer case: first search after a long idle gap, no
        # daemon process anywhere, signal hours old - and the stores did not
        # move since the verified generation. The answer is green, honestly.
        now_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, now_ms]}}
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(now_ms), record=record):
            self.assertIsNone(indexd_runtime.indexing_failure())
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(disclosure["state"], "no-known-failure")
        self.assertFalse(disclosure["failing"])
        self.assertTrue(disclosure["checked"])

    def test_stale_snapshot_discloses_even_when_the_census_says_current(
            self) -> None:
        # the served index's own stamp is invisible to the census: a recorded
        # stale-snapshot serve must disclose even on a census-green box
        now_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, now_ms]}}
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(now_ms), record=record):
            indexd_runtime.disclose_foreground_snapshot(direct_scan=False)
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("may be stale", notice)
        self.assertIn("published search snapshot", notice)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(disclosure["code"], "search-index-stale")
        self.assertTrue(disclosure["may_be_stale"])

    def test_verified_publisher_makes_stale_snapshot_work_in_progress(
            self) -> None:
        now_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, now_ms]}}
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(now_ms), record=record):
            indexd_runtime.defer_foreground_refresh(
                indexd_runtime.FRESHENER_OWNS_REFRESH_REASON)
            indexd_runtime.disclose_foreground_snapshot(direct_scan=False)
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(
                checked=True, publication_converging=True)
        self.assertEqual(notice, "")
        self.assertEqual(disclosure, {
            "state": "index-behind", "failing": False,
            "may_be_stale": True, "code": "index-behind",
            "cause": "publication-in-progress", "checked": True,
        })

    def test_verified_publisher_never_hides_a_real_failure(self) -> None:
        self.addCleanup(indexd_runtime._clear_freshen_failure)
        failure = surface.FreshnessFailure(
            "consecutive-failures", "writer crashed", 3)
        indexd_runtime.defer_foreground_refresh(
            indexd_runtime.FRESHENER_OWNS_REFRESH_REASON)
        indexd_runtime.disclose_foreground_snapshot(direct_scan=False)
        disclosure = indexd_runtime.machine_freshness(
            checked=True, failure=failure, publication_converging=True)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(disclosure["code"], "consecutive-failures")
        self.assertEqual(disclosure["consecutive_failures"], 3)

    def test_verified_publisher_never_hides_an_ownership_refusal(self) -> None:
        self.addCleanup(indexd_runtime._clear_freshen_failure)
        indexd_runtime.defer_foreground_refresh(
            indexd_runtime.FRESHENER_OWNS_REFRESH_REASON)
        indexd_runtime.disclose_foreground_snapshot(
            direct_scan=False,
            code=indexd_runtime.DERIVED_STORE_OWNER_CODE,
            reason="another build owns the index")
        disclosure = indexd_runtime.machine_freshness(
            checked=True, failure=None, publication_converging=True)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(
            disclosure["code"], indexd_runtime.DERIVED_STORE_OWNER_CODE)

    def test_unchecked_request_never_claims_publication_progress(self) -> None:
        self.addCleanup(indexd_runtime._clear_freshen_failure)
        indexd_runtime.defer_foreground_refresh(
            indexd_runtime.FRESHENER_OWNS_REFRESH_REASON)
        indexd_runtime.disclose_foreground_snapshot(direct_scan=False)
        disclosure = indexd_runtime.machine_freshness(
            checked=False, failure=None, publication_converging=True)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(disclosure["code"], "search-index-stale")
        self.assertFalse(disclosure["checked"])

    def test_real_drift_during_idle_is_disclosed_with_age(self) -> None:
        # Negative control for the grace window: the store's last write is
        # 40 minutes old - far past the daemon's debounce horizon - so the
        # drift is real and must disclose, not ride the tolerance.
        stale_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 2520,
                  "census": {"claude": [3, stale_ms]}}
        live = _census(int((time.time() - 2400) * 1000), files=4)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            self.assertIsNone(indexd_runtime.indexing_failure())
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("42m behind", notice)
        self.assertIn("1 store changed", notice)
        # observing real drift now schedules its own absorption: the story
        # kicks the daemon (stubbed in-flight module-wide) and touches the
        # beat, so the promise is earned, and no manual command renders
        self.assertIn("catching up", notice)
        self.assertNotIn("agrep index", notice)
        self.assertEqual(disclosure["state"], "index-behind")
        self.assertFalse(disclosure["failing"])
        self.assertTrue(disclosure["may_be_stale"])
        self.assertEqual(disclosure["changed_stores"], 1)
        self.assertAlmostEqual(disclosure["behind_s"], 2520, delta=30)

    def test_wedged_daemon_disclosure_is_failing_fast_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(int(time.time() * 1000)),
                             sig_age_s=3600.0, streak=44,
                             streak_reason="ingest exited 101"):
            started = time.perf_counter()
            notice = indexd_runtime.agent_freshness_notice()
            disclosure = indexd_runtime.machine_freshness(checked=True)
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertIn("44 consecutive", notice)
        self.assertIn("last-good index from 60m ago", notice)
        self.assertIn("ingest exited 101", notice)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(disclosure["code"], "consecutive-failures")
        self.assertEqual(disclosure["consecutive_failures"], 44)

    def test_daemon_liveness_never_clears_a_failure_streak(self) -> None:
        # C3's other half: a crashlooping-but-alive freshener is not a
        # freshness owner; the persisted streak stays the story.
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(int(time.time() * 1000)),
                             streak=5, streak_reason="wedged"), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=True):
            failure = indexd_runtime.indexing_failure()
        self.assertEqual(failure.code, "consecutive-failures")
        self.assertEqual(failure.consecutive_failures, 5)

    def test_unverifiable_census_is_disclosed_not_greenwashed(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), None):  # census unavailable
            failure = indexd_runtime.indexing_failure()
            notice = indexd_runtime.agent_freshness_notice()
        self.assertEqual(failure.code, "census-unavailable")
        self.assertIn("history may be stale", notice)


class DriftGraceWindow(unittest.TestCase):
    """The blocker's tolerance: drift younger than the daemon's debounce
    horizon is the system working as designed and stays silent for humans;
    machines still disclose that the served generation may lag."""

    def test_live_session_write_within_debounce_horizon_is_machine_visible(
            self) -> None:
        # A live transcript post-dates publication. Humans stay quiet during
        # healthy debounce; machines refuse to certify the older generation.
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(time.time() * 1000), files=4)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            self.assertIsNone(indexd_runtime.indexing_failure())
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(disclosure["state"], "index-behind")
        self.assertTrue(disclosure["may_be_stale"])
        self.assertEqual(disclosure["cause"], "store-drift")
        self.assertEqual(disclosure["changed_stores"], 1)

    def test_fresh_record_graces_every_undated_change(self) -> None:
        # A record younger than the horizon bounds all drift age outright:
        # even shrink (undatable by mtime) happened after the stamp.
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 30,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(quiet_ms, files=2)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            self.assertIsNone(indexd_runtime.indexing_failure())
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")

    def test_shrink_past_the_horizon_is_disclosed(self) -> None:
        # Shrink has no fresh write to date it; past a stale record it is
        # behind - the record-path negative control for count-only changes.
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 2520,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(quiet_ms, files=2)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)
        self.assertIn("1 store changed", notice)

    def test_recordless_fallback_graces_recent_writes_only(self) -> None:
        # Same tolerance on the sig-baseline fallback: a write seconds old is
        # the debounce working; a write older than the horizon discloses.
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(int(time.time() * 1000))):
            self.assertIsNone(indexd_runtime.indexing_failure())
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw),
                             _census(int((time.time() - 600) * 1000))):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)

    def test_grace_horizon_covers_the_daemon_cadence_only(self) -> None:
        # QUIET_S (4s) + the marathon publication backstop (60s) + ingest
        # headroom, and never past the idle re-stamp rate: the tolerance is
        # the daemon's designed catch-up window, not a new wall-clock verdict.
        import indexer
        self.assertGreaterEqual(indexd_runtime.DRIFT_GRACE_S, 64.0)
        self.assertLessEqual(
            indexd_runtime.DRIFT_GRACE_S,
            indexd_runtime.FRESHNESS_WRITE_RATE_S)
        self.assertIsNotNone(indexer.QUIET_S)  # the cadence source survives

    def test_future_dated_store_write_cannot_greenwash(self) -> None:
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int((time.time() + 7200) * 1000), files=3)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)


class CallerOwnTranscript(unittest.TestCase):
    """Caller writes stay freshness-visible because query scope may include them."""

    SESSION = "b0d9e7f0-4242-4000-8000-freshnesscaller"

    @contextmanager
    def _caller_home(self, mtime_s: float):
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            transcript = (home / ".claude" / "projects" / "box" /
                          f"{self.SESSION}.jsonl")
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}\n", encoding="utf-8")
            os.utime(transcript, (mtime_s, mtime_s))
            with mock.patch.dict(os.environ, {
                    "HOME": str(home), "USERPROFILE": str(home)}), \
                    mock.patch.object(
                        session_context, "calling_identity",
                        return_value=session_context.CallerIdentity(
                            self.SESSION, "claude")):
                yield

    def test_no_boundary_includes_caller_and_blocks_exact_empty_exit(
            self) -> None:
        # This exact change used to be globally exempt because it matched the
        # caller file. With no recap, that file is in query scope.
        wrote = time.time() - 600
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(wrote * 1000), files=4)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record), \
                self._caller_home(wrote):
            family = session_context.CallingFamily(
                self.SESSION, self.SESSION, frozenset({self.SESSION}),
                True, None)
            with mock.patch.object(
                    session_context, "calling_family", return_value=family):
                self.assertIsNone(session_context.calling_self_exclusion())
                story = indexd_runtime.freshness_story()
        self.assertEqual(story.state, "behind")
        self.assertEqual(
            surface.grep_absence_exit(exact=True, freshness=story), 2)

    def test_caller_file_cannot_mask_other_changed_files(self) -> None:
        # Two new files cannot both be the caller's transcript: the store's
        # change is not fully explained, so the drift still discloses.
        wrote = time.time() - 600
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(wrote * 1000), files=5)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record), \
                self._caller_home(wrote):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)

    def test_unresolved_identity_leans_on_the_age_horizon(self) -> None:
        # No env identity: the fixture's default caller is unresolved, and
        # the same store change beyond the horizon discloses honestly.
        wrote = time.time() - 600
        quiet_ms = int((time.time() - 9000) * 1000)
        record = {"version": 1, "ts": time.time() - 7200,
                  "census": {"claude": [3, quiet_ms]}}
        live = _census(int(wrote * 1000), files=4)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), live, record=record):
            notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)


class CensusDegradesPerStore(unittest.TestCase):
    def test_one_unreadable_store_degrades_named_not_census_wide(self) -> None:
        rows = _census(int((time.time() - 9000) * 1000)) + [
            {"name": "gemini", "files": 2, "state": "source-unreadable",
             "newest_mtime_ms": 0,
             "issues": [{"path": "x", "reason": "permission denied"}]}]
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), rows):
            failure = indexd_runtime.indexing_failure()
            notice = indexd_runtime.agent_freshness_notice()
        self.assertEqual(failure.code, "store-unreadable")
        self.assertIn("gemini", failure.reason)
        self.assertIn("history may be stale", notice)
        self.assertIn("gemini", notice)

    def test_unreadable_store_blocks_the_verified_stamp(self) -> None:
        rows = _census(int((time.time() - 9000) * 1000)) + [
            {"name": "gemini", "files": 2, "state": "source-unreadable",
             "newest_mtime_ms": 0, "issues": []}]
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), rows), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)):
            self.assertFalse(indexd_runtime.stamp_verified_current())
            self.assertFalse(
                (Path(raw) / indexd_runtime.VERIFIED_CURRENT_FILE).exists())


class RecordlessStoreSetShrink(unittest.TestCase):
    def test_vanished_published_store_is_visible_without_a_record(
            self) -> None:
        # Shrink-only drift the mtime compare cannot see: the publication
        # knew a store that now has no files at all.
        quiet_ms = int((time.time() - 9000) * 1000)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sessions.jsonl").write_text(
                json.dumps({"session": "a", "agent": "claude"}) + "\n"
                + json.dumps({"session": "b", "agent": "gemini"}) + "\n",
                encoding="utf-8")
            with _publication(root, _census(quiet_ms)):
                notice = indexd_runtime.agent_freshness_notice()
        self.assertIn("behind", notice)
        self.assertIn("1 store changed", notice)

    def test_matching_store_set_stays_green(self) -> None:
        quiet_ms = int((time.time() - 9000) * 1000)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sessions.jsonl").write_text(
                json.dumps({"session": "a", "agent": "claude"}) + "\n",
                encoding="utf-8")
            with _publication(root, _census(quiet_ms)):
                self.assertEqual(indexd_runtime.agent_freshness_notice(), "")


class DriftProbeLifecycle(unittest.TestCase):
    def test_armed_probe_is_reaped_on_early_exit(self) -> None:
        # A command that dies between arming the census and rendering the
        # verdict must not strand a zombie child; the exit reaper is pinned.
        real_popen = indexd_runtime.subprocess.Popen

        def spawn_sleeper(_command, **kwargs):
            return real_popen(
                [indexd_runtime.sys.executable, "-c",
                 "import time; time.sleep(30)"],
                **kwargs)

        with (
            mock.patch.object(
                common, "ingest_bin", return_value=Path(__file__)),
            mock.patch.object(
                indexd_runtime, "_read_verified_record",
                return_value=None),
            mock.patch.object(
                indexd_runtime.subprocess, "Popen",
                side_effect=spawn_sleeper),
        ):
            indexd_runtime._arm_drift_probe()
        proc = getattr(indexd_runtime._DRIFT_PROBE, "proc", None)
        try:
            self.assertIsNotNone(proc)
            with indexd_runtime._DRIFT_PROBE_LOCK:
                self.assertIn(proc, indexd_runtime._DRIFT_PROBE_LIVE)
            indexd_runtime._reap_drift_probes()
            # killed AND waited: no zombie left for the OS to hold
            self.assertIsNotNone(proc.poll())
            with indexd_runtime._DRIFT_PROBE_LOCK:
                self.assertNotIn(proc, indexd_runtime._DRIFT_PROBE_LIVE)
        finally:
            indexd_runtime._DRIFT_PROBE.proc = None


    def test_exit_reaper_is_registered_with_atexit(self) -> None:
        import indexd_runtime as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertIn("atexit.register(_reap_drift_probes)", source)


class NoAutoSpawnsNothing(unittest.TestCase):
    def test_no_auto_never_arms_or_consumes_the_census(self) -> None:
        # --no-auto is the script-safe mode: no census probe, no daemon, no
        # freshen - not at ensure_index time and not resurrected later by the
        # read-time verdicts either.
        wall = mock.Mock(
            side_effect=AssertionError("census/daemon work under --no-auto"))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_arm_drift_probe", wall), \
                    mock.patch.object(indexd_runtime, "_store_census", wall), \
                    mock.patch.object(indexd_runtime, "_spawn_indexd", wall), \
                    mock.patch.object(indexd_runtime, "_maybe_freshen", wall):
                self.assertTrue(indexd_runtime.ensure_index(auto=False))
                indexd_runtime.agent_freshness_notice()
                disclosure = indexd_runtime.machine_freshness(checked=False)
            indexd_runtime._clear_freshen_failure()
        wall.assert_not_called()
        self.assertEqual(disclosure["state"], "unchecked")
        self.assertFalse(disclosure["failing"])

    def test_no_auto_fences_direct_and_freshness_story_repair_kicks(self) -> None:
        wall = mock.Mock(
            side_effect=AssertionError("repair work escaped --no-auto"))
        failure = surface.FreshnessFailure(
            indexd_runtime.DERIVED_STORE_OWNER_CODE, "foreign owner")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(indexd_runtime, "_arm_drift_probe", wall), \
                    mock.patch.object(indexd_runtime, "_store_census", wall), \
                    mock.patch.object(indexd_runtime, "_spawn_indexd", wall), \
                    mock.patch.object(indexd_runtime, "freshener_alive", wall), \
                    mock.patch.object(
                        indexd_runtime, "indexing_failure",
                        return_value=failure):
                try:
                    self.assertTrue(indexd_runtime.ensure_index(auto=False))
                    direct = indexd_runtime.kick_background_repair()
                    story = indexd_runtime.freshness_story()
                finally:
                    indexd_runtime._clear_freshen_failure()
        self.assertEqual(
            direct, indexd_runtime.RepairKick(False, "no-auto"))
        self.assertFalse(story.converging)
        wall.assert_not_called()


class VerifiedCurrentRecord(unittest.TestCase):
    def test_record_round_trips_and_rejects_malformed_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)):
                self.assertTrue(indexd_runtime.record_verified_current(
                    {"claude": (3, 123456)}, wall=100.0))
                self.assertEqual(
                    indexd_runtime.read_verified_current(),
                    (100.0, {"claude": (3, 123456)}))
                path = root / indexd_runtime.VERIFIED_CURRENT_FILE
                for bad in (
                    "[]",
                    json.dumps({"version": 2, "ts": 1, "census": {}}),
                    json.dumps({"version": 1, "ts": -1, "census": {}}),
                    json.dumps({"version": 1, "ts": 1,
                                "census": {"claude": [3]}}),
                    json.dumps({"version": 1, "ts": 1,
                                "census": {"claude": [3, -1]}}),
                    json.dumps({"version": 1, "ts": 1,
                                "census": {"claude": [3, 1.5]}}),
                ):
                    path.write_text(bad, encoding="utf-8")
                    self.assertIsNone(
                        indexd_runtime.read_verified_current(), bad)

    def test_stamp_bootstraps_cold_verified_from_the_signal(self) -> None:
        # Upgraded box: no record yet, old signal, stores quiet since the
        # last publication. The stamp proves it and mints the record, and a
        # daemonless search then answers green.
        quiet_ms = int((time.time() - 9000) * 1000)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(quiet_ms)), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)):
            root = Path(raw)
            self.assertTrue(indexd_runtime.stamp_verified_current())
            self.assertTrue(
                (root / indexd_runtime.VERIFIED_CURRENT_FILE).exists())
            indexd_runtime._clear_freshen_failure()
            self.assertEqual(indexd_runtime.agent_freshness_notice(), "")

    def test_stamp_refuses_when_stores_moved_past_the_signal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(int(time.time() * 1000))), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)):
            root = Path(raw)
            self.assertFalse(indexd_runtime.stamp_verified_current())
            self.assertFalse(
                (root / indexd_runtime.VERIFIED_CURRENT_FILE).exists())

    def test_stamp_defers_when_a_publication_lands_mid_census(self) -> None:
        # The mid-ingest race: an ingest that publishes while the census
        # walks could post-date content it never read. The signal identity is
        # pinned across the walk; movement defers the stamp.
        quiet_ms = int((time.time() - 9000) * 1000)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(quiet_ms)), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)):
            root = Path(raw)
            signature = root / ".ingest.sig"

            def census_during_ingest(timeout_s=None):
                # a publication lands while the census walks the stores
                signature.write_text("2:mid-ingest\n", encoding="utf-8")
                return _census(quiet_ms)

            with mock.patch.object(
                    indexd_runtime, "_store_census",
                    side_effect=census_during_ingest):
                self.assertFalse(indexd_runtime.stamp_verified_current())
            self.assertFalse(
                (root / indexd_runtime.VERIFIED_CURRENT_FILE).exists())
            # quiescent signal: the same stamp succeeds on the next lapse
            self.assertTrue(indexd_runtime.stamp_verified_current())
            self.assertTrue(
                (root / indexd_runtime.VERIFIED_CURRENT_FILE).exists())

    def test_stamp_refuses_under_a_failure_streak(self) -> None:
        quiet_ms = int((time.time() - 9000) * 1000)
        with tempfile.TemporaryDirectory() as raw, \
                _publication(Path(raw), _census(quiet_ms),
                             streak=3, streak_reason="wedged"), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)):
            self.assertFalse(indexd_runtime.stamp_verified_current())


class WallClockDemotion(unittest.TestCase):
    def test_the_read_time_age_verdict_is_deleted(self) -> None:
        self.assertFalse(hasattr(surface, "FRESHNESS_MAX_AGE_S"))
        self.assertFalse(hasattr(surface, "freshness_signal_failure"))
        self.assertFalse(hasattr(indexd_runtime, "CLI_FRESHEN_MAX_AGE_S"))
        # the constant survives only as the write-side stamp rate limit
        self.assertEqual(indexd_runtime.FRESHNESS_WRITE_RATE_S, 120.0)


class ExplicitIndexHandsOffTheSearchDatabase(unittest.TestCase):
    """`agrep index` publishes the corpus, then reports what it actually did
    about the FTS build: queued it, built it, or nothing owns it."""

    def tearDown(self) -> None:
        indexd_runtime._set_fts_delegated(False)
        indexd_runtime._FRESHEN_FAILURE.value = None

    @staticmethod
    def _logged(logged) -> list[str]:
        return [str(call.args[0]) for call in logged.call_args_list]

    def test_handoff_returns_without_building_inline(self) -> None:
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="stale"), \
                mock.patch.object(
                    indexd_runtime, "_run_search_index_build",
                    return_value=surface.IndexBuildOutcome.DELEGATED
                ) as delegated, \
                mock.patch.object(indexd_runtime, "refresh_search_index") as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        delegated.assert_called_once_with(announce=False)
        inline.assert_not_called()
        self.assertTrue(any(surface.EXPLICIT_INDEX_HANDOFF_LINE in str(c.args[0])
                            for c in logged.call_args_list))
        indexd_runtime._set_fts_delegated(False)

    def test_the_handoff_line_names_the_lane_and_its_one_gap(self) -> None:
        line = surface.EXPLICIT_INDEX_HANDOFF_LINE
        self.assertIn("searchable now", line)
        self.assertIn("still building", line)
        self.assertIn("tool output is not in results yet", line)

    def test_a_live_daemon_is_delegated_to_by_queueing_the_build(self) -> None:
        """Delegation is a durable request, so "handed off" names work that
        exists rather than a process that happens to be running."""
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="stale"), \
                mock.patch.object(
                    indexd_runtime, "_reclassify_indexd_spawn_failure",
                    return_value=indexd_runtime._IndexdSpawnResult.READY), \
                mock.patch.object(indexd_runtime, "_spawn_indexd"), \
                mock.patch.object(indexd_runtime, "request_search_index_build",
                                  return_value=True) as queued, \
                mock.patch.object(indexd_runtime, "refresh_search_index") as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
            self.assertTrue(indexd_runtime.fts_delegation_active())
        queued.assert_called_once_with()
        inline.assert_not_called()
        self.assertIn(surface.EXPLICIT_INDEX_HANDOFF_LINE, self._logged(logged))

    def test_a_live_daemon_that_cannot_be_queued_builds_here_instead(self) -> None:
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="stale"), \
                mock.patch.object(
                    indexd_runtime, "_reclassify_indexd_spawn_failure",
                    return_value=indexd_runtime._IndexdSpawnResult.READY), \
                mock.patch.object(indexd_runtime, "_spawn_indexd"), \
                mock.patch.object(indexd_runtime, "request_search_index_build",
                                  return_value=False), \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=True) as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        inline.assert_called_once_with(quiet=False)
        self.assertNotIn(
            surface.EXPLICIT_INDEX_HANDOFF_LINE, self._logged(logged))
        self.assertFalse(indexd_runtime.fts_delegation_active())

    def test_a_blocked_handoff_fails_and_promises_no_build(self) -> None:
        """B1: the state that records blocked-owner must not exit 0 behind a
        line saying something is building; nothing is."""
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="stale"), \
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.BLOCKED), \
                mock.patch.object(indexd_runtime, "refresh_search_index") as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertFalse(indexd_runtime.hand_off_search_index())
        inline.assert_not_called()
        lines = self._logged(logged)
        self.assertNotIn(surface.EXPLICIT_INDEX_HANDOFF_LINE, lines)
        self.assertTrue(any("nothing is building it" in line for line in lines))
        self.assertTrue(any("tool output is not in results yet" in line
                            for line in lines))
        self.assertEqual(
            indexd_runtime.indexing_failure().code, "blocked-owner")
        self.assertFalse(indexd_runtime.fts_delegation_active())

    def test_a_current_search_database_claims_no_background_build(self) -> None:
        """B3: a re-index of a complete store says nothing - there is no
        build in flight and no coverage missing from results."""
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="current"), \
                mock.patch.object(indexd_runtime,
                                  "_keep_freshness_owner_running") as owner, \
                mock.patch.object(indexd_runtime, "refresh_search_index") as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        owner.assert_called_once_with()
        inline.assert_not_called()
        self.assertEqual(self._logged(logged), [])
        self.assertFalse(indexd_runtime.fts_delegation_active())

    def test_no_daemon_builds_the_index_here_without_a_handoff_claim(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "_search_db_state",
                                  return_value="stale"), \
                mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn, \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=True) as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        spawn.assert_not_called()
        inline.assert_called_once_with(quiet=False)
        self.assertEqual(self._logged(logged), [])
        self.assertFalse(indexd_runtime.fts_delegation_active())

    def test_no_daemon_never_asserts_a_full_history_scan_it_did_not_measure(
            self) -> None:
        """B4: the conditional announcement inside the build owns this
        surface; nothing above it may assert what the build will cost."""
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=True), \
                mock.patch.object(common, "log") as logged:
            self.assertFalse(indexd_runtime._delegate_fts_build())
        self.assertEqual(self._logged(logged), [])

    def test_a_failed_inline_fallback_still_fails_the_command(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "_search_db_state",
                                  return_value="stale"), \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=False), \
                mock.patch.object(common, "log") as logged:
            self.assertFalse(indexd_runtime.hand_off_search_index())
        self.assertTrue(indexd_runtime.inline_refresh_failed())
        self.assertTrue(any("could not be rebuilt" in line
                            for line in self._logged(logged)))
        indexd_runtime._set_fts_delegated(False)

    def test_a_successful_inline_fallback_succeeds(self) -> None:
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="stale"), \
                mock.patch.object(
                    indexd_runtime, "_run_search_index_build",
                    return_value=surface.IndexBuildOutcome.BUILT), \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        self.assertEqual(self._logged(logged), [])
        indexd_runtime._set_fts_delegated(False)

    def test_delegation_records_a_failed_inline_build(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=False), \
                mock.patch.object(indexd_runtime, "_set_freshen_failure"), \
                mock.patch.object(common, "log"):
            self.assertFalse(indexd_runtime._delegate_fts_build())
            self.assertTrue(indexd_runtime.inline_refresh_failed())
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "refresh_search_index",
                                  return_value=True), \
                mock.patch.object(common, "log"):
            self.assertFalse(indexd_runtime._delegate_fts_build())
            self.assertFalse(indexd_runtime.inline_refresh_failed())

    def test_an_unsupported_sqlite_reports_no_build_and_no_failure(self) -> None:
        with mock.patch.object(indexd_runtime, "_search_db_state",
                               return_value="unsupported"), \
                mock.patch.object(indexd_runtime,
                                  "_keep_freshness_owner_running"), \
                mock.patch.object(indexd_runtime, "refresh_search_index") as inline, \
                mock.patch.object(common, "log") as logged:
            self.assertTrue(indexd_runtime.hand_off_search_index())
        inline.assert_not_called()
        self.assertEqual(self._logged(logged), [])


class ADelegatedBuildIsAQueuedWorkItem(unittest.TestCase):
    """B2: the daemon converges on a MISSING search db only because the
    handoff leaves it a durable request; nothing else in its loop would."""

    @contextmanager
    def _data_dir(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(common, "DATA_DIR", root):
                yield root

    def test_a_request_is_written_and_retired_by_the_build_that_serves_it(
            self) -> None:
        with self._data_dir() as root:
            self.assertTrue(indexd_runtime.request_search_index_build())
            request = root / ".search_index_request"
            self.assertTrue(request.exists())
            self.assertTrue(indexd_runtime.search_index_build_pending())
            with mock.patch.object(indexd_runtime, "derived_writes_permitted",
                                   return_value=True), \
                    mock.patch.object(indexd_runtime, "refresh_search_index",
                                      return_value=True) as built:
                self.assertTrue(indexd_runtime.serve_search_index_request())
            built.assert_called_once_with()
            self.assertFalse(request.exists())

    def test_a_failed_build_keeps_the_request_for_the_next_daemon(self) -> None:
        with self._data_dir() as root:
            self.assertTrue(indexd_runtime.request_search_index_build())
            with mock.patch.object(indexd_runtime, "derived_writes_permitted",
                                   return_value=True), \
                    mock.patch.object(indexd_runtime, "refresh_search_index",
                                      return_value=False):
                self.assertFalse(indexd_runtime.serve_search_index_request())
            self.assertTrue((root / ".search_index_request").exists())

    def test_a_newer_request_survives_the_build_that_served_its_predecessor(
            self) -> None:
        with self._data_dir() as root:
            self.assertTrue(indexd_runtime.request_search_index_build())
            request = root / ".search_index_request"
            before = request.stat().st_mtime_ns

            def refresh():
                request.write_text('{"requested":2}\n', encoding="utf-8")
                os.utime(request, ns=(before + 1, before + 1))
                return True

            with mock.patch.object(
                    indexd_runtime, "derived_writes_permitted",
                    return_value=True):
                self.assertTrue(indexd_runtime.serve_search_index_request(refresh))
            self.assertTrue(request.exists())

    def test_no_request_means_no_work(self) -> None:
        with self._data_dir():
            with mock.patch.object(indexd_runtime, "refresh_search_index") as built:
                self.assertFalse(indexd_runtime.serve_search_index_request())
            built.assert_not_called()

    def test_an_oversize_request_is_not_pending_and_can_be_replaced(self) -> None:
        with self._data_dir() as root:
            request = root / ".search_index_request"
            request.write_bytes(
                b"x" * (indexd_runtime._SEARCH_INDEX_REQUEST_MAX_BYTES + 1))
            self.assertFalse(indexd_runtime.search_index_build_pending())
            with mock.patch.object(indexd_runtime, "refresh_search_index") as built:
                self.assertFalse(indexd_runtime.serve_search_index_request())
            built.assert_not_called()
            self.assertTrue(indexd_runtime.request_search_index_build())
            self.assertTrue(indexd_runtime.search_index_build_pending())

    def test_the_daemon_loop_serves_queued_requests(self) -> None:
        import indexer
        owner = indexer.AutoIndexer.__new__(indexer.AutoIndexer)
        owner.state = {"phase": "idle"}
        owner._search_refresh_active = threading.Event()
        with mock.patch.object(
                owner, "_refresh_search_index") as refresh, \
                mock.patch.object(
                    indexd_runtime, "serve_search_index_request") as served:
            owner._serve_index_request()
        served.assert_called_once_with(refresh=refresh)
        self.assertFalse(owner._search_refresh_active.is_set())
        owner.state = {"phase": "indexing"}
        with mock.patch.object(indexd_runtime, "serve_search_index_request") as busy:
            owner._serve_index_request()
        busy.assert_not_called()


class ColdBuildBulkLoad(unittest.TestCase):
    """The cold build's temp file is unlinked on every exit, so it trades the
    rollback journal for the page cache FTS5's merge actually needs."""

    def test_bulk_load_pragmas_are_write_only_and_cached(self) -> None:
        import corpusdb
        pragmas = corpusdb._BULK_LOAD_PRAGMAS.lower()
        self.assertIn("journal_mode=off", pragmas)
        self.assertIn("synchronous=off", pragmas)
        self.assertIn("temp_store=memory", pragmas)
        self.assertRegex(pragmas, r"cache_size=-\d{5,}")

    def test_the_cold_build_never_writes_the_published_path_directly(self) -> None:
        import corpusdb
        self.assertTrue(str(corpusdb._tmp_db_path()).endswith(".building"))
        self.assertNotEqual(corpusdb._tmp_db_path(), corpusdb.DB_PATH)


class ContentDigestNarrowState(unittest.TestCase):
    """The digest travels in saved notes: the faster loop must not move it."""

    def test_narrow_state_reproduces_the_wide_fnv_low_word(self) -> None:
        import compact

        def wide(text: str) -> str:
            digest = 0xCBF29CE484222325
            for byte in (text or "").encode("utf-8"):
                digest = ((digest ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
            return f"{digest & 0xFFFF:04x}"

        for sample in ("", "a", "the quick brown fox", "héllo wörld ünicode ✓",
                       "x" * 4096, "\n\t mixed \x00 bytes ", "İıſK"):
            self.assertEqual(compact.content_digest(sample), wide(sample))


class ScanTooLaneDigestIsLazy(unittest.TestCase):
    """A tool row's digest derives from its own text, so only surfaced rows pay."""

    def test_tool_rows_carry_no_eager_digest(self) -> None:
        import explore
        source = Path(explore.__file__).read_text(encoding="utf-8")
        tool_lane = source.split('"model_source": "tool"')[1][:600]
        self.assertNotIn("compact.content_digest(text)", tool_lane)

    def test_a_digestless_entry_still_publishes_the_same_digest(self) -> None:
        import compact
        import explore
        text = "pragma journal rebuild posting"
        entry = {"session": "s", "agent": "a", "project": "p", "concept": "",
                 "model": "", "model_source": "tool", "turn": 0, "ts": 0,
                 "who": "tool", "text": text, "low": text.lower()}
        self.assertEqual(explore._entry_content_digest(entry),
                         compact.content_digest(text))
        self.assertEqual(explore.scan_hit(entry, 0, 6)["content_digest"],
                         compact.content_digest(text))


class AbsorbedDriftObservation(unittest.TestCase):
    """Drift a daemon should absorb stays green AND stays observed: display
    silence is licensed, but the zero's currency proof must see the change."""

    def setUp(self) -> None:
        indexd_runtime._clear_freshen_failure()
        self.addCleanup(indexd_runtime._clear_freshen_failure)

    def test_record_drift_keeps_the_absorbed_observation(self) -> None:
        now = time.time()
        quiet_ms = int((now - 9000) * 1000)
        recorded = {"claude": (3, quiet_ms)}
        live = {"claude": (4, int(now * 1000))}
        with mock.patch.dict(os.environ), \
                mock.patch.object(indexd_runtime, "_data_dir_readonly",
                                  return_value=False), \
                mock.patch.object(
                    indexd_runtime, "_refresh_owner_possible",
                    return_value=True):
            os.environ.pop("AGREP_NO_DAEMON", None)
            report = indexd_runtime._record_drift(live, now - 30, recorded, now)
            clean = indexd_runtime._record_drift(
                recorded, now - 30, recorded, now)
        self.assertEqual(report.state, "current")
        self.assertEqual(report.absorbed, 1)
        # negative control: an unchanged census carries no observation
        self.assertEqual((clean.state, clean.absorbed), ("current", 0))

    def test_signal_drift_keeps_the_absorbed_observation(self) -> None:
        now = time.time()
        live = {"claude": (3, int(now * 1000))}
        with mock.patch.dict(os.environ), \
                mock.patch.object(indexd_runtime, "_data_dir_readonly",
                                  return_value=False), \
                mock.patch.object(indexd_runtime, "_vanished_published_stores",
                                  return_value=0), \
                mock.patch.object(
                    indexd_runtime, "_refresh_owner_possible",
                    return_value=True):
            os.environ.pop("AGREP_NO_DAEMON", None)
            report = indexd_runtime._signal_drift(live, now - 20, now)
        self.assertEqual(report.state, "current")
        self.assertEqual(report.absorbed, 1)

    def test_freshness_story_carries_the_absorbed_fact_silently(self) -> None:
        observed = indexd_runtime.DriftReport("current", absorbed=1)
        with mock.patch.object(indexd_runtime, "indexing_failure",
                               return_value=None), \
                mock.patch.object(indexd_runtime, "_drift_report",
                                  return_value=observed):
            story = indexd_runtime.freshness_story()
        self.assertEqual(story.state, "current")
        self.assertTrue(story.absorbed_drift)
        # display stays green (law 3); only the zero's verdict reads the fact
        self.assertEqual(surface.freshness_story_line(story), "")
        verdict = surface.miss_verdict(
            story, meaning_served=True,
            meaning_coverage={"indexed": 5, "total": 5, "complete": True},
            sessions=5)
        self.assertFalse(verdict.confident)
        self.assertNotIn("index current", verdict.tail)


if __name__ == "__main__":
    unittest.main()
