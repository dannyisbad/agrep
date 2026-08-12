from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import doctor  # noqa: E402
import embed  # noqa: E402
import explore  # noqa: E402
import indexd  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import ownerfile  # noqa: E402
import semantic  # noqa: E402
import session_context  # noqa: E402
from hookless.registry import AGENT_CONTEXT_ENV_KEYS  # noqa: E402

sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))
import reindex  # noqa: E402


BUILD_A = "aaaaaaaaaaaaaaaaaaaa"
BUILD_B = "bbbbbbbbbbbbbbbbbbbb"
ROOT = Path(__file__).resolve().parents[1]
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if os.name == "nt" else "agrep-rs")


class DerivedOwnershipTests(unittest.TestCase):
    def _release_binary(self) -> Path:
        self.assertTrue(
            RELEASE_BIN.is_file(),
            f"release ingest binary missing: {RELEASE_BIN}",
        )
        return RELEASE_BIN.resolve(strict=True)

    @staticmethod
    def _census(root: Path) -> dict:
        def metadata(path: Path) -> tuple[int, int, int, int]:
            observed = path.lstat()
            return (
                stat.S_IFMT(observed.st_mode) | stat.S_IMODE(observed.st_mode),
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
            )

        entries = {}
        for path in sorted(
                root.rglob("*"), key=lambda value: os.fspath(value)):
            relative = path.relative_to(root).as_posix()
            observed = path.lstat()
            entries[relative] = (
                metadata(path),
                path.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
            )
        return {"root": metadata(root), "entries": entries}

    @staticmethod
    def _messages(root: Path) -> Path:
        path = root / "messages.jsonl"
        path.write_text(json.dumps({
            "id": "codex:s:1",
            "agent": "codex",
            "project": "repo",
            "session": "s",
            "turn": 1,
            "ts": 1,
            "who": "user",
            "text": "owned snapshot needle",
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _database(
            path: Path, *, owner: str | None, stamp: str = "stamp") -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        db.execute(corpusdb._INS, (
            "s", 1, 1, "codex", "repo", "", "", "", "user",
            "owned snapshot needle",
        ))
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute(
            "INSERT INTO msgs_prose_fts(rowid, text) "
            "SELECT id, text FROM msgs WHERE who <> 'tool'")
        meta = [
            ("schema", corpusdb._SCHEMA),
            ("stamp", stamp),
            ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
        ]
        if owner is not None:
            meta.append(("build_id", owner))
        db.executemany("INSERT INTO meta VALUES(?, ?)", meta)
        db.commit()
        db.close()

    @staticmethod
    def _legacy_proof(path: Path) -> dict:
        identity = corpusdb._proof_file_identity(path)
        size, modified_ns, changed, _device, _inode = identity
        if corpusdb._PLATFORM_NAME == "posix":
            change_token = {
                "Metadata": corpusdb._unix_change_token(changed)}
        elif corpusdb._PLATFORM_NAME == "nt":
            try:
                _change_time, usn = corpusdb._windows_file_state(
                    path, include_usn=True)
                if usn is None:
                    raise OSError("filesystem did not return a USN")
                change_token = {"Metadata": usn}
            except OSError:
                change_token = {
                    "ContentSha256": list(
                        corpusdb._content_sha256(path, identity))}
        else:
            change_token = {"Metadata": 0}
        return {
            "name": "corpus.db",
            "len": size,
            "modified_ns": modified_ns,
            "change_token": change_token,
            "edge_hash": corpusdb._edge_hash(path, size, identity),
        }

    @staticmethod
    def _owner(path: Path, build: str, proof: dict | None = None) -> None:
        record = {"version": 1, "build_id": build}
        if proof is not None:
            record["legacy_corpus_db"] = proof
        path.write_text(
            json.dumps(record, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def _cache(path: Path, build: str) -> None:
        header = bytearray(44)
        header[:4] = int(19).to_bytes(4, "little")
        header[12:20] = b"AGRPCB01"
        header[20:24] = int(4).to_bytes(4, "little")
        header[24:44] = build.encode("ascii")
        path.write_bytes(header)

    @contextlib.contextmanager
    def _paths(self, root: Path, build: str = BUILD_A):
        messages = root / "messages.jsonl"
        db_path = root / "corpus.db"
        owner_path = root / ".derived-owner.json"
        cache_path = root / ".ingest_cache.bin"
        with (
            mock.patch.object(common, "DATA_DIR", root),
            mock.patch.object(common, "MESSAGES_PATH", messages),
            mock.patch.object(corpusdb, "DB_PATH", db_path),
            mock.patch.object(
                corpusdb, "CHANGED_PATH", root / ".changed_sessions"),
            mock.patch.object(
                indexd_runtime, "DERIVED_OWNER_PATH", owner_path),
            mock.patch.object(
                indexd_runtime, "INGEST_CACHE_PATH", cache_path),
            mock.patch.object(indexd_runtime, "INDEXD_BUILD_ID", build),
            mock.patch.object(
                indexd_runtime, "derived_writer_build_id",
                return_value=build),
        ):
            yield messages, db_path, owner_path

    def setUp(self) -> None:
        indexd_runtime._clear_freshen_failure()

    def tearDown(self) -> None:
        indexd_runtime._clear_freshen_failure()

    def test_python_prepares_one_exact_launch_scoped_build_id_for_rust(
            self) -> None:
        self.assertRegex(indexd_runtime.INDEXD_BUILD_ID, r"^[0-9a-f]{20}$")
        binary = common.ingest_bin()
        inherited = "c" * 20
        with mock.patch.dict(
                os.environ, {"AGREP_RUNTIME_BUILD_ID": inherited}, clear=False):
            env = indexd_runtime.rust_writer_env(binary)
            exported = env[
                "AGREP_RUNTIME_BUILD_ID"]
            self.assertEqual(
                os.environ.get("AGREP_RUNTIME_BUILD_ID"), inherited)
            self.assertEqual(
                exported,
                indexd_runtime.derived_writer_build_id(binary))
            self.assertEqual(
                env[indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV],
                indexd_runtime.INDEXD_BUILD_ID)

    def test_binary_bytes_are_part_of_the_derived_writer_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-binaries-") as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            first.write_bytes(b"A" * 4096)
            second.write_bytes(b"B" * 4096)
            fixed_ns = 1_700_000_000_000_000_000
            os.utime(first, ns=(fixed_ns, fixed_ns))
            os.utime(second, ns=(fixed_ns, fixed_ns))
            with mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_ID", BUILD_A):
                first_id = indexd_runtime.derived_writer_build_id(first)
                second_id = indexd_runtime.derived_writer_build_id(second)
            self.assertNotEqual(first_id, second_id)
            self.assertRegex(first_id, r"^[0-9a-f]{20}$")
            self.assertRegex(second_id, r"^[0-9a-f]{20}$")

    def test_python_bytes_are_part_of_the_derived_writer_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-python-") as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            fixed_ns = 1_700_000_000_000_000_000
            for directory, content in (
                    (first, b"writer = 'A'\n"),
                    (second, b"writer = 'B'\n")):
                member = directory / "writer.py"
                member.write_bytes(content)
                os.utime(member, ns=(fixed_ns, fixed_ns))
            first_digest = indexd_runtime._python_runtime_digest(
                ("writer.py",), first)
            second_digest = indexd_runtime._python_runtime_digest(
                ("writer.py",), second)
            self.assertNotEqual(
                first_digest, second_digest,
                "same-size restored-mtime Python edits are different writers")

    def test_runtime_digest_cache_rehashes_identity_movement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-python-cache-") as raw:
            root = Path(raw)
            member = root / "writer.py"
            fixed_ns = 1_700_000_000_000_000_000
            member.write_bytes(b"writer = 'A'\n")
            os.utime(member, ns=(fixed_ns, fixed_ns))
            with mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_FILES", ("writer.py",)), \
                    mock.patch.object(indexd_runtime.common, "PY_DIR", root), \
                    mock.patch.object(
                        indexd_runtime, "_PYTHON_RUNTIME_DIGEST_CACHE", None):
                startup = indexd_runtime._python_runtime_digest()
                with mock.patch.object(
                        indexd_runtime, "INDEXD_BUILD_DIGEST", startup):
                    indexd_runtime.assert_python_runtime_unchanged()
                    replacement = root / "writer.py.new"
                    replacement.write_bytes(b"writer = 'B'\n")
                    os.utime(replacement, ns=(fixed_ns, fixed_ns))
                    os.replace(replacement, member)
                    with self.assertRaisesRegex(
                            OSError, "runtime changed after process start"):
                        indexd_runtime.assert_python_runtime_unchanged()

    def test_resident_writer_refuses_after_exact_python_runtime_moves(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-python-move-") as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            fixed_ns = 1_700_000_000_000_000_000
            for directory, content in (
                    (first, b"lazy_writer = 'A'\n"),
                    (second, b"lazy_writer = 'B'\n")):
                member = directory / "lazy_writer.py"
                member.write_bytes(content)
                os.utime(member, ns=(fixed_ns, fixed_ns))
            startup = indexd_runtime._python_runtime_digest(
                ("lazy_writer.py",), first)
            moved = indexd_runtime._python_runtime_digest(
                ("lazy_writer.py",), second)
            binary = root / "agrep-rs"
            binary.write_bytes(b"exact writer binary")
            with (
                mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_DIGEST", startup),
                mock.patch.object(
                    indexd_runtime, "_python_runtime_digest",
                    return_value=moved),
                mock.patch.dict(os.environ, {
                    "AGREP_RUNTIME_BUILD_ID": BUILD_A,
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED": "prior refusal",
                }, clear=False),
            ):
                with self.assertRaisesRegex(
                        OSError, "runtime changed after process start"):
                    indexd_runtime.rust_writer_env(binary)
                self.assertEqual(
                    os.environ["AGREP_RUNTIME_BUILD_ID"], BUILD_A)
                self.assertEqual(
                    os.environ["AGREP_DERIVED_WRITER_IDENTITY_BLOCKED"],
                    "prior refusal")

    def test_only_exact_current_daemon_authorizes_ownerless_adoption(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-daemon-token-") as raw:
            binary = Path(raw) / "agrep-rs"
            binary.write_bytes(b"exact writer binary")
            writer = indexd_runtime.derived_writer_build_id(binary)
            token = "c" * 32
            snapshot = ownerfile.Snapshot(
                (1, 2, 3, 4), 0.0,
                (
                    f"pid=1 start=birth protocol=2 package=0.2.0 "
                    f"build=python writer={writer} group=1 "
                    f"token={token} time=1.000\n"
                ).encode("ascii"),
            )
            compatible = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.COMPATIBLE,
                snapshot, 1, "birth")
            incompatible = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.INCOMPATIBLE,
                snapshot, 1, "birth")
            key = indexd_runtime._DERIVED_ADOPTION_OWNER_TOKEN_ENV
            with (
                mock.patch.dict(os.environ, {}, clear=False),
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=compatible),
            ):
                env = indexd_runtime.rust_writer_env(binary)
                self.assertEqual(env.get(key), token)
                with mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=incompatible):
                    fenced = indexd_runtime.rust_writer_env(binary)
                self.assertNotIn(key, fenced)
                self.assertNotIn(key, os.environ)

    def test_writer_launch_refuses_post_adoption_clobber_without_mutation(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-clobber-launch-") as raw:
            root = Path(raw)
            binary = root / "agrep-rs"
            binary.write_bytes(b"exact writer binary")
            with self._paths(root) as (_messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=None)
                before = self._census(root)
                with self.assertRaisesRegex(
                        OSError, "automatic repair is disabled"):
                    indexd_runtime.rust_writer_env(binary)
                self.assertEqual(self._census(root), before)

    def test_unreadable_ownership_records_refuse_in_the_owner_vocabulary(
            self) -> None:
        """An unreadable anchor is a verdict, never an unhandled exception.

        `build_index` is doctor's own advised remedy, so a diagnostic path may
        not hand the CLI's generic crash handler an OSError over a local
        condition the module already knows how to name."""
        corruptions = (
            ("zero-length anchor", ".derived-owner.json", b"",
             "ownership record"),
            ("truncated anchor", ".derived-owner.json", b'{"version":1',
             "ownership record"),
            ("zero-length cache", ".ingest_cache.bin", b"",
             "no matching writing-build identity"),
        )
        for label, name, body, expected in corruptions:
            with self.subTest(corruption=label):
                with tempfile.TemporaryDirectory(
                        prefix="agrep-owner-unreadable-") as raw:
                    root = Path(raw)
                    binary = root / "agrep-rs"
                    binary.write_bytes(b"exact writer binary")
                    with self._paths(root) as (_messages, db_path, owner_path):
                        self._owner(owner_path, BUILD_A)
                        self._cache(root / ".ingest_cache.bin", BUILD_A)
                        self._database(db_path, owner=BUILD_A)
                        (root / name).write_bytes(body)
                        before = self._census(root)
                        with self.assertRaises(
                                indexd_runtime.DerivedWriteFenced) as fenced:
                            indexd_runtime.rust_writer_env(binary)
                        self.assertFalse(fenced.exception.info.writable)
                        self.assertFalse(
                            indexd_runtime.derived_writer_launchable(
                                fenced.exception.info))
                        self.assertIn(expected, str(fenced.exception))
                        self.assertEqual(self._census(root), before)

    def test_build_index_declines_an_unreadable_anchor_without_crashing(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-declined-") as raw:
            root = Path(raw)
            binary = root / "agrep-rs"
            binary.write_bytes(b"exact writer binary")
            stderr = io.StringIO()
            with self._paths(root) as (_messages, db_path, owner_path):
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=BUILD_A)
                owner_path.write_bytes(b"")
                before = self._census(root)
                with (
                    mock.patch.object(
                        common, "ingest_bin", return_value=binary),
                    mock.patch.object(
                        indexd_runtime.subprocess, "run",
                        side_effect=AssertionError("ingest launched")),
                    contextlib.redirect_stderr(stderr),
                ):
                    self.assertIs(indexd_runtime.build_index(quiet=True), False)
                self.assertEqual(self._census(root), before)
            self.assertIn("indexing declined", stderr.getvalue())
            self.assertIn(".derived-owner.json", stderr.getvalue())

    def test_writer_launch_recomputes_after_binary_appears_and_changes(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-recompute-") as raw:
            binary = Path(raw) / "agrep-rs"
            with (
                mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_ID", BUILD_A),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                missing = indexd_runtime.derived_writer_build_id(binary)
                binary.write_bytes(b"A" * 4096)
                fixed_ns = 1_700_000_000_000_000_000
                os.utime(binary, ns=(fixed_ns, fixed_ns))
                first = indexd_runtime.rust_writer_env(binary)[
                    "AGREP_RUNTIME_BUILD_ID"]
                binary.write_bytes(b"B" * 4096)
                os.utime(binary, ns=(fixed_ns, fixed_ns))
                second = indexd_runtime.rust_writer_env(binary)[
                    "AGREP_RUNTIME_BUILD_ID"]
                self.assertEqual(
                    os.environ.get("AGREP_RUNTIME_BUILD_ID"), None)
            self.assertNotEqual(missing, first)
            self.assertNotEqual(first, second)

    def test_common_binary_resolution_is_read_only(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-resolve-") as raw:
            binary = Path(raw) / "agrep-rs"
            binary.write_bytes(b"A" * 4096)
            fixed_ns = 1_700_000_000_000_000_000
            os.utime(binary, ns=(fixed_ns, fixed_ns))
            with (
                mock.patch.dict(os.environ, {
                    "AGREP_RS_BIN": str(binary),
                    "AGREP_RUNTIME_BUILD_ID": BUILD_A,
                }, clear=False),
                mock.patch.object(
                    indexd_runtime, "rust_writer_env",
                    side_effect=AssertionError("reader prepared a writer")) as prepare,
            ):
                self.assertEqual(common.ingest_bin(), binary)
                before = os.environ["AGREP_RUNTIME_BUILD_ID"]
                binary.write_bytes(b"B" * 4096)
                os.utime(binary, ns=(fixed_ns, fixed_ns))
                self.assertEqual(common.ingest_bin(), binary)
                self.assertEqual(
                    os.environ["AGREP_RUNTIME_BUILD_ID"], before)
                prepare.assert_not_called()

    def test_direct_rust_launch_binds_the_inherited_python_build_to_its_binary(
            self) -> None:
        binary = self._release_binary()
        with tempfile.TemporaryDirectory(prefix="agrep-owner-direct-rust-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"direct writer identity","ts":1}]',
                encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                "APPDATA": os.fspath(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": os.fspath(home / "AppData" / "Local"),
                "XDG_CONFIG_HOME": os.fspath(home / ".config"),
                "XDG_DATA_HOME": os.fspath(home / ".local" / "share"),
                "CLINE_DIR": os.fspath(home / ".cline"),
                "AGREP_DATA_DIR_SOURCE": "env",
                "AGREP_NO_DAEMON": "1",
                "AGREP_NO_FETCH": "1",
                indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV: BUILD_A,
            })
            for key in (
                    "AGREP_RUNTIME_BUILD_ID",
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN"):
                env.pop(key, None)
            result = subprocess.run(
                [os.fspath(binary), "index", "--agent", "cline"],
                cwd=os.fspath(common.REPO_ROOT.resolve()), env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            owner = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
            with mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_ID", BUILD_A):
                expected = indexd_runtime.derived_writer_build_id(binary)
            self.assertEqual(owner["build_id"], expected)

    def test_direct_rust_launch_refuses_a_malformed_python_build_identity(
            self) -> None:
        binary = self._release_binary()
        with tempfile.TemporaryDirectory(prefix="agrep-owner-direct-refuse-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"must stay unpublished","ts":1}]',
                encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV: "not-a-build",
            })
            for key in (
                    "AGREP_RUNTIME_BUILD_ID",
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN"):
                env.pop(key, None)
            result = subprocess.run(
                [os.fspath(binary), "index", "--agent", "cline"],
                cwd=common.REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((data / ".derived-owner.json").exists())
            self.assertIn(
                "derived writer identity is unavailable", result.stderr)

    def test_explicit_child_identity_refuses_a_binary_swap_before_exec(
            self) -> None:
        source_binary = self._release_binary()
        executable = source_binary.name
        with tempfile.TemporaryDirectory(prefix="agrep-owner-exec-swap-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"swapped writer must refuse","ts":1}]',
                encoding="utf-8")
            binary_a = root / f"a-{executable}"
            binary_b = root / f"b-{executable}"
            shutil.copy2(source_binary, binary_a)
            shutil.copy2(source_binary, binary_b)
            with binary_b.open("ab") as stream:
                stream.write(b"\nforeign executable bytes")
            for binary in (binary_a, binary_b):
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                "AGREP_RUNTIME_BUILD_ID":
                    indexd_runtime.derived_writer_build_id(binary_a),
                indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV:
                    indexd_runtime.INDEXD_BUILD_ID,
            })
            for key in (
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN"):
                env.pop(key, None)
            result = subprocess.run(
                [os.fspath(binary_b), "index", "--agent", "cline"],
                cwd=common.REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((data / ".derived-owner.json").exists())
            self.assertIn(
                "does not match the Python runtime and running ingest executable",
                result.stderr)

    def test_readonly_data_dir_blocks_writer_preparation_before_owner_settlement(
            self) -> None:
        binary = common.ingest_bin()
        with tempfile.TemporaryDirectory(prefix="agrep-owner-readonly-prep-") as raw:
            root = Path(raw)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
                mock.patch.object(
                    indexd_runtime, "_settle_indexd_owner",
                    side_effect=AssertionError("protected owner was settled")) as settle,
                self.assertRaisesRegex(OSError, "AGREP_DATA_READONLY"),
            ):
                indexd_runtime.rust_writer_env(binary)
            settle.assert_not_called()

    def test_direct_rust_writer_honors_the_exact_readonly_data_dir(
            self) -> None:
        binary = self._release_binary()
        with tempfile.TemporaryDirectory(prefix="agrep-owner-rust-readonly-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"protected publication","ts":1}]',
                encoding="utf-8")
            data.mkdir()
            sentinel = data / "keep.bin"
            sentinel.write_bytes(b"exact protected bytes")
            before = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in data.iterdir()
            }
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_DATA_READONLY": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV:
                    indexd_runtime.INDEXD_BUILD_ID,
            })
            for key in (
                    "AGREP_RUNTIME_BUILD_ID",
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN"):
                env.pop(key, None)
            result = subprocess.run(
                [os.fspath(binary), "index", "--agent", "cline"],
                cwd=common.REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=30)
            after = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in data.iterdir()
            }
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(after, before)
            self.assertIn("AGREP_DATA_READONLY protects", result.stderr)

    def test_streamed_first_run_binds_a_binary_that_appears_after_import(
            self) -> None:
        source_binary = self._release_binary()
        with tempfile.TemporaryDirectory(prefix="agrep-owner-streamed-import-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            target = root / source_binary.name
            result_path = root / "result.json"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"streamed owner needle","ts":1}]',
                encoding="utf-8")
            script = """
import json
import os
import shutil
import stat
import sys
from pathlib import Path

target, source, result = map(Path, sys.argv[1:4])
import search
assert not target.exists()
shutil.copy2(source, target)
target.chmod(target.stat().st_mode | stat.S_IXUSR)
rc = search.main(["streamed owner needle", "--color", "never"])
owner = json.loads(
    (Path(os.environ["AGREP_DATA_DIR"]) / ".derived-owner.json").read_text(
        encoding="utf-8"))
result.write_text(
    json.dumps({"rc": rc, "owner": owner["build_id"]}),
    encoding="utf-8")
raise SystemExit(rc)
"""
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "AGREP_RS_BIN": os.fspath(target),
                "AGREP_NO_DAEMON": "1",
                "AGREP_PROFILE": "classic",
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                "PYTHONPATH": os.fspath(common.PY_DIR),
            })
            for key in (
                    "AGREP_RUNTIME_BUILD_ID",
                    indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV,
                    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN",
                    "AGREP_DATA_READONLY"):
                env.pop(key, None)
            for key in AGENT_CONTEXT_ENV_KEYS:
                env.pop(key, None)
            result = subprocess.run(
                [sys.executable, "-c", script, os.fspath(target),
                 os.fspath(source_binary), os.fspath(result_path)],
                cwd=common.REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(proof["rc"], 0)
            self.assertEqual(
                proof["owner"],
                indexd_runtime.derived_writer_build_id(target))
            self.assertIn("streamed owner needle", result.stdout)

    def test_build_index_passes_the_current_combined_id_to_rust(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-launch-") as raw:
            root = Path(raw)
            binary = root / "agrep-rs"
            binary.write_bytes(b"exact writer binary")
            messages = self._messages(root)
            with (
                mock.patch.object(
                    indexd_runtime, "INDEXD_BUILD_ID", BUILD_A),
                mock.patch.object(
                    common, "ingest_bin", return_value=binary),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(
                    indexd_runtime, "_data_dir_readonly", return_value=False),
                mock.patch.object(
                    indexd_runtime, "refresh_search_index",
                    return_value=True),
                mock.patch.object(
                    indexd_runtime.subprocess, "run",
                    return_value=types.SimpleNamespace(returncode=0),
                ) as run,
                mock.patch.object(
                    indexer, "run_post_index_hooks", return_value=None),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                expected = indexd_runtime.derived_writer_build_id(binary)
                self.assertTrue(indexd_runtime.build_index(quiet=True))
            self.assertEqual(
                run.call_args.kwargs["env"]["AGREP_RUNTIME_BUILD_ID"],
                expected)

    def test_anchor_reader_is_strict_bounded_and_no_follow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-anchor-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, _db, owner_path):
                self._owner(owner_path, BUILD_A)
                self.assertEqual(
                    indexd_runtime.derived_owner_info().state, "current")

                self._owner(owner_path, BUILD_B)
                foreign = indexd_runtime.derived_owner_info()
                self.assertEqual(foreign.state, "foreign")
                self.assertIn(f"owned-by {BUILD_B}", foreign.reason)

                owner_path.write_text(json.dumps({
                    "version": 1,
                    "build_id": BUILD_A,
                    "unexpected": True,
                }), encoding="utf-8")
                self.assertEqual(
                    indexd_runtime.derived_owner_info().state, "unavailable")

                owner_path.write_bytes(b"{" + b"x" * 5000 + b"}")
                self.assertEqual(
                    indexd_runtime.derived_owner_info().state, "unavailable")

                if os.name != "nt":
                    owner_path.unlink()
                    target = root / "owner-target.json"
                    self._owner(target, BUILD_A)
                    owner_path.symlink_to(target)
                    self.assertEqual(
                        indexd_runtime.derived_owner_info().state,
                        "unavailable")

                else:
                    owner_path.unlink()
                    target = root / "owner-target"
                    target.mkdir()
                    created = subprocess.run(
                        [
                            "cmd.exe", "/d", "/c", "mklink", "/J",
                            os.fspath(owner_path), os.fspath(target),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(
                        created.returncode, 0,
                        created.stdout + created.stderr)
                    reparse = getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    self.assertTrue(
                        getattr(
                            owner_path.lstat(),
                            "st_file_attributes",
                            0,
                        ) & reparse)
                    self.assertEqual(
                        indexd_runtime.derived_owner_info().state,
                        "unavailable")

    def test_anchor_rejects_two_migration_authorities(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-dual-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._database(db_path, owner=BUILD_B)
                proof = self._legacy_proof(db_path)
                identity = corpusdb._proof_file_identity(db_path)
                owner_path.write_text(json.dumps({
                    "version": 1,
                    "build_id": BUILD_A,
                    "legacy_corpus_db": proof,
                    "retained_corpus_db": {
                        "build_id": BUILD_B,
                        "proof": proof,
                        "reader_identity": dict(zip(
                            ("len", "modified_ns", "changed_ns",
                             "device", "inode"),
                            identity,
                            strict=True,
                        )),
                    },
                }, separators=(",", ":")), encoding="utf-8")
                observed = indexd_runtime.derived_owner_info()
                self.assertEqual(observed.state, "unavailable")
                self.assertIn("is malformed", observed.reason)

    def test_parse_cache_owner_probe_is_bounded_stable_and_no_follow(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-cache-owner-") as raw:
            root = Path(raw)
            cache_path = root / ".ingest_cache.bin"
            with self._paths(root) as (_messages, _db, _owner):
                self._cache(cache_path, BUILD_A)
                cache_path.write_bytes(
                    cache_path.read_bytes() + b"payload-not-probed" * 1024)
                current = indexd_runtime.ingest_cache_owner_info()
                self.assertEqual(current.state, "current")
                self.assertEqual(current.build_id, BUILD_A)

                self._cache(cache_path, BUILD_B)
                foreign = indexd_runtime.ingest_cache_owner_info()
                self.assertEqual(foreign.state, "foreign")
                self.assertEqual(
                    foreign.reason,
                    f"parse cache owned-by {BUILD_B}; "
                    f"this build is {BUILD_A}")

                cache_path.write_bytes(
                    b"\0" * 12 + b"AGRPCB01" + (4).to_bytes(4, "little")
                    + b"not-a-lower-hex-owner")
                self.assertEqual(
                    indexd_runtime.ingest_cache_owner_info().state,
                    "malformed")

                if os.name != "nt":
                    cache_path.unlink()
                    target = root / "cache-target.bin"
                    self._cache(target, BUILD_A)
                    cache_path.symlink_to(target)
                    self.assertEqual(
                        indexd_runtime.ingest_cache_owner_info().state,
                        "unavailable")
                else:
                    cache_path.unlink()
                    target = root / "cache-target"
                    target.mkdir()
                    created = subprocess.run(
                        [
                            "cmd.exe", "/d", "/c", "mklink", "/J",
                            os.fspath(cache_path), os.fspath(target),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(
                        created.returncode, 0,
                        created.stdout + created.stderr)
                    reparse = getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    self.assertTrue(
                        getattr(
                            cache_path.lstat(),
                            "st_file_attributes",
                            0,
                        ) & reparse)
                    self.assertEqual(
                        indexd_runtime.ingest_cache_owner_info().state,
                        "unavailable")

    def test_low_layer_mutation_preflight_preserves_only_authorized_states(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-preflight-") as raw:
            root = Path(raw)
            cache = root / ".ingest_cache.bin"
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, owner_path):
                absent = indexd_runtime.derived_mutation_info()
                self.assertTrue(absent.writable)
                self.assertEqual(absent.state, "absent")

                cache.write_bytes(b"pre-owner legacy parse cache")
                legacy = indexd_runtime.derived_mutation_info()
                self.assertTrue(legacy.writable)
                self.assertEqual(legacy.state, "legacy")
                semantic_legacy = (
                    indexd_runtime.derived_writer_mutation_info())
                self.assertFalse(semantic_legacy.writable)
                rust_legacy = indexd_runtime.derived_writer_mutation_info(
                    allow_legacy_adoption=True)
                self.assertTrue(rust_legacy.writable)
                self.assertEqual(rust_legacy.state, "legacy")

                self._database(db_path, owner=BUILD_A)
                foreign_db = indexd_runtime.derived_writer_mutation_info(
                    allow_legacy_adoption=True)
                self.assertFalse(foreign_db.writable)
                self.assertEqual(foreign_db.state, "foreign")
                self.assertEqual(foreign_db.build_id, BUILD_A)
                db_path.unlink()

                self._database(db_path, owner=None)
                semantic_ownerless = (
                    indexd_runtime.derived_writer_mutation_info())
                self.assertFalse(semantic_ownerless.writable)
                rust_ownerless = indexd_runtime.derived_writer_mutation_info(
                    allow_legacy_adoption=True)
                self.assertTrue(rust_ownerless.writable)
                self.assertEqual(rust_ownerless.state, "legacy")

                self._cache(cache, BUILD_B)
                self._owner(owner_path, BUILD_B)
                current = indexd_runtime.derived_mutation_info()
                self.assertTrue(current.writable)
                self.assertEqual(current.state, "current")

                cache.write_bytes(b"older writer resumed")
                resumed = indexd_runtime.derived_mutation_info()
                self.assertFalse(resumed.writable)
                self.assertEqual(resumed.state, "unavailable")
                self.assertIn(
                    "parse cache is legacy and has no matching "
                    "writing-build identity",
                    resumed.reason)

                self._cache(cache, BUILD_A)
                foreign = indexd_runtime.derived_mutation_info()
                self.assertFalse(foreign.writable)
                self.assertEqual(foreign.state, "foreign")
                self.assertIn(f"owned-by {BUILD_A}", foreign.reason)

                cache.write_bytes(
                    b"\0" * 12 + b"AGRPCB01" + (4).to_bytes(4, "little")
                    + b"not-a-lower-hex-owner")
                unavailable = indexd_runtime.derived_mutation_info()
                self.assertFalse(unavailable.writable)
                self.assertEqual(unavailable.state, "unavailable")

    def test_foreign_build_fences_daemon_semantic_and_housekeeping_writers(
            self) -> None:
        class Watcher:
            _last_event_wall = 0.0

        snapshot = ownerfile.Snapshot((1, 2, 3, 4), 0.0, b"owner")
        with tempfile.TemporaryDirectory(prefix="agrep-owner-all-writers-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, _db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                (root / "keep.bin").write_bytes(b"same bytes")
                response_probe = root / ".indexd.probe"
                response_probe.write_bytes(b"retain probe")
                before = self._census(root)
                # live foreign owner: these fences hold; a dead
                # owner's claim is reaped instead (DeadOwnerReapTests)
                with (
                    mock.patch.object(
                        indexd_runtime, "live_indexer_claim",
                        return_value=True),
                    mock.patch.object(
                        indexd_runtime, "_INDEXD_RESPONSE_PATH",
                        response_probe),
                    mock.patch.object(
                        indexd_runtime, "_clear_own_spawn_guard",
                        side_effect=AssertionError("foreign spawn guard mutated")),
                    mock.patch.object(
                        indexd_runtime, "_settle_indexd_owner",
                        side_effect=AssertionError("foreign owner settled")),
                    mock.patch.object(
                        indexd_runtime.subprocess, "Popen",
                        side_effect=AssertionError("foreign daemon spawned")),
                    mock.patch.object(
                        indexd_runtime, "indexd_failure_state",
                        return_value=(0, "", 0.0)),
                    mock.patch.object(
                        indexd_runtime, "auto_index_escalated",
                        return_value=False),
                    mock.patch.object(
                        common, "open_bounded_log",
                        side_effect=AssertionError("foreign log opened")),
                    mock.patch.object(
                        semantic.subprocess, "Popen",
                        side_effect=AssertionError("foreign semantic child spawned")),
                    mock.patch.object(
                        indexer, "configured_post_index_hooks",
                        side_effect=AssertionError("foreign hook configured")),
                    mock.patch.object(
                        indexer.AutoIndexer, "_reassert_teach",
                        side_effect=AssertionError("foreign teach ran")),
                    mock.patch.object(
                        indexer.AutoIndexer, "_capture_archive",
                        side_effect=AssertionError("foreign archive ran")),
                    mock.patch.object(
                        indexer.AutoIndexer, "_refresh_embeddings",
                        side_effect=AssertionError("foreign embed housekeeping ran")),
                    mock.patch.object(
                        indexer.AutoIndexer, "_launch_index_process",
                        side_effect=AssertionError("foreign ingest launched")),
                    mock.patch.object(
                        indexd, "_acquire",
                        side_effect=AssertionError("foreign daemon acquired")),
                    mock.patch.object(
                        indexer, "configure_indexd_mode",
                        side_effect=AssertionError("foreign daemon configured")),
                ):
                    self.assertIs(
                        indexd_runtime._spawn_indexd(),
                        indexd_runtime._IndexdSpawnResult.BLOCKED)
                    self.assertFalse(indexd_runtime.freshener_alive())
                    self.assertEqual(
                        indexd_runtime.indexd_resource_status()["state"],
                        "derived-store-owner")
                    self.assertIsNone(indexd_runtime.acquire_indexd_owner())
                    daemon_owner = mock.Mock()
                    with self.assertRaisesRegex(
                            ownerfile.OwnershipLost, f"owned-by {BUILD_A}"):
                        indexd_runtime.heartbeat_indexd_owner(daemon_owner)
                    with self.assertRaisesRegex(
                            ownerfile.OwnershipLost, f"owned-by {BUILD_A}"):
                        indexd_runtime.publish_indexd_ready(daemon_owner)
                    daemon_owner.touch.assert_not_called()
                    daemon_owner.verify.assert_not_called()
                    indexd_runtime.record_auto_index_health(
                        1, "must not persist")
                    indexd_runtime._clear_indexd_response_probe()
                    self.assertFalse(indexd_runtime._write_indexd_response_probe(
                        {"fixture": True}))
                    self.assertEqual(indexd.main(), 0)
                    indexer.run_post_index_hooks()
                    auto = indexer.AutoIndexer(
                        Watcher(), owns_lifetime=lambda: True,
                        owner_snapshot=snapshot)
                    auto._run_housekeeping(time.monotonic())
                    auto._index()

                    semantic.note_semantic_use()
                    for outcome in (
                            semantic.ensure_fresh_async(),
                            semantic.ensure_refs_async(),
                            semantic.refresh_embeddings_sync(),
                            semantic.request_full_rebuild("foreign integrity")):
                        self.assertEqual(outcome["state"], "read-only")
                        self.assertIn(f"owned-by {BUILD_A}", outcome["reason"])
                    self.assertEqual(
                        semantic.stop_background_writers_for_removal()["state"],
                        "read-only")
                    with self.assertRaisesRegex(
                            OSError, f"owned-by {BUILD_A}"):
                        semantic.write_generation_marker({"fixture": True})
                self.assertEqual(self._census(root), before)

    def test_foreign_database_fences_daemon_and_semantic_entrypoints(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-foreign-db-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_A) as (
                    _messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=BUILD_B)
                low = indexd_runtime.derived_mutation_info()
                self.assertTrue(low.writable)
                self.assertEqual(low.state, "current")
                high = indexd_runtime.derived_writer_mutation_info()
                self.assertFalse(high.writable)
                self.assertEqual(high.state, "foreign")
                self.assertEqual(high.build_id, BUILD_B)
                self.assertIn(f"corpus.db owned-by {BUILD_B}", high.reason)
                before = self._census(root)
                # live foreign owner: these fences hold; a dead
                # owner's claim is reaped instead (DeadOwnerReapTests)
                with (
                    mock.patch.object(
                        indexd_runtime, "live_indexer_claim",
                        return_value=True),
                    mock.patch.object(
                        indexd_runtime, "_clear_own_spawn_guard",
                        side_effect=AssertionError("foreign DB spawn mutated")),
                    mock.patch.object(
                        indexd_runtime.subprocess, "Popen",
                        side_effect=AssertionError("foreign DB daemon spawned")),
                    mock.patch.object(
                        common, "open_bounded_log",
                        side_effect=AssertionError("foreign DB log opened")),
                    mock.patch.object(
                        semantic.subprocess, "Popen",
                        side_effect=AssertionError(
                            "foreign DB semantic child spawned")),
                    mock.patch.object(
                        indexer, "configured_post_index_hooks",
                        side_effect=AssertionError("foreign DB hook configured")),
                    mock.patch.object(
                        indexd, "_acquire",
                        side_effect=AssertionError("foreign DB daemon acquired")),
                    mock.patch.object(
                        indexer, "configure_indexd_mode",
                        side_effect=AssertionError("foreign DB daemon configured")),
                ):
                    self.assertIs(
                        indexd_runtime._spawn_indexd(),
                        indexd_runtime._IndexdSpawnResult.BLOCKED)
                    self.assertEqual(indexd.main(), 0)
                    indexer.run_post_index_hooks()
                    semantic.note_semantic_use()
                    for outcome in (
                            semantic.ensure_fresh_async(),
                            semantic.ensure_refs_async(),
                            semantic.refresh_embeddings_sync(),
                            semantic.request_full_rebuild("foreign DB")):
                        self.assertEqual(outcome["state"], "read-only")
                        self.assertIn(
                            f"corpus.db owned-by {BUILD_B}",
                            outcome["reason"])
                self.assertEqual(self._census(root), before)

    def test_foreign_database_dominates_legacy_cache_for_every_writer_lane(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-legacy-db-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, _owner_path):
                (root / ".ingest_cache.bin").write_bytes(
                    b"pre-owner legacy parse cache")
                self._database(db_path, owner=BUILD_A)
                binary = root / "agrep-rs"
                binary.write_bytes(b"exact binary")
                before = self._census(root)
                low = indexd_runtime.derived_mutation_info()
                self.assertEqual(low.state, "legacy")
                high = indexd_runtime.derived_writer_mutation_info(
                    allow_legacy_adoption=True)
                self.assertFalse(high.writable)
                self.assertEqual(high.state, "foreign")
                self.assertEqual(high.build_id, BUILD_A)
                # live foreign owner: these fences hold; a dead
                # owner's claim is reaped instead (DeadOwnerReapTests)
                with (
                    mock.patch.object(
                        indexd_runtime, "live_indexer_claim",
                        return_value=True),
                    mock.patch.object(
                        indexd_runtime, "_clear_own_spawn_guard",
                        side_effect=AssertionError("foreign DB spawn mutated")),
                    mock.patch.object(
                        indexd_runtime, "_settle_indexd_owner",
                        side_effect=AssertionError("foreign DB owner settled")),
                    mock.patch.object(
                        indexd_runtime.subprocess, "Popen",
                        side_effect=AssertionError("foreign DB daemon spawned")),
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": ""}, clear=False),
                ):
                    self.assertIs(
                        indexd_runtime._spawn_indexd(),
                        indexd_runtime._IndexdSpawnResult.BLOCKED)
                    outcome = semantic.ensure_fresh_async()
                    self.assertEqual(outcome["state"], "read-only")
                    self.assertIn(
                        f"corpus.db owned-by {BUILD_A}", outcome["reason"])
                    with self.assertRaisesRegex(
                            OSError, f"corpus.db owned-by {BUILD_A}"):
                        semantic.write_generation_marker({"fixture": True})
                # The launch preflight delegates a foreign family to the Rust
                # writer, which holds the locks and either takes over a dead
                # owner or serves read-only; preparing its env mutates nothing.
                env = indexd_runtime.rust_writer_env(binary)
                self.assertEqual(
                    env["AGREP_RUNTIME_BUILD_ID"],
                    indexd_runtime.derived_writer_build_id(binary))
                self.assertEqual(self._census(root), before)

    def test_foreign_database_refresh_is_a_fenced_success_not_a_retry(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-refresh-db-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_A) as (
                    _messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=BUILD_B)
                before = self._census(root)
                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "connect", return_value=None) as connect,
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("foreign DB incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("foreign DB rebuilt")),
                ):
                    self.assertIs(
                        indexd_runtime.refresh_search_index(quiet=True), True)
                connect.assert_called_once_with(quiet=True)
                self.assertEqual(self._census(root), before)

    def test_reindex_foreign_preflight_and_signature_helpers_preserve_census(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-reindex-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_A) as (
                    _messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=BUILD_B)
                binary = root / "agrep-rs"
                binary.write_bytes(b"binary")
                signature = root / ".reindex.sig"
                signature.write_bytes(b"retain signature\n")
                before = self._census(root)
                with (
                    mock.patch.object(
                        sys, "argv", ["reindex.py", "--no-build"]),
                    mock.patch.object(
                        common, "ingest_bin", return_value=binary),
                    mock.patch.object(
                        reindex, "run",
                        side_effect=AssertionError("foreign reindex launched")),
                ):
                    self.assertEqual(reindex.main(), 1)
                    with self.assertRaisesRegex(
                            PermissionError,
                            f"corpus.db owned-by {BUILD_B}"):
                        reindex._write_sig(signature, "replacement")
                    with self.assertRaisesRegex(
                            PermissionError,
                            f"corpus.db owned-by {BUILD_B}"):
                        reindex._publish_completion_signature(
                            signature, "replacement", {"coherent": True})
                self.assertEqual(self._census(root), before)

    def test_reindex_signature_helpers_refuse_protected_descendants(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-reindex-protected-") as raw:
            protected = Path(raw) / "protected"
            nested = protected / "nested"
            nested.mkdir(parents=True)
            signature = nested / ".reindex.sig"
            signature.write_bytes(b"retain\n")
            before = self._census(protected)
            with mock.patch.dict(
                    os.environ,
                    {"AGREP_DATA_READONLY": os.fspath(protected)},
                    clear=False):
                with self.assertRaisesRegex(
                        PermissionError, "AGREP_DATA_READONLY"):
                    reindex._write_sig(signature, "replacement")
                with self.assertRaisesRegex(
                        PermissionError, "AGREP_DATA_READONLY"):
                    reindex._publish_completion_signature(
                        signature, "", {"coherent": False})
            self.assertEqual(self._census(protected), before)

    def test_cache_owner_survives_anchor_crash_window_and_fences_db_writer(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-cache-crash-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, _owner_path):
                self._messages(root)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "_stale_db",
                        side_effect=AssertionError(
                            "foreign reader opened SQLite")),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        side_effect=AssertionError("foreign cache swept")),
                    mock.patch.object(
                        corpusdb, "_valid_db",
                        side_effect=AssertionError("foreign cache validated")),
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("foreign cache incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("foreign cache rebuilt")),
                    mock.patch.object(
                        common, "IndexLock",
                        side_effect=AssertionError("foreign cache locked")),
                    contextlib.redirect_stderr(stderr),
                ):
                    indexd_runtime._clear_freshen_failure()
                    self.assertIs(
                        indexd_runtime.refresh_search_index(quiet=True), True)
                    with mock.patch.object(
                            indexd_runtime, "indexing_failure",
                            return_value=None):
                        disclosure = indexd_runtime.machine_freshness(
                            checked=False)
                self.assertFalse(db_path.exists())
                # The fence is recorded for the one story line and
                # machine surfaces; the read lane itself prints nothing
                self.assertNotIn("may be stale", stderr.getvalue())
                self.assertIn(
                    f"parse cache owned-by {BUILD_A}; "
                    f"this build is {BUILD_B}",
                    disclosure["reason"])

    def test_python_never_adopts_without_current_rust_ownership_proof(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-no-proof-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, _owner_path):
                absent = corpusdb._derived_write_ownership()
                self.assertEqual(absent.state, "refused")
                self.assertIn("ownership is not established", absent.reason)

                self._database(db_path, owner=BUILD_A)
                foreign_db = corpusdb._derived_write_ownership()
                self.assertEqual(foreign_db.state, "refused")
                self.assertFalse(foreign_db.writable)

                db_path.unlink()
                self._cache(root / ".ingest_cache.bin", BUILD_B)
                authorized = corpusdb._derived_write_ownership()
                self.assertEqual(authorized.state, "adoption")
                self.assertTrue(authorized.writable)
                db_path.write_bytes(b"damaged sqlite bytes")
                repairable = corpusdb._derived_write_ownership()
                self.assertEqual(repairable.state, "adoption")
                self.assertTrue(repairable.writable)

    def test_current_anchor_refuses_an_existing_ownerless_cache(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-repair-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._database(db_path, owner=BUILD_A)
                (root / ".ingest_cache.bin").write_bytes(
                    b"\0" * 12 + b"AGRPCB01" + (4).to_bytes(4, "little")
                    + b"not-an-owner")
                ownership = corpusdb._derived_write_ownership()
                self.assertEqual(ownership.state, "refused")
                self.assertFalse(ownership.writable)
                self.assertIn(
                    "parse cache ownership is malformed", ownership.reason)

    def test_current_anchor_repairs_matching_cache_payload_corruption(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-repair-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._database(db_path, owner=BUILD_A)
                cache = root / ".ingest_cache.bin"
                self._cache(cache, BUILD_A)
                cache.write_bytes(cache.read_bytes() + b"corrupt payload")
                ownership = corpusdb._derived_write_ownership()
                self.assertEqual(ownership.state, "current")
                self.assertTrue(ownership.writable)

    def test_current_anchor_repairs_non_file_main_but_fences_non_file_sidecar(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-db-shape-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)

                db_path.mkdir()
                state, _owner, _reason = corpusdb._database_build_id()
                self.assertEqual(state, "unavailable")
                ownership = indexd_runtime.derived_writer_mutation_info()
                self.assertEqual(ownership.state, "current")
                self.assertTrue(ownership.writable)

                db_path.rmdir()
                self._database(db_path, owner=BUILD_A)
                Path(f"{db_path}-wal").mkdir()
                state, _owner, _reason = corpusdb._database_build_id()
                self.assertEqual(state, "uncertain")
                ownership = indexd_runtime.derived_writer_mutation_info()
                self.assertEqual(ownership.state, "unavailable")
                self.assertFalse(ownership.writable)

    def test_current_anchor_rebuilds_a_damaged_same_build_database(
            self) -> None:
        @contextlib.contextmanager
        def granted_lock(_blocking: bool):
            yield True

        with tempfile.TemporaryDirectory(prefix="agrep-owner-db-repair-") as raw:
            root = Path(raw)
            with self._paths(root) as (messages, db_path, owner_path):
                self._messages(root)
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                db_path.write_bytes(b"damaged sqlite bytes")
                ownership = indexd_runtime.derived_writer_mutation_info()
                self.assertTrue(ownership.writable)
                self.assertEqual(ownership.state, "current")

                def rebuild(dst: Path, stamp: str) -> None:
                    self._database(dst, owner=BUILD_A, stamp=stamp)

                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "query_rebuild_required",
                        return_value=False),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        return_value={"removed": 0, "removed_bytes": 0}),
                    mock.patch.object(
                        corpusdb, "_ConnectIndexLock", granted_lock),
                    mock.patch.object(
                        corpusdb, "_read_changed", return_value="*"),
                    mock.patch.object(
                        corpusdb, "_incremental", return_value=None),
                    mock.patch.object(
                        corpusdb, "_build", side_effect=rebuild) as build,
                ):
                    refreshed = indexd_runtime.refresh_search_index(quiet=True)
                self.assertIs(refreshed, True)
                build.assert_called_once()
                self.assertEqual(_db_meta(db_path, "build_id"), BUILD_A)
                self.assertTrue(messages.exists())

    def test_foreign_reader_serves_snapshot_before_every_writer_primitive(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-foreign-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (messages, db_path, owner_path):
                self._messages(root)
                self._database(db_path, owner=BUILD_A)
                self._owner(owner_path, BUILD_A)
                before = (
                    db_path.read_bytes(),
                    corpusdb._proof_file_identity(db_path),
                )
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        side_effect=AssertionError("foreign reader swept")),
                    mock.patch.object(
                        corpusdb, "_valid_db",
                        side_effect=AssertionError("foreign reader validated")),
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("foreign reader incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("foreign reader rebuilt")),
                    mock.patch.object(
                        common, "IndexLock",
                        side_effect=AssertionError("foreign reader locked")),
                    contextlib.redirect_stderr(stderr),
                ):
                    indexd_runtime._clear_freshen_failure()
                    got = corpusdb.connect(quiet=True)
                    self.assertIsNotNone(got)
                    try:
                        self.assertEqual(
                            got.execute(
                                "SELECT text FROM msgs WHERE session='s'"
                            ).fetchone(),
                            ("owned snapshot needle",),
                        )
                    finally:
                        got.close()
                    with mock.patch.object(
                            indexd_runtime, "indexing_failure",
                            return_value=None):
                        disclosure = indexd_runtime.machine_freshness(
                            checked=False)

                self.assertEqual(
                    (db_path.read_bytes(),
                     corpusdb._proof_file_identity(db_path)), before)
                # Recorded for the one story line, never self-printed
                self.assertNotIn("may be stale", stderr.getvalue())
                # the recorded reason is the CAUSE alone (law 5): which lane
                # served is --json's engine fact, not this record's clause
                self.assertIn(f"owned-by {BUILD_A}", disclosure["reason"])
                self.assertTrue(messages.exists())
                self.assertIn(
                    "owned snapshot needle",
                    messages.read_text(encoding="utf-8"),
                )

    def test_current_anchor_reads_a_replaced_foreign_db_without_writing(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-replaced-db-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_A) as (
                    messages, db_path, owner_path):
                self._messages(root)
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=BUILD_B)
                before = (
                    db_path.read_bytes(),
                    corpusdb._proof_file_identity(db_path),
                )
                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        side_effect=AssertionError("foreign reader swept")),
                    mock.patch.object(
                        corpusdb, "_valid_db",
                        side_effect=AssertionError("foreign reader validated")),
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("foreign reader incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("foreign reader rebuilt")),
                    mock.patch.object(
                        common, "IndexLock",
                        side_effect=AssertionError("foreign reader locked")),
                    contextlib.redirect_stderr(stderr),
                ):
                    indexd_runtime._clear_freshen_failure()
                    got = corpusdb.connect(quiet=True, allow_stale=True)
                    self.assertIsNotNone(got)
                    try:
                        self.assertEqual(
                            got.execute(
                                "SELECT text FROM msgs WHERE session='s'"
                            ).fetchone(),
                            ("owned snapshot needle",),
                        )
                    finally:
                        got.close()
                    with mock.patch.object(
                            indexd_runtime, "indexing_failure",
                            return_value=None):
                        disclosure = indexd_runtime.machine_freshness(
                            checked=False)

                self.assertEqual(
                    (db_path.read_bytes(),
                     corpusdb._proof_file_identity(db_path)), before)
                # Recorded for the one story line, never self-printed
                self.assertNotIn("may be stale", stderr.getvalue())
                self.assertIn(
                    f"corpus.db owned-by {BUILD_B}; this build is {BUILD_A}",
                    disclosure["reason"])
                self.assertTrue(messages.exists())

    def test_wal_owner_probe_never_creates_source_shm_or_mutates_store(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-wal-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, owner_path):
                # The current parse cache is the only local witness. The
                # foreign database owner exists solely in WAL, so adoption
                # must inspect the complete private SQLite family.
                self._cache(root / ".ingest_cache.bin", BUILD_B)

                # Freeze a legitimate main+WAL publication without copying its
                # shared-memory file. A read-only SQLite open of db_path would
                # create corpus.db-shm in the live derived-store directory.
                seed = root / "seed.db"
                with contextlib.closing(sqlite3.connect(seed)) as writer:
                    self.assertEqual(
                        writer.execute(
                            "PRAGMA journal_mode=WAL").fetchone()[0],
                        "wal")
                    writer.execute("PRAGMA wal_autocheckpoint=0")
                    writer.execute(
                        "CREATE TABLE meta("
                        "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    writer.execute(
                        "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                        (BUILD_A,))
                    writer.commit()
                    seed_wal = Path(f"{seed}-wal")
                    self.assertTrue(seed_wal.exists())
                    shutil.copyfile(seed, db_path)
                    db_wal = Path(f"{db_path}-wal")
                    shutil.copyfile(seed_wal, db_wal)

                db_shm = Path(f"{db_path}-shm")
                self.assertFalse(db_shm.exists())
                before_names = set(root.iterdir())
                before = {
                    path: (
                        path.read_bytes(),
                        corpusdb._proof_file_identity(path),
                    )
                    for path in (db_path, db_wal)
                }

                state, observed_owner, _error = corpusdb._database_build_id()
                self.assertEqual((state, observed_owner), ("owned", BUILD_A))
                foreign = corpusdb._derived_write_ownership()
                self.assertEqual(foreign.state, "refused")
                self.assertIn(
                    f"corpus.db owned-by {BUILD_A}", foreign.reason)

                self.assertFalse(
                    db_shm.exists(),
                    "ownership preflight created a live SQLite SHM file")
                self.assertEqual(set(root.iterdir()), before_names)
                for path, snapshot in before.items():
                    self.assertEqual(
                        (path.read_bytes(),
                         corpusdb._proof_file_identity(path)),
                        snapshot)

                # A foreground read trusts the current durable witness without
                # paying or mutating a SQLite ownership probe. A writer must
                # still inspect the explicit DB owner and fence this family.
                self._owner(owner_path, BUILD_B)
                with mock.patch.object(
                        corpusdb, "_database_build_id",
                        side_effect=AssertionError(
                            "foreground current owner opened corpus.db")):
                    current = corpusdb._derived_write_ownership()
                self.assertEqual(current.state, "current")
                self.assertTrue(current.writable)

                refused = corpusdb._derived_write_ownership(for_write=True)
                self.assertEqual(refused.state, "refused")
                self.assertIn(
                    f"corpus.db owned-by {BUILD_A}", refused.reason)
                self.assertFalse(
                    db_shm.exists(),
                    "ownership preflight created a live SQLite SHM file")
                for path, snapshot in before.items():
                    self.assertEqual(
                        (path.read_bytes(),
                         corpusdb._proof_file_identity(path)),
                        snapshot)

    def test_protected_enrichment_readers_never_open_a_foreign_database(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-enrichment-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (
                    _messages, db_path, owner_path):
                self._messages(root)
                self._owner(owner_path, BUILD_B)
                self._cache(root / ".ingest_cache.bin", BUILD_B)
                self._database(db_path, owner=BUILD_A)
                explore._primary_models.cache_clear()

                def snapshot() -> tuple:
                    found = root.stat()
                    entries = tuple(
                        (
                            path.name, path.read_bytes(), path.stat().st_mode,
                            path.stat().st_size, path.stat().st_mtime_ns,
                            path.stat().st_ctime_ns,
                        )
                        for path in sorted(root.iterdir())
                    )
                    return (
                        found.st_mode, found.st_mtime_ns, found.st_ctime_ns,
                        entries,
                    )

                before = snapshot()
                with (
                    mock.patch.object(session_context, "DATA_DIR", root),
                    mock.patch.object(
                        session_context, "session_family_source_stamp",
                        return_value="family"),
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": os.fspath(root)},
                        clear=False),
                    mock.patch.object(
                        corpusdb, "_connect_read_snapshot",
                        side_effect=AssertionError(
                            "foreign enrichment opened corpus.db")) as opened,
                ):
                    self.assertEqual(explore._primary_models(("s",)), {})
                    self.assertIsNone(
                        session_context._open_session_family_index())
                opened.assert_not_called()
                self.assertEqual(snapshot(), before)
                self.assertFalse(Path(f"{db_path}-shm").exists())
                explore._primary_models.cache_clear()

    def test_protected_private_corpus_writers_refuse_before_mutation(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-private-writers-") as raw:
            root = Path(raw)
            with self._paths(root) as (
                    _messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                changed = root / ".changed_sessions"
                changed.write_bytes(b"s\n")
                rebuild = root / ".corpusdb-rebuild"
                rebuild.write_bytes(b"pending\n")
                orphan = root / "corpus.db.999999999.1.tmp"
                orphan.write_bytes(b"orphan")

                def snapshot() -> tuple:
                    found = root.stat()
                    entries = tuple(
                        (
                            path.name, path.read_bytes(), path.stat().st_mode,
                            path.stat().st_size, path.stat().st_mtime_ns,
                            path.stat().st_ctime_ns,
                        )
                        for path in sorted(root.iterdir())
                    )
                    return (
                        found.st_mode, found.st_mtime_ns, found.st_ctime_ns,
                        entries,
                    )

                before = snapshot()
                with (
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": os.fspath(root)},
                        clear=False),
                    mock.patch.object(common, "pid_alive", return_value=False),
                ):
                    swept = corpusdb.purge_legacy_build_temps()
                    corpusdb._consume_changed()
                    corpusdb._cleanup_tmp(orphan)
                    corpusdb._clear_query_rebuild_request()
                    self.assertIsNone(corpusdb._incremental("stamp"))
                    self.assertFalse(
                        corpusdb._adopt_legacy_database_owner())
                    with self.assertRaisesRegex(
                            OSError, "AGREP_DATA_READONLY"):
                        corpusdb._build(
                            root / "corpus.db.1.2.tmp", "stamp")
                self.assertEqual(swept["removed"], 0)
                self.assertEqual(swept["deferred"], 1)
                self.assertEqual(snapshot(), before)
                self.assertFalse(db_path.exists())

    def test_sqlite_snapshot_uses_native_change_identity(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-native-identity-") as raw:
            path = Path(raw) / "corpus.db"
            path.write_bytes(b"same-size SQLite fixture")
            size = path.stat().st_size
            native_before = (11, 22, size, 33, 44)
            native_after = (11, 22, size, 33, 45)
            expected = corpusdb._native_file_identity(native_before)
            with (
                mock.patch.object(
                    corpusdb.fileops, "file_identity_fd",
                    side_effect=[native_before, native_after]),
                mock.patch.object(
                    corpusdb.fileops, "file_identity",
                    return_value=native_before),
            ):
                with self.assertRaisesRegex(
                        OSError, "changed while reading"):
                    with corpusdb._open_sqlite_file(
                            path, expected) as stream:
                        self.assertEqual(
                            stream.read(), b"same-size SQLite fixture")

    def test_codeless_sqlite_failure_never_authorizes_writer_repair(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-codeless-sqlite-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._database(db_path, owner=BUILD_A)
                self._owner(owner_path, BUILD_A)
                error = sqlite3.DatabaseError("database is locked")
                self.assertIsNone(
                    getattr(error, "sqlite_errorcode", None),
                    "the negative control must model Python 3.10")
                connection = mock.MagicMock()
                connection.execute.side_effect = error
                with mock.patch.object(
                        corpusdb, "_open", return_value=connection):
                    state = corpusdb._database_build_id()
                self.assertEqual(state[0], "uncertain")

                with mock.patch.object(
                        corpusdb, "_database_build_id",
                        return_value=state):
                    ownership = corpusdb._derived_write_ownership(
                        for_write=True)
                self.assertEqual(ownership.state, "refused")
                self.assertFalse(ownership.writable)

    def test_real_busy_owner_probe_preserves_coded_sqlite_failure(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-real-busy-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._database(db_path, owner=BUILD_A)
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                locker = sqlite3.connect(db_path, timeout=0)
                try:
                    locker.execute("BEGIN EXCLUSIVE")
                    ownership = corpusdb._derived_write_ownership(
                        for_write=True)
                    readiness = doctor._corpus_db_readiness()
                finally:
                    locker.rollback()
                    locker.close()

                self.assertEqual(ownership.state, "refused")
                self.assertIsInstance(
                    ownership.sqlite_failure, sqlite3.Error)
                code = getattr(
                    ownership.sqlite_failure, "sqlite_errorcode", None)
                if code is not None:
                    self.assertIn(
                        code & 0xFF,
                        {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
                else:
                    # 3.10 exposes no code; the preserved prose is the proof
                    self.assertIn(
                        "locked", str(ownership.sqlite_failure).lower())
                self.assertFalse(ownership.writable)
                self.assertEqual(readiness["state"], "busy")
                self.assertIn("index writer", readiness["detail"])

    def test_codeless_owner_probe_reaches_doctors_unavailable_classifier(
            self) -> None:
        error = sqlite3.OperationalError("database is locked")
        self.assertFalse(hasattr(error, "sqlite_errorcode"))
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-codeless-sqlite-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._database(db_path, owner=BUILD_A)
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                with mock.patch.object(
                        corpusdb, "_open", side_effect=error):
                    ownership = corpusdb._derived_write_ownership(
                        for_write=True)
                    readiness = doctor._corpus_db_readiness()

        self.assertEqual(ownership.state, "refused")
        self.assertIs(ownership.sqlite_failure, error)
        # Ownership stays conservative (refused) on codeless evidence, but the
        # doctor readout classifies by SQLite's decades-stable locked strings:
        # a locked database reports busy, not an indistinguishable shrug.
        self.assertEqual(readiness["state"], "busy")
        self.assertIn("index writer", readiness["detail"])

    def test_private_owner_probe_recovers_hot_journal_only_in_alias(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-hot-journal-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, _owner_path):
                self._database(db_path, owner=BUILD_A)
                journal = Path(f"{db_path}-journal")
                journal.write_bytes(b"live rollback bytes")
                paths = (db_path, journal)
                before = {
                    path: (path.read_bytes(), corpusdb._proof_file_identity(path))
                    for path in paths
                }
                opened = []
                real_connect = corpusdb.sqlite3.connect

                def traced_connect(database, *args, **kwargs):
                    opened.append(str(database))
                    return real_connect(database, *args, **kwargs)

                with mock.patch.object(
                        corpusdb.sqlite3, "connect", side_effect=traced_connect):
                    snapshot = corpusdb._connect_read_alias(db_path, 0)
                try:
                    self.assertEqual(
                        snapshot.execute(
                            "SELECT value FROM meta WHERE key='schema'"
                        ).fetchone(),
                        (corpusdb._SCHEMA,),
                    )
                finally:
                    snapshot.close()
                self.assertGreaterEqual(len(opened), 2)
                for target in opened:
                    parsed = urllib.parse.urlparse(target)
                    self.assertEqual(parsed.scheme, "file")
                    uri_path = (
                        f"//{parsed.netloc}{parsed.path}"
                        if parsed.netloc else parsed.path)
                    opened_path = Path(
                        urllib.request.url2pathname(uri_path)).resolve()
                    self.assertNotEqual(opened_path, db_path.resolve())
                    self.assertNotEqual(opened_path.parent, root.resolve())
                self.assertEqual({
                    path: (path.read_bytes(), corpusdb._proof_file_identity(path))
                    for path in paths
                }, before)

    def test_legacy_db_proof_is_consumed_and_later_owner_erasure_refuses(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-legacy-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._database(db_path, owner=None)
                self._owner(
                    owner_path, BUILD_A, self._legacy_proof(db_path))
                ownership = corpusdb._derived_write_ownership(
                    for_write=True)
                self.assertEqual(ownership.state, "adoption")
                self.assertTrue(ownership.adopt_legacy_db)
                self.assertTrue(corpusdb._adopt_legacy_database_owner())
                self.assertEqual(
                    _db_meta(db_path, "build_id"), BUILD_A)
                self.assertEqual(
                    corpusdb._derived_write_ownership(
                        for_write=True).state, "current")
                with mock.patch.object(
                        corpusdb, "_database_build_id",
                        side_effect=AssertionError(
                            "foreground re-probed adopted legacy DB")):
                    self.assertEqual(
                        corpusdb._derived_write_ownership().state,
                        "current")

                # Recreate the old writer's behavior after the durable anchor:
                # owner metadata disappears and the file no longer matches the
                # exact legacy snapshot Rust authorized.
                db = sqlite3.connect(db_path)
                db.execute("DELETE FROM meta WHERE key = 'build_id'")
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('legacy_rewrite', ?)",
                    ("changed after anchor",))
                db.commit()
                db.close()
                refused = corpusdb._derived_write_ownership(
                    for_write=True)
                self.assertEqual(
                    refused.state, "post-adoption-clobber")
                self.assertIn(f"owned-by {BUILD_A}", refused.reason)
                self.assertIn(
                    "agrep doctor", refused.reason)
                self.assertFalse(refused.writable)

    def test_doctor_discloses_backup_then_manual_reindex_without_mutation(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-owner-clobber-doctor-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, owner_path):
                self._owner(owner_path, BUILD_A)
                self._cache(root / ".ingest_cache.bin", BUILD_A)
                self._database(db_path, owner=None)
                before = self._census(root)
                stdout = io.StringIO()
                with (
                    mock.patch.dict(
                        os.environ, {"AGREP_NO_DAEMON": "1"}, clear=False),
                    contextlib.redirect_stdout(stdout),
                ):
                    rc = doctor.main([])
                after = self._census(root)

        output = stdout.getvalue().lower()
        self.assertEqual(rc, 0)
        self.assertEqual(after, before)
        self.assertIn("ownership lockout", output)
        self.assertIn("safe manual remedy", output)
        self.assertIn("copy the entire data directory", output)
        self.assertIn("move", output)
        self.assertIn("do not delete", output)
        self.assertIn(
            doctor._cli_command("index", "--full").lower(), output)
        self.assertIn(
            doctor._cli_command("doctor", "--deep").lower(), output)
        self.assertIn("search and daemon auto-repair are disabled", output)
        self.assertNotIn("repairs on the next search", output)
        self.assertNotIn("daemon pass", output)
        self.assertNotIn("starts with your next search", output)
        self.assertNotIn("any search also triggers it", output)

    def test_legacy_db_proof_requires_exact_change_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-token-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, _owner_path):
                self._database(db_path, owner=None)
                proof = self._legacy_proof(db_path)
                self.assertTrue(
                    corpusdb._legacy_corpus_proof_matches(proof))

                wrong = dict(proof)
                if "Metadata" in proof["change_token"]:
                    wrong["change_token"] = {
                        "Metadata":
                        (proof["change_token"]["Metadata"] + 1)
                        & 0xFFFFFFFFFFFFFFFF
                    }
                else:
                    digest = list(proof["change_token"]["ContentSha256"])
                    digest[0] ^= 1
                    wrong["change_token"] = {"ContentSha256": digest}
                self.assertFalse(
                    corpusdb._legacy_corpus_proof_matches(wrong))

                before = db_path.stat()
                size = before.st_size
                self.assertGreater(size, 1024)
                offset = size // 2
                self.assertGreater(offset, 512)
                self.assertLess(offset, size - 512)
                with db_path.open("r+b") as stream:
                    stream.seek(offset)
                    original = stream.read(1)
                    stream.seek(offset)
                    stream.write(bytes((original[0] ^ 1,)))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.utime(
                    db_path,
                    ns=(before.st_atime_ns, proof["modified_ns"]))
                # len/mtime and the first/last 512 bytes still match. Only the
                # exact platform change token closes this interior-edit wedge.
                changed = corpusdb._proof_file_identity(db_path)
                self.assertEqual(changed[0], proof["len"])
                self.assertEqual(changed[1], proof["modified_ns"])
                self.assertEqual(
                    corpusdb._edge_hash(db_path, changed[0], changed),
                    proof["edge_hash"])
                self.assertFalse(
                    corpusdb._legacy_corpus_proof_matches(proof))

    def test_missing_exact_ingest_binary_never_publishes_provisional_db_owner(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-no-bin-") as raw:
            root = Path(raw)
            with self._paths(root) as (_messages, db_path, _owner_path):
                self._messages(root)
                with (
                    mock.patch.object(
                        indexd_runtime, "derived_writer_build_id",
                        side_effect=FileNotFoundError("agrep-rs is absent")),
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        side_effect=AssertionError("missing binary swept")),
                    mock.patch.object(
                        corpusdb, "_valid_db",
                        side_effect=AssertionError("missing binary validated")),
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("missing binary incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("missing binary rebuilt")),
                    mock.patch.object(
                        common, "IndexLock",
                        side_effect=AssertionError("missing binary locked")),
                ):
                    self.assertIsNone(corpusdb.connect(quiet=True))
                self.assertFalse(db_path.exists())

    def test_full_build_records_database_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-build-") as raw:
            root = Path(raw)
            dst = root / "new.db"
            families = corpusdb._SessionFamilySnapshot(
                "family", frozenset(), {})
            with (
                mock.patch.object(corpusdb, "_stamp", return_value="stamp"),
                mock.patch.object(
                    corpusdb, "_read_session_families",
                    return_value=families),
                mock.patch.object(corpusdb, "_scan", return_value={}),
                mock.patch.object(
                    corpusdb, "_replace_boundary_stats", return_value=None),
                mock.patch.object(
                    corpusdb, "_replace_session_families",
                    return_value=None),
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value=BUILD_A),
            ):
                corpusdb._build(dst, "stamp")
            self.assertEqual(_db_meta(dst, "build_id"), BUILD_A)

    def test_foreign_noop_is_refresh_success_even_without_fts_snapshot(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-owner-noop-") as raw:
            root = Path(raw)
            with self._paths(root, BUILD_B) as (_messages, _db, owner_path):
                self._messages(root)
                self._owner(owner_path, BUILD_A)
                with (
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        side_effect=AssertionError("foreign refresh swept")),
                    mock.patch.object(
                        corpusdb, "_incremental",
                        side_effect=AssertionError("foreign refresh incremented")),
                    mock.patch.object(
                        corpusdb, "_build",
                        side_effect=AssertionError("foreign refresh rebuilt")),
                    mock.patch.object(
                        common, "IndexLock",
                        side_effect=AssertionError("foreign refresh locked")),
                ):
                    self.assertIs(
                        indexd_runtime.refresh_search_index(quiet=True), True)

    def test_auto_indexer_treats_deliberate_foreign_noop_as_success(
            self) -> None:
        class Watcher:
            _last_event_wall = 0.0

        class Process:
            pid = 4242
            returncode = 0

            @staticmethod
            def communicate(timeout=None):
                return "", ""

        snapshot = ownerfile.Snapshot((1, 2, 3, 4), 0.0, b"owner")
        with (
            mock.patch.object(
                indexd_runtime, "indexd_failure_state",
                return_value=(1, "prior refusal", 1.0)),
            mock.patch.object(
                indexd_runtime, "auto_index_escalated",
                return_value=False),
        ):
            auto = indexer.AutoIndexer(
                Watcher(), owns_lifetime=lambda: True,
                owner_snapshot=snapshot)
        auto._identical = 1
        auto._retry_needed = True
        foreign = indexd_runtime.DerivedMutationInfo(
            "foreign", BUILD_A, "dead foreign owner")
        with (
            mock.patch.object(
                indexd_runtime, "derived_writer_mutation_info",
                return_value=foreign),
            mock.patch.object(
                auto, "_launch_index_process",
                return_value=(Process(), None)) as launch,
            mock.patch.object(
                common, "process_start_identity", return_value="start"),
            mock.patch.object(
                semantic, "source_generation", return_value="same"),
            mock.patch.object(corpusdb, "_read_changed", return_value=set()),
            mock.patch.object(
                auto, "_refresh_search_index", return_value=True),
            mock.patch.object(
                embed, "rebase_generation_marker", return_value=None),
            mock.patch.object(auto, "_run_post_index_hooks", return_value=None),
            mock.patch.object(
                indexd_runtime, "record_auto_index_health") as health,
            mock.patch.object(
                indexd_runtime, "rust_writer_env", return_value={}),
        ):
            auto._index()
        launched = launch.call_args.args[0]
        self.assertNotIn("--full", launched)
        self.assertEqual(auto._fail_streak, 0)
        self.assertFalse(auto._retry_needed)
        self.assertEqual(auto.state["phase"], "idle")
        health.assert_called_with(0, "", escalated=False)

    def test_real_build_b_daemon_reaps_a_dead_build_a_owner(
            self) -> None:
        # Build A's daemon is gone, so its anchor is a dead owner's claim
        # and build B's first publication performs the takeover; a LIVE
        # foreign owner still fences every writer (the tests above).
        source_binary = self._release_binary()
        executable = source_binary.name
        with tempfile.TemporaryDirectory(prefix="agrep-owner-daemon-e2e-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            source = (
                home / ".cline" / "data" / "tasks" / "1"
                / "api_conversation_history.json")
            source.parent.mkdir(parents=True)
            source.write_text(
                '[{"role":"user","content":"daemon wedge snapshot","ts":1}]',
                encoding="utf-8")
            data.mkdir()
            binary_a, binary_b = root / f"a-{executable}", root / f"b-{executable}"
            for binary, trailer in ((binary_a, b"\nA"), (binary_b, b"\nB")):
                shutil.copy2(source_binary, binary)
                with binary.open("ab") as stream:
                    stream.write(trailer)
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": os.fspath(data),
                "AGREP_HOME": os.fspath(home),
                "HOME": os.fspath(home),
                "USERPROFILE": os.fspath(home),
                "APPDATA": os.fspath(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": os.fspath(home / "AppData" / "Local"),
                "AGREP_RUNTIME_BUILD_ID": BUILD_A,
            })
            env.pop(indexd_runtime._PYTHON_RUNTIME_BUILD_ID_ENV, None)
            first = subprocess.run(
                [os.fspath(binary_a), "index", "--agent", "cline"],
                cwd=common.REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            owner = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["build_id"], BUILD_A)

            db_path = data / "corpus.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (BUILD_A,))
            db.commit()
            db.close()
            protected = [
                data / ".derived-owner.json",
                data / ".ingest_cache.bin",
                data / "corpus.db",
                data / "messages.jsonl",
            ]
            before = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in protected
            }

            daemon_env = dict(env)
            daemon_env.pop("AGREP_RUNTIME_BUILD_ID", None)
            daemon_env.update({
                "AGREP_RS_BIN": os.fspath(binary_b),
                # long enough for the startup publication (the takeover) to
                # land before idle-exit; the read-only pin never needed this
                "AGREP_INDEXD_IDLE_S": "5",
                "PYTHONPATH": os.fspath(common.PY_DIR),
            })
            daemon = subprocess.run(
                [sys.executable, os.fspath(common.PY_DIR / "indexd.py")],
                cwd=common.REPO_ROOT, env=daemon_env,
                capture_output=True, text=True, timeout=20,
                start_new_session=(os.name != "nt"))
            self.assertEqual(daemon.returncode, 0, daemon.stderr)
            del before  # the takeover legitimately replaces these artifacts
            self.assertIn("taking over on the next publication", daemon.stderr)
            owner_after = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
            self.assertNotEqual(owner_after["build_id"], BUILD_A)
            # the reaped publication is a working one, not a wreck: the
            # message survived and the census parses
            self.assertIn(
                "daemon wedge snapshot",
                (data / "messages.jsonl").read_text(encoding="utf-8"))


def _db_meta(path: Path, key: str) -> str | None:
    db = sqlite3.connect(path)
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])
    finally:
        db.close()


if __name__ == "__main__":
    unittest.main()
