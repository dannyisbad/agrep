from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def _load_resources():
    path = Path(__file__).resolve().parents[1] / "bench" / "resources.py"
    spec = importlib.util.spec_from_file_location("agrep_resource_harness_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _support_root(env: dict[str, str]) -> tuple[Path, bool]:
    code = (
        "import _test_support as s;"
        "print(s._DATA_ROOT);"
        "print(s._DATA is None)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent,
        env=env, capture_output=True, text=True, check=True)
    lines = completed.stdout.splitlines()
    return Path(lines[0]), lines[1] == "True"


class TestSupportIsolationTests(unittest.TestCase):
    def test_inherited_test_root_requires_the_complete_sandbox_tuple(self):
        with tempfile.TemporaryDirectory() as raw:
            forged = Path(raw)
            env = dict(os.environ)
            env.update({
                "AGREP_TEST_DATA_ROOT": str(forged),
                "AGREP_DATA_DIR": str(forged / "data"),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(forged / "home"),
            })
            observed, inherited = _support_root(env)
        self.assertNotEqual(observed, forged)
        self.assertFalse(inherited)

    def test_spawned_test_process_reuses_a_marked_temporary_root(self):
        with tempfile.TemporaryDirectory(
                prefix="agrep-unittest-data-proof-") as raw:
            root = Path(raw)
            env = dict(os.environ)
            env.update({
                "AGREP_TEST_DATA_ROOT": str(root),
                "AGREP_DATA_DIR": str(root / "data"),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(root / "home"),
            })
            observed, inherited = _support_root(env)
            self.assertEqual(observed, root)
            self.assertTrue(inherited)


class ResourceHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = _load_resources()

    def test_idle_fixture_sources_are_aged_before_ingest(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(self.resources, "FILES", 2), \
                mock.patch.object(self.resources, "ROWS", 7):
            root = Path(td)
            self.assertEqual(self.resources._write_store(root), 7)
            mtimes = [path.stat().st_mtime for path in root.iterdir()]
        self.assertEqual(len(mtimes), 2)
        self.assertTrue(all(
            time.time() - mtime >= self.resources._IDLE_SOURCE_AGE_S - 2
            for mtime in mtimes))

    def test_idle_fixture_disables_background_semantic_work(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            self.resources._write_fixture_settings(data)
            settings = (data / "settings.json").read_text(encoding="utf-8")
        self.assertEqual(settings, '{"embeddings":"off"}\n')

    def test_windows_console_host_is_not_idle_work(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(self.resources, "_tree_pids", return_value=[10, 11]), \
                mock.patch.object(self.resources, "_windows_process_snapshot",
                                  return_value={10: (1, "python.exe"),
                                                11: (10, "ConHost.exe")}):
            self.assertTrue(self.resources._idle_tree_child_free(10))

    def test_windows_unknown_or_product_child_blocks_idle(self):
        cases = (
            ({10: (1, "python.exe")}, [10, 11]),
            ({10: (1, "python.exe"), 11: (10, "agrep-rs.exe")}, [10, 11]),
            ({10: (1, "python.exe"), 11: (10, "conhost.exe"),
              12: (10, "python.exe")}, [10, 11, 12]),
        )
        for snapshot, tree in cases:
            with self.subTest(snapshot=snapshot), \
                    mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(self.resources, "_tree_pids", return_value=tree), \
                    mock.patch.object(self.resources, "_windows_process_snapshot",
                                      return_value=snapshot):
                self.assertFalse(self.resources._idle_tree_child_free(10))

    def test_non_windows_requires_an_empty_descendant_tree(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.resources, "_tree_pids", return_value=[10]):
            self.assertTrue(self.resources._idle_tree_child_free(10))
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.resources, "_tree_pids", return_value=[10, 11]):
            self.assertFalse(self.resources._idle_tree_child_free(10))

    def test_console_host_remains_in_resource_measurement(self):
        mib = 1024 * 1024
        with mock.patch.object(self.resources, "_tree_pids", return_value=[10, 11]), \
                mock.patch.object(self.resources, "_cpu_seconds", return_value=0.0), \
                mock.patch.object(self.resources, "_rss_bytes",
                                  side_effect=[100 * mib, 20 * mib]), \
                mock.patch.object(self.resources, "_handle_count", side_effect=[5, 2]):
            measured = self.resources._TreeAccumulator(10).metrics()
        self.assertEqual(measured["processes"], 2)
        self.assertEqual(measured["rss_mib"], 120.0)
        self.assertEqual(measured["handles"], 7)

    def test_exited_child_still_invalidates_the_sample_window(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.resources, "_tree_pids",
                                  side_effect=[[10, 11], [10]]), \
                mock.patch.object(self.resources, "_cpu_seconds", return_value=0.0), \
                mock.patch.object(self.resources, "_rss_bytes", return_value=1), \
                mock.patch.object(self.resources, "_handle_count", return_value=1):
            measured = self.resources._TreeAccumulator(10)
            measured.observe()
        self.assertTrue(measured.metrics()["child_work_seen"])

    def test_windows_console_host_does_not_invalidate_the_sample_window(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(self.resources, "_tree_pids", return_value=[10, 11]), \
                mock.patch.object(self.resources, "_windows_process_snapshot",
                                  return_value={10: (1, "python.exe"),
                                                11: (10, "conhost.exe")}), \
                mock.patch.object(self.resources, "_cpu_seconds", return_value=0.0), \
                mock.patch.object(self.resources, "_rss_bytes", return_value=1), \
                mock.patch.object(self.resources, "_handle_count", return_value=1):
            measured = self.resources._TreeAccumulator(10).metrics()
        self.assertFalse(measured["child_work_seen"])

    def test_windows_indexd_harness_uses_expected_base_detachment(self):
        with mock.patch.object(self.resources.os, "name", "nt"):
            options = self.resources._indexd_spawn_options()
        self.assertEqual(options, {"creationflags": 0x00000208})

    def test_non_windows_indexd_harness_uses_a_private_session(self):
        with mock.patch.object(self.resources.os, "name", "posix"):
            self.assertEqual(
                self.resources._indexd_spawn_options(),
                {"start_new_session": True})

    def test_cold_ingest_is_bound_to_the_exact_python_rust_writer(self):
        identity = {
            "AGREP_RUNTIME_BUILD_ID": "a" * 20,
            "AGREP_PYTHON_RUNTIME_BUILD_ID": "b" * 20,
            "AGREP_DERIVED_ADOPTION_OWNER_TOKEN": None,
            "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED": None,
        }
        completed = subprocess.CompletedProcess(
            ["python"], 0, self.resources.json.dumps(identity), "")
        original = {
            "AGREP_DATA_DIR": "/owned/fixture",
            "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED": "stale",
        }
        with mock.patch.object(
                self.resources.subprocess, "run", return_value=completed):
            bound = self.resources._bind_ingest_writer_env(
                Path("/repo/agrep-rs"), original)

        self.assertEqual(bound["AGREP_DATA_DIR"], "/owned/fixture")
        self.assertEqual(bound["AGREP_RUNTIME_BUILD_ID"], "a" * 20)
        self.assertEqual(bound["AGREP_PYTHON_RUNTIME_BUILD_ID"], "b" * 20)
        self.assertNotIn("AGREP_DERIVED_WRITER_IDENTITY_BLOCKED", bound)

    def test_semantic_resource_query_uses_query_policy(self):
        helper = self.resources._SEMANTIC_HELPER
        self.assertIn("model.embed_query(query)", helper)
        self.assertNotIn("model.embed_texts([query])", helper)

    def test_semantic_worker_env_explicitly_enables_the_measured_worker(self):
        model_root = Path(tempfile.gettempdir()) / "agrep-resource-model"
        env = self.resources._semantic_worker_env(
            {"AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1"},
            model_root)
        self.assertEqual(env["AGREP_NO_DAEMON"], "")
        self.assertEqual(env["AGREP_NO_SEM_WORKER"], "")
        self.assertEqual(env["AGREP_MODEL_DIR"], os.fspath(model_root))
        self.assertEqual(env["AGREP_SEM_IDLE_S"], "300")

    def test_semantic_request_deadline_header_matches_worker(self):
        import semworker

        self.assertEqual(
            self.resources.SEMANTIC_DEADLINE_HEADER,
            semworker.REQUEST_DEADLINE_HEADER)

    def test_semantic_idle_sample_spans_owner_and_policy_polls(self):
        self.assertEqual(
            self.resources._semantic_idle_sample_duration(2.0, 5.0, 30.0),
            35.25)
        self.assertEqual(
            self.resources._semantic_idle_sample_duration(40.0, 5.0, 30.0),
            40.0)
        for value in (None, 0.0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.resources._semantic_idle_sample_duration(
                    2.0, value, 30.0)
            with self.subTest(refresh=value), self.assertRaises(RuntimeError):
                self.resources._semantic_idle_sample_duration(
                    2.0, 5.0, value)

    def test_active_profile_dimension_and_provider_are_preserved(self):
        payload = (b'{"dim":768,"profile":"candidate","requested_provider":'
                   b'"CPUExecutionProvider","available_providers":'
                   b'["CPUExecutionProvider"],"onnxruntime":"1.2.3"}\n')
        completed = mock.Mock(returncode=0, stdout=payload.decode(), stderr="")
        with mock.patch.object(self.resources.subprocess, "run",
                               return_value=completed):
            profile = self.resources._active_semantic_profile({}, Path("models"))
        self.assertEqual(profile["dim"], 768)
        self.assertEqual(profile["requested_provider"], "CPUExecutionProvider")

    def test_missing_semantic_runtime_is_optional(self):
        with mock.patch.object(
                self.resources, "_active_semantic_profile",
                side_effect=RuntimeError("optional runtime missing")):
            result = self.resources._measure_semantic(
                Path("data"), {}, Path("models"), sample_s=0.1)
        self.assertEqual(result["semantic_status"], "skipped")
        self.assertIn("optional runtime missing", result["semantic_skip_reason"])

    def test_unavailable_semantic_owner_is_optional_for_portable_gate(self):
        helper = self.resources._SEMANTIC_HELPER
        unavailable = helper[helper.index("if lifetime is None:"):
                             helper.index("try:", helper.index("if lifetime is None:"))]
        self.assertIn('"state": "skipped"', unavailable)
        self.assertIn("raise SystemExit(75)", unavailable)

    def test_semantic_gate_requires_an_idle_cpu_measurement(self):
        result = {
            name: 0.0
            for name in (
                self.resources.PORTABLE_BUDGETS
                | self.resources.SEMANTIC_BUDGETS)
        }
        result["semantic_status"] = "measured"
        result.pop("semantic_idle_cpu_percent")
        missing, breached = self.resources._gate(
            result, self.resources.BUDGETS, require_semantic=True)
        self.assertIn("semantic_idle_cpu_percent", missing)
        self.assertEqual(breached, [])

    def test_stop_process_closes_pipes_after_an_early_exit(self):
        process = mock.Mock()
        process.poll.return_value = 0
        self.resources._stop_process(process)
        process.terminate.assert_not_called()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_exited_private_group_is_not_signalled_after_permission_loss(self):
        process = mock.Mock(pid=42)
        process.poll.return_value = 1
        setattr(process, self.resources._PRIVATE_GROUP_ATTR, 42)
        with mock.patch.object(
                self.resources.os, "killpg", side_effect=PermissionError,
                create=True):
            self.resources._stop_posix_process(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "killpg"),
        "requires POSIX process groups")
    def test_stop_process_catches_child_spawned_by_term_handler(self):
        with tempfile.TemporaryDirectory(prefix="agrep-resource-stop-") as tmp:
            root = Path(tmp)
            ready = root / "ready"
            child_pid_path = root / "child-pid"
            script = (
                "import pathlib,signal,subprocess,sys,time\n"
                "ready=pathlib.Path(sys.argv[1])\n"
                "child_pid=pathlib.Path(sys.argv[2])\n"
                "def stop(_signum,_frame):\n"
                " child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(60)'])\n"
                " child_pid.write_text(str(child.pid),encoding='ascii')\n"
                " while True: time.sleep(1)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "ready.touch()\n"
                "while True: time.sleep(1)\n")
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(ready), str(child_pid_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
            self.resources._validated_private_process(process)
            try:
                deadline = time.monotonic() + 5.0
                while not ready.exists() and time.monotonic() < deadline:
                    self.assertIsNone(process.poll())
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                with mock.patch.object(
                        self.resources, "_STOP_TERM_TIMEOUT_S", 1.0):
                    self.resources._stop_process(process)
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.wait(timeout=5)

    def test_mac_cpu_ticks_use_the_mach_timebase(self):
        usage = self.resources._MacRusageV2()
        usage.user_ns = 18_000_000
        usage.system_ns = 6_000_000
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.resources, "_mac_rusage", return_value=usage), \
                mock.patch.object(
                    self.resources, "_mac_timebase_ns_per_tick",
                    return_value=125 / 3):
            seconds = self.resources._cpu_seconds(42)
        self.assertAlmostEqual(seconds, 1.0)


if __name__ == "__main__":
    unittest.main()
