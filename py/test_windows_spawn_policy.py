from __future__ import annotations

import ctypes
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import proc  # noqa: E402
import removal_fence  # noqa: E402
import indexd_runtime  # noqa: E402
import embed  # noqa: E402
import embedder  # noqa: E402
import embedding_segments  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402
import teach  # noqa: E402
import winjob  # noqa: E402


class WindowsSpawnPolicyTests(unittest.TestCase):
    @staticmethod
    def _expected(base: int, in_job: bool) -> int:
        return (base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB
                if in_job else base)

    def test_breakaway_requires_agrep_owned_job(self) -> None:
        base = 0x08004208
        cases = (
            ("non-windows", False, object(), True, os.getpid(), base),
            ("ordinary-or-foreign-job", True, None, False, None, base),
            ("unbound-handle", True, object(), False, None, base),
            ("inherited-handle", True, object(), True, os.getpid() + 1, base),
            ("agrep-owned-job", True, object(), True, os.getpid(),
             base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
        )
        for label, windows, handle, bound, bound_pid, expected in cases:
            with self.subTest(label=label), \
                    mock.patch.object(proc, "WIN", windows), \
                    mock.patch.object(
                        proc, "_DESCENDANT_JOB_HANDLE", handle), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_BOUND", bound), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_PID", bound_pid):
                self.assertEqual(
                    common.windows_detached_child_flags(base), expected)
        self.assertEqual(
            common._WINDOWS_DESCENDANT_JOB_LIMITS,
            common._WINDOWS_JOB_KILL_ON_CLOSE
            | common._WINDOWS_JOB_BREAKAWAY_OK)

    def test_windows_job_membership_uses_kernel_proof(self) -> None:
        kernel32 = mock.Mock()
        kernel32.GetCurrentProcess.return_value = 123
        membership = [1]

        def report_membership(_process, _job, result) -> int:
            result._obj.value = membership[0]
            return 1

        kernel32.IsProcessInJob.side_effect = report_membership
        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    ctypes, "WinDLL", create=True, return_value=kernel32):
            self.assertTrue(common._windows_current_process_in_job())
        membership[0] = 0
        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    ctypes, "WinDLL", create=True, return_value=kernel32):
            self.assertFalse(common._windows_current_process_in_job())
        kernel32.IsProcessInJob.side_effect = None
        kernel32.IsProcessInJob.return_value = 0
        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    ctypes, "WinDLL", create=True, return_value=kernel32):
            self.assertIsNone(common._windows_current_process_in_job())

    def test_background_work_breaks_out_unless_no_job_is_proven(self) -> None:
        base = 0x08004208
        cases = (
            ("non-windows", False, False, False, base),
            ("no-job", True, False, False, base),
            ("external-job", True, False, True,
             base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
            ("unknown-job", True, False, None,
             base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
            ("agrep-owned-job", True, True, False,
             base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
        )
        for label, windows, owned, membership, expected in cases:
            with self.subTest(label=label), \
                    mock.patch.object(proc, "WIN", windows), \
                    mock.patch.object(
                        proc, "_DESCENDANT_JOB_HANDLE",
                        object() if owned else None), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_BOUND", owned), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_PID",
                        os.getpid() if owned else None), \
                    mock.patch.object(
                        proc, "_windows_current_process_in_job",
                        return_value=membership):
                self.assertEqual(
                    common.windows_background_child_flags(base), expected)

    def test_existing_job_handle_does_not_resurrect_invalid_proof(self) -> None:
        for label, bound, bound_pid in (
                ("unbound", False, os.getpid()),
                ("inherited", True, os.getpid() + 1)):
            with self.subTest(label=label), \
                    mock.patch.object(proc, "WIN", True), \
                    mock.patch.object(
                        proc, "_DESCENDANT_JOB_HANDLE", object()), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_BOUND", bound), \
                    mock.patch.object(
                        proc, "_DESCENDANT_LIFETIME_PID", bound_pid):
                self.assertFalse(
                    common.bind_descendants_to_process_lifetime())
                self.assertFalse(proc._DESCENDANT_LIFETIME_BOUND)
                self.assertIsNone(proc._DESCENDANT_LIFETIME_PID)

    def test_exact_job_name_uses_the_global_birth_namespace(self) -> None:
        self.assertEqual(
            winjob.name_for(42, "win_123"),
            "Global\\agrep-descendants-42-123")
        for pid, birth in (
                (0, "win_123"),
                (-1, "win_123"),
                (42, "proc_123"),
                (42, "win_123\\other")):
            with self.subTest(pid=pid, birth=birth):
                self.assertIsNone(winjob.name_for(pid, birth))

    def test_named_job_bind_retains_only_a_configured_assignment(self) -> None:
        kernel32 = mock.Mock()
        kernel32.CreateJobObjectW.return_value = 123
        kernel32.SetInformationJobObject.return_value = 1
        kernel32.GetCurrentProcess.return_value = 456
        kernel32.AssignProcessToJobObject.return_value = 1
        limits = common._WINDOWS_DESCENDANT_JOB_LIMITS
        with mock.patch.object(winjob, "_kernel32", return_value=kernel32), \
                mock.patch.object(
                    winjob.ctypes, "set_last_error", create=True), \
                mock.patch.object(
                    winjob.ctypes, "get_last_error",
                    return_value=0, create=True):
            self.assertEqual(
                winjob.bind_current(42, "win_123", limits), 123)
        kernel32.CreateJobObjectW.assert_called_once_with(
            None, "Global\\agrep-descendants-42-123")
        configured = kernel32.SetInformationJobObject.call_args
        self.assertEqual(configured.args[2]._obj.basic.flags, limits)
        kernel32.AssignProcessToJobObject.assert_called_once_with(123, 456)
        kernel32.CloseHandle.assert_not_called()

    def test_named_job_namespace_collision_fails_closed(self) -> None:
        kernel32 = mock.Mock()
        kernel32.CreateJobObjectW.return_value = 123
        with mock.patch.object(winjob, "_kernel32", return_value=kernel32), \
                mock.patch.object(
                    winjob.ctypes, "set_last_error", create=True), \
                mock.patch.object(
                    winjob.ctypes, "get_last_error",
                    return_value=winjob._ERROR_ALREADY_EXISTS, create=True):
            self.assertIsNone(winjob.bind_current(
                42, "win_123", common._WINDOWS_DESCENDANT_JOB_LIMITS))
        kernel32.CloseHandle.assert_called_once_with(123)
        kernel32.SetInformationJobObject.assert_not_called()
        kernel32.AssignProcessToJobObject.assert_not_called()

    def test_open_exact_rejects_wrong_birth_and_nonmember(self) -> None:
        def process_times(ticks: int):
            def read(_handle, created, _exited, _kernel, _user) -> int:
                created._obj.low = ticks
                created._obj.high = 0
                return 1
            return read

        wrong_birth = mock.Mock()
        wrong_birth.OpenJobObjectW.return_value = 101
        wrong_birth.OpenProcess.return_value = 202
        wrong_birth.GetProcessTimes.side_effect = process_times(124)
        with mock.patch.object(
                winjob, "_kernel32", return_value=wrong_birth):
            self.assertIsNone(winjob.open_exact(42, "win_123"))
        wrong_birth.IsProcessInJob.assert_not_called()
        self.assertEqual(
            wrong_birth.CloseHandle.call_args_list,
            [mock.call(202), mock.call(101)])

        nonmember = mock.Mock()
        nonmember.OpenJobObjectW.return_value = 303
        nonmember.OpenProcess.return_value = 404
        nonmember.GetProcessTimes.side_effect = process_times(123)

        def outside_job(_process, _job, member) -> int:
            member._obj.value = 0
            return 1

        nonmember.IsProcessInJob.side_effect = outside_job
        with mock.patch.object(winjob, "_kernel32", return_value=nonmember):
            self.assertIsNone(winjob.open_exact(42, "win_123"))
        self.assertEqual(
            nonmember.CloseHandle.call_args_list,
            [mock.call(404), mock.call(303)])

    def test_job_drain_waits_for_zero_active_and_signaled_root(self) -> None:
        kernel32 = mock.Mock()
        kernel32.TerminateJobObject.return_value = 1
        active = iter((1, 0))

        def accounting(_job, _kind, record, _size, _returned) -> int:
            record._obj.active_processes = next(active)
            return 1

        kernel32.QueryInformationJobObject.side_effect = accounting
        kernel32.WaitForSingleObject.side_effect = (258, winjob._WAIT_OBJECT_0)
        tree = winjob.Handle(kernel32, 101, 202)
        with mock.patch.object(
                winjob.time, "monotonic", side_effect=(0.0, 0.0)), \
                mock.patch.object(winjob.time, "sleep") as sleep:
            self.assertTrue(tree.terminate_and_wait(1.0))
        self.assertEqual(kernel32.QueryInformationJobObject.call_count, 2)
        self.assertEqual(kernel32.WaitForSingleObject.call_count, 2)
        sleep.assert_called_once_with(0.02)

    def test_job_drain_fails_without_each_kernel_proof(self) -> None:
        cases = (
            ("query", False, 0, winjob._WAIT_OBJECT_0),
            ("active", True, 1, winjob._WAIT_OBJECT_0),
            ("root", True, 0, 258),
        )
        for label, queried, active, root_wait in cases:
            with self.subTest(label=label):
                kernel32 = mock.Mock()
                kernel32.TerminateJobObject.return_value = 1

                def accounting(
                        _job, _kind, record, _size, _returned) -> int:
                    record._obj.active_processes = active
                    return int(queried)

                kernel32.QueryInformationJobObject.side_effect = accounting
                kernel32.WaitForSingleObject.return_value = root_wait
                tree = winjob.Handle(kernel32, 101, 202)
                self.assertFalse(tree.terminate_and_wait(0.0))

    def test_bound_tree_dispatch_requires_the_exact_named_job(self) -> None:
        tree = mock.Mock()
        tree.terminate_and_wait.return_value = True
        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    proc, "process_start_identity", return_value="win_123"), \
                mock.patch.object(
                    winjob, "open_exact", return_value=tree) as opened, \
                mock.patch.object(proc, "terminate_exact_process") as root:
            self.assertTrue(common.terminate_exact_process_tree(
                42, "win_123", wait_s=2.0, require_bound_tree=True))
        opened.assert_called_once_with(42, "win_123")
        tree.terminate_and_wait.assert_called_once()
        self.assertLessEqual(tree.terminate_and_wait.call_args.args[0], 2.0)
        tree.close.assert_called_once_with()
        root.assert_not_called()

        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    proc, "process_start_identity", return_value="win_123"), \
                mock.patch.object(winjob, "open_exact", return_value=None), \
                mock.patch.object(proc, "terminate_exact_process") as root:
            self.assertFalse(common.terminate_exact_process_tree(
                42, "win_123", wait_s=2.0, require_bound_tree=True))
        root.assert_not_called()

        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    proc, "pid_alive", side_effect=(True, False)), \
                mock.patch.object(
                    proc, "process_start_identity", return_value="win_123"), \
                mock.patch.object(winjob, "open_exact", return_value=None), \
                mock.patch.object(proc, "terminate_exact_process") as root:
            self.assertFalse(common.terminate_exact_process_tree(
                42, "win_123", wait_s=2.0, require_bound_tree=True))
        root.assert_not_called()

    def test_semantic_refresh_uses_background_job_policy(self) -> None:
        base = 0x08004208
        for external_job in (False, True):
            with self.subTest(external_job=external_job):
                log = mock.Mock()
                with mock.patch.object(common, "WIN", True), \
                        mock.patch.object(proc, "WIN", True), \
                        mock.patch.object(
                            proc, "_DESCENDANT_JOB_HANDLE", None), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_PID", None), \
                        mock.patch.object(
                            proc, "_windows_current_process_in_job",
                            return_value=external_job), \
                        mock.patch.object(
                            proc, "process_start_identity",
                            return_value=None), \
                        mock.patch.object(semantic.sys, "platform", "win32"), \
                        mock.patch.object(
                            semantic, "_background_refresh_disabled",
                            return_value=None), \
                        mock.patch.object(
                            semantic, "runtime_dependencies_available",
                            return_value=True), \
                        mock.patch.object(
                            semantic, "embedding_coherence",
                            return_value={"coherent": False, "state": "missing"}), \
                        mock.patch.object(
                            semantic, "embed_running", return_value=False), \
                        mock.patch.object(
                            semantic, "read_embed_state", return_value={}), \
                        mock.patch.object(embedder, "ensure_model"), \
                        mock.patch.object(
                            semantic, "_needs_unverified_bundle_rebuild",
                            return_value=False), \
                        mock.patch.object(
                            common, "open_bounded_log", return_value=log), \
                        mock.patch.object(
                            semantic.subprocess, "Popen") as popen:
                    result = semantic.ensure_fresh_async()
                self.assertEqual(result["state"], "running")
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    self._expected(base, external_job))
                log.close.assert_called_once_with()

    def test_semantic_refs_uses_background_job_policy(self) -> None:
        base = 0x08004208
        for external_job in (False, True):
            with self.subTest(external_job=external_job):
                log = mock.Mock()
                with mock.patch.object(common, "WIN", True), \
                        mock.patch.object(proc, "WIN", True), \
                        mock.patch.object(
                            proc, "_DESCENDANT_JOB_HANDLE", None), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_PID", None), \
                        mock.patch.object(
                            proc, "_windows_current_process_in_job",
                            return_value=external_job), \
                        mock.patch.object(
                            proc, "process_start_identity",
                            return_value=None), \
                        mock.patch.object(semantic.sys, "platform", "win32"), \
                        mock.patch.object(
                            semantic, "_background_refresh_disabled",
                            return_value=None), \
                        mock.patch.object(
                            semantic, "runtime_dependencies_available",
                            return_value=True), \
                        mock.patch.object(
                            semantic, "embed_running", return_value=False), \
                        mock.patch.object(
                            common, "open_bounded_log", return_value=log), \
                        mock.patch.object(
                            semantic.subprocess, "Popen") as popen:
                    result = semantic.ensure_refs_async()
                self.assertEqual(result["state"], "running")
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    self._expected(base, external_job))
                log.close.assert_called_once_with()

    def test_segment_compactor_uses_background_job_policy(self) -> None:
        base = 0x08004208
        for external_job in (False, True):
            with self.subTest(external_job=external_job):
                log = mock.Mock()
                manifest = mock.Mock()
                with mock.patch.object(common, "WIN", True), \
                        mock.patch.object(proc, "WIN", True), \
                        mock.patch.object(
                            proc, "_DESCENDANT_JOB_HANDLE", None), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_PID", None), \
                        mock.patch.object(
                            proc, "_windows_current_process_in_job",
                            return_value=external_job), \
                        mock.patch.object(
                            proc, "process_start_identity",
                            return_value=None), \
                        mock.patch.object(embed.sys, "platform", "win32"), \
                        mock.patch.object(
                            embedding_segments, "load_manifest",
                            return_value=manifest), \
                        mock.patch.object(
                            embedding_segments, "prune_orphans"), \
                        mock.patch.object(
                            removal_fence, "background_removal_active",
                            return_value=False), \
                        mock.patch.object(
                            common, "open_bounded_log", return_value=log), \
                        mock.patch.object(
                            embed.subprocess, "Popen") as popen:
                    launched = embed._schedule_segment_compaction(
                        refresh_metadata=True)
                self.assertTrue(launched)
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    self._expected(base, external_job))
                log.close.assert_called_once_with()

    def test_semantic_worker_uses_background_job_policy(self) -> None:
        base = 0x08000208
        nonce = "a" * 32
        claim = mock.Mock()
        claim.snapshot.raw = json.dumps({
            "pid": os.getpid(), "at": time.time(),
            "process_start": "birth", "nonce": nonce,
        }, separators=(",", ":")).encode()
        for external_job in (False, True):
            with self.subTest(external_job=external_job):
                log = mock.Mock()
                process = mock.Mock()
                with mock.patch.object(common, "WIN", True), \
                        mock.patch.object(proc, "WIN", True), \
                        mock.patch.object(
                            proc, "_DESCENDANT_JOB_HANDLE", None), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                        mock.patch.object(
                            proc, "_DESCENDANT_LIFETIME_PID", None), \
                        mock.patch.object(
                            proc, "_windows_current_process_in_job",
                            return_value=external_job), \
                        mock.patch.object(
                            proc, "process_start_identity",
                            return_value=None), \
                        mock.patch.object(semworker.sys, "platform", "win32"), \
                        mock.patch.object(
                            common, "open_bounded_log", return_value=log), \
                        mock.patch.object(
                            semworker, "loopback_bind_status",
                            return_value={"bindable": True, "reason": None}), \
                        mock.patch.object(
                            semworker.subprocess, "Popen",
                            return_value=process) as popen:
                    returned = semworker._spawn_worker(claim)
                self.assertIs(returned, process)
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    self._expected(base, external_job))
                self.assertEqual(
                    popen.call_args.kwargs["env"][
                        semworker._LAUNCH_CLAIM_ENV],
                    nonce)
                log.close.assert_called_once_with()

    def test_indexd_uses_background_job_policy(self) -> None:
        base = 0x00000208
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        process = mock.Mock(pid=4242)
        guard = mock.Mock()
        log = mock.Mock()
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            patches = (
                mock.patch.object(common, "DATA_DIR", Path(td)),
                mock.patch.object(common, "WIN", True),
                mock.patch.object(proc, "WIN", True),
                mock.patch.object(proc, "_DESCENDANT_JOB_HANDLE", None),
                mock.patch.object(proc, "_DESCENDANT_LIFETIME_BOUND", False),
                mock.patch.object(proc, "_DESCENDANT_LIFETIME_PID", None),
                mock.patch.object(
                    proc, "_windows_current_process_in_job", return_value=True),
                mock.patch.object(
                    removal_fence, "background_removal_active", return_value=False),
                mock.patch.object(indexd_runtime, "_clear_own_spawn_guard"),
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd", return_value=True),
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=absent),
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"),
                mock.patch.object(
                    common.ownerfile, "create_exclusive", return_value=guard),
                mock.patch.object(common, "open_bounded_log", return_value=log),
                mock.patch.object(
                    indexd_runtime, "_publish_spawn_child",
                    return_value=common.ownerfile.ProcessOwner.DEAD),
                mock.patch.object(
                    indexd_runtime, "_inspect_spawn_guard",
                    return_value=mock.Mock()),
                mock.patch.object(
                    indexd_runtime, "_settle_spawn_child",
                    return_value=indexd_runtime._SpawnChildState.ABSENT),
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True),
                mock.patch.object(indexd_runtime, "_release_spawn_guard"),
            )
            for patch in patches:
                stack.enter_context(patch)
            popen = stack.enter_context(mock.patch.object(
                common.subprocess, "Popen", return_value=process))
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            base | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)
        log.close.assert_called_once_with()

    def test_uninstall_sentinel_uses_background_job_policy(self) -> None:
        created = mock.Mock(returncode=0)
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    proc, "_DESCENDANT_JOB_HANDLE", None), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_PID", None), \
                mock.patch.object(
                    proc, "_windows_current_process_in_job",
                    return_value=True), \
                mock.patch.object(teach, "_atomic_write_text"), \
                mock.patch.object(
                    teach.subprocess, "run", return_value=created), \
                mock.patch.object(
                    teach.subprocess, "Popen") as popen, \
                mock.patch.object(teach, "_pythonw", return_value="pythonw.exe"), \
                mock.patch.object(
                    teach, "sentinel_armed", return_value=True):
            self.assertTrue(teach._sentinel_install_win([]))
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            0x08000008 | common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)

    def test_uninstall_sentinel_reports_denied_breakaway(self) -> None:
        created = mock.Mock(returncode=0)
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    proc, "_DESCENDANT_JOB_HANDLE", None), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_PID", None), \
                mock.patch.object(
                    proc, "_windows_current_process_in_job",
                    return_value=True), \
                mock.patch.object(teach, "_atomic_write_text"), \
                mock.patch.object(
                    teach.subprocess, "run", return_value=created), \
                mock.patch.object(
                    teach.subprocess, "Popen",
                    side_effect=PermissionError("job denies breakaway")) as popen, \
                mock.patch.object(teach, "_pythonw", return_value="pythonw.exe"), \
                mock.patch.object(
                    teach, "sentinel_armed", return_value=True):
            self.assertFalse(teach._sentinel_install_win([]))
        popen.assert_called_once()
        self.assertTrue(
            popen.call_args.kwargs["creationflags"]
            & common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)

    def test_denied_breakaway_is_not_retried_inherited(self) -> None:
        claim = mock.Mock()
        claim.snapshot.raw = json.dumps({
            "pid": os.getpid(), "at": time.time(),
            "process_start": "birth", "nonce": "a" * 32,
        }, separators=(",", ":")).encode()
        log = mock.Mock()
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    proc, "_DESCENDANT_JOB_HANDLE", None), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_BOUND", False), \
                mock.patch.object(
                    proc, "_DESCENDANT_LIFETIME_PID", None), \
                mock.patch.object(
                    proc, "_windows_current_process_in_job",
                    return_value=True), \
                mock.patch.object(
                    proc, "process_start_identity",
                    return_value=None), \
                mock.patch.object(semworker.sys, "platform", "win32"), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    semworker, "loopback_bind_status",
                    return_value={"bindable": True, "reason": None}), \
                mock.patch.object(
                    semworker.subprocess, "Popen",
                    side_effect=PermissionError("job denies breakaway")) as popen:
            with self.assertRaises(PermissionError):
                semworker._spawn_worker(claim)
        popen.assert_called_once()
        self.assertTrue(
            popen.call_args.kwargs["creationflags"]
            & common._WINDOWS_CREATE_BREAKAWAY_FROM_JOB)
        log.close.assert_called_once_with()

    def test_sync_refresh_remains_bound_to_its_caller(self) -> None:
        log_context = mock.MagicMock()
        process = mock.Mock()
        process.wait.return_value = 0
        with mock.patch.object(semantic.sys, "platform", "win32"), \
                mock.patch.object(
                    semantic, "runtime_dependencies_available",
                    return_value=True), \
                mock.patch.object(
                    semantic, "embedding_coherence",
                    side_effect=[
                        {"coherent": False}, {"coherent": True}]), \
                mock.patch.object(
                    semantic, "embed_running", return_value=False), \
                mock.patch.object(
                    semantic, "_needs_unverified_bundle_rebuild",
                    return_value=False), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log_context), \
                mock.patch.object(
                    common, "windows_background_child_flags") as policy, \
                mock.patch.object(
                    semantic.subprocess, "Popen",
                    return_value=process) as popen:
            result = semantic.refresh_embeddings_sync()
        self.assertTrue(result["ok"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08004000)
        policy.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object contract")
    def test_owned_job_kills_sync_child_but_detached_work_escapes(self) -> None:
        child = "import time; time.sleep(30)"
        parent = (
            "import common,json,subprocess,sys\n"
            "assert common.bind_descendants_to_process_lifetime()\n"
            "sync=subprocess.Popen([sys.executable,'-c',sys.argv[1]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True,"
            "creationflags=subprocess.CREATE_NO_WINDOW)\n"
            "flags=common.windows_detached_child_flags("
            "subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)\n"
            "detached=subprocess.Popen([sys.executable,'-c',sys.argv[1]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,close_fds=True,creationflags=flags)\n"
            "birth=common.process_start_identity(detached.pid)\n"
            "print(json.dumps({'sync':sync.pid,'detached':detached.pid,"
            "'birth':birth}),flush=True)\n"
        )
        process = subprocess.run(
            [sys.executable, "-c", parent, child],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10)
        self.assertEqual(
            process.returncode, 0, f"{process.stdout}\n{process.stderr}")
        record = json.loads(process.stdout.strip().splitlines()[-1])
        birth = str(record.get("birth") or "")
        try:
            self.assertTrue(birth)
            deadline = time.monotonic() + 5.0
            while (common.pid_alive(record["sync"])
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertFalse(common.pid_alive(record["sync"]))
            self.assertTrue(common.pid_alive(record["detached"]))
        finally:
            if birth:
                self.assertTrue(common.terminate_exact_process_tree(
                    record["detached"], birth, wait_s=5.0))


if __name__ == "__main__":
    unittest.main()
