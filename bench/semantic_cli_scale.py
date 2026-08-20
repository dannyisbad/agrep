#!/usr/bin/env python3
"""Full-process semantic and hybrid scale gate.

The publisher-backed segmented fixture exercises ``cli.py`` plus the resident
worker exactly as an installed command does. Vector, grouping, refs, FTS, and
cold generation validation cardinalities are real.

    python bench/semantic_cli_scale.py --quick --runs 1
    python bench/semantic_cli_scale.py --check --runs 3
    python bench/semantic_cli_scale.py --json
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
RUST_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
DIM = 384
QUERY = "recover parser retry incident"
SMALL_ROWS = 1_000_000
DESIGN_ROWS = 2_000_000
PROJECTION_ROWS = 10_000_000
MATERIALIZED_ROWS = 640
SEMANTIC_ROWS = range(0, 512)
LEXICAL_ROWS = range(512, 576)
DEFAULT_ROWS = (SMALL_ROWS, DESIGN_ROWS)
QUICK_ROWS = (10_000, 50_000)
MIB = 1024 * 1024
GIB = 1024 * MIB
TARGET_BUDGETS_MS = {
    "warm_semantic_2m_ms": 150.0,
    "warm_hybrid_2m_ms": 250.0,
    "cold_semantic_2m_ms": 700.0,
    "projected_warm_semantic_10m_ms": 300.0,
}
PORTABLE_MULTIPLIER = 3.0
_TIMING_LINE = re.compile(r"semantic timing (?P<payload>\{.*\})$")
_SEARCH_DONE = re.compile(
    r"search done: (?P<total>\d+) hit\(s\) in (?P<chats>\d+) chat\(s\) "
    r"via (?P<engine>[^;]+);")


def _runtime_error(
        argv: list[str], version_info: tuple[int, ...] | None = None) -> str | None:
    version = tuple(sys.version_info if version_info is None else version_info)
    if version >= (3, 10):
        return None
    project_python = ROOT / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(project_python), str(Path(__file__).resolve()), *argv]
    rerun = (subprocess.list2cmdline(command) if os.name == "nt"
             else shlex.join(command))
    actual = ".".join(str(part) for part in version[:3])
    return (f"bench/semantic_cli_scale.py requires Python 3.10+; running {actual}.\n"
            f"rerun with the project runtime: {rerun}")


class _MacRusageV2(ctypes.Structure):
    _fields_ = (
        ("uuid", ctypes.c_ubyte * 16), ("user_ns", ctypes.c_uint64),
        ("system_ns", ctypes.c_uint64), ("pkg_idle", ctypes.c_uint64),
        ("interrupt", ctypes.c_uint64), ("pageins", ctypes.c_uint64),
        ("wired", ctypes.c_uint64), ("resident", ctypes.c_uint64),
        ("footprint", ctypes.c_uint64), ("start_abs", ctypes.c_uint64),
        ("exit_abs", ctypes.c_uint64), ("child_user", ctypes.c_uint64),
        ("child_system", ctypes.c_uint64),
        ("child_pkg_idle", ctypes.c_uint64),
        ("child_interrupt", ctypes.c_uint64),
        ("child_pageins", ctypes.c_uint64),
        ("child_elapsed", ctypes.c_uint64),
        ("disk_read", ctypes.c_uint64), ("disk_write", ctypes.c_uint64),
    )


class _MacTimebase(ctypes.Structure):
    _fields_ = (("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32))


_MAC_TIMEBASE: float | None = None


def _mac_ticks_ms(value: int) -> float:
    global _MAC_TIMEBASE
    if _MAC_TIMEBASE is None:
        info = _MacTimebase()
        if ctypes.CDLL(None).mach_timebase_info(ctypes.byref(info)) != 0:
            raise OSError("mach_timebase_info failed")
        _MAC_TIMEBASE = float(info.numer) / float(info.denom) / 1_000_000.0
    return float(value) * _MAC_TIMEBASE


def _process_usage(pid: int) -> dict[str, float] | None:
    if sys.platform == "darwin":
        try:
            fn = ctypes.CDLL(
                "/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int
            info = _MacRusageV2()
            if fn(pid, 2, ctypes.byref(info)) != 0:
                return None
            return {
                "user_ms": _mac_ticks_ms(info.user_ns),
                "system_ms": _mac_ticks_ms(info.system_ns),
                "rss_mib": info.resident / MIB,
                "private_mib": info.footprint / MIB,
                "disk_read_mib": info.disk_read / MIB,
                "disk_write_mib": info.disk_write / MIB,
            }
        except (AttributeError, OSError):
            return None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(
                encoding="ascii").split()
            status = {}
            for line in Path(f"/proc/{pid}/status").read_text(
                    encoding="ascii").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            io = {}
            for line in Path(f"/proc/{pid}/io").read_text(
                    encoding="ascii").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    io[key] = int(value.strip())
            ticks = float(os.sysconf("SC_CLK_TCK"))
            return {
                "user_ms": float(fields[13]) * 1000.0 / ticks,
                "system_ms": float(fields[14]) * 1000.0 / ticks,
                "rss_mib": int(status["VmRSS"].split()[0]) / 1024.0,
                "private_mib": int(
                    status.get("RssAnon", "0 kB").split()[0]) / 1024.0,
                "disk_read_mib": io.get("read_bytes", 0) / MIB,
                "disk_write_mib": io.get("write_bytes", 0) / MIB,
            }
        except (KeyError, OSError, ValueError):
            return None
    return None


def _child_pids(pid: int) -> list[int]:
    if os.name == "nt":
        return []
    try:
        result = subprocess.run(
            ["ps", "-axo", "ppid=,pid="], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3, check=True)
        pairs = [line.split() for line in result.stdout.splitlines()]
        return [int(child) for parent, child in pairs if int(parent) == pid]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _process_group_usage(pid: int) -> dict[str, float] | None:
    records = [value for child in [pid, *_child_pids(pid)]
               if (value := _process_usage(child)) is not None]
    if not records:
        return None
    return {key: sum(record[key] for record in records)
            for key in records[0]}


def _usage_delta(before: dict | None, after: dict | None) -> dict | None:
    if before is None or after is None:
        return None
    return {
        "cpu_ms": max(0.0, after["user_ms"] - before["user_ms"])
        + max(0.0, after["system_ms"] - before["system_ms"]),
        "disk_read_mib": max(
            0.0, after["disk_read_mib"] - before["disk_read_mib"]),
        "disk_write_mib": max(
            0.0, after["disk_write_mib"] - before["disk_write_mib"]),
        "rss_mib": after["rss_mib"],
        "private_mib": after["private_mib"],
    }


def _tree_sizes(path: Path) -> dict[str, int]:
    logical = allocated = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        stat = item.stat()
        logical += stat.st_size
        allocated += int(getattr(stat, "st_blocks", 0)) * 512 or stat.st_size
    return {"logical_bytes": logical, "allocated_bytes": allocated}


def _mid(ordinal: int) -> str:
    return f"scale:sem-{ordinal // 4:07d}:{ordinal % 4}"


def _session(ordinal: int) -> str:
    return f"sem-{ordinal // 4:07d}"


def _text(ordinal: int) -> str:
    if ordinal in SEMANTIC_ROWS:
        return f"meaning-only planted winner {ordinal}"
    if ordinal in LEXICAL_ROWS:
        return f"{QUERY} lexical winner {ordinal}"
    return "semantic scale filler"


def _private_env(home: Path, data: Path, model_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
        "XDG_CONFIG_HOME", "AGREP_PROFILE", "AGREP_RS_BIN", "CODEX_THREAD_ID",
        "CODEX_SANDBOX", "CLAUDECODE",
        "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE", "GEMINI_CLI",
        "CLINE_ACTIVE", "CURSOR_AGENT", "AGREP_NO_DAEMON",
        "AGREP_DATA_READONLY",
        "AGREP_NO_SEM_WORKER", "AGREP_SEMANTIC_Q8_SHADOW",
    ):
        env.pop(name, None)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "AGREP_HOME": str(home),
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_MODEL_DIR": str(model_root),
        "AGREP_RS_BIN": str(RUST_BIN),
        "AGREP_SEM_IDLE_S": "600",
        "AGREP_SEM_TIMING": "1",
        "AGREP_DEBUG": "1",
        "PYTHONHASHSEED": "0",
    })
    return env


def _write_sources(data: Path, common_api) -> dict[int, tuple[int, int, str, dict]]:
    now = int(time.time() * 1000)
    locators = {}
    sessions = []
    families = []
    with (data / "messages.jsonl").open(
            "wb", buffering=MIB) as stream:
        for ordinal in range(MATERIALIZED_ROWS):
            row = {
                "id": _mid(ordinal), "session": _session(ordinal),
                "agent": "codex", "project": "/semantic-scale",
                "model": "scale-fixture", "model_source": "explicit",
                "turn": ordinal % 4, "ts": now - ordinal,
                "who": "user", "text": _text(ordinal),
            }
            raw = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            offset = stream.tell()
            stream.write(raw)
            text_hash = hashlib.blake2b(
                row["text"].encode(), digest_size=8).hexdigest()
            locators[ordinal] = (offset, len(raw), text_hash, row)
            if ordinal % 4 == 0:
                session = _session(ordinal)
                sessions.append({
                    "session": session, "agent": "codex",
                    "project": "/semantic-scale",
                    "first_ts": now - ordinal - 3,
                    "last_ts": now - ordinal, "n": 4, "parent": "",
                    "first_text": row["text"],
                })
                families.append((session, ""))
    (data / "replies.jsonl").write_text("", encoding="utf-8")
    (data / "sessions.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n"
                for row in sessions),
        encoding="utf-8",
    )
    signature = "semantic-cli-scale-v1"
    (data / common_api.SESSION_FAMILY_META_FILE).write_text(
        json.dumps({
            "version": common_api.SESSION_FAMILY_INDEX_VERSION,
            "algorithm": common_api.SESSION_FAMILY_DIGEST_ALGORITHM,
            "ingest_signature": signature, "count": len(families),
            "digest": common_api.session_family_digest(families),
        }, separators=(",", ":")),
        encoding="utf-8",
    )
    (data / "boundary_stats.json").write_text(
        json.dumps({
            "schema": 2, "generation": signature, "tokens": {},
        }, separators=(",", ":")),
        encoding="utf-8",
    )
    (data / ".boundary_stats.bin").write_bytes(b"semantic-scale-v1\n")
    (data / "event_stats.json").write_text("{}\n", encoding="utf-8")
    (data / "settings.json").write_text(
        '{"tools":"off"}\n', encoding="utf-8")
    (data / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
    return locators


def _write_derived_proof(data: Path, signature: str, corpusdb_api) -> None:
    rows = []
    for name in corpusdb_api._DERIVED_PROOF_NAMES:
        path = data / name
        identity = corpusdb_api._proof_file_identity(path)
        if corpusdb_api._PLATFORM_NAME == "posix":
            token = {"Metadata": corpusdb_api._unix_change_token(identity[2])}
        elif corpusdb_api._PLATFORM_NAME == "nt":
            token = {"ContentSha256": list(
                corpusdb_api._content_sha256(path, identity))}
        else:
            token = {"Metadata": 0}
        rows.append({
            "name": name, "len": identity[0], "modified_ns": identity[1],
            "change_token": token,
            "edge_hash": corpusdb_api._edge_hash(path, identity[0]),
        })
    (data / ".derived_generation.json").write_text(
        json.dumps({
            "version": corpusdb_api._DERIVED_PROOF_VERSION,
            "signature": signature, "files": rows,
        }, separators=(",", ":")),
        encoding="utf-8",
    )


def _write_embedding_bundle(data: Path, rows: int, query: object) -> dict:
    import common
    import embedder
    import semantic

    ids_path = data / "embeddings.ids"
    hashes_path = data / "embeddings.hashes"
    decoy_hash = hashlib.blake2b(
        _text(MATERIALIZED_ROWS).encode(), digest_size=8).hexdigest()
    special_hashes = {
        ordinal: hashlib.blake2b(
            _text(ordinal).encode(), digest_size=8).hexdigest()
        for ordinal in range(MATERIALIZED_ROWS)
    }
    with (ids_path.open(
            "w", encoding="utf-8", newline="\n", buffering=MIB) as id_stream,
          hashes_path.open(
              "w", encoding="ascii", newline="\n", buffering=MIB) as hash_stream):
        for ordinal in range(rows):
            mid = _mid(ordinal)
            id_stream.write(mid + "\n")
            hash_stream.write(special_hashes.get(ordinal, decoy_hash) + "\n")

    matrix_path = data / "embeddings.f32"
    with matrix_path.open("wb") as stream:
        stream.truncate(rows * DIM * 4)
    vector = query.astype("<f4", copy=False).tobytes(order="C")
    with matrix_path.open("r+b", buffering=0) as stream:
        for ordinal in SEMANTIC_ROWS:
            stream.seek(ordinal * DIM * 4)
            stream.write(vector)
    generation = common.write_embedding_commit(
        data / "embeddings.meta", DIM, embedder.PROFILE_STRING,
        matrix_path, ids_path, rows, hashes_path=hashes_path)
    semantic.write_generation_marker(
        common.transcript_generation(), indexed_rows=rows, total_rows=rows)
    return {
        "generation": generation, "f32": matrix_path,
        "ids": ids_path, "hashes": hashes_path,
    }


def _write_segment_metadata(data: Path, rows: int) -> tuple[Path, Path]:
    path = data / "segment.refs.sqlite"
    groups_path = data / "segment.groups"
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE refs(
            local_ord INTEGER PRIMARY KEY, row_ref INTEGER NOT NULL,
            mid TEXT NOT NULL, text_hash TEXT NOT NULL, agent TEXT NOT NULL,
            project TEXT NOT NULL, session TEXT NOT NULL, ts INTEGER NOT NULL,
            turn INTEGER NOT NULL, who TEXT NOT NULL, model TEXT,
            family_id INTEGER NOT NULL, metadata_hash TEXT NOT NULL,
            family_label TEXT, model_source TEXT NOT NULL);
    """)
    now = int(time.time() * 1000)
    decoy_hash = hashlib.blake2b(
        _text(MATERIALIZED_ROWS).encode(), digest_size=8).hexdigest()
    special_hashes = {
        ordinal: hashlib.blake2b(
            _text(ordinal).encode(), digest_size=8).hexdigest()
        for ordinal in range(MATERIALIZED_ROWS)
    }
    insert = "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    batch = []
    with groups_path.open("w", encoding="ascii", newline="\n", buffering=MIB) as groups:
        for ordinal in range(rows):
            session = _session(ordinal)
            family_id = ordinal // 4 + 1
            groups.write(f"{family_id}\n")
            batch.append((
                ordinal, ordinal, _mid(ordinal),
                special_hashes.get(ordinal, decoy_hash), "codex",
                "/semantic-scale", session, now - ordinal, ordinal % 4,
                "user", "scale-fixture", family_id, "0" * 32,
                f"f:{session}", "explicit",
            ))
            if len(batch) == 8192:
                db.executemany(insert, batch)
                batch.clear()
    if batch:
        db.executemany(insert, batch)
    db.execute("CREATE UNIQUE INDEX refs_row_ref ON refs(row_ref)")
    db.execute("CREATE UNIQUE INDEX refs_mid ON refs(mid)")
    db.execute("CREATE INDEX refs_session ON refs(session,turn)")
    db.commit()
    db.close()
    return path, groups_path


