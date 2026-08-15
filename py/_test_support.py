"""Process-wide filesystem isolation for focused unittest modules."""

from __future__ import annotations

import atexit
import contextlib
import os
from pathlib import Path
import sys
import tempfile


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


def isolate_data_dir() -> Path:
    """Set the data-dir contract before any product module is imported.

    unittest discovery imports every test module into one interpreter. Keeping
    this owner in a shared module makes import order irrelevant and prevents a
    diagnostic test from ever observing or mutating the developer's real corpus.
    """
    root = _DATA_ROOT / "data"
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
    (root / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
    (root / "settings.json").write_text(
        '{"tools":"off"}', encoding="utf-8")
    if mode is not None:
        for name in (".derived_generation.json", ".ingest.sig", "settings.json"):
            (root / name).chmod(mode)
    return messages


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
