from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_manylinux_policy_test", ROOT / "bench" / "validate_manylinux.py")
manylinux = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(manylinux)

SEARCH_JSON_SPEC = importlib.util.spec_from_file_location(
    "agrep_search_json_canary_test", ROOT / "bench" / "validate_search_json.py")
search_json = importlib.util.module_from_spec(SEARCH_JSON_SPEC)
assert SEARCH_JSON_SPEC.loader is not None
SEARCH_JSON_SPEC.loader.exec_module(search_json)


class SearchJsonCanaryTests(unittest.TestCase):
    def _page(self, records: list[dict]) -> tuple[dict, list[dict]]:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "page.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8")
            return search_json.parse_page(path)

    def test_release_canary_reads_engine_once_and_keeps_tool_hits(self):
        meta, hits = self._page([
            {"kind": "agrep-meta", "engine": "semantic:hybrid"},
            {"kind": "tool", "snippet": "retry the fetch helper"},
        ])

        self.assertTrue(search_json.relevant_page(
            meta, hits, engine_prefix="semantic:", needles=["retry", "fetch"]))

    def test_release_canary_rejects_missing_or_repeated_envelopes(self):
        with self.assertRaisesRegex(search_json.InvalidSearchPage, "leading"):
            self._page([{"engine": "semantic:hybrid", "text": "retry"}])
        with self.assertRaisesRegex(search_json.InvalidSearchPage, "multiple"):
            self._page([
                {"kind": "agrep-meta", "engine": "semantic:hybrid"},
                {"kind": "agrep-meta", "engine": "semantic:hybrid"},
            ])


def _job(text: str, name: str) -> str:
    return _jobs(text)[name]


