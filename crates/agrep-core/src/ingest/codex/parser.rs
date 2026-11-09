//! The per-record state machine turning rollout lines into rows and tool events.

use std::path::Path;

use serde_json::value::RawValue;

use super::discover::rollout_stem;
use super::prefilter::{
    canonical_timestamp, fast_class, raw_call_output, FallbackNeedles, FastClass,
};
use super::records::{extract_assistant, extract_text, Block, Line, Payload};
use super::state::{CodexState, Finished};
use super::subagent::{delegated_input, is_guardian_source, subagent_anchor, SubagentContext};
use super::submissions::Submissions;
use super::tools::{call_input, call_status, parse_call_output, web_search_query, CallInput};
use super::{is_codex_injected, COMPACTED_MARKER};
use crate::ingest::{cap_str_with_chars, is_wrapper, parse_timestamp, project_name, EVENT_CAP};
use crate::intake::{Skip, Tally};
use crate::intake_batch::ReadOutcome;
use crate::model::{Event, Message, RawMessage};

pub(super) type Parsed = (Vec<Message>, Vec<Event>, ReadOutcome);

pub(super) fn parse_data(
    path: &Path,
    tally: std::sync::Arc<Tally>,
    data: &(impl crate::ingest::jsonl::Text + ?Sized),
    previous: Option<CodexState>,
) -> (Parsed, CodexState) {
    let mut parser = Parser::new(path, &tally, data, previous);
    let mut lines = 0;
    for (offset, line) in data.lines().enumerate() {
        parser.line(offset, line.as_ref());
        lines = offset + 1;
    }
    parser.finish(lines)
}

/// Derive a session id from a `rollout-<ISO>-<uuid>.jsonl[.zst]` filename (fallback when the
/// `session_meta` line is missing its id).
fn session_from_filename(path: &Path) -> String {
    let stem = rollout_stem(path).unwrap_or_default();
    // The id is the trailing UUID (5 dash-separated groups).
    let parts: Vec<&str> = stem.split('-').collect();
    if parts.len() >= 5 {
        parts[parts.len() - 5..].join("-")
    } else {
        stem.to_string()
    }
}

struct Parser<'a> {
    path: &'a Path,
    /// The record ordinal is the stable fallback identity for store records that carry no
    /// call_id. Include the rollout stem because one logical session may have more than one
    /// source file after resume/import; an ordinal alone would conflate unrelated calls.
    source_stem: &'a str,
    tally: &'a Tally,
    state: CodexState,
    /// Rows and events resumed from the previous parse, whose tally was already counted.
    carried_rows: usize,
    carried_events: usize,
    needles: FallbackNeedles,
    fast_meta: u64,
    fast_non_message: u64,
}

impl<'a> Parser<'a> {
    fn new(
        path: &'a Path,
        tally: &'a Tally,
        data: &(impl crate::ingest::jsonl::Text + ?Sized),
        previous: Option<CodexState>,
    ) -> Self {
        let mut state = previous.unwrap_or_default();
        state.submissions.merge(Submissions::collect(data));
        state.reset = false;
        Self {
            path,
            source_stem: rollout_stem(path).unwrap_or("rollout"),
            tally,
            carried_rows: state.out.len(),
            carried_events: state.events.len(),
            state,
            needles: FallbackNeedles::new(),
            fast_meta: 0,
            fast_non_message: 0,
        }
    }

    fn line(&mut self, offset: usize, line: &str) {
        let record_ordinal = self.state.ordinal + offset;
        if line.is_empty() {
            return;
        }
        // Guardian rollouts are automatic approval-review control traffic. Once
        // session_meta proves that subtype, account every remaining record without
        // parsing or indexing its large echoed parent-history wrapper.
        if self.state.internal_guardian {
            self.tally.seen();
            self.tally.skip(Skip::Sidechain);
            return;
        }
        if !self.fast_path(line, record_ordinal) {
            return;
        }
        let record: Line = match serde_json::from_str(line) {
            Ok(record) => record,
            Err(e) => {
                self.tally
                    .error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                return;
            }
        };
        self.record(&record, record_ordinal);
    }

