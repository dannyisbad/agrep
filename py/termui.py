"""The terminal while a full-screen view owns it: `agrep live` (`agrep board`).

One key reader and one screen owner, so every full-screen view decodes the same
keys and hands the terminal back the same way on every exit path: quit, an
exception, Ctrl-C (however many), Ctrl-Z then fg or bg, a start with `&`,
SIGTERM, SIGHUP, SIGQUIT (Ctrl-\\), Ctrl-Break, the end of input and a dead
terminal.

On POSIX every write goes straight to the file descriptor. A signal can land
while a frame is half written: a BufferedWriter must never be re-entered from a
handler, nor left holding frame bytes for the interpreter to flush onto the
shell's screen at exit.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import signal
import sys
import time
from collections.abc import Iterator

try:
    import termios
except ImportError:  # Windows
    termios = None

# alternate screen, hidden cursor, bracketed paste: a terminal then frames
# pasted text in ESC[200~ ... ESC[201~, so a paste is never read as keys
ENTER_SCREEN = "\x1b[?1049h\x1b[?25l\x1b[?2004h"
# default colours, paste framing off, cursor shown, main screen back
LEAVE_SCREEN = "\x1b[0m\x1b[?2004l\x1b[?25h\x1b[?1049l"
_SEQ_WAIT_S = 0.025  # how long the rest of an escape sequence may trail its ESC
_PASTE_QUIET_S = 1.0  # a paste whose end marker never comes ends after this silence
_PASTE_MAX = 1 << 20
_PASTE_END = b"\x1b[201~"
_VT_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
_VT_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete",
             "5": "pgup", "6": "pgdn"}
# the signals that end a process without running its cleanup; a view hands the
# terminal back first, then dies of the same signal (Windows: Ctrl-Break)
_FATAL = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK")
               if hasattr(signal, name))
_LAST_RESORT_S = 5  # a fatal signal's restore that hangs anyway ends with SIGALRM


class Paste(str):
    """The key "paste": text a terminal delivered as one bracketed paste. It is
    data, never keys; the text is in `.text`."""

    text: str

    def __new__(cls, text: str) -> Paste:
        key = super().__new__(cls, "paste")
        key.text = text
        return key


# ---------------------------------------------------------------- POSIX keys

class _Keyboard:
    """Bytes from one input descriptor, buffered so a burst (a paste, keys that
    a terminal or ssh coalesced) decodes key by key and nothing read is lost."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = bytearray()
        self.eof = False

    def fill(self, timeout: float) -> bool:
        if self.eof:
            return False
        try:
            if not select.select([self.fd], [], [], timeout)[0]:
                return False
            chunk = os.read(self.fd, 4096)
        except OSError:  # EIO: a hung-up terminal reads as the end of input
            chunk = b""
        if not chunk:
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def peek(self, timeout: float) -> int | None:
        if not self.buffer and not self.fill(timeout):
            return None
        return self.buffer[0]

    def take(self, timeout: float) -> int | None:
        byte = self.peek(timeout)
        if byte is not None:
            del self.buffer[0]
        return byte


_keyboard: _Keyboard | None = None


def getkey() -> str:
    """The next key, or "" when none is waiting: a key name ("up", "pgdn",
    "enter", "backspace", "esc", and "eof" once input has ended), a Paste, or
    the typed character."""
    if os.name == "nt":
        return _nt_getkey()
    global _keyboard
    # os.read on the raw fd: buffered sys.stdin would slurp an arrow's "[A"
    # tail into Python's buffer, where select() can no longer see it
    fd = sys.stdin.fileno()
    if _keyboard is None or _keyboard.fd != fd:
        _keyboard = _Keyboard(fd)
    keys = _keyboard
    first = keys.take(0)
    if first is None:
        return "eof" if keys.eof else ""
    if first in (0x0D, 0x0A):
        return "enter"
    if first in (0x7F, 0x08):
        return "backspace"
    if first == 0x1B:
        return _escape(keys)
    # a typed character is one UTF-8 sequence: its lead byte says how long
    need = (1 if 0xC0 <= first < 0xE0 else 2 if 0xE0 <= first < 0xF0
            else 3 if 0xF0 <= first < 0xF8 else 0)
    data = bytearray([first])
    while need:
        follow = keys.peek(_SEQ_WAIT_S)
        if follow is None or not 0x80 <= follow < 0xC0:
            break
        data.append(follow)
        del keys.buffer[0]
        need -= 1
    return data.decode("utf-8", "replace")


