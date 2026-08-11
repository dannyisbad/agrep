//! Byte-aware frozen source records for the opt-in SABEL shadow lane.
//!
//! Existing adapters intentionally tolerate invalid UTF-8 by replacement-decoding
//! a whole live file before calling `str::lines`.  That is good availability
//! behavior, but it cannot support honest raw-byte provenance after the fact.
//! This module freezes one already-read byte extent and reproduces the relevant
//! line framing while keeping raw and decoded coordinate systems separate.

use crate::source_atom::{sha256_hex, ByteRange, ImmutableSnapshotV1, RecordLocatorV1};
use std::borrow::Cow;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LineEndingV1 {
    None,
    Lf,
    CrLf,
}

#[derive(Clone, Debug)]
pub struct FrozenSourceV1 {
    pub snapshot: ImmutableSnapshotV1,
    /// Adapter-native identity for the logical append-only stream/file.  This
    /// is deliberately separate from both a mutable path and snapshot content.
    pub source_stream_id: String,
    bytes: Vec<u8>,
}

impl FrozenSourceV1 {
    /// Capture exactly the bytes supplied by the guarded source reader.
    ///
    /// `source_path_hint` is diagnostic only.  Snapshot identity is content-bound
    /// so moving the frozen artifact does not change its identity.
    pub fn from_bytes(
        source_stream_id: impl Into<String>,
        source_path_hint: impl Into<String>,
        bytes: Vec<u8>,
    ) -> Self {
        let content_sha256 = sha256_hex(&bytes);
        let snapshot_id = format!("ss1:{}", &content_sha256[..32]);
        Self {
            snapshot: ImmutableSnapshotV1 {
                snapshot_id,
                source_path_hint: source_path_hint.into(),
                captured_bytes: bytes.len() as u64,
                content_sha256,
            },
            source_stream_id: source_stream_id.into(),
            bytes,
        }
    }

    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    pub fn records(&self) -> FrozenRecordIter<'_> {
        FrozenRecordIter {
            snapshot_id: &self.snapshot.snapshot_id,
            source_stream_id: &self.source_stream_id,
            bytes: &self.bytes,
            cursor: 0,
            ordinal: 0,
        }
    }
}

#[derive(Debug)]
pub struct FrozenRecordV1<'a> {
    pub snapshot_id: &'a str,
    pub source_stream_id: &'a str,
    pub ordinal: u64,
    /// Raw record payload, excluding its JSONL line terminator.
    pub payload_byte_range: ByteRange,
    /// Raw record frame, including LF/CRLF when present.
    pub framed_byte_range: ByteRange,
    pub line_ending: LineEndingV1,
    pub raw_payload: &'a [u8],
    pub decoded: Cow<'a, str>,
    /// True only when record-level replacement decoding was unnecessary.  This
    /// does not claim that JSON-unescaped field offsets map into raw JSON bytes.
    pub offset_mappable: bool,
    pub record_sha256: String,
}

impl FrozenRecordV1<'_> {
    pub fn locator(
        &self,
        adapter: impl Into<String>,
        session_id: impl Into<String>,
        record_path: impl Into<String>,
    ) -> RecordLocatorV1 {
        RecordLocatorV1 {
            snapshot_id: self.snapshot_id.to_string(),
            adapter: adapter.into(),
            source_stream_id: self.source_stream_id.to_string(),
            session_id: session_id.into(),
            record_ordinal: self.ordinal,
            record_sha256: self.record_sha256.clone(),
            record_byte_range: self.payload_byte_range.clone(),
            record_path: record_path.into(),
            offset_mappable: self.offset_mappable,
        }
    }
}

pub struct FrozenRecordIter<'a> {
    snapshot_id: &'a str,
    source_stream_id: &'a str,
    bytes: &'a [u8],
    cursor: usize,
    ordinal: u64,
}

