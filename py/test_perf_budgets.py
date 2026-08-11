"""Black-box budget gates: the public CLI, a fixture corpus, wall clocks.

An earlier UX audit found that correctness-only instruments stayed green while
first-search and diagnostic latency grew by tens of seconds. These gates drive
`cli.py` as a stranger would - no internal imports on the measured path - so a
regression in startup tax, inline rebuilds, or a dropped disclosure line fails
a suite instead of waiting for a human to notice. These are product budgets,
not broad hang detectors; a command outside its public contract is a regression.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs")

# Binding UX-verdict budgets on a quiet fixture corpus - product numbers, not
# hang detectors. Hosted Windows x64 sets the ceilings (burst 0.63 s, warm
# renders 0.30 s measured quiet); ARM and unix sit comfortably under them.
BUDGET_CLASSIC_SEARCH_S = 0.350
BUDGET_BURST_SEARCH_S = 0.750
BUDGET_MACHINE_S = 0.350
# The export floor leaves headroom for supported two-core runners without hiding collapse.
MIN_FLAT_EXPORT_ROWS_PER_S = 25_000
MAX_FLAT_EXPORT_SAMPLE_S = 10.000
BUDGET_RECALL_S = 1.000
BUDGET_DIAGNOSTIC_S = 1.000
BUDGET_LIVE_SNAPSHOT_S = 0.300
BUDGET_LIGHTWEIGHT_S = 0.250

SESSION = "b0d9e7f0-0000-4000-8000-perfbudget00"
DRIFT_SESSION = "c1d9e7f0-0000-4000-8000-driftbudget0"
SCALE_SESSION = "d2d9e7f0-0000-4000-8000-flat50k00000"
SCALE_ROWS = 50_000
SMALL_CORPUS_MESSAGES = 60

_PROFILE_SECONDS_RE = r"(?:0|[1-9][0-9]*)\.[0-9]{2}"
_SLOW_PROFILE_RE = re.compile(
    rf"\Atook (?P<seconds>{_PROFILE_SECONDS_RE})s(?P<body>[^\r\n]*)\n\Z")
_SLOW_PROFILE_PHASE_RE = re.compile(
    rf"\A(?P<label>imports|freshen|query\[corpusdb\]|rest) "
    rf"{_PROFILE_SECONDS_RE}s\Z")
_SLOW_PROFILE_PHASE_ORDER = {
    "imports": 0, "freshen": 1, "query[corpusdb]": 2, "rest": 3,
}

_ENV_PREFIXES_TO_CLEAR = (
    "AGREP_", "CLAUDE", "CODEX_", "CURSOR_", "GEMINI_", "OPENCODE",
    "CLINE_", "CRUSH_",
)
_ENV_KEYS_TO_CLEAR = {
    "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "PYTHONDONTWRITEBYTECODE", "PYTHONHOME", "PYTHONPATH",
    "PYTHONPYCACHEPREFIX",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME",
}

_PERF_CLI_ENV = "AGREP_PERF_CLI"

_SEARCH_PAGE_FIELDS = frozenset({
    "completeness", "freshness", "filter_coverage", "self_exclusion",
    "semantic_coverage", "semantic_integrity", "engine", "query",
    "semantic", "tools_excluded",
})
_SEARCH_REQUIRED_FIELDS = frozenset({
    "completeness", "freshness", "filter_coverage", "self_exclusion",
    "semantic_coverage", "engine", "query",
})


def _search_json_page(stdout: str) -> tuple[dict, list[dict]]:
    records = [json.loads(line) for line in stdout.splitlines() if line]
    if not records or records[0].get("kind") != "agrep-meta":
        raise AssertionError("search JSON did not lead with agrep-meta")
    if any(row.get("kind") == "agrep-meta" for row in records[1:]):
        raise AssertionError("search JSON emitted more than one agrep-meta")
    return records[0], records[1:]


def _wall(fn):
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def _median_wall(fn, attempts: int = 3):
    """Require all samples to agree on outcome and budget the median.

    The cold/drift gates below are deliberately single-shot. Warm gates get an
    odd-sized sample so one scheduler outlier cannot turn a healthy path red,
    while one lucky fast sample cannot hide a deterministic regression.
    """
    if attempts < 3 or attempts % 2 == 0:
        raise ValueError("timed warm samples require an odd count of at least 3")
    samples = [_wall(fn) for _ in range(attempts)]
    return_codes = {getattr(result, "returncode", None)
                    for _, result in samples}
    if len(return_codes) != 1:
        raise AssertionError(
            f"timed command had unstable return codes: {return_codes!r}")
    samples.sort(key=lambda item: item[0])
    return samples[len(samples) // 2]


def _report_installed_timing(label: str, seconds: float) -> None:
    if _INSTALLED_CLI is not None:
        print("installed-wheel timing mode=cli "
              f"{label}={seconds * 1000:.1f}ms", flush=True)


def _report_installed_throughput(
        label: str, rows: int, seconds: float) -> None:
    if _INSTALLED_CLI is not None:
        print("installed-wheel throughput mode=cli "
              f"{label}={rows / seconds:.0f}rows/s", flush=True)


def _report_bulk_sample(
        transport: str, sample: int, result: subprocess.CompletedProcess[str],
        rows: int, byte_count: int, seconds: float, profile: str,
) -> None:
    if _INSTALLED_CLI is None:
        return
    print(
        "installed-wheel bulk-output "
        f"transport={transport} sample={sample} rc={result.returncode} "
        f"full_exit_ms={seconds * 1000:.1f} bytes={byte_count} rows={rows} "
        f"rows_per_s={rows / seconds:.0f} profile={json.dumps(profile)}",
        flush=True,
    )


def _validate_optional_slow_profile(
        stderr: str, wall_s: float, *, forced: bool = False) -> str:
    if not stderr:
        if forced:
            raise AssertionError("forced 50k timing profile was missing")
        return ""
    match = _SLOW_PROFILE_RE.fullmatch(stderr)
    if match is None:
        raise AssertionError(f"unexpected 50k export stderr: {stderr!r}")
    body = match.group("body")
    if body.startswith(" (") and body.endswith(")"):
        labels = [body[2:-1]]
        if labels[0] not in _SLOW_PROFILE_PHASE_ORDER:
            raise AssertionError(f"unexpected 50k profile phase: {stderr!r}")
    elif body.startswith(": "):
        phases = body[2:].split(" · ")
        labels = []
        for phase in phases:
            phase_match = _SLOW_PROFILE_PHASE_RE.fullmatch(phase)
            if phase_match is None:
                raise AssertionError(f"unexpected 50k profile phase: {stderr!r}")
            labels.append(phase_match.group("label"))
        positions = [_SLOW_PROFILE_PHASE_ORDER[label] for label in labels]
        if len(labels) < 2 or positions != sorted(set(positions)):
            raise AssertionError(f"invalid 50k profile phase order: {stderr!r}")
    else:
        raise AssertionError(f"unexpected 50k profile grammar: {stderr!r}")
    reported = float(match.group("seconds"))
    if reported < 1.5 and not forced:
        raise AssertionError(f"profile line below its threshold: {stderr!r}")
    if reported > wall_s + 0.02 or wall_s - reported > 1.0:
        raise AssertionError(
            f"profile/wall mismatch: profile={reported:.2f}s wall={wall_s:.2f}s")
    return stderr.rstrip("\r\n")


def _require_release_binary() -> Path:
    if not RELEASE_BIN.is_file():
        raise AssertionError(f"release binary missing: {RELEASE_BIN}")
    return RELEASE_BIN


def _validate_installed_runtime(raw: str | None) -> Path | None:
    if raw is None:
        return None
    name = _PERF_CLI_ENV
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise AssertionError(f"{name} must name one absolute executable")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AssertionError(f"{name} is unavailable: {path}: {exc}") from exc
    checkout = ROOT.resolve()
    supplied = Path(os.path.abspath(path))
    for candidate in (supplied, resolved):
        try:
            candidate.relative_to(checkout)
        except ValueError:
            continue
        raise AssertionError(f"{name} must be outside the source checkout")
    if not resolved.is_file():
        raise AssertionError(f"{name} is not a file: {resolved}")
    if os.name == "nt" and resolved.suffix.lower() != ".exe":
        raise AssertionError(f"{name} must name an .exe launcher on Windows")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise AssertionError(f"{name} is not executable: {resolved}")
    return resolved


_INSTALLED_CLI = _validate_installed_runtime(os.environ.get(_PERF_CLI_ENV))


def _require_measured_runtime() -> Path:
    if _INSTALLED_CLI is None:
        return _require_release_binary()
    if not _INSTALLED_CLI.is_file():
        raise AssertionError(
            f"installed candidate disappeared: {_INSTALLED_CLI}")
    return _INSTALLED_CLI


def _cli_argv(*args: str) -> list[str]:
    if _INSTALLED_CLI is None:
        return [sys.executable, str(CLI), *args]
    return [str(_INSTALLED_CLI), *args]


def _cli_cwd(home: Path) -> Path:
    return home if _INSTALLED_CLI is not None else ROOT


def _fixture_env(home: Path, data: Path, protected_data: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key not in _ENV_KEYS_TO_CLEAR
        and not key.startswith(_ENV_PREFIXES_TO_CLEAR)
    }
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "AGREP_HOME": str(home),
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        # This sentinel is deliberately separate from the writable fixture.
        # Every child still carries the live-data safety discipline.
        "AGREP_DATA_READONLY": str(protected_data),
        "AGREP_NO_DAEMON": "1",
        "AGREP_NO_FETCH": "1",
        "PYTHONNOUSERSITE": "1",
        "RAYON_NUM_THREADS": "2",
    })
    if _INSTALLED_CLI is None:
        env["AGREP_RS_BIN"] = os.fspath(_require_release_binary())
        env["PYTHONPYCACHEPREFIX"] = str(data.parent / "python-cache")
    return env


def _public_cli(
        home: Path, data: Path, protected_data: Path, *args: str,
        timeout: float = 120, timing: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _fixture_env(home, data, protected_data)
    if timing:
        env["AGREP_TIMING"] = "1"
    return subprocess.run(
        _cli_argv(*args),
        cwd=_cli_cwd(home),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _timed_public_cli_to_file(
        home: Path, data: Path, protected_data: Path, output: Path,
        *args: str, timeout: float = 120,
) -> tuple[float, subprocess.CompletedProcess[str]]:
    env = _fixture_env(home, data, protected_data)
    env["AGREP_TIMING"] = "1"
    with output.open("wb") as sink:
        started = time.perf_counter()
        result = subprocess.run(
            _cli_argv(*args),
            cwd=_cli_cwd(home),
            env=env,
            stdout=sink,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        elapsed = time.perf_counter() - started
    return elapsed, result


def _prepare_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    home, data = root / "home", root / "data"
    protected = root / "protected-live-data"
    protected.mkdir(parents=True)
    canary = protected / "AGREP_DATA_READONLY.canary"
    canary.write_text("fixture commands must not touch this directory\n",
                      encoding="utf-8")
    return home, data, protected, canary


def _perf_fixture_parent() -> str | None:
    if os.name != "nt":
        return None
    parent = os.environ.get("LOCALAPPDATA")
    if not parent or not Path(parent).is_dir():
        raise RuntimeError("Windows performance fixtures require LOCALAPPDATA")
    return parent


def _assert_protected_canary(protected: Path, canary: Path) -> None:
    entries = sorted(path.name for path in protected.iterdir())
    if entries != [canary.name]:
        raise AssertionError(
            f"AGREP_DATA_READONLY sentinel changed: {entries!r}")
    if canary.read_text(encoding="utf-8") != (
            "fixture commands must not touch this directory\n"):
        raise AssertionError("AGREP_DATA_READONLY canary content changed")


def _write_small_fixture(
        home: Path, root: Path, *, session: str = SESSION,
        live_tail: bool = False, recap_at: int | None = None,
) -> Path:
    project = home / ".claude" / "projects" / "budget-fixture"
    project.mkdir(parents=True)
    rows = []
    for i in range(60):
        user_row = {
            "type": "user", "userType": "external", "sessionId": session,
            "timestamp": f"2026-07-25T12:{i:02d}:00.000Z",
            "cwd": str(root / "work" / "budget-fixture"),
            "message": {"role": "user",
                        "content": f"budget probe {i} quorum lantern"},
        }
        if recap_at == i:
            user_row["isCompactSummary"] = True
        rows.append(user_row)
        rows.append({
            "type": "assistant", "sessionId": session,
            "timestamp": f"2026-07-25T12:{i:02d}:01.000Z",
            "cwd": str(root / "work" / "budget-fixture"),
            "message": {
                "role": "assistant", "model": "claude-fixture",
                "content": [{"type": "text",
                             "text": f"reply {i} for the lantern"}],
            },
        })
    if live_tail:
        rows[-1]["timestamp"] = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    source = project / f"{session}.jsonl"
    source.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n"
                for row in rows),
        encoding="utf-8",
    )
    return source


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _tree_identity(root: Path) -> tuple[tuple[str, int, int, int, int, int], ...]:
    rows = []
    if not root.exists():
        return ()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rows.append((path.relative_to(root).as_posix(), *_file_identity(path)))
    return tuple(rows)


class PerfBudgets(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory
    home: Path
    data: Path
    protected_data: Path

    @classmethod
    def setUpClass(cls) -> None:
        _require_measured_runtime()
        cls.tmp = tempfile.TemporaryDirectory(
            prefix="agrep-budget-", dir=_perf_fixture_parent())
        root = Path(cls.tmp.name)
        cls.home, cls.data, cls.protected_data, cls.protected_canary = (
            _prepare_paths(root))
        cls.source = _write_small_fixture(cls.home, root, live_tail=True)
        # one paid build so every measured command below is a warm read
        build = cls._cli("index")
        if build.returncode != 0:
            raise AssertionError(
                f"fixture ingest failed: {build.stderr[-400:]}")
        warm = cls._cli("quorum lantern", "--color", "never")
        if warm.returncode != 0:
            raise AssertionError(
                f"fixture warmup search failed: {warm.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            _assert_protected_canary(
                cls.protected_data, cls.protected_canary)
        finally:
            cls.tmp.cleanup()

    @classmethod
    def _cli(
            cls, *args: str, timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return _public_cli(
            cls.home, cls.data, cls.protected_data, *args, timeout=timeout)

    def test_warm_search_meets_budget_in_each_display_renderer(self) -> None:
        for renderer in (
                ("classic-color", "--color", "always"),
                ("plain", "--color", "never")):
            with self.subTest(renderer=renderer[0]):
                secs, res = _median_wall(
                    lambda: self._cli(
                        "quorum lantern", *renderer[1:], "-n", "3"))
                _report_installed_timing(
                    f"warm_search_{renderer[0]}", secs)
                self.assertEqual(res.returncode, 0, res.stderr[-400:])
                self.assertIn("lantern", res.stdout)
                self.assertLess(secs, BUDGET_CLASSIC_SEARCH_S)

    def test_machine_surfaces_meet_budget_and_agree(self) -> None:
        secs_c, count = _median_wall(
            lambda: self._cli("quorum lantern", "-c"))
        self.assertEqual(count.returncode, 0, count.stderr[-400:])
        self.assertLess(secs_c, BUDGET_MACHINE_S)
        total = int(count.stdout.strip().splitlines()[-1])
        self.assertGreater(total, 0)

        secs_json, jsn = _median_wall(
            lambda: self._cli("quorum lantern", "--json", "-n", "0"))
        self.assertEqual(jsn.returncode, 0, jsn.stderr[-400:])
        self.assertLess(secs_json, BUDGET_MACHINE_S)
        meta, rows = _search_json_page(jsn.stdout)
        self.assertTrue(rows)
        self.assertTrue(all("self" in row for row in rows))
        # one counting story on a corpus with no self-session or tool rows
        self.assertEqual(len(rows), total)
        self.assertEqual(meta["completeness"]["total"], total)
        self.assertTrue(_SEARCH_REQUIRED_FIELDS <= meta.keys())
        self.assertTrue(all(_SEARCH_PAGE_FIELDS.isdisjoint(row) for row in rows))

        secs_flat, flat = _median_wall(
            lambda: self._cli("quorum lantern", "--flat", "-n", "0"))
        _report_installed_timing("warm_machine_flat", secs_flat)
        self.assertEqual(flat.returncode, 0, flat.stderr[-400:])
        self.assertLess(secs_flat, BUDGET_MACHINE_S)
        flat_rows = [l for l in flat.stdout.splitlines() if l and "\t" in l]
        self.assertEqual(len(flat_rows), total)

    def test_source_cli_current_window_is_identical_across_machine_surfaces(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-window-contract-") as td:
            root = Path(td)
            home, data, protected, canary = _prepare_paths(root)
            _write_small_fixture(
                home, root, session=SESSION, recap_at=30)
            env = _fixture_env(home, data, protected)
            env.update({
                "CLAUDECODE": "1",
                "CLAUDE_CODE_SESSION_ID": SESSION,
            })

            def cli(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(CLI), *args], cwd=ROOT, env=env,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120, check=False)

            built = cli("index")
            self.assertEqual(built.returncode, 0, built.stderr[-400:])
            count = cli("quorum lantern", "-c", "--no-auto")
            flat = cli("quorum lantern", "--flat", "-n", "0", "--no-auto")
            machine = cli("quorum lantern", "--json", "-n", "0", "--no-auto")
            included = cli(
                "quorum lantern", "-c", "--self", "--no-auto")

            for result in (count, flat, machine, included):
                self.assertEqual(result.returncode, 0, result.stderr[-400:])
            visible = int(count.stdout.strip())
            all_rows = int(included.stdout.strip())
            objects = [json.loads(line) for line in machine.stdout.splitlines()]
            head, rows = objects[0], objects[1:]
            flat_rows = [line for line in flat.stdout.splitlines() if "\t" in line]
            excluded = head["self_exclusion"]["excluded_hits"]

            self.assertGreater(excluded, 0)
            self.assertEqual(all_rows, visible + excluded)
            self.assertEqual(len(rows), visible)
            self.assertEqual(len(flat_rows), visible)
            self.assertEqual(head["self_exclusion"]["scope"], "current-window")
            self.assertEqual(count.stderr.count(f"excluded {excluded} hits"), 1)
            self.assertEqual(flat.stderr.count(f"excluded {excluded} hits"), 1)
            self.assertNotIn("excluded", machine.stderr)
            self.assertNotIn("--self to include", count.stderr + flat.stderr)
            _assert_protected_canary(protected, canary)

    def test_json_rows_honor_the_machine_contract(self) -> None:
        res = self._cli("budget probe 3", "--json")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        meta, rows = _search_json_page(res.stdout)
        self.assertTrue(rows)
        self.assertTrue(_SEARCH_REQUIRED_FIELDS <= meta.keys())
        for row in rows:
            self.assertIn("session", row)
            self.assertIn("turn", row)
            self.assertIn("self", row)
            self.assertTrue(_SEARCH_PAGE_FIELDS.isdisjoint(row))

    def test_doctor_meets_budget_without_traceback(self) -> None:
        before = _tree_identity(self.data)
        secs, res = _median_wall(lambda: self._cli("doctor"))
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, merged[-400:])
        self.assertNotIn("Traceback", merged)
        self.assertLess(secs, BUDGET_DIAGNOSTIC_S)
        self.assertIn("corpus", merged)
        # the page scan that would earn this row costs seconds, so routine
        # never runs it - and therefore never rules a line about it
        self.assertNotIn("integrity", merged.lower())
        self.assertEqual(_tree_identity(self.data), before)

    def test_status_meets_the_same_read_only_budget(self) -> None:
        before = _tree_identity(self.data)
        secs, res = _median_wall(lambda: self._cli("status"))
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, merged[-400:])
        self.assertNotIn("Traceback", merged)
        self.assertLess(secs, BUDGET_DIAGNOSTIC_S)
        # the page scan that would earn this row costs seconds, so routine
        # never runs it - and therefore never rules a line about it
        self.assertNotIn("integrity", merged.lower())
        self.assertEqual(_tree_identity(self.data), before)

    def test_routine_audit_cold_and_warm_paths_meet_budget(self) -> None:
        # Other tests may exercise --full first; force this exact fixture cache
        # cold so an untimed seed cannot hide the bounded routine recount.
        (self.data / ".audit-census.json").unlink(missing_ok=True)
        cold_secs, cold = _wall(lambda: self._cli("audit"))
        self.assertEqual(cold.returncode, 0, cold.stderr[-400:])
        self.assertLess(cold_secs, BUDGET_DIAGNOSTIC_S)
        cold_output = (cold.stdout + cold.stderr).lower()
        self.assertIn(
            "routine recounted 1 uncached or changed file", cold_output)
        self.assertIn("no changed file remained pending", cold_output)
        self.assertIn("audit clean", cold_output)
        self.assertNotIn("routine did not scan", cold_output)

        seeded = self._cli("audit", "--full")
        self.assertNotEqual(seeded.returncode, 2, seeded.stderr[-400:])
        warm_secs, warm = _median_wall(lambda: self._cli("audit"))
        self.assertNotEqual(warm.returncode, 2, warm.stderr[-400:])
        self.assertLess(warm_secs, BUDGET_DIAGNOSTIC_S)
        warm_output = (warm.stdout + warm.stderr).lower()
        self.assertIn("file verified from cache", warm_output)
        self.assertIn(
            "--full ignores cache and recounts every eligible file",
            warm_output,
        )

    def test_opt_in_diagnostics_disclose_their_evidence_tier(self) -> None:
        full = self._cli("audit", "--full")
        self.assertNotEqual(full.returncode, 2, full.stderr[-400:])
        full_output = (full.stdout + full.stderr).lower()
        self.assertIn("trustless recount", full_output)
        self.assertIn("estimated", full_output)

        first = self._cli("doctor", "--deep")
        self.assertEqual(first.returncode, 0, first.stderr[-400:])
        first_output = (first.stdout + first.stderr).lower()
        self.assertIn("quick_check", first_output)
        self.assertIn("estimate", first_output)
        secs, cached = _wall(lambda: self._cli("doctor", "--deep"))
        self.assertEqual(cached.returncode, 0, cached.stderr[-400:])
        self.assertLess(secs, BUDGET_DIAGNOSTIC_S)
        self.assertIn("cached quick_check",
                      (cached.stdout + cached.stderr).lower())

    def test_one_shot_live_surfaces_do_not_pay_interactive_settle(self) -> None:
        for argv in (("board", "--once"), ("tail", "--snapshot")):
            with self.subTest(argv=argv):
                secs, res = _median_wall(lambda: self._cli(*argv))
                self.assertEqual(res.returncode, 0, res.stderr[-400:])
                self.assertLess(secs, BUDGET_LIVE_SNAPSHOT_S)
                rendered = res.stdout.lower()
                self.assertNotIn("keys:", rendered)
                self.assertNotIn("q quit", rendered)
                if argv[0] == "board":
                    # Fast one-shot is still a liveness snapshot, not a
                    # decorative empty shell.
                    self.assertRegex(rendered, r"\b(working|live-window)\b")
                    self.assertRegex(rendered, r"\b[1-9][0-9]* live-window\b")
                else:
                    payload = json.loads(res.stdout)
                    self.assertIn("sessions", payload)
                    self.assertIn("window_s", payload)

    def test_recall_steady_and_probe_meet_budget(self) -> None:
        secs, recall = _median_wall(
            lambda: self._cli("recall", "quorum lantern", "--lexical"))
        self.assertEqual(recall.returncode, 0, recall.stderr[-400:])
        self.assertLess(secs, BUDGET_RECALL_S)
        self.assertIn(f"@{SESSION[:8]}:", recall.stdout)

        secs, probe = _median_wall(
            lambda: self._cli(
                "recall", "quorum lantern", "--probe", "--lexical"))
        self.assertEqual(probe.returncode, 0, probe.stderr[-400:])
        self.assertLess(secs, BUDGET_RECALL_S)
        probe_line = probe.stdout.strip().lower()
        self.assertIn("top candidate", probe_line)
        self.assertIn("provenance:", probe_line)
        self.assertIn("pull:", probe_line)

        secs, miss = _wall(lambda: self._cli(
            "recall", "zzqxv no prior context", "--probe"))
        self.assertEqual(miss.returncode, 2, miss.stderr[-400:])
        self.assertLess(secs, BUDGET_RECALL_S)
        miss_line = miss.stdout.strip().lower()
        self.assertIn("recall: no confident past-context pointer", miss_line)
        self.assertIn("searched", miss_line)

    def test_unavailable_explicit_semantic_fails_within_budget(self) -> None:
        secs, res = _wall(lambda: self._cli(
            "quorum lantern", "-s", "--color", "never", "-n", "3"))
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 2, merged[-600:])
        self.assertLess(secs, BUDGET_RECALL_S)
        self.assertEqual(res.stdout, "")
        self.assertIn("semantic search unavailable", merged.lower())
        self.assertNotIn("semantic warming", merged.lower())

    def test_lightweight_commands_keep_their_startup_budget(self) -> None:
        for argv, expected in (
                (("--help",), "usage:"),
                (("resume", "-l"), "")):
            with self.subTest(argv=argv):
                secs, res = _median_wall(lambda: self._cli(*argv))
                self.assertEqual(res.returncode, 0, res.stderr[-400:])
                self.assertLess(secs, BUDGET_LIGHTWEIGHT_S)
                if expected:
                    self.assertIn(expected, res.stdout.lower())

    def test_keyword_empty_result_states_its_corpus_size(self) -> None:
        keyword = self._cli(
            "zzqxv nonexistent phrase", "--lexical", "--color", "never")
        self.assertEqual(keyword.returncode, 1, keyword.stderr[-400:])
        keyword_output = (keyword.stdout + keyword.stderr).lower()
        self.assertRegex(
            keyword_output,
            rf"keyword:\s*0 matching rows across\s+"
            rf"{SMALL_CORPUS_MESSAGES} indexed messages")

    def test_unavailable_semantic_never_claims_searched_coverage(self) -> None:
        semantic = self._cli(
            "zzqxv nonexistent phrase", "-s", "--color", "never")
        self.assertEqual(semantic.returncode, 2, semantic.stderr[-400:])
        semantic_output = (semantic.stdout + semantic.stderr).lower()
        self.assertIn("semantic search unavailable", semantic_output)
        self.assertNotIn("embedded rows searched", semantic_output)
        self.assertNotIn("no match among", semantic_output)

    def test_repeat_search_is_not_dramatically_slower(self) -> None:
        first, _ = _wall(lambda: self._cli("budget probe 7"))
        second, _ = _wall(lambda: self._cli("budget probe 7"))
        # no-warm-path detector, loose enough to ignore scheduler noise
        self.assertLess(second, max(first * 3, BUDGET_CLASSIC_SEARCH_S))


class FreshnessBudgets(unittest.TestCase):
    """D2/D3 black-box cases live in their own corpus per test.

    A drifted source must not contaminate the steady-state timing class, and an
    earlier renderer must not accidentally pay or hide the first-reader cost.
    """

    tmp: tempfile.TemporaryDirectory
    home: Path
    data: Path
    protected_data: Path
    protected_canary: Path
    source: Path

    def setUp(self) -> None:
        _require_measured_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix="agrep-freshness-budget-")
        root = Path(self.tmp.name)
        (self.home, self.data, self.protected_data,
         self.protected_canary) = _prepare_paths(root)
        self.source = _write_small_fixture(
            self.home, root, session=DRIFT_SESSION)
        built = self._cli("index")
        if built.returncode != 0:
            self.fail(f"fixture ingest failed: {built.stderr[-600:]}")

    def tearDown(self) -> None:
        try:
            _assert_protected_canary(
                self.protected_data, self.protected_canary)
        finally:
            self.tmp.cleanup()

    def _cli(
            self, *args: str, timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return _public_cli(
            self.home, self.data, self.protected_data,
            *args, timeout=timeout)

    def test_idle_undrifted_burst_opener_is_fast_and_green(self) -> None:
        # D3 explicitly rejects ingest-signal wall age as read-time truth.
        ingest_signal = self.data / ".ingest.sig"
        self.assertTrue(ingest_signal.is_file())
        old = time.time() - (2 * 24 * 60 * 60)
        os.utime(ingest_signal, (old, old))

        secs, first = _wall(lambda: self._cli(
            "quorum lantern", "--color", "never", "-n", "1"))
        self.assertEqual(first.returncode, 0, first.stderr[-600:])
        self.assertLess(secs, BUDGET_BURST_SEARCH_S)
        self.assertIn("lantern", first.stdout)
        first_disclosure = first.stderr.lower()
        self.assertNotIn("history may be stale", first_disclosure)
        self.assertNotIn("behind", first_disclosure)

        for argv in (
                ("quorum lantern", "-c"),
                ("quorum lantern", "--flat", "-n", "1")):
            with self.subTest(argv=argv):
                secs, res = _median_wall(
                    lambda: self._cli(*argv), attempts=5)
                self.assertEqual(res.returncode, 0, res.stderr[-600:])
                self.assertLess(secs, BUDGET_MACHINE_S)
                self.assertNotIn("history may be stale", res.stderr.lower())

        secs, jsn = _median_wall(
            lambda: self._cli(
                "quorum lantern", "--json", "-n", "1"),
            attempts=5)
        self.assertEqual(jsn.returncode, 0, jsn.stderr[-600:])
        self.assertLess(secs, BUDGET_MACHINE_S)
        row = json.loads(jsn.stdout.splitlines()[0])
        freshness = row["freshness"]
        self.assertFalse(freshness.get("may_be_stale"), freshness)

    def test_real_drift_serves_last_good_fast_with_disclosure(self) -> None:
        # Publish one more source event without running the writer. The first
        # subsequent reader must serve the old row, narrate the drift, and
        # leave the published database byte identity alone.
        event = {
            "type": "user",
            "userType": "external",
            "sessionId": DRIFT_SESSION,
            "timestamp": "2026-07-25T13:01:00.000Z",
            "cwd": str(self.home / "work" / "budget-fixture"),
            "message": {
                "role": "user",
                "content": "new source drift sentinel row",
            },
        }
        with self.source.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        database = self.data / "corpus.db"
        before = _file_identity(database)

        secs, first = _wall(lambda: self._cli(
            "quorum lantern", "--color", "never", "-n", "1"))
        self.assertEqual(first.returncode, 0, first.stderr[-600:])
        self.assertLess(secs, BUDGET_BURST_SEARCH_S)
        self.assertIn("lantern", first.stdout)
        disclosure = first.stderr.lower()
        self.assertRegex(disclosure, r"\b(behind|last-good)\b")
        self.assertRegex(disclosure, r"\b(changed|drift)\b")
        self.assertEqual(_file_identity(database), before)

        # Every machine renderer carries the same drift story. JSON uses its
        # structured field; grep-parity surfaces keep it on stderr.
        for argv in (
                ("quorum lantern", "-c"),
                ("quorum lantern", "--flat", "-n", "1")):
            with self.subTest(argv=argv):
                secs, res = _median_wall(
                    lambda: self._cli(*argv), attempts=5)
                self.assertEqual(res.returncode, 0, res.stderr[-600:])
                self.assertLess(secs, BUDGET_MACHINE_S)
                machine_disclosure = res.stderr.lower()
                self.assertRegex(
                    machine_disclosure, r"\b(behind|last-good)\b")
                self.assertRegex(machine_disclosure, r"\b(changed|drift)\b")
                self.assertEqual(_file_identity(database), before)

        secs, jsn = _median_wall(
            lambda: self._cli(
                "quorum lantern", "--json", "-n", "1"),
            attempts=5)
        self.assertEqual(jsn.returncode, 0, jsn.stderr[-600:])
        self.assertLess(secs, BUDGET_MACHINE_S)
        freshness = json.loads(jsn.stdout.splitlines()[0])["freshness"]
        self.assertTrue(freshness.get("may_be_stale"), freshness)
        structured = json.dumps(freshness, sort_keys=True).lower()
        self.assertRegex(structured, r"\b(behind|last-good)\b")
        self.assertRegex(structured, r"\b(changed|drift)\b")
        self.assertEqual(_file_identity(database), before)


class FlatScaleBudget(unittest.TestCase):
    """The explicit uncapped export is exact and sustains useful throughput."""

    tmp: tempfile.TemporaryDirectory
    home: Path
    data: Path
    protected_data: Path
    protected_canary: Path

    @classmethod
    def setUpClass(cls) -> None:
        _require_measured_runtime()
        cls.tmp = tempfile.TemporaryDirectory(prefix="agrep-flat50k-budget-")
        root = Path(cls.tmp.name)
        (cls.home, cls.data, cls.protected_data,
         cls.protected_canary) = _prepare_paths(root)
        project = cls.home / ".claude" / "projects" / "flat50k-fixture"
        project.mkdir(parents=True)
        source = project / f"{SCALE_SESSION}.jsonl"
        turns = SCALE_ROWS // 2
        with source.open("w", encoding="utf-8", newline="\n") as handle:
            for i in range(turns):
                minute, second = divmod(i, 60)
                hour, minute = divmod(minute, 60)
                day, hour = divmod(hour, 24)
                timestamp = (
                    f"2025-01-{1 + day:02d}T{hour:02d}:{minute:02d}:"
                    f"{second:02d}.000Z")
                user = {
                    "type": "user", "userType": "external",
                    "sessionId": SCALE_SESSION,
                    "timestamp": timestamp,
                    "cwd": "/flat50k-fixture",
                    "message": {
                        "role": "user",
                        "content": f"flat budget user {i} fiftyk lantern",
                    },
                }
                reply = {
                    "type": "assistant", "sessionId": SCALE_SESSION,
                    "timestamp": timestamp,
                    "cwd": "/flat50k-fixture",
                    "message": {
                        "role": "assistant", "model": "claude-fixture",
                        "content": [{
                            "type": "text",
                            "text": (
                                f"flat budget reply {i} fiftyk lantern"),
                        }],
                    },
                }
                handle.write(json.dumps(
                    user, separators=(",", ":")) + "\n")
                handle.write(json.dumps(
                    reply, separators=(",", ":")) + "\n")
        build = cls._cli("index")
        if build.returncode != 0:
            raise AssertionError(
                f"50k fixture ingest failed: {build.stderr[-600:]}")
        count = cls._cli("fiftyk lantern", "-c")
        try:
            observed = int(count.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            raise AssertionError(
                f"50k fixture count was not numeric: {count.stdout!r}") from None
        if count.returncode != 0 or observed != SCALE_ROWS:
            raise AssertionError(
                f"50k fixture shape drifted: rc={count.returncode}, "
                f"rows={observed}, stderr={count.stderr[-400:]!r}")
        warm = cls._cli("fiftyk lantern", "--flat", "-n", "1")
        if warm.returncode != 0:
            raise AssertionError(
                f"50k flat warmup failed: {warm.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            _assert_protected_canary(
                cls.protected_data, cls.protected_canary)
        finally:
            cls.tmp.cleanup()

    @classmethod
    def _cli(
            cls, *args: str, timeout: float = 120, timing: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return _public_cli(
            cls.home, cls.data, cls.protected_data,
            *args, timeout=timeout, timing=timing)

    def test_flat_50k_rows_meet_exact_file_export_budget(self) -> None:
        samples = []
        root = Path(self.tmp.name)
        for sample in range(1, 4):
            output = root / f"flat50k-file-{sample}.tsv"
            secs, res = _timed_public_cli_to_file(
                self.home, self.data, self.protected_data, output,
                "fiftyk lantern", "--flat", "-n", "0")
            byte_count, profile = self._assert_flat_50k_file(
                output, res, secs)
            _report_bulk_sample(
                "file", sample, res, SCALE_ROWS, byte_count, secs, profile)
            samples.append((secs, profile, byte_count))

        pipe_secs, pipe = _wall(lambda: self._cli(
            "fiftyk lantern", "--flat", "-n", "0", timing=True))
        pipe_profile = self._assert_flat_50k_result(
            pipe, pipe_secs, forced_profile=True)
        pipe_bytes = len(pipe.stdout.encode("utf-8"))
        _report_bulk_sample(
            "pipe", 1, pipe, SCALE_ROWS, pipe_bytes,
            pipe_secs, pipe_profile)

        self.assertLess(
            max(sample[0] for sample in samples),
            MAX_FLAT_EXPORT_SAMPLE_S,
            f"file-sink sample stalled: {[sample[0] for sample in samples]!r}")
        secs = sorted(sample[0] for sample in samples)[1]
        _report_installed_timing("flat_50k_file_full_exit", secs)
        _report_installed_throughput(
            "flat_50k_file_export", SCALE_ROWS, secs)
        self.assertGreaterEqual(
            SCALE_ROWS / secs, MIN_FLAT_EXPORT_ROWS_PER_S,
            "file-sink samples=" + repr([
                {
                    "wall_ms": round(wall * 1000, 1),
                    "rows_per_s": round(SCALE_ROWS / wall),
                    "bytes": byte_count,
                    "profile": profile,
                }
                for wall, profile, byte_count in samples
            ]))

    def _assert_flat_50k_result(
            self, res: subprocess.CompletedProcess[str], wall_s: float,
            *, forced_profile: bool = False,
    ) -> str:
        self.assertEqual(res.returncode, 0, res.stderr[-600:])
        profile = _validate_optional_slow_profile(
            res.stderr, wall_s, forced=forced_profile)
        self.assertTrue(res.stdout.endswith("\n"))
        rows = res.stdout.splitlines()
        self.assertEqual(len(rows), SCALE_ROWS)
        for offset, row in enumerate(rows):
            self._assert_flat_50k_row(offset, row)
        return profile

    def _assert_flat_50k_file(
            self, output: Path, res: subprocess.CompletedProcess[str],
            wall_s: float,
    ) -> tuple[int, str]:
        self.assertIsNone(res.stdout)
        self.assertEqual(res.returncode, 0, res.stderr[-600:])
        profile = _validate_optional_slow_profile(
            res.stderr, wall_s, forced=True)
        byte_count = output.stat().st_size
        self.assertGreater(byte_count, 0)
        with output.open("rb") as raw:
            raw.seek(-1, os.SEEK_END)
            self.assertEqual(raw.read(1), b"\n")
        rows = 0
        with output.open("r", encoding="utf-8", errors="strict") as handle:
            for offset, line in enumerate(handle):
                self.assertTrue(line.endswith("\n"))
                self._assert_flat_50k_row(offset, line[:-1])
                rows = offset + 1
        self.assertEqual(rows, SCALE_ROWS)
        return byte_count, profile

    def _assert_flat_50k_row(self, offset: int, row: str) -> None:
        fields = row.split("\t", 5)
        self.assertEqual(len(fields), 6)
        session, agent, row_project, turn, who, snippet = fields
        turns = SCALE_ROWS // 2
        expected_who = "user" if offset < turns else "agent"
        expected_turn = turns - 1 - (offset % turns)
        expected_text = (
            f"flat budget user {expected_turn} fiftyk lantern"
            if expected_who == "user"
            else f"flat budget reply {expected_turn} fiftyk lantern")
        self.assertEqual(session, SCALE_SESSION)
        self.assertEqual(agent, "claude")
        self.assertEqual(row_project, "flat50k-fixture")
        self.assertEqual(turn, str(expected_turn))
        self.assertEqual(who, expected_who)
        self.assertEqual(snippet, expected_text)


class BudgetHarnessNegativeControls(unittest.TestCase):
    def test_missing_release_artifact_is_a_hard_failure(self) -> None:
        missing = ROOT / "target" / "release" / "definitely-not-agrep-rs"
        with mock.patch(f"{__name__}.RELEASE_BIN", missing):
            with self.assertRaisesRegex(
                    AssertionError, "release binary missing"):
                _require_release_binary()

    def test_installed_candidate_must_be_absolute_and_outside_checkout(self) -> None:
        with mock.patch.dict(os.environ, {_PERF_CLI_ENV: ""}):
            with self.assertRaisesRegex(AssertionError, "absolute executable"):
                _validate_installed_runtime(os.environ[_PERF_CLI_ENV])
        with mock.patch.dict(os.environ, {_PERF_CLI_ENV: "relative/agrep"}):
            with self.assertRaisesRegex(AssertionError, "absolute executable"):
                _validate_installed_runtime(os.environ[_PERF_CLI_ENV])
        with tempfile.TemporaryDirectory(prefix="agrep-missing-seam-") as tmp:
            missing = Path(tmp) / "agrep"
            with mock.patch.dict(os.environ, {_PERF_CLI_ENV: str(missing)}):
                with self.assertRaisesRegex(AssertionError, "is unavailable"):
                    _validate_installed_runtime(os.environ[_PERF_CLI_ENV])
        with mock.patch.dict(os.environ, {_PERF_CLI_ENV: str(CLI)}):
            with self.assertRaisesRegex(AssertionError, "outside the source"):
                _validate_installed_runtime(os.environ[_PERF_CLI_ENV])
        if os.name != "nt":
            with tempfile.TemporaryDirectory(
                    prefix="agrep-nonexec-seam-") as tmp:
                nonexec = Path(tmp) / "agrep"
                nonexec.write_text("candidate\n", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "not executable"):
                    _validate_installed_runtime(str(nonexec))

    def test_installed_candidate_owns_python_and_native_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-installed-seam-") as tmp:
            root = Path(tmp)
            executable = root / (
                "agrep.exe" if os.name == "nt" else "agrep")
            executable.write_text("candidate\n", encoding="utf-8")
            executable.chmod(0o755)
            candidate = _validate_installed_runtime(str(executable))
            missing = root / "missing-release-binary"
            with mock.patch(f"{__name__}._INSTALLED_CLI", candidate), \
                    mock.patch(f"{__name__}.RELEASE_BIN", missing), \
                    mock.patch.object(
                        _IsolatedBlackBox, "home", root / "home",
                        create=True), \
                    mock.patch.object(
                        _IsolatedBlackBox, "data", root / "data",
                        create=True), \
                    mock.patch.dict(os.environ, {
                        "CODEX_THREAD_ID": "ambient-thread",
                        "CLAUDECODE": "1",
                    }):
                env = _fixture_env(root / "home", root / "data",
                                   root / "protected")
                isolated = _IsolatedBlackBox._env()
                self.assertEqual(_require_measured_runtime(), candidate)
                self.assertEqual(_cli_argv("status"), [
                    str(executable.resolve()), "status"])
                self.assertEqual(_cli_cwd(root / "home"), root / "home")
                self.assertNotIn("AGREP_RS_BIN", env)
                self.assertNotIn("PYTHONPATH", env)
                self.assertNotIn("PYTHONHOME", env)
                self.assertNotIn("PYTHONPYCACHEPREFIX", env)
                self.assertNotIn("AGREP_RS_BIN", isolated)
                self.assertNotIn("PYTHONDONTWRITEBYTECODE", isolated)
                self.assertNotIn("CODEX_THREAD_ID", isolated)
                self.assertNotIn("CLAUDECODE", isolated)

    def test_source_mode_preserves_checkout_argv_native_and_bytecode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-source-seam-") as tmp:
            root = Path(tmp)
            release = root / "agrep-rs"
            release.write_text("source native\n", encoding="utf-8")
            with mock.patch(f"{__name__}._INSTALLED_CLI", None), \
                    mock.patch(f"{__name__}.RELEASE_BIN", release), \
                    mock.patch.object(
                        _IsolatedBlackBox, "home", root / "home",
                        create=True), \
                    mock.patch.object(
                        _IsolatedBlackBox, "data", root / "data",
                        create=True):
                env = _fixture_env(root / "home", root / "data",
                                   root / "protected")
                isolated = _IsolatedBlackBox._env()
                self.assertEqual(_cli_argv("status"), [
                    sys.executable, str(CLI), "status"])
                self.assertEqual(_cli_cwd(root / "home"), ROOT)
                self.assertEqual(env["AGREP_RS_BIN"], str(release))
                self.assertIn("PYTHONPYCACHEPREFIX", env)
                self.assertEqual(isolated["AGREP_RS_BIN"], str(release))
                self.assertEqual(isolated["PYTHONDONTWRITEBYTECODE"], "1")

    def test_warm_budget_uses_median_not_a_lucky_minimum(self) -> None:
        result = subprocess.CompletedProcess([], 0, "", "")
        samples = iter(((0.010, result), (0.240, result), (0.260, result)))
        with mock.patch(
                f"{__name__}._wall", side_effect=lambda _fn: next(samples)):
            seconds, observed = _median_wall(lambda: None)
        self.assertEqual(seconds, 0.240)
        self.assertIs(observed, result)

    def test_warm_budget_rejects_unstable_outcomes(self) -> None:
        samples = iter((
            (0.010, subprocess.CompletedProcess([], 0, "", "")),
            (0.011, subprocess.CompletedProcess([], 1, "", "")),
            (0.012, subprocess.CompletedProcess([], 0, "", "")),
        ))
        with mock.patch(
                f"{__name__}._wall", side_effect=lambda _fn: next(samples)):
            with self.assertRaisesRegex(
                    AssertionError, "unstable return codes"):
                _median_wall(lambda: None)

    def test_bulk_export_runs_after_bounded_command_budgets(self) -> None:
        standard = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromTestCase(case)
            for case in (
                PerfBudgets,
                FreshnessBudgets,
                FlatScaleBudget,
                BudgetHarnessNegativeControls,
                FreshnessDisclosureBlackBox,
                DisclosureCompositionBlackBox,
            )
        )
        suite = load_tests(unittest.defaultTestLoader, standard, None)
        self.assertEqual(
            list(suite)[-1].id().rsplit(".", 1)[-1],
            "test_flat_50k_rows_meet_exact_file_export_budget",
        )
        self.assertEqual(MAX_FLAT_EXPORT_SAMPLE_S, 10.000)

    def test_50k_stderr_allows_only_the_truthful_slow_profile(self) -> None:
        _validate_optional_slow_profile("", 1.0)
        _validate_optional_slow_profile("took 1.52s (rest)\n", 1.60)
        _validate_optional_slow_profile(
            "took 1.52s: imports 0.06s · freshen 0.05s · "
            "query[corpusdb] 0.11s · rest 1.30s\n", 1.60)
        for stderr in (
                "warning: fixture failed\n",
                "took 1.52s (rest)\nwarning: fixture failed\n",
                "took 1.52s: ERROR exactness skipped\n",
                "took 1.52s (fatal)\n",
                "took 1.52s (render)\n",
                "took 0.40s (rest)\n"):
            with self.subTest(stderr=stderr), self.assertRaises(AssertionError):
                _validate_optional_slow_profile(stderr, 1.60)

        forced = (
            "took 0.40s: imports 0.04s · freshen 0.03s · "
            "query[corpusdb] 0.25s · rest 0.08s\n")
        self.assertEqual(
            _validate_optional_slow_profile(forced, 0.42, forced=True),
            forced.rstrip())
        with self.assertRaisesRegex(AssertionError, "timing profile was missing"):
            _validate_optional_slow_profile("", 0.42, forced=True)

    def test_bulk_file_runner_bypasses_stdout_pipe_and_forces_timing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-bulk-sink-") as tmp:
            output = Path(tmp) / "rows.tsv"
            seen = {}

            def fake_run(argv, **kwargs):
                seen.update(kwargs)
                kwargs["stdout"].write(b"fixture-row\n")
                return subprocess.CompletedProcess(
                    argv, 0, None, "took 0.01s: rest 0.01s\n")

            with mock.patch(f"{__name__}._cli_argv", return_value=["agrep"]), \
                    mock.patch(f"{__name__}._cli_cwd", return_value=Path(tmp)), \
                    mock.patch(f"{__name__}._fixture_env", return_value={}), \
                    mock.patch(f"{__name__}.subprocess.run", side_effect=fake_run):
                elapsed, result = _timed_public_cli_to_file(
                    Path(tmp), Path(tmp), Path(tmp), output, "query")

            self.assertGreaterEqual(elapsed, 0.0)
            self.assertEqual(result.returncode, 0)
            self.assertIsNone(result.stdout)
            self.assertEqual(output.read_bytes(), b"fixture-row\n")
            self.assertTrue(seen["stdout"].closed)
            self.assertIs(seen["stderr"], subprocess.PIPE)
            self.assertEqual(seen["env"]["AGREP_TIMING"], "1")
            self.assertNotIn("capture_output", seen)

    def test_bulk_file_validation_rejects_truncation_and_bad_bytes(self) -> None:
        case = FlatScaleBudget(
            "test_flat_50k_rows_meet_exact_file_export_budget")
        result = subprocess.CompletedProcess(
            ["agrep"], 0, None, "took 0.01s (rest)\n")
        user = (
            f"{SCALE_SESSION}\tclaude\tflat50k-fixture\t0\tuser\t"
            "flat budget user 0 fiftyk lantern")
        agent = (
            f"{SCALE_SESSION}\tclaude\tflat50k-fixture\t0\tagent\t"
            "flat budget reply 0 fiftyk lantern")
        with tempfile.TemporaryDirectory(prefix="agrep-bulk-validate-") as tmp, \
                mock.patch(f"{__name__}.SCALE_ROWS", 2):
            output = Path(tmp) / "rows.tsv"
            output.write_bytes(f"{user}\r\n{agent}\r\n".encode())
            byte_count, _ = case._assert_flat_50k_file(output, result, 0.02)
            self.assertEqual(byte_count, output.stat().st_size)

            for payload, error in (
                    (user.encode(), AssertionError),
                    (f"{user}\n".encode(), AssertionError),
                    (b"\xff\n", UnicodeDecodeError)):
                output.write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(error):
                    case._assert_flat_50k_file(output, result, 0.02)


class _IsolatedBlackBox:
    """HOME/XDG-walled subprocess driver for the black-box suites."""

    home: object
    data: object

    @classmethod
    def _env(cls) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(_ENV_PREFIXES_TO_CLEAR)
               and k not in _ENV_KEYS_TO_CLEAR
               and k != "CLINE_DIR"}
        env.update({
            "HOME": str(cls.home), "USERPROFILE": str(cls.home),
            "XDG_CONFIG_HOME": str(cls.home / ".config"),
            "XDG_DATA_HOME": str(cls.home / ".local" / "share"),
            "AGREP_HOME": str(cls.home),
            "AGREP_DATA_DIR": str(cls.data),
            "AGREP_DATA_DIR_SOURCE": "env",
            "AGREP_NO_DAEMON": "1",
            "AGREP_NO_FETCH": "1",
            "PYTHONNOUSERSITE": "1",
            "RAYON_NUM_THREADS": "2",
        })
        if _INSTALLED_CLI is None:
            env["AGREP_RS_BIN"] = str(_require_release_binary())
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    @classmethod
    def _cli(cls, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            _cli_argv(*args), cwd=_cli_cwd(cls.home), env=cls._env(),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, check=False)


FRESH_SESSION = "b0d9e7f0-0000-4000-8000-freshstate00"


class FreshnessDisclosureBlackBox(_IsolatedBlackBox, unittest.TestCase):
    """The three-state freshness lines, driven through the public CLI only.

    green (nothing) / `index Nm behind (...)` / `history may be stale: ...`
    - asserted as a stranger sees them: fixture
    home, subprocess cli.py, no internal imports on the measured path. The
    fixture's filesystem (store mtimes, the verified-current record) is the
    environment under test; the assertions never reach into the product."""

    tmp: tempfile.TemporaryDirectory
    home: Path
    data: Path
    transcript: Path

    @classmethod
    def setUpClass(cls) -> None:
        if _INSTALLED_CLI is None and not RELEASE_BIN.is_file():
            raise unittest.SkipTest(f"release binary missing: {RELEASE_BIN}")
        cls.tmp = tempfile.TemporaryDirectory(prefix="agrep-fresh-")
        root = Path(cls.tmp.name)
        cls.home, cls.data = root / "home", root / "data"
        project = cls.home / ".claude" / "projects" / "fresh-fixture"
        project.mkdir(parents=True)
        rows = []
        for i in range(6):
            rows.append({
                "type": "user", "userType": "external",
                "sessionId": FRESH_SESSION,
                "timestamp": f"2026-07-25T13:{i:02d}:00.000Z",
                "cwd": str(root / "work" / "fresh-fixture"),
                "message": {"role": "user",
                            "content": f"freshness probe {i} beacon ember"},
            })
        cls.transcript = project / f"{FRESH_SESSION}.jsonl"
        cls.transcript.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
            encoding="utf-8")
        build = cls._cli("index")
        if build.returncode != 0:
            raise unittest.SkipTest(f"fixture ingest failed: {build.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()


    def _meta_freshness(self) -> dict:
        res = self._cli("beacon ember", "--json")
        for line in filter(None, res.stdout.splitlines()):
            row = json.loads(line)
            if isinstance(row, dict) and "freshness" in row:
                return row["freshness"]
        self.fail(f"no freshness meta row in --json output: {res.stdout[-400:]}")

    def test_three_state_disclosure_lines(self) -> None:
        # state 1 - current: an indexed, undrifted box says nothing at all
        res = self._cli("beacon ember", "--color", "never")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        merged = res.stdout + res.stderr
        self.assertNotIn("behind", merged)
        self.assertNotIn("may be stale", merged)
        fresh = self._meta_freshness()
        self.assertEqual(fresh["state"], "no-known-failure")

        # state 1b - the grace window: the CALLING session writing its own
        # transcript can never read "behind" - self-exclusion hides its rows
        # anyway. The exported identity is what the exemption keys on.
        caller = "b0d9e7f0-0000-4000-8000-freshcaller0"
        caller_transcript = self.transcript.with_name(f"{caller}.jsonl")
        caller_transcript.write_text(json.dumps({
            "type": "user", "userType": "external",
            "sessionId": caller,
            "timestamp": "2026-07-25T14:00:00.000Z",
            "message": {"role": "user", "content": "live burst zephyr"},
        }) + "\n", encoding="utf-8")
        caller_env = {**self._env(), "CLAUDE_CODE_SESSION_ID": caller}
        caller_env.pop("CODEX_THREAD_ID", None)
        res = subprocess.run(
            _cli_argv("beacon ember", "--color", "never"),
            cwd=_cli_cwd(self.home), env=caller_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False)
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        self.assertNotIn("behind", merged)
        self.assertNotIn("may be stale", merged)
        js = subprocess.run(
            _cli_argv("beacon ember", "--json"),
            cwd=_cli_cwd(self.home), env=caller_env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False)
        for line in filter(None, js.stdout.splitlines()):
            row = json.loads(line)
            if isinstance(row, dict) and "freshness" in row:
                self.assertEqual(row["freshness"]["state"], "index-behind")
                self.assertTrue(row["freshness"]["may_be_stale"])
                break
        else:
            self.fail(f"no freshness meta row: {js.stdout[-400:]}")

        # state 2 - behind: the same drift, aged past the debounce horizon
        # (a store write from an hour ago, a verification from two hours
        # ago), must disclose with its age - the negative control
        stale = time.time() - 3600
        os.utime(self.transcript, (stale, stale))
        os.utime(caller_transcript, (stale, stale))
        record_path = self.data / "verified-current.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["ts"] = time.time() - 7200
        record_path.write_text(json.dumps(record), encoding="utf-8")
        res = self._cli("beacon ember", "--color", "never")
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        self.assertIn("behind", merged)
        self.assertIn("store changed", merged)
        # AGREP_NO_DAEMON means nothing converges: promising "catching up"
        # here would be the law-7 inverse lie, so the remedy names the human
        self.assertNotIn("catching up", merged)
        self.assertIn("agrep index", merged)
        fresh = self._meta_freshness()
        self.assertEqual(fresh["state"], "index-behind")
        self.assertFalse(fresh["failing"])
        self.assertTrue(fresh["may_be_stale"])
        self.assertEqual(fresh["changed_stores"], 1)

        # state 3 - may-be-stale: drift cannot be judged (no census without
        # the ingest binary); the hedge line names the reason and the search
        # still serves the published snapshot
        missing = str(self.data / "missing-ingest-binary")
        env_override = {**self._env(), "AGREP_RS_BIN": missing}
        res = subprocess.run(
            _cli_argv("beacon ember", "--color", "never"),
            cwd=_cli_cwd(self.home), env=env_override, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False)
        merged = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        self.assertIn("may be stale", merged)
        self.assertIn("beacon", res.stdout)


DRIFT_SESSION = "b0d9e7f0-0000-4000-8000-composeddrift"


class DisclosureCompositionBlackBox(_IsolatedBlackBox, unittest.TestCase):
    """Goal 10 unification: one degraded search, at most ONE freshness line
    plus at most one lane line, agreeing surfaces - never the 2-5 stacked,
    contradicting stderr hedges the composition review found. Same stranger's
    posture as above: fixture home, subprocess cli.py, filesystem as input."""

    tmp: tempfile.TemporaryDirectory
    home: Path
    data: Path
    transcript: Path

    @classmethod
    def setUpClass(cls) -> None:
        if _INSTALLED_CLI is None and not RELEASE_BIN.is_file():
            raise unittest.SkipTest(f"release binary missing: {RELEASE_BIN}")
        cls.tmp = tempfile.TemporaryDirectory(prefix="agrep-compose-")
        root = Path(cls.tmp.name)
        cls.home, cls.data = root / "home", root / "data"
        project = cls.home / ".claude" / "projects" / "compose-fixture"
        project.mkdir(parents=True)
        rows = [{
            "type": "user", "userType": "external",
            "sessionId": DRIFT_SESSION,
            "timestamp": f"2026-07-25T13:{i:02d}:00.000Z",
            "cwd": str(root / "work" / "compose-fixture"),
            "message": {"role": "user",
                        "content": f"compose probe {i} cobalt anchor"},
        } for i in range(6)]
        cls.transcript = project / f"{DRIFT_SESSION}.jsonl"
        cls.transcript.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
            encoding="utf-8")
        build = cls._cli("index")
        if build.returncode != 0:
            raise unittest.SkipTest(f"fixture ingest failed: {build.stderr[-400:]}")
        # age the drift past the debounce horizon: a store write from an hour
        # ago against a verification from two hours ago is honestly "behind"
        stale = time.time() - 3600
        os.utime(cls.transcript, (stale, stale))
        record_path = cls.data / "verified-current.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["ts"] = time.time() - 7200
        record_path.write_text(json.dumps(record), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()


    @staticmethod
    def _freshness_lines(text: str) -> list[str]:
        return [l for l in text.splitlines()
                if "behind" in l or "may be stale" in l or "catching up" in l]

    @staticmethod
    def _lane_lines(text: str) -> list[str]:
        return [l for l in text.splitlines()
                if "scanning the published snapshot" in l
                or "serving the published" in l]

    def test_degraded_search_stacks_no_contradicting_lines(self) -> None:
        res = self._cli("cobalt anchor", "--color", "never")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        freshness = self._freshness_lines(res.stderr)
        self.assertEqual(
            len(freshness), 1, f"stacked freshness lines: {freshness}")
        self.assertIn("behind", freshness[0])
        self.assertLessEqual(len(self._lane_lines(res.stderr)), 1)
        # AGREP_NO_DAEMON: convergence must not be promised anywhere
        self.assertNotIn("catching up", res.stdout + res.stderr)

    def test_count_porcelain_carries_the_same_story_line(self) -> None:
        res = self._cli("cobalt anchor", "-c")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        # stdout stays machine-pure; the one story line rides stderr
        self.assertEqual(res.stdout.strip().splitlines()[-1], "6")
        freshness = self._freshness_lines(res.stderr)
        self.assertEqual(
            len(freshness), 1, f"stacked freshness lines: {freshness}")
        self.assertIn("behind", freshness[0])

    def test_around_json_surfaces_the_drifted_index(self) -> None:
        res = self._cli("around", DRIFT_SESSION, "1", "--json")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        meta = [json.loads(line) for line in res.stdout.splitlines()
                if line and "agrep-meta" in line and "freshness" in line]
        self.assertTrue(meta, "drifted index rendered as rc0-no-field")
        self.assertEqual(meta[0]["freshness"]["state"], "index-behind")
        self.assertTrue(meta[0]["freshness"]["may_be_stale"])

    def test_no_auto_hedges_unchecked_instead_of_guessing_drift(self) -> None:
        # the box IS drifted, but --no-auto runs no census: the only honest
        # line is the unchecked hedge, never a drift verdict
        res = self._cli("cobalt anchor", "--no-auto", "--color", "never")
        self.assertEqual(res.returncode, 0, res.stderr[-400:])
        freshness = self._freshness_lines(res.stderr)
        self.assertEqual(
            len(freshness), 1, f"stacked freshness lines: {freshness}")
        self.assertIn("may be stale", freshness[0])
        self.assertIn("--no-auto", freshness[0])
        self.assertNotIn("behind", freshness[0])


def _flatten_test_suite(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten_test_suite(test)
        else:
            yield test


def load_tests(_loader, tests, _pattern):
    # Bulk export runs last so it cannot contaminate bounded command latency.
    cases = list(_flatten_test_suite(tests))
    bounded = [test for test in cases
               if not isinstance(test, FlatScaleBudget)]
    bulk = [test for test in cases if isinstance(test, FlatScaleBudget)]
    return unittest.TestSuite([*bounded, *bulk])



if __name__ == "__main__":
    unittest.main()
