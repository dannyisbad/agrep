//! Versioned source-bound evidence primitives for the opt-in SABEL shadow lane.
//!
//! These types are deliberately not wired into v1 ingest or ranking.  Their first
//! job is to make provenance claims precise enough to measure: raw-record bytes,
//! decoded UTF-8 bytes, and tokenizer coordinates are separate namespaces.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fmt;

pub const SOURCE_ATOM_SCHEMA_VERSION: u16 = 2;
pub const EVIDENCE_IDENTITY_SCHEMA_VERSION: u16 = 2;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ByteRange {
    pub start: u64,
    pub end: u64,
}

impl ByteRange {
    pub fn new(start: u64, end: u64) -> Result<Self, SourceAtomError> {
        let value = Self { start, end };
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<(), SourceAtomError> {
        if self.start > self.end {
            return Err(SourceAtomError::InvalidRange {
                start: self.start,
                end: self.end,
            });
        }
        Ok(())
    }

    pub fn len(&self) -> u64 {
        self.end - self.start
    }

    pub fn is_empty(&self) -> bool {
        self.start == self.end
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ImmutableSnapshotV1 {
    /// Stable identity of this captured byte snapshot, not an external live path.
    pub snapshot_id: String,
    /// Debug-only path hint.  It is never part of an atom identity.
    pub source_path_hint: String,
    pub captured_bytes: u64,
    pub content_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RecordLocatorV1 {
    /// The immutable snapshot in which this record occurrence was observed.
    /// This binds provenance without making the snapshot's whole-file hash part
    /// of the append-stable atom identity.
    pub snapshot_id: String,
    pub adapter: String,
    /// Adapter-native stable identity of the logical source stream/file.  This
    /// distinguishes resumed/imported files in one session without depending
    /// on a whole growing-file hash or a mutable path hint.
    pub source_stream_id: String,
    pub session_id: String,
    pub record_ordinal: u64,
    /// Hash of the immutable record bytes.  Unlike a whole growing-file hash,
    /// this remains stable when a transcript receives a later appended record.
    pub record_sha256: String,
    pub record_byte_range: ByteRange,
    /// Source-native path such as `/message/content/2/text`.
    pub record_path: String,
    /// False when record-level replacement decoding was required.  Even when
    /// true, JSON escapes mean decoded field coordinates are not raw JSON byte
    /// coordinates; `record_path` and `decoded_utf8` remain separate locators.
    pub offset_mappable: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct DecodedUtf8CoordinateV1 {
    /// UTF-8 byte coordinates within the decoded source field named by
    /// `RecordLocatorV1::record_path`; never raw JSONL byte coordinates.
    pub range: ByteRange,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TokenCoordinateV1 {
    pub tokenizer_profile: String,
    pub start: u32,
    pub end: u32,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ContentRetentionV1 {
    RetainedText,
    LocatorOnly {
        /// UTF-8 bytes in this atom.  The payload itself is intentionally not
        /// retained, but its size must equal `decoded_utf8.range.len()`.
        atom_utf8_bytes: u64,
        content_sha256: String,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SourceOriginV1 {
    pub speaker_role: String,
    pub base_origin: String,
    pub atom_type: String,
    pub source_native_type: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ProvenanceV1 {
    pub project: String,
    pub session_id: String,
    pub family_id: String,
    pub turn: u32,
    pub timestamp_ms: i64,
    pub root_or_delegated: String,
    pub parent_session_id: Option<String>,
    pub tool_links: Vec<String>,
    pub replay_of: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SourceAtomV2 {
    pub schema_version: u16,
    pub atom_id: String,
    pub snapshot: ImmutableSnapshotV1,
    pub locator: RecordLocatorV1,
    pub decoded_utf8: DecodedUtf8CoordinateV1,
    pub token_coordinate: Option<TokenCoordinateV1>,
    pub origin: SourceOriginV1,
    pub provenance: ProvenanceV1,
    /// Exact decoded atom text when retention policy permits it.  Locator-only
    /// tool payloads intentionally leave this absent.
    pub text: Option<String>,
    pub retention: ContentRetentionV1,
}

impl SourceAtomV2 {
    pub fn new_retained(
        snapshot: ImmutableSnapshotV1,
        locator: RecordLocatorV1,
        decoded_utf8: DecodedUtf8CoordinateV1,
        origin: SourceOriginV1,
        provenance: ProvenanceV1,
        text: String,
    ) -> Result<Self, SourceAtomError> {
        let content_hash = sha256_hex(text.as_bytes());
        let atom_id = atom_id(&locator, &decoded_utf8, &content_hash);
        let value = Self {
            schema_version: SOURCE_ATOM_SCHEMA_VERSION,
            atom_id,
            snapshot,
            locator,
            decoded_utf8,
            token_coordinate: None,
            origin,
            provenance,
            text: Some(text),
            retention: ContentRetentionV1::RetainedText,
        };
        value.validate()?;
        Ok(value)
    }

    pub fn new_locator_only(
        snapshot: ImmutableSnapshotV1,
        locator: RecordLocatorV1,
        decoded_utf8: DecodedUtf8CoordinateV1,
        origin: SourceOriginV1,
        provenance: ProvenanceV1,
        atom_utf8_bytes: u64,
        content_sha256: String,
    ) -> Result<Self, SourceAtomError> {
        require_sha256("content_sha256", &content_sha256)?;
        let atom_id = atom_id(&locator, &decoded_utf8, &content_sha256);
        let value = Self {
            schema_version: SOURCE_ATOM_SCHEMA_VERSION,
            atom_id,
            snapshot,
            locator,
            decoded_utf8,
            token_coordinate: None,
            origin,
            provenance,
            text: None,
            retention: ContentRetentionV1::LocatorOnly {
                atom_utf8_bytes,
                content_sha256,
            },
        };
        value.validate()?;
        Ok(value)
    }

    pub fn content_sha256(&self) -> Result<String, SourceAtomError> {
        self.validate()?;
        match (&self.retention, &self.text) {
            (ContentRetentionV1::RetainedText, Some(text)) => Ok(sha256_hex(text.as_bytes())),
            (ContentRetentionV1::LocatorOnly { content_sha256, .. }, None) => {
                require_sha256("content_sha256", content_sha256)?;
                Ok(content_sha256.clone())
            }
            _ => Err(SourceAtomError::RetentionMismatch),
        }
    }

    pub fn quote(&self, range: &ByteRange) -> Result<&str, SourceAtomError> {
        // Deserialized public structs can be mutated independently.  Never hand
        // out a quote until its retained bytes still match the immutable ID.
        self.validate()?;
        if range.start < self.decoded_utf8.range.start || range.end > self.decoded_utf8.range.end {
            return Err(SourceAtomError::QuoteOutsideAtom);
        }
        let text = self
            .text
            .as_deref()
            .ok_or(SourceAtomError::ContentNotRetained)?;
        let local = ByteRange {
            start: range.start - self.decoded_utf8.range.start,
            end: range.end - self.decoded_utf8.range.start,
        };
        slice_utf8(text, &local)
    }

    /// Return the exact decoded span declared by this atom.
    pub fn exact_quote(&self) -> Result<&str, SourceAtomError> {
        self.validate()?;
        self.text
            .as_deref()
            .ok_or(SourceAtomError::ContentNotRetained)
    }

    pub fn evidence_alias(&self, kind: AliasKindV2) -> EvidenceAliasV2 {
        EvidenceAliasV2 {
            atom_id: self.atom_id.clone(),
            kind,
            locator: self.locator.clone(),
            decoded_utf8: self.decoded_utf8.clone(),
        }
    }

    pub fn validate(&self) -> Result<(), SourceAtomError> {
        if self.schema_version != SOURCE_ATOM_SCHEMA_VERSION {
            return Err(SourceAtomError::UnsupportedSchema(self.schema_version));
        }
        validate_snapshot(&self.snapshot)?;
        validate_locator(&self.locator)?;
        if self.locator.snapshot_id != self.snapshot.snapshot_id {
            return Err(SourceAtomError::SnapshotMismatch);
        }
        if self.locator.record_byte_range.end > self.snapshot.captured_bytes {
            return Err(SourceAtomError::RawRangeOutsideSnapshot);
        }
        if self.locator.session_id != self.provenance.session_id {
            return Err(SourceAtomError::ProvenanceMismatch);
        }
        self.decoded_utf8.range.validate()?;
        if let Some(token) = &self.token_coordinate {
            if token.start > token.end || token.tokenizer_profile.is_empty() {
                return Err(SourceAtomError::InvalidTokenCoordinate);
            }
        }
        let content_hash = match (&self.retention, &self.text) {
            (ContentRetentionV1::RetainedText, Some(text)) => {
                if text.len() as u64 != self.decoded_utf8.range.len() {
                    return Err(SourceAtomError::DecodedRangeLengthMismatch {
                        range_bytes: self.decoded_utf8.range.len(),
                        atom_bytes: text.len() as u64,
                    });
                }
                sha256_hex(text.as_bytes())
            }
            (
                ContentRetentionV1::LocatorOnly {
                    atom_utf8_bytes,
                    content_sha256,
                },
                None,
            ) => {
                require_sha256("content_sha256", content_sha256)?;
                if *atom_utf8_bytes != self.decoded_utf8.range.len() {
                    return Err(SourceAtomError::DecodedRangeLengthMismatch {
                        range_bytes: self.decoded_utf8.range.len(),
                        atom_bytes: *atom_utf8_bytes,
                    });
                }
                content_sha256.clone()
            }
            _ => return Err(SourceAtomError::RetentionMismatch),
        };
        let expected = atom_id(&self.locator, &self.decoded_utf8, &content_hash);
        if self.atom_id != expected {
            return Err(SourceAtomError::IdentityMismatch);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AliasKindV2 {
    Exact,
    Replay,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct EvidenceAliasV2 {
    pub atom_id: String,
    pub kind: AliasKindV2,
    /// Every exact/replay occurrence retains its independently resolvable
    /// source coordinates.  An atom ID alone is not an evidence locator.
    pub locator: RecordLocatorV1,
    pub decoded_utf8: DecodedUtf8CoordinateV1,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct NormalizedIdentityAuditV2 {
    pub normalization_version: String,
    pub normalized_sha256: String,
    /// Must remain false until a content-type-aware experiment authorizes use.
    pub ranking_eligible: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct EvidenceIdentityV2 {
    pub schema_version: u16,
    pub identity_id: String,
    pub exact_content_sha256: String,
    pub canonical_atom_id: String,
    pub aliases: Vec<EvidenceAliasV2>,
    pub normalized_audit: Option<NormalizedIdentityAuditV2>,
}

impl EvidenceIdentityV2 {
    pub fn new(
        exact_content_sha256: String,
        canonical_atom_id: String,
        aliases: Vec<EvidenceAliasV2>,
    ) -> Result<Self, SourceAtomError> {
        require_sha256("exact_content_sha256", &exact_content_sha256)?;
        if canonical_atom_id.is_empty() {
            return Err(SourceAtomError::MissingField("canonical_atom_id"));
        }
        // Keep the complete cryptographic digest.  Truncation would make this
        // grouping key weaker than the exact-copy hash it claims to represent.
        let identity_id = format!("ae2:{exact_content_sha256}");
        let value = Self {
            schema_version: EVIDENCE_IDENTITY_SCHEMA_VERSION,
            identity_id,
            exact_content_sha256,
            canonical_atom_id,
            aliases,
            normalized_audit: None,
        };
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<(), SourceAtomError> {
        if self.schema_version != EVIDENCE_IDENTITY_SCHEMA_VERSION {
            return Err(SourceAtomError::UnsupportedEvidenceSchema(
                self.schema_version,
            ));
        }
        require_sha256("exact_content_sha256", &self.exact_content_sha256)?;
        require_prefixed_sha256("canonical_atom_id", &self.canonical_atom_id, "as2:")?;
        let expected_identity_id = format!("ae2:{}", self.exact_content_sha256);
        if self.identity_id != expected_identity_id {
            return Err(SourceAtomError::EvidenceIdentityMismatch);
        }
        for alias in &self.aliases {
            require_prefixed_sha256("alias atom_id", &alias.atom_id, "as2:")?;
            validate_locator(&alias.locator)?;
            alias.decoded_utf8.range.validate()?;
        }
        if !self
            .aliases
            .iter()
            .any(|alias| alias.atom_id == self.canonical_atom_id)
        {
            return Err(SourceAtomError::CanonicalAliasMissing);
        }
        if self
            .normalized_audit
            .as_ref()
            .is_some_and(|audit| audit.ranking_eligible)
        {
            return Err(SourceAtomError::NormalizedIdentityUsedForRanking);
        }
        if let Some(audit) = &self.normalized_audit {
            require_sha256("normalized_sha256", &audit.normalized_sha256)?;
            if audit.normalization_version.is_empty() {
                return Err(SourceAtomError::MissingField("normalization_version"));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Eq, PartialEq)]
pub enum SourceAtomError {
    ContentNotRetained,
    CanonicalAliasMissing,
    DecodedRangeLengthMismatch { range_bytes: u64, atom_bytes: u64 },
    EvidenceIdentityMismatch,
    IdentityMismatch,
    InvalidRange { start: u64, end: u64 },
    InvalidIdentifier(&'static str),
    InvalidSha256(&'static str),
    InvalidTokenCoordinate,
    InvalidUtf8Boundary,
    MissingField(&'static str),
    NormalizedIdentityUsedForRanking,
    ProvenanceMismatch,
    QuoteOutsideAtom,
    RawRangeOutsideSnapshot,
    RangeOutsideText,
    RetentionMismatch,
    SnapshotMismatch,
    UnsupportedEvidenceSchema(u16),
    UnsupportedSchema(u16),
}

impl fmt::Display for SourceAtomError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for SourceAtomError {}

fn validate_snapshot(snapshot: &ImmutableSnapshotV1) -> Result<(), SourceAtomError> {
    if snapshot.snapshot_id.is_empty() {
        return Err(SourceAtomError::MissingField("snapshot_id"));
    }
    require_sha256("snapshot content_sha256", &snapshot.content_sha256)
}

fn validate_locator(locator: &RecordLocatorV1) -> Result<(), SourceAtomError> {
    locator.record_byte_range.validate()?;
    require_sha256("record_sha256", &locator.record_sha256)?;
    if locator.snapshot_id.is_empty() {
        return Err(SourceAtomError::MissingField("locator snapshot_id"));
    }
    if locator.adapter.is_empty() {
        return Err(SourceAtomError::MissingField("adapter"));
    }
    if locator.source_stream_id.is_empty() {
        return Err(SourceAtomError::MissingField("source_stream_id"));
    }
    if locator.session_id.is_empty() {
        return Err(SourceAtomError::MissingField("locator session_id"));
    }
    if locator.record_path.is_empty() {
        return Err(SourceAtomError::MissingField("record_path"));
    }
    Ok(())
}

fn require_sha256(field: &'static str, value: &str) -> Result<(), SourceAtomError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(SourceAtomError::InvalidSha256(field));
    }
    Ok(())
}

fn require_prefixed_sha256(
    field: &'static str,
    value: &str,
    prefix: &str,
) -> Result<(), SourceAtomError> {
    let digest = value
        .strip_prefix(prefix)
        .ok_or(SourceAtomError::InvalidIdentifier(field))?;
    require_sha256(field, digest).map_err(|_| SourceAtomError::InvalidIdentifier(field))
}

fn atom_id(
    locator: &RecordLocatorV1,
    decoded: &DecodedUtf8CoordinateV1,
    content_sha256: &str,
) -> String {
    let payload = serde_json::to_vec(&(
        SOURCE_ATOM_SCHEMA_VERSION,
        &locator.adapter,
        &locator.source_stream_id,
        &locator.session_id,
        locator.record_ordinal,
        &locator.record_sha256,
        &locator.record_path,
        decoded.range.start,
        decoded.range.end,
        content_sha256,
    ))
    .expect("source atom identity tuple serializes");
    format!("as2:{}", sha256_hex(&payload))
}

fn slice_utf8<'a>(text: &'a str, range: &ByteRange) -> Result<&'a str, SourceAtomError> {
    range.validate()?;
    let start = usize::try_from(range.start).map_err(|_| SourceAtomError::RangeOutsideText)?;
    let end = usize::try_from(range.end).map_err(|_| SourceAtomError::RangeOutsideText)?;
    if end > text.len() {
        return Err(SourceAtomError::RangeOutsideText);
    }
    if !text.is_char_boundary(start) || !text.is_char_boundary(end) {
        return Err(SourceAtomError::InvalidUtf8Boundary);
    }
    Ok(&text[start..end])
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(id: &str, body: &[u8]) -> ImmutableSnapshotV1 {
        ImmutableSnapshotV1 {
            snapshot_id: id.to_string(),
            source_path_hint: "/private/history.jsonl".to_string(),
            captured_bytes: body.len() as u64,
            content_sha256: sha256_hex(body),
        }
    }

    fn locator(snapshot_id: &str, record: &[u8]) -> RecordLocatorV1 {
        RecordLocatorV1 {
            snapshot_id: snapshot_id.to_string(),
            adapter: "claude".to_string(),
            source_stream_id: "history-stream-1".to_string(),
            session_id: "session-1".to_string(),
            record_ordinal: 7,
            record_sha256: sha256_hex(record),
            record_byte_range: ByteRange::new(0, record.len() as u64).unwrap(),
            record_path: "/message/content/0/text".to_string(),
            offset_mappable: true,
        }
    }

    fn origin(atom_type: &str) -> SourceOriginV1 {
        SourceOriginV1 {
            speaker_role: "assistant".to_string(),
            base_origin: "delegated".to_string(),
            atom_type: atom_type.to_string(),
            source_native_type: "assistant".to_string(),
        }
    }

    fn provenance() -> ProvenanceV1 {
        ProvenanceV1 {
            project: "agrep".to_string(),
            session_id: "session-1".to_string(),
            family_id: "family-1".to_string(),
            turn: 2,
            timestamp_ms: 123,
            root_or_delegated: "delegated".to_string(),
            parent_session_id: Some("root".to_string()),
            tool_links: Vec::new(),
            replay_of: None,
        }
    }

    #[test]
    fn atom_identity_survives_a_later_snapshot_append() {
        let record = br#"{"message":{"content":"done"}}"#;
        let decoded = DecodedUtf8CoordinateV1 {
            range: ByteRange::new(0, 4).unwrap(),
        };
        let first = SourceAtomV2::new_retained(
            snapshot("capture-1", record),
            locator("capture-1", record),
            decoded.clone(),
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        let mut appended = record.to_vec();
        appended.extend_from_slice(b"\n{\"later\":true}");
        let second = SourceAtomV2::new_retained(
            snapshot("capture-2", &appended),
            locator("capture-2", record),
            decoded,
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        assert_ne!(
            first.snapshot.content_sha256,
            second.snapshot.content_sha256
        );
        assert_eq!(first.atom_id, second.atom_id);
    }

    #[test]
    fn identical_records_in_different_source_streams_do_not_collide() {
        let record = br#"{"message":{"content":"done"}}"#;
        let decoded = DecodedUtf8CoordinateV1 {
            range: ByteRange::new(0, 4).unwrap(),
        };
        let first = SourceAtomV2::new_retained(
            snapshot("capture", record),
            RecordLocatorV1 {
                source_stream_id: "codex-file-a".to_string(),
                ..locator("capture", record)
            },
            decoded.clone(),
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        let second = SourceAtomV2::new_retained(
            snapshot("capture", record),
            RecordLocatorV1 {
                source_stream_id: "codex-file-b".to_string(),
                ..locator("capture", record)
            },
            decoded,
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        assert_ne!(first.atom_id, second.atom_id);
    }

    #[test]
    fn nonzero_parent_coordinate_quotes_translate_into_atom_text() {
        let record = br#"{"message":{"content":"done"}}"#;
        let atom = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(100, 104).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        assert_eq!(atom.exact_quote().unwrap(), "done");
        assert_eq!(
            atom.quote(&ByteRange::new(101, 103).unwrap()).unwrap(),
            "on"
        );
        assert_eq!(
            atom.quote(&ByteRange::new(0, 1).unwrap()),
            Err(SourceAtomError::QuoteOutsideAtom)
        );
    }

    #[test]
    fn raw_json_and_decoded_utf8_coordinates_are_distinct() {
        let record = br#"{"text":"\u00e9"}"#;
        let atom = SourceAtomV2::new_retained(
            snapshot("capture", record),
            RecordLocatorV1 {
                offset_mappable: false,
                ..locator("capture", record)
            },
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 2).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "é".to_string(),
        )
        .unwrap();
        assert_ne!(
            atom.locator.record_byte_range.len(),
            atom.decoded_utf8.range.len()
        );
        assert_eq!(atom.quote(&ByteRange::new(0, 2).unwrap()).unwrap(), "é");
    }

    #[test]
    fn quote_rejects_a_mid_codepoint_boundary() {
        let record = br#"{"text":"\u00e9"}"#;
        let atom = SourceAtomV2::new_retained(
            snapshot("capture", record),
            RecordLocatorV1 {
                offset_mappable: false,
                ..locator("capture", record)
            },
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 2).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "é".to_string(),
        )
        .unwrap();
        assert_eq!(
            atom.quote(&ByteRange::new(1, 2).unwrap()),
            Err(SourceAtomError::InvalidUtf8Boundary)
        );
    }

    #[test]
    fn locator_only_tool_payload_cannot_be_quoted() {
        let record = br#"{"tool_result":"secret"}"#;
        let atom = SourceAtomV2::new_locator_only(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 6).unwrap(),
            },
            origin("tool_observation"),
            provenance(),
            6,
            sha256_hex(b"secret"),
        )
        .unwrap();
        assert_eq!(
            atom.quote(&ByteRange::new(0, 1).unwrap()),
            Err(SourceAtomError::ContentNotRetained)
        );
    }

    #[test]
    fn normalized_identity_is_audit_only() {
        let exact = sha256_hex(b"Do not change CASE");
        let canonical_locator = locator("capture", b"Do not change CASE");
        let canonical_atom_id = format!("as2:{}", sha256_hex(b"canonical atom"));
        let mut identity = EvidenceIdentityV2::new(
            exact,
            canonical_atom_id.clone(),
            vec![EvidenceAliasV2 {
                atom_id: canonical_atom_id,
                kind: AliasKindV2::Exact,
                locator: canonical_locator,
                decoded_utf8: DecodedUtf8CoordinateV1 {
                    range: ByteRange::new(0, 18).unwrap(),
                },
            }],
        )
        .unwrap();
        let identity_id = identity.identity_id.clone();
        identity.normalized_audit = Some(NormalizedIdentityAuditV2 {
            normalization_version: "audit-v1".to_string(),
            normalized_sha256: sha256_hex(b"do not change case"),
            ranking_eligible: false,
        });
        assert_eq!(identity.validate(), Ok(()));
        assert_eq!(identity.identity_id, identity_id);

        identity.normalized_audit.as_mut().unwrap().ranking_eligible = true;
        assert_eq!(
            identity.validate(),
            Err(SourceAtomError::NormalizedIdentityUsedForRanking)
        );
    }

    #[test]
    fn retained_atom_rejects_a_decoded_range_outside_text() {
        let record = br#"{"text":"short"}"#;
        let error = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 6).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "short".to_string(),
        )
        .unwrap_err();
        assert_eq!(
            error,
            SourceAtomError::DecodedRangeLengthMismatch {
                range_bytes: 6,
                atom_bytes: 5,
            }
        );
    }

    #[test]
    fn locator_must_belong_to_the_declared_snapshot() {
        let record = br#"{"text":"done"}"#;
        let error = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("other-capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 4).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap_err();
        assert_eq!(error, SourceAtomError::SnapshotMismatch);
    }

    #[test]
    fn raw_record_range_must_fit_the_frozen_snapshot() {
        let record = br#"{"text":"done"}"#;
        let mut outside = locator("capture", record);
        outside.record_byte_range.end += 1;
        let error = SourceAtomV2::new_retained(
            snapshot("capture", record),
            outside,
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 4).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap_err();
        assert_eq!(error, SourceAtomError::RawRangeOutsideSnapshot);
    }

    #[test]
    fn locator_and_provenance_sessions_cannot_contradict() {
        let record = br#"{"text":"done"}"#;
        let mut contradictory = provenance();
        contradictory.session_id = "another-session".to_string();
        let error = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 4).unwrap(),
            },
            origin("assistant_message"),
            contradictory,
            "done".to_string(),
        )
        .unwrap_err();
        assert_eq!(error, SourceAtomError::ProvenanceMismatch);
    }

    #[test]
    fn identifiers_are_canonical_lowercase_full_sha256() {
        let record = br#"{"text":"done"}"#;
        let atom = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 4).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        assert_eq!(atom.atom_id.len(), "as2:".len() + 64);
        assert!(atom.atom_id.starts_with("as2:"));

        let exact = atom.content_sha256().unwrap();
        let identity = EvidenceIdentityV2::new(
            exact,
            atom.atom_id.clone(),
            vec![atom.evidence_alias(AliasKindV2::Exact)],
        )
        .unwrap();
        assert_eq!(identity.identity_id.len(), "ae2:".len() + 64);
        assert!(identity.identity_id.starts_with("ae2:"));

        let uppercase_hash = sha256_hex(b"secret").to_ascii_uppercase();
        assert_eq!(
            require_sha256("content_sha256", &uppercase_hash),
            Err(SourceAtomError::InvalidSha256("content_sha256"))
        );
    }

    #[test]
    fn source_atom_v2_json_identity_matches_cross_language_golden() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("fixtures/source_atom_v2_golden.json")).unwrap();
        assert_eq!(fixture["schema"], "agrep.source-atom-v2-golden");
        let atom: SourceAtomV2 = serde_json::from_value(fixture["atom"].clone()).unwrap();
        atom.validate().unwrap();
        assert_eq!(
            atom.atom_id,
            "as2:a543836ee28f119b8ada655307f770b3601c71c1ce0e276c9ae979515e8d2138"
        );
        assert_eq!(atom.locator.record_ordinal, u64::MAX);
        assert_eq!(atom.provenance.turn, u32::MAX);
        assert_eq!(atom.provenance.timestamp_ms, i64::MIN);
        assert_eq!(atom.token_coordinate.as_ref().unwrap().end, u32::MAX);
    }

    #[test]
    fn content_hash_accessor_rejects_mutated_invalid_state() {
        let record = br#"{"text":"done"}"#;
        let mut atom = SourceAtomV2::new_retained(
            snapshot("capture", record),
            locator("capture", record),
            DecodedUtf8CoordinateV1 {
                range: ByteRange::new(0, 4).unwrap(),
            },
            origin("assistant_message"),
            provenance(),
            "done".to_string(),
        )
        .unwrap();
        atom.text = None;
        assert_eq!(
            atom.content_sha256(),
            Err(SourceAtomError::RetentionMismatch)
        );
    }
}