impl<'a> Iterator for FrozenRecordIter<'a> {
    type Item = FrozenRecordV1<'a>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.cursor >= self.bytes.len() {
            return None;
        }

        let start = self.cursor;
        let relative_newline = self.bytes[start..].iter().position(|byte| *byte == b'\n');
        let (payload_end, frame_end, line_ending) = match relative_newline {
            Some(relative) => {
                let newline = start + relative;
                if newline > start && self.bytes[newline - 1] == b'\r' {
                    (newline - 1, newline + 1, LineEndingV1::CrLf)
                } else {
                    (newline, newline + 1, LineEndingV1::Lf)
                }
            }
            None => (self.bytes.len(), self.bytes.len(), LineEndingV1::None),
        };
        self.cursor = frame_end;

        let ordinal = self.ordinal;
        self.ordinal += 1;
        let raw_payload = &self.bytes[start..payload_end];
        let offset_mappable = std::str::from_utf8(raw_payload).is_ok();
        let decoded = String::from_utf8_lossy(raw_payload);

        Some(FrozenRecordV1 {
            snapshot_id: self.snapshot_id,
            source_stream_id: self.source_stream_id,
            ordinal,
            payload_byte_range: ByteRange {
                start: start as u64,
                end: payload_end as u64,
            },
            framed_byte_range: ByteRange {
                start: start as u64,
                end: frame_end as u64,
            },
            line_ending,
            raw_payload,
            decoded,
            offset_mappable,
            record_sha256: sha256_hex(raw_payload),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::source_atom::{DecodedUtf8CoordinateV1, ProvenanceV1, SourceAtomV2, SourceOriginV1};

    #[test]
    fn records_preserve_crlf_blank_lines_and_raw_ranges() {
        let source = FrozenSourceV1::from_bytes(
            "claude-fixture-1",
            "/history.jsonl",
            b"{\"one\":1}\r\n\r\n{\"two\":2}\n".to_vec(),
        );
        let records: Vec<_> = source.records().collect();

        assert_eq!(records.len(), 3);
        assert_eq!(records[0].raw_payload, b"{\"one\":1}");
        assert_eq!(
            records[0].payload_byte_range,
            ByteRange { start: 0, end: 9 }
        );
        assert_eq!(
            records[0].framed_byte_range,
            ByteRange { start: 0, end: 11 }
        );
        assert_eq!(records[0].line_ending, LineEndingV1::CrLf);

        assert_eq!(records[1].raw_payload, b"");
        assert_eq!(
            records[1].payload_byte_range,
            ByteRange { start: 11, end: 11 }
        );
        assert_eq!(
            records[1].framed_byte_range,
            ByteRange { start: 11, end: 13 }
        );
        assert_eq!(records[1].line_ending, LineEndingV1::CrLf);

        assert_eq!(records[2].raw_payload, b"{\"two\":2}");
        assert_eq!(
            records[2].payload_byte_range,
            ByteRange { start: 13, end: 22 }
        );
        assert_eq!(
            records[2].framed_byte_range,
            ByteRange { start: 13, end: 23 }
        );
        assert_eq!(records[2].line_ending, LineEndingV1::Lf);
    }

    #[test]
    fn trailing_terminator_does_not_invent_an_extra_record() {
        let source = FrozenSourceV1::from_bytes("stream", "x", b"a\nb\n".to_vec());
        assert_eq!(
            source
                .records()
                .map(|record| record.decoded.into_owned())
                .collect::<Vec<_>>(),
            vec!["a", "b"]
        );
    }

    #[test]
    fn valid_multibyte_text_is_exactly_mappable() {
        let source = FrozenSourceV1::from_bytes("stream", "x", "é\n".as_bytes().to_vec());
        let record = source.records().next().unwrap();
        assert_eq!(record.decoded, "é");
        assert!(record.offset_mappable);
        assert_eq!(record.payload_byte_range.len(), 2);
    }

    #[test]
    fn invalid_utf8_is_retained_raw_but_never_claimed_mappable() {
        let source = FrozenSourceV1::from_bytes("stream", "x", vec![b'a', 0xff, b'\n']);
        let record = source.records().next().unwrap();
        assert_eq!(record.raw_payload, &[b'a', 0xff]);
        assert_eq!(record.decoded, "a�");
        assert!(!record.offset_mappable);
        assert_eq!(record.record_sha256, sha256_hex(&[b'a', 0xff]));
        assert!(!record.locator("codex", "s", "/payload").offset_mappable);
    }

    #[test]
    fn final_lone_carriage_return_is_payload_not_a_line_ending() {
        let source = FrozenSourceV1::from_bytes("stream", "x", b"record\r".to_vec());
        let record = source.records().next().unwrap();
        assert_eq!(record.raw_payload, b"record\r");
        assert_eq!(record.line_ending, LineEndingV1::None);
        assert_eq!(record.framed_byte_range, record.payload_byte_range);
    }

    #[test]
    fn snapshot_identity_is_path_independent_and_content_bound() {
        let left = FrozenSourceV1::from_bytes("stream", "/old", b"same".to_vec());
        let moved = FrozenSourceV1::from_bytes("stream", "/new", b"same".to_vec());
        let changed = FrozenSourceV1::from_bytes("stream", "/old", b"changed".to_vec());
        assert_eq!(left.snapshot.snapshot_id, moved.snapshot.snapshot_id);
        assert_ne!(left.snapshot.snapshot_id, changed.snapshot.snapshot_id);
    }

    #[test]
    fn framing_matches_the_existing_lossy_lines_contract() {
        let cases: &[&[u8]] = &[
            b"",
            b"\n",
            b"\n\n",
            b"a\r\n",
            b"a\r\r\n",
            b"final\r",
            "é\nnext".as_bytes(),
            &[b'a', 0xff, b'\n', b'b'],
            &[0xff, b'\n'],
        ];

        for (index, bytes) in cases.iter().enumerate() {
            let current: Vec<_> = String::from_utf8_lossy(bytes)
                .lines()
                .map(str::to_owned)
                .collect();
            let frozen = FrozenSourceV1::from_bytes(
                format!("fixture-{index}"),
                "fixture.jsonl",
                bytes.to_vec(),
            );
            let shadow: Vec<_> = frozen
                .records()
                .map(|record| record.decoded.into_owned())
                .collect();
            assert_eq!(shadow, current, "case {index}: {bytes:?}");
        }
    }

    #[test]
    fn frozen_json_record_resolves_to_a_source_bound_atom() {
        let bytes = br#"{"session":"s","message":{"content":[{"type":"text","text":"caf\u00e9"}]}}
"#
        .to_vec();
        let source = FrozenSourceV1::from_bytes("codex-rollout-1", "fixture.jsonl", bytes);
        let record = source.records().next().unwrap();
        let value: serde_json::Value = serde_json::from_str(&record.decoded).unwrap();
        let text = value["message"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .to_string();
        let decoded_utf8 = DecodedUtf8CoordinateV1 {
            range: ByteRange::new(0, text.len() as u64).unwrap(),
        };
        let atom = SourceAtomV2::new_retained(
            source.snapshot.clone(),
            record.locator("codex", "s", "/message/content/0/text"),
            decoded_utf8,
            SourceOriginV1 {
                speaker_role: "assistant".to_string(),
                base_origin: "root".to_string(),
                atom_type: "assistant_message".to_string(),
                source_native_type: "agent_message".to_string(),
            },
            ProvenanceV1 {
                project: "agrep".to_string(),
                session_id: "s".to_string(),
                family_id: "family-s".to_string(),
                turn: 1,
                timestamp_ms: 1,
                root_or_delegated: "root".to_string(),
                parent_session_id: None,
                tool_links: Vec::new(),
                replay_of: None,
            },
            text,
        )
        .unwrap();

        assert_eq!(atom.exact_quote().unwrap(), "café");
        assert!(atom.atom_id.starts_with("as2:"));
        assert_eq!(atom.locator.snapshot_id, source.snapshot.snapshot_id);
        assert_ne!(
            atom.locator.record_byte_range.len(),
            atom.decoded_utf8.range.len()
        );
    }
}
