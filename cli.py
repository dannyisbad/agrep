#!/usr/bin/env python
"""agrep (agentic grep) - search and explore your cross-agent chat history.

  agrep "race condition"    grep your whole agent history; print matches  (the namesake)
  agrep recall "<query>"    top hits + the chat around each, one byte budget (for agents)
  agrep pack <q> [<q>...]   recall over several queries, deduped, one shared budget
  agrep around <id> <turn>  show the conversation around a search hit, tools inline
  agrep resume <id>         jump back into a past session in its agent, cd'd there
  agrep doctor              check what's installed and what each tier needs
  agrep setup               prefetch optional meaning search + agent instructions/archive
  agrep remove              remove agrep from your agents' instructions
  agrep index               just (re)build the index from your agent stores
  agrep reindex             refresh ingest + search db + embeddings
  agrep tail                follow live agent events as JSON lines
  agrep chats               newest indexed chat history
  agrep board               bounded live-activity window across agents
  agrep ui                  private read-only history explorer with live Board

A bare first argument that isn't a command is treated as a search, so `agrep deadlock`
greps. A recognized command word always invokes that command; use `agrep search <word>`
to search for a word that is also a command. Bare `agrep` prints status + usage.
In a dev checkout the same commands
run as `python cli.py <cmd>`. Run `agrep <command> --help` for a command's own options.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
sys.path.insert(0, str(ROOT / "py"))
# anchor the phase profiler before any product module imports; every cli
# process re-anchors - an inherited value would bill the parent's lifetime
os.environ["AGREP_T0"] = repr(time.perf_counter())


_CORE_EVIDENCE_PATH = """\
CORE EVIDENCE PATH

1. Recover a missing prior fact, decision, artifact, or result:

   agrep recall "<faithful clue-preserving description>" --hits 2 --budget 5000

