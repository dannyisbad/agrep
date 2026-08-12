"""The six laws, as tests: what agrep repairs silently and what it may say.

The charter these pin: derived data is agrep's own product, so a damaged
derived artifact is a task and not a report (law 1); the only message the
reader earns is one naming something only she can supply (law 2); a repair in
flight is not news (law 3); a check that did not run is not a row (law 4); one
cause renders one line (law 5); escalation waits for the tool's own repair to
have failed, and asks for the smallest thing that unblocks it (law 6).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

DATA = isolate_data_dir()

import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import surface_policy as surface  # noqa: E402

PY_DIR = Path(__file__).resolve().parent
_WRITABLE = indexd_runtime.DerivedMutationInfo("current", None, "")


def _write_ledger(**fields: object) -> None:
    record = {"streak": 0, "last_err": "", "ts": 1.0, "escalated": False,
              "repair": "", "repair_streak": 0}
    record.update(fields)
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (common.DATA_DIR / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
        json.dumps(record), encoding="utf-8")


class HostBlockClassificationTests(unittest.TestCase):
    """Law 2: the volume underneath is the only thing left to ask about."""

    def test_every_spelling_the_writers_actually_print_is_recognized(
            self) -> None:
        # Rust ingest prints the errno form, Python's OSError the strerror
        # form, and the owner's box produced the first of these verbatim.
        for text in ("Error: No space left on device (os error 28)",
                     "OSError: [Errno 28] No space left on device",
                     "failed to write messages.jsonl: Disk quota exceeded",
                     "Disk quota exceeded (os error 69)",
                     "publish failed (os error 122)"):
            with self.subTest(text=text):
                block = surface.host_block(text)
                self.assertIsNotNone(block, text)
                self.assertEqual(block.code, "out-of-space")

    def test_a_read_only_volume_is_its_own_condition(self) -> None:
        block = surface.host_block("Read-only file system (os error 30)")
        self.assertIsNotNone(block)
        self.assertEqual(block.code, "read-only")

    def test_damage_agrep_owns_is_never_classified_as_the_readers(
            self) -> None:
        # Each of these is a derived store agrep rebuilds; naming a host
        # condition for any of them would put the repair back on the reader.
        for text in ("database disk image is malformed",
                     "sessions.jsonl does not match its committed generation",
                     "the corpus generation commit is incomplete",
                     "SegmentError: segmented embedding prefix moved",
                     "", None):
            with self.subTest(text=text):
                self.assertIsNone(surface.host_block(text))

    def test_the_line_names_the_amount_and_never_a_command(self) -> None:
        line = surface.host_block_line(
            surface.host_block("os error 28"),
            surface.rebuild_shortfall_bytes(10 * 1024 ** 2, 400 * 1024 ** 2))
        self.assertIn("agrep needs about", line)
        self.assertIn("MiB", line)
        self.assertNotIn("`", line)
        self.assertNotIn("agrep index", line)

    def test_every_condition_renders_one_grammatical_sentence(self) -> None:
        # host_block_line frames each ask as "agrep needs {ask} to rebuild";
        # an ask phrased as an imperative would read as gibberish there.
        for code, block in surface.HOST_BLOCKS.items():
            with self.subTest(code=code):
                line = surface.host_block_line(block)
                self.assertTrue(line.startswith("agrep needs "))
                self.assertIn(" to rebuild its index;", line)
                self.assertNotIn("`", line)

    def test_a_volume_with_room_asks_for_nothing(self) -> None:
        self.assertEqual(
            surface.rebuild_shortfall_bytes(50 * 1024 ** 3, 100 * 1024 ** 2), 0)
        self.assertEqual(surface.host_block_line(None), "")


class EscalationTests(unittest.TestCase):
    """Law 6: the tool exhausts its own repair before it asks for anything."""

    def tearDown(self) -> None:
        ledger = common.DATA_DIR / indexd_runtime.AUTO_INDEX_HEALTH
        ledger.unlink(missing_ok=True)

    def test_a_healthy_ledger_escalates_nothing(self) -> None:
        _write_ledger()
        self.assertEqual(indexd_runtime.host_block_escalation(), "")

    def test_a_queued_repair_that_has_not_failed_yet_stays_silent(
            self) -> None:
        # Law 3: a rebuild agrep is still working on is not news.
        _write_ledger(repair="torn-generation", repair_streak=0)
        self.assertEqual(indexd_runtime.host_block_escalation(), "")
        _write_ledger(repair="torn-generation",
                      repair_streak=surface.REPAIR_ESCALATE_AFTER - 1)
        self.assertEqual(indexd_runtime.host_block_escalation(), "")

    def test_the_os_naming_the_condition_is_not_a_guess_and_speaks_at_once(
            self) -> None:
        _write_ledger(streak=1,
                      last_err="Error: No space left on device (os error 28)")
        line = indexd_runtime.host_block_escalation()
        self.assertTrue(line)
        self.assertIn("agrep needs", line)
        self.assertNotIn("`", line)

    def test_repair_damage_agrep_owns_never_becomes_the_readers_problem(
            self) -> None:
        # A repair that failed twice on damage with no host cause underneath
        # it has nothing the reader can supply, so it asks for nothing.
        _write_ledger(streak=1, last_err="database disk image is malformed",
                      repair="proof-damaged",
                      repair_streak=surface.REPAIR_ESCALATE_AFTER)
        self.assertEqual(indexd_runtime.host_block_escalation(), "")

    def test_an_unreadable_ledger_is_not_an_escalation(self) -> None:
        common.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (common.DATA_DIR / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
            "{not json", encoding="utf-8")
        self.assertEqual(indexd_runtime.host_block_escalation(), "")


class RepairPlanTests(unittest.TestCase):
    """Law 1: the plan the daemon rebuilds from and the surfaces report from."""

    def test_the_scope_table_names_only_states_a_rebuild_closes(self) -> None:
        for code, scope in corpusdb._REPAIR_SCOPE.items():
            with self.subTest(code=code):
                self.assertIn(scope, ("full", "publish"))
        # A race is a healthy write observed mid-flight, and an unbuilt corpus
        # is not a damaged one; neither may schedule a reparse.
        self.assertNotIn("generation-moving", corpusdb._REPAIR_SCOPE)
        self.assertNotIn("never-built", corpusdb._REPAIR_SCOPE)

    def test_an_unbuilt_corpus_asks_for_no_repair(self) -> None:
        # unbuilt means the whole derived set is absent, not just the database:
        # a proof or marker from another module is a different story entirely
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(common, "DATA_DIR", Path(raw)), \
                mock.patch.object(
                    corpusdb, "DB_PATH", Path(raw) / "corpus.db"):
            plan = corpusdb.derived_repair_plan()
        self.assertEqual(plan, corpusdb.NO_REPAIR)
        self.assertFalse(plan.code)

    def test_the_ownership_lockout_is_not_in_the_repair_table(self) -> None:
        # The one legitimate exception in the census: corpus.db that agrep did
        # not write may hold messages whose sources are gone, so a rebuild
        # would destroy the only copy. It is never auto-repaired.
        self.assertNotIn("post-adoption-clobber", corpusdb._REPAIR_SCOPE)


class LedgerTests(unittest.TestCase):
    """The emitter and the checker share one owned artifact (house rule)."""

    def tearDown(self) -> None:
        ledger = common.DATA_DIR / indexd_runtime.AUTO_INDEX_HEALTH
        ledger.unlink(missing_ok=True)

    def test_a_record_without_the_repair_fields_still_reads(self) -> None:
        # Records written before the repair loop existed must not read as
        # damage, or an upgrade would look like a broken index.
        common.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (common.DATA_DIR / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
            json.dumps({"streak": 0, "last_err": "", "ts": 1.0,
                        "escalated": False}), encoding="utf-8")
        health = indexd_runtime._auto_index_health()
        self.assertEqual(health.state, "available")
        self.assertEqual(health.repair, "")
        self.assertEqual(health.repair_streak, 0)

    def test_invalid_repair_fields_are_rejected_like_every_other_field(
            self) -> None:
        for repair, streak in (("x" * 65, 0), (None, 0), ("torn", -1),
                               ("torn", "two")):
            with self.subTest(repair=repair, streak=streak):
                common.DATA_DIR.mkdir(parents=True, exist_ok=True)
                (common.DATA_DIR
                 / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
                    json.dumps({"streak": 0, "last_err": "", "ts": 1.0,
                                "escalated": False, "repair": repair,
                                "repair_streak": streak}), encoding="utf-8")
                self.assertEqual(
                    indexd_runtime._auto_index_health().state, "unavailable")


class _StubWatcher:
    _last_event_wall = 0.0

    def wait_boot(self, timeout: float) -> bool:
        return True


def _loop() -> "indexer.AutoIndexer":
    """An AutoIndexer with its thread machinery bypassed: the scheduling
    decisions under test are pure, and starting a real daemon here would
    index the developer's own stores."""
    loop = indexer.AutoIndexer.__new__(indexer.AutoIndexer)
    loop._w = _StubWatcher()
    loop.state = {"phase": "idle", "last_run": 0.0, "last_err": "", "runs": 0}
    loop._retry_needed = False
    loop._fail_streak = 0
    loop._repair = corpusdb.NO_REPAIR
    loop._repair_seen = ""
    loop._repair_attempted = False
    loop._repair_streak = 0
    loop._last_repair_run = 0.0
    loop._last_index_wall = 0.0
    loop._last_health_check = 0.0
    return loop


