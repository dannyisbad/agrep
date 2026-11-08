//! Record parsing: one pass over a transcript's lines into human messages, the replies that
//! answer them and tool/control events, continuing from a resumed state when given one.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use memchr::memmem;
use serde_json::value::RawValue;
use serde_json::Value;

use super::notifications::{
    is_task_notification_row, notification_blocks, push_task_notifications,
    queued_notification_prompt,
};
use super::project::{collect_tool_paths, directory_project, primary_project, side_context};
use super::records::{
    extract_text, has_json_key, is_api_error_row, optional_cwd, parse_content, raw_str_value, Line,
    ProjectLine,
};
use super::resume::ClaudeState;
use super::throwaway::{is_throwaway_path, tally_throwaway};
use super::tool_calls::{apply_tool_result, call_meta, tool_kind};
use super::API_ERROR_MARKER;
use crate::ingest::jsonl::Text;
use crate::ingest::{cap_str_with_chars, is_wrapper, summarize_tool_input_with_chars, EVENT_CAP};
use crate::intake::{Skip, Tally};
use crate::intake_batch::ReadOutcome;
use crate::model::{Event, Message, RawMessage};

type Parsed = (Vec<Message>, Vec<Event>, ReadOutcome);

pub(super) fn parse_file(path: &Path) -> Parsed {
    let tally = crate::intake::tally();
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            // a present-but-unreadable file is a real problem; silence would make a
            // permissions break look identical to "no history"
            crate::intake::diagnostic(format_args!(
                "  ! claude: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            ));
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest::read_failure_outcome(&e),
            );
        }
    };
    parse_data(path, tally, &data, None).0
}

pub(super) fn parse_data(
    path: &Path,
    tally: Arc<Tally>,
    data: &(impl Text + ?Sized),
    previous: Option<ClaudeState>,
) -> (Parsed, ClaudeState) {
    let resumed = previous.is_some();
    let state = previous.unwrap_or_default();
    let side_parent = if resumed {
        state.side_parent.clone()
    } else {
        side_context(path)
    };
    if is_throwaway_path(path) {
        tally_throwaway(&tally, data);
        return ((Vec::new(), Vec::new(), ReadOutcome::Complete), state);
    }
    let mut parser = Parser::new(path, tally, state, side_parent);
    if parser.is_side && !resumed {
        parser.adopt_inner_parent(data);
    }
    for (offset, line) in data.lines().enumerate() {
        parser.record(parser.state.ordinal + offset, line.as_ref());
    }
    parser.finish(path, data)
}

/// Cheap key probes choose which borrowed schema to parse; attribution still comes from JSON.
struct Probes {
    message: memmem::Finder<'static>,
    cwd: memmem::Finder<'static>,
    tool_result: memmem::Finder<'static>,
    queued: memmem::Finder<'static>,
}

impl Probes {
    fn new() -> Self {
        Probes {
            message: memmem::Finder::new(b"\"message\""),
            cwd: memmem::Finder::new(b"\"cwd\""),
            tool_result: memmem::Finder::new(b"\"tool_result\""),
            queued: memmem::Finder::new(b"\"queued_command\""),
        }
    }
}

/// A message row being handled: its record, the session it belongs to and its file ordinal.
struct Row<'r, 'a> {
    line: &'r Line<'a>,
    session: String,
    ordinal: usize,
}

/// One file's parse. `state` carries what a resumed parse continues; the rest is per pass.
struct Parser {
    tally: Arc<Tally>,
    state: ClaudeState,
    file_session: String,
    is_side: bool,
    cwd_counts: HashMap<String, usize>,
    tool_paths: Vec<String>,
    probes: Probes,
}

impl Parser {
    fn new(
        path: &Path,
        tally: Arc<Tally>,
        mut state: ClaudeState,
        side_parent: Option<String>,
    ) -> Self {
        let file_session = path
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        let is_side = side_parent.is_some();
        state.side_parent = side_parent;
        Parser {
            tally,
            state,
            file_session,
            is_side,
            cwd_counts: HashMap::new(),
            tool_paths: Vec::new(),
            probes: Probes::new(),
        }
    }

    /// A side file's parent is decided ONCE, before any row is emitted: the first inner
    /// sessionId (the parent's indexed identity, preferred since a dir stem can drift),
    /// else the dir stem. A parent that flips mid-file splits one child across two ids.
    fn adopt_inner_parent(&mut self, data: &(impl Text + ?Sized)) {
        let f_sess = memmem::Finder::new(b"\"sessionId\":\"");
        if let Some(pid) = data.lines().find_map(|line| {
            raw_str_value(&f_sess, line.as_bytes())
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
        }) {
            self.state.side_parent = Some(pid);
        }
    }