def _write_corpus_db(data: Path, rows: int, build_id: str) -> Path:
    import corpusdb

    family_stamp = corpusdb.common.session_family_source_stamp(data)
    if (family_stamp is None
            or family_stamp == corpusdb.common.SESSION_FAMILY_MISSING_STAMP):
        raise RuntimeError("semantic fixture family publication is incomplete")
    path = data / "corpus.db"
    db = sqlite3.connect(path)
    db.executescript(
        "PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF;" + corpusdb._SCHEMA_SQL)
    insert = (
        "INSERT INTO msgs(session,turn,ts,agent,project,concept,model,"
        "model_source,who,text) VALUES(?,?,?,?,?,?,?,?,?,?)")
    now = int(time.time() * 1000)
    batch = []
    for ordinal in range(rows):
        text = (_text(ordinal) if ordinal < MATERIALIZED_ROWS
                else "semantic scale filler")
        batch.append((
            _session(ordinal), ordinal % 4, now - ordinal, "codex",
            "/semantic-scale", "", "scale-fixture", "explicit", "user", text))
        if len(batch) == 8192:
            db.executemany(insert, batch)
            batch.clear()
    if batch:
        db.executemany(insert, batch)
    db.executemany(
        "INSERT INTO session_family(session,root,side) VALUES(?,?,?)",
        ((_session(ordinal), _session(ordinal), 0)
         for ordinal in range(0, rows, 4)),
    )
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid,text) SELECT id,text FROM msgs")
    db.executescript(corpusdb._TRIGGERS_SQL)
    db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", (
        ("stamp", corpusdb._stamp()), ("schema", corpusdb._SCHEMA),
        ("fts_triggers", corpusdb._TRIGGER_SCHEMA), ("build_id", build_id),
        ("family_stamp", family_stamp)))
    db.commit()
    db.close()
    return path