class RepairTriggerTests(unittest.TestCase):
    """Finding 1: the corpus lane had no health-driven trigger at all."""

    def test_an_idle_lane_with_no_damage_does_not_run(self) -> None:
        loop = _loop()
        self.assertFalse(loop._should_run())

    def test_damage_alone_schedules_a_rebuild_without_any_new_activity(
            self) -> None:
        # The whole defect: the lane converged only on new chat activity, so a
        # box that went idle while damaged stayed damaged forever.
        loop = _loop()
        loop._repair = corpusdb.RepairPlan("torn-generation", "full", "x")
        self.assertEqual(loop._w._last_event_wall, 0.0)
        self.assertTrue(loop._should_run())

    def test_two_repairs_cannot_be_scheduled_back_to_back(self) -> None:
        loop = _loop()
        loop._repair = corpusdb.RepairPlan("torn-generation", "full", "x")
        loop._last_repair_run = time.time()
        self.assertFalse(loop._should_run())

    def test_a_verdict_seen_once_is_a_race_and_never_schedules_work(
            self) -> None:
        # A publication landing under the probe looks exactly like damage on
        # one look; only damage survives a second.
        loop = _loop()
        plan = corpusdb.RepairPlan("generation-unavailable", "full", "x")
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=_WRITABLE), \
                mock.patch.object(corpusdb, "derived_repair_plan",
                                  return_value=plan), \
                mock.patch.object(indexd_runtime, "record_derived_repair"):
            loop._check_derived_health(1000.0)
            self.assertEqual(loop._repair, corpusdb.NO_REPAIR)
            loop._last_health_check = 0.0
            loop._check_derived_health(2000.0)
            self.assertEqual(loop._repair.code, "generation-unavailable")

    def test_a_rebuild_that_leaves_the_damage_standing_counts_once(
            self) -> None:
        loop = _loop()
        plan = corpusdb.RepairPlan("torn-generation", "full", "x")
        loop._repair_seen = "torn-generation"
        loop._repair_attempted = True
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=_WRITABLE), \
                mock.patch.object(corpusdb, "derived_repair_plan",
                                  return_value=plan), \
                mock.patch.object(indexd_runtime, "record_derived_repair"):
            loop._check_derived_health(1000.0)
        self.assertEqual(loop._repair_streak, 1)

    def test_a_rebuild_that_worked_clears_the_streak_and_the_plan(
            self) -> None:
        loop = _loop()
        loop._repair_seen = "torn-generation"
        loop._repair_attempted = True
        loop._repair_streak = 1
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=_WRITABLE), \
                mock.patch.object(corpusdb, "derived_repair_plan",
                                  return_value=corpusdb.NO_REPAIR), \
                mock.patch.object(indexd_runtime, "record_derived_repair"):
            loop._check_derived_health(1000.0)
        self.assertEqual(loop._repair_streak, 0)
        self.assertEqual(loop._repair, corpusdb.NO_REPAIR)

    def test_an_ownership_lockout_withdraws_the_repair_entirely(self) -> None:
        # The census's one legitimate exception: those bytes are not ours to
        # replace, so the loop must not keep trying to.
        loop = _loop()
        loop._repair = corpusdb.RepairPlan("torn-generation", "full", "x")
        locked = indexd_runtime.DerivedMutationInfo("foreign", "other", "held")
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=locked), \
                mock.patch.object(indexd_runtime, "derived_writes_permitted",
                                  return_value=False):
            loop._check_derived_health(1000.0)
        self.assertEqual(loop._repair, corpusdb.NO_REPAIR)


