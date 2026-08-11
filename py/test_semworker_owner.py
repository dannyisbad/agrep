from __future__ import annotations

import json
import os
from pathlib import Path
import stat
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


class SemanticWorkerOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agrep-semworker-owner-")
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
    def _owner_raw(
            *, pid: int = 4242, process_start: object = "birth",
            nonce: str = "a" * 32, tree_bound: bool = True,
            include_start: bool = True) -> bytes:
        record = {
            "pid": pid,
            "started_at": 123.5,
        }
        if include_start:
            record["process_start"] = process_start
        record["nonce"] = nonce
        record["tree_bound"] = tree_bound
        record["named_job"] = bool(tree_bound and common.WIN)
        return json.dumps(record, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _descriptor_record(
            *, pid: int = 4242, process_start: str = "birth",
            owner_nonce: str = "a" * 32) -> dict:
        return {
            "version": semworker.PROTOCOL,
            "pid": pid,
            "port": 31337,
            "token": "b" * 64,
            "started_at": 123.5,
            "process_start": process_start,
            "build_id": semworker.WORKER_BUILD_ID,
            "capabilities": list(semworker.CAPABILITIES),
            "owner": "disposable",
            "tree_bound": True,
            "named_job": common.WIN,
            "owner_nonce": owner_nonce,
        }

    def _write_owner(
            self, raw: bytes, *, age: float = 0.0,
            now: float = 100.0) -> None:
        path = semworker.worker_lock_path()
        path.write_bytes(raw)
        changed = now - age
        os.utime(path, (changed, changed))

    def _acquire(
            self, tree_bound: bool = True) -> ownerfile.Handle | None:
        handle = semworker._acquire_worker_lock(tree_bound=tree_bound)
        if handle is not None:
            self.handles.append(handle)
        return handle

    @staticmethod
    def _release(handle: ownerfile.Handle) -> bool:
        return handle.release(
            tombstone=True, require_stable_mtime=True)

    def test_wire_mode_retained_handle_and_exact_release(self) -> None:
        create = ownerfile.create_exclusive
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker.secrets, "token_hex", return_value="c" * 32), \
                mock.patch.object(semworker.time, "time", return_value=123.5), \
                mock.patch.object(
                    ownerfile, "create_exclusive", wraps=create) as create_call:
            handle = self._acquire(tree_bound=True)
        self.assertIsInstance(handle, ownerfile.Handle)
        self.assertIsNotNone(handle.fd)
        expected = self._owner_raw(
            pid=os.getpid(), process_start="birth", nonce="c" * 32)
        self.assertEqual(handle.snapshot.raw, expected)
        self.assertEqual(semworker.worker_lock_path().read_bytes(), expected)
        self.assertEqual(json.loads(expected), {
            "pid": os.getpid(),
            "started_at": 123.5,
            "process_start": "birth",
            "nonce": "c" * 32,
            "tree_bound": True,
            "named_job": common.WIN,
        })
        if os.name != "nt":
            mode = stat.S_IMODE(semworker.worker_lock_path().stat().st_mode)
            self.assertEqual(mode, 0o600)
        create_call.assert_called_once_with(
            semworker.worker_lock_path(), expected,
            mode=0o600, retain_fd=True)
        descriptor = handle.fd
        self.assertTrue(self._release(handle))
        self.assertFalse(semworker.worker_lock_path().exists())
        self.assertIsNone(handle.fd)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_missing_self_birth_never_publishes_owner(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value=None):
            self.assertIsNone(self._acquire())
        self.assertFalse(semworker.worker_lock_path().exists())

    def test_live_protocol9_owner_blocks_new_runtime_owner(self) -> None:
        semworker.legacy_worker_lock_path().write_bytes(self._owner_raw())
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=lambda pid: (
                        "birth" if pid == 4242 else "self-birth")):
            self.assertEqual(
                semworker._inspect_legacy_worker_lock().state,
                semworker._WorkerOwnerState.EXACT)
            self.assertIsNone(self._acquire())
        self.assertFalse(semworker.worker_lock_path().exists())

    def test_proven_dead_protocol9_owner_does_not_block_readonly_fallback(
            self) -> None:
        observed = ownerfile.Snapshot(
            (1, 2, 3, 4), 1.0, self._owner_raw())
        reclaimable = semworker._WorkerOwner(
            semworker._WorkerOwnerState.RECLAIMABLE,
            json.loads(observed.raw), observed)
        with mock.patch.object(
                semworker, "_inspect_legacy_worker_lock",
                return_value=reclaimable), \
                mock.patch.object(
                    semworker, "_discard_record", return_value=False) as discard:
            self.assertTrue(semworker._legacy_worker_lock_clear_for_acquire())
        discard.assert_called_once_with(
            semworker.legacy_worker_lock_path(), observed)

    def test_incompatible_descriptor_binds_protocol9_owner(self) -> None:
        semworker.legacy_worker_lock_path().write_bytes(self._owner_raw())
        descriptor = self._descriptor_record()
        descriptor["version"] = semworker.PROTOCOL - 1
        descriptor["build_id"] = "old-build"
        semworker.descriptor_path().write_text(
            json.dumps(descriptor, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=lambda pid: (
                        "birth" if pid == 4242 else "self-birth")):
            observed = semworker._inspect_descriptor()
        self.assertIs(observed.state, semworker._DescriptorState.INCOMPATIBLE)
        self.assertTrue(observed.bound_owner)

    def test_missing_self_birth_never_publishes_start_claim(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value=None):
            self.assertIsNone(semworker._acquire_start_claim())
        self.assertFalse(semworker.start_claim_path().exists())

    def test_exact_live_owner_is_protected_even_when_ancient(self) -> None:
        raw = self._owner_raw()
        self._write_owner(raw, age=24 * 3600)

        def identity(pid: int) -> str:
            return "birth" if pid == 4242 else "self-birth"

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", side_effect=identity):
            inspected = semworker._inspect_worker_lock()
            contender = self._acquire()
        self.assertIs(inspected.state, semworker._WorkerOwnerState.EXACT)
        self.assertIsNone(contender)
        self.assertEqual(semworker.worker_lock_path().read_bytes(), raw)

    def test_dead_and_reused_reclaim_but_unverifiable_protects(self) -> None:
        cases = (
            ("dead", False, None, semworker._WorkerOwnerState.RECLAIMABLE, True),
            ("reused", True, "other", semworker._WorkerOwnerState.RECLAIMABLE, True),
            ("unverifiable", True, None,
             semworker._WorkerOwnerState.UNVERIFIABLE, False),
        )
        for label, alive, actual, state, acquired in cases:
            with self.subTest(label=label):
                original = self._owner_raw()
                self._write_owner(original, age=3600)

                def identity(pid: int, found=actual):
                    return found if pid == 4242 else "self-birth"

                with mock.patch.object(
                        common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "_process_group_active",
                            return_value=False), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=identity):
                    inspected = semworker._inspect_worker_lock()
                    handle = self._acquire()
                self.assertIs(inspected.state, state)
                self.assertEqual(handle is not None, acquired)
                if handle is None:
                    self.assertEqual(
                        semworker.worker_lock_path().read_bytes(), original)
                else:
                    self.assertNotEqual(handle.snapshot.raw, original)
                    self.assertTrue(self._release(handle))
                semworker.worker_lock_path().unlink(missing_ok=True)

    def test_dead_bound_owner_with_live_group_is_not_reclaimed(self) -> None:
        original = self._owner_raw()
        self._write_owner(original, age=3600)
        with mock.patch.object(common, "WIN", False), \
                mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "_process_group_active", return_value=True):
            inspected = semworker._inspect_worker_lock()
            handle = self._acquire()
        self.assertIs(
            inspected.state, semworker._WorkerOwnerState.ORPHANED_GROUP)
        self.assertIsNone(handle)
        self.assertEqual(semworker.worker_lock_path().read_bytes(), original)

    def test_missing_birth_records_protect_live_pids_and_reclaim_dead_ones(
            self) -> None:
        cases = (
            ("absent", self._owner_raw(include_start=False)),
            ("null", self._owner_raw(process_start=None)),
            ("empty", self._owner_raw(process_start="")),
            ("unknown", self._owner_raw(process_start="unknown")),
        )
        for label, original in cases:
            with self.subTest(label=label):
                self._write_owner(original, age=0.0)
                with mock.patch.object(common, "pid_alive", return_value=True), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value="self-birth"):
                    live = semworker._inspect_worker_lock()
                    contender = self._acquire()
                self.assertIs(
                    live.state, semworker._WorkerOwnerState.UNVERIFIABLE)
                self.assertIsNone(contender)
                self.assertEqual(
                    semworker.worker_lock_path().read_bytes(), original)

                with mock.patch.object(common, "pid_alive", return_value=False), \
                        mock.patch.object(
                            common, "_process_group_active",
                            return_value=False), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value="self-birth"):
                    dead = semworker._inspect_worker_lock()
                    handle = self._acquire()
                self.assertIs(
                    dead.state, semworker._WorkerOwnerState.RECLAIMABLE)
                self.assertIsInstance(handle, ownerfile.Handle)
                self.assertNotEqual(handle.snapshot.raw, original)
                self.assertTrue(self._release(handle))

    def test_fresh_unverifiable_start_claim_is_protected_until_grace_expires(
            self) -> None:
        claim = json.dumps({
            "pid": 4242,
            "at": 100.0,
            "process_start": "birth",
            "nonce": "a" * 32,
        }, separators=(",", ":")).encode("utf-8")
        semworker.start_claim_path().write_bytes(claim)

        def identity(pid: int) -> str | None:
            return None if pid == 4242 else "self-birth"

        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", side_effect=identity):
            self.assertIsNone(semworker._acquire_start_claim())
        self.assertEqual(semworker.start_claim_path().read_bytes(), claim)

        stale = {**json.loads(claim), "at": 93.999}
        semworker.start_claim_path().write_text(
            json.dumps(stale, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", side_effect=identity):
            acquired = semworker._acquire_start_claim()
        self.assertIsInstance(acquired, ownerfile.Handle)
        self.handles.append(acquired)
        self.assertTrue(acquired.release(
            tombstone=True, require_stable_mtime=True))

    def test_future_start_claim_and_malformed_owner_do_not_extend_lease(self) -> None:
        claim = json.dumps({
            "pid": 4242,
            "at": 200.0,
            "process_start": "birth",
            "nonce": "a" * 32,
        }, separators=(",", ":")).encode("utf-8")
        semworker.start_claim_path().write_bytes(claim)
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            acquired = semworker._acquire_start_claim()
        self.assertIsInstance(acquired, ownerfile.Handle)
        self.handles.append(acquired)
        self.assertTrue(acquired.release(
            tombstone=True, require_stable_mtime=True))

        self._write_owner(b"{", age=-3600.0)
        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            acquired = self._acquire()
        self.assertIsInstance(acquired, ownerfile.Handle)
        self.assertTrue(self._release(acquired))

    def test_malformed_records_use_the_exact_six_second_policy(self) -> None:
        malformed = (b"", b"{", b"[]", b"null", b"\"text\"", b"\xff")
        for body in malformed:
            with self.subTest(body=body):
                for age in (5.999, 6.0):
                    self._write_owner(body, age=age)
                    with mock.patch.object(
                            semworker.time, "time", return_value=100.0), \
                            mock.patch.object(
                                common, "process_start_identity",
                                return_value="self-birth"):
                        inspected = semworker._inspect_worker_lock()
                        contender = self._acquire()
                    self.assertIs(
                        inspected.state,
                        semworker._WorkerOwnerState.MALFORMED_FRESH)
                    self.assertIsNone(contender)
                    self.assertEqual(
                        semworker.worker_lock_path().read_bytes(), body)

                self._write_owner(body, age=6.001)
                with mock.patch.object(
                        semworker.time, "time", return_value=100.0), \
                        mock.patch.object(
                            common, "process_start_identity",
                            return_value="self-birth"):
                    inspected = semworker._inspect_worker_lock()
                    handle = self._acquire()
                self.assertIs(
                    inspected.state,
                    semworker._WorkerOwnerState.MALFORMED_STALE)
                self.assertIsInstance(handle, ownerfile.Handle)
                self.assertTrue(self._release(handle))

    @unittest.skipIf(os.name == "nt", "symlink creation is not portable on Windows")
    def test_symlink_owner_fails_closed(self) -> None:
        target = self.root / "target"
        target.write_bytes(b"foreign")
        semworker.worker_lock_path().symlink_to(target)
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"), \
                mock.patch.object(
                    semworker, "_discard_worker_record") as discard:
            inspected = semworker._inspect_worker_lock()
            contender = self._acquire()
        self.assertIs(inspected.state, semworker._WorkerOwnerState.BLOCKED)
        self.assertIsNone(contender)
        discard.assert_not_called()
        self.assertTrue(semworker.worker_lock_path().is_symlink())
        self.assertEqual(target.read_bytes(), b"foreign")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO owner fixture is POSIX-only")
    def test_fifo_owner_fails_closed_without_opening_it(self) -> None:
        os.mkfifo(semworker.worker_lock_path())
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"), \
                mock.patch.object(
                    semworker, "_discard_worker_record") as discard:
            inspected = semworker._inspect_worker_lock()
            contender = self._acquire()
        self.assertIs(inspected.state, semworker._WorkerOwnerState.BLOCKED)
        self.assertIsNone(contender)
        discard.assert_not_called()
        self.assertTrue(stat.S_ISFIFO(
            semworker.worker_lock_path().lstat().st_mode))

    def test_oversized_owner_fails_closed(self) -> None:
        original = b"x" * (semworker._WORKER_OWNER_BYTES + 1)
        semworker.worker_lock_path().write_bytes(original)
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"), \
                mock.patch.object(
                    semworker, "_discard_worker_record") as discard:
            inspected = semworker._inspect_worker_lock()
            contender = self._acquire()
        self.assertIs(inspected.state, semworker._WorkerOwnerState.BLOCKED)
        self.assertIsNone(contender)
        discard.assert_not_called()
        self.assertEqual(semworker.worker_lock_path().stat().st_size, len(original))

    def test_stale_owner_reclaim_preserves_an_aba_replacement(self) -> None:
        self._write_owner(b"{", age=7.0)
        replacement = self._owner_raw(
            pid=5252, process_start="replacement", nonce="d" * 32)
        discard_exact = semworker._discard_worker_record

        def replace_then_discard(
                path: Path, observed: ownerfile.Snapshot) -> bool:
            path.unlink()
            path.write_bytes(replacement)
            return discard_exact(path, observed)

        with mock.patch.object(semworker.time, "time", return_value=100.0), \
                mock.patch.object(
                    common, "process_start_identity",
                    return_value="self-birth"), \
                mock.patch.object(
                    semworker, "_discard_worker_record",
                    side_effect=replace_then_discard) as discard:
            contender = self._acquire()
        self.assertIsNone(contender)
        discard.assert_called_once()
        self.assertEqual(
            semworker.worker_lock_path().read_bytes(), replacement)

    def test_descriptor_publication_is_private_exclusive_and_bound(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker.secrets, "token_hex", return_value="a" * 32):
            lifetime = self._acquire(tree_bound=True)
        self.assertIsInstance(lifetime, ownerfile.Handle)
        owner_nonce = json.loads(lifetime.snapshot.raw)["nonce"]
        record = self._descriptor_record(
            pid=os.getpid(), process_start="birth",
            owner_nonce=owner_nonce)
        raw = json.dumps(record, separators=(",", ":"))
        create = ownerfile.create_exclusive
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    ownerfile, "create_exclusive",
                    wraps=create) as create_call:
            snapshot = semworker._publish_descriptor(
                lifetime, raw, owner_nonce)
        self.assertEqual(snapshot.raw, raw.encode("utf-8"))
        self.assertEqual(semworker.descriptor_path().read_text(), raw)
        if os.name != "nt":
            mode = stat.S_IMODE(semworker.descriptor_path().stat().st_mode)
            self.assertEqual(mode, 0o600)
        create_call.assert_called_once_with(
            semworker.descriptor_path(), raw.encode("utf-8"), mode=0o600)
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            found = semworker._reconcile_descriptor()
        self.assertIsNotNone(found)
        self.assertEqual(found[0], record)
        self.assertEqual(found[1], snapshot)
        self.assertTrue(semworker._discard_record(
            semworker.descriptor_path(), snapshot))
        self.assertTrue(self._release(lifetime))

    def test_foreign_live_descriptor_cannot_be_overwritten(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            lifetime = self._acquire(tree_bound=True)
        self.assertIsInstance(lifetime, ownerfile.Handle)
        nonce = json.loads(lifetime.snapshot.raw)["nonce"]
        foreign = self._descriptor_record(
            pid=4242, process_start="foreign-birth",
            owner_nonce="e" * 32)
        foreign_raw = json.dumps(
            foreign, separators=(",", ":")).encode("utf-8")
        semworker.descriptor_path().write_bytes(foreign_raw)
        replacement = self._descriptor_record(
            pid=os.getpid(), process_start="self-birth",
            owner_nonce=nonce)

        def identity(pid: int) -> str:
            return "foreign-birth" if pid == 4242 else "self-birth"

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", side_effect=identity):
            with self.assertRaises(ownerfile.OwnershipLost):
                semworker._publish_descriptor(
                    lifetime,
                    json.dumps(replacement, separators=(",", ":")),
                    nonce)
        self.assertEqual(semworker.descriptor_path().read_bytes(), foreign_raw)
        self.assertTrue(self._release(lifetime))

    def test_descriptor_reclaim_preserves_an_aba_replacement(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="self-birth"):
            lifetime = self._acquire(tree_bound=True)
        self.assertIsInstance(lifetime, ownerfile.Handle)
        nonce = json.loads(lifetime.snapshot.raw)["nonce"]
        semworker.descriptor_path().write_bytes(b"{")
        old = time.time() - semworker.START_CLAIM_GRACE_S - 1.0
        os.utime(semworker.descriptor_path(), (old, old))
        replacement = json.dumps(
            self._descriptor_record(
                pid=5252, process_start="replacement",
                owner_nonce="f" * 32),
            separators=(",", ":")).encode("utf-8")
        discard_exact = semworker._discard_record

        def replace_then_discard(
                path: Path, observed: ownerfile.Snapshot) -> bool:
            path.unlink()
            path.write_bytes(replacement)
            return discard_exact(path, observed)

        record = self._descriptor_record(
            pid=os.getpid(), process_start="self-birth",
            owner_nonce=nonce)
        with mock.patch.object(
                common, "process_start_identity",
                return_value="self-birth"), \
                mock.patch.object(
                    semworker, "_discard_record",
                    side_effect=replace_then_discard):
            with self.assertRaises(ownerfile.OwnershipLost):
                semworker._publish_descriptor(
                    lifetime, json.dumps(record, separators=(",", ":")),
                    nonce)
        self.assertEqual(semworker.descriptor_path().read_bytes(), replacement)
        self.assertTrue(self._release(lifetime))

    def test_descriptor_requires_the_exact_owner_nonce(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker.secrets, "token_hex", return_value="a" * 32):
            lifetime = self._acquire(tree_bound=True)
        self.assertIsInstance(lifetime, ownerfile.Handle)
        owner_nonce = json.loads(lifetime.snapshot.raw)["nonce"]
        accepted = self._descriptor_record(
            pid=os.getpid(), process_start="birth",
            owner_nonce=owner_nonce)
        semworker.descriptor_path().write_text(
            json.dumps(accepted, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            found = semworker._reconcile_descriptor()
        self.assertIsNotNone(found)
        self.assertTrue(semworker._discard_record(
            semworker.descriptor_path(), found[1]))

        rejected = {**accepted, "owner_nonce": "f" * 32}
        semworker.descriptor_path().write_text(
            json.dumps(rejected, separators=(",", ":")), encoding="utf-8")
        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    semworker, "_terminate_worker_tree",
                    return_value=False) as terminate:
            self.assertIsNone(semworker._reconcile_descriptor())
        self.assertFalse(semworker.descriptor_path().exists())
        terminate.assert_not_called()
        self.assertTrue(self._release(lifetime))

    def test_generation_tokens_must_be_canonical_hex_strings(self) -> None:
        self.assertEqual(
            semworker._hex_token("a" * 32, 32), "a" * 32)
        for value in (b"a" * 32, int("a" * 32, 16), ["a" * 32]):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(semworker._hex_token(value, 32))

        record = self._descriptor_record(owner_nonce="a" * 32)
        self.assertFalse(semworker._descriptor_compatible({
            **record, "token": int("b" * 64, 16)}))
        self.assertFalse(semworker._descriptor_compatible({
            **record, "owner_nonce": int("a" * 32, 16)}))
        with mock.patch.object(
                semworker.http.client, "HTTPConnection") as connection:
            self.assertFalse(semworker._request_worker_stop({
                **record, "token": int("b" * 64, 16)}))
        connection.assert_not_called()

    def test_server_detects_owner_loss_and_stops(self) -> None:
        http = mock.Mock()
        http.server_address = ("127.0.0.1", 31337)
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            lifetime = self._acquire(tree_bound=True)
            with mock.patch.object(
                    common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                    mock.patch.object(
                        semworker, "_BoundedHTTPServer",
                        return_value=http):
                server = semworker.SemanticWorkerServer(
                    search_fn=lambda *_args, **_kwargs: {},
                    lifetime=lifetime)
        try:
            with mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
                self.assertTrue(server._owns_lifetime())
            self.assertTrue(self._release(lifetime))
            self.assertFalse(server._owns_lifetime())
            self.assertTrue(server.stop.is_set())
        finally:
            server.http.server_close()
            if server.descriptor_snapshot is not None:
                semworker._discard_record(
                    semworker.descriptor_path(),
                    server.descriptor_snapshot)

    def test_server_rejects_a_caller_claim_without_an_os_boundary(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            lifetime = self._acquire(tree_bound=True)
        try:
            with mock.patch.object(
                    common, "process_start_identity",
                    return_value="birth"), \
                    mock.patch.object(
                        common, "bind_descendants_to_process_lifetime",
                        return_value=False), \
                    self.assertRaises(ownerfile.OwnershipLost):
                semworker.SemanticWorkerServer(
                    search_fn=lambda *_args, **_kwargs: {},
                    lifetime=lifetime)
        finally:
            self.assertTrue(self._release(lifetime))

    def test_server_detects_same_bytes_descriptor_replacement(self) -> None:
        http = mock.Mock()
        http.server_address = ("127.0.0.1", 31337)
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            lifetime = self._acquire(tree_bound=True)
            with mock.patch.object(
                    common, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                    mock.patch.object(
                        semworker, "_BoundedHTTPServer",
                        return_value=http):
                server = semworker.SemanticWorkerServer(
                    search_fn=lambda *_args, **_kwargs: {},
                    lifetime=lifetime)
        path = semworker.descriptor_path()
        replacement_raw = path.read_bytes()
        path.unlink()
        path.write_bytes(replacement_raw)
        try:
            with mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
                self.assertFalse(server._owns_lifetime())
            self.assertTrue(server.stop.is_set())
            self.assertEqual(path.read_bytes(), replacement_raw)
        finally:
            server.http.server_close()
            path.unlink(missing_ok=True)
            self.assertTrue(self._release(lifetime))

    def test_inprocess_owner_release_and_failed_release_close_semantics(
            self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            released = semworker.acquire_inprocess_owner()
        self.assertIsInstance(released, ownerfile.Handle)
        self.handles.append(released)
        released_fd = released.fd
        self.assertFalse(json.loads(released.snapshot.raw)["tree_bound"])
        semworker.verify_inprocess_owner(released)
        semworker.finish_inprocess_owner(
            released, resources_released=True)
        self.assertFalse(semworker.worker_lock_path().exists())
        self.assertIsNone(released.fd)
        with self.assertRaises(OSError):
            os.fstat(released_fd)

        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            retained = semworker.acquire_inprocess_owner()
        self.assertIsInstance(retained, ownerfile.Handle)
        self.handles.append(retained)
        retained_fd = retained.fd
        retained_raw = retained.snapshot.raw
        semworker.finish_inprocess_owner(
            retained, resources_released=False)
        self.assertEqual(
            semworker.worker_lock_path().read_bytes(), retained_raw)
        self.assertIsNone(retained.fd)
        with self.assertRaises(OSError):
            os.fstat(retained_fd)
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            self.assertIsNone(semworker.acquire_inprocess_owner())
        self.assertTrue(self._release(retained))

    def test_inprocess_exact_release_preserves_replacement(self) -> None:
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            owner = semworker.acquire_inprocess_owner()
        self.assertIsInstance(owner, ownerfile.Handle)
        self.handles.append(owner)
        replacement = self._owner_raw(
            pid=5252, process_start="replacement", nonce="f" * 32)
        remove_exact = ownerfile.remove_exact

        def replace_before_remove(
                path: Path, expected: ownerfile.Snapshot, **kwargs) -> bool:
            path.unlink(missing_ok=True)
            path.write_bytes(replacement)
            return remove_exact(path, expected, **kwargs)

        with mock.patch.object(
                ownerfile, "remove_exact",
                side_effect=replace_before_remove):
            semworker.finish_inprocess_owner(
                owner, resources_released=True)
        self.assertIsNone(owner.fd)
        self.assertEqual(
            semworker.worker_lock_path().read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
