"""Raw-binary platform compatibility contracts."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import dist  # noqa: E402


class LinuxRawBinaryCompatibilityTests(unittest.TestCase):
    def _asset(self, libc: str | None, machine: str = "x86_64") -> str | None:
        result = libc if libc is not None else OSError("unavailable")
        with mock.patch.object(dist.sys, "platform", "linux"), \
                mock.patch.object(dist, "WIN", False), \
                mock.patch("platform.machine", return_value=machine), \
                mock.patch.object(dist.os, "confstr", create=True, side_effect=(
                    result if isinstance(result, BaseException) else None
                ), return_value=(None if isinstance(result, BaseException) else result)):
            return dist._platform_asset()

    def test_glibc_floor_and_newer_select_release_asset(self) -> None:
        self.assertEqual(self._asset("glibc 2.28"), "agrep-rs-linux-x86_64")
        self.assertEqual(self._asset("glibc 2.39", "aarch64"),
                         "agrep-rs-linux-aarch64")

    def test_old_unknown_and_non_glibc_hosts_refuse_asset(self) -> None:
        self.assertIsNone(self._asset("glibc 2.27"))
        self.assertIsNone(self._asset(None))
        self.assertIsNone(self._asset("musl 1.2.5"))

    def test_refusal_precedes_network_and_names_source_build_remedy(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(dist.sys, "platform", "linux"), \
                mock.patch.object(dist, "WIN", False), \
                mock.patch("platform.machine", return_value="x86_64"), \
                mock.patch.object(
                    dist.os, "confstr", create=True, return_value="glibc 2.17"), \
                mock.patch.object(dist, "_download_binary") as download, \
                contextlib.redirect_stderr(stderr):
            self.assertIsNone(dist.fetch_binary(assume_yes=True))
        download.assert_not_called()
        rendered = stderr.getvalue()
        self.assertIn("require glibc 2.28 or newer", rendered)
        self.assertIn("detected glibc 2.17", rendered)
        self.assertIn("cargo build --release", rendered)
        self.assertIn("AGREP_RS_BIN", rendered)

    def test_unknown_arch_uses_the_generic_source_build_remedy(self) -> None:
        with mock.patch.object(dist.sys, "platform", "linux"), \
                mock.patch.object(dist, "WIN", False), \
                mock.patch("platform.machine", return_value="riscv64"), \
                mock.patch.object(dist.os, "confstr", create=True) as confstr:
            self.assertIsNone(dist._platform_asset())
            rendered = dist._unsupported_asset_message()
        confstr.assert_not_called()
        self.assertIn("no prebuilt agrep-rs is published", rendered)
        self.assertNotIn("glibc 2.28", rendered)


class SupportedRawBinaryPlatformTests(unittest.TestCase):
    def test_stable_reader_uses_platform_file_identities(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "member.bin"
            path.write_bytes(b"candidate-bytes")
            chunks = []
            with mock.patch.object(
                    dist.fileops, "file_identity",
                    wraps=dist.fileops.file_identity) as path_identity, \
                    mock.patch.object(
                        dist.fileops, "file_identity_fd",
                        wraps=dist.fileops.file_identity_fd) as fd_identity:
                dist._consume_stable_regular(
                    path, max_bytes=1024, consume=chunks.append)
        self.assertEqual(b"".join(chunks), b"candidate-bytes")
        self.assertEqual(path_identity.call_count, 2)
        self.assertGreaterEqual(fd_identity.call_count, 2)

    def test_macos_selection_does_not_probe_libc(self) -> None:
        with mock.patch.object(dist.sys, "platform", "darwin"), \
                mock.patch.object(dist, "WIN", False), \
                mock.patch("platform.machine", return_value="arm64"), \
                mock.patch.object(dist.os, "confstr", create=True) as confstr:
            self.assertEqual(dist._platform_asset(), "agrep-rs-macos-aarch64")
        confstr.assert_not_called()

    def test_windows_selection_does_not_probe_libc(self) -> None:
        with mock.patch.object(dist.sys, "platform", "win32"), \
                mock.patch.object(dist, "WIN", True), \
                mock.patch("platform.machine", return_value="AMD64"), \
                mock.patch.object(dist.os, "confstr", create=True) as confstr:
            self.assertEqual(dist._platform_asset(), "agrep-rs-windows-x86_64.exe")
        confstr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
