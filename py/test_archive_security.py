"""Adversarial archive path, framing, and ownership regressions."""

from __future__ import annotations

import copy
from contextlib import redirect_stdout
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir, without_store_override

isolate_data_dir()
import archive  # noqa: E402


class ArchiveSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.saved = {name: getattr(archive, name) for name in (
            "HOME", "ARCHIVE_DIR", "MANIFEST", "STORE", "CONFIG", "HEALTH", "ROOTS",
            "_SQLITE_SETTLE_S")}
        archive.HOME = self.home
        archive.ARCHIVE_DIR = self.base / "archive"
        archive.MANIFEST = archive.ARCHIVE_DIR / "manifest.jsonl"
        archive.STORE = archive.ARCHIVE_DIR / "store"
        archive.CONFIG = archive.ARCHIVE_DIR / "config.json"
        archive.HEALTH = archive.ARCHIVE_DIR / "capture-health.json"
        archive.ROOTS = [("codex", ".codex/sessions/*.jsonl", False)]
        archive._SQLITE_SETTLE_S = 0
        self.env = mock.patch.dict(os.environ, {
            "APPDATA": "", "CRUSH_GLOBAL_DATA": "", "XDG_DATA_HOME": "", "LOCALAPPDATA": "",
            "OPENCODE_DB": ""}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        for name, value in self.saved.items():
            setattr(archive, name, value)
        self.temp.cleanup()

    def _capture_codex(self) -> tuple[Path, bytes]:
        source = self.home / ".codex" / "sessions" / "rollout-archive-safe.jsonl"
        source.parent.mkdir(parents=True)
        data = b'{"type":"event_msg","text":"safe"}\n'
        source.write_bytes(data)
        self.assertEqual(archive.capture()["full"], 1)
        return source, data

    def _restore(self, *args, **kwargs) -> int:
        with redirect_stdout(io.StringIO()):
            return archive.restore(*args, **kwargs)

    def _lock_path(self) -> Path:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        return archive.ARCHIVE_DIR / "lock"

    def _lock_tombs(self) -> list[Path]:
        return list(archive.ARCHIVE_DIR.glob(".lock.owner-reap-*"))

    def test_force_restore_refuses_symlink_target_and_parent(self) -> None:
        source, _ = self._capture_codex()
        source.unlink()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"outside")
        try:
            source.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self._restore("archive-safe", force=True), 1)
        self.assertEqual(outside.read_bytes(), b"outside")
        source.unlink()
        source.parent.rmdir()
        escaped = self.base / "escaped"
        escaped.mkdir()
        source.parent.symlink_to(escaped, target_is_directory=True)
        self.assertEqual(self._restore("archive-safe", force=True), 1)
        self.assertFalse((escaped / source.name).exists())

    def test_restore_refuses_parent_identity_swap_before_publish(self) -> None:
        source, _ = self._capture_codex()
        source.unlink()
        parent = source.parent
        parked = parent.with_name("sessions-old")
        real_ensure = archive._ensure_plain_parent
        calls = 0

        def swapping(root: Path, candidate: Path):
            nonlocal calls
            identity = real_ensure(root, candidate)
            calls += 1
            if calls == 1:
                parent.rename(parked)
                parent.mkdir()
            return identity

        with mock.patch.object(archive, "_ensure_plain_parent", side_effect=swapping):
            self.assertEqual(self._restore("archive-safe", force=True), 1)
        self.assertFalse(source.exists())
        self.assertFalse((parked / source.name).exists())

    def test_outside_manifest_record_is_ignored_for_unrelated_restore(self) -> None:
        source, _ = self._capture_codex()
        record = archive._records()[0]
        outside = copy.deepcopy(record)
        outside["path"] = str(self.base / "outside.jsonl")
        archive.MANIFEST.write_text(
            json.dumps(outside) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(archive._records(), [])
        self.assertEqual(self._restore(source.name), 1)

    def test_matching_oversized_manifest_record_blocks_restore(self) -> None:
        source, _ = self._capture_codex()
        oversized = copy.deepcopy(archive._records()[0])
        oversized["size"] = archive._MAX_ARCHIVE_BYTES + 1
        oversized["chunks"][0]["len"] = archive._MAX_ARCHIVE_BYTES + 1
        archive.MANIFEST.write_text(
            json.dumps(oversized) + "\n",
            encoding="utf-8")
        self.assertEqual(archive._records(), [])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = archive.restore_main([source.name])
        self.assertEqual(rc, 2)
        self.assertIn("cannot prove the requested archive version", stdout.getvalue())

    def test_chunk_framing_and_symlinks_fail_before_publication(self) -> None:
        source, _ = self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        chunk.write_bytes(chunk.read_bytes() + b"trailing")
        source.unlink()
        output = self.base / "out"
        self.assertEqual(self._restore("archive-safe", to=str(output)), 1)
        self.assertFalse((output / source.name).exists())
        outside = self.base / "packed.xz"
        outside.write_bytes(chunk.read_bytes())
        chunk.unlink()
        try:
            chunk.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self._restore("archive-safe", to=str(output)), 1)
        self.assertFalse((output / source.name).exists())

    def test_append_capture_and_atomic_restore_round_trip(self) -> None:
        source, first = self._capture_codex()
        second = first + b'{"type":"event_msg","text":"appended"}\n'
        source.write_bytes(second)
        self.assertEqual(archive.capture()["appended"], 1)
        output = self.base / "out"
        self.assertEqual(self._restore("archive-safe", to=str(output)), 0)
        self.assertEqual((output / source.name).read_bytes(), second)

    def _capture_same_basename_pair(self) -> tuple[Path, Path]:
        archive.ROOTS = [("codex", ".codex/sessions/*.jsonl", False),
                         ("codex", ".codex/spare/*.jsonl", False)]
        first = self.home / ".codex" / "sessions" / "context.jsonl"
        second = self.home / ".codex" / "spare" / "context.jsonl"
        for path, text in ((first, "one"), (second, "two")):
            path.parent.mkdir(parents=True)
            path.write_bytes(
                b'{"type":"event_msg","text":"%s"}\n' % text.encode("ascii"))
        self.assertEqual(archive.capture()["full"], 2)
        return first, second

    def test_restore_to_refuses_colliding_basenames_before_writing(self) -> None:
        first, second = self._capture_same_basename_pair()
        output = self.base / "out"
        for force in (True, False):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = archive.restore(
                    "context.jsonl", to=str(output), force=force)
            self.assertEqual(rc, 1)
            self.assertFalse(output.exists(), "collision must precede writes")
            for path in (first, second):
                self.assertIn(str(path), stdout.getvalue())
            self.assertIn("would both restore", stdout.getvalue())

    def test_narrowed_needle_still_restores_one_of_the_twins(self) -> None:
        first, second = self._capture_same_basename_pair()
        output = self.base / "out"
        self.assertEqual(
            self._restore("spare/context.jsonl", to=str(output)), 0)
        self.assertEqual((output / "context.jsonl").read_bytes(),
                         second.read_bytes())
        self.assertNotEqual(second.read_bytes(), first.read_bytes())

    def test_restore_path_needles_are_separator_portable(self) -> None:
        windows = {
            "path": r"C:\synthetic-root\.codex\spare\context.jsonl",
            "sessions": [],
        }
        self.assertTrue(archive._matches_restore_needle(
            windows, "spare/context.jsonl"))
        posix = {
            "path": "/synthetic-root/.codex/spare/context.jsonl",
            "sessions": [],
        }
        self.assertTrue(archive._matches_restore_needle(
            posix, r"spare\context.jsonl"))
        sessions = {"path": "/other/file", "sessions": ["abcdef123456"]}
        self.assertTrue(archive._matches_restore_needle(sessions, "abcdef12"))

    def test_torn_manifest_tail_is_repaired_before_next_capture(self) -> None:
        source, first = self._capture_codex()
        second = first + b'{"type":"event_msg","text":"after-crash"}\n'
        source.write_bytes(second)
        with archive.MANIFEST.open("ab") as stream:
            stream.write(b'{"path":')

        result = archive.capture()

        self.assertEqual(result["appended"], 1)
        self.assertEqual(result["failed"], 0)
        expected = hashlib.sha256(second).hexdigest()
        latest = archive._records()[-1]
        self.assertEqual(latest["sha256"], expected)
        self.assertTrue(all(
            archive._chunk_path(chunk["sha256"]).is_file() for chunk in latest["chunks"]))
        source.unlink()
        output = self.base / "out"
        self.assertEqual(self._restore("archive-safe", to=str(output)), 0)
        self.assertEqual((output / source.name).read_bytes(), second)

    def test_manifest_append_fsyncs_successful_record(self) -> None:
        self._capture_codex()
        record = archive._records()[0]
        with mock.patch.object(archive.os, "fsync", wraps=os.fsync) as sync:
            archive._append_manifest(record)
        sync.assert_called()

    def test_restore_refuses_when_manifest_tail_cannot_prove_latest_version(self) -> None:
        source, _ = self._capture_codex()
        with archive.MANIFEST.open("ab") as stream:
            stream.write(b'{"path":')
        source.unlink()
        output = self.base / "out"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = archive.restore_main(["archive-safe", "--to", str(output)])

        self.assertEqual(rc, 2)
        self.assertIn("cannot prove the requested archive version", stdout.getvalue())
        self.assertFalse((output / source.name).exists())

    def test_restore_refuses_newer_unrecognized_version(self) -> None:
        source, first = self._capture_codex()
        source.write_bytes(first + b'{"type":"event_msg","text":"future"}\n')
        self.assertEqual(archive.capture()["appended"], 1)
        records = archive._records()
        future = {**records[-1], "future_schema_field": {"version": 2}}
        archive.MANIFEST.write_text(
            json.dumps(records[0]) + "\n" + json.dumps(future) + "\n",
            encoding="utf-8")
        source.unlink()
        output = self.base / "out"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = archive.restore_main(["archive-safe", "--to", str(output)])

        self.assertEqual(rc, 2)
        self.assertIn("cannot prove the requested archive version", stdout.getvalue())
        self.assertFalse((output / source.name).exists())

    def test_restore_refuses_when_only_matching_record_has_future_schema(self) -> None:
        source, _ = self._capture_codex()
        future = {
            **archive._records()[0],
            "future_schema_field": {"version": 2},
        }
        archive.MANIFEST.write_text(
            json.dumps(future) + "\n",
            encoding="utf-8",
        )
        source.unlink()
        output = self.base / "out"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = archive.restore_main(["archive-safe", "--to", str(output)])

        self.assertEqual(rc, 2)
        self.assertIn("cannot prove the requested archive version", stdout.getvalue())
        self.assertFalse(output.exists())

    def test_restore_refuses_cross_path_future_record_matching_needle(self) -> None:
        sessions = self.home / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        primary = sessions / "rollout-primary.jsonl"
        other = sessions / "rollout-other.jsonl"
        primary.write_bytes(b'{"type":"event_msg","text":"primary"}\n')
        other.write_bytes(b'{"type":"event_msg","text":"other"}\n')
        self.assertEqual(archive.capture()["full"], 2)
        records = {Path(rec["path"]).name: rec for rec in archive._records()}
        valid = records[primary.name]
        future = {**records[other.name], "future_schema_field": {"version": 2}}
        valid["sessions"] = ["shared-session"]
        future["sessions"] = ["shared-session"]
        primary.unlink()
        other.unlink()

        for needle in ("rollout-", "shared-se"):
            with self.subTest(needle=needle):
                archive.MANIFEST.write_text(
                    json.dumps(valid) + "\n" + json.dumps(future) + "\n",
                    encoding="utf-8")
                output = self.base / f"out-{needle}"
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    rc = archive.restore_main([needle, "--to", str(output)])
                self.assertEqual(rc, 2)
                self.assertIn(
                    "cannot prove the requested archive version", stdout.getvalue())
                self.assertFalse(output.exists())

    def test_restore_names_the_verified_version_hash(self) -> None:
        source, first = self._capture_codex()
        second = first + b'{"type":"event_msg","text":"newest"}\n'
        source.write_bytes(second)
        self.assertEqual(archive.capture()["appended"], 1)
        expected = hashlib.sha256(second).hexdigest()
        source.unlink()
        output = self.base / "out"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = archive.restore("archive-safe", to=str(output))

        self.assertEqual(rc, 0)
        self.assertIn(f"sha256 verified: {expected}", stdout.getvalue())
        self.assertNotIn(hashlib.sha256(first).hexdigest(), stdout.getvalue())

    def test_lock_wire_bytes_retained_fd_and_idempotent_release(self) -> None:
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        lock = archive.ARCHIVE_DIR / "lock"
        raw = lock.read_bytes()
        matched = re.fullmatch(
            rb"pid=([0-9]+) start=([^ \n]+) token=([0-9a-f]{32})\n", raw)
        self.assertIsNotNone(matched)
        self.assertEqual(int(matched.group(1)), os.getpid())
        expected = str(
            archive.common.process_start_identity(os.getpid()) or "unknown").encode()
        self.assertEqual(matched.group(2), expected)
        self.assertEqual(owner.snapshot.raw, raw)
        fd = owner.fd
        self.assertIsNotNone(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        self.assertEqual(os.read(fd, len(raw)), raw)
        first_token = matched.group(3)

        archive._unlock(owner)
        archive._unlock(owner)
        self.assertFalse(lock.exists())
        self.assertEqual(self._lock_tombs(), [])
        with self.assertRaises(OSError):
            os.fstat(fd)

        second = archive._try_lock()
        self.assertIsNotNone(second)
        second_raw = lock.read_bytes()
        second_match = re.fullmatch(
            rb"pid=([0-9]+) start=([^ \n]+) token=([0-9a-f]{32})\n",
            second_raw)
        self.assertIsNotNone(second_match)
        self.assertNotEqual(second_match.group(3), first_token)
        archive._unlock(second)

    def test_late_release_preserves_same_and_different_body_aba(self) -> None:
        lock = self._lock_path()
        for index, same_body in enumerate((True, False)):
            owner = archive._try_lock()
            self.assertIsNotNone(owner)
            replacement = owner.snapshot.raw if same_body else (
                b"pid=999 start=new token=" + b"a" * 32 + b"\n")
            newer = lock.with_name(f"lock-new-{index}")
            newer.write_bytes(replacement)
            try:
                os.replace(newer, lock)
            except PermissionError:
                if os.name != "nt":
                    raise
                archive._unlock(owner)
                self.assertFalse(lock.exists())
                self.assertEqual(newer.read_bytes(), replacement)
                newer.unlink()
                continue
            archive._unlock(owner)
            self.assertEqual(lock.read_bytes(), replacement)
            self.assertEqual(self._lock_tombs(), [])
            lock.unlink()

    def test_reclaim_preserves_same_and_different_body_aba(self) -> None:
        lock = self._lock_path()
        original_remove = archive.ownerfile.remove_exact
        for index, same_body in enumerate((True, False)):
            stale = b"pid=424242 start=old token=" + b"a" * 32 + b"\n"
            lock.write_bytes(stale)
            replacement = stale if same_body else (
                b"pid=434343 start=new token=" + b"b" * 32 + b"\n")

            def replace_before_remove(path, observed, **kwargs):
                newer = lock.with_name(f"lock-race-{index}")
                newer.write_bytes(replacement)
                os.replace(newer, lock)
                return original_remove(path, observed, **kwargs)

            with mock.patch.object(
                    archive.common, "pid_alive", return_value=False), \
                    mock.patch.object(
                        archive.ownerfile, "remove_exact",
                        side_effect=replace_before_remove) as remove:
                self.assertIsNone(archive._try_lock())
            self.assertEqual(lock.read_bytes(), replacement)
            self.assertTrue(remove.call_args.kwargs["tombstone"])
            self.assertTrue(remove.call_args.kwargs["require_stable_mtime"])
            self.assertEqual(self._lock_tombs(), [])
            lock.unlink()

    @unittest.skipIf(os.name == "nt", "Windows denies replacing a retained lock")
    def test_post_create_aba_propagates_and_preserves_replacement(self) -> None:
        lock = self._lock_path()
        original_snapshot = archive.ownerfile.snapshot
        for index, same_body in enumerate((True, False)):
            fired = False
            expected = None

            def replace_before_verify(path, **kwargs):
                nonlocal expected, fired
                if path == lock and not fired:
                    fired = True
                    created = lock.read_bytes()
                    replacement = created if same_body else (
                        b"pid=999 start=new token=" + b"c" * 32 + b"\n")
                    expected = replacement
                    newer = lock.with_name(f"lock-create-race-{index}")
                    newer.write_bytes(replacement)
                    os.replace(newer, lock)
                return original_snapshot(path, **kwargs)

            with mock.patch.object(
                    archive.ownerfile, "snapshot",
                    side_effect=replace_before_verify):
                with self.assertRaises(archive.ownerfile.OwnershipLost):
                    archive._try_lock()
            self.assertTrue(fired)
            self.assertIsNotNone(expected)
            self.assertEqual(lock.read_bytes(), expected)
            lock.unlink()

    def test_lock_owner_classification_and_two_attempt_reclaim(self) -> None:
        lock = self._lock_path()
        exact = b"pid=424242 start=birth token=" + b"a" * 32 + b"\n"
        live_cases = (
            ("birth", exact),
            ("other", b"pid=424242 start=unknown token=" + b"a" * 32 + b"\n"),
            ("birth", b"pid=424242 token=" + b"a" * 32 + b"\n"),
            (None, exact),
        )
        for actual_start, raw in live_cases:
            lock.write_bytes(raw)
            with mock.patch.object(archive.common, "pid_alive", return_value=True), \
                    mock.patch.object(
                        archive.common, "process_start_identity",
                        return_value=actual_start):
                self.assertIsNone(archive._try_lock())
            self.assertEqual(lock.read_bytes(), raw)
            lock.unlink()

        lock.write_bytes(exact)
        with mock.patch.object(
                archive.common, "pid_alive",
                side_effect=OSError("process query denied")):
            self.assertIsNone(archive._try_lock())
        self.assertEqual(lock.read_bytes(), exact)
        lock.unlink()

        reclaim_cases = (
            (False, "birth", exact),
            (True, "new-birth", exact),
            (True, "birth", b"pid=424242 start= token=" + b"a" * 32 + b"\n"),
            (True, "birth", b"pid=424242 start=None token=" + b"a" * 32 + b"\n"),
        )
        for alive, actual_start, raw in reclaim_cases:
            lock.write_bytes(raw)
            with mock.patch.object(
                    archive.ownerfile, "create_exclusive",
                    wraps=archive.ownerfile.create_exclusive) as create, \
                    mock.patch.object(
                        archive.common, "pid_alive", return_value=alive), \
                    mock.patch.object(
                        archive.common, "process_start_identity",
                        return_value=actual_start):
                owner = archive._try_lock()
            self.assertIsNotNone(owner)
            self.assertEqual(create.call_count, 2)
            archive._unlock(owner)
            self.assertFalse(lock.exists())
            self.assertEqual(self._lock_tombs(), [])

    def test_pidless_and_malformed_lock_use_exact_three_second_grace(self) -> None:
        lock = self._lock_path()
        records = (
            b"start=unknown token=" + b"a" * 32 + b"\n",
            b"pid=424242 start=birth token=" + b"a" * 32 + b" broken\n",
            b"\xffnot-an-owner",
        )
        for raw in records:
            lock.write_bytes(raw)
            mtime = lock.stat().st_mtime
            with mock.patch.object(archive.time, "time", return_value=mtime + 3.0):
                self.assertIsNone(archive._try_lock())
            self.assertEqual(lock.read_bytes(), raw)
            with mock.patch.object(
                    archive.time, "time", return_value=mtime + 3.000001):
                owner = archive._try_lock()
            self.assertIsNotNone(owner)
            archive._unlock(owner)
            self.assertFalse(lock.exists())

    def test_future_malformed_lock_does_not_extend_publication_grace(self) -> None:
        lock = self._lock_path()
        lock.write_bytes(b"{")
        future = time.time() + 3600
        os.utime(lock, (future, future))
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        archive._unlock(owner)
        self.assertFalse(lock.exists())

    def test_oversized_and_nonregular_lock_are_busy(self) -> None:
        lock = self._lock_path()
        oversized = b"x" * (64 * 1024 + 1)
        lock.write_bytes(oversized)
        self.assertIsNone(archive._try_lock())
        self.assertEqual(lock.read_bytes(), oversized)
        lock.unlink()

        lock.mkdir()
        self.assertIsNone(archive._try_lock())
        self.assertTrue(lock.is_dir())
        lock.rmdir()

    def test_lock_leaf_symlink_is_busy(self) -> None:
        lock = self._lock_path()
        outside = self.base / "outside-lock"
        outside.write_bytes(b"outside")
        try:
            lock.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertIsNone(archive._try_lock())
        self.assertTrue(lock.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_lock_parent_symlink_is_rejected(self) -> None:
        outside = self.base / "outside-archive"
        outside.mkdir()
        try:
            archive.ARCHIVE_DIR.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "unsafe restore parent"):
            archive._try_lock()
        self.assertFalse((outside / "lock").exists())

    def test_unlock_uses_acquired_path_and_requires_stable_mtime(self) -> None:
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        lock = archive.ARCHIVE_DIR / "lock"
        raw = lock.read_bytes()
        changed = owner.snapshot.identity[3] + 1_000_000_000
        os.utime(lock, ns=(changed, changed))
        archive._unlock(owner)
        self.assertEqual(lock.read_bytes(), raw)
        lock.unlink()

        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        original = archive.ARCHIVE_DIR
        archive.ARCHIVE_DIR = self.base / "other-archive"
        try:
            archive._unlock(owner)
        finally:
            archive.ARCHIVE_DIR = original
        self.assertFalse((original / "lock").exists())
        self.assertFalse((self.base / "other-archive" / "lock").exists())

    def test_non_file_exists_create_errors_propagate(self) -> None:
        self._lock_path()
        with mock.patch.object(
                archive.ownerfile, "create_exclusive",
                side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                archive._try_lock()

    def test_transient_post_create_reopen_does_not_strand_owner(self) -> None:
        with mock.patch.object(
                archive.ownerfile, "snapshot",
                side_effect=PermissionError(13, "sharing violation")):
            owner = archive._try_lock()
        self.assertIsNotNone(owner)
        archive._unlock(owner)
        self.assertFalse((archive.ARCHIVE_DIR / "lock").exists())

    def test_crashed_holder_is_reclaimed_without_grace(self) -> None:
        ready = self.base / "archive-lock-ready"
        py_dir = Path(archive.__file__).resolve().parent
        script = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import archive
archive.ARCHIVE_DIR = pathlib.Path(sys.argv[2])
owner = archive._try_lock()
if owner is None:
    raise SystemExit(2)
pathlib.Path(sys.argv[3]).write_text("ready", encoding="ascii")
time.sleep(30)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(py_dir),
             str(archive.ARCHIVE_DIR), str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5.0
            while not ready.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            if not ready.exists():
                stdout, stderr = process.communicate(timeout=2)
                self.fail(f"child did not acquire archive lock: {stdout}\n{stderr}")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        archive._unlock(owner)
        self.assertFalse((archive.ARCHIVE_DIR / "lock").exists())
        self.assertEqual(self._lock_tombs(), [])

    def test_live_unverifiable_lock_and_manifest_symlink_are_never_reaped_or_followed(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        lock = archive.ARCHIVE_DIR / "lock"
        lock_body = f"pid={os.getpid()} start=unknown token=legacy\n".encode()
        lock.write_bytes(lock_body)
        old = archive.time.time() - 60
        os.utime(lock, (old, old))
        with mock.patch.object(archive.common, "pid_alive", return_value=True), \
                mock.patch.object(archive.common, "process_start_identity", return_value=None):
            self.assertIsNone(archive._try_lock())
        self.assertEqual(lock.read_bytes(), lock_body)
        lock.unlink()
        archive.set_enabled(True)

        outside = self.base / "outside-manifest"
        outside.write_text("do not append", encoding="utf-8")
        try:
            archive.MANIFEST.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        source = self.home / ".codex" / "sessions" / "rollout-no-follow.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text("safe", encoding="utf-8")
        with self.assertRaises(ValueError):
            archive.capture()
        self.assertEqual(outside.read_text(encoding="utf-8"), "do not append")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(archive.main(["--status"]), 2)

        config_target = self.base / "outside-config"
        config_target.write_text("do not replace", encoding="utf-8")
        archive.CONFIG.unlink()
        archive.CONFIG.symlink_to(config_target)
        with self.assertRaises(ValueError):
            archive.set_enabled(True)
        self.assertEqual(config_target.read_text(encoding="utf-8"), "do not replace")

    def test_dynamic_crush_registry_discovers_project_databases(self) -> None:
        global_data = self.base / "crush-global"
        project = self.base / "project"
        data_dir = project / ".crush"
        global_data.mkdir()
        data_dir.mkdir(parents=True)
        registry = global_data / "projects.json"
        registry.write_text(json.dumps({"projects": [{
            "path": str(project), "data_dir": str(data_dir), "last_accessed": 1}]}),
            encoding="utf-8")
        global_db = global_data / "crush.db"
        project_db = data_dir / "crush.db"
        legacy_db = self.home / ".crush" / "crush.db"
        global_db.write_bytes(b"global")
        project_db.write_bytes(b"project")
        legacy_db.parent.mkdir()
        legacy_db.write_bytes(b"legacy")
        with mock.patch.dict(os.environ, {"CRUSH_GLOBAL_DATA": str(global_data)}):
            found = {path for agent, path, _ in archive._discovered_sources()
                     if agent == "crush"}
            self.assertEqual(found, {registry, global_db, project_db, legacy_db})
            self.assertEqual(archive._source_location(str(project_db)),
                             (data_dir, Path("crush.db")))
            project_db.unlink()
            self.assertIsNotNone(archive._source_location(str(project_db)))

    def test_dynamic_crush_archive_survives_discovery_state_removal(self) -> None:
        global_data = self.base / "crush-global"
        project_data = self.base / "project" / ".crush"
        global_data.mkdir()
        project_data.mkdir(parents=True)
        registry = global_data / "projects.json"
        registry.write_text(json.dumps({"projects": [{
            "path": str(project_data.parent), "data_dir": str(project_data)}]}),
            encoding="utf-8")
        project_db = project_data / "crush.db"
        connection = sqlite3.connect(project_db)
        connection.execute("CREATE TABLE messages(body TEXT)")
        connection.execute("INSERT INTO messages VALUES('retained')")
        connection.commit()
        connection.close()
        with mock.patch.dict(os.environ, {"CRUSH_GLOBAL_DATA": str(global_data)}):
            self.assertEqual(archive.capture()["full"], 2)
            records = {record["path"]: record for record in archive._records()}
            db_chunk = archive._chunk_path(records[str(project_db)]["chunks"][0]["sha256"])
            registry_chunk = archive._chunk_path(records[str(registry)]["chunks"][0]["sha256"])
            project_db.unlink()
            registry.unlink()
        self.assertEqual({record["path"] for record in archive._records()},
                         {str(project_db), str(registry)})
        self.assertEqual(self._restore("crush.db"), 1)
        self.assertEqual(archive._prune()["reclaimed"], 0)
        self.assertTrue(db_chunk.is_file())
        self.assertTrue(registry_chunk.is_file())
        output = self.base / "restored"
        self.assertEqual(self._restore("crush.db", to=str(output)), 0)
        restored = sqlite3.connect(output / "crush.db")
        try:
            row = restored.execute("SELECT body FROM messages").fetchone()
        finally:
            restored.close()
        self.assertEqual(row, ("retained",))

    def test_opencode_direct_and_nested_storage_restore_to_dynamic_root(self) -> None:
        xdg = self.base / "xdg"
        storage = xdg / "opencode" / "storage"
        nested = storage / "sessions" / "nested.json"
        direct = storage / "direct.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("nested", encoding="utf-8")
        direct.write_text("direct", encoding="utf-8")
        with without_store_override(), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg),
                                             "OPENCODE_DB": ""}):
            found = {path for agent, path, sqlite in archive._discovered_sources()
                     if agent == "opencode" and not sqlite}
            self.assertEqual(found, {direct, nested})
            self.assertIsNotNone(archive._source_location(str(direct)))
            self.assertIsNotNone(archive._source_location(str(nested)))
            self.assertEqual(archive.capture()["full"], 2)
            direct.unlink()
            self.assertEqual(self._restore("direct.json"), 0)
            self.assertEqual(direct.read_text(encoding="utf-8"), "direct")

    def test_sqlite_wal_only_commit_invalidates_archive_signature(self) -> None:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        archive.ROOTS = [("opencode", ".local/share/opencode/opencode.db", True)]
        con = sqlite3.connect(db)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.execute("CREATE TABLE messages(body TEXT)")
            con.execute("INSERT INTO messages VALUES('first')")
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            first = archive.capture()
            first_record = archive._manifest()[str(db)]
            main_before = (db.stat().st_mtime_ns, db.stat().st_size)
            con.execute("INSERT INTO messages VALUES('second')")
            con.commit()
            self.assertEqual((db.stat().st_mtime_ns, db.stat().st_size), main_before)
            second = archive.capture()
            second_record = archive._manifest()[str(db)]
            idle = archive.capture()
        finally:
            con.close()
        snapshot = self.base / "snapshot.db"
        snapshot.write_bytes(archive._reconstruct(second_record))
        restored = sqlite3.connect(snapshot)
        try:
            rows = restored.execute("SELECT body FROM messages ORDER BY rowid").fetchall()
        finally:
            restored.close()
        self.assertEqual(first["full"], 1)
        self.assertEqual(second["full"], 1)
        self.assertNotEqual(first_record["source_sig"], second_record["source_sig"])
        self.assertEqual(rows, [("first",), ("second",)])
        self.assertEqual(idle["unchanged"], 1)
        self.assertEqual(idle["full"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows file-USN contract")
    def test_sqlite_signature_sees_restored_mtime_same_size_edits(self) -> None:
        source = self.base / "change-token.db"
        source.write_bytes(b"same size before")
        before, _, _ = archive._sqlite_source_state(source)
        stamp = source.stat().st_mtime_ns
        source.write_bytes(b"same size after!")
        os.utime(source, ns=(stamp, stamp))
        after, _, _ = archive._sqlite_source_state(source)
        self.assertNotEqual(before, after)

    def test_sqlite_zero_usn_selects_content_hash_fallback(self) -> None:
        self.assertIsNone(archive._file_usn_change_token(0))
        self.assertEqual(archive._file_usn_change_token(1), ("usn", 1))
        source = self.base / "change-token.db"
        source.write_bytes(b"content")
        with mock.patch.object(
                archive, "_metadata_change_token",
                return_value=archive._file_usn_change_token(0)), \
                mock.patch.object(
                    archive, "_hash_regular_file",
                    wraps=archive._hash_regular_file) as hashed:
            archive._sqlite_source_state(source)
        hashed.assert_called_once_with(source, source.parent)

    def test_sqlite_signature_hashes_when_file_usn_is_unavailable(self) -> None:
        source = self.base / "change-token.db"
        source.write_bytes(b"same size before")
        with mock.patch.object(archive, "_metadata_change_token", return_value=None):
            before, _, _ = archive._sqlite_source_state(source)
            stamp = source.stat().st_mtime_ns
            source.write_bytes(b"same size after!")
            os.utime(source, ns=(stamp, stamp))
            after, _, _ = archive._sqlite_source_state(source)
        self.assertNotEqual(before, after)

    def test_sqlite_hash_fallback_rejects_path_replacement(self) -> None:
        source = self.base / "change-token.db"
        source.write_bytes(b"same size before")
        original_hash = archive._hash_regular_file

        def replace_after_hash(path: Path, root: Path, **kwargs):
            result = original_hash(path, root, **kwargs)
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(b"same size after!")
            os.replace(replacement, source)
            return result

        with mock.patch.object(archive, "_metadata_change_token", return_value=None), \
                mock.patch.object(archive, "_hash_regular_file",
                                  side_effect=replace_after_hash):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                archive._sqlite_source_state(source)

    def test_sqlite_snapshot_closes_handles_before_failure_cleanup(self) -> None:
        source_path = self.base / "source.db"
        source_path.write_bytes(b"sqlite fixture")

        class Connection:
            def __init__(self, fail_backup: bool = False) -> None:
                self.fail_backup = fail_backup
                self.closed = False

            def backup(self, _destination) -> None:
                if self.fail_backup:
                    raise RuntimeError("backup failed")

            def commit(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        source = Connection(fail_backup=True)
        destination = Connection()
        closed_at_unlink = None
        original_unlink = Path.unlink

        def lock_sensitive_unlink(path: Path, *args, **kwargs) -> None:
            nonlocal closed_at_unlink
            if path.name.startswith(".snapshot-"):
                closed_at_unlink = source.closed, destination.closed
                if not all(closed_at_unlink):
                    raise PermissionError(13, "file is in use")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(
                archive.sqlite3, "connect", side_effect=[source, destination]), \
                mock.patch.object(Path, "unlink", lock_sensitive_unlink):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                archive._sqlite_snapshot(source_path)
        self.assertEqual(closed_at_unlink, (True, True))
        self.assertFalse(list(archive.ARCHIVE_DIR.glob(".snapshot-*.db")))

        source = Connection()
        with mock.patch.object(
                archive.sqlite3, "connect",
                side_effect=[source, RuntimeError("destination open failed")]):
            with self.assertRaisesRegex(RuntimeError, "destination open failed"):
                archive._sqlite_snapshot(source_path)
        self.assertTrue(source.closed)
        self.assertFalse(list(archive.ARCHIVE_DIR.glob(".snapshot-*.db")))

    def test_oversized_sqlite_wal_is_rejected_before_snapshot(self) -> None:
        db = self.base / "bounded.db"
        db.write_bytes(b"db")
        db.with_name(db.name + "-wal").write_bytes(b"w" * 33)
        with mock.patch.object(archive, "_MAX_ARCHIVE_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "WAL exceeds"):
                archive._sqlite_source_state(db)

    def test_claude_nested_transcripts_are_archived_without_following_links(self) -> None:
        archive.ROOTS = [("claude", ".claude/projects/*/**/*.jsonl", False)]
        project = self.home / ".claude" / "projects" / "project"
        parent = project / "parent.jsonl"
        child = project / "session" / "subagents" / "agent-1.jsonl"
        child.parent.mkdir(parents=True)
        parent.write_text("parent", encoding="utf-8")
        child.write_text("child", encoding="utf-8")
        escaped = self.base / "escaped"
        escaped.mkdir()
        leaked = escaped / "leaked.jsonl"
        leaked.write_text("leaked", encoding="utf-8")
        link = project / "linked"
        linked_file = link / leaked.name
        try:
            link.symlink_to(escaped, target_is_directory=True)
        except OSError:
            pass
        found = {path for agent, path, _ in archive._discovered_sources()
                 if agent == "claude"}
        self.assertEqual(found, {parent, child})
        self.assertNotIn(linked_file, found)
        self.assertEqual(archive.capture()["full"], 2)
        child.unlink()
        self.assertEqual(self._restore("agent-1.jsonl"), 0)
        self.assertEqual(child.read_text(encoding="utf-8"), "child")

    def test_windows_cursor_uses_redirected_appdata_for_capture_and_restore(self) -> None:
        archive.ROOTS = list(self.saved["ROOTS"])
        appdata = self.base / "roaming"
        fallback = self.home / "AppData" / "Roaming" / "Cursor" / "User" / \
            "globalStorage" / "state.vscdb"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"fallback")
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            locations = archive._cursor_db_locations("nt")
            source = locations[0][0] / locations[0][1]
            source.parent.mkdir(parents=True)
            con = sqlite3.connect(source)
            con.execute("CREATE TABLE cursor_data(body TEXT)")
            con.execute("INSERT INTO cursor_data VALUES('redirected')")
            con.commit()
            con.close()
            with mock.patch.object(archive, "_cursor_db_locations", return_value=locations):
                found = {path for agent, path, _ in archive._discovered_sources()
                         if agent == "cursor"}
                self.assertEqual(found, {source})
                self.assertNotIn(fallback, found)
                self.assertEqual(archive._source_location(str(source)), locations[0])
                self.assertIsNone(archive._source_location(str(fallback)))
                self.assertEqual(archive.capture()["full"], 1)
                source.unlink()
                self.assertEqual(self._restore("state.vscdb"), 0)
        con = sqlite3.connect(source)
        try:
            row = con.execute("SELECT body FROM cursor_data").fetchone()
        finally:
            con.close()
        self.assertEqual(row, ("redirected",))

    def test_antigravity_recursive_native_store_is_archived(self) -> None:
        transcript = (self.home / ".gemini" / "antigravity-cli" / "brain" / "session" /
                      ".system_generated" / "logs" / "transcript.jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text("native transcript", encoding="utf-8")
        archive.ROOTS = [
            ("antigravity", ".gemini/antigravity-cli/brain/**/*.jsonl", False)]
        found = list(archive._discovered_sources())
        self.assertEqual(found, [("antigravity", transcript, False)])
        self.assertEqual(archive.capture()["full"], 1)

    def test_kimi_session_depth_and_subagents_are_archived(self) -> None:
        session = "01234567-89ab-cdef-0123-456789abcdef"
        root = self.home / ".kimi" / "sessions" / "project-hash" / session
        parent = root / "context.jsonl"
        child = root / "subagents" / "child-id" / "wire.jsonl"
        child.parent.mkdir(parents=True)
        parent.write_text("parent", encoding="utf-8")
        child.write_text("child", encoding="utf-8")
        archive.ROOTS = [("kimi", ".kimi/sessions/**/*.jsonl", False)]
        found = {(agent, path) for agent, path, _ in archive._discovered_sources()}
        self.assertEqual(found, {("kimi", parent), ("kimi", child)})
        self.assertEqual(archive.capture()["full"], 2)
        self.assertTrue(all(session in record["sessions"] for record in archive._records()))

    def test_cline_native_roots_include_task_and_attribution_state(self) -> None:
        cline = self.base / "cline"
        conversation = cline / "data" / "tasks" / "task-1" / "api_conversation_history.json"
        history = cline / "data" / "state" / "taskHistory.json"
        conversation.parent.mkdir(parents=True)
        history.parent.mkdir(parents=True)
        conversation.write_text("conversation", encoding="utf-8")
        history.write_text("history", encoding="utf-8")
        archive.ROOTS = []
        with mock.patch.dict(os.environ, {"CLINE_DIR": str(cline)}):
            found = {(agent, path) for agent, path, _ in archive._discovered_sources()}
            self.assertEqual(found, {("cline", conversation), ("cline", history)})
            self.assertEqual(archive.capture()["full"], 2)

    def test_vanished_glob_candidate_is_reported_and_cli_returns_nonzero(self) -> None:
        good = self.home / ".codex" / "sessions" / "good.jsonl"
        missing = self.home / ".codex" / "sessions" / "missing.jsonl"
        good.parent.mkdir(parents=True)
        good.write_bytes(b"good")
        real_scandir = os.scandir
        real_stat = good.stat()

        class Entry:
            def __init__(self, path: Path) -> None:
                self.path = str(path)
                self.name = path.name

            def stat(self, *, follow_symlinks: bool):
                return real_stat

        class Entries:
            def __enter__(self):
                return iter((Entry(good), Entry(missing)))

            def __exit__(self, *_):
                return False

        def scandir(path):
            if Path(path) == good.parent:
                return Entries()
            return real_scandir(path)

        with mock.patch.object(archive.os, "scandir", side_effect=scandir):
            result = archive.capture()
        self.assertEqual(result["full"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["path"], str(missing))
        out = io.StringIO()
        with mock.patch.object(archive.os, "scandir", side_effect=scandir), \
                redirect_stdout(out):
            self.assertEqual(archive.main([]), 1)
        self.assertIn("archive incomplete: 1 source path(s) failed", out.getvalue())
        self.assertIn(str(missing), out.getvalue())

    def test_unreadable_claude_subtree_reports_failure_and_keeps_good_files(self) -> None:
        root = self.home / ".claude" / "projects"
        good = root / "good-project" / "chat.jsonl"
        bad = root / "bad-project"
        good.parent.mkdir(parents=True)
        bad.mkdir(parents=True)
        good.write_bytes(b"good")
        archive.ROOTS = [("claude", ".claude/projects/*/**/*.jsonl", False)]
        real_iterdir = Path.iterdir

        def iterdir(path: Path):
            if path == bad:
                raise PermissionError(13, "permission denied", str(path))
            return real_iterdir(path)

        with mock.patch.object(Path, "iterdir", autospec=True, side_effect=iterdir):
            result = archive.capture()
        self.assertEqual(result["full"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["path"], str(bad))
        self.assertEqual(result["failures"][0]["phase"], "walk")

    def test_unreadable_opencode_directory_reports_failure_and_keeps_good_db(self) -> None:
        good_root = self.base / "opencode-good"
        bad_root = self.base / "opencode-denied"
        good_root.mkdir()
        bad_root.mkdir()
        database = good_root / "opencode.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE messages(body TEXT)")
        connection.commit()
        connection.close()
        real_scandir = os.scandir

        def scandir(path):
            if Path(path) == bad_root:
                raise PermissionError(13, "permission denied", str(path))
            return real_scandir(path)

        archive.ROOTS = []
        roots = [str(bad_root), str(good_root)]
        with mock.patch.object(archive, "opencode_data_dirs", return_value=roots), \
                mock.patch.object(archive.os, "scandir", side_effect=scandir):
            result = archive.capture()
        self.assertEqual(result["full"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["path"], str(bad_root))
        self.assertEqual(result["failures"][0]["phase"], "scandir")

    def test_missing_explicit_opencode_database_is_a_failure(self) -> None:
        archive.ROOTS = []
        missing = self.base / "declared-opencode.db"
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(missing)}):
            result = archive.capture()
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["path"], str(missing))
        self.assertEqual(result["failures"][0]["phase"], "lstat")

    def test_absent_optional_roots_are_skips(self) -> None:
        result = archive.capture()
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["failed"], 0)

    def test_indexer_reports_archive_failures_without_content_changes(self) -> None:
        import indexer
        stats = {"appended": 0, "full": 0, "bytes_stored": 0, "failed": 2}
        with mock.patch.object(archive, "enabled", return_value=True), \
                mock.patch.object(archive, "capture", return_value=stats), \
                mock.patch.object(indexer.common, "log") as log:
            indexer.AutoIndexer._capture_archive(object())
        log.assert_called_once_with(
            "archive: 2 source path(s) failed; successful captures were kept")

    def test_every_capture_pass_persists_outcome_and_age(self) -> None:
        archive.set_enabled(True)
        self.assertEqual(archive.status()["state"], "freshness-unknown")
        first = archive.capture()
        self.assertEqual(first["files"], 0)
        status = archive.status()
        self.assertEqual(status["state"], "healthy")
        self.assertEqual(status["last_pass"]["outcome"], "healthy")
        self.assertLess(status["last_pass"]["age_s"], 1.0)
        old = time.time() - 180
        os.utime(archive.HEALTH, (old, old))
        health = json.loads(archive.HEALTH.read_text(encoding="utf-8"))
        health["last_pass_ms"] = int(old * 1000)
        archive.HEALTH.write_text(json.dumps(health), encoding="utf-8")
        before = archive.HEALTH.read_bytes()
        status = archive.status()
        self.assertFalse(status["last_pass"]["fresh"])
        self.assertEqual(archive.HEALTH.read_bytes(), before)

    def test_partial_capture_has_a_distinct_durable_state(self) -> None:
        archive.ROOTS = []
        missing = self.base / "declared-opencode.db"
        archive.set_enabled(True)
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(missing)}):
            result = archive.capture()
        self.assertEqual(result["failed"], 1)
        state = archive.status()
        self.assertEqual(state["state"], "partial")
        self.assertEqual(state["last_pass"]["outcome"], "partial")

    def test_invalid_capture_health_never_certifies_freshness(self) -> None:
        archive.set_enabled(True)
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        archive.HEALTH.write_text(json.dumps({
            "version": 1, "last_pass_ms": int((time.time() + 3600) * 1000),
            "outcome": "healthy", "detail": "fixture", "failed": "zero",
        }), encoding="utf-8")
        state = archive.status()
        self.assertEqual(state["state"], "capture-blocked")
        self.assertEqual(state["last_pass"]["outcome"], "unknown")
        self.assertFalse(state["last_pass"]["fresh"])

    def test_status_distinguishes_disabled_busy_and_repairable_tail(self) -> None:
        self.assertEqual(archive.status()["state"], "disabled")
        self._capture_codex()
        archive.set_enabled(True)
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        try:
            busy = archive.status()
            attempt = archive.capture()
        finally:
            archive._unlock(owner)
        self.assertEqual(busy["state"], "busy")
        self.assertEqual(busy["lock"]["pid"], os.getpid())
        self.assertTrue(attempt["busy"])
        self.assertEqual(archive._capture_health()["outcome"], "busy")
        with archive.MANIFEST.open("ab") as stream:
            stream.write(b'{"path":')
        before = archive.MANIFEST.read_bytes()
        self.assertEqual(archive.status()["state"], "repairable-tail")
        self.assertEqual(archive.MANIFEST.read_bytes(), before)

    def test_disabled_status_does_not_inspect_a_future_manifest(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        body = b'{"future_schema_field":{"version":2}}\n'
        archive.MANIFEST.write_bytes(body)

        with mock.patch.object(
                archive, "_manifest_status",
                side_effect=AssertionError("disabled status inspected manifest")):
            state = archive.status()
            output = io.StringIO()
            with redirect_stdout(output):
                rc = archive.main(["--status"])

        self.assertEqual(state["state"], "disabled")
        self.assertEqual(state["manifest_state"], "not-inspected")
        self.assertIsNone(state["files"])
        self.assertEqual(rc, 0)
        # a count it declined to measure is omitted, never rendered as a value
        self.assertNotIn("files", output.getvalue())
        self.assertEqual(archive.MANIFEST.read_bytes(), body)

    def test_damaged_config_is_never_laundered_to_disabled(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        cases = {
            "malformed": lambda path: path.write_bytes(b"{"),
            "oversize": lambda path: path.write_bytes(
                b"x" * (archive._MAX_CONFIG_BYTES + 1)),
            "nonregular": lambda path: path.mkdir(),
        }
        for label, create in cases.items():
            with self.subTest(case=label):
                path = archive.ARCHIVE_DIR / f"{label}.json"
                create(path)
                with mock.patch.object(archive, "CONFIG", path):
                    observed = archive.status(
                        stored_bytes=0, manifest_timeout_s=0.1)
                self.assertEqual(observed["state"], "capture-blocked")
                self.assertEqual(observed["config_state"], "unavailable")
                self.assertIsNone(observed["enabled"])
                self.assertNotEqual(observed["state"], "disabled")

    def test_disabled_status_skips_health_lock_and_store_walk(self) -> None:
        with (
            mock.patch.object(
                archive, "_capture_health",
                side_effect=AssertionError("disabled status read health")),
            mock.patch.object(
                archive, "_lock_status",
                side_effect=AssertionError("disabled status read lock")),
            mock.patch.object(
                archive.os, "walk",
                side_effect=AssertionError("disabled status walked storage")),
        ):
            observed = archive.status(manifest_timeout_s=0.1)
        self.assertEqual(observed["state"], "disabled")
        self.assertEqual(observed["last_pass"]["outcome"], "not-inspected")
        self.assertEqual(observed["lock"]["state"], "not-inspected")

    def test_status_prework_and_manifest_share_one_deadline(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        archive.CONFIG.write_text('{"enabled":true}', encoding="utf-8")
        clock = [0.0]

        def health() -> dict:
            clock[0] = 0.2
            return {"outcome": "healthy", "age_s": 0.0}

        with (
            mock.patch.object(
                archive.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(archive, "_capture_health", side_effect=health),
            mock.patch.object(
                archive, "_lock_status",
                side_effect=AssertionError(
                    "archive lock ran after the shared deadline")) as lock,
            mock.patch.object(
                archive, "_manifest_status",
                side_effect=AssertionError(
                    "manifest ran after the shared deadline")) as manifest,
        ):
            observed = archive.status(
                stored_bytes=0, manifest_timeout_s=0.1)
        self.assertEqual(observed["state"], "status-deferred")
        self.assertEqual(observed["last_pass"]["outcome"], "healthy")
        lock.assert_not_called()
        manifest.assert_not_called()

    def test_failed_and_busy_attempts_preserve_last_completed_pass(self) -> None:
        archive.set_enabled(True)
        archive.capture()
        completed = archive._capture_health()["last_pass_ms"]
        self.assertIsNotNone(completed)
        owner = archive._try_lock()
        self.assertIsNotNone(owner)
        try:
            with mock.patch.object(
                    archive.time, "time", return_value=completed / 1000 + 10):
                self.assertTrue(archive.capture()["busy"])
        finally:
            archive._unlock(owner)
        busy = archive._capture_health()
        self.assertEqual(busy["outcome"], "busy")
        self.assertEqual(busy["last_pass_ms"], completed)
        self.assertEqual(busy["last_attempt_ms"], completed + 10_000)

        with mock.patch.object(
                archive.time, "time", return_value=completed / 1000 + 20):
            archive.record_capture_failure(ValueError("future manifest schema"))
        failed = archive._capture_health()
        self.assertEqual(failed["outcome"], "capture-blocked")
        self.assertEqual(failed["last_pass_ms"], completed)
        self.assertEqual(failed["last_attempt_ms"], completed + 20_000)

    def test_future_manifest_status_is_capture_blocked_and_nonmutating(self) -> None:
        self._capture_codex()
        archive.set_enabled(True)
        record = archive._records()[0]
        body = (json.dumps({**record, "future_schema_field": {"version": 2}})
                + "\n").encode("utf-8")
        archive.MANIFEST.write_bytes(body)
        chunk_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in archive.STORE.rglob("*.xz")}

        state = archive.status()
        status_output = io.StringIO()
        with redirect_stdout(status_output):
            status_rc = archive.main(["--status"])
        output = io.StringIO()
        with redirect_stdout(output):
            rc = archive.main([])

        self.assertEqual(state["state"], "capture-blocked")
        self.assertEqual(state["manifest_state"], "future")
        self.assertEqual(status_rc, 1)
        # one cause, one line: the block is named once, not per symptom
        self.assertEqual(status_output.getvalue().count("capture-blocked"), 1)
        self.assertEqual(rc, 2)
        self.assertIn("unrecognized manifest record", output.getvalue())
        self.assertEqual(archive._capture_health()["outcome"], "capture-blocked")
        self.assertEqual(archive.MANIFEST.read_bytes(), body)
        self.assertEqual(chunk_hashes, {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in archive.STORE.rglob("*.xz")})

    def test_archive_lock_state_distinguishes_busy_and_wedged(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        lock = archive.ARCHIVE_DIR / "lock"
        now = time.time()
        lock.write_text(
            f"pid={os.getpid()} start=unknown token=fixture\n", encoding="utf-8")
        with mock.patch.object(archive.common, "pid_alive", return_value=True), \
                mock.patch.object(
                    archive.common, "process_start_identity", return_value=None):
            os.utime(lock, (now, now))
            self.assertEqual(archive._lock_status()["state"], "lock-protected")
            old = now - archive._LOCK_WEDGED_S - 1
            os.utime(lock, (old, old))
            wedged = archive._lock_status()
        self.assertEqual(wedged["state"], "lock-wedged")
        self.assertEqual(wedged["pid"], os.getpid())
        self.assertGreater(wedged["age_s"], archive._LOCK_WEDGED_S)

    def test_prune_names_a_wedged_lock_and_required_repair(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        lock = archive.ARCHIVE_DIR / "lock"
        lock.write_text(
            f"pid={os.getpid()} start=unknown token=fixture\n", encoding="utf-8")
        old = time.time() - archive._LOCK_WEDGED_S - 1
        os.utime(lock, (old, old))
        output = io.StringIO()
        with mock.patch.object(archive.common, "pid_alive", return_value=True), \
                mock.patch.object(
                    archive.common, "process_start_identity", return_value=None), \
                redirect_stdout(output):
            rc = archive.main(["--prune"])

        self.assertEqual(rc, 1)
        self.assertIn("lock-wedged", output.getvalue())
        self.assertIn("repair required", output.getvalue())
        self.assertIn(str(lock), output.getvalue())
        self.assertNotIn("try again", output.getvalue())
        self.assertTrue(lock.exists())

    def test_indexer_persists_and_logs_capture_exception(self) -> None:
        import indexer
        failure = ValueError("future manifest schema")
        with mock.patch.object(archive, "enabled", return_value=True), \
                mock.patch.object(archive, "capture", side_effect=failure), \
                mock.patch.object(archive, "record_capture_failure") as durable, \
                mock.patch.object(indexer.common, "log") as log:
            indexer.AutoIndexer._capture_archive(object())
        durable.assert_called_once_with(failure)
        self.assertIn("future manifest schema", log.call_args_list[0].args[0])

    def test_keep_zero_preserves_duplicate_and_distinct_versions(self) -> None:
        source, first = self._capture_codex()
        archive._set_config(keep=0)
        for tick in range(3):
            stamp = source.stat().st_mtime_ns + tick + 1_000_000
            os.utime(source, ns=(stamp, stamp))
            self.assertEqual(archive.capture()["unchanged"], 1)
        source.write_bytes(first + b"second")
        self.assertEqual(archive.capture()["appended"], 1)
        before = archive._records()
        pruned = archive._prune()
        after = archive._records()
        self.assertEqual(pruned["dropped"], 0)
        self.assertEqual(after, before)
        self.assertEqual(len(after), 5)
        self.assertEqual(len({record["sha256"] for record in after}), 2)

    def test_home_migrated_record_restores_and_capture_continues(self) -> None:
        source, original = self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        migrated_home = self.base / "new-home"
        migrated_home.mkdir()
        archive.HOME = migrated_home
        migrated_source = (
            migrated_home / ".codex" / "sessions" / source.name)

        self.assertEqual(archive.restore("archive-safe"), 0)
        self.assertEqual(migrated_source.read_bytes(), original)
        migrated_source.write_bytes(original + b'{"text":"new-home"}\n')
        self.assertEqual(archive.capture()["full"], 1)
        archive.set_enabled(True)

        self.assertTrue(chunk.is_file())
        self.assertIn(json.dumps(record), archive.MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(archive.status()["state"], "migration")

    def test_windows_manifest_path_restores_on_posix(self) -> None:
        source, original = self._capture_codex()
        record = archive._records()[0]
        source.unlink()
        record["path"] = (
            r"C:\Users\Example\.codex\sessions\rollout-archive-safe.jsonl")
        archive.MANIFEST.write_text(json.dumps(record) + "\n", encoding="utf-8")

        self.assertEqual(archive.restore("archive-safe"), 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(archive._record_migrated(record))

    def test_windows_manifest_path_uses_portable_name_with_to(self) -> None:
        source, original = self._capture_codex()
        record = archive._records()[0]
        record["path"] = (
            r"C:\Users\Example\.codex\sessions\rollout-archive-safe.jsonl")
        archive.MANIFEST.write_text(json.dumps(record) + "\n", encoding="utf-8")
        output = self.base / "out"

        self.assertEqual(archive.restore("archive-safe", to=str(output)), 0)
        self.assertEqual(
            (output / "rollout-archive-safe.jsonl").read_bytes(), original)
        self.assertFalse((output / record["path"]).exists())

    def test_portable_paths_reject_devices_unc_ads_and_traversal(self) -> None:
        values = (
            r"\\?\C:\Users\Example\.codex\sessions\rollout-safe.jsonl",
            r"\\server\share\.codex\sessions\rollout-safe.jsonl",
            r"C:\Users\Example\.codex\sessions\rollout-safe.jsonl:stream",
            r"C:\Users\Example\.codex\sessions\..\rollout-safe.jsonl",
            r"C:Users\Example\.codex\sessions\rollout-safe.jsonl",
            r"/Users/Example/.codex/sessions/..\outside.jsonl",
            r"/Users/Example/.codex/sessions/rollout-safe.jsonl:stream",
            r"/Users/Example/.codex/sessions/CON.jsonl",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(archive._manifest_source_shape(
                    value, "codex", False))

    def test_crush_portable_source_accepts_the_nested_stores_it_captures(
            self) -> None:
        accepted = {
            "/Users/Example/.local/share/crush/crush.db":
                ".local/share/crush/crush.db",
            "/Users/Example/.local/share/crush/projdir/crush.db":
                ".local/share/crush/projdir/crush.db",
            "/Users/Example/.local/share/crush/deep/nested/crush.db":
                ".local/share/crush/deep/nested/crush.db",
            r"C:\Users\Example\AppData\Local\crush\projdir\crush.db":
                "AppData/Local/crush/projdir/crush.db",
        }
        for value, relative in accepted.items():
            with self.subTest(value=value):
                self.assertEqual(
                    archive._portable_source_relative(value, "crush", True),
                    Path(relative))
        rejected = (
            "/Users/Example/.local/share/crush/projdir/other.db",
            "/Users/Example/.local/share/crush/projdir/crush.db.bak",
            "/Users/Example/.local/share/elsewhere/projdir/crush.db",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(
                    archive._portable_source_relative(value, "crush", True))
        # the registry is a root-only file, unlike the stores it points at
        self.assertEqual(
            archive._portable_source_relative(
                "/Users/Example/.local/share/crush/projects.json",
                "crush", False),
            Path(".local/share/crush/projects.json"))
        self.assertIsNone(archive._portable_source_relative(
            "/Users/Example/.local/share/crush/projdir/projects.json",
            "crush", False))

    def test_archive_import_uses_authoritative_discovery_home(self) -> None:
        configured = self.base / "configured-home"
        data = self.base / "configured-data"
        env = os.environ.copy()
        env.update({
            "AGREP_HOME": str(configured), "AGREP_DATA_DIR": str(data),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        result = subprocess.run(
            [sys.executable, "-c", "import archive; print(archive.HOME)"],
            cwd=Path(__file__).resolve().parent, env=env,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(configured))

    def test_dynamic_store_records_follow_home_and_data_root_migration(self) -> None:
        old = self.base / "old-home"
        new = self.base / "new-home"
        new.mkdir()
        records = [
            {
                "path": str(old / "Library" / "Application Support" / "Code" /
                            "User" / "globalStorage" /
                            "saoudrizwan.claude-dev" / "tasks" / "task-1" /
                            "message.json"),
                "agent": "cline", "sqlite": False,
            },
            {
                "path": str(old / "AppData" / "Local" / "opencode" /
                            "opencode.db"),
                "agent": "opencode", "sqlite": True,
            },
            {
                "path": str(old / "AppData" / "Local" / "crush" /
                            "projects.json"),
                "agent": "crush", "sqlite": False,
            },
        ]
        local = self.base / "new-local"
        archive.HOME = new
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            locations = [archive._restore_location(record) for record in records]
            self.assertTrue(all(archive._record_migrated(record)
                                for record in records))
        self.assertEqual(
            locations[0],
            (new, Path("Library/Application Support/Code/User/globalStorage/"
                       "saoudrizwan.claude-dev/tasks/task-1/message.json")))
        self.assertEqual(locations[1], (local, Path("opencode/opencode.db")))
        self.assertEqual(locations[2], (local, Path("crush/projects.json")))

        old_xdg = self.base / "old-xdg"
        new_xdg = self.base / "new-xdg"
        external = {
            "path": str(old_xdg / "opencode" / "storage" /
                        "session" / "entry.json"),
            "agent": "opencode", "sqlite": False,
        }
        with without_store_override(), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(new_xdg)}):
            self.assertEqual(
                archive._restore_location(external),
                (new_xdg / "opencode", Path("storage/session/entry.json")))
            self.assertTrue(archive._record_migrated(external))

    def test_prune_refuses_future_schema_record_without_deleting_chunk(self) -> None:
        self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        future = {**record, "future_schema_field": {"version": 2}}
        archive.MANIFEST.write_text(json.dumps(future) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unrecognized manifest record"):
            archive._prune()

        self.assertTrue(chunk.is_file())
        self.assertEqual(json.loads(archive.MANIFEST.read_text(encoding="utf-8")), future)

    def test_unchanged_capture_refuses_future_same_path_record(self) -> None:
        self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        future = {**record, "future_schema_field": {"version": 2}}
        body = (
            json.dumps(record) + "\n" + json.dumps(future) + "\n"
        ).encode("utf-8")
        archive.MANIFEST.write_bytes(body)

        with self.assertRaisesRegex(ValueError, "refusing to capture"):
            archive.capture()

        self.assertEqual(archive.MANIFEST.read_bytes(), body)
        self.assertTrue(chunk.is_file())

    def test_prune_refuses_overlong_future_record_without_deleting_chunk(
            self) -> None:
        self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        future = {
            **record,
            "future_schema_field": "x" * archive._MAX_MANIFEST_LINE,
        }
        body = (json.dumps(future) + "\n").encode("utf-8")
        archive.MANIFEST.write_bytes(body)

        with self.assertRaisesRegex(ValueError, "record bound"):
            archive._prune()

        self.assertEqual(archive.MANIFEST.read_bytes(), body)
        self.assertTrue(chunk.is_file())

    def test_prune_repairs_only_an_unframed_torn_tail(self) -> None:
        self._capture_codex()
        record = archive._records()[0]
        chunk = archive._chunk_path(record["chunks"][0]["sha256"])
        healthy = archive.MANIFEST.read_bytes()
        archive.MANIFEST.write_bytes(healthy + b'{"path":')

        archive._prune()

        self.assertEqual(archive.MANIFEST.read_bytes(), healthy)
        unsafe = healthy + b'{"path":\n'
        archive.MANIFEST.write_bytes(unsafe)
        with self.assertRaisesRegex(ValueError, "unsafe manifest"):
            archive._prune()
        self.assertEqual(archive.MANIFEST.read_bytes(), unsafe)
        self.assertTrue(chunk.is_file())

    def test_touch_records_compact_and_keep_bounds_records_per_path(self) -> None:
        source, first = self._capture_codex()
        archive._set_config(keep=2)
        archive._append_manifest(archive._records()[0])
        self.assertEqual(archive.capture()["unchanged"], 1)
        self.assertEqual(len(archive._records()), 1)
        for tick in range(3):
            stamp = source.stat().st_mtime_ns + tick + 1_000_000
            os.utime(source, ns=(stamp, stamp))
            self.assertEqual(archive.capture()["unchanged"], 1)
        records = archive._records()
        self.assertEqual(len(records), 1)
        second = first + b"two\n"
        source.write_bytes(second)
        archive.capture()
        third = second + b"three\n"
        source.write_bytes(third)
        archive.capture()
        records = archive._records()
        self.assertEqual(len(records), 2)
        self.assertEqual([record["sha256"] for record in records],
                         [archive.hashlib.sha256(second).hexdigest(),
                          archive.hashlib.sha256(third).hexdigest()])

    def test_negative_keep_is_usage_error(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            archive.main(["--keep", "-1"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("keep", archive._config())

    def test_capture_and_restore_use_streaming_paths(self) -> None:
        source = self.home / ".codex" / "sessions" / "rollout-streamed.jsonl"
        source.parent.mkdir(parents=True)
        data = (b"0123456789abcdef" * (256 * 1024)) + b"tail"
        source.write_bytes(data)
        with mock.patch.object(archive, "_read_source", side_effect=AssertionError("buffered")):
            self.assertEqual(archive.capture()["full"], 1)
        source.unlink()
        with mock.patch.object(archive, "_reconstruct", side_effect=AssertionError("buffered")):
            self.assertEqual(self._restore("rollout-streamed"), 0)
        self.assertEqual(source.read_bytes(), data)

    def _wal_cursor_store(self, value: str) -> Path:
        db = (self.home / "Library" / "Application Support" / "Cursor" /
              "User" / "globalStorage" / "state.vscdb")
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            con.execute("CREATE TABLE IF NOT EXISTS notes(body TEXT)")
            con.execute("DELETE FROM notes")
            con.execute("INSERT INTO notes VALUES(?)", (value,))
            con.commit()
        finally:
            con.close()
        return db

    def _notes_of(self, db: Path) -> list[tuple]:
        con = sqlite3.connect(db)
        try:
            return con.execute("SELECT body FROM notes").fetchall()
        finally:
            con.close()

    def test_first_capture_of_a_quiescent_wal_store_succeeds(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("first-pass")
        self.assertEqual(sorted(p.name for p in db.parent.iterdir()), [db.name])
        first = archive.capture()
        self.assertEqual((first["full"], first["failed"]), (1, 0))
        self.assertEqual(first["failures"], [])
        second = archive.capture()
        self.assertEqual((second["unchanged"], second["full"], second["failed"]),
                         (1, 0, 0))

    def test_backup_authored_empty_wal_is_not_source_activity(self) -> None:
        db = self._wal_cursor_store("settled")
        before, activity_before, _ = archive._sqlite_source_state(db)
        archive._sqlite_snapshot(db).unlink(missing_ok=True)
        wal = db.with_name(db.name + "-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size == 0)
        after, activity_after, _ = archive._sqlite_source_state(db)
        self.assertEqual(before, after)
        self.assertEqual(activity_before, activity_after)

    def test_restore_supersedes_stale_sqlite_sidecars(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        live = sqlite3.connect(db)
        try:
            live.execute("DELETE FROM notes")
            live.execute("INSERT INTO notes VALUES('POST_ARCHIVE')")
            live.commit()
            self.assertTrue(db.with_name(db.name + "-wal").stat().st_size > 0)
            self.assertTrue(db.with_name(db.name + "-shm").exists())
        finally:
            live.close()
        stale_wal = db.with_name(db.name + "-wal")
        stale_wal.write_bytes(b"\x00" * 4096)
        self.assertEqual(self._restore("state.vscdb", force=True), 0)
        self.assertFalse(stale_wal.exists())
        self.assertFalse(db.with_name(db.name + "-shm").exists())
        self.assertEqual(self._notes_of(db), [("ARCHIVED",)])

    def test_failed_sqlite_restore_rolls_back_stale_sidecars(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        wal = db.with_name(db.name + "-wal")
        shm = db.with_name(db.name + "-shm")
        wal_bytes = b"deterministic stale wal"
        shm_bytes = b"deterministic stale shm"
        wal.write_bytes(wal_bytes)
        shm.write_bytes(shm_bytes)

        with mock.patch.object(
                archive, "_replace_restore_temp",
                side_effect=OSError("denied")) as replace_main:
            self.assertEqual(self._restore("state.vscdb", force=True), 1)

        replace_main.assert_called_once()
        self.assertEqual(wal.read_bytes(), wal_bytes)
        self.assertEqual(shm.read_bytes(), shm_bytes)
        self.assertEqual(self._notes_of(db), [("ARCHIVED",)])
        self.assertFalse(list(db.parent.glob(".*.superseded-*")))

    def test_synthetic_partial_sidecar_lock_rolls_back_prior_parking(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        before = db.read_bytes()
        wal = db.with_name(db.name + "-wal")
        shm = db.with_name(db.name + "-shm")
        wal_bytes = b"partially parked wal"
        shm_bytes = b"locked shm bytes"
        wal.write_bytes(wal_bytes)
        shm.write_bytes(shm_bytes)
        real_move = archive.common.replace_with_retry
        attempts = []
        moves = []

        def locked(src, dst, *args, **kwargs):
            moves.append((Path(src), Path(dst), kwargs.get("attempts")))
            if Path(src) == shm:
                attempts.append((Path(src), Path(dst), kwargs.get("attempts")))
                error = PermissionError(13, "file is in use", str(shm))
                error.winerror = 32
                raise error
            return real_move(src, dst, *args, **kwargs)

        output = io.StringIO()
        with mock.patch.object(
                archive.common, "replace_with_retry", side_effect=locked), \
                redirect_stdout(output):
            rc = archive.restore("state.vscdb", force=True)

        self.assertEqual(rc, 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][0], shm)
        self.assertEqual(attempts[0][2], 6)
        self.assertTrue(any(
            src == wal and ".superseded-" in dst.name
            for src, dst, _ in moves))
        self.assertTrue(any(
            ".superseded-" in src.name and dst == wal
            for src, dst, _ in moves))
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(wal.read_bytes(), wal_bytes)
        self.assertEqual(shm.read_bytes(), shm_bytes)
        self.assertFalse(list(db.parent.glob(".*.superseded-*")))
        refusal = output.getvalue().lower()
        self.assertNotIn("sha256 verified", refusal)
        self.assertIn("state.vscdb-shm", refusal)
        self.assertIn("is in use", refusal)
        self.assertIn(
            "close the application using this history store, then rerun the "
            "same `agrep restore` command", refusal)

    def test_failed_parking_names_unremovable_empty_reservation(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        before = db.read_bytes()
        wal = db.with_name(db.name + "-wal")
        wal_bytes = b"locked wal with blocked reservation cleanup"
        wal.write_bytes(wal_bytes)
        real_unlink = Path.unlink
        move_attempts = []
        cleanup_attempts = []

        def locked(src, dst, *args, **kwargs):
            move_attempts.append((Path(src), Path(dst), kwargs.get("attempts")))
            raise OSError(13, "simulated parking refusal")

        def blocked_cleanup(path, missing_ok=False):
            if ".superseded-" in path.name:
                cleanup_attempts.append(path)
                raise OSError(13, "simulated reservation cleanup refusal")
            return real_unlink(path, missing_ok=missing_ok)

        output = io.StringIO()
        with mock.patch.object(
                archive.common, "replace_with_retry", side_effect=locked), \
                mock.patch.object(Path, "unlink", blocked_cleanup), \
                redirect_stdout(output):
            rc = archive.restore("state.vscdb", force=True)

        self.assertEqual(rc, 1)
        self.assertEqual(len(move_attempts), 1)
        self.assertEqual(move_attempts[0][0], wal)
        self.assertEqual(move_attempts[0][2], 6)
        self.assertEqual(len(cleanup_attempts), 1)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(wal.read_bytes(), wal_bytes)
        residue = list(db.parent.glob(".*.superseded-*"))
        self.assertEqual(residue, cleanup_attempts)
        disclosure = output.getvalue()
        self.assertNotIn("sha256 verified", disclosure)
        self.assertIn(
            "restore could not remove unused sidecar parking file", disclosure)
        self.assertIn(str(residue[0]), disclosure)

    @unittest.skipUnless(os.name == "nt", "Windows SQLite sharing contract")
    def test_locked_sqlite_wal_refuses_fast_without_residue(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        live = sqlite3.connect(db)
        try:
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("INSERT INTO notes VALUES('LIVE_LOCKED_ROW')")
            live.commit()
            before = db.read_bytes()
            wal = db.with_name(db.name + "-wal")
            shm = db.with_name(db.name + "-shm")
            wal_bytes = wal.read_bytes()
            shm_bytes = shm.read_bytes()
            self.assertTrue(wal_bytes)
            self.assertTrue(shm_bytes)

            output = io.StringIO()
            started = time.perf_counter()
            with redirect_stdout(output):
                rc = archive.restore("state.vscdb", force=True)
            elapsed = time.perf_counter() - started

            self.assertEqual(rc, 1)
            self.assertLess(elapsed, 2.0)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(wal.read_bytes(), wal_bytes)
            self.assertEqual(shm.read_bytes(), shm_bytes)
            self.assertEqual(
                live.execute("SELECT body FROM notes ORDER BY rowid").fetchall(),
                [("ARCHIVED",), ("LIVE_LOCKED_ROW",)])
            self.assertFalse(list(db.parent.glob(".*.superseded-*")))
            self.assertFalse(list(db.parent.glob(".*.restore-*")))
            refusal = output.getvalue().lower()
            self.assertNotIn("sha256 verified", refusal)
            self.assertIn("state.vscdb-wal", refusal)
            self.assertIn("is in use", refusal)
            self.assertIn(
                "close the application using this history store, then rerun the "
                "same `agrep restore` command", refusal)
        finally:
            live.close()

    def test_sqlite_restore_verification_refuses_a_symlinked_sidecar(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        outside = self.base / "outside-wal"
        outside.write_bytes(b"outside")
        wal = db.with_name(db.name + "-wal")
        wal.unlink(missing_ok=True)
        db.with_name(db.name + "-shm").unlink(missing_ok=True)
        try:
            wal.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self._restore("state.vscdb", force=True), 1)
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(wal.is_symlink())

    # Two action flags in one run have no defined winner; the verb must
    # reject the combination instead of silently obeying the first branch
    # (`--on --off` previously enabled retention and exited 0).
    def test_conflicting_archive_action_flags_are_rejected(self) -> None:
        for argv in (["--on", "--off"], ["--off", "--on"],
                     ["--off", "--status"], ["--keep", "5", "--status"],
                     ["--prune", "--status"]):
            with self.subTest(argv=argv):
                with redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        archive.main(argv)
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse(archive.CONFIG.exists(),
                         "a rejected flag combination changed retention state")

    # A restore whose superseded-sidecar cleanup fails must fail its verdict
    # and name the residue, never print "sha256 verified".
    def test_restore_fails_loudly_when_superseded_cleanup_leaves_residue(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        wal = db.with_name(db.name + "-wal")
        shm = db.with_name(db.name + "-shm")
        wal_bytes = b"cleanup stale wal"
        shm_bytes = b"cleanup stale shm"
        wal.write_bytes(wal_bytes)
        shm.write_bytes(shm_bytes)
        real_unlink = Path.unlink
        cleanup_attempts = []

        def flaky(path, missing_ok=False):
            if ".superseded-" in path.name:
                cleanup_attempts.append(path)
                raise OSError(13, "simulated cleanup failure")
            return real_unlink(path, missing_ok=missing_ok)

        buffer = io.StringIO()
        with mock.patch.object(Path, "unlink", flaky), \
                redirect_stdout(buffer):
            rc = archive.restore("state.vscdb", force=True)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(len(cleanup_attempts), 2)
        out = buffer.getvalue()
        self.assertNotIn("sha256 verified", out)
        self.assertIn("superseded sidecar cleanup failed", out)
        residue = sorted(p for p in db.parent.iterdir()
                         if ".superseded-" in p.name)
        self.assertTrue(residue, "the failed cleanup left no residue to name")
        self.assertTrue(all(str(p) in out for p in residue),
                        f"residue path missing from output: {out!r}")
        self.assertEqual(
            {p.read_bytes() for p in residue}, {wal_bytes, shm_bytes})

    def test_failed_restore_rollback_discloses_the_parked_sidecar(self) -> None:
        archive.ROOTS = []
        db = self._wal_cursor_store("ARCHIVED")
        self.assertEqual(archive.capture()["full"], 1)
        wal = db.with_name(db.name + "-wal")
        shm = db.with_name(db.name + "-shm")
        wal_bytes = b"rollback stale wal"
        shm_bytes = b"rollback stale shm"
        wal.write_bytes(wal_bytes)
        shm.write_bytes(shm_bytes)
        real_move = archive.common.replace_with_retry
        rollback_attempts = []

        def flaky(src, dst, *args, **kwargs):
            if ".superseded-" in Path(src).name:
                rollback_attempts.append((Path(src), Path(dst)))
                raise OSError(13, "simulated rollback failure")
            return real_move(src, dst, *args, **kwargs)

        buffer = io.StringIO()
        with mock.patch.object(
                archive, "_replace_restore_temp",
                side_effect=OSError("denied")) as replace_main, \
                mock.patch.object(
                    archive.common, "replace_with_retry", flaky), \
                redirect_stdout(buffer):
            rc = archive.restore("state.vscdb", force=True)
        self.assertEqual(rc, 1)
        replace_main.assert_called_once()
        self.assertEqual(
            {dst for _src, dst in rollback_attempts}, {wal, shm})
        out = buffer.getvalue()
        parked = sorted(p for p in db.parent.iterdir()
                        if ".superseded-" in p.name)
        self.assertTrue(parked, "rollback failure left no parked sidecar")
        self.assertTrue(all(str(p) in out for p in parked),
                        f"parked path missing from output: {out!r}")
        self.assertEqual(
            {p.read_bytes() for p in parked}, {wal_bytes, shm_bytes})

    # Non-force publication must be atomic: a file born between the absence
    # check and the publish syscall survives.
    @unittest.skipIf(os.name == "nt", "dir_fd publication is the posix leg")
    def test_nonforce_restore_never_replaces_a_file_born_in_the_window(self) -> None:
        source, _ = self._capture_codex()
        source.unlink()
        live = b'{"type":"event_msg","text":"live-new-transcript"}\n'
        real_link = os.link

        def intruder(src, dst, *, src_dir_fd=None, dst_dir_fd=None,
                     follow_symlinks=True):
            # another process creates the destination inside the window
            source.write_bytes(live)
            return real_link(src, dst, src_dir_fd=src_dir_fd,
                             dst_dir_fd=dst_dir_fd,
                             follow_symlinks=follow_symlinks)

        buffer = io.StringIO()
        with mock.patch.object(archive.os, "link", side_effect=intruder):
            with redirect_stdout(buffer):
                rc = archive.restore("archive-safe")
        self.assertEqual(rc, 1)
        self.assertIn("exists, not overwriting", buffer.getvalue())
        self.assertEqual(source.read_bytes(), live)
        leftovers = [p.name for p in source.parent.iterdir()
                     if p.name.startswith(".rollout")]
        self.assertEqual(leftovers, [], "restore temp survived the refusal")

    @unittest.skipIf(os.name == "nt", "dir_fd publication is the posix leg")
    def test_force_restore_still_replaces_atomically(self) -> None:
        source, data = self._capture_codex()
        source.write_bytes(b"live")
        with mock.patch.object(
                archive.os, "link",
                side_effect=AssertionError("force path must not link")):
            self.assertEqual(self._restore("archive-safe", force=True), 0)
        self.assertEqual(source.read_bytes(), data)


class GlobNonMatchTests(unittest.TestCase):
    """A sibling without the literal component is a non-match, not a failure.

    The laptop logged `archive: failed ...\\.gemini\\tmp\\bin\\chats: cannot
    find the file` on every capture pass forever: `.gemini/tmp/*/chats/*.json`
    visits every directory under tmp, and only some have a chats/ child.
    """

    def test_a_sibling_without_the_literal_component_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".gemini/tmp/session-a/chats").mkdir(parents=True)
            (root / ".gemini/tmp/session-a/chats/c.json").write_text("{}")
            (root / ".gemini/tmp/bin").mkdir(parents=True)  # no chats/ child
            failures: list[dict] = []
            found = archive._glob_paths(
                root, ".gemini/tmp/*/chats/*.json", failures)
            self.assertEqual(
                [p.name for p in found], ["c.json"])
            self.assertEqual(
                failures, [],
                "a sibling lacking the literal component was reported as a "
                "capture failure")

    def test_a_missing_wildcard_root_is_still_silent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            failures: list[dict] = []
            found = archive._glob_paths(
                Path(raw), ".gemini/tmp/*/chats/*.json", failures)
            self.assertEqual(found, [])
            self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
