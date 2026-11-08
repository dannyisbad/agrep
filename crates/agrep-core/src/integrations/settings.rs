//! Writing hook entries into a client's JSON settings in the file's own style.
use anyhow::Result;
use serde::Serialize;
use serde_json::{value::RawValue, Value};
use std::{collections::BTreeMap, ops::Range};

/// 0.4.9's spaced one-line separators (`, ` and `: `), which serde_json's compact
/// formatter lacks.
struct Spaced;
impl serde_json::ser::Formatter for Spaced {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        w.write_all(if first { b"" } else { b", " })
    }
    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        w.write_all(if first { b"" } else { b", " })
    }
    fn begin_object_value<W: ?Sized + std::io::Write>(&mut self, w: &mut W) -> std::io::Result<()> {
        w.write_all(b": ")
    }
}
/// A settings file's style as 0.4.9's `_render_settings` read it (py/hookinstall.py:325-342):
/// its indentation and line endings, spaced separators on one line when it has `": `, and
/// `\u` escapes when it holds one.
struct Style<'a> {
    eol: &'a str,
    indent: &'a str,
    one_line: bool,
    spaced: bool,
    escaped: bool,
}
impl<'a> Style<'a> {
    fn of(raw: &'a str) -> Self {
        let indent = raw
            .lines()
            .skip(1)
            .find_map(|line| {
                let trimmed = line.trim_start_matches([' ', '\t']);
                trimmed
                    .starts_with('"')
                    .then_some(&line[..line.len() - trimmed.len()])
            })
            .unwrap_or("  ");
        let one_line = !raw.contains('\n');
        Self {
            eol: if raw.contains("\r\n") { "\r\n" } else { "\n" },
            indent,
            one_line,
            spaced: !one_line || raw.contains("\": "),
            escaped: raw.contains("\\u"),
        }
    }
    fn render(&self, hooks: &Value) -> Result<String> {
        let mut bytes = Vec::new();
        if self.one_line && self.spaced {
            hooks.serialize(&mut serde_json::Serializer::with_formatter(
                &mut bytes, Spaced,
            ))?;
        } else if self.one_line {
            hooks.serialize(&mut serde_json::Serializer::new(&mut bytes))?;
        } else {
            hooks.serialize(&mut serde_json::Serializer::with_formatter(
                &mut bytes,
                serde_json::ser::PrettyFormatter::with_indent(self.indent.as_bytes()),
            ))?;
        }
        let mut text = String::from_utf8(bytes)?;
        if self.escaped {
            text = text
                .chars()
                .map(|c| {
                    if c.is_ascii() {
                        c.to_string()
                    } else {
                        c.encode_utf16(&mut [0; 2])
                            .iter()
                            .map(|unit| format!("\\u{unit:04x}"))
                            .collect()
                    }
                })
                .collect();
        }
        Ok(if self.one_line {
            text
        } else {
            text.replace('\n', &format!("{}{}", self.eol, self.indent))
        })
    }
}
fn span(raw: &str, v: &RawValue) -> Range<usize> {
    let start = v.get().as_ptr() as usize - raw.as_ptr() as usize;
    start..start + v.get().len()
}
/// Replace only the top-level hooks value. Other user JSON stays byte-for-byte; the hooks
/// take the file's style.
pub(super) fn render_json(value: &Value, old: &[u8]) -> Result<Vec<u8>> {
    let raw = std::str::from_utf8(old)?;
    if raw.trim().is_empty() {
        return Ok((serde_json::to_string_pretty(value)? + "\n").into_bytes());
    }
    let fields: BTreeMap<String, &RawValue> = serde_json::from_str(raw)?;
    let style = Style::of(raw);
    let mut result = raw.to_owned();
    match (fields.get("hooks"), value.get("hooks")) {
        (Some(previous), Some(hooks)) => {
            result.replace_range(span(raw, previous), &style.render(hooks)?);
        }
        (Some(previous), None) => {
            result.replace_range(removed(raw, &fields, span(raw, previous)), "");
        }
        (None, Some(hooks)) => {
            let last = fields.values().map(|v| span(raw, v).end).max();
            let start = last.unwrap_or_else(|| raw.find('{').unwrap() + 1);
            let gap = if !style.one_line {
                format!("{}{}", style.eol, style.indent)
            } else if style.spaced && last.is_some() {
                " ".into()
            } else {
                String::new()
            };
            result.insert_str(
                start,
                &format!(
                    "{}{gap}\"hooks\"{}{}",
                    if last.is_some() { "," } else { "" },
                    if style.spaced { ": " } else { ":" },
                    style.render(hooks)?
                ),
            );
        }
        (None, None) => {}
    }
    serde_json::from_str::<Value>(&result)?;
    Ok(result.into_bytes())
}
/// The bytes that removing the hooks member deletes: from the end of the member before
/// it, or for a first member through its trailing comma.
fn removed(raw: &str, fields: &BTreeMap<String, &RawValue>, current: Range<usize>) -> Range<usize> {
    let previous_end = fields
        .iter()
        .filter(|(key, _)| key.as_str() != "hooks")
        .map(|(_, v)| span(raw, v).end)
        .filter(|end| *end < current.start)
        .max();
    let start = previous_end.unwrap_or_else(|| raw.find('{').unwrap() + 1);
    let mut end = current.end;
    if previous_end.is_none() {
        let tail = &raw[end..];
        if let Some(comma) = tail.find(',').filter(|i| tail[..*i].trim().is_empty()) {
            end += comma + 1;
        }
    }
    start..end
}
