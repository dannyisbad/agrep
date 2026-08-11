#!/usr/bin/env python3
"""Fail unless auditwheel proves a wheel satisfies its claimed policy."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


POLICY = re.compile(
    r"^manylinux_(?P<major>[0-9]+)_(?P<minor>[0-9]+)_"
    r"(?P<arch>x86_64|aarch64)$")
REPORT_POLICY = re.compile(
    r'is\s+consistent\s+with\s+the\s+following\s+platform\s+tag:\s*"'
    r'(?P<policy>[^"]+)"\.',
    re.IGNORECASE,
)
MAX_REPORT_BYTES = 256 * 1024


class InvalidPolicy(ValueError):
    """The auditwheel report did not prove the requested compatibility."""


def _parts(policy: str) -> tuple[int, int, str]:
    match = POLICY.fullmatch(policy)
    if match is None:
        raise InvalidPolicy(f"unsupported manylinux policy: {policy!r}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        match.group("arch"),
    )


def validate(report: str, target: str) -> str:
    target_major, target_minor, target_arch = _parts(target)
    found = [match.group("policy") for match in REPORT_POLICY.finditer(report)]
    if len(found) != 1:
        raise InvalidPolicy(
            f"auditwheel report must contain one overall policy, got {found!r}")
    actual = found[0]
    actual_major, actual_minor, actual_arch = _parts(actual)
    if actual_arch != target_arch:
        raise InvalidPolicy(
            f"auditwheel architecture {actual_arch!r} does not match {target_arch!r}")
    if (actual_major, actual_minor) > (target_major, target_minor):
        raise InvalidPolicy(
            f"{actual} exceeds the wheel policy {target}")
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="enforce auditwheel's computed manylinux policy")
    parser.add_argument("--target", required=True)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        size = args.report.stat().st_size
        if size > MAX_REPORT_BYTES:
            raise InvalidPolicy(f"auditwheel report is oversized: {size} bytes")
        actual = validate(args.report.read_text(encoding="utf-8"), args.target)
    except (InvalidPolicy, OSError, UnicodeError) as exc:
        print(f"manylinux policy FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"manylinux policy ok: {actual} satisfies {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