    fn row_session(&self, l: &Line) -> String {
        match l.session_id.as_deref() {
            Some(id) if !self.is_side => id.to_string(),
            _ => self.file_session.clone(),
        }
    }

    fn row_id(&self, ordinal: usize) -> String {
        format!("claude:{}:{ordinal}", self.file_session)
    }

    /// Inline sidechain rows of a top-level session (a side file's own rows are its content).
    fn is_inline_side(&self, l: &Line) -> bool {
        l.is_sidechain == Some(true) && self.state.side_parent.is_none()
    }

    fn count_cwd(&mut self, cwd: Option<String>) {
        let Some(cwd) = cwd else {
            return;
        };
        if self.state.first_cwd.is_empty() {
            self.state.first_cwd = cwd.clone();
        }
        *self.cwd_counts.entry(cwd).or_insert(0) += 1;
    }

    fn record(&mut self, ordinal: usize, line: &str) {
        if line.is_empty() {
            return;
        }
        self.tally.seen();
        let bytes = line.as_bytes();
        if !has_json_key(&self.probes.message, bytes) {
            self.non_message_line(ordinal, line);
            return;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(e) => {
                self.tally
                    .error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                return;
            }
        };
        self.count_cwd(optional_cwd(l.cwd));
        let row = Row {
            session: self.row_session(&l),
            line: &l,
            ordinal,
        };
        if self.is_inline_side(&l) {
            // inline sidechain rows duplicate the subagents/ child transcripts (their own
            // side sessions); dropping the whole line covers events and reply text too
            self.tally.skip(Skip::Sidechain);
            return;
        }
        if l.ty.as_deref() == Some("assistant") {
            self.assistant_row(&row);
            self.tally.agent_row();
            return;
        }
        if l.ty.as_deref() != Some("user") {
            self.tally.skip(Skip::NonHuman);
            return;
        }
        self.user_row(row, bytes);
    }

    /// A line with no `message` key: progress, summary, file-history or attachment rows.
    fn non_message_line(&mut self, ordinal: usize, line: &str) {
        let bytes = line.as_bytes();
        // a notification delivered mid-turn is an attachment row, which has no message
        if self.probes.queued.find(bytes).is_some() {
            self.queued_attachment(ordinal, line);
        }
        if has_json_key(&self.probes.cwd, bytes) {
            if let Ok(fields) = serde_json::from_str::<ProjectLine>(line) {
                self.count_cwd(optional_cwd(fields.cwd));
            }
        }
        // progress / summary / file-history lines: structural, no message payload
        self.tally.skip(Skip::NonMessage);
    }

    fn queued_attachment(&mut self, ordinal: usize, line: &str) {
        let Ok(l) = serde_json::from_str::<Line>(line) else {
            return;
        };
        let inline_side = self.is_inline_side(&l);
        let Some(prompt) = queued_notification_prompt(l.attachment) else {
            return;
        };
        if inline_side {
            return;
        }
        let session = self.row_session(&l);
        let row_id = self.row_id(ordinal);
        push_task_notifications(
            &mut self.state.events,
            &self.tally,
            (&session, l.ts(), &row_id),
            None,
            Some(&notification_blocks(Some(&prompt))),
        );
    }

    /// Assistant turn -> attach its text + model to the user message it answers, and note
    /// the files it touched (to infer the real working dir at the end).
    fn assistant_row(&mut self, row: &Row) {
        let Some(m) = &row.line.message else {
            return;
        };
        if m.role.as_deref() != Some("assistant") {
            return;
        }
        // Assistant content is always needed (tool paths, events, reply text):
        // parse the RawValue into a DOM once.
        let content = parse_content(m.content);
        // events recorded before the row's last text block were answered by it
        let mut text_at: Option<usize> = None;
        if let Some(content) = &content {
            collect_tool_paths(content, &mut self.tool_paths);
            // tool_use blocks -> events (inline sidechain lines never reach
            // here; the subagents/ child transcript is their one source)
            if let Value::Array(blocks) = content {
                text_at = self.tool_uses(row, blocks);
            }
        }
        let text = content.as_ref().and_then(extract_text);
        if is_api_error_row(row.line) {
            self.push_api_error(row, text.as_deref());
        }
        self.attach_reply(m.model.as_deref(), text.as_deref(), text_at);
    }

