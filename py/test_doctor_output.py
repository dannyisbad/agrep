"""Doctor/status output contracts: replayable remedies and concise rows."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from _test_support import (
    fixture_tree, generous_routine_budget, isolate_data_dir, kill_mid_write)


isolate_data_dir()
import console  # noqa: E402
import corpusdb  # noqa: E402
import dist  # noqa: E402
import doctor  # noqa: E402
import index_lock  # noqa: E402

setUpModule, tearDownModule = generous_routine_budget()
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
            os.path.abspath("/fixture python"),
            os.path.abspath(os.fspath(root / "cli.py")), "index"))
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
                (os.path.abspath("/fixture shim/agrep"), "setup"),
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
                executable="/Users/runner/uv/tools/agrep/bin/python",
                version="0.2.0"),
            ("uv", "pip", "install", "--python",
             "/Users/runner/uv/tools/agrep/bin/python", "agrep[semantic]==0.2.0"),
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
                executable="/Users/runner/pipx/venvs/agrep/bin/python",
                version="0.2.0"),
            ("/Users/runner/pipx/venvs/agrep/bin/python", "-m", "pip", "install",
             "agrep[semantic]==0.2.0"),
        )

    def test_semantic_install_hint_fails_closed_for_windows_shell_meta(self) -> None:
        with mock.patch.object(dist, "WIN", True):
            self.assertIsNone(dist.semantic_install_command(
                installer="pip",
                executable=r"/Users/runner&B\agrep\python.exe",
                version="0.2.0"))
            self.assertEqual(
                dist.semantic_install_hint(
                    installer="pip",
                    executable=r"/Users/runner&B\agrep\python.exe",
                    version="0.2.0"),
                "install agrep[semantic] into this agrep environment "
                "from a shell-safe path",
            )
            rendered = dist.semantic_install_command(
                installer="uv",
                executable=r"/Users/runner\agrep env\python.exe",
                version="0.2.0")
        self.assertEqual(
            rendered,
            'uv pip install --python "/Users/runner\\agrep env\\python.exe" '
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
        with fixture_tree(
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

    def test_embedding_lane_row_is_informational_unless_the_lane_mismatches(
            self) -> None:
        # A healthy box carried a permanent `[!! ]` for an informational fact;
        # agents reading status treat `!!` as action-needed.
        snapshot = _report_snapshot()
        with mock.patch.object(
                doctor, "_store_lane_notice",
                return_value=(doctor.OPT, "store built on the cpu lane")):
            rendered = _render_report(snapshot)
        lane_rows = [line for line in rendered.splitlines()
                     if "embedding lane" in line]
        self.assertEqual(len(lane_rows), 1)
        self.assertIn("[-- ] embedding lane", lane_rows[0])

        with mock.patch.object(
                doctor, "_store_lane_notice",
                return_value=(doctor.WARN, "store built on the metal lane, "
                              "which does not open here (no)")):
            rendered = _render_report(snapshot)
        self.assertIn("[!! ] embedding lane", rendered)

    def test_a_newer_anchor_is_named_once_with_its_remedy(self) -> None:
        # [W16 round 2] the fence a reader can end was invisible to the human
        # report: the corpus row said "`agrep index` runs a publication now",
        # freshness "another agrep's claim on the index cannot be verified"
        reason = ("the data dir was written by a newer agrep (ownership record "
                  "version 2, format 1); this agrep serves it read-only; "
                  "upgrade, or point AGREP_DATA_DIR elsewhere")
        owner = {"state": "newer-agrep", "version": 2, "format": 1,
                 "detail": reason}
        snapshot = _report_snapshot()
        snapshot["core"]["derived_owner"] = owner
        snapshot["core"]["indexed"] = {"state": "never-built"}
        kick = doctor.indexd_runtime.RepairKick(False, "newer-anchor", reason)
        with mock.patch.object(
                doctor.indexd_runtime, "kick_background_repair",
                return_value=kick):
            rendered = _render_report(snapshot)
        corpus = [line for line in rendered.splitlines() if "corpus" in line]
        self.assertEqual(len(corpus), 1)
        self.assertIn("none yet - this agrep cannot build it (see freshness)",
                      corpus[0])
        self.assertNotIn("runs a publication now", rendered)
        self.assertIn(
            f"[!! ] freshness         daemon not running - {reason}", rendered)
        self.assertNotIn("cannot be verified", rendered)
        self.assertEqual(rendered.count("written by a newer agrep"), 1)

        # the same fence over a built corpus: the census stays, freshness
        # carries the cause
        snapshot["core"]["indexed"] = {"state": "ready"}
        snapshot["core"]["search_db"] = {
            "state": "owned-elsewhere", "detail": reason}
        with mock.patch.object(
                doctor.indexd_runtime, "kick_background_repair",
                return_value=kick):
            rendered = _render_report(snapshot)
        self.assertIn(
            f"[!! ] freshness         daemon not running - {reason}", rendered)
        self.assertEqual(rendered.count("written by a newer agrep"), 1)

    def test_the_newer_anchor_field_is_absent_unless_the_anchor_is_newer(
            self) -> None:
        with mock.patch.object(
                doctor.indexd_runtime, "newer_anchor", return_value=None):
            self.assertEqual(doctor._derived_owner_fields(), {})
        with mock.patch.object(
                doctor.indexd_runtime, "newer_anchor", return_value=(3, None)):
            fields = doctor._derived_owner_fields()
        self.assertEqual(fields["derived_owner"]["state"], "newer-agrep")
        self.assertEqual(fields["derived_owner"]["version"], 3)
        self.assertIsNone(fields["derived_owner"]["format"])
        self.assertEqual(
            fields["derived_owner"]["detail"],
            doctor.indexd_runtime.newer_anchor_reason(3, None))

    def test_deep_no_semantic_keeps_the_semantic_tier_at_routine_depth(
            self) -> None:
        snapshot = _report_snapshot()
        with (
            mock.patch.object(doctor, "probe", return_value=snapshot) as probe,
            mock.patch.object(doctor, "_deep_notice") as notice,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(doctor.main(["--deep", "--no-semantic"]), 0)
        self.assertEqual(probe.call_args.kwargs["deep"], True)
        self.assertEqual(probe.call_args.kwargs["semantic"], False)
        notice.assert_called_once_with(semantic=False)
        with mock.patch.object(doctor, "_semantic_probe") as semantic_probe, \
                mock.patch.object(doctor.indexd_runtime, "arm_store_census"), \
                mock.patch.object(doctor, "_orphan_inventory",
                                  side_effect=RuntimeError("stop here")):
            semantic_probe.return_value = {}
            with self.assertRaises(RuntimeError):
                doctor.probe(deep=True, semantic=False)
        semantic_probe.assert_called_once_with(deep=False, fix=False)

    def test_deep_notice_names_the_skipped_semantic_runtime(self) -> None:
        output = io.StringIO()
        doctor._deep_notice(stream=output, semantic=False)
        self.assertIn("deep diagnostics: verifying SQLite integrity",
                      output.getvalue())
        self.assertIn("--no-semantic", output.getvalue())
        plain = io.StringIO()
        doctor._deep_notice(stream=plain)
        self.assertNotIn("--no-semantic", plain.getvalue())

    def test_installed_build_names_the_binary_date_and_the_checkout_hook(
            self) -> None:
        snapshot = _report_snapshot(install_lag={
            "state": "unavailable",
            "detail": "installed package has no unique local-source provenance",
            "reason": "no-local-source-provenance",
        })
        with fixture_tree() as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"")
            os.utime(binary, (1_756_166_400, 1_756_166_400))
            with mock.patch.object(doctor, "INGEST_BIN", binary):
                rendered = _render_report(snapshot)
        row = next(line for line in rendered.splitlines()
                   if "installed build" in line)
        self.assertIn("[-- ]", row)
        self.assertIn("(a wheel install); native binary dated 2025-08-26", row)
        self.assertIn("AGREP_SOURCE_DIR=<checkout>", row)

    def test_lagging_install_lists_checkout_changes_beside_the_remedy(self) -> None:
        with fixture_tree() as td:
            source = Path(td) / "checkout"
            source.mkdir()
            (source / "CHANGELOG.md").write_text(
                "# Changelog\n\n## Unreleased\n\n"
                "- Semantic search chunks long rows.\n"
                "- Advisor sidecars index their own voice.\n\n"
                "## 0.3.1 — 2026-08-26\n\n- Released change.\n",
                encoding="utf-8")
            with mock.patch.object(
                    doctor.install_lag, "_local_distribution_ids",
                    return_value=("installed", "checkout", None)):
                observation = doctor.install_lag._content_comparison(
                    Path(td) / "installed" / "py" / "install_lag.py", source,
                    deadline=None, source_label="the AGREP_SOURCE_DIR checkout")
            rendered = _render_report(_report_snapshot(install_lag=observation))
        row = next(line for line in rendered.splitlines()
                   if "] installed build" in line)
        self.assertIn("differs from the local checkout", row)
        self.assertIn("2 unreleased changes since 0.3.1 — 2026-08-26", row)
        self.assertIn('newest: "Semantic search chunks long rows."', row)
        command = console.shell_command(
            "uv", "tool", "install", "--force", "--from", str(source), "agrep")
        self.assertIn(
            surface.render_remedy("replace-installed-tool", command=command), row)

    def test_a_checkout_renders_its_unreleased_tail_as_one_row(self) -> None:
        snapshot = _report_snapshot(install_lag={
            "state": "not-installed",
            "detail": "running from a source checkout; the checkout lists "
                      "3 unreleased changes",
            "source": "/fixture/checkout",
            "unreleased": {"count": 3, "newest": "Semantic search chunks "
                           "long rows.", "since": "0.3.1 — 2026-08-26"},
        })
        rendered = _render_report(snapshot)
        self.assertNotIn("] installed build", rendered)
        row = next(line for line in rendered.splitlines()
                   if "] source checkout" in line)
        self.assertIn("[-- ]", row)
        self.assertIn("3 unreleased changes in CHANGELOG.md since 0.3.1 — "
                      '2026-08-26; newest: "Semantic search chunks long rows."',
                      row)
        plain = _render_report(_report_snapshot())
        self.assertNotIn("] source checkout", plain)

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
        # every check above declined to run, so every row it could have earned is
        # absent - no phrasing, no footer, no count. Rows are matched by their
        # `[..] label` shape: the runtime-build line embeds the checkout path.
        for label in (
                "footprint", "orphan artifacts", "corpus", "search db",
                "integrity", "archive", "model attribution", "agent stores",
                "instructions", "model", "embeddings"):
            self.assertEqual(self._rows(rendered, label), [], label)
        self.assertIn("runtime build", rendered)
        self.assertNotIn("semantic search - optional", rendered)
        self.assertNotIn("doctor --deep", rendered)
        self.assertNotIn("deferred", rendered)
        self.assertNotIn("stale alternate wording", rendered)
        self.assertNotIn("different stale wording", rendered)

    @staticmethod
    def _rows(rendered: str, label: str) -> list[str]:
        return [line for line in rendered.splitlines()
                if f"] {label}" in line]

    @staticmethod
    def _stale_semantic(**updates: object) -> dict:
        readiness = {
            "state": "generation-not-ready", "ready": False,
            "query_serving": False,
            "reason": "the current semantic generation is not query-ready",
            "generation_state": "stale", "current_generation": False,
            "complete": False, "coverage": None,
            "refresh": {"state": "idle", "running": False,
                        "phase": None, "done": None, "total": None},
        }
        state = _semantic_state(
            live=False, generation_ready=False, embeddings="stale",
            query_readiness=readiness)
        state.update(updates)
        return state

    def _render_under(self, snapshot: dict, *, memory: float, load: float,
                      battery: tuple = (False, None)) -> str:
        with mock.patch.multiple(
                doctor.common,
                available_memory_fraction=mock.Mock(return_value=memory),
                battery_state=mock.Mock(return_value=battery),
                host_cpu_fraction=mock.Mock(return_value=load)):
            return _render_report(snapshot, deep=True)

    def test_a_stale_generation_under_load_alarms_nothing_and_promises_repair(
            self) -> None:
        # The owner's upgrade morning: embeddings stale under heavy load, the
        # worker waiting on them - all self-healing, so every row is [--], and
        # the row says what happens next: the lane repairs through the load.
        for state in ("stale", "missing-embeddings"):
            with self.subTest(state=state):
                snapshot = _report_snapshot(
                    semantic=self._stale_semantic(embeddings=state))
                rendered = self._render_under(
                    snapshot, memory=0.6, load=27.4, battery=(True, 10))
                section = rendered.split("semantic search - optional", 1)[1]
                self.assertNotIn("[!! ]", section)
                row = self._rows(rendered, "embeddings")[0]
                self.assertIn("[-- ]", row)
                self.assertNotIn("paused", row)
                self.assertIn("first semantic search", row)
                self.assertIn(doctor._cli_command("doctor", "--fix"), row)
                self.assertIn("[-- ]", self._rows(rendered, "query readiness")[0])
                self.assertIn("[-- ]", self._rows(rendered, "semantic worker")[0])

    def test_memory_pressure_still_pauses_an_unsearchable_lane(self) -> None:
        rendered = self._render_under(
            _report_snapshot(semantic=self._stale_semantic()),
            memory=0.05, load=0.1)
        row = self._rows(rendered, "embeddings")[0]
        self.assertIn("paused", row)
        self.assertIn("memory pressure", row)

    def test_a_searchable_lane_catch_up_still_reports_the_load_pause(
            self) -> None:
        partial = _semantic_state(
            live=True, generation_ready=True, embeddings="partial",
            embedding_coverage={"indexed": 128, "total": 400,
                                "complete": False})
        rendered = self._render_under(
            _report_snapshot(semantic=partial), memory=0.6, load=27.4)
        row = self._rows(rendered, "embeddings")[0]
        self.assertIn("searchable now", row)
        self.assertIn("paused", row)
        self.assertIn("CPU load", row)

    def test_a_stale_generation_with_a_running_build_alarms_nothing(self) -> None:
        snapshot = _report_snapshot(semantic=self._stale_semantic(
            embed_running=True, embed_job="running", embed_pid=42))
        rendered = _render_report(snapshot, deep=True)
        semantic_section = rendered.split("semantic search - optional", 1)[1]
        self.assertNotIn("[!! ]", semantic_section)
        self.assertIn("rebuilding", self._rows(rendered, "embeddings")[0])
        self.assertIn("[-- ]", self._rows(rendered, "semantic worker")[0])

    def test_a_corrupt_generation_still_alarms_the_worker_row(self) -> None:
        corrupt = self._stale_semantic(
            embeddings="corrupt-embeddings",
            embedding_integrity={
                "state": "corrupt", "verified": False,
                "reason": "fixture index failure",
            })
        corrupt["query_readiness"] = {
            **corrupt["query_readiness"],
            "generation_state": "corrupt-embeddings"}
        rendered = _render_report(
            _report_snapshot(semantic=corrupt), deep=True)
        self.assertIn("[!! ]", self._rows(rendered, "embeddings")[0])
        self.assertIn("[!! ]", self._rows(rendered, "semantic worker")[0])

    def test_generations_that_need_a_human_keep_the_worker_alarm(self) -> None:
        # legacy-publication waits on `agrep index`; unknown/empty is a probe
        # that crashed. Neither heals in the background, so the worker row
        # stays [!!] exactly as before the self-healing states went quiet.
        for state in ("legacy-publication", "unknown", ""):
            with self.subTest(state=state):
                stuck = self._stale_semantic(embeddings=state or "unknown")
                stuck["query_readiness"] = {
                    **stuck["query_readiness"], "generation_state": state}
                rendered = _render_report(
                    _report_snapshot(semantic=stuck), deep=True)
                self.assertIn(
                    "[!! ]", self._rows(rendered, "semantic worker")[0])

    def test_self_healing_generations_are_exactly_the_refresh_repairs(
            self) -> None:
        for state in ("stale", "missing-embeddings", "missing-source",
                      "unstable-source", "unstable-embeddings",
                      "profile-mismatch", "profile-unavailable",
                      "legacy-embeddings", "partial", "current"):
            self.assertTrue(
                surface.semantic_generation_self_heals(state), state)
        for state in ("corrupt-embeddings", "legacy-publication", "unknown",
                      "", None):
            self.assertFalse(
                surface.semantic_generation_self_heals(state), state)

    def test_a_worker_that_cannot_query_a_ready_generation_still_alarms(
            self) -> None:
        broken = _semantic_state(
            live=False, generation_ready=True, embeddings="current",
            resident_worker={"running": True, "pid": 42},
            query_readiness={
                "state": "query-failed", "ready": True,
                "query_serving": False,
                "reason": "resident semantic worker disconnected during the query",
                "generation_state": "current", "current_generation": True,
                "complete": True, "coverage": None,
                "refresh": {"state": "idle", "running": False},
            })
        rendered = _render_report(
            _report_snapshot(semantic=broken), deep=True)
        self.assertIn("[!! ]", self._rows(rendered, "query readiness")[0])
        self.assertIn("[!! ]", self._rows(rendered, "semantic worker")[0])

    def test_a_generation_that_moved_under_the_probe_is_unverified_not_broken(
            self) -> None:
        moved = self._stale_semantic(
            resident_worker={"running": True, "pid": 42},
            embed_running=True, embed_job="running", embed_pid=43)
        moved["query_readiness"] = {
            **moved["query_readiness"],
            "state": "generation-moved", "query_serving": None,
            "reason": "the semantic generation moved during the probe",
            "refresh": {"state": "running", "running": True,
                        "phase": "refs", "done": None, "total": None},
        }
        rendered = _render_report(
            _report_snapshot(semantic=moved), deep=True)
        semantic_section = rendered.split("semantic search - optional", 1)[1]
        self.assertNotIn("[!! ]", semantic_section)
        readiness = self._rows(rendered, "query readiness")[0]
        self.assertIn("[-- ]", readiness)
        self.assertIn("moved", readiness)
        self.assertIn("stale", readiness)
        self.assertIn("[-- ]", self._rows(rendered, "semantic worker")[0])

    def _unverified_freshness_snapshot(
            self, code: str, daemon: dict, **render: object) -> dict:
        snapshot = _report_snapshot(resources={
            **_report_snapshot()["resources"], "indexd": daemon})
        snapshot["freshness"] = {
            "state": "unchecked", "failing": False, "may_be_stale": True,
            "checked": False, "code": code,
        }
        snapshot["_render"] = {
            **snapshot["_render"],
            "indexing_failure": surface.FreshnessFailure(
                code, "the live store census exceeded its diagnostic time budget"),
            **render,
        }
        return snapshot

    def test_an_unverifiable_census_renders_freshness_neutrally(self) -> None:
        # A diagnostic budget that ran out under load is not an indexing
        # failure: nothing is known to be wrong, so the row says so and
        # stops promising a last-good index.
        running = {"state": "running", "running": True, "pid": 7,
                   "rss_bytes": 10 * 1024 ** 2}
        starting = {"state": "starting", "starting": True, "age_s": 1.5}
        for code in ("diagnostic-budget-exceeded", "census-unavailable"):
            for daemon in (running, starting):
                with self.subTest(code=code, daemon=daemon["state"]):
                    rendered = _render_report(
                        self._unverified_freshness_snapshot(code, daemon))
                    row = self._rows(rendered, "freshness")[0]
                    self.assertIn("[-- ]", row)
                    self.assertIn("not verified", row)
                    self.assertIn("diagnostic time budget", row)
                    self.assertNotIn(
                        surface.FRESHNESS_POLICY.last_good_index_tail, row)
                    self.assertNotIn("DEGRADED", row)

    def test_a_real_failure_still_alarms_beside_an_unverified_census(
            self) -> None:
        # indexing_failure ranks a recorded failure above a census that could
        # not vouch; the row must keep alarming on the failure it did record.
        snapshot = _report_snapshot(core={
            **_report_snapshot()["core"],
            "store_observation": {"state": "budget-exceeded"},
        })
        snapshot["resources"]["indexd"] = {
            "state": "running", "running": True, "pid": 7}
        snapshot["_render"] = {
            **snapshot["_render"],
            "freshness_failures": 3,
            "indexing_failure": surface.FreshnessFailure(
                "consecutive-failures",
                "ingest exited 101: permission denied", 3),
        }
        row = self._rows(_render_report(snapshot), "freshness")[0]
        self.assertIn("[!! ]", row)
        self.assertIn("permission denied", row)
        self.assertIn(surface.FRESHNESS_POLICY.last_good_index_tail, row)

    def test_machine_freshness_reports_an_unverifiable_census_as_unchecked(
            self) -> None:
        reason = "the live store census exceeded its diagnostic time budget"
        for code in ("diagnostic-budget-exceeded", "census-unavailable"):
            with self.subTest(code=code):
                shared = surface.freshness_disclosure(
                    surface.FreshnessFailure(code, reason))
                fields = doctor._machine_freshness_fields(
                    shared, {"state": "ready", "corpus_age_s": 3.0})
                freshness = fields["freshness"]
                self.assertEqual(freshness, shared)
                self.assertFalse(freshness["failing"])
                self.assertEqual(freshness["state"], "unchecked")
                self.assertTrue(freshness["may_be_stale"])
                self.assertFalse(freshness["checked"])
                self.assertEqual(freshness["code"], code)
        torn = doctor._machine_freshness_fields(
            surface.freshness_disclosure(surface.FreshnessFailure(
                "diagnostic-budget-exceeded", "budget")),
            {"state": "torn-generation", "detail": "incomplete"})
        self.assertTrue(torn["freshness"]["failing"])
        self.assertEqual(torn["freshness"]["code"], "torn-generation")

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
        self.assertEqual(self._rows(rendered, "integrity"), [])
        self.assertIn("archive           automatic capture is disabled", rendered)
        self.assertNotIn("disabled -", rendered)
        # law 5: attribution has no failure of its own - every reason it
        # could give restates the corpus or search-db row above it
        self.assertEqual(self._rows(rendered, "model attribution"), [])
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

    def test_archive_row_names_sources_still_waiting_for_capture(self) -> None:
        waiting = {"count": 2, "kinds": ["rewrite"], "by_kind": {"rewrite": 2},
                   "oldest_age_s": 90.0,
                   "oldest_path": "/fixture/home/.codex/sessions/rollout-big.jsonl"}
        rendered = _render_report(_report_snapshot(archive={
            "state": "healthy", "detail": "capture is healthy",
            "last_pass": {"age_s": 1.0, "fresh": True}, "waiting": waiting}))
        rows = self._rows(rendered, "archive")
        self.assertEqual(len(rows), 1, rendered)
        self.assertIn("[ok ]", rows[0])
        self.assertIn("2 source(s) waiting for capture", rows[0])
        rendered = _render_report(_report_snapshot(archive={
            "state": "busy", "detail": "capture running (pid 4242, since 12s)",
            "last_pass": {"age_s": 1.0, "fresh": True}}))
        rows = self._rows(rendered, "archive")
        self.assertEqual(len(rows), 1, rendered)
        self.assertIn("[-- ]", rows[0])
        self.assertIn("capture running (pid 4242, since 12s)", rows[0])
        for state, detail in (
                ("stalled", "capture stalled (pid 4242, running 20m, no progress for 16m); "
                            "the daemon stops it, or stop it yourself"),
                ("disk-full", "2 source(s) not captured: too little free space on the "
                              "volume holding /fixture/data/archive for a point-in-time "
                              "copy; free space there (or move the data dir) and the "
                              "next pass captures them")):
            rendered = _render_report(_report_snapshot(archive={
                "state": state, "detail": detail,
                "last_pass": {"age_s": 1.0, "fresh": True}}))
            rows = self._rows(rendered, "archive")
            self.assertEqual(len(rows), 1, rendered)
            self.assertIn("[!! ]", rows[0], state)
            self.assertIn(detail, rows[0])

    def test_unavailable_attribution_renders_no_row_at_all(self) -> None:
        # The reason was always a copy of another row's state. Suppressed
        # under its cause, the percentage returns when that row does.
        rendered = _render_report(_report_snapshot(
            model_attribution={
                "state": "unavailable", "reason": "unavailable"}))
        self.assertEqual(self._rows(rendered, "model attribution"), [])
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

    def test_young_converging_churn_renders_as_the_system_working(self) -> None:
        # Live sessions write on every turn, so a converging daemon is behind
        # by one publish essentially forever; that steady state must not read
        # as a fault or name a manual remedy.
        snapshot = _report_snapshot(
            resources={
                **_report_snapshot()["resources"],
                "indexd": {
                    "state": "running", "running": True, "pid": 42,
                    "rss_state": "not-inspected", "rss_bytes": None,
                },
            },
            freshness={
                "state": "index-behind", "changed_stores": 2,
                "young": True, "converging": True,
            },
        )
        rendered = _render_report(snapshot)
        line = next(
            line for line in rendered.splitlines() if "freshness" in line)
        self.assertIn("[ok ]", line)
        self.assertIn("absorbing live-session churn (2 stores still writing", line)
        self.assertNotIn("index behind", rendered)
        self.assertNotIn(doctor._cli_command("index"), rendered)

    def test_aged_drift_with_a_running_daemon_still_names_the_remedy(self) -> None:
        snapshot = _report_snapshot(
            resources={
                **_report_snapshot()["resources"],
                "indexd": {
                    "state": "running", "running": True, "pid": 42,
                    "rss_state": "not-inspected", "rss_bytes": None,
                },
            },
            freshness={
                "state": "index-behind", "behind_s": 2520.0,
                "changed_stores": 1,
            },
        )
        rendered = _render_report(snapshot)
        line = next(
            line for line in rendered.splitlines() if "freshness" in line)
        self.assertIn("[-- ]", line)
        self.assertIn("index 42m behind (1 store changed)", line)
        self.assertIn(doctor._cli_command("index"), line)

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


class DoctorCensusContradictionTests(unittest.TestCase):
    """The corpus row may never stay green across a census/cache contradiction.

    The live incident's face: `[ok ] corpus 1 messages · 1 sessions` printed
    beside an 853-source parse cache after a poisoned publication clobbered
    the census. The contradiction outranks the green row; an agreeing census
    keeps today's rendering byte for byte.
    """

    def _contradicted_snapshot(self) -> dict:
        snapshot = _report_snapshot()
        snapshot["core"]["indexed"] = {
            "state": "ready", "sessions": 1, "messages": 1, "age_s": 0,
            "census_contradiction": True, "parse_cache_sources": 853,
        }
        return snapshot

    def test_a_clobbered_corpus_breaks_the_green_corpus_row(self) -> None:
        rendered = _render_report(self._contradicted_snapshot())
        line = next(row for row in rendered.splitlines() if "] corpus" in row)
        self.assertIn("[!! ]", line)
        self.assertIn(
            "corpus census (1 sessions) contradicts parse cache "
            "(853 sources)", line)
        self.assertIn("the published snapshot lost most of the corpus", line)
        self.assertIn("run agrep reindex --full", line)
        self.assertNotIn("[ok ] corpus", rendered)

    def test_an_agreeing_census_keeps_todays_row_unchanged(self) -> None:
        rendered = _render_report(_report_snapshot())
        line = next(row for row in rendered.splitlines() if "] corpus" in row)
        self.assertIn("[ok ]", line)
        self.assertIn("1 messages · 1 sessions", line)
        self.assertNotIn("contradicts", rendered)
        self.assertNotIn("reindex --full", rendered)


class DoctorDisplacedSidecarTests(unittest.TestCase):
    """A directory the ingest moved out of a parse cache sidecar's place (journal
    or head) holds whatever someone put there: doctor names each with its kind
    and size and tells the user to inspect, then delete. It never deletes, and
    a quiet data dir prints no such row.
    """

    def test_the_census_names_displaced_directories_with_their_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            displaced = data / ".ingest_cache.bin.journal.displaced-1791480000000000000"
            (displaced / "nested").mkdir(parents=True)
            (displaced / "keep.txt").write_bytes(b"x" * 1024)
            (displaced / "nested" / "more.bin").write_bytes(b"y" * 2048)
            # the live journal and an unrelated sibling are not displaced directories
            (data / ".ingest_cache.bin.journal").write_bytes(b"frames")
            (data / ".ingest_cache.bin.journal.displaced-notanumber").mkdir()
            with mock.patch.object(doctor.common, "DATA_DIR", data):
                census = doctor._displaced_journals()
            self.assertTrue(census["complete"])
            self.assertEqual(
                census["paths"],
                ({"path": str(displaced), "bytes": 3072},))
            self.assertTrue(displaced.is_dir(), "the census deleted the directory")
            line = doctor.displaced_sidecar_line(census)
            self.assertIn(f"{displaced} (journal, 0.0 MiB)", line)
            self.assertIn("1 directory stood where a parse cache sidecar belongs and was "
                          "moved aside, not deleted", line)
            self.assertIn("inspect, then delete", line)
            # a displaced head sidecar is census'd the same way
            head = data / ".ingest_cache.bin.head.displaced-1791480000000000001"
            head.mkdir()
            with mock.patch.object(doctor.common, "DATA_DIR", data):
                both = doctor._displaced_journals()
            self.assertEqual([item["path"] for item in both["paths"]],
                             [str(head), str(displaced)])
            with mock.patch.object(doctor.common, "DATA_DIR", data / "absent"):
                self.assertFalse(doctor._displaced_journals()["complete"])

    def test_the_report_warns_once_per_displaced_directory_and_stays_quiet_otherwise(
            self) -> None:
        snapshot = _report_snapshot()
        snapshot["resources"]["displaced_journals"] = {
            "state": "complete", "complete": True,
            "paths": (
                {"path": "/fixture/data/.ingest_cache.bin.head.displaced-1",
                 "bytes": 5 * 1024 * 1024},
                {"path": "/fixture/data/.ingest_cache.bin.journal.displaced-2",
                 "bytes": None},
            ),
        }
        rendered = _render_report(snapshot)
        line = next(row for row in rendered.splitlines() if "displaced cache sidecar" in row)
        self.assertIn("[!! ]", line)
        self.assertIn("2 directories stood where a parse cache sidecar belongs and were "
                      "moved aside", line)
        self.assertIn(".head.displaced-1 (head, 5.0 MiB)", line)
        self.assertIn(".journal.displaced-2 (journal, size unknown)", line)
        self.assertIn("inspect, then delete", line)
        self.assertNotIn("displaced journal", rendered)
        # a head alone is labelled by what it is, never as a journal
        head_only = _report_snapshot()
        head_only["resources"]["displaced_journals"] = {
            "state": "complete", "complete": True,
            "paths": ({"path": "/fixture/data/.ingest_cache.bin.head.displaced-7",
                       "bytes": 0},),
        }
        rendered = _render_report(head_only)
        line = next(row for row in rendered.splitlines() if "displaced cache sidecar" in row)
        self.assertIn("1 directory stood where a parse cache sidecar belongs and was", line)
        self.assertIn(".head.displaced-7 (head, 0.0 MiB)", line)
        self.assertNotIn("journal", line)
        quiet = _report_snapshot()
        quiet["resources"]["displaced_journals"] = {
            "state": "complete", "complete": True, "paths": ()}
        self.assertNotIn("displaced cache sidecar", _render_report(quiet))
        self.assertNotIn("displaced cache sidecar", _render_report(_report_snapshot()))

    def test_sizing_stops_at_the_routine_deadline_but_still_names_every_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            first = data / ".ingest_cache.bin.journal.displaced-1"
            second = data / ".ingest_cache.bin.journal.displaced-2"
            for displaced in (first, second):
                (displaced / "nested").mkdir(parents=True)
                for index in range(40):
                    (displaced / "nested" / f"{index}.bin").write_bytes(b"z" * 100)
            with mock.patch.object(doctor.common, "DATA_DIR", data):
                expired = doctor._displaced_journals(deadline=time.monotonic() - 1.0)
                deep = doctor._displaced_journals(deadline=None)
                # a deadline that passes mid-walk sizes nothing past it, names everything
                ticks = iter([0.0] * 3 + [10.0] * 10_000)
                with mock.patch.object(doctor.time, "monotonic", lambda: next(ticks)):
                    partial = doctor._displaced_journals(deadline=5.0)
            self.assertEqual(
                [item["bytes"] for item in expired["paths"]], [None, None])
            self.assertEqual(
                [item["bytes"] for item in deep["paths"]], [4000, 4000])
            self.assertEqual([item["path"] for item in partial["paths"]],
                             [str(first), str(second)])
            self.assertEqual([item["bytes"] for item in partial["paths"]], [None, None])
            self.assertIn("size unknown", doctor.displaced_sidecar_line(expired))

    def test_the_row_lists_the_first_few_paths_and_counts_the_rest(self) -> None:
        census = {
            "state": "complete", "complete": True,
            "paths": tuple(
                {"path": f"/fixture/data/.ingest_cache.bin.journal.displaced-{index}",
                 "bytes": index * 1024}
                for index in range(3000)),
        }
        line = doctor.displaced_sidecar_line(census)
        self.assertLess(len(line), 600)
        self.assertIn("3000 directories stood where", line)
        self.assertIn(".journal.displaced-0 (journal, 0.0 MiB)", line)
        self.assertIn(".journal.displaced-2 (journal, 0.0 MiB)", line)
        self.assertNotIn(".journal.displaced-3 ", line)
        self.assertIn("and 2997 more (see --json)", line)
        self.assertIn("inspect, then delete", line)
        three = dict(census, paths=census["paths"][:3])
        self.assertNotIn("more", doctor.displaced_sidecar_line(three))


class DoctorInterruptedPassTests(unittest.TestCase):
    """A pass killed mid-commit leaves the event store's hot journal behind. Without a daemon
    nothing rolls it back before the next `agrep index`; the read surfaces said so and doctor
    was silent, its footprint quietly counting the journal."""

    MIB = 1024 ** 2

    def _store(self, data: Path) -> Path:
        events_dir = data / "events"
        events_dir.mkdir()
        store = events_dir / doctor.common.EVENT_STORE_NAME
        db = sqlite3.connect(store)
        try:
            db.execute("CREATE TABLE event_sessions(name TEXT PRIMARY KEY, payload BLOB)")
            db.execute("INSERT INTO event_sessions VALUES('s1', x'00')")
            db.commit()
        finally:
            db.close()
        return store

    @unittest.skipUnless(os.name == "posix", "needs SIGKILL")
    def test_a_killed_writers_hot_journal_is_an_interrupted_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            store = self._store(data)
            size = kill_mid_write(
                store, "UPDATE event_sessions SET payload = payload || zeroblob(400000)")
            lock = data / ".index.lock"
            with (mock.patch.object(doctor.common, "DATA_DIR", data),
                  mock.patch.object(doctor.common, "INDEX_LOCK_PATH", lock)):
                self.assertEqual(
                    doctor._event_store_journal(),
                    {"state": "interrupted-pass", "bytes": size, "hot": True})
                # a live publisher's journal is its write in progress
                start = doctor.common.process_start_identity(os.getpid())
                lock.write_bytes(index_lock.render_record(
                    pid=os.getpid(), start=start, token="a" * 32, label="agrep-rs",
                    time_ms=time.time_ns() // 1_000_000))
                self.assertEqual(doctor._event_store_journal()["state"], "live-pass")
                lock.unlink()
                # the next pass's write-capable open replays it
                repair = sqlite3.connect(store, isolation_level=None)
                try:
                    repair.execute("BEGIN EXCLUSIVE")
                    repair.execute("COMMIT")
                finally:
                    repair.close()
                self.assertEqual(
                    doctor._event_store_journal(),
                    {"state": "none", "bytes": 0, "hot": False})

    def test_an_unsynced_journal_is_inert_and_earns_no_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            store = self._store(data)
            Path(f"{store}-journal").write_bytes(bytes(4096))
            with (mock.patch.object(doctor.common, "DATA_DIR", data),
                  mock.patch.object(doctor.common, "INDEX_LOCK_PATH", data / ".index.lock")):
                journal = doctor._event_store_journal()
        self.assertEqual(journal, {"state": "interrupted-pass", "bytes": 4096, "hot": False})
        self.assertFalse(doctor.event_store_interrupted(journal))

    def test_the_report_names_the_interrupted_pass_and_its_fix(self) -> None:
        snapshot = _report_snapshot()
        snapshot["core"]["event_store_journal"] = {
            "state": "interrupted-pass", "bytes": 47 * self.MIB, "hot": True}
        rows = [row for row in _render_report(snapshot).splitlines() if "event store" in row]
        self.assertEqual(len(rows), 1)
        self.assertIn("[!! ]", rows[0])
        self.assertIn(
            "tool events are unavailable right now - an interrupted index pass left the event "
            "store unreadable until the next one (47 MiB rollback journal); `", rows[0])
        self.assertRegex(rows[0], r" index` runs a publication now$")
        for journal in ({"state": "none", "bytes": 0, "hot": False},
                        {"state": "live-pass", "bytes": 47 * self.MIB, "hot": True},
                        {"state": "interrupted-pass", "bytes": 4096, "hot": False}):
            quiet = _report_snapshot()
            quiet["core"]["event_store_journal"] = journal
            with self.subTest(journal=journal):
                self.assertNotIn("event store", _render_report(quiet))
        self.assertNotIn("event store", _render_report(_report_snapshot()))

    def test_the_report_asks_a_background_indexer_for_the_next_pass(self) -> None:
        def kicks(snapshot: dict) -> int:
            with mock.patch.object(doctor.indexd_runtime, "kick_background_repair",
                                   return_value=doctor.indexd_runtime.RepairKick(
                                       False, "no-daemon")) as kick:
                _render_report(snapshot)
            return kick.call_count

        healthy = kicks(_report_snapshot())
        for core in ({"event_store_journal": {
                          "state": "interrupted-pass", "bytes": 1, "hot": True}},
                     {"search_db": {"state": "interrupted-pass",
                                    "detail": surface.SEARCH_INDEX_INTERRUPTED}}):
            snapshot = _report_snapshot()
            snapshot["core"].update(core)
            with self.subTest(core=core):
                self.assertEqual(kicks(snapshot), healthy + 1)

    def test_a_journal_is_hot_by_sqlites_rule_a_nonzero_first_byte(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            store = self._store(data)
            # a torn header: not SQLite's magic, but SQLite still replays it
            Path(f"{store}-journal").write_bytes(b"\x01" + bytes(511))
            reader = sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("SELECT count(*) FROM event_sessions").fetchone()
            finally:
                reader.close()
            with (mock.patch.object(doctor.common, "DATA_DIR", data),
                  mock.patch.object(doctor.common, "INDEX_LOCK_PATH", data / ".index.lock")):
                journal = doctor._event_store_journal()
        self.assertEqual(journal, {"state": "interrupted-pass", "bytes": 512, "hot": True})
        self.assertTrue(doctor.event_store_interrupted(journal))

    def _corpus_readiness(self, data: Path) -> dict:
        journal_refusal = SimpleNamespace(
            writable=False, state="refused", journal_blocked=True, sqlite_failure=None,
            reason=f"corpus.db has a live rollback journal at {data / 'corpus.db-journal'}")
        with (mock.patch.object(doctor.common, "DATA_DIR", data),
              mock.patch.object(doctor.common, "INDEX_LOCK_PATH", data / ".index.lock"),
              mock.patch.object(corpusdb, "DB_PATH", data / "corpus.db"),
              mock.patch.object(corpusdb, "_derived_write_ownership",
                                return_value=journal_refusal)):
            return doctor._corpus_db_readiness(generation={"state": "ready"})

    def test_a_corpus_journal_is_an_index_pass_never_another_agrep_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            (data / "corpus.db").write_bytes(b"SQLite format 3\0" + bytes(84))
            Path(f"{data / 'corpus.db'}-journal").write_bytes(bytes(4096))
            dead = self._corpus_readiness(data)
            start = doctor.common.process_start_identity(os.getpid())
            (data / ".index.lock").write_bytes(index_lock.render_record(
                pid=os.getpid(), start=start, token="a" * 32, label="corpusdb",
                time_ms=time.time_ns() // 1_000_000))
            live = self._corpus_readiness(data)
        self.assertEqual(
            (dead["state"], dead["detail"], dead["journal"]["state"]),
            ("interrupted-pass", surface.SEARCH_INDEX_INTERRUPTED, "interrupted-pass"))
        self.assertEqual((live["state"], live["code"]), ("busy", "index-pass-writing"))
        snapshot = _report_snapshot()
        snapshot["core"]["search_db"] = dead
        with mock.patch.object(doctor.indexd_runtime, "kick_background_repair"):
            rows = [row for row in _render_report(snapshot).splitlines() if "search db" in row]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("[!! ]", rows[0])
        self.assertIn(
            "an interrupted index pass left the search index mid-update - searches serve its "
            "last complete update until the next one; `", rows[0])
        self.assertRegex(rows[0], r" index` runs a publication now$")
        snapshot["core"]["search_db"] = live
        self.assertNotIn("search db", _render_report(snapshot))


def _sidecar_fix(command: str, path: str) -> str:
    """source_health's remedy: a quoted shell command on POSIX, prose elsewhere."""
    return f"`{command} {path}`" if os.name == "posix" else "fix its owner or permissions"


