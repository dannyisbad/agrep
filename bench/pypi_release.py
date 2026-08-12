#!/usr/bin/env python3
"""Validate and reconcile one immutable agrep PyPI distribution set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

from package_metadata import normalize_distribution_version


PLATFORMS = {
    "win_amd64",
    "win_arm64",
    "macosx_11_0_x86_64",
    "macosx_11_0_arm64",
    "manylinux_2_28_x86_64",
    "manylinux_2_28_aarch64",
}


class IncompleteRelease(RuntimeError):
    pass


def expected_filenames(version: str) -> set[str]:
    version = normalize_distribution_version(version)
    wheels = {
        f"agrep-{version}-py3-none-{platform}.whl"
        for platform in PLATFORMS
    }
    return {*wheels, f"agrep-{version}.tar.gz"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_manifest(directory: Path, version: str) -> dict[str, str]:
    expected = expected_filenames(version)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError(f"distribution directory is not regular: {directory}")
    files = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(
                f"distribution is not a regular file: {path.name}")
        files[path.name] = path
    if set(files) != expected:
        raise RuntimeError(
            "malformed local distribution set: "
            f"missing={sorted(expected - set(files))} "
            f"unexpected={sorted(set(files) - expected)}")
    return {name: _sha256(files[name]) for name in sorted(files)}


def remote_manifest(version: str) -> dict[str, str]:
    version = normalize_distribution_version(version)
    url = f"https://pypi.org/pypi/agrep/{version}/json"
    try:
        with urlopen(url, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    if not isinstance(payload, dict):
        raise RuntimeError("PyPI response is not an object")
    info = payload.get("info")
    if not isinstance(info, dict) or info.get("version") != version:
        raise RuntimeError("PyPI response reports a different version")
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise RuntimeError("PyPI response has no distribution list")
    manifest = {}
    for row in urls:
        if not isinstance(row, dict):
            raise RuntimeError("PyPI response contains a malformed distribution")
        name = row.get("filename")
        digests = row.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (not isinstance(name, str) or not name
                or not isinstance(digest, str) or len(digest) != 64):
            raise RuntimeError("PyPI response contains an invalid filename or SHA-256")
        if name in manifest:
            raise RuntimeError(f"PyPI response repeats {name}")
        manifest[name] = digest.lower()
    return manifest


def reconcile(local: dict[str, str], remote: dict[str, str],
              *, require_complete: bool) -> list[str]:
    unexpected = set(remote) - set(local)
    if unexpected:
        raise RuntimeError(
            f"PyPI release contains unexpected files: {sorted(unexpected)}")
    mismatched = sorted(
        name for name in set(local) & set(remote)
        if local[name] != remote[name])
    if mismatched:
        raise RuntimeError(
            f"PyPI release hash mismatch for: {', '.join(mismatched)}")
    missing = sorted(set(local) - set(remote))
    if require_complete and missing:
        raise IncompleteRelease(
            f"PyPI release is incomplete; missing: {', '.join(missing)}")
    return missing


def stage_missing(source: Path, destination: Path, names: list[str],
                  manifest: dict[str, str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        staged = destination / name
        shutil.copy2(source / name, staged)
        if _sha256(staged) != manifest[name]:
            raise RuntimeError(f"distribution moved while staging: {name}")


def _append_output(path: Path, missing: list[str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"missing_count={len(missing)}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--stage-missing", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds cannot be negative")
    if args.offline and (args.stage_missing or args.require_complete or args.wait_seconds):
        parser.error("--offline only validates the local distribution set")

    try:
        local = local_manifest(args.dist_dir, args.version)
        if args.offline:
            print("validated PyPI distribution set:", *local, sep="\n  ")
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
            print("PyPI files to publish:", *missing, sep="\n  ")
        else:
            print("PyPI release already matches the validated bundle")
        return 0
    except (HTTPError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"PyPI release reconciliation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
