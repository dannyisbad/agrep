"""Focused cross-platform regressions for release ownership/resource hardening."""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


_TMP = tempfile.TemporaryDirectory(prefix="agrep-lifecycle-test-")
atexit.register(_TMP.cleanup)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_support
from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime  # noqa: E402

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)

import common  # noqa: E402
import index_lock  # noqa: E402
import corpusdb  # noqa: E402
import embed  # noqa: E402
import embedder  # noqa: E402
import indexd  # noqa: E402
import ownerfile  # noqa: E402
import removal_fence  # noqa: E402
import resources  # noqa: E402
import semantic  # noqa: E402
import surface_policy as surface  # noqa: E402
import semworker  # noqa: E402
from hookless import capture, native  # noqa: E402


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def _read_published_pid(path: Path, timeout: float = 5.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid = int(path.read_text(encoding="ascii"))
                if pid > 0:
                    return pid
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        raise AssertionError(f"pid file was not completely published: {path}")

    @staticmethod
    def _test_indexer(watcher, *, owns_lifetime=None):
        owns = owns_lifetime or (lambda: True)
        tree = (
            f" tree={common.WINDOWS_DESCENDANT_TREE}"
            if common.WIN else "")
        raw = (
            f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group={'job' if common.WIN else 4242}{tree} token={'a' * 32} "
            "time=100.000\n"
        ).encode("ascii")
        snapshot = ownerfile.Snapshot(
            (1, 2, len(raw), 100_000_000_000), 100.0, raw)
        return indexd.indexer.AutoIndexer(
            watcher, owns_lifetime=owns, owner_snapshot=snapshot)

    def _run_successful_ingest(self, *, rebase, refresh):
        process = mock.Mock(pid=4242, returncode=0)
        process.communicate.return_value = ("", "")
        owner = self._test_indexer(mock.Mock())
        with (
            mock.patch.object(
                semantic, "source_generation", side_effect=("before", "after")),
            mock.patch.object(corpusdb, "_read_changed", return_value={"moved"}),
            mock.patch.object(
                embed, "rebase_generation_marker", side_effect=rebase) as rebased,
            mock.patch.object(
                owner, "_refresh_search_index", side_effect=refresh),
            mock.patch.object(
                owner, "_launch_index_process", return_value=(process, None)),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "rust_writer_env", return_value={}),
            mock.patch.object(owner, "_run_post_index_hooks") as hooks,
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "record_auto_index_health") as health,
        ):
            owner._index()
        return owner, rebased, hooks, health

    # cli.main() runs in this interpreter here and restores grep's SIGPIPE
    # disposition process-wide: left set, the next write to a closed pipe
    # kills every remaining test instead of raising BrokenPipeError.
    _SIGPIPE = getattr(signal, "SIGPIPE", None)
    _INHERITED_SIGPIPE = (
        signal.getsignal(_SIGPIPE) if _SIGPIPE is not None else None)

    def tearDown(self) -> None:
        if self._SIGPIPE is not None:
            signal.signal(self._SIGPIPE, self._INHERITED_SIGPIPE)
        indexd_runtime._set_fts_delegated(False)
        indexd_runtime._clear_freshen_failure()
        for path in (common.INDEX_LOCK_PATH, indexd_runtime.INDEXD_LOCK_PATH,
                     semworker.descriptor_path(), semworker.worker_lock_path(),
                     semworker.start_claim_path(),
                     removal_fence.background_removal_path(),
                     removal_fence.background_removal_cooldown_path()):
            path.unlink(missing_ok=True)

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits are not an ACL")
    def test_default_data_dir_is_private(self) -> None:
        self.assertEqual(common.DATA_DIR.stat().st_mode & 0o077, 0)

    def test_index_lock_drop_cannot_delete_replacement(self) -> None:
        lock = common.IndexLock("test", timeout=1)
        lock.__enter__()
        replacement = "pid=999999 start=unknown token=replacement label=test\n"
        common.INDEX_LOCK_PATH.write_text(replacement, encoding="utf-8")
        lock.__exit__(None, None, None)
        self.assertEqual(common.INDEX_LOCK_PATH.read_text(encoding="utf-8"), replacement)

    def test_index_lock_keeps_live_owner_when_birth_is_unreadable(self) -> None:
        body = f"pid={os.getpid()} start=known-owner token=owner label=test\n"
        common.INDEX_LOCK_PATH.write_text(body, encoding="utf-8")
        with (mock.patch.object(index_lock, "pid_alive", return_value=True),
              mock.patch.object(
                  index_lock, "process_start_identity", return_value=None)):
            with self.assertRaises(TimeoutError):
                common.IndexLock("contender", timeout=0.06).__enter__()
        self.assertEqual(common.INDEX_LOCK_PATH.read_text(encoding="utf-8"), body)

    def test_background_removal_fence_blocks_both_resident_launchers(self) -> None:
        fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(fence)
        try:
            with mock.patch.object(common.subprocess, "Popen") as spawn, \
                    mock.patch.object(
                        semworker, "_acquire_worker_lock") as acquire:
                self.assertEqual(
                    _test_support.REAL_SPAWN_INDEXD(),
                    indexd_runtime._IndexdSpawnResult.BLOCKED)
                self.assertTrue(semworker._worker_disabled())
                self.assertIsNone(semworker.acquire_resident_owner())
                self.assertIsNone(semworker.acquire_inprocess_owner())
            spawn.assert_not_called()
            acquire.assert_not_called()
        finally:
            fence.release(
                tombstone=True, require_stable_mtime=True)

    def test_dead_removal_fence_is_reclaimed_without_an_age_lease(self) -> None:
        path = removal_fence.background_removal_path()
        path.write_text(json.dumps({
            "pid": 999_999, "process_start": "dead-birth",
            "started_at": time.time(), "nonce": "a" * 32,
        }), encoding="utf-8")
        self.assertFalse(removal_fence.background_removal_active())
        self.assertFalse(path.exists())

    def test_empty_index_lock_uses_short_publication_grace(self) -> None:
        common.INDEX_LOCK_PATH.write_text("", encoding="utf-8")
        old = time.time() - 10
        os.utime(common.INDEX_LOCK_PATH, (old, old))
        with common.IndexLock("reclaim-empty", timeout=0.2):
            self.assertIn("pid=", common.INDEX_LOCK_PATH.read_text(encoding="utf-8"))

    def test_auto_indexer_resumes_the_persisted_failure_streak(self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = 0.0
        now = 10_000.0
        with mock.patch.object(
                indexd.indexer.indexd_runtime,
                "indexd_failure_state",
                return_value=(4, "still failing", now - 10),
        ), mock.patch.object(indexd.indexer.time, "time", return_value=now):
            owner = self._test_indexer(watcher)
        self.assertEqual(owner._fail_streak, 4)
        self.assertTrue(owner._retry_needed)
        self.assertEqual(owner.state["phase"], "error")
        self.assertEqual(owner.state["last_err"], "still failing")
        self.assertEqual(owner.state["last_run"], now - 10)
        with mock.patch.object(indexd.indexer.time, "time", return_value=now):
            self.assertFalse(owner._should_run())

    def test_auto_indexer_preserves_the_complete_failure_reason(self) -> None:
        reason = "ingest failed: " + "detail-" * 100
        process = mock.Mock(pid=4242, returncode=1)
        process.communicate.return_value = ("", reason)
        owner = self._test_indexer(mock.Mock())
        with (
            mock.patch.object(semantic, "source_generation", return_value=None),
            mock.patch.object(
                owner, "_launch_index_process",
                return_value=(process, None)),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(indexd.indexer.common, "log"),
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "record_auto_index_health") as record,
        ):
            owner._index()
        self.assertEqual(owner.state["last_err"], reason)
        record.assert_called_once_with(1, reason, escalated=False)

    def test_auto_indexer_escalates_to_full_once_per_identical_streak(
            self) -> None:
        wedge = ("ingest recovery needs a valid parse cache or two stable "
                 "source snapshots; retry or run --full")
        threshold = indexd.indexer.surface.FRESHNESS_POLICY.failure_threshold
        process = mock.Mock(pid=4242, returncode=1)
        process.communicate.return_value = ("", wedge)
        with mock.patch.object(
                indexd.indexer.indexd_runtime, "indexd_failure_state",
                return_value=(threshold, wedge, time.time() - 10)):
            owner = self._test_indexer(mock.Mock())
        launches: list[list[str]] = []
        with (
            mock.patch.object(semantic, "source_generation", return_value=None),
            mock.patch.object(
                owner, "_launch_index_process",
                side_effect=lambda cmd, launch: (
                    launches.append(list(cmd)), (process, None))[1]),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(indexd.indexer.common, "log") as log,
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "record_auto_index_health") as record,
        ):
            owner._index()
            owner._index()
        self.assertIn("--full", launches[0])
        self.assertNotIn("--full", launches[1])
        self.assertEqual(record.call_args_list, [
            # the streak's one --full is spent before launch, crash-safe
            mock.call(threshold, wedge, escalated=True),
            mock.call(threshold + 1, wedge, escalated=True),
            mock.call(threshold + 2, wedge, escalated=True),
        ])
        logged = "\n".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn("escalating to --full", logged)
        self.assertIn(f"failure streak {threshold} -> {threshold + 1}", logged)

    def test_auto_index_success_rearms_the_escalation_guard(self) -> None:
        wedge = "ingest could not read a source and had no complete cache fallback"
        threshold = indexd.indexer.surface.FRESHNESS_POLICY.failure_threshold
        process = mock.Mock(pid=4242, returncode=0)
        process.communicate.return_value = ("", "")
        with mock.patch.object(
                indexd.indexer.indexd_runtime, "indexd_failure_state",
                return_value=(threshold + 5, wedge, time.time() - 10)), \
                mock.patch.object(
                    indexd.indexer.indexd_runtime, "auto_index_escalated",
                    return_value=True):
            owner = self._test_indexer(mock.Mock())
        launches: list[list[str]] = []
        with (
            mock.patch.object(semantic, "source_generation", return_value=None),
            mock.patch.object(
                owner, "_launch_index_process",
                side_effect=lambda cmd, launch: (
                    launches.append(list(cmd)), (process, None))[1]),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(
                owner, "_refresh_search_index", return_value=True),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(owner, "_run_post_index_hooks"),
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "record_auto_index_health") as record,
        ):
            owner._index()
        self.assertNotIn("--full", launches[0])
        record.assert_called_once_with(0, "", escalated=False)
        self.assertEqual(owner._identical, 0)
        self.assertFalse(owner._escalated)

    def test_semantic_rebase_precedes_failed_fts_refresh(self) -> None:
        events = []

        def rebase(*_args, **_kwargs):
            events.append("rebase")

        def refresh():
            events.append("refresh")
            return False

        owner, rebased, hooks, _health = self._run_successful_ingest(
            rebase=rebase, refresh=refresh)
        self.assertEqual(events, ["rebase", "refresh"])
        rebased.assert_called_once_with(
            {"moved"}, expected_previous_source="before",
            expected_current_source="after")
        self.assertEqual(
            owner.state["last_err"],
            "derived search-index refresh failed; retry scheduled")
        hooks.assert_not_called()

    def test_semantic_rebase_precedes_fts_refresh_exception(self) -> None:
        events = []

        def rebase(*_args, **_kwargs):
            events.append("rebase")

        def refresh():
            events.append("refresh")
            raise RuntimeError("fts boom")

        owner, _rebased, hooks, _health = self._run_successful_ingest(
            rebase=rebase, refresh=refresh)
        self.assertEqual(events, ["rebase", "refresh"])
        self.assertEqual(owner.state["last_err"], "RuntimeError: fts boom")
        hooks.assert_not_called()

    def test_semantic_rebase_failure_does_not_mask_fts_success(self) -> None:
        events = []

        def rebase(*_args, **_kwargs):
            events.append("rebase")
            raise RuntimeError("rebase boom")

        def refresh():
            events.append("refresh")
            return True

        owner, _rebased, hooks, health = self._run_successful_ingest(
            rebase=rebase, refresh=refresh)
        self.assertEqual(events, ["rebase", "refresh"])
        self.assertEqual(owner.state["last_err"], "")
        self.assertFalse(owner._retry_needed)
        hooks.assert_called_once_with()
        health.assert_called_once_with(0, "", escalated=False)

    def test_pending_publication_after_clean_exit_schedules_confirming_pass(
            self) -> None:
        # A guarded deletion pass exits 0 but retains .ingest_pending.bin:
        # that is withheld work awaiting its confirming second pass, never
        # success. The loop must schedule the retry itself.
        process = mock.Mock(pid=4242, returncode=0)
        process.communicate.return_value = ("", "")
        owner = self._test_indexer(mock.Mock())
        pending = common.DATA_DIR / ".ingest_pending.bin"
        pending.write_bytes(b"withheld deletion preflight")
        mocks = lambda: (  # noqa: E731 -- one mock stack for both runs
            mock.patch.object(semantic, "source_generation", return_value=None),
            mock.patch.object(
                owner, "_launch_index_process", return_value=(process, None)),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(
                owner, "_refresh_search_index", return_value=True),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(owner, "_run_post_index_hooks"),
            mock.patch.object(
                indexd.indexer.indexd_runtime, "record_auto_index_health"),
        )
        try:
            with contextlib.ExitStack() as stack:
                for patch in mocks():
                    stack.enter_context(patch)
                owner._index()
            self.assertTrue(owner._retry_needed)
            self.assertEqual(owner._pending_streak, 1)
            self.assertEqual(owner._fail_streak, 0)
            # pending confirmation is work, not failure: the chip stays idle
            self.assertEqual(owner.state["phase"], "idle")
            self.assertEqual(owner.state["last_err"], "")
            ingest = Path(_TMP.name) / "agrep-rs-pending"
            ingest.write_bytes(b"fixture")
            later = owner.state["last_run"] + indexd.indexer.MIN_GAP_S + 1
            with mock.patch.object(indexd.indexer, "INGEST", ingest), \
                    mock.patch.object(
                        indexd.indexer.time, "time", return_value=later):
                self.assertTrue(owner._should_run())
        finally:
            pending.unlink(missing_ok=True)
        # the confirming pass publishes (marker gone): the loop stands down
        with contextlib.ExitStack() as stack:
            for patch in mocks():
                stack.enter_context(patch)
            owner._index()
        self.assertFalse(owner._retry_needed)
        self.assertEqual(owner._pending_streak, 0)

    def test_auto_indexer_never_promotes_progress_output_to_the_error(self) -> None:
        timing = "  indexed 1234 sessions in 11.9s\n  embedding coverage 98%\n"
        process = mock.Mock(pid=4242, returncode=125)
        process.communicate.return_value = (timing, "")
        owner = self._test_indexer(mock.Mock())
        with (
            mock.patch.object(semantic, "source_generation", return_value=None),
            mock.patch.object(
                owner, "_launch_index_process",
                return_value=(process, None)),
            mock.patch.object(
                indexd.indexer.common,
                "process_start_identity", return_value="birth"),
            mock.patch.object(indexd.indexer.common, "_close_event_reader"),
            mock.patch.object(indexd.indexer.common, "log") as log,
            mock.patch.object(
                indexd.indexer.indexd_runtime,
                "record_auto_index_health") as record,
        ):
            owner._index()
        err = owner.state["last_err"]
        self.assertEqual(
            err,
            "ingest exited 125: lifetime guard refused or lost ownership"
            " (last stdout: embedding coverage 98%)")
        record.assert_called_once_with(1, err, escalated=False)
        log.assert_called_once_with(f"auto-index failed: {err}")

    def test_ingest_failure_names_the_exit_mode_without_stderr(self) -> None:
        self.assertEqual(
            indexd.indexer._ingest_failure(-15, "", ""),
            "ingest killed by signal 15")
        self.assertEqual(
            indexd.indexer._ingest_failure(127, "", ""),
            "ingest exited 127: lifetime guard could not exec ingest")
        self.assertEqual(
            indexd.indexer._ingest_failure(2, "", ""),
            "ingest exited 2 with no error output")

    def test_ingest_failure_prefers_the_error_block_over_debug_timing(self) -> None:
        stderr = (
            "* [agrep ingest] collect 12.0ms · normalize 3.1ms\n"
            "* [agrep ingest] messages 40.0ms · replies 8.0ms\n"
            "Error: ingest could not read a source: permission denied")
        self.assertEqual(
            indexd.indexer._ingest_failure(1, "progress 50%", stderr),
            "Error: ingest could not read a source: permission denied")

    def test_auto_indexer_requires_an_owned_generation(self) -> None:
        with self.assertRaisesRegex(TypeError, "owned daemon generation"):
            indexd.indexer.AutoIndexer(
                mock.Mock(), owns_lifetime=lambda: True,
                owner_snapshot=None)

    def test_auto_index_retries_back_off_after_repeated_failures(self) -> None:
        self.assertEqual(indexd.indexer._retry_gap(0), indexd.indexer.MIN_GAP_S)
        self.assertEqual(indexd.indexer._retry_gap(1), indexd.indexer.MIN_GAP_S)
        self.assertEqual(indexd.indexer._retry_gap(2),
                         indexd.indexer.MIN_GAP_S * 2)
        self.assertLessEqual(indexd.indexer._retry_gap(100), 900)

    def test_indexd_cadence_is_configured_explicitly(self) -> None:
        names = ("CHECK_S", "QUIET_S", "MIN_GAP_S", "MAX_STALE_S")
        saved = tuple(getattr(indexd.indexer, name) for name in names)
        try:
            for name in names:
                setattr(indexd.indexer, name, -1)
            indexd.indexer.configure_indexd_mode()
            self.assertEqual(
                tuple(getattr(indexd.indexer, name) for name in names),
                (3, 4, 12, 60))
        finally:
            for name, value in zip(names, saved):
                setattr(indexd.indexer, name, value)

    def test_startup_reconcile_runs_with_existing_corpus_and_no_live_event(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "messages.jsonl").write_text(
                '{"content":"published before source changed"}\n',
                encoding="utf-8")
            ingest = root / "agrep-rs"
            ingest.write_bytes(b"fixture")
            watcher = mock.Mock()
            watcher._last_event_wall = 0.0
            checks = iter((True, False))
            owner = self._test_indexer(
                watcher, owns_lifetime=lambda: next(checks))
            with mock.patch.object(indexd.indexer, "INGEST", ingest), \
                    mock.patch.object(owner, "_index") as reconcile:
                owner.run()
        reconcile.assert_called_once_with(startup=True)

    def test_startup_hooks_skip_only_on_proven_unchanged_generation(
            self) -> None:
        for label, generations, hook_count in (
                ("unchanged", ("same", "same"), 0),
                ("changed", ("before", "after"), 1),
                ("before-unreadable", (RuntimeError("unreadable"), "after"), 1),
                ("before-missing", (None, "after"), 1),
        ):
            with self.subTest(label=label):
                watcher = mock.Mock()
                process = mock.Mock(pid=4242, returncode=0)
                process.communicate.return_value = ("", "")
                owner = self._test_indexer(watcher)
                with mock.patch.object(
                        semantic, "source_generation",
                        side_effect=generations), \
                        mock.patch.object(
                            owner, "_launch_index_process",
                            return_value=(process, None)), \
                        mock.patch.object(
                            indexd.indexer.common,
                            "process_start_identity",
                            return_value="birth"), \
                        mock.patch.object(
                            owner, "_refresh_search_index", return_value=True), \
                        mock.patch.object(
                            indexd.indexer.common, "_close_event_reader"), \
                        mock.patch.object(
                            indexd.indexer.indexd_runtime,
                            "record_auto_index_health"), \
                        mock.patch.object(
                            owner, "_run_post_index_hooks") as hooks:
                    owner._index(startup=True)
                self.assertEqual(hooks.call_count, hook_count)

    def test_failed_index_error_names_source_when_stderr_is_empty(self) -> None:
        health_path = common.DATA_DIR / ".source-health.json"
        health_path.write_text(json.dumps({
            "code": "source-unreadable",
            "issues": [{
                "agent": "claude",
                "path": "/fixture/.claude/projects/session.jsonl",
                "kind": "permission-denied",
                "reason": "permission denied",
            }],
        }), encoding="utf-8")
        watcher = mock.Mock()
        process = mock.Mock(pid=4242, returncode=1)
        process.communicate.return_value = (
            "  phases: source-check 3ms · ingest+dedupe 9ms\n", "")
        owner = self._test_indexer(watcher)
        try:
            with mock.patch.object(
                    owner, "_launch_index_process",
                    return_value=(process, None)), \
                    mock.patch.object(
                        indexd.indexer.common,
                        "process_start_identity",
                        return_value="birth"), \
                    mock.patch.object(
                        indexd.indexer.common, "_close_event_reader"), \
                    mock.patch.object(
                        indexd.indexer.indexd_runtime,
                        "record_auto_index_health") as health, \
                    mock.patch.object(indexd.indexer.common, "log") as log:
                owner._index()
        finally:
            health_path.unlink(missing_ok=True)
        err = health.call_args.args[1]
        self.assertIn("phases:", err)
        self.assertIn(
            "/fixture/.claude/projects/session.jsonl could not be read "
            "(permission denied)", err)
        self.assertIn(
            "/fixture/.claude/projects/session.jsonl", log.call_args.args[0])

    @unittest.skipIf(sys.platform == "win32", "POSIX guardian")
    def test_auto_indexer_launches_generation_guardian(self) -> None:
        token = "a" * 32
        raw = (
            f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group=4242 token={token} time=100.000\n"
        ).encode("ascii")
        snapshot = ownerfile.Snapshot(
            (1, 2, len(raw), 100_000_000_000), 100.0, raw)
        watcher = mock.Mock()
        process = mock.Mock()
        owner = indexd.indexer.AutoIndexer(
            watcher, owns_lifetime=lambda: True,
            owner_snapshot=snapshot)
        launch = {"cwd": str(common.REPO_ROOT)}
        with mock.patch.object(
                indexd.indexer.os, "pipe", return_value=(41, 42)), \
                mock.patch.object(indexd.indexer.os, "close") as close, \
                mock.patch.object(
                    indexd.indexer.subprocess, "Popen",
                    return_value=process) as popen:
            returned, control_fd = owner._launch_index_process(
                ["agrep-rs", "index"], launch)
        self.assertIs(returned, process)
        self.assertEqual(control_fd, 42)
        guardian = popen.call_args.args[0]
        self.assertEqual(guardian[:2], [
            sys.executable, str(common.PY_DIR / "lifetime.py")])
        self.assertEqual(guardian[2:8], [
            "--parent-fd", "41",
            "--fence", str(indexd_runtime.indexd_child_path(snapshot)),
            "--owner-token", token,
        ])
        self.assertEqual(guardian[8:], ["--", "agrep-rs", "index"])
        self.assertEqual(popen.call_args.kwargs, {
            "pass_fds": (41,),
            "start_new_session": True,
            **launch,
        })
        close.assert_called_once_with(41)

    @unittest.skipIf(sys.platform == "win32", "POSIX guardian")
    def test_auto_indexer_refuses_a_second_guard_for_an_active_fence(
            self) -> None:
        token = "b" * 32
        raw = (
            f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
            f"package={common.package_version()} build={indexd_runtime.INDEXD_BUILD_ID} "
            f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
            f"group=4242 token={token} time=100.000\n"
        ).encode("ascii")
        snapshot = ownerfile.Snapshot(
            (1, 2, len(raw), 100_000_000_000), 100.0, raw)
        owner = indexd.indexer.AutoIndexer(
            mock.Mock(), owns_lifetime=lambda: True,
            owner_snapshot=snapshot)
        with mock.patch.object(
                indexd.indexer.indexd_runtime, "_indexd_child_active",
                return_value=True), \
                mock.patch.object(indexd.indexer.os, "pipe") as pipe, \
                mock.patch.object(
                    indexd.indexer.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(
                    ownerfile.OwnershipLost, "earlier index child"):
                owner._launch_index_process(
                    ["agrep-rs", "index"], {"cwd": str(common.REPO_ROOT)})
        pipe.assert_not_called()
        popen.assert_not_called()

    def test_auto_indexer_stop_closes_guardian_control(self) -> None:
        watcher = mock.Mock()
        process = mock.Mock(pid=4242)
        owner = self._test_indexer(watcher)
        owner._child = (process, "birth", 42)
        with mock.patch.object(indexd.indexer.os, "close") as close, \
                mock.patch.object(
                    indexd.indexer.common,
                    "terminate_exact_process_tree") as terminate:
            owner._terminate_child()
        close.assert_called_once_with(42)
        terminate.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(owner._child, (process, "birth", None))

    def test_daemon_fts_refresh_uses_only_the_owned_child(self) -> None:
        outcomes = (
            (0, True, ""),
            (indexd_runtime.SEARCH_INDEX_REFRESH_UNSUPPORTED_RC, None, ""),
            (1, False, "sqlite full rebuild failed"),
        )
        for returncode, expected, stderr in outcomes:
            with self.subTest(returncode=returncode):
                process = mock.Mock(pid=4242, returncode=returncode)
                process.communicate.return_value = ("", stderr)
                owner = self._test_indexer(mock.Mock())
                expected_writer = "a" * 20
                with mock.patch.object(
                        owner, "_launch_index_process",
                        return_value=(process, 42)) as launch, \
                        mock.patch.object(
                            indexd.indexer.common,
                            "process_start_identity", return_value="birth"), \
                        mock.patch.object(
                            indexd.indexer.indexd_runtime,
                            "rust_writer_env", return_value={
                                "AGREP_RUNTIME_BUILD_ID": expected_writer,
                                "EXACT": "1",
                            }), \
                        mock.patch.object(
                            indexd.indexer.indexd_runtime,
                            "refresh_search_index") as inline, \
                        mock.patch.object(indexd.indexer.os, "close") as close, \
                        mock.patch.object(indexd.indexer.common, "log") as log:
                    self.assertIs(owner._refresh_search_index(), expected)
                command, child_launch = launch.call_args.args
                self.assertEqual(command, [
                    sys.executable, str(common.PY_DIR / "indexd.py"),
                    "--refresh-search-index-child",
                ])
                self.assertEqual(child_launch["env"], {
                    "AGREP_RUNTIME_BUILD_ID": expected_writer,
                    "AGREP_INDEXD_REFRESH_EXPECTED_WRITER": expected_writer,
                    "EXACT": "1",
                })
                inline.assert_not_called()
                close.assert_called_once_with(42)
                self.assertIsNone(owner._child)
                if stderr:
                    self.assertIn(stderr, log.call_args.args[0])
                else:
                    log.assert_not_called()

    def test_fts_refresh_owner_loss_drains_before_releasing_child(self) -> None:
        checks = iter((True, False))
        owner = self._test_indexer(
            mock.Mock(), owns_lifetime=lambda: next(checks))
        process = mock.Mock(pid=4242, returncode=0)
        process.communicate.return_value = ("", "")
        control_fd = None if common.WIN else 42
        with mock.patch.object(
                owner, "_launch_index_process",
                return_value=(process, control_fd)), \
                mock.patch.object(
                    indexd.indexer.common,
                    "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    indexd.indexer.indexd_runtime,
                    "rust_writer_env", return_value={
                        "AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                mock.patch.object(indexd.indexer.os, "close") as close, \
                mock.patch.object(
                    indexd.indexer.common,
                    "terminate_exact_process_tree", return_value=True) as terminate:
            with self.assertRaisesRegex(
                    ownerfile.OwnershipLost, "lifetime owner changed"):
                owner._refresh_search_index()
        process.communicate.assert_called_once_with(timeout=3.0)
        self.assertIsNone(owner._child)
        if common.WIN:
            terminate.assert_called_once_with(
                4242, "birth", wait_s=2.0, require_bound_tree=True)
            close.assert_not_called()
        else:
            close.assert_called_once_with(42)
            terminate.assert_not_called()

    def test_refresh_child_exit_preserves_the_tristate(self) -> None:
        expected_writer = "a" * 20
        for refreshed, expected in (
                (True, 0), (False, 1),
                (None, indexd_runtime.SEARCH_INDEX_REFRESH_UNSUPPORTED_RC)):
            ownership = indexd_runtime.DerivedMutationInfo(
                "current", expected_writer, "")
            with self.subTest(refreshed=refreshed), \
                    mock.patch.object(
                        indexd_runtime, "_INDEXD_REFRESH_EXPECTED_WRITER",
                        expected_writer), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_build_id",
                        return_value=expected_writer), \
                    mock.patch.object(
                        indexd_runtime, "derived_writer_mutation_info",
                        return_value=ownership), \
                    mock.patch.object(
                        indexd_runtime, "refresh_search_index",
                        return_value=refreshed):
                self.assertEqual(
                    indexd_runtime.search_index_refresh_child_exit(), expected)

    def test_refresh_child_rejects_a_runtime_swap_before_refresh(self) -> None:
        with mock.patch.object(
                indexd_runtime, "_INDEXD_REFRESH_EXPECTED_WRITER", "a" * 20), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value="b" * 20), \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index") as refresh, \
                mock.patch.object(indexd_runtime.common, "log") as log:
            self.assertEqual(indexd_runtime.search_index_refresh_child_exit(), 1)
        refresh.assert_not_called()
        self.assertIn("runtime changed", log.call_args.args[0])

    def test_refresh_child_rejects_a_foreign_read_only_result(self) -> None:
        expected_writer = "a" * 20
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", "b" * 20, "foreign owner")
        with mock.patch.object(
                indexd_runtime, "_INDEXD_REFRESH_EXPECTED_WRITER",
                expected_writer), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=expected_writer), \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=foreign), \
                mock.patch.object(indexd_runtime.common, "log") as log:
            self.assertEqual(indexd_runtime.search_index_refresh_child_exit(), 1)
        self.assertIn("foreign owner", log.call_args.args[0])

    def test_refresh_child_keeps_request_when_runtime_moves_during_build(
            self) -> None:
        expected_writer = "a" * 20
        with mock.patch.object(
                indexd_runtime, "_INDEXD_REFRESH_EXPECTED_WRITER",
                expected_writer), \
                mock.patch.object(
                    indexd_runtime, "assert_python_runtime_unchanged",
                    side_effect=(None, OSError("runtime moved"))), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=expected_writer), \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info") as ownership, \
                mock.patch.object(indexd_runtime.common, "log") as log:
            self.assertEqual(indexd_runtime.search_index_refresh_child_exit(), 1)
        ownership.assert_not_called()
        self.assertIn("lost its writer proof", log.call_args.args[0])

    def test_internal_refresh_entrypoint_never_starts_a_daemon(self) -> None:
        with mock.patch.object(
                indexd_runtime, "search_index_refresh_child_exit",
                return_value=indexd_runtime.SEARCH_INDEX_REFRESH_UNSUPPORTED_RC), \
                mock.patch.object(indexd, "main") as daemon:
            self.assertEqual(
                indexd._entrypoint(["--refresh-search-index-child"]),
                indexd_runtime.SEARCH_INDEX_REFRESH_UNSUPPORTED_RC)
        daemon.assert_not_called()

    def test_refresh_rejects_a_stale_database_as_publication(self) -> None:
        stale = mock.Mock()
        stale.execute.return_value = (
            ("stamp", "old"),
            ("schema", corpusdb._SCHEMA),
            ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
            ("build_id", "current-build"),
        )
        with mock.patch.object(corpusdb, "_trigram_ok", return_value=True), \
                mock.patch.object(corpusdb, "connect", return_value=stale), \
                mock.patch.object(corpusdb, "_stamp", return_value="current"), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value="current-build"), \
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")):
            self.assertIs(indexd_runtime.refresh_search_index(), False)
        stale.close.assert_called_once_with()

    def test_auto_indexer_stops_child_after_post_launch_lifetime_loss(
            self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = 0.0
        checks = iter((True, True, False))
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        owner = self._test_indexer(
            watcher, owns_lifetime=lambda: next(checks))
        with mock.patch.object(
                indexd.indexer.os, "pipe", return_value=(41, 42)), \
                mock.patch.object(indexd.indexer.os, "close") as close, \
                mock.patch.object(
                    indexd.indexer.subprocess, "Popen",
                    return_value=process) as popen, \
                mock.patch.object(
                    indexd.indexer.common,
                    "process_start_identity", return_value="birth"), \
                mock.patch.object(
                    indexd.indexer.common,
                    "terminate_exact_process_tree",
                    return_value=True) as terminate, \
                mock.patch.object(indexd.indexer.common, "_close_event_reader"):
            owner._index()
        popen.assert_called_once()
        process.communicate.assert_not_called()
        if common.WIN:
            terminate.assert_called_once_with(
                4242, "birth", wait_s=2.0, require_bound_tree=True)
            close.assert_not_called()
        else:
            terminate.assert_not_called()
            self.assertIn(mock.call(41), close.call_args_list)
            self.assertIn(mock.call(42), close.call_args_list)
        self.assertTrue(owner._stop_requested.is_set())

    def test_embedding_scheduler_env_numbers_fail_safe(self) -> None:
        with mock.patch.dict(
                os.environ,
                {"AGREP_TEST_BAD_INT": "not-a-number"},
                clear=False,
        ):
            self.assertEqual(
                indexd.indexer._env_int("AGREP_TEST_BAD_INT", 500, 10), 500)
        with mock.patch.dict(
                os.environ,
                {"AGREP_TEST_BAD_INT": "-20"},
                clear=False,
        ):
            self.assertEqual(
                indexd.indexer._env_int("AGREP_TEST_BAD_INT", 500, 10), 10)

    def test_no_daemon_first_run_builds_fts_inline(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn, \
                mock.patch.object(indexd_runtime, "refresh_search_index") as refresh:
            self.assertFalse(indexd_runtime._delegate_fts_build())
        spawn.assert_not_called()
        # quiet=False: the build's own announcement is conditional on a build
        # that will cost something, and owns this surface alone.
        refresh.assert_called_once_with(quiet=False)

    def test_no_daemon_full_stream_page_still_builds_fts_inline(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn, \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index",
                    return_value=True) as refresh:
            outcome = indexd_runtime._run_search_index_build(
                allow_inline_fallback=False)

        self.assertIs(outcome, surface.IndexBuildOutcome.BUILT)
        spawn.assert_not_called()
        refresh.assert_called_once_with(quiet=False)

    def test_true_first_run_states_cold_fts_cost_before_inline_fallback(
            self) -> None:
        events = []

        def log(message):
            events.append(("log", message))

        def refresh(quiet: bool = True):
            events.append(("refresh", None))
            return True

        with mock.patch.object(
                indexd_runtime, "_reclassify_indexd_spawn_failure",
                return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                mock.patch.object(indexd_runtime, "_spawn_indexd"), \
                mock.patch.object(indexd_runtime, "_clear_own_spawn_guard"), \
                mock.patch.object(indexd_runtime.common, "log", side_effect=log), \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index", side_effect=refresh):
            self.assertFalse(indexd_runtime._delegate_fts_build())

        self.assertEqual(events[-1], ("refresh", None))
        self.assertEqual(events[0][0], "log")
        self.assertIn("full history", events[0][1])
        self.assertIn("several minutes", events[0][1])

    def test_full_stream_page_durably_queues_failed_fts_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(common, "DATA_DIR", Path(raw)), \
                mock.patch.object(
                    indexd_runtime, "_reclassify_indexd_spawn_failure",
                    return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                mock.patch.object(indexd_runtime, "_spawn_indexd"), \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index") as refresh, \
                mock.patch.object(
                    indexd_runtime, "_clear_own_spawn_guard") as clear, \
                mock.patch.object(indexd_runtime.common, "log") as log:
            outcome = indexd_runtime._run_search_index_build(
                allow_inline_fallback=False)
            request = Path(raw) / ".search_index_request"
            self.assertTrue(request.exists())
            built = mock.Mock(return_value=True)
            self.assertTrue(indexd_runtime.serve_search_index_request(built))
            self.assertFalse(request.exists())

        self.assertIs(outcome, surface.IndexBuildOutcome.BLOCKED)
        refresh.assert_not_called()
        built.assert_called_once_with()
        clear.assert_called_once_with(force=True)
        log.assert_not_called()

    def test_full_stream_page_builds_inline_when_queue_write_fails(self) -> None:
        with mock.patch.object(
                indexd_runtime, "_reclassify_indexd_spawn_failure",
                return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                mock.patch.object(indexd_runtime, "_spawn_indexd"), \
                mock.patch.object(
                    indexd_runtime, "request_search_index_build",
                    return_value=False) as queued, \
                mock.patch.object(
                    indexd_runtime, "refresh_search_index",
                    return_value=True) as refresh, \
                mock.patch.object(
                    indexd_runtime, "_clear_own_spawn_guard") as clear:
            outcome = indexd_runtime._run_search_index_build(
                allow_inline_fallback=False)

        self.assertIs(outcome, surface.IndexBuildOutcome.BUILT)
        queued.assert_called_once_with()
        refresh.assert_called_once_with(quiet=False)
        clear.assert_called_once_with(force=True)

    def test_b3_1_full_disk_ledger_failure_still_discloses(self) -> None:
        # A ledger that cannot be written must not go silent: the writing
        # process discloses the transient, and the backdated sig makes a
        # restarted process read the box as drifted instead of green.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            signature = root / ".ingest.sig"
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            signature.touch()
            old = time.time() - indexd_runtime.FRESHNESS_WRITE_RATE_S - 1
            os.utime(signature, (old, old))
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            # the store moved after the backdated sig but past the daemon's
            # debounce grace horizon: the restart must read real drift
            census = [{"name": "claude", "files": 3, "state": "available",
                       "newest_mtime_ms": int(
                           (time.time() - indexd_runtime.DRIFT_GRACE_S - 10)
                           * 1000)}]
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent), \
                    mock.patch.object(
                        indexd_runtime.Path, "write_text",
                        side_effect=OSError(28, "no space left on device")):
                indexd_runtime.record_auto_index_health(3, "lost ledger")
            self.assertLess(
                signature.stat().st_mtime,
                time.time() - indexd_runtime.FRESHNESS_WRITE_RATE_S)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                self.assertEqual(indexd_runtime.indexd_failure_state(), (0, "", 0.0))
                disclosure = indexd_runtime.machine_freshness(checked=True)
            self.assertEqual(disclosure["state"], "unknown")
            self.assertEqual(disclosure["code"], "freshness-ledger-unavailable")
            self.assertFalse(disclosure["checked"])
            self.assertTrue(disclosure["may_be_stale"])
            indexd_runtime._clear_freshen_failure()
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_store_census",
                        return_value=census), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                restarted = indexd_runtime.machine_freshness(checked=True)
            self.assertEqual(restarted["state"], "index-behind")
            self.assertTrue(restarted["may_be_stale"])
            self.assertFalse(restarted["failing"])

    def test_missing_ingest_signal_discloses_published_corpus_as_stale(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_store_census", return_value=[]), \
                    mock.patch.object(
                        indexd_runtime, "indexd_failing", return_value=(0, "")), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(disclosure["state"], "degraded")
        self.assertEqual(disclosure["code"], "missing-ingest-signal")
        self.assertTrue(disclosure["may_be_stale"])

    def test_missing_ingest_signal_is_neutral_before_first_publication(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(
                        common, "INGEST_SIG_PATH", root / ".ingest.sig"), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "indexd_failing", return_value=(0, "")), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                failure = indexd_runtime.indexing_failure()
        self.assertIsNone(failure)

    def test_b3_2_stalled_live_lock_cannot_suppress_drift_disclosure(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock = root / ".index.lock"
            signature = root / ".ingest.sig"
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            start = common.process_start_identity(os.getpid()) or "unknown"
            lock.write_text(
                f"pid={os.getpid()} start={start} token={'a' * 32} "
                "label=stalled\n",
                encoding="ascii")
            signature.touch()
            old = time.time() - 3600
            os.utime(signature, (old, old))
            census = [{"name": "claude", "files": 3, "state": "available",
                       "newest_mtime_ms": int((time.time() - 1800) * 1000)}]
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INDEX_LOCK_PATH", lock), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_store_census",
                        return_value=census), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                self.assertTrue(corpusdb._live_refresh_lock())
                self.assertIsNone(indexd_runtime.indexing_failure())
                disclosure = indexd_runtime.machine_freshness(checked=True)
            self.assertEqual(disclosure["state"], "index-behind")
            self.assertTrue(disclosure["may_be_stale"])

    def test_b3_3_ready_daemon_liveness_never_claims_freshness(self) -> None:
        # The old _freshener_alive short-circuit is dead as a truth source: a
        # live daemon only wins ownership of the refresh; the verdict is the
        # drift compare, which discloses drift right past a "ready" daemon.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            signature = root / ".ingest.sig"
            beat = root / ".search"
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            signature.touch()
            old = time.time() - 3600
            os.utime(signature, (old, old))
            census = [{"name": "claude", "files": 3, "state": "available",
                       "newest_mtime_ms": int((time.time() - 1800) * 1000)}]
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(indexd_runtime, "SEARCH_BEAT_PATH", beat), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=True), \
                    mock.patch.object(
                        indexd_runtime, "_store_census",
                        return_value=census), \
                    mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn, \
                    mock.patch.object(indexd_runtime, "build_index") as build, \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                indexd_runtime._maybe_freshen()
                self.assertIsNone(indexd_runtime.indexing_failure())
                notice = indexd_runtime.agent_freshness_notice()
            self.assertTrue(beat.exists())
            self.assertIn("behind", notice)
            self.assertIn("1 store changed", notice)
            self.assertIn("catching up", notice)
            spawn.assert_not_called()
            build.assert_not_called()

    def test_b3_8_future_ingest_signal_is_never_trusted_as_green(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            signature = root / ".ingest.sig"
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            signature.touch()
            future = time.time() + 3600
            os.utime(signature, (future, future))
            census = [{"name": "claude", "files": 3, "state": "available",
                       "newest_mtime_ms": int(time.time() * 1000)}]
            absent = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_store_census",
                        return_value=census), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=absent):
                failure = indexd_runtime.indexing_failure()
            self.assertEqual(failure.code, "future-ingest-signal")

    def test_future_semantic_demand_signal_is_not_recent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            beat = root / ".semantic-use.beat"
            beat.touch()
            future = time.time() + 3600
            os.utime(beat, (future, future))
            with mock.patch.object(common, "DATA_DIR", root):
                self.assertFalse(semantic.semantic_recently_used())

    def test_missing_binary_marks_indexing_as_not_happening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "agrep-rs"
            with mock.patch.object(common, "ingest_bin", return_value=missing):
                indexd_runtime._maybe_freshen()
            failure = indexd_runtime.indexing_failure()
        self.assertEqual(failure.code, "missing-ingest-binary")
        self.assertIn(str(missing), failure.reason)

    def test_blocked_owner_marks_indexing_as_not_happening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            messages = Path(raw) / "messages.jsonl"
            messages.touch()
            with mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.BLOCKED):
                indexd_runtime._maybe_freshen()
            failure = indexd_runtime.indexing_failure()
        self.assertEqual(failure.code, "blocked-owner")
        self.assertTrue(indexd_runtime._fts_delegated)

    def test_fresh_process_observes_a_current_blocked_owner(self) -> None:
        blocked = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.HOSTILE, None, 42, "birth")
        indexd_runtime._clear_freshen_failure()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            # a blocked owner is only news once something is published for it
            # to be blocking; the publication is this test's, not the run's
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            (root / ".ingest.sig").write_text("sig", encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "INGEST_SIG_PATH", root / ".ingest.sig"), \
                    mock.patch.object(
                        indexd_runtime, "_store_census", return_value=[]), \
                    mock.patch.object(
                        indexd_runtime, "indexd_failing", return_value=(0, "")), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=blocked), \
                    mock.patch.object(
                        indexd_runtime, "indexd_resource_status",
                        return_value={
                            "state": indexd_runtime._IndexdOwnerState
                            .HOSTILE.value}), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)):
                failure = indexd_runtime.indexing_failure()
        self.assertEqual(failure.code, "blocked-owner")
        self.assertIn("hostile", failure.reason)

    def test_blocked_first_build_marks_indexing_as_not_happening(self) -> None:
        with mock.patch.object(
                indexd_runtime, "_spawn_indexd",
                return_value=indexd_runtime._IndexdSpawnResult.BLOCKED):
            self.assertTrue(indexd_runtime._delegate_fts_build())
        failure = indexd_runtime.indexing_failure()
        self.assertEqual(failure.code, "blocked-owner")

    def test_failed_daemon_defers_published_snapshot_without_inline_build(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.touch()
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                    mock.patch.object(
                        indexd_runtime, "build_index", return_value=False) as build:
                indexd_runtime._maybe_freshen()
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=True)
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)
            build.assert_not_called()
        self.assertEqual(disclosure["code"], "search-index-stale")
        self.assertIn("failed to start", disclosure["reason"])

    def test_no_daemon_published_snapshot_never_refreshes_inline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            signature = root / ".ingest.sig"
            messages = root / "messages.jsonl"
            signature.touch()
            messages.touch()
            with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                    mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "_store_census", return_value=[]), \
                    mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn, \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
                failure = indexd_runtime.indexing_failure()
            self.assertIsNone(failure)
            build.assert_not_called()
            spawn.assert_not_called()

    def test_no_daemon_stale_snapshot_discloses_without_inline_refresh(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            messages = Path(raw) / "messages.jsonl"
            messages.touch()
            with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=False)
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)
            build.assert_not_called()
        self.assertEqual(disclosure["code"], "search-index-stale")
        self.assertIn("background indexing is disabled", disclosure["reason"])

    def test_failed_daemon_launch_does_not_use_successful_inline_refresh(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            signature = root / ".ingest.sig"
            messages.touch()
            signature.touch()
            (root / "corpus.db").touch()
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=False)
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)
            build.assert_not_called()
            self.assertEqual(disclosure["code"], "search-index-stale")
            self.assertIn("failed to start", disclosure["reason"])

    def test_search_beat_is_sent_only_to_a_compatible_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            beat = root / ".search"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(indexd_runtime, "SEARCH_BEAT_PATH", beat), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.BLOCKED):
                indexd_runtime._maybe_freshen()
            self.assertFalse(beat.exists())

            with mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(indexd_runtime, "SEARCH_BEAT_PATH", beat), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=True), \
                    mock.patch.object(indexd_runtime, "_spawn_indexd") as spawn:
                indexd_runtime._maybe_freshen()
            self.assertTrue(beat.exists())
            spawn.assert_not_called()

    def test_protected_first_fts_build_keeps_later_searches_on_jsonl(self) -> None:
        for result in (
                indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                indexd_runtime._IndexdSpawnResult.BLOCKED):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                messages = root / "messages.jsonl"
                messages.write_text("{}\n", encoding="utf-8")
                indexd_runtime._set_fts_delegated(False)
                with mock.patch.object(common, "DATA_DIR", root), \
                        mock.patch.object(common, "MESSAGES_PATH", messages), \
                        mock.patch.object(
                            indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"), \
                        mock.patch.object(
                            common, "ingest_bin", return_value=messages), \
                        mock.patch.object(
                            indexd_runtime, "freshener_alive", return_value=False), \
                        mock.patch.object(
                            indexd_runtime, "_spawn_indexd", return_value=result), \
                        mock.patch.object(indexd_runtime, "build_index") as build:
                    indexd_runtime._maybe_freshen()
                build.assert_not_called()
                self.assertTrue(indexd_runtime._fts_delegated)

    def test_protected_refresh_with_stale_db_does_not_rebuild_inline(self) -> None:
        for result in (
                indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                indexd_runtime._IndexdSpawnResult.BLOCKED):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                messages = root / "messages.jsonl"
                db_path = root / "corpus.db"
                messages.write_text("{}\n", encoding="utf-8")
                db_path.write_bytes(b"stale")
                indexd_runtime._set_fts_delegated(False)
                with mock.patch.object(common, "DATA_DIR", root), \
                        mock.patch.object(common, "MESSAGES_PATH", messages), \
                        mock.patch.object(
                            indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"), \
                        mock.patch.object(
                            common, "ingest_bin", return_value=messages), \
                        mock.patch.object(
                            indexd_runtime, "freshener_alive", return_value=False), \
                        mock.patch.object(
                            indexd_runtime, "_spawn_indexd", return_value=result), \
                        mock.patch.object(indexd_runtime, "build_index") as build, \
                        mock.patch.object(corpusdb, "DB_PATH", db_path), \
                        mock.patch.object(
                            corpusdb, "_trigram_ok", return_value=True), \
                        mock.patch.object(
                            corpusdb, "_stamp", return_value="new"), \
                        mock.patch.object(
                            corpusdb, "_valid_db", return_value=None), \
                        mock.patch.object(corpusdb, "_incremental") as incremental:
                    indexd_runtime._maybe_freshen()
                    self.assertIsNone(corpusdb.connect())
                build.assert_not_called()
                incremental.assert_not_called()
                self.assertTrue(indexd_runtime._fts_delegated)

    def test_failed_daemon_never_rebuilds_a_published_snapshot_inline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
            build.assert_not_called()

    def test_failed_spawn_is_rechecked_before_inline_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            incompatible = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
                mock.Mock(), 4242, "birth")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                    mock.patch.object(
                        indexd_runtime, "_retire_legacy_indexd",
                        return_value=True), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=incompatible), \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
            build.assert_not_called()

    def test_failed_spawn_releases_only_its_guard_without_inline_fallback(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            guard_path = root / ".indexd.spawn"
            messages.write_text("{}\n", encoding="utf-8")
            start = common.process_start_identity(os.getpid())
            self.assertIsNotNone(start)
            body = (
                f"state=launching pid={os.getpid()} start={start} "
                f"token={'a' * 32}\n"
            ).encode("ascii")
            handle = ownerfile.create_exclusive(guard_path, body)
            indexd_runtime._OWN_SPAWN_GUARD.snapshot = handle.snapshot
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(
                        indexd_runtime, "SEARCH_BEAT_PATH", root / ".search"), \
                    mock.patch.object(
                        indexd_runtime, "_SPAWN_GUARD_PATH", guard_path), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=messages), \
                    mock.patch.object(
                        indexd_runtime, "freshener_alive", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "_spawn_indexd",
                        return_value=indexd_runtime._IndexdSpawnResult.FAILED), \
                    mock.patch.object(indexd_runtime, "build_index") as build:
                indexd_runtime._maybe_freshen()
            build.assert_not_called()
            self.assertFalse(guard_path.exists())

    def test_forced_spawn_guard_cleanup_retries_transient_remove_failure(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard_path = Path(raw) / ".indexd.spawn"
            start = common.process_start_identity(os.getpid())
            self.assertIsNotNone(start)
            body = (
                f"state=launching pid={os.getpid()} start={start} "
                f"token={'a' * 32}\n"
            ).encode("ascii")
            handle = ownerfile.create_exclusive(guard_path, body)
            indexd_runtime._OWN_SPAWN_GUARD.snapshot = handle.snapshot
            with mock.patch.object(
                    indexd_runtime, "_SPAWN_GUARD_PATH", guard_path), \
                    mock.patch.object(
                        indexd_runtime, "_retire_legacy_indexd", return_value=True), \
                    mock.patch.object(
                        ownerfile, "remove_exact", return_value=False), \
                    mock.patch.object(common.subprocess, "Popen") as popen:
                indexd_runtime._clear_own_spawn_guard(force=True)
                self.assertEqual(
                    indexd_runtime._OWN_SPAWN_GUARD.snapshot, handle.snapshot)
                self.assertIs(
                    indexd_runtime._spawn_indexd(),
                    indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
                self.assertEqual(
                    indexd_runtime._OWN_SPAWN_GUARD.snapshot, handle.snapshot)
                popen.assert_not_called()
            with mock.patch.object(indexd_runtime, "_SPAWN_GUARD_PATH", guard_path):
                indexd_runtime._clear_own_spawn_guard(force=True)
            self.assertFalse(guard_path.exists())
            self.assertIsNone(indexd_runtime._OWN_SPAWN_GUARD.snapshot)

    def test_valid_db_and_dead_delegate_clear_process_local_fallback(self) -> None:
        db = mock.Mock()
        messages = mock.Mock()
        messages.exists.return_value = True
        indexd_runtime._set_fts_delegated(True)
        with mock.patch.object(
                common, "MESSAGES_PATH", messages), \
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True), \
                mock.patch.object(corpusdb, "_stamp", return_value="stamp"), \
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")), \
                mock.patch.object(corpusdb, "_valid_db", return_value=db):
            self.assertIs(corpusdb.connect(), db)
        self.assertFalse(indexd_runtime._fts_delegated)

        indexd_runtime._set_fts_delegated(True)
        delegated_at = indexd_runtime._fts_delegated_at
        absent = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.ABSENT, None, None, None)
        with mock.patch.object(
                common.time, "monotonic",
                return_value=delegated_at + indexd_runtime._SPAWN_GUARD_S + 1.0), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner", return_value=absent):
            self.assertFalse(indexd_runtime.fts_delegation_active())
        self.assertFalse(indexd_runtime._fts_delegated)

    def test_resource_release_never_interrupts_indexing(self) -> None:
        self.assertFalse(indexd._resource_release_allowed("indexing", 0.01))
        self.assertFalse(indexd._resource_release_allowed("idle", None))
        self.assertFalse(indexd._resource_release_allowed("idle", 0.10))
        self.assertTrue(indexd._resource_release_allowed("idle", 0.09))

    def test_daemon_idle_clock_includes_live_source_activity(self) -> None:
        self.assertEqual(indexd._idle_reference(
            started=10.0, last_search=20.0, last_source_event=30.0), 30.0)
        self.assertEqual(indexd._idle_reference(
            started=40.0, last_search=20.0, last_source_event=30.0), 40.0)

    def test_new_daemon_gets_one_bounded_publication_wait(self) -> None:
        indexd_runtime.defer_foreground_refresh(
            indexd_runtime.REFRESH_DELEGATED_REASON)
        drifted = indexd_runtime.DriftReport("drifted", 1, 120.0)
        current = indexd_runtime.DriftReport("current")
        try:
            with mock.patch.object(
                    indexd_runtime, "_drift_report",
                    side_effect=[drifted, current]), \
                    mock.patch.object(
                        indexd_runtime, "_daemon_will_converge",
                        return_value=True), \
                    mock.patch.object(indexd_runtime.time, "sleep") as sleep:
                self.assertTrue(
                    indexd_runtime.wait_for_delegated_publication())
            sleep.assert_called_once()
        finally:
            indexd_runtime.defer_foreground_refresh("")

    def test_delegated_publication_wait_has_a_hard_timeout(self) -> None:
        indexd_runtime.defer_foreground_refresh(
            indexd_runtime.REFRESH_DELEGATED_REASON)
        try:
            with mock.patch.object(
                    indexd_runtime, "_drift_report",
                    return_value=indexd_runtime.DriftReport(
                        "drifted", 1, 120.0)), \
                    mock.patch.object(
                        indexd_runtime, "_daemon_will_converge",
                        return_value=True), \
                    mock.patch.object(indexd_runtime.time, "sleep") as sleep:
                self.assertFalse(indexd_runtime.wait_for_delegated_publication(
                    timeout_s=0.0))
            sleep.assert_not_called()
        finally:
            indexd_runtime.defer_foreground_refresh("")

    def test_semantic_backfill_uses_idle_capacity_and_throttles_pressure(self) -> None:
        state = {"state": "partial", "indexed": 1_000, "total": 20_000}
        fast = indexd.indexer._embedding_backfill_policy(
            state, recently_active=False, on_battery=False,
            memory_fraction=0.60, cpu_fraction=0.10)
        self.assertEqual(fast["mode"], "catch-up")
        self.assertEqual(fast["cap"], indexd.indexer.EMBED_CATCHUP_MAX_NEW)
        self.assertEqual(fast["interval"], indexd.indexer.EMBED_CATCHUP_CHECK_S)
        self.assertGreaterEqual(fast["threads"], 2)

        large = indexd.indexer._embedding_backfill_policy(
            {"state": "partial", "indexed": 2_000_000, "total": 10_000_000},
            recently_active=False, on_battery=False,
            memory_fraction=0.60, cpu_fraction=0.10)
        self.assertEqual(large["cap"],
                         indexd.indexer.EMBED_CATCHUP_MAX_GROWTH)

        active = indexd.indexer._embedding_backfill_policy(
            state, recently_active=True, on_battery=False,
            memory_fraction=0.60, cpu_fraction=0.10)
        self.assertEqual(active["mode"], "polite")
        self.assertEqual(active["cap"], 128)
        self.assertEqual(active["interval"], indexd.indexer.EMBED_CHECK_S)
        self.assertEqual(active["threads"], 2)

        battery = indexd.indexer._embedding_backfill_policy(
            state, recently_active=False, on_battery=True,
            memory_fraction=0.60, cpu_fraction=0.10)
        self.assertEqual(battery["mode"], "polite")
        self.assertEqual(battery["cap"], 128)
        self.assertEqual(battery["interval"], indexd.indexer.EMBED_CHECK_S)

        pressure = indexd.indexer._embedding_backfill_policy(
            state, recently_active=False, on_battery=False,
            memory_fraction=0.10, cpu_fraction=0.10)
        self.assertEqual(pressure["mode"], "memory-pressure")
        self.assertEqual(pressure["cap"], 0)

    def test_semantic_backfill_spawn_receives_only_scheduling_environment(self) -> None:
        with (mock.patch.object(semantic, "runtime_dependencies_available",
                               return_value=True),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value={"state": "partial"}),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(semantic, "read_embed_state", return_value={}),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "_needs_unverified_bundle_rebuild",
                                return_value=False),
              mock.patch.object(semantic.subprocess, "Popen") as popen):
            result = semantic.ensure_fresh_async(
                max_new=1000, spawn_env={
                    "AGREP_SEM_THREADS": "6", "AGREP_SEM_BG_NICE": "5",
                    "AGREP_SEM_BG_POLICY": "catch-up", "PATH": "poison",
                }, ignore_battery=True)
        self.assertEqual(result["state"], "running")
        command = popen.call_args.args[0]
        child_env = popen.call_args.kwargs["env"]
        self.assertIn("--max-new", command)
        self.assertEqual(command[command.index("--max-new") + 1], "1000")
        self.assertEqual(child_env["AGREP_SEM_THREADS"], "6")
        self.assertEqual(child_env["AGREP_SEM_BG_NICE"], "5")
        self.assertEqual(child_env["AGREP_SEM_BG_POLICY"], "catch-up")
        self.assertEqual(child_env["AGREP_EMBED_IGNORE_BATTERY"], "1")
        self.assertNotEqual(child_env.get("PATH"), "poison")

    def test_fresh_ingest_boot_replay_does_not_trip_hard_polite(self) -> None:
        # The embed policy's recently_active signal derives solely from the
        # watcher's _last_event_wall. A fresh mass-ingest replays history and
        # seeds tails pre-boot; none of that may read as live user activity.
        from hookless import live
        watcher = live.LiveWatcher(headless_indexd=True)
        self.assertEqual(watcher._last_event_wall, 0.0)
        now = time.time()
        watcher._mark_live("claude:seed-session", now)      # pre-boot growth
        watcher._mark_store_mutation(now)                    # pre-boot shrink
        stale_ts = (now - live._SNAPSHOT_EXPIRE_S - 60) * 1000
        watcher._emit_unlocked({                             # replayed history
            "agent": "claude", "session": "old", "type": "user",
            "ts": stale_ts, "text": "old history"})
        self.assertEqual(watcher._last_event_wall, 0.0)
        watcher._booted = True
        watcher._mark_live("claude:seed-session", now)       # real growth
        self.assertEqual(watcher._last_event_wall, now)

    def test_hard_polite_drain_cadence_default_is_30s(self) -> None:
        if os.environ.get("AGREP_EMBED_CHECK_S"):
            self.skipTest("explicit AGREP_EMBED_CHECK_S override")
        self.assertEqual(indexd.indexer.EMBED_CHECK_S, 30.0)

    def test_active_tiny_semantic_tail_is_the_only_deferred_shape(self) -> None:
        searchable = {
            "coherent": False, "searchable": True, "state": "partial",
            "migration_pending": False,
            "coverage": {"indexed": 10_000, "total": 10_003, "pending": 3},
        }
        with (mock.patch.object(indexd.indexer, "EMBED_ACTIVE_BATCH_ROWS", 128),
              mock.patch.object(indexd.indexer, "EMBED_ACTIVE_MAX_DEFER_S", 120.0)):
            deferred = indexd.indexer._embedding_refresh_admission(
                searchable, recently_active=True, deferred_for_s=30.0)
            cases = {
                "bootstrap": ({**searchable, "searchable": False,
                               "state": "missing-embeddings"}, True, 30.0),
                "migration": ({**searchable, "migration_pending": True},
                              True, 30.0),
                "idle": (searchable, False, 30.0),
                "large-backlog": ({**searchable,
                                   "coverage": {"pending": 128}}, True, 30.0),
                "max-defer": (searchable, True, 120.0),
                "integrity": ({**searchable, "searchable": False,
                               "state": "corrupt-embeddings"}, True, 30.0),
            }
            admitted = {
                name: indexd.indexer._embedding_refresh_admission(
                    coherence, recently_active=active,
                    deferred_for_s=deferred_for)
                for name, (coherence, active, deferred_for) in cases.items()
            }

        self.assertFalse(deferred.admit)
        self.assertEqual(deferred.state, "deferred_active_churn")
        self.assertEqual(
            deferred.reason, "searchable_tiny_tail_during_active_churn")
        self.assertEqual(deferred.pending, 3)
        self.assertTrue(all(decision.admit for decision in admitted.values()))
        self.assertEqual(admitted["max-defer"].reason, "max_defer_expired")
        self.assertEqual(
            admitted["large-backlog"].reason, "batch_threshold_reached")

    def test_active_tiny_tail_defers_spawn_and_records_typed_state(self) -> None:
        now = 10_000.0
        watcher = mock.Mock()
        watcher._last_event_wall = now
        owner = self._test_indexer(watcher)
        coherence = {
            "coherent": False, "searchable": True, "state": "partial",
            "migration_pending": False,
            "coverage": {"indexed": 10_000, "total": 10_003, "pending": 3},
        }
        policy = {"cap": 128, "threads": 2, "nice": 15, "mode": "polite"}
        with (mock.patch.object(indexd.indexer.time, "time", return_value=now),
              mock.patch.object(indexd.indexer.time, "monotonic", return_value=50.0),
              mock.patch.object(indexd.indexer, "EMBED_ACTIVE_BATCH_ROWS", 128),
              mock.patch.object(indexd.indexer, "EMBED_ACTIVE_MAX_DEFER_S", 120.0),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value=coherence),
              mock.patch.object(semantic, "ensure_fresh_async") as fresh):
            owner._refresh_embeddings(policy)

        fresh.assert_not_called()
        self.assertEqual(owner._embed_defer_started_mono, 50.0)
        self.assertEqual(owner.state["semantic_refresh"], {
            "state": "deferred_active_churn",
            "reason": "searchable_tiny_tail_during_active_churn",
            "pending": 3,
        })

    def test_active_tiny_tail_runs_when_max_defer_expires(self) -> None:
        now = 10_000.0
        watcher = mock.Mock()
        watcher._last_event_wall = now
        owner = self._test_indexer(watcher)
        owner._embed_defer_started_mono = 50.0
        owner.state["semantic_refresh"] = {
            "state": "deferred_active_churn", "reason": "old", "pending": 2}
        coherence = {
            "coherent": False, "searchable": True, "state": "partial",
            "migration_pending": False,
            "coverage": {"indexed": 10_000, "total": 10_003, "pending": 3},
        }
        policy = {"cap": 128, "threads": 2, "nice": 15, "mode": "polite"}
        with (mock.patch.object(indexd.indexer.time, "time", return_value=now),
              mock.patch.object(indexd.indexer.time, "monotonic", return_value=170.0),
              mock.patch.object(indexd.indexer, "EMBED_ACTIVE_BATCH_ROWS", 128),
              mock.patch.object(indexd.indexer, "EMBED_ACTIVE_MAX_DEFER_S", 120.0),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value=coherence),
              mock.patch.object(
                  semantic, "ensure_fresh_async",
                  return_value={"state": "running"}) as fresh):
            owner._refresh_embeddings(policy)

        fresh.assert_called_once_with(
            max_new=128,
            spawn_env={
                "AGREP_SEM_THREADS": "2", "AGREP_SEM_BG_NICE": "15",
                "AGREP_SEM_BG_POLICY": "polite",
            },
            ignore_battery=False)
        self.assertEqual(owner._embed_defer_started_mono, 0.0)
        self.assertNotIn("semantic_refresh", owner.state)

    def test_completed_generation_clears_prior_active_churn_deferral(self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = time.time()
        owner = self._test_indexer(watcher)
        owner._embed_defer_started_mono = 50.0
        owner.state["semantic_refresh"] = {
            "state": "deferred_active_churn", "reason": "old", "pending": 2}
        current = {
            "coherent": True, "searchable": True, "state": "current",
            "migration_pending": False,
            "coverage": {"indexed": 10_003, "total": 10_003, "pending": 0},
        }
        with (mock.patch.object(semantic, "embedding_coherence",
                                return_value=current),
              mock.patch.object(
                  semantic, "clear_superseded_embed_failure") as clear,
              mock.patch.object(semantic, "ensure_fresh_async") as fresh):
            owner._refresh_embeddings()

        clear.assert_called_once_with(current)
        fresh.assert_not_called()
        self.assertEqual(owner._embed_defer_started_mono, 0.0)
        self.assertNotIn("semantic_refresh", owner.state)

    def test_battery_bootstrap_uses_the_bounded_indexd_policy(self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = 0.0
        owner = self._test_indexer(watcher)
        calls: dict[str, object] = {}

        def capture(max_new=None, spawn_env=None, ignore_battery=False, **_kw):
            calls.update(max_new=max_new, spawn_env=spawn_env,
                         ignore_battery=ignore_battery)
            return {"state": "running"}

        policy = indexd.indexer._embedding_backfill_policy(
            {"state": "missing", "indexed": 0, "total": 20_000},
            recently_active=False, on_battery=True,
            memory_fraction=0.60, cpu_fraction=0.10)
        with (mock.patch.object(
                semantic, "embedding_coherence",
                return_value={"coherent": False, "searchable": False,
                              "state": "missing"}),
              mock.patch.object(semantic, "ensure_fresh_async",
                                side_effect=capture)):
            owner._refresh_embeddings(policy)
        self.assertEqual(calls["max_new"], 128)
        env = calls["spawn_env"]
        self.assertEqual(env["AGREP_SEM_THREADS"], "2")
        self.assertEqual(env["AGREP_SEM_BG_NICE"], "15")
        self.assertEqual(env["AGREP_SEM_BG_POLICY"], "polite")
        self.assertTrue(calls["ignore_battery"])

    def test_critical_memory_prevents_even_a_stale_bootstrap(self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = 0.0
        owner = self._test_indexer(watcher)
        owner._embed_defer_started_mono = 50.0
        owner.state["semantic_refresh"] = {
            "state": "deferred_active_churn", "reason": "old", "pending": 2}
        policy = indexd.indexer._embedding_backfill_policy(
            {"state": "missing", "indexed": 0, "total": 20_000},
            recently_active=False, on_battery=True,
            memory_fraction=0.10, cpu_fraction=0.10)
        self.assertEqual(policy["cap"], 0)
        with (mock.patch.object(
                semantic, "embedding_coherence",
                return_value={"coherent": False, "searchable": False,
                              "state": "missing"}),
              mock.patch.object(semantic, "ensure_fresh_async") as fresh):
            owner._refresh_embeddings(policy)
        fresh.assert_not_called()
        self.assertEqual(owner._embed_defer_started_mono, 0.0)
        self.assertNotIn("semantic_refresh", owner.state)

    def test_searchable_battery_backfill_keeps_the_child_governor(self) -> None:
        watcher = mock.Mock()
        watcher._last_event_wall = 0.0
        owner = self._test_indexer(watcher)
        policy = indexd.indexer._embedding_backfill_policy(
            {"state": "partial", "indexed": 128, "total": 20_000},
            recently_active=False, on_battery=True,
            memory_fraction=0.60, cpu_fraction=0.10)
        calls: dict[str, object] = {}

        def capture(**kwargs):
            calls.update(kwargs)
            return {"state": "running"}

        with (mock.patch.object(
                semantic, "embedding_coherence",
                return_value={"coherent": False, "searchable": True,
                              "state": "partial"}),
              mock.patch.object(
                semantic, "ensure_fresh_async", side_effect=capture)):
            owner._refresh_embeddings(policy)
        self.assertEqual(calls["max_new"], 128)
        self.assertFalse(calls["ignore_battery"])

    def test_single_embed_crash_backs_off_seconds_not_the_cap(self) -> None:
        failed = {"state": "failed", "finished_at": time.time(),
                  "failures": 1, "error": "boom"}
        with (mock.patch.object(semantic, "runtime_dependencies_available",
                               return_value=True),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value={"state": "stale"}),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(semantic, "read_embed_state",
                                return_value=failed),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic.subprocess, "Popen") as popen):
            backing_off = semantic.ensure_fresh_async()
        popen.assert_not_called()
        self.assertEqual(backing_off["state"], "failed")
        self.assertEqual(backing_off["backoff_s"], semantic.BOOTSTRAP_RETRY_BASE_S)
        self.assertLessEqual(
            backing_off["retry_at"],
            failed["finished_at"] + semantic.BOOTSTRAP_RETRY_BASE_S)

        # once the short window has passed, the spawn goes out again
        failed_earlier = {**failed,
                          "finished_at": time.time()
                          - semantic.BOOTSTRAP_RETRY_BASE_S - 1}
        with (mock.patch.object(semantic, "runtime_dependencies_available",
                               return_value=True),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value={"state": "stale"}),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(semantic, "read_embed_state",
                                return_value=failed_earlier),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "_needs_unverified_bundle_rebuild",
                                return_value=False),
              mock.patch.object(semantic.subprocess, "Popen") as popen):
            retried = semantic.ensure_fresh_async()
        popen.assert_called_once()
        self.assertEqual(retried["state"], "running")

    def test_bootstrap_backoff_steps_to_the_cap_and_decays(self) -> None:
        now = time.time()
        prior, steps = {"state": "idle"}, []
        for _ in range(9):
            prior = {"state": "failed", "finished_at": now,
                     "failures": semantic.embed_failure_streak(prior, now)}
            steps.append(int(semantic.bootstrap_backoff_s(prior)))
        base, cap = semantic.BOOTSTRAP_RETRY_BASE_S, semantic.BOOTSTRAP_RETRY_S
        self.assertEqual(steps, [min(cap, base * (1 << i)) for i in range(9)])
        # a legacy failed state without the field also costs seconds, not 600
        self.assertEqual(semantic.bootstrap_backoff_s({"state": "failed"}),
                         semantic.BOOTSTRAP_RETRY_BASE_S)
        # decay: a streak past twice the cap is stale and starts over
        self.assertEqual(semantic.embed_failure_streak(
            {"state": "failed", "failures": 8,
             "finished_at": now - 2 * semantic.BOOTSTRAP_RETRY_S - 1}, now), 1)
        # a pass that died while running inherits the streak it rode in with
        self.assertEqual(semantic.embed_failure_streak(
            {"state": "running", "failures": 3, "started_at": now - 30},
            now), 4)
        # running priors never decay: pass DURATION is not quiet time, so a
        # crash loop of >20-minute doomed passes still steps to the cap
        self.assertEqual(semantic.embed_failure_streak(
            {"state": "running", "failures": 7,
             "started_at": now - 2 * semantic.BOOTSTRAP_RETRY_S - 600}, now), 8)
        # any successful publish resets the streak
        self.assertEqual(
            semantic.embed_failure_streak({"state": "ready"}, now), 1)

    def test_worker_death_without_publishing_steps_the_streak(self) -> None:
        dead = {"state": "running", "started_at": time.time(),
                "failures": 2, "pid": 12345}
        written = {}
        state_path = mock.MagicMock()
        state_path.write_text = lambda text, encoding=None: written.update(
            json.loads(text))
        with (mock.patch.object(semantic, "runtime_dependencies_available",
                               return_value=True),
              mock.patch.object(semantic, "embedding_coherence",
                                return_value={"state": "stale"}),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(semantic, "read_embed_state",
                                return_value=dead),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(semantic, "embed_state_path",
                                return_value=state_path),
              mock.patch.object(semantic.subprocess, "Popen") as popen):
            synthesized = semantic.ensure_fresh_async()
        popen.assert_not_called()
        self.assertEqual(written["state"], "failed")
        self.assertEqual(written["failures"], 3)
        self.assertEqual(synthesized["backoff_s"],
                         4 * semantic.BOOTSTRAP_RETRY_BASE_S)

    def test_no_daemon_semantic_refresh_never_spawns_background_children(self) -> None:
        with mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                mock.patch.object(semantic, "runtime_dependencies_available",
                                  return_value=True), \
                mock.patch.object(semantic, "embedding_coherence") as coherence, \
                mock.patch.object(common, "open_bounded_log") as open_log, \
                mock.patch.object(semantic.subprocess, "Popen") as popen:
            refresh = semantic.ensure_fresh_async(max_new=100)
            refs = semantic.ensure_refs_async()
        self.assertEqual(refresh["state"], "disabled")
        self.assertEqual(refs["state"], "disabled")
        self.assertIn("AGREP_NO_DAEMON", refresh["reason"])
        coherence.assert_not_called()
        open_log.assert_not_called()
        popen.assert_not_called()

    def test_windows_first_cpu_sample_stays_polite(self) -> None:
        state = {"state": "partial", "indexed": 1_000, "total": 20_000}
        with mock.patch.object(indexd.indexer, "WIN", True):
            windows = indexd.indexer._embedding_backfill_policy(
                state, recently_active=False, on_battery=False,
                memory_fraction=0.60, cpu_fraction=None)
        self.assertEqual(windows["mode"], "polite")
        self.assertEqual(windows["cap"], 128)
        self.assertEqual(windows["interval"], indexd.indexer.EMBED_CHECK_S)
        self.assertEqual(windows["threads"], 2)

        with mock.patch.object(indexd.indexer, "WIN", False):
            posix = indexd.indexer._embedding_backfill_policy(
                state, recently_active=False, on_battery=False,
                memory_fraction=0.60, cpu_fraction=None)
        self.assertEqual(posix["mode"], "catch-up")

    def test_semantic_residency_adapts_to_scale_and_memory(self) -> None:
        common.EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # a segment manifest overrides the matrix size, so the size cases below
        # only mean what they say when no earlier one is lying around
        (common.EMBEDDINGS_PATH.parent / "embeddings.meta").unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"AGREP_SEM_IDLE_S": ""}):
            with mock.patch.object(resources, "available_memory_fraction", return_value=0.50):
                for size, one, repeated in (
                        (255 * 1024 ** 2, 120.0, 600.0),
                        (256 * 1024 ** 2, 60.0, 180.0),
                        (1024 * 1024 ** 2, 30.0, 90.0)):
                    with common.EMBEDDINGS_PATH.open("wb") as handle:
                        handle.truncate(size)
                    self.assertEqual(common.semantic_idle_seconds(1), one)
                    self.assertEqual(common.semantic_idle_seconds(2), repeated)
                meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
                meta.write_text(json.dumps({
                    "version": 2,
                    "segments": [{"artifacts": {"f32": {"size": 1024 ** 3}}}],
                }), encoding="utf-8")
                try:
                    self.assertEqual(common.semantic_idle_seconds(1), 30.0)
                finally:
                    meta.unlink(missing_ok=True)
            with mock.patch.object(resources, "available_memory_fraction", return_value=0.05):
                self.assertEqual(common.semantic_idle_seconds(2), 5.0)
            with mock.patch.object(resources, "available_memory_fraction", return_value=0.12):
                self.assertEqual(common.semantic_idle_seconds(2), 30.0)
            with mock.patch.object(resources, "available_memory_fraction", return_value=0.20):
                self.assertEqual(common.semantic_idle_seconds(2), 60.0)
        with mock.patch.dict(os.environ, {"AGREP_SEM_IDLE_S": "77"}), \
                mock.patch.object(resources, "available_memory_fraction", return_value=0.50):
            self.assertEqual(common.semantic_idle_seconds(1), 77.0)

    def test_semworker_samples_memory_pressure_coarsely_while_idle(self) -> None:
        owner = semworker.SemanticWorkerServer.__new__(semworker.SemanticWorkerServer)
        owner.requests = 2
        owner._idle_sample_at = 0.0
        owner._idle_sample_requests = -1
        owner._idle_sample_value = 0.0
        with mock.patch.object(semworker.time, "monotonic",
                               side_effect=(100.0, 101.0, 129.0, 131.0)), \
                mock.patch.object(common, "semantic_idle_seconds",
                                  return_value=180.0) as sample:
            self.assertEqual([owner._idle_limit() for _ in range(4)],
                             [180.0, 180.0, 180.0, 180.0])
        self.assertEqual(sample.call_count, 2)

    def test_semworker_build_id_uses_repo_aware_package_version(self) -> None:
        with mock.patch.object(common, "package_version", return_value="checkout-a"):
            first = semworker._compute_build_id()
        with mock.patch.object(common, "package_version", return_value="checkout-b"):
            second = semworker._compute_build_id()
        self.assertNotEqual(first, second)

    def test_semworker_build_id_covers_owner_runtime(self) -> None:
        original = Path.read_bytes

        def compute(owner: str, marker: bytes) -> str:
            def read(path: Path) -> bytes:
                if path.name == owner:
                    return marker
                return original(path)

            with mock.patch.object(Path, "read_bytes", read):
                return semworker._compute_build_id()

        for owner in (
                "ownerfile.py", "session_context.py",
                "embedding_store.py", "fileops.py", "index_lock.py",
        ):
            with self.subTest(owner=owner):
                self.assertNotEqual(
                    compute(owner, b"owner-a"),
                    compute(owner, b"owner-b"))

    def test_semworker_validates_caller_family_exclusions(self) -> None:
        request = {
            "query": "deadlock",
            "level": "hybrid",
            "k": 4,
            "filters": {
                "exclude_session": "child",
                "exclude_session_from_turn": 12,
                "_family_diverse": True,
            },
        }
        _query, _level, _k, filters, _timing = semworker._validate_request(
            request)
        self.assertEqual(filters["exclude_session"], "child")
        self.assertEqual(filters["exclude_session_from_turn"], 12)
        request["filters"]["_exclude_sessions"] = ("root",)
        _query, _level, _k, filters, _timing = semworker._validate_request(
            request)
        self.assertEqual(filters["_exclude_sessions"], ("root",))
        request["filters"]["_exclude_sessions"] = tuple(
            f"session-{index}" for index in range(5))
        with self.assertRaisesRegex(ValueError, "invalid semantic filter"):
            semworker._validate_request(request)
        request["filters"]["_exclude_sessions"] = ("root",)
        request["filters"]["exclude_session"] = "x" * 1025
        with self.assertRaisesRegex(ValueError, "invalid semantic filter"):
            semworker._validate_request(request)
        request["filters"] = {"exclude_session_from_turn": 12}
        with self.assertRaisesRegex(ValueError, "invalid semantic filter"):
            semworker._validate_request(request)

    def test_incompatible_self_worker_never_probes_stale_endpoint(self) -> None:
        owner = semworker._acquire_worker_lock(tree_bound=True)
        self.assertIsNotNone(owner)
        try:
            lock = json.loads(owner.snapshot.raw)
            record = {
                "version": semworker.PROTOCOL, "pid": os.getpid(), "port": 9,
                "token": "a" * 64,
                "process_start": lock["process_start"],
                "started_at": time.time(), "tree_bound": True,
                "owner_nonce": lock["nonce"], "build_id": "old-build",
                "capabilities": list(semworker.CAPABILITIES),
            }
            semworker.descriptor_path().write_text(
                json.dumps(record), encoding="utf-8")
            with mock.patch.object(semworker, "_request_worker_stop") as stop:
                self.assertIsNone(semworker._reconcile_descriptor())
            self.assertFalse(semworker.descriptor_path().exists())
            stop.assert_not_called()
        finally:
            owner.release(tombstone=True, require_stable_mtime=True)

    def test_semworker_shutdown_releases_mmaps_before_process_exit(self) -> None:
        owner = semworker.SemanticWorkerServer.__new__(semworker.SemanticWorkerServer)
        owner.stop = mock.Mock()
        owner.stop.is_set.return_value = True
        owner.http = mock.Mock()
        owner._release_lock = semworker.threading.Lock()
        owner._resources_released = False
        owner._semantic_loaded = True
        owner.owner_nonce = "a" * 32
        order = []
        body = b"fixture-owner"
        owner.descriptor_snapshot = ownerfile.Snapshot(
            (1, 2, len(body), 3), 0.0, body)
        owner.http.server_close.side_effect = lambda: order.append("close")
        with mock.patch.object(
                semantic, "release",
                side_effect=lambda: order.append("release") or True) as release, \
                mock.patch.object(
                    semworker, "_discard_record",
                    side_effect=lambda _path, _snapshot: order.append("remove")) as remove, \
                mock.patch.object(
                    semworker, "_discard_retire_handoff",
                    side_effect=lambda _nonce: order.append("retire")) as retire:
            owner.serve()
        release.assert_called_once_with()
        self.assertTrue(owner._resources_released)
        self.assertEqual(order, ["release", "remove", "retire", "close"])
        owner.http.server_close.assert_called_once_with()
        remove.assert_called_once_with(
            semworker.descriptor_path(), owner.descriptor_snapshot)
        retire.assert_called_once_with(owner.owner_nonce)

    def test_semworker_release_failure_remains_visible_and_retriable(self) -> None:
        owner = semworker.SemanticWorkerServer.__new__(semworker.SemanticWorkerServer)
        owner._release_lock = semworker.threading.Lock()
        owner._resources_released = False
        owner._semantic_loaded = True
        with mock.patch.object(
                semantic, "release", side_effect=(RuntimeError("mapped"), True)) as release:
            self.assertFalse(owner._release_resources())
            self.assertTrue(owner._release_resources())
        self.assertEqual(release.call_count, 2)

    def test_semworker_stop_does_not_remove_replacement_lock(self) -> None:
        descriptor = {"pid": 11, "process_start": "old", "port": 9, "token": "x"}
        replacement = {"pid": 22, "process_start": "new", "tree_bound": False}
        old_raw = b"old"
        new_raw = b"new"
        old_snapshot = ownerfile.Snapshot(
            (1, 2, len(old_raw), 3), 0.0, old_raw)
        new_snapshot = ownerfile.Snapshot(
            (1, 4, len(new_raw), 5), 0.0, new_raw)
        replacement_owner = semworker._WorkerOwner(
            semworker._WorkerOwnerState.EXACT, replacement, new_snapshot)
        with (mock.patch.object(
                semworker, "_reconcile_descriptor",
                side_effect=((descriptor, old_snapshot), None, None)),
              mock.patch.object(
                  semworker, "_inspect_worker_lock",
                  side_effect=(
                      replacement_owner, replacement_owner,
                      semworker._WorkerOwner(
                          semworker._WorkerOwnerState.ABSENT))),
              mock.patch.object(
                  semworker, "_request_and_drain_worker",
                  return_value=(True, (descriptor, old_snapshot), True)),
              mock.patch.object(
                  semworker, "_verify_record",
                  side_effect=ownerfile.OwnershipLost("released")),
              mock.patch.object(semworker, "_discard_record") as remove):
            result = semworker.stop_worker_and_wait()
        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        remove.assert_called_once_with(
            semworker.descriptor_path(), old_snapshot)

    def test_semworker_stop_waits_for_starting_descriptor(self) -> None:
        lock = {
            "pid": 11, "process_start": "same", "tree_bound": True,
            "nonce": "a" * 32,
        }
        descriptor = {
            **lock, "version": semworker.PROTOCOL,
            "port": 9, "token": "b" * 64, "owner_nonce": "a" * 32,
            "started_at": 1.0,
            "build_id": semworker.WORKER_BUILD_ID,
            "capabilities": list(semworker.CAPABILITIES),
        }
        raw = b"descriptor"
        snapshot = ownerfile.Snapshot(
            (1, 2, len(raw), 3), 0.0, raw)
        with (mock.patch.object(
                semworker, "_reconcile_descriptor",
                return_value=(descriptor, snapshot)),
              mock.patch.object(
                  semworker, "_process_owner",
                  side_effect=(ownerfile.ProcessOwner.EXACT_LIVE,
                               ownerfile.ProcessOwner.DEAD)),
              mock.patch.object(
                  semworker, "_request_worker_stop", return_value=True) as stop,
              mock.patch.object(
                  semworker, "_terminate_worker_tree", return_value=True) as drain,
              mock.patch.object(
                  semworker.time, "monotonic", return_value=100.0)):
            exited, found, acknowledged = semworker._request_and_drain_worker(
                lock, None, 0.1)
        self.assertTrue(exited)
        self.assertEqual(found, (descriptor, snapshot))
        self.assertTrue(acknowledged)
        stop.assert_called_once()
        self.assertIs(stop.call_args.args[0], descriptor)
        self.assertGreater(stop.call_args.kwargs["timeout_s"], 0.0)
        self.assertLessEqual(stop.call_args.kwargs["timeout_s"], 0.1)
        drain.assert_called_once()
        self.assertIs(drain.call_args.args[0], lock)
        self.assertGreater(drain.call_args.args[1], 0.0)
        self.assertLessEqual(drain.call_args.args[1], 0.1)

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object contract")
    def test_windows_worker_force_exit_releases_mapped_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-worker-job-") as raw:
            root = Path(raw)
            mapped = root / "mapped.bin"
            ready = root / "ready.txt"
            mapped.write_bytes(b"x" * 4096)
            child = (
                "import mmap,sys,time\n"
                "from pathlib import Path\n"
                "stream=open(sys.argv[1],'r+b')\n"
                "mapping=mmap.mmap(stream.fileno(),0)\n"
                "Path(sys.argv[2]).write_text('ready',encoding='utf-8')\n"
                "while True: time.sleep(1)\n"
            )
            parent = (
                "import common,subprocess,sys,time\n"
                "if not common.bind_descendants_to_process_lifetime(): raise SystemExit(4)\n"
                "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],"
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "while True: time.sleep(1)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent, child, str(mapped), str(ready)],
                cwd=Path(__file__).resolve().parent,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 5.0
                while not ready.exists() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), f"job fixture exited {process.poll()}")
                birth = common.process_start_identity(process.pid)
                self.assertIsNotNone(birth)
                self.assertTrue(common.terminate_exact_process(process.pid, str(birth)))
                deadline = time.monotonic() + 5.0
                while mapped.exists() and time.monotonic() < deadline:
                    try:
                        mapped.unlink()
                    except PermissionError:
                        time.sleep(0.02)
                self.assertFalse(mapped.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_semworker_status_samples_rss_only_for_resource_probes(self) -> None:
        rec = {"pid": 4242, "owner": "disposable", "build_id": "current",
               "started_at": time.time() - 5}
        observation = semworker._DescriptorObservation(
            semworker._DescriptorState.READY, rec, mock.Mock(), True)
        with mock.patch.object(
                semworker, "_inspect_descriptor",
                return_value=observation), \
                mock.patch.object(common, "process_rss_bytes",
                                  return_value=123_456) as rss:
            cheap = semworker.resident_status()
            detailed = semworker.resident_status(include_resources=True)
        self.assertIsNone(cheap["rss_bytes"])
        self.assertEqual(detailed["rss_bytes"], 123_456)
        rss.assert_called_once_with(4242)

    def test_semworker_legacy_pid_cannot_be_signalled_or_poison_lock(self) -> None:
        legacy = {
            "version": semworker.PROTOCOL - 1, "pid": 4242, "port": 9,
            "token": "a" * 64, "build_id": "legacy",
            "capabilities": [],
        }
        semworker.descriptor_path().write_text(json.dumps(legacy), encoding="utf-8")
        with (mock.patch.object(common, "pid_alive", return_value=True),
              mock.patch.object(common, "process_start_identity",
                                return_value="reused-process"),
              mock.patch.object(semworker, "_terminate_worker_tree") as terminate,
              mock.patch.object(semworker, "_request_worker_stop", return_value=False)):
            self.assertIsNone(semworker._reconcile_descriptor())
            terminate.assert_not_called()
        self.assertTrue(semworker.descriptor_path().exists())
        old = time.time() - semworker.START_CLAIM_GRACE_S - 0.1
        os.utime(semworker.descriptor_path(), (old, old))
        with mock.patch.object(semworker, "_terminate_worker_tree") as terminate:
            self.assertIsNone(semworker._reconcile_descriptor())
            terminate.assert_not_called()
        self.assertFalse(semworker.descriptor_path().exists())

        semworker.worker_lock_path().write_text(
            json.dumps({"pid": 4242, "started_at": time.time()}), encoding="utf-8")
        with (mock.patch.object(common, "pid_alive", return_value=True),
              mock.patch.object(common, "process_start_identity",
                                return_value="reused-process")):
            owner = semworker._acquire_worker_lock()
        self.assertIsNone(owner)
        self.assertTrue(semworker.worker_lock_path().exists())
        old = time.time() - semworker.START_CLAIM_GRACE_S - 0.1
        os.utime(semworker.worker_lock_path(), (old, old))
        with mock.patch.object(common, "pid_alive", return_value=False):
            owner = semworker._acquire_worker_lock()
        self.assertIsNotNone(owner)
        if owner is not None:
            owner.release(tombstone=True, require_stable_mtime=True)

    def test_semworker_failed_spawn_falls_back_without_timeout(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 7
        claim = mock.Mock()
        with mock.patch.dict(os.environ, {
                "AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "_reconcile_descriptor", return_value=None), \
                mock.patch.object(semworker, "_acquire_start_claim", return_value=claim), \
                mock.patch.object(semworker, "_spawn_worker", return_value=process), \
                mock.patch.object(semworker, "_release_start_claim") as release, \
                mock.patch.object(semworker.time, "sleep") as sleep:
            self.assertIsNone(semworker._ensure_worker())
        process.poll.assert_called_once_with()
        sleep.assert_not_called()
        claim.verify.assert_called_once_with(require_stable_mtime=True)
        release.assert_called_once_with(claim)

    def test_semworker_connect_refusal_uses_published_replacement(self) -> None:
        old = {"pid": 42, "process_start": "old-birth"}
        new = {"pid": 43, "process_start": "new-birth"}
        result = {"results": [], "score_kind": "cosine"}
        with mock.patch.object(semworker, "_ensure_worker",
                               return_value=old) as ensure, \
                mock.patch.object(semworker, "_worker_request",
                                  side_effect=(None, result)), \
                mock.patch.object(semworker, "_reconcile_descriptor",
                                  return_value=(new, "snapshot")), \
                mock.patch.object(semworker, "_terminate_worker_tree") as terminate, \
                mock.patch.object(semworker.time, "sleep") as sleep:
            got = semworker.search_worker("query", level="hybrid", k=3)
        self.assertEqual(got, result)
        ensure.assert_called_once_with()
        sleep.assert_called_once_with(0.02)
        terminate.assert_not_called()

    def test_windows_resume_keeps_metacharacters_out_of_command_text(self) -> None:
        cwd = r"C:\work\a&whoami^(test)!%TEMP%"
        argv = ["opencode.cmd", cwd, "--session", "ses_abcdef"]
        host, env = native._windows_resume_host(argv, cwd)
        self.assertNotIn(cwd, " ".join(host))
        decoded = json.loads(base64.b64decode(env["AGREP_RESUME_PAYLOAD"]))
        self.assertEqual(decoded, {"argv": argv, "cwd": cwd})
        self.assertNotIn("cmd", Path(host[0]).stem.lower())

        in_place, in_place_env = native._windows_resume_host(argv, cwd, keep_open=False)
        self.assertNotIn("-NoExit", in_place)
        self.assertNotIn(cwd, " ".join(in_place))
        self.assertEqual(in_place_env["AGREP_RESUME_PAYLOAD"],
                         env["AGREP_RESUME_PAYLOAD"])

    def test_windows_batch_resume_in_place_uses_encoded_host(self) -> None:
        cwd = r"C:\work\a&whoami^(test)!%TEMP%"
        runner = mock.Mock(returncode=9)
        host = ["powershell.exe", "-Command", native._POWERSHELL_RESUME]
        env = {"AGREP_RESUME_PAYLOAD": "opaque"}
        with mock.patch.object(native.sys, "platform", "win32"), \
                mock.patch.object(native, "resolve_cwd", return_value=cwd), \
                mock.patch.object(native.shutil, "which", return_value="opencode.cmd"), \
                mock.patch.object(native, "_windows_resume_host",
                                  return_value=(host, env)) as encode, \
                mock.patch.object(native, "run_owned_process",
                                  return_value=runner) as run, \
                mock.patch("builtins.print"):
            self.assertEqual(native.resume_in_place("opencode", "ses_abcdef"), 9)
        encode.assert_called_once_with(
            ["opencode.cmd", cwd, "--session", "ses_abcdef"], cwd, keep_open=False)
        run.assert_called_once_with(
            host, cwd=cwd, env=env, relay_signals=True)

    def test_windows_terminal_never_receives_store_controlled_cwd(self) -> None:
        cwd = r"C:\work\path;new-tab --title injected"
        host = ["powershell.exe", "-Command", native._POWERSHELL_RESUME]
        env = {"AGREP_RESUME_PAYLOAD": "opaque"}
        with mock.patch.object(native, "_windows_resume_host", return_value=(host, env)), \
                mock.patch.object(native.shutil, "which", return_value="wt.exe"), \
                mock.patch.object(native.subprocess, "Popen") as launch:
            self.assertEqual(native._spawn_windows(["codex", "resume", "session"], cwd),
                             "wt-tab")
            first = launch.call_args.args[0]
            self.assertEqual(first, ["wt.exe", "-w", "0", "new-tab", *host])
            self.assertNotIn(cwd, first)

            self.assertEqual(native._spawn_windows(
                ["codex", "resume", "session"], cwd, same_window=False), "wt")
            second = launch.call_args.args[0]
            self.assertEqual(second, ["wt.exe", "new-tab", *host])
            self.assertNotIn(cwd, second)

    def test_windows_owned_bootstrap_binds_before_agent_launch(self) -> None:
        payload = native._owned_windows_payload(
            ["codex.exe", "resume", "session-safe"], r"C:\work")
        child = mock.Mock()
        child.wait.return_value = 0
        with mock.patch.dict(os.environ, {native._OWNED_PAYLOAD: payload}), \
                mock.patch.object(
                    native.process_util, "bind_descendants_to_process_lifetime",
                    return_value=False), \
                mock.patch.object(native.subprocess, "Popen") as launch, \
                mock.patch("builtins.print"):
            self.assertEqual(native._owned_windows_child(), 125)
        launch.assert_not_called()

        with mock.patch.dict(os.environ, {native._OWNED_PAYLOAD: payload}), \
                mock.patch.object(
                    native.process_util, "bind_descendants_to_process_lifetime",
                    return_value=True), \
                mock.patch.object(
                    native.subprocess, "Popen", return_value=child) as launch:
            self.assertEqual(native._owned_windows_child(), 0)
        launch.assert_called_once_with(
            ["codex.exe", "resume", "session-safe"], cwd=r"C:\work")

    def test_windows_owned_bootstrap_resolves_from_unrelated_cwd(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env[native._OWNED_PAYLOAD] = ""
        with tempfile.TemporaryDirectory() as unrelated:
            result = subprocess.run(
                native._owned_bootstrap_argv(), cwd=unrelated, env=env,
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("owned child payload error", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_resume_rejects_invalid_session_before_store_or_process_access(self) -> None:
        for session in ("abcde&whoami", "abcdef/../../escape", "abcdef\n"):
            with self.subTest(session=session):
                with (mock.patch.object(native, "resolve_cwd") as resolve,
                      mock.patch.object(native.shutil, "which") as which,
                      mock.patch.object(native.subprocess, "run") as run):
                    self.assertEqual(native.resume_in_place("codex", session), 2)
                    resolve.assert_not_called()
                    which.assert_not_called()
                    run.assert_not_called()
                    self.assertFalse(native.open_session("codex", session)["ok"])

    def test_claude_resume_validates_executable_before_store_mutation(self) -> None:
        with mock.patch.object(native.shutil, "which", return_value=None), \
                mock.patch.object(native, "resolve_cwd") as resolve, \
                mock.patch.object(native, "_claude_link_session") as link, \
                mock.patch("builtins.print"):
            self.assertEqual(native.resume_in_place("claude", "session-safe"), 127)
            self.assertFalse(native.open_session("claude", "session-safe")["ok"])
        resolve.assert_not_called()
        link.assert_not_called()

    def test_claude_resume_refuses_dangling_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(native, "HOME", td):
            root = Path(td) / ".claude" / "projects"
            source_dir = root / "source"
            source_dir.mkdir(parents=True)
            session = "session-safe"
            (source_dir / f"{session}.jsonl").write_text("safe\n", encoding="utf-8")
            cwd = str(Path(td) / "work")
            destination = root / native._claude_slug(cwd) / f"{session}.jsonl"
            destination.parent.mkdir()
            try:
                destination.symlink_to(Path(td) / "missing.jsonl")
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(OSError, "unsafe Claude resume destination"):
                native._claude_link_session(session, cwd)
            self.assertTrue(destination.is_symlink())

    def test_claude_resume_refuses_dangling_link_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(native, "HOME", td):
            root = Path(td) / ".claude" / "projects"
            source_dir = root / "source"
            source_dir.mkdir(parents=True)
            session = "session-safe"
            (source_dir / f"{session}.jsonl").write_text("safe\n", encoding="utf-8")
            cwd = str(Path(td) / "work")
            destination_dir = root / native._claude_slug(cwd)
            try:
                destination_dir.symlink_to(
                    Path(td) / "missing-dir", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(OSError, "unsafe Claude resume destination parent"):
                native._claude_link_session(session, cwd)
            self.assertTrue(destination_dir.is_symlink())

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_owned_process_normalizes_signal_exit_and_drains_descendants(self) -> None:
        signalled = native.run_owned_process([
            sys.executable, "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"],
            cwd=os.getcwd())
        self.assertEqual(signalled.returncode, 143)

        with tempfile.TemporaryDirectory() as td:
            pid_path = Path(td) / "grandchild.pid"
            grandchild = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            root = (
                "import os,sys,time\n"
                f"os.spawnv(os.P_NOWAIT,sys.executable,[sys.executable,'-c',{grandchild!r}])\n"
                "deadline=time.monotonic()+5\n"
                f"while not os.path.exists({str(pid_path)!r}) and "
                "time.monotonic()<deadline: time.sleep(0.01)\n"
                f"raise SystemExit(0 if os.path.exists({str(pid_path)!r}) else 2)\n")
            completed = native.run_owned_process(
                [sys.executable, "-c", root], cwd=td)
            self.assertEqual(completed.returncode, 0)
            grandchild_pid = int(pid_path.read_text(encoding="ascii"))
            self.assertFalse(common.pid_alive(grandchild_pid))

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_owned_drain_accepts_a_zombie_only_group(self) -> None:
        process = mock.Mock(pid=4242, returncode=-signal.SIGTERM)
        process.poll.return_value = -signal.SIGTERM
        with mock.patch.object(
                native, "_posix_group_active", return_value=False), \
                mock.patch.object(native.os, "killpg", create=True) as killpg:
            self.assertTrue(native._drain_owned(process))
        killpg.assert_not_called()

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_owned_drain_never_signals_a_recycled_pgid(self) -> None:
        # The leader is reaped but its pid is occupied again: the numeric
        # pgid belongs to a stranger now, so the drain must not touch it.
        process = mock.Mock(pid=4242, returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(
                native, "_posix_group_active", return_value=True), \
                mock.patch.object(
                    native.process_util, "pid_alive", return_value=True), \
                mock.patch.object(native.os, "killpg", create=True) as killpg:
            self.assertTrue(native._drain_owned(process))
        killpg.assert_not_called()

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_owned_drain_still_reaps_true_orphans_of_a_dead_leader(self) -> None:
        process = mock.Mock(pid=4242, returncode=0)
        process.poll.return_value = 0
        actives = iter((True, False, False, False))
        with mock.patch.object(
                native, "_posix_group_active",
                side_effect=lambda _group: next(actives, False)), \
                mock.patch.object(
                    native.process_util, "pid_alive", return_value=False), \
                mock.patch.object(
                    native.process_util, "process_start_identity",
                    return_value=None), \
                mock.patch.object(native.os, "killpg", create=True) as killpg:
            self.assertTrue(native._drain_owned(process))
        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_owned_tick_failure_drains_exactly_once(self) -> None:
        process = mock.Mock(pid=4242)
        with mock.patch.object(
                native, "_launch_owned", return_value=process), \
                mock.patch.object(native, "_drain_owned", return_value=True) as drain:
            with self.assertRaisesRegex(RuntimeError, "cancel hook"):
                native.run_owned_process(
                    ["hook"], cwd=os.getcwd(),
                    tick=lambda _process: (_ for _ in ()).throw(
                        RuntimeError("cancel hook")))
        drain.assert_called_once_with(process, signal.SIGTERM)

    def test_fast_capture_still_emits_a_complete_lifecycle(self) -> None:
        watcher = mock.Mock()
        watcher.sessions = {}
        with mock.patch.object(
                capture, "_agent_new_session_argv",
                return_value=[sys.executable, "-c", "pass"]), \
                mock.patch("hookless.live.watcher", return_value=watcher):
            self.assertEqual(capture.run_captured("codex", [], cwd=os.getcwd()), 0)
        self.assertEqual(len(watcher.sessions), 1)
        session = next(iter(watcher.sessions.values()))
        self.assertFalse(session["working"])
        self.assertEqual(session["state"], "done")

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_owned_wrapper_forwards_term_and_exits_canonically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_path = Path(td) / "child.pid"
            child = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            driver = (
                "from hookless import native; import os,sys; "
                f"r=native.run_owned_process([sys.executable,'-c',{child!r}],"
                "cwd=os.getcwd(),relay_signals=True); raise SystemExit(r.returncode)")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            wrapper = subprocess.Popen(
                [sys.executable, "-c", driver], cwd=td, env=env,
                start_new_session=True)
            deadline = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_path.exists())
            child_pid = self._read_published_pid(pid_path)
            os.kill(wrapper.pid, signal.SIGTERM)
            self.assertEqual(wrapper.wait(timeout=6), 143)
            self.assertFalse(common.pid_alive(child_pid))

    @unittest.skipIf(sys.platform == "win32", "POSIX launch-window proof")
    def test_owned_wrapper_catches_term_during_launch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_path = Path(td) / "launch-window.pid"
            child = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            original = native._launch_owned

            def interrupt_after_launch(*args, **kwargs):
                process = original(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGTERM)
                return process

            with mock.patch.object(
                    native, "_launch_owned", side_effect=interrupt_after_launch):
                completed = native.run_owned_process(
                    [sys.executable, "-c", child], cwd=td, relay_signals=True)
            self.assertEqual(completed.returncode, 143)
            if pid_path.exists():
                self.assertFalse(common.pid_alive(
                    int(pid_path.read_text(encoding="ascii"))))

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_post_index_timeout_drains_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_path = Path(td) / "hook-grandchild.pid"
            grandchild = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            hook = (
                "import os,sys,time; "
                f"os.spawnv(os.P_NOWAIT,sys.executable,[sys.executable,'-c',{grandchild!r}]); "
                "time.sleep(60)")
            owner = indexd.indexer
            with mock.patch.object(owner.common, "data_dir_readonly", return_value=False), \
                    mock.patch.object(
                        owner.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(
                        owner, "configured_post_index_hooks",
                        return_value=[("test", [sys.executable, "-c", hook], 1)]), \
                    mock.patch.object(owner.common, "log"):
                owner.run_post_index_hooks()
            grandchild_pid = int(pid_path.read_text(encoding="ascii"))
            self.assertFalse(common.pid_alive(grandchild_pid))

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_post_index_owner_stop_drains_the_active_hook_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root_path = Path(td) / "hook-root.pid"
            grandchild_path = Path(td) / "hook-grandchild.pid"
            grandchild = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(grandchild_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            hook = (
                "import os,sys,time; from pathlib import Path; "
                f"Path({str(root_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                f"os.spawnv(os.P_NOWAIT,sys.executable,[sys.executable,'-c',{grandchild!r}]); "
                "time.sleep(60)")
            owner = self._test_indexer(mock.Mock())
            runner = threading.Thread(target=owner._run_post_index_hooks)
            stopper = threading.Thread(target=owner.stop)
            root_pid = None
            try:
                with mock.patch.object(
                        indexd.indexer.common, "data_dir_readonly",
                        return_value=False), mock.patch.object(
                        indexd.indexer.indexd_runtime,
                        "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), mock.patch.object(
                        indexd.indexer, "configured_post_index_hooks",
                        return_value=[(
                            "test", [sys.executable, "-c", hook], 60)]), \
                        mock.patch.object(indexd.indexer.common, "log"):
                    runner.start()
                    deadline = time.monotonic() + 5
                    while ((not root_path.exists()
                            or not grandchild_path.exists())
                           and time.monotonic() < deadline):
                        time.sleep(0.02)
                    self.assertTrue(root_path.exists())
                    self.assertTrue(grandchild_path.exists())
                    root_pid = self._read_published_pid(root_path)
                    grandchild_pid = self._read_published_pid(grandchild_path)
                    stopper.start()
                    stopper.join(timeout=6)
                    runner.join(timeout=6)
                self.assertFalse(stopper.is_alive())
                self.assertFalse(runner.is_alive())
                self.assertTrue(owner._post_index_idle.is_set())
                self.assertFalse(common.pid_alive(root_pid))
                self.assertFalse(common.pid_alive(grandchild_pid))
            finally:
                owner._stop_requested.set()
                if root_pid is not None and common.pid_alive(root_pid):
                    try:
                        os.killpg(root_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if stopper.ident is not None:
                    stopper.join(timeout=6)
                runner.join(timeout=6)

    def test_post_index_stop_closes_the_launch_window(self) -> None:
        owner = self._test_indexer(mock.Mock())
        launched = threading.Event()
        release_launch = threading.Event()
        completed = subprocess.CompletedProcess(["hook"], 0, "", "")

        def delayed_run(*_args, **kwargs):
            launched.set()
            self.assertTrue(release_launch.wait(5))
            self.assertFalse(kwargs["relay_signals"])
            kwargs["tick"](mock.Mock())
            return completed

        runner = threading.Thread(target=owner._run_post_index_hooks)
        with mock.patch.object(
                indexd.indexer.common, "data_dir_readonly",
                return_value=False), mock.patch.object(
                indexd.indexer.indexd_runtime, "derived_writer_mutation_info",
                return_value=mock.Mock(writable=True)), mock.patch.object(
                indexd.indexer, "configured_post_index_hooks",
                return_value=[("test", ["hook"], 60)]), mock.patch.object(
                native, "run_owned_process", side_effect=delayed_run):
            runner.start()
            self.assertTrue(launched.wait(5))
            stopper = threading.Thread(target=owner.stop)
            stopper.start()
            time.sleep(0.05)
            self.assertTrue(stopper.is_alive())
            release_launch.set()
            stopper.join(timeout=6)
            runner.join(timeout=6)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(runner.is_alive())
        self.assertTrue(owner._post_index_idle.is_set())

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group proof")
    def test_indexd_retirement_allows_the_hook_tree_to_drain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hook_path = root / "hook.pid"
            grandchild_path = root / "hook-grandchild.pid"
            grandchild = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(grandchild_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                "time.sleep(60)")
            hook = (
                "import os,sys,time; from pathlib import Path; "
                f"Path({str(hook_path)!r}).write_text(str(os.getpid()),encoding='ascii'); "
                f"os.spawnv(os.P_NOWAIT,sys.executable,[sys.executable,'-c',{grandchild!r}]); "
                "time.sleep(60)")
            driver = (
                "import os,sys,threading; "
                "import indexd; from hookless import native; "
                "shutdown=threading.Event(); caught=[None]; "
                "previous=indexd._install_shutdown_handlers(shutdown,caught); "
                "cancel=type('PostIndexCancel',(RuntimeError,),{}); "
                "tick=lambda _process: (_ for _ in ()).throw(cancel()) "
                "if shutdown.is_set() else None; "
                "\ntry:\n native.run_owned_process("
                f"[sys.executable,'-c',{hook!r}],cwd={td!r},tick=tick)"
                "\nexcept cancel:\n pass"
                "\nfinally:\n indexd._restore_signal_handlers(previous)\n")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            env["AGREP_DATA_DIR"] = td
            env.pop("AGREP_DATA_READONLY", None)
            daemon = subprocess.Popen(
                [sys.executable, "-c", driver], cwd=td, env=env,
                start_new_session=True)
            hook_pid = None
            try:
                deadline = time.monotonic() + 5
                while ((not hook_path.exists()
                        or not grandchild_path.exists())
                       and daemon.poll() is None
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
                self.assertIsNone(daemon.poll())
                self.assertTrue(hook_path.exists())
                self.assertTrue(grandchild_path.exists())
                hook_pid = self._read_published_pid(hook_path)
                grandchild_pid = self._read_published_pid(grandchild_path)
                birth = common.process_start_identity(daemon.pid)
                self.assertIsNotNone(birth)
                owner_path = root / ".indexd.v2.lock"
                raw = (
                    f"pid={daemon.pid} start={birth} "
                    f"protocol={indexd_runtime.INDEXD_PROTOCOL} "
                    f"package={common.package_version()} "
                    f"build={indexd_runtime.INDEXD_BUILD_ID} "
                    f"writer={indexd_runtime.derived_writer_build_id(common._resolved_ingest_bin())} "
                    f"group={daemon.pid} token={'a' * 32} time={time.time():.3f}\n"
                )
                owner_path.write_text(raw, encoding="ascii")
                with mock.patch.object(
                        indexd_runtime, "INDEXD_LOCK_PATH", owner_path), \
                        mock.patch.object(
                            indexd_runtime, "INDEXD_READY_PATH",
                            root / ".indexd.ready"), \
                        mock.patch.object(
                            indexd_runtime, "INDEXD_CHILD_PATH",
                            root / ".indexd.child"), \
                        mock.patch.object(
                            indexd_runtime, "LEGACY_INDEXD_LOCK_PATH",
                            root / ".indexd.lock"):
                    self.assertTrue(indexd_runtime.stop_indexd_owner(wait_s=5))
                self.assertEqual(daemon.wait(timeout=3), 0)
                self.assertFalse(common.pid_alive(hook_pid))
                self.assertFalse(common.pid_alive(grandchild_pid))
            finally:
                if daemon.poll() is None:
                    try:
                        os.killpg(daemon.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    daemon.wait(timeout=5)
                if hook_pid is not None and common.pid_alive(hook_pid):
                    try:
                        os.killpg(hook_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_post_index_failure_output_is_terminal_safe(self) -> None:
        owner = indexd.indexer
        result = subprocess.CompletedProcess(
            ["hook"], 9, "", "bad\n\x1b]52;payload\x07\u202e")
        with mock.patch.object(owner.common, "data_dir_readonly", return_value=False), \
                mock.patch.object(
                    owner.indexd_runtime, "derived_writer_mutation_info",
                    return_value=mock.Mock(writable=True)), \
                mock.patch.object(
                    owner, "configured_post_index_hooks",
                    return_value=[("test", ["hook"], 1)]), \
                mock.patch.object(
                    owner.native, "run_owned_process", return_value=result), \
                mock.patch.object(owner.common, "log") as log:
            owner.run_post_index_hooks()
        rendered = log.call_args.args[0]
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("\\u001b", rendered)

    def test_foreground_post_index_rethrows_the_relayed_signal(self) -> None:
        owner = indexd.indexer
        for signum, exception in (
                (signal.SIGINT, KeyboardInterrupt),
                (signal.SIGTERM, SystemExit)):
            with self.subTest(signum=signum):
                result = native.OwnedProcessResult(["hook"], 128 + signum, "", "")
                result.relayed_signal = signum
                with mock.patch.object(
                        owner.common, "data_dir_readonly", return_value=False), \
                        mock.patch.object(
                            owner.indexd_runtime, "derived_writer_mutation_info",
                            return_value=mock.Mock(writable=True)), \
                        mock.patch.object(
                            owner, "configured_post_index_hooks",
                            return_value=[("test", ["hook"], 1)]), \
                        mock.patch.object(
                            owner.native, "run_owned_process",
                            return_value=result):
                    with self.assertRaises(exception) as raised:
                        owner.run_post_index_hooks()
                if exception is SystemExit:
                    self.assertEqual(raised.exception.code, 128 + signum)

    def test_opencode_resolver_closes_database_after_query_error(self) -> None:
        class BrokenConnection:
            closed = False

            def execute(self, *_args):
                raise native.sqlite3.OperationalError("schema changed")

            def close(self):
                self.closed = True

        opened = []

        def connect(*_args, **_kwargs):
            item = BrokenConnection()
            opened.append(item)
            return item

        with (mock.patch.object(native.os.path, "exists", return_value=True),
              mock.patch.object(native.sqlite3, "connect", side_effect=connect)):
            self.assertEqual(native._opencode_cwd("ses_abcdef"), "")
        self.assertTrue(opened)
        self.assertTrue(all(item.closed for item in opened))

    def test_binary_fetch_fails_closed_without_sidecar(self) -> None:
        source = Path(_TMP.name) / "unsigned-agrep-rs"
        source.write_bytes(b"not executable")
        old = os.environ.pop("AGREP_ALLOW_UNVERIFIED_BINARY", None)
        try:
            dest = common.DATA_DIR / "bin" / "unsigned" / "agrep-rs"
            self.assertIsNone(common._download_binary(source.as_uri(), dest))
            self.assertFalse(dest.exists())
        finally:
            if old is not None:
                os.environ["AGREP_ALLOW_UNVERIFIED_BINARY"] = old

    def test_fetch_never_prints_mirror_credentials(self) -> None:
        # P6: an AGREP_BIN_URL with basic-auth userinfo printed unredacted to
        # stderr before the consent prompt (and into any CI log capturing it).
        import dist
        secret_url = (
            "https://s3cr3t_user:hunter2_TOKEN@mirror.internal.example/agrep/")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {
                    "AGREP_BIN_URL": secret_url, "AGREP_NO_FETCH": ""}), \
                mock.patch.object(dist.sys.stdin, "isatty",
                                  return_value=False), \
                contextlib.redirect_stderr(stderr):
            self.assertIsNone(dist.fetch_binary(assume_yes=False))
        output = stderr.getvalue()
        self.assertIn("***@mirror.internal.example", output)
        self.assertNotIn("hunter2_TOKEN", output)
        self.assertNotIn("s3cr3t_user", output)
        # failure paths scrub any echo of the credentialed URL from errors
        asset_url = secret_url + "agrep-rs-macos-aarch64"
        scrubbed = dist._scrub_credentials(
            f"<urlopen error {asset_url}.sha256 unreachable>", asset_url)
        self.assertNotIn("hunter2_TOKEN", scrubbed)
        self.assertIn("***@mirror.internal.example", scrubbed)

    def test_installed_cli_skips_cargo_before_verified_fetch(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agrep_cli_lifecycle_test", Path(__file__).resolve().parents[1] / "cli.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "agrep-rs"
            module.ROOT = root
            with (mock.patch.object(module.common, "ingest_bin", return_value=binary),
                  mock.patch("shutil.which", return_value="/usr/bin/cargo"),
                  mock.patch.object(module.subprocess, "run") as run,
                  mock.patch.object(module.common, "fetch_binary", return_value=None),
                  mock.patch.object(module.common, "cli_name", return_value="agrep"),
                  mock.patch("builtins.print")):
                self.assertFalse(module._ensure_binary())
            run.assert_not_called()

            (root / "Cargo.toml").touch()
            (root / "crates").mkdir()

            def build(*_args, **_kwargs):
                binary.touch()
                return mock.Mock(returncode=0)

            with (mock.patch.object(module.common, "ingest_bin", return_value=binary),
                  mock.patch("shutil.which", return_value="/usr/bin/cargo"),
                  mock.patch.object(module.subprocess, "run", side_effect=build) as run,
                  mock.patch.object(module.common, "fetch_binary") as fetch,
                  mock.patch("builtins.print")):
                self.assertTrue(module._ensure_binary())
            run.assert_called_once_with(["cargo", "build", "--release"], cwd=str(root))
            fetch.assert_not_called()

    def test_cli_boundary_reserves_exit_one_for_clean_misses(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agrep_cli_boundary_test", Path(__file__).resolve().parents[1] / "cli.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stdout, stderr = io.StringIO(), io.StringIO()
        failure = UnicodeEncodeError("utf-8", "\udcff", 0, 1, "fixture")
        with mock.patch.object(module, "_main", side_effect=failure), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = module.main()
        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().count("\n"), 1)
        # the reader gets the consequence and a command; the exception class
        # is debugging detail and waits behind AGREP_DEBUG
        self.assertNotIn("UnicodeEncodeError", stderr.getvalue())
        self.assertIn("`", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        with mock.patch.object(module.common, "DEBUG", True), \
                mock.patch.object(module, "_main", side_effect=failure), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr := io.StringIO()):
            module.main()
        self.assertIn("UnicodeEncodeError", stderr.getvalue())

    def test_cli_boundary_maps_the_exact_handle_and_regex_crashes_to_two(
            self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agrep_cli_exact_crash_test",
            Path(__file__).resolve().parents[1] / "cli.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import search

        fixtures = (
            (
                ["agrep", "around", "@abcdef01:" + "9" * 4301],
                None,
                "ValueError",
                "invalid result handle",
            ),
            (
                ["agrep", "-E", "(" * 1500 + "x" + ")" * 1500, "--no-auto"],
                True,
                "RecursionError",
                "`",
            ),
        )
        for argv, index_ready, error_name, public_marker in fixtures:
            stdout, stderr = io.StringIO(), io.StringIO()
            ready = (
                mock.patch.object(
                    search.indexd_runtime, "ensure_index",
                    return_value=index_ready)
                if index_ready is not None else contextlib.nullcontext()
            )
            with (
                self.subTest(error=error_name),
                mock.patch.object(module.sys, "argv", argv),
                mock.patch.object(module.common, "profile_report",
                                  return_value=None),
                ready,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = module.main()
            self.assertEqual(rc, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue().count("\n"),
                1,
                repr(stderr.getvalue()),
            )
            self.assertNotIn(error_name, stderr.getvalue())
            self.assertIn(public_marker, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_import_boundary_names_unwritable_data_dir_without_traceback(
            self) -> None:
        blocker = Path(_TMP.name) / "data-dir-parent-is-file"
        blocker.write_text("not a directory", encoding="utf-8")
        data_dir = blocker / "nested\\agrep"
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(data_dir),
            "AGREP_DATA_DIR_SOURCE": "env",
        }

        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "cli.py"),
                "doctor",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1)
        self.assertIn("AGREP_DATA_DIR", completed.stderr)
        self.assertIn(str(data_dir), completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_first_run_machine_surfaces_request_quiet_indexing(self) -> None:
        import recall
        import search

        missing = Path(_TMP.name) / "machine-first-run-missing"
        cases = (
            (search, ["needle", "--json"], True),
            (search, ["needle", "-c"], False),
            (search, ["needle", "--flat"], False),
            (recall, ["needle", "--json"], True),
        )
        for module, argv, json_output in cases:
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(module=module.__name__, argv=argv),
                mock.patch.object(common, "MESSAGES_PATH", missing),
                mock.patch.object(
                    common, "ingest_bin", return_value=missing),
                mock.patch.object(
                    module.indexd_runtime, "ensure_index",
                    return_value=False) as ensure,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = module.main(argv)
            self.assertEqual(rc, 2)
            ensure.assert_called_once_with(auto=True, quiet=True)
            if json_output:
                for line in stdout.getvalue().splitlines():
                    self.assertIsInstance(json.loads(line), dict)
                self.assertTrue(stdout.getvalue().strip())
            else:
                self.assertEqual(stdout.getvalue(), "")

    def test_machine_first_build_captures_ingest_output(self) -> None:
        missing = Path(_TMP.name) / "missing-messages.jsonl"
        binary = Path(__file__)
        with mock.patch.object(common, "MESSAGES_PATH", missing), \
                mock.patch.object(common, "ingest_bin", return_value=binary), \
                mock.patch.object(
                    indexd_runtime, "build_index", return_value=True) as build:
            self.assertTrue(indexd_runtime.ensure_index(quiet=True))
        build.assert_called_once_with(quiet=True, delegate_fts=True)

    def test_explicit_index_requires_the_search_index_refresh(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agrep_cli_index_test", Path(__file__).resolve().parents[1] / "cli.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(
                module.indexd_runtime, "build_index", return_value=False) as build, \
                mock.patch("builtins.print"):
            self.assertFalse(module._index())
        build.assert_called_once_with(require_search_index=True)

    @unittest.skipIf(sys.platform == "win32", "fixture uses a POSIX executable script")
    def test_binary_fetch_requires_matching_version(self) -> None:
        source = Path(_TMP.name) / "agrep-rs-fixture"
        source.write_text(
            f"#!/bin/sh\necho 'agrep-rs {common.package_version()}'\n", encoding="utf-8")
        source.chmod(0o755)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        Path(str(source) + ".sha256").write_text(digest + "\n", encoding="ascii")
        dest = common.DATA_DIR / "bin" / common.package_version() / "agrep-rs-test"
        self.assertEqual(common._download_binary(source.as_uri(), dest), dest)
        self.assertTrue(dest.exists())

    def test_wheel_manifest_and_packaging_enumerate_the_same_payload(self) -> None:
        """The build hook and validator share one closed runtime manifest."""
        try:
            import tomllib
        except ImportError:  # stdlib tomllib arrives in 3.11
            self.skipTest("tomllib unavailable")
        root = Path(__file__).resolve().parent.parent
        cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        force_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(force_include, {"_bin": "agrep/_bin"})
        sys.path.insert(0, str(root / "bench"))
        spec = importlib.util.spec_from_file_location(
            "validate_wheel", root / "bench" / "validate_wheel.py")
        wheelcheck = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(wheelcheck)
        finally:
            sys.path.pop(0)
        manifest = json.loads(
            (root / "py" / "runtime_manifest.json").read_text(encoding="utf-8"))
        expected = {
            item["member"]: root / item["source"] for item in manifest["files"]
        }
        self.assertEqual(expected, wheelcheck.RUNTIME_SOURCES)

    def test_wheel_manifest_carries_the_metal_embedding_import_chain(self) -> None:
        """embedder's optional MLX lane must remain importable after packaging."""
        root = Path(__file__).resolve().parent.parent
        manifest = json.loads(
            (root / "py" / "runtime_manifest.json").read_text(encoding="utf-8"))
        packaged = {
            item["source"]: item["member"] for item in manifest["files"]
        }
        required = {
            "py/embedder.py": "agrep/py/embedder.py",
            "py/mlx_embed.py": "agrep/py/mlx_embed.py",
            "py/mlx_modernbert.py": "agrep/py/mlx_modernbert.py",
        }
        self.assertEqual(
            {source: packaged.get(source) for source in required}, required)


if __name__ == "__main__":
    unittest.main()
