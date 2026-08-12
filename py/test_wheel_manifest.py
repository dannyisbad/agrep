from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
import package_metadata
import validate_wheel
sys.path.pop(0)
import common
import indexd_runtime  # noqa: E402

VERSION = package_metadata.distribution_version()
BINARY_VERSION = package_metadata.checkout_version()
PLATFORM = "macosx_11_0_arm64"
BINARY_NAME = "agrep/_bin/agrep-rs"


def _fake_binary(minimum: tuple[int, int, int] = (11, 0, 0)) -> bytes:
    data = bytearray(100_100)
    data[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<7I", data, 4, 0x0100000C, 0, 2, 1, 24, 0, 0)
    encoded_minimum = minimum[0] << 16 | minimum[1] << 8 | minimum[2]
    struct.pack_into("<6I", data, 32, 0x32, 24, 1, encoded_minimum, 0, 0)
    marker = f"agrep-release-version:{BINARY_VERSION}".encode("ascii")
    data[512:512 + len(marker)] = marker
    return bytes(data)


def _fake_pe_binary(machine: int = 0x8664) -> bytes:
    data = bytearray(100_100)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    marker = f"agrep-release-version:{BINARY_VERSION}".encode("ascii")
    data[512:512 + len(marker)] = marker
    return bytes(data)


def _record(contents: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, data in contents.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _wheel(path: Path, *, runtime_override: bytes | None = None,
           metadata_override: bytes | None = None, zip_comment: bytes = b"",
           extra_member: str | None = None, comment_member: str | None = None,
           stale_member: str | None = None, mode_member: str | None = None,
           macho_minimum: tuple[int, int, int] = (11, 0, 0),
           platform: str = PLATFORM, binary_name: str = BINARY_NAME,
           binary: bytes | None = None, create_system: int = 3) -> None:
    dist_info = f"agrep-{VERSION}.dist-info"
    contents = {
        member: validate_wheel._runtime_source(member).read_bytes()
        for member in sorted(validate_wheel.RUNTIME_FILES)
    }
    if runtime_override is not None:
        contents["agrep/py/search.py"] = runtime_override
    contents[binary_name] = (
        _fake_binary(macho_minimum) if binary is None else binary)
    metadata_name = f"{dist_info}/METADATA"
    contents[metadata_name] = (
        metadata_override
        if metadata_override is not None
        else package_metadata.expected_core_metadata(VERSION)
    )
    contents[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: hatchling 1.31.0\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{platform}\n"
    ).encode("ascii")
    contents[f"{dist_info}/entry_points.txt"] = (
        b"[console_scripts]\nagrep = agrep.__main__:main\n")
    contents[f"{dist_info}/licenses/LICENSE"] = (ROOT / "LICENSE").read_bytes()
    contents[f"{dist_info}/licenses/THIRD_PARTY_LICENSES.txt"] = (
        ROOT / "THIRD_PARTY_LICENSES.txt").read_bytes()
    record_name = f"{dist_info}/RECORD"
    contents[record_name] = _record(contents, record_name)

    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = zip_comment
        for name, data in contents.items():
            timestamp = (
                (2021, 2, 2, 0, 0, 0)
                if name == stale_member
                else validate_wheel.CANONICAL_ZIP_TIME
            )
            info = zipfile.ZipInfo(name, timestamp)
            info.create_system = create_system
            info.create_version = 20
            info.extract_version = 20
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in validate_wheel.RUNTIME_FILES:
                mode = validate_wheel._normalized_source_mode(
                    validate_wheel._runtime_source(name))
            elif name == binary_name:
                mode = stat.S_IFREG | 0o755
            else:
                mode = 0o644
            if name == mode_member:
                mode = stat.S_IFREG | 0o755
            info.external_attr = mode << 16
            if name == extra_member:
                info.extra = b"\x99\x99\x00\x00"
            if name == comment_member:
                info.comment = b"private member comment"
            archive.writestr(info, data)


@unittest.skipIf(sys.version_info < (3, 11), "tomllib is required by release gates")
class WheelManifestTests(unittest.TestCase):
    def test_indexd_runtime_sources_are_in_the_closed_wheel_manifest(self) -> None:
        mapped = {
            f"agrep/py/{name}" for name in indexd_runtime.INDEXD_BUILD_FILES
        }
        expected = {
            name for name in validate_wheel.RUNTIME_FILES
            if name.startswith("agrep/py/")
        }
        self.assertEqual(mapped, expected)

    def test_shipped_runtime_import_closure_is_in_the_manifest(self) -> None:
        """Pin the manifest against the real import graph: every intra-package
        module a shipped source imports (lazy included) must itself ship.
        Manifest-vs-manifest checks cannot see an omitted module; this can."""
        missing: list[str] = []
        for member in sorted(validate_wheel.RUNTIME_FILES):
            if not member.endswith(".py"):
                continue
            tree = ast.parse(validate_wheel._runtime_source(member).read_text(
                encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    targets = [f"{node.module}.{alias.name}" if node.module
                               else alias.name for alias in node.names]
                else:
                    continue
                for dotted in targets:
                    parts = dotted.split(".")
                    if parts[0] == "hookless":
                        sub = parts[1] if len(parts) > 1 else ""
                        required = (f"agrep/py/hookless/{sub}.py"
                                    if (ROOT / "py" / "hookless" / f"{sub}.py").is_file()
                                    else "agrep/py/hookless/__init__.py")
                    elif (ROOT / "py" / f"{parts[0]}.py").is_file():
                        required = f"agrep/py/{parts[0]}.py"
                    else:
                        continue
                    if required not in validate_wheel.RUNTIME_FILES:
                        missing.append(f"{member}:{node.lineno} imports {dotted}"
                                       f" but {required} is not in the manifest")
        self.assertFalse(missing, "\n".join(missing))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / f"agrep-{VERSION}-py3-none-{PLATFORM}.whl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_closed_wheel(self) -> None:
        _wheel(self.path)
        self.assertEqual(
            validate_wheel.validate(self.path),
            (PLATFORM, len(validate_wheel.RUNTIME_FILES) + 7))

    def test_rejects_binary_without_release_marker(self) -> None:
        marker = f"agrep-release-version:{BINARY_VERSION}".encode("ascii")
        binary = _fake_binary().replace(marker, b"x" * len(marker))
        _wheel(self.path, binary=binary)
        with self.assertRaisesRegex(
                validate_wheel.InvalidWheel, "release version marker"):
            validate_wheel.validate(self.path)

    def test_rejects_raw_tail(self) -> None:
        _wheel(self.path)
        with self.path.open("ab") as stream:
            stream.write(b"private tail")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "trailing bytes"):
            validate_wheel.validate(self.path)

    def test_rejects_zip_comment(self) -> None:
        _wheel(self.path, zip_comment=b"/Users/private-builder/project")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "comments"):
            validate_wheel.validate(self.path)

    def test_rejects_member_extra_field(self) -> None:
        _wheel(self.path, extra_member="agrep/py/search.py")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "extra fields"):
            validate_wheel.validate(self.path)

    def test_rejects_member_comment(self) -> None:
        _wheel(self.path, comment_member="agrep/py/search.py")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "metadata"):
            validate_wheel.validate(self.path)

    def test_rejects_noncanonical_timestamp(self) -> None:
        _wheel(self.path, stale_member="agrep/py/search.py")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "timestamp"):
            validate_wheel.validate(self.path)

    def test_rejects_noncanonical_mode(self) -> None:
        _wheel(self.path, mode_member="agrep/py/search.py")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "mode"):
            validate_wheel.validate(self.path)

    def test_accepts_hatchling_windows_executable_mode(self) -> None:
        path = self.root / f"agrep-{VERSION}-py3-none-win_amd64.whl"
        _wheel(
            path,
            platform="win_amd64",
            binary_name="agrep/_bin/agrep-rs.exe",
            binary=_fake_pe_binary(),
            create_system=0,
        )
        self.assertEqual(
            validate_wheel.validate(path),
            ("win_amd64", len(validate_wheel.RUNTIME_FILES) + 7),
        )

    def test_rejects_windows_executable_mode_on_source(self) -> None:
        path = self.root / f"agrep-{VERSION}-py3-none-win_amd64.whl"
        _wheel(
            path,
            platform="win_amd64",
            binary_name="agrep/_bin/agrep-rs.exe",
            binary=_fake_pe_binary(),
            create_system=0,
            mode_member="agrep/py/search.py",
        )
        with self.assertRaisesRegex(
                validate_wheel.InvalidWheel, "Windows ZIP mode"):
            validate_wheel.validate(path)

    def test_rejects_rehashed_runtime_change(self) -> None:
        _wheel(self.path, runtime_override=b"print('not the checkout')\n")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "differs from checkout"):
            validate_wheel.validate(self.path)

    def test_rejects_rehashed_metadata_change(self) -> None:
        metadata = package_metadata.expected_core_metadata(VERSION)
        headers, body = metadata.split(b"\n\n", 1)
        poisoned = headers + b"\nAuthor-email: private@example.test\n\n" + body
        _wheel(self.path, metadata_override=poisoned)
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "invalid METADATA"):
            validate_wheel.validate(self.path)

    def test_rejects_version_divergent_from_runtime(self) -> None:
        with self.assertRaisesRegex(package_metadata.InvalidMetadata, "archive version"):
            package_metadata.expected_core_metadata(f"{VERSION}.mismatch")

    def test_rejects_macos_deployment_target_above_tag(self) -> None:
        _wheel(self.path, macho_minimum=(12, 0, 0))
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "minimum version"):
            validate_wheel.validate(self.path)

    def test_rejects_prefix(self) -> None:
        _wheel(self.path)
        self.path.write_bytes(b"prefix" + self.path.read_bytes())
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "before its first"):
            validate_wheel.validate(self.path)

    def test_rejects_symlink(self) -> None:
        target = self.root / f"agrep-{VERSION}-py3-none-{PLATFORM}.real"
        _wheel(target)
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "non-symlink"):
            validate_wheel.validate(self.path)

    def test_rejects_oversized_container_before_parsing(self) -> None:
        with self.path.open("wb") as stream:
            stream.truncate(validate_wheel.MAX_WHEEL_BYTES + 1)
        with self.assertRaisesRegex(validate_wheel.InvalidWheel, "oversized compressed"):
            validate_wheel.validate(self.path)


