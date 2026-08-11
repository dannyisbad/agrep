from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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
import embedder  # noqa: E402
import ownerfile  # noqa: E402


PY_DIR = Path(__file__).resolve().parent


class DownloadClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "model"
        self.root.mkdir()
        self.payload = b"pinned-model"
        self.saved = (
            embedder.PROFILE,
            embedder.model_dir,
            embedder.MODEL_DOWNLOAD_WAIT_S,
        )
        embedder.PROFILE = {
            "id": "fixture",
            "repo": "fixture/repo",
            "revision": "a" * 40,
            "files": {
                "model.bin": (
                    len(self.payload),
                    hashlib.sha256(self.payload).hexdigest(),
                ),
            },
            "remote_dir": {},
        }
        embedder.model_dir = lambda: self.root
        embedder.MODEL_DOWNLOAD_WAIT_S = 0.5

    def tearDown(self) -> None:
        (embedder.PROFILE, embedder.model_dir,
         embedder.MODEL_DOWNLOAD_WAIT_S) = self.saved
        self.temp.cleanup()

    @property
    def claim_path(self) -> Path:
        return self.root / ".download.lock"

    def _claim_raw(
            self, *, pid: int | None = None, process_start="birth",
            at: float | None = None, token: str = "b" * 32) -> bytes:
        return json.dumps({
            "pid": os.getpid() if pid is None else pid,
            "process_start": process_start,
            "at": time.time() if at is None else at,
            "token": token,
        }, separators=(",", ":")).encode("utf-8")

    def test_claim_wire_mode_closed_descriptor_and_tolerant_release(self) -> None:
        token = "a" * 32
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(embedder.time, "time", return_value=123.25), \
                mock.patch.object(
                    embedder.secrets, "token_hex", return_value=token):
            claim = embedder._acquire_download_claim(self.root)
        expected = (
            f'{{"pid":{os.getpid()},"process_start":"birth",'
            f'"at":123.25,"token":"{token}"}}'
        ).encode("ascii")
        self.assertIsInstance(claim, ownerfile.Handle)
        self.assertIsNone(claim.fd)
        actual = ownerfile.snapshot(self.claim_path).raw
        self.assertEqual(actual, expected)
        self.assertNotIn(b"\n", actual)
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE(self.claim_path.stat().st_mode), 0o600)
        observed = ownerfile.snapshot(self.claim_path)
        changed = observed.identity[3] + 1_000_000_000
        os.utime(self.claim_path, ns=(changed, changed))
        self.assertTrue(claim.release(tombstone=True))
        self.assertFalse(self.claim_path.exists())

    def test_complete_model_never_claims(self) -> None:
        (self.root / "model.bin").write_bytes(self.payload)
        with mock.patch.object(ownerfile, "create_exclusive") as create:
            self.assertIsNone(embedder._acquire_download_claim(self.root))
        create.assert_not_called()
        self.assertFalse(self.claim_path.exists())

    def test_owner_and_malformed_age_policies(self) -> None:
        raw = self._claim_raw(at=100.0)
        observed = ownerfile.Snapshot((1, 2, len(raw), 3), 699.0, raw)
        with mock.patch.object(embedder.time, "time", return_value=700.0), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            protected, age = embedder._download_claim_state(observed)
        self.assertTrue(protected)
        self.assertEqual(age, embedder._DOWNLOAD_STALE_S)

        with mock.patch.object(embedder.time, "time", return_value=700.1), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            protected, age = embedder._download_claim_state(observed)
        self.assertTrue(protected)
        self.assertGreater(age, embedder._DOWNLOAD_STALE_S)

        with mock.patch.object(embedder.time, "time", return_value=101.0), \
                mock.patch.object(common, "pid_alive", return_value=False):
            self.assertFalse(embedder._download_claim_state(observed)[0])
        unknown = ownerfile.Snapshot(
            observed.identity, observed.mtime,
            self._claim_raw(process_start="unknown", at=100.0))
        with mock.patch.object(embedder.time, "time", return_value=101.0):
            self.assertFalse(embedder._download_claim_state(unknown)[0])

        malformed = (b"{", b"[]", b"\xff", b'{"pid":1e400}',
                     b'{"pid":1,"at":"1e400"}')
        for body in malformed:
            fresh = ownerfile.Snapshot((1, 2, len(body), 3), 99.0, body)
            with mock.patch.object(embedder.time, "time", return_value=100.999):
                self.assertTrue(embedder._download_claim_state(fresh)[0], body)
            with mock.patch.object(embedder.time, "time", return_value=101.0):
                self.assertFalse(embedder._download_claim_state(fresh)[0], body)
            future = ownerfile.Snapshot(
                (1, 2, len(body), 3), 101.0, body)
            with mock.patch.object(embedder.time, "time", return_value=100.0):
                self.assertFalse(embedder._download_claim_state(future)[0], body)

    def test_late_acquisition_gets_fresh_timestamp_after_reclaim(self) -> None:
        stale = self._claim_raw(pid=999999, at=1.0, token="c" * 32)
        self.claim_path.write_bytes(stale)
        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    embedder.secrets, "token_hex", return_value="d" * 32), \
                mock.patch.object(
                    embedder.time, "time", side_effect=(100.0, 699.0, 700.0)):
            claim = embedder._acquire_download_claim(self.root)
        current = json.loads(claim.snapshot.raw)
        self.assertEqual(current["at"], 700.0)
        self.assertEqual(current["token"], "d" * 32)
        self.assertTrue(claim.release(tombstone=True))

    def test_each_acquisition_uses_a_fresh_token(self) -> None:
        tokens = ("a" * 32, "b" * 32)
        token_values = iter(tokens)
        real_token_hex = embedder.secrets.token_hex
        records = []

        def token_hex(width: int) -> str:
            return next(token_values) if width == 16 else real_token_hex(width)

        with mock.patch.object(
                common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    embedder.secrets, "token_hex",
                    side_effect=token_hex) as token_hex_call:
            for _ in tokens:
                claim = embedder._acquire_download_claim(self.root)
                records.append(json.loads(claim.snapshot.raw))
                self.assertTrue(claim.release())
        self.assertEqual([record["token"] for record in records], list(tokens))
        self.assertEqual(
            [call for call in token_hex_call.call_args_list if call == mock.call(16)],
            [mock.call(16), mock.call(16)])

    def test_acquisition_reclaims_expired_live_and_recycled_owners(self) -> None:
        now = time.time()
        cases = (
            (self._claim_raw(process_start="birth", at=now - 600.1), "birth"),
            (self._claim_raw(process_start="old-birth", at=now), "new-birth"),
        )
        for raw, actual_birth in cases:
            self.claim_path.write_bytes(raw)
            with mock.patch.object(common, "pid_alive", return_value=True), \
                    mock.patch.object(
                        common, "process_start_identity",
                        return_value=actual_birth):
                claim = embedder._acquire_download_claim(self.root)
            self.assertNotEqual(claim.snapshot.raw, raw)
            self.assertTrue(claim.release(tombstone=True))

        raw = self._claim_raw(process_start="birth", at=now - 600.0)
        self.claim_path.write_bytes(raw)
        real_snapshot = ownerfile.snapshot

        def complete_after_snapshot(path: Path, **kwargs):
            observed = real_snapshot(path, **kwargs)
            (self.root / "model.bin").write_bytes(self.payload)
            return observed

        with mock.patch.object(embedder.time, "time", return_value=now), \
                mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    ownerfile, "snapshot",
                    side_effect=complete_after_snapshot), \
                mock.patch.object(ownerfile, "remove_exact") as remove:
            self.assertIsNone(embedder._acquire_download_claim(self.root))
        remove.assert_not_called()
        self.assertEqual(self.claim_path.read_bytes(), raw)
        self.claim_path.unlink()
        (self.root / "model.bin").unlink()

    def test_repeated_reclaims_poll_before_retrying(self) -> None:
        raw = self._claim_raw(pid=999999, at=1.0)
        create_calls = 0

        def collide(path: Path, *_args, **_kwargs):
            nonlocal create_calls
            create_calls += 1
            path.write_bytes(raw)
            raise FileExistsError(path)

        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    ownerfile, "create_exclusive", side_effect=collide), \
                mock.patch.object(
                    embedder.time, "monotonic",
                    side_effect=(0.0, 0.0, 0.0, 0.0, 1.0)), \
                mock.patch.object(embedder.time, "sleep") as sleep, \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "^timed out waiting for another model download$"):
            embedder._acquire_download_claim(self.root)
        self.assertEqual(create_calls, 2)
        sleep.assert_any_call(embedder._DOWNLOAD_POLL_S)
        self.assertFalse(self.claim_path.exists())

    def test_peer_completion_at_deadline_wins_without_claiming(self) -> None:
        calls = 0

        def clock() -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 0.0
            (self.root / "model.bin").write_bytes(self.payload)
            return 1.0

        embedder.MODEL_DOWNLOAD_WAIT_S = 0.5
        with mock.patch.object(
                embedder.time, "monotonic", side_effect=clock), \
                mock.patch.object(ownerfile, "create_exclusive") as create:
            self.assertIsNone(embedder._acquire_download_claim(self.root))
        create.assert_not_called()
        self.assertFalse(self.claim_path.exists())

    def test_malformed_grace_and_hostile_entries_fail_safely(self) -> None:
        embedder.MODEL_DOWNLOAD_WAIT_S = 0.06
        self.claim_path.write_bytes(b"{")
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable,
                "^timed out waiting for another model download$"):
            embedder._acquire_download_claim(self.root)
        self.assertEqual(self.claim_path.read_bytes(), b"{")

        old = time.time() - 2.1
        os.utime(self.claim_path, (old, old))
        embedder.MODEL_DOWNLOAD_WAIT_S = 0.5
        claim = embedder._acquire_download_claim(self.root)
        self.assertTrue(claim.release(tombstone=True))

        self.claim_path.mkdir()
        embedder.MODEL_DOWNLOAD_WAIT_S = 10.0
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable,
                "^could not claim model download: .*not a plain regular file"):
            embedder._acquire_download_claim(self.root)
        self.assertTrue(self.claim_path.is_dir())
        self.claim_path.rmdir()

        self.claim_path.write_bytes(b"x" * (embedder._DOWNLOAD_CLAIM_BYTES + 1))
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable,
                "^could not claim model download: .*exceeds 4096 bytes"):
            embedder._acquire_download_claim(self.root)
        self.assertEqual(
            self.claim_path.stat().st_size,
            embedder._DOWNLOAD_CLAIM_BYTES + 1)
        self.claim_path.unlink()

        self.claim_path.write_bytes(b"claim")
        info = self.claim_path.lstat()
        reparse = mock.Mock(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with mock.patch.object(Path, "lstat", return_value=reparse), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "^could not claim model download: .*not a plain regular file"):
            embedder._acquire_download_claim(self.root)
        self.assertEqual(self.claim_path.read_bytes(), b"claim")
        self.claim_path.unlink()

        target = self.root / "saved-claim"
        target.write_bytes(b"do-not-touch")
        try:
            self.claim_path.symlink_to(target)
        except OSError:
            return
        with self.assertRaisesRegex(
                embedder.EmbedderUnavailable,
                "^could not claim model download: .*not a plain regular file"):
            embedder._acquire_download_claim(self.root)
        self.assertEqual(target.read_bytes(), b"do-not-touch")
        self.assertTrue(self.claim_path.is_symlink())

    def test_reclaim_and_release_preserve_aba_replacements(self) -> None:
        stale = self._claim_raw(pid=999999, at=1.0, token="e" * 32)
        self.claim_path.write_bytes(stale)
        replacement = self.root / "replacement.lock"
        real_remove = ownerfile.remove_exact
        calls = []

        def replace_then_remove(path: Path, expected, **kwargs):
            calls.append(kwargs)
            replacement.write_bytes(stale)
            os.replace(replacement, path)
            (self.root / "model.bin").write_bytes(self.payload)
            return real_remove(path, expected, **kwargs)

        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    ownerfile, "remove_exact", side_effect=replace_then_remove):
            self.assertIsNone(embedder._acquire_download_claim(self.root))
        self.assertEqual(self.claim_path.read_bytes(), stale)
        self.assertTrue(calls[0]["tombstone"])
        self.claim_path.unlink()
        (self.root / "model.bin").unlink()

        for body in (b"replacement", None):
            claim = embedder._acquire_download_claim(self.root)
            replacement_body = claim.snapshot.raw if body is None else body
            replaced = False

            def late_replace(path: Path, expected, **kwargs):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    replacement.write_bytes(replacement_body)
                    os.replace(replacement, path)
                return real_remove(path, expected, **kwargs)

            with mock.patch.object(
                    ownerfile, "remove_exact", side_effect=late_replace):
                self.assertFalse(claim.release(tombstone=True))
            self.assertEqual(self.claim_path.read_bytes(), replacement_body)
            self.claim_path.unlink()

    def test_post_create_and_mid_download_ownership_loss_abort(self) -> None:
        orphan = self.root / ".old.part"
        orphan.write_bytes(b"partial")
        fetch = mock.Mock()

        def lose_after_create(path: Path, *_args, **_kwargs):
            path.write_bytes(b"replacement")
            raise ownerfile.OwnershipLost("claim replaced")

        with mock.patch.object(
                ownerfile, "create_exclusive", side_effect=lose_after_create), \
                mock.patch.object(embedder, "_fetch_pinned", fetch), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "^could not claim model download: claim replaced$") as failure:
            embedder.ensure_model()
        self.assertIsInstance(failure.exception.__cause__, ownerfile.OwnershipLost)
        fetch.assert_not_called()
        self.assertEqual(orphan.read_bytes(), b"partial")
        self.assertEqual(self.claim_path.read_bytes(), b"replacement")
        self.claim_path.unlink()
        orphan.unlink()

        replacement = self.root / "replacement.lock"

        def replace_during_fetch(_url: str, part: Path, _size: int):
            part.write_bytes(self.payload)
            replacement.write_bytes(b"new-owner")
            os.replace(replacement, self.claim_path)

        with mock.patch.object(
                embedder, "_fetch_pinned", side_effect=replace_during_fetch), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download ownership lost"):
            embedder.ensure_model()
        self.assertFalse((self.root / "model.bin").exists())
        self.assertEqual(self.claim_path.read_bytes(), b"new-owner")
        self.assertEqual(list(self.root.glob(".*.part")), [])

    def test_post_acquire_loss_preserves_stale_partial(self) -> None:
        orphan = self.root / ".orphan.part"
        orphan.write_bytes(b"partial")
        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        replacement = self.root / "replacement.lock"
        replacement.write_bytes(b"next-owner")
        os.replace(replacement, self.claim_path)
        with mock.patch.object(
                embedder, "_acquire_download_claim", return_value=claim), \
                mock.patch.object(embedder, "_fetch_pinned") as fetch, \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download ownership lost"):
            embedder.ensure_model()
        fetch.assert_not_called()
        self.assertEqual(orphan.read_bytes(), b"partial")
        self.assertEqual(self.claim_path.read_bytes(), b"next-owner")

    def test_post_acquire_loss_stops_before_any_download_work(self) -> None:
        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        replacement = self.root / "replacement.lock"
        replacement.write_bytes(b"next-owner")
        os.replace(replacement, self.claim_path)
        with mock.patch.object(
                embedder, "_acquire_download_claim", return_value=claim), \
                mock.patch.object(embedder, "_fetch_pinned") as fetch, \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download ownership lost"):
            embedder.ensure_model()
        fetch.assert_not_called()
        self.assertEqual(list(self.root.glob(".*.part")), [])
        self.assertEqual(self.claim_path.read_bytes(), b"next-owner")

    def test_peer_completion_hashes_each_unchanged_file_once(self) -> None:
        payload_b = b"second-model"
        embedder.PROFILE["files"] = {
            "model.bin": (
                len(self.payload), hashlib.sha256(self.payload).hexdigest()),
            "tokenizer.json": (
                len(payload_b), hashlib.sha256(payload_b).hexdigest()),
        }
        (self.root / "model.bin").write_bytes(self.payload)
        raw = self._claim_raw(process_start="birth")
        owner = ownerfile.create_exclusive(self.claim_path, raw)
        hashes = 0
        real_hash = embedder._sha256_fd
        result = []
        errors = []
        observed_twice = threading.Event()
        snapshots = 0
        real_snapshot = ownerfile.snapshot

        def count_hash(fd: int, expected_size: int):
            nonlocal hashes
            hashes += 1
            return real_hash(fd, expected_size)

        def count_snapshot(path: Path, **kwargs):
            nonlocal snapshots
            value = real_snapshot(path, **kwargs)
            if path == self.claim_path:
                snapshots += 1
                if snapshots >= 2:
                    observed_twice.set()
            return value

        def wait_for_peer():
            try:
                result.append(embedder._acquire_download_claim(self.root))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    ownerfile, "snapshot", side_effect=count_snapshot), \
                mock.patch.object(
                    embedder, "_sha256_fd", side_effect=count_hash):
            thread = threading.Thread(target=wait_for_peer)
            thread.start()
            self.assertTrue(observed_twice.wait(timeout=2.0))
            (self.root / "tokenizer.json").write_bytes(payload_b)
            thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [None])
        self.assertEqual(hashes, 2)
        self.assertTrue(self.claim_path.exists())
        self.assertTrue(owner.release(tombstone=True))

    def test_failed_reverification_evicts_cached_file_stamp(self) -> None:
        payload_b = b"second-model"
        embedder.PROFILE["files"] = {
            "model.bin": (
                len(self.payload), hashlib.sha256(self.payload).hexdigest()),
            "tokenizer.json": (
                len(payload_b), hashlib.sha256(payload_b).hexdigest()),
        }
        raw = self._claim_raw(process_start="birth")
        owner = ownerfile.create_exclusive(self.claim_path, raw)
        old_stamp = (1, 2, len(self.payload), 3, 4)
        new_stamp = (1, 2, len(self.payload), 5, 6)
        stamp_calls = 0
        verify_calls = 0

        def artifact_stamp(path: Path, _size: int):
            nonlocal stamp_calls
            if path.name != "model.bin":
                return None
            stamp_calls += 1
            return old_stamp if stamp_calls != 2 else new_stamp

        def verified_stamp(_path: Path, _size: int, _sha: str):
            nonlocal verify_calls
            verify_calls += 1
            return old_stamp if verify_calls == 1 else None

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    embedder, "_artifact_stamp", side_effect=artifact_stamp), \
                mock.patch.object(
                    embedder, "_verified_file_stamp",
                    side_effect=verified_stamp), \
                mock.patch.object(
                    embedder.time, "monotonic",
                    side_effect=(0.0, 0.0, 0.0, 0.0, 1.0)), \
                mock.patch.object(embedder.time, "sleep"), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "^timed out waiting for another model download$"):
            embedder._acquire_download_claim(self.root)
        self.assertGreaterEqual(verify_calls, 3)
        self.assertTrue(owner.release(tombstone=True))

    def test_hash_reader_is_bounded_to_the_pinned_size(self) -> None:
        path = self.root / "growing.bin"
        path.write_bytes(self.payload + b"extra")
        fd = os.open(path, os.O_RDONLY)
        try:
            self.assertIsNone(embedder._sha256_fd(fd, len(self.payload)))
        finally:
            os.close(fd)

        path.write_bytes(self.payload[:-1])
        fd = os.open(path, os.O_RDONLY)
        try:
            self.assertIsNone(embedder._sha256_fd(fd, len(self.payload)))
        finally:
            os.close(fd)

    def test_partial_and_publish_retries_are_bounded(self) -> None:
        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        part = self.root / ".retry.part"
        part.write_bytes(self.payload)
        real_unlink = Path.unlink
        unlink_calls = 0

        def flaky_unlink(path: Path, *args, **kwargs):
            nonlocal unlink_calls
            unlink_calls += 1
            if path == part and unlink_calls == 1:
                raise PermissionError("sharing violation")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=flaky_unlink):
            self.assertTrue(embedder._discard_download_part(part, claim))
        self.assertEqual(unlink_calls, 2)

        part.write_bytes(self.payload)
        with mock.patch.object(
                Path, "unlink", autospec=True,
                side_effect=PermissionError("sharing violation")) as unlink:
            self.assertFalse(embedder._discard_download_part(part))
        self.assertEqual(
            unlink.call_count, len(embedder._DOWNLOAD_FS_RETRY_DELAYS))
        self.assertTrue(part.exists())
        part.unlink()

        part.write_bytes(self.payload)
        target = self.root / "published.bin"
        real_replace = os.replace
        replace_calls = 0

        def flaky_replace(source, destination):
            nonlocal replace_calls
            if Path(destination) == target:
                replace_calls += 1
                if replace_calls == 1:
                    raise PermissionError("sharing violation")
            return real_replace(source, destination)

        with mock.patch.object(embedder.os, "replace", side_effect=flaky_replace):
            embedder._publish_download_part(part, target, claim)
        self.assertEqual(replace_calls, 2)
        self.assertEqual(target.read_bytes(), self.payload)
        target.unlink()

        part.write_bytes(self.payload)
        with mock.patch.object(
                embedder.os, "replace",
                side_effect=PermissionError("sharing violation")) as replace, \
                self.assertRaisesRegex(PermissionError, "sharing violation"):
            embedder._publish_download_part(part, target, claim)
        self.assertEqual(
            replace.call_count, len(embedder._DOWNLOAD_FS_RETRY_DELAYS))
        self.assertTrue(part.exists())
        part.unlink()
        self.assertTrue(claim.release(tombstone=True))

    def test_retry_rechecks_claim_before_cleanup_and_publication(self) -> None:
        orphan = self.root / ".stale.part"
        orphan.write_bytes(b"partial")
        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        real_unlink = Path.unlink
        mutated = False

        def lose_during_unlink(path: Path, *args, **kwargs):
            nonlocal mutated
            if path == orphan and not mutated:
                mutated = True
                self.claim_path.write_bytes(b"next-owner")
                raise PermissionError("sharing violation")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
                embedder, "_acquire_download_claim", return_value=claim), \
                mock.patch.object(
                    Path, "unlink", autospec=True,
                    side_effect=lose_during_unlink), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download ownership lost"):
            embedder.ensure_model()
        self.assertEqual(orphan.read_bytes(), b"partial")
        self.assertEqual(self.claim_path.read_bytes(), b"next-owner")
        self.claim_path.unlink()
        orphan.unlink()

        replacement = self.root / "replacement.lock"
        publish_calls = 0

        def fetch(_url: str, part: Path, _size: int):
            part.write_bytes(self.payload)

        real_replace = os.replace

        def replace_once(source, destination):
            nonlocal publish_calls
            if Path(destination).name == "model.bin":
                publish_calls += 1
                replacement.write_bytes(b"next-owner")
                real_replace(replacement, self.claim_path)
                raise PermissionError("sharing violation")
            return real_replace(source, destination)

        with mock.patch.object(embedder, "_fetch_pinned", side_effect=fetch), \
                mock.patch.object(embedder.os, "replace",
                                  side_effect=replace_once), \
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download ownership lost"):
            embedder.ensure_model()
        self.assertEqual(publish_calls, 1)
        self.assertFalse((self.root / "model.bin").exists())
        self.assertEqual(self.claim_path.read_bytes(), b"next-owner")
        self.assertEqual(list(self.root.glob(".*.part")), [])

    def test_release_failure_is_silent_and_preserves_the_claim(self) -> None:
        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        with mock.patch.object(claim, "release", return_value=False), \
                mock.patch.object(common, "log") as log:
            embedder._release_download_claim(claim)
        log.assert_not_called()
        self.assertTrue(self.claim_path.exists())
        with mock.patch.object(
                claim, "release", side_effect=OSError("close failed")), \
                mock.patch.object(common, "log") as log:
            embedder._release_download_claim(claim)
        log.assert_not_called()
        self.assertTrue(self.claim_path.exists())
        self.assertTrue(claim.release(tombstone=True))

        claim = ownerfile.create_exclusive(
            self.claim_path, self._claim_raw(process_start="birth"))
        self.claim_path.write_bytes(b"replacement")
        with mock.patch.object(claim, "release", return_value=False), \
                mock.patch.object(common, "log") as log:
            embedder._release_download_claim(claim)
        log.assert_not_called()
        self.assertEqual(self.claim_path.read_bytes(), b"replacement")
        self.claim_path.unlink()

    def test_crashed_process_claim_is_reclaimed_with_its_partial(self) -> None:
        ready = self.root / "child-ready"
        script = """
import json, os, pathlib, sys, time
import common, ownerfile
root, ready = map(pathlib.Path, sys.argv[1:])
pid = os.getpid()
raw = json.dumps({
    "pid": pid, "process_start": common.process_start_identity(pid),
    "at": time.time(), "token": "c" * 32,
}, separators=(",", ":")).encode("utf-8")
ownerfile.create_exclusive(root / ".download.lock", raw)
(root / ".crashed.part").write_bytes(b"partial")
ready.write_text("ready", encoding="ascii")
os._exit(0)
"""
        env = {**os.environ, "PYTHONPATH": str(PY_DIR)}
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(ready)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertTrue(ready.exists())
        self.assertTrue(self.claim_path.exists())

        def fetch(_url: str, part: Path, _size: int):
            part.write_bytes(self.payload)

        with mock.patch.object(
                embedder, "_fetch_pinned", side_effect=fetch) as download:
            self.assertEqual(embedder.ensure_model(), self.root)
        download.assert_called_once()
        self.assertEqual((self.root / "model.bin").read_bytes(), self.payload)
        self.assertFalse(self.claim_path.exists())
        self.assertEqual(list(self.root.glob(".*.part")), [])

    def test_file_verification_rejects_links_and_fifo_swap(self) -> None:
        target = self.root / "target.bin"
        target.write_bytes(self.payload)
        self.assertTrue(embedder._file_ok(
            target, len(self.payload), hashlib.sha256(self.payload).hexdigest()))
        self.assertFalse(embedder._file_ok(
            target, len(self.payload), "0" * 64))
        stamp = embedder._artifact_stamp(target, len(self.payload))
        path_view = (*stamp[:4], stamp[4] - 1)
        with mock.patch.object(
                embedder, "_artifact_stamp",
                side_effect=(path_view, path_view)):
            self.assertEqual(
                embedder._verified_file_stamp(
                    target, len(self.payload),
                    hashlib.sha256(self.payload).hexdigest()),
                path_view)
        changed_path_view = (*path_view[:4], path_view[4] + 1)
        with mock.patch.object(
                embedder, "_artifact_stamp",
                side_effect=(path_view, changed_path_view)):
            self.assertIsNone(embedder._verified_file_stamp(
                target, len(self.payload),
                hashlib.sha256(self.payload).hexdigest()))
        with mock.patch.object(
                embedder, "_artifact_stamp",
                side_effect=(stamp, None)):
            self.assertFalse(embedder._file_ok(
                target, len(self.payload),
                hashlib.sha256(self.payload).hexdigest()))

        link = self.root / "link.bin"
        try:
            link.symlink_to(target)
        except OSError:
            pass
        else:
            self.assertFalse(embedder._file_ok(
                link, len(self.payload),
                hashlib.sha256(self.payload).hexdigest()))

        info = target.lstat()
        reparse = mock.Mock(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_file_attributes=getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        with mock.patch.object(Path, "lstat", return_value=reparse):
            self.assertFalse(embedder._file_ok(
                target, len(self.payload),
                hashlib.sha256(self.payload).hexdigest()))

        if not hasattr(os, "mkfifo"):
            return
        path = self.root / "fifo-race.bin"
        fifo = self.root / "fifo"
        path.write_bytes(self.payload)
        os.mkfifo(fifo)
        real_open = embedder.os.open

        def swap_then_open(target_path, flags, *args):
            path.unlink()
            os.replace(fifo, path)
            return real_open(target_path, flags, *args)

        with mock.patch.object(
                embedder.os, "open", side_effect=swap_then_open):
            self.assertFalse(embedder._file_ok(
                path, len(self.payload),
                hashlib.sha256(self.payload).hexdigest()))
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))


if __name__ == "__main__":
    unittest.main()
