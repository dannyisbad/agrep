from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_support
from _test_support import isolate_data_dir

isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)

import common  # noqa: E402
import embed  # noqa: E402
import embedder  # noqa: E402
import embedding_segments  # noqa: E402
import ownerfile  # noqa: E402
import removal_fence  # noqa: E402
import semantic  # noqa: E402
import semantic_segment_compact as compact  # noqa: E402


class RemovalFenceTests(unittest.TestCase):
    def tearDown(self) -> None:
        embed._release_claim()
        compact._release_claim()
        for path in (
                removal_fence.background_removal_path(),
                removal_fence.background_removal_cooldown_path(),
                semantic.embed_claim_path(),
                semantic.compaction_claim_path(),
                indexd_runtime.INDEXD_LOCK_PATH,
                indexd_runtime._SPAWN_GUARD_PATH):
            path.unlink(missing_ok=True)

    @staticmethod
    def _claim(pid: int, process_start: str, token: str = "a" * 32) -> bytes:
        return json.dumps({
            "pid": pid,
            "process_start": process_start,
            "token": token,
        }).encode("utf-8")

    def test_finish_hands_live_fence_to_bounded_ownerless_cooldown(self) -> None:
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        self.assertTrue(
            removal_fence.background_removal_owned_by_current_process())
        self.assertTrue(removal_fence.finish_background_removal_fence(fence))
        self.assertFalse(removal_fence.background_removal_path().exists())
        self.assertFalse(
            removal_fence.background_removal_owned_by_current_process())
        cooldown = removal_fence.background_removal_cooldown_path()
        record = json.loads(cooldown.read_bytes())
        self.assertNotIn("pid", record)
        self.assertTrue(removal_fence.background_removal_active())
        with mock.patch.object(
                removal_fence.time, "time",
                return_value=record["expires_at"] + 0.001):
            self.assertFalse(removal_fence.background_removal_active())
        self.assertFalse(cooldown.exists())

    def test_future_dated_cooldown_and_malformed_fence_do_not_extend_lease(
            self) -> None:
        now = time.time()
        cooldown = removal_fence.background_removal_cooldown_path()
        cooldown.write_text(json.dumps({
            "completed_at": now + 3600,
            "expires_at": now + 3630,
            "nonce": "a" * 32,
        }), encoding="utf-8")
        self.assertFalse(removal_fence.background_removal_active())
        self.assertFalse(cooldown.exists())

        fence = removal_fence.background_removal_path()
        fence.write_bytes(b"{")
        future = time.time() + 3600
        os.utime(fence, (future, future))
        self.assertFalse(removal_fence.background_removal_active())
        self.assertFalse(fence.exists())

    def test_cross_uid_recycled_pid_does_not_pin_removal_fence(self) -> None:
        path = removal_fence.background_removal_path()
        path.write_text(json.dumps({
            "pid": 42_424,
            "process_start": "darwin_100_1",
            "started_at": time.time(),
            "nonce": "a" * 32,
        }), encoding="utf-8")
        with mock.patch.object(removal_fence, "pid_alive", return_value=True), \
                mock.patch.object(
                    removal_fence, "process_start_identity",
                    return_value="darwin_uid_0"):
            self.assertFalse(removal_fence.background_removal_active())
        self.assertFalse(path.exists())

    def test_foreign_live_fence_is_not_owned_by_current_process(self) -> None:
        removal_fence.background_removal_path().write_text(json.dumps({
            "pid": os.getpid(), "process_start": "foreign-birth",
            "started_at": time.time(), "nonce": "a" * 32,
        }), encoding="utf-8")
        self.assertFalse(
            removal_fence.background_removal_owned_by_current_process())

    def test_failed_cooldown_handoff_keeps_live_fence_closed(self) -> None:
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        with mock.patch.object(
                ownerfile, "create_exclusive", side_effect=OSError("denied")), \
                mock.patch.object(
                    fence, "touch", wraps=fence.touch) as touch:
            self.assertFalse(removal_fence.finish_background_removal_fence(fence))
        self.assertTrue(removal_fence.background_removal_path().exists())
        touch.assert_called_once_with()
        self.assertTrue(removal_fence.background_removal_active())

    def test_failed_live_release_leaves_both_fences_closed(self) -> None:
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        with mock.patch.object(fence, "release", return_value=False):
            self.assertFalse(removal_fence.finish_background_removal_fence(fence))
        self.assertTrue(removal_fence.background_removal_path().exists())
        self.assertTrue(removal_fence.background_removal_cooldown_path().exists())
        self.assertTrue(removal_fence.background_removal_active())
        fence.close()

    def test_setup_clear_removes_an_ownerless_cooldown(self) -> None:
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        self.assertTrue(removal_fence.finish_background_removal_fence(fence))
        self.assertTrue(removal_fence.clear_background_removal_fence())
        self.assertFalse(removal_fence.background_removal_active())

    def test_indexd_spawn_rechecks_fence_after_arbitration(self) -> None:
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        guard = mock.Mock()
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=absent), \
                mock.patch.object(indexd_runtime, "_indexd_ready", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    ownerfile, "create_exclusive", return_value=guard), \
                mock.patch.object(
                    indexd_runtime, "_release_spawn_guard", return_value=True) as release, \
                mock.patch.object(common.subprocess, "Popen") as spawn:
            result = _test_support.REAL_SPAWN_INDEXD()
        self.assertIs(result, indexd_runtime._IndexdSpawnResult.BLOCKED)
        release.assert_called_once_with(guard)
        spawn.assert_not_called()

    def test_indexd_owner_releases_a_post_claim_fence_race(self) -> None:
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, False, True)), \
                mock.patch.object(
                    indexd_runtime, "indexd_owner_body", return_value="owner\n"):
            self.assertIsNone(indexd_runtime.acquire_indexd_owner())
        self.assertFalse(indexd_runtime.INDEXD_LOCK_PATH.exists())

    def test_async_embed_spawn_rechecks_fence_before_popen(self) -> None:
        log = mock.Mock()
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    semantic, "runtime_dependencies_available", return_value=True), \
                mock.patch.object(
                    semantic, "embedding_coherence",
                    return_value={"coherent": False, "state": "stale"}), \
                mock.patch.object(semantic, "embed_running", return_value=False), \
                mock.patch.object(
                semantic, "read_embed_state", return_value={"state": "idle"}), \
                mock.patch.object(embedder, "ensure_model"), \
                mock.patch.object(
                    semantic, "_needs_unverified_bundle_rebuild",
                    return_value=False), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(semantic.subprocess, "Popen") as spawn:
            result = semantic.ensure_fresh_async()
        self.assertEqual(result["state"], "disabled")
        spawn.assert_not_called()
        log.close.assert_called_once_with()

    def test_async_refs_spawn_rechecks_fence_before_popen(self) -> None:
        log = mock.Mock()
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    semantic, "runtime_dependencies_available", return_value=True), \
                mock.patch.object(semantic, "embed_running", return_value=False), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(semantic.subprocess, "Popen") as spawn:
            result = semantic.ensure_refs_async()
        self.assertEqual(result["state"], "disabled")
        spawn.assert_not_called()
        log.close.assert_called_once_with()

    def test_compactor_scheduler_rechecks_fence_before_popen(self) -> None:
        log = mock.Mock()
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    embedding_segments, "load_manifest",
                    return_value=mock.Mock()), \
                mock.patch.object(embedding_segments, "prune_orphans"), \
                mock.patch.object(
                    common, "open_bounded_log", return_value=log), \
                mock.patch.object(embed.subprocess, "Popen") as spawn:
            result = embed._schedule_segment_compaction(
                refresh_metadata=True)
        self.assertFalse(result)
        spawn.assert_not_called()
        log.close.assert_called_once_with()

    def test_embed_claim_releases_a_post_claim_fence_race(self) -> None:
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            self.assertFalse(embed._acquire_claim())
        self.assertFalse(semantic.embed_claim_path().exists())

    def test_compactor_claim_releases_a_post_claim_fence_race(self) -> None:
        with mock.patch.object(
                removal_fence, "background_removal_active",
                side_effect=(False, True)), \
                mock.patch.object(
                    common, "process_start_identity", return_value="birth"):
            self.assertFalse(compact._acquire_claim())
        self.assertFalse(semantic.compaction_claim_path().exists())

    def test_claims_require_a_kernel_birth_identity(self) -> None:
        with mock.patch.object(
                removal_fence, "background_removal_active", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value=None), \
                mock.patch.object(ownerfile, "create_exclusive") as create:
            self.assertFalse(embed._acquire_claim())
            self.assertFalse(compact._acquire_claim())
        create.assert_not_called()

    def test_old_unverifiable_claims_are_never_reclaimed(self) -> None:
        owner_pid = 42_424
        for path, acquire in (
                (semantic.embed_claim_path(), embed._acquire_claim),
                (semantic.compaction_claim_path(), compact._acquire_claim)):
            with self.subTest(path=path.name):
                raw = self._claim(owner_pid, "owner-birth")
                path.write_bytes(raw)
                old = time.time() - 3_600
                os.utime(path, (old, old))

                def process_start(pid: int) -> str | None:
                    return "self-birth" if pid == os.getpid() else None

                with mock.patch.object(
                        removal_fence, "background_removal_active",
                        return_value=False), \
                        mock.patch.object(common, "pid_alive", return_value=True), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=process_start):
                    self.assertFalse(acquire())
                self.assertEqual(path.read_bytes(), raw)
                path.unlink()

    def test_removal_stops_and_settles_both_exact_writer_owners(self) -> None:
        owners = {
            42_421: "embed-birth",
            42_422: "compact-birth",
        }
        semantic.embed_claim_path().write_bytes(
            self._claim(42_421, owners[42_421]))
        semantic.compaction_claim_path().write_bytes(
            self._claim(42_422, owners[42_422], "b" * 32))
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        live = set(owners)

        def process_start(pid: int) -> str | None:
            return owners.get(pid)

        def terminate(pid: int, process_start: str, *, wait_s: float) -> bool:
            self.assertEqual(process_start, owners[pid])
            self.assertGreater(wait_s, 0.0)
            live.discard(pid)
            return True

        try:
            with mock.patch.object(
                    common, "pid_alive",
                    side_effect=lambda pid: pid in live), \
                    mock.patch.object(
                        common, "process_start_identity",
                        side_effect=process_start), \
                    mock.patch.object(
                        common, "terminate_exact_process_tree",
                        side_effect=terminate) as terminate_call:
                result = semantic.stop_background_writers_for_removal()
        finally:
            fence.release(tombstone=True, require_stable_mtime=True)
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["stopped"], ("semantic embed", "semantic compactor"))
        self.assertEqual(terminate_call.call_count, 2)
        self.assertFalse(semantic.embed_claim_path().exists())
        self.assertFalse(semantic.compaction_claim_path().exists())

    def test_removal_fails_closed_on_fresh_malformed_and_unverifiable(self) -> None:
        semantic.embed_claim_path().write_bytes(b"{")
        semantic.compaction_claim_path().write_bytes(
            self._claim(42_422, "compact-birth"))
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        try:
            with mock.patch.object(common, "pid_alive", return_value=True), \
                    mock.patch.object(
                        common, "process_start_identity", return_value=None):
                result = semantic.stop_background_writers_for_removal()
        finally:
            fence.release(tombstone=True, require_stable_mtime=True)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["claims"]["semantic embed"]["state"],
            "malformed-fresh")
        self.assertEqual(
            result["claims"]["semantic compactor"]["state"],
            ownerfile.ProcessOwner.UNVERIFIABLE.value)

    def test_removal_keeps_a_stale_malformed_writer_claim(self) -> None:
        path = semantic.embed_claim_path()
        path.write_bytes(b"{")
        old = time.time() - 3_600
        os.utime(path, (old, old))
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        try:
            with mock.patch.object(
                    common, "terminate_exact_process_tree") as terminate:
                result = semantic.stop_background_writers_for_removal()
        finally:
            fence.release(tombstone=True, require_stable_mtime=True)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["claims"]["semantic embed"]["state"],
            "malformed-stale")
        self.assertEqual(path.read_bytes(), b"{")
        terminate.assert_not_called()

    def test_future_malformed_writer_claim_is_not_fresh(self) -> None:
        path = semantic.embed_claim_path()
        path.write_bytes(b"{")
        future = time.time() + 3_600
        os.utime(path, (future, future))
        inspected = semantic._inspect_background_writer_claim(path)
        self.assertEqual(inspected["state"], "malformed-stale")

    def test_removal_never_signals_a_claim_without_a_valid_token(self) -> None:
        path = semantic.embed_claim_path()
        path.write_bytes(json.dumps({
            "pid": 42_421,
            "process_start": "embed-birth",
        }).encode("utf-8"))
        old = time.time() - 3_600
        os.utime(path, (old, old))
        with mock.patch.object(
                common, "terminate_exact_process_tree") as terminate:
            result = semantic._stop_background_writer_claim(
                path, wait_s=5.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "malformed-token")
        terminate.assert_not_called()
        self.assertTrue(path.exists())

    def test_removal_fails_closed_on_a_claim_replacement(self) -> None:
        path = semantic.embed_claim_path()
        path.write_bytes(self._claim(42_421, "first-birth"))
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        live = {42_421: "first-birth", 42_422: "replacement-birth"}

        def terminate(pid: int, _start: str, *, wait_s: float) -> bool:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(
                self._claim(42_422, "replacement-birth", "b" * 32))
            os.replace(replacement, path)
            live.pop(pid)
            return True

        try:
            with mock.patch.object(
                    common, "pid_alive",
                    side_effect=lambda pid: pid in live), \
                    mock.patch.object(
                        common, "process_start_identity",
                        side_effect=lambda pid: live.get(pid)), \
                    mock.patch.object(
                        common, "terminate_exact_process_tree",
                        side_effect=terminate):
                result = semantic.stop_background_writers_for_removal()
        finally:
            fence.release(tombstone=True, require_stable_mtime=True)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["claims"]["semantic embed"]["state"], "replaced")
        self.assertEqual(
            json.loads(path.read_bytes())["pid"], 42_422)


if __name__ == "__main__":
    unittest.main()
