"""Install post-compact recovery hooks for Claude, Codex, pi, and OMP.

Claude shapes the summary with PreCompact. Codex injects resumed context with
SessionStart/compact. pi and OMP load the same lifecycle extension: OMP also
adds native summarizer context, while both export their exact session ID and
queue recovery guidance only at a compaction boundary or compacted resume.

Installed only by the hook choice in `agrep setup`; `--no-hook` skips it.
Never auto-repaired, and never overwrites a user's own hook or extension.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import common
import console

HOOKS_DIR = Path(__file__).resolve().parent / "hooks"
COMPACT_CONTRACT = HOOKS_DIR / "compact-contract.md"

# sha256 of every shipped compact-contract.md: a matching target is
# agrep-owned and upgradeable; anything else is the user's, never
# overwritten. Append the outgoing hash whenever the payload changes.
PRIOR_PAYLOAD_HASHES = frozenset({
    # v1 (8be88aa): anchors/killed/recovery as sections 10/11/12
    "1db9cb02f553646a67f8aa324568ed9189b0684cf58dddb1ceff2fa44bfa637e",
    # v2 (5b8be3d): recovery-first, six-item taxonomy
    "e5f5094089d8933f8eaf40cd7a755d6d751235eb768c6ebf4f02a2cb4eeab5ab",
    # v3 (2e2f013): recovery-first sections 10/11/12, --no-hook removal text
    "5a0e30eb423aaf6e0ad80d7b7f67727321a2ca1450ce690a55a34017f63cb204",
    # v4: recovery as a "can run" option, before the imperative-first rewrite
    "554847646608c9c83cc9964133cba32d44f13cec743af50de63ac041fba26988",
    # v5: imperative-first recovery, before the bench-path comment trim
    "210eb133202b27883f1f234a2b7c114d29a841806fb3479d5a347436594b26ad",
    # v5b: imperative-first with the bench-path comment and the pre-correction
    # recall claim (installed on the dev box between edits)
    "5ec787c96ae42955900d989bf63c18ee60dc71067cdf5d747afebd74dca09852",
    # v6: before postcompact's "returns exactly the pre-boundary turns" became
    # the bounded-excerpt wording the implementation actually serves
    "0557eb91adce33d36c08ca136e62a5a47e77e044b25b787f027d34b6e17347e1",
})

def _home() -> Path:
    """teach.HOME, read at call time - the one sandbox seam isolation patches.

    These paths must NOT be module constants: teach._remove() calls into this
    module, and a test that sandboxed teach's paths but not a snapshot taken
    here at import time deleted the developer's real hooks once. Deferred
    import because teach imports this module.
    """
    import teach
    return teach.HOME


# The shell `cat` of the payload, registered as claude's PreCompact hook.
def claude_settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def claude_hooks_dir() -> Path:
    return _home() / ".claude" / "hooks"


def claude_hook_target() -> Path:
    return claude_hooks_dir() / "compact-contract.md"


# codex fires SessionStart with source "compact" on a compaction; the hook
# emits the JSON envelope the codex CLI reads as additional context. Written
# only when ~/.codex already exists - setup never fabricates an agent's home.
def codex_hooks_paths() -> tuple[Path, ...]:
    return (_home() / ".codex" / "hooks.json",)


CODEX_PAYLOAD = HOOKS_DIR / "codex_compact_payload.py"

PI_EXTENSION = HOOKS_DIR / "pi_postcompact.ts"
PI_EXTENSION_NAME = "agrep-postcompact.ts"
PRIOR_PI_EXTENSION_HASHES: frozenset[str] = frozenset()


def pi_extension_paths() -> tuple[tuple[str, Path], ...]:
    """The active global config roots whose extension loaders share this payload."""
    home = _home()
    profile = os.environ.get("OMP_PROFILE") or os.environ.get("PI_PROFILE")
    if profile and Path(profile).name == profile and profile not in (".", ".."):
        omp_agent = home / ".omp" / "profiles" / profile / "agent"
    else:
        omp_agent = home / ".omp" / "agent"
    return (
        ("pi", home / ".pi" / "agent" / "extensions" / PI_EXTENSION_NAME),
        ("omp", omp_agent / "extensions" / PI_EXTENSION_NAME),
    )


def _codex_python() -> str:
    """The python that runs the codex payload: the one running agrep."""
    return sys.executable


def _shell_quote(value: str) -> str:
    """Single-quote a path for a shell command string (POSIX)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _read_settings() -> dict | None:
    """The user's claude settings.json as a dict.

    A missing file is a fresh box, not an error: it reads as an empty
    settings object so the hook block can be the file's first content.
    Only an unreadable or non-object file returns None (never overwrite
    what cannot be parsed).
    """
    try:
        raw = claude_settings_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_settings_atomic(value: dict) -> bool:
    """Atomically replace settings.json without touching user sidecars."""
    path = claude_settings_path()
    tmp: Path | None = None
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return False
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _payload_owned(existing: bytes, shipped: bytes) -> bool:
    """True when the bytes on disk are a payload agrep itself installed."""
    digest = hashlib.sha256(existing).hexdigest()
    return (digest == hashlib.sha256(shipped).hexdigest()
            or digest in PRIOR_PAYLOAD_HASHES)


