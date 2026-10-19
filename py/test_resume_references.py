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


SIDE = "agent-ahostile01"


def _side_row(session: str = SIDE, *, parent: str = SESSION, last_ts: int = 9) -> dict:
    return {**_row(session, first_text="[subagent task] probe the module",
                   last_ts=last_ts), "parent": parent}


class SideChatResumeTests(unittest.TestCase):
    """A side chat is not a resumable transcript; it resumes the chat that spawned it."""

    _run = ResumeReferenceTests._run

    def test_side_chat_handle_resumes_its_root_parent(self) -> None:
        rows = [_side_row(), _row(last_ts=1)]
        for query in (SIDE, f"@{SIDE}:0.1895", "agent-ah"):
            with self.subTest(query=query):
                rc, _out, err, _live, launch, _prompt = self._run([query], rows)
                self.assertEqual(rc, 0, err)
                launch.assert_called_once_with("claude", SESSION)
                self.assertIn("side chat", err)
                self.assertIn("spawned by 01a06003", err)

    def test_nested_side_chats_climb_to_the_root(self) -> None:
        rows = [_side_row("agent-grandchild", parent="agent-child"),
                _side_row("agent-child"), _row()]
        rc, _out, err, _live, launch, _prompt = self._run(["agent-grandchild"], rows)
        self.assertEqual(rc, 0, err)
        launch.assert_called_once_with("claude", SESSION)

    def test_side_chat_without_an_indexed_parent_refuses_and_names_it(self) -> None:
        rc, _out, err, _live, launch, _prompt = self._run(
            [SIDE], [_side_row(parent="deadbeef-missing")])
        self.assertEqual(rc, 1)
        launch.assert_not_called()
        self.assertIn("deadbeef-missing", err)
        self.assertIn("side chat", err)
        self.assertIn("isn't indexed", err)

    def test_a_looping_parent_chain_is_named_as_a_loop(self) -> None:
        rows = [_side_row("agent-one", parent="agent-two"),
                _side_row("agent-two", parent="agent-one")]
        rc, _out, err, _live, launch, _prompt = self._run(["agent-one"], rows)
        self.assertEqual(rc, 1)
        launch.assert_not_called()
        self.assertIn("loops back to agent-one", err)
        self.assertNotIn("isn't indexed", err)

    def test_side_chat_resume_follows_each_agents_layout(self) -> None:
        # codex side threads are rollouts with their own thread ids: resumed directly.
        # pi/omp nest side chats as <slug>/<stamp>_<parent>/<name>.jsonl, which `-r`
        # cannot address: they resume through the root like Claude's subagents.
        side_id = "019a-side-thread"
        for agent, expect_launch in (("codex", side_id), ("pi", SESSION)):
            with self.subTest(agent=agent):
                side = {**_side_row(side_id), "agent": agent}
                parent = {**_row(), "agent": agent}
                rc, _out, err, _live, launch, _prompt = self._run(
                    [side_id], [side, parent])
                self.assertEqual(rc, 0, err)
                launch.assert_called_once_with(agent, expect_launch)
                self.assertEqual("spawned by" in err, expect_launch == SESSION)
        # a top-level pi session (no parent) is its own <stamp>_<id>.jsonl: direct
        top = {**_row("019b-top-level", first_text="plain pi chat"), "agent": "pi"}
        rc, _out, err, _live, launch, _prompt = self._run(["019b-top-level"], [top])
        self.assertEqual(rc, 0, err)
        launch.assert_called_once_with("pi", "019b-top-level")

    def test_live_only_side_chat_resumes_its_indexed_parent(self) -> None:
        live_side = _side_row("agent-live-child")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(resume, "_sessions", return_value=[_row()]), \
                mock.patch.object(resume, "_live_match",
                                  return_value=([live_side], True)), \
                mock.patch.object(resume.native, "resume_in_place", return_value=0) as launch, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = resume.main(["agent-live-child"])
        self.assertEqual(rc, 0, err.getvalue())
        launch.assert_called_once_with("claude", SESSION)

    def test_listings_and_pickers_offer_root_chats_only(self) -> None:
        rows = [_side_row(), _row(last_ts=1)]
        rc, out, err, _live, launch, _prompt = self._run(["--list"], rows)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("agent-ah", out)
        self.assertIn("01a06003", out)

        rc, _out, err, _live, launch, prompt = self._run([], rows, tty=True, selection="1")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("agent-ah", err)
        launch.assert_called_once_with("claude", SESSION)

        # a project label or first-line substring never picks the side chat
        for query in ("solo-finder", "probe the module"):
            with self.subTest(query=query):
                rc, _out, err, _live, launch, _prompt = self._run([query], rows)
                self.assertEqual(rc, 0 if query == "solo-finder" else 1, err)
                if query == "solo-finder":
                    launch.assert_called_once_with("claude", SESSION)
                else:
                    launch.assert_not_called()

    def test_n_zero_lists_every_session_like_sibling_surfaces(self) -> None:
        rows = [_row(f"chat-{i:03d}", last_ts=i) for i in range(20)]
        rc, out, err, _live, _launch, _prompt = self._run(["--list", "-n", "0"], rows)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.splitlines()), 20)
        rc, out, _err, _live, _launch, _prompt = self._run(["zzzznope", "-n", "0"], rows)
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.splitlines()), 20)
        rc, out, _err, _live, _launch, _prompt = self._run(["--list"], rows)
        self.assertEqual(len(out.splitlines()), 15)

    def test_help_documents_the_missing_cli_exit(self) -> None:
        rc, out, _err, _live, _launch, _prompt = self._run(["--help"], [])
        self.assertEqual(rc, 0)
        flat = " ".join(out.split())
        self.assertIn("127 the agent's CLI isn't on PATH", flat)
        self.assertIn("0 = all", flat)

    def test_unresumable_claude_id_leaves_no_project_folder_behind(self) -> None:
        import tempfile
        native = resume.native
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(native, "HOME", td):
            cwd = str(Path(td) / "work")
            native._claude_link_session("agent-nothing-to-link", cwd)
            self.assertFalse((Path(td) / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
