from __future__ import annotations

import contextlib
import errno
import io
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
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import lifetime  # noqa: E402
import ownerfile  # noqa: E402
import proc  # noqa: E402
import removal_fence  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402
import teach  # noqa: E402


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class PosixProcessTreeTerminationTests(unittest.TestCase):
    def test_group_leader_drains_its_process_group(self) -> None:
        pid = 4242
        birth = "birth"
        alive = {"root": True, "group": True}
        signals = []

        def kill_group(_group: int, sent: int) -> None:
            if sent == 0:
                if not alive["group"]:
                    raise ProcessLookupError
                return
            signals.append(sent)
            alive["root"] = False
            alive["group"] = False

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: alive["root"]), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(proc.os, "kill") as kill:
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertTrue(stopped)
        self.assertEqual(signals, [signal.SIGTERM])
        kill.assert_not_called()

    def test_group_escalates_when_a_descendant_ignores_term(self) -> None:
        pid = 4242
        birth = "birth"
        alive = {"root": True, "group": True}
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent == 0:
                if not alive["group"]:
                    raise ProcessLookupError
                return
            signals.append(sent)
            if sent == signal.SIGTERM:
                alive["root"] = False
            elif sent == signal.SIGKILL:
                alive["group"] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: alive["root"]), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    side_effect=lambda _group: alive["group"]), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: not alive["root"]), \
                mock.patch.object(
                    proc, "_original_group_survivor",
                    return_value=True), \
                mock.patch.object(proc.os, "kill") as kill, \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2)
        self.assertTrue(stopped)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        kill.assert_not_called()

    def test_zombie_leader_does_not_block_descendant_escalation(self) -> None:
        pid = 4242
        birth = "birth"
        state = {"root": "exact", "group_live": True}
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent:
                signals.append(sent)
            if sent == signal.SIGTERM:
                state["root"] = "zombie"
            elif sent == signal.SIGKILL:
                state["group_live"] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: (
                        birth if state["root"] == "exact" else None)), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    side_effect=lambda _group: state["group_live"]), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: state["root"] != "exact"), \
                mock.patch.object(
                    proc, "_original_group_survivor",
                    return_value=True), \
                mock.patch.object(proc.os, "waitid", None, create=True), \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2)
        self.assertTrue(stopped)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_unverifiable_transition_without_exit_witness_does_not_escalate(
            self) -> None:
        pid = 4242
        birth = "birth"
        exact = [True]
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent:
                signals.append(sent)
            if sent == signal.SIGTERM:
                exact[0] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth if exact[0] else None), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    return_value=False), \
                mock.patch.object(proc.os, "waitid", None, create=True), \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2, require_bound_tree=True)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])

    def test_positive_exit_witness_keeps_unknown_nonchild_group_closed(
            self) -> None:
        pid = 4242
        birth = "birth"
        exact = [True]
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent:
                signals.append(sent)
            if sent == signal.SIGTERM:
                exact[0] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth if exact[0] else None), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_watch_process_group_members",
                    return_value=[]), \
                mock.patch.object(
                    proc, "_process_group_active", return_value=None), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: not exact[0]), \
                mock.patch.object(
                    proc.os, "waitpid",
                    side_effect=ChildProcessError) as waitpid, \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2, require_bound_tree=True)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])
        waitpid.assert_called_once_with(pid, os.WNOHANG)

    def test_bound_tree_never_succeeds_while_a_dead_roots_group_is_live(
            self) -> None:
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(proc, "pid_alive", return_value=False), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True), \
                mock.patch.object(proc.os, "killpg") as killpg:
            stopped = proc.terminate_exact_process_tree(
                4242, "birth", wait_s=0.0, require_bound_tree=True)
        self.assertFalse(stopped)
        killpg.assert_not_called()

    def test_bound_tree_never_accepts_an_unverifiable_live_owner(self) -> None:
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=None), \
                mock.patch.object(
                    proc, "_process_group_active", return_value=False), \
                mock.patch.object(proc.os, "killpg") as killpg:
            stopped = proc.terminate_exact_process_tree(
                4242, "birth", wait_s=0.0, require_bound_tree=True)
        self.assertFalse(stopped)
        killpg.assert_not_called()

    def test_root_exit_before_first_group_signal_fails_closed(self) -> None:
        pid = 4242
        birth = "birth"
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc, "_watch_process_group_members",
                    return_value=[]), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    return_value=True), \
                mock.patch.object(proc.os, "killpg") as killpg:
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2, require_bound_tree=True)
        self.assertFalse(stopped)
        killpg.assert_not_called()

    def test_reused_group_after_root_exit_is_not_escalated(self) -> None:
        pid = 4242
        birth = "birth"
        root_alive = [True]
        group_live = [True]
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent == 0:
                return
            signals.append(sent)
            root_alive[0] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: root_alive[0]), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    side_effect=lambda _group: group_live[0]), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: not root_alive[0]), \
                mock.patch.object(
                    proc, "_original_group_survivor",
                    return_value=False), \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2, require_bound_tree=True)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])

    def test_root_exit_during_final_group_scan_requires_original_survivor(
            self) -> None:
        pid = 4242
        birth = "birth"
        state = {
            "owner_checks": 0,
            "root_alive": True,
            "group_live": True,
        }
        signals = []
        now = [100.0]

        def pid_alive(_pid: int) -> bool:
            state["owner_checks"] += 1
            return state["root_alive"]

        def group_active(_group: int) -> bool:
            if state["owner_checks"] >= 5:
                state["root_alive"] = False
            return state["group_live"]

        def kill_group(_group: int, sent: int) -> None:
            if sent:
                signals.append(sent)
            if sent == signal.SIGKILL:
                state["group_live"] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "pid_alive", side_effect=pid_alive), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_watch_process_group_members",
                    return_value=[]), \
                mock.patch.object(
                    proc, "_process_group_active",
                    side_effect=group_active), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: not state["root_alive"]), \
                mock.patch.object(
                    proc, "_original_group_survivor",
                    return_value=False) as survivor, \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2, require_bound_tree=True)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])
        survivor.assert_called_once_with([], pid, pid)

    def test_group_survivor_requires_the_original_birth_identity(self) -> None:
        witness = mock.Mock()
        witness.exited.return_value = False
        watched = [
            proc._WatchedGroupMember(5001, "child-a", witness)]
        observed = proc._PosixProcessObservation("active", 4242)
        for current_birth, expected in (
                ("child-a", True), ("child-b", False)):
            with self.subTest(current_birth=current_birth), \
                    mock.patch.object(
                        proc, "process_start_identity",
                        return_value=current_birth), \
                    mock.patch.object(
                        proc, "_observe_posix_process",
                        return_value=observed), \
                    mock.patch.object(proc.os, "getsid", return_value=4242):
                self.assertIs(
                    proc._original_group_survivor(
                        watched, 4242, 4242),
                    expected)

    def test_sigkill_is_not_reported_as_drain_while_a_member_is_live(
            self) -> None:
        pid = 4242
        birth = "birth"
        root_alive = [True]
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent:
                signals.append(sent)
                root_alive[0] = False

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: root_alive[0]), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True), \
                mock.patch.object(
                    proc._ProcessExitWitness, "exited",
                    side_effect=lambda: not root_alive[0]), \
                mock.patch.object(
                    proc, "_original_group_survivor",
                    return_value=True), \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_nonleader_terminates_only_the_exact_process(self) -> None:
        pid = 4242
        birth = "birth"
        alive = [True]
        signals = []

        def kill(_pid: int, sent: int) -> None:
            signals.append(sent)
            alive[0] = False

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: alive[0]), \
                mock.patch.object(proc.os, "getpgid", return_value=4000), \
                mock.patch.object(proc.os, "getsid", return_value=4000), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(proc.os, "killpg") as killpg, \
                mock.patch.object(proc.os, "kill", side_effect=kill):
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertTrue(stopped)
        killpg.assert_not_called()
        self.assertEqual(signals, [signal.SIGTERM])

    def test_caller_group_is_never_group_signalled(self) -> None:
        pid = 4242
        birth = "birth"
        alive = [True]
        signals = []

        def kill(_pid: int, sent: int) -> None:
            signals.append(sent)
            alive[0] = False

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: alive[0]), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=pid), \
                mock.patch.object(proc.os, "killpg") as killpg, \
                mock.patch.object(proc.os, "kill", side_effect=kill):
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertTrue(stopped)
        killpg.assert_not_called()
        self.assertEqual(signals, [signal.SIGTERM])

    def test_birth_swap_before_signal_is_success_without_a_signal(self) -> None:
        pid = 4242
        birth = "birth"
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=(birth, "replacement")), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(proc.os, "killpg") as killpg, \
                mock.patch.object(proc.os, "kill") as kill:
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertTrue(stopped)
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_reused_owner_after_term_prevents_group_escalation(self) -> None:
        pid = 4242
        birth = "birth"
        current_birth = [birth]
        signals = []
        now = [100.0]

        def kill_group(_group: int, sent: int) -> None:
            if sent == 0:
                return
            signals.append(sent)
            current_birth[0] = "replacement"

        def sleep(delay: float) -> None:
            now[0] += delay

        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity",
                    side_effect=lambda _pid: current_birth[0]), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "killpg", side_effect=kill_group), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True), \
                mock.patch.object(proc.os, "kill") as kill, \
                mock.patch.object(
                    proc.time, "monotonic",
                    side_effect=lambda: now[0]), \
                mock.patch.object(proc.time, "sleep", side_effect=sleep):
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.2)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])
        kill.assert_not_called()

    def test_signal_timeout_returns_false(self) -> None:
        pid = 4242
        birth = "birth"
        signals = []
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=4000), \
                mock.patch.object(proc.os, "getsid", return_value=4000), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc.os, "kill",
                    side_effect=lambda _pid, sent: signals.append(sent)), \
                mock.patch.object(proc.time, "monotonic", return_value=100.0), \
                mock.patch.object(proc.time, "sleep") as sleep:
            stopped = proc.terminate_exact_process_tree(
                pid, birth, wait_s=0.0)
        self.assertFalse(stopped)
        self.assertEqual(signals, [signal.SIGTERM])
        sleep.assert_not_called()

    def test_signal_failure_keeps_a_still_exact_owner(self) -> None:
        pid = 4242
        birth = "birth"
        with mock.patch.object(proc, "WIN", False), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(proc, "pid_alive", return_value=True), \
                mock.patch.object(proc.os, "getpgid", return_value=pid), \
                mock.patch.object(proc.os, "getsid", return_value=pid), \
                mock.patch.object(proc.os, "getpgrp", return_value=9999), \
                mock.patch.object(
                    proc, "_watch_process_group_members",
                    return_value=[]), \
                mock.patch.object(
                    proc.os, "killpg",
                    side_effect=PermissionError("denied")) as killpg, \
                mock.patch.object(proc.os, "kill") as kill:
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertFalse(stopped)
        killpg.assert_called_once_with(pid, signal.SIGTERM)
        kill.assert_not_called()

    def test_real_private_group_drains_an_inherited_index_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-indexd-tree-") as raw:
            ready = Path(raw) / "child.pid"
            child = "import time\nwhile True: time.sleep(1)\n"
            parent = (
                "import pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]])\n"
                "pathlib.Path(sys.argv[2]).write_text(str(child.pid))\n"
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
                child_pid = int(ready.read_text())
                birth = proc.process_start_identity(process.pid)
                self.assertIsNotNone(birth)
                waiter.start()
                self.assertTrue(proc.terminate_exact_process_tree(
                    process.pid, str(birth), wait_s=5.0))
                waiter.join(timeout=2.0)
                self.assertFalse(proc.pid_alive(child_pid))
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                process.wait(timeout=5)
                if child_pid and proc.pid_alive(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "kernel exit witness",
    )
    def test_nonparent_zombie_leader_drains_its_stubborn_descendant(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-zombie-tree-") as raw:
            ready = Path(raw) / "ready.json"
            cleanup = Path(raw) / "cleanup"
            helper_code = (
                "import json,os,pathlib,signal,sys,time\n"
                "ready,cleanup=sys.argv[1:]\n"
                "root=os.fork()\n"
                "if root==0:\n"
                " os.setsid()\n"
                " read_fd,write_fd=os.pipe()\n"
                " child=os.fork()\n"
                " if child==0:\n"
                "  os.close(read_fd)\n"
                "  signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "  os.write(write_fd,b'1');os.close(write_fd)\n"
                "  while True: time.sleep(1)\n"
                " os.close(write_fd);os.read(read_fd,1);os.close(read_fd)\n"
                " target=pathlib.Path(ready);staged=target.with_suffix('.tmp')\n"
                " staged.write_text(json.dumps({"
                "'root':os.getpid(),'child':child}),encoding='ascii')\n"
                " os.replace(staged,target)\n"
                " while True: time.sleep(1)\n"
                "while not pathlib.Path(cleanup).exists(): time.sleep(.01)\n"
                "os.waitpid(root,0)\n"
            )
            helper = subprocess.Popen(
                [sys.executable, "-c", helper_code, str(ready), str(cleanup)])
            root_pid = child_pid = None
            try:
                deadline = time.monotonic() + 5.0
                while not ready.exists() and helper.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), f"helper exited {helper.poll()}")
                record = json.loads(ready.read_text(encoding="ascii"))
                root_pid = int(record["root"])
                child_pid = int(record["child"])
                birth = proc.process_start_identity(root_pid)
                self.assertIsNotNone(birth)
                self.assertTrue(proc.terminate_exact_process_tree(
                    root_pid, str(birth), wait_s=2.0,
                    require_bound_tree=True))
                self.assertEqual(
                    proc._observe_posix_process(root_pid).state, "zombie")
                self.assertIs(
                    proc._process_group_active(root_pid), False)
                self.assertFalse(proc.pid_alive(child_pid))
            finally:
                if root_pid is not None:
                    try:
                        os.killpg(root_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                cleanup.touch()
                try:
                    helper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    helper.kill()
                    helper.wait(timeout=5)

    def test_real_termination_preserves_subprocess_exit_status(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            started = proc.process_start_identity(process.pid)
            self.assertIsNotNone(started)
            with mock.patch.object(proc.os, "waitid", None, create=True):
                self.assertTrue(proc.terminate_exact_process_tree(
                    process.pid, str(started), wait_s=2.0))
            self.assertIn(
                process.wait(timeout=2.0),
                (-signal.SIGTERM, -signal.SIGKILL),
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2.0)

    def test_real_unreaped_zombie_is_not_executable_liveness(self) -> None:
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            [sys.executable, "-c", "import os,sys; os.read(int(sys.argv[1]),1)",
             str(read_fd)],
            pass_fds=(read_fd,), start_new_session=True,
        )
        os.close(read_fd)
        try:
            started = proc.process_start_identity(process.pid)
            self.assertIsNotNone(started)
            os.write(write_fd, b"x")
            os.close(write_fd)
            write_fd = -1
            deadline = time.monotonic() + 2.0
            observed = proc._observe_posix_process(process.pid)
            while observed.state != "zombie" and time.monotonic() < deadline:
                time.sleep(0.01)
                observed = proc._observe_posix_process(process.pid)
            self.assertEqual(observed.state, "zombie")
            os.kill(process.pid, 0)
            self.assertFalse(proc.pid_alive(process.pid))
            self.assertTrue(proc.terminate_exact_process_tree(
                process.pid, str(started), wait_s=0.2,
                require_bound_tree=True))
            self.assertEqual(process.wait(timeout=2.0), 0)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2.0)

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "kernel zombie observation",
    )
    def test_positive_exit_witness_reaps_a_blind_direct_child(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        birth = proc.process_start_identity(process.pid)
        self.assertIsNotNone(birth)
        real_alive = proc.pid_alive
        real_group_active = proc._process_group_active
        real_start = proc.process_start_identity
        saw_zombie = False

        def zombie_blind_start(pid: int) -> str | None:
            if proc._observe_posix_process(pid).state == "zombie":
                return None
            return real_start(pid)

        def zombie_blind_alive(pid: int) -> bool:
            nonlocal saw_zombie
            observed = proc._observe_posix_process(pid)
            if observed.state == "zombie":
                saw_zombie = True
                return True
            if observed.state == "active":
                return True
            return real_alive(pid)

        def zombie_blind_group(group: int) -> bool | None:
            nonlocal saw_zombie
            observed = proc._observe_posix_process(process.pid)
            if observed.state == "zombie":
                saw_zombie = True
                return None
            if observed.state == "active":
                return True
            return real_group_active(group)

        def positive_exit_witness() -> bool:
            nonlocal saw_zombie
            observed = proc._observe_posix_process(process.pid)
            if observed.state == "zombie":
                saw_zombie = True
                return True
            return False

        try:
            with mock.patch.object(
                    proc, "pid_alive", side_effect=zombie_blind_alive), \
                    mock.patch.object(
                        proc, "process_start_identity",
                        side_effect=zombie_blind_start), \
                    mock.patch.object(
                        proc, "_process_group_active",
                        side_effect=zombie_blind_group), \
                    mock.patch.object(
                        proc._ProcessExitWitness, "exited",
                        side_effect=positive_exit_witness), \
                    mock.patch.object(proc.os, "waitid", None, create=True):
                self.assertTrue(proc.terminate_exact_process_tree(
                    process.pid, str(birth), wait_s=1.0,
                    require_bound_tree=True))
            self.assertTrue(saw_zombie)
            self.assertFalse(real_alive(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2.0)

    def test_unready_spawn_drains_private_group_without_birth_identity(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-unready-tree-") as raw:
            ready = Path(raw) / "child.pid"
            child = "import time\nwhile True: time.sleep(1)\n"
            parent = (
                "import pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]])\n"
                "pathlib.Path(sys.argv[2]).write_text(str(child.pid))\n"
                "while True: time.sleep(1)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent, child, str(ready)],
                start_new_session=True)
            child_pid = None
            try:
                deadline = time.monotonic() + 5.0
                while not ready.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), f"fixture exited {process.poll()}")
                child_pid = int(ready.read_text())
                with mock.patch.object(
                        common, "process_start_identity", return_value=None):
                    self.assertTrue(indexd_runtime._stop_unready_child(process))
                self.assertFalse(common.pid_alive(child_pid))
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                process.wait(timeout=5)
                if child_pid and common.pid_alive(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    def test_exited_unready_child_is_never_signalled(self) -> None:
        process = mock.Mock(pid=4242)
        process.poll.return_value = 7
        with mock.patch.object(
                common, "process_start_identity") as process_start, \
                mock.patch.object(
                    common, "_process_group_active",
                    return_value=False) as group_active, \
                mock.patch.object(common.os, "getpgid") as getpgid, \
                mock.patch.object(common.os, "getsid") as getsid, \
                mock.patch.object(common.os, "killpg") as killpg:
            self.assertTrue(indexd_runtime._stop_unready_child(process))
        process_start.assert_not_called()
        group_active.assert_called_once_with(process.pid)
        getpgid.assert_not_called()
        getsid.assert_not_called()
        killpg.assert_not_called()

    def test_exited_unready_child_keeps_a_live_or_unknown_group(self) -> None:
        for group_active in (True, None):
            with self.subTest(group_active=group_active):
                process = mock.Mock(pid=4242)
                process.poll.return_value = 7
                with mock.patch.object(
                        common, "_process_group_active",
                        return_value=group_active), \
                        mock.patch.object(common.os, "killpg") as killpg:
                    self.assertFalse(
                        indexd_runtime._stop_unready_child(process))
                killpg.assert_not_called()

    def test_vanished_unready_root_keeps_a_live_process_group(self) -> None:
        process = mock.Mock(pid=4242)
        process.poll.side_effect = (None, 7)
        with mock.patch.object(
                common, "process_start_identity", return_value=None), \
                mock.patch.object(
                    common.os, "getpgid",
                    side_effect=ProcessLookupError), \
                mock.patch.object(
                    common, "_process_group_active", return_value=True), \
                mock.patch.object(common.os, "killpg") as killpg:
            self.assertFalse(indexd_runtime._stop_unready_child(process))
        killpg.assert_not_called()


@unittest.skipIf(os.name == "nt", "POSIX lifetime guardian")
class ReapedLeaderGroupDrainTests(unittest.TestCase):
    """After the guard reaps its leader, a pid==group occupant proves the
    numeric pgid was recycled by a stranger - it must never be signalled."""

    def test_recycled_pgid_is_never_signalled(self) -> None:
        with mock.patch.object(
                lifetime.common, "pid_alive", return_value=True), \
                mock.patch.object(lifetime.os, "killpg") as killpg:
            self.assertTrue(lifetime._drain_group(4242, reaped_leader=True))
        killpg.assert_not_called()

    def test_identity_bearing_occupant_blocks_the_signal(self) -> None:
        with mock.patch.object(
                lifetime.common, "pid_alive", return_value=False), \
                mock.patch.object(
                    lifetime.common, "process_start_identity",
                    return_value="stranger-birth"), \
                mock.patch.object(lifetime.os, "killpg") as killpg:
            self.assertTrue(lifetime._drain_group(4242, reaped_leader=True))
        killpg.assert_not_called()

    def test_unoccupied_reaped_group_still_drains_orphans(self) -> None:
        with mock.patch.object(
                lifetime.common, "pid_alive", return_value=False), \
                mock.patch.object(
                    lifetime.common, "process_start_identity",
                    return_value=None), \
                mock.patch.object(
                    lifetime.os, "killpg",
                    side_effect=ProcessLookupError) as killpg:
            self.assertTrue(lifetime._drain_group(4242, reaped_leader=True))
        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_recycle_between_term_and_kill_blocks_the_kill(self) -> None:
        with mock.patch.object(
                lifetime.common, "pid_alive", side_effect=[False, True]), \
                mock.patch.object(
                    lifetime.common, "process_start_identity",
                    return_value=None), \
                mock.patch.object(
                    lifetime, "_group_present", return_value=True), \
                mock.patch.object(lifetime.os, "killpg") as killpg:
            self.assertTrue(lifetime._drain_group(
                4242, grace_s=0.05, reaped_leader=True))
        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_live_root_drain_keeps_unconditional_signalling(self) -> None:
        with mock.patch.object(
                lifetime.os, "killpg",
                side_effect=ProcessLookupError) as killpg:
            self.assertTrue(lifetime._drain_group(4242))
        killpg.assert_called_once_with(4242, signal.SIGTERM)


@unittest.skipIf(os.name == "nt", "POSIX lifetime guardian")
class PosixLifetimeGuardianTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agrep-lifetime-")
        self.root = Path(self.temp.name)
        self.saved_paths = (
            common.DATA_DIR,
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
        )
        common.DATA_DIR = self.root
        indexd_runtime.INDEXD_LOCK_PATH = self.root / ".indexd.lock"
        indexd_runtime.INDEXD_READY_PATH = self.root / ".indexd.ready"
        indexd_runtime.INDEXD_CHILD_PATH = self.root / ".indexd.child"

    def tearDown(self) -> None:
        (
            common.DATA_DIR,
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
        ) = self.saved_paths
        self.temp.cleanup()

    @staticmethod
    def _wait_for(path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if path.read_bytes():
                    return
            except FileNotFoundError:
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"guardian exited {process.returncode}: {stdout}\n{stderr}")

    def test_wait_for_requires_a_published_readiness_payload(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(
                Path, "read_bytes", side_effect=[b"", b"ready"]) as read, \
                mock.patch.object(time, "sleep"):
            self._wait_for(self.root / "ready", process)
        self.assertEqual(read.call_count, 2)

    def _launch(
            self, token: str, fence: Path,
            command: list[str]) -> tuple[subprocess.Popen, int]:
        read_fd, write_fd = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable, str(Path(__file__).parent / "lifetime.py"),
                    "--parent-fd", str(read_fd),
                    "--fence", str(fence),
                    "--owner-token", token,
                    "--", *command,
                ],
                pass_fds=(read_fd,), start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
        finally:
            os.close(read_fd)
        return process, write_fd

    @staticmethod
    def _wait_dead(*pids: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(not common.pid_alive(pid) for pid in pids):
                return
            time.sleep(0.02)

    @staticmethod
    def _owner_snapshot(token: str) -> ownerfile.Snapshot:
        raw = (
            f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group=4242 token={token} time=100.000\n"
        ).encode("ascii")
        return ownerfile.Snapshot(
            (1, 2, len(raw), 100_000_000_000), 100.0, raw)

    def test_parent_control_eof_drains_target_and_grandchild(self) -> None:
        token = "a" * 32
        fence = self.root / "child.fence"
        ready = self.root / "target.json"
        grand_ready = self.root / "grand.ready"
        grand = (
            "import pathlib,signal,sys,time\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "pathlib.Path(sys.argv[1]).write_text('ready',encoding='utf-8')\n"
            "while True: time.sleep(1)\n"
        )
        target = (
            "import json,os,pathlib,subprocess,sys,time\n"
            "grand=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[3]])\n"
            "while not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.01)\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'target':os.getpid(),'grandchild':grand.pid}),encoding='utf-8')\n"
            "while True: time.sleep(1)\n"
        )
        guard, control_fd = self._launch(
            token, fence, [
                sys.executable, "-c", target, str(ready),
                grand, str(grand_ready)])
        target_pid = grandchild_pid = None
        try:
            self._wait_for(ready, guard)
            self._wait_for(fence, guard)
            record = json.loads(ready.read_text(encoding="utf-8"))
            target_pid = int(record["target"])
            grandchild_pid = int(record["grandchild"])
            os.close(control_fd)
            control_fd = -1
            guard.communicate(timeout=10)
            self._wait_dead(target_pid, grandchild_pid)
            self.assertFalse(common.pid_alive(target_pid))
            self.assertFalse(common.pid_alive(grandchild_pid))
            self.assertFalse(fence.exists())
        finally:
            if control_fd >= 0:
                os.close(control_fd)
            if guard.poll() is None:
                try:
                    os.killpg(guard.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                guard.wait(timeout=5)
            if target_pid is not None:
                try:
                    os.killpg(target_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in (target_pid, grandchild_pid):
                if pid is not None and common.pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    def test_natural_leader_exit_drains_its_live_descendant(self) -> None:
        fence = self.root / "natural-exit.fence"
        ready = self.root / "natural-exit.json"
        target = (
            "import json,os,pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;"
            "time.sleep(60)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'target':os.getpid(),'child':child.pid}),encoding='utf-8')\n"
        )
        guard, control_fd = self._launch(
            "9" * 32, fence,
            [sys.executable, "-c", target, str(ready)])
        child_pid = None
        try:
            self._wait_for(ready, guard)
            record = json.loads(ready.read_text(encoding="utf-8"))
            child_pid = int(record["child"])
            stdout, stderr = guard.communicate(timeout=5)
            self.assertEqual(guard.returncode, 0, f"{stdout}\n{stderr}")
            self._wait_dead(child_pid)
            self.assertFalse(common.pid_alive(child_pid))
            self.assertFalse(fence.exists())
        finally:
            os.close(control_fd)
            if guard.poll() is None:
                guard.kill()
                guard.wait(timeout=5)
            if child_pid is not None and common.pid_alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for stream in (guard.stdout, guard.stderr):
                if stream is not None:
                    stream.close()

    def test_zombie_only_group_is_not_executable_liveness(self) -> None:
        with mock.patch.object(
                lifetime.common, "_process_group_active",
                return_value=False) as active:
            self.assertFalse(lifetime._group_present(4242))
        active.assert_called_once_with(4242)

    def test_failed_group_drain_closes_but_preserves_the_fence(self) -> None:
        process = mock.Mock(pid=4242, returncode=0)
        process.stdout = None
        process.stderr = None
        process.poll.return_value = 0
        process.wait.return_value = 0
        fence_handle = mock.Mock(spec=ownerfile.Handle)
        with mock.patch.object(lifetime.signal, "signal"), \
                mock.patch.object(lifetime.os, "pipe", return_value=(51, 52)), \
                mock.patch.object(lifetime.os, "close"), \
                mock.patch.object(lifetime.os, "write"), \
                mock.patch.object(
                    lifetime.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    lifetime.common, "process_start_identity",
                    return_value="birth"), \
                mock.patch.object(
                    lifetime.ownerfile, "create_exclusive",
                    return_value=fence_handle), \
                mock.patch.object(
                    lifetime.selectors, "DefaultSelector"), \
                mock.patch.object(lifetime, "_parent_gone", return_value=False), \
                mock.patch.object(lifetime, "_group_present", return_value=True), \
                mock.patch.object(lifetime, "_drain_group", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = lifetime._guard(
                50, self.root / "child.fence", "d" * 32, ["fixture"])
        self.assertEqual(result, 125)
        self.assertIn(
            "index lifetime guard: target group drain failed",
            stderr.getvalue())
        fence_handle.release.assert_not_called()
        fence_handle.close.assert_called_once_with()

    def test_exec_gate_refusal_names_itself_on_stderr(self) -> None:
        gate_read, gate_write = os.pipe()
        os.close(gate_write)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(
                lifetime._exec_after_gate(gate_read, ["fixture"]), 125)
        self.assertIn(
            "index lifetime exec gate never opened", stderr.getvalue())

    def test_exec_gate_restores_signals_and_reports_launch_failure(self) -> None:
        with mock.patch.object(lifetime.os, "read", return_value=b"1"), \
                mock.patch.object(lifetime.os, "close"), \
                mock.patch.object(lifetime.signal, "signal") as set_signal, \
                mock.patch.object(
                    lifetime.os, "execvpe",
                    side_effect=FileNotFoundError(
                        errno.ENOENT, "missing fixture")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(lifetime._exec_after_gate(
                50, ["fixture"]), 127)
        self.assertIn("index lifetime exec failed", stderr.getvalue())
        expected = {
            getattr(lifetime.signal, name)
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
            if hasattr(lifetime.signal, name)
        }
        self.assertEqual(
            {call.args[0] for call in set_signal.call_args_list}, expected)
        self.assertTrue(all(
            call.args[1] is lifetime.signal.SIG_DFL
            for call in set_signal.call_args_list))

    def test_retire_child_drains_tree_after_guard_is_killed(self) -> None:
        token = "c" * 32
        owner_snapshot = self._owner_snapshot(token)
        fence = indexd_runtime.indexd_child_path(owner_snapshot)
        ready = self.root / "orphan.json"
        grand_ready = self.root / "orphan-grand.ready"
        grand = (
            "import pathlib,signal,sys,time\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "pathlib.Path(sys.argv[1]).write_text('ready',encoding='utf-8')\n"
            "while True: time.sleep(1)\n"
        )
        target = (
            "import json,os,pathlib,subprocess,sys,time\n"
            "grand=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[3]])\n"
            "while not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.01)\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            "'target':os.getpid(),'grandchild':grand.pid}),encoding='utf-8')\n"
            "while True: time.sleep(1)\n"
        )
        guard, control_fd = self._launch(
            token, fence, [
                sys.executable, "-c", target, str(ready),
                grand, str(grand_ready)])
        target_pid = grandchild_pid = None
        try:
            self._wait_for(ready, guard)
            self._wait_for(fence, guard)
            record = json.loads(ready.read_text(encoding="utf-8"))
            target_pid = int(record["target"])
            grandchild_pid = int(record["grandchild"])
            os.kill(guard.pid, signal.SIGKILL)
            guard.wait(timeout=5)
            self.assertTrue(indexd_runtime._retire_indexd_child(
                owner_snapshot, wait_s=5.0))
            guard.communicate(timeout=2)
            self._wait_dead(target_pid, grandchild_pid)
            self.assertFalse(common.pid_alive(target_pid))
            self.assertFalse(common.pid_alive(grandchild_pid))
            self.assertFalse(fence.exists())
        finally:
            os.close(control_fd)
            if guard.poll() is None:
                guard.kill()
                guard.wait(timeout=5)
            if target_pid is not None:
                try:
                    os.killpg(target_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in (target_pid, grandchild_pid):
                if pid is not None and common.pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            for stream in (guard.stdout, guard.stderr):
                if stream is not None:
                    stream.close()

    def test_killed_guard_closes_parent_pipes_before_target_retirement(
            self) -> None:
        token = "e" * 32
        owner_snapshot = self._owner_snapshot(token)
        fence = indexd_runtime.indexd_child_path(owner_snapshot)
        ready = self.root / "no-pipe-leak.pid"
        target = (
            "import os,pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii')\n"
            "print('target ready',flush=True)\n"
            "print('target ready',file=sys.stderr,flush=True)\n"
            "while True: time.sleep(1)\n"
        )
        owner = indexer.AutoIndexer(
            mock.Mock(), owns_lifetime=lambda: True,
            owner_snapshot=owner_snapshot)
        guard, control_fd = owner._launch_index_process(
            [sys.executable, "-c", target, str(ready)],
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            })
        target_pid = None
        try:
            self._wait_for(ready, guard)
            self._wait_for(fence, guard)
            target_pid = int(ready.read_text(encoding="ascii"))
            os.kill(guard.pid, signal.SIGKILL)
            started = time.monotonic()
            guard.communicate(timeout=2.0)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(common.pid_alive(target_pid))
            self.assertTrue(fence.exists())
            self.assertTrue(indexd_runtime._retire_indexd_child(
                owner_snapshot, wait_s=5.0))
            self.assertFalse(fence.exists())
        finally:
            os.close(control_fd)
            if guard.poll() is None:
                guard.kill()
                guard.wait(timeout=5)
            if target_pid is not None and common.pid_alive(target_pid):
                try:
                    os.killpg(target_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for stream in (guard.stdout, guard.stderr):
                if stream is not None:
                    stream.close()

    def test_guard_relays_only_bounded_output_tails(self) -> None:
        fence = self.root / "bounded-output.fence"
        target = (
            "import sys\n"
            "sys.stdout.write('x'*100000+'OUT-END')\n"
            "sys.stderr.write('y'*100000+'ERR-END')\n"
        )
        guard, control_fd = self._launch(
            "f" * 32, fence, [sys.executable, "-c", target])
        try:
            stdout, stderr = guard.communicate(timeout=10)
        finally:
            os.close(control_fd)
        self.assertEqual(guard.returncode, 0)
        self.assertLessEqual(
            len(stdout.encode("utf-8")), lifetime._OUTPUT_TAIL_BYTES)
        self.assertLessEqual(
            len(stderr.encode("utf-8")), lifetime._OUTPUT_TAIL_BYTES)
        self.assertTrue(stdout.endswith("OUT-END"))
        self.assertTrue(stderr.endswith("ERR-END"))

    def test_child_fence_protects_a_root_dead_generation(self) -> None:
        root_process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            start_new_session=True)
        root_start = common.process_start_identity(root_process.pid)
        self.assertIsNotNone(root_start)
        root_process.wait(timeout=5)
        token = "b" * 32
        raw = (
            f"pid={root_process.pid} start={root_start} "
            f"protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group={root_process.pid} token={token} time={time.time():.3f}\n"
        ).encode("ascii")
        indexd_runtime.INDEXD_LOCK_PATH.write_bytes(raw)
        snapshot = ownerfile.snapshot(indexd_runtime.INDEXD_LOCK_PATH)
        fence = indexd_runtime.indexd_child_path(snapshot)
        ready = self.root / "target.ready"
        target = (
            "import pathlib,sys,time\n"
            "pathlib.Path(sys.argv[1]).write_text('ready',encoding='utf-8')\n"
            "while True: time.sleep(1)\n"
        )
        guard, control_fd = self._launch(
            token, fence, [sys.executable, "-c", target, str(ready)])
        try:
            self._wait_for(ready, guard)
            self._wait_for(fence, guard)
            inspected = indexd_runtime._inspect_indexd_owner()
            self.assertIs(
                inspected.state, indexd_runtime._IndexdOwnerState.ORPHANED_GROUP)
            self.assertIsNone(indexd_runtime.acquire_indexd_owner())
            self.assertEqual(indexd_runtime.INDEXD_LOCK_PATH.read_bytes(), raw)
        finally:
            os.close(control_fd)
            guard.communicate(timeout=10)


class ProcessTreeDispatchTests(unittest.TestCase):
    def test_invalid_or_caller_pids_are_never_signalled(self) -> None:
        invalid = (0, -1, proc._MAX_PROCESS_ID + 1, os.getpid())
        with mock.patch.object(proc, "pid_alive") as alive, \
                mock.patch.object(
                    proc, "process_start_identity") as process_start, \
                mock.patch.object(
                    proc.os, "killpg", create=True) as killpg, \
                mock.patch.object(proc.os, "kill", create=True) as kill:
            tree_results = [
                proc.terminate_exact_process_tree(pid, "birth")
                for pid in invalid
            ]
            root_results = [
                proc.terminate_exact_process(pid, "birth")
                for pid in invalid
            ]
        self.assertEqual(tree_results, [False] * len(invalid))
        self.assertEqual(root_results, [False] * len(invalid))
        alive.assert_not_called()
        process_start.assert_not_called()
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_windows_dispatches_only_the_exact_root(self) -> None:
        pid = 4242
        birth = "birth"
        alive = [True]

        def terminate(_pid: int, _birth: str) -> bool:
            alive[0] = False
            return True

        with mock.patch.object(proc, "WIN", True), \
                mock.patch.object(
                    proc, "process_start_identity", return_value=birth), \
                mock.patch.object(
                    proc, "pid_alive",
                    side_effect=lambda _pid: alive[0]), \
                mock.patch.object(
                    proc, "terminate_exact_process",
                    side_effect=terminate) as terminate_root, \
                mock.patch.object(
                    proc.os, "getpgid", create=True) as getpgid, \
                mock.patch.object(
                    proc.os, "killpg", create=True) as killpg, \
                mock.patch.object(proc.os, "kill", create=True) as kill:
            stopped = proc.terminate_exact_process_tree(pid, birth)
        self.assertTrue(stopped)
        terminate_root.assert_called_once_with(pid, birth)
        getpgid.assert_not_called()
        killpg.assert_not_called()
        kill.assert_not_called()


class TeachIndexdTeardownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agrep-indexd-stop-")
        self.root = Path(self.temp.name)
        self.path = self.root / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.lock"
        self.legacy_path = self.root / ".indexd.lock"
        self.saved_paths = (
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
            indexd_runtime.LEGACY_INDEXD_LOCK_PATH,
        )
        indexd_runtime.INDEXD_LOCK_PATH = self.path
        indexd_runtime.INDEXD_READY_PATH = self.root / ".indexd.ready"
        indexd_runtime.INDEXD_CHILD_PATH = self.root / ".indexd.child"
        indexd_runtime.LEGACY_INDEXD_LOCK_PATH = self.legacy_path

    def tearDown(self) -> None:
        (
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
            indexd_runtime.LEGACY_INDEXD_LOCK_PATH,
        ) = self.saved_paths
        self.temp.cleanup()

    @staticmethod
    def _raw(*, token: str = "a" * 32) -> bytes:
        group = "job" if common.WIN else "4242"
        tree = (
            f" tree={common.WINDOWS_DESCENDANT_TREE}"
            if common.WIN else "")
        return (
            f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group={group}{tree} token={token} time=100.000\n"
        ).encode("ascii")

    def _stop(self) -> str:
        output = io.StringIO()
        with mock.patch.object(
                semworker, "stop_worker_and_wait",
                return_value={"ok": True, "pid": None}), \
                mock.patch.object(
                    semantic, "stop_background_writers_for_removal",
                    return_value={"ok": True, "stopped": ()}), \
                contextlib.redirect_stdout(output):
            self.last_clean = teach._stop_daemons()
        return output.getvalue()

    def test_successful_exact_owner_is_stopped_and_removed(self) -> None:
        raw = self._raw()
        self.path.write_bytes(raw)
        running = True

        def pid_alive(_pid: int) -> bool:
            return running

        def terminate(
                _pid: int, _start: str, *, wait_s: float,
                require_bound_tree: bool = False,
                term_grace_s: float | None = None) -> bool:
            nonlocal running
            self.assertTrue(require_bound_tree)
            self.assertEqual(term_grace_s, 4.0)
            running = False
            return True

        with mock.patch.object(common, "pid_alive", side_effect=pid_alive), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common, "_process_group_active", return_value=False), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=terminate) as terminate_call:
            output = self._stop()
        expected = {
            "wait_s": 5.0, "require_bound_tree": True,
            "term_grace_s": 4.0,
        }
        terminate_call.assert_called_once_with(4242, "birth", **expected)
        self.assertFalse(self.path.exists())
        self.assertIn("background processes stopped: indexd", output)
        self.assertTrue(self.last_clean)

    def test_termination_failure_preserves_the_exact_snapshot(self) -> None:
        raw = self._raw()
        self.path.write_bytes(raw)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=False) as terminate, \
                mock.patch.object(
                    ownerfile, "remove_exact",
                    wraps=ownerfile.remove_exact) as remove:
            output = self._stop()
        expected = {
            "wait_s": 5.0, "require_bound_tree": True,
            "term_grace_s": 4.0,
        }
        terminate.assert_called_once_with(4242, "birth", **expected)
        remove.assert_not_called()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertIn(
            "freshness daemon could not be stopped (compatible)", output)
        self.assertFalse(self.last_clean)

    def test_unverifiable_is_protected_and_stale_malformed_is_reclaimed(
            self) -> None:
        cases = (
            ("unverifiable", self._raw(), True),
            ("malformed", b"\xff malformed\n", False),
        )
        for label, raw, unverifiable in cases:
            with self.subTest(label=label):
                self.path.write_bytes(raw)
                if not unverifiable:
                    old = time.time() - 10.0
                    os.utime(self.path, (old, old))
                with mock.patch.object(
                        common, "pid_alive", return_value=True), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value=None), \
                        mock.patch.object(
                            common,
                            "terminate_exact_process_tree") as terminate:
                    output = self._stop()
                terminate.assert_not_called()
                if unverifiable:
                    self.assertEqual(self.path.read_bytes(), raw)
                    self.assertIn(
                        "freshness daemon could not be stopped (unverifiable)",
                        output)
                    self.assertFalse(self.last_clean)
                    self.path.unlink()
                else:
                    self.assertFalse(self.path.exists())
                    self.assertEqual(output, "")
                    self.assertTrue(self.last_clean)

    def test_different_body_replacement_during_termination_survives(self) -> None:
        original = self._raw()
        replacement = self._raw(token="b" * 32)
        self.path.write_bytes(original)

        def replace_owner(
                _pid: int, _birth: str, *, wait_s: float,
                require_bound_tree: bool = False,
                term_grace_s: float | None = None) -> bool:
            self.assertEqual(wait_s, 5.0)
            self.assertTrue(require_bound_tree)
            self.assertEqual(term_grace_s, 4.0)
            temporary = self.root / "next-owner"
            temporary.write_bytes(replacement)
            os.replace(temporary, self.path)
            return True

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=replace_owner):
            output = self._stop()
        self.assertEqual(self.path.read_bytes(), replacement)
        self.assertIn(
            "freshness daemon could not be stopped (compatible)", output)

    def test_inode_only_replacement_during_termination_survives(self) -> None:
        raw = self._raw()
        self.path.write_bytes(raw)
        os.utime(self.path, (100.0, 100.0))
        original = ownerfile.snapshot(self.path)
        replacement_snapshot = None

        def replace_owner(
                _pid: int, _birth: str, *, wait_s: float,
                require_bound_tree: bool = False,
                term_grace_s: float | None = None) -> bool:
            nonlocal replacement_snapshot
            self.assertEqual(wait_s, 5.0)
            self.assertTrue(require_bound_tree)
            self.assertEqual(term_grace_s, 4.0)
            temporary = self.root / "same-owner"
            temporary.write_bytes(raw)
            os.utime(
                temporary, ns=(original.identity[3], original.identity[3]))
            os.replace(temporary, self.path)
            replacement_snapshot = ownerfile.snapshot(self.path)
            return True

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=replace_owner):
            output = self._stop()
        current = ownerfile.snapshot(self.path)
        self.assertIsNotNone(replacement_snapshot)
        self.assertEqual(original.raw, replacement_snapshot.raw)
        self.assertEqual(original.identity[0], replacement_snapshot.identity[0])
        self.assertNotEqual(original.identity[1], replacement_snapshot.identity[1])
        self.assertEqual(original.identity[2:], replacement_snapshot.identity[2:])
        self.assertEqual(current.identity, replacement_snapshot.identity)
        self.assertEqual(current.raw, raw)
        self.assertIn(
            "freshness daemon could not be stopped (compatible)", output)

    def test_legacy_dead_owner_is_removed_from_one_snapshot(self) -> None:
        raw = b"pid=4242 start=birth\n"
        self.legacy_path.write_bytes(raw)
        if common.WIN:
            changed = time.time() - indexd_runtime._LEGACY_ORPHAN_GRACE_S - 1.0
            os.utime(self.legacy_path, (changed, changed))
        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=False):
            indexd_runtime._retire_legacy_indexd()
        self.assertFalse(self.legacy_path.exists())

    @unittest.skipUnless(common.WIN, "Windows legacy orphan grace")
    def test_windows_legacy_dead_owner_observes_orphan_grace(self) -> None:
        raw = b"pid=4242 start=birth\n"
        self.legacy_path.write_bytes(raw)
        with mock.patch.object(common, "pid_alive", return_value=False):
            self.assertFalse(indexd_runtime._retire_legacy_indexd())
        self.assertEqual(self.legacy_path.read_bytes(), raw)

    def test_legacy_dead_owner_keeps_a_present_process_group(self) -> None:
        raw = b"pid=4242 start=birth\n"
        self.legacy_path.write_bytes(raw)
        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True):
            self.assertFalse(indexd_runtime._retire_legacy_indexd())
        self.assertEqual(self.legacy_path.read_bytes(), raw)

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_live_legacy_retirement_preserves_a_surviving_group(self) -> None:
        raw = b"pid=4242 start=birth\n"
        self.legacy_path.write_bytes(raw)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=True), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True):
            self.assertFalse(indexd_runtime._retire_legacy_indexd(
                allow_retire=True, retire_budget_s=5.0))
        self.assertEqual(self.legacy_path.read_bytes(), raw)

    def test_windows_live_legacy_owner_is_never_root_only_retired(self) -> None:
        raw = b"pid=4242 start=birth\n"
        self.legacy_path.write_bytes(raw)
        with mock.patch.object(common, "WIN", True), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            self.assertFalse(indexd_runtime._retire_legacy_indexd(
                allow_retire=True, retire_budget_s=5.0))
        terminate.assert_not_called()
        self.assertEqual(self.legacy_path.read_bytes(), raw)

    @unittest.skipIf(common.WIN, "live legacy retirement is POSIX-only")
    def test_legacy_retirement_preserves_a_replacement(self) -> None:
        original = b"pid=4242 start=birth\n"
        replacement = b"pid=5252 start=next-birth\n"
        self.legacy_path.write_bytes(original)

        def replace_owner(
                _pid: int, _birth: str, *, wait_s: float) -> bool:
            self.assertEqual(wait_s, 5.0)
            temporary = self.root / "next-legacy-owner"
            temporary.write_bytes(replacement)
            os.replace(temporary, self.legacy_path)
            return True

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=replace_owner):
            indexd_runtime._retire_legacy_indexd(
                allow_retire=True, retire_budget_s=5.0)
        self.assertEqual(self.legacy_path.read_bytes(), replacement)

    def test_legacy_unknown_birth_is_never_retired_from_command_text(self) -> None:
        raw = b"pid=4242 start=unknown\n"
        self.legacy_path.write_bytes(raw)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate, \
                mock.patch.object(common.subprocess, "run") as probe:
            safe = indexd_runtime._retire_legacy_indexd()
        self.assertFalse(safe)
        terminate.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.legacy_path.read_bytes(), raw)

    def test_legacy_oversized_pid_is_reclaimed_without_an_os_probe(self) -> None:
        raw = f"pid={common._MAX_PROCESS_ID + 1} start=unknown\n".encode()
        self.legacy_path.write_bytes(raw)
        old = time.time() - 10.0
        os.utime(self.legacy_path, (old, old))
        self.assertTrue(indexd_runtime._retire_legacy_indexd())
        self.assertFalse(self.legacy_path.exists())

    def test_fresh_empty_legacy_publication_is_protected(self) -> None:
        self.legacy_path.write_bytes(b"")
        self.assertFalse(indexd_runtime._retire_legacy_indexd())
        self.assertTrue(self.legacy_path.exists())
        old = time.time() - indexd_runtime._INDEXD_PUBLICATION_GRACE_S
        os.utime(self.legacy_path, (old, old))
        self.assertTrue(indexd_runtime._retire_legacy_indexd())
        self.assertFalse(self.legacy_path.exists())

    def test_future_empty_legacy_publication_is_recovered(self) -> None:
        self.legacy_path.write_bytes(b"")
        future = time.time() + 3600.0
        os.utime(self.legacy_path, (future, future))
        self.assertTrue(indexd_runtime._retire_legacy_indexd())
        self.assertFalse(self.legacy_path.exists())

    def test_uninstall_uses_the_settled_semantic_stopper(self) -> None:
        output = io.StringIO()
        outcome = {
            "ok": True, "running": False, "pid": 4242,
            "stop_ack": True, "fallback_termination": False,
        }
        with mock.patch.object(
                indexd_runtime, "stop_indexd_owner", return_value=False), \
                mock.patch.object(indexd_runtime, "_legacy_indexd_live",
                                  return_value=False), \
                mock.patch.object(
                    semworker, "stop_worker_and_wait",
                    return_value=outcome) as stop, \
                mock.patch.object(
                    semantic, "stop_background_writers_for_removal",
                    return_value={"ok": True, "stopped": ()}), \
                contextlib.redirect_stdout(output):
            teach._stop_daemons()
        stop.assert_called_once_with()
        self.assertIn(
            "background processes stopped: semantic worker",
            output.getvalue())

    def test_failed_teardown_preserves_integration_and_sentinel(self) -> None:
        output = io.StringIO()
        fence = mock.Mock()
        with mock.patch.object(
                removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    removal_fence, "finish_background_removal_fence",
                    return_value=True) as finish, \
                mock.patch.object(teach, "_stop_daemons", return_value=False), \
                mock.patch.object(teach, "_remove_block") as remove_block, \
                mock.patch.object(teach, "_remove_skill") as remove_skill, \
                mock.patch.object(teach, "_sentinel_remove") as sentinel_remove, \
                contextlib.redirect_stdout(output):
            result = teach._remove()
        self.assertEqual(result, 1)
        self.assertIn("removal aborted", output.getvalue())
        remove_block.assert_not_called()
        remove_skill.assert_not_called()
        sentinel_remove.assert_not_called()
        finish.assert_called_once_with(fence)


if __name__ == "__main__":
    unittest.main()