def _jobs(text: str) -> dict[str, str]:
    _, marker, body = text.partition("\njobs:\n")
    if not marker:
        raise AssertionError("workflow has no jobs mapping")
    matches = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\n", body))
    jobs = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        if name in jobs:
            raise AssertionError(f"workflow repeats job: {name}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        jobs[name] = body[match.start():end]
    if not jobs:
        raise AssertionError("workflow jobs mapping is empty")
    return jobs


def _job_field(block: str, name: str) -> str | None:
    lines = block.splitlines()
    indexes = [
        index for index, line in enumerate(lines)
        if line.startswith(f"    {name}:")
    ]
    if len(indexes) > 1:
        raise AssertionError(f"job repeats field: {name}")
    if not indexes:
        return None
    index = indexes[0]
    value = lines[index].split(":", 1)[1].strip()
    if value not in {">", ">-", "|", "|-"}:
        return value
    continuation = []
    for line in lines[index + 1:]:
        if line and len(line) - len(line.lstrip()) <= 4:
            break
        if line.strip():
            continuation.append(line.strip())
    return " ".join(continuation)


def _job_permissions(block: str) -> dict[str, str]:
    lines = block.splitlines()
    try:
        index = lines.index("    permissions:")
    except ValueError:
        return {}
    permissions = {}
    for line in lines[index + 1:]:
        if line and len(line) - len(line.lstrip()) <= 4:
            break
        match = re.fullmatch(r"      ([A-Za-z0-9_-]+):\s*([^\s#]+).*$", line)
        if match:
            permissions[match.group(1)] = match.group(2)
    return permissions


def _is_publication_sensitive(block: str) -> bool:
    if "write" in _job_permissions(block).values():
        return True
    patterns = (
        r"\$\{\{\s*secrets\.",
        r"pypa/gh-action-pypi-publish@",
        r"softprops/action-gh-release@",
        r"(?m)^\s+(?:npm|pnpm) publish(?:\s|$)",
        r"(?m)^\s+gh release (?:create|edit|upload)(?:\s|$)",
        r"(?m)^\s+(?:python -m )?twine upload(?:\s|$)",
    )
    return any(re.search(pattern, block) for pattern in patterns)


def _has_release_context_guard(condition: str | None) -> bool:
    if condition is None:
        return False
    normalized = " ".join(condition.split())
    required = (
        "${{ github.repository == 'dannyisbad/agrep' "
        "&& github.event_name == 'push' "
        "&& startsWith(github.ref, 'refs/tags/v')"
    )
    if not normalized.startswith(required):
        return False
    remainder = normalized[len(required):]
    return remainder.startswith(" }}") or remainder.startswith(" && ")


def _release_context_allowed(
    condition: str | None,
    repository: str,
    event: str,
    ref: str,
) -> bool:
    if not _has_release_context_guard(condition):
        raise AssertionError("condition lacks the mandatory release context guard")
    return (
        repository == "dannyisbad/agrep"
        and event == "push"
        and ref.startswith("refs/tags/v")
    )


class ManylinuxPolicyTests(unittest.TestCase):
    def test_accepts_target_or_older_policy_with_wrapped_output(self):
        for actual in ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64"):
            with self.subTest(actual=actual):
                report = (
                    "agrep.whl is consistent with the following platform\n"
                    f'tag: "{actual}".\n')
                self.assertEqual(
                    manylinux.validate(report, "manylinux_2_28_x86_64"),
                    actual,
                )

    def test_rejects_unproven_or_incompatible_policy(self):
        reports = (
            'wheel is consistent with the following platform tag: "linux_x86_64".',
            'wheel is consistent with the following platform tag: '
            '"manylinux_2_34_x86_64".',
            'wheel is consistent with the following platform tag: '
            '"manylinux_2_28_aarch64".',
            'no policy result',
            (
                'wheel is consistent with the following platform tag: '
                '"manylinux_2_28_x86_64". '
                'again is consistent with the following platform tag: '
                '"manylinux_2_28_x86_64".'
            ),
        )
        for report in reports:
            with self.subTest(report=report), self.assertRaises(
                    manylinux.InvalidPolicy):
                manylinux.validate(report, "manylinux_2_28_x86_64")

    def test_both_linux_jobs_enforce_the_computed_policy(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")
        for name, target in (
                ("wheels-linux", "manylinux_2_28_x86_64"),
                ("wheels-linux-arm64", "manylinux_2_28_aarch64")):
            with self.subTest(job=name):
                block = _job(text, name)
                show = block.index(
                    "auditwheel -v show dist/*.whl > /tmp/auditwheel-report.txt")
                enforce = block.index("bench/validate_manylinux.py")
                install = block.index('"$PYBIN/pip" install dist/*.whl')
                self.assertLess(show, enforce)
                self.assertLess(enforce, install)
                self.assertIn(f"--target {target}", block)
                self.assertNotIn("auditwheel -v show dist/*.whl || true", block)

    def test_linux_x64_wheel_runs_binding_installed_full_exit_budgets(self):
        block = _job(
            (ROOT / ".github/workflows/release.yml").read_text(
                encoding="utf-8"),
            "wheels-linux",
        )
        validate = block.index('"$PYBIN/python" bench/validate_wheel.py')
        audit = block.index("auditwheel -v show dist/*.whl")
        enforce = block.index("bench/validate_manylinux.py")
        install = block.index('"$PYBIN/pip" install dist/*.whl')
        smoke = block.index("bench/smoke_installed_wheel.py")
        perf = block.index('AGREP_PERF_CLI="$PYBIN/agrep"')
        upload = block.index("actions/upload-artifact@")
        self.assertLess(validate, audit)
        self.assertLess(audit, enforce)
        self.assertLess(enforce, install)
        self.assertLess(install, smoke)
        self.assertLess(smoke, perf)
        self.assertLess(perf, upload)
        self.assertIn("manylinux_2_28_x86_64@sha256:", block)
        self.assertIn('cd "${RUNNER_TEMP:-/tmp}"', block)
        self.assertIn("env -u PYTHONPATH -u PYTHONHOME", block)
        self.assertIn(
            '"$GITHUB_WORKSPACE/py/test_perf_budgets.py" -v', block)
        self.assertNotIn("AGREP_RS_BIN", block)
        self.assertNotIn("AGREP_PERF_SLACK", block)


class ReleaseBodyTests(unittest.TestCase):
    def test_release_gate_has_history_and_release_mode_rust(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")
        block = _job(text, "release-gate")
        self.assertIn("with: { fetch-depth: 0 }", block)
        self.assertIn("cargo test --workspace --locked --release", block)
        dependencies = block.index("- name: install Python release dependencies")
        fixed_step = block[
            block.index("- name: fixed JSONL full-exit budget"):
            block.index("- name: Python suite and portable budgets")]
        self.assertLess(
            dependencies, block.index("- name: fixed JSONL full-exit budget"))
        self.assertNotIn("AGREP_PERF_SLACK", fixed_step)
        self.assertNotIn("py/test_perf_budgets.py", fixed_step)
        fixed = block.index("python py/test_jsonl_native_full_exit_perf.py -v")
        focused = block.index("for test in py/test_*.py; do")
        self.assertLess(fixed, focused)
        self.assertIn("py/test_jsonl_native_full_exit_perf.py|", block[focused:])
        self.assertIn(
            'PYTHONPATH="$GITHUB_WORKSPACE:$GITHUB_WORKSPACE/py" '
            'python "$test" -v', block[focused:])

    def test_release_body_is_static_and_useful(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")
        block = _job(text, "release-assets")
        lines = block.splitlines()
        start = lines.index("          body: |") + 1
        body = []
        for line in lines[start:]:
            if line and not line.startswith("            "):
                break
            body.append(line[12:] if line else "")
        rendered = "\n".join(body)
        self.assertIn("uv tool install agrep", rendered)
        self.assertIn("npm i -g @mundy/agrep", rendered)
        self.assertIn("SHA-256 sidecars", rendered)
        self.assertIn("remain on your machine", rendered)
        self.assertNotIn("${{", rendered)
        self.assertNotIn("body_path:", block)

    def test_semantic_canary_uses_the_search_page_validator(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")
        block = _job(text, "semantic-canary")
        self.assertIn("bench/validate_search_json.py", block)
        self.assertIn("--engine-prefix semantic:", block)
        self.assertNotIn('row.get("engine")', block)

    def test_npm_publish_marks_reconciled_tarballs_as_local_paths(self):
        text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")
        block = _job(text, "publish-npm")
        self.assertIn('npm publish "./$package"', block)
        self.assertNotIn('npm publish "$package"', block)


class ReleaseRepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8")

    def test_tagged_release_fails_before_checkout_outside_canonical_repo(self):
        block = _job(self.text, "release-preflight")
        guard = block.index(
            "require the canonical repository for tagged releases")
        checkout = block.index("actions/checkout@")
        self.assertLess(guard, checkout)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", block)
        self.assertIn(
            'if [ "$GITHUB_REPOSITORY" != "dannyisbad/agrep" ]; then',
            block,
        )
        self.assertIn("exit 1", block[guard:checkout])

    def test_every_publication_sensitive_job_has_a_job_level_guard(self):
        jobs = _jobs(self.text)
        sensitive = {
            name for name, block in jobs.items()
            if _is_publication_sensitive(block)
        }
        self.assertGreaterEqual(sensitive, {
            "release-assets", "publish", "publish-npm", "finalize-release",
        })
        for name in sorted(sensitive):
            with self.subTest(job=name):
                self.assertTrue(
                    _has_release_context_guard(_job_field(jobs[name], "if")),
                    f"publication-sensitive job lacks release guard: {name}",
                )

    def test_release_context_truth_table_rejects_dispatch_and_foreign_repos(self):
        sensitive = {
            name: block for name, block in _jobs(self.text).items()
            if _is_publication_sensitive(block)
        }
        cases = (
            ("dannyisbad/agrep", "push", "refs/tags/v0.2.0", True),
            ("dannyisbad/agrep", "workflow_dispatch", "refs/tags/v0.2.0", False),
            ("dannyisbad/agrep", "push", "refs/heads/v0.2.0", False),
            ("dannyisbad/agrep-private", "push", "refs/tags/v0.2.0", False),
            ("fork/agrep", "push", "refs/tags/v0.2.0", False),
        )
        for name, block in sorted(sensitive.items()):
            condition = _job_field(block, "if")
            for repository, event, ref, expected in cases:
                with self.subTest(
                        job=name, repository=repository, event=event, ref=ref):
                    self.assertEqual(
                        _release_context_allowed(
                            condition, repository, event, ref),
                        expected,
                    )

    def test_branch_dispatch_remains_available_for_read_only_builds(self):
        trigger = self.text[:self.text.index("\npermissions:\n")]
        self.assertIn("  workflow_dispatch:\n", trigger)
        aggregate = _job(self.text, "validate-built-artifacts")
        self.assertEqual(
            _job_field(aggregate, "if"),
            "${{ !startsWith(github.ref, 'refs/tags/v') }}")
        self.assertIn(
            "needs: [wheels, wheels-linux, wheels-linux-arm64, sdist, "
            "npm-smoke, semantic-canary]", aggregate)
        self.assertIn("pattern: binasset-*", aggregate)
        self.assertIn("pattern: wheel-*", aggregate)
        self.assertIn("name: sdist", aggregate)
        self.assertIn("bench/release_assets.py", aggregate)
        self.assertIn("bench/pypi_release.py", aggregate)
        self.assertIn(
            "from bench.package_metadata import checkout_version", aggregate)
        self.assertNotIn(
            "from bench.package_metadata import distribution_version", aggregate)
        self.assertNotIn("contents: write", aggregate)
        for name in ("release-gate", "wheels", "wheels-linux",
                     "wheels-linux-arm64", "sdist", "semantic-canary",
                     "npm-smoke", "validate-built-artifacts"):
            with self.subTest(job=name):
                block = _job(self.text, name)
                self.assertNotRegex(
                    block, r"(?m)^      (contents|id-token): write$")


class PrivacyWorkflowTests(unittest.TestCase):
    def test_private_ci_proves_candidate_ancestry_and_canonical_ci_all_refs(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        block = _job(text, "sync")
        self.assertIn("with: { fetch-depth: 0 }", block)
        self.assertIn(
            "validate_repo_privacy.py --index --history HEAD", block)
        self.assertEqual(block.count("validate_repo_privacy.py --all-refs"), 1)
        canonical = block[
            block.index("- name: canonical reachable-ref privacy"):
            block.index("- name: release identity")]
        self.assertIn("if: github.repository == 'dannyisbad/agrep'", canonical)
        self.assertNotIn("continue-on-error", canonical)

    def test_release_preflight_unconditionally_scans_every_ref(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")
        block = _job(text, "release-preflight")
        privacy = block[
            block.index("- name: reject private artifacts"):
            block.index("- name: tag, Python, npm, Cargo")]
        self.assertIn(
            "validate_repo_privacy.py --index --all-refs", privacy)
        self.assertNotIn("if:", privacy)
        self.assertNotIn("continue-on-error", privacy)


if __name__ == "__main__":
    unittest.main()
