"""Process-wide filesystem isolation for focused unittest modules."""

from __future__ import annotations

import atexit
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import time


_TEST_ROOT_ENV = "AGREP_TEST_DATA_ROOT"
_inherited_root = Path(os.environ.get(_TEST_ROOT_ENV, ""))
_temp_root = Path(tempfile.gettempdir())
# Windows multiprocessing re-imports the test module. Reuse only the complete
# sandbox tuple this helper exported ("test", or "env" after events.py import
# normalized it); a lone ambient variable cannot redirect into production data.
_reuse_inherited_root = (
    os.environ.get("AGREP_DATA_DIR_SOURCE") in ("test", "env")
    and _inherited_root.is_absolute()
    and _inherited_root.parent == _temp_root
    and _inherited_root.name.startswith("agrep-unittest-data-")
    and os.environ.get("AGREP_DATA_DIR") == str(_inherited_root / "data")
    and os.environ.get("AGREP_HOME") == str(_inherited_root / "home")
)
if _reuse_inherited_root:
    _DATA = None
    _DATA_ROOT = Path(_inherited_root)
else:
    _DATA = tempfile.TemporaryDirectory(prefix="agrep-unittest-data-")
    _DATA_ROOT = Path(_DATA.name)
    atexit.register(_DATA.cleanup)
    # Any semantic coordination path read against the sandbox mints its
    # per-data-dir namespace in the shared /tmp; it leaves with the sandbox.
    atexit.register(lambda: remove_coordination_namespace(_DATA_ROOT / "data"))
if "AGREP_DATA_DIR" not in os.environ:
    # Any test importing this helper starts in a disposable corpus by default.
    os.environ[_TEST_ROOT_ENV] = str(_DATA_ROOT)
    os.environ["AGREP_DATA_DIR"] = str(_DATA_ROOT / "data")
    os.environ["AGREP_DATA_DIR_SOURCE"] = "test"
    os.environ["AGREP_HOME"] = str(_DATA_ROOT / "home")
    os.environ["AGREP_NO_DAEMON"] = "1"
    os.environ["AGREP_NO_SEM_WORKER"] = "1"


def isolate_data_dir() -> Path:
    """Set the data-dir contract before any product module is imported.

    unittest discovery imports every test module into one interpreter. Keeping
    this owner in a shared module makes import order irrelevant and prevents a
    diagnostic test from ever observing or mutating the developer's real corpus.
    """
    root = _DATA_ROOT / "data"
    register_data_dir(root)
    os.environ[_TEST_ROOT_ENV] = str(_DATA_ROOT)
    os.environ["AGREP_DATA_DIR"] = str(root)
    os.environ["AGREP_DATA_DIR_SOURCE"] = "test"
    # Store discovery is half the contract: pointed at the real home, a
    # background rebuild ingests the developer's transcripts into the sandbox
    # mid-run, and every census and fence after it inherits how far that got.
    home = _DATA_ROOT / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["AGREP_HOME"] = str(home)
    # Background indexers and query-time semantic workers would keep mutating
    # the shared sandbox after their test; suites about either opt back in.
    os.environ["AGREP_NO_DAEMON"] = "1"
    os.environ["AGREP_NO_SEM_WORKER"] = "1"
    # A live agent process above the test runner publishes its caller record;
    # identity tests must see only what they write themselves.
    os.environ["AGREP_CALLER_PUBLICATION_DIR"] = str(_DATA_ROOT / "caller")
    # Tree purity: the venv's editable .pth serves the MAIN checkout's cli.py
    # to worktree runs, which then prepends ITS py/ and mixes two source
    # trees. This tree's root goes first; a crossing resolution is an error.
    tree_root = Path(__file__).resolve().parent.parent
    if str(tree_root) not in sys.path:
        sys.path.insert(0, str(tree_root))
    import importlib.util
    spec = importlib.util.find_spec("cli")
    if spec is not None and spec.origin is not None:
        origin = Path(spec.origin).resolve()
        if origin.parent != tree_root:
            raise RuntimeError(
                f"test isolation would import cli from another tree: {origin}")
    loaded = sys.modules.get("common")
    if loaded is not None and Path(loaded.DATA_DIR) != root:
        raise RuntimeError("test isolation was requested after common initialized real paths")
    return root


