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
WHOLE_STORE_SESSIONS = (
    ("kimi", "55555555-5555-4555-8555-555555555555"),
    ("cline", "1767348000000"),
    ("antigravity", "66666666-6666-4666-8666-666666666666"),
)
KIMI_NEW = "88888888-8888-4888-8888-888888888888"
KIMI_CHILD = "99999999-9999-4999-8999-999999999999"
KIMI_NESTED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CLINE_NEW = "1767349000000"
GEMINI = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
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


def _dead_explorer_descriptor() -> tuple[str, subprocess.Popen[bytes]]:
    """A `.server` record in the shape legacy_cleanup retires, and its exited owner.

    Keep the owner referenced: its open handle stops Windows handing the pid to the next process."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return json.dumps({"pid": child.pid, "port": 1, "mode": "explorer",
                       "process_start": "unknown"}) + "\n", child


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


_CRUSH_SCHEMA = """
CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT,
                      updated_at INTEGER, created_at INTEGER);
CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT, model TEXT,
                      created_at INTEGER, updated_at INTEGER);
"""
_OPENCODE_SCHEMA = """
CREATE TABLE session_v2(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
                        directory TEXT NOT NULL, title TEXT, model TEXT, agent TEXT,
                        time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
                             seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
                             time_updated INTEGER NOT NULL, data TEXT NOT NULL,
                             UNIQUE(session_id, seq));
"""


def _crush_add(path: Path, session: str, at: int, text: str, *, new_session: bool = True) -> None:
    """One user turn in a crush store; a new session row or a bump of an existing one's updated_at."""
    db = sqlite3.connect(str(path))
    try:
        with db:
            if new_session:
                db.execute("INSERT INTO sessions VALUES (?, '', ?, ?, ?)", (session, text[:10], at, at))
            else:
                db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (at, session))
            db.execute("INSERT INTO messages VALUES (?, ?, 'user', ?, '', ?, ?)",
                       (f"{session}-{at}", session, json.dumps([{"type": "text", "data": {"text": text}}]),
                        at, at))
    finally:
        db.close()


