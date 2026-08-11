"""Physical reader for a manifest-last opt-in SABEL shadow generation.

The schemas in :mod:`sabel_shadow` prove internal record continuity.  This
module supplies the intentionally separate Gate-0 physical seam: it opens a
sealed bundle and source registry from a generation root, re-opens every byte
artifact, derives atoms again from frozen raw snapshots, and checks renderer
claims against exact ``SourceAtomV2`` bytes.  Nothing here is imported by the
v1 search path.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import PurePosixPath
from typing import Mapping

import sabel_pool
import sabel_shadow


PHYSICAL_MANIFEST_SCHEMA = "agrep.sabel-physical-manifest"
SOURCE_REGISTRY_SCHEMA = "agrep.sabel-source-registry"
PHYSICAL_SCHEMA_VERSION = 1
SOURCE_REGISTRY_VERSION = 2
SOURCE_ATOM_SCHEMA_VERSION = 2
EVIDENCE_IDENTITY_SCHEMA_VERSION = 2

_CONTENT_KEYS = {
    "artifact_id", "path", "size_bytes", "sha256", "media_type", "encoding",
}
_HEX = frozenset("0123456789abcdef")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_ARTIFACT_COUNT = 4096
_MAX_TOTAL_ARTIFACT_BYTES = 512 * 1024 * 1024
_U16_MAX = (1 << 16) - 1
_U32_MAX = (1 << 32) - 1
_U64_MAX = (1 << 64) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1

_FRAMING_PROFILE = "jsonl-lf-crlf-v1"
_DECODING_PROFILE = "utf8-lossy-record-v1"
_PATH_PROFILE = "rfc6901-json-pointer-v1"


class PhysicalProofError(ValueError):
    """A physical generation is unsafe, incomplete, or byte-inconsistent."""


def _fail(where: str, message: str) -> None:
    raise PhysicalProofError(f"{where}: {message}")


def _object(value: object, keys: set[str], where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(where, "must be an object")
    actual = set(value)
    if actual != keys:
        _fail(where, f"keys mismatch (missing={sorted(keys - actual)}, "
                     f"extra={sorted(actual - keys)})")
    return value


def _string(value: object, where: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(where, "must be a non-empty NUL-free string")
    return value


def _bounded_integer(value: object, where: str, minimum: int,
                     maximum: int, type_name: str) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail(where, f"must be a {type_name} integer")
    return value


def _u16(value: object, where: str) -> int:
    return _bounded_integer(value, where, 0, _U16_MAX, "u16")


def _u32(value: object, where: str) -> int:
    return _bounded_integer(value, where, 0, _U32_MAX, "u32")


def _u64(value: object, where: str) -> int:
    return _bounded_integer(value, where, 0, _U64_MAX, "u64")


def _i64(value: object, where: str) -> int:
    return _bounded_integer(value, where, _I64_MIN, _I64_MAX, "i64")


def _boolean(value: object, where: str) -> bool:
    if type(value) is not bool:
        _fail(where, "must be a boolean")
    return value


def _digest(value: object, where: str) -> str:
    text = _string(value, where)
    assert text is not None
    if len(text) != 64 or any(character not in _HEX for character in text):
        _fail(where, "must be a lowercase SHA-256 hex digest")
    return text


def _normalized_path(value: object, where: str, *,
                     allow_manifest: bool = False) -> str:
    text = _string(value, where)
    assert text is not None
    parsed = PurePosixPath(text)
    if (parsed.is_absolute() or not parsed.name or ".." in parsed.parts
            or "." in parsed.parts or str(parsed) != text or "\\" in text):
        _fail(where, "must be a normalized relative POSIX path")
    if parsed.name == "manifest.json" and not allow_manifest:
        _fail(where, "manifest.json is the commit marker, not an artifact")
    return text


def _artifact(value: object, where: str) -> dict:
    record = _object(value, _CONTENT_KEYS, where)
    artifact_id = _string(record["artifact_id"], f"{where}.artifact_id")
    path = _normalized_path(record["path"], f"{where}.path")
    size = _u64(record["size_bytes"], f"{where}.size_bytes")
    digest = _digest(record["sha256"], f"{where}.sha256")
    media_type = _string(record["media_type"], f"{where}.media_type")
    encoding = record["encoding"]
    if encoding not in (None, "utf-8", "binary"):
        _fail(f"{where}.encoding", "must be utf-8, binary, or null")
    return {
        "artifact_id": artifact_id,
        "path": path,
        "size_bytes": size,
        "sha256": digest,
        "media_type": media_type,
        "encoding": encoding,
    }


def _byte_range(value: object, where: str) -> tuple[int, int]:
    record = _object(value, {"start", "end"}, where)
    start = _u64(record["start"], f"{where}.start")
    end = _u64(record["end"], f"{where}.end")
    if end < start:
        _fail(where, "end must not precede start")
    return start, end


def _source_range(value: object, where: str) -> tuple[str, int, int]:
    record = _object(value, {
        "source_span_id", "atom_id", "decoded_start", "decoded_end",
    }, where)
    _string(record["source_span_id"], f"{where}.source_span_id")
    atom_id = _string(record["atom_id"], f"{where}.atom_id")
    start = _u64(record["decoded_start"], f"{where}.decoded_start")
    end = _u64(record["decoded_end"], f"{where}.decoded_end")
    assert atom_id is not None
    if end <= start:
        _fail(where, "source ranges must be non-empty and ordered")
    return atom_id, start, end


def _validate_locator(value: object, where: str) -> Mapping[str, object]:
    record = _object(value, {
        "snapshot_id", "adapter", "source_stream_id", "session_id",
        "record_ordinal", "record_sha256", "record_byte_range", "record_path",
        "offset_mappable",
    }, where)
    for key in ("snapshot_id", "adapter", "source_stream_id", "session_id",
                "record_path"):
        _string(record[key], f"{where}.{key}")
    _u64(record["record_ordinal"], f"{where}.record_ordinal")
    _digest(record["record_sha256"], f"{where}.record_sha256")
    _byte_range(record["record_byte_range"], f"{where}.record_byte_range")
    _boolean(record["offset_mappable"], f"{where}.offset_mappable")
    return record


def _source_atom_identity(locator: Mapping[str, object], start: int, end: int,
                          content_sha256: str) -> str:
    # This is the exact serde_json tuple used by source_atom.rs::atom_id.
    payload = json.dumps([
        SOURCE_ATOM_SCHEMA_VERSION,
        locator["adapter"], locator["source_stream_id"], locator["session_id"],
        locator["record_ordinal"], locator["record_sha256"],
        locator["record_path"], start, end, content_sha256,
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "as2:" + hashlib.sha256(payload).hexdigest()


def _validate_source_atom(value: object, where: str) -> dict:
    atom = _object(value, {
        "schema_version", "atom_id", "snapshot", "locator", "decoded_utf8",
        "token_coordinate", "origin", "provenance", "text", "retention",
    }, where)
    if atom["schema_version"] != SOURCE_ATOM_SCHEMA_VERSION:
        _fail(f"{where}.schema_version", "must identify SourceAtomV2")
    atom_id = _string(atom["atom_id"], f"{where}.atom_id")
    assert atom_id is not None

    snapshot = _object(atom["snapshot"], {
        "snapshot_id", "source_path_hint", "captured_bytes", "content_sha256",
    }, f"{where}.snapshot")
    snapshot_id = _string(snapshot["snapshot_id"], f"{where}.snapshot.snapshot_id")
    _string(snapshot["source_path_hint"], f"{where}.snapshot.source_path_hint")
    captured_bytes = _u64(
        snapshot["captured_bytes"], f"{where}.snapshot.captured_bytes")
    _digest(snapshot["content_sha256"], f"{where}.snapshot.content_sha256")

    locator = _validate_locator(atom["locator"], f"{where}.locator")
    if locator["snapshot_id"] != snapshot_id:
        _fail(where, "locator is not bound to the atom snapshot")
    raw_start, raw_end = _byte_range(
        locator["record_byte_range"], f"{where}.locator.record_byte_range")
    if raw_end > captured_bytes:
        _fail(where, "record locator lies outside the immutable snapshot")

    decoded = _object(atom["decoded_utf8"], {"range"}, f"{where}.decoded_utf8")
    start, end = _byte_range(decoded["range"], f"{where}.decoded_utf8.range")

    token = atom["token_coordinate"]
    if token is not None:
        token = _object(token, {"tokenizer_profile", "start", "end"},
                        f"{where}.token_coordinate")
        _string(token["tokenizer_profile"],
                f"{where}.token_coordinate.tokenizer_profile")
        token_start = _u32(token["start"], f"{where}.token_coordinate.start")
        token_end = _u32(token["end"], f"{where}.token_coordinate.end")
        if token_end < token_start:
            _fail(f"{where}.token_coordinate", "end must not precede start")

    origin = _object(atom["origin"], {
        "speaker_role", "base_origin", "atom_type", "source_native_type",
    }, f"{where}.origin")
    for key in origin:
        _string(origin[key], f"{where}.origin.{key}")

    provenance = _object(atom["provenance"], {
        "project", "session_id", "family_id", "turn", "timestamp_ms",
        "root_or_delegated", "parent_session_id", "tool_links", "replay_of",
    }, f"{where}.provenance")
    for key in ("project", "session_id", "family_id", "root_or_delegated"):
        _string(provenance[key], f"{where}.provenance.{key}")
    _u32(provenance["turn"], f"{where}.provenance.turn")
    _i64(provenance["timestamp_ms"], f"{where}.provenance.timestamp_ms")
    _string(provenance["parent_session_id"],
            f"{where}.provenance.parent_session_id", nullable=True)
    _string(provenance["replay_of"], f"{where}.provenance.replay_of", nullable=True)
    if not isinstance(provenance["tool_links"], list):
        _fail(f"{where}.provenance.tool_links", "must be an array")
    for index, link in enumerate(provenance["tool_links"]):
        _string(link, f"{where}.provenance.tool_links[{index}]")
    if provenance["session_id"] != locator["session_id"]:
        _fail(where, "provenance session does not match locator session")

    retention = atom["retention"]
    text = atom["text"]
    if retention == "retained_text":
        retained = _string(text, f"{where}.text")
        assert retained is not None
        retained_bytes = retained.encode("utf-8")
        if len(retained_bytes) != end - start:
            _fail(where, "retained UTF-8 size does not match decoded range")
        content_sha = hashlib.sha256(retained_bytes).hexdigest()
        state = "verified_retained"
    else:
        wrapper = _object(retention, {"locator_only"}, f"{where}.retention")
        locator_only = _object(wrapper["locator_only"], {
            "atom_utf8_bytes", "content_sha256",
        }, f"{where}.retention.locator_only")
        if text is not None:
            _fail(f"{where}.text", "locator-only atoms cannot retain text")
        atom_bytes = _u64(
            locator_only["atom_utf8_bytes"],
            f"{where}.retention.locator_only.atom_utf8_bytes")
        if atom_bytes != end - start:
            _fail(where, "locator-only size does not match decoded range")
        content_sha = _digest(
            locator_only["content_sha256"],
            f"{where}.retention.locator_only.content_sha256")
        retained_bytes = None
        state = "unverifiable_locator_only"

    if atom_id != _source_atom_identity(locator, start, end, content_sha):
        _fail(f"{where}.atom_id", "does not match SourceAtomV2 identity")
    return {
        "atom": atom,
        "atom_id": atom_id,
        "start": start,
        "end": end,
        "content_sha256": content_sha,
        "text_bytes": retained_bytes,
        "verification_state": state,
    }


def _validate_alias(value: object, where: str) -> dict:
    alias = _object(value, {"atom_id", "kind", "locator", "decoded_utf8"}, where)
    atom_id = _string(alias["atom_id"], f"{where}.atom_id")
    if alias["kind"] not in ("exact", "replay"):
        _fail(f"{where}.kind", "must be exact or replay")
    locator = _validate_locator(alias["locator"], f"{where}.locator")
    decoded = _object(alias["decoded_utf8"], {"range"}, f"{where}.decoded_utf8")
    start, end = _byte_range(decoded["range"], f"{where}.decoded_utf8.range")
    return {"atom_id": atom_id, "locator": locator, "start": start, "end": end}


def _validate_evidence(value: object, where: str) -> dict:
    binding = _object(value, {"evidence_id", "identity"}, where)
    evidence_id = _string(binding["evidence_id"], f"{where}.evidence_id")
    identity = _object(binding["identity"], {
        "schema_version", "identity_id", "exact_content_sha256",
        "canonical_atom_id", "aliases", "normalized_audit",
    }, f"{where}.identity")
    if identity["schema_version"] != EVIDENCE_IDENTITY_SCHEMA_VERSION:
        _fail(f"{where}.identity.schema_version", "must identify EvidenceIdentityV2")
    exact_sha = _digest(
        identity["exact_content_sha256"],
        f"{where}.identity.exact_content_sha256")
    identity_id = _string(identity["identity_id"], f"{where}.identity.identity_id")
    if identity_id != f"ae2:{exact_sha}":
        _fail(f"{where}.identity.identity_id", "does not match exact content digest")
    canonical_atom_id = _string(
        identity["canonical_atom_id"], f"{where}.identity.canonical_atom_id")
    if not isinstance(identity["aliases"], list) or not identity["aliases"]:
        _fail(f"{where}.identity.aliases", "must be a non-empty array")
    aliases = [_validate_alias(alias, f"{where}.identity.aliases[{index}]")
               for index, alias in enumerate(identity["aliases"])]
    alias_ids = [alias["atom_id"] for alias in aliases]
    if len(alias_ids) != len(set(alias_ids)):
        _fail(f"{where}.identity.aliases", "atom aliases must be unique")
    if canonical_atom_id not in alias_ids:
        _fail(f"{where}.identity.canonical_atom_id", "must be one of the aliases")
    audit = identity["normalized_audit"]
    if audit is not None:
        audit = _object(audit, {
            "normalization_version", "normalized_sha256", "ranking_eligible",
        }, f"{where}.identity.normalized_audit")
        _string(audit["normalization_version"],
                f"{where}.identity.normalized_audit.normalization_version")
        _digest(audit["normalized_sha256"],
                f"{where}.identity.normalized_audit.normalized_sha256")
        if _boolean(audit["ranking_eligible"],
                    f"{where}.identity.normalized_audit.ranking_eligible"):
            _fail(f"{where}.identity.normalized_audit.ranking_eligible",
                  "normalized identities are audit-only")
    return {
        "evidence_id": evidence_id,
        "identity_id": identity_id,
        "exact_content_sha256": exact_sha,
        "canonical_atom_id": canonical_atom_id,
        "aliases": aliases,
    }


def _validate_snapshot_entry(value: object, where: str) -> dict:
    snapshot = _object(value, {
        "snapshot_id", "raw_artifact", "framing_profile",
        "decoding_profile", "path_profile",
    }, where)
    snapshot_id = _string(snapshot["snapshot_id"], f"{where}.snapshot_id")
    if snapshot["framing_profile"] != _FRAMING_PROFILE:
        _fail(f"{where}.framing_profile", "unsupported record framing profile")
    if snapshot["decoding_profile"] != _DECODING_PROFILE:
        _fail(f"{where}.decoding_profile", "unsupported record decoding profile")
    if snapshot["path_profile"] != _PATH_PROFILE:
        _fail(f"{where}.path_profile", "unsupported decoded-field path profile")
    artifact = _artifact(snapshot["raw_artifact"], f"{where}.raw_artifact")
    if artifact["encoding"] != "binary":
        _fail(f"{where}.raw_artifact.encoding",
              "raw snapshots must preserve binary source bytes")
    return {
        "snapshot_id": snapshot_id,
        "raw_artifact": artifact,
        "framing_profile": snapshot["framing_profile"],
        "decoding_profile": snapshot["decoding_profile"],
        "path_profile": snapshot["path_profile"],
    }


def _validate_source_registry(record: object, *, require_hash: bool) -> dict:
    keys = {
        "schema", "version", "registry_id", "trial_id", "source_generation_id",
        "snapshots", "atoms", "evidence",
    }
    if require_hash:
        keys.add("registry_sha256")
    value = _object(record, keys, "source_registry")
    if (value["schema"] != SOURCE_REGISTRY_SCHEMA
            or value["version"] != SOURCE_REGISTRY_VERSION):
        _fail("source_registry", "unsupported schema or version")
    for key in ("registry_id", "trial_id", "source_generation_id"):
        _string(value[key], f"source_registry.{key}")
    if not isinstance(value["snapshots"], list) or not value["snapshots"]:
        _fail("source_registry.snapshots", "must be a non-empty array")
    snapshots: dict[str, dict] = {}
    snapshot_artifact_ids = set()
    snapshot_paths = set()
    for index, snapshot_value in enumerate(value["snapshots"]):
        parsed_snapshot = _validate_snapshot_entry(
            snapshot_value, f"source_registry.snapshots[{index}]")
        snapshot_id = parsed_snapshot["snapshot_id"]
        if snapshot_id in snapshots:
            _fail("source_registry.snapshots", "snapshot ids must be unique")
        artifact = parsed_snapshot["raw_artifact"]
        if (artifact["artifact_id"] in snapshot_artifact_ids
                or artifact["path"] in snapshot_paths):
            _fail("source_registry.snapshots",
                  "raw snapshot artifacts and paths must be unique")
        snapshot_artifact_ids.add(artifact["artifact_id"])
        snapshot_paths.add(artifact["path"])
        snapshots[snapshot_id] = parsed_snapshot
    if not isinstance(value["atoms"], list) or not value["atoms"]:
        _fail("source_registry.atoms", "must be a non-empty array")
    atoms: dict[str, dict] = {}
    for index, entry_value in enumerate(value["atoms"]):
        where = f"source_registry.atoms[{index}]"
        entry = _object(entry_value, {"atom", "text_artifact"}, where)
        parsed = _validate_source_atom(entry["atom"], f"{where}.atom")
        atom_id = parsed["atom_id"]
        if atom_id in atoms:
            _fail("source_registry.atoms", "atom ids must be unique")
        if parsed["verification_state"] == "verified_retained":
            if entry["text_artifact"] is None:
                _fail(f"{where}.text_artifact",
                      "retained atoms require a physical text artifact")
            parsed["text_artifact"] = _artifact(
                entry["text_artifact"], f"{where}.text_artifact")
        else:
            if entry["text_artifact"] is not None:
                _fail(f"{where}.text_artifact",
                      "locator-only atoms cannot claim retained bytes")
            parsed["text_artifact"] = None
        if parsed["atom"]["snapshot"]["snapshot_id"] not in snapshots:
            _fail(f"{where}.atom.snapshot.snapshot_id",
                  "does not resolve to a sealed raw snapshot")
        atoms[atom_id] = parsed

    if not isinstance(value["evidence"], list):
        _fail("source_registry.evidence", "must be an array")
    evidence: dict[str, dict] = {}
    identities = set()
    for index, evidence_value in enumerate(value["evidence"]):
        parsed = _validate_evidence(
            evidence_value, f"source_registry.evidence[{index}]")
        if parsed["evidence_id"] in evidence:
            _fail("source_registry.evidence", "evidence ids must be unique")
        if parsed["identity_id"] in identities:
            _fail("source_registry.evidence", "identity ids must be unique")
        identities.add(parsed["identity_id"])
        for alias in parsed["aliases"]:
            atom = atoms.get(alias["atom_id"])
            if atom is None:
                _fail(f"source_registry.evidence[{index}].identity.aliases",
                      "alias names an atom absent from this source generation")
            if (alias["locator"] != atom["atom"]["locator"]
                    or alias["start"] != atom["start"]
                    or alias["end"] != atom["end"]):
                _fail(f"source_registry.evidence[{index}].identity.aliases",
                      "alias coordinates do not match the registered SourceAtomV2")
            if atom["content_sha256"] != parsed["exact_content_sha256"]:
                _fail(f"source_registry.evidence[{index}].identity.aliases",
                      "alias content does not match the evidence exact-content digest")
        evidence[parsed["evidence_id"]] = parsed

    if require_hash:
        supplied = _digest(value["registry_sha256"],
                           "source_registry.registry_sha256")
        body = dict(value)
        del body["registry_sha256"]
        if sabel_shadow.canonical_sha256(body) != supplied:
            _fail("source_registry.registry_sha256", "canonical hash mismatch")
    return {
        "record": value, "snapshots": snapshots,
        "atoms": atoms, "evidence": evidence,
    }


def seal_source_registry(record: Mapping[str, object]) -> dict:
    body = json.loads(json.dumps(dict(record)))
    body.pop("registry_sha256", None)
    _validate_source_registry(body, require_hash=False)
    body["registry_sha256"] = sabel_shadow.canonical_sha256(body)
    _validate_source_registry(body, require_hash=True)
    return body


def validate_source_registry(record: object) -> None:
    _validate_source_registry(record, require_hash=True)


def _validate_manifest(record: object, *, require_hash: bool) -> dict:
    keys = {
        "schema", "version", "trial_id", "source_generation_id", "bundle",
        "source_registry", "artifacts",
    }
    if require_hash:
        keys.add("manifest_sha256")
    value = _object(record, keys, "physical_manifest")
    if (value["schema"] != PHYSICAL_MANIFEST_SCHEMA
            or value["version"] != PHYSICAL_SCHEMA_VERSION):
        _fail("physical_manifest", "unsupported schema or version")
    for key in ("trial_id", "source_generation_id"):
        _string(value[key], f"physical_manifest.{key}")
    bundle = _artifact(value["bundle"], "physical_manifest.bundle")
    registry = _artifact(
        value["source_registry"], "physical_manifest.source_registry")
    if not isinstance(value["artifacts"], list):
        _fail("physical_manifest.artifacts", "must be an array")
    artifacts: dict[str, dict] = {}
    paths = {bundle["path"], registry["path"]}
    ids = {bundle["artifact_id"], registry["artifact_id"]}
    if len(paths) != 2 or len(ids) != 2:
        _fail("physical_manifest", "bundle and registry must be distinct artifacts")
    for index, artifact_value in enumerate(value["artifacts"]):
        artifact = _artifact(
            artifact_value, f"physical_manifest.artifacts[{index}]")
        if artifact["artifact_id"] in ids or artifact["artifact_id"] in artifacts:
            _fail("physical_manifest.artifacts", "artifact ids must be unique")
        if artifact["path"] in paths:
            _fail("physical_manifest.artifacts", "artifact paths must be unique")
        ids.add(artifact["artifact_id"])
        paths.add(artifact["path"])
        artifacts[artifact["artifact_id"]] = artifact
    if require_hash:
        supplied = _digest(value["manifest_sha256"],
                           "physical_manifest.manifest_sha256")
        body = dict(value)
        del body["manifest_sha256"]
        if sabel_shadow.canonical_sha256(body) != supplied:
            _fail("physical_manifest.manifest_sha256", "canonical hash mismatch")
    return {"record": value, "bundle": bundle, "registry": registry,
            "artifacts": artifacts}


def seal_physical_manifest(record: Mapping[str, object]) -> dict:
    body = json.loads(json.dumps(dict(record)))
    body.pop("manifest_sha256", None)
    _validate_manifest(body, require_hash=False)
    body["manifest_sha256"] = sabel_shadow.canonical_sha256(body)
    _validate_manifest(body, require_hash=True)
    return body


def validate_physical_manifest(record: object) -> None:
    _validate_manifest(record, require_hash=True)


def _decode_json(data: bytes, where: str) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PhysicalProofError(f"{where}: is not UTF-8 JSON") from error

    return _decode_json_text(text, where)


def _decode_json_text(text: str, where: str) -> object:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail(where, f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value: str):
        _fail(where, f"non-finite JSON number {value!r} is forbidden")

    try:
        return json.loads(
            text, object_pairs_hook=unique_pairs,
            parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise PhysicalProofError(f"{where}: invalid JSON: {error.msg}") from error


def _json_pointer(value: object, pointer: str, where: str) -> object:
    if not pointer.startswith("/"):
        _fail(where, "must be a non-empty RFC 6901 JSON pointer")
    current = value
    for raw_token in pointer.split("/")[1:]:
        token_parts = []
        index = 0
        while index < len(raw_token):
            character = raw_token[index]
            if character != "~":
                token_parts.append(character)
                index += 1
                continue
            if index + 1 >= len(raw_token) or raw_token[index + 1] not in "01":
                _fail(where, "contains an invalid RFC 6901 escape")
            token_parts.append("~" if raw_token[index + 1] == "0" else "/")
            index += 2
        token = "".join(token_parts)
        if isinstance(current, Mapping):
            if token not in current:
                _fail(where, f"does not resolve object key {token!r}")
            current = current[token]
        elif isinstance(current, list):
            if (not token.isascii() or not token.isdigit()
                    or (len(token) > 1 and token.startswith("0"))):
                _fail(where, f"contains non-canonical array index {token!r}")
            item_index = int(token)
            if item_index >= len(current):
                _fail(where, f"array index {item_index} is out of bounds")
            current = current[item_index]
        else:
            _fail(where, "continues beyond a scalar JSON value")
    return current


def _frozen_records(data: bytes):
    cursor = 0
    ordinal = 0
    while cursor < len(data):
        newline = data.find(b"\n", cursor)
        if newline < 0:
            payload_end = len(data)
            frame_end = len(data)
        else:
            payload_end = newline - 1 if newline > cursor and data[newline - 1] == 13 else newline
            frame_end = newline + 1
        payload = data[cursor:payload_end]
        yield {
            "ordinal": ordinal,
            "start": cursor,
            "end": payload_end,
            "payload": payload,
            "offset_mappable": _is_utf8(payload),
            "decoded": payload.decode("utf-8", errors="replace"),
        }
        cursor = frame_end
        ordinal += 1


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_relative(root: os.PathLike[str] | str, relative: str, *,
                   expected_size: int | None, max_bytes: int, where: str,
                   allow_manifest: bool = False) -> bytes:
    normalized = _normalized_path(
        relative, f"{where}.path", allow_manifest=allow_manifest)
    components = PurePosixPath(normalized).parts
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if (not nofollow or not directory
            or os.open not in getattr(os, "supports_dir_fd", set())):
        _fail(where,
              "safe no-follow dir-fd traversal is unavailable on this platform")
    root_fd = None
    opened_dirs = []
    file_fd = None
    try:
        root_fd = os.open(os.fspath(root), os.O_RDONLY | directory | nofollow)
        current_fd = root_fd
        for component in components[:-1]:
            next_fd = os.open(component, os.O_RDONLY | directory | nofollow,
                              dir_fd=current_fd)
            opened_dirs.append(next_fd)
            current_fd = next_fd
        file_fd = os.open(components[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(where, "must resolve to a regular file")
        if expected_size is not None and metadata.st_size != expected_size:
            _fail(where, f"size mismatch (declared={expected_size}, "
                         f"observed={metadata.st_size})")
        if metadata.st_size > max_bytes:
            _fail(where, f"exceeds bounded read limit {max_bytes}")
        chunks = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(file_fd, min(remaining, 1024 * 1024))
            if not chunk:
                _fail(where, "short read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_fd, 1):
            _fail(where, "grew during bounded read")
        return b"".join(chunks)
    except OSError as error:
        raise PhysicalProofError(f"{where}: cannot safely open {normalized!r}: "
                                 f"{error.strerror}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(opened_dirs):
            os.close(descriptor)
        if root_fd is not None:
            os.close(root_fd)


def _read_artifact(root: os.PathLike[str] | str, artifact: Mapping[str, object],
                   where: str, *, max_bytes: int = _MAX_ARTIFACT_BYTES) -> bytes:
    data = _read_relative(
        root, artifact["path"], expected_size=artifact["size_bytes"],
        max_bytes=max_bytes, where=where)
    observed = hashlib.sha256(data).hexdigest()
    if observed != artifact["sha256"]:
        _fail(where, "SHA-256 mismatch")
    if artifact["encoding"] == "utf-8":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PhysicalProofError(f"{where}: declared UTF-8 bytes are invalid") from error
    return data


def _collect_content_artifacts(value: object, where: str,
                               found: dict[str, dict]) -> None:
    if isinstance(value, Mapping):
        if set(value) == _CONTENT_KEYS:
            artifact = _artifact(value, where)
            prior = found.get(artifact["artifact_id"])
            if prior is not None and prior != artifact:
                _fail(where, "one artifact id has conflicting physical metadata")
            found[artifact["artifact_id"]] = artifact
            return
        for key, child in value.items():
            _collect_content_artifacts(child, f"{where}.{key}", found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _collect_content_artifacts(child, f"{where}[{index}]", found)


def _iter_source_ranges(value: object, where: str):
    if isinstance(value, Mapping):
        if set(value) == {
                "source_span_id", "atom_id", "decoded_start", "decoded_end"}:
            yield where, value
            return
        for key, child in value.items():
            yield from _iter_source_ranges(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_source_ranges(child, f"{where}[{index}]")


def _quote(atom: Mapping[str, object], start: int, end: int, where: str) -> bytes:
    if start < atom["start"] or end > atom["end"]:
        _fail(where, "source range lies outside its registered SourceAtomV2")
    text = atom["text_bytes"]
    if text is None:
        _fail(where,
              "locator-only atom is explicitly unverifiable and cannot support exact mappings")
    local_start = start - atom["start"]
    local_end = end - atom["start"]
    quote = text[local_start:local_end]
    try:
        quote.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PhysicalProofError(
            f"{where}: source range does not follow decoded UTF-8 boundaries") from error
    return quote


def _verify_source_snapshots(registry: dict,
                             artifact_bytes: Mapping[str, bytes]) -> dict[str, str]:
    verified_snapshots: dict[str, dict] = {}
    for snapshot_id, snapshot in registry["snapshots"].items():
        where = f"source_registry.snapshots.{snapshot_id}"
        artifact = snapshot["raw_artifact"]
        raw = artifact_bytes[artifact["artifact_id"]]
        digest = hashlib.sha256(raw).hexdigest()
        derived_snapshot_id = f"ss1:{digest[:32]}"
        if snapshot_id != derived_snapshot_id:
            _fail(f"{where}.snapshot_id",
                  "does not match the physical raw snapshot digest")
        verified_snapshots[snapshot_id] = {
            "raw": raw, "digest": digest,
            "records": list(_frozen_records(raw)),
        }

    atom_states: dict[str, str] = {}
    for atom_id, atom in registry["atoms"].items():
        atom_record = atom["atom"]
        snapshot_meta = atom_record["snapshot"]
        snapshot_id = snapshot_meta["snapshot_id"]
        verified_snapshot = verified_snapshots[snapshot_id]
        raw = verified_snapshot["raw"]
        where = f"source_registry.atoms.{atom_id}"
        if (snapshot_meta["captured_bytes"] != len(raw)
                or snapshot_meta["content_sha256"]
                != verified_snapshot["digest"]):
            _fail(f"{where}.atom.snapshot",
                  "metadata differs from the physical raw snapshot")

        locator = atom_record["locator"]
        ordinal = locator["record_ordinal"]
        records = verified_snapshot["records"]
        if ordinal >= len(records):
            _fail(f"{where}.atom.locator.record_ordinal",
                  "does not resolve in the physical raw snapshot")
        record = records[ordinal]
        raw_start, raw_end = _byte_range(
            locator["record_byte_range"],
            f"{where}.atom.locator.record_byte_range")
        if (raw_start != record["start"] or raw_end != record["end"]
                or locator["record_sha256"]
                != hashlib.sha256(record["payload"]).hexdigest()):
            _fail(f"{where}.atom.locator",
                  "record ordinal, range, or digest differs from the frozen record")
        if locator["offset_mappable"] != record["offset_mappable"]:
            _fail(f"{where}.atom.locator.offset_mappable",
                  "does not match record-level UTF-8 decoding")

        decoded_record = _decode_json_text(
            record["decoded"], f"{where}.atom.locator.record")
        decoded_field = _json_pointer(
            decoded_record, locator["record_path"],
            f"{where}.atom.locator.record_path")
        if not isinstance(decoded_field, str):
            _fail(f"{where}.atom.locator.record_path",
                  "must resolve to a decoded JSON string")
        field_bytes = decoded_field.encode("utf-8")
        start = atom["start"]
        end = atom["end"]
        if end > len(field_bytes):
            _fail(f"{where}.atom.decoded_utf8.range",
                  "lies outside the resolved decoded field")
        quote = field_bytes[start:end]
        try:
            quote.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PhysicalProofError(
                f"{where}.atom.decoded_utf8.range: splits a UTF-8 code point") from error
        if hashlib.sha256(quote).hexdigest() != atom["content_sha256"]:
            _fail(f"{where}.atom.decoded_utf8.range",
                  "resolved field bytes differ from the SourceAtomV2 digest")
        if atom["text_bytes"] is not None and quote != atom["text_bytes"]:
            _fail(f"{where}.atom.text",
                  "retained text differs from the resolved source field bytes")
        atom_states[atom_id] = (
            "verified_retained" if atom["text_bytes"] is not None
            else "verified_locator_only")
    return atom_states


def _candidate_requires_evidence(candidate: Mapping[str, object],
                                 result_candidate_ids: set[str]) -> bool:
    if candidate["exact_identity_id"] is not None:
        return True
    if candidate["source_bindings"]:
        return True
    if candidate["selection"]["opened"]:
        return True
    if candidate["candidate_id"] in result_candidate_ids:
        return True
    return any(
        support["state"] in ("direct", "partial", "contradictory")
        or bool(support["supporting_source_ranges"])
        for support in candidate["slot_support"])


def _validate_frozen_lane_pools(bundle: Mapping[str, object],
                                artifact_bytes: Mapping[str, bytes]) -> int:
    """Bind each captured lane pool to its trace rows and diagnostics.

    This proves only lanes actually declared and frozen by the producer.  In
    particular, the v1 observer exposes one serving ``semantic`` lane; it does
    not thereby prove internal q8 candidates, f16 reranking, or an exhaustive
    diagnostic lane.
    """
    if not bundle["retrievals"]:
        return 0
    traces = {
        trace["trace_id"]: trace for trace in bundle["candidate_traces"]
    }
    verified = 0
    for retrieval_index, retrieval in enumerate(bundle["retrievals"]):
        trace_id = retrieval["candidate_trace_id"]
        if trace_id is None:
            continue
        trace = traces.get(trace_id)
        if trace is None:
            _fail(
                f"trial_bundle.retrievals[{retrieval_index}].candidate_trace_id",
                "does not resolve for frozen-pool verification")
        trace_lanes = {
            lane["lane"]: lane for lane in trace["lane_diagnostics"]
        }
        for lane_index, lane in enumerate(retrieval["lanes"]):
            if lane["state"] != "captured":
                continue
            where = (f"trial_bundle.retrievals[{retrieval_index}]."
                     f"lanes[{lane_index}]")
            capture = lane["pool"]
            if capture["state"] != "captured" or capture["artifact"] is None:
                _fail(f"{where}.pool",
                      "captured lane requires captured physical pool bytes")
            artifact_id = capture["artifact"]["artifact_id"]
            data = artifact_bytes.get(artifact_id)
            if data is None:
                _fail(f"{where}.pool.artifact",
                      "is absent from the physical generation")
            try:
                parsed = sabel_pool.parse_pool_document(
                    data, expected_artifact_id=artifact_id,
                    expected_retrieval_id=retrieval["retrieval_invocation_id"],
                    expected_lane=lane["lane"],
                    expected_stage="run_query_return")
            except sabel_pool.PoolDocumentError as error:
                raise PhysicalProofError(f"{where}.pool: {error}") from error

            trace_lane = trace_lanes.get(lane["lane"])
            if trace_lane is None:
                _fail(f"{where}.lane",
                      "is absent from candidate-trace lane diagnostics")
            if trace_lane["pool_artifact_id"] != artifact_id:
                _fail(f"{where}.pool.artifact.artifact_id",
                      "differs from candidate-trace pool_artifact_id")
            if (trace_lane["score_kind"] != lane["score_kind"]
                    or parsed["candidates"]
                    and any(row["score_kind"] != lane["score_kind"]
                            for row in parsed["candidates"])):
                _fail(f"{where}.score_kind",
                      "differs across retrieval, frozen pool, and candidate trace")

            count = parsed["candidate_count"]
            for label, diagnostics in (
                    ("retrieval", lane["diagnostics"]),
                    ("candidate_trace", trace_lane["diagnostics"])):
                for diagnostic in ("coverage", "truncation"):
                    if diagnostics[diagnostic]["observed_count"] != count:
                        _fail(
                            f"{where}.diagnostics.{diagnostic}.observed_count",
                            f"{label} diagnostics differ from frozen pool count")

            expected = []
            for candidate in trace["candidates"]:
                for lane_score in candidate["lane_scores"]:
                    if lane_score["lane"] == lane["lane"]:
                        expected.append({
                            "candidate_id": candidate["candidate_id"],
                            "rank": lane_score["rank"],
                            "score": lane_score["score"],
                            "score_kind": lane_score["score_kind"],
                            "lane": lane_score["lane"],
                        })
            expected.sort(key=lambda row: row["rank"])
            if parsed["candidates"] != expected:
                _fail(f"{where}.pool.candidates",
                      "ordered rows differ from candidate-trace lane membership")
            verified += 1
    return verified


def _validate_bundle_registry_join(bundle: Mapping[str, object], registry: dict,
                                   artifact_bytes: Mapping[str, bytes]) -> dict:
    atoms = registry["atoms"]
    evidence = registry["evidence"]
    atom_states = _verify_source_snapshots(registry, artifact_bytes)
    frozen_lane_pool_count = _validate_frozen_lane_pools(
        bundle, artifact_bytes)

    for where, value in _iter_source_ranges(bundle, "trial_bundle"):
        atom_id, start, end = _source_range(value, where)
        atom = atoms.get(atom_id)
        if atom is None:
            _fail(f"{where}.atom_id", "phantom atom absent from sealed source registry")
        if start < atom["start"] or end > atom["end"]:
            _fail(where, "source range lies outside its registered SourceAtomV2")

    for trace_index, trace in enumerate(bundle["candidate_traces"]):
        result_candidate_ids = set(trace["result"]["candidate_ids"])
        for candidate_index, candidate in enumerate(trace["candidates"]):
            where = (f"trial_bundle.candidate_traces[{trace_index}]."
                     f"candidates[{candidate_index}]")
            identity = evidence.get(candidate["evidence_id"])
            if identity is None:
                if (candidate["evidence_id"] is not None
                        or _candidate_requires_evidence(
                            candidate, result_candidate_ids)):
                    _fail(f"{where}.evidence_id",
                          "does not resolve a claim that requires sealed evidence bytes")
                continue
            if (candidate["exact_identity_id"] != identity["identity_id"]
                    or candidate["exact_content_sha256"]
                    != identity["exact_content_sha256"]):
                _fail(where, "candidate exact identity contradicts evidence registry")
            alias_ids = {alias["atom_id"] for alias in identity["aliases"]}
            for binding_index, binding in enumerate(candidate["source_bindings"]):
                atom_id = binding["source_range"]["atom_id"]
                if atom_id not in alias_ids:
                    _fail(f"{where}.source_bindings[{binding_index}]",
                          "source atom is not an alias of this candidate evidence")
            selected = candidate["selection"]["selected_alias_atom_id"]
            if selected is not None and selected not in alias_ids:
                _fail(f"{where}.selection.selected_alias_atom_id",
                      "is not an alias of this candidate evidence")

    for atom_id, atom in atoms.items():
        artifact = atom["text_artifact"]
        if artifact is None:
            continue
        physical = artifact_bytes[artifact["artifact_id"]]
        if physical != atom["text_bytes"]:
            _fail(f"source_registry.atoms.{atom_id}.text_artifact",
                  "physical bytes differ from retained SourceAtomV2 text")
        if hashlib.sha256(physical).hexdigest() != atom["content_sha256"]:
            _fail(f"source_registry.atoms.{atom_id}.text_artifact",
                  "physical bytes differ from SourceAtomV2 identity digest")

    exact_query_origin_count = 0
    for retrieval_index, retrieval in enumerate(bundle["retrievals"]):
        query_artifact = retrieval["query"]["artifact"]
        query = artifact_bytes[query_artifact["artifact_id"]]
        for provenance_index, provenance in enumerate(
                retrieval["query_provenance"]):
            query_start = provenance["query_start"]
            query_end = provenance["query_end"]
            for origin_index, origin in enumerate(provenance["origin_ranges"]):
                if origin["transform"] != "exact_copy":
                    continue
                where = (f"trial_bundle.retrievals[{retrieval_index}]."
                         f"query_provenance[{provenance_index}]."
                         f"origin_ranges[{origin_index}]")
                origin_bytes = artifact_bytes.get(origin["artifact_id"])
                if origin_bytes is None:
                    _fail(where, "origin artifact is absent from physical generation")
                if (query[query_start:query_end]
                        != origin_bytes[origin["origin_start"]:origin["origin_end"]]):
                    _fail(where,
                          "exact-copy query bytes differ from the declared origin slice")
                exact_query_origin_count += 1

    mapping_count = 0
    handle_count = 0
    for renderer_index, renderer in enumerate(bundle["renderers"]):
        output_artifact = renderer["output"]["artifact"]
        output = artifact_bytes[output_artifact["artifact_id"]]
        for segment_index, segment in enumerate(renderer["segments"]):
            where = (f"trial_bundle.renderers[{renderer_index}]."
                     f"segments[{segment_index}]")
            displayed_handle = segment["displayed_handle"]
            if displayed_handle is not None:
                start = segment["handle_output_start"]
                end = segment["handle_output_end"]
                if output[start:end] != displayed_handle.encode("utf-8"):
                    _fail(f"{where}.displayed_handle",
                          "declared handle output slice does not contain displayed_handle bytes")
                handle_count += 1
            for mapping_index, mapping in enumerate(segment["source_mappings"]):
                mapping_where = f"{where}.source_mappings[{mapping_index}]"
                atom_id, start, end = _source_range(
                    mapping["source_range"], f"{mapping_where}.source_range")
                quote = _quote(atoms[atom_id], start, end,
                               f"{mapping_where}.source_range")
                output_quote = output[mapping["output_start"]:mapping["output_end"]]
                if output_quote != quote:
                    _fail(mapping_where,
                          "same-length output slice is not the registered source quote")
                mapping_count += 1
    return {
        "artifact_count": len(artifact_bytes),
        "source_mapping_count": mapping_count,
        "displayed_handle_count": handle_count,
        "exact_query_origin_count": exact_query_origin_count,
        "frozen_lane_pool_count": frozen_lane_pool_count,
        "atom_states": atom_states,
    }


def read_and_verify_generation(root: os.PathLike[str] | str) -> dict:
    """Read the manifest commit marker and prove the referenced generation bytes.

    The returned report is deliberately small; callers should retain the sealed
    files as the evidence.  It validates the committed tree; it cannot prove a
    publisher's historical write order.  A successful return means every
    referenced content artifact was safely opened and size/hash checked, every
    atom was resolved again through its raw record and decoded JSON field, and
    all exact renderer mappings and displayed handle slices matched their
    actual bytes.
    """
    manifest_bytes = _read_relative(
        root, "manifest.json", expected_size=None,
        max_bytes=_MAX_MANIFEST_BYTES, where="physical_manifest",
        allow_manifest=True)
    manifest = _decode_json(manifest_bytes, "physical_manifest")
    parsed_manifest = _validate_manifest(manifest, require_hash=True)

    bundle_bytes = _read_artifact(
        root, parsed_manifest["bundle"], "physical_manifest.bundle",
        max_bytes=_MAX_RECORD_BYTES)
    registry_bytes = _read_artifact(
        root, parsed_manifest["registry"], "physical_manifest.source_registry",
        max_bytes=_MAX_RECORD_BYTES)
    bundle = _decode_json(bundle_bytes, "trial_bundle")
    source_registry = _decode_json(registry_bytes, "source_registry")

    try:
        sabel_shadow.validate_trial_bundle(bundle)
    except sabel_shadow.ShadowSchemaError as error:
        raise PhysicalProofError(f"trial_bundle: {error}") from error
    if not isinstance(bundle, Mapping):
        _fail("trial_bundle", "must be an object")
    parsed_registry = _validate_source_registry(source_registry, require_hash=True)

    trial = bundle["trial"]
    trial_id = trial["trial_id"]
    source_generation_id = trial["generation"]["source_generation_id"]
    if (manifest["trial_id"] != trial_id
            or parsed_registry["record"]["trial_id"] != trial_id):
        _fail("physical_manifest.trial_id",
              "manifest, registry, and bundle must name one trial")
    if (manifest["source_generation_id"] != source_generation_id
            or parsed_registry["record"]["source_generation_id"]
            != source_generation_id):
        _fail("physical_manifest.source_generation_id",
              "manifest and registry must bind the trial-frozen source generation")

    referenced: dict[str, dict] = {}
    _collect_content_artifacts(bundle, "trial_bundle", referenced)
    _collect_content_artifacts(source_registry, "source_registry", referenced)
    declared = parsed_manifest["artifacts"]
    if set(declared) != set(referenced):
        _fail("physical_manifest.artifacts",
              f"must exactly enumerate referenced content (missing="
              f"{sorted(set(referenced) - set(declared))}, extra="
              f"{sorted(set(declared) - set(referenced))})")
    for artifact_id, artifact in referenced.items():
        if declared[artifact_id] != artifact:
            _fail(f"physical_manifest.artifacts.{artifact_id}",
                  "metadata differs from the sealed record reference")

    total_artifact_count = len(declared) + 2
    total_artifact_bytes = (
        parsed_manifest["bundle"]["size_bytes"]
        + parsed_manifest["registry"]["size_bytes"]
        + sum(artifact["size_bytes"] for artifact in declared.values()))
    if total_artifact_count > _MAX_ARTIFACT_COUNT:
        _fail("physical_manifest.artifacts",
              f"exceeds artifact-count limit {_MAX_ARTIFACT_COUNT}")
    if total_artifact_bytes > _MAX_TOTAL_ARTIFACT_BYTES:
        _fail("physical_manifest.artifacts",
              f"exceeds cumulative-byte limit {_MAX_TOTAL_ARTIFACT_BYTES}")

    artifact_bytes = {
        artifact_id: _read_artifact(
            root, artifact, f"physical_manifest.artifacts.{artifact_id}")
        for artifact_id, artifact in declared.items()
    }
    report = _validate_bundle_registry_join(
        bundle, parsed_registry, artifact_bytes)
    report.update({
        "trial_id": trial_id,
        "source_generation_id": source_generation_id,
        "bundle_sha256": parsed_manifest["bundle"]["sha256"],
        "source_registry_sha256": parsed_manifest["registry"]["sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
    })
    return report
