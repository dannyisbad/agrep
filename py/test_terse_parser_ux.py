"""Product argument failures are terse without weakening command dispatch."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import surface_policy as surface  # noqa: E402


class TerseParserSubprocessTests(unittest.TestCase):
    INVALID = (
        (["search", "needle", "--definitely-invalid"], "agrep:"),
        (["recall", "needle", "--definitely-invalid"], "agrep recall:"),
        (["pack", "needle", "--definitely-invalid"], "agrep pack:"),
        (["around"], "agrep around:"),
        (["chats", "--definitely-invalid"], "agrep chats:"),
        (["board", "--definitely-invalid"], "agrep board:"),
        (["tail", "--definitely-invalid"], "agrep tail:"),
        (["resume", "--definitely-invalid"], "agrep resume:"),
        (["doctor", "--definitely-invalid"], "agrep doctor:"),
        (["status", "--json", "--definitely-invalid"], "agrep status:"),
        (["setup", "--definitely-invalid"], "agrep setup:"),
        (["index", "--definitely-invalid"], "agrep index:"),
        (["reindex", "--definitely-invalid"], "agrep reindex:"),
        (["audit", "--definitely-invalid"], "agrep audit:"),
        (["archive", "--on", "--off"], "agrep archive:"),
        (["restore"], "agrep restore:"),
        (["set", "not-a-setting", "1"], "agrep set:"),
        (["remove", "--definitely-invalid"], "agrep remove:"),
        (["run", "--cwd", "."], "agrep run:"),
        (["ui", "--definitely-invalid"], "agrep ui:"),
        (["serve", "--definitely-invalid"], "agrep serve:"),
        (["search", "needle", "-n", "-1"], "agrep:"),
        (["recall", "needle", "--budget", "1"], "agrep recall:"),
        (["around", "@deadbeef", "--context", "-1"], "agrep around:"),
        (["chats", "-n", "-1"], "agrep chats:"),
        (["board", "--once", "-n", "0"], "agrep board:"),
        (["audit", "--agent", "codex", "--agent", "claude"], "agrep audit:"),
        (["doctor", "--json", "--fix"], "agrep doctor:"),
        (["tail", "--events", "not-an-event"], "agrep tail:"),
        (["resume", "-n", "-1"], "agrep resume:"),
        (["reindex", "--max-new", "0"], "agrep reindex:"),
        (["archive", "--keep", "-1"], "agrep archive:"),
        (["ui", "--port", "-1"], "agrep ui:"),
    )

    def _run(self, argv: list[str], data_dir: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "AGREP_DATA_DIR": data_dir, "NO_COLOR": "1"}
        return subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), *argv],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )

    def test_invalid_product_commands_emit_one_stderr_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            for argv, prefix in self.INVALID:
                with self.subTest(argv=argv):
                    proc = self._run(argv, data_dir)
                    self.assertEqual(proc.returncode, 2, proc)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(len(proc.stderr.splitlines()), 1, proc.stderr)
                    self.assertTrue(proc.stderr.startswith(prefix), proc.stderr)
                    self.assertNotIn("usage:", proc.stderr.lower())

    def test_help_keeps_the_full_parser_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            for argv, usage in (
                    (["--help"], "usage: agrep"),
                    (["search", "--help"], "usage: agrep"),
                    (["board", "--help"], "usage: agrep board"),
                    (["archive", "--help"], "usage: agrep archive")):
                with self.subTest(argv=argv):
                    proc = self._run(argv, data_dir)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stderr, "")
                    self.assertIn(usage, proc.stdout)
                    self.assertIn("options:", proc.stdout)

    def test_reserved_search_escape_preserves_valid_search_argv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            proc = self._run(
                ["board", "--sort", "time", "--no-self", "--who", "user"],
                data_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "agrep board: argument --sort: invalid choice: 'time' "
            "(choose from working, updated); to search for the word \"board\": "
            "agrep search board --sort time --no-self --who user\n")

    def test_invalid_search_value_never_gets_a_dead_end_hint(self) -> None:
        cases = (
            ["board", "--sort", "bogus"],
            ["board", "--agent", "--json"],
            ["board", "--project", "-n", "3"],
        )
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            for argv in cases:
                with self.subTest(argv=argv):
                    proc = self._run(argv, data_dir)
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(len(proc.stderr.splitlines()), 1)
                    self.assertNotIn("to search for", proc.stderr)

    def test_top_parser_hint_preserves_options_it_already_consumed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            proc = self._run(["status", "--json", "deadlock"], data_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(
            "agrep search status --json deadlock", proc.stderr)

    def test_untrusted_argument_is_escaped_and_not_repeated_as_a_command(self) -> None:
        payload = "bad\x1b]52;c;payload\x07"
        with tempfile.TemporaryDirectory(prefix="agrep-parser-") as data_dir:
            proc = self._run(["doctor", payload], data_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("\x1b", proc.stderr)
        self.assertNotIn("\x07", proc.stderr)
        self.assertIn("\\u001b", proc.stderr)
        self.assertNotIn("to search for", proc.stderr)


class CommandRenderingTests(unittest.TestCase):
    def test_argparse_choice_lists_are_stable_across_python_releases(self) -> None:
        quoted = (
            "argument --sort: invalid choice: 'time' "
            "(choose from 'working', 'updated')")
        expected = (
            "argument --sort: invalid choice: 'time' "
            "(choose from working, updated)")
        self.assertEqual(surface._stable_argparse_error(quoted), expected)
        self.assertEqual(surface._stable_argparse_error(expected), expected)
        unsafe = "invalid choice: 'x' (choose from 'two words', 'safe')"
        self.assertEqual(surface._stable_argparse_error(unsafe), unsafe)

    def test_windows_rendering_quotes_spaces_and_refuses_shell_metacharacters(self) -> None:
        self.assertEqual(
            surface.render_cli_argv(
                ["agrep", "search", "board", "two words"], windows=True),
            'agrep search board "two words"')
        self.assertEqual(
            surface.render_cli_argv(
                ["agrep", "search", "board", "@deadbeef"], windows=True),
            'agrep search board "@deadbeef"')
        for unsafe in (
                "a&b", "%PATH%", "$env:HOME", "`whoami`",
                "x#comment", "x' y", 'x" y', "bad\x1b]52;c;x\x07",
                "line\nbreak"):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(surface.render_cli_argv(
                    ["agrep", "search", "board", unsafe], windows=True))

    def test_windows_reserved_hint_quotes_initial_at_sign(self) -> None:
        with mock.patch.object(surface.os, "name", "nt"):
            self.assertEqual(
                surface.reserved_search_hint("board", ["@deadbeef"]),
                'to search for the word "board": '
                'agrep search board "@deadbeef"')
            self.assertEqual(
                surface.reserved_search_hint(
                    "board", ["--sort", "time", "two words"]),
                'to search for the word "board": '
                'agrep search board --sort time "two words"')

    def test_posix_rendering_round_trips_spaces_and_quotes(self) -> None:
        rendered = surface.render_cli_argv(
            ["agrep", "search", "board", "two words", "it's"], windows=False)
        self.assertEqual(
            shlex.split(rendered or ""),
            ["agrep", "search", "board", "two words", "it's"])


if __name__ == "__main__":
    unittest.main()