def wait_for_next_ctime_tick(path: Path) -> None:
    """Linux stamps ctime from the coarse tick clock; a rewrite in the same tick
    as ``path``'s last change keeps its identity."""
    if os.name == "nt":
        # Windows stat ctime is creation time; change identities there are the USN or a content proof.
        return
    before = os.stat(path).st_ctime_ns
    probe = Path(f"{path}.ctime-probe")
    deadline = time.monotonic() + 5.0
    try:
        while True:
            probe.write_bytes(b"tick")
            if probe.stat().st_ctime_ns > before:
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"ctime never advanced past {path} within 5 s")
            time.sleep(0.001)
    finally:
        probe.unlink(missing_ok=True)


def kill_mid_write(database: Path, statement: str) -> int:
    """SIGKILL a writer once `statement` spilled past its 20-page cache, leaving the hot (synced)
    rollback journal an interrupted index pass leaves; returns the journal's size."""
    import signal
    import subprocess
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sqlite3, sys\n"
         "db = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
         "db.execute('PRAGMA cache_size=20')\n"
         "db.execute('BEGIN IMMEDIATE')\n"
         "db.execute(sys.argv[2])\n"
         "print('ready', flush=True)\n"
         "sys.stdin.read()\n",
         os.fspath(database), statement],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        ready = child.stdout.readline()
        if ready != "ready\n":
            raise AssertionError(f"the writer did not open its transaction: {ready!r}")
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdin.close()
        child.stdout.close()
    journal = Path(f"{database}-journal")
    with journal.open("rb") as stream:
        if stream.read(8) != b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7":
            raise AssertionError("the statement did not spill, so its journal is not hot")
    return journal.stat().st_size


def publish_derived_generation(
        root: Path, rows: list[dict], common_api, corpusdb_api, *,
        signature: str = "test-generation",
        replies: list[dict] | None = None,
        mode: int | None = None) -> Path:
    """Publish the minimal Rust-shaped derived generation used by reader tests."""
    import json

    root.mkdir(parents=True, exist_ok=True)
    messages = root / "messages.jsonl"
    messages.write_text("".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in rows
    ), encoding="utf-8")
    (root / "replies.jsonl").write_text("".join(
        json.dumps(row, separators=(",", ":")) + "\n"
        for row in (replies or [])
    ), encoding="utf-8")
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["session"]), []).append(row)
    sessions = []
    families = []
    for session, session_rows in sorted(grouped.items()):
        first = session_rows[0]
        parent = str(first.get("parent") or "")
        timestamps = [int(row.get("ts") or 0) for row in session_rows]
        sessions.append({
            "session": session, "agent": str(first.get("agent") or ""),
            "project": str(first.get("project") or ""),
            "first_ts": min(timestamps), "last_ts": max(timestamps),
            "n": len(session_rows), "parent": parent,
            "first_text": str(first.get("text") or ""),
        })
        families.append((session, parent))
    (root / "sessions.jsonl").write_text("".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in sessions
    ), encoding="utf-8")
    (root / common_api.SESSION_FAMILY_META_FILE).write_text(json.dumps({
        "version": common_api.SESSION_FAMILY_INDEX_VERSION,
        "algorithm": common_api.SESSION_FAMILY_DIGEST_ALGORITHM,
        "ingest_signature": signature,
        "count": len(families),
        "digest": common_api.session_family_digest(families),
    }, separators=(",", ":")), encoding="utf-8")
    for name, body in (
            ("boundary_stats.json", b"{}"),
            (".boundary_stats.bin", b"fixture"),
            ("event_stats.json", b"{}")):
        (root / name).write_bytes(body)
    if mode is not None:
        for name in corpusdb_api._DERIVED_PROOF_NAMES:
            (root / name).chmod(mode)
    publish_derived_proof(root, corpusdb_api, signature=signature)
    (root / "settings.json").write_text(
        '{"tools":"off"}', encoding="utf-8")
    if mode is not None:
        for name in (".derived_generation.json", ".ingest.sig", "settings.json"):
            (root / name).chmod(mode)
    return messages


