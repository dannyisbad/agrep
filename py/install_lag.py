"""Bounded local-install provenance for the doctor lag warning.

The check deliberately has no ambient-cwd or network fallback.  It only compares
an installed distribution with the local checkout named by that distribution's
PEP 610 ``direct_url.json``.  A normal ``uv tool install --from /path agrep``
records that relationship.  Missing, remote, malformed, or divergent provenance
is ``unavailable`` rather than a guessed warning.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

LAG_BOUND_SECONDS = 7 * 24 * 60 * 60
_DIRECT_URL_MAX_BYTES = 16 * 1024
_GIT_TOTAL_SECONDS = 0.35
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_DISTRIBUTION_MANIFEST_MAX_BYTES = 1024 * 1024
_DISTRIBUTION_MEMBER_MAX_BYTES = 16 * 1024 * 1024
_DISTRIBUTION_TOTAL_MAX_BYTES = 64 * 1024 * 1024


def _result(state: str, detail: str, **fields: object) -> dict:
    return {"state": state, "detail": detail, **fields}


def _inside(path: Path, root: Path) -> bool:
    try:
        child = os.path.normcase(os.fspath(path))
        parent = os.path.normcase(os.fspath(root))
        return os.path.commonpath((child, parent)) == parent
    except (OSError, ValueError):
        return False


def _trusted_provenance_stat(observed: os.stat_result) -> bool:
    """Require current-UID provenance that another local account cannot edit."""
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and int(observed.st_uid) != int(getuid()):
        return False
    return os.name == "nt" or not (stat.S_IMODE(observed.st_mode) & 0o022)


def _stat_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        int(observed.st_dev), int(observed.st_ino), int(observed.st_mode),
        int(observed.st_size), int(observed.st_mtime_ns),
        int(observed.st_ctime_ns), int(observed.st_uid),
    )


def _distribution_direct_url(
        distribution: object, module_path: Path) -> tuple[str, Path | None]:
    """Distinguish a dev checkout from installed-but-unproven metadata."""
    try:
        files = distribution.files  # type: ignore[attr-defined]
        located = distribution.locate_file("agrep")  # type: ignore[attr-defined]
        package_root = Path(located).resolve()
    except (AttributeError, OSError, TypeError, ValueError):
        return "unavailable", None
    try:
        resolved_module = module_path.resolve()
    except (OSError, ValueError):
        return "unavailable", None
    if not _inside(resolved_module, package_root):
        return "not-installed", None
    candidates = []
    try:
        for item in files or ():
            name = str(item).replace("\\", "/")
            if (name.endswith(".dist-info/direct_url.json")
                    and name.count("/") == 1):
                located = distribution.locate_file(item)  # type: ignore[attr-defined]
                candidates.append(Path(located))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return "unavailable", None
    if len(candidates) != 1:
        return "unavailable", None
    return "installed", candidates[0]


def _read_regular(path: Path, max_bytes: int) -> tuple[bytes, os.stat_result] | None:
    """Read one small non-symlink regular file from a stable open descriptor."""
    try:
        before = path.lstat()
    except OSError:
        return None
    if (not stat.S_ISREG(before.st_mode)
            or before.st_size < 0 or before.st_size > max_bytes):
        return None
    if not _trusted_provenance_stat(before):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        current = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode)
                or current.st_size > max_bytes
                or _stat_identity(before) != _stat_identity(current)):
            return None
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (len(data) > max_bytes or len(data) != current.st_size
                or _stat_identity(current) != _stat_identity(after)):
            return None
        return data, current
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _local_source(payload: object) -> Path | None:
    if not isinstance(payload, dict) or set(payload) - {
            "url", "dir_info", "vcs_info", "archive_info", "subdirectory"}:
        return None
    url = payload.get("url")
    if not isinstance(url, str) or len(url) > 8192 or "\0" in url:
        return None
    parts = urlsplit(url)
    if (parts.scheme.lower() != "file" or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        return None
    if parts.netloc not in ("", "localhost"):
        return None
    try:
        decoded = unquote(parts.path, errors="strict")
    except (UnicodeError, ValueError):
        return None
    if "\0" in decoded:
        return None
    local = url2pathname(decoded)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", local):
        local = local[1:]
    path = Path(local)
    if not path.is_absolute():
        return None
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_dir():
        return None
    provenance_kinds = sum(
        isinstance(payload.get(key), dict)
        for key in ("dir_info", "vcs_info")
    )
    if (provenance_kinds != 1 or payload.get("archive_info") is not None
            or payload.get("subdirectory") not in (None, "")):
        return None
    vcs_info = payload.get("vcs_info")
    if vcs_info is not None:
        commit_id = vcs_info.get("commit_id")
        if (vcs_info.get("vcs") != "git"
                or not isinstance(commit_id, str)
                or not _COMMIT_RE.fullmatch(commit_id)):
            return None
    # direct_url establishes the relationship; these cheap markers prevent a
    # stale/replaced path from being mistaken for the agrep source checkout.
    project = path / "pyproject.toml"
    init = path / "agrep" / "__init__.py"
    git_marker = path / ".git"
    try:
        source_stat = path.stat()
        git_stat = git_marker.lstat()
    except OSError:
        return None
    if (not _trusted_provenance_stat(source_stat)
            or not _trusted_provenance_stat(git_stat)
            or not (stat.S_ISREG(git_stat.st_mode)
                    or stat.S_ISDIR(git_stat.st_mode))):
        return None
    project_read = _read_regular(project, 256 * 1024)
    init_read = _read_regular(init, 64 * 1024)
    if project_read is None or init_read is None:
        return None
    prefix = project_read[0][:64 * 1024]
    if not re.search(rb"(?m)^name\s*=\s*[\"']agrep[\"']\s*$", prefix):
        return None
    return path


def _git_environment() -> dict[str, str]:
    # A caller can legitimately set Git selectors for its own work.  They must
    # not redirect this read to a different repository/object database.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    return environment


def _remaining_git_timeout(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(_GIT_TOTAL_SECONDS, remaining)


def _git_revision(
        source: Path, revision: str, *, deadline: float) -> tuple[str, int] | None:
    git = shutil.which("git")
    if git is None:
        return None
    timeout = _remaining_git_timeout(deadline)
    if timeout is None:
        return None
    try:
        completed = subprocess.run(
            [git, "--no-pager", "-c", "core.hooksPath=/dev/null",
             "-c", "core.fsmonitor=false", "-C",
             os.fspath(source), "show", "-s", "--no-show-signature",
             "--format=%H%x00%ct", revision, "--"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout,
            check=False, env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 256:
        return None
    try:
        commit_raw, timestamp_raw = completed.stdout.strip().split(b"\0", 1)
        commit = commit_raw.decode("ascii").lower()
        timestamp = int(timestamp_raw)
    except (UnicodeError, ValueError):
        return None
    if not _COMMIT_RE.fullmatch(commit):
        return None
    return commit, timestamp


def _git_is_ancestor(
        source: Path, ancestor: str, descendant: str,
        *, deadline: float) -> bool | None:
    git = shutil.which("git")
    if git is None:
        return None
    timeout = _remaining_git_timeout(deadline)
    if timeout is None:
        return None
    try:
        completed = subprocess.run(
            [git, "--no-pager", "-c", "core.hooksPath=/dev/null",
             "-c", "core.fsmonitor=false", "-C",
             os.fspath(source), "merge-base", "--is-ancestor",
             ancestor, descendant, "--"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout,
            check=False, env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def _payload_commit(payload: dict) -> str | None:
    info = payload.get("vcs_info")
    if not isinstance(info, dict) or info.get("vcs") != "git":
        return None
    value = info.get("commit_id")
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        return None
    return value.lower()


def _day(value: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(value))


def _budget_result() -> dict:
    return _result(
        "budget-exceeded",
        "installed-build provenance was deferred because the shared "
        "routine diagnostic budget expired",
    )


def _caller_budget_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _manifest_relative(value: object, *, prefix: str = "") -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    if prefix and not value.startswith(prefix):
        return None
    relative = value.removeprefix(prefix)
    path = Path(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in path.parts)):
        return None
    return path


def _distribution_build_id_at(
        root: Path, *, source_tree: bool, deadline: float | None,
) -> tuple[str | None, str | None]:
    manifest_path = root / "py" / "runtime_manifest.json"
    opened = _read_regular(
        manifest_path, _DISTRIBUTION_MANIFEST_MAX_BYTES)
    if opened is None:
        return None, f"distribution manifest is unreadable: {manifest_path}"
    try:
        payload = json.loads(
            opened[0].decode("utf-8"),
            object_pairs_hook=_unique_json_object)
        entries = payload["files"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError,
            _DuplicateJsonKey):
        return None, f"distribution manifest is malformed: {manifest_path}"
    if (payload.get("version") != 1
            or not isinstance(entries, list) or not entries):
        return None, f"distribution manifest has an invalid schema: {manifest_path}"

    members: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for entry in entries:
        if (not isinstance(entry, dict)
                or set(entry) != {"source", "member"}):
            return None, "distribution manifest has an invalid member"
        source = _manifest_relative(entry["source"])
        member = _manifest_relative(entry["member"], prefix="agrep/")
        if source is None or member is None:
            return None, "distribution manifest has an invalid member path"
        logical = f"agrep/{member.as_posix()}"
        if logical in seen:
            return None, "distribution manifest has a duplicate member"
        seen.add(logical)
        members.append((
            logical,
            root / (source if source_tree else member),
        ))

    digest = hashlib.sha256(b"agrep-distribution-runtime-v1\0")
    total = 0
    for logical, path in sorted(members):
        if _caller_budget_expired(deadline):
            return None, "distribution comparison exceeded the diagnostic budget"
        opened = _read_regular(path, _DISTRIBUTION_MEMBER_MAX_BYTES)
        if opened is None:
            return None, f"distribution member is unreadable: {path}"
        body = opened[0]
        total += len(body)
        if total > _DISTRIBUTION_TOTAL_MAX_BYTES:
            return None, "distribution members exceed the identity bound"
        encoded = logical.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "little"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "little"))
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()[:20], None


def _local_distribution_ids(
        runtime: Path, source: Path, *, deadline: float | None,
) -> tuple[str | None, str | None, str | None]:
    if runtime.parent.name != "py":
        return None, None, "installed runtime layout is not recognized"
    installed, problem = _distribution_build_id_at(
        runtime.parent.parent, source_tree=False, deadline=deadline)
    if installed is None:
        return None, None, problem
    source_id, problem = _distribution_build_id_at(
        source, source_tree=True, deadline=deadline)
    return installed, source_id, problem


def installed_master_lag(
        *, now: float | None = None, module_path: Path | None = None,
        distribution: object | None = None,
        bound_seconds: int = LAG_BOUND_SECONDS,
        deadline: float | None = None) -> dict:
    """Compare this installed local build to its recorded checkout's master.

    ``deadline`` is an optional absolute ``time.monotonic()`` deadline shared
    by a larger diagnostic. ``budget-exceeded`` is explicit; unavailable or
    deferred evidence never becomes a lag assertion.
    """
    if type(bound_seconds) is not int or bound_seconds < 0:
        raise ValueError("bound_seconds must be a non-negative integer")
    if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))):
        raise ValueError("deadline must be a finite monotonic timestamp")
    deadline = None if deadline is None else float(deadline)
    if _caller_budget_expired(deadline):
        return _budget_result()
    checked_at = time.time() if now is None else float(now)
    if not (0 < checked_at < 253_402_300_800):
        return _result("unavailable", "clock is outside the supported range")
    runtime = Path(__file__) if module_path is None else Path(module_path)
    if distribution is None:
        try:
            distribution = metadata.distribution("agrep")
        except metadata.PackageNotFoundError:
            return _result("not-installed", "running from a source checkout")
        except (OSError, TypeError, ValueError):
            return _result("unavailable", "installed package metadata is unreadable")
    distribution_state, direct_url = _distribution_direct_url(distribution, runtime)
    if distribution_state == "not-installed":
        return _result("not-installed", "running from a source checkout")
    if direct_url is None:
        return _result(
            "unavailable", "installed package has no unique local-source provenance")
    opened = _read_regular(direct_url, _DIRECT_URL_MAX_BYTES)
    if opened is None:
        return _result("unavailable", "installed source provenance is unreadable")
    raw, _direct_stat = opened
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey):
        return _result("unavailable", "installed source provenance is malformed")
    source = _local_source(payload)
    if source is None:
        return _result("unavailable", "installed build has no verified local source checkout")
    installed_commit = _payload_commit(payload)
    if installed_commit is None:
        installed_id, source_id, problem = _local_distribution_ids(
            runtime, source, deadline=deadline)
        if installed_id is None or source_id is None:
            if _caller_budget_expired(deadline):
                return _budget_result()
            return _result(
                "unavailable",
                problem or "exact local-source comparison is unavailable",
                installed_basis="distribution-content",
                installed_commit=None,
                source=os.fspath(source),
            )
        exact = {
            "installed_basis": "distribution-content",
            "installed_commit": None,
            "installed_distribution_id": installed_id,
            "source_distribution_id": source_id,
            "source": os.fspath(source),
        }
        if installed_id == source_id:
            return _result(
                "current",
                f"matches recorded local source exactly "
                f"(distribution {installed_id})",
                **exact,
            )
        return _result(
            "lagging",
            f"installed distribution {installed_id} differs from recorded "
            f"local source {source_id}",
            remedy="replace-installed-tool",
            remedy_argv=[
                "uv", "tool", "install", "--force", "--from",
                os.fspath(source), "agrep",
            ],
            **exact,
        )

    git_started = time.monotonic()
    git_deadline = git_started + _GIT_TOTAL_SECONDS
    if deadline is not None:
        git_deadline = min(git_deadline, deadline)
    if git_deadline <= git_started:
        return _budget_result()
    master = _git_revision(source, "refs/heads/master", deadline=git_deadline)
    if master is None:
        if _caller_budget_expired(deadline):
            return _budget_result()
        return _result("unavailable", "local source master cannot be verified")
    master_commit, master_time = master
    installed = _git_revision(
        source, installed_commit, deadline=git_deadline)
    if installed is None:
        if _caller_budget_expired(deadline):
            return _budget_result()
        return _result(
            "unavailable", "installed revision cannot be verified in local master")
    ancestor = _git_is_ancestor(
        source, installed_commit, master_commit, deadline=git_deadline)
    if ancestor is None and _caller_budget_expired(deadline):
        return _budget_result()
    if ancestor is None:
        return _result(
            "unavailable",
            "installed revision ancestry could not be verified within "
            "the local Git budget",
        )
    if ancestor is False:
        return _result(
            "unavailable", "installed revision is not an ancestor of local master")
    _resolved_commit, installed_time = installed
    future_slack = 24 * 60 * 60
    if (installed_time <= 0 or master_time <= 0
            or installed_time > checked_at + future_slack
            or master_time > checked_at + future_slack):
        return _result("unavailable", "install or source timestamp is not credible")
    gap_seconds = max(0, master_time - installed_time)
    gap_days = gap_seconds / (24 * 60 * 60)
    common = {
        "gap_seconds": gap_seconds,
        "gap_days": gap_days,
        "bound_seconds": bound_seconds,
        "installed_basis": "commit",
        "installed_commit": installed_commit,
        "master_commit": master_commit,
        "source": os.fspath(source),
    }
    if gap_seconds > bound_seconds:
        detail = (
            f"lags local master by {gap_days:.1f} days "
            f"(installed {_day(installed_time)}; master {master_commit[:12]} "
            f"from {_day(master_time)})"
        )
        return _result(
            "lagging", detail,
            remedy="replace-installed-tool",
            remedy_argv=[
                "uv", "tool", "install", "--force", "--from",
                os.fspath(source), "agrep",
            ],
            **common,
        )
    detail = (
        f"within {bound_seconds / 86_400:.0f}-day local-master bound "
        f"(master {master_commit[:12]}; gap {gap_days:.1f} days)"
    )
    return _result("current", detail, **common)