def _escape(keys: _Keyboard) -> str:
    follow = keys.peek(_SEQ_WAIT_S)
    if follow == 0x5B:  # "[": a CSI sequence
        del keys.buffer[0]
        return _csi(keys)
    if follow == 0x4F:  # "O": SS3, which always carries one more byte
        del keys.buffer[0]
        final = keys.peek(_SEQ_WAIT_S)
        if final is None:  # Esc, then the letter O
            keys.buffer.insert(0, 0x4F)
            return "esc"
        del keys.buffer[0]
        return _VT_FINAL.get(chr(final), "")
    # nothing more, another Esc, or a key typed right after: this Esc stands
    # alone and whatever follows is the next key
    return "esc"


def _csi(keys: _Keyboard) -> str:
    sequence = bytearray()
    while True:
        byte = keys.take(_SEQ_WAIT_S)
        if byte is None or len(sequence) >= 32:
            return ""  # a sequence cut short decodes to nothing
        sequence.append(byte)
        if 0x40 <= byte <= 0x7E:
            break
    text = sequence.decode("latin-1")
    if text.endswith("~"):
        param = text[:-1].split(";")[0]
        if param == "200":
            return _paste(keys)
        return _VT_TILDE.get(param, "")  # a stray end marker (201) included
    return _VT_FINAL.get(text[-1], "")


def _paste(keys: _Keyboard) -> Paste:
    data = bytearray()
    while True:
        end = keys.buffer.find(_PASTE_END)
        if end >= 0:
            data += keys.buffer[:end]
            del keys.buffer[:end + len(_PASTE_END)]
            break
        # hold back a tail that may be the start of an end marker split by a read
        cut = max(0, len(keys.buffer) - (len(_PASTE_END) - 1))
        data += keys.buffer[:cut]
        del keys.buffer[:cut]
        del data[_PASTE_MAX:]
        if not keys.fill(_PASTE_QUIET_S):
            data += keys.buffer
            keys.buffer.clear()
            break
    return Paste(bytes(data[:_PASTE_MAX]).decode("utf-8", "replace"))


# -------------------------------------------------------------- Windows keys

# A console reports each key as an input record that carries the virtual-key
# code beside the character. getwch() reports an arrow as U+00E0 and then a
# letter, the same as a typed "à", so typed text and arrows cannot be told apart.
_KEY_EVENT = 1
_VK_KEYS = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x21: "pgup",
            0x22: "pgdn", 0x24: "home", 0x23: "end", 0x2E: "delete"}
_NT_SCAN_KEYS = {"H": "up", "P": "down", "M": "right", "K": "left", "I": "pgup",
                 "Q": "pgdn", "G": "home", "O": "end", "S": "delete"}
_VT_INPUT = 0x0200  # ENABLE_VIRTUAL_TERMINAL_INPUT
_KEY_RECORD = None


def _key_record_type():
    """INPUT_RECORD holding a KEY_EVENT_RECORD, in fixed-width fields (20 bytes);
    one class, so ReadConsoleInputW's argtypes accept the records read with it."""
    global _KEY_RECORD
    if _KEY_RECORD is None:
        import ctypes

        class KeyRecord(ctypes.Structure):
            _fields_ = [("event_type", ctypes.c_uint16), ("key_down", ctypes.c_int32),
                        ("repeat", ctypes.c_uint16), ("vk", ctypes.c_uint16),
                        ("scan", ctypes.c_uint16), ("char", ctypes.c_uint16),
                        ("control", ctypes.c_uint32)]
        _KEY_RECORD = KeyRecord
    return _KEY_RECORD


def _key_from_record(vk: int, char: int) -> str:
    """The key one key-down record means: the character the console resolved
    (any layout, dead keys, Alt codes), else the navigation key its code names."""
    if char:
        text = chr(char)
        return {"\r": "enter", "\n": "enter", "\x08": "backspace", "\x1b": "esc"}.get(text, text)
    return _VK_KEYS.get(vk, "")


