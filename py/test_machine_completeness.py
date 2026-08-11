"""Every machine surface states its own completeness once per search page.

The defect these pin: `--json` printed forty of 1,765 rows with no total, no
truncation flag, and - unlike `--flat` - not even a stderr line. A parser
piping the surface built for parsers got a confidently wrong count. The
checker shares `surface_policy.completeness_disclosure/completeness_line`
with the emitter, so a reworded signal cannot pass by re-spelling a string.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shlex
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import compact  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402
import session_context  # noqa: E402
import surface_policy as surface  # noqa: E402


def _hit(index: int) -> dict:
    snippet = f"needle row {index}"
    return {
        "session": f"s{index:04d}", "turn": index, "ts": 1, "who": "user",
        "agent": "codex", "project": "agrep", "score": 0.9,
        "matched": "phrase", "_boundary_class": "aligned",
        "snippet": snippet, "content_digest": compact.content_digest(snippet),
    }


def _result(shown: int, total: int, *, truncated: bool = True,
            totals_exact: bool = True, semantic: bool = False) -> dict:
    hits = [_hit(i) for i in range(shown)]
    if semantic:
        for hit in hits:
            hit.update(sem_score=0.9, score_kind="cosine")
    return {
        "hits": hits, "total": total,
        "chats": len({hit["session"] for hit in hits}),
        "tool_hits": 0,
        "engine": "semantic:q8" if semantic else "corpusdb",
        "mode": "semantic" if semantic else "keyword",
        "totals_exact": totals_exact, "truncated": truncated,
        "fallback_recommended": False,
        "semantic_status": ({"state": "ready", "complete": True,
                             "fallback_recommended": False}
                            if semantic else None),
        "semantic_coverage": None,
    }


class MachineCompleteness(unittest.TestCase):
    def setUp(self) -> None:
        indexd_runtime._clear_freshen_failure()
        self.addCleanup(indexd_runtime._clear_freshen_failure)
        family = session_context.CallingFamily(
            "root", "root", frozenset({"root"}), True, 5)
        self.policy = session_context.SelfExclusion(family, None, "forced")
        self.failure = surface.FreshnessFailure(
            "fixture-failure", "fixture index failure", 4)
        self.calls: list[dict] = []

    def _search(self, argv: list[str],
                result: dict) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()

        def query(*_args, **kwargs):
            self.calls.append(kwargs)
            return copy.deepcopy(result)

        with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "indexing_failure",
                    return_value=self.failure), \
                mock.patch.object(
                    common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    common, "calling_self_exclusion", return_value=self.policy), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(
                    search, "_resolve_chat", return_value="session-prefix"), \
                mock.patch.object(search, "run_query", side_effect=query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def _rows(self, stdout: str) -> list[dict]:
        return [json.loads(line) for line in stdout.splitlines() if line]

    def test_truncated_json_leads_with_one_run_envelope(self):
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"], _result(40, 1765))
        head, *rows = self._rows(stdout)
        self.assertEqual(head["kind"], "agrep-meta")
        self.assertEqual(len(rows), 40)
        expected = surface.completeness_disclosure(
            shown=40, total=1765, unit="matching row", totals_exact=True,
            truncated=True,
            more_command="agrep --lexical --json -n 80 -- needle",
            more_command_kind="broader-rerun",
            full_command="agrep --lexical --json -n 0 -- needle")
        self.assertEqual(head["completeness"], expected)
        page_fields = {
            "completeness", "freshness", "filter_coverage",
            "self_exclusion", "semantic_coverage", "semantic_integrity",
            "engine", "query", "tools_excluded",
        }
        for row in rows:
            self.assertFalse(page_fields & row.keys())
            self.assertRegex(row["handle"], r"^@s\d{4}:\d+\.[0-9a-f]{4}$")
            self.assertNotIn("content_digest", row)
        self.assertTrue(expected["truncated"])
        self.assertEqual(expected["total"], 1765)

    def test_complete_json_says_so_rather_than_staying_silent(self):
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"],
            _result(3, 3, truncated=False))
        lines = self._rows(stdout)
        self.assertEqual(len(lines), 4)
        block = lines[0]["completeness"]
        self.assertFalse(block["truncated"])
        self.assertEqual((block["shown"], block["total"]), (3, 3))
        self.assertNotIn("more_command", block)

    def test_empty_json_meta_record_carries_completeness(self):
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"],
            _result(0, 0, truncated=False))
        rows = self._rows(stdout)
        self.assertEqual([row["kind"] for row in rows], ["agrep-meta"])
        self.assertEqual(
            rows[0]["completeness"],
            surface.completeness_disclosure(
                shown=0, total=0, unit="matching row",
                totals_exact=True, truncated=False))

    def test_json_total_reconciles_with_count(self):
        result = _result(40, 1765)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"], result)
        _rc, count_stdout, _err = self._search(
            ["needle", "-c", "--lexical"], result)
        total = self._rows(stdout)[0]["completeness"]["total"]
        self.assertEqual(str(total), count_stdout.strip())

    def test_named_more_command_is_a_real_bounded_invocation(self):
        result = _result(40, 1765)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical", "--project", "agrep"], result)
        command = self._rows(stdout)[0]["completeness"]["more_command"]
        self.calls.clear()
        rerun = shlex.split(command)[1:]
        rc, _stdout, _err = self._search(rerun, result)
        # The signal names an accepted, bounded invocation with intact filters.
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0]["limit"], 80)
        self.assertEqual(self.calls[0]["project"], "agrep")

    def test_json_n80_names_n160_and_the_uncapped_keyword_form(self):
        result = _result(80, 1765)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical", "-n", "80"], result)
        block = self._rows(stdout)[0]["completeness"]
        self.assertEqual(
            block["more_command"],
            "agrep --lexical --json -n 160 -- needle")
        self.assertEqual(block["more_command_kind"], "broader-rerun")
        self.assertEqual(
            block["full_command"],
            "agrep --lexical --json -n 0 -- needle")

    def test_json_n200_names_only_the_uncapped_keyword_form(self):
        result = _result(200, 1765)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical", "-n", "200"], result)
        block = self._rows(stdout)[0]["completeness"]
        self.assertTrue(block["truncated"])
        self.assertNotIn("more_command", block)
        self.assertEqual(
            block["full_command"],
            "agrep --lexical --json -n 0 -- needle")

    def test_json_actions_preserve_every_filter_and_safety_flag(self):
        result = _result(80, 1765)
        argv = [
            "needle", "--json", "--lexical", "-n", "80",
            "--agent", "codex", "--project", "agrep",
            "--exclude-project", "scratch", "--model", "gpt-5",
            "--soft", "--no-who", "tool",
            "--chat", "session-prefix", "--since", "7d", "--until", "1d",
            "--sort", "time", "--no-self",
            "--no-meta", "--no-auto", "--color", "never",
        ]
        _rc, stdout, _err = self._search(argv, result)
        block = self._rows(stdout)[0]["completeness"]
        for field, limit in (("more_command", 160), ("full_command", 0)):
            command = shlex.split(block[field])
            self.assertEqual(
                command[-4:], ["-n", str(limit), "--", "needle"])
            for expected in (
                    "--agent=codex", "--project=agrep",
                    "--exclude-project=scratch", "--model=gpt-5",
                    "--soft", "--no-who=tool",
                    "--chat=session-prefix", "--since=7d", "--until=1d",
                    "--sort=time", "--no-self",
                    "--no-meta", "--no-auto", "--color=never", "--json"):
                self.assertIn(expected, command)
            self.calls.clear()
            rc, _stdout, _stderr = self._search(command[1:], result)
            self.assertEqual(rc, 0)
            call = self.calls[0]
            self.assertEqual(call["limit"], limit)
            self.assertEqual(call["project"], "agrep")
            self.assertEqual(call["exclude_project"], "scratch")
            self.assertEqual(call["model"], "gpt-5")
            self.assertEqual(call["chat"], "session-prefix")
            self.assertFalse(surface.speaker_filter_admits(call["who"], "tool"))

    def test_posix_control_text_uses_direct_argv_not_unsafe_prose(self):
        query = "line\nbreak"
        with mock.patch.object(search.console, "WIN", False):
            _rc, stdout, _err = self._search(
                [query, "--json", "--lexical", "--no-auto"],
                _result(40, 1765))
        rows = self._rows(stdout)
        self.assertEqual(len(rows), 41)
        block = rows[0]["completeness"]
        self.assertNotIn("more_command", block)
        self.assertNotIn("full_command", block)
        self.assertEqual(block["more_command_kind"], "broader-rerun")
        self.assertEqual(block["more_argv"][-4:], ["-n", "80", "--", query])
        self.assertEqual(block["full_argv"][-4:], ["-n", "0", "--", query])
        self.assertIn("--no-auto", block["more_argv"])
        self.assertNotIn("action_unavailable_reason", block)

    def test_windows_shell_metacharacters_use_direct_argv(self):
        query = '@issue#7 "quoted"'
        with mock.patch.object(search.console, "WIN", True):
            _rc, stdout, _err = self._search(
                [query, "--json", "--lexical", "--project", "repo#one"],
                _result(40, 1765))
        block = self._rows(stdout)[0]["completeness"]
        self.assertNotIn("more_command", block)
        self.assertNotIn("full_command", block)
        self.assertEqual(block["more_command_kind"], "broader-rerun")
        self.assertIn("--project=repo#one", block["more_argv"])
        self.assertEqual(block["more_argv"][-1], query)
        self.assertEqual(block["full_argv"][-1], query)
        self.calls.clear()
        with mock.patch.object(search.console, "WIN", True):
            rc, _stdout, _stderr = self._search(
                block["more_argv"][1:], _result(40, 1765))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0]["limit"], 80)
        self.assertEqual(self.calls[0]["project"], "repo#one")

    def test_truncated_uncapped_json_explains_why_no_action_exists(self):
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical", "-n", "0"],
            _result(40, 1765))
        block = self._rows(stdout)[0]["completeness"]
        self.assertEqual(
            block["action_unavailable_reason"],
            "this invocation already requested uncapped keyword results")
        self.assertFalse(any(key in block for key in (
            "more_command", "more_argv", "full_command", "full_argv")))

    def test_flat_stderr_line_renders_the_json_disclosure(self):
        result = _result(40, 1765)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"], result)
        block = self._rows(stdout)[0]["completeness"]
        _rc, _stdout, stderr = self._search(
            ["needle", "--flat", "--lexical", "--color", "never"], result)
        # one artifact behind both, so the piped line cannot contradict the
        # field a parser reads off the same query; only the named invocation
        # differs, because each surface names its own render
        counts, _sep, named = surface.completeness_line(block).partition(" · ")
        self.assertIn(counts, stderr)
        self.assertIn("--json", named)
        self.assertIn("--flat -n 80", stderr)

    def test_floor_total_states_its_basis_on_both_surfaces(self):
        result = _result(40, 1765, totals_exact=False)
        _rc, stdout, _err = self._search(
            ["needle", "--json", "--lexical"], result)
        block = self._rows(stdout)[0]["completeness"]
        self.assertEqual(block["total_basis"], "floor")
        _rc, count_stdout, count_stderr = self._search(
            ["needle", "-c", "--lexical"], result)
        self.assertEqual(count_stdout.strip(), ">=1765")
        self.assertIn("floor", count_stderr)

    def test_semantic_truncation_names_no_exhaustive_form(self):
        _rc, stdout, _err = self._search(
            ["needle", "--json", "-s"], _result(10, 10, semantic=True))
        block = self._rows(stdout)[0]["completeness"]
        self.assertEqual(block["more_command"], "agrep -s --json -n 80 -- needle")
        self.assertEqual(block["more_command_kind"], "broader-rerun")
        self.assertNotIn("full_command", block)
        self.assertEqual(block["no_exhaustive_form"],
                         surface.SEMANTIC_NO_EXHAUSTIVE_FORM)

    def test_non_json_surfaces_do_not_gain_full_result_metadata(self):
        result = _result(80, 1765)
        rc, flat_out, flat_err = self._search(
            ["needle", "--flat", "--lexical", "-n", "80",
             "--color", "never"], result)
        self.assertEqual(rc, 0)
        self.assertNotIn("full_command", flat_out + flat_err)
        self.assertNotIn("broader-rerun", flat_out + flat_err)
        self.assertNotIn("more: agrep", flat_err)
        rc, count_out, count_err = self._search(
            ["needle", "-c", "--lexical"], result)
        self.assertEqual((rc, count_out), (0, "1765\n"))
        self.assertNotIn("full_command", count_err)

    def test_complete_classic_flat_and_count_bytes_stay_pinned(self):
        result = _result(1, 1, truncated=False)
        expected_row = "s0000\tcodex\tagrep\t0\tuser\tneedle row 0\n"
        for surface_args in (("--classic",), ("--flat",)):
            rc, stdout, stderr = self._search([
                "needle", *surface_args, "--lexical", "--color", "never",
            ], result)
            self.assertEqual((rc, stdout), (0, expected_row))
            self.assertEqual(stderr, "history may be stale: fixture index failure\n")
        rc, stdout, stderr = self._search(
            ["needle", "-c", "--lexical"], result)
        self.assertEqual((rc, stdout), (0, "1\n"))
        self.assertEqual(stderr, "history may be stale: fixture index failure\n")

    def test_floor_recommends_count_only_where_the_pair_is_accepted(self):
        # -c --semantic is rejected at the parser, so a semantic floor must
        # not send the reader to it; keyword floors keep the recommendation
        keyword = surface.completeness_line(surface.completeness_disclosure(
            shown=5, total=40, unit="matching row", totals_exact=False,
            truncated=True))
        self.assertIn("-c counts exactly", keyword)
        semantic = surface.completeness_line(surface.completeness_disclosure(
            shown=5, total=40, unit="chat", totals_exact=False,
            truncated=True,
            no_exhaustive_form=surface.SEMANTIC_NO_EXHAUSTIVE_FORM))
        self.assertNotIn("-c", semantic)
        self.assertIn("(a floor, not the total)", semantic)
        self.assertIn(surface.SEMANTIC_NO_EXHAUSTIVE_FORM, semantic)

    def test_count_by_tier_discloses_a_short_exhaustive_count(self):
        _rc, stdout, stderr = self._search(
            ["needle", "--count-by-tier", "--lexical"], _result(40, 1765))
        self.assertIn("total=40", stdout)
        self.assertIn("40 of 1765", stderr)

    def test_more_on_a_machine_surface_names_a_bounded_spelling(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as caught:
            search.main(["--more", "HANDLE", "--json"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--json -n 80", stderr.getvalue())


class ChatsCompleteness(unittest.TestCase):
    """`agrep chats --json` truncates by the same silent cap (default 20)."""

    def setUp(self) -> None:
        import explore
        # This fixture writes into the SHARED sandbox: restore exact bytes AND
        # mtimes (the generation proof binds both), or every module after
        # this one inherits a torn generation - a bisected order red.
        import os as _os
        for path in (common.DATA_DIR / "sessions.jsonl", common.MESSAGES_PATH):
            if path.exists():
                stat = path.stat()
                self.addCleanup(
                    lambda p=path, b=path.read_bytes(),
                    ns=(stat.st_atime_ns, stat.st_mtime_ns):
                    (p.write_bytes(b), _os.utime(p, ns=ns)))
            else:
                self.addCleanup(lambda p=path: p.unlink(missing_ok=True))
        self.addCleanup(explore._freshen)
        self._write_sessions(5)
        common.MESSAGES_PATH.touch()
        explore._freshen()

    def _write_sessions(self, count: int) -> None:
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as handle:
            for index in range(count):
                handle.write(json.dumps({
                    "session": f"0199aaaa-1111-7000-8000-{index:012d}",
                    "agent": "codex", "project": "/home/u/webapp",
                    "n": 3, "first_ts": 1000 + index, "last_ts": 9000 + index,
                    "first_text": "fix the login race", "parent": "",
                }) + "\n")
        import explore
        explore._freshen()

    def _chats(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                indexd_runtime, "ensure_index", return_value=True), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.chats_main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_truncated_chats_json_carries_completeness(self):
        _rc, stdout, _err = self._chats(["--json", "-n", "2"])
        lines = [json.loads(line) for line in stdout.splitlines() if line]
        # OUTPUT_CONTRACTS: chats JSON uses the one-envelope page shape
        self.assertEqual(lines[0]["kind"], "agrep-meta")
        self.assertEqual(len(lines[1:]), 2)
        expected = surface.completeness_disclosure(
            shown=2, total=5, unit="matching chat", totals_exact=True,
            truncated=True, more_command="agrep chats --json -n 80")
        self.assertEqual(lines[0]["completeness"], expected)
        for row in lines[1:]:
            self.assertNotIn("completeness", row)

    def test_chats_more_command_never_shrinks_an_explicit_page(self):
        self._write_sessions(100)
        _rc, stdout, _err = self._chats(["--json", "-n", "90"])
        lines = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual(lines[0]["kind"], "agrep-meta")
        self.assertEqual(len(lines[1:]), 90)
        self.assertEqual(
            lines[0]["completeness"]["more_command"],
            "agrep chats --json -n 360")

    def test_empty_chats_json_still_emits_a_meta_record(self):
        _rc, stdout, _err = self._chats(["--json", "no-such-chat"])
        rows = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual([row["kind"] for row in rows], ["agrep-meta"])
        self.assertTrue(rows[0]["completeness"]["truncated"])


class LargerResultCommandTests(unittest.TestCase):
    """The machine-named larger page keeps the output surface it continues:
    dropping -l beside --json returned message rows where the page counted
    chats (unit 'chat', total 7)."""

    @staticmethod
    def _args(**over):
        import argparse
        base = dict(regex=False, word=False, lexical=True, agent=None,
                    project=None, exclude_project=None, model=None, who=None,
                    no_who=None, chat=None, since=None, until=None,
                    model_soft=False, no_meta=False, sort="score",
                    include_self=False, force_no_self=False,
                    all_side_chats=False, strict_semantic=False, no_auto=False,
                    color="auto", max=3, json=False, flat=False, chats=False)
        base.update(over)
        return argparse.Namespace(**base)

    def _command(self, **over) -> list[str]:
        command = search._larger_result_command(
            self._args(**over), "pelican", semantic=False)
        self.assertIsNotNone(command)
        return shlex.split(command)

    def test_l_beside_json_keeps_both_surfaces(self) -> None:
        argv = self._command(chats=True, json=True)
        self.assertIn("-l", argv)
        self.assertIn("--json", argv)

    def test_each_surface_alone_is_unchanged(self) -> None:
        self.assertIn("-l", self._command(chats=True))
        self.assertNotIn("--json", self._command(chats=True))
        self.assertIn("--json", self._command(json=True))
        self.assertNotIn("-l", self._command(json=True))
        self.assertIn("--flat", self._command(flat=True))
        self.assertIn("--classic", self._command())


if __name__ == "__main__":
    unittest.main()
