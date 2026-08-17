"""Teach detected agent CLIs how to search the history agrep supports.

Public surface (via cli.py):

    agrep setup              after the dependency tiers, offers the teach step:
                             consent prompt listing exactly which files get the
                             recall block, then writes it - existence, capability,
                             when to use, and a demonstrated terminal exchange -
                             into each detected agent's global instructions file
                             (AGENTS.md and friends; marker-delimited, idempotent,
                             versioned). Consent is asked once: enrolled boxes
                             converge silently on later runs. --yes accepts
                             for scripts; a non-tty run never prompts and never
                             writes unenrolled.
    agrep remove             take all of it back out (blocks, hook, sentinel,
                             enrollment).

Hidden verbs (back-compat / plumbing):

    agrep inject             the old explicit verb: teach without the consent
                             prompt (typing it IS consent). --remove =
                             `agrep remove`.

The block is written for frontier-model readers: it states what exists and what
it can do, names the moments it pays off, then SHOWS the tool being used - agents
have years of terminal training, so a demonstrated grep-shaped exchange lands
harder than prose. Every write is gated on the agent's own home dir existing.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from hookless import registry as agent_registry
from hookless.locators import opencode_data_dirs

import common
import indexd_runtime
import ownerfile
import removal_fence
import surface_policy as surface

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()
STATE_PATH = common.DATA_DIR / "teach.json"
RECONCILE_HEALTH = "teach-reconcile.json"
_RECONCILE_HEALTH_MAX_BYTES = 64 * 1024
_RECONCILE_STATE_MAX_BYTES = 64 * 1024
_RECONCILE_TARGET_MAX_BYTES = 1024 * 1024
_RECONCILE_FIELD_MAX_CHARS = 1024
_LAST_RECONCILE_HEALTH: dict | None = None
_PENDING_RECONCILE_HEALTH: dict | None = None


def _data_dir_readonly() -> bool:
    return common.data_dir_readonly(common.DATA_DIR)


def _readonly_error() -> PermissionError:
    return PermissionError(
        "AGREP_DATA_READONLY protects this data directory")


# Bump NUDGE_V on ANY block-text change (selftest hash-enforces). Write-fight
# tiebreaker: a process only rewrites blocks older than its own, so a stale daemon
# can't byte-flip newer text every reconcile tick and shred agents' prompt cache.
NUDGE_V = 37
MARK_PREFIX = "<!-- agrep:recall"
MARK_BEGIN = f"{MARK_PREFIX} v{NUDGE_V} -->"
MARK_END = "<!-- /agrep:recall -->"

# Block text lives in nudge_default.md / nudge_codex.md so the owner iterates
# on prose without touching code; codex has its own file, everything else
# shares the default. Bump NUDGE_V on ANY change to either (hash-enforced).
_PROMPTS_DIR = Path(__file__).resolve().parent


def _nudge_source(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").rstrip("\n")


NUDGE = _nudge_source("nudge_default.md")
NUDGE_CODEX = _nudge_source("nudge_codex.md")


def manifest_targets(
        kind: str, *, home: Path | None = None,
        targets: tuple[agent_registry.TeachTarget, ...] | None = None,
        environ: dict[str, str] | None = None,
        os_name: str | None = None,
) -> list[tuple[str, Path, Path]]:
    """Resolve the manifest's portable teaching paths for this machine."""
    if kind not in ("markdown", "skill"):
        raise ValueError(f"unknown teach target kind {kind!r}")
    root = HOME if home is None else Path(home)
    roots = {
        "home": root,
        "opencode_data": Path(opencode_data_dirs(
            str(root), environ=environ, os_name=os_name)[0]),
    }

    def resolve(spec: agent_registry.TeachPath) -> Path:
        return roots[spec.base].joinpath(*spec.parts)

    source = agent_registry.TEACH_TARGETS if targets is None else targets
    resolved = [
        (spec.label, resolve(spec.proof), resolve(spec.target))
        for spec in source
        if spec.kind == kind
    ]
    # codex honors CODEX_HOME as its live home; an active non-default home is
    # an additional teach target or its user never sees the block.
    env = os.environ if environ is None else environ
    raw_codex_home = env.get("CODEX_HOME")
    has_codex = any(
        spec.kind == "markdown" and spec.label == "codex" for spec in source)
    if kind == "markdown" and has_codex and raw_codex_home:
        supplied = Path(os.path.expanduser(raw_codex_home))
        candidate = Path(os.path.abspath(supplied))
        default = Path(os.path.abspath(root / ".codex"))
        if supplied.is_absolute() and candidate != default:
            active = ("codex", candidate, candidate / "AGENTS.md")
            if active not in resolved:
                resolved.append(active)
    return resolved


# Setup tests replace these lists to isolate filesystem mutations.
MD_TARGETS = manifest_targets("markdown")
SKILL_TARGETS = manifest_targets("skill")

_SKILL_FRONT = """\
---
name: agrep-recall
description: Search this machine's cross-agent chat history (Claude, Codex, Cursor, more) for already-solved problems - errors already debugged, decisions already argued out, code already written. Use when a failure smells previously seen, when the user says "again" or "like last time" or references work you can't see, or when the exact text of an earlier conversation matters.
---

"""

_MARK_PREFIX_B = MARK_PREFIX.encode("ascii")
_MARK_END_B = MARK_END.encode("ascii")
_MARK_BEGIN_LINE_RE = re.compile(
    rb"<!-- agrep:recall v([0-9]+) -->\Z")


class BlockStructureError(ValueError):
    pass


class _BlockSpan(NamedTuple):
    start: int
    end: int
    version: int


def _bounded_safe(value: object) -> str:
    rendered = common.terminal_safe(value)
    if len(rendered) <= _RECONCILE_FIELD_MAX_CHARS:
        return rendered
    half = (_RECONCILE_FIELD_MAX_CHARS - 3) // 2
    return f"{rendered[:half]}...{rendered[-half:]}"


def _reconcile_health_path() -> Path:
    return common.DATA_DIR / RECONCILE_HEALTH


def _reconcile_issue(path: Path, kind: str, reason: object) -> dict[str, str]:
    return {
        "path": _bounded_safe(path),
        "kind": kind,
        "reason": _bounded_safe(reason),
    }


def _health_unavailable(value: dict, reason: object) -> dict:
    issue = _reconcile_issue(
        _reconcile_health_path(), "health-unavailable",
        f"reconcile health could not be persisted: {reason}")
    return {
        **value,
        "state": "health-unavailable",
        "refusals": [issue, *value["refusals"]],
    }


def _carry_health_unavailable(pending: dict, value: dict) -> dict:
    issue = next(
        row for row in pending["refusals"]
        if row["kind"] == "health-unavailable")
    return {
        **value,
        "state": "health-unavailable",
        "refusals": [dict(issue), *value["refusals"]],
    }


def _write_reconcile_health(value: dict) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    path = _reconcile_health_path()
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    try:
        if ownerfile.snapshot(
                path, max_bytes=_RECONCILE_HEALTH_MAX_BYTES).raw == body:
            return
    except OSError:
        pass
    tmp: Path | None = None
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        common.replace_with_retry(tmp, path)
        if os.name != "nt":
            directory = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _valid_reconcile_field(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _RECONCILE_FIELD_MAX_CHARS
        and common.terminal_safe(value) == value
    )


def _valid_reconcile_rows(
        value: object, *, max_rows: int,
        allowed_kinds: frozenset[str],
) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or len(value) > max_rows:
        return None
    rows: list[dict[str, str]] = []
    paths: set[str] = set()
    for item in value:
        if (not isinstance(item, dict)
                or set(item) != {"path", "kind", "reason"}):
            return None
        path, kind, reason = item["path"], item["kind"], item["reason"]
        if (not all(_valid_reconcile_field(field)
                    for field in (path, kind, reason))
                or kind not in allowed_kinds or path in paths):
            return None
        paths.add(path)
        rows.append({"path": path, "kind": kind, "reason": reason})
    return rows


def _valid_reconcile_outcome(
        state: str, repaired: list[str], refusals: list[dict[str, str]],
        preserved: list[dict[str, str]],
) -> bool:
    repaired_paths = set(repaired)
    refusal_paths = {row["path"] for row in refusals}
    preserved_paths = {row["path"] for row in preserved}
    health_failures = sum(
        row["kind"] == "health-unavailable" for row in refusals)
    if (repaired_paths & refusal_paths or repaired_paths & preserved_paths
            or refusal_paths & preserved_paths):
        return False
    if state == "clean":
        return not repaired and not refusals and not preserved
    if state == "repaired":
        return bool(repaired) and not refusals and not preserved
    if state == "preserved-newer":
        return bool(preserved) and not refusals and not health_failures
    if state == "refused":
        return bool(refusals) and not health_failures
    if state == "health-unavailable":
        return health_failures == 1
    return False


def _unique_reconcile_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate reconcile health field: {key}")
        value[key] = item
    return value


