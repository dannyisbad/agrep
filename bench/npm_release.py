#!/usr/bin/env python3
"""Validate and reconcile the two immutable agrep npm tarballs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tarfile
import time
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zlib


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAMES = ("@mundy/agrep", "agrep-cli")
PACKAGE_MEMBERS = (
    "package/LICENSE",
    "package/bin.js",
    "package/postinstall.js",
    "package/package.json",
    "package/README.md",
)
PACKAGE_MODES = {
    "package/LICENSE": 0o644,
    "package/bin.js": 0o755,
    "package/postinstall.js": 0o644,
    "package/package.json": 0o644,
    "package/README.md": 0o644,
}
VERSION_RE = re.compile(r"[0-9][A-Za-z0-9._+-]*")
MAX_TARBALL_BYTES = 2 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024
MAX_TAR_BYTES = 4 * 1024 * 1024
MAX_TAR_HEADERS = 16
MAX_MEMBERS = len(PACKAGE_MEMBERS)
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
NPM_TAR_MTIME = 499_162_500
PACKAGE_JSON_KEYS = frozenset({
    "author",
    "bin",
    "description",
    "engines",
    "files",
    "homepage",
    "keywords",
    "license",
    "name",
    "repository",
    "scripts",
    "version",
})


class IncompleteRelease(RuntimeError):
    """The registry has not exposed both tarballs yet."""


class PackageArtifact(NamedTuple):
    name: str
    version: str
    filename: str
    integrity: str


def _version(value: str) -> str:
    if VERSION_RE.fullmatch(value) is None:
        raise ValueError(f"invalid npm release version: {value!r}")
    return value


def expected_filenames(version: str) -> dict[str, str]:
    version = _version(version)
    return {
        "@mundy/agrep": f"mundy-agrep-{version}.tgz",
        "agrep-cli": f"agrep-cli-{version}.tgz",
    }


def _sha512_integrity(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha512-" + base64.b64encode(digest.digest()).decode("ascii")


def _validate_integrity(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        raise RuntimeError("npm registry returned a non-sha512 integrity")
    try:
        decoded = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except ValueError as exc:
        raise RuntimeError("npm registry returned malformed base64 integrity") from exc
    if len(decoded) != hashlib.sha512().digest_size:
        raise RuntimeError("npm registry returned a malformed sha512 integrity")
    return value


def _validate_source_package(package: object, version: str) -> None:
    if not isinstance(package, dict) or set(package) != PACKAGE_JSON_KEYS:
        raise RuntimeError("npm/package.json has an unexpected top-level contract")
    if package.get("name") != "@mundy/agrep" or package.get("version") != version:
        raise RuntimeError("npm/package.json identity differs from the release version")
    exact = {
        "bin": {"agrep": "bin.js"},
        "scripts": {"postinstall": "node postinstall.js"},
        "files": ["bin.js", "postinstall.js", "README.md", "LICENSE"],
        "engines": {"node": ">=16"},
        "repository": {
            "type": "git",
            "url": "git+https://github.com/dannyisbad/agrep.git",
        },
        "homepage": "https://github.com/dannyisbad/agrep#readme",
        "license": "MIT",
    }
    for key, expected in exact.items():
        if package.get(key) != expected:
            raise RuntimeError(f"npm/package.json has an unexpected {key} contract")


def _expected_contents(name: str, version: str) -> dict[str, bytes]:
    package_path = ROOT / "npm" / "package.json"
    package_bytes = package_path.read_bytes()
    package = json.loads(package_bytes)
    _validate_source_package(package, version)
    license_bytes = (ROOT / "LICENSE").read_bytes()
    if (ROOT / "npm" / "LICENSE").read_bytes() != license_bytes:
        raise RuntimeError("npm/LICENSE differs from the canonical root LICENSE")
    if name == "@mundy/agrep":
        readme = (ROOT / "npm" / "README.md").read_bytes()
    elif name == "agrep-cli":
        package["name"] = "agrep-cli"
        package["description"] = (
            "unscoped npm alias for @mundy/agrep, "
            "local search across AI coding-agent history"
        )
        package_bytes = (json.dumps(package, indent=2) + "\n").encode("utf-8")
        readme = (ROOT / "npm" / "alias" / "README.md").read_bytes()
    else:
        raise RuntimeError(f"unsupported npm package identity: {name}")
    return {
        "package/LICENSE": license_bytes,
        "package/bin.js": (ROOT / "npm" / "bin.js").read_bytes(),
        "package/postinstall.js": (ROOT / "npm" / "postinstall.js").read_bytes(),
        "package/package.json": package_bytes,
        "package/README.md": readme,
    }


def _read_single_gzip(path: Path) -> bytes:
    try:
        state = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect npm tarball {path.name}: {exc}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise RuntimeError(f"npm tarball is not a regular file: {path.name}")
    if state.st_size > MAX_TARBALL_BYTES:
        raise RuntimeError(f"npm tarball is oversized: {path.name}")
    raw = path.read_bytes()
    if len(raw) > MAX_TARBALL_BYTES:
        raise RuntimeError(f"npm tarball is oversized: {path.name}")
    if len(raw) < 18:
        raise RuntimeError(f"npm tarball is too small to be gzip: {path.name}")
    magic, method, flags, mtime, extra_flags, system = struct.unpack(
        "<HBBIBB", raw[:10])
    if magic != 0x8B1F or method != 8:
        raise RuntimeError(f"npm tarball is not gzip deflate: {path.name}")
    if flags != 0:
        raise RuntimeError(f"npm gzip header carries optional metadata: {path.name}")
    if (mtime, extra_flags, system) != (0, 2, 255):
        raise RuntimeError(f"npm gzip header is not canonical: {path.name}")
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        data = decoder.decompress(raw[10:], MAX_TAR_BYTES + 1)
        if decoder.unconsumed_tail:
            raise RuntimeError(f"npm tar payload is oversized: {path.name}")
        data += decoder.flush()
    except zlib.error as exc:
        raise RuntimeError(f"invalid npm gzip stream {path.name}: {exc}") from exc
    if len(data) > MAX_TAR_BYTES:
        raise RuntimeError(f"npm tar payload is oversized: {path.name}")
    if not decoder.eof or decoder.unconsumed_tail:
        raise RuntimeError(f"npm gzip stream is incomplete: {path.name}")
    if len(decoder.unused_data) != 8:
        raise RuntimeError(
            f"npm tarball has a second gzip stream or trailing bytes: {path.name}")
    expected_crc, expected_size = struct.unpack("<II", decoder.unused_data)
    if zlib.crc32(data) & 0xFFFF_FFFF != expected_crc:
        raise RuntimeError(f"npm gzip checksum mismatch: {path.name}")
    if len(data) & 0xFFFF_FFFF != expected_size:
        raise RuntimeError(f"npm gzip size trailer mismatch: {path.name}")
    return data


def _tar_size(header: bytes) -> int:
    field = header[124:136]
    if field[0] & 0x80:
        raise RuntimeError("npm tar uses a noncanonical binary size field")
    value = field.rstrip(b"\0 ")
    if not value or any(byte not in b"01234567" for byte in value):
        raise RuntimeError("npm tar has a malformed size field")
    return int(value, 8)


def _physical_headers(data: bytes, expected: dict[str, bytes]) -> list[int]:
    offsets = []
    offset = 0
    while True:
        if offset + 512 > len(data):
            raise RuntimeError("npm tar ends before its zero records")
        header = data[offset:offset + 512]
        if not any(header):
            if offset + 1024 != len(data) or any(data[offset:]):
                raise RuntimeError("npm tar physical end is not canonical")
            return offsets
        if len(offsets) >= MAX_TAR_HEADERS:
            raise RuntimeError("npm tar has too many physical headers")
        if len(offsets) >= len(PACKAGE_MEMBERS):
            raise RuntimeError("npm tar has an unexpected physical header")
        name = PACKAGE_MEMBERS[len(offsets)]
        name_bytes = name.encode("ascii")
        if header[:100] != name_bytes + b"\0" * (100 - len(name_bytes)):
            raise RuntimeError("npm tar physical member name or order drift")
        mode = PACKAGE_MODES[name]
        if header[100:108] != f"{mode:06o}".encode("ascii") + b" \0":
            raise RuntimeError(f"npm tar mode field drift: {name}")
        if header[108:124] != b"\0" * 16:
            raise RuntimeError(f"npm tar carries owner identifiers: {name}")
        size = _tar_size(header)
        if size != len(expected[name]):
            raise RuntimeError(f"npm tar physical member size drift: {name}")
        if header[124:136] != f"{size:010o}".encode("ascii") + b" \0":
            raise RuntimeError(f"npm tar size field is not canonical: {name}")
        if header[136:148] != (
                f"{NPM_TAR_MTIME:010o}".encode("ascii") + b" \0"):
            raise RuntimeError(f"npm tar timestamp field drift: {name}")
        checksum = sum(header[:148]) + 8 * ord(" ") + sum(header[156:])
        if header[148:156] != f"{checksum:06o}".encode("ascii") + b" \0":
            raise RuntimeError(f"npm tar checksum field drift: {name}")
        if header[156:157] != tarfile.REGTYPE:
            raise RuntimeError(f"npm tar physical member is not regular: {name}")
        if header[157:257] != b"\0" * 100:
            raise RuntimeError(f"npm tar physical member carries a link: {name}")
        if header[257:265] != b"ustar\0" + b"00":
            raise RuntimeError(f"npm tar format marker drift: {name}")
        if (header[265:329] != b"\0" * 64
                or header[329:345] != (b"000000 \0" * 2)
                or header[345:500] != b"\0" * 155
                or header[500:512] != b"\0" * 12):
            raise RuntimeError(f"npm tar extended metadata drift: {name}")
        offsets.append(offset)
        if size > MAX_MEMBER_BYTES:
            raise RuntimeError("npm tar physical member is oversized")
        data_end = offset + 512 + size
        offset += 512 + ((size + 511) // 512) * 512
        if offset > len(data):
            raise RuntimeError("npm tar member extends beyond the archive")
        if any(data[data_end:offset]):
            raise RuntimeError(f"npm tar member padding is not zero: {name}")


def _inspect_tarball(path: Path, name: str, version: str) -> None:
    expected = _expected_contents(name, version)
    tar_data = _read_single_gzip(path)
    physical_offsets = _physical_headers(tar_data, expected)

    try:
        archive = tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError(f"cannot open npm tarball {path.name}: {exc}") from exc
    with archive:
        if archive.pax_headers:
            raise RuntimeError(f"npm tarball has global PAX metadata: {path.name}")
        members = []
        while True:
            member = archive.next()
            if member is None:
                break
            members.append(member)
            if len(members) > MAX_MEMBERS:
                raise RuntimeError(f"npm tarball has too many members: {path.name}")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise RuntimeError(f"npm tarball repeats a member: {path.name}")
        if tuple(names) != PACKAGE_MEMBERS:
            raise RuntimeError(
                f"npm tarball manifest or order drift: {path.name}: {names}")
        if physical_offsets != [member.offset for member in members]:
            raise RuntimeError(f"npm tarball contains hidden headers: {path.name}")
        for member, physical_offset in zip(members, physical_offsets):
            if member.type != tarfile.REGTYPE or not member.isfile():
                raise RuntimeError(
                    f"npm tarball member is not a regular file: {member.name}")
            if member.mode != PACKAGE_MODES[member.name]:
                raise RuntimeError(
                    f"npm tarball member mode drift: {member.name}: {member.mode:o}")
            if (member.uid, member.gid, member.uname, member.gname) != (0, 0, "", ""):
                raise RuntimeError(
                    f"npm tarball member carries owner metadata: {member.name}")
            if member.mtime != NPM_TAR_MTIME:
                raise RuntimeError(
                    f"npm tarball member timestamp drift: {member.name}")
            if (member.pax_headers or member.linkname or member.devmajor != 0
                    or member.devminor != 0 or member.sparse is not None):
                raise RuntimeError(
                    f"npm tarball member carries extended metadata: {member.name}")
            if member.offset_data != physical_offset + 512:
                raise RuntimeError(
                    f"npm tarball member has a hidden data header: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None or extracted.read() != expected[member.name]:
                raise RuntimeError(
                    f"npm tarball member differs from checkout: {member.name}")


def local_manifest(directory: Path, version: str) -> dict[str, PackageArtifact]:
    expected = expected_filenames(version)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError(f"npm distribution directory is not regular: {directory}")
    entries = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"npm distribution is not a regular file: {path.name}")
        entries[path.name] = path
    if set(entries) != set(expected.values()):
        raise RuntimeError(
            "malformed npm distribution set: "
            f"missing={sorted(set(expected.values()) - set(entries))} "
            f"unexpected={sorted(set(entries) - set(expected.values()))}")
    output = {}
    for name, filename in expected.items():
        path = entries[filename]
        _inspect_tarball(path, name, version)
        output[name] = PackageArtifact(
            name=name, version=version, filename=filename,
            integrity=_sha512_integrity(path))
    return output


def _registry_package(name: str, version: str) -> dict | None:
    url = (
        "https://registry.npmjs.org/"
        f"{quote(name, safe='@')}/{quote(version, safe='')}"
    )
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "agrep-release-validator"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(MAX_REGISTRY_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(body) > MAX_REGISTRY_BYTES:
        raise RuntimeError(f"npm registry response is oversized for {name}")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError(f"npm registry response is not an object for {name}")
    if payload.get("name") != name or payload.get("version") != version:
        raise RuntimeError(f"npm registry returned the wrong identity for {name}")
    return payload


def remote_manifest(version: str) -> dict[str, str | None]:
    version = _version(version)
    output = {}
    for name in PACKAGE_NAMES:
        payload = _registry_package(name, version)
        if payload is None:
            output[name] = None
            continue
        dist = payload.get("dist")
        if not isinstance(dist, dict):
            raise RuntimeError(f"npm registry response has no dist object for {name}")
        output[name] = _validate_integrity(dist.get("integrity"))
    return output


def reconcile(
    local: dict[str, PackageArtifact],
    remote: dict[str, str | None],
    *,
    require_complete: bool,
) -> list[str]:
    expected = set(PACKAGE_NAMES)
    if set(local) != expected or set(remote) != expected:
        raise RuntimeError("npm reconciliation received an invalid package set")
    mismatched = sorted(
        name for name in PACKAGE_NAMES
        if remote[name] is not None and remote[name] != local[name].integrity)
    if mismatched:
        raise RuntimeError(
            "npm registry integrity mismatch for: " + ", ".join(mismatched))
    missing = [name for name in PACKAGE_NAMES if remote[name] is None]
    if require_complete and missing:
        raise IncompleteRelease(
            "npm release is incomplete; missing: " + ", ".join(missing))
    return missing


def stage_missing(
    source: Path,
    destination: Path,
    names: list[str],
    manifest: dict[str, PackageArtifact],
) -> None:
    unknown = set(names) - set(manifest)
    if unknown:
        raise RuntimeError(f"unknown npm package requested for staging: {sorted(unknown)}")
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        artifact = manifest[name]
        staged = destination / artifact.filename
        shutil.copyfile(source / artifact.filename, staged)
        _inspect_tarball(staged, name, artifact.version)
        if _sha512_integrity(staged) != artifact.integrity:
            raise RuntimeError(f"npm tarball moved while staging: {artifact.filename}")


def _append_output(path: Path, missing: list[str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"missing_count={len(missing)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--stage-missing", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.wait_seconds < 0:
        parser.error("--wait-seconds cannot be negative")
    if args.offline and (
            args.stage_missing or args.require_complete or args.wait_seconds):
        parser.error("--offline only validates the local npm distribution set")

    try:
        local = local_manifest(args.dist_dir, args.version)
        if args.offline:
            print("validated npm distribution set:", *(
                f"{artifact.filename} {artifact.integrity}"
                for artifact in local.values()), sep="\n  ")
            return 0

        deadline = time.monotonic() + args.wait_seconds
        while True:
            remote = remote_manifest(args.version)
            try:
                missing = reconcile(
                    local, remote, require_complete=args.require_complete)
            except IncompleteRelease:
                if time.monotonic() < deadline:
                    time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                raise
            break

        if args.stage_missing is not None:
            stage_missing(args.dist_dir, args.stage_missing, missing, local)
        if args.github_output is not None:
            _append_output(args.github_output, missing)
        if missing:
            print("npm packages to publish:", *missing, sep="\n  ")
        else:
            print("npm release already matches the sealed tarballs")
        return 0
    except (
            HTTPError, URLError, OSError, RuntimeError, ValueError,
            json.JSONDecodeError, tarfile.TarError) as exc:
        print(f"npm release reconciliation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
