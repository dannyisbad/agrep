#!/usr/bin/env python3
"""Controlled semantic-serving churn experiment.

This is deliberately an isolated benchmark, not a product daemon.  It drives the
same Rust ingest, semantic rebase, SQLite refresh, embed publisher, resident
semantic worker, and ``agrep recall`` process paths as the candidate checkout.
The arm switch controls only when the benchmark admits low-priority embedding
work:

* eager: publish the pending semantic tail after every transcript publication;
* debounce: keep the proven partial generation serving and batch the tail once.

The benchmark requires fresh AGREP_DATA_DIR and AGREP_HOME directories.  It never
reads or writes the default agrep data directory; the separately cached model is
opened read-only with ``--no-model-download``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY = (
    "why should the last good semantic generation stay queryable during "
    "low priority refresh work"
)


def _record(timestamp: str, kind: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": kind, "payload": payload},
        separators=(",", ":"), sort_keys=True,
    )


def _session_meta(session: str, second: int = 0) -> str:
    return _record(
        f"2026-08-05T12:00:{second:02d}Z", "session_meta",
        {"type": "session_meta", "id": session,
         "cwd": "/isolated/semantic-lifecycle"},
    )


def _turn_records(session: str, turn: int, *, live: bool) -> list[str]:
    minute = min(59, turn // 50)
    second = turn % 50
    timestamp = f"2026-08-05T12:{minute:02d}:{second:02d}Z"
    marker = "live" if live else "baseline"
    user = (
        f"{marker} semantic lifecycle evidence {session} turn {turn}: "
        "keep the last good generation queryable while low priority refresh "
        "builds newly arrived transcript rows"
    )
    assistant = (
        "The serving semantic snapshot remains available. The keyword lane "
        "covers the unpublished tail and publication swaps generations atomically."
    )
    return [
        _record(timestamp, "event_msg",
                {"type": "user_message", "message": user}),
        _record(timestamp, "response_item",
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": user}]}),
        _record(timestamp, "response_item",
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": assistant}],
                 "phase": "final_answer"}),
    ]


def _rollout_path(home: Path, session: str) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "08" / "05"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"rollout-2026-08-05T12-00-00-{session}.jsonl"


def _write_initial_sources(home: Path) -> Path:
    for number in range(8):
        session = f"00000000-0000-4000-8000-{number:012d}"
        lines = [_session_meta(session)]
        for turn in range(16):
            lines.extend(_turn_records(session, turn, live=False))
        _rollout_path(home, session).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    live_session = "11111111-1111-4111-8111-111111111111"
    live = _rollout_path(home, live_session)
    live.write_text(
        "\n".join([_session_meta(live_session),
                    *_turn_records(live_session, 0, live=True)]) + "\n",
        encoding="utf-8",
    )
    return live


def _append_live_turn(path: Path, turn: int) -> float:
    before = time.time()
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(_turn_records(
            "11111111-1111-4111-8111-111111111111", turn, live=True)))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return before


class _ProcessSampler:
    """Best-effort per-child RSS/CPU proxy using macOS ``ps``."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.rss_kib: list[int] = []
        self.cpu_percent: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() and self.process.poll() is None:
            try:
                sample = subprocess.run(
                    ["ps", "-o", "rss=", "-o", "%cpu=", "-p",
                     str(self.process.pid)], capture_output=True, text=True,
                    timeout=0.5, check=False)
                fields = sample.stdout.split()
                if len(fields) >= 2:
                    self.rss_kib.append(int(fields[0]))
                    self.cpu_percent.append(float(fields[1]))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self._stop.wait(0.02)

    def finish(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=1.0)
        return {
            "peak_rss_kib": max(self.rss_kib, default=None),
            "mean_cpu_percent": (
                round(statistics.fmean(self.cpu_percent), 3)
                if self.cpu_percent else None),
            "samples": len(self.rss_kib),
        }


def _start_process(command: list[str], env: dict[str, str]) -> tuple:
    started = time.perf_counter()
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace")
    return process, _ProcessSampler(process), started


