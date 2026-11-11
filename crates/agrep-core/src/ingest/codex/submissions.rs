//! The rollout's `event_msg` submission log: the only proof that composed `role:user`
//! input was typed by a human, in legacy and Desktop schemas.

use std::borrow::Cow;

use memchr::memmem;
use serde::Deserialize;

use super::records::Block;
use crate::model::RawMessage;

#[derive(Deserialize)]
struct SubmissionLine<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    payload: Option<SubmissionPayload<'a>>,
}

/// Legacy `message` values must be strings; object-valued event messages are not submissions.
#[derive(Deserialize)]
struct SubmissionPayload<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Cow<'a, str>>,
    #[serde(borrow)]
    thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    item: Option<SubmissionItem<'a>>,
}

#[derive(Deserialize)]
struct SubmissionItem<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<Vec<Block<'a>>>,
}

/// Only native submission events attest composed `role:user` input as human prose.
#[derive(Default, serde::Serialize, serde::Deserialize)]
pub(super) struct Submissions {
    /// Sorted and deduped, so `attests` binary-searches instead of scanning.
    texts: Vec<String>,
    /// Desktop text is scoped to its thread and turn so routed copies cannot attest it.
    desktop: Vec<(String, String, String)>,
}

impl Submissions {
    pub(super) fn collect(data: &(impl crate::ingest::jsonl::Text + ?Sized)) -> Self {
        let legacy_finder = memmem::Finder::new(b"\"user_message\"");
        let desktop_finder = memmem::Finder::new(b"\"UserMessage\"");
        let mut texts: Vec<String> = Vec::new();
        let mut desktop = Vec::new();
        for line in data.lines() {
            let line = line.as_ref();
            if legacy_finder.find(line.as_bytes()).is_none()
                && desktop_finder.find(line.as_bytes()).is_none()
            {
                continue;
            }
            let Ok(parsed) = serde_json::from_str::<SubmissionLine>(line) else {
                continue;
            };
            if parsed.ty.as_deref() != Some("event_msg") {
                continue;
            }
            let Some(payload) = parsed.payload else {
                continue;
            };
            match payload.ty.as_deref() {
                Some("user_message") => texts.extend(legacy_submission(&payload)),
                Some("item_completed") => desktop.extend(desktop_submission(payload)),
                _ => {}
            }
        }
        texts.sort_unstable();
        texts.dedup();
        desktop.sort_unstable();
        desktop.dedup();
        Self { texts, desktop }
    }

    /// Add a later parse's submissions to the carried ones, keeping both lists sorted and deduped.
    pub(super) fn merge(&mut self, fresh: Self) {
        self.texts.extend(fresh.texts);
        self.texts.sort_unstable();
        self.texts.dedup();
        self.desktop.extend(fresh.desktop);
        self.desktop.sort_unstable();
        self.desktop.dedup();
    }

    /// Drop submissions already consumed by `out` before carrying the log forward, except
    /// those that can still attest the last row.
    pub(super) fn forget_consumed(&mut self, out: &[RawMessage]) {
        let last = out.last().map(|m| m.text.as_str()).unwrap_or("");
        let consumed: std::collections::HashSet<_> = out.iter().map(|m| m.text.as_str()).collect();
        self.texts
            .retain(|t| !consumed.contains(t.as_str()) || last.starts_with(t.as_str()));
        self.desktop
            .retain(|(_, _, t)| !consumed.contains(t.as_str()) || last == t);
    }

    pub(super) fn is_empty(&self) -> bool {
        self.texts.is_empty() && self.desktop.is_empty()
    }

    /// Both schemas allow markers appended to the submitted text (pasted images); Desktop
    /// text must also come from the same thread and turn.
    pub(super) fn attests(&self, text: &str, session: Option<&str>, turn_id: Option<&str>) -> bool {
        let text = text.trim();
        let after = self.texts.partition_point(|s| s.as_str() <= text);
        if after > 0 && text.starts_with(self.texts[after - 1].as_str()) {
            return true;
        }
        let (Some(session), Some(turn_id)) = (session, turn_id) else {
            return false;
        };
        let from = self.desktop.partition_point(|(thread, turn, _)| {
            (thread.as_str(), turn.as_str()) < (session, turn_id)
        });
        self.desktop[from..]
            .iter()
            .take_while(|(thread, turn, _)| thread == session && turn == turn_id)
            .any(|(_, _, submitted)| text.starts_with(submitted.as_str()))
    }
}

fn legacy_submission(payload: &SubmissionPayload<'_>) -> Option<String> {
    payload
        .message
        .as_deref()
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
}

/// A Desktop `item_completed` UserMessage as `(thread, turn, text)`.
fn desktop_submission(payload: SubmissionPayload<'_>) -> Option<(String, String, String)> {
    let (Some(thread_id), Some(turn_id), Some(item)) =
        (payload.thread_id, payload.turn_id, payload.item)
    else {
        return None;
    };
    if thread_id.is_empty() || turn_id.is_empty() || item.ty.as_deref() != Some("UserMessage") {
        return None;
    }
    let mut parts = item
        .content
        .unwrap_or_default()
        .into_iter()
        .filter(|block| block.ty.as_deref() == Some("text"))
        .filter_map(|block| block.text);
    let mut text = parts.next()?.into_owned();
    for part in parts {
        if !text.is_empty() {
            text.push('\n');
        }
        text.push_str(&part);
    }
    text.truncate(text.trim_end().len());
    text.replace_range(..text.len() - text.trim_start().len(), "");
    if text.is_empty() {
        return None;
    }
    Some((thread_id.into_owned(), turn_id.into_owned(), text))
}
