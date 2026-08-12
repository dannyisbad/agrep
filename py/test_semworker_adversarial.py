from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import signal
import stat
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
import indexd_runtime  # noqa: E402

# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)
import ownerfile  # noqa: E402
import semworker  # noqa: E402


PY_DIR = Path(__file__).resolve().parent


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class SemanticWorkerAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="agrep-semworker-adversarial-")
        self.root = Path(self.temp.name)
        self.saved_data_dir = common.DATA_DIR
        common.DATA_DIR = self.root
        self.coordination = mock.patch.object(
            semworker, "_coordination_base_path", return_value=self.root)
        self.coordination.start()
        self.handles: list[ownerfile.Handle] = []

    def tearDown(self) -> None:
        for handle in self.handles:
            try:
                handle.close()
            except OSError:
                pass
        self.coordination.stop()
        common.DATA_DIR = self.saved_data_dir
        self.temp.cleanup()

    @staticmethod
    def _snapshot(
            raw: bytes = b"record", identity: int = 1,
            mtime: float = 100.0) -> ownerfile.Snapshot:
        return ownerfile.Snapshot(
            (1, identity, len(raw), identity * 1000), mtime, raw)

    @staticmethod
    def _descriptor(
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
            "named_job": common.WIN,
            "owner_nonce": owner_nonce,
        }

    @staticmethod
    def _owner_record(
            *, pid: int = 4242, process_start: str = "birth",
            nonce: str = "a" * 32, tree_bound: bool = True) -> dict:
        return {
            "pid": pid,
            "started_at": 100.0,
            "process_start": process_start,
            "nonce": nonce,
            "tree_bound": tree_bound,
            "named_job": bool(tree_bound and common.WIN),
        }

    @classmethod
    def _owner(cls, record: dict, identity: int = 1):
        return semworker._WorkerOwner(
            semworker._WorkerOwnerState.EXACT,
            record,
            cls._snapshot(b"owner", identity))

    def _write_descriptor(
            self, raw: bytes, *, age: float = 0.0,
            now: float = 100.0) -> ownerfile.Snapshot:
        path = semworker.descriptor_path()
        path.write_bytes(raw)
        changed = now - age
        os.utime(path, (changed, changed))
        return ownerfile.snapshot(path)

    @staticmethod
    def _stop_with_self_birth(
            grace_s: float = 0.0, fallback_s: float = 0.0) -> dict:
        with mock.patch.object(
                common, "process_start_identity",
                return_value="self-birth"):
            return semworker.stop_worker_and_wait(grace_s, fallback_s)

    def test_stop_never_succeeds_through_a_live_peer_start_claim(self) -> None:
        clock = _Clock()
        with mock.patch.object(
                semworker, "_acquire_start_claim", return_value=None), \
                mock.patch.object(
                    semworker, "_start_claim_active", return_value=True), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=clock.monotonic), \
                mock.patch.object(
                    semworker.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(
                    semworker, "_stop_worker_under_start_claim") as scan:
            result = semworker.stop_worker_and_wait(0.04, 0.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["owner_state"], "start-claim")
        self.assertGreaterEqual(clock.now, 0.04)
        scan.assert_not_called()

    def test_nonfinite_stop_budgets_fail_closed_without_waiting(self) -> None:
        with mock.patch.object(
                semworker, "_acquire_start_claim", return_value=None), \
                mock.patch.object(
                    semworker, "_start_claim_active", return_value=True), \
                mock.patch.object(
                    semworker.time, "monotonic", return_value=10.0), \
                mock.patch.object(semworker.time, "sleep") as sleep:
            result = semworker.stop_worker_and_wait(
                float("inf"), float("nan"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["owner_state"], "start-claim")
        sleep.assert_not_called()

    def test_stop_acquires_peer_claim_then_rescans_before_release(self) -> None:
        clock = _Clock()
        claim = mock.Mock()
        order = []
        claim.verify.side_effect = lambda **_kwargs: order.append("verify")

        def scan(_grace: float, _force: float, _deadline: float) -> dict:
            order.append("scan")
            return {"ok": True, "running": False}

        def release(_claim) -> None:
            order.append("release")

        with mock.patch.object(
                semworker, "_acquire_start_claim",
                side_effect=(None, claim)), \
                mock.patch.object(
                    semworker, "_start_claim_active", return_value=True), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=clock.monotonic), \
                mock.patch.object(
                    semworker.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(
                    semworker, "_stop_worker_under_start_claim",
                    side_effect=scan) as rescan, \
                mock.patch.object(
                    semworker, "_release_start_claim",
                    side_effect=release):
            result = semworker.stop_worker_and_wait(0.1, 0.1)
        self.assertTrue(result["ok"])
        self.assertEqual(order, ["verify", "scan", "release"])
        rescan.assert_called_once()
        self.assertEqual(clock.sleeps, [0.02])

    def test_fresh_malformed_descriptor_blocks_status_and_stop(self) -> None:
        original = b"[]"
        self._write_descriptor(original, age=0.0)
        with mock.patch.object(semworker.time, "time", return_value=100.0):
            status = semworker.resident_status()
            result = self._stop_with_self_birth()
        self.assertTrue(status["blocked"])
        self.assertTrue(status["descriptor_blocked"])
        self.assertEqual(
            status["owner_state"], "descriptor-malformed-fresh")
        self.assertEqual(
            status["reason"],
            "the semantic endpoint record is still being published")
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["owner_state"], "descriptor-blocked")
        self.assertEqual(semworker.descriptor_path().read_bytes(), original)

    def test_fresh_malformed_descriptor_never_spawns_a_worker(self) -> None:
        semworker.descriptor_path().write_bytes(b"[]")
        with mock.patch.object(
                common, "process_start_identity",
                return_value="self-birth"), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker())
        spawn.assert_not_called()

    def test_descriptor_published_during_discovery_is_used(self) -> None:
        record = self._descriptor()
        descriptor = (record, self._snapshot(b"descriptor"))
        with mock.patch.object(
                semworker, "_reconcile_descriptor",
                side_effect=(None, descriptor)) as read, \
                mock.patch.object(
                    semworker, "_descriptor_entry_present",
                    return_value=True), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIs(semworker._ensure_worker(), record)
        self.assertEqual(read.call_count, 2)
        spawn.assert_not_called()

    def test_blocked_descriptor_does_not_report_an_active_owner_mode(self) -> None:
        owner = semworker._acquire_worker_lock(tree_bound=False)
        if owner is None:
            self.skipTest("process birth identity is unavailable")
        self.handles.append(owner)
        semworker.descriptor_path().write_bytes(b"[]")
        status = semworker.resident_status()
        self.assertTrue(status["blocked"])
        self.assertFalse(status["starting"])
        self.assertFalse(status["inprocess"])
        self.assertEqual(
            status["owner_state"], "descriptor-malformed-fresh")
        self.assertEqual(
            status["reason"],
            "the semantic endpoint record is still being published")

    def test_stale_malformed_descriptor_is_exact_snapshot_reaped(self) -> None:
        expected = self._write_descriptor(b"[]", age=6.001)
        discard_exact = semworker._discard_record
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(
                    semworker, "_discard_record",
                    wraps=discard_exact) as discard:
            result = self._stop_with_self_birth()
        self.assertTrue(result["ok"])
        self.assertFalse(semworker.descriptor_path().exists())
        discard.assert_called_once_with(
            semworker.descriptor_path(), expected)

    def test_status_is_read_only_for_a_reclaimable_descriptor(self) -> None:
        self._write_descriptor(b"[]", age=6.001)
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(semworker, "_discard_record") as discard, \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            status = semworker.resident_status()
        self.assertFalse(status["running"])
        self.assertFalse(status["blocked"])
        self.assertEqual(status["descriptor_state"], "malformed-stale")
        self.assertTrue(semworker.descriptor_path().exists())
        discard.assert_not_called()
        stop.assert_not_called()
        terminate.assert_not_called()

    def test_status_never_retires_an_incompatible_live_worker(self) -> None:
        owner = semworker._acquire_worker_lock(tree_bound=True)
        if owner is None:
            self.skipTest("process birth identity is unavailable")
        self.handles.append(owner)
        lock = json.loads(owner.snapshot.raw)
        record = self._descriptor(
            pid=os.getpid(), process_start=lock["process_start"],
            owner_nonce=lock["nonce"])
        record["build_id"] = "incompatible"
        semworker.descriptor_path().write_text(
            json.dumps(record, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(
                semworker, "_retire_incompatible") as retire, \
                mock.patch.object(semworker, "_request_worker_stop") as stop, \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            status = semworker.resident_status()
        self.assertFalse(status["running"])
        self.assertTrue(status["blocked"])
        self.assertEqual(
            status["owner_state"], "descriptor-incompatible")
        self.assertEqual(
            status["reason"],
            "the semantic endpoint belongs to an incompatible live worker")
        self.assertTrue(semworker.descriptor_path().exists())
        retire.assert_not_called()
        stop.assert_not_called()
        terminate.assert_not_called()

    @unittest.skipIf(os.name == "nt", "symlink creation is not portable on Windows")
    def test_descriptor_symlink_remains_blocked_and_untouched(self) -> None:
        target = self.root / "descriptor-target"
        target.write_bytes(b"foreign")
        semworker.descriptor_path().symlink_to(target)
        status = semworker.resident_status()
        result = self._stop_with_self_birth()
        self.assertTrue(status["blocked"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["owner_state"], "descriptor-blocked")
        self.assertTrue(semworker.descriptor_path().is_symlink())
        self.assertEqual(target.read_bytes(), b"foreign")

    @unittest.skipIf(os.name == "nt", "symlink creation is not portable on Windows")
    def test_descriptor_symlink_never_spawns_a_worker(self) -> None:
        target = self.root / "descriptor-target"
        target.write_bytes(b"foreign")
        semworker.descriptor_path().symlink_to(target)
        with mock.patch.object(
                common, "process_start_identity",
                return_value="self-birth"), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker())
        spawn.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO descriptor is POSIX-only")
    def test_descriptor_fifo_remains_blocked_without_opening(self) -> None:
        os.mkfifo(semworker.descriptor_path())
        status = semworker.resident_status()
        result = self._stop_with_self_birth()
        self.assertTrue(status["blocked"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["owner_state"], "descriptor-blocked")
        self.assertTrue(stat.S_ISFIFO(
            semworker.descriptor_path().lstat().st_mode))

    def test_oversized_descriptor_remains_blocked_and_untouched(self) -> None:
        original = b"x" * (64 * 1024 + 1)
        semworker.descriptor_path().write_bytes(original)
        status = semworker.resident_status()
        result = self._stop_with_self_birth()
        self.assertTrue(status["blocked"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["owner_state"], "descriptor-blocked")
        self.assertEqual(
            semworker.descriptor_path().stat().st_size, len(original))

    def test_noncanonical_lock_schema_is_never_exact(self) -> None:
        base = self._owner_record()
        cases = (
            ("missing-nonce", {key: value for key, value in base.items()
                               if key != "nonce"}),
            ("missing-birth-and-nonce", {
                key: value for key, value in base.items()
                if key not in {"process_start", "nonce"}}),
            ("short-nonce", {**base, "nonce": "a" * 31}),
            ("numeric-nonce", {**base, "nonce": int("a" * 32, 16)}),
            ("nan-started", {**base, "started_at": float("nan")}),
            ("text-started", {**base, "started_at": "100"}),
            ("numeric-bound", {**base, "tree_bound": 1}),
            ("text-bound", {**base, "tree_bound": "true"}),
            ("missing-bound", {key: value for key, value in base.items()
                               if key != "tree_bound"}),
        )
        for label, record in cases:
            with self.subTest(label=label):
                raw = json.dumps(
                    record, separators=(",", ":")).encode("utf-8")
                path = semworker.worker_lock_path()
                path.write_bytes(raw)
                os.utime(path, (100.0, 100.0))
                with mock.patch.object(
                        semworker.time, "time", return_value=100.0), \
                        mock.patch.object(
                            common, "pid_alive",
                            side_effect=AssertionError(
                                "invalid lock reached a process probe")):
                    inspected = semworker._inspect_worker_lock()
                self.assertIs(
                    inspected.state,
                    semworker._WorkerOwnerState.MALFORMED_FRESH)
                self.assertEqual(path.read_bytes(), raw)
                path.unlink()

    def test_descriptor_cannot_bind_an_inprocess_owner(self) -> None:
        descriptor = self._descriptor()
        inprocess = self._owner_record(tree_bound=False)
        inspected = self._owner(inprocess)
        self.assertFalse(
            semworker._descriptor_bound_to_lock(descriptor, inspected))

    def test_explicit_semantic_gate_bypasses_an_existing_descriptor(self) -> None:
        rec = self._descriptor()
        descriptor = (rec, self._snapshot(b"descriptor"))
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_SEM_WORKER": "1", "AGREP_NO_DAEMON": ""}), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=descriptor), \
                mock.patch.object(semworker, "_worker_request") as request, \
                mock.patch.object(
                    semworker, "_semantic_owner_pending",
                    return_value=False) as pending:
            result = semworker.search_worker(
                "disabled worker", level="hybrid", k=3)
            disabled_reason = semworker._worker_query_disabled_reason()
        self.assertIsNone(result)
        self.assertEqual(
            disabled_reason,
            "AGREP_NO_SEM_WORKER disables semantic worker queries")
        request.assert_not_called()
        pending.assert_not_called()

    def test_no_daemon_still_queries_the_semantic_worker(self) -> None:
        rec = self._descriptor()
        payload = {"results": [], "score_kind": "cosine"}
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_SEM_WORKER": "", "AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(
                    semworker.removal_fence, "background_removal_active",
                    return_value=False), \
                mock.patch.object(
                    semworker, "_worker_coordination_refusal_reason",
                    return_value=None), \
                mock.patch.object(
                    semworker, "_ensure_worker", return_value=rec) as ensure, \
                mock.patch.object(
                    semworker, "_worker_request",
                    return_value=payload) as request:
            result = semworker.search_worker(
                "meaning survives", level="hybrid", k=3)
            disabled_reason = semworker._worker_query_disabled_reason()
        self.assertIs(result, payload)
        self.assertIsNone(disabled_reason)
        ensure.assert_called_once()
        request.assert_called_once()

    def test_no_daemon_still_starts_the_semantic_worker(self) -> None:
        rec = self._descriptor()
        claim = mock.Mock()
        process = mock.Mock()
        absent = semworker._WorkerOwner(semworker._WorkerOwnerState.ABSENT)
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_SEM_WORKER": "", "AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(
                    semworker.removal_fence, "background_removal_active",
                    return_value=False), \
                mock.patch.object(
                    semworker, "_worker_coordination_refusal_reason",
                    return_value=None), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_descriptor_entry_present",
                    return_value=False), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(absent, absent)), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=claim), \
                mock.patch.object(
                    semworker, "_spawn_worker", return_value=process) as spawn, \
                mock.patch.object(
                    semworker, "_wait_for_worker", return_value=rec), \
                mock.patch.object(semworker, "_release_start_claim") as release:
            result = semworker._ensure_worker(0.2)
        self.assertIs(result, rec)
        spawn.assert_called_once_with(claim)
        release.assert_called_once_with(claim)

    def test_disable_environment_reason_precedes_removal_fence(self) -> None:
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_SEM_WORKER": "1", "AGREP_NO_DAEMON": ""}), \
                mock.patch.object(
                    semworker.removal_fence, "background_removal_active",
                    return_value=True):
            self.assertEqual(
                semworker._worker_query_disabled_reason(),
                "AGREP_NO_SEM_WORKER disables semantic worker queries")

    def test_stop_http_zero_budget_never_opens_a_connection(self) -> None:
        rec = self._descriptor()
        with mock.patch.object(
                semworker.http.client, "HTTPConnection") as connection:
            self.assertFalse(
                semworker._request_worker_stop(rec, timeout_s=0.0))
        connection.assert_not_called()

    def test_stop_http_small_budget_never_reuses_an_expired_timeout(self) -> None:
        rec = self._descriptor()
        response = mock.Mock()
        connection = mock.Mock()
        connection.sock = mock.Mock()
        connection.getresponse.return_value = response
        timestamps = iter((0.0, 0.0, 0.04, 0.06))
        with mock.patch.object(
                semworker.time, "monotonic",
                side_effect=lambda: next(timestamps)), \
                mock.patch.object(
                    semworker.http.client, "HTTPConnection",
                    return_value=connection) as create:
            self.assertFalse(
                semworker._request_worker_stop(rec, timeout_s=0.05))
        timeout = create.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0.0)
        self.assertLessEqual(timeout, 0.05)
        connection.sock.settimeout.assert_called_once()
        socket_timeout = connection.sock.settimeout.call_args.args[0]
        self.assertGreater(socket_timeout, 0.0)
        self.assertLessEqual(socket_timeout, 0.011)
        connection.getresponse.assert_not_called()
        connection.close.assert_called_once_with()

    def test_outer_stop_preserves_force_budget_after_slow_grace(self) -> None:
        clock = _Clock()
        rec = self._descriptor()
        descriptor = self._snapshot(b"descriptor", 2)
        owner_record = self._owner_record()
        owner = self._owner(owner_record, 3)
        absent = semworker._WorkerOwner(
            semworker._WorkerOwnerState.ABSENT)
        claim = mock.Mock()
        fallback_budgets = []

        def consume_grace(
                _rec: dict, found, timeout_s: float):
            clock.now += timeout_s
            return False, found, False

        def use_fallback(_rec: dict, timeout_s: float) -> bool:
            fallback_budgets.append(timeout_s)
            return True

        with mock.patch.object(
                semworker, "_acquire_start_claim",
                return_value=claim), \
                mock.patch.object(
                    semworker, "_release_start_claim"), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    side_effect=((rec, descriptor), None)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    side_effect=(owner, absent)), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    side_effect=consume_grace) as wait, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    side_effect=use_fallback), \
                mock.patch.object(
                    semworker, "_discard_record",
                    return_value=True), \
                mock.patch.object(
                    semworker.time, "monotonic",
                    side_effect=clock.monotonic):
            result = semworker.stop_worker_and_wait(0.1, 0.2)
        self.assertTrue(result["ok"])
        self.assertEqual(wait.call_args.args[2], 0.1)
        self.assertEqual(len(fallback_budgets), 1)
        self.assertAlmostEqual(fallback_budgets[0], 0.2)

    def test_outer_stop_zero_budget_never_contacts_or_terminates(self) -> None:
        rec = self._descriptor()
        descriptor = self._snapshot(b"descriptor", 2)
        owner = self._owner(self._owner_record(), 3)
        claim = mock.Mock()
        with mock.patch.object(
                semworker, "_acquire_start_claim",
                return_value=claim), \
                mock.patch.object(
                    semworker, "_release_start_claim"), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    return_value=(rec, descriptor)), \
                mock.patch.object(
                    semworker, "_inspect_worker_lock",
                    return_value=owner), \
                mock.patch.object(
                    semworker, "_request_and_drain_worker",
                    return_value=(False, (rec, descriptor), False)) as wait, \
                mock.patch.object(
                    semworker, "_request_worker_stop") as contact, \
                mock.patch.object(
                    semworker, "_terminate_worker_tree") as force:
            result = semworker.stop_worker_and_wait(0.0, 0.0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["running"])
        wait.assert_called_once_with(
            rec, (rec, descriptor), 0.0)
        contact.assert_not_called()
        force.assert_not_called()

    def test_oversized_utf8_request_is_unavailable_not_transport_none(
            self) -> None:
        rec = self._descriptor()
        payload = {
            "query": "🧠" * (semworker.MAX_BODY_BYTES // 4 + 100),
            "level": "hybrid",
            "k": 3,
            "filters": {},
            "timing": False,
        }
        with mock.patch.object(
                semworker.http.client, "HTTPConnection") as connection:
            with self.assertRaisesRegex(
                    semworker.ResidentSemanticUnavailable,
                    "transport size limit"):
                semworker._worker_request(rec, payload)
        connection.assert_not_called()

    def test_start_claim_rejects_bad_schema_and_future_clocks(self) -> None:
        base = {
            "pid": 4242,
            "at": 100.0,
            "process_start": "birth",
            "nonce": "a" * 32,
        }
        bad = (
            {key: value for key, value in base.items() if key != "nonce"},
            {**base, "pid": True},
            {**base, "at": "100"},
            {**base, "process_start": None},
            {**base, "nonce": int("a" * 32, 16)},
        )
        with mock.patch.object(semworker.time, "time", return_value=100.0):
            for record in bad:
                with self.subTest(record=record):
                    raw = json.dumps(
                        record, separators=(",", ":")).encode("utf-8")
                    observed = self._snapshot(raw, mtime=90.0)
                    self.assertFalse(
                        semworker._start_claim_protected(observed))

        def exact(*_args, **_kwargs):
            return ownerfile.ProcessOwner.EXACT_LIVE

        future_wall = self._snapshot(
            json.dumps(
                {**base, "at": 107.0},
                separators=(",", ":")).encode("utf-8"),
            mtime=100.0)
        future_mtime = self._snapshot(
            json.dumps(
                base, separators=(",", ":")).encode("utf-8"),
            mtime=107.0)
        malformed_future_mtime = self._snapshot(
            b"[]", mtime=107.0)
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(
                    ownerfile, "classify_process", side_effect=exact):
            self.assertFalse(
                semworker._start_claim_protected(future_wall))
            self.assertTrue(
                semworker._start_claim_protected(future_mtime))
            self.assertFalse(
                semworker._start_claim_protected(malformed_future_mtime))

    def test_non_finite_result_limit_is_a_validation_error_not_a_crash(self) -> None:
        # X1: json parses 1e1000 to float('inf'); int(inf) raised OverflowError
        # past the handler's 400 tuple instead of the invalid-limit refusal.
        for raw_k in ("1e1000", "-1e1000"):
            with self.subTest(k=raw_k):
                obj = json.loads(
                    '{"query":"hi","level":"message","k":%s,'
                    '"filters":{},"timing":false}' % raw_k)
                with self.assertRaisesRegex(
                        ValueError, "invalid semantic result limit"):
                    semworker._validate_request(obj)
        obj = json.loads(
            '{"query":"hi","level":"message","k":NaN,'
            '"filters":{},"timing":false}')
        with self.assertRaisesRegex(
                ValueError, "invalid semantic result limit"):
            semworker._validate_request(obj)


@unittest.skipIf(os.name == "nt", "POSIX private-session worker")
class PosixSemanticWorkerProductStopTests(unittest.TestCase):
    def test_product_stop_drains_a_sigterm_ignoring_descendant(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-product-stop-") as raw:
            root = Path(raw)
            ready = root / "child.pid"
            child = (
                "import os,pathlib,signal,sys,time\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "while True: time.sleep(1)\n"
            )
            worker = (
                "import common,semworker,subprocess,sys\n"
                "owner=semworker.acquire_resident_owner()\n"
                "if owner is None: raise SystemExit(4)\n"
                "server=semworker.SemanticWorkerServer("
                "search_fn=lambda *a,**k:{'results':[]},"
                "lifetime=owner)\n"
                "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])\n"
                "server.serve()\n"
                "owner.close()\n"
            )
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(root),
                "AGREP_DATA_DIR_SOURCE": "test",
                "PYTHONPATH": str(PY_DIR),
            }
            process = subprocess.Popen(
                [sys.executable, "-c", worker, child, str(ready)],
                cwd=PY_DIR, env=env, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            child_pid = None
            saved_data = common.DATA_DIR
            common.DATA_DIR = root
            try:
                deadline = time.monotonic() + 5.0
                while (not ready.exists()
                       or not semworker.descriptor_path().exists()):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=2)
                    if ("Operation not permitted" in stderr
                            or "PermissionError" in stderr):
                        self.skipTest("loopback binding is unavailable")
                    self.fail(f"worker fixture exited: {stdout}\n{stderr}")
                self.assertTrue(ready.exists())
                self.assertTrue(semworker.descriptor_path().exists())
                child_pid = int(ready.read_text(encoding="ascii"))
                result = semworker.stop_worker_and_wait(2.0, 3.0)
                self.assertTrue(result["ok"], result)
                process.wait(timeout=5)
                deadline = time.monotonic() + 3.0
                while (common.pid_alive(child_pid)
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
                self.assertFalse(common.pid_alive(child_pid))
            finally:
                common.DATA_DIR = saved_data
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
                if child_pid is not None and common.pid_alive(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass


class SemanticWorkerLoopbackBurstTests(unittest.TestCase):
    @staticmethod
    def _raw_search(record: dict, deadlines: list[str]) -> tuple[int, dict]:
        body = json.dumps({
            "query": "deadline contract", "level": "hybrid",
            "k": 1, "filters": {}, "timing": False,
        }, separators=(",", ":")).encode()
        conn = http.client.HTTPConnection(
            "127.0.0.1", int(record["port"]), timeout=2.0)
        try:
            conn.connect()
            conn.putrequest("POST", "/v1/search")
            conn.putheader(
                "Authorization", f"Bearer {record['token']}")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Connection", "close")
            for deadline in deadlines:
                conn.putheader(
                    semworker.REQUEST_DEADLINE_HEADER, deadline)
            conn.endheaders(body)
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            conn.close()

    def test_deadline_contract_rejects_stale_work_without_side_effects(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-deadline-") as raw:
            root = Path(raw)
            saved_data = common.DATA_DIR
            common.DATA_DIR = root
            owner = None
            server = None
            server_thread = None
            calls = []
            beat = root / "search.beat"

            def search(*_args, **_kwargs):
                calls.append("search")
                return {"results": [], "score_kind": "cosine"}

            try:
                with mock.patch.object(
                        common, "bind_descendants_to_process_lifetime",
                        return_value=True):
                    owner = semworker.acquire_resident_owner()
                    if owner is None:
                        self.skipTest("semantic owner is unavailable")
                    try:
                        server = semworker.SemanticWorkerServer(
                            search_fn=search, lifetime=owner)
                    except (PermissionError,
                            semworker.ResidentSemanticPreflightUnavailable):
                        self.skipTest("loopback binding is unavailable")
                record = json.loads(server.record)
                server_thread = threading.Thread(
                    target=server.serve, daemon=True)
                server_thread.start()
                now = time.monotonic_ns()
                future = str(now + 10_000_000_000)
                invalid = (
                    ([], 400),
                    ([future, future], 400),
                    (["not-a-deadline"], 400),
                    ([str(now + semworker.MAX_REQUEST_DEADLINE_NS
                          + 1_000_000_000)], 400),
                    ([str(now - 1)], 408),
                )
                with mock.patch.object(indexd_runtime, "SEARCH_BEAT_PATH", beat):
                    for deadlines, expected in invalid:
                        with self.subTest(deadlines=deadlines):
                            status, payload = self._raw_search(
                                record, deadlines)
                            self.assertEqual(status, expected, payload)
                    self.assertEqual(calls, [])
                    self.assertEqual(server.requests, 0)
                    self.assertFalse(beat.exists())
                    result = semworker._worker_request(
                        record, {
                            "query": "still usable", "level": "hybrid",
                            "k": 1, "filters": {}, "timing": False,
                        }, timeout_s=1.0)
                self.assertIsInstance(result, dict)
                self.assertEqual(calls, ["search"])
                self.assertEqual(server.requests, 1)
                self.assertTrue(beat.exists())
            finally:
                if server is not None:
                    server.request_stop()
                if server_thread is not None:
                    server_thread.join(timeout=2.0)
                if owner is not None:
                    owner.release(
                        tombstone=True, require_stable_mtime=True)
                common.DATA_DIR = saved_data

    def test_failed_release_stop_never_accepts_a_queued_search(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-stop-") as raw:
            root = Path(raw)
            saved_data = common.DATA_DIR
            common.DATA_DIR = root
            owner = None
            server = None
            server_thread = None
            release_entered = threading.Event()
            finish_release = threading.Event()
            calls = []
            stop_result = []
            search_result = []

            def search(*_args, **_kwargs):
                calls.append("search")
                return {"results": [], "score_kind": "cosine"}

            def release() -> bool:
                release_entered.set()
                finish_release.wait(timeout=2.0)
                return False

            try:
                with mock.patch.object(
                        common, "bind_descendants_to_process_lifetime",
                        return_value=True):
                    owner = semworker.acquire_resident_owner()
                    if owner is None:
                        self.skipTest("semantic owner is unavailable")
                    try:
                        server = semworker.SemanticWorkerServer(
                            search_fn=search, lifetime=owner)
                    except (PermissionError,
                            semworker.ResidentSemanticPreflightUnavailable):
                        self.skipTest("loopback binding is unavailable")
                record = json.loads(server.record)
                server._release_resources = mock.Mock(side_effect=release)
                server_thread = threading.Thread(
                    target=server.serve, daemon=True)
                server_thread.start()
                stopper = threading.Thread(
                    target=lambda: stop_result.append(
                        semworker._request_worker_stop(
                            record, timeout_s=1.0)),
                    daemon=True)
                stopper.start()
                self.assertTrue(release_entered.wait(timeout=1.0))

                def queued_search() -> None:
                    try:
                        search_result.append(semworker._worker_request(
                            record, {
                                "query": "queued", "level": "hybrid",
                                "k": 1, "filters": {}, "timing": False,
                            }, timeout_s=1.0))
                    except semworker.ResidentSemanticUnavailable:
                        search_result.append("unavailable")

                queued = threading.Thread(target=queued_search, daemon=True)
                queued.start()
                time.sleep(0.05)
                finish_release.set()
                stopper.join(timeout=2.0)
                queued.join(timeout=2.0)
                self.assertFalse(stopper.is_alive())
                self.assertFalse(queued.is_alive())
                self.assertTrue(server_thread.is_alive())
                self.assertGreater(server.retire_deadline, time.monotonic())
                self.assertEqual(stop_result, [False])
                self.assertEqual(calls, [])
                self.assertTrue(search_result)
                self.assertNotIsInstance(search_result[0], dict)
            finally:
                finish_release.set()
                if server is not None:
                    server.request_stop()
                if server_thread is not None:
                    server_thread.join(timeout=2.0)
                if owner is not None:
                    owner.release(
                        tombstone=True, require_stable_mtime=True)
                common.DATA_DIR = saved_data

    def test_successful_stop_ack_holds_root_for_external_tree_drain(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-ack-") as raw:
            saved_data = common.DATA_DIR
            common.DATA_DIR = Path(raw)
            owner = None
            server = None
            server_thread = None
            try:
                with mock.patch.object(
                        common, "bind_descendants_to_process_lifetime",
                        return_value=True):
                    owner = semworker.acquire_resident_owner()
                    if owner is None:
                        self.skipTest("semantic owner is unavailable")
                    try:
                        server = semworker.SemanticWorkerServer(
                            search_fn=lambda *_args, **_kwargs: {},
                            lifetime=owner)
                    except (PermissionError,
                            semworker.ResidentSemanticPreflightUnavailable):
                        self.skipTest("loopback binding is unavailable")
                record = json.loads(server.record)
                server_thread = threading.Thread(
                    target=server.serve, daemon=True)
                server_thread.start()
                self.assertTrue(
                    semworker._request_worker_stop(record, timeout_s=1.0))
                self.assertTrue(server_thread.is_alive())
                self.assertGreater(server.retire_deadline, time.monotonic())
            finally:
                if server is not None:
                    server.request_stop()
                if server_thread is not None:
                    server_thread.join(timeout=2.0)
                if owner is not None:
                    owner.release(
                        tombstone=True, require_stable_mtime=True)
                common.DATA_DIR = saved_data

    def test_expired_burst_is_drained_before_a_valid_sentinel(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-burst-") as raw:
            root = Path(raw)
            saved_data = common.DATA_DIR
            common.DATA_DIR = root
            owner = None
            server = None
            server_thread = None
            gate = threading.Event()
            started = threading.Event()
            count_lock = threading.Lock()
            calls = [0]
            unexpected = []

            def search(*_args, **_kwargs):
                with count_lock:
                    calls[0] += 1
                started.set()
                gate.wait(timeout=2.0)
                return {"results": [], "score_kind": "cosine"}

            try:
                owner = semworker._acquire_worker_lock(tree_bound=True)
                if owner is None:
                    self.skipTest("process birth identity is unavailable")
                try:
                    with mock.patch.object(
                            common, "bind_descendants_to_process_lifetime",
                            return_value=True), \
                            mock.patch.object(
                                semworker._BoundedHTTPServer,
                                "request_queue_size", 128):
                        server = semworker.SemanticWorkerServer(
                            search_fn=search, lifetime=owner)
                except (PermissionError,
                        semworker.ResidentSemanticPreflightUnavailable):
                    self.skipTest("loopback binding is unavailable")
                record = json.loads(server.record)
                server_thread = threading.Thread(
                    target=server.serve, daemon=True)
                server_thread.start()
                barrier = threading.Barrier(9)
                clients = []

                def request() -> None:
                    try:
                        barrier.wait(timeout=2)
                        result = semworker._worker_request(
                            record, {
                                "query": "queued", "level": "hybrid",
                                "k": 3, "filters": {}, "timing": False,
                            }, timeout_s=0.12)
                        unexpected.append(("queued-result", result))
                    except semworker.ResidentSemanticTimeout:
                        pass
                    except BaseException as exc:
                        unexpected.append(("queued-error", exc))

                def accepted_request() -> None:
                    try:
                        result = semworker._worker_request(
                            record, {
                                "query": "accepted",
                                "level": "hybrid",
                                "k": 3,
                                "filters": {},
                                "timing": False,
                            }, timeout_s=2.0)
                        if not isinstance(result, dict):
                            unexpected.append(("accepted-result", result))
                    except BaseException as exc:
                        unexpected.append(("accepted-error", exc))

                with mock.patch.object(
                        semworker, "_cancel_timed_out_worker",
                        wraps=semworker._cancel_timed_out_worker) as cancel:
                    accepted = threading.Thread(
                        target=accepted_request, daemon=True)
                    accepted.start()
                    self.assertTrue(started.wait(timeout=1.0))

                    for _ in range(8):
                        thread = threading.Thread(
                            target=request, daemon=True)
                        clients.append(thread)
                        thread.start()
                    barrier.wait(timeout=2)
                    for thread in clients:
                        thread.join(timeout=1.0)
                    self.assertFalse(any(
                        thread.is_alive() for thread in clients))
                    gate.set()
                    accepted.join(timeout=2.0)
                    if accepted.is_alive():
                        unexpected.append(("accepted-alive", None))
                    sentinel = semworker._worker_request(
                        record, {
                            "query": "sentinel", "level": "hybrid",
                            "k": 3, "filters": {}, "timing": False,
                        }, timeout_s=2.0)
                cancel.assert_not_called()
                self.assertIsInstance(sentinel, dict)
                server.request_stop()
                server_thread.join(timeout=2.0)
                self.assertFalse(server_thread.is_alive())
                self.assertEqual(unexpected, [])
                self.assertEqual(calls[0], 2)
            finally:
                gate.set()
                if server is not None:
                    server.request_stop()
                if server_thread is not None:
                    server_thread.join(timeout=2.0)
                if owner is not None:
                    owner.release(
                        tombstone=True, require_stable_mtime=True)
                common.DATA_DIR = saved_data

    # COORDINATION.md: only an accepted request retires the worker generation
    # that exceeded its deadline. Both leaders here are accepted with a
    # queue-consumed residual; only the one whose inference outlives it retires.
    def test_accepted_sliver_budget_pins_which_side_retires_the_worker(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-sliver-") as raw:
            root = Path(raw)
            saved_data = common.DATA_DIR
            common.DATA_DIR = root
            owner = None
            server = None
            server_thread = None
            gates = {"holder-fits": threading.Event(),
                     "holder-sliver": threading.Event(),
                     "sliver": threading.Event()}
            entered = {name: threading.Event() for name in gates}

            def search(query, **_kwargs):
                if query in gates:
                    entered[query].set()
                    gates[query].wait(timeout=5.0)
                return {"results": [], "score_kind": "cosine"}

            outcomes: list[object] = []

            def leader(query: str, timeout_s: float) -> None:
                try:
                    outcomes.append(semworker._worker_request(
                        record, {"query": query, "level": "hybrid",
                                 "k": 1, "filters": {}, "timing": False},
                        timeout_s=timeout_s))
                except BaseException as exc:  # noqa: BLE001
                    outcomes.append(exc)

            def queued_round(holder_query: str, query: str,
                             timeout_s: float) -> None:
                holder = threading.Thread(
                    target=leader, args=(holder_query, 5.0), daemon=True)
                holder.start()
                self.assertTrue(entered[holder_query].wait(timeout=2.0))
                follower = threading.Thread(
                    target=leader, args=(query, timeout_s), daemon=True)
                follower.start()
                time.sleep(0.1)
                gates[holder_query].set()
                holder.join(timeout=3.0)
                follower.join(timeout=3.0)
                self.assertFalse(holder.is_alive())
                self.assertFalse(follower.is_alive())

            try:
                owner = semworker._acquire_worker_lock(tree_bound=True)
                if owner is None:
                    self.skipTest("process birth identity is unavailable")
                try:
                    with mock.patch.object(
                            common, "bind_descendants_to_process_lifetime",
                            return_value=True):
                        server = semworker.SemanticWorkerServer(
                            search_fn=search, lifetime=owner)
                except (PermissionError,
                        semworker.ResidentSemanticPreflightUnavailable):
                    self.skipTest("loopback binding is unavailable")
                record = json.loads(server.record)
                server_thread = threading.Thread(
                    target=server.serve, daemon=True)
                server_thread.start()

                with mock.patch.object(
                        semworker, "_cancel_timed_out_worker",
                        wraps=semworker._cancel_timed_out_worker) as retire:
                    # barely fits: accepted with the queue-consumed residual,
                    # and the inference completes inside it
                    queued_round("holder-fits", "fits", 2.0)
                    self.assertEqual(
                        [type(item) for item in outcomes], [dict, dict],
                        outcomes)
                    retire.assert_not_called()
                    self.assertTrue(server_thread.is_alive())

                    # barely does not fit: accepted the same way, but the
                    # inference is held past the residual deadline
                    queued_round("holder-sliver", "sliver", 0.8)
                    self.assertIsInstance(outcomes[2], dict)
                    self.assertIsInstance(
                        outcomes[3], semworker.ResidentSemanticTimeout)
                retire.assert_called_once_with(record)
                gates["sliver"].set()
                server_thread.join(timeout=3.0)
                self.assertFalse(server_thread.is_alive())
            finally:
                for gate in gates.values():
                    gate.set()
                if server is not None:
                    server.request_stop()
                if server_thread is not None:
                    server_thread.join(timeout=2.0)
                if owner is not None:
                    owner.release(
                        tombstone=True, require_stable_mtime=True)
                common.DATA_DIR = saved_data


class ClientDeadlineHardEnforcementTests(unittest.TestCase):
    def test_trickled_response_cannot_outlive_the_client_deadline(self) -> None:
        # P3: per-op socket timeouts reset on every recv, so a worker feeding
        # one byte per interval kept the client alive far past its deadline.
        # The watchdog must sever the socket and surface a timeout instead.
        import socket as socket_module
        body = b'{"ok":true,"result":{}}'
        server = socket_module.socket()
        server.setsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            server.close()
            self.skipTest(f"loopback binding is unavailable: {exc}")
        server.listen(1)
        port = server.getsockname()[1]
        stop = threading.Event()

        def serve() -> None:
            try:
                client, _ = server.accept()
            except OSError:
                return
            with client:
                client.settimeout(5.0)
                try:
                    client.recv(65536)
                    client.sendall(
                        ("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                         "Connection: close\r\n\r\n" % len(body)).encode())
                    for value in body:
                        if stop.wait(0.2):
                            return
                        client.sendall(bytes([value]))
                except OSError:
                    return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        rec = {"port": port, "token": "a" * 64, "pid": os.getpid()}
        budget = 0.5
        started = time.monotonic()
        try:
            with self.assertRaises(semworker.ResidentSemanticTimeout):
                semworker._worker_request(
                    rec, {"query": "deadline"}, timeout_s=budget)
            elapsed = time.monotonic() - started
            # untreated, the trickle holds the client ~4.6s; the wall-clock
            # deadline must cut it near the 0.5s budget (slack for CI wobble)
            self.assertLess(elapsed, budget + 1.5)
        finally:
            stop.set()
            server.close()
            thread.join(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
