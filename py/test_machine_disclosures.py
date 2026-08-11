from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import around  # noqa: E402
import common  # noqa: E402
import corpusdb  # noqa: E402
import doctor  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import session_context  # noqa: E402
import surface_policy as surface  # noqa: E402


_DEFAULT_POLICY = object()


def _hit(session: str, turn: int, text: str, *, semantic: bool = False) -> dict:
    row = {
        "session": session,
        "turn": turn,
        "ts": 1,
        "who": "user",
        "agent": "codex",
        "project": "agrep",
        "score": 0.9,
        "matched": "phrase",
        "snippet": text,
    }
    if semantic:
        row.update(sem_score=0.9, score_kind="cosine")
    return row


def _result(hits: list[dict], *, semantic: bool = False,
            coverage: dict | None = None) -> dict:
    return {
        "hits": hits,
        "total": len(hits),
        "chats": len({hit["session"] for hit in hits}),
        "tool_hits": 0,
        "engine": "semantic:q8" if semantic else "corpusdb",
        "mode": "semantic" if semantic else "keyword",
        "totals_exact": not semantic,
        "truncated": False,
        "fallback_recommended": False,
        "semantic_status": ({
            "state": "ready",
            "complete": bool(coverage and coverage.get("complete")),
            "fallback_recommended": False,
        } if semantic else None),
        "semantic_coverage": coverage,
    }


def _window(requests) -> list[dict]:
    return [{
        "session": session,
        "center": turn,
        "first_turn": turn,
        "last_turn": turn,
        "agent": "codex",
        "project": "agrep",
        "turns": [{
            "turn": turn, "ts": 1, "who": "user",
            "text": "independent evidence", "reply": "",
        }],
        "events": [],
    } for session, turn, _context in requests]


def _records(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line]


def _envelope(output: str) -> dict:
    return _records(output)[0]