def _pi_extension_owned(existing: bytes, shipped: bytes) -> bool:
    digest = hashlib.sha256(existing).hexdigest()
    return (
        digest == hashlib.sha256(shipped).hexdigest()
        or digest in PRIOR_PI_EXTENSION_HASHES
    )


def _claude_precompact_entry() -> list[dict[str, object]]:
    """The exact structured PreCompact registration written by setup."""
    return [{
        "matcher": "manual|auto",
        "hooks": [{
            "type": "command",
            "command": f"cat {_shell_quote(str(claude_hook_target()))}",
        }],
    }]


def install_claude_hook(*, warn: bool = True) -> bool:
    """Copy the payload to ~/.claude/hooks/ and register the PreCompact block.

    Never overwrites the user's work, on either side: if settings.json
    already has a PreCompact entry that is not ours we leave it, and if the
    contract file on disk is not one agrep shipped (the user edited or
    authored it) we leave that too and register the hook around their file.
    Installed once by setup; reconcile never touches it. install() reads its
    receipts back from the config afterwards; this only reports failure.
    """
    if not claude_settings_path().parent.is_dir():
        # no ~/.claude: not a claude box; setup never fabricates one
        return True
    target = claude_hook_target()
    if target.is_symlink():
        if warn:
            common.log(
                "~/.claude/hooks/compact-contract.md is a user-owned symlink; "
                "leaving it and skipping registration")
        return False
    try:
        claude_hooks_dir().mkdir(parents=True, exist_ok=True)
        payload = COMPACT_CONTRACT.read_bytes()
        try:
            existing = target.read_bytes()
        except FileNotFoundError:
            existing = None
        if existing is None or _payload_owned(existing, payload):
            if existing != payload:
                target.write_bytes(payload)
        elif warn:
            common.log(
                "~/.claude/hooks/compact-contract.md was edited by hand; "
                "leaving it (agrep never overwrites an edited contract)")
    except OSError:
        if warn:
            common.log("could not write ~/.claude/hooks/compact-contract.md")
        return False

    settings = _read_settings()
    if settings is None:
        if warn:
            common.log(
                "skipped registering the claude PreCompact hook: "
                "~/.claude/settings.json exists but could not be parsed "
                "(never overwriting it)")
        return False
    hooks = settings.get("hooks")
    if "hooks" in settings and not isinstance(hooks, dict):
        if warn:
            common.log(
                "skipped registering the claude PreCompact hook: "
                "~/.claude/settings.json has a non-object hooks field "
                "(never overwriting it)")
        return False
    if hooks is None:
        hooks = {}
    if "PreCompact" in hooks:
        existing = hooks["PreCompact"]
        # our own exact entry from a prior run is convergence, not a user hook
        if not _our_precompact_entry(existing) and warn:
            common.log(
                "~/.claude/settings.json already has a PreCompact hook; "
                "leaving it (agrep never overwrites a user hook)")
        return True
    hooks["PreCompact"] = _claude_precompact_entry()
    settings["hooks"] = hooks
    return _write_settings_atomic(settings)


