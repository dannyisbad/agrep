"""Fail-closed scrubbing of transcript records: a value survives only under an explicit keep rule."""

from __future__ import annotations

import hmac
import json
import os
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone

NEUTRAL_WORDS = (
    "amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo",
    "juniper", "kestrel", "lagoon", "marble", "nectar", "ocelot", "pebble", "quartz",
    "raven", "saffron", "tundra", "umber", "velvet", "willow", "xenon", "yarrow", "zephyr",
    "acorn", "birch", "cobalt", "dune", "elm", "fern", "granite", "heron", "iris", "jade",
    "kelp", "linen", "maple", "nickel", "onyx", "pine", "quill", "reed", "slate", "thistle",
    "ultra", "violet", "walnut", "yucca", "zinc", "aspen", "bramble", "canyon", "dusk",
    "echo", "flint", "glacier", "hazel", "inlet", "jasper", "koala", "lotus", "meadow",
    "nimbus", "orchid", "prairie", "quince", "ridge", "sequoia", "tulip", "upland", "vesper",
    "wren", "yonder", "zenith", "alder", "beacon", "cinder", "driftwood", "estuary", "falcon",
    "gorge", "hollow", "isle", "jetty", "knoll", "lantern", "mesa", "nook", "oriole", "plume",
    "quarry", "rapids", "summit", "timber", "umbra", "valley", "wharf", "yew", "zircon",
    "abalone", "bluff", "cairn", "dapple", "eddy", "fennel", "gale", "heath", "icicle",
    "jonquil", "kiln", "lichen", "mallow", "nutmeg", "oasis", "parsley", "quail", "rosemary",
    "sage", "tarn", "ursa", "verdant", "wisteria", "yolk", "zest", "atoll", "boulder",
    "clover", "dewdrop", "ebony", "foxglove", "gull", "hyssop", "ivory", "jacaranda", "kite",
    "lupine", "myrtle", "nettle", "obsidian", "poppy", "quiver", "rushes", "sorrel", "teal",
    "undertow", "vireo", "wagtail", "yarn", "zinnia", "anise", "barley", "cress", "dill",
    "endive", "fig", "ginger", "hickory", "ironwood", "jute", "kumquat", "laurel", "mint",
    "nance", "oat", "pecan", "quinoa", "rye", "sesame", "tamarind", "ube", "vanilla", "wasabi",
    "yam", "zucchini", "argon", "boron", "carbon", "dolomite", "feldspar", "gypsum", "halite",
    "iodine", "jasperite", "kaolin", "lazuli", "mica", "neon", "opal", "pyrite", "radon",
    "silica", "topaz", "uranite", "vermeil", "wolfram", "yttria", "zeolite", "auburn", "bisque",
    "celadon", "damask", "ecru", "fawn", "gamboge", "henna", "ivorine", "jacinth", "khaki",
    "lilac", "mauve", "navy", "ochre", "periwinkle", "russet", "sienna", "taupe", "umbral",
    "vermilion", "wheat", "xanthic", "zaffre",
)

# Text markers whose presence drives record classification in the adapters; the marker
# bytes stay literal, everything around them is filler.
PRESERVED_TAGS = frozenset((
    "command-name", "command-message", "command-args", "local-command-stdout",
    "local-command-stderr", "local-command-caveat", "bash-input", "bash-stdout",
    "bash-stderr", "user-prompt-submit-hook", "system-reminder", "teammate-message",
    "task-notification", "system-notification", "environment_context", "INSTRUCTIONS",
    "permissions", "user_instructions", "turn_aborted", "subagent_notification",
    "goal_context", "codex_internal_context", "codex_delegation", "realtime_delegation",
    "user_action", "image", "path", "type", "content", "ide_context", "skills_instructions",
    "collaboration_mode", "plugin_catalog", "app_catalog", "available_skills",
    "user_context", "channel_message", "agent_message", "routing_instructions",
))
PRESERVED_PREFIXES = (
    "Caveat:", "# AGENTS.md", "[SYSTEM NOTIFICATION", "Called the ",
    "Image read successfully", "[subagent task]", "[subagent message]",
    "[Request interrupted by user", "Request interrupted by user",
)
PRESERVED_ANYWHERE = ("Process exited with code ",)

