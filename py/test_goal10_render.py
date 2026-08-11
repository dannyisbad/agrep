"""Goal-10 recall/around rendering: relevance-shaped budgets, color, laps.

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
                self_scope_has_matches=None):
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
            mock.patch.object(recall.common, "in_agent_context",
                              return_value=agent_context), \
            mock.patch.object(
                recall.search, "_self_exclusion_match_keys",
                side_effect=exclusion_keys), \
            mock.patch.object(recall.search, "run_query", side_effect=query), \
            mock.patch.object(recall.explore, "get_windows",
                              side_effect=windows or _windows), \
            mock.patch.object(recall, "_expand",
                              side_effect=lambda pairs, *a, **k: pairs), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        rc = recall.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


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
        text = "x [+1,234 chars - agrep around s 1 -C 0 --full] y"
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


if __name__ == "__main__":
    unittest.main()