    /// Record the row's tool_use blocks; returns the event count at its last non-blank text.
    fn tool_uses(&mut self, row: &Row, blocks: &[Value]) -> Option<usize> {
        let mut text_at = None;
        for (block_ordinal, b) in blocks.iter().enumerate() {
            let ty = b.get("type").and_then(|t| t.as_str());
            if ty == Some("text") {
                if b.get("text")
                    .and_then(|t| t.as_str())
                    .is_some_and(|t| !t.trim().is_empty())
                {
                    text_at = Some(self.state.events.len());
                }
                continue;
            }
            if ty == Some("tool_use") {
                self.push_tool_use(row, block_ordinal, b);
            }
        }
        text_at
    }

    fn push_tool_use(&mut self, row: &Row, block_ordinal: usize, b: &Value) {
        let name = b
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("?")
            .to_string();
        let native_call_id = b
            .get("id")
            .and_then(|v| v.as_str())
            .filter(|id| !id.trim().is_empty());
        let call_id = native_call_id.map(str::to_string).unwrap_or_else(|| {
            format!(
                "claude:{}:{}:{block_ordinal}",
                self.file_session, row.ordinal
            )
        });
        let (input, input_chars) = b
            .get("input")
            .map(summarize_tool_input_with_chars)
            .unwrap_or_default();
        let kind = tool_kind(&name);
        if native_call_id.is_some() {
            self.state
                .pending
                .insert(call_id.clone(), self.state.events.len());
        }
        let meta = call_meta(&name, b.get("input"));
        let state = crate::ingest::task_state(&name, b.get("input"));
        self.tally.event();
        self.state.events.push(Event {
            turn: self.state.out.last().map(|m| m.turn),
            agent: "claude",
            session: row.session.clone(),
            ts: row.line.ts(),
            kind,
            name,
            input,
            output: String::new(),
            input_chars,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id,
            child_session: String::new(),
            meta,
            state,
        });
    }