def publish_derived_proof(root: Path, corpusdb_api, *, signature: str) -> None:
    """Publish the proof covering the derived files as they are now, then its signature."""
    import json

    proof_rows = []
    for name in corpusdb_api._DERIVED_PROOF_NAMES:
        path = root / name
        identity = corpusdb_api._proof_file_identity(path)
        if corpusdb_api._PLATFORM_NAME == "posix":
            token = {"Metadata": corpusdb_api._unix_change_token(identity[2])}
        elif corpusdb_api._PLATFORM_NAME == "nt":
            token = {
                "ContentSha256": list(
                    corpusdb_api._content_sha256(path, identity))}
        else:
            token = {"Metadata": 0}
        proof_rows.append({
            "name": name, "len": identity[0], "modified_ns": identity[1],
            "change_token": token,
            "edge_hash": corpusdb_api._edge_hash(path, identity[0]),
        })
    (root / ".derived_generation.json").write_text(json.dumps({
        "version": corpusdb_api._DERIVED_PROOF_VERSION,
        "signature": signature, "files": proof_rows,
    }, separators=(",", ":")), encoding="utf-8")
    (root / ".ingest.sig").write_bytes((signature + "\n").encode("utf-8"))


# The real _spawn_indexd, captured by lift_daemon_semantics for the few tests
# whose subject IS the spawn's own fence/guard semantics.
REAL_SPAWN_INDEXD = None


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def lift_daemon_semantics(indexd_runtime):
    """Module-scope lift for suites ABOUT background-process semantics.

    Returns (setUpModule, tearDownModule): the isolation defaults lift for
    exactly the module's run, while _spawn_indexd is stubbed in-flight so
    daemon PROCESSES never are - a real spawn mutates the shared sandbox under
    every later module. Tests about the spawn itself patch over the stub."""
    state = {}

    def set_up_module() -> None:
        from unittest import mock
        global REAL_SPAWN_INDEXD
        state["no_daemon"] = os.environ.pop("AGREP_NO_DAEMON", None)
        state["no_sem_worker"] = os.environ.pop(
            "AGREP_NO_SEM_WORKER", None)
        REAL_SPAWN_INDEXD = indexd_runtime._spawn_indexd
        state["stub"] = mock.patch.object(
            indexd_runtime, "_spawn_indexd",
            return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT)
        try:
            state["stub"].start()
        except BaseException:
            REAL_SPAWN_INDEXD = None
            _restore_env("AGREP_NO_DAEMON", state.get("no_daemon"))
            _restore_env(
                "AGREP_NO_SEM_WORKER", state.get("no_sem_worker"))
            state.clear()
            raise

    def tear_down_module() -> None:
        global REAL_SPAWN_INDEXD
        stub = state.get("stub")
        try:
            if stub is not None:
                stub.stop()
        finally:
            REAL_SPAWN_INDEXD = None
            _restore_env("AGREP_NO_DAEMON", state.get("no_daemon"))
            _restore_env(
                "AGREP_NO_SEM_WORKER", state.get("no_sem_worker"))
            state.clear()

    return set_up_module, tear_down_module


GENEROUS_ROUTINE_BUDGET_S = 60.0

# The same widening for processes a suite starts: a sitecustomize on their PYTHONPATH
# that widens cli's and doctor's routine budgets as they import (mode "generous"), or
# defers one named observation once (mode "shortfall", see test_cli_conformance).
BUDGET_INJECTION = '''
import importlib.abc, importlib.util, os, sys, time
MODE = <MODE>
INJECT = <INJECT>
BUDGET_S = 60.0
STATUS_CAPS = ("_STATUS_ROUTINE_TIMEOUT_S", "_STATUS_STORE_TIMEOUT_S",
               "_STATUS_DETECT_TIMEOUT_S", "_BINARY_IDENTITY_TIMEOUT_S")
DOCTOR_CAPS = ("_DIAGNOSTIC_STORE_TIMEOUT_S", "_DIAGNOSTIC_DETECT_TIMEOUT_S",
               "_DIAGNOSTIC_SENTINEL_TIMEOUT_S", "_DIAGNOSTIC_QUALITY_TIMEOUT_S",
               "_DIAGNOSTIC_ARCHIVE_TIMEOUT_S")


def reached(label):
    open(os.path.join(INJECT, "reached-%s-%d" % (label, os.getpid())), "w").close()


def consume(name):
    try:
        os.unlink(os.path.join(INJECT, "defer-" + name))
    except FileNotFoundError:
        return False
    return True


def widen_status(module, label):
    if getattr(module, "_agrep_generous", False) or not hasattr(module, "_status_deadline"):
        return
    for name in STATUS_CAPS:
        if hasattr(module, name):
            setattr(module, name, BUDGET_S)
    module._agrep_generous = True
    reached(label)


class Budget(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        main = sys.modules.get("__main__")
        if MODE == "generous" and main is not None:
            widen_status(main, "main")
        if name not in ("cli", "doctor", "ownerfile"):
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return None
        loader = spec.loader
        exec_module = loader.exec_module

        def patched(module):
            exec_module(module)
            argv = sys.argv[1:]
            if MODE == "generous":
                if name == "cli":
                    widen_status(module, "cli")
                elif name == "doctor":
                    for cap in DOCTOR_CAPS:
                        setattr(module, cap, BUDGET_S)
                    module._routine_deadline = (
                        lambda *, deep: None if deep else time.monotonic() + BUDGET_S)
                    reached("doctor")
                return
            if name == "ownerfile" and argv == [] and consume("status"):
                snapshot = module.snapshot

                def slow(*args, **kwargs):
                    module.snapshot = snapshot
                    time.sleep(1.0)
                    return snapshot(*args, **kwargs)
                module.snapshot = slow
            if name == "doctor" and argv == ["doctor", "--json"] and consume("doctor"):
                module._routine_deadline = (
                    lambda *, deep: None if deep else time.monotonic())
        loader.exec_module = patched
        return spec


sys.meta_path.insert(0, Budget())
'''


