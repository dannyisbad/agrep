"""One colour rule for every surface: an explicit --color wins, NO_COLOR (set and
non-empty, no-color.org) disables, FORCE_COLOR (set and non-empty) forces, then a
tty that renders VT. The surfaces are driven through a real pty, as a user sees them."""
from __future__ import annotations

import errno
import io
import os
try:
    import pty
except ImportError:
    pty = None
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from _test_support import fixture_tree, isolate_data_dir, publish_derived_generation

isolate_data_dir()

import common  # noqa: E402
import console  # noqa: E402
import corpusdb  # noqa: E402
from hookless import _log, native  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ESC = b"\x1b["
RUN_TIMEOUT_S = 120.0


def _tty_stream() -> io.StringIO:
    stream = io.StringIO()
    stream.isatty = lambda: True
    return stream


class ColorRuleTests(unittest.TestCase):
    def _auto(self, stream, env: dict[str, str]) -> bool:
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(console, "enable_vt", return_value=True):
            return common.color_enabled(stream)

    def test_no_color_disables_a_tty_only_when_set_and_non_empty(self) -> None:
        tty = _tty_stream()
        self.assertTrue(self._auto(tty, {}))
        self.assertFalse(self._auto(tty, {"NO_COLOR": "1"}))
        self.assertFalse(self._auto(tty, {"NO_COLOR": "anything"}))
        self.assertTrue(self._auto(tty, {"NO_COLOR": ""}))

    def test_force_color_colours_a_pipe_and_no_color_beats_it(self) -> None:
        pipe = io.StringIO()
        self.assertFalse(self._auto(pipe, {}))
        self.assertTrue(self._auto(pipe, {"FORCE_COLOR": "1"}))
        self.assertFalse(self._auto(pipe, {"FORCE_COLOR": ""}))
        self.assertFalse(self._auto(pipe, {"FORCE_COLOR": "1", "NO_COLOR": "1"}))
        self.assertFalse(self._auto(_tty_stream(), {"FORCE_COLOR": "1", "NO_COLOR": "1"}))

    def test_an_explicit_flag_beats_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertTrue(common.color_enabled(io.StringIO(), "always"))
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True), \
                mock.patch.object(console, "enable_vt", return_value=True):
            self.assertFalse(common.color_enabled(_tty_stream(), "never"))

    def test_debug_traces_follow_the_rule(self) -> None:
        for logger in (console, _log):
            with self.subTest(logger=logger.__name__):
                stream = _tty_stream()
                with mock.patch.object(logger, "DEBUG", True), \
                        mock.patch.object(console, "enable_vt", return_value=True), \
                        mock.patch.object(sys, "stderr", stream), \
                        mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
                    logger.dbg("quiet trace")
                self.assertIn("quiet trace", stream.getvalue())
                self.assertNotIn("\x1b", stream.getvalue())
                stream = io.StringIO()
                with mock.patch.object(logger, "DEBUG", True), \
                        mock.patch.object(sys, "stderr", stream), \
                        mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
                    logger.dbg("forced trace")
                self.assertIn("\x1b[2m", stream.getvalue())

    def test_forced_colour_still_switches_on_console_vt(self) -> None:
        """Forced colour on a tty still enables VT processing (conhost prints the
        escapes literally otherwise); a pipe or a disabled surface never asks."""
        for env, when in (({"FORCE_COLOR": "1"}, "auto"), ({}, "always"), ({"NO_COLOR": "1"}, "always")):
            with self.subTest(env=env, when=when), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(console, "enable_vt", return_value=False) as vt:
                self.assertTrue(common.color_enabled(_tty_stream(), when))
                vt.assert_called_once()
                vt.reset_mock()
                self.assertTrue(common.color_enabled(io.StringIO(), when))
                vt.assert_not_called()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True), \
                mock.patch.object(console, "enable_vt") as vt:
            self.assertFalse(common.color_enabled(_tty_stream()))
            vt.assert_not_called()
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True), \
                mock.patch.object(native, "_vt_ok", return_value=False) as vt_ok:
            self.assertIn("\x1b[2m", native._dim("dim"))
            vt_ok.assert_called_once()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True), \
                mock.patch.object(native, "_vt_ok", return_value=True) as vt_ok:
            self.assertEqual(native._dim("plain"), "plain")
            vt_ok.assert_not_called()

    def test_hookless_dim_follows_the_rule(self) -> None:
        with mock.patch.object(native, "_vt_ok", return_value=True), \
                mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertEqual(native._dim("plain"), "plain")
        with mock.patch.object(native, "_vt_ok", return_value=False), \
                mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            self.assertIn("\x1b[2m", native._dim("dim"))
        with mock.patch.object(native, "_vt_ok", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("\x1b[2m", native._dim("dim"))

@unittest.skipUnless(pty is not None, "real pty support is POSIX-only")
class ColorSurfaceTests(unittest.TestCase):
    """Each colour surface through a real pty: the status page, doctor, the board,
    and recall with an explicit flag over a one-row published generation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree = fixture_tree(prefix="agrep-colour-")
        root = Path(cls._tree.__enter__())
        cls.home = root / "home"
        cls.data = root / "data"
        cls.home.mkdir()
        row = {
            "id": "codex:colour-session:0", "agent": "codex", "project": "colour",
            "session": "colour-session", "ts": 1, "turn": 0,
            "text": "fixture needle", "who": "user", "model": "fixture",
        }
        publish_derived_generation(
            cls.data, [row], common, corpusdb, signature="colour-generation",
            replies=[{"id": row["id"], "reply": "fixture answer"}])
        cls.env = {
            "HOME": str(cls.home), "AGREP_HOME": str(cls.home),
            "AGREP_DATA_DIR": str(cls.data), "AGREP_DATA_READONLY": str(cls.data),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "TERM": "xterm-256color",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1", "AGREP_NO_RESIDENT": "1",
            "AGREP_NO_FETCH": "1", "PYTHONPATH": f"{ROOT}:{ROOT / 'py'}",
        }
        if "AGREP_RS_BIN" in os.environ:
            cls.env["AGREP_RS_BIN"] = os.environ["AGREP_RS_BIN"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree.__exit__(None, None, None)

    def _run(self, argv: list[str], extra: dict[str, str], *, tty: bool) -> bytes:
        env = {**self.env, **extra}
        command = [sys.executable, str(ROOT / "cli.py"), *argv]
        if not tty:
            result = subprocess.run(
                command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=RUN_TIMEOUT_S)
            return result.stdout + result.stderr
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=slave, stderr=slave)
            os.close(slave)
            slave = -1
            body = bytearray()
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                body.extend(chunk)
            process.wait(timeout=RUN_TIMEOUT_S)
            return bytes(body)
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)

    SURFACES = {
        "status page": [],
        "doctor": ["doctor"],
        "board": ["board", "--once"],
    }

    def test_no_color_silences_every_auto_surface_on_a_tty(self) -> None:
        for name, argv in self.SURFACES.items():
            with self.subTest(surface=name):
                coloured = self._run(argv, {"NO_COLOR": ""}, tty=True)
                self.assertIn(ESC, coloured, "the tty was not painted at all")
                plain = self._run(argv, {"NO_COLOR": "1"}, tty=True)
                self.assertNotIn(ESC, plain)
                self.assertTrue(plain.strip(), "the surface rendered nothing")

    def test_no_color_beats_force_color(self) -> None:
        for name, argv in self.SURFACES.items():
            with self.subTest(surface=name):
                plain = self._run(argv, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, tty=True)
                self.assertNotIn(ESC, plain)

    def test_force_color_paints_a_pipe(self) -> None:
        for name, argv in self.SURFACES.items():
            with self.subTest(surface=name):
                self.assertNotIn(ESC, self._run(argv, {}, tty=False))
                self.assertIn(ESC, self._run(argv, {"FORCE_COLOR": "1"}, tty=False))

    def test_an_explicit_always_beats_no_color(self) -> None:
        argv = ["recall", "fixture", "--color", "always", "--no-auto"]
        painted = self._run(argv, {"NO_COLOR": "1"}, tty=True)
        self.assertIn(b"fixture needle", painted)
        self.assertIn(ESC, painted)
        plain = self._run(["recall", "fixture", "--no-auto"], {"NO_COLOR": "1"}, tty=True)
        self.assertIn(b"fixture needle", plain)
        self.assertNotIn(ESC, plain)


if __name__ == "__main__":
    unittest.main()
