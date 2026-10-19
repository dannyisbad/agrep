"""Whether a newer agrep owns the data dir: the derived-store ownership anchor's version, read
on its own so every layer that words a remedy (audit's included) can defer to that agrep."""

from __future__ import annotations

import json
from pathlib import Path

import common
import ownerfile

OWNER_NAME = ".derived-owner.json"
OWNER_VERSION = 1
OWNER_MAX_BYTES = 4096


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    record: dict = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate key {key!r}")
        record[key] = value
    return record


def newer_version(record: object) -> tuple[int, int | None] | None:
    """(version, format) of an ownership record a newer agrep wrote: a JSON
    object whose integer `version` exceeds this build's. Any other shape is
    not newer; it is either this build's record or a malformed one."""
    if not isinstance(record, dict):
        return None
    version = record.get("version")
    if type(version) is not int or version <= OWNER_VERSION:
        return None
    fmt = record.get("format")
    return version, (fmt if type(fmt) is int else None)


def newer_anchor(path: Path | None = None) -> tuple[int, int | None] | None:
    """(version, format) when the anchor at `path` (the data dir's by default) was written by a
    newer agrep, else None; a missing or undecodable anchor is simply not newer."""
    try:
        observed = ownerfile.snapshot(
            path or common.DATA_DIR / OWNER_NAME, max_bytes=OWNER_MAX_BYTES)
        record = json.loads(observed.raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, RecursionError, UnicodeError, ValueError):
        return None
    return newer_version(record)