def write_budget_injection(directory: Path, mode: str) -> None:
    (directory / "sitecustomize.py").write_text(
        BUDGET_INJECTION.replace("<MODE>", repr(mode)).replace("<INJECT>", repr(str(directory))),
        encoding="utf-8")




def generous_routine_budget():
    """Module-scope (setUpModule, tearDownModule) widening the routine budgets.

    Bare status and doctor's routine tier observe under 0.8 s deadlines that a
    saturated host spends before the observation a test asserts on; the
    disclosed constants in payloads are left alone. Suites that assert on the
    deferral itself pass an explicit deadline or render synthetic payloads.
    """
    from unittest import mock
    state: dict = {}

    def set_up_module() -> None:
        # Widen whichever surfaces the suite already imported; this helper
        # itself never imports product code (it is a layer-0 leaf).
        cli = sys.modules.get("cli")
        doctor = sys.modules.get("doctor")
        if cli is None and doctor is None:
            raise RuntimeError("generous_routine_budget needs cli or doctor imported first")
        patches = []
        if cli is not None:
            # The binary-identity cap bounds a hashing child, not the routine
            # deadline; tests pin it by value, so it keeps its production figure.
            for name in ("_STATUS_ROUTINE_TIMEOUT_S", "_STATUS_STORE_TIMEOUT_S",
                         "_STATUS_DETECT_TIMEOUT_S"):
                if hasattr(cli, name):
                    patches.append(mock.patch.object(cli, name, GENEROUS_ROUTINE_BUDGET_S))
        if doctor is not None:
            patches.append(mock.patch.object(
                doctor, "_routine_deadline",
                lambda *, deep: None if deep else time.monotonic() + GENEROUS_ROUTINE_BUDGET_S))
            for name in ("_DIAGNOSTIC_STORE_TIMEOUT_S", "_DIAGNOSTIC_DETECT_TIMEOUT_S",
                         "_DIAGNOSTIC_SENTINEL_TIMEOUT_S", "_DIAGNOSTIC_QUALITY_TIMEOUT_S",
                         "_DIAGNOSTIC_ARCHIVE_TIMEOUT_S"):
                if hasattr(doctor, name):
                    patches.append(mock.patch.object(doctor, name, GENEROUS_ROUTINE_BUDGET_S))
        for patch in patches:
            patch.start()
        state["patches"] = patches

    def tear_down_module() -> None:
        for patch in reversed(state.pop("patches", [])):
            patch.stop()

    return set_up_module, tear_down_module


# Semantic worker coordination: one namespace per data dir in a fixed shared base.
# semworker's formula is replicated so the harness can name a sandbox's namespace
# without importing semworker or touching common.DATA_DIR; a test pins them equal.
COORDINATION_NAMESPACE_VERSION = 1


def coordination_base_path() -> Path:
    root = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    return root.resolve(strict=False)


def coordination_namespace_for(data_dir) -> Path:
    import hashlib
    identity = os.path.normcase(os.path.realpath(os.fspath(data_dir)))
    uid_fn = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    uid = str(uid_fn()) if uid_fn is not None else "user"
    digest = hashlib.sha256(
        f"v{COORDINATION_NAMESPACE_VERSION}\0{uid}\0".encode()
        + os.fsencode(identity)).hexdigest()[:20]
    return coordination_base_path() / (
        f"agrep-semantic-v{COORDINATION_NAMESPACE_VERSION}-{uid}-{digest}")


