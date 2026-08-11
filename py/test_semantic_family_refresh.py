"""Semantic family metadata follows ingest generations without reusing stale groups."""

from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import embed  # noqa: E402
import embedding_segments  # noqa: E402
import semantic_q8  # noqa: E402
import semantic_segment_build  # noqa: E402

_MLX_PATCH = None


def setUpModule() -> None:
    global _MLX_PATCH
    _MLX_PATCH = mock.patch.dict(os.environ, {"AGREP_MLX": "off"})
    _MLX_PATCH.start()


def tearDownModule() -> None:
    _MLX_PATCH.stop()


class SemanticFamilyRefreshTests(unittest.TestCase):
    @staticmethod
    def _message(**updates) -> common.Message:
        fields = {
            "id": "codex:child:1",
            "agent": "codex",
            "project": "project",
            "session": "child",
            "ts": 10,
            "turn": 1,
            "text": "same",
            "who": "user",
            "model": "model-a",
            "model_source": "explicit",
        }
        fields.update(updates)
        return common.Message(**fields)

    @staticmethod
    def _write_family_proof(root: Path, signature: str,
                            rows: list[tuple[str, str]]) -> None:
        (root / ".ingest.sig").write_text(
            signature + "\n", encoding="utf-8")
        (root / common.SESSION_FAMILY_META_FILE).write_text(
            json.dumps({
                "version": common.SESSION_FAMILY_INDEX_VERSION,
                "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
                "ingest_signature": signature,
                "count": len(rows),
                "digest": common.session_family_digest(sorted(rows)),
            }),
            encoding="utf-8",
        )

    @staticmethod
    def _v4_manifest(root: Path, messages: list[common.Message],
                     parents: dict[str, str], source: dict) -> tuple[dict, Path]:
        refs_path = root / "refs.sqlite"
        refs = sqlite3.connect(refs_path)
        memo: dict[str, str] = {}
        labels = [
            semantic_segment_build._family_label(message, parents, memo)
            for message in messages
        ]
        ids = {
            label: index for index, label in enumerate(
                sorted({label for label in labels if label is not None}), 1)
        }
        try:
            embedding_segments._create_refs_schema(refs)
            refs.executemany(
                "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ((index, index, message.id, embed._text_hash(message.text),
                  message.agent, message.project, message.session, message.ts,
                  message.turn, message.who, message.model,
                  0 if label is None else ids[label],
                  semantic_segment_build.refs_metadata_fingerprint(
                      message, label, message.session in parents),
                  label, message.model_source, int(message.session in parents))
                 for index, (message, label) in enumerate(zip(
                     messages, labels, strict=True))),
            )
            refs.commit()
        finally:
            refs.close()
        return ({
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": source,
            "live_rows": len(messages),
            "physical_rows": len(messages),
            "coverage": {"total": len(messages), "pending": 0},
            "generation": "segment-generation",
            "delta_count": 0,
            "shadows": [],
            "segments": [{"artifacts": {"refs": {"path": "fixture"}}}],
        }, refs_path)

    def test_transcript_generation_includes_family_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                '{"id":"codex:child:1","session":"child","text":"same"}\n',
                encoding="utf-8",
            )
            sessions = root / "sessions.jsonl"
            sessions.write_text(
                '{"session":"child"}\n', encoding="utf-8")
            self._write_family_proof(root, "first", [("child", "")])
            first = common.transcript_generation(root)
            sessions.write_text(
                '{"session":"child","n":99,"first_text":"changed"}\n',
                encoding="utf-8",
            )
            metadata_only = common.transcript_generation(root)
            sessions.write_text(
                '{"session":"child","parent":"root"}\n', encoding="utf-8")
            self._write_family_proof(root, "second", [("child", "root")])
            second = common.transcript_generation(root)
        self.assertEqual(first["version"], 4)
        self.assertEqual(first, metadata_only)
        self.assertNotEqual(first["family"], second["family"])

    def test_same_text_metadata_changes_are_pending_without_vector_change(self) -> None:
        old = self._message()
        current = self._message(
            project="new-project", ts=20, who="control",
            model="model-b", model_source="inferred")
        old_meta = semantic_segment_build.message_metadata_fingerprint(
            old, {"child": "old-root"}, {})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                json.dumps({
                    "id": current.id, "agent": current.agent,
                    "project": current.project, "session": current.session,
                    "ts": current.ts, "turn": current.turn,
                    "text": current.text, "who": current.who,
                    "model": current.model,
                    "model_source": current.model_source,
                }) + "\n",
                encoding="utf-8",
            )
            metadata_only: set[str] = set()
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(
                        embed, "REPLIES_PATH", root / "replies.jsonl"), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value={"child": "new-root"}):
                hashes, pending, count = embed._scan_source(
                    rebuild=False,
                    old_hash_by_id={current.id: embed._text_hash(current.text)},
                    max_new=None,
                    old_metadata_by_id={current.id: old_meta},
                    metadata_only_ids=metadata_only,
                )
        self.assertEqual(hashes[current.id], embed._text_hash(current.text))
        self.assertEqual([message.id for message in pending], [current.id])
        self.assertEqual(count, 1)
        self.assertEqual(metadata_only, {current.id})
        self.assertNotEqual(
            semantic_segment_build.message_metadata_fingerprint(
                pending[0], {"child": "new-root"}, {}),
            old_meta,
        )

    def test_role_and_parent_are_part_of_refs_metadata(self) -> None:
        user = self._message(who="user")
        control = self._message(who="control")
        user_old_root = semantic_segment_build.message_metadata_fingerprint(
            user, {"child": "old-root"}, {})
        user_new_root = semantic_segment_build.message_metadata_fingerprint(
            user, {"child": "new-root"}, {})
        excluded = semantic_segment_build.message_metadata_fingerprint(
            control, {"child": "old-root"}, {})
        self.assertNotEqual(user_old_root, user_new_root)
        self.assertNotEqual(user_old_root, excluded)
        self.assertEqual(
            semantic_segment_build._family_label(
                user, {"child": "old-root"}, {}),
            "f:old-root",
        )
        self.assertIsNone(semantic_segment_build._family_label(
            control, {"child": "old-root"}, {}))

    def test_full_rebase_refuses_stale_refs_with_unchanged_text(self) -> None:
        message = self._message(project="current")
        stale = semantic_segment_build.message_metadata_fingerprint(
            self._message(project="stale"), {"child": "root"}, {})
        with mock.patch.object(
                common, "iter_messages", return_value=iter([message])), \
                mock.patch.object(
                    embed, "iter_reply_messages", return_value=iter(())), \
                mock.patch.object(
                    common, "strict_family_parent_map",
                    return_value={"child": "root"}):
            total = embed._full_rebase_total(
                [message.id],
                {message.id: embed._text_hash(message.text)},
                {message.id: stale},
            )
        self.assertIsNone(total)

    def test_reply_metadata_survives_rebase_and_source_binding(self) -> None:
        base = self._message(text="question")
        reply = self._message(
            id=base.id + "#r", text="answer", who="agent")
        messages = (base, reply)
        hashes = {
            message.id: embed._text_hash(message.text)
            for message in messages
        }
        memo: dict[str, str] = {}
        metadata = {
            message.id: semantic_segment_build.message_metadata_fingerprint(
                message, {"child": "root"}, memo)
            for message in messages
        }
        previous = {"version": 4, "family": "previous"}
        current = {"version": 4, "family": "current"}
        with tempfile.TemporaryDirectory() as td:
            replies = Path(td) / "replies.jsonl"
            replies.write_text(
                json.dumps({"id": base.id, "reply": reply.text}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(embed, "REPLIES_PATH", replies), \
                    mock.patch.object(
                        common, "iter_messages",
                        side_effect=lambda **_kwargs: iter([base])), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value={"child": "root"}), \
                    mock.patch.object(
                        embed.semantic, "source_generation",
                        return_value=current):
                total = embed._full_rebase_total(
                    [message.id for message in messages],
                    hashes,
                    metadata,
                )
                binding = embed._validated_source_binding(
                    previous,
                    [message.id for message in messages],
                    [hashes[message.id] for message in messages],
                    len(messages),
                    metadata,
                )
        self.assertEqual(total, 2)
        self.assertEqual(binding, (current, 2))

    def test_delta_rebase_stays_before_the_fts_crash_boundary(self) -> None:
        indexed = self._message(text="indexed")
        covered = self._message(text="covered")
        added = self._message(
            id="codex:child:2", turn=2, text="added")
        other = self._message(
            id="codex:other:1", session="other", text="other")
        reply = self._message(
            id=covered.id + "#r", text="answer", who="agent")
        memo: dict[str, str] = {}
        metadata = {
            message.id: semantic_segment_build.message_metadata_fingerprint(
                message, {}, memo)
            for message in (indexed, reply)
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages_path = root / "messages.jsonl"
            messages_path.write_text("".join(
                json.dumps(message._asdict()) + "\n"
                for message in (covered, added, other)), encoding="utf-8")
            replies_path = root / "replies.jsonl"
            replies_path.write_text(json.dumps({
                "id": covered.id, "reply": reply.text,
            }) + "\n", encoding="utf-8")
            refs_path = root / "refs.sqlite"
            refs = sqlite3.connect(refs_path)
            try:
                embedding_segments._create_refs_schema(refs)
                refs.executemany(
                    "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        (index, index, message.id,
                         embed._text_hash(message.text), message.agent,
                         message.project, message.session, message.ts,
                         message.turn, message.who, message.model, 0,
                         metadata[message.id], "f:child",
                         message.model_source, 0)
                        for index, message in enumerate((indexed, reply))
                    ),
                )
                refs.commit()
            finally:
                refs.close()
            manifest = {
                "physical_rows": 2, "shadows": [],
                "segments": [{"artifacts": {"refs": {"path": "fixture"}}}],
            }
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages_path),
                mock.patch.object(embed, "REPLIES_PATH", replies_path),
                mock.patch.object(
                    common, "strict_family_parent_map", return_value={}),
                mock.patch.object(
                    embedding_segments, "artifact_path",
                    return_value=refs_path),
                mock.patch.object(
                    corpusdb, "connect",
                    side_effect=AssertionError("crossed the FTS boundary")) as fts,
            ):
                result = embed._segmented_delta_rebase_total(
                    manifest, {"child"})
        self.assertIsNotNone(result)
        self.assertEqual(result.total, 4)
        self.assertEqual(
            [message.id for message in result.pending_messages],
            [covered.id, added.id])
        self.assertEqual(result.shadow_refs, [0])
        fts.assert_not_called()

    def test_empty_delta_rebind_stays_before_the_fts_crash_boundary(self) -> None:
        message = self._message(text="covered")
        previous = {
            "version": 4, "family": "same", "ingest_signature": "old"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "new"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 1,
            "coverage": {"total": 1, "pending": 0},
            "generation": "segment-generation", "segments": [],
        }
        rebound = {
            **manifest, "source": current_source,
            "coverage": {"total": 1, "pending": 0},
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages_path = root / "messages.jsonl"
            messages_path.write_text(
                json.dumps(message._asdict()) + "\n", encoding="utf-8")
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages_path),
                mock.patch.object(embed, "REPLIES_PATH", root / "replies.jsonl"),
                mock.patch.object(
                    common, "EMBEDDINGS_PATH", root / "embeddings.f32"),
                mock.patch.object(
                    embedding_segments, "load_manifest",
                    return_value=manifest),
                mock.patch.object(
                    embed.semantic, "source_generation",
                    return_value=current_source),
                mock.patch.object(
                    embed.semantic, "output_generation",
                    return_value={"bundle": "output"}),
                mock.patch.object(
                    embedding_segments, "publish_rebind",
                    return_value=rebound) as publish,
                mock.patch.object(
                    corpusdb, "connect",
                    side_effect=AssertionError("crossed the FTS boundary")) as fts,
            ):
                result = embed._rebase_segmented_generation(
                    set(), expected_previous_source=previous,
                    expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 1, "total": 1, "pending": 0})
        publish.assert_called_once()
        fts.assert_not_called()

    def test_legacy_delta_rebase_supports_colon_session_ids(self) -> None:
        message = self._message(
            id="codex:child:branch:1", session="child:branch",
            text="covered")
        digest = embed._text_hash(message.text)
        with mock.patch.object(
                embed, "_iter_source_messages", return_value=iter([message])):
            total = embed._delta_rebase_total(
                [message.id], {message.id: digest}, {"child:branch"})
        self.assertEqual(total, 1)

    def test_segmented_source_binding_streams_retained_rows_after_movement(
            self) -> None:
        retained = self._message(id="codex:a:1", session="a", text="alpha")
        replaced = self._message(id="codex:b:1", session="b", text="beta")
        retained_hash = embed._text_hash(retained.text)
        replaced_hash = embed._text_hash(replaced.text)
        retained_meta = semantic_segment_build.refs_metadata_fingerprint(
            retained, "f:a")
        replaced_meta = semantic_segment_build.refs_metadata_fingerprint(
            replaced, "f:b")
        catalog = embed._SegmentCatalog()
        try:
            catalog.add(
                retained.id,
                embed._SegmentCatalogRow(
                    retained_hash, retained_meta, 0, 0, 0))
            catalog.add(
                replaced.id,
                embed._SegmentCatalogRow(
                    "0" * 16, "1" * 32, 1, 0, 1))
            catalog.finish()
            previous = {"version": 4, "family": "previous"}
            planned = {"version": 4, "family": "planned"}
            current = {"version": 4, "family": "current"}
            manifest = {
                "catalog": catalog,
                "segments": {"source": previous},
            }
            with tempfile.TemporaryDirectory() as raw, \
                    mock.patch.object(common, "DATA_DIR", Path(raw)), \
                    mock.patch.object(
                        embed, "_iter_source_messages",
                        return_value=iter([retained, replaced])), \
                    mock.patch.object(
                        embed.semantic, "source_generation",
                        return_value=current):
                binding = embed._validated_segmented_source_binding(
                    planned, manifest, 2,
                    [replaced.id], [replaced_hash], [1],
                    {replaced.id: replaced_meta},
                )
            self.assertEqual(binding, (current, 2))
            self.assertEqual(catalog.binding_mismatch_count([1]), 0)
            self.assertEqual(catalog.expected_mismatch_count(
                [replaced.id], [replaced_hash],
                {replaced.id: replaced_meta}), 0)
        finally:
            catalog.close()

    def test_metadata_scan_fails_closed_without_family_proof(self) -> None:
        message = self._message()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                json.dumps({
                    "id": message.id, "agent": message.agent,
                    "session": message.session, "turn": message.turn,
                    "text": message.text,
                }) + "\n",
                encoding="utf-8",
            )
            (root / "sessions.jsonl").write_text(
                '{"session":"child"}\n', encoding="utf-8")
            with mock.patch.object(
                    common, "DATA_DIR", root), \
                    mock.patch.object(
                    common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(
                        embed, "REPLIES_PATH", root / "replies.jsonl"), \
                    mock.patch.object(
                        common, "_open_session_family_index",
                        return_value=None), \
                    mock.patch.object(
                        common, "await_family_publication",
                        return_value=None):
                with self.assertRaisesRegex(
                        RuntimeError, "family publication is unavailable"):
                    embed._scan_source(
                        rebuild=False,
                        old_hash_by_id={
                            message.id: embed._text_hash(message.text),
                        },
                        max_new=None,
                        old_metadata_by_id={message.id: "0" * 32},
                    )

    def test_metadata_scan_uses_verified_families_while_index_lags(self) -> None:
        message = self._message()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                json.dumps({
                    "id": message.id, "agent": message.agent,
                    "session": message.session, "turn": message.turn,
                    "text": message.text,
                }) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "MESSAGES_PATH", root / "messages.jsonl"), \
                    mock.patch.object(
                        embed, "REPLIES_PATH", root / "replies.jsonl"), \
                    mock.patch.object(
                        common, "_open_session_family_index",
                        return_value=None), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value={"child": "root"}):
                current, pending, count = embed._scan_source(
                    rebuild=False,
                    old_hash_by_id={message.id: embed._text_hash(message.text)},
                    max_new=None,
                    old_metadata_by_id={message.id: "0" * 32},
                )

        self.assertEqual(current[message.id], embed._text_hash(message.text))
        self.assertEqual([item.id for item in pending], [message.id])
        self.assertEqual(count, 1)

    def test_catalog_refuses_split_family_namespace(self) -> None:
        rows = [
            {
                "mid": "a", "session": "child-a", "family_id": 1,
                "family_label": "f:root",
            },
            {
                "mid": "b", "session": "child-b", "family_id": 2,
                "family_label": "f:root",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = root / "embeddings.meta"
            meta.write_text("{}", encoding="utf-8")
            catalog = sqlite3.connect(":memory:")
            catalog.execute(
                "CREATE TABLE families("
                "label TEXT PRIMARY KEY,id INTEGER NOT NULL UNIQUE)")
            with mock.patch.object(
                    common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        embedding_segments, "load_manifest",
                        return_value={"manifest": "fixture"}), \
                    mock.patch.object(
                        embedding_segments, "iter_active_rows",
                        return_value=iter(rows)):
                with self.assertRaisesRegex(
                        RuntimeError, "cannot be reconstructed safely"):
                    semantic_segment_build._seed_catalog(catalog, {})
            catalog.close()

    def test_complete_legacy_ref_replacement_resets_split_namespace(self) -> None:
        rows = [
            {
                "mid": "a", "session": "child-a", "family_id": 1,
                "family_label": None,
            },
            {
                "mid": "b", "session": "child-b", "family_id": 2,
                "family_label": None,
            },
        ]
        messages = [
            self._message(id="a", session="child-a"),
            self._message(id="b", session="child-b"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "embeddings.meta").write_text("{}", encoding="utf-8")
            with mock.patch.object(
                    semantic_segment_build, "_catalog_path",
                    return_value=root / "families.sqlite"), \
                    mock.patch.object(
                        common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value={
                            "child-a": "root",
                            "child-b": "root",
                        }), \
                    mock.patch.object(
                        common, "indexed_family_roots",
                        return_value={
                            "child-a": "root",
                            "child-b": "root",
                        }), \
                    mock.patch.object(
                        embedding_segments, "load_manifest",
                        return_value={"manifest": "fixture"}), \
                    mock.patch.object(
                        embedding_segments, "iter_active_rows",
                        return_value=iter(rows)):
                family_ids, labels = semantic_segment_build._family_ids(
                    messages, allow_legacy_reset=True)
        self.assertEqual(family_ids, [1, 1])
        self.assertEqual(labels, ["f:root", "f:root"])
        self.assertTrue(embed._replaces_all_active_refs(
            {"catalog": {
                "a": embed._SegmentCatalogRow("", None, 1, 0, 0),
                "b": embed._SegmentCatalogRow("", None, 2, 0, 1),
            }},
            [1, 2],
            append_segmented=True,
        ))
        self.assertFalse(embed._replaces_all_active_refs(
            {"catalog": {
                "a": embed._SegmentCatalogRow("", None, 1, 0, 0),
                "b": embed._SegmentCatalogRow("", None, 2, 0, 1),
            }},
            [1],
            append_segmented=True,
        ))

    def test_normal_family_topup_uses_only_indexed_pending_roots(self) -> None:
        message = self._message()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                    semantic_segment_build, "_catalog_path",
                    return_value=root / "families.sqlite"), \
                    mock.patch.object(
                        common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "indexed_family_roots",
                        return_value={"child": "root"}) as indexed, \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        side_effect=AssertionError("full family census loaded")), \
                    mock.patch.object(
                        common, "read_session_family_census",
                        side_effect=AssertionError("family JSONL was scanned")):
                family_ids, labels = semantic_segment_build._family_ids(
                    [message])
        self.assertEqual(family_ids, [1])
        self.assertEqual(labels, ["f:root"])
        indexed.assert_called_once_with({"child"})

    def test_family_topup_carries_indexed_side_provenance(self) -> None:
        message = self._message()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                    semantic_segment_build, "_catalog_path",
                    return_value=root / "families.sqlite"), \
                    mock.patch.object(
                        common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "indexed_family_metadata",
                        return_value={"child": ("root", True)}) as indexed, \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        side_effect=AssertionError("full family census loaded")):
                family_ids, labels, sides = semantic_segment_build._family_ids(
                    [message], _include_sides=True)
        self.assertEqual(family_ids, [1])
        self.assertEqual(labels, ["f:root"])
        self.assertEqual(sides, [True])
        indexed.assert_called_once_with({"child"})

    def test_family_topup_uses_verified_census_while_index_lags(self) -> None:
        message = self._message()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions.jsonl").write_text(
                '{"session":"child","parent":"root"}\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                    semantic_segment_build, "_catalog_path",
                    return_value=root / "families.sqlite"), \
                    mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "indexed_family_roots", return_value=None), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value={"child": "root"}):
                family_ids, labels = semantic_segment_build._family_ids(
                    [message])

        self.assertEqual(family_ids, [1])
        self.assertEqual(labels, ["f:root"])

    def test_legacy_group_publication_requires_family_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ids = root / "embeddings.ids"
            ids.write_text("codex:child:1\n", encoding="utf-8")
            messages = root / "messages.jsonl"
            messages.write_text(
                '{"id":"codex:child:1","who":"user","text":"same"}\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                    common, "strict_family_parent_map", return_value=None):
                with self.assertRaisesRegex(
                        RuntimeError, "family publication is unavailable"):
                    semantic_q8._write_family_groups(
                        root / "groups.ids",
                        ids_path=ids,
                        messages_path=messages,
                    )

    def test_metadata_only_vector_copy_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root / embedding_segments.SEGMENT_DIR
            directory.mkdir()
            matrix = np.arange(12, dtype=np.float32).reshape(3, 4)
            path = directory / "segment.f32"
            matrix.tofile(path)
            segments = embedding_segments.LoadedManifest({
                "segments": [{
                    "rows": 3,
                    "artifacts": {
                        "f32": {
                            "path": f"{embedding_segments.SEGMENT_DIR}/"
                                    "segment.f32",
                        },
                    },
                }],
            }, root / "embeddings.meta")
            manifest = {
                "segments": segments,
                "catalog": {
                    "a": embed._SegmentCatalogRow("", None, 2, 0, 2),
                    "b": embed._SegmentCatalogRow("", None, 0, 0, 0),
                },
            }
            copied = embed._segment_vectors_for_ids(
                manifest, ["a", "b"], 4)
            messages = [
                self._message(id="a", session="a"),
                self._message(id="b", session="b"),
            ]
            with mock.patch.object(embed.embedder, "get") as load_model:
                pending = embed._materialize_pending_vectors(
                    messages, ["a" * 32, "b" * 32], {"a", "b"},
                    append_segmented=True, manifest=manifest, dim=4,
                    source=None,
                    args=mock.Mock(smoke=None, background=False),
                    state={"state": "running"},
                )
        np.testing.assert_array_equal(copied, matrix[[2, 0]])
        np.testing.assert_array_equal(pending.parts[0], matrix[[2, 0]])
        self.assertEqual([message.id for message in pending.messages], ["a", "b"])
        load_model.assert_not_called()

    def test_segment_manifest_retains_one_catalog_without_row_mirrors(self) -> None:
        rows = [
            {
                "mid": "a", "text_hash": "a" * 16,
                "metadata_hash": "b" * 32, "row_ref": 4,
                "model_source_stored": True,
                "segment": 1, "local_ord": 2,
            },
            {
                "mid": "b", "text_hash": "c" * 16,
                "metadata_hash": "d" * 32, "row_ref": 7,
                "model_source_stored": False,
                "segment": 2, "local_ord": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = root / "embeddings.meta"
            meta.write_text('{"version":2}', encoding="utf-8")
            segmented = {
                "model": {"id": embed.embedder.PROFILE_STRING, "dim": 4},
                "live_rows": 2,
            }
            with mock.patch.object(
                    common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "embedding_artifact_state",
                        return_value={"identity": "fixture"}), \
                    mock.patch.object(
                        embedding_segments, "load_manifest",
                        return_value=segmented), \
                    mock.patch.object(
                        embedding_segments, "refs_schema_versions",
                        return_value=frozenset({5})), \
                    mock.patch.object(
                        embedding_segments, "iter_active_rows",
                        return_value=iter(rows)), \
                    mock.patch.object(
                        embedding_segments, "active_rows",
                        side_effect=AssertionError("full row mirror built")):
                manifest = embed._load_old_manifest(4)
        self.assertIsNotNone(manifest)
        self.assertEqual(set(manifest["catalog"]), {"a", "b"})
        self.assertFalse({
            "active_rows", "ids", "hashes", "metadata_hashes", "row_refs",
        } & set(manifest))
        self.assertEqual(
            manifest["catalog"]["a"],
            embed._SegmentCatalogRow("a" * 16, "b" * 32, 4, 1, 2),
        )
        self.assertIsNone(manifest["catalog"]["b"].metadata_hash)
        embed._close_manifest_catalog(manifest)

    def test_large_segment_catalog_keeps_only_bounded_write_buffers(self) -> None:
        catalog = embed._SegmentCatalog()
        try:
            for index in range(50_000):
                digest = f"{index:016x}"
                catalog.add(
                    f"codex:s:{index}",
                    embed._SegmentCatalogRow(
                        digest, digest * 2, index, index // 10_000,
                        index % 10_000),
                )
                self.assertLess(len(catalog.buffer), 4096)
            catalog.finish()
            catalog.begin_current()
            for index in range(50_000):
                digest = f"{index:016x}"
                catalog.add_current(f"codex:s:{index}", digest, digest * 2, False)
                self.assertLess(len(catalog.current_buffer), 4096)
                self.assertLess(len(catalog.current_ids), 4096)
            catalog.finish_current()
            self.assertEqual(len(catalog), 50_000)
            self.assertEqual(catalog.covered_mismatch_count(), 0)
            plan = embed._RefreshPlan(
                source={"generation": "current"},
                replacement_generation=None,
                manifest={
                    "rows": 50_000, "segmented": True,
                    "catalog": catalog, "metadata_only": False,
                },
                incremental=True, cached_plan=None,
                cur_hash=catalog.current_hashes(), messages=[],
                selected_hashes=[], pending_count=0, total_rows=50_000,
                manifest_output="fixture", pending_plan_active=False,
                metadata_only_ids=catalog.metadata_only_ids(),
            )
            retention = embed._plan_retention(plan, 4)
            self.assertEqual(retention.kept_count, 50_000)
            self.assertEqual(retention.kept_ids, [])
            self.assertEqual(retention.kept_hashes, [])
        finally:
            catalog.close()

    def test_manifest_row_mismatch_closes_catalog_before_fallback(self) -> None:
        rows = [{
            "mid": "a", "text_hash": "a" * 16,
            "metadata_hash": "b" * 32, "row_ref": 0,
            "model_source_stored": True, "segment": 0, "local_ord": 0,
        }]
        created: list[embed._SegmentCatalog] = []
        catalog_type = embed._SegmentCatalog

        def new_catalog():
            catalog = catalog_type()
            created.append(catalog)
            return catalog

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "embeddings.meta").write_text(
                '{"version":2}', encoding="utf-8")
            segmented = {
                "model": {"id": embed.embedder.PROFILE_STRING, "dim": 4},
                "live_rows": 2,
            }
            with mock.patch.object(
                    common, "EMBEDDINGS_PATH", root / "embeddings.f32"), \
                    mock.patch.object(
                        common, "embedding_artifact_state",
                        return_value={"identity": "fixture"}), \
                    mock.patch.object(
                        embedding_segments, "load_manifest",
                        return_value=segmented), \
                    mock.patch.object(
                        embedding_segments, "refs_schema_versions",
                        return_value=frozenset({5})), \
                    mock.patch.object(
                        embedding_segments, "iter_active_rows",
                        return_value=iter(rows)), \
                    mock.patch.object(
                        embed, "_SegmentCatalog", side_effect=new_catalog):
                self.assertIsNone(embed._load_old_manifest(4))
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)

    def test_temp_catalog_schema_failure_closes_connection(self) -> None:
        for catalog_type in (
                embed._SegmentCatalog, embed._ReplyContextCatalog):
            with self.subTest(catalog=catalog_type.__name__):
                connection = mock.Mock()
                connection.executescript.side_effect = RuntimeError("injected")
                with mock.patch.object(
                        embed.sqlite3, "connect", return_value=connection):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        catalog_type()
                connection.close.assert_called_once_with()

    def test_planning_retains_only_compact_reply_context(self) -> None:
        messages = [
            self._message(id="codex:a:1", session="a", turn=1, text="alpha"),
            self._message(id="codex:b:2", session="b", turn=2, text="beta"),
        ]
        captured: dict[str, object] = {}

        def inspect_contexts(base_rows, wanted_ids=None):
            captured.update(base_rows)
            return iter(())

        with tempfile.TemporaryDirectory() as td:
            replies = Path(td) / "replies.jsonl"
            replies.write_text("", encoding="utf-8")
            with mock.patch.object(embed, "REPLIES_PATH", replies), \
                    mock.patch.object(
                        common, "iter_messages",
                        return_value=iter(messages)), \
                    mock.patch.object(
                        embed, "iter_reply_messages",
                        side_effect=inspect_contexts):
                embed._scan_source(
                    rebuild=True, old_hash_by_id=None, max_new=1)
        self.assertEqual(set(captured), {message.id for message in messages})
        self.assertTrue(all(
            isinstance(value, embed._ReplyContext)
            and not isinstance(value, common.Message)
            and not hasattr(value, "text")
            for value in captured.values()
        ))

    def test_selected_reply_recovery_retains_only_wanted_base_context(self) -> None:
        messages = [
            self._message(id="codex:a:1", session="a", turn=1, text="alpha"),
            self._message(id="codex:b:2", session="b", turn=2, text="beta"),
            self._message(id="codex:c:3", session="c", turn=3, text="gamma"),
        ]
        reply = common.Message(
            id="codex:b:2#r", agent="codex", project="project",
            session="b", ts=10, turn=2, text="answer", who="agent",
            model="model-a", model_source="explicit",
        )

        def selected_only(base_rows, wanted_ids=None):
            self.assertEqual(set(base_rows), {"codex:b:2"})
            self.assertEqual(wanted_ids, {reply.id})
            self.assertIsInstance(base_rows["codex:b:2"], embed._ReplyContext)
            self.assertFalse(hasattr(base_rows["codex:b:2"], "text"))
            return iter([reply])

        with mock.patch.object(
                common, "iter_messages", return_value=iter(messages)), \
                mock.patch.object(
                    embed, "iter_reply_messages", side_effect=selected_only):
            found = embed._messages_for_ids(
                [reply.id], [embed._text_hash(reply.text)])
        self.assertEqual(found, [reply])

    def test_non_exact_rebase_refuses_family_generation_change(self) -> None:
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": {"version": 4, "family": "old"},
            "live_rows": 1,
            "coverage": {"total": 1, "pending": 0},
            "generation": "segment-generation",
        }
        current = {"version": 4, "family": "new"}
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation", return_value=current), \
                mock.patch.object(
                    embedding_segments, "publish_rebind") as publish:
            result = embed._rebase_segmented_generation(
                {"child"},
                expected_previous_source=None,
                expected_current_source=current,
            )
        self.assertIsNone(result)
        publish.assert_not_called()

    def test_pending_plan_rebase_uses_old_and_new_live_counts(self) -> None:
        previous = {"version": 4, "family": "old", "ingest_signature": "g0"}
        current = {"version": 4, "family": "new", "ingest_signature": "g1"}
        kept = self._message(
            id="codex:keep:1", session="keep", text="kept")
        replaced = self._message(text="old")
        replacements = [
            self._message(text="changed"),
            self._message(id="codex:child:2", turn=2, text="added"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(common, "DATA_DIR", root):
                builder = embed._PendingPlanBuilder(
                    previous, "old-output", embed.embedder.PROFILE_STRING)
                builder.add(kept, embed._text_hash(kept.text), 0)
                builder.add(replaced, embed._text_hash(replaced.text), 1)
                builder.publish(4)
                db = sqlite3.connect(embed._pending_plan_path())
                try:
                    db.execute("UPDATE meta SET value='1' WHERE key='reusable'")
                    db.commit()
                finally:
                    db.close()
                moved = embed._rebase_pending_plan(
                    previous, current, "old-output", "new-output",
                    previous_live_rows=2, current_live_rows=1,
                    previous_pending=2, total=4,
                    changed_sessions={"child"}, messages=replacements)
                db = sqlite3.connect(embed._pending_plan_path())
                try:
                    meta = dict(db.execute("SELECT key,value FROM meta"))
                    pending = list(db.execute(
                        "SELECT mid,session FROM pending ORDER BY seq"))
                finally:
                    db.close()
        self.assertTrue(moved)
        self.assertEqual(meta["source"], embed._generation_key(current))
        self.assertEqual(meta["output"], "new-output")
        self.assertEqual((meta["total"], meta["pending"]), ("4", "3"))
        self.assertEqual(pending, [
            (kept.id, "keep"),
            (replacements[0].id, "child"),
            (replacements[1].id, "child"),
        ])

    def test_exact_ingest_rebase_moves_an_existing_partial_plan(self) -> None:
        previous = {"version": 4, "family": "same", "ingest_signature": "old"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "new"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 10,
            "coverage": {"total": 20, "pending": 10},
            "generation": "segment-generation",
        }
        current = {
            **manifest, "live_rows": 9,
            "source": current_source,
            "coverage": {"total": 21, "pending": 12},
        }
        added = self._message(id="codex:child:2", turn=2, text="new")
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation", return_value=current_source), \
                mock.patch.object(
                    embed, "_segmented_delta_rebase_total",
                    return_value=embed._SegmentedRebaseDelta(
                        21, [added], [4])), \
                mock.patch.object(
                    embed.semantic, "output_generation",
                    side_effect=({"bundle": "old-output"},
                                 {"bundle": "new-output"})), \
                mock.patch.object(
                    embedding_segments, "publish_delta", return_value=current), \
                mock.patch.object(
                    embed, "_rebase_pending_plan", return_value=True) as rebase, \
                mock.patch.object(embed, "_schedule_segment_compaction"):
            result = embed._rebase_segmented_generation(
                {"child"}, expected_previous_source=previous,
                expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 9, "total": 21, "pending": 12})
        rebase.assert_called_once_with(
            previous, current_source, "old-output", "new-output",
            previous_live_rows=10, current_live_rows=9,
            previous_pending=10, total=21,
            changed_sessions={"child"}, messages=[added])

    def test_exact_v4_rebase_accepts_a_new_independent_session(self) -> None:
        previous = {
            "version": 4, "family": "family-count-2", "ingest_signature": "g0"}
        current_source = {
            "version": 4, "family": "family-count-3", "ingest_signature": "g1"}
        old = [
            self._message(id="codex:a:1", session="a", text="alpha"),
            self._message(id="codex:b:1", session="b", text="beta"),
        ]
        added = self._message(id="codex:c:1", session="c", text="gamma")
        with tempfile.TemporaryDirectory() as raw:
            manifest, refs_path = self._v4_manifest(
                Path(raw), old, {}, previous)
            rebound = {
                **manifest, "source": current_source,
                "coverage": {"total": 3, "pending": 1},
            }
            with mock.patch.object(
                    embedding_segments, "load_manifest",
                    return_value=manifest), \
                    mock.patch.object(
                        embedding_segments, "artifact_path",
                        return_value=refs_path), \
                    mock.patch.object(
                        common, "strict_family_parent_map", return_value={}), \
                    mock.patch.object(
                        embed, "_iter_source_messages",
                        return_value=iter([*old, added])), \
                    mock.patch.object(
                        embed.semantic, "source_generation",
                        return_value=current_source), \
                    mock.patch.object(
                        embed.semantic, "output_generation",
                        side_effect=({"bundle": "old-output"},
                                     {"bundle": "new-output"})), \
                    mock.patch.object(
                        embedding_segments, "publish_rebind",
                        return_value=rebound) as publish, \
                    mock.patch.object(
                        embed, "_publish_rebased_pending_plan",
                        return_value=True) as pending:
                result = embed._rebase_segmented_generation(
                    {"c"}, expected_previous_source=previous,
                    expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 2, "total": 3, "pending": 1})
        publish.assert_called_once()
        pending.assert_called_once_with(
            current_source, "new-output", 3, [added])

    def test_exact_v4_rebase_shadows_a_deleted_whole_session(self) -> None:
        previous = {
            "version": 4, "family": "family-count-3", "ingest_signature": "g0"}
        current_source = {
            "version": 4, "family": "family-count-2", "ingest_signature": "g1"}
        old = [
            self._message(id="codex:a:1", session="a", text="alpha"),
            self._message(id="codex:b:1", session="b", text="beta"),
            self._message(id="codex:c:1", session="c", text="gamma"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            manifest, refs_path = self._v4_manifest(
                Path(raw), old, {}, previous)
            rebound = {
                **manifest, "source": current_source, "live_rows": 2,
                "coverage": {"total": 2, "pending": 0}, "delta_count": 1,
            }
            with mock.patch.object(
                    embedding_segments, "load_manifest",
                    return_value=manifest), \
                    mock.patch.object(
                        embedding_segments, "artifact_path",
                        return_value=refs_path), \
                    mock.patch.object(
                        common, "strict_family_parent_map", return_value={}), \
                    mock.patch.object(
                        embed, "_iter_source_messages",
                        return_value=iter(old[:2])), \
                    mock.patch.object(
                        embed.semantic, "source_generation",
                        return_value=current_source), \
                    mock.patch.object(
                        embed.semantic, "output_generation",
                        side_effect=({"bundle": "old-output"},
                                     {"bundle": "new-output"})), \
                    mock.patch.object(
                        embedding_segments, "publish_delta",
                        return_value=rebound) as publish, \
                    mock.patch.object(
                        embed, "_schedule_segment_compaction") as schedule:
                result = embed._rebase_segmented_generation(
                    {"c"}, expected_previous_source=previous,
                    expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 2, "total": 2, "pending": 0})
        self.assertEqual(publish.call_args.kwargs["shadows"], [2])
        schedule.assert_called_once_with()

    def test_exact_v4_rebase_expands_reparented_descendants(self) -> None:
        previous = {
            "version": 4, "family": "old-tree", "ingest_signature": "g0"}
        current_source = {
            "version": 4, "family": "new-tree", "ingest_signature": "g1"}
        rows = [
            self._message(id="codex:r:1", session="r", text="root"),
            self._message(id="codex:c:1", session="c", text="child"),
            self._message(id="codex:d:1", session="d", text="descendant"),
        ]
        old_parents = {"c": "r", "d": "c"}
        current_parents = {"c": "x", "d": "c"}
        with tempfile.TemporaryDirectory() as raw:
            manifest, refs_path = self._v4_manifest(
                Path(raw), rows, old_parents, previous)
            rebound = {
                **manifest, "source": current_source, "live_rows": 1,
                "coverage": {"total": 3, "pending": 2}, "delta_count": 1,
            }
            with mock.patch.object(
                    embedding_segments, "load_manifest",
                    return_value=manifest), \
                    mock.patch.object(
                        embedding_segments, "artifact_path",
                        return_value=refs_path), \
                    mock.patch.object(
                        common, "strict_family_parent_map",
                        return_value=current_parents), \
                    mock.patch.object(
                        embed, "_iter_source_messages",
                        return_value=iter(rows)), \
                    mock.patch.object(
                        embed.semantic, "source_generation",
                        return_value=current_source), \
                    mock.patch.object(
                        embed.semantic, "output_generation",
                        side_effect=({"bundle": "old-output"},
                                     {"bundle": "new-output"})), \
                    mock.patch.object(
                        embedding_segments, "publish_delta",
                        return_value=rebound) as publish, \
                    mock.patch.object(
                        embed, "_publish_rebased_pending_plan",
                        return_value=True) as pending, \
                    mock.patch.object(
                        embed, "_schedule_segment_compaction") as schedule:
                result = embed._rebase_segmented_generation(
                    {"c"}, expected_previous_source=previous,
                    expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 1, "total": 3, "pending": 2})
        self.assertEqual(publish.call_args.kwargs["shadows"], [1, 2])
        pending.assert_called_once_with(
            current_source, "new-output", 3, rows[1:])
        schedule.assert_called_once_with()

    def test_exact_rebase_shadows_only_moved_covered_rows(self) -> None:
        previous = {"version": 4, "family": "same", "ingest_signature": "old"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "new"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 2,
            "coverage": {"total": 2, "pending": 0},
            "generation": "segment-generation",
        }
        changed = self._message(text="changed")
        added = self._message(id="codex:child:2", turn=2, text="new")
        rebound = {
            **manifest, "source": current_source, "live_rows": 1,
            "coverage": {"total": 3, "pending": 2},
        }
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation",
                    return_value=current_source), \
                mock.patch.object(
                    embed, "_segmented_delta_rebase_total",
                    return_value=embed._SegmentedRebaseDelta(
                        3, [changed, added], [0])), \
                mock.patch.object(
                    embed.semantic, "output_generation",
                    side_effect=({"bundle": "old-output"},
                                 {"bundle": "new-output"})), \
                mock.patch.object(
                    embedding_segments, "publish_delta",
                    return_value=rebound) as publish, \
                mock.patch.object(
                    embedding_segments, "publish_rebind") as rebind, \
                mock.patch.object(
                    embed, "_publish_rebased_pending_plan",
                    return_value=True) as pending, \
                mock.patch.object(embed, "_schedule_segment_compaction"):
            result = embed._rebase_segmented_generation(
                {"child"}, expected_previous_source=previous,
                expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 1, "total": 3, "pending": 2})
        publish.assert_called_once_with(
            common.EMBEDDINGS_PATH.parent / "embeddings.meta",
            source=current_source, artifacts=None, shadows=[0],
            coverage={"total": 3}, expected_generation="segment-generation",
            _before_replace=mock.ANY)
        rebind.assert_not_called()
        pending.assert_called_once_with(
            current_source, "new-output", 3, [changed, added])

    def test_exact_rebase_allows_a_deleted_covered_row(self) -> None:
        previous = {"version": 4, "family": "same", "ingest_signature": "old"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "new"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 2,
            "coverage": {"total": 2, "pending": 0},
            "generation": "segment-generation",
        }
        rebound = {
            **manifest, "source": current_source, "live_rows": 1,
            "coverage": {"total": 1, "pending": 0},
        }
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation",
                    return_value=current_source), \
                mock.patch.object(
                    embed, "_segmented_delta_rebase_total",
                    return_value=embed._SegmentedRebaseDelta(1, [], [0])), \
                mock.patch.object(
                    embed.semantic, "output_generation",
                    side_effect=({"bundle": "old-output"},
                                 {"bundle": "new-output"})), \
                mock.patch.object(
                    embedding_segments, "publish_delta",
                    return_value=rebound) as publish, \
                mock.patch.object(embed, "_schedule_segment_compaction"):
            result = embed._rebase_segmented_generation(
                {"child"}, expected_previous_source=previous,
                expected_current_source=current_source)
        self.assertEqual(result, {"indexed": 1, "total": 1, "pending": 0})
        publish.assert_called_once_with(
            common.EMBEDDINGS_PATH.parent / "embeddings.meta",
            source=current_source, artifacts=None, shadows=[0],
            coverage={"total": 1}, expected_generation="segment-generation",
            _before_replace=mock.ANY)

    def test_seventeenth_shadow_generation_defers_to_compaction(self) -> None:
        previous = {"version": 4, "family": "same", "ingest_signature": "g0"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "g1"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 2, "delta_count": 16,
            "coverage": {"total": 2, "pending": 0},
            "generation": "sixteenth-generation",
        }
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation",
                    return_value=current_source), \
                mock.patch.object(
                    embed, "_segmented_delta_rebase_total",
                    return_value=embed._SegmentedRebaseDelta(1, [], [0])), \
                mock.patch.object(
                    embed.semantic, "output_generation",
                    return_value={"bundle": "old-output"}), \
                mock.patch.object(
                    embed, "_schedule_segment_compaction",
                    return_value=False) as schedule, \
                mock.patch.object(
                    embedding_segments, "publish_delta") as publish:
            result = embed._rebase_segmented_generation(
                {"child"}, expected_previous_source=previous,
                expected_current_source=current_source)
        self.assertIsNone(result)
        schedule.assert_called_once_with()
        publish.assert_not_called()

    def test_exact_rebase_refuses_to_shadow_every_live_row(self) -> None:
        previous = {"version": 4, "family": "same", "ingest_signature": "old"}
        current_source = {
            "version": 4, "family": "same", "ingest_signature": "new"}
        manifest = {
            "model": {
                "id": embed.embedder.PROFILE_STRING,
                "dim": int(embed.embedder.PROFILE["dim"]),
            },
            "source": previous, "live_rows": 1,
            "coverage": {"total": 1, "pending": 0},
            "generation": "segment-generation",
        }
        with mock.patch.object(
                embedding_segments, "load_manifest", return_value=manifest), \
                mock.patch.object(
                    embed.semantic, "source_generation",
                    return_value=current_source), \
                mock.patch.object(
                    embed, "_segmented_delta_rebase_total",
                    return_value=embed._SegmentedRebaseDelta(1, [], [0])), \
                mock.patch.object(
                    embed.semantic, "output_generation",
                    return_value={"bundle": "old-output"}), \
                mock.patch.object(embedding_segments, "publish_delta") as publish:
            result = embed._rebase_segmented_generation(
                {"child"}, expected_previous_source=previous,
                expected_current_source=current_source)
        self.assertIsNone(result)
        publish.assert_not_called()

    def test_segmented_marker_retry_reloads_winning_manifest(self) -> None:
        previous = {"version": 4, "ingest_signature": "old"}
        current_source = {"version": 4, "ingest_signature": "new"}

        def manifest(generation: str) -> dict:
            return {
                "model": {
                    "id": embed.embedder.PROFILE_STRING,
                    "dim": int(embed.embedder.PROFILE["dim"]),
                },
                "source": previous,
                "live_rows": 1,
                "coverage": {"total": 1, "pending": 0},
                "generation": generation,
                "delta_count": 0,
            }

        first = manifest("generation-before-race")
        winner = manifest("generation-from-winner")
        rebound = {
            **winner,
            "generation": "rebound-generation",
            "source": current_source,
        }
        expected_generations: list[str] = []

        def publish(_meta, **kwargs):
            expected_generations.append(kwargs["expected_generation"])
            kwargs["_before_replace"]()
            if len(expected_generations) == 1:
                raise embedding_segments.SegmentPublicationRace("injected")
            return rebound

        meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text('{"version":2}', encoding="utf-8")

        with (
            mock.patch.object(
                embedding_segments, "load_manifest",
                side_effect=(first, winner)) as load,
            mock.patch.object(
                embed.semantic, "source_generation", return_value=current_source),
            mock.patch.object(
                embed.semantic, "output_generation",
                return_value={"bundle": "output"}),
            mock.patch.object(
                embed, "_segmented_delta_rebase_total",
                return_value=embed._SegmentedRebaseDelta(1, [], [])),
            mock.patch.object(
                embedding_segments, "publish_rebind", side_effect=publish),
            mock.patch.object(
                embed, "_publish_rebased_pending_plan", return_value=True),
        ):
            result = embed.rebase_generation_marker(
                set(), expected_previous_source=previous,
                expected_current_source=current_source)

        self.assertEqual(result, {"indexed": 1, "total": 1, "pending": 0})
        self.assertEqual(load.call_count, 2)
        self.assertEqual(expected_generations, [
            "generation-before-race", "generation-from-winner"])

    def test_segmented_marker_retry_is_bounded_and_typed(self) -> None:
        meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text('{"version":2}', encoding="utf-8")
        race = embedding_segments.SegmentPublicationRace("still contended")

        with mock.patch.object(
                embed, "_rebase_segmented_generation",
                side_effect=(race, race, AssertionError("third attempt"))) as rebase:
            self.assertIsNone(embed.rebase_generation_marker({"changed"}))
        self.assertEqual(rebase.call_count, 2)

        with mock.patch.object(
                embed, "_rebase_segmented_generation",
                side_effect=RuntimeError("validation failed")) as rebase:
            self.assertIsNone(embed.rebase_generation_marker({"changed"}))
        rebase.assert_called_once()


if __name__ == "__main__":
    unittest.main()
