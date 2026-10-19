"""Record shape signatures and the per-adapter source oracle shared by the sampler and the gates."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

RECURSE_KEYS = frozenset(("message", "payload", "item", "content", "summary", "data"))
DISCRIMINATORS = frozenset((
    "type", "role", "userType", "isSidechain", "isMeta", "synthetic", "isCompactSummary"))
JSONL_ADAPTERS = ("claude", "codex", "pi")
STAMP_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[^/]+\.jsonl(?:\.gz)?$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

WRAPPER_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command", "<bash-input>",
    "<bash-stdout>", "<user-prompt-submit-hook>", "Caveat:", "<system-reminder>",
    "<teammate-message", "<task-notification", "[SYSTEM NOTIFICATION", "<system-notification",
)


def _leaf(value) -> str:
    if isinstance(value, bool):
        return "b"
    if value is None:
        return "n"
    if isinstance(value, (int, float)):
        return "#"
    if isinstance(value, str):
        return "s"
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return "?"


def shape(value) -> str:
    """Record type plus sorted key set, recursing only through message/content containers."""
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            child = value[key]
            if key in DISCRIMINATORS and isinstance(child, (str, bool)):
                items.append(f"{key}={child}")
            elif key in RECURSE_KEYS and isinstance(child, (dict, list)):
                items.append(f"{key}:{shape(child)}")
            else:
                items.append(f"{key}:{_leaf(child)}")
        return "{" + ",".join(items) + "}"
    if isinstance(value, list):
        return "[" + "|".join(sorted({shape(child) for child in value})) + "]"
    return _leaf(value)


def file_class(adapter: str, relative: Path) -> str:
    """Where a file sits in its store's grammar; sidecar grammar is a shape dimension."""
    parts = relative.parts
    if adapter == "claude":
        return "side" if "subagents" in parts else "root"
    if adapter == "codex":
        return "root"
    if adapter == "pi":
        if len(parts) <= 2:
            return "root"
        if parts[-1] == "__advisor.jsonl":
            return "advisor" if len(parts) == 3 else "nested-advisor"
        return "side" if len(parts) == 3 else "nested-side"
    return "root"


def is_wrapper(text: str) -> bool:
    head = text.lstrip()
    return (head.startswith(WRAPPER_PREFIXES)
            or ("<command-name>" in head and "</command-name>" in head))


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def codex_text(blocks) -> str:
    parts = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def classify(adapter: str, record, side: bool = False) -> str:
    """Source-side class of one record; `side` marks a Claude subagents/ transcript, whose sidechain rows are content."""
    if not isinstance(record, dict):
        return "other"
    if adapter == "pi":
        kind = record.get("type")
        if kind == "session":
            return "header"
        if kind == "compaction":
            return "compaction"
        if kind == "message":
            message = record.get("message")
            if not isinstance(message, dict):
                return "other"
            role = message.get("role")
            if role == "user":
                if message.get("synthetic") is True:
                    return "synthetic_user"
                text = text_of(message.get("content"))
                if not text.strip():
                    return "empty_user"
                return "wrapper" if is_wrapper(text) else "user"
            if role == "assistant":
                return "assistant"
            if role == "toolResult":
                return "tool_result"
            return "other"
        return "other"
    if adapter == "claude":
        message = record.get("message")
        if not isinstance(message, dict):
            return "header" if "cwd" in record else "other"
        kind = record.get("type")
        role = message.get("role")
        if record.get("isSidechain") is True and not side:
            return "sidechain"
        if kind == "user" and role == "user":
            if record.get("isMeta") is True:
                return "meta"
            content = message.get("content")
            user_type = record.get("userType")
            if user_type not in (None, "external"):
                return "non_human"
            text = text_of(content)
            if not text.strip():
                if isinstance(content, list) and any(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in content):
                    return "tool_result"
                return "empty_user"
            return "wrapper" if is_wrapper(text) else "user"
        if kind == "assistant" and role == "assistant":
            return "assistant"
        return "other"
    if adapter == "codex":
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "session_meta":
            return "header"
        if kind == "turn_context":
            return "context"
        if kind == "compacted":
            return "compaction"
        if not isinstance(payload, dict):
            return "other"
        if kind == "event_msg":
            if payload.get("type") == "user_message":
                return "submission"
            item = payload.get("item")
            if (payload.get("type") == "item_completed" and isinstance(item, dict)
                    and item.get("type") == "UserMessage"):
                return "submission"
            return "other"
        if kind == "response_item":
            if payload.get("type") == "message":
                role = payload.get("role")
                if role == "user":
                    text = codex_text(payload.get("content"))
                    if not text.strip():
                        return "empty_user"
                    return "wrapper" if is_wrapper(text) else "user"
                if role == "assistant":
                    return "assistant"
                return "non_human"
            if payload.get("type") == "agent_message":
                return "handoff"
            return "other"
        return "other"
    return "other"