def _codex_hook_command() -> str:
    """Render the payload argv for the current platform's command parser."""
    command = console.shell_command(
        _codex_python(), str(CODEX_PAYLOAD), fallback="")
    if not command:
        raise ValueError("codex hook command cannot be rendered safely")
    return command


def _codex_hooks_value(command: str) -> dict[str, object]:
    """The exact structured Codex hook registration for one command."""
    return {
        "hooks": {
            "SessionStart": [{
                "matcher": "^compact$",
                "hooks": [{
                    "type": "command",
                    "command": command,
                    "timeout": 2,
                    "statusMessage": "Post-compact recovery available",
                    "additionalContextLimit": 700,
                }],
            }],
        },
    }


def _legacy_codex_hook_command() -> str:
    """Exact command emitted before both argv operands were shell-rendered."""
    return f"{_codex_python()} {_shell_quote(str(CODEX_PAYLOAD))}"


def _codex_hook_payload() -> str:
    """The codex SessionStart/compact hook JSON (fire only on compaction).

    Runs the payload script, which reads the hook event on stdin and emits
    the JSON envelope codex reads as additional context - never on a normal
    prompt, and never a per-message gate.
    """
    return json.dumps(
        _codex_hooks_value(_codex_hook_command()), indent=2) + "\n"