2. Open zero or one qualifying result at its source:

   agrep around <handle>"""


def _core_evidence_path(cli: str = "agrep") -> str:
    if cli == "agrep":
        return _CORE_EVIDENCE_PATH
    return _CORE_EVIDENCE_PATH.replace("   agrep ", f"   {cli} ")


def _import_safe_text(value: str) -> str:
    return "".join(
        char if char.isprintable() else ascii(char)[1:-1]
        for char in value
    )


try:
    import common  # noqa: E402  -- single source for binary / data / platform paths
    import dist  # noqa: E402
    import indexd_runtime  # noqa: E402
    import legacy_cleanup  # noqa: E402
    import ownerfile  # noqa: E402
    import settings  # noqa: E402
    import surface_policy as surface  # noqa: E402
    from hookless import registry  # noqa: E402
except OSError as exc:
    configured = os.environ.get("AGREP_DATA_DIR")
    expected = (
        f"AGREP_DATA_DIR='{_import_safe_text(configured)}'"
        if configured else
        "the platform user-data directory (or AGREP_DATA_DIR)"
    )
    reason = _import_safe_text(str(exc))
    sys.stderr.write(
        f"agrep failed: data directory unavailable; {expected} must be writable "
        f"({type(exc).__name__}: {reason})\n"
    )
    raise SystemExit(2) from None

INGEST_BIN = common.ingest_bin()
SEMANTIC_INSTALL_COMMAND = dist.semantic_install_command()
SEMANTIC_INSTALL_HINT = dist.semantic_install_hint()
_BINARY_IDENTITY_TIMEOUT_S = 0.25
_BINARY_IDENTITY_STOP_S = 0.05


def _version() -> str:
    """The package version for both installed and source invocations."""
    return dist.package_version()


def _binary_identity_child(binary: str, sender) -> None:
    def send(kind: str, state: str, value: str, detail: str) -> bool:
        try:
            sender.send((
                indexd_runtime.INDEXD_BUILD_ID,
                kind, state, value, detail))
            return True
        except (BrokenPipeError, EOFError, OSError):
            return False

    try:
        for kind, derive in (
                ("writer", lambda: indexd_runtime.derived_writer_build_id(
                    Path(binary), require_binary=True)),
                ("native_binary", lambda: dist.native_binary_build_id(
                    Path(binary)))):
            try:
                value = derive()
                state, detail = "verified", ""
            except Exception as exc:  # noqa: BLE001 -- bounded child diagnosis
                value = ""
                state = "unavailable"
                rendered = common.terminal_safe(exc)[:240]
                detail = f"{type(exc).__name__}: {rendered}"
            if not send(kind, state, value, detail):
                break
    finally:
        sender.close()


def _bounded_binary_identity(binary: Path, *, timeout_s: float) -> dict:
    if timeout_s <= 0.0:
        raise TimeoutError("binary identity deadline expired before hashing")
    import multiprocessing
    method = "spawn" if WIN else "fork"
    context = multiprocessing.get_context(method)
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_binary_identity_child,
        args=(os.fspath(binary), sender),
        name="agrep-binary-identity",
        daemon=True,
    )
    started = False
    observed = {}
    terminal_detail = "binary identity worker exited without a complete result"
    try:
        process.start()
        started = True
        sender.close()
        deadline = time.monotonic() + timeout_s
        while len(observed) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0 or not receiver.poll(remaining):
                terminal_detail = (
                    f"binary identity exceeded {timeout_s:.2f}s deadline")
                break
            try:
                payload = receiver.recv()
            except EOFError:
                break
            if (not isinstance(payload, tuple) or len(payload) != 5
                    or payload[1] not in {"writer", "native_binary"}
                    or payload[2] not in {"verified", "unavailable"}
                    or payload[1] in observed):
                terminal_detail = "binary identity worker returned an invalid result"
                break
            if payload[0] != indexd_runtime.INDEXD_BUILD_ID:
                raise OSError(
                    "Python runtime changed while deriving writer identity")
            observed[payload[1]] = payload[2:]
    finally:
        receiver.close()
        sender.close()
        if started:
            process.join(0)
            if process.is_alive():
                process.terminate()
                process.join(_BINARY_IDENTITY_STOP_S)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(_BINARY_IDENTITY_STOP_S)
            if not process.is_alive():
                process.close()
    if not observed:
        if "exceeded" in terminal_detail:
            raise TimeoutError(terminal_detail)
        raise OSError(terminal_detail)
    fields = {}
    for prefix in ("writer", "native_binary"):
        state, value, detail = observed.get(
            prefix, ("unavailable", "", terminal_detail))
        value = str(value)
        if state == "verified" and (
                len(value) != 20
                or any(char not in "0123456789abcdef" for char in value)):
            state = "unavailable"
            detail = (
                f"{prefix.replace('_', ' ')} identity worker returned "
                "an invalid build id")
        fields[f"{prefix}_build_id"] = (
            value if state == "verified" else None)
        fields[f"{prefix}_build_state"] = state
        if state != "verified":
            fields[f"{prefix}_build_detail"] = str(detail) or (
                f"{prefix.replace('_', ' ')} identity is unavailable")
    return fields


def _build_identity(*, timeout_s: float = _BINARY_IDENTITY_TIMEOUT_S) -> dict:
    identity = {
        "distribution_build_id": None,
        "distribution_build_state": "unavailable",
        "runtime_build_id": indexd_runtime.INDEXD_BUILD_ID,
        "native_binary_build_id": None,
        "native_binary_build_state": "unavailable",
        "writer_build_id": None,
        "writer_build_state": "unavailable",
    }
    try:
        identity["distribution_build_id"] = common.distribution_build_id()
    except Exception as exc:  # noqa: BLE001 -- identity is diagnostic, not execution
        identity["distribution_build_detail"] = (
            f"{type(exc).__name__}: {common.terminal_safe(exc)}")
    else:
        identity["distribution_build_state"] = "verified"
    try:
        identity.update(_bounded_binary_identity(
            common.ingest_bin(), timeout_s=timeout_s))
    except Exception as exc:  # noqa: BLE001 -- identity is diagnostic, not execution
        rendered = f"{type(exc).__name__}: {common.terminal_safe(exc)}"
        identity["native_binary_build_detail"] = rendered
        identity["writer_build_detail"] = rendered
    return identity


def _version_text() -> str:
    identity = _build_identity()
    distribution = identity["distribution_build_id"] or "unavailable"
    native = identity["native_binary_build_id"] or "unavailable"
    writer = identity["writer_build_id"] or "unavailable"
    return (f"agrep {_version()} distribution {distribution} "
            f"runtime {identity['runtime_build_id']} "
            f"native {native} "
            f"writer {writer}")


class _VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(_version_text() + "\n", sys.stdout)
        parser.exit(0)


def _source_checkout() -> bool:
    """A distributable install is not a buildable Cargo workspace."""
    return (ROOT / "Cargo.toml").is_file() and (ROOT / "crates").is_dir()


def _ensure_binary() -> bool:
    """Make the ingest binary available, or explain (honestly) why we can't. Resolution:
    already present -> done; source checkout + rust -> build; else offer to fetch the
    prebuilt binary; else route to doctor. Re-resolves common.ingest_bin() after each step."""
    if common.ingest_bin().exists():
        return True
    import shutil
    if _source_checkout() and shutil.which("cargo"):
        print("=== first run: building the ingest binary (cargo build --release) ===", flush=True)
        if subprocess.run(["cargo", "build", "--release"], cwd=str(ROOT)).returncode == 0 \
                and common.ingest_bin().exists():
            return True
    fetched = common.fetch_binary()
    if fetched and fetched.exists():
        return True
    cli = common.cli_name()
    print(f"  ! no ingest binary, and couldn't build or fetch one. `{cli} doctor` shows the "
          f"options (install Rust from https://rustup.rs, or set AGREP_BIN_URL to a mirror).")
    return False


def _index() -> bool:
    # The ingest invocation and derived-db refresh are shared with first-search indexing.
    print("=== indexing transcripts ===", flush=True)
    ok = indexd_runtime.build_index(require_search_index=True)
    # The CLI profiler owns the end-to-end duration; this names its index phase.
    common.lap("index")
    return ok


# --- status (bare `agrep`) ------------------------------------------------

# same palette as search/around/doctor (common owns it)
_paint = common.paint
_meter = common.meter
_STATUS_ROUTINE_TIMEOUT_S = 0.80
_STATUS_STORE_TIMEOUT_S = 0.20
_STATUS_DETECT_TIMEOUT_S = 0.15
_STATUS_TEACH_MAX_BYTES = 64 * 1024


def _status_deadline() -> float:
    return time.monotonic() + _STATUS_ROUTINE_TIMEOUT_S


def _status_remaining(
        deadline: float | None, local_cap_s: float) -> float:
    cap = max(0.0, float(local_cap_s))
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - time.monotonic()))


def _status_defer(d: dict, label: str, detail: str) -> None:
    diagnostics = d["diagnostics"]
    if label not in diagnostics["deferred"]:
        diagnostics["deferred"].append(label)
    diagnostics["state"] = "partial"
    diagnostics.setdefault("details", {})[label] = detail


def _status_instruction_enrollment(*, deadline: float) -> dict:
    if time.monotonic() >= deadline:
        return {
            "state": "status-deferred", "taught": None,
            "detail": (
                "routine budget expired before instruction enrollment metadata"),
        }
    path = common.DATA_DIR / "teach.json"
    try:
        raw = ownerfile.snapshot(
            path, max_bytes=_STATUS_TEACH_MAX_BYTES).raw
    except FileNotFoundError:
        return {"state": "unenrolled", "taught": False, "targets": 0}
    except OSError as exc:
        return {
            "state": "unavailable", "taught": None,
            "detail": (
                "instruction enrollment is unavailable "
                f"({type(exc).__name__}: {exc})"),
        }
    if time.monotonic() >= deadline:
        return {
            "state": "status-deferred", "taught": None,
            "detail": (
                "routine budget expired while reading instruction enrollment"),
        }
    try:
        value = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeError, ValueError) as exc:
        return {
            "state": "unavailable", "taught": None,
            "detail": (
                "instruction enrollment is malformed "
                f"({type(exc).__name__}: {exc})"),
        }
    if time.monotonic() >= deadline:
        return {
            "state": "status-deferred", "taught": None,
            "detail": (
                "routine budget expired while validating instruction enrollment"),
        }
    targets = value.get("targets") if isinstance(value, dict) else None
    if (not isinstance(targets, list)
            or not all(isinstance(item, str) and item for item in targets)):
        return {
            "state": "unavailable", "taught": None,
            "detail": "instruction enrollment has an invalid schema",
        }
    return {
        "state": "enrolled" if targets else "unenrolled",
        "taught": bool(targets), "targets": len(targets),
    }


def _fmt_age(seconds: float) -> str:
    """Compact human age: '3s', '12m', '5h', '2d' ago. Coarse on purpose - the
    status line wants a glance, not a stopwatch."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _status_core(*, deadline: float | None = None,
                 writer_build_id: str | None = None) -> dict:
    """The fast half of the status probe: sessions.jsonl summary (never the
    ~50 MB messages.jsonl), db/teach stats, and freshness observations. Every
    potentially scaling read shares one routine deadline."""
    deadline = _status_deadline() if deadline is None else deadline
    d: dict = {
        "version": _version(),
        "data_dir": str(common.DATA_DIR),
        "data_dir_source": common.data_dir_source(),
        "warnings": list(common.data_dir_warnings()),
        "index_built": None,
        "index_state": "not-verified",
        "semantic_optional": True,
        "semantic_install_hint": None,
        "diagnostics": {
            "tier": "routine", "state": "complete",
            "budget_s": _STATUS_ROUTINE_TIMEOUT_S,
            "deferred": [],
        },
    }
    embeddings_setting = settings.setting_observation("embeddings")
    d["embeddings_setting"] = embeddings_setting
    if embeddings_setting.get("state") != "verified":
        _status_defer(
            d, "embeddings setting",
            str(embeddings_setting.get("detail")
                or "embeddings setting is unavailable"))
    if _status_remaining(deadline, _STATUS_ROUTINE_TIMEOUT_S) > 0.0:
        resource_args = {"observe_only": True, "include_rss": False}
        if writer_build_id is not None:
            resource_args["current_writer_id"] = writer_build_id
        daemon = indexd_runtime.indexd_resource_status(**resource_args)
        if daemon.get("running") and daemon.get("rss_state") == "not-inspected":
            _status_defer(
                d, "daemon RSS",
                "routine status omits the potentially blocking RSS subprocess")
    else:
        detail = "routine budget expired before daemon resource observation"
        daemon = {
            "running": False, "state": "status-deferred", "detail": detail}
        _status_defer(d, "daemon resource observation", detail)
    d["daemon"] = daemon
    writer_active = bool(daemon.get("running") or daemon.get("starting"))
    store_timeout = _status_remaining(deadline, _STATUS_STORE_TIMEOUT_S)
    if store_timeout > 0.0:
        _stores, drift_report = indexd_runtime.observe_store_drift(
            timeout_s=store_timeout)
        if _stores is None:
            _status_defer(
                d, "store freshness",
                drift_report.detail or "live store census returned no verdict")
    else:
        detail = "routine budget expired before live store freshness census"
        _stores = None
        drift_report = indexd_runtime.DriftReport(
            "unknown", code="diagnostic-budget-exceeded", detail=detail)
        _status_defer(d, "store freshness", detail)
    failure = indexd_runtime.indexing_failure(
        daemon_status=daemon, drift_report=drift_report)
    d["freshness"] = indexd_runtime.machine_freshness(
        checked=_stores is not None, failure=failure,
        drift_report=drift_report)
    if failure is not None:
        if failure.code == "diagnostic-budget-exceeded":
            _status_defer(d, "freshness verdict", failure.reason)
        else:
            # surface owns the translation; the raw code and reason stay in
            # d["freshness"] for machines that want the mechanism
            line = surface.indexing_advice_line(failure, common.cli_name())
            if line:
                d["warnings"].append(line)
    try:
        summary = common.index_summary(deadline=deadline)
    except TimeoutError:
        summary = None
        d["index_state"] = "status-deferred"
        _status_defer(
            d, "index summary",
            "routine budget expired before the proof-bound index census completed")
    if summary is None:
        candidates = (
            common.DATA_DIR / "sessions.jsonl",
            common.MESSAGES_PATH,
            common.DATA_DIR / ".ingest.sig",
        )
        any_present = False
        for path in candidates:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                any_present = True
                break
            else:
                any_present = True
                break
        if writer_active:
            d["index_state"] = "status-deferred"
            _status_defer(
                d, "index summary",
                "the published summary is moving while its writer is active")
        elif not any_present and time.monotonic() < deadline:
            d.update(index_built=False, index_state="never-built")
        elif time.monotonic() >= deadline:
            d["index_state"] = "status-deferred"
            _status_defer(
                d, "index summary",
                "routine budget expired before the index summary was read")
        elif "index summary" not in d["diagnostics"]["deferred"]:
            _status_defer(
                d, "index summary",
                "published index evidence exists but its census was not verified")
    else:
        d.update(
            index_built=True,
            index_state="ready",
            sessions=summary["sessions"],
            messages=summary["messages"],
            agents=summary["agents"],
            per_agent=summary["per_agent"],
        )
        if "age_s" in summary:
            d["last_indexed_age_s"] = summary["age_s"]

    if time.monotonic() < deadline:
        db = common.DATA_DIR / "corpus.db"
        try:
            db_info = db.lstat()
        except FileNotFoundError:
            if writer_active:
                d["search_index_ready"] = None
                d["search_index_state"] = "status-deferred"
                d["index_state"] = "status-deferred"
                _status_defer(
                    d, "search database readiness",
                    "the search database is moving while its writer is active")
            else:
                d["search_index_ready"] = False
                d["search_index_state"] = "missing"
        except OSError as exc:
            d["search_index_ready"] = None
            d["search_index_state"] = "unavailable"
            # the state stays coarse for machines; the defect names which of
            # the three unusable shapes it is, so the render can be specific
            d["search_index_defect"] = "unreadable"
            _status_defer(
                d, "search index presence",
                f"search database metadata is unavailable ({type(exc).__name__})")
        else:
            reparse = getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (not stat.S_ISREG(db_info.st_mode)
                    or bool(getattr(
                        db_info, "st_file_attributes", 0) & reparse)):
                d["search_index_ready"] = None
                d["search_index_state"] = "unavailable"
                d["search_index_defect"] = "not-a-file"
                _status_defer(
                    d, "search index presence",
                    "search database entry is not a regular file")
            elif db_info.st_size <= 0:
                d["search_index_ready"] = None
                d["search_index_state"] = "unavailable"
                d["search_index_defect"] = "empty"
                _status_defer(
                    d, "search database readiness",
                    "search database exists but is empty; it is not a verified "
                    "missing or ready index")
            else:
                # ~14ms of metadata reads behind a clone-bounded open. Skipping
                # them is what let this surface answer "ready" while the deep
                # tier called the same database unreadable.
                import doctor
                readiness = doctor._corpus_db_readiness()
                state = str(readiness.get("state") or "unavailable")
                code = str(readiness.get("code") or "")
                d["search_index_ready"] = state == "ready"
                d["search_index_state"] = state
                if code:
                    d["search_index_code"] = code
                moved_after_presence = state == "missing"
                if writer_active and state != "ready" and not moved_after_presence:
                    try:
                        after_info = db.lstat()
                    except FileNotFoundError:
                        moved_after_presence = True
                    except OSError:
                        pass
                    else:
                        before_identity = (
                            db_info.st_dev, db_info.st_ino, db_info.st_mode,
                            db_info.st_size, db_info.st_mtime_ns)
                        after_identity = (
                            after_info.st_dev, after_info.st_ino,
                            after_info.st_mode, after_info.st_size,
                            after_info.st_mtime_ns)
                        moved_after_presence = before_identity != after_identity
                if writer_active and moved_after_presence:
                    d["search_index_ready"] = None
                    d["search_index_state"] = "status-deferred"
                    d["index_state"] = "status-deferred"
                    _status_defer(
                        d, "search database readiness",
                        "the search database moved between presence and "
                        "readiness checks while its writer was active")
                elif summary is None and writer_active:
                    import corpusdb
                    if (corpusdb.state_self_clears(code)
                            or state in {"stale", "rebuild-pending", "busy",
                                         "owned-elsewhere", "status-deferred"}):
                        d["search_index_ready"] = None
                        d["search_index_state"] = "status-deferred"
                if not doctor._concluded(readiness):
                    d["search_index_ready"] = None
                    _status_defer(
                        d, "search database readiness",
                        "the search database verdict was not reached")
        enrollment = _status_instruction_enrollment(deadline=deadline)
        d["instruction_enrollment"] = enrollment
        d["agents_taught"] = enrollment.get("taught")
        if enrollment.get("state") in ("unavailable", "status-deferred"):
            _status_defer(
                d, "instruction enrollment",
                str(enrollment.get("detail")
                    or "instruction enrollment was not verified"))
    else:
        d["search_index_ready"] = None
        d["search_index_state"] = "status-deferred"
        d["agents_taught"] = None
        d["instruction_enrollment"] = {
            "state": "status-deferred", "taught": None,
            "detail": (
                "routine budget expired before instruction enrollment metadata"),
        }
        _status_defer(
            d, "search index presence",
            "routine budget expired before search database metadata")
        _status_defer(
            d, "instruction enrollment",
            "routine budget expired before instruction enrollment metadata")

    detect_timeout = _status_remaining(deadline, _STATUS_DETECT_TIMEOUT_S)
    if detect_timeout > 0.0:
        observation: dict = {}
        d["detected_not_indexed"] = common.detected_stores(
            timeout_s=detect_timeout, observation=observation)
        if observation.get("state") != "complete":
            _status_defer(
                d, "unsupported-store detection",
                str(observation.get("detail") or
                    "ingest-registry detection returned no verdict"))
    else:
        d["detected_not_indexed"] = []
        _status_defer(
            d, "unsupported-store detection",
            "routine budget expired before ingest-registry detection")
    # "ready" is read by agents as "searches work here". A published corpus
    # whose search database is stale, missing or unreadable does not, so the
    # census verdict is capped by the database that serves the queries.
    if d["index_state"] == "ready" and d["search_index_state"] != "ready":
        d["index_state"] = (
            d["search_index_state"]
            if d.get("search_index_ready") is False else "not-verified")
    return d


