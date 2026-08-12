#!/usr/bin/env python3
"""No test module may hand the next one a changed interpreter.

Three outages came from this one shape, each correct code doing its job and
forgetting to give the interpreter back: cli.main()'s grep-style SIGPIPE
disposition outliving its test, so the next write to a closed pipe killed the
whole run; selftest.main()'s read-only guard outliving its process, so every
writer after it refused; a background rebuild filling the shared data dir
mid-run, so what a later test measured depended on how far it had got.

Runs py/ discovery module by module in one interpreter and diffs the state a
later module inherits after each. Not a test of any product behavior: it is
the check that the other suites mean the same thing in any order.

    python py/global_state_sweep.py            # exits 1 on a leak
"""

from __future__ import annotations

import atexit
import locale
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None

PY_DIR = Path(__file__).resolve().parent
REPO = PY_DIR.parent

_SIGNALS = tuple(
    name for name in (
        "SIGPIPE", "SIGINT", "SIGTERM", "SIGHUP", "SIGALRM", "SIGCHLD",
        "SIGXFSZ", "SIGUSR1", "SIGUSR2")
    if hasattr(signal, name))
_LIMITS = tuple(
    name for name in ("RLIMIT_NOFILE", "RLIMIT_AS", "RLIMIT_CPU")
    if hasattr(resource, name))

# _test_support establishes the sandbox on the first module that imports it,
# which is the isolation working. Allowed for exactly these keys and only from
# unset to set: a module changing one afterwards is still a leak.
ESTABLISHED = (
    "AGREP_DATA_DIR", "AGREP_DATA_DIR_SOURCE", "AGREP_HOME",
    "AGREP_PYTHON_RUNTIME_BUILD_ID", "AGREP_NO_DAEMON",
    "AGREP_NO_SEM_WORKER")

# events.py:98 writes the source it resolved back to the environment on import,
# so the harness's "test" becomes the product's "env" once. Only that pair.
NORMALIZED = {"AGREP_DATA_DIR_SOURCE": {"test", "env"}}

# A monotonic start stamp product code writes on first use. Its value is meant
# to move; its presence is not a handover of state.
CHURN = ("AGREP_T0",)

# One tempfile cleanup per fixture root, so this is bounded by the number of
# modules that build one - never by the number of tests.
MAX_ATEXIT_GROWTH = 24

# The suite only grows. A collapse to a handful means the glob or the path is
# wrong, not that the tree shrank, and the sweep must say so instead of passing.
MIN_MODULES = 100


def _python_source_inventory(repo: Path = REPO) -> set[str]:
    """Existing tracked and non-ignored untracked Python source paths."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", "*.py"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or f"exit {result.returncode}"
        raise RuntimeError(f"source inventory unavailable: {detail}")
    paths = (os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)
    return {str(Path(path)) for path in paths
            if os.path.lexists(repo / Path(path))}


def _snapshot() -> dict:
    return {
        "signals": {name: signal.getsignal(getattr(signal, name))
                    for name in _SIGNALS},
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        # a duplicate entry appended and left is churn, not a changed search
        # order; what a later module inherits is the set of roots
        "syspath": set(sys.path),
        "locale": locale.setlocale(locale.LC_ALL),
        "limits": {name: resource.getrlimit(getattr(resource, name))
                   for name in _LIMITS},
        "atexit": atexit._ncallbacks(),
        # Git's ignored-aware inventory keeps generated dependency trees out
        # while retaining additions and removals visible to the repository.
        "tree": _python_source_inventory(),
    }


def _leaks(before: dict, after: dict) -> list[str]:
    """Every inherited difference, named by the property that carries it."""
    found = []
    for name, was in before["signals"].items():
        if was != after["signals"][name]:
            found.append(
                f"signal {name}: {was!r} -> {after['signals'][name]!r}")
    for key in sorted(set(before["env"]) | set(after["env"])):
        was, now = before["env"].get(key), after["env"].get(key)
        if was == now or key in CHURN:
            continue
        if key in ESTABLISHED and was is None:
            continue
        if {was, now} <= NORMALIZED.get(key, set()):
            continue
        found.append(f"env {key}: {was!r} -> {now!r}")
    for field in ("cwd", "locale"):
        if before[field] != after[field]:
            found.append(f"{field}: {before[field]!r} -> {after[field]!r}")
    if before["syspath"] != after["syspath"]:
        found.append(
            f"sys.path added {sorted(after['syspath'] - before['syspath'])} "
            f"dropped {sorted(before['syspath'] - after['syspath'])}")
    for name, was in before["limits"].items():
        if was != after["limits"][name]:
            found.append(f"{name}: {was} -> {after['limits'][name]}")
    if before["tree"] != after["tree"]:
        found.append(
            f"repo .py files added {sorted(after['tree'] - before['tree'])} "
            f"removed {sorted(before['tree'] - after['tree'])}")
    return found


def _result_failures(module: str, result: unittest.TestResult) -> list[str]:
    """Test failures make a module's inherited-state observation unusable."""
    failed = len(result.failures)
    errored = len(result.errors)
    if not failed and not errored:
        return []
    found = [
        f"{module}: test run failed ({failed} failure(s), {errored} error(s))"
    ]
    for kind, records in (("failure", result.failures), ("error", result.errors)):
        found.extend(
            f"{module}: {kind} {test.id()}" for test, _traceback in records)
    return found


def main() -> int:
    loader = unittest.TestLoader()
    modules = sorted(path.stem for path in PY_DIR.glob("test_*.py"))
    started = time.monotonic()
    found: list[str] = []
    # A sweep that probed nothing must not report clean.
    if len(modules) < MIN_MODULES:
        print(f"  swept {len(modules)} modules, fewer than the {MIN_MODULES} "
              "this tree had: the probe did not run")
        return 1
    # Establish the sandbox BEFORE the baseline: env, the daemon dead switch
    # and the purity prepend belong to the harness, not the first importer -
    # measured from here, any later touch is a leak.
    import _test_support
    _test_support.isolate_data_dir()
    with open(os.devnull, "w", encoding="utf-8") as quiet:
        runner = unittest.TextTestRunner(stream=quiet, verbosity=0)
        state = _snapshot()
        first_atexit = state["atexit"]
        for name in modules:
            suite = loader.loadTestsFromName(name)
            result = runner.run(suite)
            found.extend(_result_failures(name, result))
            after = _snapshot()
            found.extend(f"{name}: {line}" for line in _leaks(state, after))
            state = after
    growth = state["atexit"] - first_atexit
    if growth > MAX_ATEXIT_GROWTH:
        found.append(
            f"atexit queue grew by {growth}, above the {MAX_ATEXIT_GROWTH} "
            "one-per-fixture-root bound")
    for line in found:
        print(f"  leak {line}")
    print(f"  ---- {len(modules)} modules swept, {len(found)} leaked, "
          f"atexit +{growth}, {time.monotonic() - started:.0f}s ----")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
