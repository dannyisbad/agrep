"""An empty filter value is a usage error, never a silently corpus-wide answer.

`--agent ""` once printed the byte-identical count to an unfiltered run while
`--agent bogus` was a hard exit-2: an agent templating `--agent "$X"` off a
variable that came back empty got the whole corpus and reported it as scoped.
Every case here is driven off the one owned artifact, surface_policy's
FILTER_SPECS, so the refusal and its wording cannot drift apart, and a filter
flag added without a row fails here rather than shipping the hole again.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import audit  # noqa: E402
import cli  # noqa: E402
import common  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


# Value-taking but not filters: paginator handles address one prior page, not
# a narrowed query. Out of the FILTER vocabulary, still honored-or-refused -
# reading "" as absent once ran an unpaginated search in silence.
NON_FILTER_VALUE_FLAGS = frozenset({"--more", "--deeper"})
BLANKS = ("", "   ")
ENTRIES = (
    ("search", search.main, ["deadlock"]),
    ("chats", search.chats_main, []),
    ("recall", recall.main, ["deadlock"]),
    ("audit", audit.main, []),
)


def _built_parser(entry, argv: list[str]) -> argparse.ArgumentParser:
    """The parser a main() builds, captured before it consumes argv."""
    captured: list[argparse.ArgumentParser] = []
    original = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        captured.append(self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        with contextlib.suppress(SystemExit):
            entry(argv)
    finally:
        argparse.ArgumentParser.parse_args = original
    if not captured:
        raise AssertionError("entry point never built a parser")
    return captured[0]


def _flags(entry, argv: list[str]) -> set[str]:
    return {option for action in _built_parser(entry, argv)._actions
            for option in action.option_strings}


def _run(entry, argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            rc = entry(argv)
        except SystemExit as exc:
            rc = 2 if exc.code is None else int(exc.code)
    return rc, err.getvalue()


class ParserRefusalTests(unittest.TestCase):
    def test_every_spec_row_exits_2_naming_the_flag_and_its_vocabulary(self) -> None:
        for name, entry, base in ENTRIES:
            flags = _flags(entry, base)
            for spec in surface.FILTER_SPECS:
                if spec.flag not in flags:
                    continue
                for blank in BLANKS:
                    with self.subTest(entry=name, flag=spec.flag, value=blank):
                        rc, err = _run(entry, [*base, spec.flag, blank])
                        self.assertEqual(rc, 2, err)
                        self.assertIn(spec.flag, err)
                        self.assertIn(spec.vocabulary(), err)

    def test_the_equals_form_is_the_same_accident(self) -> None:
        for spec in surface.FILTER_SPECS:
            with self.subTest(flag=spec.flag):
                rc, err = _run(search.main, ["deadlock", f"{spec.flag}="])
                self.assertEqual(rc, 2, err)
                self.assertIn(spec.flag, err)

    def test_a_scoped_run_still_reaches_the_engine(self) -> None:
        # the refusal must not swallow a legitimate value: these get past parsing
        # and stop at the absent index (--no-auto: never ingest from a test), so
        # any "needs a value" here would be the seam firing on a real filter
        for argv in (["deadlock", "--no-auto", "--agent", "claude"],
                     ["deadlock", "--no-auto", "--since", "7d"]):
            with self.subTest(argv=argv):
                _rc, err = _run(search.main, argv)
                self.assertNotIn("needs a value", err)


class FilterSpecCoverageTests(unittest.TestCase):
    def test_every_value_taking_filter_flag_has_a_spec_row(self) -> None:
        for name, entry, base in ENTRIES:
            for action in _built_parser(entry, base)._actions:
                if not action.option_strings or action.nargs == 0:
                    continue
                if action.choices is not None or action.type is int:
                    continue
                primary = action.option_strings[0]
                if primary in NON_FILTER_VALUE_FLAGS:
                    continue
                with self.subTest(entry=name, flag=primary):
                    self.assertIn(
                        primary, surface.FILTERS_BY_FLAG,
                        f"{primary} narrows {name} results but has no "
                        "FILTER_SPECS row, so an empty value would silently "
                        "widen the run")

    def test_no_spec_row_is_stale(self) -> None:
        live = set().union(*(_flags(entry, base) for _n, entry, base in ENTRIES))
        for spec in surface.FILTER_SPECS:
            with self.subTest(flag=spec.flag):
                self.assertIn(spec.flag, live)

    def test_spec_dests_match_the_parsers(self) -> None:
        for name, entry, base in ENTRIES:
            by_flag = {option: action.dest
                       for action in _built_parser(entry, base)._actions
                       for option in action.option_strings}
            for spec in surface.FILTER_SPECS:
                if spec.flag not in by_flag:
                    continue
                with self.subTest(entry=name, flag=spec.flag):
                    # a drifted dest would read None and never fire the refusal
                    self.assertEqual(by_flag[spec.flag], spec.dest)


class NonFilterValueFlagTests(unittest.TestCase):
    """Exempt from the filter vocabulary is not exempt from honored-or-refused."""

    def test_each_allowlisted_flag_still_refuses_a_blank_value(self) -> None:
        for flag in sorted(NON_FILTER_VALUE_FLAGS):
            for blank in BLANKS:
                with self.subTest(flag=flag, value=blank):
                    rc, err = _run(search.main, [flag, blank])
                    self.assertEqual(rc, 2, err)

    def test_a_blank_handle_never_falls_through_to_a_fresh_search(self) -> None:
        for flag in sorted(NON_FILTER_VALUE_FLAGS):
            with self.subTest(flag=flag):
                rc, err = _run(search.main, ["deadlock", flag, ""])
                self.assertEqual(rc, 2, err)
                self.assertIn(flag, err)


class EmptyFilterNoticeTests(unittest.TestCase):
    def test_notice_names_flag_vocabulary_and_the_widening(self) -> None:
        for spec in surface.FILTER_SPECS:
            notice = surface.empty_filter_notice(spec)
            with self.subTest(flag=spec.flag):
                self.assertIn(spec.flag, notice)
                self.assertIn(spec.vocabulary(), notice)
                self.assertIn(spec.empty_effect, notice)
                self.assertTrue(spec.vocabulary().strip(),
                                "a filter with an empty domain would have a "
                                "legitimate empty value; this one claims none")

    def test_blank_values_are_refused_and_real_ones_pass(self) -> None:
        for spec in surface.FILTER_SPECS:
            for blank in ("", "   "):
                with self.subTest(flag=spec.flag, value=blank):
                    self.assertEqual(
                        surface.filter_value_error(
                            argparse.Namespace(**{spec.dest: blank})),
                        surface.empty_filter_notice(spec))
            with self.subTest(flag=spec.flag, value="x"):
                self.assertIsNone(surface.filter_value_error(
                    argparse.Namespace(**{spec.dest: "x"})))

    def test_absent_and_non_string_values_are_left_alone(self) -> None:
        self.assertIsNone(surface.filter_value_error(argparse.Namespace()))
        self.assertIsNone(surface.filter_value_error(
            argparse.Namespace(**{spec.dest: None for spec in surface.FILTER_SPECS})))

    def test_flags_are_unique_and_indexed(self) -> None:
        flags = [spec.flag for spec in surface.FILTER_SPECS]
        self.assertCountEqual(flags, set(flags))
        self.assertEqual(set(surface.FILTERS_BY_FLAG), set(flags))


class AgentPrescanTests(unittest.TestCase):
    """cli.py screens --agent before dispatch; both bad shapes exit 2 there too."""

    def test_empty_agent_prescan_speaks_the_owned_sentence(self) -> None:
        expected = surface.empty_filter_notice(
            surface.FILTERS_BY_FLAG["--agent"])
        for argv in (["q", "--agent", ""], ["q", "--agent="],
                     ["q", "--agent", "  "]):
            with self.subTest(argv=argv):
                self.assertEqual(common.agent_filter_error(argv), expected)

    def test_unknown_agent_prescan_still_names_the_vocabulary(self) -> None:
        err = common.agent_filter_error(["q", "--agent", "bogus"])
        self.assertIsNotNone(err)
        self.assertIn("bogus", err)
        for agent in common.KNOWN_AGENTS:
            self.assertIn(agent, err)

    def test_a_real_agent_passes_the_prescan(self) -> None:
        for agent in common.KNOWN_AGENTS:
            with self.subTest(agent=agent):
                self.assertIsNone(
                    common.agent_filter_error(["q", "--agent", agent]))

    def test_option_terminator_ends_the_agent_prescan(self) -> None:
        self.assertIsNone(common.agent_filter_error(
            ["--", "--agent=bogus", "--agent", ""]))

    def test_chats_screens_agent_like_search_does(self) -> None:
        # `chats --agent bogus` used to print "0 of 0 matching chats": a
        # confident zero from a filter the tool never accepted
        for value in ("bogus", ""):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.cmd_chats(argparse.Namespace(
                        rest=["--agent", value, "--no-auto"]))
                self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