def _build_fixture(data: Path, rows: int) -> int:
    sys.path.insert(0, str(PY))
    import common
    import corpusdb
    import embedder
    import embedding_segments
    import indexd_runtime
    import semantic_q8

    started = time.perf_counter()
    data.mkdir(parents=True, exist_ok=True)
    build_id = indexd_runtime.derived_writer_build_id(
        RUST_BIN, require_binary=True)
    (data / ".derived-owner.json").write_text(
        json.dumps({"version": 1, "build_id": build_id}, separators=(",", ":"))
        + "\n", encoding="utf-8")
    _write_sources(data, common)
    query_model = embedder.Embedder(download=False)
    query = query_model.embed_query(QUERY)
    del query_model
    bundle = _write_embedding_bundle(data, rows, query)
    refs, groups = _write_segment_metadata(data, rows)
    corpus = _write_corpus_db(data, rows, build_id)
    _write_derived_proof(data, "semantic-cli-scale-v1", corpusdb)
    q8_dir = data / "semantic-q8"
    built = semantic_q8.build_from_f32(
        bundle["f32"], data / "embeddings.meta", q8_dir,
        binary=RUST_BIN, groups_path=groups, numeric_groups=True)
    exact = semantic_q8._build_f16(
        bundle["f32"], q8_dir, generation=bundle["generation"],
        rows=rows, dim=DIM)
    published = embedding_segments.publish_base(
        data / "embeddings.meta", source=common.transcript_generation(),
        model_id=embedder.PROFILE_STRING, dim=DIM,
        artifacts={
            "f32": bundle["f32"], "q8": Path(built["artifact"]),
            "f16": Path(exact["exact_artifact"]),
            "groups": Path(built["group_artifact"]),
        },
        ids=bundle["ids"], hashes=bundle["hashes"], refs=refs,
        coverage={"total": rows}, expected_generation=bundle["generation"],
        _adopt_inputs=True)
    if embedding_segments.publication_artifact_identities(published) is None:
        raise RuntimeError("segmented fixture has no publisher integrity proof")
    if not indexd_runtime.record_verified_current({}):
        raise RuntimeError("semantic fixture freshness proof could not be published")
    segment = published["segments"][0]["artifacts"]
    print(json.dumps({
        "rows": rows,
        "storage": "segments-v2",
        "generation": published["generation"],
        "publication_proof": published[embedding_segments.PROOF_KEY],
        "build_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "refs_bytes": int(segment["refs"]["size"]),
        "corpus_bytes": corpus.stat().st_size,
        "q8_bytes": int(segment["q8"]["size"]),
        "f16_bytes": int(segment["f16"]["size"]),
        "f32_logical_bytes": int(segment["f32"]["size"]),
    }, sort_keys=True))
    return 0


