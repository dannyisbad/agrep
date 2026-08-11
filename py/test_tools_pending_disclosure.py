"""The tools-pending window is one fact, read everywhere (hunt 3 cluster C).

While the derived FTS build is owned elsewhere, the keyword lane serves prose
only. That narrowing must be a field on the result - never a stderr line the
renderers cannot see - and a tool-only query in the window must be refused
with the pending story, not a confident zero over the full corpus.
"""

from __future__ import annotations

import io
import contextlib
import json
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import corpusdb  # noqa: E402
import common  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


class ToolsPendingLane(unittest.TestCase):
    """The lane records the exclusion on its result, once, at the one site
    that decides it."""

    def _pending_window(self, stack: contextlib.ExitStack) -> None:
        search.corpusdb = corpusdb
        stack.enter_context(mock.patch.object(
            corpusdb, "connect", return_value=None))
        stack.enter_context(mock.patch.object(
            corpusdb, "_trigram_ok", return_value=True))
        stack.enter_context(mock.patch.object(
            corpusdb, "DB_PATH", Path("/nonexistent/corpus.db")))
        stack.enter_context(mock.patch.object(
            search, "_native_event_shape", return_value=False))
        stack.enter_context(mock.patch.object(
            search, "_jsonl_bounded_single_keyword_rows", return_value=None))
        stack.enter_context(mock.patch.object(
            search.common, "setting",
            side_effect=lambda name: "on" if name == "tools" else "off"))
        stack.enter_context(mock.patch.object(
            explore, "direct_snapshot_attempt",
            side_effect=lambda **_kwargs: contextlib.nullcontext()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        search._SLOW_LANE_ANNOUNCED.clear()

    def test_tool_only_query_is_refused_with_the_pending_fact(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=True))
            res = search.run_query("cargo build failure", who="tool")
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["engine"], "none")
        self.assertTrue(res.get("tools_excluded"))
        self.assertFalse(res.get("index_missing", False))
        # the total is a floor of a corpus this lane never searched
        self.assertFalse(res.get("totals_exact", True))

    def test_tool_only_query_with_nothing_queued_uses_the_exact_scan(
            self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=False))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query("cargo build failure", who="tool")
        self.assertFalse(res.get("index_missing", False))
        self.assertFalse(res.get("tools_excluded", False))
        self.assertTrue(res.get("totals_exact", True))
        self.assertTrue(explored.call_args.args[2]["include_tools"])

    def test_prose_lane_records_the_exclusion_it_applied(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=True))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query("cargobuild")
        self.assertTrue(res.get("tools_excluded"))
        self.assertFalse(res.get("totals_exact", True))
        # the exclusion reached the scan, not just the disclosure
        self.assertFalse(explored.call_args.args[2]["include_tools"])

    def test_live_fts_build_skips_event_scan_and_serves_prose(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                corpusdb, "query_search_index_build_active",
                return_value=True))
            stack.enter_context(mock.patch.object(
                corpusdb, "query_publication_active",
                side_effect=AssertionError("the FTS build is already classified")))
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=False))
            stack.enter_context(mock.patch.object(
                search, "_native_event_shape", return_value=True))
            native = stack.enter_context(mock.patch.object(
                search, "_jsonl_native_keyword",
                side_effect=AssertionError("tool events must wait for FTS")))
            direct = stack.enter_context(mock.patch.object(
                explore, "direct_snapshot_attempt",
                side_effect=lambda **_kwargs: contextlib.nullcontext()))
            events = stack.enter_context(mock.patch.object(
                explore.common, "event_blobs_bulk",
                side_effect=AssertionError("tool events must not be read")))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query(
                "cargo build failure",
                who=surface.SpeakerFilter(("user", "tool"), ()))
        self.assertTrue(res.get("tools_excluded"))
        self.assertFalse(res.get("totals_exact", True))
        self.assertFalse(explored.call_args.args[2]["include_tools"])
        self.assertFalse(explored.call_args.args[2]["_tool_lane_enabled"])
        direct.assert_called_once_with(include_events=False)
        events.assert_not_called()
        native.assert_not_called()

    def test_live_fts_build_keeps_exact_prose_filters_exact(self) -> None:
        cases = (
            ("who-user", "user", "on"),
            ("no-who-tool", surface.SpeakerFilter(None, ("tool",)), "on"),
            ("tools-off", None, "off"),
        )
        for label, who, tools_setting in cases:
            with self.subTest(label=label), contextlib.ExitStack() as stack:
                self._pending_window(stack)
                stack.enter_context(mock.patch.object(
                    corpusdb, "query_search_index_build_active",
                    return_value=True))
                stack.enter_context(mock.patch.object(
                    search.common, "setting",
                    side_effect=lambda name: (
                        tools_setting if name == "tools" else "off")))
                explored = stack.enter_context(mock.patch.object(
                    explore, "keyword_search", return_value={"hits": []}))
                res = search.run_query("cargo build failure", who=who)
            self.assertFalse(res.get("tools_excluded", False))
            self.assertTrue(res.get("totals_exact", True))
            self.assertFalse(explored.call_args.args[2]["_tool_lane_enabled"])

    def test_live_fts_build_refuses_a_speaker_filter_tool_only_page(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                corpusdb, "query_search_index_build_active",
                return_value=True))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search"))
            res = search.run_query(
                "cargo build failure",
                who=surface.SpeakerFilter(("tool",), ()))
        self.assertEqual(res["engine"], "none")
        self.assertTrue(res.get("tools_excluded"))
        self.assertFalse(res.get("totals_exact", True))
        explored.assert_not_called()

    def test_queued_build_keeps_no_who_tool_exact(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=True))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query(
                "cargo build failure",
                who=surface.SpeakerFilter(None, ("tool",)))
        self.assertFalse(res.get("tools_excluded", False))
        self.assertTrue(res.get("totals_exact", True))
        self.assertFalse(explored.call_args.args[2]["_tool_lane_enabled"])

    def test_no_queued_build_means_the_scan_serves_tool_rows(self) -> None:
        # nothing will absorb the gap, so nothing may be dropped: the scan
        # carries tool rows itself and the result claims no narrowing
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=False))
            explored = stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query("cargobuild")
        self.assertFalse(res.get("tools_excluded", False))
        self.assertTrue(res.get("totals_exact", True))
        self.assertTrue(explored.call_args.args[2]["include_tools"])

    def test_prose_only_spec_gave_up_nothing_and_claims_nothing(self) -> None:
        with contextlib.ExitStack() as stack:
            self._pending_window(stack)
            stack.enter_context(mock.patch.object(
                search.indexd_runtime, "search_index_build_pending",
                return_value=True))
            stack.enter_context(mock.patch.object(
                explore, "keyword_search", return_value={"hits": []}))
            res = search.run_query("cargobuild", include_tools=False)
        self.assertFalse(res.get("tools_excluded", False))

    def test_count_and_json_surfaces_forfeit_on_the_exclusion(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                    "engine": "explore", "mode": "keyword",
                    "totals_exact": False, "tools_excluded": True}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(search.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(search.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(search, "_stream_first_run",
                                  return_value=None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = search.main(["cargo build failure", "-c"])
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue().strip(), ">=0")
        self.assertIn("tool output is not indexed yet", err.getvalue())
        self.assertNotIn("stopped early", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(search.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(search.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(search, "_stream_first_run",
                                  return_value=None), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = search.main(["cargo build failure", "--who", "tool",
                              "--json"])
        self.assertEqual(rc, 2)
        meta = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(meta["error"]["code"],
                         surface.TOOLS_PENDING_ERROR_CODE)
        self.assertEqual(meta["completeness"]["total_basis"], "floor")
        self.assertEqual(meta["tools_excluded"],
                         {"reason": surface.TOOLS_PENDING_ERROR_CODE})

        def pending_count(_query, *, mode="keyword", **_kwargs):
            return {"hits": [], "total": 7, "chats": 3, "tool_hits": 0,
                    "engine": "explore", "mode": "keyword",
                    "totals_exact": False, "tools_excluded": True}

        for argv, expected_stdout in (
                (["cargo build failure", "-c"], ">=7"),
                (["cargo build failure", "--count-by-tier"], "")):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(
                        search.indexd_runtime, "ensure_index",
                        return_value=True), mock.patch.object(
                        search.common, "in_agent_context", return_value=False), \
                        mock.patch.object(
                            search, "run_query", side_effect=pending_count), \
                        mock.patch.object(
                            search, "_stream_first_run", return_value=None), \
                        contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = search.main(argv)
                self.assertEqual(rc, 2)
                self.assertEqual(out.getvalue().strip(), expected_stdout)
                self.assertIn("tool output", err.getvalue())


class RecallToolsPendingRegression(unittest.TestCase):
    SESSION = "0199aaaa-0000-7000-8000-000000000001"

    @staticmethod
    def _result(hits: list[dict], *, pending: bool = False) -> dict:
        return {
            "hits": hits, "total": len(hits),
            "chats": len({hit["session"] for hit in hits}),
            "phrase_chats": len({hit["session"] for hit in hits}),
            "tool_hits": 0, "engine": "none" if pending else "explore",
            "mode": "keyword", "totals_exact": not pending,
            **({"tools_excluded": True} if pending else {}),
        }

    @classmethod
    def _hit(cls) -> dict:
        return {
            "session": cls.SESSION, "turn": 3, "ts": 10,
            "who": "user", "agent": "codex", "project": "agrep",
            "score": 1.0, "matched": "phrase", "snippet": "needle",
            "content_digest": "beef",
        }

    @classmethod
    def _window(cls) -> dict:
        return {
            "session": cls.SESSION, "center": 3,
            "first_turn": 3, "last_turn": 3,
            "agent": "codex", "project": "agrep", "events": [],
            "turns": [{
                "turn": 3, "who": "user", "ts": 10,
                "text": "needle", "reply": "the useful answer",
            }],
        }

    def _run(self, argv: list[str], prose_hits: list[dict]) -> tuple[
            int, str, str, list[dict]]:
        stdout, stderr = io.StringIO(), io.StringIO()
        calls: list[dict] = []

        def run_query(_query, *, mode="keyword", **kwargs):
            self.assertEqual(mode, "keyword")
            calls.append(dict(kwargs))
            if kwargs.get("who") == "tool":
                return self._result([], pending=True)
            self.assertIs(kwargs.get("include_tools"), False)
            return self._result(prose_hits)

        freshness = surface.FreshnessStory("current")
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(indexd_runtime, "freshness_story",
                              return_value=freshness),
            mock.patch.object(indexd_runtime, "agent_freshness_notice",
                              return_value=""),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(common, "index_summary",
                              return_value={"sessions": 12}),
            mock.patch.object(common, "indexed_session_prefix_candidates",
                              return_value=(self.SESSION,)),
            mock.patch.object(search, "_semantic_runtime_installed",
                              return_value=False),
            mock.patch.object(search, "run_query", side_effect=run_query),
            mock.patch.object(explore, "get_windows",
                              return_value=[self._window()] if prose_hits else []),
            mock.patch.object(recall, "_expand",
                              side_effect=lambda pairs, *_args, **_kwargs: pairs),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = recall.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue(), calls

    def _run_real_pending(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        freshness = surface.FreshnessStory("current")
        search._SLOW_LANE_ANNOUNCED.clear()
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(indexd_runtime, "freshness_story",
                              return_value=freshness),
            mock.patch.object(indexd_runtime, "agent_freshness_notice",
                              return_value=""),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(common, "index_summary",
                              return_value={"sessions": 12}),
            mock.patch.object(
                common, "setting",
                side_effect=lambda name: "on" if name == "tools" else "off"),
            mock.patch.object(search, "_semantic_runtime_installed",
                              return_value=False),
            mock.patch.object(corpusdb, "connect", return_value=None),
            mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
            mock.patch.object(corpusdb, "DB_PATH",
                              Path("/nonexistent/corpus.db")),
            mock.patch.object(corpusdb, "query_search_index_build_active",
                              return_value=True),
            mock.patch.object(corpusdb, "query_publication_active",
                              return_value=False),
            mock.patch.object(search, "_native_event_shape", return_value=False),
            mock.patch.object(search, "_jsonl_bounded_single_keyword_rows",
                              return_value=None),
            mock.patch.object(
                explore, "direct_snapshot_attempt",
                side_effect=lambda **_kwargs: contextlib.nullcontext()),
            mock.patch.object(explore, "keyword_search", return_value={"hits": []}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = recall.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_probe_miss_is_unverified_with_one_pending_line(self) -> None:
        rc, stdout, stderr, calls = self._run(
            ["needle", "--probe", "--lexical", "--self", "--no-auto"], [])
        lines = [line for line in (stdout + stderr).splitlines() if line]
        self.assertEqual(rc, 2)
        self.assertEqual(len(lines), 1)
        self.assertIn("tool output isn't indexed yet", lines[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("who"), "tool")

    def test_json_miss_carries_structured_pending_error(self) -> None:
        rc, stdout, stderr, _calls = self._run(
            ["needle", "--json", "--lexical", "--self", "--no-auto"], [])
        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["error"]["code"],
                         surface.TOOLS_PENDING_ERROR_CODE)
        self.assertEqual(payload["tools_excluded"],
                         {"reason": surface.TOOLS_PENDING_ERROR_CODE})

    def test_json_hit_discloses_pending_tool_rescue_without_failure(self) -> None:
        rc, stdout, stderr, calls = self._run(
            ["needle", "--json", "--hits", "2", "--lexical", "--self",
             "--no-auto"], [self._hit()])
        payload = json.loads(stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(payload["hits"]), 1)
        self.assertNotIn("error", payload)
        self.assertEqual(payload["tools_excluded"],
                         {"reason": surface.TOOLS_PENDING_ERROR_CODE})
        self.assertEqual(sum(call.get("who") == "tool" for call in calls), 1)

    def test_real_pending_search_probe_owns_one_disclosure(self) -> None:
        rc, stdout, stderr = self._run_real_pending(
            ["needle", "--probe", "--lexical", "--self", "--no-auto"])
        lines = [line for line in (stdout + stderr).splitlines() if line]
        self.assertEqual(rc, 2)
        self.assertEqual(len(lines), 1)
        self.assertIn("tool output isn't indexed yet", lines[0])

    def test_real_pending_search_json_is_structured_and_stderr_clean(self) -> None:
        rc, stdout, stderr = self._run_real_pending(
            ["needle", "--json", "--lexical", "--self", "--no-auto"])
        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["error"]["code"],
                         surface.TOOLS_PENDING_ERROR_CODE)
        self.assertEqual(payload["tools_excluded"],
                         {"reason": surface.TOOLS_PENDING_ERROR_CODE})

    def test_minimum_probe_budget_keeps_pending_cause_for_hit_and_miss(
            self) -> None:
        weak_hit = self._hit()
        weak_hit.update({"matched": "all-terms", "_evidence": 1.0})
        cases = (("miss", [], 2), ("hit", [weak_hit], 0))
        for label, hits, expected_rc in cases:
            with self.subTest(label=label):
                rc, stdout, stderr, _calls = self._run(
                    ["needle", "--probe", "--budget", "64", "--lexical",
                     "--self", "--no-auto"], hits)
                rendered = stdout + stderr
                self.assertEqual(rc, expected_rc)
                self.assertLessEqual(len(rendered.encode("utf-8")), 64)
                self.assertTrue(rendered.startswith(
                    surface.SCAN_TOOLS_PENDING_LINE))

    def test_minimal_json_envelope_keeps_pending_cause(self) -> None:
        generation_fields = {
            "freshness": {
                "state": "current", "failing": False,
                "checked": False, "may_be_stale": False,
            },
            "fit_pressure": "x" * 8_000,
        }
        with mock.patch.object(
                corpusdb, "machine_freshness_fields",
                return_value=generation_fields):
            rc, stdout, stderr, _calls = self._run(
                ["needle", "--json", "--hits", "2", "--budget", "2048",
                 "--lexical", "--self", "--no-auto"], [self._hit()])
        payload = json.loads(stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        self.assertLessEqual(len(stdout.encode("utf-8")), 2048)
        self.assertEqual(payload["hits"], [])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["omitted_hits"], 1)
        self.assertEqual(payload["tools_excluded"],
                         {"reason": surface.TOOLS_PENDING_ERROR_CODE})

    def test_minimal_json_envelope_does_not_add_a_pending_field(self) -> None:
        raw = recall._fit_json_payload({
            "query": "needle", "engine": "fixture", "hits": [],
            "fit_pressure": "x" * 8_000,
        }, 2048)
        payload = json.loads(raw)
        self.assertTrue(payload["truncated"])
        self.assertNotIn("tools_excluded", payload)


if __name__ == "__main__":
    unittest.main()
