"""A supplied option is honored or refused, never dropped.

`agrep -c deadlock --json` printed 1898 as plain text; `-c -l` dropped -l;
`--soft` without `--model` did nothing; `resume -l zzzznope` listed recent
chats and never mentioned the id; and `-c deadlock --more ""` ran page one
again because the empty handle was falsy and the incompatibility guard reads
truthiness. Every case ran a different query than the caller asked for and
said nothing about it.

The refusals are driven off surface_policy's OPTION_GATES so emitter and
checker cannot drift, and the partition below covers the mechanism rather
than the reported six: every option a whole-run surface accepts is classified
here as refused, honored, or inert-with-a-reason, so a new option added to any
of these parsers fails this suite until someone states which it is.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import indexd_runtime  # noqa: E402
import recall  # noqa: E402
import resume  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402

# "Corpus-free" must mean write-free: a real first-run ingest into the SHARED
# sandbox outlives this module and steals the engine choice from later
# fixtures (a bisected order red). The parser seam never reaches this.
_no_ingest = None


def setUpModule() -> None:
    global _no_ingest, _no_first_run
    from unittest import mock
    _no_ingest = mock.patch.object(
        indexd_runtime, "ensure_index", return_value=True)
    _no_ingest.start()
    # Accepted argv executes past the parser; unstubbed, the first accepted
    # machine run streams a REAL rust ingest into the shared sandbox and
    # later discovery modules read that world instead of their fixtures.
    _no_first_run = mock.patch.object(
        search, "_stream_first_run", return_value=None)
    _no_first_run.start()


def tearDownModule() -> None:
    _no_first_run.stop()
    _no_ingest.stop()


class _Refused(Exception):
    """One parser refusal, captured where it is raised."""


def _run(entry, argv: list[str]) -> str | None:
    """The refusal argv earned, or None if the parser accepted it.

    Instrumenting ap.error is what keeps this corpus-free: past parsing every
    run stops at the empty sandbox, and a no-index exit 2 is indistinguishable
    from a usage exit 2."""
    def refuse(_self, message):
        raise _Refused(message)

    original = surface.ArgumentParser.error
    surface.ArgumentParser.error = refuse
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                entry(argv)
            except _Refused as exc:
                return str(exc)
            except SystemExit:
                return None
            except Exception:  # noqa: BLE001 -- the empty sandbox, not a refusal
                return None
    finally:
        surface.ArgumentParser.error = original
    return None


def _exit_code(entry, argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            rc = entry(argv)
        except SystemExit as exc:
            rc = 2 if exc.code is None else int(exc.code)
    return rc, err.getvalue()


def _parser(entry, argv: list[str]) -> argparse.ArgumentParser:
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
    return captured[0]


# what each option needs on the command line to be supplied at all; a value
# flag with no value is an argparse error, and --soft with no --model is its
# own refusal, so neither would exercise the surface under test
ARGV = {
    "-n": ["-n", "3"],
    "--sort": ["--sort", "time"],
    "--color": ["--color", "never"],
    "--agent": ["--agent", "claude"],
    "--project": ["--project", "web"],
    "--exclude-project": ["--exclude-project", "bench"],
    "--model": ["--model", "gpt-5"],
    "--soft": ["--model", "gpt-5", "--soft"],
    "--who": ["--who", "user"],
    "--no-who": ["--no-who", "tool"],
    "--chat": ["--chat", "11111111"],
    "--since": ["--since", "7d"],
    "--until": ["--until", "1d"],
    "--more": ["--more", "m." + "a" * 24],
    "--hits": ["--hits", "2"],
    "--budget": ["--budget", "4000"],
    "-C": ["-C", "3"],
    "id": ["zzzznope"],
}


class Surface(NamedTuple):
    """One surface that renders a whole run, and the partition of everything
    its parser accepts alongside it. `inert` is the named allowlist: an option
    the surface cannot honor but whose loss costs the caller nothing, each with
    the reason it is not a refusal. `elsewhere` maps the options an earlier,
    surface-independent rule already refuses to the flag that refusal names."""
    entry: object
    base: list[str]
    flag: str
    refused: frozenset[str]
    inert: dict[str, str]
    honored: frozenset[str]
    elsewhere: dict[str, str] = {}


_SEARCH_REFUSED = frozenset({
    "-l", "--json", "--flat", "--classic", "-n", "--sort", "--more",
    "--no-meta", "--coverage", "-s",
})
_SEMANTIC_ONLY = {"--strict-semantic": "--semantic",
                  "--all-side-chats": "--semantic"}
_SEARCH_INERT = {
    "-i": "a documented no-op: search is always case-insensitive",
    "--self": "counts already include the calling family; the flag asks for "
              "the state the surface is in",
    "--lexical": "disables compact meaning rows, and a count renders none",
    "--color": "one number has nothing to style",
}
_SEARCH_HONORED = frozenset({
    "-E", "-w", "--agent", "--project", "--exclude-project", "--model",
    "--soft", "--who", "--no-who", "--chat", "--since", "--until",
    "--no-self", "--no-auto",
})
_ROW_SURFACE_HONORED = frozenset({
    "-E", "-w", "-n", "-s", "--sort", "--lexical", "--agent", "--project",
    "--exclude-project", "--model", "--soft", "--who", "--no-who", "--chat",
    "--since", "--until", "--no-self", "--no-meta", "--no-auto", "--color",
})
# --more's generic refusal names the option class, not the base flag
_ROW_SURFACE_ELSEWHERE = {**_SEMANTIC_ONLY, "--more": "output options"}
SURFACES = (
    Surface(search.main, ["deadlock", "-c"], "-c",
            _SEARCH_REFUSED | {"--count-by-tier"}, _SEARCH_INERT,
            _SEARCH_HONORED, _SEMANTIC_ONLY),
    Surface(search.main, ["deadlock", "--count-by-tier"], "--count-by-tier",
            _SEARCH_REFUSED | {"-c", "-E", "-w"}, _SEARCH_INERT,
            _SEARCH_HONORED - {"-E", "-w"}, _SEMANTIC_ONLY),
    Surface(search.main, ["deadlock", "-l"], "-l",
            frozenset({"-c", "--count-by-tier", "--flat", "--classic",
                       "--coverage"}),
            {"-i": "a documented no-op: search is always case-insensitive"},
            _ROW_SURFACE_HONORED | {"--json", "--self"},
            _ROW_SURFACE_ELSEWHERE),
    Surface(search.main, ["deadlock", "--flat"], "--flat",
            frozenset({"-c", "--count-by-tier", "--json", "-l", "--classic",
                       "--coverage", "--more"}),
            {"-i": "a documented no-op: search is always case-insensitive",
             "--self": "machine rows already include the calling family; "
                       "the flag asks for the state the surface is in"},
            _ROW_SURFACE_HONORED, _SEMANTIC_ONLY),
    Surface(recall.main, ["deadlock", "--probe"], "--probe",
            frozenset({"--hits", "--json"}), {},
            frozenset({"--budget", "-C", "-s", "--lexical", "--self",
                       "--no-self", "--all-side-chats", "--agent", "--project",
                       "--model", "--soft", "--who", "--no-who", "--no-meta",
                       "--chat", "--since", "--until", "--no-auto", "--color"})),
    Surface(resume.main, ["-l"], "-l",
            frozenset({"id"}), {}, frozenset({"-n", "--color"})),
)


def _option_flags(parser: argparse.ArgumentParser) -> set[str]:
    """Every option a caller can supply: the primary spelling, plus positionals
    by dest. Help and the internal replay flags are excluded - `--hybrid` and
    `--deeper` carry SUPPRESS help because search builds them for itself."""
    flags = set()
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.help == argparse.SUPPRESS:
            continue
        flags.add(action.option_strings[0] if action.option_strings
                  else action.dest)
    return flags


class SurfacePartitionTests(unittest.TestCase):
    def test_every_option_a_surface_accepts_is_classified(self) -> None:
        for face in SURFACES:
            parser = _parser(face.entry, face.base)
            classified = (face.refused | set(face.inert) | face.honored
                          | set(face.elsewhere))
            with self.subTest(surface=face.flag):
                self.assertEqual(
                    _option_flags(parser) - {face.flag, "pattern", "query"},
                    classified,
                    f"an option {face.flag} accepts is neither refused, "
                    "honored, nor named inert with a reason - it would be "
                    "supplied and dropped")
                self.assertFalse(
                    face.refused & set(face.inert) or face.honored & face.refused
                    or face.honored & set(face.inert),
                    "an option cannot be two of refused/inert/honored")

    def test_every_refused_option_is_refused_and_names_the_surface(self) -> None:
        for face in SURFACES:
            by_flag = {action.option_strings[0] if action.option_strings
                       else action.dest: action for action in
                       _parser(face.entry, face.base)._actions}
            named = {flag: face.flag for flag in face.refused}
            named.update(face.elsewhere)
            for flag, other in sorted(named.items()):
                with self.subTest(surface=face.flag, flag=flag):
                    message = _run(face.entry,
                                   [*face.base, *ARGV.get(flag, [flag])])
                    self.assertIsNotNone(
                        message, f"{flag} is accepted alongside {face.flag} "
                                 "and then dropped")
                    spellings = by_flag[flag].option_strings or [flag]
                    self.assertTrue(
                        any(spelling in message for spelling in spellings),
                        f"{message!r} never names {flag}")
                    self.assertIn(other, message)

    def test_every_honored_and_inert_option_is_accepted(self) -> None:
        # the other half of the partition: a classification that quietly became
        # a refusal is drift too, and the caller would read it as a usage bug
        for face in SURFACES:
            for flag in sorted(face.honored | set(face.inert)):
                with self.subTest(surface=face.flag, flag=flag):
                    self.assertIsNone(
                        _run(face.entry, [*face.base, *ARGV.get(flag, [flag])]))

    def test_every_inert_entry_states_a_reason(self) -> None:
        for face in SURFACES:
            for flag, reason in face.inert.items():
                with self.subTest(surface=face.flag, flag=flag):
                    self.assertTrue(reason.strip())


class OptionGateTests(unittest.TestCase):
    """The gates are the artifact both halves read; these pin the artifact."""

    ALL = (surface.SEARCH_OPTION_GATES + surface.RECALL_OPTION_GATES
           + surface.RESUME_OPTION_GATES)

    def test_presence_is_not_truthiness(self) -> None:
        # the hole that let `--more ""` through: every falsy-but-supplied value
        for ref, value in ((surface.MODEL_OPTION, ""),
                           (surface.OptionRef("-n", "max", None), 0),
                           (surface.COUNT_OPTION, True)):
            with self.subTest(flag=ref.flag, value=value):
                self.assertTrue(
                    ref.supplied(argparse.Namespace(**{ref.dest: value})))
        self.assertFalse(surface.MODEL_OPTION.supplied(
            argparse.Namespace(model=None)))
        self.assertFalse(surface.COUNT_OPTION.supplied(
            argparse.Namespace(count=False)))

    def test_presence_parser_distinguishes_an_explicit_default(self) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("pattern", nargs="*")
        parser.add_argument("-c", "--count", action="store_true")
        parser.add_argument(
            "--sort", choices=("score", "time"), default="score")
        omitted = surface.parse_args_with_presence(
            parser, ["needle", "-c"])
        explicit = surface.parse_args_with_presence(
            parser, ["needle", "-c", "--sort=score"])
        ref = surface.OptionRef("--sort", "sort", "score")
        self.assertFalse(ref.supplied(omitted))
        self.assertTrue(ref.supplied(explicit))
        self.assertEqual(explicit.sort, omitted.sort)

    def test_presence_parser_honors_the_option_terminator(self) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("pattern", nargs="*")
        parser.add_argument("--sort", default="score")
        parsed = surface.parse_args_with_presence(
            parser, ["--", "--sort", "score"])
        self.assertNotIn("sort", parsed._agrep_supplied_options)
        self.assertEqual(parsed.pattern, ["--sort", "score"])

    def test_presence_parser_restores_defaults_after_a_refusal(self) -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--sort", choices=("score", "time"),
                            default="score")
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            surface.parse_args_with_presence(parser, ["--sort", "bogus"])
        parsed = surface.parse_args_with_presence(parser, [])
        self.assertEqual(parsed.sort, "score")
        self.assertNotIn("sort", parsed._agrep_supplied_options)

    def test_absent_values_match_the_parsers(self) -> None:
        entries = ((search.main, ["deadlock"], surface.SEARCH_OPTION_GATES),
                   (recall.main, ["deadlock"], surface.RECALL_OPTION_GATES),
                   (resume.main, [], surface.RESUME_OPTION_GATES))
        for entry, base, gates in entries:
            defaults = {action.dest: action.default
                        for action in _parser(entry, base)._actions}
            for gate in gates:
                for ref in (gate.option, *gate.blocked_by, *gate.needs):
                    with self.subTest(entry=entry.__module__, flag=ref.flag):
                        # a drifted default reads as permanently supplied
                        self.assertIn(ref.dest, defaults)
                        self.assertEqual(ref.absent, defaults[ref.dest])

    def test_every_blocker_states_what_it_renders(self) -> None:
        for gate in self.ALL:
            for blocker in gate.blocked_by:
                with self.subTest(flag=blocker.flag):
                    self.assertTrue(blocker.renders.strip(),
                                    "the refusal would name a flag with no "
                                    "account of why it swallows the other")

    def test_the_error_names_both_flags(self) -> None:
        for gate in self.ALL:
            supplied = {gate.option.dest: not gate.option.absent
                        if isinstance(gate.option.absent, bool) else "x"}
            for blocker in gate.blocked_by:
                args = argparse.Namespace(
                    **supplied,
                    **{blocker.dest: True if isinstance(blocker.absent, bool)
                       else "x"})
                message = surface.option_gate_error(args, [gate])
                with self.subTest(flag=gate.option.flag, blocker=blocker.flag):
                    self.assertIn(gate.option.flag, message)
                    self.assertIn(blocker.flag, message)
            for needed in gate.needs:
                args = argparse.Namespace(**supplied,
                                          **{needed.dest: needed.absent})
                message = surface.option_gate_error(args, [gate])
                with self.subTest(flag=gate.option.flag, needs=needed.flag):
                    self.assertIn(gate.option.flag, message)
                    self.assertIn(needed.flag, message)

    def test_a_run_that_supplies_neither_side_is_untouched(self) -> None:
        for gate in self.ALL:
            args = argparse.Namespace(**{
                ref.dest: ref.absent
                for ref in (gate.option, *gate.blocked_by, *gate.needs)})
            with self.subTest(flag=gate.option.flag):
                self.assertIsNone(surface.option_gate_error(args, [gate]))


class ReportedCaseTests(unittest.TestCase):
    """The six verbatim seat cases: exit 2, and both flags in the sentence."""

    CASES = (
        (search.main, ["-c", "deadlock", "--more", ""], ("-c", "--more")),
        (search.main, ["deadlock", "--more", ""], ("--more",)),
        (search.main, ["-c", "-l", "deadlock"], ("-c", "-l")),
        (search.main, ["-c", "deadlock", "--json"], ("-c", "--json")),
        (search.main, ["-l", "--flat", "deadlock"], ("-l", "--flat")),
        (search.main, ["deadlock", "--classic", "--json"],
         ("--classic", "--json")),
        (search.main, ["deadlock", "--classic", "--flat"],
         ("--classic", "--flat")),
        (search.main, ["deadlock", "--classic", "-l"], ("--classic", "-l")),
        (search.main, ["deadlock", "--soft"], ("--soft", "--model")),
        (resume.main, ["-l", "zzzznope"], ("-l", "id")),
    )

    def test_each_case_exits_2_naming_both_flags(self) -> None:
        for entry, argv, named in self.CASES:
            with self.subTest(argv=argv):
                rc, err = _exit_code(entry, argv)
                self.assertEqual(rc, 2, err)
                for flag in named:
                    self.assertIn(flag, err)

    def test_an_empty_more_is_a_supplied_more(self) -> None:
        # alone it reaches the handle parser, which refuses it; the one thing
        # it may never do is fall through to a fresh unpaginated search
        rc, err = _exit_code(search.main, ["--more", ""])
        self.assertEqual(rc, 2)
        self.assertNotIn("no index", err)

    def test_an_explicit_default_value_is_still_supplied(self) -> None:
        rc, err = _exit_code(
            search.main, ["deadlock", "-c", "--sort", "score"])
        self.assertEqual(rc, 2)
        self.assertIn("-c", err)
        self.assertIn("--sort", err)

    def test_the_other_blank_values_that_widened_a_run(self) -> None:
        # same hole, outside the filter vocabulary: a blank --session followed
        # every chat, a blank needle matched every archived file, and a blank
        # --to restored to the path the caller named an alternative to
        import archive
        import tail
        for entry, argv, flag in ((tail.main, ["--session", "", "--snapshot"],
                                   "--session"),
                                  (archive.restore_main, [""], "needle"),
                                  (archive.restore_main, ["abcdefgh", "--to", ""],
                                   "--to")):
            with self.subTest(argv=argv):
                rc, err = _exit_code(entry, argv)
                self.assertEqual(rc, 2, err)
                self.assertIn(flag, err)
                self.assertIn("needs a value", err)

    def test_blank_positional_scopes_are_refused(self) -> None:
        def pack(argv):
            return recall.main(argv, prog="pack")

        for entry, argv, noun in (
                (search.chats_main, [""], "pattern"),
                (pack, [""], "query"),
                (pack, ["deadlock", "   "], "query")):
            with self.subTest(argv=argv):
                rc, err = _exit_code(entry, argv)
                self.assertEqual(rc, 2, err)
                self.assertIn(noun, err)
                self.assertIn("must not be empty", err)

    def test_long_option_abbreviations_are_refused(self) -> None:
        for entry, argv, flag in (
                (search.main, ["needle", "--sor", "score"], "--sor"),
                (recall.main, ["needle", "--bud", "4000"], "--bud"),
                (resume.main, ["--col", "never"], "--col")):
            with self.subTest(argv=argv):
                rc, err = _exit_code(entry, argv)
                self.assertEqual(rc, 2, err)
                self.assertIn(flag, err)

    def test_every_public_leaf_parser_disables_abbreviations(self) -> None:
        import archive
        import around
        import audit
        import livetui
        import tail
        import teach

        entries = (
            (search.main, ["needle"]),
            (search.chats_main, []),
            (recall.main, ["needle"]),
            (resume.main, []),
            (around.main, ["session", "0"]),
            (audit.main, []),
            (archive._main, []),
            (archive.restore_main, ["abcdefgh"]),
            (livetui.main, []),
            (tail.main, []),
            (teach.main, []),
        )
        for entry, argv in entries:
            with self.subTest(entry=f"{entry.__module__}.{entry.__name__}"):
                self.assertFalse(_parser(entry, argv).allow_abbrev)

    def test_a_supplied_pagination_handle_still_paginates(self) -> None:
        # the refusals must not swallow the legitimate call: a well-formed
        # handle gets past every guard and stops at the absent page
        rc, _err = _exit_code(search.main, ["--more", "m." + "a" * 24])
        self.assertEqual(rc, 2)


# -- machine surfaces (--json / --flat / -l): the same partition, every
# refusal live (docket S14/M9). -l beside --json is HONORED - one json row
# per chat (selftest pins the shape) - a combined surface, not a drop.
_MACHINE_HONORED = frozenset({
    "-E", "-w", "--agent", "--project", "--exclude-project", "--model",
    "--soft", "--who", "--no-who", "--chat", "--since", "--until",
    "--no-self", "--no-auto", "-n", "--sort", "-s", "--no-meta",
})
_MACHINE_SELF_INERT = ("machine rows already include the calling family and "
                       "carry a per-row self field; the flag asks for the "
                       "state the surface is in")
MACHINE_SURFACES = (
    Surface(search.main, ["deadlock", "--json"], "--json",
            frozenset({"-c", "--count-by-tier", "--flat", "--coverage",
                       "--more", "--classic"}),
            {"-i": _SEARCH_INERT["-i"],
             "--self": _MACHINE_SELF_INERT,
             "--color": "machine json rows are never styled",
             "--lexical": "the json lane never auto-merges meaning rows, so "
                          "there is nothing for --lexical to disable"},
            _MACHINE_HONORED | {"-l"}, _SEMANTIC_ONLY),
    Surface(search.main, ["deadlock", "--flat"], "--flat",
            frozenset({"-c", "--count-by-tier", "--json", "--coverage",
                       "--more", "-l", "--classic"}),
            {"-i": _SEARCH_INERT["-i"], "--self": _MACHINE_SELF_INERT},
            _MACHINE_HONORED | {"--color", "--lexical"}, _SEMANTIC_ONLY),
    Surface(search.main, ["deadlock", "-l"], "-l",
            frozenset({"-c", "--count-by-tier", "--coverage",
                       "--flat", "--classic"}),
            {"-i": _SEARCH_INERT["-i"]},
            _MACHINE_HONORED | {"--color", "--lexical", "--self", "--json"},
            {**_SEMANTIC_ONLY, "--more": "output options"}),
)


class MachineSurfacePartitionTests(unittest.TestCase):
    """SurfacePartitionTests for the machine renders, pending pairs split out."""

    def test_every_option_a_machine_surface_accepts_is_classified(self) -> None:
        for face in MACHINE_SURFACES:
            parser = _parser(face.entry, face.base)
            classified = (face.refused | set(face.inert) | face.honored
                          | set(face.elsewhere))
            with self.subTest(surface=face.flag):
                self.assertEqual(
                    _option_flags(parser) - {face.flag, "pattern", "query"},
                    classified,
                    f"an option {face.flag} accepts is neither refused, "
                    "honored, nor named inert with a reason - it would be "
                    "supplied and dropped")
                self.assertFalse(
                    face.refused & set(face.inert) or face.honored & face.refused
                    or face.honored & set(face.inert),
                    "an option cannot be two of refused/inert/honored")

    def test_every_live_machine_refusal_names_the_surface(self) -> None:
        for face in MACHINE_SURFACES:
            by_flag = {action.option_strings[0] if action.option_strings
                       else action.dest: action for action in
                       _parser(face.entry, face.base)._actions}
            named = {flag: face.flag for flag in face.refused}
            named.update(face.elsewhere)
            for flag, other in sorted(named.items()):
                with self.subTest(surface=face.flag, flag=flag):
                    message = _run(face.entry,
                                   [*face.base, *ARGV.get(flag, [flag])])
                    self.assertIsNotNone(
                        message, f"{flag} is accepted alongside {face.flag} "
                                 "and then dropped")
                    spellings = by_flag[flag].option_strings or [flag]
                    self.assertTrue(
                        any(spelling in message for spelling in spellings),
                        f"{message!r} never names {flag}")
                    self.assertIn(other, message)

    def test_every_machine_honored_and_inert_option_is_accepted(self) -> None:
        for face in MACHINE_SURFACES:
            for flag in sorted(face.honored | set(face.inert)):
                with self.subTest(surface=face.flag, flag=flag):
                    self.assertIsNone(
                        _run(face.entry, [*face.base, *ARGV.get(flag, [flag])]))

    def test_every_machine_inert_entry_states_a_reason(self) -> None:
        for face in MACHINE_SURFACES:
            for flag, reason in face.inert.items():
                with self.subTest(surface=face.flag, flag=flag):
                    self.assertTrue(reason.strip())

    # Docket S14/M9 closed: refused pairs are exercised live above; the
    # honored -l/--json pair (per-chat json rows) is pinned in both orders.
    def test_l_beside_json_is_honored_in_both_orders(self) -> None:
        for base in (["deadlock", "--json", "-l"], ["deadlock", "-l", "--json"]):
            with self.subTest(argv=base):
                self.assertIsNone(_run(search.main, base))


class AroundOptionGateTests(unittest.TestCase):
    """around's silently one-sided pairs: --full swallowed an explicit
    --max-chars (3,570-char lines under --max-chars 60), and --no-tools
    swallowed an explicit --tool-output. Both are refusals now."""

    def test_each_pair_is_refused_naming_both_flags(self) -> None:
        import around
        for argv, named in (
                (["11111111", "0", "--full", "--max-chars", "60"],
                 ("--max-chars", "--full")),
                (["11111111", "0", "--no-tools", "--tool-output", "200"],
                 ("--tool-output", "--no-tools"))):
            with self.subTest(argv=argv):
                rc, err = _exit_code(around.main, argv)
                self.assertEqual(rc, 2, err)
                for flag in named:
                    self.assertIn(flag, err)

    def test_each_side_alone_is_still_accepted(self) -> None:
        import around
        for argv in (["11111111", "0", "--max-chars", "60"],
                     ["11111111", "0", "--full"],
                     ["11111111", "0", "--tool-output", "200"],
                     ["11111111", "0", "--no-tools"]):
            with self.subTest(argv=argv):
                self.assertIsNone(_run(around.main, argv))

    def test_around_gates_match_the_parser_and_state_renders(self) -> None:
        import around
        defaults = {action.dest: action.default
                    for action in _parser(around.main, ["s", "0"])._actions}
        for gate in surface.AROUND_OPTION_GATES:
            for ref in (gate.option, *gate.blocked_by):
                with self.subTest(flag=ref.flag):
                    self.assertIn(ref.dest, defaults)
                    self.assertEqual(ref.absent, defaults[ref.dest])
            for blocker in gate.blocked_by:
                self.assertTrue(blocker.renders.strip())


class ContinuationGateTests(unittest.TestCase):
    """--deeper shares --more's refusals: `--deeper H -c` silently discarded
    the count and replayed 14 rows, and `--exclude-project` beside either verb
    silently dropped the filter while every served row matched it."""

    HANDLE = "m." + "a" * 24

    def test_deeper_refuses_a_count(self) -> None:
        for flag in ("-c", "--count-by-tier"):
            with self.subTest(flag=flag):
                rc, err = _exit_code(search.main, ["--deeper", self.HANDLE, flag])
                self.assertEqual(rc, 2, err)
                self.assertIn("--deeper", err)
                self.assertIn(flag, err)

    def test_both_verbs_refuse_exclude_project(self) -> None:
        for verb in ("--more", "--deeper"):
            with self.subTest(verb=verb):
                rc, err = _exit_code(
                    search.main,
                    [verb, self.HANDLE, "--exclude-project", "work"])
                self.assertEqual(rc, 2, err)
                self.assertIn(verb, err)
                self.assertIn("filter", err)


class NonsenseValueTests(unittest.TestCase):
    """A nonsense option value is refused, never reinterpreted: `resume -n=-1`
    sliced rows[:-1] and served every session but the oldest, rc 0."""

    def test_resume_refuses_a_negative_n(self) -> None:
        rc, err = _exit_code(resume.main, ["-l", "-n=-1"])
        self.assertEqual(rc, 2, err)
        self.assertIn("-n", err)
        self.assertIn("0 or greater", err)

    def test_resume_still_honors_a_zero_and_positive_n(self) -> None:
        for value in ("0", "3"):
            with self.subTest(value=value):
                self.assertIsNone(_run(resume.main, ["-l", "-n", value]))


if __name__ == "__main__":
    unittest.main()
