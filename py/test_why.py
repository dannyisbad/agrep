"""`agrep why` verdicts, black-box: fixture stores ingested by the real `cli.py index`."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import dist  # noqa: E402

FIXTURES = ROOT / "py" / "fixtures" / "why" / "store"
CLAUDE = "11111111-1111-4111-8111-111111111111"
CLAUDE_TWIN = "11111111-2222-4222-8222-222222222222"
PI_ALIAS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PI_HEADER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PI_NEW = "77777777-7777-4777-8777-777777777777"
OMP_ROOT = "33333333-3333-4333-8333-333333333333"
OMP_SIDE = "44444444-4444-4444-8444-444444444444"
OMP_CONTAINER = f"-work-beta/2000-02-03T04-05-06-000Z_{OMP_ROOT}"
VERDICT_EXIT = {"indexed": 0, "indexed-under-alias": 0, "indexed-as-side-chat": 0,
                "ambiguous": 2, "not-provable": 2}
NOT_SERVED = 97
RESIDENT_CLIENT = (
    f"import sys;sys.path[:]={[str(ROOT / 'py'), str(ROOT), *sys.path[1:]]!r};"
    f"import resident;sys.argv[0]={str(ROOT / 'cli.py')!r};"
    f"code=resident.try_run();sys.exit({NOT_SERVED} if code is None else code)"
)


def _rust_bin() -> Path:
    return Path(os.environ.get("AGREP_RS_BIN") or dist.ingest_bin())


def _dead_explorer_descriptor() -> str:
    """A `.server` record in the shape legacy_cleanup retires: its owner pid has exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return json.dumps({"pid": child.pid, "port": 1, "mode": "explorer",
                       "process_start": "unknown"}) + "\n"


def _native(relative: str) -> str:
    """A fixture-relative path spelled with this platform's separator."""
    return relative.replace("/", os.sep)


def _tilde(relative: str) -> str:
    """The way `why` displays a path under the sandbox home."""
    return "~" + os.sep + _native(relative)


def _pi_transcript(path: Path, session: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session", "id": session, "version": 3, "cwd": "/work/new",
                    "timestamp": "2000-05-01T00:00:00.000Z"}) + "\n"
        + json.dumps({"type": "message", "id": "n1", "parentId": None,
                      "timestamp": "2000-05-01T00:00:01.000Z",
                      "message": {"role": "user", "content": [{"type": "text", "text": text}],
                                  "timestamp": "2000-05-01T00:00:01.000Z"}}) + "\n",
        encoding="utf-8")


def _append_claude_turn(path: Path, home: Path, text: str, stamp: str, *,
                        session: str = CLAUDE, project: str = "cedar") -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "sessionId": session, "cwd": str(home / "projects" / project), "type": "user",
            "userType": "external", "timestamp": stamp,
            "message": {"role": "user", "content": text}}) + "\n")


def _processes_mentioning(marker: str) -> set[int]:
    listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=5, check=False)
    return {int(line.split(None, 1)[0]) for line in listing.stdout.splitlines()
            if marker in line and int(line.split(None, 1)[0]) != os.getpid()}


def _kill_processes_mentioning(marker: str) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _processes_mentioning(marker)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.2)


