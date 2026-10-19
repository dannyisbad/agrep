"""Per-operation deadlines for user regexes inside the isolated search worker."""

from __future__ import annotations

from functools import partial
import re
import time


class MatchTimeoutError(RuntimeError):
    pass


class _Guard:
    __slots__ = ("deadline", "timeout")

    def __init__(self, deadline, timeout: float) -> None:
        self.deadline = deadline
        self.timeout = timeout

    def call(self, method, *args, **kwargs):
        expires = time.monotonic() + self.timeout
        self.deadline.value = expires
        try:
            result = method(*args, **kwargs)
            if time.monotonic() >= expires:
                raise MatchTimeoutError
            return result
        finally:
            self.deadline.value = 0.0


class _Pattern:
    __slots__ = ("_pattern", "_guard", "search", "match", "fullmatch", "sub")

    def __init__(self, pattern: re.Pattern, guard: _Guard) -> None:
        self._pattern = pattern
        self._guard = guard
        self.search = partial(guard.call, pattern.search)
        self.match = partial(guard.call, pattern.match)
        self.fullmatch = partial(guard.call, pattern.fullmatch)
        self.sub = partial(guard.call, pattern.sub)

    def finditer(self, *args, **kwargs):
        matches = self._pattern.finditer(*args, **kwargs)
        call = self._guard.call
        while (match := call(next, matches, None)) is not None:
            yield match


_guard: _Guard | None = None


def install(deadline, timeout: float) -> None:
    global _guard
    _guard = _Guard(deadline, timeout)


def uninstall() -> None:
    global _guard
    _guard = None


def compile(pattern: str, flags: int = 0):
    compiled = re.compile(pattern, flags)
    return compiled if _guard is None else _Pattern(compiled, _guard)