# Every data dir this process handed to a test or a child, by the namespace
# name it would mint, with the module running when it was registered. The sweep
# attributes a leaked namespace only through this registry, so strangers never count.
_CANDIDATE_DATA_DIRS: dict[str, tuple[str, str | None]] = {}
_REGISTRAR: str | None = None


@contextlib.contextmanager
def registering_for(module: str | None):
    """Attribute every data dir registered inside the block to `module`."""
    global _REGISTRAR
    previous = _REGISTRAR
    _REGISTRAR = module
    try:
        yield
    finally:
        _REGISTRAR = previous


def register_data_dir(data_dir) -> Path:
    """Remember a data dir a test may point product code at; returns its namespace."""
    namespace = coordination_namespace_for(data_dir)
    _CANDIDATE_DATA_DIRS.setdefault(namespace.name, (os.fspath(data_dir), _REGISTRAR))
    return namespace


def describe_namespace(name: str) -> str:
    """The namespace with the registered data dir and module that explain it, if any.

    A detached process from an earlier module can mint its data dir's namespace
    while a later module runs; the registration names the culprit either way.
    """
    entry = _CANDIDATE_DATA_DIRS.get(name)
    if entry is None:
        return name
    data_dir, module = entry
    origin = f"data dir {data_dir}"
    if module:
        origin += f", registered during {module}"
    return f"{name} ({origin})"


def register_fixture_tree(root) -> None:
    """Register a temporary tree's root and first-level directories, where every
    sandboxed data dir lives. Call while the tree exists so realpath resolves."""
    root = Path(root)
    register_data_dir(root)
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return
    for child in children:
        register_data_dir(child)


def registered_namespaces() -> set[str]:
    return set(_CANDIDATE_DATA_DIRS)


def remove_coordination_namespace(data_dir) -> bool:
    """Remove the semantic coordination namespace a sandboxed data dir minted.

    Only this uid's EMPTY namespace for exactly ``data_dir`` is removed; a
    namespace holding any record, or any other sibling, is never touched.
    Works whether or not this process imported semworker: child processes
    mint too. Tests call it when they tear down a data dir.
    """
    namespace = register_data_dir(data_dir)
    try:
        if namespace.is_symlink() or not namespace.is_dir():
            return False
        uid_fn = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
        if uid_fn is not None and namespace.lstat().st_uid != uid_fn():
            return False
        namespace.rmdir()
    except OSError:
        return False
    return True


def scrub_coordination_namespaces(root) -> int:
    """Remove the empty namespaces a fixture tree's root and first-level
    directories minted. Returns how many were removed."""
    root = Path(root)
    register_fixture_tree(root)
    candidates = [root]
    try:
        candidates.extend(child for child in root.iterdir() if child.is_dir())
    except OSError:
        pass
    return sum(remove_coordination_namespace(candidate) for candidate in candidates)


@contextlib.contextmanager
def fixture_tree(**kwargs):
    """tempfile.TemporaryDirectory whose namespaces are scrubbed before removal.

    For suites whose temporary trees become data dirs of product processes.
    """
    with tempfile.TemporaryDirectory(**kwargs) as raw:
        try:
            yield raw
        finally:
            scrub_coordination_namespaces(raw)


@contextlib.contextmanager
def daemon_spawns_allowed():
    """Run one test with the isolation default's daemon switch re-enabled.

    isolate_data_dir sets AGREP_NO_DAEMON so an incidental repair kick can
    never leave a live indexd rebuilding the sandbox under later tests. A
    test that is ABOUT spawn semantics declares that here - and still mocks
    or captures _spawn_indexd itself, so nothing real is launched."""
    saved = os.environ.pop("AGREP_NO_DAEMON", None)
    try:
        yield
    finally:
        _restore_env("AGREP_NO_DAEMON", saved)


@contextlib.contextmanager
def without_store_override():
    """Run one test on the discovery branch AGREP_HOME is not taking.

    The override does more than move a root: locators read it as "the caller
    named the home", which turns XDG and platform lookup off. A test about
    those lookups has to say it wants them.
    """
    saved = os.environ.pop("AGREP_HOME", None)
    try:
        yield
    finally:
        _restore_env("AGREP_HOME", saved)
