#!/usr/bin/env python3
"""Deterministic, disposable million-row search scale benchmark.

The default campaign builds and removes one 1M-row corpus, then one 2M-row
corpus. Every timed search is a fresh Python process and runs through
``cli.py`` through complete process exit, including the normal freshness check,
with the production SQLite schema, matcher, ranker, renderer, and exit contract.
The derived-index build begins from materialized messages; it is not raw-store
ingest. No semantic model or user corpus is touched.

    python bench/scale.py
    python bench/scale.py --rows 1000000 2000000 --runs 5
    python bench/scale.py --quick --runs 1
    python bench/scale.py --json --rows 1000000
    python bench/scale.py --check --runs 3
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "py"
SPECIAL_ROWS = 64
SPECIAL_FAMILIES = 16
SHORT_DECOYS = 12_500
DEFAULT_ROWS = (1_000_000, 2_000_000)
QUICK_ROWS = (10_000, 25_000)
SCALE_BUDGETS_MS = {
    1_000_000: {
        "miss": 100, "selective": 120, "broad_single": 300, "short2": 300,
        "short_interior": 350, "broad_phrase": 600, "hybrid_terms": 1_100,
        "hybrid_chats": 1_100, "all_terms": 1_100, "terms_chats": 1_100,
        "old_mid_density": 350, "old_dense": 1_200, "compact": 300, "count": 600,
    },
    2_000_000: {
        "miss": 100, "selective": 120, "broad_single": 400, "short2": 400,
        "short_interior": 450, "broad_phrase": 1_000, "hybrid_terms": 1_700,
        "hybrid_chats": 1_700, "all_terms": 1_700, "terms_chats": 1_700,
        "old_mid_density": 500, "old_dense": 2_300, "compact": 400, "count": 1_000,
    },
}
SCALE_CAMPAIGN_BUDGETS = {
    1_000_000: {
        "index_build_ms": 20_000, "index_build_rss_mib": 128,
        "db_mib": 550, "bytes_per_row": 850, "query_rss_mib": 320,
    },
    2_000_000: {
        "index_build_ms": 45_000, "index_build_rss_mib": 128,
        "db_mib": 1_100, "bytes_per_row": 850, "query_rss_mib": 450,
    },
}
# Two hosted release runs needed 3.22x and 3.12x target latency; 3.25x
# covers that band without changing the hardware-independent target budgets.
PORTABLE_LATENCY_SLACK = 3.25

SCALE_BUDGET_PROFILES = {
    "target": (SCALE_BUDGETS_MS, SCALE_CAMPAIGN_BUDGETS),
    "portable-ci": (
        {rows: {name: budget * PORTABLE_LATENCY_SLACK
                for name, budget in budgets.items()}
         for rows, budgets in SCALE_BUDGETS_MS.items()},
        {
            1_000_000: {
                "index_build_ms": 60_000, "index_build_rss_mib": 256,
                "db_mib": 550, "bytes_per_row": 850, "query_rss_mib": 512,
            },
            2_000_000: {
                "index_build_ms": 135_000, "index_build_rss_mib": 256,
                "db_mib": 1_100, "bytes_per_row": 850, "query_rss_mib": 768,
            },
        },
    ),
}
MIB = 1024 * 1024
GIB = 1024 * MIB
MISS_QUERY = "zzscale_missing_7d4c9a"
SELECTIVE_QUERY = "neon glacier sentinel"
BROAD_QUERY = "the"
SHORT_QUERY = "hi"
SHORT_INTERIOR_QUERY = "zz"
PHRASE_QUERY = "race condition"
TERMS_QUERY = "alpha omega"
HYBRID_QUERY = "the omega"
HYBRID_PHRASES = 8
MID_OLD_QUERY = "midtail"
OLD_DENSE_QUERY = "deepold"
RUST_BIN = ROOT / "target" / "release" / ("agrep-rs.exe" if os.name == "nt" else "agrep-rs")
_RESOURCE_MODULE = None
_SEARCH_DONE = re.compile(
    r"search done: (?P<total>\d+) hit\(s\) in (?P<chats>\d+) chat\(s\) via (?P<engine>[^;]+);")
_SHORT_EXAMINED = re.compile(r"bounded short rows: examined (?P<count>\d+) candidate\(s\)")
_ROUTE_ROWS = re.compile(
    r"bounded rows: examined (?P<examined>\d+) candidate\(s\), scored (?P<scored>\d+), "
    r"observed (?P<observed>\d+) hit\(s\).* exact=(?P<exact>True|False)")
_ROUTE_SHORT = re.compile(
    r"bounded short rows: examined (?P<examined>\d+) candidate\(s\), "
    r"observed (?P<observed>\d+) hit\(s\).* exact=(?P<exact>True|False)")
_ROUTE_SESSIONS = re.compile(
    r"bounded sessions: examined (?P<examined>\d+) candidate\(s\), "
    r"observed (?P<observed>\d+) hit\(s\).* exact=(?P<exact>True|False)")
_DENSE_PREFLIGHT = re.compile(
    r"dense phrase preflight: examined (?P<examined>\d+) row\(s\).+ "
    r"in (?P<ms>[\d.]+)ms")
_BOUNDARY_BATCH = re.compile(
    r"boundary: Rust scored (?P<items>\d+) occurrence\(s\) in (?P<ms>[\d.]+)ms")
EARLY_STOP_CASES = frozenset({
    "broad_single", "short2", "broad_phrase", "hybrid_terms", "all_terms",
    "hybrid_chats", "terms_chats", "compact",
})


@dataclass(frozen=True)
class Case:
    name: str
    query: str
    args: tuple[str, ...]
    required_markers: tuple[str, ...]
    validate: Callable[[subprocess.CompletedProcess[str], int], dict[str, object]]
    profile: str | None = None


def _private_env(home: Path, data: Path) -> dict[str, str]:
    env = dict(os.environ)
    # calling_identity reads CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID and
    # AGREP_PI_SESSION_ID; leaking any one lets self-exclusion hide a row.
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
                 "XDG_CONFIG_HOME", "AGREP_PROFILE", "AGREP_RS_BIN", "CODEX_THREAD_ID",
                 "CLAUDE_CODE_SESSION_ID", "AGREP_PI_SESSION_ID",
                 "CODEX_SANDBOX", "CLAUDECODE",
                 "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE", "GEMINI_CLI",
                 "CLINE_ACTIVE", "CURSOR_AGENT", "CLINE_DIR", "OPENCODE_DB", "OMPCODE"):
        env.pop(name, None)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "AGREP_HOME": str(home),
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_NO_DAEMON": "1",
        "AGREP_RS_BIN": str(RUST_BIN),
        "PYTHONHASHSEED": "0",
    })
    return env


def _base_text(ordinal: int) -> str:
    joiners = ("race_condition", "race-condition", "race.condition",
               "race/condition", "raceCondition", "RACE_condition")
    middles = ("payload", "adapter", "cache", "parser")
    return (f"the this fzzg {joiners[ordinal % len(joiners)]} alpha "
            f"{middles[(ordinal // len(joiners)) % len(middles)]} omega")


def _special_text(index: int) -> str:
    text = ("hi hi hi the the the race_condition race_condition "
            f"alpha payload omega alpha payload omega fixture_variant_{index:03d}")
    if index < HYBRID_PHRASES:
        text += " the_omega"
    if index == SPECIAL_ROWS - 1:
        text += " neon_glacier_sentinel"
    return text


def _content_digest(text: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in (text or "").encode("utf-8"):
        digest = ((digest ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{digest & 0xFFFF:04x}"


def _session_family_meta(rows: list[dict[str, object]], signature: str) -> dict:
    py_path = str(PY)
    if py_path not in sys.path:
        # bench/resources.py must remain the scale sampler, not py/resources.py.
        sys.path.insert(min(1, len(sys.path)), py_path)
    import session_context

    pairs = sorted(
        (str(row["session"]), str(row.get("parent") or "")) for row in rows)
    return {
        "version": session_context.SESSION_FAMILY_INDEX_VERSION,
        "algorithm": session_context.SESSION_FAMILY_DIGEST_ALGORITHM,
        "ingest_signature": signature,
        "count": len(rows),
        "digest": session_context.session_family_digest(pairs),
    }


def _write_fixture(data: Path, rows: int) -> dict[str, object]:
    digests: dict[str, str] = {}

    def content_digest(text: str) -> str:
        digest = digests.get(text)
        if digest is None:
            digest = _content_digest(text)
            digests[text] = digest
        return digest

    started = time.perf_counter()
    cpu_started = time.process_time()
    now_ms = int(time.time() * 1000)
    base_rows = rows - SPECIAL_ROWS
    base_sessions = min(65_536, max(256, base_rows // 16))
    short_decoys = min(SHORT_DECOYS, max(0, base_rows - 128))
    mid_old_count = min(max(6_000, rows // 50), max(1, base_rows - 2_048))
    mid_old_start = base_rows - mid_old_count
    old_dense_count = max(1, base_rows // 3)
    old_dense_start = base_rows - old_dense_count
    messages = data / "messages.jsonl"
    with messages.open("w", encoding="utf-8", newline="\n", buffering=MIB) as stream:
        for ordinal in range(base_rows):
            session_no = ordinal % base_sessions
            session = f"scale-base-{session_no:06d}"
            who = ("user" if ordinal < short_decoys or ordinal >= mid_old_start else
                   ("user", "agent", "subagent", "tool")[ordinal % 4])
            text = _base_text(ordinal)
            # The possessive defeats the ASCII certification fast path, so
            # these lanes keep measuring the native scorer their budgets were
            # set against; the trigram index still matches the bare token.
            if ordinal >= old_dense_start:
                text += f" {OLD_DENSE_QUERY}'s"
            if ordinal >= mid_old_start:
                text += f" {MID_OLD_QUERY}'s"
            timestamp = (now_ms - 365 * 86_400_000 - (ordinal - mid_old_start) * 1000
                         if ordinal >= mid_old_start else
                         now_ms - 180 * 86_400_000 - (ordinal - old_dense_start) * 1000
                         if ordinal >= old_dense_start else
                         now_ms - ordinal * 1000 if ordinal < short_decoys else
                         now_ms - 45 * 86_400_000 - (ordinal % 4096) * 1000)
            row = {
                "id": f"scale:{session}:{ordinal // base_sessions}",
                "session": session,
                "agent": ("codex", "claude", "cursor")[ordinal % 3],
                "project": f"/scale/project-{session_no % 32:02d}",
                "model": "scale-fixture",
                "model_source": "explicit",
                "turn": ordinal // base_sessions,
                "ts": timestamp,
                "who": who,
                "text": text,
                "content_digest": content_digest(text),
            }
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        for index in [*range(SPECIAL_ROWS - 2, -1, -1), SPECIAL_ROWS - 1]:
            session = f"scale-child-{index:03d}"
            text = _special_text(index)
            row = {
                "id": f"scale:{session}:9000000",
                "session": session,
                "agent": "codex",
                "project": f"/scale/winner-{index // 4:02d}",
                "model": "scale-fixture",
                "model_source": "explicit",
                "turn": 9_000_000,
                "ts": now_ms - 86_400_000,
                "who": "user",
                "text": text,
                "content_digest": content_digest(text),
            }
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    session_rows = []
    for index in range(SPECIAL_ROWS):
        session_rows.append({
            "session": f"scale-child-{index:03d}",
            "parent": f"scale-root-{index // 4:03d}",
            "agent": "codex",
            "project": f"/scale/winner-{index // 4:02d}",
            "n": 1,
            "first_ts": now_ms,
            "last_ts": now_ms,
            "first_text": _special_text(index)[:120],
        })
    signature = f"scale-v1:{rows}:{now_ms}"
    (data / "session_family.meta.json").write_text(
        json.dumps(_session_family_meta(session_rows, signature),
                   separators=(",", ":")),
        encoding="utf-8",
    )
    sessions = data / "sessions.jsonl"
    sessions.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n"
                for row in session_rows),
        encoding="utf-8",
    )
    (data / "replies.jsonl").write_text("", encoding="utf-8")
    # The derived-generation proof census covers these artifacts too. The
    # synthetic corpus has no compaction boundaries and no tool events, so
    # valid-empty stats keep the freshness proof honest without real ingest.
    (data / "boundary_stats.json").write_text(
        json.dumps({"schema": 2, "generation": signature,
                    "families": 0, "tokens": {}}, separators=(",", ":")),
        encoding="utf-8",
    )
    (data / ".boundary_stats.bin").write_bytes(b"")
    (data / "event_stats.json").write_text(
        json.dumps({"total": 0, "fails": 0, "subagents": 0,
                    "by_agent": {}, "by_tool": []}, separators=(",", ":")),
        encoding="utf-8",
    )
    (data / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
    return {
        "rows": rows,
        "base_sessions": base_sessions,
        "special_rows": SPECIAL_ROWS,
        "special_families": SPECIAL_FAMILIES,
        "short_decoys": short_decoys,
        "mid_old_rows": mid_old_count,
        "old_dense_rows": old_dense_count,
        "selective_session": f"scale-child-{SPECIAL_ROWS - 1:03d}",
        "source_bytes": sum(path.stat().st_size for path in (
            messages, sessions, data / "session_family.meta.json",
            data / "replies.jsonl", data / ".ingest.sig",
            data / "boundary_stats.json", data / ".boundary_stats.bin",
            data / "event_stats.json")),
        "wall_ms": round((time.perf_counter() - started) * 1000, 3),
        "cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
    }


def _build_database(data: Path) -> int:
    sys.path.insert(0, str(PY))
    import corpusdb
    import indexd_runtime

    build_id = indexd_runtime.derived_writer_build_id(require_binary=True)

    db = sqlite3.connect(data / "corpus.db")
    try:
        db.executescript(
            "PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF;" + corpusdb._SCHEMA_SQL)
        insert = corpusdb._INS_DIGEST
        batch: list[tuple[object, ...]] = []
        known_sessions: set[str] = set()
        with (data / "messages.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                known_sessions.add(str(row["session"]))
                batch.append((
                    row["session"], row["turn"], row["ts"], row["agent"],
                    row["project"], "", row["model"], row["model_source"],
                    row["who"], row["text"], row["content_digest"],
                ))
                if len(batch) == 8192:
                    db.executemany(insert, batch)
                    batch.clear()
        if batch:
            db.executemany(insert, batch)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        db.executescript(corpusdb._TRIGGERS_SQL)
        row_count = int(db.execute("SELECT count(*) FROM msgs").fetchone()[0])
        family_count = min(65_536, max(256, (row_count - SPECIAL_ROWS) // 16))
        total_families = family_count + SPECIAL_FAMILIES
        db.executemany(
            "INSERT INTO boundary_stats(token, n, s, q) VALUES(?, ?, ?, ?)",
            (("the", total_families, total_families, 2),
             ("hi", total_families, SPECIAL_FAMILIES, 2),
             ("race", total_families, total_families, 2),
             (SHORT_INTERIOR_QUERY, family_count, 0, 0)),
        )
        family_snapshot = corpusdb._read_session_families()
        corpusdb._replace_session_families(
            db, family_snapshot, known_sessions)
        stamp = corpusdb._stamp()
        db.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            (("stamp", stamp), ("schema", corpusdb._SCHEMA),
             ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
             ("build_id", build_id)),
        )
        db.commit()
    finally:
        db.close()
    owner = data / ".derived-owner.json"
    owner.write_text(json.dumps(
        {"version": 1, "build_id": build_id}, separators=(",", ":")),
        encoding="utf-8")
    if os.name != "nt":
        owner.chmod(0o600)
    _write_generation_proof(data)
    return 0


def _write_generation_proof(data: Path) -> None:
    """Commit the derived-generation proof the freshness verdict demands.

    Mirrors the Rust derived writer bit-for-bit via corpusdb's own proof
    helpers, so both the Python and Rust freshness validators read this
    synthetic corpus as a complete, current generation.
    """
    import corpusdb

    signature = (data / ".ingest.sig").read_text(encoding="utf-8").strip()
    files = []
    for name in corpusdb._DERIVED_PROOF_NAMES:
        path = data / name
        identity = corpusdb._proof_file_identity(path)
        if os.name == "posix":
            token: dict[str, object] = {
                "Metadata": corpusdb._unix_change_token(identity[2])}
        elif os.name == "nt":
            try:
                token = {"Metadata": corpusdb._windows_file_state(
                    path, include_usn=True)[1]}
            except OSError:
                token = {"ContentSha256": list(
                    corpusdb._content_sha256(path, identity))}
        else:
            token = {"Metadata": 0}
        files.append({
            "name": name,
            "len": identity[0],
            "modified_ns": identity[1],
            "change_token": token,
            "edge_hash": corpusdb._edge_hash(path, identity[0], identity),
        })
    (data / ".derived_generation.json").write_text(
        json.dumps({"version": corpusdb._DERIVED_PROOF_VERSION,
                    "signature": signature, "files": files},
                   separators=(",", ":")),
        encoding="utf-8",
    )


def _write_live_store(home: Path) -> None:
    """Give the census a live codex store matching the published agent set.

    The fixture's sessions.jsonl publishes agent "codex"; without a live
    store the record-less drift fallback counts it as vanished and every
    zero-hit query hedges to exit 2. One old rollout file is a complete
    store to the census (it reads file counts and mtimes, never content).
    """
    day = home / ".codex" / "sessions" / "2020" / "01" / "01"
    day.mkdir(parents=True)
    rollout = day / "rollout-2020-01-01T00-00-00-scale.jsonl"
    rollout.write_text("", encoding="utf-8")
    moment = 1_577_836_800  # 2020-01-01, firmly older than any ingest signal
    os.utime(rollout, (moment, moment))


def _resource_module():
    global _RESOURCE_MODULE
    if _RESOURCE_MODULE is None:
        path = Path(__file__).with_name("resources.py")
        spec = importlib.util.spec_from_file_location("agrep_scale_resources", path)
        module = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            raise RuntimeError("could not load the resource measurement harness")
        spec.loader.exec_module(module)
        _RESOURCE_MODULE = module
    return _RESOURCE_MODULE


def _drain_stream(stream, name: str, sink: dict[str, str]) -> None:
    """Read one child pipe to EOF so the child never blocks on a full buffer."""
    try:
        sink[name] = stream.read()
    except (ValueError, OSError):
        sink.setdefault(name, "")
    finally:
        try:
            stream.close()
        except (ValueError, OSError):
            pass


def _sampled_process(cmd: list[str], *, cwd: Path, env: dict[str, str],
                     timeout: float, interval: float,
                     ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    resources = _resource_module()
    started = time.perf_counter()
    before_times = os.times()
    child_cpu_before = before_times.children_user + before_times.children_system
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **({"creationflags": subprocess.CREATE_NO_WINDOW}
           if os.name == "nt" else {}))
    # Polling without draining blocks a debug-heavy child on its own stderr
    # once the pipe buffer fills, until the timeout.
    collected: dict[str, str] = {}
    readers = [threading.Thread(target=_drain_stream, args=(stream, name, collected),
                                daemon=True)
               for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for reader in readers:
        reader.start()

    def finish(join_timeout: float | None = None) -> tuple[str, str]:
        for reader in readers:
            reader.join(join_timeout)
        # Reap unconditionally: the child is already SIGKILLed on the timeout
        # path, and a zombie's CPU lands in a later run's children_* delta.
        proc.wait()
        return collected.get("stdout", ""), collected.get("stderr", "")

    accumulator = resources._TreeAccumulator(proc.pid, new_process=True)
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            proc.kill()
            stdout, stderr = finish(join_timeout=5.0)
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
        time.sleep(interval)
        accumulator.observe()
    accumulator.observe()
    stdout, stderr = finish()
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    measured = accumulator.metrics()
    after_times = os.times()
    child_cpu_after = after_times.children_user + after_times.children_system
    waited_cpu = max(0.0, child_cpu_after - child_cpu_before)
    sampled_cpu = measured.get("cpu_seconds")
    if sampled_cpu is not None:
        measured["cpu_seconds"] = max(float(sampled_cpu), waited_cpu)
    elif os.name != "nt" and math.isfinite(waited_cpu):
        measured["cpu_seconds"] = waited_cpu
    measured["wall_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result, measured


def _build_index(data: Path, env: dict[str, str], timeout: float) -> dict[str, object]:
    result, measured = _sampled_process(
        [sys.executable, str(Path(__file__).resolve()), "--_build-data", str(data)],
        cwd=ROOT, env=env, timeout=timeout, interval=0.05)
    if result.returncode != 0:
        tail = (result.stderr + result.stdout)[-1000:]
        raise RuntimeError(f"fixture index build exited {result.returncode}: {tail}")
    measured["db_bytes"] = (data / "corpus.db").stat().st_size
    return measured


def _json_payload(
        stdout: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    values: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("JSON output contained a non-object row")
            values.append(value)
    if not values or values[0].get("kind") != "agrep-meta":
        raise ValueError("JSON output lacked a leading metadata row")
    if any(value.get("kind") == "agrep-meta" for value in values[1:]):
        raise ValueError("JSON output contained multiple metadata rows")
    return values[1:], values[0]


def _json_rows(stdout: str) -> list[dict[str, object]]:
    return _json_payload(stdout)[0]


def _debug_summary(stderr: str) -> dict[str, object]:
    matches = list(_SEARCH_DONE.finditer(stderr))
    if len(matches) != 1:
        raise ValueError(f"expected one search-done marker, found {len(matches)}")
    match = matches[0]
    return {
        "total": int(match.group("total")),
        "chats": int(match.group("chats")),
        "engine": match.group("engine"),
    }


def _route_metrics(stderr: str) -> dict[str, object] | None:
    for lane, pattern in (("rows", _ROUTE_ROWS), ("short", _ROUTE_SHORT),
                          ("sessions", _ROUTE_SESSIONS)):
        match = pattern.search(stderr)
        if match is None:
            continue
        out: dict[str, object] = {
            "lane": lane,
            "examined": int(match.group("examined")),
            "observed": int(match.group("observed")),
            "totals_exact": match.group("exact") == "True",
        }
        if "scored" in match.groupdict():
            out["boundary_scored"] = int(match.group("scored"))
        boundary = list(_BOUNDARY_BATCH.finditer(stderr))
        if boundary:
            out["boundary_native_calls"] = len(boundary)
            out["boundary_native_items"] = sum(
                int(value.group("items")) for value in boundary)
            out["boundary_native_ms"] = round(sum(
                float(value.group("ms")) for value in boundary), 3)
        preflight = _DENSE_PREFLIGHT.search(stderr)
        if preflight is not None:
            out["preflight_examined"] = int(preflight.group("examined"))
            out["preflight_ms"] = float(preflight.group("ms"))
            out["work_examined"] = out["examined"] + out["preflight_examined"]
        return out
    return None


def _validate_json(result: subprocess.CompletedProcess[str], rows: int, *,
                   expected_total: int | None, expected_rows: int,
                   lane: str | None = None, special: bool = False) -> dict[str, object]:
    output, meta = _json_payload(result.stdout)
    summary = _debug_summary(result.stderr)
    if result.returncode != (0 if expected_rows else 1):
        raise ValueError(f"exit {result.returncode}, expected {0 if expected_rows else 1}")
    if len(output) != expected_rows:
        raise ValueError(f"rendered {len(output)} rows, expected {expected_rows}")
    if meta.get("error"):
        raise ValueError("search output carried an error metadata row")
    if not expected_rows and meta.get("hits") != []:
        raise ValueError("miss output lacked a successful metadata row")
    if expected_total is not None and summary["total"] != expected_total:
        raise ValueError(f"reported total {summary['total']}, expected {expected_total}")
    if expected_total is None and not expected_rows <= summary["total"] <= rows:
        raise ValueError(f"reported lower-bound total {summary['total']} is invalid")
    if lane == "all-terms" and any(row.get("matched") != lane for row in output):
        raise ValueError("all-terms query leaked a phrase-class row")
    if lane == "phrase" and any(row.get("matched") == "all-terms" for row in output):
        raise ValueError("phrase query ranked an all-terms row in its head")
    if special and any(not str(row.get("session", "")).startswith("scale-child-")
                       for row in output):
        raise ValueError("a source-early base row displaced a planted late winner")
    if special:
        expected = [f"scale-child-{index:03d}" for index in range(expected_rows)]
        actual = [row.get("session") for row in output]
        if actual != expected:
            raise ValueError(f"special-row tie order was {actual!r}, expected {expected!r}")
    return {**summary, "rendered_rows": len(output), "output": output}


def _validate_miss(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    del rows
    return _validate_json(result, 0, expected_total=0, expected_rows=0)


def _validate_selective(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(result, rows, expected_total=1, expected_rows=1)
    expected = f"scale-child-{SPECIAL_ROWS - 1:03d}"
    if checked["output"][0].get("session") != expected:
        raise ValueError("the source-last selective sentinel was not returned")
    return checked


def _validate_broad(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows), special=True)
    if not min(40, rows) <= checked["total"] <= rows:
        raise ValueError(f"broad lower-bound total {checked['total']} is invalid")
    families = {int(str(row["session"]).rsplit("-", 1)[1]) // 4
                for row in checked["output"]}
    if len(families) < min(10, SPECIAL_FAMILIES):
        raise ValueError(f"broad head collapsed to only {len(families)} planted families")
    checked["head_families"] = len(families)
    return checked


def _validate_short(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows), special=True)
    if checked["total"] < min(40, rows):
        raise ValueError("bounded short lane observed fewer hits than it rendered")
    marker = _SHORT_EXAMINED.search(result.stderr)
    base_rows = rows - SPECIAL_ROWS
    mid_old = min(max(6_000, rows // 50), max(1, base_rows - 2_048))
    expected_decoys = min(
        SHORT_DECOYS, max(0, base_rows - 128), base_rows - mid_old)
    if marker is None or int(marker.group("count")) < expected_decoys:
        raise ValueError("short lane did not cross the planted late-winner frontier")
    return checked


def _validate_short_interior(
        result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows))
    expected = [f"scale-base-{index:06d}" for index in range(min(40, rows))]
    actual = [row.get("session") for row in checked["output"]]
    if actual != expected:
        raise ValueError(f"interior short head was {actual!r}, expected {expected!r}")
    route = _route_metrics(result.stderr)
    if rows > 25_000 and (route is None or route["totals_exact"]):
        raise ValueError("large interior-only short route did not terminate early")
    return checked


def _validate_phrase(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    return _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows),
        lane="phrase", special=True)


def _validate_terms(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    return _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows),
        lane="all-terms")


def _validate_hybrid(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows))
    markers = [row.get("matched") for row in checked["output"]]
    expected = [None] * HYBRID_PHRASES + ["all-terms"] * (40 - HYBRID_PHRASES)
    if markers != expected:
        raise ValueError(f"hybrid lane order was {markers!r}, expected {expected!r}")
    expected_phrase = [f"scale-child-{index:03d}" for index in range(HYBRID_PHRASES)]
    if [row.get("session") for row in checked["output"][:HYBRID_PHRASES]] != expected_phrase:
        raise ValueError("hybrid phrase tier lost a planted thin phrase")
    route = _route_metrics(result.stderr)
    if route is None or route.get("preflight_examined") != rows:
        raise ValueError("hybrid row preflight work was not reported exactly")
    return checked


def _validate_hybrid_chats(
        result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(12, rows))
    markers = [row.get("matched") for row in checked["output"]]
    expected = [None] * HYBRID_PHRASES + ["all-terms"] * (12 - HYBRID_PHRASES)
    if markers != expected:
        raise ValueError(f"hybrid chat order was {markers!r}, expected {expected!r}")
    if len({row.get("session") for row in checked["output"]}) != 12:
        raise ValueError("hybrid chat lane returned duplicate sessions")
    expected_phrase = [f"scale-child-{index:03d}" for index in range(HYBRID_PHRASES)]
    if [row.get("session") for row in checked["output"][:HYBRID_PHRASES]] != expected_phrase:
        raise ValueError("hybrid chat phrase ordering changed")
    route = _route_metrics(result.stderr)
    if route is None or route.get("preflight_examined") != rows:
        raise ValueError("hybrid chat preflight work was not reported exactly")
    return checked


def _validate_terms_chats(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(12, rows),
        lane="all-terms")
    sessions = {row.get("session") for row in checked["output"]}
    if len(sessions) != min(12, rows):
        raise ValueError("session-head lane returned duplicate chats")
    return checked


def _validate_mid_old(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows))
    if any(not str(row.get("session", "")).startswith("scale-base-")
           for row in checked["output"]):
        raise ValueError("old-tail posting query returned a non-fixture row")
    return checked


def _validate_old_dense(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    checked = _validate_json(
        result, rows, expected_total=None, expected_rows=min(40, rows))
    if any(not str(row.get("session", "")).startswith("scale-base-")
           for row in checked["output"]):
        raise ValueError("old-dense query returned a non-fixture row")
    marker = ("dense recency walk" if (rows - SPECIAL_ROWS) // 3 > 100_000
              else "indexed posting sort")
    if marker not in result.stderr:
        raise ValueError(f"old-dense query missed expected {marker!r} route")
    route = _route_metrics(result.stderr)
    if rows > 300_000 and (route is None or route["totals_exact"]):
        raise ValueError("large old-dense route did not terminate early")
    return checked


def _validate_compact(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    if result.returncode != 0:
        raise ValueError(f"compact search exited {result.returncode}")
    summary = _debug_summary(result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not 4 <= len(lines) <= 16:
        raise ValueError(f"compact rendered {len(lines)} rows outside [4, 16]")
    rendered_bytes = len(result.stdout.encode("utf-8"))
    if rendered_bytes > 4096:
        raise ValueError(f"compact rendered {rendered_bytes} bytes, over its 4 KiB budget")
    if not len(lines) <= summary["total"] <= rows:
        raise ValueError(f"compact lower-bound total {summary['total']} is invalid")
    if not all(line.startswith("@scale-child-") for line in lines):
        raise ValueError("compact output displaced a source-late winner")
    ids = [int(line.split()[0].removeprefix("@scale-child-").split(":", 1)[0])
           for line in lines]
    families = len({index // 4 for index in ids})
    if families < min(10, len(ids)):
        raise ValueError(f"compact output collapsed to {families} planted root families")
    return {**summary, "rendered_rows": len(lines), "rendered_bytes": rendered_bytes}


def _validate_count(result: subprocess.CompletedProcess[str], rows: int) -> dict[str, object]:
    if result.returncode != 0:
        raise ValueError(f"count exited {result.returncode}")
    if result.stdout.strip() != str(rows):
        raise ValueError(f"count printed {result.stdout.strip()!r}, expected {rows}")
    summary = _debug_summary(result.stderr)
    if summary["total"] != rows:
        raise ValueError(f"count debug total {summary['total']}, expected {rows}")
    return {**summary, "rendered_rows": 1}


CASES = (
    Case("miss", MISS_QUERY, ("--json", "-n", "40"),
         ("engine: corpusdb (FTS index)", "search done:"), _validate_miss),
    Case("selective", SELECTIVE_QUERY, ("--json", "-n", "40"),
         ("engine: corpusdb (FTS index)", "boundary: Rust scored", "search done:"),
         _validate_selective),
    Case("broad_single", BROAD_QUERY, ("--json", "-n", "40"),
         ("bounded rows:", "boundary: Rust scored", "search done:"), _validate_broad),
    Case("short2", SHORT_QUERY, ("--json", "-n", "40"),
         ("bounded short rows:", "search done:"),
         _validate_short),
    Case("short_interior", SHORT_INTERIOR_QUERY, ("--json", "-n", "40"),
         ("bounded short rows:", "search done:"), _validate_short_interior),
    Case("broad_phrase", PHRASE_QUERY, ("--json", "-n", "40"),
         ("bounded rows:", "boundary: Rust scored", "search done:"), _validate_phrase),
    Case("hybrid_terms", HYBRID_QUERY, ("--json", "-n", "40"),
         ("dense phrase lane complete with 8 match(es)", "bounded rows:",
          "boundary: Rust scored", "search done:"), _validate_hybrid),
    Case("hybrid_chats", HYBRID_QUERY, ("-l", "--json", "-n", "12"),
         ("dense phrase lane complete with 8 match(es)", "bounded sessions:",
          "boundary: Rust scored", "search done:"),
         _validate_hybrid_chats),
    Case("all_terms", TERMS_QUERY, ("--json", "-n", "40"),
         ("bounded rows:", "boundary: Rust scored", "search done:"), _validate_terms),
    Case("terms_chats", TERMS_QUERY, ("-l", "--json", "-n", "12"),
         ("bounded sessions:", "boundary: Rust scored", "search done:"),
         _validate_terms_chats),
    Case("old_mid_density", MID_OLD_QUERY, ("--json", "-n", "40"),
         ("ordered candidates: indexed posting sort", "bounded rows:",
          "boundary: Rust scored", "search done:"), _validate_mid_old),
    Case("old_dense", OLD_DENSE_QUERY, ("--json", "-n", "40"),
         ("ordered candidates:", "bounded rows:",
          "boundary: Rust scored", "search done:"), _validate_old_dense),
    Case("compact", BROAD_QUERY, (),
         ("bounded rows:", "boundary: Rust scored", "search done:"),
         _validate_compact, profile="compact"),
    Case("count", BROAD_QUERY, ("-c",),
         ("count-only keyword:", "search done:"), _validate_count),
)


def _search_command(case: Case) -> list[str]:
    return [sys.executable, str(ROOT / "cli.py"), case.query, *case.args,
            "--color", "never"]


def _run_case(case: Case, rows: int, env: dict[str, str], *,
              runs: int, timeout: float) -> dict[str, object]:
    case_env = dict(env)
    if case.profile is not None:
        case_env["AGREP_PROFILE"] = case.profile
    else:
        case_env.pop("AGREP_PROFILE", None)
    case_env["AGREP_DEBUG"] = "1"
    cmd = _search_command(case)
    ingest_sig = Path(case_env["AGREP_DATA_DIR"]) / ".ingest.sig"

    def once() -> tuple[float, subprocess.CompletedProcess[str], dict[str, object]]:
        ingest_sig.touch()
        started = time.perf_counter()
        result = subprocess.run(
            cmd, cwd=ROOT, env=case_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        wall = (time.perf_counter() - started) * 1000
        if "ensure_index(auto=True)" not in result.stderr:
            raise ValueError(f"{case.name}: normal freshness check was absent")
        for marker in case.required_markers:
            if marker not in result.stderr:
                raise ValueError(f"{case.name}: required route marker {marker!r} was absent")
        proof = case.validate(result, rows)
        route = _route_metrics(result.stderr)
        if any("bounded" in marker for marker in case.required_markers) and route is None:
            raise ValueError(f"{case.name}: bounded route metrics were absent")
        if case.name in EARLY_STOP_CASES and route and route["totals_exact"]:
            raise ValueError(f"{case.name}: bounded lane unexpectedly exhausted its corpus")
        if route is not None:
            proof["route"] = route
        return wall, result, proof

    first_ms, first, proof = once()
    samples: list[float] = []
    stdout_bytes: list[int] = []
    for _ in range(runs):
        wall, result, current = once()
        samples.append(wall)
        stdout_bytes.append(len(result.stdout.encode("utf-8")))
        proof = current
    ingest_sig.touch()
    sampled, resources = _sampled_process(
        cmd, cwd=ROOT, env=case_env, timeout=timeout, interval=0.005)
    for marker in case.required_markers:
        if marker not in sampled.stderr:
            raise ValueError(f"{case.name}: sampled run missed route marker {marker!r}")
    case.validate(sampled, rows)
    p95 = (statistics.quantiles(samples, n=100, method="inclusive")[94]
           if len(samples) >= 20 else None)
    return {
        "query": case.query,
        "args": list(case.args),
        "profile": case.profile or "classic",
        "process_first_ms": round(first_ms, 3),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": None if p95 is None else round(p95, 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
        "stdout_bytes": int(statistics.median(stdout_bytes)),
        "returncode": first.returncode,
        "required_markers": list(case.required_markers),
        "proof": {key: value for key, value in proof.items() if key != "output"},
        "peak_rss_mib": resources.get("rss_mib"),
        "cpu_seconds_sampled": resources.get("cpu_seconds"),
        "sampled_processes": resources.get("processes"),
    }


def _database_proof(data: Path, rows: int) -> dict[str, object]:
    started = time.perf_counter()
    db = sqlite3.connect(data / "corpus.db")
    try:
        counts = {
            "rows": int(db.execute("SELECT count(*) FROM msgs").fetchone()[0]),
            "broad_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?", ('\"the\"',)
            ).fetchone()[0]),
            "short_candidates": int(db.execute(
                "SELECT count(*) FROM msgs WHERE text LIKE '%hi%'"
            ).fetchone()[0]),
            "short_interior_candidates": int(db.execute(
                "SELECT count(*) FROM msgs WHERE text LIKE '%zz%'"
            ).fetchone()[0]),
            "phrase_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                ('\"race\" AND \"condition\"',)).fetchone()[0]),
            "terms_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                ('\"alpha\" AND \"omega\"',)).fetchone()[0]),
            "hybrid_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                ('\"the\" AND \"omega\"',)).fetchone()[0]),
            "mid_old_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                (f'\"{MID_OLD_QUERY}\"',)).fetchone()[0]),
            "old_dense_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                (f'\"{OLD_DENSE_QUERY}\"',)).fetchone()[0]),
            "missing_content_digests": int(db.execute(
                "SELECT count(*) FROM msgs WHERE content_digest IS NULL",
            ).fetchone()[0]),
            "selective_candidates": int(db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                ('\"neon\" AND \"glacier\" AND \"sentinel\"',)).fetchone()[0]),
        }
    finally:
        db.close()
    expected = {
        "rows": rows,
        "broad_candidates": rows,
        "short_candidates": rows,
        "short_interior_candidates": rows - SPECIAL_ROWS,
        "phrase_candidates": rows,
        "terms_candidates": rows,
        "hybrid_candidates": rows,
        "mid_old_candidates": min(max(6_000, rows // 50),
                                  max(1, rows - SPECIAL_ROWS - 2_048)),
        "old_dense_candidates": max(1, (rows - SPECIAL_ROWS) // 3),
        "missing_content_digests": 0,
        "selective_candidates": 1,
    }
    if counts != expected:
        raise ValueError(f"fixture cardinality mismatch: {counts} != {expected}")
    return {**counts, "wall_ms": round((time.perf_counter() - started) * 1000, 3)}


def _projected_bytes(rows: int) -> int:
    return 768 * MIB + rows * 2048


def _space_guard(root: Path, rows: int, force: bool) -> dict[str, int]:
    disk = shutil.disk_usage(root)
    projected = _projected_bytes(rows)
    reserve = 2 * GIB
    if not force and disk.free < projected + reserve:
        raise RuntimeError(
            f"refusing {rows:,} rows: {disk.free / GIB:.2f} GiB free, "
            f"projected {projected / GIB:.2f} GiB plus {reserve / GIB:.0f} GiB reserve; "
            "free space, choose fewer rows, or pass --force-low-space")
    return {"free_before": disk.free, "projected": projected, "reserve": reserve}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _physical_memory_bytes() -> int | None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
        try:
            if result.returncode == 0:
                return int(result.stdout.strip())
        except ValueError:
            pass
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        match = re.search(r"^\s*Memory:\s*([\d.]+)\s*([GMT]B)$",
                          result.stdout, re.MULTILINE)
        scales = {"MB": MIB, "GB": GIB, "TB": 1024 * GIB}
        return (int(float(match.group(1)) * scales[match.group(2)])
                if match is not None else None)
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                        ("page_total", ctypes.c_ulonglong),
                        ("page_available", ctypes.c_ulonglong),
                        ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_available", ctypes.c_ulonglong),
                        ("extended_available", ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return (int(status.total) if ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)) else None)
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def _provenance() -> dict[str, object]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or platform.machine()
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            cpu = result.stdout.strip()
        else:
            result = subprocess.run(
                ["system_profiler", "SPHardwareDataType"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30)
            match = re.search(r"^\s*Chip:\s*(.+)$", result.stdout, re.MULTILINE)
            if match:
                cpu = match.group(1).strip()
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "physical_memory_bytes": _physical_memory_bytes(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "rust_binary": str(RUST_BIN),
        "rust_binary_sha256": _sha256(RUST_BIN),
        "source_sha256": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in (PY / "search.py", PY / "corpusdb.py", Path(__file__).resolve())
        },
    }


def _newer_rust_sources(binary: Path = RUST_BIN) -> list[Path]:
    try:
        binary_mtime = binary.stat().st_mtime_ns
    except OSError:
        return []
    sources = [ROOT / "Cargo.toml", ROOT / "Cargo.lock", ROOT / ".cargo" / "config.toml"]
    sources.extend(ROOT.glob("crates/*/Cargo.toml"))
    sources.extend((ROOT / "crates").rglob("*.rs"))
    return sorted(path for path in sources
                  if path.is_file() and path.stat().st_mtime_ns > binary_mtime)


def _campaign(rows: int, args: argparse.Namespace) -> dict[str, object]:
    temp_root = Path(args.temp_root).expanduser().resolve() if args.temp_root else None
    guard_root = temp_root or Path(tempfile.gettempdir())
    guard_root.mkdir(parents=True, exist_ok=True)
    space = _space_guard(guard_root, rows, args.force_low_space)
    with tempfile.TemporaryDirectory(prefix=f"agrep-scale-{rows}-", dir=temp_root) as raw:
        root = Path(raw)
        if os.name != "nt":
            os.chmod(root, 0o700)
        data, home = root / "data", root / "home"
        data.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        _write_live_store(home)
        env = _private_env(home, data)
        source = _write_fixture(data, rows)
        build = _build_index(data, env, args.build_timeout)
        workloads: dict[str, object] = {}
        for case in CASES:
            _progress(args, f"{rows:,} rows: {case.name}")
            workloads[case.name] = _run_case(
                case, rows, env, runs=args.runs, timeout=args.query_timeout)
        proof = _database_proof(data, rows)
        actual_bytes = int(source["source_bytes"]) + int(build["db_bytes"])
        return {
            "rows": rows,
            "fixture": source,
            "index_build": build,
            "cardinality_proof": proof,
            "storage": {
                "source_bytes": source["source_bytes"],
                "db_bytes": build["db_bytes"],
                "total_bytes": actual_bytes,
                "bytes_per_row": round(actual_bytes / rows, 3),
                "free_before_bytes": space["free_before"],
                "projected_bytes": space["projected"],
                "reserve_bytes": space["reserve"],
            },
            "workloads": workloads,
        }


def _progress(args: argparse.Namespace, message: str) -> None:
    print(message, file=sys.stderr if args.json else sys.stdout, flush=True)


def _fmt_ms(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.1f}"


def _fmt_mib(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.1f}"


def _print_campaign(report: dict[str, object]) -> None:
    rows = int(report["rows"])
    source = report["fixture"]
    build = report["index_build"]
    storage = report["storage"]
    print(f"\n{rows:,} rows")
    print(f"  source: {storage['source_bytes'] / MIB:.1f} MiB in {source['wall_ms']:.1f} ms")
    print(f"  corpus.db: {storage['db_bytes'] / MIB:.1f} MiB; "
          f"derived-index build {build['wall_ms']:.1f} ms wall / "
          f"{_fmt_ms(None if build.get('cpu_seconds') is None else build['cpu_seconds'] * 1000)} ms CPU; "
          f"peak RSS {_fmt_mib(build.get('rss_mib'))} MiB")
    print(f"  footprint: {storage['bytes_per_row']:.1f} bytes/row; "
          f"cardinality proof {report['cardinality_proof']['wall_ms']:.1f} ms")
    print("  workload      proc-1st   median      p95      max  RSS MiB   CPU ms   exam native  route")
    for name, result in report["workloads"].items():
        route = result["required_markers"][0]
        cpu_ms = (None if result.get("cpu_seconds_sampled") is None
                  else float(result["cpu_seconds_sampled"]) * 1000)
        route_proof = result["proof"].get("route", {})
        examined = route_proof.get("work_examined", route_proof.get("examined", "-"))
        native = (f"{route_proof['boundary_native_ms']:.0f}/"
                  f"{route_proof['boundary_native_calls']}"
                  if route_proof.get("boundary_native_calls") else "-")
        print(f"  {name:<15} {_fmt_ms(result['process_first_ms']):>7} "
              f"{_fmt_ms(result['median_ms']):>8} {_fmt_ms(result['p95_ms']):>8} "
              f"{_fmt_ms(result['max_ms']):>8} {_fmt_mib(result['peak_rss_mib']):>8} "
              f"{_fmt_ms(cpu_ms):>8} {str(examined):>6} {native:>6}  {route}")


def _rows_arg(value: str) -> int:
    try:
        rows = int(value.replace("_", "").replace(",", ""))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("rows must be an integer") from exc
    if rows < 10_000:
        raise argparse.ArgumentTypeError("rows must be at least 10000")
    return rows


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", nargs="+", type=_rows_arg,
                    help="row counts, run sequentially (default: 1000000 2000000)")
    ap.add_argument("--runs", type=int, default=5,
                    help="completion samples per workload; p95 needs at least 20 (default: 5)")
    ap.add_argument("--quick", action="store_true",
                    help="use 10000 and 25000 rows unless --rows is explicit")
    ap.add_argument("--json", action="store_true", help="emit one machine-readable report")
    ap.add_argument("--check", action="store_true",
                    help="enforce the complete 1M/2M latency and footprint budgets")
    ap.add_argument("--budget-profile", choices=tuple(SCALE_BUDGET_PROFILES),
                    default="target", help="committed budget set (default: target)")
    ap.add_argument("--temp-root", help="fixture parent (default: platform temp dir)")
    ap.add_argument("--force-low-space", action="store_true",
                    help="override the projected-space guard")
    ap.add_argument("--build-timeout", type=float, default=3600.0)
    ap.add_argument("--query-timeout", type=float, default=900.0)
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    if not math.isfinite(args.build_timeout) or args.build_timeout <= 0:
        ap.error("--build-timeout must be positive and finite")
    if not math.isfinite(args.query_timeout) or args.query_timeout <= 0:
        ap.error("--query-timeout must be positive and finite")
    selected = args.rows or (QUICK_ROWS if args.quick else DEFAULT_ROWS)
    args.rows = list(dict.fromkeys(selected))
    if args.check and args.runs < 3:
        ap.error("--check requires at least 3 completion samples per workload")
    if args.check and set(args.rows) != set(DEFAULT_ROWS):
        ap.error("--check requires the complete 1M and 2M campaign")
    if not args.check and args.budget_profile != "target":
        ap.error("--budget-profile requires --check")
    return args


def _budget_failures(reports: list[dict[str, object]],
                     profile: str = "target") -> list[str]:
    latency_budgets, campaign_budgets = SCALE_BUDGET_PROFILES[profile]
    failures = []
    for report in reports:
        rows = int(report["rows"])
        budgets = latency_budgets.get(rows)
        campaign_budget = campaign_budgets.get(rows)
        if budgets is None or campaign_budget is None:
            failures.append(f"no committed scale budgets for {rows:,} rows")
            continue
        workloads = report["workloads"]
        for name, budget in budgets.items():
            if name not in workloads:
                failures.append(f"{rows:,} {name}: workload result missing")
                continue
            measured = float(workloads[name]["median_ms"])
            if not math.isfinite(measured):
                failures.append(f"{rows:,} {name}: median was not finite")
            elif measured > float(budget):
                failures.append(
                    f"{rows:,} {name}: {measured:.1f}ms > {float(budget):.1f}ms")
            rss = workloads[name].get("peak_rss_mib")
            cpu = workloads[name].get("cpu_seconds_sampled")
            if rss is None:
                failures.append(f"{rows:,} {name}: peak RSS was not measured")
            elif not math.isfinite(float(rss)):
                failures.append(f"{rows:,} {name}: peak RSS was not finite")
            elif float(rss) > campaign_budget["query_rss_mib"]:
                failures.append(
                    f"{rows:,} {name} RSS: {float(rss):.1f}MiB > "
                    f"{campaign_budget['query_rss_mib']:.1f}MiB")
            if cpu is None:
                failures.append(f"{rows:,} {name}: CPU time was not measured")
            elif not math.isfinite(float(cpu)):
                failures.append(f"{rows:,} {name}: CPU time was not finite")
            elif float(cpu) * 1_000 > float(budget) * 2:
                failures.append(
                    f"{rows:,} {name} CPU: {float(cpu) * 1_000:.1f}ms > "
                    f"{float(budget) * 2:.1f}ms")
        build = report["index_build"]
        storage = report["storage"]
        campaign_values = {
            "index_build_ms": float(build["wall_ms"]),
            "index_build_rss_mib": build.get("rss_mib"),
            "db_mib": float(storage["db_bytes"]) / MIB,
            "bytes_per_row": float(storage["bytes_per_row"]),
        }
        for name, budget in campaign_budget.items():
            if name == "query_rss_mib":
                continue
            measured = campaign_values[name]
            if measured is None:
                failures.append(f"{rows:,} {name}: value was not measured")
            elif not math.isfinite(float(measured)):
                failures.append(f"{rows:,} {name}: value was not finite")
            elif float(measured) > float(budget):
                failures.append(
                    f"{rows:,} {name}: {float(measured):.1f} > {float(budget):.1f}")
    return failures


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("scale benchmark requires Python 3.10 or newer", file=sys.stderr)
        return 2
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--_build-data":
        return _build_database(Path(argv[1]))
    args = _parse_args(argv)
    if not RUST_BIN.is_file():
        print(f"scale benchmark needs the current release binary at {RUST_BIN}; "
              "run cargo build --release", file=sys.stderr)
        return 2
    if args.check:
        newer_sources = _newer_rust_sources()
        if newer_sources:
            sample = ", ".join(str(path.relative_to(ROOT)) for path in newer_sources[:3])
            suffix = " ..." if len(newer_sources) > 3 else ""
            print(f"release binary predates Rust sources ({sample}{suffix}); "
                  "run cargo build --release", file=sys.stderr)
            return 2
    provenance = _provenance()
    if not args.json:
        memory = provenance.get("physical_memory_bytes")
        ram = "unknown RAM" if memory is None else f"{memory / GIB:.1f} GiB RAM"
        print(f"machine: {provenance['cpu']} · {provenance['machine']} · "
              f"{ram} · Python {provenance['python']} · SQLite {provenance['sqlite']}")
        print(f"binary: {provenance['rust_binary']} "
              f"sha256={str(provenance['rust_binary_sha256'])[:16]}…")
        print("timing: checkout CLI process start through exit; process-cold, OS-cache-warm")
        print("freshness: synthetic signature fast path; no real-store discovery/reconcile")
        print("resources: one 5ms-sampled process-tree run; RSS is transient/tree-summed")
        if args.check:
            print(f"budgets: {args.budget_profile} latency, CPU, RSS, build, and storage")
    reports: list[dict[str, object]] = []
    try:
        for row_count in args.rows:
            _progress(args, f"building disposable {row_count:,}-row corpus")
            report = _campaign(row_count, args)
            reports.append(report)
            if not args.json:
                _print_campaign(report)
    except (OSError, RuntimeError, ValueError, sqlite3.Error,
            subprocess.SubprocessError) as exc:
        print(f"scale benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    output = {
        "schema": 3,
        "provenance": provenance,
        "runs": args.runs,
        "sequential_cleanup": True,
        "timing_scope": "checkout CLI process start through exit; process-cold, OS-cache-warm",
        "freshness_scope": "synthetic signature fast path; no real-store discovery/reconcile",
        "index_scope": "derived corpusdb build from materialized messages.jsonl",
        "rss_scope": "sampled process-tree resident working set; shared pages may overlap",
        "semantic": "not measured",
        "campaigns": reports,
    }
    if args.check:
        failures = _budget_failures(reports, args.budget_profile)
        output["budget_profile"] = args.budget_profile
        output["budget_failures"] = failures
        if failures:
            for failure in failures:
                print(f"scale budget failed: {failure}", file=sys.stderr)
            if args.json:
                print(json.dumps(output, separators=(",", ":"), sort_keys=True))
            return 1
    if args.json:
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