# Shapes that put agrep's own work back on the reader. Each is the literal
# text the product owner was looking at, or its close kin.
_DENIED = (
    re.compile(r"then report it"),
    re.compile(r"try `[^`]*index"),
    re.compile(r"repair the source-health record"),
    re.compile(r"not inspected in the routine tier"),
)

# The repair loop's own modules, which carry no remedy text at all. The
# registry in surface_policy.py and the render paths in doctor.py join this
# scan with the remedy shrink; they still hold the sentences being deleted.
_OWNED = ("indexer.py", "corpusdb.py", "indexd_runtime.py")


class DeadOwnerReapTests(unittest.TestCase):
    """Ownership and liveness are different facts: a killed daemon's leftover
    claim is reaped by this build's next publication, while a claim that is
    live - or that we cannot prove dead - is never touched."""

    def _claim(self, state: "indexd_runtime._IndexdOwnerState") -> bool:
        inspection = indexd_runtime._IndexdOwnerInspection(
            state, None, None, None)
        with mock.patch.object(indexd_runtime, "_inspect_indexd_owner",
                               return_value=inspection):
            return indexd_runtime.live_indexer_claim()

    def test_dead_reused_and_absent_claims_are_not_live(self) -> None:
        for state in (indexd_runtime._IndexdOwnerState.ABSENT,
                      indexd_runtime._IndexdOwnerState.DEAD,
                      indexd_runtime._IndexdOwnerState.REUSED,
                      indexd_runtime._IndexdOwnerState.MALFORMED_STALE):
            with self.subTest(state=state):
                self.assertFalse(self._claim(state))

    def test_our_own_compatible_daemon_is_nobodys_blocker(self) -> None:
        self.assertFalse(self._claim(
            indexd_runtime._IndexdOwnerState.COMPATIBLE))

    def test_live_and_unprovable_claims_stay_untouchable(self) -> None:
        # A claim we cannot prove dead is not ours to reap.
        for state in (indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
                      indexd_runtime._IndexdOwnerState.HOSTILE,
                      indexd_runtime._IndexdOwnerState.UNVERIFIABLE,
                      indexd_runtime._IndexdOwnerState.MALFORMED_FRESH,
                      indexd_runtime._IndexdOwnerState.ORPHANED_GROUP):
            with self.subTest(state=state):
                self.assertTrue(self._claim(state))

    def test_a_dead_foreign_anchor_permits_the_takeover(self) -> None:
        foreign = indexd_runtime.DerivedMutationInfo("foreign", "old", "held")
        with mock.patch.object(indexd_runtime,
                               "derived_writer_mutation_info",
                               return_value=foreign), \
                mock.patch.object(indexd_runtime, "live_indexer_claim",
                                  return_value=False):
            self.assertTrue(indexd_runtime.derived_writes_permitted())

    def test_a_live_foreign_owner_still_holds_the_fence(self) -> None:
        foreign = indexd_runtime.DerivedMutationInfo("foreign", "old", "held")
        with mock.patch.object(indexd_runtime,
                               "derived_writer_mutation_info",
                               return_value=foreign), \
                mock.patch.object(indexd_runtime, "live_indexer_claim",
                                  return_value=True):
            self.assertFalse(indexd_runtime.derived_writes_permitted())

    def test_non_foreign_lockouts_never_ride_the_reap_path(self) -> None:
        # a verdict no writer may act on stays closed at any liveness; the
        # door opens only for states derived_writer_launchable accepts
        other = indexd_runtime.DerivedMutationInfo("unavailable", None, "x")
        with mock.patch.object(indexd_runtime,
                               "derived_writer_mutation_info",
                               return_value=other), \
                mock.patch.object(indexd_runtime, "live_indexer_claim",
                                  return_value=False):
            self.assertFalse(indexd_runtime.derived_writes_permitted())