class MachineDisclosureContracts(unittest.TestCase):
    def setUp(self) -> None:
        family = session_context.CallingFamily(
            "root", "root", frozenset({"root"}), True, 5)
        self.policy = session_context.SelfExclusion(family, None, "forced")
        self.window_policy = session_context.SelfExclusion(
            family, 5, "window")
        self.failure = surface.FreshnessFailure(
            "fixture-failure", "fixture index failure", 4)

        generation = mock.patch.object(
            corpusdb, "search_generation_health",
            return_value={"state": "ready", "corpus_age_s": 0.0})
        generation.start()
        self.addCleanup(generation.stop)
        indexd_runtime._clear_freshen_failure()
        self.addCleanup(indexd_runtime._clear_freshen_failure)

    def test_exact_exclusion_count_probes_the_unfiltered_window(self) -> None:
        probes = []

        def run_query(query, *, mode="keyword", **kwargs):
            probes.append((query, mode, kwargs))
            hits = [
                _hit("root", 4, "older visible"),
                _hit("root", 5, "first hidden"),
                _hit("root", 8, "second hidden"),
            ]
            return {**_result(hits), "totals_exact": True,
                    "truncated": False}

        kwargs = {
            "limit": 40, "sort": "score", "project": "agrep",
            "exclude_session": "root", "exclude_session_from_turn": 5,
        }
        with mock.patch.object(search, "run_query", side_effect=run_query):
            keys = search._self_exclusion_match_keys(
                "needle", "keyword", kwargs, self.window_policy)

        self.assertEqual(len(keys), 2)
        self.assertEqual(len(probes), 1)
        _query, _mode, probe = probes[0]
        self.assertEqual(probe["chat"], "root")
        self.assertEqual(probe["limit"], 0)
        self.assertTrue(probe["exact_totals"])
        self.assertNotIn("exclude_session", probe)
        self.assertNotIn("exclude_session_from_turn", probe)

    def test_exact_exclusion_count_never_walks_a_large_family(self) -> None:
        members = frozenset(
            f"member-{index}"
            for index in range(search._SELF_EXCLUSION_COUNT_MAX_SESSIONS + 1))
        family = session_context.CallingFamily(
            "member-0", "member-0", members, True, None)
        policy = session_context.SelfExclusion(family, None, "forced")
        with mock.patch.object(search, "run_query") as run_query:
            keys = search._self_exclusion_match_keys(
                "needle", "keyword", {}, policy)
        self.assertIsNone(keys)
        run_query.assert_not_called()

    def test_exact_exclusion_count_refuses_an_incomplete_probe(self) -> None:
        incomplete = {**_result([_hit("root", 7, "hidden")]),
                      "total": 2, "totals_exact": False,
                      "truncated": True}
        with mock.patch.object(search, "run_query", return_value=incomplete):
            keys = search._self_exclusion_match_keys(
                "needle", "keyword", {}, self.window_policy)
        self.assertIsNone(keys)

        unavailable = {**_result([]), "index_missing": True}
        with mock.patch.object(search, "run_query", return_value=unavailable):
            keys = search._self_exclusion_match_keys(
                "needle", "keyword", {}, self.window_policy)
        self.assertIsNone(keys)

    def test_exact_exclusion_count_honors_recall_semantic_floor(self) -> None:
        strong = _hit("root", 7, "strong hidden", semantic=True)
        weak = _hit("root", 8, "weak hidden", semantic=True)
        strong["sem_score"] = recall.PROBE_MIN_SEM + 0.01
        weak["sem_score"] = recall.PROBE_MIN_SEM - 0.01
        result = {**_result(
                      [strong, weak], semantic=True,
                      coverage={"indexed": 2, "total": 2,
                                "complete": True}),
                  "totals_exact": True, "truncated": False}
        with mock.patch.object(search, "run_query", return_value=result):
            keys = search._self_exclusion_match_keys(
                "needle", "semantic", {}, self.window_policy,
                minimum_sem_score=recall.PROBE_MIN_SEM)
        self.assertEqual(len(keys), 1)

    def test_exact_exclusion_count_preserves_duplicate_row_multiplicity(self) -> None:
        duplicate = _hit("root", 7, "byte-identical hidden row")
        result = {**_result([duplicate, copy.deepcopy(duplicate)]),
                  "totals_exact": True, "truncated": False}
        with mock.patch.object(search, "run_query", return_value=result):
            keys = search._self_exclusion_match_keys(
                "needle", "keyword", {}, self.window_policy)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_no_meta_rows_are_not_attributed_to_self_exclusion(self) -> None:
        hidden_meta = _hit("root", 7, "fixture metadata row")
        hidden_meta["_meta_row"] = True
        result = _result([
            hidden_meta,
            _hit("other", 2, "independent evidence"),
        ])

        search_rc, stdout, search_stderr = self._search(
            ["needle", "--json", "--lexical", "--no-meta"], result,
            policy=self.window_policy)
        search_policy = self._envelope(stdout)["self_exclusion"]
        self.assertEqual(search_rc, 0)
        self.assertEqual(search_policy["excluded_hits"], 0)
        self.assertTrue(search_policy["excluded_hits_known"])
        self.assertNotIn("excluded", search_stderr)

        recall_rc, stdout, recall_stderr = self._recall(
            ["needle", "--json", "--lexical", "--who", "user",
             "--no-meta", "--budget", "0"], result)
        recall_policy = json.loads(stdout)["self_exclusion"]
        self.assertEqual(recall_rc, 0)
        self.assertEqual(recall_policy["excluded_hits"], 0)
        self.assertTrue(recall_policy["excluded_hits_known"])
        self.assertNotIn("excluded", recall_stderr)

    def test_coverage_scan_makes_cross_lane_exclusion_count_unknown(self) -> None:
        result = _result([
            _hit("root", 7, "current echo"),
            _hit("other", 2, "independent evidence"),
        ])
        coverage = search._CoverageRetry(search._COVERAGE_SCANNED)
        with mock.patch.object(
                search, "_overspec_retry_attempt",
                return_value=coverage), \
                mock.patch.object(
                    search, "_emit_overspec_block", return_value=True) as emit:
            rc, _stdout, stderr = self._search(
                ["needle", "--coverage", "--classic", "--color", "never"],
                result, policy=self.window_policy)
        self.assertEqual(rc, 0)
        emit.assert_called()
        self.assertNotIn("excluded", stderr)

    def _search(self, argv: list[str], result: dict | BaseException, *,
                ensure: bool = True,
                known_failure: bool = True,
                resolved_chat: str | None = "other",
                policy: object = _DEFAULT_POLICY) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        query_effect = (
            result if isinstance(result, BaseException)
            else lambda *_args, **_kwargs: copy.deepcopy(result))
        exclusion = self.policy if policy is _DEFAULT_POLICY else policy
        family = getattr(exclusion, "family", self.window_policy.family)
        with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "ensure_index", return_value=ensure), \
                mock.patch.object(
                    indexd_runtime, "indexing_failure",
                    return_value=self.failure if known_failure else None), \
                mock.patch.object(common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    common, "calling_self_exclusion",
                    return_value=exclusion), \
                mock.patch.object(
                    common, "calling_identity",
                    return_value=session_context.CallerIdentity(
                        "root", "codex")), \
                mock.patch.object(
                    common, "calling_family",
                    return_value=family), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(
                    search, "_resolve_chat", return_value=resolved_chat), \
                mock.patch.object(
                    search, "run_query", side_effect=query_effect), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def _recall(self, argv: list[str], result: dict, *,
                known_failure: bool = True,
                resolved_chat: str | None = "other") -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "indexing_failure",
                    return_value=self.failure if known_failure else None), \
                mock.patch.object(common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    common, "calling_self_exclusion",
                    return_value=self.policy), \
                mock.patch.object(
                    common, "indexed_session_prefix_candidates",
                    return_value=("other",)), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(
                    search, "_resolve_chat", return_value=resolved_chat), \
                mock.patch.object(
                    search, "run_query",
                    side_effect=lambda *_args, **_kwargs: copy.deepcopy(result)), \
                mock.patch.object(explore, "get_windows", side_effect=_window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _envelope(stdout: str) -> dict:
        """Search's run envelope: the leading agrep-meta line, present whether
        or not rows follow. State about the RUN lives here exactly once."""
        return json.loads(stdout.splitlines()[0])

    @staticmethod
    def _json_rows(stdout: str) -> list[dict]:
        """Result rows only - the agrep-meta header is envelope, not a row."""
        return [json.loads(line) for line in stdout.splitlines()[1:] if line]

    def test_search_json_matches_text_self_and_freshness_disclosures(self) -> None:
        result = _result([
            _hit("root", 7, "current echo"),
            _hit("other", 2, "independent evidence"),
        ])
        json_rc, stdout, json_stderr = self._search(
            ["needle", "--json", "--lexical"], result,
            policy=self.window_policy)
        text_rc, text_stdout, stderr = self._search(
            ["needle", "--classic", "--color", "never",
             "--lexical"], result, policy=self.window_policy)

        payload = self._envelope(stdout)
        rows = self._json_rows(stdout)
        objects = [payload, *rows]
        self.assertEqual((json_rc, text_rc), (0, 0))
        self.assertEqual([row["session"] for row in rows], ["other"])
        self.assertNotIn("current echo", text_stdout)
        self.assertIn("independent evidence", text_stdout)
        self.assertEqual(
            sum(obj.get("kind") == "agrep-meta" for obj in objects), 1)
        self.assertEqual(
            sum("excluded_hits" in obj.get("self_exclusion", {})
                for obj in objects), 1)
        self.assertEqual(payload["self_exclusion"], {
            "active": True,
            "reason": "window",
            "scope": "current-window",
            "session": "root",
            "excluded_hits": 1,
            "excluded_hits_known": True,
            "from_turn": 5,
        })
        run_fields = {
            "kind", "query", "engine", "self_exclusion", "completeness",
            "filter_coverage", "freshness", "semantic_coverage",
            "semantic_accelerator_coverage", "semantic_partial",
        }
        self.assertTrue(run_fields.isdisjoint(rows[0]))
        self.assertEqual(payload["freshness"]["state"], "degraded")
        self.assertTrue(payload["freshness"]["may_be_stale"])
        self.assertEqual(
            payload["freshness"]["reason"], self.failure.reason)
        self.assertIsNone(payload["semantic_coverage"])
        self.assertNotIn("excluded", json_stderr)
        self.assertEqual(stderr.count("excluded 1 hit"), 1)
        self.assertIn(self.failure.reason, stderr)

    def test_search_json_meta_discloses_zero_hits_and_index_failure(self) -> None:
        zero_rc, stdout, stderr = self._search(
            ["needle", "--json", "--lexical"], _result([]),
            policy=self.window_policy)
        zero = json.loads(stdout)
        self.assertEqual(zero_rc, 2)
        self.assertEqual(zero["kind"], "agrep-meta")
        self.assertEqual(zero["hits"], [])
        self.assertEqual(
            zero["self_exclusion"]["excluded_hits"], 0)
        self.assertTrue(zero["self_exclusion"]["excluded_hits_known"])
        self.assertNotIn("excluded", stderr)

        failed_rc, stdout, _ = self._search(
            ["needle", "--json", "--lexical", "--no-self"],
            _result([]), ensure=False)
        failed = json.loads(stdout)
        self.assertEqual(failed_rc, 2)
        self.assertEqual(failed["kind"], "agrep-meta")
        self.assertEqual(failed["engine"], "index:unavailable")
        self.assertEqual(
            failed["self_exclusion"],
            {"active": False, "reason": "index-unavailable"})
        self.assertEqual(failed["freshness"]["code"], self.failure.code)
        self.assertEqual(failed["completeness"]["total_basis"], "floor")
        self.assertTrue(failed["completeness"]["truncated"])

        unavailable_rc, stdout, _ = self._search(
            ["needle", "--json", "--lexical"], _result([]),
            ensure=False, known_failure=False)
        unavailable = json.loads(stdout)
        self.assertEqual(unavailable_rc, 2)
        self.assertEqual(unavailable["freshness"], {
            "state": "unavailable",
            "failing": False,
            "checked": True,
            "may_be_stale": True,
            "code": "index-unavailable",
        })
        self.assertEqual(
            unavailable["completeness"]["total_basis"], "floor")
        self.assertTrue(unavailable["completeness"]["truncated"])

    def test_empty_search_exit_is_renderer_independent_and_freshness_gated(
            self) -> None:
        empty = _result([])
        surfaces = (
            ["needle", "--lexical"],
            ["needle", "--flat", "--lexical", "--color", "never"],
            ["needle", "--json", "--lexical"],
            ["needle", "-c", "--lexical"],
        )
        for story, expected in (
                (surface.FreshnessStory("current"), 1),
                (surface.FreshnessStory(
                    "unverified", detail="fixture freshness unknown"), 2)):
            for argv in surfaces:
                with self.subTest(state=story.state, argv=argv), \
                        mock.patch.object(
                            indexd_runtime, "freshness_story",
                            return_value=story):
                    rc, _stdout, _stderr = self._search(
                        argv, empty, known_failure=False)
                self.assertEqual(rc, expected)

    def test_search_json_unknown_recap_fails_open_without_a_notice(self) -> None:
        result = _result([
            _hit("root", 7, "current echo"),
            _hit("other", 2, "independent evidence"),
        ])
        rc, stdout, stderr = self._search(
            ["needle", "--json", "--lexical"], result, policy=None)

        payload = self._envelope(stdout)
        rows = self._json_rows(stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [row["session"] for row in rows], ["root", "other"])
        self.assertEqual(payload["self_exclusion"], {
            "active": False,
            "reason": "window-unresolved",
        })
        self.assertNotIn("excluded_hits", payload["self_exclusion"])
        self.assertNotIn("excluded", stderr)

    def test_json_self_marks_only_the_exact_caller_not_its_family(self) -> None:
        family = session_context.CallingFamily(
            "root", "root", frozenset({"root", "child"}), True, 5)
        policy = session_context.SelfExclusion(family, 5, "window")
        result = _result([
            _hit("root", 4, "older caller evidence"),
            _hit("child", 99, "child evidence"),
            _hit("other", 2, "independent evidence"),
        ])
        rc, stdout, _stderr = self._search(
            ["needle", "--json", "--lexical"], result, policy=policy)
        rows = self._json_rows(stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [(row["session"], row["self"]) for row in rows],
            [("root", True), ("child", False), ("other", False)])

    def test_search_runtime_query_errors_emit_disclosure_meta(self) -> None:
        cases = (
            (["(", "--json", "-E"], re.error("bad fixture"),
             "invalid-regex", "other"),
            (["needle", "--json", "--lexical", "--chat", "missing"],
             _result([]), "chat-unresolved", None),
            (["needle", "--json", "--lexical", "--since", "never"],
             _result([]), "invalid-time-filter", "other"),
            (["--json", "--lexical"],
             _result([]), "empty-query", "other"),
        )
        for argv, result, code, resolved in cases:
            with self.subTest(code=code):
                rc, stdout, _ = self._search(
                    argv, result, resolved_chat=resolved)
                payload = json.loads(stdout)
                self.assertEqual(rc, 2)
                self.assertEqual(payload["kind"], "agrep-meta")
                self.assertEqual(payload["error"]["code"], code)
                self.assertIn("self_exclusion", payload)
                self.assertIn("freshness", payload)
                self.assertIn("semantic_coverage", payload)
                self.assertEqual(
                    payload["completeness"]["total_basis"], "floor")
                self.assertTrue(payload["completeness"]["truncated"])
                self.assertIn(
                    "did not produce a result set",
                    payload["completeness"]["action_unavailable_reason"])

    def test_search_json_carries_query_semantic_coverage(self) -> None:
        coverage = {"indexed": 4, "total": 10, "complete": False}
        rc, stdout, _ = self._search(
            ["meaning", "--json", "-s"],
            _result([_hit("other", 2, "meaning", semantic=True)],
                    semantic=True, coverage=coverage))
        self.assertEqual(rc, 0)
        self.assertEqual(self._envelope(stdout)["semantic_coverage"], coverage)

    def test_machine_self_inclusion_reasons_are_explicit_per_verb(self) -> None:
        search_cases = (
            (["needle", "--json", "--lexical", "--self"],
             _result([_hit("root", 2, "needle")]), "explicit-include"),
            (["needle", "--json", "--lexical", "--chat", "other"],
             _result([_hit("other", 2, "needle")]), "chat-filter"),
        )
        for argv, result, reason in search_cases:
            with self.subTest(verb="search", reason=reason):
                rc, stdout, _ = self._search(argv, result)
                self.assertEqual(rc, 0)
                self.assertEqual(
                    self._envelope(stdout)["self_exclusion"],
                    {"active": False, "reason": reason})

    def test_explicit_no_self_binds_inside_chat_filter_per_verb(self) -> None:
        # teach.py: "--no-self hides the whole family" - unconditionally, so an
        # explicit --no-self must keep excluding even when --chat narrows scope.
        result = _result([
            _hit("root", 7, "current echo"),
            _hit("other", 2, "independent evidence"),
        ])
        json_rc, stdout, _ = self._search(
            ["needle", "--json", "--lexical", "--no-self", "--chat", "other"],
            result)
        text_rc, _, stderr = self._search(
            ["needle", "--classic", "--color", "never",
             "--lexical", "--no-self", "--chat", "other"], result)
        payload = self._envelope(stdout)
        self.assertEqual((json_rc, text_rc), (0, 0))
        self.assertEqual(self._json_rows(stdout)[0]["session"], "other")
        self.assertTrue(payload["self_exclusion"]["active"])
        self.assertEqual(payload["self_exclusion"]["scope"], "session-family")
        self.assertEqual(
            payload["self_exclusion"]["excluded_hits"], 0)
        self.assertTrue(payload["self_exclusion"]["excluded_hits_known"])
        self.assertNotIn("excluded", stderr)

        args = ["needle", "--lexical", "--who", "user", "--hits", "2",
                "--budget", "0", "--no-self", "--chat", "other"]
        json_rc, stdout, _ = self._recall([*args, "--json"], result)
        text_rc, _, stderr = self._recall(args, result)
        payload = json.loads(stdout)
        self.assertEqual((json_rc, text_rc), (0, 0))
        self.assertEqual(
            [hit["session"] for hit in payload["hits"]], ["other"])
        self.assertTrue(payload["self_exclusion"]["active"])
        self.assertEqual(
            payload["self_exclusion"]["excluded_hits"], 0)
        self.assertTrue(payload["self_exclusion"]["excluded_hits_known"])
        self.assertNotIn("excluded", stderr)

    def test_no_self_with_own_chat_is_an_honest_empty_per_verb(self) -> None:
        # --chat <own-session> + --no-self: everything is excluded; the page
        # must say so instead of quietly returning the caller's own rows.
        own = _result([_hit("root", 7, "current echo")])
        rc, stdout, _ = self._search(
            ["needle", "--json", "--lexical", "--no-self", "--chat", "root"],
            own, resolved_chat="root")
        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["kind"], "agrep-meta")
        self.assertEqual(payload["hits"], [])
        self.assertTrue(payload["self_exclusion"]["active"])
        self.assertEqual(payload["self_exclusion"]["excluded_hits"], 1)
        text_rc, _, stderr = self._search(
            ["needle", "--classic", "--color", "never",
             "--lexical", "--no-self", "--chat", "root"],
            own, resolved_chat="root")
        self.assertEqual(text_rc, 2)
        self.assertIn("excluded 1 hit", stderr)

        rc, stdout, stderr = self._recall(
            ["needle", "--json", "--lexical", "--who", "user",
             "--budget", "0", "--no-self", "--chat", "root"],
            own, resolved_chat="root")
        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["hits"], [])
        self.assertTrue(payload["self_exclusion"]["active"])

    def test_no_auto_discloses_that_source_freshness_was_not_checked(self) -> None:
        result = _result([_hit("other", 2, "needle")])
        expected = {
            "state": "unchecked",
            "failing": False,
            "checked": False,
            "may_be_stale": True,
        }
        # an unchecked verdict is about --no-auto, so no corpus another module
        # left in the shared data dir may answer for it
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(common, "DATA_DIR", Path(raw)):
            search_rc, stdout, _ = self._search(
                ["needle", "--json", "--lexical", "--no-auto"],
                result, known_failure=False)
            self.assertEqual(search_rc, 0)
            self.assertEqual(self._envelope(stdout)["freshness"], expected)

            recall_rc, stdout, _ = self._recall(
                ["needle", "--json", "--lexical", "--who", "user",
                 "--budget", "0", "--no-auto"], result, known_failure=False)
        self.assertEqual(recall_rc, 0)
        self.assertEqual(json.loads(stdout)["freshness"], expected)

        unverified = surface.FreshnessStory(
            "unverified", code="freshness-unchecked",
            detail=indexd_runtime.NO_AUTO_REFRESH_REASON)
        empty = _result([])
        with mock.patch.object(
                indexd_runtime, "freshness_story", return_value=unverified):
            compact_rc, _, _ = self._search(
                ["missing", "--lexical", "--no-auto"], empty,
                known_failure=False)
            json_rc, _, _ = self._search(
                ["missing", "--json", "--lexical", "--no-auto"], empty,
                known_failure=False)
            probe_rc, _, _ = self._recall(
                ["missing", "--probe", "--lexical", "--no-auto"], empty,
                known_failure=False)
            recall_json_rc, _, _ = self._recall(
                ["missing", "--json", "--lexical", "--who", "user",
                 "--no-auto"], empty, known_failure=False)
        self.assertEqual(
            (compact_rc, json_rc, probe_rc, recall_json_rc), (2, 2, 2, 2))

    def test_search_and_recall_share_owned_publication_disclosure(self) -> None:
        result = _result([_hit("other", 2, "needle")])
        moving = {"state": "generation-moving", "detail": "proof moved",
                  "corpus_age_s": 0.1}
        for verb in ("search", "recall"):
            with self.subTest(verb=verb), \
                    mock.patch.object(
                        indexd_runtime, "foreground_refresh_converging",
                        return_value=True) as converging, \
                    mock.patch.object(
                        corpusdb, "search_generation_health",
                        return_value=moving):
                if verb == "search":
                    rc, stdout, _ = self._search(
                        ["needle", "--json", "--lexical"], result,
                        known_failure=False)
                else:
                    rc, stdout, _ = self._recall(
                        ["needle", "--json", "--lexical", "--who", "user",
                         "--budget", "0"], result, known_failure=False)
            payload = _envelope(stdout)
            self.assertEqual(rc, 0)
            self.assertEqual(payload["freshness"]["state"], "index-behind")
            self.assertEqual(
                payload["freshness"]["cause"], "publication-in-progress")
            self.assertFalse(payload["freshness"]["failing"])
            converging.assert_called_once_with(checked=True)
        recall_cases = (
            (["needle", "--json", "--lexical", "--who", "user",
              "--budget", "0", "--self"], "explicit-include"),
            (["needle", "--json", "--lexical", "--who", "user",
              "--budget", "0", "--chat", "other"], "chat-filter"),
        )
        result = _result([_hit("other", 2, "needle")])
        for argv, reason in recall_cases:
            with self.subTest(verb="recall", reason=reason):
                rc, stdout, _ = self._recall(argv, result)
                self.assertEqual(rc, 0)
                self.assertEqual(
                    json.loads(stdout)["self_exclusion"],
                    {"active": False, "reason": reason})

    def test_recall_json_matches_text_self_and_freshness_disclosures(self) -> None:
        result = _result([
            _hit("root", 7, "current echo"),
            _hit("other", 2, "independent evidence"),
        ])
        args = ["needle", "--lexical", "--who", "user", "--hits", "2",
                "--budget", "0", "--no-self"]
        json_rc, stdout, _ = self._recall([*args, "--json"], result)
        text_rc, _, stderr = self._recall(args, result)

        payload = json.loads(stdout)
        self.assertEqual((json_rc, text_rc), (0, 0))
        self.assertEqual(payload["hits"][0]["session"], "other")
        self.assertEqual(payload["self_exclusion"]["excluded_hits"], 1)
        self.assertTrue(payload["self_exclusion"]["excluded_hits_known"])
        self.assertEqual(payload["self_exclusion"]["scope"], "session-family")
        self.assertEqual(
            payload["freshness"]["reason"], self.failure.reason)
        self.assertIsNone(payload["semantic_coverage"])
        self.assertIn("excluded 1 hit", stderr)
        self.assertIn(self.failure.reason, stderr)

    def test_recall_json_carries_semantic_coverage_and_error_exit(self) -> None:
        coverage = {"indexed": 7, "total": 11, "complete": False}
        semantic = _result(
            [_hit("other", 2, "meaning", semantic=True)],
            semantic=True, coverage=coverage)
        rc, stdout, _ = self._recall(
            ["meaning", "-s", "--who", "user", "--json", "--budget", "0"],
            semantic)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout)["semantic_coverage"], coverage)

        none_rc, stdout, _ = self._recall(
            ["missing", "--json", "--lexical", "--who", "user"],
            _result([]))
        none = json.loads(stdout)
        self.assertEqual(none_rc, 2)
        self.assertEqual(none["hits"], [])
        self.assertIn("self_exclusion", none)
        self.assertIn("freshness", none)
        self.assertIn("semantic_coverage", none)

        output = io.StringIO()
        with mock.patch.object(
                indexd_runtime, "ensure_index", return_value=False), \
                mock.patch.object(
                    indexd_runtime, "indexing_failure",
                    return_value=self.failure), \
                contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["meaning", "--json", "--no-self"])
        failed = json.loads(output.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(failed["engine"], "index:unavailable")
        self.assertEqual(failed["freshness"]["code"], self.failure.code)
        self.assertEqual(
            failed["self_exclusion"]["reason"], "index-unavailable")

    def test_recall_runtime_query_errors_emit_disclosure_envelopes(self) -> None:
        cases = (
            (["needle", "--json", "--lexical", "--chat", "missing"],
             "chat-unresolved", None),
            (["needle", "--json", "--lexical", "--since", "never"],
             "invalid-time-filter", "other"),
            (["   ", "--json"], "empty-query", "other"),
        )
        for argv, code, resolved in cases:
            with self.subTest(code=code):
                rc, stdout, _ = self._recall(
                    argv, _result([]), resolved_chat=resolved)
                payload = json.loads(stdout)
                self.assertEqual(rc, 2)
                self.assertEqual(payload["error"]["code"], code)
                self.assertIn("self_exclusion", payload)
                self.assertIn("freshness", payload)
                self.assertIn("semantic_coverage", payload)

    def test_doctor_json_matches_text_freshness_and_semantic_coverage(self) -> None:
        coverage = {"indexed": 1, "total": 3, "complete": False}
        smart = {
            "live": True,
            "available": True,
            "optional": True,
            "install_hint": "fixture",
            "unavailable_reason": None,
            "deps": {
                "numpy": True, "onnxruntime": True, "tokenizers": True},
            "model_cached": True,
            "embeddings": "partial",
            "embedding_coverage": coverage,
            "embed_job": "running",
            "embed_fail_reason": None,
            "embed_pid": 123,
            "embed_phase": "embedding",
            "embed_done": 1,
            "embed_total": 3,
            "embed_running": True,
            "resident_worker": {"running": False},
        }
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)), \
                mock.patch.object(doctor.common, "DATA_DIR", Path(td)), \
                mock.patch.object(doctor, "INGEST_BIN", Path(__file__)), \
                mock.patch.object(doctor.shutil, "which", return_value="/cargo"), \
                mock.patch.object(doctor, "_semantic_probe", return_value=smart), \
                mock.patch.object(doctor, "_store_counts", return_value=[]), \
                mock.patch.object(
                    doctor, "_corpus_db_readiness",
                    return_value={"state": "missing", "detail": "missing"}), \
                mock.patch.object(
                    corpusdb, "search_generation_health",
                    return_value={
                        "state": corpusdb.GENERATION_VERIFICATION_DEFERRED,
                        "detail": "fixture generation proof deferred",
                        "corpus_age_s": 4.2,
                    }), \
                mock.patch.object(common, "index_summary", return_value=None), \
                mock.patch.object(
                    common, "data_dir_usage",
                    return_value={"bytes": 0, "files": 0}), \
                mock.patch.object(
                    indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}), \
                mock.patch.object(
                    indexd_runtime, "indexing_failure",
                    return_value=self.failure), \
                mock.patch.object(
                    indexd_runtime, "indexd_failing",
                    return_value=(4, self.failure.reason)), \
                mock.patch.object(common, "detected_stores", return_value=[]), \
                mock.patch.object(doctor, "_footprint_breakdown", return_value=""), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            json_rc = doctor.main(["--json"])
            payload = json.loads(stdout.getvalue())
            stdout.seek(0)
            stdout.truncate(0)
            doctor.report()
            rendered = stdout.getvalue()

        self.assertEqual(json_rc, 0)
        self.assertEqual(
            payload["self_exclusion"],
            {"active": False, "reason": "not-applicable"})
        self.assertEqual(payload["freshness"]["reason"], self.failure.reason)
        self.assertEqual(payload["semantic_coverage"], coverage)
        self.assertTrue(payload["freshness"]["checked"])
        self.assertIn(self.failure.reason, rendered)
        self.assertIn("1/3 rows", rendered)

    def test_machine_freshness_strips_terminal_control(self) -> None:
        failure = surface.FreshnessFailure(
            "unsafe", "worker \x1b[31mfailed", 3)
        with mock.patch.object(
                indexd_runtime, "indexing_failure", return_value=failure):
            payload = indexd_runtime.machine_freshness()
        self.assertNotIn("\x1b", payload["reason"])
        self.assertTrue(payload["failing"])
        self.assertEqual(payload["code"], "unsafe")
        self.assertEqual(payload["consecutive_failures"], 3)
        self.assertEqual(
            surface.self_exclusion_disclosure(
                self.policy, inactive_reason="unused")["excluded_hits"],
            None)

    def test_around_json_gets_a_freshness_channel_on_a_failing_index(self) -> None:
        session = "0199cccc-0000-7000-8000-000000000009"
        window = {
            "session": session, "center": 3, "first_turn": 0, "last_turn": 9,
            "agent": "codex", "project": "agrep", "concept": "", "title": "",
            "turns": [{"turn": 3, "ts": 1, "who": "user",
                       "text": "stored evidence", "reply": ""}],
            "events": [],
        }

        def run(known_failure: bool) -> tuple[int, list[dict]]:
            stdout = io.StringIO()
            with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "indexing_failure",
                        return_value=self.failure if known_failure else None), \
                    mock.patch.object(
                        explore, "resolve_session", return_value=[session]), \
                    mock.patch.object(
                        explore, "get_window", return_value=window), \
                    mock.patch.object(
                        explore, "_session_index", return_value=[session]), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = around.main([session, "3", "--json"])
            rows = [json.loads(line)
                    for line in stdout.getvalue().splitlines()]
            return rc, rows

        rc, rows = run(known_failure=True)
        self.assertEqual(rc, 0)
        meta = rows[-1]
        self.assertEqual(meta["kind"], "agrep-meta")
        self.assertTrue(meta["freshness"]["failing"])
        self.assertEqual(meta["freshness"]["reason"], self.failure.reason)
        self.assertEqual(
            [row["kind"] for row in rows[:-1]], ["agrep-meta", "msg"])
        self.assertIn("scope", rows[0])

        rc, rows = run(known_failure=False)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [row["kind"] for row in rows], ["agrep-meta", "msg"])
        self.assertIn("scope", rows[0])

    def test_recall_json_budget_keeps_a_hard_disclosure_floor(self) -> None:
        obj = {
            "query": "needle",
            "engine": "corpusdb",
            "hits": [_hit("other", 2, "needle")],
            "self_exclusion": {
                "active": False, "reason": "machine-surface"},
            "freshness": {
                "state": "unchecked", "failing": False,
                "checked": False, "may_be_stale": True},
            "semantic_coverage": None,
        }
        raw = recall._fit_json_payload(obj, recall.MIN_JSON_BUDGET)
        payload = json.loads(raw)
        self.assertLessEqual(len(raw), recall.MIN_JSON_BUDGET)
        self.assertIn("self_exclusion", payload)
        self.assertIn("freshness", payload)
        self.assertIn("semantic_coverage", payload)

        for budget in (64, 128, 256):
            with self.subTest(budget=budget):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as raised:
                    recall.main([
                        "missing", "--json", "--lexical",
                        "--budget", str(budget)])
                self.assertEqual(raised.exception.code, 2)


