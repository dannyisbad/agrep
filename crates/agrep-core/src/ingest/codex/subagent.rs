//! Forked subagent rollouts: who the child and its parent are, and the proven boundary
//! where the child's own transcript starts after its verbatim parent replay.

use std::borrow::Cow;

use serde::Deserialize;
use serde_json::value::RawValue;

use super::records::{extract_text, Payload};

#[derive(Deserialize)]
struct SessionSource<'a> {
    #[serde(borrow)]
    subagent: Option<SubagentSource<'a>>,
}

#[derive(Deserialize)]
struct SubagentSource<'a> {
    #[serde(borrow)]
    thread_spawn: Option<ThreadSpawn<'a>>,
    /// Codex-internal control children use `other:"guardian"` for approval
    /// assessments rather than real delegated work.
    #[serde(borrow)]
    other: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct ThreadSpawn<'a> {
    #[serde(borrow)]
    agent_path: Option<Cow<'a, str>>,
}

#[derive(serde::Serialize, serde::Deserialize)]
pub(super) struct SubagentContext {
    pub(super) agent_path: Option<String>,
    start_turn_id: Option<String>,
    replayed_parent: bool,
    pub(super) parent_session: String,
    pub(super) started: bool,
}

impl SubagentContext {
    /// The child identity named by the rollout's first (own) `session_meta`.
    pub(super) fn from_session_meta(meta: &Payload<'_>) -> Self {
        // Legacy journals store source as a scalar ("cli"/"vscode");
        // newer subagents use an object with thread_spawn metadata.
        let nested_agent_path = subagent_source(meta.source)
            .and_then(|source| source.thread_spawn)
            .and_then(|spawn| spawn.agent_path.map(Cow::into_owned));
        let agent_path = meta
            .agent_path
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or(nested_agent_path.filter(|s| !s.is_empty()));
        // Newer journals name the parent outright; rollouts without the
        // field still learn it from the replayed parent's session_meta.
        let parent_session = meta
            .parent_thread_id
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_default();
        Self {
            agent_path,
            start_turn_id: None,
            replayed_parent: false,
            parent_session,
            started: false,
        }
    }

    /// A later `session_meta` with a different id is the replayed parent's.
    pub(super) fn note_session_meta(&mut self, meta_id: Option<&str>, session_id: &str) {
        if let Some(parent_id) = meta_id.filter(|id| !id.is_empty() && *id != session_id) {
            self.replayed_parent = true;
            self.parent_session = parent_id.to_string();
        }
    }

    /// Path-less subagents get their task as a plain role:user message; UUIDv7 turn ids
    /// survive fork re-timestamping, so the first v7 id newer than the session id marks
    /// the child-owned task boundary (older v7 replay turns and v4 ids are excluded).
    pub(super) fn note_event(&mut self, event: &Payload<'_>, session_id: &str) {
        if self.started || event.ty.as_deref() != Some("task_started") {
            return;
        }
        if let Some(turn_id) = event
            .turn_id
            .as_deref()
            .filter(|turn_id| is_uuid_v7_after(turn_id, session_id))
        {
            self.start_turn_id = Some(turn_id.to_string());
        }
    }

    /// Label for a path-less child's `role:user` prompt carrying `message_turn_id`, or
    /// `None` while that message is not proven to be past the task boundary.
    pub(super) fn prompt_label(&self, message_turn_id: Option<&str>) -> Option<&'static str> {
        let turn_matches = self.started
            // both ids present and equal - None == None is absence matching
            // absence, not proof, and must never open the boundary
            || (message_turn_id.is_some() && message_turn_id == self.start_turn_id.as_deref())
            || (message_turn_id.is_none() && !self.replayed_parent && self.start_turn_id.is_some());
        turn_matches.then_some(if self.started { "message" } else { "task" })
    }
}

fn subagent_source(raw: Option<&RawValue>) -> Option<SubagentSource<'_>> {
    raw.and_then(|value| serde_json::from_str::<SessionSource<'_>>(value.get()).ok())
        .and_then(|source| source.subagent)
}

pub(super) fn is_guardian_source(raw: Option<&RawValue>) -> bool {
    subagent_source(raw)
        .and_then(|subagent| subagent.other)
        .is_some_and(|kind| kind == "guardian")
}

