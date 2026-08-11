from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sabel_physical
import sabel_pool
import sabel_shadow
import test_sabel_bundle as bundle_fixture


ATOM_TEXT = b"0123456789abcdefghijABCDEFGHIJklmnopqrst"
HANDLE = b"@session:1.digest"
CONTENT_KEYS = {
    "artifact_id", "path", "size_bytes", "sha256", "media_type", "encoding",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_artifact(artifact_id: str, data: bytes, *, path: str | None = None,
                     media_type: str = "application/octet-stream",
                     encoding: str | None = "utf-8"):
    return {
        "artifact_id": artifact_id,
        "path": path or f"artifacts/{artifact_id}.bin",
        "size_bytes": len(data),
        "sha256": sha(data),
        "media_type": media_type,
        "encoding": encoding,
    }


def record_artifact(artifact_id: str, path: str, data: bytes):
    return content_artifact(
        artifact_id, data, path=path, media_type="application/json",
        encoding="utf-8")


def walk_artifacts(value, found):
    if isinstance(value, dict):
        if set(value) == CONTENT_KEYS:
            artifact_id = value["artifact_id"]
            if artifact_id in found:
                assert found[artifact_id] == value
            found[artifact_id] = value
            return
        for child in value.values():
            walk_artifacts(child, found)
    elif isinstance(value, list):
        for child in value:
            walk_artifacts(child, found)


def replace_atom_id(value, prior: str, replacement: str):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("atom_id", "selected_alias_atom_id") and child == prior:
                value[key] = replacement
            else:
                replace_atom_id(child, prior, replacement)
    elif isinstance(value, list):
        for child in value:
            replace_atom_id(child, prior, replacement)


def update_artifact_metadata(value, content_by_id):
    if isinstance(value, dict):
        if set(value) == CONTENT_KEYS:
            data = content_by_id[value["artifact_id"]]
            value["size_bytes"] = len(data)
            value["sha256"] = sha(data)
            return
        for child in value.values():
            update_artifact_metadata(child, content_by_id)
    elif isinstance(value, list):
        for child in value:
            update_artifact_metadata(child, content_by_id)


def source_payload(text: bytes) -> bytes:
    decoded = text.decode("utf-8")
    value = {"message": {"content": [{
        "text": decoded, "other": "X" * len(decoded),
    }]}}
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def source_snapshot_bytes(text: bytes, *, line_ending: bytes = b"\n") -> bytes:
    return source_payload(text) + line_ending


def source_atom(text: bytes, *, retained: bool = True,
                line_ending: bytes = b"\n"):
    record_bytes = source_payload(text)
    snapshot_bytes = record_bytes + line_ending
    return source_atom_from_snapshot(
        text, snapshot_bytes, record_bytes, ordinal=0,
        record_start=0, retained=retained)


def source_atom_from_snapshot(text: bytes, snapshot_bytes: bytes,
                              record_bytes: bytes, *, ordinal: int,
                              record_start: int, retained: bool = True,
                              path: str = "/message/content/0/text"):
    snapshot_sha = sha(snapshot_bytes)
    snapshot_id = f"ss1:{snapshot_sha[:32]}"
    locator = {
        "snapshot_id": snapshot_id,
        "adapter": "codex",
        "source_stream_id": "source-input-1",
        "session_id": "session-1",
        "record_ordinal": ordinal,
        "record_sha256": sha(record_bytes),
        "record_byte_range": {
            "start": record_start, "end": record_start + len(record_bytes),
        },
        "record_path": path,
        "offset_mappable": _is_utf8(record_bytes),
    }
    content_sha = sha(text)
    atom_id = sabel_physical._source_atom_identity(
        locator, 0, len(text), content_sha)
    atom = {
        "schema_version": 2,
        "atom_id": atom_id,
        "snapshot": {
            "snapshot_id": snapshot_id,
            "source_path_hint": "/private/history.jsonl",
            "captured_bytes": len(snapshot_bytes),
            "content_sha256": snapshot_sha,
        },
        "locator": locator,
        "decoded_utf8": {"range": {"start": 0, "end": len(text)}},
        "token_coordinate": None,
        "origin": {
            "speaker_role": "assistant", "base_origin": "root",
            "atom_type": "assistant_message", "source_native_type": "assistant",
        },
        "provenance": {
            "project": "agrep", "session_id": "session-1",
            "family_id": "family-1", "turn": 1, "timestamp_ms": 1,
            "root_or_delegated": "root", "parent_session_id": None,
            "tool_links": [], "replay_of": None,
        },
        "text": text.decode("utf-8") if retained else None,
        "retention": "retained_text" if retained else {
            "locator_only": {
                "atom_utf8_bytes": len(text), "content_sha256": content_sha,
            },
        },
    }
    return atom


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def refresh_atom_id(atom):
    text = atom["text"]
    if text is not None:
        content_sha = sha(text.encode("utf-8"))
    else:
        content_sha = atom["retention"]["locator_only"]["content_sha256"]
    decoded = atom["decoded_utf8"]["range"]
    atom["atom_id"] = sabel_physical._source_atom_identity(
        atom["locator"], decoded["start"], decoded["end"], content_sha)


def source_registry(main_atom, *, main_text=ATOM_TEXT,
                    include_locator_only=True, with_contents=False,
                    line_ending=b"\n"):
    main_bytes = (main_atom["text"].encode("utf-8")
                  if main_atom["text"] is not None else None)
    main_artifact = (content_artifact(
        "atom-text-1", main_bytes, media_type="text/plain")
        if main_bytes is not None else None)
    atoms = [{"atom": main_atom, "text_artifact": main_artifact}]
    main_raw = source_snapshot_bytes(main_text, line_ending=line_ending)
    raw_contents = {"raw-snapshot-1": main_raw}
    snapshots = [{
        "snapshot_id": main_atom["snapshot"]["snapshot_id"],
        "raw_artifact": content_artifact(
            "raw-snapshot-1", main_raw,
            path="sources/raw-snapshot-1.jsonl",
            media_type="application/x-ndjson", encoding="binary"),
        "framing_profile": sabel_physical._FRAMING_PROFILE,
        "decoding_profile": sabel_physical._DECODING_PROFILE,
        "path_profile": sabel_physical._PATH_PROFILE,
    }]
    if include_locator_only:
        hidden_text = b"hidden-tools"
        hidden = source_atom(hidden_text, retained=False)
        atoms.append({"atom": hidden, "text_artifact": None})
        hidden_raw = source_snapshot_bytes(hidden_text)
        raw_contents["raw-snapshot-2"] = hidden_raw
        snapshots.append({
            "snapshot_id": hidden["snapshot"]["snapshot_id"],
            "raw_artifact": content_artifact(
                "raw-snapshot-2", hidden_raw,
                path="sources/raw-snapshot-2.jsonl",
                media_type="application/x-ndjson", encoding="binary"),
            "framing_profile": sabel_physical._FRAMING_PROFILE,
            "decoding_profile": sabel_physical._DECODING_PROFILE,
            "path_profile": sabel_physical._PATH_PROFILE,
        })
    exact_sha = (sha(main_bytes) if main_bytes is not None
                 else main_atom["retention"]["locator_only"]["content_sha256"])
    identity_id = f"ae2:{exact_sha}"
    identity = {
        "schema_version": 2,
        "identity_id": identity_id,
        "exact_content_sha256": exact_sha,
        "canonical_atom_id": main_atom["atom_id"],
        "aliases": [{
            "atom_id": main_atom["atom_id"], "kind": "exact",
            "locator": copy.deepcopy(main_atom["locator"]),
            "decoded_utf8": copy.deepcopy(main_atom["decoded_utf8"]),
        }],
        "normalized_audit": None,
    }
    registry = {
        "schema": sabel_physical.SOURCE_REGISTRY_SCHEMA,
        "version": sabel_physical.SOURCE_REGISTRY_VERSION,
        "registry_id": "source-registry-1",
        "trial_id": "trial-1",
        "source_generation_id": "source-1",
        "snapshots": snapshots,
        "atoms": atoms,
        "evidence": [{"evidence_id": "evidence-1", "identity": identity}],
    }
    if with_contents:
        return registry, raw_contents
    return registry


def initial_contents(bodies, atom_text=ATOM_TEXT):
    descriptors = {}
    walk_artifacts(bodies, descriptors)
    contents = {}
    for artifact_id, descriptor in descriptors.items():
        size = descriptor["size_bytes"]
        seed = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest().encode("ascii")
        contents[artifact_id] = (seed * ((size // len(seed)) + 1))[:size]
    contents.update({
        "query": b"recall",
        "user_request": b"recall" + contents["user_request"][6:],
        "around-query": HANDLE,
        "retrieval-output": HANDLE + b"---" + ATOM_TEXT[:30],
        "around-output": b"." * 30 + ATOM_TEXT[20:30],
    })
    traces = {
        trace["trace_id"]: trace for trace in bodies["candidate_traces"]
    }
    for retrieval in bodies["retrievals"]:
        trace_id = retrieval["candidate_trace_id"]
        if trace_id is None:
            continue
        trace = traces[trace_id]
        for lane in retrieval["lanes"]:
            if lane["state"] != "captured":
                continue
            descriptor = lane["pool"]["artifact"]
            descriptor["media_type"] = "application/json"
            descriptor["encoding"] = "utf-8"
            artifact_id = descriptor["artifact_id"]
            candidates = []
            lane_rows = []
            for candidate in trace["candidates"]:
                for score in candidate["lane_scores"]:
                    if score["lane"] == lane["lane"]:
                        lane_rows.append((score["rank"], candidate, score))
            for rank, candidate, score in sorted(lane_rows):
                raw_hit = {
                    "candidate_id": candidate["candidate_id"],
                    "lane": lane["lane"], "rank": rank,
                    "score": score["score"],
                    "score_kind": score["score_kind"],
                }
                semantic = lane["kind"] == "semantic"
                candidates.append({
                    "candidate_id": candidate["candidate_id"],
                    "raw_rank": rank, "duplicate_of_raw_rank": None,
                    "lane": lane["lane"],
                    "lane_score": {
                        "value": score["score"],
                        "score": None if semantic else score["score"],
                        "sem_score": score["score"] if semantic else None,
                        "score_kind": score["score_kind"], "matched": None,
                    },
                    "session": candidate["session_id"],
                    "family": {
                        "state": "captured", "root": candidate["family_id"],
                        "source": "candidate_trace.family_id",
                    },
                    "evidence": {
                        "turn": candidate["raw_sequence"], "who": None,
                        "ts": None, "kind": None, "event_kind": None,
                        "name": None, "event_identity": None,
                        "match_span": None,
                        "content_digest": candidate["exact_content_sha256"],
                    },
                    "view": {key: None for key in (
                        "snippet", "summary", "title", "semantic_source")},
                    "exact_hashes": {
                        "raw_hit_sha256": sabel_pool.canonical_sha256(raw_hit),
                        "source_bytes_sha256": None,
                        "source_bytes_state":
                            "unavailable_at_v1_run_query_boundary",
                    },
                    "raw_hit": raw_hit,
                })
            pool = {
                "schema": sabel_pool.POOL_SCHEMA,
                "artifact_id": artifact_id,
                "retrieval_id": retrieval["retrieval_invocation_id"],
                "call_id": retrieval["action_id"],
                "lane": lane["lane"], "stage": "run_query_return",
                "ordered": True, "candidate_count": len(candidates),
                "duration_ns": 0,
                "result_meta": {
                    "observer_scope": "declared_frozen_lane_fixture",
                },
                "candidates": candidates,
            }
            sabel_pool.validate_pool_document(pool)
            contents[artifact_id] = sabel_pool.canonical_bytes(pool)
    return contents


def rewrite_pool(contents, artifact_id, mutate):
    document = json.loads(contents[artifact_id])
    mutate(document)
    contents[artifact_id] = sabel_pool.canonical_bytes(document)


def append_valid_pool_row(document):
    row = copy.deepcopy(document["candidates"][0])
    row["candidate_id"] = "candidate-extra"
    row["raw_rank"] = 2
    row["duplicate_of_raw_rank"] = None
    row["raw_hit"]["candidate_id"] = "candidate-extra"
    row["raw_hit"]["rank"] = 2
    row["exact_hashes"]["raw_hit_sha256"] = (
        sabel_pool.canonical_sha256(row["raw_hit"]))
    document["candidates"].append(row)
    document["candidate_count"] = 2


def prepare_records(*, main_text=ATOM_TEXT, retained=True,
                    mutate_bodies=None, mutate_contents=None,
                    mutate_atom=None):
    bodies = bundle_fixture.bundle_bodies()
    main_atom = source_atom(main_text, retained=retained)
    if mutate_atom is not None:
        mutate_atom(main_atom)
    real_atom_id = main_atom["atom_id"]
    replace_atom_id(bodies, "atom-1", real_atom_id)
    exact_sha = sha(main_text)
    for trace in bodies["candidate_traces"]:
        for candidate in trace["candidates"]:
            candidate["exact_identity_id"] = f"ae2:{exact_sha}"
            candidate["exact_content_sha256"] = exact_sha
    if mutate_bodies is not None:
        mutate_bodies(bodies, main_atom)

    contents = initial_contents(bodies, main_text)
    if mutate_contents is not None:
        mutate_contents(contents)
    update_artifact_metadata(bodies, contents)
    sealed_bundle = bundle_fixture.seal_bundle(bodies)

    registry_body, raw_contents = source_registry(
        main_atom, main_text=main_text, with_contents=True)
    sealed_registry = sabel_physical.seal_source_registry(registry_body)
    contents.update(raw_contents)
    if main_atom["text"] is not None:
        contents["atom-text-1"] = main_text
    return sealed_bundle, sealed_registry, contents, main_atom


def write_generation(root: Path, bundle, registry, contents):
    bundle_bytes = sabel_shadow.canonical_bytes(bundle)
    registry_bytes = sabel_shadow.canonical_bytes(registry)
    referenced = {}
    walk_artifacts(bundle, referenced)
    walk_artifacts(registry, referenced)
    for artifact_id, descriptor in referenced.items():
        data = contents[artifact_id]
        path = root / descriptor["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    bundle_descriptor = record_artifact(
        "trial-bundle-record", "records/trial-bundle.json", bundle_bytes)
    registry_descriptor = record_artifact(
        "source-registry-record", "records/source-registry.json", registry_bytes)
    for descriptor, data in (
            (bundle_descriptor, bundle_bytes),
            (registry_descriptor, registry_bytes)):
        path = root / descriptor["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = sabel_physical.seal_physical_manifest({
        "schema": sabel_physical.PHYSICAL_MANIFEST_SCHEMA,
        "version": sabel_physical.PHYSICAL_SCHEMA_VERSION,
        "trial_id": "trial-1", "source_generation_id": "source-1",
        "bundle": bundle_descriptor,
        "source_registry": registry_descriptor,
        "artifacts": [referenced[key] for key in sorted(referenced)],
    })
    (root / "manifest.json").write_bytes(sabel_shadow.canonical_bytes(manifest))
    return manifest


def verify_registry_sources(registry_body, contents):
    sealed = sabel_physical.seal_source_registry(registry_body)
    parsed = sabel_physical._validate_source_registry(sealed, require_hash=True)
    artifacts = {}
    walk_artifacts(sealed, artifacts)
    physical = {artifact_id: contents[artifact_id]
                for artifact_id in artifacts}
    return parsed, physical, sabel_physical._verify_source_snapshots(
        parsed, physical)


class PhysicalGenerationTests(unittest.TestCase):
    def build(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        bundle, registry, contents, atom = prepare_records(**kwargs)
        write_generation(root, bundle, registry, contents)
        return root, bundle, registry, contents, atom

    def test_valid_generation_reopens_and_proves_every_exact_byte_claim(self):
        root, _, registry, _, _ = self.build()
        report = sabel_physical.read_and_verify_generation(root)
        self.assertEqual(report["source_mapping_count"], 2)
        self.assertEqual(report["displayed_handle_count"], 1)
        self.assertEqual(report["exact_query_origin_count"], 2)
        self.assertEqual(report["frozen_lane_pool_count"], 3)
        states = report["atom_states"]
        self.assertEqual(states[registry["atoms"][0]["atom"]["atom_id"]],
                         "verified_retained")
        self.assertIn("verified_locator_only", states.values())

    def test_frozen_pool_rejects_wrong_missing_and_extra_rows(self):
        def wrong(document):
            document["candidates"][0]["candidate_id"] = "candidate-wrong"

        def missing(document):
            document["candidates"].clear()
            document["candidate_count"] = 0

        for name, mutation in (
                ("wrong", wrong), ("missing", missing),
                ("extra", append_valid_pool_row)):
            with self.subTest(name=name):
                def mutate(contents, mutation=mutation):
                    rewrite_pool(contents, "q8-pool", mutation)

                root, _, _, _, _ = self.build(mutate_contents=mutate)
                with self.assertRaisesRegex(
                        sabel_physical.PhysicalProofError,
                        "candidate-trace lane membership|diagnostics differ"):
                    sabel_physical.read_and_verify_generation(root)

    def test_frozen_pool_rejects_rank_score_and_score_kind_drift(self):
        def rank(document):
            document["candidates"][0]["raw_rank"] = 2

        def score(document):
            document["candidates"][0]["lane_score"]["value"] = 0.81

        def score_kind(document):
            document["candidates"][0]["lane_score"][
                "score_kind"] = "cosine-wrong"

        for name, mutation in (
                ("rank", rank), ("score", score),
                ("score_kind", score_kind)):
            with self.subTest(name=name):
                def mutate(contents, mutation=mutation):
                    rewrite_pool(contents, "q8-pool", mutation)

                root, _, _, _, _ = self.build(mutate_contents=mutate)
                with self.assertRaisesRegex(
                        sabel_physical.PhysicalProofError,
                        "one-based|candidate-trace lane membership|score_kind"):
                    sabel_physical.read_and_verify_generation(root)

    def test_frozen_pool_rejects_wrong_self_binding(self):
        for field, value in (
                ("artifact_id", "wrong-pool"),
                ("retrieval_id", "wrong-retrieval"),
                ("lane", "wrong-lane")):
            with self.subTest(field=field):
                def mutate(contents, field=field, value=value):
                    rewrite_pool(
                        contents, "q8-pool",
                        lambda document: document.__setitem__(field, value))

                root, _, _, _, _ = self.build(mutate_contents=mutate)
                with self.assertRaisesRegex(
                        sabel_physical.PhysicalProofError,
                        "does not match sealed binding"):
                    sabel_physical.read_and_verify_generation(root)

    def test_frozen_pool_rejects_malformed_duplicate_and_nonfinite_json(self):
        def malformed(contents):
            contents["q8-pool"] = b"{"

        def duplicate(contents):
            payload = contents["q8-pool"]
            contents["q8-pool"] = payload.replace(
                b'{"artifact_id":',
                b'{"artifact_id":"duplicate","artifact_id":', 1)

        def nonfinite(contents):
            contents["q8-pool"] = contents["q8-pool"].replace(
                b'"duration_ns":0', b'"duration_ns":NaN', 1)

        for name, mutation in (
                ("malformed", malformed), ("duplicate", duplicate),
                ("nonfinite", nonfinite)):
            with self.subTest(name=name):
                root, _, _, _, _ = self.build(mutate_contents=mutation)
                with self.assertRaisesRegex(
                        sabel_physical.PhysicalProofError,
                        "invalid JSON|duplicate JSON key|non-finite JSON"):
                    sabel_physical.read_and_verify_generation(root)

    def test_phantom_atom_is_rejected_after_all_record_seals_pass(self):
        phantom = "as2:" + "d" * 64

        def mutate(bodies, main_atom):
            replace_atom_id(bodies, main_atom["atom_id"], phantom)

        root, _, _, _, _ = self.build(mutate_bodies=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "phantom atom"):
            sabel_physical.read_and_verify_generation(root)

    def test_evidence_identity_digest_must_match_every_alias_atom(self):
        false_sha = sha(b"different exact evidence")
        registry = source_registry(source_atom(ATOM_TEXT))
        identity = registry["evidence"][0]["identity"]
        identity["identity_id"] = f"ae2:{false_sha}"
        identity["exact_content_sha256"] = false_sha
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "alias content does not match"):
            sabel_physical.seal_source_registry(registry)

    def test_invented_snapshot_identity_and_digest_fail_full_reader(self):
        def mutate_identity(atom):
            atom["snapshot"]["snapshot_id"] = "invented-snapshot"
            atom["locator"]["snapshot_id"] = "invented-snapshot"

        root, _, _, _, _ = self.build(mutate_atom=mutate_identity)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "physical raw snapshot digest"):
            sabel_physical.read_and_verify_generation(root)

        def mutate_digest(atom):
            atom["snapshot"]["content_sha256"] = "0" * 64

        root, _, _, _, _ = self.build(mutate_atom=mutate_digest)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "metadata differs from the physical raw snapshot"):
            sabel_physical.read_and_verify_generation(root)

    def test_invented_record_digest_fails_full_reader(self):
        def mutate_digest(atom):
            atom["locator"]["record_sha256"] = "0" * 64
            refresh_atom_id(atom)

        root, _, _, _, _ = self.build(mutate_atom=mutate_digest)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "record ordinal, range, or digest"):
            sabel_physical.read_and_verify_generation(root)

        def mutate_range(atom):
            atom["locator"]["record_byte_range"]["start"] = 1

        root, _, _, _, _ = self.build(mutate_atom=mutate_range)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "record ordinal, range, or digest"):
            sabel_physical.read_and_verify_generation(root)

        def mutate_ordinal(atom):
            atom["locator"]["record_ordinal"] = 1
            refresh_atom_id(atom)

        root, _, _, _, _ = self.build(mutate_atom=mutate_ordinal)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "does not resolve in the physical raw snapshot"):
            sabel_physical.read_and_verify_generation(root)

    def test_wrong_json_pointer_is_rejected(self):
        def mutate(atom):
            atom["locator"]["record_path"] = "/message/content/0/missing"
            refresh_atom_id(atom)

        root, _, _, _, _ = self.build(mutate_atom=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "does not resolve object key"):
            sabel_physical.read_and_verify_generation(root)

    def test_same_length_wrong_decoded_field_is_rejected(self):
        def mutate(atom):
            atom["locator"]["record_path"] = "/message/content/0/other"
            refresh_atom_id(atom)

        root, _, _, _, _ = self.build(mutate_atom=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError,
                "resolved field bytes differ"):
            sabel_physical.read_and_verify_generation(root)

    def test_unicode_escapes_and_crlf_round_trip_from_one_raw_snapshot(self):
        text = "café 😀".encode("utf-8")
        atom = source_atom(text, line_ending=b"\r\n")
        registry, raw = source_registry(
            atom, main_text=text, include_locator_only=False,
            with_contents=True, line_ending=b"\r\n")
        raw["atom-text-1"] = text
        _, _, states = verify_registry_sources(registry, raw)
        self.assertEqual(states[atom["atom_id"]], "verified_retained")

    def test_invalid_utf8_record_uses_rust_lossy_decoding_profile(self):
        payload = b'{"message":{"content":[{"text":"a\xffb","other":"XXX"}]}}'
        raw_snapshot = payload + b"\n"
        text = "a�b".encode("utf-8")
        atom = source_atom_from_snapshot(
            text, raw_snapshot, payload, ordinal=0, record_start=0)
        registry = source_registry(
            atom, main_text=text, include_locator_only=False)
        registry["snapshots"][0]["raw_artifact"] = content_artifact(
            "raw-snapshot-1", raw_snapshot,
            path="sources/raw-snapshot-1.jsonl",
            media_type="application/x-ndjson", encoding="binary")
        contents = {"raw-snapshot-1": raw_snapshot, "atom-text-1": text}
        _, _, states = verify_registry_sources(registry, contents)
        self.assertFalse(atom["locator"]["offset_mappable"])
        self.assertEqual(states[atom["atom_id"]], "verified_retained")

    def test_two_atoms_share_one_physically_verified_snapshot(self):
        first_text = b"first"
        second_text = b"second"
        first_payload = source_payload(first_text)
        second_payload = source_payload(second_text)
        snapshot = first_payload + b"\r\n" + second_payload + b"\n"
        first = source_atom_from_snapshot(
            first_text, snapshot, first_payload, ordinal=0, record_start=0)
        second_start = len(first_payload) + 2
        second = source_atom_from_snapshot(
            second_text, snapshot, second_payload,
            ordinal=1, record_start=second_start)
        registry = source_registry(
            first, main_text=first_text, include_locator_only=False)
        registry["snapshots"] = [{
            "snapshot_id": first["snapshot"]["snapshot_id"],
            "raw_artifact": content_artifact(
                "raw-shared", snapshot, path="sources/shared.jsonl",
                media_type="application/x-ndjson", encoding="binary"),
            "framing_profile": sabel_physical._FRAMING_PROFILE,
            "decoding_profile": sabel_physical._DECODING_PROFILE,
            "path_profile": sabel_physical._PATH_PROFILE,
        }]
        registry["atoms"].append({
            "atom": second,
            "text_artifact": content_artifact(
                "atom-text-2", second_text, media_type="text/plain"),
        })
        contents = {
            "raw-shared": snapshot,
            "atom-text-1": first_text,
            "atom-text-2": second_text,
        }
        _, _, states = verify_registry_sources(registry, contents)
        self.assertEqual(set(states), {first["atom_id"], second["atom_id"]})

    def test_rust_integer_widths_are_enforced(self):
        atom = source_atom(ATOM_TEXT)
        atom["locator"]["record_ordinal"] = 1 << 64
        refresh_atom_id(atom)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "must be a u64"):
            sabel_physical.seal_source_registry(source_registry(atom))

        for field, value, expected in (
                ("turn", 1 << 32, "u32"),
                ("timestamp_ms", 1 << 63, "i64")):
            atom = source_atom(ATOM_TEXT)
            atom["provenance"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                    sabel_physical.PhysicalProofError, expected):
                sabel_physical.seal_source_registry(source_registry(atom))

    def test_source_atom_identity_matches_rust_serde_golden(self):
        fixture_path = (Path(__file__).resolve().parents[1]
                        / "crates/agrep-core/src/fixtures"
                        / "source_atom_v2_golden.json")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        parsed = sabel_physical._validate_source_atom(
            fixture["atom"], "golden.atom")
        self.assertEqual(
            parsed["atom_id"],
            "as2:a543836ee28f119b8ada655307f770b3601c71c1ce0e276c9ae979515e8d2138")
        self.assertEqual(parsed["atom"]["locator"]["record_ordinal"],
                         (1 << 64) - 1)

    def test_preview_only_unresolved_candidate_needs_no_evidence_bytes(self):
        atom = source_atom(ATOM_TEXT)
        registry, raw = source_registry(
            atom, include_locator_only=False, with_contents=True)
        raw["atom-text-1"] = ATOM_TEXT
        sealed = sabel_physical.seal_source_registry(registry)
        parsed = sabel_physical._validate_source_registry(
            sealed, require_hash=True)
        candidate = {
            "candidate_id": "unresolved-1", "evidence_id": None,
            "exact_identity_id": None, "source_bindings": [],
            "unresolved_reason": "snapshot_unavailable",
            "selection": {"opened": False},
            "slot_support": [{
                "state": "unavailable_or_unjudged",
                "supporting_source_ranges": [],
            }],
        }
        bundle = {
            "candidate_traces": [{
                "candidates": [candidate],
                "result": {"candidate_ids": []},
            }],
            "retrievals": [], "renderers": [],
        }
        report = sabel_physical._validate_bundle_registry_join(
            bundle, parsed, raw)
        self.assertEqual(report["source_mapping_count"], 0)

        candidate["exact_identity_id"] = "ae2:" + "0" * 64
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "requires sealed evidence"):
            sabel_physical._validate_bundle_registry_join(bundle, parsed, raw)

    def test_safe_open_primitives_fail_closed_and_total_bytes_are_bounded(self):
        root, _, _, _, _ = self.build()
        with mock.patch.object(sabel_physical.os, "O_NOFOLLOW", 0):
            with self.assertRaisesRegex(
                    sabel_physical.PhysicalProofError,
                    "safe no-follow dir-fd traversal"):
                sabel_physical.read_and_verify_generation(root)
        with mock.patch.object(sabel_physical, "_MAX_TOTAL_ARTIFACT_BYTES", 1):
            with self.assertRaisesRegex(
                    sabel_physical.PhysicalProofError,
                    "cumulative-byte limit"):
                sabel_physical.read_and_verify_generation(root)

    def test_out_of_range_source_span_is_rejected(self):
        root, _, _, _, _ = self.build(main_text=ATOM_TEXT[:25])
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "outside its registered"):
            sabel_physical.read_and_verify_generation(root)

    def test_tampered_same_size_artifact_fails_physical_hash(self):
        root, _, _, _, _ = self.build()
        path = root / "artifacts/query.bin"
        body = path.read_bytes()
        path.write_bytes(b"X" + body[1:])
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "SHA-256 mismatch"):
            sabel_physical.read_and_verify_generation(root)

    def test_same_length_false_source_mapping_is_rejected(self):
        def mutate(contents):
            output = bytearray(contents["retrieval-output"])
            output[49] = ord("X") if output[49] != ord("X") else ord("Y")
            contents["retrieval-output"] = bytes(output)

        root, _, _, _, _ = self.build(mutate_contents=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "not the registered source quote"):
            sabel_physical.read_and_verify_generation(root)

    def test_false_handle_slice_is_rejected_even_with_matching_length(self):
        def mutate(bodies, main_atom):
            segment = bodies["renderers"][1]["segments"][0]
            claimed = "XXXXXXXXXX"
            segment.update({
                "displayed_handle": claimed,
                "handle_output_start": 0,
                "handle_output_end": len(claimed),
                "handle_sha256": sha(claimed.encode("utf-8")),
            })

        root, _, _, _, _ = self.build(mutate_bodies=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "displayed_handle bytes"):
            sabel_physical.read_and_verify_generation(root)

    def test_locator_only_atom_cannot_back_an_exact_renderer_mapping(self):
        root, _, _, _, _ = self.build(retained=False)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "explicitly unverifiable"):
            sabel_physical.read_and_verify_generation(root)

    def test_false_exact_copy_query_origin_is_rejected(self):
        def mutate(contents):
            contents["user_request"] = b"WRONG!" + contents["user_request"][6:]

        root, _, _, _, _ = self.build(mutate_contents=mutate)
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "exact-copy query bytes"):
            sabel_physical.read_and_verify_generation(root)

    def test_manifest_rejects_unsafe_relative_path_before_open(self):
        root, bundle, registry, contents, _ = self.build()
        bundle_bytes = sabel_shadow.canonical_bytes(bundle)
        registry_bytes = sabel_shadow.canonical_bytes(registry)
        referenced = {}
        walk_artifacts(bundle, referenced)
        walk_artifacts(registry, referenced)
        unsafe = {
            "schema": sabel_physical.PHYSICAL_MANIFEST_SCHEMA,
            "version": sabel_physical.PHYSICAL_SCHEMA_VERSION,
            "trial_id": "trial-1", "source_generation_id": "source-1",
            "bundle": record_artifact(
                "trial-bundle-record", "../trial-bundle.json", bundle_bytes),
            "source_registry": record_artifact(
                "source-registry-record", "records/source-registry.json",
                registry_bytes),
            "artifacts": [referenced[key] for key in sorted(referenced)],
        }
        with self.assertRaisesRegex(
                sabel_physical.PhysicalProofError, "normalized relative"):
            sabel_physical.seal_physical_manifest(unsafe)


if __name__ == "__main__":
    unittest.main()
