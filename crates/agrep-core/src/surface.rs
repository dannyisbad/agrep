//! Small output primitives; all byte budgets include the final newline.
//!
//! A reader that goes away (`agrep setup | head -1`) ends the output, not the command:
//! stdout writes drop a broken pipe and the command finishes with its own exit code.
//! Commands print through these helpers and the `out!`/`outln!`/`errln!` macros, never
//! `print!`/`println!`/`eprintln!`, whose failed writes panic.
use anyhow::Result;
use std::fmt;
use std::io::{self, Write};
pub fn write(value: &str) -> Result<()> {
    let mut out = io::stdout().lock();
    Ok(drop_closed(
        out.write_all(value.as_bytes()).and_then(|_| out.flush()),
    )?)
}
pub fn line(value: &str) -> Result<()> {
    write(&format!("{value}\n"))
}
/// `print!` for the `out!` and `outln!` macros.
pub fn print(args: fmt::Arguments<'_>) -> io::Result<()> {
    drop_closed(io::stdout().lock().write_fmt(args))
}
/// `eprint!` for the `errln!` macro: with stderr gone there is no one left to tell.
pub fn note(args: fmt::Arguments<'_>) {
    let _ = io::stderr().lock().write_fmt(args);
}
/// The reader of this stream has gone away.
pub fn closed(e: &io::Error) -> bool {
    e.kind() == io::ErrorKind::BrokenPipe
}
/// Puts a question to the reader of `out`; false when that reader has gone away, so a
/// command never waits on the answer to a prompt no one saw.
pub fn ask(out: &mut impl Write, args: fmt::Arguments<'_>) -> io::Result<bool> {
    match out.write_fmt(args).and_then(|()| out.flush()) {
        Err(e) if closed(&e) => Ok(false),
        other => other.map(|()| true),
    }
}
fn drop_closed(result: io::Result<()>) -> io::Result<()> {
    match result {
        Err(e) if closed(&e) => Ok(()),
        other => other,
    }
}
/// `print!` to stdout that returns `io::Result<()>`, treating a closed reader as success.
#[macro_export]
macro_rules! out {
    ($($arg:tt)*) => {
        $crate::surface::print(::std::format_args!($($arg)*))
    };
}
/// `println!` to stdout that returns `io::Result<()>`, treating a closed reader as success.
#[macro_export]
macro_rules! outln {
    () => {
        $crate::surface::print(::std::format_args!("\n"))
    };
    ($($arg:tt)*) => {
        $crate::surface::print(::std::format_args!("{}\n", ::std::format_args!($($arg)*)))
    };
}
/// `eprintln!` that never fails.
#[macro_export]
macro_rules! errln {
    () => {
        $crate::surface::note(::std::format_args!("\n"))
    };
    ($($arg:tt)*) => {
        $crate::surface::note(::std::format_args!("{}\n", ::std::format_args!($($arg)*)))
    };
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
/// 0.4.9's one colour rule (py/console.py:175-191): an explicit `--color always|never`
/// wins, then a non-empty `NO_COLOR` disables colour, then a non-empty `FORCE_COLOR`
/// forces it, and otherwise colour follows whether the stream is a terminal.
pub fn color_enabled(when: &str, terminal: bool) -> bool {
    let set = |name| std::env::var_os(name).is_some_and(|v| !v.is_empty());
    color_rule(when, set("NO_COLOR"), set("FORCE_COLOR"), terminal)
}
fn color_rule(when: &str, no_color: bool, force_color: bool, terminal: bool) -> bool {
    match when {
        "always" => true,
        "never" => false,
        _ => !no_color && (force_color || terminal),
    }
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
    // 0.4.9's test_color_rule.py cases.
    #[test]
    fn explicit_color_then_no_color_then_force_color_then_the_terminal() {
        for (when, no_color, force_color, terminal, expected) in [
            ("always", true, false, false, true),
            ("never", false, true, true, false),
            ("auto", true, true, true, false),
            ("auto", true, false, true, false),
            ("auto", false, true, false, true),
            ("auto", false, false, true, true),
            ("auto", false, false, false, false),
        ] {
            assert_eq!(
                color_rule(when, no_color, force_color, terminal),
                expected,
                "{when} NO_COLOR={no_color} FORCE_COLOR={force_color} tty={terminal}"
            );
        }
    }
}
