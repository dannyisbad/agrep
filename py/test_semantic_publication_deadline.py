from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import search


class SemanticPublicationDeadlineTests(unittest.TestCase):
    @staticmethod
    def _unstarted_thread():
        thread = mock.Mock()
        thread.is_alive.return_value = True
        return thread

    def test_inactive_publisher_keeps_the_exact_default_deadline(self) -> None:
        thread = self._unstarted_thread()
        started = 100.0
        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=False),
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(threading, "Thread", return_value=thread),
        ):
            pending = search._start_semantic_query("meaningful query", {})

        self.assertEqual(
            pending[2] - started, search._AUTO_SEMANTIC_TIMEOUT_S)
        thread.start.assert_called_once_with()

    def test_verified_publisher_gets_lexical_horizon_plus_headroom(self) -> None:
        thread = self._unstarted_thread()
        started = 200.0
        base = search._AUTO_SEMANTIC_TIMEOUT_S
        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=True),
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(threading, "Thread", return_value=thread),
        ):
            pending = search._start_semantic_query("meaningful query", {})

        self.assertEqual(
            pending[2] - started, search._QUERY_PUBLICATION_WAIT_S + base)

    def test_stuck_publisher_cannot_extend_past_the_shared_horizon(self) -> None:
        thread = self._unstarted_thread()
        started = 300.0
        base = 0.25
        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=True),
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(threading, "Thread", return_value=thread),
        ):
            pending = search._start_semantic_query(
                "meaningful query", {"semantic_timeout_s": base})
        deadline = pending[2]

        with (
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(search.common, "dbg"),
        ):
            self.assertIsNone(search._finish_semantic_query(pending))

        maximum = search._QUERY_PUBLICATION_WAIT_S + base
        self.assertEqual(deadline - started, maximum)
        thread.join.assert_called_once_with(timeout=maximum)

    def test_publication_extension_does_not_enable_the_opt_in_observer(self) -> None:
        captured = {}

        def query(_query: str, **kwargs):
            captured.update(kwargs)
            return {"hits": []}

        with (
            mock.patch.dict(
                os.environ, {search.sabel_observer.TRACE_ENV: ""}, clear=False),
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=True),
            mock.patch.object(search, "_QUERY_PUBLICATION_WAIT_S", 0.2),
            mock.patch.object(search, "run_query", side_effect=query),
            mock.patch.object(search.sabel_observer, "_atomic_write") as write,
        ):
            self.assertFalse(search.sabel_observer.active())
            pending = search._start_semantic_query(
                "meaningful query", {"semantic_timeout_s": 0.1})
            result = search._finish_semantic_query(pending)
            self.assertFalse(search.sabel_observer.active())

        self.assertEqual(result, {"hits": []})
        self.assertEqual(captured["mode"], "semantic")
        self.assertGreater(captured["semantic_timeout_s"], 0.0)
        self.assertLessEqual(captured["semantic_timeout_s"], 0.3)
        write.assert_not_called()

    def test_slow_observer_recording_cannot_consume_semantic_deadline(self) -> None:
        result = {
            "hits": [], "total": 0, "chats": 0,
            "engine": "semantic:fixture", "mode": "semantic",
        }
        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=False),
            mock.patch.object(
                search, "_semantic_candidates", return_value=object()),
            mock.patch.object(search, "_finalize_query", return_value=result),
            mock.patch.dict(
                os.environ, {search.sabel_observer.TRACE_ENV: ""}, clear=False),
        ):
            without_observer = search._finish_semantic_query(
                search._start_semantic_query(
                    "meaningful query", {"semantic_timeout_s": 0.03}))

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.dict(
                    os.environ,
                    {search.sabel_observer.TRACE_ENV: raw}, clear=False):
                scope = search.sabel_observer.safe_begin(
                    "recall", ["meaningful query"])
                self.assertIsNotNone(scope)
                original = search.sabel_observer.record_search_call
                recording_threads = []

                def delayed_record(*args, **kwargs):
                    recording_threads.append(threading.current_thread().name)
                    time.sleep(0.08)
                    return original(*args, **kwargs)

                try:
                    with (
                        mock.patch.object(
                            search, "_semantic_index_update_active",
                            return_value=False),
                        mock.patch.object(
                            search, "_semantic_candidates", return_value=object()),
                        mock.patch.object(
                            search, "_finalize_query", return_value=result),
                        mock.patch.object(
                            search.sabel_observer, "record_search_call",
                            side_effect=delayed_record),
                    ):
                        with_observer = search._finish_semantic_query(
                            search._start_semantic_query(
                                "meaningful query",
                                {"semantic_timeout_s": 0.03}))
                finally:
                    search.sabel_observer.safe_finish(scope, 0)

        self.assertIs(without_observer, result)
        self.assertIs(with_observer, result)
        self.assertEqual(recording_threads, [threading.main_thread().name])

    def test_timed_out_worker_is_recorded_unobserved_and_never_writes_late(self) -> None:
        release = threading.Event()
        result = {
            "hits": [], "total": 0, "chats": 0,
            "engine": "semantic:fixture", "mode": "semantic",
        }

        def blocked_candidates(_spec):
            release.wait(timeout=1.0)
            return object()

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.dict(
                    os.environ,
                    {search.sabel_observer.TRACE_ENV: raw}, clear=False):
                scope = search.sabel_observer.safe_begin(
                    "recall", ["meaningful query"])
                self.assertIsNotNone(scope)
                original = search.sabel_observer.record_search_call
                try:
                    with (
                        mock.patch.object(
                            search, "_semantic_index_update_active",
                            return_value=False),
                        mock.patch.object(
                            search, "_semantic_candidates",
                            side_effect=blocked_candidates),
                        mock.patch.object(
                            search, "_finalize_query", return_value=result),
                        mock.patch.object(
                            search.sabel_observer, "record_search_call",
                            wraps=original) as record,
                    ):
                        pending = search._start_semantic_query(
                            "meaningful query", {"semantic_timeout_s": 0.02})
                        self.assertIsNone(search._finish_semantic_query(pending))
                        search.sabel_observer.safe_finish(scope, 0)
                        scope = None
                        self.assertEqual(record.call_count, 1)
                        self.assertIsNone(record.call_args.args[3])
                        release.set()
                        pending[0].join(timeout=1.0)
                        self.assertFalse(pending[0].is_alive())
                        self.assertEqual(record.call_count, 1)
                finally:
                    release.set()
                    search.sabel_observer.safe_finish(scope, 0)


if __name__ == "__main__":
    unittest.main()
