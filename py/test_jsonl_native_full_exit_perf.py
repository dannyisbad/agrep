"""Full-exit gate for one native attempt plus the exact JSONL fallback."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"
EVENTS = 20_000
SESSIONS = 100
PAGE = 40
FRONTIER_INTERIOR = 520
FRONTIER_EXACT_INDEX = EVENTS - 6
PAYLOAD_PADDING = "x" * 768
MIN_STORE_BYTES = 12 * 1024 * 1024
EXIT_BUDGET_MS = 2_500.0 if sys.platform == "win32" else 1_200.0
ENGINE_LINE = re.compile(
    r"search done: (?P<total>\d+) hit\(s\) in (?P<chats>\d+) chat\(s\) "
    r"via (?P<engine>[^;]+); showing (?P<shown>\d+)")

_ENV_PREFIXES_TO_CLEAR = (
    "AGREP_", "CLAUDE", "CODEX_", "CURSOR_", "GEMINI_", "OPENCODE",
    "CLINE_", "CRUSH_",
)
_ENV_KEYS_TO_CLEAR = {
    "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "PYTHONDONTWRITEBYTECODE", "PYTHONHOME", "PYTHONPATH",
    "PYTHONPYCACHEPREFIX", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
}


def _release_binary() -> Path | None:
    name = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
    candidates = [
        Path(os.environ["AGREP_RS_BIN"]) if os.environ.get("AGREP_RS_BIN") else None,
        ROOT / "target" / "release" / name,
        ROOT / "_bin" / name,
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def _installed_cli() -> Path | None:
    raw = os.environ.get("AGREP_PERF_CLI")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(
            f"AGREP_PERF_CLI must name an absolute installed launcher: {path}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"AGREP_PERF_CLI installed launcher is unavailable: {path}") from exc
    checkout = ROOT.resolve()
    for candidate in (Path(os.path.abspath(path)), resolved):
        try:
            candidate.relative_to(checkout)
        except ValueError:
            continue
        raise RuntimeError(
            "AGREP_PERF_CLI installed launcher must be outside the checkout")
    if not resolved.is_file():
        raise RuntimeError(
            f"AGREP_PERF_CLI does not name an installed launcher: {resolved}")
    if os.name == "nt" and resolved.suffix.lower() != ".exe":
        raise RuntimeError("AGREP_PERF_CLI installed launcher must be an .exe")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise RuntimeError(
            f"AGREP_PERF_CLI installed launcher is not executable: {resolved}")
    return resolved


def _private_env(
        home: Path, data: Path, binary: Path | None) -> dict[str, str]:
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
        "AGREP_DATA_DIR": str(data), "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_NO_DAEMON": "1", "AGREP_NO_FETCH": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        "RAYON_NUM_THREADS": "2",
    })
    if binary is not None:
        env["AGREP_RS_BIN"] = str(binary)
    return env


def _event_text(index: int) -> str:
    if index < FRONTIER_INTERIOR:
        text = f"the xjsonlfrontierx ordinary candidate {index:05d}"
    elif index == FRONTIER_EXACT_INDEX:
        text = "the jsonlfrontier exact candidate"
    elif index == EVENTS - 1:
        text = "the selective needle lives in the final event"
    elif index == EVENTS - 2:
        text = "the web explorer removed tilt ui exact phrase"
    elif index == EVENTS - 3:
        text = "the web x explorer x removed x tilt x ui scattered terms"
    elif index == EVENTS - 4:
        text = "the Unicode compatibility spelling is ıſſue"
    elif index == EVENTS - 5:
        text = "the xjsonlrankx newest interior candidate"
    elif index == EVENTS // 2:
        text = "the jsonlrank aligned older candidate"
    else:
        text = f"the ordinary fixture event {index:05d}"
    return f"{text} {PAYLOAD_PADDING}"


def _write_fixture(home: Path) -> None:
    project = home / ".claude" / "projects" / "jsonl-native-perf"
    project.mkdir(parents=True)
    base = datetime.now(timezone.utc) - timedelta(minutes=1)
    per_session = EVENTS // SESSIONS
    for session_index in range(SESSIONS):
        session = f"jsonl-perf-{session_index:04d}"
        cwd = "/fixtures" if session_index == SESSIONS - 1 else "/product"
        records = [json.dumps({
            "type": "user", "userType": "external", "sessionId": session,
            "timestamp": base.isoformat().replace("+00:00", "Z"),
            "cwd": cwd,
            "message": {"role": "user", "content": "fixture owner row"},
        }, separators=(",", ":"))]
        for local_index in range(per_session):
            index = session_index * per_session + local_index
            call_id = f"toolu_jsonl_perf_{index:05d}"
            stamp = (base + timedelta(milliseconds=index * 2 + 1)) \
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            done = (base + timedelta(milliseconds=index * 2 + 2)) \
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            records.append(json.dumps({
                "type": "assistant", "sessionId": session, "timestamp": stamp,
                "cwd": cwd,
                "message": {"role": "assistant", "model": "claude-perf",
                            "content": [{"type": "tool_use", "id": call_id,
                                         "name": "Read",
                                         "input": {"note": _event_text(index)}}]},
            }, separators=(",", ":")))
            records.append(json.dumps({
                "type": "user", "userType": "external", "sessionId": session,
                "timestamp": done, "cwd": cwd,
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call_id,
                    "content": "fixture result", "is_error": False,
                }]},
            }, separators=(",", ":")))
        (project / f"{session}.jsonl").write_text(
            "\n".join(records) + "\n", encoding="utf-8", newline="\n")


def _identity(row: dict) -> tuple:
    return tuple(row.get(field) for field in (
        "session", "turn", "ts", "who", "call_id", "name", "matched", "snippet"))


class JsonlNativeFullExitPerf(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setup_started = time.perf_counter()
        installed_cli = _installed_cli()
        binary = None if installed_cli is not None else _release_binary()
        if installed_cli is None and binary is None:
            raise unittest.SkipTest("release agrep-rs binary is unavailable")
        cls._tmp = tempfile.TemporaryDirectory(prefix="agrep-jsonl-native-perf-")
        root = Path(cls._tmp.name)
        cls.home, cls.data = root / "home", root / "data"
        cls.command = (
            [str(installed_cli)] if installed_cli is not None
            else [sys.executable, str(CLI)])
        cls.command_cwd = cls.home if installed_cli is not None else ROOT
        cls.data.mkdir()
        (cls.data / "settings.json").write_text(
            '{"embeddings":"off"}\n', encoding="utf-8", newline="\n")
        _write_fixture(cls.home)
        cls.env = _private_env(cls.home, cls.data, binary)
        index_command = (
            [*cls.command, "index", "--full"]
            if installed_cli is not None
            else [str(binary), "index", "--agent", "claude", "--full"])
        indexed = subprocess.run(
            index_command,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=cls.env, cwd=cls.command_cwd, timeout=120)
        if indexed.returncode != 0:
            raise AssertionError(
                f"fixture ingest rc={indexed.returncode}: {indexed.stderr[-1000:]}")
        proofs = list(cls.data.glob(".events_complete.*.json"))
        if not proofs or any(json.loads(path.read_text())["version"] != 10
                             for path in proofs):
            raise AssertionError("fixture ingest did not publish v10 event proofs")
        store = cls.data / "events" / ".store.sqlite3"
        if store.stat().st_size < MIN_STORE_BYTES:
            raise AssertionError(
                f"fixture event store is only {store.stat().st_size} bytes")
        with contextlib.closing(sqlite3.connect(
                store.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            published = db.execute(
                "SELECT COALESCE(sum(n_events),0) FROM event_sessions").fetchone()[0]
        if published != EVENTS:
            raise AssertionError(f"fixture published {published} events, expected {EVENTS}")
        for suffix in ("", "-wal", "-shm", "-journal"):
            (cls.data / f"corpus.db{suffix}").unlink(missing_ok=True)
        cls.setup_ms = (time.perf_counter() - setup_started) * 1000

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _native_cli(
            self, query: str, page: int = PAGE,
    ) -> tuple[dict, list[dict], subprocess.CompletedProcess, float]:
        env = {**self.env, "AGREP_DEBUG": "1", "AGREP_TIMING": "1"}
        started = time.perf_counter()
        run = subprocess.run(
            [*self.command, query, "--json", "--no-auto",
             "--lexical", "--self", "--who", "tool", "-n", str(page)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=self.command_cwd, timeout=15)
        wall_ms = (time.perf_counter() - started) * 1000
        records = [
            json.loads(line) for line in run.stdout.splitlines() if line.strip()
        ]
        if not records or records[0].get("kind") != "agrep-meta":
            raise AssertionError("search JSON did not lead with agrep-meta")
        return records[0], records[1:], run, wall_ms

    def _python_reference(self, query: str, page: int = PAGE) -> dict:
        code = r'''import json
from unittest import mock
import corpusdb
import explore
import search
with mock.patch.object(explore, "native_event_scan_preflight", return_value=False), \
     mock.patch.object(corpusdb, "connect", return_value=None), \
     mock.patch.object(corpusdb, "_trigram_ok", return_value=False):
    result = search.run_query(QUERY, limit=PAGE, who="tool", exact_totals=True)
print(json.dumps({
    "total": result["total"], "chats": result["chats"],
    "tool_hits": result["tool_hits"],
    "rows": [[hit.get(field) for field in FIELDS] for hit in result["hits"]],
}, ensure_ascii=False))'''
        fields = [
            "session", "turn", "ts", "who", "call_id", "name", "matched", "snippet",
        ]
        bootstrap = (
            f"QUERY={query!r}\nPAGE={page}\nFIELDS={fields!r}\n" + code)
        env = {**self.env, "AGREP_RS_BIN": str(self.data / "missing-agrep-rs")}
        run = subprocess.run(
            [sys.executable, "-c", bootstrap], cwd=ROOT / "py",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        return json.loads(run.stdout)

    def test_20k_broad_query_exits_under_budget_with_exact_python_parity(self) -> None:
        meta, rows, run, wall_ms = self._native_cli("the")
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        match = ENGINE_LINE.search(run.stderr)
        self.assertIsNotNone(match, run.stderr[-2000:])
        self.assertEqual(match.group("engine"), "jsonl+native-events")
        self.assertEqual(int(match.group("total")), EVENTS)
        self.assertEqual(int(match.group("chats")), SESSIONS)
        self.assertEqual(int(match.group("shown")), PAGE)
        self.assertEqual(len(rows), PAGE)
        completeness = meta["completeness"]
        self.assertEqual(completeness["total"], EVENTS)
        self.assertEqual(completeness["total_basis"], "exact")
        reference = self._python_reference("the")
        self.assertEqual(
            (reference["total"], reference["chats"], reference["tool_hits"]),
            (EVENTS, SESSIONS, EVENTS))
        self.assertEqual([list(_identity(row)) for row in rows], reference["rows"])
        timing_lines = [
            line for line in run.stderr.splitlines()
            if "native event scan:" in line or "search done:" in line
            or line.startswith("took ")
        ]
        self.assertEqual(
            sum("native event scan:" in line for line in timing_lines), 1)
        self.assertNotIn("using exact JSONL scan", run.stderr)
        self.assertLess(
            wall_ms, EXIT_BUDGET_MS,
            f"20k JSONL full exit took {wall_ms:.1f}ms "
            f"(budget {EXIT_BUDGET_MS:.0f}ms): {'; '.join(timing_lines)}")
        print(
            f"20k JSONL fixture setup={self.setup_ms:.1f}ms "
            f"full_exit={wall_ms:.1f}ms budget={EXIT_BUDGET_MS:.0f}ms")
        print("\n".join(timing_lines))

    def test_limit_one_matches_exhaustive_boundary_ranking(self) -> None:
        _meta, rows, run, _wall_ms = self._native_cli("jsonlrank", page=1)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertEqual(len(rows), 1)
        reference = self._python_reference("jsonlrank", page=1)
        self.assertEqual([list(_identity(row)) for row in rows], reference["rows"])

    def test_omitted_project_named_fixture_cannot_hide_the_true_winner(self) -> None:
        meta, rows, run, _wall_ms = self._native_cli("jsonlfrontier", page=1)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertEqual(meta["engine"], "jsonl+native-events")
        self.assertNotIn("using exact JSONL scan", run.stderr)
        self.assertEqual(len(rows), 1)
        reference = self._python_reference("jsonlfrontier", page=1)
        self.assertEqual([list(_identity(row)) for row in rows], reference["rows"])
        self.assertEqual(Path(rows[0]["project"]).name, "fixtures")


class InstalledCliSelectionTests(unittest.TestCase):
    def test_explicit_installed_cli_never_falls_back_to_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {"AGREP_PERF_CLI": str(Path(raw) / "missing")},
                clear=False):
            with self.assertRaisesRegex(RuntimeError, "installed launcher"):
                _installed_cli()

    def test_installed_cli_must_be_absolute_and_outside_checkout(self) -> None:
        with mock.patch.dict(
                os.environ, {"AGREP_PERF_CLI": "relative/agrep"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "absolute installed"):
                _installed_cli()
        with mock.patch.dict(
                os.environ, {"AGREP_PERF_CLI": str(CLI)}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "outside the checkout"):
                _installed_cli()

    def test_installed_cli_env_cannot_override_its_packaged_binary(self) -> None:
        polluted = {
            "AGREP_RS_BIN": "checkout-binary",
            "AGREP_NO_NATIVE": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONHOME": str(ROOT / "fake-python"),
            "CLAUDECODE": "1",
        }
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, polluted, clear=False):
            root = Path(raw)
            env = _private_env(root / "home", root / "data", None)
        for key in polluted:
            self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()
