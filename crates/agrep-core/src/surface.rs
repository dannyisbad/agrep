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
pub fn safe(value: &str, multiline: bool) -> String {
    value
        .chars()
        .map(|c| {
            if (c.is_control() && !(multiline && (c == '\n' || c == '\t')))
                || matches!(c, '\u{2028}' | '\u{2029}')
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
    fn bytes_and_terminal() {
        assert_eq!(prefix("é🙂a", 5), "é");
        assert!(clip("é🙂a", 6).len() <= 6);
        assert_eq!(safe("\u{1b}[31m\nx", false), " [31m x");
    }
}