def submission_texts(records) -> tuple[list[str], set[tuple[str, str, str]]]:
    """Legacy prefix-attesting texts and Desktop (thread, turn, text) triples of a codex rollout."""
    legacy: list[str] = []
    desktop: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                legacy.append(message.strip())
        elif payload.get("type") == "item_completed":
            item = payload.get("item")
            thread = payload.get("thread_id")
            turn = payload.get("turn_id")
            if (isinstance(item, dict) and item.get("type") == "UserMessage"
                    and isinstance(thread, str) and thread and isinstance(turn, str) and turn):
                parts = [block.get("text") for block in item.get("content") or []
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str)]
                text = "\n".join(parts).strip()
                if text:
                    desktop.add((thread, turn, text))
    return legacy, desktop


def codex_attested(record, legacy: list[str], desktop: set[tuple[str, str, str]],
                   session: str | None) -> bool:
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return False
    text = codex_text(payload.get("content")).strip()
    if not text:
        return False
    if any(text.startswith(candidate) for candidate in legacy):
        return True
    turn = payload.get("turn_id")
    if not isinstance(turn, str):
        meta = payload.get("internal_chat_message_metadata_passthrough")
        turn = meta.get("turn_id") if isinstance(meta, dict) else None
    return bool(session and isinstance(turn, str) and (session, turn, text) in desktop)


def iter_records(path: Path, limit: int | None = None):
    """(ordinal, raw line, parsed or None) for every non-blank line; gzip pi sessions included.
    `limit` reads only the first `limit` bytes of a plain file (the extent an older tally covered)."""
    opener = open
    if path.name.endswith(".gz"):
        import gzip
        opener = gzip.open
    with opener(path, "rb") as handle:
        if limit is not None:
            handle = io.BytesIO(handle.read(limit))
        for ordinal, raw in enumerate(handle):
            if not raw.strip():
                continue
            try:
                yield ordinal, raw, json.loads(raw)
            except ValueError:
                yield ordinal, raw, None


def oracle_counts(adapter: str, path: Path, limit: int | None = None) -> dict[str, int]:
    """Independent per-file census: how many records of each source class the file holds."""
    counts: dict[str, int] = {"seen": 0, "malformed": 0}
    records = []
    side = adapter == "claude" and "subagents" in path.parts
    for _ordinal, _raw, record in iter_records(path, limit):
        counts["seen"] += 1
        if record is None:
            counts["malformed"] += 1
            continue
        records.append(record)
        kind = classify(adapter, record, side)
        counts[kind] = counts.get(kind, 0) + 1
    if adapter == "codex":
        legacy, desktop = submission_texts(records)
        session = None
        for record in records:
            if isinstance(record, dict) and record.get("type") == "session_meta":
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    session = payload["id"]
                break
        attested = sum(1 for record in records
                       if classify("codex", record) == "user"
                       and codex_attested(record, legacy, desktop, session))
        counts["attested_user"] = attested
    return counts


def text_bearing_bound(adapter: str, counts: dict[str, int]) -> int:
    """Upper bound on rows the adapter may emit: only these source classes can become rows."""
    if adapter == "pi":
        return counts.get("user", 0) + counts.get("assistant", 0) + counts.get("compaction", 0)
    if adapter == "claude":
        return counts.get("user", 0)
    if adapter == "codex":
        return counts.get("user", 0) + counts.get("compaction", 0) + counts.get("handoff", 0)
    return counts.get("seen", 0)


def source_session_ids(adapter: str, path: Path, limit: int = 400) -> set[str]:
    """Session ids a file can publish under: header/record ids plus the filename id."""
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    ids = {name}
    if adapter == "pi" and "_" in name:
        ids.add(name.rsplit("_", 1)[1])
    if adapter == "codex":
        parts = name.split("-")
        if len(parts) >= 5:
            ids.add("-".join(parts[-5:]))
    for ordinal, (_offset, _raw, record) in enumerate(iter_records(path)):
        if ordinal >= limit:
            break
        if not isinstance(record, dict):
            continue
        if adapter == "pi":
            if record.get("type") == "session" and isinstance(record.get("id"), str):
                ids.add(record["id"])
                break
        elif adapter == "claude":
            if isinstance(record.get("sessionId"), str):
                ids.add(record["sessionId"])
        elif adapter == "codex":
            if record.get("type") == "session_meta":
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    ids.add(payload["id"])
                break
    return ids
