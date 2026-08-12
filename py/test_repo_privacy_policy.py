"""Repository privacy policy and Git-snapshot regression tests."""

from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "validate_repo_privacy.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_repo_privacy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture Author")
    _git(root, "config", "user.email", "fixture@example.invalid")


class RepoPrivacyPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _load_validator()

    def test_current_checkout_and_validator_sources_are_clean(self) -> None:
        self.assertEqual(self.validator.scan_worktree(ROOT), [])
        self.assertEqual(
            self.validator.scan(ROOT, [SCRIPT, Path(__file__).resolve()]), [])

    def test_validator_sources_are_clean_when_staged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            bench = root / "bench"
            py = root / "py"
            bench.mkdir()
            py.mkdir()
            (bench / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
            (py / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
            _git(root, "add", ".")
            self.assertEqual(self.validator.scan_index(root), [])

    def test_private_artifact_classes_are_rejected_without_echoing_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            capture = root / "bench" / "render" / "capture.txt"
            capture.parent.mkdir(parents=True)
            capture.write_text("private transcript", encoding="utf-8")
            local = root / "RELEASE.local.md"
            local.write_text("private metadata", encoding="utf-8")
            audit = root / "bench" / "adversarial" / "UX_AUDIT.json"
            audit.parent.mkdir()
            audit.write_text("{}", encoding="utf-8")
            findings = self.validator.scan(root, [capture, local, audit])
        self.assertEqual(
            findings,
            [("RELEASE.local.md", "local-only metadata"),
             ("bench/adversarial/UX_AUDIT.json",
              "local or transcript-derived evidence"),
             ("bench/render/capture.txt", "private capture or agent scratch tree")])

    def test_secret_network_foreign_home_and_size_guards(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            suspect = root / "suspect.txt"
            token = "sk-" + "x" * 24
            private_ip = ".".join(("100", "100", "10", "20"))
            mac_home = "/".join(("", "Users", "real-person", "project"))
            mac_temp = "/".join((
                "", "var", "folders", "xy", "fixture" * 6, "T", "artifact"))
            win_home = "C:" + "\\".join(("", "Users", "actual-user", "repo"))
            suspect.write_text(
                f"{token}\n{private_ip}\n{mac_home}\n{mac_temp}\n{win_home}\n",
                encoding="utf-8")
            oversized = root / "oversized.txt"
            oversized.write_bytes(
                b"x" * (self.validator.MAX_REPOSITORY_BYTES + 1))
            findings = self.validator.scan(root, [suspect, oversized])
        reasons = {reason for _path, reason in findings}
        self.assertEqual(
            reasons,
            {"credential-shaped content", "non-synthetic macOS temp path",
             "non-synthetic user home path",
             "private-network address",
             "repository file exceeds 1000000 bytes"})

    def test_personal_identity_tokens_are_caught_by_digest(self) -> None:
        digests = self.validator.PERSONAL_TOKEN_DIGESTS
        self.assertGreaterEqual(len(digests), 7)
        self.assertTrue(all(
            len(digest) == 64 and set(digest) <= set("0123456789abcdef")
            for digest in digests))
        banned = "erin" + "ys"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            leaky = root / "config.py"
            leaky.write_text(f'PROJECT = "{banned}"\n', encoding="utf-8")
            cased = root / "notes.md"
            cased.write_text(f"shipped by {banned.title()} today\n",
                             encoding="utf-8")
            exempt = root / "THIRD_PARTY_LICENSES.txt"
            exempt.write_text(f"Copyright (c) {banned}\n", encoding="utf-8")
            findings = self.validator.scan(root, [leaky, cased, exempt])
        self.assertEqual(
            sorted(findings),
            [("config.py", "personal-identity token"),
             ("notes.md", "personal-identity token")])

    def test_private_evaluation_trees_are_rejected_structurally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            paths = [
                root / "bench" / "adversarial" / "report.json",
                root / "bench" / "gauntlet" / "trace.json",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            findings = self.validator.scan(root, paths)

        self.assertEqual(
            findings,
            [(path.relative_to(root).as_posix(),
              "local or transcript-derived evidence")
             for path in paths])

    def test_banned_external_and_broken_symlinks_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as out:
            root = Path(raw).resolve()
            outside = Path(out) / "outside.txt"
            outside.write_text("clean", encoding="utf-8")
            banned = root / "bench" / "render" / "capture.txt"
            external = root / "external.txt"
            broken = root / "broken.txt"
            banned.parent.mkdir(parents=True)
            try:
                banned.symlink_to(outside)
                external.symlink_to(outside)
                broken.symlink_to(root / "missing.txt")
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            findings = self.validator.scan(root, [banned, external, broken])
        self.assertIn(
            ("bench/render/capture.txt", "private capture or agent scratch tree"),
            findings)
        self.assertIn(("external.txt", "symlink escapes repository"), findings)
        self.assertIn(("broken.txt", "broken repository symlink"), findings)

    def test_worktree_includes_untracked_nonignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            safe = root / "safe.txt"
            safe.write_text("safe", encoding="utf-8")
            _git(root, "add", "safe.txt")
            _git(root, "commit", "-qm", "safe root")
            leak = root / "leak.txt"
            leak.write_text("sk-" + "z" * 24, encoding="utf-8")
            findings = self.validator.scan_worktree(root)
        self.assertEqual(findings, [("leak.txt", "credential-shaped content")])

    def test_index_reads_staged_bytes_not_mutable_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            suspect = root / "suspect.txt"
            suspect.write_text("sk-" + "q" * 24, encoding="utf-8")
            _git(root, "add", "suspect.txt")
            suspect.write_text("safe worktree", encoding="utf-8")
            index_findings = self.validator.scan_index(root)
            worktree_findings = self.validator.scan_worktree(root)
        self.assertEqual(
            index_findings, [("suspect.txt", "credential-shaped content")])
        self.assertEqual(worktree_findings, [])

    def test_history_catches_an_add_then_delete_leak(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            leak = root / "bench" / "render" / "capture.txt"
            leak.parent.mkdir(parents=True)
            leak.write_text("private", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "add capture")
            leak.unlink()
            _git(root, "add", "-u")
            _git(root, "commit", "-qm", "delete capture")
            self.assertEqual(self.validator.scan_index(root), [])
            self.assertEqual(self.validator.scan_worktree(root), [])
            history_findings = self.validator.scan_history(root)
        self.assertIn(
            ("bench/render/capture.txt", "private capture or agent scratch tree"),
            history_findings)

    def test_all_refs_catches_remote_backup_outside_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            safe = root / "safe.txt"
            safe.write_text("safe", encoding="utf-8")
            _git(root, "add", "safe.txt")
            _git(root, "commit", "-qm", "safe root")
            safe_commit = _git(root, "rev-parse", "HEAD").decode().strip()
            _git(root, "checkout", "--detach", "-q")
            leak = root / "bench" / "render" / "capture.txt"
            leak.parent.mkdir(parents=True)
            leak.write_text("private", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "private backup")
            backup = _git(root, "rev-parse", "HEAD").decode().strip()
            _git(root, "update-ref", "refs/remotes/origin/backup", backup)
            _git(root, "checkout", "--detach", "-q", safe_commit)

            self.assertEqual(self.validator.scan_history(root, "HEAD"), [])
            all_refs = self.validator.scan_history(root, "--all")

        self.assertIn(
            ("bench/render/capture.txt", "private capture or agent scratch tree"),
            all_refs)

    def test_cli_all_refs_catches_remote_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            safe = root / "safe.txt"
            safe.write_text("safe", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "safe root")
            safe_commit = _git(root, "rev-parse", "HEAD").decode().strip()
            _git(root, "checkout", "--detach", "-q")
            leak = root / "RELEASE.local.md"
            leak.write_text("private", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "private backup")
            backup = _git(root, "rev-parse", "HEAD").decode().strip()
            _git(root, "update-ref", "refs/remotes/origin/backup", backup)
            _git(root, "checkout", "--detach", "-q", safe_commit)
            output = io.StringIO()
            with redirect_stderr(output):
                code = self.validator.main([os.fspath(root), "--all-refs"])

        self.assertEqual(code, 1)
        self.assertIn("RELEASE.local.md: local-only metadata", output.getvalue())

    def test_all_refs_scans_annotated_tag_messages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            safe = root / "safe.txt"
            safe.write_text("safe", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "safe root")
            private_home = "/Users/" + "actual-user/private-build"
            _git(root, "tag", "-a", "private-note", "-m",
                 private_home)
            findings = self.validator.scan_history(root, "--all")

        self.assertTrue(any(
            path.startswith("<tag:") and reason == "non-synthetic user home path"
            for path, reason in findings))

    def test_history_rejects_machine_local_git_identities(self) -> None:
        cases = (
            ("author", "author", "Public Builder <public@example.invalid>"),
            ("committer", "committer", "Public Builder <public@example.invalid>"),
        )
        for role, expected, public_identity in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                _init_repo(root)
                (root / "safe.txt").write_text("safe", encoding="utf-8")
                _git(root, "add", ".")
                if role == "author":
                    _git(root, "commit", "-qm", "safe root", "--author",
                         "Local Builder <private@build-mac.local>")
                else:
                    _git(root, "config", "user.name", "Local Builder")
                    _git(root, "config", "user.email", "private@build-mac.local")
                    _git(root, "commit", "-qm", "safe root", "--author",
                         public_identity)
                findings = self.validator.scan_history(root)

            self.assertTrue(any(
                path.startswith("<commit:")
                and reason == f"machine-local Git {expected} identity"
                for path, reason in findings))

    def test_history_rejects_machine_local_taggers_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "safe root")
            _git(root, "config", "user.name", "Local Tagger")
            _git(root, "config", "user.email", "private@tag-host.localdomain")
            _git(root, "tag", "-am", "public release", "v1")
            findings = self.validator.scan_history(root, "--all")

        self.assertTrue(any(
            path.startswith("<tag:")
            and reason == "machine-local Git tagger identity"
            for path, reason in findings))

    def test_public_git_identities_do_not_trigger_local_host_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "safe root", "--author",
                 "Public Builder <123+builder@users.noreply.github.com>")
            findings = self.validator.scan_history(root)

        self.assertFalse(any(
            reason.startswith("machine-local Git") for _path, reason in findings))

    def test_cli_does_not_echo_secret_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _init_repo(root)
            secret = "sk-" + "n" * 24
            (root / "leak.txt").write_text(secret, encoding="utf-8")
            output = io.StringIO()
            with redirect_stderr(output):
                code = self.validator.main([os.fspath(root), "--worktree"])
        self.assertEqual(code, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("leak.txt: credential-shaped content", output.getvalue())


if __name__ == "__main__":
    unittest.main()