class StagedBinaryFreshnessTests(unittest.TestCase):
    """A local wheel never ships a `_bin/` artifact that isn't what you built.

    The release version alone does not identify a source-only rebuild, so a
    days-old staged binary can validate and ship silently. That shipped a stale
    binary to a live box twice and invalidated the measurements taken from it.
    """

    def setUp(self) -> None:
        # hatchling is a build-time dep the runtime venv has no reason to
        # carry; stub only the interface the hook subclasses.
        import types
        stubs = {}
        for name in ("hatchling", "hatchling.builders",
                     "hatchling.builders.hooks", "hatchling.builders.hooks.plugin",
                     "hatchling.builders.hooks.plugin.interface"):
            stubs[name] = sys.modules.get(name) or types.ModuleType(name)
        stubs["hatchling.builders.hooks.plugin.interface"].BuildHookInterface = object
        with mock.patch.dict(sys.modules, stubs):
            sys.path.insert(0, str(ROOT))
            try:
                import hatch_build
                self.module = hatch_build
                self.hook = hatch_build.PlatformWheelHook.__new__(
                    hatch_build.PlatformWheelHook)
            finally:
                sys.path.pop(0)
                sys.modules.pop("hatch_build", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "_bin").mkdir()
        (self.root / "target" / "release").mkdir(parents=True)
        self.staged = self.root / "_bin" / "agrep-rs"

    def _built(self, body: bytes) -> None:
        (self.root / "target" / "release" / "agrep-rs").write_bytes(body)

    def test_a_staged_binary_that_is_not_the_local_build_is_stale(self) -> None:
        self.staged.write_bytes(b"yesterday")
        self._built(b"today")
        self.assertTrue(self.hook._stale_against_local_build(
            self.staged, self.root, "agrep-rs"))

    def test_matching_bytes_are_not_stale(self) -> None:
        self.staged.write_bytes(b"same")
        self._built(b"same")
        self.assertFalse(self.hook._stale_against_local_build(
            self.staged, self.root, "agrep-rs"))

    def test_no_local_build_leaves_the_staged_artifact_alone(self) -> None:
        self.staged.write_bytes(b"ci-staged")
        self.assertFalse(self.hook._stale_against_local_build(
            self.staged, self.root, "agrep-rs"))

    def test_a_ci_distributable_build_is_never_second_guessed(self) -> None:
        self.staged.write_bytes(b"ci-staged")
        self._built(b"something-else")
        with mock.patch.dict(os.environ, {"AGREP_WHEEL_PLAT": "macosx_11_0_arm64"}):
            self.assertFalse(self.hook._stale_against_local_build(
                self.staged, self.root, "agrep-rs"))

    def test_external_cargo_target_supplies_the_staged_binary(self) -> None:
        exe = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
        target = self.root / "external-target"
        (target / "release").mkdir(parents=True)
        built = target / "release" / exe
        built.write_bytes(b"external-build")
        (self.root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        self.hook.root = str(self.root)
        for configured in (str(target), target.name):
            with self.subTest(target_dir=configured):
                staged = self.root / "_bin" / exe
                staged.unlink(missing_ok=True)
                with mock.patch.dict(
                        os.environ, {"CARGO_TARGET_DIR": configured}), \
                        mock.patch.object(
                            self.module.shutil, "which", return_value="/cargo"), \
                        mock.patch.object(self.module.subprocess, "run") as run, \
                        mock.patch.object(self.hook, "_validate_binary_version"):
                    self.hook._ensure_binary()
                run.assert_called_once()
                self.assertEqual(staged.read_bytes(), b"external-build")

    def test_external_cargo_target_participates_in_staleness(self) -> None:
        target = self.root / "external-target"
        (target / "release").mkdir(parents=True)
        (target / "release" / "agrep-rs").write_bytes(b"today")
        self.staged.write_bytes(b"yesterday")
        with mock.patch.dict(
                os.environ, {"CARGO_TARGET_DIR": str(target)}):
            self.assertTrue(self.hook._stale_against_local_build(
                self.staged, self.root, "agrep-rs"))


if __name__ == "__main__":
    unittest.main()
