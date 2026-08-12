"""Diagnostic and one-shot live surfaces stay in-process."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

import cli  # noqa: E402

# cli.main installs grep-style SIGPIPE handling; give the interpreter back
# its disposition or every module discovered after this one inherits it.
_sigpipe = None


def setUpModule() -> None:
    global _sigpipe
    import signal
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        _sigpipe = signal.getsignal(sigpipe)


def tearDownModule() -> None:
    import signal
    sigpipe = getattr(signal, "SIGPIPE", None)
    if _sigpipe is not None and sigpipe is not None:
        signal.signal(sigpipe, _sigpipe)


import livetui  # noqa: E402
import tail  # noqa: E402


class InProcessDispatchTests(unittest.TestCase):
    def _assert_dispatch(self, fn, module_name: str, argv: list[str]) -> None:
        module = types.SimpleNamespace(main=mock.Mock(return_value=17))
        with mock.patch.dict(sys.modules, {module_name: module}), \
                mock.patch.object(cli.subprocess, "run") as spawn:
            self.assertEqual(fn(argparse.Namespace(rest=argv)), 17)
        module.main.assert_called_once_with(argv)
        spawn.assert_not_called()

    def test_doctor_audit_tail_and_board_dispatch_in_process(self) -> None:
        self._assert_dispatch(cli.cmd_doctor, "doctor", ["--json"])
        self._assert_dispatch(cli.cmd_audit, "audit", ["--full"])
        self._assert_dispatch(cli.cmd_tail, "tail", ["--snapshot"])
        self._assert_dispatch(cli.cmd_board, "livetui", ["--once"])

    def test_explicit_status_uses_the_same_in_process_doctor(self) -> None:
        doctor = types.SimpleNamespace(main=mock.Mock(return_value=19))
        args = argparse.Namespace(fn=cli.cmd_status, json=False, rest=[])
        with mock.patch.dict(sys.modules, {"doctor": doctor}), \
                mock.patch.object(cli.subprocess, "run") as spawn:
            self.assertEqual(cli.cmd_status(args), 19)
        doctor.main.assert_called_once_with([])
        spawn.assert_not_called()

    def test_status_json_rejects_every_trailing_argument(self) -> None:
        for trailing in ("--fix", "--bogus", "word"):
            err = io.StringIO()
            with self.subTest(trailing=trailing), \
                    mock.patch.object(
                        sys, "argv", ["agrep", "status", "--json", trailing]), \
                    mock.patch.object(cli, "_status_data") as status_data, \
                    redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", err):
                self.assertEqual(cli._main(), 2)
            self.assertIn("--json", err.getvalue())
            self.assertIn(trailing, err.getvalue())
            status_data.assert_not_called()

    def test_cli_keyboard_interrupt_is_quiet_and_canonical(self) -> None:
        err = io.StringIO()
        with mock.patch.object(cli, "_main", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(), 130)
        self.assertEqual(err.getvalue(), "")

    def test_audit_help_reaches_the_real_option_parser(self) -> None:
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["agrep", "audit", "--help"]), \
                redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli._main()
        self.assertEqual(raised.exception.code, 0)
        rendered = output.getvalue()
        for option in ("--agent", "--json", "--strict", "--full"):
            self.assertIn(option, rendered)


class OneShotLiveSurfaceTests(unittest.TestCase):
    class Watcher:
        def __init__(
                self, *, booting: bool = False,
                sessions: list[dict] | None = None) -> None:
            self.timeouts: list[float | None] = []
            self.booting = booting
            self.sessions = sessions or []

        def wait_boot(self, timeout: float | None = None) -> bool:
            self.timeouts.append(timeout)
            return not self.booting

        def snapshot(self) -> dict:
            return {
                "now": 1,
                "sessions": self.sessions,
                "booting": self.booting,
                "last_err": "",
                "degraded_sources": [],
            }

    def test_board_once_omits_interactive_keys_and_legend(self) -> None:
        out = io.StringIO()
        watcher = self.Watcher()
        with mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                redirect_stdout(out):
            self.assertEqual(livetui.main(["--once"]), 0)
        self.assertEqual(
            watcher.timeouts, [livetui._ONESHOT_BOOT_TIMEOUT_S])
        self.assertLess(livetui._ONESHOT_BOOT_TIMEOUT_S, 0.3)
        rendered = out.getvalue()
        self.assertIn("agrep board", rendered)
        self.assertNotIn("select", rendered)
        self.assertNotIn("working  ", rendered)
        self.assertNotIn("child result", rendered)

    def test_board_redacts_ciphertext_without_hiding_tool_context(self) -> None:
        token = "gAAAAAB" + "x" * 96 + "=="
        line = livetui._event_line({
            "type": "tool", "name": "send_message",
            "input": '{"task":"probe","message":"' + token + '"}',
        }, 240)
        self.assertIn("send_message", line)
        self.assertIn('"task":"probe"', line)
        self.assertIn('"message":"[encrypted payload]"', line)
        self.assertNotIn("gAAAAAB", line)

    def test_board_redacts_capabilities_without_hiding_command_context(self) -> None:
        line = livetui._event_line({
            "type": "tool", "name": "exec_command",
            "input": (
                "agrep ui http://127.0.0.1:8732/#bootstrap=urlsafe-token "
                "--project agrep --api-key secret-value "
                "-H 'X-Agrep-Bootstrap: second-token' "
                "-H 'Authorization: Bearer third.token.value'"),
        }, 400)
        self.assertIn("agrep ui", line)
        self.assertIn("--project agrep", line)
        self.assertEqual(line.count("[redacted]"), 4)
        for secret in (
                "urlsafe-token", "secret-value", "second-token",
                "third.token.value"):
            self.assertNotIn(secret, line)
        ordinary = livetui._event_line({
            "type": "tool", "name": "echo",
            "input": "token/count fixture-id --token-count 2048",
        }, 200)
        self.assertIn("token/count", ordinary)
        self.assertIn("fixture-id", ordinary)
        self.assertIn("--token-count 2048", ordinary)

        expanded = livetui._event_line({
            "type": "tool", "name": "exec_command",
            "input": (
                "curl -H 'X-Agrep-Bootstrap: \"quoted-token\"' "
                "-H '{\"X-Agrep-Bootstrap\":\"json-header-token\"}' "
                "-H 'Authorization: Basic dXNlcjpwYXNz' "
                "-H 'Authorization: token ghp_example123' "
                "--cookie agrep_ui_0123456789ab=cookie-secret "
                "postgres://user:pass@host/db "
                "'{\"refresh_token\":\"refresh-secret\","
                "\"private_key\":\"private-secret\"}'"),
        }, 2000)
        for secret in (
                "quoted-token", "json-header-token", "dXNlcjpwYXNz",
                "ghp_example123", "cookie-secret", "user:pass",
                "refresh-secret", "private-secret"):
            self.assertNotIn(secret, expanded)
        self.assertGreaterEqual(expanded.count("[redacted]"), 8)

        prose = livetui._event_line({
            "type": "tool", "name": "docs",
            "input": (
                "implement Bearer authentication middleware; document Bearer "
                "authorization semantics; client_secret: string; "
                "password: boolean; postgres://host/db"),
        }, 1000)
        self.assertIn("Bearer authentication middleware", prose)
        self.assertIn("Bearer authorization semantics", prose)
        self.assertIn("client_secret: string", prose)
        self.assertIn("password: boolean", prose)
        self.assertIn("postgres://host/db", prose)
        self.assertNotIn("[redacted]", prose)

        explicit = livetui._event_line({
            "type": "tool", "name": "exec_command",
            "input": "tool --token false",
        }, 200)
        self.assertIn("--token [redacted]", explicit)
        self.assertNotIn("false", explicit)

    def test_board_fallback_title_names_agent_project_and_short_id(self) -> None:
        title = livetui._live_title({
            "agent": "codex", "project": "/work/agrep",
            "session": "019f563a-db7e", "recent": [],
        })
        self.assertEqual(title, "codex in agrep · 019f563a")

    def test_board_promotes_a_quiet_root_with_a_working_child(self) -> None:
        ordered = livetui._order([{
            "agent": "codex", "session": "newer-idle", "last_ts": 90_000,
            "working": False,
        }, {
            "agent": "codex", "session": "parent", "last_ts": 1,
            "working": False,
        }, {
            "agent": "codex", "session": "child", "parent": "parent",
            "last_ts": 2, "working": True, "sub": True,
        }])
        self.assertEqual(
            [row["session"] for row in ordered],
            ["parent", "child", "newer-idle"])

    def test_tail_snapshot_accepts_explicit_argv(self) -> None:
        out = io.StringIO()
        watcher = self.Watcher()
        with mock.patch.object(tail.live, "watcher", return_value=watcher), \
                redirect_stdout(out):
            self.assertEqual(tail.main(["--snapshot"]), 0)
        self.assertEqual(watcher.timeouts, [tail._ONESHOT_BOOT_TIMEOUT_S])
        self.assertLess(tail._ONESHOT_BOOT_TIMEOUT_S, 0.3)
        self.assertIn('"sessions":[]', out.getvalue())
        self.assertEqual(__import__("json").loads(out.getvalue())["type"],
                         "snapshot")

    def test_timeout_keeps_partial_boot_state_visible(self) -> None:
        out = io.StringIO()
        watcher = self.Watcher(booting=True)
        with mock.patch.object(tail.live, "watcher", return_value=watcher), \
                redirect_stdout(out):
            self.assertEqual(tail.main(["--snapshot"]), 0)
        self.assertTrue(__import__("json").loads(out.getvalue())["booting"])

    def test_board_names_partial_boot_even_with_a_visible_session(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        watcher = self.Watcher(booting=True, sessions=[{
            "agent": "codex",
            "session": "partial-session",
            "project": "fixture",
            "title": "visible before boot completes",
            "last_ts": 1,
            "state": "thinking",
            "working": True,
            "recent": [],
        }])
        with mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(livetui.main(["--once"]), 2)
        self.assertIn("snapshot still scanning", out.getvalue())
        self.assertIn("partial live snapshot; retry:", err.getvalue())

    def test_board_refuses_an_ambiguous_session_with_candidates(self) -> None:
        watcher = self.Watcher(sessions=[
            {"agent": "codex", "session": "shared-one", "recent": []},
            {"agent": "claude", "session": "shared-two", "recent": []},
        ])
        err = io.StringIO()
        with mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(
                    livetui.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                mock.patch("sys.stderr", err):
            self.assertEqual(
                livetui.main(["--once", "--session", "shared"]), 2)
        rendered = err.getvalue()
        self.assertIn("ambiguous", rendered)
        self.assertIn("shared-one", rendered)
        self.assertIn("shared-two", rendered)

    def test_board_exact_session_wins_over_a_substring_neighbour(self) -> None:
        watcher = self.Watcher(sessions=[
            {"agent": "codex", "session": "shared", "recent": []},
            {"agent": "codex", "session": "shared-extra", "recent": []},
        ])
        with mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(
                    livetui.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(
                livetui.main(["--once", "--session", "shared"]), 0)

    def test_board_accepts_a_pasted_result_handle(self) -> None:
        watcher = self.Watcher(sessions=[
            {"agent": "codex", "session": "shared", "recent": []},
        ])
        with mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(
                    livetui.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(
                livetui.main(["--once", "--session", "@shared:12.abcd"]), 0)


if __name__ == "__main__":
    unittest.main()