def reconcile_health() -> dict:
    """Return the bounded durable outcome consumed by index status and doctor."""
    path = _reconcile_health_path()
    try:
        observed = ownerfile.snapshot(
            path, max_bytes=_RECONCILE_HEALTH_MAX_BYTES)
        value = json.loads(
            observed.raw.decode("utf-8"),
            object_pairs_hook=_unique_reconcile_object)
        if (not isinstance(value, dict)
                or set(value) != {
                    "version", "state", "repaired", "refusals",
                    "preserved_newer"}
                or type(value.get("version")) is not int
                or value["version"] != 1
                or value.get("state") not in {
                    "clean", "repaired", "preserved-newer", "refused",
                    "health-unavailable"}
                or not isinstance(value.get("repaired"), list)
                or len(value["repaired"]) > len(MD_TARGETS + SKILL_TARGETS)
                or not all(_valid_reconcile_field(item)
                           for item in value["repaired"])
                or len(set(value["repaired"])) != len(value["repaired"])):
            raise ValueError("health record has an invalid schema")
        enrolled_count = len(_state_paths(
            _load_state().get("targets"), include_retired=True))
        target_count = max(len(MD_TARGETS + SKILL_TARGETS), enrolled_count)
        refusals = _valid_reconcile_rows(
            value.get("refusals"), max_rows=target_count + 1,
            allowed_kinds=frozenset({
                "malformed-markers", "unowned-skill", "invalid-utf8",
                "target-unreadable", "health-unavailable", "drifted",
                "concurrent-edit", "removal-pending",
            }))
        preserved = _valid_reconcile_rows(
            value.get("preserved_newer"), max_rows=target_count,
            allowed_kinds=frozenset({"preserved-newer"}))
        if (refusals is None or preserved is None
                or (value["state"] != "health-unavailable"
                    and len(refusals) > target_count)
                or not _valid_reconcile_outcome(
                    value["state"], value["repaired"], refusals, preserved)):
            raise ValueError("health record has invalid issue rows")
        return {**value, "refusals": refusals,
                "preserved_newer": preserved}
    except FileNotFoundError:
        return {"version": 1, "state": "not-checked", "repaired": [],
                "refusals": [], "preserved_newer": []}
    except (OSError, UnicodeError, ValueError, RecursionError,
            json.JSONDecodeError) as exc:
        return {
            "version": 1,
            "state": "unreadable",
            "repaired": [],
            "refusals": [_reconcile_issue(
                path, "health-unreadable", f"reconcile health is unreadable: {exc}")],
            "preserved_newer": [],
        }


def current_reconcile_health() -> dict:
    value = _LAST_RECONCILE_HEALTH
    if value is None:
        return reconcile_health()
    return {
        **value,
        "repaired": list(value["repaired"]),
        "refusals": [dict(row) for row in value["refusals"]],
        "preserved_newer": [dict(row) for row in value["preserved_newer"]],
    }


def _block_spans(raw: bytes) -> list[_BlockSpan]:
    """Parse marker lines without allowing one damaged block to claim a later end."""
    spans: list[_BlockSpan] = []
    opened: tuple[int, int] | None = None
    offset = 0
    while offset < len(raw):
        nl = raw.find(b"\n", offset)
        end = len(raw) if nl < 0 else nl + 1
        line = raw[offset:end if nl < 0 else nl]
        if line.endswith(b"\r"):
            line = line[:-1]
        begin = _MARK_BEGIN_LINE_RE.fullmatch(line)
        if begin:
            if opened is not None:
                raise BlockStructureError("nested agrep begin marker")
            opened = (offset, int(begin.group(1)))
        elif line == _MARK_END_B:
            if opened is None:
                raise BlockStructureError("agrep end marker has no begin marker")
            spans.append(_BlockSpan(opened[0], end, opened[1]))
            opened = None
        elif _MARK_PREFIX_B in line or _MARK_END_B in line:
            raise BlockStructureError("malformed agrep marker line")
        offset = end
    if opened is not None:
        raise BlockStructureError("agrep begin marker has no end marker")
    return spans


def _splice_blocks(
        raw: bytes, spans: list[_BlockSpan], replacement: bytes | None = None,
) -> bytes:
    pieces: list[bytes] = []
    cursor = 0
    for index, span in enumerate(spans):
        pieces.append(raw[cursor:span.start])
        if index == 0 and replacement is not None:
            pieces.append(replacement)
        cursor = span.end
    pieces.append(raw[cursor:])
    return b"".join(pieces)


def _preferred_eol(raw: bytes) -> bytes:
    nl = raw.find(b"\n")
    return b"\r\n" if nl > 0 and raw[nl - 1:nl] == b"\r" else b"\n"


def _encode_lines(text: str, eol: bytes) -> bytes:
    return text.encode("utf-8").replace(b"\n", eol)


def _has_skill_frontmatter(raw: bytes, eol: bytes) -> bool:
    opening = b"---" + eol
    return raw.startswith(opening) and eol + b"---" + eol in raw[len(opening):]


# the template conjugates third-person ("{name} wakes"), so values must be
# proper nouns; lowercase brands keep their casing. codex routes to its own
# template and is absent on purpose.
_AGENT_NAMES = {
    "claude": "Claude",
    "opencode": "opencode",
    "gemini/antigravity": "Gemini",
    "qwen": "Qwen",
    "crush": "Crush",
    "kimi": "Kimi",
    "windsurf": "Windsurf",
    "cline": "Cline",
    "roo": "Roo",
    "goose": "goose",
    "amp": "Amp",
    "copilot": "Copilot",
    "droid": "Droid",
    "grok": "Grok",
    "continue": "Continue",
    "cursor": "Cursor",
    # lowercase per the owner; both roots register this one label
    "pi": "pi",
}


def _label_for(path: Path | None) -> str | None:
    """The registry label that owns ``path``, from the live target tables.

    Routing must be by agent, not filename: AGENTS.md alone is shared by
    codex, opencode, Kimi, Amp and droid. Stale paths from old state (a
    retired CODEX_HOME target, a renamed agent) miss the tables and fall
    back to the filename, where AGENTS.md really does mean codex - the only
    agent whose extra homes get appended outside the manifest.
    """
    if path is None:
        return None
    value = str(path)
    for agent, _, target in MD_TARGETS + SKILL_TARGETS:
        if str(target) == value:
            return agent
    if path.name.upper() == "CLAUDE.MD":
        return "claude"
    if path.name.upper() == "AGENTS.MD":
        return "codex"
    return None


def _person(path: Path | None) -> str:
    """The third-person subject for the NUDGE template. Model post-training
    corpora speak about the assistant by name ("Claude should..."), so each
    block addresses its agent the way its training data does; an unknown
    target reads as "the agent", which conjugates at every slot."""
    return _AGENT_NAMES.get(_label_for(path), "the agent")


_TAG_BLOCK_RE = re.compile(r"^<([a-zA-Z][\w-]*)>\s*$[\s\S]*?^</\1>\s*$", re.M)


def _tag_styled(host: str) -> bool:
    """Does the host file organize content as XML-ish tag blocks? Tag-structured
    system prompts treat tag sections as first-class; the block should match the
    convention it lands in. Judged on the host WITHOUT our own block."""
    return len(_TAG_BLOCK_RE.findall(host)) >= 2


def _codex_target(path: Path | None) -> bool:
    """Only codex gets the codex prompt file; AGENTS.md alone proves nothing."""
    return _label_for(path) == "codex"


def _block(path: Path | None = None, host: str = "") -> str:
    # NUDGE_CODEX is used raw: it carries no subject slots, and codex's
    # dialect is second person throughout.
    body = (NUDGE_CODEX if _codex_target(path)
            else NUDGE.format(name=_person(path)))
    if _tag_styled(host):
        # <agrep-recall>, not <instructions>: the host likely owns an
        # <instructions> block already, and a duplicate sibling tag collides
        body = f"<agrep-recall>\n{body}\n</agrep-recall>"
    return f"{MARK_BEGIN}\n{body}\n{MARK_END}\n"


def _atomic_write_bytes(
        path: Path, body: bytes, *, mode: int | None = None,
        expect_absent: bool = False,
) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    path.parent.mkdir(parents=True, exist_ok=True)
    destination = path
    if path.is_symlink():
        try:
            destination = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"refusing to replace dangling symlink {path}") from exc
        if not destination.is_file():
            raise ValueError(f"refusing to write non-file symlink target {destination}")
    if mode is None:
        try:
            mode = stat.S_IMODE(destination.stat().st_mode)
        except OSError:
            mode = 0o600
    if expect_absent:
        try:
            created = ownerfile.create_exclusive(
                destination, body, mode=mode, fsync=True, exact_mode=True)
        except FileExistsError as exc:
            raise ownerfile.OwnershipLost(
                f"teach target appeared during reconcile: {path}") from exc
        created.close()
        if os.name != "nt":
            directory = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                dir=destination.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        common.replace_with_retry(tmp, destination)
        if os.name != "nt":
            directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, body: str, *, encoding: str = "utf-8",
                       mode: int | None = None) -> None:
    _atomic_write_bytes(path, body.encode(encoding), mode=mode)


def _updated_block_bytes(path: Path, raw: bytes) -> tuple[str, bytes]:
    raw.decode("utf-8")
    spans = _block_spans(raw)
    if any(span.version > NUDGE_V for span in spans):
        return "kept", raw
    current = [span for span in spans if span.version == NUDGE_V]
    if current:
        redundant = [span for span in spans if span != current[0]]
        return (
            ("updated", _splice_blocks(raw, redundant))
            if redundant else ("kept", raw)
        )
    eol = _preferred_eol(raw)
    host_without_blocks = _splice_blocks(raw, spans).decode("utf-8")
    block = _encode_lines(_block(path, host_without_blocks), eol)
    if spans:
        return "updated", _splice_blocks(raw, spans, block)
    if not raw:
        return "added", block
    separator = b"" if raw.endswith(eol + eol) else (
        eol if raw.endswith(b"\n") else eol + eol)
    return "added", raw + separator + block


