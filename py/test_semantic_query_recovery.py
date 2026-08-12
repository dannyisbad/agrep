"""Regression tests for bounded automatic semantic recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import search  # noqa: E402
import semantic  # noqa: E402
import surface_policy as surface  # noqa: E402


def _coherence(state: str, *, searchable: bool = False) -> dict:
    return {
        "state": state,
        "coherent": searchable,
        "searchable": searchable,
        "coverage": ({
            "indexed": 2, "total": 2, "pending": 0, "complete": True,
        } if searchable else None),
    }


def _unavailable(reason: str) -> dict:
    return {
        "hits": [],
        "fallback_recommended": True,
        "semantic_status": {
            "state": "unavailable", "reason": reason,
            "retryable": True, "complete": False,
            "fallback_recommended": True,
        },
    }


def _ready() -> dict:
    return {
        "hits": [{"session": "history", "turn": 4}],
        "fallback_recommended": False,
        "semantic_status": {
            "state": "ready", "complete": True,
            "fallback_recommended": False,
        },
    }


class SemanticGenerationRecoveryTests(unittest.TestCase):
    def test_recovery_environment_values_are_finite_and_capped_at_import(
            self) -> None:
        cases = (
            ("inf", "inf", 12.0, 0.025),
            ("nan", "nan", 12.0, 0.025),
            ("1e100", "1e100", 12.0, 0.25),
            ("-5", "-5", 0.0, 0.001),
            ("bogus", "bogus", 12.0, 0.025),
            ("0.05", "0.01", 0.05, 0.01),
        )
        code = (
            "import json, semantic; "
            "print(json.dumps([semantic.SEMANTIC_QUERY_RECOVERY_WAIT_S, "
            "semantic.SEMANTIC_QUERY_RECOVERY_POLL_S]))"
        )
        for wait_raw, poll_raw, expected_wait, expected_poll in cases:
            with self.subTest(wait=wait_raw, poll=poll_raw):
                env = dict(os.environ)
                env["AGREP_SEM_QUERY_RECOVERY_WAIT_S"] = wait_raw
                env["AGREP_SEM_QUERY_RECOVERY_POLL_S"] = poll_raw
                run = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=Path(__file__).resolve().parent,
                    env=env, capture_output=True, text=True,
                    timeout=10.0, check=False)
                self.assertEqual(run.returncode, 0, run.stderr)
                wait_s, poll_s = json.loads(run.stdout)
                self.assertEqual(wait_s, expected_wait)
                self.assertEqual(poll_s, expected_poll)

    def test_current_searchable_generation_is_ready_without_waiting(self) -> None:
        sleep = mock.Mock()
        with mock.patch.object(
                semantic, "embedding_coherence",
                return_value=_coherence("current", searchable=True)), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    return_value=False) as active, \
                mock.patch.object(semantic, "embed_running") as embed, \
                mock.patch.object(semantic.time, "sleep", sleep):
            result = semantic.wait_for_query_recovery(timeout_s=1.0)
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["waited"])
        active.assert_called_once_with()
        embed.assert_not_called()
        sleep.assert_not_called()

    def test_partial_searchable_generation_waits_for_active_publisher(
            self) -> None:
        partial = {
            "state": "partial", "coherent": False, "searchable": True,
            "coverage": {
                "indexed": 1, "total": 2, "pending": 1,
                "complete": False,
            },
        }
        clock = [0.0]

        def sleep(delay: float) -> None:
            clock[0] += delay

        with mock.patch.object(
                semantic, "embedding_coherence", return_value=partial), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    return_value=True), \
                mock.patch.object(
                    semantic, "embed_running", return_value=False), \
                mock.patch.object(
                    semantic.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(semantic.time, "sleep", side_effect=sleep):
            result = semantic.wait_for_query_recovery(timeout_s=0.05)
        self.assertEqual(result["state"], "timeout")
        self.assertTrue(result["waited"])
        self.assertLessEqual(clock[0], 0.05)

    def test_unexplained_stale_generation_does_not_sleep_or_fake_ready(
            self) -> None:
        sleep = mock.Mock()
        with mock.patch.object(
                semantic, "embedding_coherence",
                return_value=_coherence("stale")), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    return_value=False), \
                mock.patch.object(
                    semantic, "embed_running", return_value=False), \
                mock.patch.object(semantic.time, "sleep", sleep):
            result = semantic.wait_for_query_recovery(timeout_s=8.0)
        self.assertEqual(result["state"], "not-converging")
        self.assertFalse(result["waited"])
        sleep.assert_not_called()

    def test_live_publication_can_converge_to_ready(self) -> None:
        stale = _coherence("stale")
        ready = _coherence("current", searchable=True)
        with mock.patch.object(
                semantic, "embedding_coherence", side_effect=[stale, ready]), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    side_effect=[True, False]), \
                mock.patch.object(
                    semantic, "embed_running", return_value=False), \
                mock.patch.object(
                    semantic, "SEMANTIC_QUERY_RECOVERY_POLL_S", 0.001):
            result = semantic.wait_for_query_recovery(timeout_s=0.1)
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["waited"])

    def test_live_recovery_deadline_is_strictly_bounded(self) -> None:
        stale = _coherence("stale")
        clock = [0.0]

        def sleep(delay: float) -> None:
            clock[0] += delay

        with mock.patch.object(
                semantic, "embedding_coherence", return_value=stale), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    return_value=True), \
                mock.patch.object(
                    semantic, "embed_running", return_value=False), \
                mock.patch.object(
                    semantic.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(semantic.time, "sleep", side_effect=sleep):
            result = semantic.wait_for_query_recovery(timeout_s=0.05)
        self.assertEqual(result["state"], "timeout")
        self.assertLessEqual(clock[0], 0.05)


class AutomaticSemanticRetryTests(unittest.TestCase):
    @staticmethod
    def _completed_pending(result: dict):
        class DoneThread:
            @staticmethod
            def join(*, timeout: float) -> None:
                del timeout

            @staticmethod
            def is_alive() -> bool:
                return False

        return (
            DoneThread(),
            {"query": "staged checklists",
             "kwargs": {"limit": 2, "semantic_timeout_s": 0.2},
             "allow_recovery": True, "result": result},
            search.time.monotonic(),
        )

    def test_runtime_recovery_override_is_rebounded_and_never_crashes(
            self) -> None:
        first = _unavailable(search._SEMANTIC_WORKER_START_MISS)
        for raw, expected in (
                (float("inf"), 12.0), (float("nan"), 12.0),
                (1e100, 12.0), (-1.0, 0.0)):
            with self.subTest(raw=raw):
                with mock.patch.object(
                        semantic, "SEMANTIC_QUERY_RECOVERY_WAIT_S", raw), \
                        mock.patch.object(
                            semantic, "wait_for_query_recovery",
                            return_value={"state": "not-converging"}) as recover:
                    result = search._safe_finish_semantic_query(
                        self._completed_pending(first))
                self.assertIs(result, first)
                if expected == 0.0:
                    recover.assert_not_called()
                else:
                    recover.assert_called_once()
                    timeout_s = recover.call_args.kwargs["timeout_s"]
                    self.assertGreater(timeout_s, 0.0)
                    self.assertLessEqual(timeout_s, expected)

    def test_thread_start_resource_failure_is_contained(self) -> None:
        import threading
        with mock.patch.object(
                threading.Thread, "start",
                side_effect=RuntimeError("thread resources exhausted")):
            pending = search._start_semantic_query(
                "staged checklists", {"semantic_timeout_s": 0.2})
        self.assertIsNone(pending)

    def test_worker_start_budget_is_short_only_for_the_initial_lane(self) -> None:
        import semworker
        self.assertEqual(search._semantic_worker_start_timeout(0.20), 0.20)
        self.assertEqual(
            search._semantic_worker_start_timeout(
                search._AUTO_SEMANTIC_TIMEOUT_S),
            search._AUTO_SEMANTIC_START_S,
        )
        self.assertEqual(
            search._semantic_worker_start_timeout(12.0),
            semworker.START_TIMEOUT_S,
        )

    def test_recovery_sized_attempt_waits_for_the_starting_worker(self) -> None:
        import semworker
        data = {
            "results": [], "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {
                "indexed": 2, "total": 2, "pending": 0, "complete": True},
            "partial": False,
        }
        with mock.patch.object(
                semworker, "resident_status", return_value={"running": False}), \
                mock.patch.object(
                    semworker, "search_worker", return_value=data) as worker:
            result = search._semantic_local(
                "staged checklists", 2,
                timeout_s=semantic.SEMANTIC_QUERY_RECOVERY_WAIT_S)
        self.assertFalse(result["fallback_recommended"])
        start_timeout = worker.call_args.kwargs["start_timeout_s"]
        self.assertGreater(start_timeout, search._AUTO_SEMANTIC_START_S)
        self.assertLessEqual(start_timeout, semworker.START_TIMEOUT_S)

    def test_retry_preserves_filters_and_uses_separate_budget(self) -> None:
        calls: list[dict] = []

        def run_query(_query: str, *, mode: str, **kwargs) -> dict:
            self.assertEqual(mode, "semantic")
            calls.append(kwargs)
            return (_unavailable(search._SEMANTIC_WORKER_START_MISS)
                    if len(calls) == 1 else _ready())

        options = {
            "limit": 4,
            "exclude_session": "caller",
            "exclude_session_from_turn": 76,
            "semantic_timeout_s": 0.2,
        }
        with mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(
                    semantic, "wait_for_query_recovery",
                    return_value={"state": "ready", "waited": False}) as recover:
            result = search._finish_semantic_query(
                search._start_semantic_query("staged checklists", options))
        self.assertEqual(result, _ready())
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["exclude_session"], "caller")
            self.assertEqual(call["exclude_session_from_turn"], 76)
            self.assertFalse(call["allow_model_download"])
        self.assertLessEqual(calls[0]["semantic_timeout_s"], 0.2)
        self.assertGreater(calls[1]["semantic_timeout_s"], 0.2)
        recover.assert_called_once()
        recovery_wait = recover.call_args.kwargs["timeout_s"]
        self.assertGreater(recovery_wait, 0.0)
        self.assertLessEqual(
            recovery_wait, semantic.SEMANTIC_QUERY_RECOVERY_WAIT_S)

    def test_worker_start_miss_retries_while_current_corpus_is_publishing(
            self) -> None:
        first = _unavailable(search._SEMANTIC_WORKER_START_MISS)
        current = _coherence("current", searchable=True)
        with mock.patch.object(
                semantic, "embedding_coherence", return_value=current), \
                mock.patch.object(
                    semantic, "_query_corpus_update_active",
                    return_value=True) as active, \
                mock.patch.object(semantic, "embed_running") as embed, \
                mock.patch.object(
                    search, "run_query", return_value=_ready()) as query:
            result = search._safe_finish_semantic_query(
                self._completed_pending(first))
        self.assertEqual(result, _ready())
        query.assert_called_once()
        active.assert_called_once_with()
        embed.assert_not_called()

    def test_convergence_and_retry_share_one_absolute_deadline(self) -> None:
        clock = [100.0]
        joins: list[float] = []
        first = _unavailable(search._SEMANTIC_WORKER_START_MISS)

        class DoneThread:
            def join(self, *, timeout: float) -> None:
                self.timeout = timeout

            @staticmethod
            def is_alive() -> bool:
                return False

        class DeadlineThread:
            def join(self, *, timeout: float) -> None:
                joins.append(timeout)
                clock[0] += timeout

            @staticmethod
            def is_alive() -> bool:
                return True

        initial = (
            DoneThread(),
            {
                "query": "staged checklists",
                "kwargs": {
                    "limit": 4,
                    "exclude_session": "caller",
                    "semantic_timeout_s": 0.2,
                },
                "allow_recovery": True,
                "result": first,
            },
            clock[0],
        )

        def wait_for_recovery(*, timeout_s: float) -> dict:
            self.assertAlmostEqual(timeout_s, 0.05)
            clock[0] += 0.04
            return {"state": "ready", "waited": True}

        def start_retry(query: str, kwargs: dict, *,
                        _allow_recovery: bool,
                        _absolute_deadline: float):
            self.assertEqual(query, "staged checklists")
            self.assertFalse(_allow_recovery)
            self.assertAlmostEqual(_absolute_deadline, 100.05)
            self.assertAlmostEqual(kwargs["semantic_timeout_s"], 0.01)
            self.assertEqual(kwargs["exclude_session"], "caller")
            return (
                DeadlineThread(),
                {"query": query, "kwargs": dict(kwargs),
                 "allow_recovery": False},
                _absolute_deadline,
            )

        with mock.patch.object(
                search.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(
                    semantic, "SEMANTIC_QUERY_RECOVERY_WAIT_S", 0.05), \
                mock.patch.object(
                    semantic, "wait_for_query_recovery",
                    side_effect=wait_for_recovery), \
                mock.patch.object(
                    search, "_start_semantic_query",
                    side_effect=start_retry):
            result = search._finish_semantic_query(initial)

        self.assertIs(result, first)
        self.assertEqual(len(joins), 1)
        self.assertAlmostEqual(joins[0], 0.01)
        self.assertAlmostEqual(clock[0], 100.05)

    def test_hot_preflight_miss_is_typed_and_retryable(self) -> None:
        import semworker
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "resident_status",
                    return_value={"running": True, "descriptor_state": "ready"}), \
                mock.patch.object(
                    semworker, "search_worker",
                    side_effect=semworker.ResidentSemanticLoopbackUnavailable(
                        "loopback denied")), \
                mock.patch.object(search, "_guarded_semantic_local_fallback") \
                as guarded:
            result = search._semantic_local(
                "staged checklists", 2, timeout_s=0.2)
        self.assertEqual(
            result["semantic_status"]["reason"],
            search._SEMANTIC_WORKER_START_MISS)
        self.assertTrue(result["semantic_status"]["retryable"])
        guarded.assert_not_called()

    def test_known_transient_preflight_preserves_exact_reason(self) -> None:
        import semworker
        reason = "semantic worker ownership is still settling"
        with mock.patch.dict(
                os.environ,
                {"AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                mock.patch.object(
                    semworker, "resident_status", return_value={"running": True}), \
                mock.patch.object(
                    semworker, "search_worker",
                    side_effect=semworker.ResidentSemanticPreflightUnavailable(
                        reason)):
            result = search._semantic_local(
                "staged checklists", 2, timeout_s=0.2)
        self.assertEqual(result["semantic_status"]["reason"], reason)
        self.assertTrue(result["semantic_status"]["retryable"])

    def test_permanent_preflight_reason_is_preserved_and_not_retried(
            self) -> None:
        import semworker
        reasons = (
            "semantic request coordination cannot prepare a path",
            "semantic worker startup coordination cannot create its claim",
            "agrep removal is active",
        )
        for reason in reasons:
            with self.subTest(reason=reason), \
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_NO_DAEMON": "", "AGREP_NO_SEM_WORKER": ""}), \
                    mock.patch.object(
                        semworker, "resident_status",
                        return_value={"running": True}), \
                    mock.patch.object(
                        semworker, "search_worker",
                        side_effect=(
                            semworker.ResidentSemanticPreflightUnavailable(
                                reason))), \
                    mock.patch.object(
                        search, "_guarded_semantic_local_fallback") as guarded:
                unavailable = search._semantic_local(
                    "staged checklists", 2, timeout_s=0.2)
            status = unavailable["semantic_status"]
            self.assertEqual(status["reason"], reason)
            self.assertNotIn("retryable", status)
            guarded.assert_not_called()
            with mock.patch.object(
                    search, "run_query", return_value=unavailable), \
                    mock.patch.object(
                        semantic, "wait_for_query_recovery") as recover:
                finished = search._safe_finish_semantic_query(
                    search._safe_start_semantic_query(
                        "staged checklists", {"semantic_timeout_s": 0.2}))
            self.assertEqual(finished, unavailable)
            recover.assert_not_called()

    def test_upper_retry_uses_guarded_read_only_fallback(self) -> None:
        import semworker
        data = {
            "results": [], "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {
                "indexed": 2, "total": 2, "pending": 0, "complete": True},
            "partial": False,
        }
        with mock.patch.object(
                semworker, "resident_status", return_value={"running": False}), \
                mock.patch.object(
                    semworker, "search_worker",
                    side_effect=semworker.ResidentSemanticLoopbackUnavailable(
                        "loopback denied")), \
                mock.patch.object(
                    search, "_guarded_semantic_local_fallback",
                    return_value={"ok": True, "data": data}) as guarded:
            result = search._semantic_local(
                "staged checklists", 2,
                timeout_s=semantic.SEMANTIC_QUERY_RECOVERY_WAIT_S)
        self.assertFalse(result["fallback_recommended"])
        self.assertGreater(guarded.call_args.kwargs["timeout_s"], 1.0)
        self.assertEqual(
            guarded.call_args.kwargs["filters"]["_family_diverse"], True)

    def test_recovery_start_failure_preserves_first_typed_status(self) -> None:
        first = _unavailable(search._SEMANTIC_WORKER_START_MISS)
        with mock.patch.object(
                semantic, "wait_for_query_recovery",
                return_value={"state": "ready"}), \
                mock.patch.object(
                    search, "_start_semantic_query",
                    side_effect=RuntimeError("thread exhausted")):
            result = search._safe_finish_semantic_query(
                self._completed_pending(first))
        self.assertIs(result, first)

    def test_recovery_finish_failure_preserves_first_typed_status(self) -> None:
        first = _unavailable(search._SEMANTIC_WORKER_START_MISS)

        class BrokenThread:
            @staticmethod
            def join(*, timeout: float) -> None:
                del timeout
                raise RuntimeError("join failed")

        broken = (
            BrokenThread(),
            {"query": "staged checklists", "kwargs": {},
             "allow_recovery": False},
            search.time.monotonic() + 1.0,
        )
        with mock.patch.object(
                semantic, "wait_for_query_recovery",
                return_value={"state": "ready"}), \
                mock.patch.object(
                    search, "_start_semantic_query", return_value=broken):
            result = search._safe_finish_semantic_query(
                self._completed_pending(first))
        self.assertIs(result, first)

    def test_post_acceptance_timeout_is_never_retried(self) -> None:
        unavailable = {
            **_unavailable("resident semantic query timed out"),
        }
        unavailable["semantic_status"].pop("retryable")
        with mock.patch.object(
                search, "run_query", return_value=unavailable), \
                mock.patch.object(
                    semantic, "wait_for_query_recovery") as recover:
            result = search._finish_semantic_query(
                search._start_semantic_query(
                    "staged checklists", {"semantic_timeout_s": 0.2}))
        self.assertEqual(result, unavailable)
        recover.assert_not_called()

    def test_only_exact_worker_transients_are_retryable(self) -> None:
        for reason in (
                "semantic worker ownership is still settling",
                "resident semantic worker is busy or unreachable",
                "semantic request expired while queued",
                search._SEMANTIC_WORKER_START_MISS):
            with self.subTest(reason=reason):
                self.assertTrue(surface.semantic_status_retryable({
                    "state": "unavailable", "reason": reason}))
        for reason in (
                "semantic worker cannot bind loopback 127.0.0.1:0",
                "resident semantic query timed out",
                "semantic model is not cached",
                "embedding profile mismatch",
                "semantic request coordination cannot prepare a path"):
            with self.subTest(reason=reason):
                self.assertFalse(surface.semantic_status_retryable({
                    "state": "unavailable", "reason": reason}))


if __name__ == "__main__":
    unittest.main()