ENUM_KEYS = frozenset((
    "type", "role", "userType", "subtype", "status", "stopReason", "stop_reason", "level",
    "kind", "source", "thread_source", "phase", "mode", "api", "provider", "providerID",
    "attribution", "entrypoint", "permissionMode", "promptSource", "trigger", "method",
    "state", "operation", "direction", "effort", "reason", "outcome", "collaboration_mode_kind",
    "approval_policy", "history_mode", "multi_agent_version", "model_provider", "modelRole",
    "thinkingLevel", "configured", "outputSchemaMode", "titleSource", "service_tier",
    "reasoning_effort", "reasoning_summary", "personality", "summary", "originator",
    "severity", "agent", "delivery", "strategy", "vcs", "item_type", "scope",
))
ENUM_VALUES = frozenset((
    "user", "assistant", "system", "developer", "tool", "toolResult", "bashExecution",
    "text", "thinking", "redacted_thinking", "tool_use", "tool_result", "toolCall", "image",
    "document", "input_text", "output_text", "input_image", "reasoning", "function_call",
    "function_call_output", "custom_tool_call", "custom_tool_call_output", "web_search_call",
    "tool_search_call", "local_shell_call", "local_shell_call_output", "message", "session",
    "session_meta", "response_item", "event_msg", "turn_context", "compacted", "compaction",
    "model_change", "progress", "file-history-snapshot", "queue-operation", "attachment",
    "ai-title", "last-prompt", "custom-title", "thinking_level_change", "label",
    "branch_summary", "user_message", "agent_message", "agent_reasoning", "item_completed",
    "item_started", "token_count", "task_started", "task_complete", "turn_aborted",
    "realtime_item", "token_usage_record", "world_state", "UserMessage", "AgentMessage",
    "Reasoning", "CommandExecution", "FileChange", "Extension", "WebSearch", "external",
    "internal", "completed", "in_progress", "incomplete", "failed", "pending", "running",
    "error", "success", "end_turn", "stop_sequence", "max_tokens", "stop", "toolUse",
    "aborted", "length", "info", "warning", "notice", "cli", "sdk", "sdk-cli", "exec",
    "vscode", "subagent", "guardian_review", "voice_chat", "auto", "default", "normal",
    "compact_boundary", "local_command", "turn_duration", "informational",
    "model_refusal_fallback", "away_summary", "task", "snapcompact", "remote",
    "anthropic", "openai", "openai-codex", "google", "gemini", "azure", "bedrock", "vertex",
    "openrouter", "ollama", "shadow", "anthropic-messages", "openai-codex-responses",
    "openai-responses", "openai-completions", "high", "medium", "low", "xhigh", "max",
    "none", "minimal", "detailed", "concise", "on-request", "never", "untrusted",
    "on-failure", "paginated", "disabled", "v1", "v2", "v3", "acceptEdits",
    "bypassPermissions", "plan", "dontAsk", "typed", "queued", "retry", "replan", "refusal",
    "interrupted", "ended", "dequeue", "enqueue", "popAll", "remove", "create", "update",
    "add", "delete", "set", "json", "native", "raw", "url", "file", "slow", "smol", "claude",
    "judge", "fallback", "agent", "human", "peer", "coordinator", "task-notification",
    "prompting", "blocker", "concern", "nit", "fast", "standard", "priority", "flex",
    "git", "manual", "codex_cli_rs", "codex_vscode", "codex_app", "build", "general",
    "explore", "friendly", "pragmatic", "none", "bytes", "lines", "inline_markdown",
    "all", "once", "immediate", "deferred", "workspace-write", "read-only",
    "danger-full-access", "restricted", "managed", "explicitRequestOnly", "strict",
    "permissive", "on", "off", "up", "down", "left", "right", "stream_interrupted_after_content",
    "thinking_dropped", "auto-retry", "recovered", "superseded", "credential", "model", "plain",
    "model_not_found", "rate_limit", "unknown", "mutable", "immutable", "started", "exited",
    "ready", "starting", "cancelled", "skipped", "dispose", "report", "web.search",
    "openPage", "search", "commentary", "final_answer", "summary_text", "namespace",
    "input_schema", "object", "string", "number", "integer", "boolean", "array", "null",
    "idle_notification", "task_completed", "shutdown_request", "shutdown_response", "idle",
    "plan_approval_response", "fork", "general-purpose", "Explore", "Plan", "statusline-setup",
))
TOOL_NAME_KEYS = frozenset(("name", "tool", "toolName", "tool_name", "commandName", "subagent_type"))
TOOL_NAMES = frozenset((
    "Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "LS", "Task", "WebFetch",
    "WebSearch", "TodoWrite", "TodoRead", "NotebookEdit", "NotebookRead", "AskUserQuestion",
    "ExitPlanMode", "EnterPlanMode", "KillShell", "BashOutput", "SendMessage", "TeamCreate",
    "TeamDelete", "TaskOutput", "TaskStop", "Skill", "Agent", "Monitor", "shell",
    "shell_command", "exec_command", "write_stdin", "apply_patch", "update_plan",
    "view_image", "spawn_agent", "wait_agent", "list_agents", "followup_task", "close_agent",
    "send_input", "request_user_input", "bash", "read", "edit", "write", "grep", "glob", "ls",
    "task", "wait", "yield", "eval", "web_search", "web_fetch", "webfetch", "websearch",
    "todo", "todowrite", "todoread", "question", "patch", "list", "find", "codesearch",
    "skill", "multiedit", "batch", "think", "report_issue", "browser", "ast_edit", "debug",
    "exec", "search", "fetch", "run", "process", "await", "self_review", "plan", "notebook",
    "fork", "general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide",
))
MODEL_KEYS = frozenset((
    "model", "modelId", "modelID", "model_id", "fallbackModel", "originalModel",
    "upstreamModel", "modelName", "model_name",
))
PATH_KEYS = frozenset((
    "cwd", "directory", "path", "file_path", "filePath", "notebook_path", "worktree",
    "workingDirectory", "projectPath", "project_path", "root", "gitRoot", "repo", "repository",
    "filename", "file", "dir", "workdir", "agent_path", "old_path", "new_path", "target",
    "destination", "source_path", "cwd_path", "homeDir", "home",
))
TIME_KEYS = frozenset((
    "timestamp", "time_created", "time_updated", "time_completed", "created", "completed",
    "updated", "completed_at_ms", "created_at", "updated_at", "startTime", "endTime",
    "start_time", "end_time", "createdAt", "updatedAt", "lastUpdated", "last_used_at",
    "ts", "time", "started", "finished", "expires", "token_expiry", "snapshotTimestamp",
))
VERSION_KEYS_RE = re.compile(r"version", re.IGNORECASE)
ID_KEY_RE = re.compile(r"(?:^id$|Id$|ID$|_id$|uuid$|Uuid$|UUID$|^parent$|^leaf$)")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PREFIXED_ID_RE = re.compile(r"^([A-Za-z]{1,8})([_-])([A-Za-z0-9_-]{6,})$")
HEX_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
ID_PREFIXES = frozenset((
    "toolu", "call", "msg", "ses", "prt", "rs", "fc", "item", "thread", "turn", "req", "run",
    "chatcmpl", "resp", "task", "agent", "tool", "user", "asst", "file", "img", "ws", "sess",
    "evt", "ev", "id", "key", "part", "tc", "fn", "tr", "pr", "cmd", "sub", "srvtoolu", "wf",
    "session", "message", "event", "project", "team", "job", "worker", "batch", "trace",
))


