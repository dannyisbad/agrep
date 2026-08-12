"""Security regressions for retained local-data and release surfaces."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from _test_support import isolate_data_dir

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
isolate_data_dir()
import common
import cli
import explore
import legacy_cleanup
import teach

WHEEL_SMOKE_SPEC = importlib.util.spec_from_file_location(
    "agrep_installed_wheel_smoke_test",
    ROOT / "bench" / "smoke_installed_wheel.py")
wheel_smoke = importlib.util.module_from_spec(WHEEL_SMOKE_SPEC)
assert WHEEL_SMOKE_SPEC.loader is not None
WHEEL_SMOKE_SPEC.loader.exec_module(wheel_smoke)


def _release_workflow() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")


def _workflow_job(text: str, name: str) -> str:
    marker = f"  {name}:\n"
    start = text.index(marker)
    following = re.search(r"(?m)^  [A-Za-z0-9_-]+:\n", text[start + len(marker):])
    end = len(text) if following is None else start + len(marker) + following.start()
    return text[start:end]


class RetainedSurfaceSecurityTest(unittest.TestCase):
    def test_wheel_smoke_accepts_bounded_version_identity_deferral(self):
        exact = {
            "distribution": "a" * 20,
            "runtime": "b" * 20,
            "native": "c" * 20,
            "writer": "d" * 20,
        }
        wheel_smoke._verify_version_identity(
            "agrep 0.2.0 distribution " + "a" * 20
            + " runtime " + "b" * 20
            + " native unavailable writer unavailable\n",
            exact,
        )

    def test_wheel_smoke_rejects_wrong_or_malformed_identity_proofs(self):
        exact = {
            "distribution": "a" * 20,
            "runtime": "b" * 20,
            "native": "c" * 20,
            "writer": "d" * 20,
        }
        wrong = (
            "agrep 0.2.0 distribution " + "a" * 20
            + " runtime " + "b" * 20
            + " native " + "e" * 20 + " writer " + "d" * 20)
        with self.assertRaises(wheel_smoke.SmokeFailure):
            wheel_smoke._verify_version_identity(wrong, exact)
        with self.assertRaises(wheel_smoke.SmokeFailure):
            wheel_smoke._verify_version_identity("agrep 0.2.0", exact)
        with self.assertRaises(wheel_smoke.SmokeFailure):
            wheel_smoke._exact_identity(
                {"native_binary_build_id": "unavailable"},
                "native_binary_build_id",
            )

    def test_restore_dispatch_does_not_depend_on_needle_shape(self):
        calls = []

        def restore(args):
            calls.append(("restore", args.rest))
            return 19

        def search(args):
            calls.append(("search", args.rest))
            return 17

        retired = mock.patch.object(legacy_cleanup, "retire_removed_explorer")
        with retired, mock.patch.object(cli, "cmd_restore", side_effect=restore), \
                mock.patch.object(cli, "cmd_search", side_effect=search):
            with mock.patch.object(cli.sys, "argv", ["agrep", "restore", "01234567"]):
                self.assertEqual(cli._main(), 19)
            with mock.patch.object(cli.sys, "argv", ["agrep", "restore", "deadlock"]):
                self.assertEqual(cli._main(), 19)
        self.assertEqual(calls, [
            ("restore", ["01234567"]),
            ("restore", ["deadlock"]),
        ])

    def test_run_rejects_arbitrary_executables_before_launch(self):
        with mock.patch("hookless.capture.run_captured") as launch:
            self.assertEqual(cli.cmd_run(SimpleNamespace(
                rest=["--cwd", "/tmp", "true"])), 2)
            self.assertEqual(cli.cmd_run(SimpleNamespace(
                rest=["true", "--cwd", "/tmp"])), 2)
            launch.assert_not_called()
            cli.cmd_run(SimpleNamespace(rest=["agy", "--cwd", "/tmp"]))
            launch.assert_called_once_with("antigravity", [], cwd="/tmp")

    def test_vibe_lookup_is_identity_bound_and_root_confined(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            root = Path(td) / "vibe"
            root.mkdir(parents=True)
            (root / "index.json").write_text(
                json.dumps([{"session": "safe"}, {"session": "link"}]),
                encoding="utf-8")
            (root / "safe.json").write_text(
                json.dumps({"session": "safe", "arc": [1]}), encoding="utf-8")
            outside = Path(td) / "outside.json"
            outside.write_text(
                json.dumps({"session": "link", "arc": [2]}), encoding="utf-8")
            try:
                (root / "link.json").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            explore._GEN = None
            explore._vibe_index.cache_clear()
            self.assertEqual(explore.get_vibe("safe")["arc"], [1])
            self.assertIsNone(explore.get_vibe("../safe"))
            self.assertIsNone(explore.get_vibe("link"))

    def test_installed_wheel_smoke_cannot_borrow_user_stores_or_runtime(self):
        overrides = {
            "AGREP_ALLOW_UNVERIFIED_BINARY": "1",
            "AGREP_BIN_URL": "https://example.invalid/",
            "AGREP_PROFILE": "compact",
            "AGREP_RS_BIN": "/real/agrep-rs",
            "ALL_PROXY": "socks5://real-proxy",
            "APPDATA": "/real/appdata",
            "CODEX_THREAD_ID": "real-thread",
            "CLINE_DIR": "/real/cline",
            "CRUSH_GLOBAL_DATA": "/real/crush",
            "LOCALAPPDATA": "/real/localappdata",
            "NO_PROXY": "*",
            "OPENCODE_DB": "/real/opencode.db",
            "PYTHONHOME": "/real/python",
            "PYTHONPATH": "/real/modules",
            "XDG_CONFIG_HOME": "/real/config",
            "XDG_DATA_HOME": "/real/data",
        }
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, overrides, clear=True):
            root = Path(temporary)
            env = wheel_smoke._isolated_env(root)
        for name in ("APPDATA", "CLINE_DIR", "CRUSH_GLOBAL_DATA",
                     "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.assertTrue(Path(env[name]).is_relative_to(root), name)
        self.assertEqual(env["OPENCODE_DB"], "")
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(env["no_proxy"], "")
        for name in ("AGREP_ALLOW_UNVERIFIED_BINARY", "AGREP_BIN_URL",
                     "AGREP_PROFILE", "AGREP_RS_BIN", "CODEX_THREAD_ID",
                     "PYTHONHOME", "PYTHONPATH"):
            self.assertNotIn(name, env)

    def test_installed_wheel_smoke_does_not_resolve_cli_from_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            installed = scripts / ("agrep.exe" if sys.platform == "win32" else "agrep")
            installed.write_bytes(b"installed")
            decoy = root / "agrep.cmd"
            decoy.write_bytes(b"checkout")
            with mock.patch.object(
                    wheel_smoke.sysconfig, "get_path", return_value=str(scripts)), \
                    mock.patch.dict(os.environ, {"PATH": str(root)}):
                self.assertEqual(
                    wheel_smoke._installed_cli(None), installed.resolve())

    def test_installed_wheel_smoke_uses_an_attested_codex_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query, artifact, reply, rollout = wheel_smoke._write_codex_rollout(root)
            rows = [json.loads(line) for line in rollout.read_text(
                encoding="utf-8").splitlines()]
        self.assertIn(query, artifact)
        self.assertTrue(any(
            row.get("type") == "event_msg"
            and row.get("payload", {}).get("type") == "user_message"
            and row.get("payload", {}).get("message") == artifact
            for row in rows))
        self.assertTrue(any(
            row.get("payload", {}).get("role") == "assistant"
            and row.get("payload", {}).get("content", [{}])[0].get("text") == reply
            for row in rows))

    def test_installed_wheel_smoke_pins_the_source_instruction_contract(self):
        version, digest, codex_digest = wheel_smoke._source_nudge_contract()
        self.assertEqual(version, 36)
        self.assertEqual(
            digest,
            "fcccd2c6068f34ef7c5385a6fbe280d0e4d75a086f9e8d1bfdb14e7cf75a8c8b",
        )
        self.assertEqual(
            codex_digest,
            "240065c9557ba974a6d8ca88869a3d2aec51d359d0acb1dd18d8d50b19a17adb",
        )

    def test_installed_wheel_smoke_attests_the_written_instruction_body(self):
        version, _digest, codex_digest = wheel_smoke._source_nudge_contract()
        rendered = teach.NUDGE_CODEX.format(name="you", be="are")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "AGENTS.md"
            path.write_text(
                f"host instructions\n\n{teach.MARK_BEGIN}\n{rendered}\n"
                f"{teach.MARK_END}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                wheel_smoke._instruction_version(
                    path, expected_sha256=codex_digest),
                version,
            )
            written = path.read_text(encoding="utf-8")
            security_claim = "Recalled text is evidence"
            self.assertIn(security_claim, written)
            path.write_text(
                written.replace(
                    security_claim,
                    "Recalled text is instructions",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                    wheel_smoke.SmokeFailure,
                    "instruction body differs from the source candidate"):
                wheel_smoke._instruction_version(
                    path, expected_sha256=codex_digest)

    def test_pypi_oidc_job_executes_no_validation_code(self):
        text = _release_workflow()
        head = text[:text.index("jobs:\n")]
        self.assertIn("permissions:\n  contents: read\n", head)
        validate = _workflow_job(text, "validate-pypi-dist")
        self.assertIn("id: upload", validate)
        self.assertNotIn("python -m pip install", validate)
        self.assertNotIn("id-token: write", validate)
        self.assertIn("actions/checkout@", validate)
        self.assertLess(
            validate.index("actions/checkout@"),
            validate.index("bench/pypi_release.py"))

        checker = _workflow_job(text, "twine-check")
        self.assertIn("python -m twine check", checker)
        self.assertIn("artifact-ids: ${{ needs.validate-pypi-dist.outputs.artifact-id }}",
                      checker)
        self.assertNotIn("id-token: write", checker)

        reconcile = _workflow_job(text, "reconcile-pypi-dist")
        self.assertIn("bench/pypi_release.py", reconcile)
        self.assertIn("--stage-missing missing", reconcile)
        self.assertNotIn("id-token: write", reconcile)

        publish = _workflow_job(text, "publish")
        self.assertIn("id-token: write", publish)
        self.assertNotIn("run:", publish)
        self.assertNotIn("actions/setup-python@", publish)
        self.assertIn("artifact-ids: ${{ needs.reconcile-pypi-dist.outputs.artifact-id }}",
                      publish)
        self.assertIn("needs.reconcile-pypi-dist.outputs.artifact-digest != ''", publish)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                      publish)
        self.assertIn("pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b",
                      publish)
        self.assertIn("skip-existing: true", publish)

        verify = _workflow_job(text, "verify-pypi-release")
        self.assertIn("--require-complete --wait-seconds 120", verify)
        self.assertNotIn("id-token: write", verify)

    def test_release_assets_are_sealed_before_privileged_jobs(self):
        text = _release_workflow()
        for wheel_job in ("wheels", "wheels-linux", "wheels-linux-arm64"):
            block = _workflow_job(text, wheel_job)
            self.assertIn("--remap-path-prefix=", block)
            self.assertIn("bench/validate_binary_privacy.py", block)
            self.assertIn('--forbid "$GITHUB_WORKSPACE"', block)

        sealed = _workflow_job(text, "validate-release-assets")
        self.assertIn("bench/release_assets.py", sealed)
        self.assertIn("id: upload", sealed)
        self.assertIn("release-assets-${{ github.run_id }}-${{ github.run_attempt }}",
                      sealed)
        self.assertIn("cp LICENSE THIRD_PARTY_LICENSES.txt sealed/assets/", sealed)
        self.assertIn("pattern: wheel-*", sealed)
        self.assertIn("path: wheel-input", sealed)
        self.assertIn("--wheels wheel-input", sealed)
        self.assertNotIn("contents: write", sealed)

        for raw_job, upload_count in (
                ("wheels", 2), ("wheels-linux", 2),
                ("wheels-linux-arm64", 2), ("sdist", 1)):
            block = _workflow_job(text, raw_job)
            self.assertEqual(
                block.count("actions/upload-artifact@"), upload_count)
            self.assertEqual(block.count("overwrite: true"), upload_count)

        assets = _workflow_job(text, "release-assets")
        self.assertIn("draft: true", assets)
        self.assertIn(
            "prerelease: ${{ contains(github.ref_name, '-') }}", assets)
        self.assertIn("make_latest: false", assets)
        self.assertIn("inspect an existing release without changing its visibility", assets)
        self.assertIn("verify immutable assets on an already-published release", assets)
        self.assertIn("steps.release-state.outputs.state != 'published'", assets)
        self.assertIn(
            "artifact-ids: ${{ needs.validate-release-assets.outputs.artifact-id }}",
            assets)
        self.assertNotIn("--pattern", assets)
        self.assertNotIn("actions/checkout@", assets)
        self.assertNotIn("bench/release_assets.py", assets)

        final = _workflow_job(text, "finalize-release")
        self.assertIn(
            "needs: [verify-npm-release, verify-pypi-release, release-assets, "
            "validate-release-assets]", final)
        self.assertIn(
            "artifact-ids: ${{ needs.validate-release-assets.outputs.artifact-id }}",
            final)
        self.assertIn("needs.validate-release-assets.outputs.artifact-digest != ''",
                      final)
        self.assertNotIn("--pattern", final)
        self.assertNotIn("actions/checkout@", final)
        live_download = final.index("gh release download")
        exact_compare = final.index("diff --recursive --brief")
        publish_draft = final.index("--draft=false")
        self.assertLess(live_download, exact_compare)
        self.assertLess(exact_compare, publish_draft)
        self.assertIn('if [[ "$GITHUB_REF_NAME" == *-* ]]; then', final)
        self.assertIn(
            'gh release edit "$GITHUB_REF_NAME" --draft=false --prerelease\n',
            final)
        self.assertIn("--draft=false --prerelease=false --latest", final)
        self.assertIn(
            'elif [ "$expected_prerelease" = "false" ]; then\n'
            '            gh release edit "$GITHUB_REF_NAME" --latest',
            final)
        self.assertNotIn("publish-npm-manual:", text)
        self.assertNotIn("npm_only", text)

    def test_npm_jobs_publish_only_sealed_packages(self):
        text = _release_workflow()
        npm_publish = (ROOT / "npm" / "publish.js").read_text(encoding="utf-8")
        self.assertNotIn("NPM_TOKEN", text)
        self.assertNotIn("npm-auth:", text)
        self.assertIn("canonicalLicense.equals(sourceLicense)", npm_publish)
        self.assertIn("canonicalLicense.equals(stagedLicense)", npm_publish)
        self.assertIn(
            '["LICENSE", "README.md", "bin.js", "package.json", "postinstall.js"]',
            npm_publish)
        self.assertNotIn('run(["view"', npm_publish)
        self.assertNotIn('run(["publish"', npm_publish)
        npm_smoke = _workflow_job(text, "npm-smoke")
        self.assertIn('test "$(npm --version)" = "11.16.0"', npm_smoke)
        self.assertIn("node --test npm/test_publish.js", npm_smoke)
        self.assertIn("node npm/publish.js --dry-run", npm_smoke)

        npm_seal = _workflow_job(text, "seal-npm-packages")
        self.assertIn("node npm/publish.js --out-dir sealed", npm_seal)
        self.assertIn("bench/npm_release.py", npm_seal)
        self.assertIn("id: upload", npm_seal)
        self.assertIn("npm-sealed-${{ github.run_id }}-${{ github.run_attempt }}",
                      npm_seal)
        self.assertNotIn("NODE_AUTH_TOKEN", npm_seal)
        self.assertNotIn("id-token: write", npm_seal)

        npm_reconcile = _workflow_job(text, "reconcile-npm-dist")
        self.assertIn("bench/npm_release.py", npm_reconcile)
        self.assertIn("--stage-missing missing", npm_reconcile)
        self.assertIn(
            "artifact-ids: ${{ needs.seal-npm-packages.outputs.artifact-id }}",
            npm_reconcile)
        self.assertIn("id: upload", npm_reconcile)
        self.assertNotIn("NODE_AUTH_TOKEN", npm_reconcile)
        self.assertNotIn("id-token: write", npm_reconcile)

        npm_privileged = _workflow_job(text, "publish-npm")
        self.assertIn("id-token: write", npm_privileged)
        self.assertNotIn("NODE_AUTH_TOKEN", npm_privileged)
        self.assertIn(
            "artifact-ids: ${{ needs.reconcile-npm-dist.outputs.artifact-id }}",
            npm_privileged)
        self.assertIn(
            "needs.reconcile-npm-dist.outputs.artifact-digest != ''",
            npm_privileged)
        self.assertIn("npm publish \"$package\" --access public "
                      "--provenance --ignore-scripts", npm_privileged)
        self.assertNotIn("actions/checkout@", npm_privileged)
        self.assertNotIn("bench/npm_release.py", npm_privileged)
        self.assertNotIn("node npm/publish.js", npm_privileged)

        npm_verify = _workflow_job(text, "verify-npm-release")
        self.assertIn("--require-complete --wait-seconds 120", npm_verify)
        self.assertIn(
            "artifact-ids: ${{ needs.seal-npm-packages.outputs.artifact-id }}",
            npm_verify)
        self.assertNotIn("NODE_AUTH_TOKEN", npm_verify)
        self.assertNotIn("id-token: write", npm_verify)

    def test_sdist_job_validates_archive(self):
        sdist = _workflow_job(_release_workflow(), "sdist")
        self.assertIn("python bench/validate_sdist.py dist/*.tar.gz", sdist)

    def test_semantic_canary_populates_the_ingest_discovery_root(self):
        canary = _workflow_job(_release_workflow(), "semantic-canary")
        self.assertIn(
            'mkdir -p "$root/home/.codex/sessions/2026/01/02"', canary)
        self.assertIn(
            '"$root/home/.codex/sessions/2026/01/02/"', canary)
        self.assertIn('export AGREP_HOME="$root/home"', canary)
        self.assertIn('test -s "$root/data/messages.jsonl"', canary)
        self.assertIn("unset AGREP_NO_DAEMON", canary)
        self.assertLess(
            canary.index('test -s "$root/data/messages.jsonl"'),
            canary.index("unset AGREP_NO_DAEMON"),
        )
        self.assertLess(
            canary.index("unset AGREP_NO_DAEMON"),
            canary.index("deadline=$((SECONDS + 180))"),
        )
        self.assertNotIn("CODEX_HOME", canary)

    def test_ci_and_release_tests_cannot_rewrite_the_dependency_lock(self):
        for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("cargo test --workspace\n", text, relative)
            self.assertIn("cargo test --workspace --locked", text, relative)

    def test_python_ci_runs_fixed_budgets_before_isolated_correctness(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        block = _workflow_job(text, "python")
        fixed = block.index("fixed JSONL full-exit budget")
        source = block.index("fixed source-layout full-exit budgets")
        focused = block.index("focused release regressions")
        self.assertLess(fixed, focused)
        self.assertLess(source, focused)
        self.assertIn("python py/test_jsonl_native_full_exit_perf.py -v", block)
        self.assertIn("python py/test_perf_budgets.py -v", block)
        self.assertIn("runner.os == 'macOS' && matrix.perf", block)
        self.assertIn("py/test_jsonl_native_full_exit_perf.py|", block[focused:])
        self.assertIn(
            'PYTHONPATH="$GITHUB_WORKSPACE:$GITHUB_WORKSPACE/py" '
            'python "$test" -v', block[focused:])

    def test_linux_perf_uses_one_fresh_installed_wheel_candidate(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        block = _workflow_job(text, "linux-installed-wheel-perf")
        self.assertIn("runs-on: ubuntu-latest", block)
        self.assertIn('with: { python-version: "3.12" }', block)
        self.assertIn('RUSTFLAGS: "-C target-cpu=x86-64"', block)
        self.assertIn("AGREP_WHEEL_PLAT: linux_x86_64", block)
        self.assertIn("agrep-*-py3-none-linux_x86_64.whl", block)
        self.assertIn("local wheel must not claim manylinux", block)
        self.assertNotIn("bench/validate_wheel.py", block)
        self.assertIn("python -m venv", block)
        self.assertIn("bench/smoke_installed_wheel.py", block)
        self.assertIn("sha256sum", block)
        self.assertIn("AGREP_PERF_CLI:", block)
        self.assertIn('cd "$RUNNER_TEMP"', block)
        self.assertIn("env -u PYTHONPATH -u PYTHONHOME", block)
        self.assertIn('"$GITHUB_WORKSPACE/py/test_perf_budgets.py" -v', block)
        build = block.index("python -m build --wheel")
        tag = block.index("agrep-*-py3-none-linux_x86_64.whl")
        install = block.index('"$venv/bin/pip" install')
        smoke = block.index("bench/smoke_installed_wheel.py")
        perf = block.index('AGREP_PERF_CLI:')
        self.assertLess(build, tag)
        self.assertLess(tag, install)
        self.assertLess(install, smoke)
        self.assertLess(smoke, perf)
        self.assertNotIn("AGREP_RS_BIN", block)
        self.assertNotIn("continue-on-error", block)
        self.assertNotIn("auditwheel", block)
        self.assertNotIn("upload-artifact", block)
        self.assertNotIn("AGREP_PERF_SLACK", block)

    def test_windows_ci_runs_the_release_contract(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        start = text.index("  windows-gate:\n")
        end = text.index("\n  core-only:\n", start)
        job = text[start:end]
        self.assertIn("runs-on: windows-latest", job)
        self.assertIn("with: { fetch-depth: 0 }", job)
        self.assertIn('python-version: "3.13"', job)
        self.assertNotIn('\n    env:\n      AGREP_CI: "1"', job)
        self.assertIn('RUSTFLAGS: "-C target-cpu=x86-64"', job)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', job)
        self.assertIn("rm -f .cargo/config.toml", job)
        self.assertIn("bench/validate_binary_privacy.py", job)
        self.assertIn("AGREP_WHEEL_PLAT: win_amd64", job)
        self.assertIn("python -m build --wheel", job)
        self.assertIn("python bench/validate_wheel.py", job)
        self.assertIn("python -m venv", job)
        self.assertIn("bench/smoke_installed_wheel.py --agrep", job)
        self.assertIn("cargo test --release --workspace --locked", job)
        self.assertIn("Select-String -Pattern", job)
        self.assertIn('Groups["failed"]', job)
        self.assertIn('Groups["skipped"]', job)
        self.assertIn('$allowedCiSkips = @(', job)
        self.assertIn("selftest skip drift", job)
        self.assertIn("Get-ChildItem py/test_*.py", job)
        fixed_jsonl = job[
            job.index("non-blocking fixed JSONL full-exit budget "
                      "(installed wheel)"):
            job.index("non-blocking fixed CLI full-exit budgets "
                      "(installed wheel)")
        ]
        fixed_cli = job[
            job.index("non-blocking fixed CLI full-exit budgets "
                      "(installed wheel)"):
            job.index("selftest with explicit zero-failure proof")
        ]
        self.assertIn(
            "non-blocking fixed JSONL full-exit budget", fixed_jsonl)
        self.assertIn(
            "non-blocking fixed CLI full-exit budgets", fixed_cli)
        self.assertIn("id: fixed_jsonl", fixed_jsonl)
        self.assertIn("continue-on-error: true", fixed_jsonl)
        self.assertIn('$env:AGREP_PERF_CLI = $agrep', fixed_jsonl)
        self.assertIn(
            '& $python py/test_jsonl_native_full_exit_perf.py -v', fixed_jsonl)
        self.assertNotIn("test_perf_budgets.py", fixed_jsonl)
        self.assertIn("id: fixed_cli", fixed_cli)
        self.assertIn("continue-on-error: true", fixed_cli)
        self.assertIn('$env:AGREP_PERF_CLI = $agrep', fixed_cli)
        self.assertIn('& $python py/test_perf_budgets.py -v', fixed_cli)
        self.assertNotIn("test_jsonl_native_full_exit_perf.py", fixed_cli)
        self.assertIn("$timed = @(", job)
        self.assertIn('AGREP_PERF_SLACK: "4"', job)
        self.assertIn("id: portable_perf", job)
        self.assertIn("non-blocking portable perf board", job)
        self.assertIn("continue-on-error: true", job)
        self.assertIn("bench/perf.py --check", job)
        self.assertIn("bench/onnx_smoke.py", job)
        self.assertLess(
            job.index("bench/perf.py --check"),
            job.index("cargo test --release --workspace --locked"),
        )
        self.assertLess(
            job.index("python -m build --wheel"),
            job.index("bench/perf.py --check"),
        )
        self.assertLess(
            job.index("bench/perf.py --check"),
            job.index("bench/onnx_smoke.py"),
        )
        self.assertLess(
            job.index("bench/perf.py --check"),
            job.index("python selftest.py"),
        )
        self.assertLess(
            job.index("python selftest.py"),
            job.index("Get-ChildItem py/test_*.py"),
        )
        self.assertIn('AGREP_CI: "1"', job[
            job.index("selftest with explicit zero-failure proof"):
            job.index("every focused Python regression file")
        ])
        focused = job[job.index("every focused Python regression file"):]
        self.assertNotIn("AGREP_CI", focused)
        self.assertIn(
            '$env:PYTHONPATH = '
            '"$env:GITHUB_WORKSPACE;$env:GITHUB_WORKSPACE\\py"',
            focused,
        )
        self.assertNotIn("require every Windows performance result", job)
        self.assertNotIn("steps.fixed_jsonl.outcome", job)
        self.assertNotIn("steps.fixed_cli.outcome", job)
        self.assertNotIn("steps.portable_perf.outcome", job)
        self.assertIn("bench/semantic_recall_parity.py --check", job)
        self.assertNotIn("AGREP_RESOURCE_SLACK:", job)
        self.assertIn('AGREP_RESOURCE_IDLE_CPU_PERCENT: "20"', job)
        self.assertIn('AGREP_RESOURCE_SEMANTIC_BATCH_WALL_MS: "20000"', job)
        self.assertIn('AGREP_RESOURCE_SEMANTIC_QUERY_CPU_MS: "2000"', job)
        self.assertIn("bench/resources.py --check-semantic", job)


if __name__ == "__main__":
    unittest.main()