class JournaledOwnerLaunchabilityTests(unittest.TestCase):
    """One launchability predicate, consulted by both writer entrypoints.

    A rollback journal is recoverable-by-us, not owned-by-someone-else, so the
    daemon lane may never refuse a state the foreground Rust writer launches
    into. Only the liveness half keeps an owner fenced, and a verdict naming a
    state no writer may act on stays closed at every liveness."""

    def _journaled(self) -> "indexd_runtime.DerivedMutationInfo":
        return indexd_runtime.DerivedMutationInfo(
            "unavailable", None,
            "corpus.db is mid-transaction behind a rollback journal",
            journal_blocked=True)

    def _permitted(self, info, *, live: bool) -> bool:
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=info), \
                mock.patch.object(indexd_runtime, "live_indexer_claim",
                                  return_value=live):
            return indexd_runtime.derived_writes_permitted()

    def test_the_two_writer_entrypoints_share_one_launch_verdict(self) -> None:
        for info in (_WRITABLE, self._journaled(),
                     indexd_runtime.DerivedMutationInfo(
                         "foreign", "old", "held")):
            with self.subTest(state=info.state):
                self.assertTrue(indexd_runtime.derived_writer_launchable(info))
                self.assertTrue(self._permitted(info, live=False))

    def test_a_cold_journal_no_longer_wedges_the_daemon(self) -> None:
        self.assertTrue(self._permitted(self._journaled(), live=False))

    def test_a_live_claim_over_a_journal_is_still_never_stolen(self) -> None:
        self.assertFalse(self._permitted(self._journaled(), live=True))

    def test_an_unreadable_anchor_is_launchable_by_nobody(self) -> None:
        unreadable = indexd_runtime.DerivedMutationInfo(
            "unavailable", None,
            "derived-store ownership record .derived-owner.json is unreadable")
        self.assertFalse(indexd_runtime.derived_writer_launchable(unreadable))
        self.assertFalse(self._permitted(unreadable, live=False))

    def _kick(self, info) -> "indexd_runtime.RepairKick":
        with mock.patch.object(
                indexd_runtime, "_data_dir_readonly", return_value=False), \
                mock.patch.dict(
                    os.environ, {"AGREP_NO_DAEMON": ""}, clear=False), \
                mock.patch.object(
                    indexd_runtime.common, "ingest_bin",
                    return_value=Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=info), \
                mock.patch.object(
                    indexd_runtime, "live_indexer_claim", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.READY
                ) as spawn:
            kick = indexd_runtime.kick_background_repair()
        self.spawned = spawn.called
        return kick

    def test_the_daemon_kick_repairs_a_cold_journal_instead_of_declining(
            self) -> None:
        kick = self._kick(self._journaled())
        self.assertTrue(self.spawned)
        self.assertEqual(kick, indexd_runtime.RepairKick(True, ""))

    def test_the_kick_still_declines_a_verdict_no_writer_may_act_on(
            self) -> None:
        kick = self._kick(indexd_runtime.DerivedMutationInfo(
            "unavailable", None, "corpus.db has no writing-build identity"))
        self.assertFalse(self.spawned)
        self.assertEqual(kick.cause, "owner-unverifiable")
        self.assertEqual(
            surface.repair_decline_line(kick.cause),
            "another agrep's claim on the index cannot be verified")


