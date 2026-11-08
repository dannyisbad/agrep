use super::*;
use crate::ingest::pi::compact_record;

fn projected_record_reader(reader: impl std::io::Read) -> serde_json::Result<serde_json::Value> {
    projected_decoded_record_reader(crate::ingest::jsonl::LossyReader::new(reader))
}

fn file_projection(bytes: &[u8]) -> serde_json::Result<Option<serde_json::Value>> {
    use std::io::Write;
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(bytes).unwrap();
    let mapped = optional_projected_record_file(&mut file, bytes.len() as u64);
    let streamed = projected_record_file(&mut file, bytes.len() as u64, 0);
    assert_eq!(
        mapped.as_ref().map_err(ToString::to_string),
        streamed.as_ref().map_err(ToString::to_string)
    );
    mapped
}

#[test]
fn oversized_plain_strings_preserve_retained_values_and_duplicate_keys() {
    let big = "x".repeat((1 << 20) + 13);
    for body in [
        format!(
            r#"{{"type":"message","message":{{"role":"toolResult","content":"ok","details":{{"padding":"{big}"}}}}}}"#
        ),
        format!(r#"{{"type":"message","message":{{"role":"user","content":"{big}"}}}}"#),
        format!(
            r#"{{"type":"message","message":{{"content":[{{"type":"toolCall","arguments":{{"{big}":1,"keep":3,"{big}":2}}}}]}}}}"#
        ),
    ] {
        let expected = projected_record(&body).unwrap();
        let actual = file_projection(body.as_bytes()).unwrap().unwrap();
        assert_eq!(
            serde_json::to_vec(&actual).unwrap(),
            serde_json::to_vec(&expected).unwrap()
        );
    }
}

#[test]
fn oversized_reader_fallback_preserves_text_and_error_positions() {
    let big = "x".repeat((1 << 20) + 13);
    let bodies = [
        format!(r#"{{"message":{{"content":"{}"}}}}"#, "é".repeat(1 << 20)).into_bytes(),
        format!(r#"{{"message":{{"content":"\u0000agrep_raw:0:999","details":"{big}"}}}}"#)
            .into_bytes(),
        format!(
            r#"{{"message":{{"content":"{}","details":"{big}"}}}}"#,
            "line\\n".repeat(1 << 18)
        )
        .into_bytes(),
        format!(r#"{{"discard":"{big}","bad":1e9999}}"#).into_bytes(),
        format!(r#"{{"discard":"{big}","bad":"\ud800"}}"#).into_bytes(),
        format!(r#"{{"discard":"{big}" trailing}}"#).into_bytes(),
    ];
    for body in bodies {
        let expected = optional_projected_record_reader(body.as_slice());
        let actual = file_projection(&body);
        match (actual, expected) {
            (Ok(actual), Ok(expected)) => assert_eq!(actual, expected),
            (Err(actual), Err(expected)) => {
                assert_eq!(actual.to_string(), expected.to_string())
            }
            pair => panic!("different parse outcomes: {pair:?}"),
        }
    }
    let mut invalid = format!(r#"{{"message":{{"content":"{big}"}}}}"#).into_bytes();
    let mut control = invalid.clone();
    control[100] = 1;
    assert_eq!(
        file_projection(&control).unwrap_err().to_string(),
        optional_projected_record_reader(control.as_slice())
            .unwrap_err()
            .to_string()
    );
    invalid[100] = 0xff;
    assert_eq!(
        file_projection(&invalid).unwrap(),
        optional_projected_record_reader(invalid.as_slice()).unwrap()
    );
}

#[test]
fn streamed_blank_records_match_trim_and_do_not_hide_late_errors() {
    for blank in [" \t\r\n", "\u{a0}\u{2003}\n"] {
        assert!(optional_projected_record_reader(blank.as_bytes())
            .unwrap()
            .is_none());
        let mut late_error = blank.repeat(64 << 10);
        late_error.push_str("not JSON");
        assert!(optional_projected_record_reader(late_error.as_bytes()).is_err());
    }
}

#[test]
fn projection_preserves_compacted_values_and_key_order() {
    for body in [
        r#"{"discard":{"nested":[null,true,2.5,"text"]},"type":"message","id":"r","message":{"role":"toolResult","details":{"large":"ignored"},"content":[{"type":"text","text":"first"},{"type":"image","data":"ignored"},{"type":"text","text":"last"}],"isError":false}}"#,
        r#"{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"reply"},{"type":"thinking","thinking":"ignored"},{"type":"toolCall","id":"c","name":"bash","arguments":{"command":"true"}}]},"id":"first","discard":0,"id":"last"}"#,
        r#"{"type":"session","id":"s","message":null,"timestamp":18446744073709551615,"discard":-0.0}"#,
        r#"{"type":"custom","message":[{"details":1}],"summary":"kept","\u0064iscard":"ignored"}"#,
        r#"{"type":"message","message":{"role":"toolResult","content":"kept","__agrep_chars":4,"__agrep_bytes":4,"details":{}}}"#,
    ] {
        let expected = compact_record(serde_json::from_str(body).unwrap());
        let actual = compact_record(projected_record(body).unwrap());
        assert_eq!(
            compact_record(projected_record_reader(body.as_bytes()).unwrap()),
            expected
        );
        assert_eq!(
            serde_json::to_vec(&actual).unwrap(),
            serde_json::to_vec(&expected).unwrap()
        );
    }
}

#[test]
fn discarded_fields_keep_json_validation_and_depth_limits() {
    for body in [
        r#"{"discard":1e9999}"#.to_owned(),
        r#"{"discard":"\ud800"}"#.to_owned(),
        r#"{"discard":[1,]}"#.to_owned(),
        r#"{"discard":true} trailing"#.to_owned(),
    ] {
        assert!(serde_json::from_str::<serde_json::Value>(&body).is_err());
        assert!(projected_record(&body).is_err());
        assert!(validate_record_reader(body.as_bytes()).is_err());
        assert!(projected_record_reader(body.as_bytes()).is_err());
    }
    for depth in 1..=130 {
        let body = format!(
            "{{\"discard\":{}0{}}}",
            "[".repeat(depth),
            "]".repeat(depth)
        );
        assert_eq!(
            projected_record(&body).is_ok(),
            serde_json::from_str::<serde_json::Value>(&body).is_ok(),
            "depth {depth}"
        );
    }
}
