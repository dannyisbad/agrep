#!/usr/bin/env python3
"""Portable end-to-end latency benchmark and regression gate.

Every number exercises the real subprocess pipeline, not a mocked parser or matcher.
Keyword/CLI metrics use an isolated generated 100k-row corpus; ingest and streaming
canaries use a second production-shaped synthetic store. Only
the separately labelled live-freshen and opt-in semantic diagnostics may touch the
installed corpus. `--check` compares portable metrics against the budgets committed
below and exits 1 on any breach - run it in CI (with AGREP_PERF_SLACK to absorb
slower runners) so a latency regression fails a PR instead of shipping.

  python bench/perf.py            # measure, human table
  python bench/perf.py --json     # machine output
  python bench/perf.py --check    # gate: exit 1 over budget
  python bench/perf.py --check-semantic  # portable gate + require live semantic lane
  python bench/perf.py --check-live  # also enforce the local live-freshen SLA
  AGREP_PERF_SLACK=3 python bench/perf.py --check   # CI: 3x budget slack

The query ladder spans the engine's behavior space: a guaranteed miss (floor),
a selective phrase (the common case), a stopword (the exhaustive-confirm worst
case), and a two-character LIKE lane with exact row and chat heads.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import shlex
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE.parent / "py"


def _runtime_error(
        argv: list[str], version_info: tuple[int, ...] | None = None) -> str | None:
    version = tuple(sys.version_info if version_info is None else version_info)
    if version >= (3, 10):
        return None
    project_python = HERE.parent / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(project_python), str(Path(__file__).resolve()), *argv]
    rerun = (subprocess.list2cmdline(command) if os.name == "nt"
             else shlex.join(command))
    actual = ".".join(str(part) for part in version[:3])
    return (f"bench/perf.py requires Python 3.10+; running {actual}.\n"
            f"rerun with the project runtime: {rerun}")


sys.path.insert(0, str(PY))
from hookless.registry import AGENT_CONTEXT_ENV_KEYS  # noqa: E402

_PRIVATE_DISCOVERY_ENV_KEYS = (
    "USERPROFILE", "HOME", "APPDATA", "CLINE_DIR", "XDG_CONFIG_HOME")


def _private_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (*_PRIVATE_DISCOVERY_ENV_KEYS, *AGENT_CONTEXT_ENV_KEYS):
        env.pop(name, None)
    return env

# local budgets in ms; CI multiplies by AGREP_PERF_SLACK. wall budgets include
# interpreter + import cost (tracked separately below); engine budgets are the
# AGREP_DEBUG "search done" stamp.
BUDGETS = {
    "import_ms": 150,            # python -c "import search" (interp + tree)
    "engine_miss_ms": 80,        # FTS miss floor
    "engine_selective_ms": 120,  # the common case (plan budget)
    "engine_stopword_ms": 900,   # 100k-hit exact totals + bounded ranked head
    "engine_short_ms": 350,      # 100k-hit two-character LIKE lane, bounded head
    "engine_short_chats_ms": 350,  # same lane after exact session-head collapse
    "engine_short_interior_ms": 350,  # 100k-hit interior-only two-character head
    "engine_short_interior_chats_ms": 350,  # same adversary after chat collapse
    "wall_selective_ms": 500,    # full subprocess incl. interpreter
    "cli_warm_ms": 300,          # current-checkout CLI end-to-end (plan budget)
    "probe_ms": 300,             # current-checkout unverified recall --probe miss
    "semantic_ms": 800,          # -s resident end-to-end (opt-in local gate)
    "ingest_cold_ms": 3500,      # synthetic 4,600-file/14k-row full publication
    "ingest_warm_ms": 250,       # unchanged snapshot: no cache load/materialization
    # one changed transcript: unlike ingest_warm_ms, covers cache load/materialization + delta publication
    "ingest_one_changed_ms": 500,
    "streamed_first_hit_ms": 150,  # no-index Python CLI -> first flushed matching row
    # Agents wait for process exit, and this command includes the cold ingest.
    # Keep the full-exit ceiling at least as high as the cold-ingest ceiling.
    "streamed_completion_ms": 3750,
}
# These are available in every source checkout once the Rust binary has been built.
# A missing value is a broken measurement, never a green performance result.
REQUIRED_METRICS = {
    "import_ms", "engine_miss_ms", "engine_selective_ms",
    "engine_stopword_ms", "engine_short_ms", "engine_short_chats_ms",
    "engine_short_interior_ms", "engine_short_interior_chats_ms",
    "wall_selective_ms", "ingest_warm_ms",
    "cli_warm_ms", "probe_ms", "ingest_cold_ms",
    "ingest_one_changed_ms", "streamed_first_hit_ms",
    "streamed_completion_ms",
}

ENGINE_MISS_Q = "zxqvnonexistentperffloorquery"
PROBE_MISS_Q = "zxqv nonexistent perf floor query"
SELECTIVE_Q = "granite embedding profile"
STOPWORD_Q = "the"
SHORT_Q = "hi"
SHORT_INTERIOR_Q = "oa"
_NO_AUTO_FRESHNESS_PROOF = (
    "history may be stale: automatic freshness checks are disabled by --no-auto")
_SEARCH_ROWS = 100_000
# Naming the corpus the miss was established against proves the whole fixture
# was searched, so the floor cannot be met by a degraded or truncated lane.
_ENGINE_MISS_STDERR_PROOF = (
    f"keyword: 0 matching rows across {_SEARCH_ROWS:,} indexed messages",
    _NO_AUTO_FRESHNESS_PROOF,
)
_DELTA_FILES = 4_600
_DELTA_ROWS = 14_000
_LIVE_UNCHANGED_MARKER = "unchanged since last index"
_LIVE_SKIP_MARKER = "skipped ingest + writes"
_LIVE_FRESHEN_DEFAULT_BUDGET_MS = 250.0
_LIVE_WARM_DEFAULT_BUDGET_MS = 250.0


def _median_wall(cmd: list[str], n: int, *, require_success: bool = False,
                 allowed_returncodes: set[int] | None = None,
                 required_stderr: str | None = None,
                 **kw) -> tuple[float | None, subprocess.CompletedProcess]:
    times, last, failed_result = [], None, None
    for _ in range(n):
        t0 = time.perf_counter()
        last = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, **kw)
        times.append((time.perf_counter() - t0) * 1000)
        invalid = ((allowed_returncodes is not None
                    and last.returncode not in allowed_returncodes)
                   or (require_success and last.returncode != 0)
                   or (required_stderr is not None and required_stderr not in last.stderr))
        if invalid and failed_result is None:
            failed_result = last
    # a fast failure is not a fast benchmark: require-success callers get None, not a green timing
    return (None if failed_result else statistics.median(times)), failed_result or last


_MAX_PERF_SLACK = 10.0
_METRIC_SLACK_ENVS = {
    "ingest_cold_ms": "AGREP_PERF_INGEST_COLD_SLACK",
    "ingest_one_changed_ms": "AGREP_PERF_INGEST_ONE_CHANGED_SLACK",
    "streamed_first_hit_ms": "AGREP_PERF_STREAMED_FIRST_HIT_SLACK",
    "streamed_completion_ms": "AGREP_PERF_STREAMED_COMPLETION_SLACK",
}


def _slack_value(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not 0.0 < value <= _MAX_PERF_SLACK:
        raise ValueError(f"{name} must be finite and in (0, {_MAX_PERF_SLACK:g}]")
    return value


def _metric_slack(metric: str, default: float) -> float:
    name = _METRIC_SLACK_ENVS.get(metric)
    return default if name is None else _slack_value(name, default)


def _effective_limits(default_slack: float) -> dict[str, float]:
    limits = {metric: budget * _metric_slack(metric, default_slack)
              for metric, budget in BUDGETS.items()}
    if limits["streamed_completion_ms"] < limits["ingest_cold_ms"]:
        raise ValueError(
            "streamed completion limit must cover the cold ingest limit")
    return limits


_SEMANTIC_TIMING_LINE = re.compile(
    r"^\* \[agrep \+\s*[\d.]+ms\] semantic timing (?P<payload>\{.*\})$")
_SEMANTIC_TIMING_TOTALS = (
    "hybrid_compute_ms", "semantic_dispatch_ms", "worker_search_ms",
    "client_roundtrip_ms", "client_overhead_ms",
)


def _valid_ms(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and float(value) >= 0.0)


def _valid_semantic_timing(parsed: object) -> bool:
    if not isinstance(parsed, dict):
        return False
    phases = parsed.get("phases_ms")
    if (not isinstance(phases, dict) or not phases
            or any(not isinstance(name, str) or not name or not _valid_ms(value)
                   for name, value in phases.items())):
        return False
    if any(not _valid_ms(parsed.get(name)) for name in _SEMANTIC_TIMING_TOTALS):
        return False
    if not isinstance(parsed.get("coherence_state"), str):
        return False
    for name in ("cache_before", "cache_after"):
        state = parsed.get(name)
        if (not isinstance(state, dict)
                or any(not isinstance(key, str) or not isinstance(value, bool)
                       for key, value in state.items())):
            return False
    return True


def _extract_semantic_timing(stderr: str) -> dict | None:
    found = None
    for line in stderr.splitlines():
        match = _SEMANTIC_TIMING_LINE.fullmatch(line)
        if match is None:
            continue
        try:
            parsed = json.loads(match.group("payload"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _valid_semantic_timing(parsed):
            found = parsed
    return found


def _verified_semantic_median(
        cmd: list[str], *, env: dict[str, str], cwd: Path, n: int = 3,
        repair_window_s: float = 8.0,
) -> tuple[float | None, subprocess.CompletedProcess, dict[str, object]]:
    """Median of ready semantic completions, with bounded off-path repair admission.

    A transcript can advance just before this opt-in ambient diagnostic. Strict semantic then
    returns immediately and starts the delta/refs repair by design; counting that fast refusal
    as query latency is wrong, while failing forever on the first handoff makes the gate race the
    freshness daemon. Admit up to ``repair_window_s`` for those explicitly labelled preparation
    outcomes, then require ``n`` real ``via semantic:`` completions. Every excluded attempt stays
    in diagnostics so availability is visible rather than silently warmed away.
    """
    samples: list[float] = []
    attempts: list[dict[str, object]] = []
    deadline = time.monotonic() + repair_window_s
    last: subprocess.CompletedProcess | None = None
    while len(samples) < n and len(attempts) < 40:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.perf_counter()
        try:
            last = subprocess.run(
                cmd, capture_output=True, text=True,
                # >3s is far past the 800ms gate; bound by the shared deadline, not the generic 10-min timeout
                timeout=max(0.05, min(3.0, remaining)),
                encoding="utf-8", errors="replace", env=env, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            # Preserve the CompletedProcess return contract for the caller's diagnostics.
            last = subprocess.CompletedProcess(cmd, 2, "", f"{type(exc).__name__}: {exc}")
        elapsed = (time.perf_counter() - started) * 1000
        lane_ready = last.returncode in (0, 1) and "via semantic:" in last.stderr
        combined = f"{last.stderr}\n{last.stdout}"
        semantic_timing = _extract_semantic_timing(last.stderr)
        ready = lane_ready and semantic_timing is not None
        if lane_ready and semantic_timing is None:
            reason = "semantic timing proof was absent or invalid"
        else:
            reason = next(
                (line.strip() for line in combined.splitlines()
                 if "semantic unavailable:" in line or "embeddings stale" in line),
                "strict semantic proof marker was absent",
            )[-240:]
        repairable = any(token in reason for token in (
            "embeddings stale", "refresh running", "refs running",
            "candidate refs are preparing",
        ))
        outcome = "ready" if ready else ("preparing" if repairable else "failed")
        attempts.append({
            "outcome": outcome,
            "wall_ms": round(elapsed, 3),
            **({"semantic_timing": semantic_timing}
               if semantic_timing is not None else {}),
            **({} if ready else {"reason": reason}),
        })
        if ready:
            samples.append(elapsed)
            continue
        if not repairable or time.monotonic() >= deadline:
            break
        time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
    detail: dict[str, object] = {
        "requested_samples": n,
        "ready_samples_ms": [round(sample, 3) for sample in samples],
        "attempts": attempts,
        "preparation_fallbacks": sum(
            attempt["outcome"] == "preparing" for attempt in attempts),
        "failed_attempts": sum(
            attempt["outcome"] == "failed" for attempt in attempts),
        "phase_samples": [attempt["semantic_timing"] for attempt in attempts
                          if attempt.get("outcome") == "ready"
                          and "semantic_timing" in attempt],
    }
    if len(samples) == n:
        detail.update(status="measured", measured_ms=round(statistics.median(samples), 3))
        return statistics.median(samples), last, detail
    detail.update(status="unavailable", reason=(
        attempts[-1].get("reason", "semantic samples unavailable")
        if attempts else "semantic command did not run"))
    if last is None:
        last = subprocess.CompletedProcess(
            cmd, 2, "", "semantic repair admission window elapsed before launch")
    return None, last, detail


def _zero_change_proven(run: subprocess.CompletedProcess) -> bool:
    """The Rust fast path proves itself with both markers on stdout."""
    return (_LIVE_UNCHANGED_MARKER in run.stdout
            and _LIVE_SKIP_MARKER in run.stdout)


def _live_outcome(run: subprocess.CompletedProcess) -> str:
    """Classify an ambient index attempt without pretending changed work was warm."""
    output = f"{run.stdout}\n{run.stderr}"
    if run.returncode != 0:
        return f"error({run.returncode})"
    if _zero_change_proven(run):
        return "unchanged"
    if "source validation incomplete" in output:
        return "recovery"
    if "event publication incomplete" in output:
        return "event-repair"
    if "unchanged message set" in output:
        return "source-moved"
    if "indexed " in output:
        return "changed"
    return "non-warm"


def _verified_live_freshen(
        cmd: list[str], env: dict[str, str] | None, n: int = 3,
        max_attempts: int = 9,
) -> tuple[float | None, float | None, dict[str, object]]:
    """First ambient freshen and median of proven zero-change shortcut runs.

    The installed corpus is allowed to move, repair, or wait on another process. Those are
    useful diagnostics but are not samples of the warm fast path. The first stabilization
    attempt gets its own wall metric and outcome, but is excluded from the shortcut median.
    Subsequent attempts are accepted only when the Rust command prints its exact
    unchanged/skip proof.
    """
    detail: dict[str, object] = {
        "required_proof": f"{_LIVE_UNCHANGED_MARKER} ... {_LIVE_SKIP_MARKER}",
        "requested_samples": n,
        "max_attempts": max_attempts,
    }

    def run_once() -> tuple[subprocess.CompletedProcess, float]:
        started = time.perf_counter()
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, env=env)
        return result, (time.perf_counter() - started) * 1000

    try:
        first, first_ms = run_once()
        first_outcome = _live_outcome(first)
        detail["first_freshen"] = {
            "outcome": first_outcome,
            "wall_ms": round(first_ms, 3),
        }
        if first.returncode != 0:
            detail["status"] = "error"
            detail["reason"] = (
                f"first freshen exited {first.returncode}: "
                f"{(first.stderr + first.stdout).strip()[-240:]}"
            )
            return first_ms, None, detail

        samples: list[float] = []
        outcomes: list[dict[str, object]] = []
        for _ in range(max_attempts):
            run, wall_ms = run_once()
            outcome = _live_outcome(run)
            outcomes.append({"outcome": outcome, "wall_ms": round(wall_ms, 3)})
            if run.returncode != 0:
                detail["status"] = "error"
                detail["attempts"] = outcomes
                detail["reason"] = (
                    f"ambient index exited {run.returncode}: "
                    f"{(run.stderr + run.stdout).strip()[-240:]}"
                )
                return first_ms, None, detail
            if outcome == "unchanged":
                samples.append(wall_ms)
                if len(samples) == n:
                    break
        detail["attempts"] = outcomes
        detail["verified_samples_ms"] = [round(sample, 3) for sample in samples]
        if len(samples) != n:
            detail["status"] = "busy"
            counts = {name: sum(item["outcome"] == name for item in outcomes)
                      for name in sorted({str(item["outcome"]) for item in outcomes})}
            detail["reason"] = (
                f"live sources did not yield {n} verified unchanged samples "
                f"within {max_attempts} attempts ({counts})"
            )
            return first_ms, None, detail
        median = statistics.median(samples)
        detail["status"] = "measured"
        detail["zero_change_shortcut_ms"] = round(median, 3)
        detail["reason"] = (
            f"{n}/{n} timed runs emitted the unchanged/skip proof; "
            f"median of verified samples"
        )
        return first_ms, median, detail
    except (OSError, subprocess.SubprocessError) as exc:
        detail["status"] = "error"
        detail["reason"] = f"{type(exc).__name__}: {exc}"
        return None, None, detail


def _debug_proves(stderr: str, marker: str) -> bool:
    pattern = re.compile(
        rf"^\* \[agrep \+\s*[\d.]+ms\] {re.escape(marker)}", re.MULTILINE)
    return pattern.search(stderr) is not None


def _user_stderr_lines(stderr: str) -> list[str]:
    return [
        line for line in stderr.splitlines()
        if line and not line.startswith("* [agrep +")
    ]


def _search_json_payload(
        output: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    values: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("search JSON contained a non-object row")
        values.append(value)
    if not values or values[0].get("kind") != "agrep-meta":
        raise ValueError("search JSON lacked a leading metadata row")
    if any(value.get("kind") == "agrep-meta" for value in values[1:]):
        raise ValueError("search JSON contained multiple metadata rows")
    return values[1:], values[0]


def _json_hit_keys(output: str) -> list[tuple[str, int | None, str]] | None:
    rows = []
    try:
        hits, _meta = _search_json_payload(output)
        for obj in hits:
            if not isinstance(obj.get("session"), str):
                return None
            turn = obj.get("turn")
            if turn is not None and not isinstance(turn, int):
                return None
            who = obj.get("who")
            if not isinstance(who, str):
                return None
            rows.append((obj["session"], turn, who))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return rows


def _engine_ms(query: str, n: int = 3, *, env: dict[str, str] | None = None,
               allowed_returncodes: set[int] | None = None,
               require_hit: bool = False,
               display_args: list[str] | None = None,
               required_debug_marker: str | None = None,
               required_stderr: tuple[str, ...] | None = None,
               expected_json_keys: list[tuple[str, int | None, str]] | None = None,
               ) -> tuple[float | None, float | None]:
    """(engine_ms from the AGREP_DEBUG 'search done' stamp, subprocess wall_ms)."""
    env = {**(env or os.environ), "AGREP_DEBUG": "1"}
    display_args = display_args or ["-n", "1"]
    cmd = [sys.executable, "-c",
           f"import search; raise SystemExit(search.main("
           f"[{query!r}, *{display_args!r}, '--color', 'never', '--no-auto']))"]
    walls, engines, invalid = [], [], False
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run(cmd, cwd=PY, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        walls.append((time.perf_counter() - t0) * 1000)
        m = re.search(r"\[agrep \+\s*([\d.]+)ms\] search done", r.stderr)
        valid_rc = (r.returncode in (allowed_returncodes or {0, 1})
                    and (not require_hit or r.returncode == 0))
        valid_marker = (required_debug_marker is None
                        or _debug_proves(r.stderr, required_debug_marker))
        valid_stderr = (required_stderr is None
                        or _user_stderr_lines(r.stderr) == list(required_stderr))
        valid_output = (expected_json_keys is None
                        or _json_hit_keys(r.stdout) == expected_json_keys)
        if valid_rc and m and valid_marker and valid_stderr and valid_output:
            engines.append(float(m.group(1)))
        else:
            invalid = True
    if invalid or len(engines) != n:
        return None, None
    return statistics.median(engines), statistics.median(walls)


def _private_event_file_count(store: Path) -> int:
    connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        return int(connection.execute(
            "SELECT count(*) FROM event_sessions WHERE agent='claude'").fetchone()[0])
    finally:
        connection.close()


def _prepare_private_stream_data(data: Path) -> None:
    data.mkdir()
    # This fixture owns index completion, not detached semantic upkeep that can outlive indexd.
    (data / "settings.json").write_text(
        '{"embeddings":"off"}\n', encoding="utf-8")


_INGEST_PHASE = re.compile(
    r"(?:^|\s)phases:\s*(?P<body>source-check \d+ms(?:\s*·\s*[^\r\n]+)?)$",
    re.MULTILINE,
)
_INGEST_PHASE_VALUE = re.compile(r"(?P<name>[a-z0-9+_-]+) (?P<ms>\d+)ms")
_STREAM_TAIL_PHASE = re.compile(
    r"first-run tail: fts-delegate\+hooks (?P<ms>[\d.]+)ms")


def _ingest_sample_diagnostic(
        run: subprocess.CompletedProcess, wall_ms: float,
) -> dict[str, object]:
    combined = f"{run.stdout}\n{run.stderr}"
    phases_match = _INGEST_PHASE.search(combined)
    phases = ({
        match.group("name"): float(match.group("ms"))
        for match in _INGEST_PHASE_VALUE.finditer(phases_match.group("body"))
    } if phases_match else {})
    return {
        "wall_ms": round(wall_ms, 3),
        "phases_ms": phases,
    }


def _ingest_proof_line(
        label: str, sample: object, artifacts: object = None,
) -> str | None:
    if not isinstance(sample, dict) or not isinstance(sample.get("wall_ms"), (int, float)):
        return None
    parts = [f"full={float(sample['wall_ms']):.1f}ms"]
    if isinstance(sample.get("first_hit_ms"), (int, float)):
        parts.insert(0, f"first={float(sample['first_hit_ms']):.1f}ms")
    if isinstance(sample.get("fts_delegate_hooks_ms"), (int, float)):
        parts.append(f"fts-tail={float(sample['fts_delegate_hooks_ms']):.1f}ms")
    phases = sample.get("phases_ms", {})
    if isinstance(phases, dict):
        rendered = ",".join(
            f"{name}={float(value):.0f}"
            for name, value in phases.items()
            if isinstance(value, (int, float)))
        if rendered:
            parts.append(f"phases[{rendered}]")
    if isinstance(artifacts, dict):
        rendered = ",".join(
            f"{name}={int(artifacts[name])}"
            for name in (
                ".ingest_cache.bin", ".boundary_stats.bin",
                "boundary_stats.json", "messages.jsonl",
            )
            if isinstance(artifacts.get(name), (int, float)))
        if rendered:
            parts.append(f"bytes[{rendered}]")
    return f"{label}: " + " · ".join(parts)


def _private_ingest_metrics(
        binp: Path, n: int = 5, diagnostics: dict[str, object] | None = None,
) -> tuple[float | None, float | None, float | None, float | None,
           float | None, str | None]:
    """Time cold, unchanged, one-changed, first-hit, and completion in isolation.

    The synthetic store matches the benchmark shape (4,600 source
    files and 14,000 materialized rows). Filesystem setup is outside every timed region.
    The cold sample includes the full parse and publication; changed samples append one
    valid row and exercise normal delta publication; the no-index CLI sample measures the
    first flushed result independently of the ingest's eventual completion. The fixture is
    deterministic on macOS and Windows and cannot race or poison the user's real stores.
    """
    try:
        suffix = ".noindex" if sys.platform == "darwin" else ""
        with tempfile.TemporaryDirectory(
                prefix="agrep-ingest-delta-", suffix=suffix) as tmp:
            root = Path(tmp)
            if sys.platform == "darwin":
                (root / ".metadata_never_index").touch()
            home = root / "home"
            project = home / ".claude" / "projects" / "perf-project"
            data = root / "data"
            project.mkdir(parents=True)
            data.mkdir()

            # production-shaped byte volume (~37MB messages + ~44MB replies at 14k rows) makes
            # publication and cache serialization real work; setup stays untimed
            payload = "incremental ingest benchmark payload " + ("x" * 2_200)
            reply_payload = "incremental ingest benchmark reply " + ("y" * 2_600)
            made = 0
            active = None
            for file_index in range(_DELTA_FILES):
                session = f"perf-session-{file_index:08d}"
                path = project / f"{session}.jsonl"
                if active is None:
                    active = path
                rows_here = 4 if file_index < (_DELTA_ROWS - 3 * _DELTA_FILES) else 3
                records = []
                for turn in range(rows_here):
                    records.append(json.dumps({
                        "type": "user", "userType": "external",
                        "sessionId": session,
                        "timestamp": "2026-01-02T10:00:00.000Z",
                        "cwd": "/work/perf-project",
                        "message": {"role": "user", "content": f"{payload} {turn}"},
                    }, separators=(",", ":")))
                    records.append(json.dumps({
                        "type": "assistant", "sessionId": session,
                        "timestamp": "2026-01-02T10:00:00.500Z",
                        "cwd": "/work/perf-project",
                        "message": {"role": "assistant", "model": "claude-perf",
                                    "content": [{"type": "text",
                                                 "text": f"{reply_payload} {turn}"}]},
                    }, separators=(",", ":")))
                # one tool event per session: adds no message row,
                # but charges per-session publication + inventory proof
                call_id = f"toolu_perf_{file_index:08d}"
                records.append(json.dumps({
                    "type": "assistant", "sessionId": session,
                    "timestamp": "2026-01-02T10:00:58.000Z",
                    "cwd": "/work/perf-project",
                    "message": {"role": "assistant", "model": "claude-perf",
                                "content": [{"type": "tool_use", "id": call_id,
                                             "name": "Read",
                                             "input": {"file_path": "/work/perf.txt"}}]},
                }, separators=(",", ":")))
                records.append(json.dumps({
                    "type": "user", "userType": "external", "sessionId": session,
                    "timestamp": "2026-01-02T10:00:59.000Z",
                    "cwd": "/work/perf-project",
                    "message": {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": call_id,
                        "content": "fixture tool result", "is_error": False,
                    }]},
                }, separators=(",", ":")))
                path.write_text("\n".join(records) + "\n", encoding="utf-8")
                made += rows_here
            if made != _DELTA_ROWS or active is None:
                return (None, None, None, None, None,
                        f"synthetic setup made {made} rows, expected {_DELTA_ROWS}")

            # the emitter parses newest sources first; touch keeps the first-hit session at the head
            active.touch()

            env = _private_environment()
            # AGREP_HOME is the test seam shared by every adapter. Remove all lower-
            # priority discovery roots so a typo can never fall through to a real store.
            env["AGREP_HOME"] = str(home)
            env["AGREP_DATA_DIR"] = str(data)
            # Empty adapters retain default discovery and proof overhead.
            cmd = [str(binp), "index"]

            # one cold sample only: deterministic canary; repeating it would dominate the local gate
            cold_started = time.perf_counter()
            seeded = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, env=env)
            cold_ms = (time.perf_counter() - cold_started) * 1000
            if seeded.returncode != 0:
                return (None, None, None, None, None,
                        f"synthetic cold seed exited {seeded.returncode}: {seeded.stderr[-300:]}")
            if diagnostics is not None:
                diagnostics["cold_sample"] = _ingest_sample_diagnostic(
                    seeded, cold_ms)

            def published_count() -> int | None:
                try:
                    return int((data / ".ingest.sig").read_text(
                        encoding="utf-8").split(":", 1)[0])
                except (OSError, ValueError, IndexError):
                    return None

            if published_count() != _DELTA_ROWS:
                return (None, None, None, None, None,
                        "synthetic cold seed did not publish the expected row count")
            try:
                store = data / "events" / ".store.sqlite3"
                event_files = _private_event_file_count(store)
                (data / "events" / ".generation").stat()
            except (OSError, sqlite3.Error, TypeError) as exc:
                return (None, None, None, None, None,
                        f"synthetic cold seed event store invalid: {exc}")
            if event_files != _DELTA_FILES:
                return (None, None, None, None, None,
                        f"synthetic cold seed published {event_files} event streams, "
                        f"expected {_DELTA_FILES}")
            try:
                cache_before = (data / ".ingest_cache.bin").stat()
                messages_before = (data / "messages.jsonl").stat()
                if diagnostics is not None:
                    diagnostics["cold_artifact_bytes"] = {
                        name: (data / name).stat().st_size
                        for name in (
                            ".ingest_cache.bin", ".boundary_stats.bin",
                            "boundary_stats.json", "messages.jsonl",
                        )
                    }
            except OSError as exc:
                return (None, None, None, None, None,
                        f"synthetic cold seed missed a required artifact: {exc}")
            warm_samples = []
            for _ in range(3):
                warm_started = time.perf_counter()
                unchanged = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=600, env=env)
                warm_samples.append((time.perf_counter() - warm_started) * 1000)
                cache_after = (data / ".ingest_cache.bin").stat()
                messages_after = (data / "messages.jsonl").stat()
                if unchanged.returncode != 0 or not _zero_change_proven(unchanged):
                    evidence = (unchanged.stdout + unchanged.stderr).strip()[-240:]
                    return (None, None, None, None, None,
                            "synthetic warm run did not emit the exact unchanged/skip "
                            f"stdout proof (rc={unchanged.returncode}): {evidence}")
                if ((cache_before.st_size, cache_before.st_mtime_ns)
                        != (cache_after.st_size, cache_after.st_mtime_ns)
                        or (messages_before.st_size, messages_before.st_mtime_ns)
                        != (messages_after.st_size, messages_after.st_mtime_ns)):
                    return (None, None, None, None, None,
                            "synthetic warm run rewrote cache or message artifacts")
            warm_ms = statistics.median(warm_samples)

            samples = []
            sample_diagnostics = []
            for sample in range(n):
                delta = json.dumps({
                    "type": "user", "userType": "external",
                    "sessionId": "perf-session-00000000",
                    "timestamp": f"2026-01-02T10:00:{sample + 1:02d}.000Z",
                    "cwd": "/work/perf-project",
                    "message": {"role": "user",
                                "content": f"changed transcript row {sample} {payload}"},
                }, separators=(",", ":"))
                with active.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(delta + "\n")
                    stream.write(json.dumps({
                        "type": "assistant", "sessionId": "perf-session-00000000",
                        "timestamp": f"2026-01-02T10:00:{sample + 1:02d}.500Z",
                        "cwd": "/work/perf-project",
                        "message": {"role": "assistant", "model": "claude-perf",
                                    "content": [{"type": "text",
                                                 "text": f"changed reply {reply_payload}"}]},
                    }, separators=(",", ":")) + "\n")
                started = time.perf_counter()
                run = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=600, env=env)
                elapsed_ms = (time.perf_counter() - started) * 1000
                samples.append(elapsed_ms)
                sample_diagnostics.append(_ingest_sample_diagnostic(run, elapsed_ms))
                if run.returncode != 0:
                    return (None, None, None, None, None,
                            f"synthetic changed run exited {run.returncode}: "
                            f"{(run.stderr + run.stdout)[-300:]}")
                expected = _DELTA_ROWS + sample + 1
                if published_count() != expected:
                    return (None, None, None, None, None,
                            f"synthetic changed run published {published_count()} rows, "
                            f"expected {expected}")
            if diagnostics is not None:
                diagnostics["changed_samples"] = sample_diagnostics
                diagnostics["changed_median_ms"] = round(statistics.median(samples), 3)
                diagnostics["changed_max_ms"] = round(max(samples), 3)
                diagnostics["changed_artifact_bytes"] = {
                    name: (data / name).stat().st_size
                    for name in (
                        ".ingest_cache.bin", ".boundary_stats.bin",
                        "boundary_stats.json", "messages.jsonl",
                    )
                }

            # an empty data dir forces the first-run streaming lane; sources reused, cache/index artifacts not
            stream_data = root / "stream-data"
            _prepare_private_stream_data(stream_data)
            stream_env = {**env, "AGREP_DATA_DIR": str(stream_data),
                          "PYTHONUNBUFFERED": "1", "AGREP_INDEXD_IDLE_S": "0",
                          "AGREP_PERF_PHASES": "1"}
            stream_query = "incremental ingest benchmark payload"
            stream_cmd = [sys.executable, str(HERE.parent / "cli.py"), stream_query,
                          "-n", "1", "--classic", "--color", "never"]
            started = time.perf_counter()
            proc = subprocess.Popen(
                stream_cmd, cwd=HERE.parent, env=stream_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if os.name == "nt" else {}))
            first: queue.Queue[tuple[float, str]] = queue.Queue(maxsize=1)

            def read_first() -> None:
                line = proc.stdout.readline() if proc.stdout is not None else ""
                first.put((time.perf_counter(), line))

            reader = threading.Thread(target=read_first, daemon=True)
            reader.start()
            try:
                first_at, first_line = first.get(timeout=600)
                # Windows communicate() must not install a second reader on the same pipe.
                reader.join()
                _stdout, stream_stderr = proc.communicate(timeout=600)
                completed_at = time.perf_counter()
            except (queue.Empty, subprocess.TimeoutExpired):
                proc.kill()
                _stdout, stream_stderr = proc.communicate()
                return (None, None, None, None, None,
                        "synthetic streamed command timed out")
            streamed_ms = (first_at - started) * 1000
            streamed_completion_ms = (completed_at - started) * 1000
            if diagnostics is not None:
                stream_run = subprocess.CompletedProcess(
                    stream_cmd, proc.returncode, first_line + _stdout, stream_stderr)
                stream_sample = _ingest_sample_diagnostic(
                    stream_run, streamed_completion_ms)
                stream_sample["first_hit_ms"] = round(streamed_ms, 3)
                tail = _STREAM_TAIL_PHASE.search(stream_stderr)
                stream_sample["fts_delegate_hooks_ms"] = (
                    float(tail.group("ms")) if tail else None)
                diagnostics["stream_sample"] = stream_sample
            lock_seen = False
            lock_start_deadline = time.monotonic() + 2
            lock_exit_deadline = time.monotonic() + 60
            while time.monotonic() < lock_exit_deadline:
                lock_seen = lock_seen or any(stream_data.glob(".indexd.v*.lock"))
                if lock_seen and not any(stream_data.glob(".indexd.v*.lock")):
                    break
                if not lock_seen and time.monotonic() >= lock_start_deadline:
                    break
                time.sleep(0.05)
            if any(stream_data.glob(".indexd.v*.lock")):
                return (None, None, None, None, None,
                        "synthetic streamed indexer did not release its temporary data dir")
            if proc.returncode != 0 or stream_query not in first_line.lower():
                return (None, None, None, None, None,
                        f"synthetic streamed command exited {proc.returncode} without a hit: "
                        f"{(stream_stderr + first_line)[-300:]}")
            return (cold_ms, warm_ms, statistics.median(samples), streamed_ms,
                    streamed_completion_ms, None)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, None, None, None, f"{type(exc).__name__}: {exc}"


def _build_search_fixture(
        root: Path, rows: int, *, short: bool,
) -> tuple[Path, dict[str, str], str | None]:
    data, home = root / "data", root / "home"
    data.mkdir(parents=True)
    home.mkdir(parents=True)
    messages = data / "messages.jsonl"
    fixture_now = int(time.time() * 1000)
    with messages.open("w", encoding="utf-8", newline="\n") as stream:
        for ordinal in range(rows):
            session_no = ordinal % _DELTA_FILES
            turn = ordinal // _DELTA_FILES
            if short:
                text = f"this broad short performance row {ordinal}"
                if ordinal < 100:
                    text = f"hi {text}"
                timestamp = fixture_now - ordinal * 60_000
            else:
                text = (f"the broad performance row {ordinal}"
                        if ordinal else f"the unique {SELECTIVE_Q} sentinel")
                timestamp = ordinal
            stream.write(json.dumps({
                "id": f"perf:session-{session_no:05d}:{turn}",
                "session": f"session-{session_no:05d}",
                "agent": "perf", "project": "/work/perf",
                "model": "perf", "model_source": "explicit",
                "turn": turn, "ts": timestamp, "who": "user", "text": text,
            }, separators=(",", ":")) + "\n")
    (data / "replies.jsonl").write_text("", encoding="utf-8")
    generation = f"{rows}:private-search-{'short' if short else 'baseline'}"
    (data / ".ingest.sig").write_text(generation + "\n", encoding="utf-8")
    # Direct fixtures still carry the generation-bound family publication that
    # Rust ingest would write. Omitting it turns each search into three failed
    # torn-publication probes and measures retry sleeps instead of lookup work.
    import session_context
    family_pairs = [
        (f"session-{ordinal:05d}", "")
        for ordinal in range(min(rows, _DELTA_FILES))
    ]
    (data / "sessions.jsonl").write_text(
        "".join(
            json.dumps(
                {"session": session, "parent": parent},
                separators=(",", ":"),
            ) + "\n"
            for session, parent in family_pairs
        ),
        encoding="utf-8",
    )
    (data / session_context.SESSION_FAMILY_META_FILE).write_text(
        json.dumps({
            "version": session_context.SESSION_FAMILY_INDEX_VERSION,
            "algorithm": session_context.SESSION_FAMILY_DIGEST_ALGORITHM,
            "ingest_signature": generation,
            "count": len(family_pairs),
            "digest": session_context.session_family_digest(family_pairs),
        }, separators=(",", ":")),
        encoding="utf-8",
    )
    if short:
        # This direct index fixture must carry the generation-pinned sidecar that ingest publishes.
        (data / "boundary_stats.json").write_text(json.dumps({
            "schema": 2,
            "generation": generation,
            "families": _DELTA_FILES,
            "tokens": {
                SHORT_Q: [_DELTA_FILES, 100, 2],
                SHORT_INTERIOR_Q: [_DELTA_FILES, 0, 0],
            },
        }, separators=(",", ":")) + "\n", encoding="utf-8")

    env = _private_environment()
    env.update({
        "AGREP_HOME": str(home),
        "AGREP_DATA_DIR": str(data),
        "AGREP_NO_DAEMON": "1",
    })
    # This synthetic harness bypasses Rust ingest by constructing messages.jsonl
    # directly. Bind that fixture to the exact Python+Rust writer identity before
    # asking corpusdb to publish its derived search database.
    built = subprocess.run(
        [sys.executable, "-c",
         "import json,os; from pathlib import Path; import indexd_runtime; "
         "owner=indexd_runtime.derived_writer_build_id(require_binary=True); "
         "Path(os.environ['AGREP_DATA_DIR'],'.derived-owner.json').write_text("
         "json.dumps({'version':1,'build_id':owner},separators=(',',':')),"
         "encoding='utf-8'); "
         "import corpusdb; db=corpusdb.connect(quiet=True); "
         "assert db is not None; db.close()"],
        cwd=PY, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    if built.returncode != 0:
        return data, env, (
            f"synthetic search index build exited {built.returncode}: "
            f"{(built.stderr + built.stdout)[-300:]}")
    return data, env, None


def _exhaustive_short_oracle(
        env: dict[str, str], query: str = SHORT_Q,
) -> tuple[dict[str, list[tuple[str, int | None, str]]] | None, str | None]:
    code = f"""
