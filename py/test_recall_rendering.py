"""Recall/around rendering: relevance-shaped budgets, color, laps.

Failure modes pinned here: a weak block dumping a full transcript, ANSI codes
leaking into piped output, an @handle silently widening into a radius window,
exclusion silently off for an unidentified caller, and slow recalls that
cannot name their phases.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import sqlite3
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import common  # noqa: E402
import compact  # noqa: E402
import corpusdb  # noqa: E402
import recall  # noqa: E402


STRONG = "01990000-0000-7000-8000-000000000001"
WEAK = "01990000-0000-7000-8000-000000000002"

ANSI = re.compile(r"\x1b\[")


def _result(hits):
    return {"hits": hits, "total": len(hits), "chats": len(hits),
            "tool_hits": 0, "engine": "corpusdb", "mode": "keyword"}


def _hits():
    return [
        {"session": STRONG, "turn": 3, "ts": 1, "who": "user",
         "agent": "codex", "project": "agrep", "score": 2.0,
         "matched": "phrase", "snippet": "needle",
         "content_digest": compact.content_digest("needle")},
        {"session": WEAK, "turn": 5, "ts": 1, "who": "user",
         "agent": "codex", "project": "agrep", "score": 0.4,
         "matched": "all-terms", "snippet": "needle",
         "content_digest": compact.content_digest("needle")},
    ]


def _windows(requests):
    out = []
    for sess, turn, _context in requests:
        turns = [{"turn": turn + i, "ts": 1, "who": "user",
                  "text": f"needle context line {i} " + "filler " * 40,
                  "reply": f"reply {i} " + "filler " * 40}
                 for i in range(3)]
        out.append({"session": sess, "center": turn, "first_turn": turn,
                    "last_turn": turn + 2, "agent": "codex",
                    "project": "agrep", "events": [], "turns": turns})
    return out


class RecallBudgetContractTests(unittest.TestCase):
    def test_exact_budget_reserves_the_final_newline(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            recall._write_payload("x" * 64, 64)
        rendered = stdout.getvalue()
        self.assertEqual(len(rendered.encode("utf-8")), 64)
        self.assertTrue(rendered.endswith("\n"))

    def test_multibyte_text_budget_is_utf8_safe_and_byte_exact(self):
        budget = 64
        payload = recall._fit_text_payload(
            "evidence 😀 漢字 " * 40, recall._content_budget(budget))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            recall._write_payload(payload, budget)
        rendered = stdout.getvalue()
        self.assertLessEqual(len(rendered.encode("utf-8")), budget)
        self.assertTrue(rendered.endswith("\n"))
        rendered.encode("utf-8").decode("utf-8")

    def test_budgeted_multibyte_json_stays_valid(self):
        budget = 2048
        obj = {"query": "needle 😀", "engine": "corpusdb", "hits": [],
               "padding": "漢😀" * 2000}
        payload = recall._fit_json_payload(
            obj, recall._content_budget(budget))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            recall._write_payload(payload, budget)
        rendered = stdout.getvalue()
        self.assertLessEqual(len(rendered.encode("utf-8")), budget)
        self.assertTrue(rendered.endswith("\n"))
        json.loads(rendered)

    def test_invalid_finite_budgets_stop_before_index_work(self):
        for prog in ("recall", "pack"):
            for value in ("-1", "1", "63"):
                with self.subTest(prog=prog, value=value), \
                        mock.patch.object(
                            recall.indexd_runtime, "ensure_index") as ensure, \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stopped:
                    recall.main(["needle", "--budget", value], prog=prog)
                self.assertEqual(stopped.exception.code, 2)
                ensure.assert_not_called()

    def test_json_budget_floor_stops_before_index_work(self):
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index") as ensure, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stopped:
            recall.main(["needle", "--json", "--budget", "2047"])
        self.assertEqual(stopped.exception.code, 2)
        ensure.assert_not_called()


class _TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def _run_recall(argv, run_query=None, agent_context=False, windows=None,
                stderr_tty=False, self_scope_has_rows=None,
                self_scope_has_matches=None, prog="recall", expand=None):
    stdout = io.StringIO()
    stderr = _TTYBuffer() if stderr_tty else io.StringIO()
    query = run_query or (
        lambda _q, mode="keyword", **kwargs:
        _result([] if kwargs.get("who") == "tool" else _hits()))
    scope_matches = (
        bool(agent_context)
        if self_scope_has_matches is None else bool(self_scope_has_matches))
    if self_scope_has_rows is False:
        scope_matches = False

    def exclusion_keys(_query, _mode, query_kwargs, policy, **_kwargs):
        if not scope_matches or query_kwargs.get("who") == "tool":
            return []
        count = 2 if policy.reason == "forced" else 1
        return [("hidden", index) for index in range(count)]

    with mock.patch.object(recall.indexd_runtime, "ensure_index",
                           return_value=True), \
            mock.patch.object(recall.indexd_runtime, "agent_freshness_notice",
                              return_value=""), \
            mock.patch.object(
                recall.indexd_runtime, "freshness_story",
                return_value=recall.surface.FreshnessStory("current")), \
            mock.patch.object(recall.common, "in_agent_context",
                              return_value=agent_context), \
            mock.patch.object(
                recall.search, "_self_exclusion_match_keys",
                side_effect=exclusion_keys), \
            mock.patch.object(recall.search, "run_query", side_effect=query), \
            mock.patch.object(recall.explore, "get_windows",
                              side_effect=windows or _windows), \
            mock.patch.object(recall, "_expand",
                              side_effect=expand or (lambda pairs, *a, **k: pairs)), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        try:
            rc = recall.main(argv, prog=prog)
        except SystemExit as exc:
            rc = exc.code
    return rc, stdout.getvalue(), stderr.getvalue()


class RecallCapSpeakerTests(unittest.TestCase):
    def _assert_cap_commands(self, who, json_output, budget):
        text = "needle " + "source " * 3_000 + "SOURCE_TAIL"
        reply = "needle " + "answer " * 3_000 + "ANSWER_TAIL"
        hit = {
            **_hits()[0], "who": who,
            "content_digest": compact.content_digest(text),
        }
        window = {
            "session": STRONG, "center": 3, "first_turn": 3,
            "last_turn": 3, "agent": "codex", "project": "agrep",
            "events": [], "turns": [{
                "turn": 3, "ts": 1, "who": who,
                "text": text, "reply": reply,
            }],
        }
        rc, capped, err = _run_recall(
            ["needle", "--lexical", "--hits", "1", "--budget", str(budget),
             "--color", "never", *(["--json"] if json_output else [])],
            run_query=lambda *_a, **_k: _result([dict(hit)]),
            windows=lambda requests: [window for _ in requests])
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(capped.encode("utf-8")), budget)
        if json_output:
            rows = json.loads(capped)["hits"][0]["window"]
            messages = [(row["who"], row["text"]) for row in rows
                        if row["kind"] == "msg"]
            for row in rows:
                if row["kind"] != "msg":
                    continue
                retained = row["text"].split(" [+")[0]
                source = reply if row["who"] == "agent" else text
                self.assertEqual(
                    len(retained) + row["omitted_chars"], len(source))
        else:
            messages = re.findall(r"^ +3 (\w+): (.+)$", capped, re.M)
        self.assertEqual({role for role, _ in messages}, {who, "agent"})
        for role, message in messages:
            command = re.search(
                r"\[(?:\+[\d,]+ chars - |snippet - open: )(agrep around [^\]]+)\]",
                message)
            self.assertIsNotNone(command, message)
            out = io.StringIO()
            with mock.patch.object(
                    around.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(
                        around.explore, "resolve_session", return_value=[STRONG]), \
                    mock.patch.object(
                        around.explore, "_session_index", return_value={STRONG: {}}), \
                    mock.patch.object(
                        around.explore, "get_window", return_value=window), \
                    mock.patch.object(
                        around.session_context, "indexed_family_roots",
                        return_value={STRONG: STRONG}), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = around.main(
                    [*shlex.split(command[1])[2:], "--json", "--no-auto"])
            self.assertEqual(rc, 0, out.getvalue())
            recovered = {
                row["who"]: row["text"]
                for row in map(json.loads, out.getvalue().splitlines())
                if row.get("kind") == "msg"
            }
            self.assertEqual(
                recovered.get(role), reply if role == "agent" else text)

    def test_cap_commands_recover_nondefault_speakers_and_agent_replies(self):
        for who in ("recap", "control", "harness", "synthetic", "subagent"):
            for json_output in (False, True):
                with self.subTest(who=who, json=json_output):
                    self._assert_cap_commands(
                        who, json_output, 4096 if json_output else 7000)

    def test_budget_shrinking_retains_runnable_cap_commands(self):
        self._assert_cap_commands("user", False, 4096)


class RecallMissRetriesCoverage(unittest.TestCase):
    """A multi-term page with no strong independent row - empty, or filled
    only by meaning rows or weak scatter - hands the shown page to the
    coverage lane, the way search does; --lexical and -s never run it."""

    QUERY = "vncpass source environment bryan vncbox 203.0.113.9 7255"

    def _run(self, argv):
        calls = []

        def emit(q, fkw, hits, self_policy, **_kw):
            calls.append((q, hits))
            common.log("~coverage top row matched 5/7 terms - dropped: a, b")
            return True

        with mock.patch.object(recall.search, "_emit_overspec_block",
                               side_effect=emit), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 12}):
            rc, out, err = _run_recall(
                argv, run_query=lambda *_a, **_k: _result([]))
        return rc, err, calls


    def test_explicit_lane_zero_page_never_retries(self):
        for flag in ("--lexical", "-s"):
            with self.subTest(flag=flag):
                rc, err, calls = self._run(
                    ["alpha beta gamma delta epsilon zeta", flag,
                     "--budget", "0"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertNotIn("~coverage", err)

    def _corpus(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, [
            ("evidence", 149, 50, "pi", "solo", "", "", "", "tool",
             "vncpass=hunter bryan vncbox 203.0.113.9 7255 connected"),
            *[(f"chatter-{n}", 0, n, "claude", "solo", "", "", "", "user",
               "source the environment before the build")
              for n in range(20)],
        ])
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        return db

    def _run_real_lane(self, page_hit):
        def query(_q, mode="keyword", **kwargs):
            if mode == "semantic":
                return {**_result([page_hit] if "sem_score" in page_hit
                                  else []),
                        "engine": "semantic", "mode": "semantic"}
            if kwargs.get("who") == "tool" or "sem_score" in page_hit:
                return _result([])
            return _result([page_hit])

        with mock.patch.object(recall.search, "corpusdb") as fake:
            fake.connect.side_effect = lambda **_kw: self._corpus()
            fake.coverage_rank = corpusdb.coverage_rank
            fake.term_session_df = corpusdb.term_session_df
            return _run_recall(
                [self.QUERY, "--budget", "0", "--project", "solo", "--no-self"],
                run_query=query)

    def test_coverage_hint_does_not_add_hits_to_the_requested_page(self):
        meaning = {"session": "meaning", "turn": 2, "ts": 1, "who": "agent",
                   "agent": "pi", "project": "solo", "sem_score": 0.8962,
                   "score": 0.8962, "snippet": "server rejected the password",
                   "content_digest": compact.content_digest("rejected")}
        rc, out, err = self._run_real_lane(meaning)
        self.assertEqual(rc, 0)
        self.assertIn("@meaning:2", out)
        self.assertNotIn("@evidence", out)
        self.assertIn("@evidence:149.", err)
        self.assertEqual(
            shlex.split(err[err.index("agrep "):].splitlines()[0]),
            ["agrep", "--project=solo", "--no-self", "--coverage", "--",
             self.QUERY])

    def test_empty_pack_surfaces_recovered_evidence_for_each_query(self):
        other_query = "calendar deadline hearing timestamp filing docket missingtoken"

        def corpus(**_kwargs):
            db = self._corpus()
            text = "calendar deadline hearing timestamp filing docket"
            db.execute(corpusdb._INS_DIGEST, (
                "calendar", 4, 85, "pi", "solo", "", "", "", "agent",
                text, compact.content_digest(text)))
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            return db

        with mock.patch.object(recall.search, "corpusdb") as fake:
            fake.connect.side_effect = corpus
            fake.coverage_rank = corpusdb.coverage_rank
            fake.term_session_df = corpusdb.term_session_df
            rc, out, err = _run_recall(
                [self.QUERY, other_query, "--budget", "0",
                 "--project", "solo", "--no-self"],
                run_query=lambda *_a, **_kw: _result([]), prog="pack")
        self.assertEqual(rc, 1)
        self.assertNotIn("@evidence", out)
        self.assertNotIn("@calendar", out)
        self.assertIn("@evidence:149.", err)
        self.assertIn("@calendar:4.", err)

    def test_strong_pack_runs_no_coverage_lane(self):
        strong = {"session": "evidence", "turn": 149, "ts": 1, "who": "tool",
                  "agent": "pi", "project": "solo", "score": 2.0,
                  "matched": "phrase", "_boundary_class": "aligned",
                  "snippet": "vncpass=hunter bryan vncbox",
                  "content_digest": compact.content_digest("vncpass")}
        rc, out, err = self._run_real_lane(strong)
        self.assertEqual(rc, 0)
        self.assertIn("@evidence:149", out)
        self.assertNotIn("coverage", err)


class RecallSelfScopeTests(unittest.TestCase):
    @staticmethod
    def _policy(reason="window"):
        boundary = None if reason == "forced" else 3
        family = common.CallingFamily(
            STRONG, STRONG, frozenset({STRONG, WEAK}), True, boundary)
        return common.SelfExclusion(family, boundary, reason)

    @staticmethod
    def _hit(session, text):
        return {
            "session": session, "turn": 3, "ts": 1, "who": "user",
            "agent": "codex", "project": "agrep", "score": 2.0,
            "matched": "phrase", "snippet": text,
            "content_digest": compact.content_digest(text),
        }

    def test_engine_filtered_nonempty_page_stays_silent(self):
        other = "01990000-0000-7000-8000-000000000003"

        def query(_query, mode="keyword", **kwargs):
            self.assertEqual(kwargs["exclude_session"], STRONG)
            return _result([] if kwargs.get("who") == "tool" else [
                self._hit(other, "needle independent")])

        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=self._policy()):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"],
                run_query=query, agent_context=True)
        self.assertEqual(rc, 0)
        self.assertIn(other, out)
        self.assertNotIn("current chat is excluded", err)
        self.assertNotIn("session family is excluded", err)

    def test_engine_filtered_zero_discloses_current_chat_scope(self):
        def query(_query, mode="keyword", **kwargs):
            self.assertEqual(kwargs["exclude_session"], STRONG)
            return _result([])

        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=self._policy()):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"],
                run_query=query, agent_context=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("excluded 1 hit from the current window", err)
        self.assertNotIn("session family is excluded", err)

    def test_engine_filtered_zero_is_silent_when_caller_is_not_indexed(self):
        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=self._policy()):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"],
                run_query=lambda *_args, **_kwargs: _result([]),
                agent_context=True, self_scope_has_rows=False)
        self.assertEqual((rc, out), (1, ""))
        self.assertNotIn("--self to include", err)

    def test_engine_filtered_zero_is_silent_for_unrelated_current_rows(self):
        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=self._policy()):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"],
                run_query=lambda *_args, **_kwargs: _result([]),
                agent_context=True, self_scope_has_rows=True,
                self_scope_has_matches=False)
        self.assertEqual((rc, out), (1, ""))
        self.assertNotIn("--self to include", err)

    def test_no_recap_keeps_all_family_chats_visible(self):
        hits = [
            self._hit(STRONG, "needle current"),
            self._hit(WEAK, "needle useful side chat"),
        ]

        with mock.patch.object(
                recall.common, "calling_self_exclusion", return_value=None):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"],
                run_query=lambda _q, mode="keyword", **kwargs: _result(
                    [] if kwargs.get("who") == "tool" else hits),
                agent_context=True)
        self.assertEqual(rc, 0)
        self.assertIn(STRONG, out)
        self.assertIn(WEAK, out)
        self.assertNotIn("excluded", err)

    def test_explicit_no_self_keeps_whole_family_scope(self):
        other = "01990000-0000-7000-8000-000000000003"
        hits = [
            self._hit(STRONG, "needle current"),
            self._hit(WEAK, "needle side chat"),
            self._hit(other, "needle independent"),
        ]

        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=self._policy("forced")):
            rc, out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0", "--no-self"],
                run_query=lambda _q, mode="keyword", **kwargs: _result(
                    [] if kwargs.get("who") == "tool" else hits),
                agent_context=True)
        self.assertEqual(rc, 0)
        self.assertNotIn(STRONG, out)
        self.assertNotIn(WEAK, out)
        self.assertIn(other, out)
        self.assertIn("excluded 2 hits from this session family", err)


class WeakBlockCollapseTests(unittest.TestCase):
    def test_weak_block_is_one_line_with_handle_even_uncapped(self):
        rc, out, _err = _run_recall(["needle", "--lexical", "--budget", "0"])
        self.assertEqual(rc, 0)
        blocks = [b for b in out.split("\n\n") if b.startswith("── ")]
        self.assertEqual(len(blocks), 2)
        weak = next(b for b in blocks if f"@{WEAK}:5" in b)
        self.assertEqual(len(weak.splitlines()), 1,
                         "weak block rendered a transcript, not one line")
        # the line stays judgeable: it carries the row's own match
        self.assertIn("needle", weak)
        # the head/snippet sentinel renders as plain ' - ' off the color path
        self.assertIn(" - ", weak)
        self.assertNotIn("\x1f", out)

    def test_top_block_keeps_its_transcript(self):
        rc, out, _err = _run_recall(["needle", "--lexical", "--budget", "0"])
        self.assertEqual(rc, 0)
        top = next(b for b in out.split("\n\n") if f"@{STRONG}:3" in b)
        self.assertGreater(len(top.splitlines()), 1,
                           "the top block lost its window to the collapse")

    def test_relevance_share_favors_the_top_block_under_pressure(self):
        strong_extra = {"session": WEAK, "turn": 5, "ts": 1, "who": "user",
                        "agent": "codex", "project": "agrep", "score": 1.0,
                        "matched": "phrase", "snippet": "needle"}
        hits = [_hits()[0], strong_extra]
        rc, out, _err = _run_recall(
            ["needle", "--lexical", "--budget", "2200"],
            run_query=lambda _q, mode="keyword", **kwargs:
            _result([] if kwargs.get("who") == "tool" else hits))
        self.assertEqual(rc, 0)
        blocks = [b for b in out.split("\n\n") if b.startswith("── ")]
        self.assertGreaterEqual(len(blocks), 2)
        top = next(b for b in blocks if f"@{STRONG}:3" in b)
        second = next(b for b in blocks if f"@{WEAK}:5" in b)
        self.assertGreater(len(top), len(second),
                           "budget split by content, not relevance")


class RecallMetaTests(unittest.TestCase):
    """The engine already rank-reduced ~meta rows; the page must say so."""

    @staticmethod
    def _query(hits):
        return (lambda _q, mode="keyword", **kwargs:
                _result([] if kwargs.get("who") == "tool" else hits))

    def test_meta_rows_are_marked_without_a_banner(self):
        hits = _hits()
        hits[1] = {**hits[1], "_meta_row": True}
        rc, out, err = _run_recall(["needle", "--lexical", "--budget", "0"],
                                   run_query=self._query(hits))
        self.assertEqual(rc, 0)
        blocks = out.split("\n\n")
        meta = next(b for b in blocks if f"@{WEAK}:5" in b)
        self.assertIn("~meta", meta.splitlines()[0])
        lived = next(b for b in blocks if f"@{STRONG}:3" in b)
        self.assertNotIn("~meta", lived)
        # the row markers are the whole disclosure (law 7): no banner restates them
        self.assertNotIn("rank-reduced", err)

    def test_an_all_meta_page_marks_rows_but_stays_quiet(self):
        hits = [{**h, "_meta_row": True} for h in _hits()]
        rc, out, err = _run_recall(["needle", "--lexical", "--budget", "0"],
                                   run_query=self._query(hits))
        self.assertEqual(rc, 0)
        self.assertIn("~meta", out)
        self.assertNotIn("rank-reduced", err)

    def test_tty_has_no_routine_hit_engine_budget_footer(self):
        rc, _out, err = _run_recall(
            ["needle", "--lexical", "--budget", "900"], stderr_tty=True)
        self.assertEqual(rc, 0)
        self.assertNotIn(" · budget ", err)
        self.assertNotRegex(err, r"\d+ hits? · ")


class RecallEventsUnavailableTests(unittest.TestCase):
    """A window whose event store could not be read was rendered as one with no tool
    events, with nothing said; recall now says so the way around does."""

    @staticmethod
    def _windows(blind: str):
        def windows(requests):
            out = _windows(requests)
            for window in out:
                window["events_unavailable"] = window["session"] == blind
            return out
        return windows

    def test_a_window_without_its_tool_events_is_disclosed(self):
        rc, out, err = _run_recall(
            ["needle", "--lexical", "--budget", "0", "--json"], windows=self._windows(WEAK))
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        flags = {hit["session"]: hit.get("events_unavailable") for hit in payload["hits"]}
        self.assertEqual(flags, {STRONG: None, WEAK: True})
        kinds = [note["kind"] for note in payload["served"]["divergences"]]
        self.assertEqual(kinds, ["events_unavailable"])
        self.assertIn("tool events are unavailable right now", err)

        rc, _out, err = _run_recall(
            ["needle", "--lexical", "--budget", "0"], windows=self._windows(WEAK))
        self.assertEqual(rc, 0, err)
        self.assertIn("tool events are unavailable right now", err)

    def test_readable_windows_disclose_nothing(self):
        rc, out, err = _run_recall(
            ["needle", "--lexical", "--budget", "0", "--json"], windows=self._windows(""))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("served", json.loads(out))
        self.assertNotIn("tool events are unavailable", err)

    def test_merged_windows_keep_the_flag_of_either_part(self):
        first, second = _windows([(STRONG, 3, 1), (STRONG, 9, 1)])
        second["events_unavailable"] = True
        merged = recall._merge_chat_windows([(_hits()[0], first), (_hits()[0], second)])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0][1]["events_unavailable"])


class RecallColorTests(unittest.TestCase):
    def test_color_off_when_piped(self):
        _rc, out, _err = _run_recall(["needle", "--lexical", "--budget", "0"])
        self.assertIsNone(ANSI.search(out), "ANSI codes leaked into a pipe")

    def test_forced_color_paints_headers_rows_and_markers(self):
        rc, out, _err = _run_recall(
            ["needle", "--lexical", "--budget", "900", "--color", "always"])
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[1;36m── ", out)      # block header
        self.assertIn("\x1b[33m3\x1b[0m", out)   # turn number
        self.assertIn("\x1b[2m[", out)           # dim continuation marker
        self.assertLessEqual(len(out.encode("utf-8")), 900)
        # Stripping paint also leaves the visible page inside the same cap.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        self.assertLessEqual(len(plain.encode("utf-8")), 900)

    def test_never_wins_over_always_absent(self):
        _rc, out, _err = _run_recall(
            ["needle", "--lexical", "--budget", "0", "--color", "never"])
        self.assertIsNone(ANSI.search(out))

    def test_dashes_in_project_and_snippet_never_move_the_header_split(self):
        # ' - ' is legal inside a project name AND inside snippet prose; the
        # head/snippet boundary is structural, not a delimiter search.
        def windows(requests):
            out = _windows(requests)
            for w in out:
                w["project"] = "app - web"
                for t in w["turns"]:
                    t["text"] = "alpha - beta needle " + "filler " * 40
            return out

        hits = _hits()
        for h in hits:
            h["project"] = "app - web"
        rc, out, _err = _run_recall(
            ["needle", "--lexical", "--budget", "0", "--color", "always"],
            run_query=lambda _q, mode="keyword", **kwargs:
            _result([] if kwargs.get("who") == "tool" else hits),
            windows=windows)
        self.assertEqual(rc, 0)
        headers = [ln for ln in out.splitlines() if "── " in ln]
        self.assertEqual(len(headers), 2)
        for line in headers:
            head = line.split("\x1b[0m")[0]  # first reset closes the header
            self.assertIn("app - web", head,
                          "a dash inside the project name split the header")
            self.assertNotIn("alpha", head,
                             "snippet prose leaked into the painted header")
        self.assertNotIn("\x1f", out)


class CallerUnknownTests(unittest.TestCase):
    """An unresolved automatic window fails open and stays silent. An explicit
    --no-self request that cannot identify its target is the only warning case."""

    LINE = ("--no-self was not applied: caller identity is unknown; "
            "no session was excluded")

    def _run(self, argv, *, agent):
        with mock.patch.object(recall.common, "calling_self_exclusion",
                               return_value=None), \
                mock.patch.object(
                    recall.common, "calling_identity",
                    return_value=common.CallerIdentity(
                        None, "caller-unresolved")):
            return _run_recall(argv, agent_context=agent)

    def test_agent_context_unknown_window_is_silent(self):
        rc, _out, err = self._run(
            ["needle", "--lexical", "--budget", "0"], agent=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("caller identity", err)
        self.assertNotIn("--no-self", err)

    def test_explicit_no_self_unknown_target_warns_once(self):
        rc, _out, err = self._run(
            ["needle", "--lexical", "--budget", "0", "--no-self"],
            agent=True)
        self.assertEqual(rc, 0)
        lines = [ln for ln in err.splitlines() if "--no-self" in ln]
        self.assertEqual(lines, [self.LINE])

    def test_human_terminal_gets_nothing(self):
        rc, _out, err = self._run(
            ["needle", "--lexical", "--budget", "0"], agent=False)
        self.assertEqual(rc, 0)
        self.assertNotIn("caller identity unknown", err)
        self.assertNotIn("could not identify", err)

    def test_resolved_caller_does_not_get_the_line(self):
        family = common.CallingFamily(
            session=STRONG, root=STRONG, members=frozenset({STRONG}),
            resolved=False, recap_turn=None)
        with mock.patch.object(
                recall.common, "calling_self_exclusion",
                return_value=common.SelfExclusion(family, None, "unresolved")):
            _rc, _out, err = _run_recall(
                ["needle", "--lexical", "--budget", "0"], agent_context=True)
        self.assertNotIn("caller identity unknown", err)


class AroundHandleDefaultTests(unittest.TestCase):
    def _radius(self, argv):
        seen = []

        def get_window(sess, center, radius):
            seen.append(radius)
            return {"session": sess, "agent": "codex", "project": "agrep",
                    "concept": "", "title": "", "center": center,
                    "first_turn": 0, "last_turn": 9,
                    "turns": [{"turn": center, "ts": 1, "who": "user",
                               "text": "t", "reply": "r"}],
                    "events": []}

        with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(around.explore, "resolve_session",
                                  return_value=[STRONG]), \
                mock.patch.object(around.explore, "get_window",
                                  side_effect=get_window), \
                mock.patch.object(around.explore, "_session_index",
                                  return_value={STRONG: {}}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = around.main(argv)
        self.assertEqual(rc, 0)
        return seen[0]

    def test_handle_defaults_to_the_named_turn(self):
        self.assertEqual(self._radius(["@01990000:5"]), 0)

    def test_positional_form_keeps_the_radius(self):
        self.assertEqual(self._radius([STRONG, "5"]), 4)

    def test_explicit_context_beats_the_handle_default(self):
        self.assertEqual(self._radius(["@01990000:5", "-C", "2"]), 2)

    def test_around_marker_dimming_is_color_gated(self):
        text = "x [+1,234 chars - agrep around s 1 -C 0 --max-chars 0] y"
        self.assertIn("\x1b[2m[+1,234 chars", around._dim_markers(text, True))
        self.assertEqual(around._dim_markers(text, False), text)


class ToolLinePreviewTests(unittest.TestCase):
    """F2/F5 owner seam: one output line first, byte count second, ok
    tri-state - a present-but-None outcome renders neutral, never FAILED."""

    @staticmethod
    def _event(**extra):
        event = {"kind": "tool", "name": "Bash", "input": "git push",
                 "ok": True, "output": "To github.com:me/agrep.git\nsecond"}
        event.update(extra)
        return event

    def test_unknown_outcome_renders_neutral_failure_stays_marked(self):
        self.assertNotIn("FAILED",
                         around._tool_line(self._event(ok=None), False, 0))
        self.assertIn("FAILED",
                      around._tool_line(self._event(ok=False), False, 0))
        colored = around._tool_line(self._event(ok=None), True, 0)
        self.assertNotIn(around._C["bad"], colored)
        self.assertIn(around._C["bad"],
                      around._tool_line(self._event(ok=False), True, 0))

    def test_default_line_leads_with_output_then_byte_count(self):
        event = self._event()
        line = around._tool_line(event, False, 0)
        first, size = "To github.com:me/agrep.git", (
            f"({len(event['output'].encode('utf-8')):,}B)")
        self.assertIn(first, line)
        self.assertIn(size, line)
        self.assertLess(line.index(first), line.index(size))
        self.assertNotIn("second", line[line.index(first) + len(first):])

    def test_unknown_source_bytes_are_omitted_not_invented(self):
        line = around._tool_line(self._event(
            output=("x" * 800) + "…", output_chars=90_000,
            output_truncated=True), False, 0)
        self.assertNotIn("B)", line)
        self.assertIn("90,000c source; indexed excerpt", line)

    def test_requested_full_output_is_not_doubled_by_the_preview(self):
        line = around._tool_line(self._event(), False, 400)
        head = line.splitlines()[0]
        self.assertNotIn("To github.com", head)


class EventCopyCarriageTests(unittest.TestCase):
    """F5/F7: the explicit JSON event copies carry output_bytes and
    payload_bounds through instead of recomputing or dropping them."""

    EVENT = {"turn": 5, "ts": 1, "kind": "tool", "name": "Bash",
             "input": "x", "ok": None, "output": "payload",
             "input_chars": 1, "output_chars": 7, "output_bytes": 7,
             "input_truncated": False, "output_truncated": False,
             "payload_bounds": (2, 7)}

    def test_around_json_events_carry_bytes_bounds_and_null_ok(self):
        window = {"session": STRONG, "agent": "codex", "project": "agrep",
                  "concept": "", "title": "", "center": 5,
                  "first_turn": 0, "last_turn": 9,
                  "turns": [{"turn": 5, "ts": 1, "who": "user",
                             "text": "t", "reply": "r"}],
                  "events": [dict(self.EVENT)]}
        stdout = io.StringIO()
        with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(around.explore, "resolve_session",
                                  return_value=[STRONG]), \
                mock.patch.object(around.explore, "get_window",
                                  return_value=window), \
                mock.patch.object(around.explore, "_session_index",
                                  return_value={STRONG: {}}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = around.main([STRONG, "5", "--json", "--who", "tool"])
        self.assertEqual(rc, 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        tool = next(row for row in rows if row.get("kind") == "tool")
        self.assertEqual(tool["output_bytes"], 7)
        self.assertEqual(tool["payload_bounds"], [2, 7])
        self.assertIsNone(tool["ok"])

    def test_recall_json_events_carry_bytes_and_bounds(self):
        def windows(requests):
            out = []
            for sess, turn, _context in requests:
                out.append({"session": sess, "center": turn,
                            "first_turn": turn, "last_turn": turn,
                            "agent": "codex", "project": "agrep",
                            "events": [dict(self.EVENT, turn=turn)],
                            "turns": [{"turn": turn, "ts": 1, "who": "user",
                                       "text": "needle", "reply": ""}]})
            return out

        rc, out, _err = _run_recall(
            ["needle", "--lexical", "--json", "--budget", "0"],
            windows=windows)
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        events = [row for hit in obj["hits"] for row in hit["window"]
                  if row.get("kind") == "tool"]
        self.assertTrue(events)
        self.assertEqual(events[0]["output_bytes"], 7)
        self.assertEqual(events[0]["payload_bounds"], [2, 7])


class PayloadRenderControlTests(unittest.TestCase):
    """F7 public-render closure: the exact payload span wins the snippet
    budget on the rendered surface, with scaffolding metadata both before
    and after the true payload."""

    @staticmethod
    def _entry(text, bounds):
        return {"session": STRONG, "agent": "codex", "project": "agrep",
                "concept": "", "model": "", "model_source": "tool",
                "turn": 3, "ts": 1, "who": "tool", "text": text,
                "low": text.lower(),
                "content_digest": compact.content_digest(text),
                "event_kind": "tool", "kind": "tool", "name": "Bash",
                "input": "x", "output": "y", "ok": True,
                "input_chars": 1, "output_chars": 1, "output_bytes": 1,
                "input_truncated": False, "output_truncated": False,
                "payload_bounds": bounds}

    def _run_search(self, argv, entry):
        import os
        import explore
        import search
        search._load_corpusdb()
        import corpusdb as corpusdb_mod
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(corpusdb_mod, "connect",
                                  return_value=None), \
                mock.patch.object(corpusdb_mod, "query_rebuild_required",
                                  return_value=True), \
                mock.patch.object(search, "_jsonl_native_keyword",
                                  return_value=None), \
                mock.patch.object(type(corpusdb_mod.DB_PATH), "exists",
                                  lambda self: True), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(explore, "_freshen", lambda: None), \
                mock.patch.object(
                    explore, "direct_snapshot_attempt",
                    return_value=contextlib.nullcontext({})), \
                mock.patch.object(explore, "_iter_kw_corpus",
                                  lambda flt=None: iter([dict(entry)])), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_metadata_before_the_payload_is_not_spent_by_the_snippet(self):
        text = ("Bash: setup scaffolding input metadata\n"
                "real payload output line zz probe evidence tail")
        entry = self._entry(text, (text.index("real payload"), len(text)))
        for argv in (["zz probe", "--classic", "--color", "never"],
                     ["probe", "-w", "--classic", "--color", "never"]):
            rc, out, _err = self._run_search(argv, entry)
            self.assertEqual(rc, 0)
            self.assertIn("zz probe", out)
            self.assertNotIn("scaffolding input", out)

    def test_trailing_metadata_never_outranks_the_true_payload(self):
        payload = "true payload body"
        text = ('Bash: deploy zz probe\n'
                f'{{"result":"{payload}","note":"trailing metadata junk"}}')
        start = text.index(payload)
        entry = self._entry(text, (start, start + len(payload)))
        rc, out, _err = self._run_search(
            ["zz probe", "--classic", "--color", "never"], entry)
        self.assertEqual(rc, 0)
        self.assertIn(payload, out)
        self.assertNotIn("trailing metadata junk", out)

    def test_json_rows_publish_the_bounds_and_keep_the_handle_outside(self):
        text = ("Bash: setup scaffolding input metadata\n"
                "real payload output line zz probe evidence tail")
        entry = self._entry(text, (text.index("real payload"), len(text)))
        rc, out, _err = self._run_search(["zz probe", "--json"], entry)
        self.assertEqual(rc, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        hit = next(row for row in rows if row.get("session") == STRONG)
        self.assertEqual(
            hit["payload_bounds"],
            [text.index("real payload"), len(text)])
        # the copyable continuation identity stays outside the elided snippet
        self.assertTrue(hit["snippet"].startswith("…"))
        self.assertEqual((hit["session"], hit["turn"]), (STRONG, 3))


class RecallLapTests(unittest.TestCase):
    def test_recall_marks_its_phases_for_the_slow_line(self):
        with mock.patch.object(recall.common, "lap") as lap:
            rc, _out, _err = _run_recall(
                ["needle", "--lexical", "--budget", "0"])
        self.assertEqual(rc, 0)
        labels = [call.args[0] for call in lap.call_args_list]
        for phase in ("freshen", "query", "windows", "expand", "render"):
            self.assertIn(phase, labels)


PACK_CHAT = "01990000-0000-7000-8000-00000000000a"


def _pack_hit(query: str, turn: int) -> dict:
    text = f"{query} evidence at turn {turn}"
    return {"session": PACK_CHAT, "turn": turn, "ts": 1 + turn, "who": "user",
            "agent": "codex", "project": "agrep", "score": 2.0,
            "matched": "phrase", "snippet": text,
            "content_digest": compact.content_digest(text)}


def _pack_windows(requests):
    out = []
    for sess, center, context in requests:
        turns = [{"turn": turn, "ts": 1 + turn, "who": "user",
                  "text": (f"escprobe evidence at turn {turn}" if turn == 0 else
                           f"metaprobe evidence at turn {turn}" if turn == 3 else
                           f"context line {turn}"),
                  "reply": f"reply {turn}"}
                 for turn in range(max(0, center - context), center + context + 1)]
        out.append({"session": sess, "center": center, "first_turn": 0,
                    "last_turn": 40, "agent": "codex", "project": "agrep",
                    "events": [], "turns": turns})
    return out


class PackSameChatTests(unittest.TestCase):
    """pack dedupes by (session, turn): a chat may serve two queries at two turns."""

    def _run_pack(self, argv, hits):
        def query(q, mode="keyword", **kwargs):
            if kwargs.get("who") == "tool":
                return _result([])
            return _result([dict(hit) for hit in hits.get(q, [])])
        return _run_recall(
            [*argv, "--lexical", "--budget", "0", "--color", "never"],
            run_query=query, windows=_pack_windows, prog="pack")

    def test_second_query_keeps_its_turn_in_an_already_chosen_chat(self):
        hits = {"escprobe": [_pack_hit("escprobe", 0)],
                "metaprobe": [_pack_hit("metaprobe", 3)]}
        rc, out, err = self._run_pack(["escprobe", "metaprobe", "-C", "0"], hits)
        self.assertEqual(rc, 0, err)
        self.assertIn("escprobe evidence at turn 0", out)
        self.assertIn("metaprobe evidence at turn 3", out)
        # one chronological block per chat, the gap between the hit turns marked
        self.assertEqual(out.count("── "), 1)
        self.assertIn("[turns 1-2 not shown]", out)
        self.assertLess(out.index("escprobe evidence"), out.index("metaprobe evidence"))

        rc, out, err = self._run_pack(
            ["escprobe", "metaprobe", "-C", "0", "--json"], hits)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual([hit["turn"] for hit in payload["hits"]], [0])
        rows = [(row["turn"], row["who"]) for hit in payload["hits"]
                for row in hit["window"] if row["kind"] == "msg"]
        self.assertEqual(rows, [(0, "user"), (0, "agent"), (3, "user"), (3, "agent")])

    def test_overlapping_windows_in_one_chat_render_each_turn_once(self):
        for second_turn, rows_shown in ((3, [0, 1, 2, 3, 4, 5]), (1, [0, 1, 2, 3])):
            hits = {"escprobe": [_pack_hit("escprobe", 0)],
                    "metaprobe": [_pack_hit("metaprobe", second_turn)]}
            with self.subTest(second_turn=second_turn):
                rc, out, err = self._run_pack(
                    ["escprobe", "metaprobe", "-C", "2", "--json"], hits)
                self.assertEqual(rc, 0, err)
                payload = json.loads(out)
                self.assertEqual(len(payload["hits"]), 1)
                shown = [row["turn"] for row in payload["hits"][0]["window"]
                         if row["kind"] == "msg" and row["who"] == "user"]
                self.assertEqual(shown, rows_shown)

                rc, out, err = self._run_pack(
                    ["escprobe", "metaprobe", "-C", "2"], hits)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("not shown", out)
                for turn in rows_shown:
                    self.assertEqual(out.count(f"\n  {turn:>4} user:"), 1, turn)

    def test_a_single_query_still_takes_one_turn_per_chat(self):
        hits = {"escprobe": [_pack_hit("escprobe", 0), _pack_hit("escprobe", 3)]}
        rc, out, err = _run_recall(
            ["escprobe", "--lexical", "--budget", "0", "-C", "0", "--json"],
            run_query=lambda q, mode="keyword", **kw: _result(
                [] if kw.get("who") == "tool" else [dict(h) for h in hits[q]]),
            windows=_pack_windows)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(json.loads(out)["hits"]), 1)

    def test_select_and_merge_contracts(self):
        chosen = recall._select([
            {"session": "a", "turn": 0, "_recall_query": 0},
            {"session": "a", "turn": 3, "_recall_query": 1},
            {"session": "a", "turn": 0, "_recall_query": 1},
            {"session": "b", "turn": 7, "_recall_query": 1},
        ], 4, 2, family_diverse=False)
        self.assertEqual([(h["session"], h["turn"]) for h in chosen],
                         [("a", 0), ("a", 3), ("b", 7)])

        def window(center, turns):
            return {"session": "a", "center": center,
                    "turns": [{"turn": t} for t in turns],
                    "events": [{"turn": t, "ts": t, "name": "x", "input": ""}
                               for t in turns]}
        pairs = recall._merge_chat_windows([
            ({"turn": 3, "_recall_query": 1}, window(3, [2, 3, 4])),
            ({"turn": 0, "_recall_query": 0}, window(0, [0, 1, 2])),
        ])
        self.assertEqual(len(pairs), 1)
        merged = pairs[0][1]
        self.assertEqual([t["turn"] for t in merged["turns"]], [0, 1, 2, 3, 4])
        self.assertEqual([e["turn"] for e in merged["events"]], [0, 1, 2, 3, 4])
        self.assertEqual(merged["hit_turns"], {0, 3})
        self.assertEqual(merged["queries"], [1, 0])
        self.assertEqual(recall._gap_line(2, 4), "       [turn 3 not shown]")
        self.assertEqual(recall._gap_line(0, 9), "       [turns 1-8 not shown]")

    def test_a_second_block_that_fits_the_remainder_is_not_over_budget(self):
        # the block reserve (budget // judgeable block) once dropped a small second
        # chat as "over budget" with most of the budget unused
        other = "01990000-0000-7000-8000-00000000000b"
        hits = [dict(_pack_hit("escprobe", 0)),
                {**_pack_hit("escprobe", 0), "session": other, "score": 1.5}]
        budget = recall._JUDGEABLE_BLOCK + 400
        rc, out, err = _run_recall(
            ["escprobe", "--lexical", "--budget", str(budget), "-C", "0",
             "--color", "never"],
            run_query=lambda q, mode="keyword", **kw: _result(
                [] if kw.get("who") == "tool" else [dict(h) for h in hits]),
            windows=_pack_windows)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.encode("utf-8")), budget)
        self.assertEqual(out.count("── "), 2)
        self.assertNotIn("over budget", out)

    def test_every_hit_turn_renders_before_any_block_widens(self):
        # at a low budget one block's widening past -C 0 once beat the other hits
        # ("9 turns at 599 bytes, +2 hits over budget")
        sessions = ["01990000-0000-7000-8000-00000000000%x" % i for i in range(3)]
        hits = [{**_pack_hit("escprobe", 0), "session": s, "score": 2.0 - i / 10}
                for i, s in enumerate(sessions)]

        def windows(requests):
            out = []
            for sess, center, context in requests:
                turns = [{"turn": turn, "ts": 1 + turn, "who": "user",
                          "text": f"escprobe evidence at turn {turn} in {sess[-1]}",
                          "reply": f"reply {turn} " + "context " * 12}
                         for turn in range(max(0, center - context), center + context + 1)]
                out.append({"session": sess, "center": center, "first_turn": 0,
                            "last_turn": 8, "agent": "codex", "project": "agrep",
                            "events": [], "turns": turns})
            return out

        def widen(pairs, *_a, **_k):
            # expansion offers the first chat whole, as a term scan would
            first_hit, first = pairs[0]
            whole = {**windows([(first["session"], 4, 4)])[0], "center": 0}
            return [(first_hit, whole), *pairs[1:]]

        budget = 1100
        rc, out, err = _run_recall(
            ["escprobe", "--lexical", "--budget", str(budget), "-C", "0",
             "--hits", "3", "--color", "never"],
            run_query=lambda q, mode="keyword", **kw: _result(
                [] if kw.get("who") == "tool" else [dict(h) for h in hits]),
            windows=windows, expand=widen)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.encode("utf-8")), budget)
        self.assertNotIn("over budget", out)
        for sess in sessions:
            self.assertIn(f"escprobe evidence at turn 0 in {sess[-1]}", out)

    def test_a_big_block_is_cut_to_its_share_so_the_other_query_renders(self):
        # one big bigprobe chat once filled --budget 400/600/900 whole and the
        # escprobe chat vanished from the text page
        big, small = PACK_CHAT, "01990000-0000-7000-8000-00000000000b"
        hits = {"bigprobe": [{**_pack_hit("bigprobe", 1), "session": big}],
                "escprobe": [{**_pack_hit("escprobe", 0), "session": small}]}

        def windows(requests):
            out = []
            for sess, center, _context in requests:
                if sess == big:
                    turn = {"turn": 1, "ts": 2, "who": "user",
                            "text": "bigprobe " + "lorem ipsum filler words " * 300,
                            "reply": "big reply " + "more filler text " * 300}
                else:
                    turn = {"turn": 0, "ts": 1, "who": "user",
                            "text": "escprobe found the module quickly",
                            "reply": "escprobe confirmed"}
                out.append({"session": sess, "center": center, "first_turn": turn["turn"],
                            "last_turn": turn["turn"], "agent": "codex",
                            "project": "agrep", "events": [], "turns": [turn]})
            return out

        def pack(budget):
            return _run_recall(
                ["bigprobe", "escprobe", "--lexical", "-C", "0", "--budget",
                 str(budget), "--color", "never"],
                run_query=lambda q, mode="keyword", **kw: _result(
                    [] if kw.get("who") == "tool" else
                    [dict(h) for h in hits.get(q, [])]),
                windows=windows, prog="pack")

        for budget in (600, 900):
            with self.subTest(budget=budget):
                rc, out, err = pack(budget)
                self.assertEqual(rc, 0, err)
                self.assertLessEqual(len(out.encode("utf-8")), budget)
                self.assertEqual(out.count("── "), 2)
                self.assertIn("escprobe found the module quickly", out)
                self.assertNotIn("over budget", out)
                self.assertIn("bigprobe", out)
                self.assertIn("agrep around", out)
        # too small for a judgeable share each: the big hit renders, the other is
        # evicted and accounted for - its handle when it fits, else the count alone
        rc, out, err = pack(400)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.encode("utf-8")), 400)
        self.assertEqual(out.count("── "), 1)
        self.assertIn("bigprobe", out)
        self.assertRegex(out, r"\[\+1 hit\(s\) over budget - (@" + small[:8]
                         + r"|\+1 more \(see --json omitted_hits\))")
        rc, out, err = pack(300)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.encode("utf-8")), 300)
        self.assertTrue(out.startswith("── @" + big[:8]))
        self.assertRegex(out, r"\[\+1 hit\(s\) over budget")

    def test_final_cap_names_every_block_it_evicts_whole(self):
        blocks = [f"── @{chr(97 + i)}:1.0000 · codex · agrep\n     1 user: " + f"block {i} "
                  + "evidence " * 20 for i in range(4)]
        text = "\n\n".join(blocks)
        ends, off = [], 0
        for i, block in enumerate(blocks):
            off += len(block)
            ends.append((off, f"@{chr(97 + i)}:1.0000"))
            off += 2
        page = recall._fit_text_payload(text, 420, ends, "")
        self.assertLessEqual(len(page.encode("utf-8")), 420)
        self.assertIn("block 0", page)
        self.assertRegex(page, r"\[\+\d+ hit\(s\) over budget - @c:1\.0000 @d:1\.0000\]")
        self.assertIn("rest at @b:1.0000", page)
        # a loop-built trailer keeps its count and gains the blocks the cap evicts
        page = recall._fit_text_payload(
            text, 420, ends, "[+2 hit(s) over budget - @e:1.0000 @f:1.0000]")
        self.assertLessEqual(len(page.encode("utf-8")), 420)
        self.assertRegex(page, r"\[\+\d+ hit\(s\) over budget - (@[a-f]:1\.0000 ?)+\]")
        self.assertIn("@e:1.0000", page)
        self.assertIn("@f:1.0000", page)
        # the count is the loop's two plus the blocks the cap evicted
        count = int(re.search(r"\[\+(\d+) hit\(s\) over budget", page).group(1))
        named = re.findall(r"@[a-f]:1\.0000", page.rsplit("\n", 1)[-1])
        self.assertEqual(count, 2 + (len(named) - 2))

    def test_trailer_never_displaces_the_first_block(self):
        # `pack manyprobe escprobe hugeprobe --budget 300 --hits 12` once printed a
        # 259-byte handle list and no evidence at all
        sessions = ["01990000-0000-7000-8000-0000000000%02x" % i for i in range(12)]
        hits = {"manyprobe": [{**_pack_hit("manyprobe", 0), "session": s}
                              for s in sessions[:10]],
                "escprobe": [{**_pack_hit("escprobe", 0), "session": sessions[10]}],
                "hugeprobe": [{**_pack_hit("hugeprobe", 0), "session": sessions[11]}]}

        def windows(requests):
            out = []
            for sess, center, _context in requests:
                huge = sess == sessions[11]
                out.append({"session": sess, "center": center, "first_turn": 0,
                            "last_turn": 0, "agent": "codex", "project": "agrep",
                            "events": [], "turns": [{
                                "turn": 0, "ts": 1, "who": "user",
                                "text": ("hugeprobe " + "filler " * 400) if huge
                                else f"probe evidence in {sess[-2:]}",
                                "reply": "reply " + "words " * (200 if huge else 5)}]})
            return out

        for budget, blocks in ((300, 1), (500, 2), (900, 3)):
            with self.subTest(budget=budget):
                rc, out, err = _run_recall(
                    ["manyprobe", "escprobe", "hugeprobe", "--lexical", "--budget",
                     str(budget), "--hits", "12", "--color", "never"],
                    run_query=lambda q, mode="keyword", **kw: _result(
                        [] if kw.get("who") == "tool" else
                        [dict(h) for h in hits.get(q, [])]),
                    windows=windows, prog="pack")
                self.assertEqual(rc, 0, err)
                self.assertLessEqual(len(out.encode("utf-8")), budget)
                self.assertTrue(out.startswith("── @" + sessions[0][:8]), out[:80])
                self.assertIn("probe evidence in 00", out)
                self.assertEqual(out.count("── "), blocks)
                trailer = re.search(r"\[\+(\d+) hit\(s\) over budget - ([^\]]*)\]\s*\Z", out)
                self.assertIsNotNone(trailer, out[-200:])
                self.assertEqual(int(trailer.group(1)), 12 - blocks)
                named = re.findall(r"@[0-9a-f-]+:\d+\.[0-9a-f]+", trailer.group(2))
                more = re.search(r"\+(\d+) more \(see --json omitted_hits\)",
                                 trailer.group(2))
                # every omitted hit is accounted for: named, or counted in "+K more"
                self.assertEqual(len(named) + (int(more.group(1)) if more else 0),
                                 12 - blocks)
                self.assertTrue(named or more)

    def test_tiny_budgets_keep_the_first_head_or_refuse_with_the_bytes_needed(self):
        # 64-200 bytes once printed nothing but a handle list (rc 0); now the bare
        # count and the first block's head render, with its match line when it fits,
        # and below the head's own cost the budget is refused naming the bytes needed
        sessions = ["01990000-0000-7000-8000-0000000000%02x" % i for i in range(12)]
        big_text = "bigprobe " + "lorem ipsum filler words " * 300

        def windows(requests):
            out = []
            for sess, center, _context in requests:
                big = sess == sessions[11]
                out.append({"session": sess, "center": center, "first_turn": 0,
                            "last_turn": 0, "agent": "codex", "project": "agrep",
                            "events": [], "turns": [{
                                "turn": 0, "ts": 1, "who": "user",
                                "text": big_text if big else f"probe evidence in {sess[-2:]}",
                                "reply": "reply " + "words " * (200 if big else 5)}]})
            return out

        scenarios = (
            ("manyprobe", ["manyprobe", "--hits", "10"],
             {"manyprobe": [{**_pack_hit("manyprobe", 0), "session": s}
                            for s in sessions[:10]]}, 10),
            ("three queries", ["manyprobe", "escprobe", "hugeprobe", "--hits", "12"],
             {"manyprobe": [{**_pack_hit("manyprobe", 0), "session": s}
                            for s in sessions[:10]],
              "escprobe": [{**_pack_hit("escprobe", 0), "session": sessions[10]}],
              "hugeprobe": [{**_pack_hit("hugeprobe", 0), "session": sessions[11]}]}, 12),
            ("bigprobe/escprobe", ["bigprobe", "escprobe"],
             {"bigprobe": [{**_pack_hit("bigprobe", 0), "session": sessions[11]}],
              "escprobe": [{**_pack_hit("escprobe", 0), "session": sessions[10]}]}, 2),
        )
        for label, argv, hits, total in scenarios:
            need = None
            for budget in (64, 100, 150, 200):
                with self.subTest(scenario=label, budget=budget):
                    rc, out, err = _run_recall(
                        [*argv, "--lexical", "--budget", str(budget), "--color", "never"],
                        run_query=lambda q, mode="keyword", **kw: _result(
                            [] if kw.get("who") == "tool" else
                            [dict(h) for h in hits.get(q, [])]),
                        windows=windows, prog="pack")
                    refusal = re.search(r"need at least (\d+) bytes", err)
                    if refusal:
                        need = int(refusal.group(1))
                        self.assertEqual((rc, out), (2, ""))
                        self.assertGreater(need, budget)
                        self.assertIn(f"agrep pack: --budget {budget} is too small", err)
                        continue
                    self.assertEqual(rc, 0, err)
                    self.assertLessEqual(len(out.encode("utf-8")), budget)
                    self.assertEqual(out.count("── "), 1)
                    head = out.split("\n", 1)[0]
                    self.assertRegex(head, r"^── @01990000-[0-9a-f-]+:0\.[0-9a-f]{4} · codex")
                    self.assertTrue(out.rstrip().endswith(
                        f"[+{total - 1} hit(s) over budget - see --json omitted_hits]"), out)
                    if need is not None and budget >= need + 40:
                        self.assertRegex(out, r"\n     0 user: \S+")
            self.assertIsNotNone(need, label)

    def test_single_hit_pages_keep_the_whole_head_or_refuse(self):
        # `recall hugeprobe --budget 64` once printed a lone '─' and at 80 cut the
        # handle mid-digest, exit 0; the head is the floor for one hit too
        sess = PACK_CHAT
        hits = [{**_pack_hit("hugeprobe", 0), "session": sess}]

        def windows(requests):
            return [{"session": sess, "center": 0, "first_turn": 0, "last_turn": 0,
                     "agent": "codex", "project": "agrep", "events": [], "turns": [{
                         "turn": 0, "ts": 1, "who": "user",
                         "text": "hugeprobe " + "filler " * 400,
                         "reply": "reply " + "words " * 200}]}
                    for _req in requests]

        need = None
        for budget in (64, 80, 100, 150, 200):
            with self.subTest(budget=budget):
                rc, out, err = _run_recall(
                    ["hugeprobe", "--lexical", "--budget", str(budget), "--color", "never"],
                    run_query=lambda q, mode="keyword", **kw: _result(
                        [] if kw.get("who") == "tool" else [dict(h) for h in hits]),
                    windows=windows)
                refusal = re.search(r"need at least (\d+) bytes", err)
                if refusal:
                    need = int(refusal.group(1))
                    self.assertEqual((rc, out), (2, ""))
                    self.assertGreater(need, budget)
                    self.assertIn(f"agrep recall: --budget {budget} is too small", err)
                    continue
                self.assertEqual(rc, 0, err)
                self.assertLessEqual(len(out.encode("utf-8")), budget)
                head = out.split("\n", 1)[0]
                self.assertRegex(head, r"^── @" + sess + r":0\.[0-9a-f]{4} · codex · agrep")
        self.assertIsNotNone(need)
        self.assertLess(need, 150)

    def test_the_cap_never_cuts_inside_a_later_blocks_head(self):
        # two blocks, so no third block's eviction can move the cut by accident
        blocks = [f"── @{chr(97 + i)}:1.0000 · codex · agrep\n     1 user: " + f"block {i} "
                  + "evidence " * 12 for i in range(2)]
        text = "\n\n".join(blocks)
        ends, off = [], 0
        for i, block in enumerate(blocks):
            off += len(block)
            ends.append((off, f"@{chr(97 + i)}:1.0000"))
            off += 2
        marker = len("\n[output truncated to --budget - rest at @b:1.0000]")
        # budgets whose raw cut lands inside block 2's head line
        for budget in range(len(blocks[0]) + 2 + marker + 4,
                            len(blocks[0]) + 2 + marker + 30, 5):
            with self.subTest(budget=budget):
                page = recall._fit_text_payload(text, budget, ends, "")
                self.assertLessEqual(len(page.encode("utf-8")), budget)
                for m in re.finditer(r"@[ab]:1", page):
                    self.assertIn(page[m.start():m.start() + 9], ("@a:1.0000", "@b:1.0000"))
                self.assertNotRegex(page, r"── @b:1\.0{0,3}\n")
                self.assertIn("block 0", page)
                self.assertRegex(page, r"\[\+1 hit\(s\) over budget - @b:1\.0000\]")


if __name__ == "__main__":
    unittest.main()
