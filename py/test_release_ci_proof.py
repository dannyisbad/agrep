from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_release_ci_proof_test", ROOT / "bench" / "release_ci_proof.py")
proof = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(proof)

SHA = "a" * 40
BRANCH = "master"


def _run(**overrides) -> dict:
    run = {
        "conclusion": "success",
        "event": "push",
        "head_branch": BRANCH,
        "head_sha": SHA,
        "id": 123,
        "path": ".github/workflows/ci.yml",
        "run_attempt": 1,
        "status": "completed",
    }
    run.update(overrides)
    return run


class ReleaseCIProofTests(unittest.TestCase):
    def test_accepts_exact_success_after_default_branch_advances(self):
        payload = {"workflow_runs": [
            _run(id=123, run_attempt=1),
            _run(id=456, run_attempt=2),
        ]}

        selected = proof.validate(payload, sha=SHA, branch=BRANCH)

        self.assertEqual(selected["id"], 456)

    def test_rejects_every_near_match(self):
        cases = (
            {"event": "pull_request"},
            {"head_branch": "feature"},
            {"head_sha": "b" * 40},
            {"path": ".github/workflows/release.yml"},
            {"status": "in_progress"},
            {"conclusion": "failure"},
        )
        for override in cases:
            with self.subTest(override=override), self.assertRaises(
                    proof.InvalidCIProof):
                proof.validate(
                    {"workflow_runs": [_run(**override)]},
                    sha=SHA,
                    branch=BRANCH,
                )

    def test_rejects_malformed_response_and_identity(self):
        for payload, sha, branch in (
                ({}, SHA, BRANCH),
                ({"workflow_runs": "not-a-list"}, SHA, BRANCH),
                ({"workflow_runs": [_run(id="123")]}, SHA, BRANCH),
                ({"workflow_runs": [_run(run_attempt=True)]}, SHA, BRANCH),
                ({"workflow_runs": []}, "not-a-sha", BRANCH),
                ({"workflow_runs": []}, SHA, "")):
            with self.subTest(payload=payload, sha=sha, branch=branch), \
                    self.assertRaises(proof.InvalidCIProof):
                proof.validate(payload, sha=sha, branch=branch)

    def test_release_workflow_gates_tags_with_read_only_actions_access(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        start = text.index("  release-preflight:\n")
        end = text.index("\n  release-gate:\n", start)
        job = text[start:end]

        self.assertIn("  workflow_dispatch:\n", text[:text.index("\njobs:\n")])
        self.assertNotIn("\n    if:", job[:job.index("\n    steps:\n")])
        self.assertIn("permissions:\n      actions: read\n      contents: read", job)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", job)
        self.assertIn('"repos/$GITHUB_REPOSITORY/actions/runs"', job)
        for query in (
                "-f event=push",
                '-f branch="$DEFAULT_BRANCH"',
                '-f head_sha="$GITHUB_SHA"',
                "-f status=success",
        ):
            self.assertIn(query, job)
        self.assertIn("python bench/release_ci_proof.py", job)
        self.assertNotIn("github.event.repository.default_branch == github.sha", job)
        self.assertNotIn("github.event.repository.default_branch.sha", job)


if __name__ == "__main__":
    unittest.main()
