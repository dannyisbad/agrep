"""Focused compact pagination and continuation contract tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import compact  # noqa: E402
import corpusdb  # noqa: E402
import events  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402


def _hit(index: int, *, family: str | None = None, matched: str | None = None,
         score: float = 1.0) -> dict:
    text = f"result {index}"
    hit = {
        "session": f"session{index:04d}",
        "turn": index,
        "score": score,
        "family": family or f"family{index:04d}",
        "text": text,
        "content_digest": compact.content_digest(text),
    }
    if matched:
        hit["matched"] = matched
    return hit


def _tool_hit(index: int, *, session: str = "tool-session",
              turn: int = 1, query: str = "TOOL_RESCUE_NEEDLE") -> dict:
    output = f"{query} unique outcome {index}"
    text = f"tool-{index}: step-{index}\n{output}"
    ts = 1000 - index
    return {
        "session": session, "turn": turn, "ts": ts,
        "who": "tool", "agent": "codex", "project": "p",
        "event_kind": "tool", "kind": "tool", "name": f"tool-{index}",
        "ok": True, "output": output, "output_truncated": False,
        "score": 0.22, "snippet": text,
        "content_digest": compact.content_digest(text),
        "_event_identity": search.common.tool_event_identity(
            session, turn, ts, text),
        "_match_span": (text.index(query), text.index(query) + len(query)),
    }


class CompactTests(unittest.TestCase):
    def test_profile_defaults_for_agents_and_explicit_overrides_win(self) -> None:
        env = {"AGREP_PROFILE": "compact", "CODEX_THREAD_ID": "ambient"}
        self.assertTrue(compact.profile_enabled(environ=env))
        self.assertFalse(compact.profile_enabled(classic=True, environ=env))
        self.assertFalse(compact.profile_enabled(json_mode=True, environ=env))
        self.assertTrue(compact.profile_enabled(environ={"CODEX_THREAD_ID": "ambient"}))
        self.assertTrue(compact.profile_enabled(
            environ={"CODEX_THREAD_ID": "ambient", "AGREP_PROFILE": ""}))
        self.assertFalse(compact.profile_enabled(environ={"TERM": "xterm"}))
        self.assertFalse(compact.profile_enabled(
            environ={"CODEX_THREAD_ID": "ambient", "AGREP_PROFILE": "classic"}))
        self.assertTrue(compact.profile_enabled(
            environ={"CODEX_THREAD_ID": "ambient", "AGREP_PROFILE": "unknown"}))
        self.assertFalse(compact.profile_enabled(
            environ={"TERM": "xterm", "AGREP_PROFILE": "unknown"}))

    def test_semantic_profile_pages_even_explicit_unlimited_results(self) -> None:
        hits = [{"session": f"semantic{i:04d}", "turn": i, "ts": 1,
                 "who": "user", "agent": "codex", "project": "p",
                 "sem_score": 1.0 - i / 1000,
                 "content_digest": compact.content_digest(
                     f"meaning {i} " + "x" * 400),
                 "snippet": f"meaning {i} " + "x" * 400}
                for i in range(100)]
        result = {"hits": hits, "total": 100, "chats": 100,
                  "engine": "semantic:hybrid", "mode": "semantic",
                  "semantic_status": {"state": "ready"},
                  "totals_exact": True}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(search.common, "DATA_DIR", Path(td)), \
                mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch("explore._session_index",
                           return_value={hit["session"]: {} for hit in hits}):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = search.main(["meaning", "-s", "-n", "0", "--color", "never"])
        lines = stdout.getvalue().splitlines()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(lines), compact.MIN_PAGE_HITS)
        self.assertLessEqual(len(lines), compact.MAX_PAGE_HITS)
        self.assertLessEqual(sum(compact.visible_bytes(line) + 1 for line in lines),
                             compact.DEFAULT_BYTE_BUDGET)
        self.assertTrue(all(line.startswith("@semantic") for line in lines))
        self.assertTrue(all("~semantic" in line for line in lines))
        self.assertRegex(stderr.getvalue(), r"agrep --more m\.[A-Za-z0-9_-]+")

    def test_snapshot_write_failure_degrades_to_classic_rows(self) -> None:
        hit = {
            "session": "readonly-session",
            "turn": 7,
            "ts": 1,
            "who": "user",
            "agent": "codex",
            "project": "p",
            "snippet": "needle survives without continuation storage",
        }
        result = {
            "hits": [hit],
            "total": 1,
            "chats": 1,
            "tool_hits": 0,
            "engine": "corpusdb",
            "mode": "keyword",
            "totals_exact": True,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(
                    search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(
                    search, "_start_compact_page",
                    side_effect=PermissionError(13, "read-only data dir")), \
                mock.patch("explore._session_index",
                           return_value={"readonly-session": {}}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(["needle", "--color", "never"])
        self.assertEqual(rc, 0)
        self.assertIn("needle survives", stdout.getvalue())
        # classic rows ARE the answer: the fallback is not the reader's
        # situation (law 3), so its reason stays on the debug channel
        self.assertNotIn("using classic output", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_snapshot_write_failure_keeps_agent_output_compact(self) -> None:
        hits = []
        for turn in range(20):
            snippet = f"needle survives in compact row {turn}"
            hits.append({
                "session": f"readonly-session-{turn}", "turn": turn,
                "ts": turn + 1, "who": "user", "agent": "codex",
                "project": "p", "snippet": snippet,
                "content_digest": compact.content_digest(snippet),
            })
        result = {
            "hits": hits, "total": 40, "chats": 20, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword", "totals_exact": True,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(
                    search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(
                    compact, "_save_snapshot",
                    side_effect=PermissionError(1, "sandbox denied")), \
                mock.patch("explore._session_index", return_value={}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(["needle", "--color", "never"])
        lines = stdout.getvalue().splitlines()
        self.assertEqual(rc, 0)
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("@readonly") for line in lines))
        self.assertNotIn("\t", stdout.getvalue())
        self.assertIn("without a further handle", stderr.getvalue())
        self.assertNotIn("using classic output", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_snapshot_write_failure_keeps_explicit_coverage(self) -> None:
        hit = {
            "session": "readonly-session",
            "turn": 7,
            "ts": 1,
            "who": "user",
            "agent": "codex",
            "project": "p",
            "snippet": "needle survives without continuation storage",
        }
        result = {
            "hits": [hit],
            "total": 1,
            "chats": 1,
            "tool_hits": 0,
            "engine": "corpusdb",
            "mode": "keyword",
            "totals_exact": True,
        }
        coverage = search._CoverageRetry(search._COVERAGE_SCANNED)
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(
                    search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(
                    search, "_start_compact_page",
                    side_effect=PermissionError(13, "read-only data dir")), \
                mock.patch.object(
                    search, "_overspec_retry_attempt",
                    return_value=coverage), \
                mock.patch.object(search, "_emit_overspec_block") as emit, \
                mock.patch("explore._session_index",
                           return_value={"readonly-session": {}}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = search.main([
                "needle", "--coverage", "--color", "never"])
        self.assertEqual(rc, 0)
        emit.assert_called_once_with(
            "needle", mock.ANY, [hit], None, force=True)

    def test_auto_semantic_compact_rows_are_labeled_but_classic_is_unchanged(self) -> None:
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        semantic_hit = {"session": "semantic-auto", "turn": 7, "ts": 1,
                        "who": "user", "agent": "codex", "project": "p",
                        "content_digest": compact.content_digest(
                            "semantic evidence"),
                        "sem_score": 0.9, "snippet": "semantic evidence"}

        def result(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return {"hits": [dict(semantic_hit)], "total": 1, "chats": 1,
                        "engine": "semantic:hybrid", "mode": "semantic",
                        "tool_hits": 0, "fallback_recommended": False}
            return {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                    "engine": "corpusdb", "mode": "keyword"}

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(search.common, "DATA_DIR", Path(td)), \
                mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", side_effect=result), \
                mock.patch.object(search, "_stream_first_run", return_value=None), \
                mock.patch("explore._session_index", return_value={"semantic-auto": {}}):
            compact_out = TtyBuffer()
            with contextlib.redirect_stdout(compact_out), \
                    contextlib.redirect_stderr(io.StringIO()):
                compact_rc = search.main(["missing exact", "--color", "never"])
            classic_out = TtyBuffer()
            with contextlib.redirect_stdout(classic_out), \
                    contextlib.redirect_stderr(io.StringIO()):
                classic_rc = search.main([
                    "missing exact", "--classic", "--color", "never"])
        self.assertEqual((compact_rc, classic_rc), (0, 0))
        self.assertIn("~semantic", compact_out.getvalue())
        self.assertNotIn("~semantic", classic_out.getvalue())

    def test_compact_semantic_marker_distinguishes_weak_cosine(self) -> None:
        index = compact.session_prefix_index(("meaning",))
        weak = _hit(1)
        weak.update({"session": "meaning", "sem_score": 0.8399,
                     "lane": "semantic"})
        strong = {**weak, "sem_score": search._RECALL_STRONG_SEM}
        self.assertIn("~semantic-weak", search._compact_line(weak, index))
        self.assertIn("~semantic", search._compact_line(strong, index))
        self.assertNotIn("~semantic-weak", search._compact_line(strong, index))
        # M11: a strong score over a query that anchors nowhere is still weak
        unanchored = {**strong, "_sem_unanchored": True}
        self.assertIn("~semantic-weak", search._compact_line(unanchored, index))

    def test_json_leads_with_page_envelope_when_capped(self) -> None:
        hits = [{"session": f"json{i}", "turn": i, "ts": 1,
                 "who": "user", "agent": "codex", "project": "p",
                 "score": 1.0, "snippet": "hit"} for i in range(2)]
        hits[1].update({"who": "tool", "kind": "tool", "event_kind": "tool"})
        result = {"hits": hits, "total": 99, "chats": 99,
                  "engine": "corpusdb", "mode": "keyword", "totals_exact": False}
        with mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(search.main(["hit", "--json"]), 0)
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        meta, *rows = records
        self.assertEqual(meta["kind"], "agrep-meta")
        self.assertEqual(sum(row.get("kind") == "agrep-meta" for row in records), 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["kind"], "tool")
        required_fields = {"completeness", "freshness", "filter_coverage",
                           "self_exclusion", "semantic_coverage", "engine", "query"}
        page_fields = required_fields | {"semantic", "semantic_integrity",
                                         "tools_excluded"}
        self.assertTrue(required_fields <= meta.keys())
        self.assertTrue(all(page_fields.isdisjoint(row) for row in rows))

    def test_semantic_count_rejected_and_strict_policy_is_explained(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            search.main(["meaning", "-s", "-c"])
        self.assertEqual(raised.exception.code, 2)

        result = {"hits": [], "total": 0, "chats": 0,
                  "engine": "semantic:hybrid", "mode": "semantic",
                  "fallback_recommended": True,
                  "semantic_status": {"state": "query-rejected",
                                      "reason": "identifier-query"}}
        with mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result):
            human_err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(human_err):
                self.assertEqual(search.main(
                    ["REPLY_CAP", "-s", "--strict-semantic"]), 2)
            json_out = io.StringIO()
            with contextlib.redirect_stdout(json_out), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(search.main(
                    ["REPLY_CAP", "-s", "--strict-semantic", "--json"]), 2)
        self.assertIn("identifier-query", human_err.getvalue())
        self.assertEqual(json.loads(json_out.getvalue())["engine"], "semantic:policy")

    def test_family_diversity_follows_ranked_top_forty(self) -> None:
        hits = [_hit(0, family="same"), _hit(1, family="same"),
                _hit(2, family="other")] + [_hit(i) for i in range(3, 45)]
        selected = compact.diversify_hits(hits)
        self.assertEqual(len(selected), 40)
        self.assertEqual([hit["turn"] for hit in selected[:3]], [0, 2, 3])
        self.assertEqual(selected[-1]["turn"], 1)

    def test_family_diversity_never_promotes_all_terms_over_phrase(self) -> None:
        hits = [_hit(0, family="same"), _hit(1, family="same"),
                _hit(2, family="other", matched="all-terms")]
        selected = compact.diversify_hits(hits)
        self.assertEqual([hit["turn"] for hit in selected], [0, 1, 2])

    def test_semantic_rescue_leads_interior_and_all_term_noise(self) -> None:
        interior = {**_hit(0), "_boundary_class": "interior"}
        all_terms = _hit(1, matched="all-terms")
        meaning = {**_hit(2), "lane": "semantic", "sem_score": 0.9}
        selected = compact.diversify_hits([interior, all_terms, meaning])
        self.assertEqual([hit["turn"] for hit in selected], [2, 0, 1])

    def test_strong_phrase_leaders_stay_ahead_of_semantic_rescue(self) -> None:
        aligned = [{**_hit(index), "_boundary_class": "aligned"}
                   for index in range(4)]
        meaning = {**_hit(9), "lane": "semantic", "sem_score": 0.9}
        selected = compact.diversify_hits([*aligned, meaning])
        self.assertEqual(
            [hit["turn"] for hit in selected], [0, 1, 2, 9, 3])

    def test_distinct_tool_events_survive_a_same_turn_digest_collision(
            self) -> None:
        texts = (
            "exec_command: probe --slot 158\nunique outcome number 158",
            "exec_command: probe --slot 233\nunique outcome number 233",
        )
        self.assertEqual(
            [compact.content_digest(text) for text in texts], ["030a", "030a"])
        hits = []
        for index, text in enumerate(texts):
            hit = _tool_hit(index, session="collision-session", turn=7)
            hit.update({
                "ts": index,
                "snippet": text,
                "output": text.split("\n", 1)[1],
                "content_digest": compact.content_digest(text),
                "_event_identity": search.common.tool_event_identity(
                    "collision-session", 7, index, text),
            })
            hits.append(hit)
        records = compact.freeze_records(hits, lambda hit: hit["snippet"])
        indices, _reason = compact._compose_page(
            records, compact.DEFAULT_BYTE_BUDGET, requested_rows=8)
        self.assertEqual(indices, [0, 1])
        self.assertNotEqual(
            hits[0]["_event_identity"], hits[1]["_event_identity"])
        self.assertEqual(
            search.common.tool_event_identity(
                "collision-session", 7, 0, texts[0]),
            search.common.tool_event_identity(
                "collision-session", 99, 0, texts[0]),
        )

    def test_tool_echo_requires_a_proven_successful_complete_outcome(
            self) -> None:
        output = "identical lived result"
        prose = [{
            "session": "echo-family", "turn": 4, "who": "user",
            "snippet": output, "_snippet_complete": True,
        }]
        base = {
            "session": "echo-family", "turn": 4, "who": "tool",
            "output": output, "output_chars": len(output),
            "output_truncated": False,
        }
        roots = {"echo-family": "echo-family"}
        for value in (None, False, "true"):
            with self.subTest(ok=value):
                self.assertFalse(search._compact_tool_echo(
                    {**base, "ok": value}, prose, roots))
        self.assertFalse(search._compact_tool_echo(base, prose, roots))
        self.assertFalse(search._compact_tool_echo(
            {key: value for key, value in {**base, "ok": True}.items()
             if key != "output_chars"}, prose, roots))
        self.assertFalse(search._compact_tool_echo(
            {**base, "ok": True, "output_truncated": True}, prose, roots))
        self.assertFalse(search._compact_tool_echo(
            {**base, "ok": True},
            [{**prose[0], "_snippet_complete": False}], roots))
        self.assertTrue(search._compact_tool_echo(
            {**base, "ok": True}, prose, roots))

    def test_tool_only_explicit_requested_rows_are_authoritative(self) -> None:
        query = "EXPLICIT_TOOL_ROWS"
        hits = [_tool_hit(index, query=query) for index in range(12)]
        sessions = tuple({hit["session"] for hit in hits})
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value={s: s for s in sessions}), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sessions), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}):
            page = search._start_compact_page(
                hits, query, search._match_pat(query, "keyword"),
                corpus_more=False, requested_rows=8)
        self.assertEqual(len(page.records), 8)
        self.assertEqual(
            [record["hit"]["_event_identity"] for record in page.records],
            [hit["_event_identity"] for hit in hits[:8]],
        )

    def test_default_tool_rescue_freezes_every_omitted_ranked_row(self) -> None:
        query = "FROZEN_TOOL_RESCUE"
        hits = [_tool_hit(index, query=query) for index in range(10)]
        sessions = tuple({hit["session"] for hit in hits})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(search.common, "indexed_family_roots",
                                      return_value={s: s for s in sessions}), \
                    mock.patch.object(
                        search.common, "indexed_session_prefix_candidates",
                        return_value=sessions), \
                    mock.patch.object(search.common, "transcript_generation",
                                      return_value={"generation": 1}):
                page = search._start_compact_page(
                    hits, query, search._match_pat(query, "keyword"),
                    corpus_more=False,
                    deeper_argv=("agrep", "--classic", "-n", "80", "--", query))
            self.assertEqual(len(page.records), 4)
            self.assertTrue(page.more)
            self.assertIsNotNone(page.handle)
            frozen = compact.load_snapshot(
                page.handle, None, search._RANKING_VERSION, data_dir=root)
            self.assertEqual(
                [record["hit"]["_event_identity"] for record in frozen],
                [hit["_event_identity"] for hit in hits[4:]],
            )
            continuation = compact.continue_compact(
                page.handle, None, search._RANKING_VERSION, data_dir=root)
            self.assertGreater(len(continuation.records), 0)

    def test_mixed_tool_rescue_caps_at_three_and_freezes_every_other_tool(
            self) -> None:
        query = "MIXED_FROZEN_TOOL_RESCUE"
        session = "mixed-tool-session"
        prose_text = f"{query} lived explanation"
        prose = {
            "session": session, "turn": 1, "ts": 1001,
            "who": "user", "agent": "codex", "project": "p",
            "score": 1.0, "snippet": prose_text,
            "content_digest": compact.content_digest(prose_text),
            "_snippet_complete": True,
        }
        tools = [_tool_hit(index, session=session, turn=1, query=query)
                 for index in range(10)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(search.common, "indexed_family_roots",
                                      return_value={session: session}), \
                    mock.patch.object(
                        search.common, "indexed_session_prefix_candidates",
                        return_value=(session,)), \
                    mock.patch.object(search.common, "transcript_generation",
                                      return_value={"generation": 1}):
                page = search._start_compact_page(
                    [prose, *tools], query,
                    search._match_pat(query, "keyword"), corpus_more=False,
                    deeper_argv=("agrep", "--classic", "-n", "80", "--", query))
            self.assertEqual(
                [record["hit"]["who"] for record in page.records],
                ["user", "tool", "tool"],
            )
            self.assertTrue(page.more)
            self.assertIsNotNone(page.handle)
            frozen = compact.load_snapshot(
                page.handle, None, search._RANKING_VERSION, data_dir=root)
            self.assertEqual(
                [record["hit"]["_event_identity"] for record in frozen],
                [hit["_event_identity"] for hit in tools[2:]],
            )

    def test_proven_echo_tools_defer_without_disappearing(self) -> None:
        query = "MIXED_TOOL_ECHO"
        session = "mixed-echo-session"
        prose_text = f"{query} lived explanation"
        prose = {
            "session": session, "turn": 1, "ts": 1001,
            "who": "user", "agent": "codex", "project": "p",
            "score": 1.0, "snippet": prose_text,
            "content_digest": compact.content_digest(prose_text),
            "_snippet_complete": True,
        }
        tools = [_tool_hit(index, session=session, turn=1, query=query)
                 for index in range(6)]
        for tool in tools:
            tool["output"] = prose_text
            tool["output_chars"] = len(prose_text)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(search.common, "indexed_family_roots",
                                      return_value={session: session}), \
                    mock.patch.object(
                        search.common, "indexed_session_prefix_candidates",
                        return_value=(session,)), \
                    mock.patch.object(search.common, "transcript_generation",
                                      return_value={"generation": 1}):
                page = search._start_compact_page(
                    [prose, *tools], query,
                    search._match_pat(query, "keyword"), corpus_more=False,
                    deeper_argv=("agrep", "--classic", "-n", "80", "--", query))
            self.assertEqual(
                [record["hit"]["who"] for record in page.records], ["user"])
            self.assertIsNotNone(page.handle)
            frozen = compact.load_snapshot(
                page.handle, None, search._RANKING_VERSION, data_dir=root)
            self.assertEqual(len(frozen), len(tools))

    def test_large_private_source_bodies_never_enter_frozen_state(self) -> None:
        query = "PRIVATE_BODY_BOUNDARY"
        sentinel = "PRIVATE_SOURCE_BODY_MUST_NOT_BE_SNAPSHOTTED"
        hits = []
        for index in range(10):
            hit = _tool_hit(index, query=query)
            hit["_search_text"] = sentinel + str(index) + ("x" * 90_000)
            hits.append(hit)
        sessions = tuple({hit["session"] for hit in hits})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(search.common, "indexed_family_roots",
                                      return_value={s: s for s in sessions}), \
                    mock.patch.object(
                        search.common, "indexed_session_prefix_candidates",
                        return_value=sessions), \
                    mock.patch.object(search.common, "transcript_generation",
                                      return_value={"generation": 1}):
                page = search._start_compact_page(
                    hits, query, search._match_pat(query, "keyword"),
                    corpus_more=False,
                    deeper_argv=("agrep", "--classic", "-n", "80", "--", query))
            self.assertTrue(page.more)
            self.assertTrue(all("_search_text" not in record["hit"]
                                for record in page.records))
            frozen = compact.load_snapshot(
                page.handle, None, search._RANKING_VERSION, data_dir=root)
            self.assertTrue(all("_search_text" not in record["hit"]
                                for record in frozen))
            snapshots = list((root / ".compact-snapshots").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            self.assertNotIn(sentinel, snapshots[0].read_text(encoding="utf-8"))

    def test_all_terms_match_span_is_one_real_anchor_not_a_union(self) -> None:
        text = "alpha " + ("middle " * 60) + "omega"
        spans = [(0, len("alpha")),
                 (text.index("omega"), text.index("omega") + len("omega"))]
        row = (
            "anchor-session", "codex", "p", "", "", "unknown",
            3, 7, "user", text, compact.content_digest(text),
        )
        hit = corpusdb._spans_hit(row, spans)
        self.assertIn(hit["_match_span"], spans)
        self.assertNotEqual(hit["_match_span"], (spans[0][0], spans[-1][1]))
        self.assertNotIn("_search_text", hit)

        phrase = "alpha---omega"
        combined = text + " " + phrase
        phrase_row = (*row[:9], combined, compact.content_digest(combined))
        with mock.patch.object(corpusdb, "_candidates",
                               return_value=[phrase_row]):
            both = corpusdb.keyword_terms(None, "alpha omega", 40)
        phrase_hit = both["phrase"]["hits"][0]
        self.assertEqual(
            phrase_hit["_match_span"],
            (combined.index(phrase), combined.index(phrase) + len(phrase)),
        )

    def test_page_keeps_four_then_stops_at_phrase_lane_drop(self) -> None:
        hits = [_hit(i) for i in range(4)]
        hits.extend(_hit(i, matched="all-terms") for i in range(4, 9))
        records = compact.freeze_records(hits, lambda hit: hit["text"])
        chosen, reason = compact.select_page(records)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(reason, "lane-drop")

    def test_page_uses_visible_bytes_and_minimum_evidence(self) -> None:
        hits = [_hit(i) for i in range(8)]
        records = compact.freeze_records(hits, lambda hit: "\x1b[31m" + "x" * 40 + "\x1b[0m")
        chosen, reason = compact.select_page(records, byte_budget=80)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(reason, "byte-budget")
        self.assertEqual(compact.visible_bytes(records[0]["line"]), 40)

    def test_thirty_term_stitched_minimum_stays_inside_page_budget(self) -> None:
        terms = [f"term{index:02d}" for index in range(30)]
        snippet = ("x" * 180).join(terms)
        hits = [{**_hit(index, matched="all-terms"),
                 "agent": "codex", "who": "user", "project": "p",
                 "ts": 0, "snippet": snippet}
                for index in range(compact.MIN_PAGE_HITS)]
        with mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch("explore._session_index",
                           return_value={hit["session"]: {} for hit in hits}):
            page = search._start_compact_page(
                hits, " ".join(terms), search._match_pat(" ".join(terms), "keyword"),
                corpus_more=False)
        rendered = sum(compact.visible_bytes(line) + 1 for line in page.lines)
        self.assertEqual(len(page.lines), compact.MIN_PAGE_HITS)
        self.assertLessEqual(rendered, compact.DEFAULT_BYTE_BUDGET)
        self.assertTrue(all(line.startswith("@") and line.endswith("…")
                            for line in page.lines))

    def test_generation_proof_failure_does_not_hide_frozen_results(self) -> None:
        def hit(index: int) -> dict:
            return {
                **_hit(index),
                "agent": "codex", "who": "user", "project": "p",
                "ts": 0, "snippet": "alpha",
            }

        generation = mock.Mock(
            side_effect=RuntimeError("session family is not generation-bound"))
        stderr = io.StringIO()
        with mock.patch.object(search.common, "transcript_generation", generation), \
                contextlib.redirect_stderr(stderr):
            page = search._start_compact_page(
                [hit(0), hit(1)], "alpha", search._match_pat("alpha", "keyword"),
                corpus_more=False)
            self.assertFalse(page.more)
            generation.assert_not_called()
            page = search._start_compact_page(
                [hit(index) for index in range(compact.MAX_PAGE_HITS + 1)],
                "alpha", search._match_pat("alpha", "keyword"),
                corpus_more=False)
            self.assertTrue(page.more)
            self.assertIsNotNone(page.handle)
        generation.assert_called_once()
        # her rows are complete; the token only a later page would check is
        # agrep's bookkeeping, so the surface stays silent about it
        self.assertEqual(stderr.getvalue(), "")

    def test_page_score_drop_and_interior_minimum(self) -> None:
        hits = [_hit(i, score=1.0 - i * 0.1) for i in range(4)]
        hits.extend(_hit(i, score=0.2) for i in range(4, 9))
        records = compact.freeze_records(hits, lambda hit: hit["text"])
        chosen, reason = compact.select_page(records)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(reason, "score-drop")

    def test_semantic_cutoff_uses_semantic_score(self) -> None:
        hits = [_hit(i, score=0.01) for i in range(4)]
        hits.extend(_hit(i, score=1.0) for i in range(4, 8))
        for index, hit in enumerate(hits):
            hit["sem_score"] = 1.0 if index < 4 else 0.2
        records = compact.freeze_records(hits, lambda hit: hit["text"])
        chosen, reason = compact.select_page(records)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(reason, "score-drop")

    def test_page_defers_family_overflow_and_backfills_lower_lane(self) -> None:
        hits = [_hit(i, family="loud") for i in range(6)]
        hits.append(_hit(6, family="beta"))
        hits.append(_hit(7, family="cross", matched="all-terms"))
        records = compact.freeze_records(hits, lambda hit: hit["text"])
        chosen, reason = compact.select_page(records)
        self.assertEqual([record["hit"]["turn"] for record in chosen], [0, 1, 2, 6, 7])
        self.assertEqual(reason, "diversity")

    def test_page_collapses_same_session_same_turn_to_one_row(self) -> None:
        dupe = {"session": "echo-s", "turn": 7, "score": 1.0, "family": "fam-echo"}
        hits = [dict(dupe, text="first echo"), dict(dupe, text="second echo"),
                _hit(2), _hit(3), _hit(4)]
        records = compact.freeze_records(hits, lambda hit: hit["text"])
        chosen, reason = compact.select_page(records)
        self.assertEqual([record["line"] for record in chosen],
                         ["first echo", "result 2", "result 3", "result 4"])
        self.assertEqual(reason, "diversity")

    def test_echo_pileup_caps_family_and_surfaces_cross_family_target(self) -> None:
        hits = [{"session": "echo-a", "turn": 0, "score": 1.0, "family": "fam-echo",
                 "text": f"echo-a t0 copy{copy}"} for copy in range(8)]
        hits.extend({"session": "echo-b", "turn": turn, "score": 1.0,
                     "family": "fam-echo", "text": f"echo-b t{turn}"}
                    for turn in range(1, 16))
        hits.append({"session": "beta-s", "turn": 0, "score": 1.0,
                     "family": "fam-beta", "text": "beta hit"})
        hits.append({"session": "gamma-s", "turn": 0, "score": 1.0,
                     "family": "fam-gamma", "text": "gamma hit"})
        hits.append({"session": "target-s", "turn": 3, "score": 1.0,
                     "family": "fam-target", "matched": "all-terms",
                     "text": "scattered target"})

        def walk(root: Path) -> list:
            page = compact.start_compact(
                hits, lambda hit: hit["text"], "generation", "rank-v1",
                data_dir=root, byte_budget=10_000, query="target")
            pages = [page]
            while page.more:
                page = compact.continue_compact(page.handle, None, "rank-v1",
                                                data_dir=root, byte_budget=10_000)
                pages.append(page)
            return pages

        with tempfile.TemporaryDirectory() as temp:
            pages = walk(Path(temp))
        for page in pages:
            echoes = [line for line in page.lines if line.startswith("echo-")]
            self.assertLessEqual(len(echoes), compact.FAMILY_PAGE_CAP)
            same_turn = [line for line in page.lines if line.startswith("echo-a t0")]
            self.assertLessEqual(len(same_turn), 1)
        first_two = pages[0].lines + (pages[1].lines if len(pages) > 1 else [])
        self.assertIn("scattered target", first_two)
        self.assertEqual(pages[0].lines[-1], "scattered target")
        self.assertEqual(pages[0].stopped_by, "diversity")
        shown = [line for page in pages for line in page.lines]
        self.assertEqual(sorted(shown), sorted(hit["text"] for hit in hits))
        with tempfile.TemporaryDirectory() as temp:
            again = walk(Path(temp))
        self.assertEqual([page.lines for page in again],
                         [page.lines for page in pages])

    def test_multi_family_echo_wall_still_reserves_terms_slots(self) -> None:
        hits = []
        for fam in range(6):
            hits.extend({"session": f"echo{fam}-s", "turn": row, "score": 1.0,
                         "family": f"fam-echo{fam}", "text": f"echo-{fam} r{row}"}
                        for row in range(3))
        hits.append({"session": "target-s", "turn": 3, "score": 1.0,
                     "family": "fam-target", "matched": "all-terms",
                     "text": "scattered target"})

        def walk(root: Path) -> list:
            page = compact.start_compact(
                hits, lambda hit: hit["text"], "generation", "rank-v1",
                data_dir=root, byte_budget=10_000, query="target")
            pages = [page]
            while page.more:
                page = compact.continue_compact(page.handle, None, "rank-v1",
                                                data_dir=root, byte_budget=10_000)
                pages.append(page)
            return pages

        with tempfile.TemporaryDirectory() as temp:
            pages = walk(Path(temp))
        first = pages[0]
        self.assertEqual(len(first.lines), compact.MAX_PAGE_HITS)
        self.assertEqual(first.lines[-1], "scattered target")
        self.assertEqual(first.records[-1]["hit"]["matched"], "all-terms")
        self.assertTrue(all(line.startswith("echo-") for line in first.lines[:-1]))
        self.assertTrue(first.more)
        self.assertIn("echo-4 r2", pages[1].lines)
        shown = [line for page in pages for line in page.lines]
        self.assertEqual(sorted(shown), sorted(hit["text"] for hit in hits))
        with tempfile.TemporaryDirectory() as temp:
            again = walk(Path(temp))
        self.assertEqual([page.lines for page in again],
                         [page.lines for page in pages])

    def test_explicit_page_bypasses_adaptive_limits(self) -> None:
        hits = [_hit(i) for i in range(4)]
        hits.extend(_hit(i, matched="all-terms", score=0.1) for i in range(4, 20))
        page = compact.fixed_compact(hits[:8], lambda hit: hit["text"])
        self.assertEqual(len(page.lines), 8)
        self.assertFalse(page.more)
        self.assertEqual(page.stopped_by, "explicit-limit")

    def test_snapshot_is_private_pinned_and_frozen(self) -> None:
        generation = {"files": {"messages.jsonl": {"size": 10}}, "version": 2}
        hits = [_hit(i) for i in range(20)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = compact.start_compact(
                hits, lambda hit: hit["text"], generation, "boundary-v1",
                data_dir=root, now=1000, byte_budget=24, corpus_more=True,
                query="result", exact_total=25,
                deeper_argv=("agrep", "--classic", "-n", "80", "--", "result"))
            self.assertEqual(len(first.records), 4)
            self.assertTrue(first.more)
            self.assertIsNotNone(first.handle)
            token = first.handle.split(".", 1)[1]
            path = root / ".compact-snapshots" / f"{token}.json"
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            hits[4]["text"] = "mutated"
            second = compact.continue_compact(
                first.handle, generation, "boundary-v1", data_dir=root,
                now=1001, byte_budget=10000)
            self.assertEqual(second.lines[0], "result 4")
            self.assertTrue(second.corpus_more)
            echoed = compact.continue_compact(
                first.handle, None, "boundary-v1", data_dir=root,
                now=1001, byte_budget=10000)
            self.assertEqual(echoed.lines[0], "result 4")
            with self.assertRaises(compact.SnapshotExpired):
                compact.load_snapshot(first.handle, {"different": True}, "boundary-v1",
                                      data_dir=root, now=1001)

    def test_snapshot_metadata_survives_every_continuation_page(self) -> None:
        query = "needle & literal"
        deeper = (
            "agrep", "-w", "--agent=codex", "--classic", "-n", "80",
            "--", query,
        )
        hits = [_hit(index) for index in range(24)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query=query, exact_total=137, deeper_argv=deeper)
            pages = [page]
            while page.more:
                token = page.handle.split(".", 1)[1]
                payload = json.loads(
                    (root / ".compact-snapshots" / f"{token}.json")
                    .read_text(encoding="utf-8"))
                self.assertEqual(payload["version"], compact.SNAPSHOT_VERSION)
                self.assertEqual(payload["query"], query)
                self.assertEqual(payload["exact_total"], 137)
                self.assertEqual(payload["deeper_argv"], list(deeper))
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
                pages.append(page)
            self.assertGreater(len(pages), 1)
            for frozen in pages:
                self.assertEqual(frozen.query, query)
                self.assertEqual(frozen.exact_total, 137)
                self.assertEqual(frozen.deeper_argv, deeper)
                self.assertTrue(frozen.corpus_more)

    def test_deeper_vocabulary_covers_every_search_filter_option(self) -> None:
        # every filter search can serialize must validate on replay -
        # --exclude-project once fell to classic output for lack of an entry
        query = "needle"
        deeper = (
            "agrep", "--agent=codex", "--project=web",
            "--exclude-project=bench", "--model=gpt-5", "--who=user,agent",
            "--chat=abc", "--since=7d", "--until=1d", "--soft", "--no-meta",
            "--sort=time", "--classic", "-n", "80", "--", query,
        )
        parsed = compact._validate_snapshot_metadata(
            query, None, deeper, True, False, 0)
        self.assertEqual(parsed[2], deeper)
        exclusion = (
            "agrep", "--no-who=subagent", "--classic", "-n", "80",
            "--", query,
        )
        self.assertEqual(
            compact._validate_snapshot_metadata(
                query, None, exclusion, True, False, 0)[2],
            exclusion)

    def test_snapshot_rejects_contradictory_total_metadata(self) -> None:
        records = compact.freeze_records(
            [_hit(1)], lambda hit: hit["text"])
        deeper = (
            "agrep", "--lexical", "--classic", "-n", "80", "--", "result",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = (
                {"exact_total": 0},
                {"exact_total": 1, "more_unknown": True},
                {
                    "exact_total": 1, "corpus_more": True,
                    "deeper_argv": deeper,
                },
            )
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                        compact.CompactError,
                        "contradictory totals|exact and unknown totals"):
                    compact.save_snapshot(
                        records, "g", "v1", data_dir=root,
                        query="result", **kwargs)

            # More matches than served rows is the fold, not a contradiction:
            # near-duplicates collapse into one row that discloses the
            # collapse, while the footer still reports every match counted.
            compact.save_snapshot(
                records, "g", "v1", data_dir=root,
                query="result", exact_total=2)

            handle = compact.save_snapshot(
                records, "g", "v1", data_dir=root,
                query="result", exact_total=1)
            path = compact._snapshot_path(handle, root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["exact_total"] = 0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                    compact.CompactError, "contradictory totals"):
                compact.load_snapshot(
                    handle, None, "v1", data_dir=root)

            for invalid_covered in (-1, "40", True):
                with self.subTest(deeper_covered=invalid_covered):
                    handle = compact.save_snapshot(
                        records, "g", "v1", data_dir=root, query="result")
                    path = compact._snapshot_path(handle, root)
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["deeper_covered"] = invalid_covered
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                            compact.CompactError, "invalid covered count"):
                        compact.load_snapshot(
                            handle, None, "v1", data_dir=root)

    def test_final_continuation_states_exact_total_and_deeper_command(self) -> None:
        query = "needle & literal"
        deeper = (
            "agrep", "--lexical", "--agent=codex", "--classic",
            "-n", "80", "--", query,
        )
        hits = [_hit(index) for index in range(20)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query=query, exact_total=137, deeper_argv=deeper)
            while page.more:
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                search._compact_summary(page)
            summary = stderr.getvalue()
            self.assertRegex(
                summary,
                r"^137 matches · broader rerun \(may repeat\): agrep --deeper "
                r"m\.[A-Za-z0-9_-]{8}\n$")
            self.assertNotIn(query, summary)
            self.assertEqual(
                compact.load_deeper_argv(
                    page.handle, "rank-v1", data_dir=root, now=11),
                deeper)

    def test_measured_floor_is_stated_on_every_incomplete_page(self) -> None:
        hits = [_hit(index) for index in range(20)]
        deeper = ("agrep", "--classic", "-n", "80", "--", "result")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query="result", deeper_argv=deeper, total_floor=195998)
            seen = []
            while True:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    search._compact_summary(page)
                seen.append(stderr.getvalue())
                if not page.more:
                    break
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
            self.assertGreater(len(seen), 1)
            for summary in seen:
                self.assertRegex(
                    summary,
                    r"^195,998\+ matches \(floor; -c exact\) · "
                    r"(?:more: agrep --more|broader rerun \(may repeat\): "
                    r"agrep --deeper) m\.[A-Za-z0-9_-]{8}\n$")

    def test_a_meaning_lane_floor_does_not_offer_keyword_count(self) -> None:
        hits = [_hit(index) for index in range(20)]
        deeper = ("agrep", "-s", "--classic", "-n", "80", "--", "result")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query="result", deeper_argv=deeper, total_floor=400)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                search._compact_summary(page)
            summary = stderr.getvalue()
            self.assertRegex(
                summary,
                r"^400\+ matches \(floor\) · more: agrep --more "
                r"m\.[A-Za-z0-9_-]{8}\n$")
            self.assertNotIn("-c exact", summary)

    def test_a_floor_never_rides_beside_an_exact_total(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(compact.CompactError):
                compact.save_snapshot(
                    records, {"sig": "a"}, "v1", data_dir=root, now=10,
                    query="result", exact_total=1, corpus_more=True,
                    deeper_argv=("agrep", "--classic", "-n", "80", "--",
                                 "result"),
                    total_floor=9)

    def test_unknown_total_survives_to_the_final_page(self) -> None:
        hits = [_hit(index) for index in range(24)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, query="result",
                more_unknown=True, total_uncounted=True)
            lower_bounds = [page.known_min]
            while page.more:
                token = page.handle.split(".", 1)[1]
                payload = json.loads(
                    (root / ".compact-snapshots" / f"{token}.json")
                    .read_text(encoding="utf-8"))
                self.assertIs(payload["more_unknown"], True)
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
                lower_bounds.append(page.known_min)
            self.assertTrue(page.more_unknown)
            self.assertEqual(lower_bounds, sorted(lower_bounds))
            self.assertEqual(page.known_min, len(hits))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                search._compact_summary(page)
            self.assertEqual(
                stderr.getvalue(), f"{len(hits)} shown · total unknown\n")

    def test_bounded_lower_bound_keeps_a_copyable_deeper_handle(self) -> None:
        hits = [_hit(index) for index in range(24)]
        deeper = (
            "agrep", "--lexical", "--classic", "-n", "80", "--", "result",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query="result", deeper_argv=deeper)
            lower_bounds = [page.known_min]
            while page.more:
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
                lower_bounds.append(page.known_min)
            self.assertEqual(lower_bounds, sorted(lower_bounds))
            self.assertEqual(page.known_min, len(hits) + 1)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                search._compact_summary(page)
            self.assertRegex(
                stderr.getvalue(),
                rf"^{len(hits) + 1}\+ matches \(floor; -c exact\) · "
                r"broader rerun \(may repeat\): agrep --deeper "
                r"m\.[A-Za-z0-9_-]{8}\n$")

    def test_exhausted_exact_page_has_no_footer(self) -> None:
        page = compact.CompactPage(
            ({"hit": {}, "line": "one"},), False, None, "exhausted", 1,
            exact_total=1)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            search._compact_summary(page)
        self.assertEqual(stderr.getvalue(), "")

    def test_snapshot_version_mismatch_uses_the_rerun_path_first(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for version in (2, compact.SNAPSHOT_VERSION + 1):
                with self.subTest(version=version):
                    handle = compact.save_snapshot(
                        records, "g", "v1", data_dir=root, now=10,
                        query="result")
                    path = compact._snapshot_path(handle, root)
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload = {"version": version, "records": payload["records"]}
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                            compact.SnapshotExpired,
                            "continuation format changed; rerun the search"):
                        compact.load_snapshot(
                            handle, None, "v1", data_dir=root, now=11)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with mock.patch.object(search.common, "DATA_DIR", root), \
                            mock.patch.object(
                                search.common, "transcript_generation",
                                return_value={"sig": "current"}), \
                            contextlib.redirect_stdout(stdout), \
                            contextlib.redirect_stderr(stderr):
                        rc = search.main(["--more", handle])
                    self.assertEqual(rc, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(
                        "continuation format changed; rerun the search",
                        stderr.getvalue())

    def test_snapshot_rejects_unactionable_deeper_metadata(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                    compact.CompactError, "lacks a deeper command"):
                compact.save_snapshot(
                    records, "g", "v1", data_dir=root, query="result",
                    corpus_more=True)
            for argv in (
                    ("agrep", "--classic", "result"),
                    ("agrep", "--classic", "--", "other"),
                    ("other", "--", "result")):
                with self.subTest(argv=argv), self.assertRaisesRegex(
                        compact.CompactError, "invalid deeper command"):
                    compact.save_snapshot(
                        records, "g", "v1", data_dir=root, query="result",
                        corpus_more=True, deeper_argv=argv)

    def test_deeper_handle_is_opaque_for_shell_sensitive_code_patterns(self) -> None:
        query = '%PATH% "$env:PATH" !bang'
        deeper = (
            "agrep", "--lexical", "--project=code$repo", "--classic",
            "-n", "80", "--", query,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                [_hit(1)], lambda hit: hit["text"], "g", "v1",
                data_dir=root, corpus_more=True, query=query,
                deeper_argv=deeper)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                search._compact_summary(page)
            summary = stderr.getvalue()
            self.assertRegex(
                summary,
                r"^2\+ matches \(floor; -c exact\) · "
                r"broader rerun \(may repeat\): "
                r"agrep --deeper m\.[A-Za-z0-9_-]{8}\n$")
            self.assertLessEqual(len(summary.encode("utf-8")), 128)
            self.assertNotIn(query, summary)
            self.assertEqual(
                compact.load_deeper_argv(
                    page.handle, "v1", data_dir=root),
                deeper)

    def test_deeper_rejects_tampering_expiry_and_other_options(self) -> None:
        records = compact.freeze_records([], lambda hit: hit["text"])
        deeper = (
            "agrep", "--lexical", "--classic", "-n", "80", "--", "result",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(
                records, "g", search._RANKING_VERSION, data_dir=root,
                corpus_more=True, query="result", deeper_argv=deeper)
            path = compact._snapshot_path(handle, root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["deeper_argv"] = [
                "agrep", "--deeper=m.attacker", "--classic", "-n", "80",
                "--", "result",
            ]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(search.main(["--deeper", handle]), 2)
            self.assertIn("invalid deeper command", stderr.getvalue())

            expired = compact.save_snapshot(
                records, "g", search._RANKING_VERSION, data_dir=root,
                now=time.time() - 10, ttl_s=1, corpus_more=True,
                query="result", deeper_argv=deeper)
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(search.main(["--deeper", expired]), 2)
            self.assertIn("continuation expired", stderr.getvalue())

            with contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                search.main(["--deeper", expired, "--json"])
            self.assertEqual(raised.exception.code, 2)

    def test_empty_frozen_continuation_points_to_deeper_without_looping(self) -> None:
        deeper = (
            "agrep", "--classic", "-n", "80", "--", "needle",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(
                [], {"generation": 1}, search._RANKING_VERSION,
                data_dir=root, query="needle", corpus_more=True,
                deeper_argv=deeper)
            snapshots = root / ".compact-snapshots"
            before = sorted(snapshots.iterdir())
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 1}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["--more", handle])
            self.assertEqual(rc, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                f"no frozen rows remain; use: agrep --deeper {handle}\n")
            self.assertEqual(sorted(snapshots.iterdir()), before)

    def test_a_reread_expiry_keeps_its_cause_and_an_absence_keeps_the_lever(self) -> None:
        # F2: the first read unlinked the snapshot, so the second read of the
        # same handle degraded to a lever-less "handle not found"
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(records, "g", "v1", ttl_s=2,
                                           data_dir=root, now=10,
                                           query="result")
            for attempt in range(2):
                with self.subTest(attempt=attempt), \
                        self.assertRaises(compact.SnapshotExpired) as raised:
                    compact.load_snapshot(handle, "g", "v1", data_dir=root,
                                          now=13)
                self.assertIn("continuation expired", str(raised.exception))
                self.assertIn("rerun the search", str(raised.exception))
            compact._snapshot_path(handle, root).unlink()
            with self.assertRaises(compact.CompactError) as raised:
                compact.load_snapshot(handle, "g", "v1", data_dir=root, now=13)
            self.assertIn("not found", str(raised.exception))
            self.assertIn("rerun the search", str(raised.exception))

    def test_snapshot_expiry_version_and_path_validation(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(records, "g", "v1", ttl_s=2,
                                           data_dir=root, now=10, query="result")
            with self.assertRaises(compact.SnapshotExpired):
                compact.load_snapshot(handle, "g", "v1", data_dir=root, now=13)
            with self.assertRaises(compact.CompactError):
                compact.load_snapshot("m.../../escape", "g", "v1", data_dir=root)
            handle = compact.save_snapshot(
                records, "g", "v1", data_dir=root, now=20, query="result")
            with self.assertRaises(compact.SnapshotExpired):
                compact.load_snapshot(handle, "g", "v2", data_dir=root, now=21)

    def test_result_handles_resolve_or_reject_ambiguity(self) -> None:
        sessions = ["abcdef011111", "abcdef012345"]
        value = compact.encode_result_handle(
            {"session": "abcdef012345", "turn": 42},
            session_index=compact.session_prefix_index(sessions))
        self.assertEqual(value, "@abcdef012:42")
        self.assertEqual(compact.parse_result_handle(value), ("abcdef012", 42))
        resolved = compact.resolve_result_handle(value, lambda prefix: ["abcdef012345"])
        self.assertEqual(resolved, ("abcdef012345", 42))
        with self.assertRaises(compact.CompactError):
            compact.resolve_result_handle(
                value, lambda prefix: ["abcdef0123", "abcdef0124"])

        opaque = compact.encode_result_handle({"session": "会话 id", "turn": 7})
        self.assertEqual(compact.parse_result_handle(opaque), ("会话 id", 7))
        with self.assertRaises(compact.CompactError):
            compact.encode_result_handle({"session": "会话 id", "turn": None})
        with self.assertRaises(compact.CompactError):
            compact.parse_result_handle("@decorator")

    def test_session_arguments_share_the_native_id_byte_limit(self) -> None:
        exact = "é" * (compact.SESSION_ID_MAX_BYTES // 2)
        self.assertEqual(compact.normalize_session_arg("@" + exact), exact)
        with self.assertRaisesRegex(
                compact.CompactError,
                f"exceeds {compact.SESSION_ID_MAX_BYTES} UTF-8 bytes"):
            compact.normalize_session_arg("@" + exact + "é")

    def test_result_handle_numbers_and_tool_claims_are_bounded(self) -> None:
        identity = "0123456789abcdef01234567"
        value = f"@abcdef:7.dead~{identity}:12-20"
        self.assertEqual(
            compact.parse_result_handle_claim(value),
            ("abcdef", 7, "dead", identity, (12, 20)),
        )
        invalid = (
            "@abcdef:9223372036854775808.dead",
            f"@abcdef:7.dead~{identity}:12-9999999",
            f"@abcdef:7.dead~{identity}:20-12",
        )
        for handle in invalid:
            with self.subTest(handle=handle), \
                    self.assertRaises(compact.CompactError):
                compact.parse_result_handle_claim(handle)

    def test_session_prefix_index_sizes_handles(self) -> None:
        sessions = ["abcdef011111", "abcdef012345", "abc", "abcdef",
                    "zz-other-session"]
        index = compact.session_prefix_index(sessions)
        expected = {"abcdef011111": "@abcdef011:1", "abcdef012345": "@abcdef012:1",
                    "abc": "@abc:1", "abcdef": "@abcdef:1",
                    "zz-other-session": "@zz-other:1", "notinlist99": "@notinlis:1"}
        for target, want in expected.items():
            hit = {"session": target, "turn": 1}
            self.assertEqual(
                compact.encode_result_handle(hit, session_index=index), want)
        self.assertEqual(
            compact.encode_session_target("abcdef012345", session_index=index),
            "abcdef012")

    def test_session_prefix_index_forces_full_targets_without_sentinel_ids(self) -> None:
        target = "abcdef012345"
        index = compact.session_prefix_index((target,), force_full=(target,))
        self.assertEqual(tuple(index), (target,))
        self.assertEqual(
            compact.encode_session_target(target, session_index=index), target)

    def test_grouped_renderers_use_collision_safe_result_handles(self) -> None:
        sessions = ["0199aaaa-db7e-a", "0199aaaa-db7e-b"]
        hit = {"session": sessions[1], "turn": 7, "agent": "codex",
               "project": "agrep", "concept": "", "who": "user",
               "content_digest": compact.content_digest("needle"),
               "snippet": "needle"}
        head = search._chat_head(
            hit, 1, False,
            session_index=compact.session_prefix_index(sessions))
        self.assertIn("@0199aaaa-db7e-b:7", head)
        for emit in (lambda: search._emit_grouped([hit], None, False),
                     lambda: search._emit_chats([hit], False)):
            stdout = io.StringIO()
            with mock.patch.object(explore, "_session_index",
                                   return_value={session: {} for session in sessions}), \
                    contextlib.redirect_stdout(stdout):
                emit()
            self.assertIn("@0199aaaa-db7e-b:7", stdout.getvalue())

    def test_snapshot_write_skips_fsync_on_hot_path(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(os, "fsync") as fsync:
                compact.save_snapshot(
                    records, "g", "v1", data_dir=Path(temp), now=10,
                    query="result")
            fsync.assert_not_called()

    def test_snapshot_writer_does_not_double_close_owned_descriptor(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    compact.common, "replace_with_retry",
                    side_effect=OSError("publish failed")), \
                mock.patch.object(compact.os, "close", wraps=os.close) as close:
            with self.assertRaisesRegex(OSError, "publish failed"):
                compact.save_snapshot(
                    records, "g", "v1", data_dir=Path(temp), now=10,
                    query="result")
        close.assert_not_called()

    def test_more_rejects_other_output_contracts(self) -> None:
        for option in ("--json", "--flat", "--classic", "-c", "--self", "--no-self"):
            with self.subTest(option=option):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as stopped:
                        search.main(["--more", "m." + "a" * 24, option])
                self.assertEqual(stopped.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")

    def test_cold_stream_does_not_bypass_self_exclusion(self) -> None:
        cases = (
            (["needle", "--no-self", "--classic", "--color", "never"], False),
            (["needle", "--classic", "--color", "never"], True),
        )
        for argv, agent_context in cases:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as temp, \
                    mock.patch.object(
                        search.common, "MESSAGES_PATH", Path(temp) / "missing.jsonl"), \
                    mock.patch.object(
                        search.common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        search.common, "in_agent_context",
                        return_value=agent_context), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index", return_value=False), \
                    mock.patch.object(
                        search.indexd_runtime, "agent_freshness_notice",
                        return_value=""), \
                    mock.patch.object(
                        search, "_stream_first_run", return_value=0) as stream, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = search.main(argv)
            self.assertEqual(rc, 2)
            stream.assert_not_called()

    def test_successful_cold_stream_reports_freshness_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(
                    search.common, "MESSAGES_PATH", Path(temp) / "missing.jsonl"), \
                mock.patch.object(
                    search.common, "ingest_bin", return_value=Path(__file__)), \
                mock.patch.object(
                    search.common, "in_agent_context", return_value=False), \
                mock.patch.object(
                    search.indexd_runtime, "ensure_index") as ensure_index, \
                mock.patch.object(
                    search.indexd_runtime, "agent_freshness_notice",
                    return_value="history may still be refreshing") as freshness, \
                mock.patch.object(
                    search, "_stream_first_run", return_value=0) as stream, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            rc = search.main([
                "needle", "--classic", "--color", "never"])
        self.assertEqual(rc, 0)
        stream.assert_called_once()
        ensure_index.assert_not_called()
        freshness.assert_called_once_with()
        self.assertIn("history may still be refreshing", stderr.getvalue())

    def test_frozen_rows_are_one_line(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: "one\r\ntwo\nthree")
        self.assertEqual(records[0]["line"], "one two three")

    def test_handles_are_short_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            records = compact.freeze_records([_hit(i) for i in range(6)], lambda hit: "row")
            handle = compact.save_snapshot(
                records, "g", "v1", data_dir=root, now=10, query="result")
            self.assertRegex(handle, r"\Am\.[A-Za-z0-9_-]{8}\Z")
            loaded = compact.load_snapshot(handle, None, "v1", data_dir=root, now=11)
            self.assertEqual(len(loaded), 6)

    def test_more_survives_unrelated_ingest_generation_bump(self) -> None:
        records = compact.freeze_records([_hit(1), _hit(2)],
                                         lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(records, {"sig": "a"},
                                           search._RANKING_VERSION, data_dir=root,
                                           query="result")

            def run_more(generation: dict) -> tuple[int, str, str]:
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(search.common, "DATA_DIR", root), \
                        mock.patch.object(search.common, "transcript_generation",
                                          lambda **kw: generation), \
                        contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = search.main(["--more", handle])
                return rc, out.getvalue(), err.getvalue()

            rc, out, err = run_more({"sig": "a"})
            self.assertEqual(rc, 0)
            self.assertNotIn("newer results may exist", err)
            rc, out, err = run_more({"sig": "b"})
            self.assertEqual(rc, 0)
            self.assertIn("result 1", out)
            self.assertIn("result 2", out)
            self.assertIn("newer results may exist", err)

    def test_more_serves_when_live_generation_cannot_be_verified(self) -> None:
        records = compact.freeze_records(
            [_hit(1), _hit(2)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(
                records, {"sig": "a"}, search._RANKING_VERSION,
                data_dir=root, query="result")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        side_effect=RuntimeError("publication kept changing")), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = search.main(["--more", handle])
            self.assertEqual(rc, 0)
            self.assertIn("result 1", out.getvalue())
            self.assertIn("result 2", out.getvalue())
            self.assertIn(
                "could not verify the current corpus generation", err.getvalue())

    def test_more_serves_page_when_successor_snapshot_cannot_be_written(
            self) -> None:
        records = compact.freeze_records(
            [_hit(index) for index in range(40)],
            lambda hit: f"result {hit['turn']} " + "x" * 500)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(
                records, {"sig": "a"}, search._RANKING_VERSION,
                data_dir=root, query="result")
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(search.common, "DATA_DIR", root),
                mock.patch.object(
                    search.common, "transcript_generation",
                    return_value={"sig": "a"}),
                mock.patch.object(
                    compact, "_save_snapshot",
                    side_effect=OSError("read-only filesystem")),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                rc = search.main(["--more", handle])
        self.assertEqual(rc, 0)
        self.assertIn("result", out.getvalue())
        self.assertIn("without a further handle", err.getvalue())
        self.assertIn("8+ matches (floor)", err.getvalue())
        self.assertNotIn("more:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_first_page_stays_compact_when_snapshot_cannot_be_written(
            self) -> None:
        hits = [_hit(index) for index in range(40)]
        out = io.StringIO()
        with mock.patch.object(
                compact, "_save_snapshot",
                side_effect=PermissionError(1, "sandbox denied")), \
                contextlib.redirect_stderr(out):
            page = compact.start_compact(
                hits, lambda hit: f"@session:{hit['turn']}.abcd result",
                {"sig": "a"}, "v1", query="result")
        self.assertGreaterEqual(len(page.records), compact.MIN_PAGE_HITS)
        self.assertTrue(all(line.startswith("@session:") for line in page.lines))
        self.assertFalse(page.more)
        self.assertIsNone(page.handle)
        self.assertTrue(page.more_unknown)
        self.assertIn("without a further handle", out.getvalue())

    def test_cleanup_reaps_stale_failed_snapshot_temps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = root / ".new-stale"
            fresh = root / ".new-fresh"
            stale.write_text("partial", encoding="utf-8")
            fresh.write_text("active", encoding="utf-8")
            os.utime(stale, (1, 1))
            compact._cleanup(root, 1 + compact.MAX_TTL_S + 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())

    def test_failed_snapshot_unlink_retries_transient_lock(self) -> None:
        path = mock.Mock()
        path.unlink.side_effect = [PermissionError("locked"), None]
        with mock.patch.object(compact.time, "sleep") as sleep:
            compact._unlink_best_effort(path)
        self.assertEqual(path.unlink.call_count, 2)
        sleep.assert_called_once_with(0.005)

    def test_stale_continuation_still_expires_at_ttl(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handle = compact.save_snapshot(records, {"sig": "a"}, "v1",
                                           data_dir=root, now=10, query="result")
            with self.assertRaises(compact.SnapshotExpired):
                compact.continue_compact(handle, {"sig": "b"}, "v1",
                                         data_dir=root, now=311)

    def test_windows_unlink_permission_errors_stay_contained(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        denied = mock.patch.object(
            Path, "unlink", side_effect=PermissionError(13, "sharing violation"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with denied:
                handle = compact.save_snapshot(records, "g", "v1", ttl_s=2,
                                               data_dir=root, now=10, query="result")
            loaded = compact.load_snapshot(handle, "g", "v1", data_dir=root, now=11)
            self.assertEqual(len(loaded), 1)
            with denied, self.assertRaises(compact.SnapshotExpired):
                compact.load_snapshot(handle, "g", "v1", data_dir=root, now=13)


    @staticmethod
    def _deeper_prose(query: str, family: int, rank: int) -> dict:
        text = f"{query} family{family} rank{rank}"
        return {
            "session": f"deep-f{family:02d}-s{rank}", "turn": rank,
            "ts": 1000 - rank, "who": "user", "agent": "codex",
            "project": "p", "score": 1.0 - (family * 6 + rank) / 100,
            "snippet": text,
            "content_digest": compact.content_digest(text),
            "_snippet_complete": True,
        }

    def test_folded_duplicates_still_build_a_compact_page(self) -> None:
        query = "FOLD_TOTALS"
        text = f"{query} the same pasted passage repeated across sessions"
        hits = [{
            "session": f"fold-s{index:02d}", "turn": 1, "ts": 1000 - index,
            "who": "user", "agent": "claude", "project": "p",
            "score": 1.0 - index / 100, "snippet": text,
            "content_digest": compact.content_digest(text),
            "_snippet_complete": True,
        } for index in range(12)]
        sessions = tuple(hit["session"] for hit in hits)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value={s: s for s in sessions}), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sessions), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}):
            page = search._start_compact_page(
                hits, query, search._match_pat(query, "keyword"),
                corpus_more=False, exact_total=len(hits))
        self.assertEqual(len(page.records), 1)
        self.assertIn("×11-chats", page.lines[0])

    def test_deeper_replay_resumes_past_served_rows(self) -> None:
        query = "DEEPER_RESUME"
        hits = [self._deeper_prose(query, family, rank)
                for family in range(10) for rank in range(6)]
        roots = {hit["session"]: hit["session"].rsplit("-", 1)[0]
                 for hit in hits}
        sessions = tuple(hit["session"] for hit in hits)
        pat = search._match_pat(query, "keyword")
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value=roots), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sessions), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}):
            page = search._start_compact_page(
                hits, query, pat, corpus_more=True,
                deeper_argv=("agrep", "--classic", "-n", "80", "--", query))
            served = [(record["hit"]["session"], record["hit"]["turn"])
                      for record in page.records]
            while page.more and page.handle:
                page = compact.continue_compact(
                    page.handle, None, search._RANKING_VERSION,
                    data_dir=Path(temp))
                served.extend(
                    (record["hit"]["session"], record["hit"]["turn"])
                    for record in page.records)
            self.assertEqual(len(served), compact.MAX_FROZEN_HITS)
            self.assertIsNotNone(page.handle)
            replay_argv, covered = compact.load_deeper_context(
                page.handle, search._RANKING_VERSION, data_dir=Path(temp))
            self.assertEqual(replay_argv[-1], query)
            self.assertEqual(covered, len(served))
            with mock.patch.object(search, "_DEEPER_SKIP_ROWS", covered):
                deeper = search._start_compact_page(
                    hits, query, pat, corpus_more=True)
        deeper_keys = [(record["hit"]["session"], record["hit"]["turn"])
                       for record in deeper.records]
        self.assertTrue(deeper_keys)
        self.assertFalse(set(served) & set(deeper_keys))

    def test_deeper_keeps_distinct_tool_events_from_one_turn(self) -> None:
        query = "DEEPER_EVENT_IDENTITY"
        first = _tool_hit(1, session="shared-tool-turn", turn=7, query=query)
        later = _tool_hit(2, session="shared-tool-turn", turn=7, query=query)
        hits = [first, *[_hit(index) for index in range(39)], later]
        sessions = tuple(str(hit["session"]) for hit in hits)
        roots = {session: session for session in sessions}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value=roots), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sessions), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(search, "_DEEPER_SKIP_ROWS",
                                  compact.MAX_FROZEN_HITS), \
                mock.patch.object(search, "_collapse_identical",
                                  side_effect=lambda rows: rows), \
                mock.patch.object(search, "_near_fold",
                                  side_effect=lambda rows, _roots: rows):
            page = search._start_compact_page(
                hits, query, search._match_pat(query, "keyword"),
                corpus_more=False)
        self.assertEqual(len(page.records), 1)
        self.assertEqual(
            page.records[0]["hit"]["_event_identity"],
            later["_event_identity"])

    def test_deeper_replay_with_nothing_left_says_so(self) -> None:
        query = "DEEPER_EXHAUSTED"
        hits = [self._deeper_prose(query, 0, 1)]
        session = hits[0]["session"]
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value={session: session}), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=(session,)), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(search, "_DEEPER_SKIP_ROWS", 1), \
                contextlib.redirect_stderr(stderr):
            page = search._start_compact_page(
                hits, query, search._match_pat(query, "keyword"),
                corpus_more=True)
        self.assertEqual(len(page.records), 0)
        self.assertFalse(page.more)
        self.assertIn("nothing deeper", stderr.getvalue())

    def test_deeper_on_deeper_skips_the_accumulated_covered_count(self) -> None:
        query = "DEEPER_HOPS"
        hits = [self._deeper_prose(query, family, rank)
                for family in range(10) for rank in range(12)]
        roots = {hit["session"]: hit["session"].rsplit("-", 1)[0]
                 for hit in hits}
        sessions = tuple(hit["session"] for hit in hits)
        pat = search._match_pat(query, "keyword")
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(search.common, "DATA_DIR", Path(temp)), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value=roots), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sessions), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}):
            # First deeper: skip the frozen 40, then drain all forty rows in
            # this hop so its terminal handle carries the accumulated 80.
            with mock.patch.object(search, "_DEEPER_SKIP_ROWS",
                                   compact.MAX_FROZEN_HITS):
                hop1 = search._start_compact_page(
                    hits, query, pat, corpus_more=True)
            hop1_keys = {(r["hit"]["session"], r["hit"]["turn"])
                         for r in hop1.records}
            while hop1.more and hop1.handle:
                hop1 = compact.continue_compact(
                    hop1.handle, None, search._RANKING_VERSION,
                    data_dir=Path(temp))
                hop1_keys.update(
                    (r["hit"]["session"], r["hit"]["turn"])
                    for r in hop1.records)
            self.assertEqual(len(hop1_keys), compact.MAX_FROZEN_HITS)
            self.assertIsNotNone(hop1.handle)
            _, covered2 = compact.load_deeper_context(
                hop1.handle, search._RANKING_VERSION, data_dir=Path(temp))
            self.assertEqual(covered2, 2 * compact.MAX_FROZEN_HITS)
            with mock.patch.object(search, "_DEEPER_SKIP_ROWS", covered2):
                hop2 = search._start_compact_page(
                    hits, query, pat, corpus_more=True)
        hop2_keys = {(r["hit"]["session"], r["hit"]["turn"])
                     for r in hop2.records}
        self.assertTrue(hop2_keys)
        self.assertFalse(hop1_keys & hop2_keys)

    def test_later_deeper_replay_widens_past_the_stored_fetch_limit(self) -> None:
        query = "DEEPER_FETCH"
        hit = self._deeper_prose(query, 0, 0)
        result = {
            "hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword", "totals_exact": True,
        }
        limits = []

        def run_query(_query, **kwargs):
            limits.append(kwargs["limit"])
            return result

        with mock.patch.object(search, "_DEEPER_SKIP_ROWS", 80), \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(search.common, "indexed_family_roots",
                                  return_value={hit["session"]: hit["session"]}), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=(hit["session"],)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = search.main([
                query, "--classic", "-n", "80", "--lexical", "--self",
                "--no-auto", "--color=never",
            ], _force_compact=True)
        self.assertEqual(rc, 0)
        self.assertEqual(limits, [120])

def _window(session: str, center: int) -> dict:
    return {"session": session, "project": "proj", "agent": "claude",
            "concept": "", "title": "", "center": center, "first_turn": 1,
            "last_turn": 42, "events": [],
            "turns": [{"turn": center, "who": "user", "ts": 0,
                       "text": "tail text", "reply": "agent reply"}]}


class AroundHandleTests(unittest.TestCase):
    """@handles are stored references, so a renumbered turn must fail loudly;
    the bare positional form keeps its friendly clamp with an always-on note."""

    SESSION = "abcd1234-full-0000"

    def _run(self, argv: list[str], center: int) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               lambda auto=True, **_kw: True), \
                mock.patch.object(around.explore, "resolve_session",
                                  lambda q: [self.SESSION]), \
                mock.patch.object(
                    around.explore, "get_window",
                    lambda session, requested, radius: _window(session, center)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = around.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_result_handle_out_of_range_turn_is_fatal(self) -> None:
        rc, out, err = self._run(["@abcd1234:999999"], center=42)
        self.assertEqual(rc, 2)
        self.assertIn("out of range", err)
        self.assertEqual(out, "")

    def test_result_handle_in_range_turn_still_serves(self) -> None:
        rc, out, err = self._run(["@abcd1234:42"], center=42)
        self.assertEqual(rc, 0)
        self.assertIn("tail text", out)

    def test_bare_turn_clamps_with_note_even_when_piped(self) -> None:
        rc, out, err = self._run([self.SESSION, "999999"], center=42)
        self.assertEqual(rc, 0)
        self.assertIn("tail text", out)
        self.assertIn("out of range - centered on 42", err)

    def test_negative_output_controls_are_usage_errors(self) -> None:
        for option in ("--context", "--max-chars", "--tool-output"):
            with self.subTest(option=option), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                around.main([self.SESSION, "42", option, "-1"])
            self.assertEqual(raised.exception.code, 2)

    def test_zero_keeps_documented_uncapped_and_hidden_semantics(self) -> None:
        rc, out, _ = self._run([
            self.SESSION, "42", "--context", "0", "--max-chars", "0",
            "--tool-output", "0"], center=42)
        self.assertEqual(rc, 0)
        self.assertIn("tail text", out)


class EventPathTests(unittest.TestCase):
    @staticmethod
    def _store(events_dir: Path, rows: list[tuple[str, str, str, bytes]],
               external: bytes | None = None) -> None:
        import sqlite3

        events_dir.mkdir(exist_ok=True)
        hashes = [explore.common._event_payload_hash(payload)
                  for _name, _agent, _session, payload in rows]
        manifest = json.dumps({name: value for (name, *_rest), value in zip(rows, hashes)},
                              separators=(",", ":"), sort_keys=True).encode()
        con = sqlite3.connect(events_dir / explore.common.EVENT_STORE_NAME)
        con.executescript(
            "CREATE TABLE event_sessions (name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
            "session TEXT NOT NULL, hash INTEGER NOT NULL, n_events INTEGER NOT NULL, "
            "payload BLOB NOT NULL, digest BLOB NOT NULL, stats BLOB NOT NULL) WITHOUT ROWID;"
            "CREATE TABLE event_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID;")
        con.executemany(
            "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
            [(name, agent, session,
              value if value < 1 << 63 else value - (1 << 64),
              payload.count(b"\n"), payload, explore.common._event_payload_digest(payload),
              json.dumps({"agent": agent, "calls": 0, "fails": 0, "known": 0,
                          "subagents": 0, "tools": {}}, separators=(",", ":")).encode())
             for (name, agent, session, payload), value in zip(rows, hashes)])
        con.execute("INSERT INTO event_meta VALUES('manifest',?)", (manifest,))
        con.execute("INSERT INTO event_meta VALUES('generation',?)",
                    (explore.common._event_generation_token(manifest),))
        con.commit()
        con.close()
        (events_dir / ".manifest").write_bytes(manifest)
        (events_dir / explore.common.EVENT_GENERATION_NAME).write_bytes(
            explore.common._event_generation_token(manifest) if external is None else external)

    def test_build_index_releases_local_event_reader_before_ingest(self) -> None:
        calls = []

        def close_reader() -> None:
            calls.append("close")

        def run(*_args, **kwargs):
            calls.append("run")
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["errors"], "replace")
            return mock.Mock(returncode=1, stdout="", stderr="failed")

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "REPO_ROOT", Path(td)), \
                mock.patch.object(explore.common, "MESSAGES_PATH", Path(td) / "messages.jsonl"), \
                mock.patch.object(explore.common, "ingest_bin", return_value=Path(td) / "agrep-rs"), \
                mock.patch.object(explore.common, "_close_event_reader", side_effect=close_reader), \
                mock.patch.object(explore.common.subprocess, "run", side_effect=run):
            self.assertFalse(indexd_runtime.build_index(quiet=True))
        self.assertEqual(calls, ["close", "run"])

    def test_reader_restat_failure_closes_replacement_and_clears_stale_state(
            self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = events.EVENTS_DIR
            self._store(events_dir, [])
            store = events_dir / explore.common.EVENT_STORE_NAME
            old = explore.common._event_reader_connection(store)["connection"]
            replacement = mock.Mock()
            changed_stamp = ("changed",)
            with mock.patch.object(
                    events, "_event_store_stamp",
                    side_effect=(changed_stamp, OSError("restat failed"))), \
                    mock.patch.object(
                        sqlite3, "connect", return_value=replacement):
                with self.assertRaisesRegex(OSError, "restat failed"):
                    explore.common._event_reader_connection(store)
            replacement.close.assert_called_once_with()
            self.assertIsNone(
                getattr(explore.common._EVENT_READER_LOCAL, "state", None))
            with self.assertRaises(sqlite3.ProgrammingError):
                old.execute("SELECT 1")

    def test_one_shot_event_readers_close_when_open_verification_raises(
            self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = events.EVENTS_DIR
            self._store(events_dir, [])
            readers = (
                ("names", lambda: explore.common.event_names(events_dir)),
                ("bulk", lambda: list(explore.common.event_blobs_bulk([], full=True))),
            )
            for name, read in readers:
                with self.subTest(reader=name):
                    connection = mock.Mock()
                    with mock.patch.object(
                            events, "_event_store_stamp",
                            side_effect=(("before",), OSError("restat failed"))), \
                            mock.patch.object(
                                sqlite3, "connect", return_value=connection):
                        if name == "names":
                            self.assertEqual(read(), set())
                        else:
                            with self.assertRaisesRegex(
                                    RuntimeError, "cannot be opened"):
                                read()
                    connection.close.assert_called_once_with()

    def test_colliding_legacy_ids_keep_distinct_tool_windows(self) -> None:
        timeline = [{"turn": 1, "ts": 100}, {"turn": 2, "ts": 200}]
        selected = [{"turn": 1, "ts": 100}]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            events_dir.mkdir()
            explore._event_checkpoints.cache_clear()
            paths = [explore.common.event_path_candidates("codex", session)[0]
                     for session in ("a/b", "a?b")]
            self.assertNotEqual(paths[0], paths[1])
            upper = explore.common.event_filename("codex", "Session")
            lower = explore.common.event_filename("codex", "session")
            self.assertNotEqual(upper.lower(), lower.lower())
            self.assertLessEqual(len(explore.common.event_filename(
                "a" * 500, "s" * 500).encode()),
                explore.common.EVENT_FILENAME_MAX_BYTES)
            self.assertEqual(
                explore.common.event_filename("gemini", "a/β?Session"),
                "gemini-a___Session--0cda50f02df0c11ed4b2e40486c8fdb4"
                "c12efbdb2bbe07be.jsonl")
            candidates = explore.common.event_path_candidates("codex", "a/b")
            self.assertEqual(candidates[0], paths[0])
            self.assertEqual(candidates[-1].name, "codex-a_b.jsonl")
            for path, name in zip(paths, ("first", "second")):
                path.write_text(json.dumps({"ts": 150, "kind": "tool",
                                            "name": name}) + "\n", encoding="utf-8")
            first = explore._events_for_turns("codex", "a/b", selected, timeline)
            second = explore._events_for_turns("codex", "a?b", selected, timeline)

            legacy = explore.common.event_path_candidates("codex", "legacy/id")[1]
            legacy.write_text(json.dumps({"ts": 150, "kind": "tool",
                                          "name": "legacy"}) + "\n", encoding="utf-8")
            old = explore._events_for_turns("codex", "legacy/id", selected, timeline)
        self.assertEqual([event["name"] for event in first], ["first"])
        self.assertEqual([event["name"] for event in second], ["second"])
        self.assertEqual([event["name"] for event in old], ["legacy"])

    def test_sqlite_events_are_authoritative_and_legacy_is_migration_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("codex", "session")
            db_payload = (json.dumps({"ts": 150, "kind": "tool", "name": "database"})
                          + "\n").encode()
            self._store(events_dir, [(name, "codex", "session", db_payload)])
            explore.common.event_path_candidates("codex", "session")[0].write_text(
                json.dumps({"ts": 150, "kind": "tool", "name": "stale"}) + "\n",
                encoding="utf-8")
            rows = explore.get_events("codex", "session")
            payload = explore.common.event_blob("codex", "session")
            self.assertIsNotNone(payload)
            tools = events.tool_rows_from_payload(payload, [(100, 1)])
            self.assertTrue(explore.has_events("codex", "session"))
            explore.common._close_event_reader()
        self.assertEqual([row["name"] for row in rows], ["database"])
        self.assertTrue(tools[0]["text"].startswith("database"))

    def test_seeded_sqlite_generation_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("codex", "session")
            db_payload = (json.dumps({"ts": 150, "kind": "tool", "name": "database"})
                          + "\n").encode()
            self._store(events_dir, [(name, "codex", "session", db_payload)], b"moving")
            explore.common.event_path_candidates("codex", "session")[0].write_text(
                json.dumps({"ts": 150, "kind": "tool", "name": "legacy"}) + "\n",
                encoding="utf-8")
            rows = explore.get_events("codex", "session")
            names = explore.common.event_names(events_dir)
            with self.assertRaisesRegex(RuntimeError, "incoherent"):
                list(explore.common.event_blobs_bulk([("codex", "session")], full=True))
            explore.common._close_event_reader()
        self.assertEqual(rows, [])
        self.assertEqual(names, set())

    @unittest.skipUnless(os.name == "posix", "POSIX symlink test")
    def test_sqlite_sidecar_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("codex", "session")
            payload = b'{"ts":1,"kind":"tool","name":"database"}\n'
            self._store(events_dir, [(name, "codex", "session", payload)])
            target = events_dir / "outside"
            target.write_bytes(b"outside")
            os.symlink(target, Path(f"{events_dir / explore.common.EVENT_STORE_NAME}-wal"))

            self.assertIsNone(explore.common.event_blob("codex", "session"))
            self.assertEqual(explore.common.event_names(events_dir), set())
            with self.assertRaisesRegex(RuntimeError, "cannot be opened"):
                list(explore.common.event_blobs_bulk(
                    [("codex", "session")], full=True))
            explore.common._close_event_reader()

    def test_unseeded_sqlite_keeps_legacy_migration_readable(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            events_dir.mkdir()
            con = sqlite3.connect(events_dir / explore.common.EVENT_STORE_NAME)
            con.executescript(
                "CREATE TABLE event_sessions (name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
                "session TEXT NOT NULL, hash INTEGER NOT NULL, n_events INTEGER NOT NULL, "
                "payload BLOB NOT NULL) WITHOUT ROWID;"
                "CREATE TABLE event_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL) "
                "WITHOUT ROWID;")
            con.close()
            (events_dir / ".manifest").write_text("{}", encoding="utf-8")
            name = explore.common.event_filename("codex", "session")
            (events_dir / name).write_text(
                json.dumps({"ts": 150, "kind": "tool", "name": "legacy"}) + "\n",
                encoding="utf-8")
            rows = explore.get_events("codex", "session")
            names = explore.common.event_names(events_dir)
            has_events = explore.has_events("codex", "session")
            explore.common._close_event_reader()
        self.assertEqual([row["name"] for row in rows], ["legacy"])
        self.assertEqual(names, {name})
        self.assertTrue(has_events)

    def test_current_sqlite_missing_row_does_not_resurrect_stale_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            self._store(events_dir, [])
            explore.common.event_path_candidates("codex", "deleted")[0].write_text(
                json.dumps({"ts": 150, "kind": "tool", "name": "stale"}) + "\n",
                encoding="utf-8")
            rows = explore.get_events("codex", "deleted")
            explore.common._close_event_reader()
        self.assertEqual(rows, [])

    def test_missing_seeded_store_never_falls_back_to_stale_legacy(self) -> None:
        import sqlite3

        for empty_store in (False, True):
            with self.subTest(empty_store=empty_store), tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                    mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
                events_dir = Path(td) / "events"
                name = explore.common.event_filename("codex", "session")
                payload = b'{"ts":1,"kind":"tool","name":"current"}\n'
                self._store(events_dir, [(name, "codex", "session", payload)])
                (events_dir / name).write_bytes(
                    b'{"ts":1,"kind":"tool","name":"stale"}\n')
                (events_dir / explore.common.EVENT_STORE_NAME).unlink()
                if empty_store:
                    sqlite3.connect(events_dir / explore.common.EVENT_STORE_NAME).close()

                self.assertIsNone(explore.common.event_blob("codex", "session"))
                self.assertFalse(explore.common.event_exists("codex", "session"))
                self.assertEqual(explore.common.event_names(events_dir), set())
                error = "read failed" if empty_store else "missing"
                with self.assertRaisesRegex(RuntimeError, error):
                    list(explore.common.event_blobs_bulk(
                        [("codex", "session")], full=True))
                explore.common._close_event_reader()

    def test_sqlite_payload_hash_mismatch_fails_closed(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("codex", "session")
            payload = b'{"ts":1,"kind":"tool","name":"valid"}\n'
            self._store(events_dir, [(name, "codex", "session", payload)])
            con = sqlite3.connect(events_dir / explore.common.EVENT_STORE_NAME)
            con.execute("UPDATE event_sessions SET payload=? WHERE name=?",
                        (b'{"ts":1,"kind":"tool","name":"corrupt"}\n', name))
            con.commit()
            con.close()
            self.assertIsNone(explore.common.event_blob("codex", "session"))
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                list(explore.common.event_blobs_bulk(
                    [("codex", "session")], full=True))
            explore.common._close_event_reader()

    @unittest.skipUnless(os.name == "posix", "unlinking an open SQLite file is POSIX-only")
    def test_reader_reopens_replaced_store_even_when_generation_is_unchanged(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("codex", "session")
            old = b'{"ts":1,"kind":"tool","name":"old"}\n'
            new = b'{"ts":1,"kind":"tool","name":"new"}\n'
            self._store(events_dir, [(name, "codex", "session", old)])
            generation = (events_dir / explore.common.EVENT_GENERATION_NAME).read_bytes()
            self.assertEqual(explore.common.event_blob("codex", "session"), old)

            (events_dir / explore.common.EVENT_STORE_NAME).unlink()
            self._store(events_dir, [(name, "codex", "session", new)])
            con = sqlite3.connect(events_dir / explore.common.EVENT_STORE_NAME)
            con.execute("UPDATE event_meta SET value=? WHERE key='generation'", (generation,))
            con.commit()
            con.close()
            (events_dir / explore.common.EVENT_GENERATION_NAME).write_bytes(generation)

            self.assertEqual(explore.common.event_blob("codex", "session"), new)
            explore.common._close_event_reader()

    def test_bulk_event_hydration_scans_sqlite_once(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            rows = []
            keys = []
            for index in range(4):
                session = f"session-{index}"
                name = explore.common.event_filename("codex", session)
                payload = (json.dumps({"ts": index, "kind": "tool", "name": session})
                           + "\n").encode()
                rows.append((name, "codex", session, payload))
                keys.append(("codex", session))
            self._store(events_dir, rows)
            statements = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with mock.patch.object(sqlite3, "connect", side_effect=traced_connect):
                loaded = list(explore.common.event_blobs_bulk(keys, full=True))
        scans = [statement for statement in statements
                 if "SELECT name,agent,session,digest,payload" in statement]
        points = [statement for statement in statements
                  if "WHERE name IN" in statement]
        self.assertEqual(len(loaded), 4)
        self.assertEqual(len(scans), 1)
        self.assertEqual(points, [])

    def test_bulk_hydration_recovers_seeded_legacy_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("claude", "untouched-session")
            payload = (json.dumps({"ts": 150, "kind": "tool", "name": "legacy"})
                       + "\n").encode()
            self._store(events_dir, [(name, "claude", "", payload)])
            loaded = list(explore.common.event_blobs_bulk(
                [("codex", "changed-session"), ("claude", "untouched-session")],
                full=True))
        self.assertEqual(loaded, [("claude", "untouched-session", payload)])

    def test_full_corpus_scan_keeps_untouched_seeded_agent_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"), \
                mock.patch.object(explore.common, "MESSAGES_PATH", Path(td) / "messages.jsonl"), \
                mock.patch("settings.SETTINGS_PATH", Path(td) / "settings.json"):
            messages = [
                {"session": "changed", "agent": "codex", "project": "p",
                 "turn": 1, "ts": 100, "who": "user", "text": "changed"},
                {"session": "untouched", "agent": "claude", "project": "p",
                 "turn": 1, "ts": 100, "who": "user", "text": "untouched"},
            ]
            explore.common.MESSAGES_PATH.write_text(
                "".join(json.dumps(row) + "\n" for row in messages), encoding="utf-8")
            events_dir = Path(td) / "events"
            name = explore.common.event_filename("claude", "untouched")
            payload = b'{"ts":150,"kind":"tool","name":"legacy"}\n'
            self._store(events_dir, [(name, "claude", "", payload)])

            rows = corpusdb._scan()

        self.assertTrue(any(row[8] == "tool" and "legacy" in row[9]
                            for row in rows["untouched"]))

    def test_bounded_delta_batches_event_rows_without_a_full_scan(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(events, "EVENTS_DIR", Path(td) / "events"):
            events_dir = Path(td) / "events"
            rows = []
            keys = []
            for index in range(501):
                session = f"session-{index}"
                name = explore.common.event_filename("codex", session)
                payload = f'{{"ts":{index},"kind":"tool","name":"x"}}\n'.encode()
                rows.append((name, "codex", session, payload))
                keys.append(("codex", session))
            self._store(events_dir, rows)
            statements = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with mock.patch.object(sqlite3, "connect", side_effect=traced_connect):
                loaded = list(explore.common.event_blobs_bulk(keys, full=False))

        batches = [statement for statement in statements if "WHERE name IN" in statement]
        full = [statement for statement in statements
                if "FROM event_sessions ORDER BY name" in statement]
        self.assertEqual(len(loaded), 501)
        self.assertEqual(len(batches), 2)
        self.assertEqual(full, [])


class RecallTests(unittest.TestCase):
    """recall/pack output contracts: numeric --json scores, runnable probe pull
    commands, and budget-cap markers that keep their agrep-around follow-up."""

    def test_probe_help_discloses_the_scoped_miss_line(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as stopped:
            recall.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("compact scoped miss otherwise", help_text)
        self.assertNotIn("silent otherwise", help_text)

    def test_probe_rejects_the_structured_output_contract(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch.object(recall.indexd_runtime, "ensure_index") as ensure, \
                self.assertRaises(SystemExit) as stopped:
            recall.main(["needle", "--probe", "--json"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("--probe emits a pointer line", stderr.getvalue())
        ensure.assert_not_called()

    def test_recall_joins_unquoted_words_but_pack_keeps_queries_separate(
            self) -> None:
        result = {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=search.surface.FreshnessStory("current")), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.search, "run_query",
                                  return_value=result) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                recall.main(["two", "words", "--who", "user", "--lexical"]), 1)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         ["two words"])

        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=search.surface.FreshnessStory("current")), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.search, "run_query",
                                  return_value=result) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                recall.main(
                    ["two", "words", "--who", "user", "--lexical"],
                    prog="pack",
                ),
                1,
            )
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         ["two", "words"])

    def test_expanded_hits_carry_numeric_score(self) -> None:
        sess = "aabbccdd-0000-0000-0000-000000000000"
        win = {"session": sess, "center": 5, "first_turn": 5, "last_turn": 5,
               "agent": "claude", "project": "p", "events": [],
               "turns": [{"turn": 5, "who": "user", "ts": 1,
                          "text": "hit text bluetooth", "reply": ""}]}
        pairs = [({"session": sess, "turn": 5, "score": 0.9, "ts": 1}, win)]
        candidates = [{"session": sess, "turn": 5, "ts": 1, "term_hits": 1},
                      {"session": sess, "turn": 20, "ts": 2, "term_hits": 1}]
        db = mock.Mock()
        with mock.patch.object(recall.corpusdb, "connect", return_value=db), \
                mock.patch.object(recall.corpusdb, "session_text_sizes",
                                  return_value={sess: 7000}), \
                mock.patch.object(recall.corpusdb, "session_term_turns",
                                  return_value=candidates), \
                mock.patch.object(recall.corpusdb, "session_rows",
                                  side_effect=AssertionError("full rows hydrated")), \
                mock.patch.object(recall.explore, "get_windows",
                                  side_effect=lambda reqs: [dict(win) for _ in reqs]):
            out = recall._expand(pairs, ["bluetooth indexing"], 5000, 2)
        db.close.assert_called_once_with()
        self.assertEqual(len(out), 2)
        extra = out[1][0]
        self.assertEqual(extra["matched"], "expanded")
        self.assertIsInstance(extra["score"], float)
        self.assertEqual(extra["score"], 0.5)  # 1 of 2 content terms present
        self.assertEqual(extra["score_kind"], "expanded")

    def test_expansion_filters_current_window_before_reserving_slots(self) -> None:
        sess = "aabbccdd-0000-0000-0000-000000000000"
        win = {"session": sess, "center": 1, "first_turn": 1, "last_turn": 1,
               "agent": "codex", "project": "p", "events": [],
               "turns": [{"turn": 1, "who": "user", "ts": 1,
                          "text": "old evidence", "reply": ""}]}
        pairs = [({"session": sess, "turn": 1, "score": 0.9, "ts": 1}, win)]
        candidates = [
            {"session": sess, "turn": turn, "ts": turn, "term_hits": 2}
            for turn in range(10, 22)
        ]
        candidates.append(
            {"session": sess, "turn": 5, "ts": 5, "term_hits": 1})
        family = recall.common.CallingFamily(
            sess, sess, frozenset({sess}), True, 10)
        policy = recall.common.SelfExclusion(family, 10, "recap")
        requested = []

        def windows(reqs):
            requested.extend(reqs)
            return [
                {**win, "center": turn, "first_turn": turn, "last_turn": turn,
                 "turns": [{**win["turns"][0], "turn": turn, "ts": turn}]}
                for _session, turn, _radius in reqs
            ]

        db = mock.Mock()
        with mock.patch.object(recall.corpusdb, "connect", return_value=db), \
                mock.patch.object(recall.corpusdb, "session_text_sizes",
                                  return_value={sess: 50000}) as sizes, \
                mock.patch.object(recall.corpusdb, "session_term_turns",
                                  return_value=candidates) as term_turns, \
                mock.patch.object(recall.explore, "get_windows",
                                  side_effect=windows):
            out = recall._expand(
                pairs, ["bluetooth indexing"], 30000, 1, policy)
        sizes.assert_called_once_with(
            db, [sess], before_turns={sess: 10})
        term_turns.assert_called_once_with(
            db, [sess], ["bluetooth", "indexing"], 384,
            before_turns={sess: 10})
        self.assertEqual([hit["turn"] for hit, _window in out], [1, 5])
        self.assertEqual(requested, [(sess, 5, 1)])

    def test_grouped_recall_head_uses_collision_safe_result_handle(self) -> None:
        sessions = ["0199aaaa-db7e-a", "0199aaaa-db7e-b"]
        target = sessions[1]
        hit = {"session": target, "turn": 7, "ts": 1, "who": "user",
               "agent": "codex", "project": "agrep", "score": 1.0,
               "snippet": "needle"}
        window = {"session": target, "center": 7, "first_turn": 7,
                  "last_turn": 7, "agent": "codex", "project": "agrep",
                  "events": [], "turns": [{"turn": 7, "who": "user", "ts": 1,
                                               "text": "needle", "reply": ""}]}
        result = {"hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}
        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "get_windows", return_value=[window]), \
                mock.patch.object(recall.explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--budget", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("@0199aaaa-db7e-b:7", stdout.getvalue().splitlines()[0])

    def test_recall_followups_use_collision_safe_session_targets(self) -> None:
        sessions = ["0199aaaa-db7e-a", "0199aaaa-db7e-b"]
        index = compact.session_prefix_index(sessions)
        row = {"session": sessions[1], "turn": 7, "text": "word " * 300,
               "omitted_chars": 0}
        self.assertTrue(recall._shrink_row(row, index))
        self.assertIn("agrep around 0199aaaa-db7e-b 7", row["text"])

        hit = {"session": sessions[1], "turn": 7, "matched": "phrase",
               "agent": "codex", "project": "agrep", "ts": 0,
               "content_digest": compact.content_digest("needle")}
        stdout = io.StringIO()
        with mock.patch.object(explore, "_session_index",
                               return_value={session: {} for session in sessions}), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(recall._probe(["needle"], [hit], "corpusdb", 1), 0)
        self.assertIn("@0199aaaa-db7e-b:7", stdout.getvalue())

    def test_recall_json_and_over_budget_commands_use_unique_targets(self) -> None:
        sessions = ["0199aaaa-db7e-a", "0199aaaa-db7e-b"]
        hits = [{"session": session, "turn": 7 + index, "ts": 1,
                 "who": "user", "agent": "codex", "project": "agrep",
                 "score": 1.0 - index / 10, "snippet": "needle"}
                for index, session in enumerate(sessions)]
        windows = [{"session": hit["session"], "center": hit["turn"],
                    "first_turn": hit["turn"], "last_turn": hit["turn"],
                    "agent": "codex", "project": "agrep", "events": [],
                    "turns": [{"turn": hit["turn"], "who": "user", "ts": 1,
                                "text": "needle " * 800, "reply": ""}]}
                   for hit in hits]
        result = {"hits": hits, "total": 2, "chats": 2, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}
        captured: dict[str, object] = {}

        def capture_json(obj, _budget, session_index, hit_anchors=None,
                         required_tool_rows=None):
            captured["json"] = obj
            captured["index"] = session_index
            captured["required_tool_rows"] = required_tool_rows
            return "{}"

        common_patches = (
            mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(recall.search, "run_query", return_value=result),
            mock.patch.object(recall.explore, "get_windows", return_value=windows),
            mock.patch.object(recall.explore, "_session_index",
                              return_value={session: {} for session in sessions}),
            mock.patch.object(recall, "_expand", side_effect=lambda pairs, *_: pairs),
        )
        with common_patches[0], common_patches[1], common_patches[2], \
                common_patches[3], common_patches[4], \
                mock.patch.object(recall, "_fit_json_payload",
                                  side_effect=capture_json), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--hits", "2",
                              "--json", "--budget", "2048"])
        self.assertEqual(rc, 0)
        json_hit = captured["json"]["hits"][1]
        self.assertIn("agrep around 0199aaaa-db7e-b 8 -C 0",
                      json_hit["window"][0]["text"])
        self.assertNotIn("--full", json_hit["window"][0]["text"])

        def capture_text(text, _budget, _ends, trailer=""):
            captured["text"] = text
            captured["trailer"] = trailer
            return "ok"

        windows = [{**window, "turns": [{**window["turns"][0], "text": "needle"}]}
                   for window in windows]
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "get_windows", return_value=windows), \
                mock.patch.object(recall.explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                mock.patch.object(recall, "_expand", side_effect=lambda pairs, *_: pairs), \
                mock.patch.object(recall, "_fit_text_payload",
                                  side_effect=capture_text), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--hits", "2",
                              "--budget", "64"])
        self.assertEqual(rc, 0)
        # The collision-safe handle matters; taught usage already supplies the verb.
        self.assertIn("@0199aaaa-db7e-b:8", captured["trailer"])

    def test_around_cap_uses_collision_safe_session_target(self) -> None:
        sessions = ["0199aaaa-db7e-a", "0199aaaa-db7e-b"]
        target = sessions[1]
        window = {"session": target, "center": 7, "first_turn": 7,
                  "last_turn": 7, "agent": "codex", "project": "agrep",
                  "concept": "", "title": "", "events": [],
                  "turns": [{"turn": 7, "who": "user", "ts": 1,
                              "text": "word " * 100, "reply": ""}]}
        stdout = io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(explore, "resolve_session", return_value=[target]), \
                mock.patch.object(explore, "get_window", return_value=window), \
                mock.patch.object(explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                contextlib.redirect_stdout(stdout):
            rc = around.main([target, "7", "--max-chars", "64", "--color", "never"])
        self.assertEqual(rc, 0)
        self.assertIn("agrep around 0199aaaa-db7e-b 7 -C 0 --full",
                      stdout.getvalue())

    def test_small_expansion_budget_skips_term_candidate_scan(self) -> None:
        sess = "aabbccdd-0000-0000-0000-000000000000"
        win = {"session": sess, "center": 5, "first_turn": 5, "last_turn": 5,
               "agent": "claude", "project": "p", "events": [],
               "turns": [{"turn": 5, "who": "user", "ts": 1,
                          "text": "hit text", "reply": ""}]}
        pairs = [({"session": sess, "turn": 5, "score": 0.9, "ts": 1}, win)]
        db = mock.Mock()
        with mock.patch.object(recall.corpusdb, "connect", return_value=db), \
                mock.patch.object(recall.corpusdb, "session_text_sizes",
                                  return_value={sess: 7000}), \
                mock.patch.object(recall.corpusdb, "session_term_turns") as term_turns:
            out = recall._expand(pairs, ["bluetooth indexing"], 2000, 2)
        self.assertEqual(out, pairs)
        term_turns.assert_not_called()

    def test_probe_pull_command_reproduces_the_probe(self) -> None:
        hits = [{"session": "0" * 32, "turn": 3, "matched": "phrase",
                 "agent": "claude", "project": "proj", "ts": 0,
                 "content_digest": compact.content_digest("probe text")}]
        for queries, expect in ((["bluetooth", "indexing"],
                                 ["agrep", "pack", "bluetooth", "indexing"]),
                                (["blue tooth"], ["agrep", "recall", "blue tooth"])):
            with self.subTest(queries=queries):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(recall._probe(queries, hits, "corpusdb", 1, 0), 0)
                pull = buf.getvalue().rsplit("pull: ", 1)[1].strip()
                self.assertEqual(shlex.split(pull), expect)
        self.assertNotIn("bluetooth / indexing", pull)

    def test_unverified_probe_miss_does_not_load_session_index(self) -> None:
        result = {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=recall.surface.FreshnessStory(
                        "unverified", code="freshness-unchecked")), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "_session_index") as session_index, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["definite miss", "--probe", "--no-auto", "--lexical"])
        self.assertEqual(rc, 2)
        session_index.assert_not_called()

    def test_chat_prefix_resolves_to_one_full_session_id(self) -> None:
        ids = ["0199aaaa-1111-7000-8000-000000000001",
               "0199aaaa-2222-7000-8000-000000000002",
               "0199bbbb-0000-7000-8000-000000000003"]
        seen: list[object] = []

        def run_query(q, **kw):
            seen.append(kw.get("chat"))
            return {"hits": [], "total": 0, "chats": 0, "engine": "fixture"}

        def recall_main(chat: str) -> tuple[int, str]:
            err = io.StringIO()
            with mock.patch.object(recall.indexd_runtime, "ensure_index",
                                   lambda auto=True, **_kw: True), \
                    mock.patch.object(
                        recall.indexd_runtime, "freshness_story",
                        return_value=search.surface.FreshnessStory("current")), \
                    mock.patch.object(recall.explore, "resolve_session",
                                      lambda q: recall.common.match_session_ids(ids, q)), \
                    mock.patch.object(recall.search, "run_query", run_query), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                rc = recall.main(["needle", "--chat", chat])
            return rc, err.getvalue()

        rc, err = recall_main("0199aaaa")
        self.assertEqual(rc, 2)
        self.assertIn("ambiguous (2 sessions)", err)
        self.assertIn("add a char: 0199aaaa-1 / 0199aaaa-2", err)
        self.assertEqual(seen, [], "ambiguous prefix must never reach run_query")

        rc, err = recall_main("0199cccc")
        self.assertEqual(rc, 2)
        self.assertIn("no session matches", err)

        for query in ("0199bbbb", ids[0]):  # unique prefix and full uuid
            with self.subTest(chat=query):
                seen.clear()
                rc, _ = recall_main(query)
                self.assertEqual(rc, 1)  # resolved fine; fixture just has no hits
                full = ids[2] if query == "0199bbbb" else ids[0]
                self.assertTrue(seen, "resolved --chat never reached run_query")
                self.assertEqual(set(seen), {full})

    def test_stale_handle_turn_exits_2_instead_of_clamping(self) -> None:
        full = "0199bbbb-0000-7000-8000-000000000003"

        def get_windows(reqs):
            # the corpus recenters a nonexistent turn on the nearest real one
            return [{"session": full, "center": min(turn, 209), "first_turn": 0,
                     "last_turn": 209, "agent": "claude", "project": "p",
                     "events": [],
                     "turns": [{"turn": min(turn, 209), "who": "user", "ts": 1,
                                "text": "body", "reply": ""}]}
                    for _, turn, _ in reqs]

        def recall_main(handle: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(recall.indexd_runtime, "ensure_index",
                                   lambda auto=True, **_kw: True), \
                    mock.patch.object(recall.explore, "resolve_session",
                                      lambda q: [full]), \
                    mock.patch.object(recall.explore, "get_windows", get_windows), \
                    mock.patch.object(recall.corpusdb, "connect",
                                      lambda **kw: None), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = recall.main([handle, "--json"])
            return rc, out.getvalue(), err.getvalue()

        rc, out, err = recall_main("@0199bbbb:999999")
        self.assertEqual(rc, 2, "stale handle turn must fail, not serve a clamp")
        failed = json.loads(out)
        self.assertEqual(failed["hits"], [])
        self.assertEqual(
            failed["error"]["code"], "stale-result-handle")
        self.assertIn("self_exclusion", failed)
        self.assertIn("freshness", failed)
        self.assertIn("semantic_coverage", failed)
        self.assertIn("result handle turn 999999 is out of range "
                      "(session has turns 0-209) - the handle is stale", err)

        rc, out, _ = recall_main("@0199bbbb:150")
        self.assertEqual(rc, 0)
        hits = json.loads(out)["hits"]
        self.assertEqual([h["turn"] for h in hits], [150])

    def test_fit_json_shrink_keeps_around_marker(self) -> None:
        sess = "abcdef01-2345-6789-abcd-ef0123456789"
        row = {"kind": "msg", "session": sess, "project": "p", "turn": 7,
               "who": "user", "ts": 0, "text": "word " * 400, "omitted_chars": 0}
        obj = {"query": "q", "engine": "corpusdb",
               "hits": [{"session": sess, "turn": 7, "window": [row]}]}
        raw = recall._fit_json_payload(obj, 900)
        self.assertLessEqual(len(raw), 900)
        got = json.loads(raw)["hits"][0]["window"][0]
        self.assertRegex(
            got["text"],
            r" \[\+[\d,]+ chars - agrep around abcdef01 7 -C 0\]\Z")
        self.assertGreater(got["omitted_chars"], 0)

    def test_fit_text_cap_points_at_cut_window_and_never_splits_markers(self) -> None:
        cap_marker = "[+1,234 chars - agrep around abcd1234 9 -C 0 --full]"
        block1 = "── abcd1234 · claude\n   9 user: " + "x" * 300 + " " + cap_marker
        block2 = "── ffff0000 · claude\n   4 user: " + "y" * 300
        text = block1 + "\n\n" + block2
        ends = [(len(block1), "agrep around abcd1234 9"),
                (len(text), "agrep around ffff0000 4")]

        # cut would land inside block1's existing marker: back off, don't corrupt it
        fitted = recall._fit_text_payload(text, len(block1) + 30, ends)
        self.assertLessEqual(len(fitted), len(block1) + 30)
        self.assertIn("agrep around abcd1234 9", fitted.rsplit("[", 1)[1])
        for m in re.finditer(r"\[\+", fitted):
            self.assertIn("]", fitted[m.start():], "marker sliced mid-command")

        # cut lands in block2's body: the cap marker names block2's window
        fitted = recall._fit_text_payload(text, len(block1) + 120, ends)
        self.assertLessEqual(len(fitted), len(block1) + 120)
        self.assertIn(cap_marker, fitted)
        self.assertIn("agrep around ffff0000 4", fitted.rsplit("[", 1)[1])

        plain_marker = "[1,234 chars]"
        stitched = "z" * 100 + plain_marker + "w" * 100
        final_marker = "\n[output truncated to --budget; rerun with a larger budget]"
        budget = 105 + len(final_marker)
        fitted = recall._fit_text_payload(stitched, budget)
        self.assertLessEqual(len(fitted), budget)
        self.assertNotIn("[1,", fitted)
        self.assertTrue(fitted.endswith(final_marker))

    def test_stitched_windows_prioritize_distinct_terms_over_repeats(self) -> None:
        text = ("alpha repeated filler words. " * 4_000
                + "late decisive context names omega exactly once. "
                + "closing evidence " * 20)
        rendered, omitted = recall._anchored_cap(
            text, 520, "agrep around abcd1234 9 -C 0",
            recall._anchor_patterns("alpha omega"))

        self.assertIn("alpha", rendered.lower())
        self.assertIn("omega", rendered.lower())
        self.assertGreater(omitted, 0)
        self.assertLessEqual(len(rendered), 520)

    def test_degrade_trailer_drops_space_separated_handles_individually(self) -> None:
        handles = ["@0199aaaa:1-aaaa", "@0199bbbb:2-bbbb", "@0199cccc:3-cccc"]
        trailer = f"[+3 hit(s) over budget - {' '.join(handles)}]"
        expected = f"[+3 hit(s) over budget - {handles[0]}]"

        self.assertEqual(
            recall._degrade_trailer(trailer, None, len(expected)), expected)

        legacy = ("[+2 hit(s) over budget - agrep around @0199aaaa:1 · "
                  "agrep around @0199bbbb:2]")
        self.assertEqual(
            recall._degrade_trailer(
                legacy, None, len(legacy.encode("utf-8"))),
            legacy)

    def test_pack_floors_hit_target_at_one_slot_per_query(self) -> None:
        sessions = [f"0199000{i}-0000-7000-8000-00000000000{i}" for i in range(4)]
        queries = ["q0", "q1", "q2", "q3"]
        limits = []

        def run_query(q, **kw):
            limits.append(kw.get("limit"))
            hit = {"session": sessions[queries.index(q)], "turn": 1, "ts": 1,
                   "who": "user", "agent": "codex", "project": "agrep",
                   "score": 1.0, "matched": "phrase", "snippet": q}
            return {"hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
                    "engine": "corpusdb", "mode": "keyword"}

        def windows(reqs):
            return [{"session": sess, "center": turn, "first_turn": turn,
                     "last_turn": turn, "agent": "codex", "project": "agrep",
                     "events": [], "turns": [{"turn": turn, "who": "user",
                                              "ts": 1, "text": "evidence",
                                              "reply": ""}]}
                    for sess, turn, _ in reqs]

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.search, "run_query", side_effect=run_query), \
                mock.patch.object(recall.explore, "get_windows", side_effect=windows), \
                mock.patch.object(recall.explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                mock.patch.object(recall, "_expand",
                                  side_effect=lambda pairs, *a, **k: pairs), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([*queries, "--hits", "3", "--lexical",
                              "--who", "user", "--budget", "0"], prog="pack")
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        # _select's docstring promise: one slot per query, so a --hits below
        # len(queries) is floored and no query silently contributes nothing
        for session in sessions:
            self.assertIn(f"@{session}:1", out)
        # Recall keeps a small post-ranking lookahead so meta echoes cannot
        # consume the only candidate available for a query.
        self.assertEqual(limits, [8, 8, 8, 8])

    def test_pressure_evicts_blocks_instead_of_stubbing_all_of_them(self) -> None:
        # The equal split rendered every hit at _msg_cap's 80-char floor.
        # Law 6 prefers a shorter page of judgeable lines to a page of stubs.
        sessions = [f"0199000{i}-0000-7000-8000-00000000000{i}" for i in range(6)]
        hits = [{"session": session, "turn": 1, "ts": 1, "who": "user",
                 "agent": "codex", "project": "agrep", "score": 1.0,
                 "matched": "phrase", "snippet": "needle"}
                for session in sessions]
        result = {"hits": hits, "total": 6, "chats": 6, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}

        def windows(reqs):
            return [{"session": sess, "center": turn, "first_turn": turn,
                     "last_turn": turn, "agent": "codex", "project": "agrep",
                     "events": [],
                     "turns": [{"turn": turn, "who": "user", "ts": 1,
                                "text": "needle " * 200, "reply": ""}]}
                    for sess, turn, _ in reqs]

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "get_windows", side_effect=windows), \
                mock.patch.object(recall.explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                mock.patch.object(recall, "_expand",
                                  side_effect=lambda pairs, *a, **k: pairs), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--who", "user",
                              "--hits", "6", "--budget", "1800"])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        blocks = re.findall(r"^── (@\S+)", out, re.M)
        self.assertLess(len(blocks), 6, "every hit rendered: the page stubbed "
                                        "instead of evicting (law 6)")
        # what did render must be judgeable, not an 80-char floor stub
        bodies = re.findall(r"^\s+\d+ user: (.*)$", out, re.M)
        self.assertTrue(bodies)
        self.assertGreater(max(len(b) for b in bodies), 200)
        # and the evicted hits are disclosed, not silently dropped
        self.assertRegex(out, r"\[\+\d+ hit\(s\) over budget")

    def test_budget_cap_keeps_the_over_budget_hit_marker(self) -> None:
        # the measured --budget 3000 case: the final cap used to cut from the
        # end and eat the [+N hit(s) over budget] disclosure appended last
        sessions = [f"0199000{i}-0000-7000-8000-00000000000{i}" for i in range(4)]
        hits = [{"session": session, "turn": 1, "ts": 1, "who": "user",
                 "agent": "codex", "project": "agrep", "score": 1.0,
                 "matched": "phrase", "snippet": "needle"}
                for session in sessions]
        result = {"hits": hits, "total": 4, "chats": 4, "tool_hits": 0,
                  "engine": "corpusdb", "mode": "keyword"}

        def windows(reqs):
            return [{"session": sess, "center": turn, "first_turn": turn,
                     "last_turn": turn + 14, "agent": "codex", "project": "agrep",
                     "events": [],
                     "turns": [{"turn": turn + i, "who": "user", "ts": 1,
                                "text": "needle " * 30, "reply": "reply " * 30}
                               for i in range(15)]}
                    for sess, turn, _ in reqs]

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(recall.explore, "get_windows", side_effect=windows), \
                mock.patch.object(recall.explore, "_session_index",
                                  return_value={session: {} for session in sessions}), \
                mock.patch.object(recall, "_expand",
                                  side_effect=lambda pairs, *a, **k: pairs), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--who", "user",
                              "--hits", "4", "--budget", "3000"])
        self.assertEqual(rc, 0)
        out = stdout.getvalue()
        self.assertLessEqual(len(out), 3000)
        evicted = re.search(r"\[\+\d+ hit\(s\) over budget - [^\]]+\]", out)
        self.assertIsNotNone(evicted, "eviction disclosure lost to the budget cap")
        # Handles remain actionable while leaving more of the exhausted budget for evidence.
        self.assertRegex(evicted.group(0), r"@[\w.-]+:\d+")
        # content is evicted before the marker: the cap cut, the trailer survived
        self.assertIn("[output truncated to --budget", out)
        self.assertTrue(out.rstrip().endswith(evicted.group(0)))
        for m in re.finditer(r"\[\+", out):
            self.assertIn("]", out[m.start():], "marker sliced mid-command")


class BoundedHeadlineTests(unittest.TestCase):
    """A stopped lane's headline: what it may state about the result set."""

    NOW_MS = 2_000_000_000_000
    DAY_MS = 86_400_000
    FRESH = 60
    OLD = 2400

    @classmethod
    def setUpClass(cls) -> None:
        search._load_corpusdb()
        cls.tmp = tempfile.TemporaryDirectory(prefix="agrep-headline-")
        cls.corpus = Path(cls.tmp.name) / "corpus.db"
        db = sqlite3.connect(cls.corpus)
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = []
        for index in range(cls.FRESH):
            rows.append((f"fresh-{index:04d}", index % 12,
                         cls.NOW_MS - index * 60_000, "codex", "/repo/blue",
                         "", "gpt-5.4", "fixture", "user",
                         f"widget rollout note {index} ok"))
        for index in range(cls.OLD):
            rows.append((f"chat-{index // 12:04d}", index % 12,
                         cls.NOW_MS - (200 + index) * cls.DAY_MS, "codex",
                         "/repo/blue", "", "gpt-5.4", "fixture",
                         "agent" if index % 3 else "tool",
                         f"deployment note {index} mentions widget ok"))
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        db.executescript(corpusdb._TRIGGERS_SQL)
        db.commit()
        db.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _connect(self, *_args, **_kwargs):
        db = sqlite3.connect(self.corpus)
        corpusdb._register_functions(db)
        return db

    def _query(self, q: str) -> dict:
        with mock.patch.object(corpusdb, "connect", side_effect=self._connect), \
                mock.patch.object(search.time, "time",
                                  return_value=self.NOW_MS / 1000):
            return search.run_query(q, limit=40, exact_totals=False,
                                    allow_fallback=False)

    def _lane_bound(self, q: str) -> dict:
        db = self._connect()
        try:
            with mock.patch.object(search.time, "time",
                                   return_value=self.NOW_MS / 1000):
                boundary = search._prepare_boundary(q, "keyword", db)
                bound = search._bounded_short_keyword_rows(
                    db, q, 40, {}, boundary=boundary)
                if bound is None:
                    bound = search._bounded_keyword_rows(
                        db, q, 40, {}, False, boundary=boundary)
            return bound
        finally:
            db.close()

    def test_an_early_stop_reports_the_counted_total_not_its_own_scan(self) -> None:
        # F1: observed_total is how far the ranked scan read before its frontier
        # closed - the same magnitude for any query, and no answer to "how many"
        bound = self._lane_bound("widget")
        self.assertFalse(bound["totals_exact"], "fixture no longer stops early")
        self.assertLess(bound["total"], self.FRESH + self.OLD)
        res = self._query("widget")
        db = self._connect()
        try:
            counted = corpusdb.keyword_count(db, "widget", {})
        finally:
            db.close()
        self.assertEqual(counted["total"], self.FRESH + self.OLD)
        self.assertEqual(res["total"], counted["total"])
        self.assertEqual(res["chats"], counted["chats"])
        self.assertTrue(res["totals_exact"])
        self.assertTrue(res["total_counted"])

    def test_a_count_that_would_leave_the_index_is_refused_not_paid(self) -> None:
        bound = self._lane_bound("ok")
        self.assertFalse(bound["totals_exact"], "fixture no longer stops early")
        self.assertFalse(corpusdb.count_rides_the_index("ok"))
        res = self._query("ok")
        self.assertFalse(res["totals_exact"])
        self.assertNotIn("total_counted", res)
        self.assertEqual(res["total"], bound["total"])

    def test_an_uncounted_page_states_unknown_and_continuation(self) -> None:
        hits = [_hit(index) for index in range(20)]
        deeper = ("agrep", "--classic", "-n", "80", "--", "result")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, byte_budget=24, corpus_more=True,
                query="result", deeper_argv=deeper, total_uncounted=True)
            seen = []
            while True:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    search._compact_summary(page)
                seen.append(stderr.getvalue())
                if not page.more:
                    break
                page = compact.continue_compact(
                    page.handle, {"sig": "a"}, "rank-v1", data_dir=root,
                    now=11, byte_budget=24)
            self.assertGreater(len(seen), 1)
            for summary in seen:
                self.assertRegex(
                    summary,
                    r"^\d+ shown · total unknown · "
                    r"(?:more: agrep --more|broader rerun \(may repeat\): "
                    r"agrep --deeper) m\.[A-Za-z0-9_-]{8}\n$")
                self.assertNotIn("matching rows", summary)

    def test_a_page_states_one_total_basis_or_the_other(self) -> None:
        records = compact.freeze_records([_hit(1)], lambda hit: hit["text"])
        for pinned in ({"exact_total": 1}, {"total_floor": 9}):
            with self.subTest(pinned=sorted(pinned)), \
                    tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(compact.CompactError):
                    compact.save_snapshot(
                        records, {"sig": "a"}, "v1", data_dir=Path(temp),
                        now=10, query="result", corpus_more=True,
                        deeper_argv=("agrep", "--classic", "-n", "80", "--",
                                     "result"),
                        total_uncounted=True, **pinned)


