"""Dependency-light vocabulary and thresholds shared by agrep's CLI surfaces."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple


class SettingSpec(NamedTuple):
    name: str
    default: object
    choices: tuple[str, ...] | None
    value_help: str
    update_note: str
    public: bool


_EMPTY_MAP = MappingProxyType({})
SETTING_SPECS = (
    SettingSpec("tools", "on", ("on", "off"), "on/off",
                "search index picks it up on the next query", True),
    SettingSpec("embeddings", "auto", ("auto", "off"), "auto/off",
                "off blocks model downloads and background builds", True),
    SettingSpec("post_index", "off", None, "any command, or off",
                "runs after each successful index", True),
    SettingSpec("post_index_hooks", _EMPTY_MAP, None, "", "", False),
    SettingSpec("board_view", _EMPTY_MAP, None, "", "", False),
)
SETTINGS: Mapping[str, SettingSpec] = MappingProxyType(
    {spec.name: spec for spec in SETTING_SPECS})
PUBLIC_SETTINGS = tuple(spec for spec in SETTING_SPECS if spec.public)
PUBLIC_SETTING_CHOICES: Mapping[str, tuple[str, ...] | None] = MappingProxyType(
    {spec.name: spec.choices for spec in PUBLIC_SETTINGS})
_MISSING = object()
_TERMINAL_TRANSLATIONS = {
    code: f"\\u{code:04x}"
    for code in (*range(0x20), *range(0x7F, 0xA0), 0xAD)
}
_TERMINAL_MULTILINE_TRANSLATIONS = {
    **_TERMINAL_TRANSLATIONS,
    ord("\t"): "    ",
    ord("\n"): "\n",
}
_TERMINAL_UNSAFE_CATEGORIES = frozenset({"Cf", "Cs", "Zl", "Zp"})
_TERMINAL_UNICODE_PROBE = re.compile(r"[^\x00-\xff]")
# Printable ASCII is a fixed point of both translation tables and never
# reaches the unicode-category walk; one C-level probe skips all of it.
_TERMINAL_PLAIN_ASCII = re.compile(r"[^ -~]")
_LIVE_CIPHERTEXT_TOKEN = re.compile(r"gAAAAA[A-Za-z0-9_-]{64,}={0,2}")
_LIVE_BOOTSTRAP_CAPABILITY = re.compile(
    r"(?i)([?#&]bootstrap(?:[-_]?token)?=)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^&\s\"'<>]+)")
_LIVE_BOOTSTRAP_HEADER = re.compile(
    r"(?i)(\bX-Agrep-Bootstrap(?:\"|')?[ \t]*:[ \t]*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)")
_LIVE_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(\b(?:Proxy-)?Authorization(?:\"|')?[ \t]*:[ \t]*"
    r"(?:\"|')?(?:Bearer|Basic|Token)[ \t]+)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)")
_LIVE_BEARER_TOKEN = re.compile(
    r"(?i)(\bBearer[ \t]+)([A-Za-z0-9._~+/=-]{8,})")
_LIVE_AGREP_COOKIE = re.compile(
    r"(?i)(\bagrep_ui_[0-9a-f]{12}=)[^;\s\"']+")
_LIVE_URL_USERINFO = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/?#\s@]+)@")
_LIVE_SECRET_FLAG = re.compile(
    r"(?i)(--?(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|"
    r"refresh[-_]?token|session[-_]?token|password|passwd|secret|"
    r"client[-_]?secret|private[-_]?key|signing[-_]?key|"
    r"secret[-_]?access[-_]?key|database[-_]?url|connection[-_]?string|"
    r"bootstrap(?:[-_]?token)?)"
    r"(?:[ \t]*=[ \t]*|[ \t]+))"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]&#]+)")
_LIVE_SECRET_FIELD = re.compile(
    r"(?i)((?:\"|')?(?:api[-_]?key|access[-_]?token|auth[-_]?token|"
    r"refresh[-_]?token|session[-_]?token|password|passwd|secret|"
    r"client[-_]?secret|private[-_]?key|signing[-_]?key|"
    r"secret[-_]?access[-_]?key|database[-_]?url|connection[-_]?string|"
    r"bootstrap[-_]?token|[a-z][a-z0-9]*_(?:api_key|access_token|"
    r"auth_token|refresh_token|session_token|password|secret|private_key|"
    r"signing_key|secret_access_key|database_url|connection_string))"
    r"(?:\"|')?[ \t]*[:=][ \t]*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]&#]+)")
_LIVE_SAFE_FIELD_VALUES = frozenset({
    "bool", "boolean", "bytes", "float", "int", "integer", "none",
    "null", "number", "optional", "required", "str", "string", "true",
    "false",
})


def terminal_safe(value: object, *, multiline: bool = False) -> str:
    """Render untrusted text without letting it issue terminal commands."""
    text = "" if value is None else str(value)
    if _TERMINAL_PLAIN_ASCII.search(text) is None:
        return text
    translations = (
        _TERMINAL_MULTILINE_TRANSLATIONS
        if multiline else _TERMINAL_TRANSLATIONS)
    translated = text.translate(translations)
    if _TERMINAL_UNICODE_PROBE.search(translated) is None:
        return translated
    return "".join(
        f"\\u{ord(char):04x}"
        if unicodedata.category(char) in _TERMINAL_UNSAFE_CATEGORIES else char
        for char in translated)


_RESERVED_SEARCH_WORDS = frozenset({
    "archive", "around", "audit", "board", "chats", "doctor", "index",
    "inject", "live", "pack", "postcompact", "recall", "reindex", "remove", "restore",
    "resume", "run", "search", "serve", "set", "setup", "status", "tail",
    "ui", "up",
})
RESERVED_COMMAND_WORDS = _RESERVED_SEARCH_WORDS
_SEARCH_FLAG_OPTIONS = frozenset({
    "-E", "-c", "-i", "-l", "-s", "-w", "--all-side-chats",
    "--classic", "--count", "--count-by-tier", "--coverage", "--flat",
    "--ignore-case", "--json", "--lexical", "--model-soft", "--no-auto",
    "--no-meta", "--no-self", "--self", "--semantic", "--soft",
    "--strict-semantic",
})
_SEARCH_VALUE_OPTIONS = frozenset({
    "-n", "--agent", "--before", "--chat", "--color", "--exclude-project",
    "--max", "--model", "--no-who", "--project", "--session", "--since",
    "--sort", "--until", "--who",
})
_SEARCH_VALUE_CHOICES = {
    "--color": frozenset({"auto", "always", "never"}),
    "--sort": frozenset({"score", "time", "position"}),
}
_WINDOWS_SHELL_AMBIGUOUS = re.compile(
    r"[&|<>^%!`$;'\"(){}\[\]@,#\r\n]")
_WINDOWS_HANDLE_ARG = re.compile(r"@[A-Za-z0-9._~:-]+\Z")


def render_cli_argv(
        argv: Sequence[object], *, windows: bool | None = None,
) -> str | None:
    """Render a copyable command, or refuse text whose shell is ambiguous."""
    values = [str(value) for value in argv]
    if any("\x00" in value or terminal_safe(value) != value for value in values):
        return None
    windows = os.name == "nt" if windows is None else windows
    if windows:
        if any(
                _WINDOWS_SHELL_AMBIGUOUS.search(value)
                and _WINDOWS_HANDLE_ARG.fullmatch(value) is None
                for value in values):
            return None
        return " ".join(
            f'"{value}"' if value.startswith("@")
            else subprocess.list2cmdline([value])
            for value in values)
    return shlex.join(values)


def _search_shaped_argv(argv: Sequence[str]) -> bool:
    if not argv:
        return True
    index = 0
    saw_search_input = False
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return index + 1 < len(argv)
        option, separator, attached = token.partition("=")
        if option in _SEARCH_FLAG_OPTIONS:
            if separator:
                return False
            saw_search_input = True
            index += 1
            continue
        if option in _SEARCH_VALUE_OPTIONS:
            if separator:
                value = attached
            elif index + 1 < len(argv):
                index += 1
                value = argv[index]
                if value.startswith("-"):
                    return False
            else:
                return False
            choices = _SEARCH_VALUE_CHOICES.get(option)
            if not value or (choices is not None and value not in choices):
                return False
            if option in {"-n", "--max"}:
                try:
                    if int(value) < 0:
                        return False
                except ValueError:
                    return False
            saw_search_input = True
            index += 1
            continue
        if token.startswith("-"):
            return False
        saw_search_input = True
        index += 1
    return saw_search_input


def reserved_search_hint(
        word: str | None, argv: Sequence[str] | None,
) -> str | None:
    """Return the explicit-search escape hatch only when it preserves argv."""
    if word not in _RESERVED_SEARCH_WORDS or word == "search":
        return None
    values = list(argv or ())
    if not _search_shaped_argv(values):
        return None
    command = render_cli_argv(["agrep", "search", word, *values])
    if command is None:
        return None
    return f'to search for the word "{word}": {command}'


def argument_error(
        prog: str, message: object, *, argv: Sequence[str] | None = None,
        search_word: str | None = None,
) -> int:
    """Emit one terminal-safe usage failure without argparse's help dump."""
    line = f"{terminal_safe(prog)}: {terminal_safe(message)}"
    hint = reserved_search_hint(search_word, argv)
    if hint:
        line += f"; {hint}"
    print(line, file=sys.stderr)
    return 2


def _stable_argparse_error(message: str) -> str:
    """Normalize simple developer-owned choices across Python releases."""
    prefix, marker, suffix = message.rpartition("(choose from ")
    if not marker or not suffix.endswith(")"):
        return message
    choices = suffix[:-1].split(", ")
    if not choices or any(
            re.fullmatch(r"'[A-Za-z0-9_.:/+-]+'", choice) is None
            for choice in choices):
        return message
    return prefix + marker + ", ".join(choice[1:-1] for choice in choices) + ")"