def looks_like_id(value: str) -> bool:
    """Opaque identifier shapes: uuids, digit-bearing hex, prefixed tokens and long opaque tokens."""
    if UUID_RE.match(value):
        return True
    digits = sum(ch.isdigit() for ch in value)
    if HEX_ID_RE.match(value) and (digits or len(value) >= 16):
        return True
    match = PREFIXED_ID_RE.match(value)
    if match and len(match.group(3)) >= 8 and digits >= 2:
        return True
    return bool(OPAQUE_ID_RE.match(value)) and digits >= 2


ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?$")
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}[-.]\d{3,6}Z?")
LITERAL_NAME_TOKENS = frozenset((
    "jsonl", "json", "gz", "db", "sql", "advisor", "agent", "rollout", "subagents",
    "sessions", "projects", "archive", "session", "state", "vscdb", "opencode", "crush", "tasks",
))
ABS_PATH_RE = re.compile(r"^(?:/|~/|[A-Za-z]:[\\/]|\\\\)")
VERSION_VALUE_RE = re.compile(r"^v?\d+(?:\.\d+){0,3}(?:[-+][A-Za-z0-9.]{1,16})?$")
TAG_RE = re.compile(r"</?([A-Za-z_][A-Za-z0-9_-]*)([^<>]*)>")
WORD_RUN_RE = re.compile(r"\S+")
ALNUM_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,64}")


