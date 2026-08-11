from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_release_assets_test", ROOT / "bench" / "release_assets.py")
release_assets = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.path.insert(0, str(ROOT / "bench"))
SPEC.loader.exec_module(release_assets)
sys.path.pop(0)


def _binary(kind: str, architecture: int,
            minimum: tuple[int, int, int] = (11, 0, 0)) -> bytes:
    data = bytearray(100_100)
    if kind == "pe":
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", data, 0x84, architecture)
    elif kind == "elf":
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, architecture)
    else:
        data[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<7I", data, 4, architecture, 0, 2, 1, 24, 0, 0)
        encoded = minimum[0] << 16 | minimum[1] << 8 | minimum[2]
        struct.pack_into("<6I", data, 32, 0x32, 24, 1, encoded, 0, 0)
    marker = (
        f"agrep-release-version:{release_assets.checkout_version()}".encode("ascii"))
    data[100:100 + len(marker)] = marker
    return bytes(data)


class ReleaseAssetsTests(unittest.TestCase):
    def _binaries(self, directory: Path) -> None:
        directory.mkdir()
        for name, (kind, architecture, _, _) in (
                release_assets.BINARY_TARGETS.items()):
            (directory / name).write_bytes(_binary(kind, architecture))
        for name in release_assets.NOTICE_NAMES:
            (directory / name).write_bytes((ROOT / name).read_bytes())

    def _wheels(self, directory: Path, assets: Path) -> None:
        directory.mkdir()
        version = release_assets.checkout_version()
        for asset_name, (_, _, platform, member) in (
                release_assets.BINARY_TARGETS.items()):
            wheel = directory / f"agrep-{version}-py3-none-{platform}.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(member, (assets / asset_name).read_bytes())

    def _prepared(self, root: Path) -> tuple[Path, Path]:
        assets = root / "assets"
        manifest = root / "manifest.json"
        self._binaries(assets)
        release_assets.prepare(assets, manifest)
        return assets, manifest

    def test_prepare_writes_exact_canonical_bundle_and_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            assets, manifest = self._prepared(Path(temporary))
            expected = set(release_assets.ASSET_NAMES)
            self.assertEqual({path.name for path in assets.iterdir()}, expected)
            payload = json.loads(manifest.read_text(encoding="ascii"))
            self.assertEqual(set(payload["files"]), expected)
            self.assertEqual(
                manifest.read_text(encoding="ascii"),
                release_assets._canonical_json(payload))
            for name in release_assets.BINARY_NAMES:
                digest = release_assets._sha256(assets / name)
                self.assertEqual(
                    (assets / f"{name}.sha256").read_text(encoding="ascii"),
                    f"{digest}  {name}\n")
            release_assets.verify(assets, manifest)

    def test_prepare_rejects_missing_extra_empty_and_nonregular_entries(self):
        cases = ("missing", "missing-notice", "extra", "empty", "directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                assets = root / "assets"
                self._binaries(assets)
                if case == "missing":
                    (assets / release_assets.BINARY_NAMES[0]).unlink()
                elif case == "missing-notice":
                    (assets / release_assets.NOTICE_NAMES[0]).unlink()
                elif case == "extra":
                    (assets / "unexpected").write_bytes(b"x")
                elif case == "empty":
                    (assets / release_assets.BINARY_NAMES[0]).write_bytes(b"")
                else:
                    (assets / "unexpected").mkdir()
                with self.assertRaises(RuntimeError):
                    release_assets.prepare(assets, root / "manifest.json")

    def test_prepare_and_verify_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            self._binaries(assets)
            target = assets / release_assets.BINARY_NAMES[0]
            original = target.read_bytes()
            target.unlink()
            try:
                target.symlink_to(assets / release_assets.BINARY_NAMES[1])
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                release_assets.prepare(assets, root / "manifest.json")

            target.unlink()
            target.write_bytes(original)
            release_assets.prepare(assets, root / "manifest.json")
            sidecar = assets / release_assets.SIDECAR_NAMES[0]
            sidecar.unlink()
            sidecar.symlink_to(assets / release_assets.SIDECAR_NAMES[1])
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                release_assets.verify(assets, root / "manifest.json")

    def test_prepare_rejects_binary_moving_between_hash_and_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            manifest = root / "manifest.json"
            self._binaries(assets)
            actual_sha256 = release_assets._sha256
            observed = 0

            def moving_sha256(path):
                nonlocal observed
                if path.name == release_assets.BINARY_NAMES[0]:
                    observed += 1
                    if observed == 2:
                        return "0" * 64
                return actual_sha256(path)

            with mock.patch.object(
                    release_assets, "_sha256", side_effect=moving_sha256):
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    release_assets.prepare(assets, manifest)
            self.assertEqual(observed, 2)
            self.assertFalse(manifest.exists())

    def test_prepare_rejects_broken_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            self._binaries(assets)
            manifest = root / "manifest.json"
            try:
                manifest.symlink_to(root / "missing.json")
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                release_assets.prepare(assets, manifest)

    def test_prepare_removes_stale_manifest_before_validating_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            manifest = root / "manifest.json"
            self._binaries(assets)
            manifest.write_text("stale", encoding="ascii")
            (assets / release_assets.BINARY_NAMES[0]).unlink()
            with self.assertRaisesRegex(RuntimeError, "missing"):
                release_assets.prepare(assets, manifest)
            self.assertFalse(manifest.exists())

    def test_verify_rejects_file_set_hash_and_sidecar_drift(self):
        cases = ("missing", "extra", "binary", "sidecar", "notice")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                assets, manifest = self._prepared(Path(temporary))
                if case == "missing":
                    (assets / release_assets.SIDECAR_NAMES[0]).unlink()
                elif case == "extra":
                    (assets / "unexpected").write_bytes(b"x")
                elif case == "binary":
                    (assets / release_assets.BINARY_NAMES[0]).write_bytes(b"changed")
                elif case == "sidecar":
                    (assets / release_assets.SIDECAR_NAMES[0]).write_text(
                        "0" * 64 + "\n", encoding="ascii")
                else:
                    (assets / release_assets.NOTICE_NAMES[0]).write_bytes(b"changed")
                with self.assertRaises(RuntimeError):
                    release_assets.verify(assets, manifest)

    def test_prepare_rejects_notice_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            self._binaries(assets)
            (assets / release_assets.NOTICE_NAMES[0]).write_bytes(b"not canonical")
            with self.assertRaisesRegex(RuntimeError, "differs from canonical"):
                release_assets.prepare(assets, root / "manifest.json")

    def test_prepare_rejects_wrong_binary_identity(self):
        marker = (
            f"agrep-release-version:{release_assets.checkout_version()}".encode("ascii"))
        cases = {
            "architecture": (
                "agrep-rs-windows-x86_64.exe",
                lambda data: data[:0x84] + b"\x64\xaa" + data[0x86:],
                "architecture",
            ),
            "version": (
                "agrep-rs-linux-x86_64",
                lambda data: data.replace(marker, b"X" * len(marker)),
                "version marker",
            ),
            "privacy": (
                "agrep-rs-linux-aarch64",
                lambda data: data[:200] + b"/Users/private-builder/" + data[223:],
                "privacy",
            ),
            "macos": (
                "agrep-rs-macos-aarch64",
                lambda data: data[:44] + struct.pack("<I", 12 << 16) + data[48:],
                "minimum version",
            ),
        }
        for case, (name, mutate, message) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                assets = root / "assets"
                self._binaries(assets)
                target = assets / name
                target.write_bytes(mutate(target.read_bytes()))
                with self.assertRaisesRegex(RuntimeError, message):
                    release_assets.prepare(assets, root / "manifest.json")

    def test_raw_binaries_must_equal_their_wheel_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            wheels = root / "wheels"
            self._binaries(assets)
            self._wheels(wheels, assets)

            def accept_wheel(path):
                for _, (_, _, platform, _) in release_assets.BINARY_TARGETS.items():
                    if platform in path.name:
                        return platform, 1
                raise AssertionError(path)

            with mock.patch.object(
                    release_assets.wheel_validator, "validate",
                    side_effect=accept_wheel):
                release_assets.validate_wheel_payloads(assets, wheels)
                asset_name = "agrep-rs-linux-x86_64"
                _, _, platform, member = release_assets.BINARY_TARGETS[asset_name]
                wheel = wheels / (
                    f"agrep-{release_assets.checkout_version()}-py3-none-{platform}.whl")
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr(member, b"different")
                with self.assertRaisesRegex(RuntimeError, "differs from wheel payload"):
                    release_assets.validate_wheel_payloads(assets, wheels)

    def test_manifest_requires_exact_canonical_schema_and_hashes(self):
        cases = {
            "noncanonical": lambda payload: json.dumps(payload, indent=2) + "\n",
            "duplicate": lambda payload: release_assets._canonical_json(
                payload).replace('"schema":1', '"schema":1,"schema":1'),
            "schema": lambda payload: release_assets._canonical_json(
                {**payload, "schema": 2}),
            "files": lambda payload: release_assets._canonical_json(
                {**payload, "files": {}}),
            "hash": lambda payload: release_assets._canonical_json({
                **payload,
                "files": {
                    **payload["files"],
                    release_assets.BINARY_NAMES[0]: "A" * 64,
                },
            }),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                assets, manifest = self._prepared(Path(temporary))
                payload = json.loads(manifest.read_text(encoding="ascii"))
                manifest.write_text(mutate(payload), encoding="ascii")
                with self.assertRaises(RuntimeError):
                    release_assets.verify(assets, manifest)

    def test_verify_rejects_symlink_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets, manifest = self._prepared(root)
            target = root / "manifest-target.json"
            manifest.replace(target)
            try:
                manifest.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                release_assets.verify(assets, manifest)


if __name__ == "__main__":
    unittest.main()
