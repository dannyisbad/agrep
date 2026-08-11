#!/usr/bin/env python3
"""Scale gate for generation-bound incremental embedding plans."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import common  # noqa: E402
import embed  # noqa: E402


BUDGETS = {
    "repeat_page_1m_ms": 150.0,
    "projected_repeat_page_10m_ms": 250.0,
}
_ISOLATED_DATA_ENV = "_AGREP_EMBED_PLAN_SCALE_DATA_DIR"


def _message(index: int) -> common.Message:
    return common.Message(
        id=f"codex:{index:016x}:{index}", agent="codex", project="/scale",
        session=f"{index:016x}", ts=index, turn=index, text="",
        who="user", model="scale", model_source="explicit",
    )


def _run_case(rows: int, page: int, parent: Path) -> dict:
    root = parent / str(rows)
    root.mkdir()
    source = {"ingest_signature": f"plan-scale-{rows}"}
    old_data = common.DATA_DIR
    original_resolver = embed._resolve_pending_messages
    original_iter = common.iter_messages
    common.DATA_DIR = root
    try:
        started = time.perf_counter()
        builder = embed._PendingPlanBuilder(source, "output-a", "scale-model")
        for index in range(rows):
            builder.add(_message(index), f"{index & ((1 << 64) - 1):016x}", index)
        builder.publish(total=rows)
        build_s = time.perf_counter() - started

        first = [f"codex:{index:016x}:{index}"
                 for index in range(rows - 1, rows - page - 1, -1)]
        if not embed._advance_pending_plan(
                source, "output-a", "output-b", first):
            raise RuntimeError("could not advance synthetic pending plan")

        def resolve(plan_rows):
            return [_message(int(str(row[0]).rsplit(":", 1)[1]))
                    for row in plan_rows]

        def source_scan(*_args, **_kwargs):
            raise RuntimeError("repeat plan touched transcript source")

        embed._resolve_pending_messages = resolve
        common.iter_messages = source_scan
        started = time.perf_counter()
        planned = embed._load_pending_plan(
            source, "output-b", "scale-model", page, page)
        repeat_ms = (time.perf_counter() - started) * 1000.0
        if planned is None or len(planned["messages"]) != page:
            raise RuntimeError("repeat plan did not return a complete bounded page")
        expected = list(range(rows - page - 1, rows - page * 2 - 1, -1))
        got = [row.turn for row in planned["messages"]]
        if got != expected:
            raise RuntimeError("repeat plan changed newest-first ordering")
        source_invalid = embed._load_pending_plan(
            {"ingest_signature": "moved"}, "output-b", "scale-model", page, page)
        output_invalid = embed._load_pending_plan(
            source, "moved", "scale-model", page, page)
        if source_invalid is not None or output_invalid is not None:
            raise RuntimeError("generation/output mismatch reused a pending plan")
        return {
            "rows": rows, "page": page, "build_s": round(build_s, 3),
            "artifact_mib": round(embed._pending_plan_path().stat().st_size / 2**20, 3),
            "repeat_page_ms": round(repeat_ms, 3), "source_scans": 0,
            "source_invalidation": True, "output_invalidation": True,
        }
    finally:
        embed._resolve_pending_messages = original_resolver
        common.iter_messages = original_iter
        common.DATA_DIR = old_data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", default=[100_000, 1_000_000])
    parser.add_argument("--page", type=int, default=1000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if any(rows <= args.page for rows in args.rows) or args.page <= 0:
        parser.error("each row count must exceed a positive page size")

    with tempfile.TemporaryDirectory(prefix="agrep-embed-plan-") as raw:
        reports = [_run_case(rows, args.page, Path(raw)) for rows in args.rows]
    largest = max(reports, key=lambda row: row["rows"])
    projection = float(largest["repeat_page_ms"]) * 1.25 + 5.0
    report = {
        "cases": reports,
        "projected_repeat_page_10m_ms": round(projection, 3),
        "budgets": BUDGETS,
    }
    failures = []
    million = next((row for row in reports if row["rows"] == 1_000_000), None)
    if million and million["repeat_page_ms"] > BUDGETS["repeat_page_1m_ms"]:
        failures.append("1M repeat page")
    if projection > BUDGETS["projected_repeat_page_10m_ms"]:
        failures.append("10M projected repeat page")
    if any(row["source_scans"] != 0 for row in reports):
        failures.append("repeat source scan")
    report["failures"] = failures
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in reports:
            print(f"{row['rows']:>10,} rows  build {row['build_s']:>7.3f}s  "
                  f"plan {row['artifact_mib']:>7.1f} MiB  "
                  f"repeat {row['repeat_page_ms']:>7.2f}ms  scans=0")
        print(f"10M projected repeat page: {projection:.2f}ms")
        print("embed-plan gate: " + ("FAIL " + ", ".join(failures)
                                     if failures else "PASS"))
    return 1 if args.check and failures else 0


def _isolated_main() -> int:
    if os.environ.get(_ISOLATED_DATA_ENV):
        return main()
    with tempfile.TemporaryDirectory(prefix="agrep-embed-plan-owner-") as raw:
        data = Path(raw) / "data"
        data.mkdir()
        env = dict(os.environ)
        env["AGREP_DATA_DIR"] = str(data)
        env[_ISOLATED_DATA_ENV] = str(data)
        return subprocess.call(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env=env)


if __name__ == "__main__":
    raise SystemExit(_isolated_main())