    /// Settle what the byte prefilter can; returns whether the record still needs serde.
    fn fast_path(&mut self, line: &str, record_ordinal: usize) -> bool {
        let bytes = line.as_bytes();
        match fast_class(bytes) {
            FastClass::Parse => self.tally.seen(),
            FastClass::CallOutput => {
                self.tally.seen();
                if self.replaying() {
                    self.tally.skip(Skip::Replay);
                    return false;
                }
                // A future reordered/pretty output record falls through to serde.
                let Some((id, raw_output)) = raw_call_output(line) else {
                    return true;
                };
                self.attach_output(id, Some(raw_output));
                self.tally.agent_row();
                return false;
            }
            FastClass::Meta => {
                self.fast_meta += 1;
                return false;
            }
            FastClass::Compact => {
                self.tally.seen();
                self.compacted(canonical_timestamp(bytes), record_ordinal);
                return false;
            }
            FastClass::NonMessage => {
                self.fast_non_message += 1;
                return false;
            }
            FastClass::Fallback => {
                if !self.needles.admits(bytes) {
                    // Non-canonical reasoning/delta records: structural, never message
                    // payloads. The fallback retains compatibility with pretty/reordered JSON.
                    self.fast_non_message += 1;
                    return false;
                }
                self.tally.seen();
            }
        }
        true
    }

    fn record(&mut self, record: &Line<'_>, record_ordinal: usize) {
        let timestamp = record.timestamp.as_deref();
        let payload = record.payload.as_ref();
        match record.ty.as_deref() {
            Some("compacted") => self.compacted(timestamp, record_ordinal),
            Some("session_meta") => {
                self.session_meta(payload);
                self.tally.skip(Skip::Meta);
            }
            Some("event_msg") => {
                self.event_msg(payload);
                self.tally.skip(Skip::Meta);
            }
            Some("turn_context") => {
                self.turn_context(payload);
                self.tally.skip(Skip::Meta);
            }
            Some("response_item") => match payload {
                Some(payload) => self.response_item(payload, timestamp, record_ordinal),
                None => self.tally.skip(Skip::NonMessage),
            },
            _ => self.tally.skip(Skip::NonMessage),
        }
    }

    /// A native context-compaction boundary becomes a recap row plus a control event.
    fn compacted(&mut self, timestamp: Option<&str>, record_ordinal: usize) {
        if self.replaying() {
            self.tally.skip(Skip::Replay);
            return;
        }
        self.tally.row();
        self.state.recap_turns.push(self.state.turn);
        let row = self.row(timestamp, String::new(), String::new());
        self.state.out.push(row);
        self.tally.event();
        let marker = Event {
            turn: None,
            ..self.event(
                timestamp,
                "control",
                COMPACTED_MARKER.to_string(),
                format!("codex:{}:{record_ordinal}:compacted", self.source_stem),
            )
        };
        self.state.events.push(marker);
        self.state.turn += 1;
    }

    /// The first metadata record names the rollout file. Forked Codex rollouts then
    /// replay their parent's history, including parent session_meta records; allowing
    /// those to overwrite this identity collapses every child back into its parent.
    fn session_meta(&mut self, payload: Option<&Payload<'_>>) {
        let state = &mut self.state;
        if state.first_meta_seen {
            if let (Some(ctx), Some(meta), Some(session_id)) =
                (state.subagent.as_mut(), payload, state.session.as_deref())
            {
                ctx.note_session_meta(meta.id.as_deref(), session_id);
            }
            return;
        }
        state.first_meta_seen = true;
        let Some(meta) = payload else {
            return;
        };
        if let Some(cwd) = meta.cwd.as_deref() {
            state.project = Some(project_name(cwd));
        }
        if let Some(id) = meta.id.as_deref().filter(|id| !id.is_empty()) {
            state.session = Some(id.to_string());
        }
        if meta.thread_source.as_deref() != Some("subagent") {
            return;
        }
        if is_guardian_source(meta.source) {
            state.internal_guardian = true;
            return;
        }
        state.subagent = Some(SubagentContext::from_session_meta(meta));
    }

