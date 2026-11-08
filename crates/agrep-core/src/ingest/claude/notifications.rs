//! Task notifications: Claude Code's `<task-notification>` deliveries, either as an
//! origin-tagged user row or as a queued-command attachment, recorded as control markers.

use std::borrow::Cow;

use serde::Deserialize;
use serde_json::value::RawValue;

use super::records::Line;
use super::TASK_NOTIFICATION_MARKER;
use crate::model::Event;

/// A `queued_command` attachment: a prompt queued while a turn ran and delivered into it.
#[derive(Deserialize)]
struct QueuedCommand<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(rename = "commandMode", borrow)]
    command_mode: Option<Cow<'a, str>>,
    #[serde(borrow)]
    origin: Option<&'a RawValue>,
    #[serde(borrow)]
    prompt: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Origin<'a> {
    #[serde(borrow)]
    kind: Option<Cow<'a, str>>,
}

pub(super) fn is_task_notification_row(line: &Line) -> bool {
    line.origin
        .and_then(|raw| serde_json::from_str::<Origin>(raw.get()).ok())
        .is_some_and(|origin| origin.kind.as_deref() == Some("task-notification"))
}

/// The prompt of a queued-command attachment carrying a task notification, the mid-turn form of
/// an origin-tagged user row. As in Claude Code, a set origin decides; only a missing or null
/// origin falls back to `commandMode`.
pub(super) fn queued_notification_prompt(
    attachment: Option<&RawValue>,
) -> Option<serde_json::Value> {
    let queued: QueuedCommand = serde_json::from_str(attachment?.get()).ok()?;
    if queued.ty.as_deref() != Some("queued_command") {
        return None;
    }
    let kind = match queued.origin {
        Some(raw) => serde_json::from_str::<Origin>(raw.get()).ok()?.kind,
        None => queued.command_mode,
    };
    if kind.as_deref() != Some("task-notification") {
        return None;
    }
    Some(
        queued
            .prompt
            .and_then(|raw| serde_json::from_str(raw.get()).ok())
            .unwrap_or_default(),
    )
}

/// One `<task-notification>` block's leading header tags. Claude Code writes `<task-id>`,
/// `<tool-use-id>`, `<task-type>`, `<output-file>`, `<status>`, `<summary>` (each optional, in
/// that order) before any body, so only that leading run is read: a result body can quote tags.
#[derive(Debug, Default, PartialEq)]
pub(super) struct TaskNotification<'a> {
    task_id: Option<&'a str>,
    tool_use_id: Option<&'a str>,
    status: Option<&'a str>,
}

/// The text blocks of a row's content. Each carries at most one notification, its first
/// opening tag, so a notification quoted inside a result body is never read as another.
fn content_texts(content: &serde_json::Value) -> Vec<&str> {
    match content {
        serde_json::Value::String(text) => vec![text.as_str()],
        serde_json::Value::Array(blocks) => blocks
            .iter()
            .filter(|b| b.get("type").and_then(|t| t.as_str()) == Some("text"))
            .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
            .collect(),
        _ => Vec::new(),
    }
}

fn task_notification(text: &str) -> Option<TaskNotification<'_>> {
    const OPEN: &str = "<task-notification";
    let mut rest = text;
    let after = loop {
        let after = &rest[rest.find(OPEN)? + OPEN.len()..];
        if after.starts_with(|c: char| c == '>' || c.is_whitespace()) {
            break after;
        }
        rest = after;
    };
    let mut cursor = &after[after.find('>')? + 1..];
    let mut found = TaskNotification::default();
    while let Some((tag, value, next)) = leading_tag(cursor) {
        let slot = match tag {
            "task-id" => Some(&mut found.task_id),
            "tool-use-id" => Some(&mut found.tool_use_id),
            "status" => Some(&mut found.status),
            _ => None,
        };
        if let Some(slot) = slot {
            slot.get_or_insert(value);
        }
        cursor = next;
    }
    Some(found)
}

/// `<tag>value</tag>` at the start of `text` (after whitespace), with a markup-free value.
fn leading_tag(text: &str) -> Option<(&str, &str, &str)> {
    let text = text.trim_start().strip_prefix('<')?;
    let end = text.find('>')?;
    let tag = &text[..end];
    if tag.is_empty() || !tag.bytes().all(|b| b.is_ascii_lowercase() || b == b'-') {
        return None;
    }
    let body = &text[end + 1..];
    let stop = body.find('<')?;
    let rest = body[stop..]
        .strip_prefix("</")?
        .strip_prefix(tag)?
        .strip_prefix('>')?;
    Some((tag, &body[..stop], rest))
}

