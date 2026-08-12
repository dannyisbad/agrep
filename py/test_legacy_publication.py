"""Pre-family publications have one truthful, terminating upgrade path."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import fileops  # noqa: E402
import embedder  # noqa: E402
import embedding_segments  # noqa: E402
import semantic  # noqa: E402
import session_context  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
)
SESSION = "99999999-9999-4999-8999-999999999999"
V020_DERIVED_PROOF_VERSION = 4
V020_DERIVED_PROOF_NAMES = (
    "messages.jsonl",
    "replies.jsonl",
    "sessions.jsonl",
    "boundary_stats.json",
    ".boundary_stats.bin",
    "event_stats.json",
)


def _fnv(payload: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in payload:
        value ^= byte
        value = (value * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _semantic_inputs(
        root: Path, mid: str, text: str,
) -> tuple[dict[str, Path], list[str], list[dict]]:
    dim = 2
    generation = hashlib.md5(b"legacy-legacy-publication").digest()
    q8_payload = struct.pack("<fbb", 1.0, 1, 0)
    q8_header = bytearray(64)
    struct.pack_into(
        "<4sIIIQ16sIIQ", q8_header, 0, b"AGQ8", 1, dim, 1, 1,
        generation, dim + 4, 0, _fnv(q8_payload),
    )
    group_payload = struct.pack("<I", 1)
    group_header = bytearray(64)
    struct.pack_into(
        "<4sIIIQ16sIIQ", group_header, 0, b"AGQG", 1, 0, 2, 1,
        generation, 4, 0, _fnv(group_payload),
    )
    artifacts: dict[str, Path] = {}
    for name, payload in {
        "f32": b"\0" * (dim * 4),
        "f16": b"\0" * (dim * 2),
        "q8": bytes(q8_header) + q8_payload,
        "groups": bytes(group_header) + group_payload,
    }.items():
        path = root / f"legacy.{name}"
        path.write_bytes(payload)
        artifacts[name] = path
    text_hash = hashlib.blake2b(text.encode(), digest_size=8).hexdigest()
    refs = [{
        "mid": mid,
        "text_hash": text_hash,
        "agent": "claude",
        "project": "legacy-legacy-publication",
        "session": SESSION,
        "ts": 1,
        "turn": 0,
        "who": "user",
        "model": "claude-legacy-fixture",
        "model_source": "explicit",
        "family_id": 1,
        "family_label": f"f:{SESSION}",
        "side": False,
        "metadata_hash": hashlib.blake2b(
            f"metadata:{mid}".encode(), digest_size=16).hexdigest(),
    }]
    return artifacts, [text_hash], refs


class LegacyPublicationUpgradeTests(unittest.TestCase):
    @staticmethod
    def _fixture(root: Path) -> tuple[Path, Path, str]:
        home = root / "home"
        data = root / "data"
        project = home / ".claude" / "projects" / "legacy-legacy-publication"
        project.mkdir(parents=True)
        work = root / "work"
        work.mkdir()
        term = "legacy publication upgrade window"
        rows = (
            {
                "type": "user",
                "userType": "external",
                "sessionId": SESSION,
                "timestamp": "2026-07-27T12:00:00.000Z",
                "cwd": os.fspath(work),
                "message": {"role": "user", "content": term},
            },
            {
                "type": "assistant",
                "sessionId": SESSION,
                "timestamp": "2026-07-27T12:00:01.000Z",
                "cwd": os.fspath(work),
                "message": {
                    "role": "assistant",
                    "model": "claude-legacy-fixture",
                    "content": [{"type": "text", "text": "legacy reply"}],
                },
            },
        )
        (project / f"{SESSION}.jsonl").write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
            newline="\n",
        )
        return home, data, term

    @staticmethod
    def _env(home: Path, data: Path) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AGREP_")
            and key not in {
                "APPDATA",
                "CLINE_DIR",
                "CRUSH_GLOBAL_DATA",
                "LOCALAPPDATA",
                "OPENCODE_DB",
                "USERPROFILE",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
            }
        }
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "CLINE_DIR": str(home / ".cline"),
            "AGREP_HOME": str(home),
            "AGREP_DATA_DIR": str(data),
            "AGREP_DATA_DIR_SOURCE": "env",
            "AGREP_RS_BIN": os.fspath(RELEASE_BIN.resolve()),
            "AGREP_NO_DAEMON": "1",
            "AGREP_NO_FETCH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RAYON_NUM_THREADS": "2",
        })
        return env

    def _run(
        self, home: Path, data: Path, *argv: str,
    ) -> subprocess.CompletedProcess[str]:
        self.assertTrue(RELEASE_BIN.is_file(), f"release binary missing: {RELEASE_BIN}")
        return subprocess.run(
            [sys.executable, str(CLI), *argv],
            cwd=ROOT,
            env=self._env(home, data),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=90,
            check=False,
        )

    def _ingest(
        self, home: Path, data: Path,
    ) -> subprocess.CompletedProcess[str]:
        # Exercise the public remedy printed by the refusal, including its
        # ordinary all-agent scope. The upgrade must terminate in one command.
        return self._run(home, data, "index")

    @staticmethod
    def _downgrade_to_v020(data: Path) -> None:
        """Recreate 747a109's v4 six-file proof, not a made-up missing-file shape."""
        proof_path = data / ".derived_generation.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["version"] = V020_DERIVED_PROOF_VERSION
        proof["files"] = [
            row for row in proof["files"]
            if row.get("name") in V020_DERIVED_PROOF_NAMES
        ]
        proof_path.write_text(
            json.dumps(proof, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        (data / session_context.SESSION_FAMILY_META_FILE).unlink()
        # A real 0.2.0 cache is undecodable and has no R8 anchor or DB owner.
        # Removing all three identities forces the same cold-cache/adoption path
        # without manufacturing an old binary.
        (data / ".ingest_cache.bin").unlink(missing_ok=True)
        (data / ".derived-owner.json").unlink(missing_ok=True)
        db = sqlite3.connect(data / "corpus.db")
        try:
            db.execute("DELETE FROM meta WHERE key = 'build_id'")
            db.commit()
        finally:
            db.close()
        self_names = tuple(row["name"] for row in proof["files"])
        if self_names != V020_DERIVED_PROOF_NAMES:
            raise AssertionError(f"not a 0.2.0-shaped proof: {self_names!r}")

    def test_v020_keyword_semantic_refusal_and_unchanged_ingest_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-legacy-") as raw:
            home, data, term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            # Stage semantics for the deterministic generation from current ingest.
            # Downgrading the transcript proof recreates the upgrade window; unchanged
            # ingest must restore it without publishing any other semantic bundle.
            current_source = session_context.transcript_generation(data)
            self.assertIsNotNone(current_source)
            message = json.loads(
                (data / "messages.jsonl").read_text(
                    encoding="utf-8").splitlines()[0])
            artifacts, hashes, refs = _semantic_inputs(
                data, message["id"], message["text"])
            embedding_segments.publish_base(
                data / "embeddings.meta",
                source=current_source,
                model_id="legacy-model",
                dim=2,
                artifacts=artifacts,
                ids=[message["id"]],
                hashes=hashes,
                refs=refs,
                coverage={"total": 1},
            )
            protected_before = {
                name: (
                    (data / name).read_bytes(),
                    fileops.file_identity(data / name))
                for name in ("messages.jsonl", "replies.jsonl", "sessions.jsonl")
            }
            self._downgrade_to_v020(data)

            keyword = self._run(
                home, data, term, "--json", "--lexical", "--no-auto")
            self.assertEqual(keyword.returncode, 0, keyword.stderr)
            self.assertIn(term, keyword.stdout)

            refused = self._run(home, data, term, "-s", "--json", "--no-auto")
            self.assertEqual(refused.returncode, 2, refused.stderr)
            payload = json.loads(refused.stdout)
            story = refused.stdout + refused.stderr
            self.assertEqual(payload["semantic"]["state"], "legacy-publication")
            self.assertIn("legacy-publication", story)
            self.assertIn("agrep index", story)
            self.assertNotIn("unstable-source", story)
            self.assertNotIn("retries automatically", story)
            with mock.patch.object(
                    semantic, "source_generation",
                    side_effect=lambda attempts=4: session_context.transcript_generation(
                        data, attempts=attempts)), \
                    mock.patch.object(semantic, "ensure_fresh_async") as refresh:
                core_refusal = semantic.search(term)
            refresh.assert_not_called()
            self.assertTrue(core_refusal["semantic_unavailable"])
            self.assertEqual(
                core_refusal["semantic_integrity"]["state"],
                "legacy-publication",
            )
            self.assertEqual(
                core_refusal["semantic_integrity"]["repair"],
                semantic.LEGACY_PUBLICATION_REPAIR,
            )

            upgraded = self._ingest(home, data)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertNotIn("skipped ingest + writes", upgraded.stdout)
            self.assertIsNotNone(session_context.transcript_generation(data))
            proof = json.loads(
                (data / ".derived_generation.json").read_text(encoding="utf-8"))
            self.assertGreater(proof["version"], V020_DERIVED_PROOF_VERSION)
            self.assertIn(
                session_context.SESSION_FAMILY_META_FILE,
                [row["name"] for row in proof["files"]],
            )
            self.assertEqual(
                {
                    name: (
                        (data / name).read_bytes(),
                        fileops.file_identity(data / name))
                    for name in protected_before
                },
                protected_before,
            )
            source = session_context.transcript_generation(data)
            self.assertEqual(source, current_source)
            served_wire = json.dumps({
                "results": [{
                    **message,
                    "sem_score": 0.99,
                    "score_kind": "cosine",
                }],
                "candidate_sessions": 1,
                "truncated": False,
                "score_kind": "cosine",
                "semantic_coverage": {
                    "indexed": 1,
                    "total": 1,
                    "pending": 0,
                    "fraction": 1.0,
                    "complete": True,
                    "order": "complete",
                },
                "partial": False,
            })
            import ask

            with mock.patch.object(common, "DATA_DIR", data), \
                    mock.patch.object(
                        common, "EMBEDDINGS_PATH", data / "embeddings.f32"), \
                    mock.patch.object(common, "IDS_PATH", data / "embeddings.ids"), \
                    mock.patch.object(session_context, "DATA_DIR", data), \
                    mock.patch.object(
                        semantic, "_active_embedding_profile",
                        return_value=(2, "legacy-model")), \
                    mock.patch.object(embedder, "get"), \
                    mock.patch.object(
                        ask, "tool_search_hybrid", return_value=served_wire), \
                    mock.patch.object(semantic, "ensure_fresh_async") as refresh:
                after = semantic.embedding_coherence()
                served = semantic.search(term)
            refresh.assert_not_called()
            self.assertEqual(after["state"], "current")
            self.assertTrue(after["coherent"])
            self.assertEqual(served["results"][0]["text"], message["text"])

    def test_explicit_full_does_not_destroy_a_spent_adoption_clobber(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-legacy-post-adoption-clobber-") as raw:
            home, data, _term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            owner = json.loads(
                (data / ".derived-owner.json").read_text(encoding="utf-8"))
            db_path = data / "corpus.db"
            db = sqlite3.connect(db_path)
            try:
                db.execute("DELETE FROM meta WHERE key = 'build_id'")
                db.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("legacy_rewrite", "after durable adoption"))
                db.commit()
            finally:
                db.close()
            before = {
                path.name: (path.read_bytes(), fileops.file_identity(path))
                for path in data.iterdir()
                if path.is_file()
            }

            ordinary = self._ingest(home, data)
            self.assertNotEqual(ordinary.returncode, 0, ordinary.stderr)
            self.assertIn("automatic repair is disabled", ordinary.stderr)
            self.assertIn("agrep doctor", ordinary.stderr)

            direct_env = self._env(home, data)
            direct_env["AGREP_RUNTIME_BUILD_ID"] = owner["build_id"]
            direct_env.pop("AGREP_PYTHON_RUNTIME_BUILD_ID", None)
            direct = subprocess.run(
                [
                    os.fspath(RELEASE_BIN.resolve()),
                    "index", "--agent", "all", "--full",
                ],
                cwd=ROOT,
                env=direct_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=90,
                check=False,
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertIn("serving the published snapshot read-only",
                          direct.stderr)
            self.assertIn("automatic repair is disabled", direct.stderr)
            self.assertIn("agrep doctor", direct.stderr)

            explicit = self._run(home, data, "index", "--full")
            self.assertNotEqual(explicit.returncode, 0, explicit.stderr)
            self.assertIn("automatic repair is disabled", explicit.stderr)
            self.assertIn("agrep doctor", explicit.stderr)
            self.assertEqual(
                {
                    name: (
                        (data / name).read_bytes(),
                        fileops.file_identity(data / name),
                    )
                    for name in before
                },
                before,
            )
            self.assertFalse(any(
                path.name.startswith(
                    ".corpus.db.post-adoption-clobber.")
                for path in data.iterdir()
            ))

    def test_stable_legacy_publication_does_not_pay_movement_retries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-stable-legacy-") as raw:
            home, data, _term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            self._downgrade_to_v020(data)
            real_snapshot = session_context.ownerfile.snapshot
            with mock.patch.object(
                    session_context.ownerfile, "snapshot",
                    wraps=real_snapshot) as snapshots, \
                    mock.patch.object(session_context.time, "sleep") as sleep:
                with self.assertRaises(session_context.LegacyPublication):
                    session_context.transcript_generation(data)

        self.assertEqual(snapshots.call_count, 6)
        sleep.assert_not_called()

    def test_family_meta_ahead_of_signature_is_a_publication_race(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-family-ahead-") as raw:
            data = Path(raw)
            (data / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            (data / "replies.jsonl").write_text("", encoding="utf-8")
            (data / "sessions.jsonl").write_text("{}\n", encoding="utf-8")
            (data / ".ingest.sig").write_text("one", encoding="utf-8")
            family = data / session_context.SESSION_FAMILY_META_FILE
            record = {
                "version": session_context.SESSION_FAMILY_INDEX_VERSION,
                "algorithm": session_context.SESSION_FAMILY_DIGEST_ALGORITHM,
                "ingest_signature": "two",
                "count": 1,
                "digest": "0" * 48,
            }
            family.write_text(json.dumps(record), encoding="utf-8")

            with self.assertRaises(session_context.TranscriptPublicationRace):
                session_context.transcript_generation(data, attempts=1)
            family.write_text(
                json.dumps({**record, "digest": "malformed"}), encoding="utf-8")
            with self.assertRaisesRegex(
                    RuntimeError, "not generation-bound") as malformed:
                session_context.transcript_generation(data, attempts=1)
            self.assertNotIsInstance(
                malformed.exception, session_context.TranscriptPublicationRace)
            family.write_text(json.dumps(record), encoding="utf-8")
            (data / ".ingest.sig").write_text("two", encoding="utf-8")
            current = session_context.transcript_generation(data, attempts=1)

        self.assertIsNotNone(current)
        self.assertEqual(current["family"], json.dumps(
            [session_context.SESSION_FAMILY_DERIVATION_VERSION, 1, "0" * 48],
            separators=(",", ":")))

    def test_current_generation_missing_family_is_not_legacy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-current-tear-") as raw:
            home, data, _term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            (data / session_context.SESSION_FAMILY_META_FILE).unlink()

            with self.assertRaisesRegex(
                    RuntimeError, "not generation-bound") as caught:
                session_context.transcript_generation(data, attempts=1)
            self.assertNotIsInstance(
                caught.exception, session_context.TranscriptPublicationRace)
            with mock.patch.object(
                    semantic, "source_generation",
                    side_effect=lambda attempts=4: session_context.transcript_generation(
                        data, attempts=attempts)):
                coherence = semantic.embedding_coherence()

        self.assertEqual(coherence["state"], "unstable-source")
        self.assertNotEqual(coherence["state"], "legacy-publication")

    def test_malformed_proof_with_missing_family_fails_closed_as_unstable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-malformed-proof-") as raw:
            home, data, _term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            (data / session_context.SESSION_FAMILY_META_FILE).unlink()
            valid = json.loads(
                (data / ".derived_generation.json").read_text(
                    encoding="utf-8"))
            valid["version"] = V020_DERIVED_PROOF_VERSION
            valid["files"] = [
                row for row in valid["files"]
                if row.get("name") in V020_DERIVED_PROOF_NAMES
            ]

            malformed_records: dict[str, str] = {
                "not-an-object": "[]",
                "recursive-depth": "[" * 2_000 + "0" + "]" * 2_000,
            }
            bad_name = json.loads(json.dumps(valid))
            bad_name["files"][0]["name"] = []
            malformed_records["non-string-name"] = json.dumps(bad_name)
            missing_len = json.loads(json.dumps(valid))
            del missing_len["files"][0]["len"]
            malformed_records["missing-len"] = json.dumps(missing_len)
            wrong_edge = json.loads(json.dumps(valid))
            wrong_edge["files"][0]["edge_hash"] ^= 1
            malformed_records["wrong-edge-hash"] = json.dumps(wrong_edge)
            wrong_order = json.loads(json.dumps(valid))
            wrong_order["files"][0], wrong_order["files"][1] = (
                wrong_order["files"][1], wrong_order["files"][0])
            malformed_records["wrong-order"] = json.dumps(wrong_order)
            extra_field = json.loads(json.dumps(valid))
            extra_field["files"][0]["invented"] = True
            malformed_records["extra-row-field"] = json.dumps(extra_field)
            compact_valid = json.dumps(valid, separators=(",", ":"))
            malformed_records["duplicate-top-level-key"] = (
                compact_valid.replace(
                    '"version":4',
                    '"version":4,"version":4',
                    1,
                )
            )
            first_name = json.dumps(
                valid["files"][0]["name"], separators=(",", ":"))
            name_field = f'"name":{first_name}'
            malformed_records["duplicate-nested-key"] = (
                compact_valid.replace(
                    name_field,
                    f"{name_field},{name_field}",
                    1,
                )
            )

            for case, malformed in malformed_records.items():
                with self.subTest(case=case):
                    (data / ".derived_generation.json").write_text(
                        malformed, encoding="utf-8")
                    with self.assertRaisesRegex(
                            RuntimeError, "not generation-bound"):
                        session_context.transcript_generation(data, attempts=1)
                    with mock.patch.object(
                            semantic, "source_generation",
                            side_effect=lambda attempts=4:
                            session_context.transcript_generation(
                                data, attempts=attempts)):
                        coherence = semantic.embedding_coherence()
                    self.assertEqual(coherence["state"], "unstable-source")
                    self.assertNotEqual(
                        coherence["state"], "legacy-publication")

    def test_a_source_that_moves_mid_read_remains_unstable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-legacy-moving-") as raw:
            home, data, _term = self._fixture(Path(raw))
            first = self._ingest(home, data)
            self.assertEqual(first.returncode, 0, first.stderr)
            real_snapshot = session_context.ownerfile.snapshot
            signature = data / ".ingest.sig"
            signature_reads = 0

            def moving_snapshot(path: Path, *, max_bytes: int):
                nonlocal signature_reads
                if path == signature:
                    signature_reads += 1
                    signature.write_text(
                        f"{signature_reads}:moving\n", encoding="utf-8")
                return real_snapshot(path, max_bytes=max_bytes)

            def moving_source(attempts: int = 4):
                with mock.patch.object(
                        session_context.ownerfile, "snapshot",
                        side_effect=moving_snapshot):
                    return session_context.transcript_generation(
                        data, attempts=attempts)

            with mock.patch.object(
                    semantic, "source_generation", side_effect=moving_source):
                coherence = semantic.embedding_coherence()

        self.assertEqual(coherence["state"], "unstable-source")
        self.assertNotEqual(coherence["state"], "legacy-publication")


if __name__ == "__main__":
    unittest.main()
