#!/usr/bin/env python3
"""Verify that every published agrep identity agrees."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys


def _python_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__"
               for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise RuntimeError(f"{path}: could not read __version__")


def _adapter_count(root: Path) -> int:
    registry = (root / "crates/agrep-core/src/ingest/registry.rs").read_text(
        encoding="utf-8")
    match = re.search(r"pub static ADAPTERS:.*?=\s*&\[(.*?)\];", registry, re.S)
    if match is None:
        raise RuntimeError("could not find registry.rs::ADAPTERS")
    return len(re.findall(r"&crate::ingest::", match.group(1)))


def _readme_agent_count(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("| Agent | Store |")
    except ValueError as exc:
        raise RuntimeError(f"{path}: could not find agent store table") from exc
    if start + 1 >= len(lines) or not lines[start + 1].startswith("|---"):
        raise RuntimeError(f"{path}: agent store table has no separator")
    rows = 0
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows += 1
    return rows


def _toml_section(text: str, section: str) -> str:
    match = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise RuntimeError(f"could not find TOML section [{section}]")
    return match.group(1)


def _workspace_version(path: Path) -> str:
    section = _toml_section(path.read_text(encoding="utf-8"), "workspace.package")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', section)
    if match is None:
        raise RuntimeError(f"{path}: could not read workspace version")
    return match.group(1)


def _lock_versions(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    found = {}
    for block in re.findall(
            r"(?ms)^\[\[package\]\]\s*(.*?)(?=^\[\[package\]\]|\Z)", text):
        name = re.search(r'(?m)^name\s*=\s*"([^"]+)"\s*$', block)
        version = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', block)
        if name and version and name.group(1) in {"agrep-core", "agrep-ingest"}:
            if name.group(1) in found:
                raise RuntimeError(f"{path}: duplicate lock entry for {name.group(1)}")
            found[name.group(1)] = version.group(1)
    return found


def verify(root: Path, tag: str | None = None) -> tuple[str, int]:
    root = root.resolve()
    npm_manifest = json.loads(
        (root / "npm/package.json").read_text(encoding="utf-8"))
    versions = {
        "python": _python_version(root / "agrep/__init__.py"),
        "npm": npm_manifest["version"],
        "cargo": _workspace_version(root / "Cargo.toml"),
    }
    if npm_manifest.get("name") != "@mundy/agrep":
        raise RuntimeError(
            f"unexpected primary npm package name: {npm_manifest.get('name')!r}")
    locked = _lock_versions(root / "Cargo.lock")
    if set(locked) != {"agrep-core", "agrep-ingest"}:
        raise RuntimeError("Cargo.lock is missing an agrep workspace package")
    versions.update({f"lock:{name}": version for name, version in locked.items()})
    if len(set(versions.values())) != 1:
        raise RuntimeError("release version drift: "
                           + json.dumps(versions, sort_keys=True))

    version = versions["python"]
    manifests = sorted((root / "crates").glob("*/Cargo.toml"))
    if not manifests:
        raise RuntimeError("no crate manifests found")
    for manifest in manifests:
        package = _toml_section(
            manifest.read_text(encoding="utf-8"), "package")
        if not re.search(r"(?m)^version\.workspace\s*=\s*true\s*$", package):
            raise RuntimeError(
                f"{manifest.relative_to(root)}: version must inherit workspace version")

    expected_tag = f"v{version}"
    if tag is not None and tag != expected_tag:
        raise RuntimeError(f"release tag {tag!r} must be exactly {expected_tag}")

    adapters = _adapter_count(root)
    documented = _readme_agent_count(root / "README.md")
    if adapters != documented:
        raise RuntimeError(
            f"agent drift: registry.rs has {adapters} adapters, "
            f"README store table lists {documented}")
    return version, adapters


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag")
    args = parser.parse_args()
    tag = args.tag
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME") or ""
    try:
        version, adapters = verify(args.root, tag)
    except (KeyError, OSError, RuntimeError, SyntaxError, ValueError) as exc:
        print(f"release identity failed: {exc}", file=sys.stderr)
        return 1
    print(f"release identity verified: v{version}, {adapters} agents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
