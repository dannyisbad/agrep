//! agrep's hook entry in a client's JSON settings. Setup and remove change only that entry
//! and the containers setup had to add for it; every other byte keeps its text. New text
//! takes the file's style: indentation, line endings, separators and `\u` escaping.
use super::Added;
use anyhow::Result;
use serde::{de, Deserialize, Serialize};
use serde_json::{json, value::RawValue, Value};
use std::ops::Range;

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
/// its indentation unit and line endings, spaced separators on one line when it has `": `,
/// and `\u` escapes when it holds one.
struct Style<'a> {
    eol: &'a str,
    indent: &'a str,
    one_line: bool,
    spaced: bool,
    escaped: bool,
}
/// How a container's members are laid out: on its own lines, or inline.
enum Layout {
    Inline { spaced: bool },
    Lines { child: String, close: String },
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
    /// The layout of the container at `at`: an empty one follows the file, a filled one
    /// keeps its own lines, separators and child indentation.
    fn layout(&self, raw: &str, at: &Range<usize>, items: &[Item]) -> Layout {
        let text = &raw[at.clone()];
        if !text.contains('\n') && !(items.is_empty() && !self.one_line) {
            let spaced = if text.contains(':') || text.contains(',') {
                text.contains("\": ") || text.contains(", ")
            } else {
                self.spaced
            };
            return Layout::Inline { spaced };
        }
        let close = line_indent(raw, at.start).to_owned();
        let child = items
            .last()
            .and_then(|item| own_line_indent(raw, item.start))
            .map_or_else(|| format!("{close}{}", self.indent), str::to_owned);
        Layout::Lines { child, close }
    }
    fn render(&self, value: &Value, layout: &Layout) -> Result<String> {
        let mut bytes = Vec::new();
        match layout {
            Layout::Inline { spaced: true } => value.serialize(
                &mut serde_json::Serializer::with_formatter(&mut bytes, Spaced),
            )?,
            Layout::Inline { spaced: false } => {
                value.serialize(&mut serde_json::Serializer::new(&mut bytes))?
            }
            Layout::Lines { .. } => {
                value.serialize(&mut serde_json::Serializer::with_formatter(
                    &mut bytes,
                    serde_json::ser::PrettyFormatter::with_indent(self.indent.as_bytes()),
                ))?
            }
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
        Ok(match layout {
            Layout::Inline { .. } => text,
            Layout::Lines { child, .. } => text.replace('\n', &format!("{}{child}", self.eol)),
        })
    }
    /// One new member or element in `layout`.
    fn piece(&self, key: Option<&str>, value: &Value, layout: &Layout) -> Result<String> {
        let value = self.render(value, layout)?;
        Ok(match key {
            None => value,
            Some(key) => {
                let colon = match layout {
                    Layout::Inline { spaced: false } => ":",
                    _ => ": ",
                };
                format!("{}{colon}{value}", self.render(&json!(key), layout)?)
            }
        })
    }
}
/// The indentation of the line holding `at`.
fn line_indent(raw: &str, at: usize) -> &str {
    let start = raw[..at].rfind('\n').map_or(0, |i| i + 1);
    let line = &raw[start..];
    &line[..line.len() - line.trim_start_matches([' ', '\t']).len()]
}
/// The indentation before `at` when nothing else precedes it on its line.
fn own_line_indent(raw: &str, at: usize) -> Option<&str> {
    let start = raw[..at].rfind('\n').map_or(0, |i| i + 1);
    let before = &raw[start..at];
    before
        .trim_start_matches([' ', '\t'])
        .is_empty()
        .then_some(before)
}

/// A member or element of a container: `start` is a member's key, `value` its value.
struct Item {
    key: Option<String>,
    start: usize,
    value: Range<usize>,
}
/// An object's members in file order, duplicates included.
struct Members<'a>(Vec<(String, &'a RawValue)>);
impl<'de> Deserialize<'de> for Members<'de> {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visit;
        impl<'de> de::Visitor<'de> for Visit {
            type Value = Members<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: de::MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut members = Vec::new();
                while let Some(member) = map.next_entry::<String, &'de RawValue>()? {
                    members.push(member);
                }
                Ok(Members(members))
            }
        }
        d.deserialize_map(Visit)
    }
}
fn span(raw: &str, v: &RawValue) -> Range<usize> {
    let start = v.get().as_ptr() as usize - raw.as_ptr() as usize;
    start..start + v.get().len()
}
/// The items of the object or array at `at`.
fn items(raw: &str, at: &Range<usize>) -> Result<Vec<Item>> {
    let text = &raw[at.clone()];
    if !text.starts_with('{') {
        let elements: Vec<&RawValue> = serde_json::from_str(text)?;
        return Ok(elements
            .into_iter()
            .map(|v| {
                let value = span(raw, v);
                Item {
                    key: None,
                    start: value.start,
                    value,
                }
            })
            .collect());
    }
    let Members(members) = serde_json::from_str(text)?;
    let mut after = at.start + 1;
    Ok(members
        .into_iter()
        .map(|(key, v)| {
            let gap = raw[after..].find(|c| !matches!(c, ' ' | '\t' | '\n' | '\r' | ','));
            let start = after + gap.expect("a member follows");
            let value = span(raw, v);
            after = value.end;
            Item {
                key: Some(key),
                start,
                value,
            }
        })
        .collect())
}
/// The value a JSON reader sees for `key`: the last member's, when it is of `kind` (`{` or
/// `[`).
fn member(items: &[Item], raw: &str, key: &str, kind: char) -> Option<Range<usize>> {
    items
        .iter()
        .rfind(|item| item.key.as_deref() == Some(key))
        .map(|item| item.value.clone())
        .filter(|value| raw[value.clone()].starts_with(kind))
}
fn top(raw: &str) -> Result<Range<usize>> {
    Ok(span(raw, serde_json::from_str::<&RawValue>(raw)?))
}