class BudgetShrinkKeepsOwnMatchTests(unittest.TestCase):
    """The JSON budget shrink anchors each row to its OWN query: pack-wide
    first-pattern-wins kept another query's incidental head match and cut the
    phrase the row existed to show (matched=phrase, phrase gone at 4400-5000)."""

    Q1 = "maple syrup protocol"
    Q2 = "reproduced at step nine"

    def _envelope(self) -> dict:
        filler = "unrelated context words about the long refactor log " * 40
        q2_text = (f"the {self.Q1} notes were merged. " + filler
                   + f"and then the quokka fence deadlock {self.Q2} of the run")
        return {
            "query": [self.Q1, self.Q2],
            "engine": "corpusdb",
            "hits": [
                {"session": "44444444-4444-4444-8444-444444444444", "turn": 0,
                 "matched": "phrase", "window": [
                     {"kind": "msg", "session": "4444", "turn": 0,
                      "who": "user", "ts": 1,
                      "text": f"all the {self.Q1} notes were merged yesterday",
                      "omitted_chars": 0}]},
                {"session": "55555555-5555-4555-8555-555555555555", "turn": 0,
                 "matched": "phrase", "window": [
                     {"kind": "msg", "session": "5555", "turn": 0,
                      "who": "user", "ts": 1, "text": q2_text,
                      "omitted_chars": 0}]},
            ],
        }

    def test_the_shrunk_row_keeps_its_own_phrase(self) -> None:
        obj = self._envelope()
        anchors = [recall._anchor_patterns(self.Q1),
                   recall._anchor_patterns(self.Q2)]
        for budget in (1400, 1800, 2200):
            with self.subTest(budget=budget):
                raw = recall._fit_json_payload(
                    copy.deepcopy(obj), budget, hit_anchors=anchors)
                payload = json.loads(raw)
                self.assertLessEqual(len(raw), budget)
                q2_hit = next(
                    hit for hit in payload["hits"]
                    if hit["session"].startswith("55555555"))
                self.assertTrue(
                    any(self.Q2 in (row.get("text") or "")
                        for row in q2_hit["window"]),
                    f"q2 hit lost its own phrase: {q2_hit['window']}")


