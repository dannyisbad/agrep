"""POSIX black-box CLI contracts; AGREP_UPDATE_CONFORMANCE=1 records goldens.

ConformanceSandbox owns one isolated, synchronously indexed store and both caller
identities: a human shell (no caller publication) and an agent whose published
session is excluded. run_matrix accepts a run_cli-compatible callable so another
test can compare execution modes without importing or patching product modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "py" / "fixtures" / "conformance"
GOLDEN = FIXTURES / "golden"
MISS = "quasarunfindable987654321"
CANONICAL_TIME = datetime(2000, 1, 20, 12, tzinfo=timezone.utc)
TIME_TEMPLATE = re.compile(r"\{\{t((?:[+-]\d+[dhms])+)\}\}")
AGENT_SESSION = "66666666-6666-4666-8666-666666666666"
CALLERS = ("human", "agent")


@dataclass(frozen=True)
class Case:
    name: str
    argv: tuple[str, ...] = ()
    project_cwd: str | None = None
    correction_of: str | None = None
    exit_code: int = 0


CASES = (
    Case("search_word", ("lantern",)),
    Case("search_phrase", ("copper lantern",)),
    Case("search_list", ("lantern", "-l")),
    Case("search_list_time", ("lantern", "-l", "--sort", "time")),
    Case("search_json", ("lantern", "--json", "-n", "3")),
    Case("search_count_short", ("lantern", "-c")),
    Case("search_count_long", ("copper lantern", "--count")),
    Case("search_chats", ("lantern", "--chats")),
    Case("search_who_user", ("lantern", "--who", "user")),
    Case("search_project", ("lantern", "--project", "cedar")),
    Case("search_exclude_project", ("lantern", "--exclude-project", "cedar")),
    Case("search_here", ("lantern", "--here"), project_cwd="cedar"),
    Case("search_regex", ("-E", "beacon-(red|blue)")),
    Case("search_word_boundary", ("-w", "key")),
    Case("search_miss", (MISS,), exit_code=1),
    Case("search_no_auto", ("lantern", "--no-auto")),
    Case("search_miss_no_auto", (MISS, "--no-auto"), exit_code=2),
    Case("search_tool", ("toolproof", "--who", "tool")),
    Case("search_recap", ("saffron recap",)),
    Case("search_side", ("sidecar lantern",)),
    Case("search_no_side", ("lantern", "--no-side")),
    Case("search_agent", ("lantern", "--agent", "codex")),
    Case("search_json_chats", ("lantern", "-l", "--json")),
    Case("search_one", ("lantern", "-n", "1")),
    Case("chats_list", ("chats",)),
    Case("chats_pattern", ("chats", "copper lantern")),
    Case("chats_side", ("chats", "--side")),
    Case("chats_no_side", ("chats", "--no-side")),
    Case("chats_json", ("chats", "--json")),
    Case("chats_three", ("chats", "-n", "3")),
    Case("chats_project", ("chats", "--project", "cedar")),
    Case("around_handle", ("around", "{handle}")),
    Case("around_session", ("around", "{session}")),
    Case("around_at_session", ("around", "@{session}")),
    Case("around_context", ("around", "{handle}", "-C", "2")),
    Case("around_whole", ("around", "{session}", "--whole")),
    Case("around_whole_user", ("around", "{session}", "--whole", "--who", "user")),
    Case("around_json", ("around", "{handle}", "-C", "2", "--json")),
    Case("around_full", ("around", "{handle}", "-C", "2", "--full")),
    Case("around_tool_results", ("around", "{handle}", "-C", "2", "--tool-output", "800")),
    Case("around_tool_handle", ("around", "{tool_handle}", "--tool-output", "800")),
    Case("recall_lexical", ("recall", "copper lantern", "--lexical")),
    Case("recall_json", ("recall", "violet checkpoint", "--json")),
    Case("recall_probe_hit", ("recall", "violet checkpoint", "--probe")),
    Case("recall_probe_miss", ("recall", MISS, "--probe"), exit_code=1),
    Case("recall_budget", ("recall", "lantern", "--budget", "4000")),
    Case("resume_list", ("resume", "--list")),
    Case("resume_project_non_tty", ("resume", "cedar"), exit_code=1),
    Case("refuse_around_cap", ("around", "{handle}", "--full", "--max-chars", "10"), exit_code=2),
    Case("correct_around_cap", correction_of="refuse_around_cap"),
    Case("refuse_doctor_actions", ("doctor", "--json", "--fix", "--setup"), exit_code=2),
    Case("correct_doctor_actions", correction_of="refuse_doctor_actions"),
    Case("version", ("--version",)),
    Case("status", ()),
)


def _utf8_locale() -> str:
    result = subprocess.run(
        ["locale", "-a"], env={"PATH": "/usr/bin:/bin"}, capture_output=True,
        text=True, encoding="utf-8", timeout=5, check=True)
    available = {name.lower().replace("-", "").replace(".", "")
                 for name in result.stdout.splitlines()}
    for name in ("C.UTF-8", "en_US.UTF-8"):
        if name.lower().replace("-", "").replace(".", "") in available:
            return name
    raise unittest.SkipTest("CLI conformance requires C.UTF-8 or en_US.UTF-8")


def _json_rows(result: subprocess.CompletedProcess[str]) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line]


class ConformanceSandbox:
    """Fresh fixture home; only the ingest binary override is inherited."""

    def __init__(self) -> None:
        utf8_locale = _utf8_locale()
        self._temp = tempfile.TemporaryDirectory(
            prefix="cli-conformance-", dir=os.environ.get("TMPDIR"))
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "home"
        self.data = self.root / "data"
        for name in ("home", "data", "tmp", "config", "cache", "share", "models"):
            (self.root / name).mkdir()
        # Socket paths stop at 104 bytes, which a deep TMPDIR (macOS /var/folders) exceeds.
        self.runtime = Path(tempfile.mkdtemp(prefix="agc-", dir="/tmp"))
        # The host agent's /tmp publication would otherwise become this caller.
        self.callers = {"human": self.root / "no-callers", "agent": self.root / "callers"}
        self.callers["agent"].mkdir(mode=0o700)
        (self.callers["agent"] / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessions": [AGENT_SESSION],
            "updated": int(time.time() * 1000)}), encoding="utf-8")
        for name in ("cedar", "birch", "maple"):
            (self.home / "projects" / name).mkdir(parents=True)
        self.env = {
            "HOME": str(self.home), "AGREP_HOME": str(self.home),
            "AGREP_DATA_DIR": str(self.data), "TMPDIR": str(self.root / "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": utf8_locale,
            "TZ": "UTC", "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1",
            "AGREP_NO_RESIDENT": "1", "AGREP_NO_FETCH": "1",
            "AGREP_CALLER_PUBLICATION_DIR": str(self.callers["human"]),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "AGREP_MODEL_DIR": str(self.root / "models"),
        }
        if "AGREP_RS_BIN" in os.environ:
            self.env["AGREP_RS_BIN"] = os.environ["AGREP_RS_BIN"]
        self._time_replacements: dict[str, str] = {}
        self._materialize()
        (self.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")

    def _materialize(self) -> None:
        origin = datetime.now(timezone.utc).replace(microsecond=0)

        def timestamp(match: re.Match[str]) -> str:
            seconds = sum(int(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                          for amount, unit in re.findall(r"([+-]\d+)([dhms])", match[1]))
            actual = origin + timedelta(seconds=seconds)
            canonical = CANONICAL_TIME + timedelta(seconds=seconds)
            for fmt in ("%Y-%m-%dT%H:%M:%S.000Z", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                self._time_replacements[actual.strftime(fmt)] = canonical.strftime(fmt)
            self._time_replacements[str(int(actual.timestamp() * 1000))] = str(
                int(canonical.timestamp() * 1000))
            return actual.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        for source in sorted((FIXTURES / "store").rglob("*.jsonl")):
            relative = source.relative_to(FIXTURES / "store")
            destination = self.home / ("." + relative.parts[0]) / Path(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = TIME_TEMPLATE.sub(timestamp, source.read_text(encoding="utf-8"))
            content = content.replace("{{home}}", str(self.home))
            if "{{" in content:
                raise AssertionError(f"unexpanded fixture template: {source}")
            destination.write_text(content, encoding="utf-8")

    def run_cli(self, argv, *, cwd=None, stdin=None, env_overrides=None):
        """One subprocess entry seam; an override value of None removes its key."""
        return self.spawn([str(ROOT / "cli.py"), *argv], cwd=cwd, stdin=stdin,
                          env_overrides=env_overrides)

    def spawn(self, command, *, cwd=None, stdin=None, env_overrides=None):
        env = dict(self.env)
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(
            [sys.executable, *command],
            cwd=cwd or self.home, env=env, input="" if stdin is None else stdin,
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=30, check=False)

    def __enter__(self):
        try:
            indexed = self.run_cli(["index"])
            if indexed.returncode:
                raise AssertionError(f"fixture indexing failed:\n{indexed.stdout}{indexed.stderr}")
            ready = self.run_cli(["lantern", "--json", "-n", "0"])
            if ready.returncode:
                raise AssertionError(f"search database not ready:\n{ready.stdout}{ready.stderr}")
            hits = [row for row in _json_rows(ready) if "session" in row]
            if ({row["agent"] for row in hits} != {"claude", "codex", "pi"}
                    or len({row["session"] for row in hits}) != 6):
                raise AssertionError(f"fixture adapters did not expose all six chats:\n{ready.stdout}")
            if not (self.data / "corpus.db").is_file():
                raise AssertionError("index did not synchronously publish the search database")
            return self
        except BaseException:
            self.close()
            raise

    def _background_processes(self) -> set[int]:
        processes = subprocess.run(
            ["ps", "axeww", "-o", "pid=", "-o", "command="],
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, check=True)
        marker = re.compile(r"(?<!\S)AGREP_DATA_DIR=" + re.escape(str(self.data)) + r"(?=\s|$)")
        pids = set()
        for line in processes.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if (len(fields) == 2 and marker.search(fields[1])
                    and re.search(r"(?:indexd|resident|sem_worker)(?:\.py|\b)", fields[1])
                    and int(fields[0]) != os.getpid()):
                pids.add(int(fields[0]))
        return pids

    def close(self) -> None:
        # Alternate execution modes can leave a resident. Match the sandbox
        # environment before each signal, never just a process name or stale PID.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            pids = self._background_processes()
            if not pids:
                break
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 1
            while self._background_processes() and time.monotonic() < deadline:
                time.sleep(0.05)
        remaining = self._background_processes()
        if remaining:
            raise AssertionError(f"sandbox processes survived cleanup: {sorted(remaining)}")
        shutil.rmtree(self.runtime, ignore_errors=True)
        self._temp.cleanup()

    def __exit__(self, *_exc) -> None:
        self.close()

    def normalize(self, text: str) -> str:
        """Keep content/order intact; replace only clocks, paths, and opaque IDs."""
        # A literal "/home/<dir>" would read as a real user's home to the privacy gate.
        text = text.replace(str(self.home), "<HOME>")
        text = text.replace(str(self.root), "<SANDBOX>").replace(str(ROOT), "<REPO>")
        binary = self.env.get("AGREP_RS_BIN")
        if binary:
            text = text.replace(binary, "<AGREP_RS_BIN>")
        # Known fixture timestamps map to one canonical epoch, retaining intervals.
        for actual, canonical in sorted(self._time_replacements.items(), key=lambda item: -len(item[0])):
            text = re.sub(r"(?<!\d)" + re.escape(actual) + r"(?!\d)", canonical, text)
        # Tool-event identities hash timestamps; retain the content digest and span.
        text = re.sub(r"(@[A-Za-z0-9_-]+:\d+\.[0-9a-f]{4})~[0-9a-f]{24}(?=[:\s\"'])",
                      r"\1~<EVENT>", text)
        # Continuation tokens identify per-process snapshots, not hit identity.
        text = re.sub(r"(?<=agrep --deeper )m\.[A-Za-z0-9_-]{8}(?![A-Za-z0-9_-])",
                      "m.<TOKEN>", text)
        text = re.sub(r"(?<=agrep --more )m\.[A-Za-z0-9_-]{8}(?![A-Za-z0-9_-])",
                      "m.<TOKEN>", text)
        text = re.sub(r"\btook \d+(?:\.\d+)?s\b", "took <DURATION>s", text)
        text = re.sub(r"(?<=last indexed )\d+[smhd](?= ago)", "<AGE>", text)
        text = re.sub(r'("(?:corpus_age_s|age_s)"\s*:\s*)\d+(?:\.\d+)?',
                      r'\1"<AGE>"', text)
        # Snapshot/cache growth and filesystem path lengths change diagnostic totals.
        text = re.sub(
            r'("resources":\s*\{"data":\s*\{[^{}]*"files":\s*)\d+(,\s*"bytes":\s*)\d+',
            r'\1"<FILES>"\2"<BYTES>"', text)
        # Version identity changes with code or native build, unlike package version.
        text = re.sub(r"(?<=distribution )[0-9a-f]{12,64}\b", "<BUILD>", text)
        text = re.sub(r"(?<=runtime )[0-9a-f]{12,64}\b", "<BUILD>", text)
        text = re.sub(r"(?<=native )[0-9a-f]{12,64}\b", "<BUILD>", text)
        text = re.sub(r"(?<=writer )[0-9a-f]{12,64}\b", "<BUILD>", text)
        text = re.sub(
            r'("(?:distribution|native_binary|runtime|writer)_build_id"\s*:\s*")[0-9a-f]{12,64}"',
            r'\1<BUILD>"', text)
        return text


def _golden_text(sandbox: ConformanceSandbox, result: subprocess.CompletedProcess[str]) -> str:
    def stream(text: str) -> str:
        normalized = sandbox.normalize(text)
        if normalized and not normalized.endswith("\n"):
            normalized += "\n[no trailing newline]\n"
        return normalized

    return (f"exit: {result.returncode}\n--- stdout ---\n{stream(result.stdout)}"
            f"--- stderr ---\n{stream(result.stderr)}")


def run_matrix(sandbox: ConformanceSandbox, *, run_cli=None) -> dict[str, str]:
    """Run all cases through one swappable runner, returning normalized results."""
    runner = sandbox.run_cli if run_cli is None else run_cli
    found = runner(["violet checkpoint", "--who", "user", "--json", "-n", "1"])
    if found.returncode:
        raise AssertionError(f"handle discovery failed:\n{found.stdout}{found.stderr}")
    hits = [row for row in _json_rows(found) if "session" in row]
    if len(hits) != 1 or not hits[0].get("handle", "").startswith("@"):
        raise AssertionError(f"handle discovery must expose one addressable hit:\n{found.stdout}")
    targets = {"handle": hits[0]["handle"], "session": hits[0]["session"]}
    tool_hit = runner(["toolproof-cedar", "--who", "tool", "--json", "-n", "1"])
    if tool_hit.returncode:
        raise AssertionError(f"tool handle discovery failed:\n{tool_hit.stdout}{tool_hit.stderr}")
    tools = [row for row in _json_rows(tool_hit) if row.get("who") == "tool"]
    if len(tools) != 1 or "~" not in tools[0].get("handle", ""):
        raise AssertionError(f"tool discovery must expose one event-bound handle:\n{tool_hit.stdout}")
    targets["tool_handle"] = tools[0]["handle"]
    raw: dict[str, subprocess.CompletedProcess[str]] = {}
    results = {}
    for case in CASES:
        if case.correction_of:
            refusal = raw[case.correction_of]
            corrections = re.findall(r"; run: (.+)$", refusal.stderr, flags=re.MULTILINE)
            if len(corrections) != 1:
                raise AssertionError(f"{case.correction_of} lacks one correction:\n{refusal.stderr}")
            command = shlex.split(corrections[0])
            if not command or command[0] != "agrep":
                raise AssertionError(f"unsafe correction: {command!r}")
            argv = command[1:]
        else:
            argv = [arg.format_map(targets) for arg in case.argv]
        cwd = sandbox.home / "projects" / case.project_cwd if case.project_cwd else None
        result = runner(argv, cwd=cwd, stdin="", env_overrides=None)
        if result.returncode != case.exit_code:
            raise AssertionError(
                f"{case.name}: expected exit {case.exit_code}, got {result.returncode}\n"
                f"{result.stdout}{result.stderr}")
        raw[case.name] = result
        results[case.name] = _golden_text(sandbox, result)
    return results


@unittest.skipUnless(os.name == "posix", "CLI conformance goldens require POSIX shell and path semantics")
class CLIConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = ConformanceSandbox()
        cls.sandbox.__enter__()
        cls.addClassCleanup(cls.sandbox.close)

    def test_matrix_goldens_and_determinism(self) -> None:
        self.maxDiff = None
        update = os.environ.get("AGREP_UPDATE_CONFORMANCE") == "1"
        for caller in CALLERS:
            runner = caller_runner(self.sandbox, caller)
            first = run_matrix(self.sandbox, run_cli=runner)
            second = run_matrix(self.sandbox, run_cli=runner)
            for case in CASES:
                with self.subTest(caller=caller, case=case.name, contract="determinism"):
                    self.assertEqual(first[case.name], second[case.name])
            if update:
                self.assertEqual(first, second, "refusing to record nondeterministic goldens")
                (GOLDEN / caller).mkdir(parents=True, exist_ok=True)
            for case in CASES:
                path = GOLDEN / caller / f"{case.name}.txt"
                with self.subTest(caller=caller, case=case.name, contract="golden"):
                    if update:
                        path.write_text(first[case.name], encoding="utf-8")
                    self.assertTrue(path.is_file(), f"missing {path}; set AGREP_UPDATE_CONFORMANCE=1")
                    self.assertEqual(path.read_text(encoding="utf-8"), first[case.name])


def caller_runner(sandbox: ConformanceSandbox, caller: str, *, resident: bool = False,
                  served: list | None = None):
    """A run_cli for one caller identity, optionally through the resident server."""
    base = {"AGREP_CALLER_PUBLICATION_DIR": sandbox.callers[caller],
            "AGREP_NO_RESIDENT": None if resident else "1"}

    def run(argv, *, cwd=None, stdin=None, env_overrides=None):
        overrides = {**base, **(env_overrides or {})}
        if not resident or not _resident_allowlisted(argv):
            return sandbox.run_cli(argv, cwd=cwd, stdin=stdin, env_overrides=overrides)
        # A loaded host can miss the 0.25 s acknowledgement once; refusals repeat.
        for _attempt in range(2):
            result = sandbox.spawn(["-c", RESIDENT_CLIENT, *argv], cwd=cwd,
                                   stdin=stdin, env_overrides=overrides)
            if result.returncode != NOT_SERVED:
                served.append(argv)
                return result
            warm_resident(sandbox, caller)
        raise AssertionError(f"resident did not serve {argv!r}:\n{result.stderr}")
    return run


NOT_SERVED = 97
RESIDENT_CLIENT = (
    f"import sys;sys.path[:]={[str(ROOT / 'py'), str(ROOT), *sys.path[1:]]!r};"
    f"import resident;sys.argv[0]={str(ROOT / 'cli.py')!r};"
    f"code=resident.try_run();sys.exit({NOT_SERVED} if code is None else code)"
)


def _resident_allowlisted(argv) -> bool:
    sys.path.insert(0, str(ROOT / "py"))
    try:
        import resident
    finally:
        sys.path.pop(0)
    saved = os.environ.pop("AGREP_NO_RESIDENT", None)
    try:
        return resident.eligible(list(argv))
    finally:
        if saved is not None:
            os.environ["AGREP_NO_RESIDENT"] = saved


def warm_resident(sandbox: ConformanceSandbox, caller: str) -> None:
    overrides = {"AGREP_CALLER_PUBLICATION_DIR": sandbox.callers[caller],
                 "AGREP_NO_RESIDENT": None}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = sandbox.spawn(["-c", RESIDENT_CLIENT, "--version"], env_overrides=overrides)
        if result.returncode != NOT_SERVED:
            if result.returncode:
                raise AssertionError(f"resident --version failed:\n{result.stderr}")
            return
        time.sleep(0.05)
    raise AssertionError(f"resident for the {caller} caller never became ready")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "fork"),
                     "the resident server needs POSIX fork and SCM_RIGHTS")
class ResidentConformance(unittest.TestCase):
    """The fork server must be unobservable, including caller self-exclusion."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = ConformanceSandbox()
        cls.sandbox.__enter__()
        cls.addClassCleanup(cls.sandbox.close)

    def test_resident_matrix_matches_direct_for_human_and_agent_callers(self) -> None:
        self.maxDiff = None
        matrices = {}
        for caller in CALLERS:
            warm_resident(self.sandbox, caller)
            served_argv: list = []
            direct = run_matrix(self.sandbox, run_cli=caller_runner(self.sandbox, caller))
            served = run_matrix(self.sandbox, run_cli=caller_runner(
                self.sandbox, caller, resident=True, served=served_argv))
            for case in CASES:
                with self.subTest(caller=caller, case=case.name):
                    self.assertEqual(served[case.name], direct[case.name])
            self.assertGreaterEqual(len(served_argv), len(CASES) // 2, served_argv)
            matrices[caller] = direct
        self.assertIn(AGENT_SESSION[:8], matrices["human"]["search_word"])
        self.assertNotIn(AGENT_SESSION[:8], matrices["agent"]["search_word"])


if __name__ == "__main__":
    unittest.main()
