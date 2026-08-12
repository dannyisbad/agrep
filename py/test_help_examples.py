"""Help clarity: examples ARE the documentation.

Every user-facing verb's --help carries an EXAMPLES section with real
invocations, and the top-level --help groups the verbs by task.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import cli  # noqa: E402
import postcompact  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402


REVIEWED_CORE_EVIDENCE_PATH = """\
CORE EVIDENCE PATH

1. Recover a missing prior fact, decision, artifact, or result:

   agrep recall "<faithful clue-preserving description>" --hits 2 --budget 5000

2. Open zero or one qualifying result at its source:

   agrep around <handle>"""


def _help_of(main, argv):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(io.StringIO()), \
            unittest.TestCase().assertRaises(SystemExit) as raised:
        main(argv)
    assert raised.exception.code == 0, raised.exception.code
    return stdout.getvalue()


def _cli_help(argv):
    stdout = io.StringIO()
    with mock.patch.object(sys, "argv", ["agrep", *argv]), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(io.StringIO()), \
            unittest.TestCase().assertRaises(SystemExit) as raised:
        cli._main()
    assert raised.exception.code == 0, raised.exception.code
    return stdout.getvalue()


def _cli_run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["agrep", *argv]), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        rc = cli._main()
    return rc, stdout.getvalue(), stderr.getvalue()


def _assert_examples(case, rendered, label):
    case.assertIn("examples:", rendered, label)
    example_lines = [line for line in rendered.splitlines()
                     if line.strip().startswith("agrep ")]
    case.assertGreaterEqual(len(example_lines), 2, label)


def _assert_exit_contract(case, rendered, label):
    case.assertIn("exit:", rendered.lower(), label)


class VerbHelpExamples(unittest.TestCase):
    def test_history_surfaces_carry_examples(self) -> None:
        for label, run in (
                ("search", lambda: _help_of(search.main, ["--help"])),
                ("chats", lambda: _help_of(search.chats_main, ["--help"])),
                ("around", lambda: _help_of(around.main, ["--help"])),
                ("postcompact", lambda: _help_of(postcompact.main, ["--help"])),
                ("recall", lambda: _help_of(
                    lambda a: recall.main(a, prog="recall"), ["--help"])),
                ("pack", lambda: _help_of(
                    lambda a: recall.main(a, prog="pack"), ["--help"])),
        ):
            _assert_examples(self, run(), label)

    def test_search_help_documents_the_new_filters(self) -> None:
        rendered = _help_of(search.main, ["--help"])
        for flag in ("--no-who", "--no-meta", "--who"):
            self.assertIn(flag, rendered)

    def test_search_examples_are_windows_safe_and_dependency_free(self) -> None:
        rendered = _help_of(search.main, ["--help"])
        self.assertIn('agrep -E "TODO|FIXME"', rendered)
        self.assertNotIn("agrep -E 'TODO|FIXME'", rendered)
        self.assertIn("agrep memory --json", rendered)
        self.assertNotIn("jq", rendered)

    def test_search_help_explains_classic_and_compact_limits(self) -> None:
        rendered = _help_of(search.main, ["--help"])
        normalized = " ".join(rendered.split())
        for detail in ("classic: default 40 keyword or 10 semantic",
                       "compact: adaptive 4-16 rows within 3584 bytes",
                       "explicit positive N requests up to 40 frozen rows",
                       "still byte-limited"):
            self.assertIn(detail, normalized)

    def test_agent_and_speaker_names_are_individually_wrappable(self) -> None:
        for rendered in (
                _help_of(search.main, ["--help"]),
                _help_of(search.chats_main, ["--help"]),
                _help_of(lambda a: recall.main(a, prog="recall"), ["--help"]),
        ):
            for agent in search.common.KNOWN_AGENTS:
                self.assertIn(agent, rendered)
            self.assertNotIn("/".join(search.common.KNOWN_AGENTS), rendered)
        search_help = _help_of(search.main, ["--help"])
        self.assertNotIn(",".join(search.surface.SEARCH_SPEAKER_CHOICES),
                         search_help)

    def test_maintenance_verbs_carry_examples_through_cli(self) -> None:
        for argv in (["index", "--help"], ["setup", "--help"],
                     ["status", "--help"], ["remove", "--help"],
                     ["run", "--help"]):
            _assert_examples(self, _cli_help(argv), argv[0])

    def test_operational_verbs_document_examples_and_exits(self) -> None:
        for verb in ("resume", "archive", "restore", "tail"):
            rendered = _cli_help([verb, "--help"])
            _assert_examples(self, rendered, verb)
            _assert_exit_contract(self, rendered, verb)

        rc, rendered, error = _cli_run(["doctor", "--help"])
        self.assertEqual((rc, error), (0, ""))
        _assert_examples(self, rendered, "doctor")
        _assert_exit_contract(self, rendered, "doctor")

    def test_run_help_documents_a_working_pass_through(self) -> None:
        rendered = _cli_help(["run", "--help"])
        self.assertIn("AGENT [-- AGENT_ARGS...]", rendered)
        self.assertIn("agrep run codex --cwd . -- --help", rendered)
        with mock.patch("hookless.capture.run_captured", return_value=0) as launch:
            rc = cli.cmd_run(SimpleNamespace(
                rest=["codex", "--cwd", ".", "--", "--help"]))
        self.assertEqual(rc, 0)
        launch.assert_called_once_with("codex", ["--help"], cwd=".")

    def test_setup_help_names_the_required_search_index(self) -> None:
        rendered = _cli_help(["setup", "--help"])
        self.assertIn("build the required search index", rendered)
        self.assertIn("--no-semantic", rendered)
        self.assertIn("~52 MiB model prefetch", rendered)

    def test_set_help_is_successful_and_does_no_work(self) -> None:
        with mock.patch.object(cli.settings, "set_setting") as changed:
            rc, output, error = _cli_run(["set", "--help"])
        self.assertEqual((rc, error), (0, ""))
        _assert_examples(self, output, "set")
        changed.assert_not_called()

    def test_pack_help_uses_pack_commands(self) -> None:
        rendered = _help_of(
            lambda a: recall.main(a, prog="pack"), ["--help"])
        example_lines = [line.strip() for line in rendered.splitlines()
                         if line.strip().startswith("agrep ")]
        self.assertTrue(example_lines)
        self.assertTrue(all(line.startswith("agrep pack ")
                            for line in example_lines))


class TopLevelHelpGrouping(unittest.TestCase):
    def test_top_level_help_carries_exact_core_evidence_path_only(self) -> None:
        self.assertEqual(cli._CORE_EVIDENCE_PATH,
                         REVIEWED_CORE_EVIDENCE_PATH)
        rendered = _cli_help(["--help"])
        self.assertIn(REVIEWED_CORE_EVIDENCE_PATH, rendered)
        self.assertNotIn("AGREP EVERYDAY USE", rendered)
        self.assertNotIn("OTHER INTENTS", rendered)

    def test_setup_confirmation_carries_exact_core_evidence_path(self) -> None:
        stdout = io.StringIO()
        args = SimpleNamespace(
            rest=[], yes=True, no_teach=False, no_hook=True, no_semantic=True,
            archive=False, no_archive=True,
        )
        with mock.patch("doctor.main", return_value=0), \
                mock.patch("teach.teach", return_value=0), \
                mock.patch.object(cli.common, "lap"), \
                mock.patch.object(
                    cli, "_setup_index_state",
                    return_value=({"messages": 1, "sessions": 1}, False)), \
                mock.patch.object(cli, "_setup_archive"), \
                mock.patch("teach.detected_agents", return_value=[]), \
                mock.patch.object(cli.common, "cli_name", return_value="agrep"), \
                mock.patch("hookinstall.install") as hook_install, \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.cmd_setup(args), 0)
        hook_install.assert_not_called()
        rendered = stdout.getvalue()
        self.assertIn(REVIEWED_CORE_EVIDENCE_PATH, rendered)
        self.assertNotIn("AGREP EVERYDAY USE", rendered)
        self.assertNotIn("OTHER INTENTS", rendered)

    def test_verbs_are_grouped_by_task(self) -> None:
        rendered = _cli_help(["--help"])
        for group in ("find text", "resume work", "maintain", "live"):
            self.assertIn(group, rendered)
        _assert_examples(self, rendered, "top-level")
        # the collision escape hatch is documented where eyes land first
        self.assertIn("agrep search index", rendered)
        self.assertIn("chats", rendered)
        self.assertIn("search history; compact prose may add meaning", rendered)
        maintain = rendered.split("maintain", 1)[1].split("live", 1)[0]
        self.assertIn("audit", maintain)

    def test_status_and_doctor_explain_bounded_vs_deep(self) -> None:
        status_help = _cli_help(["status", "--help"])
        self.assertIn("bounded diagnostic", status_help)
        self.assertIn("doctor --deep", status_help)
        self.assertNotIn("full diagnostic (same", status_help)

        rc, doctor_help, error = _cli_run(["doctor", "--help"])
        self.assertEqual((rc, error), (0, ""))
        self.assertIn("bounded diagnostic", doctor_help)
        self.assertIn("doctor --deep", doctor_help)

    def test_bare_status_regex_example_is_windows_safe(self) -> None:
        stdout = io.StringIO()
        args = SimpleNamespace(json=False, fn=None)
        with mock.patch.object(cli, "_status_lines", return_value=iter(())), \
                mock.patch.object(cli.common, "cli_name", return_value="agrep"), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.cmd_status(args), 0)
        rendered = stdout.getvalue()
        self.assertIn('agrep -E "TODO|FIXME"', rendered)
        self.assertNotIn("agrep -E 'TODO|FIXME'", rendered)


if __name__ == "__main__":
    unittest.main()