class ArgumentParser(argparse.ArgumentParser):
    """argparse help with terse, one-line failures for the product CLI."""

    def __init__(self, *args, search_word: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        inferred = self.prog.rsplit(" ", 1)[-1]
        self._agrep_search_word = search_word or (
            inferred if inferred in _RESERVED_SEARCH_WORDS else None)
        self._agrep_argv: list[str] | None = None

    def parse_known_args(self, args=None, namespace=None):
        values = sys.argv[1:] if args is None else args
        self._agrep_argv = list(values)
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> None:
        message = _stable_argparse_error(message)
        line = f"{terminal_safe(self.prog)}: {terminal_safe(message)}"
        hint = reserved_search_hint(
            self._agrep_search_word, self._agrep_argv)
        if hint:
            line += f"; {hint}"
        self.exit(2, line + "\n")


def redact_live_ciphertext(value: object) -> str:
    """Keep live context readable without presenting opaque encrypted blobs."""
    return _LIVE_CIPHERTEXT_TOKEN.sub(
        "[encrypted payload]", "" if value is None else str(value))


def _redact_credential(match: re.Match[str]) -> str:
    value = match.group(2)
    quote = value[:1] if value[:1] in {"'", '"'} else ""
    return f"{match.group(1)}{quote}[redacted]{quote}"


def _redact_labeled_secret(match: re.Match[str]) -> str:
    value = match.group(2)
    quote = value[:1] if value[:1] in {"'", '"'} else ""
    if not quote and value.lower() in _LIVE_SAFE_FIELD_VALUES:
        return match.group(0)
    return _redact_credential(match)


def _redact_bearer_secret(match: re.Match[str]) -> str:
    value = match.group(2)
    if value.isalpha() and len(value) < 20:
        return match.group(0)
    return f"{match.group(1)}[redacted]"


def _redact_url_userinfo(match: re.Match[str]) -> str:
    if ":" not in match.group(2):
        return match.group(0)
    return f"{match.group(1)}[redacted]@"


def redact_live_secrets(value: object) -> str:
    """Keep live activity useful without echoing capabilities or credentials."""
    text = redact_live_ciphertext(value)
    text = _LIVE_BOOTSTRAP_CAPABILITY.sub(_redact_credential, text)
    text = _LIVE_BOOTSTRAP_HEADER.sub(_redact_credential, text)
    text = _LIVE_AUTHORIZATION_HEADER.sub(_redact_credential, text)
    text = _LIVE_BEARER_TOKEN.sub(_redact_bearer_secret, text)
    text = _LIVE_AGREP_COOKIE.sub(r"\1[redacted]", text)
    text = _LIVE_URL_USERINFO.sub(_redact_url_userinfo, text)
    text = _LIVE_SECRET_FLAG.sub(_redact_credential, text)
    return _LIVE_SECRET_FIELD.sub(_redact_labeled_secret, text)


def setting_default(name: str, fallback: object = _MISSING) -> object:
    spec = SETTINGS.get(name)
    if spec is None:
        return None if fallback is _MISSING else fallback
    if isinstance(spec.default, Mapping):
        return dict(spec.default)
    return spec.default


class SpeakerSpec(NamedTuple):
    name: str
    display: str
    glyph: str
    semantic_default: bool


SPEAKER_SPECS = (
    SpeakerSpec("user", "you", "›", True),
    SpeakerSpec("subagent", "subagent", "⇒", True),
    SpeakerSpec("agent", "agent", "→", True),
    SpeakerSpec("tool", "tool", "⚙", True),
    SpeakerSpec("control", "control", "›", False),
    SpeakerSpec("synthetic", "synthetic", "›", False),
    SpeakerSpec("recap", "recap", "›", False),
    SpeakerSpec("harness", "harness", "›", False),
)
SPEAKERS: Mapping[str, SpeakerSpec] = MappingProxyType(
    {spec.name: spec for spec in SPEAKER_SPECS})
SPEAKER_CHOICES = tuple(spec.name for spec in SPEAKER_SPECS)
SEARCH_SPEAKER_CHOICES = SPEAKER_CHOICES
RECALL_SPEAKER_CHOICES = SPEAKER_CHOICES
AROUND_SPEAKER_CHOICES = ("you", *SPEAKER_CHOICES)
# The one alias table. `you` is what speaker_legend() prints for `user`, so a
# reader who filters by what the legend taught must not be refused.
SPEAKER_ALIASES: Mapping[str, str] = MappingProxyType({"you": "user"})
SEMANTIC_DEFAULT_EXCLUDED_ROLES = frozenset(
    spec.name for spec in SPEAKER_SPECS if not spec.semantic_default)


class SpeakerFilter(NamedTuple):
    """A parsed --who/--no-who selection over the owned speaker vocabulary.

    ``include`` None admits every speaker; ``exclude`` is a predicate, never
    a complement set, so a role the vocabulary does not know (`unknown`)
    survives an exclusion it is not named in."""
    include: tuple[str, ...] | None
    exclude: tuple[str, ...]

    def admits(self, who: str) -> bool:
        actual = "user" if who == "you" else who
        if actual in self.exclude:
            return False
        return self.include is None or actual in self.include


def parse_speaker_list(value: str, choices: Sequence[str]) -> tuple[str, ...]:
    """One comma-separated speaker list against the owned choices; the error
    names the bad token and the whole vocabulary. `you` is the display name
    the legend teaches, so every command that takes a speaker accepts it."""
    names: list[str] = []
    for token in str(value).split(","):
        token = SPEAKER_ALIASES.get(token.strip(), token.strip())
        if not token:
            continue
        if token not in choices:
            raise ValueError(f"unknown speaker {token!r} "
                             f"(choose from {', '.join(choices)})")
        if token not in names:
            names.append(token)
    if not names:
        raise ValueError(
            f"empty speaker list (choose from {', '.join(choices)})")
    return tuple(names)


def speaker_filter(include: str | None, exclude: str | None,
                   choices: Sequence[str]) -> "str | SpeakerFilter | None":
    """Resolve --who/--no-who into the engine filter shape: None (unfiltered),
    one speaker string (the engines' exact fast path), or a SpeakerFilter."""
    inc = parse_speaker_list(include, choices) if include else None
    exc = parse_speaker_list(exclude, choices) if exclude else ()
    if inc is not None and exc:
        raise ValueError("--who and --no-who are mutually exclusive")
    if inc is not None and len(inc) == 1:
        return inc[0]
    if inc is None and not exc:
        return None
    return SpeakerFilter(inc, exc)


def speaker_filter_admits(who: object, actual: str) -> bool:
    """Row admission for every engine shape a who filter can take."""
    if who is None:
        return True
    normalized = "user" if actual == "you" else actual
    if isinstance(who, str):
        return normalized == who
    return who.admits(normalized)


class FilterSpec(NamedTuple):
    """One value-taking filter flag: where a legal value comes from, and what a
    run that dropped the filter would silently become. No filter flag has a
    legitimate empty value - every domain below is non-empty, and the way to
    run unfiltered is to omit the flag, which is already spelled that way."""
    flag: str
    dest: str
    domain: str | Callable[[], str]
    empty_effect: str

    def vocabulary(self) -> str:
        return self.domain() if callable(self.domain) else self.domain


def _agent_vocabulary() -> str:
    # derived from the pinned manifest, never mirrored (docs/ARCHITECTURE.md)
    from hookless.registry import ADAPTER_NAMES
    return ", ".join(ADAPTER_NAMES)


def _speaker_vocabulary() -> str:
    return ", ".join(SPEAKER_CHOICES)


_WHEN_DOMAIN = "7d / 24h / 2w / 30m, or 2026-06-01"
_PROJECT_DOMAIN = "a substring of a chat's stored project label"
FILTER_SPECS = (
    FilterSpec("--agent", "agent", _agent_vocabulary, "match every agent"),
    FilterSpec("--project", "project", _PROJECT_DOMAIN, "match every project"),
    FilterSpec("--exclude-project", "exclude_project", _PROJECT_DOMAIN,
               "exclude nothing"),
    FilterSpec("--model", "model",
               "an exact model name; --soft substring-matches",
               "match every model"),
    FilterSpec("--who", "who", _speaker_vocabulary, "include every speaker"),
    FilterSpec("--no-who", "no_who", _speaker_vocabulary, "exclude no speaker"),
    FilterSpec("--chat", "chat",
               "an 8-char id prefix (as shown) or a full session uuid",
               "match every chat"),
    FilterSpec("--since", "since", _WHEN_DOMAIN, "match the whole history"),
    FilterSpec("--until", "until", _WHEN_DOMAIN, "match the whole history"),
)
FILTERS_BY_FLAG: Mapping[str, FilterSpec] = MappingProxyType(
    {spec.flag: spec for spec in FILTER_SPECS})


def blank_value_notice(flag: str, vocabulary: str, effect: str) -> str:
    """The one refusal a supplied-but-blank value earns anywhere in the CLI:
    the flag, what a legal value is, and the run the blank would silently
    become. Blank is never a spelling of "omit me" - omitting already is."""
    return (f"{flag} needs a value ({vocabulary}) - an empty "
            f"value would silently {effect}")


def blank_value_error(flag: str, value: object, vocabulary: str,
                      effect: str) -> str | None:
    """That refusal for one option, or None if the caller gave it a value."""
    if isinstance(value, str) and not value.strip():
        return blank_value_notice(flag, vocabulary, effect)
    return None


def restore_residue_error(asides: Sequence[object]) -> str:
    """A restore that published but left superseded sidecar copies behind must
    fail its verdict and name the leftovers, never claim a clean restore."""
    listed = ", ".join(str(path) for path in asides)
    return ("the file was restored, but superseded sidecar cleanup failed - "
            f"stale sidecar copies remain at: {listed}")


def restore_rollback_residue_line(sidecar: object, aside: object) -> str:
    """A failed restore whose rollback also failed strands the live store's
    sidecar under a parking name; say where its bytes went."""
    return (f"restore rollback could not put back {sidecar}; "
            f"the live store's sidecar bytes are parked at {aside}")


def restore_locked_sidecar_error(sidecar: object) -> str:
    """A live store cannot be atomically replaced while Windows locks it."""
    return (f"{sidecar} is in use; close the application using this history "
            "store, then rerun the same `agrep restore` command")


def restore_unused_parking_residue_line(aside: object) -> str:
    """Name an empty reservation only when even its cleanup was refused."""
    return f"restore could not remove unused sidecar parking file: {aside}"


def empty_filter_notice(spec: FilterSpec) -> str:
    """The one refusal an empty filter value gets, naming the flag, its
    vocabulary, and the corpus-wide answer the caller would otherwise report
    as scoped."""
    return blank_value_notice(spec.flag, spec.vocabulary(), spec.empty_effect)


def filter_value_error(args: object) -> str | None:
    """Scan a parsed namespace for a filter flag present with a blank value.

    Whitespace-only counts: both shapes come from templating `--agent "$X"`
    off a variable that came back empty, and both would answer corpus-wide."""
    for spec in FILTER_SPECS:
        value = getattr(args, spec.dest, None)
        if isinstance(value, str) and not value.strip():
            return empty_filter_notice(spec)
    return None


class DimensionSpec(NamedTuple):
    """One filter whose domain the index can enumerate, so a zero under it can
    be told apart from a coverage gap. ``match`` mirrors the engine predicate
    in corpusdb._filter_sql - a disclosure that used different semantics from
    the search would name the wrong dimension as empty.

    --chat is absent on purpose: it resolves its value against the indexed
    sessions before any search runs, so an unindexed chat is already an error
    (search._resolve_chat), which is strictly more than a disclosure."""
    flag: str
    dimension: str
    label: str
    match: str


COVERAGE_DIMENSIONS = (
    DimensionSpec("--agent", "agent", "agents", "substring"),
    DimensionSpec("--project", "project", "projects", "substring"),
    DimensionSpec("--model", "model", "models", "exact"),
    DimensionSpec("--who", "who", "speakers", "exact"),
)
COVERAGE_DIMENSIONS_BY_FLAG: Mapping[str, DimensionSpec] = MappingProxyType(
    {spec.flag: spec for spec in COVERAGE_DIMENSIONS})
_KNOWN_VALUES_SHOWN = 6


def dimension_selects_nothing(spec: DimensionSpec, value: str,
                              known: Sequence[str], *,
                              soft: bool = False) -> bool:
    """Would the engine's own predicate admit any indexed value? --soft turns
    --model's equality into the substring test the search actually runs."""
    needle = value.casefold()
    substring = spec.match == "substring" or soft
    return not any(needle in known_value.casefold() if substring
                   else needle == known_value.casefold()
                   for known_value in known)


def _nearest_values(value: str, known: Sequence[str]) -> tuple[str, ...]:
    """The indexed values a typo most likely meant, closest first."""
    from difflib import SequenceMatcher
    folded = value.casefold()
    ranked = sorted(known, key=lambda item: (
        -SequenceMatcher(None, folded, item.casefold()).ratio(), item))
    return tuple(ranked[:_KNOWN_VALUES_SHOWN])


def empty_dimension_disclosure(spec: DimensionSpec, value: str,
                               known: Sequence[str]) -> dict:
    """One filter that selects a dimension the index holds no value for. The
    caller asked a legal question, so this is never an error - but its zero is
    an artifact of what was indexed, and only this record says so."""
    known = tuple(known)
    listed = (tuple(sorted(known)) if len(known) <= _KNOWN_VALUES_SHOWN
              else _nearest_values(value, known))
    return {
        "flag": spec.flag,
        "dimension": spec.dimension,
        "value": value,
        "indexed_values": len(known),
        "known": list(listed),
        "known_complete": len(listed) == len(known),
    }


def empty_dimension_line(disclosure: Mapping) -> str:
    """The prose of the same record: what was asked for, that the zero is a
    coverage gap rather than an absence, and what the index does hold."""
    spec = COVERAGE_DIMENSIONS_BY_FLAG[str(disclosure["flag"])]
    known = [terminal_safe(value) for value in disclosure["known"]]
    value = terminal_safe(disclosure["value"])
    total = int(disclosure["indexed_values"])
    if not known:
        held = f"the index holds no {spec.label} at all"
    elif disclosure["known_complete"]:
        held = f"indexed {spec.label}: {', '.join(known)}"
    else:
        held = f"{total} indexed {spec.label}, closest: {', '.join(known)}"
    return (f"{spec.flag} {value} isn't in the index "
            f"- {held}")


def unproven_coverage_reason(unproven: Sequence[str],
                             *, gaps: bool = False) -> str | None:
    """Why some flags went unchecked - said about those flags only. A reason
    that speaks for the whole run contradicts the proven gaps beside it, which
    is the sentence a reader believes over the structured field."""
    flags = [str(flag) for flag in unproven if str(flag)]
    if not flags:
        return None
    named = ", ".join(flags)
    scope = ("" if not gaps else
             " for it" if len(flags) == 1 else " for them")
    tail = ("" if not gaps else
            "; the gaps listed here were proven independently")
    return (f"the indexed values behind {named} are unavailable, so no "
            f"coverage gap is claimed{scope}{tail}")


def filter_coverage_disclosure(
        empty: Sequence[Mapping], *, checked: bool,
        reason: str | None = None) -> dict:
    """What a machine surface knows about coverage behind its zero. Always
    emitted: a caller must never read an absent field as "nothing empty",
    which is exactly the inference an unexplained zero invites."""
    out: dict = {"empty_dimensions": [dict(item) for item in empty],
                 "checked": bool(checked)}
    if not checked and reason:
        out["reason"] = reason
    return out


def inverted_window_error(since: str, until: str) -> str:
    """--since/--until name the half-open interval [since, until). With until
    at or before since that interval holds no instant at all, so every corpus
    answers zero - a fact the tool holds before it touches the index, and the
    one shape a plain miss can never be told from."""
    return (f"empty time window: --since {since} is at or after "
            f"--until {until}, so [since, until) selects no time at all "
            f"- swap them for the range between them")


def window_bounds_error(since: str | None, since_ms: int | None,
                        until: str | None, until_ms: int | None) -> str | None:
    """The one window check every timed surface runs on parsed bounds."""
    if since is None or until is None:
        return None
    if since_ms is None or until_ms is None or until_ms > since_ms:
        return None
    return inverted_window_error(since, until)


class OptionRef(NamedTuple):
    """One option as the gates address it: how it is spelled, the namespace
    field it lands in, and the value that means the caller never supplied it.

    Presence is `value != absent`, never truthiness: `--more ""` is a supplied
    --more, and a guard reading it as absent runs a different query than the
    caller asked for."""
    flag: str
    dest: str
    absent: object = False
    renders: str = ""

    def supplied(self, args: object) -> bool:
        supplied = getattr(args, "_agrep_supplied_options", None)
        tracked = getattr(args, "_agrep_tracked_options", ())
        if supplied is not None and self.dest in tracked:
            return self.dest in supplied
        return getattr(args, self.dest, self.absent) != self.absent


def parse_args_with_presence(
        parser: argparse.ArgumentParser,
        argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    """Parse once while retaining which optional actions actually fired."""
    # append actions copy-and-extend their default, which a sentinel object
    # cannot survive; they stay untracked (value != default already works)
    actions = [action for action in parser._actions
               if action.option_strings
               and not isinstance(action, argparse._AppendAction)]
    originals = [(action, action.default) for action in actions]
    sentinels: dict[str, list[object]] = {}
    defaults: dict[str, object] = {}
    for action, default in originals:
        defaults.setdefault(action.dest, default)
        sentinel = object()
        sentinels.setdefault(action.dest, []).append(sentinel)
        action.default = sentinel
    try:
        parsed = parser.parse_args(argv)
    finally:
        for action, default in originals:
            action.default = default
    supplied = set()
    for dest, markers in sentinels.items():
        value = getattr(parsed, dest, _MISSING)
        if not any(value is marker for marker in markers):
            supplied.add(dest)
            continue
        default = defaults[dest]
        if default == argparse.SUPPRESS:
            delattr(parsed, dest)
        else:
            setattr(parsed, dest, default)
    setattr(parsed, "_agrep_supplied_options", frozenset(supplied))
    setattr(parsed, "_agrep_tracked_options", frozenset(sentinels))
    return parsed


class OptionGate(NamedTuple):
    """One option another option cancels: supplying it under `blocked_by`, or
    without all of `needs`, changes nothing about the run. Either the gate
    refuses or the option was dropped in silence; there is no third state."""
    option: OptionRef
    blocked_by: tuple[OptionRef, ...] = ()
    needs: tuple[OptionRef, ...] = ()


COUNT_OPTION = OptionRef("-c", "count", renders="prints one number")
COUNT_BY_TIER_OPTION = OptionRef("--count-by-tier", "count_by_tier",
                                 renders="prints one number per tier")
AGGREGATE_OPTIONS = (COUNT_OPTION, COUNT_BY_TIER_OPTION)
JSON_OPTION = OptionRef("--json", "json",
                        renders="already writes one machine row per hit")
CHATS_OPTION = OptionRef("-l", "chats",
                         renders="lists matching chats, one line each")
FLAT_OPTION = OptionRef("--flat", "flat",
                        renders="writes one tab-separated row per hit")
MODEL_OPTION = OptionRef("--model", "model", None)
PROBE_OPTION = OptionRef("--probe", "probe", renders="prints one pointer line")
SOFT_GATE = OptionGate(OptionRef("--soft", "model_soft"), needs=(MODEL_OPTION,))

SEARCH_OPTION_GATES = (
    OptionGate(CHATS_OPTION, AGGREGATE_OPTIONS),
    OptionGate(JSON_OPTION, AGGREGATE_OPTIONS),
    # -l keeps its own row shape under --json, so only --flat is blocked:
    # supplying both would drop --flat's shape while it still steered policy
    OptionGate(FLAT_OPTION, AGGREGATE_OPTIONS + (JSON_OPTION, CHATS_OPTION)),
    OptionGate(OptionRef("--classic", "classic"),
               AGGREGATE_OPTIONS + (JSON_OPTION, FLAT_OPTION, CHATS_OPTION)),
    OptionGate(OptionRef("-n", "max", None), AGGREGATE_OPTIONS),
    OptionGate(OptionRef("--sort", "sort", "score"), AGGREGATE_OPTIONS),
    SOFT_GATE,
)
RECALL_OPTION_GATES = (
    OptionGate(OptionRef("--hits", "hits", None), (PROBE_OPTION,)),
    SOFT_GATE,
)
RESUME_OPTION_GATES = (
    OptionGate(OptionRef("-l", "list"),
               (OptionRef("an id", "id", None, "names the one chat to resume"),)),
)
DOCTOR_JSON_OPTION = OptionRef("--json", "json",
                               renders="prints one machine report")
DOCTOR_SETUP_OPTION = OptionRef("--setup", "setup",
                                renders="runs guided setup")
DOCTOR_OPTION_GATES = (
    OptionGate(OptionRef("--fix", "fix",
                         renders="repairs what the report flags"),
               (DOCTOR_JSON_OPTION, DOCTOR_SETUP_OPTION)),
    OptionGate(DOCTOR_SETUP_OPTION, (DOCTOR_JSON_OPTION,)),
    OptionGate(OptionRef("--deep", "deep"), (DOCTOR_SETUP_OPTION,)),
)
TAIL_OPTION_GATES = (
    # a snapshot's per-session recents never contain done/queued (state-only
    # events), so an event filter cannot be honored there - refuse, never drop
    OptionGate(OptionRef("--events", "events", "done"),
               (OptionRef("--snapshot", "snapshot",
                          renders="prints current session state once, "
                                  "not an event stream"),)),
)
AROUND_OPTION_GATES = (
    OptionGate(OptionRef("--max-chars", "max_chars", 4000),
               (OptionRef("--full", "full",
                          renders="uncaps indexed message text"),)),
    OptionGate(OptionRef("--tool-output", "tool_output", 0),
               (OptionRef("--no-tools", "no_tools",
                          renders="hides tool-call lines"),)),
)


def option_gate_error(args: object,
                      gates: Sequence[OptionGate]) -> str | None:
    """The one refusal a supplied-but-inert option gets, naming both flags."""
    for gate in gates:
        if not gate.option.supplied(args):
            continue
        for blocker in gate.blocked_by:
            if blocker.supplied(args):
                return (f"{gate.option.flag} cannot be combined with "
                        f"{blocker.flag}, which {blocker.renders}")
        for needed in gate.needs:
            if not needed.supplied(args):
                return f"{gate.option.flag} has no effect without {needed.flag}"
    return None


def doctor_action_conflict(argv: Sequence[str]) -> str | None:
    """Doctor's refusal for two actions supplied in one run (gate law: the
    losing action must be refused by name, never dropped in silence)."""
    supplied = argparse.Namespace(
        json="--json" in argv, fix="--fix" in argv,
        setup="--setup" in argv, deep="--deep" in argv)
    return option_gate_error(supplied, DOCTOR_OPTION_GATES)


def meta_exclusion_notice(dropped: int) -> str:
    """The one --no-meta disclosure line (fact and flag, law 5)."""
    return f"excluded {count_noun(dropped, '~meta row')} (--no-meta)"


def meta_filter_notice(dropped: int, retained: int) -> str:
    """Disclose the sole-evidence exception without turning it into a miss."""
    if retained and dropped:
        return (f"excluded {count_noun(dropped, '~meta row')}; retained "
                f"{count_noun(retained, 'sole ~meta row')} (--no-meta)")
    if retained:
        return (f"only ~meta evidence; retained "
                f"{count_noun(retained, 'best row')} (--no-meta)")
    return meta_exclusion_notice(dropped)


def speaker_glyph(name: str | None) -> str:
    if not name:
        return " "
    return SPEAKERS.get(name, SPEAKERS["user"]).glyph


def speaker_legend() -> str:
    order = ("user", "agent", "tool", "subagent")
    return "  ".join(
        f"{SPEAKERS[name].glyph} {SPEAKERS[name].display}" for name in order)


def around_speaker_matches(requested: str | None, actual: str) -> bool:
    if requested is None:
        return True
    expected = "user" if requested == "you" else requested
    observed = "user" if actual == "you" else actual
    return observed == expected


class Glyphs(NamedTuple):
    tool: str
    success: str
    failure: str
    subagent_start: str
    subagent_result: str
    done: str
    queued: str
    child_result: str
    working: str
    idle: str
    prompt: str
    reply: str
    unknown: str


GLYPHS = Glyphs("⚙", "✓", "✗", "⇒", "⇐", "■", "⧗", "↳",
                "●", "○", ">", "<", "·")
EVENT_GLYPHS: Mapping[str, str] = MappingProxyType({
    "user": GLYPHS.prompt,
    "reply": GLYPHS.reply,
    "tool": GLYPHS.tool,
    "tool_done": GLYPHS.success,
    "done": GLYPHS.done,
    "queued": GLYPHS.queued,
    "subagent_result": GLYPHS.child_result,
})
STATUS_GLYPHS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "ok": ("ok ", "g"),
    "MISSING": ("MISS", "e"),
    "--": ("-- ", "d"),
    "warn": ("!! ", "y"),
})