/// `raw` with a new last item in the container at `at`.
fn append(
    raw: &str,
    style: &Style,
    at: &Range<usize>,
    key: Option<&str>,
    value: &Value,
) -> Result<String> {
    let items = items(raw, at)?;
    let layout = style.layout(raw, at, &items);
    let piece = style.piece(key, value, &layout)?;
    let mut out = raw.to_owned();
    match (items.last(), &layout) {
        (Some(last), Layout::Inline { spaced }) => {
            let gap = if *spaced { " " } else { "" };
            out.insert_str(last.value.end, &format!(",{gap}{piece}"));
        }
        (Some(last), Layout::Lines { child, .. }) => {
            out.insert_str(last.value.end, &format!(",{}{child}{piece}", style.eol));
        }
        (None, Layout::Inline { .. }) => out.replace_range(at.start + 1..at.end - 1, &piece),
        (None, Layout::Lines { child, close }) => out.replace_range(
            at.start + 1..at.end - 1,
            &format!("{eol}{child}{piece}{eol}{close}", eol = style.eol),
        ),
    }
    Ok(out)
}
/// `raw` without item `i` of the container at `at`: with the separator before it, or for
/// a first item the one after it. A container left empty is written `{}` or `[]`.
fn cut(raw: &str, at: &Range<usize>, items: &[Item], i: usize) -> String {
    let range = match (i.checked_sub(1), items.get(i + 1)) {
        (Some(previous), _) => items[previous].value.end..items[i].value.end,
        (None, Some(next)) => items[i].start..next.start,
        (None, None) => at.start + 1..at.end - 1,
    };
    let mut out = raw.to_owned();
    out.replace_range(range, "");
    out
}

