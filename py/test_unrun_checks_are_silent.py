"""A check that did not run earns no row, and no machine field pretends it did.

The doctor surface has one deferral phrasing: four
spellings became one, and every row that should not have existed still
rendered. So none of these pins name a phrasing. They assert shapes a rewrite
cannot satisfy - that a row is absent, that a render is empty, and that the
routine verdict equals the verdict the deep tier reaches on the same bytes.

The verdict pin builds both states in a sandbox data directory and compares
the two tiers on identical bytes, because the bug it exists to catch was
`status --json` answering "ready" while `doctor --deep` called the same
database unreadable.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import cli  # noqa: E402
import corpusdb  # noqa: E402
import doctor  # noqa: E402
from test_perf_budgets import (  # noqa: E402
    _prepare_paths, _public_cli, _require_release_binary, _write_small_fixture,
)

SANDBOX_ROOT = tempfile.gettempdir()


def _write_rebuild_marker(database: Path) -> Path:
    state, build_id, detail = corpusdb._database_build_id(path=database)
    if state != "owned" or build_id is None:
        raise AssertionError(f"fixture database has no owner: {state} {detail}")
    marker = database.with_name(".corpusdb-rebuild")
    marker.write_text(json.dumps({
        "version": corpusdb._QUERY_REBUILD_MARKER_VERSION,
        "database_identity": list(corpusdb._optional_sqlite_identity(database)),
        "build_id": build_id,
    }), encoding="utf-8")
    return marker


class UnrunChecksRenderNothing(unittest.TestCase):
    def rows(self, snapshot: dict, *, deep: bool = False) -> list[str]:
        """Every label the report emits, taken from the emitter itself."""
        seen: list[str] = []
        with (
            mock.patch.object(
                doctor, "_row",
                side_effect=lambda label, *a, **k: seen.append(label)),
            mock.patch.object(doctor, "probe", return_value=snapshot),
            mock.patch.object(doctor, "_report_teach_reconcile"),
            mock.patch.object(doctor.common, "detected_stores", return_value=[]),
            mock.patch.object(doctor.dist, "cli_invocation", return_value=("agrep",)),
        ):
            with mock.patch("sys.stdout", new=io.StringIO()):
                doctor.report(deep=deep)
        return seen

    def test_a_tier_that_concluded_nothing_renders_nothing(self) -> None:
        """The strongest form: if no check reached a verdict, there are no rows.

        Not one row, not a footer, not a count of what was skipped.
        """
        from test_doctor_output import _report_snapshot, _semantic_state
        unrun = {"state": "status-deferred"}
        snapshot = _report_snapshot(
            paths={"data_dir": "/fixture", "data_source": "default",
                   "warnings": []},
            core={
                "live": True, "rust": True, "binary": True, "stores": [],
                "indexed": {"state": "status-deferred"},
                "search_db": {**unrun,
                              "integrity": doctor._integrity_not_verified()},
                "store_observation": dict(unrun),
            },
            archive=dict(unrun),
            model_attribution={"state": "not-inspected"},
            teach_reconcile={**unrun, "refusals": [], "preserved_newer": []},
            teach_enrollment={**unrun, "targets": None},
            sentinel=dict(unrun),
            install_lag=dict(unrun),
            detection=dict(unrun),
            semantic=_semantic_state(
                runtime_verified=False, runtime_state="not-inspected",
                embed_job="not-inspected"),
            resources={
                "data": {**unrun, "complete": False},
                "orphans": {**unrun, "complete": False},
                "indexd": {**unrun, "running": False},
            },
            drift=[],
            detected=[],
            diagnostics={"deferred": ["everything"]},
        )
        # the data directory's path, the shipped binary and the running
        # interpreter are known by existing; they cannot be a check that
        # did not run, so they are the only labels allowed to survive
        always_known = {"location", "ingest binary", "python", "runtime build"}
        rendered = self.rows(snapshot)
        self.assertEqual(
            [label for label in rendered if label not in always_known], [],
            f"rows survived a tier that concluded nothing: {rendered}")

    def test_each_deep_only_check_is_absent_from_routine_and_present_in_deep(
            self) -> None:
        """The two checks measured too expensive for routine own no routine row.

        Their absence is the contract: `--deep` is where they speak.
        """
        from test_doctor_output import _report_snapshot
        routine = self.rows(_report_snapshot(
            model_attribution={"state": "not-inspected"},
            core={
                **_report_snapshot()["core"],
                "search_db": {
                    "state": "ready", "detail": "current",
                    "integrity": doctor._integrity_not_verified(),
                },
            },
        ))
        self.assertNotIn("integrity", routine)
        self.assertNotIn("model attribution", routine)
        deep = self.rows(_report_snapshot(), deep=True)
        self.assertIn("integrity", deep)
        self.assertIn("model attribution", deep)


class RoutineAndDeepAgree(unittest.TestCase):
    """Both tiers read the same sandbox bytes; their verdicts must match."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_release_binary()
        cls.tmp = tempfile.TemporaryDirectory(
            prefix="agrep-verdict-", dir=SANDBOX_ROOT)
        root = Path(cls.tmp.name)
        (cls.home, cls.data, cls.protected,
         cls.canary) = _prepare_paths(root)
        _write_small_fixture(cls.home, root)
        built = cls._cli("index")
        if built.returncode != 0:
            raise AssertionError(f"fixture ingest failed: {built.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @classmethod
    def _cli(cls, *args: str, timeout: float = 180):
        return _public_cli(
            cls.home, cls.data, cls.protected, *args, timeout=timeout)

    def verdicts(self) -> tuple[str, str]:
        """(what a machine is told by routine, what the deep tier concludes)."""
        machine = self._cli("status", "--json")
        self.assertEqual(machine.returncode, 0, machine.stderr[-400:])
        routine = json.loads(machine.stdout)["search_index_state"]
        probe = self._cli("doctor", "--json", "--deep")
        self.assertEqual(probe.returncode, 0, probe.stderr[-400:])
        return routine, json.loads(probe.stdout)["core"]["search_db"]["state"]

    def test_a_healthy_database_is_ready_in_both_tiers(self) -> None:
        routine, deep = self.verdicts()
        self.assertEqual(routine, "ready")
        self.assertEqual(routine, deep)

    def test_a_pending_rebuild_is_never_reported_as_ready(self) -> None:
        """The bug this suite exists for: routine said ready, deep said not.

        An agent gates on the routine field, so it may lag the deep verdict
        only by saying less - never by saying ready.
        """
        marker = _write_rebuild_marker(self.data / "corpus.db")
        try:
            routine, deep = self.verdicts()
        finally:
            marker.unlink()
        self.assertNotEqual(routine, "ready")
        self.assertEqual(routine, deep)

    def test_a_damaged_database_is_never_reported_as_ready(self) -> None:
        database = self.data / "corpus.db"
        original = database.read_bytes()
        database.write_bytes(original[:4096])
        try:
            routine, deep = self.verdicts()
        finally:
            database.write_bytes(original)
        self.assertNotEqual(routine, "ready")
        self.assertEqual(routine, deep)


class HealthyBoxIsQuiet(RoutineAndDeepAgree):
    def test_bare_agrep_says_nothing_about_the_tool_itself(self) -> None:
        """A working box gets its corpus and what to type, and no diagnosis."""
        rendered = self._cli("status")
        self.assertEqual(rendered.returncode, 0, rendered.stderr[-400:])
        body = rendered.stdout.lower()
        for confession in (
                "not inspected", "not verified", "deferred", "routine tier",
                "budget", "doctor --deep"):
            self.assertNotIn(confession, body)


if __name__ == "__main__":
    unittest.main()
