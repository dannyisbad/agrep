#!/usr/bin/env python3
"""Prepare and verify the immutable GitHub release asset set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile

from package_metadata import checkout_version
import validate_wheel as wheel_validator

ROOT = Path(__file__).resolve().parents[1]
BINARY_TARGETS = {
    "agrep-rs-windows-x86_64.exe":
        ("pe", 0x8664, "win_amd64", "agrep/_bin/agrep-rs.exe"),
    "agrep-rs-windows-aarch64.exe":
        ("pe", 0xAA64, "win_arm64", "agrep/_bin/agrep-rs.exe"),
    "agrep-rs-macos-x86_64":
        ("macho", 0x01000007, "macosx_11_0_x86_64", "agrep/_bin/agrep-rs"),
    "agrep-rs-macos-aarch64":
        ("macho", 0x0100000C, "macosx_11_0_arm64", "agrep/_bin/agrep-rs"),
    "agrep-rs-linux-x86_64":
        ("elf", 62, "manylinux_2_28_x86_64", "agrep/_bin/agrep-rs"),
    "agrep-rs-linux-aarch64":
        ("elf", 183, "manylinux_2_28_aarch64", "agrep/_bin/agrep-rs"),
}
BINARY_NAMES = tuple(BINARY_TARGETS)
SIDECAR_NAMES = tuple(f"{name}.sha256" for name in BINARY_NAMES)
NOTICE_NAMES = ("LICENSE", "THIRD_PARTY_LICENSES.txt")
ASSET_NAMES = frozenset((*BINARY_NAMES, *SIDECAR_NAMES, *NOTICE_NAMES))
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_BINARY_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_entries(directory: Path) -> dict[str, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError(f"asset directory is not a regular directory: {directory}")
    entries = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"release asset is not a regular file: {path.name}")
        entries[path.name] = path
    return entries


def _require_names(actual: set[str], expected: set[str], label: str) -> None:
    if actual != expected:
        raise RuntimeError(
            f"malformed {label}: missing={sorted(expected - actual)} "
            f"unexpected={sorted(actual - expected)}")


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


def _write_text_lf(path: Path, value: str) -> None:
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write(value)


def _validate_binaries(entries: dict[str, Path]) -> None:
    version = checkout_version()
    for name, (kind, architecture, _, _) in BINARY_TARGETS.items():
        path = entries[name]
        size = path.stat().st_size
        if size > MAX_BINARY_BYTES:
            raise RuntimeError(f"release binary is oversized: {name}")
        try:
            wheel_validator.validate_native_binary(
                path.read_bytes(),
                kind=kind,
                architecture=architecture,
                version=version,
                macos_minimum=(11, 0, 0) if kind == "macho" else None,
            )
        except wheel_validator.InvalidWheel as exc:
            raise RuntimeError(f"invalid release binary {name}: {exc}") from exc


def validate_wheel_payloads(asset_directory: Path, wheel_directory: Path) -> None:
    assets = _directory_entries(asset_directory)
    missing_assets = set(BINARY_NAMES) - set(assets)
    if missing_assets:
        raise RuntimeError(
            f"release binary set is incomplete: missing={sorted(missing_assets)}")
    wheels = _directory_entries(wheel_directory)
    version = checkout_version()
    expected_wheels = {
        f"agrep-{version}-py3-none-{target[2]}.whl"
        for target in BINARY_TARGETS.values()
    }
    _require_names(set(wheels), expected_wheels, "release wheel set")

    for asset_name, (_, _, platform, member) in BINARY_TARGETS.items():
        wheel_name = f"agrep-{version}-py3-none-{platform}.whl"
        try:
            actual_platform, _ = wheel_validator.validate(wheels[wheel_name])
        except wheel_validator.InvalidWheel as exc:
            raise RuntimeError(f"invalid release wheel {wheel_name}: {exc}") from exc
        if actual_platform != platform:
            raise RuntimeError(f"release wheel platform mismatch: {wheel_name}")
        try:
            with zipfile.ZipFile(wheels[wheel_name]) as archive:
                wheel_binary = archive.read(member)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise RuntimeError(
                f"cannot read native payload from {wheel_name}: {exc}") from exc
        if wheel_binary != assets[asset_name].read_bytes():
            raise RuntimeError(
                f"raw release binary differs from wheel payload: {asset_name}")


def prepare(directory: Path, manifest_path: Path,
            wheel_directory: Path | None = None) -> dict[str, str]:
    if manifest_path.is_symlink():
        raise RuntimeError(f"manifest path is not a regular file: {manifest_path}")
    try:
        manifest_path.resolve().relative_to(directory.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("release asset manifest must be outside the asset directory")
    if manifest_path.exists() and not manifest_path.is_file():
        raise RuntimeError(f"manifest path is not a regular file: {manifest_path}")
    allowed = set(ASSET_NAMES)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.unlink(missing_ok=True)
    entries = _directory_entries(directory)
    unexpected = set(entries) - allowed
    missing = set((*BINARY_NAMES, *NOTICE_NAMES)) - set(entries)
    if missing or unexpected:
        raise RuntimeError(
            "malformed release binary set: "
            f"missing={sorted(missing)} unexpected={sorted(unexpected)}")
    _validate_binaries(entries)
    if wheel_directory is not None:
        validate_wheel_payloads(directory, wheel_directory)
    files = {}
    for name in NOTICE_NAMES:
        source = ROOT / name
        if entries[name].read_bytes() != source.read_bytes():
            raise RuntimeError(f"release notice differs from canonical {name}")
        files[name] = _sha256(entries[name])
    for name in BINARY_NAMES:
        binary = entries[name]
        digest = _sha256(binary)
        sidecar_name = f"{name}.sha256"
        sidecar_body = f"{digest}  {name}\n"
        _write_text_lf(
            directory / sidecar_name, sidecar_body)
        files[name] = digest
        files[sidecar_name] = hashlib.sha256(
            sidecar_body.encode("ascii")).hexdigest()

    payload = {"files": dict(sorted(files.items())), "schema": 1}
    with tempfile.NamedTemporaryFile(
            "w", encoding="ascii", newline="\n", dir=manifest_path.parent,
            prefix=".release-assets-", suffix=".json", delete=False) as stream:
        stream.write(_canonical_json(payload))
        temporary_manifest = Path(stream.name)
    try:
        verify(directory, temporary_manifest)
        temporary_manifest.replace(manifest_path)
    finally:
        temporary_manifest.unlink(missing_ok=True)
    return files


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(f"release asset manifest repeats key: {key}")
        value[key] = item
    return value


def load_manifest(manifest_path: Path) -> dict[str, str]:
    try:
        mode = manifest_path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(f"cannot inspect release asset manifest: {exc}") from exc
    if manifest_path.is_symlink() or not stat.S_ISREG(mode):
        raise RuntimeError(
            f"release asset manifest is not a regular file: {manifest_path}")
    raw = manifest_path.read_text(encoding="ascii")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid release asset manifest JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"files", "schema"}:
        raise RuntimeError("release asset manifest has an invalid schema")
    if type(payload["schema"]) is not int or payload["schema"] != 1:
        raise RuntimeError("release asset manifest has an unsupported schema")
    files = payload["files"]
    if not isinstance(files, dict) or set(files) != ASSET_NAMES:
        raise RuntimeError("release asset manifest has an invalid file set")
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise RuntimeError("release asset manifest has an invalid hash entry")
        if SHA256_RE.fullmatch(digest) is None:
            raise RuntimeError(f"release asset manifest has an invalid hash: {name}")
    if raw != _canonical_json(payload):
        raise RuntimeError("release asset manifest is not canonical JSON")
    return dict(files)


def verify(directory: Path, manifest_path: Path,
           wheel_directory: Path | None = None) -> None:
    expected = load_manifest(manifest_path)
    entries = _directory_entries(directory)
    _require_names(set(entries), set(ASSET_NAMES), "release asset set")
    for name in sorted(entries):
        if _sha256(entries[name]) != expected[name]:
            raise RuntimeError(f"release asset hash mismatch: {name}")
    _validate_binaries(entries)
    if wheel_directory is not None:
        validate_wheel_payloads(directory, wheel_directory)
    for name in BINARY_NAMES:
        digest = expected[name]
        want = f"{digest}  {name}\n".encode("ascii")
        if entries[f"{name}.sha256"].read_bytes() != want:
            raise RuntimeError(f"release asset sidecar mismatch: {name}.sha256")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path, metavar="DIRECTORY")
    mode.add_argument("--verify", type=Path, metavar="DIRECTORY")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheels", type=Path)
    args = parser.parse_args()

    try:
        if args.prepare is not None:
            files = prepare(args.prepare, args.manifest, args.wheels)
            print("prepared GitHub release assets:", *sorted(files), sep="\n  ")
        else:
            verify(args.verify, args.manifest, args.wheels)
            print("verified GitHub release assets")
        return 0
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"GitHub release asset validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
