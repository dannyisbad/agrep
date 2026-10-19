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
import common  # noqa: E402
import embed  # noqa: E402
import ownerfile  # noqa: E402
import semantic  # noqa: E402


PY_DIR = Path(__file__).resolve().parent


class OwnerFileTests(unittest.TestCase):
    def setUp(self) -> None:
        embed._release_claim()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_data = common.DATA_DIR
        common.DATA_DIR = self.root

    def tearDown(self) -> None:
        embed._release_claim()
        common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def _spawn(self, script: str, *args: Path) -> subprocess.Popen:
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        return subprocess.Popen(
            [sys.executable, "-c", script, *(str(arg) for arg in args)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _wait_path(self, path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5.0
        while not path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if not path.exists():
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"child did not publish {path.name}: {stdout} {stderr}")

    def _finish(self, process: subprocess.Popen) -> None:
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")

    def test_process_classification_uses_kernel_birth_identity(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = common.process_start_identity(process.pid)
            if start is None:
                self.skipTest("kernel process birth identity unavailable")
            exact = ownerfile.classify_process(
                process.pid, start, pid_alive=common.pid_alive,
                process_start=common.process_start_identity)
            reused = ownerfile.classify_process(
                process.pid, f"{start}-other", pid_alive=common.pid_alive,
                process_start=common.process_start_identity)
            self.assertIs(exact, ownerfile.ProcessOwner.EXACT_LIVE)
            self.assertIs(reused, ownerfile.ProcessOwner.REUSED)
        finally:
            process.terminate()
            process.wait(timeout=5)
        dead = ownerfile.classify_process(
            process.pid, start, pid_alive=common.pid_alive,
            process_start=common.process_start_identity)
        self.assertIs(dead, ownerfile.ProcessOwner.DEAD)

    def test_embed_keeps_recent_empty_publication_then_reclaims_crash(self) -> None:
        path = semantic.embed_claim_path()
        ready = self.root / "ready"
        script = """
import os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
pathlib.Path(sys.argv[2]).write_text("ready", encoding="ascii")
time.sleep(30)
"""
        process = self._spawn(script, path, ready)
        try:
            self._wait_path(ready, process)
            self.assertFalse(embed._acquire_claim())
            self.assertTrue(path.exists())
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        old = time.time() - embed._MALFORMED_CLAIM_STALE_S - 1
        os.utime(path, (old, old))
        self.assertTrue(embed._acquire_claim())
        embed._release_claim()
        self.assertFalse(path.exists())
        path.write_bytes(b"")
        future = time.time() + 3600
        os.utime(path, (future, future))
        self.assertTrue(embed._acquire_claim())
        embed._release_claim()
        self.assertFalse(path.exists())

    def test_post_create_verification_detects_process_delete_and_replace(self) -> None:
        script = """
import pathlib, sys, time
import ownerfile
path, ready, go, result = map(pathlib.Path, sys.argv[1:])
real_snapshot = ownerfile.snapshot
def gated(target, *args, **kwargs):
    ready.write_text("ready", encoding="ascii")
    while not go.exists():
        time.sleep(0.01)
    return real_snapshot(target, *args, **kwargs)
ownerfile.snapshot = gated
try:
    ownerfile.create_exclusive(path, b"first-owner")
except ownerfile.OwnershipLost:
    result.write_text("lost", encoding="ascii")
else:
    result.write_text("owned", encoding="ascii")
"""
        for label, body in (("delete", None), ("replace", b"second-owner")):
            path = self.root / f"owner-{label}.lock"
            ready = self.root / f"ready-{label}"
            go = self.root / f"go-{label}"
            result = self.root / f"result-{label}"
            process = self._spawn(script, path, ready, go, result)
            self._wait_path(ready, process)
            replacement = self.root / f"replacement-{label}"
            mutation_denied = False
            try:
                if body is None:
                    path.unlink()
                else:
                    replacement.write_bytes(body)
                    os.replace(replacement, path)
            except OSError as exc:
                sharing_error = (
                    isinstance(exc, PermissionError)
                    or getattr(exc, "winerror", None) in (5, 32, 33))
                if os.name != "nt" or not sharing_error:
                    raise
                mutation_denied = True
            finally:
                go.write_text("go", encoding="ascii")
            self._finish(process)
            if mutation_denied:
                self.assertEqual(result.read_text(encoding="ascii"), "owned")
                self.assertEqual(path.read_bytes(), b"first-owner")
                path.unlink()
                replacement.unlink(missing_ok=True)
            else:
                self.assertEqual(result.read_text(encoding="ascii"), "lost")
                if body is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), body)

    def test_late_release_preserves_replacement_and_same_body_aba(self) -> None:
        for body in (b"replacement", b"same-owner-body"):
            path = self.root / f"owner-{len(body)}.lock"
            ready = self.root / f"ready-{len(body)}"
            go = self.root / f"go-{len(body)}"
            result = self.root / f"result-{len(body)}"
            script = """
import pathlib, sys, time
import ownerfile
path, ready, go, result = map(pathlib.Path, sys.argv[1:])
handle = ownerfile.create_exclusive(path, b"same-owner-body")
ready.write_text("ready", encoding="ascii")
while not go.exists():
    time.sleep(0.01)
result.write_text(str(handle.release()), encoding="ascii")
"""
            process = self._spawn(script, path, ready, go, result)
            self._wait_path(ready, process)
            replacement = self.root / f"replacement-{len(body)}"
            replacement.write_bytes(body)
            os.replace(replacement, path)
            go.write_text("go", encoding="ascii")
            self._finish(process)
            self.assertEqual(result.read_text(encoding="ascii"), "False")
            self.assertEqual(path.read_bytes(), body)

    def _mutex_gap(self, path: Path, land):
        """Patch the slot-mutex seam so `land()` runs after the caller's judgement and
        before the locked re-check; returns the patch and a call counter."""
        real_mutex = ownerfile._slot_mutex
        calls = []

        def gap_then_lock(record, **kwargs):
            if record == path:
                calls.append(record)
                land()
            return real_mutex(record, **kwargs)

        return mock.patch.object(ownerfile, "_slot_mutex", side_effect=gap_then_lock), calls

    def test_default_release_leaves_a_same_body_replacement_untouched(self) -> None:
        path = self.root / "precheck-owner.lock"
        replacement = self.root / "precheck-replacement"
        handle = ownerfile.create_exclusive(path, b"same-owner-body")
        replacement.write_bytes(b"same-owner-body")
        patch, swaps = self._mutex_gap(path, lambda: os.replace(replacement, path))
        with patch:
            self.assertFalse(handle.release())
        self.assertEqual(len(swaps), 1)
        self.assertEqual(path.read_bytes(), b"same-owner-body")
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])
        self.assertFalse((self.root / ownerfile.SLOT_MUTEX_NAME).exists())

    def test_a_live_record_swapped_in_after_the_judgement_is_never_removed(self) -> None:
        path = self.root / "gap-owner.lock"
        live = self.root / "gap-live"
        stale = ownerfile.create_exclusive(path, b"stale-owner").snapshot
        live.write_bytes(b"live-owner")
        identity = {}

        def swap_in_the_live_record() -> None:
            os.replace(live, path)
            identity["live"] = ownerfile.snapshot(path).identity[:3]

        patch, _calls = self._mutex_gap(path, swap_in_the_live_record)
        with patch:
            self.assertFalse(ownerfile.remove_exact(path, stale))
        observed = ownerfile.snapshot(path)
        self.assertEqual((observed.identity[:3], observed.raw), (identity["live"], b"live-owner"))
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])

    def test_removals_in_a_directory_are_serialized_by_its_mutex(self) -> None:
        path = self.root / "serialized-owner.lock"
        stale = ownerfile.create_exclusive(path, b"stale-owner").snapshot
        with mock.patch.object(ownerfile, "_SLOT_MUTEX_WAIT_S", 0.05):
            with ownerfile._SlotMutex(self.root) as held:
                self.assertEqual(held.state, "held")
                with ownerfile._SlotMutex(self.root) as contender:
                    self.assertEqual(contender.state, "busy")
                self.assertFalse(ownerfile.remove_exact(path, stale))
                self.assertEqual(path.read_bytes(), b"stale-owner")
            self.assertTrue(ownerfile.remove_exact(path, stale))
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])
        self.assertTrue((self.root / ownerfile.SLOT_MUTEX_NAME).is_file())

    def test_unsupported_mutex_falls_back_to_the_tomb_protocol(self) -> None:
        path = self.root / "fallback-owner.lock"
        handle = ownerfile.create_exclusive(path, b"fallback-owner")
        renames = []
        real_replace = ownerfile.os.replace

        def record_rename(source, target):
            renames.append((Path(source).name, Path(target).name))
            return real_replace(source, target)

        with mock.patch.object(ownerfile, "_lock_descriptor", return_value="unsupported"), \
                mock.patch.object(ownerfile.os, "replace", side_effect=record_rename):
            self.assertTrue(handle.release())
        self.assertFalse(path.exists())
        self.assertEqual(len(renames), 1)
        self.assertIn(".owner-reap-", renames[0][1])
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])

    def test_removal_renames_aside_under_the_mutex(self) -> None:
        path = self.root / "aside-owner.lock"
        stale = ownerfile.create_exclusive(path, b"stale-owner").snapshot
        real_unlink = Path.unlink
        seen = {}

        def observe_unlink(target: Path, *args, **kwargs):
            if ".owner-reap-" in target.name:
                with mock.patch.object(ownerfile, "_SLOT_MUTEX_WAIT_S", 0.05):
                    with ownerfile._SlotMutex(self.root) as contender:
                        seen["mutex"] = contender.state
                seen["slot"] = path.exists()
                seen["tomb"] = target.read_bytes()
            return real_unlink(target, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=observe_unlink):
            self.assertTrue(ownerfile.remove_exact(path, stale))
        # the slot is free, and the mutex still held, while the tomb is unlinked
        self.assertEqual(seen, {"mutex": "busy", "slot": False, "tomb": b"stale-owner"})
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])

    def test_a_failed_create_never_mints_the_mutex_but_honours_one(self) -> None:
        path = self.root / "unfinished-owner.lock"
        short_write = mock.patch.object(os, "write", return_value=0)
        with short_write:
            with self.assertRaisesRegex(OSError, "short write"):
                ownerfile.create_exclusive(path, b"unfinished-owner")
        self.assertEqual(os.listdir(self.root), [])

        # a reap minted the mutex: the creator's cleanup then waits on it like any remover
        stale = ownerfile.create_exclusive(path, b"stale-owner").snapshot
        self.assertTrue(ownerfile.remove_exact(path, stale))
        with mock.patch.object(ownerfile, "_SLOT_MUTEX_WAIT_S", 0.05), \
                ownerfile._SlotMutex(self.root) as held:
            self.assertEqual(held.state, "held")
            with short_write:
                with self.assertRaisesRegex(OSError, "short write"):
                    ownerfile.create_exclusive(path, b"unfinished-owner")
            self.assertEqual(path.read_bytes(), b"")
        partial = ownerfile.snapshot(path)
        self.assertTrue(ownerfile.remove_exact(
            path, partial, tombstone=False, mint_mutex=False))
        self.assertEqual(os.listdir(self.root), [ownerfile.SLOT_MUTEX_NAME])

    def test_remove_record_dir_treats_a_lone_mutex_as_empty(self) -> None:
        namespace = self.root / "namespace"
        namespace.mkdir()
        self.assertTrue(ownerfile.remove_record_dir(namespace))
        self.assertFalse(namespace.exists())
        namespace.mkdir()
        path = namespace / "worker.lock"
        stale = ownerfile.create_exclusive(path, b"stale-owner").snapshot
        self.assertFalse(ownerfile.remove_record_dir(namespace))
        self.assertTrue(ownerfile.remove_exact(path, stale))
        self.assertEqual(os.listdir(namespace), [ownerfile.SLOT_MUTEX_NAME])
        with mock.patch.object(ownerfile, "_SLOT_MUTEX_WAIT_S", 0.05):
            with ownerfile._SlotMutex(namespace) as held:
                self.assertEqual(held.state, "held")
                # a remover is active in the directory: nothing is touched
                self.assertFalse(ownerfile.remove_record_dir(namespace))
                self.assertEqual(os.listdir(namespace), [ownerfile.SLOT_MUTEX_NAME])
        self.assertTrue(ownerfile.remove_record_dir(namespace))
        self.assertFalse(namespace.exists())
        self.assertFalse(ownerfile.remove_record_dir(namespace))

    def test_a_delete_pending_slot_is_contention_only_on_windows(self) -> None:
        path = self.root / "pending-owner.lock"
        denied = PermissionError(13, "Access is denied")
        with mock.patch.object(ownerfile.os, "open", side_effect=denied), \
                mock.patch.object(Path, "lstat", side_effect=PermissionError(13, "pending")):
            with mock.patch.object(ownerfile, "_WINDOWS", True):
                with self.assertRaises(FileExistsError):
                    ownerfile.create_exclusive(path, b"owner")
            with mock.patch.object(ownerfile, "_WINDOWS", False):
                with self.assertRaisesRegex(PermissionError, "Access is denied"):
                    ownerfile.create_exclusive(path, b"owner")
        with mock.patch.object(ownerfile.os, "open", side_effect=denied), \
                mock.patch.object(ownerfile, "_WINDOWS", True):
            # an absent entry refused by the directory itself is still a real error
            with self.assertRaisesRegex(PermissionError, "Access is denied"):
                ownerfile.create_exclusive(path, b"owner")

    def test_oversized_create_fails_before_publishing_an_entry(self) -> None:
        path = self.root / "oversized-owner.lock"
        with self.assertRaisesRegex(ValueError, "owner record exceeds"):
            ownerfile.create_exclusive(
                path, b"x" * (ownerfile._MAX_RECORD_BYTES + 1))
        self.assertFalse(path.exists())
        handle = ownerfile.create_exclusive(path, b"owner")
        self.assertTrue(handle.release())

    def test_create_held_registers_the_token_before_the_create(self) -> None:
        path = self.root / "held-owner.lock"
        start = "birth"
        seen = {}
        real_create = ownerfile.create_exclusive

        def observe(target, raw, **kwargs):
            # a sibling thread judging the fresh record in the create window finds the token held
            seen["during"] = ownerfile.stranded_own_record(
                os.getpid(), start, "a" * 32, process_start=lambda _pid: start)
            return real_create(target, raw, **kwargs)

        with mock.patch.object(ownerfile, "create_exclusive", side_effect=observe):
            handle = ownerfile.create_held(path, b"owner", token="a" * 32)
        self.assertEqual(handle.token, "a" * 32)
        self.assertEqual(seen, {"during": False})
        self.assertFalse(ownerfile.stranded_own_record(
            os.getpid(), start, "a" * 32, process_start=lambda _pid: start))
        with mock.patch.object(ownerfile, "_lock_descriptor", return_value="busy"), \
                mock.patch.object(ownerfile, "_SLOT_MUTEX_WAIT_S", 0.01):
            (self.root / ownerfile.SLOT_MUTEX_NAME).touch()
            self.assertFalse(handle.release())
        # a release that gave up leaves the record stranded, and only its own process sees that
        self.assertEqual(path.read_bytes(), b"owner")
        self.assertTrue(ownerfile.stranded_own_record(
            os.getpid(), start, "a" * 32, process_start=lambda _pid: start))
        self.assertFalse(ownerfile.stranded_own_record(
            os.getpid() + 1, start, "a" * 32, process_start=lambda _pid: start))
        self.assertFalse(ownerfile.stranded_own_record(
            os.getpid(), "other-birth", "a" * 32, process_start=lambda _pid: start))
        self.assertFalse(ownerfile.stranded_own_record(
            os.getpid(), "unknown", "a" * 32, process_start=lambda _pid: "unknown"))
        self.assertFalse(ownerfile.stranded_own_record(
            os.getpid(), start, None, process_start=lambda _pid: start))
        with self.assertRaises(FileExistsError):
            ownerfile.create_held(path, b"owner", token="b" * 32)
        self.assertNotIn("b" * 32, ownerfile._HELD_TOKENS)
        path.unlink()

    def test_full_record_retained_fd_and_exact_release(self) -> None:
        path = self.root / "retained.lock"
        raw = bytes(range(256)) * 128
        handle = ownerfile.create_exclusive(
            path, raw, fsync=True, retain_fd=True)
        self.assertIsNotNone(handle.fd)
        self.assertEqual(ownerfile.snapshot(path).raw, raw)
        self.assertTrue(handle.release(tombstone=True))
        self.assertFalse(path.exists())

    def test_handle_verify_rejects_replacement_and_released_state(self) -> None:
        path = self.root / "verified-owner.lock"
        replacement = self.root / "verified-owner-replacement"
        handle = ownerfile.create_exclusive(path, b"owner")
        self.assertEqual(handle.verify().raw, b"owner")
        replacement.write_bytes(b"owner")
        os.replace(replacement, path)
        with self.assertRaises(ownerfile.OwnershipLost):
            handle.verify()
        self.assertFalse(handle.release(tombstone=True))
        self.assertEqual(path.read_bytes(), b"owner")
        path.unlink()

        released = ownerfile.create_exclusive(path, b"owner")
        self.assertTrue(released.release(tombstone=True))
        with self.assertRaises(ownerfile.OwnershipLost):
            released.verify()

    def test_retained_handle_verifies_through_transient_reopen_denial(self) -> None:
        path = self.root / "retained-verify.lock"
        handle = ownerfile.create_exclusive(
            path, b"retained-owner", retain_fd=True)
        with mock.patch.object(
                ownerfile, "snapshot",
                side_effect=PermissionError(13, "sharing violation")):
            self.assertEqual(handle.verify().raw, b"retained-owner")
        self.assertTrue(handle.release(tombstone=True))

        path = self.root / "closed-verify.lock"
        handle = ownerfile.create_exclusive(path, b"closed-owner")
        with mock.patch.object(
                ownerfile, "snapshot",
                side_effect=PermissionError(13, "sharing violation")), \
                self.assertRaises(ownerfile.OwnershipLost):
            handle.verify()
        self.assertTrue(handle.release(tombstone=True))

    def test_retained_fd_verifies_when_path_reopen_is_transiently_denied(self) -> None:
        raw = b"retained-owner"
        for retain_fd in (False, True):
            path = self.root / f"transient-reopen-{retain_fd}.lock"
            with mock.patch.object(
                    ownerfile, "snapshot",
                    side_effect=PermissionError(13, "sharing violation")):
                handle = ownerfile.create_exclusive(
                    path, raw, retain_fd=retain_fd)
            self.assertEqual(handle.snapshot.raw, raw)
            self.assertEqual(handle.fd is not None, retain_fd)
            self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(handle.release(tombstone=True))
            self.assertFalse(path.exists())

    def test_create_verify_and_retention_matrix(self) -> None:
        for verify in (False, True):
            for retain_fd in (False, True):
                path = self.root / f"create-{verify}-{retain_fd}.lock"
                handle = ownerfile.create_exclusive(
                    path, b"owner", verify=verify, retain_fd=retain_fd)
                self.assertEqual(handle.snapshot.raw, b"owner")
                self.assertEqual(handle.fd is not None, retain_fd)
                self.assertTrue(handle.release(tombstone=True))

    def test_snapshot_rejects_nonregular_and_linked_entries(self) -> None:
        directory = self.root / "owner-directory"
        directory.mkdir()
        with self.assertRaises(OSError):
            ownerfile.snapshot(directory)

        original = self.root / "original-owner"
        saved = self.root / "saved-owner"
        original.write_bytes(b"same-owner-body")
        os.link(original, saved)
        original.unlink()
        try:
            original.symlink_to(saved)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(OSError):
            ownerfile.snapshot(original)
        self.assertEqual(saved.read_bytes(), b"same-owner-body")
        self.assertTrue(original.is_symlink())

    def test_create_normalizes_only_proven_existing_entry_errors(self) -> None:
        existing = self.root / "existing-owner"
        existing.mkdir()
        denied = PermissionError(13, "sharing violation")
        with mock.patch.object(ownerfile.os, "open", side_effect=denied):
            with self.assertRaises(FileExistsError) as collision:
                ownerfile.create_exclusive(existing, b"owner")
        self.assertIs(collision.exception.__cause__, denied)

        missing = self.root / "missing-owner"
        denied = PermissionError(13, "write denied")
        with mock.patch.object(ownerfile.os, "open", side_effect=denied):
            with self.assertRaises(PermissionError) as failure:
                ownerfile.create_exclusive(missing, b"owner")
        self.assertIs(failure.exception, denied)

    @unittest.skipIf(os.name == "nt", "POSIX simulates a Windows link-following open")
    def test_snapshot_rejects_link_to_saved_inode_after_first_check(self) -> None:
        path = self.root / "owner-race"
        saved = self.root / "owner-race-saved"
        path.write_bytes(b"same-owner-body")
        os.link(path, saved)
        real_open = ownerfile.os.open

        def replace_then_open(target, flags, *args):
            path.unlink()
            path.symlink_to(saved)
            flags &= ~getattr(os, "O_NOFOLLOW", 0)
            return real_open(target, flags, *args)

        with mock.patch.object(
                ownerfile.os, "open", side_effect=replace_then_open):
            with self.assertRaises(OSError):
                ownerfile.snapshot(path)
        self.assertTrue(path.is_symlink())
        self.assertEqual(saved.read_bytes(), b"same-owner-body")

    def test_plain_entry_rejects_windows_reparse_attribute(self) -> None:
        path = self.root / "reparse-owner"
        path.write_bytes(b"owner")
        info = path.lstat()
        reparse = mock.Mock(
            st_mode=info.st_mode,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with mock.patch.object(Path, "lstat", return_value=reparse):
            with self.assertRaises(OSError):
                ownerfile._plain_entry(path)

    @unittest.skipIf(os.name == "nt", "Windows denies replacing an open owner file")
    def test_snapshot_rejects_path_swap_after_open(self) -> None:
        path = self.root / "snapshot-race"
        replacement = self.root / "snapshot-replacement"
        path.write_bytes(b"same-owner-body")
        replacement.write_bytes(b"same-owner-body")
        plain_entry = ownerfile._plain_entry
        calls = 0

        def swap_on_second_check(target: Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(replacement, target)
            return plain_entry(target)

        with mock.patch.object(ownerfile, "_plain_entry",
                               side_effect=swap_on_second_check):
            with self.assertRaises(ownerfile.OwnershipLost):
                ownerfile.snapshot(path)
        self.assertEqual(calls, 2)
        self.assertEqual(path.read_bytes(), b"same-owner-body")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO race is POSIX-only")
    def test_snapshot_does_not_block_on_post_check_fifo_swap(self) -> None:
        path = self.root / "fifo-race"
        fifo = self.root / "fifo-replacement"
        path.write_bytes(b"owner")
        os.mkfifo(fifo)
        real_open = ownerfile.os.open

        def swap_then_open(target, flags, *args):
            path.unlink()
            os.replace(fifo, path)
            return real_open(target, flags, *args)

        with mock.patch.object(
                ownerfile.os, "open", side_effect=swap_then_open):
            with self.assertRaises(OSError):
                ownerfile.snapshot(path)
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))

    @unittest.skipIf(os.name == "nt", "Windows denies replacing a retained owner file")
    def test_retained_fallback_rechecks_path_after_read(self) -> None:
        path = self.root / "retained-post-read-race"
        replacement = self.root / "retained-post-read-replacement"
        replacement.write_bytes(b"retained-owner")
        plain_entry = ownerfile._plain_entry
        calls = 0

        def swap_on_second_check(target: Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(replacement, target)
            return plain_entry(target)

        with mock.patch.object(
                ownerfile, "snapshot",
                side_effect=PermissionError(13, "sharing violation")), \
                mock.patch.object(
                    ownerfile, "_plain_entry", side_effect=swap_on_second_check):
            with self.assertRaises(ownerfile.OwnershipLost):
                ownerfile.create_exclusive(
                    path, b"retained-owner", retain_fd=True)
        self.assertEqual(calls, 2)
        self.assertEqual(path.read_bytes(), b"retained-owner")

    @unittest.skipIf(os.name == "nt", "Windows denies replacing a retained file")
    def test_retained_fallback_rejects_same_and_different_replacements(self) -> None:
        for index, replacement_body in enumerate(
                (b"retained-owner", b"replacement-owner")):
            path = self.root / f"retained-race-{index}.lock"
            fired = False

            def deny_after_replace(target: Path, **_kwargs):
                nonlocal fired
                if not fired:
                    fired = True
                    replacement = self.root / f"replacement-{index}"
                    replacement.write_bytes(replacement_body)
                    os.replace(replacement, target)
                raise PermissionError(13, "sharing violation")

            with mock.patch.object(
                    ownerfile, "snapshot", side_effect=deny_after_replace):
                with self.assertRaises(ownerfile.OwnershipLost):
                    ownerfile.create_exclusive(
                        path, b"retained-owner", retain_fd=True)
            self.assertTrue(fired)
            self.assertEqual(path.read_bytes(), replacement_body)

    def test_strict_release_preserves_in_place_mtime_change(self) -> None:
        strict_path = self.root / "strict.lock"
        strict = ownerfile.create_exclusive(strict_path, b"same-owner-body")
        changed = strict.snapshot.identity[3] + 1_000_000_000
        os.utime(strict_path, ns=(changed, changed))
        self.assertNotEqual(ownerfile.snapshot(strict_path).identity[3],
                            strict.snapshot.identity[3])
        self.assertFalse(strict.release(
            tombstone=True, require_stable_mtime=True))
        self.assertEqual(strict_path.read_bytes(), b"same-owner-body")

        tolerant_path = self.root / "tolerant.lock"
        tolerant = ownerfile.create_exclusive(tolerant_path, b"same-owner-body")
        changed = tolerant.snapshot.identity[3] + 1_000_000_000
        os.utime(tolerant_path, ns=(changed, changed))
        self.assertTrue(tolerant.release(tombstone=True))
        self.assertFalse(tolerant_path.exists())

    def test_release_retries_transient_exact_snapshot_failure(self) -> None:
        path = self.root / "retry-release.lock"
        handle = ownerfile.create_exclusive(path, b"same-owner-body")
        snapshot = ownerfile.snapshot
        calls = 0

        def transient(target: Path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError(13, "sharing violation")
            return snapshot(target, **kwargs)

        with mock.patch.object(ownerfile, "snapshot", side_effect=transient):
            self.assertTrue(handle.release(tombstone=True))
        self.assertGreaterEqual(calls, 3)
        self.assertFalse(path.exists())
        self.assertFalse(handle.release(tombstone=True))

    def test_release_with_transient_unlink_failures_never_touches_a_new_owner(self) -> None:
        path = self.root / "retry-unlink-release.lock"
        handle = ownerfile.create_exclusive(path, b"same-owner-body")
        real_unlink = Path.unlink
        attempts = 0
        newcomer = {}

        def transient_unlink(target: Path, *args, **kwargs):
            nonlocal attempts
            if ".owner-reap-" not in target.name:
                return real_unlink(target, *args, **kwargs)
            attempts += 1
            if attempts == 1:
                # the record is already out of the slot: a newcomer takes it at once
                newcomer["handle"] = ownerfile.create_exclusive(path, b"new-owner-body")
            if attempts <= 3:
                raise PermissionError(13, "sharing violation")
            real_unlink(target, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=transient_unlink):
            self.assertTrue(handle.release(tombstone=True))
        self.assertEqual(attempts, 4)
        self.assertEqual(path.read_bytes(), b"new-owner-body")
        newcomer["handle"].verify()
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])

    def test_release_gives_up_rather_than_disturb_the_slot(self) -> None:
        path = self.root / "stuck-unlink-release.lock"
        handle = ownerfile.create_exclusive(path, b"same-owner-body")
        real_unlink = Path.unlink

        def stuck_unlink(target: Path, *args, **kwargs):
            if ".owner-reap-" in target.name:
                raise PermissionError(13, "sharing violation")
            return real_unlink(target, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=stuck_unlink):
            self.assertFalse(handle.release(tombstone=True))
        # the record is back in its slot, the same entry it always was
        restored = ownerfile.snapshot(path)
        self.assertEqual(restored.raw, b"same-owner-body")
        self.assertEqual(restored.identity[:3], handle.snapshot.identity[:3])
        for tomb in self.root.glob(".*.owner-reap-*"):
            self.assertEqual(ownerfile.snapshot(tomb).identity[:3], restored.identity[:3])

    def test_release_never_removes_after_ambiguous_close_error(self) -> None:
        path = self.root / "close-error.lock"
        handle = ownerfile.create_exclusive(
            path, b"same-owner-body", retain_fd=True)
        fd = handle.fd
        with mock.patch.object(
                ownerfile.os, "close", side_effect=OSError("close failed")), \
                mock.patch.object(ownerfile, "remove_exact") as remove:
            with self.assertRaisesRegex(OSError, "^close failed$"):
                handle.release(tombstone=True)
        remove.assert_not_called()
        self.assertIsNone(handle.fd)
        self.assertTrue(path.exists())
        os.close(fd)
        path.unlink()

    def test_embed_preserves_json_shape_and_replacement_owner(self) -> None:
        path = semantic.embed_claim_path()
        self.assertTrue(embed._acquire_claim())
        first_raw = path.read_text(encoding="utf-8")
        first = json.loads(first_raw)
        self.assertEqual(
            list(first), ["pid", "process_start", "token", "started_at"])
        self.assertEqual(len(first["token"]), 32)
        replacement = json.dumps({
            "pid": 999999, "process_start": "other", "token": "b" * 32,
            "started_at": time.time(),
        })
        path.write_text(replacement, encoding="utf-8")
        embed._release_claim()
        self.assertEqual(path.read_text(encoding="utf-8"), replacement)
        path.unlink()
        self.assertTrue(embed._acquire_claim())
        second = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["token"], second["token"])
        embed._release_claim()


if __name__ == "__main__":
    unittest.main()