class ChatScopedPageTests(unittest.TestCase):
    """A page scoped to one chat has no family to diversify across."""

    def _page(self, hits: list[dict], **kwargs) -> compact.CompactPage:
        with tempfile.TemporaryDirectory() as temp:
            return compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=Path(temp), now=10, query="result", **kwargs)

    def test_one_chats_rows_are_not_clamped_to_the_family_cap(self) -> None:
        # F2: under --chat every row is that chat's by construction, so the
        # spreading caps could only withhold the rows the caller asked for
        hits = [_hit(index, family="one-chat", score=1.0 - index * 0.05)
                for index in range(24)]
        for hit in hits:
            hit["session"] = "one-chat"
        clamped = self._page(hits)
        self.assertEqual(len(clamped.records), compact.FAMILY_PAGE_CAP)
        page = self._page(hits, chat_scoped=True)
        self.assertGreater(len(page.records), compact.FAMILY_PAGE_CAP)
        self.assertEqual(len(page.records), compact.MAX_PAGE_HITS)

    def test_one_chats_repeated_turn_still_shows_every_matching_row(self) -> None:
        hits = [_hit(index, family="one-chat") for index in range(12)]
        for hit in hits:
            hit["session"], hit["turn"] = "one-chat", 7
        self.assertEqual(len(self._page(hits).records), 1)
        page = self._page(hits, chat_scoped=True)
        self.assertEqual(len(page.records), len(hits))

    def test_a_chat_scoped_continuation_keeps_the_caps_off(self) -> None:
        hits = [_hit(index, family="one-chat") for index in range(24)]
        for hit in hits:
            hit["session"], hit["turn"] = "one-chat", 7
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["text"], {"sig": "a"}, "rank-v1",
                data_dir=root, now=10, query="result", chat_scoped=True)
            self.assertTrue(page.more)
            page = compact.continue_compact(
                page.handle, {"sig": "a"}, "rank-v1", data_dir=root, now=11)
            self.assertTrue(page.chat_scoped)
            self.assertGreater(len(page.records), compact.FAMILY_PAGE_CAP)

    def test_a_page_spanning_chats_still_spreads_across_them(self) -> None:
        hits = []
        for family in ("chat-a", "chat-b"):
            for index in range(8):
                hit = _hit(index, family=family)
                hit["session"] = family
                hit["turn"] = index
                hits.append(hit)
        page = self._page(hits)
        seen: dict[str, int] = {}
        for record in page.records:
            key = compact._page_family(record["hit"])
            seen[key] = seen.get(key, 0) + 1
        self.assertEqual(len(seen), 2)
        for family, rows in seen.items():
            self.assertLessEqual(rows, compact.FAMILY_PAGE_CAP, family)


if __name__ == "__main__":
    unittest.main()
