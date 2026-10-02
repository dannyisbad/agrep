"""`agrep why` verdicts, black-box: fixture stores ingested by the real `cli.py index`."""

from __future__ import annotations

import json
import os
from pathlib import Path
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
    child = subprocess.Popen(["/bin/sh", "-c", ":"], env={"PATH": "/usr/bin:/bin"})
    child.wait(timeout=10)
    return json.dumps({"pid": child.pid, "port": 1, "mode": "explorer",
                       "process_start": "unknown"}) + "\n"


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
            target.write_text(source.read_text(encoding="utf-8").replace("{{home}}", str(self.home)),
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

    def spawn(self, command: list[str], *, timeout: float = 60,
              env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(command, cwd=self.home, env=self.env if env is None else env,
                              input="", capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)

    def cli(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """The real entry point, the way `agrep ...` reaches `why`."""
        return self.spawn([sys.executable, str(ROOT / "cli.py"), *argv], env=env)

    def data_snapshot(self) -> dict:
        return {str(p.relative_to(self.data)): (p.stat().st_mtime_ns, p.stat().st_size)
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

    def why(self, *argv: str) -> subprocess.CompletedProcess:
        return self.spawn([sys.executable, str(ROOT / "py" / "why.py"), *argv])

    def why_json(self, *argv: str) -> tuple[dict, int]:
        result = self.why(*argv, "--json")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise AssertionError(f"not one JSON object:\n{result.stdout}{result.stderr}") from exc
        return payload, result.returncode

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

    def assert_verdict(self, verdict: str, *argv: str, next_action: object = None) -> dict:
        payload, code = self.sandbox.why_json(*argv)
        self.assertEqual(payload.get("verdict"), verdict, payload)
        self.assertEqual(code, VERDICT_EXIT.get(verdict, 1), payload)
        self.assertEqual(payload["exit"], code)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["reference"], argv[0])
        self.assertEqual(payload.get("next_action"), next_action, payload)
        human = self.sandbox.why(*argv)
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
                self.assertTrue(any(line.startswith("parse cache: ~/.claude/projects/")
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
                          f"{OMP_CONTAINER}/mirror.jsonl", "mirror.jsonl"):
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
                self.assertTrue(any("present here: claude (~/.claude/projects), pi (~/.pi/agent/sessions)" in line
                                    for line in lines), lines)
                self.assertEqual(payload["evidence"]["exists"], reference == str(elsewhere))
        under_root = self.sandbox.store("claude/projects/-projects-cedar/notes.txt")
        under_root.write_text("not a transcript\n", encoding="utf-8")
        payload = self.assert_verdict("source-not-discovered", str(under_root))
        self.assertIn("claude: the path sits under its store root ~/.claude/projects but is not a "
                      "transcript claude parses", payload["evidence"]["lines"])
        copilot = self.sandbox.home / ".copilot" / "session-state" / "x" / "events.jsonl"
        copilot.parent.mkdir(parents=True)
        copilot.write_text("{}\n", encoding="utf-8")
        payload = self.assert_verdict("source-not-discovered", str(copilot))
        self.assertIn("copilot: detected-only store ~/.copilot/session-state; agrep does not index copilot yet",
                      payload["evidence"]["lines"])

    def test_ambiguous_fragment_lists_candidates_without_guessing(self) -> None:
        payload = self.assert_verdict("ambiguous", "11111111")
        sessions = {c["session"] for c in payload["candidates"]}
        self.assertEqual(sessions, {CLAUDE, CLAUDE_TWIN})
        human = self.sandbox.why("11111111")
        self.assertEqual(human.returncode, 2)
        for session in sessions:
            self.assertIn(session, human.stdout)
        self.assertEqual(self.sandbox.why_json("lantern")[0]["verdict"], "ambiguous")

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
        self.assertRegex(lines[1], r"^intake_stats\.json: ~/.claude/projects/.* parsed at .* \(s:\d+:\d+\), now .* \(s:\d+:\d+\)$")
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
        fresh = self.sandbox.store("pi/agent/sessions/-work-new/2000-05-01T00-00-00-000Z_77777777-7777-4777-8777-777777777777.jsonl")
        fresh.parent.mkdir(parents=True)
        fresh.write_text(
            '{"type":"session","id":"77777777-7777-4777-8777-777777777777","version":3,"cwd":"/work/new","timestamp":"2000-05-01T00:00:00.000Z"}\n'
            '{"type":"message","id":"n1","parentId":null,"timestamp":"2000-05-01T00:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"fresh question"}],"timestamp":"2000-05-01T00:00:01.000Z"}}\n',
            encoding="utf-8")
        self.sandbox.rust_index()
        payload = self.assert_verdict("corpus-behind-transcripts", "77777777",
                                      next_action="agrep index")
        self.assertEqual(payload["evidence"]["corpus"], {
            "state": "ok", "rows": 0, "session_sig": False, "root": None, "side": None,
            "stamp_current": False, "proof": "session_sig", "current": False, "published_rows": 1})
        self.assertIn("corpus.db: 0 rows, session_sig absent, messages.jsonl publishes 1 row, "
                      "stamp behind the published sources", payload["evidence"]["lines"])
        self.sandbox.index()
        self.assert_verdict("indexed", "77777777")

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
                         ("session_sig", False, False), corpus)
        self.assertEqual(corpus["published_rows"], rows + 1)
        self.assertIn("session_sig differs", payload["summary"])
        self.assertEqual(payload["evidence"]["lines"][1],
                         f"corpus.db: {rows} rows, session_sig differs from the {rows + 1} rows "
                         "messages.jsonl publishes, stamp behind the published sources, "
                         f"family root {CLAUDE}")
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
        """With the stamp behind, an untouched chat is proven current by its own session_sig."""
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        _append_claude_turn(path, self.sandbox.home, "A turn the search database never saw.",
                            "2000-01-20T12:04:00.000Z")
        self.sandbox.rust_index()
        for verdict, reference in (("indexed", CLAUDE_TWIN), ("indexed-as-side-chat", "44444444"),
                                   ("indexed", OMP_ROOT)):
            with self.subTest(reference=reference):
                corpus = self.assert_verdict(verdict, reference)["evidence"]["corpus"]
                self.assertEqual((corpus["proof"], corpus["stamp_current"], corpus["current"]),
                                 ("session_sig", False, True), corpus)

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
