from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
import embedding_store  # noqa: E402
import ownerfile  # noqa: E402


PY_DIR = Path(__file__).resolve().parent


class EmbeddingPublishLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / "embeddings.f32"
        self.path = self.root / ".embeddings.f32.publish.lock"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _raw(
            self, *, pid: int = 4242, token: str = "a" * 32,
            process_start: object = "birth", created_at: float = 100.0) -> bytes:
        return json.dumps({
            "pid": pid, "token": token, "process_start": process_start,
            "created_at": created_at,
        }).encode()

    def _write(self, raw: bytes, *, age: float = 0.0,
               now: float = 1000.0) -> None:
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

    def test_wire_mode_retained_handle_context_return_and_release(self) -> None:
        lock = common.EmbeddingPublishLock(self.target, timeout=0)
        initial_token = lock.token
        create = ownerfile.create_exclusive
        with mock.patch.object(
                ownerfile, "create_exclusive", wraps=create) as create_call:
            with mock.patch.object(
                    embedding_store.time, "time", return_value=123.5), \
                    mock.patch.object(
                        embedding_store,
                        "process_start_identity", return_value="birth"):
                with lock as entered:
                    self.assertIs(entered, lock)
                    self.assertTrue(lock.owned)
                    self.assertIsInstance(lock.handle, ownerfile.Handle)
                    self.assertIsNotNone(lock.handle.fd)
                    first_handle = lock.handle
                    first_fd = lock.handle.fd
                    expected = json.dumps({
                        "pid": os.getpid(), "token": initial_token,
                        "process_start": "birth", "created_at": 123.5,
                    }).encode()
                    self.assertEqual(lock.token, initial_token)
                    self.assertEqual(lock.handle.snapshot.raw, expected)
                    self.assertEqual(self.path.read_bytes(), expected)
                    self.assertNotIn(b"\n", expected)
            self.assertFalse(lock.owned)
            self.assertIsNone(lock.handle)
            self.assertFalse(self.path.exists())
            self.assertIsNone(first_handle.fd)
            with self.assertRaises(OSError):
                os.fstat(first_fd)

            with mock.patch.object(
                    embedding_store.time, "time", return_value=124.5), \
                    mock.patch.object(
                        embedding_store,
                        "process_start_identity", return_value="birth"):
                with lock as entered:
                    self.assertIs(entered, lock)
                    second = json.loads(self.path.read_bytes())
                    self.assertEqual(lock.token, initial_token)
                    self.assertEqual(second["token"], initial_token)
                    self.assertEqual(second["created_at"], 124.5)
            self.assertEqual(create_call.call_count, 2)
            self.assertTrue(all(
                call.kwargs.get("mode") == 0o777
                for call in create_call.call_args_list))
            self.assertTrue(all(
                call.kwargs.get("retain_fd") is True
                for call in create_call.call_args_list))
        self.assertFalse(lock.owned)
        self.assertIsNone(lock.handle)
        self.assertFalse(self.path.exists())

    def test_owner_state_and_legacy_birth_policy(self) -> None:
        cases = (
            ("exact", "birth", True, "birth", False),
            ("dead", "birth", False, None, True),
            ("reused", "birth", True, "other-birth", True),
            ("missing-expected", None, True, "birth", False),
            ("falsey-expected", "", True, "birth", False),
            ("unreadable-actual", "birth", True, None, False),
            ("falsey-actual", "birth", True, "", False),
            ("unknown-reused", "unknown", True, "birth", True),
            ("none-reused", "None", True, "birth", True),
            ("unknown-unreadable", "unknown", True, None, False),
        )
        for label, expected_start, alive, actual_start, acquired in cases:
            with self.subTest(label=label):
                original = self._raw(process_start=expected_start)
                self._write(original)
                lock = common.EmbeddingPublishLock(self.target, timeout=0)
                with mock.patch.object(
                        embedding_store, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            embedding_store, "process_start_identity",
                            return_value=actual_start):
                    if acquired:
                        with lock:
                            self.assertNotEqual(self.path.read_bytes(), original)
                    else:
                        with self.assertRaises(TimeoutError):
                            lock.__enter__()
                if acquired:
                    self.assertFalse(self.path.exists())
                else:
                    self.assertEqual(self.path.read_bytes(), original)
                    self.path.unlink()

    def test_malformed_records_use_strict_120_second_mtime_grace(self) -> None:
        malformed = (
            b"{", b"[]", b"\xff",
            b'{"pid":1e309,"token":"x","process_start":"birth"}',
            b"[" * 2000 + b"]" * 2000,
        )
        for body in malformed:
            with self.subTest(body=body[:32]):
                self._write(body, age=120.0)
                with mock.patch.object(
                        embedding_store.time, "time", return_value=1000.0):
                    with self.assertRaises(TimeoutError):
                        common.EmbeddingPublishLock(
                            self.target, timeout=0).__enter__()
                self.assertEqual(self.path.read_bytes(), body)

                self._write(body, age=120.001)
                with mock.patch.object(
                        embedding_store.time, "time", return_value=1000.0):
                    with common.EmbeddingPublishLock(self.target, timeout=0):
                        self.assertNotEqual(self.path.read_bytes(), body)
                self.assertFalse(self.path.exists())

                self._write(body, age=-3600.0)
                with mock.patch.object(
                        embedding_store.time, "time", return_value=1000.0):
                    with common.EmbeddingPublishLock(self.target, timeout=0):
                        self.assertNotEqual(self.path.read_bytes(), body)
                self.assertFalse(self.path.exists())

    def test_timeout_backoff_is_bounded_and_preserves_owner(self) -> None:
        raw = self._raw()
        self._write(raw)
        lock = common.EmbeddingPublishLock(self.target, timeout=0.02)
        with mock.patch.object(
                ownerfile, "classify_process",
                return_value=ownerfile.ProcessOwner.EXACT_LIVE), \
                mock.patch.object(
                    embedding_store.time, "monotonic",
                    side_effect=(0.0, 0.0, 0.02)), \
                mock.patch.object(embedding_store.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                    TimeoutError,
                    f"^{re.escape(f'timed out waiting for {self.path}')}$"):
                lock.__enter__()
        sleep.assert_called_once_with(0.01)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_zero_timeout_allows_one_stale_reclaim_then_stops_on_collision(
            self) -> None:
        stale = self._raw(pid=999999, token="a" * 32)
        replacement = self._raw(pid=os.getpid(), token="b" * 32)
        self._write(stale)
        create = ownerfile.create_exclusive
        create_calls = 0

        def collide_after_reclaim(path, raw, **kwargs):
            nonlocal create_calls
            create_calls += 1
            if create_calls == 2:
                path.write_bytes(replacement)
                raise FileExistsError(17, "exists", path)
            return create(path, raw, **kwargs)

        with mock.patch.object(
                ownerfile, "create_exclusive",
                side_effect=collide_after_reclaim), \
                mock.patch.object(
                    ownerfile, "classify_process",
                    side_effect=(
                        ownerfile.ProcessOwner.DEAD,
                        ownerfile.ProcessOwner.EXACT_LIVE,
                    )) as classify:
            with self.assertRaisesRegex(
                    TimeoutError,
                    f"^{re.escape(f'timed out waiting for {self.path}')}$"):
                common.EmbeddingPublishLock(
                    self.target, timeout=0).__enter__()
        self.assertEqual(create_calls, 2)
        self.assertEqual(classify.call_count, 1)
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_zero_timeout_bounds_disappearing_leaf_churn(self) -> None:
        collision = FileExistsError(17, "exists", self.path)
        missing = FileNotFoundError(2, "missing", self.path)
        with mock.patch.object(
                ownerfile, "create_exclusive",
                side_effect=collision) as create, \
                mock.patch.object(
                    ownerfile, "snapshot",
                    side_effect=missing) as snapshot:
            with self.assertRaisesRegex(
                    TimeoutError,
                    f"^{re.escape(f'timed out waiting for {self.path}')}$"):
                common.EmbeddingPublishLock(
                    self.target, timeout=0).__enter__()
        self.assertEqual(create.call_count, 2)
        snapshot.assert_called_once_with(
            self.path, max_bytes=common._EMBEDDING_PUBLISH_RECORD_BYTES)
        self.assertFalse(self.path.exists())

    def test_reclaim_and_release_preserve_replacements(self) -> None:
        stale = self._raw(pid=999999)
        replacement = b'{"replacement":true}'
        self._write(stale)
        original_remove = ownerfile.remove_exact
        calls = []

        def replace_before_remove(path, expected, **kwargs):
            calls.append(kwargs)
            path.unlink()
            path.write_bytes(replacement)
            return original_remove(path, expected, **kwargs)

        with mock.patch.object(
                ownerfile, "classify_process",
                return_value=ownerfile.ProcessOwner.DEAD), \
                mock.patch.object(
                    ownerfile, "remove_exact",
                    side_effect=replace_before_remove):
            with self.assertRaises(TimeoutError):
                common.EmbeddingPublishLock(
                    self.target, timeout=0).__enter__()
        self.assertEqual(calls, [{
            "tombstone": True, "require_stable_mtime": True}])
        self.assertEqual(self.path.read_bytes(), replacement)
        self.path.unlink()

        for index, body in enumerate((replacement, None)):
            with self.subTest(release_replacement=index):
                lock = common.EmbeddingPublishLock(self.target, timeout=0)
                lock.__enter__()
                body = lock.handle.snapshot.raw if body is None else body
                mutation_denied = False
                try:
                    self.path.unlink()
                    self.path.write_bytes(body)
                except OSError as exc:
                    sharing = (
                        isinstance(exc, PermissionError)
                        or getattr(exc, "winerror", None) in (5, 32, 33))
                    if os.name != "nt" or not sharing:
                        raise
                    mutation_denied = True
                lock.__exit__(None, None, None)
                self.assertFalse(lock.owned)
                self.assertIsNone(lock.handle)
                if mutation_denied:
                    self.assertFalse(self.path.exists())
                else:
                    self.assertEqual(self.path.read_bytes(), body)
                    self.path.unlink()

    def test_release_failure_is_silent_and_clears_local_ownership(self) -> None:
        lock = common.EmbeddingPublishLock(self.target, timeout=0)
        lock.__enter__()
        handle = lock.handle

        def fail_after_close(**_kwargs):
            handle.close()
            raise OSError("sharing")

        exact_remove = ownerfile.remove_exact
        with mock.patch.object(
                handle, "release", side_effect=fail_after_close) as release, \
                mock.patch.object(
                    ownerfile, "remove_exact", wraps=exact_remove) as remove, \
                mock.patch.object(embedding_store.time, "sleep") as sleep:
            lock.__exit__(None, None, None)
        release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)
        sleep.assert_called_once_with(
            common._EMBEDDING_PUBLISH_RELEASE_DELAYS[0])
        remove.assert_called_once_with(
            self.path, handle.snapshot,
            tombstone=True, require_stable_mtime=True)
        self.assertFalse(lock.owned)
        self.assertIsNone(lock.handle)
        self.assertFalse(self.path.exists())

        lock = common.EmbeddingPublishLock(self.target, timeout=0)
        lock.__enter__()
        handle = lock.handle
        raw = self.path.read_bytes()
        with mock.patch.object(
                handle, "release", side_effect=OSError("sharing")) as release, \
                mock.patch.object(
                    ownerfile, "remove_exact", return_value=False) as remove, \
                mock.patch.object(embedding_store.time, "sleep") as sleep:
            lock.__exit__(None, None, None)
        release.assert_called_once_with(
            tombstone=True, require_stable_mtime=True)
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(delay)
             for delay in common._EMBEDDING_PUBLISH_RELEASE_DELAYS])
        self.assertEqual(
            remove.call_count,
            len(common._EMBEDDING_PUBLISH_RELEASE_DELAYS))
        for call in remove.call_args_list:
            self.assertEqual(call.args, (self.path, handle.snapshot))
            self.assertEqual(call.kwargs, {
                "tombstone": True, "require_stable_mtime": True})
        self.assertFalse(lock.owned)
        self.assertIsNone(lock.handle)
        self.assertEqual(self.path.read_bytes(), raw)
        handle.close()
        self.path.unlink()

    def test_flat_writer_refuses_v2_and_guards_both_namespaces(self) -> None:
        import numpy as np

        ids = self.root / "embeddings.ids"
        meta = self.root / "embeddings.meta"
        self.target.write_bytes(b"legacy-matrix")
        ids.write_text("legacy\n", encoding="utf-8")
        manifest = b'{"version":2,"generation":"segmented"}'
        meta.write_bytes(manifest)
        entered = []
        lock_type = common.EmbeddingPublishLock

        class TrackingLock(lock_type):
            def __enter__(self):
                result = super().__enter__()
                entered.append((self.path, self.handle is not None))
                return result

        with mock.patch.object(
                embedding_store, "EmbeddingPublishLock", TrackingLock):
            with self.assertRaisesRegex(
                    RuntimeError,
                    "^refusing to replace a segmented embedding index "
                    "with flat artifacts$"):
                common.write_embeddings(
                    ["new"], np.ones((1, 2), dtype=np.float32),
                    self.target, ids, dim=2, model_id="fixture",
                    text_hashes=["hash"])
        self.assertEqual(entered, [
            (self.root / ".embeddings.f32.publish.lock", True),
            (self.root / ".embeddings.meta.publish.lock", True),
        ])
        self.assertEqual(self.target.read_bytes(), b"legacy-matrix")
        self.assertEqual(ids.read_text(encoding="utf-8"), "legacy\n")
        self.assertEqual(meta.read_bytes(), manifest)
        self.assertFalse((self.root / ".embeddings.f32.publish.lock").exists())
        self.assertFalse((self.root / ".embeddings.meta.publish.lock").exists())

    def test_publication_guard_verify_fails_closed_outside_context(self) -> None:
        meta = self.root / "embeddings.meta"
        guard = common.EmbeddingPublicationGuard(
            meta, self.target, timeout=0)
        message = (
            f"embedding publication guard is not held: {meta}")
        with self.assertRaisesRegex(
                ownerfile.OwnershipLost, f"^{re.escape(message)}$"):
            guard.verify()
        with guard as entered:
            self.assertIs(entered, guard)
            guard.verify()
        with self.assertRaisesRegex(
                ownerfile.OwnershipLost, f"^{re.escape(message)}$"):
            guard.verify()

    def test_publication_guard_zero_timeout_is_bounded_in_process(self) -> None:
        meta = self.root / "embeddings.meta"
        claims = (
            self.root / ".embeddings.f32.publish.lock",
            self.root / ".embeddings.meta.publish.lock",
        )
        holder_ready = threading.Event()
        release_holder = threading.Event()
        holder_done = threading.Event()
        holder_errors = []

        def hold() -> None:
            try:
                with common.EmbeddingPublicationGuard(
                        meta, self.target, timeout=1):
                    holder_ready.set()
                    if not release_holder.wait(5):
                        raise TimeoutError("test holder release timed out")
            except BaseException as exc:
                holder_errors.append(exc)
            finally:
                holder_done.set()

        holder = threading.Thread(target=hold)
        holder.start()
        contender = None
        try:
            self.assertTrue(holder_ready.wait(2))
            held = {path: path.read_bytes() for path in claims}
            result = []
            contender_done = threading.Event()

            def contend() -> None:
                started = time.monotonic()
                try:
                    with common.EmbeddingPublicationGuard(
                            meta, self.target, timeout=0):
                        outcome = AssertionError("contender acquired held guard")
                except BaseException as exc:
                    outcome = exc
                result.append((outcome, time.monotonic() - started))
                contender_done.set()

            lock_type = common.EmbeddingPublishLock
            with mock.patch.object(
                    embedding_store, "EmbeddingPublishLock",
                    wraps=lock_type) as file_lock:
                contender = threading.Thread(target=contend)
                contender.start()
                self.assertTrue(contender_done.wait(1))
                contender.join(timeout=1)
                file_lock.assert_not_called()
            self.assertIsInstance(result[0][0], TimeoutError)
            self.assertLess(result[0][1], 0.5)
            self.assertEqual(
                {path: path.read_bytes() for path in claims}, held)
        finally:
            release_holder.set()
            holder_done.wait(2)
            holder.join(timeout=1)
            if contender is not None:
                contender.join(timeout=1)
        if holder_errors:
            raise holder_errors[0]
        self.assertFalse(any(path.exists() for path in claims))

        with common.EmbeddingPublicationGuard(
                meta, self.target, timeout=0) as acquired:
            acquired.verify()
            self.assertTrue(all(path.exists() for path in claims))
        self.assertFalse(any(path.exists() for path in claims))

    def test_flat_publication_seals_legacy_then_rejects_every_mixed_seam(
            self) -> None:
        import numpy as np

        ids_path = self.root / "embeddings.ids"
        hashes_path = self.target.with_suffix(".hashes")
        meta_path = self.root / "embeddings.meta"
        old_ids = ["old-a", "old-b"]
        old_matrix = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        new_ids = ["new-a", "new-b"]
        new_matrix = np.array([[5.0, 6.0], [7.0, 8.0]], dtype=np.float32)
        old_matrix.tofile(self.target)
        ids_path.write_text("\n".join(old_ids) + "\n", encoding="utf-8")
        common.write_index_meta(meta_path, dim=2, model_id="legacy")
        self.assertIsNone(common.embedding_commit_identity(
            meta_path, self.target, ids_path))

        initial_ids, initial_matrix = common.read_embeddings(
            self.target, ids_path, dim=2, meta_path=meta_path, attempts=1)
        try:
            self.assertEqual(initial_ids, old_ids)
            np.testing.assert_array_equal(initial_matrix, old_matrix)
        finally:
            common.close_embedding_matrix(initial_matrix)

        replace = common.replace_with_retry
        events = []
        meta_replacements = 0

        def assert_rejected() -> None:
            with self.assertRaises(ValueError):
                common.read_embeddings(
                    self.target, ids_path, dim=2, meta_path=meta_path,
                    attempts=1)

        def tracked_replace(src, dst, *args, **kwargs):
            nonlocal meta_replacements
            destination = Path(dst)
            if destination == meta_path:
                meta_replacements += 1
                if meta_replacements == 2:
                    assert_rejected()
                    events.append("before-final-meta")
            result = replace(src, dst, *args, **kwargs)
            if destination == meta_path and meta_replacements == 1:
                sealed_ids, sealed_matrix = common.read_embeddings(
                    self.target, ids_path, dim=2, meta_path=meta_path,
                    attempts=1)
                try:
                    self.assertEqual(sealed_ids, old_ids)
                    np.testing.assert_array_equal(sealed_matrix, old_matrix)
                    self.assertIsNotNone(common.embedding_commit_identity(
                        meta_path, self.target, ids_path))
                finally:
                    common.close_embedding_matrix(sealed_matrix)
                events.append("sealed-legacy")
            elif destination == self.target:
                assert_rejected()
                events.append("after-matrix")
            elif destination == ids_path:
                assert_rejected()
                events.append("after-ids")
            elif destination == hashes_path:
                assert_rejected()
                events.append("after-hashes")
            elif destination == meta_path:
                events.append("final-meta")
            return result

        with mock.patch.object(
                embedding_store,
                "replace_with_retry", side_effect=tracked_replace):
            common.write_embeddings(
                new_ids, new_matrix, self.target, ids_path, dim=2,
                model_id="new", text_hashes=["a" * 16, "b" * 16])

        self.assertEqual(events, [
            "sealed-legacy",
            "after-matrix",
            "after-ids",
            "after-hashes",
            "before-final-meta",
            "final-meta",
        ])
        final_ids, final_matrix = common.read_embeddings(
            self.target, ids_path, dim=2, meta_path=meta_path, attempts=1)
        try:
            self.assertEqual(final_ids, new_ids)
            np.testing.assert_array_equal(final_matrix, new_matrix)
        finally:
            common.close_embedding_matrix(final_matrix)
        final_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(final_meta["model"], "new")
        self.assertEqual(final_meta["commit"]["rows"], len(new_ids))
        self.assertEqual(
            hashes_path.read_text(encoding="utf-8"),
            f"{'a' * 16}\n{'b' * 16}\n")

    def test_invalid_legacy_barrier_survives_crash_and_next_write_recovers(
            self) -> None:
        import numpy as np

        old_matrix = np.array([[1.0, 2.0]], dtype=np.float32)
        new_ids = ["new-a", "new-b"]
        new_matrix = np.array([[3.0, 4.0], [5.0, 6.0]], dtype=np.float32)
        for label, meta_bytes in (("missing", None), ("invalid", b"{")):
            with self.subTest(label=label):
                root = self.root / label
                root.mkdir()
                target = root / "embeddings.f32"
                ids_path = root / "embeddings.ids"
                meta_path = root / "embeddings.meta"
                old_matrix.tofile(target)
                ids_path.write_text("old\n", encoding="utf-8")
                if meta_bytes is not None:
                    meta_path.write_bytes(meta_bytes)

                replace = common.replace_with_retry

                def crash_after_matrix(src, dst, *args, **kwargs):
                    result = replace(src, dst, *args, **kwargs)
                    if Path(dst) == target:
                        raise RuntimeError("simulated crash after matrix")
                    return result

                with mock.patch.object(
                        embedding_store, "replace_with_retry",
                        side_effect=crash_after_matrix):
                    with self.assertRaisesRegex(
                            RuntimeError,
                            "^simulated crash after matrix$"):
                        common.write_embeddings(
                            new_ids, new_matrix, target, ids_path, dim=2,
                            model_id="new")

                barrier = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(barrier["state"], "publishing")
                self.assertEqual(barrier["commit"]["rows"], -1)
                with self.assertRaises(ValueError):
                    common.read_embeddings(
                        target, ids_path, dim=2, meta_path=meta_path,
                        attempts=1)

                common.write_embeddings(
                    new_ids, new_matrix, target, ids_path, dim=2,
                    model_id="new")
                final_ids, final_matrix = common.read_embeddings(
                    target, ids_path, dim=2, meta_path=meta_path, attempts=1)
                try:
                    self.assertEqual(final_ids, new_ids)
                    np.testing.assert_array_equal(final_matrix, new_matrix)
                finally:
                    common.close_embedding_matrix(final_matrix)

    def test_directory_oversize_and_reparse_leaves_are_preserved(self) -> None:
        self.path.mkdir()
        with self.assertRaises(TimeoutError):
            common.EmbeddingPublishLock(self.target, timeout=0).__enter__()
        self.assertTrue(self.path.is_dir())
        self.path.rmdir()

        oversized = b"x" * (64 * 1024 + 1)
        self.path.write_bytes(oversized)
        with self.assertRaises(TimeoutError):
            common.EmbeddingPublishLock(self.target, timeout=0).__enter__()
        self.assertEqual(self.path.read_bytes(), oversized)
        self.path.unlink()

        body = self._raw(pid=999999)
        self.path.write_bytes(body)
        info = self.path.lstat()
        reparse = mock.Mock(
            st_mode=info.st_mode, st_size=info.st_size,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with mock.patch.object(Path, "lstat", return_value=reparse):
            with self.assertRaises(TimeoutError):
                common.EmbeddingPublishLock(
                    self.target, timeout=0).__enter__()
        self.assertEqual(self.path.read_bytes(), body)

    def test_symlink_leaf_does_not_touch_target(self) -> None:
        victim = self.root / "victim"
        body = self._raw(pid=999999)
        victim.write_bytes(body)
        try:
            self.path.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(TimeoutError):
            common.EmbeddingPublishLock(self.target, timeout=0).__enter__()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(victim.read_bytes(), body)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_fifo_leaf_does_not_block_or_get_replaced(self) -> None:
        os.mkfifo(self.path)
        script = """
import pathlib, sys
import common
target = pathlib.Path(sys.argv[1])
try:
    common.EmbeddingPublishLock(target, timeout=0).__enter__()
except TimeoutError:
    raise SystemExit(0)
raise SystemExit(3)
"""
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.target)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"publish lock blocked on FIFO: {stdout}\n{stderr}")
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))

    def test_real_dead_child_claim_is_reclaimed(self) -> None:
        ready = self.root / "ready"
        script = """
import os, pathlib, sys
import common
target = pathlib.Path(sys.argv[1])
lock = common.EmbeddingPublishLock(target, timeout=0)
lock.__enter__()
pathlib.Path(sys.argv[2]).write_text("ready", encoding="ascii")
os._exit(0)
"""
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.target), str(ready)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._wait_path(ready, process)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        abandoned = self.path.read_bytes()
        with common.EmbeddingPublishLock(self.target, timeout=0) as entered:
            self.assertIsInstance(entered.handle, ownerfile.Handle)
            self.assertNotEqual(self.path.read_bytes(), abandoned)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
