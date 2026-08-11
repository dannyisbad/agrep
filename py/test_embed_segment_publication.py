from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import types
import unittest
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()

import common
import corpusdb
import embed
import embedder
import embedding_segments
import indexd_runtime
import semantic
import semantic_q8
import segment_query


class _Embedder:
    @property
    def profile_string(self):
        return embedder.PROFILE_STRING

    def embed_texts(self, texts):
        rows = np.zeros((len(texts), int(embedder.PROFILE["dim"])), dtype=np.float32)
        for index, text in enumerate(texts):
            rows[index, index % rows.shape[1]] = 0.8
            rows[index, (len(text) + 1) % rows.shape[1]] += 0.6
        rows /= np.linalg.norm(rows, axis=1, keepdims=True)
        return rows


def _row(mid: str, text: str, *, session: str, turn: int) -> dict:
    return {
        "id": mid, "agent": "codex", "project": "p", "session": session,
        "ts": turn * 1000, "turn": turn, "text": text, "who": "user",
        "model": "gpt", "model_source": "explicit",
    }


def _roots(sessions) -> dict[str, str]:
    return {str(session): str(session) for session in sessions}


class SegmentedEmbedPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        segment_query.close_cache()
        shutil.rmtree(common.DATA_DIR, ignore_errors=True)
        common.DATA_DIR.mkdir(parents=True, mode=0o700)
        build_id = indexd_runtime.derived_writer_build_id(
            common.ingest_bin(), require_binary=True)
        indexd_runtime.DERIVED_OWNER_PATH.write_text(
            json.dumps(
                {"version": 1, "build_id": build_id},
                separators=(",", ":")),
            encoding="utf-8")

    def tearDown(self) -> None:
        segment_query.close_cache()
        indexd_runtime.DERIVED_OWNER_PATH.unlink(missing_ok=True)

    @staticmethod
    def _write(rows: list[dict]) -> None:
        common.MESSAGES_PATH.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8", newline="\n")

    @staticmethod
    def _write_corpus(rows: list[dict]):
        path = common.DATA_DIR / "test-corpus.db"
        path.unlink(missing_ok=True)
        db = sqlite3.connect(path)
        try:
            db.execute(
                "CREATE TABLE msgs(agent TEXT,session TEXT,turn INTEGER,who TEXT,text TEXT,"
                "ts INTEGER,project TEXT,model TEXT,model_source TEXT)")
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
                [(row["agent"], row["session"], row["turn"], row["who"], row["text"],
                  row["ts"], row["project"], row["model"], row["model_source"])
                 for row in rows])
            db.commit()
        finally:
            db.close()
        return path

    @staticmethod
    def _args(*, smoke=None, max_new=None, full=False):
        return types.SimpleNamespace(
            smoke=smoke, full=full, max_new=max_new, background=False)

    def test_full_rebuild_replaces_existing_segmented_generation(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1),
                _row("codex:b:2", "beta", session="b", turn=2)]
        self._write(rows)
        with (mock.patch.object(
                  semantic, "source_generation",
                  return_value={"ingest_signature": "one"}),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots",
                                side_effect=_roots),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=False),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(smoke=2)), 0)
            meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
            before = embedding_segments.load_manifest(meta)
            self.assertEqual(embed._run(self._args(full=True)), 0)
        after = embedding_segments.load_manifest(
            meta, verify_hashes=True, validate_liveness=True)
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertEqual((len(after["segments"]), after["live_rows"]), (1, 2))
        self.assertEqual(
            [row["mid"] for row in embedding_segments.active_rows(after)],
            ["codex:a:1", "codex:b:2"])

    def test_segment_publish_checks_source_at_final_manifest_fence(self) -> None:
        bound = {"ingest_signature": "bound"}
        moved = {"ingest_signature": "moved"}

        def publish(*_args, **kwargs):
            kwargs["_before_replace"]()
            self.fail("a moved transcript source reached manifest replacement")

        with (
            mock.patch.object(
                embed, "_validated_segmented_source_binding",
                return_value=(bound, 1)),
            mock.patch.object(
                semantic, "source_generation", return_value=moved),
            mock.patch.object(
                embedding_segments, "publish_delta", side_effect=publish) as delta,
        ):
            with self.assertRaises(common.TranscriptPublicationRace):
                embed._publish_segment_generation(
                    {"segments": {"generation": "base"}}, bound, 1,
                    [], [], [], [], [], [0],
                    dim=int(embedder.PROFILE["dim"]), append_segmented=True)
        delta.assert_called_once()

    def test_no_change_pass_repairs_byte_identical_prefix_relocation(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1),
                _row("codex:b:2", "beta", session="b", turn=2)]
        source = {"ingest_signature": "one"}
        self._write(rows)
        with (mock.patch.object(
                  semantic, "source_generation", return_value=source),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots", side_effect=_roots),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=False),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(smoke=2)), 0)
            meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
            before = embedding_segments.load_manifest(meta)
            identities = embedding_segments.publication_artifact_identities(before)
            self.assertIsNotNone(identities)
            for index, path in enumerate(identities):
                replacement = path.with_name(f"relocated-{index}-{path.name}")
                shutil.copy2(path, replacement)
                replacement.replace(path)
            segment_query.close_cache()
            with self.assertRaises(segment_query.SegmentQueryError):
                segment_query.open_current(meta)

            self.assertEqual(embed._run(self._args()), 0)

        after = embedding_segments.load_manifest(meta)
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertTrue(
            embedding_segments.publication_artifacts_still_bound(after))
        segment_query.close_cache()
        with mock.patch.object(
                common, "transcript_generation", return_value=source):
            opened, _, _, coverage = segment_query.open_current(meta)
        self.assertEqual(opened["generation"], after["generation"])
        self.assertEqual(
            {key: coverage[key] for key in ("indexed", "total", "pending")},
            {"indexed": 2, "total": 2, "pending": 0})

    def test_v2_publication_retires_legacy_layout(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1)]
        self._write(rows)
        with (mock.patch.object(
                  semantic, "source_generation",
                  return_value={"ingest_signature": "one"}),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots",
                                side_effect=_roots),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(smoke=1)), 0)
        legacy = [common.EMBEDDINGS_PATH, common.IDS_PATH, embed.HASHES_PATH]
        for path in legacy:
            path.write_bytes(b"legacy")
        semantic_q8.ARTIFACT_DIR.mkdir()
        old_q8 = semantic_q8.ARTIFACT_DIR / "embeddings.old.q8"
        old_q8.write_bytes(b"legacy")
        semantic_q8.MANIFEST_PATH.write_bytes(b"legacy")
        result = embed._prune_legacy_embedding_layout()
        self.assertEqual(result["deferred"], 0)
        self.assertFalse(any(path.exists() for path in [
            *legacy, old_q8, semantic_q8.MANIFEST_PATH,
            semantic_q8.ARTIFACT_DIR,
        ]))
        marker = semantic.generation_marker_path()
        marker.write_bytes(b"legacy")
        unlink = type(marker).unlink

        def windows_busy(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("simulated mapped Windows artifact")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(type(marker), "unlink", new=windows_busy):
            deferred = embed._prune_legacy_embedding_layout()
        self.assertEqual(deferred["deferred"], 1)
        self.assertTrue(marker.exists())
        retried = embed._prune_legacy_embedding_layout()
        self.assertEqual(retried["deferred"], 0)
        self.assertFalse(marker.exists())

    def test_worker_release_retries_deferred_legacy_cleanup(self) -> None:
        with (mock.patch("ask.models_loaded", return_value=False),
              mock.patch("ask.clear_artifact_cache"),
              mock.patch.object(
                  embedding_segments, "prune_legacy_layout") as prune):
            self.assertTrue(semantic.release())
        prune.assert_called_once_with(
            embeddings_path=common.EMBEDDINGS_PATH,
            ids_path=common.IDS_PATH,
            q8_manifest_path=semantic_q8.MANIFEST_PATH,
            q8_artifact_dir=semantic_q8.ARTIFACT_DIR,
            generation_marker_path=semantic.generation_marker_path(),
        )

    def test_unchanged_flat_bundle_migrates_without_inference(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1),
                _row("codex:b:2", "beta", session="b", turn=2)]
        self._write(rows)
        dim = int(embedder.PROFILE["dim"])
        matrix = np.zeros((2, dim), dtype=np.float32)
        matrix[0, 0] = 1.0
        matrix[1, 1] = 1.0
        hashes = [embed._text_hash(row["text"]) for row in rows]
        source = {
            "version": 1, "ingest_signature": "flat-current",
            "files": {"messages": {"size": 10, "mtime_ns": 100}},
        }
        with mock.patch.object(semantic, "source_generation", return_value=source):
            common.write_embeddings(
                [row["id"] for row in rows], matrix,
                common.EMBEDDINGS_PATH, common.IDS_PATH, dim=dim,
                model_id=embedder.PROFILE_STRING, text_hashes=hashes)
            semantic.write_generation_marker(source)
            before = semantic.embedding_coherence()
            with (mock.patch.object(
                      embedder, "get",
                      side_effect=AssertionError("migration loaded ONNX")),
                  mock.patch.object(
                      common, "strict_family_parent_map", return_value={}),
                  mock.patch.object(
                      common, "indexed_family_roots", side_effect=_roots),
                  mock.patch.object(common, "log")):
                self.assertEqual(embed._run(self._args(max_new=128)), 0)
            after = semantic.embedding_coherence()

        self.assertTrue(before["coherent"])
        self.assertTrue(before["migration_pending"])
        self.assertTrue(after["coherent"])
        self.assertFalse(after["migration_pending"])
        manifest = embedding_segments.load_manifest(
            common.EMBEDDINGS_PATH.parent / "embeddings.meta",
            verify_hashes=True, validate_liveness=True)
        self.assertEqual((manifest["live_rows"], len(manifest["segments"])), (2, 1))
        f32_path = embedding_segments.artifact_path(
            manifest, manifest["segments"][0]["artifacts"]["f32"])
        np.testing.assert_array_equal(
            np.fromfile(f32_path, dtype="<f4").reshape((2, dim)), matrix)
        self.assertFalse(semantic.generation_marker_path().exists())
        self.assertFalse(any(path.exists() for path in (
            common.EMBEDDINGS_PATH, common.IDS_PATH, embed.HASHES_PATH)))

    def test_release_does_not_import_an_unloaded_semantic_stack(self) -> None:
        semantic._LAST_USE["mono"] = 123.0
        unloaded = {
            "ask": None,
            "embedder": None,
            "embedding_segments": None,
        }
        with mock.patch.dict(sys.modules, unloaded), \
                mock.patch.object(common, "log") as log:
            self.assertTrue(semantic.release())
        self.assertEqual(semantic.last_use_mono(), 0.0)
        log.assert_not_called()

    def test_release_drops_a_direct_embedder_without_importing_ask(self) -> None:
        direct = mock.Mock()
        direct.model_loaded.return_value = True
        with mock.patch.dict(sys.modules, {
                "ask": None,
                "embedder": direct,
                "embedding_segments": None,
        }):
            self.assertTrue(semantic.release())
        direct.model_loaded.assert_called_once_with()
        direct.release.assert_called_once_with()

    def test_base_append_supersede_and_delete_are_immutable_generations(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1),
                _row("codex:b:2", "beta", session="b", turn=2)]
        source = [{"ingest_signature": "one"}]
        self._write(rows)
        with (mock.patch.object(semantic, "source_generation",
                               side_effect=lambda: source[0]),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots",
                                side_effect=_roots),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=False),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(smoke=2)), 0)
            meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
            base = embedding_segments.load_manifest(meta)
            self.assertEqual((len(base["segments"]), base["live_rows"]), (1, 2))

            rows.append(_row("codex:c:3", "gamma", session="c", turn=3))
            source[0] = {"ingest_signature": "two"}
            self._write(rows)
            corpus = self._write_corpus(rows)
            with mock.patch.object(
                    corpusdb, "connect", side_effect=lambda **_: sqlite3.connect(corpus)):
                rebound = embed._rebase_segmented_generation(
                    {"c"}, expected_previous_source={"ingest_signature": "one"},
                    expected_current_source=source[0])
                self.assertEqual(rebound, {"indexed": 2, "total": 3, "pending": 1})
                with mock.patch.object(
                        embedding_segments, "active_rows",
                        side_effect=AssertionError("append rebuilt the full row catalog")):
                    self.assertEqual(embed._run(self._args(max_new=1)), 0)
            appended = embedding_segments.load_manifest(meta)
            self.assertEqual((len(appended["segments"]), appended["live_rows"]),
                             (2, 3))
            self.assertEqual(base["segments"][0], appended["segments"][0])

            rows[0] = _row("codex:a:1", "alpha changed", session="a", turn=1)
            source[0] = {"ingest_signature": "three"}
            self._write(rows)
            corpus = self._write_corpus(rows)
            with mock.patch.object(
                    corpusdb, "connect", side_effect=lambda **_: sqlite3.connect(corpus)):
                rebound = embed._rebase_segmented_generation(
                    {"a"}, expected_previous_source={"ingest_signature": "two"},
                    expected_current_source=source[0])
                self.assertEqual(
                    rebound, {"indexed": 2, "total": 3, "pending": 1})
            self.assertEqual(embed._run(self._args()), 0)
            superseded = embedding_segments.load_manifest(meta)
            active = embedding_segments.active_rows(superseded)
            self.assertEqual([row["mid"] for row in active],
                             ["codex:b:2", "codex:c:3", "codex:a:1"])
            self.assertEqual(superseded["live_rows"], 3)

            rows.pop(1)
            source[0] = {"ingest_signature": "four"}
            self._write(rows)
            rebound = embed._rebase_segmented_generation(
                {"b"}, expected_previous_source={"ingest_signature": "three"},
                expected_current_source=source[0])
            self.assertEqual(
                rebound, {"indexed": 2, "total": 2, "pending": 0})
            self.assertEqual(embed._run(self._args()), 0)
            deleted = embedding_segments.load_manifest(meta)
            self.assertEqual([row["mid"] for row in
                              embedding_segments.active_rows(deleted)],
                             ["codex:c:3", "codex:a:1"])
            self.assertEqual(deleted["live_rows"], 2)

    def test_reply_inherits_filter_metadata_from_its_prompt(self) -> None:
        prompt = common.Message(
            id="codex:s:4", agent="codex", project="agrep", session="s",
            ts=900, turn=4, text="prompt", who="user", model="gpt-5",
            model_source="explicit")
        embed.REPLIES_PATH.write_text(
            json.dumps({"id": prompt.id, "reply": "answer"}) + "\n",
            encoding="utf-8", newline="\n")
        reply = list(embed.iter_reply_messages({prompt.id: prompt}))[0]
        self.assertEqual((reply.project, reply.model, reply.model_source, reply.ts),
                         ("agrep", "gpt-5", "explicit", 900))

    def test_reply_reader_skips_non_objects_and_preserves_colon_sessions(self) -> None:
        rid = "codex:root:child:7"
        prompt = common.Message(
            id=rid, agent="codex", project="agrep", session="root:child",
            ts=900, turn=7, text="prompt", who="user", model="gpt-5",
            model_source="explicit")
        records = [
            "null", "[]", "7",
            json.dumps({"id": 7, "reply": "numeric id"}),
            json.dumps({"id": rid, "reply": 7}),
            json.dumps({"id": "missing-turn", "reply": "bad"}),
            json.dumps({"id": "codex:s:-1", "reply": "negative"}),
            json.dumps({"id": rid, "reply": "answer"}),
        ]
        embed.REPLIES_PATH.write_text(
            "\n".join(records) + "\n", encoding="utf-8", newline="\n")
        replies = list(embed.iter_reply_messages({rid: prompt}))
        self.assertEqual(len(replies), 1)
        self.assertEqual(
            (replies[0].id, replies[0].agent, replies[0].session,
             replies[0].turn, replies[0].text),
            (rid + "#r", "codex", "root:child", 7, "answer"),
        )

    def test_model_source_only_update_republishes_refs_without_onnx(self) -> None:
        rows = [_row("codex:a:1", "alpha", session="a", turn=1)]
        source = [{"ingest_signature": "one"}]
        self._write(rows)
        with (mock.patch.object(semantic, "source_generation",
                               side_effect=lambda: source[0]),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots",
                                side_effect=_roots),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=False),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(smoke=1)), 0)

            rows[0]["model_source"] = "inferred"
            source[0] = {"ingest_signature": "two"}
            self._write(rows)
            with mock.patch.object(
                    embedder, "get",
                    side_effect=AssertionError("metadata refresh loaded ONNX")):
                self.assertEqual(embed._run(self._args()), 0)

        meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        manifest = embedding_segments.load_manifest(meta)
        active = embedding_segments.active_rows(manifest)
        self.assertEqual([row["model_source"] for row in active], ["inferred"])
        corpus = self._write_corpus(rows)
        with mock.patch.object(common, "transcript_generation",
                               return_value=source[0]):
            _, _, refs, _ = segment_query.open_current(meta)
            refs.corpus_connect = lambda: sqlite3.connect(corpus)
            resolved = refs.resolve([active[0]["row_ref"]])
        self.assertEqual(resolved[0]["model_source"], "inferred")

    def test_cached_backlog_topup_does_not_reconstruct_the_live_row_catalog(self) -> None:
        rows = [_row(f"codex:s:{turn}", f"row {turn}", session="s", turn=turn)
                for turn in range(1, 5)]
        self._write(rows)
        by_id = {
            row["id"]: common.Message(**row) for row in rows
        }

        def resolve(plan_rows):
            return [by_id[str(row[0])] for row in plan_rows]

        with (mock.patch.object(semantic, "source_generation",
                               return_value={"ingest_signature": "one"}),
              mock.patch.object(embedder, "get", return_value=_Embedder()),
              mock.patch.object(common, "strict_family_parent_map", return_value={}),
              mock.patch.object(common, "indexed_family_roots",
                                side_effect=_roots),
              mock.patch.object(embed, "_resolve_pending_messages", side_effect=resolve),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(max_new=1)), 0)
            meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
            first = embedding_segments.load_manifest(meta)
            self.assertEqual((first["live_rows"], first["coverage"]["pending"]),
                             (1, 3))
            with mock.patch.object(
                    embedding_segments, "active_rows",
                    side_effect=AssertionError("top-up rebuilt the full row catalog")):
                self.assertEqual(embed._run(self._args(max_new=1)), 0)
            second = embedding_segments.load_manifest(meta)
            self.assertEqual((second["live_rows"], second["coverage"]["pending"]),
                             (2, 2))

    def test_hard_segment_cap_defers_before_source_or_model_work(self) -> None:
        manifest = {
            "model": embedder.PROFILE_STRING, "segmented": True,
            "metadata_only": True, "rows": 12,
            "segments": {
                "delta_count": 16, "live_rows": 12,
                "coverage": {"total": 20, "pending": 8},
            },
        }
        args = self._args(max_new=1)
        with (mock.patch.object(semantic, "source_generation",
                               return_value={"ingest_signature": "new"}),
              mock.patch.object(embed, "_load_old_manifest", return_value=manifest),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=True) as schedule,
              mock.patch.object(embed, "_publish_state"),
              mock.patch.object(embed, "_scan_source",
                                side_effect=AssertionError("source was scanned")),
              mock.patch.object(embedder, "get",
                                side_effect=AssertionError("model was loaded")),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(args), 0)
        schedule.assert_called_once_with()

    def test_owned_planning_sweeps_crash_orphans_before_new_work(self) -> None:
        orphan = common.DATA_DIR / embedding_segments.SEGMENT_DIR / "crash.bin"
        segmented = embedding_segments.LoadedManifest({
            "delta_count": 0,
            "live_rows": 1,
            "coverage": {"total": 1, "pending": 0},
        }, common.EMBEDDINGS_PATH.parent / "embeddings.meta")
        manifest = {
            "model": embedder.PROFILE_STRING,
            "segmented": True,
            "metadata_only": True,
            "rows": 1,
            "refs_versions": frozenset({5}),
            "segments": segmented,
            "state": {"identity": "fixture"},
        }
        pruned = {"removed": [orphan], "deferred": []}

        def defer(_manifest, _incremental):
            prune.assert_called_once_with(segmented)
            return True

        with mock.patch.object(
                semantic, "source_generation",
                return_value={"ingest_signature": "same"}), \
                mock.patch.object(
                    embed, "_load_old_manifest", return_value=manifest), \
                mock.patch.object(
                    embedding_segments, "prune_orphans",
                    return_value=pruned) as prune, \
                mock.patch.object(
                    embed, "_defer_segment_maintenance",
                    side_effect=defer), \
                mock.patch.object(
                    embed, "_scan_source",
                    side_effect=AssertionError("source was scanned")), \
                mock.patch.object(common, "log"):
            self.assertEqual(embed._run(self._args()), 0)
        prune.assert_called_once_with(segmented)

    def test_legacy_refs_schedule_upgrade_without_max_new_coverage_drop(self) -> None:
        segments = {
            "delta_count": 0, "live_rows": 10_000,
            "coverage": {"total": 10_000, "pending": 0},
        }
        manifest = {
            "model": embedder.PROFILE_STRING, "segmented": True,
            "metadata_only": True, "rows": 10_000,
            "refs_versions": frozenset({1}), "segments": segments,
        }
        with (mock.patch.object(
                  semantic, "source_generation",
                  return_value={"ingest_signature": "same"}),
              mock.patch.object(embed, "_load_old_manifest",
                                return_value=manifest),
              mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=True) as schedule,
              mock.patch.object(embed, "_publish_state") as publish_state,
              mock.patch.object(
                  embed, "_scan_source",
                  side_effect=AssertionError("legacy upgrade scanned source")),
              mock.patch.object(
                  embedder, "get",
                  side_effect=AssertionError("legacy upgrade loaded ONNX")),
              mock.patch.object(common, "log")):
            self.assertEqual(embed._run(self._args(max_new=1)), 0)
        schedule.assert_called_once_with(refresh_metadata=True)
        self.assertEqual(
            publish_state.call_args.args[0]["indexed"],
            segments["live_rows"],
        )
        self.assertEqual(
            publish_state.call_args.args[0]["pending"],
            segments["coverage"]["pending"],
        )

    def test_planning_failure_closes_owned_segment_catalog(self) -> None:
        catalog = embed._SegmentCatalog()
        catalog.add(
            "codex:s:1",
            embed._SegmentCatalogRow("a" * 16, "b" * 32, 0, 0, 0),
        )
        catalog.finish()
        manifest = {
            "model": embedder.PROFILE_STRING, "segmented": True,
            "metadata_only": False, "rows": 1,
            "refs_versions": frozenset({5}),
            "segments": {
                "delta_count": 0, "live_rows": 1,
                "coverage": {"total": 1, "pending": 0},
            },
            "state": {"identity": "fixture"},
            "catalog": catalog,
        }
        with (mock.patch.object(
                  semantic, "source_generation",
                  return_value={"ingest_signature": "same"}),
              mock.patch.object(embed, "_load_old_manifest",
                                return_value=manifest),
              mock.patch.object(embed, "_load_pending_plan", return_value=None),
              mock.patch.object(
                  embed, "_scan_source",
                  side_effect=RuntimeError("injected planning failure")),
              mock.patch.object(embed, "_publish_state"),
              mock.patch.object(common, "log")):
            with self.assertRaisesRegex(RuntimeError, "injected planning failure"):
                embed._run(self._args())
        self.assertTrue(catalog.closed)


if __name__ == "__main__":
    unittest.main()
