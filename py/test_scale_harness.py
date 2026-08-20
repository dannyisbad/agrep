from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


def _load_scale():
    path = Path(__file__).resolve().parents[1] / "bench" / "scale.py"
    spec = importlib.util.spec_from_file_location("agrep_scale_harness_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ScaleHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scale = _load_scale()

    def test_private_env_pins_binary_and_removes_agent_identity(self):
        inherited = {
            "AGREP_RS_BIN": "/wrong/agrep-rs",
            "CODEX_THREAD_ID": "would-hide-a-row",
            "CLAUDE_CODE_SESSION_ID": "would-hide-a-row",
            "AGREP_PI_SESSION_ID": "would-hide-a-row",
            "CLAUDECODE": "1",
            "CLINE_DIR": "/real/cline",
            "OPENCODE_DB": "/real/opencode.db",
        }
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, inherited, clear=True):
            root = Path(tmp)
            env = self.scale._private_env(root / "home", root / "data")
        self.assertEqual(env["AGREP_RS_BIN"], str(self.scale.RUST_BIN))
        # every var calling_identity reads, or self-exclusion hides a row
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("AGREP_PI_SESSION_ID", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLINE_DIR", env)
        self.assertNotIn("OPENCODE_DB", env)
        self.assertEqual(env["AGREP_NO_DAEMON"], "1")

    def test_search_command_is_full_cli_with_normal_freshness(self):
        cmd = self.scale._search_command(self.scale.CASES[0])
        self.assertEqual(cmd[:2], [sys.executable, str(self.scale.ROOT / "cli.py")])
        self.assertNotIn("--no-auto", cmd)

    def test_json_miss_metadata_is_not_counted_as_a_hit(self):
        payload = json.dumps({
            "kind": "agrep-meta", "query": "absent", "hits": [],
            "freshness": {"state": "current"},
        })
        rows, meta = self.scale._json_payload(payload + "\n")
        self.assertEqual(rows, [])
        self.assertEqual(meta["query"], "absent")

    def test_json_payload_accepts_tool_hits_after_the_envelope(self):
        payload = "\n".join((
            json.dumps({"kind": "agrep-meta", "engine": "corpusdb"}),
            json.dumps({"kind": "tool", "session": "s", "turn": 1}),
        ))
        rows, meta = self.scale._json_payload(payload)
        self.assertEqual(rows, [{"kind": "tool", "session": "s", "turn": 1}])
        self.assertEqual(meta["engine"], "corpusdb")

    def test_json_payload_requires_exactly_one_leading_envelope(self):
        hit = json.dumps({"session": "s", "turn": 1})
        meta = json.dumps({"kind": "agrep-meta", "engine": "corpusdb"})
        with self.assertRaisesRegex(ValueError, "leading metadata"):
            self.scale._json_payload(hit + "\n" + meta)
        with self.assertRaisesRegex(ValueError, "multiple metadata"):
            self.scale._json_payload(meta + "\n" + meta + "\n" + hit)

    def test_fixture_has_exact_old_tail_density(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            proof = self.scale._write_fixture(data, 10_000)
            rows = [json.loads(line) for line in
                    (data / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        old = [row for row in rows if self.scale.MID_OLD_QUERY in row["text"]]
        dense = [row for row in rows if self.scale.OLD_DENSE_QUERY in row["text"]]
        self.assertEqual(len(old), proof["mid_old_rows"])
        self.assertEqual(len(dense), proof["old_dense_rows"])
        self.assertTrue(all(row["who"] == "user" for row in old))
        self.assertLess(max(row["ts"] for row in old), min(
            row["ts"] for row in rows if self.scale.MID_OLD_QUERY not in row["text"]))

    def test_fixture_digest_helper_matches_production_contract(self):
        import compact

        for text in ("", "hello world", "café 🧡"):
            with self.subTest(text=text):
                self.assertEqual(
                    self.scale._content_digest(text), compact.content_digest(text))

    def test_fixture_publishes_generation_bound_family_census(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.scale._write_fixture(data, 10_000)
            import session_context

            proof = session_context.read_session_family_proof(data)
            census = session_context.read_session_family_census(data)
        if proof is None or census is None:
            self.fail("scale fixture family publication did not validate")
        self.assertEqual(proof.count, self.scale.SPECIAL_ROWS)
        self.assertEqual(len(census.sessions), self.scale.SPECIAL_ROWS)
        self.assertEqual(census.parents["scale-child-007"], "scale-root-001")

    def test_fixture_database_carries_verified_family_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, home = root / "data", root / "home"
            data.mkdir()
            home.mkdir()
            self.scale._write_fixture(data, 10_000)
            env = self.scale._private_env(home, data)
            result = subprocess.run(
                [sys.executable, str(self.scale.ROOT / "bench" / "scale.py"),
                 "--_build-data", str(data)],
                cwd=self.scale.ROOT, env=env, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            db = sqlite3.connect(data / "corpus.db")
            try:
                root_row = db.execute(
                    "SELECT root FROM session_family WHERE session=?",
                    ("scale-child-007",),
                ).fetchone()
                family_stamp = db.execute(
                    "SELECT value FROM meta WHERE key='family_stamp'",
                ).fetchone()
                build_id = db.execute(
                    "SELECT value FROM meta WHERE key='build_id'",
                ).fetchone()
                missing_digests = int(db.execute(
                    "SELECT count(*) FROM msgs WHERE content_digest IS NULL",
                ).fetchone()[0])
                digest_row = db.execute(
                    "SELECT text, content_digest FROM msgs WHERE session=?",
                    ("scale-child-007",),
                ).fetchone()
            finally:
                db.close()
            import compact
            import session_context

            proof = session_context.read_session_family_proof(data)
            owner = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
        self.assertEqual(root_row, ("scale-root-001",))
        self.assertIsNotNone(proof)
        self.assertEqual(family_stamp, (proof.stamp if proof else None,))
        self.assertEqual(missing_digests, 0)
        self.assertIsNotNone(digest_row)
        self.assertEqual(digest_row[1], compact.content_digest(digest_row[0]))
        self.assertEqual(build_id, (owner["build_id"],))
        self.assertEqual(set(owner), {"version", "build_id"})
        self.assertEqual(owner["version"], 1)

    def test_budget_gate_uses_completion_medians(self):
        budgets = self.scale.SCALE_BUDGETS_MS[1_000_000]
        report = {
            "rows": 1_000_000,
            "workloads": {
                name: {"median_ms": float(budget), "peak_rss_mib": 100.0,
                       "cpu_seconds_sampled": float(budget) / 1_000}
                for name, budget in budgets.items()
            },
            "index_build": {"wall_ms": 10_000.0, "rss_mib": 64.0},
            "storage": {"db_bytes": 500 * self.scale.MIB, "bytes_per_row": 800.0},
        }
        self.assertEqual(self.scale._budget_failures([report]), [])
        report["workloads"]["selective"]["median_ms"] = 121.0
        self.assertEqual(
            self.scale._budget_failures([report]),
            ["1,000,000 selective: 121.0ms > 120.0ms"],
        )
        self.assertEqual(self.scale._budget_failures([report], "portable-ci"), [])

    def test_budget_gate_rejects_unbudgeted_and_incomplete_campaigns(self):
        unbudgeted = {"rows": 30_000, "workloads": {}}
        self.assertEqual(
            self.scale._budget_failures([unbudgeted]),
            ["no committed scale budgets for 30,000 rows"],
        )
        incomplete = {
            "rows": 1_000_000, "workloads": {},
            "index_build": {"wall_ms": 1.0, "rss_mib": 1.0},
            "storage": {"db_bytes": 1, "bytes_per_row": 1.0},
        }
        failures = self.scale._budget_failures([incomplete])
        self.assertEqual(len(failures), len(self.scale.SCALE_BUDGETS_MS[1_000_000]))
        self.assertIn("1,000,000 selective: workload result missing", failures)

    def test_check_requires_three_samples(self):
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.scale._parse_args(["--check", "--runs", "2"])

    def test_check_requires_complete_scale_set(self):
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.scale._parse_args(["--check", "--rows", "1000000"])

    def test_portable_profile_requires_check(self):
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.scale._parse_args(["--budget-profile", "portable-ci"])

    def test_binary_freshness_detects_newer_rust_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "agrep-rs"
            source = root / "source.rs"
            binary.write_bytes(b"binary")
            source.write_text("fn main() {}\n", encoding="utf-8")
            old = binary.stat().st_mtime_ns
            os.utime(source, ns=(old + 1_000_000, old + 1_000_000))
            with mock.patch.object(self.scale, "ROOT", root), \
                    mock.patch.object(Path, "rglob", return_value=iter([source])), \
                    mock.patch.object(Path, "glob", return_value=iter(())):
                newer = self.scale._newer_rust_sources(binary)
        self.assertEqual(newer, [source])

    def test_sampled_run_survives_more_output_than_one_pipe_buffer(self):
        # A debug-heavy lane writes far past the OS pipe buffer. If the
        # sampler polls without draining, the child blocks on its own stderr
        # and the workload dies on the query timeout instead of reporting.
        line = "boundary: Rust scored 160 occurrence(s)\n"
        repeats = 40_000
        program = (
            "import sys\n"
            f"sys.stderr.write({line!r} * {repeats})\n"
            "sys.stdout.write('done')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, measured = self.scale._sampled_process(
                [sys.executable, "-c", program], cwd=Path(tmp), env=dict(os.environ),
                timeout=15.0, interval=0.005)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "done")
        self.assertEqual(len(result.stderr), len(line) * repeats)
        self.assertGreater(len(result.stderr), 1 << 16)
        self.assertIn("wall_ms", measured)


if __name__ == "__main__":
    unittest.main()
