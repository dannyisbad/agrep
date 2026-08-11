from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_pypi_release_test", ROOT / "bench" / "pypi_release.py")
pypi_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pypi_release)


class PyPIReleaseTests(unittest.TestCase):
    def _bundle(self, root: Path, version: str = "1.2.3") -> dict[str, str]:
        root.mkdir()
        for index, name in enumerate(
                sorted(pypi_release.expected_filenames(version))):
            (root / name).write_bytes(f"distribution-{index}".encode())
        return pypi_release.local_manifest(root, version)

    def test_partial_release_stages_only_missing_exact_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            local = self._bundle(dist)
            existing = dict(list(local.items())[:2])
            missing = pypi_release.reconcile(
                local, existing, require_complete=False)
            staged = root / "missing"
            pypi_release.stage_missing(dist, staged, missing, local)

            self.assertEqual(
                {path.name for path in staged.iterdir()}, set(missing))
            self.assertEqual(len(missing), len(local) - 2)

    def test_mismatch_unexpected_and_incomplete_releases_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = self._bundle(Path(temporary) / "dist")
        name = next(iter(local))
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            pypi_release.reconcile(
                local, {name: "0" * 64}, require_complete=False)
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            pypi_release.reconcile(
                local, {"other.whl": "0" * 64}, require_complete=False)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            pypi_release.reconcile(local, {}, require_complete=True)

    def test_local_bundle_requires_all_platforms_and_no_extras(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            manifest = self._bundle(dist)
            removed = dist / next(iter(manifest))
            removed.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing"):
                pypi_release.local_manifest(dist, "1.2.3")
            removed.write_bytes(b"restored")
            (dist / "extra.txt").write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                pypi_release.local_manifest(dist, "1.2.3")

    def test_local_bundle_rejects_nonregular_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            self._bundle(dist)
            (dist / "unexpected").mkdir()
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                pypi_release.local_manifest(dist, "1.2.3")

    def test_local_bundle_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            manifest = self._bundle(dist)
            name = next(iter(manifest))
            target = dist / name
            target.unlink()
            try:
                target.symlink_to(next(iter(dist.iterdir())))
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                pypi_release.local_manifest(dist, "1.2.3")


if __name__ == "__main__":
    unittest.main()