def _run(cmd: list[str], env: dict[str, str],
         timeout: float) -> tuple[float, subprocess.CompletedProcess[str]]:
    started = time.perf_counter()
    result = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return (time.perf_counter() - started) * 1000.0, result


def _timing(stderr: str) -> dict | None:
    for line in stderr.splitlines():
        found = _TIMING_LINE.search(line)
        if found is not None:
            try:
                value = json.loads(found.group("payload"))
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _validate_q8_timing(timing: dict) -> None:
    phases = timing.get("phases_ms") or {}
    if "q8_retrieval" not in phases or "matmul" in phases:
        raise RuntimeError(
            f"semantic CLI did not prove the q8 bounded path: {sorted(phases)}")


def _search_json_payload(stdout: str) -> tuple[list[dict], dict]:
    values = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("semantic CLI JSON contained a non-object row")
        values.append(value)
    if not values or values[0].get("kind") != "agrep-meta":
        raise RuntimeError("semantic CLI JSON lacked a leading metadata row")
    if any(value.get("kind") == "agrep-meta" for value in values[1:]):
        raise RuntimeError("semantic CLI JSON contained multiple metadata rows")
    return values[1:], values[0]


def _validate_semantic(result: subprocess.CompletedProcess[str]) -> dict:
    if result.returncode != 0:
        raise RuntimeError(
            f"semantic CLI exited {result.returncode}: {(result.stderr + result.stdout)[-1000:]}")
    rows, meta = _search_json_payload(result.stdout)
    if meta.get("error"):
        raise RuntimeError(f"semantic CLI returned an error envelope: {meta['error']}")
    if not rows or any(not str(row.get("session", "")).startswith("sem-") for row in rows):
        raise RuntimeError("semantic CLI did not return planted sessions")
    planted = {_session(ordinal) for ordinal in SEMANTIC_ROWS}
    if any(str(row.get("session") or "") not in planted for row in rows):
        sessions = [str(row.get("session") or "") for row in rows]
        raise RuntimeError(f"semantic CLI returned a decoy family: {sessions}")
    freshness = meta.get("freshness")
    if (not isinstance(freshness, dict)
            or freshness.get("state") != "no-known-failure"
            or freshness.get("failing") is not False
            or freshness.get("may_be_stale") is True):
        raise RuntimeError(
            f"semantic CLI served a degraded generation: {freshness}")
    engine = str(meta.get("engine") or "")
    if "semantic" not in engine:
        raise RuntimeError(f"semantic CLI used the wrong engine: {engine}")
    timing = _timing(result.stderr)
    if timing is None:
        raise RuntimeError("semantic CLI omitted its worker timing proof")
    _validate_q8_timing(timing)
    done = list(_SEARCH_DONE.finditer(result.stderr))
    if len(done) != 1 or "semantic" not in done[0].group("engine"):
        raise RuntimeError("semantic CLI omitted its completion-engine proof")
    return {"rows": len(rows), "engine": done[0].group("engine"), "timing": timing}


def _validate_hybrid(result: subprocess.CompletedProcess[str]) -> dict:
    if result.returncode != 0:
        raise RuntimeError(
            f"hybrid CLI exited {result.returncode}: {(result.stderr + result.stdout)[-1000:]}")
    if "~semantic" not in result.stdout:
        raise RuntimeError("hybrid CLI did not render a labeled semantic result")
    if "meaning-only planted winner" not in result.stdout:
        raise RuntimeError("hybrid CLI omitted the planted meaning-only lane")
    disclosure = result.stderr.lower()
    if ("history may be stale" in disclosure
            or "torn-generation" in disclosure
            or "serving the last-good index" in disclosure
            or "spawn indexd daemon" in disclosure
            or "new daemon catches up" in disclosure
            or re.search(r"\bindex [^\n]*\bbehind\b", disclosure)):
        raise RuntimeError("hybrid CLI served a degraded generation")
    timing = _timing(result.stderr)
    if timing is None:
        raise RuntimeError("hybrid CLI omitted its worker timing proof")
    _validate_q8_timing(timing)
    done = list(_SEARCH_DONE.finditer(result.stderr))
    if len(done) != 1 or "+" not in done[0].group("engine"):
        raise RuntimeError("hybrid CLI did not prove both retrieval lanes")
    return {"engine": done[0].group("engine"), "timing": timing}