def _crush_store(path: Path, turns: list[tuple[str, int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    try:
        db.executescript(_CRUSH_SCHEMA)
    finally:
        db.close()
    for session, at, text in turns:
        _crush_add(path, session, at, text)


def _crush_exec(path: Path, *statements: str) -> None:
    db = sqlite3.connect(str(path))
    try:
        with db:
            for statement in statements:
                db.execute(statement)
    finally:
        db.close()


def _opencode_add(path: Path, home: Path, session: str, at: int, text: str, *,
                  new_session: bool = True) -> None:
    db = sqlite3.connect(str(path))
    try:
        with db:
            if new_session:
                db.execute("INSERT INTO session_v2 VALUES (?, 'oak', NULL, ?, ?, NULL, 'build', ?, ?)",
                           (session, str(home / "projects" / "oak"), text[:10], at, at))
            else:
                db.execute("UPDATE session_v2 SET time_updated = ? WHERE id = ?", (at, session))
            seq = db.execute("SELECT count(*) FROM session_message WHERE session_id = ?",
                             (session,)).fetchone()[0] + 1
            db.execute("INSERT INTO session_message VALUES (?, ?, 'user', ?, ?, ?, ?)",
                       (f"msg_{session}_{seq}", session, seq, at, at,
                        json.dumps({"time": {"created": at}, "text": text, "files": [], "agents": []})))
    finally:
        db.close()


def _opencode_store(path: Path, home: Path, turns: list[tuple[str, int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    try:
        db.executescript(_OPENCODE_SCHEMA)
    finally:
        db.close()
    for session, at, text in turns:
        _opencode_add(path, home, session, at, text)


def _cursor_store(path: Path, turns: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    try:
        with db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS cursorDiskKV(key TEXT PRIMARY KEY, value BLOB);"
                "CREATE TABLE IF NOT EXISTS composerHeaders(composerId TEXT PRIMARY KEY, workspaceId TEXT);")
            for session, text in turns:
                db.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                           (f"composerData:{session}", json.dumps({
                               "createdAt": 946684800000,
                               "fullConversationHeadersOnly": [{"bubbleId": "u", "type": 1}]})))
                db.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                           (f"bubbleId:{session}:u", json.dumps({"type": 1, "text": text})))
    finally:
        db.close()


def _gemini_session(path: Path, session: str, texts: list[str]) -> None:
    """A gemini chat of user turns: a legacy `.json` record, or the `.jsonl` log resuming writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {"sessionId": session, "projectHash": "hashsynthetic",
            "startTime": "2000-03-10T08:00:00.000Z", "lastUpdated": "2000-03-10T08:00:09.000Z"}
    turns = [{"id": f"m{n}", "timestamp": f"2000-03-10T08:00:0{n}.000Z", "type": "user",
              "content": [{"text": text}]} for n, text in enumerate(texts, 1)]
    if path.suffix == ".json":
        path.write_text(json.dumps({**head, "messages": turns}), encoding="utf-8")
    else:
        path.write_text("".join(json.dumps(record) + "\n" for record in (head, *turns)), encoding="utf-8")


def _whole_store_add(path: Path, agent: str, text: str) -> None:
    if agent == "cline":
        messages = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        messages.append({"role": "user", "content": text})
        path.write_text(json.dumps(messages) + "\n", encoding="utf-8")
    else:
        message = ({"role": "user", "content": text} if agent == "kimi" else
                   {"type": "USER_INPUT", "source": "USER_EXPLICIT",
                    "content": f"<USER_REQUEST>{text}</USER_REQUEST>",
                    "created_at": "2000-01-01T00:00:00.000Z"})
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")


def _whole_store_transcript(home: Path, agent: str, session: str, *, parent: str | None = None) -> Path:
    if agent == "kimi":
        chat = Path(parent, "subagents", session) if parent else Path(session)
        path = home / ".kimi" / "sessions" / ("0" * 32) / chat / "context.jsonl"
    elif agent == "cline":
        path = home / ".cline" / "data" / "tasks" / session / "api_conversation_history.json"
    else:
        path = (home / ".gemini" / "antigravity-cli" / "brain" / session
                / ".system_generated" / "logs" / "transcript.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    _whole_store_add(path, agent, "asciidoc ledger question")
    return path


def _kimi_wire(session_dir: Path) -> Path:
    """The UI event log kimi writes beside context.jsonl: one turn start, no message of its own."""
    path = session_dir / "wire.jsonl"
    path.write_text(json.dumps({"timestamp": 946684800.0,
                                "message": {"type": "TurnBegin", "payload": {}}}) + "\n",
                    encoding="utf-8")
    return path


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

    def display(self, path: Path) -> str:
        """A sandbox-home path the way `why` prints it."""
        return "~" + str(path)[len(str(self.home)):]

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
        self.record, self.dead_owner = _dead_explorer_descriptor()
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

    def test_untallied_old_file_without_an_intake_book_is_not_provable(self) -> None:
        """With intake_stats.json gone, nothing shows whether the last index parsed a file older
        than it: unprovable, never a file that appeared after the last index."""
        mirror = self.sandbox.store(f"omp/agent/sessions/{OMP_CONTAINER}/mirror.jsonl")
        (self.sandbox.data / "intake_stats.json").unlink()
        payload = self.assert_verdict("not-provable", str(mirror))
        self.assertEqual(payload["summary"],
                         f"unprovable: pi file {self.sandbox.display(mirror)} predates the last "
                         "index, but intake_stats.json cannot say whether it was parsed")
        self.assertEqual(payload["evidence"]["lines"][0], "intake_stats.json: missing")

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

    def test_deleted_transcript_path_answers_like_its_chat_id(self) -> None:
        """Until the next index the parse cache still claims a deleted transcript and search still
        serves it, so its path gets its chat's verdict; afterwards the path is reported deleted."""
        relative = f"-work-new/2000-05-01T00-00-00-000Z_{PI_NEW}.jsonl"
        path = self.sandbox.store(f"pi/agent/sessions/{relative}")
        _pi_transcript(path, PI_NEW, "quokka question")
        self.sandbox.index()
        path.unlink()
        by_id = self.assert_verdict("indexed", PI_NEW)
        for reference in (str(path), self.sandbox.display(path), _native(relative)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                self.assertEqual(payload["summary"], by_id["summary"])
                self.assertEqual(payload["evidence"]["sources"], [str(path)])
        self.assertIn(PI_NEW, self.sandbox.cli("search", "quokka", "--json").stdout)
        self.sandbox.index()
        self.assertNotIn(PI_NEW, self.sandbox.cli("search", "quokka", "--json").stdout)
        payload = self.assert_verdict("source-not-discovered", str(path))
        self.assertEqual(payload["summary"], f"not indexed: pi file {self.sandbox.display(path)} was "
                                             "deleted after an index parsed it")
        self.assertEqual(payload["evidence"]["lines"][:2], [
            f"filesystem: no file at {self.sandbox.display(path)}",
            "intake_stats.json: seen 2, rows 1, skips unreferenced:1"])
        never = path.with_name("never-written.jsonl")
        payload = self.assert_verdict("source-not-discovered", str(never))
        self.assertEqual(payload["summary"], f"not indexed: no file at {self.sandbox.display(never)}; "
                                             "it was deleted, or never existed")
        self.assertIn(f"pi: the path sits under its store root {_tilde('.pi/agent/sessions')}, but "
                      "no census source, parse-cache claim or intake record names it",
                      payload["evidence"]["lines"])
        self.assertFalse(any("parses" in line for line in payload["evidence"]["lines"]), payload)

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
    def test_unreadable_search_database_is_the_direct_scan_lane_with_a_doctor_hint(self) -> None:
        """The reader serves messages.jsonl when it cannot open corpus.db at all; `why` judges
        from the published rows and still points at doctor for the damaged database."""
        corpus_db = self.sandbox.data / "corpus.db"
        corpus_db.chmod(0)
        try:
            payload = self.assert_verdict("indexed", CLAUDE, next_action="agrep doctor")
            search = self.sandbox.cli("search", "copper lantern", "--json")
        finally:
            corpus_db.chmod(0o600)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["state"], corpus["served"], corpus["proof"], corpus["published_rows"]),
                         ("unreadable", "messages.jsonl", "scan", 4), corpus)
        self.assertTrue(payload["evidence"]["lines"][1].startswith("corpus.db: unreadable ("),
                        payload["evidence"]["lines"])
        self.assertIn("search scans messages.jsonl directly (4 rows published for this chat)",
                      payload["evidence"]["lines"][1])
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
        self.assert_verdict("indexed", CLAUDE)

    def test_dead_writers_hot_journal_reads_like_search(self) -> None:
        """A writer killed mid-transaction leaves a hot corpus.db-journal; the reader recovers it
        in a private system-temp clone and serves the last commit, and so must `why`, leaving
        the journal and the data dir untouched."""
        crash = ("import os, sqlite3, sys; db = sqlite3.connect(sys.argv[1], isolation_level=None); "
                 "db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA cache_size=2'); "
                 "db.execute('BEGIN IMMEDIATE'); db.execute(\"UPDATE msgs SET text = text || ' torn'\"); "
                 "os._exit(0)")
        crashed = subprocess.run([sys.executable, "-c", crash, str(self.sandbox.data / "corpus.db")],
                                 capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        journal = self.sandbox.data / "corpus.db-journal"
        self.assertTrue(journal.exists() and journal.stat().st_size > 0, "no hot journal was left")
        payload = self.assert_verdict("indexed", CLAUDE)
        corpus = payload["evidence"]["corpus"]
        self.assertEqual((corpus["state"], corpus["served"], corpus["proof"], corpus["rows"]),
                         ("ok", "corpus.db", "stamp", 4), corpus)
        self.assertTrue(journal.exists(), "why recovered the journal against the data dir")
        search = self.sandbox.cli("search", "copper lantern", "--json")
        self.assertIn(CLAUDE, search.stdout, search.stdout + search.stderr)
        self.assertNotIn("torn", search.stdout)

    def test_torn_sessions_index_falls_back_to_messages_like_resume(self) -> None:
        """sessions.jsonl holding no parseable row is agrep's own derived damage: resume and search
        answer from messages.jsonl, so `why` derives the same rows, without waking the daemon."""
        (self.sandbox.data / "sessions.jsonl").write_text("{torn\n", encoding="utf-8")
        env = self.sandbox.without_daemon_guard()
        for reference in (CLAUDE, "11111111-1111", "cedar"):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference, env=env)
                self.assertEqual(payload["evidence"]["index_row"]["session"], CLAUDE)
                self.assertTrue(payload["evidence"]["lines"][0].startswith(
                    f"messages.jsonl (sessions.jsonl torn): claude chat {CLAUDE}, 2 messages"),
                    payload["evidence"]["lines"])
        payload = self.assert_verdict("ambiguous", "11111111", env=env)
        self.assertEqual({c["session"] for c in payload["candidates"]}, {CLAUDE, CLAUDE_TWIN})
        self.assertEqual(self.assert_verdict("indexed-as-side-chat", "44444444", env=env)["evidence"]["corpus"]["root"],
                         OMP_ROOT)
        self.assertIn(CLAUDE, self.sandbox.cli("search", "copper lantern", "--json").stdout)

    def test_relative_sidecar_path_from_its_own_directory_is_exact(self) -> None:
        """`./advisor.jsonl` run inside a session directory names one file even when other
        sessions carry a same-named sidecar; the exact path outranks every trailing-part match."""
        other = self.sandbox.store("omp/agent/sessions/-work-gamma/"
                                   "2000-02-04T04-05-06-000Z_66666666-6666-4666-8666-666666666666")
        other.mkdir(parents=True)
        (other.parent / (other.name + ".jsonl")).write_text(
            json.dumps({"type": "session", "id": "66666666-6666-4666-8666-666666666666", "version": 3,
                        "cwd": "/work/gamma", "timestamp": "2000-02-04T04:05:06.000Z"}) + "\n"
            + json.dumps({"type": "message", "id": "g1", "parentId": None,
                          "timestamp": "2000-02-04T04:05:07.000Z",
                          "message": {"role": "user", "content": [{"type": "text", "text": "gamma plan"}],
                                      "timestamp": "2000-02-04T04:05:07.000Z"}}) + "\n",
            encoding="utf-8")
        (other / "advisor.jsonl").write_text(
            json.dumps({"type": "session", "id": "77777777-7777-4777-8777-777777777777", "version": 3,
                        "cwd": "/work/gamma", "timestamp": "2000-02-04T04:05:11.000Z"}) + "\n"
            + json.dumps({"type": "message", "id": "ga1", "parentId": None,
                          "timestamp": "2000-02-04T04:05:12.000Z",
                          "message": {"role": "user", "content": [{"type": "text", "text": "gamma advisor"}],
                                      "timestamp": "2000-02-04T04:05:12.000Z"}}) + "\n",
            encoding="utf-8")
        self.sandbox.index()
        beta = self.sandbox.store(f"omp/agent/sessions/{OMP_CONTAINER}")
        for cwd, session in ((beta, OMP_SIDE), (other, PI_NEW)):
            for reference in ("./advisor.jsonl", "advisor.jsonl"):
                with self.subTest(cwd=cwd.name, reference=reference):
                    payload = self.assert_verdict("indexed-as-side-chat", reference, cwd=cwd)
                    self.assertEqual(payload["evidence"]["index_row"]["session"], session)
                    self.assertEqual(payload["evidence"]["sources"], [str(cwd / "advisor.jsonl")])
        payload = self.assert_verdict("ambiguous", "advisor.jsonl")
        self.assertEqual(len(payload["candidates"]), 2)

    def test_new_token_store_conversation_is_written_after_last_index(self) -> None:
        """A crush conversation created after the index is in the census token list but in no
        parse-cache claim or intake record: it appeared after the last index, like a new file."""
        crush = self.sandbox.home / ".local" / "share" / "crush" / "crush.db"
        _crush_store(crush, [("sc1", 1000, "walnut ledger question")])
        self.sandbox.index()
        self.assert_verdict("indexed", "sc1")
        _crush_add(crush, "sc9new", 9000, "spruce gauge question")
        payload = self.assert_verdict("written-after-last-index", "sc9new", next_action="agrep index")
        self.assertEqual((payload["evidence"]["path"], payload["evidence"]["session"], payload["evidence"]["agent"]),
                         (str(crush), "sc9new", "crush"), payload["evidence"])
        self.assertEqual(payload["evidence"]["lines"][0],
                         "intake_stats.json: no record of this file, so no index has parsed it")
        self.assertIn(f"crush file {_tilde('.local/share/crush/crush.db')} conversation sc9new appeared "
                      "after the last index", payload["summary"])
        self.sandbox.index()
        self.assert_verdict("indexed", "sc9new")

    def test_new_chat_in_a_moved_stat_store_is_not_provable(self) -> None:
        """opencode keys its whole database by one stat key, so a new `ses_` id after the index
        has only that moved key as evidence: unprovable, naming the store, not undiscovered."""
        opencode = self.sandbox.home / ".local" / "share" / "opencode" / "opencode.db"
        _opencode_store(opencode, self.sandbox.home, [("ses_oc0001", 1_000_000, "oscar parser fix")])
        self.sandbox.index()
        self.assert_verdict("source-not-discovered", "ses_oc0002")
        _opencode_add(opencode, self.sandbox.home, "ses_oc0002", 2_000_000, "zephyr quartz question")
        payload = self.assert_verdict("not-provable", "ses_oc0002", next_action="agrep index")
        self.assertTrue(payload["evidence"]["lines"][0].startswith(
            f"intake_stats.json: {_tilde('.local/share/opencode/opencode.db')} holds 1 chat and changed "
            "since its parse at"), payload["evidence"]["lines"])
        self.assertIn("a store that changed since the last index may hold it", payload["summary"])
        self.sandbox.index()
        self.assert_verdict("indexed", "ses_oc0002")

    def test_store_wide_key_move_does_not_mark_untouched_chats(self) -> None:
        """Writing one conversation moves opencode's stat key and crush's database generation for
        every chat in the store; an untouched chat stays the corpus verdict, with the move as a
        caveat, while a chat whose own token moved is written-after-last-index."""
        crush = self.sandbox.home / ".local" / "share" / "crush" / "crush.db"
        _crush_store(crush, [("sc1", 1000, "walnut ledger question"), ("sc2", 2000, "hazel invoice question")])
        opencode = self.sandbox.home / ".local" / "share" / "opencode" / "opencode.db"
        _opencode_store(opencode, self.sandbox.home,
                        [("ses_oc0001", 1_000_000, "oscar parser fix"), ("ses_oc0002", 2_000_000, "quartz question")])
        self.sandbox.index()
        _crush_add(crush, "sc2", 2500, "hazel follow-up", new_session=False)
        _opencode_add(opencode, self.sandbox.home, "ses_oc0002", 2_500_000, "quartz follow-up",
                      new_session=False)
        for reference, store in (("sc1", crush), ("ses_oc0001", opencode)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                corpus = payload["evidence"]["corpus"]
                self.assertEqual((corpus["served"], corpus["proof"], corpus["current"]),
                                 ("corpus.db", "stamp", True), corpus)
                self.assertEqual(payload["evidence"]["intake"][0]["fresh"], False)
                caveat = [line for line in payload["evidence"]["lines"]
                          if line.startswith(f"intake_stats.json: {self.sandbox.display(store)} moved since its parse")]
                self.assertEqual(len(caveat), 1, payload["evidence"]["lines"])
        self.assertIn("its own part u:1000 is unchanged, only the database generation moved",
                      self.sandbox.why_json("sc1")[0]["evidence"]["lines"][2])
        self.assertIn("the store holds 2 chats, so the move singles out none",
                      self.sandbox.why_json("ses_oc0001")[0]["evidence"]["lines"][2])
        payload = self.assert_verdict("written-after-last-index", "sc2", next_action="agrep index")
        self.assertIn("(u:2000:x:", payload["evidence"]["lines"][1])
        self.assertIn("now unknown time (u:2500:x:", payload["evidence"]["lines"][1])
        for reference in ("sc1", "ses_oc0001"):
            search = self.sandbox.cli("search", "walnut ledger" if reference == "sc1" else "oscar parser", "--json")
            self.assertIn(reference, search.stdout, search.stdout + search.stderr)

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

    def test_moved_or_restored_transcript_waits_for_the_next_index(self) -> None:
        """A move, `cp -p` or restore keeps a transcript's old mtime, so its age shows nothing; only
        the last index's store walk (.source_snapshot.bin) proves a file was seen and left unparsed."""
        birch = self.sandbox.store(f"claude/projects/-projects-birch/{CLAUDE_TWIN}.jsonl")
        moved = self.sandbox.store(f"claude/projects/-projects-oak/{CLAUDE_TWIN}.jsonl")
        moved.parent.mkdir()
        os.rename(birch, moved)
        restored_id = "99999999-9999-4999-8999-999999999999"
        restored = moved.with_name(f"{restored_id}.jsonl")
        _append_claude_turn(restored, self.sandbox.home, "A restored question.",
                            "2000-03-01T00:00:00.000Z", session=restored_id, project="oak")
        os.utime(restored, (time.time() - 86400,) * 2)
        # A move keeps a Windows file's creation time, the ctime Python reports there.
        renamed = "not-provable" if os.name == "nt" else "written-after-last-index"
        for path, verdict in ((moved, renamed), (restored, "written-after-last-index")):
            with self.subTest(path=path.name):
                payload = self.assert_verdict(verdict, str(path), next_action="agrep index")
                self.assertIn("intake_stats.json: no record of this file, so no index has parsed it",
                              payload["evidence"]["lines"])
                self.assertNotIn("parses", payload["summary"])
        cedar = self.sandbox.store("claude/projects/-projects-cedar")
        elm = cedar.with_name("-projects-elm")
        os.rename(cedar, elm)
        relocated = elm / f"{CLAUDE}.jsonl"
        payload = self.assert_verdict("not-provable", str(relocated), next_action="agrep index")
        self.assertEqual(payload["summary"],
                         f"unprovable: claude file {self.sandbox.display(relocated)} "
                         "predates the last index, but nothing shows that index saw it")
        self.assertEqual(payload["evidence"]["lines"][0],
                         ".source_snapshot.bin: the last index's store walk did not list this file")
        self.sandbox.index()
        for path, session in ((moved, CLAUDE_TWIN), (restored, restored_id), (relocated, CLAUDE)):
            with self.subTest(path=path.name):
                payload = self.assert_verdict("indexed", str(path))
                self.assertEqual(payload["evidence"]["index_row"]["session"], session)

    def test_trailing_part_after_a_move_names_the_live_file(self) -> None:
        """intake_stats.json keeps a moved transcript's old path until an audit: a trailing part both
        fit names the live file, and only the old relative path still reaches the stale record."""
        birch = self.sandbox.store(f"claude/projects/-projects-birch/{CLAUDE_TWIN}.jsonl")
        moved = self.sandbox.store(f"claude/projects/-projects-oak/{CLAUDE_TWIN}.jsonl")
        moved.parent.mkdir()
        os.rename(birch, moved)
        self.sandbox.index()
        payload = self.assert_verdict("indexed", f"{CLAUDE_TWIN}.jsonl")
        self.assertEqual(payload["evidence"]["sources"], [str(moved)])
        payload = self.assert_verdict("source-not-discovered",
                                      _native(f"-projects-birch/{CLAUDE_TWIN}.jsonl"))
        self.assertEqual(payload["summary"], f"not indexed: claude file {self.sandbox.display(birch)} was "
                                             "deleted after an index parsed it")

    def test_unreadable_parse_cache_is_named_for_a_transcript_path(self) -> None:
        """Only the parse cache ties a stat-store file to its chat: with the cache unreadable a path
        is unprovable for that reason, never rows that no published session references."""
        path = self.sandbox.store(f"claude/projects/-projects-cedar/{CLAUDE}.jsonl")
        cache = self.sandbox.data / ".ingest_cache.bin"
        cache.write_bytes(b"garbage")
        payload = self.assert_verdict("not-provable", str(path))
        self.assertEqual(payload["summary"],
                         "unprovable: the parse cache is unreadable (undecodable cache payload), so "
                         f"claude file {self.sandbox.display(path)} cannot be tied to its chat")
        self.assertEqual(payload["evidence"]["lines"][0],
                         "parse cache: undecodable cache payload; no file can be tied to its chat")
        self.assert_verdict("indexed", CLAUDE)
        cache.unlink()
        payload = self.assert_verdict("not-provable", str(path), next_action="agrep index --full")
        self.assertIn("the parse cache is missing (missing cache file)", payload["summary"])
        full = self.sandbox.cli("index", "--full")
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        self.assert_verdict("indexed", str(path))

    def test_missing_sessions_index_is_labelled_missing_not_torn(self) -> None:
        """messages.jsonl answers when sessions.jsonl is gone, as resume reads; the evidence names
        the aggregate missing, a different damage from a torn one."""
        (self.sandbox.data / "sessions.jsonl").unlink()
        payload = self.assert_verdict("indexed", CLAUDE)
        self.assertTrue(payload["evidence"]["lines"][0].startswith(
            f"messages.jsonl (sessions.jsonl missing): claude chat {CLAUDE}, 2 messages"),
            payload["evidence"]["lines"])

    def test_empty_index_is_not_called_torn(self) -> None:
        """An index of no chats publishes an empty sessions.jsonl beside an empty messages.jsonl:
        nothing is torn, so the evidence reads from sessions.jsonl."""
        self.sandbox.close()
        self.sandbox = Sandbox()
        for store in (".claude", ".pi", ".omp"):
            shutil.rmtree(self.sandbox.home / store)
        self.sandbox.index()
        self.assertEqual((self.sandbox.data / "sessions.jsonl").read_text(encoding="utf-8"), "")
        payload = self.assert_verdict("source-not-discovered", "deadbeefcafe")
        self.assertEqual(payload["evidence"]["lines"][0],
                         "sessions.jsonl: 0 chats, none match by id, alias, project or first line")


class WhyUnclaimedStoreTests(_VerdictAssertions):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.close()

    def whole_stores(self) -> list[tuple[str, str, Path]]:
        return [(agent, session, _whole_store_transcript(self.sandbox.home, agent, session))
                for agent, session in WHOLE_STORE_SESSIONS]

    def cursor_path(self) -> Path:
        return (self.sandbox.home / ".config" / "Cursor" / "User"
                / "globalStorage" / "state.vscdb")

    def crush_path(self) -> Path:
        return self.sandbox.home / ".local" / "share" / "crush" / "crush.db"

    def crush_project(self, name: str, turns: list[tuple[str, int, str]]) -> tuple[Path, Path]:
        """A crush store in a project that projects.json registers, and that registry."""
        project = self.sandbox.home / "projects" / name
        store = project / ".crush" / "crush.db"
        _crush_store(store, turns)
        registry = self.crush_path().with_name("projects.json")
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"projects": [{"path": str(project), "data_dir": str(store.parent)}]}),
                            encoding="utf-8")
        return store, registry

    def assert_no_hits(self, query: str) -> None:
        search = self.sandbox.cli("search", query, "--json")
        self.assertEqual(json.loads(search.stdout)["completeness"]["shown"], 0, search.stdout)

    def test_whole_store_changed_transcripts_are_not_fully_indexed(self) -> None:
        stores = self.whole_stores()
        self.sandbox.index()
        for agent, session, path in stores:
            self.assert_verdict("indexed", session)
            _whole_store_add(path, agent, "walnut follow-up question")
        search = self.sandbox.cli("search", "walnut", "--json")
        self.assertEqual(search.returncode, 2, search.stderr)
        self.assertEqual(json.loads(search.stdout)["completeness"]["shown"], 0)
        for agent, session, path in stores:
            for reference in dict.fromkeys((session, session.split("-")[0], str(path))):
                with self.subTest(agent=agent, reference=reference):
                    payload = self.assert_verdict("written-after-last-index", reference,
                                                  next_action="agrep index")
                    self.assertTrue(any(e["path"] == str(path) and e["fresh"] is False
                                        for e in payload["evidence"]["intake"]), payload)
        self.sandbox.index()
        for _, session, _ in stores:
            self.assert_verdict("indexed", session)
        search = self.sandbox.cli("search", "walnut", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual({r["session"] for r in map(json.loads, search.stdout.splitlines()) if "session" in r},
                         {session for _, session, _ in stores})

    def test_whole_store_transcript_paths_resolve_indexed_chats(self) -> None:
        stores = self.whole_stores()
        self.sandbox.index()
        for agent, session, path in stores:
            with self.subTest(agent=agent):
                payload = self.assert_verdict("indexed", str(path))
                self.assertEqual(payload["evidence"]["index_row"]["session"], session)
                self.assertEqual(payload["evidence"]["index_row"]["agent"], agent)
                self.assertEqual(payload["evidence"]["sources"], [str(path)])
                self.assertTrue(all(e["fresh"] is True for e in payload["evidence"]["intake"]))
        search = self.sandbox.cli("search", "asciidoc", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual({r["session"] for r in map(json.loads, search.stdout.splitlines()) if "session" in r},
                         {session for _, session, _ in stores})

    def test_whole_store_without_session_intake_is_unverified(self) -> None:
        stores = self.whole_stores()
        self.sandbox.index()
        (self.sandbox.data / "intake_stats.json").unlink()
        for agent, session, _ in stores:
            with self.subTest(agent=agent):
                payload = self.assert_verdict("not-provable", session)
                self.assertEqual(payload["evidence"]["intake"], [])
                self.assertTrue(any("freshness unverified" in line
                                    for line in payload["evidence"]["lines"]), payload)

    def test_whole_store_session_matching_uses_complete_path_components(self) -> None:
        stores = self.whole_stores()
        neighbors = [(agent, _whole_store_transcript(self.sandbox.home, agent, session + "0"))
                     for agent, session, _ in stores]
        self.sandbox.index()
        for agent, path in neighbors:
            _whole_store_add(path, agent, "walnut in another session")
        for agent, session, path in stores:
            with self.subTest(agent=agent):
                payload = self.assert_verdict("indexed", session)
                self.assertEqual(payload["evidence"]["sources"], [str(path)])
                self.assertEqual([e["path"] for e in payload["evidence"]["intake"]], [str(path)])
                self.assertTrue(all(e["fresh"] is True for e in payload["evidence"]["intake"]))

    def test_crush_database_path_resolves_indexed_chat(self) -> None:
        path = self.sandbox.home / ".local" / "share" / "crush" / "crush.db"
        _crush_store(path, [("sc1", 1000, "asciidoc ledger question")])
        self.sandbox.index()
        self.assert_verdict("indexed", "sc1")
        search = self.sandbox.cli("search", "asciidoc", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertEqual({r["session"] for r in map(json.loads, search.stdout.splitlines()) if "session" in r},
                         {"sc1"})
        payload = self.assert_verdict("indexed", str(path))
        self.assertEqual(payload["evidence"]["index_row"]["session"], "sc1")
        _crush_add(path, "sc1", 2000, "walnut follow-up", new_session=False)
        self.assert_verdict("written-after-last-index", str(path), next_action="agrep index")

    def test_cursor_database_path_resolves_indexed_chat(self) -> None:
        path = self.cursor_path()
        _cursor_store(path, [("cursor-chat-one", "asciidoc ledger question")])
        self.sandbox.index()
        self.assert_verdict("indexed", "cursor-chat-one")
        payload = self.assert_verdict("indexed", str(path))
        self.assertEqual(payload["evidence"]["index_row"]["session"], "cursor-chat-one")
        self.assertEqual(payload["candidates"], [])

    def test_cursor_database_candidates_exclude_census(self) -> None:
        path = self.cursor_path()
        sessions = {"cursor-chat-one", "cursor-chat-two", "cursor-chat-three"}
        _cursor_store(path, [(session, "asciidoc ledger question") for session in sorted(sessions)])
        self.sandbox.index()
        payload = self.assert_verdict("ambiguous", str(path))
        self.assertEqual({c["session"] for c in payload["candidates"]}, sessions)
        self.assertTrue(all(c.get("via") == "path" for c in payload["candidates"]), payload)

    def test_cursor_unindexed_candidates_exclude_census(self) -> None:
        path = self.cursor_path()
        sessions = {"cursor-chat-one", "cursor-chat-two"}
        _cursor_store(path, [(session, "") for session in sorted(sessions)])
        self.sandbox.index()
        payload = self.assert_verdict("ambiguous", str(path))
        self.assertEqual({c["session"] for c in payload["candidates"]}, sessions)
        self.assertTrue(all(c["rows"] == 0 for c in payload["candidates"]), payload)

    def test_cursor_empty_conversation_keeps_its_intake_tally(self) -> None:
        path = self.cursor_path()
        _cursor_store(path, [("cursor-empty-chat", "")])
        self.sandbox.index()
        payload = self.assert_verdict("discovered-no-rows", str(path))
        self.assertEqual(payload["candidates"], [])
        self.assertTrue(any(e["session"] == "cursor-empty-chat" and e["seen"] == 1 and e["rows"] == 0
                            for e in payload["evidence"]["intake"]), payload)

    def test_new_file_in_an_indexed_session_directory_is_not_indexed(self) -> None:
        """A mailbox message or a subagent that lands in an indexed session directory has no intake
        record until the next index; a rotated context the adapter never parses is no such file."""
        stores = {agent: (session, path) for agent, session, path in self.whole_stores()}
        kimi, kimi_path = stores["kimi"]
        brain, transcript = stores["antigravity"]
        mailbox = transcript.parents[1] / "messages"
        mailbox.mkdir()
        (mailbox / "m1.json").write_text(json.dumps({"sender": "system", "content": "hello"}),
                                         encoding="utf-8")
        rotated = kimi_path.with_name("context_1.jsonl")
        rotated.write_text(json.dumps({"role": "user", "content": "pre-clear question"}) + "\n",
                           encoding="utf-8")
        os.utime(rotated, (time.time() - 3600,) * 2)
        self.sandbox.index()
        for session in (kimi, brain):
            self.assert_verdict("indexed", session)
        arrived = mailbox / "m2.json"
        arrived.write_text(json.dumps({"sender": f"{brain}/task-1", "content": "zeppelin result"}),
                           encoding="utf-8")
        child = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_CHILD, parent=kimi)
        for reference in (brain, str(transcript)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("written-after-last-index", reference,
                                              next_action="agrep index")
                self.assertEqual(payload["evidence"]["index_row"]["session"], brain)
                self.assertEqual(payload["evidence"]["unparsed"], [str(arrived)])
        for path in (arrived, child):
            with self.subTest(path=path):
                payload = self.assert_verdict("written-after-last-index", str(path),
                                              next_action="agrep index")
                self.assertIn("appeared after the last index", payload["summary"])
                self.assertEqual(payload["evidence"]["intake"], [])
        payload = self.assert_verdict("indexed", kimi)
        self.assertEqual(payload["evidence"]["sources"], [str(kimi_path)])
        self.sandbox.index()
        self.assert_verdict("indexed", brain)
        self.assert_verdict("indexed-as-side-chat", str(child))

    def test_kimi_subagent_files_belong_to_the_child_chat(self) -> None:
        """Kimi keeps a subagent under its parent (`<P>/subagents/<C>/context.jsonl`); that file is
        the child's alone, so its writes leave the parent current and its path names one chat."""
        kimi = WHOLE_STORE_SESSIONS[0][1]
        path = _whole_store_transcript(self.sandbox.home, "kimi", kimi)
        child = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_CHILD, parent=kimi)
        self.sandbox.index()
        payload = self.assert_verdict("indexed", kimi)
        self.assertEqual(payload["evidence"]["sources"], [str(path)])
        payload = self.assert_verdict("indexed-as-side-chat", str(child))
        self.assertEqual(payload["evidence"]["index_row"]["session"], KIMI_CHILD)
        _whole_store_add(child, "kimi", "walnut follow-up question")
        for reference in (kimi, str(path)):
            with self.subTest(reference=reference):
                self.assert_verdict("indexed", reference)
        for reference in (KIMI_CHILD, str(child)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("written-after-last-index", reference,
                                              next_action="agrep index")
                self.assertEqual(payload["evidence"]["index_row"]["session"], KIMI_CHILD)
                self.assertEqual([e["path"] for e in payload["evidence"]["intake"]], [str(child)])

    def test_deleted_whole_store_file_answers_like_its_chat_until_the_next_index(self) -> None:
        """No parse-cache claim names a kimi file, so only its intake record ties a deleted one to
        the chat search still serves; once the next index drops that chat, the file was deleted."""
        kimi = WHOLE_STORE_SESSIONS[0][1]
        _whole_store_transcript(self.sandbox.home, "kimi", kimi)
        child = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_CHILD, parent=kimi)
        self.sandbox.index()
        child.unlink()
        by_id = self.assert_verdict("indexed-as-side-chat", KIMI_CHILD)
        payload = self.assert_verdict("indexed-as-side-chat", str(child))
        self.assertEqual(payload["summary"], by_id["summary"])
        self.sandbox.index()
        self.assert_verdict("source-not-discovered", KIMI_CHILD)
        payload = self.assert_verdict("source-not-discovered", str(child))
        self.assertEqual(payload["summary"], f"not indexed: kimi file {self.sandbox.display(child)} "
                                             "was deleted after an index parsed it")

    def test_untallied_file_older_than_the_last_index_is_not_parsed(self) -> None:
        """kimi never ingests a rotated context_N.jsonl or a subagent below a subagent; no index
        tallies either, so neither appeared after the last index nor waits for the next one."""
        kimi = WHOLE_STORE_SESSIONS[0][1]
        path = _whole_store_transcript(self.sandbox.home, "kimi", kimi)
        child = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_CHILD, parent=kimi)
        nested = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_NESTED,
                                         parent=str(Path(kimi, "subagents", KIMI_CHILD)))
        _whole_store_add(nested, "kimi", "quokka nested question")
        rotated = path.with_name("context_1.jsonl")
        _whole_store_add(rotated, "kimi", "quokka pre-clear question")
        for old in (rotated, nested):
            os.utime(old, (time.time() - 3600,) * 2)
        for _ in range(2):
            self.sandbox.index()
            search = self.sandbox.cli("search", "quokka", "--json")
            self.assertEqual(json.loads(search.stdout)["completeness"]["shown"], 0, search.stdout)
            for reference, unparsed in ((str(rotated), rotated), (str(nested), nested),
                                        (KIMI_NESTED, nested)):
                with self.subTest(reference=reference):
                    payload = self.assert_verdict("discovered-no-rows", reference)
                    self.assertEqual(payload["summary"],
                                     f"discovered but not parsed: {self.sandbox.display(unparsed)} "
                                     "is not a file kimi parses, so none of it is searchable")
                    self.assertEqual(payload["evidence"]["intake"], [])
            self.assertEqual(self.assert_verdict("indexed", kimi)["evidence"]["sources"], [str(path)])
            self.assert_verdict("indexed-as-side-chat", str(child))

    def test_claude_file_its_store_walk_never_lists_is_not_parsed(self) -> None:
        """claude walks only transcripts 2 to 7 levels below ~/.claude/projects; a `.jsonl` the census
        finds above or below that range is never parsed, however new it is or often the index runs."""
        projects = self.sandbox.home / ".claude" / "projects"
        nested = projects.joinpath("-p", "a", "b", "c", "d", "e")
        shallow = projects / "eeeeeeee-1111-4111-8111-111111111111.jsonl"
        deep = nested / "f" / "ffffffff-1111-4111-8111-111111111111.jsonl"
        deepest = nested / "dddddddd-1111-4111-8111-111111111111.jsonl"
        deep.parent.mkdir(parents=True)

        def write(path: Path) -> None:
            _append_claude_turn(path, self.sandbox.home, "quokka depth question",
                                "2000-01-01T00:00:00.000Z", session=path.stem)

        for path in (shallow, deep, deepest):
            write(path)
            os.utime(path, (time.time() - 86400,) * 2)
        late = deep.with_name("cccccccc-1111-4111-8111-111111111111.jsonl")
        for round_ in range(2):
            self.sandbox.index()
            if round_:
                write(late)
            for path in (shallow, deep) + ((late,) if round_ else ()):
                with self.subTest(path=path, round=round_):
                    payload = self.assert_verdict("discovered-no-rows", str(path))
                    self.assertEqual(payload["summary"],
                                     f"discovered but not parsed: {self.sandbox.display(path)} "
                                     "is not a file claude parses, so none of it is searchable")
                    self.assertEqual(payload["evidence"]["intake"], [])
            self.assert_verdict("indexed", str(deepest))
            search = self.sandbox.cli("search", "quokka", "--json")
            self.assertEqual({r["session"] for r in map(json.loads, search.stdout.splitlines())
                              if "session" in r}, {deepest.stem})

    def test_token_store_path_with_a_new_conversation_is_not_indexed(self) -> None:
        """A conversation created after the index is in the census token list but in no intake
        record, so the database path is not indexed, whatever its indexed chats would say."""
        crush = self.sandbox.home / ".local" / "share" / "crush" / "crush.db"
        _crush_store(crush, [("sc1", 1000, "asciidoc ledger question")])
        cursor = self.cursor_path()
        _cursor_store(cursor, [("cursor-chat-one", "asciidoc ledger question"),
                               ("cursor-chat-two", "asciidoc ledger question")])
        self.sandbox.index()
        self.assert_verdict("indexed", str(crush))
        self.assert_verdict("ambiguous", str(cursor))
        _crush_add(crush, "sc2", 2000, "walnut follow-up question")
        _cursor_store(cursor, [("cursor-chat-three", "walnut follow-up question")])
        for path, new in ((crush, "sc2"), (cursor, "cursor-chat-three")):
            with self.subTest(path=path):
                payload = self.assert_verdict("written-after-last-index", str(path),
                                              next_action="agrep index")
                self.assertEqual(payload["evidence"]["unparsed"], [new])
                self.assertEqual(payload["candidates"], [])
        search = self.sandbox.cli("search", "walnut", "--json")
        self.assertEqual(search.returncode, 2, search.stderr)
        self.sandbox.index()
        for path, sessions in ((crush, {"sc1", "sc2"}),
                               (cursor, {"cursor-chat-one", "cursor-chat-two", "cursor-chat-three"})):
            with self.subTest(path=path):
                payload = self.assert_verdict("ambiguous", str(path))
                self.assertEqual({c["session"] for c in payload["candidates"]}, sessions)

    def test_new_whole_store_chats_resolve_by_id(self) -> None:
        """A kimi session, a kimi subagent or a cline task created after the index is found by its
        directory id, one candidate per chat however many of its files the census lists."""
        stores = {agent: (session, path) for agent, session, path in self.whole_stores()}
        kimi, kimi_path = stores["kimi"]
        _kimi_wire(kimi_path.parent)
        self.sandbox.index()
        fresh = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_NEW)
        _kimi_wire(fresh.parent)
        child = _whole_store_transcript(self.sandbox.home, "kimi", KIMI_CHILD, parent=kimi)
        task = _whole_store_transcript(self.sandbox.home, "cline", CLINE_NEW)
        for session, path in ((KIMI_NEW, fresh), (KIMI_CHILD, child), (CLINE_NEW, task)):
            for reference in dict.fromkeys((session, session.split("-")[0])):
                with self.subTest(reference=reference):
                    payload = self.assert_verdict("written-after-last-index", reference,
                                                  next_action="agrep index")
                    self.assertEqual((payload["evidence"]["path"], payload["evidence"]["session"]),
                                     (str(path), session))
                    self.assertIn("appeared after the last index", payload["summary"])
        self.sandbox.index()
        self.assert_verdict("indexed", KIMI_NEW)
        self.assert_verdict("indexed-as-side-chat", KIMI_CHILD)
        self.assert_verdict("indexed", CLINE_NEW)

    def test_store_issue_that_kept_the_last_good_parse_is_not_indexed(self) -> None:
        """An invalid cline taskHistory.json makes the index keep serving the last good snapshot
        while intake_stats.json tallies the fresh parse; `why` must not vouch for that parse."""
        session = WHOLE_STORE_SESSIONS[1][1]
        path = _whole_store_transcript(self.sandbox.home, "cline", session)
        history = self.sandbox.home / ".cline" / "data" / "state" / "taskHistory.json"
        history.parent.mkdir(parents=True)
        history.write_text(json.dumps([{"id": session}]), encoding="utf-8")
        self.sandbox.index()
        self.assert_verdict("indexed", session)
        _whole_store_add(path, "cline", "zeppelin follow-up question")
        history.write_text("{not json", encoding="utf-8")
        self.sandbox.index()
        search = self.sandbox.cli("search", "zeppelin", "--json")
        self.assertEqual(search.returncode, 2, search.stderr)
        payload = self.assert_verdict("not-provable", session,
                                      next_action="make the file readable, then agrep index")
        self.assertIn("freshness unverified", payload["summary"])
        self.assertEqual(payload["evidence"]["store_issue"]["path"], str(history))
        self.assertTrue(all(e["fresh"] is True for e in payload["evidence"]["intake"]), payload)
        history.write_text(json.dumps([{"id": session}]), encoding="utf-8")
        self.sandbox.index()
        self.assert_verdict("indexed", session)
        search = self.sandbox.cli("search", "zeppelin", "--json")
        self.assertEqual(search.returncode, 0, search.stderr)

    def test_file_moved_into_an_indexed_chat_waits_for_the_next_index(self) -> None:
        """A mailbox message copied in with an old mtime is as new to the index as a fresh one, and
        without .source_snapshot.bin no old untallied file is proven skipped by the last index."""
        stores = {agent: (session, path) for agent, session, path in self.whole_stores()}
        brain, transcript = stores["antigravity"]
        rotated = stores["kimi"][1].with_name("context_1.jsonl")
        _whole_store_add(rotated, "kimi", "quokka pre-clear question")
        self.sandbox.index()
        self.assert_verdict("discovered-no-rows", str(rotated))
        mailbox = transcript.parents[1] / "messages"
        mailbox.mkdir()
        restored = mailbox / "m1.json"
        restored.write_text(json.dumps({"sender": f"{brain}/task-1", "content": "zeppelin result"}),
                            encoding="utf-8")
        os.utime(restored, (time.time() - 86400,) * 2)
        payload = self.assert_verdict("written-after-last-index", brain, next_action="agrep index")
        self.assertEqual(payload["evidence"]["unparsed"], [str(restored)])
        self.assert_verdict("written-after-last-index", str(restored), next_action="agrep index")
        (self.sandbox.data / ".source_snapshot.bin").unlink()
        payload = self.assert_verdict("not-provable", str(rotated), next_action="agrep index")
        self.assertEqual(payload["evidence"]["lines"][0], ".source_snapshot.bin: missing")
        self.assert_verdict("written-after-last-index", str(restored), next_action="agrep index")
        self.sandbox.index()
        self.assert_verdict("indexed", brain)
        self.assert_verdict("discovered-no-rows", str(rotated))

    def test_deleted_file_of_a_chat_with_other_files_is_deleted_after_the_next_index(self) -> None:
        """A chat that keeps other files stays indexed when one is deleted; that file answers like
        its chat only until an index walks the store without it, then it was deleted."""
        stores = {agent: (session, path) for agent, session, path in self.whole_stores()}
        brain, transcript = stores["antigravity"]
        mailbox = transcript.parents[1] / "messages"
        mailbox.mkdir()
        message = mailbox / "m1.json"
        message.write_text(json.dumps({"sender": f"{brain}/task-1", "content": "zeppelin result"}),
                           encoding="utf-8")
        self.sandbox.index()
        message.unlink()
        relative = _native("messages/m1.json")
        for reference in (str(message), relative):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("indexed", reference)
                self.assertEqual(payload["evidence"]["index_row"]["session"], brain)
        self.assertIn(brain, self.sandbox.cli("search", "zeppelin", "--json").stdout)
        self.sandbox.index()
        self.assert_no_hits("zeppelin")
        for reference in (str(message), relative):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("source-not-discovered", reference)
                self.assertEqual(payload["summary"], f"not indexed: antigravity file "
                                                     f"{self.sandbox.display(message)} was deleted after "
                                                     "an index parsed it")
        self.assertEqual(self.assert_verdict("indexed", brain)["evidence"]["sources"], [str(transcript)])
        # Without .source_snapshot.bin, a directory last changed before the index dates the deletion.
        (self.sandbox.data / ".source_snapshot.bin").unlink()
        os.utime(mailbox, (time.time() - 3600,) * 2)
        self.assert_verdict("source-not-discovered", str(message))
        self.assertEqual(self.assert_verdict("indexed", brain)["evidence"]["sources"], [str(transcript)])

    def test_deleted_token_store_is_deleted_not_ambiguous(self) -> None:
        """intake_stats.json tallies a deleted database's conversations until an audit; once the
        index drops their chats the path is a deleted file, never ambiguous between them."""
        crush = self.crush_path()
        _crush_store(crush, [("sc1", 1000, "walnut ledger question"),
                             ("sc2", 2000, "hazel invoice question")])
        self.sandbox.index()
        crush.unlink()
        # The first index after the store vanishes keeps its last good parse; the next drops it.
        self.sandbox.index()
        self.sandbox.index()
        self.assert_no_hits("walnut")
        payload = self.assert_verdict("source-not-discovered", str(crush))
        self.assertEqual(payload["summary"], f"not indexed: crush file {self.sandbox.display(crush)} was "
                                             "deleted after an index parsed it")
        self.assertEqual(payload["evidence"]["lines"][:2], [
            f"filesystem: no file at {self.sandbox.display(crush)}",
            "intake_stats.json: 2 conversations parsed from it: sc1, sc2"])
        self.assertEqual(payload["candidates"], [])

    def test_conversation_deleted_from_a_live_token_store_is_deleted(self) -> None:
        """The census reads a live database's whole token list, so a tallied conversation missing
        from it was deleted after an index parsed it while the store's other chat stays indexed."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question"),
                             ("crushchat-two", 2000, "hazel invoice question")])
        self.sandbox.index()
        db = sqlite3.connect(str(crush))
        try:
            with db:
                db.execute("DELETE FROM messages WHERE session_id = 'crushchat-two'")
                db.execute("DELETE FROM sessions WHERE id = 'crushchat-two'")
        finally:
            db.close()
        self.assert_verdict("indexed", "crushchat-two")
        self.sandbox.index()
        self.assert_no_hits("hazel")
        payload = self.assert_verdict("source-not-discovered", "crushchat-two")
        self.assertEqual(payload["summary"], "not indexed: crush conversation crushchat-two was deleted "
                                             f"from {self.sandbox.display(crush)} after an index parsed it")
        self.assertEqual(payload["evidence"]["lines"][0], f"store census: {self.sandbox.display(crush)} "
                                                          "holds 1 conversation, none of them crushchat-two")
        self.assert_verdict("indexed", "crushchat-one")
        self.assert_verdict("indexed", str(crush))

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "permission bits do not bind here")
    def test_unreadable_token_store_is_not_called_indexed(self) -> None:
        """A token-store chat reads its issues from its database the way a transcript does: an
        unreadable crush.db still serves the last good parse, which is not fully indexed."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question")])
        self.sandbox.index()
        crush.chmod(0)
        self.sandbox.index()
        for reference in ("crushchat-one", str(crush)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("source-unreadable", reference,
                                              next_action="make the file readable, then agrep index")
                self.assertEqual(payload["evidence"]["issue"]["path"], str(crush))
                self.assertIn("sessions.jsonl still serves the last good parse (1 message)",
                              payload["evidence"]["lines"])
        crush.chmod(0o600)
        self.sandbox.index()
        self.assert_verdict("indexed", "crushchat-one")

    def test_a_conversations_bad_row_leaves_its_siblings_indexed(self) -> None:
        """crush files one conversation's unreadable row as an issue on its whole database; only the
        conversation whose own tally shows that failed parse serves its last good parse."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question"),
                             ("crushchat-two", 2000, "hazel invoice question"),
                             ("crushchat-three", 3000, "maple budget question")])
        self.sandbox.index()
        _crush_exec(crush, "INSERT INTO messages VALUES "
                           "('bad', 'crushchat-two', 'user', '{not json', '', 4000, 4000)",
                    "UPDATE sessions SET updated_at = 4000 WHERE id = 'crushchat-two'")
        self.sandbox.index()
        unreadable = "make the file readable, then agrep index"
        payload = self.assert_verdict("source-unreadable", "crushchat-two", next_action=unreadable)
        self.assertEqual(payload["evidence"]["issue"]["path"], str(crush))
        for session in ("crushchat-one", "crushchat-three"):
            with self.subTest(session=session):
                self.assertIsNone(self.assert_verdict("indexed", session)["evidence"]["issue"])
        _crush_add(crush, "crushchat-one", 5000, "oak follow-up question", new_session=False)
        _crush_exec(crush, "DELETE FROM messages WHERE session_id = 'crushchat-three'",
                    "DELETE FROM sessions WHERE id = 'crushchat-three'")
        self.assert_verdict("written-after-last-index", "crushchat-one", next_action="agrep index")
        self.assert_verdict("indexed", "crushchat-three")
        self.sandbox.index()
        self.assert_no_hits("maple")
        payload = self.assert_verdict("source-not-discovered", "crushchat-three")
        self.assertEqual(payload["summary"], "not indexed: crush conversation crushchat-three was deleted "
                                             f"from {self.sandbox.display(crush)} after an index parsed it")
        self.assert_verdict("indexed", "crushchat-one")
        self.assert_verdict("source-unreadable", "crushchat-two", next_action=unreadable)

    def test_an_emptied_sibling_leaves_the_other_conversation_indexed(self) -> None:
        """A conversation whose rows are gone while its session stays parses to nothing, so crush keeps
        its last good parse and flags the database; that says nothing about the other chat."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question"),
                             ("crushchat-two", 2000, "hazel invoice question")])
        self.sandbox.index()
        _crush_exec(crush, "DELETE FROM messages WHERE session_id = 'crushchat-two'",
                    "UPDATE sessions SET updated_at = 4000 WHERE id = 'crushchat-two'")
        self.sandbox.index()
        self.assertIsNone(self.assert_verdict("indexed", "crushchat-one")["evidence"]["issue"])
        payload = self.assert_verdict("source-unreadable", "crushchat-two",
                                      next_action="make the file readable, then agrep index")
        self.assertEqual(payload["evidence"]["issue"]["path"], str(crush))

    def test_store_the_census_no_longer_discovers_is_not_called_deleted(self) -> None:
        """crush reads only the databases projects.json registers. One dropped from it still holds its
        chats, so a census that never opened it proves no deletion."""
        store, registry = self.crush_project("oak", [("projchat-one", 1000, "marzipan ledger question")])
        self.sandbox.index()
        self.assert_verdict("indexed", "projchat-one")
        registry.write_text(json.dumps({"projects": []}), encoding="utf-8")
        self.sandbox.index()
        self.sandbox.index()
        self.assert_no_hits("marzipan")
        for reference in ("projchat-one", str(store)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("not-provable", reference)
                self.assertEqual(payload["evidence"]["lines"][0],
                                 f"store census: discovers no crush store at {self.sandbox.display(store)}")

    def test_emptied_live_token_store_is_deleted_not_ambiguous(self) -> None:
        """A live database the census reads whole that holds none of the conversations an index parsed
        from it lost them all: deleted, never ambiguous between chats it no longer holds."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question"),
                             ("crushchat-two", 2000, "hazel invoice question")])
        self.sandbox.index()
        _crush_exec(crush, "DELETE FROM messages", "DELETE FROM sessions")
        # The first index after the store empties keeps its last good parse; the next drops it.
        self.sandbox.index()
        self.sandbox.index()
        self.assert_no_hits("walnut")
        shown = self.sandbox.display(crush)
        payload = self.assert_verdict("source-not-discovered", str(crush))
        self.assertEqual(payload["summary"], f"not indexed: every conversation an index parsed from crush "
                                             f"file {shown} was deleted from it")
        self.assertEqual(payload["evidence"]["lines"][:2], [
            f"store census: {shown} holds 0 conversations, none of the 2 an index parsed",
            "intake_stats.json: 2 conversations parsed from it: crushchat-one, crushchat-two"])
        self.assertEqual(payload["candidates"], [])

    def test_unregistered_store_of_several_chats_is_unprovable_not_ambiguous(self) -> None:
        """A census that never opened a database projects.json dropped tells none of its chats apart:
        its path is as unprovable as each id, never ambiguous between chats search no longer serves."""
        store, registry = self.crush_project("oak", [("projchat-one", 1000, "marzipan ledger question"),
                                                     ("projchat-two", 2000, "nutmeg invoice question")])
        self.sandbox.index()
        registry.write_text(json.dumps({"projects": []}), encoding="utf-8")
        self.sandbox.index()
        self.sandbox.index()
        self.assert_no_hits("marzipan")
        unlisted = f"store census: discovers no crush store at {self.sandbox.display(store)}"
        for reference in ("projchat-one", "projchat-two"):
            with self.subTest(reference=reference):
                self.assertEqual(self.assert_verdict("not-provable", reference)["evidence"]["lines"][0],
                                 unlisted)
        payload = self.assert_verdict("not-provable", str(store))
        self.assertEqual(payload["evidence"]["lines"][:2], [
            unlisted, "intake_stats.json: 2 conversations parsed from it: projchat-one, projchat-two"])
        self.assertEqual(payload["candidates"], [])

    def test_blind_token_census_proves_no_chat_current_or_absent(self) -> None:
        """crush lists no conversation of any database while one cannot be read: the census shows
        neither that a healthy database's chat is unchanged nor that an unknown one is not in it."""
        crush = self.crush_path()
        _crush_store(crush, [("crushchat-one", 1000, "walnut ledger question")])
        store, _ = self.crush_project("elm", [("elmchat-one", 1000, "hazel invoice question")])
        self.sandbox.index()
        store.write_bytes(b"not a crush database")
        self.sandbox.index()
        self.assert_verdict("source-unreadable", "elmchat-one",
                            next_action="make the file readable, then agrep index")
        _crush_add(crush, "crushchat-new", 3000, "quince new question")
        _crush_add(crush, "crushchat-one", 4000, "maple follow-up question", new_session=False)
        self.assert_no_hits("maple")
        blind = f"store census: token-census-unreadable on {self.sandbox.display(store)} - "
        for round_, references in enumerate((("crushchat-one", str(crush), "crushchat-new"),
                                             ("crushchat-one", "crushchat-new"))):
            for reference in references:
                with self.subTest(reference=reference, round=round_):
                    payload = self.assert_verdict("not-provable", reference, next_action="agrep index")
                    self.assertTrue(any(line.startswith(blind) for line in payload["evidence"]["lines"]),
                                    payload)
            self.sandbox.index()
        for query, session in (("maple", "crushchat-one"), ("quince", "crushchat-new")):
            self.assertIn(session, self.sandbox.cli("search", query, "--json").stdout)
        store.unlink()
        _crush_store(store, [("elmchat-one", 1000, "hazel invoice question")])
        self.sandbox.index()
        for session in ("crushchat-one", "crushchat-new", "elmchat-one"):
            with self.subTest(session=session):
                self.assert_verdict("indexed", session)

    def test_resumed_gemini_chat_is_judged_by_the_jsonl_gemini_reads(self) -> None:
        """Resuming a legacy gemini `X.json` copies it into `X.jsonl`, which carries the chat from then
        on: the chat waits for the next index, after which the retired file answers like its successor."""
        legacy = (self.sandbox.home / ".gemini" / "tmp" / "hashsynthetic" / "chats"
                  / "session-2000-03-10T08-00-eeeeeeee.json")
        resumed = legacy.with_suffix(".jsonl")
        _gemini_session(legacy, GEMINI, ["walnut ledger question"])
        self.sandbox.index()
        self.assert_verdict("indexed", str(legacy))
        book = self.sandbox.data / "intake_stats.json"
        tally = json.loads(book.read_text(encoding="utf-8"))["files"][str(legacy)]
        _gemini_session(resumed, GEMINI, ["walnut ledger question", "zeppelin follow-up question"])
        for reference in (GEMINI, str(legacy)):
            with self.subTest(reference=reference):
                payload = self.assert_verdict("written-after-last-index", reference,
                                              next_action="agrep index")
                self.assertEqual(payload["evidence"]["unparsed"], [str(resumed)])
        self.assert_no_hits("zeppelin")
        retired = (f"gemini: reads {self.sandbox.display(resumed)} instead; "
                   "resuming copied this legacy file into it")
        for round_ in range(2):
            self.sandbox.index()
            self.assertIn(GEMINI, self.sandbox.cli("search", "zeppelin", "--json").stdout)
            for reference in (GEMINI, str(resumed)):
                self.assert_verdict("indexed", reference)
            # A data dir indexed before intake forgot retired files still tallies the legacy one.
            for tallied in (True, False):
                book_data = json.loads(book.read_text(encoding="utf-8"))
                book_data["files"].pop(str(legacy), None)
                if tallied:
                    book_data["files"][str(legacy)] = tally
                book.write_text(json.dumps(book_data), encoding="utf-8")
                for reference in (str(legacy), legacy.name):
                    with self.subTest(round=round_, tallied=tallied, reference=reference):
                        payload = self.assert_verdict("indexed", reference)
                        self.assertEqual(payload["evidence"]["lines"][0], retired)
                        self.assertEqual(payload["evidence"]["superseded_by"], str(resumed))
                        self.assertEqual(payload["evidence"]["index_row"]["session"], GEMINI)

    def test_store_file_its_agent_stopped_reading_is_not_indexed(self) -> None:
        """opencode reads the database OPENCODE_DB names only while it names it: a parsed file the census
        no longer discovers and the last index did not walk is one its agent stopped reading."""
        store = self.sandbox.home / "elsewhere" / "oak.db"
        _opencode_store(store, self.sandbox.home, [("ses_quince", 1000, "quince ledger question")])
        named = {**self.sandbox.env, "OPENCODE_DB": str(store)}
        indexed = self.sandbox.cli("index", env=named)
        self.assertEqual(indexed.returncode, 0, indexed.stdout + indexed.stderr)
        self.assert_verdict("indexed", str(store), env=named)
        for round_ in range(2):
            self.sandbox.index()
            self.assert_no_hits("quince")
            for reference in (str(store), "ses_quince"):
                with self.subTest(reference=reference, round=round_):
                    payload = self.assert_verdict("source-not-discovered", reference)
                    self.assertEqual(payload["summary"], f"not indexed: opencode no longer reads "
                                                         f"{self.sandbox.display(store)}, which an index "
                                                         "parsed before")


if __name__ == "__main__":
    unittest.main()