def _write_block(
        path: Path, *, observed: bytes | None = None,
        expect_absent: bool = False,
) -> str:
    if _data_dir_readonly():
        raise _readonly_error()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if observed is None:
            old = path.read_bytes() if path.exists() else b""
        else:
            old = observed
        verb, new = _updated_block_bytes(path, old)
    except UnicodeDecodeError:
        # a legacy cp1252/ANSI byte: we cannot faithfully rewrite what we
        # cannot faithfully read - leave the user's file alone
        print(f"  ! {path}: not valid UTF-8 - skipped (fix its encoding and "
              "re-run setup)")
        return "skipped"
    if new != old:
        _atomic_write_bytes(path, new, expect_absent=expect_absent)
    return verb


def _write_skill(
        path: Path, *, observed: bytes | None = None,
        expect_absent: bool = False,
) -> str:
    if _data_dir_readonly():
        raise _readonly_error()
    path.parent.mkdir(parents=True, exist_ok=True)
    if observed is not None or path.exists():
        on_disk = path.read_bytes() if observed is None else observed
        old = on_disk
        old.decode("utf-8")
        spans = _block_spans(old)
        if not spans:
            return "conflict"
        if any(span.version > NUDGE_V for span in spans):
            return "kept"
        eol = _preferred_eol(old)
        front = _encode_lines(_SKILL_FRONT, eol)
        missing_front = not _has_skill_frontmatter(old, eol)
        if any(span.version == NUDGE_V for span in spans):
            if missing_front:
                old = front + old
            _, new = _updated_block_bytes(path, old)
            if new != on_disk:
                _atomic_write_bytes(
                    path, new, expect_absent=expect_absent)
                return "updated"
            return "kept"
        if missing_front:
            old = front + old
        verb, new = _updated_block_bytes(path, old)
        if new != on_disk:
            _atomic_write_bytes(
                path, new, expect_absent=expect_absent)
        return verb
    body = _SKILL_FRONT.encode("utf-8") + _block(path).encode("utf-8")
    _atomic_write_bytes(
        path, body, expect_absent=expect_absent)
    return "added"


def _legacy_block_version(path: Path) -> int | None:
    try:
        versions = [span.version for span in _block_spans(path.read_bytes())]
    except (OSError, UnicodeError, ValueError):
        return None
    if not versions or any(version >= NUDGE_V for version in versions):
        return None
    return max(versions)


def _remove_block(path: Path) -> bool:
    if _data_dir_readonly():
        raise _readonly_error()
    if not path.exists():
        return False
    try:
        old = path.read_bytes()
        old.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8; block left in place") from exc
    spans = _block_spans(old)
    if not spans:
        return False
    new = _splice_blocks(old, spans)
    if not new.strip():
        if path.is_symlink():
            _atomic_write_bytes(path, b"")
        else:
            path.unlink()
    else:
        _atomic_write_bytes(path, new)
    return True


def _remove_skill(path: Path) -> bool:
    if _data_dir_readonly():
        raise _readonly_error()
    if not path.exists():
        return False
    try:
        old = path.read_bytes()
        old.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8; skill left in place") from exc
    spans = _block_spans(old)
    if not spans:
        return False
    new = _splice_blocks(old, spans)
    fronts = {
        _SKILL_FRONT.encode("utf-8"),
        _encode_lines(_SKILL_FRONT, b"\r\n"),
    }
    if (new in fronts or not new.strip()) and not path.is_symlink():
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
    else:
        _atomic_write_bytes(path, new)
    return True