class _ConsoleKeys:
    """Keys from console input records. `read()` gives the next record as
    (event_type, key_down, repeat, vk, char), or None when none is waiting."""

    def __init__(self, read) -> None:
        self.read = read
        self.queue: list[str] = []

    def next(self) -> str:
        if self.queue:
            return self.queue.pop(0)
        while True:
            record = self.read()
            if record is None:
                return ""
            event_type, key_down, repeat, vk, char = record
            if event_type != _KEY_EVENT or not key_down:
                continue
            if 0xDC00 <= char < 0xE000:
                continue  # a low surrogate without its high half carries nothing
            if 0xD800 <= char < 0xDC00:
                # beyond the BMP: the console sends a surrogate pair as two records
                low = self._low_surrogate()
                if low is None:
                    # the half carries nothing; a key read in its place comes next
                    if self.queue:
                        return self.queue.pop(0)
                    continue
                key = chr(0x10000 + ((char - 0xD800) << 10) + (low - 0xDC00))
            else:
                key = _key_from_record(vk, char)
            if key:
                self.queue.extend([key] * (max(1, repeat) - 1))
                return key

    def _low_surrogate(self) -> int | None:
        while True:
            record = self.read()
            if record is None:
                return None
            event_type, key_down, _repeat, vk, char = record
            if event_type != _KEY_EVENT or not key_down:
                continue
            if 0xDC00 <= char < 0xE000:
                return char
            key = _key_from_record(vk, char)
            if key:
                self.queue.append(key)
            return None


_K32 = None
_console_keys: _ConsoleKeys | None = None


def _kernel32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = (wintypes.DWORD,)
        k32.GetStdHandle.restype = wintypes.HANDLE
        count = ctypes.POINTER(wintypes.DWORD)
        for name, argtypes in (
                ("GetConsoleMode", (wintypes.HANDLE, count)),
                ("SetConsoleMode", (wintypes.HANDLE, wintypes.DWORD)),
                ("GetNumberOfConsoleInputEvents", (wintypes.HANDLE, count)),
                ("ReadConsoleInputW", (wintypes.HANDLE, ctypes.POINTER(_key_record_type()),
                                       wintypes.DWORD, count))):
            function = getattr(k32, name)
            function.argtypes = argtypes
            function.restype = wintypes.BOOL
        _K32 = k32
    return _K32


def _console_input() -> tuple[object, int] | None:
    """The console input handle and its mode, or None when stdin is no console."""
    import ctypes
    k32 = _kernel32()
    handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    mode = ctypes.c_ulong()
    if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    return handle, mode.value


def _console_record_reader():
    import ctypes
    found = _console_input()
    if found is None:
        return None
    handle = found[0]
    k32 = _kernel32()
    record_type = _key_record_type()

    def read():
        waiting, got = ctypes.c_ulong(), ctypes.c_ulong()
        if not k32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(waiting)) \
                or not waiting.value:
            return None
        record = record_type()
        if not k32.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(got)) \
                or not got.value:
            return None
        return record.event_type, record.key_down, record.repeat, record.vk, record.char
    return read


def _msvcrt_record():
    """A key record from msvcrt, for input that is no console: an extended key's
    prefix is taken as one only when its scan code follows at once. After \\x00
    (function and Alt keys) a scan code with no binding is dropped, never typed;
    after \\xe0 it may be a typed "à" followed by another key, which is kept."""
    import msvcrt
    if not msvcrt.kbhit():
        return None
    char = msvcrt.getwch()
    if char in ("\x00", "\xe0") and msvcrt.kbhit():
        scan = msvcrt.getwch()
        name = _NT_SCAN_KEYS.get(scan)
        if name is not None:
            vk = next(code for code, key in _VK_KEYS.items() if key == name)
            return _KEY_EVENT, 1, 1, vk, 0
        if char == "\xe0":
            _console_keys.queue.append(_key_from_record(0, ord(scan)))
    return _KEY_EVENT, 1, 1, 0, ord(char) if char != "\x00" else 0


