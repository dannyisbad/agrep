from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import search


def _spec(**overrides) -> search.QuerySpec:
    values = {
        "q": "needle",
        "mode": "keyword",
        "limit": 40,
        "sort": "score",
        "agent": None,
        "project": None,
        "who": None,
        "model": None,
        "model_soft": False,
        "chat": None,
        "since_ms": None,
        "until_ms": None,
        "exhaustive": False,
        "session_limit": None,
        "include_tools": True,
        "exclude_session": None,
        "exclude_session_from_turn": None,
        "allow_fallback": True,
        "exact_totals": True,
        "family_diverse": False,
        "semantic_timeout_s": None,
    }
    values.update(overrides)
    return search.QuerySpec(**values)


class QueryContractTests(unittest.TestCase):
    def test_query_spec_is_frozen_and_slotted(self):
        spec = _spec()
        self.assertFalse(hasattr(spec, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.limit = 1

    def test_lane_metadata_defaults_are_not_shared(self):
        first = search.LaneResult([], "one")
        second = search.LaneResult([], "two")
        self.assertFalse(hasattr(first, "__dict__"))
        first.semantic_meta["partial"] = True
        self.assertEqual(second.semantic_meta, {})

    def test_keyword_finalizer_preserves_key_omissions(self):
        hit = {"session": "s", "who": "user"}
        result = search._finalize_query(
            _spec(limit=1),
            search.LaneResult([hit], "fixture", pre_ranked=True))
        self.assertEqual(result, {
            "hits": [hit],
            "total": 1,
            "chats": 1,
            "engine": "fixture",
            "mode": "keyword",
            "tool_hits": 0,
            "returned_chats": 1,
            "phrase_chats": 1,
        })

    def test_bounded_finalizer_reports_only_observed_total_state(self):
        hit = {"session": "s", "who": "tool"}
        bounded = {
            "hits": [hit],
            "total": 8,
            "chats": 3,
            "tool_hits": 2,
            "totals_exact": False,
        }
        result = search._finalize_query(
            _spec(limit=1, exact_totals=False),
            search.LaneResult(
                [hit], "corpusdb", pre_ranked=True, bounded_rows=bounded))
        self.assertEqual(result, {
            "hits": [hit],
            "total": 8,
            "chats": 3,
            "engine": "corpusdb",
            "mode": "keyword",
            "tool_hits": 2,
            "returned_chats": 1,
            "totals_exact": False,
        })

    def test_semantic_finalizer_keeps_status_and_truncation_contract(self):
        hit = {"session": "s", "who": "user"}
        meta = {
            "score_kind": "cosine",
            "semantic_coverage": 0.5,
            "semantic_accelerator_coverage": 0.25,
            "partial": True,
            "semantic_status": {"complete": True},
            "fallback_recommended": False,
        }
        result = search._finalize_query(
            _spec(mode="semantic", family_diverse=True),
            search.LaneResult(
                [hit], "semantic:hybrid", pre_ranked=True,
                semantic_meta=meta, semantic_truncated=True))
        self.assertEqual(result["hits"], [{**hit, "session_hits": 1}])
        self.assertFalse(result["totals_exact"])
        self.assertTrue(result["truncated"])
        self.assertNotIn("phrase_chats", result)
        for key, value in meta.items():
            self.assertEqual(result[key], value)

    def test_run_query_normalizes_before_candidate_dispatch(self):
        seen = []

        def candidates(spec):
            seen.append(spec)
            return search.LaneResult([], "fixture", pre_ranked=True)

        with mock.patch.object(search, "_keyword_candidates", side_effect=candidates):
            result = search.run_query(
                "needle", session_limit=-4, family_diverse=None)
        self.assertEqual(seen[0].session_limit, 0)
        self.assertFalse(seen[0].family_diverse)
        self.assertEqual(result["engine"], "fixture")

    def test_unavailable_semantic_lane_remains_none(self):
        seen = []

        def candidates(spec):
            seen.append(spec)
            return None

        with mock.patch.object(search, "_semantic_candidates", side_effect=candidates):
            result = search.run_query("meaning", mode="semantic")
        self.assertIsNone(result)
        self.assertTrue(seen[0].family_diverse)

    def test_plumbing_surfaces_never_enable_content_recovery(self):
        calls = []

        def run_query(_query, **kwargs):
            calls.append(kwargs)
            return {
                "hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                "engine": "fixture", "mode": "keyword", "totals_exact": True,
            }

        variants = (
            ["wordy missing phrase", "-c"],
            ["wordy missing phrase", "--count-by-tier"],
            ["wordy missing phrase", "--json"],
            ["wordy missing phrase", "--flat"],
            ["wordy missing phrase", "-l"],
            ["wordy missing phrase", "--lexical"],
            ["wordy missing phrase", "--classic", "--color", "never"],
        )
        for argv in variants:
            calls.clear()
            with self.subTest(argv=argv), \
                    mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                    mock.patch.object(search.common, "MESSAGES_PATH", Path(__file__)), \
                    mock.patch.object(search.common, "in_agent_context", return_value=False), \
                    mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(search, "_semantic_runtime_installed", return_value=False), \
                    mock.patch.object(search, "_stream_first_run", return_value=None), \
                    mock.patch.object(search, "run_query", side_effect=run_query), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                search.main(argv)
            self.assertTrue(calls)
            self.assertTrue(all(call["allow_fallback"] is False for call in calls))

        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        calls.clear()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                mock.patch.object(search.common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(search.common, "in_agent_context", return_value=False), \
                mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(search, "_stream_first_run", return_value=None), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(TtyBuffer()), \
                contextlib.redirect_stderr(io.StringIO()):
            search.main(["wordy missing phrase", "--classic", "--color", "never"])
        self.assertTrue(calls)
        self.assertTrue(all(call["allow_fallback"] is True for call in calls))


if __name__ == "__main__":
    unittest.main()