def _alnum_tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in ALNUM_TOKEN_RE.finditer(text)}


def vocabulary_tokens() -> set[str]:
    """Every token the scrubber may emit verbatim; the name scan must not count these as leaks."""
    tokens: set[str] = set()
    for group in (ENUM_VALUES, TOOL_NAMES, PRESERVED_TAGS, LITERAL_NAME_TOKENS, CONTAINER_SEGMENTS,
                  HOME_CONTAINERS, NEUTRAL_WORDS, PRESERVED_PREFIXES, PRESERVED_ANYWHERE):
        for value in group:
            tokens |= _alnum_tokens(value)
    tokens |= {"home", "projects", "tmp", "model"}
    return tokens


HOME_CONTAINERS = frozenset(("users", "home"))
CONTAINER_SEGMENTS = frozenset((
    "desktop", "documents", "downloads", "onedrive", "tmp", "temp", "private", "var",
    "appdata", "local", "locallow", "roaming", "src", "projects", "project", "code", "repos",
    "repositories", "git", "github", "work", "dev", "workspace", "workspaces", "library",
    "application support", "folders", "t",
))
TEMP_PREFIXES = ("/private/var/folders/", "/var/folders/", "/tmp/", "/private/tmp/")

ERA_ORIGIN = datetime(2001, 1, 1, tzinfo=timezone.utc)
EPOCH_MS = (10**11, 10**13)
EPOCH_S = (10**8, 10**11)
STRUCTURAL_DEPTH = 3
# Keys that stay readable at any depth: the adapters and shape oracle address them by name.
STRUCTURAL_KEYS = frozenset((
    "type", "text", "name", "id", "input", "output", "status", "role", "content", "state",
    "arguments", "call_id", "callID", "tool", "toolCallId", "tool_use_id", "model", "modelId",
    "provider", "timestamp", "summary", "message", "payload", "title", "cwd", "directory",
    "path", "file_path", "command", "pattern", "query", "url", "prompt", "description", "cmd",
    "items", "properties", "required", "enum", "elements", "optionalProperties", "result",
    "error", "is_error", "isError", "action", "kind", "source", "thread_id", "turn_id",
    "item", "completed_at_ms", "client_id", "text_elements", "images", "files", "agents",
    "time", "created", "completed", "updated", "seq", "data", "version", "parentId",
    "parent_id", "sessionId", "session_id", "uuid", "parentUuid", "isSidechain", "isMeta",
    "userType", "synthetic", "fromExtension", "shortSummary", "details", "usage",
    "input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_output_tokens", "signature", "thinking",
    "mimeType", "media_type", "format", "stdout", "stderr", "exit_code", "exitCode",
    "metadata", "aggregated_output", "duration", "value", "key", "count", "total",
))


class ScrubError(RuntimeError):
    """A value could not be scrubbed under any rule; the caller must fail closed."""


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_iso(text: str) -> tuple[datetime, int, str] | None:
    match = ISO_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        base = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None
    micros = 0
    digits = 0
    if fraction:
        digits = len(fraction) - 1
        micros = int((fraction[1:] + "000000")[:6])
    base = base.replace(microsecond=micros)
    if zone is None or zone == "Z":
        aware = base.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if zone[0] == "+" else -1
        hours = int(zone[1:3])
        minutes = int(zone[-2:])
        aware = base.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    return aware, digits, zone or ""


