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


PY_DIR = Path(__file__).resolve().parent


class SemanticWorkerStartClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_data = common.DATA_DIR
        common.DATA_DIR = self.root
        self.handles: list[ownerfile.Handle] = []

    def tearDown(self) -> None:
        for handle in self.handles:
            handle.close()
        common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    @property
    def path(self) -> Path:
        return semworker.start_claim_path()

    def _raw(self, *, pid: int = 4242, at: float = 100.0,
             process_start: object = "birth", nonce: str = "a" * 32) -> bytes:
        return json.dumps({
            "pid": pid, "at": at, "process_start": process_start,
            "nonce": nonce,
        }, separators=(",", ":")).encode()

    def _acquire(self) -> ownerfile.Handle | None:
        handle = semworker._acquire_start_claim()
        if handle is not None:
            self.handles.append(handle)
        return handle

    def _write(self, raw: bytes, *, age: float = 0.0,
               now: float = 100.0) -> None:
        self.path.write_bytes(raw)
        changed = now - age
        os.utime(self.path, (changed, changed))

    def _wait_path(self, path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5.0
        while not path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if not path.exists():
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"child did not publish {path.name}: {stdout} {stderr}")

    def test_wire_mode_handle_and_exact_release(self) -> None:
        with mock.patch.object(semworker.time, "time", return_value=123.5), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker.secrets, "token_hex", return_value="c" * 32):
            claim = self._acquire()
        self.assertIsInstance(claim, ownerfile.Handle)
        self.assertIsNone(claim.fd)
        expected = (
            f'{{"pid":{os.getpid()},"at":123.5,'
            '"process_start":"birth","nonce":"'
            f'{"c" * 32}"}}'
        ).encode()
        self.assertEqual(claim.snapshot.raw, expected)
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertNotIn(b"\n", expected)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with mock.patch.object(
                claim, "release", wraps=claim.release) as release:
            semworker._release_start_claim(claim)
        release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)
        self.assertFalse(self.path.exists())
        self.assertTrue(claim._released)

        failure = mock.Mock()
        failure.release.side_effect = OSError("sharing")
        semworker._release_start_claim(failure)
        failure.release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)

        with mock.patch.object(semworker.time, "time", return_value=124.5), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker.secrets, "token_hex", return_value="d" * 32):
            second = self._acquire()
        self.assertNotEqual(second.snapshot.raw, expected)
        self.assertIn(b'"at":124.5', second.snapshot.raw)
        self.assertIn(b'"nonce":"' + b"d" * 32 + b'"', second.snapshot.raw)
        semworker._release_start_claim(second)

    def test_valid_claim_policy_is_exact_at_the_six_second_boundary(self) -> None:
        cases = (
            ("exact-boundary", 94.0, True, "birth", False),
            ("exact-stale", 93.999, True, "birth", True),
            ("future-exact", 200.0, True, "birth", True),
            ("dead", 100.0, False, None, True),
            ("reused", 100.0, True, "other-birth", True),
            ("unverifiable", 100.0, True, None, False),
            ("unverifiable-stale", 93.999, True, None, True),
        )
        for label, at, alive, actual_start, acquired in cases:
            with self.subTest(label=label):
                self._write(self._raw(at=at))
                with mock.patch.object(
                        semworker.time, "time", return_value=100.0), \
                        mock.patch.object(common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=lambda pid: (
                                "self-birth" if pid == os.getpid()
                                else actual_start)):
                    claim = self._acquire()
                self.assertEqual(claim is not None, acquired)
                if claim is None:
                    self.assertEqual(self.path.read_bytes(), self._raw(at=at))
                else:
                    semworker._release_start_claim(claim)
                self.path.unlink(missing_ok=True)

    def test_launched_owner_requires_the_parent_claim_generation(self) -> None:
        claim = self._acquire()
        nonce = json.loads(claim.snapshot.raw)["nonce"]
        with mock.patch.object(
                common, "bind_descendants_to_process_lifetime",
                return_value=True):
            owner = semworker._acquire_launched_owner(nonce)
        self.assertIsNotNone(owner)
        self.assertTrue(owner.release(
            tombstone=True, require_stable_mtime=True))

        semworker._release_start_claim(claim)
        replacement = self._acquire()
        with mock.patch.object(
                common, "bind_descendants_to_process_lifetime") as bind, \
                mock.patch.object(
                    semworker, "_acquire_worker_lock") as acquire:
            self.assertIsNone(
                semworker._acquire_launched_owner(nonce))
        bind.assert_not_called()
        acquire.assert_not_called()
        semworker._release_start_claim(replacement)

    def test_claim_replacement_during_owner_acquire_releases_owner(self) -> None:
        claim = self._acquire()
        nonce = json.loads(claim.snapshot.raw)["nonce"]
        replacement = self._raw(
            pid=os.getpid(), at=time.time(),
            process_start=str(common.process_start_identity(os.getpid())),
            nonce="f" * 32)
        acquire = semworker._acquire_worker_lock

        def acquire_then_replace(*, tree_bound):
            owner = acquire(tree_bound=tree_bound)
            self.path.unlink()
            self.path.write_bytes(replacement)
            return owner

        with mock.patch.object(
                common, "bind_descendants_to_process_lifetime",
                return_value=True), \
                mock.patch.object(
                    semworker, "_acquire_worker_lock",
                    side_effect=acquire_then_replace):
            owner = semworker._acquire_launched_owner(nonce)
        self.assertIsNone(owner)
        self.assertFalse(semworker.worker_lock_path().exists())
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_retry_stamps_the_successful_create_attempt(self) -> None:
        self._write(self._raw(pid=999999, at=1.0))
        with mock.patch.object(
                semworker.time, "time",
                side_effect=(10.0, 100.0, 200.0)), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="self"), \
                mock.patch.object(
                    semworker.secrets, "token_hex",
                    side_effect=("b" * 32, "t" * 16, "c" * 32)):
            claim = self._acquire()
        record = json.loads(claim.snapshot.raw)
        self.assertEqual(record["at"], 200.0)
        self.assertEqual(record["nonce"], "c" * 32)
        semworker._release_start_claim(claim)

    def test_malformed_and_nonfinite_records_use_strict_mtime_grace(self) -> None:
        malformed = (
            b"{", b"[]", b"\xff",
            b'{"pid":1,"at":NaN,"process_start":"birth","nonce":"x"}',
            b'{"pid":1,"at":Infinity,"process_start":"birth","nonce":"x"}',
            b'{"pid":1,"at":-Infinity,"process_start":"birth","nonce":"x"}',
            b"[" * 2000 + b"]" * 2000,
        )
        for body in malformed:
            with self.subTest(body=body[:32]):
                self._write(body, age=semworker.START_CLAIM_GRACE_S)
                with mock.patch.object(
                        semworker.time, "time", return_value=100.0):
                    self.assertIsNone(self._acquire())
                self.assertEqual(self.path.read_bytes(), body)
                self._write(
                    body, age=semworker.START_CLAIM_GRACE_S + 0.01)
                with mock.patch.object(
                        semworker.time, "time", return_value=100.0):
                    claim = self._acquire()
                self.assertIsNotNone(claim)
                semworker._release_start_claim(claim)

    def test_reclaim_release_and_pre_spawn_preserve_replacements(self) -> None:
        stale = self._raw(pid=999999, at=1.0)
        replacement = b'{"replacement":true}'
        self._write(stale)
        original_remove = ownerfile.remove_exact
        remove_calls = []

        def replace_before_remove(path, expected, **kwargs):
            remove_calls.append(kwargs)
            path.write_bytes(replacement)
            return original_remove(path, expected, **kwargs)

        with mock.patch.object(
                ownerfile, "remove_exact", side_effect=replace_before_remove):
            self.assertIsNone(self._acquire())
        self.assertEqual(remove_calls, [{
            "tombstone": True, "require_stable_mtime": True}])
        self.assertEqual(self.path.read_bytes(), replacement)
        self.path.unlink()

        self._write(stale)
        with mock.patch.object(
                ownerfile, "remove_exact",
                side_effect=OSError("sharing violation")):
            self.assertIsNone(self._acquire())
        self.assertEqual(self.path.read_bytes(), stale)
        self.path.unlink()

        claim = self._acquire()
        self.path.unlink()
        self.path.write_bytes(replacement)
        semworker._release_start_claim(claim)
        self.assertEqual(self.path.read_bytes(), replacement)
        self.path.unlink()

        claim = self._acquire()
        copied = claim.snapshot.raw
        self.path.unlink()
        self.path.write_bytes(copied)
        semworker._release_start_claim(claim)
        self.assertEqual(self.path.read_bytes(), copied)
        self.path.unlink()

        claim = self._acquire()
        self.path.unlink()
        self.path.write_bytes(replacement)
        with mock.patch.dict(os.environ, {
                "AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=claim), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker())
        spawn.assert_not_called()
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_hostile_claim_leaves_are_silent_conservative_contention(self) -> None:
        self.path.mkdir()
        self.assertIsNone(self._acquire())
        self.assertTrue(self.path.is_dir())
        self.path.rmdir()

        body = self._raw(pid=999999, at=1.0)
        self.path.write_bytes(body)
        info = self.path.lstat()
        reparse = mock.Mock(
            st_mode=info.st_mode, st_size=info.st_size,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with mock.patch.object(Path, "lstat", return_value=reparse):
            self.assertIsNone(self._acquire())
        self.assertEqual(self.path.read_bytes(), body)
        self.path.unlink()

        oversized = b"x" * (64 * 1024 + 1)
        self.path.write_bytes(oversized)
        self.assertIsNone(self._acquire())
        self.assertEqual(self.path.stat().st_size, len(oversized))

    def test_symlink_claim_does_not_touch_its_target(self) -> None:
        victim = self.root / "victim"
        body = self._raw(pid=999999, at=1.0)
        victim.write_bytes(body)
        try:
            self.path.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.assertIsNone(self._acquire())
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(victim.read_bytes(), body)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_fifo_claim_returns_without_opening_the_fifo(self) -> None:
        os.mkfifo(self.path)
        script = """
import pathlib, sys
import common, semworker
common.DATA_DIR = pathlib.Path(sys.argv[1])
raise SystemExit(0 if semworker._acquire_start_claim() is None else 3)
"""
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"claim acquisition blocked on FIFO: {stdout}\n{stderr}")
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))

    def test_real_dead_and_expired_live_launchers_are_reclaimable(self) -> None:
        script = """
import os, pathlib, sys, time
import common, semworker
common.DATA_DIR = pathlib.Path(sys.argv[1])
if sys.argv[3] == "stale":
    semworker.time.time = lambda: 1.0
claim = semworker._acquire_start_claim()
if claim is None:
    raise SystemExit(4)
pathlib.Path(sys.argv[2]).write_text("ready", encoding="ascii")
if sys.argv[3] == "stale":
    time.sleep(30)
os._exit(0)
"""
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        for mode in ("dead", "stale"):
            with self.subTest(mode=mode):
                ready = self.root / f"ready-{mode}"
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(self.root),
                     str(ready), mode],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                try:
                    self._wait_path(ready, process)
                    if mode == "dead":
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
                    else:
                        self.assertIsNone(process.poll())
                        record = json.loads(self.path.read_bytes())
                        actual_start = common.process_start_identity(process.pid)
                        if actual_start is None:
                            self.skipTest("kernel process birth identity unavailable")
                        self.assertEqual(record["pid"], process.pid)
                        self.assertEqual(
                            str(record["process_start"]), str(actual_start))
                    claim = self._acquire()
                    self.assertIsNotNone(claim)
                    semworker._release_start_claim(claim)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                self.assertFalse(self.path.exists())

    def test_peer_wait_returns_descriptor_or_times_out_without_spawning(self) -> None:
        record = {"pid": 7, "process_start": "birth"}
        with mock.patch.dict(os.environ, {
                "AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor",
                    side_effect=(None, (record, "raw"))), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=None), \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertEqual(semworker._ensure_worker(0.05), record)
        spawn.assert_not_called()

        with mock.patch.dict(os.environ, {
                "AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(
                    semworker, "_acquire_start_claim", return_value=None), \
                mock.patch.object(semworker.time, "sleep") as sleep, \
                mock.patch.object(semworker, "_spawn_worker") as spawn:
            self.assertIsNone(semworker._ensure_worker(0.05))
        sleep.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
