"""Cross-language contract for the corpus-generation lock."""

from __future__ import annotations

import base64
import contextlib
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
import index_lock  # noqa: E402
import ownerfile  # noqa: E402
import proc  # noqa: E402


FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures"
    / "index_lock_conformance.json"
)
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ROOT = Path(__file__).resolve().parents[1]
RUST_BIN = (
    ROOT / "target" / "release"
    / ("agrep-rs.exe" if sys.platform == "win32" else "agrep-rs")
)


def _fixture_raw(case: dict) -> bytes:
    repeat = case.get("base64_repeat")
    if repeat is not None:
        byte = base64.b64decode(repeat["base64_byte"], validate=True)
        return byte * int(repeat["count"])
    return base64.b64decode(case["base64"], validate=True)


class IndexLockFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agrep index lock Δ ")
        self.path = Path(self.tmp.name) / ".index.lock"
        self.old_path = index_lock.INDEX_LOCK_PATH
        self.old_common_path = common.INDEX_LOCK_PATH
        index_lock.INDEX_LOCK_PATH = self.path
        common.INDEX_LOCK_PATH = self.path

    def tearDown(self) -> None:
        index_lock.INDEX_LOCK_PATH = self.old_path
        common.INDEX_LOCK_PATH = self.old_common_path
        self.tmp.cleanup()

    def test_facade_reexports_the_real_owner(self) -> None:
        self.assertIs(common.IndexLock, index_lock.IndexLock)
        self.assertIs(common._owner_field, index_lock._owner_field)
        self.assertEqual(common.INDEX_LOCK_PATH, index_lock.INDEX_LOCK_PATH)

    def test_process_liveness_failure_classes_are_conservative(self) -> None:
        self.assertFalse(proc._windows_open_failure_is_alive(87))
        for error in (0, 5, 8, 1450):
            with self.subTest(error=error):
                self.assertTrue(proc._windows_open_failure_is_alive(error))
        for state in ("Z", "X", "x"):
            self.assertTrue(proc._posix_state_is_zombie(state))
        self.assertFalse(proc._posix_state_is_zombie("S"))
        self.assertEqual(
            proc._darwin_cross_uid_identity(0, current_uid=501),
            "darwin_uid_0")
        self.assertIsNone(
            proc._darwin_cross_uid_identity(501, current_uid=501))
        owner = ownerfile.classify_process(
            42, "darwin_1_2", pid_alive=lambda _pid: True,
            process_start=lambda _pid: "darwin_uid_0")
        self.assertIs(owner, ownerfile.ProcessOwner.REUSED)

    def test_fixture_constants_and_canonical_bytes(self) -> None:
        self.assertEqual(FIXTURE["schema"], 1)
        self.assertEqual(index_lock.PROTOCOL, FIXTURE["protocol"])
        self.assertEqual(
            FIXTURE["canonical"]["fields"],
            ["pid", "start", "token", "label", "time"])
        constants = FIXTURE["constants"]
        actual = {
            "timeout_ms": index_lock.TIMEOUT_MS,
            "publication_grace_ms": index_lock.PUBLICATION_GRACE_MS,
            "retry_initial_ms": index_lock.RETRY_INITIAL_MS,
            "retry_factor_numerator": index_lock.RETRY_FACTOR_NUMERATOR,
            "retry_factor_denominator": index_lock.RETRY_FACTOR_DENOMINATOR,
            "retry_max_ms": index_lock.RETRY_MAX_MS,
            "wait_notice_ms": int(index_lock.WAIT_NOTICE_S * 1000),
            "wait_repeat_ms": int(index_lock.WAIT_REPEAT_S * 1000),
            "max_record_bytes": index_lock.MAX_RECORD_BYTES,
            "max_pid": {
                "posix": 0x7FFFFFFF,
                "windows": 0xFFFFFFFF,
            },
        }
        self.assertEqual(actual, constants)
        canonical = FIXTURE["canonical"]
        expected = base64.b64decode(canonical["base64"], validate=True)
        self.assertEqual(expected.decode("ascii"), canonical["record"])
        self.assertEqual(
            index_lock.render_record(
                pid=canonical["pid"], start=canonical["start"],
                token=canonical["token"], label=canonical["label"],
                time_ms=canonical["time_ms"]),
            expected,
        )

    def test_legacy_and_future_records_keep_the_safety_core(self) -> None:
        for record in FIXTURE["legacy_records"]:
            with self.subTest(record=record):
                owner = index_lock.parse_owner(record.encode("utf-8"))
                self.assertIsNotNone(owner)
                self.assertEqual(owner.pid, 12345)

    def test_malformed_fixture_cases_match_platform_policy(self) -> None:
        for case in FIXTURE["raw_cases"]:
            with self.subTest(case=case["name"]):
                raw = _fixture_raw(case)
                expected = case["classification"]
                owner = index_lock.parse_owner(raw)
                if isinstance(expected, dict):
                    state = expected[
                        "windows" if sys.platform == "win32" else "posix"]
                else:
                    state = expected
                if state == "owner":
                    self.assertEqual(owner.pid, expected["pid"])
                    self.assertEqual(owner.start, expected["start"])
                else:
                    self.assertIsNone(owner)

    def test_writer_rejects_noncanonical_fields_before_create(self) -> None:
        canonical = FIXTURE["canonical"]
        base = {
            "pid": canonical["pid"],
            "start": canonical["start"],
            "token": canonical["token"],
            "label": canonical["label"],
            "time_ms": canonical["time_ms"],
        }
        for field, value in (
            ("pid", 0),
            ("pid", True),
            ("pid", 1.5),
            ("pid", index_lock.MAX_PID + 1),
            ("start", "has space"),
            ("start", "has\x01control"),
            ("token", "a" * 31),
            ("token", "G" * 32),
            ("label", "has space"),
            ("label", "x" * 65),
            ("time_ms", -1),
            ("time_ms", 0x10000000000000000),
        ):
            with self.subTest(field=field, value=value):
                values = {**base, field: value}
                with self.assertRaises(ValueError):
                    index_lock.render_record(**values)

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits are not ACLs")
    def test_new_record_is_exact_mode_and_lf_under_umask_zero(self) -> None:
        previous = os.umask(0)
        try:
            with index_lock.IndexLock("fixture", timeout=0.2):
                raw = self.path.read_bytes()
                self.assertTrue(raw.endswith(b"\n"))
                self.assertNotIn(b"\r", raw)
                self.assertEqual(
                    stat.S_IMODE(self.path.stat().st_mode), 0o600)
        finally:
            os.umask(previous)
        self.assertFalse(self.path.exists())

    def test_timeout_rejects_non_finite_and_non_numeric_values(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), True, "1"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "finite nonnegative"):
                index_lock.IndexLock("fixture", timeout=value)
        with self.assertRaisesRegex(ValueError, "finite nonnegative"):
            index_lock.IndexLock("fixture", timeout=-0.1)

    def test_live_pid_only_legacy_record_is_never_stolen_by_age(self) -> None:
        self.path.write_bytes(f"pid={os.getpid()}\n".encode("ascii"))
        old = time.time() - 24 * 3600
        os.utime(self.path, (old, old))
        with self.assertRaises(TimeoutError):
            index_lock.IndexLock("contender", timeout=0.06).__enter__()
        self.assertTrue(self.path.exists())

    def test_dead_and_reused_owners_are_reclaimed(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = proc.pid
        proc.wait()
        self.path.write_bytes(f"pid={dead_pid}\n".encode("ascii"))
        with index_lock.IndexLock("dead", timeout=1):
            self.assertIn(b"label=dead", self.path.read_bytes())
        start = index_lock.process_start_identity(os.getpid())
        if start is None:
            self.skipTest("current process birth identity is unavailable")
        self.path.write_bytes(
            f"pid={os.getpid()} start=definitely-not-{start}\n".encode("ascii"))
        with index_lock.IndexLock("reused", timeout=1):
            self.assertIn(b"label=reused", self.path.read_bytes())

    def test_malformed_grace_and_oversized_fail_closed(self) -> None:
        self.path.write_bytes(b"")
        with self.assertRaises(TimeoutError):
            index_lock.IndexLock("fresh", timeout=0.06).__enter__()
        old = time.time() - 4
        os.utime(self.path, (old, old))
        with index_lock.IndexLock("old", timeout=0.5):
            self.assertIn(b"label=old", self.path.read_bytes())
        self.path.write_bytes(b"")
        future = time.time() + 3600
        os.utime(self.path, (future, future))
        with index_lock.IndexLock("future", timeout=0.5):
            self.assertIn(b"label=future", self.path.read_bytes())
        oversized = next(
            case for case in FIXTURE["raw_cases"]
            if case["name"] == "oversized")
        self.path.write_bytes(_fixture_raw(oversized))
        os.utime(self.path, (old, old))
        with self.assertRaises(TimeoutError):
            index_lock.IndexLock("oversized", timeout=0.06).__enter__()

    def test_wait_notice_names_holder_and_timeout_bound(self) -> None:
        self.path.write_bytes(
            f"pid={os.getpid()} start=unknown label=holder\n".encode("ascii"))
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = (0.0, 0.0, 0.02)
        clock.sleep.return_value = None
        stderr = io.StringIO()
        with mock.patch.object(index_lock, "time", clock), \
                mock.patch.object(index_lock, "WAIT_NOTICE_S", 0.0), \
                mock.patch.object(index_lock, "pid_alive", return_value=True), \
                mock.patch.object(
                    index_lock, "process_start_identity",
                    return_value="fixture-birth"), \
                mock.patch.dict(os.environ, {"AGREP_DEBUG": ""}), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaisesRegex(
                    TimeoutError,
                    "another index task did not finish; retry, or set "
                    "AGREP_DEBUG=1 for details"):
            index_lock.IndexLock("contender", timeout=0.01).__enter__()
        notice = stderr.getvalue()
        self.assertEqual(notice, "another index task is finishing; waiting...\n")
        self.assertNotIn(str(self.path), notice)
        self.assertNotIn(f"pid={os.getpid()}", notice)

    def test_debug_wait_notice_keeps_owner_diagnostics(self) -> None:
        self.path.write_bytes(
            f"pid={os.getpid()} start=unknown label=corpusdb\n".encode("ascii"))
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = (0.0, 0.0, 0.02)
        clock.sleep.return_value = None
        stderr = io.StringIO()
        with mock.patch.object(index_lock, "time", clock), \
                mock.patch.object(index_lock, "WAIT_NOTICE_S", 0.0), \
                mock.patch.object(index_lock, "pid_alive", return_value=True), \
                mock.patch.object(
                    index_lock, "process_start_identity",
                    return_value="fixture-birth"), \
                mock.patch.dict(os.environ, {"AGREP_DEBUG": "1"}), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(TimeoutError) as raised:
            index_lock.IndexLock("contender", timeout=0.01).__enter__()
        notice = stderr.getvalue()
        self.assertIn(str(self.path), notice)
        self.assertIn(f"pid={os.getpid()} label=corpusdb", notice)
        self.assertIn("timeout in", notice)
        self.assertIn(str(self.path), str(raised.exception))
        self.assertIn(f"pid={os.getpid()} label=corpusdb", str(raised.exception))

    def test_corpusdb_wait_notice_names_work_not_owner_internals(self) -> None:
        self.assertEqual(
            index_lock._public_wait_notice(
                "pid=42 label=corpusdb:0123456789abcdefabcd"),
            "search database update is finishing; waiting...")
        self.assertEqual(
            index_lock._public_wait_notice("pid=42 label=agrep-rs"),
            "another transcript index is finishing; waiting...")
        self.assertEqual(
            index_lock._public_timeout_error(
                "pid=42 label=corpusdb:0123456789abcdefabcd"),
            "search database update did not finish; retry, or set "
            "AGREP_DEBUG=1 for details")

    def test_normal_wait_notice_is_emitted_once(self) -> None:
        self.path.write_bytes(
            f"pid={os.getpid()} start=unknown label=holder\n".encode("ascii"))
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = (0.0, 0.0, 31.0, 61.0, 101.0)
        clock.sleep.return_value = None
        stderr = io.StringIO()
        with mock.patch.object(index_lock, "time", clock), \
                mock.patch.object(index_lock, "WAIT_NOTICE_S", 0.0), \
                mock.patch.object(index_lock, "pid_alive", return_value=True), \
                mock.patch.object(
                    index_lock, "process_start_identity",
                    return_value="fixture-birth"), \
                mock.patch.dict(os.environ, {"AGREP_DEBUG": ""}), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(TimeoutError):
            index_lock.IndexLock("contender", timeout=100).__enter__()
        self.assertEqual(
            stderr.getvalue(),
            "another index task is finishing; waiting...\n")

    def test_corpusdb_lock_label_binds_the_exact_writer(self) -> None:
        import corpusdb
        writer = "0123456789abcdefabcd"
        with mock.patch.object(
                corpusdb.indexd_runtime, "derived_writer_build_id",
                return_value=writer) as build_id, \
                mock.patch.object(corpusdb.common, "IndexLock") as lock_type:
            corpusdb._ConnectIndexLock(False)
            build_id.assert_called_once_with(require_binary=True)
            lock_type.assert_called_once_with(f"corpusdb:{writer}")
            lock_type.reset_mock()
            corpusdb._ConnectIndexLock(True)
            lock_type.assert_called_once_with(f"corpusdb:{writer}", timeout=0)

    def test_release_preserves_replacement_and_cleans_transient_failures(self) -> None:
        lock = index_lock.IndexLock("replace", timeout=1)
        lock.__enter__()
        replacement = b"pid=999999 start=unknown future=value\n"
        real_remove = ownerfile.remove_exact

        def replace_then_remove(path, expected, **kwargs):
            path.unlink()
            path.write_bytes(replacement)
            return real_remove(path, expected, **kwargs)

        with mock.patch.object(
                ownerfile, "remove_exact", side_effect=replace_then_remove):
            lock.__exit__(None, None, None)
        self.assertEqual(self.path.read_bytes(), replacement)
        self.path.unlink()
        real_replace = ownerfile.os.replace
        attempts = 0

        def transient(source, target):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("transient sharing failure")
            return real_replace(source, target)

        with mock.patch.object(ownerfile.os, "replace", transient):
            with index_lock.IndexLock("retry", timeout=1):
                pass
        self.assertGreaterEqual(attempts, 3)
        self.assertFalse(self.path.exists())
        self.assertFalse(any(
            ".owner-reap-" in entry.name
            for entry in Path(self.tmp.name).iterdir()))

    def test_release_surfaces_a_stranded_owned_tombstone(self) -> None:
        lock = index_lock.IndexLock("stranded", timeout=1)
        lock.__enter__()
        real_unlink = ownerfile._retry_unlink
        real_restore = ownerfile._restore_tomb

        with (
            mock.patch.object(ownerfile, "_retry_unlink", return_value=False),
            mock.patch.object(ownerfile, "_restore_tomb", return_value=None),
        ):
            with self.assertRaisesRegex(
                    OSError, "could not release owned index lock"):
                lock.__exit__(None, None, None)
        tomb = next(
            entry for entry in Path(self.tmp.name).iterdir()
            if ".owner-reap-" in entry.name)
        real_restore(self.path, tomb)
        if tomb.exists():
            self.assertTrue(real_unlink(tomb))
        if self.path.exists():
            self.path.unlink()

    def test_release_surfaces_unverifiable_cleanup(self) -> None:
        lock = index_lock.IndexLock("unverifiable", timeout=1)
        lock.__enter__()
        handle = lock._handle
        with (
            mock.patch.object(handle, "release", return_value=False),
            mock.patch.object(
                Path, "iterdir", side_effect=PermissionError("denied")),
        ):
            with self.assertRaisesRegex(
                    OSError, "could not verify index lock release"):
                lock.__exit__(None, None, None)
        self.assertTrue(handle.release())

    def test_partial_publication_failure_exactly_cleans_its_claim(self) -> None:
        raw = b"pid=1 start=unknown token=" + b"a" * 32 + b" label=x time=1.000\n"
        with mock.patch.object(
                ownerfile.os, "write",
                side_effect=OSError("fixture short write")):
            with self.assertRaises(OSError):
                ownerfile.create_exclusive(
                    self.path, raw, retain_fd=True, exact_mode=True)
        self.assertFalse(self.path.exists())

    @unittest.skipIf(sys.platform == "win32", "fork zombies are POSIX-only")
    def test_real_unreaped_zombie_is_reclaimable(self) -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 2
            while index_lock.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(index_lock.pid_alive(pid))
            self.path.write_bytes(f"pid={pid}\n".encode("ascii"))
            with index_lock.IndexLock("zombie", timeout=1):
                self.assertIn(b"label=zombie", self.path.read_bytes())
        finally:
            os.waitpid(pid, 0)

    def test_symlink_claim_is_protected(self) -> None:
        target = Path(self.tmp.name) / "target"
        target.write_bytes(b"do-not-touch")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(TimeoutError):
            index_lock.IndexLock("symlink", timeout=0.06).__enter__()
        self.assertEqual(target.read_bytes(), b"do-not-touch")


class IndexLockCrossLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not RUST_BIN.is_file():
            raise AssertionError(
                f"build the real release binary before this gate: {RUST_BIN}")

    def _start_rust_holder(
            self, path: Path, *, label: str = "rust-fixture"
    ) -> subprocess.Popen:
        process = subprocess.Popen(
            [
                str(RUST_BIN), "index-lock-contract", "hold",
                "--path", str(path), "--label", label, "--timeout-ms", "2000",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read().decode("utf-8", "replace")
                self.fail(f"Rust holder exited before acquisition: {stderr}")
            try:
                observed = ownerfile.snapshot(
                    path, max_bytes=index_lock.MAX_RECORD_BYTES)
            except OSError:
                time.sleep(0.01)
                continue
            owner = index_lock.parse_owner(observed.raw)
            if owner is not None and owner.pid == process.pid:
                break
            time.sleep(0.01)
        else:
            process.kill()
            process.wait()
            self.fail("Rust holder did not publish its claim")
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready, {"ready": True, "pid": process.pid})
        return process

    def _finish_rust_holder(
            self, process: subprocess.Popen, *, expected: int = 0) -> None:
        process.stdin.close()
        returncode = process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", "replace")
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(returncode, expected, stderr)

    def _kill_holder(self, process: subprocess.Popen) -> None:
        process.kill()
        process.wait(timeout=5)
        with contextlib.suppress(OSError):
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()

    def _start_python_holder(
            self, path: Path, *, label: str = "python-child"
    ) -> subprocess.Popen:
        script = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "import index_lock\n"
            "index_lock.INDEX_LOCK_PATH=Path(sys.argv[1])\n"
            "lock=index_lock.IndexLock(sys.argv[2],timeout=2)\n"
            "lock.__enter__()\n"
            "print(json.dumps({'ready':True,'pid':__import__('os').getpid()}),"
            "flush=True)\n"
            "sys.stdin.buffer.read()\n"
            "lock.__exit__(None,None,None)\n"
        )
        environment = os.environ.copy()
        python_path = str(ROOT / "py")
        environment["PYTHONPATH"] = (
            python_path + os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH") else python_path)
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(path), label],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read().decode("utf-8", "replace")
                self.fail(f"Python holder exited before acquisition: {stderr}")
            try:
                owner = index_lock.parse_owner(path.read_bytes())
            except OSError:
                owner = None
            if owner is not None and owner.pid == process.pid:
                break
            time.sleep(0.01)
        else:
            process.kill()
            process.wait()
            self.fail("Python holder did not publish its claim")
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready, {"ready": True, "pid": process.pid})
        return process

    def test_rust_describe_and_render_match_static_fixture(self) -> None:
        described = subprocess.run(
            [str(RUST_BIN), "index-lock-contract", "describe"],
            check=True, capture_output=True)
        payload = json.loads(described.stdout)
        self.assertEqual(payload["schema"], FIXTURE["schema"])
        self.assertEqual(payload["protocol"], FIXTURE["protocol"])
        self.assertEqual(
            payload["canonical_fields"], FIXTURE["canonical"]["fields"])
        self.assertEqual(payload["constants"], FIXTURE["constants"])
        canonical = FIXTURE["canonical"]
        rendered = subprocess.run(
            [
                str(RUST_BIN), "index-lock-contract", "render",
                "--pid", str(canonical["pid"]),
                "--start", canonical["start"],
                "--token", canonical["token"],
                "--label", canonical["label"],
                "--time-ms", str(canonical["time_ms"]),
            ],
            check=True, capture_output=True)
        self.assertEqual(
            rendered.stdout,
            base64.b64decode(canonical["base64"], validate=True))

    def test_live_rust_owner_excludes_python_then_releases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep rust owner Δ ") as raw:
            path = Path(raw) / ".index.lock"
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", path):
                process = self._start_rust_holder(path)
                try:
                    with self.assertRaises(TimeoutError):
                        index_lock.IndexLock(
                            "python-contender", timeout=0).__enter__()
                    self.assertEqual(
                        index_lock._lock_holder_pid(path), process.pid)
                finally:
                    self._finish_rust_holder(process)
            self.assertFalse(path.exists())

    def test_live_python_owner_excludes_rust_then_rust_acquires(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep python owner Δ ") as raw:
            path = Path(raw) / ".index.lock"
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", path):
                with index_lock.IndexLock("python-holder", timeout=1):
                    original = path.read_bytes()
                    blocked = subprocess.run(
                        [
                            str(RUST_BIN), "index-lock-contract", "hold",
                            "--path", str(path), "--label", "rust-contender",
                            "--timeout-ms", "0",
                        ],
                        input=b"", capture_output=True)
                    self.assertNotEqual(blocked.returncode, 0)
                    self.assertEqual(path.read_bytes(), original)
                acquired = subprocess.run(
                    [
                        str(RUST_BIN), "index-lock-contract", "hold",
                        "--path", str(path), "--label", "rust-after",
                        "--timeout-ms", "1000",
                    ],
                    input=b"", check=True, capture_output=True)
                ready = json.loads(acquired.stdout)
                self.assertTrue(ready["ready"])
            self.assertFalse(path.exists())

    def test_python_reclaims_a_dead_rust_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep dead rust Δ ") as raw:
            path = Path(raw) / ".index.lock"
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", path):
                process = self._start_rust_holder(path, label="dead-rust")
                self._kill_holder(process)
                with index_lock.IndexLock("python-after", timeout=1):
                    self.assertIn(b"label=python-after", path.read_bytes())
            self.assertFalse(path.exists())

    def test_rust_reclaims_dead_and_reused_python_owners(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep dead python Δ ") as raw:
            path = Path(raw) / ".index.lock"
            process = self._start_python_holder(path)
            self._kill_holder(process)
            acquired = subprocess.run(
                [
                    str(RUST_BIN), "index-lock-contract", "hold",
                    "--path", str(path), "--label", "rust-after-dead",
                    "--timeout-ms", "1000",
                ],
                input=b"", check=True, capture_output=True)
            self.assertTrue(json.loads(acquired.stdout)["ready"])
            self.assertFalse(path.exists())

            path.write_bytes(
                f"pid={os.getpid()} start=not-the-current-birth\n"
                .encode("ascii"))
            acquired = subprocess.run(
                [
                    str(RUST_BIN), "index-lock-contract", "hold",
                    "--path", str(path), "--label", "rust-after-reuse",
                    "--timeout-ms", "1000",
                ],
                input=b"", check=True, capture_output=True)
            self.assertTrue(json.loads(acquired.stdout)["ready"])
            self.assertFalse(path.exists())

    def test_rust_obeys_malformed_publication_grace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep grace Δ ") as raw:
            path = Path(raw) / ".index.lock"
            path.write_bytes(b"")
            blocked = subprocess.run(
                [
                    str(RUST_BIN), "index-lock-contract", "hold",
                    "--path", str(path), "--label", "fresh-malformed",
                    "--timeout-ms", "0",
                ],
                input=b"", capture_output=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertTrue(path.exists())
            old = time.time() - 4
            os.utime(path, (old, old))
            acquired = subprocess.run(
                [
                    str(RUST_BIN), "index-lock-contract", "hold",
                    "--path", str(path), "--label", "old-malformed",
                    "--timeout-ms", "1000",
                ],
                input=b"", check=True, capture_output=True)
            self.assertTrue(json.loads(acquired.stdout)["ready"])
            self.assertFalse(path.exists())
            path.write_bytes(b"")
            future = time.time() + 3600
            os.utime(path, (future, future))
            acquired = subprocess.run(
                [
                    str(RUST_BIN), "index-lock-contract", "hold",
                    "--path", str(path), "--label", "future-malformed",
                    "--timeout-ms", "1000",
                ],
                input=b"", check=True, capture_output=True)
            self.assertTrue(json.loads(acquired.stdout)["ready"])
            self.assertFalse(path.exists())

    def test_rust_wait_notice_is_terse_unless_debugging(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep rust wait Δ ") as raw:
            path = Path(raw) / ".index.lock"
            process = self._start_python_holder(path, label="corpusdb")
            try:
                blocked = subprocess.run(
                    [
                        str(RUST_BIN), "index-lock-contract", "hold",
                        "--path", str(path), "--label", "rust-contender",
                        "--timeout-ms", "1700",
                    ],
                    input=b"", capture_output=True,
                    env={**os.environ, "AGREP_DEBUG": ""})
                debug = subprocess.run(
                    [
                        str(RUST_BIN), "index-lock-contract", "hold",
                        "--path", str(path), "--label", "rust-contender",
                        "--timeout-ms", "1700",
                    ],
                    input=b"", capture_output=True,
                    env={**os.environ, "AGREP_DEBUG": "1"})
            finally:
                self._finish_rust_holder(process)
        self.assertNotEqual(blocked.returncode, 0)
        stderr = blocked.stderr.decode("utf-8", "replace")
        notice = stderr.splitlines()[0]
        self.assertEqual(
            notice, "search database update is finishing; waiting...")
        self.assertNotIn(str(path), notice)
        self.assertNotIn(f"pid={process.pid}", notice)
        self.assertIn(
            "search database update did not finish; retry, or set "
            "AGREP_DEBUG=1 for details",
            stderr)
        self.assertNotIn(str(path), stderr)
        self.assertNotIn(f"pid={process.pid}", stderr)
        debug_stderr = debug.stderr.decode("utf-8", "replace")
        self.assertIn(f"pid={process.pid} label=corpusdb", debug_stderr)
        self.assertIn("timeout in", debug_stderr)

    def test_unicode_owner_syntax_is_malformed_in_both_runtimes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep unicode owner Δ ") as raw:
            path = Path(raw) / ".index.lock"
            for name in ("unicode-nbsp-delimiter", "unicode-decimal-pid"):
                case = next(
                    item for item in FIXTURE["raw_cases"]
                    if item["name"] == name)
                malformed = _fixture_raw(case)
                with self.subTest(name=name):
                    self.assertIsNone(index_lock.parse_owner(malformed))
                    path.write_bytes(malformed)
                    blocked = subprocess.run(
                        [
                            str(RUST_BIN), "index-lock-contract", "hold",
                            "--path", str(path), "--label", "unicode",
                            "--timeout-ms", "0",
                        ],
                        input=b"", capture_output=True)
                    self.assertNotEqual(blocked.returncode, 0)
                    self.assertEqual(path.read_bytes(), malformed)
                    path.unlink()

    def test_rust_release_preserves_a_replacement_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep rust replace Δ ") as raw:
            path = Path(raw) / ".index.lock"
            process = self._start_rust_holder(path, label="replace-rust")
            replacement = b"pid=999999 start=unknown future=value\n"
            path.unlink()
            path.write_bytes(replacement)
            self._finish_rust_holder(process)
            self.assertEqual(path.read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