/// `raw` with `entry` added to `hooks.<event>` unless it is there, and the outermost
/// member that was missing for it. The caller has checked the file's shape.
pub(super) fn add(raw: &str, event: &str, entry: &Value) -> Result<(String, Added)> {
    if raw.trim().is_empty() {
        let value = json!({"hooks": {event: [entry]}});
        return Ok((serde_json::to_string_pretty(&value)? + "\n", Added::Hooks));
    }
    let style = Style::of(raw);
    let (text, added) = match locate(raw, event)? {
        [Some(object), None, _] => {
            let hooks = json!({event: [entry]});
            let text = append(raw, &style, &object, Some("hooks"), &hooks)?;
            (text, Added::Hooks)
        }
        [_, Some(hooks), None] => {
            let text = append(raw, &style, &hooks, Some(event), &json!([entry]))?;
            (text, Added::Event)
        }
        [_, _, Some(entries)] if position(raw, &items(raw, &entries)?, entry).is_none() => {
            let text = append(raw, &style, &entries, None, entry)?;
            (text, Added::Entry)
        }
        _ => (raw.to_owned(), Added::Entry),
    };
    Ok((text, added))
}
fn position(raw: &str, items: &[Item], entry: &Value) -> Option<usize> {
    items.iter().position(|item| {
        serde_json::from_str::<Value>(&raw[item.value.clone()]).is_ok_and(|v| v == *entry)
    })
}

/// `raw` without any `entry` in `hooks.<event>`, and without the containers `added` says
/// setup created for it once they are empty.
pub(super) fn remove(raw: &str, event: &str, entry: &Value, added: Added) -> Result<String> {
    let mut text = raw.to_owned();
    let mut removed = false;
    while let [_, Some(hooks), Some(entries)] = locate(&text, event)? {
        let elements = items(&text, &entries)?;
        if let Some(i) = position(&text, &elements, entry) {
            text = cut(&text, &entries, &elements, i);
            removed = true;
            continue;
        }
        if removed && elements.is_empty() && added != Added::Entry {
            let events = items(&text, &hooks)?;
            text = cut(&text, &hooks, &events, last_index(&events, event));
            if added == Added::Hooks && events.len() == 1 {
                let object = top(&text)?;
                let members = items(&text, &object)?;
                text = cut(&text, &object, &members, last_index(&members, "hooks"));
            }
        }
        break;
    }
    Ok(text)
}
fn last_index(items: &[Item], key: &str) -> usize {
    items
        .iter()
        .rposition(|item| item.key.as_deref() == Some(key))
        .expect("the member was found")
}
/// The top-level object, its `hooks` object and that object's `event` array, as far as the
/// file has them.
fn locate(raw: &str, event: &str) -> Result<[Option<Range<usize>>; 3]> {
    let object = top(raw)?;
    if !raw[object.clone()].starts_with('{') {
        return Ok([None, None, None]);
    }
    let hooks = member(&items(raw, &object)?, raw, "hooks", '{');
    let entries = match &hooks {
        Some(hooks) => member(&items(raw, hooks)?, raw, event, '['),
        None => None,
    };
    Ok([Some(object), hooks, entries])
}

#[cfg(test)]
mod tests {
    use super::*;

    // Each expectation is what 0.4.9's `_render_settings` wrote for the same file and value.
    #[test]
    fn hooks_take_the_files_separators_and_escapes() {
        let entry = json!({"command": "café"});
        for (old, want) in [
            (
                r#"{"model": "opus"}"#,
                r#"{"model": "opus", "hooks": {"Stop": [{"command": "café"}]}}"#,
            ),
            (
                r#"{"model":"opus"}"#,
                r#"{"model":"opus","hooks":{"Stop":[{"command":"café"}]}}"#,
            ),
            (
                "{\n  \"a\": \"\\u00e9\"\n}\n",
                "{\n  \"a\": \"\\u00e9\",\n  \"hooks\": {\n    \"Stop\": [\n      {\n        \"command\": \"caf\\u00e9\"\n      }\n    ]\n  }\n}\n",
            ),
        ] {
            let (text, added) = add(old, "Stop", &entry).unwrap();
            assert_eq!(text, want);
            assert_eq!(added, Added::Hooks);
            assert_eq!(remove(&text, "Stop", &entry, added).unwrap(), old);
        }
    }

