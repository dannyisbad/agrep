"""Command-word dispatch is total and never falls through accidentally.

`agrep "search"` parses as the search verb with an empty pattern; the error
must name the escape hatch (`agrep search "search"`). Every recognized or
reserved verb reaches its own handler for every argument shape; only bare
non-command text takes the namesake search path.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import cli  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402


def _dispatch(argv: list[str]) -> tuple[str, list[str] | None]:
    """Run cli._main with every handler stubbed; report which one fired."""
    fired: list[tuple[str, list[str]]] = []

    def record(name):
        def handler(a):
            fired.append((name, list(getattr(a, "rest", []) or [])))
            return 0
        return handler

    handlers = {name: record(name) for name in (
        "cmd_search", "cmd_chats", "cmd_around", "cmd_postcompact", "cmd_recall",
        "cmd_pack", "cmd_resume",
        "cmd_status", "cmd_setup", "cmd_index", "cmd_doctor", "cmd_audit",
        "cmd_reindex", "cmd_archive", "cmd_restore", "cmd_set", "cmd_remove",
        "cmd_tail", "cmd_board", "cmd_inject", "cmd_run",
    )}
    with mock.patch.multiple(cli, **handlers), \
            mock.patch.object(sys, "argv", ["agrep", *argv]):
        cli._main()
    if not fired:
        return "", None
    return fired[0][0], fired[0][1]


class VerbCollisionDispatchTests(unittest.TestCase):
    COMMANDS = (
        ("search", "cmd_search"), ("chats", "cmd_chats"),
        ("around", "cmd_around"), ("postcompact", "cmd_postcompact"),
        ("recall", "cmd_recall"),
        ("pack", "cmd_pack"), ("resume", "cmd_resume"),
        ("status", "cmd_status"), ("setup", "cmd_setup"),
        ("index", "cmd_index"), ("doctor", "cmd_doctor"),
        ("audit", "cmd_audit"), ("reindex", "cmd_reindex"),
        ("archive", "cmd_archive"), ("restore", "cmd_restore"),
        ("set", "cmd_set"), ("remove", "cmd_remove"),
        ("tail", "cmd_tail"), ("board", "cmd_board"),
        ("live", "cmd_board"), ("inject", "cmd_inject"),
        ("run", "cmd_run"),
    )

    def test_bare_verb_words_keep_their_verb_semantics(self) -> None:
        for word, handler in self.COMMANDS:
            self.assertEqual(_dispatch([word]), (handler, []), word)

    def test_bare_noncommand_tokens_are_the_namesake_search(self) -> None:
        for word in ("deadlock", "frobnicate"):
            name, rest = _dispatch([word])
            self.assertEqual((name, rest), ("cmd_search", [word]), word)

    def test_every_command_word_owns_malformed_trailing_arguments(self) -> None:
        for word, handler in self.COMMANDS:
            name, rest = _dispatch([word, "deadlock"])
            self.assertEqual(name, handler, word)
            self.assertEqual(rest, ["deadlock"], word)

    def test_explorer_verbs_never_fall_through_to_search(self) -> None:
        with mock.patch.object(cli, "cmd_explorer", return_value=2) as explorer, \
                mock.patch.object(cli, "cmd_search") as search_handler:
            for word in ("ui", "up", "serve"):
                with self.subTest(word=word), \
                        mock.patch.object(sys, "argv", ["agrep", word, "deadlock"]):
                    self.assertEqual(cli._main(), 2)
            self.assertEqual(explorer.call_count, 3)
            search_handler.assert_not_called()

    def test_restore_and_unknown_set_key_reach_their_validators(self) -> None:
        self.assertEqual(
            _dispatch(["restore", "deadlock"]),
            ("cmd_restore", ["deadlock"]),
        )
        self.assertEqual(
            _dispatch(["set", "zzznotarealkey", "1"]),
            ("cmd_set", ["zzznotarealkey", "1"]),
        )

    def test_explicit_search_verb_always_searches_command_words(self) -> None:
        for word in [item[0] for item in self.COMMANDS] + ["ui", "up", "serve"]:
            name, rest = _dispatch(["search", word])
            self.assertEqual((name, rest), ("cmd_search", [word]), word)


class EmptyPatternHintTests(unittest.TestCase):
    def _run_search(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = search.main(argv)
        return rc, stderr.getvalue()

    def test_empty_pattern_names_the_escape_hatch(self) -> None:
        rc, err = self._run_search([])
        self.assertEqual(rc, 2)
        self.assertIn("empty pattern", err)
        self.assertIn('agrep search "search"', err)

    def test_whitespace_pattern_gets_the_same_hint(self) -> None:
        rc, err = self._run_search(["  "])
        self.assertEqual(rc, 2)
        self.assertIn('agrep search "search"', err)

    def test_recall_and_pack_empty_query_name_their_own_word(self) -> None:
        for prog in ("recall", "pack"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = recall.main([" "], prog=prog)
            self.assertEqual(rc, 2, prog)
            self.assertIn(f'agrep search "{prog}"', stderr.getvalue(), prog)

    def test_bare_recall_still_exits_2_for_scripts(self) -> None:
        # Auto-search would make a malformed reserved verb look successful.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            recall.main([], prog="recall")
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
