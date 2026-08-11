"""Structured display policy shared below the search/around render seam.

The functions here classify only fields already carried by indexed rows and
events. They do not inspect query prose, tune ranking, or hide machine rows.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Mapping, Sequence
import json
import math
import re
import shlex
from types import MappingProxyType
from typing import NamedTuple
import zlib


# F7 role taxonomy. Search/recall own the labels they print; this artifact keeps
# their source classification structural and gives the Claude lane one object
# to review instead of another hand-mirrored word list.
MESSAGE_ROLE_ORIGINS: Mapping[str, str] = MappingProxyType({
    "user": "lived",
    "agent": "lived",
    "subagent": "sidechain",
    "tool": "tool-output",
    "recap": "recap",
    "control": "synthetic",
    "synthetic": "synthetic",
    "harness": "fixture",
})
EVENT_KIND_ORIGINS: Mapping[str, str] = MappingProxyType({
    "tool": "tool-output",
    "control": "synthetic",
    "subagent_start": "sidechain",
    "subagent_result": "sidechain",
})
# Stable review/export name for the canonical Message taxonomy.
ROLE_ORIGINS = MESSAGE_ROLE_ORIGINS
_TAXONOMY_TOKEN_MAX_CHARS = 32


def _event_kind(row: Mapping) -> str:
    recognized: list[str] = []
    for field in ("event_kind", "kind"):
        kind = row.get(field)
        # Bound before mapping membership: hashing attacker-sized prose is not
        # part of provenance classification.
        if (type(kind) is str
                and len(kind) <= _TAXONOMY_TOKEN_MAX_CHARS
                and kind in EVENT_KIND_ORIGINS):
            recognized.append(kind)
    if not recognized or any(kind != recognized[0] for kind in recognized[1:]):
        return ""
    return recognized[0]


def _event_kind_conflicts(row: Mapping) -> bool:
    event_kind = row.get("event_kind")
    kind = row.get("kind")
    return (
        type(event_kind) is str
        and len(event_kind) <= _TAXONOMY_TOKEN_MAX_CHARS
        and event_kind in EVENT_KIND_ORIGINS
        and type(kind) is str
        and len(kind) <= _TAXONOMY_TOKEN_MAX_CHARS
        and kind in EVENT_KIND_ORIGINS
        and event_kind != kind
    )


def row_origin(row: Mapping) -> str:
    if _event_kind_conflicts(row):
        return "unknown"
    kind = _event_kind(row)
    who = row.get("who")
    if type(who) is not str or len(who) > _TAXONOMY_TOKEN_MAX_CHARS:
        who = ""
    origin = (
        EVENT_KIND_ORIGINS[kind]
        if kind else
        MESSAGE_ROLE_ORIGINS.get(who, "unknown")
    )
    if origin == "lived" and row.get("_sidechain") is True:
        return "sidechain"
    return origin


_HISTORY_INPUT_MAX_CHARS = 64 * 1024
_HISTORY_QUERY_MAX_CHARS = 4096
_HISTORY_COMMAND_LIMIT = 16
_COMMAND_TOOL_NAMES = frozenset({
    "bash", "cmd", "exec", "exec_command", "powershell", "pwsh", "shell",
    "terminal", "run_terminal_cmd",
})
_HISTORY_VALUE_OPTIONS = frozenset({
    "-C", "-n", "--agent", "--before", "--budget", "--chat", "--color",
    "--context", "--exclude-project", "--hits", "--max", "--model",
    "--no-who", "--project", "--session", "--since", "--sort", "--until",
    "--who", "--tool-output",
})
_HISTORY_FLAG_OPTIONS = frozenset({
    "-E", "-c", "-i", "-l", "-s", "-w", "--all-side-chats", "--classic",
    "--count", "--count-by-tier", "--coverage", "--flat", "--ignore-case",
    "--json", "--lexical", "--model-soft", "--no-auto", "--no-meta",
    "--no-self", "--no-tools", "--probe", "--self", "--semantic", "--soft",
    "--strict-semantic",
})
_HISTORY_READ_VERBS = frozenset({"pack", "recall", "search"})
_HISTORY_EVENT_VERBS = frozenset({
    "around", "audit", "board", "chats", "doctor", "pack", "postcompact", "recall",
    "search", "status", "tail",
})
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
_EXEC_COMMAND_WRAPPER = re.compile(
    r"^\s*(?:(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*)?"
    r"(?:await\s+)?tools\.exec_command\s*\(\s*\{")
_CMD_FIELD = re.compile(r"\s*(?:cmd\b|[\"']cmd[\"'])\s*:\s*")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)


def _history_text(value: object, limit: int) -> str | None:
    if type(value) is not str or not value or len(value) > limit:
        return None
    return value


def _history_query(value: object) -> str | None:
    text = _history_text(value, _HISTORY_QUERY_MAX_CHARS)
    if text is None:
        return None
    normalized = " ".join(text.split()).casefold()
    return normalized or None


def _quoted_value(text: str, start: int) -> str | None:
    if start >= len(text) or text[start] not in "\"'":
        return None
    quote = text[start]
    escaped = False
    for end in range(start + 1, min(len(text), start + _HISTORY_INPUT_MAX_CHARS)):
        char = text[end]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char != quote:
            continue
        try:
            value = ast.literal_eval(text[start:end + 1])
        except (SyntaxError, ValueError):
            return None
        return _history_text(value, _HISTORY_INPUT_MAX_CHARS)
    return None


def _json_command_values(text: str) -> tuple[list[str], list[list[str]]]:
    commands: list[str] = []
    argvs: list[list[str]] = []
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return commands, argvs
    if isinstance(value, list) and value and all(type(item) is str for item in value):
        if len(value) <= 128:
            argvs.append(value)
        return commands, argvs
    if not isinstance(value, dict):
        return commands, argvs
    authorities = [key for key in ("cmd", "command", "argv") if key in value]
    if len(authorities) > 1:
        return commands, argvs
    for key in ("cmd", "command"):
        command = _history_text(value.get(key), _HISTORY_INPUT_MAX_CHARS)
        if command is not None:
            commands.append(command)
    argv = value.get("argv")
    if type(argv) is str and len(argv) <= _HISTORY_INPUT_MAX_CHARS:
        try:
            decoded = json.loads(argv)
        except (json.JSONDecodeError, RecursionError):
            decoded = None
        if (isinstance(decoded, list) and decoded and len(decoded) <= 128
                and all(type(item) is str for item in decoded)):
            argvs.append(decoded)
    if (isinstance(argv, list) and argv and len(argv) <= 128
            and all(type(item) is str for item in argv)):
        argvs.append(argv)
    return commands, argvs


def _embedded_command_values(text: str) -> list[str]:
    call = _EXEC_COMMAND_WRAPPER.search(text)
    if call is None:
        return []
    match = _CMD_FIELD.match(
        text, call.end(), min(len(text), call.end() + 2048))
    if match is None:
        return []
    command = _quoted_value(text, match.end())
    return [command] if command is not None else []


def _shell_argvs(text: str) -> list[list[str]]:
    if len(text.splitlines()) != 1:
        return []
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return []
    if any(token in _SHELL_SEPARATORS
           or (token and set(token) <= {"<", ">"}) for token in tokens):
        return []
    return [tokens] if tokens else []


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_argv(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)) or not 0 < len(node.elts) <= 128:
        return None
    values = []
    for item in node.elts:
        if isinstance(item, ast.Constant) and type(item.value) is str:
            values.append(item.value)
        elif (_call_name(item) == "sys.executable"
              and isinstance(item, ast.Attribute)):
            values.append("<python>")
        else:
            return None
    return values


def _python_subprocess_argvs(code: str) -> list[list[str]]:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return []
    out = []
    for statement in tree.body:
        if isinstance(statement, ast.Expr):
            node = statement.value
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            node = statement.value
        else:
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if _call_name(node.func) not in {
                "subprocess.call", "subprocess.check_call",
                "subprocess.check_output", "subprocess.Popen",
                "subprocess.run"}:
            continue
        argv = _literal_argv(node.args[0])
        if argv is not None:
            out.append(argv)
            if len(out) >= _HISTORY_COMMAND_LIMIT:
                break
    return out


def _executable_name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name in {".agrep.cmd", ".agrep.exe"}:
        return name[1:]
    return name


def _agrep_arguments(argv: Sequence[str]) -> list[str] | None:
    values = list(argv)
    index = 0
    while index < len(values) and _ASSIGNMENT.fullmatch(values[index]):
        index += 1
    while index < len(values):
        executable = _executable_name(values[index])
        if executable == "env":
            index += 1
            if index < len(values) and values[index].startswith("-"):
                return None
            while index < len(values) and _ASSIGNMENT.fullmatch(values[index]):
                index += 1
            continue
        if executable == "time":
            index += 1
            if index < len(values) and values[index].startswith("-"):
                return None
            continue
        if executable == "command":
            index += 1
            if index < len(values) and values[index].startswith("-"):
                return None
            continue
        break
    if index >= len(values):
        return None
    executable = _executable_name(values[index])
    if executable in {"agrep", "agrep.cmd", "agrep.exe"}:
        return values[index + 1:]
    is_python = (
        values[index] == "<python>"
        or executable in {"py", "py.exe"}
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable)
        is not None
    )
    if is_python:
        index += 1
        while index < len(values) and values[index].startswith("-"):
            if values[index] in {"-c", "-m"}:
                return None
            index += 1
        if index < len(values) and _executable_name(values[index]) == "cli.py":
            return values[index + 1:]
    return None


def _history_positionals(args: Sequence[str]) -> list[str] | None:
    out = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            out.extend(args[index + 1:])
            break
        option = token.split("=", 1)[0]
        if token.startswith("-"):
            if "=" in token:
                if option not in _HISTORY_VALUE_OPTIONS:
                    return None
            elif option in _HISTORY_VALUE_OPTIONS:
                index += 1
                if index >= len(args):
                    return None
            elif option not in _HISTORY_FLAG_OPTIONS:
                return None
        else:
            out.append(token)
        index += 1
    return out


def _argv_history_queries(argv: Sequence[str]) -> list[str]:
    args = _agrep_arguments(argv)
    if args is None:
        return []
    verb = args[0].casefold() if args else ""
    if verb in _HISTORY_READ_VERBS:
        values = _history_positionals(args[1:])
        if values is None:
            return []
        candidates = values if verb == "pack" else [" ".join(values)]
    else:
        import surface_policy
        if verb in surface_policy.RESERVED_COMMAND_WORDS:
            return []
        values = _history_positionals(args)
        candidates = [] if values is None else [" ".join(values)]
    return [normalized for candidate in candidates
            if (normalized := _history_query(candidate)) is not None]


def _argv_is_history_read(argv: Sequence[str]) -> bool:
    args = _agrep_arguments(argv)
    if args is None:
        return False
    if not args:
        return True
    verb = args[0].casefold()
    if verb in _HISTORY_EVENT_VERBS:
        return True
    if verb in {"--help", "-h", "--version"}:
        return True
    return bool(_argv_history_queries(argv))


def _history_read_record(event: Mapping) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(event, Mapping) or event.get("kind") != "tool":
        return False, ()
    name = _history_text(event.get("name"), _TAXONOMY_TOKEN_MAX_CHARS)
    if name is None or name.casefold().rsplit(".", 1)[-1] not in _COMMAND_TOOL_NAMES:
        return False, ()
    text = _history_text(event.get("input"), _HISTORY_INPUT_MAX_CHARS)
    if (text is None or event.get("input_truncated") is not False
            or type(event.get("input_chars")) is not int
            or event.get("input_chars") != len(text)
            or any(ord(char) < 32 and char not in "\t\r\n" for char in text)):
        return False, ()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False, ()
    commands, argvs = _json_command_values(text)
    commands.extend(_embedded_command_values(text))
    commands.append(text)
    queries = []
    invoked = False
    for command in commands[:_HISTORY_COMMAND_LIMIT]:
        for argv in _shell_argvs(command):
            invoked = invoked or _argv_is_history_read(argv)
            queries.extend(_argv_history_queries(argv))
            executable = _executable_name(argv[0]) if argv else ""
            if (executable.startswith("python") or executable in {"py", "py.exe"}) \
                    and "-c" in argv:
                code_index = argv.index("-c") + 1
                if code_index < len(argv):
                    argvs.extend(_python_subprocess_argvs(argv[code_index]))
    for argv in argvs[:_HISTORY_COMMAND_LIMIT]:
        invoked = invoked or _argv_is_history_read(argv)
        queries.extend(_argv_history_queries(argv))
    return invoked, tuple(dict.fromkeys(queries))


def history_read_queries(event: Mapping) -> tuple[str, ...]:
    """Return queries from one complete, executed agrep history-read event."""
    return _history_read_record(event)[1]


def history_read_invocation(event: Mapping) -> bool:
    return _history_read_record(event)[0]


def history_read_invoked(event: Mapping, query: object) -> bool:
    normalized = _history_query(query)
    return (event.get("ok") is True and normalized is not None
            and normalized in history_read_queries(event))


_NEAR_FOLD_ORIGINS = frozenset({"sidechain", "tool-output", "synthetic", "fixture"})
_NEAR_BUCKET_LIMIT = 64
_NEAR_CANDIDATE_LIMIT = 4
_NEAR_HASH_SEEDS = (0x13579BDF, 0x2468ACE0, 0x9E3779B9, 0x85EBCA6B)
_NEAR_FEATURE_REPEAT_CAP = 8
_SIMILARITY_MAX_CHARS = 512
_STRUCTURE_TOKEN_MAX_CHARS = 256
_IDENTITY_TOKEN_MAX_CHARS = 1024
_NEAR_LINEAGE_SESSION_LIMIT = 4096


def _bounded_field(
    row: Mapping, name: str, limit: int,
) -> str | None:
    if name not in row or row.get(name) is None:
        return ""
    value = row.get(name)
    if type(value) is not str or len(value) > limit:
        return None
    return value


def _structure_key(row: Mapping) -> tuple[str, ...] | None:
    """Structure that must survive even when two visible strings coincide."""
    fields = tuple(
        _bounded_field(row, name, _STRUCTURE_TOKEN_MAX_CHARS)
        for name in (
            "who", "kind", "event_kind", "name", "tool_name", "status",
        )
    )
    if any(value is None for value in fields):
        return None
    who, kind, event_kind, name, tool_name, status = fields
    raw_turn = row.get("turn")
    if raw_turn is None:
        turn = None
    elif (type(raw_turn) is int
          and -(1 << 63) <= raw_turn <= (1 << 63) - 1):
        turn = raw_turn
    else:
        return None
    if "is_error" in row and type(row.get("is_error")) is not bool:
        return None
    is_error = (
        "true" if row.get("is_error") is True else
        "false" if row.get("is_error") is False else
        ""
    )
    sidechain_phase = (
        "spawn" if who == "subagent" and turn == 0 else
        "body" if who == "subagent" and turn is not None else
        "unknown" if who == "subagent" else
        ""
    )
    return (
        who, kind, event_kind, name, tool_name, status, is_error,
        sidechain_phase,
    )


def _row_text(row: Mapping) -> str:
    for field in ("snippet", "text", "output"):
        value = row.get(field)
        if type(value) is str and value:
            return value
    return ""


def _similarity_sample(text: str) -> str:
    """Keep stable head/tail evidence while bounding similarity work."""
    limit = _SIMILARITY_MAX_CHARS
    if len(text) <= limit:
        return text
    side = (limit - 1) // 2
    head = text[:side]
    if not text[side].isspace() and " " in head:
        head = head.rsplit(" ", 1)[0]
    tail_start = len(text) - side
    tail = text[tail_start:]
    if not text[tail_start - 1].isspace() and " " in tail:
        tail = tail.split(" ", 1)[1]
    return f"{head} {tail}"


class _TextProfile(NamedTuple):
    features: Counter[tuple[str, ...]]
    feature_count: int
    bands: tuple[int, ...]


def _text_profile(text: str) -> _TextProfile:
    """Bounded structural shingles plus cheap locality-sensitive bands."""
    sample = " ".join(_similarity_sample(text).split()).casefold()
    tokens = sample.split()
    features: Counter[tuple[str, ...]] = Counter()
    features.update(("word", token) for token in tokens)
    features.update(
        ("pair", left, right)
        for left, right in zip(tokens, tokens[1:])
    )
    if len(sample) < 5:
        features[("chars", sample)] += 1
    else:
        features.update(
            ("chars", sample[index:index + 5])
            for index in range(0, len(sample) - 4, 3)
        )
    # Cap repeated scaffolding so it cannot drown distinct payload features.
    # Small multiplicities still identify genuinely repetitive sibling rows
    # without letting boilerplate vote away different content.
    for feature, count in tuple(features.items()):
        if count > _NEAR_FEATURE_REPEAT_CAP:
            features[feature] = _NEAR_FEATURE_REPEAT_CAP
    encoded = tuple(
        b"\0".join(
            part.encode("utf-8", errors="surrogatepass")
            for part in feature
        )
        for feature in features
    )
    bands = tuple(
        min(zlib.crc32(feature, seed) for feature in encoded)
        for seed in _NEAR_HASH_SEEDS
    )
    return _TextProfile(features, sum(features.values()), bands)


def _profile_similarity(left: _TextProfile, right: _TextProfile) -> bool:
    """Linear multiset Dice score over bounded structural shingles."""
    if not left.feature_count or not right.feature_count:
        return False
    smaller, larger = (
        (left.features, right.features)
        if len(left.features) <= len(right.features) else
        (right.features, left.features)
    )
    common = 0
    for feature, count in smaller.items():
        other = larger.get(feature, 0)
        if other:
            common += min(count, other)
    return (
        2 * common / (left.feature_count + right.feature_count)
        >= 0.92
    )


def _family(row: Mapping, roots: Mapping[str, str] | None) -> str:
    session = _bounded_field(row, "session", _IDENTITY_TOKEN_MAX_CHARS)
    if session is None:
        return ""
    if roots is not None and session:
        root = roots.get(session)
        return (
            root
            if type(root) is str
            and len(root) <= _IDENTITY_TOKEN_MAX_CHARS
            else ""
        )
    for field in ("_family_root", "family_root", "parent"):
        value = _bounded_field(row, field, _IDENTITY_TOKEN_MAX_CHARS)
        if value is None:
            return ""
        if value:
            return value
    return ""


def _near_fold_eligible(row: Mapping) -> bool:
    """Require structural success evidence wherever a row can be a tool event."""
    if not _near_dedup_key(row):
        return False
    if _event_kind_conflicts(row):
        return False
    if "ok" in row and type(row.get("ok")) is not bool:
        return False
    if row.get("ok") is False:
        return False
    if "is_error" in row and type(row.get("is_error")) is not bool:
        return False
    if row.get("is_error") is True:
        return False
    if "status" in row:
        status = row.get("status")
        if type(status) is not str or len(status) > _DISPLAY_TOKEN_MAX_CHARS:
            return False
        normalized_status = status.strip().casefold()
        if normalized_status in {"error", "failed", "failure"}:
            return False
        if normalized_status not in {
                "", "ok", "success", "succeeded", "complete", "completed"}:
            return False
    origin = row_origin(row)
    if origin not in _NEAR_FOLD_ORIGINS and row.get("_meta_row") is not True:
        return False
    if _event_kind(row) or row.get("who") == "tool":
        # Unknown is not success for canonical events or flattened tool hits.
        # Wait for the owner seam to carry an explicit outcome instead of
        # folding a failed call or a start/result pair by prose.
        return row.get("ok") is True and bool(_event_kind(row))
    return True


def _near_dedup_key(row: Mapping) -> str:
    """Owner-derived identity of a payload-preserving repeat template.

    Fuzzy prose alone cannot distinguish a volatile worker token from an
    outcome-critical ``allow``/``block`` substitution. The ingest/render owner
    may opt a row in only with an exact private key derived from structured
    fields: semantic payload must remain in that identity, while only fields
    already known to be volatile may be normalized away.
    """
    value = row.get("_near_dedup_key")
    return (
        value
        if type(value) is str
        and 0 < len(value) <= _STRUCTURE_TOKEN_MAX_CHARS
        else ""
    )


def _lineage_sessions(row: Mapping) -> set[str] | None:
    """Exact session membership, including any upstream exact-fold lineage.

    A scalar `_dup_chats` count is not composable: two exact-fold groups may
    overlap by session. Refuse the near fold unless the caller also supplies
    the private `_dup_sessions` identities needed for a set union.
    """
    session = _bounded_field(row, "session", _IDENTITY_TOKEN_MAX_CHARS)
    if not session:
        return None
    upstream = row.get("_dup_chats")
    upstream_count = _source_count(upstream) if "_dup_chats" in row else 0
    if upstream_count is None:
        return None
    raw_sessions = row.get("_dup_sessions")
    if raw_sessions is None:
        return {session} if upstream_count == 0 else None
    if not isinstance(raw_sessions, (list, tuple, set, frozenset)):
        return None
    if len(raw_sessions) > _NEAR_LINEAGE_SESSION_LIMIT:
        return None
    hidden = {
        value for value in raw_sessions
        if type(value) is str
        and 0 < len(value) <= _IDENTITY_TOKEN_MAX_CHARS
        and value != session
    }
    if len(hidden) != len(raw_sessions) or len(hidden) != upstream_count:
        return None
    return {session, *hidden}


def _near_duplicate(
    left: Mapping,
    right: Mapping,
    roots: Mapping[str, str] | None,
    left_profile: _TextProfile | None = None,
    right_profile: _TextProfile | None = None,
) -> bool:
    if not _near_fold_eligible(left) or not _near_fold_eligible(right):
        return False
    if _near_dedup_key(left) != _near_dedup_key(right):
        return False
    if row_origin(left) != row_origin(right):
        return False
    if (row_origin(left) not in _NEAR_FOLD_ORIGINS
            and not (left.get("_meta_row") is True
                     and right.get("_meta_row") is True)):
        return False
    if _structure_key(left) != _structure_key(right):
        return False
    left_family, right_family = _family(left, roots), _family(right, roots)
    if not left_family or left_family != right_family:
        return False
    a_text = _row_text(left)
    b_text = _row_text(right)
    if min(len(a_text), len(b_text)) < 48:
        return False
    if min(len(a_text), len(b_text)) / max(len(a_text), len(b_text)) < 0.90:
        return False
    return _profile_similarity(
        left_profile or _text_profile(a_text),
        right_profile or _text_profile(b_text),
    )


def collapse_display_rows(
    rows: Sequence[dict],
    family_roots: Mapping[str, str] | None = None,
) -> list[dict]:
    """Collapse conservative sibling near-copies after the exact-fold pass.

    The caller owns cross-chat exact folding and must use this only on its
    display copy. Count/flat/JSON surfaces retain every row. Near-collapse is
    limited to one proven conversation lineage and machinery roles; lived
    prose, failures, and unrelated families remain separate even when their
    wording is identical. An existing exact-fold count remains compositional.
    """
    kept: list[dict] = []
    lineages: dict[int, set[str]] = {}
    profiles: dict[int, _TextProfile] = {}
    candidates: dict[
        tuple[tuple[str, tuple[str, ...], str, str], int, int],
        list[dict],
    ] = {}
    recent: dict[tuple[str, tuple[str, ...], str, str], list[dict]] = {}
    for row in rows:
        session = _bounded_field(row, "session", _IDENTITY_TOKEN_MAX_CHARS)
        session = session or ""
        origin = row_origin(row)
        family = _family(row, family_roots)
        lineage = _lineage_sessions(row)
        structure = _structure_key(row)
        eligible = (
            _near_fold_eligible(row)
            and bool(session)
            and bool(family)
            and lineage is not None
            and structure is not None
        )
        key = (
            origin,
            structure or (),
            family,
            _near_dedup_key(row),
        )
        profile = (
            _text_profile(_row_text(row))
            if eligible else None
        )
        pool: list[dict] = []
        seen_candidates: set[int] = set()
        if profile is not None:
            # Reserve the first slot for the newest structural peer. Minhash
            # is probabilistic: letting four band collisions fill the pool
            # first can otherwise crowd out an adjacent true near-pair.
            for candidate in reversed(recent.get(key, ())):
                candidate_id = id(candidate)
                if candidate.get("session") == session:
                    continue
                seen_candidates.add(candidate_id)
                pool.append(candidate)
                break
            for band_index, band in enumerate(profile.bands):
                bucket = candidates.get((key, band_index, band), ())
                for candidate in reversed(bucket):
                    candidate_id = id(candidate)
                    if (candidate_id in seen_candidates
                            or candidate.get("session") == session):
                        continue
                    seen_candidates.add(candidate_id)
                    pool.append(candidate)
                    if len(pool) >= _NEAR_CANDIDATE_LIMIT:
                        break
                if len(pool) >= _NEAR_CANDIDATE_LIMIT:
                    break
            # Fill unused slots with other recent peers. Minhash remains only
            # an accelerator, never the truth boundary.
            if len(pool) < _NEAR_CANDIDATE_LIMIT:
                for candidate in reversed(recent.get(key, ())):
                    candidate_id = id(candidate)
                    if (candidate_id in seen_candidates
                            or candidate.get("session") == session):
                        continue
                    seen_candidates.add(candidate_id)
                    pool.append(candidate)
                    if len(pool) >= _NEAR_CANDIDATE_LIMIT:
                        break
        representative = None
        for candidate in pool:
            if _near_duplicate(
                    candidate, row, family_roots,
                    profiles.get(id(candidate)), profile):
                representative = candidate
                break
        if representative is None:
            kept.append(row)
            if profile is not None:
                lineages[id(row)] = set(lineage)
                profiles[id(row)] = profile
                for band_index, band in enumerate(profile.bands):
                    stored = candidates.setdefault(
                        (key, band_index, band), [])
                    stored.append(row)
                    if len(stored) > _NEAR_BUCKET_LIMIT:
                        del stored[:-_NEAR_BUCKET_LIMIT]
                structural = recent.setdefault(key, [])
                structural.append(row)
                if len(structural) > _NEAR_BUCKET_LIMIT:
                    del structural[:-_NEAR_BUCKET_LIMIT]
            continue
        lineages.setdefault(
            id(representative),
            _lineage_sessions(representative)
            or {representative.get("session") or ""},
        ).update(lineage)
        representative["_dup_lineage"] = True
    for row in kept:
        lineage = lineages.get(id(row))
        if lineage:
            hidden = sorted(
                lineage - {row.get("session") or ""})
            if hidden:
                row["_dup_sessions"] = hidden
                row["_dup_chats"] = len(hidden)
    return kept


class ToolOutputPreview(NamedTuple):
    text: str
    source_bytes: int | None
    source_chars: int
    truncated: bool


_MAX_DISPLAY_COUNT = (1 << 63) - 1
_TOOL_PREVIEW_HARD_MAX = 4096
_EVENT_OUTPUT_CAP = 800


def _source_count(value: object) -> int | None:
    return (
        value
        if type(value) is int and 0 <= value <= _MAX_DISPLAY_COUNT
        else None
    )


def _declared_output_bytes(
    raw: str,
    raw_bytes: int | None,
    source_chars: int,
    declared_bytes: int | None,
    truncated: bool,
) -> int | None:
    """Accept only UTF-8 totals compatible with the retained source excerpt."""
    if declared_bytes is None or raw_bytes is None:
        return None
    raw_chars = len(raw)
    if source_chars < raw_chars:
        return None
    synthetic_ellipsis = truncated and raw.endswith("…")
    if source_chars == raw_chars and not synthetic_ellipsis:
        if truncated:
            return None
        return declared_bytes if declared_bytes == raw_bytes else None
    retained_chars, retained_bytes = raw_chars, raw_bytes
    # Rust's capped event form retains the exact prefix and adds one synthetic
    # ellipsis. Do not charge that display marker to the original UTF-8 source.
    if synthetic_ellipsis:
        retained_chars -= 1
        retained_bytes -= len("…".encode("utf-8"))
    remaining = source_chars - retained_chars
    if remaining < 0:
        return None
    lower = retained_bytes + remaining
    upper = retained_bytes + 4 * remaining
    return declared_bytes if lower <= declared_bytes <= upper else None


def _match_centered_preview(
        value: str, start: int, end: int, cap: int,
) -> str:
    """Bound one line around a proven span without discarding the match first."""
    if cap <= 0:
        return ""
    if len(value) <= cap:
        return value
    left_marker = 1 if start > 0 else 0
    right_marker = 1 if end < len(value) else 0
    room = max(0, cap - left_marker - right_marker)
    if room == 0:
        return (("…" if left_marker else "")
                + ("…" if right_marker else ""))[:cap]
    match_size = max(0, end - start)
    if match_size >= room:
        a, b = start, min(end, start + room)
    else:
        extra = room - match_size
        left = min(start, extra // 2)
        right = min(len(value) - end, extra - left)
        leftover = extra - left - right
        if leftover:
            take = min(start - left, leftover)
            left += take
            leftover -= take
        if leftover:
            right += min(len(value) - end - right, leftover)
        a, b = start - left, end + right
    return ("…" if a > 0 else "") + value[a:b] + (
        "…" if b < len(value) else "")


def tool_output_preview(
        event: Mapping, *, max_chars: int = 160,
        match_span: tuple[int, int] | None = None,
) -> ToolOutputPreview:
    """Return the first output line, or center a proven selected match."""
    output_value = event.get("output", "")
    if type(output_value) is not str:
        return ToolOutputPreview("", None, 0, False)
    raw = output_value
    raw_chars = len(raw)
    try:
        raw_bytes = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raw_bytes = None
    has_declared_chars = "output_chars" in event
    declared_chars = (
        _source_count(event.get("output_chars"))
        if has_declared_chars else raw_chars
    )
    declared_chars_valid = (
        declared_chars is not None and declared_chars >= raw_chars
    )
    source_chars = (
        declared_chars
        if declared_chars_valid
        else raw_chars
    )
    declared_bytes = (
        _source_count(event.get("output_bytes"))
        if "output_bytes" in event else None
    )
    marker_present = "output_truncated" in event
    truncated_marker = event.get("output_truncated", False)
    marker_valid = type(truncated_marker) is bool
    marker_true = marker_valid and truncated_marker is True
    inferred_truncated = source_chars > raw_chars
    impossible_complete_shape = raw_chars > _EVENT_OUTPUT_CAP
    stored_truncated = (
        marker_true or inferred_truncated or not marker_valid
        or impossible_complete_shape
    )
    canonical_truncated_excerpt = (
        has_declared_chars
        and declared_chars_valid
        and source_chars > _EVENT_OUTPUT_CAP
        and raw_chars == _EVENT_OUTPUT_CAP + 1
        and raw.endswith("…")
        and (
            marker_true
            or (not marker_present and inferred_truncated)
        )
    )
    valid_bytes = _declared_output_bytes(
        raw,
        raw_bytes,
        source_chars,
        declared_bytes,
        stored_truncated,
    )
    if stored_truncated and not canonical_truncated_excerpt:
        valid_bytes = None
    source_bytes = (
        valid_bytes
        if valid_bytes is not None else
        None if stored_truncated or raw_bytes is None else
        raw_bytes
    )
    if raw_bytes is None:
        return ToolOutputPreview("", None, source_chars, True)
    cap = (
        min(max_chars, _TOOL_PREVIEW_HARD_MAX)
        if type(max_chars) is int and max_chars >= 0 else
        160
    )
    selected = (
        isinstance(match_span, tuple) and len(match_span) == 2
        and all(type(value) is int for value in match_span)
        and 0 <= match_span[0] < match_span[1] <= len(raw)
    )
    if selected:
        line, start, end = _one_line_with_span(
            raw, match_span[0], match_span[1])
        shown = _match_centered_preview(line, start, end, cap)
    else:
        lines = [" ".join(value.split()) for value in raw.splitlines()
                 if value.strip()]
        line = lines[0] if lines else ""
        shown = line[:cap]
    return ToolOutputPreview(
        shown,
        source_bytes,
        source_chars,
        bool(stored_truncated or len(line) > len(shown)
             or (not selected and len(lines) > 1)),
    )


def _quoted_value_bounds(text: str, at: int) -> tuple[int, int] | None:
    """Bounds of the JSON-like string value containing `at`, excluding quotes."""
    quote = -1
    escaped = False
    for index, char in enumerate(text[:at + 1]):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quote = -1 if quote >= 0 else index
    if quote < 0:
        return None
    end = -1
    for index in range(max(at + 1, quote + 1), len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            end = index
            break
    if end < 0:
        return None
    # A quoted key is scaffolding, not the payload. Values are preceded by a
    # colon or live as an array element; keys are followed by a colon.
    if text[end + 1:].lstrip().startswith(":"):
        return None
    before = text[:quote].rstrip()
    if before and before[-1] not in ":,[{":
        return None
    return quote + 1, end


def _one_line(value: str) -> str:
    return " ".join(value.split())


_DISPLAY_TOKEN_CHARS = frozenset("-_.:+/")
_DISPLAY_TOKEN_MAX_CHARS = 64


def _display_token(value: object, fallback: str) -> str:
    """Bound one-line structured labels; malformed prose is not provenance."""
    if type(value) is not str or len(value) > _DISPLAY_TOKEN_MAX_CHARS:
        return fallback
    cleaned = _one_line(value)
    if (not cleaned or not all(
                char.isalnum() or char in _DISPLAY_TOKEN_CHARS
                for char in cleaned
            )):
        return fallback
    return cleaned


def _one_line_with_span(
    value: str, start: int, end: int,
) -> tuple[str, int, int]:
    """Normalize whitespace without letting discarded bytes spend span budget."""
    if start < 0 or end < start or end > len(value):
        raise ValueError("normalization span outside text")
    out: list[str] = []
    boundaries = [0] * (len(value) + 1)
    index = 0
    while index < len(value):
        boundaries[index] = len(out)
        if value[index].isspace():
            run_start = index
            while index < len(value) and value[index].isspace():
                index += 1
            if out and index < len(value):
                out.append(" ")
            for boundary in range(run_start + 1, index + 1):
                boundaries[boundary] = len(out)
            continue
        out.append(value[index])
        index += 1
        boundaries[index] = len(out)
    return "".join(out), boundaries[start], boundaries[end]


def _quoted_value_ranges(text: str) -> list[tuple[int, int]]:
    """Return JSON-like quoted value ranges, never quoted object keys."""
    values: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        quote = index
        index += 1
        escaped = False
        while index < len(text):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                break
            index += 1
        if index >= len(text):
            break
        end = index
        after = end + 1
        while after < len(text) and text[after].isspace():
            after += 1
        before = quote - 1
        while before >= 0 and text[before].isspace():
            before -= 1
        is_key = after < len(text) and text[after] == ":"
        is_value_position = before < 0 or text[before] in ":,[{"
        if not is_key and is_value_position:
            values.append((quote + 1, end))
        index = end + 1
    return values


def _terminal_followup_value(
    text: str, after: int,
) -> tuple[int, int] | None:
    """Pick the terminal nonblank following value without field-name heuristics."""
    candidates = [
        bounds for bounds in _quoted_value_ranges(text)
        if bounds[0] >= after
        and _one_line(text[bounds[0]:bounds[1]])
    ]
    return candidates[-1] if candidates else None


def payload_snip_at(
    text: str,
    start: int,
    end: int,
    pad: int = 80,
    *,
    payload_bounds: tuple[int, int] | None = None,
) -> str:
    """Center a snippet inside a structured value before spending on its keys.

    Plain prose keeps the established centered window. When the match is inside
    a JSON-like quoted value, the same budget shifts within that value: field
    names and sibling values are discarded before post-match payload bytes.
    A caller-supplied payload span is structural authority and may contain the
    match or name a later quoted/unquoted value such as canonical tool output;
    it is never inferred from field-name phrases.
    """
    if start < 0 or end < start or end > len(text):
        raise ValueError("snippet span outside text")
    pad = max(0, int(pad))
    if payload_bounds is not None and (
            not isinstance(payload_bounds, tuple)
            or len(payload_bounds) != 2
            or not all(type(value) is int for value in payload_bounds)
            or payload_bounds[0] >= payload_bounds[1]
            or payload_bounds[0] < 0
            or payload_bounds[1] > len(text)
            or not (
                payload_bounds[0] >= end
                or (
                    payload_bounds[0] <= start
                    and end <= payload_bounds[1]
                )
            )):
        raise ValueError("payload bounds outside following text")
    bounds = (
        None if payload_bounds is not None
        else _quoted_value_bounds(text, start)
    )
    if payload_bounds is not None:
        normalized = False
        if payload_bounds[0] <= start:
            # Normal tool-output search: discard name/input before spending
            # any snippet budget around the match inside the exact payload.
            left, source_end = payload_bounds
            source = text[left:source_end]
        else:
            # A discriminator/input match may point to a later authoritative
            # payload; omit the intervening scaffolding.
            left, match_end = start, end
            payload = _one_line(text[payload_bounds[0]:payload_bounds[1]])
            source = text[left:match_end]
            if payload:
                source += " · " + payload
            source_end = payload_bounds[1]
            normalized = not any(
                character.isspace() for character in text[left:match_end])
    elif bounds is None or end > bounds[1]:
        left, source_end = 0, len(text)
        source = text
    else:
        # A regex span may include the opening quote itself. Keep that byte in
        # the local source instead of producing a negative normalized offset.
        left, source_end = min(start, bounds[0]), bounds[1]
        source = text[left:source_end]
    local_start, local_end = start - left, end - left
    # A short discriminator can own the match while a later value owns the
    # useful payload. Spend the remainder on the terminal following value,
    # omitting intervening keys and metadata values.
    if (payload_bounds is None and bounds is not None
            and source_end - end < pad):
        followup = _terminal_followup_value(text, source_end)
        if followup is not None:
            source += " · " + _one_line(
                text[followup[0]:followup[1]])
            source_end = followup[1]
    if not (payload_bounds is not None and normalized):
        source, local_start, local_end = _one_line_with_span(
            source, local_start, local_end)
    budget = max(0, local_end - local_start) + 2 * pad
    a = max(0, local_start - pad)
    b = min(len(source), local_end + pad)
    missing = max(0, budget - (b - a))
    if missing:
        take = min(missing, len(source) - b)
        b += take
        missing -= take
    if missing:
        a -= min(missing, a)
    return (
        ("…" if left > 0 or a > 0 else "") + source[a:b]
        + ("…" if source_end < len(text) or b < len(source) else "")
    )


def semantic_warming_line(estimated_s: float | None = None) -> str:
    """One edge disclosure while the keyword lane answers immediately."""
    seconds = math.nan
    if type(estimated_s) in (int, float):
        try:
            seconds = float(estimated_s)
        except (ValueError, OverflowError):
            pass
    estimate = (
        f" (~{max(1, round(seconds))}s)"
        if math.isfinite(seconds) and seconds > 0 else ""
    )
    return f"semantic warming{estimate} — keyword results below"


def _coverage_counts(
    coverage: Mapping,
) -> tuple[int, int, int, bool] | None:
    if not isinstance(coverage, Mapping):
        return None
    indexed = _source_count(coverage.get("indexed"))
    total = _source_count(coverage.get("total"))
    pending = _source_count(coverage.get("pending"))
    complete = coverage.get("complete")
    if (indexed is None or total is None or pending is None
            or type(complete) is not bool
            or indexed + pending != total
            or complete is not (pending == 0)):
        return None
    percent = 100 if total == 0 and complete else (
        indexed * 100 // total if total else 0)
    return indexed, total, percent, complete


def semantic_coverage_line(coverage: Mapping | None) -> str | None:
    """Render partial semantic coverage without implying full-corpus search."""
    counts = _coverage_counts(coverage) if coverage is not None else None
    if counts is None:
        return (
            "semantic: embedding coverage unavailable; "
            "searched scope is not verified"
        )
    indexed, total, percent, complete = counts
    if complete:
        return None
    return f"semantic: searched {indexed}/{total} embedded rows ({percent}%)"


def semantic_empty_line(coverage: Mapping | None) -> str:
    """Distinguish a covered miss from history that has not embedded yet."""
    counts = _coverage_counts(coverage) if coverage is not None else None
    if counts is None:
        return "semantic: no match; embedding coverage is unavailable"
    indexed, total, _, complete = counts
    if complete:
        return f"semantic: no match among {total} embedded rows"
    waiting = total - indexed
    if indexed == 0:
        return ("semantic: no embedded rows were searchable yet; "
                f"{waiting} rows are not embedded yet")
    return (f"semantic: no match in the {indexed} embedded rows searched; "
            f"{waiting} rows are not embedded yet")


def keyword_empty_line(corpus_messages: int | None) -> str:
    """A lexical miss names the corpus against which it was established."""
    return matcher_empty_line("keyword", corpus_messages)


# -w and -E state exact intent, so their miss names the lane that reads for
# meaning. Every matcher owns a zero line: rc 1 may mean no match, but on no
# profile may it be the only evidence the query ran.
_EXACT_MATCHER_LEVER = " - `-s` runs semantic search"


def matcher_empty_line(mode: str, corpus_messages: int | None) -> str:
    """A lexical miss names its matcher, the corpus it was established
    against, and - for the exact matchers - the lane that reads for meaning."""
    label = mode if mode in ("keyword", "word", "regex") else "keyword"
    lever = "" if label == "keyword" else _EXACT_MATCHER_LEVER
    corpus_count = _source_count(corpus_messages)
    if corpus_count is None:
        return f"{label}: no match; indexed corpus size is unavailable{lever}"
    return (
        f"{label}: 0 matching rows across "
        f"{corpus_count:,} indexed messages{lever}"
    )


def probe_pointer_label(
    row: Mapping, *, semantic: bool, weak: bool = False,
) -> str:
    """A top pointer is evidence to inspect, never a claimed conclusion."""
    evidence_kind = (
        "semantic evidence" if semantic is True else
        "prose evidence" if semantic is False else
        "unverified evidence"
    )
    # A malformed hedge marker cannot strengthen a pointer.
    evidence = f"weak {evidence_kind}" if weak is not False else evidence_kind
    kind = _event_kind(row)
    who = _display_token(
        kind or row.get("who") or row.get("event_kind")
        or row.get("kind"),
        "unknown",
    )
    meta = ", ~meta" if row.get("_meta_row") is True else ""
    return (f"top candidate ({evidence}; provenance: "
            f"{row_origin(row)}/{who}{meta})")


def probe_miss_line(
    engine: str, *, corpus_sessions: int | None = None,
    semantic_warming: bool = False,
) -> str:
    """The enrollment/miss edge remains judgeable even when rc is one."""
    session_count = _source_count(corpus_sessions)
    searched = (
        "corpus session count unavailable"
        if session_count is None else
        f"searched {session_count} past session(s)"
    )
    lane = ("; semantic model warming, keyword evidence only"
            if semantic_warming is True else "")
    engine_label = _display_token(engine, "unknown engine")
    return (f"recall: no confident past-context pointer "
            f"({engine_label}; {searched}{lane})")