PALETTE: Mapping[str, str] = MappingProxyType({
    "m": "\033[1;31m",
    "bad": "\033[1;31m",
    "hd": "\033[1;36m",
    "d": "\033[2m",
    "a": "\033[36m",
    "g": "\033[32m",
    "y": "\033[33m",
    "e": "\033[31m",
    "n": "\033[1;33m",
    "bold": "\033[1m",
    "r": "\033[0m",
})


def cell_clusters(text: str) -> Iterator[tuple[str, int]]:
    cluster, width, join_next = "", 0, False
    for char in text:
        code = ord(char)
        attached = (
            unicodedata.category(char).startswith("M")
            or 0x1F3FB <= code <= 0x1F3FF
            or 0xE0020 <= code <= 0xE007F
        )
        if char == "\u200d":
            cluster += char
            join_next = True
            continue
        if attached:
            cluster += char
            if char in ("\ufe0f", "\u20e3"):
                width = max(width, 2)
            continue
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if not cluster:
            cluster, width = char, char_width
        elif join_next:
            cluster += char
            width = max(width, char_width)
            join_next = False
        else:
            yield cluster, width
            cluster, width = char, char_width
    if cluster:
        yield cluster, width


def cell_width(text: str) -> int:
    return sum(width for _, width in cell_clusters(text))


