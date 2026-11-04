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
/// Public indexing statistics omit internal lock coordination; `extra`'s fields follow.
pub fn refresh_json(refresh: &crate::store::Refresh, extra: serde_json::Value) -> Result<String> {
    let mut value = serde_json::to_value(refresh)?;
    if let Some(fields) = value.as_object_mut() {
        fields.shift_remove("skipped_lock");
        if let serde_json::Value::Object(extra) = extra {
            fields.extend(extra);
        }
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

/// 0.4.9's `terminal_safe`: C0, DEL, C1 and the other format, surrogate and line or
/// paragraph separator characters print as `\uXXXX` (`\UXXXXXXXX` past the BMP), except
/// the two joiners ordinary text needs. Multiline text keeps newlines and expands tabs.
pub fn safe(value: &str, multiline: bool) -> String {
    use std::fmt::Write;
    if value.bytes().all(|b| (b' '..=b'~').contains(&b)) {
        return value.to_owned();
    }
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '\n' if multiline => out.push('\n'),
            '\t' if multiline => out.push_str("    "),
            '\u{200c}' | '\u{200d}' => out.push(c),
            _ if (c.is_control() || is_terminal_format(c)) && (c as u32) > 0xffff => {
                write!(out, "\\U{:08x}", c as u32).expect("writing to a String cannot fail")
            }
            _ if c.is_control() || is_terminal_format(c) => {
                write!(out, "\\u{:04x}", c as u32).expect("writing to a String cannot fail")
            }
            _ => out.push(c),
        }
    }
    out
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
    // Expected strings are agrep 0.4.9's `surface_policy.terminal_safe` output.
    #[test]
    fn shared_sanitizer_quotes_controls_and_formats_as_0_4_9() {
        for (input, multiline, expected) in [
            ("before\u{7f}after", false, r"before\u007fafter"),
            ("before\u{85}after", false, r"before\u0085after"),
            ("before\u{9b}after", false, r"before\u009bafter"),
            ("before\u{202e}after", false, r"before\u202eafter"),
            ("before\u{2066}after", false, r"before\u2066after"),
            ("before\u{200b}after", false, r"before\u200bafter"),
            ("before\u{feff}after", false, r"before\ufeffafter"),
            ("\u{1b}[31m\nx", false, r"\u001b[31m\u000ax"),
            ("a\nb\tc\rd", true, "a\nb    c\\u000dd"),
            ("a\nb\tc", false, r"a\u000ab\u0009c"),
            ("tag\u{e0061}!", false, r"tag\U000e0061!"),
            (
                "emoji\u{200d}\u{200c} ok",
                false,
                "emoji\u{200d}\u{200c} ok",
            ),
            ("line\u{2028}para\u{2029}", true, r"line\u2028para\u2029"),
            ("soft\u{ad}hyphen", false, r"soft\u00adhyphen"),
            ("plain ascii", false, "plain ascii"),
            ("caf\u{e9} \u{754c}", false, "caf\u{e9} \u{754c}"),
        ] {
            assert_eq!(safe(input, multiline), expected, "{input:?}");
            if !multiline {
                assert_eq!(crate::ingest::terminal_safe(input), expected);
            }
        }
    }
    #[test]
    fn bytes_and_terminal() {
        assert_eq!(prefix("é🙂a", 5), "é");
        assert!(clip("é🙂a", 6).len() <= 6);
    }
}
