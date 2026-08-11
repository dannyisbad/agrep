#!/usr/bin/env python3
"""Fail closed unless an agrep release wheel has the exact runtime payload.

This intentionally uses only the standard library so every release runner can
validate a wheel *before* installing it.  The allowlist is explicit: broad
``agrep/py/**`` rules would repeat the packaging bug this gate exists to catch
(ignored tests, screenshots, or local scratch files leaking into an artifact).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import zipfile

from package_metadata import InvalidMetadata, validate_core_metadata
from validate_binary_privacy import (
    InvalidBinary as InvalidBinaryPrivacy,
    validate_bytes as validate_binary_privacy,
)

ROOT = Path(__file__).resolve().parents[1]


class InvalidWheel(ValueError):
    """A release artifact violated the closed manifest."""


def _manifest_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidWheel(f"invalid runtime manifest {label}: {value!r}")
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise InvalidWheel(f"invalid runtime manifest {label}: {value!r}")
    return value


def _load_runtime_sources() -> dict[str, Path]:
    manifest = ROOT / "py" / "runtime_manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        raw_files = payload["files"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidWheel(f"invalid runtime manifest {manifest}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(raw_files, list):
        raise InvalidWheel(f"invalid runtime manifest schema: {manifest}")
    sources: dict[str, Path] = {}
    seen_sources: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"source", "member"}:
            raise InvalidWheel(f"invalid runtime manifest entry: {item!r}")
        source = _manifest_path(item["source"], "source")
        member = _manifest_path(item["member"], "member")
        if not member.startswith("agrep/"):
            raise InvalidWheel(f"runtime member is outside agrep/: {member!r}")
        if source in seen_sources:
            raise InvalidWheel(f"duplicate runtime manifest source: {source!r}")
        if member in sources:
            raise InvalidWheel(f"duplicate runtime manifest member: {member!r}")
        path = ROOT / source
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise InvalidWheel(f"runtime source is unreadable: {source}: {exc}") from exc
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise InvalidWheel(f"runtime source is not a regular file: {source}")
        seen_sources.add(source)
        sources[member] = path
    if not sources:
        raise InvalidWheel(f"empty runtime manifest: {manifest}")
    expected_self = "agrep/py/runtime_manifest.json"
    if sources.get(expected_self) != manifest:
        raise InvalidWheel("runtime manifest must list itself")
    return sources


RUNTIME_SOURCES = _load_runtime_sources()
RUNTIME_FILES = frozenset(RUNTIME_SOURCES)

# Pinned Hatchling emits this closed metadata set; every member is validated
# against release inputs or the builder's exact contract.
DIST_INFO_FILES = frozenset({
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
    "licenses/THIRD_PARTY_LICENSES.txt",
})

# platform tag -> (binary path, container format, architecture id)
PLATFORMS = {
    "win_amd64": ("agrep/_bin/agrep-rs.exe", "pe", 0x8664),
    "win_arm64": ("agrep/_bin/agrep-rs.exe", "pe", 0xAA64),
    "macosx_11_0_x86_64": ("agrep/_bin/agrep-rs", "macho", 0x01000007),
    "macosx_11_0_arm64": ("agrep/_bin/agrep-rs", "macho", 0x0100000C),
    "manylinux_2_28_x86_64": ("agrep/_bin/agrep-rs", "elf", 62),
    "manylinux_2_28_aarch64": ("agrep/_bin/agrep-rs", "elf", 183),
}

WHEEL_NAME = re.compile(
    r"^agrep-(?P<version>[A-Za-z0-9_.!+]+)-py3-none-(?P<platform>.+)\.whl$")
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_WHEEL_BYTES = 128 * 1024 * 1024
CANONICAL_ZIP_TIME = (2020, 2, 2, 0, 0, 0)


def _fail(message: str) -> None:
    raise InvalidWheel(message)


def _safe_member(name: str) -> None:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        _fail(f"unsafe ZIP member path: {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")):
        _fail(f"non-canonical ZIP member path: {name!r}")
    if str(path) != name or path.is_absolute():
        _fail(f"non-canonical ZIP member path: {name!r}")


def _wheel_bytes(path: Path) -> bytes:
    try:
        status = path.lstat()
    except OSError as exc:
        _fail(f"cannot stat wheel: {exc}")
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        _fail("wheel path must be a regular, non-symlink file")
    if status.st_size > MAX_WHEEL_BYTES:
        _fail(f"oversized compressed wheel: {status.st_size} bytes")
    try:
        data = path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read wheel: {exc}")
    if len(data) != status.st_size:
        _fail("wheel changed while it was being read")
    return data


def _zip_envelope(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"PK\x03\x04"):
        _fail("wheel has bytes before its first local ZIP member")
    eocd_offset = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if eocd_offset < 0 or eocd_offset + 22 > len(data):
        _fail("wheel has no complete ZIP end record")
    fields = struct.unpack_from("<4s4H2LH", data, eocd_offset)
    _, disk, central_disk, disk_entries, entries, size, offset, comment_size = fields
    if eocd_offset + 22 + comment_size != len(data):
        _fail("wheel has trailing bytes after its ZIP end record")
    if comment_size:
        _fail("wheel ZIP comments are forbidden")
    if disk or central_disk or disk_entries != entries:
        _fail("multi-disk wheel ZIPs are forbidden")
    if entries == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        _fail("ZIP64 wheel containers are forbidden")
    if offset + size != eocd_offset:
        _fail("wheel has bytes between its central directory and ZIP end record")
    return offset, entries


def _local_layout(data: bytes, infos: list[zipfile.ZipInfo],
                  central_offset: int) -> None:
    cursor = 0
    for info in sorted(infos, key=lambda value: value.header_offset):
        if info.header_offset != cursor or cursor + 30 > central_offset:
            _fail("wheel contains hidden or overlapping local ZIP members")
        fields = struct.unpack_from("<4s5H3L2H", data, cursor)
        (signature, extract_version, flags, compression, _, _, crc,
         compressed_size, file_size, name_size, extra_size) = fields
        if signature != b"PK\x03\x04":
            _fail(f"invalid local ZIP header for {info.filename!r}")
        if (extract_version, flags, compression, crc, compressed_size, file_size) != (
                info.extract_version, info.flag_bits, info.compress_type, info.CRC,
                info.compress_size, info.file_size):
            _fail(f"local and central ZIP headers differ for {info.filename!r}")
        name_start = cursor + 30
        name_end = name_start + name_size
        extra_end = name_end + extra_size
        if extra_end > central_offset:
            _fail(f"truncated local ZIP header for {info.filename!r}")
        try:
            encoded_name = info.filename.encode("ascii")
        except UnicodeEncodeError:
            _fail(f"non-ASCII ZIP member name: {info.filename!r}")
        if data[name_start:name_end] != encoded_name:
            _fail(f"local ZIP member name differs for {info.filename!r}")
        if extra_size:
            _fail(f"local ZIP extra fields are forbidden: {info.filename!r}")
        cursor = extra_end + info.compress_size
    if cursor != central_offset:
        _fail("wheel contains unmanifested bytes before its central directory")


def _runtime_source(member: str) -> Path:
    source = RUNTIME_SOURCES.get(member)
    if source is None:
        _fail(f"internal error: no checkout source for {member!r}")
    return source


def _normalized_source_mode(path: Path) -> int:
    executable = bool(path.stat().st_mode & stat.S_IXUSR)
    return stat.S_IFREG | (0o755 if executable else 0o644)


def _expected_member_mode(name: str, binary_name: str, platform: str,
                          dist_info: str) -> int:
    if name in RUNTIME_FILES:
        return _normalized_source_mode(_runtime_source(name))
    if name == binary_name:
        executable = platform.startswith(("macosx_", "manylinux_"))
        return stat.S_IFREG | (0o755 if executable else 0o644)
    if name.startswith(f"{dist_info}/"):
        return 0o644
    _fail(f"internal error: no canonical mode for {name!r}")
    raise AssertionError("unreachable")


def _decode_macho_version(value: int) -> tuple[int, int, int]:
    return value >> 16, (value >> 8) & 0xFF, value & 0xFF


def _macho_minimum_version(data: bytes) -> tuple[int, int, int]:
    byte_order = {
        b"\xcf\xfa\xed\xfe": "<",
        b"\xfe\xed\xfa\xcf": ">",
    }.get(data[:4])
    if byte_order is None or len(data) < 32:
        _fail("macOS binary has an invalid 64-bit Mach-O header")
    command_count, command_bytes = struct.unpack_from(
        f"{byte_order}II", data, 16)
    command_end = 32 + command_bytes
    if command_end > len(data):
        _fail("macOS binary has truncated load commands")
    versions = []
    cursor = 32
    for _ in range(command_count):
        if cursor + 8 > command_end:
            _fail("macOS binary has a truncated load command")
        command, size = struct.unpack_from(f"{byte_order}II", data, cursor)
        if size < 8 or size % 8 or cursor + size > command_end:
            _fail("macOS binary has an invalid load command size")
        if command == 0x32:
            if size < 24:
                _fail("macOS LC_BUILD_VERSION command is truncated")
            platform, minimum = struct.unpack_from(
                f"{byte_order}II", data, cursor + 8)
            if platform != 1:
                _fail("macOS wheel binary targets a non-macOS platform")
            versions.append(_decode_macho_version(minimum))
        elif command == 0x24:
            if size < 16:
                _fail("macOS LC_VERSION_MIN_MACOSX command is truncated")
            minimum = struct.unpack_from(f"{byte_order}I", data, cursor + 8)[0]
            versions.append(_decode_macho_version(minimum))
        cursor += size
    if cursor != command_end:
        _fail("macOS load command sizes do not match the Mach-O header")
    if len(versions) != 1:
        _fail("macOS binary must declare exactly one minimum OS version")
    return versions[0]


def _binary_identity(data: bytes, kind: str) -> int:
    if kind == "pe":
        if len(data) < 64 or data[:2] != b"MZ":
            _fail("Windows binary is not PE (missing MZ header)")
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_offset + 6 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
            _fail("Windows binary has an invalid PE header")
        return struct.unpack_from("<H", data, pe_offset + 4)[0]
    if kind == "elf":
        if len(data) < 20 or data[:4] != b"\x7fELF" or data[4] != 2:
            _fail("Linux binary is not a 64-bit ELF")
        byte_order = {1: "<", 2: ">"}.get(data[5])
        if byte_order is None:
            _fail("Linux ELF has an invalid byte order")
        return struct.unpack_from(f"{byte_order}H", data, 18)[0]
    if kind == "macho":
        if len(data) < 8:
            _fail("macOS binary is too small for a Mach-O header")
        byte_order = {
            b"\xcf\xfa\xed\xfe": "<",  # MH_MAGIC_64, little endian
            b"\xfe\xed\xfa\xcf": ">",  # MH_CIGAM_64
        }.get(data[:4])
        if byte_order is None:
            _fail("macOS binary is not a thin 64-bit Mach-O")
        return struct.unpack_from(f"{byte_order}I", data, 4)[0]
    _fail(f"internal error: unknown binary format {kind!r}")
    raise AssertionError("unreachable")


def validate_native_binary(data: bytes, *, kind: str, architecture: int,
                           version: str,
                           macos_minimum: tuple[int, int, int] | None = None) -> None:
    if len(data) < 100_000:
        _fail(f"native binary is implausibly small: {len(data)} bytes")
    actual_arch = _binary_identity(data, kind)
    if actual_arch != architecture:
        _fail(f"native binary architecture mismatch: expected 0x{architecture:x}, "
              f"got 0x{actual_arch:x}")
    if kind == "macho":
        if macos_minimum is None:
            _fail("internal error: macOS minimum version is required")
        minimum = _macho_minimum_version(data)
        if minimum != macos_minimum:
            _fail(f"macOS binary minimum version must be "
                  f"{macos_minimum[0]}.{macos_minimum[1]}.{macos_minimum[2]}, "
                  f"got {minimum[0]}.{minimum[1]}.{minimum[2]}")
    elif macos_minimum is not None:
        _fail("internal error: minimum OS version applies only to macOS")
    try:
        validate_binary_privacy(data)
    except InvalidBinaryPrivacy as exc:
        _fail(f"native binary privacy check failed: {exc}")
    version_marker = b"agrep-release-version:" + version.encode("ascii")
    if version_marker not in data:
        _fail(f"native binary does not carry the release version marker {version!r}")


def _verify_record(zf: zipfile.ZipFile, record_name: str,
                   expected: set[str]) -> None:
    try:
        text = zf.read(record_name).decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        _fail(f"invalid RECORD: {exc}")
    if any(len(row) != 3 for row in rows):
        _fail("RECORD rows must each have exactly three fields")
    names = [row[0] for row in rows]
    if len(names) != len(set(names)):
        _fail("RECORD contains duplicate paths")
    for name in names:
        _safe_member(name)
    got = set(names)
    if got != expected:
        _fail("RECORD manifest mismatch: "
              f"missing={sorted(expected - got)} unexpected={sorted(got - expected)}")

    for name, digest, size_text in rows:
        if name == record_name:
            if digest or size_text:
                _fail("RECORD must leave its own digest and size empty")
            continue
        if not digest.startswith("sha256=") or not size_text.isdigit():
            _fail(f"RECORD lacks sha256/size for {name!r}")
        data = zf.read(name)
        expected_digest = digest.removeprefix("sha256=")
        actual_digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        if actual_digest != expected_digest:
            _fail(f"RECORD sha256 mismatch for {name!r}")
        if len(data) != int(size_text):
            _fail(f"RECORD size mismatch for {name!r}")


def validate(path: Path) -> tuple[str, int]:
    match = WHEEL_NAME.fullmatch(path.name)
    if match is None:
        _fail("filename must be agrep-<version>-py3-none-<release-platform>.whl")
    version = match.group("version")
    platform = match.group("platform")
    try:
        binary_name, binary_kind, expected_arch = PLATFORMS[platform]
    except KeyError:
        _fail(f"unsupported or non-release wheel platform: {platform!r}")

    raw = _wheel_bytes(path)
    central_offset, expected_entries = _zip_envelope(raw)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"cannot open wheel ZIP: {exc}")
    with zf:
        if zf.comment:
            _fail("wheel ZIP comments are forbidden")
        infos = zf.infolist()
        if len(infos) != expected_entries:
            _fail("ZIP end record member count differs from the central directory")
        _local_layout(raw, infos, central_offset)
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _fail("ZIP contains duplicate member names")
        total_size = 0
        total_compressed = 0
        for info in infos:
            _safe_member(info.filename)
            if info.is_dir():
                _fail(f"directory entries are not part of the wheel manifest: {info.filename!r}")
            if info.flag_bits:
                _fail(f"non-canonical ZIP flags for {info.filename!r}")
            if info.date_time != CANONICAL_ZIP_TIME:
                _fail(f"non-canonical ZIP timestamp for {info.filename!r}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                _fail(f"non-canonical ZIP compression for {info.filename!r}")
            if info.extra or info.comment:
                _fail(f"ZIP member metadata is forbidden: {info.filename!r}")
            if info.create_system not in {0, 3}:
                _fail(f"unsupported ZIP creator system for {info.filename!r}")
            if (info.create_version, info.extract_version, info.internal_attr,
                    info.volume) != (20, 20, 0, 0):
                _fail(f"non-canonical ZIP attributes for {info.filename!r}")
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG):
                _fail(f"non-regular ZIP member: {info.filename!r}")
            if info.file_size > MAX_MEMBER_BYTES:
                _fail(f"oversized ZIP member: {info.filename!r}")
            if info.compress_size > MAX_MEMBER_BYTES:
                _fail(f"oversized compressed ZIP member: {info.filename!r}")
            total_size += info.file_size
            total_compressed += info.compress_size
        if total_size > MAX_WHEEL_BYTES:
            _fail(f"oversized uncompressed wheel payload: {total_size} bytes")
        if total_compressed > MAX_WHEEL_BYTES:
            _fail(f"oversized compressed wheel payload: {total_compressed} bytes")
        corrupt = zf.testzip()
        if corrupt is not None:
            _fail(f"CRC failure in ZIP member: {corrupt!r}")

        dist_info = f"agrep-{version}.dist-info"
        expected = set(RUNTIME_FILES)
        expected.add(binary_name)
        expected.update(f"{dist_info}/{name}" for name in DIST_INFO_FILES)
        got = set(names)
        if got != expected:
            _fail("wheel manifest mismatch: "
                  f"missing={sorted(expected - got)} unexpected={sorted(got - expected)}")

        for info in infos:
            mode = info.external_attr >> 16
            expected_mode = _expected_member_mode(
                info.filename, binary_name, platform, dist_info)
            if info.create_system == 3 and mode != expected_mode:
                _fail(f"non-canonical ZIP mode for {info.filename!r}")
            if info.create_system == 0:
                permissions = {0, 0o600, 0o644}
                if info.filename == binary_name:
                    permissions.add(0o755)
                actual = mode & 0o777
                if actual not in permissions:
                    _fail(
                        f"non-canonical Windows ZIP mode {actual:#05o} "
                        f"for {info.filename!r}")

        for member in RUNTIME_FILES:
            source = _runtime_source(member)
            if zf.read(member) != source.read_bytes():
                _fail(f"wheel runtime source differs from checkout: {member!r}")

        binaries = {name for name in names if name.startswith("agrep/_bin/")}
        if binaries != {binary_name}:
            _fail(f"expected exactly one native binary {binary_name!r}, got {sorted(binaries)!r}")
        binary_info = zf.getinfo(binary_name)
        binary = zf.read(binary_name)
        validate_native_binary(
            binary,
            kind=binary_kind,
            architecture=expected_arch,
            version=version,
            macos_minimum=(11, 0, 0) if binary_kind == "macho" else None,
        )
        if platform.startswith(("macosx_", "manylinux_")):
            mode = binary_info.external_attr >> 16
            if not mode & 0o111:
                _fail("POSIX native binary is not executable in the wheel")

        try:
            validate_core_metadata(zf.read(f"{dist_info}/METADATA"), version)
        except InvalidMetadata as exc:
            _fail(f"invalid METADATA: {exc}")

        wheel_bytes = zf.read(f"{dist_info}/WHEEL")
        expected_wheel = (
            "Wheel-Version: 1.0\n"
            "Generator: hatchling 1.31.0\n"
            "Root-Is-Purelib: false\n"
            f"Tag: py3-none-{platform}\n"
        ).encode("ascii")
        if wheel_bytes != expected_wheel:
            _fail("WHEEL metadata differs from the pinned release builder")

        entry_points = zf.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        if entry_points.replace("\r\n", "\n") != (
                "[console_scripts]\nagrep = agrep.__main__:main\n"):
            _fail("unexpected console entry point metadata")

        for member, source in (
                (f"{dist_info}/licenses/LICENSE", ROOT / "LICENSE"),
                (f"{dist_info}/licenses/THIRD_PARTY_LICENSES.txt",
                 ROOT / "THIRD_PARTY_LICENSES.txt")):
            if zf.read(member) != source.read_bytes():
                _fail(f"bundled license differs from {source.name}")

        _verify_record(zf, f"{dist_info}/RECORD", expected)
    return platform, len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="validate the exact closed manifest of agrep release wheels")
    parser.add_argument("wheels", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.wheels:
        try:
            if not path.is_file():
                _fail("wheel path is not a file")
            platform, count = validate(path)
            print(f"wheel manifest ok: {path} ({platform}, {count} files)")
        except (InvalidWheel, zipfile.BadZipFile, OSError) as exc:
            failed = True
            print(f"wheel manifest FAILED: {path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
