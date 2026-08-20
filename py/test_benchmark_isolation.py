"""Release benchmarks must not inherit the user's derived-store owner."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class BenchmarkIsolationTests(unittest.TestCase):
    @staticmethod
    def _semantic_scale_module():
        path = ROOT / "bench" / "semantic_cli_scale.py"
        spec = importlib.util.spec_from_file_location(
            "semantic_cli_scale_fixture_test", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot import benchmark: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw) / "ambient"
            data.mkdir()
            (data / ".derived-owner.json").write_text(
                json.dumps({"version": 1, "build_id": "0" * 20}),
                encoding="utf-8")
            env = dict(os.environ)
            env["AGREP_DATA_DIR"] = str(data)
            env.pop("_AGREP_SEMANTIC_SCALE_DATA_DIR", None)
            env.pop("_AGREP_EMBED_PLAN_SCALE_DATA_DIR", None)
            return subprocess.run(
                [PYTHON, str(ROOT / "bench" / script), *args], env=env,
                capture_output=True, text=True, timeout=60)

    def test_semantic_q8_scale_ignores_ambient_foreign_owner(self) -> None:
        result = self._run(
            "semantic_q8_scale.py", "--rows", "256", "--dim", "16",
            "--repeats", "1", "--parity-queries", "1", "--block-rows", "128",
            "--topup-rows", "16")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_embed_plan_scale_ignores_ambient_foreign_owner(self) -> None:
        result = self._run(
            "embed_plan_scale.py", "--rows", "2501", "--page", "1000", "--check")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_semantic_scale_rejects_a_degraded_generation(self) -> None:
        scale = self._semantic_scale_module()
        meta = {
            "kind": "agrep-meta", "engine": "semantic:hybrid",
            "freshness": {
                "state": "degraded", "failing": True,
                "may_be_stale": True, "code": "torn-generation",
            },
        }
        row = {
            "session": scale._session(0),
        }
        timing = {"phases_ms": {"q8_retrieval": 1.0}}
        result = subprocess.CompletedProcess(
            [], 0, json.dumps(meta) + "\n" + json.dumps(row) + "\n",
            "semantic timing " + json.dumps(timing) + "\n"
            "search done: 1 hit(s) in 1 chat(s) via semantic:hybrid;\n",
        )
        with self.assertRaisesRegex(RuntimeError, "degraded generation"):
            scale._validate_semantic(result)

    def test_semantic_scale_accepts_tool_kind_hits_after_the_envelope(self) -> None:
        scale = self._semantic_scale_module()
        payload = "\n".join((
            json.dumps({"kind": "agrep-meta", "engine": "semantic:hybrid"}),
            json.dumps({"kind": "tool", "session": scale._session(0)}),
        ))

        rows, meta = scale._search_json_payload(payload)

        self.assertEqual(rows[0]["kind"], "tool")
        self.assertEqual(meta["engine"], "semantic:hybrid")

    def test_semantic_scale_requires_exactly_one_leading_envelope(self) -> None:
        scale = self._semantic_scale_module()
        hit = json.dumps({"session": scale._session(0)})
        meta = json.dumps({"kind": "agrep-meta", "engine": "semantic:hybrid"})

        with self.assertRaisesRegex(RuntimeError, "leading metadata"):
            scale._search_json_payload(hit + "\n" + meta)
        with self.assertRaisesRegex(RuntimeError, "multiple metadata"):
            scale._search_json_payload(meta + "\n" + meta + "\n" + hit)

    def test_semantic_scale_rejects_a_behind_hybrid_page(self) -> None:
        scale = self._semantic_scale_module()
        timing = {"phases_ms": {"q8_retrieval": 1.0}}
        result = subprocess.CompletedProcess(
            [], 0, "~semantic meaning-only planted winner\n",
            "index 2s behind\nsemantic timing " + json.dumps(timing) + "\n"
            "search done: 1 hit(s) in 1 chat(s) via corpusdb+semantic:hybrid;\n",
        )
        with self.assertRaisesRegex(RuntimeError, "degraded generation"):
            scale._validate_hybrid(result)

    def test_semantic_scale_scrubs_a_readonly_parent_environment(self) -> None:
        scale = self._semantic_scale_module()
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {"AGREP_DATA_READONLY": "1"}):
            root = Path(raw)
            env = scale._private_env(root / "home", root / "data", root / "model")
        self.assertNotIn("AGREP_DATA_READONLY", env)

    def test_semantic_scale_tolerates_one_transient_owner_observation(self) -> None:
        scale = self._semantic_scale_module()
        process = mock.Mock(pid=41, returncode=None)
        process.poll.return_value = None
        healthy = {"running": True, "responsive": True, "pid": 41}
        with mock.patch.object(
                scale, "_freshener_status",
                side_effect=({"running": False, "blocked": True,
                              "state": "hostile"}, healthy)) as status, \
                mock.patch.object(scale.time, "sleep") as sleep:
            self.assertEqual(scale._assert_freshener(process, {}), healthy)
        self.assertEqual(status.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_semantic_scale_rejects_persistent_owner_degradation(self) -> None:
        scale = self._semantic_scale_module()
        process = mock.Mock(pid=41, returncode=None)
        process.poll.return_value = None
        hostile = {"running": False, "blocked": True, "state": "hostile"}
        with mock.patch.object(
                scale, "_freshener_status", return_value=hostile) as status, \
                mock.patch.object(scale.time, "sleep") as sleep, \
                self.assertRaisesRegex(RuntimeError, "ownership moved or degraded"):
            scale._assert_freshener(process, {})
        self.assertEqual(status.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_semantic_scale_requests_stop_before_waiting(self) -> None:
        scale = self._semantic_scale_module()

        class Process:
            returncode = 0
            stderr = io.StringIO("")

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(timeout):
                del timeout
                self.assertEqual(stop.read_text(encoding="ascii"), "stop\n")

            @staticmethod
            def terminate():
                raise AssertionError("graceful stop should not terminate")

        with tempfile.TemporaryDirectory() as raw:
            stop = Path(raw) / "stop"
            stderr, forced = scale._request_freshener_stop(Process(), stop)
        self.assertEqual(stderr, "")
        self.assertFalse(forced)

    def test_semantic_scale_python_39_names_one_project_rerun(self) -> None:
        scale = self._semantic_scale_module()

        message = scale._runtime_error(["--quick", "--runs", "1"], (3, 9, 6))

        self.assertIsNotNone(message)
        self.assertIn("requires Python 3.10+; running 3.9.6", message)
        self.assertEqual(message.count("rerun with the project runtime:"), 1)
        expected = ".venv\\Scripts\\python.exe" if os.name == "nt" \
            else ".venv/bin/python"
        self.assertIn(expected, message)
        self.assertIn(str(ROOT / "bench" / "semantic_cli_scale.py"), message)
        self.assertTrue(message.endswith("--quick --runs 1"))

    def test_quick_scale_does_not_project_ten_million_rows(self) -> None:
        scale = self._semantic_scale_module()
        reports = [
            {"rows": 10_000, "warm_semantic": {"median_ms": 127.5}},
            {"rows": 50_000, "warm_semantic": {"median_ms": 134.6}},
        ]

        projection = scale._projection(reports)

        self.assertEqual(projection["status"], "not-computed")
        self.assertIsNone(projection["projected_10m_ms"])
        self.assertIn("at or above 1M", projection["reason"])
        self.assertIn("not computed", scale._projection_line(projection))

    def test_scale_projection_requires_a_wide_large_corpus_basis(self) -> None:
        scale = self._semantic_scale_module()
        reports = [
            {"rows": 1_000_000, "warm_semantic": {"median_ms": 90.0}},
            {"rows": 2_000_000, "warm_semantic": {"median_ms": 110.0}},
        ]

        projection = scale._projection(reports)

        self.assertEqual(projection["status"], "valid")
        self.assertEqual(projection["kind"], "projected")
        self.assertEqual(projection["basis_rows"], [1_000_000, 2_000_000])
        self.assertEqual(projection["projected_10m_ms"], 270.0)
        self.assertEqual(
            scale._projection_line(projection),
            "10M projected warm semantic: 270.0ms")

    def test_measured_ten_million_rows_are_not_called_a_projection(self) -> None:
        scale = self._semantic_scale_module()
        reports = [{
            "rows": 10_000_000,
            "warm_semantic": {"median_ms": 244.25},
        }]

        projection = scale._projection(reports)

        self.assertEqual(projection["kind"], "measured")
        self.assertEqual(
            scale._projection_line(projection),
            "10M measured warm semantic: 244.2ms")

    def test_invalid_projection_is_a_gate_failure_not_a_number(self) -> None:
        scale = self._semantic_scale_module()
        report = {
            "rows": scale.DESIGN_ROWS,
            "warm_semantic": {"median_ms": 100.0},
            "warm_hybrid": {"median_ms": 200.0},
            "cold_semantic": {"median_ms": 400.0},
        }
        projection = {
            "status": "not-computed", "projected_10m_ms": None,
            "reason": "insufficient scale basis",
        }

        failures = scale._failures(
            [report], projection, scale._budgets("target"))

        self.assertEqual(len(failures), 1)
        self.assertIn("insufficient scale basis", failures[0])

    def test_quick_json_discloses_that_no_gate_or_projection_ran(self) -> None:
        scale = self._semantic_scale_module()

        def report(rows, _args, _model_root):
            return {"rows": rows, "warm_semantic": {"median_ms": 130.0}}

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "agrep-rs"
            binary.touch()
            model = root / "model" / "revision"
            model.mkdir(parents=True)
            embedder = mock.MagicMock()
            embedder.ensure_model.return_value = model
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(scale, "RUST_BIN", binary), \
                    mock.patch.object(scale, "_campaign", side_effect=report), \
                    mock.patch.object(scale, "_provenance", return_value={}), \
                    mock.patch.dict(sys.modules, {"embedder": embedder}), \
                    mock.patch.object(sys, "path", list(sys.path)), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                returncode = scale.main(["--quick", "--runs", "1", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["gate_status"], "not-run")
        self.assertIsNone(payload["failures"])
        self.assertEqual(payload["projection"]["status"], "not-computed")
        self.assertIsNone(payload["projection"]["projected_10m_ms"])


if __name__ == "__main__":
    unittest.main()
