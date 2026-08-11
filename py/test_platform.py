"""Windows-platform regressions exercised from posix with native API doubles."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import common  # noqa: E402
import console  # noqa: E402
import embed  # noqa: E402
import embedder  # noqa: E402
import resources  # noqa: E402
import search  # noqa: E402


class _FakePowerAPI:
    """Stands in for ctypes.windll on posix; fills the byref'd struct."""

    def __init__(self, ret=1, ac_line=1, percent=255):
        self.kernel32 = self
        self._ret, self._ac, self._pct = ret, ac_line, percent

    def GetSystemPowerStatus(self, ref):
        ref._obj.ACLineStatus = self._ac
        ref._obj.BatteryLifePercent = self._pct
        return self._ret


class _FakeSystemTimesAPI:
    def __init__(self, samples):
        self.kernel32 = self
        self._samples = iter(samples)

    @staticmethod
    def _set(ref, value):
        ref._obj.low = value & 0xFFFFFFFF
        ref._obj.high = value >> 32

    def GetSystemTimes(self, idle, kernel, user):
        sample = next(self._samples)
        if sample is None:
            return 0
        for ref, value in zip((idle, kernel, user), sample):
            self._set(ref, value)
        return 1


class WindowsBatteryProbeTests(unittest.TestCase):
    def _probe(self, **kw):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(ctypes, "windll", _FakePowerAPI(**kw), create=True):
            return embed._probe_battery()

    def test_on_battery_reports_percent(self):
        self.assertEqual(self._probe(ac_line=0, percent=10), (True, 10))

    def test_on_ac(self):
        self.assertEqual(self._probe(ac_line=1, percent=88), (False, 88))

    def test_unknown_ac_line_reads_ac(self):
        self.assertEqual(self._probe(ac_line=255, percent=50), (False, 50))

    def test_unknown_percent_is_none(self):
        self.assertEqual(self._probe(ac_line=0, percent=255), (True, None))

    def test_call_failure_reads_ac(self):
        self.assertEqual(self._probe(ret=0, ac_line=0, percent=10), (False, None))

    def test_missing_power_api_reads_ac(self):
        # object() has no kernel32, the same AttributeError path a bare posix
        # interpreter hits at ctypes.windll
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(ctypes, "windll", object(), create=True):
            self.assertEqual(embed._probe_battery(), (False, None))