    // A new entry follows its neighbours' layout: inline beside inline ones, on its own
    // lines at their indentation beside pretty ones, and in the file's style in a new or
    // empty container.
    #[test]
    fn an_entry_takes_its_containers_layout_and_remove_takes_it_back_out() {
        let entry = json!({"command": "agrep"});
        for (old, want, added) in [
            (
                "{\n  \"hooks\": {\n    \"Stop\": [{\"command\": \"user\"}]\n  }\n}\n",
                "{\n  \"hooks\": {\n    \"Stop\": [{\"command\": \"user\"}, {\"command\": \"agrep\"}]\n  }\n}\n",
                Added::Entry,
            ),
            (
                "{\n\t\"hooks\": {\n\t\t\"Stop\": [\n\t\t\t{\"command\": \"user\"}\n\t\t]\n\t}\n}",
                "{\n\t\"hooks\": {\n\t\t\"Stop\": [\n\t\t\t{\"command\": \"user\"},\n\t\t\t{\n\t\t\t\t\"command\": \"agrep\"\n\t\t\t}\n\t\t]\n\t}\n}",
                Added::Entry,
            ),
            (
                "{\r\n  \"hooks\": {\r\n    \"Other\": []\r\n  }\r\n}\r\n",
                "{\r\n  \"hooks\": {\r\n    \"Other\": [],\r\n    \"Stop\": [\r\n      {\r\n        \"command\": \"agrep\"\r\n      }\r\n    ]\r\n  }\r\n}\r\n",
                Added::Event,
            ),
            (
                "{\n  \"hooks\": {}\n}\n",
                "{\n  \"hooks\": {\n    \"Stop\": [\n      {\n        \"command\": \"agrep\"\n      }\n    ]\n  }\n}\n",
                Added::Event,
            ),
            (
                r#"{"hooks":{"Stop":[]}}"#,
                r#"{"hooks":{"Stop":[{"command":"agrep"}]}}"#,
                Added::Entry,
            ),
            (
                "{}\n",
                "{\n  \"hooks\": {\n    \"Stop\": [\n      {\n        \"command\": \"agrep\"\n      }\n    ]\n  }\n}\n",
                Added::Hooks,
            ),
        ] {
            assert_eq!(add(old, "Stop", &entry).unwrap(), (want.into(), added));
            assert_eq!(add(want, "Stop", &entry).unwrap(), (want.into(), Added::Entry));
            assert_eq!(remove(want, "Stop", &entry, added).unwrap(), old);
        }
    }

    // What the user wrote after setup stays: their entry beside agrep's, and a container
    // setup added that now holds their entry.
    #[test]
    fn remove_keeps_what_the_user_added_after_setup() {
        let entry = json!({"command": "agrep"});
        let (installed, added) = add(r#"{"model": "x"}"#, "Stop", &entry).unwrap();
        let edited = installed.replace(
            r#"{"command": "agrep"}"#,
            r#"{"command": "agrep"}, {"command": "user"}"#,
        );
        assert_eq!(
            remove(&edited, "Stop", &entry, added).unwrap(),
            r#"{"model": "x", "hooks": {"Stop": [{"command": "user"}]}}"#
        );
        let edited = installed.replace(
            r#"{"command": "agrep"}"#,
            r#"{"command": "user"}, {"command": "agrep"}"#,
        );
        assert_eq!(
            remove(&edited, "Stop", &entry, added).unwrap(),
            r#"{"model": "x", "hooks": {"Stop": [{"command": "user"}]}}"#
        );
        let theirs = r#"{"hooks": {"Stop": [{"command": "agrep"}], "Other": [1]}}"#;
        assert_eq!(
            remove(theirs, "Stop", &entry, Added::Hooks).unwrap(),
            r#"{"hooks": {"Other": [1]}}"#
        );
    }
}
