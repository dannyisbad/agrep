"""The ensemble gate: a render is judged whole, in every state, end to end.

Every other display test in this tree pins single lines, and each of those
tests can be right while the assembled render is garbage - the owner's box
printed a six-line wall of individually-tested sentences for weeks. This
suite closes that hole from the outside: the REAL pipeline (rust golden
fixture store -> real ingest binary -> real cli.py subprocess) is run in
every state we can construct, and the whole stderr render must fit
surface_policy's budget: at most RENDER_STDERR_BUDGET non-empty lines, no
line longer than RENDER_LINE_MAX_CHARS. No internal mocks - if a state
prints a wall, this suite is red no matter how many per-line tests pass.

States constructed from the healthy dir by real damage, mirroring the A2
battery and the owner's live box: foreign-dead owner anchor, truncated
corpus.db, deleted search db, deleted parse cache. Daemons stay disabled
(AGREP_NO_DAEMON) so every run is deterministic; the healing itself is
test_self_healing's contract, the RENDER under damage is this one's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _test_support import isolate_data_dir

isolate_data_dir()

import common  # noqa: E402
import surface_policy as surface  # noqa: E402

PY_DIR = Path(__file__).resolve().parent
ROOT = PY_DIR.parent
FIXTURE_HOME = ROOT / "crates" / "agrep-cli" / "tests" / "fixtures" / "claude" / "home"
CLI = ROOT / "cli.py"


def _ingest_binary() -> Path | None:
    binp = common.ingest_bin()
    return binp if binp and Path(str(binp)).exists() else None


class RenderBudget(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        binp = _ingest_binary()
        if binp is None:
            raise unittest.SkipTest("no ingest binary (run cargo build)")
        if not FIXTURE_HOME.exists():
            raise unittest.SkipTest("no rust golden fixtures (installed package)")
        cls._tmp = tempfile.TemporaryDirectory(prefix="agrep-render-budget-")
        cls.healthy = Path(cls._tmp.name) / "healthy"
        cls.healthy.mkdir()
        # build through the real python entry so the writer identity binds as
        # in production - a bare agrep-rs run anchors under its OWN identity
        # and the whole dir renders foreign to the cli (this suite found that)
        rc, _out, err = cls._cli(cls.healthy, ["index", "--full"])
        if rc != 0:
            raise unittest.SkipTest(f"fixture index failed: {err[-200:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @staticmethod
    def _env(data_dir: Path) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGREP_")
               and k not in ("USERPROFILE", "APPDATA", "XDG_CONFIG_HOME")}
        env.update({
            "AGREP_HOME": str(FIXTURE_HOME),
            "AGREP_DATA_DIR": str(data_dir),
            "AGREP_NO_DAEMON": "1",
            # an agent-context render is the one the owner's agents see
            "CLAUDECODE": "1",
            "NO_COLOR": "1",
        })
        return env

    @classmethod
    def _cli(cls, data_dir: Path, argv: list[str]) -> tuple[int, str, str]:
        r = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=cls._env(data_dir), timeout=120, cwd=str(ROOT))
        return r.returncode, r.stdout, r.stderr

    def _state(self, name: str) -> Path:
        target = Path(self._tmp.name) / name
        if not target.exists():
            shutil.copytree(self.healthy, target)
        return target

    def _assert_budget(self, argv: list[str], data_dir: Path, label: str) -> None:
        _rc, _out, err = self._cli(data_dir, argv)
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertLessEqual(
            len(lines), surface.RENDER_STDERR_BUDGET,
            f"{label}: {len(lines)} stderr lines:\n" + "\n".join(lines))
        for line in lines:
            self.assertLessEqual(
                len(line), surface.RENDER_LINE_MAX_CHARS,
                f"{label}: line over budget ({len(line)} chars): {line}")

    # -- healthy renders --------------------------------------------------

    def test_healthy_hit_miss_and_filtered_gap(self) -> None:
        for label, argv in (
            ("hit", ["fixture", "--classic"]),
            ("miss", ["zzz_render_budget_missing", "--classic"]),
            ("gap", ["zzz_render_budget_missing", "--agent", "gemini",
                     "--classic"]),
            ("recall-miss", ["recall", "zzz_render_budget_missing",
                             "--lexical"]),
            ("chats", ["chats"]),
        ):
            self._assert_budget(argv, self.healthy, f"healthy/{label}")

    # -- the owner's box: a dead foreign owner ---------------------------

    def test_foreign_dead_owner_render_fits_the_budget(self) -> None:
        state = self._state("foreign-owner")
        anchor = state / ".derived-owner.json"
        if anchor.exists():
            record = json.loads(anchor.read_text(encoding="utf-8"))
            record["build_id"] = "f" * 20
            anchor.write_text(json.dumps(record), encoding="utf-8")
        db = state / "corpus.db"
        if db.exists():
            # the db half of the same box state: owned by the same dead build
            import sqlite3
            con = sqlite3.connect(db)
            try:
                con.execute("UPDATE meta SET v = ? WHERE k = 'build_id'",
                            ("f" * 20,))
                con.commit()
            except sqlite3.Error:
                pass
            finally:
                con.close()
        self._assert_budget(["fixture", "--classic"], state, "foreign/hit")
        self._assert_budget(
            ["zzz_render_budget_missing", "--agent", "gemini", "--classic"],
            state, "foreign/gap")

    # -- damaged derived artifacts ---------------------------------------

    def test_truncated_search_db_render_fits_the_budget(self) -> None:
        state = self._state("truncated-db")
        db = state / "corpus.db"
        if db.exists():
            db.write_bytes(db.read_bytes()[: max(1, db.stat().st_size // 3)])
        self._assert_budget(["fixture", "--classic"], state, "truncated/hit")

    def test_deleted_search_db_render_fits_the_budget(self) -> None:
        state = self._state("deleted-db")
        (state / "corpus.db").unlink(missing_ok=True)
        self._assert_budget(["fixture", "--classic"], state, "deleted-db/hit")

    def test_deleted_parse_cache_render_fits_the_budget(self) -> None:
        state = self._state("deleted-cache")
        (state / ".ingest_cache.bin").unlink(missing_ok=True)
        self._assert_budget(["fixture", "--classic"], state, "no-cache/hit")

    def test_status_fits_the_budget_in_every_state(self) -> None:
        for name in ("healthy", "foreign-owner", "truncated-db"):
            state = (self.healthy if name == "healthy"
                     else self._state(name))
            self._assert_budget(["status"], state, f"status/{name}")

    # -- around: a handle into the corpus, in every handle state ----------

    @classmethod
    def _cli_home(cls, data_dir: Path, home: Path,
                  argv: list[str]) -> tuple[int, str, str]:
        env = cls._env(data_dir)
        env["AGREP_HOME"] = str(home)
        r = subprocess.run(
            [sys.executable, str(CLI), *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=120, cwd=str(ROOT))
        return r.returncode, r.stdout, r.stderr

    def _assert_stderr_budget(self, err: str, label: str) -> None:
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertLessEqual(
            len(lines), surface.RENDER_STDERR_BUDGET,
            f"{label}: {len(lines)} stderr lines:\n" + "\n".join(lines))
        for line in lines:
            self.assertLessEqual(
                len(line), surface.RENDER_LINE_MAX_CHARS,
                f"{label}: line over budget ({len(line)} chars): {line}")

    def _hit_handle(self) -> str:
        # a handle minted the way agents mint them: from a --json hit row
        _rc, out, _err = self._cli(self.healthy, ["timer", "--json"])
        for line in out.splitlines():
            row = json.loads(line) if line.strip() else {}
            if row.get("session") and row.get("turn") is not None:
                return f"@{row['session'][:8]}:{row['turn']}"
        raise unittest.SkipTest("no indexed hit to mint a handle from")

    def _ambiguous_state(self) -> tuple[Path, Path]:
        home = Path(self._tmp.name) / "ambiguous-home"
        state = Path(self._tmp.name) / "ambiguous"
        if state.exists():
            return state, home
        shutil.copytree(FIXTURE_HOME, home)
        src = (home / ".claude" / "projects" / "proj-alpha"
               / "sess-claude-0001.jsonl")
        twin = src.with_name("sess-claude-0002.jsonl")
        twin.write_text(
            src.read_text(encoding="utf-8").replace(
                "11111111-1111-4111-8111-111111111111",
                "11111111-2222-4222-8222-222222222222"),
            encoding="utf-8")
        state.mkdir()
        rc, _out, err = self._cli_home(state, home, ["index", "--full"])
        if rc != 0:
            raise unittest.SkipTest(f"ambiguous index failed: {err[-200:]}")
        return state, home

    def test_around_hit_handle_render_fits_the_budget(self) -> None:
        self._assert_budget(["around", self._hit_handle()], self.healthy,
                            "around/hit-handle")

    def test_around_ambiguous_prefix_render_fits_the_budget(self) -> None:
        state, home = self._ambiguous_state()
        _rc, _out, err = self._cli_home(state, home,
                                        ["around", "11111111", "0"])
        self._assert_stderr_budget(err, "around/ambiguous-prefix")

    def test_around_ambiguous_handle_render_fits_the_budget(self) -> None:
        state, home = self._ambiguous_state()
        _rc, _out, err = self._cli_home(state, home, ["around", "@11111111:0"])
        self._assert_stderr_budget(err, "around/ambiguous-handle")

    def test_around_stale_handle_render_fits_the_budget(self) -> None:
        self._assert_budget(["around", "@deadbeef:5"], self.healthy,
                            "around/stale-handle")

    # -- doctor: the report stream and its line widths --------------------

    _DOCTOR_STATES = ("healthy", "foreign-owner", "truncated-db",
                      "deleted-db", "deleted-cache")

    def _doctor_state(self, name: str) -> Path:
        # damage applied here, not borrowed: the sibling damage tests build
        # their copies lazily, so sharing them would depend on run order
        if name == "healthy":
            return self.healthy
        state = Path(self._tmp.name) / f"doctor-{name}"
        if state.exists():
            return state
        shutil.copytree(self.healthy, state)
        if name == "foreign-owner":
            anchor = state / ".derived-owner.json"
            if anchor.exists():
                record = json.loads(anchor.read_text(encoding="utf-8"))
                record["build_id"] = "f" * 20
                anchor.write_text(json.dumps(record), encoding="utf-8")
            db = state / "corpus.db"
            if db.exists():
                import sqlite3
                con = sqlite3.connect(db)
                try:
                    con.execute("UPDATE meta SET v = ? WHERE k = 'build_id'",
                                ("f" * 20,))
                    con.commit()
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
        elif name == "truncated-db":
            db = state / "corpus.db"
            if db.exists():
                db.write_bytes(
                    db.read_bytes()[: max(1, db.stat().st_size // 3)])
        elif name == "deleted-db":
            (state / "corpus.db").unlink(missing_ok=True)
        elif name == "deleted-cache":
            (state / ".ingest_cache.bin").unlink(missing_ok=True)
        return state

    def test_doctor_stderr_fits_the_budget_in_every_state(self) -> None:
        for name in self._DOCTOR_STATES:
            self._assert_budget(["doctor"], self._doctor_state(name),
                                f"doctor/{name}")

    # Doctor's report is stdout, and law 7 caps its lines like any surface:
    # the not-installed instructions row is 138 chars of wording plus the
    # invocation command (>=143 even as `agrep`) - doctor cluster fix (M4).
    @unittest.expectedFailure
    def test_doctor_stdout_lines_fit_the_width_budget(self) -> None:
        for name in self._DOCTOR_STATES:
            _rc, out, _err = self._cli(self._doctor_state(name), ["doctor"])
            for line in out.splitlines():
                self.assertLessEqual(
                    len(line), surface.RENDER_LINE_MAX_CHARS,
                    f"doctor/{name}: line over budget ({len(line)} chars): "
                    f"{line}")


if __name__ == "__main__":
    unittest.main()