    fn event_msg(&mut self, payload: Option<&Payload<'_>>) {
        let state = &mut self.state;
        if let (Some(ctx), Some(event), Some(session_id)) =
            (state.subagent.as_mut(), payload, state.session.as_deref())
        {
            ctx.note_event(event, session_id);
        }
    }

    /// turn_context announces the model in force for the turns that follow.
    fn turn_context(&mut self, payload: Option<&Payload<'_>>) {
        if let Some(model) = payload
            .and_then(|p| p.model.as_deref())
            .filter(|model| !model.is_empty())
        {
            self.state.current_model = model.to_string();
        }
    }

    fn response_item(
        &mut self,
        payload: &Payload<'_>,
        timestamp: Option<&str>,
        record_ordinal: usize,
    ) {
        if payload.ty.as_deref() == Some("agent_message") {
            self.agent_message(payload, timestamp);
            return;
        }
        let message_turn_id = payload
            .internal_chat_message_metadata_passthrough
            .as_ref()
            .and_then(|m| m.turn_id.as_deref());
        if payload.ty.as_deref() == Some("message") && payload.role.as_deref() == Some("user") {
            if let Some(ctx) = self
                .state
                .subagent
                .as_ref()
                .filter(|ctx| ctx.agent_path.is_none())
            {
                let label = ctx.prompt_label(message_turn_id);
                self.pathless_child_message(payload, timestamp, message_turn_id, label);
                return;
            }
        }
        if self.replaying() {
            self.tally.skip(Skip::Replay);
            return;
        }
        // Tool stream -> events. (Reasoning stays skipped.)
        match payload.ty.as_deref() {
            Some("function_call") | Some("custom_tool_call") | Some("tool_search_call") => {
                self.tool_call(payload, timestamp, record_ordinal);
            }
            Some("function_call_output") | Some("custom_tool_call_output") => {
                if let Some(id) = payload.call_id.as_deref() {
                    self.attach_output(id, payload.output.map(RawValue::get));
                }
                self.tally.agent_row();
            }
            Some("web_search_call") => self.web_search(payload, timestamp, record_ordinal),
            Some("message") => self.message(payload, timestamp, message_turn_id),
            _ => self.tally.skip(Skip::NonMessage),
        }
    }

    /// A child rollout contains a verbatim parent replay before this inbound team
    /// message. Start the child transcript here, then accept later MESSAGE handoffs as
    /// additional turns. Outbound child messages have a different recipient.
    fn agent_message(&mut self, payload: &Payload<'_>, timestamp: Option<&str>) {
        let anchor = self
            .state
            .subagent
            .as_ref()
            .and_then(|ctx| ctx.agent_path.as_deref())
            .and_then(|agent_path| subagent_anchor(payload, agent_path));
        let Some(text) = anchor else {
            // inter-agent chatter routed elsewhere (outbound child replies etc.)
            self.tally.skip(Skip::NonHuman);
            return;
        };
        self.open_child();
        self.push_prompt(timestamp, text);
    }

    /// Legacy/native subagents with no routing path carry a real plaintext task.
    /// Require its passthrough turn id to match the child-owned task_started marker
    /// (`label` is `None` until it does); without that proof, keep suppressing the
    /// inherited parent transcript.
    fn pathless_child_message(
        &mut self,
        payload: &Payload<'_>,
        timestamp: Option<&str>,
        message_turn_id: Option<&str>,
        label: Option<&'static str>,
    ) {
        let text = payload
            .content
            .as_deref()
            .and_then(extract_text)
            .and_then(|text| self.child_task_text(text, message_turn_id));
        let (Some(text), Some(label)) = (text, label) else {
            // suppressed inherited-parent transcript before the proven boundary
            self.tally.skip(Skip::Replay);
            return;
        };
        self.open_child();
        self.push_prompt(timestamp, format!("[subagent {label}] {text}"));
    }

    /// A delegated task reaches a child without a keyboard, so an empty log says nothing
    /// here. A log that exists and does not attest does: the text is the replayed parent
    /// prologue.
    fn child_task_text(&self, text: String, message_turn_id: Option<&str>) -> Option<String> {
        let submissions = &self.state.submissions;
        delegated_input(&text).or_else(|| {
            (!is_wrapper(&text)
                && !is_codex_injected(&text)
                && (submissions.is_empty()
                    || submissions.attests(&text, self.state.session.as_deref(), message_turn_id)))
            .then_some(text)
        })
    }

