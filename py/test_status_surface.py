"""Bare `agrep` renders the reader's situation, never agrep's own plumbing.

Six laws hold this surface. A healthy box is quiet (1); the render never
reports agrep's tiers, budgets or scheduling (2); every label and value form
one sentence (3); no internal noun escapes (4); anything wrong gets exactly one
line ending in the command that ends it (5); nothing that was not observed is
narrated (6).

DENY is a structural pin on our own output vocabulary - the emitter's words,
not a user's prose - so it is legitimate to match exactly.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cli
import dist
import surface_policy as surface


class IndexOutputTests(unittest.TestCase):
    def test_index_has_one_owned_slow_timing_line(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(
                cli.indexd_runtime, "build_index", return_value=True), \
                mock.patch.object(cli.common, "lap") as lap, \
                contextlib.redirect_stdout(stdout):
            self.assertTrue(cli._index())
        self.assertIn("=== indexing transcripts ===", stdout.getvalue())
        self.assertNotRegex(stdout.getvalue(), r"(?m)^\s*\([0-9.]+s\)$")
        lap.assert_called_once_with("index")


class BuildIdentityTests(unittest.TestCase):
    def test_native_identity_depends_only_on_exact_binary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"same native bytes")
            second.write_bytes(first.read_bytes())
            expected = hashlib.sha256(first.read_bytes()).hexdigest()[:20]
            self.assertEqual(dist.native_binary_build_id(first), expected)
            self.assertEqual(
                dist.native_binary_build_id(first),
                dist.native_binary_build_id(second))
            with mock.patch.object(
                    cli.indexd_runtime, "INDEXD_BUILD_ID", "f" * 20):
                self.assertEqual(dist.native_binary_build_id(first), expected)
            second.write_bytes(b"different native bytes")
            self.assertNotEqual(
                dist.native_binary_build_id(first),
                dist.native_binary_build_id(second))

    def test_native_and_writer_identity_states_are_independent(self) -> None:
        with (mock.patch.object(
                  cli.common, "distribution_build_id", return_value="d" * 20),
              mock.patch.object(
                  cli, "_bounded_binary_identity", return_value={
                      "native_binary_build_id": None,
                      "native_binary_build_state": "unavailable",
                      "native_binary_build_detail": "native missing",
                      "writer_build_id": "c" * 20,
                      "writer_build_state": "verified",
                  })):
            native_missing = cli._build_identity()
        self.assertIsNone(native_missing["native_binary_build_id"])
        self.assertEqual(
            native_missing["native_binary_build_state"], "unavailable")
        self.assertEqual(native_missing["writer_build_id"], "c" * 20)
        self.assertEqual(native_missing["writer_build_state"], "verified")

        with (mock.patch.object(
                  cli.common, "distribution_build_id", return_value="d" * 20),
              mock.patch.object(
                  cli, "_bounded_binary_identity", return_value={
                      "native_binary_build_id": "b" * 20,
                      "native_binary_build_state": "verified",
                      "writer_build_id": None,
                      "writer_build_state": "unavailable",
                      "writer_build_detail": "writer deadline",
                  })):
            writer_missing = cli._build_identity()
        self.assertEqual(writer_missing["native_binary_build_id"], "b" * 20)
        self.assertEqual(
            writer_missing["native_binary_build_state"], "verified")
        self.assertIsNone(writer_missing["writer_build_id"])
        self.assertEqual(writer_missing["writer_build_state"], "unavailable")

    def test_cli_version_uses_distribution_identity(self) -> None:
        with mock.patch.object(
                cli.dist, "package_version", return_value="1.2.3-rc.4"):
            self.assertEqual(cli._version(), "1.2.3-rc.4")

    def test_version_reports_portable_native_identity(self) -> None:
        identity = {
            "distribution_build_id": "d" * 20,
            "runtime_build_id": "a" * 20,
            "native_binary_build_id": "b" * 20,
            "writer_build_id": "c" * 20,
        }
        with mock.patch.object(cli, "_build_identity", return_value=identity):
            rendered = cli._version_text()
        self.assertEqual(
            rendered,
            f"agrep {cli._version()} distribution " + "d" * 20
            + " runtime " + "a" * 20 + " native " + "b" * 20
            + " writer " + "c" * 20)

    def test_help_does_not_hash_native_identity(self) -> None:
        with mock.patch.object(
                cli.dist, "native_binary_build_id",
                side_effect=AssertionError("help hashed native")) as native, \
                mock.patch.object(sys, "argv", ["agrep", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                cli._main()
        self.assertEqual(stopped.exception.code, 0)
        native.assert_not_called()

    def test_status_json_carries_portable_native_identity(self) -> None:
        core = {"diagnostics": {
            "tier": "routine", "state": "complete",
            "budget_s": 0.8, "deferred": []}}
        semantic = {
            "semantic_status": "not-inspected", "semantic_verified": False}
        identity = {
            "runtime_build_id": "a" * 20,
            "native_binary_build_id": "b" * 20,
            "native_binary_build_state": "verified",
        }
        with (mock.patch.object(cli, "_status_core", return_value=core),
              mock.patch.object(cli, "_status_semantic", return_value=semantic),
              mock.patch.object(cli, "_build_identity", return_value=identity),
              mock.patch.object(cli, "_kick_repair_if_damaged")):
            payload = cli._status_data()
        self.assertEqual(payload["native_binary_build_id"], "b" * 20)
        self.assertEqual(payload["native_binary_build_state"], "verified")

    def test_distribution_identity_matches_source_and_installed_layouts(self) -> None:
        manifest = {"version": 1, "files": [
            {"source": "cli.py", "member": "agrep/cli.py"},
            {"source": "py/runtime_manifest.json",
             "member": "agrep/py/runtime_manifest.json"},
        ]}
        raw = json.dumps(manifest, sort_keys=True).encode()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            package = root / "installed" / "agrep"
            (source / "py").mkdir(parents=True)
            (source / "cli.py").write_bytes(b"same launcher\n")
            (source / "py" / "runtime_manifest.json").write_bytes(raw)
            (package / "py").mkdir(parents=True)
            (package / "cli.py").write_bytes(b"same launcher\n")
            (package / "py" / "runtime_manifest.json").write_bytes(raw)
            with mock.patch.object(dist, "REPO_ROOT", source), \
                    mock.patch.object(dist, "PY_DIR", source / "py"), \
                    mock.patch.object(dist, "_is_dev_checkout", return_value=True):
                source_id = dist.distribution_build_id()
            with mock.patch.object(dist, "REPO_ROOT", package), \
                    mock.patch.object(dist, "PY_DIR", package / "py"), \
                    mock.patch.object(dist, "_is_dev_checkout", return_value=False):
                installed_id = dist.distribution_build_id()
            self.assertEqual(source_id, installed_id)
            (package / "cli.py").write_bytes(b"different launcher\n")
            with mock.patch.object(dist, "REPO_ROOT", package), \
                    mock.patch.object(dist, "PY_DIR", package / "py"), \
                    mock.patch.object(dist, "_is_dev_checkout", return_value=False):
                self.assertNotEqual(source_id, dist.distribution_build_id())

    def test_source_and_installed_layouts_keep_content_identities_separate(self) -> None:
        manifest = {"version": 1, "files": [
            {"source": "cli.py", "member": "agrep/cli.py"},
            {"source": "py/runtime_manifest.json",
             "member": "agrep/py/runtime_manifest.json"},
        ]}
        raw = json.dumps(manifest, sort_keys=True).encode()
        binary = b"same release binary"
        executable = "agrep-rs.exe" if dist.WIN else "agrep-rs"
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"AGREP_RS_BIN": ""}):
            root = Path(td)
            source = root / "source"
            package = root / "installed" / "agrep"
            source_binary = source / "target" / "release" / executable
            installed_binary = package / "_bin" / executable
            for path in (
                    source / "py" / "runtime_manifest.json",
                    package / "py" / "runtime_manifest.json"):
                path.parent.mkdir(parents=True)
                path.write_bytes(raw)
            (source / "cli.py").write_bytes(b"same launcher\n")
            (package / "cli.py").write_bytes(b"same launcher\n")
            source_binary.parent.mkdir(parents=True)
            installed_binary.parent.mkdir(parents=True)
            source_binary.write_bytes(binary)
            installed_binary.write_bytes(binary)

            with mock.patch.object(dist, "REPO_ROOT", source), \
                    mock.patch.object(dist, "PY_DIR", source / "py"), \
                    mock.patch.object(dist, "_is_dev_checkout", return_value=True):
                source_distribution = dist.distribution_build_id()
                self.assertEqual(dist.ingest_bin(), source_binary)
                source_native = dist.native_binary_build_id(dist.ingest_bin())
                (source / "cli.py").write_bytes(b"changed launcher\n")
                self.assertNotEqual(
                    source_distribution, dist.distribution_build_id())
                self.assertEqual(
                    source_native,
                    dist.native_binary_build_id(dist.ingest_bin()))

            with mock.patch.object(dist, "REPO_ROOT", package), \
                    mock.patch.object(dist, "PY_DIR", package / "py"), \
                    mock.patch.object(dist, "_is_dev_checkout", return_value=False):
                installed_distribution = dist.distribution_build_id()
                self.assertEqual(dist.ingest_bin(), installed_binary)
                installed_native = dist.native_binary_build_id(dist.ingest_bin())
                self.assertEqual(source_native, installed_native)
                installed_binary.write_bytes(b"changed release binary")
                self.assertEqual(
                    installed_distribution, dist.distribution_build_id())
                self.assertNotEqual(
                    installed_native,
                    dist.native_binary_build_id(dist.ingest_bin()))

    def test_identity_uses_content_derived_runtime_and_writer_ids(self) -> None:
        with (mock.patch.object(
                  cli.common, "distribution_build_id", return_value="d" * 20),
              mock.patch.object(
                  cli.indexd_runtime, "INDEXD_BUILD_ID", "a" * 20),
              mock.patch.object(
                  cli, "_bounded_binary_identity", return_value={
                      "native_binary_build_id": "c" * 20,
                      "native_binary_build_state": "verified",
                      "writer_build_id": "b" * 20,
                      "writer_build_state": "verified",
                  }) as binary):
            identity = cli._build_identity()
        self.assertEqual(identity["distribution_build_id"], "d" * 20)
        self.assertEqual(identity["runtime_build_id"], "a" * 20)
        self.assertEqual(identity["writer_build_id"], "b" * 20)
        self.assertEqual(identity["writer_build_state"], "verified")
        binary.assert_called_once_with(
            cli.common.ingest_bin(), timeout_s=cli._BINARY_IDENTITY_TIMEOUT_S)

    def test_missing_binary_is_an_honest_identity_state(self) -> None:
        with mock.patch.object(
                cli, "_bounded_binary_identity",
                side_effect=FileNotFoundError("binary missing")):
            identity = cli._build_identity()
        self.assertIsNone(identity["writer_build_id"])
        self.assertEqual(identity["writer_build_state"], "unavailable")
        self.assertIn("FileNotFoundError", identity["writer_build_detail"])

    def test_slow_or_malformed_binary_is_an_honest_identity_state(self) -> None:
        for failure in (
                TimeoutError("writer identity deadline expired"),
                ValueError("embedded null byte")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(
                        cli, "_bounded_binary_identity", side_effect=failure):
                identity = cli._build_identity(timeout_s=0.01)
            self.assertIsNone(identity["writer_build_id"])
            self.assertEqual(identity["writer_build_state"], "unavailable")
            self.assertIn(type(failure).__name__,
                          identity["writer_build_detail"])

    @unittest.skipIf(cli.WIN, "fork-only worker timing fixture")
    def test_writer_identity_worker_enforces_its_deadline(self) -> None:
        def slow_writer(*_args, **_kwargs):
            time.sleep(2.0)
            return "a" * 20

        started = time.monotonic()
        with mock.patch.object(
                cli.indexd_runtime, "derived_writer_build_id",
                side_effect=slow_writer), self.assertRaises(TimeoutError):
            cli._bounded_binary_identity(
                cli.common.ingest_bin(), timeout_s=0.01)
        self.assertLess(time.monotonic() - started, 0.75)

    @unittest.skipIf(cli.WIN, "fork-only worker timing fixture")
    def test_native_timeout_preserves_a_verified_writer_identity(self) -> None:
        def slow_native(*_args, **_kwargs):
            time.sleep(2.0)
            return "b" * 20

        started = time.monotonic()
        with mock.patch.object(
                cli.indexd_runtime, "derived_writer_build_id",
                return_value="a" * 20), mock.patch.object(
                    cli.dist, "native_binary_build_id",
                    side_effect=slow_native):
            identity = cli._bounded_binary_identity(
                cli.common.ingest_bin(), timeout_s=0.01)
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(identity["writer_build_id"], "a" * 20)
        self.assertEqual(identity["writer_build_state"], "verified")
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertEqual(
            identity["native_binary_build_state"], "unavailable")

    @unittest.skipIf(cli.WIN, "fork-only worker patch fixture")
    def test_malformed_native_identity_does_not_erase_writer_identity(self) -> None:
        with mock.patch.object(
                cli.indexd_runtime, "derived_writer_build_id",
                return_value="a" * 20), mock.patch.object(
                    cli.dist, "native_binary_build_id", return_value="bad"):
            identity = cli._bounded_binary_identity(
                cli.common.ingest_bin(), timeout_s=0.1)
        self.assertEqual(identity["writer_build_id"], "a" * 20)
        self.assertIsNone(identity["native_binary_build_id"])
        self.assertIn(
            "invalid build id", identity["native_binary_build_detail"])

    def test_version_hashes_the_writer_only_when_requested(self) -> None:
        with mock.patch.object(
                cli, "_bounded_binary_identity",
                side_effect=AssertionError("help hashed the binary")) as binary, \
                mock.patch.object(sys, "argv", ["agrep", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                cli._main()
        self.assertEqual(stopped.exception.code, 0)
        binary.assert_not_called()

        output = io.StringIO()
        with (mock.patch.object(
                  cli.common, "distribution_build_id", return_value="e" * 20),
              mock.patch.object(
                  cli.indexd_runtime, "INDEXD_BUILD_ID", "c" * 20),
              mock.patch.object(
                  cli, "_bounded_binary_identity", return_value={
                      "native_binary_build_id": "b" * 20,
                      "native_binary_build_state": "verified",
                      "writer_build_id": "d" * 20,
                      "writer_build_state": "verified",
                  }),
              mock.patch.object(sys, "argv", ["agrep", "--version"]),
              contextlib.redirect_stdout(output)):
            with self.assertRaises(SystemExit) as stopped:
                cli._main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("distribution " + "e" * 20, output.getvalue())
        self.assertIn("runtime " + "c" * 20, output.getvalue())
        self.assertIn("writer " + "d" * 20, output.getvalue())

    def test_status_json_payload_carries_the_same_identity_fields(self) -> None:
        core = {
            "diagnostics": {
                "tier": "routine", "state": "complete",
                "budget_s": 0.8, "deferred": []},
        }
        semantic = {
            "semantic_status": "not-inspected", "semantic_verified": False}
        identity = {
            "runtime_build_id": "e" * 20,
            "writer_build_id": "f" * 20,
            "writer_build_state": "verified",
        }
        with (mock.patch.object(cli, "_status_core", return_value=core),
              mock.patch.object(cli, "_status_semantic", return_value=semantic),
              mock.patch.object(cli, "_build_identity", return_value=identity),
              mock.patch.object(cli, "_kick_repair_if_damaged")):
            payload = cli._status_data()
        for key, value in identity.items():
            self.assertEqual(payload[key], value)

    def test_status_json_never_rehashes_an_unavailable_writer(self) -> None:
        identity = {
            "runtime_build_id": "e" * 20,
            "writer_build_id": None,
            "writer_build_state": "unavailable",
            "writer_build_detail": "TimeoutError: fixture deadline",
        }

        def core(**_kwargs):
            with self.assertRaises(OSError):
                cli.indexd_runtime.derived_writer_build_id(
                    cli.common.ingest_bin(), require_binary=True)
            return {"diagnostics": {
                "tier": "routine", "state": "complete",
                "budget_s": 0.8, "deferred": []}}

        semantic = {
            "semantic_status": "not-inspected", "semantic_verified": False}
        with (mock.patch.object(cli, "_status_core", side_effect=core),
              mock.patch.object(cli, "_status_semantic", return_value=semantic),
              mock.patch.object(cli, "_build_identity", return_value=identity),
              mock.patch.object(cli.indexd_runtime, "_ingest_binary_digest",
                                side_effect=AssertionError("writer rehashed")),
              mock.patch.object(cli, "_kick_repair_if_damaged")):
            payload = cli._status_data()
        self.assertEqual(payload["writer_build_state"], "unavailable")


# every noun the audits found leaking: ownership model, scheduler tiers, store
# internals, and the raw daemon-state enum
DENY = (
    "freshness owner", "blocked-owner", "incompatible", "orphaned-group",
    "malformed-fresh", "spawn-guard", "legacy-owner", "hostile",
    "unverifiable", "derived-store-owner", "dead-owner", "fenced",
    "daemon", "generation", "publication", "publish", "derived stores",
    "census", "enrollment", "routine tier", "routine status", "routine budget",
    "status-deferred", "deferred", "not verified", "not inspected",
    "budget", "tier", "sentinel", "anchor", "build id", "corpus.db",
    "sessions.jsonl", "messages.jsonl", ".ingest.sig", "replies.jsonl",
    "keyword search works", "last good index", "read-only",
)

_HEALTHY = {
    "data_dir": "/fixture", "data_dir_source": "default", "warnings": [],
    "index_built": True, "index_state": "ready",
    "sessions": 5_736, "messages": 18_194, "agents": ["codex", "claude"],
    "per_agent": [
        {"agent": "codex", "messages": 9_277, "sessions": 1_692},
        {"agent": "claude", "messages": 8_881, "sessions": 4_032},
    ],
    "last_indexed_age_s": 30,
    "search_index_ready": None, "search_index_state": "not-verified",
    "agents_taught": True, "detected_not_indexed": [],
    "freshness": {"state": "no-known-failure"},
    "daemon": {"running": True},
    "embeddings_setting": {
        "state": "verified", "value": "auto", "source": "default"},
    "diagnostics": {
        "tier": "routine", "state": "partial", "budget_s": 0.8,
        # a real box always defers something; none of it may reach the render
        "deferred": ["search database readiness", "semantic runtime and index"],
        "details": {"search database readiness": "fixture deferral"},
    },
}

_UNOBSERVED_SEMANTIC = {
    "semantic_deps": None, "semantic_verified": False,
    "semantic_status": "not-inspected", "semantic_state": "not-verified",
    "semantic_ready": None, "semantic_embedding_now": None,
}


def render(**overrides) -> str:
    core = {**_HEALTHY, **overrides}
    semantic = core.pop("_semantic", _UNOBSERVED_SEMANTIC)
    repair = core.pop("_repair", None)
    with (mock.patch.object(cli, "_status_core", return_value=core),
          mock.patch.object(
              cli, "_kick_repair_if_damaged", return_value=repair),
          mock.patch.object(cli, "_status_semantic", return_value=semantic)):
        return "\n".join(cli._status_lines("agrep"))


class StatusVocabularyTests(unittest.TestCase):
    def assertClean(self, rendered: str) -> None:
        low = rendered.lower()
        for term in DENY:
            self.assertNotIn(
                term, low, f"internal vocabulary reached the render: {term!r}")

    def test_healthy_box_renders_only_the_corpus(self) -> None:
        rendered = render()
        self.assertEqual(rendered.splitlines(), [
            "  18,194 messages · 5,736 sessions · last indexed 30s ago",
            "    codex   9,277 messages · 1,692 sessions",
            "    claude  8,881 messages · 4,032 sessions",
        ])
        self.assertClean(rendered)

    def test_the_default_data_dir_earns_no_line(self) -> None:
        self.assertNotIn("data dir", render())
        self.assertIn("data dir", render(data_dir_source="AGREP_DATA_DIR"))

    def test_no_deferral_footer_survives_anywhere(self) -> None:
        for extra in ({}, {"index_built": False},
                      {"index_built": None, "index_state": "status-deferred"},
                      {"search_index_ready": False}):
            self.assertNotIn("doctor --deep", render(**extra))

    def test_every_deny_term_would_be_caught(self) -> None:
        # the pin is only worth its line if it fails on the old render
        with self.assertRaises(AssertionError):
            self.assertClean("  embeddings: keyword search works")


class StatusDegradedStateTests(unittest.TestCase):
    """One problem, one line, ending in a command the reader can paste."""

    def problem_lines(self, rendered: str) -> list[str]:
        corpus = ("messages ·", "sessions", "data dir")
        return [line for line in rendered.splitlines()
                if line.strip() and not any(k in line for k in corpus)]

    def assertOneCommandLine(self, rendered: str, command: str) -> None:
        lines = self.problem_lines(rendered)
        self.assertEqual(len(lines), 1, rendered)
        self.assertIn(f"`{command}`", lines[0])
        for term in DENY:
            self.assertNotIn(term, lines[0].lower())

    def test_first_run_offers_one_command(self) -> None:
        rendered = render(
            index_built=False, index_state="never-built",
            search_index_ready=False, search_index_state="missing",
            search_index_defect=None,
            _repair=cli.indexd_runtime.RepairKick(False, "no-binary"))
        self.assertOneCommandLine(rendered, "agrep setup")

    def test_specific_search_damage_owns_the_one_remedy_line(self) -> None:
        rendered = render(
            index_built=None, index_state="not-verified",
            search_index_ready=False, search_index_state="stale",
            daemon={"running": False})
        self.assertOneCommandLine(rendered, "agrep index")

    def test_budget_expiry_renders_nothing_at_all(self) -> None:
        # law 6: a check that did not run has no result to report
        rendered = render(index_built=None, index_state="status-deferred")
        self.assertEqual(self.problem_lines(rendered), [])

    def test_missing_search_index(self) -> None:
        self.assertOneCommandLine(
            render(search_index_ready=False,
                   search_index_state="missing"), "agrep index")

    def test_stale_search_index_is_never_called_missing(self) -> None:
        rendered = render(
            search_index_ready=False, search_index_state="stale")
        self.assertNotIn("missing", rendered)
        self.assertOneCommandLine(rendered, "agrep index")

    def test_active_repair_keeps_search_index_damage_quiet(self) -> None:
        rendered = render(
            search_index_ready=False, search_index_state="stale",
            _repair=cli.indexd_runtime.RepairKick(True, ""))
        self.assertEqual(self.problem_lines(rendered), [])

    def test_another_running_version_is_not_silently_rendered_as_healthy(self) -> None:
        rendered = render(
            index_state="owned-elsewhere", search_index_ready=False,
            search_index_state="owned-elsewhere",
            daemon={"running": False, "blocked": True, "state": "incompatible"})
        self.assertOneCommandLine(rendered, "agrep doctor")
        self.assertIn("another agrep version", rendered)

    def test_active_repair_keeps_another_running_version_quiet(self) -> None:
        rendered = render(
            index_state="owned-elsewhere", search_index_ready=False,
            search_index_state="owned-elsewhere",
            daemon={"running": False, "blocked": True, "state": "incompatible"},
            _repair=cli.indexd_runtime.RepairKick(True, ""))
        self.assertEqual(self.problem_lines(rendered), [])

    def test_summary_movement_does_not_hide_concluded_search_damage(self) -> None:
        cases = (
            ("empty", "agrep index"),
            ("not-a-file", "agrep index --full"),
            ("unreadable", "agrep doctor"),
        )
        for defect, command in cases:
            with self.subTest(defect=defect):
                rendered = render(
                    index_built=None, index_state="status-deferred",
                    search_index_ready=None, search_index_state="unavailable",
                    search_index_defect=defect,
                    daemon={"running": True},
                    _repair=cli.indexd_runtime.RepairKick(False, "spawn-failed"))
                self.assertOneCommandLine(rendered, command)

    def test_concluded_search_index_damage_is_actionable(self) -> None:
        self.assertOneCommandLine(
            render(search_index_state="corrupt"), "agrep index --full")
        self.assertOneCommandLine(
            render(search_index_state="unavailable"), "agrep doctor")

    def test_empty_search_index_is_not_reported_as_present(self) -> None:
        self.assertOneCommandLine(
            render(search_index_state="unavailable",
                   search_index_defect="empty"), "agrep index")

    def test_search_index_that_is_not_a_file(self) -> None:
        self.assertOneCommandLine(
            render(search_index_state="unavailable",
                   search_index_defect="not-a-file"), "agrep index --full")

    def test_unreadable_search_index(self) -> None:
        self.assertOneCommandLine(
            render(search_index_state="unavailable",
                   search_index_defect="unreadable"), "agrep doctor")

    def test_drift_a_writer_is_converging_earns_no_line(self) -> None:
        rendered = render(
            freshness={"state": "index-behind", "behind_s": 240,
                       "changed_stores": 2},
            daemon={"running": True})
        self.assertEqual(self.problem_lines(rendered), [])

    def test_drift_nothing_will_absorb_gets_the_command(self) -> None:
        self.assertOneCommandLine(
            render(freshness={"state": "index-behind", "behind_s": 240,
                              "changed_stores": 2},
                   daemon={"running": False}), "agrep index")

    def test_untaught_agents_are_the_one_warning_that_earns_its_line(self) -> None:
        self.assertOneCommandLine(render(agents_taught=False), "agrep setup")

    def test_inconclusive_teach_check_says_nothing(self) -> None:
        self.assertEqual(self.problem_lines(render(agents_taught=None)), [])

    def test_empty_corpus_points_at_what_was_scanned(self) -> None:
        rendered = render(messages=0, sessions=0, per_agent=[])
        self.assertIn("`agrep doctor`", rendered)


class IndexingAdviceTests(unittest.TestCase):
    """The shared table cli, doctor and search all render from."""

    def test_every_failure_code_is_a_sentence_plus_a_command(self) -> None:
        for code, advice in surface.INDEXING_ADVICE.items():
            with self.subTest(code=code):
                if advice is None:
                    self.assertEqual(
                        surface.indexing_advice_line(
                            surface.FreshnessFailure(code, "raw"), "agrep"),
                        "")
                    continue
                line = surface.indexing_advice_line(
                    surface.FreshnessFailure(code, "raw internal reason"),
                    "agrep")
                self.assertIn("`agrep ", line)
                self.assertNotIn("raw internal reason", line)
                for term in DENY:
                    self.assertNotIn(term, line.lower())

    def test_an_unlisted_code_still_gets_a_command(self) -> None:
        line = surface.indexing_advice_line(
            surface.FreshnessFailure("brand-new-code", "internals"), "agrep")
        self.assertIn("`agrep index`", line)
        self.assertNotIn("brand-new-code", line)

    def test_a_persistent_streak_is_counted_in_the_readers_terms(self) -> None:
        line = surface.indexing_advice_line(
            surface.FreshnessFailure("consecutive-failures", "x", 42), "agrep")
        self.assertIn("42 attempts in a row", line)
        self.assertNotIn("streak", line)


class CrashBoundaryTests(unittest.TestCase):
    """An unhandled exception is a user surface too: no class names, no
    internal message, always a command."""

    def line(self, exc: BaseException) -> str:
        return surface.crash_advice_line(exc, "agrep")

    def test_known_failures_map_to_the_readers_loss(self) -> None:
        cases = (
            (PermissionError("denied"), "data directory", "agrep doctor"),
            (RuntimeError("event generation changed during bulk read"),
             "live-session data", "agrep doctor"),
            (RuntimeError("event store cannot be opened"),
             "live-session data", "agrep doctor"),
        )
        for exc, expected, command in cases:
            with self.subTest(exc=type(exc).__name__):
                line = self.line(exc)
                self.assertIn(expected, line)
                self.assertIn(f"`{command}`", line)

    def test_a_damaged_search_index_names_the_rebuild(self) -> None:
        import sqlite3
        self.assertIn(
            "`agrep index --full`", self.line(sqlite3.DatabaseError("x")))

    def test_no_exception_class_or_internal_message_reaches_the_line(self) -> None:
        for exc in (RuntimeError("event generation changed during bulk read"),
                    ValueError("__internal_marker__"),
                    OSError("__internal_marker__")):
            line = self.line(exc)
            self.assertNotIn("__internal_marker__", line)
            self.assertNotIn(type(exc).__name__, line)
            self.assertFalse(re.search(r"\b[A-Za-z]+Error\b", line), line)

    def test_an_unknown_failure_still_routes_somewhere(self) -> None:
        self.assertIn("`agrep doctor`", self.line(ValueError("surprise")))


if __name__ == "__main__":
    unittest.main()