def _worker_status(env: dict[str, str]) -> dict:
    command = [
        sys.executable, "-c",
        "import json,semworker;print(json.dumps(semworker.resident_status()))",
    ]
    result = subprocess.run(
        command, cwd=PY, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10)
    return json.loads(result.stdout) if result.returncode == 0 else {"running": False}


def _freshener_status(env: dict[str, str]) -> dict:
    command = [
        sys.executable, "-c",
        "import json,indexd_runtime;print(json.dumps("
        "indexd_runtime.indexd_resource_status(observe_only=True)))",
    ]
    result = subprocess.run(
        command, cwd=PY, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10)
    try:
        if result.returncode == 0:
            return json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    return {
        "running": False,
        "error": (result.stderr + result.stdout)[-500:],
    }


def _assert_freshener(
        process: subprocess.Popen, env: dict[str, str]) -> dict:
    observed = []
    for attempt in range(3):
        if process.poll() is not None:
            raise RuntimeError(
                f"synthetic freshness owner exited early ({process.returncode})")
        status = _freshener_status(env)
        if (status.get("running") is True
                and status.get("responsive") is True
                and status.get("pid") == process.pid):
            return status
        observed.append(status)
        if attempt < 2:
            # The controlled owner self-verifies and heartbeats every 50 ms.
            # Require degradation to survive three of those periods.
            time.sleep(0.05)
    raise RuntimeError(
        f"synthetic freshness ownership moved or degraded: {observed}")


def _request_freshener_stop(
        process: subprocess.Popen, stop: Path) -> tuple[str, bool]:
    forced = False
    if process.poll() is None:
        stop.write_text("stop\n", encoding="ascii")
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            forced = True
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    stderr = process.stderr.read() if process.stderr is not None else ""
    return stderr, forced


def _start_freshener(env: dict[str, str]) -> subprocess.Popen:
    data = Path(env["AGREP_DATA_DIR"])
    marker = data / ".semantic-scale-freshener-ready"
    stop = data / ".semantic-scale-freshener-stop"
    marker.unlink(missing_ok=True)
    stop.unlink(missing_ok=True)
    script = (
        "import common,indexd_runtime,signal,threading\n"
        "from pathlib import Path\n"
        "if not common.bind_descendants_to_process_lifetime(): raise SystemExit(70)\n"
        "owner=indexd_runtime.acquire_indexd_owner()\n"
        "if owner is None: raise SystemExit(71)\n"
        "ready=None\n"
        "stop=threading.Event()\n"
        "def request_stop(*_args): stop.set()\n"
        "for signum in (signal.SIGINT,signal.SIGTERM,getattr(signal,'SIGHUP',None)):\n"
        " if signum is not None: signal.signal(signum,request_stop)\n"
        "marker=Path(indexd_runtime.common.DATA_DIR)/'.semantic-scale-freshener-ready'\n"
        "stop_path=Path(indexd_runtime.common.DATA_DIR)/'.semantic-scale-freshener-stop'\n"
        "try:\n"
        " ready=indexd_runtime.publish_indexd_ready(owner)\n"
        " marker.write_text('ready\\n',encoding='ascii')\n"
        " while not stop.wait(0.05):\n"
        "  if stop_path.exists(): break\n"
        "  indexd_runtime.heartbeat_indexd_owner(owner)\n"
        "  ready.verify()\n"
        "finally:\n"
        " marker.unlink(missing_ok=True)\n"
        " if ready is not None: ready.release(tombstone=True,require_stable_mtime=True)\n"
        " owner.release(tombstone=True,require_stable_mtime=True)\n"
    )
    kwargs = {
        "cwd": PY, "env": env, "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace",
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, "-c", script], **kwargs)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and process.poll() is None:
        if marker.is_file() and marker.read_text(encoding="ascii") == "ready\n":
            _assert_freshener(process, env)
            return process
        time.sleep(0.01)
    stderr, _forced = _request_freshener_stop(process, stop)
    stop.unlink(missing_ok=True)
    raise RuntimeError(f"synthetic freshness owner failed: {stderr[-500:]}")


def _stop_freshener(process: subprocess.Popen, env: dict[str, str]) -> None:
    data = Path(env["AGREP_DATA_DIR"])
    stop = data / ".semantic-scale-freshener-stop"
    stderr, forced = _request_freshener_stop(process, stop)
    stop.unlink(missing_ok=True)
    residue = [
        path.name
        for pattern in (
            ".indexd.v*.lock", ".indexd.v*.ready.*",
            ".semantic-scale-freshener-ready",
        )
        for path in data.glob(pattern)
    ]
    if process.returncode != 0 or forced or residue:
        raise RuntimeError(
            "synthetic freshness owner did not stop cleanly: "
            f"rc={process.returncode} forced={forced} residue={residue} "
            f"stderr={stderr[-500:]}")


