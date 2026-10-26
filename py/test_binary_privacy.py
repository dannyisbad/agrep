from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agrep_binary_privacy_test",
    ROOT / "bench" / "validate_binary_privacy.py")
binary_privacy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(binary_privacy)
sys.path.insert(0, str(ROOT / "bench"))
import package_metadata
import validate_wheel
sys.path.pop(0)


class BinaryPrivacyTests(unittest.TestCase):
    def test_accepts_remapped_and_relative_source_paths(self):
        binary_privacy.validate_bytes(
            b"\x7fELF/src/agrep/src/main.rs\0build/cargo/serde/src/lib.rs")

    def test_rejects_platform_home_paths_without_exposing_them(self):
        cases = (
            b"/Users/private-name/.cargo/registry/src/lib.rs",
            b"/home/private-name/project/src/main.rs",
            b"/root/.cargo/registry/src/lib.rs",
            rb"C:\Users\private-name\.cargo\registry\src\lib.rs",
            b"C:/Users/private-name/project/src/main.rs",
        )
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(binary_privacy.InvalidBinary) as raised:
                    binary_privacy.validate_bytes(data)
                self.assertNotIn("private-name", str(raised.exception))

    def test_exact_forbidden_path_covers_both_separator_styles(self):
        forbidden = ("/private/build/work",)
        for data in (
                b"/private/build/work/crates/main.rs",
                rb"\private\build\work\crates\main.rs"):
            with self.subTest(data=data), self.assertRaises(
                    binary_privacy.InvalidBinary):
                binary_privacy.validate_bytes(data, forbidden)

    def test_file_validation_rejects_empty_and_symlink_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "empty"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(binary_privacy.InvalidBinary, "empty"):
                binary_privacy.validate(empty)

            target = root / "target"
            target.write_bytes(b"safe")
            link = root / "link"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(
                    binary_privacy.InvalidBinary, "regular file"):
                binary_privacy.validate(link)

    def test_wheel_validator_applies_the_same_byte_gate(self):
        binary = bytearray(100_100)
        binary[:2] = b"MZ"
        struct.pack_into("<I", binary, 0x3C, 128)
        binary[128:132] = b"PE\0\0"
        struct.pack_into("<H", binary, 132, 0x8664)
        version = package_metadata.checkout_version()
        marker = f"Cli{version}".encode("ascii")
        binary[256:256 + len(marker)] = marker
        binary[-40:] = b"/Users/private-name/project/source-path"
        with self.assertRaisesRegex(
                validate_wheel.InvalidWheel, "privacy check"):
            validate_wheel.validate_native_binary(
                bytes(binary), kind="pe", architecture=0x8664,
                version=version)


if __name__ == "__main__":
    unittest.main()
