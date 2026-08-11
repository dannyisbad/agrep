"""Select exactly the source files allowed in agrep's sdist."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import stat


ROOT_FILES = frozenset({
    "Cargo.lock",
    "Cargo.toml",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_LICENSES.txt",
    "cli.py",
    "hatch_build.py",
    "hatch_sdist_build.py",
    "py/README.md",
    "pyproject.toml",
    "reindex.py",
    "rust-toolchain.toml",
    "sdist_sources.py",
})


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"invalid runtime manifest {label}: {value!r}")
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise RuntimeError(f"invalid runtime manifest {label}: {value!r}")
    return value


def source_files(root: Path) -> dict[str, Path]:
    root = root.resolve()
    files = {name: root / name for name in ROOT_FILES}
    manifest = root / "py" / "runtime_manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        entries = payload["files"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"invalid runtime manifest {manifest}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(entries, list):
        raise RuntimeError(f"invalid runtime manifest schema: {manifest}")
    seen_sources = set()
    seen_members = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source", "member"}:
            raise RuntimeError(f"invalid runtime manifest entry: {entry!r}")
        source = _relative(entry["source"], "source")
        member = _relative(entry["member"], "member")
        if source in seen_sources or member in seen_members:
            raise RuntimeError(f"duplicate runtime manifest entry: {entry!r}")
        seen_sources.add(source)
        seen_members.add(member)
        files[source] = root / source
    for path in sorted((root / "crates").glob("*/Cargo.toml")):
        files[path.relative_to(root).as_posix()] = path
    for path in sorted((root / "crates").glob("*/src/**/*.rs")):
        files[path.relative_to(root).as_posix()] = path
    for name, path in files.items():
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"sdist source is unreadable: {name}: {exc}") from exc
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise RuntimeError(f"sdist source is not a regular file: {name}")
    return dict(sorted(files.items()))
