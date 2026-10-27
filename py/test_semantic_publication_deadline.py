from __future__ import annotations

import threading
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

    def test_stuck_first_attempt_is_abandoned_at_the_cold_start_cap(self) -> None:
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
        # A still-running first attempt is usually a cold worker start: it is
        # awaited up to the cold-start cap, never beyond it.
        self.assertEqual(thread.join.call_args_list, [
            mock.call(timeout=maximum),
            mock.call(timeout=search._semantic_cold_start_cap_s()),
        ])

    def test_recovery_retry_is_never_extended_past_its_deadline(self) -> None:
        thread = self._unstarted_thread()
        started = 400.0
        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=False),
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(threading, "Thread", return_value=thread),
        ):
            pending = search._start_semantic_query(
                "meaningful query", {"semantic_timeout_s": 2.0},
                _allow_recovery=False, _absolute_deadline=started + 1.5)
        with (
            mock.patch.object(search.time, "monotonic", return_value=started),
            mock.patch.object(search.common, "dbg"),
        ):
            self.assertIsNone(search._finish_semantic_query(pending))
        thread.join.assert_called_once_with(timeout=1.5)

    def test_publication_extension_preserves_the_semantic_call(self) -> None:
        captured = {}

        def query(_query: str, **kwargs):
            captured.update(kwargs)
            return {"hits": []}

        with (
            mock.patch.object(
                search, "_semantic_index_update_active", return_value=True),
            mock.patch.object(search, "_QUERY_PUBLICATION_WAIT_S", 0.2),
            mock.patch.object(search, "run_query", side_effect=query),
        ):
            pending = search._start_semantic_query(
                "meaningful query", {"semantic_timeout_s": 0.1})
            result = search._finish_semantic_query(pending)

        self.assertEqual(result, {"hits": []})
        self.assertEqual(captured["mode"], "semantic")
        self.assertGreater(captured["semantic_timeout_s"], 0.0)
        self.assertLessEqual(captured["semantic_timeout_s"], 0.3)



if __name__ == "__main__":
    unittest.main()