/// A notification field kept in the compact marker input: one bounded token.
pub(super) fn notification_token(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|token| {
        !token.is_empty()
            && token.len() <= 200
            && !token.contains('=')
            && !token.chars().any(char::is_whitespace)
    })
}

/// Claude Code writes a subagent transcript as `agent-<agentId>.jsonl`, and an agent's task id is
/// its agent id: `a` + 16 hex (optionally `a<label>-` + 16 hex) or the older `a` + 7 hex. Every
/// other task kind uses a one-letter prefix plus 8 base-36 chars, which never takes these shapes.
fn agent_task_session(task_id: &str) -> Option<String> {
    let hex = |s: &str| s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
    let rest = task_id.strip_prefix('a')?;
    let plain = matches!(rest.len(), 7 | 16) && hex(rest);
    let labelled = rest.rsplit_once('-').is_some_and(|(label, tail)| {
        (1..=63).contains(&label.len())
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            && tail.len() == 16
            && hex(tail)
    });
    (plain || labelled).then(|| format!("agent-{task_id}"))
}

pub(super) fn notification_blocks(
    content: Option<&serde_json::Value>,
) -> Vec<TaskNotification<'_>> {
    content
        .map(content_texts)
        .unwrap_or_default()
        .into_iter()
        .filter_map(task_notification)
        .collect()
}

const USER: &str = "task-notification";
const QUEUED: &str = "queued-task-notification";

/// One marker per notification block of a row. A block both the user-row content and a queued
/// attachment carry is recorded once, under the user-row call id; a row with no block gets one.
pub(super) fn push_task_notifications(
    events: &mut Vec<Event>,
    tally: &crate::intake::Tally,
    row: (&str, i64, &str),
    user: Option<&[TaskNotification]>,
    queued: Option<&[TaskNotification]>,
) {
    let empty = TaskNotification::default();
    for (form, index, notification) in notification_forms(user, queued, &empty) {
        tally.event();
        events.push(task_notification_event(row, form, index, notification));
    }
}

/// Each block to record with its form and index within that form; `empty` stands in when
/// the row carries none.
fn notification_forms<'n, 'a>(
    user: Option<&'n [TaskNotification<'a>]>,
    queued: Option<&'n [TaskNotification<'a>]>,
    empty: &'n TaskNotification<'a>,
) -> Vec<(&'static str, usize, &'n TaskNotification<'a>)> {
    let user_blocks = user.unwrap_or_default();
    let mut found: Vec<(&str, usize, &TaskNotification)> = user_blocks
        .iter()
        .enumerate()
        .map(|(index, block)| (USER, index, block))
        .collect();
    for (index, block) in queued.unwrap_or_default().iter().enumerate() {
        if !user_blocks.contains(block) {
            found.push((QUEUED, index, block));
        }
    }
    if found.is_empty() {
        found.push((if user.is_some() { USER } else { QUEUED }, 0, empty));
    }
    found
}

fn task_notification_event(
    (session, ts, row_id): (&str, i64, &str),
    form: &str,
    index: usize,
    notification: &TaskNotification,
) -> Event {
    let task_id = notification_token(notification.task_id);
    let fields = [
        ("status", notification_token(notification.status)),
        ("task_id", task_id),
        ("tool_use_id", notification_token(notification.tool_use_id)),
    ];
    let input = fields
        .iter()
        .filter_map(|(key, value)| value.map(|value| format!("{key}={value}")))
        .collect::<Vec<_>>()
        .join(" ");
    let mut call_id = format!("{row_id}:{form}");
    if index > 0 {
        call_id.push_str(&format!(":{index}"));
    }
    Event {
        turn: None,
        agent: "claude",
        session: session.to_string(),
        ts,
        kind: "control",
        name: TASK_NOTIFICATION_MARKER.to_string(),
        input_chars: input.chars().count(),
        input,
        output: String::new(),
        output_chars: 0,
        output_bytes: 0,
        ok: None,
        call_id,
        child_session: task_id.and_then(agent_task_session).unwrap_or_default(),
        meta: String::new(),
        state: String::new(),
    }
}
