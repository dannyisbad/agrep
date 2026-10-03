#!/usr/bin/env python3
"""Freeze the operator's stores into a read-only home: copy-on-write clones for files, SQLite
backups for databases. The live stores are only ever read; the frozen home can be indexed by
run.py --home while agents keep appending to the originals."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sandbox  # noqa: E402

MANIFEST_NAME = ".agrep-snapshot.json"
SQLITE_HEADER = b"SQLite format 3\x00"
CLONE_FILESYSTEMS = {"darwin": ("apfs",), "linux": ("btrfs", "xfs", "bcachefs")}
# A byte-copy fallback shows up as allocation; a fresh clone allocates mostly metadata. Free space
# is volume-wide and other writers move it, so this catches a gross fallback, not the exact cost.
ALLOCATION_SLACK_KB = 64 * 1024
ALLOCATION_FRACTION = 1 / 8


class SnapshotError(RuntimeError):
    pass


def mount_point(path: Path) -> Path:
    path = path.resolve()
    while not os.path.ismount(path) and path.parent != path:
        path = path.parent
    return path


def filesystem_type(path: Path) -> str:
    if sys.platform == "darwin":
        result = subprocess.run(["diskutil", "info", "-plist", str(mount_point(path))],
                                capture_output=True, env={"PATH": "/usr/sbin:/usr/bin:/bin"},
                                check=True, timeout=60)
        return str(plistlib.loads(result.stdout).get("FilesystemType", "")).lower()
    result = subprocess.run(["stat", "-f", "-c", "%T", str(path)], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, check=True, timeout=60)
    return result.stdout.strip().lower()


def require_clone_capable(source: Path, dest_parent: Path) -> str:
    supported = CLONE_FILESYSTEMS.get(sys.platform)
    if supported is None:
        options = "; ".join(f"{name} with {' or '.join(kinds)}" for name, kinds in CLONE_FILESYSTEMS.items())
        raise SnapshotError(f"{sys.platform} cannot clone; copy-on-write clones need {options}")
    kind = filesystem_type(dest_parent)
    if kind not in supported:
        raise SnapshotError(f"{dest_parent} is {kind or 'unknown'}; copy-on-write clones need "
                            f"{' or '.join(supported)}")
    if source.stat().st_dev != dest_parent.stat().st_dev:
        raise SnapshotError(f"{source} and {dest_parent} are on different volumes; cp would copy bytes")
    return kind


def free_kilobytes(path: Path) -> int:
    return shutil.disk_usage(path).free // 1024


def du_kilobytes(path: Path) -> int:
    result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, check=False, timeout=3600)
    return int(result.stdout.split()[0]) if result.stdout.strip() else 0


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def clone_tree(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "darwin":
        command = ["cp", "-cRp", str(source), str(dest)]
    else:
        command = ["cp", "-R", "--reflink=always", "--preserve=timestamps,mode", str(source), str(dest)]
    result = subprocess.run(command, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
                            check=False, timeout=3600)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise SnapshotError(f"cp exited {result.returncode} cloning {source.name}: "
                            f"{len(lines)} error line(s); first: {lines[0][:160] if lines else ''}")


def backup_sqlite(source: Path, dest: Path) -> None:
    """Consistent read-only snapshot through the backup API; WAL frames are folded in and the
    copy leaves WAL mode so the frozen store is one file without -wal/-shm sidecars."""
    import sqlite3
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(dest)
        try:
            origin.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close()
    finally:
        origin.close()
    os.chmod(dest, 0o600)


def store_root_for(home: Path, path: Path) -> tuple[str, str] | None:
    """(adapter, home-relative root) owning `path`, or None when no registry root covers it."""
    for adapter, relatives in sandbox.STORE_ROOTS.items():
        for relative in relatives:
            root = home / relative
            if path == root or root in path.parents:
                return adapter, relative
    return None


def plan(source_home: Path, discovered: list[tuple[str, Path]]) -> list[dict]:
    """One entry per store root that owns discovered content; SQLite content is backed up
    file by file, everything else is cloned as a whole root."""
    by_root: dict[tuple[str, str], list[Path]] = {}
    unmapped = []
    for adapter, path in discovered:
        owner = store_root_for(source_home, path)
        if owner is None:
            unmapped.append(f"{adapter}: {path.relative_to(source_home) if source_home in path.parents else path}")
            continue
        by_root.setdefault(owner, []).append(path)
    if unmapped:
        raise SnapshotError("content outside every known store root (extend sandbox.STORE_ROOTS): "
                            + "; ".join(sorted(unmapped)[:5]))
    entries = []
    for (adapter, relative), paths in sorted(by_root.items()):
        databases = [path for path in paths if path.is_file() and is_sqlite_file(path)]
        if databases and len(databases) != len(paths):
            raise SnapshotError(f"{relative} mixes SQLite and file content; refusing to guess")
        entries.append({"adapter": adapter, "relative": relative,
                        "kind": "sqlite-backup" if databases else "clone",
                        "files": len(paths),
                        "sources": sorted(str(path.relative_to(source_home)) for path in databases)})
    return entries


def freeze(source_home: Path, dest: Path, discovered: list[tuple[str, Path]]) -> dict:
    """Clone every discovered store root of `source_home` under `dest`, back up SQLite stores,
    estimate each copy's allocation from the volume's free-space change and write the manifest."""
    given = Path(os.path.abspath(source_home))
    source_home = source_home.resolve()
    # the census reports paths under the home as given; the root checks below compare resolved ones
    discovered = [(adapter, source_home / path.relative_to(given) if given in path.parents else path)
                  for adapter, path in discovered]
    dest = dest.resolve()
    if dest == source_home or dest in source_home.parents:
        raise SnapshotError(f"snapshot {dest} contains the source home {source_home}")
    if dest.exists() and any(dest.iterdir()):
        raise SnapshotError(f"snapshot destination {dest} is not empty")
    entries = plan(source_home, discovered)
    for entry in entries:
        root = source_home / entry["relative"]
        if dest == root or root in dest.parents:
            raise SnapshotError(f"snapshot {dest} lies inside the store root {root}")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    kind = require_clone_capable(source_home, dest.parent)
    manifest = {"source_home": str(source_home), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "filesystem": kind, "roots": entries}
    dest.mkdir(exist_ok=True, mode=0o700)
    free_start = free_kilobytes(dest)
    try:
        for entry in entries:
            source = source_home / entry["relative"]
            target = dest / entry["relative"]
            before = free_kilobytes(dest)
            started = time.monotonic()
            if entry["kind"] == "clone":
                clone_tree(source, target)
                entry["apparent_kilobytes"] = du_kilobytes(target)
            else:
                for relative in entry["sources"]:
                    backup_sqlite(source_home / relative, dest / relative)
                entry["apparent_kilobytes"] = sum(
                    (dest / relative).stat().st_size for relative in entry["sources"]) // 1024
            entry["seconds"] = round(time.monotonic() - started, 2)
            # free space moves under us (other processes write), so a negative delta reads as 0
            entry["allocated_kilobytes"] = max(0, before - free_kilobytes(dest))
            if entry["kind"] == "clone" and entry["allocated_kilobytes"] > (
                    entry["apparent_kilobytes"] * ALLOCATION_FRACTION + ALLOCATION_SLACK_KB):
                raise SnapshotError(
                    f"cloning {entry['relative']} allocated {entry['allocated_kilobytes']} KiB of "
                    f"{entry['apparent_kilobytes']} KiB apparent: cp fell back to copying bytes")
        manifest["allocated_kilobytes"] = max(0, free_start - free_kilobytes(dest))
        manifest["apparent_kilobytes"] = sum(entry["apparent_kilobytes"] for entry in entries)
        (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return manifest


def read_manifest(home: Path) -> dict | None:
    path = home / MANIFEST_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete(dest: Path) -> bool:
    """Remove a snapshot created here; refuses anything without our manifest."""
    if read_manifest(dest) is None:
        raise SnapshotError(f"{dest} carries no {MANIFEST_NAME}; not deleting")
    shutil.rmtree(dest)
    return not dest.exists()


def discover(source_home: Path, binary: Path, scratch: Path) -> list[tuple[str, Path]]:
    """`agrep-rs stores --paths` under a sealed env whose data dir is throwaway scratch."""
    data = scratch / "discover-data"
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = sandbox.sealed_env(home=source_home, data=data, scratch=scratch, binary=binary)
    env["AGREP_DATA_READONLY"] = str(sandbox.production_data_dir(source_home))
    runner = sandbox.CliRunner(env, cwd=scratch, default_timeout=1800)
    listing = runner.rs(["stores", "--paths"])
    if listing.returncode != 0:
        raise SnapshotError(f"stores --paths failed: rc={listing.returncode}")
    return [(row["name"], Path(row["path"])) for row in json.loads(listing.stdout)
            if row.get("state") == "available"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path, help="frozen home to create (or delete with --delete)")
    parser.add_argument("--source-home", type=Path, default=Path.home())
    parser.add_argument("--binary", type=Path, default=sandbox.resolve_binary())
    parser.add_argument("--delete", action="store_true", help="remove a snapshot made by this tool")
    args = parser.parse_args(argv)
    try:
        if args.delete:
            delete(args.dest)
            print(f"snapshot deleted: {args.dest}")
            return 0
        scratch = args.dest.parent / f".snapshot-scratch-{os.getpid()}"
        scratch.mkdir(parents=True, mode=0o700)
        try:
            discovered = discover(args.source_home, args.binary, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        manifest = freeze(args.source_home, args.dest, discovered)
    except SnapshotError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
