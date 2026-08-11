#!/usr/bin/env python3
"""Every test this repository has, in one command, with counts.

A gate that runs a chosen subset is a spotlight: whatever it does not name
rots unobserved. So this names nothing to run - it runs Rust, the whole
Python discovery tree, selftest and the bench suites, and the only entries in
`SEPARATE_PASSES` are suites that *cannot* share an interpreter, each with the
reason it needs its own.

    python gate.py                 # everything; exit 0 only if all of it passed
    python gate.py --list          # what would run, and what is excluded
    python gate.py --no-rust       # skip cargo (Python contracts only)

Nothing here writes to the real data directory: the Python suites isolate one
under $TMPDIR (py/_test_support.py) and the bench harnesses build their own.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PY = ROOT / "py"
BENCH = ROOT / "bench"

# One process per bench module: the first to import bench/resources.py caches
# it as `resources` and every later py/ import resolves against the wrong one.
SEPARATE_PASSES = {
    "bench/*": "bench/resources.py shadows py/resources.py in a shared interpreter",
}

EXCLUDED = {}

# These measure the box, not the code: CPU and RSS budgets breach on a machine
# that is busy with anything else, so a red here is not always a defect.
HOST_SENSITIVE = ("bench/perf", "bench/resources")

# Reds that predate this gate, each verified by running the same pass at the
# recorded commit in a throwaway worktree. Annotation only - a listed pass
# still fails the gate. Drop the entry the day the pass goes green.
KNOWN_RED = {
    "bench/resources":
        "host-coupled budget check: exits 1 on breached=['idle_cpu_percent', "
        "'rss_mib'] when the box is busy, measured 7.298 vs 5.0 and 578 vs 384 "
        "MiB here; the semantic-helper ownership line is an optional section "
        "skipping, not the failure; owner: none",
}


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Every AGREP_* the shell happens to carry, not a list of the ones we have
    # been bitten by: an installed agrep exports its own data dir and binary,
    # and AGREP_DATA_READONLY would silently make writer suites refuse.
    for key in [key for key in env if key.startswith("AGREP_")]:
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(PY), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["CARGO_INCREMENTAL"] = "0"
    return env


_UNITTEST_COUNT = re.compile(r"^Ran (\d+) tests?", re.M)
# "expected failures=N" is a passing verdict; only unexpected kinds are bad.
_UNITTEST_BAD = re.compile(r"(?<!expected )(failures|errors)=(\d+)")
_SELFTEST = re.compile(r"---- (\d+) passed, (\d+) failed")
_CARGO = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed", re.M)
_SWEEP = re.compile(r"---- (\d+) modules swept, (\d+) leaked")


def _tally(output: str) -> tuple[int, int]:
    """(ran, bad) counted from whichever runner produced this output."""
    ran = sum(int(n) for n in _UNITTEST_COUNT.findall(output))
    bad = sum(int(n) for _kind, n in _UNITTEST_BAD.findall(output))
    for passed, failed in _SELFTEST.findall(output):
        ran += int(passed) + int(failed)
        bad += int(failed)
    for passed, failed in _CARGO.findall(output):
        ran += int(passed) + int(failed)
        bad += int(failed)
    for modules, leaked in _SWEEP.findall(output):
        ran += int(modules)
        bad += int(leaked)
    return ran, bad


class Pass:
    def __init__(self, name: str, argv: list[str], cwd: Path) -> None:
        self.name, self.argv, self.cwd = name, argv, cwd
        self.ran = self.bad = 0
        self.code = -1
        self.seconds = 0.0

    @property
    def ok(self) -> bool:
        return self.code == 0 and self.bad == 0

    def run(self, env: dict[str, str]) -> None:
        started = time.monotonic()
        proc = subprocess.run(
            self.argv, cwd=self.cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.seconds = time.monotonic() - started
        self.code = proc.returncode
        self.ran, self.bad = _tally(proc.stdout)
        if not self.ok:
            sys.stdout.write(proc.stdout)
        # A checker that collects no tests still has to say what went wrong,
        # or "0 tests, 0 failed" and FAIL on one line teaches nobody anything.
        tally = (f"{self.ran} tests, {self.bad} failed" if self.ran
                 else f"no tests collected, exit {self.code}")
        note = KNOWN_RED.get(self.name, "")
        print(f"  {'ok  ' if self.ok else 'FAIL'} {self.name}: {tally} "
              f"({self.seconds:.0f}s)"
              + (f"\n         known red: {note}" if note and not self.ok else ""),
              flush=True)


def _passes(rust: bool) -> list[Pass]:
    python = [sys.executable]
    out = []
    if rust:
        out += [
            Pass("cargo build", ["cargo", "build", "--release"], ROOT),
            Pass("cargo test", ["cargo", "test", "--release"], ROOT),
        ]
    out.append(Pass(
        "repository privacy",
        [*python, str(BENCH / "validate_repo_privacy.py"), str(ROOT)], ROOT))
    out.append(Pass(
        "python discovery",
        [*python, "-m", "unittest", "discover", "-s", str(PY), "-p", "test_*.py"],
        ROOT))
    out.append(Pass("selftest", [*python, str(PY / "selftest.py")], ROOT))
    out.append(Pass(
        "global-state sweep", [*python, str(PY / "global_state_sweep.py")],
        ROOT))
    for module in sorted(path.stem for path in BENCH.glob("test_*.py")):
        out.append(Pass(
            f"bench/{module}", [*python, "-m", "unittest", module], BENCH))
    out.append(Pass("bench/perf", [*python, str(BENCH / "perf.py"), "--check"], ROOT))
    out.append(Pass(
        "bench/resources", [*python, str(BENCH / "resources.py"), "--check"], ROOT))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gate.py", description=__doc__.splitlines()[0])
    parser.add_argument("--no-rust", action="store_true",
                        help="skip cargo build/test")
    parser.add_argument("--no-host-gates", action="store_true",
                        help=f"skip the host-measurement passes "
                             f"({', '.join(HOST_SENSITIVE)}); they are named "
                             f"in the summary either way")
    parser.add_argument("--list", action="store_true",
                        help="print the passes and exclusions, run nothing")
    args = parser.parse_args(argv)

    passes = _passes(not args.no_rust)
    if args.list:
        for one in passes:
            host = " (measures this box)" if one.name in HOST_SENSITIVE else ""
            print(f"  {one.name}{host}")
        for what, why in {**SEPARATE_PASSES, **EXCLUDED}.items():
            print(f"  ({what}: {why})")
        return 0

    env = _env()
    started = time.monotonic()
    skipped = []
    for one in passes:
        if args.no_host_gates and one.name in HOST_SENSITIVE:
            skipped.append(one.name)
            continue
        one.run(env)
    passes = [one for one in passes if one.name not in skipped]
    for name in skipped:
        print(f"  --   {name}: not run (--no-host-gates)")
    failed = [one for one in passes if not one.ok]
    ran = sum(one.ran for one in passes)
    bad = sum(one.bad for one in passes)
    print(f"\n{ran} tests across {len(passes)} passes, {bad} failed, "
          f"{time.monotonic() - started:.0f}s")
    if failed:
        fresh = [one.name for one in failed if one.name not in KNOWN_RED]
        old = [one.name for one in failed if one.name in KNOWN_RED]
        print("RED: " + ", ".join(one.name for one in failed))
        if old:
            print("  known red before this gate existed: " + ", ".join(old))
        print("  new: " + (", ".join(fresh) if fresh else "none"))
        return 1
    print("GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