def _codex_hooks_owned(raw: str) -> bool:
    """True only for an exact current or explicitly admitted legacy hook.

    Formatting and object-key order may differ because JSON is structural.
    Commands at any other path, changed arguments or fields, additional
    entries, and unparseable files are user-owned and must be preserved.
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    try:
        if value == _codex_hooks_value(_codex_hook_command()):
            return True
    except ValueError:
        # The current platform cannot safely render this installation path.
        pass
    # This is the one pre-platform-renderer receipt. It is still exact: the
    # current interpreter, payload path, matcher, arguments, and fields must
    # all match. Arbitrary same-basename paths are never admitted.
    return value == _codex_hooks_value(_legacy_codex_hook_command())


def install_codex_hooks(*, warn: bool = True) -> bool:
    """Write the Codex compact-only lifecycle integration.

    A hooks.json that is not agrep's own compaction hook is the user's and
    is left alone with a warning; codex's config.toml trust table is
    unchanged either way. A box without ~/.codex is skipped - setup never
    fabricates an agent home it did not find.
    """
    ok = True
    try:
        body = _codex_hook_payload()
    except ValueError:
        if warn:
            common.log("could not safely render the codex hook command")
        return False
    for path in codex_hooks_paths():
        if not path.parent.is_dir():
            continue
        if path.is_symlink():
            if warn:
                common.log(f"{path} is a user-owned symlink; leaving it")
            continue
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = None
        except OSError:
            ok = False
            if warn:
                common.log(f"could not read {path}")
            continue
        if existing is not None and not _codex_hooks_owned(existing):
            if warn:
                common.log(
                    f"{path} holds hooks agrep did not install; leaving it "
                    "(agrep never overwrites a user hook)")
            continue
        if existing == body:
            continue
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            ok = False
            if warn:
                common.log(f"could not write {path}")
    return ok


def install_pi_extensions(*, warn: bool = True) -> bool:
    """Install one byte-identical extension in each detected pi-family home."""
    try:
        payload = PI_EXTENSION.read_bytes()
    except OSError:
        if warn:
            common.log("could not read the shipped pi/OMP extension")
        return False
    ok = True
    for agent, path in pi_extension_paths():
        root = path.parent.parent
        if not root.is_dir():
            continue
        if root.is_symlink() or (
                path.parent.exists()
                and (not path.parent.is_dir() or path.parent.is_symlink())):
            if warn:
                common.log(
                    f"{agent} extension directory is not a plain directory; "
                    "leaving it")
            continue
        try:
            if not path.parent.exists():
                path.parent.mkdir()
            if path.is_symlink():
                if warn:
                    common.log(
                        f"{path} is a user-owned symlink; leaving it")
                continue
            try:
                existing = path.read_bytes()
            except FileNotFoundError:
                existing = None
            if existing is not None and not _pi_extension_owned(
                    existing, payload):
                if warn:
                    common.log(
                        f"{path} was not installed by agrep; leaving it")
                continue
            if existing != payload:
                path.write_bytes(payload)
        except OSError:
            ok = False
            if warn:
                common.log(f"could not write {path}")
    return ok


def _readonly_fenced() -> bool:
    """AGREP_DATA_READONLY means a no-writes run; hook files count.

    The fence names the data directory, but its contract is "this process
    mutates nothing" - a fenced removal that still edited settings.json
    would be a write nobody asked for.
    """
    return common.data_dir_readonly(common.DATA_DIR)


def _claude_hook_state() -> str:
    """What ~/.claude/settings.json holds right now, judged by the same
    ownership predicate install() writes with: no-box | none | ours | user |
    unreadable."""
    if not claude_settings_path().parent.is_dir():
        return "no-box"
    settings = _read_settings()
    if settings is None:
        return "unreadable"
    hooks = settings.get("hooks")
    if "hooks" in settings and not isinstance(hooks, dict):
        return "unreadable"
    if not isinstance(hooks, dict) or "PreCompact" not in hooks:
        return "none"
    entries = hooks["PreCompact"]
    return "ours" if _our_precompact_entry(entries) else "user"


def _codex_hook_state() -> str:
    """Same reading for codex's hooks.json, aggregated over the config paths.
    A user-owned or unreadable file anywhere outranks an agrep-owned one: the
    receipt must name the least-installed truth, never the friendliest."""
    states = set()
    for path in codex_hooks_paths():
        if not path.parent.is_dir():
            continue
        if path.is_symlink():
            states.add("user")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            states.add("none")
        except OSError:
            states.add("unreadable")
        else:
            states.add("ours" if _codex_hooks_owned(raw) else "user")
    for rank in ("unreadable", "user", "ours", "none"):
        if rank in states:
            return rank
    return "no-box"


def _pi_extension_state(path: Path) -> str:
    root = path.parent.parent
    if not root.is_dir():
        return "no-box"
    if root.is_symlink() or (
            path.parent.exists()
            and (not path.parent.is_dir() or path.parent.is_symlink())):
        return "unreadable"
    if path.is_symlink():
        return "user"
    try:
        existing = path.read_bytes()
        shipped = PI_EXTENSION.read_bytes()
    except FileNotFoundError:
        return "none"
    except OSError:
        return "unreadable"
    return "ours" if _pi_extension_owned(existing, shipped) else "user"


def _hook_states() -> tuple[str, ...]:
    return (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_extension_paths()),
    )


def has_hookable_agents() -> bool:
    return any(state != "no-box" for state in _hook_states())


def hooks_need_consent() -> bool:
    """Whether setup can add at least one integration it does not own yet."""
    return "none" in _hook_states()


def hooks_enrolled() -> bool:
    return "ours" in _hook_states()


def _install_receipt(agent: str, label: str, before: str, after: str) -> str:
    """One exact line per target, derived from what the config holds now."""
    if after == "user":
        return (f"  {agent}: skipped - your own {label} is already "
                "registered, left untouched")
    if after == "ours":
        if before == "ours":
            return f"  {agent}: {label} already installed, unchanged"
        return f"  {agent}: {label} installed (fires on compaction only)"
    if after == "unreadable":
        return f"  {agent}: {label} NOT installed - the config is unreadable"
    return f"  {agent}: {label} NOT installed"


def install(yes: bool = False, *, warn: bool = True) -> int:
    """Install every detected agent's post-compact recovery integration."""
    if _readonly_fenced():
        if warn:
            common.log("post-compact hook install skipped: "
                       "AGREP_DATA_READONLY forbids writes")
        return 1
    pi_paths = pi_extension_paths()
    before = (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_paths),
    )
    claude_ok = install_claude_hook(warn=warn)
    codex_ok = install_codex_hooks(warn=warn)
    pi_ok = install_pi_extensions(warn=warn)
    after = (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_paths),
    )
    if warn:
        targets = [
            ("claude", "PreCompact hook", before[0], after[0]),
            ("codex", "SessionStart/compact hook", before[1], after[1]),
            *(
                (agent, "post-compact extension", was, now)
                for (agent, _), was, now in zip(
                    pi_paths, before[2:], after[2:])
            ),
        ]
        lines = [
            _install_receipt(agent, label, was, now)
            for agent, label, was, now in targets
            if now != "no-box"
        ]
        for line in lines:
            print(line)
        if not lines:
            print("  no hookable agents on this box")
    return 0 if claude_ok and codex_ok and pi_ok else 1


