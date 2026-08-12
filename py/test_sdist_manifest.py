from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zlib

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
import package_metadata
SPEC = importlib.util.spec_from_file_location(
    "agrep_sdist_manifest_test", ROOT / "bench" / "validate_sdist.py")
sdist = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
try:
    SPEC.loader.exec_module(sdist)
finally:
    sys.path.pop(0)
SOURCE_SPEC = importlib.util.spec_from_file_location(
    "agrep_sdist_sources_test", ROOT / "sdist_sources.py")
sdist_sources = importlib.util.module_from_spec(SOURCE_SPEC)
assert SOURCE_SPEC.loader is not None
SOURCE_SPEC.loader.exec_module(sdist_sources)

VERSION = package_metadata.distribution_version()


def _gzip_tar(data: bytes, *, flags: int = 0, optional: bytes = b"") -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    encoded = compressor.compress(data) + compressor.flush()
    header = struct.pack(
        "<HBBIBB", 0x8B1F, 8, flags, sdist.ARCHIVE_MTIME, 2, 255)
    trailer = struct.pack(
        "<II", zlib.crc32(data) & 0xFFFF_FFFF, len(data) & 0xFFFF_FFFF)
    return header + optional + encoded + trailer


def _archive(path: Path, *, mode_override: str | None = None,
             owner_override: str | None = None,
             pax_override: str | None = None,
             metadata_override: bytes | None = None) -> None:
    source = sdist._source_files()
    payloads = {name: item.read_bytes() for name, item in source.items()}
    payloads["PKG-INFO"] = (
        metadata_override
        if metadata_override is not None
        else package_metadata.expected_core_metadata(VERSION)
    )
    stream = io.BytesIO()
    prefix = f"agrep-{VERSION}/"
    archive_format = (
        tarfile.PAX_FORMAT if pax_override is not None else tarfile.USTAR_FORMAT)
    with tarfile.open(fileobj=stream, mode="w:", format=archive_format) as tar:
        for name, data in payloads.items():
            member = tarfile.TarInfo(prefix + name)
            member.size = len(data)
            member.mtime = sdist.ARCHIVE_MTIME
            member.mode = (
                0o777 if name == mode_override
                else sdist.SOURCE_FILE_MODE
            )
            if name == owner_override:
                member.uid = member.gid = 501
                member.uname = member.gname = "private-builder"
            if name == pax_override:
                member.pax_headers = {
                    "agrep.builder-path": "/Users/private-builder/project"}
            tar.addfile(member, io.BytesIO(data))
    path.write_bytes(_gzip_tar(stream.getvalue()))


