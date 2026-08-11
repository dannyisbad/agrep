#!/usr/bin/env python3
"""Generate notices for the locked Rust graph used to build agrep-rs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "THIRD_PARTY_LICENSES.txt"


def _metadata() -> dict:
    run = subprocess.run(
        ["cargo", "metadata", "--locked", "--format-version", "1"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if run.returncode:
        raise RuntimeError(run.stderr.strip() or "cargo metadata failed")
    return json.loads(run.stdout)


def _license_files(package: dict) -> list[Path]:
    root = Path(package["manifest_path"]).parent
    found = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        upper = path.name.upper()
        if not upper.startswith(("LICENSE", "COPYING", "NOTICE")):
            continue
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise RuntimeError(
                f"{package['name']} {package['version']} has a non-regular license file")
        found.append(path)
    if not found:
        raise RuntimeError(
            f"{package['name']} {package['version']} has no license file")
    return found


def _normalized_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def generate() -> str:
    packages = sorted(
        (package for package in _metadata()["packages"]
         if package.get("source") is not None),
        key=lambda package: (package["name"].lower(), package["version"]))
    texts: dict[str, list[str]] = {}
    inventory = []
    for package in packages:
        license_id = package.get("license")
        if not license_id:
            raise RuntimeError(
                f"{package['name']} {package['version']} has no license metadata")
        repository = package.get("repository") or package.get("homepage") or "-"
        inventory.append(
            f"{package['name']} {package['version']} | {license_id} | {repository}")
        for path in _license_files(package):
            label = f"{package['name']} {package['version']} ({path.name})"
            texts.setdefault(_normalized_text(path), []).append(label)

    lines = [
        "agrep third-party software notices",
        "==================================",
        "",
        "This file covers the locked Rust dependency graph used to build agrep-rs",
        "and the Unicode 16.0 data compiled into its boundary-ranking tables.",
        "It is generated from Cargo.lock by bench/third_party_licenses.py.",
        "",
        "Package inventory",
        "-----------------",
        *inventory,
        "",
        "License texts",
        "-------------",
    ]
    for index, (text, labels) in enumerate(sorted(
            texts.items(), key=lambda item: tuple(item[1])), 1):
        lines.extend([
            "",
            f"[{index}] Applies to:",
            *(f"- {label}" for label in sorted(labels)),
            "",
            text.rstrip(),
        ])
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    if path.is_symlink():
        raise RuntimeError("notice output is a symlink")
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate or verify bundled Rust dependency notices")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = generate()
        if args.write:
            _write_atomic(OUTPUT, expected)
            print(f"wrote {OUTPUT}")
            return 0
        actual = OUTPUT.read_text(encoding="utf-8")
        if actual != expected:
            raise RuntimeError(
                "THIRD_PARTY_LICENSES.txt is stale; run "
                "python bench/third_party_licenses.py --write")
        print("third-party license notices match Cargo.lock")
        return 0
    except (OSError, UnicodeError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"third-party license check FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