    /// The child's own transcript starts at its first proven prompt: everything before it,
    /// including resumed rows, was the inherited parent replay.
    fn open_child(&mut self) {
        let state = &mut self.state;
        let Some(ctx) = state.subagent.as_mut() else {
            return;
        };
        if ctx.started {
            return;
        }
        ctx.started = true;
        state.pending.clear();
        self.tally
            .discard_rows_as_replay(state.out.len().saturating_sub(self.carried_rows) as u64);
        self.tally
            .discard_events(state.events.len().saturating_sub(self.carried_events) as u64);
        state.events.clear();
        state.order.reset(&state.events);
        state.out.clear();
        state.turn = 0;
        self.carried_rows = 0;
        self.carried_events = 0;
        state.reset = true;
    }

    fn tool_call(&mut self, payload: &Payload<'_>, timestamp: Option<&str>, record_ordinal: usize) {
        // tool_search_call records carry no `name`; label them for the mix stats
        let fallback = if payload.ty.as_deref() == Some("tool_search_call") {
            "tool_search"
        } else {
            "?"
        };
        let name = payload.name.as_deref().unwrap_or(fallback).to_string();
        let CallInput {
            input,
            input_chars,
            control,
            task_state,
        } = call_input(&name, payload);
        let ok = call_status(payload.status.as_deref());
        let call_id = self.event_id(payload, record_ordinal);
        // Native ids pair with output records. A synthesized source-record id still
        // gives the call durable identity, but cannot invent a correlation the store
        // did not record.
        if payload.call_id.as_deref().is_some_and(|id| !id.is_empty()) {
            self.state
                .pending
                .insert(call_id.clone(), self.state.events.len());
        }
        self.tally.agent_row();
        self.tally.event();
        let kind = if control { "control" } else { "tool" };
        let event = Event {
            input,
            input_chars,
            ok,
            state: task_state,
            ..self.event(timestamp, kind, name, call_id)
        };
        self.state.events.push(event);
    }

    fn web_search(
        &mut self,
        payload: &Payload<'_>,
        timestamp: Option<&str>,
        record_ordinal: usize,
    ) {
        self.tally.agent_row();
        self.tally.event();
        let query = web_search_query(payload.action);
        let (input, input_chars) = cap_str_with_chars(&query, EVENT_CAP);
        let call_id = self.event_id(payload, record_ordinal);
        let event = Event {
            input,
            input_chars,
            ..self.event(timestamp, "tool", "web_search".to_string(), call_id)
        };
        self.state.events.push(event);
    }

