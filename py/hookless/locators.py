"""Store-location rules shared by hookless live observation and native resume."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys

from hookless.registry import LIVE_AGENTS, require_exact


def discovery_home(
        environ: Mapping[str, str] | None = None) -> str:
    """Match the Rust adapter registry's discovery-home precedence."""
    env = os.environ if environ is None else environ
    for key in ("AGREP_HOME", "USERPROFILE", "HOME"):
        if key in env:
            return env[key]
    return "."


HOME = discovery_home()
_KINDS = {
    "claude": "fixed",
    "codex": "fixed",
    "opencode": "opencode",
    "antigravity": "fixed",
    "cursor": "cursor",
    "pi": "pi",
}
_FIXED_ROOTS = {
    "claude": (".claude", "projects"),
    "codex": (".codex", "sessions"),
    "antigravity": (".gemini", "antigravity-cli", "brain"),
}
_PI_ROOTS = (
    (".pi", "agent", "sessions"),
    (".omp", "agent", "sessions"),
)
require_exact("hookless store locators", _KINDS, LIVE_AGENTS)
STORE_AGENTS = tuple(_KINDS)


def store_root(agent: str, home: str = HOME) -> str:
    try:
        parts = _FIXED_ROOTS[agent]
    except KeyError as exc:
        raise ValueError(f"{agent!r} has no fixed store root") from exc
    return os.path.join(home, *parts)


def opencode_data_dirs(
        home: str = HOME, *, environ: Mapping[str, str] | None = None,
        os_name: str | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    platform = os.name if os_name is None else os_name
    legacy = os.path.join(home, ".local", "share", "opencode")
    xdg = env.get("XDG_DATA_HOME")
    if "AGREP_HOME" in env:
        primary = legacy
    elif xdg:
        primary = os.path.join(xdg, "opencode")
    elif platform == "nt":
        local = env.get("LOCALAPPDATA")
        primary = os.path.join(
            local if local else os.path.join(home, "AppData", "Local"),
            "opencode")
    else:
        primary = legacy
    return [primary] if os.path.normcase(primary) == os.path.normcase(legacy) \
        else [primary, legacy]


def opencode_db_name(name: str) -> bool:
    lower = name.lower()
    return (lower.startswith("opencode") and lower.endswith(".db")
            and ".bak" not in lower and ".corrupted" not in lower)


def opencode_explicit_db(
        home: str = HOME, *, environ: Mapping[str, str] | None = None,
        os_name: str | None = None) -> str | None:
    env = os.environ if environ is None else environ
    raw = env.get("OPENCODE_DB", "")
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    if os.path.basename(raw) != raw or "/" in raw or "\\" in raw:
        return None
    return os.path.join(
        opencode_data_dirs(home, environ=env, os_name=os_name)[0], raw)


def cursor_db_candidates(
        home: str = HOME, *, environ: Mapping[str, str] | None = None,
        os_name: str | None = None, sys_platform: str | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    platform = os.name if os_name is None else os_name
    system = sys.platform if sys_platform is None else sys_platform
    appdata = env.get("APPDATA")
    windows_base = (
        (appdata if appdata else os.path.join(home, "AppData", "Roaming"))
        if platform == "nt"
        else os.path.join(home, "AppData", "Roaming")
    )
    windows = os.path.join(windows_base, "Cursor")
    macos = os.path.join(home, "Library", "Application Support", "Cursor")
    linux = os.path.join(home, ".config", "Cursor")
    if platform == "nt":
        roots = [windows, macos, linux]
    elif system == "darwin":
        roots = [macos, windows, linux]
    else:
        roots = [linux, windows, macos]
    return [
        os.path.join(root, "User", "globalStorage", "state.vscdb")
        for root in roots
    ]


def cursor_db_paths(
        home: str = HOME, *, environ: Mapping[str, str] | None = None,
        os_name: str | None = None, sys_platform: str | None = None) -> list[str]:
    candidates = cursor_db_candidates(
        home, environ=environ, os_name=os_name, sys_platform=sys_platform)
    selected = next(
        (path for path in candidates
         if os.path.isfile(path) and not os.path.islink(path)),
        candidates[0],
    )
    return [selected]


def store_roots(
        agent: str, home: str = HOME, *,
        environ: Mapping[str, str] | None = None,
        os_name: str | None = None, sys_platform: str | None = None) -> list[str]:
    kind = _KINDS.get(agent)
    if kind == "fixed":
        return [store_root(agent, home)]
    if kind == "opencode":
        roots = opencode_data_dirs(
            home, environ=environ, os_name=os_name)
        explicit = opencode_explicit_db(
            home, environ=environ, os_name=os_name)
        return roots + ([explicit] if explicit else [])
    if kind == "cursor":
        return cursor_db_paths(
            home, environ=environ, os_name=os_name,
            sys_platform=sys_platform)
    if kind == "pi":
        return [os.path.join(home, *parts) for parts in _PI_ROOTS]
    raise ValueError(f"no hookless store locator for {agent!r}")


def store_content(
        agent: str, path: str | os.PathLike[str], *,
        home: str = HOME, environ: Mapping[str, str] | None = None,
        os_name: str | None = None) -> bool:
    candidate = os.fspath(path)
    name = Path(candidate).name
    if agent == "claude":
        return Path(candidate).suffix == ".jsonl"
    if agent == "codex":
        return name.startswith("rollout-") and name.endswith(".jsonl")
    if agent == "opencode":
        explicit = opencode_explicit_db(
            home, environ=environ, os_name=os_name)
        return opencode_db_name(name) or (
            explicit is not None
            and os.path.normcase(os.path.abspath(candidate))
            == os.path.normcase(os.path.abspath(explicit))
        )
    if agent == "antigravity":
        return Path(candidate).suffix in {".json", ".jsonl"}
    if agent == "pi":
        return Path(candidate).suffix == ".jsonl"
    if agent == "cursor":
        return True
    raise ValueError(f"no hookless content predicate for {agent!r}")