def _our_precompact_entry(entries: object) -> bool:
    """True when the PreCompact block is exactly the one install() writes."""
    return entries == _claude_precompact_entry()


def remove(*, warn: bool = True) -> int:
    """Remove only exact hook and extension artifacts agrep still owns."""
    if _readonly_fenced():
        if warn:
            common.log("post-compact hook removal skipped: "
                       "AGREP_DATA_READONLY forbids writes")
        return 1
    pi_paths = pi_extension_paths()
    before = (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_paths),
    )
    ok = True
    settings = _read_settings()
    if settings:
        hooks = settings.get("hooks")
        if isinstance(hooks, dict) and "PreCompact" in hooks:
            if _our_precompact_entry(hooks["PreCompact"]):
                del hooks["PreCompact"]
                if not hooks:
                    settings.pop("hooks", None)
                if not _write_settings_atomic(settings):
                    ok = False
                    if warn:
                        common.log(
                            "could not update ~/.claude/settings.json")
            elif warn:
                common.log(
                    "~/.claude/settings.json has a PreCompact hook agrep "
                    "did not install; leaving it")
    target = claude_hook_target()
    if target.is_symlink():
        if warn:
            common.log(
                "~/.claude/hooks/compact-contract.md is a user-owned symlink; "
                "leaving it")
        existing = None
    else:
        try:
            existing = target.read_bytes()
        except FileNotFoundError:
            existing = None
        except OSError:
            existing = None
            ok = False
    if existing is not None:
        try:
            shipped = COMPACT_CONTRACT.read_bytes()
        except OSError:
            shipped = b""
        if _payload_owned(existing, shipped):
            try:
                target.unlink()
            except OSError:
                ok = False
        elif warn:
            common.log(
                "~/.claude/hooks/compact-contract.md was edited by hand; "
                "leaving it")
    for path in codex_hooks_paths():
        if path.is_symlink():
            if warn:
                common.log(f"{path} is a user-owned symlink; leaving it")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            ok = False
            continue
        if _codex_hooks_owned(raw):
            try:
                path.unlink()
            except OSError:
                ok = False
        elif warn:
            common.log(
                f"{path} holds hooks agrep did not install; leaving it")
    try:
        pi_payload = PI_EXTENSION.read_bytes()
    except OSError:
        pi_payload = b""
        ok = False
    for _, path in pi_paths:
        if path.is_symlink():
            if warn:
                common.log(f"{path} is a user-owned symlink; leaving it")
            continue
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            ok = False
            continue
        if _pi_extension_owned(existing, pi_payload):
            try:
                path.unlink()
            except OSError:
                ok = False
        elif warn:
            common.log(f"{path} was not installed by agrep; leaving it")
    if warn:
        after = (
            _claude_hook_state(),
            _codex_hook_state(),
            *(_pi_extension_state(path) for _, path in pi_paths),
        )
        targets = [
            ("claude", "PreCompact hook", before[0], after[0]),
            ("codex", "SessionStart/compact hook", before[1], after[1]),
            *(
                (agent, "post-compact extension", was, now)
                for (agent, _), was, now in zip(
                    pi_paths, before[2:], after[2:])
            ),
        ]
        for agent, label, was, now in targets:
            line = _remove_receipt(agent, label, was, now)
            if line:
                print(line)
    return 0 if ok else 1


def _remove_receipt(agent: str, label: str, before: str,
                    after: str) -> str | None:
    """The inverse receipt: "removed" only when agrep's own hook is gone, and
    a preserved user hook says so instead of borrowing the success line."""
    if before == "user":
        return f"  {agent}: left your own {label} in place"
    if before != "ours":
        return None
    if after == "ours":
        return f"  {agent}: {label} could NOT be removed"
    return f"  {agent}: {label} removed"