def _format_iso(value: datetime, digits: int, zone: str, separator: str) -> str:
    text = value.strftime("%Y-%m-%d") + separator + value.strftime("%H:%M:%S")
    if digits:
        text += "." + f"{value.microsecond:06d}"[:digits].ljust(digits, "0")
    if zone == "Z" or zone == "":
        return text + zone
    offset = value.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    colon = ":" if ":" in zone else ""
    return text + f"{sign}{total // 3600:02d}{colon}{(total % 3600) // 60:02d}"


def observe_timestamps(value, key=None, sink=None) -> list[datetime]:
    """Every timestamp the scrubber would shift, as aware datetimes (for the era offset)."""
    found = [] if sink is None else sink
    if isinstance(value, dict):
        for child_key, child in value.items():
            observe_timestamps(child, child_key, found)
    elif isinstance(value, list):
        for child in value:
            observe_timestamps(child, key, found)
    elif isinstance(value, str):
        parsed = _parse_iso(value.strip())
        if parsed:
            found.append(parsed[0])
        elif value.lstrip()[:1] in "{[":
            try:
                nested = json.loads(value)
            except ValueError:
                nested = None
            if isinstance(nested, (dict, list)):
                observe_timestamps(nested, key, found)
    elif isinstance(value, bool):
        return found
    elif isinstance(value, (int, float)) and key is not None and _time_key(key):
        epoch = _epoch_kind(value)
        if epoch == "ms":
            found.append(datetime.fromtimestamp(value / 1000, tz=timezone.utc))
        elif epoch == "s":
            found.append(datetime.fromtimestamp(value, tz=timezone.utc))
    return found


def _time_key(key: str) -> bool:
    if key in TIME_KEYS:
        return True
    lowered = key.lower()
    return (lowered.endswith(("_ms", "_at", "time", "timestamp", "_ts"))
            or lowered.startswith(("time_", "created", "updated", "completed")))


