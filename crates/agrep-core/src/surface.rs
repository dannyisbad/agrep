//! Small output primitives; all byte budgets include the final newline.
use anyhow::Result;
use std::io::{self, Write};
pub fn write(value: &str) -> Result<()> {
    let mut out = io::stdout().lock();
    match out.write_all(value.as_bytes()).and_then(|_| out.flush()) {
        Err(e) if e.kind() == io::ErrorKind::BrokenPipe => Ok(()),
        other => Ok(other?),
    }
}
/// Public indexing statistics omit internal lock coordination.
pub fn refresh_json(refresh: &crate::store::Refresh) -> Result<String> {
    let mut value = serde_json::to_value(refresh)?;
    if let Some(fields) = value.as_object_mut() {
        fields.shift_remove("skipped_lock");
    }
    Ok(crate::json_output::to_string(&value)?)
}
pub fn line(value: &str) -> Result<()> {
    write(&format!("{value}\n"))
}
pub fn prefix(value: &str, bytes: usize) -> &str {
    let mut end = bytes.min(value.len());
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    &value[..end]
}
pub fn clip(value: &str, bytes: usize) -> String {
    if value.len() <= bytes {
        value.into()
    } else if bytes < 3 {
        String::new()
    } else {
        format!("{}…", prefix(value, bytes - 3))
    }
}
pub fn is_terminal_format(char: char) -> bool {
    matches!(char as u32,
        0x00ad | 0x0600..=0x0605 | 0x061c | 0x06dd | 0x070f |
        0x0890..=0x0891 | 0x08e2 | 0x180e | 0x200b..=0x200f |
        0x2028..=0x202e | 0x2060..=0x2064 | 0x2066..=0x206f | 0xfeff |
        0xfff9..=0xfffb | 0x110bd | 0x110cd | 0x13430..=0x1343f |
        0x1bca0..=0x1bca3 | 0x1d173..=0x1d17a | 0xe0001 |
        0xe0020..=0xe007f)
}

pub fn safe(value: &str, multiline: bool) -> String {
    value
        .chars()
        .map(|c| {
            if (c.is_control() && !(multiline && (c == '\n' || c == '\t'))) || is_terminal_format(c)
            {
                ' '
            } else {
                c
            }
        })
        .collect()
}
pub fn shell(args: &[String]) -> String {
    args.iter()
        .map(|s| {
            if !s.is_empty()
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
            {
                s.clone()
            } else {
                format!("'{}'", s.replace('\'', "'\"'\"'"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}
pub fn number(n: usize) -> String {
    let s = n.to_string();
    s.chars()
        .enumerate()
        .flat_map(|(i, c)| {
            if i > 0 && (s.len() - i).is_multiple_of(3) {
                vec![',', c]
            } else {
                vec![c]
            }
        })
        .collect()
}
pub fn timestamp(ts: i64) -> String {
    if ts == 0 {
        return String::new();
    }
    chrono::DateTime::from_timestamp_millis(ts)
        .map(|t| {
            t.with_timezone(&chrono::Local)
                .format("%Y-%m-%d %H:%M")
                .to_string()
        })
        .unwrap_or_default()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_sanitizer_neutralizes_controls_and_bidi_formats() {
        for c in [
            '\u{7f}', '\u{85}', '\u{9b}', '\u{202e}', '\u{2066}', '\u{200b}', '\u{feff}',
        ] {
            let input = format!("before{c}after");
            assert_eq!(safe(&input, false), "before after");
            assert_eq!(crate::ingest::terminal_safe(&input), safe(&input, false));
        }
        assert_eq!(safe("a\nb\tc", true), "a\nb\tc");
    }
    #[test]
    fn bytes_and_terminal() {
        assert_eq!(prefix("é🙂a", 5), "é");
        assert!(clip("é🙂a", 6).len() <= 6);
        assert_eq!(safe("\u{1b}[31m\nx", false), " [31m x");
    }
}