def pad_cells(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_width(text))


def truncate_cells(text: str, width: int, ellipsis: str = "…") -> str:
    if cell_width(text) <= width:
        return text
    available = max(0, width - cell_width(ellipsis))
    output: list[str] = []
    used = 0
    for cluster, step in cell_clusters(text):
        if used + step > available:
            break
        output.append(cluster)
        used += step
    return "".join(output) + ellipsis


class FreshnessPolicy(NamedTuple):
    failure_threshold: int
    last_good_index_tail: str


class FreshnessFailure(NamedTuple):
    code: str
    reason: str
    consecutive_failures: int = 0


FRESHNESS_POLICY = FreshnessPolicy(
    failure_threshold=3,
    last_good_index_tail="searches serve the last good index",
)


# What an explicit `agrep index` may claim when it returns before the derived
# FTS build finishes: the corpus IS published and searched, so name the lane
# that serves and the one coverage it does not carry yet.
EXPLICIT_INDEX_HANDOFF_LINE = (
    "indexed and searchable now; the search database is still building in the "
    "background - until it lands searches scan the transcripts directly "
    "(slower, and tool output is not in results yet)")


class IndexBuildOutcome(Enum):
    """What a run actually did about the derived search database.

    A build handed to an owner that will run it and a build nothing is going
    to run are different facts; giving them one name is how a command comes to
    report success for work that will never happen."""
    CURRENT = "current"          # already serves this corpus; nothing to build
    DELEGATED = "delegated"      # queued to the indexer that will build it
    BUILT = "built"              # built here, before returning
    UNSUPPORTED = "unsupported"  # this SQLite has no trigram FTS lane to build
    FAILED = "failed"            # the build ran here and failed
    BLOCKED = "blocked"          # no owner is left to build it


# Why nothing can build it, in the reader's terms - keyed by the blocking
# condition the spawn refused on.
_INDEX_BUILD_BLOCKED_CAUSES = MappingProxyType({
    "data-readonly": "this data directory is protected from writes",
    "removal": "an agrep removal is finishing",
    "foreign-owner": "another agrep version owns this index",
    "blocked-owner": "another process holds the background indexer",
})
_INDEX_BUILD_BLOCKED_CAUSE_ANY = "the background indexer cannot be started"
# The consequence both refusals share, and the reason the handoff line is
# load-bearing: the direct scan serves, without tool output.
_INDEX_BUILD_SCAN_TAIL = (
    "searches scan the transcripts directly (slower, and tool output is not "
    "in results yet)")

# One fact behind every tools-gap surface: while the derived FTS build is
# outside this process, the scan lane serves prose only. The lane records the
# exclusion on its result; every renderer below speaks from that record.
SCAN_TOOLS_PENDING_LINE = "tool output isn't indexed yet - prose only"
TOOLS_PENDING_ERROR_CODE = "tools-pending"
TOOL_QUERY_PENDING_LINE = (
    "tool output is not indexed yet - the search database is still building "
    "in the background; retry once it lands")


def tools_pending_zero_line(mode: str) -> str:
    """A zero from a lane that excluded tool rows names the corpus it
    actually searched, never the full indexed count beside a dropped lane."""
    label = mode if mode in ("keyword", "word", "regex") else "keyword"
    return (f"{label}: 0 matching rows in prose - "
            "tool output isn't indexed yet")


def count_floor_tools_pending(total: int) -> str:
    """Why a -c total is a floor when the cause is the excluded tool lane."""
    return (f">={max(0, int(total)):,} counts prose only - "
            "tool output is not indexed yet")


def index_build_line(
        outcome: "IndexBuildOutcome", cause: str = "", *,
        cli: str = "agrep") -> str:
    """The one sentence `agrep index` may say about the search database.

    Rendered from the outcome the run produced, so no call site can announce a
    background build that nothing was asked to run. "" is silence: a db that
    already serves this corpus, or one this process built before returning."""
    if outcome is IndexBuildOutcome.DELEGATED:
        return EXPLICIT_INDEX_HANDOFF_LINE
    if outcome is IndexBuildOutcome.BLOCKED:
        why = _INDEX_BUILD_BLOCKED_CAUSES.get(
            str(cause or ""), _INDEX_BUILD_BLOCKED_CAUSE_ANY)
        return (f"indexed, but the search database was not built and nothing "
                f"is building it - {why}; until that clears "
                f"{_INDEX_BUILD_SCAN_TAIL}: `{cli} doctor`")
    if outcome is IndexBuildOutcome.FAILED:
        return (f"indexed, but the search database could not be rebuilt - "
                f"{_INDEX_BUILD_SCAN_TAIL}: `{cli} doctor`")
    return ""


def index_build_succeeded(outcome: "IndexBuildOutcome") -> bool:
    """Exit status for an explicit index: 0 only when the search database
    serves this corpus, cannot exist here, or is queued to an owner that
    will build it."""
    return outcome not in (
        IndexBuildOutcome.FAILED, IndexBuildOutcome.BLOCKED)


# The two lines a background handoff cannot own: a build this process is about
# to run in the foreground, and the daemon start that made it necessary.
BACKGROUND_INDEX_BUILD_LINE = (
    "building the search index in the background - first searches use the "
    "direct scan until it lands…")
INLINE_INDEX_BUILD_LINE = (
    "background indexer failed to start; the first search-index build scans "
    "your full history and can take several minutes, so it is running inline…")


class HostBlock(NamedTuple):
    """A condition on the reader's machine that no rebuild can clear.

    Law 2's whole class: the tool owns every derived byte it writes, so the
    only thing left to ask about is the volume underneath them. Nothing here
    names an agrep command, because typing one is never the fix."""
    code: str
    ask: str


# Ordered; the first marker found in a failed write's text wins. The strings
# are what the OS, Rust's io::Error and Python's OSError actually print.
_HOST_BLOCK_MARKERS = (
    ("out-of-space", (
        "no space left on device", "os error 28", "errno 28",
        # EDQUOT is 69 on the BSDs (macOS) and 122 on Linux
        "disk quota exceeded", "os error 69", "errno 69",
        "os error 122", "errno 122",
        "not enough space", "there is not enough space on the disk")),
    ("read-only", (
        "read-only file system", "read-only filesystem",
        "os error 30", "errno 30")),
)

# Each ask completes "agrep needs ... to rebuild its index".
HOST_BLOCKS = MappingProxyType({
    "out-of-space": HostBlock("out-of-space", "free space on this volume"),
    "read-only": HostBlock("read-only", "this volume remounted read-write"),
})

# A rebuild stages a second copy of the derived set beside the live one before
# the atomic rename, so the volume needs about that much slack, never less.
REBUILD_HEADROOM_FLOOR_BYTES = 64 * 1024 * 1024

# Law 6: rebuilds that merely have not finished are not news. Two that ran and
# left the damage standing are the tool having exhausted what it owns.
REPAIR_ESCALATE_AFTER = 2


def host_block(text: object) -> HostBlock | None:
    """Name the host condition behind a failed write, or None when the failure
    is agrep's own to repair. The only classification a failure text earns."""
    haystack = str(text or "").lower()
    if not haystack:
        return None
    for code, markers in _HOST_BLOCK_MARKERS:
        if any(marker in haystack for marker in markers):
            return HOST_BLOCKS[code]
    return None


def rebuild_shortfall_bytes(free_bytes: object, derived_bytes: object) -> int:
    """How much more room a rebuild needs, or 0 when it already fits."""
    try:
        free, derived = int(free_bytes), int(derived_bytes)
    except (TypeError, ValueError):
        return 0
    return max(0, max(REBUILD_HEADROOM_FLOOR_BYTES, derived) - max(0, free))


def _mib(byte_count: int) -> str:
    mib = max(1, round(byte_count / (1024 * 1024)))
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib} MiB"


