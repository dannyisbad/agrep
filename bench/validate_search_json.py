#!/usr/bin/env python3
"""Validate a search JSON page for release workflow canaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


class InvalidSearchPage(ValueError):
    pass


def parse_page(path: Path) -> tuple[dict, list[dict]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InvalidSearchPage(f"invalid JSON line: {exc}") from exc
        if not isinstance(value, dict):
            raise InvalidSearchPage("search JSON contained a non-object record")
        records.append(value)
    if not records or records[0].get("kind") != "agrep-meta":
        raise InvalidSearchPage("search JSON lacked a leading agrep-meta record")
    if any(record.get("kind") == "agrep-meta" for record in records[1:]):
        raise InvalidSearchPage("search JSON contained multiple agrep-meta records")
    return records[0], records[1:]


def relevant_page(
        meta: dict, hits: list[dict], *, engine_prefix: str,
        needles: list[str],
) -> bool:
    if not str(meta.get("engine") or "").startswith(engine_prefix):
        return False
    folded = [needle.casefold() for needle in needles]
    return any(
        any(needle in text for needle in folded)
        for row in hits
        for text in [str(row.get("text") or row.get("snippet") or "").casefold()]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--engine-prefix", required=True)
    parser.add_argument("--contains-any", nargs="+", required=True)
    args = parser.parse_args(argv)
    try:
        meta, hits = parse_page(args.path)
    except (OSError, InvalidSearchPage) as exc:
        print(f"invalid search JSON: {exc}", file=sys.stderr)
        return 2
    if relevant_page(
            meta, hits, engine_prefix=args.engine_prefix,
            needles=args.contains_any):
        return 0
    print("search JSON did not contain the required engine and evidence", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
