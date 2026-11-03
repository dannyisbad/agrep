//! JSON for terminal-facing output. Escaping changes bytes, never decoded values.
use serde::Serialize;
use serde_json::ser::Formatter;
use std::io::{self, Write};

fn fragment<W: Write + ?Sized>(writer: &mut W, text: &str) -> io::Result<()> {
    let mut start = 0;
    for (at, c) in text.char_indices() {
        if matches!(c, '\u{7f}'..='\u{9f}') || crate::surface::is_terminal_format(c) {
            writer.write_all(&text.as_bytes()[start..at])?;
            let mut utf16 = [0; 2];
            for code in c.encode_utf16(&mut utf16) {
                write!(writer, "\\u{code:04x}")?;
            }
            start = at + c.len_utf8();
        }
    }
    writer.write_all(&text.as_bytes()[start..])
}
struct TerminalJson;
impl Formatter for TerminalJson {
    fn write_string_fragment<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        text: &str,
    ) -> io::Result<()> {
        fragment(writer, text)
    }
    fn write_raw_fragment<W: Write + ?Sized>(
        &mut self,
        writer: &mut W,
        text: &str,
    ) -> io::Result<()> {
        fragment(writer, text)
    }
}
pub fn to_writer<W: Write, T: Serialize + ?Sized>(writer: W, value: &T) -> serde_json::Result<()> {
    value.serialize(&mut serde_json::Serializer::with_formatter(
        writer,
        TerminalJson,
    ))
}
pub fn to_string<T: Serialize + ?Sized>(value: &T) -> serde_json::Result<String> {
    let mut bytes = Vec::new();
    to_writer(&mut bytes, value)?;
    Ok(String::from_utf8(bytes).expect("JSON serializer writes UTF-8"))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn controls_keys_and_supplementary_formats_round_trip_without_raw_controls() {
        let text =
            "nul\0 esc\x1b del\u{7f} c1\u{85}\u{9b} bidi\u{202e} join\u{200d} tag\u{e0061} emoji🙂";
        let value = serde_json::json!({text:[text,{"nested":text}]});
        let encoded = to_string(&value).unwrap();
        assert!(!encoded
            .chars()
            .any(|c| c.is_control() || crate::surface::is_terminal_format(c)));
        assert!(encoded.contains("\\u007f"));
        assert!(encoded.contains("\\u009b"));
        assert!(encoded.contains("\\udb40\\udc61"));
        assert!(encoded.contains('🙂'));
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&encoded).unwrap(),
            value
        );
    }
    #[test]
    fn raw_json_fragments_are_escaped_too() {
        let raw = serde_json::value::RawValue::from_string("{\"text\":\"\u{9b}\u{202e}\"}".into())
            .unwrap();
        let encoded = to_string(&raw).unwrap();
        assert_eq!(encoded, "{\"text\":\"\\u009b\\u202e\"}");
    }
    #[test]
    fn write_errors_remain_errors() {
        struct Closed;
        impl Write for Closed {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        assert_eq!(
            to_writer(Closed, &"x").unwrap_err().io_error_kind(),
            Some(io::ErrorKind::BrokenPipe)
        );
    }
}