def _status_semantic(*, deadline: float | None = None) -> dict:
    """Import-free semantic capability observation for routine status.

    Doctor --deep owns runtime, model, and embedding verification; this reports
    only what can be known without importing any of it.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return {
            "semantic_deps": None,
            "semantic_verified": False,
            "semantic_status": "status-deferred",
            "semantic_state": "not-verified",
            "semantic_ready": None,
            "semantic_embedding_now": None,
        }
    return {
        "semantic_deps": None,
        "semantic_verified": False,
        "semantic_status": "not-inspected",
        "semantic_state": "not-verified",
        "semantic_ready": None,
        "semantic_embedding_now": None,
    }


def _kick_repair_if_damaged(
        d: dict) -> indexd_runtime.RepairKick | None:
    """A damaged verdict leaves a daemon running behind it.

    Status observes and does not rebuild - that much of the old contract holds,
    and nothing here prints, blocks, or mutates what the reader came to inspect.
    What it may not do is see a fault agrep repairs by itself, stay silent about
    repairing it, and leave: a reader who only ever runs `agrep status` would
    then be shown the same damage forever and never once have it fixed.

    A healthy box never reaches the plan; the verdict just above already said
    ready, so the fast path costs nothing."""
    if d.get("index_state") == "ready" and d.get("search_index_state") == "ready":
        return None
    try:
        import corpusdb
        import indexd_runtime
        # The states already observed above, never a second probe: re-deriving
        # the verdict here would make the diagnostic pay twice to learn what it
        # just printed.
        if (str(d.get("index_state") or "")
                in corpusdb.SELF_REPAIRING_CORPUS_STATES
                or str(d.get("search_index_state") or "")
                in corpusdb.SELF_REPAIRING_DB_STATES):
            return indexd_runtime.kick_background_repair()
    except Exception:  # noqa: BLE001 - a kick never fails a diagnostic
        pass
    return None


def _status_data() -> dict:
    """The full machine-readable summary (`agrep status --json`), one shot.
    Bare status deliberately does not import the native ONNX runtime; `doctor`
    performs that stronger availability check."""
    deadline = _status_deadline()
    binary = common.ingest_bin()
    identity = _build_identity(
        timeout_s=_status_remaining(deadline, _BINARY_IDENTITY_TIMEOUT_S))
    detail = str(identity.get("writer_build_detail") or "")
    with indexd_runtime.use_observed_writer_build_id(
            binary, identity.get("writer_build_id"), detail):
        d = _status_core(
            deadline=deadline,
            writer_build_id=str(identity.get("writer_build_id") or ""))
        d.update(identity)
        semantic = _status_semantic(deadline=deadline)
        d.update(semantic)
        if semantic.get("semantic_status") == "status-deferred":
            _status_defer(
                d, "semantic capability",
                "routine budget expired before optional-dependency discovery")
        elif not semantic.get("semantic_verified"):
            _status_defer(
                d, "semantic runtime and index",
                "routine status does not discover optional packages, load native "
                "code, or scan semantic artifacts")
        _kick_repair_if_damaged(d)
    return d


def _status_lines(cli: str, color: bool = False):
    """Human render for bare `agrep`; all observations share one deadline."""
    deadline = _status_deadline()
    binary = common.ingest_bin()
    identity = _build_identity(
        timeout_s=_status_remaining(deadline, _BINARY_IDENTITY_TIMEOUT_S))
    detail = str(identity.get("writer_build_detail") or "")
    with indexd_runtime.use_observed_writer_build_id(
            binary, identity.get("writer_build_id"), detail):
        d = _status_core(
            deadline=deadline,
            writer_build_id=str(identity.get("writer_build_id") or ""))
        repair = _kick_repair_if_damaged(d)
    d.setdefault(
        "diagnostics",
        {"tier": "routine", "state": "complete",
         "budget_s": _STATUS_ROUTINE_TIMEOUT_S, "deferred": []},
    )
    repairing = bool(repair is not None and repair.in_flight)
    # the default path explains nothing; a redirected one explains why the
    # corpus below looks unfamiliar
    if str(d.get("data_dir_source") or "default") != "default":
        yield _paint("d", f"  data dir: {d['data_dir']} "
                          f"({d['data_dir_source']})", color)
    # the corpus block is what the reader came for and never moves below a
    # problem; problems follow it, one line each, each ending in a command
    if d["index_built"]:
        age = ""
        if "last_indexed_age_s" in d:
            s = d["last_indexed_age_s"]
            age = f" · last indexed {_fmt_age(s)} ago"
        yield f"  {d['messages']:,} messages · {d['sessions']:,} sessions{age}"
        pad = max((len(a["agent"]) for a in d.get("per_agent", [])), default=0)
        for a in d.get("per_agent", []):
            name = _paint("a", a["agent"].ljust(pad), color)
            yield (
                f"    {name}  {a['messages']:,} messages · "
                f"{a['sessions']:,} sessions")
        if not d.get("messages"):
            yield (f"  indexed, but no chats were found: `{cli} doctor` lists "
                   "the folders agrep looked in")
    elif d["index_built"] is False:
        yield (f"  no index yet - `{cli} setup` makes this machine's agent "
               "history searchable")
    elif (d.get("index_state") != "status-deferred"
          and str(d.get("search_index_state") or "") not in {
              "missing", "stale", "rebuild-pending", "corrupt", "unavailable",
              "owned-elsewhere",
          }):
        # a summary read that merely ran out of time learned nothing, so it
        # says nothing; damaged evidence is what the reader can act on
        yield (f"  the index is unreadable, so counts cannot be trusted: "
               f"`{cli} index --full`")
    for warning in d["warnings"]:
        yield f"  {warning}"
    freshness_value = d.get("freshness")
    freshness = freshness_value if isinstance(freshness_value, dict) else {}
    # drift a background writer is converging needs no line - it ends without
    # the reader; drift nothing will absorb is one line with the command
    if (freshness.get("state") == "index-behind"
            and not (d.get("daemon") or {}).get("running")):
        yield (f"  new chats since the last index are not searchable yet: "
               f"`{cli} index`")
    # a present index is the unremarkable case and gets no line; the four
    # unusable shapes each get one, with the command that ends them
    defect = str(d.get("search_index_defect") or "")
    search_state = str(d.get("search_index_state") or "")
    foreign_owner = (
        search_state == "owned-elsewhere"
        or str(d.get("index_state") or "") == "owned-elsewhere"
        or bool((d.get("daemon") or {}).get("blocked"))
    )
    if d["index_built"] is False:
        pass
    elif repairing and (foreign_owner or search_state in {
            "missing", "stale", "rebuild-pending", "corrupt", "unavailable"}):
        pass
    elif foreign_owner:
        yield "  " + _paint(
            "y", "another agrep version is using the search index: "
            f"`{cli} doctor`", color)
    elif search_state == "missing":
        yield "  " + _paint(
            "y", f"the search index is missing: `{cli} index`", color)
    elif defect == "empty":
        yield "  " + _paint(
            "y", f"the search index is empty: `{cli} index`", color)
    elif defect == "not-a-file":
        yield "  " + _paint(
            "y",
            f"the search index is not a usable file: `{cli} index --full`",
            color)
    elif defect == "unreadable":
        yield "  " + _paint(
            "y", f"the search index cannot be read: `{cli} doctor`", color)
    elif search_state in {"stale", "rebuild-pending"}:
        yield "  " + _paint(
            "y", f"the search index is out of date: `{cli} index`", color)
    elif search_state == "corrupt":
        yield "  " + _paint(
            "y", f"the search index is damaged: `{cli} index --full`", color)
    elif search_state == "unavailable":
        yield "  " + _paint(
            "y", f"the search index cannot be read: `{cli} doctor`", color)

    semantic = _status_semantic(deadline=deadline)
    d.update(semantic)
    if semantic.get("semantic_status") == "status-deferred":
        _status_defer(
            d, "semantic capability",
            "routine budget expired before optional-dependency discovery")
    elif not semantic.get("semantic_verified"):
        _status_defer(
            d, "semantic runtime and index",
            "routine status does not discover optional packages, load native "
            "code, or scan semantic artifacts")
    setting_observation = d.get("embeddings_setting") or {
        "state": "unavailable", "value": None}
    setting = (
        setting_observation.get("value")
        if setting_observation.get("state") == "verified" else None)
    governor_deferral = (
        surface.observed_semantic_deferral(
            common.available_memory_fraction,
            common.battery_state,
            common.host_cpu_fraction,
        )
        if (d.get("semantic_deps") and d.get("semantic_verified")
            and not d.get("semantic_ready") and setting != "off")
        else None
    )
    sem = ""
    if d.get("semantic_deps") is False:
        sem = ("meaning search is off - optional dependencies are not "
               f"installed. enable: {SEMANTIC_INSTALL_HINT}")
    elif (d.get("semantic_status") == "status-deferred"
            or d.get("semantic_deps") is None or setting is None
            or setting == "off" or not d.get("semantic_verified")):
        # nothing about meaning search was observed here, and a working
        # keyword search is not news - the routine tier stays quiet about both
        sem = ""
    elif d.get("semantic_embedding_now"):
        cov = d.get("semantic_coverage") or {}
        done, total = cov.get("indexed"), cov.get("total")
        # the worker's own counters beat published coverage: vectors land on disk
        # only at the end of a pass, so during a first build "published" reads 0
        # for the whole run while the worker is chewing through thousands of rows
        if d.get("embed_total"):
            total = total or d["embed_total"]
            done = min(total, (done or 0) + d.get("embed_done", 0))
        # the meaning index embeds every non-tool row (subagent/control/recap too),
        # a larger set than the banner's prose "messages" - so say rows, not messages
        if done is not None and total:
            pct = f"{100 * done // total}%"
            # Without live counters, "building" can be a kick the governor will
            # decline; render the pause instead of work that is not happening.
            if governor_deferral and not d.get("embed_total"):
                at = f" at {pct} ({done:,}/{total:,} rows)" if done else ""
                sem = (f"meaning search {_paint('y', 'is paused', color)}{at} - "
                       f"{governor_deferral.surface_reason}; resumes automatically")
            elif not done:
                sem = f"meaning search is building - {total:,} rows to embed"
            elif done / total >= 0.99:
                # an active box is nearly always a few fresh turns behind; that
                # is current with a top-up in flight, not a build
                sem = ""
            else:
                sem = (f"meaning search is building "
                       f"{_paint('g', _meter(done, total), color)} {pct} · "
                       f"{done:,}/{total:,} rows"
                       if color else
                       f"meaning search is building "
                       f"({done:,}/{total:,} rows, {pct})")
        else:
            sem = (f"meaning search {_paint('y', 'is paused', color)} - "
                   f"{governor_deferral.surface_reason}; resumes automatically"
                   if governor_deferral and not d.get("embed_total")
                   else "meaning search is building")
    elif d.get("semantic_kick_state") == "failed":
        retry = d.get("semantic_retry_in_s")
        when = (f"in ~{max(1, retry // 60)}m" if retry and retry > 60
                else "shortly" if retry is not None else "automatically")
        sem = (f"meaning search {_paint('y', 'failed to build', color)} - "
               f"retries {when}; keyword search is unaffected")
    elif governor_deferral:
        sem = (f"meaning search {_paint('y', 'is paused', color)} - "
               f"{governor_deferral.surface_reason}; resumes automatically")
    else:
        # ready, never built, or repairing: all end without the reader
        sem = ""
    if sem:
        yield f"  {sem}"
    det = d.get("detected_not_indexed") or []
    if det:
        names = ", ".join(f"{x['name']} ({x['count']})" for x in det)
        yield f"  found but not supported yet: {names}"
    if d.get("agents_taught") is False:
        # the entire value channel: no model has an agrep prior, so an agent
        # never reaches for it until its instructions say it exists
        yield ("  your agents " + _paint("y", "do not know about agrep yet", color)
               + f": `{cli} setup`")


def cmd_status(a) -> int:
    """Bare `agrep` orients (state + examples). Explicit `agrep status` IS
    `agrep doctor` - one diagnostic surface, two names. `--json` stays the cheap
    machine summary (doctor --json is the deep probe)."""
    common.utf8_stdio()
    if getattr(a, "json", False):
        rest = getattr(a, "rest", None) or []
        if rest:
            raw_rest = getattr(a, "raw_rest", None) or rest
            return surface.argument_error(
                "agrep status",
                f"--json cannot be combined with {rest[0]}",
                argv=raw_rest, search_word="status")
        print(json.dumps(_status_data(), ensure_ascii=False))
        return 0
    if getattr(a, "fn", None) is not None:  # explicit `agrep status`
        import doctor
        return doctor.main(a.rest)
    cli = common.cli_name()  # `python cli.py` in a dev checkout, `agrep` once installed
    color = common.color_enabled(sys.stdout)
    print(_paint("hd", "agrep (agentic grep) - search and explore your "
                       "cross-agent chat history", color) + "\n", flush=True)
    for line in _status_lines(cli, color):
        print(line)
    print("\ntry:")
    print(f'  {cli} "race condition"        grep every agent for a phrase')
    print(f"  {cli} deadlock --agent codex  filter to one agent")
    print(f"  {cli} search index            search a word that is also a command")
    print(f'  {cli} -E "TODO|FIXME"         regex search')
    print(f"  {cli} -l auth                 which chats mention it")
    print(f"  {cli} recall \"topic\" --probe   one-line pointer to past sessions (cheap)")
    print(f"  {cli} recall \"topic\"          the conversations themselves, budget-capped")
    print(f"  {cli} around <id> <turn>      the conversation around a hit")
    print(f"  {cli} resume <id>             reopen a past session in its agent")
    return 0


# --- subcommands ----------------------------------------------------------


def cmd_index(a) -> int:
    if getattr(a, "rest", None):
        raw_rest = getattr(a, "raw_rest", None) or a.rest
        return surface.argument_error(
            "agrep index", f"unrecognized argument: {a.rest[0]}",
            argv=raw_rest, search_word="index")
    if common.data_dir_readonly(common.DATA_DIR):
        common.log(
            "index skipped: AGREP_DATA_READONLY protects this data directory")
        return 1
    if not _ensure_binary():
        return 1
    if getattr(a, "full", False):
        print("=== indexing transcripts ===", flush=True)
        # cold-cache reparse of every store file (also reseeds the intake book)
        ingest = common.ingest_bin()
        r = subprocess.run(
            [str(ingest), "index", "--agent", "all", "--full"],
            cwd=str(ROOT),
            env=indexd_runtime.rust_writer_env(ingest))
        if r.returncode != 0:
            common.lap("index")
            return r.returncode
        if indexd_runtime.explicit_index_declined():
            common.lap("index")
            return 1
        ok = indexd_runtime.hand_off_search_index()
        common.lap("index")
        return 0 if ok else 1
    return 0 if _index() else 1


def cmd_reindex(a) -> int:
    return subprocess.run([sys.executable, str(ROOT / "reindex.py"), *a.rest],
                          cwd=str(ROOT)).returncode


def cmd_audit(a) -> int:
    import audit
    return audit.main(a.rest)


def cmd_doctor(a) -> int:
    import doctor
    if "-h" in a.rest or "--help" in a.rest:
        print(doctor.__doc__.strip())
        return 0
    unknown = [value for value in a.rest
               if value not in {
                   "--json", "--fix", "--setup", "--deep", "--no-semantic"}]
    if unknown:
        return surface.argument_error(
            "agrep doctor", f"unrecognized argument: {unknown[0]}",
            argv=a.rest, search_word="doctor")
    return doctor.main(a.rest)


def _setup_archive(choice: bool | None, *, assume_yes: bool = False) -> None:
    """Consent-gated archive retention; an existing explicit choice is stable."""
    import archive

    if archive.CONFIG.exists() and choice is None:
        print(f"archive retention already {'enabled' if archive.enabled() else 'disabled'}; "
              "leaving that choice unchanged.")
        return
    if choice is None and assume_yes:
        choice = True
    if choice is None:
        print("\nkeep compressed recovery copies of indexed source-store files?")
        print(f"  location: {archive.ARCHIVE_DIR}")
        print("  retention: 3 versions per source path by default; may include deleted chats")
        print("  inspect/disable: agrep archive --status / agrep archive --off")
        if not sys.stdin.isatty():
            print("  (not a terminal - archive left off; use `agrep setup --archive`)")
            choice = False
        else:
            try:
                choice = input("enable archive retention? [y/N] ").strip().lower() in (
                    "y", "yes")
            except EOFError:
                choice = False
    archive.set_enabled(bool(choice))
    print(f"archive retention {'enabled' if choice else 'disabled'}"
          + (f" in {archive.ARCHIVE_DIR}" if choice else ""))


def _plain_regular_leaf(path: Path) -> bool:
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        stat.S_ISREG(info.st_mode)
        and not bool(getattr(info, "st_file_attributes", 0) & reparse)
    )


def _preserve_invalid_corpus_leaf(path: Path) -> bool:
    """Move a non-file derived corpus leaf aside before Rust republishes it."""
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        common.log(f"setup cannot inspect {path.name}: {type(exc).__name__}: {exc}")
        return False
    if _plain_regular_leaf(path):
        return True
    quarantine = path.with_name(
        f".{path.name}.invalid-{os.getpid()}-{time.time_ns()}")
    try:
        if path.lstat() != observed:
            common.log(f"setup cannot repair {path.name}: it changed while being checked")
            return False
        path.rename(quarantine)
    except FileNotFoundError:
        return True
    except OSError as exc:
        common.log(f"setup cannot preserve invalid {path.name}: "
                   f"{type(exc).__name__}: {exc}")
        return False
    print(f"  preserved invalid {path.name} as {quarantine.name}", flush=True)
    return True


def _setup_index_state() -> tuple[dict | None, bool]:
    if not _plain_regular_leaf(common.MESSAGES_PATH):
        return None, False
    import corpusdb
    health = corpusdb.search_generation_health()
    state = str(health.get("state") or "")
    if state == "ready":
        return common.index_summary(), False
    if (not corpusdb.state_self_clears(state)
            or corpusdb._derived_publication_health().get("state") != "ready"):
        return None, False
    summary = common.index_summary()
    daemon = indexd_runtime.indexd_resource_status(
        observe_only=True, include_rss=False)
    if summary is None or not daemon.get("running"):
        return None, False
    return summary, True


def _setup_semantic_deferred(reason: str) -> None:
    print(f"\nmeaning search deferred: {reason}; keyword search is ready.",
          flush=True)


def _setup_start_semantic(
        cli: str, *, index_upgrade_disclosed: bool = False) -> None:
    try:
        import embedder
        import semantic
    except Exception:  # noqa: BLE001 -- meaning search remains optional
        _setup_semantic_deferred("its optional runtime could not load")
        return
    try:
        embedder.ensure_model(download=False)
    except Exception:  # noqa: BLE001 -- setup already attempted the disclosed fetch
        _setup_semantic_deferred(
            "its model is not cached and setup will not retry the network fetch")
        return
    try:
        runtime_available = semantic.runtime_dependencies_available()
    except Exception:  # noqa: BLE001 -- keyword setup is still complete
        _setup_semantic_deferred("its optional runtime could not load")
        return
    if not runtime_available:
        _setup_semantic_deferred("optional runtime dependencies are unavailable")
        return
    try:
        result = semantic.ensure_fresh_async(allow_model_download=True)
    except Exception:  # noqa: BLE001 -- keyword setup is still complete
        _setup_semantic_deferred("the background worker did not start")
        return
    state = str(result.get("state") or "") if isinstance(result, dict) else ""
    if state == "running":
        print("\nbuilding the embeddings index in the background "
              f"- bare `{cli}` shows progress", flush=True)
        return
    if state == "ready":
        return
    if (state == "read-only" and isinstance(result, dict)
            and result.get("owner_build") is not None):
        if index_upgrade_disclosed:
            return
        _setup_semantic_deferred(
            "another agrep build owns its current index")
        return
    reason = {
        "disabled": "background refresh is disabled",
        "failed": "the background build is waiting to retry",
        "legacy-publication": "the existing meaning index needs repair",
        "model-not-cached": "its model is not cached",
        "optional-runtime-unavailable": (
            "optional runtime dependencies are unavailable"),
        "read-only": "this data directory is read-only",
    }.get(state, "the background worker did not start")
    _setup_semantic_deferred(reason)


def _setup_consent_screen(*, no_semantic: bool) -> str:
    """Disclose the core and instruction writes before instruction consent."""
    import platform

    import teach
    lines = ["setup writes these, in this order:", "",
             "core (agrep's own directories; removed by uninstall):"]
    lines.append(f"  search index at {common.DATA_DIR}")
    lines.append("    reason: the searchable copy of your agent history; "
                 "built read-only over the agent stores")
    if not no_semantic:
        lines.append("  semantic model cache (~52 MiB download, one time)")
        lines.append("    reason: meaning search over that index; "
                     "--no-semantic skips it this run")
        if sys.platform == "darwin" and platform.machine() == "arm64":
            lines.append("  Metal GPU weights (~91 MiB, on first GPU embed)")
            lines.append("    reason: GPU embedding, measured ~10.9x the CPU "
                         "lane; AGREP_MLX=off opts out")
    lines += ["", "agent instructions (first consent choice; "
              "undone by `agrep remove`):"]
    found = [(agent, target)
             for agent, proof, target in teach.MD_TARGETS + teach.SKILL_TARGETS
             if proof.is_dir()]
    for agent, target in found:
        lines.append(f"  instruction block in {target}")
    if found:
        lines.append("    reason: an agent only reaches for a tool it knows "
                     "exists; the block says what agrep is and when")
        where = ("a launchd agent (com.agrep.sentinel)"
                 if sys.platform == "darwin" else
                 "a scheduled task (agrep-sentinel)" if os.name == "nt" else
                 "systemd user units (agrep-sentinel)")
        lines.append(f"  cleanup sentinel: {where}")
        lines.append("    reason: takes agrep's instruction blocks and "
                     "owned recovery integrations back out if the binary "
                     "disappears; removes only bytes frozen at install time")
    return "\n".join(lines) + "\n"


def _setup_hook_consent_screen() -> str:
    """Disclose each detected compaction integration as a separate choice."""
    import hookinstall

    lines = ["post-compact recovery integrations "
             "(separate consent choice; undone by `agrep remove`):"]
    if hookinstall.claude_settings_path().parent.is_dir():
        lines.append(f"  claude PreCompact hook in "
                     f"{hookinstall.claude_settings_path()}")
        lines.append("    does: adds recovery requirements to the compaction "
                     "summary; fires only on PreCompact")
    for path in hookinstall.codex_hooks_paths():
        if path.parent.is_dir():
            lines.append(f"  codex SessionStart/compact hook in {path}")
            lines.append("    does: injects recovery context only when Codex "
                         "reports a compacted-session start")
    pi_targets = [
        (agent, path) for agent, path in hookinstall.pi_extension_paths()
        if path.parent.parent.is_dir()
    ]
    for agent, path in pi_targets:
        lines.append(f"  {agent} post-compact extension at {path}")
    if pi_targets:
        lines.append("    does: exports the exact live session ID to agrep; "
                     "OMP also extends its native compaction prompt; both "
                     "queue hidden recovery context only after compaction or "
                     "a compacted resume")
    where = ("launchd com.agrep.sentinel"
             if sys.platform == "darwin" else
             "the agrep-sentinel scheduled task" if os.name == "nt" else
             "the agrep-sentinel systemd user units")
    lines.append(f"  cleanup snapshot in {where}")
    lines.append("    does: removes only these exact agrep-owned files if "
                 "the package disappears; any edited copy survives")
    lines.append("  none of these run on ordinary user messages")
    return "\n".join(lines) + "\n"


def _setup_consentable_writes() -> bool:
    """True when instruction consent would govern at least one real write."""
    import teach
    return any(proof.is_dir()
               for _, proof, _ in teach.MD_TARGETS + teach.SKILL_TARGETS)


def cmd_setup(a) -> int:
    # dependency tier first, then two separately disclosed local-write choices.
    cli = common.cli_name()
    rest = list(getattr(a, "rest", []) or [])
    allowed_legacy = {"--yes", "-y", "--no-teach", "--no-semantic",
                      "--no-hook", "--archive", "--no-archive"}
    unknown = [value for value in rest if value not in allowed_legacy]
    if unknown:
        raw_rest = getattr(a, "raw_rest", None) or rest
        return surface.argument_error(
            "agrep setup", f"unrecognized argument: {unknown[0]}",
            argv=raw_rest, search_word="setup")
    yes = bool(getattr(a, "yes", False) or any(x in rest for x in ("--yes", "-y")))
    no_teach = bool(getattr(a, "no_teach", False) or "--no-teach" in rest)
    no_hook = bool(getattr(a, "no_hook", False) or "--no-hook" in rest)
    no_semantic = bool(
        getattr(a, "no_semantic", False) or "--no-semantic" in rest)
    archive_on = bool(getattr(a, "archive", False) or "--archive" in rest)
    archive_off = bool(getattr(a, "no_archive", False) or "--no-archive" in rest)
    # Instruction consent happens before agent files change. Hook consent is a
    # distinct choice after the instruction phase, at the point of installation.
    import teach
    consented = yes
    hook_consented = yes
    # A valid receipt with a live target already answered the instruction choice.
    enrolled = teach.enrollment_active()
    if (not yes and not enrolled and not no_teach
            and _setup_consentable_writes()):
        if sys.stdin.isatty():
            print(_setup_consent_screen(no_semantic=no_semantic), flush=True)
            try:
                answer = input("write these instructions? [Y/n] ").strip().lower()
            except EOFError:  # windows NUL reports as a tty; EOF decides
                answer = "n"
            if answer in ("n", "no"):
                no_teach = True
                print("core only so far: agent instructions untouched. "
                      f"`{cli} setup` re-offers them any time.", flush=True)
            else:
                consented = True
        else:
            no_teach = True
            print("headless setup without --yes: agent instructions left "
                  "untouched; rerun with --yes to write the files disclosed "
                  "by `setup` interactively.", flush=True)
    print("=== setup 1/5: dependencies (doctor) ===", flush=True)
    import doctor
    doctor_args = ["--setup"] + (["--no-semantic"] if no_semantic else [])
    rc = doctor.main(doctor_args)
    common.lap("dependencies")
    if rc != 0:
        return rc
    if no_teach:
        print("\n=== setup 2/5: agent instructions skipped ===", flush=True)
    else:
        print("\n=== setup 2/5: agent instructions ===", flush=True)
        import teach
        rc = teach.teach(yes=yes or consented)
    if not no_hook:
        import hookinstall
        if not yes and hookinstall.hooks_need_consent():
            if sys.stdin.isatty():
                print("\n" + _setup_hook_consent_screen(), flush=True)
                try:
                    answer = input(
                        "install these recovery integrations? [Y/n] "
                    ).strip().lower()
                except EOFError:
                    answer = "n"
                if answer in ("n", "no"):
                    no_hook = True
                    print("post-compact integrations skipped; "
                          f"`{cli} setup` re-offers them any time.", flush=True)
                else:
                    hook_consented = True
            else:
                no_hook = True
                print("headless setup without --yes: post-compact "
                      "integrations left untouched; rerun with --yes to "
                      "install the files disclosed by `setup` interactively.",
                      flush=True)
    if no_hook:
        print("\n=== setup 3/5: post-compact integrations skipped ===", flush=True)
    else:
        print("\n=== setup 3/5: post-compact recovery integrations ===",
              flush=True)
        import hookinstall
        hook_rc = hookinstall.install(yes=hook_consented)
        import teach
        if not teach.refresh_sentinel():
            print("  ! uninstall cleanup snapshot could not be refreshed; "
                  "`agrep doctor` tracks the sentinel", flush=True)
        if hook_rc != 0:
            print("  ! post-compact hook install failed (setup continues)", flush=True)
    index_upgrade_pending = False
    if rc == 0:
        # setup ends with search working - never "your first search will fetch/build"
        print("\n=== setup 4/5: search index ===", flush=True)
        # A plain leaf is not enough: its committed generation must also prove
        # that the bytes belong to the corpus setup is about to vouch for.
        s, index_upgrade_pending = _setup_index_state()
        if s is not None:
            if index_upgrade_pending:
                print(f"  {s.get('messages', 0):,} messages across "
                      f"{s.get('sessions', 0):,} sessions remain keyword-searchable; "
                      "the background index upgrade is finishing", flush=True)
            else:
                print(f"  already built - {s.get('messages', 0):,} messages across "
                      f"{s.get('sessions', 0):,} sessions (searches keep it fresh)",
                      flush=True)
        elif (not _preserve_invalid_corpus_leaf(common.MESSAGES_PATH)
              or cmd_index(argparse.Namespace(full=True)) != 0):
            # instructions may already be installed - a silent exit here reads
            # as success while agents are enrolled against no index
            print(f"\nsetup incomplete: the index build failed. "
                  f"anything already installed (agent instructions) stays; "
                  f"`{cli} doctor` shows what is missing, and any search "
                  f"retries the build.", flush=True)
            rc = 1
    if rc == 0:
        print("\n=== setup 5/5: optional archive retention ===", flush=True)
        choice = True if archive_on else False if archive_off else None
        _setup_archive(choice, assume_yes=yes)
    if rc == 0 and not no_semantic and common.MESSAGES_PATH.is_file() and \
            common.setting("embeddings") != "off":
        _setup_start_semantic(
            cli, index_upgrade_disclosed=index_upgrade_pending)
    if rc == 0:
        knows = (common.DATA_DIR / "teach.json").exists()
        if knows:
            tail = " - your agents now know agrep exists"
        else:
            # "re-run setup" is only advice when there is something to enroll -
            # on an agentless box that advice can never converge
            import teach
            tail = ((" (agents not enrolled - they cannot use agrep until "
                     f"`{cli} setup` writes their instructions)")
                    if teach.detected_agents() else
                    " (no agents detected on this box - `agrep setup` enrolls "
                    "them once one is installed)")
        print(f"\nsetup complete{tail}.")
        print(f"\n{_core_evidence_path(cli)}")
    return rc


def cmd_remove(a) -> int:
    if a.rest:
        return surface.argument_error(
            "agrep remove", f"unrecognized argument: {a.rest[0]}",
            argv=a.rest, search_word="remove")
    import teach
    return teach.main(["--remove"])


KNOWN_SETTINGS = surface.PUBLIC_SETTING_CHOICES


def _print_set_help() -> None:
    print("usage: agrep set <key> <value>")
    for item in surface.PUBLIC_SETTINGS:
        print(f"  {item.name}: {item.value_help}")
    print("\nexamples:")
    print("  agrep set                  list current settings")
    print("  agrep set embeddings off   disable semantic downloads and builds")
    print("\nexit: 0 listed or changed, 2 invalid arguments.")


def cmd_set(a) -> int:
    import common

    if a.rest in (["-h"], ["--help"]):
        _print_set_help()
        return 0
    if not a.rest:
        for spec in surface.PUBLIC_SETTINGS:
            print(f"  {spec.name} = {common.setting(spec.name)}"
                  f"   ({spec.value_help})")
        managed = common.setting("post_index_hooks")
        if isinstance(managed, dict) and managed:
            print(f"  managed post-index hooks = {', '.join(sorted(managed))}"
                  "   (owned by their apps)")
        return 0
    key, val = a.rest[0] if a.rest else "", " ".join(a.rest[1:])
    spec = surface.SETTINGS.get(key)
    if (spec is None or not spec.public or not val
            or (spec.choices and val not in spec.choices)):
        if spec is None or not spec.public:
            detail = "key must be one of: " + ", ".join(KNOWN_SETTINGS)
        elif not val:
            detail = f"{key} requires a value ({spec.value_help})"
        else:
            detail = f"{key} must be {spec.value_help}"
        return surface.argument_error(
            "agrep set", detail, argv=a.rest, search_word="set")
    settings.set_setting(key, val)
    tail = f"  ({spec.update_note})" if spec.update_note else ""
    print(f"{key} = {val}{tail}")
    return 0


def cmd_archive(a) -> int:
    import archive
    return archive.main(a.rest)


def cmd_restore(a) -> int:
    import archive
    return archive.restore_main(a.rest)


def cmd_tail(a) -> int:
    import tail
    return tail.main(a.rest)


def cmd_board(a) -> int:
    import livetui
    return livetui.main(a.rest)


def _reject_unknown_agent(rest: list[str]) -> int | None:
    # an unknown --agent silently matches nothing; refuse it with the valid list
    err = common.agent_filter_error(rest)
    if err:
        common.log(err)
        return 2
    return None


def cmd_search(a) -> int:
    # in-process (stdlib-only, like resume): spawning a second interpreter doubled
    # the cold-start cost of the single hottest command. --semantic talks to the
    # bounded, short-lived ONNX worker and falls back in-process if it cannot start.
    rc = _reject_unknown_agent(a.rest)
    if rc is not None:
        return rc
    import search
    return search.main(a.rest)


def cmd_chats(a) -> int:
    # identity listing over the small sessions.jsonl aggregate, in-process.
    rc = _reject_unknown_agent(a.rest)
    if rc is not None:
        return rc
    import search
    return search.chats_main(a.rest)


def cmd_around(a) -> int:
    # core-tier like search: stdlib over the materialized index, runs in-process.
    import around
    return around.main(a.rest)


def cmd_postcompact(a) -> int:
    import postcompact
    return postcompact.main(a.rest)


def cmd_recall(a) -> int:
    # core-tier like search/around: stdlib over the materialized index, in-process.
    rc = _reject_unknown_agent(a.rest)
    if rc is not None:
        return rc
    import recall
    return recall.main(a.rest, prog="recall")


def cmd_pack(a) -> int:
    rc = _reject_unknown_agent(a.rest)
    if rc is not None:
        return rc
    import recall
    return recall.main(a.rest, prog="pack")


def cmd_inject(a) -> int:
    import teach
    return teach.main(a.rest)


def cmd_resume(a) -> int:
    # imported and called in-process (not a subprocess) so the resumed agent is a direct
    # child of this process and cleanly inherits the terminal.
    import resume
    return resume.main(a.rest)


def cmd_run(a) -> int:
    if hasattr(a, "agent"):
        agent = a.agent
        extra = a.rest or []
        cwd = getattr(a, "cwd", None)
    else:
        rest = a.rest or []
        # The early hot-command dispatch skips the large top-level parser, but this
        # verb still needs its real grammar: argparse permits --cwd before or after
        # the positional agent and preserves everything after `--` for that agent.
        run_parser = surface.ArgumentParser(
            prog="agrep run",
            usage="agrep run [-h] [--cwd DIR] AGENT [-- AGENT_ARGS...]",
            description="launch an agent, recording child-process liveness from start",
            allow_abbrev=False,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="examples:\n"
                   "  agrep run claude              launch claude, captured\n"
                   "  agrep run codex --cwd . -- --help\n"
                   "                                pass --help to codex here")
        run_parser.add_argument("agent",
                                help="agent to launch "
                                     f"({'/'.join(registry.NATIVE_RESUME_AGENTS)})")
        run_parser.add_argument("--cwd", help="working directory for the agent")
        parsed, extra = run_parser.parse_known_args(rest)
        agent, cwd = parsed.agent, parsed.cwd
        if extra[:1] == ["--"]:
            extra = extra[1:]
    agent = registry.normalize_agent_name(agent)
    unsupported = registry.capability_error("native_resume", agent)
    if unsupported:
        common.log(common.terminal_safe(unsupported))
        return 2
    from hookless import capture
    return capture.run_captured(agent, extra, cwd=cwd)


def cmd_explorer(a, *, open_browser: bool) -> int:
    import server
    return server.main(a.rest, open_browser=open_browser)


def _main() -> int:
    """CLI entry point.

    Dispatch invariant: a recognized command word always reaches that command's
    parser or handler, so malformed commands fail instead of silently becoming
    searches. Bare non-command text is the namesake search; `agrep search <word>`
    is the explicit escape hatch for searching a command word.
    """
    # The hot commands own their argument grammar. Route them before constructing the
    # large top-level parser: a one-shot search/recall otherwise pays to build parsers
    # for every unrelated command before building its real parser in the target module.
    raw = sys.argv[1:]
    if raw and raw[0] not in {
            "status", "doctor", "audit", "tail", "board", "live"}:
        legacy_cleanup.retire_removed_explorer()
    if raw and raw[0] in {"ui", "up", "serve"}:
        return cmd_explorer(
            argparse.Namespace(rest=raw[1:]),
            open_browser=raw[0] != "serve")
    delegated = {
        "doctor": cmd_doctor,
        "reindex": cmd_reindex,
        "resume": cmd_resume,
        "run": cmd_run,
        "search": cmd_search,
        "chats": cmd_chats,
        "tail": cmd_tail,
        "board": cmd_board,
        "live": cmd_board,
        "around": cmd_around,
        "postcompact": cmd_postcompact,
        "recall": cmd_recall,
        "pack": cmd_pack,
        "archive": cmd_archive,
        "restore": cmd_restore,
        "set": cmd_set,
        "inject": cmd_inject,
    }
    if raw and raw[0] in delegated:
        return delegated[raw[0]](argparse.Namespace(rest=raw[1:]))

    parser_commands = {
        "audit", "status", "setup", "remove", "index",
    }
    if (raw and raw[0] not in parser_commands
            and raw[0] not in ("-h", "--help", "-V", "--version")):
        return cmd_search(argparse.Namespace(rest=raw))

    p = surface.ArgumentParser(
        prog="agrep", description="agentic grep: search and explore your "
                                  "cross-agent chat history",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_CORE_EVIDENCE_PATH + "\n\n"
               "find text         search (the default: agrep \"<pattern>\"), "
               "recall, pack\n"
               "resume work       postcompact, around, resume, chats\n"
               "maintain          status, doctor, audit, setup, index, reindex, set, "
               "archive, restore, remove\n"
               "live              tail, board, ui, serve, run\n"
               "\nexamples:\n"
               "  agrep \"race condition\"       grep every agent's history\n"
               "  agrep recall \"index lock\"    hits + the conversation around each\n"
               "  agrep postcompact           recent root context after compaction\n"
               "  agrep around @11111111:144   replay the moment itself\n"
               "  agrep chats webapp           find a chat by name, not content\n"
               "  agrep search index           search a word that is also a command\n"
               "\na bare first argument that isn't a command searches; "
               "`agrep <command> --help`\nshows each command's own examples")
    p.add_argument("-V", "--version", action=_VersionAction, nargs=0)
    # metavar: the public commands only - without it the usage {brace} would leak the
    # hidden compatibility subparsers (live, inject) have no help below.
    sub = p.add_subparsers(
        dest="cmd",
        metavar="{search,chats,postcompact,around,recall,pack,resume,status,setup,index,doctor,"
                "audit,reindex,archive,restore,set,remove,tail,board,ui,serve,run}")

    se = sub.add_parser(
        "search",
        help="search history; compact prose may add meaning")
    se.set_defaults(fn=cmd_search)

    ch = sub.add_parser("chats", help="newest indexed chats; filter by identity")
    ch.set_defaults(fn=cmd_chats)

    ar = sub.add_parser("around", help="show the conversation around one turn of a chat")
    ar.set_defaults(fn=cmd_around)

    pc = sub.add_parser(
        "postcompact", help="supplement a compact summary with recent root context")
    pc.set_defaults(fn=cmd_postcompact)

    rl = sub.add_parser("recall", help="top hits + the chat around each, budget-capped")
    rl.set_defaults(fn=cmd_recall)

    pk = sub.add_parser("pack", help="recall over several queries: merged, deduped, one budget")
    pk.set_defaults(fn=cmd_pack)

    rs = sub.add_parser("resume", help="resume a past session in its own agent, cd'd there")
    rs.set_defaults(fn=cmd_resume)

    st = sub.add_parser(
        "status", help="bounded diagnostic; doctor --deep runs full integrity checks",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep status         bounded diagnostic (same as agrep doctor)\n"
               "  agrep status --json  cheap machine summary, one JSON object\n"
               "  agrep doctor --deep  full integrity, attribution, and archive proofs\n"
               "\nexit: 0 diagnostic complete; 2 invalid arguments.")
    st.add_argument("--json", action="store_true",
                    help="cheap machine-readable index summary instead")
    st.set_defaults(fn=cmd_status)

    su = sub.add_parser("setup", help="build the required search index; offer optional "
                                      "semantics, agent instructions, and recovery archives",
                        allow_abbrev=False,
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        description="build the required search index; optionally prepare "
                                    "meaning search, agent instructions, and recovery archives",
                        epilog="examples:\n"
                               "  agrep setup                     interactive, every choice disclosed\n"
                               "  agrep setup -y                  accept the prompts (for scripts)\n"
                               "  agrep setup --no-teach          skip agent instructions\n"
                               "  agrep setup --no-semantic       skip the ~52 MiB model prefetch\n"
                               "  agrep setup --archive -y        also keep recovery archives\n"
                               "\nexit: 0 setup complete, 1 required setup failed, "
                               "2 invalid arguments.")
    su.add_argument("-y", "--yes", action="store_true",
                    help="accept both instruction and archive prompts (for scripts)")
    su.add_argument("--no-teach", action="store_true",
                    help="do not write recall instructions into agent config files")
    su.add_argument("--no-hook", action="store_true",
                    help="do not install post-compact recovery integrations "
                         "(claude, codex, pi, and OMP)")
    su.add_argument("--no-semantic", action="store_true",
                    help="skip semantic model prefetch for this setup run")
    archive_group = su.add_mutually_exclusive_group()
    archive_group.add_argument("--archive", action="store_true",
                               help="enable compressed source-store recovery retention")
    archive_group.add_argument("--no-archive", action="store_true",
                               help="explicitly keep source-store recovery retention off")
    su.set_defaults(fn=cmd_setup)

    ix = sub.add_parser("index", help="rebuild the base index from your agent stores",
                        allow_abbrev=False,
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog="examples:\n"
                               "  agrep index          ingest new sessions + refresh the search db\n"
                               "  agrep index --full   cold reparse of every store file")
    ix.add_argument("--full", action="store_true",
                    help="ignore the parse cache; re-read every store file")
    ix.set_defaults(fn=cmd_index)

    di = sub.add_parser("doctor", help="bounded diagnostic; --deep runs full integrity "
                                       "checks and --fix repairs safe faults")
    di.set_defaults(fn=cmd_doctor)

    au = sub.add_parser(
        "audit", add_help=False,
        help="prove the ingest misses nothing: per-file intake "
             "accounting vs an independent raw census")
    au.set_defaults(fn=cmd_audit)

    rx = sub.add_parser("reindex", help="refresh ingest, search, and semantic embeddings")
    rx.set_defaults(fn=cmd_reindex)

    av = sub.add_parser("archive", help="snapshot agent store files: files byte-for-byte, "
                                        "sqlite via backup api, deduped, compressed")
    av.set_defaults(fn=cmd_archive)

    ro = sub.add_parser("restore", help="bring an archived store file back, exact bytes, resumable")
    ro.set_defaults(fn=cmd_restore)

    se2 = sub.add_parser("set", help="change a setting (`agrep set` lists them)")
    se2.set_defaults(fn=cmd_set)

    rm = sub.add_parser(
        "remove", help="remove the agent-teaching integration "
                       "(blocks, hooks, sentinel; index/data untouched)",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="remove agrep's agent instructions, post-compact hooks, "
                    "and cleanup scheduler; the search index and source "
                    "histories are left untouched",
        epilog="examples:\n"
               "  agrep remove         remove the enrolled agent integration\n"
               "  agrep setup          enroll it again later\n"
               "\nexit: 0 removed, 1 cleanup incomplete, 2 invalid arguments.")
    rm.set_defaults(fn=cmd_remove)

    ta = sub.add_parser("tail", help="follow live agent events as JSON lines (turn ends by default)")
    ta.set_defaults(fn=cmd_tail)

    lv = sub.add_parser("board", help="bounded live-activity window across agents")
    lv.set_defaults(fn=cmd_board)

    ui = sub.add_parser(
        "ui", help="open the private read-only history explorer with live Board")
    ui.set_defaults(fn=lambda a: cmd_explorer(a, open_browser=True))

    serve = sub.add_parser(
        "serve", help="serve the private history + live Board explorer")
    serve.set_defaults(fn=lambda a: cmd_explorer(a, open_browser=False))

    legacy_live = sub.add_parser("live")
    legacy_live.set_defaults(fn=cmd_board)

    # hidden (no help=): legacy explicit verb; `setup`/`remove` are the public pair
    ij = sub.add_parser("inject")
    ij.set_defaults(fn=cmd_inject)

    rn = sub.add_parser("run", help="launch an agent, recording child-process liveness",
                        usage="agrep run [-h] [--cwd DIR] AGENT [-- AGENT_ARGS...]",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog="examples:\n"
                               "  agrep run claude              launch claude, captured\n"
                               "  agrep run codex --cwd . -- --help\n"
                               "                                pass --help to codex here")
    rn.add_argument(
        "agent",
        help="agent to launch "
             f"({'/'.join(registry.NATIVE_RESUME_AGENTS)})",
    )
    rn.add_argument("--cwd", help="working directory for the agent (default: current dir)")
    rn.set_defaults(fn=cmd_run)

    # parse_known_args, not REMAINDER: REMAINDER errors on leading optionals
    # (`agrep tail --follow`) and scrambles token order; unknowns pass to the subcommand.
    args, unknown = p.parse_known_args()
    args.rest = unknown
    args.raw_rest = raw[1:] if raw else []
    if not getattr(args, "fn", None):
        return cmd_status(args)
    return args.fn(args)


def _crash_line(exc: BaseException) -> str:
    """The reader gets the consequence and a command; the class name and the
    internal message are debugging detail, so they wait behind AGREP_DEBUG."""
    line = surface.crash_advice_line(exc, common.cli_name())
    if common.DEBUG:
        line += f" [{type(exc).__name__}: {common.terminal_safe(exc)}]"
    return line


def main() -> int:
    """CLI boundary: grep-style broken-pipe behavior without Python tracebacks."""
    common.utf8_stdio()
    if sys.platform != "win32":
        try:
            import signal
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        try:
            result = _main()
        except SystemExit as exc:
            result = exc.code if isinstance(exc.code, int) else 1
        slow = common.profile_report()
        if slow:
            common.log(slow)
        # Catch a buffered Windows pipe failure inside this boundary instead of
        # letting interpreter shutdown print "Exception ignored" after main exits.
        sys.stdout.flush()
        return result
    except KeyboardInterrupt:
        return 130
    except (BrokenPipeError, OSError) as exc:
        # Windows reports a vanished pipe reader (ERROR_NO_DATA) as plain
        # OSError(EINVAL), never BrokenPipeError; other OSErrors are real failures.
        if not isinstance(exc, BrokenPipeError) and not (
                WIN and exc.errno in (errno.EPIPE, errno.EINVAL)):
            common.log(_crash_line(exc))
            return 2
        # Windows has no SIGPIPE. Point later interpreter flushes at the null
        # device and return the conventional 128+SIGPIPE status.
        try:
            import os
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, sys.stdout.fileno())
            os.close(fd)
        except OSError:
            pass
        return 141
    except Exception as exc:  # noqa: BLE001 -- rc=1 is reserved for a clean miss
        common.log(_crash_line(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