def host_block_line(block: HostBlock | None, shortfall_bytes: object = 0) -> str:
    """The one sentence the owner's box should have shown her.

    Everything else on that screen was agrep reporting its own derived data
    back to the person who cannot repair it; this is the remainder. No command
    is offered, because there is nothing for her to type."""
    if block is None:
        return ""
    try:
        shortfall = max(0, int(shortfall_bytes))
    except (TypeError, ValueError):
        shortfall = 0
    room = (f"about {_mib(shortfall)} free"
            if block.code == "out-of-space" and shortfall else block.ask)
    return (f"agrep needs {room} to rebuild its index; "
            f"{FRESHNESS_POLICY.last_good_index_tail} until then")


class StatusAdvice(NamedTuple):
    """One broken thing, in the reader's terms: what it costs them, and the
    exact command that ends it.

    The failure codes below are the indexer's own vocabulary - owner states,
    ledgers, censuses, signals. None of that is the reader's situation; the
    consequence is. This table is the only place the translation happens, so
    status, doctor and search cannot spell one condition three ways."""
    cost: str
    command: str = ""  # bare verb; the renderer prefixes the invocation name


# Keyed by FreshnessFailure.code. None means the condition clears itself and
# earns no line (law 1); an unlisted code falls back to _INDEXING_ADVICE_ANY.
INDEXING_ADVICE = MappingProxyType({
    "blocked-owner": StatusAdvice(
        "new chats are not being indexed - another agrep version holds the "
        "indexer here", "index"),
    "derived-store-owner": StatusAdvice(
        "new chats are not being indexed - another agrep version built this "
        "index", "index --full"),
    "daemon-unresponsive": StatusAdvice(
        "background indexing stopped - new chats are not searchable yet",
        "index"),
    "daemon-unverifiable": StatusAdvice(
        "background indexing stopped - new chats are not searchable yet",
        "index"),
    "missing-ingest-binary": StatusAdvice(
        "agrep is installed incompletely, so nothing new can be indexed",
        "setup"),
    "consecutive-failures": StatusAdvice(
        "indexing keeps failing - new chats are not searchable", "doctor"),
    "inline-refresh-failed": StatusAdvice(
        "the search index could not be rebuilt - new chats are not searchable",
        "index"),
    "freshness-ledger-unavailable": StatusAdvice(
        "agrep cannot tell whether the index is current", "index"),
    "source-unreadable": StatusAdvice(
        "some chats could not be read, so they are missing from results",
        "doctor"),
    "store-unreadable": StatusAdvice(
        "one agent's chats cannot be read - the other agents index normally",
        "doctor"),
    "missing-ingest-signal": StatusAdvice(
        "agrep cannot tell whether the index is current", "index"),
    "unreadable-ingest-signal": StatusAdvice(
        "agrep cannot tell whether the index is current", "index"),
    "future-ingest-signal": StatusAdvice(
        "this machine's clock is ahead of the index, so agrep cannot tell "
        "whether the index is current", "index"),
    "search-index-stale": StatusAdvice(
        "searches are scanning transcripts directly and may be slow",
        "index"),
    # Nothing is known to be wrong, or the reader chose it: stay quiet.
    "census-unavailable": None,
    "freshness-unchecked": None,
    "diagnostic-budget-exceeded": None,
})

_INDEXING_ADVICE_ANY = StatusAdvice(
    "new chats are not being indexed yet", "index")


def indexing_advice(code: object) -> StatusAdvice | None:
    key = str(code or "")
    if key in INDEXING_ADVICE:
        return INDEXING_ADVICE[key]
    return _INDEXING_ADVICE_ANY


def indexing_advice_line(failure: object | None, cli: str) -> str:
    """The single rendered sentence for a freshness failure, or "" for silence."""
    if failure is None:
        return ""
    advice = indexing_advice(getattr(failure, "code", ""))
    if advice is None:
        return ""
    failures = max(0, int(getattr(failure, "consecutive_failures", 0) or 0))
    cost = (f"{advice.cost} ({failures} attempts in a row)"
            if failures >= FRESHNESS_POLICY.failure_threshold else advice.cost)
    return f"{cost}: `{cli} {advice.command}`" if advice.command else cost


# An unhandled exception is still a user-facing surface. The class name and
# the internal message are the tool's own vocabulary; what the reader loses
# and what they type is not. Ordered - the first matching rule wins.
_CRASH_ADVICE = (
    ("PermissionError", (), StatusAdvice(
        "agrep cannot read or write its data directory", "doctor")),
    ("", ("event store", "event generation", "event payload"), StatusAdvice(
        "live-session data is unavailable; searching history still works",
        "doctor")),
    ("DatabaseError", (), StatusAdvice(
        "the search index is damaged", "index --full")),
    ("OperationalError", (), StatusAdvice(
        "the search index is damaged", "index --full")),
    ("DatabaseCorrupt", (), StatusAdvice(
        "the search index is damaged", "index --full")),
    ("MemoryError", (), StatusAdvice(
        "agrep ran out of memory; narrow the search with --agent, --project "
        "or --since", "")),
    ("FileNotFoundError", (), StatusAdvice(
        "a file agrep expected is gone", "doctor")),
)

_CRASH_ADVICE_ANY = StatusAdvice("agrep hit an unexpected error", "doctor")


def crash_advice_line(exc: BaseException, cli: str) -> str:
    """One sentence for an unhandled exception: what broke, and what to type."""
    message = str(exc)
    if "`" in message:
        # a message that already carries the command was authored for the
        # reader (law 5's shape); translating it would destroy the remedy
        return f"agrep failed: {message}"
    name = type(exc).__name__
    text = str(exc).lower()
    for want_name, markers, advice in _CRASH_ADVICE:
        if want_name and want_name not in name:
            continue
        if markers and not any(marker in text for marker in markers):
            continue
        break
    else:
        advice = _CRASH_ADVICE_ANY
    return (f"agrep failed: {advice.cost}: `{cli} {advice.command}`"
            if advice.command else f"agrep failed: {advice.cost}")


class Remedy(NamedTuple):
    """Law 7: a remedy classifies before it renders. `auto` remedies belong
    to a product mechanism and their text reports it; only the other kinds
    may tell the reader to act, because the product cannot."""
    kind: str  # auto | consent | privilege | human-prereq
    text: str
    owner: str = ""  # the mechanism (auto) or the reason the product can't act


REMEDIES = MappingProxyType({
    "auto-rebuild-pending": Remedy(
        "auto", "an automatic rebuild is pending in the background",
        owner="indexer.py escalation ledger"),
    "auto-rebuild-spent": Remedy(
        "auto", "the automatic rebuild also failed; the reason above is the defect",
        owner="indexer.py escalation ledger"),
    "index-behind-manual": Remedy(
        "human-prereq", "rebuild on the next `agrep index`",
        owner="no background indexer is running to converge this drift"),
    "stale-handle": Remedy(
        "auto", "rerun the search - fresh results mint current handles",
        owner="search re-mints handles"),
    "audit-gap": Remedy(
        "auto", "the next full index pass seeds the book",
        owner="index --full tally pass"),
    "store-unreadable": Remedy(
        "privilege", "restore read permission / grant disk access",
        owner="OS permissions are the human's"),
    "source-read-incomplete": Remedy(
        "human-prereq", "retry `{command}` after the agent store is stable",
        owner="source stability is outside agrep's control"),
    "source-not-found": Remedy(
        "auto", "the next automatic index pass retries the vanished source",
        owner="indexd source retry"),
    "source-health-unavailable": Remedy(
        "human-prereq",
        "repair the source-health record, then retry `{command}`",
        owner="the durable health record is unreadable or malformed"),
    "source-inspect": Remedy(
        "human-prereq",
        "retry `{command}`; inspect the named source if it persists",
        owner="the source failure kind is not recognized"),
    "legacy-publication": Remedy(
        "human-prereq", "run `agrep index` once to upgrade this legacy publication",
        owner="a full ingest can be expensive and rewrites derived stores"),
    "replace-installed-tool": Remedy(
        "consent",
        "run `{command}`; agrep cannot replace the installed tool without your consent",
        owner="local tool installation changes a user-owned executable"),
    "setup-enroll": Remedy(
        "consent", "`{command}` teaches your agents to search this history",
        owner="rewrites user agent files"),
    "fetch-binary": Remedy(
        "consent", "`agrep index` offers to fetch the prebuilt binary",
        owner="network fetch"),
    "install-rust": Remedy(
        "human-prereq", "install Rust: https://rustup.rs, then `agrep index`",
        owner="toolchain install"),
    "index-publish": Remedy(
        "human-prereq", "`{command}` runs a publication now",
        owner="a full ingest can be expensive and rewrites derived stores"),
    "index-binary": Remedy(
        "human-prereq", "`{command}` fetches or compiles the ingest binary",
        owner="a build or network fetch can require user-controlled resources"),
    "setup-resync": Remedy(
        "consent", "run `{command}` to re-sync agent instructions",
        owner="rewrites user agent files"),
    "setup-reconcile": Remedy(
        "consent", "run `{command}` to reconcile it safely",
        owner="rewrites a user-owned agent instruction file"),
    "setup-retry": Remedy(
        "consent", "retry `{command}` when network and cache access return",
        owner="setup may fetch a model and rewrite user agent files"),
    "semantic-reindex": Remedy(
        "human-prereq", "run `{command}` to rebuild semantic embeddings",
        owner="a semantic rebuild rewrites derived embedding stores"),
    "legacy-publication-upgrade": Remedy(
        "human-prereq",
        "run `{command}` once to upgrade the legacy semantic publication",
        owner="the legacy publication predates generation binding"),
    "semantic-fix": Remedy(
        "consent", "`{command}` starts the semantic build now",
        owner="model loading and an embedding build can consume resources"),
    "embeddings-enable": Remedy(
        "consent",
        "run `{command}` to allow model downloads and background builds",
        owner="changes the user's semantic-download setting"),
    "diagnostic-deep": Remedy(
        "human-prereq", "run `{command}` for complete verification",
        owner="deep verification is opt-in and can do unbounded work"),
    "post-adoption-clobber": Remedy(
        "human-prereq",
        "automatic repair is disabled; safe manual remedy (nothing is removed "
        "by doctor): 1) stop all agrep processes; 2) copy the entire data "
        "directory {data} to a backup directory outside {data}; 3) move "
        "{database} and any {sidecars} sidecars out of the data directory "
        "(do not delete them); 4) run `{command}`; 5) retain the backup until "
        "`{verify_command}` reports ready",
        owner="ownership evidence forbids automatic replacement"),
})


def render_remedy(
        name: str, *, command: str | None = None, **values: object,
) -> str:
    """Render only fields declared by a classified registry remedy."""
    text = REMEDIES[name].text
    needs_command = "{command}" in text
    if needs_command != (command is not None):
        raise ValueError(f"remedy {name!r} command binding mismatch")
    fields = dict(values)
    if command is not None:
        fields["command"] = command
    undeclared = [key for key in fields if f"{{{key}}}" not in text]
    if undeclared:
        raise ValueError(f"remedy {name!r} has undeclared fields: {undeclared}")
    return text.format(**fields)