def _stop_worker(env: dict[str, str]) -> None:
    script = (
        "import json,semworker\n"
        "result=semworker.stop_worker_and_wait()\n"
        "print(json.dumps(result,separators=(',',':')))\n"
        "raise SystemExit(0 if result['ok'] else 1)\n"
    )
    stopped = subprocess.run(
        [sys.executable, "-c", script], cwd=PY, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    if stopped.returncode != 0:
        detail = (stopped.stdout + stopped.stderr).strip()[-1000:]
        raise RuntimeError(
            f"semantic worker did not release its synthetic fixture: {detail}")


@contextlib.contextmanager
def _cleanup_on_exit(*actions):
    try:
        yield
    except BaseException as primary:
        errors = []
        for action in actions:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 -- preserve the primary failure
                errors.append(exc)
        if errors:
            detail = "cleanup also failed: " + "; ".join(map(str, errors))
            if hasattr(primary, "add_note"):
                primary.add_note(detail)
            else:
                print(detail, file=sys.stderr)
        raise
    else:
        errors = []
        for action in actions:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 -- run every independent cleanup
                errors.append(exc)
        if errors:
            raise RuntimeError(
                "semantic campaign cleanup failed: "
                + "; ".join(map(str, errors))) from errors[0]


def _guarded_run(
        command: list[str], env: dict[str, str], timeout: float,
        freshener: subprocess.Popen,
) -> tuple[float, subprocess.CompletedProcess[str]]:
    _assert_freshener(freshener, env)
    elapsed, result = _run(command, env, timeout)
    _assert_freshener(freshener, env)
    return elapsed, result


def _fixture_status(env: dict[str, str]) -> dict:
    script = (
        "import common,corpusdb,indexd_runtime,json,sqlite3\n"
        "path=common.DATA_DIR/'corpus.db'\n"
        "db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)\n"
        "meta=dict(db.execute(\"SELECT key,value FROM meta WHERE key IN "
        "('stamp','family_stamp','schema')\"))\n"
        "count=db.execute('SELECT count(*) FROM msgs').fetchone()[0]\n"
        "db.close()\n"
        "print(json.dumps({'rows':count,'meta':meta,'family_stamp':"
        "common.session_family_source_stamp(common.DATA_DIR),'health':"
        "corpusdb.search_generation_health(routine=False),'freshness':"
        "indexd_runtime.machine_freshness()},sort_keys=True))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PY, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            "semantic fixture verification failed: "
            f"{(result.stderr + result.stdout)[-1000:]}")
    return json.loads(result.stdout)


def _assert_fixture(env: dict[str, str], rows: int) -> dict:
    status = _fixture_status(env)
    meta = status.get("meta") or {}
    freshness = status.get("freshness") or {}
    if (status.get("rows") != rows
            or status.get("family_stamp") != meta.get("family_stamp")
            or (status.get("health") or {}).get("state") != "ready"
            or freshness.get("state") != "no-known-failure"
            or freshness.get("failing") is not False
            or freshness.get("may_be_stale") is True):
        raise RuntimeError(f"semantic fixture moved or degraded: {status}")
    return status


def _samples(command: list[str], env: dict[str, str], runs: int,
             validator, timeout: float, freshener: subprocess.Popen,
             ) -> tuple[list[float], list[dict], list[dict]]:
    values = []
    proofs = []
    resources = []
    for _ in range(runs):
        worker = _worker_status(env)
        before = (_process_group_usage(int(worker["pid"]))
                  if worker.get("running") else None)
        elapsed, result = _guarded_run(command, env, timeout, freshener)
        proof = validator(result)
        after_worker = _worker_status(env)
        after = (_process_group_usage(int(after_worker["pid"]))
                 if after_worker.get("running") else None)
        values.append(elapsed)
        proofs.append(proof)
        delta = _usage_delta(before, after)
        if delta is not None:
            resources.append(delta)
    return values, proofs, resources


def _timing_summary(proofs: list[dict]) -> dict:
    timings = [proof["timing"] for proof in proofs]
    result = {}
    for key in ("client_roundtrip_ms", "worker_search_ms", "hybrid_compute_ms"):
        values = [float(timing[key]) for timing in timings
                  if isinstance(timing.get(key), (int, float))]
        if values:
            result[key] = round(statistics.median(values), 3)
    return result


def _resource_summary(samples: list[dict]) -> dict | None:
    if not samples:
        return None
    return {
        "median_cpu_ms": round(statistics.median(
            sample["cpu_ms"] for sample in samples), 3),
        "max_rss_mib": round(max(sample["rss_mib"] for sample in samples), 3),
        "max_private_mib": round(max(
            sample["private_mib"] for sample in samples), 3),
        "max_disk_read_mib": round(max(
            sample["disk_read_mib"] for sample in samples), 3),
        "max_disk_write_mib": round(max(
            sample["disk_write_mib"] for sample in samples), 3),
    }


def _campaign(rows: int, args: argparse.Namespace, model_root: Path) -> dict:
    parent = (Path(args.temp_root).expanduser().resolve()
              if args.temp_root else Path(tempfile.gettempdir()))
    parent.mkdir(parents=True, exist_ok=True)
    projected = rows * 1_800 + 2 * GIB
    free = shutil.disk_usage(parent).free
    if not args.force_low_space and free < projected:
        raise RuntimeError(
            f"refusing {rows:,} rows: {free / GIB:.1f} GiB free, "
            f"need about {projected / GIB:.1f} GiB; pass --force-low-space to override")
    with tempfile.TemporaryDirectory(
            prefix=f"agrep-semantic-cli-{rows}-", dir=parent) as raw:
        root = Path(raw)
        data, home = root / "data", root / "home"
        data.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        env = _private_env(home, data, model_root)
        build_cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--_build-fixture", str(data), str(rows),
        ]
        built = subprocess.run(
            build_cmd, cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=args.build_timeout)
        if built.returncode != 0:
            raise RuntimeError(
                f"semantic fixture build failed: {(built.stderr + built.stdout)[-2000:]}")
        fixture = json.loads(built.stdout.splitlines()[-1])
        if (fixture.get("storage") != "segments-v2"
                or not isinstance(fixture.get("publication_proof"), dict)):
            raise RuntimeError(
                "semantic scale fixture did not publish a proven segmented generation")
        fixture["verification"] = _assert_fixture(env, rows)
        fixture["disk"] = _tree_sizes(data)
        semantic = [
            sys.executable, str(ROOT / "cli.py"), "search", "-s", QUERY,
            "--strict-semantic", "--json", "--max", "8",
        ]
        hybrid = [
            sys.executable, str(ROOT / "cli.py"), "search", QUERY,
            "--max", "8",
        ]
        freshener = _start_freshener(env)
        with _cleanup_on_exit(
                lambda: _stop_worker(env),
                lambda: _stop_freshener(freshener, env)):
            _stop_worker(env)
            cold_samples = []
            cold_proofs = []
            cold_resources = []
            cold_first = None
            for index in range(args.runs):
                _stop_worker(env)
                elapsed, result = _guarded_run(
                    semantic, env, args.query_timeout, freshener)
                cold_proof = _validate_semantic(result)
                cold_worker = _worker_status(env)
                if cold_worker.get("running"):
                    usage = _process_group_usage(int(cold_worker["pid"]))
                    if usage is not None:
                        cold_resources.append({
                            "cpu_ms": usage["user_ms"] + usage["system_ms"],
                            "disk_read_mib": usage["disk_read_mib"],
                            "disk_write_mib": usage["disk_write_mib"],
                            "rss_mib": usage["rss_mib"],
                            "private_mib": usage["private_mib"],
                        })
                cold_samples.append(elapsed)
                cold_proofs.append(cold_proof)
                if index == 0:
                    cold_first = elapsed
            _stop_worker(env)
            warmup_ms, warmup = _guarded_run(
                semantic, env, args.query_timeout, freshener)
            warmup_proof = _validate_semantic(warmup)
            worker = _worker_status(env)
            if not worker.get("running"):
                log_path = data / "semantic-worker.log"
                detail = (log_path.read_text(
                    encoding="utf-8", errors="replace")[-1500:]
                    if log_path.is_file() else "worker log absent")
                raise RuntimeError(
                    f"semantic warmup did not leave a resident worker: {detail}")
            warm_samples, warm_proofs, warm_resources = _samples(
                semantic, env, args.runs, _validate_semantic,
                args.query_timeout, freshener)
            after = _worker_status(env)
            if after.get("pid") != worker.get("pid"):
                raise RuntimeError("warm semantic samples did not reuse one worker")
            hybrid_samples = []
            hybrid_proofs = []
            hybrid_resources = []
            if rows == max(args.rows):
                hybrid_env = {**env, "AGREP_PROFILE": "compact"}
                hybrid_samples, hybrid_proofs, hybrid_resources = _samples(
                    hybrid, hybrid_env, args.runs,
                    _validate_hybrid, args.query_timeout, freshener)
            _assert_freshener(freshener, env)
            fixture["verification"] = _assert_fixture(env, rows)
        return {
            "rows": rows,
            "fixture": fixture,
            "warmup_ms": round(warmup_ms, 3),
            "warmup_proof": warmup_proof,
            "warm_semantic": {
                "median_ms": round(statistics.median(warm_samples), 3),
                "max_ms": round(max(warm_samples), 3),
                "samples_ms": [round(value, 3) for value in warm_samples],
                "daemon": _timing_summary(warm_proofs),
                "resources": _resource_summary(warm_resources),
                "proof": warm_proofs[-1],
            },
            "cold_semantic": None if not cold_samples else {
                "first_ms": round(cold_first or 0.0, 3),
                "median_ms": round(statistics.median(cold_samples), 3),
                "max_ms": round(max(cold_samples), 3),
                "samples_ms": [round(value, 3) for value in cold_samples],
                "daemon": _timing_summary(cold_proofs),
                "resources": _resource_summary(cold_resources),
                "proof": cold_proofs[-1],
            },
            "warm_hybrid": None if not hybrid_samples else {
                "median_ms": round(statistics.median(hybrid_samples), 3),
                "max_ms": round(max(hybrid_samples), 3),
                "samples_ms": [round(value, 3) for value in hybrid_samples],
                "daemon": _timing_summary(hybrid_proofs),
                "resources": _resource_summary(hybrid_resources),
                "proof": hybrid_proofs[-1],
            },
        }


