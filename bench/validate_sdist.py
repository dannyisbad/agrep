#!/usr/bin/env python3
"""Fail closed unless an agrep sdist contains only buildable source."""

from __future__ import annotations

import argparse
import io
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tarfile
import zlib

from package_metadata import InvalidMetadata, validate_core_metadata
from validate_wheel import RUNTIME_SOURCES


ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"agrep-(?P<version>[A-Za-z0-9_.!+]+)\.tar\.gz")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 512
ARCHIVE_MTIME = 1_580_601_600
TAR_RECORD_BYTES = 10_240
SOURCE_FILE_MODE = 0o644


class InvalidSdist(ValueError):
    """A source distribution violated the closed manifest."""


def _fail(message: str) -> None:
    raise InvalidSdist(message)


def _safe_relative(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        _fail(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")):
        _fail(f"non-canonical archive member path: {name!r}")
    if path.is_absolute() or str(path) != name:
        _fail(f"non-canonical archive member path: {name!r}")
    return name


def _source_files() -> dict[str, Path]:
    files = {
        "Cargo.lock": ROOT / "Cargo.lock",
        "Cargo.toml": ROOT / "Cargo.toml",
        "LICENSE": ROOT / "LICENSE",
        "README.md": ROOT / "README.md",
        "THIRD_PARTY_LICENSES.txt": ROOT / "THIRD_PARTY_LICENSES.txt",
        "cli.py": ROOT / "cli.py",
        "hatch_build.py": ROOT / "hatch_build.py",
        "hatch_sdist_build.py": ROOT / "hatch_sdist_build.py",
        "py/README.md": ROOT / "py" / "README.md",
        "pyproject.toml": ROOT / "pyproject.toml",
        "reindex.py": ROOT / "reindex.py",
        "rust-toolchain.toml": ROOT / "rust-toolchain.toml",
        "sdist_sources.py": ROOT / "sdist_sources.py",
    }
    for path in RUNTIME_SOURCES.values():
        relative = path.relative_to(ROOT).as_posix()
        files[relative] = path
    for path in sorted((ROOT / "crates").glob("*/Cargo.toml")):
        files[path.relative_to(ROOT).as_posix()] = path
    for path in sorted((ROOT / "crates").glob("*/src/**/*.rs")):
        files[path.relative_to(ROOT).as_posix()] = path
    for name, path in files.items():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            _fail(f"source manifest entry is unreadable: {name}: {exc}")
        if path.is_symlink() or not stat.S_ISREG(mode):
            _fail(f"source manifest entry is not a regular file: {name}")
    return files


def _metadata(data: bytes, version: str) -> None:
    try:
        validate_core_metadata(data, version)
    except InvalidMetadata as exc:
        _fail(f"invalid PKG-INFO: {exc}")


def _read_single_gzip(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) > MAX_ARCHIVE_BYTES:
        _fail("compressed sdist is oversized")
    if len(raw) < 18:
        _fail("sdist is too small to be gzip")
    magic, method, flags, mtime, extra_flags, system = struct.unpack(
        "<HBBIBB", raw[:10])
    if magic != 0x8B1F or method != 8:
        _fail("sdist is not a gzip deflate stream")
    if flags != 0:
        _fail("gzip header contains optional metadata")
    if (mtime, extra_flags, system) != (ARCHIVE_MTIME, 2, 255):
        _fail("gzip header is not canonical")
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        data = decoder.decompress(raw[10:], MAX_TOTAL_BYTES + 1)
        if decoder.unconsumed_tail:
            _fail("uncompressed sdist is oversized")
        data += decoder.flush()
    except zlib.error as exc:
        _fail(f"invalid gzip deflate stream: {exc}")
    if len(data) > MAX_TOTAL_BYTES:
        _fail("uncompressed sdist is oversized")
    if not decoder.eof or decoder.unconsumed_tail:
        _fail("gzip deflate stream is incomplete")
    if len(decoder.unused_data) != 8:
        _fail("sdist has a second gzip member or trailing bytes")
    expected_crc, expected_size = struct.unpack("<II", decoder.unused_data)
    if zlib.crc32(data) & 0xFFFF_FFFF != expected_crc:
        _fail("gzip payload checksum mismatch")
    if len(data) & 0xFFFF_FFFF != expected_size:
        _fail("gzip payload size trailer mismatch")
    return data


def validate(path: Path) -> tuple[str, int]:
    match = NAME.fullmatch(path.name)
    if match is None:
        _fail("filename must be agrep-<version>.tar.gz")
    version = match.group("version")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        _fail(f"cannot inspect sdist: {exc}")
    if path.is_symlink() or not stat.S_ISREG(mode):
        _fail("sdist path is not a regular file")

    prefix = f"agrep-{version}/"
    source = _source_files()
    expected = set(source) | {"PKG-INFO"}
    try:
        tar_data = _read_single_gzip(path)
        archive = tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:")
    except (OSError, tarfile.TarError) as exc:
        _fail(f"cannot open sdist: {exc}")
    with archive:
        if archive.pax_headers:
            _fail("sdist has global PAX metadata")
        members = []
        while True:
            member = archive.next()
            if member is None:
                break
            members.append(member)
            if len(members) > MAX_MEMBERS:
                _fail("sdist has too many member headers")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            _fail("sdist contains duplicate member names")
        relative = []
        total = 0
        next_offset = 0
        for member in members:
            _safe_relative(member.name)
            if not member.name.startswith(prefix):
                _fail(f"sdist member is outside {prefix!r}")
            item = member.name.removeprefix(prefix)
            _safe_relative(item)
            if item not in expected:
                _fail(f"unexpected sdist member: {item}")
            if not member.isfile():
                _fail(f"sdist member is not a regular file: {item}")
            if member.offset != next_offset:
                _fail(f"sdist contains hidden or non-canonical headers before: {item}")
            if member.size > MAX_MEMBER_BYTES:
                _fail(f"oversized sdist member: {item}")
            expected_mode = SOURCE_FILE_MODE
            if member.mode != expected_mode:
                _fail(
                    f"sdist member mode mismatch: {item}: "
                    f"{member.mode:o} != {expected_mode:o}")
            if (member.uid, member.gid, member.uname, member.gname) != (0, 0, "", ""):
                _fail(f"sdist member carries owner metadata: {item}")
            if member.pax_headers:
                _fail(f"sdist member carries PAX metadata: {item}")
            if member.mtime != ARCHIVE_MTIME:
                _fail(f"sdist member timestamp is not canonical: {item}")
            if member.devmajor != 0 or member.devminor != 0:
                _fail(f"sdist member carries device metadata: {item}")
            total += member.size
            relative.append(item)
            next_offset = member.offset_data + ((member.size + 511) // 512) * 512
        if total > MAX_TOTAL_BYTES:
            _fail("uncompressed sdist is oversized")
        expected_tar_size = (
            (next_offset + 1024 + TAR_RECORD_BYTES - 1)
            // TAR_RECORD_BYTES * TAR_RECORD_BYTES)
        if len(tar_data) != expected_tar_size or any(tar_data[next_offset:]):
            _fail("sdist tar padding or physical end is not canonical")
        got = set(relative)
        if got != expected:
            _fail("sdist manifest mismatch: "
                  f"missing={sorted(expected - got)} unexpected={sorted(got - expected)}")
        for member, item in zip(members, relative):
            extracted = archive.extractfile(member)
            if extracted is None:
                _fail(f"cannot read sdist member: {item}")
            data = extracted.read()
            if len(data) != member.size:
                _fail(f"short sdist member: {item}")
            if item == "PKG-INFO":
                _metadata(data, version)
            elif data != source[item].read_bytes():
                _fail(f"sdist member differs from source: {item}")
    return version, len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate the closed agrep source distribution")
    parser.add_argument("sdists", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.sdists:
        try:
            version, count = validate(path)
            print(f"sdist manifest ok: {path} ({version}, {count} files)")
        except (InvalidSdist, OSError, tarfile.TarError) as exc:
            failed = True
            print(f"sdist manifest FAILED: {path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
