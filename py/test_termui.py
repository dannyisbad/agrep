"""The shared terminal layer of `agrep live` (`agrep board`): keys and screen.

Keys are decoded from real descriptors, the screen is entered and left on a
real pseudo-terminal, and the Windows console paths run against recorded
input records and fake console calls, on every platform. The end-to-end exit
paths (signals, Ctrl-Z, `&`, a dead terminal) run the CLI itself in
test_board_terminal. The POSIX classes skip where there are no pseudo-terminals.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import io
import os
try:
    import pty
except ImportError:  # Windows: no pseudo-terminals (pty needs termios)
    pty = None
import signal
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import console  # noqa: E402
import termui  # noqa: E402

POSIX = os.name == "posix"
PTYS = POSIX and pty is not None


class _Stdin:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def fileno(self) -> int:
        return self.fd


@unittest.skipUnless(POSIX, "POSIX key decoding")
class KeyTests(unittest.TestCase):
    def setUp(self) -> None:
        termui._keyboard = None
        self.addCleanup(setattr, termui, "_keyboard", None)

    def keys(self, *chunks: bytes, close: bool = True, gap: float = 0.0) -> list[str]:
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        if gap:
            def feed():
                for chunk in chunks:
                    os.write(write, chunk)
                    time.sleep(gap)
                os.close(write)
            threading.Thread(target=feed, daemon=True).start()
        else:
            os.write(write, b"".join(chunks))
            if close:
                os.close(write)
            else:
                self.addCleanup(os.close, write)
        out = []
        deadline = time.monotonic() + 5
        with mock.patch.object(sys, "stdin", _Stdin(read)):
            while time.monotonic() < deadline:
                key = termui.getkey()
                if key == "eof":
                    break
                if key:
                    out.append(key)
                elif not close and not gap:
                    break
                else:
                    time.sleep(0.01)
        return out

    def test_navigation_and_editing_keys(self) -> None:
        self.assertEqual(self.keys(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[5~\x1b[6~\x1b[H\x1b[F"
                                   b"\x1bOH\x1bOF\x1b[1~\x1b[4~\x1b[3~\x1b[1;5A"),
                         ["up", "down", "right", "left", "pgup", "pgdn", "home", "end",
                          "home", "end", "home", "end", "delete", "up"])
        self.assertEqual(self.keys(b"\x7f\x08\r\n"), ["backspace", "backspace", "enter", "enter"])

    def test_typed_characters_are_whole_utf8_sequences(self) -> None:
        self.assertEqual(self.keys("é日👩q".encode()), ["é", "日", "👩", "q"])

    def test_esc_alone_double_esc_and_esc_then_a_key(self) -> None:
        self.assertEqual(self.keys(b"\x1b"), ["esc"])
        # one read holding both keys: neither is swallowed
        self.assertEqual(self.keys(b"\x1b\x1b"), ["esc", "esc"])
        self.assertEqual(self.keys(b"\x1bq"), ["esc", "q"])
        self.assertEqual(self.keys(b"\x1bO"), ["esc", "O"])

    def test_unknown_or_cut_sequences_decode_to_nothing(self) -> None:
        self.assertEqual(self.keys(b"\x1b[I\x1b[<0;3;4Mq"), ["q"])
        self.assertEqual(self.keys(b"\x1b[201~q"), ["q"])

    def test_a_bracketed_paste_is_one_paste_key_never_keys(self) -> None:
        keys = self.keys(b"\x1b[200~a1\rb2c\rrq\r3 \xc3\xa9\x1b[201~q")
        self.assertEqual(keys, ["paste", "q"])
        paste = keys[0]
        self.assertIsInstance(paste, termui.Paste)
        self.assertEqual(paste.text, "a1\rb2c\rrq\r3 é")
        # no binding a view compares against can match it
        self.assertNotIn(paste, ("q", "r", "a", "enter", "1"))
        self.assertFalse(paste.isdigit())

    def test_a_paste_split_across_reads_and_one_without_an_end_marker(self) -> None:
        keys = self.keys(b"\x1b[200~line one\r", b"line two\x1b[20", b"1~q", gap=0.05)
        self.assertEqual(keys, ["paste", "q"])
        self.assertEqual(keys[0].text, "line one\rline two")
        with mock.patch.object(termui, "_PASTE_QUIET_S", 0.05):
            keys = self.keys(b"\x1b[200~cut short", close=False)
        self.assertEqual([key.text for key in keys], ["cut short"])

    def test_the_end_of_input_reads_eof_every_time(self) -> None:
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        os.close(write)
        with mock.patch.object(sys, "stdin", _Stdin(read)):
            self.assertEqual([termui.getkey() for _ in range(3)], ["eof"] * 3)

    def test_eio_from_a_hung_up_terminal_reads_as_eof(self) -> None:
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b"x")
        with mock.patch.object(sys, "stdin", _Stdin(read)), \
                mock.patch.object(termui.os, "read", side_effect=OSError(errno.EIO, "EIO")):
            self.assertEqual(termui.getkey(), "eof")


@unittest.skipUnless(PTYS, "POSIX pseudo-terminals")
class ScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.stdin = _Stdin(self.slave)
        self.stdout = io.StringIO()

    def test_entering_sets_cbreak_without_flow_control_and_leaving_restores_it(self) -> None:
        import termios
        before = termios.tcgetattr(self.slave)
        owner = termui.Screen(self.stdin, self.stdout)
        owner.enter()
        inside = termios.tcgetattr(self.slave)
        self.assertFalse(inside[0] & termios.IXON)
        self.assertFalse(inside[3] & (termios.ECHO | termios.ICANON))
        self.assertTrue(inside[3] & termios.ISIG)
        self.assertEqual((inside[6][termios.VMIN], inside[6][termios.VTIME]), (1, 0))
        owner.leave()
        self.assertEqual(termios.tcgetattr(self.slave), before)
        self.assertEqual(self.stdout.getvalue(), termui.ENTER_SCREEN + termui.LEAVE_SCREEN)

    def test_the_screen_strings_cover_paste_cursor_colours_and_buffer(self) -> None:
        self.assertEqual(termui.ENTER_SCREEN, "\x1b[?1049h\x1b[?25l\x1b[?2004h")
        self.assertTrue(termui.LEAVE_SCREEN.startswith("\x1b[0m"))
        for part in ("\x1b[?2004l", "\x1b[?25h", "\x1b[?1049l"):
            self.assertIn(part, termui.LEAVE_SCREEN)

    def test_leaving_holds_the_escapes_until_drained_and_never_raises(self) -> None:
        # the user's modes first, but with NOFLSH set and flow control off; the
        # escapes; the drain; then the exact modes, though every call failed
        import termios
        calls = []
        owner = termui.Screen(self.stdin, self.stdout)
        owner.enter()
        saved = owner.saved

        class DeadOutput(io.StringIO):
            def write(self, text):
                calls.append("escapes")
                raise OSError(errno.EIO, "EIO")

        owner.stdout = DeadOutput()

        def restore(fd, when, state):
            calls.append(("modes", when, state[0] & termios.IXON, state[3] & termios.NOFLSH))
            raise termios.error(errno.EIO, "EIO")

        def drain(fd):
            calls.append("drain")
            raise termios.error(errno.EIO, "EIO")

        with mock.patch.object(termui.termios, "tcsetattr", side_effect=restore), \
                mock.patch.object(termui.termios, "tcdrain", side_effect=drain):
            owner.leave()
        self.assertEqual(calls, [
            ("modes", termios.TCSAFLUSH, 0, termios.NOFLSH), "escapes", "drain",
            ("modes", termios.TCSANOW, saved[0] & termios.IXON, saved[3] & termios.NOFLSH)])
        self.assertEqual((owner.saved, owner.shown), (None, False))

    def test_ctrl_c_is_held_while_the_escapes_drain(self) -> None:
        owner = termui.Screen(self.stdin, self.stdout)  # nothing queued on the pty
        owner.enter()
        held = []

        def drain(fd):
            held.append(signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, ()))

        with mock.patch.object(termui.termios, "tcdrain", side_effect=drain):
            owner.leave()
        self.assertEqual(held, [True])
        self.assertNotIn(signal.SIGINT, signal.pthread_sigmask(signal.SIG_BLOCK, ()))

    def test_unread_input_is_discarded_on_leave(self) -> None:
        owner = termui.Screen(self.stdin, self.stdout)
        owner.enter()
        os.write(self.master, b"echo typed-ahead\r")
        time.sleep(0.1)
        owner.leave()
        import select
        self.assertEqual(select.select([self.slave], [], [], 0.2)[0], [])

    def test_a_fatal_signal_leave_never_waits_on_a_stalled_terminal(self) -> None:
        import fcntl
        import termios
        before = termios.tcgetattr(self.slave)
        out = _Stdin(self.slave)  # the screen's output is the terminal itself
        owner = termui.Screen(self.stdin, out)
        owner.enter()
        # nobody reads the other end, as in a frozen ssh session: fill its queue
        flags = fcntl.fcntl(self.slave, fcntl.F_GETFL)
        fcntl.fcntl(self.slave, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(self.slave, b"x" * 4096)
        fcntl.fcntl(self.slave, fcntl.F_SETFL, flags)
        begun = time.monotonic()
        owner.leave(urgent=True)
        self.assertLess(time.monotonic() - begun, 2.0)
        after = termios.tcgetattr(self.slave)
        # macOS sets PENDIN whenever the line discipline returns to canonical mode
        pendin = getattr(termios, "PENDIN", 0)
        before[3], after[3] = before[3] & ~pendin, after[3] & ~pendin
        self.assertEqual(after, before)
        self.assertFalse(fcntl.fcntl(self.slave, fcntl.F_GETFL) & os.O_NONBLOCK)

    def test_a_fatal_leave_from_any_point_of_the_main_path_restores_everything(self) -> None:
        # A fatal signal can land between any two terminal operations: inside
        # enter(), before a frame, inside a normal leave(). Its urgent leave runs
        # from there, then the process dies (here: _Died) and nothing resumes.
        import fcntl
        import select
        import termios

        class _Died(BaseException):
            pass

        # the far end reads all along, so a normal leave's TCSAFLUSH can drain
        drawn, stop = bytearray(), threading.Event()

        def read_all() -> None:
            while not stop.is_set():
                if select.select([self.master], [], [], 0.02)[0]:
                    drawn.extend(os.read(self.master, 65536))
        reader = threading.Thread(target=read_all, daemon=True)
        reader.start()
        self.addCleanup(reader.join, 2)
        self.addCleanup(stop.set)
        pendin = getattr(termios, "PENDIN", 0)
        before = termios.tcgetattr(self.slave)
        real_call, real_write = termui._tty_call, termui._write
        point = 0
        while True:
            point += 1
            seen, start = [0], len(drawn)
            owner = termui.Screen(self.stdin, _Stdin(self.slave))

            def operation(real, *args, owner=owner, seen=seen, point=point):
                seen[0] += 1
                if seen[0] == point:
                    with mock.patch.object(termui, "_tty_call", real_call), \
                            mock.patch.object(termui, "_write", real_write):
                        owner.leave(urgent=True)
                    raise _Died
                return real(*args)
            with mock.patch.object(termui, "_tty_call",
                                   side_effect=lambda *a: operation(real_call, *a)), \
                    mock.patch.object(termui, "_write",
                                      side_effect=lambda *a: operation(real_write, *a)):
                try:
                    owner.enter()
                    termui.repaint(["one frame", "of two lines"], stdout=owner.stdout)
                    owner.leave()
                    break  # past the last operation: every point was taken
                except _Died:
                    pass
            time.sleep(0.2)  # the reader catches up
            with self.subTest(point=point):
                after = termios.tcgetattr(self.slave)
                before[3], after[3] = before[3] & ~pendin, after[3] & ~pendin
                self.assertEqual(after, before)
                self.assertFalse(fcntl.fcntl(self.slave, fcntl.F_GETFL) & os.O_NONBLOCK)
                output = bytes(drawn[start:])
                shown = output.rfind(termui.ENTER_SCREEN.encode())
                self.assertTrue(shown < 0 or termui.LEAVE_SCREEN.encode() in output[shown:])
        # enter (modes read, cbreak, screen), one frame, leave (modes held,
        # screen, drain, exact modes)
        self.assertEqual(point, 9)

    def test_a_fatal_leave_rewrites_escapes_its_flush_discarded(self) -> None:
        # A Linux pty's TCIOFLUSH also drops output the master side has not read yet, the
        # hand-back's drained escapes included. Model that worst case: a flush discards
        # everything written so far, and whatever survives must still leave the screen.
        import termios

        class _Died(BaseException):
            pass

        real_call = termui._tty_call
        surviving: list[str] = []

        def write(_stream, text: str) -> None:
            surviving.append(text)

        def tty_call(function, *args):
            if function is termios.tcflush:
                surviving.clear()
                return None
            return real_call(function, *args)

        before = termios.tcgetattr(self.slave)
        # enter is 1-3, the frame 4, the hand-back's escapes 6, its drain 7, its last call 8;
        # "returned" lands after the hand-back, before leave() lets go of the saved modes
        for point in (5, 6, 7, 8, "returned"):
            with self.subTest(point=point):
                surviving.clear()
                seen = [0]
                owner = termui.Screen(self.stdin, self.stdout)

                def die(owner=owner):
                    with mock.patch.object(termui, "_tty_call", tty_call), \
                            mock.patch.object(termui, "_write", write):
                        owner.leave(urgent=True)
                    raise _Died

                def operation(real, *args, seen=seen, point=point, die=die):
                    seen[0] += 1
                    if seen[0] == point:
                        die()
                    return real(*args)

                def hand_back_then_die(fd, real=owner._hand_back_in_order, die=die):
                    real(fd)
                    die()

                with mock.patch.object(
                        termui, "_tty_call",
                        side_effect=lambda *a: operation(tty_call, *a)), \
                        mock.patch.object(
                            termui, "_write",
                            side_effect=lambda *a: operation(write, *a)), \
                        self.assertRaises(_Died):
                    if point == "returned":
                        owner._hand_back_in_order = hand_back_then_die
                    owner.enter()
                    termui.repaint(["one frame", "of two lines"], stdout=owner.stdout)
                    owner.leave()
                self.assertEqual("".join(surviving), termui.LEAVE_SCREEN)
                after = termios.tcgetattr(self.slave)
                # macOS sets PENDIN whenever the line discipline returns to canonical mode
                pendin = getattr(termios, "PENDIN", 0)
                before[3], after[3] = before[3] & ~pendin, after[3] & ~pendin
                self.assertEqual(after, before)

    def test_a_second_fatal_signal_inside_the_first_handler_is_turned_away(self) -> None:
        # SIGHUP lands while the SIGTERM handler writes the escapes, inside its
        # restore; the process dies of SIGTERM after exactly one restore
        import termios

        class _Died(BaseException):
            pass

        owner = termui.Screen(self.stdin, _Stdin(self.slave))
        owner.enter()
        deaths, fired, masks, alarms = [], [], [], []
        blocked, pending = set(), set()
        real_write = termui._write
        flushes = mock.Mock(wraps=termios.tcflush)

        def second_signal(stream, text):
            if text == termui.LEAVE_SCREEN and not fired:
                fired.append(True)
                owner._on_fatal(signal.SIGHUP, None)
            real_write(stream, text)

        def die(signum):
            if not deaths:  # the first death ends the process; nothing after it runs
                deaths.append((signum, flushes.call_count))
            raise _Died

        # the kernel's part: a raised signal kills at once unless blocked, then
        # it waits until unblocked
        def raise_signal(signum):
            if signum in blocked:
                pending.add(signum)
            else:
                die(signum)

        def sigmask(how, signals):
            masks.append((how, set(signals)))
            if how == signal.SIG_BLOCK:
                blocked.update(signals)
            else:
                blocked.difference_update(signals)
                for signum in pending & set(signals):
                    die(signum)
            return set()
        with mock.patch.object(termui, "_write", side_effect=second_signal), \
                mock.patch.object(termui.termios, "tcflush", flushes), \
                mock.patch.object(termui.signal, "pthread_sigmask", create=True,
                                  side_effect=sigmask), \
                mock.patch.object(termui.signal, "alarm", create=True, side_effect=alarms.append), \
                mock.patch.object(termui.signal, "signal"), \
                mock.patch.object(termui.signal, "raise_signal", side_effect=raise_signal), \
                mock.patch.object(termui.os, "_exit", side_effect=die), \
                self.assertRaises(_Died):
            owner._on_fatal(signal.SIGTERM, None)
        self.assertTrue(fired)
        self.assertEqual(deaths, [(signal.SIGTERM, 1)])
        handled = {signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGINT}
        self.assertEqual(masks[0], (signal.SIG_BLOCK, handled))
        self.assertEqual(alarms, [termui._LAST_RESORT_S])

    def test_a_stop_and_continue_retries_and_other_errors_surface_as_oserror(self) -> None:
        import termios
        flaky = mock.Mock(side_effect=[termios.error(errno.EINTR, "EINTR"), "done"])
        self.assertEqual(termui._tty_call(flaky, 1), "done")
        broken = mock.Mock(side_effect=termios.error(errno.ENOTTY, "ENOTTY"))
        with self.assertRaises(OSError) as raised:
            termui._tty_call(broken, 1)
        self.assertEqual(raised.exception.errno, errno.ENOTTY)

    def test_frames_go_straight_to_the_descriptor(self) -> None:
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        stream = io.TextIOWrapper(io.BufferedWriter(io.FileIO(write, "w", closefd=False)))
        with mock.patch.object(stream, "write", side_effect=AssertionError("buffered write")):
            termui.repaint(["one", "twö"], stream)
        self.assertEqual(os.read(read, 100).decode(),
                         "\x1b[Hone\x1b[K\ntwö\x1b[K\n\x1b[0J")

    def test_ctrl_z_suspends_from_the_loop_and_resumes_with_a_full_repaint(self) -> None:
        import termios
        before = termios.tcgetattr(self.slave)
        seen = []
        with mock.patch.object(termui.signal, "raise_signal",
                               side_effect=lambda signum: seen.append(
                                   (signum, termios.tcgetattr(self.slave),
                                    self.stdout.getvalue()))), \
                mock.patch.object(sys, "stdin", self.stdin), \
                termui.screen(self.stdin, self.stdout) as owner:
            # the handler only notes the stop: no I/O while a write may be under way
            owner.stdout = mock.Mock(write=mock.Mock(side_effect=AssertionError("I/O")))
            owner._on_stop(signal.SIGTSTP, None)
            self.assertTrue(owner.stop_requested)
            owner.stdout = self.stdout
            owner.key()
            self.assertTrue(owner.resumed)
        signum, stopped_mode, written = seen[0]
        self.assertEqual(signum, signal.SIGTSTP)
        self.assertEqual(stopped_mode, before)
        self.assertTrue(written.endswith(termui.LEAVE_SCREEN))
        self.assertEqual(self.stdout.getvalue().count(termui.ENTER_SCREEN), 2)

    def test_handlers_are_installed_for_the_screen_and_restored_after(self) -> None:
        watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
                   signal.SIGTSTP, signal.SIGPIPE)
        before = {signum: signal.getsignal(signum) for signum in watched}
        with termui.screen(self.stdin, self.stdout) as owner:
            self.assertEqual(signal.getsignal(signal.SIGTERM), owner._on_fatal)
            self.assertEqual(signal.getsignal(signal.SIGTSTP), owner._on_stop)
            self.assertEqual(signal.getsignal(signal.SIGINT), owner._on_interrupt)
            self.assertEqual(signal.getsignal(signal.SIGPIPE), signal.SIG_IGN)
        self.assertEqual({signum: signal.getsignal(signum) for signum in watched}, before)

    def test_an_inherited_ignore_stays_ignored(self) -> None:
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        with termui.screen(self.stdin, self.stdout):
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_one_ctrl_c_ends_the_view_and_the_rest_of_a_storm_is_absorbed(self) -> None:
        owner = termui.Screen(self.stdin, self.stdout)
        owner.leaving = True
        owner._on_interrupt(signal.SIGINT, None)  # mid-restore: absorbed
        owner.leaving = False
        with self.assertRaises(KeyboardInterrupt):
            owner._on_interrupt(signal.SIGINT, None)
        for _ in range(100):
            owner._on_interrupt(signal.SIGINT, None)

    def sweep(self, run, source: str, check=None) -> int:
        """Run `run` once per line it executes in `source`, a trace hook firing SIGINT
        at that line, and check what the screen hands back each time (and `check`,
        if given); returns the number of lines swept."""
        import termios
        pendin = getattr(termios, "PENDIN", 0)
        watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
                   signal.SIGTSTP, signal.SIGPIPE)
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        signal.signal(signal.SIGINT, lambda _signum, _frame: None)  # the caller's own
        handlers = {signum: signal.getsignal(signum) for signum in watched}
        saved = termios.tcgetattr(self.slave)

        def masked(modes: list) -> list:
            return [*modes[:3], modes[3] & ~pendin, *modes[4:]]
        fire = signal.raise_signal
        point = 0
        while True:
            point += 1
            lines, fired = [0], []

            def trace(frame, event, _arg, lines=lines, fired=fired, point=point):
                if frame.f_code.co_filename != source:
                    return None
                if event == "line":
                    lines[0] += 1
                    if lines[0] == point:
                        fired.append(True)
                        fire(signal.SIGINT)
                return trace
            out = io.StringIO()
            sys.settrace(trace)
            try:
                run(out)
            except KeyboardInterrupt:
                pass
            finally:
                sys.settrace(None)
            if not fired:
                return point - 1
            with self.subTest(line=point):
                if check is not None:
                    check()
                self.assertEqual(masked(termios.tcgetattr(self.slave)), masked(saved))
                drawn = out.getvalue()
                self.assertGreaterEqual(drawn.rfind(termui.LEAVE_SCREEN),
                                        drawn.rfind(termui.ENTER_SCREEN))
                self.assertEqual({signum: signal.getsignal(signum) for signum in watched},
                                 handlers)
            # a failing line must not leave its terminal or handlers to the next one
            termios.tcsetattr(self.slave, termios.TCSANOW, saved)
            for signum, handler in handlers.items():
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)

    def test_a_ctrl_c_at_any_line_of_the_screen_still_hands_everything_back(self) -> None:
        def view(out) -> None:
            with termui.screen(self.stdin, out):
                pass
        swept = self.sweep(view, termui.Screen.leave.__code__.co_filename)
        self.assertGreater(swept, 60)  # setup, enter, leave and the close all swept

    def test_a_ctrl_c_at_any_line_of_the_fatal_handler_cannot_abandon_it(self) -> None:
        class _Died(BaseException):
            pass
        deaths: list = []

        def die(signum) -> None:
            if not deaths:  # the first death ends the process; nothing after it runs
                deaths.append(signum)
            raise _Died

        def dying(out) -> None:
            deaths.clear()
            owner = termui.Screen(self.stdin, out)
            owner.enter()
            caller = signal.signal(signal.SIGINT, owner._on_interrupt)
            try:
                with mock.patch.object(termui.signal, "pthread_sigmask", create=True), \
                        mock.patch.object(termui.signal, "alarm", create=True), \
                        mock.patch.object(termui.signal, "signal"), \
                        mock.patch.object(termui.signal, "raise_signal", side_effect=die), \
                        mock.patch.object(termui.os, "_exit", side_effect=die):
                    owner._on_fatal(signal.SIGTERM, None)
            except _Died:
                pass
            finally:
                signal.signal(signal.SIGINT, caller)
        swept = self.sweep(dying, termui.Screen.leave.__code__.co_filename,
                           check=lambda: self.assertEqual(deaths, [signal.SIGTERM]))
        self.assertGreater(swept, 20)  # enter, the fatal handler and its urgent leave


class WindowsKeyTests(unittest.TestCase):
    """Console input records as a Windows console reports them."""

    @staticmethod
    def keys(*records) -> list[str]:
        queue = list(records)
        reader = termui._ConsoleKeys(lambda: queue.pop(0) if queue else None)
        out = []
        while key := reader.next():
            out.append(key)
        return out

    @staticmethod
    def char(text: str, down: int = 1, repeat: int = 1):
        """One record per UTF-16 code unit, as a console reports typed text."""
        data = text.encode("utf-16-le")
        return [(termui._KEY_EVENT, down, repeat, 0, int.from_bytes(data[i:i + 2], "little"))
                for i in range(0, len(data), 2)]

    def test_a_typed_a_grave_is_never_an_arrow(self) -> None:
        up = (termui._KEY_EVENT, 1, 1, 0x26, 0)
        self.assertEqual(self.keys(*self.char("à")), ["à"])
        self.assertEqual(self.keys(*self.char("voilà Paris")), list("voilà Paris"))
        self.assertEqual(self.keys(*self.char("à"), *self.char("H")), ["à", "H"])
        self.assertEqual(self.keys(up, *self.char("à")), ["up", "à"])

    def test_navigation_editing_repeats_and_ignored_events(self) -> None:
        key = termui._KEY_EVENT
        self.assertEqual(self.keys((key, 1, 1, 0x22, 0), (key, 1, 1, 0x2E, 0),
                                   (key, 1, 1, 0x0D, 13), (key, 1, 1, 0x08, 8),
                                   (key, 1, 1, 0x1B, 27), (key, 0, 1, 0x41, ord("a")),
                                   (2, 1, 1, 0, 0), (key, 1, 3, 0x28, 0),
                                   (key, 1, 1, 0x10, 0)),
                         ["pgdn", "delete", "enter", "backspace", "esc",
                          "down", "down", "down"])

    def test_characters_beyond_the_bmp_join_their_surrogate_halves(self) -> None:
        self.assertEqual(self.keys(*self.char("👩x")), ["👩", "x"])
        high, low = (record[4] for record in self.char("👩"))
        lone = [(termui._KEY_EVENT, 1, 1, 0, high), *self.char("x"),
                (termui._KEY_EVENT, 1, 1, 0, low), *self.char("y")]
        self.assertEqual(self.keys(*lone), ["x", "y"])

    def test_the_input_record_layout_matches_the_console_api(self) -> None:
        record = termui._key_record_type()
        self.assertIs(record, termui._key_record_type())
        self.assertEqual(ctypes.sizeof(record), 20)
        self.assertEqual([getattr(record, name).offset
                          for name in ("event_type", "key_down", "repeat", "vk", "char",
                                       "control")], [0, 4, 8, 10, 14, 16])

    def test_without_a_console_msvcrt_prefixes_are_told_from_a_typed_a_grave(self) -> None:
        def run(*typed: str) -> list[str]:
            queue = list(typed)
            fake = types.SimpleNamespace(kbhit=lambda: bool(queue),
                                         getwch=lambda: queue.pop(0))
            with mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                    mock.patch.object(termui, "_console_keys",
                                      termui._ConsoleKeys(termui._msvcrt_record)):
                out = []
                while key := termui._console_keys.next():
                    out.append(key)
                return out
        self.assertEqual(run("\xe0"), ["à"])
        self.assertEqual(run("\xe0", "H"), ["up"])
        self.assertEqual(run("\xe0", " ", "P"), ["à", " ", "P"])
        self.assertEqual(run("\x00", "P", "\x1b"), ["down", "esc"])
        # F7 (\x00 A) and Alt+F10 (\x00 q) are unbound keys, never a typed A or q
        self.assertEqual(run("\x00", "A"), [])
        self.assertEqual(run("\x00", "q", "x"), ["x"])

    def test_the_console_vt_input_mode_is_cleared_and_handed_back(self) -> None:
        calls = []
        fake = types.SimpleNamespace(SetConsoleMode=lambda handle, mode: calls.append(
            (handle, mode)) or 1)
        with mock.patch.object(termui, "_console_input", return_value=(7, 0x0207)), \
                mock.patch.object(termui, "_kernel32", return_value=fake):
            saved = termui._console_input_plain()
            owner = termui.Screen(io.StringIO(), io.StringIO())
            owner.console_input = saved
            with mock.patch.object(termui, "_tty_fd", return_value=None):
                owner.leave()
        self.assertEqual(calls, [(7, 0x0007), (7, 0x0207)])

    def test_enable_vt_records_the_mode_it_changed_and_restore_vt_puts_it_back(self) -> None:
        modes = {-11: 0x0003, -12: 0x0001}
        calls = []

        def get_mode(handle, pointer):
            pointer._obj.value = modes[handle]
            return 1

        fake = types.SimpleNamespace(
            GetStdHandle=lambda which: which, GetConsoleMode=get_mode,
            SetConsoleMode=lambda handle, mode: calls.append((handle, mode)) or 1)
        wintypes = types.SimpleNamespace(DWORD=ctypes.c_uint32)
        with mock.patch.object(console, "WIN", True), \
                mock.patch.object(console, "_console_kernel32", return_value=fake), \
                mock.patch.dict(sys.modules, {"ctypes.wintypes": wintypes}), \
                mock.patch.object(ctypes, "wintypes", wintypes, create=True), \
                mock.patch.dict(console._VT_ORIGINAL, clear=True):
            self.assertTrue(console.enable_vt())
            self.assertEqual(calls, [(-11, 0x0007), (-12, 0x0005)])
            console.restore_vt()
            self.assertEqual(calls[2:], [(-11, 0x0003), (-12, 0x0001)])
            self.assertEqual(console._VT_ORIGINAL, {})


if __name__ == "__main__":
    unittest.main()