    /// Pair a call output with its pending call by native call id.
    fn attach_output(&mut self, id: &str, raw_output: Option<&str>) {
        let Some(&i) = self.state.pending.get(id) else {
            return;
        };
        let (text, chars, bytes, ok) = raw_output
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(raw).ok())
            .map(|value| parse_call_output(&value))
            .unwrap_or_default();
        let event = &mut self.state.events[i];
        event.output = text;
        event.output_chars = chars;
        event.output_bytes = bytes;
        // No fabricated Some(true): codex output carries no exit codes and
        // "arrived" != "worked", so ok stays None (reported as not-recorded)
        if ok.is_some() {
            event.ok = ok;
        }
        self.state.pending.remove(id);
    }

    fn message(&mut self, payload: &Payload<'_>, timestamp: Option<&str>, turn_id: Option<&str>) {
        let Some(blocks) = payload.content.as_deref() else {
            self.tally.skip(Skip::EmptyText);
            return;
        };
        // Assistant prose -> attach to the user message it answers.
        if payload.role.as_deref() == Some("assistant") {
            self.attach_reply(blocks);
            self.tally.agent_row();
            return;
        }
        // Only the human past here; skip developer / system.
        if payload.role.as_deref() != Some("user") {
            self.tally.skip(Skip::NonHuman);
            return;
        }
        let Some(text) = extract_text(blocks) else {
            self.tally.skip(Skip::EmptyText);
            return;
        };
        if is_wrapper(&text) || is_codex_injected(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        // An absent submission is not evidence of a human turn: a subagent thread
        // logs none because nobody is at the keyboard, and its real inbound work
        // arrives on the handoff paths above.
        let session = self.state.session.as_deref();
        if !self.state.submissions.attests(&text, session, turn_id) {
            self.tally.skip(Skip::NonHuman);
            return;
        }
        self.push_prompt(timestamp, text);
    }

    fn attach_reply(&mut self, blocks: &[Block<'_>]) {
        let Some(txt) = extract_assistant(blocks) else {
            return;
        };
        let state = &mut self.state;
        let Some(last) = state.out.last_mut() else {
            return;
        };
        let chars = crate::ingest::append_reply(last, &txt);
        if chars > 0 {
            state
                .order
                .joined(&mut last.reply_shape, &state.events, state.events.len());
            state.order.reply(state.events.len());
        }
        if last.model.is_empty() && !state.current_model.is_empty() {
            last.model = state.current_model.clone();
        }
    }

    fn push_prompt(&mut self, timestamp: Option<&str>, text: String) {
        self.tally.row();
        self.state.order.prompt(&mut self.state.events);
        let row = self.row(timestamp, text, self.state.current_model.clone());
        self.state.out.push(row);
        self.state.turn += 1;
    }

    fn row(&self, timestamp: Option<&str>, text: String, model: String) -> RawMessage {
        let subagent = self.state.subagent.as_ref();
        RawMessage {
            agent: "codex",
            project: self
                .state
                .project
                .clone()
                .unwrap_or_else(|| "unknown".to_string()),
            session: self.session_id(),
            ts: parse_timestamp::rfc3339(timestamp),
            turn: self.state.turn,
            text,
            model,
            reply: String::new(),
            reply_chars: 0,
            side: subagent.is_some(),
            parent: subagent
                .map(|ctx| ctx.parent_session.clone())
                .unwrap_or_default(),
            reply_shape: Default::default(),
        }
    }

    /// An event attributed to the current turn, with no input, output or outcome yet.
    fn event(
        &self,
        timestamp: Option<&str>,
        kind: &'static str,
        name: String,
        call_id: String,
    ) -> Event {
        Event {
            turn: self.state.out.last().map(|m| m.turn),
            agent: "codex",
            session: self.session_id(),
            ts: parse_timestamp::rfc3339(timestamp),
            kind,
            name,
            input: String::new(),
            output: String::new(),
            input_chars: 0,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id,
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        }
    }

    fn event_id(&self, payload: &Payload<'_>, record_ordinal: usize) -> String {
        payload
            .call_id
            .as_deref()
            .or(payload.id.as_deref())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("codex:{}:{record_ordinal}", self.source_stem))
    }

    fn session_id(&self) -> String {
        self.state
            .session
            .clone()
            .unwrap_or_else(|| session_from_filename(self.path))
    }

    /// A subagent rollout before its proven boundary is still replaying its parent.
    fn replaying(&self) -> bool {
        self.state.subagent.as_ref().is_some_and(|ctx| !ctx.started)
    }

    fn finish(self, lines: usize) -> (Parsed, CodexState) {
        let Self {
            tally,
            mut state,
            fast_meta,
            fast_non_message,
            ..
        } = self;
        state.order.finish(&mut state.events);
        // Canonical prefilter skips dominate the line count. Fold them into the per-file tally
        // once, keeping the same seen identity while avoiding millions of atomic increments.
        tally.seen_n(fast_meta + fast_non_message);
        tally.skip_n(Skip::Meta, fast_meta);
        tally.skip_n(Skip::NonMessage, fast_non_message);
        state.submissions.forget_consumed(&state.out);
        let (
            next,
            Finished {
                out,
                recap_turns,
                events,
            },
        ) = state.checkpoint(lines);
        let messages = out
            .into_iter()
            .map(|raw| {
                let turn = raw.turn;
                let mut m = raw.freeze();
                if recap_turns.contains(&turn) {
                    m.who = "recap".into();
                    m.model_source = "recap".into();
                }
                m
            })
            .collect();
        ((messages, events, ReadOutcome::Complete), next)
    }
}
