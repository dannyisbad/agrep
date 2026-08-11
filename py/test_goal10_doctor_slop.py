"""Goal 10 doctor/status slop contracts: replayable remedies and concise rows."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import console  # noqa: E402
import dist  # noqa: E402
import doctor  # noqa: E402
import surface_policy as surface  # noqa: E402


def _semantic_state(**updates: object) -> dict:
    state = {
        "live": True,
        "available": True,
        "runtime_verified": True,
        "runtime_state": "verified",
        "optional": True,
        "deps": {
            "numpy": True, "onnxruntime": True, "tokenizers": True},
        "model_cached": True,
        "model_integrity": {
            "state": "verified", "checked": True, "verified": True,
            "cached": True,
            "detail": "cached model SHA256 verified for unchanged files",
        },
        "embeddings": "current",
        "embedding_integrity": {"state": "verified", "verified": True},
        "embedding_coverage": None,
        "embed_job": "idle",
        "embed_fail_reason": None,
        "embed_pid": None,
        "embed_phase": None,
        "embed_done": None,
        "embed_total": None,
        "embed_running": False,
        "resident_worker": {"running": False},
    }
    state.update(updates)
    return state


def _report_snapshot(**updates: object) -> dict:
    snapshot = {
        "paths": {
            "data_dir": "/fixture/data", "data_source": "fixture",
            "warnings": [],
        },
        "core": {
            "live": True, "rust": True, "binary": True,
            "indexed": {"state": "ready"},
            "search_db": {
                "state": "ready", "detail": "current",
                "integrity": {
                    "state": "verified", "detail": "PRAGMA quick_check passed",
                },
            },
            "stores": [],
            "store_observation": {"state": "complete"},
        },
        "archive": {
            "state": "healthy", "detail": "capture is healthy",
            "last_pass": {"age_s": 1.0, "fresh": True},
        },
        "teach_reconcile": {
            "state": "clean", "refusals": [], "preserved_newer": []},
        "teach_enrollment": {"state": "unenrolled", "targets": 0},
        "sentinel": {"state": "not-applicable"},
        "model_attribution": {"state": "empty"},
        "install_lag": {"state": "not-installed", "detail": "source checkout"},
        "settings": {
            "embeddings": {"state": "verified", "value": "auto"}},
        "semantic": _semantic_state(),
        "resources": {
            "data": {
                "state": "complete", "complete": True,
                "files": 1, "bytes": 1, "detail": None,
            },
            "orphans": {
                "state": "complete", "complete": True,
                "count": 0, "bytes": 0,
            },
            "indexd": {"state": "not-running", "running": False},
        },
        "freshness": {"state": "no-known-failure"},
        "detected": [],
        "detection": {"state": "complete"},
        "diagnostics": {"deferred": []},
        "_render": {
            "summary": {
                "messages": 1, "sessions": 1, "age_s": 0,
                "per_agent": [],
            },
            "footprint_breakdown": "",
            "freshness_failures": 0,
            "freshness_failure_detail": "",
            "indexing_failure": None,
            "generation": {"state": "ready"},
        },
    }
    snapshot.update(updates)
    return snapshot


def _render_report(snapshot: dict, *, deep: bool = False) -> str:
    output = io.StringIO()
    with (
        mock.patch.object(doctor, "probe", return_value=snapshot),
        contextlib.redirect_stdout(output),
    ):
        doctor.report(deep=deep)
    return output.getvalue()


class InvocationResolverTests(unittest.TestCase):
    def test_dev_invocation_is_absolute_and_cwd_independent(self) -> None:
        root = Path("/fixture repo")
        with (
            mock.patch.object(dist, "_is_dev_checkout", return_value=True),
            mock.patch.object(dist, "REPO_ROOT", root),
            mock.patch.object(dist.sys, "executable", "/fixture python"),
        ):
            argv = dist.cli_invocation(
                "index", environ={"AGREP_CLI_NAME": ""})
        self.assertEqual(argv, (
            "/fixture python", str(root / "cli.py"), "index"))
        self.assertTrue(Path(argv[1]).is_absolute())

    def test_installed_and_invoked_as_names_are_single_tokens(self) -> None:
        with mock.patch.object(
                dist, "_is_dev_checkout", return_value=False):
            self.assertEqual(
                dist.cli_invocation(
                    "doctor", environ={"AGREP_CLI_NAME": ""}),
                ("agrep", "doctor"),
            )
            self.assertEqual(
                dist.cli_invocation(
                    "setup",
                    environ={"AGREP_CLI_NAME": "/fixture shim/agrep"}),
                ("/fixture shim/agrep", "setup"),
            )
        with (
            mock.patch.object(
                dist, "_is_dev_checkout", return_value=True),
            mock.patch.object(dist.os.path, "abspath",
                              side_effect=lambda value: f"/abs/{value}"),
        ):
            self.assertEqual(
                dist.cli_invocation(
                    "index", environ={"AGREP_CLI_NAME": "./fixture-agrep"}),
                ("/abs/./fixture-agrep", "index"),
            )

    def test_semantic_install_targets_the_owning_environment(self) -> None:
        self.assertEqual(
            dist.semantic_install_argv(
                installer="uv",
                executable="/home/alice/uv/tools/agrep/bin/python",
                version="0.2.0"),
            ("uv", "pip", "install", "--python",
             "/home/alice/uv/tools/agrep/bin/python", "agrep[semantic]==0.2.0"),
        )
        self.assertEqual(
            dist.semantic_install_argv(
                installer="uv",
                executable="/workspace/.venv/bin/python", version="0.2.0"),
            ("uv", "pip", "install", "--python",
             "/workspace/.venv/bin/python", "agrep[semantic]==0.2.0"),
        )
        self.assertEqual(
            dist.semantic_install_argv(
                installer="pip",
                executable="/home/alice/pipx/venvs/agrep/bin/python",
                version="0.2.0"),
            ("/home/alice/pipx/venvs/agrep/bin/python", "-m", "pip", "install",
             "agrep[semantic]==0.2.0"),
        )

    def test_semantic_install_hint_fails_closed_for_windows_shell_meta(self) -> None:
        with mock.patch.object(dist, "WIN", True):
            self.assertIsNone(dist.semantic_install_command(
                installer="pip",
                executable=r"C:\Users\Alice&B\agrep\python.exe",
                version="0.2.0"))
            self.assertEqual(
                dist.semantic_install_hint(
                    installer="pip",
                    executable=r"C:\Users\Alice&B\agrep\python.exe",
                    version="0.2.0"),
                "install agrep[semantic] into this agrep environment "
                "from a shell-safe path",
            )
            rendered = dist.semantic_install_command(
                installer="uv",
                executable=r"C:\Users\Alice\agrep env\python.exe",
                version="0.2.0")
        self.assertEqual(
            rendered,
            'uv pip install --python "C:\\Users\\Alice\\agrep env\\python.exe" '
            '"agrep[semantic]==0.2.0"',
        )

    def test_registry_requires_exact_command_binding(self) -> None:
        rendered = surface.render_remedy(
            "source-read-incomplete",
            command="/fixture/agrep index")
        self.assertEqual(
            rendered,
            "retry `/fixture/agrep index` after the agent store is stable")
        with self.assertRaises(ValueError):
            surface.render_remedy("source-read-incomplete")
        with self.assertRaises(ValueError):
            surface.render_remedy(
                "store-unreadable", command="/fixture/agrep index")


class DoctorRemedyTests(unittest.TestCase):
    def test_unstable_store_retry_is_aggregated_after_all_named_stores(
            self) -> None:
        rows = [{
            "agent": agent,
            "found": 0,
            "state": "source-unreadable",
            "issues": [{
                "path": f"/fixture/{agent}",
                "kind": "source-read-incomplete",
            }],
        } for agent in ("antigravity", "cline", "kimi")]
        resolved = ("/fixture python", "/fixture repo/cli.py")
        with mock.patch.object(
                dist, "cli_invocation",
                side_effect=lambda *args: (*resolved, *args)):
            status, detail = doctor._store_status(rows)
        command = console.shell_command(*resolved, "index")
        self.assertEqual(status, doctor.MISS)
        self.assertEqual(detail.count(f"retry `{command}`"), 1)
        for agent in ("antigravity", "cline", "kimi"):
            self.assertIn(f"{agent} at /fixture/{agent}", detail)

    def test_structured_teach_drift_uses_the_registry_command(self) -> None:
        health = {
            "state": "refused",
            "refusals": [{
                "path": "/fixture/AGENTS.md",
                "kind": "drifted",
                "reason": "stale emitter prose must not render",
            }],
            "preserved_newer": [],
        }
        output = io.StringIO()
        with (
            mock.patch.object(
                dist, "cli_invocation",
                side_effect=lambda *args: ("/fixture/agrep", *args)),
            contextlib.redirect_stdout(output),
        ):
            doctor._report_teach_reconcile(health)
        rendered = output.getvalue()
        self.assertIn(
            "run `/fixture/agrep setup` to reconcile it safely", rendered)
        self.assertNotIn("stale emitter prose", rendered)

    def test_real_dev_remedy_argv_runs_from_outside_the_repo(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-remedy-cwd-") as raw:
            outside = Path(raw)
            child_env = {
                **os.environ,
                "AGREP_DATA_DIR": str(outside / "data"),
                "AGREP_DATA_READONLY": str(outside / "data"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            child_env.pop("AGREP_CLI_NAME", None)
            with mock.patch.dict(os.environ, child_env, clear=True):
                argv = dist.cli_invocation("doctor", "--help")
            result = subprocess.run(
                argv, cwd=outside, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=child_env,
                timeout=10.0,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agrep doctor", result.stdout)


class DoctorRowLanguageTests(unittest.TestCase):
    def test_report_color_follows_the_current_output_stream(self) -> None:
        snapshot = _report_snapshot()
        snapshot["_render"]["summary"]["per_agent"] = [{
            "agent": "codex", "messages": 1, "sessions": 1,
        }]
        output = io.StringIO()
        with (
            mock.patch.object(doctor, "probe", return_value=snapshot),
            mock.patch.object(
                doctor.common, "color_enabled",
                side_effect=lambda stream: stream.isatty(),
            ) as color_enabled,
            contextlib.redirect_stdout(output),
        ):
            doctor.report()
        self.assertGreater(color_enabled.call_count, 0)
        self.assertTrue(all(
            call.args == (output,) for call in color_enabled.call_args_list))
        self.assertNotIn("\x1b", output.getvalue())

    def test_permission_denied_renders_one_line_even_with_a_repair_queued(
            self) -> None:
        # Law 2's exact class: only the reader can fix a permission. The
        # torn generation beside it once swallowed and renamed this row;
        # suppression keys on the failure's OWN code, never on neighbors.
        snapshot = _report_snapshot()
        snapshot["_render"] = {
            **snapshot["_render"],
            "freshness_failures": 3,
            "indexing_failure": surface.FreshnessFailure(
                "consecutive-failures",
                "ingest exited 101: permission denied", 3),
            "generation": {"state": "torn-generation",
                           "detail": "generation commit is incomplete"},
        }
        rendered = _render_report(snapshot)
        freshness = [line for line in rendered.splitlines()
                     if "freshness" in line]
        self.assertEqual(len(freshness), 1)
        self.assertIn("daemon down after 3+ consecutive failures",
                      freshness[0])
        self.assertIn("permission denied", freshness[0])
        self.assertNotIn("torn-generation", rendered)

    def _unreadable_store_snapshot(self, **core: object) -> dict:
        snapshot = _report_snapshot()
        snapshot["core"] = {
            **snapshot["core"],
            "stores": [{
                "agent": "codex", "found": True,
                "state": "source-unreadable",
                "issues": [{"path": "/fixture/rollout.jsonl",
                            "reason": "read did not complete"}],
            }],
            **core,
        }
        snapshot["_render"] = {
            **snapshot["_render"],
            "indexing_failure": surface.FreshnessFailure(
                "source-unreadable",
                "/fixture/rollout.jsonl could not be read"),
        }
        return snapshot

    def test_an_unreadable_store_is_named_once_not_in_two_rows(self) -> None:
        # Law 5: the stores row carries the store and the path, so the
        # freshness row repeating it is one cause counted twice.
        rendered = _render_report(self._unreadable_store_snapshot())
        self.assertEqual(rendered.count("/fixture/rollout.jsonl"), 1)
        stores = [line for line in rendered.splitlines()
                  if "agent stores" in line]
        self.assertEqual(len(stores), 1)
        self.assertIn("store unreadable", stores[0])
        self.assertNotIn("could not be read", rendered)

    def test_the_cause_still_speaks_when_no_store_row_will_carry_it(
            self) -> None:
        # Suppressing under a row that stays silent would delete the reader's
        # only mention, so ownership requires a row that actually renders.
        for core in ({"binary": False},
                     {"store_observation": {"state": "unavailable"}}):
            with self.subTest(core=core):
                rendered = _render_report(
                    self._unreadable_store_snapshot(**core))
                self.assertIn("could not be read", rendered)

    def test_an_escalated_daemon_speaks_even_while_its_repair_is_queued(
            self) -> None:
        # Law 6's trigger state: the tool's own repair has failed repeatedly,
        # so silence here would be deleting the escalation, not law 3.
        snapshot = _report_snapshot()
        snapshot["_render"] = {
            **snapshot["_render"],
            "freshness_failures": 4,
            "indexing_failure": surface.FreshnessFailure(
                "inline-refresh-failed", "ingest exited 7: worker crashed", 4),
            "generation": {"state": "torn-generation", "detail": "incomplete"},
        }
        rendered = _render_report(snapshot)
        freshness = [line for line in rendered.splitlines()
                     if "freshness" in line]
        self.assertEqual(len(freshness), 1)
        self.assertIn("daemon down after 4+ consecutive failures",
                      freshness[0])

    def test_a_pristine_box_is_not_alarmed_about_its_first_capture(
            self) -> None:
        # Laws 1/3: seconds after setup the first scheduled pass simply has
        # not run yet - agrep's own task, never a [!!] handed to the reader.
        rendered = _render_report(_report_snapshot(archive={
            "state": "freshness-unknown",
            "detail": "no capture pass has completed",
            "last_pass": {"age_s": None, "outcome": "never", "fresh": False},
        }))
        line = next(l for l in rendered.splitlines() if "archive" in l)
        self.assertIn("[-- ]", line)
        self.assertIn("first capture pass is pending", line)
        self.assertNotIn("no capture pass has completed", rendered)

    def test_unenrolled_instructions_name_setup_once(self) -> None:
        rendered = _render_report(_report_snapshot())
        command = doctor._cli_command("setup")
        self.assertEqual(rendered.count(command), 1)
        self.assertNotIn("to unlock more", rendered)

    def test_corrupt_embeddings_warn_plainly_with_registry_remedy(self) -> None:
        semantic = _semantic_state(
            live=False,
            embeddings="corrupt-embeddings",
            embedding_integrity={
                "state": "corrupt", "verified": False,
                "reason": "fixture index failure",
            },
        )
        rendered = _render_report(
            _report_snapshot(semantic=semantic), deep=True)
        self.assertIn("[!! ] embeddings", rendered)
        self.assertIn(
            "semantic embeddings are corrupt: fixture index failure", rendered)
        self.assertIn(
            f"run `{doctor._cli_command('reindex')}`", rendered)
        self.assertNotIn("  corrupt-embeddings", rendered)

    def test_a_tier_that_ran_nothing_rules_no_lines(self) -> None:
        snapshot = _report_snapshot(
            core={
                **_report_snapshot()["core"],
                "indexed": {"state": "status-deferred"},
                "search_db": {
                    "state": "not-verified",
                    "integrity": doctor._integrity_not_verified(),
                },
                "store_observation": {"state": "budget-exceeded"},
            },
            archive={"state": "status-deferred"},
            model_attribution={"state": "status-deferred"},
            teach_reconcile={
                "state": "status-deferred", "deferred_kind": "routine-tier",
                "refusals": [], "preserved_newer": [],
            },
            teach_enrollment={"state": "status-deferred", "targets": None},
            semantic=_semantic_state(
                live=False, available=None, runtime_verified=False,
                runtime_state="not-inspected", model_cached=False,
                embeddings="not-verified", embed_job="not-inspected"),
            resources={
                "data": {
                    "state": "status-deferred", "complete": False,
                    "files": None, "bytes": None,
                    "detail": "stale alternate wording",
                },
                "orphans": {
                    "state": "status-deferred", "complete": False,
                    "count": None, "bytes": None,
                    "detail": "different stale wording",
                },
                "indexd": {"state": "status-deferred", "running": False},
            },
            diagnostics={"deferred": ["fixture"]},
            _render={
                **_report_snapshot()["_render"],
                "summary": {"state": "status-deferred"},
            },
        )
        rendered = _render_report(snapshot)
        # every check above declined to run, so every row it could have earned
        # is absent - no phrasing, no footer, no count of what was skipped
        for label in (
                "footprint", "orphan artifacts", "corpus", "search db",
                "integrity", "archive", "model attribution", "agent stores",
                "instructions", "model", "embeddings"):
            self.assertNotIn(label, rendered)
        self.assertIn("runtime build", rendered)
        self.assertNotIn("semantic search - optional", rendered)
        self.assertNotIn("doctor --deep", rendered)
        self.assertNotIn("deferred", rendered)
        self.assertNotIn("stale alternate wording", rendered)
        self.assertNotIn("different stale wording", rendered)

    def test_rows_do_not_repeat_labels_or_structured_state_names(self) -> None:
        snapshot = _report_snapshot(
            core={
                **_report_snapshot()["core"],
                "search_db": {
                    "state": "ready", "detail": "current",
                    "integrity": doctor._integrity_not_verified(),
                },
            },
            archive={"state": "disabled"},
            model_attribution={"state": "unavailable", "reason": "stale"},
        )
        rendered = _render_report(snapshot)
        # the row that restated its own label is gone with the check it named
        self.assertNotIn("integrity", rendered)
        self.assertIn("archive           automatic capture is disabled", rendered)
        self.assertNotIn("disabled -", rendered)
        # law 5: attribution has no failure of its own - every reason it
        # could give restates the corpus or search-db row above it
        self.assertNotIn("model attribution", rendered)
        self.assertNotIn("unavailable -", rendered)

    def test_cached_model_says_cached_once(self) -> None:
        rendered = _render_report(_report_snapshot(), deep=True)
        model_line = next(
            line for line in rendered.splitlines()
            if "] model             " in line)
        self.assertEqual(model_line.lower().count("cached"), 1)

    def test_indirect_doctor_remedies_use_the_same_resolver(self) -> None:
        snapshot = _report_snapshot(
            freshness={
                "state": "index-behind", "behind_s": 2.0,
                "changed_stores": 1,
            },
            settings={
                "embeddings": {"state": "verified", "value": "off"}},
            semantic=_semantic_state(
                live=False, embeddings="legacy-publication"),
        )
        rendered = _render_report(snapshot, deep=True)
        self.assertIn(doctor._cli_command("index"), rendered)
        self.assertNotIn("`agrep index`", rendered)
        self.assertNotIn("  legacy-publication", rendered)
        self.assertIn(
            "semantic embeddings use a legacy publication", rendered)

    def test_unavailable_attribution_renders_no_row_at_all(self) -> None:
        # The reason was always a copy of another row's state. Suppressed
        # under its cause, the percentage returns when that row does.
        rendered = _render_report(_report_snapshot(
            model_attribution={
                "state": "unavailable", "reason": "unavailable"}))
        self.assertNotIn("model attribution", rendered)
        self.assertNotIn("not available because", rendered)

    def test_uninspected_rss_leaves_a_complete_daemon_row(self) -> None:
        snapshot = _report_snapshot(
            resources={
                **_report_snapshot()["resources"],
                "indexd": {
                    "state": "running", "running": True, "pid": 42,
                    "rss_state": "not-inspected", "rss_bytes": None,
                },
            },
            diagnostics={"deferred": ["daemon RSS"]},
        )
        rendered = _render_report(snapshot)
        # an unmeasured number is dropped, not announced: the row still says
        # everything it observed, and nothing about what it did not
        self.assertIn("daemon running - pid 42", rendered)
        self.assertNotIn("RSS", rendered)
        self.assertNotIn("doctor --deep", rendered)

    def test_clobber_recovery_owns_one_resolved_verification_step(self) -> None:
        database = Path("/fixture/data/corpus.db")
        search_db = {
            "state": "post-adoption-clobber",
            "detail": "an older writer returned",
            "remedy": doctor._post_adoption_clobber_remedy(database),
            "integrity": doctor._integrity_not_verified(),
        }
        snapshot = _report_snapshot(
            core={**_report_snapshot()["core"], "search_db": search_db},
            diagnostics={"deferred": ["fixture"]},
        )
        for deep in (False, True):
            with self.subTest(deep=deep):
                rendered = _render_report(snapshot, deep=deep)
                self.assertIn("recovery remedy:", rendered)
                self.assertIn("retain the backup until", rendered)
                self.assertIn(
                    doctor._cli_command("doctor", "--deep"), rendered)
                self.assertEqual(rendered.count("doctor --deep"), 1)


if __name__ == "__main__":
    unittest.main()
