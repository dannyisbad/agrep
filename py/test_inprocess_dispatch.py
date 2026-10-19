"""Diagnostic and one-shot live surfaces stay in-process."""

from __future__ import annotations

import argparse
import contextlib
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

from _test_support import isolate_data_dir
isolate_data_dir()

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

    @unittest.skipIf(sys.platform == "win32", "the boundary borrows SIGPIPE on POSIX only")
    def test_a_ctrl_c_during_the_boundary_teardown_is_quiet_too(self) -> None:
        # a Ctrl-C storm that outlasts a view's exit lands while the boundary
        # gives SIGPIPE back
        import signal
        real, calls = signal.signal, []

        def give_back(signum, handler):
            previous = real(signum, handler)
            calls.append(signum)
            if signum == signal.SIGPIPE and len(calls) == 2:
                raise KeyboardInterrupt
            return previous
        err = io.StringIO()
        # a long test process would otherwise add its own "took Ns" line
        with mock.patch.object(cli, "_main", return_value=0), \
                mock.patch.object(cli.common, "profile_report", return_value=None), \
                mock.patch.object(signal, "signal", side_effect=give_back), \
                mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(), 130)
        self.assertEqual(calls, [signal.SIGPIPE, signal.SIGPIPE])
        self.assertEqual(err.getvalue(), "")

    @unittest.skipIf(sys.platform == "win32", "dying of a signal is POSIX")
    def test_a_late_ctrl_c_after_settling_ends_the_process_silently(self) -> None:
        # the entry points settle once the command is done, so the rest of a
        # storm cannot surface as a traceback from interpreter shutdown
        import os
        import signal
        import subprocess
        script = ("import signal, time\n"
                  "import console\n"
                  "console.settle_interrupts()\n"
                  "signal.raise_signal(signal.SIGINT)\n"
                  "time.sleep(30)\n")
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=60,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py")))})
        self.assertEqual((done.returncode, done.stderr), (-signal.SIGINT, b""))

    def test_only_the_process_entry_settles_and_a_ctrl_c_cannot_break_it(self) -> None:
        import signal
        import console
        from agrep import __main__ as entry
        before = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, before)
        launcher = str(ROOT / "agrep-launcher.py")  # stands in for the main script

        def at_top_level(filename: str) -> dict:
            scope = {"__name__": "__main__", "main": entry.main}
            exec(compile("status = main()", filename, "exec"), scope)  # noqa: S102
            return scope

        with mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys.modules["__main__"], "__file__", launcher, create=True), \
                mock.patch("resident.try_run", return_value=None), \
                mock.patch.object(cli, "main", return_value=7):
            # an in-process caller, a prompt and a notebook cell keep their own handler
            self.assertEqual(entry.main(), 7)
            for filename in ("<stdin>", "<python-input-0>", "/tmp/ipykernel_1/cell.py"):
                self.assertEqual(at_top_level(filename)["status"], 7)
            with mock.patch.object(sys, "ps1", ">>> ", create=True):
                self.assertEqual(at_top_level(launcher)["status"], 7)
            self.assertIs(signal.getsignal(signal.SIGINT), before)
            # the main script's top level is the process
            self.assertEqual(at_top_level(launcher)["status"], 7)
            self.assertIs(signal.getsignal(signal.SIGINT), console._die_of_signal)
            # a Ctrl-C caught before the switch makes it an interrupted command
            signal.signal(signal.SIGINT, before)
            with mock.patch.object(console, "settle_interrupts",
                                   side_effect=[KeyboardInterrupt, None]) as settle:
                self.assertEqual(at_top_level(launcher)["status"], 130)
            self.assertEqual(settle.call_count, 2)
        script = (ROOT / "cli.py").read_text(encoding="utf-8").rsplit(
            'if __name__ == "__main__":', 1)[1]
        self.assertIn("console.settle_interrupts()", script)
        self.assertIn("except KeyboardInterrupt:", script)
        self.assertIn("os._exit(130)", script)

    def test_a_ctrl_c_on_the_settle_loop_itself_ends_the_process_quietly(self) -> None:
        # the trace hook raises the second Ctrl-C at the loop head, outside the inner try,
        # once the first one made the status 130
        import os
        import subprocess
        import tempfile
        script = (
            "import sys\n"
            "from unittest import mock\n"
            "from agrep.__main__ import main\n"
            "import cli, console, resident\n"
            "def settle():\n"
            "    settle.calls += 1\n"
            "    if settle.calls == 1:\n"
            "        raise KeyboardInterrupt\n"
            "settle.calls = 0\n"
            "def local(frame, event, arg):\n"
            "    if event == 'line' and frame.f_locals.get('status') == 130:\n"
            "        raise KeyboardInterrupt\n"
            "    return local\n"
            "def tracer(frame, event, arg):\n"
            "    return local if frame.f_code is main.__code__ else None\n"
            "with mock.patch.object(resident, 'try_run', return_value=None), \\\n"
            "        mock.patch.object(cli, 'main', return_value=0), \\\n"
            "        mock.patch.object(console, 'settle_interrupts', settle):\n"
            "    sys.settrace(tracer)\n"
            "    status = main()\n"
            "print('survived', flush=True)\n")
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "agrep-launcher.py"
            launcher.write_text(script, encoding="utf-8")
            done = subprocess.run(
                [sys.executable, str(launcher)], capture_output=True, timeout=60,
                env={**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py")))})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (130, b"", b""))

    @unittest.skipUnless(sys.platform != "win32", "a prompt on a pseudo-terminal")
    def test_main_run_at_a_prompt_leaves_ctrl_c_with_the_prompt(self) -> None:
        import tempfile
        lines = ("import cli, resident",
                 "cli.main = lambda: 0; resident.try_run = lambda: None",
                 "from agrep.__main__ import main",
                 "print('main', 'returned', main())")
        self._ctrl_c_at_the_prompt([], {}, lines)
        # PYTHONINSPECT, at startup or set by the script, opens a prompt once the script ends
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "agrep-launcher.py"
            for setup, extra in (((), {"PYTHONINSPECT": "1"}),
                                 (("import os; os.environ['PYTHONINSPECT'] = '1'",), {})):
                with self.subTest(setup=setup, env=extra):
                    script.write_text("\n".join((*setup, *lines)) + "\n", encoding="utf-8")
                    self._ctrl_c_at_the_prompt([str(script)], extra, ())

    def _ctrl_c_at_the_prompt(self, argv: list, extra: dict, typed: tuple) -> None:
        import fcntl
        import os
        import pty
        import select
        import subprocess
        import termios
        import time
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py"))),
               "PYTHON_BASIC_REPL": "1", "TERM": "dumb"}
        env.pop("PYTHONSTARTUP", None)
        env.pop("PYTHONINSPECT", None)
        prompt = subprocess.Popen(
            [sys.executable, "-q", *argv], stdin=slave, stdout=slave, stderr=slave,
            env={**env, **extra}, start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.addCleanup(lambda: prompt.poll() is None and prompt.kill())
        seen = bytearray()

        def read(until_text: str = "", start: int = 0) -> int:
            """Read the prompt's output until `until_text` shows, or until it exits;
            its terminal's last close waits for this end to read what is queued."""
            deadline = time.monotonic() + 30
            while True:
                at = seen.find(until_text.encode(), start) if until_text else -1
                if at >= 0:
                    return at + len(until_text)
                exited = prompt.poll() is not None
                if exited and not until_text:
                    return len(seen)
                if exited or time.monotonic() > deadline:
                    self.fail(f"no {until_text!r}: exit {prompt.poll()}, "
                              f"tail {bytes(seen[-300:])!r}")
                if select.select([master], [], [], 0.1)[0]:
                    with contextlib.suppress(OSError):  # EIO once the prompt is gone
                        seen.extend(os.read(master, 4096))

        def type_line(line: str) -> None:
            os.write(master, line.encode() + b"\n")

        at = read(">>> ") if typed else 0
        for line in typed:
            type_line(line)
        at = read(">>> ", read("main returned 0", at))
        os.write(master, b"\x03")  # Ctrl-C at the prompt
        at = read("KeyboardInterrupt", at)
        type_line("print('still', 'here')")
        read("still here", at)
        # os._exit: 3.14's prompt reports any later exit as SIGINT once it has caught a Ctrl-C
        type_line("import os; os._exit(0)")
        read()
        self.assertEqual(prompt.wait(30), 0)

    def test_audit_help_reaches_the_real_option_parser(self) -> None:
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["agrep", "audit", "--help"]), \
                redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli._main()
        self.assertEqual(raised.exception.code, 0)
        rendered = output.getvalue()
        for option in ("--agent", "--json", "--strict", "--full"):
            self.assertIn(option, rendered)

    def test_top_level_help_leads_with_agent_commands_and_lists_every_name(self) -> None:
        import re
        import surface_policy as surface
        rendered = {}
        for flag in ("--help", "-h"):
            output, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["agrep", flag]), \
                    redirect_stdout(output), redirect_stderr(err):
                self.assertEqual(cli._main(), 0)
            self.assertEqual(err.getvalue(), "")
            rendered[flag] = output.getvalue()
        self.assertEqual(rendered["-h"], rendered["--help"])
        page = rendered["--help"]
        head, separator, tail = page.partition("other commands:")
        self.assertTrue(separator, page)
        lead = re.findall(r"^  ([a-z]+) ", head, flags=re.M)
        self.assertEqual(lead, ["search", "around", "recall", "chats", "postcompact",
                                "board", "resume", "status", "why", "summary"])
        for name in lead:
            example = r'^ {17}agrep "' if name == "search" else rf"^ {{17}}agrep {name}\b"
            self.assertRegex(head, re.compile(example, re.M))
        other = set(re.findall(r"[a-z]+", tail.split("\n\n", 1)[0]))
        hidden = {"live", "inject", "up"}
        self.assertEqual(set(lead) | other, surface.RESERVED_COMMAND_WORDS - hidden)
        self.assertFalse(set(lead) & other)


class OneShotLiveSurfaceTests(unittest.TestCase):
    class Watcher:
        def __init__(
                self, *, booting: bool = False,
                sessions: list[dict] | None = None) -> None:
            self.booting = booting
            self.sessions = sessions or []

        def wait_boot(self, timeout: float | None = None) -> bool:
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