fn is_uuid_v7_after(candidate: &str, earlier: &str) -> bool {
    fn canonical_v7(s: &str) -> bool {
        let b = s.as_bytes();
        b.len() == 36
            && b[8] == b'-'
            && b[13] == b'-'
            && b[18] == b'-'
            && b[23] == b'-'
            && b[14] == b'7'
            && b.iter()
                .enumerate()
                .all(|(i, c)| matches!(i, 8 | 13 | 18 | 23) || c.is_ascii_hexdigit())
    }

    canonical_v7(candidate) && canonical_v7(earlier) && candidate > earlier
}

fn header_value<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    text.lines()
        .find_map(|line| line.strip_prefix(key).map(str::trim))
        .filter(|value| !value.is_empty())
}

pub(super) fn delegated_input(text: &str) -> Option<String> {
    let text = text.trim_start();
    if !text.starts_with("<codex_delegation") && !text.starts_with("<realtime_delegation") {
        return None;
    }
    let (_, rest) = text.split_once("<input>")?;
    let (body, _) = rest.split_once("</input>")?;
    let body = body.trim();
    if body.is_empty() {
        return None;
    }
    // Delegation wrappers XML-escape the task body in older Codex journals.
    Some(
        body.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&apos;", "'")
            .replace("&amp;", "&"),
    )
}

/// Turn an inbound Codex team message into a searchable child-turn anchor. Newer
/// Codex builds encrypt the task body, so retain plaintext payloads when present and
/// otherwise describe only the routing metadata that the rollout actually exposes.
pub(super) fn subagent_anchor(payload: &Payload<'_>, expected_recipient: &str) -> Option<String> {
    if payload.ty.as_deref() != Some("agent_message")
        || payload.recipient.as_deref() != Some(expected_recipient)
    {
        return None;
    }
    let visible = extract_text(payload.content.as_deref()?)?;
    let message_type = header_value(&visible, "Message Type:")?;
    let label = match message_type {
        "NEW_TASK" => "task",
        "MESSAGE" | "FOLLOWUP_TASK" => "message",
        _ => return None,
    };

    if let Some((_, body)) = visible.split_once("Payload:") {
        let body = body.trim();
        if !body.is_empty() {
            return Some(format!("[subagent {label}] {body}"));
        }
    }

    let task = header_value(&visible, "Task name:").unwrap_or(expected_recipient);
    let sender = payload
        .author
        .as_deref()
        .or_else(|| header_value(&visible, "Sender:"));
    Some(match sender {
        Some(sender) => format!("[subagent {label}] {task} (from {sender})"),
        None => format!("[subagent {label}] {task}"),
    })
}

/// The most of a rollout's head `fork_time` reads for its two leading records.
const HEAD_CAP: u64 = 16 << 20;

/// When the rollout at `path` is a fork child that replays its parent's history: the moment
/// it forked, its own `session_meta` timestamp. Ingest keeps the replayed history only under
/// the parent (`SubagentContext`), so the parent's rows up to then are the child's too.
pub fn fork_time(path: &std::path::Path) -> Option<i64> {
    let head =
        crate::ingest::registry::read_regular_snapshot(path, |file| head_records(path, file))
            .ok()
            .flatten()
            .flatten()?;
    let mut records = head.into_iter();
    let own = records.next()?;
    let replayed = records.next();
    let meta = (own["type"] == "session_meta").then_some(&own["payload"])?;
    let id = meta["id"].as_str().filter(|id| !id.is_empty())?;
    let replays = replayed.as_ref().is_some_and(|record| {
        record["type"] == "session_meta"
            && record["payload"]["id"]
                .as_str()
                .is_some_and(|parent| !parent.is_empty() && parent != id)
    });
    let forked = meta["forked_from_id"]
        .as_str()
        .is_some_and(|parent| !parent.is_empty());
    if !(replays || forked) {
        return None;
    }
    let at = crate::ingest::parse_timestamp::rfc3339(
        meta["timestamp"].as_str().or(own["timestamp"].as_str()),
    );
    (at > 0).then_some(at)
}

/// A rollout's leading records, at most two, up to the first that does not parse.
fn head_records(path: &std::path::Path, file: &std::fs::File) -> Option<Vec<serde_json::Value>> {
    use std::io::{BufRead, Read};
    let reader: Box<dyn Read + '_> = if super::discover::is_compressed(path) {
        Box::new(zstd::stream::read::Decoder::new(file).ok()?)
    } else {
        Box::new(file)
    };
    Some(
        std::io::BufReader::new(reader.take(HEAD_CAP))
            .lines()
            .take(2)
            .map_while(|line| serde_json::from_str(&line.ok()?).ok())
            .collect(),
    )
}