class NonWindowsBatteryProbeTests(unittest.TestCase):
    def _probe_darwin(self, pmset_out):
        done = subprocess.CompletedProcess([], 0, stdout=pmset_out, stderr="")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(subprocess, "run", return_value=done):
            return embed._probe_battery()

    def test_darwin_battery_discharging(self):
        out = ("Now drawing from 'Battery Power'\n"
               " -InternalBattery-0 (id=123)\t23%; discharging; 4:56 remaining\n")
        self.assertEqual(self._probe_darwin(out), (True, 23))

    def test_darwin_ac_power(self):
        out = ("Now drawing from 'AC Power'\n"
               " -InternalBattery-0 (id=123)\t100%; charged; 0:00 remaining\n")
        self.assertEqual(self._probe_darwin(out), (False, 100))

    def test_linux_reads_ac(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(embed._probe_battery(), (False, None))


@unittest.skipUnless(sys.platform == "darwin", "Darwin native memory API")
class DarwinMemoryProbeTests(unittest.TestCase):
    def test_memory_headroom_needs_no_child_process(self):
        with mock.patch.object(
                common.subprocess, "check_output",
                side_effect=AssertionError("spawned a memory probe")):
            fraction = common.available_memory_fraction()
        self.assertIsNotNone(fraction)
        self.assertGreaterEqual(fraction, 0.0)
        self.assertLessEqual(fraction, 1.0)


class WindowsCpuProbeTests(unittest.TestCase):
    def _probe(self, samples):
        api = _FakeSystemTimesAPI(samples)
        with mock.patch.object(resources, "WIN", True), \
             mock.patch.object(ctypes, "windll", api, create=True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGREP_HOST_CPU_FRACTION", None)
            resources._WINDOWS_CPU_SAMPLE = None
            return [common.host_cpu_fraction() for _ in samples]

    def test_first_sample_is_unknown_then_reports_busy_fraction(self):
        values = self._probe([(100, 300, 200), (120, 360, 240)])
        self.assertIsNone(values[0])
        self.assertAlmostEqual(values[1], 0.8)

    def test_native_failure_does_not_poison_next_pair(self):
        values = self._probe([
            None, (1 << 35, 1 << 36, 1 << 34),
            ((1 << 35) + 50, (1 << 36) + 100, (1 << 34) + 100),
        ])
        self.assertEqual(values[:2], [None, None])
        self.assertAlmostEqual(values[2], 0.75)

    def test_regressed_or_stationary_counters_are_unknown(self):
        regressed = self._probe([(100, 300, 200), (90, 350, 240)])
        stationary = self._probe([(100, 300, 200), (100, 300, 200)])
        self.assertIsNone(regressed[1])
        self.assertIsNone(stationary[1])

    def test_impossible_idle_delta_clamps_to_zero_busy(self):
        values = self._probe([(100, 300, 200), (300, 350, 250)])
        self.assertEqual(values[1], 0.0)

    def test_environment_override_skips_native_sampling(self):
        api = mock.Mock()
        with mock.patch.object(resources, "WIN", True), \
             mock.patch.object(ctypes, "windll", api, create=True), \
             mock.patch.dict(os.environ, {"AGREP_HOST_CPU_FRACTION": "1.25"}):
            self.assertEqual(common.host_cpu_fraction(), 1.25)
        api.assert_not_called()


class EmbedderThreadBudgetTests(unittest.TestCase):
    def _budget(self, platform: str, cpus: int = 24) -> int:
        with mock.patch.object(embedder.sys, "platform", platform), \
                mock.patch.object(embedder.os, "cpu_count", return_value=cpus), \
                mock.patch.dict(embedder.os.environ, {}, clear=False):
            embedder.os.environ.pop("AGREP_SEM_THREADS", None)
            return embedder._thread_budget()

    def test_platform_defaults_preserve_measured_cpu_knees(self):
        self.assertEqual(self._budget("win32"), 5)
        self.assertEqual(self._budget("darwin"), 6)
        self.assertEqual(self._budget("linux"), 8)

    def test_small_hosts_use_half_their_logical_cpus(self):
        self.assertEqual(self._budget("win32", cpus=4), 2)

    def test_explicit_override_remains_user_controlled(self):
        with mock.patch.dict(embedder.os.environ, {"AGREP_SEM_THREADS": "12"}):
            self.assertEqual(embedder._thread_budget(), 12)


class GovernorWindowsTests(unittest.TestCase):
    def _deferral(self, anyway=None, memory=0.5, load=0.0,
                  ignore_battery=False, **probe_kw):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "platform", "win32"))
            stack.enter_context(mock.patch.object(
                ctypes, "windll", _FakePowerAPI(**probe_kw), create=True))
            stack.enter_context(mock.patch.object(embed, "_BATTERY_PROBE", []))
            stack.enter_context(mock.patch.object(
                embed, "_normalized_load", lambda: load))
            stack.enter_context(mock.patch.object(
                common, "available_memory_fraction", return_value=memory))
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("AGREP_EMBED_ANYWAY", None)
            if anyway is not None:
                os.environ["AGREP_EMBED_ANYWAY"] = anyway
            return embed._governor_deferral(ignore_battery=ignore_battery)

    def test_low_battery_defers(self):
        self.assertEqual(self._deferral(ac_line=0, percent=10), "on battery (10%)")

    def test_floor_is_exclusive(self):
        self.assertIsNone(self._deferral(ac_line=0, percent=embed.GOVERNOR_BATTERY_FLOOR_PCT))

    def test_healthy_battery_proceeds(self):
        self.assertIsNone(self._deferral(ac_line=0, percent=80))

    def test_ac_proceeds_at_any_percent(self):
        self.assertIsNone(self._deferral(ac_line=1, percent=10))

    def test_embed_anyway_bypasses(self):
        self.assertIsNone(self._deferral(anyway="1", ac_line=0, percent=5))

    def test_memory_pressure_defers_before_background_work(self):
        self.assertEqual(self._deferral(ac_line=1, percent=100, memory=0.1),
                         "memory pressure (10% available)")

    def test_stale_bootstrap_override_ignores_only_battery(self):
        self.assertIsNone(self._deferral(
            ac_line=0, percent=10, ignore_battery=True))
        self.assertEqual(self._deferral(
            ac_line=0, percent=10, memory=0.1, ignore_battery=True),
            "memory pressure (10% available)")
        self.assertEqual(self._deferral(
            ac_line=0, percent=10, load=2.0, ignore_battery=True),
            "load 2.0/core")

    def test_normalized_load_uses_cross_platform_probe(self):
        with mock.patch.object(
                common, "host_cpu_fraction", side_effect=(0.25, None)) as probe:
            self.assertEqual(embed._normalized_load(), 0.25)
            self.assertEqual(embed._normalized_load(), 0.0)
        self.assertEqual(probe.call_count, 2)


