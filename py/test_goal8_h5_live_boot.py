"""Oracle checks for the Goal 8 H5 cold-tree acceptance harness."""

from __future__ import annotations

import copy
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "bench" / "adversarial" / "goal8_h5_live_boot.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("agrep_goal8_h5_live_boot", HARNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "darwin", "H5 acceptance oracle is macOS-only")
class Goal8H5HarnessOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HARNESS.exists():
            raise unittest.SkipTest(
                "the H5 harness is absent from the public export")
        cls.harness = _load_harness()

    def _evidence(self) -> dict:
        now = datetime.now(timezone.utc)
        body = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in self.harness._session_rows(now, Path("/fixture/work/goal8-h5"))
        )
        relative = now.strftime("%Y/%m/%d/") + (
            f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{self.harness.SESSION}.jsonl")
        row = {
            "agent": "codex", "session": self.harness.SESSION, "working": True,
            "recent": [
                {"type": "user", "text": self.harness.MARKER},
                {"type": "tool", "name": "shell", "input": "sleep 600",
                 "call_id": "goal8-h5-running-call"},
            ],
        }
        legacy_payload = {
            "fixed_wait_s": 1.4, "pre_ready_elapsed_s": 1.41,
            "pre_ready": {"booting": True, "n_loops": 0, "match": None},
            "wait_boot": True, "boot_elapsed_s": 3.0,
            "post_ready": {"booting": False, "n_loops": 1, "match": row},
        }
        tail_payload = {"booting": False, "n_loops": 1, "sessions": [row]}
        board = (
            "agrep board\n"
            f"1 ● codex goal8-h5 ⚙ shell 1s {self.harness.MARKER}\n"
            "  ⚙ shell: sleep 600\n"
        )
        repo = self.harness._repo_identity()
        repo.update({
            "dirty": False,
            "status": self.harness._blob_record(b""),
            "diff": self.harness._blob_record(b""),
            "untracked": {},
        })
        repo["identity_sha256"] = self.harness._digest({
            key: value for key, value in repo.items()
            if key != "identity_sha256"
        })
        absent = Path(tempfile.gettempdir()).resolve() / (
            f"agrep-goal8-h5-unit-absent-{os.getpid()}")
        topology = self.harness._expected_topology(
            336_000, relative, len(body.encode("utf-8")))
        evidence = {
            "schema": self.harness.SCHEMA,
            "parameters": {
                "entries": 336_000, "legacy_wait_s": 1.4, "timeout_s": 180.0,
            },
            "repo": {"root": str(ROOT), "before": repo, "after": repo},
            "platform": {
                "sys_platform": sys.platform, "os_name": os.name,
                "machine": self.harness.platform.machine(),
                "python": self.harness.platform.python_version(),
                "python_executable": str(self.harness.PYTHON),
            },
            "scratch": {
                "requested_files": 336_000, "observed_files": 336_000,
                "created_old_files": 335_999,
                "observed_old_files": 335_999, "observed_current_files": 1,
                "observed_directories": topology["directories"],
                "observed_tree_entries": topology["tree_entries"],
                "unexpected_nonempty_old_files": 0,
                "unexpected_nonregular_files": 0,
                "observed_source_sha256": self.harness.hashlib.sha256(
                    body.encode("utf-8")).hexdigest(),
                "file_manifest_sha256": topology["file_manifest_sha256"],
                "path": str(absent),
                "parent": str(Path(tempfile.gettempdir()).resolve()),
                "free_before": self.harness.MIN_FREE_BYTES,
                "owner_token_sha256": "a" * 64,
                "cleanup": {
                    "removed": True, "error": "", "elapsed_s": 1.0,
                    "free_after": self.harness.MIN_FREE_BYTES,
                },
            },
            "identity": {
                "agent": "codex", "session": self.harness.SESSION,
                "marker": self.harness.MARKER, "source_body": body,
                "source_path": f"home/.codex/sessions/{relative}",
                "source_sha256": self.harness.hashlib.sha256(
                    body.encode("utf-8")).hexdigest(),
                "store_relative_path": relative,
            },
            "legacy_ab": {
                "command": [str(self.harness.PYTHON), "-c",
                            self.harness.LEGACY_PROBE_CODE],
                "rc": 0, "wall_s": 3.1, "timed_out": False, "stderr": "",
                "stdout": json.dumps(legacy_payload, sort_keys=True) + "\n",
                "payload": legacy_payload,
            },
            "surfaces": {
                "tail_snapshot": {
                    "command": [str(self.harness.PYTHON), str(ROOT / "cli.py"),
                                "tail", "--snapshot"],
                    "rc": 0, "wall_s": 2.1, "timed_out": False, "stderr": "",
                    "stdout": json.dumps(tail_payload) + "\n",
                    "payload": tail_payload, "match": row,
                },
                "board_once": {
                    "command": [str(self.harness.PYTHON), str(ROOT / "cli.py"),
                                "board", "--once"],
                    "rc": 0, "wall_s": 2.1, "timed_out": False, "stderr": "",
                    "stdout": board, "found": True,
                },
                "board_piped": {
                    "command": [str(self.harness.PYTHON), str(ROOT / "cli.py"),
                                "board"],
                    "rc": 0, "wall_s": 2.1, "timed_out": False, "stderr": "",
                    "stdout": board, "found": True,
                },
                "tail_ready": {
                    "command": [str(self.harness.PYTHON),
                                str(ROOT / "py" / "tail.py"),
                                "--events", "done"],
                    "rc": -signal.SIGTERM, "wall_s": 2.1,
                    "timed_out": False, "stderr": "", "remaining_stdout": "",
                    "terminated_by_harness": True,
                    "line": json.dumps({
                        "type": "tail_ready", "agents": "all", "events": ["done"]},
                        separators=(",", ":")),
                    "payload": {
                        "type": "tail_ready", "agents": "all", "events": ["done"]},
                },
            },
            "errors": [],
        }
        self.harness._seal_evidence(evidence)
        return evidence

    def _resign(self, evidence: dict) -> dict:
        self.harness._seal_evidence(evidence)
        return evidence

    def test_complete_evidence_passes_every_oracle(self) -> None:
        checks = self.harness.evaluate(self._evidence())
        self.assertTrue(checks)
        self.assertTrue(all(checks.values()), checks)

    def test_each_authoritative_surface_can_invalidate_the_proof(self) -> None:
        changes = (
            ("legacy-pre", lambda value: value["legacy_ab"]["payload"]
             ["pre_ready"].update(match={"working": True})),
            ("legacy-post", lambda value: value["legacy_ab"]["payload"]
             ["post_ready"].update(booting=True)),
            ("tail", lambda value: value["surfaces"]["tail_snapshot"]
             ["payload"].update(booting=True)),
            ("board-once", lambda value: value["surfaces"]
             ["board_once"].update(found=False)),
            ("board-piped", lambda value: value["surfaces"]
             ["board_piped"].update(found=False)),
            ("tail-ready", lambda value: value["surfaces"]
             ["tail_ready"].update(timed_out=True)),
            ("count", lambda value: value["scratch"].update(observed_files=335_999)),
            ("cleanup", lambda value: value["scratch"]["cleanup"].update(removed=False)),
        )
        for label, mutate in changes:
            with self.subTest(label=label):
                evidence = copy.deepcopy(self._evidence())
                mutate(evidence)
                self.assertFalse(all(self.harness.evaluate(evidence).values()))

    def test_fabricated_self_certifying_record_is_rejected(self) -> None:
        evidence = {
            "checks": {"everything": True},
            "passed": True,
        }
        evidence["evidence_sha256"] = self.harness._digest(evidence)
        checks = self.harness.evaluate(evidence)
        self.assertFalse(all(checks.values()))
        self.assertFalse(checks["top_level_schema_exact"])
        self.assertFalse(checks["stored_checks_match_rederived_checks"])

    def test_evidence_loader_rejects_duplicate_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"schema":"first","schema":"forged"}',
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate evidence field"):
                self.harness._load_evidence(path)

    def test_written_evidence_round_trips_through_public_validator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            self.harness._write_evidence(path, self._evidence())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = self.harness._validate_file(path)
            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 0)
            self.assertTrue(payload["valid"])
            self.assertTrue(all(payload["checks"].values()))

    def test_repo_identity_treats_nested_repository_as_opaque_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*arguments: str, cwd: Path = root) -> None:
                self.harness.subprocess.run(
                    ["git", *arguments], cwd=cwd, check=True,
                    capture_output=True)

            git("init", "--quiet")
            git("config", "user.email", "h5@example.invalid")
            git("config", "user.name", "H5 Test")
            tracked = root / "tracked.txt"
            tracked.write_text("outer\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "--quiet", "-m", "outer")

            nested = root / "nested"
            nested.mkdir()
            git("init", "--quiet", cwd=nested)

            with (
                mock.patch.object(self.harness, "ROOT", root),
                mock.patch.object(
                    self.harness, "RELEVANT_FILES", ("tracked.txt",)),
            ):
                identity = self.harness._repo_identity()
                self.assertTrue(identity["dirty"])
                self.assertEqual(set(identity["untracked"]), {"nested/"})
                record = identity["untracked"]["nested/"]
                descriptor = json.loads(self.harness.base64.b64decode(
                    record["content_b64"], validate=True))
                self.assertEqual(descriptor, {
                    "kind": "nested-repository", "path": "nested/",
                })
                self.assertTrue(self.harness._repo_identity_valid(identity))

    def test_raw_and_derived_fields_must_agree(self) -> None:
        changes = (
            ("legacy-derived", lambda value: value["legacy_ab"]["payload"]
             ["pre_ready"].update(booting=False)),
            ("legacy-raw", lambda value: value["legacy_ab"].update(
                stdout=value["legacy_ab"]["stdout"].replace(
                    '"booting": true', '"booting": false', 1))),
            ("tail-derived-payload", lambda value: value["surfaces"]
             ["tail_snapshot"]["payload"].update(booting=True)),
            ("tail-derived-match", lambda value: value["surfaces"]
             ["tail_snapshot"]["match"].update(working=False)),
            ("board-once-derived", lambda value: value["surfaces"]
             ["board_once"].update(found=False)),
            ("board-piped-derived", lambda value: value["surfaces"]
             ["board_piped"].update(found=False)),
            ("tail-ready-derived", lambda value: value["surfaces"]
             ["tail_ready"]["payload"].update(events=[])),
            ("tail-ready-raw", lambda value: value["surfaces"]
             ["tail_ready"].update(line='{"type":"tail_ready"}')),
        )
        for label, mutate in changes:
            with self.subTest(label=label):
                evidence = copy.deepcopy(self._evidence())
                mutate(evidence)
                self._resign(evidence)
                self.assertFalse(all(self.harness.evaluate(evidence).values()))

    def test_each_process_contract_field_is_authoritative(self) -> None:
        runs = (
            ("legacy_ab",),
            ("surfaces", "tail_snapshot"),
            ("surfaces", "board_once"),
            ("surfaces", "board_piped"),
        )
        for path in runs:
            for field, replacement in (
                    ("command", ["forged"]), ("rc", 9),
                    ("timed_out", True), ("stderr", "forged error")):
                with self.subTest(path=path, field=field):
                    evidence = copy.deepcopy(self._evidence())
                    run = evidence
                    for part in path:
                        run = run[part]
                    run[field] = replacement
                    self._resign(evidence)
                    self.assertFalse(all(self.harness.evaluate(evidence).values()))

        for field, replacement in (
                ("command", ["forged"]), ("rc", 0),
                ("timed_out", True), ("stderr", "forged error"),
                ("remaining_stdout", "late output"),
                ("terminated_by_harness", False)):
            with self.subTest(path="tail_ready", field=field):
                evidence = copy.deepcopy(self._evidence())
                evidence["surfaces"]["tail_ready"][field] = replacement
                self._resign(evidence)
                self.assertFalse(all(self.harness.evaluate(evidence).values()))

    def test_identity_topology_cleanup_and_digest_are_authoritative(self) -> None:
        changes = (
            ("source-sha", lambda value: value["identity"].update(
                source_sha256="0" * 64)),
            ("source-body", lambda value: value["identity"].update(
                source_body=value["identity"]["source_body"] + "{}\n")),
            ("topology-count", lambda value: value["scratch"].update(
                observed_old_files=335_998)),
            ("topology-manifest", lambda value: value["scratch"].update(
                file_manifest_sha256="0" * 64)),
            ("repo-file", lambda value: value["repo"]["after"]["files"].update(
                {self.harness.RELEVANT_FILES[0]: "0" * 64})),
            ("repo-dirty", lambda value: value["repo"]["after"].update(
                status_sha256="0" * 64)),
        )
        for label, mutate in changes:
            with self.subTest(label=label):
                evidence = copy.deepcopy(self._evidence())
                mutate(evidence)
                self._resign(evidence)
                self.assertFalse(all(self.harness.evaluate(evidence).values()))

        with tempfile.TemporaryDirectory() as existing:
            evidence = copy.deepcopy(self._evidence())
            evidence["scratch"]["path"] = existing
            self._resign(evidence)
            self.assertFalse(all(self.harness.evaluate(evidence).values()))

        evidence = self._evidence()
        evidence["evidence_sha256"] = "0" * 64
        self.assertFalse(
            self.harness.evaluate(evidence)["evidence_digest_matches_bytes"])

        evidence = self._evidence()
        evidence["checks"]["schema_exact"] = False
        evidence["evidence_sha256"] = self.harness._digest({
            key: value for key, value in evidence.items()
            if key != "evidence_sha256"
        })
        self.assertFalse(
            self.harness.evaluate(evidence)["stored_checks_match_rederived_checks"])


if __name__ == "__main__":
    unittest.main()
