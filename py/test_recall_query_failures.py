from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import common  # noqa: E402
import corpusdb  # noqa: E402
import indexd_runtime  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402


class RecallQueryFailureTests(unittest.TestCase):
    @staticmethod
    def _run(
            exc_type, *, message: str = "snapshot proof failed",
            machine: bool = False):
        stdout, stderr = io.StringIO(), io.StringIO()
        freshness = {
            "state": "current", "failing": False,
            "checked": True, "may_be_stale": False,
        }
        argv = ["needle", "--lexical", "--self"]
        if machine:
            argv.append("--json")
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(
                indexd_runtime, "machine_freshness", return_value=freshness),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(search, "_semantic_runtime_installed", return_value=False),
            mock.patch.object(
                search, "run_query",
                side_effect=exc_type(message)),
            mock.patch.object(
                corpusdb, "machine_freshness_fields",
                side_effect=lambda value, **_kwargs: {
                    "freshness": value, "corpus_age_s": 0.0}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = recall.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_classic_snapshot_failures_are_terse_rc2(self) -> None:
        cases = (
            (
                search.DirectSnapshotQueryError,
                "the published transcript snapshot could not be verified\n",
            ),
            (
                search.NativeEventScanError,
                "tool-event search could not verify a complete snapshot\n",
            ),
        )
        for exc_type, expected in cases:
            with self.subTest(error=exc_type.__name__):
                rc, stdout, stderr = self._run(exc_type)
                self.assertEqual((rc, stdout, stderr), (2, "", expected))

    def test_json_snapshot_failures_keep_structured_stdout(self) -> None:
        cases = (
            (search.DirectSnapshotQueryError, "direct-snapshot-unverified"),
            (search.NativeEventScanError, "event-scan-failed"),
        )
        for exc_type, code in cases:
            with self.subTest(error=exc_type.__name__):
                rc, stdout, stderr = self._run(exc_type, machine=True)
                payload = json.loads(stdout)
                self.assertEqual(rc, 2)
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(payload["hits"], [])
                self.assertEqual(stderr, "")

    def test_publication_timeout_is_actionable_and_machine_clean(self) -> None:
        message = search._QUERY_PUBLICATION_TIMEOUT
        rc, stdout, stderr = self._run(
            search.SnapshotPublicationTimeout, message=message)
        self.assertEqual((rc, stdout, stderr), (2, "", message + "\n"))

        rc, stdout, stderr = self._run(
            search.SnapshotPublicationTimeout, message=message, machine=True)
        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"], {
            "code": "snapshot-publication-timeout", "reason": message,
        })
        self.assertEqual(payload["hits"], [])
        self.assertEqual(stderr, "")

    def test_classic_database_failures_are_terse_rc2(self) -> None:
        cases = (
            (
                search.QueryDatabaseBusyError,
                "search index is busy updating; retry",
            ),
            (
                search.QueryDatabaseUnavailableError,
                "search index is temporarily unavailable; retry",
            ),
        )
        for exc_type, message in cases:
            with self.subTest(error=exc_type.__name__):
                rc, stdout, stderr = self._run(
                    exc_type, message=message)
                self.assertEqual(
                    (rc, stdout, stderr), (2, "", message + "\n"))

    def test_json_database_failures_keep_structured_stdout(self) -> None:
        cases = (
            (
                search.QueryDatabaseBusyError,
                "search index is busy updating; retry",
                "search-index-busy", "corpusdb:busy",
            ),
            (
                search.QueryDatabaseUnavailableError,
                "search index is temporarily unavailable; retry",
                "search-index-unavailable", "corpusdb:unavailable",
            ),
        )
        for exc_type, message, code, engine in cases:
            with self.subTest(error=exc_type.__name__):
                rc, stdout, stderr = self._run(
                    exc_type, message=message, machine=True)
                payload = json.loads(stdout)
                self.assertEqual(rc, 2)
                self.assertEqual(payload["error"], {
                    "code": code, "reason": message,
                })
                self.assertEqual(payload["engine"], engine)
                self.assertEqual(payload["hits"], [])
                self.assertEqual(stderr, message + "\n")
                self.assertNotIn("Traceback", stderr)

    def test_query_failure_joins_an_active_meaning_lane(self) -> None:
        pending = object()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(search, "_semantic_runtime_installed", return_value=True),
            mock.patch.object(recall, "_auto_semantic_query", return_value=True),
            mock.patch.object(
                search, "_start_semantic_query", return_value=pending),
            mock.patch.object(
                search, "run_query",
                side_effect=search.DirectSnapshotQueryError("proof failed")),
            mock.patch.object(
                search, "_finish_semantic_query", return_value=None) as finish,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = recall.main(["natural language query", "--self"])

        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "the published transcript snapshot could not be verified\n")
        finish.assert_called_once_with(pending)

    def test_database_failure_joins_an_active_meaning_lane(self) -> None:
        cases = (
            (
                search.QueryDatabaseBusyError,
                "search index is busy updating; retry",
            ),
            (
                search.QueryDatabaseUnavailableError,
                "search index is temporarily unavailable; retry",
            ),
        )
        for exc_type, message in cases:
            with self.subTest(error=exc_type.__name__):
                pending = object()
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(
                        indexd_runtime, "ensure_index", return_value=True),
                    mock.patch.object(
                        common, "in_agent_context", return_value=False),
                    mock.patch.object(
                        search, "_semantic_runtime_installed",
                        return_value=True),
                    mock.patch.object(
                        recall, "_auto_semantic_query", return_value=True),
                    mock.patch.object(
                        search, "_start_semantic_query",
                        return_value=pending),
                    mock.patch.object(
                        search, "run_query", side_effect=exc_type(message)),
                    mock.patch.object(
                        search, "_finish_semantic_query",
                        return_value=None) as finish,
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    rc = recall.main(["natural language query", "--self"])

                self.assertEqual((rc, stdout.getvalue()), (2, ""))
                self.assertEqual(stderr.getvalue(), message + "\n")
                finish.assert_called_once_with(pending)

    def test_optional_thread_start_failure_keeps_recall_keyword_output(
            self) -> None:
        result = {
            "hits": [{
                "session": "history", "turn": 4, "ts": 1,
                "who": "user", "agent": "codex", "project": "agrep",
                "score": 1.0, "matched": "phrase",
                "snippet": "deployment kept retrying",
            }],
            "total": 1, "chats": 1, "phrase_chats": 1,
            "tool_hits": 0, "engine": "corpusdb", "mode": "keyword",
            "totals_exact": True, "truncated": False,
        }
        windows = [{
            "session": "history", "center": 4,
            "first_turn": 4, "last_turn": 4,
            "agent": "codex", "project": "agrep",
            "turns": [{"turn": 4, "ts": 1, "who": "user",
                       "text": "deployment kept retrying", "reply": ""}],
            "events": [],
        }]
        start_calls = []
        query_calls = []

        def start(query, kwargs):
            start_calls.append((query, kwargs))
            raise RuntimeError("thread resources exhausted")

        def run_query(_query, *, mode="keyword", **_kwargs):
            query_calls.append(mode)
            if mode == "semantic":
                raise RuntimeError("synchronous semantic fallback escaped")
            return result

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(search, "_semantic_runtime_installed", return_value=True),
            mock.patch.object(recall, "_auto_semantic_query", return_value=True),
            mock.patch.object(
                search, "_start_semantic_query",
                side_effect=start),
            mock.patch.object(search, "run_query", side_effect=run_query),
            mock.patch.object(recall.explore, "get_windows", return_value=windows),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = recall.main([
                "natural language query", "--self", "--json",
                "--hits", "2", "--budget", "4000"])

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["hits"][0]["session"], "history")
        self.assertEqual(len(start_calls), 1)
        self.assertNotIn("semantic", query_calls)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_ranked_hit_without_a_replayable_window_exits_2(self) -> None:
        result = {
            "hits": [{
                "session": "missing-window", "turn": 4, "ts": 1,
                "who": "user", "agent": "codex", "project": "agrep",
                "score": 1.0, "matched": "phrase", "snippet": "needle",
            }],
            "total": 1, "chats": 1, "phrase_chats": 1,
            "tool_hits": 0, "engine": "corpusdb", "mode": "keyword",
            "totals_exact": True, "truncated": False,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "freshness_story",
                    return_value=recall.surface.FreshnessStory("current")), \
                mock.patch.object(
                    indexd_runtime, "agent_freshness_notice", return_value=""), \
                mock.patch.object(common, "in_agent_context", return_value=False), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "get_windows", return_value=[]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--lexical", "--self"])
        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("no hits", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