def _dead_pid() -> int | None:
    """A pid the kernel has certainly reclaimed, or None if it was reused."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = process.pid
    process.wait()
    for _ in range(50):
        if not common.pid_alive(pid):
            return pid
        time.sleep(0.02)
    return None


class DeadLauncherReapTests(unittest.TestCase):
    """The launch claim gets the same liveness discipline as the daemon owner.

    A launcher that exited leaves `.spawn` behind; nothing else ever removes
    it, so before this every later pass read a dead process as "another agrep
    holds the indexer here" and indexing never resumed."""

    def setUp(self) -> None:
        self.guard = indexd_runtime._SPAWN_GUARD_PATH
        self.guard.parent.mkdir(parents=True, exist_ok=True)
        self.guard.unlink(missing_ok=True)
        self.addCleanup(self.guard.unlink, True)
        self.token = "0" * 32
        self.child = indexd_runtime._spawn_child_path(self.token)
        self.addCleanup(self.child.unlink, True)

    def _write_guard(self, pid: int, start: str) -> None:
        self.guard.write_bytes(
            f"state=launching pid={pid} start={start} "
            f"token={self.token}\n".encode("ascii"))

    def _status(self, **kwargs: object) -> dict | None:
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=_WRITABLE):
            return indexd_runtime._spawn_guard_resource_status(**kwargs)

    def test_a_dead_launchers_claim_is_reaped_by_one_ordinary_pass(
            self) -> None:
        pid = _dead_pid()
        if pid is None:
            self.skipTest("the probe pid was reused before it could be read")
        self._write_guard(pid, "darwin_1_1")
        self.assertIsNone(self._status())
        self.assertFalse(self.guard.exists())

    def test_the_dead_launchers_child_handoff_goes_with_it(self) -> None:
        pid = _dead_pid()
        if pid is None:
            self.skipTest("the probe pid was reused before it could be read")
        self._write_guard(pid, "darwin_1_1")
        self.child.write_bytes(
            f"state=spawned owner={self.token} pid={pid} "
            f"start=darwin_1_1\n".encode("ascii"))
        self.assertIsNone(self._status())
        self.assertFalse(self.child.exists())
        self.assertFalse(self.guard.exists())

    def test_a_diagnostic_pass_neither_writes_nor_calls_it_occupied(
            self) -> None:
        # Being forbidden to reap a record is not evidence that it fences.
        pid = _dead_pid()
        if pid is None:
            self.skipTest("the probe pid was reused before it could be read")
        self._write_guard(pid, "darwin_1_1")
        self.assertIsNone(self._status(settle_child=False))
        self.assertTrue(self.guard.exists())

    def test_a_diagnostic_pass_reads_a_dead_child_handoff_as_dead_too(
            self) -> None:
        # The owner's box carried both records; a child left by the same dead
        # launcher fences exactly as little as the launcher does.
        pid = _dead_pid()
        if pid is None:
            self.skipTest("the probe pid was reused before it could be read")
        self._write_guard(pid, "darwin_1_1")
        self.child.write_bytes(
            f"state=spawned owner={self.token} pid={pid} "
            f"start=darwin_1_1\n".encode("ascii"))
        self.assertIsNone(self._status(settle_child=False))
        self.assertTrue(self.child.exists())

    def test_a_live_launcher_still_fences_and_is_never_reaped(self) -> None:
        start = common.process_start_identity(os.getpid())
        if start is None:
            self.skipTest("this platform cannot prove process start identity")
        self._write_guard(os.getpid(), start)
        self.assertEqual(self._status().get("starting"), True)
        self.assertTrue(self.guard.exists())

    def test_an_incomplete_record_keeps_its_publication_grace(self) -> None:
        # A launcher mid-write owns the window it is writing in.
        self.guard.write_bytes(b"state=launch")
        self.assertEqual(self._status().get("starting"), True)
        self.assertTrue(self.guard.exists())


class DeadGenerationRecordTests(unittest.TestCase):
    """A6: one readiness and one handoff record per daemon launch accumulated
    forever. Each is addressed through the live owner's token, so a generation
    whose processes are gone leaves files no reader can reach."""

    def setUp(self) -> None:
        # the reclaim count is over the whole directory, so it counts this
        # test's records only when nothing else's are lying beside them
        temp = tempfile.TemporaryDirectory(prefix="agrep-dead-generation-")
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(common, "DATA_DIR", Path(temp.name))
        patch.start()
        self.addCleanup(patch.stop)
        common.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.made: list[Path] = []
        self.addCleanup(
            lambda: [path.unlink(missing_ok=True) for path in self.made])

    def _record(self, name: str, body: bytes) -> Path:
        path = common.DATA_DIR / name
        path.write_bytes(body)
        self.made.append(path)
        return path

    def _reclaim(self) -> int:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        with mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                               return_value=_WRITABLE), \
                mock.patch.object(indexd_runtime, "_inspect_indexd_owner",
                                  return_value=absent):
            return indexd_runtime.reclaim_dead_generation_records()

    def test_records_of_a_generation_that_ended_are_reclaimed(self) -> None:
        pid = _dead_pid()
        if pid is None:
            self.skipTest("the probe pid was reused before it could be read")
        token = "b" * 32
        version = indexd_runtime.INDEXD_PROTOCOL
        ready = self._record(
            f".indexd.v{version}.ready.{token}",
            f"pid={pid} start=darwin_1_1 token={token}\n".encode("ascii"))
        child = self._record(
            f".indexd.v{version}.child.{token}",
            f"owner={token} guard={pid} target={pid}\n".encode("ascii"))
        self.assertEqual(self._reclaim(), 2)
        self.assertFalse(ready.exists())
        self.assertFalse(child.exists())

    def test_a_record_a_live_process_answers_for_is_kept(self) -> None:
        token = "c" * 32
        ready = self._record(
            f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.ready.{token}",
            f"pid={os.getpid()} start=x token={token}\n".encode("ascii"))
        self.assertEqual(self._reclaim(), 0)
        self.assertTrue(ready.exists())

    def test_a_pidless_snapshot_is_kept_until_no_reader_would_accept_it(
            self) -> None:
        token = "d" * 32
        live = self._record(
            f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.live.{token}",
            b'{"_agrep_live_ipc":{"generation":"x"}}')
        self.assertEqual(self._reclaim(), 0)
        self.assertTrue(live.exists())
        os.utime(live, (2_000, 2_000))
        self.assertEqual(self._reclaim(), 1)
        self.assertFalse(live.exists())

    def test_unrelated_data_dir_files_are_never_candidates(self) -> None:
        keep = [self._record("messages.jsonl", b"{}\n"),
                self._record(".ingest.sig", b"x"),
                self._record(
                    f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.lock",
                    b"pid=1 start=x\n")]
        self.assertEqual(self._reclaim(), 0)
        self.assertTrue(all(path.exists() for path in keep))


