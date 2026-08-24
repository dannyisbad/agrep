from __future__ import annotations

import contextlib
from datetime import datetime
import io
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
import common  # noqa: E402
import indexd_runtime  # noqa: E402
import ownerfile  # noqa: E402
import proc  # noqa: E402
import indexd  # noqa: E402
from hookless import _log  # noqa: E402
from hookless import locators  # noqa: E402


PY_DIR = Path(__file__).resolve().parent


class IndexdOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agrep-indexd-owner-")
        self.root = Path(self.temp.name)
        self.path = self.root / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.lock"
        self.search_beat = self.root / f".agrep.search.v{indexd_runtime.INDEXD_PROTOCOL}"
        self.saved = (
            common.DATA_DIR,
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
            indexd_runtime.SEARCH_BEAT_PATH,
            indexd_runtime._SPAWN_GUARD_PATH,
            indexd_runtime._INDEXD_RESPONSE_PATH,
            proc._DESCENDANT_LIFETIME_BOUND,
            proc._DESCENDANT_LIFETIME_PID,
        )
        # P13: any test driving indexd.main() reaches enable_log_timestamps(),
        # which is process-permanent; give the interpreter back afterwards.
        self.saved_timestamps = _log._TIMESTAMPS
        common.DATA_DIR = self.root
        indexd_runtime.INDEXD_LOCK_PATH = self.path
        indexd_runtime.INDEXD_READY_PATH = self.root / ".indexd.ready"
        indexd_runtime.INDEXD_CHILD_PATH = self.root / ".indexd.child"
        indexd_runtime.SEARCH_BEAT_PATH = self.search_beat
        indexd_runtime._SPAWN_GUARD_PATH = self.root / ".indexd.spawn"
        indexd_runtime._INDEXD_RESPONSE_PATH = self.root / ".indexd.probe"
        proc._DESCENDANT_LIFETIME_BOUND = True
        proc._DESCENDANT_LIFETIME_PID = os.getpid()
        self.handles: list[ownerfile.Handle] = []

    def tearDown(self) -> None:
        for handle in self.handles:
            try:
                handle.close()
            except OSError:
                pass
        (
            common.DATA_DIR,
            indexd_runtime.INDEXD_LOCK_PATH,
            indexd_runtime.INDEXD_READY_PATH,
            indexd_runtime.INDEXD_CHILD_PATH,
            indexd_runtime.SEARCH_BEAT_PATH,
            indexd_runtime._SPAWN_GUARD_PATH,
            indexd_runtime._INDEXD_RESPONSE_PATH,
            proc._DESCENDANT_LIFETIME_BOUND,
            proc._DESCENDANT_LIFETIME_PID,
        ) = self.saved
        _log._TIMESTAMPS = self.saved_timestamps
        self.temp.cleanup()

    def _raw(
            self, *, pid: int = 4242, process_start: str = "birth",
            protocol: int | None = None, package: str | None = None,
            build: str | None = None, writer: str | None = None,
            token: str = "a" * 32,
            group: int | str | None = None, tree: str | None = None,
            home: str | None = None, data: str | None = None,
            stamped: bool = True,
            created_at: float = 100.0) -> bytes:
        process_group = ("job" if common.WIN else pid) if group is None else group
        tree_name = (
            common.WINDOWS_DESCENDANT_TREE
            if tree is None and common.WIN else (tree or ""))
        tree_field = f" tree={tree_name}" if tree_name else ""
        world_fields = ""
        if stamped:
            home_stamp = indexd_runtime._world_stamp(
                locators.discovery_home() if home is None else home)
            data_stamp = indexd_runtime._world_stamp(
                common.DATA_DIR if data is None else data)
            world_fields = f" home={home_stamp} data={data_stamp}"
        return (
            f"pid={pid} start={process_start} "
            f"protocol={indexd_runtime.INDEXD_PROTOCOL if protocol is None else protocol} "
            f"package={common.package_version() if package is None else package} "
            f"build={indexd_runtime.INDEXD_BUILD_ID if build is None else build} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin()) if writer is None else writer} "
            f"group={process_group}{tree_field} token={token} "
            f"time={created_at:.3f}{world_fields}\n"
        ).encode("ascii")

    def _write(self, raw: bytes, *, age: float = 0.0,
               now: float = 100.0) -> None:
        self.path.write_bytes(raw)
        changed = now - age
        os.utime(self.path, (changed, changed))

    def _acquire(self) -> ownerfile.Handle | None:
        handle = indexd_runtime.acquire_indexd_owner()
        if handle is not None:
            self.handles.append(handle)
        return handle

    def _release(self, handle: ownerfile.Handle) -> bool:
        return handle.release(tombstone=True, require_stable_mtime=True)

    def _snapshot(self, raw: bytes | None = None) -> ownerfile.Snapshot:
        body = raw or self._raw()
        return ownerfile.Snapshot(
            (1, 2, len(body), 100_000_000_000), 100.0, body)

    def test_wire_mode_retained_handle_and_exact_release(self) -> None:
        create = ownerfile.create_exclusive
        process_group = "job" if common.WIN else 31337
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgrp", return_value=31337, create=True), \
                mock.patch.object(common.secrets, "token_hex",
                                  return_value="c" * 32), \
                mock.patch.object(common.time, "time", return_value=123.5), \
                mock.patch.object(
                    ownerfile, "create_exclusive", wraps=create) as create_call:
            handle = self._acquire()
        self.assertIsInstance(handle, ownerfile.Handle)
        self.assertIsNotNone(handle.fd)
        expected = self._raw(
            pid=os.getpid(), process_start="birth",
            token="c" * 32, group=process_group, created_at=123.5)
        self.assertEqual(handle.snapshot.raw, expected)
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertTrue(expected.endswith(b"\n"))
        self.assertEqual(expected.count(b"\n"), 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        create_call.assert_called_once()
        self.assertEqual(create_call.call_args.args, (self.path, expected))
        self.assertEqual(create_call.call_args.kwargs, {
            "mode": 0o600,
            "fsync": True,
            "retain_fd": True,
        })
        fd = handle.fd
        self.assertTrue(self._release(handle))
        self.assertFalse(self.path.exists())
        self.assertIsNone(handle.fd)
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_indexd_acquire_delegates_to_runtime_owner(self) -> None:
        sentinel = mock.Mock(spec=ownerfile.Handle)
        with mock.patch.object(
                indexd_runtime, "acquire_indexd_owner",
                return_value=sentinel) as acquire:
            self.assertIs(indexd._acquire(), sentinel)
        acquire.assert_called_once_with()

    def test_missing_self_birth_identity_never_publishes_an_owner(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value=None):
            self.assertIsNone(self._acquire())
        self.assertFalse(self.path.exists())

    def test_unbound_lifetime_never_publishes_an_owner(self) -> None:
        proc._DESCENDANT_LIFETIME_BOUND = False
        self.assertIsNone(self._acquire())
        self.assertFalse(self.path.exists())

    def test_inherited_lifetime_proof_never_publishes_an_owner(self) -> None:
        proc._DESCENDANT_LIFETIME_PID = os.getpid() + 1
        self.assertIsNone(self._acquire())
        self.assertFalse(self.path.exists())

    @unittest.skipIf(os.name == "nt", "fork is POSIX-only")
    def test_self_birth_cache_is_not_inherited_across_fork(self) -> None:
        parent_identity = common.process_start_identity(os.getpid())
        self.assertIsNotNone(parent_identity)
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                identity = common.process_start_identity(os.getpid()) or ""
                os.write(write_fd, identity.encode("ascii"))
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            child_identity = os.read(read_fd, 256).decode("ascii")
        finally:
            os.close(read_fd)
        waited, status = os.waitpid(child_pid, 0)
        self.assertEqual(waited, child_pid)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertTrue(child_identity)
        self.assertNotEqual(child_identity, parent_identity)

    def test_compatible_owner_is_busy_even_when_ancient(self) -> None:
        raw = self._raw()
        self._write(raw, age=24 * 3600)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            inspection = indexd_runtime._inspect_indexd_owner()
            handle = self._acquire()
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.COMPATIBLE)
        self.assertEqual(inspection.snapshot.raw, raw)
        self.assertIsNone(handle)
        terminate.assert_not_called()
        self.assertEqual(self.path.read_bytes(), raw)

    def _inspect_live_exact(self) -> indexd_runtime._IndexdOwnerInspection:
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True):
            return indexd_runtime._inspect_indexd_owner()

    def test_matching_world_stamps_stay_compatible(self) -> None:
        raw = self._raw()
        self._write(raw)
        inspection = self._inspect_live_exact()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.COMPATIBLE)
        self.assertIsNone(inspection.mismatch)
        self.assertEqual(
            inspection.home, os.path.abspath(locators.discovery_home()))
        self.assertEqual(
            inspection.data, os.path.abspath(os.fspath(common.DATA_DIR)))

    def test_divergent_home_stamp_is_mismatched_naming_both_worlds(
            self) -> None:
        foreign = os.path.join(os.sep, "poisoned sandbox", "home")
        raw = self._raw(home=foreign)
        self._write(raw)
        stamp = indexd_runtime._world_stamp(foreign)
        self.assertNotIn(" ", stamp)
        self.assertIn(f" home={stamp} ".encode("ascii"), raw)
        inspection = self._inspect_live_exact()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.MISMATCHED)
        # The percent-encoded space survives the single-token parser.
        self.assertEqual(inspection.home, os.path.abspath(foreign))
        self.assertIn("freshness daemon (pid 4242)", inspection.mismatch)
        self.assertIn(f"home {os.path.abspath(foreign)}", inspection.mismatch)
        self.assertIn(
            f"this invocation resolves home "
            f"{os.path.abspath(locators.discovery_home())}",
            inspection.mismatch)
        self.assertIn(
            "stop that daemon or unset the divergent environment",
            inspection.mismatch)

    def test_divergent_data_stamp_is_mismatched_naming_both_worlds(
            self) -> None:
        foreign = os.path.join(os.sep, "other world", "data")
        raw = self._raw(data=foreign)
        self._write(raw)
        inspection = self._inspect_live_exact()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.MISMATCHED)
        self.assertEqual(inspection.data, os.path.abspath(foreign))
        self.assertIn(f"data {os.path.abspath(foreign)}", inspection.mismatch)
        self.assertIn(
            f"data {os.path.abspath(os.fspath(common.DATA_DIR))}",
            inspection.mismatch)

    def test_legacy_lock_without_stamps_never_infers_mismatch(self) -> None:
        raw = self._raw(stamped=False)
        self._write(raw)
        inspection = self._inspect_live_exact()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.COMPATIBLE)
        self.assertIsNone(inspection.mismatch)
        self.assertIsNone(inspection.home)
        self.assertIsNone(inspection.data)

    def test_process_states_are_classified_from_one_snapshot(self) -> None:
        cases = (
            ("compatible", True, "birth",
             indexd_runtime._IndexdOwnerState.COMPATIBLE),
            ("dead", False, None, indexd_runtime._IndexdOwnerState.DEAD),
            ("reused", True, "other", indexd_runtime._IndexdOwnerState.REUSED),
            ("unverifiable", True, None,
             indexd_runtime._IndexdOwnerState.UNVERIFIABLE),
        )
        for label, alive, actual, expected in cases:
            with self.subTest(label=label):
                raw = self._raw()
                self._write(raw)
                with mock.patch.object(
                        common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=lambda pid, found=actual:
                            found if pid == 4242 else "self-birth"), \
                        mock.patch.object(
                            common.os, "getpgid", return_value=4242,
                            create=True), \
                        mock.patch.object(
                            common.os, "getsid", return_value=4242,
                            create=True), \
                        mock.patch.object(
                            proc, "_process_group_present",
                            return_value=False):
                    inspection = indexd_runtime._inspect_indexd_owner()
                self.assertIs(inspection.state, expected)
                self.assertEqual(inspection.snapshot.raw, raw)
                self.assertEqual(inspection.pid, 4242)
                self.assertEqual(inspection.process_start, "birth")
                self.path.unlink()

    def test_dead_and_reused_are_reclaimed_but_unverifiable_is_protected(
            self) -> None:
        cases = (
            ("dead", False, None, True),
            ("reused", True, "other", True),
            ("unverifiable", True, None, False),
        )
        for label, alive, actual, acquired in cases:
            with self.subTest(label=label):
                original = self._raw()
                self._write(original)
                with mock.patch.object(
                        common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=lambda pid, found=actual:
                            found if pid == 4242 else "self-birth"), \
                        mock.patch.object(
                            proc, "_process_group_present",
                            return_value=False):
                    handle = self._acquire()
                self.assertEqual(handle is not None, acquired)
                if handle is None:
                    self.assertEqual(self.path.read_bytes(), original)
                else:
                    self.assertNotEqual(handle.snapshot.raw, original)
                    self.assertTrue(self._release(handle))
                self.path.unlink(missing_ok=True)

    def test_malformed_records_use_the_exact_three_second_boundary(self) -> None:
        malformed = (b"", b"{", b"\xff", b"pid=not-a-number\n")
        for body in malformed:
            with self.subTest(body=body):
                self._write(body, age=2.999)
                with mock.patch.object(common.time, "time", return_value=100.0):
                    fresh = indexd_runtime._inspect_indexd_owner()
                    protected = indexd_runtime._settle_indexd_owner()
                self.assertIs(
                    fresh.state, indexd_runtime._IndexdOwnerState.MALFORMED_FRESH)
                self.assertIs(
                    protected.state, indexd_runtime._IndexdOwnerState.MALFORMED_FRESH)
                self.assertEqual(self.path.read_bytes(), body)

                self._write(body, age=3.0)
                with mock.patch.object(common.time, "time", return_value=100.0):
                    stale = indexd_runtime._inspect_indexd_owner()
                    handle = self._acquire()
                self.assertIs(
                    stale.state, indexd_runtime._IndexdOwnerState.MALFORMED_STALE)
                self.assertIsInstance(handle, ownerfile.Handle)
                self.assertTrue(self._release(handle))

    def test_oversized_pid_is_malformed_without_a_process_probe(self) -> None:
        raw = self._raw(pid=common._MAX_PROCESS_ID + 1)
        self._write(raw, age=0.0)
        with mock.patch.object(common.time, "time", return_value=100.0), \
                mock.patch.object(
                    common, "pid_alive",
                    side_effect=AssertionError("oversized pid was probed")), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=AssertionError("oversized pid birth was probed")):
            inspection = indexd_runtime._inspect_indexd_owner()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.MALFORMED_FRESH)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_live_owner_with_an_invalid_token_is_never_reclaimed(self) -> None:
        raw = self._raw(token="not-a-generation-token")
        self._write(raw, age=10.0)
        with mock.patch.object(common.time, "time", return_value=100.0), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            inspection = indexd_runtime._inspect_indexd_owner()
            handle = self._acquire()
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIsNone(handle)
        terminate.assert_not_called()
        self.assertEqual(self.path.read_bytes(), raw)

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_oversized_process_group_is_unverifiable_without_exception(
            self) -> None:
        with mock.patch.object(
                common.os, "killpg",
                side_effect=OverflowError("group is outside pid_t")) as killpg:
            self.assertIsNone(common._process_group_present(10 ** 100))
        killpg.assert_called_once_with(10 ** 100, 0)

    def test_future_dated_malformed_owner_has_a_monotonic_bound(self) -> None:
        malformed = b"pid=not-a-number\n"
        self.path.write_bytes(malformed)
        future = time.time() + 3600.0
        os.utime(self.path, (future, future))
        with mock.patch.object(indexd_runtime, "_INDEXD_ACQUIRE_WAIT_S", 0.1):
            handle = self._acquire()
        self.assertIsInstance(handle, ownerfile.Handle)
        self.assertNotEqual(handle.snapshot.raw, malformed)
        self.assertTrue(self._release(handle))

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_reused_child_target_reclaims_its_own_recycled_group(
            self) -> None:
        owner = self._snapshot()
        token = indexd_runtime.indexd_generation_token(owner)
        fence = indexd_runtime.indexd_child_path(owner)
        fence.write_text(
            f"owner={token} guard=111 guard_start=guard-birth "
            "target=222 target_start=target-birth group=222\n",
            encoding="ascii")

        def alive(pid: int) -> bool:
            return pid == 222

        with mock.patch.object(common, "pid_alive", side_effect=alive), \
                mock.patch.object(
                    common, "process_start_identity",
                    return_value="replacement-birth"), \
                mock.patch.object(
                    proc, "_process_group_present",
                    return_value=True), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True):
            self.assertIs(indexd_runtime._indexd_child_active(owner), False)
        self.assertFalse(fence.exists())

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_dead_child_target_still_protects_its_orphaned_group(self) -> None:
        owner = self._snapshot()
        token = indexd_runtime.indexd_generation_token(owner)
        fence = indexd_runtime.indexd_child_path(owner)
        fence.write_text(
            f"owner={token} guard=111 guard_start=guard-birth "
            "target=222 target_start=target-birth group=222\n",
            encoding="ascii")
        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value=None), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True):
            self.assertIs(indexd_runtime._indexd_child_active(owner), True)
        self.assertTrue(fence.exists())

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_recycled_owner_pid_on_a_live_group_stays_reclaimable(
            self) -> None:
        raw = self._raw()
        self._write(raw, age=3600.0)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=lambda pid:
                    "stranger-birth" if pid == 4242 else "self-birth"), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True), \
                mock.patch.object(
                    proc, "_process_group_has_live_members",
                    return_value=True):
            inspection = indexd_runtime._inspect_indexd_owner()
            claimed = indexd_runtime.live_indexer_claim()
            handle = self._acquire()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.REUSED)
        self.assertNotIn(
            inspection.state, indexd_runtime._INDEXD_UNRETIRABLE_STATES)
        self.assertFalse(claimed)
        self.assertIsInstance(handle, ownerfile.Handle)
        self.assertNotEqual(handle.snapshot.raw, raw)
        self.assertTrue(self._release(handle))

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_a_real_recycled_leader_never_wedges_the_indexer(self) -> None:
        """The owner's wedge with no mocks: a live session leader holding the
        dead daemon's number. The kernel cannot reissue a PID while its group
        still names it, so the group probe proves nothing about the daemon."""
        leader = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True)
        self.addCleanup(leader.wait)
        self.addCleanup(leader.kill)
        self.assertEqual(os.getpgid(leader.pid), leader.pid)
        self.assertTrue(common.pid_alive(leader.pid))
        self.assertIsNotNone(common.process_start_identity(leader.pid))
        self.assertIs(common._process_group_active(leader.pid), True)

        self._write(self._raw(pid=leader.pid, process_start="dead-daemon"))
        inspection = indexd_runtime._inspect_indexd_owner()
        self.assertIs(
            inspection.state, indexd_runtime._IndexdOwnerState.REUSED)
        self.assertFalse(indexd_runtime.live_indexer_claim())
        handle = self._acquire()
        self.assertIsInstance(handle, ownerfile.Handle)
        self.assertTrue(self._release(handle))
        self.assertTrue(common.pid_alive(leader.pid))

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_owner_group_probe_is_skipped_only_for_a_recycled_leader(
            self) -> None:
        with mock.patch.object(
                proc, "_process_group_active", return_value=True) as probe:
            self.assertIs(common._owner_group_active(4242, 4242, True), False)
            self.assertEqual(probe.call_count, 0)
            self.assertIs(common._owner_group_active(4242, 4242, False), True)
            self.assertIs(common._owner_group_active(4242, 99, True), True)
        self.assertEqual(probe.call_count, 2)

    def test_malformed_child_fence_stays_protected(self) -> None:
        owner = self._snapshot()
        fence = indexd_runtime.indexd_child_path(owner)
        fence.write_bytes(b"malformed\n")
        changed = time.time() - indexd_runtime._INDEXD_PUBLICATION_GRACE_S - 3600.0
        os.utime(fence, (changed, changed))
        self.assertIsNone(indexd_runtime._indexd_child_active(owner))
        self.assertEqual(fence.read_bytes(), b"malformed\n")

    def test_incompatible_retirement_receives_only_the_acquisition_budget(
            self) -> None:
        raw = self._raw(build="older-build")
        self._write(raw)
        waits = []

        def refuse(
                _pid: int, _birth: str, *, wait_s: float,
                require_bound_tree: bool = False) -> bool:
            self.assertTrue(require_bound_tree)
            waits.append(wait_s)
            return False

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True), \
                mock.patch.object(indexd_runtime, "_INDEXD_ACQUIRE_WAIT_S", 0.6), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=refuse):
            self.assertIsNone(self._acquire())
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(
            waits[0], indexd_runtime._INDEXD_AUTORETIRE_MIN_S)
        self.assertLessEqual(waits[0], 0.601)

    def test_incompatible_owner_requires_explicit_proven_retirement(self) -> None:
        original = self._raw(build="old-build")
        self._write(original)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=False) as terminate:
            failed = indexd_runtime._settle_indexd_owner()
        self.assertIs(failed.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        self.assertEqual(self.path.read_bytes(), original)
        terminate.assert_not_called()

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=False) as terminate:
            failed = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(failed.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args, (4242, "birth"))
        self.assertGreater(terminate.call_args.kwargs["wait_s"], 0.0)
        self.assertLessEqual(terminate.call_args.kwargs["wait_s"], 5.0)
        self.assertTrue(
            terminate.call_args.kwargs.get("require_bound_tree", False))

        def retire(
                _pid: int, _start: str, *, wait_s: float,
                require_bound_tree: bool = False) -> bool:
            self.assertGreater(wait_s, 0.0)
            self.assertTrue(require_bound_tree)
            self.path.unlink()
            return True

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=retire):
            retired = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(retired.state, indexd_runtime._IndexdOwnerState.ABSENT)
        self.assertFalse(self.path.exists())

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_incompatible_retirement_preserves_a_surviving_group(self) -> None:
        original = self._raw(build="old-build")
        self._write(original)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_indexd_child_active", return_value=False), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True):
            settled = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(
            settled.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(common.WIN, "POSIX process-group proof")
    def test_stop_owner_preserves_a_surviving_group(self) -> None:
        snapshot = self._snapshot()
        compatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        with mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=compatible), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_child_clear",
                    return_value=True), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=True), \
                mock.patch.object(ownerfile, "remove_exact") as remove:
            self.assertFalse(indexd_runtime.stop_indexd_owner())
        remove.assert_not_called()

    def test_stop_owner_reloads_after_a_final_heartbeat(self) -> None:
        raw = self._raw(pid=4242, process_start="birth")
        self.path.write_bytes(raw)
        initial = ownerfile.snapshot(self.path)
        first = True

        def inspect() -> indexd_runtime._IndexdOwnerInspection:
            nonlocal first
            if first:
                first = False
                return indexd_runtime._IndexdOwnerInspection(
                    indexd_runtime._IndexdOwnerState.COMPATIBLE,
                    initial, 4242, "birth")
            try:
                current = ownerfile.snapshot(self.path)
            except FileNotFoundError:
                return indexd_runtime._IndexdOwnerInspection(
                    indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            return indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.DEAD,
                current, 4242, "birth")

        def terminate(
                _pid: int, _start: str, *, wait_s: float,
                require_bound_tree: bool = False,
                term_grace_s: float | None = None) -> bool:
            self.assertGreaterEqual(wait_s, 0.0)
            self.assertTrue(require_bound_tree)
            self.assertEqual(term_grace_s, 4.0)
            changed = self.path.stat().st_mtime + 1.0
            os.utime(self.path, (changed, changed))
            return True

        with mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner", side_effect=inspect), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=terminate), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_child_clear",
                    return_value=True), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=False):
            self.assertTrue(indexd_runtime.stop_indexd_owner())
        self.assertFalse(self.path.exists())

    def test_stop_owner_preserves_a_replacement_after_tree_drain(self) -> None:
        old = self._snapshot()
        old_owner = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            old, 4242, "birth")
        replacement_raw = self._raw(
            pid=5252, process_start="replacement", token="b" * 32)
        replacement = ownerfile.Snapshot(
            (9, 9, len(replacement_raw), 200_000_000_000),
            200.0, replacement_raw)
        replacement_owner = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            replacement, 5252, "replacement")
        with mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                side_effect=(old_owner, replacement_owner)), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_child_clear",
                    return_value=True), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=False):
            self.assertFalse(indexd_runtime.stop_indexd_owner())

    @unittest.skipIf(common.WIN, "POSIX private-session compatibility")
    def test_legacy_incompatible_owner_without_group_is_retireable(
            self) -> None:
        original = self._raw(build="old-build").replace(
            b"group=4242 ", b"")
        self._write(original)

        def retire(
                _pid: int, _start: str, *, wait_s: float,
                require_bound_tree: bool = False) -> bool:
            self.assertGreater(wait_s, 0.0)
            self.assertTrue(require_bound_tree)
            self.path.unlink()
            return True

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242), \
                mock.patch.object(
                    common, "terminate_exact_process_tree",
                    side_effect=retire):
            inspected = indexd_runtime._inspect_indexd_owner()
            retired = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(
            inspected.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        self.assertIs(retired.state, indexd_runtime._IndexdOwnerState.ABSENT)

    @unittest.skipIf(common.WIN, "POSIX private-session compatibility")
    def test_incompatible_shared_process_group_is_never_retired(self) -> None:
        claimed = self._raw(build="old-build")
        missing = claimed.replace(b"group=4242 ", b"")
        for label, original in (("claimed", claimed), ("missing", missing)):
            with self.subTest(label=label):
                self._write(original)
                with mock.patch.object(
                        common, "pid_alive", return_value=True), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value="birth"), \
                        mock.patch.object(
                            common.os, "getpgid", return_value=7777), \
                        mock.patch.object(
                            common.os, "getsid", return_value=7777), \
                        mock.patch.object(
                            common, "terminate_exact_process_tree") as terminate:
                    inspected = indexd_runtime._inspect_indexd_owner()
                    settled = indexd_runtime._settle_indexd_owner(allow_retire=True)
                self.assertIs(
                    inspected.state, indexd_runtime._IndexdOwnerState.HOSTILE)
                self.assertIs(
                    settled.state, indexd_runtime._IndexdOwnerState.HOSTILE)
                terminate.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()

    @unittest.skipUnless(common.WIN, "Windows Job proof")
    def test_windows_incompatible_owner_without_job_is_never_retired(
            self) -> None:
        original = self._raw(build="old-build").replace(b"group=job ", b"")
        self._write(original)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            inspected = indexd_runtime._inspect_indexd_owner()
            settled = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(inspected.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIs(settled.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        terminate.assert_not_called()

    @unittest.skipUnless(common.WIN, "Windows Job proof")
    def test_windows_current_owner_without_exact_tree_is_hostile(self) -> None:
        original = self._raw(tree="")
        self._write(original)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            inspected = indexd_runtime._inspect_indexd_owner()
            settled = indexd_runtime._settle_indexd_owner(allow_retire=True)
        self.assertIs(inspected.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIs(settled.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        terminate.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(common.WIN, "Windows Job proof")
    def test_windows_legacy_tree_is_protected_during_upgrade(self) -> None:
        original = self._raw(build="old-build", tree="")
        self._write(original)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
            inspected = indexd_runtime._inspect_indexd_owner()
            settled = indexd_runtime._settle_indexd_owner(allow_retire=True)
            stopped = indexd_runtime.stop_indexd_owner()
        self.assertIs(inspected.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        self.assertIs(settled.state, indexd_runtime._IndexdOwnerState.INCOMPATIBLE)
        self.assertFalse(stopped)
        terminate.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_reclaim_and_release_preserve_aba_replacements(self) -> None:
        stale = self._raw(pid=999999)
        replacement = self._raw(
            pid=os.getpid(), process_start="replacement-birth",
            token="b" * 32)
        self._write(stale)
        remove = ownerfile.remove_exact

        def replace_before_remove(path, expected, **kwargs):
            path.unlink()
            path.write_bytes(replacement)
            return remove(path, expected, **kwargs)

        with mock.patch.object(
                common, "pid_alive",
                side_effect=lambda pid: pid == os.getpid()), \
                mock.patch.object(
                    common, "process_start_identity",
                    return_value="replacement-birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=os.getpid(),
                    create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=os.getpid(),
                    create=True), \
                mock.patch.object(
                    proc, "_process_group_present", return_value=False), \
                mock.patch.object(
                    ownerfile, "remove_exact",
                    side_effect=replace_before_remove):
            inspection = indexd_runtime._settle_indexd_owner()
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.COMPATIBLE)
        self.assertEqual(self.path.read_bytes(), replacement)
        self.path.unlink()

        handle = self._acquire()
        copied = handle.snapshot.raw
        try:
            self.path.unlink()
            self.path.write_bytes(copied)
        except OSError as exc:
            sharing = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in (5, 32, 33))
            if os.name != "nt" or not sharing:
                raise
            handle.close()
            self.path.unlink()
            self.path.write_bytes(copied)
        self.assertFalse(self._release(handle))
        self.assertEqual(self.path.read_bytes(), copied)

    def test_hostile_directory_is_protected_and_acquire_is_bounded(self) -> None:
        self.path.mkdir()
        started = time.monotonic()
        inspection = indexd_runtime._inspect_indexd_owner()
        handle = self._acquire()
        elapsed = time.monotonic() - started
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIsNone(handle)
        self.assertTrue(self.path.is_dir())
        self.assertLess(elapsed, 0.5)

    def test_hostile_symlink_never_touches_its_target(self) -> None:
        target = self.root / "target"
        target.write_bytes(self._raw())
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        before = target.read_bytes()
        inspection = indexd_runtime._inspect_indexd_owner()
        self.assertIsNone(self._acquire())
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are not portable")
    def test_hostile_fifo_never_blocks_acquisition(self) -> None:
        os.mkfifo(self.path)
        started = time.monotonic()
        inspection = indexd_runtime._inspect_indexd_owner()
        handle = self._acquire()
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIsNone(handle)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_oversize_owner_is_hostile_and_does_not_block(self) -> None:
        body = b"x" * (indexd_runtime._INDEXD_OWNER_MAX_BYTES + 1)
        self.path.write_bytes(body)
        started = time.monotonic()
        inspection = indexd_runtime._inspect_indexd_owner()
        handle = self._acquire()
        elapsed = time.monotonic() - started
        self.assertIs(inspection.state, indexd_runtime._IndexdOwnerState.HOSTILE)
        self.assertIsNone(handle)
        self.assertEqual(self.path.stat().st_size, len(body))
        self.assertLess(elapsed, 0.5)

    def test_heartbeat_refreshes_only_the_owned_entry(self) -> None:
        handle = self._acquire()
        raw = handle.snapshot.raw
        os.utime(self.path, (1.0, 1.0))
        indexd_runtime.heartbeat_indexd_owner(handle)
        handle.verify()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertGreater(self.path.stat().st_mtime, 1.0)
        self.assertTrue(self._release(handle))

        handle = self._acquire()
        replacement = b"replacement-owner\n"
        try:
            self.path.unlink()
            self.path.write_bytes(replacement)
        except OSError as exc:
            sharing = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in (5, 32, 33))
            if os.name != "nt" or not sharing:
                raise
            handle.close()
            self.path.unlink()
            self.path.write_bytes(replacement)
        with self.assertRaises(ownerfile.OwnershipLost):
            indexd_runtime.heartbeat_indexd_owner(handle)
        self.assertFalse(self._release(handle))
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_heartbeat_keeps_exact_successor_alive_during_takeover(self) -> None:
        handle = self._acquire()
        inspected = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            handle.snapshot, os.getpid(),
            common.process_start_identity(os.getpid()))
        cases = (
            indexd_runtime.DerivedMutationInfo(
                "foreign", "a" * 20, "durable owner is still the predecessor"),
            indexd_runtime.DerivedMutationInfo(
                "unavailable", None, "rollback journal needs recovery",
                journal_blocked=True),
        )
        os.utime(self.path, (1.0, 1.0))
        for ownership in cases:
            with self.subTest(state=ownership.state,
                              journal=ownership.journal_blocked), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_mutation_info",
                        return_value=ownership), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=inspected):
                indexd_runtime.heartbeat_indexd_owner(handle)
                handle.verify()
        self.assertGreater(self.path.stat().st_mtime, 1.0)

    def test_heartbeat_uses_one_takeover_verdict_across_commit_transition(
            self) -> None:
        handle = self._acquire()
        inspected = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            handle.snapshot, os.getpid(),
            common.process_start_identity(os.getpid()))
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "a" * 20, "durable owner is still the predecessor")
        transitional = indexd_runtime.DerivedMutationInfo(
            "unavailable", None,
            "anchor and cache moved before the database commit")
        with mock.patch.object(
                indexd_runtime, "derived_writer_mutation_info",
                side_effect=(foreign, transitional)) as mutation, \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=inspected):
            indexd_runtime.heartbeat_indexd_owner(handle)
        mutation.assert_called_once_with()
        handle.verify()

    def test_heartbeat_rejects_takeover_behind_a_live_foreign_claim(self) -> None:
        owner = mock.Mock(spec=ownerfile.Handle)
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "a" * 20, "durable owner is still the predecessor")
        for state in (
                indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
                indexd_runtime._IndexdOwnerState.HOSTILE):
            inspected = indexd_runtime._IndexdOwnerInspection(
                state, self._snapshot(), 4242, "birth")
            with self.subTest(state=state.value), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_mutation_info",
                        return_value=foreign), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=inspected):
                with self.assertRaisesRegex(
                        ownerfile.OwnershipLost, "predecessor"):
                    indexd_runtime.heartbeat_indexd_owner(owner)
        owner.touch.assert_not_called()

    def test_slow_startup_heartbeats_keep_the_owner_fresh_for_contenders(
            self) -> None:
        raw = self._raw()
        handle = ownerfile.create_exclusive(
            self.path, raw, retain_fd=True)
        self.handles.append(handle)
        stale = time.time() - indexd_runtime._INDEXD_STARTUP_GRACE_S - 1.0
        os.utime(self.path, (stale, stale))
        watcher = mock.Mock()
        watcher.is_alive.return_value = True
        auto_indexer = mock.Mock()
        auto_indexer.wait_operational.side_effect = (False, True)
        auto_indexer.is_alive.return_value = True
        self.assertTrue(indexd._await_operational(
            watcher, auto_indexer, handle, timeout=1.0))
        self.assertGreater(self.path.stat().st_mtime, stale)

        (self.root / "corpus.db").write_bytes(b"snapshot")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    common.os, "getpgid", return_value=4242, create=True), \
                mock.patch.object(
                    common.os, "getsid", return_value=4242, create=True), \
                mock.patch.object(
                    indexd_runtime, "stop_indexd_owner") as stop, \
                mock.patch.object(common.subprocess, "Popen") as popen:
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        stop.assert_not_called()
        popen.assert_not_called()

    def test_operational_wait_heartbeats_and_checks_both_workers(self) -> None:
        owner = mock.Mock(spec=ownerfile.Handle)
        watcher = mock.Mock()
        watcher.is_alive.return_value = True
        auto_indexer = mock.Mock()
        auto_indexer.wait_operational.side_effect = (False, False, True)
        auto_indexer.is_alive.return_value = True
        with mock.patch.object(
                indexd.indexd_runtime, "heartbeat_indexd_owner") as heartbeat:
            self.assertTrue(indexd._await_operational(
                watcher, auto_indexer, owner, timeout=5.0))
        self.assertEqual(auto_indexer.wait_operational.call_count, 3)
        self.assertEqual(heartbeat.call_args_list, [
            mock.call(owner),
            mock.call(owner),
        ])
        self.assertEqual(watcher.is_alive.call_count, 3)
        self.assertEqual(auto_indexer.is_alive.call_count, 3)

    def test_main_stamps_daemon_log_lines_with_wall_clock_time(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(_log, "_TIMESTAMPS", False), \
                mock.patch.object(indexd.common, "ingest_bin",
                                  return_value=self.root / "missing"), \
                contextlib.redirect_stderr(stderr):
            common.log("cli: stays bare")
            self.assertEqual(indexd.main(), 0)
        lines = stderr.getvalue().splitlines()
        bare, stamped = lines[0], lines[-1]
        self.assertEqual(bare, "cli: stays bare")
        prefix, _, rest = stamped.partition(" ")
        self.assertEqual(
            rest, "indexd: no ingest binary; nothing to keep fresh. exiting.")
        datetime.strptime(prefix, "%Y-%m-%dT%H:%M:%S%z")

    def test_main_verifies_owner_before_starting_watcher_or_indexer(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        handle = mock.Mock(spec=ownerfile.Handle)
        handle.snapshot = self._snapshot()
        handle.verify.side_effect = ownerfile.OwnershipLost("replaced")
        with mock.patch.object(indexd.common, "ingest_bin",
                               return_value=binary), \
                mock.patch.object(
                    indexd.indexer, "configure_indexd_mode") as configure, \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(indexd, "_acquire", return_value=handle), \
                mock.patch.object(indexd.live, "watcher") as watcher, \
                mock.patch.object(indexd.indexer, "start") as start:
            result = indexd.main()
        self.assertEqual(result, 0)
        configure.assert_called_once_with()
        watcher.assert_not_called()
        start.assert_not_called()
        handle.verify.assert_called_once_with()
        handle.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)

    def test_main_ownership_loss_during_boot_never_publishes_ready(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        handle = mock.Mock(spec=ownerfile.Handle)
        handle.snapshot = self._snapshot()
        watcher = mock.Mock()
        auto_indexer = mock.Mock()
        auto_indexer.is_alive.return_value = False
        with mock.patch.object(
                indexd.common, "ingest_bin", return_value=binary), \
                mock.patch.object(indexd.indexer, "configure_indexd_mode"), \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(indexd, "_acquire", return_value=handle), \
                mock.patch.object(
                    indexd.live, "watcher", return_value=watcher), \
                mock.patch.object(
                    indexd.indexer, "start", return_value=auto_indexer), \
                mock.patch.object(
                    indexd, "_await_operational",
                    side_effect=ownerfile.OwnershipLost("replaced")), \
                mock.patch.object(
                    indexd.indexd_runtime, "publish_indexd_ready") as publish:
            self.assertEqual(indexd.main(), 0)
        publish.assert_not_called()
        auto_indexer.stop.assert_called_once_with()
        auto_indexer.join.assert_called_once_with(timeout=3.0)

    def test_main_refuses_an_unbound_child_tree(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        with mock.patch.object(indexd.common, "ingest_bin",
                               return_value=binary), \
                mock.patch.object(
                    indexd.indexer, "configure_indexd_mode") as configure, \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=False), \
                mock.patch.object(indexd, "_acquire") as acquire, \
                mock.patch.object(indexd.live, "watcher") as watcher, \
                mock.patch.object(indexd.common, "log"):
            result = indexd.main()
        self.assertEqual(result, 0)
        configure.assert_called_once_with()
        acquire.assert_not_called()
        watcher.assert_not_called()

    def test_main_startup_failure_never_publishes_readiness(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        handle = mock.Mock(spec=ownerfile.Handle)
        handle.snapshot = self._snapshot()
        with mock.patch.object(
                indexd.common, "ingest_bin", return_value=binary), \
                mock.patch.object(indexd.indexer, "configure_indexd_mode"), \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(indexd, "_acquire", return_value=handle), \
                mock.patch.object(
                    indexd.live, "watcher",
                    side_effect=RuntimeError("startup failed")), \
                mock.patch.object(
                    indexd.indexd_runtime, "publish_indexd_ready") as publish_ready:
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                indexd.main()
        publish_ready.assert_not_called()
        handle.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)

    def test_main_starts_the_explicit_headless_watcher(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        handle = mock.Mock(spec=ownerfile.Handle)
        handle.snapshot = self._snapshot()
        ready_handle = mock.Mock(spec=ownerfile.Handle)
        watcher = mock.Mock()
        watcher.is_alive.return_value = True
        auto_indexer = mock.Mock()
        auto_indexer.is_alive.side_effect = (True, False)
        calls = []
        handle.verify.side_effect = lambda: calls.append("verify") or True
        with mock.patch.object(indexd.common, "ingest_bin",
                               return_value=binary), \
                mock.patch.object(
                    indexd.indexer, "configure_indexd_mode") as configure, \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(indexd, "_acquire", return_value=handle), \
                mock.patch.object(
                    indexd.live, "watcher",
                    side_effect=lambda **_kw: calls.append("watcher") or watcher
                    ) as start_watcher, \
                mock.patch.object(
                    indexd.indexer, "start",
                    side_effect=lambda *_args, **_kw:
                    calls.append("start") or auto_indexer) as start_indexer, \
                mock.patch.object(
                    indexd, "_await_operational", return_value=True), \
                mock.patch.object(
                    indexd.indexd_runtime, "publish_indexd_ready",
                    side_effect=lambda _owner:
                    calls.append("ready") or ready_handle) as publish_ready, \
                mock.patch.object(indexd.time, "sleep"), \
                mock.patch.object(
                    indexd.indexd_runtime, "heartbeat_indexd_owner",
                    side_effect=ownerfile.OwnershipLost("replaced")), \
                mock.patch.object(
                    indexd.indexd_runtime, "_indexd_child_active",
                    return_value=False), \
                mock.patch.object(indexd.common, "log"):
            result = indexd.main()
        self.assertEqual(result, 0)
        configure.assert_called_once_with()
        start_watcher.assert_called_once_with(headless_indexd=True)
        start_indexer.assert_called_once()
        self.assertEqual(start_indexer.call_args.args, (watcher,))
        self.assertTrue(callable(
            start_indexer.call_args.kwargs["owns_lifetime"]))
        self.assertIs(
            start_indexer.call_args.kwargs["owner_snapshot"],
            handle.snapshot)
        self.assertEqual(handle.verify.call_count, 3)
        self.assertEqual(
            calls, ["verify", "watcher", "verify", "start", "verify", "ready"])
        publish_ready.assert_called_once_with(handle)
        ready_handle.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)
        auto_indexer.stop.assert_called_once_with()
        auto_indexer.join.assert_called_once_with(timeout=3.0)
        self.assertEqual(auto_indexer.is_alive.call_count, 2)
        handle.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)

    def test_main_never_publishes_ready_for_an_unhealthy_worker(self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        for label, watcher_alive, indexer_alive in (
                ("watcher", False, True),
                ("indexer", True, False)):
            with self.subTest(label=label):
                handle = mock.Mock(spec=ownerfile.Handle)
                handle.snapshot = self._snapshot()
                handle.verify.return_value = handle.snapshot
                watcher = mock.Mock()
                watcher.is_alive.return_value = watcher_alive
                auto_indexer = mock.Mock()
                auto_indexer.is_alive.return_value = indexer_alive
                with mock.patch.object(
                        indexd.common, "ingest_bin", return_value=binary), \
                        mock.patch.object(
                            indexd.indexer, "configure_indexd_mode"), \
                        mock.patch.object(
                            indexd.common,
                            "bind_descendants_to_process_lifetime",
                            return_value=True), \
                        mock.patch.object(
                            indexd, "_acquire", return_value=handle), \
                        mock.patch.object(
                            indexd.live, "watcher",
                            return_value=watcher), \
                        mock.patch.object(
                            indexd.indexer, "start",
                            return_value=auto_indexer), \
                        mock.patch.object(
                            indexd.indexd_runtime, "publish_indexd_ready") as publish, \
                        mock.patch.object(
                            indexd.indexd_runtime, "_indexd_child_active",
                            return_value=False), \
                        mock.patch.object(indexd.common, "log"):
                    self.assertEqual(indexd.main(), 0)
                publish.assert_not_called()
                auto_indexer.stop.assert_called_once_with()
                auto_indexer.join.assert_called_once_with(timeout=3.0)

    def test_main_preserves_owner_until_a_stuck_indexer_process_exits(
            self) -> None:
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"fixture")
        handle = mock.Mock(spec=ownerfile.Handle)
        handle.snapshot = self._snapshot()
        ready_handle = mock.Mock(spec=ownerfile.Handle)
        watcher = mock.Mock()
        watcher.is_alive.return_value = True
        auto_indexer = mock.Mock()
        auto_indexer.is_alive.return_value = True
        with mock.patch.object(
                indexd.common, "ingest_bin", return_value=binary), \
                mock.patch.object(indexd.indexer, "configure_indexd_mode"), \
                mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(indexd, "_acquire", return_value=handle), \
                mock.patch.object(
                    indexd.live, "watcher", return_value=watcher), \
                mock.patch.object(
                    indexd.indexer, "start", return_value=auto_indexer), \
                mock.patch.object(
                    indexd.indexd_runtime, "publish_indexd_ready",
                    return_value=ready_handle), \
                mock.patch.object(indexd.time, "sleep"), \
                mock.patch.object(
                    indexd.indexd_runtime, "heartbeat_indexd_owner",
                    side_effect=ownerfile.OwnershipLost("replaced")), \
                mock.patch.object(indexd.common, "log"):
            self.assertEqual(indexd.main(), 0)
        ready_handle.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)
        auto_indexer.stop.assert_called_once_with()
        auto_indexer.join.assert_called_once_with(timeout=3.0)
        handle.release.assert_not_called()

    def test_failed_child_retains_launcher_fence_for_inline_cleanup(self) -> None:
        process = mock.Mock(pid=999999)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=False):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.FAILED)
        guard = ownerfile.snapshot(indexd_runtime._SPAWN_GUARD_PATH)
        self.assertIn(b"state=launching", guard.raw)
        self.assertEqual(indexd_runtime._OWN_SPAWN_GUARD.snapshot, guard)
        indexd_runtime._clear_own_spawn_guard(force=True)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
        log.close.assert_called_once_with()

    def test_failed_child_yields_to_incompatible_owner(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        incompatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
            self._snapshot(), 4242, "birth")
        process = mock.Mock(pid=999999)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    side_effect=(absent, absent, incompatible)), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=False):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.BLOCKED)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
        log.close.assert_called_once_with()

    def test_retained_spawn_guard_is_immutable_and_cleanup_retriable(
            self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        body = (
            f"state=launching pid={os.getpid()} start={start} "
            f"token={'a' * 32}\n"
        ).encode("ascii")
        handle = ownerfile.create_exclusive(
            indexd_runtime._SPAWN_GUARD_PATH, body, retain_fd=True)
        original = handle.snapshot
        indexd_runtime._retain_spawn_guard(handle)
        current = ownerfile.snapshot(indexd_runtime._SPAWN_GUARD_PATH)
        self.assertEqual(current, original)
        self.assertEqual(indexd_runtime._OWN_SPAWN_GUARD.snapshot, current)
        indexd_runtime._clear_own_spawn_guard(force=True)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())

    def test_future_mtime_does_not_override_exact_live_spawn_owner(self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid={os.getpid()} start={start} "
            f"token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() + 3600.0
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        indexd_runtime._OWN_SPAWN_GUARD.snapshot = ownerfile.snapshot(
            indexd_runtime._SPAWN_GUARD_PATH)
        indexd_runtime._clear_own_spawn_guard()
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        indexd_runtime._clear_own_spawn_guard(force=True)

    def test_ready_release_failure_remains_cleanup_retriable(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        ready = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            self._snapshot(), 4242, "birth")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    side_effect=(absent, ready)), \
                mock.patch.object(
                    indexd_runtime, "_indexd_ready",
                    side_effect=(False, True)), \
                mock.patch.object(
                    ownerfile, "remove_exact", return_value=False):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        snapshot = ownerfile.snapshot(indexd_runtime._SPAWN_GUARD_PATH)
        self.assertEqual(indexd_runtime._OWN_SPAWN_GUARD.snapshot, snapshot)
        indexd_runtime._clear_own_spawn_guard(force=True)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())

    def test_true_cold_spawn_keeps_the_launcher_fence(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        process = mock.Mock(pid=999999)
        process.poll.return_value = None
        log = mock.Mock()
        launcher_start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(launcher_start)

        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=absent), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=lambda pid:
                    "child-birth" if pid == process.pid else launcher_start), \
                mock.patch.object(
                    common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        guard = ownerfile.snapshot(indexd_runtime._SPAWN_GUARD_PATH)
        self.assertIn(f"pid={os.getpid()}".encode("ascii"), guard.raw)
        token = common._owner_field(guard.raw.decode("ascii"), "token")
        child = ownerfile.snapshot(indexd_runtime._spawn_child_path(token))
        self.assertIn(b"pid=999999 start=child-birth", child.raw)
        self.assertEqual(indexd_runtime._OWN_SPAWN_GUARD.snapshot, guard)
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=absent), \
                mock.patch.object(
                    common, "pid_alive",
                    side_effect=lambda pid: pid == process.pid), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=lambda pid:
                    "child-birth" if pid == process.pid
                    else "contender-birth"), \
                mock.patch.object(common.subprocess, "Popen") as contender:
            second = indexd_runtime._spawn_indexd()
        self.assertIs(second, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        contender.assert_not_called()
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    return_value="child-birth"):
            indexd_runtime._clear_own_spawn_guard(force=True)
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        indexd_runtime._clear_own_spawn_guard()
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
        self.assertFalse(indexd_runtime._spawn_child_path(token).exists())
        log.close.assert_called_once_with()

    def test_incomplete_live_spawn_holder_only_gets_publication_grace(self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        fresh_age = indexd_runtime._INDEXD_PUBLICATION_GRACE_S - 0.1
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=unknown pid={os.getpid()} start={start} "
            f"token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() - fresh_age
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        popen.assert_not_called()
        indexd_runtime._SPAWN_GUARD_PATH.unlink()

        for age in (
                indexd_runtime._INDEXD_PUBLICATION_GRACE_S + 0.1,
                indexd_runtime._SPAWN_GUARD_S + 3600.0,
                -3600.0):
            with self.subTest(age=age):
                indexd_runtime._SPAWN_GUARD_PATH.write_text(
                    f"state=unknown pid={os.getpid()} start={start} "
                    f"token={'a' * 32}\n",
                    encoding="ascii")
                changed = time.time() - age
                os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
                process = mock.Mock(pid=999_999)
                log = mock.Mock()
                with mock.patch.object(
                        indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                        mock.patch.object(
                            common, "open_bounded_log", return_value=log), \
                        mock.patch.object(
                            common.subprocess, "Popen", return_value=process) as popen, \
                        mock.patch.object(
                            indexd_runtime, "_await_indexd_ready", return_value=True):
                    result = indexd_runtime._spawn_indexd()
                self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
                self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
                popen.assert_called_once()
                log.close.assert_called_once_with()

    def test_fresh_partial_spawn_guard_is_not_reclaimed(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_bytes(b"")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        popen.assert_not_called()

    def test_unreadable_existing_spawn_guard_blocks_inline_fallback(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_bytes(b"hostile")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    ownerfile, "snapshot", side_effect=OSError("unreadable")), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.BLOCKED)
        popen.assert_not_called()

    def test_dead_launch_guard_is_reclaimed_without_a_timer(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=birth token={'a' * 32}\n",
            encoding="ascii")
        process = mock.Mock(pid=999_998)
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(common, "open_bounded_log", return_value=log), \
                mock.patch.object(common.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
        popen.assert_called_once()
        log.close.assert_called_once_with()

    def test_spawn_backoff_never_runs_beside_incompatible_owner(self) -> None:
        incompatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
            self._snapshot(), 4242, "birth")
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=birth "
            f"token={'a' * 32}\n",
            encoding="ascii")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd",
                return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=incompatible), \
                mock.patch.object(
                    common, "pid_alive", return_value=False), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.BLOCKED)
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        popen.assert_not_called()

    def test_successor_retires_exact_incompatible_owner_then_spawns(self) -> None:
        incompatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
            self._snapshot(), 4242, "birth")
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "old-build", "foreign owner")
        process = mock.Mock(pid=999_998)
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "derived_writes_permitted",
                side_effect=(False, True)), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=foreign), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    side_effect=(incompatible, absent, absent)), \
                mock.patch.object(
                    indexd_runtime, "_settle_indexd_owner",
                    return_value=absent) as settle, \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd",
                    return_value=True), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(
                    indexd_runtime, "_publish_spawn_child",
                    return_value=ownerfile.ProcessOwner.EXACT_LIVE), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        settle.assert_called_once_with(
            allow_retire=True,
            retire_budget_s=indexd_runtime._INDEXD_ACQUIRE_WAIT_S)
        popen.assert_called_once()
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        log.close.assert_called_once_with()

    def test_future_launch_guard_is_reclaimed(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=birth token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() + 3600.0
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        process = mock.Mock(pid=999_998)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())
        log.close.assert_called_once_with()

    def test_complete_live_launch_guard_does_not_expire(self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid={os.getpid()} start={start} "
            f"token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() - indexd_runtime._SPAWN_GUARD_S - 3600.0
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        process = mock.Mock(pid=999_999)
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        self.assertTrue(indexd_runtime._SPAWN_GUARD_PATH.exists())
        popen.assert_not_called()

    def test_old_dead_launch_guard_does_not_pin_unready_owner(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=birth token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() - indexd_runtime._SPAWN_GUARD_S - 1.0
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        compatible = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            self._snapshot(), 4242, "birth")
        process = mock.Mock(pid=999_998)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=compatible), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "stop_indexd_owner", return_value=True) as stop, \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        stop.assert_called_once_with(wait_s=indexd_runtime._INDEXD_ACQUIRE_WAIT_S)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())

    def test_old_dead_launch_guard_reaches_orphan_retirement(self) -> None:
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=birth token={'a' * 32}\n",
            encoding="ascii")
        changed = time.time() - indexd_runtime._SPAWN_GUARD_S - 1.0
        os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
        orphan = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ORPHANED_GROUP,
            self._snapshot(), 4242, "birth")
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        process = mock.Mock(pid=999_998)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=orphan), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "_retire_indexd_child",
                    return_value=True) as retire, \
                mock.patch.object(
                    indexd_runtime, "_settle_indexd_owner",
                    return_value=absent), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        retire.assert_called_once_with(
            orphan.snapshot, wait_s=indexd_runtime._INDEXD_ACQUIRE_WAIT_S)
        self.assertFalse(indexd_runtime._SPAWN_GUARD_PATH.exists())

    def test_future_unready_owner_is_recovered(self) -> None:
        raw = self._raw()
        self.path.write_bytes(raw)
        changed = time.time() + 3600.0
        os.utime(self.path, (changed, changed))
        process = mock.Mock(pid=999_998)
        process.poll.return_value = 0
        log = mock.Mock()
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True), \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "stop_indexd_owner", return_value=True) as stop, \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(
                    common.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    indexd_runtime, "_await_indexd_ready", return_value=True):
            result = indexd_runtime._spawn_indexd()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.READY)
        stop.assert_called_once_with(wait_s=indexd_runtime._INDEXD_ACQUIRE_WAIT_S)
        log.close.assert_called_once_with()

    def test_orphaned_group_blocks_spawn_and_is_visible_in_status(self) -> None:
        orphan = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ORPHANED_GROUP,
            self._snapshot(), 4242, "birth")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=orphan), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            self.assertIs(
                indexd_runtime._spawn_indexd(), indexd_runtime._IndexdSpawnResult.BLOCKED)
        popen.assert_not_called()

        (self.root / "corpus.db").write_bytes(b"snapshot")
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=orphan), \
                mock.patch.object(common.time, "time", return_value=100.0), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            self.assertIs(
                indexd_runtime._spawn_indexd(), indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        popen.assert_not_called()
        with mock.patch.object(
                indexd_runtime, "_settle_indexd_owner", return_value=orphan):
            self.assertEqual(indexd_runtime.indexd_resource_status(), {
                "running": False,
                "blocked": True,
                "state": "orphaned-group",
            })

    def test_protected_owner_states_are_visible_in_status(self) -> None:
        for state in (
                indexd_runtime._IndexdOwnerState.HOSTILE,
                indexd_runtime._IndexdOwnerState.UNVERIFIABLE,
                indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
        ):
            with self.subTest(state=state.value):
                inspection = indexd_runtime._IndexdOwnerInspection(
                    state, self._snapshot(), 4242, "birth")
                with mock.patch.object(
                        indexd_runtime, "_settle_indexd_owner",
                        return_value=inspection), \
                        mock.patch.object(
                            indexd_runtime, "_retire_legacy_indexd") as retire_legacy:
                    status = indexd_runtime.indexd_resource_status()
                self.assertEqual(status, {
                    "running": False,
                    "blocked": True,
                    "state": state.value,
                })
                retire_legacy.assert_not_called()

    def test_spawn_guard_fences_are_visible_in_status(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        with mock.patch.object(
                indexd_runtime, "_settle_indexd_owner", return_value=absent), \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_spawn_guard",
                    side_effect=OSError("unreadable")):
            self.assertEqual(indexd_runtime.indexd_resource_status(), {
                "running": False,
                "blocked": True,
                "state": "spawn-guard",
            })

        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid={os.getpid()} start={start} "
            f"token={'a' * 32}\n",
            encoding="ascii")
        with mock.patch.object(
                indexd_runtime, "_settle_indexd_owner", return_value=absent), \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd", return_value=True):
            status = indexd_runtime.indexd_resource_status()
        self.assertFalse(status["running"])
        self.assertTrue(status["starting"])
        self.assertGreaterEqual(status["age_s"], 0.0)

    def test_protected_status_preserves_a_stale_malformed_owner_exactly(
            self) -> None:
        now = 1_000.0
        self._write(b"malformed owner\n", age=60.0, now=now)
        indexd_runtime._INDEXD_RESPONSE_PATH.write_bytes(b"probe sentinel")
        before_names = tuple(sorted(path.name for path in self.root.iterdir()))
        before = {
            path.name: (
                path.read_bytes(),
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
            )
            for path in self.root.iterdir()
        }
        root_before = (
            self.root.stat().st_mode,
            self.root.stat().st_mtime_ns,
            self.root.stat().st_ctime_ns,
        )
        with (
            mock.patch.dict(
                os.environ,
                {"AGREP_DATA_READONLY": os.fspath(self.root)},
                clear=False),
            mock.patch.object(common.time, "time", return_value=now),
        ):
            status = indexd_runtime.indexd_resource_status()
        self.assertEqual(status, {
            "running": False,
            "blocked": True,
            "state": "malformed-stale",
        })
        self.assertEqual(
            tuple(sorted(path.name for path in self.root.iterdir())),
            before_names)
        self.assertEqual(root_before, (
            self.root.stat().st_mode,
            self.root.stat().st_mtime_ns,
            self.root.stat().st_ctime_ns,
        ))
        self.assertEqual({
            path.name: (
                path.read_bytes(),
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
            )
            for path in self.root.iterdir()
        }, before)

    def test_protected_status_never_rebases_a_stale_heartbeat_probe(
            self) -> None:
        now = 1_000.0
        raw = self._raw()
        self._write(raw, age=60.0, now=now)
        owner = ownerfile.snapshot(
            self.path, max_bytes=indexd_runtime._INDEXD_OWNER_MAX_BYTES)
        ready = indexd_runtime._indexd_ready_path(owner)
        ready.write_bytes(raw)
        indexd_runtime._INDEXD_RESPONSE_PATH.write_bytes(b"probe sentinel")
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.iterdir()
        }
        root_before = (
            self.root.stat().st_mtime_ns,
            self.root.stat().st_ctime_ns,
        )
        with (
            mock.patch.dict(
                os.environ,
                {"AGREP_DATA_READONLY": os.fspath(self.root)},
                clear=False),
            mock.patch.object(common.time, "time", return_value=now),
            mock.patch.object(common, "pid_alive", return_value=True),
            mock.patch.object(
                common, "process_start_identity", return_value="birth"),
            mock.patch.object(
                common.os, "getpgid", return_value=4242, create=True),
            mock.patch.object(
                common.os, "getsid", return_value=4242, create=True),
            mock.patch.object(
                proc, "process_execution_state", return_value="active"),
            mock.patch.object(common, "process_rss_bytes", return_value=123),
        ):
            status = indexd_runtime.indexd_resource_status()
        self.assertTrue(status["running"])
        self.assertFalse(status["responsive"])
        self.assertEqual(status["state"], "heartbeat-unprobed")
        self.assertEqual(
            (self.root.stat().st_mtime_ns, self.root.stat().st_ctime_ns),
            root_before)
        self.assertEqual({
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.iterdir()
        }, before)

    def test_dead_spawn_guard_status_is_reclaimable_at_every_age(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        now = 100.0
        for age in (
                indexd_runtime._INDEXD_PUBLICATION_GRACE_S - 0.1,
                indexd_runtime._INDEXD_PUBLICATION_GRACE_S + 0.1):
            with self.subTest(age=age):
                indexd_runtime._SPAWN_GUARD_PATH.write_text(
                    f"state=launching pid=999999 start=birth "
                    f"token={'a' * 32}\n",
                    encoding="ascii")
                changed = now - age
                os.utime(indexd_runtime._SPAWN_GUARD_PATH, (changed, changed))
                with mock.patch.object(
                        indexd_runtime, "_settle_indexd_owner",
                        return_value=absent), \
                        mock.patch.object(
                            indexd_runtime, "_retire_legacy_indexd",
                            return_value=True), \
                        mock.patch.object(
                            common, "pid_alive", return_value=False), \
                        mock.patch.object(
                            common.time, "time", return_value=now):
                    self.assertEqual(
                        indexd_runtime.indexd_resource_status(),
                        {"running": False})

    def test_spawn_child_handoff_uses_identity_not_mtime(self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        token = "a" * 32
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid={os.getpid()} start={start} "
            f"token={token}\n",
            encoding="ascii")
        guard = indexd_runtime._inspect_spawn_guard()
        child_path = indexd_runtime._spawn_child_path(token)

        def write_child(raw: bytes, age: float) -> None:
            child_path.unlink(missing_ok=True)
            child_path.write_bytes(raw)
            changed = time.time() - age
            os.utime(child_path, (changed, changed))

        valid = (
            f"state=spawned owner={token} pid=4242 start=child-birth\n"
        ).encode("ascii")
        write_child(valid, -3600.0)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    return_value="child-birth"):
            self.assertIs(
                indexd_runtime._settle_spawn_child(guard),
                indexd_runtime._SpawnChildState.ACTIVE)
        self.assertTrue(child_path.exists())

        controls = (
            ("dead", False, None),
            ("reused", True, "other-birth"),
        )
        for label, alive, actual in controls:
            with self.subTest(label=label):
                write_child(valid, -3600.0)
                with mock.patch.object(
                        common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value=actual):
                    self.assertIs(
                        indexd_runtime._settle_spawn_child(guard),
                        indexd_runtime._SpawnChildState.RECLAIMED)
                self.assertFalse(child_path.exists())

        write_child(
            f"state=spawned owner={token} pid=4242 start=unknown\n".encode(
                "ascii"),
            indexd_runtime._SPAWN_GUARD_S + 3600.0)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value=None):
            self.assertIs(
                indexd_runtime._settle_spawn_child(guard),
                indexd_runtime._SpawnChildState.BLOCKED)
        self.assertTrue(child_path.exists())
        child_path.unlink()

        write_child(b"partial", 0.0)
        self.assertIs(
            indexd_runtime._settle_spawn_child(guard),
            indexd_runtime._SpawnChildState.ACTIVE)
        self.assertTrue(child_path.exists())
        write_child(b"partial", -3600.0)
        self.assertIs(
            indexd_runtime._settle_spawn_child(guard),
            indexd_runtime._SpawnChildState.RECLAIMED)
        self.assertFalse(child_path.exists())

    def test_unreadable_spawn_child_handoff_blocks_relaunch(self) -> None:
        start = common.process_start_identity(os.getpid())
        self.assertIsNotNone(start)
        token = "a" * 32
        indexd_runtime._SPAWN_GUARD_PATH.write_text(
            f"state=launching pid=999999 start=dead-birth token={token}\n",
            encoding="ascii")
        indexd_runtime._spawn_child_path(token).mkdir()
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            self.assertIs(
                indexd_runtime._spawn_indexd(),
                indexd_runtime._IndexdSpawnResult.BLOCKED)
        popen.assert_not_called()

    def test_unretirable_legacy_owner_is_visible_in_status(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        with mock.patch.object(
                indexd_runtime, "_settle_indexd_owner", return_value=absent), \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd",
                    return_value=False):
            self.assertEqual(indexd_runtime.indexd_resource_status(), {
                "running": False,
                "blocked": True,
                "state": "legacy-owner",
            })

    def test_readiness_paths_are_generation_scoped_and_aba_safe(self) -> None:
        with mock.patch.object(
                common.secrets, "token_hex",
                side_effect=("a" * 32, "d" * 32, "b" * 32)):
            owner_a = self._acquire()
            ready_a = indexd_runtime.publish_indexd_ready(owner_a)
            self.handles.append(ready_a)
            snapshot_a = owner_a.snapshot
            path_a = indexd_runtime._indexd_ready_path(snapshot_a)
            self.assertTrue(self._release(owner_a))

            owner_b = self._acquire()
            ready_b = indexd_runtime.publish_indexd_ready(owner_b)
            self.handles.append(ready_b)
            snapshot_b = owner_b.snapshot
            path_b = indexd_runtime._indexd_ready_path(snapshot_b)
        self.assertNotEqual(path_a, path_b)
        self.assertTrue(path_a.exists())
        self.assertTrue(path_b.exists())

        inspection_b = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot_b, os.getpid(),
            common.process_start_identity(os.getpid()))
        self.assertTrue(indexd_runtime._indexd_ready(inspection_b))
        self.assertTrue(indexd_runtime._remove_indexd_ready_for(snapshot_a))
        self.assertFalse(path_a.exists())
        self.assertTrue(path_b.exists())
        self.assertTrue(indexd_runtime._indexd_ready(inspection_b))
        with mock.patch.object(
                indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=inspection_b), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            self.assertIs(
                indexd_runtime._spawn_indexd(), indexd_runtime._IndexdSpawnResult.READY)
        popen.assert_not_called()
        self.assertTrue(self._release(owner_b))

    def test_await_readiness_observes_publication_and_child_exit(self) -> None:
        process = mock.Mock()
        process.poll.side_effect = (None, 7)
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        with mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=absent), \
                mock.patch.object(common.time, "sleep"):
            self.assertFalse(indexd_runtime._await_indexd_ready(process))

        compatible = mock.Mock()
        with mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=compatible), \
                mock.patch.object(indexd_runtime, "_indexd_ready", return_value=True):
            self.assertTrue(indexd_runtime._await_indexd_ready(process))

    def test_status_uses_the_pid_from_the_validated_snapshot(self) -> None:
        raw = self._raw(pid=4242)
        self._write(raw)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(common.os, "getpgid",
                                  return_value=4242, create=True), \
                mock.patch.object(common.os, "getsid",
                                  return_value=4242, create=True):
            inspection = indexd_runtime._inspect_indexd_owner()
        ready_path = indexd_runtime._indexd_ready_path(inspection.snapshot)
        ready_path.write_bytes(raw)
        with mock.patch.object(
                indexd_runtime, "_settle_indexd_owner",
                return_value=inspection) as settle, \
                mock.patch.object(
                    common, "_lock_holder_pid",
                    side_effect=AssertionError("status reread the owner path")), \
                mock.patch.object(common.time, "time", return_value=100.0), \
                mock.patch.object(
                    common, "process_rss_bytes",
                    return_value=123456) as rss:
            status = indexd_runtime.indexd_resource_status()
        settle.assert_called_once_with(
            retire_budget_s=indexd_runtime._INDEXD_FOREGROUND_RETIRE_S)
        rss.assert_called_once_with(4242)
        self.assertTrue(status["running"])
        self.assertTrue(status["responsive"])
        self.assertEqual(status["pid"], 4242)
        self.assertEqual(status["rss_bytes"], 123456)
        self.assertEqual(status["protocol"], indexd_runtime.INDEXD_PROTOCOL)
        self.assertEqual(status["build"], indexd_runtime.INDEXD_BUILD_ID)
        self.assertLessEqual(status["heartbeat_age_s"], 0.1)

    def test_stopped_daemon_is_immediately_unresponsive(self) -> None:
        now = 1_000.0
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        with (
            mock.patch.object(common.time, "time", return_value=now),
            mock.patch.object(proc, "process_execution_state",
                              return_value="stopped"),
        ):
            state, age = indexd_runtime._indexd_responsiveness(inspection)
        self.assertEqual(state, "unresponsive")
        self.assertEqual(age, now - snapshot.mtime)

    def test_repeated_stale_heartbeat_polls_preserve_the_first_observation(
            self) -> None:
        now = 1_000.0
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        grace = indexd_runtime._INDEXD_RESPONSE_GRACE_S
        with mock.patch.object(
                proc, "process_execution_state", return_value="active"):
            for offset in (0.0, grace * 0.25, grace * 0.75):
                with mock.patch.object(
                        common.time, "time", return_value=now + offset):
                    self.assertEqual(
                        indexd_runtime._indexd_responsiveness(inspection)[0],
                        "checking")
                record = json.loads(
                    indexd_runtime._INDEXD_RESPONSE_PATH.read_text(
                        encoding="utf-8"))
                self.assertEqual(record["observed_at"], now)
            with mock.patch.object(
                    common.time, "time", return_value=now + grace + 0.1):
                self.assertEqual(
                    indexd_runtime._indexd_responsiveness(inspection)[0],
                    "unresponsive")

    def test_heartbeat_advance_resets_the_response_probe(self) -> None:
        now = 1_000.0
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        grace = indexd_runtime._INDEXD_RESPONSE_GRACE_S
        with (
            mock.patch.object(common.time, "time", return_value=now),
            mock.patch.object(proc, "process_execution_state",
                              return_value="active"),
        ):
            self.assertEqual(
                indexd_runtime._indexd_responsiveness(inspection)[0],
                "checking")

        advanced_at = now + grace * 0.75
        advanced = ownerfile.Snapshot(
            (1, 2, len(snapshot.raw), snapshot.identity[3] + 1),
            snapshot.mtime, snapshot.raw)
        resumed = inspection._replace(snapshot=advanced)
        with (
            mock.patch.object(common.time, "time", return_value=advanced_at),
            mock.patch.object(proc, "process_execution_state",
                              return_value="active"),
        ):
            self.assertEqual(
                indexd_runtime._indexd_responsiveness(resumed)[0],
                "checking")
        record = json.loads(indexd_runtime._INDEXD_RESPONSE_PATH.read_text(
            encoding="utf-8"))
        self.assertEqual(record["heartbeat"], advanced.identity[3])
        self.assertEqual(record["observed_at"], advanced_at)

        with (
            mock.patch.object(
                common.time, "time", return_value=advanced_at + grace * 0.75),
            mock.patch.object(proc, "process_execution_state",
                              return_value="active"),
        ):
            self.assertEqual(
                indexd_runtime._indexd_responsiveness(resumed)[0],
                "checking")

        fresh = ownerfile.Snapshot(
            advanced.identity,
            advanced_at + grace, snapshot.raw)
        healthy = inspection._replace(snapshot=fresh)
        with mock.patch.object(
                common.time, "time",
                return_value=advanced_at + grace):
            self.assertEqual(
                indexd_runtime._indexd_responsiveness(healthy)[0],
                "responsive")
        self.assertFalse(indexd_runtime._INDEXD_RESPONSE_PATH.exists())

    def test_malformed_response_probe_is_rebased_once(self) -> None:
        now = 1_000.0
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        indexd_runtime._INDEXD_RESPONSE_PATH.write_text(json.dumps({
            "token": indexd_runtime.indexd_generation_token(snapshot),
            "heartbeat": snapshot.identity[3],
            "observed_at": "invalid",
        }), encoding="utf-8")
        with (
            mock.patch.object(common.time, "time", return_value=now),
            mock.patch.object(proc, "process_execution_state",
                              return_value="active"),
        ):
            self.assertEqual(
                indexd_runtime._indexd_responsiveness(inspection)[0],
                "checking")
        record = json.loads(indexd_runtime._INDEXD_RESPONSE_PATH.read_text(
            encoding="utf-8"))
        self.assertEqual(record["observed_at"], now)

    def test_deep_response_probe_is_rebased_without_crashing(self) -> None:
        now = 1_000.0
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        indexd_runtime._INDEXD_RESPONSE_PATH.write_text(
            ("[" * 1500) + "0" + ("]" * 1500), encoding="utf-8")
        with (
            mock.patch.object(common.time, "time", return_value=now),
            mock.patch.object(proc, "process_execution_state",
                              return_value="active"),
        ):
            state, _age = indexd_runtime._indexd_responsiveness(inspection)
        self.assertEqual(state, "checking")
        record = json.loads(indexd_runtime._INDEXD_RESPONSE_PATH.read_text(
            encoding="utf-8"))
        self.assertEqual(record["observed_at"], now)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_special_response_probe_never_blocks_status(self) -> None:
        snapshot = self._snapshot()
        inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            snapshot, 4242, "birth")
        os.mkfifo(indexd_runtime._INDEXD_RESPONSE_PATH)
        with mock.patch.object(
                proc, "process_execution_state", return_value="active"):
            started = time.perf_counter()
            state, _age = indexd_runtime._indexd_responsiveness(inspection)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(state, "checking")
        self.assertTrue(indexd_runtime._INDEXD_RESPONSE_PATH.is_file())

    @unittest.skipIf(common.WIN, "SIGSTOP is POSIX-only")
    def test_sigstopped_real_daemon_is_not_reported_running(self) -> None:
        import signal

        script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "import common,indexd_runtime\n"
            "indexd_runtime.INDEXD_LOCK_PATH=Path(os.environ['OWNER'])\n"
            "indexd_runtime.INDEXD_READY_PATH=Path(os.environ['READY'])\n"
            "indexd_runtime.INDEXD_CHILD_PATH=Path(os.environ['CHILD'])\n"
            "if not common.bind_descendants_to_process_lifetime(): raise SystemExit(4)\n"
            "owner=indexd_runtime.acquire_indexd_owner()\n"
            "if owner is None: raise SystemExit(3)\n"
            "ready=indexd_runtime.publish_indexd_ready(owner)\n"
            "print('ready',flush=True)\n"
            "while True:\n"
            " indexd_runtime.heartbeat_indexd_owner(owner); time.sleep(0.05)\n"
        )
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(self.root),
            "OWNER": str(indexd_runtime.INDEXD_LOCK_PATH),
            "READY": str(indexd_runtime.INDEXD_READY_PATH),
            "CHILD": str(indexd_runtime.INDEXD_CHILD_PATH),
            "PYTHONPATH": str(PY_DIR),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", script], cwd=PY_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            deadline = time.monotonic() + 2.0
            healthy = {}
            while time.monotonic() < deadline:
                healthy = indexd_runtime.indexd_resource_status()
                if healthy.get("running"):
                    break
                time.sleep(0.02)
            self.assertTrue(healthy["running"])
            os.kill(process.pid, signal.SIGSTOP)
            deadline = time.monotonic() + 2.0
            status = {}
            while time.monotonic() < deadline:
                status = indexd_runtime.indexd_resource_status()
                if status.get("state") == "daemon-unresponsive":
                    break
                time.sleep(0.02)
            self.assertEqual(status.get("state"), "daemon-unresponsive")
            self.assertFalse(status["running"])
            self.assertTrue(status["blocked"])
        finally:
            try:
                os.kill(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            process.terminate()
            process.communicate(timeout=10)

    def test_indexd_import_does_not_leak_daemon_mode(self) -> None:
        script = (
            "import os\n"
            "before=os.environ.get('AGREP_INDEXD')\n"
            "import indexd\n"
            "raise SystemExit(0 if os.environ.get('AGREP_INDEXD')==before else 7)\n"
        )
        for label, value in (("absent", None), ("outer", "outer-mode")):
            with self.subTest(label=label):
                env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
                if value is None:
                    env.pop("AGREP_INDEXD", None)
                else:
                    env["AGREP_INDEXD"] = value
                process = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=PY_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10)
                self.assertEqual(
                    process.returncode, 0,
                    f"{process.stdout}\n{process.stderr}")

    def test_spawn_guard_exact_live_lifetime_serializes_real_contenders(
            self) -> None:
        fake_py = self.root / "fake-py"
        fake_py.mkdir()
        attempt_log = self.root / "attempts.log"
        launch_log = self.root / "launches.log"
        child_script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "import common,indexd_runtime\n"
            "start=common.process_start_identity(os.getpid())\n"
            "with Path(os.environ['AGREP_ATTEMPT_LOG']).open('a',encoding='ascii') as f:\n"
            " f.write(f'{os.getpid()} {start}\\n'); f.flush(); os.fsync(f.fileno())\n"
            "if not common.bind_descendants_to_process_lifetime(): raise SystemExit(4)\n"
            "owner=indexd_runtime.acquire_indexd_owner()\n"
            "if owner is None: raise SystemExit(3)\n"
            "ready=indexd_runtime.publish_indexd_ready(owner)\n"
            "with Path(os.environ['AGREP_LAUNCH_LOG']).open('a',encoding='ascii') as f:\n"
            " f.write(f'{os.getpid()} {start} {indexd_runtime.indexd_generation_token(owner.snapshot)}\\n')\n"
            " f.flush(); os.fsync(f.fileno())\n"
            "deadline=time.monotonic()+1.5\n"
            "while time.monotonic()<deadline:\n"
            " indexd_runtime.heartbeat_indexd_owner(owner); time.sleep(0.05)\n"
            "os._exit(0)\n"
        )
        (fake_py / "indexd.py").write_text(child_script, encoding="utf-8")
        holder_script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "import common,indexd_runtime,ownerfile\n"
            "start=common.process_start_identity(os.getpid())\n"
            "token='a'*32\n"
            "raw=f'state=launching pid={os.getpid()} start={start} token={token}\\n'.encode()\n"
            "guard=ownerfile.create_exclusive(indexd_runtime._SPAWN_GUARD_PATH,raw,retain_fd=True)\n"
            "print(f'{os.getpid()} {start} {token}',flush=True)\n"
            "stop=Path(os.environ['AGREP_HOLDER_STOP'])\n"
            "while not stop.exists(): time.sleep(0.01)\n"
            "guard.close()\n"
        )
        contender_script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "import common,indexd_runtime\n"
            "common.PY_DIR=Path(os.environ['AGREP_FAKE_PY'])\n"
            "indexd_runtime._INDEXD_ACQUIRE_WAIT_S=0.2\n"
            "indexd_runtime._INDEXD_READY_WAIT_S=1.5\n"
            "gate=Path(os.environ['AGREP_CONTENDER_GATE'])\n"
            "while not gate.exists(): time.sleep(0.005)\n"
            "result=indexd_runtime._spawn_indexd()\n"
            "print(f'{os.getpid()} {result.value}',flush=True)\n"
        )
        stop = self.root / "holder.stop"
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(self.root),
            "AGREP_FAKE_PY": str(fake_py),
            "AGREP_HOLDER_STOP": str(stop),
            "AGREP_ATTEMPT_LOG": str(attempt_log),
            "AGREP_LAUNCH_LOG": str(launch_log),
            "PYTHONPATH": str(PY_DIR),
        }
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script], cwd=PY_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        holder_line = holder.stdout.readline().strip()
        self.assertEqual(len(holder_line.split()), 3)
        guard_path = self.root / (
            f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.spawn")
        stale = time.time() - indexd_runtime._SPAWN_GUARD_S - 1.0
        os.utime(guard_path, (stale, stale))

        def contend(gate: Path):
            child_env = {**env, "AGREP_CONTENDER_GATE": str(gate)}
            return subprocess.Popen(
                [sys.executable, "-c", contender_script], cwd=PY_DIR,
                env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")

        first_gate = self.root / "first.gate"
        first = [contend(first_gate), contend(first_gate)]
        first_gate.touch()
        first_outputs = [process.communicate(timeout=10) for process in first]
        for process, (stdout, stderr) in zip(first, first_outputs, strict=True):
            self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
            self.assertTrue(stdout.strip().endswith("in-flight"), stdout)
        self.assertFalse(launch_log.exists())

        stop.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, 0, f"{holder_stdout}\n{holder_stderr}")

        second_gate = self.root / "second.gate"
        second = [contend(second_gate), contend(second_gate)]
        second_gate.touch()
        second_outputs = [process.communicate(timeout=10) for process in second]
        for process, (stdout, stderr) in zip(second, second_outputs, strict=True):
            self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
            self.assertIn(
                stdout.strip().split()[-1],
                {"ready", "in-flight", "blocked"})
        deadline = time.monotonic() + 5.0
        while ((not launch_log.exists() or launch_log.stat().st_size == 0)
               and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertTrue(launch_log.exists(), repr(second_outputs))
        launches = launch_log.read_text(encoding="ascii").splitlines()
        self.assertEqual(len(launches), 1)
        self.assertEqual(len(
            attempt_log.read_text(encoding="ascii").splitlines()), 1)
        child_pid, child_start, owner_generation = launches[0].split()
        self.assertTrue(child_pid.isdigit())
        self.assertTrue(child_start)
        self.assertEqual(len(owner_generation), 32)
        deadline = time.monotonic() + 3.0
        while common.pid_alive(int(child_pid)) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(common.pid_alive(int(child_pid)))

    def test_true_cold_launch_handoff_survives_launcher_exit_and_reclaims_once(
            self) -> None:
        fake_py = self.root / "delayed-py"
        fake_py.mkdir()
        indexd_runtime._SPAWN_GUARD_PATH = self.root / (
            f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.spawn")
        launch_log = self.root / "delayed-launches.log"
        done_dir = self.root / "delayed-done"
        done_dir.mkdir()
        write_gate = self.root / "delayed-write.gate"
        child_script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "pid=os.getpid()\n"
            "with Path(os.environ['AGREP_LAUNCH_LOG']).open('a',encoding='ascii') as f:\n"
            " gate=Path(os.environ['AGREP_WRITE_GATE'])\n"
            " while not gate.exists(): time.sleep(0.005)\n"
            " f.write(f'{pid}\\n'); f.flush(); os.fsync(f.fileno())\n"
            "release=Path(os.environ['AGREP_DONE_DIR'],f'{pid}.release')\n"
            "while not release.exists(): time.sleep(0.005)\n"
            "Path(os.environ['AGREP_DONE_DIR'],str(pid)).touch()\n"
        )
        (fake_py / "indexd.py").write_text(child_script, encoding="utf-8")
        launcher_script = (
            "import os,time\n"
            "from pathlib import Path\n"
            "import common,indexd_runtime\n"
            "common.PY_DIR=Path(os.environ['AGREP_FAKE_PY'])\n"
            "gate=os.environ.get('AGREP_LAUNCH_GATE')\n"
            "while gate and not Path(gate).exists(): time.sleep(0.005)\n"
            "print(indexd_runtime._spawn_indexd().value,flush=True)\n"
        )
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(self.root),
            "AGREP_FAKE_PY": str(fake_py),
            "AGREP_LAUNCH_LOG": str(launch_log),
            "AGREP_WRITE_GATE": str(write_gate),
            "AGREP_DONE_DIR": str(done_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PY_DIR),
        }

        launchers: list[subprocess.Popen] = []

        def launch(gate: Path | None = None) -> subprocess.Popen:
            child_env = dict(env)
            if gate is not None:
                child_env["AGREP_LAUNCH_GATE"] = str(gate)
            process = subprocess.Popen(
                [sys.executable, "-c", launcher_script], cwd=PY_DIR,
                env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            launchers.append(process)
            return process

        def read_launches() -> list[int]:
            try:
                raw = launch_log.read_bytes()
            except OSError:
                return []
            if not raw or not raw.endswith(b"\n"):
                return []
            rows = raw.decode("ascii").splitlines()
            if any(not row.isdigit() or int(row) <= 0 for row in rows):
                return []
            return [int(row) for row in rows]

        def await_launches(expected: int) -> list[int]:
            deadline = time.monotonic() + 5.0
            launches = read_launches()
            while len(launches) < expected and time.monotonic() < deadline:
                time.sleep(0.01)
                launches = read_launches()
            self.assertEqual(len(launches), expected)
            return launches

        def spawn_child_status() -> tuple[int | None, bool]:
            try:
                guard = indexd_runtime._inspect_spawn_guard()
            except FileNotFoundError:
                return None, False
            except OSError:
                return None, True
            if not guard.complete or guard.token is None:
                return None, True
            try:
                child = indexd_runtime._inspect_spawn_child(guard.token)
            except (OSError, ownerfile.OwnershipLost):
                return None, True
            if not child.complete:
                return None, True
            if child.owner in {
                    ownerfile.ProcessOwner.EXACT_LIVE,
                    ownerfile.ProcessOwner.UNVERIFIABLE,
            }:
                return child.child_pid, True
            return child.child_pid, False

        def drain_fixture_children() -> None:
            deadline = time.monotonic() + 6.0
            launches: set[int] = set()
            while time.monotonic() < deadline:
                launches.update(read_launches())
                child_pid, active = spawn_child_status()
                if child_pid is not None:
                    launches.add(child_pid)
                for pid in launches:
                    (done_dir / f"{pid}.release").touch()
                if (not active
                        and all(not common.pid_alive(pid) for pid in launches)):
                    return
                time.sleep(0.02)

        def collect(
                processes: list[subprocess.Popen],
                allowed: tuple[str, ...] = ("in-flight",),
        ) -> None:
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                outputs.append((process, stdout, stderr))
            for process, stdout, stderr in outputs:
                self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
                self.assertIn(stdout.strip(), allowed)

        try:
            first = launch()
            collect([first])
            deadline = time.monotonic() + 5.0
            while not launch_log.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(launch_log.exists())
            self.assertEqual(launch_log.stat().st_size, 0)
            write_gate.touch()
            first_pid = await_launches(1)[0]
            self.assertEqual(spawn_child_status(), (first_pid, True))

            live_gate = self.root / "live-contenders.gate"
            live = [launch(live_gate), launch(live_gate)]
            live_gate.touch()
            collect(live)
            self.assertEqual(read_launches(), [first_pid])
            self.assertEqual(spawn_child_status(), (first_pid, True))

            done = done_dir / str(first_pid)
            (done_dir / f"{first_pid}.release").touch()
            deadline = time.monotonic() + 5.0
            while (not done.exists() or common.pid_alive(first_pid)) \
                    and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(done.exists())
            self.assertFalse(common.pid_alive(first_pid))

            dead_gate = self.root / "dead-contenders.gate"
            dead = [launch(dead_gate), launch(dead_gate)]
            dead_gate.touch()
            collect(dead, ("in-flight", "blocked"))
            launches = await_launches(2)
            second_pid = launches[1]
            self.assertEqual(spawn_child_status(), (second_pid, True))
            second_done = done_dir / str(second_pid)
            (done_dir / f"{second_pid}.release").touch()
            deadline = time.monotonic() + 5.0
            while (not second_done.exists() or common.pid_alive(second_pid)) \
                    and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(second_done.exists())
            self.assertFalse(common.pid_alive(second_pid))
        finally:
            write_gate.touch()
            for launcher in launchers:
                if launcher.poll() is None:
                    try:
                        launcher.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            launcher.terminate()
                        except OSError:
                            pass
                        try:
                            launcher.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            try:
                                launcher.kill()
                            except OSError:
                                pass
                            launcher.communicate(timeout=2)
            drain_fixture_children()

    def test_live_watcher_mode_cannot_change_after_startup(self) -> None:
        existing = mock.Mock()
        existing._headless_indexd = False
        saved = indexd.live._WATCHER
        indexd.live._WATCHER = existing
        try:
            self.assertIs(indexd.live.watcher(), existing)
            with self.assertRaisesRegex(
                    RuntimeError, "mode cannot change after startup"):
                indexd.live.watcher(headless_indexd=True)
        finally:
            indexd.live._WATCHER = saved

    def test_crashed_subprocess_owner_is_reclaimed(self) -> None:
        script = (
            "import common,indexd_runtime,os\n"
            "if not common.bind_descendants_to_process_lifetime(): raise SystemExit(4)\n"
            "owner=indexd_runtime.acquire_indexd_owner()\n"
            "if owner is None: raise SystemExit(3)\n"
            "print(owner.snapshot.raw.decode('ascii').strip(),flush=True)\n"
            "os._exit(0)\n"
        )
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(self.root),
            "PYTHONPATH": str(PY_DIR),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=PY_DIR, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", start_new_session=not common.WIN)
        child_pid = process.pid
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(
            process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertIn(f"pid={child_pid}", stdout)
        self.assertTrue(self.path.exists())
        self.assertFalse(common.pid_alive(child_pid))
        replacement = self._acquire()
        self.assertIsInstance(replacement, ownerfile.Handle)
        self.assertIn(
            f"pid={os.getpid()}".encode(), replacement.snapshot.raw)
        self.assertTrue(self._release(replacement))

    def test_timestamp_enablement_is_reversible_and_scoped_to_the_test(self) -> None:
        # P13: enable_log_timestamps() had no inverse, so a main() test left
        # every later bare-log assertion staring at an ISO prefix.
        common.enable_log_timestamps()
        stamped = io.StringIO()
        with contextlib.redirect_stderr(stamped):
            common.log("assert-line")
        self.assertTrue(stamped.getvalue().endswith(" assert-line\n"))
        self.assertNotEqual(stamped.getvalue(), "assert-line\n")
        common.disable_log_timestamps()
        bare = io.StringIO()
        with contextlib.redirect_stderr(bare):
            common.log("assert-line")
        self.assertEqual(bare.getvalue(), "assert-line\n")

    def test_main_gives_the_interpreter_back_unstamped(self) -> None:
        # The whole-tree failure shape: run the real main() (which enables
        # stamps at entry), then prove this fixture's tearDown restores the
        # seam a later module's log assertions depend on.
        before = _log._TIMESTAMPS
        with mock.patch.object(indexd.common, "ingest_bin",
                               return_value=self.root / "missing"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(indexd.main(), 0)
        self.assertTrue(_log._TIMESTAMPS)
        self.tearDown()
        try:
            self.assertEqual(_log._TIMESTAMPS, before)
            if not before:
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    common.log("assert-line")
                self.assertEqual(output.getvalue(), "assert-line\n")
        finally:
            self.setUp()


if __name__ == "__main__":
    unittest.main()
