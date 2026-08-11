#!/usr/bin/env python3
"""Run a frozen recall task set through the user-facing CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = Path(__file__).with_name("semantic_worth_tasks.example.json")


def _tasks(path: Path, selected: set[str]) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("semantic worth tasks must be a JSON array")
    found = [row for row in rows if not selected or row.get("id") in selected]
    missing = selected - {str(row.get("id")) for row in found}
    if missing:
        raise ValueError(f"unknown task ids: {', '.join(sorted(missing))}")
    return found


def _evidence(hit: dict, limit: int = 900) -> str:
    parts = []
    for row in hit.get("window") or []:
        text = row.get("text")
        if isinstance(text, str) and text:
            parts.append(" ".join(text.split()))
    return " ".join(parts)[:limit]


def _expected_rank(hits: list[dict], expected: list[str]) -> int | None:
    """Return the first result page containing an expected session/turn."""
    wanted: list[tuple[str, int]] = []
    for raw in expected:
        session, sep, turn = str(raw).rpartition(":")
        if not sep:
            continue
        try:
            wanted.append((session, int(turn)))
        except ValueError:
            continue
    for rank, hit in enumerate(hits, 1):
        rows = [hit, *(hit.get("window") or [])]
        for row in rows:
            session = str(row.get("session") or "")
            try:
                turn = int(row.get("turn"))
            except (TypeError, ValueError):
                continue
            if any(session.startswith(prefix) and turn == wanted_turn
                   for prefix, wanted_turn in wanted):
                return rank
    return None


def _snapshot_digest(data_dir: Path) -> str:
    """Bind a run to the transcript and keyword-index bytes it searched."""
    digest = hashlib.sha256()
    for name in ("messages.jsonl", "replies.jsonl", "corpus.db"):
        path = data_dir / name
        digest.update(name.encode("utf-8"))
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
        except FileNotFoundError:
            digest.update(b"\0missing")
    return digest.hexdigest()


def _run(task: dict, lane: str, budget: int, hits: int,
         env: dict[str, str]) -> dict:
    command = [
        sys.executable, str(ROOT / "cli.py"), "recall", task["query"],
        "--json", "--hits", str(hits), "--budget", str(budget),
        "--no-auto", "--no-self",
    ]
    if lane == "lexical":
        command.append("--lexical")
    elif lane == "semantic":
        command.append("--semantic")
    started = time.perf_counter()
    process = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=env,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    payload = {}
    if process.stdout.strip():
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload = {"parse_error": process.stdout[-1000:]}
    compact_hits = []
    raw_hits = payload.get("hits") or []
    for hit in raw_hits:
        compact_hits.append({
            "session": hit.get("session"),
            "turn": hit.get("turn"),
            "project": hit.get("project"),
            "matched": hit.get("matched"),
            "lane": hit.get("lane"),
            "sem_score": hit.get("sem_score"),
            "evidence": _evidence(hit),
        })
    expected_rank = _expected_rank(raw_hits, task.get("expected") or [])
    return {
        "id": task["id"],
        "category": task["category"],
        "query": task["query"],
        "target": task["target"],
        "lane": lane,
        "returncode": process.returncode,
        "elapsed_ms": round(elapsed_ms, 3),
        "engine": payload.get("engine"),
        "target_found": expected_rank is not None,
        "expected_rank": expected_rank,
        "hits": compact_hits,
        "stderr": process.stderr.strip()[-500:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--tasks", type=Path, default=TASKS_PATH)
    parser.add_argument(
        "--lane", choices=("lexical", "hybrid", "semantic"),
        action="append", default=[])
    parser.add_argument("--budget", type=int, default=6000)
    parser.add_argument("--hits", type=int, default=3)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.budget < 256 or not 1 <= args.hits <= 20:
        parser.error("--budget must be >=256 and --hits must be 1..20")
    lanes = args.lane or ["lexical", "hybrid", "semantic"]
    try:
        tasks = _tasks(args.tasks, set(args.task))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    env = dict(os.environ)
    if args.data_dir:
        env["AGREP_DATA_DIR"] = str(args.data_dir.resolve())
    if args.profile:
        env["AGREP_BENCH_EMBED_PROFILE"] = str(args.profile.resolve())
    configured_data_dir = env.get("AGREP_DATA_DIR")
    if configured_data_dir:
        data_dir = Path(configured_data_dir).expanduser()
    else:
        sys.path.insert(0, str(ROOT / "py"))
        import common
        data_dir = common.DATA_DIR
    before = _snapshot_digest(data_dir)
    output = []
    for task in tasks:
        for lane in lanes:
            output.append(_run(task, lane, args.budget, args.hits, env))
    after = _snapshot_digest(data_dir)
    if before != after:
        raise RuntimeError("semantic worth source snapshot moved during the run")
    for row in output:
        row["snapshot_sha256"] = before
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