ROOT = PY_DIR.parent
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")


class ColdRollbackJournalTests(unittest.TestCase):
    """The upgrade-day wedge: foreign stores plus a rollback journal nobody reaps.

    Declining a database mid-transaction is right, but a journal whose writer is
    provably gone is recoverable, and leaving it left the box on a full scan
    forever (law 1). A journal a live writer still holds is not ours, and the
    decline is one line the reader can act on - never a success line over work
    that did not happen (law 2)."""

    def _sandbox(self, root: Path) -> tuple[dict, dict, Path]:
        """Two builds of one binary over one data dir: the upgrade the box saw."""
        self.assertTrue(RELEASE_BIN.is_file(), f"release binary missing: {RELEASE_BIN}")
        home, data = root / "home", root / "data"
        source = (home / ".cline" / "data" / "tasks" / "1"
                  / "api_conversation_history.json")
        source.parent.mkdir(parents=True)
        source.write_text(
            '[{"role":"user","content":"cold journal wedge phrase","ts":1}]',
            encoding="utf-8")
        data.mkdir(parents=True)
        binaries = []
        for name, trailer in (("a", b"\nA"), ("b", b"\nB")):
            binary = root / f"{name}-{RELEASE_BIN.name}"
            binary.write_bytes(RELEASE_BIN.read_bytes() + trailer)
            binary.chmod(0o755)
            binaries.append(binary)
        env = dict(os.environ)
        for key in ("AGREP_RS_BIN", "AGREP_DATA_READONLY", "AGREP_RUNTIME_BUILD_ID",
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN",
                    indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV):
            env.pop(key, None)
        env.update({
            "AGREP_DATA_DIR": os.fspath(data), "AGREP_DATA_DIR_SOURCE": "env",
            "AGREP_HOME": os.fspath(home), "HOME": os.fspath(home),
            "USERPROFILE": os.fspath(home),
            "APPDATA": os.fspath(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": os.fspath(home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": os.fspath(home / ".config"),
            "XDG_DATA_HOME": os.fspath(home / ".local" / "share"),
            "CLINE_DIR": os.fspath(home / ".cline"),
            "AGREP_NO_DAEMON": "1", "AGREP_NO_FETCH": "1",
            "PYTHONPATH": os.fspath(ROOT),
        })
        first, second = (dict(env, AGREP_RS_BIN=os.fspath(binary))
                         for binary in binaries)
        return first, second, data

    def _agrep(self, env: dict, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, os.fspath(ROOT / "cli.py"), *argv],
            cwd=os.fspath(ROOT), env=env, capture_output=True, text=True,
            timeout=180)

    @staticmethod
    def _owner(data: Path) -> str:
        return json.loads(
            (data / ".derived-owner.json").read_text(encoding="utf-8"))["build_id"]

    def _wedge(self, data: Path, live: bool) -> subprocess.Popen | None:
        """Leave the shape the upgrade left: a rollback journal over the stores."""
        db = data / "corpus.db"
        held = (
            'db.execute("BEGIN IMMEDIATE")\n'
            'db.execute("CREATE TABLE IF NOT EXISTS wedge(x)")\n')
        program = (
            f"import sqlite3, time\n"
            f"db = sqlite3.connect({os.fspath(db)!r}, isolation_level=None)\n"
            f'db.execute("PRAGMA journal_mode=delete")\n'
            f"{held}"
            f'print("held", flush=True)\n'
            f"{'time.sleep(600)' if live else 'import os; os._exit(9)'}\n")
        writer = subprocess.Popen(
            [sys.executable, "-c", program], stdout=subprocess.PIPE, text=True,
            start_new_session=(os.name != "nt"))
        if live:
            writer.stdout.readline()
        else:
            writer.wait(timeout=60)
            writer.stdout.close()
            writer = None
        journal = Path(f"{db}-journal")
        self.assertTrue(journal.exists() and journal.stat().st_size,
                        "the wedge needs a rollback journal on disk")
        return writer

    def test_one_ordinary_index_reclaims_a_cold_journal_and_takes_over(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-cold-journal-") as raw:
            root = Path(raw)
            first, second, data = self._sandbox(root)
            self.assertEqual(self._agrep(first, "index").returncode, 0)
            seeded = self._owner(data)
            self._wedge(data, live=False)

            # ONE ordinary index: no --full, no flags
            index = self._agrep(second, "index")
            self.assertEqual(index.returncode, 0, index.stderr)
            self.assertFalse(Path(f"{data / 'corpus.db'}-journal").exists())
            self.assertNotEqual(self._owner(data), seeded)

            search = self._agrep(second, "search", "cold journal wedge phrase")
            self.assertIn("cold journal wedge phrase", search.stdout)
            # the 100x-slow tell: a search that cannot use the search db
            self.assertNotIn("scanning the published snapshot directly",
                             search.stderr)

    def test_a_cold_journal_on_this_build_s_own_stores_also_recovers(self) -> None:
        # No takeover here: the same journal on the stores this build already owns
        # blocked the ownership probe, and the refusal returned before any lock.
        with tempfile.TemporaryDirectory(prefix="agrep-own-journal-") as raw:
            root = Path(raw)
            first, _second, data = self._sandbox(root)
            self.assertEqual(self._agrep(first, "index").returncode, 0)
            owner = self._owner(data)
            self._wedge(data, live=False)

            index = self._agrep(first, "index")
            self.assertEqual(index.returncode, 0, index.stderr)
            self.assertFalse(Path(f"{data / 'corpus.db'}-journal").exists())
            self.assertEqual(self._owner(data), owner)

            search = self._agrep(first, "search", "cold journal wedge phrase")
            self.assertIn("cold journal wedge phrase", search.stdout)
            self.assertNotIn("scanning the published snapshot directly",
                             search.stderr)

    def test_a_live_journal_declines_in_one_line_on_both_index_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-live-journal-") as raw:
            root = Path(raw)
            first, second, data = self._sandbox(root)
            self.assertEqual(self._agrep(first, "index").returncode, 0)
            seeded = self._owner(data)
            writer = self._wedge(data, live=True)
            assert writer is not None
            try:
                for label, env in (("upgraded build", second), ("own build", first)):
                    for argv in (("index",), ("index", "--full")):
                        with self.subTest(build=label, argv=argv):
                            self._assert_declined(
                                self._agrep(env, *argv), data, seeded)
            finally:
                if writer.poll() is None:
                    writer.kill()
                writer.wait(timeout=60)
                if writer.stdout is not None:
                    writer.stdout.close()

    def _stop_daemons(self, env: dict) -> None:
        subprocess.run(
            [sys.executable, "-c",
             "import indexd_runtime; indexd_runtime.stop_indexers_for_removal()"],
            cwd=os.fspath(PY_DIR), env=env, capture_output=True, text=True,
            timeout=120)

    def test_the_daemon_lane_self_heals_a_cold_journal_without_a_human(
            self) -> None:
        """Law 1 in the lane the reader actually uses: the crash repairs itself.

        Every other case here runs with the daemon disabled, so the wedge that
        put the box on the 100x-slow scan forever was untested by
        construction. One ordinary search, no flags, no explicit index."""
        with tempfile.TemporaryDirectory(prefix="agrep-daemon-journal-") as raw:
            root = Path(raw)
            first, _second, data = self._sandbox(root)
            live = dict(first)
            live.pop("AGREP_NO_DAEMON", None)
            try:
                self.assertEqual(self._agrep(first, "index").returncode, 0)
                self._wedge(data, live=False)
                journal = Path(f"{data / 'corpus.db'}-journal")

                search = self._agrep(
                    live, "search", "cold journal wedge phrase")
                self.assertIn("cold journal wedge phrase", search.stdout)
                deadline = time.monotonic() + 30.0
                while journal.exists() and time.monotonic() < deadline:
                    time.sleep(0.25)
                self.assertFalse(
                    journal.exists(),
                    "the daemon lane left the rollback journal wedged")
                healed = self._agrep(
                    live, "search", "cold journal wedge phrase")
                self.assertNotIn("history may be stale", healed.stderr)
                self.assertNotIn(
                    "scanning sources this query", healed.stderr)
            finally:
                self._stop_daemons(dict(first))

    def _assert_declined(self, declined: subprocess.CompletedProcess,
                         data: Path, seeded: str) -> None:
        self.assertNotEqual(declined.returncode, 0, declined.stderr)
        self.assertEqual(self._owner(data), seeded)
        self.assertTrue(Path(f"{data / 'corpus.db'}-journal").exists(),
                        "a live writer's journal is not ours to reap")
        wedge = [line for line in declined.stderr.splitlines()
                 if "live rollback journal" in line]
        self.assertEqual(len(wedge), 1, declined.stderr)


class DenyListTests(unittest.TestCase):
    """Law 1 read as prose: no owned module tells the reader to repair us."""

    def test_no_owned_module_carries_a_repair_instruction_shape(self) -> None:
        for name in _OWNED:
            text = (PY_DIR / name).read_text(encoding="utf-8")
            for pattern in _DENIED:
                with self.subTest(module=name, pattern=pattern.pattern):
                    self.assertIsNone(
                        pattern.search(text),
                        f"{name} carries a denied remedy shape: "
                        f"{pattern.pattern}")


class OwnershipFaultsHealSilently(unittest.TestCase):
    """The state the fleet left on the owner's box, as a red test: a dead
    foreign owner's derived stores are a repair task, not a message. The
    freshness story converges - renders nothing - exactly when the kick
    verifiably started repair (laws 1, 3, 4), and still hedges when the
    claim is held by an owner the kick may not reap (law 5)."""

    def _story(self, code: str, kick: indexd_runtime.RepairKick):
        failure = surface.FreshnessFailure(code, "derived stores owned-by old")
        with mock.patch.object(indexd_runtime, "indexing_failure",
                               return_value=failure), \
                mock.patch.object(indexd_runtime, "kick_background_repair",
                                  return_value=kick) as kicked:
            story = indexd_runtime.freshness_story()
        return story, kicked

    def test_a_failed_owner_probe_declines_without_escaping(self):
        with mock.patch.object(
                indexd_runtime, "_data_dir_readonly", return_value=False), \
                mock.patch.dict(
                    os.environ, {"AGREP_NO_DAEMON": ""}, clear=False), \
                mock.patch.object(
                    indexd_runtime.common, "ingest_bin",
                    return_value=Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive",
                    side_effect=PermissionError("fixture lock cannot be reaped")):
            kick = indexd_runtime.kick_background_repair()
        self.assertEqual(
            kick, indexd_runtime.RepairKick(False, "probe-failed"))
        self.assertEqual(
            surface.repair_decline_line(kick.cause),
            "the repair state could not be verified")

    def test_exact_incompatible_owner_delegates_to_successor_handoff(self):
        indexd_runtime._clear_freshen_failure()
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "old-build", "foreign owner")
        incompatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
            None, 4242, "birth")
        with mock.patch.object(
                indexd_runtime, "_data_dir_readonly", return_value=False), \
                mock.patch.dict(
                    os.environ, {"AGREP_NO_DAEMON": ""}, clear=False), \
                mock.patch.object(
                    indexd_runtime.common, "ingest_bin",
                    return_value=Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "derived_writes_permitted",
                    return_value=False), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=foreign), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=incompatible), \
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT
                ) as spawn:
            kick = indexd_runtime.kick_background_repair()
        self.assertEqual(kick, indexd_runtime.RepairKick(True, ""))
        spawn.assert_called_once_with()

    def test_failed_successor_handoff_preserves_held_owner_cause(self):
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "old-build", "foreign owner")
        incompatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
            None, 4242, "birth")
        with mock.patch.object(
                indexd_runtime, "_data_dir_readonly", return_value=False), \
                mock.patch.dict(
                    os.environ, {"AGREP_NO_DAEMON": ""}, clear=False), \
                mock.patch.object(
                    indexd_runtime.common, "ingest_bin",
                    return_value=Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "derived_writes_permitted",
                    return_value=False), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=foreign), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=incompatible), \
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.BLOCKED):
            kick = indexd_runtime.kick_background_repair()
        self.assertEqual(
            kick, indexd_runtime.RepairKick(False, "held-foreign-owner"))

    def test_dead_foreign_owner_goes_silent_once_repair_is_in_flight(self):
        story, kicked = self._story(
            indexd_runtime.DERIVED_STORE_OWNER_CODE,
            indexd_runtime.RepairKick(True, ""))
        kicked.assert_called_once()
        self.assertTrue(story.converging)
        self.assertEqual(surface.freshness_story_line(story), "")

    def test_blocked_owner_goes_silent_once_repair_is_in_flight(self):
        story, _ = self._story(
            "blocked-owner", indexd_runtime.RepairKick(True, ""))
        self.assertEqual(surface.freshness_story_line(story), "")

    def test_a_held_claim_still_renders_the_hedge(self):
        story, _ = self._story(
            indexd_runtime.DERIVED_STORE_OWNER_CODE,
            indexd_runtime.RepairKick(False, "held-foreign-owner"))
        self.assertFalse(story.converging)
        line = surface.freshness_story_line(story)
        self.assertIn("history may be stale", line)

    def test_non_ownership_faults_never_reach_the_kick(self):
        story, kicked = self._story(
            "census-unavailable", indexd_runtime.RepairKick(True, ""))
        kicked.assert_not_called()
        self.assertFalse(story.converging)