def _projection(reports: list[dict]) -> dict:
    ordered = sorted(reports, key=lambda item: int(item["rows"]))
    basis_rows = [int(item["rows"]) for item in ordered]
    basis_ms = [round(float(item["warm_semantic"]["median_ms"]), 3)
                for item in ordered]
    exact = next((item for item in ordered
                  if int(item["rows"]) == PROJECTION_ROWS), None)
    if exact is not None:
        measured = round(float(exact["warm_semantic"]["median_ms"]), 3)
        return {
            "status": "valid", "kind": "measured",
            "basis_rows": [PROJECTION_ROWS],
            "basis_ms": [measured], "slope_ns_per_row": None,
            "projected_10m_ms": measured,
            "method": "measured full-CLI median at 10M rows",
        }
    eligible = [item for item in ordered
                if SMALL_ROWS <= int(item["rows"]) < PROJECTION_ROWS]
    if len(eligible) < 2:
        return {
            "status": "not-computed", "basis_rows": basis_rows,
            "basis_ms": basis_ms, "slope_ns_per_row": None,
            "projected_10m_ms": None,
            "reason": ("needs at least two measured points at or above 1M rows, "
                       "including one at or above 2M rows"),
        }
    low, high = eligible[0], eligible[-1]
    low_rows, high_rows = int(low["rows"]), int(high["rows"])
    low_ms = float(low["warm_semantic"]["median_ms"])
    high_ms = float(high["warm_semantic"]["median_ms"])
    if high_rows < DESIGN_ROWS or high_rows - low_rows < SMALL_ROWS:
        return {
            "status": "not-computed", "basis_rows": basis_rows,
            "basis_ms": basis_ms, "slope_ns_per_row": None,
            "projected_10m_ms": None,
            "reason": ("needs a scale-appropriate span of at least 1M rows "
                       "ending at or above 2M rows"),
        }
    slope = max(0.0, (high_ms - low_ms) / (high_rows - low_rows))
    projected = high_ms + slope * (PROJECTION_ROWS - high_rows)
    return {
        "status": "valid", "kind": "projected",
        "basis_rows": [low_rows, high_rows],
        "basis_ms": [round(low_ms, 3), round(high_ms, 3)],
        "slope_ns_per_row": round(slope * 1_000_000.0, 6),
        "projected_10m_ms": round(projected, 3),
        "method": "affine full-CLI fit; fixed startup/IPC held constant",
    }