class Sandbox:
    """One fixture home plus data dir; nothing outside it is read or written."""

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="agrep-why-")
        self.root = Path(self._temp.name).resolve()
        self.home, self.data, self.tmp = self.root / "home", self.root / "data", self.root / "tmp"
        for path in (self.home, self.data, self.tmp, self.root / "callers"):
            path.mkdir()
        for source in sorted(FIXTURES.rglob("*.jsonl")):
            relative = source.relative_to(FIXTURES)
            target = self.home / ("." + relative.parts[0]) / Path(*relative.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._substitute_home(source.read_text(encoding="utf-8")),
                              encoding="utf-8")
        (self.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        self.env = {
            "HOME": str(self.home), "AGREP_HOME": str(self.home),
            "AGREP_DATA_DIR": str(self.data), "AGREP_DATA_DIR_SOURCE": "env",
            "TMPDIR": str(self.tmp), "PATH": "/usr/bin:/bin", "TZ": "UTC",
            "LANG": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1", "AGREP_NO_RESIDENT": "1",
            "AGREP_NO_FETCH": "1", "AGREP_RS_BIN": str(_rust_bin()),
            "AGREP_CALLER_PUBLICATION_DIR": str(self.root / "callers"),
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py"))),
        }
        if os.name == "nt":
            # CPython and the ingest binary need the system roots; store discovery still
            # resolves under AGREP_HOME, so the sandbox home stays the only one read.
            self.env.update({key: os.environ[key] for key in (
                "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH") if key in os.environ})
            self.env.update({"USERPROFILE": str(self.home), "TEMP": str(self.tmp),
                             "TMP": str(self.tmp)})

    def _substitute_home(self, text: str) -> str:
        """`{{home}}/a/b` becomes the native sandbox path, escaped for the JSON string it sits in."""
        def native(match: re.Match) -> str:
            parts = [part for part in match.group(1).split("/") if part]
            return json.dumps(str(self.home.joinpath(*parts)))[1:-1]
        return re.sub(r"\{\{home\}\}((?:/[^\"/\\]+)*)", native, text)

    def spawn(self, command: list[str], *, timeout: float = 60,
              env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(command, cwd=self.home, env=self.env if env is None else env,
                              input="", capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)

    def cli(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """The real entry point, the way `agrep ...` reaches `why`."""
        return self.spawn([sys.executable, str(ROOT / "cli.py"), *argv], env=env)

    def data_snapshot(self) -> dict:
        return {str(p.relative_to(self.data)): (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino)
                for p in self.data.rglob("*")}

    def index(self) -> None:
        result = self.spawn([sys.executable, str(ROOT / "cli.py"), "index"])
        if result.returncode:
            raise AssertionError(f"fixture indexing failed:\n{result.stdout}{result.stderr}")

    def rust_index(self) -> None:
        """The Rust ingest alone: transcripts publish, the search database lags."""
        result = self.spawn([str(_rust_bin()), "index", "--agent", "all"])
        if result.returncode:
            raise AssertionError(f"rust ingest failed:\n{result.stderr}")

    def why(self, *argv: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Every `why` run must leave the data dir byte-for-byte alone (mtime, size, inode)."""
        before = self.data_snapshot()
        result = subprocess.run([sys.executable, str(ROOT / "py" / "why.py"), *argv],
                                cwd=cwd or self.home, env=self.env if env is None else env,
                                input="", capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60, check=False)
        after = self.data_snapshot()
        if after != before:
            raise AssertionError(f"why {argv} wrote the data dir:\n{result.stdout}{result.stderr}")
        return result

    def why_json(self, *argv: str, env: dict | None = None, cwd: Path | None = None) -> tuple[dict, int]:
        result = self.why(*argv, "--json", env=env, cwd=cwd)
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise AssertionError(f"not one JSON object:\n{result.stdout}{result.stderr}") from exc
        return payload, result.returncode

    def without_daemon_guard(self) -> dict:
        """The sandbox env with AGREP_NO_DAEMON unset: `why` must still never wake indexd."""
        return {key: value for key, value in self.env.items() if key != "AGREP_NO_DAEMON"}

    def store(self, relative: str) -> Path:
        agent, rest = relative.split("/", 1)
        return self.home / ("." + agent) / rest

    def close(self) -> None:
        if os.name != "nt":
            _kill_processes_mentioning(str(self.data))
        for path in self.home.rglob("*"):
            try:
                path.chmod(0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        self._temp.cleanup()


class _VerdictAssertions(unittest.TestCase):
    sandbox: Sandbox

    def assert_verdict(self, verdict: str, *argv: str, next_action: object = None,
                       env: dict | None = None, cwd: Path | None = None) -> dict:
        payload, code = self.sandbox.why_json(*argv, env=env, cwd=cwd)
        self.assertEqual(payload.get("verdict"), verdict, payload)
        self.assertEqual(code, VERDICT_EXIT.get(verdict, 1), payload)
        self.assertEqual(payload["exit"], code)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["reference"], argv[0])
        self.assertEqual(payload.get("next_action"), next_action, payload)
        human = self.sandbox.why(*argv, env=env, cwd=cwd)
        self.assertEqual(human.returncode, code, human.stdout + human.stderr)
        self.assertTrue(human.stdout, f"human form printed nothing\n{human.stderr}")
        first, *rest = human.stdout.splitlines()
        self.assertEqual(first, payload["summary"])
        shown = [line for line in rest if line.startswith("  ")]
        self.assertTrue(1 <= len(shown) <= 4 + len(payload["candidates"]), human.stdout)
        if next_action:
            self.assertEqual(rest[-1], f"next: {next_action}")
        else:
            self.assertFalse(any(line.startswith("next:") for line in rest), human.stdout)
        return payload


class WhyReadOnlyVerdictTests(_VerdictAssertions):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = Sandbox()
        cls.sandbox.index()
        cls.snapshot = {p.name: p.stat().st_mtime_ns for p in cls.sandbox.data.iterdir()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    def tearDown(self) -> None:
        after = {p.name: p.stat().st_mtime_ns for p in self.sandbox.data.iterdir()}
        self.assertEqual(after, self.snapshot, "why mutated the data dir")

    def test_indexed_claude_chat_by_id_prefix_and_handle(self) -> None:
        for reference in (CLAUDE, "11111111-1111", f"@{CLAUDE}:0", "@11111111-1111:1"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                lines = payload["evidence"]["lines"]
                self.assertIn(f"sessions.jsonl: claude chat {CLAUDE}, 2 messages", lines[0])
                self.assertTrue(lines[1].startswith("corpus.db: "), lines)
                self.assertIn("session_sig present", lines[1])
                self.assertTrue(any(line.startswith("parse cache: " + _tilde(".claude/projects/"))
                                    for line in lines), lines)
                self.assertTrue(any("source unchanged since that parse" in line for line in lines))
                self.assertEqual(payload["evidence"]["corpus"]["session_sig"], True)

    def test_indexed_by_project_label_and_first_line_fragment(self) -> None:
        for reference in ("cedar", "copper lantern launch"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                self.assertEqual(payload["evidence"]["index_row"]["session"], CLAUDE)

    def test_pi_chat_whose_header_id_changed_is_indexed_under_alias(self) -> None:
        for reference in (PI_ALIAS, "aaaaaaaa", f"2000-01-02T03-04-05-000Z_{PI_ALIAS}.jsonl"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed-under-alias", reference)
                self.assertIn(PI_HEADER, payload["summary"])
                self.assertIn(f"alias {PI_ALIAS}", payload["evidence"]["lines"][0])
        payload = self.assert_verdict("indexed", PI_HEADER)
        self.assertEqual(payload["evidence"]["index_row"]["alias"], PI_ALIAS)

    def test_side_chat_names_its_parent(self) -> None:
        payload = self.assert_verdict("indexed-as-side-chat", "44444444")
        self.assertIn(OMP_ROOT, payload["summary"])
        self.assertEqual(payload["evidence"]["index_row"]["parent"], OMP_ROOT)
        self.assertEqual(payload["evidence"]["corpus"]["root"], OMP_ROOT)
        self.assertTrue(payload["evidence"]["corpus"]["side"])
        self.assertEqual(self.assert_verdict("indexed", OMP_ROOT)["evidence"]["corpus"]["side"], False)

    def test_sidecar_of_synthetic_mirrors_is_discovered_with_no_rows(self) -> None:
        path = self.sandbox.store(f"omp/agent/sessions/{OMP_CONTAINER}/mirror.jsonl")
        for reference in (str(path), "~" + str(path)[len(str(self.sandbox.home)):],
                          _native(f"{OMP_CONTAINER}/mirror.jsonl"), "mirror.jsonl"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("discovered-no-rows", reference)
                line = payload["evidence"]["lines"][0]
                self.assertIn("intake_stats.json: seen 3, rows 0, skips sidechain:2 unreferenced:1", line)
                self.assertEqual(payload["evidence"]["intake"][0]["skips"],
                                 {"sidechain": 2, "unreferenced": 1})

    def test_path_outside_every_store_is_not_discovered(self) -> None:
        elsewhere = self.sandbox.root / "elsewhere.jsonl"
        elsewhere.write_text("{}\n", encoding="utf-8")
        for reference in (str(elsewhere), str(self.sandbox.root / "missing.jsonl")):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("source-not-discovered", reference)
                lines = payload["evidence"]["lines"]
                self.assertTrue(lines[0].startswith("store census: 6 discovered file(s) across claude, pi"), lines)
                self.assertTrue(any(f"present here: claude ({_tilde('.claude/projects')}), "
                                    f"pi ({_tilde('.pi/agent/sessions')})" in line
                                    for line in lines), lines)
                self.assertEqual(payload["evidence"]["exists"], reference == str(elsewhere))
        under_root = self.sandbox.store("claude/projects/-projects-cedar/notes.txt")
        under_root.write_text("not a transcript\n", encoding="utf-8")
        payload = self.assert_verdict("source-not-discovered", str(under_root))
        self.assertIn(f"claude: the path sits under its store root {_tilde('.claude/projects')} but "
                      "is not a transcript claude parses", payload["evidence"]["lines"])
        copilot = self.sandbox.home / ".copilot" / "session-state" / "x" / "events.jsonl"
        copilot.parent.mkdir(parents=True)
        copilot.write_text("{}\n", encoding="utf-8")
        payload = self.assert_verdict("source-not-discovered", str(copilot))
        self.assertIn(f"copilot: detected-only store {_tilde('.copilot/session-state')}; agrep does "
                      "not index copilot yet", payload["evidence"]["lines"])

    def test_ambiguous_fragment_lists_candidates_without_guessing(self) -> None:
        payload = self.assert_verdict("ambiguous", "11111111")
        sessions = {c["session"] for c in payload["candidates"]}
        self.assertEqual(sessions, {CLAUDE, CLAUDE_TWIN})
        self.assertEqual(payload["evidence"]["lines"],
                         ["sessions.jsonl: 2 chats match '11111111'; pass a full id, a @handle from "
                          "a search hit, or the transcript path"])
        human = self.sandbox.why("11111111")
        self.assertEqual(human.returncode, 2)
        for session in sessions:
            self.assertIn(session, human.stdout)
        self.assertEqual(self.sandbox.why_json("lantern")[0]["verdict"], "ambiguous")

    def test_same_named_file_in_cwd_does_not_shadow_a_reference(self) -> None:
        """A bare word is a project label or id first; only path-shaped or transcript-named
        references are looked up as files in the working directory."""
        for name in ("cedar", "11111111-1111", "copper lantern launch"):
            (self.sandbox.home / name).write_text("not a transcript\n", encoding="utf-8")
        (self.sandbox.home / "stray.jsonl").write_text("{}\n", encoding="utf-8")
        try:
            for reference in ("cedar", "11111111-1111", "copper lantern launch"):
                with self.subTest(reference=reference):
                    payload = self.assert_verdict("indexed", reference)
                    self.assertEqual(payload["evidence"]["index_row"]["session"], CLAUDE)
            payload = self.assert_verdict("source-not-discovered", "stray.jsonl")
            self.assertEqual(payload["evidence"]["path"], str(self.sandbox.home / "stray.jsonl"))
            payload = self.assert_verdict("source-not-discovered", "./cedar")
            self.assertEqual(payload["evidence"]["exists"], True)
        finally:
            for name in ("cedar", "11111111-1111", "copper lantern launch", "stray.jsonl"):
                (self.sandbox.home / name).unlink()

    def test_unknown_reference_names_what_was_searched(self) -> None:
        payload = self.assert_verdict("source-not-discovered", "deadbeefcafe")
        self.assertEqual(payload["candidates"], [])
        self.assertIn("sessions.jsonl: 5 chats, none match by id, alias, project or first line",
                      payload["evidence"]["lines"])

    def test_usage_errors_exit_two(self) -> None:
        self.assertEqual(self.sandbox.why("x" * 5000).returncode, 2)
        self.assertEqual(self.sandbox.why("   ").returncode, 2)


class WhyCliReadOnlyTests(unittest.TestCase):
    """Through cli.py: `why` skips the legacy-explorer retirement other commands run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = Sandbox()
        cls.sandbox.index()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    def setUp(self) -> None:
        self.descriptor = self.sandbox.data / ".server"
        self.plant()

    def tearDown(self) -> None:
        if self.descriptor.exists():
            self.descriptor.unlink()

    def plant(self) -> None:
        self.record = _dead_explorer_descriptor()
        self.descriptor.write_text(self.record, encoding="utf-8")
        self.snapshot = self.sandbox.data_snapshot()

    def assert_untouched(self, result: subprocess.CompletedProcess) -> None:
        detail = result.stdout + result.stderr
        self.assertTrue(self.descriptor.exists(), f"why retired the descriptor\n{detail}")
        self.assertEqual(self.descriptor.read_text(encoding="utf-8"), self.record, detail)
        self.assertEqual(self.sandbox.data_snapshot(), self.snapshot, f"why wrote the data dir\n{detail}")

    def test_why_leaves_a_dead_explorer_descriptor_alone(self) -> None:
        for argv in ((CLAUDE,), (CLAUDE, "--json"), ("--help",)):
            with self.subTest(argv=argv):
                result = self.sandbox.cli("why", *argv)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_untouched(result)

    def test_other_commands_still_retire_the_descriptor(self) -> None:
        result = self.sandbox.cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.descriptor.exists(), "the control command left the descriptor")

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "fork"),
                         "the resident server needs POSIX fork and SCM_RIGHTS")
    def test_served_why_leaves_the_descriptor_alone(self) -> None:
        # Socket paths stop at 104 bytes, which a deep TMPDIR (macOS /var/folders) exceeds.
        runtime = Path(tempfile.mkdtemp(prefix="agw-", dir="/tmp"))
        env = {key: value for key, value in self.sandbox.env.items() if key != "AGREP_NO_RESIDENT"}
        env["XDG_RUNTIME_DIR"] = str(runtime)
        client = [sys.executable, "-c", RESIDENT_CLIENT]
        try:
            deadline = time.monotonic() + 30
            while True:
                warm = self.sandbox.spawn([*client, "--version"], env=env)
                if warm.returncode != NOT_SERVED:
                    break
                self.assertLess(time.monotonic(), deadline, "the resident never became ready")
                time.sleep(0.05)
            self.assertEqual(warm.returncode, 0, warm.stderr)
            # The warm-up command retires descriptors itself, so plant after it.
            self.plant()
            served = self.sandbox.spawn([*client, "why", CLAUDE], env=env)
            self.assertNotEqual(served.returncode, NOT_SERVED, f"not served\n{served.stderr}")
            self.assertEqual(served.returncode, 0, served.stdout + served.stderr)
            self.assertTrue(served.stdout.startswith("indexed: claude chat"), served.stdout)
            self.assert_untouched(served)
        finally:
            stop = self.sandbox.spawn(
                [sys.executable, "-c", "import json,resident;print(json.dumps(resident.stop_servers()))"],
                env=env)
            _kill_processes_mentioning(str(runtime))
            shutil.rmtree(runtime, ignore_errors=True)
        self.assertEqual(json.loads(stop.stdout).get("ok"), True, stop.stdout + stop.stderr)


class WhyMutationVerdictTests(_VerdictAssertions):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.sandbox.index()

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_source_appended_after_indexing(self) -> None:
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        self.assert_verdict("indexed", "11111111-1111")
        _append_claude_turn(path, self.sandbox.home, "Add a brand new question.",
                            "2000-01-20T12:04:00.000Z")
        payload = self.assert_verdict("written-after-last-index", "11111111-1111",
                                      next_action="agrep index")
        lines = payload["evidence"]["lines"]
        self.assertRegex(lines[1], r"^intake_stats\.json: " + re.escape(_tilde(".claude/projects/"))
                         + r".* parsed at .* \(s:\d+:\d+\), now .* \(s:\d+:\d+\)$")
        self.assertTrue(lines[2].startswith(".ingest.sig: last index published"), lines)
        self.assertEqual(payload["evidence"]["intake"][0]["fresh"], False)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["proof"], corpus["stamp_current"], corpus["current"]),
                         ("stamp", True, True), corpus)
        self.sandbox.index()
        self.assertEqual(self.assert_verdict("indexed", "11111111-1111")["evidence"]["index_row"]["session"], CLAUDE)
        self.assertEqual(self.sandbox.why_json("11111111-1111")[0]["evidence"]["intake"][0]["rows"], 3)

    def test_new_file_never_parsed_is_written_after_last_index(self) -> None:
        fresh = self.sandbox.store("claude/projects/-projects-cedar/99999999-9999-4999-8999-999999999999.jsonl")
        fresh.write_text(json.dumps({
            "sessionId": "99999999-9999-4999-8999-999999999999", "cwd": "/x", "type": "user",
            "userType": "external", "timestamp": "2000-03-01T00:00:00.000Z",
            "message": {"role": "user", "content": "never parsed"}}) + "\n", encoding="utf-8")
        payload = self.assert_verdict("written-after-last-index", "99999999",
                                      next_action="agrep index")
        self.assertEqual(payload["evidence"]["lines"][0],
                         "intake_stats.json: no record of this file, so no index has parsed it")

    def test_corpus_behind_transcripts(self) -> None:
        _pi_transcript(self.sandbox.store(f"pi/agent/sessions/-work-new/2000-05-01T00-00-00-000Z_{PI_NEW}.jsonl"),
                       PI_NEW, "fresh question")
        self.sandbox.rust_index()
        payload = self.assert_verdict("corpus-behind-transcripts", "77777777",
                                      next_action="agrep index")
        self.assertEqual(payload["evidence"]["corpus"], {
            "state": "ok", "served": "corpus.db", "scan_reason": None, "stamp_current": False,
            "rows": 0, "session_sig": False, "root": None, "side": None,
            "proof": "rows", "current": False, "published_rows": 1,
            "published": {"text": 1, "tool": 0}, "missing": {"text": 1, "tool": 0},
            "extra": {"text": 0, "tool": 0}, "concept_differs": False, "tools": "on"})
        self.assertIn("corpus.db: 0 rows, 1 row messages.jsonl publishes not stored, "
                      "stamp behind the published sources", payload["evidence"]["lines"])
        self.assertIn("the search database does not hold it", payload["summary"])
        self.sandbox.index()
        self.assert_verdict("indexed", "77777777")

    def test_deleted_transcript_is_still_served_from_the_lagging_search_database(self) -> None:
        """Between the ingest that drops a removed transcript and the next corpus refresh, search
        still serves the chat from corpus.db; `why` names those stale rows instead of nothing."""
        self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl").unlink()
        self.sandbox.rust_index()
        for reference in (CLAUDE, "11111111-1111"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("corpus-behind-transcripts", reference,
                                              next_action="agrep index")
                self.assertEqual(payload["summary"],
                                 "not current: the search database still holds 4 rows of claude "
                                 "chat 11111111 that messages.jsonl no longer publishes")
                self.assertIsNone(payload["evidence"]["index_row"])
                self.assertEqual(payload["evidence"]["stored_row"]["session"], CLAUDE)
                self.assertEqual(payload["evidence"]["stored_row"]["via"], "corpus.db")
                corpus = payload["evidence"]["corpus"]
                self.assertEqual((corpus["served"], corpus["rows"], corpus["published_rows"],
                                  corpus["extra"]), ("corpus.db", 4, 0, {"text": 4, "tool": 0}))
                self.assertEqual(payload["evidence"]["lines"][:2], [
                    f"sessions.jsonl: 4 chats, none with id {CLAUDE}",
                    "corpus.db: 4 rows, 4 stored rows messages.jsonl no longer publishes, "
                    f"stamp behind the published sources, family root {CLAUDE}"])
        search = self.sandbox.cli("search", "copper lantern", "--json")
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
        self.sandbox.index()
        payload = self.assert_verdict("source-not-discovered", CLAUDE)
        self.assertIn("corpus.db: every stored chat is listed in sessions.jsonl",
                      payload["evidence"]["lines"])
        search = self.sandbox.cli("search", "copper lantern", "--json")
        self.assertNotIn(CLAUDE, search.stdout, search.stdout + search.stderr)

    def test_deleted_transcript_resolves_by_project_and_first_line_like_resume(self) -> None:
        """The chats only corpus.db still holds answer to a project label or a first-line fragment
        the way `agrep resume` resolves them, with a listed chat always winning."""
        second = "55555555-5555-4555-8555-555555555555"
        _append_claude_turn(self.sandbox.store(f"claude/projects/-projects-cedar/{second}.jsonl"),
                            self.sandbox.home, "Second cedar question.", "2000-01-22T12:00:00.000Z",
                            session=second)
        self.sandbox.index()
        self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl").unlink()
        self.sandbox.rust_index()
        for reference in ("copper lantern", "Map the copper", "COPPER LANTERN LAUNCH"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("corpus-behind-transcripts", reference,
                                              next_action="agrep index")
                stored = payload["evidence"]["stored_row"]
                self.assertEqual((stored["session"], stored["project"], stored["via"]),
                                 (CLAUDE, "cedar", "corpus.db"), stored)
                self.assertEqual(stored["first_text"], "Map the copper lantern launch checklist for cedar.")
                self.assertIn("still holds 4 rows of claude chat 11111111", payload["summary"])
        payload = self.assert_verdict("source-not-discovered", "no such first line")
        self.assertIn("corpus.db: 1 stored chat sessions.jsonl no longer lists, none match by id, "
                      "project or first line", payload["evidence"]["lines"])
        # Listed chats win outright, as in resume; corpus.db answers only once none of them does.
        self.assertEqual(self.assert_verdict("indexed", "cedar")["evidence"]["index_row"]["session"], second)
        payload = self.assert_verdict("ambiguous", "lantern")
        self.assertEqual({c["session"] for c in payload["candidates"]}, {CLAUDE_TWIN, OMP_ROOT})
        self.assertTrue(payload["evidence"]["lines"][0].startswith("sessions.jsonl: 2 chats match"))
        self.sandbox.store(f"claude/projects/-projects-cedar/{second}.jsonl").unlink()
        self.sandbox.rust_index()
        payload = self.assert_verdict("ambiguous", "cedar")
        self.assertEqual({c["session"] for c in payload["candidates"]}, {CLAUDE, second})
        self.assertEqual(payload["evidence"]["lines"],
                         ["corpus.db: 2 chats match 'cedar'; pass a full id, a @handle from a search "
                          "hit, or the transcript path"])
        payload = self.assert_verdict("corpus-behind-transcripts", "second cedar", next_action="agrep index")
        self.assertEqual(payload["evidence"]["stored_row"]["session"], second)
        self.assertIn(CLAUDE, self.sandbox.cli("search", "copper lantern", "--json").stdout)

    def test_stored_chat_that_moves_between_the_two_reads_is_reported_not_crashed(self) -> None:
        """_judge_stored re-reads corpus.db after the stored lane picked a chat; a refresh that
        dropped it, or a writer's lock, in between is an honest no-match, never a traceback."""
        probe = ("import json, sys, why; row = {'session': sys.argv[1], 'agent': 'claude', "
                 "'via': 'corpus.db'}; print(json.dumps(why._judge_stored(why._Context(sys.argv[1]), row)))")
        before = self.sandbox.data_snapshot()
        gone = self.sandbox.spawn([sys.executable, "-c", probe, "66666666-6666-4666-8666-666666666666"])
        self.assertEqual(gone.returncode, 0, gone.stderr)
        payload = json.loads(gone.stdout)
        self.assertEqual((payload["verdict"], payload["exit"]), ("source-not-discovered", 1), payload)
        self.assertIn("corpus.db: 0 rows, session_sig absent", payload["evidence"]["lines"])
        holder = sqlite3.connect(str(self.sandbox.data / "corpus.db"), isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            busy = self.sandbox.spawn([sys.executable, "-c", probe, CLAUDE])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(busy.returncode, 0, busy.stderr)
        payload = json.loads(busy.stdout)
        self.assertEqual((payload["verdict"], payload["exit"]), ("source-not-discovered", 1), payload)
        self.assertIn("corpus.db: busy updating (database is locked); search scans messages.jsonl "
                      "directly (4 rows published for this chat)", payload["evidence"]["lines"])
        self.assertEqual(self.sandbox.data_snapshot(), before, "the probe wrote the data dir")

    def test_stored_first_line_skips_replies_and_recaps_like_sessions_jsonl(self) -> None:
        """A chat resumed after /compact opens with a recap and its assistant reply; sessions.jsonl
        takes the first real user line, and so must the stored lane once the file is gone."""
        session = "88888888-8888-4888-8888-888888888888"
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{session}.jsonl")
        cwd = str(self.sandbox.home / "projects" / "cedar")
        path.write_text("".join(json.dumps(row) + "\n" for row in (
            {"sessionId": session, "cwd": cwd, "type": "user", "userType": "external",
             "isCompactSummary": True, "timestamp": "2000-01-23T12:00:00.000Z",
             "message": {"role": "user", "content": "Summary of the walnut ledger so far."}},
            {"sessionId": session, "cwd": cwd, "type": "assistant", "timestamp": "2000-01-23T12:00:30.000Z",
             "message": {"role": "assistant", "model": "claude-sonnet-4",
                         "content": [{"type": "text", "text": "Understood, resuming the walnut ledger."}]}},
            {"sessionId": session, "cwd": cwd, "type": "user", "userType": "external",
             "timestamp": "2000-01-23T12:01:00.000Z",
             "message": {"role": "user", "content": "Now polish the hazel invoice template."}})),
            encoding="utf-8")
        self.sandbox.index()
        listed = self.assert_verdict("indexed", "hazel invoice")["evidence"]["index_row"]
        self.assertEqual((listed["session"], listed["first_text"]),
                         (session, "Now polish the hazel invoice template."))
        path.unlink()
        self.sandbox.rust_index()
        payload = self.assert_verdict("corpus-behind-transcripts", "hazel invoice", next_action="agrep index")
        self.assertEqual(payload["evidence"]["stored_row"]["first_text"], "Now polish the hazel invoice template.")
        self.assertEqual(payload["evidence"]["stored_row"]["session"], session)
        self.assertEqual(self.assert_verdict("source-not-discovered", "resuming the walnut")["verdict"],
                         "source-not-discovered")

    def test_stored_chat_without_a_first_line_still_resolves_by_id_and_project(self) -> None:
        """sessions.jsonl lists a recap-only chat with an empty first line and resume finds it by
        id or project; the stored lane keeps it as a candidate with first_text '' once it is gone."""
        session = "99999999-9999-4999-8999-999999999999"
        path = self.sandbox.store(f"claude/projects/-projects-oak/{session}.jsonl")
        path.parent.mkdir()
        path.write_text(json.dumps({
            "sessionId": session, "cwd": str(self.sandbox.home / "projects" / "oak"), "type": "user",
            "userType": "external", "isCompactSummary": True, "timestamp": "2000-01-24T12:00:00.000Z",
            "message": {"role": "user", "content": "Summary: the walnut ledger is balanced."}}) + "\n",
            encoding="utf-8")
        self.sandbox.index()
        self.assertEqual(self.assert_verdict("indexed", "oak")["evidence"]["index_row"]["first_text"], "")
        path.unlink()
        self.sandbox.rust_index()
        self.assertIn(session, self.sandbox.cli("search", "walnut ledger", "--json").stdout)
        for reference in (session, "99999999", "oak"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("corpus-behind-transcripts", reference, next_action="agrep index")
                stored = payload["evidence"]["stored_row"]
                self.assertEqual((stored["session"], stored["project"], stored["first_text"]),
                                 (session, "oak", ""), stored)

    def test_stored_side_chat_ids_match_exactly_and_by_prefix_like_resume(self) -> None:
        """Claude side chats are stored under their file stem (`agent-child1`), which is not
        uuid-shaped; resume matches such ids exactly and by prefix, so the stored lane must too."""
        child = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}/subagents/agent-child1.jsonl")
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({
            "sessionId": CLAUDE, "cwd": str(self.sandbox.home / "projects" / "cedar"), "type": "user",
            "userType": "external", "timestamp": "2000-01-20T12:03:00.000Z",
            "message": {"role": "user", "content": "Calibrate the spruce gauge."}}) + "\n",
            encoding="utf-8")
        self.sandbox.index()
        for reference in ("agent-child1", "agent-child"):
            with self.subTest(reference=reference, listed=True):
                self.assertEqual(self.assert_verdict("indexed-as-side-chat", reference)["evidence"]["index_row"]["session"],
                                 "agent-child1")
        child.unlink()
        self.sandbox.rust_index()
        self.assertIn("agent-child1", self.sandbox.cli("search", "spruce gauge", "--json").stdout)
        for reference in ("agent-child1", "agent-child", "spruce gauge"):
            with self.subTest(reference=reference, listed=False):
                payload = self.assert_verdict("corpus-behind-transcripts", reference, next_action="agrep index")
                self.assertEqual(payload["evidence"]["stored_row"]["session"], "agent-child1")
        payload = self.assert_verdict("source-not-discovered", "agent-nobody")
        self.assertIn("corpus.db: 1 stored chat sessions.jsonl no longer lists, none match by id, "
                      "project or first line", payload["evidence"]["lines"])

    def test_deleted_store_converges_through_the_lagging_search_database(self) -> None:
        """A whole store's removal drops its rows from sessions.jsonl on the second ingest; until
        the corpus refresh, search and `why` both still answer from corpus.db."""
        shutil.rmtree(self.sandbox.home / ".claude")
        self.sandbox.rust_index()
        self.assertIn(CLAUDE, self.sandbox.cli("search", "copper lantern", "--json").stdout)
        self.sandbox.rust_index()
        listed = {json.loads(line)["session"]
                  for line in (self.sandbox.data / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
                  if line.strip()}
        self.assertNotIn(CLAUDE, listed)
        payload = self.assert_verdict("corpus-behind-transcripts", CLAUDE, next_action="agrep index")
        self.assertIn("still holds 4 rows of claude chat 11111111 that messages.jsonl no longer publishes",
                      payload["summary"])
        self.assertIn(CLAUDE, self.sandbox.cli("search", "copper lantern", "--json").stdout)
        self.sandbox.index()
        self.assert_verdict("source-not-discovered", CLAUDE)
        self.assertNotIn(CLAUDE, self.sandbox.cli("search", "copper lantern", "--json").stdout)

    def test_locked_search_database_is_the_busy_direct_scan_lane(self) -> None:
        """A writer's exclusive lock is contention, not damage: search answers from messages.jsonl
        and discloses the busy index, and `why` judges from the same published rows."""
        holder = sqlite3.connect(str(self.sandbox.data / "corpus.db"), isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            payload = self.assert_verdict("indexed", CLAUDE)
            self.assertEqual(payload["evidence"]["corpus"], {
                "state": "busy", "served": "messages.jsonl",
                "scan_reason": "busy updating (database is locked)", "stamp_current": False,
                "proof": "scan", "current": True, "published_rows": 4})
            self.assertEqual(payload["evidence"]["lines"][1],
                             "corpus.db: busy updating (database is locked); search scans "
                             "messages.jsonl directly (4 rows published for this chat)")
            search = self.sandbox.cli("search", "copper lantern", "--json")
            self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
            self.assertIn("the search index is busy updating", search.stderr)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        corpus = self.assert_verdict("indexed", CLAUDE)["evidence"]["corpus"]
        self.assertEqual((corpus["state"], corpus["served"], corpus["proof"]), ("ok", "corpus.db", "stamp"))

    def test_protected_data_dir_takes_the_lane_search_takes(self) -> None:
        """Under AGREP_DATA_READONLY, connect() serves the published database as it stands: a
        queued build or a rebuild marker sends neither search nor `why` to the direct scan."""
        _pi_transcript(self.sandbox.store(f"pi/agent/sessions/-work-new/2000-05-01T00-00-00-000Z_{PI_NEW}.jsonl"),
                       PI_NEW, "zephyr quartz question")
        self.sandbox.rust_index()
        request = self.sandbox.data / ".search_index_request"
        request.write_text(json.dumps({"requested": time.time()}), encoding="utf-8")
        protected = dict(self.sandbox.env, AGREP_DATA_READONLY=str(self.sandbox.data))
        payload = self.assert_verdict("corpus-behind-transcripts", "77777777",
                                      next_action="agrep index", env=protected)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["served"], corpus["scan_reason"], corpus["proof"], corpus["rows"],
                          corpus["current"]), ("corpus.db", None, "rows", 0, False), corpus)
        self.assertIn("the search database does not hold it", payload["summary"])
        search = self.sandbox.cli("search", "zephyr quartz", "--json", env=protected)
        self.assertNotIn(PI_NEW, search.stdout, search.stdout + search.stderr)
        payload = self.assert_verdict("indexed", "77777777")
        self.assertEqual(payload["evidence"]["corpus"]["served"], "messages.jsonl")
        self.assertIn(PI_NEW, self.sandbox.cli("search", "zephyr quartz", "--json").stdout)
        request.unlink()

        marker = self.sandbox.spawn([sys.executable, "-c", (
            "import json, corpusdb; print(json.dumps({'version': 1, 'build_id': corpusdb._database_build_id()[1], "
            "'database_identity': list(corpusdb._sqlite_file_identity(corpusdb.DB_PATH))}))")])
        self.assertEqual(marker.returncode, 0, marker.stderr)
        (self.sandbox.data / ".corpusdb-rebuild").write_text(marker.stdout, encoding="utf-8")
        payload = self.assert_verdict("indexed", CLAUDE)
        self.assertEqual((payload["evidence"]["corpus"]["served"], payload["evidence"]["corpus"]["scan_reason"]),
                         ("messages.jsonl", "marked for rebuild after a query failure"))
        payload = self.assert_verdict("indexed", CLAUDE, env=protected)
        self.assertEqual((payload["evidence"]["corpus"]["served"], payload["evidence"]["corpus"]["proof"]),
                         ("corpus.db", "rows"))
        search = self.sandbox.cli("search", "copper lantern", "--json", env=protected)
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
        self.assertNotIn("busy updating", search.stderr)

    def test_native_only_ingest_leaves_corpus_behind(self) -> None:
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        before = self.assert_verdict("indexed", "11111111-1111")["evidence"]["corpus"]
        self.assertEqual((before["proof"], before["stamp_current"], before["current"]),
                         ("stamp", True, True), before)
        rows = before["rows"]
        _append_claude_turn(path, self.sandbox.home, "A turn the search database never saw.",
                            "2000-01-20T12:04:00.000Z")
        self.sandbox.rust_index()
        payload = self.assert_verdict("corpus-behind-transcripts", "11111111-1111",
                                      next_action="agrep index")
        corpus = payload["evidence"]["corpus"]
        self.assertEqual(corpus["rows"], rows)
        self.assertEqual(corpus["session_sig"], True)
        self.assertEqual((corpus["proof"], corpus["stamp_current"], corpus["current"]),
                         ("rows", False, False), corpus)
        self.assertEqual(corpus["published_rows"], rows + 1)
        self.assertEqual((corpus["missing"], corpus["extra"]),
                         ({"text": 1, "tool": 0}, {"text": 0, "tool": 0}), corpus)
        self.assertEqual(payload["summary"],
                         "not fully searchable: the search database holds an older copy of claude "
                         "chat 11111111 than messages.jsonl publishes (1 row not stored)")
        self.assertEqual(payload["evidence"]["lines"][1],
                         f"corpus.db: {rows} rows, 1 row messages.jsonl publishes not stored, "
                         f"stamp behind the published sources, family root {CLAUDE}")
        self.assertEqual(payload["evidence"]["intake"][0]["fresh"], True)
        # A transcript written after that native publication is the upstream verdict again.
        _append_claude_turn(path, self.sandbox.home, "And one the ingest never saw.",
                            "2000-01-20T12:05:00.000Z")
        payload = self.assert_verdict("written-after-last-index", "11111111-1111",
                                      next_action="agrep index")
        self.assertEqual(payload["evidence"]["corpus"]["current"], False)
        self.sandbox.index()
        after = self.assert_verdict("indexed", "11111111-1111")["evidence"]["corpus"]
        self.assertEqual((after["proof"], after["current"], after["rows"]),
                         ("stamp", True, rows + 2), after)

    def test_unchanged_chats_stay_indexed_while_another_lags(self) -> None:
        """With the stamp behind, an untouched chat is proven current by its own stored rows."""
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        _append_claude_turn(path, self.sandbox.home, "A turn the search database never saw.",
                            "2000-01-20T12:04:00.000Z")
        self.sandbox.rust_index()
        for verdict, reference in (("indexed", CLAUDE_TWIN), ("indexed-as-side-chat", "44444444"),
                                   ("indexed", OMP_ROOT)):
            with self.subTest(reference=reference):
                corpus = self.assert_verdict(verdict, reference)["evidence"]["corpus"]
                self.assertEqual((corpus["proof"], corpus["stamp_current"], corpus["current"]),
                                 ("rows", False, True), corpus)

    def test_direct_scan_states_agree_with_search(self) -> None:
        """Whenever the interactive reader serves the direct scan of messages.jsonl - no corpus.db,
        a queued first build, a stale db behind a queued rebuild, another schema - the chat is
        searchable, and `why` says so from the published rows without touching the data dir."""
        shutil.rmtree(self.sandbox.data)
        self.sandbox.data.mkdir()
        (self.sandbox.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        self.sandbox.rust_index()
        self.assertFalse((self.sandbox.data / "corpus.db").exists())
        env = self.sandbox.without_daemon_guard()
        payload = self.assert_verdict("indexed", CLAUDE, env=env)
        self.assertEqual(payload["evidence"]["lines"][1],
                         "corpus.db: missing; search scans messages.jsonl directly "
                         "(4 rows published for this chat)")
        self.assertEqual(payload["evidence"]["corpus"], {
            "state": "missing", "served": "messages.jsonl", "scan_reason": "missing",
            "stamp_current": False, "proof": "scan", "current": True, "published_rows": 4})
        request = self.sandbox.data / ".search_index_request"
        request.write_text(json.dumps({"requested": time.time()}), encoding="utf-8")
        payload = self.assert_verdict("indexed", CLAUDE, env=env)
        self.assertEqual(payload["evidence"]["corpus"]["scan_reason"], "not built yet, build queued")
        self.assertIn("search scans messages.jsonl directly", payload["evidence"]["lines"][1])
        request.unlink()
        search = self.sandbox.cli("search", "copper lantern", "--json")
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)

        self.sandbox.index()
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        _append_claude_turn(path, self.sandbox.home, "zephyr quartz marker", "2000-01-20T12:04:00.000Z")
        self.sandbox.rust_index()
        self.assert_verdict("corpus-behind-transcripts", CLAUDE, next_action="agrep index", env=env)
        request.write_text(json.dumps({"requested": time.time()}), encoding="utf-8")
        payload = self.assert_verdict("indexed", CLAUDE, env=env)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["served"], corpus["proof"], corpus["rows"], corpus["published_rows"]),
                         ("messages.jsonl", "scan", 4, 5), corpus)
        self.assertEqual(payload["evidence"]["lines"][1],
                         "corpus.db: 4 rows, stamp behind the published sources, rebuild queued; "
                         "search scans messages.jsonl directly (5 rows published for this chat)")
        search = self.sandbox.cli("search", "zephyr quartz", "--json")
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
        request.unlink(missing_ok=True)

        self.sandbox.index()
        db = sqlite3.connect(self.sandbox.data / "corpus.db")
        try:
            with db:
                db.execute("UPDATE meta SET value='14' WHERE key='schema'")
        finally:
            db.close()
        payload = self.assert_verdict("indexed", CLAUDE, env=env)
        self.assertEqual(payload["evidence"]["corpus"]["scan_reason"], "schema 14, this build reads 15")
        self.assertNotIn("rows", payload["evidence"]["corpus"])
        self.assertEqual(payload["evidence"]["lines"][1],
                         "corpus.db: schema 14, this build reads 15; search scans messages.jsonl "
                         "directly (5 rows published for this chat)")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "permission bits do not bind here")
    def test_unreadable_search_database_is_not_provable(self) -> None:
        corpus_db = self.sandbox.data / "corpus.db"
        corpus_db.chmod(0)
        try:
            payload = self.assert_verdict("not-provable", CLAUDE, next_action="agrep doctor")
        finally:
            corpus_db.chmod(0o600)
        self.assertEqual(payload["evidence"]["corpus"]["state"], "unreadable")
        self.assertTrue(payload["evidence"]["lines"][1].startswith("corpus.db: unreadable ("),
                        payload["evidence"]["lines"])
        self.assertIn("cannot be read", payload["summary"])
        self.assertNotIn("does not hold", payload["summary"])
        self.assert_verdict("indexed", CLAUDE)

    def test_concept_relabel_is_not_an_older_copy_but_a_tools_toggle_is(self) -> None:
        """A concept publication moves the stamp and every affected session_sig while the served
        text rows are unchanged; a tools toggle adds or removes tool rows for real."""
        (self.sandbox.data / "concepts.json").write_text(
            json.dumps([{"concept_id": 7, "name": "lantern launch"}]) + "\n", encoding="utf-8")
        (self.sandbox.data / "session_concepts.jsonl").write_text(
            json.dumps({"session": CLAUDE, "concept_id": 7}) + "\n", encoding="utf-8")
        payload = self.assert_verdict("indexed", CLAUDE)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["proof"], corpus["stamp_current"], corpus["current"],
                          corpus["concept_differs"]), ("rows", False, True, True), corpus)
        self.assertEqual(payload["evidence"]["lines"][1],
                         "corpus.db: 4 rows, stored rows match the 4 rows messages.jsonl publishes "
                         "(only the concept label from session_concepts.jsonl differs), "
                         f"stamp behind the published sources, family root {CLAUDE}")

        toggled = self.sandbox.cli("set", "tools", "off")
        self.assertEqual(toggled.returncode, 0, toggled.stdout + toggled.stderr)
        payload = self.assert_verdict("corpus-behind-transcripts", CLAUDE_TWIN, next_action="agrep index")
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["rows"], corpus["published_rows"], corpus["missing"], corpus["extra"]),
                         (3, 2, {"text": 0, "tool": 0}, {"text": 0, "tool": 1}), corpus)
        self.assertEqual(payload["summary"],
                         "not current: the search database still holds 1 row of claude chat 11111111 "
                         "that the event store (settings.json tools=off) no longer publishes")
        self.assertEqual(payload["evidence"]["lines"][1],
                         "corpus.db: 3 rows, 1 stored tool row the event store no longer publishes "
                         "(settings.json tools=off), stamp behind the published sources, "
                         f"family root {CLAUDE_TWIN}")
        self.sandbox.index()
        self.assertEqual(self.assert_verdict("indexed", CLAUDE_TWIN)["evidence"]["corpus"]["rows"], 2)
        toggled = self.sandbox.cli("set", "tools", "on")
        self.assertEqual(toggled.returncode, 0, toggled.stdout + toggled.stderr)
        payload = self.assert_verdict("corpus-behind-transcripts", CLAUDE_TWIN, next_action="agrep index")
        self.assertEqual(payload["evidence"]["corpus"]["missing"], {"text": 0, "tool": 1})
        self.assertEqual(payload["summary"],
                         "not fully searchable: the search database holds an older copy of claude chat "
                         "11111111 than the event store publishes (1 row not stored)")
        self.assertIn("1 tool row the event store publishes (settings.json tools=on) not stored",
                      payload["evidence"]["lines"][1])

    @unittest.skipIf(os.name == "nt", "symlinks need POSIX semantics")
    def test_symlinked_transcript_paths_reach_the_discovered_file(self) -> None:
        """A dotfile-managed store (`~/.claude -> <repo>/claude`) and a file-level link both name
        the discovered transcript once paths are resolved on both sides."""
        self.sandbox.close()
        self.sandbox = Sandbox()
        managed = self.sandbox.root / "dotfiles-claude"
        shutil.move(str(self.sandbox.home / ".claude"), str(managed))
        (self.sandbox.home / ".claude").symlink_to(managed)
        link = self.sandbox.home / "link.jsonl"
        link.symlink_to(managed / "projects" / "-projects-cedar" / f"{CLAUDE}.jsonl")
        self.sandbox.index()
        configured = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        for reference in (str(configured), str(managed / "projects" / "-projects-cedar" / f"{CLAUDE}.jsonl"),
                          str(link), "~/link.jsonl", "link.jsonl"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                self.assertEqual(payload["evidence"]["index_row"]["session"], CLAUDE)
                self.assertEqual(payload["evidence"]["sources"], [str(configured)])
        stray = managed / "projects" / "-projects-cedar" / "notes.txt"
        stray.write_text("not a transcript\n", encoding="utf-8")
        payload = self.assert_verdict("source-not-discovered", str(stray))
        self.assertIn(f"claude: the path sits under its store root {_tilde('.claude/projects')} but "
                      "is not a transcript claude parses", payload["evidence"]["lines"])

    def test_damaged_event_store_never_wakes_the_daemon(self) -> None:
        """A lagging corpus makes `why` fingerprint event payloads; a bad digest kicks no repair."""
        twin = self.sandbox.store(f"claude/projects/-projects-birch/{CLAUDE_TWIN}.jsonl")
        _append_claude_turn(twin, self.sandbox.home, "A turn the search database never saw.",
                            "2000-01-21T12:02:00.000Z", session=CLAUDE_TWIN, project="birch")
        self.sandbox.rust_index()
        db = sqlite3.connect(self.sandbox.data / "events" / ".store.sqlite3")
        try:
            with db:
                damaged = db.execute("UPDATE event_sessions SET digest=X'00' WHERE name LIKE ?",
                                     (f"%{CLAUDE_TWIN}%",)).rowcount
        finally:
            db.close()
        self.assertEqual(damaged, 1)
        env = {key: value for key, value in self.sandbox.env.items() if key != "AGREP_NO_DAEMON"}
        env["AGREP_INDEXD_IDLE_S"] = "2"
        snapshot = self.sandbox.data_snapshot()
        result = self.sandbox.cli("why", CLAUDE_TWIN, "--json", env=env)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["verdict"], result.returncode), ("not-provable", 2), payload)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["proof"], corpus["current"], corpus["stamp_current"]),
                         (None, None, False), corpus)
        self.assertIn("could not be compared", payload["summary"])
        self.assertIn("messages.jsonl unverifiable (", payload["evidence"]["lines"][1])
        self.assertEqual(self.sandbox.data_snapshot(), snapshot,
                         "why scheduled a repair or wrote the data dir")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "permission bits do not bind here")
    def test_unreadable_file_is_reported_from_durable_health(self) -> None:
        path = self.sandbox.store(f"pi/agent/sessions/-work-alias/2000-01-02T03-04-05-000Z_{PI_ALIAS}.jsonl")
        path.chmod(0)
        self.sandbox.index()
        for reference in (PI_ALIAS, PI_HEADER, str(path)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("source-unreadable", reference,
                                              next_action="make the file readable, then agrep index")
                self.assertEqual(payload["evidence"]["issue"]["durable"], True)
                self.assertEqual(payload["evidence"]["issue"]["path"], str(path))
                self.assertTrue(any(line.startswith(".source-health.json: source-read-failed on ")
                                    for line in payload["evidence"]["lines"]), payload)
        container = path.parent
        container.chmod(0)
        try:
            self.sandbox.index()
            payload = self.assert_verdict("source-unreadable", str(container / "never-seen.jsonl"),
                                          next_action="make the file readable, then agrep index")
            self.assertEqual(payload["evidence"]["issue"]["kind"], "permission-denied")
        finally:
            container.chmod(0o700)
        path.chmod(0o600)
        self.sandbox.index()
        self.assert_verdict("indexed-under-alias", PI_ALIAS)

    def test_missing_census_is_not_provable_but_index_proof_still_answers(self) -> None:
        broken = self.sandbox.root / "no-such-agrep-rs"
        self.sandbox.env["AGREP_RS_BIN"] = str(broken)
        payload = self.assert_verdict("not-provable", "deadbeefcafe")
        self.assertIn("store census: unavailable (ingest binary is unavailable at ",
                      payload["evidence"]["lines"][1])
        payload = self.assert_verdict("indexed", "11111111-1111")
        self.assertTrue(any("freshness unverified" in line for line in payload["evidence"]["lines"]))


if __name__ == "__main__":
    unittest.main()
