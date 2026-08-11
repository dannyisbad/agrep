#!/usr/bin/env python3
"""Reject release binaries that retain absolute builder paths."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import stat
import sys


HOME_PATTERNS = (
    ("macOS home", re.compile(rb"/Users/[^/\x00\r\n]+/")),
    ("Linux home", re.compile(rb"/home/[^/\x00\r\n]+/")),
    ("Linux root home", re.compile(rb"/root/")),
    ("Windows home", re.compile(
        rb"(?i:[A-Z]:[\\/]Users[\\/][^\\/\x00\r\n]+[\\/])")),
)


class InvalidBinary(ValueError):
    """A release binary retained private or machine-specific path data."""


def _variants(value: str) -> tuple[bytes, ...]:
    clean = value.rstrip("/\\")
    if not clean:
        return ()
    values = {clean, clean.replace("\\", "/"), clean.replace("/", "\\")}
    return tuple(sorted((item.encode("utf-8") for item in values), key=len,
                        reverse=True))


def validate_bytes(data: bytes, forbidden: tuple[str, ...] = ()) -> None:
    if not data:
        raise InvalidBinary("binary is empty")
    for label, pattern in HOME_PATTERNS:
        if pattern.search(data):
            raise InvalidBinary(f"embedded absolute {label} path")
    for index, value in enumerate(forbidden, 1):
        for marker in _variants(value):
            if marker in data:
                raise InvalidBinary(
                    f"embedded forbidden build path (rule {index})")


def validate(path: Path, forbidden: tuple[str, ...] = ()) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise InvalidBinary(f"cannot inspect binary: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise InvalidBinary("binary path is not a regular file")
    try:
        validate_bytes(path.read_bytes(), forbidden)
    except OSError as exc:
        raise InvalidBinary(f"cannot read binary: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="reject absolute builder paths in release binaries")
    parser.add_argument("binaries", nargs="+", type=Path)
    parser.add_argument("--forbid", action="append", default=[])
    args = parser.parse_args(argv)
    failed = False
    forbidden = tuple(value for value in args.forbid if value)
    for path in args.binaries:
        try:
            validate(path, forbidden)
            print(f"binary privacy ok: {path}")
        except InvalidBinary as exc:
            failed = True
            print(f"binary privacy FAILED: {path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