def _nt_getkey() -> str:
    global _console_keys
    if _console_keys is None:
        _console_keys = _ConsoleKeys(_console_record_reader() or _msvcrt_record)
    return _console_keys.next()


def _console_input_plain() -> tuple[object, int] | None:
    """Clear the console's VT input mode, which an earlier program may have
    left on and which turns an arrow into ESC [ A read as Esc; returns the
    (handle, mode) to restore."""
    try:
        found = _console_input()
        if found is not None and found[1] & _VT_INPUT:
            _kernel32().SetConsoleMode(found[0], found[1] & ~_VT_INPUT)
        return found
    except (AttributeError, OSError):
        return None


# -------------------------------------------------------------- the terminal

def _tty_fd(stream) -> int | None:
    if termios is None:
        return None
    try:
        fd = stream.fileno()
        return fd if os.isatty(fd) else None
    except (AttributeError, OSError, ValueError):
        return None


def _tty_call(function, *args):
    """A termios call that a stop and continue (EINTR) cannot break; its other
    errors surface as OSError, the error class the CLI reports."""
    for attempt in range(10):
        try:
            return function(*args)
        except termios.error as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else errno.EIO
            if code != errno.EINTR or attempt == 9:
                raise OSError(code, f"the terminal could not be set up: {os.strerror(code)}") \
                    from exc


@contextlib.contextmanager
def _signal_set(signum: int, handler) -> Iterator[None]:
    """One signal's disposition for a moment; a no-op off the main thread."""
    try:
        previous = signal.signal(signum, handler)
    except (OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError, ValueError, TypeError):
            signal.signal(signum, signal.SIG_DFL if previous is None else previous)


@contextlib.contextmanager
def _held(signum: int) -> Iterator[None]:
    """`signum` held pending in this thread for a moment; its handler runs once
    it is let go. A no-op where signals cannot be masked."""
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    except (AttributeError, OSError, ValueError):
        yield
        return
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signum})
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _wait_foreground(fd: int) -> None:
    """Started with `&` or continued with `bg`, a view stays stopped until it
    is brought to the foreground, as a shell job would, instead of drawing over
    the shell or stopping inside a terminal call."""
    idle = 0
    while idle < 40:
        try:
            if os.tcgetpgrp(fd) == os.getpgrp():
                return
        except OSError:
            return  # no terminal to wait for: the reads that follow end the view
        started = time.monotonic()
        with _signal_set(signal.SIGTTOU, signal.SIG_DFL):
            signal.raise_signal(signal.SIGTTOU)  # stopped here until fg or bg
        if time.monotonic() - started < 0.05:
            idle += 1  # an orphaned process group discards the stop
            time.sleep(0.05)


