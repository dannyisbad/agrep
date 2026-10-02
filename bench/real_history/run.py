#!/usr/bin/env python3
"""Index the operator's real stores read-only into a fresh scratch data dir and check the scale invariants."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import invariants  # noqa: E402
import sandbox  # noqa: E402
import shapes  # noqa: E402

DEFAULT_SCRATCH_ROOT = Path.home() / ".agrep-p4" / "RealHistory"


class Refusal(RuntimeError):
    pass


def du_kilobytes(path: Path) -> int:
    result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, check=True, timeout=600)
    return int(result.stdout.split()[0])


def store_roots(home: Path) -> dict[str, list[Path]]:
    roots: dict[str, list[Path]] = {}
    for adapter, relatives in sandbox.STORE_ROOTS.items():
        present = [home / relative for relative in relatives if (home / relative).exists()]
        if present:
            roots[adapter] = present
    return roots


def refuse_production(data: Path, home: Path) -> None:
    production = sandbox.production_data_dir(home).resolve()
    resolved = data.resolve()
    if resolved == production or production in resolved.parents or resolved in production.parents:
        raise Refusal(f"scratch data dir {resolved} overlaps the production data dir {production}")
    for name in ("AGREP_DATA_DIR", "AGREP_HOME"):
        value = os.environ.get(name)
        if value and Path(value).expanduser().resolve() == production:
            raise Refusal(f"{name} in the calling environment points at production; unset it")


def discover_paths(runner: sandbox.CliRunner) -> list[tuple[str, Path]]:
    listing = runner.rs(["stores", "--paths"], timeout=1800)
    if listing.returncode != 0:
        raise Refusal(f"stores --paths failed: rc={listing.returncode}")
    return [(row["name"], Path(row["path"])) for row in json.loads(listing.stdout)
            if row.get("state") == "available"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="real home whose stores are read (never written)")
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--binary", type=Path, default=sandbox.resolve_binary())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--min-free-ratio", type=float, default=2.0,
                        help="refuse unless free disk >= ratio x du(store roots)")
    parser.add_argument("--sample", type=int, default=24, help="handles/files sampled per probe")
    parser.add_argument("--timeout", type=float, default=7200, help="per-command timeout in seconds")
    parser.add_argument("--no-audit", action="store_true", help="skip `agrep audit --full`")
    parser.add_argument("--keep", action="store_true", help="keep the scratch data dir")
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)

    home = args.home.resolve()
    if not args.binary.is_file():
        print(f"ingest binary missing: {args.binary}", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    scratch = args.scratch_root / f"run-{stamp}-{os.getpid()}"
    data = scratch / "data"
    report: dict = {"scratch": str(scratch), "home": str(home), "checks": [], "timings": {},
                    "store_kilobytes": {}, "findings": []}
    try:
        refuse_production(data, home)
        scratch.mkdir(parents=True, mode=0o700)
        data.mkdir(mode=0o700)
        roots = store_roots(home)
        total_kb = 0
        for adapter, paths in roots.items():
            kilobytes = sum(du_kilobytes(path) for path in paths)
            report["store_kilobytes"][adapter] = kilobytes
            total_kb += kilobytes
        free_kb = shutil.disk_usage(scratch).free // 1024
        report["store_kilobytes"]["total"] = total_kb
        report["free_kilobytes"] = free_kb
        if free_kb < args.min_free_ratio * total_kb:
            raise Refusal(f"free disk {free_kb} KiB < {args.min_free_ratio} x store size {total_kb} KiB")
        (data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        env = sandbox.sealed_env(home=home, data=data, scratch=scratch, binary=args.binary)
        env["AGREP_DATA_READONLY"] = str(sandbox.production_data_dir(home))
        runner = sandbox.CliRunner(env, cwd=scratch, python=args.python, default_timeout=args.timeout)
        discovered = discover_paths(runner)
        report["discovered_files"] = {}
        for adapter, _path in discovered:
            report["discovered_files"][adapter] = report["discovered_files"].get(adapter, 0) + 1
        report["content_bytes"] = sum(path.stat().st_size for _adapter, path in discovered
                                      if path.is_file())

        cold = runner.cli(["index"], label="cold index")
        report["timings"]["cold_index_s"] = round(runner.timings[-1][1], 2)
        report["cold_index_rc"] = cold.returncode
        if cold.returncode != 0:
            report["findings"].append(f"cold index exited {cold.returncode}")
            raise Refusal("cold index failed; see rc")
        before = invariants.artifact_hashes(data)
        warm = runner.cli(["index"], label="warm index")
        report["timings"]["warm_index_s"] = round(runner.timings[-1][1], 2)
        report["warm_index_rc"] = warm.returncode
        after = invariants.artifact_hashes(data)

        book = invariants.intake_book(data)
        messages = invariants.read_jsonl(data / "messages.jsonl")
        sessions = invariants.read_jsonl(data / "sessions.jsonl")
        checks = [
            invariants.check_intake_identity(book),
            invariants.check_source_bounds(book, home, shapes.JSONL_ADAPTERS, sample=None),
            invariants.check_coverage(discovered, book, sessions),
            invariants.check_per_adapter_bounds(messages, book),
            invariants.check_duplicate_ids(messages),
            invariants.check_family_closure(messages, sessions, data / "corpus.db"),
            invariants.check_project_labels(sessions, book),
            invariants.check_warm_identity(before, after),
            invariants.check_handle_round_trip(runner, messages, sessions, sample=args.sample,
                                               corpus=data / "corpus.db"),
            invariants.check_search_first_lines(runner, sessions, sample=min(args.sample, 12)),
        ]
        if not args.no_audit:
            audit = runner.cli(["audit", "--full", "--json"], label="audit --full")
            report["timings"]["audit_full_s"] = round(runner.timings[-1][1], 2)
            summary = {}
            for row in runner.json_rows(audit):
                for key in ("files", "ok", "problems", "gaps", "warnings", "errors", "exit", "status"):
                    if key in row and not isinstance(row[key], (list, dict)):
                        summary[key] = row[key]
            checks.append(invariants.Check("audit_full", audit.returncode == 0,
                                           {"rc": audit.returncode, **summary}))
        report["checks"] = [check.as_dict() for check in checks]
        report["timings"]["commands"] = [(label, round(seconds, 2)) for label, seconds in runner.timings]
        for check in checks:
            if not check.ok:
                adapters = sorted({str(key).split(":")[0] for key in
                                   (check.counts.get("violations") or check.counts.get("problems")
                                    or check.counts.get("generic_container_labels") or {})})
                report["findings"].append(
                    f"{check.name} failed: counts={json.dumps(check.counts, sort_keys=True)}"
                    + (f" adapters={adapters}" if adapters else ""))
        report["derived_bytes"] = sum(path.stat().st_size for path in data.rglob("*") if path.is_file())
        report["sessions"] = len(sessions)
        report["messages"] = len(messages)
        stopped = runner.reap()
        report["background_processes_stopped"] = stopped
    except Refusal as refusal:
        report["refused"] = str(refusal)
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=1, sort_keys=True))
        if scratch.exists() and not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
            print(f"scratch deleted: {scratch}", file=sys.stderr)
        elif scratch.exists():
            print(f"scratch kept: {scratch}", file=sys.stderr)
    if report.get("refused"):
        return 2
    return 0 if all(check["ok"] for check in report["checks"]) and report["checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