def _finish_process(started: tuple, timeout: float = 120.0) -> dict:
    process, sampler, start = started
    stdout, stderr = process.communicate(timeout=timeout)
    resources = sampler.finish()
    return {
        "returncode": process.returncode,
        "latency_s": round(time.perf_counter() - start, 6),
        "stdout": stdout,
        "stderr": stderr,
        "resources": resources,
    }


def _query_command(python: str) -> list[str]:
    return [
        python, str(ROOT / "cli.py"), "recall", QUERY,
        "--hits", "2", "--budget", "5000", "--json", "--no-auto",
    ]


def _parse_query(result: dict) -> dict:
    payload = None
    for line in reversed(result["stdout"].splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "semantic_status" in candidate:
            payload = candidate
            break
    status = ((payload or {}).get("semantic_status") or {})
    coverage = (payload or {}).get("semantic_coverage")
    return {
        "returncode": result["returncode"],
        "latency_s": result["latency_s"],
        "semantic_state": status.get("state", "missing"),
        "semantic_complete": status.get("complete"),
        "coverage": coverage,
        "stderr": result["stderr"][-2000:],
        "resources": result["resources"],
    }


def _run_initial_reindex(env: dict[str, str], python: str) -> dict:
    started = _start_process(
        [python, str(ROOT / "reindex.py"), "--no-build"], env)
    result = _finish_process(started, timeout=300.0)
    if result["returncode"] != 0:
        raise RuntimeError(
            "initial reindex failed\n" + result["stdout"] + result["stderr"])
    return {key: value for key, value in result.items()
            if key not in {"stdout", "stderr"}}


def _coverage(coherence: dict) -> dict:
    raw = coherence.get("coverage") or {}
    return {
        "state": coherence.get("state"),
        "searchable": bool(coherence.get("searchable")),
        "coherent": bool(coherence.get("coherent")),
        "indexed": raw.get("indexed"),
        "total": raw.get("total"),
        "pending": raw.get("pending"),
        "generation": coherence.get("generation"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("eager", "debounce"), required=True)
    parser.add_argument("--publications", type=int, default=20)
    parser.add_argument("--seed", type=int, default=8052026)
    args = parser.parse_args()
    if args.publications < 20:
        parser.error("--publications must be at least 20")

    data_text = os.environ.get("AGREP_DATA_DIR", "")
    home_text = os.environ.get("AGREP_HOME", "")
    if not data_text or not Path(data_text).is_absolute():
        parser.error("AGREP_DATA_DIR must be a fresh absolute directory")
    if not home_text or not Path(home_text).is_absolute():
        parser.error("AGREP_HOME must be a fresh absolute directory")
    data = Path(data_text)
    home = Path(home_text)
    if data.exists() and any(data.iterdir()):
        parser.error("AGREP_DATA_DIR must be empty")
    if home.exists() and any(home.iterdir()):
        parser.error("AGREP_HOME must be empty")
    data.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    live_path = _write_initial_sources(home)

    python = sys.executable
    env = dict(os.environ)
    env.update({
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_HOME": str(home),
        "AGREP_NO_DAEMON": "1",
        "AGREP_ON_BATTERY": "0",
        "AGREP_PROFILE": "compact",
        "AGREP_SEM_THREADS": "2",
    })
    env.pop("AGREP_DATA_READONLY", None)
    query_env = dict(env)
    # Keep the transcript/index daemon disabled for deterministic manual
    # publications while permitting the independently owned semantic server.
    query_env.pop("AGREP_NO_DAEMON", None)
    query_env.pop("AGREP_NO_SEM_WORKER", None)
    initial = _run_initial_reindex(env, python)

    # Imports must happen only after the per-process data-root contract is set.
    sys.path.insert(0, str(ROOT / "py"))
    import common
    import corpusdb
    import embed
    import indexd_runtime
    import semantic
    import semworker

    binary = common.ingest_bin()
    initial_coherence = semantic.embedding_coherence()
    if not initial_coherence.get("coherent"):
        raise RuntimeError(f"initial semantic generation is not coherent: "
                           f"{initial_coherence}")

    warm = _parse_query(_finish_process(
        _start_process(_query_command(python), query_env), timeout=60.0))
    if warm["semantic_state"] == "unavailable":
        raise RuntimeError(f"semantic warmup unavailable: {warm}")

    rng = random.Random(args.seed)
    # Eager offsets cross the embedding publication. Debounce deliberately
    # removes that overlap; its jitter samples normal post-ingest query cadence.
    offsets = [round(rng.uniform(-0.06, 0.18), 6)
               for _ in range(args.publications)]
    trials = []
    appended_at: dict[int, float] = {}
    semantic_ready_at: dict[int, float] = {}

    for publication in range(1, args.publications + 1):
        appended_at[publication] = _append_live_turn(live_path, publication)
        before = semantic.source_generation()
        ingest_started = _start_process(
            [str(binary), "index", "--agent", "codex"],
            indexd_runtime.rust_writer_env(binary))
        ingest = _finish_process(ingest_started, timeout=120.0)
        if ingest["returncode"] != 0:
            raise RuntimeError(
                f"ingest {publication} failed: {ingest['stderr']}")
        after = semantic.source_generation()
        changed = corpusdb._read_changed()
        rebase_started = time.perf_counter()
        rebased = embed.rebase_generation_marker(
            changed, expected_previous_source=before,
            expected_current_source=after)
        rebase_s = time.perf_counter() - rebase_started
        refresh_started = time.perf_counter()
        refreshed = indexd_runtime.refresh_search_index(quiet=True)
        refresh_s = time.perf_counter() - refresh_started
        before_query = semantic.embedding_coherence()
        offset = offsets[publication - 1]

        embed_result = None
        if args.arm == "eager":
            embed_command = [
                python, str(ROOT / "py" / "embed.py"),
                "--max-new", "512", "--no-model-download",
            ]
            if offset < 0:
                query_started = _start_process(
                    _query_command(python), query_env)
                time.sleep(-offset)
                embed_started = _start_process(embed_command, env)
            else:
                embed_started = _start_process(embed_command, env)
                time.sleep(offset)
                query_started = _start_process(
                    _query_command(python), query_env)
            query = _parse_query(_finish_process(query_started, timeout=60.0))
            embed_result = _finish_process(embed_started, timeout=180.0)
            if embed_result["returncode"] != 0:
                raise RuntimeError(
                    f"embed {publication} failed: {embed_result['stderr']}")
        else:
            if offset > 0:
                time.sleep(offset)
            query = _parse_query(_finish_process(
                _start_process(_query_command(python), query_env),
                timeout=60.0))

        after_trial = semantic.embedding_coherence()
        if after_trial.get("coherent"):
            semantic_ready_at[publication] = time.time()
        trial = {
            "publication": publication,
            "offset_s": offset,
            "query_relative_to_embed_start_s": (
                offset if args.arm == "eager" else None),
            "source_moved": before != after,
            "source_before": ((before or {}).get("ingest_signature")
                              if isinstance(before, dict) else None),
            "source_after": ((after or {}).get("ingest_signature")
                             if isinstance(after, dict) else None),
            "ingest_s": ingest["latency_s"],
            "ingest_resources": ingest["resources"],
            "rebase_s": round(rebase_s, 6),
            "rebased": rebased,
            "search_refresh_s": round(refresh_s, 6),
            "search_refreshed": refreshed,
            "before_query": _coverage(before_query),
            "query": query,
            "after_trial": _coverage(after_trial),
            "embed": (None if embed_result is None else {
                "latency_s": embed_result["latency_s"],
                "resources": embed_result["resources"],
            }),
        }
        trial["hard_unavailable"] = bool(
            before_query.get("searchable")
            and query["semantic_state"] == "unavailable")
        trial["last_good_served"] = bool(
            before_query.get("searchable")
            and query["semantic_state"] != "unavailable")
        trial["semantic_no_match"] = query["semantic_state"] in {
            "no-confident-match", "no-match", "empty",
        }
        trial["refresh_complete"] = bool(after_trial.get("coherent"))
        if after_trial.get("coherent"):
            trial["newest_row_semantic_visibility_proxy_s"] = round(
                semantic_ready_at[publication] - appended_at[publication], 6)
            trial["refresh_lag_s"] = (
                trial["newest_row_semantic_visibility_proxy_s"])
        trials.append(trial)
        print(json.dumps({"kind": "trial", **trial}, sort_keys=True),
              flush=True)

    final_embed = None
    if args.arm == "debounce":
        started_at = time.time()
        final_embed_raw = _finish_process(_start_process([
            python, str(ROOT / "py" / "embed.py"),
            "--max-new", "512", "--no-model-download",
        ], env), timeout=180.0)
        if final_embed_raw["returncode"] != 0:
            raise RuntimeError("final batched embed failed: "
                               + final_embed_raw["stderr"])
        ready = semantic.embedding_coherence()
        if not ready.get("coherent"):
            raise RuntimeError(f"batched embed did not converge: {ready}")
        landed = time.time()
        for publication in appended_at:
            semantic_ready_at[publication] = landed
        final_embed = {
            "started_at": started_at,
            "latency_s": final_embed_raw["latency_s"],
            "resources": final_embed_raw["resources"],
            "coverage": _coverage(ready),
        }

    unavailable = sum(
        trial["query"]["semantic_state"] == "unavailable" for trial in trials)
    hard_unavailable = sum(trial["hard_unavailable"] for trial in trials)
    latencies = [trial["query"]["latency_s"] for trial in trials]
    newest_delays = [
        semantic_ready_at[index] - appended_at[index]
        for index in sorted(semantic_ready_at)
    ]
    pending = [int((trial["before_query"].get("pending") or 0))
               for trial in trials]
    summary = {
        "kind": "summary",
        "schema": 1,
        "arm": args.arm,
        "publications": args.publications,
        "real_paths": [
            "Rust Codex ingest", "segmented semantic rebase",
            "derived SQLite refresh", "Granite ONNX embedding publication",
            "resident semantic worker", "agrep recall JSON surface",
        ],
        "simulated_or_controlled": [
            "synthetic Codex transcript content",
            "benchmark-controlled eager/debounce admission timing",
            "candidate source checkout plus installed dependency environment; "
            "not an installed candidate wheel",
            "newest-row visibility is represented by source-bound complete "
            "coverage; targeted row retrieval is not claimed",
        ],
        "arms_predeclared": {
            "eager": "run",
            "debounce": "run",
            "query_priority": (
                "not supported by a production control seam in this candidate; "
                "not fabricated"),
        },
        "initial_reindex": initial,
        "warmup": warm,
        "semantic_unavailable": unavailable,
        "hard_unavailable_with_searchable_last_good": hard_unavailable,
        "query_latency_s": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "p95": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
            "max": max(latencies),
        },
        "semantic_staleness_pending_rows": {
            "min": min(pending), "median": statistics.median(pending),
            "max": max(pending),
        },
        "newest_row_semantic_delay_s": {
            "min": min(newest_delays),
            "median": statistics.median(newest_delays),
            "max": max(newest_delays),
        },
        "final_embed": final_embed,
        "final_coverage": _coverage(semantic.embedding_coherence()),
        "offset_seed": args.seed,
    }
    result_path = data / "semantic-lifecycle-result.json"
    result_path.write_text(
        json.dumps({"summary": summary, "trials": trials},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps({**summary, "result_path": str(result_path)},
                     sort_keys=True), flush=True)
    try:
        # The harness parent keeps AGREP_NO_DAEMON set, but stopping an already
        # identified worker is still safe and exact. Temporarily lift the query
        # disable flag so the public stop path can coordinate with that owner.
        saved_no_daemon = os.environ.pop("AGREP_NO_DAEMON", None)
        semworker.stop_worker_and_wait(grace_s=3.0, fallback_s=2.0)
    except Exception:
        pass
    finally:
        if saved_no_daemon is not None:
            os.environ["AGREP_NO_DAEMON"] = saved_no_daemon
    return 0 if hard_unavailable == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
