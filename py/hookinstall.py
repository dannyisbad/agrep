"""Install post-compact recovery hooks for Claude, Codex, pi, and OMP.

Claude shapes the summary with PreCompact. Codex injects resumed context with
SessionStart/compact. pi and OMP load the same lifecycle extension: OMP also
adds native summarizer context, while both export their exact session ID to
the process environment, publish it per process for tool shells that inherit
a stale environment snapshot, and queue recovery guidance only at a
compaction boundary or compacted resume.

Installed only by the hook choice in `agrep setup`; `--no-hook` skips it.
Never auto-repaired, and never overwrites a user's own hook or extension.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
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
    # v7: before recovery explicitly checked the visible summary first
    "1d6c76882e0b8a8da3d19b724e88cfe47e946f56d5d8d17b50768bf2ae7251ed",
    # v8: before the recall warning was conditioned on caller identification
    "09c73004070ddabd3f2828c0f39b92a051e9e9ec5f04cb7f902865135f1708c6",
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
# only into a codex home that already exists - setup never fabricates one.
def codex_hooks_paths() -> tuple[Path, ...]:
    """hooks.json in every codex home this user runs: ~/.codex plus the
    CODEX_HOME override, mirroring teach.manifest_targets (CODEX_HOME is often
    per-shell, so the default home stays live in other shells)."""
    home = _home()
    paths = [home / ".codex" / "hooks.json"]
    raw = os.environ.get("CODEX_HOME", "")
    if raw:
        supplied = Path(os.path.expanduser(raw))
        candidate = Path(os.path.abspath(supplied))
        if supplied.is_absolute() and candidate != Path(os.path.abspath(home / ".codex")):
            paths.append(candidate / "hooks.json")
    return tuple(paths)


def known_codex_hooks_paths() -> tuple[Path, ...]:
    """The active homes plus every hooks.json agrep recorded writing: a hook
    installed under a per-shell CODEX_HOME is still found by a later remove."""
    paths = list(codex_hooks_paths())
    recorded = _load_restore().get("codex_hooks")
    home = _home()
    for item in recorded if isinstance(recorded, list) else ():
        path = Path(item) if isinstance(item, str) else None
        if (path is not None and path.is_absolute() and path.name == "hooks.json"
                and path.parent != home and path not in paths):
            paths.append(path)
    return tuple(paths)


CODEX_PAYLOAD = HOOKS_DIR / "codex_compact_payload.py"
CODEX_GUIDANCE = HOOKS_DIR / "codex-postcompact.md"
# the hook runs a copy of the stdlib-only payload placed inside the codex home,
# never the package's own (an npm/uv/pipx cache copy vanishes on every prune)
CODEX_PAYLOAD_DIR_PARTS = ("hooks", "agrep")
CODEX_LAUNCHER_NAME = "run"
# every payload/guidance byte stream agrep has shipped: a copy holding one of
# these is agrep's even when the restore record that placed it is gone
PRIOR_CODEX_PAYLOAD_HASHES: frozenset[str] = frozenset({
    # payload v1 (0.3.x) and v2 (0.4.0-0.4.2)
    "68a5073ae3d122ee5791d2ae9f87d15ca3cd1d0cb3c8a18dfc94bea734941b35",
    "34d8c5d8fec176392054cf56da33de1cb35aa36ebc2ff54a2bab2b21842235fa",
    # guidance v1 (0.3.x) and v2 (0.4.0-0.4.2)
    "0038bb5938acac0137e5ae9ba977d9da5332dc19836ade04cd72b0ae3c335a3d",
    "71c34acca9512759ed62edb2cdd90f75eac3c674674c3f9878f7485b769b99b6",
})


def codex_payload_dir(hooks_json: Path) -> Path:
    return hooks_json.parent.joinpath(*CODEX_PAYLOAD_DIR_PARTS)


def codex_payload_files(hooks_json: Path) -> tuple[tuple[Path, Path], ...]:
    """(installed copy, shipped source) for the files the codex hook needs."""
    directory = codex_payload_dir(hooks_json)
    return ((directory / CODEX_PAYLOAD.name, CODEX_PAYLOAD),
            (directory / CODEX_GUIDANCE.name, CODEX_GUIDANCE))


def codex_launcher_path(hooks_json: Path) -> Path:
    return codex_payload_dir(hooks_json) / CODEX_LAUNCHER_NAME


PI_EXTENSION = HOOKS_DIR / "pi_postcompact.ts"
PI_EXTENSION_NAME = "agrep-postcompact.ts"
PRIOR_PI_EXTENSION_HASHES: frozenset[str] = frozenset({
    # v1: before recovery required a current ID and missing IDs cleared the env
    "34c99ac023bca77e457359c36bc57f698e364e49aece0100c3135a4fe22ea55b",
    # v2: before the check-first gate stopped sufficient-summary pulls
    "127c3cf0cc2afc4bd3d2aee86b589ec3a80810b896413e3b6e3f084869186e32",
    # v3: env-only identity, before the per-process caller publication
    "4b26c969f97e57607fc8d8d21666442ac162dcdcf3d083ae6a2a9c72050b656f",
    # v4: publication written without checking the /tmp directory's owner
    "7f9fd2e3910ffa76c9b928aa14a98651f3809fdb55a0ac37c71bfc2e9efde114",
    "6cfdf55fd59ba3834f9557804b79da19930ea14e803b267a5cf04e2690033a94",
    # v5: recovery text named a `--more` continuation postcompact never printed
    "f87f3fa065e74d4954fda4d9cc6473615e8023c44781808de42efa86e0a6fc9f",
})


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
    """The python the codex hook may reference for years: under `uv tool run`
    (the npm shim) or pipx, sys.executable is a venv inside a prunable cache,
    while the venv's base interpreter is the managed install a cache clean never
    touches. The payload is stdlib-only, so the venv's packages are not missed."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    if base.name.lower().startswith("pythonw"):
        console_exe = base.with_name(base.name.lower().replace("pythonw", "python", 1))
        if console_exe.exists():
            base = console_exe
    return str(base if base.exists() else Path(sys.executable))


