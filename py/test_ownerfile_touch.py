from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import ownerfile  # noqa: E402


class OwnerFileTouchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.handles: list[ownerfile.Handle] = []

    def tearDown(self) -> None:
        for handle in self.handles:
            handle.close()
        self.temp.cleanup()

    def _owner(self, name: str = "owner.lock",
               raw: bytes = b"owner") -> ownerfile.Handle:
        handle = ownerfile.create_exclusive(
            self.root / name, raw, retain_fd=True)
        self.handles.append(handle)
        return handle

    def test_non_windows_touch_uses_retained_descriptor(self) -> None:
        fake_utime = mock.Mock()
        with (
                mock.patch.object(ownerfile, "_WINDOWS", False),
                mock.patch.object(ownerfile.os, "utime", fake_utime),
                mock.patch.object(ownerfile.os, "supports_fd", {fake_utime})):
            ownerfile._touch_retained_fd(17)
        fake_utime.assert_called_once_with(17, None)

    def test_touch_refreshes_mtime_without_changing_bytes(self) -> None:
        if ownerfile._WINDOWS:
            self.skipTest("POSIX descriptor-utime integration test")
        handle = self._owner(raw=b"unchanged")
        old_ns = 1_000_000_000
        os.utime(handle.path, ns=(old_ns, old_ns))
        handle.snapshot = ownerfile.snapshot(handle.path)
        observed = handle.touch()
        self.assertEqual(observed.raw, b"unchanged")
        self.assertGreater(observed.identity[3], old_ns)
        self.assertEqual(handle.snapshot, observed)

    @unittest.skipUnless(ownerfile._WINDOWS, "Windows SetFileTime integration")
    def test_windows_touch_refreshes_a_real_retained_handle(self) -> None:
        handle = self._owner(raw=b"unchanged")
        old_ns = 946_684_800_000_000_000
        os.utime(handle.path, ns=(old_ns, old_ns))
        handle.snapshot = ownerfile.snapshot(handle.path)
        observed = handle.touch()
        self.assertEqual(observed.raw, b"unchanged")
        self.assertGreater(observed.identity[3], old_ns)
        self.assertEqual(handle.snapshot, observed)

    def test_windows_touch_uses_os_handle_and_setfiletime(self) -> None:
        get_osfhandle = mock.Mock(return_value=0x1234)
        fake_msvcrt = types.SimpleNamespace(get_osfhandle=get_osfhandle)
        set_file_time = mock.Mock(return_value=1)
        kernel32 = types.SimpleNamespace(SetFileTime=set_file_time)
        now_ns = 1_725_000_000_123_456_700
        with (
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                mock.patch.object(
                    ctypes, "WinDLL", create=True, return_value=kernel32
                ) as win_dll,
                mock.patch.object(ownerfile.time, "time_ns", return_value=now_ns)):
            ownerfile._touch_windows_fd(7)

        get_osfhandle.assert_called_once_with(7)
        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        set_file_time.assert_called_once()
        args = set_file_time.call_args.args
        self.assertEqual(args[0].value, 0x1234)
        expected = now_ns // 100 + 116_444_736_000_000_000
        self.assertEqual(args[3]._obj.low, expected & 0xFFFFFFFF)
        self.assertEqual(args[3]._obj.high, expected >> 32)

    def test_windows_dispatches_to_descriptor_helper(self) -> None:
        with (
                mock.patch.object(ownerfile, "_WINDOWS", True),
                mock.patch.object(ownerfile, "_touch_windows_fd") as touch):
            ownerfile._touch_retained_fd(23)
        touch.assert_called_once_with(23)

    def test_unsupported_platform_fails_without_path_utime(self) -> None:
        fake_utime = mock.Mock()
        with (
                mock.patch.object(ownerfile, "_WINDOWS", False),
                mock.patch.object(ownerfile.os, "utime", fake_utime),
                mock.patch.object(ownerfile.os, "supports_fd", set()),
                self.assertRaises(OSError) as raised):
            ownerfile._touch_retained_fd(17)
        self.assertIn(
            raised.exception.errno,
            {getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.EOPNOTSUPP})
        fake_utime.assert_not_called()

    def test_same_byte_replacement_race_is_rejected_without_touching_path(
            self) -> None:
        if ownerfile._WINDOWS:
            self.skipTest("Windows sharing rules can prevent path replacement")
        raw = b"same-owner-body"
        handle = self._owner(raw=raw)
        original = handle.snapshot
        replacement = self.root / "replacement"
        replacement.write_bytes(raw)
        os.utime(
            replacement,
            ns=(original.identity[3], original.identity[3]))
        replacement_snapshot = ownerfile.snapshot(replacement)
        self.assertNotEqual(
            replacement_snapshot.identity[:2], original.identity[:2])
        real_touch = ownerfile._touch_retained_fd

        def replace_then_touch(fd: int) -> None:
            os.replace(replacement, handle.path)
            real_touch(fd)

        with (
                mock.patch.object(
                    ownerfile, "_touch_retained_fd",
                    side_effect=replace_then_touch),
                self.assertRaises(ownerfile.OwnershipLost)):
            handle.touch()

        observed = ownerfile.snapshot(handle.path)
        self.assertEqual(observed.identity[:3], replacement_snapshot.identity[:3])
        self.assertEqual(observed.identity[3], replacement_snapshot.identity[3])
        self.assertEqual(observed.raw, raw)
        self.assertFalse(
            handle.release(tombstone=True, require_stable_mtime=True))
        self.assertEqual(ownerfile.snapshot(handle.path), observed)


if __name__ == "__main__":
    unittest.main()