def persistent_freshness_failure(failures: int) -> bool:
    return failures >= FRESHNESS_POLICY.failure_threshold


class FreshnessStory(NamedTuple):
    """The one freshness authority every surface renders from.

    Fresh means no source drift since the published generation - never a
    wall-clock signal age, and never process liveness. Exactly one of four
    states: current (say nothing), behind (drift, a healthy daemon converges),
    failing (drift, the writer's own health streak crossed the threshold),
    unverified (drift cannot be judged - the only may-be-stale hedge left)."""
    state: str  # current | behind | failing | unverified
    code: str = ""
    behind_s: float | None = None
    changed_stores: int = 0
    consecutive_failures: int = 0
    escalated: bool = False
    last_good_age_s: float | None = None
    detail: str = ""
    # behind only: promise convergence only when a daemon will actually run
    converging: bool = False
    # behind only: drift younger than the debounce horizon that nothing will
    # absorb - served last-good, worth a line, but not an aged "behind"
    young: bool = False
    # current only: drift was observed but a background owner is expected to
    # absorb it - display stays silent (law 3), yet a zero must not read this
    # state as proof of currency (the observation survives the silence)
    absorbed_drift: bool = False


NO_AUTO_HELP = (
    "skip automatic index/freshness work; an empty result exits 2 because "
    "absence is unverified")


def grep_absence_exit(*, exact: bool, freshness: FreshnessStory) -> int:
    """Exit 1 is "proven none"; 2 is "unverified". The rendered freshness
    story decides which: a hedge on the page denies the proof, silence
    licenses it. The exit code is a display surface for machines and obeys
    law 3 like every other one - a zero the reader is told nothing about must
    not carry a different verdict for a script.

    The proof requires POSITIVE facts, matching miss_verdict: a current
    index, no absorbed drift, and no visible hedge. Absorbed drift and a
    young converging behind are silent by design ("system working") but they
    are still in-flight - miss_verdict renders "index catching up; retry
    shortly" for them, so the zero is not proven and must not exit 1."""
    if not exact:
        return 2
    if freshness.state != "current" or freshness.absorbed_drift:
        return 2
    return 1 if not freshness_story_line(freshness) else 2


def brief_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 100.0:
        return f"{seconds:.0f}s"
    if seconds < 6000.0:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600.0:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


# The ownership-refusal code every emitter and checker shares, and the ONE
# sentence its display hedge may be (law 7; --json keeps the full reason).
# Renders only when nothing is verifiably repairing the state.
DERIVED_STORE_OWNER_CODE = "derived-store-owner"
OWNERSHIP_STALE_LINE = ("history may be stale: the search index belongs "
                        "to another agrep build")


# Repair declines render as facts, never commands; self-clearing states do not survive.
REPAIR_DECLINE_LINES = {
    "readonly": "the data dir is read-only",
    "no-daemon": "background indexing is disabled",
    "no-binary": "the ingest binary is missing",
    "held-foreign-owner": "another agrep holds the index",
    "owner-unverifiable": "another agrep's claim on the index cannot be verified",
    "probe-failed": "the repair state could not be verified",
    "spawn-failed": "the daemon failed to start",
}


def repair_decline_line(cause: str) -> str:
    return REPAIR_DECLINE_LINES.get(cause, "the daemon could not start")


def freshness_story_line(story: FreshnessStory) -> str:
    if story.state == "current":
        return ""
    if story.state == "behind":
        if story.young and story.converging:
            # drift younger than the debounce with a daemon on it is the
            # system working (law 3): nothing to say
            return ""
        plural = "s" if story.changed_stores != 1 else ""
        if story.young and not story.converging:
            return (f"serving the last-good index ({story.changed_stores} "
                    f"store{plural} changed) - "
                    f"{REMEDIES['index-behind-manual'].text}")
        age = ("" if story.behind_s is None
               else f"{brief_duration(story.behind_s)} ")
        drift = (f"index {age}behind ({story.changed_stores} "
                 f"store{plural} changed)")
        if story.converging:
            return f"{drift} - daemon catching up"
        return f"{drift} - {REMEDIES['index-behind-manual'].text}"
    if story.state == "failing":
        detail = f": {story.detail}" if story.detail else ""
        why = (f"background indexing failed {story.consecutive_failures} "
               f"consecutive times{detail}")
        last_good = (
            "" if story.last_good_age_s is None
            else (" - serving the last-good index from "
                  f"{brief_duration(story.last_good_age_s)} ago"))
        remedy = REMEDIES[
            "auto-rebuild-spent" if story.escalated else "auto-rebuild-pending"]
        return f"history may be stale: {why}{last_good} - {remedy.text}"
    if story.converging:
        # Law 3: a refresh the daemon owns is work in flight, not news; the
        # served rows are complete, so this could only report that agrep is
        # busy. The "behind" branch still speaks - there the ANSWER is short.
        return ""
    if story.code == DERIVED_STORE_OWNER_CODE:
        return OWNERSHIP_STALE_LINE
    return f"history may be stale: {story.detail or 'unknown ingest state'}"


def freshness_behind_disclosure(story: FreshnessStory) -> dict:
    """Machine shape of the catching-up state: honest, but not a failure."""
    out = {
        "state": "index-behind",
        "failing": False,
        "may_be_stale": True,
        "code": "index-behind",
        "cause": "store-drift",
        "changed_stores": max(0, int(story.changed_stores)),
    }
    if story.young:
        out["young"] = True
    if story.converging:
        out["converging"] = True
    if story.behind_s is not None:
        out["behind_s"] = round(max(0.0, story.behind_s))
    return out


def publication_freshness_disclosure() -> dict:
    """Machine shape for a verified publisher closing the next generation."""
    return {
        "state": "index-behind",
        "failing": False,
        "may_be_stale": True,
        "code": "index-behind",
        "cause": "publication-in-progress",
    }


def freshness_disclosure(failure: object | None) -> dict:
    if failure is None:
        return {"state": "no-known-failure", "failing": False}
    out = {
        "state": "degraded",
        "failing": True,
        "may_be_stale": True,
        "code": str(getattr(failure, "code", "") or "unknown"),
        "reason": str(getattr(failure, "reason", "") or ""),
    }
    failures = max(0, int(getattr(failure, "consecutive_failures", 0) or 0))
    if failures:
        # a zero streak beside failing=true reads as a contradiction; failing
        # derives from the recorded failure itself, so zero is just omitted
        out["consecutive_failures"] = failures
    return out


def row_freshness_disclosure(freshness: Mapping, *, first: bool) -> dict:
    """Trim repeated prose on row-oriented machine surfaces.

    Search carries freshness once in its run envelope; chats and recall keep
    structured code fields per row while rendering the prose reason once.
    """
    out = {key: value for key, value in freshness.items()
           if (first or key != "reason")
           and (key != "consecutive_failures" or value)}
    return out


SEMANTIC_NO_EXHAUSTIVE_FORM = (
    "the meaning lane ranks a bounded candidate set; no invocation returns "
    "every semantically related row")


def completeness_disclosure(
        *, shown: int, total: int, unit: str, totals_exact: bool,
        truncated: bool, more_command: str | None = None,
        no_exhaustive_form: str | None = None,
        full_command: str | None = None,
        more_command_kind: str | None = None,
        more_argv: Sequence[str] | None = None,
        full_argv: Sequence[str] | None = None,
        action_unavailable_reason: str | None = None) -> dict:
    """What a machine surface's numbers mean: rows printed, rows matched,
    whether that total is exact, and - when the page was cut - a bounded
    larger-page invocation or why no exhaustive form exists. A caller
    must never infer a cap from the row count it happened to receive."""
    shown = max(0, int(shown))
    total = max(0, int(total))
    cut = bool(truncated or total > shown or not totals_exact)
    out = {
        "shown": shown,
        "total": total,
        "total_basis": "exact" if totals_exact else "floor",
        "unit": unit,
        "truncated": cut,
    }
    if cut and more_command:
        out["more_command"] = more_command
    if cut and more_argv:
        out["more_argv"] = list(more_argv)
    if cut and more_command_kind and (more_command or more_argv):
        out["more_command_kind"] = more_command_kind
    if cut and full_command:
        out["full_command"] = full_command
    if cut and full_argv:
        out["full_argv"] = list(full_argv)
    if cut and no_exhaustive_form:
        out["no_exhaustive_form"] = no_exhaustive_form
    if (cut and action_unavailable_reason
            and not any((more_command, more_argv, full_command, full_argv,
                         no_exhaustive_form))):
        out["action_unavailable_reason"] = action_unavailable_reason
    return out


def count_floor_note(*, exhaustible: bool = True) -> str:
    """The one wording for a count a bounded lane stopped short of. Every
    surface that prints a "+" total says this, so a compact header can never
    read as an exact answer the JSON beside it calls a floor."""
    if not exhaustible:
        return "a floor, not the total"
    return "a floor, not the total; -c counts exactly"


# How far a top-k lane read before its frontier closed is a property of the
# ranking, not of the corpus, so a lane that stops early hands its headline to
# the counting lane - capped in rows so that count costs a bounded scan.
HEADLINE_COUNT_CAP = 25_000


def uncounted_total_line(shown: int, *, exhaustible: bool = True) -> str:
    """What a page says when nothing counted the result set: the rows it holds,
    and that the total is unknown. Leading with the scan's stopping point
    instead states a number that reads as a magnitude and is not one."""
    lever = "; -c counts exactly" if exhaustible else ""
    return (f"{count_noun(shown, 'row')} shown · total unknown "
            f"(the search lane stopped early{lever})")


def completeness_line(disclosure: Mapping, *, tool_hits: int = 0) -> str:
    """The prose of the same disclosure, for surfaces whose stdout is rows.
    One artifact behind both so a porcelain line can never disagree with the
    JSON field beside it."""
    total = int(disclosure["total"])
    exact = disclosure["total_basis"] == "exact"
    tool = f" ({tool_hits} of them in tool output)" if tool_hits else ""
    more = disclosure.get("more_command")
    reason = disclosure.get("no_exhaustive_form")
    # F3: a "+" total is a floor from a lane that stopped early; name the
    # surface that counts exactly - unless this disclosure has no exhaustive
    # form at all, which is exactly where -c refuses the pair
    basis = "" if exact else f" ({count_floor_note(exhaustible=not reason)})"
    tail = (f" · larger page: {more}" if more
            else f" · {reason}" if reason else "")
    return (f"showing {disclosure['shown']} of {total}{'' if exact else '+'} "
            f"{disclosure['unit']}{'' if total == 1 else 's'}"
            f"{tool}{basis}{tail}")