def _write(stream, text: str) -> None:
    """Write straight to the descriptor. A Windows console takes text beyond
    ASCII through the stream, which hands it to the console API as UTF-16."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None or (os.name == "nt" and not text.isascii()):
        stream.write(text)
        stream.flush()
        return
    view = memoryview(text.encode("utf-8", "replace"))
    while view:
        view = view[os.write(fd, view):]


class Screen:
    """Cbreak input (no echo, no line editing, no XON/XOFF flow control), the
    alternate screen, a hidden cursor and bracketed paste. `leave` hands all of
    it back and never raises; after Ctrl-Z and fg, `resumed` turns true so the
    view repaints in full."""

    def __init__(self, stdin=None, stdout=None) -> None:
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.saved = None  # the termios state to hand back
        self.console_input = None  # Windows: (handle, mode) to hand back
        self.shown = False
        self.resumed = False
        self.stop_requested = False
        self.interrupted = False
        self.leaving = False
        self.hand_back_sent = False  # hand-back escapes written while the saved modes are still held
        self.dying = False  # a fatal signal is being handled: nothing nests in it

    def key(self) -> str:
        """The next key (see getkey), after a Ctrl-Z the handler only noted."""
        if self.stop_requested:
            self._suspend()
        return getkey()

    def enter(self) -> None:
        fd = _tty_fd(self.stdin)
        if fd is not None:
            _wait_foreground(fd)
            saved = _tty_call(termios.tcgetattr, fd)
            mode = [*saved[:6], list(saved[6])]
            mode[0] &= ~termios.IXON  # Ctrl-S must not freeze the view
            mode[3] &= ~(termios.ECHO | termios.ICANON)
            mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
            self.saved = saved
            _tty_call(termios.tcsetattr, fd, termios.TCSANOW, mode)
        elif os.name == "nt":
            self.console_input = _console_input_plain()
        with contextlib.suppress(AttributeError, OSError, ValueError):
            self.stdout.flush()
        self.shown = True
        _write(self.stdout, ENTER_SCREEN)

    def leave(self, *, urgent: bool = False) -> None:
        """Hand the terminal back; never raises, also on a dead terminal.

        The modes go back before the escapes are written, so a dead or blocked
        output cannot strand the keyboard in cbreak, and input still unread is
        discarded (TCSAFLUSH) rather than reaching the shell or the next
        program. Until tcdrain returns, NOFLSH stays set and flow control off:
        with the user's modes, a Ctrl-C from the storm that ended the view would
        flush the escapes unsent, leaving the shell on the alternate screen with
        the cursor hidden, and a Ctrl-S would stop them. On macOS tcdrain waits
        until the terminal has read them. A Linux pty's tcdrain returns once they
        are queued, so there a Ctrl-C after the hand-back can still drop escapes
        the terminal has not read yet. SIGINT is held for that wait,
        which a storm would otherwise break by exhausting its retries. Then the
        exact saved modes go back; they are kept until that last call, so a fatal
        signal landing anywhere before it still restores them.

        `urgent` (a fatal signal) never waits for output to drain, which a
        stalled terminal never does: queued input and output are dropped
        (TCIOFLUSH; the output is the view's own), the modes go back at once
        (TCSANOW), and the escapes go into the emptied queue. A Linux pty flush also
        drops escapes an interrupted hand-back already drained, so they go out again.
        Nothing touches the file flags, which the shell and its other jobs share."""
        self.leaving = True
        try:
            fd = _tty_fd(self.stdin) if self.saved is not None else None
            if fd is not None:
                # a background job restores without being stopped for it
                with _signal_set(signal.SIGTTOU, signal.SIG_IGN):
                    if urgent:
                        with contextlib.suppress(OSError):
                            _tty_call(termios.tcflush, fd, termios.TCIOFLUSH)
                        if self.hand_back_sent:
                            self.shown, self.hand_back_sent = True, False
                        self._restore_modes(fd)
                    else:
                        self._hand_back_in_order(fd)
            self.saved, self.hand_back_sent = None, False
            if self.console_input is not None:
                with contextlib.suppress(AttributeError, OSError):
                    _kernel32().SetConsoleMode(*self.console_input)
                self.console_input = None
            self._show_main_screen()
        finally:
            self.leaving = False

    def _hand_back_in_order(self, fd: int) -> None:
        saved = self.saved
        holding = [saved[0] & ~termios.IXON, saved[1], saved[2], saved[3] | termios.NOFLSH,
                   *saved[4:]]
        with _held(signal.SIGINT):
            try:
                with contextlib.suppress(OSError):
                    _tty_call(termios.tcsetattr, fd, termios.TCSAFLUSH, holding)
                if self.shown:
                    with contextlib.suppress(OSError, ValueError):
                        _write(self.stdout, LEAVE_SCREEN)
                    self.hand_back_sent = True
                    with contextlib.suppress(OSError):
                        _tty_call(termios.tcdrain, fd)
                    self.shown = False
            finally:
                self._restore_modes(fd)

    def _restore_modes(self, fd: int) -> None:
        """The exact saved modes, at once: the last terminal call of a leave."""
        if self.saved is not None:
            with contextlib.suppress(OSError):
                _tty_call(termios.tcsetattr, fd, termios.TCSANOW, self.saved)

    def _show_main_screen(self) -> None:
        if self.shown:
            with contextlib.suppress(OSError, ValueError):
                _write(self.stdout, LEAVE_SCREEN)
            self.shown = False
            if os.name == "nt":
                import console
                console.restore_vt()

    def _suspend(self) -> None:
        """Ctrl-Z, run from the view's loop (the handler only asks): hand the
        terminal back, stop, and take it again once in the foreground."""
        self.stop_requested = False
        self.leave()
        with _signal_set(signal.SIGTSTP, signal.SIG_DFL):
            signal.raise_signal(signal.SIGTSTP)  # stopped here until fg or bg
        self.enter()
        self.resumed = True

    def _on_stop(self, _signum, _frame) -> None:
        # no I/O here: the loop may be inside a write this handler interrupted
        self.stop_requested = True

    def _on_interrupt(self, _signum, frame) -> None:
        if self.leaving or self.interrupted or self.dying or self._inside_fatal(frame):
            return  # one Ctrl-C ends the view; the rest of a storm cannot stop the restore
        self.interrupted = True
        raise KeyboardInterrupt

    def _inside_fatal(self, frame) -> bool:
        """Whether a Ctrl-C landed inside the fatal handler before it set `dying`: raised
        there, it would abandon the handler, and the process would not die of its signal."""
        code = self._on_fatal.__func__.__code__
        while frame is not None:
            if frame.f_code is code:
                return True
            frame = frame.f_back
        return False

    def _on_fatal(self, signum, _frame) -> None:
        # A handler can run again inside itself: blocking stops a second fatal signal in this
        # thread, and `dying` turns away one another thread took, so one restore runs to its end.
        if self.dying:
            return
        self.dying = True
        if hasattr(signal, "pthread_sigmask"):
            with contextlib.suppress(OSError, ValueError):
                signal.pthread_sigmask(signal.SIG_BLOCK, {*_FATAL, signal.SIGINT})
        if hasattr(signal, "alarm"):
            # a restore that hangs anyway cannot keep the process alive
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                signal.alarm(_LAST_RESORT_S)
        try:
            self.leave(urgent=True)
        finally:
            try:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)  # pending while blocked
                if hasattr(signal, "pthread_sigmask"):
                    # delivered now: the exit status names the first signal handled
                    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
            finally:
                os._exit(128 + signum)


@contextlib.contextmanager
def screen(stdin=None, stdout=None) -> Iterator[Screen]:
    """Own the terminal for a full-screen view and give it back on every exit."""
    owner = Screen(stdin, stdout)
    previous: dict = {}
    try:
        try:
            _take_signals(owner, previous)
            owner.enter()
            yield owner
        finally:
            _close(owner, previous)
    except KeyboardInterrupt:
        # a Ctrl-C handled before leave() marks the screen as leaving cuts the close short;
        # the screen's handler raises only once, so this close runs to its end
        _close(owner, previous)
        raise


def _take_signals(owner: Screen, previous: dict) -> None:
    handlers = {signal.SIGINT: owner._on_interrupt, **dict.fromkeys(_FATAL, owner._on_fatal)}
    if hasattr(signal, "SIGTSTP"):
        handlers[signal.SIGTSTP] = owner._on_stop
    if hasattr(signal, "SIGPIPE"):
        # a helper that exits before reading its input must not kill the view
        # while it owns the screen
        handlers[signal.SIGPIPE] = signal.SIG_IGN
    for signum, handler in handlers.items():
        try:
            current = signal.getsignal(signum)
            # an inherited "ignore" (nohup, `trap '' HUP`, a `&` job's
            # Ctrl-C) is the caller's choice and stays
            if current is not signal.SIG_IGN:
                previous[signum] = current  # first, so a Ctrl-C cannot lose it
                signal.signal(signum, handler)
        except (OSError, ValueError):  # not the main thread: defaults stay
            pass


def _close(owner: Screen, previous: dict) -> None:
    owner.leave()
    # SIGINT last: until then a Ctrl-C meets the screen's handler, which raises only once
    for signum in sorted(previous, key=lambda signum: signum == signal.SIGINT):
        handler = previous[signum]
        with contextlib.suppress(OSError, ValueError, TypeError):
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def repaint(lines: list[str], stdout=None) -> None:
    """Draw a frame over the last one in place: home, every line's tail cleared,
    then the rest of the screen; no full-clear flicker."""
    _write(sys.stdout if stdout is None else stdout,
           "\x1b[H" + "".join(line + "\x1b[K\n" for line in lines) + "\x1b[0J")