class SemanticBootstrapKickTests(unittest.TestCase):
    """An index publication warms a stale meaning lane instead of leaving it
    for the first semantic query to trip over: every fresh box used to answer
    its opening searches 'meaning unavailable; keyword-only' because nothing
    scheduled embeddings until a query hit the stale lane."""

    def _kick(self, *, cached: bool, coherence: dict):
        import embedder
        import semantic
        with mock.patch.object(embedder, "model_cached",
                               return_value=cached), \
                mock.patch.object(semantic, "embedding_coherence",
                                  return_value=coherence), \
                mock.patch.object(semantic, "ensure_fresh_async",
                                  return_value={"state": "running"}) as kick:
            indexd_runtime.kick_semantic_bootstrap()
        return kick

    def test_a_stale_lane_with_a_cached_model_is_kicked(self) -> None:
        kick = self._kick(cached=True,
                          coherence={"state": "missing-embeddings",
                                     "coherent": False})
        kick.assert_called_once()

    def test_a_missing_model_is_never_fetched_from_index(self) -> None:
        # The kick schedules the warm-up even when the model is absent; the
        # refusal is owned by ensure_fresh_async (download=False returns a
        # model-not-cached state), never by the kick itself fetching it.
        kick = self._kick(cached=False,
                          coherence={"state": "missing-embeddings",
                                     "coherent": False})
        kick.assert_called_once()

    def test_a_coherent_lane_is_left_alone(self) -> None:
        kick = self._kick(cached=True,
                          coherence={"coherent": True,
                                     "migration_pending": False})
        kick.assert_not_called()

    def test_a_broken_probe_never_wounds_the_index(self) -> None:
        import embedder
        with mock.patch.object(embedder, "model_cached",
                               side_effect=OSError("cache probe failed")):
            indexd_runtime.kick_semantic_bootstrap()


if __name__ == "__main__":
    unittest.main(verbosity=2)
