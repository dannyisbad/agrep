#!/usr/bin/env python3
"""Reject private artifacts in the worktree, index, or reachable Git history."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Iterable


MAX_REPOSITORY_BYTES = 1_000_000
ROOT_SCRATCH = {
    "ar.err", "err.corrupt", "err.stderr", "full_recall.txt", "j1.txt",
    "j2.txt", "ns.err", "out.stderr", "out.stdout", "p1.err", "p1.out",
    "p2.out", "p3.out", "rc.stderr",
}
PRIVATE_PATH_PREFIXES = (
    "bench/adversarial/",
    "bench/gauntlet/",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
)
GIT_IDENTITY = re.compile(
    rb"^(author|committer|tagger) .* <([^<>\r\n]+)> [0-9]+ [+-][0-9]{4}$")
IPV4 = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network((address, prefix))
    for address, prefix in (
        (0x0A000000, 8), (0xAC100000, 12),
        (0xC0A80000, 16), (0x64400000, 10),
    )
)
HOME_PATH = re.compile(
    rb"(?i)(?:[A-Z]:[\\/]+Users[\\/]+|/Users/|/home/)"
    rb"([A-Za-z0-9][A-Za-z0-9._-]*)")
MACOS_TEMP_PATH = re.compile(
    rb"/var/folders/[A-Za-z0-9]{2}/[A-Za-z0-9_+-]{20,}/T(?:/|\b)")
SYNTHETIC_USERS = {
    b"alice", b"bob", b"example", b"private", b"private-builder",
    b"private-name", b"runner", b"runneradmin", b"shared", b"test",
    b"tester", b"u",
}
# Personal-name tokens are stored as one-way digests so the shipped gate
# never publishes the strings it bans; blobs only, because commit headers
# legitimately carry the public author identity.
PERSONAL_TOKEN_DIGESTS = frozenset((
    "37378669017bf56fc0ae893e7ed8796ce30490fd4b8703de919e234122518f29",
    "45b8ae52561799baf7f2cfc4490aadd5f5ff1a419a1f2a1193b5b57fd15facb8",
    "ebb3e4912528380ea2038f3e12a649d632b911d1e30c8e3c33c889e1ad419e43",
    "12ae16a9c30743879b65c10b273d458dd49fda9f16a7de0fb2ffc397715eada0",
    "668e2b73ac556a2f051304702da290160b29bad3392ddcc72074fefbee80c55a",
    "d37c5e7961d0ed500ecf0475346027cf2d7010b7083c411f953936b0e38b1ab2",
    "c5422c052bfbd7bbd9764e0467688b62193fec4fa32a1b13af28d1708d5870ec",
))
PERSONAL_TOKEN_EXEMPT_NAMES = frozenset(("THIRD_PARTY_LICENSES.txt",))
WORD_TOKEN = re.compile(rb"[A-Za-z0-9]{3,64}")

Finding = tuple[str, str]


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, input=input_bytes, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def worktree_paths(repo: Path) -> list[Path]:
    raw = _git(
        repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [repo / os.fsdecode(item) for item in raw.split(b"\0") if item]


def _relative(repo: Path, path: Path) -> tuple[str, Path] | None:
    candidate = path if path.is_absolute() else repo / path
    try:
        relative = Path(os.path.relpath(os.path.abspath(candidate), repo))
    except ValueError:
        return None
    if relative == Path("..") or (relative.parts and relative.parts[0] == ".."):
        return None
    return relative.as_posix(), candidate


def _path_reason(relative: str) -> str | None:
    if relative in ROOT_SCRATCH:
        return "root scratch output"
    if relative.startswith(PRIVATE_PATH_PREFIXES):
        return "local or transcript-derived evidence"
    if relative.startswith("bench/render/"):
        return "private capture or agent scratch tree"
    if relative.endswith((".local.md", ".local.json")):
        return "local-only metadata"
    return None


def _content_reasons(raw: bytes) -> list[str]:
    reasons = []
    if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
        reasons.append("credential-shaped content")
    for token in IPV4.findall(raw):
        try:
            address = ipaddress.ip_address(token.decode("ascii"))
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETWORKS):
            reasons.append("private-network address")
            break
    for match in HOME_PATH.finditer(raw):
        if match.group(1).lower() not in SYNTHETIC_USERS:
            reasons.append("non-synthetic user home path")
            break
    if MACOS_TEMP_PATH.search(raw):
        reasons.append("non-synthetic macOS temp path")
    return reasons


def _personal_token_reasons(raw: bytes) -> list[str]:
    tokens = {match.group(0).lower() for match in WORD_TOKEN.finditer(raw)}
    for token in tokens:
        if hashlib.sha256(token).hexdigest() in PERSONAL_TOKEN_DIGESTS:
            return ["personal-identity token"]
    return []


def _git_identity_reasons(raw: bytes) -> list[str]:
    header = raw.partition(b"\n\n")[0]
    reasons = []
    for line in header.splitlines():
        match = GIT_IDENTITY.fullmatch(line)
        if match is None:
            continue
        email = match.group(2).lower()
        _local, separator, domain = email.rpartition(b"@")
        domain = domain.rstrip(b".")
        if not separator:
            continue
        machine_local = (
            b"." not in domain
            or domain in {b"localhost", b"localdomain"}
            or domain.endswith((b".local", b".localdomain", b".(none)"))
        )
        if machine_local:
            role = match.group(1).decode("ascii")
            reasons.append(f"machine-local Git {role} identity")
    return reasons


def _scan_blob(label: str, raw: bytes, findings: list[Finding]) -> None:
    reason = _path_reason(label)
    if reason is not None:
        findings.append((label, reason))
    if len(raw) > MAX_REPOSITORY_BYTES:
        findings.append((
            label, f"repository file exceeds {MAX_REPOSITORY_BYTES} bytes"))
        return
    for content_reason in _content_reasons(raw):
        findings.append((label, content_reason))
    if Path(label).name not in PERSONAL_TOKEN_EXEMPT_NAMES:
        for token_reason in _personal_token_reasons(raw):
            findings.append((label, token_reason))


def scan_worktree(repo: Path, paths: list[Path] | None = None) -> list[Finding]:
    repo = repo.resolve()
    findings: list[Finding] = []
    for path in paths if paths is not None else worktree_paths(repo):
        located = _relative(repo, path)
        if located is None:
            findings.append((os.fspath(path), "path escapes repository"))
            continue
        relative, candidate = located
        if not os.path.lexists(candidate):
            continue
        reason = _path_reason(relative)
        if reason is not None:
            findings.append((relative, reason))
        try:
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                target = candidate.resolve(strict=False)
                if _relative(repo, target) is None:
                    findings.append((relative, "symlink escapes repository"))
                    continue
                if not target.exists():
                    findings.append((relative, "broken repository symlink"))
                    continue
                if not target.is_file():
                    continue
                raw = target.read_bytes()
            elif stat.S_ISREG(metadata.st_mode):
                raw = candidate.read_bytes()
            else:
                continue
        except OSError as exc:
            findings.append((relative, f"unreadable repository file: {exc}"))
            continue
        if len(raw) > MAX_REPOSITORY_BYTES:
            findings.append((
                relative,
                f"repository file exceeds {MAX_REPOSITORY_BYTES} bytes"))
            continue
        for content_reason in _content_reasons(raw):
            findings.append((relative, content_reason))
        if Path(relative).name not in PERSONAL_TOKEN_EXEMPT_NAMES:
            for token_reason in _personal_token_reasons(raw):
                findings.append((relative, token_reason))
    return sorted(set(findings))


def _cat_objects(repo: Path, object_ids: Iterable[str]):
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    try:
        for object_id in object_ids:
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n").split()
            if len(header) != 3 or header[1] == b"missing":
                raise RuntimeError(f"cannot read Git object {object_id}")
            size = int(header[2])
            raw = process.stdout.read(size)
            if len(raw) != size or process.stdout.read(1) != b"\n":
                raise RuntimeError(f"short read for Git object {object_id}")
            yield header[1].decode("ascii"), raw
    finally:
        process.stdin.close()
        process.wait()
        error = process.stderr.read().decode("utf-8", "replace")
        process.stdout.close()
        process.stderr.close()
        if process.returncode:
            raise RuntimeError(error.strip() or "git cat-file failed")


def _index_entries(repo: Path) -> list[tuple[str, str, str, str]]:
    entries = []
    for raw in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split()
        entries.append((os.fsdecode(name), object_id, mode, stage))
    return entries


def scan_index(repo: Path) -> list[Finding]:
    repo = repo.resolve()
    entries = _index_entries(repo)
    findings: list[Finding] = []
    objects = _cat_objects(repo, (entry[1] for entry in entries))
    for (relative, _object_id, mode, stage), (kind, raw) in zip(entries, objects):
        if stage != "0":
            findings.append((relative, "unmerged index entry"))
            continue
        if kind != "blob":
            findings.append((relative, f"unexpected index object type: {kind}"))
            continue
        _scan_blob(relative, raw, findings)
        if mode == "120000":
            target = Path(os.fsdecode(raw))
            linked = Path(os.path.normpath(Path(relative).parent / target))
            if target.is_absolute() or linked == Path("..") or (
                    linked.parts and linked.parts[0] == ".."):
                findings.append((relative, "symlink escapes repository"))
    return sorted(set(findings))


def scan_history(repo: Path, revision: str = "HEAD") -> list[Finding]:
    repo = repo.resolve()
    objects = []
    paths: dict[str, str] = {}
    for raw_line in _git(repo, "rev-list", "--objects", revision).splitlines():
        object_id, separator, raw_path = raw_line.partition(b" ")
        decoded_id = object_id.decode("ascii")
        objects.append(decoded_id)
        if separator:
            paths.setdefault(decoded_id, os.fsdecode(raw_path))
    findings: list[Finding] = []
    changed_paths = _git(
        repo, "log", "--format=", "--name-only", "-z", revision)
    for raw_path in changed_paths.split(b"\0"):
        if not raw_path:
            continue
        relative = os.fsdecode(raw_path)
        reason = _path_reason(relative)
        if reason is not None:
            findings.append((relative, reason))
    for object_id, (kind, raw) in zip(objects, _cat_objects(repo, objects)):
        if kind == "blob":
            _scan_blob(paths.get(object_id, f"<blob:{object_id[:12]}>"), raw, findings)
        elif kind in {"commit", "tag"}:
            label = f"<{kind}:{object_id[:12]}>"
            for reason in _content_reasons(raw):
                findings.append((label, reason))
            for reason in _git_identity_reasons(raw):
                findings.append((label, reason))
    return sorted(set(findings))


def scan(repo: Path, paths: list[Path] | None = None) -> list[Finding]:
    return scan_worktree(repo, paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--index", action="store_true",
                        help="scan the exact staged snapshot")
    history = parser.add_mutually_exclusive_group()
    history.add_argument(
        "--history", nargs="?", const="HEAD", metavar="REV",
        help="scan every blob, commit, and tag reachable from REV")
    history.add_argument(
        "--all-refs", action="store_true",
        help="scan every blob, commit, and tag reachable from any Git ref")
    parser.add_argument("--worktree", action="store_true",
                        help="scan tracked and untracked non-ignored files")
    args = parser.parse_args(argv)
    selected = (
        args.index or args.history is not None or args.all_refs or args.worktree)
    findings: list[Finding] = []
    if args.worktree or not selected:
        findings.extend(scan_worktree(args.repo))
    if args.index:
        findings.extend(scan_index(args.repo))
    if args.history is not None:
        findings.extend(scan_history(args.repo, args.history))
    if args.all_refs:
        findings.extend(scan_history(args.repo, "--all"))
    findings = sorted(set(findings))
    if findings:
        for path, reason in findings[:100]:
            safe_path = path.encode("unicode_escape").decode("ascii")[:240]
            print(f"{safe_path}: {reason}", file=sys.stderr)
        if len(findings) > 100:
            print(f"... {len(findings) - 100} more finding(s)", file=sys.stderr)
        return 1
    print("repository privacy scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
