#!/usr/bin/env python3
"""Index a store home read-only into a fresh scratch data dir and check the scale invariants.

The home is the operator's real one or, preferably, a frozen snapshot of it (snapshot.py or
--freeze): clones never change under the run, so every invariant holds or is a real finding."""

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
import snapshot  # noqa: E402

DEFAULT_SCRATCH_ROOT = Path.home() / ".agrep-real-history"
# Derived artifacts measured 0.26 x content on 11.45 GB of real stores; 1.0 keeps a 4x margin.
DEFAULT_MIN_FREE_RATIO = 1.0


class Refusal(RuntimeError):
    pass


def refuse_production(data: Path, homes: list[Path]) -> None:
    """The scratch data dir may not touch any production data dir: the indexed home's, the
    snapshot source's, nor the operator's own."""
    resolved = data.resolve()
    productions = {sandbox.production_data_dir(home).resolve() for home in [*homes, Path.home()]}
    for production in sorted(productions):
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
                        help="home whose stores are indexed (never written): the real one, or a "
                             "frozen snapshot written by snapshot.py")
    parser.add_argument("--freeze", nargs="?", const=True, default=None, metavar="DIR",
                        help="snapshot --home's stores first (clones + SQLite backups) into DIR "
                             "(default: inside scratch) and index the snapshot instead")
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--binary", type=Path, default=sandbox.resolve_binary())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--min-free-ratio", type=float, default=DEFAULT_MIN_FREE_RATIO,
                        help="refuse unless free disk >= ratio x bytes of discovered content")
    parser.add_argument("--sample", type=int, default=24, help="handles/files sampled per probe")
    parser.add_argument("--timeout", type=float, default=7200, help="per-command timeout in seconds")
    parser.add_argument("--no-audit", action="store_true", help="skip `agrep audit --full`")
    parser.add_argument("--keep", action="store_true", help="keep the scratch data dir and snapshot")
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)

    home = args.home.resolve()
    if not args.binary.is_file():
        print(f"ingest binary missing: {args.binary}", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    scratch = args.scratch_root / f"run-{stamp}-{os.getpid()}"
    data = scratch / "data"
    frozen: Path | None = None
    report: dict = {"scratch": str(scratch), "home": str(home), "checks": [], "timings": {},
                    "content_kilobytes": {}, "findings": []}
    try:
        manifest = snapshot.read_manifest(home)
        source_home = Path(manifest["source_home"]) if manifest else home
        refuse_production(data, [home, source_home])
        scratch.mkdir(parents=True, mode=0o700)
        data.mkdir(mode=0o700)
        if args.freeze is not None:
            if manifest:
                raise Refusal(f"{home} is already a snapshot; drop --freeze")
            frozen = scratch / "home" if args.freeze is True else Path(args.freeze).resolve()
            started = time.monotonic()
            discovered = snapshot.discover(home, args.binary, scratch / "freeze")
            manifest = snapshot.freeze(home, frozen, discovered)
            report["timings"]["freeze_s"] = round(time.monotonic() - started, 2)
            source_home, home = home, frozen
            report["home"] = str(home)
        if manifest:
            report["snapshot"] = {key: manifest[key] for key in
                                  ("created", "filesystem", "apparent_kilobytes", "allocated_kilobytes")}
            report["snapshot"]["roots"] = [
                {key: root[key] for key in ("adapter", "relative", "kind", "files", "allocated_kilobytes")}
                for root in manifest["roots"]]
        (data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        env = sandbox.sealed_env(home=home, data=data, scratch=scratch, binary=args.binary)
        env["AGREP_DATA_READONLY"] = str(sandbox.production_data_dir(source_home))
        runner = sandbox.CliRunner(env, cwd=scratch, python=args.python, default_timeout=args.timeout)
        discovered = discover_paths(runner)
        report["discovered_files"] = {}
        for adapter, path in discovered:
            report["discovered_files"][adapter] = report["discovered_files"].get(adapter, 0) + 1
            if path.is_file():
                report["content_kilobytes"][adapter] = (
                    report["content_kilobytes"].get(adapter, 0) + path.stat().st_size // 1024)
        # du counts a clone's shared blocks twice; the run allocates derived data (measured
        # 0.26 x content) plus diverged clone blocks and SQLite backups, which the ratio's margin covers.
        content_kb = sum(report["content_kilobytes"].values())
        report["content_kilobytes"]["total"] = content_kb
        free_kb = shutil.disk_usage(scratch).free // 1024
        report["free_kilobytes"] = free_kb
        if free_kb < args.min_free_ratio * content_kb:
            raise Refusal(f"free disk {free_kb} KiB < {args.min_free_ratio} x content {content_kb} KiB")

        cold = runner.cli(["index"], label="cold index")
        report["timings"]["cold_index_s"] = round(runner.timings[-1][1], 2)
        report["cold_index_rc"] = cold.returncode
        if cold.returncode != 0:
            tail = (cold.stderr.strip().splitlines() or [""])[-1][:300]
            report["findings"].append(f"cold index exited {cold.returncode}: {tail}")
            raise Refusal("cold index failed; see findings")
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
            invariants.check_search_first_lines(runner, sessions, messages, sample=args.sample),
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
    except (Refusal, snapshot.SnapshotError) as refusal:
        report["refused"] = str(refusal)
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=1, sort_keys=True))
        if frozen is not None and frozen.exists() and scratch not in frozen.parents and not args.keep:
            snapshot.delete(frozen)
            print(f"snapshot deleted: {frozen}", file=sys.stderr)
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