class StoryLineSaidOnceTests(unittest.TestCase):
    """The freshness story is one line per render, whatever path serves it.

    HONEST SCOPE: these paths are single-lined today with or without the
    once-guard, so this pins the future, not a reproduced bug. The reported
    double-render on a wedged box (whole hedge+result block twice) is NOT
    reproduced by any argv here and remains unexplained. What the guard buys
    is that ten recall sites and four search sites no longer each have to be
    the last one to run for the invariant to hold.
    """

    NOTICE = "history may be stale: the freshness owner is blocked"

    @contextlib.contextmanager
    def _wedged(self):
        with mock.patch.object(indexd_runtime, "agent_freshness_notice",
                               return_value=self.NOTICE), \
                mock.patch.object(indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(
                    search, "run_query", return_value=_result([])), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False):
            yield

    def _stderr_of(self, entry, argv) -> str:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            with contextlib.suppress(SystemExit):
                entry(argv)
        return err.getvalue()

    def test_every_recall_path_says_the_story_once(self) -> None:
        for argv in (["zzzznotathing"], ["zzzznotathing", "--probe"],
                     ["zzzznotathing", "--json"], ["deadlock"],
                     ["deadlock", "--probe"], ["deadlock", "--json"],
                     ["@ffffffff:1"], ["deadlock", "--agent", "gemini"]):
            with self.subTest(argv=argv), self._wedged():
                err = self._stderr_of(recall.main, [*argv, "--no-auto"])
                self.assertLessEqual(
                    err.count(self.NOTICE), 1,
                    f"recall {argv} printed the freshness story "
                    f"{err.count(self.NOTICE)} times")

    def test_every_search_path_says_the_story_once(self) -> None:
        for argv in (["zzzznotathing"], ["deadlock"], ["deadlock", "--json"],
                     ["deadlock", "-c"], ["deadlock", "-l"],
                     ["deadlock", "--flat"], ["deadlock", "--agent", "gemini"]):
            with self.subTest(argv=argv), self._wedged():
                err = self._stderr_of(search.main, [*argv, "--no-auto"])
                self.assertLessEqual(
                    err.count(self.NOTICE), 1,
                    f"search {argv} printed the freshness story "
                    f"{err.count(self.NOTICE)} times")


if __name__ == "__main__":
    unittest.main()
