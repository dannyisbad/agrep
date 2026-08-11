#!/usr/bin/env python3
"""Frozen end-to-end recall parity gate for lexical and semantic lanes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
FIXTURE_PATH = Path(__file__).with_name("semantic_recall_fixture.json")
RUST_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
DIM = 384
MIB = 1024 * 1024
_TIMING_LINE = re.compile(r"semantic timing (?P<payload>\{.*\})$")
_PARITY_AUTO_SEMANTIC_TIMEOUT_S = 2.5


def _fixture() -> dict:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    tasks = value.get("tasks") if isinstance(value, dict) else None
    sizes = value.get("corpus_sizes") if isinstance(value, dict) else None
    rows_per_task = int(value.get("semantic_rows_per_task", 0))
    required = {
        "id", "query", "semantic_text", "keyword_text",
        "expected_keyword_session", "expected_semantic_session",
    }
    if (value.get("version") != 1 or not isinstance(tasks, list) or not tasks
            or not isinstance(sizes, list) or len(sizes) < 2
            or rows_per_task < 128 or rows_per_task % 4
            or any(not isinstance(task, dict) or set(task) != required
                   for task in tasks)
            or len({task["id"] for task in tasks}) != len(tasks)):
        raise ValueError("semantic recall fixture has an invalid shape")
    materialized = rows_per_task * len(tasks) + len(tasks)
    if any(not isinstance(size, int) or size < materialized for size in sizes):
        raise ValueError("semantic recall corpus sizes are too small")
    return value


def _private_env(home: Path, data: Path, model_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
        "XDG_CONFIG_HOME", "AGREP_PROFILE", "AGREP_RS_BIN", "CODEX_THREAD_ID",
        "CODEX_SANDBOX", "CLAUDECODE",
        "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE", "GEMINI_CLI",
        "CLINE_ACTIVE", "CURSOR_AGENT", "AGREP_NO_DAEMON",
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


def _semantic_layout(fixture: dict, ordinal: int) -> tuple[dict, int] | None:
    per_task = int(fixture["semantic_rows_per_task"])
    task_index, local = divmod(ordinal, per_task)
    tasks = fixture["tasks"]
    if task_index >= len(tasks):
        return None
    return tasks[task_index], local


def _row_identity(fixture: dict, ordinal: int) -> tuple[str, int, str]:
    semantic = _semantic_layout(fixture, ordinal)
    if semantic is not None:
        task, local = semantic
        session = f"sem-{task['id']}-{local // 4:04d}"
        return session, local % 4, f"fixture:{session}:{local % 4}"
    semantic_rows = (
        int(fixture["semantic_rows_per_task"]) * len(fixture["tasks"]))
    lexical_index = ordinal - semantic_rows
    if 0 <= lexical_index < len(fixture["tasks"]):
        session = fixture["tasks"][lexical_index]["expected_keyword_session"]
        return session, 0, f"fixture:{session}:0"
    session = f"fill-{ordinal // 4:07d}"
    return session, ordinal % 4, f"fixture:{session}:{ordinal % 4}"


def _materialized_text(fixture: dict, ordinal: int) -> str | None:
    semantic = _semantic_layout(fixture, ordinal)
    if semantic is not None:
        task, local = semantic
        return f"{task['semantic_text']} Evidence record {local}."
    semantic_rows = (
        int(fixture["semantic_rows_per_task"]) * len(fixture["tasks"]))
    lexical_index = ordinal - semantic_rows
    if 0 <= lexical_index < len(fixture["tasks"]):
        return fixture["tasks"][lexical_index]["keyword_text"]
    return None


def _source_rows(data: Path, fixture: dict) -> None:
    total = (int(fixture["semantic_rows_per_task"]) * len(fixture["tasks"])
             + len(fixture["tasks"]))
    sessions: dict[str, list[dict]] = {}
    now = 1_800_000_000_000
    with (data / "messages.jsonl").open("wb", buffering=MIB) as stream:
        for ordinal in range(total):
            session, turn, mid = _row_identity(fixture, ordinal)
            row = {
                "id": mid, "session": session, "agent": "codex",
                "project": "/semantic-parity", "model": "parity-fixture",
                "model_source": "explicit", "turn": turn,
                "ts": now - ordinal, "who": "user",
                "text": _materialized_text(fixture, ordinal),
            }
            raw = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            stream.write(raw)
            sessions.setdefault(session, []).append(row)
    (data / "replies.jsonl").write_text("", encoding="utf-8")
    with (data / "sessions.jsonl").open(
            "w", encoding="utf-8", newline="\n") as stream:
        for session, rows in sessions.items():
            stream.write(json.dumps({
                "session": session, "agent": "codex",
                "project": "/semantic-parity", "n": len(rows),
                "first_ts": rows[-1]["ts"], "last_ts": rows[0]["ts"],
                "first_text": rows[0]["text"],
            }, separators=(",", ":")) + "\n")
    (data / ".ingest.sig").write_text(
        "semantic-recall-parity-v1\n", encoding="utf-8")
    # The segment builder refuses to publish without the Rust-authored
    # session-family proof once sessions.jsonl exists; publish the same
    # proof shape here (identity families, real digest).
    import common
    pairs = sorted((session, "") for session in sessions)
    (data / common.SESSION_FAMILY_META_FILE).write_text(
        json.dumps({
            "version": common.SESSION_FAMILY_INDEX_VERSION,
            "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
            "ingest_signature": "semantic-recall-parity-v1",
            "count": len(pairs),
            "digest": common.session_family_digest(pairs),
        }, separators=(",", ":")), encoding="utf-8")


def _variant(query: np.ndarray, offset: int) -> np.ndarray:
    other = np.roll(query, offset + 1).astype(np.float32, copy=False)
    other = other - query * float(other @ query)
    norm = float(np.linalg.norm(other))
    if norm < 1e-6:
        other = np.zeros_like(query)
        other[(offset + 17) % len(query)] = 1.0
        other = other - query * float(other @ query)
        norm = float(np.linalg.norm(other))
    other /= norm
    value = 0.96 * query + np.float32((1.0 - 0.96 ** 2) ** 0.5) * other
    return value / np.linalg.norm(value)


def _embedding_bundle(data: Path, rows: int, fixture: dict) -> dict:
    import common
    import embedding_segments
    import embedder
    import semantic_segment_build

    model = embedder.Embedder(download=False)
    queries = [model.embed_query(task["query"]) for task in fixture["tasks"]]
    del model
    matrix_path = data / ".parity-base.f32"
    with matrix_path.open("wb") as stream:
        stream.truncate(rows * DIM * 4)
    per_task = int(fixture["semantic_rows_per_task"])
    with matrix_path.open("r+b", buffering=0) as stream:
        for task_index, query in enumerate(queries):
            alternative = _variant(query, task_index)
            block = np.repeat(alternative.reshape(1, -1), per_task, axis=0)
            block[:4] = query
            stream.seek(task_index * per_task * DIM * 4)
            stream.write(block.astype("<f4", copy=False).tobytes(order="C"))

    ids, hashes, messages = [], [], []
    now = 1_800_000_000_000
    for ordinal in range(rows):
        session, turn, mid = _row_identity(fixture, ordinal)
        text = _materialized_text(fixture, ordinal) or "parity corpus filler"
        ids.append(mid)
        hashes.append(hashlib.blake2b(
            text.encode(), digest_size=8).hexdigest())
        messages.append(common.Message(
            mid, "codex", "/semantic-parity", session, now - ordinal,
            turn, text, "user", "parity-fixture", "explicit"))
    matrix = np.memmap(matrix_path, dtype="<f4", mode="r", shape=(rows, DIM))
    try:
        with semantic_segment_build.prepare(
                [matrix], ids, hashes, messages, dim=DIM,
                model_id=embedder.PROFILE_STRING) as prepared:
            return embedding_segments.publish_base(
                data / "embeddings.meta", source=common.transcript_generation(),
                model_id=embedder.PROFILE_STRING, dim=DIM,
                artifacts=prepared.artifacts, ids=ids, hashes=hashes,
                refs=prepared.refs, coverage={"total": rows})
    finally:
        common.close_embedding_matrix(matrix)
        matrix_path.unlink(missing_ok=True)


def _corpus_db(data: Path, rows: int, fixture: dict) -> Path:
    import corpusdb
    import indexd_runtime

    # Semantic writers require the Rust-anchored derived-store owner once
    # corpus.db exists; publish the same anchor + DB owner the ingest would.
    build_id = indexd_runtime.derived_writer_build_id(require_binary=True)
    (data / ".derived-owner.json").write_text(
        json.dumps({"version": 1, "build_id": build_id},
                   separators=(",", ":")), encoding="utf-8")
    path = data / "corpus.db"
    db = sqlite3.connect(path)
    db.executescript(
        "PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF;" + corpusdb._SCHEMA_SQL)
    insert = (
        "INSERT INTO msgs(session,turn,ts,agent,project,concept,model,"
        "model_source,who,text) VALUES(?,?,?,?,?,?,?,?,?,?)")
    now = 1_800_000_000_000
    batch = []
    for ordinal in range(rows):
        session, turn, _ = _row_identity(fixture, ordinal)
        text = _materialized_text(fixture, ordinal) or "parity corpus filler"
        batch.append((
            session, turn, now - ordinal, "codex", "/semantic-parity", "",
            "parity-fixture", "explicit", "user", text))
        if len(batch) == 8192:
            db.executemany(insert, batch)
            batch.clear()
    if batch:
        db.executemany(insert, batch)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid,text) SELECT id,text FROM msgs")
    db.executescript(corpusdb._TRIGGERS_SQL)
    import common
    db.execute(
        "INSERT INTO session_family(session,root,side) "
        "SELECT DISTINCT session, session, 0 FROM msgs")
    db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", (
        ("stamp", corpusdb._stamp()), ("schema", corpusdb._SCHEMA),
        ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
        ("build_id", build_id),
        ("family_stamp", common.session_family_source_stamp(data))))
    db.commit()
    db.close()
    return path


def _build_fixture(data: Path, rows: int) -> int:
    sys.path.insert(0, str(PY))
    import semantic_q8

    fixture = _fixture()
    started = time.perf_counter()
    data.mkdir(parents=True, exist_ok=True)
    _source_rows(data, fixture)
    # corpus.db first: the segment builder reads family roots through its
    # stamp-validated session_family index.
    corpus = _corpus_db(data, rows, fixture)
    manifest = _embedding_bundle(data, rows, fixture)
    generation = str(manifest["generation"])
    if not semantic_q8.artifact_available(generation):
        raise RuntimeError("semantic parity fixture has no exact-f16 accelerator")
    refs_bytes = sum(int(segment["artifacts"]["refs"]["size"])
                     for segment in manifest["segments"])
    q8_bytes = sum(int(segment["artifacts"]["q8"]["size"])
                   for segment in manifest["segments"])
    f16_bytes = sum(int(segment["artifacts"]["f16"]["size"])
                    for segment in manifest["segments"])
    print(json.dumps({
        "rows": rows,
        "build_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "layout": "segmented-v2",
        "refs_bytes": refs_bytes,
        "corpus_bytes": corpus.stat().st_size,
        "q8_bytes": q8_bytes,
        "f16_bytes": f16_bytes,
        "exact_score_kind": semantic_q8.EXACT_SCORE_KIND,
    }, sort_keys=True))
    return 0


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


def _run_recall(task: dict, lane: str, env: dict[str, str]) -> dict:
    recall_args = [
        task["query"],
        "--json", "--hits", "8", "--budget", "4000", "--no-auto", "--no-self",
    ]
    if lane == "keyword":
        recall_args.append("--lexical")
    elif lane == "semantic":
        recall_args.append("--semantic")
    if lane == "hybrid":
        launcher = (
            "import sys\n"
            f"sys.path.insert(0, {str(PY)!r})\n"
            "import recall\n"
            "raise SystemExit(recall.main("
            f"sys.argv[1:], auto_semantic_timeout_s={_PARITY_AUTO_SEMANTIC_TIMEOUT_S!r}))\n"
        )
        command = [sys.executable, "-c", launcher, *recall_args]
    else:
        command = [
            sys.executable, str(ROOT / "cli.py"), "recall", *recall_args]
    result = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"{task['id']} {lane} exited {result.returncode}: "
            f"{(result.stderr + result.stdout)[-1200:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{task['id']} {lane} returned invalid JSON") from exc
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        raise RuntimeError(f"{task['id']} {lane} omitted its hit list")
    stderr = result.stderr
    if lane != "keyword" and "semantic" not in str(payload.get("engine") or ""):
        log_path = Path(env["AGREP_DATA_DIR"]) / "semantic-worker.log"
        if log_path.is_file():
            stderr += "\nworker:\n" + log_path.read_text(
                encoding="utf-8", errors="replace")[-1200:]
    return {"payload": payload, "stderr": stderr, "hits": hits}


def _require_session(result: dict, session: str, lane: str | None) -> dict:
    found = next((hit for hit in result["hits"]
                  if hit.get("session") == session), None)
    if found is None:
        shown = [hit.get("session") for hit in result["hits"]]
        raise RuntimeError(
            f"lost expected {lane or 'keyword'} hit {session}; got {shown}; "
            f"engine={result['payload'].get('engine')}; "
            f"stderr={result['stderr'][-600:]}")
    if lane == "semantic" and found.get("lane") != "semantic":
        raise RuntimeError(f"semantic hit {session} lost its lane label")
    if lane is None and (found.get("lane") == "semantic"
                         or found.get("sem_score") is not None):
        raise RuntimeError(f"keyword hit {session} absorbed a semantic score")
    return found


def _require_q8(result: dict, task_id: str, lane: str) -> dict:
    timing = _timing(result["stderr"])
    phases = (timing or {}).get("phases_ms") or {}
    if "q8_retrieval" not in phases or "matmul" in phases:
        raise RuntimeError(
            f"{task_id} {lane} did not use q8 candidates plus exact f16: "
            f"{sorted(phases)}")
    return timing


def _campaign(rows: int, fixture: dict, model_root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"agrep-semantic-parity-{rows}-") as raw:
        root = Path(raw)
        data, home = root / "data", root / "home"
        data.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        env = _private_env(home, data, model_root)
        built = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             "--_build-fixture", str(data), str(rows)],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        if built.returncode != 0:
            raise RuntimeError(
                f"{rows:,}-row parity fixture failed: "
                f"{(built.stderr + built.stdout)[-2000:]}")
        build = json.loads(built.stdout.splitlines()[-1])
        checks = []
        with _cleanup_on_exit(lambda: _stop_worker(env)):
            for task in fixture["tasks"]:
                lexical = _run_recall(task, "keyword", env)
                semantic = _run_recall(task, "semantic", env)
                hybrid = _run_recall(task, "hybrid", env)
                _require_session(
                    lexical, task["expected_keyword_session"], None)
                _require_session(
                    semantic, task["expected_semantic_session"], "semantic")
                _require_session(
                    hybrid, task["expected_keyword_session"], None)
                _require_session(
                    hybrid, task["expected_semantic_session"], "semantic")
                if "semantic" in str(lexical["payload"].get("engine") or ""):
                    raise RuntimeError(f"{task['id']} lexical lane invoked semantics")
                hybrid_engine = str(hybrid["payload"].get("engine") or "")
                if "semantic" not in hybrid_engine or "+" not in hybrid_engine:
                    raise RuntimeError(
                        f"{task['id']} hybrid did not preserve both engines")
                sem_timing = _require_q8(semantic, task["id"], "semantic")
                hybrid_timing = _require_q8(hybrid, task["id"], "hybrid")
                checks.append({
                    "task": task["id"],
                    "keyword_engine": lexical["payload"].get("engine"),
                    "semantic_engine": semantic["payload"].get("engine"),
                    "hybrid_engine": hybrid_engine,
                    "semantic_phases": sorted(sem_timing["phases_ms"]),
                    "hybrid_phases": sorted(hybrid_timing["phases_ms"]),
                })
        return {"rows": rows, "build": build, "checks": checks}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--_build-fixture", nargs=2, metavar=("DATA", "ROWS"))
    args = parser.parse_args(argv)
    if args.check and args.quick:
        parser.error("--check and --quick are mutually exclusive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args._build_fixture:
        return _build_fixture(
            Path(args._build_fixture[0]), int(args._build_fixture[1]))
    if not RUST_BIN.is_file():
        print("semantic recall parity needs target/release/agrep-rs", file=sys.stderr)
        return 2
    fixture = _fixture()
    sizes = fixture["corpus_sizes"][:1] if args.quick else fixture["corpus_sizes"]
    if args.check and sizes != fixture["corpus_sizes"]:
        print("semantic recall parity check requires every frozen corpus size",
              file=sys.stderr)
        return 2
    sys.path.insert(0, str(PY))
    import embedder

    model_root = embedder.ensure_model(download=True).parent
    reports = []
    try:
        for rows in sizes:
            print(f"semantic recall parity: {rows:,} rows", file=sys.stderr)
            reports.append(_campaign(int(rows), fixture, model_root))
    except (OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"semantic recall parity failed: {exc}", file=sys.stderr)
        return 1
    payload = {"fixture_version": fixture["version"], "reports": reports}
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for report in reports:
            print(f"{report['rows']:>9,} rows  "
                  f"{len(report['checks'])}/{len(fixture['tasks'])} tasks  "
                  "keyword + semantic + hybrid expected hits preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