def _load_state() -> dict:
    try:
        raw = ownerfile.snapshot(
            STATE_PATH, max_bytes=_RECONCILE_STATE_MAX_BYTES).raw
        state = json.loads(raw.decode("utf-8"))
    except (OSError, RecursionError, UnicodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _read_reconcile_target(path: Path) -> ownerfile.Snapshot | None:
    try:
        return ownerfile.snapshot(
            path, max_bytes=_RECONCILE_TARGET_MAX_BYTES)
    except FileNotFoundError:
        return None


def _state_paths(
        value: object, *, skills_only: bool = False,
        include_retired: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return []
    targets = SKILL_TARGETS if skills_only else MD_TARGETS + SKILL_TARGETS
    allowed = {str(target) for _, _, target in targets}
    home = Path(os.path.abspath(HOME))
    resolved_home = home.resolve()
    accepted: list[str] = []
    for item in value:
        if not isinstance(item, str) or item in accepted:
            continue
        if item in allowed:
            accepted.append(item)
            continue
        if not include_retired:
            continue
        path = Path(item)
        normalized = Path(os.path.abspath(path))
        if (not path.is_absolute() or path != normalized or path == home
                or not path.is_relative_to(home)):
            continue
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == resolved_home or not resolved.is_relative_to(resolved_home):
            continue
        accepted.append(item)
    return accepted

def enrollment_active() -> bool:
    """True only when a current receipt names at least one enrolled target."""
    state = _load_state()
    return bool(
        state.get("version") == 2
        and _state_paths(state.get("targets"), include_retired=True)
    )


def _save_state(
        targets: list[Path], skills: list[Path] | None = None,
        removing: list[Path] | None = None) -> None:
    global _LAST_RECONCILE_HEALTH, _PENDING_RECONCILE_HEALTH
    if _data_dir_readonly():
        raise _readonly_error()
    body = {"version": 2, "targets": [str(t) for t in targets],
            "skills": [str(t) for t in (skills or [])],
            "removing": [str(t) for t in (removing or [])]}
    _atomic_write_text(STATE_PATH, json.dumps(body, indent=1) + "\n")
    _LAST_RECONCILE_HEALTH = None
    _PENDING_RECONCILE_HEALTH = None
    try:
        _reconcile_health_path().unlink(missing_ok=True)
    except OSError:
        pass


def _unenroll_target(target: Path) -> None:
    if _data_dir_readonly():
        raise _readonly_error()
    state = _load_state()
    targets = [
        Path(value)
        for value in _state_paths(state.get("targets"), include_retired=True)
    ]
    removing = [Path(value) for value in _state_paths(
        state.get("removing"), include_retired=True)]
    if target not in targets and target not in removing:
        return
    skills = [Path(value) for value in _state_paths(
        state.get("skills"), skills_only=True, include_retired=True)]
    remaining = [path for path in targets if path != target]
    remaining_skills = [path for path in skills if path != target]
    remaining_removing = [path for path in removing if path != target]
    if remaining:
        _save_state(remaining, remaining_skills, remaining_removing)
    else:
        STATE_PATH.unlink(missing_ok=True)


def _mark_target_removing(target: Path, is_skill: bool) -> None:
    state = _load_state()
    targets = [Path(value) for value in _state_paths(
        state.get("targets"), include_retired=True)]
    skills = [Path(value) for value in _state_paths(
        state.get("skills"), skills_only=True, include_retired=True)]
    removing = [Path(value) for value in _state_paths(
        state.get("removing"), include_retired=True)]
    if target not in targets:
        targets.append(target)
    if is_skill and target not in skills:
        skills.append(target)
    if target not in removing:
        removing.append(target)
    _save_state(targets, skills, removing)


def _reenroll_target(target: Path, is_skill: bool) -> None:
    """A failed removal keeps its enrollment record: without it a retry finds
    nothing to remove and reports a clean removal with the block still on
    disk."""
    state = _load_state()
    targets = [Path(value) for value in _state_paths(
        state.get("targets"), include_retired=True)]
    skills = [Path(value) for value in _state_paths(
        state.get("skills"), skills_only=True, include_retired=True)]
    removing = [Path(value) for value in _state_paths(
        state.get("removing"), include_retired=True) if Path(value) != target]
    if target not in targets:
        targets.append(target)
    if is_skill and target not in skills:
        skills.append(target)
    _save_state(targets, skills, removing)


# Uninstall sentinel: taught blocks must vanish seconds after agrep is deleted,
# not scheduler-hours - per-platform watch catalog on _sentinel_install's docstring.

TASK_NAME = "agrep-sentinel"
_LINUX_UNARMED_MARKER = "sentinel-linux-unarmed"

# Windows resident children need CREATE_NO_WINDOW or each subprocess flashes a console.
_NO_WINDOW = ({"creationflags": subprocess.CREATE_NO_WINDOW}
              if os.name == "nt" else {})

_SENTINEL_WATCH_PY = r'''
import ctypes, json, os, re, subprocess, sys, tempfile, time
from ctypes import wintypes
from pathlib import Path

DIR = Path(__file__).resolve().parent
CFG = DIR / "sentinel.json"

k32 = ctypes.windll.kernel32
# singleton: a second copy (logon task + install-time spawn) exits immediately
k32.CreateMutexW(None, False, "Local\\agrep-sentinel")
if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

FILE_NOTIFY = 0x1 | 0x2  # FILE_NAME | DIR_NAME changes
INVALID = wintypes.HANDLE(-1).value


def _watch(path):
    h = k32.FindFirstChangeNotificationW(str(path), False, FILE_NOTIFY)
    return None if h == INVALID else h


def _atomic(path, body):
    if path.is_symlink():
        path = path.resolve(strict=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def strip(cfg):
    prefix = cfg["mark_prefix"].encode("ascii")
    end_marker = cfg["mark_end"].encode("ascii")
    begin = re.compile(re.escape(prefix) + rb" v([0-9]+) -->\Z")
    skills = set(cfg.get("skill_files", []))
    for t in cfg.get("targets", []):
        p = Path(t)
        try:
            if not p.exists():
                continue
            old = p.read_bytes()
            old.decode("utf-8")
            spans = []
            opened = None
            bad = False
            offset = 0
            while offset < len(old):
                nl = old.find(b"\n", offset)
                line_end = len(old) if nl < 0 else nl + 1
                line = old[offset:line_end if nl < 0 else nl]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if begin.fullmatch(line):
                    if opened is not None:
                        bad = True
                        break
                    opened = offset
                elif line == end_marker:
                    if opened is None:
                        bad = True
                        break
                    spans.append((opened, line_end))
                    opened = None
                elif prefix in line or end_marker in line:
                    bad = True
                    break
                offset = line_end
            if bad or opened is not None or not spans:
                continue
            pieces = []
            cursor = 0
            for start, finish in spans:
                pieces.append(old[cursor:start])
                cursor = finish
            pieces.append(old[cursor:])
            new = b"".join(pieces)
            front = cfg.get("skill_front", "").encode("utf-8")
            fronts = {front, front.replace(b"\n", b"\r\n")}
            if t in skills and new in fronts and not p.is_symlink():
                p.unlink()
                try:
                    p.parent.rmdir()
                except OSError:
                    pass
                continue
            if new.strip() or p.is_symlink():
                _atomic(p, new)
            else:
                p.unlink()
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError: one cp1252 byte in one
            # target must not abort the strip and skip the teardown below
            continue
    # codex's hook command names a script inside the package that just went
    # away, so a stranded one errors on every compaction. Exact match against
    # the install-time snapshot; any edit since then makes the file the user's.
    snapshot = cfg.get("codex_hooks_snapshot") or ""
    for t in (cfg.get("codex_hooks") or []) if snapshot else ():
        p = Path(t)
        try:
            if not p.is_symlink() and p.is_file() \
                    and p.read_text(encoding="utf-8") == snapshot:
                p.unlink()
        except (OSError, ValueError):
            continue
    # Pi and OMP load agrep's exact extension bytes on startup. A changed file
    # is user-owned and survives, just like a changed Codex hook registration.
    snapshot = cfg.get("pi_extension_snapshot") or ""
    for t in (cfg.get("pi_extensions") or []) if snapshot else ():
        p = Path(t)
        try:
            if not p.is_symlink() and p.is_file() \
                    and p.read_text(encoding="utf-8") == snapshot:
                p.unlink()
        except (OSError, ValueError):
            continue
    subprocess.run(["schtasks", "/Delete", "/TN", cfg["task_name"], "/F"],
                   capture_output=True, creationflags=0x08000000)
    for n in ("sentinel.json", "teach.json", "teach-reconcile.json"):
        try:
            (DIR / n).unlink()
        except OSError:
            pass
    try:
        Path(__file__).unlink()
    except OSError:
        pass


def main():
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    cli = Path(cfg["cli"])
    handles = [h for h in (_watch(cli.parent), _watch(DIR)) if h]
    n = len(handles)
    arr = (wintypes.HANDLE * n)(*handles)
    while True:
        if n:
            # 1h timeout as insurance against a silently dead handle; the wait
            # itself costs nothing - the process is off the scheduler until then
            k32.WaitForMultipleObjects(n, arr, False, 3600 * 1000)
        else:
            time.sleep(3600)
        if not CFG.exists():
            return  # agrep remove: deliberate, everything else already handled
        if not cli.exists():
            time.sleep(20)  # a git checkout / restore can blip the file
            if not CFG.exists():
                return
            if not cli.exists():
                strip(cfg)
                return
        for h in handles:
            k32.FindNextChangeNotification(h)


main()
'''.lstrip()


LAUNCHD_LABEL = "com.agrep.sentinel"

# mac/linux twins share this body; values are templated at install time (sh can't
# parse JSON) and only the scheduler-teardown tail differs. Two-miss contract.
_SENTINEL_STRIP_PERL = r"""use strict;
use warnings;
use File::Basename qw(basename dirname);
use File::Temp qw(tempfile);

my ($path, $prefix, $ending) = @ARGV;
open my $in, "<:raw", $path or exit 2;
local $/;
my $old = <$in>;
close $in;

my (@spans, $opened);
my $offset = 0;
while ($offset < length($old)) {
    my $nl = index($old, "\n", $offset);
    my $finish = $nl < 0 ? length($old) : $nl + 1;
    my $line = substr($old, $offset, ($nl < 0 ? $finish : $nl) - $offset);
    $line =~ s/\r\z//;
    if ($line =~ /^\Q$prefix\E v[0-9]+ -->\z/) {
        exit 3 if defined $opened;
        $opened = $offset;
    } elsif ($line eq $ending) {
        exit 3 unless defined $opened;
        push @spans, [$opened, $finish];
        undef $opened;
    } elsif (index($line, $prefix) >= 0 || index($line, $ending) >= 0) {
        exit 3;
    }
    $offset = $finish;
}
exit 3 if defined $opened;
exit 0 unless @spans;

my ($new, $cursor) = ("", 0);
for my $span (@spans) {
    $new .= substr($old, $cursor, $span->[0] - $cursor);
    $cursor = $span->[1];
}
$new .= substr($old, $cursor);

my @stat = stat($path);
my ($out, $tmp) = tempfile("." . basename($path) . ".XXXXXX",
                           DIR => dirname($path), UNLINK => 0);
binmode $out;
print {$out} $new or do { unlink $tmp; exit 2 };
close $out or do { unlink $tmp; exit 2 };
chmod($stat[2] & 07777, $tmp) if @stat;
rename($tmp, $path) or do { unlink $tmp; exit 2 };
"""

_SENTINEL_SH = r"""#!/bin/sh
DIR=@@DIR@@
if [ -e @@CLI@@ ]; then exit 0; fi
sleep 20  # a git checkout / restore can blip the file
if [ -e @@CLI@@ ]; then exit 0; fi

# still gone: agrep was uninstalled - clean everything it taught.
for t in @@TARGETS@@; do
    p=$t
    linked=0
    if [ -L "$p" ]; then
        linked=1
        p=$(perl -MCwd=abs_path -e 'print abs_path(shift)' "$p" 2>/dev/null) || continue
    fi
    [ -f "$p" ] || continue
    if perl "$DIR/sentinel_strip.pl" "$p" '@@PREFIX@@' '@@END@@' 2>/dev/null; then
        if ! grep -q '[^[:space:]]' "$p" 2>/dev/null && [ "$linked" -eq 0 ]; then rm -f "$p"; fi
    fi
done
for f in @@SKILLFILES@@; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    if cmp -s "$f" "$DIR/sentinel_skill_front" || perl -0777 -e '
        open my $a, "<:raw", $ARGV[0] or exit 1;
        open my $b, "<:raw", $ARGV[1] or exit 1;
        my ($left, $right) = (<$a>, <$b>);
        $left =~ s/\r\n/\n/g;
        exit($left eq $right ? 0 : 1);
    ' "$f" "$DIR/sentinel_skill_front"; then
        rm -f "$f"
        rmdir "$(dirname "$f")" 2>/dev/null || true
    fi
done
# codex's hook runs a script inside the package that just vanished, so a
# stranded one errors on every compaction. Byte-exact against the snapshot
# taken when it was installed: any edit since then makes it the user's.
for f in @@CODEXHOOKS@@; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    if cmp -s "$f" "$DIR/sentinel_codex_hooks"; then rm -f "$f"; fi
done
# Pi and OMP share one byte-identical extension payload. Preserve any copy
# edited since enrollment; only the install-time snapshot is ours to remove.
for f in @@PIEXTENSIONS@@; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    if cmp -s "$f" "$DIR/sentinel_pi_extension"; then rm -f "$f"; fi
done
"""

_SENTINEL_TAIL_MAC = r"""launchctl bootout gui/$(id -u)/@@LABEL@@ 2>/dev/null || launchctl unload @@PLIST@@ 2>/dev/null
rm -f @@PLIST@@ "$DIR/teach.json" "$DIR/teach-reconcile.json" "$DIR/sentinel_skill_front" "$DIR/sentinel_codex_hooks" "$DIR/sentinel_pi_extension" "$DIR/sentinel_strip.pl" "$0"
"""

_SENTINEL_TAIL_LINUX = r"""systemctl --user disable --now @@UNIT@@.timer @@UNIT@@.path 2>/dev/null
rm -f @@UNITS@@ "$DIR/teach.json" "$DIR/teach-reconcile.json" "$DIR/sentinel_skill_front" "$DIR/sentinel_codex_hooks" "$DIR/sentinel_pi_extension" "$DIR/sentinel_strip.pl" "$0"
systemctl --user daemon-reload 2>/dev/null
"""

# WatchPaths = launchd holds the file watch and fires the script the moment the
# install dir changes (deletion included) - event-driven, no process of ours.
# The hourly StartInterval is insurance for a watch lost across logout races.
_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>{script}</string></array>
  <key>WatchPaths</key><array>{watch}</array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><false/>
</dict></plist>
"""


def _sentinel_watch_paths() -> list[Path]:
    """The install dir, plus a stable ancestor when the install lives inside a
    tool venv (uv/pipx reinstalls delete the whole venv; a watch on the deleted
    tree can dangle and take the launchd job with it - the surviving parent
    still fires on the venv's own delete/recreate)."""
    paths = [REPO]
    prefix = Path(sys.prefix)
    try:
        if REPO.is_relative_to(prefix) and prefix.parent != prefix:
            paths.append(prefix.parent)
    except (OSError, ValueError):
        pass
    return paths

_SYSTEMD_SERVICE = """\
[Unit]
Description=agrep uninstall sentinel

[Service]
Type=oneshot
ExecStart=/bin/sh "{script}"
"""

# The .path unit is the mechanism: the user manager holds an inotify watch on the
# CLI and fires the service the moment it changes or vanishes. Event-driven,
# no process of ours.
_SYSTEMD_PATH = """\
[Unit]
Description=agrep uninstall sentinel (file watch)

[Path]
PathModified={cli}

[Install]
WantedBy=paths.target
"""

# OnStartupSec is relative to the user manager (login), not boot - and a timer with
# only OnUnitActiveSec never fires the first time, so both lines are load-bearing.
# Hourly: insurance for a watch lost across relogin, not the mechanism.
_SYSTEMD_TIMER = """\
[Unit]
Description=agrep uninstall sentinel

[Timer]
OnStartupSec=1h
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
"""


def _sh_squote(s: str) -> str:
    """Single-quote a string for POSIX sh: wrap in '...', escape embedded quotes as '\\''."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_unit_dir() -> Path:
    return HOME / ".config" / "systemd" / "user"


def _systemctl_user(
        *args: str, timeout_s: float | None = None,
        observation: dict | None = None,
) -> int:
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_s)
        if observation is not None:
            observation.update(
                state="complete",
                detail=f"systemctl returned {r.returncode}",
                stdout=r.stdout.strip(), stderr=r.stderr.strip())
        return r.returncode
    except subprocess.TimeoutExpired:
        if observation is not None:
            observation.update(
                state="budget-exceeded",
                detail="systemd sentinel verification exceeded its budget")
        return 1
    except OSError as exc:  # non-systemd or unavailable user manager
        if observation is not None:
            observation.update(
                state="unavailable",
                detail=(
                    "systemd sentinel verification is unavailable "
                    f"({type(exc).__name__}: {exc})"))
        return 1


def _sentinel_skill_paths(targets: list[Path]) -> list[Path]:
    current_skills = {str(target) for _, _, target in SKILL_TARGETS}
    current_markdown = {str(target) for _, _, target in MD_TARGETS}
    state = _load_state()
    persisted = set(_state_paths(
        state.get("skills"), skills_only=True, include_retired=True))
    known_skills = current_skills | (persisted - current_markdown)
    return [target for target in targets if str(target) in known_skills]


def _owned_codex_hooks() -> list[Path]:
    """The codex hooks.json files agrep installed and still owns, judged now,
    while agrep is here to judge. The sentinel runs after the package is gone
    and cannot import this verdict, so it is frozen to disk as a snapshot."""
    try:
        import hookinstall
    except ImportError:
        return []
    owned = []
    for path in hookinstall.codex_hooks_paths():
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        if not path.is_symlink() and hookinstall._codex_hooks_owned(raw):
            owned.append(path)
    return owned


def _write_codex_hook_snapshot(directory: Path) -> None:
    """Freeze the exact bytes of the owned codex hooks.json, or clear a stale
    snapshot when there is none. One file is all codex has; a second target
    would need its own snapshot rather than sharing this one."""
    owned = _owned_codex_hooks()
    snapshot = directory / "sentinel_codex_hooks"
    if not owned:
        snapshot.unlink(missing_ok=True)
        return
    _atomic_write_bytes(snapshot, owned[0].read_bytes())


def _owned_pi_extensions() -> list[Path]:
    """Extension files whose bytes still belong to agrep at snapshot time."""
    try:
        import hookinstall
        shipped = hookinstall.PI_EXTENSION.read_bytes()
    except (ImportError, OSError):
        return []
    owned = []
    for _, path in hookinstall.pi_extension_paths():
        try:
            existing = path.read_bytes()
        except OSError:
            continue
        if (not path.is_symlink()
                and hookinstall._pi_extension_owned(existing, shipped)):
            owned.append(path)
    return owned


def _write_pi_extension_snapshot(directory: Path) -> None:
    owned = _owned_pi_extensions()
    snapshot = directory / "sentinel_pi_extension"
    if not owned:
        snapshot.unlink(missing_ok=True)
        return
    _atomic_write_bytes(snapshot, owned[0].read_bytes())


def _sh_subs(targets: list[Path]) -> dict[str, str]:
    """The template values the mac and linux sentinel scripts share."""
    skills = _sentinel_skill_paths(targets)
    return {
        "@@DIR@@": _sh_squote(common.DATA_DIR),
        "@@CLI@@": _sh_squote(REPO / "cli.py"),
        "@@TARGETS@@": " ".join(_sh_squote(t) for t in targets),
        "@@SKILLFILES@@": " ".join(_sh_squote(t) for t in skills),
        "@@CODEXHOOKS@@": " ".join(
            _sh_squote(t) for t in _owned_codex_hooks()),
        "@@PIEXTENSIONS@@": " ".join(
            _sh_squote(t) for t in _owned_pi_extensions()),
        "@@PREFIX@@": MARK_PREFIX,
        "@@END@@": MARK_END,
    }


def _write_sentinel_sh(tail: str, subs: dict[str, str]) -> Path:
    if _data_dir_readonly():
        raise _readonly_error()
    d = common.DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    script = d / "sentinel.sh"
    body = _SENTINEL_SH + tail
    for k, v in subs.items():
        body = body.replace(k, v)
    _atomic_write_text(d / "sentinel_skill_front", _SKILL_FRONT)
    _write_codex_hook_snapshot(d)
    _write_pi_extension_snapshot(d)
    _atomic_write_text(d / "sentinel_strip.pl", _SENTINEL_STRIP_PERL, mode=0o700)
    _atomic_write_text(script, body, mode=0o700)
    return script


def _sentinel_install(targets: list[Path]) -> bool:
    """Arm package-loss cleanup for enrolled instructions and owned hooks.

    The platform watcher rechecks a missing CLI after 20 seconds, then strips
    every enrolled instruction block and removes only hook/extension bytes
    frozen during this install. User edits survive. Timers are backstops for
    watch-manager loss; the filesystem watch is the mechanism.
    """
    if _data_dir_readonly():
        return False
    if sys.platform == "win32":
        return _sentinel_install_win(targets)
    if sys.platform == "darwin":
        return _sentinel_install_mac(targets)
    if sys.platform.startswith("linux"):
        return _sentinel_install_linux(targets)
    return False


def refresh_sentinel() -> bool:
    """Refresh hook snapshots after the separately consented hook phase."""
    state = _load_state()
    targets = [
        Path(value) for value in _state_paths(
            state.get("targets"), include_retired=True)
    ]
    try:
        import hookinstall
        has_hooks = hookinstall.hooks_enrolled()
    except ImportError:
        has_hooks = False
    if not targets and not has_hooks:
        return True
    return _sentinel_install(targets)


def sentinel_status(*, timeout_s: float | None = None) -> dict:
    """Bounded, structured evidence for the uninstall sentinel."""
    deadline = (
        None if timeout_s is None
        else time.monotonic() + max(0.0, timeout_s)
    )

    def remaining() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def missing(detail: str) -> dict:
        return {"state": "not-armed", "armed": False, "detail": detail}

    def run_status(command: list[str], label: str) -> dict:
        left = remaining()
        if left == 0.0:
            return {
                "state": "budget-exceeded", "armed": None,
                "detail": f"{label} verification exceeded its budget",
            }
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=left, **_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {
                "state": "budget-exceeded", "armed": None,
                "detail": f"{label} verification exceeded its budget",
            }
        except OSError as exc:
            return {
                "state": "unavailable", "armed": None,
                "detail": (
                    f"{label} verification is unavailable "
                    f"({type(exc).__name__}: {exc})"),
            }
        if result.returncode == 0:
            return {
                "state": "armed", "armed": True,
                "detail": f"{label} is registered and active",
            }
        return missing(f"{label} is not registered and active")

    if sys.platform == "win32":
        if not all((common.DATA_DIR / name).is_file()
                   for name in ("sentinel.json", "sentinel_watch.py")):
            return missing("Windows sentinel artifacts are missing")
        return run_status(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            "Windows uninstall sentinel")
    if sys.platform == "darwin":
        if (not _plist_path().is_file()
                or not (common.DATA_DIR / "sentinel.sh").is_file()
                or not (common.DATA_DIR / "sentinel_strip.pl").is_file()):
            return missing("macOS sentinel artifacts are missing")
        return run_status(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            "macOS uninstall sentinel")
    if sys.platform.startswith("linux"):
        if not all((common.DATA_DIR / name).is_file()
                   for name in ("sentinel.sh", "sentinel_strip.pl")):
            return missing("Linux sentinel artifacts are missing")

        units = (f"{TASK_NAME}.path", f"{TASK_NAME}.timer")
        for unit in units:
            for action in ("is-enabled", "is-active"):
                left = remaining()
                if left == 0.0:
                    return {
                        "state": "budget-exceeded", "armed": None,
                        "detail": (
                            "systemd sentinel verification exceeded its budget"),
                    }
                observed: dict = {}
                rc = _systemctl_user(
                    action, "--quiet", unit, timeout_s=left,
                    observation=observed)
                if observed.get("state") == "budget-exceeded":
                    return {
                        "state": "budget-exceeded", "armed": None,
                        "detail": str(observed.get("detail")),
                    }
                if observed.get("state") == "unavailable":
                    return {
                        "state": "unavailable", "armed": None,
                        "detail": str(observed.get("detail")),
                    }
                if rc != 0:
                    return missing(
                        f"systemd unit {unit} is not both enabled and active")
        return {
            "state": "armed", "armed": True,
            "detail": "systemd uninstall sentinel is registered and active",
        }
    return missing("this platform has no uninstall sentinel implementation")


def sentinel_armed(*, timeout_s: float | None = None) -> bool:
    """Compatibility boolean for setup/remove behavior."""
    return sentinel_status(timeout_s=timeout_s).get("armed") is True


def _pythonw() -> str:
    """The interpreter the sentinel task may reference for years.

    Under `uv tool run` (the npm shim) sys.executable is a venv shim inside
    uv's GC-able cache - `uv cache clean` leaves the logon task dangling.
    The venv's base interpreter is uv's managed install, which cache cleans
    never touch, so the task points there. The watcher is stdlib-only, so
    losing the venv's site-packages costs nothing."""
    exe = Path(getattr(sys, "_base_executable", "") or sys.executable)
    if not exe.exists():
        exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def _sentinel_install_win(targets: list[Path]) -> bool:
    if _data_dir_readonly():
        return False
    d = common.DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    watcher = d / "sentinel_watch.py"
    _atomic_write_text(watcher, _SENTINEL_WATCH_PY, encoding="ascii")
    skills = _sentinel_skill_paths(targets)
    # Snapshots ride in sentinel.json on Windows because the watcher already
    # reads that config. Exact text survives the JSON round-trip.
    codex_hooks = _owned_codex_hooks()
    codex_snapshot = (codex_hooks[0].read_text(encoding="utf-8")
                      if codex_hooks else "")
    codex_hooks = codex_hooks[:1]
    pi_extensions = _owned_pi_extensions()
    pi_snapshot = (pi_extensions[0].read_text(encoding="utf-8")
                   if pi_extensions else "")
    _atomic_write_text(d / "sentinel.json", json.dumps({
        "cli": str(REPO / "cli.py"),
        "task_name": TASK_NAME,
        "mark_prefix": MARK_PREFIX,
        "mark_end": MARK_END,
        "targets": [str(t) for t in targets],
        "skill_files": [str(t) for t in skills],
        "skill_front": _SKILL_FRONT,
        "codex_hooks": [str(t) for t in codex_hooks],
        "codex_hooks_snapshot": codex_snapshot,
        "pi_extensions": [str(t) for t in pi_extensions],
        "pi_extension_snapshot": pi_snapshot,
    }, indent=1) + "\n")
    # schtasks /SC ONLOGON demands elevation (denied under npm postinstall);
    # the Task Scheduler COM surface allows an own-user logon trigger
    # unelevated, so Register-ScheduledTask leads and schtasks is the fallback.
    def _ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    register = (
        "Register-ScheduledTask -Force -TaskName " + _ps_quote(TASK_NAME)
        + " -Action (New-ScheduledTaskAction -Execute "
        + _ps_quote(_pythonw())
        + " -Argument " + _ps_quote(f'"{watcher}"')
        + ") -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "
        + _ps_quote(os.environ.get("USERNAME") or "") + ")"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", register],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_NO_WINDOW)
    if r.returncode != 0:
        # logon task revives the waiter after reboots; the spawn below covers right now
        r = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON",
             "/TR", f'"{_pythonw()}" "{watcher}"'],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **_NO_WINDOW)
    launched = False
    try:
        subprocess.Popen([_pythonw(), str(watcher)],
                         creationflags=common.windows_background_child_flags(
                             0x08000008),
                         close_fds=True, cwd=str(d))
        launched = True
    except OSError:
        pass
    return r.returncode == 0 and launched and sentinel_armed()


def _sentinel_install_mac(targets: list[Path]) -> bool:
    if _data_dir_readonly():
        return False
    plist = _plist_path()
    subs = _sh_subs(targets) | {
        "@@LABEL@@": LAUNCHD_LABEL,
        "@@PLIST@@": _sh_squote(plist),
    }
    script = _write_sentinel_sh(_SENTINEL_TAIL_MAC, subs)
    plist.parent.mkdir(parents=True, exist_ok=True)
    from xml.sax.saxutils import escape
    watch = "".join(f"<string>{escape(str(p))}</string>"
                    for p in _sentinel_watch_paths())
    body = _LAUNCHD_PLIST.format(label=escape(LAUNCHD_LABEL),
                                 script=escape(str(script)), watch=watch)
    _atomic_write_text(plist, body)
    # bootstrap works from contexts load -w does not (daemon-spawned shells);
    # bootout only after the new body is on disk, so a failed load never
    # leaves less armed than before this ran
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        r = subprocess.run(["launchctl", "load", "-w", str(plist)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    return r.returncode == 0 and sentinel_armed()


def _user_manager_unavailable() -> bool:
    """Positive probe: no systemd user manager is reachable at all.

    Deliberately wording-independent - the no-bus refusal text was reworded
    across systemd generations (255 vs 259) and containers with systemctl
    answer differently again, and each variant broke a substring match in
    turn. `is-system-running` is the stable discriminator: a reachable
    manager prints its state word (running, degraded, starting, stopping);
    no reachable manager prints nothing (bus-less shell) or "offline"
    (container), and a missing/hung systemctl cannot answer at all."""
    observed: dict = {}
    _systemctl_user("is-system-running", timeout_s=10.0, observation=observed)
    if observed.get("state") != "complete":
        return True
    return (observed.get("stdout") or "").strip().lower() in ("", "offline")


def _sentinel_install_linux(targets: list[Path]) -> bool:
    if _data_dir_readonly():
        return False
    marker = common.DATA_DIR / _LINUX_UNARMED_MARKER
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        return False
    unit_dir = _systemd_unit_dir()
    service = unit_dir / f"{TASK_NAME}.service"
    timer = unit_dir / f"{TASK_NAME}.timer"
    path_unit = unit_dir / f"{TASK_NAME}.path"
    links = (
        (unit_dir / "paths.target.wants" / path_unit.name, path_unit),
        (unit_dir / "timers.target.wants" / timer.name, timer),
    )
    preexisting = any(
        path.exists() or path.is_symlink()
        for path in (service, timer, path_unit, *(link for link, _ in links))
    )
    subs = _sh_subs(targets) | {
        "@@UNIT@@": TASK_NAME,
        "@@UNITS@@": " ".join(_sh_squote(u) for u in (service, timer, path_unit)),
    }
    script = _write_sentinel_sh(_SENTINEL_TAIL_LINUX, subs)
    service.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(service, _SYSTEMD_SERVICE.format(script=script))
    _atomic_write_text(timer, _SYSTEMD_TIMER)
    _atomic_write_text(path_unit, _SYSTEMD_PATH.format(cli=REPO / "cli.py"))
    _systemctl_user("daemon-reload")
    ok_path = _systemctl_user("enable", "--now", path_unit.name) == 0
    ok_timer = _systemctl_user("enable", "--now", timer.name) == 0
    if ok_path and ok_timer and sentinel_armed():
        return True
    _systemctl_user("disable", "--now", timer.name, path_unit.name)
    manager_unavailable = _user_manager_unavailable()
    proven_unarmed = (
        not preexisting and not ok_path and not ok_timer and manager_unavailable)
    if proven_unarmed:
        for link, target in links:
            try:
                if link.is_symlink():
                    if link.resolve(strict=False) != target.resolve(strict=False):
                        proven_unarmed = False
                        continue
                    link.unlink()
                    try:
                        link.parent.rmdir()
                    except OSError:
                        pass
                elif link.exists():
                    proven_unarmed = False
            except OSError:
                proven_unarmed = False
    for unit in (service, timer, path_unit):
        unit.unlink(missing_ok=True)
    if proven_unarmed:
        for name in (
                "sentinel.sh", "sentinel_skill_front", "sentinel_codex_hooks",
                "sentinel_pi_extension", "sentinel_strip.pl"):
            try:
                (common.DATA_DIR / name).unlink(missing_ok=True)
            except OSError:
                proven_unarmed = False
        if proven_unarmed:
            try:
                _atomic_write_bytes(
                    marker, b"not-armed\n", expect_absent=True)
            except (OSError, ValueError):
                pass
    return False


def _sentinel_remove() -> bool:
    global _LAST_RECONCILE_HEALTH, _PENDING_RECONCILE_HEALTH
    if _data_dir_readonly():
        return False
    try:
        if sys.platform == "win32":
            removed = subprocess.run(
                ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", **_NO_WINDOW)
            verified = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", **_NO_WINDOW)
            absent = verified.returncode != 0 and (
                removed.returncode == 0 or any(
                    marker in (verified.stderr + verified.stdout).lower()
                    for marker in ("cannot find", "not found", "does not exist")))
        elif sys.platform == "darwin":
            plist = _plist_path()
            domain = f"gui/{os.getuid()}"
            bootout = subprocess.run(
                ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            unloaded = subprocess.run(
                ["launchctl", "unload", str(plist)], capture_output=True,
                text=True, encoding="utf-8", errors="replace")
            verified = subprocess.run(
                ["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            missing = (verified.stderr + verified.stdout).lower()
            absent = verified.returncode != 0 and (
                bootout.returncode == 0 or unloaded.returncode == 0
                or any(marker in missing for marker in (
                    "could not find service", "not found", "no such process")))
        elif sys.platform.startswith("linux"):
            unit_dir = _systemd_unit_dir()
            units = tuple(
                unit_dir / f"{TASK_NAME}{suffix}"
                for suffix in (".service", ".timer", ".path")
            )
            links = (
                unit_dir / "paths.target.wants" / f"{TASK_NAME}.path",
                unit_dir / "timers.target.wants" / f"{TASK_NAME}.timer",
            )
            marker = common.DATA_DIR / _LINUX_UNARMED_MARKER
            try:
                proven_unarmed = (
                    not marker.is_symlink()
                    and marker.read_bytes() == b"not-armed\n"
                    and not any(
                        path.exists() or path.is_symlink()
                        for path in (*units, *links)
                    )
                )
            except OSError:
                proven_unarmed = False
            if proven_unarmed:
                absent = True
            else:
                disabled: dict = {}
                _systemctl_user(
                    "disable", "--now", f"{TASK_NAME}.timer", f"{TASK_NAME}.path",
                    observation=disabled)
                absent = disabled.get("state") == "complete"
                expected = {
                    "is-enabled": {
                        "disabled", "static", "indirect", "masked", "not-found"},
                    "is-active": {"inactive", "failed", "unknown"},
                }
                for unit in (f"{TASK_NAME}.timer", f"{TASK_NAME}.path"):
                    for action, states in expected.items():
                        observed: dict = {}
                        rc = _systemctl_user(action, unit, observation=observed)
                        absent = absent and (
                            rc != 0 and observed.get("state") == "complete"
                            and observed.get("stdout") in states)
        else:
            absent = True
    except OSError:
        return False
    if not absent:
        return False
    try:
        if sys.platform == "darwin":
            _plist_path().unlink(missing_ok=True)
        elif sys.platform.startswith("linux"):
            for suffix in (".service", ".timer", ".path"):
                (_systemd_unit_dir() / f"{TASK_NAME}{suffix}").unlink(missing_ok=True)
            _systemctl_user("daemon-reload")
        for n in ("sentinel.ps1", "sentinel.sh", "sentinel.json", "sentinel.miss",
                  "sentinel_watch.py", "sentinel_skill_front",
                  "sentinel_codex_hooks", "sentinel_pi_extension",
                  "sentinel_strip.pl", _LINUX_UNARMED_MARKER,
                  RECONCILE_HEALTH):
            (common.DATA_DIR / n).unlink(missing_ok=True)
    except OSError:
        return False
    _LAST_RECONCILE_HEALTH = None
    _PENDING_RECONCILE_HEALTH = None
    return True


def reconcile() -> list[str]:
    """Check enrolled targets and recreate only missing files.

    Existing drift is reported for foreground setup because no portable file
    replacement can atomically exclude an uncooperative editor."""
    global _LAST_RECONCILE_HEALTH, _PENDING_RECONCILE_HEALTH
    if _data_dir_readonly():
        return []
    repaired: list[str] = []
    refusals: list[dict[str, str]] = []
    preserved_newer: list[dict[str, str]] = []
    try:
        state = _load_state()
        targets = _state_paths(state.get("targets"))
        all_targets = _state_paths(
            state.get("targets"), include_retired=True)
    except Exception:  # noqa: BLE001 -- no state = nothing enrolled
        return repaired
    removing = set(_state_paths(
        state.get("removing"), include_retired=True))
    if not targets and not any(t in removing for t in all_targets):
        _PENDING_RECONCILE_HEALTH = None
        _LAST_RECONCILE_HEALTH = {
            "version": 1, "state": "unenrolled", "repaired": [],
            "refusals": [], "preserved_newer": [],
        }
        try:
            _reconcile_health_path().unlink(missing_ok=True)
        except OSError:
            pass
        return repaired
    skill_paths = set(_state_paths(
        state.get("skills"), skills_only=True, include_retired=True))
    for t in [value for value in all_targets if value in removing]:
        p = Path(t)
        try:
            remove = _remove_skill if t in skill_paths else _remove_block
            remove(p)
            _unenroll_target(p)
            removing.discard(t)
        except Exception as exc:  # noqa: BLE001 -- keep the removal recoverable
            refusals.append(_reconcile_issue(
                p, "removal-pending", f"{type(exc).__name__}: {exc}"))
    state = _load_state()
    targets = _state_paths(state.get("targets"))
    all_targets = _state_paths(state.get("targets"), include_retired=True)
    removing = set(_state_paths(
        state.get("removing"), include_retired=True))
    if not all_targets:
        _PENDING_RECONCILE_HEALTH = None
        _LAST_RECONCILE_HEALTH = {
            "version": 1, "state": "unenrolled", "repaired": [],
            "refusals": [], "preserved_newer": [],
        }
        try:
            _reconcile_health_path().unlink(missing_ok=True)
        except OSError:
            pass
        return repaired
    pending = _PENDING_RECONCILE_HEALTH
    pending_replayed = False
    pending_failed = False
    if pending is not None:
        try:
            _write_reconcile_health(pending)
            _PENDING_RECONCILE_HEALTH = None
            pending_replayed = True
        except Exception:  # noqa: BLE001 -- target repairs must still run
            pending_failed = True
    skill_paths = set(_state_paths(state.get("skills"), skills_only=True))
    known_skills = {str(target) for _, _, target in SKILL_TARGETS}
    skill_paths.update(str(t) for t in targets if str(t) in known_skills)
    for t in targets:
        if t in removing:
            continue
        p = Path(t)
        try:
            snapshot = _read_reconcile_target(p)
            exists = snapshot is not None
            cur = snapshot.raw if snapshot is not None else b""
            cur.decode("utf-8")
            spans = _block_spans(cur)
            is_skill = t in skill_paths
            newer = [span.version for span in spans if span.version > NUDGE_V]
            if newer:
                preserved_newer.append(_reconcile_issue(
                    p, "preserved-newer",
                    f"newer agrep block v{max(newer)} preserved"))
                continue
            current_count = sum(span.version == NUDGE_V for span in spans)
            has_legacy = any(span.version < NUDGE_V for span in spans)
            if current_count == 1 and not has_legacy and (
                    not is_skill
                    or _has_skill_frontmatter(cur, _preferred_eol(cur))):
                continue
            if exists:
                kind = "unowned-skill" if is_skill and not spans else "drifted"
                legacy = max(
                    (span.version for span in spans
                     if span.version < NUDGE_V), default=None)
                detail = (
                    "existing skill has no agrep marker block"
                    if kind == "unowned-skill" else
                    (f"installed agrep block v{legacy}; this build teaches "
                     f"v{NUDGE_V}; run agrep setup"
                     if current_count == 0 and legacy is not None else
                     "target drifted; run agrep setup to reconcile it safely"))
                refusals.append(_reconcile_issue(p, kind, detail))
                continue
            if is_skill:
                verb = _write_skill(
                    p, expect_absent=True)
            else:
                verb = _write_block(
                    p, expect_absent=True)
            if verb == "conflict":
                refusals.append(_reconcile_issue(
                    p, "unowned-skill",
                    "existing skill has no agrep marker block"))
                continue
            if verb == "skipped":
                refusals.append(_reconcile_issue(
                    p, "invalid-utf8", "target is not valid UTF-8"))
                continue
            if verb not in ("kept", "conflict"):
                repaired.append(t)
        except ownerfile.OwnershipLost:
            refusals.append(_reconcile_issue(
                p, "concurrent-edit",
                "target changed during reconcile; current bytes preserved"))
        except BlockStructureError as exc:
            refusals.append(_reconcile_issue(p, "malformed-markers", exc))
        except UnicodeError:
            refusals.append(_reconcile_issue(
                p, "invalid-utf8", "target is not valid UTF-8"))
        except Exception as exc:  # noqa: BLE001 -- one bad target must not stop the rest
            refusals.append(_reconcile_issue(
                p, "target-unreadable", f"{type(exc).__name__}: {exc}"))
            continue
    health = {
        "version": 1,
        "state": ("refused" if refusals else
                  "preserved-newer" if preserved_newer else
                  "repaired" if repaired else "clean"),
        "repaired": [_bounded_safe(path) for path in repaired],
        "refusals": refusals,
        "preserved_newer": preserved_newer,
    }
    if pending_failed and pending is not None:
        health = _carry_health_unavailable(pending, health)
        _PENDING_RECONCILE_HEALTH = health
        _LAST_RECONCILE_HEALTH = health
        return repaired
    if pending_replayed and pending is not None and health["state"] == "clean":
        _LAST_RECONCILE_HEALTH = pending
        return repaired
    _LAST_RECONCILE_HEALTH = health
    try:
        _write_reconcile_health(health)
    except Exception as exc:  # noqa: BLE001 -- instruction repair never wounds indexing
        health = _health_unavailable(
            health, f"{type(exc).__name__}: {exc}")
        _PENDING_RECONCILE_HEALTH = health
        _LAST_RECONCILE_HEALTH = health
        try:
            _write_reconcile_health(health)
        except Exception:  # noqa: BLE001 -- in-process disclosure still survives
            pass
    return repaired


def _consent(found: list[tuple[str, Path]]) -> bool:
    """The setup-path gate: say exactly what gets written and where, then ask.
    Never prompts (and never writes) when stdin isn't a terminal - scripts and
    agents opt in explicitly with --yes."""
    print()
    print("add agrep to your agents' instructions? (recommended)")
    for agent, target in found:
        print(f"  {agent}: {target}")
    print("one marker-delimited block per file: what agrep is, when to reach for it.")
    print("recreates missing blocks; changed existing files need `agrep setup`.")
    print("self-removes if you delete agrep.")
    print("undo any time: agrep remove")
    if not sys.stdin.isatty():
        print("(not a terminal - skipped; `agrep setup --yes` opts in, recommended)")
        return False
    try:
        return input("write them? [Y/n] ").strip().lower() not in ("n", "no")
    except EOFError:  # windows NUL reports as a tty; EOF is the ground truth
        print("(no terminal input - skipped; `agrep setup --yes` opts in, recommended)")
        return False


def detected_agents() -> list[str]:
    """Agent names whose proof dir exists on this box (cli's completion copy)."""
    return [agent for agent, proof, _ in MD_TARGETS + SKILL_TARGETS
            if proof.is_dir()]


def teach(yes: bool = False) -> int:
    """The `agrep setup` teach step. Consent is asked once; a valid non-empty
    enrollment receipt converges silently, so setup stays safe to re-run."""
    if _data_dir_readonly():
        print("agent setup skipped: AGREP_DATA_READONLY protects this data directory")
        return 1
    found = [(agent, target) for agent, proof, target in MD_TARGETS + SKILL_TARGETS
             if proof.is_dir()]
    if not found:
        print("no agents detected.")
        return 0
    if not yes and not enrollment_active() and not _consent(found):
        return 0
    return _install()


def _install() -> int:
    if _data_dir_readonly():
        print("agent setup skipped: AGREP_DATA_READONLY protects this data directory")
        return 1
    if not removal_fence.clear_background_removal_fence():
        print("  ! setup is blocked by an active removal")
        return 1
    previous = _load_state()
    current_markdown = {str(target) for _, _, target in MD_TARGETS}
    current_skills = {str(target) for _, _, target in SKILL_TARGETS}
    persisted_skills = set(_state_paths(
        previous.get("skills"), skills_only=True, include_retired=True))
    prior_skills = current_skills | (persisted_skills - current_markdown)
    current_targets = current_markdown | current_skills
    prior_owned: list[Path] = []
    prior_refused: list[Path] = []
    retired_owned: list[tuple[Path, bool]] = []
    retired_refused: list[tuple[Path, bool, str]] = []
    for value in _state_paths(
            previous.get("targets"), include_retired=True):
        target = Path(value)
        retired = value not in current_targets
        is_skill = value in prior_skills
        try:
            spans = _block_spans(target.read_bytes())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except BlockStructureError as exc:
            if retired:
                retired_refused.append((target, is_skill, str(exc)))
            else:
                prior_refused.append(target)
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            if retired:
                retired_refused.append((target, is_skill, str(exc)))
            continue
        if not spans:
            continue
        if retired:
            if any(span.version > NUDGE_V for span in spans):
                retired_refused.append(
                    (target, is_skill, "newer agrep block preserved"))
            else:
                retired_owned.append((target, is_skill))
        else:
            prior_owned.append(target)
    written: list[Path] = []
    skills: list[Path] = []
    failed = False
    for agent, proof, target in MD_TARGETS:
        if not proof.is_dir():
            continue
        legacy = _legacy_block_version(target)
        try:
            verb = _write_block(target)
        except (OSError, ValueError) as exc:
            print(f"  {agent}: could not write {target}: {common.terminal_safe(exc)}")
            failed = True
            continue
        if verb == "skipped":
            failed = True
            continue
        transition = (
            f" v{legacy} -> v{NUDGE_V}"
            if verb == "updated" and legacy is not None else "")
        print(f"  {agent}: {verb} block{transition} in {target}")
        written.append(target)
    for agent, proof, target in SKILL_TARGETS:
        if not proof.is_dir():
            continue
        legacy = _legacy_block_version(target)
        try:
            verb = _write_skill(target)
        except (OSError, ValueError) as exc:
            print(f"  {agent}: could not write {target}: {common.terminal_safe(exc)}")
            failed = True
            continue
        if verb == "conflict":
            print(f"  {agent}: refused to overwrite existing unowned skill {target}")
            failed = True
            continue
        transition = (
            f" v{legacy} -> v{NUDGE_V}"
            if verb == "updated" and legacy is not None else "")
        print(f"  {agent}: {verb} skill{transition} in {target}")
        written.append(target)
        skills.append(target)
    enrolled = {str(target) for target in written}
    skill_paths = {str(target) for target in skills}

    def retain(target: Path, is_skill: bool) -> None:
        key = str(target)
        if key not in enrolled:
            written.append(target)
            enrolled.add(key)
        if is_skill and key not in skill_paths:
            skills.append(target)
            skill_paths.add(key)

    for target in prior_owned:
        retain(target, str(target) in prior_skills)
    installable = bool(written)
    for target in prior_refused:
        retain(target, str(target) in prior_skills)
    for target, is_skill in retired_owned:
        if not installable:
            retain(target, is_skill)
            continue
        try:
            remove = _remove_skill if is_skill else _remove_block
            removed = remove(target)
        except (OSError, ValueError) as exc:
            print(f"  retired: could not remove {target}: "
                  f"{common.terminal_safe(exc)}")
            failed = True
            retain(target, is_skill)
            continue
        if removed:
            noun = "skill" if is_skill else "block"
            print(f"  retired: {noun} removed from {target}")
    for target, is_skill, reason in retired_refused:
        retain(target, is_skill)
        if installable:
            print(f"  retired: cleanup deferred for {target}: "
                  f"{common.terminal_safe(reason)}")
            failed = True
    if written:
        _save_state(written, skills)
        if installable and not _sentinel_install(written):
            # the sentinel is uninstall hygiene, not the product - its failure
            # must not block enrollment or the index. doctor shows it unarmed.
            print("  ! uninstall sentinel could not be armed - instructions "
                  "work, but would linger if agrep is deleted "
                  "(`agrep doctor` tracks this; re-run setup from a normal "
                  "terminal to retry)")
    elif not failed:
        STATE_PATH.unlink(missing_ok=True)
    return 1 if failed else 0


def _stop_daemons() -> bool:
    """Removal must not strand background processes after their venv is deleted;
    lock arbitration against such a zombie can hang the next install."""
    if _data_dir_readonly():
        return False
    stopped = []
    clean = True
    indexers = indexd_runtime.stop_indexers_for_removal()
    stopped.extend(indexers.get("stopped") or ())
    if not indexers.get("ok"):
        state = str(indexers.get("owner_state") or "unsettled")
        print(f"  ! freshness daemon could not be stopped ({state})")
        clean = False
    try:
        import semworker
        outcome = semworker.stop_worker_and_wait()
        if outcome.get("ok") and outcome.get("pid"):
            stopped.append("semantic worker")
        elif not outcome.get("ok"):
            reason = str(
                outcome.get("reason")
                or outcome.get("owner_state")
                or "ownership did not settle")
            print(f"  ! semantic worker could not be stopped ({reason})")
            clean = False
    except Exception:  # noqa: BLE001 -- continue cleanup but return a failure
        print("  ! semantic worker teardown failed")
        clean = False
    try:
        import semantic
        outcome = semantic.stop_background_writers_for_removal()
        stopped.extend(outcome.get("stopped") or ())
        if not outcome.get("ok"):
            reason = str(outcome.get("state") or "ownership did not settle")
            print(f"  ! semantic background writer could not be stopped ({reason})")
            clean = False
    except Exception:  # noqa: BLE001 -- continue cleanup but return a failure
        print("  ! semantic background writer teardown failed")
        clean = False
    if stopped:
        print(f"  background processes stopped: {', '.join(stopped)}")
    return clean


def _remove() -> int:
    print("removing agrep integration:")
    if _data_dir_readonly():
        print("  ! removal skipped: AGREP_DATA_READONLY protects this data directory")
        return 1
    fence = removal_fence.acquire_background_removal_fence()
    if fence is None:
        print("  ! another removal is active; nothing was changed")
        return 1
    result = 1
    try:
        stopped = _stop_daemons()
        if not stopped:
            print("  ! removal aborted; integration and cleanup sentinel were preserved")
        else:
            state = _load_state()
            persisted = [
                Path(value)
                for value in _state_paths(
                    state.get("targets"), include_retired=True)
            ]
            persisted_skills = set(_state_paths(
                state.get("skills"), skills_only=True, include_retired=True))
            removals: list[tuple[str, Path, bool]] = []
            seen: set[str] = set()
            for agent, _, target in MD_TARGETS:
                removals.append((agent, target, False))
                seen.add(str(target))
            for agent, _, target in SKILL_TARGETS:
                removals.append((agent, target, True))
                seen.add(str(target))
            for target in persisted:
                key = str(target)
                if key not in seen:
                    removals.append(("retired", target, key in persisted_skills))
                    seen.add(key)
            failed = False
            for agent, target, is_skill in removals:
                try:
                    _mark_target_removing(target, is_skill)
                    remove = _remove_skill if is_skill else _remove_block
                    if remove(target):
                        noun = "skill" if is_skill else "block"
                        print(f"  {agent}: {noun} removed")
                    _unenroll_target(target)
                except (OSError, ValueError) as exc:
                    _reenroll_target(target, is_skill)
                    print(f"  {agent}: could not remove {target}: "
                          f"{common.terminal_safe(exc)}")
                    failed = True
            try:
                import hookinstall
                # remove() prints a receipt per agent, read back from the
                # config; a blanket "removed" here would speak for the
                # targets it deliberately preserved.
                if hookinstall.remove() != 0:
                    print("  ! post-compact hook removal was incomplete")
            except Exception as exc:  # noqa: BLE001 -- block removal still counts
                print(f"  ! post-compact hook removal failed: "
                      f"{common.terminal_safe(exc)}")
            if failed:
                print("  ! some targets were not removed; cleanup sentinel preserved")
            else:
                if _sentinel_remove():
                    STATE_PATH.unlink(missing_ok=True)
                    result = 0
                else:
                    if not STATE_PATH.exists():
                        _save_state([], [])
                    print("  ! cleanup scheduler could not be deregistered; "
                          "sentinel artifacts were preserved for retry")
    finally:
        if not removal_fence.finish_background_removal_fence(fence):
            print("  ! removal fence handoff failed; background work remains blocked")
            result = 1
    return result


def main(argv: list[str] | None = None) -> int:
    ap = surface.ArgumentParser(
        prog="agrep inject",
        description="teach detected agent CLIs how to search agrep's supported history "
                    "(idempotent). The public verbs are `agrep setup` (consent-gated) "
                    "and `agrep remove`.",
        allow_abbrev=False)
    ap.add_argument("--remove", action="store_true",
                    help="remove everything the teach step installed (= agrep remove)")
    args = ap.parse_args(argv)
    return _remove() if args.remove else _install()


if __name__ == "__main__":
    raise SystemExit(main())
