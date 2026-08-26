"""Compatibility facade plus cross-cutting status and bounded-log helpers."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time

from hookless._log import (
    disable_timestamps as disable_log_timestamps,
    enable_timestamps as enable_log_timestamps,
    log,
)
from fileops import replace_with_retry
import ownerfile
# Process primitives live in proc.py (layer 0); re-exported so importers keep working.
from proc import (
    WIN,
    _MAX_PROCESS_ID,
    pid_alive,
    process_start_identity,
    _WINDOWS_CREATE_BREAKAWAY_FROM_JOB,
    _WINDOWS_JOB_BREAKAWAY_OK,
    _WINDOWS_JOB_KILL_ON_CLOSE,
    _WINDOWS_DESCENDANT_JOB_LIMITS,
    WINDOWS_DESCENDANT_TREE,
    windows_detached_child_flags,
    _windows_current_process_in_job,
    windows_background_child_flags,
    bind_descendants_to_process_lifetime,
    descendant_lifetime_contract,
    _process_group_present,
    _process_group_has_live_members,
    _process_group_active,
    _owner_group_active,
    _ProcessExitWitness,
    _watch_process_group_members,
    _original_group_survivor,
    terminate_exact_process,
    terminate_exact_process_tree,
    process_rss_bytes,
)
# The event-store reader and the resolved data dir live in events.py (layer 1);
# re-exported so importers keep working.
from events import (
    DEFAULT_DATA_DIR,
    DATA_DIR,
    data_dir_readonly,
    EVENT_STORE_NAME,
    EVENT_GENERATION_NAME,
    EVENT_FILENAME_MAX_BYTES,
    event_filename,
    event_path_candidates,
    _EVENT_READER_LOCAL,
    _close_event_reader,
    _event_metadata_stamp,
    _event_file_stamp,
    _event_fd_stamp,
    _event_payload_hash,
    _event_payload_digest,
    open_regular_binary,
    _event_generation_token,
    _event_reader_connection,
    event_store_blob,
    event_blob,
    event_exists,
    event_names,
    event_blobs_bulk,
    tool_literal_matches_from_payload,
    tool_event_identity,
    tool_search_record,
    tool_row_from_event,
    tool_search_text,
    tool_rows_from_payload,
)
# Terminal rendering primitives live in console.py (layer 0); re-exported so
# importers keep working.
from console import (
    DEBUG,
    dbg,
    lap,
    profile_report,
    timing_trace,
    enable_vt,
    color_enabled,
    utf8_stdio,
    terminal_safe,
    original_span_for_lowered,
    insensitive_span,
    literal_word_pattern,
    one_line,
    snip_at,
    snip_spans,
    age_label,
    PALETTE,
    paint,
    meter,
)
# Settings and data-dir provenance live in settings.py (layer 1); re-exported so
# importers keep working.
from settings import (
    data_dir_source,
    data_dir_warnings,
    setting,
)
# Distribution identity and ingest-binary resolution live in dist.py (layer 1);
# re-exported so importers keep working.
from dist import (
    PY_DIR,
    REPO_ROOT,
    package_version,
    distribution_build_id,
    runtime_build_id,
    ingest_bin as _resolved_ingest_bin,
    fetch_binary,
    _download_binary,
)


def ingest_bin():
    """Resolve the Rust executable without preparing or mutating writer state."""
    return _resolved_ingest_bin()


# Host resource probes and bounded logs live in resources.py (layer 1);
# re-exported so importers keep working.
import resources
from resources import (
    EMBEDDINGS_PATH,
    available_memory_fraction,
    battery_state,
    host_cpu_fraction,
    semantic_idle_seconds,
)
import surface_policy as surface
from index_lock import (
    INDEX_LOCK_PATH,
    _owner_field,
    _lock_holder_pid,
    _unlink_if_unchanged,
    IndexLock,
)


# These wrappers resolve DATA_DIR at call time so the long-standing test seam
# (rebinding common.DATA_DIR) keeps redirecting the footprint helpers.
def data_dir_usage() -> dict[str, int]:
    """Current on-disk footprint, evaluated only by explicit status/doctor calls."""
    return resources.data_dir_usage(DATA_DIR)


def open_bounded_log(name: str, *, max_bytes: int = 2 * 1024 * 1024,
                     backups: int = 2):
    """Open a private append log, rotating it before it grows without bound."""
    return resources.open_bounded_log(DATA_DIR / name, max_bytes=max_bytes,
                                      backups=backups, data_root=DATA_DIR)


class _LineStampWriter:
    """Prefix every line with a local-time stamp, tracebacks included."""

    def __init__(self, raw):
        self._raw = raw
        self._at_line_start = True

    def write(self, text: str) -> int:
        if not text:
            return 0
        parts: list[str] = []
        for line in text.splitlines(keepends=True):
            if self._at_line_start:
                parts.append(time.strftime("%Y-%m-%dT%H:%M:%S%z "))
            parts.append(line)
            self._at_line_start = line.endswith(("\n", "\r"))
        self._raw.write("".join(parts))
        return len(text)

    def __getattr__(self, name):
        return getattr(self._raw, name)


LOG_STAMP_ENV = "AGREP_LOG_STAMP"


def stamp_stdio_lines() -> None:
    """Timestamp this process's stdout/stderr lines when the parent asked.

    Background embed children write straight into semantic-embed.log via an
    inherited fd, so an undated failure there cannot be placed in time.
    Parents set LOG_STAMP_ENV on the spawn; direct/manual runs and test captures
    stay byte-identical."""
    import sys
    if os.environ.get(LOG_STAMP_ENV) != "1":
        return
    sys.stdout = _LineStampWriter(sys.stdout)
    sys.stderr = _LineStampWriter(sys.stderr)

# numpy is imported lazily so core indexing and keyword search run on pure stdlib.
SEMANTIC_DEFAULT_EXCLUDED_ROLES = surface.SEMANTIC_DEFAULT_EXCLUDED_ROLES

_INGEST_SIGNATURE_MAX_BYTES = 4096


def committed_message_total() -> int | None:
    """The message total the published generation committed.

    ``.ingest.sig`` is the commit marker and carries that total as its
    leading field, so a corpus size stays knowable while the session-family
    proof is withdrawn mid-republish. None for an absent or malformed
    marker: an unprovable size still claims nothing."""
    try:
        raw = (DATA_DIR / ".ingest.sig").read_bytes()
    except OSError:
        return None
    if len(raw) > _INGEST_SIGNATURE_MAX_BYTES:
        return None
    try:
        total, separator, _content = raw.decode("utf-8").strip().partition(":")
        if not separator:
            return None
        value = int(total)
    except (UnicodeError, ValueError):
        return None
    return value if value >= 0 else None


def index_summary(*, deadline: float | None = None) -> dict | None:
    """Proof-bound corpus counts, or None before ingest or during damage/movement."""
    def budget_check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("index summary budget exceeded")

    def family_census():
        if deadline is None:
            return read_session_family_census()
        return read_session_family_census(deadline=deadline)

    budget_check()
    sj = DATA_DIR / "sessions.jsonl"
    census = family_census()
    if census is None:
        return None
    n_sessions = n_messages = 0
    # Per-agent messages, sessions, and newest last_ts share this one proof pass.
    per: dict[str, list[int]] = {}
    try:
        budget_check()
        with sj.open("rb") as f:
            while True:
                budget_check()
                wire = f.readline(SESSION_JSONL_ROW_MAX_BYTES + 1)
                budget_check()
                if not wire:
                    break
                if (len(wire) > SESSION_JSONL_ROW_MAX_BYTES
                        or not wire.endswith(b"\n")):
                    return None
                line = wire.decode("utf-8").strip()
                if not line:
                    continue
                budget_check()
                o = json.loads(line)
                budget_check()
                if not isinstance(o, dict):
                    return None
                session = o.get("session")
                count = o.get("n")
                agent = o.get("agent")
                if (not isinstance(session, str)
                        or session not in census.sessions
                        or type(count) is not int or count < 0
                        or not isinstance(agent, str) or not agent):
                    return None
                n_sessions += 1
                n_messages += count
                counts = per.setdefault(agent, [0, 0])
                counts[0] += count
                counts[1] += 1
    except TimeoutError:
        raise
    except (OSError, RecursionError, UnicodeError, ValueError, TypeError):
        return None
    raw_total, separator, _content_signature = (
        census.proof.ingest_signature.partition(":"))
    try:
        expected_messages = int(raw_total)
    except ValueError:
        return None
    if (not separator or expected_messages < 0
            or n_messages != expected_messages
            or n_sessions != len(census.sessions)
            or family_census() is not census):
        return None
    budget_check()
    d: dict = {"sessions": n_sessions, "messages": n_messages, "agents": sorted(per),
               "per_agent": [{"agent": a, "messages": m, "sessions": s}
                             for a, (m, s) in sorted(
                                 per.items(), key=lambda item: -item[1][0])]}
    try:
        budget_check()
        if MESSAGES_PATH.exists():
            d["age_s"] = int(time.time() - MESSAGES_PATH.stat().st_mtime)
    except TimeoutError:
        raise
    except OSError:
        return None
    return d


def detected_stores(
        *, timeout_s: float = 10.0,
        observation: dict | None = None) -> list[dict]:
    """Present stores recognized by Rust but not supported by an ingest adapter.

    The registry is authoritative, so Python asks ``agrep-rs detect`` instead of
    duplicating its probes. Detection remains optional and returns an empty
    list on failure for legacy callers. Diagnostics may supply ``observation``
    to distinguish a verified empty list from timeout or unreadable output.
    """
    def observed(state: str, detail: str) -> None:
        if observation is not None:
            observation.clear()
            observation.update(state=state, detail=detail)

    binp = ingest_bin()
    if not binp.exists():
        observed("unavailable", "ingest binary is unavailable")
        return []
    kw = {"capture_output": True, "text": True, "timeout": timeout_s}
    if WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run([str(binp), "detect"], **kw)
        if r.returncode != 0:
            observed(
                "unavailable",
                f"ingest-registry detection exited {r.returncode}")
            return []
        out = json.loads(r.stdout)
        if (not isinstance(out, list)
                or any(
                    not isinstance(row, dict)
                    or not isinstance(row.get("name"), str)
                    or not row["name"]
                    or type(row.get("count")) is not int
                    or row["count"] < 0
                    for row in out)):
            observed("unavailable", "ingest-registry detection output is malformed")
            return []
        observed("complete", "bounded ingest-registry detection completed")
        return out
    except subprocess.TimeoutExpired:
        observed(
            "budget-exceeded",
            "unsupported-store detection exceeded its bounded routine budget")
        return []
    except Exception as exc:  # noqa: BLE001 -- detection is best-effort
        observed(
            "unavailable",
            "ingest-registry detection is unavailable: "
            f"{type(exc).__name__}")
        return []


def store_freshness() -> list[dict]:
    """Per-adapter store presence + newest file mtime,
    `[{"name","files","newest_mtime_ms","mtime_tracks_content"}]`
    via `agrep-rs stores` - the registry owns discovery, so python never re-learns store
    paths. Empty on any failure (no binary, old binary without the subcommand)."""
    binp = ingest_bin()
    if not binp.exists():
        return []
    kw = {"capture_output": True, "text": True, "timeout": 30}
    if WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run([str(binp), "stores"], **kw)
        if r.returncode != 0:
            return []
        out = json.loads(r.stdout)
        return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001 -- the canary is enrichment, never fatal
        return []

# rewritten (mtime refreshed) by every ingest run even when unchanged -
# the CLI's "last freshened" clock (_sync_freshen).
INGEST_SIG_PATH = DATA_DIR / ".ingest.sig"


# Session-family publication and caller identity live in session_context.py (layer 2);
# re-exported so importers keep working.
from session_context import (
    transcript_generation,
    LegacyPublication,
    TranscriptPublicationRace,
    SESSION_FAMILY_INDEX_VERSION,
    SESSION_FAMILY_DERIVATION_VERSION,
    SESSION_FAMILY_DIGEST_ALGORITHM,
    SESSION_JSONL_ROW_MAX_BYTES,
    SESSION_FAMILY_META_FILE,
    SESSION_FAMILY_MISSING_STAMP,
    SESSION_PREFIX_MAX_CANDIDATES,
    SIDECHAIN_SESSION_PREFIX,
    is_sidechain_session,
    is_side_session,
    session_family_digest,
    read_session_family_census,
    session_family_source_stamp,
    strict_family_parent_map,
    await_family_publication,
    CallingFamily,
    CallerIdentity,
    SelfExclusion,
    family_root,
    _open_session_family_index,
    indexed_calling_family,
    indexed_calling_family_with_sides,
    indexed_family_metadata,
    indexed_family_roots,
    indexed_session_matches,
    indexed_session_prefix_candidates,
    indexed_session_prose_count,
    indexed_self_exclusion_has_rows,
    cli_name,
    KNOWN_AGENTS,
    normalize_agent_name,
    match_session_ids,
    agent_filter_error,
    in_agent_context,
    calling_identity,
    calling_session,
    calling_family,
    calling_self_exclusion,
    self_exclusion_unavailable_notice,
    setup_hint,
)

# Embedding identity and legacy matrix publication live in embedding_store.py;
# re-exported so importers keep working.
from embedding_store import (
    EMBED_DIM,
    MESSAGES_PATH,
    IDS_PATH,
    semantic_text_hash,
    semantic_chunk_split,
    Message,
    iter_messages,
    write_index_meta,
    read_index_meta,
    _embedding_identity_matches,
    read_embedding_commit,
    embedding_artifact_identity,
    embedding_artifact_state,
    committed_embedding_artifact_state,
    embedding_matrix_identity,
    embedding_commit_identity,
    write_embedding_commit,
    _EMBEDDING_PUBLISH_RECORD_BYTES,
    _EMBEDDING_PUBLISH_RELEASE_DELAYS,
    EmbeddingPublishLock,
    EmbeddingPublicationGuard,
    _write_embedding_publication_barrier,
    ensure_embedding_commit,
    embedding_temp_path,
    _prune_embedding_temps,
    write_embeddings,
    _embedding_snapshot_name,
    _prune_embedding_snapshots,
    close_embedding_matrix,
    read_embeddings,
)