import json
import search
result = search.run_query({json.dumps(query)}, limit=0, exact_totals=True)
hits = result["hits"] if result else []
key = lambda hit: [hit.get("session"), hit.get("turn"), hit.get("who") or ""]
rows = [key(hit) for hit in hits[:40]]
seen = set()
chats = []
for hit in hits:
    session = hit.get("session")
    if not session or session in seen:
        continue
    seen.add(session)
    chats.append(key(hit))
    if len(chats) == 12:
        break
print(json.dumps({{"rows": rows, "chats": chats}}, separators=(",", ":")))
"""
    run = subprocess.run(
        [sys.executable, "-c", code], cwd=PY, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if run.returncode != 0:
        return None, (
            f"short exhaustive oracle exited {run.returncode}: "
            f"{(run.stderr + run.stdout)[-300:]}")
    try:
        raw = json.loads(run.stdout)
        oracle = {}
        for name in ("rows", "chats"):
            values = raw[name]
            if not isinstance(values, list):
                raise TypeError(f"{name} is not a list")
            normalized = []
            for value in values:
                if (not isinstance(value, list) or len(value) != 3
                        or not isinstance(value[0], str)
                        or (value[1] is not None
                            and (not isinstance(value[1], int)
                                 or isinstance(value[1], bool)))
                        or not isinstance(value[2], str)):
                    raise TypeError(f"{name} contains an invalid hit key")
                normalized.append((value[0], value[1], value[2]))
            oracle[name] = normalized
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"short exhaustive oracle was invalid: {exc}"
    if (len(oracle["rows"]) != 40 or len(oracle["chats"]) != 12
            or len({key[0] for key in oracle["chats"]}) != 12):
        return None, "short exhaustive oracle did not produce 40 rows and 12 chats"
    return oracle, None


def _private_search_metrics(
        cli_cmd: list[str], diagnostics: dict[str, object],
) -> tuple[dict[str, float | None], str | None]:
    """Measure checkout engine/CLI paths on deterministic synthetic corpora."""
    measured = {
        "engine_miss_ms": None,
        "engine_selective_ms": None,
        "engine_stopword_ms": None,
        "engine_short_ms": None,
        "engine_short_chats_ms": None,
        "engine_short_interior_ms": None,
        "engine_short_interior_chats_ms": None,
        "wall_selective_ms": None,
        "cli_warm_ms": None,
        "probe_ms": None,
    }
    rows = _SEARCH_ROWS
    try:
        import sqlite3
        suffix = ".noindex" if sys.platform == "darwin" else ""
        with tempfile.TemporaryDirectory(
                prefix="agrep-search-perf-", suffix=suffix) as tmp:
            root = Path(tmp)
            if sys.platform == "darwin":
                (root / ".metadata_never_index").touch()
            data, env, build_error = _build_search_fixture(
                root / "baseline", rows, short=False)
            if build_error is not None:
                return measured, build_error

            db = sqlite3.connect(data / "corpus.db")
            try:
                actual_rows = int(db.execute("SELECT count(*) FROM msgs").fetchone()[0])
                broad_hits = int(db.execute(
                    "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                    ('"the"',)).fetchone()[0])
                selective_hits = int(db.execute(
                    "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
                    ('"granite" AND "embedding" AND "profile"',)).fetchone()[0])
            finally:
                db.close()
            if (actual_rows, broad_hits, selective_hits) != (rows, rows, 1):
                return measured, (
                    "synthetic search proof mismatch: "
                    f"rows={actual_rows}, stopword={broad_hits}, "
                    f"selective={selective_hits}")

            short_data, short_env, build_error = _build_search_fixture(
                root / "short", rows, short=True)
            if build_error is not None:
                return measured, build_error
            db = sqlite3.connect(short_data / "corpus.db")
            try:
                short_rows = int(db.execute("SELECT count(*) FROM msgs").fetchone()[0])
                short_hits = int(db.execute(
                    "SELECT count(*) FROM msgs WHERE text LIKE ?",
                    (f"%{SHORT_Q}%",)).fetchone()[0])
                short_aligned = int(db.execute(
                    "SELECT count(*) FROM msgs WHERE text LIKE 'hi %'").fetchone()[0])
                short_interior = int(db.execute(
                    "SELECT count(*) FROM msgs WHERE text LIKE ?",
                    (f"%{SHORT_INTERIOR_Q}%",)).fetchone()[0])
                short_chats = int(db.execute(
                    "SELECT count(DISTINCT session) FROM msgs").fetchone()[0])
                short_boundary = {
                    str(token): (int(n), int(s), int(quality))
                    for token, n, s, quality in db.execute(
                        "SELECT token, n, s, q FROM boundary_stats "
                        "WHERE token IN (?, ?)", (SHORT_Q, SHORT_INTERIOR_Q))
                }
            finally:
                db.close()
            if ((short_rows, short_hits, short_aligned, short_interior, short_chats)
                    != (rows, rows, 100, rows, _DELTA_FILES)
                    or short_boundary != {
                        SHORT_Q: (_DELTA_FILES, 100, 2),
                        SHORT_INTERIOR_Q: (_DELTA_FILES, 0, 0),
                    }):
                return measured, (
                    "synthetic short-search proof mismatch: "
                    f"rows={short_rows}, hits={short_hits}, "
                    f"aligned={short_aligned}, interior={short_interior}, "
                    f"chats={short_chats}, boundary={short_boundary}")
            oracle, oracle_error = _exhaustive_short_oracle(short_env)
            if oracle_error is not None or oracle is None:
                return measured, oracle_error or "short exhaustive oracle unavailable"
            interior_oracle, oracle_error = _exhaustive_short_oracle(
                short_env, SHORT_INTERIOR_Q)
            if oracle_error is not None or interior_oracle is None:
                return measured, oracle_error or "interior short oracle unavailable"

            diagnostics["private_search"] = {
                "rows": actual_rows,
                "stopword_hits": broad_hits,
                "short_rows": short_rows,
                "short_hits": short_hits,
                "short_aligned_hits": short_aligned,
                "short_interior_hits": short_interior,
                "short_chats": short_chats,
                "selective_hits": selective_hits,
                "cli": cli_cmd,
            }

            measured["engine_miss_ms"], _ = _engine_ms(
                ENGINE_MISS_Q, env=env, allowed_returncodes={2},
                required_debug_marker=(
                    "search done: 0 hit(s) in 0 chat(s) via corpusdb; showing 0"),
                required_stderr=_ENGINE_MISS_STDERR_PROOF)
            (measured["engine_selective_ms"],
             measured["wall_selective_ms"]) = _engine_ms(
                 SELECTIVE_Q, env=env, allowed_returncodes={0}, require_hit=True)
            measured["engine_stopword_ms"], _ = _engine_ms(
                STOPWORD_Q, env=env, allowed_returncodes={0}, require_hit=True)
            measured["engine_short_ms"], _ = _engine_ms(
                SHORT_Q, env=short_env, allowed_returncodes={0}, require_hit=True,
                display_args=["--json", "-n", "40"],
                required_debug_marker="bounded short rows:",
                expected_json_keys=oracle["rows"])
            measured["engine_short_chats_ms"], _ = _engine_ms(
                SHORT_Q, env=short_env, allowed_returncodes={0}, require_hit=True,
                display_args=["-l", "--json", "-n", "12"],
                required_debug_marker="bounded short sessions:",
                expected_json_keys=oracle["chats"])
            measured["engine_short_interior_ms"], _ = _engine_ms(
                SHORT_INTERIOR_Q, env=short_env, allowed_returncodes={0}, require_hit=True,
                display_args=["--json", "-n", "40"],
                required_debug_marker="bounded short rows:",
                expected_json_keys=interior_oracle["rows"])
            measured["engine_short_interior_chats_ms"], _ = _engine_ms(
                SHORT_INTERIOR_Q, env=short_env, allowed_returncodes={0}, require_hit=True,
                display_args=["-l", "--json", "-n", "12"],
                required_debug_marker="bounded short sessions:",
                expected_json_keys=interior_oracle["chats"])
            measured["cli_warm_ms"], _ = _median_wall(
                [*cli_cmd, SELECTIVE_Q, "-n", "2", "--color", "never",
                 "--no-auto"],
                10, cwd=HERE.parent, env=env, require_success=True)
            measured["probe_ms"], _ = _median_wall(
                [*cli_cmd, "recall", PROBE_MISS_Q, "--probe", "--no-auto", "--lexical"],
                5, cwd=HERE.parent, env=env, allowed_returncodes={2},
                required_stderr=_NO_AUTO_FRESHNESS_PROOF)
            missing = [key for key, value in measured.items() if value is None]
            if missing:
                return measured, (
                    "synthetic search subprocess proof missing for " + ", ".join(missing))
            return measured, None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return measured, f"{type(exc).__name__}: {exc}"


def measure(
        cli_cmd: list[str],
        errors: list[str] | None = None,
        diagnostics: dict[str, object] | None = None,
        *, measure_live: bool = True, measure_semantic: bool = True,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    errors = errors if errors is not None else []
    diagnostics = diagnostics if diagnostics is not None else {}

    # Measure ingest before any CLI query wakes the index daemon and adds contention.
    binp = None
    ingest_env = None
    try:
        sys.path.insert(0, str(PY))
        import common  # noqa: E402
        binp = common.ingest_bin()
        # a direct checkout run bypasses the installed shim that normally exports the data dir
        ingest_env = {**os.environ, "AGREP_DATA_DIR": str(common.DATA_DIR)}
    except Exception:
        pass
    if binp and Path(str(binp)).exists():
        ingest_diagnostics: dict[str, object] = {}
        (out["ingest_cold_ms"], out["ingest_warm_ms"],
         out["ingest_one_changed_ms"], out["streamed_first_hit_ms"],
         out["streamed_completion_ms"],
         fixture_error) = _private_ingest_metrics(
             Path(str(binp)), diagnostics=ingest_diagnostics)
        diagnostics["ingest_fixture"] = ingest_diagnostics
        if fixture_error is not None:
            errors.append(f"ingest_fixture: {fixture_error}")
        if measure_live:
            live_first_ms, live_warm_ms, live_detail = _verified_live_freshen(
                [str(binp), "index"], ingest_env)
            out["live_first_freshen_wall_ms"] = live_first_ms
            out["live_zero_change_shortcut_ms"] = live_warm_ms
            diagnostics["live_freshen"] = live_detail
        else:
            out["live_first_freshen_wall_ms"] = None
            out["live_zero_change_shortcut_ms"] = None
            diagnostics["live_freshen"] = {
                "status": "not-requested",
                "reason": "portable --check never touches the ambient corpus",
            }
    else:
        out["live_first_freshen_wall_ms"] = None
        out["live_zero_change_shortcut_ms"] = None
        out["ingest_warm_ms"] = None
        out["ingest_cold_ms"] = None
        out["ingest_one_changed_ms"] = None
        out["streamed_first_hit_ms"] = None
        out["streamed_completion_ms"] = None
        diagnostics["live_freshen"] = {
            "status": "unavailable", "reason": "Rust ingest binary not found"}

    out["import_ms"], _ = _median_wall(
        [sys.executable, "-c", "import search"], 5, cwd=PY, require_success=True)

    search_measured, search_error = _private_search_metrics(cli_cmd, diagnostics)
    out.update(search_measured)
    if search_error is not None:
        errors.append(f"search_fixture: {search_error}")

    if measure_semantic:
        sem_env = {**os.environ, "AGREP_DEBUG": "1"}
        sem, _sem_run, sem_detail = _verified_semantic_median(
            [*cli_cmd, "-s", "how did we pin the model revision", "-n", "3",
             "--color", "never", "--strict-semantic"], env=sem_env,
            cwd=HERE.parent)
        # strict mode + the debug marker prove each timed run reached the semantic lane,
        # not the keyword fallback
        out["semantic_ms"] = sem
        diagnostics["semantic"] = sem_detail
    else:
        out["semantic_ms"] = None
        diagnostics["semantic"] = {
            "status": "not-requested",
            "reason": "portable --check skips the ambient semantic lane; use --check-semantic",
        }

    return out


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if runtime_error := _runtime_error(raw_argv):
        print(runtime_error, file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="agrep latency harness + budget gate")
    ap.add_argument(
        "--check", action="store_true",
        help="exit 1 if any portable budget is breached (live diagnostics excluded)",
    )
    ap.add_argument(
        "--check-live", action="store_true",
        help=("also enforce AGREP_LIVE_FRESHEN_BUDGET_MS on the first live freshen "
              "and AGREP_LIVE_WARM_BUDGET_MS on the verified shortcut (defaults: 250ms)"),
    )
    ap.add_argument(
        "--check-semantic", action="store_true",
        help=("run the portable gate and require the current local semantic lane "
              "to produce three ready samples within semantic_ms; bounded off-path "
              "repair attempts remain visible in diagnostics"),
    )
    ap.add_argument("--json", action="store_true", help="machine output")
    args = ap.parse_args(raw_argv)

    try:
        slack = _slack_value("AGREP_PERF_SLACK", 1.0)
        limits = _effective_limits(slack)
    except ValueError as exc:
        ap.error(str(exc))
    cli_cmd = [sys.executable, str(HERE.parent / "cli.py")]
    measurement_errors: list[str] = []
    diagnostics: dict[str, object] = {}
    portable_check = args.check or args.check_live or args.check_semantic
    got = measure(
        cli_cmd, measurement_errors, diagnostics,
        measure_live=(not portable_check or args.check_live),
        measure_semantic=(not portable_check or args.check_semantic),
    )

    breaches = list(measurement_errors)
    error_metrics = {error.split(":", 1)[0] for error in measurement_errors}
    rows = []
    for key, budget in BUDGETS.items():
        val = got.get(key)
        limit = limits[key]
        if val is None:
            required = (key in REQUIRED_METRICS
                        or (key == "semantic_ms" and args.check_semantic))
            if required and key not in error_metrics:
                breaches.append(f"{key}: required measurement unavailable")
            rows.append((key, None, limit,
                         "ERROR" if required or key in error_metrics else "skip"))
            continue
        ok = val <= limit
        if not ok:
            breaches.append(f"{key}: {val:.0f}ms > {limit:.0f}ms")
        rows.append((key, val, limit, "ok" if ok else "OVER"))

    live = diagnostics.get("live_freshen", {})
    live = live if isinstance(live, dict) else {}
    first_value = got.get("live_first_freshen_wall_ms")
    warm_value = got.get("live_zero_change_shortcut_ms")
    first = live.get("first_freshen", {})
    first = first if isinstance(first, dict) else {}
    first_outcome = str(first.get("outcome", "unavailable"))

    def live_budget(name: str, default: float) -> tuple[float, str | None]:
        try:
            return float(os.environ.get(name, str(default))) * slack, None
        except ValueError:
            return default * slack, f"{name}: expected a number"

    first_limit, first_budget_error = live_budget(
        "AGREP_LIVE_FRESHEN_BUDGET_MS", _LIVE_FRESHEN_DEFAULT_BUDGET_MS)
    warm_limit, warm_budget_error = live_budget(
        "AGREP_LIVE_WARM_BUDGET_MS", _LIVE_WARM_DEFAULT_BUDGET_MS)
    live_budget_errors = [error for error in (first_budget_error, warm_budget_error) if error]
    diagnostics["live_budgets_ms"] = {
        "first_freshen": first_limit,
        "zero_change_shortcut": warm_limit,
    }

    first_valid = first_value is not None and not first_outcome.startswith("error(")
    if not first_valid:
        first_status = "ERROR" if first_outcome.startswith("error(") else "UNAVAILABLE"
    else:
        first_status = "within" if first_value <= first_limit else "OVER-local"
    if warm_value is None:
        warm_status = str(live.get("status", "unavailable")).upper()
    else:
        warm_status = "within" if warm_value <= warm_limit else "OVER-local"

    if args.check_live:
        breaches.extend(live_budget_errors)
        if not first_valid:
            breaches.append(
                f"live_first_freshen_wall_ms: measurement unavailable or failed "
                f"(outcome={first_outcome})")
        elif first_value > first_limit:
            breaches.append(
                f"live_first_freshen_wall_ms: {first_value:.0f}ms > "
                f"{first_limit:.0f}ms local SLA (outcome={first_outcome})")
        if warm_value is None:
            breaches.append(
                f"live_zero_change_shortcut_ms: verified unchanged measurement unavailable "
                f"({live.get('reason', warm_status.lower())})")
        elif warm_value > warm_limit:
            breaches.append(
                f"live_zero_change_shortcut_ms: {warm_value:.0f}ms > "
                f"{warm_limit:.0f}ms local SLA")

    if args.json:
        print(json.dumps({"measured": got, "slack": slack, "limits_ms": limits,
                          "breaches": breaches,
                          "errors": measurement_errors, "diagnostics": diagnostics,
                          "live_budget_errors": live_budget_errors},
                         ensure_ascii=False))
    else:
        print(f"\n{'metric':<22} {'measured':>10} {'budget':>10}  status")
        print("-" * 52)
        for key, val, limit, status in rows:
            shown = f"{val:.0f}ms" if val is not None else "-"
            print(f"{key:<22} {shown:>10} {f'{limit:.0f}ms':>10}  {status}")
        ingest_detail = diagnostics.get("ingest_fixture", {})
        ingest_detail = ingest_detail if isinstance(ingest_detail, dict) else {}
        cold_proof = _ingest_proof_line(
            "ingest cold proof", ingest_detail.get("cold_sample"),
            ingest_detail.get("cold_artifact_bytes"))
        stream_proof = _ingest_proof_line(
            "stream proof", ingest_detail.get("stream_sample"))
        for proof in (cold_proof, stream_proof):
            if proof is not None:
                print(f"\n{proof}")
        changed_samples = ingest_detail.get("changed_samples", [])
        if isinstance(changed_samples, list) and changed_samples:
            walls = [
                float(sample["wall_ms"])
                for sample in changed_samples
                if isinstance(sample, dict)
                and isinstance(sample.get("wall_ms"), (int, float))
            ]
            print(
                "\ningest delta proof: "
                f"p50={float(ingest_detail['changed_median_ms']):.1f}ms · "
                f"max={float(ingest_detail['changed_max_ms']):.1f}ms · "
                f"samples={','.join(f'{value:.1f}' for value in walls)}"
            )
            phase_values: dict[str, list[float]] = {}
            for sample in changed_samples:
                phases = sample.get("phases_ms", {}) if isinstance(sample, dict) else {}
                if not isinstance(phases, dict):
                    continue
                for name, value in phases.items():
                    if isinstance(value, (int, float)):
                        phase_values.setdefault(str(name), []).append(float(value))
            if phase_values:
                rendered = " · ".join(
                    f"{name}={statistics.median(values):.1f}"
                    for name, values in phase_values.items())
                print(f"  phase p50: {rendered}")
        if args.check_semantic:
            semantic_detail = diagnostics.get("semantic", {})
            semantic_detail = semantic_detail if isinstance(semantic_detail, dict) else {}
            ready_samples = semantic_detail.get("ready_samples_ms", [])
            preparation_fallbacks = semantic_detail.get("preparation_fallbacks", 0)
            print(
                "\nsemantic proof: "
                f"{len(ready_samples)}/3 ready strict samples; "
                f"{preparation_fallbacks} off-path preparation fallback(s) excluded"
            )
            phase_samples = semantic_detail.get("phase_samples", [])
            phase_values: dict[str, list[float]] = {}
            for sample in phase_samples if isinstance(phase_samples, list) else []:
                if not isinstance(sample, dict):
                    continue
                phases = sample.get("phases_ms", {})
                if isinstance(phases, dict):
                    for name, value in phases.items():
                        if isinstance(value, (int, float)):
                            phase_values.setdefault(str(name), []).append(float(value))
                for name in ("hybrid_compute_ms", "semantic_dispatch_ms",
                             "worker_search_ms", "client_roundtrip_ms",
                             "client_overhead_ms"):
                    value = sample.get(name)
                    if isinstance(value, (int, float)):
                        phase_values.setdefault(name, []).append(float(value))
            if phase_values:
                medians = {
                    name: statistics.median(values)
                    for name, values in phase_values.items()
                }
                hybrid = sorted(
                    (name, name) for name in medians
                    if name not in {"coherence", "hybrid_compute_ms",
                                    "semantic_dispatch_ms", "worker_search_ms",
                                    "client_roundtrip_ms", "client_overhead_ms"})
                scoped = [
                    ("hybrid", hybrid),
                    ("semantic", [(name, name) for name in (
                        "coherence", "hybrid_compute_ms", "semantic_dispatch_ms")
                        if name in medians]),
                    ("worker", [("search", "worker_search_ms")]
                     if "worker_search_ms" in medians else []),
                    ("client", [(name.removeprefix("client_"), name)
                                for name in ("client_roundtrip_ms", "client_overhead_ms")
                                if name in medians]),
                ]
                print("semantic phase medians ms:")
                ready_wall = [float(value) for value in ready_samples
                              if isinstance(value, (int, float))]
                if ready_wall:
                    print(f"  command: wall={statistics.median(ready_wall):.1f}")
                for scope, values in scoped:
                    if values:
                        rendered = " · ".join(
                            f"{label}={medians[name]:.1f}[n={len(phase_values[name])}]"
                            for label, name in values)
                        print(f"  {scope}: {rendered}")
        print("\nlive diagnostic (ambient corpus; excluded from the portable gate):")
        print(f"  {'metric':<31} {'measured':>10} {'local SLA':>10}  status / outcome")
        first_shown = f"{first_value:.0f}ms" if first_value is not None else "-"
        print(
            f"  {'live_first_freshen_wall_ms':<31} {first_shown:>10} "
            f"{first_limit:>9.0f}ms  {first_status} / {first_outcome}"
        )
        warm_shown = f"{warm_value:.0f}ms" if warm_value is not None else "-"
        print(
            f"  {'live_zero_change_shortcut_ms':<31} {warm_shown:>10} "
            f"{warm_limit:>9.0f}ms  {warm_status}"
        )
        live_reason = live.get("reason")
        if live_reason:
            print(f"  proof: {live_reason}")
        for error in live_budget_errors:
            print(f"  config: {error}")

        if breaches:
            print(f"\n{len(breaches)} budget breach(es):")
            for b in breaches:
                print(f"  ! {b}")
        else:
            skipped = [key for key, val, _limit, status in rows if val is None and status == "skip"]
            suffix = f"; unavailable optional metrics: {', '.join(skipped)}" if skipped else ""
            live_suffix = "; live SLA green" if args.check_live else ""
            print(f"\nportable measured budgets green{live_suffix}{suffix}.")

    return 1 if (portable_check and breaches) else 0


if __name__ == "__main__":
    raise SystemExit(main())