def _projection_line(projection: dict) -> str:
    value = projection.get("projected_10m_ms")
    if projection.get("status") != "valid" or value is None:
        return f"10M projection: not computed ({projection['reason']})"
    kind = "measured" if projection.get("kind") == "measured" else "projected"
    return f"10M {kind} warm semantic: {float(value):.1f}ms"


def _budgets(profile: str) -> dict[str, float]:
    multiplier = PORTABLE_MULTIPLIER if profile == "portable-ci" else 1.0
    return {name: value * multiplier for name, value in TARGET_BUDGETS_MS.items()}


def _failures(reports: list[dict], projection: dict,
              budgets: dict[str, float]) -> list[str]:
    design = next((item for item in reports if int(item["rows"]) == DESIGN_ROWS), None)
    if design is None:
        return ["2,000,000-row report missing"]
    projection_value = (projection.get("projected_10m_ms")
                        if projection.get("status") == "valid" else None)
    values = {
        "warm_semantic_2m_ms": design["warm_semantic"]["median_ms"],
        "warm_hybrid_2m_ms": design["warm_hybrid"]["median_ms"],
        "cold_semantic_2m_ms": design["cold_semantic"]["median_ms"],
        "projected_warm_semantic_10m_ms": projection_value,
    }
    failures = []
    for name, budget in budgets.items():
        value = values.get(name)
        if value is None or not math.isfinite(float(value)):
            detail = projection.get("reason") if name.startswith("projected_") else None
            suffix = f" ({detail})" if detail else ""
            failures.append(f"{name}: missing or non-finite{suffix}")
        elif float(value) > budget:
            failures.append(f"{name}: {float(value):.1f}ms > {budget:.1f}ms")
    return failures


def _provenance() -> dict:
    cpu = platform.processor()
    if sys.platform == "darwin" and cpu.lower() in ("", "arm", "arm64", "i386"):
        try:
            cpu = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True,
                encoding="utf-8", errors="replace", timeout=2).strip()
        except (OSError, subprocess.SubprocessError):
            cpu = "unknown"
    memory = None
    try:
        memory = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        pass
    return {
        "os": platform.platform(), "machine": platform.machine(),
        "cpu": cpu or "unknown", "python": sys.version.split()[0],
        "logical_cpus": os.cpu_count(), "physical_memory_bytes": memory,
        "rust_binary": str(RUST_BIN),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", nargs="+", type=int)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--budget-profile", choices=("target", "portable-ci"),
                    default="target")
    ap.add_argument("--temp-root")
    ap.add_argument("--force-low-space", action="store_true")
    ap.add_argument("--build-timeout", type=float, default=900.0)
    ap.add_argument("--query-timeout", type=float, default=120.0)
    ap.add_argument("--_build-fixture", nargs=2, metavar=("DATA", "ROWS"))
    args = ap.parse_args(argv)
    if args._build_fixture:
        return args
    args.rows = list(dict.fromkeys(
        args.rows or (QUICK_ROWS if args.quick else DEFAULT_ROWS)))
    if args.runs < 1:
        ap.error("--runs must be positive")
    if args.check and args.runs < 3:
        ap.error("--check requires at least three completion samples")
    if args.check and set(args.rows) != set(DEFAULT_ROWS):
        ap.error("--check requires the 1,000,000 and 2,000,000 row campaigns")
    if not args.check and args.budget_profile != "target":
        ap.error("--budget-profile requires --check")
    if any(rows < MATERIALIZED_ROWS for rows in args.rows):
        ap.error(f"rows must be at least {MATERIALIZED_ROWS}")
    return args


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if runtime_error := _runtime_error(raw_argv):
        print(runtime_error, file=sys.stderr)
        return 2
    args = _parse_args(raw_argv)
    if args._build_fixture:
        return _build_fixture(Path(args._build_fixture[0]), int(args._build_fixture[1]))
    if not RUST_BIN.is_file():
        print("semantic CLI scale needs target/release/agrep-rs", file=sys.stderr)
        return 2
    newer = [path for path in (ROOT / "crates").rglob("*.rs")
             if path.stat().st_mtime_ns > RUST_BIN.stat().st_mtime_ns]
    if args.check and newer:
        print("release binary predates Rust sources; run cargo build --release",
              file=sys.stderr)
        return 2
    sys.path.insert(0, str(PY))
    import embedder

    model_dir = embedder.ensure_model(download=True)
    model_root = model_dir.parent
    reports = []
    for rows in args.rows:
        print(f"semantic CLI scale: {rows:,} rows", file=sys.stderr, flush=True)
        reports.append(_campaign(rows, args, model_root))
    projection = _projection(reports)
    budgets = _budgets(args.budget_profile)
    failures = _failures(reports, projection, budgets) if args.check else None
    gate_status = ("not-run" if failures is None
                   else "failed" if failures else "passed")
    payload = {
        "provenance": _provenance(), "reports": reports,
        "projection": projection, "budgets_ms": budgets,
        "budget_profile": args.budget_profile, "gate_status": gate_status,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print("rows       warm semantic   cold semantic   warm hybrid")
        for report in reports:
            cold = report["cold_semantic"]
            hybrid = report["warm_hybrid"]
            cold_label = "-" if cold is None else f"{cold['median_ms']:.1f}ms"
            hybrid_label = (
                "-" if hybrid is None else f"{hybrid['median_ms']:.1f}ms")
            print(f"{report['rows']:>9,}  "
                  f"{report['warm_semantic']['median_ms']:>11.1f}ms  "
                  f"{cold_label:>13}  {hybrid_label:>11}")
        print(_projection_line(projection))
        if failures:
            print("semantic CLI gate failed: " + "; ".join(failures), file=sys.stderr)
        elif failures is None:
            print("budget gate: not run (--check requires the full 1M/2M campaign)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
