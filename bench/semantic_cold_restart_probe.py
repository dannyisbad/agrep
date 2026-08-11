#!/usr/bin/env python3
"""Repeat real semantic-worker cold starts against an isolated churn corpus."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


QUERY = (
    "why should the last good semantic generation stay queryable during "
    "low priority refresh work"
)
PREFLIGHT = """
import json
import sys
sys.path.insert(0, "py")
import indexd_runtime
import semantic
owner = indexd_runtime.derived_writer_mutation_info()
print(json.dumps({
    "owner_state": owner.state,
    "owner_build_id": owner.build_id,
    "owner_reason": owner.reason,
    "coherence": semantic.embedding_coherence(),
}, sort_keys=True))
"""
STOP = """
import json
import sys
sys.path.insert(0, "py")
import semworker
print(json.dumps({"stopped": bool(semworker.stop_worker_and_wait(
    grace_s=3.0, fallback_s=2.0))}, sort_keys=True))
"""


def _last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _run(command: list[str], *, repo: Path, env: dict[str, str],
         timeout: float) -> dict:
    started = time.perf_counter()
    process = subprocess.run(
        command, cwd=repo, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False)
    return {
        "returncode": process.returncode,
        "latency_s": round(time.perf_counter() - started, 6),
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def _stop(python: str, *, repo: Path, env: dict[str, str]) -> dict:
    raw = _run([python, "-c", STOP], repo=repo, env=env, timeout=15.0)
    parsed = _last_json(raw["stdout"])
    return {
        "returncode": raw["returncode"],
        "latency_s": raw["latency_s"],
        "stopped": parsed.get("stopped"),
        "stderr": raw["stderr"][-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--restarts", type=int, default=6)
    parser.add_argument(
        "--callers", type=int, default=1,
        help="real recall processes launched concurrently after each cold stop")
    parser.add_argument(
        "--quiet", action="store_true",
        help="write the full artifact but print only the final summary")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    data = args.data_dir.resolve()
    home = args.home.resolve()
    if args.restarts < 1:
        parser.error("--restarts must be positive")
    if not 1 <= args.callers <= 16:
        parser.error("--callers must be between 1 and 16")
    if not (repo / "cli.py").is_file():
        parser.error("--repo must contain cli.py")
    # This sentinel intentionally refuses arbitrary or live data roots. The
    # lifecycle harness leaves this manifest only in its isolated corpus.
    manifest = data / "semantic-lifecycle-result.json"
    if not manifest.is_file():
        parser.error("--data-dir is not a semantic lifecycle corpus")
    if not (home / ".codex" / "sessions").is_dir():
        parser.error("--home is not the synthetic lifecycle source tree")

    env = dict(os.environ)
    env.update({
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_HOME": str(home),
        "AGREP_RS_BIN": str(args.binary.resolve()),
        "AGREP_ON_BATTERY": "0",
        "AGREP_PROFILE": "compact",
        "AGREP_SEM_THREADS": "2",
    })
    env.pop("AGREP_NO_DAEMON", None)
    env.pop("AGREP_NO_SEM_WORKER", None)
    env.pop("AGREP_DATA_READONLY", None)

    preflight_raw = _run(
        [args.python, "-c", PREFLIGHT], repo=repo, env=env, timeout=15.0)
    preflight = _last_json(preflight_raw["stdout"])
    coherence = preflight.get("coherence") or {}
    if (preflight_raw["returncode"] != 0
            or preflight.get("owner_state") != "current"
            or not coherence.get("searchable")):
        raise RuntimeError(
            "candidate preflight is not current/searchable: "
            + json.dumps({"raw": preflight_raw, "parsed": preflight},
                         sort_keys=True))

    trials = []
    _stop(args.python, repo=repo, env=env)
    for restart in range(1, args.restarts + 1):
        command = [
            args.python, str(repo / "cli.py"), "recall", QUERY,
            "--hits", "2", "--budget", "5000", "--json", "--no-auto",
        ]
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=args.callers) as executor:
            wave = list(executor.map(
                lambda _caller: _run(
                    command, repo=repo, env=env, timeout=60.0),
                range(args.callers)))
        stop = _stop(args.python, repo=repo, env=env)
        for caller, query_raw in enumerate(wave, 1):
            payload = _last_json(query_raw["stdout"])
            status = payload.get("semantic_status") or {}
            coverage = payload.get("semantic_coverage") or {}
            state = status.get("state", "missing")
            unavailable = state == "unavailable"
            fallback_recommended = bool(status.get("fallback_recommended"))
            searchable = bool(
                not unavailable
                and (coverage.get("indexed", 0) > 0
                     or state in {
                         "ready", "no-match", "no-confident-match"}))
            semantic_served = bool(searchable and not fallback_recommended)
            keyword_only = bool(not semantic_served)
            meaning_unavailable_notice = (
                "meaning unavailable; keyword-only" in (
                    query_raw["stdout"] + query_raw["stderr"]))
            trial = {
                "restart": restart,
                "caller": caller,
                "returncode": query_raw["returncode"],
                "latency_s": query_raw["latency_s"],
                "semantic_state": state,
                "semantic_status": status,
                "semantic_coverage": coverage,
                "searchable": searchable,
                "semantic_served": semantic_served,
                "keyword_only": keyword_only,
                "meaning_unavailable_notice": meaning_unavailable_notice,
                "partial_searchable": bool(
                    searchable and not coverage.get("complete", False)),
                "unavailable": unavailable,
                "hard_unavailable_with_preexisting_last_good": bool(
                    coherence.get("searchable") and unavailable),
                "stderr": query_raw["stderr"][-2000:],
                "stop": stop,
            }
            trials.append(trial)
            if not args.quiet:
                print(json.dumps(
                    {"kind": "cold-restart", **trial}, sort_keys=True),
                    flush=True)

    latencies = [trial["latency_s"] for trial in trials]
    summary = {
        "kind": "cold-restart-summary",
        "schema": 2,
        "restarts": args.restarts,
        "callers": args.callers,
        "queries": len(trials),
        "preflight": preflight,
        "searchable": sum(trial["searchable"] for trial in trials),
        "semantic_served": sum(
            trial["semantic_served"] for trial in trials),
        "keyword_only": sum(trial["keyword_only"] for trial in trials),
        "meaning_unavailable_notice": sum(
            trial["meaning_unavailable_notice"] for trial in trials),
        "partial_searchable": sum(
            trial["partial_searchable"] for trial in trials),
        "unavailable": sum(trial["unavailable"] for trial in trials),
        "hard_unavailable_with_preexisting_last_good": sum(
            trial["hard_unavailable_with_preexisting_last_good"]
            for trial in trials),
        "nonzero_exit": sum(trial["returncode"] != 0 for trial in trials),
        "query_latency_s": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "max": max(latencies),
        },
    }
    result = {"summary": summary, "trials": trials}
    if args.output:
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if (
        summary["hard_unavailable_with_preexisting_last_good"] == 0
        and summary["nonzero_exit"] == 0
        and summary["searchable"] == args.restarts * args.callers
        and summary["semantic_served"] == args.restarts * args.callers
        and summary["keyword_only"] == 0
        and summary["meaning_unavailable_notice"] == 0
    ) else 3


if __name__ == "__main__":
    raise SystemExit(main())
