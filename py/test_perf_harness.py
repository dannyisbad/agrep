from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


def _load_perf():
    path = Path(__file__).resolve().parents[1] / "bench" / "perf.py"
    spec = importlib.util.spec_from_file_location("agrep_perf_harness_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PerfHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.perf = _load_perf()

    def test_engine_sample_requires_lane_marker_and_exact_json_head(self):
        expected = [("session-a", 1, "user")]
        stdout = "\n".join((
            json.dumps({"kind": "agrep-meta", "engine": "corpusdb"}),
            json.dumps({
                "kind": "tool", "session": "session-a", "turn": 1,
                "who": "user",
            }),
        ))
        stderr = (
            "* [agrep +     8.0ms] bounded short rows: examined 4096 candidate(s)\n"
            "* [agrep +     9.0ms] search done: 4096 hit(s) via corpusdb")
        completed = subprocess.CompletedProcess(["agrep"], 0, stdout, stderr)
        with mock.patch.object(self.perf.subprocess, "run", return_value=completed):
            engine, _wall = self.perf._engine_ms(
                "hi", n=1, require_hit=True,
                required_debug_marker="bounded short rows:",
                expected_json_keys=expected)
        self.assertEqual(engine, 9.0)

        no_lane = subprocess.CompletedProcess(
            ["agrep"], 0, stdout,
            "* [agrep + 9.0ms] search done: 1 hit(s) via corpusdb")
        with mock.patch.object(self.perf.subprocess, "run", return_value=no_lane):
            engine, _wall = self.perf._engine_ms(
                "hi", n=1, require_hit=True,
                required_debug_marker="bounded short rows:",
                expected_json_keys=expected)
        self.assertIsNone(engine)

        wrong_head = subprocess.CompletedProcess(
            ["agrep"], 0,
            "\n".join((
                json.dumps({"kind": "agrep-meta", "engine": "corpusdb"}),
                json.dumps({"session": "wrong", "turn": 1, "who": "user"}),
            )), stderr)
        with mock.patch.object(self.perf.subprocess, "run", return_value=wrong_head):
            engine, _wall = self.perf._engine_ms(
                "hi", n=1, require_hit=True,
                required_debug_marker="bounded short rows:",
                expected_json_keys=expected)
        self.assertIsNone(engine)

    def test_json_head_requires_exactly_one_leading_envelope(self):
        hit = {"session": "session-a", "turn": 1, "who": "tool"}
        meta = {"kind": "agrep-meta", "engine": "corpusdb"}

        self.assertIsNone(self.perf._json_hit_keys(json.dumps(hit)))
        self.assertIsNone(self.perf._json_hit_keys("\n".join((
            json.dumps(hit), json.dumps(meta)))))
        self.assertIsNone(self.perf._json_hit_keys("\n".join((
            json.dumps(meta), json.dumps(meta), json.dumps(hit)))))

    def test_stream_completion_limit_cannot_undercut_cold_ingest(self):
        # Cold's slack is pinned explicitly: CI calibrates it via env, and an
        # ambient override would silently defuse the undercut premise.
        with mock.patch.dict(os.environ, {
            "AGREP_PERF_INGEST_COLD_SLACK": "4",
            "AGREP_PERF_STREAMED_COMPLETION_SLACK": "3.5",
        }):
            with self.assertRaisesRegex(ValueError, "must cover"):
                self.perf._effective_limits(4.0)
        with mock.patch.dict(os.environ, {
            "AGREP_PERF_INGEST_COLD_SLACK": "4",
            "AGREP_PERF_STREAMED_COMPLETION_SLACK": "4",
        }):
            limits = self.perf._effective_limits(4.0)
        self.assertGreaterEqual(
            limits["streamed_completion_ms"], limits["ingest_cold_ms"])

    def test_short_fixture_does_not_weaken_stopword_fixture(self):
        built = subprocess.CompletedProcess(["python"], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(self.perf.subprocess, "run", return_value=built):
            base, _env, error = self.perf._build_search_fixture(
                Path(tmp) / "base", 200, short=False)
            self.assertIsNone(error)
            short, _env, error = self.perf._build_search_fixture(
                Path(tmp) / "short", 200, short=True)
            self.assertIsNone(error)
            baseline = [json.loads(line) for line in
                        (base / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
            short_rows = [json.loads(line) for line in
                          (short / "messages.jsonl").read_text(
                              encoding="utf-8").splitlines()]
            base_family = json.loads(
                (base / "session_family.meta.json").read_text(encoding="utf-8"))
            short_family = json.loads(
                (short / "session_family.meta.json").read_text(encoding="utf-8"))

        self.assertEqual([row["ts"] for row in baseline], list(range(200)))
        self.assertTrue(all("this" not in row["text"] for row in baseline))
        self.assertGreater(short_rows[0]["ts"], short_rows[-1]["ts"])
        self.assertTrue(all("hi" in row["text"].lower() for row in short_rows))
        self.assertTrue(all(row["text"].startswith("hi ") for row in short_rows[:100]))
        self.assertFalse(short_rows[100]["text"].startswith("hi "))
        self.assertEqual(base_family["count"], 200)
        self.assertEqual(short_family["count"], 200)
        self.assertEqual(
            base_family["ingest_signature"], "200:private-search-baseline")
        self.assertEqual(
            short_family["ingest_signature"], "200:private-search-short")

    def test_engine_miss_floor_is_one_absent_fts_token(self):
        built = subprocess.CompletedProcess(["python"], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(self.perf.subprocess, "run", return_value=built):
            data, _env, error = self.perf._build_search_fixture(
                Path(tmp) / "base", 200, short=False)
            rows = [json.loads(line) for line in
                    (data / "messages.jsonl").read_text(
                        encoding="utf-8").splitlines()]

        tokens = [token for token in self.perf.re.split(
            r"[\s\-_]+", self.perf.ENGINE_MISS_Q.strip()) if token]
        self.assertIsNone(error)
        self.assertEqual(tokens, [self.perf.ENGINE_MISS_Q])
        self.assertTrue(all(self.perf.ENGINE_MISS_Q not in row["text"].lower()
                            for row in rows))

    def test_engine_miss_requires_unchecked_zero_proof(self):
        debug = (
            "* [agrep + 9.0ms] search done: 0 hit(s) in 0 chat(s) "
            "via corpusdb; showing 0\n")
        completed = subprocess.CompletedProcess(
            ["agrep"], 2, "", debug + "\n".join(
                self.perf._ENGINE_MISS_STDERR_PROOF))
        kwargs = {
            "n": 1,
            "allowed_returncodes": {2},
            "required_debug_marker": (
                "search done: 0 hit(s) in 0 chat(s) via corpusdb; showing 0"),
            "required_stderr": self.perf._ENGINE_MISS_STDERR_PROOF,
        }
        with mock.patch.object(self.perf.subprocess, "run", return_value=completed):
            engine, _wall = self.perf._engine_ms("missing", **kwargs)
        self.assertEqual(engine, 9.0)

        unproven = subprocess.CompletedProcess(["agrep"], 2, "", debug)
        with mock.patch.object(self.perf.subprocess, "run", return_value=unproven):
            engine, _wall = self.perf._engine_ms("missing", **kwargs)
        self.assertIsNone(engine)

        noisy = subprocess.CompletedProcess(
            ["agrep"], 2, "",
            debug + "\n".join(self.perf._ENGINE_MISS_STDERR_PROOF)
            + "\nunexpected failure\n")
        with mock.patch.object(self.perf.subprocess, "run", return_value=noisy):
            engine, _wall = self.perf._engine_ms("missing", **kwargs)
        self.assertIsNone(engine)

    def test_probe_miss_keeps_natural_language_shape(self):
        self.assertIn(" ", self.perf.PROBE_MISS_Q.strip())
        self.assertNotEqual(self.perf.PROBE_MISS_Q, self.perf.ENGINE_MISS_Q)

    def test_private_event_probe_closes_its_read_handle(self):
        connection = mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = (4600,)
        with mock.patch.object(self.perf.sqlite3, "connect", return_value=connection):
            count = self.perf._private_event_file_count(Path("events.sqlite3"))

        self.assertEqual(count, 4600)
        connection.close.assert_called_once_with()

    def test_private_event_probe_closes_its_read_handle_on_error(self):
        connection = mock.MagicMock()
        connection.execute.side_effect = self.perf.sqlite3.DatabaseError("bad store")
        with mock.patch.object(self.perf.sqlite3, "connect", return_value=connection):
            with self.assertRaises(self.perf.sqlite3.DatabaseError):
                self.perf._private_event_file_count(Path("events.sqlite3"))

        connection.close.assert_called_once_with()

    def test_ingest_sample_diagnostic_keeps_wall_and_rust_phases(self):
        run = subprocess.CompletedProcess(
            ["agrep-rs", "index"], 0,
            "  phases: source-check 12ms · load-cache 34ms · "
            "write-derived+source-validate 56ms\n",
            "",
        )

        sample = self.perf._ingest_sample_diagnostic(run, 123.4567)

        self.assertEqual(sample["wall_ms"], 123.457)
        self.assertEqual(sample["phases_ms"], {
            "source-check": 12.0,
            "load-cache": 34.0,
            "write-derived+source-validate": 56.0,
        })

    def test_ingest_sample_diagnostic_ignores_other_phase_reports(self):
        run = subprocess.CompletedProcess(
            ["agrep", "query"], 0, "",
            "embed phases: tokenize 90ms · inference 12ms\n"
            "* [agrep perf] ingest phases: source-check 4ms · load-cache 7ms\n",
        )

        sample = self.perf._ingest_sample_diagnostic(run, 20.0)

        self.assertEqual(sample["phases_ms"], {
            "source-check": 4.0,
            "load-cache": 7.0,
        })

    def test_ingest_proof_lines_expose_cold_and_stream_diagnostics(self):
        cold = self.perf._ingest_proof_line(
            "ingest cold proof",
            {"wall_ms": 18_579.25,
             "phases_ms": {"source-check": 402.0, "load-cache": 251.0}},
            {".ingest_cache.bin": 12_345, ".boundary_stats.bin": 67_890,
             "boundary_stats.json": 456, "messages.jsonl": 78_901},
        )
        stream = self.perf._ingest_proof_line(
            "stream proof",
            {"wall_ms": 25_197.4, "first_hit_ms": 1_354.1,
             "fts_delegate_hooks_ms": 65.25,
             "phases_ms": {"source-check": 0.0, "ingest+dedupe": 91.0}},
        )

        self.assertEqual(
            cold,
            "ingest cold proof: full=18579.2ms · "
            "phases[source-check=402,load-cache=251] · "
            "bytes[.ingest_cache.bin=12345,.boundary_stats.bin=67890,"
            "boundary_stats.json=456,messages.jsonl=78901]",
        )
        self.assertEqual(
            stream,
            "stream proof: first=1354.1ms · full=25197.4ms · fts-tail=65.2ms · "
            "phases[source-check=0,ingest+dedupe=91]",
        )

    def test_stream_fixture_disables_detached_semantic_upkeep(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "stream-data"
            self.perf._prepare_private_stream_data(data)
            settings = json.loads(
                (data / "settings.json").read_text(encoding="utf-8"))
            guard = (
                "import indexer,ownerfile,time\n"
                "class Watcher: _last_event_wall=0\n"
                "owner=indexer.AutoIndexer(Watcher(),owns_lifetime=lambda:True,"
                "owner_snapshot=ownerfile.Snapshot((1,2,0,0),0.0,b''))\n"
                "now=time.monotonic()\n"
                "owner._last_teach_check=owner._last_archive_check=now\n"
                "def unexpected(*args,**kwargs): raise RuntimeError('semantic spawned')\n"
                "owner._refresh_embeddings=unexpected\n"
                "owner._run_housekeeping(now)\n"
            )
            run = subprocess.run(
                [self.perf.sys.executable, "-c", guard], cwd=self.perf.PY,
                env={**self.perf.os.environ, "AGREP_DATA_DIR": str(data)},
                capture_output=True, text=True, encoding="utf-8", errors="replace")

        self.assertEqual(settings, {"embeddings": "off"})
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_windows_sensitive_metrics_can_use_explicit_platform_slack(self):
        with mock.patch.dict(
                self.perf.os.environ,
                {"AGREP_PERF_INGEST_ONE_CHANGED_SLACK": "8",
                 "AGREP_PERF_STREAMED_FIRST_HIT_SLACK": "8",
                 "AGREP_PERF_STREAMED_COMPLETION_SLACK": "4"}):
            self.assertEqual(self.perf._metric_slack("ingest_one_changed_ms", 4), 8)
            self.assertEqual(self.perf._metric_slack("streamed_first_hit_ms", 4), 8)
            self.assertEqual(self.perf._metric_slack("streamed_completion_ms", 4), 4)
            self.assertEqual(self.perf._metric_slack("engine_selective_ms", 4), 4)
        with mock.patch.dict(self.perf.os.environ, {}, clear=True):
            self.assertEqual(self.perf._metric_slack("streamed_first_hit_ms", 4), 4)

    def test_machine_limits_disclose_per_metric_slack(self):
        with mock.patch.dict(
                self.perf.os.environ,
                {"AGREP_PERF_INGEST_ONE_CHANGED_SLACK": "8",
                 "AGREP_PERF_STREAMED_FIRST_HIT_SLACK": "8",
                 "AGREP_PERF_STREAMED_COMPLETION_SLACK": "4"}):
            limits = self.perf._effective_limits(4)
        self.assertEqual(limits["ingest_one_changed_ms"], 4_000)
        self.assertEqual(limits["streamed_first_hit_ms"], 1_200)
        self.assertEqual(limits["streamed_completion_ms"], 15_000)

    def test_full_exit_budget_cannot_be_tighter_than_its_cold_ingest(self):
        self.assertGreaterEqual(
            self.perf.BUDGETS["streamed_completion_ms"],
            self.perf.BUDGETS["ingest_cold_ms"])

    def test_python_39_fails_before_measurement_with_one_project_rerun(self):
        message = self.perf._runtime_error(
            ["--check-semantic"], (3, 9, 6))

        self.assertIsNotNone(message)
        self.assertIn("requires Python 3.10+; running 3.9.6", message)
        self.assertEqual(message.count("rerun with the project runtime:"), 1)
        expected = ".venv\\Scripts\\python.exe" if os.name == "nt" \
            else ".venv/bin/python"
        self.assertIn(expected, message)
        self.assertIn(str(Path(__file__).resolve().parents[1]
                          / "bench" / "perf.py"), message)
        self.assertTrue(message.endswith("--check-semantic"))

    def test_slack_overrides_are_finite_positive_and_bounded(self):
        for name in self.perf._METRIC_SLACK_ENVS.values():
            for raw in ("inf", "nan", "0", "-1", "10.1", "not-a-number"):
                with self.subTest(name=name, raw=raw), mock.patch.dict(
                        self.perf.os.environ, {name: raw}):
                    with self.assertRaises(ValueError):
                        self.perf._effective_limits(4)
        with mock.patch.dict(self.perf.os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                self.perf._slack_value("AGREP_PERF_SLACK", float("inf"))


if __name__ == "__main__":
    unittest.main()
