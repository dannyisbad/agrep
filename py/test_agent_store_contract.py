"""Cross-language store-locator conformance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
from hookless import live, locators, native, registry  # noqa: E402


_ROOT = Path(__file__).resolve().parents[1]
_BINARY_NAME = "agrep-rs.exe" if os.name == "nt" else "agrep-rs"


def _release_binary() -> Path | None:
    candidates = (
        _ROOT / "target" / "release" / _BINARY_NAME,
        _ROOT / "_bin" / _BINARY_NAME,
    )
    return next((path for path in candidates if path.is_file()), None)


def _write(path: str, payload: bytes = b"fixture") -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return str(target)


def _hookless_census(
        agents: tuple[str, ...], home: str,
        environ: dict[str, str]) -> dict[str, set[str]]:
    census = {}
    for agent in agents:
        paths = set()
        for raw_root in locators.store_roots(
                agent, home, environ=environ, os_name=os.name,
                sys_platform=sys.platform):
            root = Path(raw_root)
            candidates = [root] if root.is_file() else (
                root.rglob("*") if root.is_dir() else ())
            for candidate in candidates:
                if (candidate.is_file()
                        and locators.store_content(
                            agent, candidate, home=home,
                            environ=environ, os_name=os.name)):
                    paths.add(os.path.normcase(os.path.abspath(candidate)))
        census[agent] = paths
    return census


class AgentStoreContractTests(unittest.TestCase):
    def test_locator_set_is_the_live_capability_set(self) -> None:
        self.assertEqual(
            locators.STORE_AGENTS,
            registry.LIVE_AGENTS,
        )

    def test_dynamic_locator_cases(self) -> None:
        home = os.path.join(os.sep, "users", "tester")
        env = {
            "XDG_DATA_HOME": os.path.join(os.sep, "var", "data"),
            "LOCALAPPDATA": os.path.join(home, "Local"),
            "APPDATA": os.path.join(home, "Roaming"),
        }
        self.assertEqual(
            locators.opencode_data_dirs(
                home, environ=env, os_name="posix"),
            [
                os.path.join(env["XDG_DATA_HOME"], "opencode"),
                os.path.join(home, ".local", "share", "opencode"),
            ],
        )
        self.assertEqual(
            locators.store_roots("pi", home),
            [
                os.path.join(home, ".pi", "agent", "sessions"),
                os.path.join(home, ".omp", "agent", "sessions"),
            ],
        )
        self.assertEqual(
            locators.opencode_data_dirs(home, environ=env, os_name="nt")[0],
            os.path.join(env["XDG_DATA_HOME"], "opencode"),
        )
        self.assertIn(
            os.path.join("Library", "Application Support", "Cursor"),
            locators.cursor_db_candidates(
                home, environ=env, os_name="posix",
                sys_platform="darwin")[0],
        )
        posix_candidates = locators.cursor_db_candidates(
            home, environ={**env, "APPDATA": "/poisoned/appdata"},
            os_name="posix", sys_platform="darwin")
        self.assertFalse(any("/poisoned/appdata" in path
                             for path in posix_candidates))
        self.assertIn(
            os.path.join(home, "AppData", "Roaming", "Cursor"),
            posix_candidates[1],
        )
        self.assertEqual(
            locators.cursor_db_candidates(
                home, environ=env, os_name="nt",
                sys_platform="win32")[0],
            os.path.join(
                env["APPDATA"], "Cursor", "User", "globalStorage",
                "state.vscdb"),
        )
        empty_platform_dirs = {"LOCALAPPDATA": "", "APPDATA": ""}
        self.assertEqual(
            locators.opencode_data_dirs(
                home, environ=empty_platform_dirs, os_name="nt")[0],
            os.path.join(home, "AppData", "Local", "opencode"),
        )
        self.assertEqual(
            locators.cursor_db_candidates(
                home, environ=empty_platform_dirs, os_name="nt",
                sys_platform="win32")[0],
            os.path.join(
                home, "AppData", "Roaming", "Cursor", "User",
                "globalStorage", "state.vscdb"),
        )

    def test_agrep_home_matches_rust_discovery_precedence(self) -> None:
        override = os.path.join(os.sep, "fixture", "home")
        env = {
            "AGREP_HOME": override,
            "USERPROFILE": os.path.join(os.sep, "poisoned", "profile"),
            "HOME": os.path.join(os.sep, "poisoned", "home"),
            "XDG_DATA_HOME": os.path.join(os.sep, "poisoned", "xdg"),
            "LOCALAPPDATA": os.path.join(os.sep, "poisoned", "local"),
        }
        self.assertEqual(locators.discovery_home(env), override)
        self.assertEqual(
            locators.store_root("claude", locators.discovery_home(env)),
            os.path.join(override, ".claude", "projects"),
        )
        self.assertEqual(
            locators.store_roots("pi", override),
            [
                os.path.join(override, ".pi", "agent", "sessions"),
                os.path.join(override, ".omp", "agent", "sessions"),
            ],
        )
        self.assertEqual(
            locators.opencode_data_dirs(
                override, environ=env, os_name="posix"),
            [os.path.join(override, ".local", "share", "opencode")],
        )

    def test_content_predicate_cases(self) -> None:
        cases = {
            "claude": {
                "yes": ["chat.jsonl"],
                "no": ["chat.JSONL", "chat.json"],
            },
            "codex": {
                "yes": ["rollout-good.jsonl"],
                "no": ["good.jsonl", "rollout-good.json"],
            },
            "opencode": {
                "yes": ["opencode.db", "OpenCode-nightly.DB"],
                "no": ["opencode.db.bak", "opencode.corrupted.db", "chat.db"],
            },
            "antigravity": {
                "yes": ["transcript.jsonl", "conversation.json"],
                "no": ["transcript.txt", "conversation.JSON"],
            },
            "pi": {
                "yes": ["chat.jsonl"],
                "no": ["chat.JSONL", "chat.jsonl.gz", "chat.json"],
            },
        }
        for agent, verdicts in cases.items():
            for name in verdicts["yes"]:
                self.assertTrue(
                    locators.store_content(agent, name),
                    f"{agent} should accept {name}",
                )
            for name in verdicts["no"]:
                self.assertFalse(
                    locators.store_content(agent, name),
                    f"{agent} should reject {name}",
                )

    def test_live_and_native_surfaces_call_the_shared_locators(self) -> None:
        watcher = live.LiveWatcher()
        with mock.patch.object(
                live, "store_root", return_value="/missing/store") as root:
            watcher._tick_claude(0.0)
            watcher._tick_codex(2 * 86_400.0)
            watcher._tick_antigravity(0.0)
        self.assertEqual(
            {call.args[0] for call in root.call_args_list},
            {"claude", "codex", "antigravity"},
        )
        with mock.patch.object(
                live, "store_roots", return_value=[]) as roots:
            watcher._tick_pi(0.0)
        roots.assert_called_once_with("pi", live.HOME)
        with mock.patch.object(
                live, "cursor_db_paths", return_value=["cursor.db"]) as cursor:
            self.assertEqual(live._cursor_db_paths(), ["cursor.db"])
        cursor.assert_called_once_with(live.HOME)
        self.assertIs(native.opencode_data_dirs, locators.opencode_data_dirs)
        self.assertIs(native._opencode_db_name, locators.opencode_db_name)

        with mock.patch.object(
                native, "store_root", return_value="/missing/store") as root, \
                mock.patch.object(native.glob, "iglob", return_value=[]):
            self.assertEqual(native._claude_cwd("session"), "")
            self.assertEqual(native._codex_cwd("session"), "")
            self.assertEqual(native._antigravity_cwd("session"), "")
        self.assertEqual(
            {call.args[0] for call in root.call_args_list},
            {"claude", "codex", "antigravity"},
        )

    def test_opencode_locator_skips_one_broken_directory(self) -> None:
        class Entries:
            def __init__(self, values=(), error=None):
                self.values = values
                self.error = error
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def __iter__(self):
                if self.error is not None:
                    raise self.error
                return iter(self.values)

        broken = Entries(error=OSError("directory vanished"))
        entry = mock.Mock()
        entry.name = "opencode-nightly.db"
        entry.path = "/good/opencode-nightly.db"
        entry.is_file.return_value = True
        good = Entries([entry])
        with mock.patch.object(
                native, "opencode_data_dirs",
                return_value=["/broken", "/good"]), \
                mock.patch.object(
                    native, "opencode_explicit_db", return_value=None), \
                mock.patch.object(
                    native.os, "scandir", side_effect=(broken, good)), \
                mock.patch.object(native.os.path, "isfile", return_value=True), \
                mock.patch.object(native.os.path, "islink", return_value=False):
            self.assertEqual(
                native.opencode_db_paths("/home"),
                ["/good/opencode-nightly.db"],
            )
        self.assertTrue(broken.closed)
        self.assertTrue(good.closed)

    def test_codex_resume_stops_store_scan_after_its_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rollout = Path(td) / "rollout-session.jsonl"
            rollout.write_text(
                json.dumps({"payload": {"cwd": "/project"}}) + "\n",
                encoding="utf-8",
            )

            def matches():
                yield str(rollout)
                raise AssertionError("resume consumed files after its match")

            with mock.patch.object(
                    native.glob, "iglob", return_value=matches()):
                self.assertEqual(native._codex_cwd("session"), "/project")

    def test_rust_census_matches_hookless_roots_and_predicates(self) -> None:
        binary = _release_binary()
        if binary is None:
            self.skipTest("release agrep-rs has not been built")
        with tempfile.TemporaryDirectory(prefix="agrep-store-contract-") as td:
            env = os.environ.copy()
            env.pop("AGREP_HOME", None)
            env.pop("OPENCODE_DB", None)
            env["HOME"] = td
            env["USERPROFILE"] = td
            if os.name == "nt":
                env["LOCALAPPDATA"] = os.path.join(td, "AppData", "Local")
                env["APPDATA"] = os.path.join(td, "AppData", "Roaming")
                env.pop("XDG_DATA_HOME", None)
            else:
                env["XDG_DATA_HOME"] = os.path.join(td, "xdg")
                env["APPDATA"] = os.path.join(td, "poisoned-appdata")
            opencode_dirs = locators.opencode_data_dirs(
                td, environ=env, os_name=os.name)
            env["OPENCODE_DB"] = "channel.sqlite"

            agents = registry.LIVE_AGENTS
            _write(os.path.join(
                locators.store_root("claude", td),
                "project", "chat.jsonl"))
            _write(os.path.join(
                locators.store_root("claude", td),
                "project", "chat.JSONL"))
            _write(os.path.join(
                locators.store_root("codex", td),
                "2026", "01", "02", "rollout-good.jsonl"))
            _write(os.path.join(
                locators.store_root("codex", td),
                "2026", "01", "02", "not-a-rollout.jsonl"))
            for root in opencode_dirs:
                _write(os.path.join(root, "opencode.db"))
            _write(os.path.join(opencode_dirs[0], "channel.sqlite"))
            _write(os.path.join(opencode_dirs[0], "opencode.db.bak"))
            _write(os.path.join(
                locators.store_root("antigravity", td),
                "session", ".system_generated", "logs",
                "transcript.jsonl"))
            _write(os.path.join(
                locators.store_root("antigravity", td),
                "session", ".system_generated", "logs",
                "transcript.txt"))
            for root in locators.store_roots("pi", td):
                _write(os.path.join(root, "project", "chat.jsonl"))
            _write(locators.cursor_db_candidates(
                td, environ=env, os_name=os.name,
                sys_platform=sys.platform)[0])
            if os.name != "nt":
                _write(os.path.join(
                    env["APPDATA"], "Cursor", "User", "globalStorage",
                    "state.vscdb"))

            completed = subprocess.run(
                [str(binary), "stores", "--paths"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = json.loads(completed.stdout)
            actual = {
                agent: {
                    os.path.normcase(os.path.abspath(row["path"]))
                    for row in rows if row["name"] == agent
                }
                for agent in agents
            }
            self.assertEqual(actual, _hookless_census(agents, td, env))

    def test_rust_census_and_hookless_share_agrep_home(self) -> None:
        binary = _release_binary()
        if binary is None:
            self.skipTest("release agrep-rs has not been built")
        with tempfile.TemporaryDirectory(
                prefix="agrep-store-home-contract-") as td:
            override = os.path.join(td, "override")
            poisoned = os.path.join(td, "poisoned")
            env = os.environ.copy()
            env["AGREP_HOME"] = override
            env["HOME"] = poisoned
            env["USERPROFILE"] = poisoned
            env["XDG_DATA_HOME"] = os.path.join(poisoned, "xdg")
            env.pop("OPENCODE_DB", None)

            wanted = {
                os.path.normcase(os.path.abspath(_write(os.path.join(
                    locators.store_root("claude", override),
                    "project", "chat.jsonl")))),
                os.path.normcase(os.path.abspath(_write(os.path.join(
                    override, ".local", "share", "opencode",
                    "opencode.db")))),
            }
            _write(os.path.join(
                locators.store_root("claude", poisoned),
                "project", "poisoned.jsonl"))
            _write(os.path.join(
                env["XDG_DATA_HOME"], "opencode", "opencode.db"))

            completed = subprocess.run(
                [str(binary), "stores", "--paths"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = json.loads(completed.stdout)
            rust_paths = {
                os.path.normcase(os.path.abspath(row["path"]))
                for row in rows if row["name"] in {"claude", "opencode"}
            }
            self.assertEqual(rust_paths, wanted)

            home = locators.discovery_home(env)
            census = _hookless_census(("claude", "opencode"), home, env)
            hookless_paths = set().union(*census.values())
            self.assertEqual(hookless_paths, wanted)

    @unittest.skipUnless(os.name == "posix", "POSIX permission fixture")
    def test_rust_census_keeps_an_unreadable_root_visible(self) -> None:
        binary = _release_binary()
        if binary is None:
            self.skipTest("release agrep-rs has not been built")
        with tempfile.TemporaryDirectory(
                prefix="agrep-store-unreadable-") as td:
            root = Path(td) / ".claude" / "projects"
            source = root / "project" / "chat.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text("fixture", encoding="utf-8")
            env = os.environ.copy()
            env["AGREP_HOME"] = td
            root.chmod(0)
            try:
                completed = subprocess.run(
                    [str(binary), "stores"],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            finally:
                root.chmod(0o700)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = json.loads(completed.stdout)
            claude = next(row for row in rows if row["name"] == "claude")
            self.assertEqual(claude["state"], "source-unreadable")
            self.assertEqual(claude["files"], 0)
            self.assertEqual(claude["issues"][0]["path"], str(root))
            self.assertEqual(
                claude["issues"][0]["kind"], "permission-denied")


if __name__ == "__main__":
    unittest.main()