def self_exclusion_disclosure(
        policy: object | None, *, inactive_reason: str,
        excluded_hits: int | None = None,
) -> dict:
    if policy is None:
        return {"active": False, "reason": inactive_reason}
    family = getattr(policy, "family")
    boundary = getattr(policy, "boundary", None)
    resolved = bool(getattr(family, "resolved", False))
    scope = ("current-window" if boundary is not None else
             "session-family" if resolved else "session")
    out = {
        "active": True,
        "reason": str(getattr(policy, "reason", "") or "unknown"),
        "scope": scope,
        "session": str(getattr(family, "session", "") or ""),
        "excluded_hits": None,
        "excluded_hits_known": False,
    }
    if excluded_hits is not None:
        out["excluded_hits"] = max(0, int(excluded_hits))
        out["excluded_hits_known"] = True
    if boundary is not None:
        out["from_turn"] = int(boundary)
    return out


def stale_handle_recovery(cli: str) -> str:
    return f"the handle is stale; {REMEDIES['stale-handle'].text}."


def regex_timeout_line(timeout: float) -> str:
    """A refusal that hands back every lever, because the caller has none.

    The scan is linear in matched rows, so narrowing is the real fix and the
    raised budget is the escape hatch; a refusal naming neither leaves an
    agent with a dead end it cannot reason its way out of."""
    return (f"regex exceeded the {timeout:g}s safety limit - narrow the scan "
            f"(--agent/--project/--since/--chat), or raise the budget with "
            f"AGREP_REGEX_TIMEOUT_S")


def count_noun(count: int, noun: str, plural: str | None = None) -> str:
    """A counted noun with a real plural - never 'session(s)' beside a
    known number."""
    return f"{count:,} {noun if count == 1 else plural or noun + 's'}"


def compact_completeness_line(
        *, exact_total: int | None, floor: int | None, shown: int,
        exhaustible: bool, continuation: str | None,
        action_label: str = "more",
) -> str:
    """One terse basis and, when one exists, its copyable continuation."""
    if exact_total is not None:
        basis = count_noun(max(0, exact_total), "match", "matches")
    elif floor is not None:
        lever = "; -c exact" if exhaustible else ""
        basis = f"{max(0, floor):,}+ matches (floor{lever})"
    else:
        basis = f"{max(0, shown):,} shown · total unknown"
    return (f"{basis} · {action_label}: {continuation}"
            if continuation else basis)


def compact_self_exclusion_unavailable_notice(reason: str) -> str:
    """Compact cannot hide a failed current-chat exclusion."""
    if reason == "identity-conflict":
        return "caller identity conflict; current-chat rows may appear"
    return "caller identity unavailable; current-chat rows may appear"


# A render is judged whole because individually valid lines can assemble into
# noise; the render-budget suite enforces these limits across every state.
RENDER_STDERR_BUDGET = 4      # non-empty stderr lines per invocation
RENDER_LINE_MAX_CHARS = 140   # a longer line is explaining, not stating (law 7)


def self_exclusion_notice(*, resolved: bool, dropped: int = 0,
                          windowed: bool = False) -> str:
    """One counted line for a policy proven to have hidden matching rows."""
    if dropped <= 0:
        return ""
    scope = ("the current window" if windowed
             else "this session family" if resolved else "this session")
    return f"excluded {count_noun(dropped, 'hit')} from {scope}"


def handle_content_moved(turn: int, found: int) -> str:
    """The rescue line: this digest's content exists at a different turn.

    A renumbered session and a mistyped digest that collides with a real
    turn are indistinguishable here, so the line states the observation and
    both causes - asserting "renumbered" alone fabricates certainty."""
    return (f"handle digest matches turn {found}, not turn {turn} - either "
            "the session was renumbered since the handle was minted, or the "
            "digest is wrong; verify the content before citing it")


def handle_content_lost(session: str, turn: int, cli: str) -> str:
    """The refusal: the address resolves but does not hold what was cited.
    Serving it anyway is the one failure a citation may never have."""
    return (f"handle {session}:{turn} no longer holds the content it cited - "
            "the session was renumbered beyond recovery, pruned, or the "
            f"prefix now resolves to a different session. {stale_handle_recovery(cli)}")


def handle_content_ambiguous(session: str, turn: int,
                             candidates: Sequence[int], cli: str) -> str:
    """The rescue found several equal claims: recoverable ambiguity, never
    reported as loss. The doc forbids guessing between candidates, so the
    refusal lists them and hands the reader the disambiguation lever."""
    shown = [str(t) for t in candidates[:6]]
    more = (f" (+{len(candidates) - len(shown)} more)"
            if len(candidates) > len(shown) else "")
    return (f"handle {session}:{turn} does not hold the cited content, but "
            f"{count_noun(len(candidates), 'other turn')} "
            f"{'does' if len(candidates) == 1 else 'do'}: "
            f"{', '.join(shown)}{more} - refusing to guess between them; "
            f"`{cli} around {session} <turn>` shows each")


def legacy_handle_unverified() -> str:
    return ("this handle has no content digest - matching by position only; "
            "the content may have changed since it was saved")


def handle_filter_override(flags: Sequence[str],
                           mismatched: Sequence[str] = ()) -> str:
    """An explicit @handle is a stronger ask than any filter (the --chat
    precedent: explicit ask wins, with disclosure). One line says the filters
    were not applied - naming the ones that provably exclude the handle."""
    line = f"explicit result handle wins: {', '.join(flags)} not applied"
    if mismatched:
        line += f" ({', '.join(mismatched)} would have excluded it)"
    return line


AROUND_SERVICE_LINES: Mapping[str, Callable[[Mapping], str]] = MappingProxyType({
    "turn_clamped": lambda n: (
        f"turn {n['requested']} is out of range - centered on {n['served']} "
        f"(session has turns {n['first_turn']}-{n['last_turn']})."),
    "handle_unverified": lambda n: legacy_handle_unverified(),
    "handle_disambiguated": lambda n: (
        f"handle prefix matched {count_noun(n['candidates'], 'session')}; "
        f"its digest holds in {n['session']} alone"),
    "content_moved": lambda n: handle_content_moved(n["requested"], n["served"]),
})


def around_service_note(kind: str, **fields) -> dict:
    """One way the window `around` served differs from the one it was asked
    for. The rendered sentence rides inside the record, so the stderr line a
    human reads and the field a pipe reads are the same decision."""
    render = AROUND_SERVICE_LINES.get(kind)
    if render is None:
        raise ValueError(f"unknown around service note: {kind!r}")
    note = {"kind": kind, **fields}
    note["note"] = render(note)
    return note


def around_service_disclosure(notes: Sequence[Mapping]) -> dict | None:
    """The machine form of those notes. `around --json` wrote rows to stdout
    and its divergences to stderr, so a caller that captured stdout - the
    normal pipe idiom - was served a different turn than it asked for with
    nothing in hand to say so. None when the request was served as asked."""
    if not notes:
        return None
    return {"served_as_requested": False,
            "divergences": [dict(note) for note in notes]}


class SemanticGovernor(NamedTuple):
    battery_floor_pct: int
    memory_floor: float
    load_limit_per_core: float


class SemanticScoreBands(NamedTuple):
    floor: float
    strong: float


class SemanticDeferral(NamedTuple):
    code: str
    runtime_reason: str
    surface_reason: str


DEFAULT_SEMANTIC_SCORE_BANDS = SemanticScoreBands(
    floor=0.82,
    strong=0.84,
)
SEMANTIC_GOVERNOR = SemanticGovernor(
    battery_floor_pct=30,
    memory_floor=0.15,
    load_limit_per_core=1.5,
)


class SemanticLanePolicy(NamedTuple):
    keyword_only: str
    warming: str
    retries: str
    disabled_tail: str
    down_detail: str


# One degradation story for the meaning lane. Search/recall/probe notices, the
# forced -s failure line, and doctor's state rows all render from this object.
SEMANTIC_LANE_POLICY = SemanticLanePolicy(
    keyword_only="meaning unavailable; keyword-only",
    warming="catching up; retry shortly",
    retries="retries automatically",
    disabled_tail="disabled by embeddings=off",
    down_detail="the meaning lane did not answer; it retries automatically",
)
SEMANTIC_INDEX_UPDATE_REASON = "index update in progress; retry shortly"
SEMANTIC_WORKER_TRANSIENT_REASON = (
    "semantic worker was busy or starting; retry shortly")


def _semantic_index_update_retryable(reason: str) -> bool:
    return bool(
        reason == SEMANTIC_INDEX_UPDATE_REASON
        or (reason.startswith("embeddings ") and "; refresh running" in reason)
    )


def _semantic_worker_retryable(reason: str) -> bool:
    """Known pre-acceptance lifecycle races that may get one upper retry.

    Keep this exact: permission, model, profile, integrity, and accepted-query
    failures must never become optimistic retries because their prose happens
    to contain words such as ``worker`` or ``busy``.
    """
    return bool(
        reason in {
            SEMANTIC_WORKER_TRANSIENT_REASON,
            "semantic worker ownership is still settling",
            "resident semantic worker is busy or unreachable",
            "semantic request expired while queued",
            "semantic worker did not become query-ready before the automatic deadline",
        }
        or reason.startswith("semantic generations kept being republished")
        or reason.startswith("active semantic artifact was republished")
    )


def semantic_status_retryable(status: Mapping | None) -> bool:
    """Whether an unavailable result proves a bounded transient."""
    status = status or {}
    if status.get("retryable") is True:
        return True
    reason = str(status.get("reason") or "")
    return bool(
        _semantic_index_update_retryable(reason)
        or _semantic_worker_retryable(reason))


SEMANTIC_ANSWERED_STATES = frozenset({"ready", "no-confident-match"})


def semantic_lane_answered(status: Mapping | None) -> bool:
    """Whether the meaning lane actually ran and ranked candidates.

    ``no-confident-match`` is an answer: the lane searched and nothing cleared
    the confidence floor. Only states that prove no search happened
    (``unavailable``, ``query-rejected``, integrity refusals) may wear the
    lane-down vocabulary and its exit code.
    """
    return str((status or {}).get("state") or "") in SEMANTIC_ANSWERED_STATES


# One short user-facing cause per internal reason. The lane already computes
# an accurate diagnosis; the notice's job is to shorten it, never to drop it.
# Ordered: the blocking cause wins when a reason names several.
SEMANTIC_LANE_CAUSES = (
    ("AGREP_NO_SEM_WORKER",
     "AGREP_NO_SEM_WORKER disables automatic meaning; unset it to enable the lane"),
    ("agrep removal", "agrep removal blocks semantic serving"),
    ("model-not-cached",
     "semantic model not cached; `-s` or `agrep setup` fetches it once"),
    ("missing-embeddings", "meaning index is still building"),
)


def semantic_lane_cause(reason: str) -> str | None:
    """The short, actionable half of a terminal lane reason (None if opaque)."""
    for token, cause in SEMANTIC_LANE_CAUSES:
        if token in reason:
            return cause
    return None


