"""Contention and bounded-exit contracts for the semantic worker client."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)
import common  # noqa: E402
import ownerfile  # noqa: E402
import semworker  # noqa: E402


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
        self.production_coordination_base_path = (
            semworker._coordination_base_path)
        self.coordination = mock.patch.object(
            semworker, "_coordination_base_path", return_value=self.root)
        self.coordination.start()

    def tearDown(self) -> None:
        self.coordination.stop()
        common.DATA_DIR = self.saved_data_dir
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
        with tempfile.TemporaryDirectory(
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
                coordination_info = os.lstat(request_path.parent)
                if os.name == "nt":
                    self.assertFalse(
                        getattr(coordination_info, "st_file_attributes", 0)
                        & getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                else:
                    self.assertEqual(
                        stat.S_IMODE(coordination_info.st_mode), 0o700)
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
        with tempfile.TemporaryDirectory(
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
            self.assertEqual(stat.S_IMODE(coordination.stat().st_mode), 0o700)
            coordination.rmdir()

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
        with tempfile.TemporaryDirectory(
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
        with tempfile.TemporaryDirectory(
                prefix="agrep-semworker-coord-symlink-") as runtime_raw:
            runtime = Path(runtime_raw).resolve()
            protected = runtime / "data"
            protected.mkdir()
            target = runtime / "target"
            target.mkdir()
            with mock.patch.object(common, "DATA_DIR", protected), \
                    mock.patch.object(
                        semworker, "_coordination_base_path",
                        return_value=runtime):
                coordination = semworker._ephemeral_coordination_dir()
                coordination.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(
                        OSError, "not a private directory"):
                    semworker.request_lock_path()

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
                side_effect=(resident, resident)),
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
                side_effect=(resident, resident)),
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
                side_effect=(resident, resident)),
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
                side_effect=(resident, resident)),
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
        with mock.patch.object(
                semworker.http.client, "HTTPConnection",
                return_value=connection), \
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