def _epoch_kind(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if EPOCH_MS[0] <= value < EPOCH_MS[1]:
        return "ms"
    if EPOCH_S[0] <= value < EPOCH_S[1]:
        return "s"
    return None


def _container_walk(path: str) -> tuple[set[str], str | None]:
    """The lowercased segments walked past as containers, and the first non-container segment."""
    normalized = path.replace("\\", "/")
    segments = [segment for segment in normalized.split("/") if segment]
    if normalized.startswith("//"):
        segments = segments[2:]
    skip_next = False
    after_folders = 0
    containers: set[str] = set()
    for segment in segments:
        lowered = segment.lower()
        if lowered.endswith(":") or skip_next:
            skip_next = False
            continue
        if after_folders:
            after_folders -= 1
            containers.add(lowered)
            continue
        if lowered in HOME_CONTAINERS:
            containers.add(lowered)
            skip_next = True
            continue
        if lowered == "folders":
            containers.add(lowered)
            after_folders = 3
            continue
        if lowered in CONTAINER_SEGMENTS:
            containers.add(lowered)
            continue
        return containers, segment
    return containers, None


def project_root(path: str) -> str | None:
    """Port of the Claude adapter's project bucket: first non-container segment, or None."""
    return _container_walk(path)[1]


def container_segments(path: str) -> set[str]:
    """The lowercased segments of `path` that project_root walks past as containers."""
    return _container_walk(path)[0]


class Scrubber:
    """One run's consistent mapping tables; the random key never leaves the process."""

    def __init__(self, *, era_delta: timedelta, max_string_chars: int = 2000,
                 words: tuple[str, ...] = NEUTRAL_WORDS, forbidden: set[str] | None = None):
        self.era_delta = era_delta
        self.max_string_chars = max_string_chars
        raw_forbidden = {token.lower() for token in forbidden or ()}
        self.words = tuple(word for word in words if word not in raw_forbidden)
        self.forbidden = raw_forbidden - vocabulary_tokens()
        self._key = os.urandom(32)
        self._ids: dict[str, str] = {}
        self._segments: dict[str, str] = {}
        self._models: dict[str, str] = {}
        self._texts: dict[str, str] = {}
        self._rng = random.Random(int.from_bytes(os.urandom(8), "big"))
        self._word_order = self._rng.sample(self.words, len(self.words))
        self.stats = {"strings": 0, "ids": 0, "paths": 0, "timestamps": 0, "kept_enum": 0,
                      "nested_json": 0}

    # -- primitives ---------------------------------------------------------------------

    def _seeded(self, text: str) -> random.Random:
        digest = hmac.new(self._key, text.encode("utf-8", "surrogatepass"), "sha256").digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def word(self, token: str) -> str:
        """A neutral word per name-like token; injective so mapped paths never collide."""
        if token not in self._segments:
            used = len(self._segments)
            if used < len(self._word_order):
                base = self._word_order[used]
            else:
                base = f"{self._word_order[used % len(self._word_order)]}{used}"
            self._segments[token] = base
        return self._segments[token]

    def fake_id(self, value: str) -> str:
        if value in self._ids:
            return self._ids[value]
        rng = self._rng
        if UUID_RE.match(value):
            fake = "%032x" % rng.getrandbits(128)
            fake = f"{fake[:8]}-{fake[8:12]}-4{fake[13:16]}-8{fake[17:20]}-{fake[20:32]}"
            if value.isupper():
                fake = fake.upper()
        elif value.isdigit():
            fake = "".join(rng.choice("0123456789") for _ in value)
        elif HEX_ID_RE.match(value):
            fake = "".join(rng.choice("0123456789abcdef") for _ in value)
        else:
            match = PREFIXED_ID_RE.match(value)
            alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            if match:
                prefix, separator, body = match.groups()
                if prefix.lower() not in ID_PREFIXES:
                    prefix = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in prefix)
                fake = prefix + separator + "".join(
                    ch if ch in "_-" else rng.choice(alphabet) for ch in body)
            else:
                fake = "".join(
                    ch if ch in "_-." else rng.choice(alphabet) for ch in value)
        self._ids[value] = fake
        self.stats["ids"] += 1
        return fake

    def fake_model(self, value: str) -> str:
        if value not in self._models:
            self._models[value] = f"model-{len(self._models) + 1}"
        return self._models[value]

    def shift_iso(self, text: str) -> str:
        parsed = _parse_iso(text)
        if parsed is None:
            raise ScrubError("not an ISO timestamp")
        value, digits, zone = parsed
        separator = "T" if "T" in text else " "
        self.stats["timestamps"] += 1
        return _format_iso(value + self.era_delta, digits, zone, separator)

    def shift_epoch(self, value):
        kind = _epoch_kind(value)
        if kind is None:
            return value
        self.stats["timestamps"] += 1
        delta = self.era_delta.total_seconds()
        if kind == "ms":
            shifted = value + delta * 1000
        else:
            shifted = value + delta
        return int(shifted) if isinstance(value, int) else float(shifted)

    def shift_stamp(self, stamp: str) -> str:
        """Filename stamps: ``YYYY-MM-DDTHH-MM-SS`` with an optional ``-mmmZ``/``.mmm`` tail."""
        if len(stamp) == 19 and STAMP_RE.fullmatch(stamp + "-000Z"):
            return self.shift_stamp(stamp + "-000Z")[:19]
        if not STAMP_RE.fullmatch(stamp):
            raise ScrubError("not a filename stamp")
        separator = stamp[19]
        zulu = stamp.endswith("Z")
        fraction = stamp[20:].rstrip("Z")
        core = stamp[:10] + "T" + stamp[11:19].replace("-", ":")
        parsed = _parse_iso(core + "Z")
        if parsed is None:
            raise ScrubError("not a filename stamp")
        micros = int((fraction + "000000")[:6])
        value = parsed[0].replace(microsecond=micros) + self.era_delta
        digits = f"{value.microsecond:06d}"[:len(fraction)]
        return value.strftime("%Y-%m-%dT%H-%M-%S") + separator + digits + ("Z" if zulu else "")

    def path(self, value: str) -> str:
        """Neutral path with the same depth; the project root becomes one neutral word."""
        self.stats["paths"] += 1
        normalized = value.replace("\\", "/")
        if normalized.startswith("~"):
            normalized = "/home/u" + normalized[1:]
        segments = [segment for segment in normalized.split("/") if segment]
        if normalized.startswith("//"):
            segments = segments[2:]
        if any(normalized.lower().startswith(prefix) for prefix in TEMP_PREFIXES) or (
                "appdata/local/temp" in normalized.lower()):
            root = project_root(normalized)
            if root is None:
                return "/tmp"
            tail = self._tail_after(segments, root)
            return "/tmp/" + "/".join([self.word(root), *tail])
        root = project_root(normalized)
        if root is None:
            kept = []
            for segment in segments:
                lowered = segment.lower()
                if lowered.endswith(":") or lowered in HOME_CONTAINERS:
                    continue
                if lowered == "folders":
                    break
                if lowered in CONTAINER_SEGMENTS:
                    kept.append(segment)
            return "/home/u" + ("/" + "/".join(kept) if kept else "")
        tail = self._tail_after(segments, root)
        return "/home/u/projects/" + "/".join([self.word(root), *tail])

    def _tail_after(self, segments: list[str], root: str) -> list[str]:
        try:
            index = segments.index(root)
        except ValueError:
            return []
        tail = []
        rest = segments[index + 1:]
        for position, segment in enumerate(rest):
            stem, dot, extension = segment.rpartition(".")
            if (position == len(rest) - 1 and dot and stem
                    and extension.isalnum() and len(extension) <= 5):
                tail.append(self.word(stem) + "." + extension.lower())
            else:
                tail.append(self.word(segment))
        return tail

    def filename_token(self, name: str) -> str:
        """A store file or directory name: stamps shift, ids map, other tokens become words."""
        out = []
        position = 0
        for match in re.finditer(
                r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:[-.]\d{3,6}Z?)?"
                r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                name):
            out.append(self._name_piece(name[position:match.start()]))
            token = match.group(0)
            out.append(self.fake_id(token) if UUID_RE.match(token) else self.shift_stamp(token))
            position = match.end()
        out.append(self._name_piece(name[position:]))
        return "".join(out)

    def _name_piece(self, piece: str) -> str:
        rendered = []
        for part in re.split(r"([._-]+)", piece):
            if not part or re.fullmatch(r"[._-]+", part):
                rendered.append(part)
            elif part.lower() in LITERAL_NAME_TOKENS:
                rendered.append(part)
            elif part.isdigit():
                rendered.append(self.fake_id(part) if len(part) >= 6 else part)
            elif looks_like_id(part):
                rendered.append(self.fake_id(part))
            else:
                rendered.append(self.word(part))
        return "".join(rendered)

    def text(self, value: str) -> str:
        """Same-length filler: whitespace kept, classification markers kept, all else replaced."""
        if value in self._texts:
            return self._texts[value]
        self.stats["strings"] += 1
        capped = value if len(value) <= self.max_string_chars else value[:self.max_string_chars]
        result = self._render_text(capped, self._seeded(capped))
        if self.forbidden:
            result = ALNUM_TOKEN_RE.sub(self._repair_forbidden, result)
        self._texts[value] = result
        return result

    def _repair_forbidden(self, match: re.Match) -> str:
        """Rotate the letters of a filler token that happens to spell a forbidden name."""
        token = match.group(0)
        if token.lower() not in self.forbidden:
            return token
        for shift in range(1, 26):
            candidate = "".join(
                chr((ord(ch.lower()) - 97 + shift) % 26 + (65 if ch.isupper() else 97))
                if ch.isalpha() else ch for ch in token)
            if candidate.lower() not in self.forbidden:
                return candidate
        raise ScrubError("filler token cannot avoid the forbidden set")

    def _render_text(self, capped: str, rng: random.Random) -> str:
        pieces = []
        position = 0
        stripped_at = len(capped) - len(capped.lstrip())
        head = capped[stripped_at:]
        for prefix in PRESERVED_PREFIXES:
            if head.startswith(prefix):
                pieces.append(capped[:stripped_at] + prefix)
                position = stripped_at + len(prefix)
                break
        spans = []
        for match in TAG_RE.finditer(capped):
            if match.start() >= position and match.group(1) in PRESERVED_TAGS:
                spans.append((match.start(), match.end(), match))
        for marker in PRESERVED_ANYWHERE:
            start = capped.find(marker, position)
            while start >= 0:
                end = start + len(marker)
                while end < len(capped) and capped[end].isdigit():
                    end += 1
                spans.append((start, end, None))
                start = capped.find(marker, end)
        spans.sort(key=lambda span: span[0])
        for start, end, match in spans:
            if start < position:
                continue
            pieces.append(self._fill(capped[position:start], rng))
            if match is None:
                pieces.append(capped[start:end])
            else:
                closing = match.group(0).startswith("</")
                attributes = match.group(2)
                pieces.append(("</" if closing else "<") + match.group(1)
                              + self._fill(attributes, rng) + ">")
            position = end
        pieces.append(self._fill(capped[position:], rng))
        return "".join(pieces)

    def _fill(self, segment: str, rng: random.Random) -> str:
        if not segment:
            return segment
        letters = iter(self._letter_stream(rng))
        out = []
        for ch in segment:
            if ch.isspace():
                out.append(ch)
            elif ch.isdigit() and ch.isascii():
                out.append(str(rng.randrange(10)))
            elif ch.isascii():
                letter = next(letters)
                out.append(letter.upper() if ch.isupper() else letter)
            else:
                out.append(self._non_ascii_filler(ch))
        return "".join(out)

    def _letter_stream(self, rng: random.Random):
        while True:
            for ch in rng.choice(self.words):
                yield ch

    @staticmethod
    def _non_ascii_filler(ch: str) -> str:
        if unicodedata.category(ch).startswith("M"):
            return "\u0301"
        width = len(ch.encode("utf-8", "surrogatepass"))
        if width == 2:
            return "\u00e9"
        if width == 3:
            return "\u5b57"
        return "\U0001f600"

    # -- structured values --------------------------------------------------------------

    def value(self, item, key: str | None = None, depth: int = 0, parent: str | None = None):
        if isinstance(item, dict):
            return {self.key(child_key, depth): self.value(child, child_key, depth + 1, key)
                    for child_key, child in item.items()}
        if isinstance(item, list):
            return [self.value(child, key, depth, parent) for child in item]
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, (int, float)):
            if key is not None and _time_key(key):
                return self.shift_epoch(item)
            return item
        if isinstance(item, str):
            return self.string(item, key, parent)
        raise ScrubError(f"unsupported value type {type(item).__name__}")

    def key(self, name: str, depth: int) -> str:
        if ABS_PATH_RE.match(name) or "/" in name or "\\" in name:
            return self.path(name)
        if looks_like_id(name):
            return self.fake_id(name)
        if depth < STRUCTURAL_DEPTH or name in STRUCTURAL_KEYS:
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$-]{0,63}", name):
                return name
        return self.text(name)

    def string(self, item: str, key: str | None, parent: str | None) -> str:
        stripped = item.strip()
        if not stripped:
            return item
        if _parse_iso(stripped) is not None:
            return self.shift_iso(stripped)
        if key is not None and parent == "model" and key == "id":
            return self.fake_model(item)
        if key in MODEL_KEYS:
            return self.fake_model(item)
        if (key is not None and ID_KEY_RE.search(key)) or looks_like_id(stripped):
            return self.fake_id(stripped)
        if key in PATH_KEYS or ABS_PATH_RE.match(stripped):
            return self.path(stripped)
        if key in ENUM_KEYS and stripped in ENUM_VALUES:
            self.stats["kept_enum"] += 1
            return stripped
        if key in TOOL_NAME_KEYS and stripped in TOOL_NAMES:
            self.stats["kept_enum"] += 1
            return stripped
        if key is not None and VERSION_KEYS_RE.search(key) and VERSION_VALUE_RE.match(stripped):
            return stripped
        if stripped[:1] in "{[":
            try:
                nested = json.loads(stripped)
            except ValueError:
                nested = None
            if isinstance(nested, (dict, list)):
                self.stats["nested_json"] += 1
                return json.dumps(self.value(nested, key, STRUCTURAL_DEPTH),
                                  ensure_ascii=False, separators=(",", ":"))
        return self.text(item)


def era_delta_for(earliest: datetime | None) -> timedelta:
    """Offset that lands the earliest observed timestamp on the synthetic era origin."""
    if earliest is None:
        return timedelta(0)
    return ERA_ORIGIN - earliest.astimezone(timezone.utc)