def semantic_keyword_only_notice(status: Mapping | None = None) -> str:
    """Automatic-lane fallback with one factual explanation - transient or not.

    Dropping non-transient reasons was the bug: on a box whose model was
    never fetched the lane reported `refresh model-not-cached` and the
    reader got a bare hedge with no cause and no lever, forever."""
    line = SEMANTIC_LANE_POLICY.keyword_only
    reason = str((status or {}).get("reason") or "")
    if _semantic_index_update_retryable(reason):
        return f"{line} ({SEMANTIC_INDEX_UPDATE_REASON})"
    if semantic_status_retryable(status):
        return f"{line} ({SEMANTIC_WORKER_TRANSIENT_REASON})"
    cause = semantic_lane_cause(reason)
    return f"{line} ({cause})" if cause else line


def semantic_unavailable_notice(status: Mapping | None,
                                query: str | None = None) -> str:
    """The forced-semantic failure line: a reason when one exists, the shared
    lane-down story otherwise - never the bare state token twice."""
    status = status or {}
    state = str(status.get("state") or "unavailable")
    detail = (status.get("reason")
              or (SEMANTIC_LANE_POLICY.down_detail
                  if state == "unavailable" else state))
    subject = ("semantic search unavailable"
               if query is None or detail == SEMANTIC_INDEX_UPDATE_REASON
               else f"semantic search unavailable for {query!r}")
    return f"{subject}: {detail}"


def semantic_anchor_notice(status: Mapping | None) -> str | None:
    """A page whose query shares no word with the corpus: the rows are real
    neighbors of nothing the reader ever wrote, and their scores cannot say so."""
    anchor = (status or {}).get("corpus_anchor")
    if not isinstance(anchor, Mapping) or anchor.get("anchored") is not False:
        return None
    return ("no query word appears in indexed history; meaning matches are "
            "speculative nearest neighbors")


def semantic_integrity_notice(
        integrity: Mapping | None, *, suppress_trivial: bool = False,
) -> str | None:
    """Rows the meaning lane refused: the page is short and the reader must
    know which way it is wrong before judging the hits it did get."""
    if not integrity:
        return None
    dropped = int(integrity.get("dropped") or 0)
    if integrity.get("state") == "generation-rejected":
        repair = ("full rebuild requested"
                  if integrity.get("repair_persistent")
                  else "full rebuild request could not be persisted")
        return f"semantic integrity: active generation rejected; {repair}"
    if dropped <= 0:
        return None
    row = "row" if dropped == 1 else "rows"
    # Rows the derived mirror has not ingested yet prove nothing about the
    # embedded text, so they are stated as the coverage gap they are.
    if int(integrity.get("mismatched", dropped) or 0) <= 0:
        if suppress_trivial and dropped <= SEMANTIC_TRIVIAL_GAP_ROWS:
            # a live box's mirror trails its newest turns by a beat; page
            # surfaces stay quiet over that churn (miss proofs never do)
            return None
        return (f"semantic integrity: {dropped} {row} held back - the search "
                "index has not mirrored them yet; it catches up in the background")
    repair = (" a full rebuild is running"
              if integrity.get("repair_persistent") else "")
    if not repair and integrity.get("repair_state") == "not-requested":
        repair = " too few to distrust the index; it is retained"
    return (f"semantic integrity: {dropped} {row} dropped - the stored text "
            f"no longer matches what was embedded;{repair or ' repair pending'}")


def semantic_lane_change_notice(change: Mapping | None) -> str | None:
    """Why a store went partial: the lane changed, so EVERY row is being redone.

    The coverage line below says history is converging in the background, which
    on a 44k-row corpus is minutes of work that reads as ordinary catch-up. A
    lane change is not ordinary - it re-embeds rows that were already current -
    and attributing it is the difference between a wait and a mystery.
    """
    if not change:
        return None
    return (f"embedding lane changed ({change['from']} -> {change['to']}); "
            "re-embedding history in the background")


# A live box never closes the newest rows (the caller regenerates them);
# below both bounds the gap is churn, not convergence - the notice stays
# silent, while miss proofs always state their scope.
SEMANTIC_TRIVIAL_GAP_ROWS = 64
SEMANTIC_TRIVIAL_GAP_RATIO = 0.99


def semantic_coverage_notice(
        coverage: Mapping | None,
        accelerator: Mapping | None = None, *,
        suppress_trivial: bool = False) -> str | None:
    if not coverage:
        return None
    base_partial = not coverage.get("complete", True)
    accelerator_partial = bool(accelerator) and not accelerator.get(
        "complete", True)
    if not base_partial and not accelerator_partial:
        return None
    indexed = coverage.get("indexed", "?")
    total = coverage.get("total", "?")

    def trivial(have: object, want: object) -> bool:
        return (isinstance(have, int) and isinstance(want, int) and want > 0
                and want - have <= SEMANTIC_TRIVIAL_GAP_ROWS
                and have >= want * SEMANTIC_TRIVIAL_GAP_RATIO)

    if suppress_trivial:
        # a live box never closes its newest rows in ANY lane; the notice
        # returns the moment a real gap opens in one of them
        base_trivial = not base_partial or trivial(indexed, total)
        accelerator_trivial = not accelerator_partial or trivial(
            accelerator.get("indexed"), accelerator.get("total"))
        if base_trivial and accelerator_trivial:
            return None
    searched = accelerator.get("indexed") if accelerator else None
    if searched is not None and searched != indexed:
        return (
            f"semantic coverage is partial: {searched} searched / "
            f"{indexed} embedded / {total} source rows")
    if accelerator_partial and not base_partial:
        # the base index is complete but the accelerated lane searched a
        # prefix of it; that gap is the whole story on this page
        return (
            f"semantic coverage is partial: {accelerator.get('indexed', '?')}/"
            f"{accelerator.get('total', '?')} accelerated rows searched; "
            "the remainder is converging in the background")
    return (
        f"semantic coverage is partial: {indexed}/{total} newest-first rows "
        "indexed; history is converging in the background")


# The zero-trust contract: a miss either proves its scope - both lanes over a
# current index - or names the ONE lever that would make it provable. One
# verdict artifact behind search's zero and recall's probe miss (law 5).
MISS_CONFIDENT_TAIL = "keyword + meaning, index current"
_MISS_COVERAGE_UNKNOWN_LEVER = (
    "embedding coverage unavailable; searched scope is not verified")
# Drift a background owner is absorbing licenses display silence, never the
# words "index current": the zero hedges with the shared warming story.
_MISS_INDEX_CONVERGING_TAIL = f"keyword + meaning; index {SEMANTIC_LANE_POLICY.warming}"
_MISS_SCOPE_UNKNOWN_TAIL = (
    "keyword + meaning; corpus scope unavailable - absence unproven")
_MISS_EMPTY_INDEX_TAIL = "nothing is indexed yet"
MISS_EMPTY_INDEX_LINE = f"no match - {_MISS_EMPTY_INDEX_TAIL}"


class MissVerdict(NamedTuple):
    confident: bool
    tail: str
    # True when the tail already tells the freshness story: the render that
    # prints it marks freshness said-once so the story never stacks twice
    owns_freshness: bool = False


def miss_verdict(story: FreshnessStory, *, meaning_served: bool,
                 meaning_coverage: Mapping | None = None,
                 meaning_accelerator: Mapping | None = None,
                 sessions: int | None = None) -> MissVerdict:
    """Confident states what the zero proved; hedged names its one lever.
    A freshness hedge outranks the lane. Confidence needs positive facts -
    a current index with no drift observed, proven meaning coverage, and a
    resolvable corpus scope - never the mere absence of a hedge string."""
    freshness_hedge = freshness_story_line(story)
    if freshness_hedge:
        return MissVerdict(False, freshness_hedge, owns_freshness=True)
    if story.state != "current" or story.absorbed_drift:
        # law 3 licenses silence beside served rows, not "index current" on
        # a zero: drift in flight (behind/unverified, or absorbed) hedges
        return MissVerdict(
            False, _MISS_INDEX_CONVERGING_TAIL, owns_freshness=True)
    if not meaning_served:
        return MissVerdict(False, SEMANTIC_LANE_POLICY.keyword_only)
    gap = semantic_coverage_notice(meaning_coverage, meaning_accelerator)
    if gap is not None:
        return MissVerdict(False, gap)
    if not (meaning_coverage and meaning_coverage.get("complete") is True):
        # a lane that cannot state its coverage cannot prove absence
        return MissVerdict(False, _MISS_COVERAGE_UNKNOWN_LEVER)
    if sessions is None:
        # a scope nobody can count is a scope nobody may claim to have proven
        return MissVerdict(False, _MISS_SCOPE_UNKNOWN_TAIL)
    if int(sessions) <= 0:
        # an empty index is named, never spoken for with a proof tail
        return MissVerdict(False, _MISS_EMPTY_INDEX_TAIL)
    return MissVerdict(True, MISS_CONFIDENT_TAIL)


def miss_zero_render(
        sessions: int, verdict: MissVerdict) -> tuple[str | None, bool]:
    """(line, freshness_consumed): the one-line zero with its verdict. A
    lever that cannot fit law 7 yields the bare-scoped zero only when the
    freshness story line beside it owns the hedge; None otherwise."""
    if int(sessions) <= 0:
        # an empty index is named, never counted to zero under a proof tail
        return MISS_EMPTY_INDEX_LINE, False
    head = f"no match across {count_noun(max(0, int(sessions)), 'session')}"
    line = f"{head} - {verdict.tail}"
    if len(line) <= RENDER_LINE_MAX_CHARS:
        return line, verdict.owns_freshness
    if verdict.owns_freshness:
        return head, False
    return None, False


def semantic_deferral(
    memory_fraction: float | None,
    on_battery: bool | None,
    battery_percent: int | None,
    load_per_core: float | None,
) -> SemanticDeferral | None:
    policy = SEMANTIC_GOVERNOR
    if memory_fraction is not None and memory_fraction < policy.memory_floor:
        return SemanticDeferral(
            "memory-pressure",
            f"memory pressure ({memory_fraction:.0%} available)",
            f"memory pressure ({memory_fraction:.0%} available; "
            f"resumes at {policy.memory_floor:.0%})",
        )
    if (on_battery and battery_percent is not None
            and battery_percent < policy.battery_floor_pct):
        return SemanticDeferral(
            "battery",
            f"on battery ({battery_percent}%)",
            f"battery {battery_percent}% below the "
            f"{policy.battery_floor_pct}% floor",
        )
    if load_per_core is not None and load_per_core > policy.load_limit_per_core:
        return SemanticDeferral(
            "cpu-load",
            f"load {load_per_core:.1f}/core",
            f"CPU load {load_per_core:.1f}/core above the "
            f"{policy.load_limit_per_core:.1f}/core limit",
        )
    return None


def observed_semantic_deferral(
    memory_probe: Callable[[], float | None],
    battery_probe: Callable[[], tuple[bool | None, int | None]],
    load_probe: Callable[[], float | None],
) -> SemanticDeferral | None:
    memory = memory_probe()
    deferral = semantic_deferral(memory, None, None, None)
    if deferral:
        return deferral
    on_battery, battery_percent = battery_probe()
    deferral = semantic_deferral(memory, on_battery, battery_percent, None)
    if deferral:
        return deferral
    return semantic_deferral(memory, on_battery, battery_percent, load_probe())
