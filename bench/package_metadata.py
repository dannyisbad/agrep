#!/usr/bin/env python3
"""Build the one canonical core-metadata document allowed in release archives."""

from __future__ import annotations

import ast
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
EXTRA_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class InvalidMetadata(ValueError):
    """Core package metadata differs from the release checkout."""


def _validated_extra_name(extra: object) -> str:
    if not isinstance(extra, str) or EXTRA_NAME.fullmatch(extra) is None:
        raise InvalidMetadata(f"unsupported optional dependency name: {extra!r}")
    return extra


def _optional_requires_dist(extra: object, requirement: object) -> str:
    """Render Hatchling's closed ``Requires-Dist`` form for one extra.

    A dependency's own marker must be grouped before the extra marker is
    conjoined.  Without those parentheses, an ``or`` in the dependency marker
    could make the dependency apply even when the extra was not requested.
    """
    extra = _validated_extra_name(extra)
    if not isinstance(requirement, str) or not requirement:
        raise InvalidMetadata("optional dependencies must be non-empty strings")
    if requirement != requirement.strip():
        raise InvalidMetadata(
            "optional dependencies cannot have leading or trailing whitespace")
    if "\r" in requirement or "\n" in requirement:
        raise InvalidMetadata("optional dependencies cannot contain header newlines")

    dependency, separator, marker = requirement.partition(";")
    if not separator:
        return f"{requirement}; extra == '{extra}'"
    marker = marker.strip()
    if (not dependency or dependency != dependency.strip()
            or not marker or ";" in marker):
        raise InvalidMetadata("optional dependency has an unsupported marker form")
    return f"{dependency}; ({marker}) and extra == '{extra}'"


def checkout_version(root: Path = ROOT) -> str:
    source = (root / "agrep" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="agrep/__init__.py")
    versions = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(versions) != 1:
        raise InvalidMetadata("agrep/__init__.py must define one literal __version__")
    return versions[0]


def normalize_distribution_version(raw: str) -> str:
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-(a|alpha|b|beta|rc)\.(0|[1-9]\d*))?",
        raw,
    )
    if match is None:
        raise InvalidMetadata(f"unsupported release version: {raw!r}")
    release = ".".join(match.group(1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return release
    label = {"alpha": "a", "beta": "b"}.get(prerelease, prerelease)
    return f"{release}{label}{match.group(5)}"


def distribution_version(root: Path = ROOT) -> str:
    return normalize_distribution_version(checkout_version(root))


def expected_core_metadata(version: str, *, root: Path = ROOT) -> bytes:
    if version != distribution_version(root):
        raise InvalidMetadata("archive version differs from agrep/__init__.py")
    if tomllib is None:
        raise InvalidMetadata("core metadata validation requires Python 3.11 or newer")
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    if project.get("name") != "agrep" or project.get("dynamic") != ["version"]:
        raise InvalidMetadata("pyproject has an unsupported project identity")
    if project.get("readme") != "README.md":
        raise InvalidMetadata("pyproject must use README.md as the package description")
    license_config = project.get("license")
    if license_config != {"file": "LICENSE"}:
        raise InvalidMetadata("pyproject must use the canonical LICENSE file")

    lines = [
        "Metadata-Version: 2.4",
        "Name: agrep",
        f"Version: {version}",
        f"Summary: {project['description']}",
    ]
    for label, url in project.get("urls", {}).items():
        lines.append(f"Project-URL: {label}, {url}")
    authors = project.get("authors", [])
    if not authors or any(set(author) != {"name"} for author in authors):
        raise InvalidMetadata("pyproject authors must contain names only")
    lines.append("Author: " + ", ".join(author["name"] for author in authors))

    license_text = (root / "LICENSE").read_text(encoding="utf-8").rstrip("\n")
    lines.append("License: " + license_text.replace("\n", "\n        "))
    for license_file in project.get("license-files", []):
        lines.append(f"License-File: {license_file}")
    lines.append("Keywords: " + ",".join(sorted(project.get("keywords", []))))
    lines.extend(f"Classifier: {value}" for value in project.get("classifiers", []))
    lines.append(f"Requires-Python: {project['requires-python']}")
    lines.extend(f"Requires-Dist: {value}" for value in project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise InvalidMetadata("pyproject optional dependencies must be a table")
    for extra in sorted(optional):
        extra = _validated_extra_name(extra)
        requirements = optional[extra]
        if not isinstance(requirements, list):
            raise InvalidMetadata("pyproject optional dependency groups must be lists")
        lines.append(f"Provides-Extra: {extra}")
        for requirement in requirements:
            lines.append(f"Requires-Dist: {_optional_requires_dist(extra, requirement)}")
    lines.append("Description-Content-Type: text/markdown")

    readme = (root / "README.md").read_bytes()
    return ("\n".join(lines) + "\n\n").encode("utf-8") + readme


def validate_core_metadata(data: bytes, version: str, *, root: Path = ROOT) -> None:
    expected = expected_core_metadata(version, root=root)
    if data != expected:
        raise InvalidMetadata(
            "core package metadata differs from pyproject.toml, LICENSE, or README.md")
