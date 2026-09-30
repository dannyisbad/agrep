"""Printed and human references select sessions without guessing on ambiguity."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import resume  # noqa: E402


SESSION = "01a06003-4e96-4a0b-a0dd-123456789abc"


def _row(session: str = SESSION, *, project: str = "solo-finder",
         first_text: str = "Fix the authentication retry loop", last_ts: int = 1) -> dict:
    return {
        "session": session,
        "agent": "claude",
        "project": project,
        "first_text": first_text,
        "last_ts": last_ts,
    }


class ResumeReferenceTests(unittest.TestCase):
    def _run(self, argv: list[str], rows: list[dict], *,
             tty: bool = False, selection: str = ""):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO()
        with mock.patch.object(resume, "_sessions", return_value=rows), \
                mock.patch.object(resume, "_live_match", return_value=([], True)) as live, \
                mock.patch.object(resume.native, "resume_in_place", return_value=0) as launch, \
                mock.patch.object(resume.sys, "stdin", stdin), \
                mock.patch.object(stdin, "isatty", return_value=tty), \
                mock.patch("builtins.input", return_value=selection) as prompt, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = resume.main(argv)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue(), live, launch, prompt

    def test_all_five_owner_spellings_resume_the_same_session(self) -> None:
        for query in ("1a06003:4", "1a06003:4-4", "@01a06003:4.82c5",
                      "01a06003:4.82c5", "01a06003"):
            with self.subTest(query=query):
                rc, _out, err, live, launch, _prompt = self._run([query], [_row()])
                self.assertEqual(rc, 0, err)
                live.assert_not_called()
                launch.assert_called_once_with("claude", SESSION)

    def test_every_printed_handle_form_resolves_to_its_session(self) -> None:
        bounded = ".82c5~0123456789abcdef01234567:12-40"
        queries = (
            SESSION, "@" + SESSION, "@01a06003", "01a06003:4", "@01a06003:4",
            "01a06003:4-7", "@01a06003:4-7", "@01a06003:4" + bounded,
            "01a06003:4" + bounded,
        )
        for query in queries:
            with self.subTest(query=query):
                rc, _out, err, _live, launch, _prompt = self._run([query], [_row()])
                self.assertEqual(rc, 0, err)
                launch.assert_called_once_with("claude", SESSION)

    def test_opencode_and_opaque_printed_references_resume(self) -> None:
        for session in ("ses_01JEXAMPLE", "session/with spaces:é", "abc:4"):
            row = _row(session)
            handle = resume.compact.encode_result_handle(
                {"session": session, "turn": 4, "content_digest": "82c5"})
            for query in (session, handle):
                with self.subTest(query=query):
                    rc, _out, err, _live, launch, _prompt = self._run([query], [row])
                    self.assertEqual(rc, 0, err)
                    launch.assert_called_once_with("claude", session)

    def test_exact_and_prefix_ids_win_over_fuzzy_or_human_matches(self) -> None:
        for query, exact in (("01a06003", "01a06003"),
                             ("1a06003", "1a06003-prefix-session")):
            with self.subTest(query=query):
                rows = [_row(), _row(exact), _row("unrelated", project=query)]
                rc, _out, err, _live, launch, _prompt = self._run([query], rows)
                self.assertEqual(rc, 0, err)
                launch.assert_called_once_with("claude", exact)

    def test_unique_hex_substrings_allow_a_dropped_leading_character(self) -> None:
        for query in ("1a06003", "A06003", "@1a06003"):
            with self.subTest(query=query):
                rc, _out, err, _live, launch, _prompt = self._run([query], [_row()])
                self.assertEqual(rc, 0, err)
                launch.assert_called_once_with("claude", SESSION)

    def test_short_or_nonhex_substrings_do_not_guess_a_session(self) -> None:
        for query, session in (("a0600", SESSION), ("middle", "my-middle-session")):
            with self.subTest(query=query):
                rc, _out, _err, _live, launch, _prompt = self._run([query], [_row(session)])
                self.assertEqual(rc, 1)
                launch.assert_not_called()

    def test_project_labels_use_filter_semantics_before_first_text(self) -> None:
        for label, query in (("solo-finder", "SOLO-FINDER"),
                             ("/work/solo-finder", "solo-finder"),
                             ("C:\\work\\solo-finder", "solo-finder"),
                             ("/work/solo-finder", "/WORK/SOLO-FINDER"),
                             ("solo-finder", "solo-*")):
            with self.subTest(label=label, query=query):
                rows = [_row(project=label),
                        _row("other-session", project="other", first_text=f"Discuss {query}")]
                rc, _out, err, live, launch, _prompt = self._run([query], rows)
                self.assertEqual(rc, 0, err)
                live.assert_not_called()
                launch.assert_called_once_with("claude", SESSION)

    def test_partial_project_label_does_not_match(self) -> None:
        rc, _out, _err, _live, launch, _prompt = self._run(["solo"], [_row()])
        self.assertEqual(rc, 1)
        launch.assert_not_called()

    def test_first_text_substring_is_case_insensitive(self) -> None:
        rc, _out, err, live, launch, _prompt = self._run(
            ["AUTHENTICATION RETRY"], [_row(), _row("other", first_text="Different task")])
        self.assertEqual(rc, 0, err)
        live.assert_not_called()
        launch.assert_called_once_with("claude", SESSION)

    def test_human_ambiguity_without_a_terminal_lists_all_candidates_newest_first(self) -> None:
        matches = [_row(f"chat-{i:03d}", last_ts=i) for i in range(14)]
        unrelated = _row("unrelated-session", project="other", first_text="Unrelated")
        for query in ("solo-finder", "authentication retry"):
            with self.subTest(query=query):
                rc, out, err, _live, launch, prompt = self._run(
                    [query, "-n", "1"], [matches[0], unrelated, *matches[1:]])
                self.assertEqual(rc, 1)
                displayed = out + err
                positions = [displayed.index(row["session"]) for row in reversed(matches)]
                self.assertEqual(positions, sorted(positions))
                self.assertNotIn("unrelate", displayed)
                launch.assert_not_called()
                prompt.assert_not_called()

    def test_human_ambiguity_picker_is_restricted_and_newest_first(self) -> None:
        older = _row("older-session", last_ts=1)
        newer = _row("newer-session", last_ts=5)
        unrelated = _row("unrelated-session", project="other", first_text="Unrelated", last_ts=9)
        for query in ("solo-finder", "authentication retry"):
            with self.subTest(query=query):
                rc, out, err, live, launch, prompt = self._run(
                    [query], [older, unrelated, newer], tty=True, selection="2")
                self.assertEqual(rc, 0, err)
                self.assertLess(err.index("newer-se"), err.index("older-se"))
                self.assertNotIn("unrelate", out + err)
                live.assert_not_called()
                prompt.assert_called_once()
                launch.assert_called_once_with("claude", "older-session")

    def test_picker_cancel_or_outside_selection_never_launches(self) -> None:
        rows = [_row("older-session"), _row("newer-session", last_ts=2),
                _row("outside-session", project="other", first_text="Unrelated")]
        for selection in ("", "outside-session"):
            with self.subTest(selection=selection):
                rc, _out, err, _live, launch, prompt = self._run(
                    ["solo-finder"], rows, tty=True, selection=selection)
                self.assertEqual(rc, 0, err)
                prompt.assert_called_once()
                launch.assert_not_called()

    def test_ambiguous_session_references_never_launch_or_choose_automatically(self) -> None:
        rows = [_row(), _row("01a06003-0000-0000-0000-000000000000")]
        for query in ("@01a06003:4.82c5", "1a06003"):
            for tty in (False, True):
                with self.subTest(query=query, tty=tty):
                    rc, out, err, _live, launch, prompt = self._run(
                        [query], rows, tty=tty, selection="1")
                    self.assertEqual(rc, 1)
                    self.assertIn("01a06003-4", out + err)
                    self.assertIn("01a06003-0", out + err)
                    launch.assert_not_called()
                    prompt.assert_not_called()

    def test_no_reference_opens_the_recent_picker(self) -> None:
        rc, _out, err, _live, launch, prompt = self._run([], [_row()], tty=True, selection="1")
        self.assertEqual(rc, 0, err)
        prompt.assert_called_once()
        launch.assert_called_once_with("claude", SESSION)

    def test_no_data_and_invalid_arguments_return_two_without_launching(self) -> None:
        for argv in ([], ["--list"], ["-n", "-1"], ["x" * 4097]):
            with self.subTest(argv=argv[:1]):
                rc, _out, _err, _live, launch, _prompt = self._run(argv, [])
                self.assertEqual(rc, 2)
                launch.assert_not_called()

    def test_resumed_agent_exit_status_is_passed_through(self) -> None:
        for status in (1, 7):
            with self.subTest(status=status), \
                    mock.patch.object(resume, "_sessions", return_value=[_row()]), \
                    mock.patch.object(resume.native, "resume_in_place", return_value=status):
                self.assertEqual(resume.main([SESSION]), status)


if __name__ == "__main__":
    unittest.main()