def _shell_quote(value: str) -> str:
    """Single-quote a path for a shell command string (POSIX)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _restore_record_path() -> Path:
    return common.DATA_DIR / "hook-restore.json"


_RESTORE_MAX_BYTES = 4 * 1024 * 1024


def _load_restore() -> dict:
    """What remove() needs to put the box back: the pre-agrep settings.json,
    the directories install() created, the codex hooks it wrote and the
    digests of the payload copies it placed."""
    try:
        raw = _restore_record_path().read_bytes()
        value = json.loads(raw.decode("utf-8")) if len(raw) <= _RESTORE_MAX_BYTES else {}
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


_RESTORE_KEYS = ("created_dirs", "claude_settings", "codex_hooks", "codex_payloads")


def _save_restore(record: dict) -> None:
    path = _restore_record_path()
    if not any(record.get(key) for key in _RESTORE_KEYS):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    body = json.dumps({"version": 1, **record}, ensure_ascii=False, indent=1) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # the record carries the user's settings.json bytes: keep it as private as they are
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(body)
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass


def _hook_dirs() -> list[Path]:
    return [claude_hooks_dir(), *(path.parent for _, path in pi_extension_paths()),
            *(codex_payload_dir(path) for path in known_codex_hooks_paths())]


def created_dirs() -> list[str]:
    """Directories install() created, deepest first; only ancestors of a hook
    location are admitted so a tampered record cannot aim rmdir elsewhere."""
    value = _load_restore().get("created_dirs")
    if not isinstance(value, list):
        return []
    home = _home()
    accepted = []
    for item in value:
        if not isinstance(item, str) or item in accepted:
            continue
        path = Path(item)
        if (path.is_absolute() and path != home
                and any(hook.is_relative_to(path) for hook in _hook_dirs())):
            accepted.append(item)
    return sorted(accepted, key=lambda item: -len(Path(item).parts))


def _mkdir_recording(directory: Path) -> None:
    """mkdir -p that records every directory it had to create."""
    missing: list[str] = []
    current = directory
    while not current.exists() and current.parent != current:
        missing.append(str(current))
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True)
    if not missing:
        return
    record = _load_restore()
    known = created_dirs()
    record["created_dirs"] = known + [item for item in missing if item not in known]
    _save_restore(record)


def _remove_created_dirs() -> None:
    record = _load_restore()
    for item in created_dirs():
        try:
            Path(item).rmdir()
        except OSError:
            pass
    record.pop("created_dirs", None)
    _save_restore(record)


def _read_settings_text() -> str | None:
    """The user's claude settings.json text, line endings intact (so a CRLF
    file is snapshotted and rendered as CRLF); None when it does not exist."""
    try:
        with open(claude_settings_path(), encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def _parse_settings(text: str | None) -> dict | None:
    """A missing file is a fresh box: it reads as an empty settings object so
    the hook block can be the file's first content. Only an unparsable or
    non-object file returns None (never overwrite what cannot be parsed)."""
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _read_settings() -> dict | None:
    try:
        return _parse_settings(_read_settings_text())
    except (OSError, UnicodeError):
        return None


def _render_settings(value: dict, like: str | None) -> str:
    """Serialize settings in the shape the user's file already has: its
    indentation, separators, escaping and trailing newline."""
    text = like or ""
    indented = re.search(r"^([ \t]+)(?=\S)", text, re.M)
    if indented:
        indent: str | int | None = indented.group(1)
    elif text.strip() and "\n" not in text.strip():
        indent = None
    else:
        indent = 2
    separators = (",", ":") if indent is None and '": ' not in text else None
    rendered = json.dumps(
        value, indent=indent, separators=separators,
        ensure_ascii=bool(like) and "\\u" in text)
    eol = "\r\n" if "\r\n" in text else "\n"
    rendered = rendered.replace("\n", eol)
    return rendered + (eol if like is None or text.endswith("\n") else "")


def _write_settings_atomic(text: str) -> bool:
    """Atomically replace settings.json without touching user sidecars."""
    path = claude_settings_path()
    tmp: Path | None = None
    fd = -1
    try:
        _mkdir_recording(path.parent)
        if path.is_symlink():
            return False
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fd = -1
            stream.write(text)
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


def _snapshot_settings_before_install(text: str | None) -> None:
    """Freeze the pre-agrep settings.json once, so remove() can restore it
    byte for byte (or delete the file agrep itself created)."""
    record = _load_restore()
    if "claude_settings" in record:
        return
    if text is None:
        record["claude_settings"] = {"existed": False}
    elif len(text.encode("utf-8")) <= _RESTORE_MAX_BYTES // 2:
        record["claude_settings"] = {"existed": True, "text": text}
    else:
        return
    _save_restore(record)

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
    # settings.json decides first: when the user's own PreCompact hook wins,
    # nothing is written at all, so "left untouched" stays literally true.
    try:
        text = _read_settings_text()
    except (OSError, UnicodeError):
        text = ""
    settings = _parse_settings(text)
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
    if "PreCompact" in hooks and not _our_precompact_entry(hooks["PreCompact"]):
        if warn:
            common.log(
                "~/.claude/settings.json already has a PreCompact hook; "
                "leaving it (agrep never overwrites a user hook)")
        return True
    try:
        _mkdir_recording(claude_hooks_dir())
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
    if "PreCompact" in hooks:
        # our own exact entry from a prior run is convergence, not a user hook
        return True
    _snapshot_settings_before_install(text)
    hooks["PreCompact"] = _claude_precompact_entry()
    settings["hooks"] = hooks
    return _write_settings_atomic(_render_settings(settings, text))


_CODEX_LAUNCHER = """\
#!/bin/sh
# agrep's codex compaction hook. Runs the payload copy beside this file with the
# interpreter recorded at install while it exists, else python3 from PATH.
p={python}
[ -x "$p" ] || p=python3
case $0 in */*) d=${{0%/*}} ;; *) d=. ;; esac
exec "$p" "$d/{payload}"
"""


def _codex_launcher_text(python: str) -> str:
    return _CODEX_LAUNCHER.format(python=shlex.quote(python), payload=CODEX_PAYLOAD.name)


def _codex_hook_command(hooks_json: Path) -> str:
    """The command codex runs on a compaction, through the user's login shell
    (`$SHELL -lc`): POSIX runs the launcher under /bin/sh, so fish, nushell and
    tcsh users get the same interpreter fallback; Windows is the plain argv."""
    python = _codex_python()
    payload = codex_payload_files(hooks_json)[0][0]
    if not console.shell_command(python, str(payload), fallback=""):
        raise ValueError("codex hook command cannot be rendered safely")
    if console.WIN:
        return console.shell_command(python, str(payload), fallback="")
    return f"/bin/sh {shlex.quote(str(codex_launcher_path(hooks_json)))}"


_CODEX_FALLBACK_TOKENS = ("[", "-x", "$p", "]", "||", "p=python3", ";", "exec", "$p")
_PYTHON_EXE_RE = re.compile(r"^python[0-9.]*(w)?(\.exe)?$|^Python$")


def _codex_interpreters() -> set[str]:
    """The interpreters agrep itself may have written into a registration."""
    candidates = {_codex_python(), sys.executable}
    recorded = _load_restore().get("codex_interpreters")
    if isinstance(recorded, list):
        candidates.update(item for item in recorded if isinstance(item, str))
    return candidates


def _codex_command_operands(command: str) -> tuple[str, str, str] | None:
    """(form, interpreter, target) for exactly the command shapes agrep has
    written, nothing looser: `/bin/sh <launcher>`; the inline fallback form
    with one of agrep's own interpreters; or one python executable path -
    quoted, or unquoted with spaces as the earliest form left it - followed
    by the payload. A prefix, an option, a wrapper or any other interpreter
    makes the registration the user's."""
    try:
        if console.WIN:
            tokens = [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
                      for t in shlex.split(command, posix=False)]
        else:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
    except ValueError:
        return None
    if len(tokens) == 2 and tokens[0] == "/bin/sh" and not console.WIN:
        return "launcher", tokens[0], tokens[1]
    if (len(tokens) == 12 and tokens[0].startswith("p=") and tokens[1] == ";"
            and tuple(tokens[2:11]) == _CODEX_FALLBACK_TOKENS):
        interpreter = tokens[0][2:]
        return ("fallback", interpreter, tokens[11]) if interpreter in _codex_interpreters() else None
    if len(tokens) >= 2 and not any(t in (";", "||", "&&", "|") for t in tokens):
        interpreter = " ".join(tokens[:-1])
        if (len(tokens) > 2 and not interpreter.startswith("/")) or any(
                t.startswith("-") for t in tokens[1:-1]):
            return None
        if _PYTHON_EXE_RE.match(Path(interpreter).name):
            return "bare", interpreter, tokens[-1]
    return None


def _codex_target_is_agreps(form: str, target: str) -> bool:
    """Only the places agrep has ever put what the command runs: the launcher
    in a codex home's hooks/agrep, or the payload there or in a package tree's
    py/hooks (any install or cache location)."""
    parts = Path(target).parts
    if len(parts) < 3:
        return False
    if form == "launcher":
        return parts[-1] == CODEX_LAUNCHER_NAME and parts[-3:-1] == CODEX_PAYLOAD_DIR_PARTS
    return parts[-1] == CODEX_PAYLOAD.name and (
        parts[-3:-1] == ("py", "hooks") or parts[-3:-1] == CODEX_PAYLOAD_DIR_PARTS)


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


def _codex_hook_payload(hooks_json: Path) -> str:
    """The codex SessionStart/compact hook JSON (fire only on compaction).

    Runs the payload copy inside this codex home, which reads the hook event
    on stdin and emits the JSON envelope codex reads as additional context -
    never on a normal prompt, and never a per-message gate.
    """
    return json.dumps(_codex_hooks_value(_codex_hook_command(hooks_json)), indent=2) + "\n"


def _codex_hooks_owned(raw: str) -> bool:
    """True only for a registration agrep wrote: the exact structure (matcher,
    arguments, fields, a single entry) around a command of one of agrep's own
    shapes aimed at one of agrep's own payload locations. An interpreter or
    payload copy that has since vanished still reads as ours, so a broken hook
    is repaired by setup and deleted by remove. Any other path, changed
    arguments or fields, additional entries, and unparseable files are
    user-owned and must be preserved."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    try:
        command = value["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(command, str) or value != _codex_hooks_value(command):
        return False
    operands = _codex_command_operands(command)
    return operands is not None and _codex_target_is_agreps(operands[0], operands[2])


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _codex_payload_owned(installed: Path, shipped: bytes) -> bool:
    """A payload copy is agrep's while it holds the shipped bytes, bytes agrep
    ever shipped, or the bytes agrep recorded placing there; anything else was
    edited by the user."""
    try:
        existing = installed.read_bytes()
    except OSError:
        return False
    if existing == shipped:
        return True
    digest = _digest(existing)
    if digest in PRIOR_CODEX_PAYLOAD_HASHES:
        return True
    recorded = _load_restore().get("codex_payloads")
    return isinstance(recorded, dict) and recorded.get(str(installed)) == digest


def _launcher_owned(launcher: Path, *, any_interpreter: bool = False) -> bool:
    """The launcher is agrep's while it is the template around an interpreter
    line naming an interpreter agrep itself recorded; a changed interpreter
    (the likeliest edit, after the recorded python broke) or any other edit
    makes it the user's. With any_interpreter, any single-path interpreter
    line counts: when agrep's own registration of the launcher is being
    removed, the launcher is dead weight whichever python it names."""
    try:
        lines = launcher.read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError):
        return False
    template = _codex_launcher_text("x").split("\n")
    if len(lines) != len(template):
        return False
    for mine, theirs in zip(template, lines):
        if mine == "p=x":
            try:
                value = shlex.split(theirs[2:]) if theirs.startswith("p=") else []
            except ValueError:
                return False
            if len(value) != 1 or not value[0]:
                return False
            if not any_interpreter and value[0] not in _codex_interpreters():
                return False
        elif mine != theirs:
            return False
    return True


def _registration_runs_launcher(raw: str | None, hooks_json: Path) -> bool:
    """True when hooks.json holds agrep's own `/bin/sh <this home's launcher>`."""
    if raw is None or not _codex_hooks_owned(raw):
        return False
    try:
        command = json.loads(raw)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    operands = _codex_command_operands(command)
    return (operands is not None and operands[0] == "launcher"
            and Path(operands[2]) == codex_launcher_path(hooks_json))


def owned_codex_files(hooks_json: Path, *, launcher_registered: bool | None = None) -> list[Path]:
    """The files under this home's hooks/agrep that are still agrep's to delete:
    payload and guidance copies holding shipped or recorded bytes, and the
    launcher. launcher_registered: hooks.json (as read by the caller) holds
    agrep's registration of this launcher; None reads it here."""
    owned: list[Path] = []
    for installed, source in codex_payload_files(hooks_json):
        try:
            shipped = source.read_bytes()
        except OSError:
            shipped = b""
        if installed.is_file() and not installed.is_symlink() \
                and _codex_payload_owned(installed, shipped):
            owned.append(installed)
    if launcher_registered is None:
        try:
            raw = hooks_json.read_text(encoding="utf-8")
        except (OSError, ValueError):
            raw = None
        launcher_registered = _registration_runs_launcher(raw, hooks_json)
    launcher = codex_launcher_path(hooks_json)
    if (launcher.is_file() and not launcher.is_symlink()
            and _launcher_owned(launcher, any_interpreter=launcher_registered)):
        owned.append(launcher)
    return owned


def _record_codex_install(hooks_json: Path, payloads: dict[str, str]) -> None:
    record = _load_restore()
    hooks = record.get("codex_hooks")
    hooks = [h for h in hooks if isinstance(h, str)] if isinstance(hooks, list) else []
    if str(hooks_json) not in hooks:
        hooks.append(str(hooks_json))
    digests = record.get("codex_payloads")
    digests = dict(digests) if isinstance(digests, dict) else {}
    digests.update(payloads)
    interpreters = record.get("codex_interpreters")
    interpreters = ([i for i in interpreters if isinstance(i, str)]
                    if isinstance(interpreters, list) else [])
    if _codex_python() not in interpreters:
        interpreters.append(_codex_python())
    record["codex_hooks"] = hooks
    record["codex_payloads"] = digests
    record["codex_interpreters"] = interpreters
    _save_restore(record)


def _install_codex_payload(hooks_json: Path, *, warn: bool) -> dict[str, str] | None:
    """Place (or refresh) the payload copy and launcher this home's hook runs;
    None on failure. A copy edited by hand is kept as it is (the hook keeps
    running the user's version) and said so, not counted as a failure."""
    digests: dict[str, str] = {}
    try:
        _mkdir_recording(codex_payload_dir(hooks_json))
        files = [*codex_payload_files(hooks_json)]
        launcher = codex_launcher_path(hooks_json)
        for installed, source in files:
            shipped = source.read_bytes()
            if installed.is_symlink():
                raise OSError(f"{installed} is a symlink")
            if installed.exists():
                if not _codex_payload_owned(installed, shipped):
                    if warn:
                        common.log(f"{installed} was edited by hand; kept as it is")
                    continue
                if installed.read_bytes() != shipped:
                    installed.write_bytes(shipped)
            else:
                installed.write_bytes(shipped)
            digests[str(installed)] = _digest(shipped)
        if not console.WIN:
            text = _codex_launcher_text(_codex_python()).encode("utf-8")
            if launcher.is_symlink():
                raise OSError(f"{launcher} is a symlink")
            if launcher.exists() and not _launcher_owned(launcher):
                if warn:
                    common.log(f"{launcher} was edited by hand; kept as it is")
            else:
                if not launcher.exists() or launcher.read_bytes() != text:
                    launcher.write_bytes(text)
                    launcher.chmod(0o755)
                digests[str(launcher)] = _digest(text)
    except OSError as exc:
        if warn:
            common.log(f"could not place the codex hook payload: {common.terminal_safe(exc)}")
        return None
    return digests


def install_codex_hooks(*, warn: bool = True) -> bool:
    """Write the Codex compact-only lifecycle integration.

    A hooks.json that is not agrep's own compaction hook is the user's and
    is left alone with a warning; codex's config.toml trust table is
    unchanged either way. A box without ~/.codex is skipped - setup never
    fabricates an agent home it did not find.
    """
    ok = True
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
        try:
            body = _codex_hook_payload(path)
        except ValueError:
            if warn:
                common.log("could not safely render the codex hook command")
            return False
        digests = _install_codex_payload(path, warn=warn)
        if digests is None:
            ok = False
            continue
        if existing != body:
            try:
                path.write_text(body, encoding="utf-8")
            except OSError:
                ok = False
                if warn:
                    common.log(f"could not write {path}")
                continue
        _record_codex_install(path, digests)
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
        if root.is_symlink() or path.parent.is_symlink():
            if warn:
                common.log(
                    f"{agent} extension directory is a symlink; leaving the "
                    "linked tree alone")
            continue
        if path.parent.exists() and not path.parent.is_dir():
            if warn:
                common.log(
                    f"{agent} extension directory is not a plain directory; "
                    "leaving it")
            continue
        try:
            if not path.parent.exists():
                _mkdir_recording(path.parent)
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
    """Same reading for codex's hooks.json, aggregated over the active config
    paths and the ones agrep recorded writing. A user-owned or unreadable file
    anywhere outranks an agrep-owned one: the receipt must name the
    least-installed truth, never the friendliest."""
    states = set()
    for path in known_codex_hooks_paths():
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


def _codex_hooks_stale_reason() -> str | None:
    """Classify an owned registration that differs from the current payload."""
    reasons: list[str] = []
    for path in codex_hooks_paths():
        try:
            raw = path.read_text(encoding="utf-8")
            if not _codex_hooks_owned(raw) or raw == _codex_hook_payload(path):
                continue
            command = json.loads(raw)["hooks"]["SessionStart"][0]["hooks"][0][
                "command"]
            operands = _codex_command_operands(command)
            if operands is None:
                continue
            reasons.append("upgrade" if Path(operands[2]).is_file() else "moved")
        except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError):
            continue
    if not reasons:
        return None
    return "upgrade" if all(reason == "upgrade" for reason in reasons) else "moved"




def _read_install_artifact(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _install_artifacts(pi_paths: tuple[tuple[str, Path], ...]) -> dict[Path, bytes | None]:
    paths = [claude_settings_path(), claude_hook_target()]
    for hooks_json in codex_hooks_paths():
        paths.append(hooks_json)
        paths.extend(installed for installed, _ in codex_payload_files(hooks_json))
        paths.append(codex_launcher_path(hooks_json))
    paths.extend(path for _, path in pi_paths)
    return {path: _read_install_artifact(path) for path in paths}


def _artifacts_changed(
        before: dict[Path, bytes | None],
        after: dict[Path, bytes | None],
        paths: tuple[Path, ...],
) -> bool:
    return any(before.get(path) != after.get(path) for path in paths)



def _pi_extension_state(path: Path) -> str:
    """no-box | none | ours | user | linked | unreadable: linked is a symlinked
    agent root or extensions dir (a dotfile-managed tree agrep leaves alone)."""
    root = path.parent.parent
    if not root.is_dir():
        return "no-box"
    if root.is_symlink() or path.parent.is_symlink():
        return "linked"
    if path.parent.exists() and not path.parent.is_dir():
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


def _install_receipt(agent: str, label: str, before: str, after: str,
                     path: Path | None = None, *, changed: bool = False) -> str:
    """One exact line per target, derived from what the config holds now."""
    if after == "user":
        return (f"  {agent}: skipped - your own {label} is already "
                "registered, left untouched")
    if after == "ours":
        if before == "stale-upgrade":
            return (f"  {agent}: {label} upgraded to the current launcher")
        if before in ("stale", "stale-moved"):
            return (f"  {agent}: {label} repaired (the previous registration "
                    "pointed at a moved or pruned install)")
        if before == "ours":
            if changed:
                return f"  {agent}: {label} updated"
            return f"  {agent}: {label} already installed, unchanged"
        return f"  {agent}: {label} installed (fires on compaction only)"
    if after == "linked" and path is not None:
        root = path.parent if path.parent.is_symlink() else path.parent.parent
        return (f"  {agent}: {label} NOT installed - {root} is a symlink, "
                "so agrep left that linked tree alone (copy "
                f"{PI_EXTENSION.name} into it yourself if you want it)")
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
    before_artifacts = _install_artifacts(pi_paths)
    before = (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_paths),
    )
    stale_reason = _codex_hooks_stale_reason()
    if before[1] == "ours" and stale_reason is not None:
        before = (before[0], f"stale-{stale_reason}", *before[2:])
    claude_ok = install_claude_hook(warn=warn)
    codex_ok = install_codex_hooks(warn=warn)
    pi_ok = install_pi_extensions(warn=warn)
    after_artifacts = _install_artifacts(pi_paths)
    after = (
        _claude_hook_state(),
        _codex_hook_state(),
        *(_pi_extension_state(path) for _, path in pi_paths),
    )
    if warn:
        codex_artifacts = tuple(
            artifact
            for hooks_json in codex_hooks_paths()
            for artifact in (
                hooks_json,
                *(installed for installed, _ in codex_payload_files(hooks_json)),
                codex_launcher_path(hooks_json),
            ))
        targets = [
            (
                "claude", "PreCompact hook", before[0], after[0], None,
                _artifacts_changed(
                    before_artifacts, after_artifacts,
                    (claude_settings_path(), claude_hook_target())),
            ),
            (
                "codex", "SessionStart/compact hook", before[1], after[1],
                None,
                _artifacts_changed(
                    before_artifacts, after_artifacts, codex_artifacts)),
            *(
                (
                    agent, "post-compact extension", was, now, path,
                    _artifacts_changed(
                        before_artifacts, after_artifacts, (path,)),
                )
                for (agent, path), was, now in zip(
                    pi_paths, before[2:], after[2:])
            ),
        ]
        lines = [
            _install_receipt(agent, label, was, now, path, changed=changed)
            for agent, label, was, now, path, changed in targets
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


def _restore_settings(settings: dict, current: str | None) -> bool:
    """Put settings.json back after agrep's entry is gone: the exact pre-agrep
    bytes when nothing else changed since, deletion when agrep created the
    file and nothing else was ever added, else a rewrite in the user's shape."""
    record = _load_restore()
    snapshot = record.get("claude_settings")
    restored = False
    if isinstance(snapshot, dict):
        if snapshot.get("existed") is False and not settings:
            try:
                claude_settings_path().unlink()
                restored = True
            except FileNotFoundError:
                restored = True
            except OSError:
                return False
        else:
            original = snapshot.get("text")
            if (isinstance(original, str)
                    and _parse_settings(original) == settings):
                restored = _write_settings_atomic(original)
                if not restored:
                    return False
    if not restored and not _write_settings_atomic(
            _render_settings(settings, current)):
        return False
    record.pop("claude_settings", None)
    _save_restore(record)
    return True


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
    try:
        text = _read_settings_text()
    except (OSError, UnicodeError):
        text = ""
    settings = _parse_settings(text)
    if settings:
        hooks = settings.get("hooks")
        if isinstance(hooks, dict) and "PreCompact" in hooks:
            if _our_precompact_entry(hooks["PreCompact"]):
                del hooks["PreCompact"]
                if not hooks:
                    settings.pop("hooks", None)
                if not _restore_settings(settings, text):
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
    for path in known_codex_hooks_paths():
        if path.is_symlink():
            if warn:
                common.log(f"{path} is a user-owned symlink; leaving it")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None
        except OSError:
            ok = False
            continue
        launcher_registered = _registration_runs_launcher(raw, path)
        if raw is not None:
            if _codex_hooks_owned(raw):
                try:
                    path.unlink()
                except OSError:
                    ok = False
            elif warn:
                common.log(
                    f"{path} holds hooks agrep did not install; leaving it")
        owned = owned_codex_files(path, launcher_registered=launcher_registered)
        for installed in owned:
            try:
                installed.unlink()
            except OSError:
                ok = False
        if warn:
            for installed in (*(c for c, _ in codex_payload_files(path)),
                              codex_launcher_path(path)):
                if installed.exists() and installed not in owned:
                    common.log(f"{installed} was edited by hand; leaving it")
        if owned:
            # hooks/agrep is agrep's directory by name; rmdir refuses anything left in it
            with contextlib.suppress(OSError):
                codex_payload_dir(path).rmdir()
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
    _remove_created_dirs()
    record = _load_restore()
    record.pop("codex_hooks", None)
    record.pop("codex_payloads", None)
    _save_restore(record)
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
