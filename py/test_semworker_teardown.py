"""Exact-owner and real process-tree tests for semantic worker teardown."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import ownerfile  # noqa: E402
import semworker  # noqa: E402
import winjob  # noqa: E402


def _snapshot(label: str, inode: int) -> ownerfile.Snapshot:
    raw = label.encode("ascii")
    return ownerfile.Snapshot(
        (1, inode, len(raw), inode * 1000), float(inode), raw)


def _owner(
        record: dict, snapshot: ownerfile.Snapshot) -> semworker._WorkerOwner:
    return semworker._WorkerOwner(
        semworker._WorkerOwnerState.EXACT, record, snapshot)


class SemanticWorkerTeardownUnitTests(unittest.TestCase):
    def tearDown(self) -> None:
        for path in (
                semworker.descriptor_path(),
                semworker.worker_lock_path(),
                semworker.start_claim_path()):
            path.unlink(missing_ok=True)

    def test_unverifiable_stop_wait_neither_succeeds_nor_signals(self) -> None:
        rec = {"pid": 41, "process_start": "birth"}
        now = iter((0.0, 0.0, 0.1))
        with mock.patch.object(
                semworker, "_process_owner",
                return_value=ownerfile.ProcessOwner.UNVERIFIABLE), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=lambda: next(now)), \
                mock.patch.object(semworker.time, "sleep") as sleep, \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            exited, descriptor, acknowledged = semworker._request_and_drain_worker(
                rec, None, 0.05)
        self.assertFalse(exited)
        self.assertIsNone(descriptor)
        self.assertFalse(acknowledged)
        sleep.assert_called_once_with(0.02)
        stop.assert_not_called()
        terminate.assert_not_called()

    def test_unverifiable_stop_wait_can_observe_later_death(self) -> None:
        rec = {"pid": 42, "process_start": "birth"}
        with mock.patch.object(
                semworker, "_process_owner",
                side_effect=(
                    ownerfile.ProcessOwner.UNVERIFIABLE,
                    ownerfile.ProcessOwner.DEAD)), \
                mock.patch.object(
                    semworker.time, "monotonic", return_value=0.0), \
                mock.patch.object(semworker.time, "sleep") as sleep:
            exited, descriptor, acknowledged = semworker._request_and_drain_worker(
                rec, None, 1.0)
        self.assertTrue(exited)
        self.assertIsNone(descriptor)
        self.assertFalse(acknowledged)
        sleep.assert_called_once_with(0.02)

    def test_dead_bound_owner_waits_for_its_process_group(self) -> None:
        rec = {
            "pid": 42, "process_start": "birth", "tree_bound": True,
        }
        now = iter((0.0, 0.0, 0.1))
        with mock.patch.object(common, "WIN", False), \
                mock.patch.object(
                    semworker, "_process_owner",
                    return_value=ownerfile.ProcessOwner.DEAD), \
                mock.patch.object(
                    common, "_process_group_active", return_value=True), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=lambda: next(now)), \
                mock.patch.object(semworker.time, "sleep") as sleep:
            exited, descriptor, acknowledged = semworker._request_and_drain_worker(
                rec, None, 0.05)
        self.assertFalse(exited)
        self.assertIsNone(descriptor)
        self.assertFalse(acknowledged)
        sleep.assert_called_once_with(0.02)

    def test_stop_wait_rejects_another_generation_descriptor(self) -> None:
        lock = {
            "pid": 42, "process_start": "birth",
            "tree_bound": True, "nonce": "a" * 32,
        }
        descriptor = {
            "version": semworker.PROTOCOL,
            "pid": 42, "process_start": "birth",
            "tree_bound": True, "owner_nonce": "b" * 32,
        }
        observed = _snapshot("other-descriptor", 13)
        with mock.patch.object(
                semworker, "_process_owner",
                side_effect=(
                    ownerfile.ProcessOwner.EXACT_LIVE,
                    ownerfile.ProcessOwner.DEAD)), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=(descriptor, observed)), \
                mock.patch.object(
                    semworker.time, "monotonic", return_value=0.0), \
                mock.patch.object(semworker.time, "sleep"), \
                mock.patch.object(semworker, "_request_worker_stop") as stop:
            exited, found, acknowledged = semworker._request_and_drain_worker(
                lock, None, 1.0)
        self.assertTrue(exited)
        self.assertIsNone(found)
        self.assertFalse(acknowledged)
        stop.assert_not_called()

    def test_windows_named_tree_never_falls_back_to_root_death(self) -> None:
        rec = {
            "pid": 42, "process_start": "win_123",
            "tree_bound": True, "named_job": True,
        }
        descriptor = (rec, _snapshot("descriptor", 14))
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(
                    winjob, "open_exact", return_value=None), \
                mock.patch.object(
                    semworker, "_request_worker_stop") as stop, \
                mock.patch.object(
                    semworker, "_process_owner",
                    return_value=ownerfile.ProcessOwner.DEAD):
            exited, found, acknowledged = semworker._request_and_drain_worker(
                rec, descriptor, 1.0)
        self.assertFalse(exited)
        self.assertIs(found, descriptor)
        self.assertFalse(acknowledged)
        stop.assert_not_called()

    def test_windows_stop_ack_drains_the_preopened_tree(self) -> None:
        rec = {
            "pid": 42, "process_start": "win_123",
            "tree_bound": True, "named_job": True,
        }
        descriptor = (rec, _snapshot("descriptor", 15))
        tree = mock.Mock()
        tree.terminate_and_wait.return_value = True
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(
                    winjob, "open_exact", return_value=tree) as opened, \
                mock.patch.object(
                    semworker, "_request_worker_stop", return_value=True), \
                mock.patch.object(
                    semworker, "_terminate_worker_tree") as fallback:
            exited, found, acknowledged = semworker._request_and_drain_worker(
                rec, descriptor, 1.0)
        self.assertTrue(exited)
        self.assertIs(found, descriptor)
        self.assertTrue(acknowledged)
        opened.assert_called_once_with(42, "win_123")
        tree.terminate_and_wait.assert_called_once()
        tree.close.assert_called_once_with()
        fallback.assert_not_called()

    def test_windows_legacy_tree_is_protected_without_signalling(self) -> None:
        rec = {
            "version": semworker.PROTOCOL - 1,
            "pid": 42, "process_start": "win_123",
            "tree_bound": True, "named_job": False,
            "port": 9, "token": "a" * 64, "started_at": 1.0,
        }
        owner_rec = {
            "pid": 42, "process_start": "win_123",
            "tree_bound": True, "named_job": False,
            "nonce": "b" * 32,
        }
        descriptor = _snapshot("legacy-descriptor", 16)
        lock = _snapshot("legacy-owner", 17)
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=(rec, descriptor)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    return_value=_owner(owner_rec, lock)), \
                mock.patch.object(
                    semworker, "_request_worker_stop") as stop, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree") as terminate:
            result = semworker._stop_worker_under_start_claim(
                1.0, 1.0, time.monotonic() + 2.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["owner_state"], "legacy-tree")
        stop.assert_not_called()
        terminate.assert_not_called()

    def test_windows_incompatible_legacy_descriptor_follows_ownership(
            self) -> None:
        rec = {
            "version": semworker.PROTOCOL - 1,
            "pid": 42, "process_start": "win_123",
            "tree_bound": True, "named_job": False,
        }
        observed = _snapshot("legacy-descriptor", 18)
        for bound, discard_count in ((True, 0), (False, 1)):
            with self.subTest(bound=bound), \
                    mock.patch.object(common, "WIN", True), \
                    mock.patch.object(
                        semworker, "_discard_record") as discard, \
                    mock.patch.object(
                        semworker, "_request_worker_stop") as stop, \
                    mock.patch.object(
                        semworker, "_terminate_worker_tree") as terminate:
                semworker._retire_incompatible(
                    rec, observed, bound_owner=bound)
            self.assertEqual(discard.call_count, discard_count)
            stop.assert_not_called()
            terminate.assert_not_called()

    def test_unverifiable_owner_blocks_stop_without_any_signal(self) -> None:
        inspected = semworker._WorkerOwner(
            semworker._WorkerOwnerState.UNVERIFIABLE,
            {"pid": 43, "process_start": "birth", "tree_bound": True},
            _snapshot("owner", 1))
        with mock.patch.object(
                semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    return_value=inspected), \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate, \
                mock.patch.object(semworker, "_discard_record") as discard:
            result = semworker.stop_worker_and_wait(0.0, 0.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["owner_state"], "unverifiable")
        stop.assert_not_called()
        terminate.assert_not_called()
        discard.assert_not_called()

    def test_stop_reports_an_orphaned_group_after_owner_exit(self) -> None:
        rec = {
            "version": semworker.PROTOCOL,
            "pid": 44, "process_start": "birth", "tree_bound": True,
            "named_job": False, "owner_nonce": "a" * 32,
            "port": 9, "token": "b" * 64, "started_at": 1.0,
        }
        owner_rec = {
            "pid": 44, "process_start": "birth", "tree_bound": True,
            "named_job": False, "nonce": "a" * 32,
        }
        descriptor = _snapshot("descriptor", 20)
        lock = _snapshot("owner", 21)
        orphaned = semworker._WorkerOwner(
            semworker._WorkerOwnerState.ORPHANED_GROUP, owner_rec, lock)
        with mock.patch.object(common, "WIN", False), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=(rec, descriptor)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(_owner(owner_rec, lock), orphaned)), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    return_value=(False, (rec, descriptor), False)), \
                mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    return_value=False), \
                mock.patch.object(semworker, "_discard_record") as discard:
            result = semworker._stop_worker_under_start_claim(
                0.0, 0.0, time.monotonic() + 1.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["running"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["owner_state"], "orphaned-group")
        discard.assert_not_called()

    def test_acknowledged_exact_exit_cleans_descriptor_and_owner(self) -> None:
        rec = {
            "version": semworker.PROTOCOL,
            "pid": 44, "process_start": "birth", "tree_bound": True,
            "named_job": common.WIN,
            "owner_nonce": "a" * 32, "port": 9,
            "token": "b" * 64, "started_at": 1.0,
        }
        owner_rec = {
            "pid": 44, "process_start": "birth", "tree_bound": True,
            "named_job": common.WIN,
            "nonce": "a" * 32,
        }
        descriptor = _snapshot("descriptor", 2)
        lock = _snapshot("owner", 3)
        absent = semworker._WorkerOwner(semworker._WorkerOwnerState.ABSENT)
        with mock.patch.object(
                semworker, "_reconcile_descriptor",
                side_effect=((rec, descriptor), None)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(_owner(owner_rec, lock), absent)), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    return_value=(True, (rec, descriptor), True)) as wait, \
                mock.patch.object(
                    semworker, "_discard_record",
                    return_value=True) as discard_descriptor, \
                mock.patch.object(
                    semworker, "_discard_worker_record",
                    return_value=True) as discard_owner, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            result = semworker.stop_worker_and_wait()
        self.assertTrue(result["ok"])
        self.assertTrue(result["stop_ack"])
        self.assertFalse(result["fallback_termination"])
        wait.assert_called_once_with(rec, (rec, descriptor), 5.0)
        discard_descriptor.assert_called_once_with(
            semworker.descriptor_path(), descriptor)
        discard_owner.assert_called_once_with(
            semworker.worker_lock_path(), lock)
        terminate.assert_not_called()

    def test_stop_settles_a_final_replacement_before_success(self) -> None:
        old = {
            "version": semworker.PROTOCOL,
            "pid": 45, "process_start": "old", "tree_bound": True,
            "named_job": common.WIN,
            "owner_nonce": "a" * 32, "port": 9,
            "token": "c" * 64, "started_at": 1.0,
        }
        new = {
            "version": semworker.PROTOCOL,
            "pid": 46, "process_start": "new", "tree_bound": True,
            "named_job": common.WIN,
            "owner_nonce": "b" * 32, "port": 10,
            "token": "d" * 64, "started_at": 2.0,
        }
        old_owner = {
            "pid": 45, "process_start": "old", "tree_bound": True,
            "named_job": common.WIN,
            "nonce": "a" * 32,
        }
        new_owner = {
            "pid": 46, "process_start": "new", "tree_bound": True,
            "named_job": common.WIN,
            "nonce": "b" * 32,
        }
        old_descriptor = _snapshot("old-descriptor", 4)
        old_lock = _snapshot("old-owner", 5)
        new_descriptor = _snapshot("new-descriptor", 6)
        new_lock = _snapshot("new-owner", 7)
        absent = semworker._WorkerOwner(semworker._WorkerOwnerState.ABSENT)
        with mock.patch.object(
                semworker, "_reconcile_descriptor",
                side_effect=(
                    (old, old_descriptor), (new, new_descriptor), None)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(
                        _owner(old_owner, old_lock),
                        _owner(new_owner, new_lock), absent)), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    side_effect=(
                        (True, (old, old_descriptor), True),
                        (True, (new, new_descriptor), True))) as wait, \
                mock.patch.object(
                    semworker, "_discard_record",
                    return_value=True) as discard_descriptor, \
                mock.patch.object(
                    semworker, "_discard_worker_record",
                    return_value=True) as discard_owner:
            result = semworker.stop_worker_and_wait()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 45)
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(discard_descriptor.call_args_list, [
            mock.call(semworker.descriptor_path(), old_descriptor),
            mock.call(semworker.descriptor_path(), new_descriptor),
        ])
        self.assertEqual(discard_owner.call_args_list, [
            mock.call(semworker.worker_lock_path(), old_lock),
            mock.call(semworker.worker_lock_path(), new_lock),
        ])

    def test_stop_never_removes_another_generation_owner(self) -> None:
        rec = {
            "version": semworker.PROTOCOL,
            "pid": 46, "process_start": "birth", "tree_bound": True,
            "named_job": common.WIN,
            "owner_nonce": "a" * 32,
        }
        other = {
            "pid": 46, "process_start": "birth", "tree_bound": True,
            "named_job": common.WIN,
            "nonce": "b" * 32,
        }
        descriptor = _snapshot("descriptor", 14)
        other_lock = _snapshot("other-owner", 15)
        blocked = semworker._WorkerOwner(
            semworker._WorkerOwnerState.UNVERIFIABLE, other, other_lock)
        with mock.patch.object(
                semworker, "_reconcile_descriptor",
                side_effect=((rec, descriptor), None)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(_owner(other, other_lock), blocked)), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    return_value=(True, (rec, descriptor), True)), \
                mock.patch.object(
                    semworker, "_discard_record",
                    return_value=True) as discard, \
                mock.patch.object(
                    semworker, "_discard_worker_record") as discard_owner:
            result = semworker.stop_worker_and_wait()
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        discard.assert_called_once_with(
            semworker.descriptor_path(), descriptor)
        discard_owner.assert_not_called()

    def test_inprocess_holder_is_polled_and_never_signalled(self) -> None:
        rec = {"pid": 47, "process_start": "birth", "tree_bound": False}
        lock = _snapshot("inprocess", 8)
        absent = semworker._WorkerOwner(semworker._WorkerOwnerState.ABSENT)
        with mock.patch.object(
                semworker, "_reconcile_descriptor", side_effect=(None, None)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(_owner(rec, lock), absent)), \
                mock.patch.object(
                    semworker, "_verify_record",
                    side_effect=(None, ownerfile.OwnershipLost("released"))) as verify, \
                mock.patch.object(
                    semworker.time, "monotonic", return_value=0.0), \
                mock.patch.object(semworker.time, "sleep") as sleep, \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(
                    semworker, "_request_and_drain_worker") as request_and_drain, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            result = semworker.stop_worker_and_wait(1.0, 1.0)
        self.assertTrue(result["ok"])
        self.assertEqual(verify.call_count, 2)
        sleep.assert_called_once_with(0.02)
        stop.assert_not_called()
        request_and_drain.assert_not_called()
        terminate.assert_not_called()

    def test_tree_termination_requires_a_verified_boundary(self) -> None:
        with mock.patch.object(
                common, "terminate_exact_process_tree",
                return_value=True) as terminate:
            for tree_bound in (None, False):
                with self.subTest(tree_bound=tree_bound):
                    rec = {
                        "pid": 48, "process_start": "birth",
                        "tree_bound": tree_bound,
                    }
                    self.assertFalse(semworker._terminate_worker_tree(rec, 1.5))
            self.assertTrue(semworker._terminate_worker_tree({
                "pid": 48, "process_start": "birth", "tree_bound": True,
                "named_job": common.WIN,
            }, 1.5))
        expected = {"wait_s": 1.5, "require_bound_tree": True}
        terminate.assert_called_once_with(48, "birth", **expected)

    def test_incompatible_worker_requires_matching_exact_owner(self) -> None:
        rec = {
            "pid": 49, "process_start": "birth", "tree_bound": True,
            "port": 9, "token": "a" * 64, "owner_nonce": "b" * 32,
        }
        observed = _snapshot("incompatible", 9)
        with mock.patch.object(
                semworker, "_discard_record", return_value=True) as discard, \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_process_owner") as classify, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            semworker._retire_incompatible(
                rec, observed, bound_owner=False)
        discard.assert_called_once_with(semworker.descriptor_path(), observed)
        stop.assert_not_called()
        classify.assert_not_called()
        terminate.assert_not_called()

    def test_incompatible_descriptor_cannot_retire_another_generation(self) -> None:
        rec = {
            "version": semworker.PROTOCOL,
            "pid": 49, "port": 9, "token": "a" * 64,
            "process_start": "birth", "started_at": time.time(),
            "tree_bound": True, "owner_nonce": "b" * 32,
            "build_id": "old-build",
            "capabilities": list(semworker.CAPABILITIES),
        }
        raw = json.dumps(rec).encode("utf-8")
        descriptor = ownerfile.Snapshot(
            (1, 11, len(raw), 11_000), 11.0, raw)
        lock = {
            "pid": 49, "process_start": "birth",
            "tree_bound": True, "nonce": "c" * 32,
        }
        with mock.patch.object(
                ownerfile, "snapshot", return_value=descriptor), \
                mock.patch.object(
                    ownerfile, "classify_process",
                    return_value=ownerfile.ProcessOwner.EXACT_LIVE), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    return_value=_owner(lock, _snapshot("other-owner", 12))), \
                mock.patch.object(
                    semworker, "_discard_record", return_value=True), \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_process_owner") as classify, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            self.assertIsNone(semworker._reconcile_descriptor())
        stop.assert_not_called()
        classify.assert_not_called()
        terminate.assert_not_called()

    def test_worker_generation_comparison_includes_nonce(self) -> None:
        rec = {
            "version": semworker.PROTOCOL,
            "pid": 50, "port": 1234, "token": "c" * 64,
            "process_start": "birth", "build_id": "build",
            "owner_nonce": "a" * 32,
        }
        self.assertTrue(semworker._same_worker_generation(rec, dict(rec)))
        for key, value in (
                ("version", semworker.PROTOCOL + 1),
                ("pid", 51),
                ("port", 1235),
                ("token", "d" * 64),
                ("process_start", "other-birth"),
                ("build_id", "other-build"),
                ("owner_nonce", "b" * 32)):
            with self.subTest(key=key):
                changed = {**rec, key: value}
                self.assertFalse(
                    semworker._same_worker_generation(rec, changed))

    def test_unqueried_worker_release_does_not_import_semantic_stack(self) -> None:
        server = object.__new__(semworker.SemanticWorkerServer)
        server._release_lock = threading.Lock()
        server._resources_released = False
        server._semantic_loaded = False
        semantic_module = mock.Mock()
        with mock.patch.dict(sys.modules, {"semantic": semantic_module}):
            self.assertTrue(server._release_resources())
        semantic_module.release.assert_not_called()

    def test_loaded_worker_requires_release_confirmation(self) -> None:
        server = object.__new__(semworker.SemanticWorkerServer)
        server._release_lock = threading.Lock()
        server._resources_released = False
        server._semantic_loaded = True
        semantic_module = mock.Mock()
        semantic_module.release.return_value = False
        with mock.patch.dict(sys.modules, {"semantic": semantic_module}):
            self.assertFalse(server._release_resources())
        self.assertFalse(server._resources_released)


@unittest.skipIf(os.name == "nt", "POSIX private process groups")
class PosixSemanticWorkerTeardownTests(unittest.TestCase):
    def test_real_private_group_drains_semantic_descendant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semworker-tree-") as raw:
            ready = Path(raw) / "child.pid"
            child = (
                "import os,pathlib,signal,sys,time\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "while True: time.sleep(1)\n"
            )
            parent = (
                "import os,sys,time\n"
                "if os.fork() == 0:\n"
                " os.execv(sys.executable,[sys.executable,'-c',sys.argv[1],sys.argv[2]])\n"
                "while True: time.sleep(1)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent, child, str(ready)],
                start_new_session=True)
            child_pid = None
            waiter = threading.Thread(target=process.wait, daemon=True)
            try:
                deadline = time.monotonic() + 5.0
                while not ready.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), f"fixture exited {process.poll()}")
                child_pid = int(ready.read_text(encoding="ascii"))
                birth = common.process_start_identity(process.pid)
                self.assertIsNotNone(birth)
                waiter.start()
                self.assertTrue(semworker._terminate_worker_tree({
                    "pid": process.pid,
                    "process_start": str(birth),
                    "tree_bound": True,
                }, 5.0))
                waiter.join(timeout=2.0)
                self.assertIs(
                    common._process_group_has_live_members(process.pid),
                    False)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                if child_pid is not None and common.pid_alive(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass


@unittest.skipUnless(sys.platform == "win32", "Windows Job Object lifetime")
class WindowsSemanticWorkerTeardownTests(unittest.TestCase):
    def tearDown(self) -> None:
        for path in (
                semworker.descriptor_path(),
                semworker.worker_lock_path(),
                semworker.start_claim_path()):
            path.unlink(missing_ok=True)

    def test_product_worker_stop_releases_mapped_descendant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-semworker-job-") as raw:
            root = Path(raw)
            mapped = root / "mapped.bin"
            ready = root / "ready.txt"
            mapped.write_bytes(b"x" * 4096)
            child = (
                "import mmap,sys,time\n"
                "from pathlib import Path\n"
                "stream=open(sys.argv[1],'r+b')\n"
                "mapping=mmap.mmap(stream.fileno(),0)\n"
                "Path(sys.argv[2]).write_text('ready',encoding='utf-8')\n"
                "while True: time.sleep(1)\n"
            )
            worker = (
                "import common,semworker,subprocess,sys\n"
                "owner=semworker.acquire_resident_owner()\n"
                "if owner is None: raise SystemExit(4)\n"
                "server=semworker.SemanticWorkerServer("
                "search_fn=lambda *a,**k:{'results':[]},"
                "lifetime=owner)\n"
                "subprocess.Popen([sys.executable,'-c',sys.argv[1],"
                "sys.argv[2],sys.argv[3]],"
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "server.serve()\n"
                "owner.close()\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", worker, child, str(mapped), str(ready)],
                cwd=Path(__file__).resolve().parent,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 8.0
                while not ready.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), f"worker fixture exited {process.poll()}")
                result = semworker.stop_worker_and_wait(5.0, 3.0)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["stop_ack"], result)
                self.assertFalse(result["fallback_termination"], result)
                self.assertIsNotNone(process.poll(), result)
                self.assertFalse(semworker.descriptor_path().exists())
                self.assertFalse(semworker.worker_lock_path().exists())
                mapped.unlink()
                self.assertFalse(mapped.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
