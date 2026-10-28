"""Contention and bounded-exit contracts for the semantic worker client."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import scrub_coordination_namespaces, fixture_tree, isolate_data_dir


isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)
import common  # noqa: E402
import corpusdb  # noqa: E402
import doctor  # noqa: E402
import ownerfile  # noqa: E402
import removal_fence  # noqa: E402
import semworker  # noqa: E402
import surface_policy as surface  # noqa: E402


def _restat(info: os.stat_result, **fields: int) -> os.stat_result:
    """A stat_result with ``fields`` replaced and every extended field kept
    (a plain tuple rebuild drops st_mtime_ns and friends)."""
    values = list(info)
    for name, value in fields.items():
        values[getattr(stat, name)] = value
    extra = {
        name: getattr(info, name)
        for name in ("st_atime", "st_mtime", "st_ctime", "st_atime_ns",
                     "st_mtime_ns", "st_ctime_ns", "st_blocks", "st_blksize",
                     "st_rdev", "st_flags", "st_gen", "st_birthtime")
        if hasattr(info, name)}
    return os.stat_result(tuple(values[:10]) + tuple(values[10:]), extra)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class SemanticWorkerContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="agrep-semworker-contention-")
        self.root = Path(self.temp.name)
        self.saved_data_dir = common.DATA_DIR
        common.DATA_DIR = self.root
        # The worker gate also reads store paths these modules bound at import to the
        # process-wide data dir, which every module in a shared interpreter writes to.
        for module, name, target in (
                (removal_fence, "DATA_DIR", self.root),
                (indexd_runtime, "DERIVED_OWNER_PATH", self.root / ".derived-owner.json"),
                (indexd_runtime, "INGEST_CACHE_PATH", self.root / ".ingest_cache.bin"),
                (corpusdb, "DB_PATH", self.root / "corpus.db")):
            patcher = mock.patch.object(module, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.production_coordination_base_path = (
            semworker._coordination_base_path)
        self.coordination = mock.patch.object(
            semworker, "_coordination_base_path", return_value=self.root)
        self.coordination.start()
        # fixtures write raw records; a writer would have minted the namespace
        semworker._prepare_ephemeral_coordination_dir()

    def tearDown(self) -> None:
        self.coordination.stop()
        common.DATA_DIR = self.saved_data_dir
        scrub_coordination_namespaces(self.temp.name)
        self.temp.cleanup()
    @staticmethod
    def _record(
            *, pid: int = 4242, process_start: str = "birth",
            owner_nonce: str = "a" * 32) -> dict:
        return {
            "version": semworker.PROTOCOL,
            "pid": pid,
            "port": 31337,
            "token": "b" * 64,
            "started_at": 100.0,
            "process_start": process_start,
            "build_id": semworker.WORKER_BUILD_ID,
            "capabilities": list(semworker.CAPABILITIES),
            "owner": "disposable",
            "tree_bound": True,
            "owner_nonce": owner_nonce,
        }

    @staticmethod
    def _snapshot(label: str, identity: int = 1) -> ownerfile.Snapshot:
        raw = label.encode("utf-8")
        return ownerfile.Snapshot(
            (1, identity, len(raw), identity * 1000),
            float(identity), raw)

    @staticmethod
    def _clear_worker_env():
        return mock.patch.dict(os.environ, {
            "AGREP_NO_DAEMON": "",
            "AGREP_NO_SEM_WORKER": "",
        })

    def test_exact_inprocess_owner_returns_without_spawn_or_wait(self) -> None:
        clock = _Clock()
        inprocess = semworker._WorkerOwner(
            semworker._WorkerOwnerState.EXACT,
            {
                "pid": 4242,
                "process_start": "birth",
                "nonce": "a" * 32,
                "tree_bound": False,
            },
            self._snapshot("inprocess"))
        with self._clear_worker_env(), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=None), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    return_value=inprocess), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=clock.monotonic), \
                mock.patch.object(
                    semworker.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker())
        spawn.assert_not_called()
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(clock.now, 0.0)

    def test_exact_inprocess_owner_is_preacceptance_contention(self) -> None:
        inprocess = semworker._WorkerOwner(
            semworker._WorkerOwnerState.EXACT,
            {
                "pid": 4242,
                "process_start": "birth",
                "nonce": "a" * 32,
                "tree_bound": False,
            },
            self._snapshot("inprocess"))
        with self._clear_worker_env(), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_descriptor_entry_present", return_value=False), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock", return_value=inprocess), \
                mock.patch.object(
                    semworker, "_worker_coordination_refusal_reason",
                    return_value=None), \
                mock.patch.object(semworker, "_worker_request") as request, \
                self.assertRaisesRegex(
                    semworker.ResidentSemanticPreflightUnavailable,
                    "ownership is still settling"):
            semworker.search_worker(
                "queued fallback", level="hybrid", k=3,
                timeout_s=1.0, start_timeout_s=0.5)
        request.assert_not_called()

    def test_worker_gate_reads_only_this_tests_store(self) -> None:
        """Ownership and removal state another module left in the shared data dir
        must not decide whether this module's follower may wait."""
        for path in (removal_fence.background_removal_path(),
                     removal_fence.background_removal_cooldown_path(),
                     indexd_runtime.DERIVED_OWNER_PATH,
                     indexd_runtime.INGEST_CACHE_PATH,
                     corpusdb.DB_PATH):
            self.assertTrue(path.is_relative_to(self.root), path)
        with self._clear_worker_env():
            self.assertFalse(semworker._worker_disabled())
            removal_fence.background_removal_path().write_text(json.dumps({
                "pid": os.getpid(),
                "process_start": common.process_start_identity(os.getpid()),
                "started_at": time.time(), "nonce": "b" * 32}), encoding="utf-8")
            self.assertTrue(semworker._worker_disabled())

    def test_follower_stops_waiting_when_peer_claim_disappears(self) -> None:
        clock = _Clock()
        birth = "self-birth"
        claim = {
            "pid": os.getpid(),
            "at": 100.0,
            "process_start": birth,
            "nonce": "a" * 32,
        }
        semworker.start_claim_path().write_text(
            json.dumps(claim, separators=(",", ":")), encoding="utf-8")

        def peer_step(delay: float) -> None:
            clock.sleep(delay)
            semworker.start_claim_path().unlink(missing_ok=True)

        with self._clear_worker_env(), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value=birth), \
                mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=clock.monotonic), \
                mock.patch.object(
                    semworker.time, "sleep", side_effect=peer_step), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker())
        spawn.assert_not_called()
        self.assertEqual(clock.sleeps, [0.02])
        self.assertLess(clock.now, semworker.START_TIMEOUT_S)

    def _assert_generation_untouched(
            self, discard, stop, terminate) -> None:
        discard.assert_not_called()
        stop.assert_not_called()
        terminate.assert_not_called()

    def test_foreign_writer_owner_can_query_compatible_resident(self) -> None:
        rec = self._record()
        result = {"results": [{"session": "compatible"}]}
        descriptor = (rec, self._snapshot("descriptor"))
        with self._clear_worker_env(), \
                mock.patch.object(
                    semworker, "_worker_coordination_refusal_reason",
                    return_value="foreign derived owner"), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=descriptor) as reconcile, \
                mock.patch.object(
                    semworker, "_ensure_worker",
                    side_effect=AssertionError("foreign reader launched a worker")), \
                mock.patch.object(
                    semworker, "_worker_request", return_value=result) as request:
            found = semworker.search_worker(
                "compatible resident", level="hybrid", k=3)
        self.assertEqual(found, result)
        reconcile.assert_called_once()
        request.assert_called_once_with(rec, mock.ANY)

    def test_journal_blocked_owner_can_launch_compatible_resident(self) -> None:
        rec = self._record()
        claim = mock.Mock()
        process = mock.Mock()
        absent = semworker._WorkerOwner(semworker._WorkerOwnerState.ABSENT)
        journal = indexd_runtime.DerivedMutationInfo(
            "unavailable", None, "live rollback journal",
            journal_blocked=True)
        with self._clear_worker_env(), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=journal), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_descriptor_entry_present", return_value=False), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock", return_value=absent), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=claim), \
                mock.patch.object(
                    semworker, "_release_start_claim") as release, \
                mock.patch.object(
                    semworker, "_spawn_worker", return_value=process) as spawn, \
                mock.patch.object(
                    semworker, "_wait_for_worker", return_value=rec) as wait:
            found = semworker._ensure_worker()
        self.assertEqual(found, rec)
        spawn.assert_called_once_with(claim)
        wait.assert_called_once_with(semworker.START_TIMEOUT_S, process=process)
        release.assert_called_once_with(claim)

    def test_foreign_writer_owner_never_queries_incompatible_resident(self) -> None:
        rec = self._record()
        rec["build_id"] = "incompatible"
        observation = semworker._DescriptorObservation(
            semworker._DescriptorState.INCOMPATIBLE, rec,
            self._snapshot("descriptor"), True)
        with self._clear_worker_env(), \
                mock.patch.object(
                    semworker, "_worker_coordination_refusal_reason",
                    return_value="foreign derived owner"), \
                mock.patch.object(
                    semworker, "_inspect_descriptor", return_value=observation), \
                mock.patch.object(
                    semworker, "_ensure_worker",
                    side_effect=AssertionError("foreign reader launched a worker")), \
                mock.patch.object(semworker, "_worker_request") as request, \
                mock.patch.object(semworker, "_retire_incompatible") as retire:
            found = semworker.search_worker(
                "incompatible resident", level="hybrid", k=3)
        self.assertIsNone(found)
        request.assert_not_called()
        retire.assert_not_called()

    def test_dead_foreign_owner_never_signals_the_owning_builds_live_worker(
            self) -> None:
        # The upgrade window: this build may launch its own query worker, but
        # a live worker of the build that still owns the stores is not its to
        # stop, retire or unpublish.
        foreign = semworker.indexd_runtime.DerivedMutationInfo(
            "foreign", "a" * 20,
            "derived stores owned-by aaaaaaaaaaaaaaaaaaaa; this build is bbbb")
        for state in (semworker._DescriptorState.INCOMPATIBLE,
                      semworker._DescriptorState.LEGACY):
            with self.subTest(state=state):
                rec = self._record()
                rec["build_id"] = "owning-build"
                observation = semworker._DescriptorObservation(
                    state, rec, self._snapshot("descriptor"), True)
                with self._clear_worker_env(), \
                        mock.patch.object(
                            semworker.indexd_runtime,
                            "derived_writer_mutation_info",
                            return_value=foreign), \
                        mock.patch.object(
                            semworker.indexd_runtime, "live_indexer_claim",
                            return_value=False), \
                        mock.patch.object(
                            semworker, "_protective_refusal_reason",
                            return_value=None), \
                        mock.patch.object(
                            semworker, "_inspect_descriptor",
                            return_value=observation), \
                        mock.patch.object(
                            semworker, "_request_worker_stop") as stop, \
                        mock.patch.object(
                            semworker, "_terminate_worker_tree") as terminate, \
                        mock.patch.object(
                            semworker, "_discard_record") as discard:
                    self.assertIsNone(semworker._worker_coordination_refusal_reason())
                    self.assertIsNone(semworker._reconcile_descriptor())
                stop.assert_not_called()
                terminate.assert_not_called()
                discard.assert_not_called()

    def _superseded_case(self, argv: list[str] | None, *, own_build_id=None):
        """A dead foreign owner and its live incompatible worker, observed with
        `argv` as that worker's command line."""
        foreign = semworker.indexd_runtime.DerivedMutationInfo(
            "foreign", "a" * 20,
            "derived stores owned-by aaaaaaaaaaaaaaaaaaaa; this build is bbbb")
        rec = self._record()
        rec["build_id"] = "owning-build"
        observation = semworker._DescriptorObservation(
            semworker._DescriptorState.INCOMPATIBLE, rec,
            self._snapshot("descriptor"), True)
        from hookless import procscan
        return rec, (
            self._clear_worker_env(),
            mock.patch.object(
                semworker.indexd_runtime, "derived_writer_mutation_info",
                return_value=foreign),
            mock.patch.object(
                semworker.indexd_runtime, "live_indexer_claim", return_value=False),
            mock.patch.object(
                semworker, "_protective_refusal_reason", return_value=None),
            mock.patch.object(
                semworker, "_inspect_descriptor", return_value=observation),
            mock.patch.object(
                semworker, "_process_owner",
                return_value=ownerfile.ProcessOwner.EXACT_LIVE),
            mock.patch.object(procscan, "process_argv", return_value=argv),
            mock.patch.object(
                semworker, "_compute_build_id",
                return_value=own_build_id or semworker.WORKER_BUILD_ID),
        )

    def test_a_worker_this_build_replaced_in_place_is_retired_while_no_query_runs(
            self) -> None:
        # After an in-place upgrade the old build can never relaunch from its own
        # path, so its idle worker must not hold meaning search hostage until it
        # idles out. Its own queries share the request slot held across the stop.
        here = str(Path(semworker.__file__).resolve())
        rec, patches = self._superseded_case(["/venv/bin/python", here, "--serve"])
        events: list[str] = []
        slot = mock.Mock()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                semworker, "_acquire_request_slot",
                side_effect=lambda *_: events.append("slot") or slot))
            stack.enter_context(mock.patch.object(
                semworker, "_release_request_slot",
                side_effect=lambda held: events.append("release")))
            stack.enter_context(mock.patch.object(
                semworker, "_request_worker_stop",
                side_effect=lambda *_a, **_k: events.append("stop") or True))
            stack.enter_context(mock.patch.object(
                semworker, "_terminate_worker_tree",
                side_effect=lambda *_a: events.append("terminate") or True))
            stack.enter_context(mock.patch.object(semworker, "_discard_record"))
            self.assertIsNone(semworker.foreign_live_worker_reason())
            self.assertIsNone(semworker._reconcile_descriptor())
        self.assertEqual(events, ["slot", "stop", "terminate", "release"])

    def test_a_live_worker_of_another_install_or_a_newer_build_is_left_alone(
            self) -> None:
        here = str(Path(semworker.__file__).resolve())
        cases = {
            "another install": (["/other/bin/python", "/other/py/semworker.py",
                                 "--serve"], None),
            "unreadable argv": (None, None),
            "this process is stale": (["/venv/bin/python", here, "--serve"],
                                      "newer-build-on-disk"),
        }
        for label, (argv, on_disk) in cases.items():
            with self.subTest(label):
                rec, patches = self._superseded_case(argv, own_build_id=on_disk)
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    stop = stack.enter_context(
                        mock.patch.object(semworker, "_request_worker_stop"))
                    terminate = stack.enter_context(
                        mock.patch.object(semworker, "_terminate_worker_tree"))
                    discard = stack.enter_context(
                        mock.patch.object(semworker, "_discard_record"))
                    self.assertIn("holds the model",
                                  semworker.foreign_live_worker_reason() or "")
                    self.assertIsNone(semworker._reconcile_descriptor())
                stop.assert_not_called()
                terminate.assert_not_called()
                discard.assert_not_called()

    def test_a_superseded_worker_mid_query_is_not_stopped(self) -> None:
        here = str(Path(semworker.__file__).resolve())
        rec, patches = self._superseded_case(["/venv/bin/python", here, "--serve"])
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                semworker, "_acquire_request_slot",
                side_effect=semworker.ResidentSemanticPreflightTimeout(
                    "semantic request expired while queued")))
            stop = stack.enter_context(
                mock.patch.object(semworker, "_request_worker_stop"))
            terminate = stack.enter_context(
                mock.patch.object(semworker, "_terminate_worker_tree"))
            self.assertIsNone(semworker._reconcile_descriptor())
        stop.assert_not_called()
        terminate.assert_not_called()

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "argv of another process is read only on macOS and Linux")
    def test_process_argv_reads_a_live_child_exactly(self) -> None:
        from hookless import procscan
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "--serve"],
            stdin=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        argv = procscan.process_argv(child.pid)
        self.assertIsNotNone(argv)
        self.assertEqual(argv[1:], ["-c", "import time; time.sleep(30)", "--serve"])
        self.assertIsNone(procscan.process_argv(0x7FFFFFFF))

    def test_foreign_writer_owner_can_reclaim_request_coordination(self) -> None:
        path = semworker.request_lock_path()
        path.write_bytes(b"stale malformed owner")
        os.utime(path, (1.0, 1.0))
        with mock.patch.object(
                semworker, "_mutation_refused", return_value=True), \
                mock.patch.object(
                    semworker, "_request_coordination_refused",
                    return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="self-birth"), \
                mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(semworker, "_discard_record") as generic:
            slot = semworker._acquire_request_slot(time.monotonic() + 1.0)
        self.assertIsNotNone(slot)
        generic.assert_not_called()
        if slot is not None:
            semworker._release_request_slot(slot)

    def test_a_caller_reclaims_its_own_stranded_request_slot(self) -> None:
        # a release honours a mutex an earlier reap minted; one held past every attempt
        # leaves the record naming this live process, which only this process may reclaim
        path = semworker.request_lock_path()
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            slot = semworker._acquire_request_slot(time.monotonic() + 1.0)
            self.assertIsNotNone(slot)
            stranded = slot.snapshot
            (path.parent / semworker.ownerfile.SLOT_MUTEX_NAME).touch()
            with mock.patch.object(
                    semworker.ownerfile, "_lock_descriptor", return_value="busy"), \
                    mock.patch.object(semworker.ownerfile, "_SLOT_MUTEX_WAIT_S", 0.01):
                self.assertFalse(semworker._release_request_slot(slot))
            self.assertEqual(semworker.ownerfile.snapshot(path), stranded)
            self.assertTrue(semworker._request_slot_reclaimable(stranded))
            with mock.patch.object(
                    semworker.ownerfile, "_HELD_TOKENS", {slot.token}):
                self.assertFalse(semworker._request_slot_reclaimable(stranded))
                with self.assertRaises(semworker.ResidentSemanticPreflightTimeout):
                    semworker._acquire_request_slot(time.monotonic() + 0.2)
            again = semworker._acquire_request_slot(time.monotonic() + 1.0)
            self.assertIsNotNone(again)
            self.assertNotEqual(again.snapshot.raw, stranded.raw)
            self.assertTrue(semworker._release_request_slot(again))
        self.assertFalse(path.exists())

    def test_a_denied_inspection_waits_only_on_this_process_holder(self) -> None:
        # Windows can refuse to open the slot a sibling thread holds: contention while this
        # process holds it, a refusal for every other denial
        denied = PermissionError(13, "Access is denied")
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            slot = semworker._acquire_request_slot(time.monotonic() + 1.0)
            self.assertIsNotNone(slot)
            try:
                with mock.patch.object(
                        semworker, "_own_record_snapshot", side_effect=denied):
                    with mock.patch.object(semworker.ownerfile, "_WINDOWS", True):
                        with self.assertRaises(semworker.ResidentSemanticPreflightTimeout):
                            semworker._acquire_request_slot(time.monotonic() + 0.2)
                    for windows in (True, False):
                        with self.subTest(windows=windows), \
                                mock.patch.object(semworker.ownerfile, "_WINDOWS", windows), \
                                mock.patch.object(
                                    semworker.ownerfile, "holds_record",
                                    return_value=not windows):
                            with self.assertRaisesRegex(
                                    semworker.ResidentSemanticPreflightUnavailable,
                                    "cannot inspect"):
                                semworker._acquire_request_slot(time.monotonic() + 1.0)
            finally:
                self.assertTrue(semworker._release_request_slot(slot))

    def test_request_coordination_respects_removal_fence(self) -> None:
        with mock.patch.object(
                semworker.removal_fence, "background_removal_active",
                return_value=True), \
                mock.patch.object(
                    semworker.ownerfile, "create_exclusive") as create:
            with self.assertRaisesRegex(
                    semworker.ResidentSemanticPreflightUnavailable,
                    r"semantic request coordination cannot write .*"
                    "agrep removal is active"):
                semworker._acquire_request_slot(
                    time.monotonic() + 1.0)
        create.assert_not_called()

    def test_removal_owner_can_settle_ephemeral_worker_record(self) -> None:
        observed = self._snapshot("worker-owner")
        with mock.patch.object(
                semworker.removal_fence, "background_removal_active",
                return_value=True), \
                mock.patch.object(
                    semworker.removal_fence,
                    "background_removal_owned_by_current_process",
                    return_value=True), \
                mock.patch.object(
                    semworker.ownerfile, "remove_exact",
                    return_value=True) as remove:
            self.assertTrue(semworker._discard_worker_record(
                semworker.worker_lock_path(), observed))

        remove.assert_called_once_with(
            semworker.worker_lock_path(), observed, tombstone=True,
            require_stable_mtime=True)

    def test_loopback_blocked_client_publishes_exact_retire_handoff(self) -> None:
        rec = self._record()
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            self.assertTrue(semworker._request_worker_retire_handoff(rec))
            self.assertTrue(semworker._request_worker_retire_handoff(rec))
        path = semworker.retire_request_path(rec["owner_nonce"])
        self.assertTrue(path.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(semworker._retire_handoff_requested(
            rec["owner_nonce"]))
        semworker._discard_retire_handoff(rec["owner_nonce"])
        self.assertFalse(path.exists())

    def test_loopback_connect_denial_requests_handoff_before_fallback(
            self) -> None:
        rec = self._record()
        slot = mock.Mock()
        connection = mock.Mock()
        connection.connect.side_effect = PermissionError(
            1, "operation not permitted")
        with mock.patch.object(
                semworker, "_acquire_request_slot", return_value=slot), \
                mock.patch.object(
                    semworker, "_release_request_slot") as release, \
                mock.patch.object(
                    semworker.http.client, "HTTPConnection",
                    return_value=connection), \
                mock.patch.object(
                    semworker, "_request_worker_retire_handoff",
                    return_value=True) as handoff, \
                self.assertRaises(
                    semworker.ResidentSemanticPreflightUnavailable):
            semworker._worker_request(
                rec, {"query": "sandbox handoff", "level": "hybrid",
                      "k": 2, "filters": {}, "timing": False},
                timeout_s=1.0)
        handoff.assert_called_once_with(rec)
        release.assert_called_once_with(slot)

    def test_live_protocol_nine_owner_fences_v10_model_acquisition(self) -> None:
        legacy = {
            "pid": os.getpid(), "started_at": time.time(),
            "process_start": "legacy-birth", "nonce": "a" * 32,
            "tree_bound": False, "named_job": False,
        }
        semworker.legacy_worker_lock_path().write_text(
            json.dumps(legacy, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(
                common, "process_start_identity",
                return_value="legacy-birth"):
            owner = semworker.acquire_inprocess_owner()
        self.assertIsNone(owner)
        self.assertFalse(semworker.worker_lock_path().exists())
        self.assertTrue(semworker.legacy_worker_lock_path().exists())

    def test_server_consumes_retire_handoff_before_next_request(self) -> None:
        server = object.__new__(semworker.SemanticWorkerServer)
        server.owner_nonce = "a" * 32
        server.stop = mock.Mock()
        server.stop.is_set.return_value = False
        server.http = mock.Mock()
        server.descriptor_snapshot = self._snapshot("descriptor")
        server._release_resources = mock.Mock(return_value=True)
        with mock.patch.object(common, "log"), \
                mock.patch.object(
                semworker, "_retire_handoff_requested",
                return_value=True), \
                mock.patch.object(semworker, "_discard_record") as discard, \
                mock.patch.object(
                    semworker, "_discard_retire_handoff") as discard_handoff:
            server.serve()
        server.http.handle_request.assert_not_called()
        server._release_resources.assert_called_once_with()
        discard.assert_called_once_with(
            semworker.descriptor_path(), server.descriptor_snapshot)
        discard_handoff.assert_called_once_with(server.owner_nonce)
        server.http.server_close.assert_called_once_with()

    def test_internal_session_filters_validate(self) -> None:
        request = {
            "query": "session filter", "level": "hybrid", "k": 2,
            "filters": {"_exclude_sessions": ["unrelated"]}, "timing": False,
        }
        _query, _level, _k, filters, _timing = (
            semworker._validate_request(request))
        self.assertEqual(filters, {"_exclude_sessions": ("unrelated",)})
        request["filters"] = {"_exclude_sessions": [""]}
        with self.assertRaisesRegex(ValueError, "invalid semantic filter"):
            semworker._validate_request(request)

    def test_production_coordination_is_external_to_readonly_data_dir(self) -> None:
        with fixture_tree(
                prefix="agrep-semworker-runtime-") as runtime_raw:
            runtime = Path(runtime_raw)
            protected = runtime / "protected-data"
            protected.mkdir()
            other = runtime / "other-data"
            other.mkdir()
            with mock.patch.object(common, "DATA_DIR", protected), \
                    mock.patch.object(
                        semworker, "_coordination_base_path",
                        return_value=runtime.resolve()), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_DIR_SOURCE": "default"}), \
                    mock.patch.object(
                        semworker, "_data_dir_readonly", return_value=True), \
                    mock.patch.object(
                        semworker.removal_fence,
                        "background_removal_active", return_value=False), \
                    mock.patch.object(
                        common, "process_start_identity",
                        return_value="self-birth"):
                request_path = semworker.request_lock_path()
                worker_path = semworker.worker_lock_path()
                self.assertEqual(request_path.parent.parent, runtime.resolve())
                self.assertEqual(worker_path.parent, request_path.parent)
                # resolving a path mints nothing; the writers below do
                self.assertFalse(request_path.parent.exists())
                self.assertNotEqual(request_path, worker_path)
                self.assertEqual(
                    semworker.descriptor_path().parent, protected)
                self.assertEqual(
                    semworker.start_claim_path().parent, protected)

                slot = semworker._acquire_request_slot(
                    time.monotonic() + 1.0)
                owner = semworker.acquire_inprocess_owner()
                self.assertIsInstance(slot, ownerfile.Handle)
                self.assertIsInstance(owner, ownerfile.Handle)
                coordination_info = os.lstat(request_path.parent)
                if os.name == "nt":
                    self.assertFalse(
                        getattr(coordination_info, "st_file_attributes", 0)
                        & getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                else:
                    self.assertEqual(
                        stat.S_IMODE(coordination_info.st_mode), 0o700)
                semworker.verify_inprocess_owner(owner)

                with mock.patch.object(common, "DATA_DIR", other):
                    self.assertNotEqual(
                        semworker.worker_lock_path(), worker_path)

                semworker.finish_inprocess_owner(
                    owner, resources_released=True)
                self.assertTrue(semworker._release_request_slot(slot))
                self.assertFalse(worker_path.exists())
                self.assertFalse(request_path.exists())

    @unittest.skipIf(os.name == "nt", "POSIX uses a fixed coordination root")
    def test_posix_coordination_root_does_not_follow_tmpdir(self) -> None:
        with mock.patch.object(
                semworker.tempfile, "gettempdir",
                return_value="/tmp/split-sandbox-root"):
            self.assertEqual(
                self.production_coordination_base_path(),
                Path("/tmp").resolve())

    @unittest.skipIf(os.name == "nt", "POSIX fixed-root convergence proof")
    def test_coordination_paths_converge_across_process_environments(self) -> None:
        script = (
            "import json,semworker;"
            "print(json.dumps([str(semworker.worker_lock_path()),"
            "str(semworker.request_lock_path())]))")
        python_path = str(Path(__file__).resolve().parent)
        with fixture_tree(
                prefix="agrep-semworker-path-proof-") as raw:
            root = Path(raw).resolve()
            data = root / "data"
            data.mkdir()
            alias = root / "data-alias"
            alias.symlink_to(data, target_is_directory=True)
            outputs = []
            for index, configured in enumerate((data, data, alias)):
                temp_root = root / f"tmp-{index}"
                temp_root.mkdir()
                env = dict(os.environ)
                env.update({
                    "AGREP_DATA_DIR": str(configured),
                    "AGREP_DATA_DIR_SOURCE": "env",
                    "PYTHONPATH": python_path,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": str(temp_root),
                })
                if index:
                    env["AGREP_DATA_READONLY"] = str(configured)
                else:
                    env.pop("AGREP_DATA_READONLY", None)
                completed = subprocess.run(
                    [sys.executable, "-c", script], env=env,
                    cwd=str(root), text=True, capture_output=True,
                    timeout=5, check=True)
                outputs.append(json.loads(completed.stdout))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], outputs[2])
            coordination = Path(outputs[0][0]).parent
            self.assertEqual(coordination, Path(outputs[0][1]).parent)
            # three processes resolved the same path and none minted it
            self.assertFalse(coordination.exists())

    @unittest.skipIf(os.name == "nt", "POSIX cross-process owner proof")
    def test_cross_process_readonly_contender_cannot_split_model_owner(self) -> None:
        holder_script = (
            "import json,sys,semworker;"
            "owner=semworker.acquire_inprocess_owner();"
            "print(json.dumps({'acquired':owner is not None,"
            "'path':str(semworker.worker_lock_path())}),flush=True);"
            "sys.stdin.readline();"
            "semworker.finish_inprocess_owner(owner,resources_released=True) "
            "if owner is not None else None")
        contender_script = (
            "import json,semworker;"
            "owner=semworker.acquire_inprocess_owner();"
            "print(json.dumps({'acquired':owner is not None,"
            "'path':str(semworker.worker_lock_path())}));"
            "semworker.finish_inprocess_owner(owner,resources_released=True) "
            "if owner is not None else None")
        python_path = str(Path(__file__).resolve().parent)
        with fixture_tree(
                prefix="agrep-semworker-owner-proof-") as raw:
            root = Path(raw).resolve()
            data = root / "data"
            data.mkdir()
            base_env = dict(os.environ)
            base_env.update({
                "AGREP_DATA_DIR": str(data),
                "AGREP_DATA_DIR_SOURCE": "env",
                "PYTHONPATH": python_path,
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            base_env.pop("AGREP_DATA_READONLY", None)
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script], env=base_env,
                cwd=str(root), text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertIsNotNone(holder.stdout)
                held = json.loads(holder.stdout.readline())
                self.assertTrue(held["acquired"])
                contender_env = dict(base_env)
                contender_env["AGREP_DATA_READONLY"] = str(data)
                completed = subprocess.run(
                    [sys.executable, "-c", contender_script],
                    env=contender_env, cwd=str(root), text=True,
                    capture_output=True, timeout=5, check=True)
                contender = json.loads(completed.stdout)
                self.assertEqual(contender["path"], held["path"])
                self.assertFalse(contender["acquired"])
            finally:
                if holder.stdin is not None:
                    holder.stdin.write("\n")
                    holder.stdin.flush()
                holder.communicate(timeout=5)
            Path(held["path"]).parent.rmdir()

    def test_coordination_directory_symlink_fails_closed(self) -> None:
        # resolving the path is pure; every writer verifies before creating
        with fixture_tree(
                prefix="agrep-semworker-coord-symlink-") as runtime_raw:
            runtime = Path(runtime_raw).resolve()
            protected = runtime / "data"
            protected.mkdir()
            target = runtime / "target"
            target.mkdir()
            with mock.patch.object(common, "DATA_DIR", protected), \
                    mock.patch.object(
                        semworker, "_coordination_base_path",
                        return_value=runtime), \
                    mock.patch.object(
                        common, "process_start_identity",
                        return_value="self-birth"):
                coordination = semworker._ephemeral_coordination_dir()
                coordination.symlink_to(target, target_is_directory=True)
                self.assertEqual(
                    semworker.request_lock_path().parent, coordination)
                with self.assertRaisesRegex(
                        semworker.ResidentSemanticPreflightUnavailable,
                        "not a private directory"):
                    semworker._acquire_request_slot(time.monotonic() + 0.5)
                self.assertIsNone(semworker._acquire_worker_lock())
                self.assertFalse(semworker._request_worker_retire_handoff(
                    {"owner_nonce": "a" * 32, "pid": 1, "process_start": "x"}))
                self.assertEqual(list(target.iterdir()), [])

    def test_request_slot_retries_normal_owner_handoff_race(self) -> None:
        slot = mock.Mock()
        with (
            mock.patch.object(
                semworker, "_request_coordination_refusal_reason",
                return_value=None),
            mock.patch.object(
                common, "process_start_identity", return_value="self-birth"),
            mock.patch.object(
                semworker.ownerfile, "create_exclusive",
                side_effect=(FileExistsError(), slot)) as create,
            mock.patch.object(
                semworker.ownerfile, "snapshot",
                side_effect=ownerfile.OwnershipLost(
                    "owner record path changed while reading")),
            mock.patch.object(semworker.time, "sleep") as sleep,
        ):
            acquired = semworker._acquire_request_slot(
                time.monotonic() + 1.0)

        self.assertIs(acquired, slot)
        self.assertEqual(create.call_count, 2)
        sleep.assert_called_once()

    def test_cold_search_preserves_the_loopback_bind_failure(self) -> None:
        claim = mock.Mock()
        claim.snapshot = ownerfile.Snapshot(
            (1, 1, 1, 1), 1.0,
            json.dumps({
                "pid": os.getpid(), "at": time.time(),
                "process_start": "self-birth", "nonce": "a" * 32,
            }).encode())
        absent = semworker._WorkerOwner(
            semworker._WorkerOwnerState.ABSENT, None, None)
        bind = {
            "bindable": False, "operation": "bind 127.0.0.1:0",
            "reason": "semantic worker cannot bind loopback 127.0.0.1:0 "
                      "(PermissionError: blocked)",
        }
        with (
            self._clear_worker_env(),
            mock.patch.object(
                semworker, "_reconcile_descriptor", return_value=None),
            mock.patch.object(
                semworker, "_descriptor_entry_present", return_value=False),
            mock.patch.object(
                semworker, "_inspect_worker_lock", return_value=absent),
            mock.patch.object(
                semworker, "_acquire_start_claim", return_value=claim),
            mock.patch.object(
                semworker, "_release_start_claim") as release,
            mock.patch.object(
                semworker, "_worker_coordination_refusal_reason",
                return_value=None),
            mock.patch.object(
                semworker, "loopback_bind_status", return_value=bind),
            mock.patch.object(semworker.subprocess, "Popen") as spawn,
            self.assertRaisesRegex(
                semworker.ResidentSemanticPreflightUnavailable,
                "cannot bind loopback 127.0.0.1:0"),
        ):
            semworker.search_worker("query", level="hybrid", k=1)
        release.assert_called_once_with(claim)
        spawn.assert_not_called()

    def test_request_write_failure_names_the_exact_record(self) -> None:
        with (
            mock.patch.object(
                semworker, "_request_coordination_refusal_reason",
                return_value=None),
            mock.patch.object(
                common, "process_start_identity", return_value="self-birth"),
            mock.patch.object(
                semworker.ownerfile, "create_exclusive",
                side_effect=PermissionError(1, "operation not permitted")),
            self.assertRaises(
                semworker.ResidentSemanticPreflightUnavailable) as raised,
        ):
            semworker._acquire_request_slot(time.monotonic() + 1.0)
        detail = str(raised.exception)
        self.assertIn(str(semworker.request_lock_path()), detail)
        self.assertIn("operation not permitted", detail)
        self.assertIn("doctor --deep", detail)

    def test_loopback_status_names_socket_creation_failure(self) -> None:
        with mock.patch.object(
                semworker.socket, "socket",
                side_effect=PermissionError(1, "socket creation denied")):
            status = semworker.loopback_bind_status()
        self.assertFalse(status["bindable"])
        self.assertEqual(status["operation"], "bind 127.0.0.1:0")
        self.assertIn("socket creation denied", status["reason"])
        self.assertIn("doctor --deep", status["reason"])

    def test_diagnostic_status_proves_a_real_query_round_trip(self) -> None:
        resident = {
            "running": True, "descriptor_state": "ready", "pid": 42,
        }
        with (
            mock.patch.object(
                semworker, "resident_status",
                return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={
                    "bindable": True, "operation": "bind 127.0.0.1:0",
                    "reason": None,
                }),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={
                    "state": "ready", "writable": True,
                    "path": str(semworker.request_lock_path()),
                    "reason": None,
                }),
            mock.patch.object(
                semworker, "search_worker",
                return_value={"results": []}) as search_query,
        ):
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        self.assertTrue(status["discovered"])
        self.assertTrue(status["alive"])
        self.assertTrue(status["ready"])
        self.assertTrue(status["query_serving"])
        self.assertEqual(status["state"], "query-serving")
        search_query.assert_called_once()
        self.assertEqual(search_query.call_args.kwargs["filters"], {
            "_allow_model_download": False,
            "_diagnostic_only": True,
        })

    def test_diagnostic_status_rejects_an_unavailable_envelope(self) -> None:
        resident = {
            "running": True, "descriptor_state": "ready", "pid": 42,
        }
        refusal = {
            "results": [], "score_kind": "unavailable",
            "semantic_unavailable": True,
            "semantic_integrity": {
                "state": "generation-rejected",
                "reason": "active semantic digest mismatch",
            },
        }
        with (
            mock.patch.object(
                semworker, "resident_status",
                return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={"writable": True, "reason": None}),
            mock.patch.object(
                semworker, "search_worker", return_value=refusal),
        ):
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        self.assertEqual(status["state"], "query-unavailable")
        self.assertFalse(status["query_serving"])
        self.assertIn("generation-rejected", status["reason"])
        self.assertIn("active semantic digest mismatch", status["reason"])

    def test_diagnostic_status_waits_through_a_busy_healthy_worker(self) -> None:
        resident = {
            "running": True, "descriptor_state": "ready", "pid": 42,
        }
        with (
            mock.patch.object(
                semworker, "resident_status",
                return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={
                    "state": "busy", "writable": None,
                    "reason": "another semantic query owns serialization",
                }),
            mock.patch.object(
                semworker, "search_worker",
                return_value={"results": []}) as search_query,
        ):
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        self.assertEqual(status["state"], "query-serving")
        self.assertTrue(status["query_serving"])
        search_query.assert_called_once()

    def test_diagnostic_status_keeps_a_persistently_busy_worker_unverified(
            self) -> None:
        resident = {
            "running": True, "descriptor_state": "ready", "pid": 42,
        }
        with (
            mock.patch.object(
                semworker, "resident_status",
                return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={
                    "state": "busy", "writable": None,
                    "reason": "another semantic query owns serialization",
                }),
            mock.patch.object(
                semworker, "search_worker",
                side_effect=semworker.ResidentSemanticTimeout(
                    "semantic request expired while queued")),
        ):
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        self.assertEqual(status["state"], "query-busy")
        self.assertIsNone(status["query_serving"])
        self.assertTrue(status["alive"])

    def test_diagnostic_status_does_not_replace_a_protected_worker(self) -> None:
        resident = {
            "running": False, "protected": True, "blocked": True,
            "descriptor_state": "incompatible",
            "owner_state": "descriptor-incompatible",
        }
        with (
            mock.patch.object(
                semworker, "resident_status", return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={"writable": True, "reason": None}),
            mock.patch.object(semworker, "search_worker") as search_query,
        ):
            status = semworker.diagnostic_query_status(ready=True)
        self.assertEqual(status["state"], "worker-protected")
        self.assertFalse(status["query_serving"])
        self.assertIn("descriptor-incompatible", status["reason"])
        search_query.assert_not_called()

    def test_diagnostic_status_names_an_incompatible_writer_owner(self) -> None:
        with (
            mock.patch.object(
                semworker, "resident_status",
                return_value={"running": False, "starting": True}),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={"writable": True, "reason": None}),
            mock.patch.object(
                semworker, "_worker_coordination_refusal_reason",
                return_value="derived stores owned by another build"),
            mock.patch.object(semworker, "search_worker") as search_query,
        ):
            status = semworker.diagnostic_query_status(ready=True)
        self.assertEqual(status["state"], "owner-blocked")
        self.assertFalse(status["discovered"])
        self.assertTrue(status["alive"])
        self.assertFalse(status["query_serving"])
        self.assertIn("derived stores owned by another build", status["reason"])
        self.assertIn("active installed agrep build", status["reason"])
        search_query.assert_not_called()

    def _diagnostic_probe_mocks(self, search_outcomes: list) -> list:
        resident = {
            "running": True, "descriptor_state": "ready", "pid": 42,
        }
        return [
            mock.patch.object(
                semworker, "resident_status",
                return_value=resident),
            mock.patch.object(
                semworker, "loopback_bind_status",
                return_value={"bindable": True, "reason": None}),
            mock.patch.object(
                semworker, "request_coordination_status",
                return_value={"writable": True, "reason": None}),
            mock.patch.object(
                semworker, "search_worker", side_effect=search_outcomes),
        ]

    def test_diagnostic_status_reports_a_generation_that_moved_under_it(
            self) -> None:
        # Right after a large publication the probe (which never refreshes)
        # answers "stale" while the lane rebinds: that is convergence, not a
        # broken worker, and the newer observation is what doctor reports.
        refusal = semworker.ResidentSemanticUnavailable(
            "embeddings stale for the current generation; inspect with "
            "`agrep doctor --deep`")
        moved = {"coherent": False, "searchable": False, "state": "stale"}
        with contextlib.ExitStack() as stack:
            for patcher in self._diagnostic_probe_mocks([refusal]):
                stack.enter_context(patcher)
            probe = mock.Mock(return_value=moved)
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5, coherence_probe=probe)
        probe.assert_called_once_with()
        self.assertEqual(status["state"], "generation-moved")
        self.assertIsNone(status["query_serving"])
        self.assertIn("moved during the probe", status["reason"])
        self.assertIn("stale", status["reason"])
        self.assertEqual(status["coherence_after"], moved)

    def test_diagnostic_status_retries_once_when_the_lane_is_searchable_again(
            self) -> None:
        refusal = semworker.ResidentSemanticUnavailable(
            "embeddings stale for the current generation")
        rebound = {"coherent": True, "searchable": True, "state": "current"}
        with contextlib.ExitStack() as stack:
            for patcher in self._diagnostic_probe_mocks(
                    [refusal, {"results": []}]):
                search_query = stack.enter_context(patcher)
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5,
                coherence_probe=mock.Mock(return_value=rebound))
        self.assertEqual(status["state"], "query-serving")
        self.assertTrue(status["query_serving"])
        self.assertEqual(search_query.call_count, 2)

    def test_diagnostic_status_bounds_its_retry_under_a_republishing_corpus(
            self) -> None:
        # Two refusals in a row with the lane moving between them is still
        # convergence: at most two queries and two reads, then the honest
        # "moved" verdict rather than a loop or a false failure.
        refusal = semworker.ResidentSemanticUnavailable(
            "embeddings stale for the current generation")
        rebound = {"coherent": True, "searchable": True, "state": "current"}
        moved = {"coherent": False, "searchable": False, "state": "stale"}
        with contextlib.ExitStack() as stack:
            for patcher in self._diagnostic_probe_mocks([refusal, refusal]):
                search_query = stack.enter_context(patcher)
            probe = mock.Mock(side_effect=[rebound, moved])
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5, coherence_probe=probe)
        self.assertEqual(search_query.call_count, 2)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(status["state"], "generation-moved")
        self.assertIsNone(status["query_serving"])

    def test_diagnostic_status_keeps_a_refusal_the_lane_cannot_explain(
            self) -> None:
        # The lane reads searchable twice yet the worker refuses twice: that
        # disagreement is real and stays a failure.
        refusal = semworker.ResidentSemanticUnavailable(
            "embeddings stale for the current generation")
        rebound = {"coherent": True, "searchable": True, "state": "current"}
        with contextlib.ExitStack() as stack:
            for patcher in self._diagnostic_probe_mocks([refusal, refusal]):
                search_query = stack.enter_context(patcher)
            probe = mock.Mock(return_value=rebound)
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5, coherence_probe=probe)
        self.assertEqual(search_query.call_count, 2)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(status["state"], "query-failed")
        self.assertFalse(status["query_serving"])

    def test_diagnostic_status_keeps_a_refusal_on_a_broken_generation(
            self) -> None:
        # A re-read landing on a generation the refresh cannot repair is not
        # "moved": the refusal stands, so doctor keeps alarming.
        refusal = semworker.ResidentSemanticUnavailable(
            "semantic embedding integrity failed")
        for state in ("corrupt-embeddings", "legacy-publication", "unknown", ""):
            with self.subTest(state=state), contextlib.ExitStack() as stack:
                for patcher in self._diagnostic_probe_mocks([refusal]):
                    stack.enter_context(patcher)
                status = semworker.diagnostic_query_status(
                    ready=True, timeout_s=0.5,
                    coherence_probe=mock.Mock(return_value={
                        "coherent": False, "searchable": False,
                        "state": state}))
                self.assertEqual(status["state"], "query-failed")
                self.assertFalse(status["query_serving"])
                self.assertIn("integrity failed", status["reason"])

    def test_diagnostic_status_without_a_probe_keeps_the_refusal(self) -> None:
        refusal = semworker.ResidentSemanticUnavailable(
            "embeddings stale for the current generation")
        with contextlib.ExitStack() as stack:
            for patcher in self._diagnostic_probe_mocks([refusal]):
                stack.enter_context(patcher)
            status = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        self.assertEqual(status["state"], "query-failed")
        self.assertFalse(status["query_serving"])

    def _probe_in_sandbox(self, *, resident: dict, preexisting: bool,
                          ready: bool = True) -> tuple[dict, mock.Mock]:
        """Run the probe against a fresh data dir whose coordination namespace
        lives under a private base. The pre-query resident read is stubbed;
        the final observation stays real."""
        with tempfile.TemporaryDirectory(prefix="agrep-probe-") as raw:
            root = Path(raw)
            data = root / "data"
            data.mkdir()
            base = root / "tmp"
            base.mkdir()
            with (mock.patch.object(common, "DATA_DIR", data),
                  mock.patch.object(
                      semworker, "_coordination_base_path", return_value=base),
                  mock.patch.dict(os.environ, {"AGREP_NO_SEM_WORKER": ""})):
                namespace = semworker._ephemeral_coordination_dir()
                if preexisting:
                    namespace.mkdir(mode=0o700)
                real_status = semworker.resident_status
                stubbed = [resident]
                with (mock.patch.object(
                          semworker, "resident_status",
                          side_effect=lambda **kw: (
                              stubbed.pop(0) if stubbed else real_status(**kw))),
                      mock.patch.object(
                          semworker, "loopback_bind_status",
                          return_value={"bindable": True, "reason": None}),
                      mock.patch.object(
                          semworker, "search_worker",
                          return_value={"results": []}),
                      mock.patch.object(
                          semworker, "stop_worker_and_wait") as stop,
                      mock.patch.object(
                          semworker, "_request_worker_stop") as request_stop):
                    status_out = semworker.diagnostic_query_status(
                        ready=ready, timeout_s=0.5)
                status_out["_namespace_leftover"] = namespace.exists()
                stop.request_stop = request_stop
        return status_out, stop

    def _fresh_sandbox(self):
        """(data dir, coordination base) patches for a data dir nothing has
        touched: the namespace must not exist unless a writer creates it."""
        raw = tempfile.TemporaryDirectory(prefix="agrep-observer-")
        root = Path(raw.name)
        data = root / "data"
        data.mkdir()
        base = root / "tmp"
        base.mkdir()
        stack = contextlib.ExitStack()
        stack.enter_context(raw)
        stack.enter_context(mock.patch.object(common, "DATA_DIR", data))
        stack.enter_context(mock.patch.object(
            semworker, "_coordination_base_path", return_value=base))
        return stack, data, base

    def test_observers_never_mint_the_coordination_namespace(self) -> None:
        # Every data dir a user ever touched used to leave an empty 0700 dir
        # in /tmp: resolving a lock path created it, and readers resolve.
        stack, _data, base = self._fresh_sandbox()
        with stack:
            semworker.worker_lock_path()
            semworker.request_lock_path()
            semworker.retire_request_path("a" * 32)
            semworker.resident_status(include_resources=True)
            self.assertEqual(
                semworker._inspect_worker_lock().state,
                semworker._WorkerOwnerState.ABSENT)
            self.assertFalse(semworker._retire_handoff_requested("a" * 32))
            semworker._discard_retire_handoff("a" * 32)
            self.assertTrue(semworker.stop_worker_and_wait(0.0, 0.0)["ok"])
            self.assertEqual(list(base.iterdir()), [])

    def test_every_writer_prepares_and_verifies_the_namespace(self) -> None:
        stack, _data, base = self._fresh_sandbox()
        with stack, mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            namespace = semworker._ephemeral_coordination_dir()
            slot = semworker._acquire_request_slot(time.monotonic() + 1.0)
            self.assertIsNotNone(slot)
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(os.lstat(namespace).st_mode), 0o700)
            semworker._release_request_slot(slot)
            namespace.rmdir()
            owner = semworker._acquire_worker_lock()
            self.assertIsNotNone(owner)
            self.assertTrue(namespace.is_dir())
            semworker._release_unpublished_owner(owner)
            namespace.rmdir()
            self.assertTrue(semworker._request_worker_retire_handoff(
                {"owner_nonce": "b" * 32, "pid": 1, "process_start": "x"}))
            self.assertTrue(
                (namespace / ("retire-" + "b" * 32)).is_file())

    @unittest.skipIf(os.name == "nt", "POSIX ownership check")
    def test_a_foreign_owned_namespace_is_refused_by_every_writer(self) -> None:
        stack, _data, base = self._fresh_sandbox()
        with stack, mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            namespace = semworker._ephemeral_coordination_dir()
            namespace.mkdir(mode=0o700)
            real_lstat = os.lstat

            def foreign(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path) == namespace:
                    return _restat(info, ST_UID=info.st_uid + 1)
                return info

            with mock.patch.object(semworker.os, "lstat", side_effect=foreign):
                with self.assertRaisesRegex(
                        semworker.ResidentSemanticPreflightUnavailable,
                        "foreign owner"):
                    semworker._acquire_request_slot(time.monotonic() + 0.5)
                self.assertIsNone(semworker._acquire_worker_lock())
                self.assertFalse(semworker._request_worker_retire_handoff(
                    {"owner_nonce": "c" * 32, "pid": 1, "process_start": "x"}))
            self.assertEqual(list(namespace.iterdir()), [])

    @contextlib.contextmanager
    def _bystander(self):
        """A session-leader process a crafted record could name as a worker."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _crafted_lock(self, process: subprocess.Popen) -> bytes:
        start = common.process_start_identity(process.pid)
        if not start:
            self.skipTest("no process start identity on this host")
        return json.dumps({
            "pid": process.pid, "started_at": time.time(),
            "process_start": start, "nonce": "d" * 32,
            "tree_bound": True, "named_job": False,
        }).encode("utf-8")

    def _assert_readers_refuse(self, process: subprocess.Popen) -> None:
        """Every record reader sees no usable owner and nothing gets killed."""
        inspected = semworker._inspect_worker_lock()
        self.assertIn(inspected.state, (
            semworker._WorkerOwnerState.ABSENT,
            semworker._WorkerOwnerState.BLOCKED))
        self.assertIsNone(inspected.record)
        resident = semworker.resident_status(include_resources=True)
        self.assertFalse(resident.get("running"))
        self.assertFalse(resident.get("starting"))
        self.assertNotEqual(resident.get("owner_state"), "exact")
        self.assertFalse(semworker._retire_handoff_requested("d" * 32))
        semworker._discard_retire_handoff("d" * 32)
        with mock.patch.object(
                semworker, "_terminate_worker_tree",
                side_effect=AssertionError("a bystander was terminated")):
            outcome = semworker.stop_worker_and_wait(0.3, 1.0)
        self.assertFalse(outcome.get("fallback_termination"))
        self.assertIsNone(process.poll(), "the bystander did not survive")

    @unittest.skipIf(os.name == "nt", "POSIX symlink and process-group fixture")
    def test_a_planted_symlink_namespace_cannot_hand_readers_a_record(
            self) -> None:
        # Sticky /tmp: another user plants our predictable namespace path as
        # a symlink to a dir holding a record naming a session leader; the
        # removal stop used to SIGTERM that process group.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            planted = base / "planted"
            planted.mkdir(mode=0o700)
            (planted / "worker.lock").write_bytes(self._crafted_lock(process))
            (planted / ("retire-" + "d" * 32)).write_bytes(b"{}")
            semworker._ephemeral_coordination_dir().symlink_to(
                planted, target_is_directory=True)
            self._assert_readers_refuse(process)
            self.assertTrue((planted / "worker.lock").exists())
            self.assertTrue((planted / ("retire-" + "d" * 32)).exists())

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode fixture")
    def test_a_foreign_or_open_namespace_cannot_hand_readers_a_record(
            self) -> None:
        for label, mutate in (
                ("foreign-uid", lambda v: v.__setitem__(
                    stat.ST_UID, v[stat.ST_UID] + 1)),
                ("group-writable", lambda v: v.__setitem__(
                    stat.ST_MODE, (v[stat.ST_MODE] & ~0o777) | 0o775)),
                ("world-writable", lambda v: v.__setitem__(
                    stat.ST_MODE, (v[stat.ST_MODE] & ~0o777) | 0o707))):
            with self.subTest(label=label):
                stack, _data, base = self._fresh_sandbox()
                with stack, self._bystander() as process:
                    namespace = semworker._ephemeral_coordination_dir()
                    namespace.mkdir(mode=0o700)
                    (namespace / "worker.lock").write_bytes(
                        self._crafted_lock(process))
                    real_lstat = os.lstat

                    def hostile(path, *args, **kwargs):
                        info = real_lstat(path, *args, **kwargs)
                        if Path(path) == namespace:
                            values = list(info)
                            mutate(values)
                            return _restat(
                                info, ST_UID=values[stat.ST_UID],
                                ST_MODE=values[stat.ST_MODE])
                        return info

                    with mock.patch.object(
                            semworker.os, "lstat", side_effect=hostile):
                        self._assert_readers_refuse(process)
                    self.assertTrue((namespace / "worker.lock").exists())

    @unittest.skipIf(os.name == "nt", "POSIX process-group fixture")
    def test_a_private_namespace_still_serves_readers_a_record(self) -> None:
        # The gate refuses only hostile entries. Readers and writers agree: an
        # own-uid directory nobody else can write into (0700, 0750, 0755)
        # reads through, as the writer would simply repair its mode.
        for mode in (0o700, 0o750, 0o755):
            with self.subTest(mode=oct(mode)):
                stack, _data, base = self._fresh_sandbox()
                with stack, self._bystander() as process:
                    namespace = semworker._ephemeral_coordination_dir()
                    namespace.mkdir(mode=0o700)
                    (namespace / "worker.lock").write_bytes(
                        self._crafted_lock(process))
                    os.chmod(namespace, mode)
                    inspected = semworker._inspect_worker_lock()
                    self.assertEqual(
                        inspected.state, semworker._WorkerOwnerState.EXACT)
                    self.assertEqual(inspected.record["pid"], process.pid)
                    status = semworker.resident_status()
                    self.assertEqual(status.get("owner_state"), "exact")
                    self.assertNotIn("namespace_blocked", status)

    @unittest.skipIf(os.name == "nt", "POSIX symlink and ownership fixture")
    def test_uninstall_stop_names_a_planted_namespace_and_proceeds(
            self) -> None:
        # No worker of ours can live behind a symlink or a foreign directory
        # (writers refuse them), so a planted entry must not hold removal
        # hostage; the outcome names the path instead of a vague 'blocked'.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            planted = base / "planted"
            planted.mkdir(mode=0o700)
            (planted / "worker.lock").write_bytes(self._crafted_lock(process))
            namespace = semworker._ephemeral_coordination_dir()
            namespace.symlink_to(planted, target_is_directory=True)
            with mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    side_effect=AssertionError("a bystander was terminated")):
                outcome = semworker.stop_worker_and_wait(0.3, 1.0)
            self.assertTrue(outcome["ok"], outcome)
            self.assertEqual(outcome["owner_state"], "namespace-planted")
            self.assertIn(str(namespace), outcome["reason"])
            self.assertIn("remove that entry", outcome["reason"])
            self.assertIsNone(outcome["pid"])
            self.assertIsNone(process.poll())

    @unittest.skipIf(os.name == "nt", "POSIX mode fixture")
    def test_uninstall_stop_reports_a_writable_namespace_with_its_remedy(
            self) -> None:
        stack, _data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            namespace = semworker._ephemeral_coordination_dir()
            namespace.mkdir(mode=0o700)
            (namespace / "worker.lock").write_bytes(self._crafted_lock(process))
            os.chmod(namespace, 0o775)
            with mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    side_effect=AssertionError("a bystander was terminated")):
                outcome = semworker.stop_worker_and_wait(0.3, 1.0)
            self.assertFalse(outcome["ok"])
            self.assertTrue(outcome["blocked"])
            self.assertEqual(outcome["owner_state"], "namespace-blocked")
            self.assertIn(str(namespace), outcome["reason"])
            self.assertIn("remove that directory", outcome["reason"])
            self.assertNotIn("0700", outcome["reason"])
            self.assertIsNone(process.poll())

    @unittest.skipIf(os.name == "nt", "POSIX mode and process-group fixture")
    def test_a_writer_never_retrusts_a_namespace_others_could_write(
            self) -> None:
        # The first writer used to fchmod a 0775 namespace to 0700, after which
        # readers trusted every record planted while it was open; the uninstall
        # stop then killed the bystander the crafted record named.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            namespace = semworker._ephemeral_coordination_dir()
            namespace.mkdir(mode=0o700)
            (namespace / "worker.lock").write_bytes(self._crafted_lock(process))
            os.chmod(namespace, 0o775)
            status = semworker.request_coordination_status()
            self.assertEqual(status["state"], "blocked")
            self.assertFalse(status["writable"])
            self.assertIn("is not private", status["reason"])
            self.assertEqual(
                stat.S_IMODE(os.lstat(namespace).st_mode), 0o775,
                "the writer repaired a namespace holding foreign-era records")
            with self.assertRaises(
                    semworker.ResidentSemanticPreflightUnavailable):
                semworker._acquire_request_slot(time.monotonic() + 0.5)
            self.assertIsNone(semworker._acquire_worker_lock())
            self._assert_readers_refuse(process)
            probe = semworker.diagnostic_query_status(ready=True, timeout_s=0.5)
            self.assertEqual(probe["state"], "namespace-blocked")
            self.assertFalse(probe["coordination"]["writable"])
            # an EMPTY open namespace is ours to repair
            (namespace / "worker.lock").unlink()
            repaired = semworker.request_coordination_status()
            self.assertEqual(repaired["state"], "ready")
            self.assertEqual(stat.S_IMODE(os.lstat(namespace).st_mode), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX ownership fixture")
    def test_a_foreign_owned_record_in_our_namespace_is_never_trusted(
            self) -> None:
        # Defense in depth for hardlinks and records left from a writable
        # window: the record file's own uid must be ours, not just the dir's.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            namespace = semworker._ephemeral_coordination_dir()
            namespace.mkdir(mode=0o700)
            lock = namespace / "worker.lock"
            lock.write_bytes(self._crafted_lock(process))
            (namespace / ("retire-" + "d" * 32)).write_bytes(b"{}")
            real_lstat = os.lstat

            def foreign_file(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path).parent == namespace:
                    return _restat(info, ST_UID=info.st_uid + 1)
                return info

            with mock.patch.object(semworker.os, "lstat", side_effect=foreign_file):
                inspected = semworker._inspect_worker_lock()
                self.assertEqual(
                    inspected.state, semworker._WorkerOwnerState.BLOCKED)
                self.assertIsNone(inspected.record)
                # named, non-retryable, planted: like a symlinked namespace
                self.assertIn(str(lock), inspected.namespace_reason)
                self.assertIn("foreign owner", inspected.namespace_reason)
                self.assertIn("remove it", inspected.namespace_reason)
                self.assertTrue(inspected.planted)
                status = semworker.resident_status()
                self.assertTrue(status["namespace_blocked"])
                self.assertEqual(status["reason"], inspected.namespace_reason)
                for entry in (
                        lambda: semworker.search_worker(
                            "foreign", level="hybrid", k=1, timeout_s=5.0),
                        semworker.acquire_inprocess_owner):
                    with (self._clear_worker_env(),
                          mock.patch.object(semworker.time, "sleep",
                                            side_effect=AssertionError("waited")),
                          mock.patch.object(
                              semworker, "_spawn_worker",
                              side_effect=AssertionError("launched")),
                          self.assertRaises(
                              semworker.ResidentSemanticNamespaceBlocked) as raised):
                        entry()
                    self.assertFalse(surface.semantic_status_retryable(
                        {"reason": str(raised.exception)}))
                self.assertFalse(semworker._retire_handoff_requested("d" * 32))
                semworker._discard_retire_handoff("d" * 32)
                with mock.patch.object(
                        semworker, "_terminate_worker_tree",
                        side_effect=AssertionError("a bystander was terminated")):
                    outcome = semworker.stop_worker_and_wait(0.3, 1.0)
                self.assertTrue(outcome["ok"], outcome)
                self.assertEqual(outcome["owner_state"], "namespace-planted")
                self.assertIn(str(lock), outcome["reason"])
            self.assertTrue(lock.exists())
            self.assertTrue((namespace / ("retire-" + "d" * 32)).exists())
            self.assertIsNone(process.poll())

    @unittest.skipIf(os.name == "nt", "POSIX symlink and real-worker fixture")
    def test_a_live_worker_is_stopped_through_its_descriptor_despite_a_planted_namespace(
            self) -> None:
        # Its namespace replaced by a symlink mid-life, a running worker is
        # still named by the trusted data-dir descriptor: the stop must end it
        # rather than report 'nothing of ours to stop' while it lives on.
        with tempfile.TemporaryDirectory(prefix="agrep-planted-live-") as raw:
            root = Path(raw)
            data = root / "data"
            data.mkdir()
            with (mock.patch.object(common, "DATA_DIR", data),
                  mock.patch.object(
                      semworker, "_coordination_base_path",
                      self.production_coordination_base_path),
                  mock.patch.object(
                      common, "bind_descendants_to_process_lifetime",
                      return_value=False),
                  mock.patch.dict(os.environ, {
                      "AGREP_NO_SEM_WORKER": "",
                      "AGREP_DATA_DIR": str(data)})):
                rec = semworker._ensure_worker(4.0)
                if rec is None:
                    self.skipTest("could not start a resident worker here")
                namespace = semworker._ephemeral_coordination_dir()
                planted = root / "planted"
                try:
                    os.rename(namespace, planted)
                    namespace.symlink_to(planted, target_is_directory=True)
                    self.assertEqual(
                        semworker._observe_coordination_dir().state,
                        semworker._NamespaceState.BLOCKED)
                    outcome = semworker.stop_worker_and_wait(3.0, 2.0)
                    self.assertTrue(outcome["ok"], outcome)
                    self.assertEqual(outcome["pid"], int(rec["pid"]))
                    self.assertFalse(common.pid_alive(int(rec["pid"])))
                    self.assertFalse(semworker.descriptor_path().exists())
                finally:
                    if common.pid_alive(int(rec["pid"])):
                        os.kill(int(rec["pid"]), signal.SIGKILL)
                    if namespace.is_symlink():
                        namespace.unlink()
                    semworker._remove_empty_dir(namespace)

    @unittest.skipIf(os.name == "nt", "POSIX ownership fixture")
    def test_a_foreign_request_record_fails_a_query_at_once(self) -> None:
        # A foreign worker.request used to read as a generic inspect failure:
        # search then ran the read-only fallback child and waited ~27 s on a
        # lease the resident still held, every query, until removed by hand.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._clear_worker_env():
            namespace = semworker._ephemeral_coordination_dir()
            namespace.mkdir(mode=0o700)
            request = namespace / "worker.request"
            request.write_bytes(b'{"pid": 1, "process_start": "x"}')
            real_lstat = os.lstat

            def foreign_request(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if Path(path) == request:
                    return _restat(info, ST_UID=info.st_uid + 1)
                return info

            rec = self._record()
            with (mock.patch.object(semworker.os, "lstat", side_effect=foreign_request),
                  mock.patch.object(semworker, "_ensure_worker", return_value=rec),
                  mock.patch.object(semworker.time, "sleep",
                                    side_effect=AssertionError("waited")),
                  mock.patch.object(
                      semworker.http.client, "HTTPConnection",
                      side_effect=AssertionError("connected without the slot"))):
                with self.assertRaises(
                        semworker.ResidentSemanticNamespaceBlocked) as raised:
                    semworker.search_worker(
                        "foreign request", level="hybrid", k=1, timeout_s=5.0)
                status = semworker.request_coordination_status()
            detail = str(raised.exception)
            self.assertIn(str(request), detail)
            self.assertIn("foreign owner", detail)
            self.assertIn("remove it", detail)
            self.assertFalse(surface.semantic_status_retryable({"reason": detail}))
            self.assertEqual(status["state"], "blocked")
            self.assertFalse(status["writable"])
            self.assertIn(str(request), status["reason"])
            self.assertTrue(request.exists())

    @unittest.skipIf(os.name == "nt", "POSIX symlink and process-group fixture")
    def test_a_forged_descriptor_cannot_steer_the_stop_into_signalling(
            self) -> None:
        # Behind a blocked namespace the descriptor is one record. A forged one
        # naming a bystander whose port never answers must not get that
        # process signalled: the stop reports not completed, with the reason.
        stack, data, base = self._fresh_sandbox()
        with stack, self._bystander() as process:
            planted = base / "planted"
            planted.mkdir(mode=0o700)
            semworker._ephemeral_coordination_dir().symlink_to(
                planted, target_is_directory=True)
            start = common.process_start_identity(process.pid)
            if not start:
                self.skipTest("no process start identity on this host")
            forged = {**self._record(pid=process.pid, process_start=start),
                      "port": 1, "tree_bound": True, "named_job": common.WIN}
            self.assertTrue(semworker._descriptor_compatible(forged))
            semworker.descriptor_path().write_text(
                json.dumps(forged), encoding="utf-8")
            with (mock.patch.object(
                      semworker, "_request_worker_stop", return_value=False) as stop,
                  mock.patch.object(
                      semworker, "_terminate_worker_tree",
                      side_effect=AssertionError("a bystander was signalled")),
                  mock.patch.object(
                      common, "terminate_exact_process_tree",
                      side_effect=AssertionError("a bystander was signalled"))):
                outcome = semworker.stop_worker_and_wait(0.3, 0.3)
            stop.assert_called()
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["owner_state"], "descriptor-unanswered")
            self.assertFalse(outcome["fallback_termination"])
            self.assertFalse(outcome["stop_ack"])
            self.assertIn("did not answer the stop", outcome["reason"])
            self.assertIn("not signalled", outcome["reason"])
            self.assertIsNone(process.poll())
            self.assertTrue(semworker.descriptor_path().exists())

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_a_blocked_namespace_fails_a_query_at_once_with_the_cause(
            self) -> None:
        # A planted namespace used to read as 'ownership is still settling':
        # the query waited ~28 s on a lease that could never clear, then gave
        # a vague failure. It now fails immediately, naming path and remedy.
        stack, _data, base = self._fresh_sandbox()
        with stack, self._clear_worker_env():
            planted = base / "planted"
            planted.mkdir(mode=0o700)
            namespace = semworker._ephemeral_coordination_dir()
            namespace.symlink_to(planted, target_is_directory=True)
            clock = _Clock()
            with (mock.patch.object(
                      semworker.time, "monotonic", side_effect=clock.monotonic),
                  mock.patch.object(
                      semworker.time, "sleep", side_effect=clock.sleep),
                  mock.patch.object(
                      semworker, "_spawn_worker",
                      side_effect=AssertionError("launched into a planted dir"))):
                with self.assertRaises(
                        semworker.ResidentSemanticNamespaceBlocked) as raised:
                    semworker.search_worker(
                        "planted", level="hybrid", k=3, timeout_s=30.0)
                with self.assertRaises(
                        semworker.ResidentSemanticNamespaceBlocked) as inprocess:
                    semworker.acquire_inprocess_owner()
            for exc in (raised.exception, inprocess.exception):
                detail = str(exc)
                self.assertIn("not a private directory", detail)
                self.assertIn(str(namespace), detail)
                self.assertIn("remove that entry", detail)
                self.assertFalse(surface.semantic_status_retryable(
                    {"reason": detail}))
            self.assertEqual(clock.sleeps, [])
            self.assertEqual(clock.now, 0.0)
            status = semworker.resident_status()
            self.assertTrue(status["namespace_blocked"])
            self.assertEqual(status["reason"], str(raised.exception))
            probe = semworker.diagnostic_query_status(ready=True, timeout_s=0.5)
            self.assertEqual(probe["state"], "namespace-blocked")
            self.assertFalse(probe["query_serving"])
            self.assertEqual(probe["reason"], str(raised.exception))
            for row in (
                    doctor._resident_worker_detail(status, readiness=probe),
                    doctor._semantic_readiness_detail({
                        **probe, "current_generation": True})):
                self.assertIn(str(namespace), row)
                self.assertIn("remove that entry", row)
                self.assertNotIn("wait for its current owner", row)
                self.assertNotIn("duplicate model memory", row)

    def test_probe_leaves_no_namespace_after_its_last_observation(self) -> None:
        # The probe's own request slot mints the namespace; the probe must
        # drop it again so doctor --deep leaves nothing behind.
        absent = {"running": False}
        for ready, state in ((True, "query-serving"),
                             (False, "generation-not-ready")):
            with self.subTest(ready=ready):
                status, stop = self._probe_in_sandbox(
                    resident=absent, preexisting=False, ready=ready)
                self.assertEqual(status["state"], state)
                self.assertTrue(status["cleanup"]["namespace_removed"])
                self.assertFalse(status["_namespace_leftover"])
                self.assertNotIn("worker_stopped", status["cleanup"])
                stop.assert_not_called()
                stop.request_stop.assert_not_called()

    def test_probe_never_stops_any_worker_nor_touches_a_shared_namespace(
            self) -> None:
        # The resident worker is shared: one started by a concurrent agent
        # query inside the probe window must survive the probe untouched.
        running = {"running": True, "descriptor_state": "ready", "pid": 42}
        status, stop = self._probe_in_sandbox(
            resident=running, preexisting=True)
        self.assertEqual(status["state"], "query-serving")
        stop.assert_not_called()
        stop.request_stop.assert_not_called()
        self.assertFalse(status["cleanup"]["namespace_removed"])
        self.assertTrue(status["_namespace_leftover"])

    def test_a_concurrent_clients_worker_survives_a_probe(self) -> None:
        # Real worker, real client: an agent query that starts the worker
        # mid-probe keeps it (and its hot model) for its next query.
        absent = {"running": False}
        witnessed: dict = {}

        def agent_query(*_args, **_kwargs):
            rec = semworker._ensure_worker(3.0)
            if rec is None:
                raise semworker.ResidentSemanticPreflightUnavailable(
                    "fixture could not start a resident worker")
            witnessed["pid"] = int(rec["pid"])
            return {"results": []}

        with tempfile.TemporaryDirectory(prefix="agrep-probe-live-") as raw:
            root = Path(raw)
            data = root / "data"
            data.mkdir()
            with (mock.patch.object(common, "DATA_DIR", data),
                  mock.patch.object(
                      semworker, "_coordination_base_path",
                      self.production_coordination_base_path),
                  mock.patch.object(
                      common, "bind_descendants_to_process_lifetime",
                      return_value=False),
                  mock.patch.dict(os.environ, {
                      "AGREP_NO_SEM_WORKER": "",
                      "AGREP_DATA_DIR": str(data)})):
                real_status = semworker.resident_status
                stubbed = [absent]
                try:
                    with (mock.patch.object(
                              semworker, "resident_status",
                              side_effect=lambda **kw: (
                                  stubbed.pop(0) if stubbed
                                  else real_status(**kw))),
                          mock.patch.object(
                              semworker, "loopback_bind_status",
                              return_value={"bindable": True, "reason": None}),
                          mock.patch.object(
                              semworker, "search_worker",
                              side_effect=agent_query)):
                        status = semworker.diagnostic_query_status(
                            ready=True, timeout_s=3.0)
                    if status["state"] != "query-serving":
                        self.skipTest(f"no resident worker here: {status['reason']}")
                    after = status["resident_after"]
                    self.assertTrue(after.get("running"), after)
                    self.assertEqual(after.get("pid"), witnessed["pid"])
                    self.assertTrue(status["alive"])
                    self.assertFalse(status["cleanup"]["namespace_removed"])
                finally:
                    semworker.stop_worker_and_wait(grace_s=3.0, fallback_s=2.0)
                    semworker._remove_empty_dir(
                        semworker._ephemeral_coordination_dir())

    def test_a_namespace_reaped_mid_prepare_is_minted_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-remint-") as raw:
            base = Path(raw)
            namespace = base / "ns"
            real_lstat = os.lstat
            vanished = [False]

            def lstat_once(path, *args, **kwargs):
                if Path(path) == namespace and not vanished[0]:
                    vanished[0] = True
                    os.rmdir(namespace)
                return real_lstat(path, *args, **kwargs)

            with (mock.patch.object(
                      semworker, "_ephemeral_coordination_dir",
                      return_value=namespace),
                  mock.patch.object(semworker.os, "lstat", side_effect=lstat_once)):
                prepared = semworker._prepare_ephemeral_coordination_dir()
            self.assertEqual(prepared, namespace)
            self.assertTrue(namespace.is_dir())
            self.assertTrue(vanished[0])

    def test_one_refusal_retries_same_live_generation_once(self) -> None:
        rec = self._record()
        result = {"results": [], "score_kind": "cosine"}
        descriptor = (rec, self._snapshot("descriptor"))
        with mock.patch.object(
                semworker, "_ensure_worker", return_value=rec), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=descriptor), \
                mock.patch.object(
                    semworker, "_worker_request",
                    side_effect=(None, result)) as request, \
                mock.patch.object(
                    semworker, "_discard_record") as discard, \
                mock.patch.object(
                    semworker, "_request_worker_stop") as stop, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree") as terminate:
            found = semworker.search_worker(
                "transient refusal", level="hybrid", k=3)
        self.assertEqual(found, result)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list], [rec, rec])
        self._assert_generation_untouched(discard, stop, terminate)

    def test_two_preacceptance_refusals_allow_guarded_fallback(self) -> None:
        rec = self._record()
        descriptor = (rec, self._snapshot("descriptor"))
        with mock.patch.object(
                semworker, "_ensure_worker", return_value=rec), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=descriptor), \
                mock.patch.object(
                    semworker, "_worker_request",
                    side_effect=(None, None)) as request, \
                mock.patch.object(
                    semworker, "_discard_record") as discard, \
                mock.patch.object(
                    semworker, "_request_worker_stop") as stop, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree") as terminate:
            with self.assertRaises(
                    semworker.ResidentSemanticPreflightUnavailable):
                semworker.search_worker(
                    "repeated refusal", level="hybrid", k=3)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list], [rec, rec])
        self._assert_generation_untouched(discard, stop, terminate)

    def test_changed_or_gone_generation_allows_replacement_discovery(
            self) -> None:
        old = self._record(
            pid=4242, process_start="old-birth",
            owner_nonce="a" * 32)
        new = self._record(
            pid=5252, process_start="new-birth",
            owner_nonce="c" * 32)
        result = {"results": [{"session": "replacement"}],
                  "score_kind": "cosine"}
        for label, descriptor in (
                ("gone", None),
                ("changed", (new, self._snapshot("replacement", 2)))):
            with self.subTest(label=label):
                calls = []

                def request(target: dict, _payload: dict):
                    calls.append(target)
                    return None if target is old else result

                with mock.patch.object(
                        semworker, "_ensure_worker",
                        side_effect=(old, new)), \
                        mock.patch.object(
                            semworker, "_reconcile_descriptor",
                            return_value=descriptor), \
                        mock.patch.object(
                            semworker, "_worker_request",
                            side_effect=request), \
                        mock.patch.object(
                            semworker, "_terminate_worker_tree") as terminate, \
                        mock.patch.object(
                            semworker, "_request_worker_stop") as stop:
                    found = semworker.search_worker(
                        "replacement", level="hybrid", k=3)
                self.assertEqual(found, result)
                self.assertEqual(calls, [old, new])
                terminate.assert_not_called()
                stop.assert_not_called()

    def test_loopback_connect_deadline_does_not_shorten_query_deadline(
            self) -> None:
        rec = self._record()
        response = mock.Mock(status=200)
        body = b'{"ok":true,"result":{"results":[]}}'
        response.getheader.return_value = str(len(body))
        response.read.return_value = body
        connection = mock.Mock()
        connection.sock = mock.Mock()
        connection.getresponse.return_value = response
        order = []

        def connect():
            order.append("connect")

        def monotonic_ns():
            self.assertEqual(order, ["connect"])
            order.append("deadline")
            return 10_000_000_000

        connection.connect.side_effect = connect
        with mock.patch.object(
                semworker.http.client, "HTTPConnection",
                return_value=connection) as create, \
                mock.patch.object(
                    semworker.time, "monotonic_ns",
                    side_effect=monotonic_ns):
            result = semworker._worker_request(
                rec, {
                    "query": "bounded connect", "level": "hybrid",
                    "k": 3, "filters": {}, "timing": False,
                }, timeout_s=9.0)
        self.assertEqual(result, {"results": []})
        create.assert_called_once_with(
            "127.0.0.1", rec["port"],
            timeout=semworker.CONNECT_TIMEOUT_S)
        socket_budget = connection.sock.settimeout.call_args.args[0]
        self.assertGreater(socket_budget, 8.9)
        self.assertLessEqual(socket_budget, 9.0)
        headers = connection.request.call_args.kwargs["headers"]
        deadline = int(headers[semworker.REQUEST_DEADLINE_HEADER])
        self.assertGreater(deadline, 18_900_000_000)
        self.assertLessEqual(deadline, 19_000_000_000)
        self.assertEqual(order, ["connect", "deadline"])

    def test_http_408_maps_to_semantic_timeout(self) -> None:
        rec = self._record()
        body = b'{"ok":false,"error":"semantic request expired"}'
        response = mock.Mock(status=408)
        response.getheader.return_value = str(len(body))
        response.read.return_value = body
        connection = mock.Mock()
        connection.sock = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(
                semworker.http.client, "HTTPConnection",
                return_value=connection), \
                mock.patch.object(
                    semworker.time, "monotonic_ns",
                    return_value=10_000_000_000):
            with self.assertRaisesRegex(
                    semworker.ResidentSemanticTimeout,
                    "semantic request expired"):
                semworker._worker_request(
                    rec, {
                        "query": "expired request", "level": "hybrid",
                        "k": 3, "filters": {}, "timing": False,
                    }, timeout_s=1.0)

    def test_transport_timeout_retires_the_exact_accepted_generation(self) -> None:
        rec = self._record()
        connection = mock.Mock()
        connection.sock = mock.Mock()
        connection.getresponse.side_effect = TimeoutError("expired")
        # A frozen clock keeps a slow runner from expiring the 0.1 s budget before the request.
        with mock.patch.object(
                semworker.http.client, "HTTPConnection",
                return_value=connection), \
                mock.patch.object(semworker.time, "monotonic", return_value=1_000.0), \
                mock.patch.object(
                    semworker, "_cancel_timed_out_worker",
                    return_value=True) as cancel:
            with self.assertRaises(semworker.ResidentSemanticTimeout):
                semworker._worker_request(
                    rec, {
                        "query": "expired request", "level": "hybrid",
                        "k": 3, "filters": {}, "timing": False,
                    }, timeout_s=0.1)
        cancel.assert_called_once_with(rec)

    def test_timeout_cancellation_is_generation_bound_before_tree_reap(self) -> None:
        rec = self._record()
        before = mock.Mock()
        after = mock.Mock()
        observations = (
            semworker._DescriptorObservation(
                semworker._DescriptorState.READY, rec, before, True),
            semworker._DescriptorObservation(
                semworker._DescriptorState.RECLAIMABLE, rec, after),
        )
        with mock.patch.object(
                semworker, "_inspect_descriptor", side_effect=observations), \
                mock.patch.object(
                    semworker, "_discard_record", return_value=True) as discard, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    return_value=True) as terminate:
            self.assertTrue(semworker._cancel_timed_out_worker(rec))
        terminate.assert_called_once_with(rec, semworker.TIMEOUT_RETIRE_S)
        discard.assert_called_once_with(semworker.descriptor_path(), after)

    def test_failed_timeout_retirement_keeps_worker_discoverable(self) -> None:
        rec = self._record()
        inspected = semworker._DescriptorObservation(
            semworker._DescriptorState.READY, rec, mock.Mock(), True)
        with mock.patch.object(
                semworker, "_inspect_descriptor", return_value=inspected), \
                mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    return_value=False) as terminate, \
                mock.patch.object(semworker, "_discard_record") as discard:
            self.assertFalse(semworker._cancel_timed_out_worker(rec))
        terminate.assert_called_once_with(rec, semworker.TIMEOUT_RETIRE_S)
        discard.assert_not_called()

    def test_queued_timeout_never_connects_or_retires_worker(self) -> None:
        rec = self._record()
        holder = semworker._acquire_request_slot(time.monotonic() + 1.0)
        if holder is None:
            self.skipTest("request serialization owner is unavailable")
        outcome = []

        def request() -> None:
            try:
                semworker._worker_request(
                    rec, {
                        "query": "queued", "level": "hybrid",
                        "k": 3, "filters": {}, "timing": False,
                    }, timeout_s=0.03)
            except BaseException as exc:
                outcome.append(exc)

        import threading
        thread = threading.Thread(target=request)
        try:
            with mock.patch.object(
                    semworker.http.client, "HTTPConnection") as connect, \
                    mock.patch.object(
                        semworker, "_cancel_timed_out_worker") as cancel:
                thread.start()
                thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], semworker.ResidentSemanticTimeout)
            self.assertIsInstance(
                outcome[0], semworker.ResidentSemanticPreflightUnavailable)
            connect.assert_not_called()
            cancel.assert_not_called()
        finally:
            semworker._release_request_slot(holder)

    def test_expiry_after_slot_acquisition_releases_the_slot(self) -> None:
        rec = self._record()
        slot = mock.Mock()
        with mock.patch.object(
                semworker, "_acquire_request_slot", return_value=slot), \
                mock.patch.object(
                    semworker.time, "monotonic", side_effect=(0.0, 2.0)), \
                mock.patch.object(
                    semworker, "_release_request_slot") as release, \
                mock.patch.object(
                    semworker.http.client, "HTTPConnection") as connect:
            with self.assertRaises(
                    semworker.ResidentSemanticPreflightTimeout):
                semworker._worker_request(
                    rec, {
                        "query": "expired", "level": "hybrid",
                        "k": 3, "filters": {}, "timing": False,
                    }, timeout_s=1.0)
        release.assert_called_once_with(slot)
        connect.assert_not_called()

    def test_automatic_deadline_is_one_budget_across_start_and_request(self) -> None:
        rec = self._record()
        clock = _Clock()
        request_budgets = []

        def start(_budget):
            clock.now += 0.3
            return rec

        def request(_record, _payload, budget):
            request_budgets.append(budget)
            return {"results": []}

        with mock.patch.object(semworker, "_ensure_worker", side_effect=start), \
                mock.patch.object(semworker, "_worker_request", side_effect=request), \
                mock.patch.object(semworker.time, "monotonic", side_effect=clock.monotonic):
            result = semworker.search_worker(
                "one budget", level="hybrid", k=3,
                timeout_s=1.0, start_timeout_s=0.5)
        self.assertEqual(result, {"results": []})
        self.assertEqual(len(request_budgets), 1)
        self.assertAlmostEqual(request_budgets[0], 0.7)

    def test_expired_response_is_terminal_and_never_retried(self) -> None:
        rec = self._record()
        timeout = semworker.ResidentSemanticTimeout(
            "semantic request expired")
        with mock.patch.object(
                semworker, "_ensure_worker", return_value=rec), \
                mock.patch.object(
                    semworker, "_worker_request",
                    side_effect=timeout) as request, \
                mock.patch.object(
                    semworker, "_reconcile_descriptor") as reconcile:
            with self.assertRaises(semworker.ResidentSemanticTimeout):
                semworker.search_worker(
                    "expired request", level="hybrid", k=3)
        request.assert_called_once()
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
