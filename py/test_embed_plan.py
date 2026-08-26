from __future__ import annotations

import os
import unittest
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import common
import embed
import embedder
import embedding_segments

_MLX_PATCH = None


def setUpModule() -> None:
    global _MLX_PATCH
    _MLX_PATCH = mock.patch.dict(os.environ, {"AGREP_MLX": "off"})
    _MLX_PATCH.start()


def tearDownModule() -> None:
    _MLX_PATCH.stop()


def _message(index: int, ts: int | None = None) -> common.Message:
    return common.Message(
        id=f"codex:s{index}:{index}", agent="codex", project="p",
        session=f"s{index}", ts=index if ts is None else ts, turn=index,
        text=f"text {index}", who="user", model="m", model_source="explicit",
    )


def _resolve(rows: list[tuple]) -> list[common.Message]:
    out = []
    for row in rows:
        agent, tail = row[0].split(":", 1)
        session, turn = tail.rsplit(":", 1)
        out.append(common.Message(
            id=row[0], agent=agent, project="p", session=session, ts=row[2],
            turn=int(turn), text=f"resolved {row[0]}", who="user", model="m",
            model_source="explicit",
        ))
    return out


class PendingEmbeddingPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        embed._pending_plan_path().unlink(missing_ok=True)

    def test_repeat_page_does_not_walk_transcript_and_keeps_newest_order(self) -> None:
        source = {"ingest_signature": "generation-a"}
        builder = embed._PendingPlanBuilder(source, "output-a", "model-a")
        messages = [_message(index) for index in range(8)]
        for seq, message in enumerate(messages):
            builder.add(message, embed._text_hash(message.text), seq)
        builder.publish(total=len(messages))

        first = [messages[7].id, messages[6].id]
        self.assertTrue(embed._advance_pending_plan(
            source, "output-a", "output-b", first))
        with mock.patch.object(embed, "_resolve_pending_messages", side_effect=_resolve), \
                mock.patch.object(common, "iter_messages",
                                  side_effect=AssertionError("source scan")):
            page = embed._load_pending_plan(
                source, "output-b", "model-a", manifest_rows=2, max_new=2)

        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual([row.id for row in page["messages"]],
                         [messages[5].id, messages[4].id])
        self.assertEqual((page["pending"], page["total"]), (6, 8))

    def test_generation_and_output_mismatch_invalidate_without_resolution(self) -> None:
        source = {"ingest_signature": "generation-a"}
        builder = embed._PendingPlanBuilder(source, "output-a", "model-a")
        messages = [_message(1), _message(2)]
        for seq, message in enumerate(messages):
            builder.add(message, embed._text_hash(message.text), seq)
        builder.publish(total=2)
        self.assertTrue(embed._advance_pending_plan(
            source, "output-a", "output-b", [messages[1].id]))

        with mock.patch.object(
                embed, "_resolve_pending_messages",
                side_effect=AssertionError("invalid plans must not resolve")):
            self.assertIsNone(embed._load_pending_plan(
                {"ingest_signature": "generation-c"}, "output-b", "model-a",
                manifest_rows=1, max_new=1))
            self.assertIsNone(embed._load_pending_plan(
                source, "output-c", "model-a", manifest_rows=1, max_new=1))

    def test_failed_advance_cannot_make_unpublished_rows_reusable(self) -> None:
        source = {"ingest_signature": "generation-a"}
        builder = embed._PendingPlanBuilder(source, "output-a", "model-a")
        message = _message(1)
        builder.add(message, embed._text_hash(message.text), 0)
        builder.publish(total=1)

        self.assertFalse(embed._advance_pending_plan(
            source, "wrong-output", "output-b", [message.id]))
        self.assertIsNone(embed._load_pending_plan(
            source, "output-a", "model-a", manifest_rows=0, max_new=1))

    def test_completed_plan_releases_its_temporary_disk(self) -> None:
        source = {"ingest_signature": "generation-a"}
        builder = embed._PendingPlanBuilder(source, "output-a", "model-a")
        message = _message(1)
        builder.add(message, embed._text_hash(message.text), 0)
        builder.publish(total=1)

        self.assertTrue(embed._advance_pending_plan(
            source, "output-a", "output-b", [message.id]))
        self.assertFalse(embed._pending_plan_path().exists())

    def test_exact_ingest_delta_rebases_a_partial_plan_without_source_scan(self) -> None:
        previous = {"ingest_signature": "generation-a"}
        current = {"ingest_signature": "generation-b"}
        messages = [_message(index) for index in range(1, 4)]
        builder = embed._PendingPlanBuilder(
            previous, "output-a", embed.embedder.PROFILE_STRING)
        for seq, message in enumerate(messages):
            builder.add(message, embed._text_hash(message.text), seq)
        builder.publish(total=3)
        self.assertTrue(embed._advance_pending_plan(
            previous, "output-a", "output-b", [messages[0].id]))

        changed = messages[1]._replace(text="changed text")
        added = _message(4)
        with mock.patch.object(
                common, "iter_messages",
                side_effect=AssertionError("delta rebase scanned source")):
            self.assertTrue(embed._rebase_pending_plan(
                previous, current, "output-b", "output-c",
                previous_live_rows=1, current_live_rows=1,
                previous_pending=2, total=4,
                changed_sessions={changed.session, added.session},
                messages=[changed, added],
            ))

        db = sqlite3.connect(embed._pending_plan_path())
        try:
            meta = embed._plan_meta(db)
            rows = db.execute(
                "SELECT mid,text_hash,session FROM pending ORDER BY mid").fetchall()
        finally:
            db.close()
        self.assertEqual(meta["source"], embed._generation_key(current))
        self.assertEqual((meta["output"], meta["pending"], meta["total"]),
                         ("output-c", "3", "4"))
        self.assertEqual(
            rows,
            sorted([
                (changed.id, embed._text_hash(changed.text), changed.session),
                (messages[2].id, embed._text_hash(messages[2].text),
                 messages[2].session),
                (added.id, embed._text_hash(added.text), added.session),
            ]),
        )

    def test_all_row_retention_avoids_a_private_matrix_copy(self) -> None:
        matrix = np.arange(12, dtype=np.float32).reshape(6, 2)
        self.assertIs(embed._retained_embedding_rows(
            matrix, None, has_pending=True), matrix)

    def test_resolver_proves_base_and_reply_text_against_indexed_rows(self) -> None:
        import corpusdb

        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(session,turn,ts,agent,project,model,"
                   "model_source,who,text)")
        db.execute("CREATE INDEX msgs_session ON msgs(session,turn)")
        db.executemany("INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)", (
            ("s", 7, 10, "codex", "p", "m", "explicit", "user", "prompt"),
            ("s", 7, 10, "codex", "p", "m", "explicit", "agent", "reply"),
        ))
        rows = [
            ("codex:s:7", embed._text_hash("prompt"), 10, 0),
            ("codex:s:7#r", embed._text_hash("reply"), 10, 1),
        ]
        with mock.patch.object(corpusdb, "connect", return_value=db):
            resolved = embed._resolve_pending_messages(rows)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual([(row.id, row.text) for row in resolved],
                         [("codex:s:7", "prompt"), ("codex:s:7#r", "reply")])

    def test_resolver_proves_chunk_ids_against_their_logical_row(self) -> None:
        import corpusdb

        long_text = "\n".join(
            f"line {index} " + "x" * 60
            for index in range(3 * embed._CHUNK_CHARS // 60))
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(session,turn,ts,agent,project,model,"
                   "model_source,who,text)")
        db.execute("CREATE INDEX msgs_session ON msgs(session,turn)")
        db.executemany("INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)", (
            ("s", 7, 10, "codex", "p", "m", "explicit", "user", long_text),
            ("s", 7, 10, "codex", "p", "m", "explicit", "agent", long_text),
        ))
        digest = embed._text_hash(long_text)
        rows = [
            ("codex:s:7", digest, 10, 0),
            ("codex:s:7#c2", digest, 10, 1),
            ("codex:s:7#r#c1", digest, 10, 2),
        ]
        with mock.patch.object(corpusdb, "connect", return_value=db):
            resolved = embed._resolve_pending_messages(rows)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        # Chunk ids resolve through their logical row and keep the full text;
        # the chunk-specific input is derived only at inference time.
        self.assertEqual([row.id for row in resolved],
                         ["codex:s:7", "codex:s:7#c2", "codex:s:7#r#c1"])
        self.assertEqual([row.who for row in resolved],
                         ["user", "user", "agent"])
        self.assertTrue(all(row.text == long_text for row in resolved))
        self.assertNotEqual(embed._embed_input(resolved[1]),
                            embed._embed_input(resolved[0]))

    def test_second_bounded_run_uses_plan_and_publishes_aligned_rows(self) -> None:
        source = {"ingest_signature": "generation-a"}

        class FakeEmbedder:
            # A real Embedder carries its lane's vector-space identity; the
            # publish path refuses mismatches, so the double presents whatever
            # identity the test pinned (read late - tests mock PROFILE_STRING).
            @property
            def profile_string(self):
                return embed.embedder.PROFILE_STRING

            def embed_texts(self, texts):
                return np.asarray(
                    [[float(text.rsplit(" ", 1)[1]), 1.0] for text in texts],
                    dtype=np.float32)

        with tempfile.TemporaryDirectory(prefix="agrep-plan-run-") as td:
            root = Path(td)
            messages_path = root / "messages.jsonl"
            embeddings_path = root / "embeddings.f32"
            ids_path = root / "embeddings.ids"
            replies_path = root / "replies.jsonl"
            hashes_path = root / "embeddings.hashes"
            messages = [_message(index) for index in range(1, 6)]
            messages_path.write_text("".join(json.dumps(row._asdict()) + "\n"
                                             for row in messages), encoding="utf-8")
            by_id = {row.id: row for row in messages}

            def resolve(rows):
                return [by_id[row[0]] for row in rows]

            args = SimpleNamespace(
                smoke=None, full=False, max_new=2, background=False)
            patches = (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages_path),
                mock.patch.object(common, "EMBEDDINGS_PATH", embeddings_path),
                mock.patch.object(common, "IDS_PATH", ids_path),
                mock.patch.object(embed, "REPLIES_PATH", replies_path),
                mock.patch.object(embed, "HASHES_PATH", hashes_path),
                mock.patch.object(embed.embedder, "PROFILE", {"dim": 2}),
                mock.patch.object(embed.embedder, "PROFILE_STRING", "model-a"),
                mock.patch.object(
                    embed.embedder, "ensure_model",
                    side_effect=AssertionError("model verified twice")),
                mock.patch.object(embed.embedder, "get", return_value=FakeEmbedder()),
                mock.patch.object(embed.semantic, "source_generation", return_value=source),
                mock.patch.object(embed, "_resolve_pending_messages", side_effect=resolve),
                mock.patch.object(embed, "_stamp", side_effect=lambda *a, **kw: {
                    "indexed": len(kw["indexed_ids"]), "total": kw["total_rows"],
                    "pending": kw["total_rows"] - len(kw["indexed_ids"]),
                }),
                mock.patch.object(embed, "_publish_state", return_value=None),
                mock.patch.object(
                    common, "indexed_family_roots",
                    side_effect=lambda sessions: {
                        str(session): str(session) for session in sessions
                    }),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patches[12], patches[13], \
                    patches[14]:
                self.assertEqual(embed._run(args), 0)
                with mock.patch.object(
                        embed, "_scan_source",
                        side_effect=AssertionError("repeat pass scanned source")):
                    self.assertEqual(embed._run(args), 0)
                manifest = embedding_segments.load_manifest(
                    root / "embeddings.meta")
                active = embedding_segments.active_rows(manifest)
                mappings = []
                try:
                    for segment in manifest["segments"]:
                        mappings.append(np.memmap(
                            embedding_segments.artifact_path(
                                manifest, segment["artifacts"]["f32"]),
                            dtype="<f4", mode="r",
                            shape=(int(segment["rows"]), 2)))
                    ids = [row["mid"] for row in active]
                    values = [float(mappings[row["segment"]][
                        row["local_ord"], 0]) for row in active]
                    self.assertEqual(ids, [messages[4].id, messages[3].id,
                                           messages[2].id, messages[1].id])
                    self.assertEqual(values, [5.0, 4.0, 3.0, 2.0])
                finally:
                    for matrix in mappings:
                        common.close_embedding_matrix(matrix)


class _FakeEmbedder:
    @property
    def profile_string(self):
        return embedder.PROFILE_STRING

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s

    def embed_texts(self, texts):
        if self.delay_s:
            time.sleep(self.delay_s)
        return np.asarray(
            [[float(len(t)), float(sum(t.encode()) % 97)] for t in texts],
            dtype=np.float32)


class BootstrapPassTests(unittest.TestCase):
    def test_bootstrap_constants_are_pinned(self) -> None:
        import semantic
        self.assertEqual(semantic.SEMANTIC_BOOTSTRAP_MAX_NEW, 256)
        self.assertEqual(embed._BOOTSTRAP_CHUNK, 64)
        self.assertEqual(embed._BOOTSTRAP_DEADLINE_S, 4.5)

    def test_bootstrap_deadline_publishes_the_finished_prefix(self) -> None:
        texts = [f"deadline row {i}" for i in range(8)]
        stats: dict = {}
        with mock.patch.object(embed, "_BOOTSTRAP_CHUNK", 2), \
                mock.patch.object(embed, "_BOOTSTRAP_DEADLINE_S", 0.01):
            parts, done, moved = embed._embed_pending_chunks(
                _FakeEmbedder(delay_s=0.05), texts, {"gen": 1},
                background=True, bootstrap=True, stats=stats)
        self.assertEqual(done, 2)
        self.assertFalse(moved)
        self.assertTrue(stats["deadline"])
        self.assertEqual(sum(part.shape[0] for part in parts), done)

    def test_drain_pass_ignores_the_bootstrap_deadline(self) -> None:
        texts = [f"drain row {i}" for i in range(6)]
        with mock.patch.object(embed, "_BOOTSTRAP_DEADLINE_S", 0.0), \
                mock.patch.object(embed, "_BACKGROUND_CHUNK", 2), \
                mock.patch.object(embed.semantic, "source_generation",
                                  return_value={"gen": 1}):
            _parts, done, moved = embed._embed_pending_chunks(
                _FakeEmbedder(delay_s=0.01), texts, {"gen": 1},
                background=True, bootstrap=False)
        self.assertEqual(done, len(texts))
        self.assertFalse(moved)

    def test_bootstrap_chunking_is_vector_identical_to_one_shot(self) -> None:
        # Scheduling only: chunk shape must not move vectors or their order.
        texts = [f"parity row {i % 5}" for i in range(11)]  # includes dupes
        model = _FakeEmbedder()
        expected = model.embed_texts(texts)
        with mock.patch.object(embed, "_BOOTSTRAP_CHUNK", 3):
            parts, done, _moved = embed._embed_pending_chunks(
                model, texts, None, background=True, bootstrap=True)
        self.assertEqual(done, len(texts))
        np.testing.assert_array_equal(np.vstack(parts), expected)


class ForeignManifestShapeTests(unittest.TestCase):
    """A manifest another build wrote cannot crash a finished embed pass.

    A real box upgraded from a June build carried an `embeddings.meta` whose
    top level was not the shape this build expects; publication died with
    `AttributeError: 'int' object has no attribute 'get'` on EVERY pass, so
    the semantic lane could never rebuild - an unbounded retry loop.
    """

    def test_a_non_mapping_manifest_field_reads_as_absent(self) -> None:
        for holder in ({"segments": 5}, {"segments": None}, {"segments": []},
                       {}, 5, None, "text"):
            with self.subTest(holder=holder):
                self.assertEqual(embed._mapping_field(holder, "segments"), {})

    def test_a_mapping_field_is_returned_intact(self) -> None:
        holder = {"segments": {"source": {"sig": "abc"}, "generation": "g1"}}
        self.assertEqual(
            embed._mapping_field(holder, "segments").get("generation"), "g1")

    def test_a_foreign_manifest_binds_nothing_instead_of_crashing(self) -> None:
        # the desktop's exact shape: a bare int where a mapping was expected
        self.assertIsNone(embed._validated_segmented_source_binding(
            {"sig": "src"}, {"segments": 5}, 10, [], [], []))

    def test_a_foreign_manifest_yields_no_expected_generation(self) -> None:
        for manifest in ({"segments": 5}, {"state": 7}, {"state": {"commit": 3}}):
            with self.subTest(manifest=manifest):
                self.assertEqual(
                    str(embed._mapping_field(manifest, "segments").get(
                        "generation")
                        or embed._mapping_field(
                            embed._mapping_field(manifest, "state"), "commit",
                        ).get("generation") or ""), "")


if __name__ == "__main__":
    unittest.main()