@unittest.skipIf(sys.version_info < (3, 11), "tomllib is required by release gates")
class SdistManifestTests(unittest.TestCase):
    def test_optional_dependency_marker_is_grouped_before_extra(self):
        requirement = (
            "mlx>=0.20; sys_platform == 'darwin' or "
            "platform_machine == 'arm64'"
        )
        self.assertEqual(
            package_metadata._optional_requires_dist("metal", requirement),
            "mlx>=0.20; (sys_platform == 'darwin' or "
            "platform_machine == 'arm64') and extra == 'metal'",
        )

    def test_optional_dependency_marker_rejects_ambiguous_forms(self):
        for requirement in (
            "mlx>=0.20;",
            " mlx>=0.20; sys_platform == 'darwin'",
            " numpy>=1.24",
            "numpy>=1.24 ",
            "mlx>=0.20; sys_platform == 'darwin'; extra == 'metal'",
            "mlx>=0.20; sys_platform == 'darwin'\nInjected: value",
        ):
            with self.subTest(requirement=requirement), self.assertRaises(
                    package_metadata.InvalidMetadata):
                package_metadata._optional_requires_dist("metal", requirement)

    def test_empty_optional_group_still_validates_extra_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("agrep/__init__.py", "LICENSE", "README.md"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            pyproject = pyproject.replace(
                "[project.optional-dependencies]\n",
                "[project.optional-dependencies]\nbad_name = []\n",
                1,
            )
            (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")

            with self.assertRaisesRegex(
                    package_metadata.InvalidMetadata,
                    "unsupported optional dependency name"):
                package_metadata.expected_core_metadata(VERSION, root=root)

    def test_core_metadata_uses_hatchling_optional_dependency_order(self):
        headers = package_metadata.expected_core_metadata(VERSION).split(
            b"\n\n", 1)[0]
        metal = headers.index(b"Provides-Extra: metal\n")
        semantic = headers.index(b"Provides-Extra: semantic\n")
        self.assertLess(metal, semantic)
        self.assertIn(
            b"Requires-Dist: mlx>=0.20; (sys_platform == 'darwin' and "
            b"platform_machine == 'arm64') and extra == 'metal'\n",
            headers + b"\n",
        )

    def test_sdist_patterns_exclude_ignored_nested_worktrees(self):
        assert tomllib is not None
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        target = config["tool"]["hatch"]["build"]["targets"]["sdist"]
        includes = target["include"]
        excludes = target["exclude"]
        self.assertTrue(all(pattern.startswith("/") for pattern in includes))
        self.assertTrue(all(pattern.startswith("/") for pattern in excludes))
        self.assertFalse(any(set(pattern) & set("*?[") for pattern in includes))
        self.assertEqual(target["only-include"], ["sdist_sources.py"])

        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            subprocess.run(
                ["git", "init", "-q"], cwd=checkout, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            exclude = checkout / ".git" / "info" / "exclude"
            exclude.write_text("**/.claude/worktrees/\n", encoding="utf-8")
            for name in sdist_sources.ROOT_FILES:
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            manifest = checkout / "py" / "runtime_manifest.json"
            entries = [
                {"source": "agrep/__init__.py", "member": "agrep/__init__.py"},
                {"source": "py/runtime_manifest.json",
                 "member": "agrep/py/runtime_manifest.json"},
                {"source": "py/search.py", "member": "agrep/py/search.py"},
            ]
            for entry in entries:
                path = checkout / entry["source"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            manifest.write_text(json.dumps({"version": 1, "files": entries}),
                                encoding="utf-8")
            crate_files = (
                "crates/core/Cargo.toml", "crates/core/src/lib.rs",
                "crates/core/tests/private.rs",
            )
            for name in crate_files:
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            nested = checkout / ".claude" / "worktrees" / "private"
            decoys = (
                "Cargo.lock", "README.md", "crates/core/src/lib.rs",
                "py/search.py", "bench/private.txt", "web/app.html",
            )
            for name in decoys:
                path = nested / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private decoy\n", encoding="utf-8")
                ignored = subprocess.run(
                    ["git", "check-ignore", "--quiet", str(path)],
                    cwd=checkout, check=False)
                self.assertEqual(ignored.returncode, 0, name)

            selected = sdist_sources.source_files(checkout)
            self.assertIn("py/search.py", selected)
            self.assertIn("crates/core/src/lib.rs", selected)
            self.assertNotIn("crates/core/tests/private.rs", selected)
            self.assertFalse(any(name.startswith(".claude/") for name in selected))

    def test_source_manifest_is_regular_and_excludes_development_payloads(self):
        files = sdist._source_files()
        self.assertEqual(files, sdist_sources.source_files(ROOT))
        self.assertTrue(all(path.is_file() for path in files.values()))
        self.assertFalse(any(name.startswith("bench/") for name in files))
        self.assertFalse(any(name.startswith("npm/") for name in files))
        self.assertFalse(any("/tests/" in name for name in files))
        self.assertFalse(any(Path(name).name.startswith("test_") for name in files))
        self.assertNotIn(".gitignore", files)
        self.assertIn("hatch_sdist_build.py", files)

    def test_member_path_validation_rejects_archive_escapes(self):
        for name in ("../secret", "/absolute", "root\\child", "root/./child"):
            with self.subTest(name=name), self.assertRaises(sdist.InvalidSdist):
                sdist._safe_relative(name)

    def test_valid_closed_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / f"agrep-{VERSION}.tar.gz"
            _archive(path)
            self.assertEqual(
                sdist.validate(path), (VERSION, len(sdist._source_files()) + 1))

    def test_rejects_physical_gzip_tails_and_optional_metadata(self):
        for case in ("tail", "second", "filename"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / f"agrep-{VERSION}.tar.gz"
                _archive(path)
                raw = path.read_bytes()
                if case == "tail":
                    path.write_bytes(raw + b"/Users/private-builder")
                elif case == "second":
                    path.write_bytes(raw + _gzip_tar(b"/Users/private-builder"))
                else:
                    path.write_bytes(raw[:3] + b"\x08" + raw[4:10]
                                     + b"private-builder\x00" + raw[10:])
                with self.assertRaises(sdist.InvalidSdist):
                    sdist.validate(path)

    def test_rejects_owner_mode_and_metadata_drift(self):
        metadata = package_metadata.expected_core_metadata(VERSION)
        cases = {
            "owner": {"owner_override": "Cargo.lock"},
            "mode": {"mode_override": "Cargo.lock"},
            "pax": {"pax_override": "Cargo.lock"},
            "metadata": {
                "metadata_override": metadata.replace(
                    b"\n\n", b"\nAuthor-email: private@example.test\n\n", 1)
            },
        }
        for case, options in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / f"agrep-{VERSION}.tar.gz"
                _archive(path, **options)
                with self.assertRaises(sdist.InvalidSdist):
                    sdist.validate(path)


if __name__ == "__main__":
    unittest.main()
