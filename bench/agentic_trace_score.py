#!/usr/bin/env python3
"""Score agentic agrep behavior from provider-authored JSONL traces.

The scorer trusts structural provider events, never prose that merely mentions a
command. It emits local evidence, performs no network access, and writes only
when an explicit output path is supplied.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_TRACE_LINE_BYTES = 16 * 1024 * 1024
MAX_TOOL_INPUT_CHARS = 512 * 1024
MAX_TOOL_OUTPUT_CHARS = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 2 * 1024 * 1024
COMMAND_TOOL_NAMES = frozenset({
    "bash", "cmd", "exec", "exec_command", "powershell", "pwsh",
    "run_terminal_cmd", "shell", "terminal",
})
FULL_HANDLE = re.compile(
    r"(?<![A-Za-z0-9_.@-])"
    r"@[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9]+\.[A-Za-z0-9]+"
    r"(?![A-Za-z0-9_.-])")
BASE_HANDLE = re.compile(
    r"@[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9]+(?:\.[A-Za-z0-9]+)?")
BARE_FULL_HANDLE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9]+\.[A-Za-z0-9]+")
BARE_BASE_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9]+")
SESSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "<", ">", "<<", ">>"})


class TraceScoreError(ValueError):
    """The trace cannot be scored without making an unsafe assumption."""


@dataclass(frozen=True)
class Event:
    index: int
    line: int
    raw: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Command:
    text: str
    position: int


@dataclass
class ProviderCall:
    call_id: str
    name: str
    event: Event
    signature: str
    commands: list[Command]
    output: str | None = None
    output_event: Event | None = None


@dataclass(frozen=True)
class AgrepInvocation:
    call: ProviderCall
    command: Command
    kind: str
    argv: tuple[str, ...]
    around_target: str | None
    around_target_status: str | None
    has_full: bool
    has_no_tools: bool


def _error(context: str, detail: str) -> TraceScoreError:
    return TraceScoreError(f"{context}: {detail}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(raw: object, context: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise _error(context, "missing timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(context, "invalid timestamp") from exc
    if value.tzinfo is None:
        raise _error(context, "timestamp has no timezone")
    return value


def _milliseconds(start: datetime, end: datetime, context: str) -> int:
    elapsed = round((end - start).total_seconds() * 1000)
    if elapsed < 0:
        raise _error(context, "timestamps move backwards")
    return elapsed


def _load_events(path: Path) -> list[Event]:
    if not path.is_file() or path.is_symlink():
        raise _error(str(path), "trace is not a regular file")
    events: list[Event] = []
    with path.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            if len(raw_line) > MAX_TRACE_LINE_BYTES:
                raise _error(str(path), f"line {line_number} is too large")
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _error(str(path), f"line {line_number} is not JSON") from exc
            if not isinstance(value, dict):
                raise _error(str(path), f"line {line_number} is not an object")
            payload = value.get("payload")
            if not isinstance(payload, dict):
                raise _error(str(path), f"line {line_number} has no object payload")
            events.append(Event(
                index=len(events), line=line_number, raw=value, payload=payload))
    if not events:
        raise _error(str(path), "trace has no events")
    return events


def _event_time(event: Event, context: str) -> datetime:
    return _timestamp(event.raw.get("timestamp"), f"{context} line {event.line}")


def _message_text(payload: Mapping[str, Any], context: str) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for index, block in enumerate(content):
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                raise _error(context, f"content block {index} is malformed")
            if "text" in block:
                if not isinstance(block["text"], str):
                    raise _error(context, f"content block {index} text is malformed")
                parts.append(block["text"])
        text = "".join(parts)
    else:
        raise _error(context, "message content is malformed")
    if len(text) > MAX_MESSAGE_CHARS:
        raise _error(context, "message is too large")
    return text


def _is_message(event: Event, role: str) -> bool:
    return (event.raw.get("type") == "response_item"
            and event.payload.get("type") == "message"
            and event.payload.get("role") == role)


def _find_boundary(events: Sequence[Event], case: Mapping[str, Any]) -> Event:
    prompt = case.get("tested_prompt")
    prompt_hash = case.get("tested_prompt_sha256")
    if (isinstance(prompt, str)) == (isinstance(prompt_hash, str)):
        raise _error(str(case.get("id", "case")),
                     "provide exactly one tested_prompt or tested_prompt_sha256")
    if isinstance(prompt_hash, str) and not re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
        raise _error(str(case.get("id", "case")), "prompt hash is not lowercase SHA-256")
    matches = []
    for event in events:
        if not _is_message(event, "user"):
            continue
        text = _message_text(event.payload, f"user message line {event.line}")
        if (text == prompt if isinstance(prompt, str)
                else _sha256_text(text) == prompt_hash):
            matches.append(event)
    if len(matches) != 1:
        raise _error(str(case.get("id", "case")),
                     f"expected one exact user boundary, found {len(matches)}")
    _event_time(matches[0], "user boundary")
    return matches[0]


def _case_window(events: Sequence[Event], boundary: Event) -> list[Event]:
    next_user = next((
        event for event in events
        if event.index > boundary.index and _is_message(event, "user")), None)
    end_index = next_user.index if next_user is not None else len(events)
    launches: dict[str, int] = {}
    for event in events:
        if (event.raw.get("type") == "response_item"
                and event.payload.get("type") in {
                    "function_call", "custom_tool_call"}
                and isinstance(event.payload.get("call_id"), str)):
            launches.setdefault(event.payload["call_id"], event.index)
    for event in events:
        if (event.raw.get("type") != "response_item"
                or event.payload.get("type") not in {
                    "function_call_output", "custom_tool_call_output"}):
            continue
        call_id = event.payload.get("call_id")
        launch = launches.get(call_id) if isinstance(call_id, str) else None
        if launch is None:
            continue
        crosses_start = launch < boundary.index < event.index
        crosses_end = launch < end_index <= event.index and launch > boundary.index
        if crosses_start or crosses_end:
            raise _error(
                f"tool call {call_id}", "call output straddles a user boundary")
    return list(events[:end_index])


def _parse_js_string(text: str, start: int, context: str) -> tuple[str, int]:
    if start >= len(text) or text[start] not in "\"'":
        raise _error(context, "expected a quoted string")
    quote = text[start]
    escaped = False
    for end in range(start + 1, len(text)):
        char = text[end]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char != quote:
            continue
        literal = text[start:end + 1]
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError) as exc:
            raise _error(context, "invalid JavaScript string literal") from exc
        if not isinstance(value, str):
            raise _error(context, "JavaScript literal is not a string")
        return value, end + 1
    raise _error(context, "unterminated JavaScript string")


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_js_comment(text: str, index: int, context: str) -> int:
    if text.startswith("//", index):
        end = text.find("\n", index + 2)
        return len(text) if end < 0 else end + 1
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        if end < 0:
            raise _error(context, "unterminated JavaScript comment")
        return end + 2
    return index


def _skip_js_template(text: str, index: int, context: str) -> int:
    cursor = index + 1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "`":
            return cursor + 1
        if text.startswith("${", cursor):
            end = _balanced_end(text, cursor + 1, "{", "}", context)
            expression = text[cursor + 2:end - 1]
            if "tools.exec_command" in expression:
                raise _error(context, "exec_command in a template expression is ambiguous")
            cursor = end
            continue
        cursor += 1
    raise _error(context, "unterminated JavaScript template literal")


def _regex_can_start(text: str, index: int) -> bool:
    prefix = text[:index].rstrip()
    if not prefix:
        return True
    return prefix[-1] in "=(:,[!&|?;{}"


def _skip_js_regex(text: str, index: int, context: str) -> int:
    if not _regex_can_start(text, index):
        return index
    cursor = index + 1
    in_class = False
    while cursor < len(text):
        char = text[cursor]
        if char in "\r\n":
            raise _error(context, "unterminated JavaScript regex literal")
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif char == "/" and not in_class:
            cursor += 1
            while cursor < len(text) and text[cursor].isalpha():
                cursor += 1
            return cursor
        cursor += 1
    raise _error(context, "unterminated JavaScript regex literal")


def _balanced_end(text: str, start: int, opening: str, closing: str,
                  context: str) -> int:
    if start >= len(text) or text[start] != opening:
        raise _error(context, f"expected {opening}")
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] in "\"'":
            _value, index = _parse_js_string(text, index, context)
            continue
        if text[index] == "`":
            index = _skip_js_template(text, index, context)
            continue
        skipped = _skip_js_comment(text, index, context)
        if skipped != index:
            index = skipped
            continue
        if text[index] == "/":
            skipped = _skip_js_regex(text, index, context)
            if skipped != index:
                index = skipped
                continue
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise _error(context, f"unterminated {opening}{closing} block")


def _value_end(text: str, start: int, context: str) -> int:
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = start
    while index < len(text):
        char = text[index]
        if char in "\"'":
            _value, index = _parse_js_string(text, index, context)
            continue
        if char == "`":
            index = _skip_js_template(text, index, context)
            continue
        skipped = _skip_js_comment(text, index, context)
        if skipped != index:
            index = skipped
            continue
        if char == "/":
            skipped = _skip_js_regex(text, index, context)
            if skipped != index:
                index = skipped
                continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in ",}":
            return index
        elif char in ")]}":
            raise _error(context, "unbalanced JavaScript value")
        index += 1
    return index


def _js_object_command(text: str, context: str) -> str:
    if not text.startswith("{") or not text.endswith("}"):
        raise _error(context, "exec_command argument is not an object")
    commands: list[str] = []
    index = 1
    while True:
        index = _skip_space(text, index)
        if index < len(text) - 1 and text[index] == ",":
            index = _skip_space(text, index + 1)
        if index == len(text) - 1:
            break
        if index >= len(text) - 1:
            raise _error(context, "malformed exec_command object")
        if text[index] in "\"'":
            key, index = _parse_js_string(text, index, context)
        else:
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", text[index:])
            if match is None:
                raise _error(context, "malformed exec_command key")
            key = match.group(0)
            index += len(key)
        index = _skip_space(text, index)
        if index >= len(text) or text[index] != ":":
            raise _error(context, "exec_command key has no colon")
        index = _skip_space(text, index + 1)
        if key in {"cmd", "command"}:
            value, index = _parse_js_string(text, index, context)
            commands.append(value)
        else:
            index = _value_end(text, index, context)
        index = _skip_space(text, index)
        if index < len(text) - 1 and text[index] != ",":
            raise _error(context, "exec_command fields are not comma separated")
    if len(commands) != 1:
        raise _error(context, f"expected one command authority, found {len(commands)}")
    if not commands[0] or len(commands[0]) > MAX_TOOL_INPUT_CHARS:
        raise _error(context, "command is empty or too large")
    return commands[0]


def _nested_exec_commands(source: str, context: str) -> list[Command]:
    if len(source) > MAX_TOOL_INPUT_CHARS:
        raise _error(context, "tool input is too large")
    token = "tools.exec_command"
    commands: list[Command] = []
    index = 0
    while index < len(source):
        if source[index] in "\"'":
            _value, index = _parse_js_string(source, index, context)
            continue
        if source[index] == "`":
            index = _skip_js_template(source, index, context)
            continue
        skipped = _skip_js_comment(source, index, context)
        if skipped != index:
            index = skipped
            continue
        if source[index] == "/":
            skipped = _skip_js_regex(source, index, context)
            if skipped != index:
                index = skipped
                continue
        if not source.startswith(token, index):
            index += 1
            continue
        before = source[index - 1] if index else ""
        after = source[index + len(token):index + len(token) + 1]
        if before and (before.isalnum() or before in "_$."):
            index += 1
            continue
        if after and (after.isalnum() or after in "_$"):
            index += 1
            continue
        cursor = _skip_space(source, index + len(token))
        if cursor >= len(source) or source[cursor] != "(":
            raise _error(context, "exec_command is not called")
        cursor = _skip_space(source, cursor + 1)
        end = _balanced_end(source, cursor, "{", "}", context)
        command = _js_object_command(source[cursor:end], context)
        close = _skip_space(source, end)
        if close >= len(source) or source[close] != ")":
            raise _error(context, "exec_command has extra arguments")
        commands.append(Command(command, index))
        index = close + 1
    return commands


def _structured_command(value: object, context: str) -> list[Command]:
    if isinstance(value, str):
        if len(value) > MAX_TOOL_INPUT_CHARS:
            raise _error(context, "tool input is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _error(context, "direct command arguments are not JSON") from exc
    if not isinstance(value, dict):
        raise _error(context, "direct command arguments are not an object")
    fields = [key for key in ("cmd", "command") if key in value]
    if len(fields) != 1 or not isinstance(value[fields[0]], str):
        raise _error(context, "direct call has no unique string command")
    command = value[fields[0]]
    if not command or len(command) > MAX_TOOL_INPUT_CHARS:
        raise _error(context, "command is empty or too large")
    return [Command(command, 0)]


def _call_commands(payload: Mapping[str, Any], context: str) -> list[Command]:
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise _error(context, "tool call has no name")
    base = name.casefold().rsplit(".", 1)[-1]
    has_arguments = "arguments" in payload and payload.get("arguments") is not None
    has_input = "input" in payload and payload.get("input") is not None
    if has_arguments and has_input:
        raise _error(context, "tool call has two input authorities")
    value = payload.get("arguments") if has_arguments else payload.get("input")
    if base not in COMMAND_TOOL_NAMES:
        return []
    if base == "exec" and isinstance(value, str):
        commands = _nested_exec_commands(value, context)
        if commands:
            return commands
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return _structured_command(decoded, context)
    return _structured_command(value, context)


def _call_signature(payload: Mapping[str, Any]) -> str:
    stable = {
        "name": payload.get("name"),
        "arguments": payload.get("arguments"),
        "input": payload.get("input"),
    }
    try:
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise _error("tool call", "input is not JSON-compatible") from exc
    return _sha256_text(encoded)


def _provider_calls(events: Sequence[Event], boundary: Event) -> tuple[list[ProviderCall], set[str]]:
    calls: list[ProviderCall] = []
    by_id: dict[str, ProviderCall] = {}
    pre_boundary_ids: set[str] = set()
    for event in events:
        payload = event.payload
        if (event.raw.get("type") != "response_item"
                or payload.get("type") not in {"function_call", "custom_tool_call"}):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            if event.index > boundary.index:
                raise _error(f"line {event.line}", "tool call has no call_id")
            continue
        if event.index <= boundary.index:
            pre_boundary_ids.add(call_id)
            continue
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise _error(f"line {event.line}", "tool call has no name")
        signature = _call_signature(payload)
        prior = by_id.get(call_id)
        if prior is not None:
            if prior.signature != signature:
                raise _error(f"line {event.line}", "conflicting duplicate call_id")
            continue
        call = ProviderCall(
            call_id=call_id, name=name, event=event, signature=signature,
            commands=_call_commands(payload, f"tool call {call_id} line {event.line}"))
        _event_time(event, f"tool call {call_id}")
        by_id[call_id] = call
        calls.append(call)
    return calls, pre_boundary_ids


def _output_text(value: object, context: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for index, block in enumerate(value):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                raise _error(context, f"output block {index} is malformed")
        text = "".join(parts)
    elif isinstance(value, dict) and isinstance(value.get("text"), str):
        text = value["text"]
    else:
        raise _error(context, "tool output is malformed")
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        raise _error(context, "tool output is too large")
    return text


def _attach_outputs(events: Sequence[Event], boundary: Event,
                    calls: Sequence[ProviderCall], pre_boundary_ids: set[str]) -> None:
    by_id = {call.call_id: call for call in calls}
    for event in events:
        payload = event.payload
        if (event.index <= boundary.index or event.raw.get("type") != "response_item"
                or payload.get("type") not in {
                    "function_call_output", "custom_tool_call_output"}):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise _error(f"line {event.line}", "tool output has no call_id")
        call = by_id.get(call_id)
        if call is None:
            if call_id in pre_boundary_ids:
                continue
            raise _error(f"line {event.line}", "tool output has no launched call")
        try:
            text = _output_text(payload.get("output"), f"tool output {call_id}")
        except TraceScoreError:
            # Output text is needed only to inspect agrep's renderer.  An
            # unrelated image/audio/structured tool result must not make an
            # otherwise measurable agent turn unscorable.
            if any("agrep" in command.text.casefold()
                   for command in call.commands):
                raise
            text = ""
        if call.output_event is not None:
            if call.output != text:
                raise _error(f"line {event.line}", "conflicting duplicate tool output")
            continue
        _event_time(event, f"tool output {call_id}")
        call.output = text
        call.output_event = event


def _shell_argv(command: str, context: str) -> tuple[str, ...] | None:
    if "\x00" in command:
        raise _error(context, "command contains NUL")
    normalized = command.strip().replace("\\\n", "")
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = tuple(lexer)
    except ValueError as exc:
        raise _error(context, "command cannot be shell-tokenized") from exc
    complex_shell = "\n" in normalized or any(
        token in SHELL_SEPARATORS for token in tokens)
    if complex_shell:
        if any(_basename(token).lstrip(".") in {
                "agrep", "agrep.cmd", "agrep.exe"} for token in tokens):
            raise _error(context, "complex command may invoke agrep ambiguously")
        return None
    return tokens or None


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _agrep_argv(command: str, context: str) -> tuple[str, ...] | None:
    argv = _shell_argv(command, context)
    if not argv:
        return None
    index = 0
    while index < len(argv) and SHELL_ASSIGNMENT.fullmatch(argv[index]):
        index += 1
    if index < len(argv) and _basename(argv[index]) == "env":
        index += 1
        if index < len(argv) and argv[index] == "--":
            index += 1
        elif index < len(argv) and argv[index].startswith("-"):
            return None
        while index < len(argv) and SHELL_ASSIGNMENT.fullmatch(argv[index]):
            index += 1
    if index < len(argv) and _basename(argv[index]) == "command":
        index += 1
        if index < len(argv) and argv[index] == "--":
            index += 1
        elif index < len(argv) and argv[index].startswith("-"):
            # `command -v agrep` inspects the executable; it does not launch
            # the product and therefore is not a retrieval action.
            return None
    if index >= len(argv):
        return None
    executable = _basename(argv[index])
    if executable in {"zsh", "bash", "sh", "dash", "ksh"}:
        command_options = [position for position, value in enumerate(
            argv[index + 1:], index + 1)
                           if value in {"-c", "-lc"}]
        if len(command_options) != 1 or command_options[0] + 1 >= len(argv):
            return None
        return _agrep_argv(argv[command_options[0] + 1], context)
    if executable.lstrip(".") not in {"agrep", "agrep.cmd", "agrep.exe"}:
        return None
    return argv[index:]


def _around_target(args: Sequence[str]) -> tuple[str, str] | None:
    positionals: list[str] = []
    index = 0
    value_options = {
        "--before", "--context", "--max", "--no-who", "--session",
        "--since", "--until", "--who",
    }
    while index < len(args):
        token = args[index]
        if token == "--":
            positionals.extend(args[index + 1:])
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    if len(positionals) == 1:
        target = positionals[0]
        if FULL_HANDLE.fullmatch(target):
            return target, "full_handle"
        if BASE_HANDLE.fullmatch(target):
            return target, "base_handle"
        if BARE_FULL_HANDLE.fullmatch(target):
            return target, "full_handle_missing_at"
        if BARE_BASE_HANDLE.fullmatch(target):
            return target, "base_handle_missing_at"
        if SESSION_TOKEN.fullmatch(target):
            return target, "session_only"
        if target.startswith("@") and SESSION_TOKEN.fullmatch(target[1:]):
            return target, "session_only_with_at"
        raise TraceScoreError("around target is not a recognized handle form")
    if (len(positionals) == 2 and SESSION_TOKEN.fullmatch(positionals[0])
            and positionals[1].isdigit()):
        return " ".join(positionals), "legacy_session_turn"
    if not positionals:
        return None
    raise TraceScoreError("around target has ambiguous positional arguments")


def _agrep_invocations(calls: Sequence[ProviderCall]) -> list[AgrepInvocation]:
    invocations: list[AgrepInvocation] = []
    for call in calls:
        in_call = []
        for command in call.commands:
            argv = _agrep_argv(command.text, f"tool call {call.call_id}")
            if not argv:
                continue
            surface = argv[1].casefold() if len(argv) >= 2 else ""
            if surface in {"recall", "around"}:
                kind = surface
            elif surface in {"search", "chats", "board"}:
                kind = f"agrep_{surface}"
            else:
                # A bare pattern, global-option search, or an unknown/reserved
                # verb still launches agrep and must not disappear into the
                # generic `other_tool` bucket.
                kind = "agrep_bare"
            parsed_target = _around_target(argv[2:]) if kind == "around" else None
            if kind == "around" and parsed_target is None:
                raise _error(f"tool call {call.call_id}", "around target is ambiguous")
            target, target_status = parsed_target if parsed_target else (None, None)
            in_call.append(AgrepInvocation(
                call=call, command=command, kind=kind, argv=argv,
                around_target=target, around_target_status=target_status,
                has_full="--full" in argv[2:],
                has_no_tools="--no-tools" in argv[2:]))
        if len(in_call) > 1:
            raise _error(f"tool call {call.call_id}",
                         "multiple agrep invocations share one output authority")
        if in_call and len(call.commands) != 1:
            raise _error(f"tool call {call.call_id}",
                         "agrep and another command share one output authority")
        invocations.extend(in_call)
    return invocations


def _returned_handles(invocation: AgrepInvocation) -> list[str]:
    if invocation.call.output is None:
        return []
    returned: list[str] = []

    def add(candidate: str) -> None:
        if FULL_HANDLE.fullmatch(candidate) and candidate not in returned:
            returned.append(candidate)

    for rendered in _command_stdout(invocation.call.output).splitlines():
        line = ANSI_ESCAPE.sub("", rendered)
        if line.startswith("── "):
            header = line[len("── "):]
            candidates = FULL_HANDLE.findall(header)
            if len(candidates) == 1 and f"{candidates[0]} ·" in header:
                add(candidates[0])
            continue
        if line.startswith("recall: ") and " - pull: " in line:
            pointer = line.split(" - pull: ", 1)[0]
            candidates = FULL_HANDLE.findall(pointer)
            if len(candidates) == 1:
                add(candidates[0])
            continue
        trailer = re.fullmatch(r"\[\+\d+ hit\(s\) over budget - (.*)\]", line)
        if trailer is not None:
            for candidate in FULL_HANDLE.findall(trailer.group(1)):
                add(candidate)
            continue
        cap = re.fullmatch(
            r"\[output truncated to --budget - rest at (" +
            FULL_HANDLE.pattern + r")\]", line)
        if cap is not None:
            add(cap.group(1))
    return returned


def _command_stdout(output: str) -> str:
    marker = "\nOutput:\n"
    if output.startswith("Script ") and marker in output:
        return output.split(marker, 1)[1]
    return output


def _semantic_renderer_status(invocation: AgrepInvocation) -> str:
    if invocation.kind != "recall":
        return "not_applicable"
    if invocation.call.output_event is None:
        return "output_unobserved"
    rendered = _command_stdout(invocation.call.output or "").strip()
    if not rendered:
        return "no_unavailability_diagnostic"
    first_line = ANSI_ESCAPE.sub("", rendered.splitlines()[0]).strip().casefold()
    if first_line.startswith((
            "meaning unavailable", "semantic worker was busy",
            "semantic coverage unavailable")):
        return "product_semantic_unavailable"
    try:
        structured = json.loads(rendered)
    except json.JSONDecodeError:
        structured = None
    if isinstance(structured, dict):
        status = structured.get("semantic_status", structured.get("meaning_status"))
        if isinstance(status, str) and status.casefold() in {
                "unavailable", "worker_busy", "coverage_unavailable"}:
            return "product_semantic_unavailable"
    return "no_unavailability_diagnostic"


def _invocation_record(invocation: AgrepInvocation) -> dict[str, Any]:
    call = invocation.call
    start = _event_time(call.event, f"tool call {call.call_id}")
    duration = None
    if call.output_event is not None:
        duration = _milliseconds(
            start, _event_time(call.output_event, f"tool output {call.call_id}"),
            f"tool call {call.call_id}")
    return {
        "call_id": call.call_id,
        "provider_tool": call.name,
        "trace_line": call.event.line,
        "kind": invocation.kind,
        "literal_command": invocation.command.text,
        "around_target": invocation.around_target,
        "around_target_status": invocation.around_target_status,
        "has_full": invocation.has_full,
        "has_no_tools": invocation.has_no_tools,
        "output_observed": call.output_event is not None,
        "output_empty": (
            call.output_event is not None
            and _command_stdout(call.output or "") == ""),
        "semantic_renderer_status": _semantic_renderer_status(invocation),
        "returned_full_handles": _returned_handles(invocation),
        "duration_ms": duration,
    }


def _final_answer(events: Sequence[Event], boundary: Event) -> tuple[Event | None, str | None]:
    answers = []
    for event in events:
        if event.index <= boundary.index or not _is_message(event, "assistant"):
            continue
        if event.payload.get("phase") != "final_answer":
            continue
        answers.append((event, _message_text(
            event.payload, f"final answer line {event.line}")))
    if len(answers) > 1:
        raise _error("final answer", f"found {len(answers)} final-answer messages")
    if not answers:
        return None, None
    _event_time(answers[0][0], "final answer")
    return answers[0]


def _first_action(calls: Sequence[ProviderCall], invocations: Sequence[AgrepInvocation],
                  final_event: Event | None) -> dict[str, Any]:
    first_call = calls[0] if calls else None
    if final_event is not None and (first_call is None
                                    or final_event.index < first_call.event.index):
        return {"kind": "final_answer", "trace_line": final_event.line}
    if first_call is None:
        return {"kind": "none", "trace_line": None}
    invocation = next((item for item in invocations if item.call is first_call), None)
    return {
        "kind": invocation.kind if invocation else "other_tool",
        "trace_line": first_call.event.line,
        "call_id": first_call.call_id,
        "provider_tool": first_call.name,
        "literal_commands": [command.text for command in first_call.commands],
    }


def _copy_status(recall: AgrepInvocation, calls: Sequence[ProviderCall],
                 invocations: Sequence[AgrepInvocation]) -> str:
    handles = _returned_handles(recall)
    if not handles:
        return "no_returned_full_handle"
    index = calls.index(recall.call)
    if index + 1 >= len(calls):
        return "no_next_tool_action"
    next_call = calls[index + 1]
    around = next((item for item in invocations if item.call is next_call
                   and item.kind == "around"), None)
    if around is None:
        return "next_action_not_around"
    if not around.has_full or not around.has_no_tools:
        return "around_missing_required_flags"
    if around.around_target in handles:
        return "exact"
    if around.around_target_status == "base_handle" and any(
            handle.rsplit(".", 1)[0] == around.around_target for handle in handles):
        return "shortened"
    malformed_matches = False
    if around.around_target_status == "full_handle_missing_at":
        malformed_matches = any(
            handle == f"@{around.around_target}" for handle in handles)
    elif around.around_target_status == "base_handle_missing_at":
        malformed_matches = any(
            handle.rsplit(".", 1)[0] == f"@{around.around_target}"
            for handle in handles)
    elif around.around_target_status == "legacy_session_turn":
        session, turn = str(around.around_target).split(" ", 1)
        malformed_matches = any(
            handle.rsplit(".", 1)[0] == f"@{session}:{turn}"
            for handle in handles)
    elif around.around_target_status == "session_only":
        malformed_matches = any(
            handle.split(":", 1)[0] == f"@{around.around_target}"
            for handle in handles)
    elif around.around_target_status == "session_only_with_at":
        malformed_matches = any(
            handle.split(":", 1)[0] == around.around_target
            for handle in handles)
    if malformed_matches:
        return str(around.around_target_status)
    return "different_handle"


def semantic_recall_status(trace_path: Path, tested_prompt: str) -> dict[str, Any]:
    """Classify only recall renderer diagnostics in one exact provider turn."""
    events = _load_events(trace_path)
    boundary = _find_boundary(events, {
        "id": "semantic-recall-status",
        "tested_prompt": tested_prompt,
    })
    events = _case_window(events, boundary)
    calls, pre_boundary_ids = _provider_calls(events, boundary)
    _attach_outputs(events, boundary, calls, pre_boundary_ids)
    invocations = _agrep_invocations(calls)
    recalls = [item for item in invocations if item.kind == "recall"]
    return {
        "total_agrep_count": len(invocations),
        "recall_count": len(recalls),
        "recalls": [_invocation_record(item) for item in recalls],
    }


def score_case(case: Mapping[str, Any], manifest_dir: Path) -> dict[str, Any]:
    case_id = case.get("id")
    trace_value = case.get("trace_path")
    authority = case.get("expected_authority")
    if not isinstance(case_id, str) or not case_id.strip():
        raise _error("manifest", "case id is missing")
    if not isinstance(trace_value, str) or not trace_value:
        raise _error(case_id, "trace_path is missing")
    if not isinstance(authority, str) or not authority.strip():
        raise _error(case_id, "expected_authority is missing")
    trace_path = Path(trace_value)
    if not trace_path.is_absolute():
        trace_path = manifest_dir / trace_path
    if trace_path.is_symlink():
        raise _error(case_id, "trace_path may not be a symlink")
    trace_path = trace_path.resolve()
    trace_sha256 = _sha256_file(trace_path)
    trace_size_bytes = trace_path.stat().st_size
    events = _load_events(trace_path)
    boundary = _find_boundary(events, case)
    events = _case_window(events, boundary)
    calls, pre_boundary_ids = _provider_calls(events, boundary)
    _attach_outputs(events, boundary, calls, pre_boundary_ids)
    invocations = _agrep_invocations(calls)
    recalls = [item for item in invocations if item.kind == "recall"]
    arounds = [item for item in invocations if item.kind == "around"]
    final_event, final_text = _final_answer(events, boundary)
    first = _first_action(calls, invocations, final_event)
    boundary_time = _event_time(boundary, "user boundary")
    first_time = None
    if calls and (final_event is None or calls[0].event.index < final_event.index):
        first_time = _event_time(calls[0].event, "first action")
    elif final_event is not None:
        first_time = _event_time(final_event, "first action")
    final_time = _event_time(final_event, "final answer") if final_event else None
    copy_statuses = [_copy_status(item, calls, invocations) for item in recalls]
    if first["kind"] in {"recall", "around"} or str(first["kind"]).startswith("agrep_"):
        observed_authority = "history"
    else:
        observed_authority = {
            "other_tool": "current_source",
            "final_answer": "visible_context",
            "none": "none",
        }[first["kind"]]
    return {
        "id": case_id,
        "trace_path": trace_value,
        "trace_sha256": trace_sha256,
        "trace_size_bytes": trace_size_bytes,
        "expected_authority": authority,
        "observed_authority": observed_authority,
        "authority_expectation_met": authority == observed_authority,
        "user_boundary": {
            "trace_line": boundary.line,
            "prompt_sha256": _sha256_text(_message_text(
                boundary.payload, f"user message line {boundary.line}")),
        },
        "first_action": first,
        "provider_tool_calls_launched": len(calls),
        "total_agrep_count": len(invocations),
        "agrep_invocations": [_invocation_record(item) for item in invocations],
        "recall_count": len(recalls),
        "around_count": len(arounds),
        "extra_recall_count": max(0, len(recalls) - 1),
        "extra_around_count": max(0, len(arounds) - 1),
        "recalls": [_invocation_record(item) for item in recalls],
        "arounds": [_invocation_record(item) for item in arounds],
        "handle_copy_statuses": copy_statuses,
        "exact_immediate_full_handle_copies": sum(
            status == "exact" for status in copy_statuses),
        "final_answer": None if final_event is None else {
            "trace_line": final_event.line,
            "text": final_text,
            "text_sha256": _sha256_text(final_text or ""),
        },
        "timing_ms": {
            "boundary_to_first_action": (
                _milliseconds(boundary_time, first_time, case_id)
                if first_time is not None else None),
            "boundary_to_final_answer": (
                _milliseconds(boundary_time, final_time, case_id)
                if final_time is not None else None),
        },
    }


def score_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise _error(str(path), "manifest is not a regular file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(str(path), "manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise _error(str(path), f"manifest schema_version must be {SCHEMA_VERSION}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise _error(str(path), "manifest cases must be a non-empty list")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or any(not isinstance(value, str) for value in ids):
        raise _error(str(path), "every case must be an object with a string id")
    if len(set(ids)) != len(ids):
        raise _error(str(path), "case ids are not unique")
    results = [score_case(case, path.parent.resolve()) for case in cases]
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cases": results,
        "summary": {
            "case_count": len(results),
            "authority_expectation_met": sum(
                case["authority_expectation_met"] for case in results),
            "first_action_recall": sum(
                case["first_action"]["kind"] == "recall" for case in results),
            "recall_launched": sum(case["recall_count"] > 0 for case in results),
            "total_agrep_calls": sum(case["total_agrep_count"] for case in results),
            "exact_immediate_full_handle_copy": sum(
                case["exact_immediate_full_handle_copies"] > 0 for case in results),
            "extra_recall_calls": sum(case["extra_recall_count"] for case in results),
            "extra_around_calls": sum(case["extra_around_count"] for case in results),
            "missing_final_answer": sum(
                case["final_answer"] is None for case in results),
        },
    }


def _write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="explicit case manifest")
    parser.add_argument("--output", type=Path, help="private local JSON output")
    parser.add_argument("--compact", action="store_true", help="omit indentation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = score_manifest(args.manifest)
    except (OSError, TraceScoreError) as exc:
        print(f"agentic trace score: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(
        report, ensure_ascii=False, sort_keys=True,
        indent=None if args.compact else 2) + "\n"
    try:
        if args.output is None:
            sys.stdout.write(text)
        else:
            _write_output(args.output.resolve(), text)
    except OSError as exc:
        print(f"agentic trace score: cannot write output: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