    fn push_api_error(&mut self, row: &Row, text: Option<&str>) {
        // the text also stays in the reply, so stored replies and search keep it
        let error = text.map_or("", str::trim);
        let (input, input_chars) = cap_str_with_chars(error, EVENT_CAP);
        self.tally.event();
        self.state.events.push(Event {
            turn: self.state.out.last().map(|m| m.turn),
            agent: "claude",
            session: row.session.clone(),
            ts: row.line.ts(),
            kind: "control",
            name: API_ERROR_MARKER.to_string(),
            input,
            output: String::new(),
            input_chars,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id: format!("claude:{}:{}:api-error", self.file_session, row.ordinal),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
    }

    /// Stamp the answered message with the reply's model and append the reply text.
    fn attach_reply(&mut self, model: Option<&str>, text: Option<&str>, text_at: Option<usize>) {
        let Some(last) = self.state.out.last_mut() else {
            return;
        };
        if last.model.is_empty() {
            if let Some(md) = model {
                // "<synthetic>" is a marker, not a model: a hook-injected
                // reply must not stamp the row it follows (it once
                // reclassified compact summaries and poisoned the pool).
                if !md.is_empty() && md != "<synthetic>" {
                    last.model = md.to_string();
                }
            }
        }
        let Some(txt) = text else {
            return;
        };
        let chars = crate::ingest::append_reply(last, txt);
        if chars > 0 {
            let at = text_at.unwrap_or(self.state.events.len());
            self.state
                .order
                .joined(&mut last.reply_shape, &self.state.events, at);
            self.state.order.reply(at);
        }
    }

    fn user_row(&mut self, row: Row, bytes: &[u8]) {
        // User content parses lazily: the tool_result pairing only runs when the raw line
        // contains the marker, and lines that fail the human filters never build a DOM.
        let raw_content = row.line.message.as_ref().and_then(|m| m.content);
        let mut content: Option<Value> = None;
        // tool_result blocks arrive in user-typed lines (userType external, isMeta null),
        // so pair them BEFORE the human-turn filters would drop the line.
        if self.probes.tool_result.find(bytes).is_some() {
            content = parse_content(raw_content);
            if let Some(Value::Array(blocks)) = &content {
                self.pair_tool_results(row.line, blocks);
            }
        }
        self.row_task_notifications(&row, raw_content, &mut content);
        let Some(text) = self.human_text(row.line, content) else {
            return;
        };
        self.push_message(row, text);
    }

    fn pair_tool_results(&mut self, l: &Line, blocks: &[Value]) {
        let is_result = |b: &Value| b.get("type").and_then(|t| t.as_str()) == Some("tool_result");
        // a row's toolUseResult belongs to its call only when it carries that one result
        let lone = blocks.iter().filter(|b| is_result(b)).count() == 1;
        for b in blocks.iter().filter(|b| is_result(b)) {
            let id = b.get("tool_use_id").and_then(|v| v.as_str()).unwrap_or("");
            let Some(&i) = self.state.pending.get(id) else {
                continue;
            };
            apply_tool_result(&mut self.state.events[i], b, l, lone);
            self.state.pending.remove(id);
        }
    }

    /// Markers for a user row that is an origin-tagged notification or carries a queued one.
    fn row_task_notifications(
        &mut self,
        row: &Row,
        raw_content: Option<&RawValue>,
        content: &mut Option<Value>,
    ) {
        let user_form = is_task_notification_row(row.line);
        let queued_prompt = queued_notification_prompt(row.line.attachment);
        if !user_form && queued_prompt.is_none() {
            return;
        }
        if user_form && content.is_none() {
            *content = parse_content(raw_content);
        }
        let user = user_form.then(|| notification_blocks(content.as_ref()));
        let queued = queued_prompt
            .as_ref()
            .map(|prompt| notification_blocks(Some(prompt)));
        let row_id = self.row_id(row.ordinal);
        push_task_notifications(
            &mut self.state.events,
            &self.tally,
            (&row.session, row.line.ts(), &row_id),
            user.as_deref(),
            queued.as_deref(),
        );
    }

    /// The text of a real human turn; None, with the skip tallied, for any other user row.
    fn human_text(&self, l: &Line, content: Option<Value>) -> Option<String> {
        if l.is_meta == Some(true) {
            self.tally.skip(Skip::Meta);
            return None;
        }
        if l.user_type.as_deref().is_some_and(|ut| ut != "external") {
            self.tally.skip(Skip::NonHuman);
            return None;
        }
        let Some(msg) = &l.message else {
            self.tally.skip(Skip::NonMessage);
            return None;
        };
        if msg.role.as_deref() != Some("user") {
            self.tally.skip(Skip::NonHuman);
            return None;
        }
        let content = content.or_else(|| parse_content(msg.content));
        let text = match content.as_ref().and_then(extract_text) {
            Some(t) if !t.trim().is_empty() => t,
            _ => {
                self.tally.skip(Skip::EmptyText);
                return None;
            }
        };
        if is_wrapper(&text) {
            self.tally.skip(Skip::Wrapper);
            return None;
        }
        Some(text)
    }

    fn push_message(&mut self, row: Row, text: String) {
        self.tally.row();
        let state = &mut self.state;
        if row.line.is_compact_summary == Some(true) {
            state.recap_turns.push(state.turn);
        } else {
            state.order.prompt(&mut state.events);
        }
        state.out.push(RawMessage {
            agent: "claude",
            project: String::new(), // filled once per session in `finish`
            session: row.session,
            ts: row.line.ts(),
            turn: state.turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: self.is_side,
            parent: state.side_parent.clone().unwrap_or_default(),
            reply_shape: Default::default(),
        });
        state.turn += 1;
    }

    fn finish(mut self, path: &Path, data: &(impl Text + ?Sized)) -> (Parsed, ClaudeState) {
        let state = &mut self.state;
        state.order.finish(&mut state.events);
        // One project for the whole session: where the work actually happened.
        let project = primary_project(
            &self.cwd_counts,
            &self.tool_paths,
            &state.first_cwd,
            &directory_project(path),
            &mut state.project_votes,
        );
        for m in &mut state.out {
            m.project = project.clone();
        }
        let read_outcome = read_outcome(&state.out, &state.events, data);
        let next = state.checkpoint(data.lines().count());
        let (messages, events) = self.state.into_output();
        ((messages, events, read_outcome), next)
    }
}

/// Invalid when nothing parsed and some non-blank line is not JSON at all.
fn read_outcome(out: &[RawMessage], events: &[Event], data: &(impl Text + ?Sized)) -> ReadOutcome {
    if out.is_empty()
        && events.is_empty()
        && data.lines().any(|line| {
            !line.trim().is_empty() && serde_json::from_str::<&RawValue>(&line).is_err()
        })
    {
        ReadOutcome::Invalid
    } else {
        ReadOutcome::Complete
    }
}