class DoctorInaccessibleSidecarTests(unittest.TestCase):
    """A store sidecar this user cannot read and write: doctor named it 'another agrep' or not
    at all, and gave no remedy."""

    ROOT_OWNED = {"store": "search index", "kind": "rollback journal",
                  "path": "/fixture/data/corpus.db-journal", "owner": "root"}

    def test_the_line_names_the_owner_and_both_commands(self) -> None:
        line = doctor.source_health.inaccessible_sidecar_line(self.ROOT_OWNED, "agrep index")
        self.assertEqual(line, (
            "the search index cannot be updated - its rollback journal at "
            "/fixture/data/corpus.db-journal is owned by root, so no index pass can use it: "
            f"{_sidecar_fix('sudo chown $USER', '/fixture/data/corpus.db-journal')}, "
            "then `agrep index`"))
        own = dict(self.ROOT_OWNED, store="event store", owner=None,
                   path="/fixture/data/events/.store.sqlite3-journal")
        self.assertIn("is not readable and writable by you, so no index pass can use it: "
                      f"{_sidecar_fix('chmod u+rw', own['path'])}, then `agrep index`",
                      doctor.source_health.inaccessible_sidecar_line(own, "agrep index"))

    def test_the_report_gives_each_one_a_row(self) -> None:
        snapshot = _report_snapshot()
        snapshot["core"]["inaccessible_sidecars"] = [
            self.ROOT_OWNED,
            dict(self.ROOT_OWNED, store="event store",
                 path="/fixture/data/events/.store.sqlite3-journal")]
        snapshot["core"]["search_db"] = {
            "state": "sidecar-blocked", "code": "sidecar-blocked", "detail": "x",
            "integrity": {"state": "not-verified"}}
        with mock.patch.object(doctor.indexd_runtime, "kick_background_repair",
                               return_value=doctor.indexd_runtime.RepairKick(
                                   False, "sidecar-blocked")):
            rendered = _render_report(snapshot)
        rows = [row for row in rendered.splitlines() if "[!! ]" in row and "owned by" in row]
        self.assertEqual([row.split()[2:4] for row in rows],
                         [["search", "db"], ["event", "store"]])
        paths = [item["path"] for item in snapshot["core"]["inaccessible_sidecars"]]
        self.assertTrue(all(_sidecar_fix("sudo chown $USER", path) in row
                            for row, path in zip(rows, paths)), rows)
        self.assertNotIn("unreadable -", rendered)
        self.assertNotIn("another agrep", rendered)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "chmod 000 must deny this user")
    def test_readiness_calls_it_a_blocked_sidecar_never_another_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            (data / "corpus.db").write_bytes(b"SQLite format 3\0" + bytes(84))
            journal = Path(f"{data / 'corpus.db'}-journal")
            journal.write_bytes(bytes(512))
            journal.chmod(0)
            blocked = SimpleNamespace(
                writable=False, state="refused", journal_blocked=False, sidecar_blocked=True,
                sqlite_failure=None, reason="the search index cannot be updated")
            try:
                with (mock.patch.object(doctor.common, "DATA_DIR", data),
                      mock.patch.object(corpusdb, "DB_PATH", data / "corpus.db"),
                      mock.patch.object(corpusdb, "_derived_write_ownership",
                                        return_value=blocked)):
                    readiness = doctor._corpus_db_readiness(generation={"state": "ready"})
                    listed = doctor.source_health.store_sidecars_blocked()
            finally:
                journal.chmod(0o600)
        self.assertEqual(readiness["state"], "sidecar-blocked")
        self.assertEqual([item["path"] for item in listed], [str(journal)])

    BLOCK = ("index updates wait on /fixture/data/corpus.db-journal: "
             "`sudo chown $USER /fixture/data/corpus.db-journal`, then `agrep index`")

    def _freshness_row(self, freshness: dict) -> str:
        snapshot = _report_snapshot(
            resources={
                **_report_snapshot()["resources"],
                "indexd": {"state": "running", "running": True, "pid": 42,
                           "rss_state": "not-inspected", "rss_bytes": None},
            },
            freshness=freshness)
        with mock.patch.object(doctor.source_health, "sidecar_block_remedy",
                               return_value=self.BLOCK):
            rendered = _render_report(snapshot)
        return next(line for line in rendered.splitlines() if "freshness" in line)

    def test_the_freshness_row_carries_the_blocks_fix_not_a_refused_publication(self) -> None:
        # While paused it said '`<cli> index` runs a publication now', which `index` refuses.
        behind = self._freshness_row(
            {"state": "index-behind", "behind_s": 2520.0, "changed_stores": 1})
        self.assertIn(f"index 42m behind (1 store changed) - {self.BLOCK}", behind)
        self.assertNotIn("runs a publication now", behind)
        idle = self._freshness_row({"state": "no-known-failure"})
        self.assertIn("[-- ]", idle)
        self.assertIn(f"daemon running - pid 42, paused - {self.BLOCK}", idle)

    def test_a_hold_and_a_block_are_both_named_in_the_order_they_must_be_taken(self) -> None:
        with mock.patch.object(doctor.source_health, "sidecar_block_remedy",
                               return_value=self.BLOCK), \
                mock.patch.object(doctor.source_health, "held_publication_remedy",
                                  return_value="fix sc1 in crush"):
            self.assertEqual(doctor.source_health.blocked_publication_remedy(),
                             f"{self.BLOCK}; fix sc1 in crush")
            self.assertEqual(doctor.interrupted_pass_remedy(), f"{self.BLOCK}; fix sc1 in crush")

    def test_under_a_newer_agrep_the_fix_ends_in_an_update_not_a_refused_index(self) -> None:
        # This build serves a newer agrep's data dir read-only, so its `index` declines.
        with mock.patch.object(doctor.source_health.derived_anchor, "newer_anchor",
                               return_value=(2, 7)):
            line = doctor.source_health.inaccessible_sidecar_line(self.ROOT_OWNED, "agrep index")
        self.assertTrue(line.endswith(
            f"{_sidecar_fix('sudo chown $USER', '/fixture/data/corpus.db-journal')}, "
            "then update agrep to index it "
            "again (a newer agrep owns this data dir, and this one cannot index it)"), line)
        self.assertNotIn("`agrep index`", line)


if __name__ == "__main__":
    unittest.main()
