from __future__ import annotations

import contextlib
import io
import json
import random
import shlex
import subprocess
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import archive  # noqa: E402
import audit  # noqa: E402
import common  # noqa: E402
import console  # noqa: E402
import surface_policy as surface  # noqa: E402
from hookless import native  # noqa: E402
import livetui  # noqa: E402
import resume  # noqa: E402
import search  # noqa: E402


PAYLOAD = "before\x1b]52;c;Y2xpcGJvYXJk\x07after\r\u2028\u202eforge\ud800"


def _terminal_safe_reference(value: object, *, multiline: bool = False) -> str:
    text = "" if value is None else str(value)
    out: list[str] = []
    for char in text:
        if char == "\n" and multiline:
            out.append(char)
            continue
        if char == "\t" and multiline:
            out.append("    ")
            continue
        code = ord(char)
        if (code < 0x20 or 0x7F <= code <= 0x9F
                or unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}):
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


class TerminalSafetyTests(unittest.TestCase):
    def test_windows_shell_hint_refuses_cross_shell_metacharacters(self):
        with mock.patch.object(console, "WIN", True):
            for payload in (
                    "needle&whoami", "%PATH%", "$env:PATH", "x;calc",
                    "x' y", "x\" y", "x#comment", "x`whoami",
                    "bad\x1b]52;c;x\x07", "line\nbreak"):
                with self.subTest(payload=payload):
                    self.assertEqual(
                        console.shell_command(
                            "agrep", payload, fallback="unsafe"),
                        "unsafe")
            self.assertEqual(
                console.shell_command(
                    "agrep", "around", "@deadbeef", fallback="unsafe"),
                'agrep around "@deadbeef"')
            self.assertEqual(
                console.shell_command("agrep", "two words", fallback="unsafe"),
                'agrep "two words"')

    def test_coverage_hint_uses_the_shared_shell_boundary(self):
        with mock.patch.object(console, "WIN", True):
            for payload in ("$(Write-Output PWN)", "`whoami`", "%PATH%"):
                with self.subTest(payload=payload):
                    self.assertEqual(
                        search._coverage_cmd(payload),
                        "agrep --coverage <query>")
        with mock.patch.object(console, "WIN", False):
            query = "meaning with spaces and 'quotes'"
            self.assertEqual(
                shlex.split(search._coverage_cmd(query)),
                ["agrep", "--coverage", "--", query])

    def test_around_expansion_quotes_untrusted_session_ids(self):
        session = "abc; touch /tmp/agrep-pwn"
        with mock.patch.object(console, "WIN", False):
            self.assertEqual(
                shlex.split(around._expand_command(session, 7)),
                ["agrep", "around", session, "7", "-C", "0", "--full"])
        with mock.patch.object(console, "WIN", True):
            self.assertEqual(
                around._expand_command(session, 7),
                "agrep around <session> <turn> -C 0 --full")

    def test_translate_path_matches_slow_reference(self):
        alphabet = (
            "".join(chr(code) for code in range(0xA0))
            + "éİ\u0301\u00ad\u2028\u2029\u202e\ud800😀\U0010ffff"
        )
        values = [
            "",
            "clean ASCII: []{} /tmp/file.py:123",
            "".join(chr(code) for code in range(0x20)),
            "".join(chr(code) for code in range(0x7F, 0xA0)),
            "\x1b[31mANSI red\x1b[0m",
            "emoji 😀, RTL \u202e, surrogate \ud800",
            alphabet,
        ]
        rng = random.Random(0)
        values.extend(
            "".join(rng.choice(alphabet) for _ in range(rng.randrange(80)))
            for _ in range(500)
        )
        for multiline in (False, True):
            for value in values:
                with self.subTest(multiline=multiline, value=repr(value)):
                    self.assertEqual(
                        common.terminal_safe(value, multiline=multiline),
                        _terminal_safe_reference(value, multiline=multiline),
                    )

    def test_shared_sanitizer_quotes_terminal_controls(self):
        rendered = common.terminal_safe(PAYLOAD)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\ud800", rendered)
        self.assertIn("\\u001b", rendered)
        self.assertIn("\\u2028", rendered)
        self.assertIn("\\u202e", rendered)
        self.assertIn("\\ud800", rendered)
        self.assertEqual(common.terminal_safe("a\nb", multiline=True), "a\nb")
        self.assertEqual(common.terminal_safe("a\nb"), "a\\u000ab")

    def test_leaf_and_public_terminal_boundaries_are_byte_identical(self):
        for multiline in (False, True):
            with self.subTest(multiline=multiline):
                expected = _terminal_safe_reference(
                    PAYLOAD, multiline=multiline)
                self.assertEqual(
                    surface.terminal_safe(PAYLOAD, multiline=multiline),
                    expected)
                self.assertEqual(
                    console.terminal_safe(PAYLOAD, multiline=multiline),
                    expected)

    def test_around_human_output_is_safe_but_json_is_exact(self):
        window = {
            "session": "session-safe", "agent": PAYLOAD, "project": PAYLOAD,
            "concept": PAYLOAD, "title": PAYLOAD, "center": 0,
            "first_turn": 0, "last_turn": 0,
            "turns": [{"turn": 0, "who": "you", "text": PAYLOAD,
                       "reply": PAYLOAD, "ts": 0}],
            "events": [{"turn": 0, "kind": "tool", "name": PAYLOAD,
                        "input": PAYLOAD, "output": PAYLOAD, "ok": True,
                        "output_chars": len(PAYLOAD), "ts": 0}],
        }
        patches = (
            mock.patch.object(common, "MESSAGES_PATH", Path(__file__)),
            mock.patch.object(around.explore, "resolve_session",
                              return_value=["session-safe"]),
            mock.patch.object(around.explore, "get_window", return_value=window),
        )
        with patches[0], patches[1], patches[2]:
            human = io.StringIO()
            with contextlib.redirect_stdout(human):
                self.assertEqual(around.main([
                    "session-safe", "0", "--color", "never", "--tool-output", "100"
                ]), 0)
            text = human.getvalue()
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
            self.assertNotIn("\r", text)
            self.assertIn("\\u001b", text)

            machine = io.StringIO()
            with contextlib.redirect_stdout(machine):
                self.assertEqual(around.main(["session-safe", "0", "--json"]), 0)
            rows = []
            payload = machine.getvalue()
            decoder = json.JSONDecoder()
            offset = 0
            while offset < len(payload):
                while offset < len(payload) and payload[offset] in " \t\r\n":
                    offset += 1
                if offset >= len(payload):
                    break
                row, offset = decoder.raw_decode(payload, offset)
                rows.append(row)
            self.assertIn("scope", rows[0])
            row = next(item for item in rows if item.get("kind") == "msg")
            self.assertEqual(row["text"], PAYLOAD)

    def test_board_and_resume_quote_transcript_fields(self):
        session = {
            "agent": PAYLOAD, "project": PAYLOAD, "session": "safe-session",
            "title": PAYLOAD, "model": PAYLOAD, "state": PAYLOAD,
            "last_ts": 1, "working": True, "recent": [
                {"type": "reply", "text": PAYLOAD, "ts": 1}
            ],
        }
        frame = livetui._frame([session], 1, False, PAYLOAD, 120, 30, False)
        focus = livetui._focus_frame(session, 1, 120, 30, False)
        label = resume._label({**session, "first_text": PAYLOAD}, False)
        for rendered in (frame, focus, label):
            self.assertNotIn("\x1b", rendered)
            self.assertNotIn("\x07", rendered)
            self.assertIn("\\u001b", rendered)

    def test_native_resume_quotes_store_cwd_inside_tool_ansi(self):
        launched = mock.Mock(returncode=0)
        with mock.patch.object(native, "resolve_cwd", return_value="/tmp/" + PAYLOAD), \
                mock.patch.object(native.shutil, "which", return_value="/usr/bin/codex"), \
                mock.patch.object(native, "run_owned_process", return_value=launched), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(native.resume_in_place("codex", "session-safe"), 0)
        rendered = stderr.getvalue()
        self.assertNotIn("\x1b]52;", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("\\u001b]52;", rendered)

    def test_live_alias_opens_board_help(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), "live", "--help"],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: agrep board", proc.stdout)
        self.assertNotIn("grep your cross-agent chat history", proc.stdout)

    def test_audit_and_archive_quote_store_paths_and_errors(self):
        danger = "bad\x1b]52;c;payload\x07\r\u202eforge"
        book = {danger: {"agent": "codex", "key": "token", "seen": 1,
                         "rows": 0, "agent_rows": 0, "events": 0,
                         "errors": 1, "skips": {}, "first_error": danger}}
        # The routine pass degrades errors to gaps (exit 1) when its 0.9s
        # observation budget expires, which a loaded box does to it. What is
        # pinned here is escape quoting, so the budget is taken out of play.
        with mock.patch.object(audit, "_book", return_value=book), \
                mock.patch.object(audit, "_ROUTINE_BUDGET_S", 3600.0), \
                mock.patch.object(audit, "_discovered", return_value=[("codex", danger)]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(audit.main([]), 2)
        rendered = out.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("\\u001b", rendered)

        matches = [{"path": danger + str(index), "size": 1, "ts": 1}
                   for index in range(2)]
        with mock.patch.object(archive, "_find", return_value=matches):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(archive.restore(danger), 1)
        rendered = out.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("\\u001b", rendered)


if __name__ == "__main__":
    unittest.main()