class CliBrokenPipeBoundaryTests(unittest.TestCase):
    def _boundary(self, exc, win):
        with mock.patch.object(cli, "_main", side_effect=exc), \
             mock.patch.object(cli, "WIN", win), \
             mock.patch("signal.signal"), \
             mock.patch.object(os, "dup2", lambda *_: None):
            return cli.main()

    def test_brokenpipe_returns_141_posix(self):
        self.assertEqual(self._boundary(BrokenPipeError(), win=False), 141)

    def test_brokenpipe_returns_141_windows(self):
        self.assertEqual(self._boundary(BrokenPipeError(), win=True), 141)

    def test_windows_einval_pipe_returns_141(self):
        self.assertEqual(self._boundary(OSError(errno.EINVAL, "reader gone"), win=True), 141)

    def test_windows_epipe_oserror_returns_141(self):
        exc = OSError()
        exc.errno = errno.EPIPE
        self.assertEqual(self._boundary(exc, win=True), 141)

    def test_posix_einval_is_a_stable_cli_error(self):
        self.assertEqual(
            self._boundary(
                OSError(errno.EINVAL, "invalid argument"), win=False),
            2)

    def test_windows_unrelated_oserror_is_a_stable_cli_error(self):
        self.assertEqual(
            self._boundary(OSError(errno.ENOENT, "missing"), win=True),
            2)

    def test_windows_bare_oserror_is_a_stable_cli_error(self):
        self.assertEqual(self._boundary(OSError("no errno"), win=True), 2)


class CliPipeSubprocessTest(unittest.TestCase):
    def test_closed_pipe_exit_is_clean(self):
        proc = subprocess.Popen([sys.executable, str(ROOT / "cli.py"), "--help"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdout.close()
            err = proc.stderr.read()
            proc.wait(timeout=30)
        finally:
            proc.stderr.close()
            if proc.poll() is None:
                proc.kill()
        self.assertNotIn(b"Traceback", err)
        # 0: writes beat the close; 141: handler; -SIGPIPE: posix default disposition
        self.assertIn(proc.returncode, (0, 141, -13))


class WindowsChildProcessTests(unittest.TestCase):
    def test_streamed_ingest_does_not_open_a_console(self):
        with mock.patch.object(search.common, "WIN", True), \
                mock.patch.object(search.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                                  create=True), \
                mock.patch.object(search.subprocess, "Popen",
                                  side_effect=OSError) as popen:
            self.assertIsNone(search._stream_first_run(
                "needle", "keyword", object(), False, None, None))
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)

    def test_auto_color_requires_vt_but_explicit_color_does_not(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch.object(console, "enable_vt", return_value=False):
            self.assertFalse(common.color_enabled(stream))
            self.assertTrue(common.color_enabled(stream, "always"))
        with mock.patch.object(console, "enable_vt", return_value=True):
            self.assertTrue(common.color_enabled(stream))

    def test_debug_trace_stays_plain_when_vt_cannot_be_enabled(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with mock.patch.object(console, "DEBUG", True), \
                mock.patch.object(console, "enable_vt", return_value=False), \
                mock.patch.object(sys, "stderr", stream):
            common.dbg("plain trace")
        self.assertIn("plain trace", stream.getvalue())
        self.assertNotIn("\033", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
